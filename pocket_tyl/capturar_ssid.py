"""Ventana aparte (su propio proceso, no la app del bot de oro) para iniciar sesión en Pocket Option y capturar el SSID
de la cuenta DEMO del WebSocket: el mensaje `42["auth",{"session":…,"isDemo":1,…}]` que la página envía al abrir la sala.

Tú escribes tu usuario y contraseña en la página oficial; este programa no los ve ni los guarda. Solo se guarda el SSID
si dice isDemo 1, protegido por Windows (DPAPI) en %LOCALAPPDATA%\\PocketTyl. Nunca se imprime ni se muestra.
Uso: `python -m pocket_tyl.capturar_ssid` (o "Conectar Pocket Option.bat")."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from .secret import demo_check, real_check, save_ssid
from .store import data_dir

MARK = "__POCKET_TYL_AUTH__"
SEP = "\x1f"  # MARK + socket URL + SEP + auth frame (4 Nov 2026: the real room's socket host is taken from the page)
HOOK = """
(function () {
  if (window.__pocketTylHook) { return; }
  window.__pocketTylHook = true;
  var send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    try {
      if (typeof data === 'string' && data.indexOf('42["auth"') === 0) { console.log('%s' + this.url + '\\x1f' + data); }
    } catch (e) {}
    return send.apply(this, arguments);
  };
})();
""" % MARK
LOGIN = "https://pocketoption.com/en/login/"
DEMO = "https://pocketoption.com/en/cabinet/demo-quick-high-low/"
REAL_ROOM = "https://pocketoption.com/en/cabinet/quick-high-low/"
# 16 Nov 2026: the PUBLIC try-demo terminal (no account, no password, play money). Its page also sends a demo auth
# frame, so the bot can run on another computer without the owner's credentials (button «Demo pública» or --publica).
TRY_DEMO = "https://pocketoption.com/en/cabinet/try-demo/"


def is_real() -> bool:
    """The REAL-account capture (user, 4 Nov 2026): only from «Conectar Pocket REAL.bat», which sets POCKET_TYL_REAL=1 and
    its own folder PocketTylReal."""
    return os.environ.get("POCKET_TYL_REAL") == "1"


def handle_console(message: str, host: str, root: Path, real: bool = False, ua: str | None = None) -> str | None:
    """A console line from the page. Returns the text to show, or None when the line is not ours (dropped, never shown)."""
    if not message.startswith(MARK):
        return None
    if not (host == "pocketoption.com" or host.endswith(".pocketoption.com")):
        return "Se ignoró un mensaje que no viene de pocketoption.com."
    payload = message[len(MARK):]
    url, auth = payload.split(SEP, 1) if SEP in payload else (None, payload)
    if url and not url.startswith("wss://"):
        url = None
    reason = (real_check if real else demo_check)(auth)
    if reason:
        return f"No se guardó: {reason} Abre la sala {'REAL' if real else 'DEMO'} con el botón de arriba."
    save_ssid(root, auth, real=real, extra=dict(url=url, ua=ua))
    return (f"✅ Sesión de la cuenta {'REAL' if real else 'DEMO'} capturada y guardada protegida. "
            "Ya puedes cerrar esta ventana.")


DEVTOOLS = "127.0.0.1:9333"


async def devtools_watch(found, stop) -> None:
    """Fallback (21 Oct 2026: the JS hook missed the demo room's socket): read the browser's own WebSocket traffic through
    its local DevTools port (this window's Chromium, 127.0.0.1 only), including workers. `found(payload, page_host)`
    receives every `42["auth"` frame; nothing else is kept."""
    import asyncio
    import json
    import urllib.request
    from urllib.parse import urlsplit
    from .ws import WebSocket
    watched = set()

    async def watch(target):
        ws = await WebSocket.connect(target["webSocketDebuggerUrl"])
        host = urlsplit(target.get("url", "")).hostname or ""
        n = 0

        async def call(method, params=None, session=None):
            nonlocal n
            n += 1
            msg = dict(id=n, method=method, params=params or {})
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))
        await call("Network.enable")
        await call("Target.setAutoAttach", dict(autoAttach=True, waitForDebuggerOnStart=False, flatten=True))
        sockets = {}  # requestId → the socket's URL (4 Nov 2026: the real room's host)
        try:
            while not stop():
                data = json.loads(await ws.recv())
                method = data.get("method")
                if method == "Target.attachedToTarget":
                    sid = data["params"]["sessionId"]
                    await call("Network.enable", session=sid)
                    await call("Runtime.runIfWaitingForDebugger", session=sid)
                elif method == "Network.webSocketCreated":
                    sockets[data["params"].get("requestId")] = data["params"].get("url", "")
                elif method == "Network.webSocketFrameSent":
                    payload = data["params"]["response"].get("payloadData", "")
                    if payload.startswith('42["auth"'):
                        found(payload, host, sockets.get(data["params"].get("requestId"), ""))
        finally:
            await ws.close()

    while not stop():
        try:
            pages = json.loads(urllib.request.urlopen(f"http://{DEVTOOLS}/json/list", timeout=3).read())
        except Exception:
            pages = []
        for t in pages:
            if t.get("type") == "page" and t.get("id") not in watched and t.get("webSocketDebuggerUrl"):
                watched.add(t["id"])
                asyncio.ensure_future(watch(t))
        await asyncio.sleep(2)


def main() -> None:
    import os
    os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", DEVTOOLS)  # local only; read by Chromium at start
    import asyncio
    import threading
    from PySide6.QtCore import QObject, QTimer, QUrl, Signal
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

    root = data_dir()
    real = is_real()
    if real and root.name.lower() != "pockettylreal":
        raise SystemExit("La cuenta REAL solo se conecta en su carpeta propia (PocketTylReal), con «Conectar Pocket REAL.bat».")
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    window = QMainWindow()
    room, room_url = ("REAL", REAL_ROOM) if real else ("DEMO", DEMO)
    window.setWindowTitle(f"Pocket Option · conectar cuenta {room}")
    window.resize(1280, 900)
    body = QWidget()
    box = QVBoxLayout(body)
    note = QLabel("1) Inicia sesión en Pocket Option (tú escribes tus datos en su página oficial).  "
                  f"2) Pulsa «Abrir sala {room}».  3) La sesión de la cuenta {room} se captura sola y se guarda protegida. "
                  + ("Una sesión DEMO se ignora. ⚠️ Esta sesión permite operar DINERO REAL." if real else
                     "Una sesión de cuenta REAL se ignora."))
    note.setWordWrap(True)
    status = QLabel(f"Esperando que abras la sala {room}…")
    status.setWordWrap(True)
    row = QHBoxLayout()
    demo_btn = QPushButton(f"Abrir sala {room}")
    close_btn = QPushButton("Cerrar")
    row.addWidget(demo_btn)
    public_btn = None
    if not real:
        public_btn = QPushButton("Demo pública (sin cuenta)")
        public_btn.setToolTip("Abre la terminal de prueba pública de Pocket Option: no pide usuario ni contraseña. "
                              "El bot queda operando sobre esa demo anónima, no sobre la cuenta del dueño.")
        row.addWidget(public_btn)
    row.addStretch()
    row.addWidget(close_btn)
    box.addWidget(note)
    box.addLayout(row)
    box.addWidget(status)

    profile = QWebEngineProfile("PocketTyl", window)
    profile.setPersistentStoragePath(str(root / "browser"))
    profile.setCachePath(str(root / "browser-cache"))
    # 4 Nov 2026: Pocket's reCAPTCHA would not let the login pass; the default agent says "QtWebEngine/6.x". The same
    # Chromium without that word reads as a normal Chrome (and is the agent the connector sends later).
    import re
    profile.setHttpUserAgent(re.sub(r"\s*QtWebEngine/\S+", "", profile.httpUserAgent()))
    script = QWebEngineScript()
    script.setName("pocket-tyl-auth")
    script.setSourceCode(HOOK)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(True)
    profile.scripts().insert(script)

    ua = profile.httpUserAgent()  # the page's own user agent: the connector sends the same one

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # every other console line is dropped
            shown = handle_console(message, self.url().host(), root, real, ua)
            if shown:
                status.setText(shown)
                if shown.startswith("✅"):
                    QTimer.singleShot(6000, window.close)

    view = QWebEngineView()
    view.setPage(Page(profile, view))
    public = (not real) and ("--publica" in sys.argv)
    view.setUrl(QUrl(TRY_DEMO if public else LOGIN))
    if public:
        status.setText("Abriendo la demo pública de Pocket Option (sin cuenta)…")

    class Bridge(QObject):
        auth = Signal(str, str, str)

    bridge = Bridge()

    def on_auth(payload, host, url):
        shown = handle_console(MARK + (url or "") + SEP + payload, host, root, real, ua)
        if shown:
            status.setText(shown)
            if shown.startswith("✅"):
                QTimer.singleShot(6000, window.close)
    bridge.auth.connect(on_auth)
    done = threading.Event()
    threading.Thread(target=lambda: asyncio.run(devtools_watch(lambda p, h, u: bridge.auth.emit(p, h, u or ""), done.is_set)),
                     daemon=True).start()
    app.aboutToQuit.connect(done.set)
    box.addWidget(view, 1)
    demo_btn.clicked.connect(lambda: view.setUrl(QUrl(room_url)))
    if public_btn is not None:
        public_btn.clicked.connect(lambda: view.setUrl(QUrl(TRY_DEMO)))
    close_btn.clicked.connect(window.close)
    window.setCentralWidget(body)
    window.showMaximized()  # the reCAPTCHA image grid opens to the right and was cut in a small window
    app.exec()


if __name__ == "__main__":
    main()
