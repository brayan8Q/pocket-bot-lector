"""Tick recorder for our own 15-s prediction bot (user, 4 Nov 2026: "sí hazlo, esto es independiente… al bot de oro"):
every price Pocket Option streams for its OTC pairs is appended to %LOCALAPPDATA%\\PocketTyl\\ticks_otc\\<day>\\<asset>.csv
(server_ts,price,local_ts; day = Mexico date), and once a minute each pair's payout to payouts.csv in the same folder.
Nothing here opens an order or touches the gold bot / MT5.

Runs inside the GPT DEMO test (it already streams the bot's 25 pairs) or alone with `python -m pocket_tyl.grabador`
(«Grabar ticks Pocket.bat»), which takes the Pocket engine lock so the two never share the DEMO session."""
from __future__ import annotations

import asyncio
from collections import defaultdict
import datetime as dt
import msvcrt
import os
from pathlib import Path
import sys
import time

MX = dt.timezone(dt.timedelta(hours=-6))
FLUSH = 5.0  # seconds between writes
PAYOUTS_EVERY = 60.0


class TickRecorder:
    def __init__(self, root: Path, conn=None, store=None):
        self.dir = Path(root) / "ticks_otc"
        self.conn, self.store = conn, store
        self.buf: dict[str, list[str]] = defaultdict(list)
        self.last_flush = time.time()
        self.last_payouts = 0.0
        self.count = 0
        self.day = None
        self.day_count = 0

    def on_tick(self, asset, server_ts, price, local_ts) -> None:
        if getattr(self, "only_otc", True) and not str(asset).endswith("_otc"):  # the Lector bridge also keeps the real pairs (16 Nov 2026)
            return
        self.buf[str(asset)].append(f"{float(server_ts):.3f},{price},{float(local_ts):.3f}\n")
        self.count += 1
        if local_ts - self.last_flush >= FLUSH:
            self.flush(local_ts)

    def folder(self, now: float) -> Path:
        day = dt.datetime.fromtimestamp(now, MX).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_count = day, 0
        d = self.dir / day
        d.mkdir(parents=True, exist_ok=True)
        return d

    def flush(self, now: float | None = None) -> None:
        now = now or time.time()
        d = self.folder(now)
        written = 0
        for asset, lines in self.buf.items():
            if lines:
                with open(d / f"{asset}.csv", "a", encoding="ascii") as f:
                    f.writelines(lines)
                written += len(lines)
        self.buf.clear()
        self.day_count += written
        self.last_flush = now
        if self.conn is not None and now - self.last_payouts >= PAYOUTS_EVERY:
            self.last_payouts = now
            rows = []
            for sym, row in (getattr(self.conn, "assets", None) or {}).items():
                if str(sym).endswith("_otc") and len(row) > 5 and row[5]:
                    rows.append(f"{now:.0f},{sym},{row[5]}\n")
            if rows:
                with open(d / "payouts.csv", "a", encoding="ascii") as f:
                    f.writelines(rows)
        if self.store is not None:
            try:
                self.store.put("grabador_dia", self.day)
                self.store.put("grabador_ticks_hoy", self.day_count)
                self.store.put("grabador_pares", len([p for p in d.glob("*_otc.csv")]))
                self.store.put("grabador_at", now)
            except Exception:
                pass


def main() -> None:
    """Alone: own DEMO connection, all OTC pairs Pocket lists subscribed, nothing played."""
    from .conector import PocketDemo
    from .engine import DemoRunner, quick_edit_off, stop_requested
    from .secret import demo_check, has_ssid, protect
    from .store import Store, data_dir, load_config, plan_of
    from .gpt import BOT_PAIRS
    from .anna import asset_of
    root = data_dir()
    lock = open(root / "engine.lock", "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("Ya hay un proceso de Pocket usando la DEMO (la prueba GPT ya graba los ticks).")
        sys.exit(0)
    quick_edit_off()
    store = Store(root / "pocket_tyl.sqlite")
    if not has_ssid(root):
        print("No hay sesión DEMO: abre «Conectar Pocket Option.bat» y presiona «Abrir sala DEMO».")
        return
    ssid = protect((root / "ssid.bin").read_bytes(), decrypt=True).decode("utf-8")
    if demo_check(ssid) is not None:
        print("La sesión guardada no es DEMO; no graba.")
        return
    conn = PocketDemo(ssid, log=store.event)
    demo = DemoRunner(store, plan_of(load_config(root)), conn)
    rec = TickRecorder(root, conn, store)
    conn.listeners.append(rec.on_tick)
    from .predictor import PaperPredictor
    pred = PaperPredictor(store, conn, trade=True)  # predicts every HORIZON s (5 s since 5 Nov 2026) and trades its model in DEMO
    conn.listeners.append(pred.on_tick)
    from .predictor import HORIZON, MAX_OPEN, MIN_PAYOUT
    store.event("GRABADOR", f"Grabando los ticks de los pares OTC, prediciendo cada {HORIZON} s y operando en DEMO $1 a {HORIZON} s "
                            f"las predicciones del modelo de cada par (pago ≥ {MIN_PAYOUT:g} %, máx. {MAX_OPEN} a la vez).")

    async def go():
        stop = lambda: stop_requested(store)
        keeper = asyncio.get_running_loop().create_task(demo.keep(stop))
        try:
            while not stop():
                if conn.ready.is_set():
                    quiet, conn.log = conn.log, (lambda kind, text: None)
                    try:
                        for label in BOT_PAIRS:
                            a = asset_of(label)
                            if a in conn.assets and a not in conn.streams:
                                await conn.subscribe(a)
                    finally:
                        conn.log = quiet
                await asyncio.sleep(10)
        finally:
            keeper.cancel()
            rec.flush()
    try:
        asyncio.run(go())
    finally:
        store.close()


if __name__ == "__main__":
    main()
