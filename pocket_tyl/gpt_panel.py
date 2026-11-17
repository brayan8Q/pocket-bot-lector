"""Clean window for the TRADING GPT BOT only (user, 4 Nov 2026: "¿dónde está el panel limpio para eso?"): connection,
balance, today's result and daily brake, the play in course, the scoreboard, every play and the latest events. It only
READS pocket_tyl.sqlite of its folder (PocketTylReal for the real account, PocketTyl for the demo); nothing here can
open, close or change an order."""
from __future__ import annotations

from datetime import datetime
import sqlite3
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QAbstractItemView, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QListWidget, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .core import MEXICO
from .gpt import DEFAULTS, REAL, day_start, duration_label
from .panel import GREEN, RED, AMBER, PocketTylPanel, card, chip, lbl, usd
from .store import data_dir


class GptPanel(QWidget):
    build_gpt = PocketTylPanel.build_gpt
    render_gpt = PocketTylPanel.render_gpt

    def __init__(self, root=None, parent=None):
        super().__init__(parent)
        self.root = root or data_dir()
        self.engine_alive = False
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
        self.title = lbl("K-MIDAS · Pocket GPT", "title")
        head.addWidget(self.title)
        head.addStretch()
        self.mode_chip = QLabel()
        chip(self.mode_chip, "—")
        head.addWidget(self.mode_chip)
        box.addLayout(head)
        top = QGridLayout()
        top.setSpacing(12)
        self.tiles = {}
        for i, (key, name) in enumerate((("conexion", "CONEXIÓN"), ("saldo", "SALDO"), ("hoy", "RESULTADO DESDE EL INICIO LIMPIO"),
                                         ("freno", "FRENO Y META DEL DÍA"))):
            f, b = card()
            b.addWidget(lbl(name, "eyebrow"))
            value = lbl("—", "value")
            note = lbl("", "subtitle", True)
            b.addWidget(value)
            b.addWidget(note)
            top.addWidget(f, 0, i)
            self.tiles[key] = (value, note)
        box.addLayout(top)
        f, b = card()
        head2 = QHBoxLayout()
        head2.addWidget(lbl("PAGOS OTC AHORA EN ESTA CUENTA (LOS MEJORES PRIMERO)", "eyebrow"))
        head2.addStretch()
        self.pay_note = lbl("", "subtitle")
        head2.addWidget(self.pay_note)
        b.addLayout(head2)
        self.pay_grid = QGridLayout()
        self.pay_cells = []
        for k in range(12):
            cell = lbl("", "sectionTitle")
            self.pay_grid.addWidget(cell, k // 4, k % 4)
            self.pay_cells.append(cell)
        b.addLayout(self.pay_grid)
        b.addWidget(lbl("En la DEMO el bot juega con el pago que haya (desde el 5 nov 10:09); en la cuenta REAL solo con 85 % o "
                        "más. Con pago P hace falta acertar más de 1/(1+P): 52.1 % al 92 %, 60.2 % al 66 %. Para cambiar de par, "
                        f"elígelo en el menú del bot (Atrás → par → {duration_label(DEFAULTS['count_seconds'])}).", "subtitle", True))
        box.addWidget(f)
        f, b = card()
        head3 = QHBoxLayout()
        head3.addWidget(lbl("POR PAR · ESTABILIDAD Y RESULTADOS (DESDE EL INICIO LIMPIO)", "eyebrow"))
        head3.addStretch()
        self.rank_note = lbl("", "subtitle")
        head3.addWidget(self.rank_note)
        b.addLayout(head3)
        self.pairs = QTableWidget(0, 8)
        self.pairs.setHorizontalHeaderLabels(("Par", "Movimiento en 15 s (más bajo = más estable)", "Pago", "Señales GPT jugadas",
                                              "Ganadas", "Win rate GPT", "Rango probable (95 %)", "Neto a $1"))
        self.pairs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pairs.verticalHeader().hide()
        self.pairs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pairs.setMinimumHeight(220)
        b.addWidget(self.pairs)
        box.addWidget(f)
        f, b = card()
        self.pred_card = f
        head4 = QHBoxLayout()
        from .predictor import HORIZON
        head4.addWidget(lbl(f"NUESTRO PREDICTOR A {HORIZON} s · EN PAPEL (SIN ÓRDENES)", "eyebrow"))
        head4.addStretch()
        self.pred_chip = QLabel()
        chip(self.pred_chip, "Aprendiendo")
        head4.addWidget(self.pred_chip)
        b.addLayout(head4)
        self.pred_note = lbl("", "subtitle", True)
        b.addWidget(self.pred_note)
        self.pred_total = lbl("", "sectionTitle", True)
        b.addWidget(self.pred_total)
        self.pred = QTableWidget(0, 6)
        self.pred.setHorizontalHeaderLabels(("Par", "Mejor regla hasta ahora", "Récord de esa regla", "Predicciones nuevas del modelo",
                                             "Aciertos / fallos / empates", "Aciertos sin empates"))
        self.pred.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pred.verticalHeader().hide()
        self.pred.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pred.setMinimumHeight(260)
        b.addWidget(self.pred)
        f.setVisible(False)
        box.addWidget(f)
        self.build_gpt(box)
        f, b = card()
        b.addWidget(lbl("LO QUE VA PASANDO", "eyebrow"))
        self.log = QListWidget()
        self.log.setMinimumHeight(220)
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
        except Exception as exc:  # stays inside this window
            self.tile("conexion", "Sin datos", f"{type(exc).__name__}: {exc}", AMBER)

    def render(self):
        db = self.connect()
        if db is None:
            self.tile("conexion", "Sin datos", "El bot todavía no ha arrancado en esta carpeta.", AMBER)
            return
        try:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            real = meta.get("gpt_mode") == "real"
            self.title.setText("K-MIDAS · Pocket GPT · CUENTA REAL" if real else "K-MIDAS · Pocket GPT · DEMO")
            self.title.setStyleSheet(f"color: {RED};" if real else "")
            events = db.execute("SELECT created, kind, detail FROM events ORDER BY id DESC LIMIT 60").fetchall()
            now = time.time()
            last = events[0][0] if events else 0
            started = float(meta.get("started") or 0)
            stopped = any(k == "DETENIDO" and t >= started for t, k, _ in events)
            press = db.execute("SELECT MAX(pressed_at) FROM gpt_ops").fetchone()[0] if self.has_ops(db) else None
            running = bool(meta.get("pid")) and not stopped and (now - max(last, float(press or 0), started) < 90)
            chip(self.mode_chip, ("REAL · operando" if real else "DEMO · operando") if running else "Detenido",
                 ("loss" if real else "win") if running else "muted")
            conn_event = next(((t, d) for t, k, d in events if k.startswith("POCKET")), None)
            mode = meta.get("mode", "")
            if not running:
                self.tile("conexion", "Detenido", "Enciéndelo con «Iniciar Pocket GPT REAL.bat» y escribe SI." if real else
                          "Enciéndelo con «Iniciar Pocket GPT.bat».", AMBER)
            elif mode == "demo_conectado":
                self.tile("conexion", "Conectado", f"Pocket Option {'REAL' if real else 'DEMO'} · latencia "
                          f"{float(meta.get('latencia') or 0):.2f} s", GREEN)
            else:
                text = conn_event[1] if conn_event else "Conectando con Pocket Option…"
                self.tile("conexion", "Conectando…", text[:160], AMBER)
            bal = meta.get("saldo_demo")
            # 4 Nov 2026 12:31 ("límpiame mi panel para iniciar"): the result and the +$25 target count from the clean
            # start (DEFAULTS since, 12:29:20); the −$50 brake keeps the whole Mexico day.
            start = max(day_start(), float(DEFAULTS.get("since") or 0))
            today = run = 0.0
            n = w = l = 0
            sim = vis = (0, 0, 0, 0.0)
            stakes_open = 0.0
            if self.has_ops(db):
                stakes_open = float(db.execute("SELECT COALESCE(SUM(stake), 0) FROM gpt_ops WHERE state='abierta' AND opened_at >= ?",
                                               (max(day_start(), float(DEFAULTS.get("since") or 0)),)).fetchone()[0] or 0)
                # 5 Nov 09:56: real orders only (a "simulada" row = low payout, judged without an order, no money)
                real_only = "AND COALESCE(state, '') NOT LIKE 'simulada%'"
                today = float(db.execute(f"SELECT SUM(net) FROM gpt_ops WHERE net IS NOT NULL AND opened_at >= ? {real_only}",
                                         (day_start(),)).fetchone()[0] or 0)
                n, w, l, run = db.execute(f"SELECT COUNT(*), COALESCE(SUM(ours='ganó'), 0), COALESCE(SUM(ours='perdió'), 0), "
                                          f"COALESCE(SUM(net), 0) FROM gpt_ops WHERE ours IS NOT NULL AND opened_at >= ? {real_only}",
                                          (start,)).fetchone()
                sim = db.execute("SELECT COUNT(*), COALESCE(SUM(ours='ganó'), 0), COALESCE(SUM(ours='perdió'), 0), "
                                 "COALESCE(SUM(net), 0) FROM gpt_ops WHERE ours IS NOT NULL AND opened_at >= ? "
                                 "AND state = 'simulada'", (start,)).fetchone()
                vis = db.execute("SELECT COUNT(*), COALESCE(SUM(ours='ganó'), 0), COALESCE(SUM(ours='perdió'), 0), "
                                 "COALESCE(SUM(net), 0) FROM gpt_ops WHERE ours IS NOT NULL AND opened_at >= ? "
                                 "AND state = 'simulada: visión'", (start,)).fetchone()
            self.tile("hoy", usd(run, True), f"{n} órdenes desde las {datetime.fromtimestamp(start, MEXICO):%H:%M} · {w} ganadas / "
                      f"{l} perdidas" + (f" · día completo {usd(today, True)}" if real else "")
                      + (f"\nModo visión (sin orden tras perder): {vis[0]} señales, {vis[1]} acertadas / {vis[2]} falladas · "
                         f"si hubiera entrado {usd(float(vis[3]), True)}" if vis[0] else "")
                      + (f"\nSin orden por pago bajo (antes de las 10:09): {sim[0]} señales, {sim[1]} acertadas / {sim[2]} "
                         f"falladas, en papel {usd(float(sim[3]), True)}" if sim[0] else ""),
                      GREEN if run > 0 else RED if run < 0 else None)
            # 5 Nov 11:00 ("para saber con cuánto dinero arrancamos"): the balance at the clean start = now − the closed
            # orders' result since then + the stakes of the order still open (Pocket takes the stake when it opens)
            if bal not in (None, "", "None"):
                first = float(bal) - float(run) + stakes_open
                self.tile("saldo", usd(float(bal)), f"Arrancó con {usd(first)} a las {datetime.fromtimestamp(start, MEXICO):%H:%M} · "
                          f"va {usd(float(run), True)} en órdenes cerradas" + (" (cuenta REAL)" if real else " (demo)")
                          + (f" · ${stakes_open:g} en una orden abierta" if stakes_open else ""))
            else:
                self.tile("saldo", "—", "Saldo que informa Pocket" + (" (cuenta REAL)" if real else " (demo)"))
            if real:
                lim, goal = float(REAL["daily_stop"]), float(REAL["daily_target"])
                left = lim + today
                braked = any(k == "GPT FRENO DIARIO" and t >= day_start() for t, k, _ in events)
                reached = run >= goal or any(k == "GPT META DIARIA" and t >= start for t, k, _ in events)
                today = run  # the target line below counts from the clean start
                if reached:
                    self.tile("freno", f"META +{usd(goal)} ✅", "Meta del día alcanzada: no opera hasta las 00:00", GREEN)
                else:
                    self.tile("freno", f"−{usd(lim)} / +{usd(goal)}",
                              ("FRENO ACTIVADO: no opera hasta las 00:00" if braked else
                               f"Freno: le quedan {usd(max(left, 0))} · Meta: faltan {usd(max(goal - today, 0))}"),
                              RED if braked else None)
            else:
                self.tile("freno", "Sin freno", "La DEMO no tiene freno diario.")
            self.render_payouts(meta, db)
            self.render_pairs(meta, db, start)
            self.render_predictor(db)
            if self.has_ops(db):
                self.render_gpt(db)
            self.log.clear()
            for t, k, d in events[:40]:
                self.log.addItem(f"{datetime.fromtimestamp(t, MEXICO):%H:%M:%S}  {k}  ·  {d[:220]}")
        finally:
            db.close()

    def render_payouts(self, meta, db):
        import json
        try:
            rows = json.loads(meta.get("gpt_payouts") or "[]")
        except ValueError:
            rows = []
        at = float(meta.get("gpt_payouts_at") or 0)
        current = None
        if self.has_ops(db):
            row = db.execute("SELECT asset FROM gpt_ops ORDER BY message DESC LIMIT 1").fetchone()
            current = row[0] if row else None
        rec_at = float(meta.get("grabador_at") or 0)
        rec = (f" · 🎙 grabando ticks: {int(meta.get('grabador_ticks_hoy') or 0):,} hoy en {meta.get('grabador_pares') or 0} pares"
               if rec_at and time.time() - rec_at < 60 else "")
        self.pay_note.setText((f"actualizado hace {time.time() - at:.0f} s" if at else
                               "se llena cuando el bot se conecta (versión de las 12:15 en adelante)") + rec)
        for k, cell in enumerate(self.pay_cells):
            if k < len(rows):
                pay, sym, name, kind = rows[k]
                mark = "  ◀ elegido en el bot" if sym == current else ""
                cell.setText(f"{name}  {pay:g} %{mark}")
                cell.setStyleSheet(f"color: {GREEN if pay >= 85 else AMBER if pay >= 75 else RED};")
            else:
                cell.setText("")

    def render_predictor(self, db):
        from .predictor import HORIZON, MIN_N, MIN_RATE, RULES
        has = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pred_rules'").fetchone() is not None
        self.pred_card.setVisible(has)
        if not has:
            return
        rules = {}
        for pair, rule, n, w, l, t in db.execute("SELECT pair, rule, n, w, l, t FROM pred_rules"):
            rules.setdefault(pair, {})[rule] = (n, w, l, t)
        fwd = {}
        for pair, res in db.execute("SELECT pair, result FROM pred_forward"):
            f = fwd.setdefault(pair, [0, 0, 0])
            f[0 if res == "acierto" else 1 if res == "fallo" else 2] += 1
        judged = sum(n for p in rules.values() for (n, _, _, _) in p.values())
        self.pred_note.setText(f"Cada {HORIZON} s, para cada par, 6 reglas leen los últimos 5, 15 y 60 s del precio de Pocket (MOM = "
                               f"el movimiento sigue, REV = rebota) y {HORIZON} s después se juzgan. Una regla se vuelve el «modelo» del par "
                               f"cuando lleva {MIN_N} juicios sin empates con {MIN_RATE:.0%} o más; desde ahí sus predicciones "
                               "nuevas se cuentan aparte: ese es el resultado que vale. Con pago del 92 % hace falta más de 52.1 %. "
                               f"Juicios hechos: {judged:,}.")
        a, f_, e = (sum(v[i] for v in fwd.values()) for i in range(3))
        rate = a / (a + f_) if a + f_ else None
        self.pred_total.setText(f"Modelo (predicciones nuevas): {a + f_ + e} · {a} aciertos / {f_} fallos / {e} empates · "
                                + (f"{rate:.1%} sin empates" if rate is not None else "todavía sin predicciones nuevas"))
        chip(self.pred_chip, "Aprendiendo" if a + f_ < 100 else ("Prometedor" if rate and rate >= 0.56 else
                                                                 "Sin ventaja" if rate is not None and rate < 0.53 else "En duda"),
             "info" if a + f_ < 100 else ("win" if rate and rate >= 0.56 else "warn"))
        rows = []
        for pair, rs in rules.items():
            best = max(rs.items(), key=lambda kv: (kv[1][1] / max(kv[1][1] + kv[1][2], 1), kv[1][0]))
            rows.append((pair, best, fwd.get(pair, [0, 0, 0])))
        rows.sort(key=lambda r: -(r[1][1][1] / max(r[1][1][1] + r[1][1][2], 1)))
        self.pred.setRowCount(len(rows))
        for i, (pair, (rule, (n, w, l, t)), (fa, ff, fe)) in enumerate(rows):
            d = w + l
            values = (pair.replace("_otc", " OTC"), rule, f"{n} juicios · {100 * w / d:.1f} % sin empates" if d else "—",
                      str(fa + ff + fe), f"{fa} / {ff} / {fe}", f"{100 * fa / (fa + ff):.1f} %" if fa + ff else "—")
            for c, v in enumerate(values):
                self.pred.setItem(i, c, QTableWidgetItem(v))

    def render_pairs(self, meta, db, start):
        import json
        try:
            ranking = json.loads(meta.get("gpt_stability") or "[]")
        except ValueError:
            ranking = []
        at = float(meta.get("gpt_stability_at") or 0)
        self.rank_note.setText(f"estabilidad medida hace {time.time() - at:.0f} s" if at else
                               "la estabilidad se mide en los primeros 5 minutos de precios")
        played = {}
        if self.has_ops(db):
            for pair, ours, stake, net, state in db.execute("SELECT pair, ours, stake, net, state FROM gpt_ops WHERE ours IS NOT NULL "
                                                            "AND opened_at >= ?", (start,)):
                p = played.setdefault(pair, [0, 0, 0.0, 0])
                p[0] += 1
                p[1] += ours == "ganó"
                p[2] += (net or 0) / (stake or 1)
                p[3] += state == "simulada"
        order = [r[0] for r in ranking] + [p for p in played if p not in {r[0] for r in ranking}]
        info = {r[0]: r for r in ranking}
        self.pairs.setRowCount(len(order))
        for i, pair in enumerate(order):
            vol, pay = (info[pair][1], info[pair][2]) if pair in info else (None, None)
            n, w, net, sim = played.get(pair, [0, 0, 0.0, 0])
            half = 196 * ((w / n) * (1 - w / n) / n) ** 0.5 if n else 0
            values = (pair, f"{vol * 1e4:.2f} pb" if vol is not None else "—", f"{pay:g} %" if pay else "—",
                      f"{n}" + (f" ({sim} sin orden)" if sim else ""), str(w),
                      f"{100 * w / n:.1f} %" if n else "—",
                      f"{max(0, 100 * w / n - half):.0f}–{min(100, 100 * w / n + half):.0f} %" if n >= 5 else "pocas jugadas",
                      usd(net, True) if n else "—")
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 7 and n:
                    item.setForeground(Qt.GlobalColor.green if net > 0 else Qt.GlobalColor.red)
                self.pairs.setItem(i, c, item)

    @staticmethod
    def has_ops(db) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpt_ops'").fetchone() is not None
