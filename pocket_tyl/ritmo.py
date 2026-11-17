"""RITMO DE LA VELA (16 Nov 2026) — continuous follow-up of the price, tick by tick, on the Pocket Option DEMO.

The user's order: «tienes que seguir el ritmo de la vela… esto casi no necesita de IA, necesita de seguimiento
continuo». Read in his own manual trades (84 deals, 15 Nov): he won when he entered AGAINST the move of the previous
seconds (19 W / 9 L) and not when he chased it (16 W / 17 L). Rule proposed by Argon (Gemini 4) from those numbers:

  - impulse: up to the PREVIOUS tick the price had moved more than K sigmas in LOOK seconds (sigma = typical size of
    the LOOK-second moves of the last SIGMA_WINDOW seconds, measured around zero on a regular half-second grid);
  - brake: the latest tick goes against that impulse;
  - then enter AGAINST the impulse for 3 s, only between seconds 2 and 11 of the 15-s candle, entries at least GAP
    seconds apart and at most MAX_PER_CANDLE in one candle.

Argon reviewed the first version and found four real faults, fixed here: ticks lost by polling (now every tick comes
through the connection's listener), the brake tick diluting the impulse (now measured to the extreme before the
brake), sigma inflated by busy moments (now sampled on a grid) and sigma measured around the drift (now around zero).

NOT proven: on the exact prices of 15 Nov the idea gave 21 W / 19 L of 44 entries (2 sigma) on paper — too few.
DEMO only: this module decides a side, the bridge (lector.py) places the order with a fixed stake (level 0, no
martingale). It never touches a real account (the bridge refuses POCKET_TYL_REAL).
"""
from __future__ import annotations

import asyncio
import math
from collections import deque

LOOK = 1.5  # seconds of the impulse
SIGMA_WINDOW = 300.0  # seconds of history for sigma
SAMPLE_EVERY = 0.5  # one sample of the LOOK-second move per this many seconds (regular grid)
K = 2.0  # impulse threshold, in sigmas
MIN_SAMPLES = 120  # samples needed before sigma means anything (one minute)
CANDLE = 15.0  # seconds of the chart's candle
WINDOW = (2.0, 11.0)  # seconds of the candle in which an entry may start (a 3-s trade ends inside the candle)
GAP = 3.5  # seconds between entries (the bridge plays one trade at a time; a trade lasts 3 s)
MAX_PER_CANDLE = 3
MIN_PAYOUT = 80.0  # below this payout no entries (16 Nov: at 90 it paused after every entry — Pocket was paying 82–89 %; at 80 % one needs 55.6 % to break even)
PAYOUT_PAUSE = 120.0  # seconds without entries after a low payout
SECONDS = 3
# MODO «COLOR» (16 Nov 2026, the user's own description of what he does by hand): «revisar el color de la
# vela; si es verde puede continuar verde… seguimiento en milisegundos para saber hacia dónde va… no entrar
# cuando la vela es muy chica». Enter WITH the colour of the running candle when its body is at least
# BODY_K sigmas and both the last second and the last tick go towards that colour.
# On paper (15–16 Nov prices): 343 W / 338 L of 713 entries at BODY_K 1 (50.4 %) — no edge shown.
BODY_K = 1.0
RECENT = 1.0  # seconds of «where is it going right now»
OPEN_SEEN = 1.5  # a candle's open is only known if a tick arrived in its first seconds
# MODE «color3» — THE THREE FROZEN RULES (16 Nov 2026 08:05, agreed with Argon after the losses of the live
# colour mode on AUD/CAD OTC: sells against the rise 45 %, 2nd–3rd entry of a candle 46 %, stretched body 43 %):
#   1. one entry per candle;  2. body between BODY_K and BODY_MAX_K sigmas;  3. only with the slope of the last
#   SLOPE seconds. Do NOT tune them on the data they produce: they are judged on TARGET new trades and need
#   more than NEEDED % of the decided ones (z > 1.96 over the 52.1 % break-even at 92 % payout).
BODY_MAX_K = 2.0
SLOPE = 600.0  # seconds of «where has it been going»
SLOPE_MIN = 300.0  # with less history than this there is no slope yet: no entries
SLOPE_STEP = 5.0  # one price kept per this many seconds for the slope
TARGET = 500
NEEDED = 55.5


class Ritmo:
    """Feed it every tick (server seconds, price); it answers 'up' / 'down' when the rule fires, else None."""

    def __init__(self, k: float = K, mode: str = "contra", body_k: float = BODY_K):
        self.k = float(k)
        self.mode = mode  # 'contra' = against an impulse that brakes · 'color' = with the candle's colour
        self.body_k = float(body_k)
        self.slow: deque[tuple[float, float]] = deque()  # one price every SLOPE_STEP seconds, for the slope
        self.bucket = None  # the running 15-s candle and its open price (None if its start was not seen)
        self.open = None
        self.ticks: deque[tuple[float, float]] = deque()
        self.squares: deque[tuple[float, float]] = deque()  # (ts, move²) on the grid
        self.sum_squares = 0.0
        self.sampled_at = 0.0
        self.last_entry = 0.0
        self.candle = None
        self.in_candle = 0
        self.last = None  # what the last tick measured: dict(impulse, sigma, second), for the log

    def price_at(self, when: float) -> float | None:
        """The latest price at or before `when` (None if that is older than 1.1 s more, or not kept)."""
        found = None
        for t, p in self.ticks:
            if t <= when:
                found = (t, p)
            else:
                break
        return found[1] if found and when - found[0] <= 1.1 else None

    def tick(self, ts: float, price: float) -> str | None:
        if self.ticks and ts <= self.ticks[-1][0]:
            return None
        previous = self.ticks[-1] if self.ticks else None
        self.ticks.append((ts, price))
        if not self.slow or ts - self.slow[-1][0] >= SLOPE_STEP:
            self.slow.append((ts, price))
            while self.slow and ts - self.slow[0][0] > SLOPE + SLOPE_STEP:
                self.slow.popleft()
        bucket = int(ts // CANDLE)
        if bucket != self.bucket:
            self.bucket = bucket
            self.open = price if ts % CANDLE <= OPEN_SEEN else None
        while self.ticks and ts - self.ticks[0][0] > LOOK + 3.0:
            self.ticks.popleft()
        # Sigma: the LOOK-second move, sampled on a regular grid and measured around zero.
        if ts - self.sampled_at >= SAMPLE_EVERY:
            base = self.price_at(ts - LOOK)
            if base is not None:
                self.sampled_at = ts
                square = (price - base) ** 2
                self.squares.append((ts, square))
                self.sum_squares += square
        while self.squares and ts - self.squares[0][0] > SIGMA_WINDOW:
            self.sum_squares -= self.squares.popleft()[1]
        if len(self.squares) < MIN_SAMPLES or previous is None:
            return None
        sigma = math.sqrt(max(0.0, self.sum_squares) / len(self.squares))
        if self.mode in ("color", "color3"):
            return self.follow(ts, price, previous, sigma)
        # Impulse: what the price had moved, up to the tick BEFORE this one, in LOOK seconds.
        base = self.price_at(previous[0] - LOOK)
        if base is None:
            return None
        impulse = previous[1] - base
        second = ts % CANDLE
        self.last = dict(impulse=impulse, sigma=sigma, second=second)
        if sigma <= 0 or abs(impulse) <= self.k * sigma:
            return None
        if (price - previous[1]) * impulse >= 0:  # no brake yet: the impulse is still running
            return None
        if not WINDOW[0] <= second <= WINDOW[1] or ts - self.last_entry < GAP:
            return None
        candle = int(ts // CANDLE)
        if candle != self.candle:
            self.candle, self.in_candle = candle, 0
        if self.in_candle >= MAX_PER_CANDLE:
            return None
        return "down" if impulse > 0 else "up"  # against the impulse

    def follow(self, ts: float, price: float, previous: tuple[float, float], sigma: float) -> str | None:
        """Mode 'color': with the colour of the running candle, if it has a body and the price is going that way now."""
        second = ts % CANDLE
        if self.open is None or sigma <= 0:
            return None
        body = price - self.open
        self.last = dict(body=body, sigma=sigma, second=second)
        if abs(body) < self.body_k * sigma:  # a small candle says nothing yet
            return None
        side = 1 if body > 0 else -1
        frozen = self.mode == "color3"
        if frozen:
            if abs(body) > BODY_MAX_K * sigma:  # rule 2: the candle is already stretched
                return None
            if not self.slow or ts - self.slow[0][0] < SLOPE_MIN:  # rule 3 needs history
                return None
            if (price - self.slow[0][1]) * side <= 0:  # rule 3: against the slope of the last minutes
                return None
        ago = self.price_at(ts - RECENT)
        if ago is None or (price - ago) * side <= 0:  # in the last second it is not going towards the colour
            return None
        if (price - previous[1]) * side < 0:  # the very last tick turned against it
            return None
        if not WINDOW[0] <= second <= WINDOW[1] or ts - self.last_entry < GAP:
            return None
        candle = int(ts // CANDLE)
        if candle != self.candle:
            self.candle, self.in_candle = candle, 0
        if self.in_candle >= (1 if frozen else MAX_PER_CANDLE):  # rule 1: one entry per candle
            return None
        return "up" if side > 0 else "down"

    def entered(self, ts: float) -> None:
        self.last_entry = ts
        candle = int(ts // CANDLE)
        if candle != self.candle:
            self.candle, self.in_candle = candle, 0
        self.in_candle += 1


async def run(bridge, conn, asset_name: str, stop, log, k: float = K, sleep=asyncio.sleep, mode: str = "contra", body_k: float = BODY_K) -> None:
    """Follow the asset's price on the DEMO connection and hand each entry to the bridge (fixed stake, 3 s)."""
    ritmo = Ritmo(k, mode, body_k)
    queue: deque[tuple[str, float, float]] = deque()
    listeners = getattr(conn, "listeners", None)
    hook = (lambda asset, ts, price, _now=None: queue.append((asset, float(ts), float(price)))) if listeners is not None else None
    if hook is not None:
        listeners.append(hook)  # every tick, in order: none is lost between two turns of this loop
    seen = None
    paused_until = 0.0
    told = False
    tally = dict(win=0, loss=0, tie=0)  # results of this run's own entries (towards TARGET)

    def booked(task) -> None:
        try:
            answer = task.result()
        except Exception:
            return
        if not answer.get("ok") or answer.get("result") not in tally:
            return
        tally[answer["result"]] += 1
        done, decided = sum(tally.values()), tally["win"] + tally["loss"]
        if done % 25 == 0 or done == TARGET:
            rate = f"{100 * tally['win'] / decided:.1f} %" if decided else "—"
            log("RITMO", f"prueba de las tres reglas: {done} de {TARGET} · {tally['win']} G / {tally['loss']} P / {tally['tie']} E · "
                         f"{rate} (hace falta más de {NEEDED:g} %)")
    try:
        while not stop():
            await sleep(0.05)
            if not conn.ready.is_set():
                continue
            asset = bridge.asset_of(asset_name)
            if asset is None:
                continue
            if asset not in conn.streams:
                await conn.subscribe(asset)
            if hook is None:  # a connection without listeners: its latest price (ticks may be skipped)
                row = conn.prices.get(asset)
                if row and row[0] != seen:
                    seen = row[0]
                    queue.append((asset, float(row[0]), float(row[1])))
            side, at = None, None
            while queue:
                name, ts, price = queue.popleft()
                if name != asset:
                    continue
                fired = ritmo.tick(ts, price)
                side, at = (fired, ts) if not queue else (None, None)  # only the newest tick may open a trade
            if not told and len(ritmo.squares) >= MIN_SAMPLES:
                told = True
                rule = (f"TRES REGLAS CONGELADAS: 1 entrada por vela, cuerpo de {body_k:g} a {BODY_MAX_K:g} sigmas, a favor de la pendiente de {SLOPE / 60:g} min; "
                        f"las entradas empiezan cuando haya {SLOPE_MIN / 60:g} min de historia" if mode == "color3"
                        else f"vela con cuerpo ≥ {body_k:g} sigmas y precio yendo hacia su color → entro a favor del color, 3 s" if mode == "color"
                        else f"impulso > {k:g} sigmas + freno → entro en contra, 3 s")
                log("RITMO", f"{asset}: ya hay historia suficiente; sigo el precio tick a tick ({rule}).")
            if side is None or bridge.open_id is not None or at < paused_until:
                continue
            answer = await bridge.order(dict(asset=asset_name, dir=side, seconds=SECONDS, level=0, tag="ritmo"))
            if answer.get("ok"):
                ritmo.entered(at)
                task = getattr(bridge, "results", {}).get(str(answer.get("id")))
                if task is not None:
                    task.add_done_callback(booked)
                payout = float(answer.get("payout") or 0)
                if 0 < payout < MIN_PAYOUT:
                    paused_until = at + PAYOUT_PAUSE
                    log("RITMO", f"{asset}: Pocket pagó {payout:g} % (menos de {MIN_PAYOUT:g} %); sin entradas de ritmo durante {PAYOUT_PAUSE:g} s.")
    finally:
        if hook is not None and hook in listeners:
            listeners.remove(hook)
