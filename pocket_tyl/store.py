"""pocket_tyl.sqlite: everything the Pocket engine knows. Written only by `python -m pocket_tyl`; the dashboard reads it
read-only. Separate from SignalBridge's journal.sqlite."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time

from .core import Book, Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS signals (
  message INTEGER PRIMARY KEY, pair TEXT, direction TEXT, entry_at REAL, gale1_at REAL, gale2_at REAL, day TEXT,
  state TEXT, result TEXT, detail TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message INTEGER, pair TEXT, direction TEXT, player INTEGER, stake REAL,
  won INTEGER, net REAL, closed_at REAL, day TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS books (player INTEGER PRIMARY KEY, debt REAL, step INTEGER, updated REAL);
CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, net REAL, stopped TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, kind TEXT, detail TEXT);
"""


def data_dir() -> Path:
    override = os.environ.get("POCKET_TYL_DIR")
    return Path(override) if override else Path(os.environ.get("LOCALAPPDATA", ".")) / "PocketTyl"


def signalbridge_dir() -> Path:
    """SignalBridge's folder, used READ-ONLY for the Telegram login (never written)."""
    return Path(os.environ.get("LOCALAPPDATA", ".")) / "SignalBridge"


DEFAULTS = dict(mode="papel", j2=[2.0, 4.0], j3=[4.4, 8.8], payout=0.92, target=20.0, loss_stop=-40.0,
                players=[2, 3], channel="Tyl trader - Grupo VIP", channel_title="Tyl trader - Grupo VIP 📊",
                poll_seconds=5.0, grace_seconds=20.0)
# The dashboard page the gold app loaded before 22 Oct 2026 shows each player's stake as base × ladder[its book's step]
# and cannot be reloaded without restarting the gold app: these keys, with the legacy rows 2 and 3 that save_book keeps
# (J2 at the level, J3 at the level + 2), make that page show the right J2/J3 stakes until then.
LEGACY_PAGE = dict(base=2.0, ladder=[1.0, 2.0, 2.2, 4.4])


def load_config(root: Path) -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((root / "config.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if cfg.get("mode") not in ("papel", "demo"):
        cfg["mode"] = "papel"  # a real account is never allowed here
    return cfg


def save_config(root: Path, cfg: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "config.json.tmp"
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(root / "config.json")


def plan_of(cfg: dict) -> Plan:
    j2, j3 = [float(x) for x in cfg["j2"]], [float(x) for x in cfg["j3"]]
    if not j2 or len(j2) != len(j3) or min(j2 + j3) <= 0:
        raise ValueError("Montos de J2/J3 inválidos en la configuración de Pocket Tyl.")
    # first_candle 1 (user, 23 Oct 2026): the same two stakes play the 1st candle (J1) and the 1st martingale (J2).
    first = 1 if int(cfg.get("first_candle", 2)) == 1 else 2
    players = (first, first + 1) if first == 1 else tuple(int(p) for p in cfg["players"])
    return Plan(stakes={first: tuple(j2), first + 1: tuple(j3)}, payout=float(cfg["payout"]), target=float(cfg["target"]),
                loss_stop=float(cfg["loss_stop"]), players=players)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, str(value)))

    def event(self, kind, detail):
        with self.db:
            self.db.execute("INSERT INTO events(created,kind,detail) VALUES(?,?,?)", (time.time(), kind, detail))

    def book(self) -> Book:
        """The signals' shared book (row 0)."""
        row = self.db.execute("SELECT debt, step FROM books WHERE player=0").fetchone()
        return Book(0, row[0], row[1]) if row else Book(0)

    def save_book(self, book: Book, levels: int = 2):
        now = time.time()
        rows = [(0, book.debt, book.step), (2, book.debt, book.step), (3, book.debt, book.step + levels)]  # 2, 3: LEGACY_PAGE
        with self.db:
            self.db.executemany("INSERT INTO books(player,debt,step,updated) VALUES(?,?,?,?) ON CONFLICT(player) DO UPDATE SET "
                                "debt=excluded.debt, step=excluded.step, updated=excluded.updated",
                                [(p, d, st, now) for p, d, st in rows])

    def day(self, day) -> tuple[float, str | None]:
        row = self.db.execute("SELECT net, stopped FROM days WHERE day=?", (day,)).fetchone()
        return (row[0], row[1]) if row else (0.0, None)

    def save_day(self, day, net, stopped):
        with self.db:
            self.db.execute("INSERT INTO days(day,net,stopped) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET "
                            "net=excluded.net, stopped=excluded.stopped", (day, net, stopped))

    def add_signal(self, s, state, detail=""):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO signals(message,pair,direction,entry_at,gale1_at,gale2_at,day,state,result,"
                            "detail,updated) VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
                            (s.message, s.pair, s.direction, s.entry_at, s.gale1_at, s.gale2_at, s.day, state, detail, time.time()))

    def set_state(self, message, state, detail="", result=None):
        with self.db:
            if result is None:
                self.db.execute("UPDATE signals SET state=?, detail=?, updated=? WHERE message=?", (state, detail, time.time(), message))
            else:
                self.db.execute("UPDATE signals SET state=?, detail=?, result=?, updated=? WHERE message=?",
                                (state, detail, result, time.time(), message))

    def open_signals(self):
        return self.db.execute("SELECT message,pair,direction,entry_at,gale1_at,gale2_at,day,state FROM signals "
                               "WHERE result IS NULL ORDER BY message").fetchall()

    def add_op(self, message, pair, direction, player, stake, won, net, day, mode):
        with self.db:
            self.db.execute("INSERT INTO ops(message,pair,direction,player,stake,won,net,closed_at,day,mode) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (message, pair, direction, player, stake, int(won), net, time.time(), day, mode))
