"""Candles of every asset the Lector bridge streams (user, 16 Nov 2026: «el escáner tiene que estar activo en los 4 al
mismo tiempo… e ir brincando»). The panel reads ONE chart off the screen; to analyse several pairs at once it asks the
bot, which already receives the price of all of them, for their 15-second candles (`GET /velas?pares=A,B,C,D`).
Only listens: nothing here opens an order. A pair seen for the first time is filled from today's recorded ticks
(ticks_otc\\<day>\\<pair>.csv), so the panel does not have to wait half an hour for history."""
from __future__ import annotations

from collections import deque
import datetime as dt
from pathlib import Path

MX = dt.timezone(dt.timedelta(hours=-6))
TAIL_BYTES = 400_000  # about 50 minutes of one pair's ticks


class CandleBook:
    def __init__(self, seconds: int = 15, keep: int = 130):
        self.seconds, self.keep = int(seconds), int(keep)
        self.books: dict[str, list] = {}  # symbol → [slot, deque of closed [o, h, l, c], forming [o, h, l, c], last tick local time]
        self.first: dict[str, float] = {}  # symbol → server time of the first live tick
        self.filled: set[str] = set()

    def on_tick(self, symbol, server_ts, price, local_ts=None) -> None:
        symbol, ts, price = str(symbol), float(server_ts), float(price)
        slot = int(ts // self.seconds)
        book = self.books.get(symbol)
        if book is None:
            self.books[symbol] = [slot, deque(maxlen=self.keep), [price, price, price, price], local_ts]
            self.first.setdefault(symbol, ts)
            return
        if slot < book[0]:
            return  # a late tick of a candle already closed
        if slot != book[0]:
            book[1].append(book[2])
            book[0], book[2] = slot, [price, price, price, price]
        else:
            forming = book[2]
            if price > forming[1]:
                forming[1] = price
            if price < forming[2]:
                forming[2] = price
            forming[3] = price
        book[3] = local_ts

    def backfill(self, symbol: str, rows) -> int:
        """Puts the candles of `rows` ((server_ts, price), oldest first) BEFORE the live ones. Returns how many were added."""
        symbol = str(symbol)
        self.filled.add(symbol)
        first = self.first.get(symbol)
        old = CandleBook(self.seconds, self.keep)
        for ts, price in rows:
            if first is not None and ts >= first:
                break
            old.on_tick(symbol, ts, price)
        past = old.books.get(symbol)
        if past is None:
            return 0
        live = self.books.get(symbol)
        if live is None:
            self.books[symbol] = past
            return len(past[1])
        before = list(past[1])
        first_slot = int(first // self.seconds)
        if past[0] == first_slot:  # the recorded ticks reach into the candle the live ones started in: one candle
            target = live[1][0] if live[1] else live[2]
            target[0] = past[2][0]
            target[1] = max(target[1], past[2][1])
            target[2] = min(target[2], past[2][2])
        else:
            before.append(past[2])
        live[1] = deque(before + list(live[1]), maxlen=self.keep)
        return len(before)

    def fill_from_disk(self, symbol: str, root) -> int:
        """First request of a pair: its history from today's tick file, if there is one."""
        if symbol in self.filled:
            return 0
        self.filled.add(symbol)
        path = Path(root) / "ticks_otc" / dt.datetime.now(MX).strftime("%Y-%m-%d") / f"{symbol}.csv"
        rows = []
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - TAIL_BYTES))
                lines = fh.read().decode("ascii", "replace").split("\n")
            for line in lines[1:] if size > TAIL_BYTES else lines:  # the first line of a cut tail is half a line
                parts = line.split(",")
                if len(parts) >= 2:
                    try:
                        rows.append((float(parts[0]), float(parts[1])))
                    except ValueError:
                        continue
        except OSError:
            return 0
        rows.sort(key=lambda r: r[0])
        return self.backfill(symbol, rows)

    def get(self, symbol: str, count: int = 120):
        """(closed candles oldest first, forming candle, server time the forming candle opened, local time of its last tick)."""
        book = self.books.get(str(symbol))
        if book is None:
            return None
        closed = list(book[1])[-int(count):]
        return closed, list(book[2]), book[0] * self.seconds, book[3]
