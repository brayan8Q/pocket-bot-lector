"""Pocket Option · Tyl (user, 21 Oct 2026): Camino 1 — Tyl's formats, the short martingale of 22 Oct 2026 (J2 2.00 → J3
4.40 in the same signal, one shared book with a single recovery level J2 4.00 → J3 8.80), off for the group's day at
+20 USD with a -40 USD brake, and the dashboard page reading its own database. The module never imports the gold bot."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime
import ast
from pathlib import Path

import pytest

from pocket_tyl.core import GROUP_TZ, Book, Plan, parse_result, parse_signal, settle_signal
from pocket_tyl.engine import PocketEngine
from pocket_tyl.secret import demo_check
from pocket_tyl.store import Store, plan_of, DEFAULTS

SIG = ("💵 GBP/JPY OTC\n⏳ Expiración: 5 minutos\n✅ Entrada a las 00:05\n🟢 ARRIBA\n\nUsa martingala si es necesario 👇\n\n"
       "1️⃣ MARTINGALA A LAS 00:10\n2️⃣ MARTINGALA A LAS 00:15\n\n📊 Haz clic aquí para abrir el bróker")


def at(h, m, day=21):
    return datetime(2026, 10, day, h, m, tzinfo=GROUP_TZ).timestamp()


def test_tyl_signal_and_results():
    s = parse_signal(39457, SIG, at(0, 2) - 86400 + 86400)  # posted 00:02 GMT-3, 21 Oct
    assert (s.pair, s.direction, s.day) == ("GBP/JPY", "ARRIBA", "2026-10-21")
    assert s.gale1_at - s.entry_at == 300 and s.gale2_at - s.entry_at == 600
    late = parse_signal(1, SIG, datetime(2026, 10, 20, 23, 58, tzinfo=GROUP_TZ).timestamp())  # posted before midnight
    assert late.day == "2026-10-21"
    assert parse_result("VICTORIA DIRECTA ✅🤑") == "D" and parse_result("VICTORIA EN 1ª MARTINGALA ✅🤑") == "G1"
    assert parse_result("VICTORIA EN 2ª MARTINGALA ✅🤑") == "G2" and parse_result("❌ PÉRDIDA") == "L"
    assert parse_result("INFORME DE OPERACIONES (21/10)📊\nX -> PÉRDIDA ❌") is None


def test_the_short_martingale_plays_j2_then_j3_in_the_same_signal():
    plan, book = Plan(), Book()
    ops, net, stop = settle_signal("G1", book, 0.0, plan)  # J2 wins: +1.84, the signal ends
    assert ops == [(2, 2.0, True, 1.84)] and net == 1.84 and (book.debt, book.step) == (0.0, 0)
    ops, net, stop = settle_signal("G2", book, 0.0, plan)  # J2 -2.00, J3 4.40 wins +4.05: +2.05 net
    assert ops == [(2, 2.0, False, -2.0), (3, 4.4, True, 4.05)] and net == 2.05 and (book.debt, book.step) == (0.0, 0)
    ops, net, stop = settle_signal("D", book, 0.0, plan)  # the 1st candle won: nobody plays
    assert ops == [] and net == 0.0


def test_both_lost_is_one_recovery_level_and_never_higher():
    plan, book = Plan(), Book()
    ops, net, stop = settle_signal("L", book, 0.0, plan)  # -6.40: the next signals at J2 4.00 → J3 8.80
    assert [(p, st) for p, st, _, _ in ops] == [(2, 2.0), (3, 4.4)] and net == -6.4 and (book.debt, book.step) == (6.4, 1)
    ops, net, stop = settle_signal("L", book, net, plan)  # -12.80 at the recovery level: still that level (the top)
    assert [(p, st) for p, st, _, _ in ops] == [(2, 4.0), (3, 8.8)] and (book.debt, book.step) == (19.2, 1)
    ops, net, stop = settle_signal("G1", book, net, plan)  # +3.68: still owes 15.52
    assert ops == [(2, 4.0, True, 3.68)] and (book.debt, book.step) == (15.52, 1)
    for _ in range(4):
        settle_signal("G2", book, 0.0, plan)  # +4.10 each (8.80 x 0.92 - 4.00)
    assert (book.debt, book.step) == (0.0, 0) and plan.stake(2, book.step) == 2.0 and plan.stake(3, book.step) == 4.4


def test_off_at_once_at_plus_20_and_brake_at_minus_40():
    plan = Plan()
    ops, net, stop = settle_signal("G2", Book(), 18.5, plan)  # J2 -2.00 → 16.50, J3 +4.05 → 20.55: off
    assert stop == "meta" and net == pytest.approx(20.55) and len(ops) == 2
    ops, net2, stop = settle_signal("L", Book(), net, plan)  # nothing more today
    assert ops == [] and net2 == net
    ops, net, stop = settle_signal("L", Book(debt=6.4, step=1), -36.5, plan)  # J2 loses 4.00 → -40.50: brake, J3 skipped
    assert stop == "freno" and len(ops) == 1 and net == -40.5


def test_paper_engine_end_to_end(tmp_path):
    store = Store(tmp_path / "pocket_tyl.sqlite")
    eng = PocketEngine(store, dict(DEFAULTS))
    eng.on_message(39457, SIG, at(0, 2))
    eng.tick(at(0, 6))
    assert store.open_signals()[0][7] == "Vigilando 1ª vela sin apostar"
    eng.tick(at(0, 10) + 30)
    assert "J2 operando ($2.00)" in store.open_signals()[0][7]
    eng.on_message(39460, "VICTORIA EN 1ª MARTINGALA ✅🤑", at(0, 15) + 9)
    assert not store.open_signals()
    assert store.db.execute("SELECT player, stake, won, net FROM ops").fetchall() == [(2, 2.0, 1, 1.84)]
    assert store.day("2026-10-21") == (1.84, None)
    eng.on_message(39461, SIG.replace("00:05", "00:20").replace("00:10", "00:25").replace("00:15", "00:30"), at(0, 17))
    eng.on_message(39462, "VICTORIA DIRECTA ✅🤑", at(0, 25) + 9)
    assert "señal descartada" in store.db.execute("SELECT state FROM signals WHERE message=39461").fetchone()[0]
    store.close()


def test_only_demo_ssids_are_accepted():
    assert demo_check('42["auth",{"session":"abcdefghijklmnopqrstuvwx","isDemo":1,"uid":1,"platform":2}]') is None
    assert "REAL" in demo_check('42["auth",{"session":"abcdefghijklmnopqrstuvwx","isDemo":0,"uid":1}]')
    assert demo_check("abcdefghijklmnopqrstuvwxyz0123456789") is not None  # no isDemo: refused


def test_pocket_never_imports_the_gold_bot():
    for path in Path(__file__).resolve().parent.parent.joinpath("pocket_tyl").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + \
                [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any(x.startswith("signalbridge") or x.startswith("MetaTrader5") for x in names), path.name


def test_the_panel_reads_its_database(tmp_path):
    from PySide6.QtWidgets import QApplication
    from pocket_tyl.panel import PocketTylPanel
    app = QApplication.instance() or QApplication([])
    store = Store(tmp_path / "pocket_tyl.sqlite")
    eng = PocketEngine(store, dict(DEFAULTS))
    today = datetime.now(GROUP_TZ)
    t0 = today.replace(hour=0, minute=2, second=0, microsecond=0).timestamp()
    eng.on_message(1, SIG, t0)
    eng.on_message(2, "❌ PÉRDIDA", t0 + 3 * 60 + 600 + 9)
    store.close()
    panel = PocketTylPanel(tmp_path)
    try:
        panel.refresh()
        assert panel.stat["rondas"].text() == "1" and panel.stat["perdidas"].text() == "2"
        assert panel.table.rowCount() == 2 and "-6.40" in panel.stat["neto"].text()
        # both lost: the next signal plays the recovery level
        assert (panel.player[2][1].text(), panel.player[3][1].text()) == ("4.00 USD", "8.80 USD")
        assert panel.level_chip.text() == "En recuperación" and "6.40 USD" in panel.book_note.text()
        assert "J2 2.00 USD → J3 4.40 USD" in panel.subtitle.text() and "+20.00" in panel.goal_text.text()
        assert panel.brake.text() == "-40.00 USD"
    finally:
        panel.close()
        app.processEvents()


def test_the_capture_window_keeps_only_a_demo_auth_from_pocket_option(tmp_path):
    from pocket_tyl.capturar_ssid import HOOK, MARK, handle_console
    from pocket_tyl.secret import has_ssid, protect
    demo = '42["auth",{"session":"abcdefghijklmnopqrstuvwx","isDemo":1,"uid":7,"platform":2}]'
    real = demo.replace('"isDemo":1', '"isDemo":0')
    assert MARK in HOOK and '42["auth"' in HOOK
    assert handle_console("cualquier otro mensaje de la página", "pocketoption.com", tmp_path) is None
    assert "no viene de pocketoption.com" in handle_console(MARK + demo, "evil.example", tmp_path) and not has_ssid(tmp_path)
    assert "REAL" in handle_console(MARK + real, "pocketoption.com", tmp_path) and not has_ssid(tmp_path)
    assert handle_console(MARK + demo, "pocketoption.com", tmp_path).startswith("✅") and has_ssid(tmp_path)
    assert protect((tmp_path / "ssid.bin").read_bytes(), decrypt=True).decode() == demo  # stored protected, not as text
    assert demo.encode() not in (tmp_path / "ssid.bin").read_bytes()


def test_any_emoji_before_the_pair_and_the_10_23_example():
    from pocket_tyl.core import MEXICO
    text = ("🪙 AUD/NZD OTC\n⏳ Expiración: 5 minutos\n✅ Entrada a las 13:25\n🔴 ABAJO\n\n"
            "1️⃣ MARTINGALA A LAS 13:30\n2️⃣ MARTINGALA A LAS 13:35")
    posted = datetime(2026, 10, 21, 10, 23, tzinfo=MEXICO).timestamp()
    s = parse_signal(1, text, posted)
    hm = lambda t: datetime.fromtimestamp(t, MEXICO).strftime("%H:%M:%S")
    assert (s.pair, s.direction, hm(s.entry_at), hm(s.gale1_at), hm(s.gale2_at)) == ("AUD/NZD", "ABAJO", "10:25:00", "10:30:00", "10:35:00")
    assert parse_signal(2, text.replace("🪙", "💵"), posted).pair == "AUD/NZD"
    assert parse_signal(3, text.replace("🪙 ", ""), posted).pair == "AUD/NZD"


class FakeClock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    async def until(self, deadline):
        self.t = max(self.t, deadline)
        return self.t


def play(s, prices):
    """prices: [(from_time, price)] — the price in force at each moment."""
    import asyncio
    from pocket_tyl.disparo import run_signal
    clock = FakeClock(s.entry_at - 120)
    def price(asset):
        return [p for t, p in prices if t <= clock.t][-1]
    orders = []
    async def send(player, asset, direction, stake, seconds):
        orders.append((round(clock.t - s.entry_at, 3), asset, direction, stake, seconds))
        return dict(id=f"o{len(orders)}", openPrice=price(asset))
    steps = []
    sent = asyncio.run(run_signal(s, price, send, lambda player: Plan().stake(player, 0), Plan(), clock, steps.append))
    return sent, orders, steps


def test_the_trigger_fires_at_the_exact_second_and_judges_at_59_8():
    from pocket_tyl.core import MEXICO
    text = "🪙 AUD/NZD OTC\n✅ Entrada a las 13:25\n🔴 ABAJO\n1️⃣ MARTINGALA A LAS 13:30\n2️⃣ MARTINGALA A LAS 13:35"
    s = parse_signal(1, text, datetime(2026, 10, 21, 10, 23, tzinfo=MEXICO).timestamp())
    e = s.entry_at
    # ABAJO: the 1st candle closes higher (lost) → J2 at 10:30:00.000 with 2.00; J2's candle closes lower (won) → no J3
    sent, orders, _ = play(s, [(e - 999, 1.0950), (e + 60, 1.0960), (e + 300, 1.0955), (e + 590, 1.0940)])
    assert orders == [(300.0, "AUDNZD_otc", "ABAJO", 2.0, 300)] and sent == [(2, 2.0)]
    # J2 loses → J3 at 10:35:00.000 with 4.40, in the same signal
    sent, orders, _ = play(s, [(e - 999, 1.0950), (e + 60, 1.0960), (e + 300, 1.0955), (e + 590, 1.0970)])
    assert [o[0] for o in orders] == [300.0, 600.0] and sent == [(2, 2.0), (3, 4.4)]
    # the 1st candle wins → discarded, nothing sent
    sent, orders, steps = play(s, [(e - 999, 1.0950), (e + 200, 1.0940)])
    assert orders == [] and steps[-1].what == "ganó"
    # a tie at 10:29:59.800 counts as a loss → J2 goes
    sent, orders, _ = play(s, [(e - 999, 1.0950), (e + 600, 1.0900)])
    assert orders and orders[0][0] == 300.0


class FakeDemo:
    """The connector as the runner sees it: ready, a price per asset by fake time, orders and results."""
    def __init__(self, clock, prices, closes):
        import asyncio
        self.clock, self.prices, self.closes = clock, prices, closes
        self.ready = asyncio.Event(); self.ready.set()
        self.orders, self.balance = [], 50000.0

    def latency(self):
        return -0.15

    def clock_offset(self):
        return 0.15

    def price(self, asset):
        return [p for t, p in self.prices if t <= self.clock.t][-1]

    async def subscribe(self, asset):
        pass

    async def open_order(self, asset, direction, amount, seconds):
        oid = f"o{len(self.orders) + 1}"
        self.orders.append((round(self.clock.t, 3), asset, direction, amount, seconds))
        return dict(id=oid, openPrice=self.price(asset))

    async def result(self, order_id, timeout):
        n = int(order_id[1:]) - 1
        t, asset, direction, amount, seconds = self.orders[n]
        close = self.closes[n]
        return dict(id=order_id, amount=amount, openPrice=self.price(asset) if False else None, closePrice=close,
                    command=0 if direction == "ARRIBA" else 1, percentProfit=92) | dict(openPrice=self._open[n])

    _open = {}


def test_the_demo_runner_fires_settles_and_books_the_signal(tmp_path):
    import asyncio
    from pocket_tyl.core import MEXICO
    from pocket_tyl.engine import DemoRunner
    text = "🪙 AUD/NZD OTC\n✅ Entrada a las 13:25\n🔴 ABAJO\n1️⃣ MARTINGALA A LAS 13:30\n2️⃣ MARTINGALA A LAS 13:35"
    s = parse_signal(7, text, datetime(2026, 10, 21, 10, 23, tzinfo=MEXICO).timestamp())
    e = s.entry_at
    clock = FakeClock(e - 100)
    conn = FakeDemo(clock, [(e - 999, 1.0950), (e + 60, 1.0960), (e + 300, 1.0955), (e + 590, 1.0970)], [1.0970, 1.0940])
    FakeDemo._open = {0: 1.0955, 1: 1.0970}  # J2 ABAJO opens 1.0955 closes 1.0970 (lost); J3 opens 1.0970 closes 1.0940 (won)
    store = Store(tmp_path / "pocket_tyl.sqlite")
    store.add_signal(s, "nueva")
    runner = DemoRunner(store, Plan(), conn, clock=lambda offset: clock)
    asyncio.run(runner.play(s))
    assert [o[0] - e for o in conn.orders] == [300.0, 600.0]  # 10:30:00.000 and 10:35:00.000
    assert [(o[2], o[3]) for o in conn.orders] == [("ABAJO", 2.0), ("ABAJO", 4.4)]
    ops = store.db.execute("SELECT player, stake, won, net, mode FROM ops ORDER BY id").fetchall()
    assert ops == [(2, 2.0, 0, -2.0, "demo"), (3, 4.4, 1, 4.05, "demo")]
    assert (store.book().debt, store.book().step) == (0.0, 0)  # +2.05 for the signal: nothing owed
    assert store.day(s.day) == (2.05, None)
    assert store.db.execute("SELECT result FROM signals WHERE message=7").fetchone()[0] == "G2"
    store.close()


def test_the_demo_runner_goes_up_one_level_after_both_lose(tmp_path):
    import asyncio
    from pocket_tyl.core import MEXICO
    from pocket_tyl.engine import DemoRunner
    text = "🪙 AUD/NZD OTC\n✅ Entrada a las 13:25\n🔴 ABAJO\n1️⃣ MARTINGALA A LAS 13:30\n2️⃣ MARTINGALA A LAS 13:35"
    store = Store(tmp_path / "pocket_tyl.sqlite")
    stakes = []
    for n, posted in ((8, datetime(2026, 10, 21, 10, 23, tzinfo=MEXICO)), (9, datetime(2026, 10, 21, 10, 38, tzinfo=MEXICO))):
        s = parse_signal(n, text.replace("13:25", f"{posted.hour + 3}:{posted.minute + 2}")
                         .replace("13:30", f"{posted.hour + 3}:{posted.minute + 7}")
                         .replace("13:35", f"{posted.hour + 3}:{posted.minute + 12}"), posted.timestamp())
        e = s.entry_at
        clock = FakeClock(e - 100)
        conn = FakeDemo(clock, [(e - 999, 1.0950), (e + 60, 1.0960), (e + 300, 1.0955), (e + 590, 1.0970)], [1.0970, 1.0990])
        FakeDemo._open = {0: 1.0955, 1: 1.0970}  # ABAJO: J2 and J3 both close higher → both lost
        store.add_signal(s, "nueva")
        asyncio.run(DemoRunner(store, Plan(), conn, clock=lambda offset: clock).play(s))
        stakes.append([o[3] for o in conn.orders])
    assert stakes == [[2.0, 4.4], [4.0, 8.8]]  # the 2nd signal plays the recovery level
    assert (store.book().debt, store.book().step) == (19.2, 1)  # 6.40 + 12.80, still the top level
    legacy = dict(store.db.execute("SELECT player, step FROM books").fetchall())
    assert legacy == {0: 1, 2: 1, 3: 3}  # the dashboard page loaded before the change: J2 4.00, J3 8.80 (base x ladder)
    events = [d for k, d in store.db.execute("SELECT kind, detail FROM events") if k == "LIBRETA"]
    assert "recuperación: J2 $4.00 → J3 $8.80" in events[0]
    store.close()


def test_deal_net_reads_pocket_results():
    from pocket_tyl.conector import deal_net
    base = dict(amount=12, percentProfit=92, command=1, openPrice=1.0955)
    assert deal_net(base | dict(closePrice=1.0940)) == 11.04  # put, lower: won
    assert deal_net(base | dict(closePrice=1.0970)) == -12  # put, higher: lost
    assert deal_net(base | dict(closePrice=1.0955)) == 0.0  # same price: refund


def test_the_connector_refuses_a_real_session():
    import asyncio
    from pocket_tyl.conector import PocketDemo
    with pytest.raises(ValueError):
        PocketDemo('42["auth",{"session":"abcdefghijklmnopqrstuvwx","isDemo":0,"uid":1}]')
    conn = PocketDemo('42["auth",{"session":"abcdefghijklmnopqrstuvwx","isDemo":1,"uid":1}]')
    with pytest.raises(ConnectionError):
        conn.handle("successupdateBalance", {"isDemo": 0, "balance": 1000})
    assert not conn.ready.is_set()


def test_jugador_1_and_2_bet_the_1st_candle_then_the_1st_martingale():
    """User, 23 Oct 2026 ("cámbialo a jugador 1 y 2"): J1 2.00 at the 1st candle's second, J2 4.40 only if it lost."""
    import asyncio
    from pocket_tyl.core import MEXICO, plays_of, settle_signal, Book
    from pocket_tyl.disparo import run_signal
    from pocket_tyl.store import plan_of, DEFAULTS
    plan = plan_of(dict(DEFAULTS, first_candle=1))
    assert plan.players == (1, 2) and plan.stake(1, 0) == 2.0 and plan.stake(2, 0) == 4.4 and plan.stake(2, 1) == 8.8
    assert plays_of("D", 1) == [(1, True)] and plays_of("G2", 1) == [(1, False), (2, False)]
    ops, net, _ = settle_signal("G1", Book(), 0.0, plan)
    assert [(p, s, w) for p, s, w, _ in ops] == [(1, 2.0, False), (2, 4.4, True)]
    text = "🪙 AUD/NZD OTC\n✅ Entrada a las 13:25\n🔴 ABAJO\n1️⃣ MARTINGALA A LAS 13:30\n2️⃣ MARTINGALA A LAS 13:35"
    s = parse_signal(1, text, datetime(2026, 10, 21, 10, 23, tzinfo=MEXICO).timestamp())
    e = s.entry_at

    def play_j1(prices):
        clock = FakeClock(e - 120)
        def price(asset):
            return [p for t, p in prices if t <= clock.t][-1]
        orders = []
        async def send(player, asset, direction, stake, seconds):
            orders.append((round(clock.t - e, 3), player, stake, seconds))
            return dict(id=f"o{len(orders)}", openPrice=price(asset))
        sent = asyncio.run(run_signal(s, price, send, lambda p: plan.stake(p, 0), plan, clock, lambda step: None))
        return sent, orders
    # the 1st candle wins (ABAJO, it closes lower): only J1, at 10:25:00.000
    sent, orders = play_j1([(e - 999, 1.0950), (e + 200, 1.0940)])
    assert orders == [(0.0, 1, 2.0, 300)] and sent == [(1, 2.0)]
    # it loses: J2 4.40 at 10:30:00.000; J2 loses too: nothing at 10:35 (no third player)
    sent, orders = play_j1([(e - 999, 1.0950), (e + 200, 1.0960), (e + 590, 1.0970)])
    assert [(o[0], o[1], o[2]) for o in orders] == [(0.0, 1, 2.0), (300.0, 2, 4.4)] and len(sent) == 2


def test_the_demo_runner_plays_jugador_1_and_2(tmp_path):
    """23 Oct 2026, AUD/CHF 18:50: with J1/J2 the runner asked for J3's stake and the play died in silence."""
    import asyncio
    from pocket_tyl.core import MEXICO
    from pocket_tyl.engine import DemoRunner
    from pocket_tyl.store import DEFAULTS, Store, plan_of
    store = Store(tmp_path / "p.sqlite")
    plan = plan_of(dict(DEFAULTS, first_candle=1))
    text = "🪙 AUD/CHF OTC\n✅ Entrada a las 21:50\n🔴 ABAJO\n1️⃣ MARTINGALA A LAS 21:55\n2️⃣ MARTINGALA A LAS 22:00"
    s = parse_signal(39920, text, datetime(2026, 10, 23, 18, 48, tzinfo=MEXICO).timestamp())
    e = s.entry_at
    clock = FakeClock(e - 100)
    demo = FakeDemo(clock, [(e - 999, 0.5450), (e + 200, 0.5440)], [0.5440])  # ABAJO, the 1st candle closes lower: J1 wins
    FakeDemo._open = {0: 0.5450}
    runner = DemoRunner(store, plan, demo, clock=lambda offset: clock)
    store.add_signal(s, "Esperando 1ª vela")
    asyncio.run(runner.play(s))
    assert [(o[0] - e, o[1], o[2], o[3], o[4]) for o in demo.orders] == [(0.0, "AUDCHF_otc", "ABAJO", 2.0, 300)]
    assert store.db.execute("SELECT player, stake, won FROM ops").fetchall() == [(1, 2.0, 1)]
    assert store.db.execute("SELECT result FROM signals WHERE message=39920").fetchone()[0] == "D"
    store.close()
