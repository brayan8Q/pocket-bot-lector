"""Ritmo de la vela (pocket_tyl/ritmo.py): the tick rule and its hand-off to the DEMO bridge. No network."""
import asyncio

from pocket_tyl import ritmo
from pocket_tyl.ritmo import GAP, MAX_PER_CANDLE, MIN_SAMPLES, Ritmo


def warm(r: Ritmo, start: float = 1000.0, price: float = 7.0, n: int = 400) -> tuple[float, float]:
    """Calm market: the price wobbles ±0.001 every half second. Returns the last (ts, price)."""
    ts = start
    for i in range(n):
        ts = start + i * 0.5
        assert r.tick(ts, price + (0.001 if i % 2 else 0.0)) is None
    return ts, price + (0.001 if (n - 1) % 2 else 0.0)


def to_second(ts: float, second: float) -> float:
    """Next moment at that second of a 15-s candle."""
    base = ts - ts % 15
    target = base + second
    return target if target > ts else target + 15


def test_no_entry_before_there_is_history():
    r = Ritmo()
    for i in range(MIN_SAMPLES - 5):
        assert r.tick(1000 + i * 0.5, 7.0 + i * 0.01) is None  # even a wild run: sigma is not known yet


def test_impulse_plus_brake_enters_against_the_impulse():
    r = Ritmo()
    ts, price = warm(r)
    t = to_second(ts, 4.0)
    assert r.tick(t - 2.0, price) is None
    assert r.tick(t - 1.5, price) is None
    assert r.tick(t - 1.0, price + 0.010) is None  # impulse up, still running: no brake yet
    assert r.tick(t - 0.5, price + 0.020) is None
    assert r.tick(t, price + 0.018) == "down"  # it braked: sell against the rise
    # And the mirror: a fall that brakes → buy.
    r2 = Ritmo()
    ts, price = warm(r2)
    t = to_second(ts, 6.0)
    r2.tick(t - 2.0, price)
    r2.tick(t - 1.5, price)
    r2.tick(t - 1.0, price - 0.010)
    r2.tick(t - 0.5, price - 0.020)
    assert r2.tick(t, price - 0.018) == "up"


def test_a_small_move_is_not_an_impulse():
    r = Ritmo()
    ts, price = warm(r)
    t = to_second(ts, 5.0)
    r.tick(t - 2.0, price)
    r.tick(t - 1.5, price)
    r.tick(t - 0.5, price + 0.0012)
    assert r.tick(t, price + 0.0010) is None


def test_only_inside_the_window_of_the_candle():
    for second in (0.5, 12.5, 14.0):
        r = Ritmo()
        ts, price = warm(r)
        t = to_second(ts, second)
        r.tick(t - 2.0, price)
        r.tick(t - 1.5, price)
        r.tick(t - 1.0, price + 0.010)
        r.tick(t - 0.5, price + 0.020)
        assert r.tick(t, price + 0.018) is None, second


def test_entries_are_spaced_and_capped_per_candle():
    r = Ritmo()
    ts, price = warm(r)
    t = to_second(ts, 2.5)

    def impulse(at, base):
        r.tick(at - 2.0, base)
        r.tick(at - 1.5, base)
        r.tick(at - 1.0, base + 0.010)
        r.tick(at - 0.5, base + 0.020)
        return r.tick(at, base + 0.018)

    assert impulse(t, price) == "down"
    r.entered(t)
    assert impulse(t + 2.0, price + 0.02) is None  # 2 s later: too soon
    assert GAP > 2.0
    assert impulse(t + 4.0, price + 0.04) == "down"
    r.entered(t + 4.0)
    assert impulse(t + 8.0, price + 0.06) == "down"
    r.entered(t + 8.0)
    assert r.in_candle == MAX_PER_CANDLE
    # Next candle: the count starts again.
    t2 = to_second(t + 8.0, 3.0)
    assert impulse(t2, price + 0.08) == "down"


class FakeConn:
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()
        self.streams = set()
        self.prices = {}

    async def subscribe(self, asset):
        self.streams.add(asset)


class FakeBridge:
    def __init__(self, payout=92.0):
        self.open_id = None
        self.orders = []
        self.payout = payout

    def asset_of(self, name):
        return "CHFNOK_otc"

    async def order(self, payload):
        self.orders.append(payload)
        return dict(ok=True, id=str(len(self.orders)), payout=self.payout)


def play(bridge, feed):
    """Run the follower over a list of ticks, one per loop turn."""
    conn = FakeConn()
    events = []
    pending = list(feed)

    async def sleep(_):
        if pending:
            ts, price = pending.pop(0)
            conn.prices["CHFNOK_otc"] = (ts, price, 0.0)

    asyncio.run(ritmo.run(bridge, conn, "CHF/NOK OTC", lambda: not pending, lambda kind, text: events.append((kind, text)), sleep=sleep))
    return events


def calm(start=1000.0, price=7.0, n=400):
    return [(start + i * 0.5, price + (0.001 if i % 2 else 0.0)) for i in range(n)]


def test_the_follower_sends_a_fixed_stake_3_second_order_tagged_ritmo():
    feed = calm()
    ts, price = feed[-1]
    t = to_second(ts, 4.0)
    feed += [(t - 2.0, price), (t - 1.5, price), (t - 1.0, price + 0.010), (t - 0.5, price + 0.020), (t, price + 0.018), (t + 0.5, price + 0.018)]
    bridge = FakeBridge()
    events = play(bridge, feed)
    assert bridge.orders == [dict(asset="CHF/NOK OTC", dir="down", seconds=3, level=0, tag="ritmo")]
    assert any(kind == "RITMO" and "historia suficiente" in text for kind, text in events)


def test_the_follower_waits_while_a_trade_is_open_and_pauses_on_a_low_payout():
    feed = calm()
    ts, price = feed[-1]
    t = to_second(ts, 4.0)
    spike = [(t - 2.0, price), (t - 1.5, price), (t - 1.0, price + 0.010), (t - 0.5, price + 0.020), (t, price + 0.018), (t + 0.5, price + 0.018)]
    busy = FakeBridge()
    busy.open_id = "otra"
    play(busy, feed + spike)
    assert busy.orders == []  # one trade at a time: the panel's order is still open

    low = FakeBridge(payout=78.0)
    t2 = to_second(t + 15, 5.0)
    again = [(t2 - 2.0, price), (t2 - 1.5, price), (t2 - 1.0, price + 0.010), (t2 - 0.5, price + 0.020), (t2, price + 0.018), (t2 + 0.5, price + 0.018)]
    events = play(low, feed + spike + again)
    assert len(low.orders) == 1  # the second impulse comes inside the pause
    assert any("menos de 80" in text for _, text in events)


def follow(r, t, price, body=0.010):
    """A candle that opens at `price` at second 0.5 and climbs: ticks every half second."""
    base = t - t % 15
    r.tick(base + 0.5, price)
    r.tick(base + 1.5, price + body * 0.3)
    r.tick(base + 2.5, price + body * 0.6)
    r.tick(base + 3.0, price + body * 0.8)
    return r.tick(base + 3.5, price + body)


def test_colour_mode_follows_a_candle_with_a_body_that_keeps_going():
    r = Ritmo(mode="color")
    ts, price = warm(r)
    assert follow(r, ts + 15, price) == "up"  # green and still rising → buy
    r2 = Ritmo(mode="color")
    ts, price = warm(r2)
    assert follow(r2, ts + 15, price, body=-0.010) == "down"  # red and still falling → sell


def test_colour_mode_does_not_enter_on_a_small_candle_or_when_the_price_turned():
    r = Ritmo(mode="color")
    ts, price = warm(r)
    assert follow(r, ts + 15, price, body=0.0006) is None  # a small candle says nothing yet
    r2 = Ritmo(mode="color")
    ts, price = warm(r2)
    base = (ts + 15) - (ts + 15) % 15
    r2.tick(base + 0.5, price)
    r2.tick(base + 2.0, price + 0.012)
    r2.tick(base + 3.0, price + 0.012)
    assert r2.tick(base + 3.5, price + 0.008) is None  # still green, but the last tick went against the colour


def test_colour_mode_needs_to_have_seen_the_candle_open():
    r = Ritmo(mode="color")
    ts, price = warm(r, n=400)
    base = (ts + 15) - (ts + 15) % 15
    r.tick(base + 4.0, price)  # first tick of this candle arrives late: its open is unknown
    r.tick(base + 4.5, price + 0.010)
    assert r.tick(base + 5.0, price + 0.020) is None


# ---------- The three frozen rules (mode color3) ----------
def long_warm(r, minutes=6.0, start=3000.0, price=7.0, drift=0.00002):
    """Minutes of a calm market with a slow drift (per half second), so there is a slope."""
    n = int(minutes * 120)
    ts = start
    for i in range(n):
        ts = start + i * 0.5
        r.tick(ts, price + i * drift + (0.001 if i % 2 else 0.0))
    return ts, price + (n - 1) * drift


def test_frozen_rules_enter_with_the_slope_once_per_candle():
    r = Ritmo(mode="color3")
    ts, price = long_warm(r)  # drifting up for 6 minutes
    assert follow(r, ts + 15, price, body=0.0015) == "up"
    base = (ts + 15) - (ts + 15) % 15
    r.entered(base + 3.5)
    r.tick(base + 7.5, price + 0.0017)  # same candle, still green and rising
    assert r.tick(base + 8.0, price + 0.0019) is None  # rule 1: one entry per candle


def test_frozen_rules_refuse_a_stretched_candle_and_a_trade_against_the_slope():
    r = Ritmo(mode="color3")
    ts, price = long_warm(r)
    assert follow(r, ts + 15, price, body=0.010) is None  # rule 2: body far beyond 2 sigmas
    r2 = Ritmo(mode="color3")
    ts, price = long_warm(r2)  # the market has been rising
    assert follow(r2, ts + 15, price, body=-0.0015) is None  # rule 3: a red candle against the rise


def test_frozen_rules_wait_for_enough_history_for_the_slope():
    r = Ritmo(mode="color3")
    ts, price = long_warm(r, minutes=3.0)  # sigma is known, the slope is not
    assert follow(r, ts + 15, price, body=0.0015) is None
