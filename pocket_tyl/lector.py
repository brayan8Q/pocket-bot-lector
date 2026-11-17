"""Bridge from the «Lector de tendencia» panel to the Pocket Option DEMO (user, 15 Nov 2026: «ya teníamos un bot que
apostaba en Pocket, ¿cómo los fusionamos?» … «dale»).

The Lector (Electron panel in Escritorio\\senales-overlay) decides; this process only places what it is told, on the DEMO,
and answers with Pocket's own numbers, so the panel no longer has to guess the entry from the screen:

  POST /orden          {"asset": "EUR/USD OTC", "dir": "up"|"down", "seconds": 3, "level": 0}
                       → {"ok": true, "id", "open_price", "payout", "amount", "sent_ms", "open_ms"} | {"ok": false, "error"}
  GET  /resultado?id=  waits for Pocket's result → {"ok": true, "result": "win"|"loss"|"tie", "net", "close_price"}
  GET  /estado         {"ok": true, "connected", "demo": true, "balance", "stake", "factor", "levels", "open"}

Only 127.0.0.1, and every request must carry the token written in lector.json next to the database (a web page cannot
place orders). DEMO only: it refuses to start with POCKET_TYL_REAL and the connector blocks any session that is not a
demo one. One order at a time. Amount by level: stake × factor^level (1.00 → 2.20), never above max_stake nor the
balance; the level (0 = the signal, 1 = its martingale) comes from the panel, which only sends a martingale after Pocket
itself said the previous order lost. Run: python -m pocket_tyl.lector  (launcher «Iniciar Pocket Lector.bat»)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import parse_qs, urlsplit

from .conector import deal_net

# 15 Nov 2026 16:40 (user: «el martingala es siempre, no en una sola»): up to four martingales in a row,
# 1.00 → 2.20 → 4.84 → 10.65 → 23.43 (a whole lost run = −42.12, the «tope 50» he chose for the GPT bot on 4 Nov).
DEFAULTS = dict(stake=1.0, factor=2.2, levels=4, max_stake=25.0, port=8765)
# 15 Nov 2026, first live session: Pocket opens and closes quick trades on WHOLE seconds of its clock (openTimestamp /
# closeTimestamp), so a "3 s" order lasts 3 s minus the fraction of the second it arrived in. An order that arrived at
# the very end of a second was refused with IncorrectExpTime (16:00:04), and one got no answer at all (16:00:35).
LATE = 0.80  # an order that would arrive after this fraction of the second waits for the next second (at most 0.25 s)
RETRY_WAIT = 0.3  # IncorrectExpTime: one more try this much later
ORDER_TIMEOUT = 4.0  # the panel waits 5 s for the entry: no answer from Pocket by then = the order did not go in
SCHEMA = """CREATE TABLE IF NOT EXISTS lector_ops (id TEXT PRIMARY KEY, created REAL, asset TEXT, direction TEXT,
  seconds INTEGER, level INTEGER, amount REAL, payout REAL, open_price REAL, close_price REAL, net REAL, result TEXT)"""


def asset_key(name: str) -> str:
    """"EUR/USD OTC" and "EURUSD_otc" both give "eurusdotc"."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def guess_code(name: str) -> str:
    """Pocket's code for a pair written the way the panel shows it: "EUR/USD OTC" → "EURUSD_otc", "EUR/USD" → "EURUSD"."""
    text = str(name or "").upper()
    otc = "OTC" in text
    base = re.sub(r"[^A-Z0-9#]", "", text.replace("OTC", ""))
    return base + ("_otc" if otc else "")


def amount_for(cfg: dict, level: int) -> float:
    return round(float(cfg["stake"]) * float(cfg["factor"]) ** int(level), 2)


PRICE_FRESH = 6.0  # seconds a streamed price counts as «now» for /precios


async def watch_pairs(conn, stop, log, every: float = 4.0, gap: float = 0.12, kinds=None) -> None:
    """Keeps a live price of every open currency pair (each changeSymbol adds a stream and the earlier ones keep
    flowing), so /precios can tell which pair has the price the panel reads on the screen. After a reconnection
    the streams are asked again. It only listens: no order comes from here.
    16 Nov 2026 ~18:20 (user: «puede ser hasta cambiar a otra divisa»): `kinds` None = every open asset of any type
    (crypto, metals, stocks, indices too), to look for an asset where a rule holds AND the payout is enough."""
    told = 0
    while not stop():
        ws = getattr(conn, "ws", None)
        if conn.ready.is_set() and ws is not None and conn.assets:
            for symbol, row in list(conn.assets.items()):
                if symbol in conn.streams or len(row) < 4 or (kinds is not None and row[3] not in kinds) or (len(row) > 14 and not row[14]):
                    continue
                try:
                    await ws.send('42["changeSymbol",' + json.dumps(dict(asset=symbol, period=60), separators=(",", ":")) + "]")
                except Exception:
                    break
                conn.streams.add(symbol)
                await asyncio.sleep(gap)
            if len(conn.streams) != told:
                told = len(conn.streams)
                log("VIGÍA", f"siguiendo el precio de {told} pares para reconocer el de la gráfica")
        await asyncio.sleep(every)


class LectorBridge:
    def __init__(self, store, cfg: dict | None, conn, token: str | None = None, clock=time.time):
        self.store, self.conn, self.clock = store, conn, clock
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.token = token or secrets.token_hex(16)
        self.open_id: str | None = None  # the order being played (one at a time)
        self.results: dict[str, asyncio.Future] = {}
        self.ops: dict[str, dict] = {}
        # 16 Nov 2026 («quita la IA y deja eso del seguimiento»): only the follower (ritmo.py) trades; the panel's
        # own orders (its signal and martingales) are refused.
        self.only_ritmo = False  # the launcher switches it on when the follower runs (see main)
        store.db.execute(SCHEMA)

    # ---------- what the panel asks ----------
    def status(self) -> dict:
        ready = self.conn.ready.is_set()
        return dict(ok=True, connected=ready, demo=not getattr(self.conn, "real", False), balance=self.conn.balance if ready else None,
                    panel_orders=not self.only_ritmo,
                    stake=float(self.cfg["stake"]), factor=float(self.cfg["factor"]), levels=int(self.cfg["levels"]),
                    open=self.open_id)

    def prices(self) -> dict:
        """Latest price of every pair the bot is streaming: [symbol, name, price, age in s, payout]. The panel compares
        them with the price on the screen to know which pair the chart shows (16 Nov 2026: the user changed pairs on
        the chart and the panel stayed tied to the one written in «Activo», so no order was sent for two hours)."""
        now = time.time()
        rows = []
        for symbol, quote in list(getattr(self.conn, "prices", {}).items()):
            age = now - quote[2]
            if age > PRICE_FRESH:
                continue
            row = self.conn.assets.get(symbol) or ()
            rows.append([symbol, str(row[2]) if len(row) > 2 else symbol, quote[1], round(age, 2), row[5] if len(row) > 5 else None,
                         str(row[3]) if len(row) > 3 else None])
        return dict(ok=True, prices=rows)

    def candles_of(self, names: str) -> dict:
        """15-second candles of the pairs asked for (names or symbols, comma separated, at most 8): for each one
        [symbol, name, payout, closed candles [o, h, l, c] oldest first, forming candle, age in s of its last price,
        seconds the forming candle has been open]. 16 Nov 2026: the panel scans several pairs at once with them."""
        book = getattr(self, "candles", None)
        if book is None:
            return dict(ok=False, error="El bot no está guardando velas.")
        now, out = time.time(), []
        for name in [n.strip() for n in str(names or "").split(",") if n.strip()][:8]:
            symbol = name if name in self.conn.assets else self.asset_of(name)
            if not symbol:
                continue
            if getattr(self, "candles_root", None) is not None:
                book.fill_from_disk(symbol, self.candles_root)
            got = book.get(symbol)
            quote = getattr(self.conn, "prices", {}).get(symbol)
            if got is None or quote is None:
                continue
            closed, forming, opened, _seen = got
            row = self.conn.assets.get(symbol) or ()
            out.append([symbol, str(row[2]) if len(row) > 2 else symbol, row[5] if len(row) > 5 else None, closed, forming,
                        round(now - quote[2], 2), round(quote[0] - opened, 2)])
        return dict(ok=True, seconds=book.seconds, pairs=out)

    def asset_of(self, name: str) -> str | None:
        want = asset_key(name)
        if not want:
            return None
        for symbol, row in self.conn.assets.items():
            if asset_key(symbol) == want or (len(row) > 2 and asset_key(row[2]) == want):
                return symbol
        return None if self.conn.assets else guess_code(name)  # the list has not arrived yet: the usual spelling

    async def order(self, payload: dict) -> dict:
        direction = {"up": "ARRIBA", "down": "ABAJO"}.get(str(payload.get("dir")))
        try:
            seconds, level = int(payload.get("seconds", 0)), int(payload.get("level", 0))
        except (TypeError, ValueError):
            return dict(ok=False, error="Orden mal escrita (segundos o nivel).")
        if self.only_ritmo and payload.get("tag") != "ritmo":
            return dict(ok=False, error="Solo opera el seguimiento de la vela (ritmo): el panel no manda órdenes.")
        if direction is None or not 1 <= seconds <= 3600:
            return dict(ok=False, error="Orden mal escrita (lado o segundos).")
        if not 0 <= level <= int(self.cfg["levels"]):
            return dict(ok=False, error=f"Nivel {level} fuera del tope de martingalas ({self.cfg['levels']}).")
        if not self.conn.ready.is_set():
            return dict(ok=False, error="La DEMO de Pocket Option no está conectada.")
        if self.open_id is not None:
            return dict(ok=False, error="Ya hay una operación abierta: una a la vez.")
        asset = self.asset_of(str(payload.get("asset", "")))
        if asset is None:
            return dict(ok=False, error=f"Pocket no tiene el par «{payload.get('asset', '')}» (revisa «Activo» en el panel).")
        amount = amount_for(self.cfg, level)
        if amount > float(self.cfg["max_stake"]):
            return dict(ok=False, error=f"El monto ${amount:.2f} pasa del tope de ${float(self.cfg['max_stake']):g}.")
        if self.conn.balance is not None and amount > self.conn.balance:
            return dict(ok=False, error=f"El saldo de la DEMO (${self.conn.balance:,.2f}) no alcanza para ${amount:.2f}.")
        self.open_id = "enviando"
        sent = self.clock()
        try:
            await self.conn.subscribe(asset)
            await self.clear_of_the_second_end()
            for attempt in (1, 2):
                try:
                    opened = await asyncio.wait_for(self.conn.open_order(asset, direction, amount, seconds), ORDER_TIMEOUT)
                    break
                except RuntimeError as exc:
                    if attempt == 2 or "IncorrectExpTime" not in str(exc):
                        raise
                    self.store.event("LECTOR", f"{asset}: Pocket no aceptó ese segundo (IncorrectExpTime); se reintenta en {RETRY_WAIT:g} s.")
                    await asyncio.sleep(RETRY_WAIT)
        except Exception as exc:  # refused by Pocket, timeout, dropped socket
            self.open_id = None
            why = f"Pocket no contestó la orden en {ORDER_TIMEOUT:g} s" if isinstance(exc, asyncio.TimeoutError) else (str(exc) or type(exc).__name__)
            self.store.event("LECTOR RECHAZO", f"{asset} {direction} ${amount:.2f} {seconds} s · {why}")
            return dict(ok=False, error=why)
        done = self.clock()
        oid = str(opened["id"])
        op = dict(id=oid, created=done, asset=asset, direction=direction, seconds=seconds, level=level, amount=amount,
                  payout=float(opened.get("percentProfit", 0) or 0), open_price=float(opened.get("openPrice", 0) or 0))
        self.ops[oid] = op
        self.open_id = oid
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO lector_ops(id,created,asset,direction,seconds,level,amount,payout,open_price) "
                                  "VALUES(?,?,?,?,?,?,?,?,?)", (oid, done, asset, direction, seconds, level, amount, op["payout"], op["open_price"]))
        label = "señal" if level == 0 else f"martingala {level}"
        if payload.get("tag") == "ritmo":  # an entry of the continuous follow-up (ritmo.py), fixed stake
            label = "ritmo"
        self.store.event("LECTOR DISPARO", f"{asset} {direction} · {label} · ${amount:.2f} · {seconds} s · entró en {op['open_price']} "
                                           f"· paga {op['payout']:g} % · tardó {(done - sent) * 1000:.0f} ms")
        self.results[oid] = asyncio.get_running_loop().create_task(self.settle(op))
        answer = dict(ok=True, id=oid, asset=asset, amount=amount, open_price=op["open_price"], payout=op["payout"],
                      sent_ms=int(sent * 1000), open_ms=int(done * 1000))
        # When Pocket will close it, on this PC's clock (its timestamps run whole hours ahead): the panel counts to it.
        hours = getattr(self.conn, "hours", None)
        if hours is not None and opened.get("closeTimestamp"):
            answer["close_ms"] = int((float(opened["closeTimestamp"]) - hours) * 1000)
        return answer

    async def clear_of_the_second_end(self) -> None:
        """Pocket opens on whole seconds: an order that would land in the last fifth of a second waits for the next one."""
        offset = getattr(self.conn, "clock_offset", None)
        if offset is None:
            return
        fraction = (self.clock() + offset()) % 1.0
        if fraction > LATE:
            await asyncio.sleep(1.0 - fraction + 0.03)

    async def settle(self, op: dict) -> dict:
        """Wait for Pocket's own result of an order and book it."""
        try:
            deal = await self.conn.result(op["id"], op["seconds"] + 20)
        except Exception as exc:
            answer = dict(ok=False, error=f"Pocket no mandó el resultado ({exc or type(exc).__name__}).")
            self.store.event("ATENCIÓN", f"Lector: {op['asset']} sin resultado de Pocket ({exc or type(exc).__name__}).")
        else:
            net = deal_net(deal)
            result = "win" if net > 0 else "loss" if net < 0 else "tie"
            close = float(deal.get("closePrice", 0) or 0)
            with self.store.db:
                self.store.db.execute("UPDATE lector_ops SET close_price=?, net=?, result=? WHERE id=?", (close, net, result, op["id"]))
            words = dict(win="ganó", loss="perdió", tie="empate")[result]
            self.store.event("LECTOR RESULTADO", f"{op['asset']} {op['direction']} · ${op['amount']:.2f} · {op['open_price']} → {close} · "
                                                 f"{words} ({net:+.2f}) · {self.totals()}")
            answer = dict(ok=True, id=op["id"], result=result, net=net, close_price=close, open_price=op["open_price"])
        if self.open_id == op["id"]:
            self.open_id = None
        return answer

    async def outcome(self, order_id: str) -> dict:
        task = self.results.get(str(order_id))
        if task is None:
            return dict(ok=False, error="No conozco esa operación.")
        return await asyncio.shield(task)

    def totals(self) -> str:
        rows = self.store.db.execute("SELECT result, COUNT(*), COALESCE(SUM(net), 0) FROM lector_ops WHERE result IS NOT NULL GROUP BY result").fetchall()
        count = {r[0]: r[1] for r in rows}
        net = sum(r[2] for r in rows)
        return f"lleva {count.get('win', 0)} G / {count.get('loss', 0)} P / {count.get('tie', 0)} E · neto {net:+.2f}"

    # ---------- the local door ----------
    async def route(self, method: str, target: str, headers: dict, body: bytes) -> tuple[int, dict]:
        if not hmac.compare_digest(headers.get("x-lector-token", ""), self.token):
            return 403, dict(ok=False, error="Falta la clave del panel.")
        url = urlsplit(target)
        if method == "GET" and url.path == "/estado":
            return 200, self.status()
        if method == "POST" and url.path == "/orden":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return 400, dict(ok=False, error="Orden mal escrita.")
            return 200, await self.order(payload if isinstance(payload, dict) else {})
        if method == "GET" and url.path == "/precios":
            return 200, self.prices()
        if method == "GET" and url.path == "/activos":
            # 17 Nov 2026: Pocket's own row of every asset (updateAssets), read-only: payout, type, open, expiries…
            return 200, dict(ok=True, assets=[list(row) for row in list(getattr(self.conn, "assets", {}).values())])
        if method == "GET" and url.path == "/velas":
            return 200, self.candles_of((parse_qs(url.query).get("pares") or [""])[0])
        if method == "GET" and url.path == "/resultado":
            return 200, await self.outcome((parse_qs(url.query).get("id") or [""])[0])
        return 404, dict(ok=False, error="No existe.")

    async def serve(self, reader, writer) -> None:
        try:
            head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode("latin-1").split("\r\n")
            method, target = head[0].split(" ")[:2]
            headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in head[1:] if ":" in line)}
            length = int(headers.get("content-length", 0) or 0)
            body = await asyncio.wait_for(reader.readexactly(length), 5) if 0 < length <= 4096 else b""
            status, payload = await self.route(method.upper(), target, headers, body)
        except Exception as exc:
            status, payload = 400, dict(ok=False, error=f"Petición inválida ({type(exc).__name__}).")
        data = json.dumps(payload).encode("utf-8")
        try:
            writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                         f"Connection: close\r\n\r\n".encode("ascii") + data)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def listen(self, folder, stop) -> None:
        """Open the door on 127.0.0.1 (the configured port, or any free one) and leave port + token in lector.json."""
        try:
            server = await asyncio.start_server(self.serve, "127.0.0.1", int(self.cfg["port"]))
        except OSError:
            server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        card = folder / "lector.json"
        card.write_text(json.dumps(dict(port=port, token=self.token, pid=os.getpid(), started=self.clock())), encoding="utf-8")
        self.store.event("LECTOR", f"Puente abierto en 127.0.0.1:{port} · ${float(self.cfg['stake']):.2f} la señal"
                                   + "".join(f" · martingala {n}: ${amount_for(self.cfg, n):.2f}" for n in range(1, int(self.cfg["levels"]) + 1)))
        try:
            async with server:
                while not stop():
                    await asyncio.sleep(0.5)
        finally:
            try:
                card.unlink()
            except OSError:
                pass


def main() -> None:
    from .engine import DemoRunner, quick_edit_off, stop_requested
    from .gpt import MX, take_lock
    from .secret import demo_check, has_ssid, protect
    from .store import Store, data_dir, load_config, plan_of
    if os.environ.get("POCKET_TYL_REAL") == "1":
        print("El puente del Lector solo opera la cuenta DEMO. No arranca.")
        sys.exit(1)
    root = data_dir()
    lock = take_lock(root)
    if lock is None:
        print("El motor Pocket, la prueba GPT o este puente ya está corriendo: ciérralo primero.")
        sys.exit(0)
    try:
        (root / "stop.flag").unlink()
    except FileNotFoundError:
        pass
    quick_edit_off()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    cfg = load_config(root)
    store = Store(root / "pocket_tyl.sqlite")
    log_path = os.environ.get("POCKET_LECTOR_LOG")  # the launcher keeps a copy of what the window shows (never the SSID)

    def say(line: str) -> None:
        print(line, flush=True)
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    stamp = lambda: dt.datetime.now(MX).strftime("%d-%m %H:%M:%S")
    if not has_ssid(root):
        say(f"{stamp()} No hay sesión guardada de la cuenta DEMO. Abre «Conectar Pocket Option.bat», entra a tu DEMO y vuelve a abrir este.")
        store.event("ATENCIÓN", "Lector: no hay sesión de la cuenta DEMO; no arranca.")
        input("Enter para cerrar…")
        return
    ssid = protect((root / "ssid.bin").read_bytes(), decrypt=True).decode("utf-8")
    if demo_check(ssid) is not None:
        say(f"{stamp()} La sesión guardada no es de la cuenta DEMO; no arranca.")
        store.event("ATENCIÓN", "Lector: la sesión guardada no es de la cuenta DEMO; no arranca.")
        input("Enter para cerrar…")
        return
    base_event = store.event

    def event(kind, detail):  # the window shows every event (connection, each order and its result)
        base_event(kind, detail)
        say(f"{stamp()} {kind} · {detail}")
    store.event = event
    store.put("pid", os.getpid())
    store.put("started", time.time())
    store.put("stop_requested", 0)
    from .conector import PocketDemo
    conn = PocketDemo(ssid, log=store.event)
    runner = DemoRunner(store, plan_of(cfg), conn)
    bridge = LectorBridge(store, cfg.get("lector"), conn)
    # Every deal closed on the DEMO account (the user's manual ones too) goes to a CSV next to the log copy:
    # time, side, amount, prices and result, so his own trading can be analysed (16 Nov 2026).
    closed_path = os.path.join(os.path.dirname(log_path), "pocket_cierres.csv") if log_path else None

    def record_closed(deal, ours):
        if not closed_path:
            return
        fields = ("openTimestamp", "closeTimestamp", "openTime", "closeTime", "asset", "command", "amount", "openPrice", "closePrice",
                  "profit", "percentProfit")
        new = not os.path.exists(closed_path)
        with open(closed_path, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write("visto,origen,id," + ",".join(fields) + "\n")
            row = [f"{time.time():.3f}", "bot" if ours else "mano", str(deal.get("id", ""))] + [str(deal.get(k, "")).replace(",", " ") for k in fields]
            fh.write(",".join(row) + "\n")
    conn.on_closed = record_closed
    print("=" * 72)
    print("  PUENTE DEL LECTOR · cuenta DEMO de Pocket Option (dinero de práctica)")
    print("  Abre las operaciones que marca el panel «Lector de tendencia».")
    print("  Para detenerlo: cierra esta ventana.")
    print("=" * 72)
    store.put("mode", "demo_conectando")
    store.event("ARRANQUE", "Puente del Lector en la DEMO de Pocket Option (Tyl, ANNA, GPT y la estrategia rápida apagadas).")

    async def go():
        stop = lambda: stop_requested(store)
        keeper = asyncio.get_running_loop().create_task(runner.keep(stop))
        # Ritmo de la vela (16 Nov 2026): continuous tick follow-up that places its own DEMO entries (ritmo.py).
        follower = None
        # 16 Nov 2026 ~10:20 (user: «pon el botón de predecir… el bot lo mete, pero ya no va a ser en automático»):
        # the follower is OFF unless config `lector.ritmo` is true; the bot takes the panel's orders again.
        if (cfg.get("lector") or {}).get("ritmo", False):
            from . import ritmo
            pair = (cfg.get("lector") or {}).get("ritmo_asset", "AUD/CAD OTC")  # 16 Nov 07:20: the pair the user is watching (CHF/NOK OTC was paying 58 %)
            store.event("RITMO", f"Seguimiento continuo de {pair} encendido: entradas de $1 fijo a 3 s, sin martingala (DEMO).")
            # Threshold in sigmas: 2 (Argon's figure) fired about 7 times an hour on 15 Nov's prices; 1 fires about 40 (paper: 50.9 % of 236).
            k = float((cfg.get("lector") or {}).get("ritmo_k", 1.0))
            mode = str((cfg.get("lector") or {}).get("ritmo_modo", "color3"))  # 'color3' = the three frozen rules (16 Nov 08:05) · 'color' · 'contra' = Argon's fade
            follower = asyncio.get_running_loop().create_task(ritmo.run(bridge, conn, pair, stop, store.event, k=k, mode=mode))
            bridge.only_ritmo = bool((cfg.get("lector") or {}).get("ritmo_solo", True))  # the panel's own orders are refused
        # 16 Nov 2026 ~13:00 («cuando diga PREDECIR el sistema apueste»): the price of every open pair is followed so
        # the panel can tell by itself which pair the chart shows (config `lector.vigia: false` turns it off).
        watcher = None
        if (cfg.get("lector") or {}).get("vigia", True):
            kinds = (cfg.get("lector") or {}).get("vigia_tipos")  # e.g. ["currency"]; missing = every type
            watcher = asyncio.get_running_loop().create_task(watch_pairs(conn, stop, store.event, kinds=set(kinds) if kinds else None))
            # 16 Nov 2026 ~18:30 («el escáner tiene que estar activo en los 4 al mismo tiempo»): 15-s candles of every
            # streamed pair, for the panel to analyse several charts at once (GET /velas).
            from .velas import CandleBook
            bridge.candles, bridge.candles_root = CandleBook(), root
            conn.listeners.append(bridge.candles.on_tick)
            # 16 Nov 2026 ~13:20: the prices of those pairs are also kept on disk (ticks_otc\<day>\<pair>.csv, as on
            # 4-5 Nov), to check any outside «bot» trade by trade against the real price and to have more days to test on.
            if (cfg.get("lector") or {}).get("grabar", True):
                from .grabador import TickRecorder
                recorder = TickRecorder(root, conn, store)
                recorder.only_otc = False  # 16 Nov ~14:00: the real-market pairs too (every search so far was on OTC only)
                conn.listeners.append(recorder.on_tick)
                store.event("VIGÍA", "grabando los precios de todos los pares (OTC y mercado real) en ticks_otc (solo lectura)")
        try:
            await bridge.listen(root, stop)
        finally:
            keeper.cancel()
            if watcher is not None:
                watcher.cancel()
            if follower is not None:
                follower.cancel()
            store.put("mode", "detenido")
            store.event("DETENIDO", "Puente del Lector detenido.")
    try:
        asyncio.run(go())
    finally:
        store.put("pid", "")
        store.close()


if __name__ == "__main__":
    main()
