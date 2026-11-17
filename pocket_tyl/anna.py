"""Live check of the two ANNA channels on the Pocket Option DEMO (user, 26 Oct 2026):
  · «ANNA VIP POCKET»: "🛰 POCKET OPTION [M1]" signals, times in UTC−3, posted 1–2 min ahead; results are bare labels
    (WIN ✅ / WIN ✅¹ / WIN ✅² / ✖️ Loss / DOJI ⚖) that belong to the oldest signal they can close;
  · «ANNA TRADER VIP M1»: "🚧 LIVE SIGNAL" signals, times in UTC+6 (22:36 = 10:36 Mexico), posted from ~25 s before to
    ~15 s after the candle opens; results name their pair and candle (✅ WIN° ✅GBPUSD | ⏰ 22:34, ☑️ HIT GALE ☑️…).
One entry per signal, a fixed stake, no martingale: at the candle's first second for its 1-minute expiry or, when the
signal comes after the candle opened, at once and expiring when that candle closes — judged on the same candle the
channel judges. The channel's label is stored next to ours; only to look (no order), the price at the end of the 2nd
and 3rd minute, the candles its ¹/° and ²/°° count. Tyl's signals, book and day are never touched; own table anna_ops.
Martingale (user, 26 Oct 2026 11:15, "aplícale el martin gala que piden"): the channel's own up to 2 gales, $1 → $2.20 →
$4.84 — each candle judged at :59.8 against the previous fill and the gale sent at the next candle's :00 (as Tyl; a tie
counts as lost). The 1st order still gives the single-entry view (ours/net); the whole sequence goes to gale_*."""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
import unicodedata

from .conector import deal_net
from .disparo import LOOK, Clock, won

MX = dt.timezone(dt.timedelta(hours=-6))
POCKET_CLOCK = dt.timezone(dt.timedelta(hours=-3))  # ANNA VIP POCKET writes its times in UTC−3 (October 2026 audit)
M1_CLOCK = dt.timezone(dt.timedelta(hours=6))  # ANNA TRADER VIP M1 writes them in UTC+6
POCKET_SIGNAL = re.compile(r"POCKET OPTION.*?💷\s*(\S+).*?(M\d+).*?⌚\S*\s*(\d{1,2}):(\d{2})(?::(\d{2}))?.*?\b(call|put)\b",
                           re.S | re.I)
M1_SIGNAL = re.compile(r"LIVE SIGNAL.*?💷\s*(\S+).*?⏳\s*(\d{1,2}):(\d{2}).*?(M\d+).*?\b(PUT|CALL)\b", re.S | re.I)
M1_RESULT = re.compile(r"(WIN\s*°*|HIT GALE|LOSS|DOJI|EMPATE)\s*[✅☑️❌✖️]*\s*(\S+?)\s*\|\s*⏰\s*(\d{1,2}):(\d{2})", re.I)
MIN_LEFT = 20  # seconds of the candle still needed to enter
SCHEMA = """CREATE TABLE IF NOT EXISTS anna_ops (
  source TEXT, message INTEGER, posted REAL, pair TEXT, asset TEXT, side TEXT, seconds INTEGER, entry_at REAL,
  opened_at REAL, duration INTEGER, state TEXT, open_price REAL, close_price REAL, stake REAL, payout REAL, net REAL,
  ours TEXT, channel TEXT, channel_raw TEXT, channel_at REAL, price2 REAL, price3 REAL, PRIMARY KEY (source, message))"""
LATER = (("gale_level", "INTEGER"), ("gale_net", "REAL"), ("gale_result", "TEXT"))  # added with the martingale
ORDERS = """CREATE TABLE IF NOT EXISTS anna_orders (
  source TEXT, message INTEGER, level INTEGER, stake REAL, opened_at REAL, duration INTEGER, open_price REAL,
  close_price REAL, net REAL, PRIMARY KEY (source, message, level))"""
DEFAULTS = dict(enabled=False, channels=["ANNA TRADER VIP M1", "ANNA VIP POCKET"], stake=1.0, poll_seconds=2.0, gales=0,
                multiplier=2.2)


def normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def short(source: str) -> str:
    """"ANNA TRADER VIP M1" → "VIP M1"."""
    return (source or "").replace("ANNA ", "").replace("TRADER ", "")


def pair_key(pair: str) -> tuple[str, bool]:
    p = (pair or "").upper()
    return re.sub(r"[^A-Z]", "", p.replace("OTC", ""))[:6], "OTC" in p


def asset_of(pair: str) -> str:
    base, otc = pair_key(pair)
    return base + ("_otc" if otc else "")


def clock_time(tz, near: float, h: int, m: int, s: int = 0) -> float:
    """The moment written h:m:s on the channel's clock that is nearest to `near` (handles the day change)."""
    local = dt.datetime.fromtimestamp(near, tz)
    t = local.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
    if t < near - 43200:
        t += 86400
    elif t > near + 43200:
        t -= 86400
    return t


def parse_signal(text: str, posted: float) -> dict | None:
    """A signal of either channel: pair, asset, side (ARRIBA/ABAJO), seconds and entry_at — or None."""
    t = normal(text)
    m = POCKET_SIGNAL.search(t)
    if m:
        return dict(pair=m[1].upper(), asset=asset_of(m[1]), side="ARRIBA" if m[6].lower() == "call" else "ABAJO",
                    seconds=int(m[2][1:]) * 60, entry_at=clock_time(POCKET_CLOCK, posted, int(m[3]), int(m[4]), int(m[5] or 0)))
    m = M1_SIGNAL.search(t)
    if m:
        return dict(pair=m[1].upper(), asset=asset_of(m[1]), side="ARRIBA" if m[5].upper() == "CALL" else "ABAJO",
                    seconds=int(m[4][1:]) * 60, entry_at=clock_time(M1_CLOCK, posted, int(m[2]), int(m[3])))
    return None


def parse_result(text: str) -> dict | None:
    """The channel's label: dict(label, raw, pair, hh, mm); label is WIN, WIN¹, WIN², LOSS or DOJI (¹ = 2nd candle,
    ² = 3rd; the M1 channel writes them ° and °°, and its loss "HIT GALE"). pair/hh/mm only on the M1 channel."""
    t = normal(text).upper()
    m = M1_RESULT.search(t)
    if m:
        raw = re.sub(r"\s+", " ", m[1]).strip().replace(" °", "°")
        label = {"WIN": "WIN", "WIN°": "WIN¹", "WIN°°": "WIN²", "DOJI": "DOJI", "EMPATE": "DOJI"}.get(raw, "LOSS")
        return dict(label=label, raw=raw, pair=m[2], hh=int(m[3]), mm=int(m[4]))
    if t.startswith("DOJI"):
        return dict(label="DOJI", raw="DOJI", pair=None, hh=None, mm=None)
    if re.match("^✖️?\\s*LOSS\\b", t):  # "✖️ Loss" (a bare "LOSS" belonged to the old ENTRY CONFIRMED posts)
        return dict(label="LOSS", raw="Loss", pair=None, hh=None, mm=None)
    m = re.match(r"^WIN ✅\s*(\d)?$", t)  # NFKC turns ¹ ² into 1 2
    if m:
        label = "WIN" + ("¹" if m[1] == "1" else "²" if m[1] == "2" else f"^{m[1]}" if m[1] else "")
        return dict(label=label, raw=label, pair=None, hh=None, mm=None)
    return None


class AnnaTest:
    def __init__(self, store, cfg: dict | None, conn, clock=None):
        self.store, self.conn = store, conn
        self.cfg = {**DEFAULTS, **(cfg or {})}
        if not (cfg or {}).get("channels") and (cfg or {}).get("channel_title"):
            self.cfg["channels"] = [cfg["channel_title"]]
        self.clock = clock or (lambda: Clock(conn.clock_offset() if conn is not None else 0.0))
        self.tasks: set[asyncio.Task] = set()
        with store.db:
            store.db.execute(SCHEMA)
            store.db.execute(ORDERS)
            have = {r[1] for r in store.db.execute("PRAGMA table_info(anna_ops)")}
            for name, kind in LATER:
                if name not in have:
                    store.db.execute(f"ALTER TABLE anna_ops ADD COLUMN {name} {kind}")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    @property
    def channels(self) -> list[str]:
        return list(self.cfg.get("channels") or [])

    # ---------- messages ----------
    def on_message(self, source: str, message: int, text: str, posted: float) -> None:
        sig = parse_signal(text, posted)
        if sig is not None:
            self.on_signal(source, message, posted, sig)
            return
        res = parse_result(text)
        if res is not None:
            self.on_result(source, res, posted)

    def on_signal(self, source, message, posted, sig) -> None:
        st = self.store
        if st.db.execute("SELECT 1 FROM anna_ops WHERE source=? AND message=?", (source, message)).fetchone():
            return
        with st.db:
            st.db.execute("INSERT INTO anna_ops(source,message,posted,pair,asset,side,seconds,entry_at,state,stake) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)", (source, message, posted, sig["pair"], sig["asset"], sig["side"],
                                                          sig["seconds"], sig["entry_at"], "esperando", float(self.cfg["stake"])))
        lead = posted - sig["entry_at"]
        st.event("ANNA SEÑAL", f"{short(source)} · {sig['pair']} {'CALL' if sig['side'] == 'ARRIBA' else 'PUT'} "
                               f"M{sig['seconds'] // 60} · vela de las {dt.datetime.fromtimestamp(sig['entry_at'], MX):%H:%M:%S} · "
                               f"publicada {abs(lead):.0f} s {'después de abrir la vela' if lead > 0 else 'antes'}")
        task = asyncio.get_running_loop().create_task(self.play(source, message, sig))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def on_result(self, source: str, res: dict, at: float) -> None:
        st = self.store
        if res["pair"] is not None:  # M1: the label names its pair and candle
            entry = clock_time(M1_CLOCK, at, res["hh"], res["mm"])
            key = pair_key(res["pair"])
            rows = st.db.execute("SELECT message, pair FROM anna_ops WHERE source=? AND channel IS NULL AND abs(entry_at - ?) < 30 "
                                 "ORDER BY message", (source, entry)).fetchall()
            row = next((r for r in rows if pair_key(r[1]) == key), None)
        else:  # POCKET: the oldest open signal whose candles it can close (entry + 50 s … + 4 min)
            row = st.db.execute("SELECT message, pair FROM anna_ops WHERE source=? AND channel IS NULL AND entry_at + 50 <= ? "
                                "AND ? <= entry_at + 240 ORDER BY entry_at, message LIMIT 1", (source, at, at)).fetchone()
        if row is None:
            return  # a result of a signal posted before the test started
        with st.db:
            st.db.execute("UPDATE anna_ops SET channel=?, channel_raw=?, channel_at=? WHERE source=? AND message=?",
                          (res["label"], res["raw"], at, source, row[0]))
        self.compare(source, row[0])

    # ---------- the single entry ----------
    async def play(self, source, message, sig) -> None:
        st, conn = self.store, self.conn
        stake = float(self.cfg["stake"])
        pair, asset, side, seconds, entry_at = sig["pair"], sig["asset"], sig["side"], sig["seconds"], sig["entry_at"]
        closes = entry_at + seconds

        def state(text, **cols):
            sets = ", ".join(f"{k}=?" for k in ("state", *cols))
            with st.db:
                st.db.execute(f"UPDATE anna_ops SET {sets} WHERE source=? AND message=?", (text, *cols.values(), source, message))
        opened = None
        try:
            if time.time() > closes - MIN_LEFT:
                state("no se jugó: llegó cuando la vela ya casi cerraba"); return
            while not conn.ready.is_set() and time.time() < closes - MIN_LEFT:
                await asyncio.sleep(0.5)
            if not conn.ready.is_set():
                state("no se jugó: la demo estaba desconectada"); return
            if conn.assets and asset not in conn.assets:
                state("no se jugó: el par no existe en Pocket"); return
            row = conn.assets.get(asset) if conn.assets else None
            payout = float(row[5]) if row and len(row) > 5 and row[5] else None
            state("esperando la hora de entrada", payout=payout)
            await conn.subscribe(asset)
            clock = self.clock()
            if clock.now() < entry_at:
                await clock.until(entry_at)
            now = clock.now()
            duration = seconds if now < entry_at + 1 else int(round(closes - now))
            if duration < MIN_LEFT:
                state("no se jugó: llegó cuando la vela ya casi cerraba"); return
            try:
                opened = await conn.open_order(asset, side, stake, duration)
                late = f" (entró {now - entry_at:.0f} s después de abrir la vela, vence al cierre)" if duration != seconds else ""
            except RuntimeError as exc:
                # real-market pairs only take a whole minute (26 Oct 11:23, GBPUSD/USDJPY "IncorrectExpTime" at 57 s)
                if "IncorrectExpTime" not in str(exc) or duration == seconds:
                    raise
                duration = seconds
                opened = await conn.open_order(asset, side, stake, duration)
                late = f" (entró {now - entry_at:.0f} s después de abrir la vela; en este par Pocket solo acepta 1 min)"
            open_price = float(opened.get("openPrice") or conn.price(asset) or 0)
            state("abierta", open_price=open_price, opened_at=now, duration=duration)
            self.order(source, message, 0, stake, now, duration, open_price)
            st.event("ANNA DISPARO", f"{short(source)} · {pair} {side} ${stake:.2f} {duration} s · abrió en {open_price}{late}")
        except Exception as exc:
            state(f"no se jugó: {exc}" if opened is None else f"error tras abrir: {type(exc).__name__}: {exc}")
            st.event("ATENCIÓN", f"Anna {short(source)} {pair}: la entrada falló ({type(exc).__name__}: {exc}).")
            return
        looking = asyncio.ensure_future(self.look(source, message, asset, entry_at, seconds))
        results = [asyncio.ensure_future(self.settle(source, message, pair, side, 0, opened["id"], duration))]
        level, previous = 0, open_price
        try:  # the channel's gales: judge each candle at :59.8, the next order at the next candle's :00
            while level < int(self.cfg["gales"]):
                at = entry_at + seconds * (level + 1)
                await clock.until(at - LOOK)
                price = conn.price(asset)
                if price is None:
                    st.event("ATENCIÓN", f"Anna {short(source)} {pair}: sin precio para juzgar la vela; no sigue la martingala.")
                    break
                if won(side, previous, price):
                    break
                level += 1
                stake_k = round(stake * float(self.cfg["multiplier"]) ** level, 2)
                await clock.until(at)
                order = await conn.open_order(asset, side, stake_k, seconds)
                previous = float(order.get("openPrice") or conn.price(asset) or price)
                state(f"gale {level}")
                self.order(source, message, level, stake_k, clock.now(), seconds, previous)
                st.event("ANNA GALE", f"{short(source)} · {pair} {side} gale {level} ${stake_k:.2f} · abrió en {previous}")
                results.append(asyncio.ensure_future(self.settle(source, message, pair, side, level, order["id"], seconds)))
        except Exception as exc:
            st.event("ATENCIÓN", f"Anna {short(source)} {pair}: la gale falló ({type(exc).__name__}: {exc}).")
        await asyncio.gather(*results, return_exceptions=True)
        self.finish(source, message, pair)
        await asyncio.gather(looking, return_exceptions=True)

    def order(self, source, message, level, stake, opened_at, duration, open_price) -> None:
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO anna_orders(source,message,level,stake,opened_at,duration,open_price) "
                                  "VALUES(?,?,?,?,?,?,?)", (source, message, level, stake, opened_at, duration, open_price))

    async def settle(self, source, message, pair, side, level, order_id, duration) -> None:
        st = self.store
        deal = await self.conn.result(order_id, duration + 90)
        net = round(deal_net(deal), 2)
        close = float(deal.get("closePrice", 0) or 0)
        with st.db:
            st.db.execute("UPDATE anna_orders SET close_price=?, net=? WHERE source=? AND message=? AND level=?",
                          (close, net, source, message, level))
        if level == 0:  # the single-entry view
            ours = "ganó" if net > 0 else "perdió" if net < 0 else "empate"
            open_price = st.db.execute("SELECT open_price FROM anna_ops WHERE source=? AND message=?", (source, message)).fetchone()[0]
            with st.db:
                st.db.execute("UPDATE anna_ops SET close_price=?, net=?, ours=? WHERE source=? AND message=?",
                              (close, net, ours, source, message))
            st.event("ANNA RESULTADO", f"{short(source)} · {pair} {side} · {open_price} → {close} · 1ª entrada {ours} "
                                       f"({'+' if net > 0 else ''}{net:.2f})")

    def finish(self, source, message, pair) -> None:
        """The whole sequence (1st order + gales): ganó en la Nª vela / perdió las N / empate."""
        st = self.store
        orders = st.db.execute("SELECT level, net FROM anna_orders WHERE source=? AND message=? ORDER BY level",
                               (source, message)).fetchall()
        if not orders or any(n is None for _, n in orders):
            with st.db:
                st.db.execute("UPDATE anna_ops SET state='cerrada' WHERE source=? AND message=?", (source, message))
            return
        level, last = orders[-1]
        total = round(sum(n for _, n in orders), 2)
        result = (f"ganó en la {level + 1}ª vela" if last > 0 else "empate" if last == 0 else
                  f"perdió las {len(orders)}" if len(orders) > 1 else "perdió")
        with st.db:
            st.db.execute("UPDATE anna_ops SET state='cerrada', gale_level=?, gale_net=?, gale_result=? WHERE source=? AND message=?",
                          (level, total, result, source, message))
        if int(self.cfg["gales"]):
            st.event("ANNA MARTINGALA", f"{short(source)} · {pair} · {result} · {'+' if total > 0 else ''}{total:.2f}")
        self.compare(source, message)

    async def look(self, source, message, asset, entry_at, seconds) -> None:
        """Only to look (no order): the price at the end of the 2nd and 3rd minute, the candles the channel's ¹/° and ²/°°
        count."""
        try:
            clock = self.clock()
            for k, col in ((2, "price2"), (3, "price3")):
                await clock.until(entry_at + seconds * k)
                price = self.conn.price(asset)
                with self.store.db:
                    self.store.db.execute(f"UPDATE anna_ops SET {col}=? WHERE source=? AND message=?", (price, source, message))
        except Exception:
            pass

    # ---------- comparison ----------
    def compare(self, source: str, message: int) -> None:
        st = self.store
        row = st.db.execute("SELECT pair, side, ours, net, channel_raw, gale_result, gale_net FROM anna_ops "
                            "WHERE source=? AND message=?", (source, message)).fetchone()
        if row is None or row[2] is None or row[4] is None or row[5] is None:
            return  # waiting for the other half (our sequence or the channel's label)
        pair, side, ours, net, channel, gale_result, gale_net = row
        gale = (f" · con martingala: {gale_result} ({'+' if gale_net > 0 else ''}{gale_net:.2f})" if int(self.cfg["gales"]) else "")
        st.event("ANNA COMPARA", f"{short(source)} · {pair} {'CALL' if side == 'ARRIBA' else 'PUT'} · canal: {channel} · "
                                 f"1ª entrada: {ours} ({'+' if net > 0 else ''}{net:.2f}){gale} · " + self.totals())

    def totals(self) -> str:
        parts = []
        for source, n, won_, lost, net, gale in self.store.db.execute(
                "SELECT source, COUNT(*), SUM(ours='ganó'), SUM(ours='perdió'), SUM(net), SUM(gale_net) FROM anna_ops "
                "WHERE ours IS NOT NULL GROUP BY source ORDER BY source").fetchall():
            parts.append(f"{short(source)}: {n} jugadas, 1ª entrada {won_} ganadas / {lost} perdidas, neto {net or 0:+.2f}"
                         + (f", con martingala {gale or 0:+.2f}" if int(self.cfg["gales"]) else ""))
        return "acumulado · " + " · ".join(parts)
