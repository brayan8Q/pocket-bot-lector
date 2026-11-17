"""TRADING GPT BOT (@signals_chatgpt_bot) on the Pocket Option DEMO (user, 4 Nov 2026: "sí dale"): every `every_seconds`
the test presses the bot's «Nueva señal» button from the user's Telegram (only that button), reads the answer
("SEÑAL DE TRADING · Activo: CAD/JPY (OTC) · Duración: 1 min · Dirección: ALCISTA/BAJISTA · Confianza: 70%"), opens at
once one DEMO order of a fixed stake for the signal's duration (no martingale) and books Pocket's own result. It stops
pressing after `max_signals` plays. Measured on 4 Nov 09:58: the signal comes ~2.8 s after the press.

Own process (`python -m pocket_tyl.gpt`), own table gpt_ops; Tyl, ANNA and the 15-s strategy stay off. Shares the
Pocket engine's lock, so it never runs next to `python -m pocket_tyl`."""
from __future__ import annotations

import asyncio
import datetime as dt
import msvcrt
import os
import re
import sqlite3
import sys
import time
import unicodedata

from .anna import asset_of
from .conector import deal_net

MX = dt.timezone(dt.timedelta(hours=-6))
BOT_ID = 8350541047  # TRADING GPT BOT, @signals_chatgpt_bot
BUTTON = "Nueva señal"
BUTTONS = ("Nueva señal", "Obtener señal")  # "Obtener señal" right after a duration is picked in the menu
# 15-s signals (user, 4 Nov 2026 10:14: "ya está habilitado el de 15 segundos"): one press a minute, 100 plays of 15 s;
# 5-s signals from 11:12 ("tenemos habilitado el de 5 segundos, úsalo"): the 100-play limit counts the 5-s plays.
# Martingale (user, 4 Nov 2026 10:25: "aplícale martingala, tope máximo 20 dólares"): after a loss the NEXT signal plays
# the stake × 2.2 while the whole losing run stays ≤ cap ($1 → $2.20 → $4.84 → $10.65 = $18.69); a win (or that 4th
# loss) goes back to $1, a tie keeps the stake. The level comes from the booked results, so the history replayed with the
# same ladder ("con martingala") and the live stakes are one sequence.
# One press every 20 s from 11:2x (user: "sí, a tu recomendación"; a 5-s play takes ~9 s and the same Telegram account
# also feeds the gold bot, so not back to back).
# Clean series from 11:26 (user: "sí dale… limpia el bot"): back to 15-s signals, 300 plays; the scoreboard, the 300-play
# count and the martingale level count only plays opened after `since`; the earlier rows stay in gpt_ops as history.
# 12:3x (user, after the REAL account lost −37.34 with the martingale and −5.20 would have been the $1 result: "quítale la
# martingala y déjala como antes a 60 s"): $1 fixed (multiplier 1 → a one-step ladder) and one press every 60 s. The $50 cap
# and the ×2.2 ladder are kept only as history in these notes.
DEFAULTS = dict(enabled=True, bot_id=BOT_ID, every_seconds=60.0, stake=1.0, max_signals=300, count_seconds=15, wait_seconds=30.0,
                multiplier=1.0, cap=50.0, since=1793816960.0, min_payout=85.0)
# 5 Nov 2026 09:05 (user: "usa el bot de Telegram y la moneda que estábamos usando, úsalo a 5 segundos"): DEMO on EUR/USD
# (OTC) with the bot's S5 signals (5-s orders), $1 fixed, 300 plays, clean scoreboard from 09:05. The REAL setting stays 15 s.
DEFAULTS.update(count_seconds=5, since=1793891100.0)
# RESULT 5 s (09:05–09:19): 97 plays, 46 won / 46 lost / 5 ties = 50.0 %, −3.68.
# 5 Nov 2026 09:20 (user: "sabes qué, GPT a 1 minuto, a ver qué tal nos va"): the bot's M1 signals (60-s orders) on EUR/USD,
# clean scoreboard from 09:19:32.
DEFAULTS.update(count_seconds=60, since=1793891972.0)
# 09:56 (user: "deja el panel solo desde que lo arreglaste… venimos arrastrando pérdida con el viejo"): the scoreboard
# counts from 09:26:37, when one-order-at-a-time started; the overlapped 1-min plays stay as history.
DEFAULTS.update(since=1793892397.0)


def duration_label(seconds: int) -> str:
    """The bot's menu button for a signal length: S5 / S15 / S30, M1 / M3 / M5."""
    seconds = int(seconds)
    return f"S{seconds}" if seconds < 60 else f"M{seconds // 60}"
# since = 12:29:20 (user: "límpiame mi panel para iniciar"): the scoreboard and the +$25 target count from this start;
# the −$50 brake keeps counting the WHOLE Mexico day (the morning's −37.34 included), so today can lose at most $50.
SCHEMA = """CREATE TABLE IF NOT EXISTS gpt_ops (
  message INTEGER PRIMARY KEY, pressed_at REAL, posted REAL, received_at REAL, pair TEXT, asset TEXT, side TEXT,
  seconds INTEGER, confidence INTEGER, bot_clock TEXT, opened_at REAL, open_price REAL, close_price REAL, payout REAL,
  stake REAL, net REAL, ours TEXT, state TEXT)"""
LATER = (("level", "INTEGER"),)  # added with the martingale
# The 25 pairs TRADING GPT BOT offers in its menu (read 4 Nov 2026 12:3x, 5 pages of 6), in menu order.
BOT_PAIRS = ("EUR/USD (OTC)", "GBP/USD (OTC)", "USD/JPY (OTC)", "AUD/USD (OTC)", "CAD/JPY (OTC)", "GBP/AUD (OTC)",
             "EUR/CAD (OTC)", "EUR/JPY (OTC)", "GBP/JPY (OTC)", "USD/CAD (OTC)", "AUD/JPY (OTC)", "EUR/GBP (OTC)",
             "AUD/CAD (OTC)", "AUD/CHF (OTC)", "AED/CNY (OTC)", "BHD/CNY (OTC)", "CAD/CHF (OTC)", "CHF/JPY (OTC)",
             "CHF/NOK (OTC)", "EUR/HUF (OTC)", "JOD/CNY (OTC)", "NGN/USD (OTC)", "NZD/JPY (OTC)", "SAR/CNY (OTC)",
             "USD/CNH (OTC)")
# The most stable pair (user, 4 Nov 2026 12:3x: "el bot tiene muchas opciones, ¿por qué no elige otra?… la que sea más
# estable, mejor"): every `pick_every` s the test measures, on Pocket's own live prices of the last `pick_window` s, how
# much each pair moves in 15 s (standard deviation of the 15-s change, relative to the price) among the pairs the bot
# offers that Pocket pays ≥ min_payout, and switches the bot's menu to the calmest one when it is clearly calmer
# (`pick_margin`) than the current pair. Stability is not accuracy: a calmer pair does not make the signal right.
PICK = dict(pick_stable=True, pick_every=900.0, pick_window=600.0, pick_margin=0.8, pick_min_seconds=300, rotate_pairs=0)
# DEMO with several pairs (user, 4 Nov 2026 12:4x: "¿puedes correr varias al mismo tiempo para no perder tiempo… y
# seleccionar la más estable?"): the bot gives one signal for the pair chosen in its menu, so the test measures all 25
# pairs' prices at once and plays the 4 calmest in turn (one per cycle of 25 s, menu switched between plays), with a
# scoreboard per pair; the ranking is refreshed every pick_every s.
DEMO_ROTATE = dict(rotate_pairs=4, every_seconds=25.0)
# 12:5x (user: "necesito saber cuál es el más estable, tanto GPT como la moneda, entonces hazlo"): the 10 calmest pairs,
# 5 signals in a row on each before switching the menu (fewer presses), one signal every 8 s without waiting for the
# previous 15-s order to close (several DEMO orders open at once), so each pair gets ~45 GPT signals an hour.
DEMO_ROTATE.update(rotate_pairs=10, rotate_block=5, every_seconds=8.0, overlap=True)
# 12:5x ("¿pero no estás midiendo todos?"): all 25 pairs in the rotation; a pair Pocket pays under min_payout gets its GPT
# signal judged WITHOUT an order (Pocket's own price at the signal and 15 s later, state "simulada"), so the GPT accuracy
# is known for every pair.
DEMO_ROTATE.update(rotate_pairs=25, rotate_all=True, shadow=True)
# 13:1x ("ya enfócate en el top 3, descarta los otros"): the 3 best by net at $1 after the first rotation (AUD/JPY +2.28
# at 82 %, EUR/USD +1.76 at 92 %, BHD/CNY +1.52 at 92 %), 5 signals each in turn; a pair paying <85 % still judged
# without an order. The stability of all 25 keeps being measured for the panel.
DEMO_ROTATE.update(rotate_fixed=("AUD/JPY (OTC)", "EUR/USD (OTC)", "BHD/CNY (OTC)"))
# 13:3x ("dale"): only EUR/USD (16 signals 68.8 %, the only one of the 3 paying 92 %) until ~50 signals confirm it or not;
# no menu switch, one signal every 8 s.
DEMO_ROTATE.update(rotate_fixed=("EUR/USD (OTC)",))
# 5 Nov 09:05: only the bot's 5-s signals are played in the DEMO; a signal of another length makes the menu switch to S5.
# 09:20: 1-minute signals (M1) instead; still one press every 8 s, so several 1-min orders run at once.
DEMO_ROTATE.update(only_seconds=60)
# 09:3x (user: "está metiendo muy seguido, espera a que termine una y empieza la otra"): one order at a time — the next
# press comes right after the previous 1-min order closes (no overlap).
DEMO_ROTATE.update(overlap=False)
# 10:09 (EUR/USD paid 66 % from 09:37, no order for 30 min; the user chose «EUR/USD aunque pague 66 %»): the DEMO plays
# every signal whatever Pocket pays (break-even 1 / (1 + payout): 60.2 % at 66 %). The REAL keeps min_payout 85.
DEMO_ROTATE.update(min_payout=0.0)
# 10:1x (user: "cuando se pierda una, que entre como modo visión o simulación hasta que gane una, ahí vuelve a entrar,
# para no tener varias pérdidas"): after a loss the DEMO stops placing orders and only judges the signals (state
# "simulada: visión"); the first one that wins puts it back on real orders. Ties change nothing. The REAL is not touched.
DEMO_ROTATE.update(pause_after_loss=True)
# 11:00 (user: "limpia el panel y solo deja con esta nueva, para saber con cuánto dinero arrancamos y no arrastrar"):
# the scoreboard counts from 10:56:44, when the vision rule started.
DEFAULTS.update(since=1793897804.0)


def ladder_of(cfg: dict) -> list[float]:
    """$1 → ×2.2 … while the sum of the run stays within the cap: [1.0, 2.2, 4.84, 10.65] for a $20 cap."""
    steps, s = [], float(cfg.get("stake", 1.0))
    mult, cap = float(cfg.get("multiplier", 0) or 0), float(cfg.get("cap", 0) or 0)
    if mult <= 1 or cap <= 0:
        return [round(s, 2)]
    while sum(steps) + s <= cap + 1e-9:
        steps.append(round(s, 2))
        s *= mult
    return steps or [round(float(cfg.get("stake", 1.0)), 2)]


def replay_martingale(rows, steps):
    """rows: (ours, stake, net) in play order → (list of (level, stake, net with martingale), next level). A win's payout
    is its own net / stake; the recorded stake does not change the outcome."""
    level, out = 0, []
    for ours, stake, net in rows:
        bet = steps[level]
        if ours == "ganó":
            out.append((level, bet, round(bet * (net or 0) / (stake or 1), 2)))
            level = 0
        elif ours == "perdió":
            out.append((level, bet, -bet))
            level = level + 1 if level + 1 < len(steps) else 0
        else:
            out.append((level, bet, 0.0))
    return out, level
SIGNAL = re.compile(r"SE[ÑN]AL DE TRADING.*?Activo:\s*([^\n]+?)\s*\n.*?Duraci[oó]n[^:\n]*:\s*(?:([SM])\s*(\d+)|(\d+)\s*(min|m|seg|sec|s)[a-z]*)\b",
                    re.S | re.I)
DIRECTION = re.compile(r"Direcci[oó]n:\s*(ALCISTA|BAJISTA|COMPRA|VENTA|CALL|PUT|ARRIBA|ABAJO|SUBE|BAJA)", re.I)
CONFIDENCE = re.compile(r"Confianza:\s*(\d+)", re.I)
CLOCK = re.compile(r"Hora actual[^:]*:\s*(\d{1,2}:\d{2}:\d{2})", re.I)
UP = {"ALCISTA", "COMPRA", "CALL", "ARRIBA", "SUBE"}


def parse_gpt(text: str) -> dict | None:
    """The bot's signal → pair, asset, side (ARRIBA/ABAJO), seconds, confidence, bot_clock — or None."""
    t = unicodedata.normalize("NFKC", text or "")
    m, d = SIGNAL.search(t), DIRECTION.search(t)
    if not m or not d:
        return None
    if m[2]:  # "S15" / "M1"
        seconds = int(m[3]) * (60 if m[2].upper() == "M" else 1)
    else:
        seconds = int(m[4]) * (60 if m[5].lower().startswith("m") else 1)
    c, k = CONFIDENCE.search(t), CLOCK.search(t)
    pair = m[1].strip()
    return dict(pair=pair, asset=asset_of(pair), side="ARRIBA" if d[1].upper() in UP else "ABAJO", seconds=seconds,
                confidence=int(c[1]) if c else None, bot_clock=k[1] if k else None)


def has_button(message) -> bool:
    markup = getattr(message, "reply_markup", None)
    return bool(markup and getattr(markup, "rows", None) and
                any(getattr(b, "text", "") in BUTTONS for r in markup.rows for b in r.buttons))


def button_of(message) -> str:
    return next(getattr(b, "text", "") for r in message.reply_markup.rows for b in r.buttons
                if getattr(b, "text", "") in BUTTONS)


# The REAL account (user, 4 Nov 2026 11:4x: "hazme el bot para que sí opere la cuenta real… sí dale y termínalo"): only
# from «Iniciar Pocket GPT REAL.bat» (own folder PocketTylReal, his typed SI), 15-s signals only, no play limit, the
# martingale starts again at $1 each Mexico day, and a hard daily brake: no order whose loss could take the day below
# −daily_stop (so the day never loses more than $50), nor one bigger than the balance.
REAL = dict(max_signals=0, daily_stop=50.0, only_seconds=15, daily_target=25.0)  # meta +$25/day (user, 4 Nov 2026 12:2x)


def day_start(now: float | None = None) -> float:
    d = dt.datetime.fromtimestamp(now or time.time(), MX)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class GptTest:
    def __init__(self, store, cfg: dict | None, conn, real: bool = False):
        self.store, self.conn, self.real = store, conn, bool(real)
        self.cfg = {**PICK, **DEFAULTS, **({} if real else DEMO_ROTATE), **(cfg or {}), **(REAL if real else {})}
        self.rotation: list[str] = []
        self.rot_i = 0
        self.steps = ladder_of(self.cfg)
        self.setup()
        self.samples: dict[str, dict[int, float]] = {}  # asset → {whole second: last price}
        self.picked_at = 0.0
        self.started_at = time.time()
        listeners = getattr(conn, "listeners", None)
        if isinstance(listeners, list):
            listeners.append(self.on_tick)

    # ---------- the most stable pair ----------
    def on_tick(self, asset, server_ts, price, local_ts) -> None:
        book = self.samples.setdefault(asset, {})
        book[int(local_ts)] = float(price)
        if len(book) > 1500:
            for k in sorted(book)[:300]:
                del book[k]

    def volatility(self, asset, now: float | None = None) -> float | None:
        """Standard deviation of the 15-s relative price change over the last pick_window s (per-second prices, the
        last known one carried forward); None with less than pick_min_seconds of prices."""
        now = int(now or time.time())
        book = self.samples.get(asset) or {}
        start = now - int(self.cfg["pick_window"])
        secs = sorted(k for k in book if k >= start)
        if len(secs) < int(self.cfg["pick_min_seconds"]) // 3 or secs[-1] - secs[0] < int(self.cfg["pick_min_seconds"]):
            return None
        series, last = [], None
        for s in range(secs[0], secs[-1] + 1):
            last = book.get(s, last)
            series.append(last)
        moves = [(b - a) / a for a, b in zip(series, series[15:]) if a]
        if len(moves) < 30:
            return None
        mean = sum(moves) / len(moves)
        return (sum((m - mean) ** 2 for m in moves) / len(moves)) ** 0.5

    def candidates(self, every: bool = False) -> list:
        """(volatility, label, asset, payout) of the bot's pairs Pocket pays ≥ min_payout (every=True: whatever it pays)
        and that have enough prices, calmest first."""
        assets, out = getattr(self.conn, "assets", None) or {}, []
        need = 0.0 if every else float(self.cfg.get("min_payout") or 0)
        for label in BOT_PAIRS:
            asset = asset_of(label)
            row = assets.get(asset)
            if not row or len(row) <= 5 or not row[5] or (len(row) > 14 and row[14] is False):
                continue
            pay = float(row[5])
            vol = self.volatility(asset)
            if pay >= need and vol is not None:
                out.append((vol, label, asset, pay))
        return sorted(out)

    async def watch_pairs(self) -> None:
        """Live prices of the bot's 25 pairs (quietly: one "Suscrito" line per pair would bury the panel's log)."""
        if not self.cfg.get("pick_stable"):
            return
        log = getattr(self.conn, "log", None)
        try:
            if log is not None:
                self.conn.log = lambda kind, text: None
            for label in BOT_PAIRS:
                asset = asset_of(label)
                if asset in (getattr(self.conn, "assets", None) or {}) and asset not in getattr(self.conn, "streams", set()):
                    try:
                        await self.conn.subscribe(asset)
                    except Exception:
                        pass
        finally:
            if log is not None:
                self.conn.log = log

    async def press(self, client, peer, text):
        """Click `text` on the newest bot message that has it; returns that message as the bot edited it, or None."""
        for m in await client.get_messages(peer, limit=6):
            markup = getattr(m, "reply_markup", None)
            if markup and getattr(markup, "rows", None) and any(getattr(b, "text", "") == text for r in markup.rows for b in r.buttons):
                try:
                    await m.click(text=text)
                except Exception:
                    pass
                await asyncio.sleep(1.5)
                got = await client.get_messages(peer, ids=m.id)
                return got
        return None

    @staticmethod
    def labels(message) -> list:
        markup = getattr(message, "reply_markup", None)
        return [getattr(b, "text", "") for r in (markup.rows if markup and getattr(markup, "rows", None) else []) for b in r.buttons]

    async def select_pair(self, client, peer, label) -> bool:
        """Menu: Atrás (to the duration list) → Atrás (to the pair list) → page with `label` → label → S15 →
        «Obtener señal». True when the bot ends ready on that pair."""
        duration = duration_label(int(self.cfg.get('only_seconds') or self.cfg['count_seconds']))
        msg = None
        for _ in range(3):
            msg = await self.press(client, peer, "Atrás")
            if msg is None or "Selecciona un activo" in (msg.raw_text or ""):
                break
        if msg is None or "Selecciona un activo" not in (msg.raw_text or ""):
            return False
        for step in [">"] * 5 + ["<"] * 5:
            if label in self.labels(msg):
                break
            if step not in self.labels(msg):
                continue
            msg = await self.press(client, peer, step) or msg
        if label not in self.labels(msg):
            return False
        msg = await self.press(client, peer, label)
        if msg is None or duration not in self.labels(msg):
            return False
        msg = await self.press(client, peer, duration)
        return msg is not None and any(t in BUTTONS for t in self.labels(msg))

    def in_vision(self) -> bool:
        """DEMO with pause_after_loss: True while the last decided play since the clean start (real or simulated, ties
        skipped) was a loss — the next signal is then only judged, without an order."""
        if self.real or not self.cfg.get("pause_after_loss"):
            return False
        # not limited to the clean start: a new scoreboard must not reset the vision state of the last play
        row = self.store.db.execute("SELECT ours FROM gpt_ops WHERE ours IN ('ganó', 'perdió') "
                                    "ORDER BY opened_at DESC LIMIT 1").fetchone()
        return bool(row and row[0] == "perdió")

    async def shadow(self, m, sig, received, pressed, payout, vision: bool = False) -> None:
        """DEMO only: a GPT signal on a pair Pocket pays under min_payout (or in vision mode after a loss), judged without an
        order on Pocket's own price at the signal and `seconds` later (win = payout, loss = −1, same price = tie), state
        "simulada" ("simulada: visión" in vision mode)."""
        st, conn = self.store, self.conn
        asset, side, seconds = sig["asset"], sig["side"], sig["seconds"]
        await conn.subscribe(asset)
        p0 = conn.price(asset)
        if p0 is None:
            return
        now = time.time()
        with st.db:
            st.db.execute("INSERT OR IGNORE INTO gpt_ops(message,pressed_at,posted,received_at,pair,asset,side,seconds,confidence,"
                          "bot_clock,stake,state,level,opened_at,open_price,payout) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (m.id, pressed, m.date.timestamp(), received, sig["pair"], asset, side, seconds, sig["confidence"],
                           sig["bot_clock"], 1.0, "simulada: visión" if vision else "simulada", 0, now, p0, payout))
        await asyncio.sleep(seconds)
        p1 = conn.price(asset)
        if p1 is None:
            with st.db:
                st.db.execute("UPDATE gpt_ops SET state='simulada: sin precio al cierre' WHERE message=?", (m.id,))
            return
        up = p1 > p0
        ours = "empate" if p1 == p0 else ("ganó" if up == (side == "ARRIBA") else "perdió")
        net = round((payout or 0) / 100, 2) if ours == "ganó" else -1.0 if ours == "perdió" else 0.0
        with st.db:
            st.db.execute("UPDATE gpt_ops SET close_price=?, net=?, ours=? WHERE message=?", (p1, net, ours, m.id))
        if vision:
            st.event("GPT VISIÓN", f"{sig['pair']} {'CALL ↑' if side == 'ARRIBA' else 'PUT ↓'} {seconds} s · sin orden · {p0} → {p1} · "
                                   + ("ganó en simulación → la siguiente señal entra con orden" if ours == "ganó" else
                                      f"{ours} en simulación → sigue en modo visión"))
            return
        st.event("GPT SIMULADA", f"{sig['pair']} {'CALL ↑' if side == 'ARRIBA' else 'PUT ↓'} {seconds} s · paga {payout} % "
                                 f"(sin orden) · {p0} → {p1} · {ours}")

    def save_ranking(self, cands) -> None:
        import json
        self.store.put("gpt_stability", json.dumps([[c[1], c[0], c[3]] for c in cands], ensure_ascii=False))
        self.store.put("gpt_stability_at", time.time())

    async def maybe_rotate(self, client, peer, target) -> None:
        """DEMO with several pairs: the next of the `rotate_pairs` calmest pairs, switched in the bot's menu if needed."""
        n = int(self.cfg.get("rotate_pairs") or 0)
        fixed0 = [p for p in (self.cfg.get("rotate_fixed") or ()) if p in BOT_PAIRS]
        if fixed0 and not self.rotation:
            self.rotation = fixed0  # 13:24: a fixed top 3 needs no warm-up (5 min were spent on the pair left in the menu)
        if not self.rotation or time.time() - self.picked_at >= float(self.cfg["pick_every"]):
            cands = self.candidates(every=bool(self.cfg.get("rotate_all")))
            if not cands and not self.rotation:
                return  # warming up: the current pair plays until pick_min_seconds of prices are in
            if cands:  # a fixed rotation plays during the warm-up; the ranking waits for the prices
                self.picked_at = time.time()
                self.save_ranking(cands)
            fixed = [p for p in (self.cfg.get("rotate_fixed") or ()) if p in BOT_PAIRS]
            new = fixed or [c[1] for c in cands[:n]]
            if fixed and new != self.rotation:
                self.rotation = new
                info = {c[1]: c for c in cands}
                self.store.event("GPT PARES", "Enfocado en el top 3: " + ", ".join(
                    f"{p} ({info[p][0] * 1e4:.2f} pb, {info[p][3]:g} %)" if p in info else p for p in fixed)
                                 + f" · {int(self.cfg.get('rotate_block') or 1)} señales seguidas en cada uno; los que pagan menos "
                                   f"de {float(self.cfg.get('min_payout') or 0):g} % se juzgan sin orden.")
            if new != self.rotation:
                self.rotation = new
                self.store.event("GPT PARES", ("Rotación entre los 25 pares del bot (los que pagan menos de "
                                               f"{float(self.cfg.get('min_payout') or 0):g} % se juzgan sin orden), del más "
                                               "estable al más movido: " if self.cfg.get("rotate_all") else
                                               "Rotación entre los más estables que pagan "
                                               f"{float(self.cfg.get('min_payout') or 0):g} % o más: ")
                                 + ", ".join(f"{c[1]} ({c[0] * 1e4:.2f} pb, {c[3]:g} %)" for c in cands[:n]))
        block = max(1, int(self.cfg.get("rotate_block") or 1))
        desired = self.rotation[(self.rot_i // block) % len(self.rotation)]
        self.rot_i += 1
        m = re.search(r"Activo:\s*([^\n]+)", unicodedata.normalize("NFKC", target.raw_text or ""))
        current = m[1].strip() if m else None
        shown = parse_gpt(target.raw_text or "")  # the last signal says which duration the bot's menu is on
        want = int(self.cfg.get("only_seconds") or self.cfg["count_seconds"])
        wrong = shown is not None and shown["seconds"] != want
        if (current != desired or wrong) and not await self.select_pair(client, peer, desired):
            self.store.event("ATENCIÓN", f"GPT: no pude poner {desired} a {duration_label(want)} en el menú del bot; sigue con {current}.")

    async def maybe_pick(self, client, peer, target) -> None:
        if int(self.cfg.get("rotate_pairs") or 0):
            return await self.maybe_rotate(client, peer, target)
        if not self.cfg.get("pick_stable") or time.time() - self.picked_at < float(self.cfg["pick_every"]):
            return
        cands = self.candidates()
        if not cands:
            return  # not enough prices yet: the first pick comes once pick_min_seconds of prices are in
        self.picked_at = time.time()
        self.save_ranking(cands)
        m = re.search(r"Activo:\s*([^\n]+)", unicodedata.normalize("NFKC", target.raw_text or ""))
        current = m[1].strip() if m else None
        best = cands[0]
        mine = next((c for c in cands if c[1] == current), None)
        if current == best[1] or (mine and best[0] >= mine[0] * float(self.cfg["pick_margin"])):
            return
        st = self.store
        if await self.select_pair(client, peer, best[1]):
            why = (f"se mueve {best[0] * 1e4:.2f} pb cada 15 s contra {mine[0] * 1e4:.2f} de {current}" if mine else
                   f"{current or 'el par anterior'} no pasa el pago mínimo o le faltan precios")
            st.event("GPT PAR", f"Cambia a {best[1]} (el más estable de {len(cands)} pares que pagan "
                                f"{float(self.cfg.get('min_payout') or 0):g} % o más; paga {best[3]:g} %): {why}.")
        else:
            st.event("ATENCIÓN", f"GPT: no pude cambiar el par a {best[1]} en el menú del bot; sigue con {current}.")

    def since(self) -> float:
        base = float(self.cfg.get("since") or 0)
        return max(base, day_start()) if self.real else base

    def today_net(self, start: float | None = None) -> float:
        row = self.store.db.execute("SELECT SUM(net) FROM gpt_ops WHERE net IS NOT NULL AND opened_at >= ?",
                                    (max(day_start(), start or 0),)).fetchone()
        return float(row[0] or 0)

    def brake(self):
        """REAL only: (event kind, text) when the next order must not go: it reached the day's target (counted from the
        clean start `since`), it could take the WHOLE day below −daily_stop, or it is bigger than the balance."""
        nxt, today, lim = self.steps[self.level()], self.today_net(), float(self.cfg["daily_stop"])
        goal = float(self.cfg.get("daily_target") or 0)
        run = self.today_net(self.since())
        if goal and run >= goal - 1e-9:
            return ("GPT META DIARIA", f"Va {run:+.2f} USD desde el inicio limpio: llegó a la meta de +${goal:g}. No opera más "
                                       "hasta las 00:00 (hora México).")
        if lim and today - nxt < -lim - 1e-9:
            return ("GPT FRENO DIARIO", f"Hoy va {today:+.2f} USD; la siguiente apuesta (${nxt:.2f}) podría pasar el freno de "
                                        f"−${lim:g}: no opera más hasta las 00:00 (hora México).")
        bal = getattr(self.conn, "balance", None)
        if bal is not None and bal < nxt:
            return ("GPT SIN SALDO", f"Saldo ${bal:.2f} menor que la siguiente apuesta ${nxt:.2f}: no opera.")
        return None

    def snapshot_payouts(self) -> list:
        """The OTC pairs Pocket pays best right now on this account (user, 4 Nov 2026: "¿cuál sí paga mejor?"), for the
        panel: meta gpt_payouts = [[payout, symbol, name, kind], …] best first, at most every 10 s."""
        assets = getattr(self.conn, "assets", None) or {}
        rows = []
        for sym, row in assets.items():
            if not str(sym).endswith("_otc") or len(row) <= 5 or not row[5]:
                continue
            if len(row) > 14 and row[14] is False:
                continue  # closed now
            try:
                rows.append([float(row[5]), str(sym), str(row[2]), str(row[3])])
            except (TypeError, ValueError):
                continue
        rows.sort(key=lambda r: (-r[0], r[1]))
        if rows and time.time() - getattr(self, "payouts_at", 0) >= 10:
            self.payouts_at = time.time()
            import json
            self.store.put("gpt_payouts", json.dumps(rows[:20], ensure_ascii=False))
            self.store.put("gpt_payouts_at", self.payouts_at)
        return rows

    def setup(self):
        store = self.store
        with store.db:
            store.db.execute(SCHEMA)
            have = {r[1] for r in store.db.execute("PRAGMA table_info(gpt_ops)")}
            for name, kind in LATER:
                if name not in have:
                    store.db.execute(f"ALTER TABLE gpt_ops ADD COLUMN {name} {kind}")
            # orders left open by a previous run (closed or restarted process): their result never reaches this one
            store.db.execute("UPDATE gpt_ops SET state='sin resultado: se reinició el proceso' "
                             "WHERE ours IS NULL AND state IN ('abierta', 'recibida')")

    def booked(self):
        # real orders only: a "simulada" row (low payout, judged without an order) moves no money
        return self.store.db.execute("SELECT ours, stake, net, seconds FROM gpt_ops WHERE ours IS NOT NULL AND opened_at >= ? "
                                     "AND COALESCE(state, '') NOT LIKE 'simulada%' ORDER BY opened_at", (self.since(),)).fetchall()

    def level(self) -> int:
        return replay_martingale([r[:3] for r in self.booked()], self.steps)[1]

    def played(self) -> int:
        return self.store.db.execute("SELECT COUNT(*) FROM gpt_ops WHERE ours IS NOT NULL AND seconds=? AND opened_at >= ?",
                                     (int(self.cfg["count_seconds"]), self.since())).fetchone()[0]

    def totals(self) -> str:
        rows = self.booked()
        mg, _ = replay_martingale([r[:3] for r in rows], self.steps)
        parts = []
        for secs in sorted({r[3] for r in rows}):
            mine = [(r, m) for r, m in zip(rows, mg) if r[3] == secs]
            w = sum(1 for r, _ in mine if r[0] == "ganó"); l = sum(1 for r, _ in mine if r[0] == "perdió")
            one = sum((r[2] or 0) / (r[1] or 1) for r, _ in mine)
            parts.append(f"{secs} s: {len(mine)} jugadas · {w} ganadas / {l} perdidas ({100 * w / len(mine):.1f} %) · "
                         f"a $1 {one:+.2f} · con martingala {sum(m[2] for _, m in mine):+.2f}")
        return " | ".join(parts) or "0 jugadas"

    async def peer(self, client):
        from telethon.tl.types import InputPeerUser
        from .store import signalbridge_dir
        db = sqlite3.connect("file:" + (signalbridge_dir() / "telegram.session").as_posix() + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT id, hash FROM entities WHERE id=?", (int(self.cfg["bot_id"]),)).fetchone()
        finally:
            db.close()
        if row is None:
            raise RuntimeError("No encontré el chat de TRADING GPT BOT en tu Telegram.")
        return InputPeerUser(row[0], row[1])

    async def run(self, stop, login) -> None:
        st = self.store
        delay, warned = 5.0, None
        limit = (f"freno diario: nunca más de −${float(self.cfg['daily_stop']):g} en el día (hora México), solo señales de "
                 f"{int(self.cfg['only_seconds'])} s" if self.real else
                 f"hasta {int(self.cfg['max_signals'])} jugadas de {int(self.cfg['count_seconds'])} s")
        st.event("GPT", f"{'CUENTA REAL · DINERO REAL' if self.real else 'Prueba en DEMO'} de TRADING GPT BOT: cada "
                        f"{float(self.cfg['every_seconds']):g} s presiona «{BUTTON}», "
                        + (f"sin martingala (${self.steps[0]:.2f} fijo por señal), " if len(self.steps) == 1 else
                           "martingala entre señales " + " → ".join(f"${s:.2f}" for s in self.steps) +
                           f" (tope ${float(self.cfg['cap']):g}; si pierde las {len(self.steps)} vuelve a ${self.steps[0]:.2f}), ")
                        + f"solo con pago de {float(self.cfg.get('min_payout') or 0):g} % o más, "
                        f"{limit} · llevamos {self.totals()}.")
        while not stop():
            client = None
            try:
                client = login()
                await client.connect()
                peer = await self.peer(client)
                st.put("gpt_telegram", "conectado")
                delay = 5.0
                while not stop():
                    if self.real:
                        brake = self.brake()
                        if brake:
                            if warned != brake[0]:
                                warned = brake[0]
                                st.event(brake[0], brake[1])
                            await asyncio.sleep(30)
                            continue
                    if int(self.cfg["max_signals"]) and self.played() >= int(self.cfg["max_signals"]):
                        if warned != "fin":
                            warned = "fin"
                            st.event("GPT FIN", f"Prueba terminada ({int(self.cfg['max_signals'])} jugadas de {int(self.cfg['count_seconds'])} s): {self.totals()}. "
                                                "Ya no presiona el botón.")
                        await asyncio.sleep(30)
                        continue
                    if not self.conn.ready.is_set():
                        await asyncio.sleep(2)
                        continue
                    self.snapshot_payouts()
                    await self.watch_pairs()
                    recent = await client.get_messages(peer, limit=8)
                    target = next((m for m in recent if has_button(m)), None)
                    if target is not None and self.cfg.get("pick_stable"):
                        await self.maybe_pick(client, peer, target)
                        recent = await client.get_messages(peer, limit=8)
                        target = next((m for m in recent if has_button(m)), None)
                    if target is None:
                        if warned != "boton":
                            warned = "boton"
                            st.event("ATENCIÓN", f"GPT: no encuentro el botón «{BUTTON}» en el chat del bot; espero 1 min.")
                        await asyncio.sleep(60)
                        continue
                    warned = None
                    last = max(m.id for m in recent)
                    pressed = time.time()
                    try:
                        await target.click(text=button_of(target))
                    except Exception as exc:  # the bot may answer the callback late; its message still comes
                        st.event("GPT", f"El botón respondió con {type(exc).__name__}: {exc}; sigo esperando la señal.")
                    signal = await self.wait_signal(client, peer, last, pressed)
                    if signal is None:
                        await self.until(pressed + float(self.cfg["every_seconds"]), stop)
                        continue
                    if self.cfg.get("overlap") and not self.real:  # DEMO: the next signal does not wait for this result
                        task = asyncio.get_running_loop().create_task(self.play(*signal, pressed))
                        self.playing = getattr(self, "playing", set())
                        self.playing.add(task)
                        task.add_done_callback(self.playing.discard)
                    else:
                        await self.play(*signal, pressed)
                    await self.until(pressed + float(self.cfg["every_seconds"]), stop)
            except Exception as exc:
                st.put("gpt_telegram", f"error: {exc}")
                st.event("ERROR", f"GPT: {type(exc).__name__}: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

    async def until(self, at: float, stop) -> None:
        while not stop() and time.time() < at:
            await asyncio.sleep(min(1.0, max(0.0, at - time.time())))

    async def wait_signal(self, client, peer, last: int, pressed: float):
        st, wait = self.store, float(self.cfg["wait_seconds"])
        other = None
        while time.time() - pressed < wait:
            for m in sorted(await client.get_messages(peer, limit=5), key=lambda x: x.id):
                if m.id <= last or m.out:
                    continue
                sig = parse_gpt(m.raw_text or "")
                if sig is not None:
                    return m, sig, time.time()
                if "Procesando" not in (m.raw_text or ""):
                    other = m
            await asyncio.sleep(0.5)
        text = (other.raw_text or "").replace("\n", " ")[:160] if other is not None else "nada"
        st.event("ATENCIÓN", f"GPT: presioné «{BUTTON}» y en {wait:g} s no llegó una señal (el bot contestó: {text}).")
        return None

    async def play(self, m, sig, received: float, pressed: float) -> None:
        st, conn = self.store, self.conn
        level = self.level()
        stake = self.steps[level]
        message, asset, side, seconds = m.id, sig["asset"], sig["side"], sig["seconds"]
        if st.db.execute("SELECT 1 FROM gpt_ops WHERE message=?", (message,)).fetchone():
            return
        only = int(self.cfg.get("only_seconds") or 0)
        if only and seconds != only:
            st.event("ATENCIÓN", f"GPT: la señal {sig['pair']} es de {seconds} s; "
                                 f"{'en la cuenta real' if self.real else 'en esta prueba'} solo se juegan las de {only} s "
                                 f"(elige {duration_label(only)} en el bot). No se jugó.")
            return
        brake = self.brake() if self.real else None
        if brake:  # the balance or the daily brake changed while the signal came
            st.event(brake[0], brake[1])
            return
        # 4 Nov 2026 12:07: the REAL account paid 58-69 % on CADJPY_otc 15 s (the DEMO 92 %): the $1 → ×2.2 ladder only
        # wins back a losing run from about 85 %, so below `min_payout` the signal is not played (it waits for the payout).
        row = conn.assets.get(asset) if conn.assets else None
        pay_now = float(row[5]) if row and len(row) > 5 and row[5] else None
        need = float(self.cfg.get("min_payout") or 0)
        if need and (pay_now is None if self.real else False) or (need and pay_now is not None and pay_now < need):
            if self.cfg.get("shadow") and not self.real:
                return await self.shadow(m, sig, received, pressed, pay_now)
            if getattr(self, "low_payout", None) != pay_now:
                st.event("GPT PAGO BAJO", f"{sig['pair']} paga {pay_now if pay_now is not None else '¿?'} % (menos de {need:g} %): "
                                          "no se juega; espera a que Pocket suba el pago.")
            self.low_payout = pay_now
            return
        self.low_payout = None
        if self.in_vision():  # 5 Nov 10:1x: after a loss, judge without an order until a signal wins
            return await self.shadow(m, sig, received, pressed, pay_now, vision=True)
        with st.db:
            st.db.execute("INSERT INTO gpt_ops(message,pressed_at,posted,received_at,pair,asset,side,seconds,confidence,bot_clock,"
                          "stake,state,level) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (message, pressed, m.date.timestamp(), received, sig["pair"], asset, side, seconds,
                           sig["confidence"], sig["bot_clock"], stake, "recibida", level))

        def state(text, **cols):
            sets = ", ".join(f"{k}=?" for k in ("state", *cols))
            with st.db:
                st.db.execute(f"UPDATE gpt_ops SET {sets} WHERE message=?", (text, *cols.values(), message))
        label = (f"{sig['pair']} {'CALL ↑' if side == 'ARRIBA' else 'PUT ↓'} {seconds} s · confianza {sig['confidence']}%"
                 + (f" · martingala nivel {level}" if level else ""))
        try:
            if conn.assets and asset not in conn.assets:
                state("no se jugó: el par no existe en Pocket")
                st.event("ATENCIÓN", f"GPT: {label}: el par {asset} no existe en Pocket.")
                return
            row = conn.assets.get(asset) if conn.assets else None
            payout = float(row[5]) if row and len(row) > 5 and row[5] else None
            opened = await conn.open_order(asset, side, stake, seconds)
            now = time.time()
            await conn.subscribe(asset)
            open_price = float(opened.get("openPrice") or conn.price(asset) or 0)
            state("abierta", opened_at=now, open_price=open_price, payout=payout)
            st.event("GPT DISPARO", f"{label} · ${stake:.2f} · abrió en {open_price} · señal {received - pressed:.1f} s después de "
                                    f"presionar, orden {now - received:.2f} s después de leerla"
                                    + (f" · pago {payout:g} %" if payout else ""))
        except Exception as exc:
            state(f"no se jugó: {type(exc).__name__}: {exc}")
            st.event("ATENCIÓN", f"GPT: {label}: la entrada falló ({type(exc).__name__}: {exc}).")
            return
        try:
            deal = await conn.result(opened["id"], seconds + 90)
        except Exception as exc:
            state(f"sin resultado: {type(exc).__name__}")
            st.event("ATENCIÓN", f"GPT: {label}: no llegó el resultado de Pocket ({type(exc).__name__}).")
            return
        net = round(deal_net(deal), 2)
        close = float(deal.get("closePrice", 0) or 0)
        ours = "ganó" if net > 0 else "perdió" if net < 0 else "empate"
        state("cerrada", close_price=close, net=net, ours=ours)
        st.event("GPT RESULTADO", f"{label} · ${stake:.2f} · {open_price} → {close} · {ours} ({net:+.2f}) · acumulado {self.totals()}")
        if ours == "perdió" and self.in_vision():
            st.event("GPT VISIÓN", "Perdió: pasa a modo visión (sin orden) hasta que una señal gane en simulación.")
        if ours == "perdió" and len(self.steps) > 1 and level == len(self.steps) - 1:
            st.event("GPT MARTINGALA", f"Perdió las {len(self.steps)} seguidas (−${sum(self.steps):.2f}, tope ${float(self.cfg['cap']):g}): "
                                       f"la siguiente vuelve a ${self.steps[0]:.2f}.")
        elif ours == "ganó" and level:
            run = round(net - sum(self.steps[:level]), 2)  # the whole run: this win minus the losses before it
            st.event("GPT MARTINGALA", f"Ganó en el nivel {level} (${stake:.2f}): " +
                     (f"recupera la racha ({run:+.2f})" if run > 0 else f"la racha queda en {run:+.2f} (el pago no alcanzó)")
                     + f" y vuelve a ${self.steps[0]:.2f}.")


def take_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    lock = open(folder / "engine.lock", "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return None
    return lock


def main() -> None:
    from pathlib import Path
    from .engine import DemoRunner, quick_edit_off, stop_requested, telegram_login
    from .secret import demo_check, has_ssid, protect, real_check, ssid_info
    from .store import Store, data_dir, load_config, plan_of
    real = os.environ.get("POCKET_TYL_REAL") == "1"
    root = data_dir()
    demo_root = Path(os.environ.get("LOCALAPPDATA", ".")) / "PocketTyl"
    if real and root.name.lower() != "pockettylreal":
        print("La cuenta REAL solo corre en su carpeta propia (PocketTylReal), con «Iniciar Pocket GPT REAL.bat».")
        sys.exit(1)
    locks = [take_lock(root)] + ([take_lock(demo_root)] if real else [])  # REAL also holds the DEMO lock: one GPT at a time
    if not all(locks):
        print("El motor Pocket o la prueba GPT (DEMO o REAL) ya está corriendo: ciérralo primero.")
        sys.exit(0)
    try:
        (root / "stop.flag").unlink()
    except FileNotFoundError:
        pass
    quick_edit_off()
    cfg = load_config(root)
    kind = "REAL" if real else "DEMO"
    if not has_ssid(root):
        print(f"No hay sesión guardada de la cuenta {kind}." + (" Abre «Conectar Pocket REAL.bat» primero." if real else ""))
        Store(root / "pocket_tyl.sqlite").event("ATENCIÓN", f"GPT: no hay sesión de la cuenta {kind}; no arranca.")
        if real:
            input("Enter para cerrar…")
        return
    ssid = protect((root / "ssid.bin").read_bytes(), decrypt=True).decode("utf-8")
    if (real_check if real else demo_check)(ssid) is not None:
        print(f"La sesión guardada no es de la cuenta {kind}; no arranca.")
        Store(root / "pocket_tyl.sqlite").event("ATENCIÓN", f"GPT: la sesión guardada no es de la cuenta {kind}; no arranca.")
        if real:
            input("Enter para cerrar…")
        return
    if real:
        steps = ladder_of({**DEFAULTS, **(cfg.get("gpt") or {})})
        print("=" * 72)
        print("  TRADING GPT BOT en tu cuenta REAL de Pocket Option · OPERA DINERO REAL")
        print(f"  Señales de {REAL['only_seconds']} s · una cada {DEFAULTS['every_seconds']:g} s · "
              + (f"sin martingala, ${steps[0]:.2f} fijo" if len(steps) == 1 else "martingala " + " → ".join(f"${s:.2f}" for s in steps)))
        print(f"  Freno diario: nunca más de −${REAL['daily_stop']:g} en el día (hora México); no apuesta más que el saldo.")
        print(f"  Meta diaria: al llegar a +${REAL['daily_target']:g} en el día deja de operar hasta las 00:00.")
        print(f"  Solo juega si Pocket paga {DEFAULTS['min_payout']:g} % o más en el par elegido.")
        print("  Para detenerlo: cierra esta ventana.")
        print("=" * 72)
        if input("Escribe SI y pulsa Enter para operar con DINERO REAL: ").strip().upper() != "SI":
            print("No se escribió SI: no opera. Cierra esta ventana.")
            return
    store = Store(root / "pocket_tyl.sqlite")
    if real:  # the REAL window shows every event too (connection, balance, each order and its result)
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
        base_event = store.event

        def event(kind, detail):
            base_event(kind, detail)
            print(dt.datetime.now(MX).strftime("%H:%M:%S"), kind, "·", detail, flush=True)
        store.event = event
    store.put("pid", os.getpid())
    store.put("started", time.time())
    store.put("stop_requested", 0)
    store.put("gpt_mode", "real" if real else "demo")
    info = ssid_info(root)
    from .conector import PocketDemo
    conn = PocketDemo(ssid, log=store.event, real=real, url=info.get("url") if real else None,
                      user_agent=info.get("ua") if real else None)
    demo = DemoRunner(store, plan_of(cfg), conn)
    test = GptTest(store, cfg.get("gpt"), conn, real=real)
    rec = None
    if not real:  # 4 Nov 2026: record every OTC tick for our own 15-s predictor (pocket_tyl/grabador.py)
        from .grabador import TickRecorder
        rec = TickRecorder(root, conn, store)
        conn.listeners.append(rec.on_tick)
        # 5 Nov 2026: this process holds the DEMO lock, so our own 5-s predictor (pocket_tyl/grabador.py) runs here too.
        # 09:3x ("espera a que termine una y empieza la otra"): one order at a time on the DEMO → the predictor only
        # learns on paper here (no orders); «Grabar ticks Pocket.bat» alone still trades it.
        from .predictor import PaperPredictor
        pred = PaperPredictor(store, conn, trade=False)
        conn.listeners.append(pred.on_tick)
    store.put("mode", "demo_conectando")
    store.event("ARRANQUE", "GPT en la cuenta REAL de Pocket Option (encendido por el usuario con SI)." if real else
                "Prueba GPT en la DEMO de Pocket Option (Tyl, ANNA y la estrategia rápida apagadas).")

    async def go():
        stop = lambda: stop_requested(store)
        keeper = asyncio.get_running_loop().create_task(demo.keep(stop))
        try:
            await test.run(stop, telegram_login)
        finally:
            keeper.cancel()
            if rec is not None:
                rec.flush()
            store.put("mode", "detenido")
            store.event("DETENIDO", "Prueba GPT detenida.")
    try:
        asyncio.run(go())
    finally:
        store.put("pid", "")
        store.close()


if __name__ == "__main__":
    main()
