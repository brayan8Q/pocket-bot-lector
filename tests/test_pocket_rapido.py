"""The own 15-second strategy on the Pocket DEMO (user, 23 Oct 2026): rules, paper scoring, one live order at a time
only on a pair paying enough, its own daily target / brake, and the Monte Carlo. Fake connection, no network."""
import asyncio
import math

import pytest

from pocket_tyl.rapido import Rapido, day_of, decide, grid, hit_rate, monte_carlo, outcome
from pocket_tyl.store import Store


class FakeConn:
    def __init__(self):
        self.listeners, self.streams, self.orders = [], set(), []
        self.ready = asyncio.Event()
        self.assets = {"USDCAD_otc": [1, "USDCAD_otc", "USD/CAD OTC", "currency", 5, 92]}
        self.last = {}
        self.close_at = None

    def price(self, asset):
        return self.last.get(asset)

    async def subscribe(self, asset):
        self.streams.add(asset)

    async def open_order(self, asset, direction, amount, seconds):
        self.orders.append((asset, direction, amount, seconds))
        return {"id": len(self.orders), "openPrice": self.last[asset]}

    async def result(self, order_id, timeout):
        asset, direction, amount, seconds = self.orders[order_id - 1]
        close = self.close_at if self.close_at is not None else self.last[asset]
        return {"id": order_id, "openPrice": self.last[asset], "closePrice": close,
                "command": 0 if direction == "ARRIBA" else 1, "amount": amount, "percentProfit": 92}

    def feed(self, asset, t, price):
        self.last[asset] = price
        for f in self.listeners:
            f(asset, t, price, t)


def up_trend(n=140, start=1000.0):
    return [(start + i, 1.3000 + 0.00001 * i + (0.00002 if i % 2 else 0)) for i in range(n)]


def test_rules_read_the_direction():
    flat_then_up = [1.3 + (0.0001 * math.sin(i)) for i in range(100)] + [1.3 + 0.0005 * k for k in range(1, 32)]
    assert decide("tendencia", flat_then_up) == "ARRIBA"
    spike_down = [1.3 + (0.00005 * math.sin(i)) for i in range(120)] + [1.3 - 0.002 * k for k in range(1, 12)]
    assert decide("rebote", spike_down) == "ARRIBA"  # against the sharp move
    assert decide("tendencia", [1.3] * 131) is None  # no movement, no signal
    assert outcome("ARRIBA", 1.0, 1.1) == 1 and outcome("ABAJO", 1.0, 1.1) == -1 and outcome("ABAJO", 1.0, 1.0) == 0


def test_grid_waits_for_two_minutes_of_prices():
    ticks = up_trend(60)
    assert grid(ticks, 1059.0) == []
    ticks = up_trend(140)
    g = grid(ticks, 1139.0)
    assert len(g) == 131 and g[-1] == ticks[-1][1]


def test_monte_carlo_counts_targets_and_brakes():
    always = monte_carlo([1] * 20, 0.92, 1.0, 20.0, -20.0, runs=200, seed=1)
    assert always["meta"] == 1.0 and always["freno"] == 0.0
    never = monte_carlo([-1] * 20, 0.92, 1.0, 20.0, -20.0, runs=200, seed=1)
    assert never["freno"] == 1.0
    assert monte_carlo([1, -1], 0.92, 1.0, 20, -20) is None  # too few results
    assert hit_rate([1, -1, 0, 1]) == (pytest.approx(2 / 3), 3)


def test_one_live_order_on_the_paying_pair_and_the_day_is_booked(tmp_path):
    async def go():
        store = Store(tmp_path / "p.sqlite")
        conn = FakeConn()
        conn.ready.set()
        now = [1000.0]
        r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], warmup=1, window=10, pause=0.0), conn, tmp_path,
                   clock=lambda: now[0])
        r.paper["tendencia"].extend([1, 1, 1])
        r.choose(now[0])
        assert r.active == "tendencia"
        conn.last["USDCAD_otc"] = 1.3
        r.maybe_trade(now[0], {("tendencia", "USDCAD_otc"): "ARRIBA"})
        r.maybe_trade(now[0], {("tendencia", "USDCAD_otc"): "ARRIBA"})  # one order at a time
        conn.close_at = 1.31
        await asyncio.sleep(0.05)
        assert len(conn.orders) == 1 and conn.orders[0][3] == 15
        net, trades, stopped = r.day(day_of(1000.0))
        assert (round(net, 2), trades) == (0.92, 1)
        conn.assets["USDCAD_otc"][5] = 60  # the pair pays 60 %: no order
        r.maybe_trade(now[0], {("tendencia", "USDCAD_otc"): "ARRIBA"})
        await asyncio.sleep(0.01)
        assert len(conn.orders) == 1
        store.close()
    asyncio.run(go())


def test_the_brake_stops_the_day(tmp_path):
    async def go():
        store = Store(tmp_path / "p.sqlite")
        conn = FakeConn()
        conn.ready.set()
        r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], warmup=1, window=10, pause=0.0, loss_stop=-2.0),
                   conn, tmp_path, clock=lambda: 1000.0)
        r.active = "tendencia"
        r.paper["tendencia"].extend([1] * 12)  # a sure rule: the Monte Carlo lets it bet
        conn.last["USDCAD_otc"] = 1.3
        conn.close_at = 1.29
        for _ in range(3):
            r.maybe_trade(1000.0, {("tendencia", "USDCAD_otc"): "ARRIBA"})
            await asyncio.sleep(0.02)
        assert len(conn.orders) == 2 and r.day(day_of(1000.0))[2] == "freno"
        store.close()
    asyncio.run(go())


def test_ticks_are_recorded_and_paper_signals_scored(tmp_path):
    async def go():
        store = Store(tmp_path / "p.sqlite")
        conn = FakeConn()
        r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"]), conn, tmp_path, clock=lambda: 2000.0)
        for t, p in up_trend(140):
            conn.feed("USDCAD_otc", t, p)
        await r.step(1139.0)
        assert r.paper_open  # at least one rule fired on the climb
        conn.feed("USDCAD_otc", 1160.0, 1.4)
        await r.step(1160.0)
        assert any(r.paper[rule] for rule in r.paper)
        r.flush(5000.0)
        files = list((tmp_path / "ticks").glob("ticks_*.csv"))
        assert files and files[0].read_text(encoding="utf-8").count("\n") == 141
        store.close()
    asyncio.run(go())


def test_a_rule_below_break_even_never_trades(tmp_path):
    store = Store(tmp_path / "p.sqlite")
    conn = FakeConn()
    r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], warmup=5, window=20), conn, tmp_path, clock=lambda: 1000.0)
    r.paper["cruce"].extend([1, -1, -1, -1, 1, -1, -1, -1])  # 25 %
    r.choose(1000.0)
    assert r.active is None and "En pausa" in store.db.execute("SELECT detail FROM events ORDER BY id DESC").fetchone()[0]
    r.paper["tendencia"].extend([1, 1, 1, -1, 1, 1])  # 83 %
    r.choose(1000.0)
    assert r.active == "tendencia"
    store.close()


def test_opposites_memory_and_monte_carlo_before_the_bet(tmp_path):
    from pocket_tyl.rapido import confidence, paper_from_rows
    rows = [["cruce", "USDCAD_otc", "ARRIBA", "1.0", "1", "1", "-1"]] * 9 + [["cruce", "USDCAD_otc", "ARRIBA", "1.0", "1", "1", "1"]] * 3
    got = paper_from_rows(rows)
    assert got["cruce"].count(-1) == 9 and got["contra_cruce"].count(1) == 9  # losing 75 % = the opposite wins 75 %
    assert confidence([1] * 12, 0.521, seed=1) == 1.0 and confidence([1, -1] * 6, 0.521, seed=1) < 0.6
    (tmp_path / "ticks").mkdir()
    from pocket_tyl.rapido import day_of
    with open(tmp_path / "ticks" / f"rapido_papel_{day_of(1000.0)}.csv", "w", encoding="utf-8") as f:
        f.writelines(",".join(r) + "\n" for r in rows)
    store = Store(tmp_path / "p.sqlite")
    conn = FakeConn()
    conn.ready.set()
    r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], window=60), conn, tmp_path, clock=lambda: 1000.0)
    assert len(r.paper["contra_cruce"]) == 12  # today's paper results survive a restart
    r.choose(1000.0)
    assert r.active == "contra_cruce"

    async def go():
        conn.last["USDCAD_otc"] = 1.3
        r.maybe_trade(1000.0, {("contra_cruce", "USDCAD_otc"): "ABAJO"})
        await asyncio.sleep(0.02)
    asyncio.run(go())
    assert conn.orders and conn.orders[0][1] == "ABAJO"
    r.paper["contra_cruce"].extend([-1, 1] * 30)  # now a coin flip: the Monte Carlo blocks the next bet
    r.trading = False
    r.next_at = 0
    asyncio.run(go())
    assert len(conn.orders) == 1
    store.close()


def test_continuous_mode_trades_every_pass_without_gates_and_falls_back_to_5_s(tmp_path):
    store = Store(tmp_path / "p.sqlite")
    conn = FakeConn()
    conn.ready.set()
    r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], seconds=3, continuous=True, pause=0.0), conn, tmp_path,
               clock=lambda: 1000.0)
    r.paper["contra_ema"].extend([1, -1, -1, -1] * 5)  # against the trend lost 75 % …
    r.paper["ema"].extend([-1, 1, 1, 1] * 5)  # … so following it won 75 % (the engine always scores both)
    r.choose(1000.0)
    assert r.active == "ema"

    async def go(fired):
        conn.last["USDCAD_otc"] = 1.3
        r.maybe_trade(1000.0, fired)
        await asyncio.sleep(0.02)
    asyncio.run(go({("ema", "USDCAD_otc"): "ARRIBA", ("contra_ema", "USDCAD_otc"): "ABAJO"}))
    assert conn.orders[-1] == ("USDCAD_otc", "ARRIBA", 1.0, 3)

    async def refuse(*a):
        raise RuntimeError("Pocket Option rechazó la orden: tiempo inválido")
    conn.open_order = refuse
    asyncio.run(go({("ema", "USDCAD_otc"): "ABAJO"}))
    assert r.cfg["seconds"] == 5 and "se pasa a 5 s" in store.db.execute("SELECT detail FROM events ORDER BY id DESC").fetchone()[0]
    store.close()


def test_the_brain_reads_candles_learns_and_always_trades(tmp_path):
    from pocket_tyl.cerebro import FEATURES, Model, candles, features
    g = [1.3 + 0.0001 * i for i in range(131)]  # a steady climb
    x = features(g)
    assert len(x) == len(FEATURES) and x[1] == 1 and x[3] == 1 and x[5] == 1  # trend, impulse and candle say up
    assert candles([1, 2, 3, 2, 1, 0, 1, 2, 3, 4, 5], size=5, count=2) == [(1, 3, 0, 0), (0, 5, 0, 5)]
    m = Model()
    for _ in range(300):  # a market where the candle colour keeps reversing: the brain learns to read it the other way
        m.learn([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0], up=False)
        m.learn([1, 0, 0, 0, 0, -1, 0, 0, 0, 0, 0], up=True)
    assert m.w[5] < -0.5 and m.direction([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0])[0] == "ABAJO"
    store = Store(tmp_path / "p.sqlite")
    conn = FakeConn()
    conn.ready.set()
    r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], seconds=3, continuous=True, brain=True, pause=0.0,
                           target=0, loss_stop=0), conn, tmp_path, clock=lambda: 1000.0)
    assert r.active == "cerebro"
    r.latest["USDCAD_otc"] = x

    async def go():
        conn.last["USDCAD_otc"] = 1.3
        conn.close_at = 1.29
        for _ in range(25):  # 25 losses in a row: no brake when it is off, it keeps trading
            r.maybe_trade(1000.0, {})
            await asyncio.sleep(0.005)
    asyncio.run(go())
    assert len(conn.orders) == 25 and conn.orders[0][3] == 3 and r.day(day_of(1000.0))[2] is None
    store.close()


def test_option_b_trades_10_s_only_on_reversal_signals(tmp_path):
    from pocket_tyl.rapido import giro_direction
    up_streak = [1.0, 0, 0, 0, 0, 1, -1, 0, -1, 0, 0]  # 3 green candles (racha says down) and RSI above 70
    assert giro_direction(up_streak) == "ABAJO"
    assert giro_direction([1.0, 1, 1, 1, 1, 1, 0, 1, 0, 0, 1]) is None  # trend votes alone never trade
    assert giro_direction([1.0, 0, 0, 0, 0, 0, -1, 0, 1, 0, 0]) is None  # they disagree: no trade
    store = Store(tmp_path / "p.sqlite")
    conn = FakeConn()
    conn.ready.set()
    r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], seconds=10, giro=True, continuous=True, pause=0.0,
                           target=0, loss_stop=0), conn, tmp_path, clock=lambda: 1000.0)
    assert r.active == "giro"

    async def go():
        conn.last["USDCAD_otc"] = 1.3
        r.latest["USDCAD_otc"] = [1.0, 1, 1, 1, 1, 1, 0, 1, 0, 0, 1]
        r.maybe_trade(1000.0, {})
        await asyncio.sleep(0.01)
        assert not conn.orders  # no reversal signal: it waits
        r.latest["USDCAD_otc"] = up_streak
        r.maybe_trade(1000.0, {})
        await asyncio.sleep(0.01)
    asyncio.run(go())
    assert conn.orders == [("USDCAD_otc", "ABAJO", 1.0, 10)]
    store.close()


def test_a_restart_reuses_the_recorded_prices(tmp_path):
    from pocket_tyl.rapido import day_of, grid
    (tmp_path / "ticks").mkdir()
    now = 50_000.0
    with open(tmp_path / "ticks" / f"ticks_{day_of(now)}.csv", "w", encoding="utf-8") as f:
        f.write("cabecera parcial\n")
        for k in range(200):
            f.write(f"USDCAD_otc,{now - 200 + k:.3f},{now - 200 + k + 7200:.3f},{1.3 + k * 1e-5}\n")
    store = Store(tmp_path / "p.sqlite")
    r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"]), FakeConn(), tmp_path, clock=lambda: now)
    assert grid(r.ticks["USDCAD_otc"], now)  # two minutes of prices already: no wait after a restart
    store.close()


def test_two_of_three_and_scorecards_per_pair(tmp_path):
    from pocket_tyl.rapido import giro_votes
    rsi_and_bands_down = [1.0, 0, 0, 0, 0, 0, 0, 0, -1, -1, 0]
    only_streak = [1.0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0]
    assert giro_votes(rsi_and_bands_down) == "ABAJO" and giro_votes(only_streak) is None
    assert giro_votes(rsi_and_bands_down, ["racha", "rsi"]) is None  # bands switched off: one vote is not enough
    store = Store(tmp_path / "p.sqlite")
    conn = FakeConn()
    conn.ready.set()
    r = Rapido(store, dict(enabled=True, assets=["USDCAD_otc"], seconds=10, giro=True, giro_votes=2, giro_cards=True,
                           continuous=True, pause=0.0, target=0, loss_stop=0), conn, tmp_path, clock=lambda: 1000.0)

    async def go(x):
        conn.last["USDCAD_otc"] = 1.3
        r.latest["USDCAD_otc"] = x
        r.trading, r.next_at = False, 0
        r.maybe_trade(1000.0, {})
        await asyncio.sleep(0.01)
    asyncio.run(go(only_streak))
    assert not conn.orders
    asyncio.run(go(rsi_and_bands_down))
    assert conn.orders[-1][:2] == ("USDCAD_otc", "ABAJO")
    r.card("bandas", "USDCAD_otc").extend([-1] * 12 + [1] * 4)  # bands lose 75 % on this pair: they stop counting
    asyncio.run(go(rsi_and_bands_down))
    assert len(conn.orders) == 1
    assert "deja de contar bandas" in store.db.execute("SELECT detail FROM events ORDER BY id DESC").fetchone()[0]
    store.close()
