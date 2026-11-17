"""The Pocket engine: reads Tyl trader - Grupo VIP 📊 (read-only) and runs Camino 1. With a DEMO SSID saved it plays on
the Pocket Option DEMO account at the exact seconds (disparo.run_signal) and books each signal with the broker's own
results; without one it runs in PAPER mode on the results the group publishes. Short martingale (22 Oct 2026): J2 → J3 in
the same signal from one shared book, booked with the signal's whole result. Its own process: stopping or restarting it
never reaches the gold bot."""
from __future__ import annotations

import asyncio
import json
import msvcrt
import os
import sqlite3
import sys
import time

from .conector import deal_net
from .core import day_stopped, mexico, parse_result, parse_signal, settle_net, settle_signal
from .disparo import Clock, Step, asset_code, run_signal, won
from .secret import demo_check, has_ssid, protect
from .store import Store, data_dir, load_config, plan_of, signalbridge_dir

RESULT_WINDOW = 45 * 60  # a result post belongs to the latest open signal whose 1st candle began this recently
NO_RESULT = 30 * 60  # after the last candle, without a result post the signal is closed without plays


def money(v: float) -> str:
    return f"${v:,.2f}"


def quick_edit_off() -> bool:
    """25 Oct 2026, 12:11-18:10: a click in the console left it in QuickEdit "Seleccionar" mode and the engine froze on
    its next print for six hours. Without QuickEdit a click no longer selects (nor freezes) the window."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel.GetStdHandle(-10)  # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    if not handle or not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
        return False  # no console (pythonw, redirected input)
    return bool(kernel.SetConsoleMode(handle, (mode.value | 0x0080) & ~0x0040))  # EXTENDED_FLAGS on, QUICK_EDIT off


class PocketEngine:
    """Message handling and paper display states; no network (testable). In demo mode the runner plays the signals."""

    def __init__(self, store: Store, cfg: dict, demo: "DemoRunner | None" = None):
        self.store, self.cfg, self.demo = store, cfg, demo
        self.plan = plan_of(cfg)

    def on_message(self, message: int, text: str, posted: float) -> None:
        s = parse_signal(message, text, posted)
        if s is not None:
            net, stopped = self.store.day(s.day)
            if stopped:
                self.store.add_signal(s, f"Día apagado ({'meta +' + money(self.plan.target) if stopped == 'meta' else 'freno de seguridad'}): "
                                         "no se juega")
                self.store.set_state(s.message, f"Día apagado: {s.pair} no se juega", result="-")
            else:
                if self.plan.players[0] == 1:  # J1 = entrada directa, J2 = 1ª martingala (user, 23 Oct 2026)
                    self.store.add_signal(s, f"Esperando: J1 (directa) a las {mexico(s.entry_at)} · J2 (1ª martingala) a las "
                                             f"{mexico(s.gale1_at)} MX")
                else:
                    self.store.add_signal(s, f"Esperando 1ª vela a las {mexico(s.entry_at)} MX (sin apostar)")
                if self.demo is not None:
                    self.demo.schedule(s)
            if self.plan.players[0] == 1:
                self.store.event("SEÑAL", f"{s.pair} OTC {s.direction} · J1 (directa) {mexico(s.entry_at)} · J2 (1ª martingala) "
                                          f"{mexico(s.gale1_at)} (hora MX)")
            else:
                self.store.event("SEÑAL", f"{s.pair} OTC {s.direction} · 1ª vela {mexico(s.entry_at)} · J2 {mexico(s.gale1_at)} · "
                                          f"J3 {mexico(s.gale2_at)} (hora MX)")
            return
        result = parse_result(text)
        if result is None:
            return
        if self.demo is not None:  # the broker's own results book the players; the group's post is only compared
            self.store.event("TYL PUBLICÓ", {"D": "victoria directa", "G1": "victoria en 1ª martingala",
                                              "G2": "victoria en 2ª martingala", "L": "pérdida"}[result])
            return
        row = None
        for r in reversed(self.store.open_signals()):
            if r[3] <= posted + 60 and posted - r[3] <= RESULT_WINDOW:
                row = r
                break
        if row is None:
            self.store.event("RESULTADO SIN SEÑAL", f"Mensaje {message}: {result}")
            return
        message_id, pair, direction, entry_at, g1, g2, day, _ = row
        book = self.store.book()
        net, stopped = self.store.day(day)
        if stopped:
            self.store.set_state(message_id, f"Día apagado: resultado {result} no se juega", result=result)
            return
        ops, net, stop = settle_signal(result, book, net, self.plan)
        for player, stake, w, value in ops:
            self.store.add_op(message_id, pair, direction, player, stake, w, value, day, "papel")
        if ops:
            self.store.save_book(book, self.plan.levels)
        self.store.save_day(day, net, stop)
        words = {"D": "1ª vela ganó: señal descartada (sin apostar)"}
        if result != "D":
            words[result] = " · ".join(f"J{p} {'ganó' if w else 'perdió'} {money(v) if v < 0 else '+' + money(v)}"
                                       for p, _, w, v in ops) or "sin jugadas"
        detail = words[result] + (f" · Día apagado en seco: +{money(self.plan.target)}" if stop == "meta" else
                                  " · Freno de seguridad del día" if stop == "freno" else "")
        self.store.set_state(message_id, detail, result=result)
        self.store.event("RESULTADO", f"{pair} OTC: {detail} · día {money(net)}")

    def tick(self, now: float) -> None:
        self.store.put("heartbeat", now)
        if self.demo is not None:
            return  # the runner keeps the states and the players' cards
        grace = float(self.cfg.get("grace_seconds", 20))
        step = self.store.book().step
        status = {2: "Libre", 3: "Libre"}
        for message_id, pair, direction, entry_at, g1, g2, day, state in self.store.open_signals():
            net, stopped = self.store.day(day)
            j2, j3 = money(self.plan.stake(self.plan.players[0], step)), money(self.plan.stake(self.plan.players[-1], step))
            if now - g2 > NO_RESULT:
                self.store.set_state(message_id, "Sin resultado del canal: cerrada sin jugadas", result="?")
                continue
            if stopped:
                new = "Día apagado: no se juega"
            elif now < entry_at:
                new = f"Esperando 1ª vela a las {mexico(entry_at)} MX (sin apostar)"
            elif now < g1 + grace:
                new = "Vigilando 1ª vela sin apostar"
                status[2] = status[3] = "Vigilando 1ª vela"
            elif now < g2 + grace:
                new = f"1ª vela perdió (sin aviso de victoria): J2 operando ({j2})"
                status[2], status[3] = "Operando", "Vigilando 2ª vela"
            else:
                new = f"J2 perdió: J3 operando ({j3})"
                status[3] = "Operando"
            if new != state:
                self.store.set_state(message_id, new)
        self.store.put("jugador2", status[2])
        self.store.put("jugador3", status[3])


class DemoRunner:
    """Plays each signal on the Pocket Option DEMO account at the exact seconds; each order counts on the day with the
    broker's result, and the signal's whole result (J2 + J3) goes on the shared book once both are settled."""

    def __init__(self, store: Store, plan, conn, clock=Clock):
        self.store, self.plan, self.conn, self.clock = store, plan, conn, clock
        self.tasks: set[asyncio.Task] = set()
        self.assets: set[str] = set()

    def players(self, j2=None, j3=None):
        """The two players' cards (jugador2/jugador3, or jugador1/jugador2 when J1 bets the 1st candle)."""
        first, second = self.plan.players[0], self.plan.players[-1]
        if j2:
            self.store.put(f"jugador{first}", j2)
        if j3:
            self.store.put(f"jugador{second}", j3)

    def schedule(self, s) -> None:
        task = asyncio.get_running_loop().create_task(self.play(s))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda t, s=s: self.failed(t, s))

    def failed(self, task, s) -> None:
        """A signal's play that stops on an error is reported and closed, never left waiting in silence (23 Oct 2026)."""
        if task.cancelled() or task.exception() is None:
            return
        exc = task.exception()
        self.store.event("ATENCIÓN", f"{s.pair}: la jugada se detuvo por un error ({type(exc).__name__}: {exc}); revisa la demo.")
        try:
            self.store.set_state(s.message, f"No se jugó: error del motor ({type(exc).__name__})", result="-")
        except Exception:
            pass
        self.players("Libre", "Libre")

    async def keep(self, stop) -> None:
        """Keep the DEMO socket up; the card shows Demo conectado / conectando. A drop is retried after 1 s first
        (21 Oct 2026: Pocket closed the socket while the same session was open in a browser)."""
        delay = 1.0
        while not stop():
            self.store.put("mode", "demo_conectando")
            task = asyncio.get_running_loop().create_task(self.conn.run())
            shown = False
            try:
                while not task.done():
                    if self.conn.ready.is_set():
                        if not shown:
                            shown, delay = True, 1.0
                            self.store.put("mode", "demo_conectado")
                            for asset in ["EURUSD_otc"] + list(self.assets):  # EURUSD_otc keeps the latency measured
                                await self.conn.subscribe(asset)
                        self.store.put("saldo_demo", self.conn.balance)
                        self.store.put("latencia", round(-self.conn.latency(), 3))
                    if stop():
                        task.cancel()
                    await asyncio.sleep(1)
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.store.event(getattr(self.conn, "label", "POCKET DEMO"), f"Conexión perdida: {type(exc).__name__}: {exc}. Reintentando.")
            self.store.put("mode", "demo_conectando")
            if not stop():
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)  # 21 Oct 2026: a 60 s wait after a Wi-Fi drop cost the session's 1st signal

    async def play(self, s) -> None:
        asset = asset_code(s.pair)
        self.assets.add(asset)
        st = self.store
        while not self.conn.ready.is_set() and time.time() < s.entry_at - 1:
            await asyncio.sleep(0.5)
        if not self.conn.ready.is_set():
            st.set_state(s.message, "Pocket Option DEMO desconectado: no se juega", result="-")
            return
        await self.conn.subscribe(asset)
        for _ in range(20):  # its live price before the 1st candle (and a fresh latency for the clock)
            if self.conn.price(asset) is not None:
                break
            await asyncio.sleep(0.5)
        if self.conn.price(asset) is None and hasattr(self.conn, "diagnose"):
            st.event("ATENCIÓN", f"{asset}: sin precio tras suscribirse · {self.conn.diagnose(asset)}")
        level = {"step": None}

        def stake_of(player):
            # J2 and J3 of one signal play at the same level: the book's when J2 goes out (another signal may end between).
            if level["step"] is None:
                level["step"] = st.book().step
            return self.plan.stake(player, level["step"])

        step = st.book().step
        a, b = self.plan.players[0], self.plan.players[-1]
        if a == 1:  # 23 Oct 2026, AUD/CHF 18:50: this line asked for J3's stake, which J1/J2 do not have, and killed the play
            plan_text = (f"J1 {mexico(s.entry_at)} ({money(self.plan.stake(1, step))}) · "
                         f"J2 {mexico(s.gale1_at)} ({money(self.plan.stake(2, step))})")
        else:
            plan_text = (f"1ª vela {mexico(s.entry_at)} · J2 {mexico(s.gale1_at)} ({money(self.plan.stake(a, step))}) · "
                         f"J3 {mexico(s.gale2_at)} ({money(self.plan.stake(b, step))})")
        st.set_state(s.message, f"Pre-cargada {asset}: {plan_text}" + (" · en recuperación" if step else ""))
        open_stakes: dict[str, float] = {}
        results: list[tuple[int, float, float]] = []
        settles: list[asyncio.Task] = []
        last = {"what": None}

        def allowed(player, stake):
            net, stopped = st.day(s.day)
            if stopped:
                return "el día ya está apagado"
            if self.plan.loss_stop and net - sum(open_stakes.values()) <= self.plan.loss_stop:
                return "freno de seguridad del día"
            return None

        async def send(player, asset_, direction, stake, seconds):
            opened = await self.conn.open_order(asset_, direction, stake, seconds)
            open_stakes[str(opened["id"])] = stake
            settles.append(asyncio.get_running_loop().create_task(self.settle(s, player, stake, opened, open_stakes, results)))
            return opened

        def log(step: Step):
            last["what"] = step.what
            st.event("DISPARO" if step.what == "disparo" else "VELA", f"{s.pair} · {step.detail} · {mexico(step.at)}")
            if step.what == "apertura":
                st.set_state(s.message, "Vigilando 1ª vela sin apostar")
                self.players("Vigilando 1ª vela", "Vigilando 1ª vela")
            elif step.what == "disparo":
                player = int(step.detail.split()[0][1:]) if step.detail.startswith("J") else 3
                if player == 1:
                    st.set_state(s.message, f"J1 disparado en la 1ª vela · {step.detail}")
                    self.players("Operando", "Vigilando 1ª vela")
                elif player == self.plan.players[0]:
                    st.set_state(s.message, f"1ª vela perdió: J2 disparado · {step.detail}")
                    self.players("Operando", "Vigilando 2ª vela")
                else:
                    st.set_state(s.message, f"J{player - 1} perdió: J{player} disparado · {step.detail}")
                    self.players(j3="Operando")
            else:
                st.set_state(s.message, step.detail)

        clock = self.clock(self.conn.clock_offset())
        try:
            sent = await run_signal(s, self.conn.price, send, stake_of, self.plan, clock, log, allowed)
            if settles:
                await asyncio.gather(*settles, return_exceptions=True)
        finally:
            self.players("Libre", "Libre")
            self.assets.discard(asset)  # finished: not re-subscribed on the next reconnect
        if results:
            self.book_signal(s, round(sum(n for _, _, n in results), 2))
        nets = {p: n for p, _, n in results}
        if 1 in self.plan.players and sent:
            code = ("D" if nets.get(1, 0) > 0 else "G1" if nets.get(2, 0) > 0 else
                    "-" if all(n == 0 for n in nets.values()) else "L")  # a refund (same price) is neither
        else:
            code = (("D" if last["what"] == "ganó" else "-") if not sent else
                    "G1" if nets.get(2, 0) > 0 else "G2" if nets.get(3, 0) > 0 else "L")
        summary = " · ".join(f"J{p} {money(n) if n < 0 else '+' + money(n)}" for p, _, n in results)
        st.set_state(s.message, ("1ª vela ganó: señal descartada (sin apostar)" if code == "D" and not results else summary or
                                 st.db.execute("SELECT state FROM signals WHERE message=?", (s.message,)).fetchone()[0]),
                     result=code)

    async def settle(self, s, player, stake, opened, open_stakes, results) -> None:
        st = self.store
        how = "Pocket"
        try:
            deal = await self.conn.result(opened["id"], 300 + 90)
            net = deal_net(deal)
        except Exception as exc:
            price = self.conn.price(asset_code(s.pair))
            if price is None:
                st.event("ATENCIÓN", f"J{player} {s.pair}: no llegó el resultado de Pocket ({exc}) ni hay precio; "
                                     "revisa la demo. La libreta no se movió.")
                open_stakes.pop(str(opened["id"]), None)
                return
            net = round(stake * self.plan.payout, 2) if won(s.direction, float(opened.get("openPrice", 0)), price) else -stake
            how = "estimado con el precio"
        day_net, stopped = st.day(s.day)
        day_net = round(day_net + net, 2)
        stop = stopped or day_stopped(day_net, self.plan)
        st.save_day(s.day, day_net, stop)
        st.add_op(s.message, s.pair, s.direction, player, stake, net > 0, net, s.day, "demo")
        open_stakes.pop(str(opened["id"]), None)
        results.append((player, stake, net))
        st.event("RESULTADO DEMO", f"{s.pair} OTC · J{player} {money(stake)} → {money(net) if net < 0 else '+' + money(net)} "
                                   f"({how}) · día {money(day_net)}" +
                 (f" · Día apagado en seco: +{money(self.plan.target)}" if stop == "meta" and not stopped else
                  " · Freno de seguridad del día" if stop == "freno" and not stopped else ""))

    def book_signal(self, s, net: float) -> None:
        """The signal is over: its whole result on the shared book (a loss of both J2 and J3 → one recovery level; the
        debt paid → back to the normal level)."""
        st, plan = self.store, self.plan
        book = st.book()
        before = book.step
        settle_net(book, net, plan.levels)
        st.save_book(book, plan.levels)
        a, b = plan.players[0], plan.players[-1]
        pair = lambda step: f"J{a} {money(plan.stake(a, step))} → J{b} {money(plan.stake(b, step))}"
        if book.step > before:
            text = f"se anota la deuda: {money(book.debt)} · las siguientes señales en recuperación: {pair(book.step)}"
        elif book.step < before:
            text = f"deuda limpia: vuelve al nivel normal {pair(0)}"
        elif book.debt > 0:
            text = f"quedan {money(book.debt)} por recuperar · sigue {pair(book.step)}"
        else:
            text = f"sin deuda · nivel normal {pair(0)}"
        st.event("LIBRETA", f"{s.pair} OTC: la señal cerró {money(net) if net < 0 else '+' + money(net)} · {text}")


def telegram_login():
    """SignalBridge's Telegram login, copied READ-ONLY into memory (its files are never written)."""
    from telethon import TelegramClient
    from telethon.crypto import AuthKey
    from telethon.sessions import MemorySession
    sb = signalbridge_dir()
    cfg = json.loads((sb / "config.json").read_text(encoding="utf-8"))
    db = sqlite3.connect("file:" + (sb / "telegram.session").as_posix() + "?mode=ro", uri=True, timeout=5)
    try:
        dc, server, port, key = db.execute("SELECT dc_id,server_address,port,auth_key FROM sessions").fetchone()
    finally:
        db.close()
    session = MemorySession()
    session.set_dc(dc, server, port)
    session.auth_key = AuthKey(key)
    return TelegramClient(session, cfg["api_id"], cfg["api_hash"], receive_updates=False)


def stop_requested(store: Store) -> bool:
    """The dashboard asks to stop with a flag file (it never writes the database)."""
    return store.get("stop_requested") == "1" or (data_dir() / "stop.flag").exists()


async def run(store: Store, cfg: dict, demo: DemoRunner | None = None, rapido=None, anna=None) -> None:
    engine = PocketEngine(store, cfg, demo)
    keeper = asyncio.get_running_loop().create_task(demo.keep(lambda: stop_requested(store))) if demo else None
    # The own 15-s strategy (23 Oct 2026) shares the DEMO connection; Tyl's signals, book and marker are not touched.
    fast = asyncio.get_running_loop().create_task(rapido.run(lambda: stop_requested(store))) if rapido else None
    poll = float(cfg.get("poll_seconds", 5))
    delay = 5.0
    mode = "demo" if demo else "papel"
    while not stop_requested(store):
        client = None
        try:
            client = telegram_login()
            await client.connect()
            entity = None
            anna_entities = {}  # the ANNA test channels (26 Oct 2026): title → entity
            wanted_anna = set(anna.channels) if anna is not None and anna.enabled else set()
            async for d in client.iter_dialogs():
                # ONLY Tyl trader - Grupo VIP 📊 (never "Operaciones gratuitas" or any other chat), and the ANNA test channels.
                name = (d.name or "").strip()
                if entity is None and (name == cfg["channel_title"] or (name.startswith(cfg["channel"]) and "VIP" in name)):
                    entity = d.entity
                elif name in wanted_anna and name not in anna_entities:
                    anna_entities[name] = d.entity
                if entity is not None and len(anna_entities) == len(wanted_anna):
                    break
            if entity is None:
                raise RuntimeError(f"No encontré el canal «{cfg['channel']}» en tu Telegram.")
            anna_last = {}
            for name in sorted(wanted_anna):
                if name not in anna_entities:
                    store.event("ATENCIÓN", f"No encontré el canal «{name}» en tu Telegram: la prueba de Anna no lo lee.")
                    continue
                key = f"anna_last:{name}"
                last_id = int(store.get(key, 0) or 0)
                if not last_id and name == "ANNA VIP POCKET":  # the first version kept it under anna_last_id
                    last_id = int(store.get("anna_last_id", 0) or 0)
                if not last_id:  # first start: from now on
                    newest = await client.get_messages(anna_entities[name], limit=1)
                    last_id = newest[0].id if newest else 0
                store.put(key, last_id)
                anna_last[name] = last_id
            if anna_entities:
                g, k = int(anna.cfg["gales"]), float(anna.cfg["multiplier"])
                how = (" → ".join(f"${float(anna.cfg['stake']) * k ** n:.2f}" for n in range(g + 1)) + f" (martingala del canal, "
                       f"hasta {g} gales)" if g else f"una sola entrada de ${float(anna.cfg['stake']):.2f}, sin martingala")
                store.event("ANNA", "Prueba en DEMO: " + how + " por señal de " + " y ".join(f"«{n}»" for n in sorted(anna_entities)) +
                            f", juzgada en la misma vela que el canal · lee cada {float(anna.cfg['poll_seconds']):g} s.")
            last = int(store.get("last_id", 0) or 0)
            if not last:  # first start: from now on (no replay of old signals)
                newest = await client.get_messages(entity, limit=1)
                last = newest[0].id if newest else 0
                store.put("last_id", last)
            store.put("telegram", "conectado")
            store.put("canal", getattr(entity, "title", cfg["channel"]))
            store.event("TELEGRAM", f"Leyendo «{getattr(entity, 'title', cfg['channel'])}» en modo {mode}.")
            delay = 5.0
            # the VIP M1 channel posts from ~25 s before to ~15 s after its candle opens: read every 2 s; Tyl stays at 5 s
            anna_poll = min(poll, float(anna.cfg["poll_seconds"])) if anna_entities else poll
            next_tyl = 0.0
            while not stop_requested(store):
                if time.monotonic() >= next_tyl:
                    got = await client.get_messages(entity, min_id=last, limit=100)
                    for m in sorted(got, key=lambda x: x.id):
                        engine.on_message(m.id, m.raw_text or "", m.date.timestamp())
                        last = max(last, m.id)
                        store.put("last_id", last)
                    engine.tick(time.time())
                    next_tyl = time.monotonic() + poll
                for name, ent in anna_entities.items():
                    got = await client.get_messages(ent, min_id=anna_last[name], limit=100)
                    for m in sorted(got, key=lambda x: x.id):
                        try:
                            anna.on_message(name, m.id, m.raw_text or "", m.date.timestamp())
                        except Exception as exc:  # the test never stops Tyl
                            store.event("ATENCIÓN", f"Anna: mensaje {m.id} de «{name}» no procesado ({type(exc).__name__}: {exc}).")
                        anna_last[name] = max(anna_last[name], m.id)
                        store.put(f"anna_last:{name}", anna_last[name])
                await asyncio.sleep(anna_poll)
        except Exception as exc:  # network or Telegram trouble: keep the state, try again
            store.put("telegram", f"error: {exc}")
            store.event("ERROR", f"{type(exc).__name__}: {exc}")
            engine.tick(time.time())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
    if demo is not None:
        for task in list(demo.tasks):
            task.cancel()
    if keeper is not None:
        keeper.cancel()
    if fast is not None:
        fast.cancel()
    store.put("telegram", "detenido")
    store.put("mode", "detenido")
    store.event("DETENIDO", "Motor Pocket detenido por el usuario.")


def main() -> None:
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = open(root / "engine.lock", "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("El motor Pocket ya está corriendo.")
        sys.exit(0)
    try:
        (root / "stop.flag").unlink()
    except FileNotFoundError:
        pass
    quick_edit_off()
    cfg = load_config(root)
    store = Store(root / "pocket_tyl.sqlite")
    store.put("pid", os.getpid())
    store.put("started", time.time())
    store.put("stop_requested", 0)
    demo = None
    if has_ssid(root):
        try:
            ssid = protect((root / "ssid.bin").read_bytes(), decrypt=True).decode("utf-8")
            if demo_check(ssid) is None:
                from .conector import PocketDemo
                demo = DemoRunner(store, plan_of(cfg), PocketDemo(ssid, log=store.event))
        except Exception as exc:
            store.event("ATENCIÓN", f"No se pudo usar el SSID demo ({type(exc).__name__}); sigue en papel.")
    store.put("mode", "demo_conectando" if demo else "papel")
    plan = plan_of(cfg)
    a, b = plan.players[0], plan.players[-1]
    store.event("ARRANQUE", f"Motor Pocket · martingala corta · normal J{a} {money(plan.stake(a, 0))} → J{b} {money(plan.stake(b, 0))} · "
                            f"recuperación J{a} {money(plan.stake(a, 1))} → J{b} {money(plan.stake(b, 1))} (tope) · "
                            + ("apuesta desde la 1ª vela · " if a == 1 else "") + "meta "
                            f"+{money(plan.target)}/día · freno {money(plan.loss_stop)} · " +
                            ("DEMO de Pocket Option" if demo else f"papel (pago {plan.payout:.0%})"))
    rapido = None
    if demo is not None and (cfg.get("rapido") or {}).get("enabled"):
        from .rapido import Rapido
        rapido = Rapido(store, cfg["rapido"], demo.conn, root, busy=lambda: bool(demo.tasks))
    anna = None
    if demo is not None and (cfg.get("anna") or {}).get("enabled"):
        from .anna import AnnaTest
        anna = AnnaTest(store, cfg["anna"], demo.conn)
    try:
        asyncio.run(run(store, cfg, demo, rapido, anna))
    finally:
        store.put("pid", "")
        store.close()
