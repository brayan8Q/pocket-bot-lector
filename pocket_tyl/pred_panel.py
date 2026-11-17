"""Clean window for OUR 15-s predictor only (user, 4 Nov 2026 ~14:00: "limpia el panel para que arranque desde cero"):
connection, DEMO balance, the predictor's DEMO orders and result, its model's out-of-sample hits, the rules per pair and
the latest events. Read-only on pocket_tyl.sqlite (PocketTyl folder); nothing here can open or close an order."""
from __future__ import annotations

from datetime import datetime
import sqlite3
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QAbstractItemView, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QListWidget, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .core import MEXICO
from .gpt_panel import GptPanel
from .panel import AMBER, GREEN, RED, card, chip, lbl, usd
from .predictor import FOCUS_DROP, FOCUS_ORDERS, FOCUS_PASS, HORIZON, MIN_N, MIN_PAYOUT, MIN_RATE
from .store import data_dir


def table(headers, height=240):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().hide()
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    t.setMinimumHeight(height)
    return t


class PredPanel(QWidget):
    render_predictor = GptPanel.render_predictor

    def __init__(self, root=None, parent=None):
        super().__init__(parent)
        self.root = root or data_dir()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        box = QVBoxLayout(content)
        box.setContentsMargins(24, 22, 24, 20)
        box.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(lbl(f"K-MIDAS · Predictor propio {HORIZON} s · DEMO", "title"))
        head.addStretch()
        self.state_chip = QLabel()
        chip(self.state_chip, "—")
        head.addWidget(self.state_chip)
        box.addLayout(head)
        top = QGridLayout()
        top.setSpacing(12)
        self.tiles = {}
        for i, (key, name) in enumerate((("conexion", "CONEXIÓN DEMO"), ("saldo", "SALDO DEMO"),
                                         ("ordenes", f"ÓRDENES DEL MODELO EN DEMO · {HORIZON} s · $1"),
                                         ("modelo", f"VEREDICTO ({FOCUS_ORDERS} ÓRDENES: ≥ {FOCUS_PASS:.0%} PASA · "
                                                    f"< {FOCUS_DROP:.0%} SE DESCARTA)"))):
            f, b = card()
            b.addWidget(lbl(name, "eyebrow"))
            v, n = lbl("—", "value"), lbl("", "subtitle", True)
            b.addWidget(v)
            b.addWidget(n)
            top.addWidget(f, 0, i)
            self.tiles[key] = (v, n)
        box.addLayout(top)
        # the predictor card (same widgets GptPanel.render_predictor fills)
        f, b = card()
        self.pred_card = f
        h = QHBoxLayout()
        h.addWidget(lbl("CÓMO APRENDE · REGLAS POR PAR", "eyebrow"))
        h.addStretch()
        self.pred_chip = QLabel()
        chip(self.pred_chip, "Aprendiendo")
        h.addWidget(self.pred_chip)
        b.addLayout(h)
        self.pred_note = lbl("", "subtitle", True)
        b.addWidget(self.pred_note)
        self.pred_total = lbl("", "sectionTitle", True)
        b.addWidget(self.pred_total)
        self.pred = table(("Par", "Mejor regla hasta ahora", "Récord de esa regla", "Predicciones nuevas del modelo",
                           "Aciertos / fallos / empates", "Aciertos sin empates"), 280)
        b.addWidget(self.pred)
        box.addWidget(f)
        f, b = card()
        b.addWidget(lbl(f"ÓRDENES EN DEMO ($1 · {HORIZON} s · SIN MARTINGALA · PAGO ≥ {MIN_PAYOUT:g} %)", "eyebrow"))
        self.orders = table(("Hora MX", "Par", "Regla", "Dirección", "Pago", "Abrió", "Cerró", "Resultado", "Neto"), 300)
        b.addWidget(self.orders)
        box.addWidget(f)
        f, b = card()
        b.addWidget(lbl("LO QUE VA PASANDO", "eyebrow"))
        self.log = QListWidget()
        self.log.setMinimumHeight(160)
        b.addWidget(self.log)
        box.addWidget(f)
        box.addStretch()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def connect(self):
        path = self.root / "pocket_tyl.sqlite"
        if not path.exists():
            return None
        try:
            return sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True, timeout=0.5)
        except sqlite3.Error:
            return None

    def tile(self, key, value, note="", color=None):
        v, n = self.tiles[key]
        v.setText(value)
        v.setStyleSheet(f"color: {color};" if color else "")
        n.setText(note)

    def refresh(self):
        try:
            self.render()
        except Exception as exc:
            self.tile("conexion", "Sin datos", f"{type(exc).__name__}: {exc}", AMBER)

    def render(self):
        db = self.connect()
        if db is None:
            self.tile("conexion", "Sin datos", "El predictor todavía no arranca.", AMBER)
            return
        try:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            now = time.time()
            rec_at = float(meta.get("grabador_at") or 0)
            alive = now - rec_at < 30
            chip(self.state_chip, "Aprendiendo y operando en DEMO" if alive else "Detenido", "win" if alive else "muted")
            if not alive:
                self.tile("conexion", "Detenido", "Enciéndelo con «Grabar ticks Pocket.bat».", AMBER)
            elif meta.get("mode") == "demo_conectado":
                self.tile("conexion", "Conectado", f"{meta.get('grabador_pares') or 0} pares · "
                          f"{int(meta.get('grabador_ticks_hoy') or 0):,} ticks grabados hoy", GREEN)
            else:
                self.tile("conexion", "Conectando…", "", AMBER)
            bal = meta.get("saldo_demo")
            self.tile("saldo", usd(float(bal)) if bal not in (None, "", "None") else "—", "Cuenta DEMO de Pocket")
            has = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pred_orders'").fetchone() is not None
            rows = db.execute("SELECT at, pair, rule, side, payout, open_price, close_price, result, net, state FROM pred_orders "
                              "ORDER BY id DESC LIMIT 300").fetchall() if has else []
            # the whole series (not only the rows shown): result, count, net and average payout
            agg = db.execute("SELECT result, COUNT(*), COALESCE(SUM(net), 0), AVG(payout) FROM pred_orders "
                             "WHERE result IS NOT NULL GROUP BY result").fetchall() if has else []
            cnt = {r: c for r, c, _, _ in agg}
            w, l, e = cnt.get("ganó", 0), cnt.get("perdió", 0), cnt.get("empate", 0)
            done = w + l + e
            net = sum(s for _, _, s, _ in agg)
            pay = sum(c * (a or 0) for _, c, _, a in agg) / done if done else None
            opened = sum(1 for r in rows if r[9] in ("abierta", "enviando"))
            d = w + l
            p = w / d if d else 0.0
            half = 1.96 * (p * (1 - p) / d) ** 0.5 if d else 0.0
            need = (f"\nPago medio {pay:.0f} %: para ganar dinero hace falta acertar más de {100 / (1 + pay / 100):.1f} %"
                    if pay else f"\nAbre solo cuando un par ya tiene modelo y Pocket paga ≥ {MIN_PAYOUT:g} %")
            self.tile("ordenes", f"{100 * p:.1f} %" if d else "—",
                      f"Órdenes: {done} · {w} ganadas / {l} perdidas / {e} empates"
                      + (f" · rango real {100 * max(p - half, 0):.0f}–{100 * min(p + half, 1):.0f} %" if d else "")
                      + f" · neto {usd(net, True)} · {opened} abierta(s){need}",
                      GREEN if d and p >= FOCUS_PASS else RED if d and p < FOCUS_DROP else None)
            fwd = db.execute("SELECT result, COUNT(*) FROM pred_forward GROUP BY result").fetchall() \
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pred_forward'").fetchone() else []
            f = dict(fwd)
            fa, ff, fe = f.get("acierto", 0), f.get("fallo", 0), f.get("empate", 0)
            if done < FOCUS_ORDERS:
                verdict, color = f"Midiendo ({done}/{FOCUS_ORDERS})", None
            elif p >= FOCUS_PASS:
                verdict, color = "PASA ✅", GREEN
            elif p < FOCUS_DROP:
                verdict, color = "SE DESCARTA ❌", RED
            else:
                verdict, color = "En duda: seguir probando", AMBER
            paper_txt = (f"En papel el modelo va en {100 * fa / (fa + ff):.1f} % ({fa} aciertos / {ff} fallos / {fe} empates)"
                         if fa + ff else "En papel el modelo todavía no tiene predicciones nuevas")
            self.tile("modelo", verdict, f"{paper_txt}. Cada {HORIZON} s juzga 6 reglas por par; un par opera cuando su mejor "
                      f"regla lleva {MIN_N} juicios con {MIN_RATE:.0%} o más.", color)
            self.render_predictor(db)
            self.orders.setRowCount(len(rows))
            for i, (at, pair, rule, side, pay, op, cp, res, n, state) in enumerate(rows):
                values = (datetime.fromtimestamp(at, MEXICO).strftime("%H:%M:%S"), pair.replace("_otc", " OTC"), rule,
                          "CALL ⬆" if side > 0 else "PUT ⬇", f"{pay:g} %" if pay else "—", f"{op}" if op else "—",
                          f"{cp}" if cp else "—", res or state or "", usd(n, True) if n is not None else "—")
                for c, v in enumerate(values):
                    item = QTableWidgetItem(v)
                    if c == 8 and n:
                        item.setForeground(Qt.GlobalColor.green if n > 0 else Qt.GlobalColor.red)
                    self.orders.setItem(i, c, item)
            self.log.clear()
            for t, k, d in db.execute("SELECT created, kind, detail FROM events WHERE kind IN ('GRABADOR','POCKET DEMO','ATENCIÓN',"
                                      "'ERROR','DETENIDO') ORDER BY id DESC LIMIT 30"):
                self.log.addItem(f"{datetime.fromtimestamp(t, MEXICO):%H:%M:%S}  {k}  ·  {d[:200]}")
        finally:
            db.close()
