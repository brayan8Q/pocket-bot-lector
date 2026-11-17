"""Dashboard page "Pocket Option · Tyl". It only READS pocket_tyl.sqlite (and writes its own files in PocketTyl: the
stop flag and the demo SSID). Any error stays inside this page; nothing here touches the gold bot or MT5."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QProgressBar, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .core import GROUP_TZ, MEXICO
from .secret import forget_ssid, has_ssid, save_ssid
from .store import data_dir, load_config, plan_of

GREEN, RED, MUTED, GOLD, BLUE, AMBER = "#68e0b5", "#ff8798", "#8da2b9", "#f5d061", "#80baff", "#ffc67d"
CHIPS = {"win": ("#12302b", GREEN), "loss": ("#381b24", RED), "gold": ("#362e16", GOLD), "info": ("#162840", BLUE),
         "warn": ("#362919", AMBER), "muted": ("#1b2a3c", "#c3cfdb")}
LAUNCHER = Path(__file__).resolve().parent.parent / "Iniciar Pocket Tyl.bat"
CONNECT = Path(__file__).resolve().parent.parent / "Conectar Pocket Option.bat"


def lbl(value="", name="subtitle", wrap=False):
    w = QLabel(value)
    w.setObjectName(name)
    w.setTextFormat(Qt.TextFormat.PlainText)
    w.setWordWrap(wrap)
    return w


def chip(widget, value, kind="muted"):
    bg, fg = CHIPS.get(kind, CHIPS["muted"])
    widget.setText(value)
    widget.setStyleSheet(f"background: {bg}; color: {fg}; border-radius: 6px; padding: 3px 9px; font-size: 11px; font-weight: 600;")


def card():
    f = QFrame()
    f.setObjectName("card")
    box = QVBoxLayout(f)
    box.setContentsMargins(16, 14, 16, 14)
    box.setSpacing(6)
    return f, box


def usd(v, signed=False):
    return f"{v:+,.2f} USD" if signed else f"{v:,.2f} USD"


def group_today() -> str:
    return datetime.now(GROUP_TZ).date().isoformat()


def anna_pair(pair: str) -> str:
    """"AUDCAD-OTC" → "AUD/CAD OTC"; "USDCAD-OTCB" → "USD/CAD OTCb"."""
    p = (pair or "").upper()
    base = "".join(ch for ch in p.replace("OTC", "") if ch.isalpha())[:6]
    return f"{base[:3]}/{base[3:]}" + (" OTCb" if "OTCB" in p else " OTC" if "OTC" in p else "")


def anna_short(source: str) -> str:
    return (source or "").replace("ANNA ", "").replace("TRADER ", "")


def candle(side, start, end):
    """A one-minute candle seen from the signal's side: "a favor", "en contra", "igual" or "—" (no price)."""
    if start is None or end is None:
        return "—"
    if start == end:
        return "igual"
    return "a favor" if (end > start) == (side == "ARRIBA") else "en contra"


ANNA_GROUPS = (("WIN", "WIN · 1ª vela"), ("WIN¹", "WIN¹ / WIN° · 2ª vela"), ("WIN²", "WIN² / WIN°° · 3ª vela"),
               ("LOSS", "Loss / HIT GALE"), ("DOJI", "DOJI · empate"))
ANNA_SUM = (("senales", "Señales"), ("jugadas", "Jugadas"), ("ganamos", "1ª entrada: ganadas"),
            ("perdimos", "1ª entrada: perdidas"), ("wr", "Win rate 1ª entrada"), ("neto", "Neto 1ª entrada"),
            ("canal", "Win rate que anuncia el canal"), ("mg_ganadas", "Martingala: ganadas"),
            ("mg_perdidas", "Martingala: perdió todo"), ("mg_neto", "Neto con martingala"))


class PocketTylPanel(QWidget):
    def __init__(self, root: Path | None = None, parent=None):
        super().__init__(parent)
        self.root = root or data_dir()
        self.period = "hoy"
        try:  # the two players of the plan: J2/J3, or J1/J2 when the 1st candle is bet (user, 23 Oct 2026)
            players = plan_of(load_config(self.root)).players
        except Exception:
            players = (2, 3)
        self.p0, self.p1 = players[0], players[-1]
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
        box.addWidget(lbl("Pocket Option · Tyl", "title"))
        self.subtitle = lbl("", "subtitle", True)
        box.addWidget(self.subtitle)
        self.engine_alive = False
        self.build_gpt(box)
        self.build_anna(box)

        top = QGridLayout()
        top.setSpacing(12)
        f, b = card(); b.addWidget(lbl("MODO", "eyebrow")); self.mode = lbl("Papel", "value"); b.addWidget(self.mode)
        self.mode_note = lbl("", "subtitle", True); b.addWidget(self.mode_note); top.addWidget(f, 0, 0)
        f, b = card(); b.addWidget(lbl("GANANCIA DEL DÍA (CORTE 21:00 MX)", "eyebrow")); self.day_net = lbl("0.00 USD", "value")
        b.addWidget(self.day_net); self.day_note = lbl("", "subtitle", True); b.addWidget(self.day_note); top.addWidget(f, 0, 1)
        f, b = card(); b.addWidget(lbl("META DIARIA", "eyebrow")); self.goal_text = lbl("+20.00 USD · apagado en seco al llegar", "sectionTitle")
        b.addWidget(self.goal_text); self.goal = QProgressBar(); self.goal.setRange(0, 1000); self.goal.setTextVisible(False)
        self.goal.setFixedHeight(10); self.goal.setStyleSheet("QProgressBar { background: #1b2a3c; border: none; border-radius: 5px; } "
                                                              f"QProgressBar::chunk {{ background: {GREEN}; border-radius: 5px; }}")
        b.addWidget(self.goal); self.goal_note = lbl("", "subtitle", True); b.addWidget(self.goal_note); top.addWidget(f, 0, 2)
        f, b = card(); b.addWidget(lbl("FRENO DE SEGURIDAD DIARIO", "eyebrow")); self.brake = lbl("-40.00 USD", "value")
        b.addWidget(self.brake); self.brake_note = lbl("Si el día llega a esta pérdida, se apaga hasta las 21:00 MX.", "subtitle", True)
        b.addWidget(self.brake_note); top.addWidget(f, 0, 3)
        box.addLayout(top)

        players = QHBoxLayout()
        players.setSpacing(12)
        self.player = {}
        first_title = f"JUGADOR {self.p0} · " + ("1ª VELA" if self.p0 == 1 else "1ª MARTINGALA")
        second_title = f"JUGADOR {self.p1} · " + ("1ª MARTINGALA" if self.p0 == 1 else "2ª MARTINGALA")
        for n, title in ((self.p0, first_title), (self.p1, second_title)):
            f, b = card()
            head = QHBoxLayout(); head.addWidget(lbl(title, "eyebrow")); head.addStretch(); state = QLabel(); chip(state, "Libre")
            head.addWidget(state); b.addLayout(head)
            stake = lbl("", "value"); b.addWidget(stake)
            debt = lbl("", "subtitle", True); b.addWidget(debt)
            self.player[n] = (state, stake, debt)
            players.addWidget(f, 1)
        f, b = card()
        head = QHBoxLayout(); head.addWidget(lbl("ESQUEMA DE CADA SEÑAL", "eyebrow")); head.addStretch()
        self.level_chip = QLabel(); chip(self.level_chip, "Nivel normal"); head.addWidget(self.level_chip); b.addLayout(head)
        self.level_rows = (lbl("", "sectionTitle"), lbl("", "sectionTitle"))
        for row in self.level_rows:
            b.addWidget(row)
        self.book_note = lbl("", "subtitle", True); b.addWidget(self.book_note)
        players.addWidget(f, 1)
        box.addLayout(players)

        f, b = card()
        head = QHBoxLayout(); head.addWidget(lbl("SEÑAL EN VIGILANCIA", "eyebrow")); head.addStretch()
        self.engine_chip = QLabel(); chip(self.engine_chip, "Motor Pocket: detenido", "warn"); head.addWidget(self.engine_chip)
        self.start_btn = QPushButton("Iniciar motor Pocket"); self.start_btn.setObjectName("primary"); self.start_btn.clicked.connect(self.start_engine)
        self.stop_btn = QPushButton("Detener motor Pocket"); self.stop_btn.setObjectName("danger"); self.stop_btn.clicked.connect(self.stop_engine)
        head.addWidget(self.start_btn); head.addWidget(self.stop_btn); b.addLayout(head)
        self.signal_title = lbl("Sin señal en curso", "sectionTitle"); b.addWidget(self.signal_title)
        self.signal_times = lbl("", "subtitle", True); b.addWidget(self.signal_times)
        self.signal_state = QLabel(); chip(self.signal_state, "Esperando la próxima señal de Tyl trader"); b.addWidget(self.signal_state)
        box.addWidget(f)

        f, b = card()
        head = QHBoxLayout(); head.addWidget(lbl("RENDIMIENTO", "eyebrow")); head.addStretch()
        self.period_btns = {}
        for key, name in (("hoy", "Hoy"), ("semana", "Semana"), ("historico", "Histórico")):
            btn = QPushButton(name); btn.setCheckable(True); btn.clicked.connect(lambda _=False, k=key: self.set_period(k))
            head.addWidget(btn); self.period_btns[key] = btn
        b.addLayout(head)
        stats = QGridLayout(); self.stat = {}
        for i, (key, name) in enumerate((("rondas", "Rondas jugadas"), ("j2", f"Ganadas por J{self.p0}"),
                                         ("j3", f"Ganadas por J{self.p1}"),
                                         ("perdidas", "Pérdidas"), ("wr", "Win rate"), ("neto", "Neto"))):
            stats.addWidget(lbl(name, "subtitle"), 0, i); v = lbl("—", "sectionTitle"); stats.addWidget(v, 1, i); self.stat[key] = v
        b.addLayout(stats)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(("Hora MX", "Día del grupo", "Par", "Dirección", "Jugador", "Apuesta", "Resultado", "Neto"))
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(260)
        b.addWidget(self.table)
        box.addWidget(f)

        f, b = card()
        b.addWidget(lbl("CONEXIÓN · CUENTA DEMO DE POCKET OPTION", "eyebrow"))
        b.addWidget(lbl("Pulsa «Iniciar sesión y capturar SSID» (o pega aquí el SSID de tu cuenta DEMO). Se guarda protegido por Windows y solo se acepta si dice que es "
                        "demo (isDemo 1). Mientras el conector de Pocket Option no esté activo, el motor trabaja en papel con las "
                        "señales y los resultados de Tyl en tiempo real.", "subtitle", True))
        row = QHBoxLayout()
        self.ssid = QLineEdit(); self.ssid.setEchoMode(QLineEdit.EchoMode.Password); self.ssid.setPlaceholderText("SSID de la cuenta DEMO")
        login = QPushButton("Iniciar sesión y capturar SSID"); login.setObjectName("primary"); login.clicked.connect(self.capture_ssid)
        save = QPushButton("Guardar SSID (solo demo)"); save.clicked.connect(self.save_ssid)
        forget = QPushButton("Borrar SSID"); forget.clicked.connect(self.forget_ssid)
        row.addWidget(login); row.addWidget(self.ssid, 1); row.addWidget(save); row.addWidget(forget); b.addLayout(row)
        self.ssid_note = lbl("", "subtitle", True); b.addWidget(self.ssid_note)
        box.addWidget(f)
        box.addStretch()

        self.set_period("hoy")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)  # 1 s: the ANNA card counts down to each entry and expiry
        self.refresh()

    def build_gpt(self, box):
        """TRADING GPT BOT on the DEMO (user, 4 Nov 2026): the test presses «Nueva señal», plays $1 at once for the signal's
        duration, no martingale. Read from the gpt_ops table that `python -m pocket_tyl.gpt` writes."""
        f, b = card()
        self.gpt_card = f
        self.gpt_title = lbl("TRADING GPT BOT · PRUEBA EN DEMO", "eyebrow")
        head = QHBoxLayout(); head.addWidget(self.gpt_title)
        head.addStretch(); self.gpt_chip = QLabel(); chip(self.gpt_chip, "Detenida"); head.addWidget(self.gpt_chip)
        b.addLayout(head)
        self.gpt_note = lbl("", "subtitle", True); b.addWidget(self.gpt_note)
        b.addWidget(lbl("EN CURSO", "eyebrow"))
        self.gpt_live = lbl("Sin operación abierta", "sectionTitle", True); b.addWidget(self.gpt_live)
        grid = QGridLayout(); self.gpt_sum = {}
        names = (("jugadas", "Jugadas"), ("ganadas", "Ganadas"), ("perdidas", "Perdidas"), ("empates", "Empates"),
                 ("wr", "Win rate"), ("neto", "Neto a $1"), ("mg", "Neto con martingala"), ("conf", "Confianza media del bot"),
                 ("lat", "Señal tras presionar"))
        grid.addWidget(lbl("Duración", "subtitle"), 0, 0)
        self.gpt_heads = {}
        for c, (key, name) in enumerate(names, start=1):
            self.gpt_heads[key] = lbl(name, "subtitle", True)
            grid.addWidget(self.gpt_heads[key], 0, c)
        self.gpt_rows = []
        for r in range(1, 4):
            cells = [lbl("", "sectionTitle")] + [lbl("", "sectionTitle") for _ in names]
            for c, w in enumerate(cells):
                grid.addWidget(w, r, c)
            self.gpt_rows.append(cells)
        self.gpt_names = [k for k, _ in names]
        b.addLayout(grid)
        self.gpt_table = QTableWidget(0, 13)
        self.gpt_table.setHorizontalHeaderLabels(("Hora MX", "Par", "Dirección", "Duración", "Confianza", "Señal tras presionar",
                                                  "Orden tras leer", "Abrió", "Cerró", "Resultado", "Neto real",
                                                  "Martingala: nivel · apuesta", "Neto con martingala"))
        self.gpt_table.setAlternatingRowColors(True)
        self.gpt_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.gpt_table.verticalHeader().hide()
        self.gpt_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.gpt_table.setMinimumHeight(300)
        b.addWidget(self.gpt_table)
        f.setVisible(False)
        box.addWidget(f)

    def render_gpt(self, db):
        has = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpt_ops'").fetchone() is not None
        self.gpt_card.setVisible(has)
        if not has:
            return
        from .gpt import DEFAULTS as GPT_DEFAULTS, ladder_of, replay_martingale
        gcfg = {**GPT_DEFAULTS, **(load_config(self.root).get("gpt") or {})}
        steps = ladder_of(gcfg)
        # 5 Nov 2026 10:0x (user: "agrégale ahí con martingala"): with the martingale off, the martingale columns show what
        # the ×2.2 ladder within the $cap would have made on the same orders (a what-if; the bot keeps playing $1).
        what_if = len(steps) == 1
        mg_steps = ladder_of({**gcfg, "multiplier": 2.2}) if what_if else steps
        mg_name = (f"Con martingala ×2.2 (simulada: " + " → ".join(f"${s:g}" for s in mg_steps) + ")" if what_if
                   else "Neto con martingala")
        self.gpt_heads["mg"].setText(mg_name)
        self.gpt_table.setHorizontalHeaderItem(11, QTableWidgetItem("Martingala simulada: nivel · apuesta" if what_if else
                                                                     "Martingala: nivel · apuesta"))
        self.gpt_table.setHorizontalHeaderItem(12, QTableWidgetItem("Neto con martingala (simulada)" if what_if else
                                                                     "Neto con martingala"))
        since = float(gcfg.get("since") or 0)  # clean series (4 Nov 11:26): older rows are history, shown apart
        allrows = db.execute("SELECT message, pressed_at, posted, received_at, pair, side, seconds, confidence, opened_at, "
                             "open_price, close_price, net, ours, state, stake FROM gpt_ops ORDER BY message DESC").fetchall()
        rows = [r for r in allrows if (r[8] or r[3] or r[1] or 0) >= since]
        old = [r for r in allrows if r not in rows and r[12]]
        # real orders only (a "simulada" row = low payout, no order, no money)
        booked = sorted((r for r in rows if r[12] and not (r[13] or "").startswith("simulada")), key=lambda r: r[8] or 0)
        mg_rows, next_level = replay_martingale([(r[12], r[14], r[11]) for r in booked], mg_steps)
        mg = {r[0]: x for r, x in zip(booked, mg_rows)}  # message → (level, stake, net with martingale)
        now = time.time()
        last_press = max((r[1] or 0 for r in allrows), default=0)
        live = now - last_press < 150
        ladder = " → ".join(f"${s:.2f}" for s in steps)
        row = db.execute("SELECT value FROM meta WHERE key='gpt_mode'").fetchone()
        real = bool(row and row[0] == "real")
        from .gpt import DEMO_ROTATE
        every = float((gcfg if real else {**gcfg, **DEMO_ROTATE})["every_seconds"])  # the DEMO presses every 8 s
        if not real:  # 5 Nov 10:09: the DEMO plays whatever Pocket pays
            gcfg = {**gcfg, "min_payout": DEMO_ROTATE.get("min_payout", gcfg.get("min_payout"))}
        self.gpt_title.setText("TRADING GPT BOT · CUENTA REAL · DINERO REAL" if real else "TRADING GPT BOT · PRUEBA EN DEMO")
        self.gpt_title.setStyleSheet(f"color: {RED};" if real else "")
        fixed = len(steps) == 1  # 4 Nov 2026 12:3x: martingale off, $1 fixed
        how = (f"sin martingala · ${steps[0]:.2f} fijo" if fixed else f"martingala {ladder} · próxima ${steps[next_level]:.2f}")
        chip(self.gpt_chip, (f"{'REAL' if real else 'Activa'} · {how}" if live else "Detenida"),
             ("loss" if real else "win") if live else "muted")
        self.gpt_note.setText(f"Cada {every:g} s presiona «Nueva señal» en el chat de TRADING GPT BOT, abre una orden por la "
                              "duración que da el bot y anota el resultado de Pocket. "
                              + (f"Sin martingala: ${steps[0]:.2f} fijo por señal. " if fixed else
                                 f"Martingala entre señales: {ladder} (la racha completa pierde ${sum(steps):.2f}, dentro del "
                                 f"tope de ${float(gcfg['cap']):g}); al ganar, o al perder las {len(steps)}, vuelve a "
                                 f"${steps[0]:.2f}. ")
                              + (f"Solo juega si Pocket paga {float(gcfg.get('min_payout') or 0):g} % o más. "
                                 if float(gcfg.get('min_payout') or 0) else "Juega con el pago que haya (DEMO). ")
                              + "«Neto a $1» = una "
                              "sola entrada de $1. Con pago del 92 % hace falta más de 52.1 % para no perder. Se detiene sola a las "
                              f"{int(gcfg['max_signals'])} jugadas de {int(gcfg['count_seconds'])} s. "
                              + ("⚠️ CUENTA REAL: opera dinero real · solo señales de 15 s · freno diario −$50 (hora México) · "
                                 "se detiene cerrando su ventana «Pocket GPT REAL»." if real else
                                 "Cuenta DEMO: el dinero real no se toca.")
                              + (f" · Marcador limpio desde las {datetime.fromtimestamp(since, MEXICO):%H:%M} del "
                                 f"{datetime.fromtimestamp(since, MEXICO):%d/%m}; historial anterior guardado: {len(old)} jugadas, "
                                 f"{sum(1 for r in old if r[12] == 'ganó')} ganadas, neto real "
                                 f"{usd(sum(r[11] or 0 for r in old), True)}." if since else ""))
        side_txt = lambda s: "CALL ⬆" if s == "ARRIBA" else "PUT ⬇"
        lines, mode = [], []
        if not real and DEMO_ROTATE.get("pause_after_loss"):  # 5 Nov 10:1x: vision mode after a loss
            last = next((r[12] for r in rows if r[12] in ("ganó", "perdió")), None)
            mode.append("MODO VISIÓN · perdió la última: no mete orden hasta que una señal gane en simulación"
                        if last == "perdió" else "MODO REAL · mete orden con cada señal (si pierde, pasa a modo visión)")
        for m, pressed, posted, received, pair, side, secs, conf, opened, op, cp, net, ours, state, stake in rows[:3]:
            if state == "abierta" and opened:
                lines.append(f"{pair} {side_txt(side)} {secs} s · ${stake or 1:.2f} · confianza {conf}% · abrió en {op} · vence en "
                             f"{max(opened + secs - now, 0):.0f} s")
            elif (state or "").startswith("simulada") and not ours and opened and now < opened + secs + 5:
                lines.append(f"{pair} {side_txt(side)} {secs} s · SIN ORDEN (simulación) · confianza {conf}% · precio {op} · "
                             f"se juzga en {max(opened + secs - now, 0):.0f} s")
            elif ours and opened and now < opened + secs + 20:
                lines.append(f"{pair} {side_txt(side)} {secs} s · {op} → {cp} · {ours} {usd(net or 0, True)}")
        if not lines and live:
            lines.append(f"Esperando la próxima señal · presiona en {max(last_press + every - now, 0):.0f} s")
        lines = mode + lines
        self.gpt_live.setText("\n".join(lines) if lines else "Sin operación abierta")
        groups = sorted({r[6] for r in rows if r[6]})[:3]
        for k, cells in enumerate(self.gpt_rows):
            if k >= len(groups):
                for w in cells:
                    w.setText("")
                continue
            secs = groups[k]
            mine = [r for r in rows if r[6] == secs and r[12] and not (r[13] or "").startswith("simulada")]  # real orders
            w = sum(1 for r in mine if r[12] == "ganó"); l_ = sum(1 for r in mine if r[12] == "perdió")
            e = sum(1 for r in mine if r[12] == "empate")
            total = sum((r[11] or 0) / (r[14] or 1) for r in mine)  # one $1 entry per signal
            total_mg = sum(mg[r[0]][2] for r in mine if r[0] in mg)
            confs = [r[7] for r in mine if r[7]]
            lats = [r[3] - r[1] for r in mine if r[3] and r[1]]
            vals = dict(jugadas=str(len(mine)), ganadas=str(w), perdidas=str(l_), empates=str(e),
                        wr=f"{w / len(mine):.1%}" if mine else "—", neto=usd(total, True), mg=usd(total_mg, True),
                        conf=f"{sum(confs) / len(confs):.0f} %" if confs else "—",
                        lat=f"{sorted(lats)[len(lats) // 2]:.1f} s" if lats else "—")
            cells[0].setText(f"{secs} s" if secs < 60 else f"{secs // 60} min")
            for c, key in enumerate(self.gpt_names, start=1):
                cells[c].setText(vals[key])
            for key, v in (("neto", total), ("mg", total_mg)):
                cells[self.gpt_names.index(key) + 1].setStyleSheet(f"color: {GREEN if v > 0 else RED if v < 0 else '#f6f9fc'};")
        self.gpt_table.setRowCount(min(len(rows), 300))
        for r, (m, pressed, posted, received, pair, side, secs, conf, opened, op, cp, net, ours, state, stake) in enumerate(rows[:300]):
            level, bet, net_mg = mg.get(m, (None, None, None))
            values = (datetime.fromtimestamp(opened or received or pressed, MEXICO).strftime("%d/%m %H:%M:%S"), pair, side_txt(side),
                      f"{secs} s" if secs < 60 else f"{secs // 60} min", f"{conf} %" if conf else "—",
                      f"{received - pressed:.1f} s" if received and pressed else "—",
                      f"{opened - received:.2f} s" if opened and received else "—", f"{op}" if op else "—", f"{cp}" if cp else "—",
                      (f"{ours} (visión, sin orden)" if state == "simulada: visión" and ours else
                       f"{ours} (sin orden)" if (state or "").startswith("simulada") and ours else ours) or (state or ""),
                      f"{usd(net, True)} (${stake or 1:.2f})" if net is not None else "—",
                      f"{level} · ${bet:.2f}" if bet is not None else "—", usd(net_mg, True) if net_mg is not None else "—")
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 10 and net:
                    item.setForeground(Qt.GlobalColor.green if net > 0 else Qt.GlobalColor.red)
                if c == 12 and net_mg:
                    item.setForeground(Qt.GlobalColor.green if net_mg > 0 else Qt.GlobalColor.red)
                self.gpt_table.setItem(r, c, item)

    def build_anna(self, box):
        """ANNA live check (user, 26 Oct 2026): «ANNA TRADER VIP M1» and «ANNA VIP POCKET», one entry per signal, no
        martingale, beside the label each channel publishes. Read from the engine's anna_ops table."""
        f, b = card()
        self.anna_card = f
        head = QHBoxLayout(); head.addWidget(lbl("ANNA · PRUEBA SIN MARTINGALA EN DEMO (VIP M1 + VIP POCKET)", "eyebrow"))
        head.addStretch(); self.anna_chip = QLabel(); chip(self.anna_chip, "Apagada"); head.addWidget(self.anna_chip)
        b.addLayout(head)
        self.anna_note = lbl("", "subtitle", True); b.addWidget(self.anna_note)
        b.addWidget(lbl("EN CURSO", "eyebrow"))
        self.anna_live = lbl("Sin señal en curso", "sectionTitle", True); b.addWidget(self.anna_live)
        grid = QGridLayout(); self.anna_sum = {}
        grid.addWidget(lbl("Canal", "subtitle"), 0, 0)
        for c, (key, name) in enumerate(ANNA_SUM, start=1):
            grid.addWidget(lbl(name, "subtitle", True), 0, c)
        for r, row in enumerate(("ANNA TRADER VIP M1", "ANNA VIP POCKET", "total"), start=1):
            grid.addWidget(lbl("Total" if row == "total" else anna_short(row), "sectionTitle"), r, 0)
            for c, (key, _) in enumerate(ANNA_SUM, start=1):
                v = lbl("—", "sectionTitle"); grid.addWidget(v, r, c); self.anna_sum[(row, key)] = v
        b.addLayout(grid)
        b.addWidget(lbl("LO QUE PUBLICÓ EL CANAL → LO QUE DIO NUESTRA ENTRADA ÚNICA (1ª vela)", "eyebrow"))
        grid = QGridLayout(); self.anna_label = {}
        grid.addWidget(lbl("Canal", "subtitle"), 0, 0)
        for c, (key, name) in enumerate(ANNA_GROUPS, start=1):
            grid.addWidget(lbl(name, "subtitle"), 0, c)
        for r, row in enumerate(("ANNA TRADER VIP M1", "ANNA VIP POCKET"), start=1):
            grid.addWidget(lbl(anna_short(row), "sectionTitle"), r, 0)
            for c, (key, _) in enumerate(ANNA_GROUPS, start=1):
                v = lbl("0", "subtitle", True); grid.addWidget(v, r, c); self.anna_label[(row, key)] = v
        b.addLayout(grid)
        self.anna_table = QTableWidget(0, 14)
        self.anna_table.setHorizontalHeaderLabels(("Vela MX", "Canal", "Par", "Dirección", "Publicada", "Pago", "Abrió",
                                                   "Cerró", "1ª entrada", "Neto 1ª", "Vela 2", "Vela 3", "Canal publicó",
                                                   "Con martingala"))
        self.anna_table.setAlternatingRowColors(True)
        self.anna_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.anna_table.verticalHeader().hide()
        self.anna_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.anna_table.setMinimumHeight(280)
        b.addWidget(self.anna_table)
        f.setVisible(False)
        box.addWidget(f)

    def render_anna(self, db, cfg):
        conf = cfg.get("anna") or {}
        on = bool(conf.get("enabled")) and self.engine_alive  # 4 Nov 2026: "Activa" only while the Pocket engine runs
        has = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anna_ops'").fetchone() is not None
        self.anna_card.setVisible(on or has)
        if not (on or has):
            return
        stake = float(conf.get("stake", 1.0) or 1.0)
        gales, mult = int(conf.get("gales", 0) or 0), float(conf.get("multiplier", 2.2) or 2.2)
        ladder = " → ".join(f"${stake * mult ** k:.2f}" for k in range(gales + 1))
        chans = conf.get("channels") or [conf.get("channel_title", "ANNA VIP POCKET")]
        chip(self.anna_chip, (f"Activa · martingala {ladder}" if gales else f"Activa · {usd(stake)} por señal") if on else "Apagada",
             "win" if on else "muted")
        how = (f"la martingala del canal, {ladder} (hasta {gales} gales): cada vela se juzga a los :59.8 y, si perdió, la "
               "gale entra al segundo :00 de la siguiente (empate cuenta como perdida, como en Tyl)" if gales else
               f"una sola entrada de {usd(stake)}, sin martingala")
        self.anna_note.setText(f"Lee {' y '.join('«' + c + '»' for c in chans)}. Por cada señal: {how}. La 1ª entrada abre en el "
                               "segundo en que abre su vela y vence al cierre (1 min); si el canal la publica ya empezada la vela, "
                               "entra en ese momento y vence igual al cierre. «1ª entrada» = lo que daría entrar una sola vez; "
                               "«Con martingala» = la secuencia completa. Al lado, lo que publica el canal: WIN = ganó la 1ª vela "
                               "· WIN¹/WIN° = la 2ª · WIN²/WIN°° = la 3ª. Aparte de Tyl: no cuenta en su meta, su freno ni su libreta.")
        cols = {r[1] for r in db.execute("PRAGMA table_info(anna_ops)")} if has else set()
        extra = ", ".join(c if c in cols else "NULL" for c in ("gale_net", "gale_result"))
        rows = db.execute("SELECT source, posted, pair, side, seconds, entry_at, opened_at, state, open_price, close_price, "
                          f"payout, net, ours, channel, channel_raw, price2, price3, {extra} FROM anna_ops "
                          "ORDER BY entry_at DESC, source, message DESC").fetchall() if has else []
        hms = lambda t: datetime.fromtimestamp(t, MEXICO).strftime("%H:%M:%S")
        now = time.time()
        lines = []
        for src, posted, pair, side, seconds, entry, opened_at, state, op, cp, payout, net, ours, ch, raw, p2, p3, gnet, gres in rows:
            state = state or ""
            level = int(state.split()[1]) if state.startswith("gale ") else 0
            closes = entry + int(seconds or 60) * (level + 1)
            if not (state.startswith(("esperando", "gale ")) or state == "abierta" or (state == "cerrada" and now < closes + 20)):
                continue
            head = f"{anna_short(src)} · {anna_pair(pair)} {'⬆ CALL' if side == 'ARRIBA' else '⬇ PUT'} · vela {hms(entry)}"
            if state.startswith("esperando"):
                left = entry - now
                lines.append(f"{head} · entra en {left:.0f} s" if left > 0 else f"{head} · entrando…")
            elif state == "abierta":
                lines.append(f"{head} · 1ª entrada abierta en {op} · vence en {max(closes - now, 0):.0f} s")
            elif state.startswith("gale "):
                lines.append(f"{head} · GALE {level} abierta · vence en {max(closes - now, 0):.0f} s")
            else:
                lines.append(f"{head} · cerrada · 1ª entrada {ours} {usd(net or 0, True)}" +
                             (f" · martingala: {gres} {usd(gnet, True)}" if gres and gales else ""))
            if len(lines) == 6:
                break
        if not on:
            lines = []  # 4 Nov 2026: a test left open on 26 Oct showed "GALE 2 abierta" while the engine was off
        self.anna_live.setText("\n".join(lines) if lines else
                               ("Sin señal en curso · esperando la próxima señal de los canales" if on else "Prueba apagada"))
        for key in ("ANNA TRADER VIP M1", "ANNA VIP POCKET", "total"):
            mine = rows if key == "total" else [r for r in rows if r[0] == key]
            played = [r for r in mine if r[12]]
            won = sum(1 for r in played if r[12] == "ganó"); lost = sum(1 for r in played if r[12] == "perdió")
            total = sum(r[11] or 0 for r in played)
            claimed = [r[13] for r in mine if r[13] in ("WIN", "WIN¹", "WIN²", "LOSS")]
            seqs = [r for r in mine if r[18]]
            mg = sum(r[17] or 0 for r in seqs)
            values = dict(senales=str(len(mine)), jugadas=str(len(played)), ganamos=str(won), perdimos=str(lost),
                          wr=f"{won / len(played):.1%}" if played else "—",
                          canal=f"{sum(1 for c in claimed if c != 'LOSS') / len(claimed):.1%}" if claimed else "—",
                          neto=usd(total, True), mg_ganadas=str(sum(1 for r in seqs if r[18].startswith("ganó"))),
                          mg_perdidas=str(sum(1 for r in seqs if r[18].startswith("perdió"))), mg_neto=usd(mg, True))
            for k, v in values.items():
                self.anna_sum[(key, k)].setText(v)
            for k, v in (("neto", total), ("mg_neto", mg)):
                self.anna_sum[(key, k)].setStyleSheet(f"color: {GREEN if v > 0 else RED if v < 0 else '#f6f9fc'};")
        for (key, group), cell in self.anna_label.items():
            mine = [r for r in rows if r[0] == key and r[13] == group]
            a = sum(1 for r in mine if r[12] == "ganó"); b_ = sum(1 for r in mine if r[12] == "perdió")
            e = sum(1 for r in mine if r[12] == "empate")
            cell.setText(f"{len(mine)} → ganamos {a} · perdimos {b_}" + (f" · empate {e}" if e else "") if mine else "0")
        self.anna_table.setRowCount(min(len(rows), 300))
        for r, (src, posted, pair, side, seconds, entry, opened_at, state, op, cp, payout, net, ours, ch, raw, p2, p3, gnet,
                gres) in enumerate(rows[:300]):
            lead = posted - entry
            waiting = ch is None and now < entry + 300 and not (state or "").startswith(("no se jugó", "error"))
            late = f" (+{opened_at - entry:.0f} s)" if opened_at and opened_at - entry >= 1 else ""
            values = (datetime.fromtimestamp(entry, MEXICO).strftime("%d/%m %H:%M"), anna_short(src), anna_pair(pair),
                      "CALL ⬆" if side == "ARRIBA" else "PUT ⬇",
                      f"{abs(lead):.0f} s tarde" if lead > 0 else f"{abs(lead):.0f} s antes", f"{payout:.0f}%" if payout else "—",
                      f"{op}{late}" if op else "—", f"{cp}" if cp else "—", ours or (state or ""),
                      usd(net, True) if net is not None else "—", candle(side, cp, p2), candle(side, p2, p3),
                      raw or ("esperando…" if waiting else "—"),
                      f"{gres} · {usd(gnet, True)}" if gres else ((state or "").upper() if (state or "").startswith("gale ") else "—"))
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 9 and net:
                    item.setForeground(Qt.GlobalColor.green if net > 0 else Qt.GlobalColor.red)
                if c == 13 and gnet:
                    item.setForeground(Qt.GlobalColor.green if gnet > 0 else Qt.GlobalColor.red)
                if c == 4 and lead > 0:
                    item.setForeground(Qt.GlobalColor.yellow)
                self.anna_table.setItem(r, c, item)

    # ---------- reading ----------
    def connect(self):
        path = self.root / "pocket_tyl.sqlite"
        if not path.exists():
            return None
        try:
            return sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True, timeout=0.5)
        except sqlite3.Error:
            return None

    def refresh(self):
        try:
            self.render()
        except Exception as exc:  # stays inside this page
            chip(self.signal_state, f"No se pudo leer Pocket Tyl: {type(exc).__name__}", "warn")

    def render(self):
        cfg = load_config(self.root)
        plan = plan_of(cfg)
        target, brake = plan.target, plan.loss_stop
        a, b = plan.players[0], plan.players[-1]
        pair = lambda step: f"J{a} {usd(plan.stake(a, step))} → J{b} {usd(plan.stake(b, step))}"
        first = ("J1 apuesta la 1ª vela y J2 la 1ª martingala" if a == 1 else "la 1ª vela solo se vigila (sin apostar)")
        self.subtitle.setText(f"Martingala corta en la misma señal: {first} · nivel normal "
                              f"{pair(0)} · si pierden J{a} y J{b}, un solo escalón de recuperación {pair(plan.levels - 1)} (tope) "
                              f"hasta limpiar la deuda · meta +{target:,.2f} / freno {brake:,.2f} USD por día (corte 21:00 MX) · "
                              "proceso y base de datos propios: no toca el bot de oro.")
        self.goal_text.setText(f"+{target:,.2f} USD · apagado en seco al llegar")
        self.brake.setText(usd(brake) if brake else "Apagado")
        demo = has_ssid(self.root)
        self.ssid_note.setText("SSID demo guardado y protegido." if demo else "Sin SSID guardado.")
        db = self.connect()
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall()) if db is not None else {}
        alive_now = bool(meta.get("pid")) and time.time() - float(meta.get("heartbeat") or 0) < 30
        mode = meta.get("mode", "papel") if alive_now else "papel"
        if mode == "demo_conectado":
            self.mode.setText("Demo conectado")
            self.mode.setStyleSheet(f"color: {GREEN};")
            self.mode_note.setText(f"Cuenta DEMO de Pocket Option · saldo {float(meta.get('saldo_demo') or 0):,.2f} USD · "
                                   f"latencia {float(meta.get('latencia') or 0):.2f} s. Juzga cada vela a las :59.8 y dispara "
                                   f"J{a}/J{b} al segundo :00. La cuenta real está bloqueada.")
        elif mode == "demo_conectando":
            self.mode.setText("Demo: conectando…")
            self.mode.setStyleSheet(f"color: {AMBER};")
            self.mode_note.setText("Conectando con la cuenta DEMO de Pocket Option…")
        else:
            self.mode.setText("Papel")
            self.mode.setStyleSheet("")
            self.mode_note.setText("Lee Tyl trader en tiempo real y anota con los resultados que publica el grupo; no apuesta. " +
                                   ("SSID demo guardado: al iniciar el motor Pocket pasa a Demo." if demo else
                                    "Sin SSID demo. La cuenta real está bloqueada."))
        if db is None:
            chip(self.engine_chip, "Motor Pocket: sin datos todavía", "warn")
            self.signal_title.setText("Sin señal en curso")
            return
        try:
            beat = float(meta.get("heartbeat") or 0)
            alive = bool(meta.get("pid")) and time.time() - beat < 30
            self.engine_alive = alive
            try:
                self.render_gpt(db)
            except Exception as exc:  # the GPT card never breaks Tyl's page
                self.gpt_live.setText(f"No se pudo leer la prueba GPT: {type(exc).__name__}: {exc}")
            tg = meta.get("telegram", "")
            chip(self.engine_chip, "Motor Pocket: activo · " + (tg or "conectando") if alive else "Motor Pocket: detenido",
                 "win" if alive and tg == "conectado" else "warn")
            today = group_today()
            row = db.execute("SELECT net, stopped FROM days WHERE day=?", (today,)).fetchone()
            net, stopped = (row or (0.0, None))
            self.day_net.setText(usd(net, True))
            self.day_net.setStyleSheet(f"color: {GREEN if net > 0 else RED if net < 0 else '#f6f9fc'};")
            self.day_note.setText({"meta": "Meta alcanzada: apagado hasta las 21:00 MX.", "freno": "Freno de seguridad: apagado hasta las 21:00 MX."}
                                  .get(stopped, f"Día del grupo {today} (empezó a las 21:00 MX)."))
            self.goal.setValue(int(max(0.0, min(1.0, net / target if target else 0)) * 1000))
            self.goal_note.setText(f"Faltan {max(target - net, 0):,.2f} USD" if not stopped else "Día cerrado")
            row = db.execute("SELECT debt, step FROM books WHERE player=0").fetchone()
            debt_now, step = (row or (0.0, 0))
            step = min(int(step), plan.levels - 1)
            for n, (state, stake, debt) in self.player.items():
                stake.setText(usd(plan.stake(n, step)))
                debt.setText(f"Normal {usd(plan.stake(n, 0))} · recuperación {usd(plan.stake(n, plan.levels - 1))}")
                st = meta.get(f"jugador{n}", "Libre") if alive else "Libre"
                chip(state, st, "gold" if st == "Operando" else "info" if st.startswith("Vigilando") else "muted")
            names = ("Nivel normal", "En recuperación")
            for k, label in enumerate(self.level_rows):
                label.setText(f"{'● ' if k == step else '○ '}{names[min(k, 1)]}: {pair(k)}")
                label.setStyleSheet(f"color: {GOLD if k == step and step else GREEN if k == step else MUTED};")
            chip(self.level_chip, names[min(step, 1)], "gold" if step else "win")
            self.book_note.setText(f"Deuda de la libreta: {usd(debt_now)} · " +
                                   ("las señales siguen en recuperación hasta limpiarla." if step else
                                    f"si una señal pierde J{a} y J{b}, se anota y las siguientes suben un escalón."))
            sig = db.execute("SELECT message, pair, direction, entry_at, gale1_at, gale2_at, state, result FROM signals "
                             "ORDER BY message DESC LIMIT 1").fetchone()
            if sig:
                _, pair, direction, e, g1, g2, state, result = sig
                hm = lambda t: datetime.fromtimestamp(t, MEXICO).strftime("%H:%M")
                self.signal_title.setText(f"{pair} OTC · {'⬆ ARRIBA' if direction == 'ARRIBA' else '⬇ ABAJO'}")
                self.signal_times.setText(f"J1 (1ª vela) {hm(e)} · J2 (1ª martingala) {hm(g1)} · hora México" if a == 1 else
                                          f"1ª vela {hm(e)} · J2 (1ª martingala) {hm(g1)} · J3 (2ª martingala) {hm(g2)} · hora México")
                kind = "info" if result is None else "win" if result in ("G1", "G2") else "loss" if result == "L" else "muted"
                chip(self.signal_state, state or "", kind)
            try:
                self.render_anna(db, cfg)
            except Exception as exc:  # the ANNA card never breaks Tyl's page
                self.anna_live.setText(f"No se pudo leer la prueba de Anna: {type(exc).__name__}: {exc}")
            self.fill_stats(db)
        finally:
            db.close()

    def set_period(self, key):
        self.period = key
        for k, btn in self.period_btns.items():
            btn.setChecked(k == key)
        self.refresh()

    def fill_stats(self, db):
        today = datetime.now(GROUP_TZ).date()
        since = {"hoy": today, "semana": today - timedelta(days=6), "historico": None}[self.period]
        where, args = ("WHERE day >= ?", (since.isoformat(),)) if since else ("", ())
        ops = db.execute(f"SELECT closed_at, day, pair, direction, player, stake, won, net, message FROM ops {where} "
                         "ORDER BY id DESC", args).fetchall()
        rounds = len({o[8] for o in ops})
        won2 = sum(1 for o in ops if o[4] == self.p0 and o[6]); won3 = sum(1 for o in ops if o[4] == self.p1 and o[6])
        lost = sum(1 for o in ops if o[7] < 0)  # a refund (same price) is not a loss
        self.stat["rondas"].setText(str(rounds)); self.stat["j2"].setText(str(won2)); self.stat["j3"].setText(str(won3))
        self.stat["perdidas"].setText(str(lost))
        # every winning order counts, whatever player it was (the day may mix J2/J3 and J1/J2, 23 Oct 2026)
        self.stat["wr"].setText(f"{sum(1 for o in ops if o[6]) / len(ops):.1%}" if ops else "—")
        total = sum(o[7] for o in ops)
        self.stat["neto"].setText(usd(total, True))
        self.stat["neto"].setStyleSheet(f"color: {GREEN if total > 0 else RED if total < 0 else '#f6f9fc'};")
        rows = ops[:200]
        self.table.setRowCount(len(rows))
        for r, (closed, day, pair, direction, player, stake, won, net, _) in enumerate(rows):
            values = (datetime.fromtimestamp(closed, MEXICO).strftime("%d/%m %H:%M"), day, f"{pair} OTC", direction, f"J{player}",
                      usd(stake), "Ganó" if won else "Empate" if net == 0 else "Perdió", usd(net, True))
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 7:
                    item.setForeground(Qt.GlobalColor.green if net > 0 else Qt.GlobalColor.red)
                self.table.setItem(r, c, item)

    # ---------- actions (only PocketTyl's own files) ----------
    def start_engine(self):
        try:
            (self.root / "stop.flag").unlink()
        except FileNotFoundError:
            pass
        if LAUNCHER.exists():
            subprocess.Popen(["explorer.exe", str(LAUNCHER)])  # its parent is Explorer, not this app
            chip(self.engine_chip, "Motor Pocket: iniciando…", "info")

    def stop_engine(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "stop.flag").write_text(str(time.time()), encoding="utf-8")
        chip(self.engine_chip, "Motor Pocket: deteniendo…", "warn")

    def capture_ssid(self):
        """A separate window (its own process, started by Explorer) to log in and capture the DEMO SSID."""
        if CONNECT.exists():
            subprocess.Popen(["explorer.exe", str(CONNECT)])
            self.ssid_note.setText("Se abrió la ventana de Pocket Option: inicia sesión y abre la sala DEMO.")

    def save_ssid(self):
        value = self.ssid.text()
        self.ssid.clear()
        try:
            save_ssid(self.root, value)
            self.ssid_note.setText("SSID demo guardado y protegido.")
        except ValueError as exc:
            self.ssid_note.setText(str(exc))

    def forget_ssid(self):
        forget_ssid(self.root)
        self.ssid_note.setText("SSID borrado.")
