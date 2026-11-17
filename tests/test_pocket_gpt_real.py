"""TRADING GPT BOT on Pocket Option (user, 4 Nov 2026): the REAL-account mode and its locks, with fakes only (no socket,
no Telegram, no money)."""
import asyncio
import json
import time
import types

import pytest

from pocket_tyl import capturar_ssid
from pocket_tyl.conector import PocketDemo, REAL_URL
from pocket_tyl.gpt import DEFAULTS, GptTest, day_start, ladder_of, parse_gpt
from pocket_tyl.secret import has_ssid, real_check, ssid_info
from pocket_tyl.store import Store

REAL_AUTH = '42["auth",{"session":"abcdefghijklmnopqrstuvwx","isDemo":0,"uid":1}]'
DEMO_AUTH = '42["auth",{"session":"abcdefghijklmnopqrstuvwx","isDemo":1,"uid":1}]'
SIGNAL = ("SEÑAL DE TRADING\n━━━\n\nActivo: CAD/JPY (OTC)\nDuración: {}\nHora actual (UTC+3): 19:12:11\n"
          "Dirección: ALCISTA\nConfianza: 74%\n")


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


MARTINGALE = dict(DEFAULTS, multiplier=2.2)  # the ladder used 10:25-12:21 (off since 12:3x)


def test_no_martingale_by_default_since_12_30():
    assert ladder_of(DEFAULTS) == [1.0] and DEFAULTS['every_seconds'] == 60


def test_the_ladder_with_a_50_dollar_cap():
    assert ladder_of(MARTINGALE) == [1.0, 2.2, 4.84, 10.65, 23.43]


def test_real_connector_takes_only_a_real_session_and_sends_real_orders():
    with pytest.raises(ValueError):
        PocketDemo(DEMO_AUTH, real=True)
    conn = PocketDemo(REAL_AUTH, real=True, user_agent="UA-de-la-pagina")
    assert conn.url == REAL_URL and conn.headers["User-Agent"] == "UA-de-la-pagina"
    with pytest.raises(ConnectionError):
        conn.handle("successupdateBalance", {"isDemo": 1, "balance": 50})
    conn.handle("successupdateBalance", {"isDemo": 0, "balance": 50})
    assert conn.ready.is_set() and conn.balance == 50

    async def go():
        conn.ws = FakeWs()
        task = asyncio.ensure_future(conn.open_order("CADJPY_otc", "ARRIBA", 1.0, 15))
        await asyncio.sleep(0)
        order = json.loads(conn.ws.sent[0][2:])
        assert order[1]["isDemo"] == 0 and order[1]["time"] == 15 and order[1]["action"] == "call"
        conn.handle("successopenOrder", {"requestId": order[1]["requestId"], "id": "x1", "openPrice": 1})
        return await task
    assert asyncio.run(go())["id"] == "x1"


def test_the_demo_connector_still_refuses_a_real_session():
    with pytest.raises(ValueError):
        PocketDemo(REAL_AUTH)
    conn = PocketDemo(DEMO_AUTH)
    with pytest.raises(ConnectionError):
        conn.handle("successupdateBalance", {"isDemo": 0, "balance": 50})


def real_test(tmp_path, balance=100.0, cfg=None):
    conn = types.SimpleNamespace(balance=balance, ready=asyncio.Event(), assets={})
    # since=0: these tests count from today's 00:00, not from the clean start in DEFAULTS (09:05 on 5 Nov 2026)
    return GptTest(Store(tmp_path / "p.sqlite"), {"since": 0.0, **(cfg or {})}, conn, real=True)


def book(test, ours, stake, net, at=None):
    with test.store.db:
        test.store.db.execute("INSERT INTO gpt_ops(message, opened_at, seconds, stake, net, ours, state) VALUES(?,?,?,?,?,?,?)",
                              (int((at or time.time()) * 1000) % 10 ** 9, at or time.time(), 15, stake, net, ours, "cerrada"))


def test_the_daily_brake_never_lets_the_day_pass_minus_50(tmp_path):
    t = real_test(tmp_path, cfg=dict(multiplier=2.2))
    assert t.brake() is None
    now = time.time()
    for k, s in enumerate((1.0, 2.2, 4.84)):  # three losses: next stake 10.65, day −8.04
        book(t, "perdió", s, -s, now - 100 + k)
    assert t.steps[t.level()] == 10.65 and t.brake() is None
    book(t, "perdió", 10.65, -10.65, now - 50)  # day −18.69, next 23.43 → −42.12 still inside −50
    assert t.brake() is None
    book(t, "perdió", 23.43, -23.43, now - 40)  # day −42.12, back to $1 → −43.12: allowed
    assert t.steps[t.level()] == 1.0 and t.brake() is None
    book(t, "perdió", 1.0, -1.0, now - 30)
    book(t, "perdió", 2.2, -2.2, now - 20)  # day −45.32, next 4.84 → −50.16 would pass the brake
    kind, text = t.brake()
    assert kind == "GPT FRENO DIARIO" and "50" in text


def test_yesterday_does_not_count_and_the_martingale_starts_again_each_day(tmp_path):
    t = real_test(tmp_path, cfg=dict(multiplier=2.2))
    yesterday = day_start() - 3600
    for k, s in enumerate((1.0, 2.2, 4.84, 10.65)):
        book(t, "perdió", s, -s, yesterday + k)
    assert t.today_net() == 0 and t.level() == 0 and t.brake() is None


def test_no_order_bigger_than_the_balance(tmp_path):
    t = real_test(tmp_path, balance=0.5)
    assert t.brake()[0] == "GPT SIN SALDO"


def test_real_plays_only_15_second_signals(tmp_path):
    t = real_test(tmp_path)
    opened = []

    async def open_order(*a):
        opened.append(a)
        return {"id": "o", "openPrice": 1}
    t.conn.open_order = open_order
    m = types.SimpleNamespace(id=7, date=types.SimpleNamespace(timestamp=time.time))
    asyncio.run(t.play(m, parse_gpt(SIGNAL.format("5 s")), time.time(), time.time() - 3))
    assert not opened
    assert any("solo se juegan las de 15 s" in d for (d,) in t.store.db.execute("SELECT detail FROM events"))


def test_the_capture_window_keeps_a_real_session_only_in_real_mode(tmp_path):
    line = capturar_ssid.MARK + "wss://api-x.po.market/socket.io/?EIO=4" + capturar_ssid.SEP
    assert "No se guardó" in capturar_ssid.handle_console(line + DEMO_AUTH, "pocketoption.com", tmp_path, real=True)
    assert not has_ssid(tmp_path)
    shown = capturar_ssid.handle_console(line + REAL_AUTH, "pocketoption.com", tmp_path, real=True, ua="UA")
    assert shown.startswith("✅") and has_ssid(tmp_path)
    assert ssid_info(tmp_path) == dict(demo=False, url="wss://api-x.po.market/socket.io/?EIO=4", ua="UA")
    assert real_check(REAL_AUTH) is None


def test_the_demo_capture_still_refuses_a_real_session(tmp_path):
    line = capturar_ssid.MARK + "wss://demo-api-eu.po.market/x" + capturar_ssid.SEP
    assert "No se guardó" in capturar_ssid.handle_console(line + REAL_AUTH, "pocketoption.com", tmp_path)
    assert not has_ssid(tmp_path)


def test_a_refused_real_host_moves_to_the_next_one():
    tried = []

    async def refuse(url, headers):
        tried.append(url)
        raise ConnectionError("El servidor no aceptó el WebSocket: HTTP/1.1 403 Forbidden")
    logs = []
    conn = PocketDemo(REAL_AUTH, real=True, url="wss://api-spb.po.market/socket.io/?EIO=4&transport=websocket",
                      connect=refuse, log=lambda k, t: logs.append(t))
    for _ in range(3):
        with pytest.raises(ConnectionError):
            asyncio.run(conn.run())
    assert [u.split("/")[2] for u in tried] == ["api-spb.po.market", "api-eu.po.market", "api-msk.po.market"]
    assert "se prueba api-eu.po.market" in logs[0]


def test_the_demo_connector_keeps_its_single_host():
    conn = PocketDemo(DEMO_AUTH)
    assert conn.urls == [conn.url] and "demo-api" in conn.url


def test_a_low_payout_signal_is_not_played(tmp_path):
    t = real_test(tmp_path)
    t.conn.assets = {"CADJPY_otc": (73, "CADJPY_otc", "CAD/JPY OTC", "currency", 3, 58)}
    opened = []

    async def open_order(*a):
        opened.append(a)
        return {"id": "o", "openPrice": 1}
    t.conn.open_order = open_order
    m = types.SimpleNamespace(id=8, date=types.SimpleNamespace(timestamp=time.time))
    asyncio.run(t.play(m, parse_gpt(SIGNAL.format("15 s")), time.time(), time.time() - 3))
    assert not opened
    assert any("paga 58" in d for (d,) in t.store.db.execute("SELECT detail FROM events"))


def test_the_ladder_wins_back_the_run_from_85_percent():
    steps = ladder_of(MARTINGALE)
    for level in range(1, len(steps)):
        assert steps[level] * 0.85 - sum(steps[:level]) > 0


def test_the_panel_list_of_best_otc_payouts(tmp_path):
    t = real_test(tmp_path)
    t.conn.assets = {"CADJPY_otc": (73, "CADJPY_otc", "CAD/JPY OTC", "currency", 3, 58),
                     "EURUSD_otc": (66, "EURUSD_otc", "EUR/USD OTC", "currency", 5, 92),
                     "EURUSD": (1, "EURUSD", "EUR/USD", "currency", 5, 80)}
    rows = t.snapshot_payouts()
    assert [r[1] for r in rows] == ["EURUSD_otc", "CADJPY_otc"]
    assert json.loads(t.store.get("gpt_payouts"))[0][:2] == [92.0, "EURUSD_otc"]


def test_the_daily_target_of_25_stops_the_day(tmp_path):
    t = real_test(tmp_path)
    now = time.time()
    book(t, "ganó", 23.43, 24.9, now - 60)
    assert t.brake() is None
    book(t, "ganó", 1.0, 0.92, now - 30)  # +25.82 today
    kind, text = t.brake()
    assert kind == "GPT META DIARIA" and "+$25" in text


def test_without_martingale_every_signal_plays_one_dollar(tmp_path):
    t = real_test(tmp_path)
    now = time.time()
    for k in range(4):
        book(t, "perdió", 1.0, -1.0, now - 100 + k)
    assert t.steps[t.level()] == 1.0


def test_the_target_counts_from_the_clean_start_but_the_brake_counts_the_whole_day(tmp_path):
    start = time.time() - 600
    t = real_test(tmp_path, cfg=dict(since=start))
    book(t, "perdió", 23.43, -45.0, max(day_start() + 1, start - 3600))  # earlier today, before the clean start
    book(t, "ganó", 1.0, 0.92, start + 60)
    assert t.today_net(t.since()) == pytest.approx(0.92)
    assert t.today_net() == pytest.approx(-44.08)
    assert t.brake() is None  # −44.08 − 1 = −45.08: still inside −50
    for k in range(5):
        book(t, "perdió", 1.0, -1.0, start + 100 + k)
    assert t.brake()[0] == "GPT FRENO DIARIO"  # −49.08 − 1 would pass −50


# ---------- the most stable pair (4 Nov 2026) ----------
from pocket_tyl.gpt import BOT_PAIRS  # noqa: E402


class FakeBot:
    """TRADING GPT BOT's menu: signal → (Atrás) duration list → (Atrás) pair list in pages of 6 → pair → duration →
    «Obtener señal»."""

    def __init__(self, pair="CAD/JPY (OTC)"):
        self.pair, self.page, self.state = pair, 0, "signal"

    def message(self):
        rows = {"signal": [["Nueva señal"], ["Atrás"]],
                "duration": [["S5", "S15", "S30"], ["M1", "M3", "M5"], ["Atrás"]],
                "ready": [["Obtener señal"], ["Atrás"]]}.get(self.state)
        if self.state == "pairs":
            chunk = list(BOT_PAIRS[self.page * 6:(self.page + 1) * 6])
            rows = [chunk[i:i + 2] for i in range(0, len(chunk), 2)] + [["<", f"{self.page + 1}/5", ">"], ["Atrás"]]
        text = {"pairs": "Selecciona un activo:", "duration": f"Activo: {self.pair}\n\nSelecciona la duración de la operación:",
                "ready": f"Activo: {self.pair}\nDuración de la operación: 15 s", "signal": f"SEÑAL DE TRADING\nActivo: {self.pair}\n"}[self.state]
        bot = self

        class Msg:
            id = 1
            raw_text = text
            reply_markup = types.SimpleNamespace(rows=[types.SimpleNamespace(buttons=[types.SimpleNamespace(text=t) for t in r])
                                                       for r in rows])

            async def click(self, text):
                bot.click(text)
        return Msg()

    def click(self, text):
        if text == "Atrás":
            self.state = {"signal": "duration", "ready": "duration", "duration": "pairs"}.get(self.state, self.state)
        elif text == ">":
            self.page = min(self.page + 1, 4)
        elif text == "<":
            self.page = max(self.page - 1, 0)
        elif text in BOT_PAIRS and self.state == "pairs":
            self.pair, self.state = text, "duration"
        elif text == "S15" and self.state == "duration":
            self.state = "ready"


class FakeClient:
    def __init__(self, bot):
        self.bot = bot

    async def get_messages(self, peer, limit=None, ids=None):
        m = self.bot.message()
        return m if ids is not None else [m]


def test_volatility_and_the_calmest_pair_first(tmp_path):
    t = real_test(tmp_path)
    t.conn.assets = {asset: (0, asset, asset, "currency", 3, 92) for asset in ("CADJPY_otc", "AEDCNY_otc", "EURUSD_otc")}
    now = int(time.time())
    for s in range(now - 400, now):
        t.on_tick("CADJPY_otc", 0, 110 + (0.05 if s % 2 else -0.05), s)  # jumps
        t.on_tick("AEDCNY_otc", 0, 1.95 + 0.00001 * (s % 3), s)          # almost still
    cands = t.candidates()
    assert [c[1] for c in cands] == ["AED/CNY (OTC)", "CAD/JPY (OTC)"]  # EUR/USD has no prices yet
    assert cands[0][0] < cands[1][0]


def test_select_pair_walks_the_menu_to_page_3(tmp_path, monkeypatch):
    import pocket_tyl.gpt as gpt_mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gpt_mod.asyncio, "sleep", lambda s: real_sleep(0))
    t = real_test(tmp_path)
    bot = FakeBot()
    ok = asyncio.run(t.select_pair(FakeClient(bot), None, "AED/CNY (OTC)"))
    assert ok and bot.pair == "AED/CNY (OTC)" and bot.state == "ready"


def test_maybe_pick_switches_only_when_clearly_calmer(tmp_path, monkeypatch):
    import pocket_tyl.gpt as gpt_mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gpt_mod.asyncio, "sleep", lambda s: real_sleep(0))
    t = real_test(tmp_path)
    t.conn.assets = {a: (0, a, a, "currency", 3, 92) for a in ("CADJPY_otc", "AEDCNY_otc")}
    bot = FakeBot()
    t.candidates = lambda **k: [(0.95, "AED/CNY (OTC)", "AEDCNY_otc", 92.0), (1.0, "CAD/JPY (OTC)", "CADJPY_otc", 92.0)]
    asyncio.run(t.maybe_pick(FakeClient(bot), None, bot.message()))
    assert bot.pair == "CAD/JPY (OTC)"  # 0.95 is not 20 % calmer: stays
    t.picked_at = 0
    t.candidates = lambda **k: [(0.5, "AED/CNY (OTC)", "AEDCNY_otc", 92.0), (1.0, "CAD/JPY (OTC)", "CADJPY_otc", 92.0)]
    asyncio.run(t.maybe_pick(FakeClient(bot), None, bot.message()))
    assert bot.pair == "AED/CNY (OTC)" and bot.state == "ready"
    assert any("Cambia a AED/CNY" in d for (d,) in t.store.db.execute("SELECT detail FROM events"))


def test_demo_rotates_between_the_calmest_pairs(tmp_path, monkeypatch):
    import pocket_tyl.gpt as gpt_mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gpt_mod.asyncio, "sleep", lambda s: real_sleep(0))
    conn = types.SimpleNamespace(balance=100.0, ready=asyncio.Event(), assets={})
    assert GptTest(Store(tmp_path / "e.sqlite"), {}, conn, real=False).cfg["every_seconds"] == 8
    t = GptTest(Store(tmp_path / "d.sqlite"), {"rotate_pairs": 4, "rotate_block": 1, "rotate_fixed": ()}, conn, real=False)
    t.candidates = lambda **k: [(0.1, "AED/CNY (OTC)", "AEDCNY_otc", 92.0), (0.2, "EUR/GBP (OTC)", "EURGBP_otc", 92.0),
                            (0.3, "USD/CNH (OTC)", "USDCNH_otc", 92.0), (0.4, "CAD/CHF (OTC)", "CADCHF_otc", 92.0),
                            (0.9, "CAD/JPY (OTC)", "CADJPY_otc", 92.0)]
    bot = FakeBot()
    seen = []
    for _ in range(5):
        asyncio.run(t.maybe_pick(FakeClient(bot), None, bot.message()))
        seen.append(bot.pair)
    assert seen == ["AED/CNY (OTC)", "EUR/GBP (OTC)", "USD/CNH (OTC)", "CAD/CHF (OTC)", "AED/CNY (OTC)"]
    assert "AED/CNY (OTC)" in t.store.get("gpt_stability")


def test_real_keeps_a_single_pair_and_60_seconds(tmp_path):
    t = real_test(tmp_path)
    assert t.cfg["rotate_pairs"] == 0 and t.cfg["every_seconds"] == 60


def test_demo_plays_five_signals_per_pair_before_switching(tmp_path, monkeypatch):
    import pocket_tyl.gpt as gpt_mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gpt_mod.asyncio, "sleep", lambda s: real_sleep(0))
    conn = types.SimpleNamespace(balance=100.0, ready=asyncio.Event(), assets={})
    assert GptTest(Store(tmp_path / "f.sqlite"), {}, conn, real=False).cfg["rotate_fixed"]
    t = GptTest(Store(tmp_path / "d.sqlite"), {"rotate_fixed": ()}, conn, real=False)
    assert t.cfg["rotate_pairs"] == 25 and t.cfg["rotate_block"] == 5 and t.cfg["shadow"]
    assert t.cfg["overlap"] is False  # 5 Nov 09:3x: one order at a time
    t.candidates = lambda **k: [(0.1, "AED/CNY (OTC)", "AEDCNY_otc", 92.0), (0.2, "EUR/GBP (OTC)", "EURGBP_otc", 92.0)]
    bot = FakeBot()
    seen = []
    for _ in range(11):
        asyncio.run(t.maybe_pick(FakeClient(bot), None, bot.message()))
        seen.append(bot.pair)
    assert seen == ["AED/CNY (OTC)"] * 5 + ["EUR/GBP (OTC)"] * 5 + ["AED/CNY (OTC)"]


def test_demo_judges_a_low_payout_signal_without_an_order(tmp_path, monkeypatch):
    import pocket_tyl.gpt as gpt_mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gpt_mod.asyncio, "sleep", lambda s: real_sleep(0))
    prices = iter([110.00, 110.05])

    class Conn:
        balance, ready, assets = 100.0, asyncio.Event(), {"CADJPY_otc": (73, "CADJPY_otc", "CAD/JPY OTC", "currency", 3, 60)}
        orders = []

        async def subscribe(self, asset):
            pass

        def price(self, asset):
            return next(prices)

        async def open_order(self, *a):
            self.orders.append(a)
    conn = Conn()
    # the shadow judging needs a payout floor (the live DEMO plays any payout since 5 Nov 10:09)
    t = GptTest(Store(tmp_path / "d.sqlite"), {"min_payout": 85.0}, conn, real=False)
    m = types.SimpleNamespace(id=9, date=types.SimpleNamespace(timestamp=time.time))
    asyncio.run(t.play(m, parse_gpt(SIGNAL.format("M1")), time.time(), time.time() - 3))  # CALL, 110.00 → 110.05  (the DEMO plays 1-min signals since 5 Nov 09:20)
    assert not conn.orders
    row = t.store.db.execute("SELECT state, ours, net, payout FROM gpt_ops WHERE message=9").fetchone()
    assert row == ("simulada", "ganó", 0.6, 60.0)


def test_demo_focuses_on_the_top_3(tmp_path, monkeypatch):
    import pocket_tyl.gpt as gpt_mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gpt_mod.asyncio, "sleep", lambda s: real_sleep(0))
    conn = types.SimpleNamespace(balance=100.0, ready=asyncio.Event(), assets={})
    assert GptTest(Store(tmp_path / "g.sqlite"), {}, conn, real=False).cfg["rotate_fixed"] == ("EUR/USD (OTC)",)
    t = GptTest(Store(tmp_path / "d.sqlite"), {"rotate_block": 1, "rotate_fixed": ("AUD/JPY (OTC)", "EUR/USD (OTC)", "BHD/CNY (OTC)")}, conn, real=False)
    t.candidates = lambda **k: [(0.1, "USD/CNH (OTC)", "USDCNH_otc", 88.0), (0.3, "BHD/CNY (OTC)", "BHDCNY_otc", 92.0)]
    bot = FakeBot()
    seen = []
    for _ in range(4):
        asyncio.run(t.maybe_pick(FakeClient(bot), None, bot.message()))
        seen.append(bot.pair)
    assert seen == ["AUD/JPY (OTC)", "EUR/USD (OTC)", "BHD/CNY (OTC)", "AUD/JPY (OTC)"]


def test_the_tick_recorder_writes_one_file_per_pair_and_day(tmp_path):
    from pocket_tyl.grabador import TickRecorder
    conn = types.SimpleNamespace(assets={"EURUSD_otc": (66, "EURUSD_otc", "EUR/USD OTC", "currency", 5, 92)})
    store = Store(tmp_path / "p.sqlite")
    rec = TickRecorder(tmp_path, conn, store)
    now = time.time()
    rec.on_tick("EURUSD_otc", now + 7200, 1.10538, now)
    rec.on_tick("EURUSD", now + 7200, 1.1, now)  # a real-market pair: not recorded
    rec.on_tick("EURUSD_otc", now + 7201, 1.10540, now + 6)  # ≥5 s later → written
    day = dt_folder = next((tmp_path / "ticks_otc").iterdir())
    lines = (day / "EURUSD_otc.csv").read_text(encoding="ascii").splitlines()
    assert len(lines) == 2 and lines[0].split(",")[1] == "1.10538"
    assert not (dt_folder / "EURUSD.csv").exists()
    assert (day / "payouts.csv").read_text(encoding="ascii").strip().endswith("EURUSD_otc,92")
    assert int(store.get("grabador_ticks_hoy")) == 2


def test_the_paper_predictor_learns_the_rule_that_works(tmp_path):
    from pocket_tyl.predictor import PaperPredictor, MIN_N
    store = Store(tmp_path / "p.sqlite")
    pred = PaperPredictor(store)
    t0 = 1_800_000_000
    price = 1.0
    for s in range(0, 3600):  # a steady climb: the move always continues
        price += 0.0001
        pred.on_tick("EURUSD_otc", t0 + s, round(price, 5), t0 + s + 0.5)
    stats = {r: v for (p, r), v in pred.stats.items() if p == "EURUSD_otc"}
    assert stats["MOM15"][1] > 100 and stats["MOM15"][2] == 0      # momentum always right
    assert stats["REV15"][1] == 0 and stats["REV15"][2] > 100      # reversal always wrong
    assert pred.model("EURUSD_otc").startswith("MOM")
    fwd = store.db.execute("SELECT result, COUNT(*) FROM pred_forward GROUP BY result").fetchall()
    assert dict(fwd).get("acierto", 0) > 50 and "fallo" not in dict(fwd)
    assert store.db.execute("SELECT COUNT(*) FROM pred_rules").fetchone()[0] == 6
    assert MIN_N == 30


def test_the_predictor_trades_its_model_in_demo_and_archives_old_series(tmp_path, monkeypatch):
    import pocket_tyl.predictor as P
    store = Store(tmp_path / "p.sqlite")
    store.put("pred_series", "vieja")
    with store.db:
        store.db.execute("CREATE TABLE pred_rules (pair TEXT, rule TEXT, n INTEGER, w INTEGER, l INTEGER, t INTEGER, updated REAL, PRIMARY KEY (pair, rule))")
        store.db.execute("INSERT INTO pred_rules VALUES ('EURUSD_otc','MOM5',10,5,5,0,0)")
    orders = []

    class Conn:
        ready = asyncio.Event()
        assets = {"EURUSD_otc": (66, "EURUSD_otc", "EUR/USD OTC", "currency", 5, 92)}

        async def open_order(self, asset, side, stake, secs):
            orders.append((asset, side, stake, secs))
            return {"id": f"o{len(orders)}", "openPrice": 1.0}

        async def result(self, oid, timeout):
            return {"amount": 1, "openPrice": 1.0, "closePrice": 1.1, "command": 0, "percentProfit": 92}

        def price(self, a):
            return 1.0
    conn = Conn()

    async def go():
        conn.ready.set()
        pred = P.PaperPredictor(store, conn, trade=True)
        assert pred.stats == {}  # the old series was archived, not mixed in
        t0, price = 1_800_000_000, 1.0
        for s in range(0, 1800):
            price += 0.0001
            pred.on_tick("EURUSD_otc", t0 + s, round(price, 5), t0 + s + 0.5)
            if s % 15 == 0:
                await asyncio.sleep(0)
        await asyncio.gather(*pred.tasks)
        return pred
    monkeypatch.setattr(P, "FOCUS", set())  # this test is about the model-driven mode
    asyncio.run(go())
    assert orders and all(o[0] == "EURUSD_otc" and o[2] == 1.0 and o[3] == P.HORIZON == 5 for o in orders)
    assert store.db.execute("SELECT COUNT(*) FROM pred_orders WHERE result='ganó'").fetchone()[0] == len(orders)
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert any(n.startswith("pred_rules_arch_") for n in names)


def test_the_clean_test_trades_only_audcad_rev15(tmp_path, monkeypatch):
    import pocket_tyl.predictor as P
    monkeypatch.setattr(P, "FOCUS", {("AUDCAD_otc", "REV15")})  # the focus mechanism (its 4 Nov test is over)
    store = Store(tmp_path / "p.sqlite")
    orders = []

    class Conn:
        ready = asyncio.Event()
        assets = {a: (0, a, a, "currency", 5, 92) for a in ("AUDCAD_otc", "EURUSD_otc")}

        async def open_order(self, asset, side, stake, secs):
            orders.append((asset, side))
            return {"id": f"o{len(orders)}", "openPrice": 1.0}

        async def result(self, oid, timeout):
            return {"amount": 1, "openPrice": 1.0, "closePrice": 0.9, "command": 1, "percentProfit": 92}

        def price(self, a):
            return 1.0
    conn = Conn()

    async def go():
        conn.ready.set()
        pred = P.PaperPredictor(store, conn, trade=True)
        t0 = 1_800_000_000
        for s in range(0, 400):
            for a, base in (("AUDCAD_otc", 0.9), ("EURUSD_otc", 1.1)):
                pred.on_tick(a, t0 + s, round(base + 0.0001 * s, 5), t0 + s + 0.5)  # both climb
            if s % 15 == 0:
                await asyncio.sleep(0)
        await asyncio.gather(*pred.tasks)
    asyncio.run(go())
    assert orders and all(o == ("AUDCAD_otc", "ABAJO") for o in orders)  # REV15 after a rise = PUT, only AUD/CAD


def test_a_pair_pocket_refuses_for_its_expiry_rests_instead_of_spamming(tmp_path, monkeypatch):
    import pocket_tyl.predictor as P
    monkeypatch.setattr(P, "FOCUS", set())
    store = Store(tmp_path / "p.sqlite")
    tries = []

    class Conn:
        ready = asyncio.Event()
        assets = {"EURUSD_otc": (66, "EURUSD_otc", "EUR/USD OTC", "currency", 5, 92)}

        async def open_order(self, asset, side, stake, secs):
            tries.append(secs)
            raise RuntimeError("Pocket Option rechazó la orden: IncorrectExpTime")

        def price(self, a):
            return 1.0
    conn = Conn()

    async def go():
        conn.ready.set()
        pred = P.PaperPredictor(store, conn, trade=True)
        t0, price = 1_800_000_000, 1.0
        for s in range(0, 900):
            price += 0.0001
            pred.on_tick("EURUSD_otc", t0 + s, round(price, 5), t0 + s + 0.5)
            await asyncio.sleep(0)
        await asyncio.gather(*pred.tasks)
        return pred
    pred = asyncio.run(go())
    assert tries == [5]  # one refusal, then the pair rests 10 min (no order spam)
    assert pred.rest["EURUSD_otc"] > 0
    assert store.db.execute("SELECT state FROM pred_orders").fetchone()[0].startswith("falló")


def test_the_demo_plays_only_1min_signals_and_the_real_keeps_15s(tmp_path):
    from pocket_tyl.gpt import DEFAULTS, REAL
    conn = types.SimpleNamespace(balance=100.0, ready=asyncio.Event(), assets={}, listeners=[])
    demo = GptTest(Store(tmp_path / "d.sqlite"), {}, conn, real=False)
    assert demo.cfg["only_seconds"] == 60 and demo.cfg["count_seconds"] == 60 and demo.cfg["rotate_fixed"] == ("EUR/USD (OTC)",)
    real = GptTest(Store(tmp_path / "r.sqlite"), {}, conn, real=True)
    assert real.cfg["only_seconds"] == REAL["only_seconds"] == 15
    assert DEFAULTS["stake"] == 1.0 and DEFAULTS["multiplier"] == 1.0


def test_duration_buttons_of_the_bot_menu():
    from pocket_tyl.gpt import duration_label
    assert [duration_label(x) for x in (5, 15, 30, 60, 180, 300)] == ["S5", "S15", "S30", "M1", "M3", "M5"]


def test_one_gpt_order_at_a_time_and_stale_open_rows_are_closed_on_start(tmp_path):
    from pocket_tyl.gpt import DEMO_ROTATE
    assert DEMO_ROTATE["overlap"] is False
    store = Store(tmp_path / "d.sqlite")
    conn = types.SimpleNamespace(balance=100.0, ready=asyncio.Event(), assets={}, listeners=[])
    GptTest(store, {}, conn, real=False)
    with store.db:
        store.db.execute("INSERT INTO gpt_ops(message, opened_at, seconds, stake, state) VALUES(1, ?, 60, 1, 'abierta')", (time.time(),))
    GptTest(store, {}, conn, real=False)  # a restart
    assert store.db.execute("SELECT state FROM gpt_ops WHERE message=1").fetchone()[0].startswith("sin resultado")


def test_the_demo_plays_whatever_pocket_pays_but_the_real_keeps_85(tmp_path):
    conn = types.SimpleNamespace(balance=100.0, ready=asyncio.Event(), assets={}, listeners=[])
    assert GptTest(Store(tmp_path / "d.sqlite"), {}, conn, real=False).cfg["min_payout"] == 0.0
    assert GptTest(Store(tmp_path / "r.sqlite"), {}, conn, real=True).cfg["min_payout"] == 85.0


def test_after_a_loss_the_demo_only_watches_until_a_signal_wins(tmp_path, monkeypatch):
    import pocket_tyl.gpt as gpt_mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gpt_mod.asyncio, "sleep", lambda s: real_sleep(0))
    prices = iter([1.0, 1.1,   # vision signal: CALL, price up → simulated win
                   1.0, 1.0])  # then a real order

    class Conn:
        balance, ready = 100.0, asyncio.Event()
        assets = {"CADJPY_otc": (73, "CADJPY_otc", "CAD/JPY OTC", "currency", 3, 92)}
        orders = []

        async def subscribe(self, asset):
            pass

        def price(self, asset):
            return next(prices)

        async def open_order(self, asset, side, stake, secs):
            self.orders.append((asset, side, stake, secs))
            return {"id": "o1", "openPrice": 1.0}

        async def result(self, oid, timeout):
            return {"amount": 1, "openPrice": 1.0, "closePrice": 1.1, "command": 0, "percentProfit": 92}
    conn = Conn()
    t = GptTest(Store(tmp_path / "d.sqlite"), {"since": 0.0}, conn, real=False)
    assert t.cfg["pause_after_loss"] and not t.in_vision()
    book(t, "perdió", 1.0, -1.0, time.time() - 120)   # a real loss
    assert t.in_vision()
    sig = parse_gpt(SIGNAL.format("M1"))
    m1 = types.SimpleNamespace(id=21, date=types.SimpleNamespace(timestamp=time.time))
    asyncio.run(t.play(m1, sig, time.time(), time.time() - 3))
    assert not conn.orders  # watched only
    row = t.store.db.execute("SELECT state, ours FROM gpt_ops WHERE message=21").fetchone()
    assert row == ("simulada: visión", "ganó") and not t.in_vision()
    m2 = types.SimpleNamespace(id=22, date=types.SimpleNamespace(timestamp=time.time))
    asyncio.run(t.play(m2, sig, time.time(), time.time() - 3))
    assert len(conn.orders) == 1  # back to real orders after the simulated win
    real = GptTest(Store(tmp_path / "r.sqlite"), {"since": 0.0}, conn, real=True)
    book(real, "perdió", 1.0, -1.0)
    assert not real.in_vision()  # the REAL account is not touched by this rule
