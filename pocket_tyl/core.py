"""Pure rules of Camino 1: Tyl's message formats, the short martingale (J2 → J3) with its shared book and the daily stops. No I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re

GROUP_TZ = timezone(timedelta(hours=-3))  # Tyl posts in GMT-3: its day starts at 21:00 Mexico City
MEXICO = timezone(timedelta(hours=-6))
# The pair line may start with 💵, 🪙 or any other emoji (user, 21 Oct 2026); only the "XXX/YYY OTC" matters.
SIGNAL = re.compile(r"(?:^|\n)[^\w\n]*(?P<pair>[A-Z]{3}/[A-Z]{3})\s*OTC")
ENTRY = re.compile(r"Entrada a las\s*(\d{1,2}):(\d{2})")
GALE = re.compile(r"(\d)\s*\S*\s*MARTINGALA A LAS\s*(\d{1,2}):(\d{2})", re.I)


@dataclass(frozen=True)
class TylSignal:
    message: int
    pair: str
    direction: str  # "ARRIBA" / "ABAJO"
    entry_at: float  # epoch seconds of the 1st candle
    gale1_at: float  # 1st martingale (player 2)
    gale2_at: float  # 2nd martingale (player 3)
    day: str  # the group's day, YYYY-MM-DD (GMT-3)


def parse_signal(message: int, text: str, posted: float) -> TylSignal | None:
    """Tyl's signal: "💵 GBP/JPY OTC · Entrada a las 00:05 · 🟢 ARRIBA · 1️⃣ MARTINGALA A LAS 00:10 · 2️⃣ … 00:15"."""
    pair = SIGNAL.search(text or "")
    entry = ENTRY.search(text or "")
    if not pair or not entry:
        return None
    upper = text.upper()
    direction = "ARRIBA" if ("ARRIBA" in upper or "🟢" in text) else "ABAJO" if ("ABAJO" in upper or "🔴" in text) else ""
    if not direction:
        return None
    seen = datetime.fromtimestamp(posted, GROUP_TZ)

    def at(hh, mm):
        when = seen.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if when < seen - timedelta(hours=2):
            when += timedelta(days=1)  # posted before midnight for a candle after it
        return when

    first = at(*entry.groups())
    gales = {int(n): at(h, m) for n, h, m in GALE.findall(text)}
    g1 = gales.get(1, first + timedelta(minutes=5))
    g2 = gales.get(2, g1 + timedelta(minutes=5))
    return TylSignal(message, pair["pair"], direction, first.timestamp(), g1.timestamp(), g2.timestamp(),
                     first.date().isoformat())


def parse_result(text: str) -> str | None:
    """'D' direct win, 'G1' win at the 1st martingale, 'G2' at the 2nd, 'L' loss; None = not a result post."""
    upper = (text or "").upper()
    if "INFORME" in upper:
        return None  # the daily report lists every result
    if "VICTORIA DIRECTA" in upper:
        return "D"
    if re.search(r"VICTORIA EN 1", upper):
        return "G1"
    if re.search(r"VICTORIA EN 2", upper):
        return "G2"
    if "PÉRDIDA" in upper or "PERDIDA" in upper:
        return "L"
    return None


@dataclass
class Book:
    """The signals' shared book (user, 22 Oct 2026, "martingala corta"): one debt for every signal, booked once a signal
    is over with its whole result (J2 + J3), and the level the next signals play at: 0 = normal, 1 = recovery (the top,
    never higher). `player` is 0 for the shared book."""
    player: int = 0
    debt: float = 0.0
    step: int = 0


def settle_net(book: Book, net: float, levels: int) -> None:
    """A signal's whole result (its J2 + J3, as the broker paid them) on the shared book: a win pays the debt down (back
    to the normal level once it is paid), a loss adds to it and goes up one level (the top level repeats), a refund
    (same price) changes nothing."""
    if net > 0:
        book.debt -= net
        if book.debt <= 1e-9:
            book.debt, book.step = 0.0, 0
    elif net < 0:
        book.debt += -net
        book.step = min(book.step + 1, levels - 1)
    book.debt = round(book.debt, 2)


@dataclass
class Plan:
    """Martingala corta (user, 22 Oct 2026): the 1st candle is only watched; J2 (1st martingale) then J3 (2nd) in the same
    signal, at the normal level (J2 2.00 → J3 4.40: a J3 win at 92 % pays 4.05, the 2.00 of J2 and 2.05 more) or, while
    the book owes, one recovery level (J2 4.00 → J3 8.80), never higher. Replaces the separate books 12 → 26.40 → 42."""
    stakes: dict = field(default_factory=lambda: {2: (2.0, 4.0), 3: (4.4, 8.8)})  # player → stake per level
    payout: float = 0.92
    target: float = 20.0  # USD per group day: off at once when reached
    loss_stop: float = -40.0  # USD per group day: safety brake (0 = off)
    players: tuple[int, ...] = (2, 3)

    @property
    def levels(self) -> int:
        return len(self.stakes[self.players[0]])

    def stake(self, player: int, step: int) -> float:
        row = self.stakes[player]
        return round(float(row[min(step, len(row) - 1)]), 2)


def plays_of(result: str, first: int = 2) -> list[tuple[int, bool]]:
    """(player, won) in the order they play: player 2 after a lost 1st candle, player 3 after a lost 1st martingale; with
    first = 1 (user, 23 Oct 2026: "jugador 1 y 2") player 1 bets the 1st candle and player 2 the 1st martingale."""
    if first == 1:
        return {"D": [(1, True)], "G1": [(1, False), (2, True)], "G2": [(1, False), (2, False)],
                "L": [(1, False), (2, False)]}[result]
    return {"D": [], "G1": [(2, True)], "G2": [(2, False), (3, True)], "L": [(2, False), (3, False)]}[result]


def day_stopped(net: float, plan: Plan) -> str | None:
    if net >= plan.target - 1e-9:
        return "meta"
    if plan.loss_stop and net <= plan.loss_stop + 1e-9:
        return "freno"
    return None


def settle_signal(result: str, book: Book, day_net: float, plan: Plan):
    """Book one signal's published result (paper mode). Returns ([(player, stake, won, net)], new day net, stop reason or
    None). The signal plays at the book's level when it starts (J2 and J3 at the same level); the day's stop is checked
    before every play, like the live order; the book takes the signal's whole result at the end."""
    ops, stop = [], day_stopped(day_net, plan)
    step = book.step
    for player, won in plays_of(result, plan.players[0]):
        if stop or player not in plan.players:
            if stop:
                break
            continue
        stake = plan.stake(player, step)
        net = round(stake * plan.payout, 2) if won else -stake
        day_net = round(day_net + net, 2)
        ops.append((player, stake, won, net))
        stop = day_stopped(day_net, plan)
    if ops:
        settle_net(book, round(sum(op[3] for op in ops), 2), plan.levels)
    return ops, day_net, stop


def mexico(ts: float) -> str:
    return datetime.fromtimestamp(ts, MEXICO).strftime("%H:%M:%S")


def group_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, GROUP_TZ).date().isoformat()
