"""The second-exact trigger of Camino 1 (user, 21 Oct 2026). For one Tyl signal, with a live price of its OTC pair:

  entry_at          (e.g. 10:25:00.000)  record the 1st candle's open, no stake
  gale1_at - 0.2 s  (10:29:59.800)       compare: the 1st candle WON → the signal is discarded
  gale1_at          (10:30:00.000)       it lost → player 2's order goes out at once (no wait for Telegram)
  gale2_at - 0.2 s  (10:34:59.800)       compare against player 2's open: it WON → done
  gale2_at          (10:35:00.000)       it lost → player 3's order goes out

A tie counts as a loss for the signal's direction (user's rule: for ABAJO, price >= open is a loss). The price source and
the order sender are plugged in by the broker connector; without them nothing is sent (paper mode). No I/O here."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Awaitable, Callable

from .core import Plan, TylSignal

LOOK = 0.2  # seconds before the next candle when the previous one is judged (user: :59.8)
SPIN = 0.15  # the last stretch before a deadline is waited by spinning (Windows asyncio timers overshoot by tens of ms)


def asset_code(pair: str) -> str:
    """Tyl's "AUD/NZD" → Pocket Option's "AUDNZD_otc"."""
    return pair.replace("/", "").upper() + "_otc"


def won(direction: str, open_price: float, price: float) -> bool:
    return price > open_price if direction == "ARRIBA" else price < open_price


class Clock:
    """Wall clock with an optional correction to the broker's server time (seconds to add to the PC clock)."""

    def __init__(self, offset: float = 0.0):
        self.offset = offset

    def now(self) -> float:
        return time.time() + self.offset

    async def until(self, deadline: float) -> float:
        while True:
            left = deadline - self.now()
            if left <= 0:
                return self.now()
            if left > SPIN:
                await asyncio.sleep(left - SPIN)
            else:
                while self.now() < deadline:
                    pass


@dataclass
class Step:
    at: float
    what: str
    detail: str


LATE = 5.0  # a signal seen this long after its 1st candle opened is not played (its open price is unknown)


async def run_signal(s: TylSignal, price: Callable[[str], float | None],
                     send: Callable[[int, str, str, float, int], Awaitable[dict]],
                     stake_of: Callable[[int], float], plan: Plan, clock: Clock, log: Callable[[Step], None],
                     allowed: Callable[[int, float], str | None] = lambda player, stake: None) -> list[tuple[int, float]]:
    """Plays one signal at the exact seconds. Returns [(player, stake)] sent. `send(player, asset, direction, stake,
    seconds)` returns the opened order ({"openPrice": …}); `stake_of(player)` is the player's stake at the signal's level
    (the caller fixes the level when J2 goes out and books the result). `allowed` → a reason not to play, or None."""
    asset = asset_code(s.pair)
    sent = []
    if clock.now() > s.entry_at + LATE:
        log(Step(clock.now(), "tarde", "La señal llegó después de abrir la 1ª vela: no se juega."))
        return sent
    if 1 in plan.players:
        # Jugador 1 (user, 23 Oct 2026): the 1st candle is bet at its exact second, not only watched. Its price is checked
        # at that second, as before (23 Oct 20:48, GBP/USD: checked 80 s early, before the pair's first tick, it skipped).
        await clock.until(s.entry_at - 1.0)
        if price(asset) is None:
            log(Step(clock.now(), "sin precio", "Sin precio del par: no se juega esta señal."))
            return sent
        stake = stake_of(1)
        reason = allowed(1, stake)
        if reason:
            log(Step(clock.now(), "freno", f"J1 no entra: {reason}"))
            return sent
        await clock.until(s.entry_at)
        try:
            order = await send(1, asset, s.direction, stake, 300)
        except Exception as exc:
            log(Step(clock.now(), "error", f"J1 no se pudo abrir: {exc}"))
            return sent
        sent.append((1, stake))
        log(Step(clock.now(), "disparo", f"J1 {s.direction} {asset} {stake:.2f} USD · abrió en {order.get('openPrice')}"))
        previous = float(order.get("openPrice") or price(asset) or 0)
    else:
        await clock.until(s.entry_at)
        open1 = price(asset)
        log(Step(clock.now(), "apertura", f"{asset} 1ª vela abre en {open1}"))
        if open1 is None:
            log(Step(clock.now(), "sin precio", "Sin precio del par: no se juega esta señal."))
            return sent
        previous = open1
    for player, at in ((2, s.gale1_at), (3, s.gale2_at)):
        await clock.until(at - LOOK)
        now_price = price(asset)
        if now_price is None:
            log(Step(clock.now(), "sin precio", f"Sin precio antes de J{player}: no se dispara."))
            return sent
        if won(s.direction, previous, now_price):
            log(Step(clock.now(), "ganó", f"La vela anterior ganó ({now_price} vs {previous}): J{player} no entra."))
            return sent
        if player not in plan.players:
            return sent
        stake = stake_of(player)
        reason = allowed(player, stake)
        if reason:
            log(Step(clock.now(), "freno", f"J{player} no entra: {reason}"))
            return sent
        await clock.until(at)
        try:
            order = await send(player, asset, s.direction, stake, 300)
        except Exception as exc:
            log(Step(clock.now(), "error", f"J{player} no se pudo abrir: {exc}"))
            return sent
        sent.append((player, stake))
        log(Step(clock.now(), "disparo", f"J{player} {s.direction} {asset} {stake:.2f} USD · abrió en {order.get('openPrice')}"))
        previous = float(order.get("openPrice") or price(asset) or now_price)  # the next candle opens at our fill
    return sent
