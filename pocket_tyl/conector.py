"""Pocket Option DEMO connector (Socket.IO over WebSocket), checked live on 21 Oct 2026: handshake `0{…}` → `40`,
the captured auth message, `successauth`, `successupdateBalance` {isDemo, balance}, `changeSymbol` → `updateStream`
[[asset, server_ts, price]], `openOrder` → `successopenOrder` {id, requestId, openPrice, percentProfit} /
`failopenOrder`, and `successcloseOrder` {deals: [{id, openPrice, closePrice, command, amount, percentProfit}]}.

DEMO by default: the auth must say isDemo 1, the balance must say isDemo 1, and every order carries isDemo 1. REAL only
when built with real=True (user, 4 Nov 2026, the GPT bot on his real account, started by him from «Iniciar Pocket GPT
REAL.bat»): then the auth and the balance must say isDemo 0 and the orders carry isDemo 0; the socket URL and user agent
are the ones the capture window saw. The SSID is never logged."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import statistics
import time

from .secret import demo_check, real_check
from .ws import WebSocket

DEMO_URL = "wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket"
REAL_URL = "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket"  # only if the capture saw no socket URL
# 4 Nov 2026 12:04: the page's own host (api-spb) answered 403 to this client while api-eu, api-msk, api-us-north and
# api-us-south accepted it (handshake only, no login): a REAL connection that is refused tries the next host.
REAL_HOSTS = ("api-eu", "api-msk", "api-us-north", "api-us-south")
REAL_URLS = tuple(f"wss://{h}.po.market/socket.io/?EIO=4&transport=websocket" for h in REAL_HOSTS)
HEADERS = {"Origin": "https://pocketoption.com",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
FRESH = 5.0  # seconds a price stays usable
SILENCE = 30.0  # seconds without any message from Pocket before the socket is treated as dead


def deal_net(deal: dict) -> float:
    """A closed deal's net in USD: win = amount × payout, loss = −amount, same price = 0 (refund)."""
    amount = float(deal.get("amount", 0))
    open_, close = float(deal.get("openPrice", 0)), float(deal.get("closePrice", 0))
    if close == open_:
        return 0.0
    up = close > open_
    won = up if int(deal.get("command", 0)) == 0 else not up
    return round(amount * float(deal.get("percentProfit", 0)) / 100, 2) if won else -amount


class PocketDemo:
    def __init__(self, ssid: str, log=lambda kind, text: None, connect=WebSocket.connect, url: str | None = None,
                 real: bool = False, user_agent: str | None = None):
        reason = (real_check if real else demo_check)(ssid)
        if reason:
            raise ValueError(reason)
        self.real = bool(real)
        self.ssid, self.log, self.connect_ws = ssid.strip(), log, connect
        self.url = url or (REAL_URL if real else DEMO_URL)
        self.urls = ([self.url] + [u for u in REAL_URLS if u != self.url]) if real else [self.url]
        self.headers = dict(HEADERS, **({"User-Agent": user_agent} if user_agent else {}))
        self.label = "POCKET REAL" if real else "POCKET DEMO"
        self.ws = None
        self.ready = asyncio.Event()
        self.balance = None
        self.prices: dict[str, tuple[float, float, float]] = {}  # asset → (server ts, price, local ts)
        self.offsets = deque(maxlen=120)
        self.hours = None  # the server's timestamps run whole hours ahead of real time (+2 h on 21 Oct 2026)
        self.opening: dict[int, asyncio.Future] = {}
        self.closing: dict[str, asyncio.Future] = {}
        self.on_closed = None  # optional: called with every deal Pocket reports closed on this account (manual ones too)
        self.streams: set[str] = set()  # changeSymbol adds a stream; earlier ones keep flowing
        self.assets: dict[str, tuple] = {}  # symbol → its row in updateAssets (payout, open…)
        self.ticks_seen: dict[str, int] = {}
        self.listeners: list = []  # f(asset, server_ts, price, local_ts) on every tick (the 15-s strategy, 23 Oct 2026)
        self.request = int(time.time() * 1000) % 1_000_000_000

    # ---------- clock ----------
    def latency(self) -> float:
        """Server timestamp minus our receive time, without the whole hours: about −(one-way delay)."""
        return statistics.median(self.offsets) if self.offsets else 0.0

    def clock_offset(self) -> float:
        """Seconds to add to the PC clock so an order sent at a whole second ARRIVES then (bounded to half a second)."""
        return max(0.0, min(0.5, -self.latency()))

    # ---------- reading ----------
    async def run(self) -> None:
        """Connect, log in and read until the connection ends (the caller reconnects)."""
        self.ready.clear()
        try:
            self.ws = await self.connect_ws(self.url, self.headers)
        except ConnectionError as exc:
            if self.real and len(self.urls) > 1 and any(code in str(exc) for code in ("403", "500", "502", "503")):
                old = self.url
                self.url = self.urls[(self.urls.index(old) + 1) % len(self.urls)]
                self.log(self.label, f"El servidor {old.split('/')[2]} rechazó la conexión ({exc}); se prueba {self.url.split('/')[2]}.")
            raise
        pending = None
        keepalive = asyncio.get_running_loop().create_task(self.keepalive())
        try:
            while True:
                try:
                    # 21 Oct 2026: after a Wi-Fi drop the socket stayed "up" but silent and GBPAUD_otc got no price.
                    # The server pings every 25 s and EURUSD_otc ticks twice a second: 30 s of silence = dead socket.
                    message = await asyncio.wait_for(self.ws.recv(), SILENCE)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"Pocket Option no mandó nada en {SILENCE:g} s: conexión muerta, se reconecta.")
                if isinstance(message, str):
                    if message.startswith("0{"):
                        await self.ws.send("40")
                    elif message == "2":
                        await self.ws.send("3")
                    elif message.startswith("40"):
                        await self.ws.send(self.ssid)
                    elif message.startswith("451-"):
                        pending = json.loads(message[4:])[0]
                    elif message.startswith("42"):
                        event = json.loads(message[2:])
                        self.handle(event[0], event[1] if len(event) > 1 else None)
                    elif message.startswith("41") or message.startswith("44"):
                        raise ConnectionError(f"Pocket Option cerró la sesión ({message[:60]}).")
                else:
                    name, pending = pending, None
                    try:
                        data = json.loads(message)
                    except ValueError:
                        continue
                    self.handle(name, data)
        finally:
            keepalive.cancel()
            self.ready.clear()
            for f in list(self.opening.values()) + list(self.closing.values()):
                if not f.done():
                    f.set_exception(ConnectionError("Se cortó la conexión con Pocket Option."))
            self.opening.clear()
            self.streams.clear()
            try:
                await self.ws.close()
            except Exception:
                pass

    async def keepalive(self, every: float = 20.0) -> None:
        """The web client's "ps" heartbeat (21 Oct 2026: without it the server closed the socket with "41" about three
        minutes after connecting, at a whole minute)."""
        while True:
            await asyncio.sleep(every)
            if self.ws is not None and self.ready.is_set():
                try:
                    await self.ws.send('42["ps"]')
                except Exception:
                    return

    def handle(self, name, data) -> None:
        if self.opening and name not in ("updateStream", "updateHistoryNewFast", "loadHistoryPeriodFast", "loadHistoryPeriod",
                                         "updateAssets", "successupdateBalance", "successopenOrder"):
            # 15 Nov 2026 (Lector bridge): one 3-s order was refused with IncorrectExpTime and the next got no answer in
            # 8 s. While an order waits for its confirmation, what Pocket sends is kept (no session data comes in these).
            self.log(self.label, f"(orden en espera) {name}: {str(data)[:300]}")
        if name == "successupdateBalance" and isinstance(data, dict):
            want = 0 if self.real else 1
            if int(data.get("isDemo", -1)) != want:
                self.ready.clear()
                raise ConnectionError(f"La sesión NO es de la cuenta {'REAL' if self.real else 'DEMO'}: se bloquea y se desconecta.")
            self.balance = float(data.get("balance", 0))
            if not self.ready.is_set():
                self.log(self.label, f"Conectado a la cuenta {'REAL' if self.real else 'DEMO'} · saldo {self.balance:,.2f}")
            self.ready.set()
        elif name == "updateStream" and isinstance(data, list):
            now = time.time()
            for row in data:
                if isinstance(row, list) and len(row) >= 3:
                    ts, price = float(row[1]), float(row[2])
                    raw = ts - now
                    if self.hours is None:
                        self.hours = round(raw / 3600) * 3600
                    self.offsets.append(raw - self.hours)
                    self.prices[str(row[0])] = (ts, price, now)
                    self.ticks_seen[str(row[0])] = self.ticks_seen.get(str(row[0]), 0) + 1
                    for listener in self.listeners:
                        try:
                            listener(str(row[0]), ts, price, now)
                        except Exception:
                            pass
        elif name in ("updateHistoryNewFast", "loadHistoryPeriodFast", "loadHistoryPeriod") and isinstance(data, dict):
            # The recent ticks sent on a subscription: its last one is a price too (21 Oct 2026, GBPAUD_otc had none).
            history = data.get("history") or data.get("data") or []
            if data.get("asset") and history and isinstance(history[-1], (list, tuple)) and len(history[-1]) >= 2:
                ts, price = float(history[-1][0]), float(history[-1][1])
                if self.hours is not None and abs(ts - self.hours - time.time()) < FRESH:
                    self.prices.setdefault(str(data["asset"]), (ts, price, time.time()))
        elif name == "updateAssets" and isinstance(data, list):
            for row in data:
                if isinstance(row, list) and len(row) > 2:
                    self.assets[str(row[1])] = tuple(row)
        elif name == "successopenOrder" and isinstance(data, dict):
            f = self.opening.pop(int(data.get("requestId", -1)), None)
            if f and not f.done():
                f.set_result(data)
        elif name == "failopenOrder":
            info = data if isinstance(data, dict) else {}
            f = self.opening.pop(int(info.get("requestId", -1)), None) if info.get("requestId") is not None else None
            if f is None and self.opening:  # no request id in the answer: the oldest order waiting
                f = self.opening.pop(next(iter(self.opening)))
            if f and not f.done():
                f.set_exception(RuntimeError(f"Pocket Option rechazó la orden: {info.get('error', info) or 'sin detalle'}"))
        elif name == "successcloseOrder" and isinstance(data, dict):
            for deal in data.get("deals") or []:
                f = self.closing.pop(str(deal.get("id")), None)
                if f and not f.done():
                    f.set_result(deal)
                if self.on_closed is not None:
                    try:
                        self.on_closed(deal, bool(f))
                    except Exception:  # a recorder must never break the connection
                        pass

    # ---------- for the trigger ----------
    def price(self, asset: str) -> float | None:
        row = self.prices.get(asset)
        return row[1] if row and time.time() - row[2] <= FRESH else None

    async def subscribe(self, asset: str) -> None:
        """Live price of `asset` (checked 21 Oct 2026: each changeSymbol adds a stream; "subfor" only sends sentiment)."""
        if self.ws is not None and self.ready.is_set() and asset not in self.streams:
            await self.ws.send('42["changeSymbol",' + json.dumps(dict(asset=asset, period=60), separators=(",", ":")) + "]")
            self.streams.add(asset)
            row = self.assets.get(asset)
            self.log(self.label, f"Suscrito a {asset}" + (f" · en la lista de Pocket: {list(row[:16])[:6]} … abierto={row[14] if len(row) > 14 else '?'}"
                                                             if row else " · ⚠️ ese par NO aparece en la lista de activos de Pocket"))

    def diagnose(self, asset: str) -> str:
        """Why there is no price: which pairs are streaming and whether `asset` exists for Pocket."""
        names = {k: v for k, v in self.ticks_seen.items()}
        like = [k for k in self.assets if asset.split("_")[0][:6] in k.replace("/", "")]
        return f"ticks recibidos {names} · parecidos en la lista: {like[:6]}"

    async def open_order(self, asset: str, direction: str, amount: float, seconds: int) -> dict:
        if not self.ready.is_set():
            raise ConnectionError(f"Pocket Option {'REAL' if self.real else 'DEMO'} no está conectado.")
        self.request += 1
        rid = self.request
        future = asyncio.get_running_loop().create_future()
        self.opening[rid] = future
        order = ["openOrder", dict(asset=asset, amount=round(float(amount), 2), action="call" if direction == "ARRIBA" else "put",
                                   isDemo=0 if self.real else 1, requestId=rid, optionType=100, time=int(seconds))]
        await self.ws.send("42" + json.dumps(order, separators=(",", ":")))
        try:
            opened = await asyncio.wait_for(future, 8)
        finally:
            self.opening.pop(rid, None)
        self.closing[str(opened["id"])] = asyncio.get_running_loop().create_future()
        return opened

    async def result(self, order_id: str, timeout: float) -> dict:
        future = self.closing.get(str(order_id))
        if future is None:
            raise KeyError(order_id)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.closing.pop(str(order_id), None)
