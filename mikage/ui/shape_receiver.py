"""
「外部から図形を受け取る」: the local HTTP server (core/shape_receiver.py) in
QGIS's main thread — a QTcpServer on 127.0.0.1 only (no firewall question,
nothing from other computers), at the settings file's `shapeReceiver.port`.
What it receives (AutoMap's 「MIKAGE に送る」, a link, a program) becomes
temporary layers in the group 「受け取った図形」 and the map goes there
(qgis_io/link_shapes_io.py); QGIS's window is brought forward (Windows may
only flash it in the taskbar), the message bar says what came from whom,
and the audit log keeps it.

The port already taken (another QGIS of this person, another program) is only
logged: AutoMap then says MIKAGE did not answer.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QObject, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from qgis.PyQt.QtWidgets import QApplication

from ..core import audit, errors
from ..core.link_shapes import read_link_shapes
from ..core.shape_receiver import (
    MAX_CONNECTIONS, READ_TIMEOUT_MS, HttpError, ReceiverSettings, Response, Sender, handle, json_response, parse_request,
)
from ..qgis_io.link_shapes_io import add_layers, layers_of_shapes, zoom_to
from ..qgis_io.preload import LOG_TAG
from .docks import plain_tip


class ShapeReceiver(QObject):
    """The server, started and stopped as the settings file says."""

    #: A sending shown: how many shapes, the layer's name, who sent.
    received = pyqtSignal(int, str, str)
    #: False: never listens (the plugin's tests, but those of the receiver: tests/conftest.py).
    LISTEN = True

    def __init__(self, iface, context, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.server: Optional[QTcpServer] = None
        #: The settings it listens with (None: not listening).
        self.settings: Optional[ReceiverSettings] = None
        #: Why it does not listen ("" when it does or is off).
        self.problem = ""
        self._connections: dict[QTcpSocket, tuple[bytearray, QTimer]] = {}
        context.configChanged.connect(self._config_changed)

    # --- Starting and stopping ---------------------------------------------------------------------------

    @property
    def listening(self) -> bool:
        return self.server is not None and self.server.isListening()

    @property
    def port(self) -> Optional[int]:
        """The port listened at (None when not)."""
        return self.server.serverPort() if self.listening else None

    @property
    def url(self) -> str:
        """Where to send (`http://127.0.0.1:<port>`), "" when not listening."""
        return f"http://127.0.0.1:{self.port}" if self.listening else ""

    def _config_changed(self) -> None:
        """The settings file read: listens as it says (tried again at each read while the port was taken)."""
        if self.context is None or self.context.loading:
            return
        wanted = getattr(self.context.config, "shape_receiver", None) or ReceiverSettings(enabled=False)
        if not wanted.enabled or not self.LISTEN:
            self.stop()
        elif self.listening and self.settings is not None and self.settings.port == wanted.port:
            self.settings = wanted  # only who may send changed
        else:
            self.start(wanted)

    def start(self, settings: ReceiverSettings) -> bool:
        """Listens at 127.0.0.1:port; False (the reason in `problem` and QGIS's log) when the port is taken."""
        self.stop()
        server = QTcpServer(self)
        server.setMaxPendingConnections(MAX_CONNECTIONS)
        if not server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), settings.port):
            self.problem = errors.tagged(self.tr("ポート {p} で待ち受けられない。別の QGIS が受け取っているか、ほかのプログラムが使用中").format(
                p=settings.port), "E110")
            QgsMessageLog.logMessage(self.tr("外部から図形を受け取る: ") + f"{self.problem}（{server.errorString()}）", LOG_TAG,
                                     Qgis.MessageLevel.Info, notifyUser=False)
            server.deleteLater()
            return False
        server.newConnection.connect(self._new_connection)
        self.server = server
        self.settings = settings
        self.problem = ""
        QgsMessageLog.logMessage(self.tr("外部から図形を受け取る: http://127.0.0.1:{p} で待ち受け").format(p=settings.port), LOG_TAG,
                                 Qgis.MessageLevel.Info, notifyUser=False)
        return True

    def stop(self) -> None:
        for socket in list(self._connections):
            self._drop(socket)
        if self.server is not None:
            try:
                self.server.newConnection.disconnect(self._new_connection)
            except (TypeError, RuntimeError):
                pass
            self.server.close()
            self.server.deleteLater()
            self.server = None
        self.settings = None
        self.problem = ""

    def close(self) -> None:
        try:
            self.context.configChanged.disconnect(self._config_changed)
        except (TypeError, RuntimeError, AttributeError):
            pass
        self.stop()
        self.context = None

    # --- Connections ---------------------------------------------------------------------------------------

    def _new_connection(self) -> None:
        server = self.server
        while server is not None and server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket is None:
                break
            if len(self._connections) >= MAX_CONNECTIONS or not socket.peerAddress().isLoopback():
                socket.abort()
                socket.deleteLater()
                continue
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda s=socket: self._drop(s))
            timer.start(READ_TIMEOUT_MS)
            self._connections[socket] = (bytearray(), timer)
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._drop(s, closing=True))

    def _read(self, socket: QTcpSocket) -> None:
        entry = self._connections.get(socket)
        if entry is None:
            return
        buffer, _timer = entry
        buffer.extend(bytes(socket.readAll()))
        try:
            request = parse_request(bytes(buffer))
        except HttpError as error:
            self._answer(socket, json_response(error.status, {"app": "MIKAGE", "ok": False, "error": error.message}))
            return
        if request is None:
            return
        settings = self.settings
        if settings is None:
            self._answer(socket, json_response(503, {"app": "MIKAGE", "ok": False, "error": "受け取りを停止中"}))
            return
        self._answer(socket, handle(request, settings, self.show))

    def _answer(self, socket: QTcpSocket, response: Response) -> None:
        entry = self._connections.pop(socket, None)
        if entry is not None:
            entry[1].stop()
            entry[1].deleteLater()
        try:
            socket.readyRead.disconnect()
        except (TypeError, RuntimeError):
            pass
        socket.disconnected.connect(socket.deleteLater)
        socket.write(response.to_bytes())
        socket.disconnectFromHost()
        if socket.state() == QTcpSocket.SocketState.UnconnectedState:
            socket.deleteLater()

    def _drop(self, socket: QTcpSocket, closing: bool = False) -> None:
        entry = self._connections.pop(socket, None)
        if entry is None:
            return
        entry[1].stop()
        entry[1].deleteLater()
        if not closing:
            socket.abort()
        socket.deleteLater()

    # --- The shapes ----------------------------------------------------------------------------------------

    def show(self, params: list, max_value: int, sender: Sender) -> tuple[int, str, list[str]]:
        """Shows the shapes of the parameters (core/link_shapes.py): (how many, the layer's name, what could not be read)."""
        shapes = read_link_shapes(params, max_value)
        n = len(shapes.shapes)
        audit.record("receive", "shapes", sender.text, result="ok" if n else "error", count=n,
                     detail={"layer": shapes.title, "errors": shapes.errors[:20]} if shapes.errors else {"layer": shapes.title})
        bar = self.iface.messageBar()
        title = self.tr("外部から図形を受け取る")
        if not n:
            bar.pushWarning(title, plain_tip(self.tr("{who} からの図形を表示できなかった（{e}）").format(
                who=sender.text, e=" / ".join(shapes.errors[:5]) or self.tr("図形なし"))))
            return 0, shapes.title, shapes.errors
        layers = layers_of_shapes(shapes, sender.text)
        add_layers(layers)
        zoom_to(self.iface.mapCanvas(), shapes)
        self._bring_forward()
        text = self.tr("{who} から図形 {n} 個を受け取り、レイヤー「{layer}」に追加").format(who=sender.text, n=f"{n:,}", layer=shapes.title)
        if shapes.errors:
            text += self.tr("（読めなかったもの: {e}）").format(e=" / ".join(shapes.errors[:5]))
            bar.pushWarning(title, plain_tip(text))
        else:
            bar.pushSuccess(title, plain_tip(text))
        self.received.emit(n, shapes.title, sender.text)
        return n, shapes.title, shapes.errors

    def show_link(self, text: str, sender: str = "Python") -> tuple[int, str, list[str]]:
        """The shapes of `geom=…&label=…` (a link's parameters, or a whole `http://127.0.0.1:…/show?…`) shown as if sent."""
        from ..core.link_shapes import MAX_VALUE, params_of_query

        query = text.split("?", 1)[1] if "?" in text else text
        return self.show(params_of_query(query), MAX_VALUE, Sender(sender))

    def _bring_forward(self) -> None:
        """QGIS's window in front (out of the taskbar when minimized); Windows may only flash it."""
        window = self.iface.mainWindow()
        if window is None:
            return
        if window.isMinimized():
            window.setWindowState((window.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        window.raise_()
        window.activateWindow()
        QApplication.alert(window, 0)
