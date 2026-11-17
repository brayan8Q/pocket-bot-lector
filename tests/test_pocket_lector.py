"""Bridge Lector de tendencia → Pocket Option DEMO (user, 15 Nov 2026), with fakes only: no socket, no account."""
import asyncio
import json

import pytest

from pocket_tyl.lector import DEFAULTS, LectorBridge, amount_for, asset_key, guess_code
from pocket_tyl.store import Store

ASSETS = {"EURUSD_otc": (66, "EURUSD_otc", "EUR/USD OTC", "currency", 5, 50),
          "AUDCAD_otc": (67, "AUDCAD_otc", "AUD/CAD OTC", "currency", 5, 92)}


class FakeConn:
    """What the bridge uses of conector.PocketDemo."""

    def __init__(self, balance=1000.0, fail=None, deal=None):
        self.ready = asyncio.Event()
        self.ready.set()
        self.real = False
        self.balance = balance
        self.assets = dict(ASSETS)
        self.sent = []
        self.fail = fail
        self.deal = deal or dict(openPrice=1.1, closePrice=1.2, command=0, amount=1.0, percentProfit=52)
        self.release = None

    async def subscribe(self, asset):
        self.sent.append(("subscribe", asset))

    async def open_order(self, asset, direction, amount, seconds):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append(("order", asset, direction, amount, seconds))
        self.command = 0 if direction == "ARRIBA" else 1  # Pocket's code for call / put
        return dict(id=f"op{len(self.sent)}", openPrice=1.1, percentProfit=52)

    async def result(self, order_id, timeout):
        if self.release is not None:
            await self.release.wait()
        return dict(self.deal, id=order_id, command=self.command)


def bridge(tmp_path, conn=None, cfg=None):
    store = Store(tmp_path / "pocket_tyl.sqlite")
    return LectorBridge(store, cfg, conn or FakeConn(), token="clave"), store


def run(coro):
    return asyncio.run(coro)


def test_names_and_amounts():
    assert asset_key("EUR/USD OTC") == asset_key("EURUSD_otc") == "eurusdotc"
    assert guess_code("EUR/USD OTC") == "EURUSD_otc" and guess_code("eur/usd") == "EURUSD"
    assert [amount_for(DEFAULTS, n) for n in range(3)] == [1.0, 2.2, 4.84]
    assert DEFAULTS["levels"] == 4 and [amount_for(DEFAULTS, n) for n in (3, 4)] == [10.65, 23.43]  # up to four martingales
    assert amount_for(DEFAULTS, 4) <= DEFAULTS["max_stake"] < amount_for(DEFAULTS, 5)


def test_an_order_goes_to_the_demo_and_comes_back_with_pockets_numbers(tmp_path):
    async def go():
        b, store = bridge(tmp_path)
        opened = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3, level=0))
        assert opened["ok"] and opened["asset"] == "EURUSD_otc" and opened["amount"] == 1.0
        assert opened["open_price"] == 1.1 and opened["payout"] == 52 and opened["open_ms"] >= opened["sent_ms"]
        assert ("order", "EURUSD_otc", "ARRIBA", 1.0, 3) in b.conn.sent
        done = await b.outcome(opened["id"])
        assert done == dict(ok=True, id=opened["id"], result="win", net=0.52, close_price=1.2, open_price=1.1)
        assert (await b.outcome(opened["id"]))["result"] == "win"  # asking twice gives the same answer
        row = store.db.execute("SELECT asset, direction, level, amount, payout, net, result FROM lector_ops").fetchone()
        assert row == ("EURUSD_otc", "ARRIBA", 0, 1.0, 52.0, 0.52, "win")
        assert b.status()["open"] is None
    run(go())


def test_the_martingale_level_sets_the_amount_and_the_cap_refuses_more(tmp_path):
    async def go():
        conn = FakeConn(deal=dict(openPrice=1.1, closePrice=1.0, command=0, amount=1.0, percentProfit=52))
        b, _ = bridge(tmp_path, conn)
        first = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3, level=0))
        assert (await b.outcome(first["id"]))["result"] == "loss"
        second = await b.order(dict(asset="EUR/USD OTC", dir="down", seconds=3, level=1))
        assert second["ok"] and second["amount"] == 2.2
        assert ("order", "EURUSD_otc", "ABAJO", 2.2, 3) in conn.sent
        await b.outcome(second["id"])
        third = await b.order(dict(asset="EUR/USD OTC", dir="down", seconds=3, level=5))
        assert not third["ok"] and "tope de martingalas" in third["error"]
    run(go())


def test_one_order_at_a_time(tmp_path):
    async def go():
        conn = FakeConn()
        conn.release = asyncio.Event()  # Pocket has not closed the first order yet
        b, _ = bridge(tmp_path, conn)
        first = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3, level=0))
        assert b.status()["open"] == first["id"]
        again = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3, level=0))
        assert not again["ok"] and "una a la vez" in again["error"]
        conn.release.set()
        await b.outcome(first["id"])
        assert (await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3, level=0)))["ok"]
    run(go())


@pytest.mark.parametrize("payload, words", [
    (dict(asset="EUR/USD OTC", dir="sideways", seconds=3), "mal escrita"),
    (dict(asset="EUR/USD OTC", dir="up", seconds=0), "mal escrita"),
    (dict(asset="XXX/YYY OTC", dir="up", seconds=3), "no tiene el par"),
])
def test_bad_orders_are_refused_without_touching_pocket(tmp_path, payload, words):
    async def go():
        b, _ = bridge(tmp_path)
        answer = await b.order(payload)
        assert not answer["ok"] and words in answer["error"]
        assert not [s for s in b.conn.sent if s[0] == "order"]
    run(go())


def test_not_connected_no_balance_and_pockets_refusal(tmp_path):
    async def go():
        conn = FakeConn()
        conn.ready.clear()
        b, _ = bridge(tmp_path, conn)
        assert "no está conectada" in (await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3)))["error"]
        assert b.status()["connected"] is False and b.status()["balance"] is None

        b2, _ = bridge(tmp_path / "b", FakeConn(balance=0.5))
        assert "no alcanza" in (await b2.order(dict(asset="EUR/USD OTC", dir="up", seconds=3)))["error"]

        b3, store = bridge(tmp_path / "c", FakeConn(fail="Pocket Option rechazó la orden: asset closed"))
        answer = await b3.order(dict(asset="EUR/USD OTC", dir="up", seconds=3))
        assert not answer["ok"] and "asset closed" in answer["error"]
        assert b3.status()["open"] is None  # a refused order does not block the next one
        assert store.db.execute("SELECT kind FROM events ORDER BY id DESC LIMIT 1").fetchone()[0] == "LECTOR RECHAZO"
    run(go())


def test_the_door_needs_the_token_and_speaks_json(tmp_path):
    async def go():
        b, _ = bridge(tmp_path)
        assert (await b.route("GET", "/estado", {}, b""))[0] == 403
        assert (await b.route("GET", "/estado", {"x-lector-token": "otra"}, b""))[0] == 403
        ok = {"x-lector-token": "clave"}
        status, body = await b.route("GET", "/estado", ok, b"")
        assert status == 200 and body["demo"] is True and body["connected"] is True and body["levels"] == 4
        status, opened = await b.route("POST", "/orden", ok, json.dumps(dict(asset="EUR/USD OTC", dir="down", seconds=3)).encode())
        assert status == 200 and opened["ok"]
        status, done = await b.route("GET", f"/resultado?id={opened['id']}", ok, b"")
        assert status == 200 and done["result"] == "loss"  # ABAJO from 1.1 and it closed at 1.2
        assert (await b.route("GET", "/resultado?id=nada", ok, b""))[1]["ok"] is False
        assert (await b.route("POST", "/orden", ok, b"esto no es json"))[0] == 400
        assert (await b.route("GET", "/otra", ok, b""))[0] == 404
    run(go())


def test_a_real_round_trip_over_127_0_0_1(tmp_path):
    async def go():
        b, _ = bridge(tmp_path, cfg=dict(port=0))
        stop = asyncio.Event()
        task = asyncio.get_running_loop().create_task(b.listen(tmp_path, stop.is_set))
        card = tmp_path / "lector.json"
        for _ in range(100):
            if card.exists():
                break
            await asyncio.sleep(0.02)
        info = json.loads(card.read_text(encoding="utf-8"))
        assert info["token"] == "clave" and info["port"] > 0

        async def ask(method, target, token="clave", body=b""):
            reader, writer = await asyncio.open_connection("127.0.0.1", info["port"])
            writer.write(f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Lector-Token: {token}\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            raw = await reader.read()
            writer.close()
            head, _, data = raw.partition(b"\r\n\r\n")
            return int(head.split(b" ")[1]), json.loads(data)

        assert (await ask("GET", "/estado", token="mala"))[0] == 403
        status, state = await ask("GET", "/estado")
        assert status == 200 and state["connected"]
        status, opened = await ask("POST", "/orden", body=json.dumps(dict(asset="EUR/USD OTC", dir="up", seconds=3)).encode())
        assert status == 200 and opened["ok"] and opened["amount"] == 1.0
        assert (await ask("GET", f"/resultado?id={opened['id']}"))[1]["result"] == "win"
        stop.set()
        await task
        assert not card.exists()  # the card is removed when the bridge closes
    run(go())


def test_incorrect_exp_time_is_retried_once_and_a_silent_pocket_fails_fast(tmp_path, monkeypatch):
    from pocket_tyl import lector

    async def go():
        monkeypatch.setattr(lector, "RETRY_WAIT", 0.01)
        monkeypatch.setattr(lector, "ORDER_TIMEOUT", 0.05)
        conn = FakeConn()
        calls = []
        real_open = conn.open_order

        async def flaky(asset, direction, amount, seconds):
            calls.append(direction)
            if len(calls) == 1:
                raise RuntimeError("Pocket Option rechazó la orden: IncorrectExpTime")
            return await real_open(asset, direction, amount, seconds)
        conn.open_order = flaky
        b, store = bridge(tmp_path, conn)
        opened = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3))
        assert opened["ok"] and len(calls) == 2  # refused once, in on the second try
        await b.outcome(opened["id"])

        async def always(asset, direction, amount, seconds):
            raise RuntimeError("Pocket Option rechazó la orden: IncorrectExpTime")
        conn.open_order = always
        refused = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3))
        assert not refused["ok"] and "IncorrectExpTime" in refused["error"]  # only one retry

        async def silent(asset, direction, amount, seconds):
            await asyncio.sleep(1)
        conn.open_order = silent
        late = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3))
        assert not late["ok"] and "no contestó" in late["error"] and b.status()["open"] is None
    run(go())


def test_an_order_near_the_end_of_the_second_waits_and_the_answer_carries_pockets_close(tmp_path, monkeypatch):
    from pocket_tyl import lector

    async def go():
        conn = FakeConn()
        conn.hours = 7200
        conn.clock_offset = lambda: 0.1
        b, _ = bridge(tmp_path, conn)
        b.clock = lambda: 100.80  # 0.80 + 0.10 of travel = it would arrive at 0.90 of the second
        waited = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            waited.append(round(seconds, 2))
            await real_sleep(0)
        monkeypatch.setattr(lector.asyncio, "sleep", fake_sleep)
        real_open = conn.open_order

        async def with_close(asset, direction, amount, seconds):
            return dict(await real_open(asset, direction, amount, seconds), closeTimestamp=7200 + 104)
        conn.open_order = with_close
        opened = await b.order(dict(asset="EUR/USD OTC", dir="up", seconds=3))
        assert opened["ok"] and waited[0] == 0.13  # 1 − 0.90 + 0.03
        assert opened["close_ms"] == 104000  # Pocket's close second, on this PC's clock
    run(go())


# ---------- Which pair is on the chart (16 Nov 2026): prices of every pair for the panel ----------
def test_prices_lists_only_fresh_quotes_with_their_names(tmp_path):
    import time as clock

    async def go():
        conn = FakeConn()
        now = clock.time()
        conn.prices = {"EURUSD_otc": (now + 7200, 1.12072, now - 0.4), "AUDCAD_otc": (now + 7200, 1.05103, now - 60)}
        b, _store = bridge(tmp_path, conn)
        status, answer = await b.route("GET", "/precios", {"x-lector-token": "clave"}, b"")
        assert status == 200 and answer["ok"]
        assert [r[:3] for r in answer["prices"]] == [["EURUSD_otc", "EUR/USD OTC", 1.12072]]  # the stale one is left out
        assert answer["prices"][0][3] < 2 and answer["prices"][0][4] == 50
        status, _ = await b.route("GET", "/precios", {"x-lector-token": "otra"}, b"")
        assert status == 403
    run(go())


def test_the_watcher_asks_for_every_open_currency_pair_once(tmp_path):
    from pocket_tyl.lector import watch_pairs

    class Socket:
        def __init__(self):
            self.out = []

        async def send(self, text):
            self.out.append(text)

    async def go():
        conn = FakeConn()
        conn.ws, conn.streams = Socket(), {"EURUSD_otc"}
        conn.assets["XAUUSD"] = (1, "XAUUSD", "Gold", "commodity", 2, 80)
        conn.assets["GBPJPY_otc"] = (2, "GBPJPY_otc", "GBP/JPY OTC", "currency", 3, 92) + (0,) * 8 + (False,)  # closed
        turns = iter([False, False, True])
        said = []
        await watch_pairs(conn, lambda: next(turns), lambda kind, text: said.append((kind, text)), every=0, gap=0, kinds={"currency"})
        assert len(conn.ws.out) == 1 and '"AUDCAD_otc"' in conn.ws.out[0] and conn.ws.out[0].startswith('42["changeSymbol",')
        assert conn.streams == {"EURUSD_otc", "AUDCAD_otc"} and len(said) == 1
        turns = iter([False, True])  # every type (the default): gold is followed too, the closed pair still is not
        await watch_pairs(conn, lambda: next(turns), lambda kind, text: said.append((kind, text)), every=0, gap=0)
        assert conn.streams == {"EURUSD_otc", "AUDCAD_otc", "XAUUSD"}
    run(go())


def test_candles_are_built_from_ticks_and_filled_from_older_ones():
    from pocket_tyl.velas import CandleBook

    book = CandleBook(seconds=15, keep=5)
    for ts, price in ((100.0, 1.0), (104.0, 1.3), (104.5, 0.9), (105.0, 1.1), (119.9, 1.2), (120.0, 1.25)):
        book.on_tick("EURUSD_otc", ts, price, ts)
    closed, forming, opened, seen = book.get("EURUSD_otc")
    # 100–104.9 is the tail of one candle, 105–119.9 the next, 120 opens the forming one
    assert closed == [[1.0, 1.3, 0.9, 0.9], [1.1, 1.2, 1.1, 1.2]] and forming == [1.25] * 4 and opened == 120 and seen == 120.0
    # older recorded ticks go BEFORE; the one that falls in the first live candle is merged into it
    added = book.backfill("EURUSD_otc", [(70.0, 2.0), (80.0, 2.2), (91.0, 1.5), (99.0, 1.4), (101.0, 9.9)])
    closed, _, _, _ = book.get("EURUSD_otc")
    assert added == 2 and closed[:2] == [[2.0, 2.0, 2.0, 2.0], [2.2, 2.2, 2.2, 2.2]]
    assert closed[2] == [1.5, 1.5, 0.9, 0.9] and closed[3] == [1.1, 1.2, 1.1, 1.2]  # 90–104.9 is ONE candle; 101.0 was live already
    assert book.get("NADA") is None


def test_candles_route_gives_the_pairs_asked_for(tmp_path):
    import time as clock
    from pocket_tyl.velas import CandleBook

    async def go():
        conn = FakeConn()
        now = clock.time()
        conn.prices = {"EURUSD_otc": (now + 7200, 1.2, now - 0.4)}
        b, _store = bridge(tmp_path, conn)
        status, answer = await b.route("GET", "/velas?pares=EUR/USD%20OTC", {"x-lector-token": "clave"}, b"")
        assert status == 200 and not answer["ok"]  # no candle book: says so instead of failing
        b.candles = CandleBook()
        slot = int((now + 7200) // 15) * 15
        for ts, price in ((slot - 20, 1.0), (slot - 10, 1.1), (slot + 1, 1.2)):
            b.candles.on_tick("EURUSD_otc", ts, price, now)
        status, answer = await b.route("GET", "/velas?pares=EUR/USD%20OTC,NO/EXISTE", {"x-lector-token": "clave"}, b"")
        assert status == 200 and answer["ok"] and answer["seconds"] == 15 and len(answer["pairs"]) == 1
        symbol, name, payout, closed, forming, age, open_for = answer["pairs"][0]
        assert (symbol, name, payout) == ("EURUSD_otc", "EUR/USD OTC", 50)
        assert closed == [[1.0, 1.0, 1.0, 1.0], [1.1, 1.1, 1.1, 1.1]] and forming == [1.2] * 4 and age < 2 and 0 <= open_for < 15
    run(go())
