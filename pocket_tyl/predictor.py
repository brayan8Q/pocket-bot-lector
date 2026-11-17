"""Our own 15-s predictor, live and on paper (user, 4 Nov 2026: "necesito que aprenda, que empiece a predecir"): no
order is ever opened here. Every HORIZON seconds (15 s until 5 Nov 2026, 5 s since), for each OTC pair that streams, six
simple rules read the last 5, 15 and 60 s of Pocket's own price and say UP or DOWN; HORIZON seconds later the price says
who was right. Each (pair, rule) keeps its record
(pred_rules). "Learning" = per pair, the rule with the best record so far (at least MIN_N judged, at least MIN_RATE without
ties) becomes that pair's model, and from then on the model's NEXT predictions are booked apart (pred_forward): that is
the honest, out-of-sample result, the only one that counts for going to the DEMO with orders.

Rules (r_k = price now − price k seconds ago): MOM5 / MOM15 / MOM60 predict the sign of r_k (the move continues),
REV5 / REV15 / REV60 the opposite sign (it bounces back). A zero move gives no prediction."""
from __future__ import annotations

import asyncio
import time

# 5 Nov 2026 ~08:00 (user: "hazlo que sea a 5 segundos"): predictions every 5 s per pair, judged 5 s later, DEMO orders of 5 s.
HORIZON = 5
RULES =(("MOM5", 5, 1), ("REV5", 5, -1), ("MOM15", 15, 1), ("REV15", 15, -1), ("MOM60", 60, 1), ("REV60", 60, -1))
MIN_N, MIN_RATE = 30, 0.55
# DEMO orders (user, 4 Nov 2026 ~14:00: "hazlo que prediga y hazlo que opere, al final es cuenta demo, que siga
# aprendiendo… limpia el panel para que arranque desde cero"): each model prediction on a pair Pocket pays ≥ MIN_PAYOUT
# opens a $1 DEMO order of 15 s, at most MAX_OPEN at once and one per pair; the paper judging keeps learning for all.
STAKE, MIN_PAYOUT, MAX_OPEN = 1.0, 85.0, 4
# Clean test fixed BEFORE seeing its results (user, 4 Nov 2026 15:2x: "hazlo y limpia el panel"): from 14:08 to 15:15 the
# level-1 model gave 50.2 % on 3,467 new predictions (657 DEMO orders 51.2 %, −11.48) — only AUD/CAD with REV15 stood out
# (113 orders 57.5 %, +11.80; AUD/CAD was also October's only candidate). Now ONLY that pair and rule trade in DEMO, every
# REV15 call (no model threshold), 300 orders: ≥ 55 % passes, < 52 % is dropped, in between keeps testing. All rules keep
# being judged on paper for every pair.
# RESULT (4 Nov 15:25 → 5 Nov 07:59): 300 orders, 149 won / 144 lost / 7 ties = 50.9 %, −37.60 (paid 63.8 % on average,
# 38–92 %; at ≥ 80 %: 62 orders +5.28) → DROPPED. With the 5-s switch the model of each pair trades again (FOCUS empty).
FOCUS: set = set()
FOCUS_ORDERS, FOCUS_PASS, FOCUS_DROP = 300, 0.55, 0.52  # also the verdict of the model's DEMO orders (first 300)
# 15:2x (user: "hazlo que opere con ese porcentaje, no pasa nada"): AUD/CAD paid 75 %; the clean test trades whatever
# Pocket pays (DEMO). Break-even then = 1 / (1 + payout): 57.1 % at 75 %, 52.1 % at 92 %.
FOCUS_MIN_PAYOUT = 0.0
SERIES = "2026-11-05 08:05 modelo 5 s"  # a new series archives the earlier tables (renamed, never deleted)
EXP_PAUSE = 600.0  # a pair whose order Pocket refuses with IncorrectExpTime (seen 5 Nov 07:59) rests 10 min
SCHEMA = """
CREATE TABLE IF NOT EXISTS pred_rules (pair TEXT, rule TEXT, n INTEGER, w INTEGER, l INTEGER, t INTEGER, updated REAL,
  PRIMARY KEY (pair, rule));
CREATE TABLE IF NOT EXISTS pred_forward (pair TEXT, rule TEXT, at REAL, side INTEGER, p0 REAL, p1 REAL, result TEXT);
CREATE TABLE IF NOT EXISTS pred_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, pair TEXT, rule TEXT, at REAL, side INTEGER,
  stake REAL, payout REAL, open_price REAL, close_price REAL, net REAL, result TEXT, state TEXT);
"""


def new_series(store) -> None:
    """Archive the predictor tables of an earlier series (renamed with a suffix) when SERIES changes."""
    if store.get("pred_series") == SERIES:
        return
    have = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    suffix = time.strftime("%Y%m%d_%H%M%S")
    with store.db:
        for name in ("pred_rules", "pred_forward", "pred_orders"):
            if name in have:
                store.db.execute(f"ALTER TABLE {name} RENAME TO {name}_arch_{suffix}")
    store.put("pred_series", SERIES)
    store.put("pred_series_at", time.time())


class PaperPredictor:
    def __init__(self, store, conn=None, trade: bool = False):
        self.store, self.conn, self.trade = store, conn, bool(trade)
        new_series(store)
        with store.db:
            store.db.executescript(SCHEMA)
            # orders left open by a previous run: their result never reaches this one
            store.db.execute("UPDATE pred_orders SET state='falló: se reinició el proceso' "
                             "WHERE result IS NULL AND state IN ('abierta', 'enviando')")
        self.open: dict[str, int] = {}  # pair → order row id while its DEMO order runs
        self.rest: dict[str, float] = {}  # pair → time until which Pocket's expiry refusal keeps it out of orders
        self.tasks: set = set()
        self.prices: dict[str, dict[int, float]] = {}
        self.pending: list[tuple] = []  # (due second, pair, p0, [(rule, side)], model rule or None, model side)
        self.last_round: dict[str, int] = {}
        self.stats = {(p, r): [n, w, l, t] for p, r, n, w, l, t in
                      store.db.execute("SELECT pair, rule, n, w, l, t FROM pred_rules")}
        self.last_sec = 0

    # ---------- prices ----------
    def on_tick(self, asset, server_ts, price, local_ts) -> None:
        if not str(asset).endswith("_otc"):
            return
        sec = int(local_ts)
        book = self.prices.setdefault(str(asset), {})
        book[sec] = float(price)
        if len(book) > 400:
            for k in sorted(book)[:100]:
                del book[k]
        if sec != self.last_sec:
            self.last_sec = sec
            self.tick_second(sec)

    def price_at(self, pair, sec):
        """The last known price at or before `sec` (carried forward up to 3 s), or None."""
        book = self.prices.get(pair) or {}
        for s in range(sec, sec - 4, -1):
            if s in book:
                return book[s]
        return None

    # ---------- the model of each pair ----------
    def model(self, pair):
        best = None
        for rule, _, _ in RULES:
            n, w, l, t = self.stats.get((pair, rule), [0, 0, 0, 0])
            d = w + l
            if d >= MIN_N and w / d >= MIN_RATE and (best is None or w / d > best[1]):
                best = (rule, w / d)
        return best[0] if best else None

    # ---------- every second ----------
    def tick_second(self, sec) -> None:
        self.judge(sec)
        for pair in list(self.prices):
            if sec - self.last_round.get(pair, 0) < HORIZON:
                continue
            p0 = self.price_at(pair, sec)
            if p0 is None:
                continue
            calls = []
            for rule, k, sign in RULES:
                past = self.price_at(pair, sec - k)
                if past is None or past == p0:
                    continue
                calls.append((rule, sign * (1 if p0 > past else -1)))
            if not calls:
                continue
            self.last_round[pair] = sec
            chosen = self.model(pair)
            side = next((s for r, s in calls if r == chosen), None) if chosen else None
            self.pending.append((sec + HORIZON, pair, p0, calls, chosen if side else None, side))
            if self.trade and FOCUS:
                for rule, s in calls:  # the clean test: only the pre-registered pair and rule trade
                    if (pair, rule) in FOCUS and self.focus_left():
                        self.maybe_trade(pair, rule, s, min_payout=FOCUS_MIN_PAYOUT)
            elif self.trade and side:
                self.maybe_trade(pair, chosen, side)

    def focus_left(self) -> bool:
        n = self.store.db.execute("SELECT COUNT(*) FROM pred_orders WHERE state NOT LIKE 'falló%'").fetchone()[0]
        if n < FOCUS_ORDERS:
            return True
        if self.store.get("pred_focus_done") != SERIES:
            self.store.put("pred_focus_done", SERIES)
            self.store.event("PREDICTOR", f"Prueba limpia terminada: {FOCUS_ORDERS} órdenes; ya no abre más.")
        return False

    # ---------- DEMO orders ----------
    def maybe_trade(self, pair, rule, side, min_payout: float = MIN_PAYOUT) -> None:
        conn = self.conn
        if conn is None or not conn.ready.is_set() or pair in self.open or len(self.open) >= MAX_OPEN:
            return
        if self.rest.get(pair, 0.0) > time.time():
            return
        row = (getattr(conn, "assets", None) or {}).get(pair)
        payout = float(row[5]) if row and len(row) > 5 and row[5] else None
        if payout is None or payout < min_payout:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self.store.db:
            cur = self.store.db.execute("INSERT INTO pred_orders(pair, rule, at, side, stake, payout, state) VALUES(?,?,?,?,?,?,?)",
                                        (pair, rule, time.time(), side, STAKE, payout, "enviando"))
        self.open[pair] = cur.lastrowid
        task = loop.create_task(self.place(cur.lastrowid, pair, rule, side, payout))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def place(self, oid, pair, rule, side, payout) -> None:
        st, conn = self.store, self.conn
        try:
            opened = await conn.open_order(pair, "ARRIBA" if side > 0 else "ABAJO", STAKE, HORIZON)
            price = float(opened.get("openPrice") or conn.price(pair) or 0)
            with st.db:
                st.db.execute("UPDATE pred_orders SET open_price=?, state='abierta' WHERE id=?", (price, oid))
            deal = await conn.result(opened["id"], HORIZON + 90)
            from .conector import deal_net
            net = round(deal_net(deal), 2)
            close = float(deal.get("closePrice", 0) or 0)
            result = "ganó" if net > 0 else "perdió" if net < 0 else "empate"
            with st.db:
                st.db.execute("UPDATE pred_orders SET close_price=?, net=?, result=?, state='cerrada' WHERE id=?",
                              (close, net, result, oid))
        except Exception as exc:
            if "IncorrectExpTime" in str(exc):
                self.rest[pair] = time.time() + EXP_PAUSE
            with st.db:
                st.db.execute("UPDATE pred_orders SET state=? WHERE id=?", (f"falló: {type(exc).__name__}: {exc}"[:120], oid))
        finally:
            self.open.pop(pair, None)

    def judge(self, sec) -> None:
        due, keep = [x for x in self.pending if x[0] <= sec], [x for x in self.pending if x[0] > sec]
        self.pending = keep
        if not due:
            return
        now = time.time()
        with self.store.db:
            for at, pair, p0, calls, chosen, side in due:
                p1 = self.price_at(pair, at)
                if p1 is None:
                    continue
                move = 0 if p1 == p0 else (1 if p1 > p0 else -1)
                for rule, s in calls:
                    st = self.stats.setdefault((pair, rule), [0, 0, 0, 0])
                    st[0] += 1
                    if move == 0:
                        st[3] += 1
                    elif move == s:
                        st[1] += 1
                    else:
                        st[2] += 1
                    self.store.db.execute("INSERT INTO pred_rules(pair, rule, n, w, l, t, updated) VALUES(?,?,?,?,?,?,?) "
                                          "ON CONFLICT(pair, rule) DO UPDATE SET n=excluded.n, w=excluded.w, l=excluded.l, "
                                          "t=excluded.t, updated=excluded.updated", (pair, rule, *st, now))
                if chosen:
                    result = "empate" if move == 0 else ("acierto" if move == side else "fallo")
                    self.store.db.execute("INSERT INTO pred_forward(pair, rule, at, side, p0, p1, result) VALUES(?,?,?,?,?,?,?)",
                                          (pair, chosen, at - HORIZON, side, p0, p1, result))
