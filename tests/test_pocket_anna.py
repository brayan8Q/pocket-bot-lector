"""ANNA live check on the Pocket DEMO (user, 26 Oct 2026): «ANNA VIP POCKET» and «ANNA TRADER VIP M1», one entry per
signal judged on the channel's own candle, no martingale; the channel's label is stored next to ours. Fake connector only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import asyncio
from datetime import datetime

from pocket_tyl.anna import AnnaTest, parse_result, parse_signal
from pocket_tyl.core import MEXICO
from pocket_tyl.store import Store, save_config, DEFAULTS
from test_pocket_tyl import FakeClock

SAT = "🛰 POCKET OPTION [M1]\n\n💷 AUDCAD-OTC\n💎 M1\n⌚️ 14:53:00\n🔼 call"
# as the M1 channel writes it (mathematical letters), 26 Oct 2026 10:36:06 Mexico
M1 = "🚧 𝗟𝗜𝗩𝗘 𝗦𝗜𝗚𝗡𝗔𝗟\n\n💷 𝙶𝙱𝙿𝙲𝙰𝙳-𝙾𝚃𝙲𝚋\n⏳  𝟸𝟸:𝟹𝟼 \n⌚️  𝙼𝟷\n🔴 𝙿𝚄𝚃"
POCKET, VIPM1 = "ANNA VIP POCKET", "ANNA TRADER VIP M1"


def mx(*a):
    return datetime(*a, tzinfo=MEXICO).timestamp()


def test_the_pocket_signal_is_read_in_its_utc_minus_3_clock():
    sig = parse_signal(SAT, mx(2026, 10, 25, 11, 51, 38))
    assert (sig["pair"], sig["asset"], sig["side"], sig["seconds"]) == ("AUDCAD-OTC", "AUDCAD_otc", "ARRIBA", 60)
    assert sig["entry_at"] == mx(2026, 10, 25, 11, 53)
    real = parse_signal(SAT.replace("AUDCAD-OTC", "EURJPY").replace("🔼 call", "🔽 put"), mx(2026, 10, 25, 11, 51, 38))
    assert (real["asset"], real["side"]) == ("EURJPY", "ABAJO")
    late = mx(2026, 10, 25, 21, 29, 52)  # 00:31 UTC−3 is the next day there
    assert parse_signal(SAT.replace("14:53:00", "00:31:00"), late)["entry_at"] == mx(2026, 10, 25, 21, 31)
    assert parse_signal("✅ ENTRY CONFIRMED ✅\n🌎 COIN: CHF/NOK OTC", late) is None


def test_the_m1_signal_is_read_in_its_utc_plus_6_clock():
    sig = parse_signal(M1, mx(2026, 10, 26, 10, 36, 6))
    assert (sig["asset"], sig["side"], sig["seconds"]) == ("GBPCAD_otc", "ABAJO", 60)
    assert sig["entry_at"] == mx(2026, 10, 26, 10, 36)
    real = parse_signal(M1.replace("𝙶𝙱𝙿𝙲𝙰𝙳-𝙾𝚃𝙲𝚋", "𝙶𝙱𝙿𝙹𝙿𝚈").replace("🔴 𝙿𝚄𝚃", "🟢 𝙲𝙰𝙻𝙻"), mx(2026, 10, 26, 10, 35, 37))
    assert (real["asset"], real["side"]) == ("GBPJPY", "ARRIBA")


def test_the_channels_labels():
    assert parse_result("WIN ✅")["label"] == "WIN"
    assert parse_result("WIN ✅¹")["label"] == "WIN¹" and parse_result("WIN ✅²")["label"] == "WIN²"
    assert parse_result("✖️ Loss")["label"] == "LOSS" and parse_result("DOJI ⚖")["label"] == "DOJI"
    assert parse_result("LOSS") is None and parse_result("WIIIIIINNN ✅✅✅") is None
    m1 = parse_result("✅  𝗪𝗜𝗡°°✅𝚄𝚂𝙳𝙹𝙿𝚈 | ⏰ 𝟸𝟸:𝟹𝟷")
    assert (m1["label"], m1["raw"], m1["pair"], m1["hh"], m1["mm"]) == ("WIN²", "WIN°°", "USDJPY", 22, 31)
    assert parse_result("✅ 𝗪𝗜𝗡°✅𝙶𝙱𝙿𝚄𝚂𝙳 | ⏰ 𝟸𝟸:𝟹𝟺")["label"] == "WIN¹"
    assert parse_result("✅ 𝗪𝗜𝗡 ✅𝙶𝙱𝙿𝙲𝙰𝙳-𝙾𝚃𝙲𝚋 | ⏰ 𝟸𝟸:𝟹𝟼")["label"] == "WIN"
    gale = parse_result("☑️ 𝗛𝗜𝗧 𝗚𝗔𝗟𝗘 ☑️𝙶𝙱𝙿𝙹𝙿𝚈 | ⏰ 𝟸𝟸:𝟸𝟿")
    assert (gale["label"], gale["raw"]) == ("LOSS", "HIT GALE")


class Conn:
    def __init__(self, clock, open_price, close_price):
        self.clock, self.open_price, self.close_price = clock, open_price, close_price
        self.ready = asyncio.Event(); self.ready.set()
        self.assets, self.orders, self.sides = {}, [], {}

    def clock_offset(self):
        return 0.0

    def price(self, asset):
        return self.open_price

    async def subscribe(self, asset):
        pass

    async def open_order(self, asset, direction, amount, seconds):
        self.orders.append((asset, direction, amount, seconds))
        self.sides[f"o{len(self.orders)}"] = 0 if direction == "ARRIBA" else 1
        return dict(id=f"o{len(self.orders)}", openPrice=self.open_price)

    async def result(self, order_id, timeout):
        return dict(amount=1.0, openPrice=self.open_price, closePrice=self.close_price, command=self.sides[order_id],
                    percentProfit=92)


def run(tmp_path, steps, start, open_price=1.0, close_price=1.1):
    """steps: [(source, message id, text, posted)] fed in order; returns (store, conn). Each entry gets its own fake clock
    starting when its signal was read (a shared one would jump to the 3rd minute for the others)."""
    clock = FakeClock(start)
    store = Store(tmp_path / "p.sqlite")
    conn = Conn(clock, open_price, close_price)
    test = AnnaTest(store, dict(enabled=True, stake=1.0), conn, clock=lambda: FakeClock(clock.t))

    async def go():
        import pocket_tyl.anna as anna
        real = anna.time.time
        anna.time.time = lambda: clock.t
        try:
            for source, message, text, posted in steps:
                clock.t = max(clock.t, posted)
                test.on_message(source, message, text, posted)
                await asyncio.sleep(0)
            await asyncio.gather(*test.tasks)
        finally:
            anna.time.time = real
    asyncio.run(go())
    return store, conn


def test_pocket_one_entry_at_the_second_and_the_channels_label_beside_it(tmp_path):
    posted, entry = mx(2026, 10, 25, 11, 51, 38), mx(2026, 10, 25, 11, 53)
    store, conn = run(tmp_path, [(POCKET, 230166, SAT, posted)], posted, 0.96003, 0.95953)  # 1st candle went down
    assert conn.orders == [("AUDCAD_otc", "ARRIBA", 1.0, 60)]
    assert store.db.execute("SELECT opened_at FROM anna_ops").fetchone()[0] == entry
    AnnaTest(store, dict(enabled=True), conn).on_message(POCKET, 230167, "WIN ✅¹", entry + 120)
    row = store.db.execute("SELECT ours, net, channel, channel_raw, state, price2, price3 FROM anna_ops").fetchone()
    assert row == ("perdió", -1.0, "WIN¹", "WIN¹", "cerrada", 0.96003, 0.96003)
    detail = store.db.execute("SELECT detail FROM events WHERE kind='ANNA COMPARA'").fetchone()[0]
    assert "canal: WIN¹" in detail and "VIP POCKET: 1 jugadas, 1ª entrada 0 ganadas / 1 perdidas" in detail


def test_m1_signal_posted_after_its_candle_opened_enters_at_once_and_expires_with_the_candle(tmp_path):
    entry = mx(2026, 10, 26, 10, 36)
    result = "✅ 𝗪𝗜𝗡 ✅𝙶𝙱𝙿𝙲𝙰𝙳-𝙾𝚃𝙲𝚋 | ⏰ 𝟸𝟸:𝟹𝟼"
    store, conn = run(tmp_path, [(VIPM1, 150614, M1, entry + 6)], entry + 6, 1.70, 1.69)
    assert conn.orders == [("GBPCAD_otc", "ABAJO", 1.0, 54)]
    AnnaTest(store, dict(enabled=True), conn).on_message(VIPM1, 150616, result, entry + 68)
    row = store.db.execute("SELECT ours, net, channel, opened_at, duration FROM anna_ops").fetchone()
    assert row == ("ganó", 0.92, "WIN", entry + 6, 54)


def test_m1_result_goes_to_its_own_pair_and_candle(tmp_path):
    entry = mx(2026, 10, 26, 10, 40)
    nzd = M1.replace("𝙶𝙱𝙿𝙲𝙰𝙳", "𝙽𝚉𝙳𝚄𝚂𝙳").replace("𝟹𝟼", "𝟺𝟶")
    usd = M1.replace("𝙶𝙱𝙿𝙲𝙰𝙳", "𝚄𝚂𝙳𝙲𝙰𝙳").replace("𝟹𝟼", "𝟺𝟶")
    store, conn = run(tmp_path, [(VIPM1, 1, nzd, entry - 2), (VIPM1, 2, usd, entry - 2)], entry - 2)
    assert len(conn.orders) == 2 and all(o[3] == 60 for o in conn.orders)
    assert [r[0] for r in store.db.execute("SELECT opened_at FROM anna_ops")] == [entry, entry]
    test = AnnaTest(store, dict(enabled=True), conn)
    test.on_message(VIPM1, 3, "✅ 𝗪𝗜𝗡°✅𝚄𝚂𝙳𝙲𝙰𝙳-𝙾𝚃𝙲𝚋 | ⏰ 𝟸𝟸:𝟺𝟶", entry + 125)
    test.on_message(VIPM1, 4, "☑️ 𝗛𝗜𝗧 𝗚𝗔𝗟𝗘 ☑️𝙽𝚉𝙳𝚄𝚂𝙳-𝙾𝚃𝙲𝚋 | ⏰ 𝟸𝟸:𝟺𝟶", entry + 185)
    got = dict(store.db.execute("SELECT asset, channel_raw FROM anna_ops").fetchall())
    assert got == {"USDCAD_otc": "WIN°", "NZDUSD_otc": "HIT GALE"}


def test_a_signal_too_late_in_its_candle_is_not_played(tmp_path):
    entry = mx(2026, 10, 26, 10, 36)
    store, conn = run(tmp_path, [(VIPM1, 150614, M1, entry + 45)], entry + 45)
    assert not conn.orders
    assert store.db.execute("SELECT state FROM anna_ops").fetchone()[0].startswith("no se jugó")


def test_both_channels_keep_their_own_message_ids(tmp_path):
    posted = mx(2026, 10, 26, 10, 35, 50)
    pocket = SAT.replace("14:53:00", "13:36:00")
    store, conn = run(tmp_path, [(VIPM1, 7, M1, posted), (POCKET, 7, pocket, posted)], posted)
    assert store.db.execute("SELECT COUNT(*) FROM anna_ops").fetchone()[0] == 2 and len(conn.orders) == 2


def test_the_page_shows_the_anna_card(tmp_path):
    from PySide6.QtWidgets import QApplication
    from pocket_tyl.panel import PocketTylPanel
    app = QApplication.instance() or QApplication([])
    save_config(tmp_path, {**DEFAULTS, "anna": dict(enabled=True, channels=[VIPM1, POCKET], stake=1.0)})
    entry = mx(2026, 10, 26, 10, 36)
    store, conn = run(tmp_path, [(VIPM1, 150614, M1, entry + 6)], entry + 6, 1.70, 1.69)
    AnnaTest(store, dict(enabled=True), conn).on_message(VIPM1, 150616, "✅ 𝗪𝗜𝗡 ✅𝙶𝙱𝙿𝙲𝙰𝙳-𝙾𝚃𝙲𝚋 | ⏰ 𝟸𝟸:𝟹𝟼", entry + 68)
    store.close()
    (tmp_path / "p.sqlite").replace(tmp_path / "pocket_tyl.sqlite")
    panel = PocketTylPanel(tmp_path)
    try:
        panel.refresh()
        assert not panel.anna_card.isHidden()
        assert panel.anna_sum[(VIPM1, "ganamos")].text() == "1" and "+0.92" in panel.anna_sum[("total", "neto")].text()
        assert panel.anna_label[(VIPM1, "WIN")].text().startswith("1 → ganamos 1")
        assert panel.anna_table.rowCount() == 1
        cells = [panel.anna_table.item(0, c).text() for c in range(13)]
        assert cells[1:5] == ["VIP M1", "GBP/CAD OTCb", "PUT ⬇", "6 s tarde"] and cells[12] == "WIN"
    finally:
        panel.close()
        app.processEvents()


# ---------- the channel's martingale (user, 26 Oct 2026 11:15) ----------
class TaskClock(FakeClock):
    """One fake clock per task; the market reads the time of the task asking."""
    def __init__(self, start, now_of):
        super().__init__(start)
        self.now_of = now_of
        now_of[asyncio.current_task()] = start

    async def until(self, deadline):
        t = await super().until(deadline)
        self.now_of[asyncio.current_task()] = t
        return t


class Market:
    """A connector with a price path [(from_time, price)]: fills at the price of the moment, closes at expiry."""
    def __init__(self, path, now_of):
        self.path, self.now_of = path, now_of
        self.ready = asyncio.Event(); self.ready.set()
        self.assets, self.orders, self.deals = {}, [], {}

    def at(self, t):
        return [p for start, p in self.path if start <= t][-1]

    def clock_offset(self):
        return 0.0

    def price(self, asset):
        return self.at(self.now_of[asyncio.current_task()])

    async def subscribe(self, asset):
        pass

    async def open_order(self, asset, direction, amount, seconds):
        t = self.now_of[asyncio.current_task()]
        self.orders.append((t, amount, seconds))
        oid = f"o{len(self.orders)}"
        self.deals[oid] = dict(amount=amount, openPrice=self.at(t), closePrice=self.at(t + seconds),
                               command=0 if direction == "ARRIBA" else 1, percentProfit=92)
        return dict(id=oid, openPrice=self.at(t))

    async def result(self, order_id, timeout):
        return self.deals[order_id]


def run_gale(tmp_path, path, gales=2):
    posted, entry = mx(2026, 10, 25, 11, 51, 38), mx(2026, 10, 25, 11, 53)
    now_of, base = {}, FakeClock(posted)
    store = Store(tmp_path / "p.sqlite")
    market = Market([(t + entry, p) for t, p in path], now_of)
    test = AnnaTest(store, dict(enabled=True, stake=1.0, gales=gales, multiplier=2.2), market,
                    clock=lambda: TaskClock(base.t, now_of))

    async def go():
        import pocket_tyl.anna as anna
        real = anna.time.time
        anna.time.time = lambda: base.t
        try:
            test.on_message(POCKET, 1, SAT, posted)
            await asyncio.gather(*test.tasks)
        finally:
            anna.time.time = real
    asyncio.run(go())
    return store, market, entry


def test_the_gale_goes_out_at_the_next_candle_and_stops_when_it_wins(tmp_path):
    # CALL: the 1st candle falls (judged at :59.8), the 2nd rises
    store, market, entry = run_gale(tmp_path, [(-100, 1.00), (30, 0.99), (90, 1.02)])
    assert market.orders == [(entry, 1.0, 60), (entry + 60, 2.2, 60)]
    row = store.db.execute("SELECT ours, net, gale_level, gale_net, gale_result, state FROM anna_ops").fetchone()
    assert row == ("perdió", -1.0, 1, 1.02, "ganó en la 2ª vela", "cerrada")
    AnnaTest(store, dict(enabled=True, gales=2), market).on_message(POCKET, 2, "WIN ✅¹", entry + 140)
    detail = store.db.execute("SELECT detail FROM events WHERE kind='ANNA COMPARA'").fetchone()[0]
    assert "1ª entrada: perdió (-1.00) · con martingala: ganó en la 2ª vela (+1.02)" in detail


def test_three_candles_against_lose_the_whole_ladder(tmp_path):
    store, market, entry = run_gale(tmp_path, [(-100, 1.00), (30, 0.99), (90, 0.98), (150, 0.97)])
    assert [o[1] for o in market.orders] == [1.0, 2.2, 4.84]
    assert [o[0] for o in market.orders] == [entry, entry + 60, entry + 120]
    assert store.db.execute("SELECT gale_net, gale_result FROM anna_ops").fetchone() == (-8.04, "perdió las 3")
    assert store.db.execute("SELECT COUNT(*) FROM anna_orders WHERE net < 0").fetchone()[0] == 3


def test_no_gale_after_a_winning_first_candle_or_when_gales_are_off(tmp_path):
    store, market, entry = run_gale(tmp_path, [(-100, 1.00), (30, 1.01)])
    assert len(market.orders) == 1
    assert store.db.execute("SELECT gale_net, gale_result FROM anna_ops").fetchone() == (0.92, "ganó en la 1ª vela")
    (tmp_path / "off").mkdir()
    store, market, entry = run_gale(tmp_path / "off", [(-100, 1.00), (30, 0.99)], gales=0)
    assert len(market.orders) == 1
    assert store.db.execute("SELECT gale_net, gale_result FROM anna_ops").fetchone() == (-1.0, "perdió")


def test_a_real_market_pair_that_refuses_a_short_expiry_gets_a_full_minute(tmp_path):
    entry = mx(2026, 10, 26, 11, 23)
    gbpusd = M1.replace("𝙶𝙱𝙿𝙲𝙰𝙳-𝙾𝚃𝙲𝚋", "𝙶𝙱𝙿𝚄𝚂𝙳").replace("𝟸𝟸:𝟹𝟼", "𝟸𝟹:𝟸𝟹")  # 23:23 UTC+6 = 11:23 Mexico
    clock = FakeClock(entry + 3)
    store = Store(tmp_path / "p.sqlite")
    conn = Conn(clock, 1.3347, 1.3349)
    real_open = conn.open_order

    async def picky(asset, direction, amount, seconds):
        if seconds != 60 and not asset.endswith("_otc"):
            raise RuntimeError("Pocket Option rechazó la orden: IncorrectExpTime")
        return await real_open(asset, direction, amount, seconds)
    conn.open_order = picky
    test = AnnaTest(store, dict(enabled=True, stake=1.0), conn, clock=lambda: FakeClock(clock.t))

    async def go():
        import pocket_tyl.anna as anna
        real = anna.time.time
        anna.time.time = lambda: clock.t
        try:
            test.on_message(VIPM1, 9, gbpusd, entry + 3)
            await asyncio.gather(*test.tasks)
        finally:
            anna.time.time = real
    asyncio.run(go())
    assert conn.orders == [("GBPUSD", "ABAJO", 1.0, 60)]
    assert store.db.execute("SELECT duration, state FROM anna_ops").fetchone() == (60, "cerrada")
    assert "solo acepta 1 min" in store.db.execute("SELECT detail FROM events WHERE kind='ANNA DISPARO'").fetchone()[0]
