"""Own 15-second strategy on the Pocket Option DEMO account (user, 23 Oct 2026: "que opere analizando el mercado si
alcista o bajista a 15 segundos … que use nuestra cuenta demo … en tiempo real", with Monte Carlo).

Every tick of its pairs is recorded (ticks/ticks_YYYY-MM-DD.csv). Each second three rules read the last two minutes of
prices sampled once a second:
  tendencia  the last 30 s moved more than 1.5 typical 30-s moves → the same way
  rebote     the last 10 s moved more than 2.5 typical 10-s moves → the other way
  cruce      the 5-s average crossed the 20-s average → the way it crossed
Each rule's signals are scored on paper at +15 s (ticks/rapido_papel_YYYY-MM-DD.csv). The rule with the best recent
paper score trades live on the DEMO: one order at a time, fixed stake, no martingale, only on a pair paying at least
`min_payout`, with its own daily target and brake (Mexico City day), apart from Tyl's book and marker. Every 15 minutes a
Monte Carlo (thousands of simulated days drawn from the recent results) estimates how often a day would end at the target
or at the brake. DEMO only: the connector refuses any other account."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import math
from pathlib import Path
import random
import statistics
import time

import json

from .cerebro import Model, features
from .conector import deal_net
from .core import MEXICO

BASE = ("tendencia", "rebote", "cruce", "ema")
# 23 Oct 2026 (user: "que sea inteligente, que se mueva"): each rule's opposite is scored too; a rule that loses 65 % on
# paper means its opposite wins 65 %. The opposites are derived from the base results (never written twice).
OPPOSITE = {"tendencia": "contra_tendencia", "rebote": "impulso", "cruce": "contra_cruce", "ema": "contra_ema"}
RULES = BASE + tuple(OPPOSITE.values())
LABEL = {"tendencia": "tendencia de 30 s", "rebote": "rebote tras un movimiento brusco",
         "cruce": "cruce de promedios 5 s / 20 s", "contra_tendencia": "contra la tendencia de 30 s",
         "impulso": "seguir el movimiento brusco", "contra_cruce": "contra el cruce de promedios",
         "ema": "tendencia del momento (promedio 5 s sobre 20 s)", "contra_ema": "contra la tendencia del momento",
         "cerebro": "cerebro (velas + tendencia, aprende solo)",
         "giro": "giro (3 velas iguales, RSI extremo o bandas)"}
DEFAULTS = dict(enabled=False, assets=["USDCAD_otc", "AUDCAD_otc", "NZDUSD_otc"], stake=1.0, seconds=15, min_payout=85,
                target=20.0, loss_stop=-20.0, warmup=12, window=60, pause=3.0, report_minutes=15, confidence=0.6,
                continuous=False, brain=False, giro=False, giro_votes=1, giro_cards=False)
# giro_votes = 2 (user, 23 Oct 2026, "dale" after the per-signal study: at 10 s two agreeing signals won 58.7 % against
# 54.3 % for any one): an order only when at least two reversal signals agree. giro_cards: each signal keeps its own paper
# scorecard per pair; one below the break-even share (after 15 results) stops counting there until it recovers, and a
# pair whose two-signal results fall below it is skipped.
# giro (user, 23 Oct 2026, option B after the tick study: at 3 s nothing beat a coin; the reversal votes won 53-61 %):
# 10-s orders only when the reversal votes (3 candles of one colour, RSI beyond 70/30, price beyond 2 deviations) point
# one way; one order at a time on the best-paying pair; each signal is also scored on paper.
# continuous (user, 23 Oct 2026: "necesito que cada 3 s esté metiendo una operación", "quita MC si eso evita que meta
# entradas", "que sea inteligente, identifique tendencia"): an order every pass, no Monte Carlo or break-even gate; the
# direction is the best-scored rule that fires now, else the better of the trend of the moment and its opposite.
SPAN = 130  # seconds of prices kept per pair: two minutes of 1-s samples plus margin
SCHEMA = """
CREATE TABLE IF NOT EXISTS rapido_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, asset TEXT, direction TEXT, opened REAL, open_price REAL,
  close_price REAL, stake REAL, payout REAL, net REAL, day TEXT);
CREATE TABLE IF NOT EXISTS rapido_days (day TEXT PRIMARY KEY, net REAL, trades INTEGER, stopped TEXT);
"""


def paper_name(day: str, seconds: int) -> str:
    """Paper results per expiry (23 Oct 2026: 15 s, then 3 s): a 3-s result is not a 15-s one."""
    return f"rapido_papel_{day}.csv" if int(seconds) == 15 else f"rapido_papel_{int(seconds)}s_{day}.csv"


MIN_SECONDS = 5  # the shortest expiry tried after Pocket refuses a shorter one


def giro_direction(x: list[float] | None) -> str | None:
    """The reversal votes of cerebro.features (racha, RSI 14, bandas): ARRIBA / ABAJO when their sum points one way."""
    if not x:
        return None
    total = x[6] + x[8] + x[9]
    return "ARRIBA" if total > 0 else "ABAJO" if total < 0 else None


GIRO = {"racha": 6, "rsi": 8, "bandas": 9}  # the reversal votes' places in cerebro.features
GIRO_LABEL = {"racha": "3 velas iguales", "rsi": "RSI 70/30", "bandas": "bandas", "2de3": "2 de 3"}


def giro_votes(x: list[float] | None, allowed=GIRO) -> str | None:
    """ARRIBA / ABAJO when at least two of the allowed reversal votes point that way (user, 23 Oct 2026)."""
    if not x:
        return None
    votes = [x[GIRO[name]] for name in allowed]
    return "ARRIBA" if votes.count(1) >= 2 else "ABAJO" if votes.count(-1) >= 2 else None


def money(v: float) -> str:
    return f"-${-v:,.2f}" if v < 0 else f"+${v:,.2f}"


def day_of(t: float) -> str:
    return datetime.fromtimestamp(t, MEXICO).date().isoformat()


def grid(ticks, now: float, span: int = SPAN) -> list[float]:
    """The price at each whole second of the last `span` seconds (the last tick at or before it); [] while too short."""
    ticks = list(ticks)
    if not ticks or ticks[0][0] > now - span:
        return []
    out, i, last = [], 0, None
    for k in range(span, -1, -1):
        t = now - k
        while i < len(ticks) and ticks[i][0] <= t:
            last = ticks[i][1]
            i += 1
        out.append(last)
    return out


def ema(values: list[float], n: int) -> list[float]:
    a, e, out = 2 / (n + 1), values[0], []
    for v in values:
        e = a * v + (1 - a) * e
        out.append(e)
    return out


def decide(rule: str, g: list[float]) -> str | None:
    """ARRIBA / ABAJO when the rule fires on these 1-s prices (oldest first), else None."""
    if len(g) < 61:
        return None
    d = [g[i] - g[i - 1] for i in range(1, len(g))]
    sigma = statistics.pstdev(d[-120:])
    if sigma <= 0:
        return None
    if rule == "tendencia":
        move = g[-1] - g[-31]
        if abs(move) > 1.5 * sigma * math.sqrt(30):
            return "ARRIBA" if move > 0 else "ABAJO"
    elif rule == "rebote":
        move = g[-1] - g[-11]
        if abs(move) > 2.5 * sigma * math.sqrt(10):
            return "ABAJO" if move > 0 else "ARRIBA"
    elif rule == "ema":
        fast, slow = ema(g[-40:], 5), ema(g[-40:], 20)
        if fast[-1] > slow[-1]:
            return "ARRIBA"
        if fast[-1] < slow[-1]:
            return "ABAJO"
    elif rule == "cruce":
        fast, slow = ema(g, 5), ema(g, 20)
        if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
            return "ARRIBA"
        if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
            return "ABAJO"
    return None


def outcome(direction: str, open_price: float, close_price: float) -> int:
    """+1 won, -1 lost, 0 same price (Pocket gives the stake back)."""
    if close_price == open_price:
        return 0
    return 1 if (close_price > open_price) == (direction == "ARRIBA") else -1


def paper_from_rows(rows) -> dict[str, list[int]]:
    """Paper outcomes per rule from the paper file's rows (rule, asset, direction, opened, open, close, outcome), with each
    base rule's opposite derived (a tie stays a tie)."""
    out = {r: [] for r in RULES}
    for row in rows:
        if len(row) < 7 or row[0] not in OPPOSITE:
            continue
        try:
            o = int(row[6])
        except ValueError:
            continue
        out[row[0]].append(o)
        out[OPPOSITE[row[0]]].append(-o)
    return out


def confidence(outcomes, need: float, runs: int = 1000, seed=None) -> float:
    """Monte Carlo before a bet (user, 23 Oct 2026): the share of `runs` resamples of these results whose win share is above
    `need`, i.e. how sure the rule's edge is not luck."""
    decided = [o for o in outcomes if o]
    if not decided:
        return 0.0
    rnd = random.Random(seed)
    k, above = len(decided), 0
    for _ in range(runs):
        sample = rnd.choices(decided, k=k)
        if sum(1 for o in sample if o > 0) / k > need:
            above += 1
    return above / runs


def hit_rate(outcomes) -> tuple[float | None, int]:
    decided = [o for o in outcomes if o]
    return (sum(1 for o in decided if o > 0) / len(decided) if decided else None), len(decided)


def monte_carlo(outcomes, payout: float, stake: float, target: float, stop: float, runs: int = 2000,
                max_trades: int = 300, seed=None) -> dict | None:
    """Simulated days drawn at random from `outcomes` (+1 / -1 / 0): how often the day ends at the target, at the brake,
    or at neither after `max_trades`; and the median day."""
    pool = list(outcomes)
    if len(pool) < 10:
        return None
    rnd = random.Random(seed)
    win = stake * payout
    meta = freno = 0
    finals = []
    for _ in range(runs):
        net = 0.0
        for _ in range(max_trades):
            o = rnd.choice(pool)
            net += win if o > 0 else -stake if o < 0 else 0.0
            if net >= target - 1e-9:
                meta += 1
                break
            if net <= stop + 1e-9:
                freno += 1
                break
        finals.append(net)
    return dict(meta=meta / runs, freno=freno / runs, ninguno=(runs - meta - freno) / runs,
                mediana=statistics.median(finals), muestras=len(pool))


class Rapido:
    def __init__(self, store, cfg: dict, conn, root: Path, busy=lambda: False, clock=time.time):
        self.store, self.conn, self.root, self.busy_tyl, self.clock = store, conn, Path(root), busy, clock
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.assets = list(self.cfg["assets"])
        self.ticks = {a: deque() for a in self.assets}
        self.paper_open: dict[tuple[str, str], tuple[float, str, float]] = {}
        self.paper = {r: deque(maxlen=400) for r in RULES}
        self.live_outcomes = deque(maxlen=400)
        self.active: str | None = None
        self.paused_told = False
        self.doubt_told = False
        self.sure: dict[str, tuple[tuple, float]] = {}  # rule → (results it was computed on, Monte Carlo confidence)
        # The learning brain (cerebro.py): its weights survive restarts in the engine's meta table.
        try:
            saved = json.loads(store.get("cerebro_pesos") or "null")
        except ValueError:
            saved = None
        self.model = Model(saved)
        self.samples: list[tuple[float, str, float, list[float], str]] = []  # (due, asset, price, votes, its call then)
        self.latest: dict[str, list[float]] = {}
        self.brain_outcomes = deque(maxlen=400)
        self.giro_open: dict[str, tuple[float, str, float]] = {}  # asset → (due, direction, price) on paper
        self.giro_outcomes = deque(maxlen=400)
        self.cards: dict[tuple[str, str], deque] = {}  # (signal or "2de3", pair) → its paper outcomes at this expiry
        self.card_open: dict[tuple[str, str], tuple[float, str, float]] = {}
        self.card_state: dict[tuple[str, str], bool] = {}  # last reported on/off of each card
        self.last_save = self.clock()
        if self.cfg["brain"]:
            self.active = "cerebro"
        if self.cfg["giro"]:
            self.active = "giro"
        self.trading = False
        self.next_at = 0.0
        self.failures = 0
        self.last_report = self.last_mc = self.clock()
        self.tick_rows: list[str] = []
        self.paper_rows: list[str] = []
        self.last_flush = self.clock()
        with store.db:
            store.db.executescript(SCHEMA)
        conn.listeners.append(self.on_tick)
        self.load_paper()
        self.load_ticks()

    # ---------- data ----------
    def load_ticks(self) -> None:
        """The last two minutes of recorded prices, so a restart trades at once instead of waiting for them (user, 23 Oct
        2026: "iba bien, ¿por qué dejó de operar?" after a restart)."""
        path = self.root / "ticks" / f"ticks_{day_of(self.clock())}.csv"
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 400_000))
                lines = f.read().decode("utf-8", "replace").splitlines()[1:]
        except OSError:
            return
        oldest = self.clock() - SPAN - 10
        for line in lines:
            parts = line.split(",")
            if len(parts) < 4 or parts[0] not in self.ticks:
                continue
            try:
                local, price = float(parts[1]), float(parts[3])
            except ValueError:
                continue
            if local >= oldest:
                self.ticks[parts[0]].append((local, price))

    def load_paper(self) -> None:
        """Today's paper results from the paper file, so a restart does not start from zero (23 Oct 2026)."""
        import csv
        path = self.root / "ticks" / paper_name(day_of(self.clock()), self.cfg["seconds"])
        try:
            with open(path, encoding="utf-8") as f:
                rows = list(csv.reader(f))
        except OSError:
            return
        for rule, outcomes in paper_from_rows(rows).items():
            self.paper[rule].extend(outcomes)
        for row in rows:  # the giro scorecards: rows "giro_<signal>" per pair
            if len(row) >= 7 and row[0].startswith("giro_"):
                try:
                    self.card(row[0][5:], row[1]).append(int(row[6]))
                except ValueError:
                    pass

    def on_tick(self, asset: str, server_ts: float, price: float, local: float) -> None:
        q = self.ticks.get(asset)
        if q is None:
            return
        q.append((local, price))
        while q and q[0][0] < local - SPAN - 10:
            q.popleft()
        self.tick_rows.append(f"{asset},{local:.3f},{server_ts:.3f},{price}\n")

    def flush(self, now: float) -> None:
        if now - self.last_flush < 2 and len(self.tick_rows) < 500:
            return
        self.last_flush = now
        folder = self.root / "ticks"
        folder.mkdir(parents=True, exist_ok=True)
        day = day_of(now)
        for rows, name in ((self.tick_rows, f"ticks_{day}.csv"), (self.paper_rows, paper_name(day, self.cfg["seconds"]))):
            if rows:
                with open(folder / name, "a", encoding="utf-8") as f:
                    f.writelines(rows)
                rows.clear()

    def payout(self, asset: str) -> float | None:
        row = self.conn.assets.get(asset)
        try:
            return float(row[5]) if row and len(row) > 5 else None
        except (TypeError, ValueError):
            return None

    def day(self, day: str) -> tuple[float, int, str | None]:
        row = self.store.db.execute("SELECT net, trades, stopped FROM rapido_days WHERE day=?", (day,)).fetchone()
        return (row[0], row[1], row[2]) if row else (0.0, 0, None)

    # ---------- the second-by-second pass ----------
    async def run(self, stop) -> None:
        c = self.cfg
        limits = (f"meta {money(c['target'])} / freno {money(c['loss_stop'])} al día" if c["target"] or c["loss_stop"]
                  else "sin meta ni freno diario")
        how = (("solo cuando coinciden al menos 2 de 3 señales de giro" if int(c["giro_votes"]) >= 2 else "solo señales de giro")
               + ": 3 velas de 5 s del mismo color, RSI 14 arriba de 70 o abajo de 30, o precio fuera de sus bandas de 20 s"
               + ("; cada señal lleva su marcador por par y deja de contar si baja del mínimo" if c["giro_cards"] else "")
               + "; una operación a la vez" if c["giro"] else
               "cerebro que aprende solo de velas de 5 s, tendencia corta y larga, impulso, mechas, RSI, bandas y rupturas; "
               "opera sin parar" if c["brain"] else f"reglas: {', '.join(LABEL[r] for r in RULES)}")
        self.store.event("RÁPIDO", f"Estrategia de {c['seconds']} s en DEMO · pares {', '.join(self.assets)} · ${c['stake']:g} por "
                                   f"operación, sin martingala · solo con pago ≥ {c['min_payout']}% · {limits} · {how}.")
        self.stop_check = stop  # no new order once a stop is asked (a restart must not cut an order in flight)
        while not stop():
            started = self.clock()
            try:
                await self.step(started)
            except Exception as exc:
                self.store.event("ATENCIÓN", f"Rápido: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(0.05, 1.0 - (self.clock() - started)))
        self.flush(self.clock() + 10)

    async def step(self, now: float) -> None:
        if self.conn.ready.is_set():
            for asset in self.assets:
                if asset not in self.conn.streams:
                    await self.conn.subscribe(asset)
        self.resolve_paper(now)
        self.resolve_brain(now)
        self.resolve_giro(now)
        self.resolve_cards(now)
        fired = {}
        for asset in self.assets:
            g = grid(self.ticks[asset], now)
            if not g or self.conn.price(asset) is None:
                continue
            if self.cfg["brain"] or self.cfg["giro"]:
                x = features(g)
                if x is not None:
                    self.latest[asset] = x
                    if self.cfg["brain"]:
                        self.samples.append((now + float(self.cfg["seconds"]), asset, g[-1], x, self.model.direction(x)[0]))
                    d = giro_direction(x) if self.cfg["giro"] else None
                    if d and asset not in self.giro_open:
                        self.giro_open[asset] = (now + float(self.cfg["seconds"]), d, g[-1])
                    if self.cfg["giro"] and self.cfg["giro_cards"]:
                        due = now + float(self.cfg["seconds"])
                        calls = {name: ("ARRIBA" if x[i] > 0 else "ABAJO") for name, i in GIRO.items() if x[i]}
                        both = giro_votes(x)
                        if both:
                            calls["2de3"] = both
                        for name, call in calls.items():
                            if (name, asset) not in self.card_open:
                                self.card_open[(name, asset)] = (due, call, g[-1])
            for rule in BASE:
                direction = decide(rule, g)
                if direction is None:
                    continue
                fired[(rule, asset)] = direction
                fired[(OPPOSITE[rule], asset)] = "ABAJO" if direction == "ARRIBA" else "ARRIBA"
                if (rule, asset) not in self.paper_open:
                    self.paper_open[(rule, asset)] = (now, direction, g[-1])
        self.choose(now)
        self.maybe_trade(now, fired)
        self.reports(now)
        self.flush(now)

    def card(self, name: str, asset: str) -> deque:
        return self.cards.setdefault((name, asset), deque(maxlen=60))

    def card_ok(self, name: str, asset: str, need: float, last: int = 40, minimum: int = 15) -> bool:
        rate, n = hit_rate(list(self.card(name, asset))[-last:])
        was = self.card_state.get((name, asset), True)
        # A margin both ways (23 Oct 2026: with 15-17 results the cards switched on and off every few seconds): off below
        # the break-even share minus 2 points, back on only above it plus 2 points.
        if n < minimum:
            ok = True
        elif was:
            ok = rate >= need - 0.02
        else:
            ok = rate >= need + 0.02
        if was != ok:
            self.card_state[(name, asset)] = ok
            what = f"{GIRO_LABEL[name]} en {asset.replace('_otc', ' OTC')}"
            self.store.event("RÁPIDO", (f"Giro: vuelve a contar {what} ({rate:.0%} de {n})" if ok else
                                        f"Giro: deja de contar {what} por ahora ({rate:.0%} de {n}; hace falta {need:.1%})"))
        return ok

    def resolve_cards(self, now: float) -> None:
        for key, (due, call, price) in list(self.card_open.items()):
            if now < due:
                continue
            del self.card_open[key]
            close = self.conn.price(key[1])
            if close is None:
                continue
            o = outcome(call, price, close)
            self.card(*key).append(o)
            if key[0] == "2de3":
                self.giro_outcomes.append(o)
            self.paper_rows.append(f"giro_{key[0]},{key[1]},{call},{due - float(self.cfg['seconds']):.3f},{price},{close},{o}\n")

    def resolve_giro(self, now: float) -> None:
        for asset, (due, direction, price) in list(self.giro_open.items()):
            if now < due:
                continue
            del self.giro_open[asset]
            close = self.conn.price(asset)
            if close is None:
                continue
            o = outcome(direction, price, close)
            self.giro_outcomes.append(o)
            self.paper_rows.append(f"giro,{asset},{direction},{due - float(self.cfg['seconds']):.3f},{price},{close},{o}\n")

    def resolve_brain(self, now: float) -> None:
        keep = []
        for due, asset, price, x, call in self.samples:
            if now < due:
                keep.append((due, asset, price, x, call))
                continue
            close = self.conn.price(asset)
            if close is None or close == price:
                continue
            up = close > price
            self.model.learn(x, up)
            self.brain_outcomes.append(1 if (call == "ARRIBA") == up else -1)
        self.samples = keep[-2000:]
        if now - self.last_save >= 60:
            self.last_save = now
            self.store.put("cerebro_pesos", json.dumps([round(w, 5) for w in self.model.w]))

    def resolve_paper(self, now: float) -> None:
        seconds = float(self.cfg["seconds"])
        for key, (opened, direction, price) in list(self.paper_open.items()):
            if now < opened + seconds:
                continue
            del self.paper_open[key]
            close = self.conn.price(key[1])
            if close is None:
                continue
            o = outcome(direction, price, close)
            self.paper[key[0]].append(o)
            self.paper[OPPOSITE[key[0]]].append(-o)
            self.paper_rows.append(f"{key[0]},{key[1]},{direction},{opened:.3f},{price},{close},{o}\n")

    def breakeven(self) -> float:
        """Share of wins needed not to lose at the pairs' current payout (92 % → 52.1 %)."""
        pays = [p for p in (self.payout(a) for a in self.assets) if p and p >= float(self.cfg["min_payout"])]
        return 1 / (1 + (statistics.median(pays) if pays else 92.0) / 100)

    def sureness(self, rule: str, need: float) -> float:
        """The Monte Carlo confidence of `rule` on its last `window` results, recomputed only when they changed."""
        recent = tuple(list(self.paper[rule])[-int(self.cfg["window"]):])
        key = (recent, round(need, 4))
        cached = self.sure.get(rule)
        if cached and cached[0] == key:
            return cached[1]
        value = confidence(recent, need)
        self.sure[rule] = (key, value)
        return value

    def choose(self, now: float) -> None:
        """The rule that trades live: the best recent paper score, and only while it wins at least the break-even share
        (23 Oct 2026: the first rule to warm up won 30 % on paper; trading it would only feed the brake)."""
        if self.cfg["giro"]:
            self.active = "giro"
            return
        if self.cfg["brain"]:
            self.active = "cerebro"
            return
        window, warmup = int(self.cfg["window"]), int(self.cfg["warmup"])
        best, best_rate, best_n = None, -1.0, 0
        for rule in RULES:
            rate, n = hit_rate(list(self.paper[rule])[-window:])
            if rate is not None and n >= warmup and rate > best_rate:
                best, best_rate, best_n = rule, rate, n
        if self.cfg["continuous"]:
            best = best or "ema"
            if best != self.active:
                before, self.active = self.active, best
                self.store.event("RÁPIDO", f"Regla preferida: {LABEL[best]}" + (f" (acierta {best_rate:.0%} de {best_n} en papel)"
                                 if best_n else " (aún sin datos)") + (f"; antes {LABEL[before]}" if before else ""))
            return
        if best is None:
            return
        need = self.breakeven()
        if best_rate < need:
            if self.active is not None or not getattr(self, "paused_told", False):
                self.active, self.paused_told = None, True
                self.store.event("RÁPIDO", f"En pausa: ninguna regla acierta lo suficiente en papel (la mejor, {LABEL[best]}, "
                                           f"{best_rate:.0%} de {best_n}; hace falta {need:.1%}). Sigue midiendo cada segundo.")
            return
        self.paused_told = False
        if best != self.active:
            before = self.active
            self.active = best
            self.store.event("RÁPIDO", f"Regla en vivo: {LABEL[best]} (acierta {best_rate:.0%} en papel en sus últimas "
                                       f"{best_n} señales; hace falta {need:.1%})" + (f"; antes {LABEL[before]}" if before else ""))

    def maybe_trade(self, now: float, fired: dict) -> None:
        c = self.cfg
        if self.trading or now < self.next_at or self.active is None or not self.conn.ready.is_set():
            return
        if getattr(self, "stop_check", None) and self.stop_check():
            return
        net, trades, stopped = self.day(day_of(now))
        if stopped:
            return
        if c["giro"]:
            self.trade_giro()
            return
        if c["continuous"]:
            self.trade_now(fired)
            return
        need = self.breakeven()
        sure = self.sureness(self.active, need)
        if sure < float(c["confidence"]):
            # Monte Carlo before the bet: the rule's edge could still be luck, so this bet is skipped (it keeps measuring).
            if not self.doubt_told:
                self.doubt_told = True
                self.store.event("RÁPIDO", f"Monte Carlo antes de apostar: {LABEL[self.active]} supera el {need:.1%} solo en "
                                           f"{sure:.0%} de 1000 simulaciones (pide {float(c['confidence']):.0%}); espera más datos.")
            return
        self.doubt_told = False
        options = []
        for (rule, asset), direction in fired.items():
            pay = self.payout(asset)
            if rule == self.active and pay is not None and pay >= float(c["min_payout"]):
                options.append((pay, asset, direction))
        if not options:
            return
        pay, asset, direction = max(options)
        self.trading = True
        self.store.put("rapido_operando", 1)
        task = asyncio.get_running_loop().create_task(self.trade(self.active, asset, direction, pay))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def trade_giro(self) -> None:
        """Option B: an order only where the reversal votes point one way now; the best-paying such pair."""
        c = self.cfg
        options = []
        need = self.breakeven()
        for asset in self.assets:
            pay, x = self.payout(asset), self.latest.get(asset)
            if int(c["giro_votes"]) >= 2:
                if c["giro_cards"]:
                    allowed = [name for name in GIRO if self.card_ok(name, asset, need)]
                    if not self.card_ok("2de3", asset, need, last=30):
                        continue
                else:
                    allowed = list(GIRO)
                d = giro_votes(x, allowed)
            else:
                d = giro_direction(x)
            if d and pay is not None and pay >= float(c["min_payout"]):
                options.append((pay, asset, d))
        if not options:
            return
        pay, asset, direction = max(options)
        self.trading = True
        self.store.put("rapido_operando", 1)
        task = asyncio.get_running_loop().create_task(self.trade("giro", asset, direction, pay))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def trade_now(self, fired: dict) -> None:
        """Continuous mode: one order now on the best-paying pair, in the preferred rule's direction when it fires, else
        the better-scored of the trend of the moment and its opposite (that one always has a direction)."""
        c = self.cfg
        if c["brain"]:
            options = []
            for asset in self.assets:
                pay, x = self.payout(asset), self.latest.get(asset)
                if pay is None or pay < float(c["min_payout"]) or x is None:
                    continue
                direction, p_up = self.model.direction(x)
                options.append((pay, abs(p_up - 0.5), asset, direction))
            if not options:
                return
            pay, _, asset, direction = max(options)
            self.trading = True
            self.store.put("rapido_operando", 1)
            task = asyncio.get_running_loop().create_task(self.trade("cerebro", asset, direction, pay))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            return
        window = int(c["window"])
        trend = max(("ema", "contra_ema"), key=lambda r: hit_rate(list(self.paper[r])[-window:])[0] or 0.5)
        options = []
        for asset in self.assets:
            pay = self.payout(asset)
            if pay is None or pay < float(c["min_payout"]):
                continue
            for rule in (self.active, trend):
                direction = fired.get((rule, asset))
                if direction:
                    options.append((pay, rule == self.active, asset, rule, direction))
                    break
        if not options:
            return
        pay, _, asset, rule, direction = max(options)
        self.trading = True
        self.store.put("rapido_operando", 1)
        task = asyncio.get_running_loop().create_task(self.trade(rule, asset, direction, pay))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def trade(self, rule: str, asset: str, direction: str, pay: float) -> None:
        c, st = self.cfg, self.store
        stake, seconds = float(c["stake"]), int(c["seconds"])
        try:
            try:
                opened = await self.conn.open_order(asset, direction, stake, seconds)
            except Exception as exc:
                if seconds < MIN_SECONDS:
                    # 23 Oct 2026: 3-s orders were asked for; if Pocket refuses them the strategy moves to its shortest
                    # accepted expiry and starts that expiry's own paper scores.
                    self.flush(self.clock() + 10)
                    self.cfg["seconds"] = MIN_SECONDS
                    self.paper = {r: deque(maxlen=400) for r in RULES}
                    self.paper_open.clear()
                    self.sure.clear()
                    self.active = None
                    self.load_paper()
                    st.event("RÁPIDO", f"Pocket no aceptó operaciones de {seconds} s ({exc}); se pasa a {MIN_SECONDS} s y se califica "
                                       "de nuevo en papel a ese tiempo.")
                    self.next_at = self.clock() + 1
                    return
                self.failures += 1
                st.event("ATENCIÓN", f"Rápido: Pocket no abrió {direction} {asset} ${stake:g} a {seconds} s ({exc}).")
                self.next_at = self.clock() + 30 * self.failures
                return
            self.failures = 0
            open_price = float(opened.get("openPrice") or self.conn.price(asset) or 0)
            try:
                deal = await self.conn.result(opened["id"], seconds + 60)
                net = deal_net(deal)
                close = float(deal.get("closePrice", 0))
                pay = float(deal.get("percentProfit", pay))
            except Exception as exc:
                st.event("ATENCIÓN", f"Rápido: no llegó el resultado de {asset} ({exc}); no se anota.")
                return
            day = day_of(self.clock())
            day_net, trades, stopped = self.day(day)
            day_net, trades = round(day_net + net, 2), trades + 1
            target, brake = float(c["target"] or 0), float(c["loss_stop"] or 0)  # 0 = off (user, 23 Oct 2026)
            stop = stopped or ("meta" if target > 0 and day_net >= target - 1e-9 else
                               "freno" if brake < 0 and day_net <= brake + 1e-9 else None)
            with st.db:
                st.db.execute("INSERT INTO rapido_trades(rule,asset,direction,opened,open_price,close_price,stake,payout,net,day) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)", (rule, asset, direction, self.clock(), open_price, close, stake, pay, net, day))
                st.db.execute("INSERT INTO rapido_days(day,net,trades,stopped) VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
                              "net=excluded.net, trades=excluded.trades, stopped=excluded.stopped", (day, day_net, trades, stop))
            self.live_outcomes.append(1 if net > 0 else -1 if net < 0 else 0)
            st.event("RÁPIDO OP", f"{asset} {direction} ${stake:g} {seconds} s ({LABEL[rule]}) · {open_price} → {close} · "
                                  f"{money(net)} · día {money(day_net)} en {trades} operaciones")
            if stop and not stopped:
                st.event("RÁPIDO", f"Día {'en la META' if stop == 'meta' else 'en el FRENO'}: {money(day_net)} en {trades} "
                                   "operaciones. Sin más operaciones rápidas hasta mañana (hora de Ciudad de México).")
        finally:
            self.trading = False
            self.store.put("rapido_operando", 0)
            self.next_at = max(self.next_at, self.clock() + float(c["pause"]))

    # ---------- reports ----------
    def reports(self, now: float) -> None:
        c = self.cfg
        if now - self.last_report >= 60 * float(c["report_minutes"]):
            self.last_report = now
            net, trades, stopped = self.day(day_of(now))
            rates = []
            for rule in RULES:
                rate, n = hit_rate(list(self.paper[rule])[-int(c["window"]):])
                rates.append(f"{LABEL[rule]} {rate:.0%} ({n})" if rate is not None else f"{LABEL[rule]} sin datos")
            if c["giro"]:
                gr, gn = hit_rate(self.giro_outcomes)
                rates.insert(0, f"giro en papel {gr:.0%} ({gn})" if gr is not None else "giro sin señales aún")
                if c["giro_cards"]:
                    cards = []
                    for (name, asset), q in sorted(self.cards.items()):
                        rate, n = hit_rate(list(q)[-40:])
                        if rate is not None:
                            cards.append(f"{GIRO_LABEL[name]} {asset.replace('_otc', '')} {rate:.0%}({n})")
                    rates.insert(1, "marcadores: " + ", ".join(cards) if cards else "marcadores aún vacíos")
            if c["brain"]:
                brain, bn = hit_rate(self.brain_outcomes)
                rates.insert(0, f"cerebro {brain:.0%} ({bn}) · pesa más: {self.model.explain()}" if brain is not None
                             else "cerebro aprendiendo")
            live, n = hit_rate(self.live_outcomes)
            pays = {a: self.payout(a) for a in self.assets}
            self.store.event("RÁPIDO", f"Resumen: hoy {trades} operaciones en demo, {money(net)}"
                                       + (f", aciertos {live:.0%} de {n}" if live is not None else "")
                                       + (f" · día {'en la meta' if stopped == 'meta' else 'en el freno'}" if stopped else "")
                                       + f" · en papel: {'; '.join(rates)} · pago actual {pays} · regla en vivo "
                                       f"{LABEL[self.active] if self.active else 'aún ninguna (juntando datos)'}.")
        if now - self.last_mc >= 60 * float(c["report_minutes"]) and self.active and not self.busy_tyl():
            self.last_mc = now
            asyncio.get_running_loop().create_task(self.monte_carlo_report())

    async def monte_carlo_report(self) -> None:
        c = self.cfg
        rule = self.active
        if rule == "giro" and int(c["giro_votes"]) >= 2 and c["giro_cards"]:
            # what trades: two agreeing signals, on the pairs whose cards allow it
            pool = [o for (name, _), q in self.cards.items() if name == "2de3" for o in q]
            label = "giro con 2 de 3 señales"
        else:
            pool = (list(self.giro_outcomes) if rule == "giro" else list(self.brain_outcomes) if rule == "cerebro"
                    else list(self.paper[rule]))
            label = LABEL[rule]
        # only the pairs it may trade now (23 Oct 2026: the median of every pair, 44 % at night, made the result look worse
        # than what the bot can actually do: it never trades below min_payout)
        pays = [p for p in (self.payout(a) for a in self.assets) if p and p >= float(c["min_payout"])]
        if not pays:
            self.store.event("MONTE CARLO", f"{LABEL.get(rule, rule)}: ahora ningún par paga {c['min_payout']}% o más; "
                                            "el bot no opera y no hay día que simular.")
            return
        payout = statistics.median(pays) / 100
        breakeven = 1 / (1 + payout)
        # with no daily target / brake (23 Oct 2026) the simulated days use ±20 USD as a yardstick
        target = float(c["target"] or 0) or 20.0
        brake = float(c["loss_stop"] or 0) or -20.0
        yardstick = "" if c["target"] or c["loss_stop"] else " (de referencia; el bot no tiene meta ni freno)"
        r = await asyncio.to_thread(monte_carlo, pool, payout, float(c["stake"]), target, brake)
        if r is None:
            return
        rate, n = hit_rate(pool)
        self.store.event("MONTE CARLO", f"{label}: acierta {rate:.0%} de {n} en papel (para no perder hace falta "
                                        f"{breakeven:.1%} con pago {payout:.0%}). En 2000 días simulados de hasta 300 operaciones: "
                                        f"llega a {money(target)} {r['meta']:.0%} · cae a {money(brake)} {r['freno']:.0%} · ninguno "
                                        f"{r['ninguno']:.0%}{yardstick} · día típico {money(r['mediana'])}.")
