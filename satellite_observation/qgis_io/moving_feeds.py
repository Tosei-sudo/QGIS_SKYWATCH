"""
Connections to moving-object feeds (the viewer's moving-feeds.ts): a
WebSocket, Server-Sent Events, a URL polled for GeoJSON / JSON, or an ArcGIS
Stream Service (its `/StreamServer` description names the WebSocket to
subscribe to). HTTP goes through QGIS's network manager (its proxy and
authentication settings); a WebSocket through Qt's QWebSocket where PyQt
has it, else through a small client of this module over a TCP (TLS) socket.

What comes in is checked before it is read: messages larger than
MAX_MESSAGE (and polled answers larger than MAX_POLL) are dropped, as are
messages beyond MAX_RATE a second, and text that is not JSON. WebSockets and
event streams connect again after a break, waiting longer each time.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit, parse_qsl, urlunsplit

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QAbstractSocket, QNetworkReply, QNetworkRequest, QSslSocket, QTcpSocket

from ..core.moving import FeedConfig, feed_url

try:  # Not every PyQt build has it.
    from qgis.PyQt.QtWebSockets import QWebSocket
except ImportError:  # pragma: no cover (depends on the PyQt build)
    QWebSocket = None

#: The largest WebSocket / SSE message read, in characters; the largest polled answer, in bytes.
MAX_MESSAGE = 2_000_000
MAX_POLL = 20_000_000
#: Messages read a second at most; the rest are dropped.
MAX_RATE = 500
#: The shortest time between polls, s.
MIN_INTERVAL = 2

STATUSES = {"connecting": "接続中…", "open": "受信中", "retrying": "再接続を待機中", "error": "エラー", "closed": "切断"}


class RateLimiter:
    """Counts messages per second and says whether one more may be read."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self.second = 0
        self.count = 0

    def __call__(self) -> bool:
        now = int(time.time())
        if now != self.second:
            self.second, self.count = now, 0
        self.count += 1
        return self.count <= self.maximum


class MiniWebSocket(QObject):
    """
    A WebSocket client (RFC 6455) over a TCP or TLS socket, for PyQt builds
    without QtWebSockets: text and binary messages (fragments joined), ping
    answered, close; no extensions.
    """
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    textMessageReceived = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.socket: Optional[QTcpSocket] = None
        self._buffer = b""
        self._open = False
        self._fragments = b""
        self._key = ""

    def open(self, url: QUrl) -> None:
        parts = urlsplit(url.toString())
        secure = parts.scheme == "wss"
        self.socket = QSslSocket(self) if secure else QTcpSocket(self)
        self._buffer, self._open, self._fragments = b"", False, b""
        path = parts.path or "/"
        if parts.query:
            path += f"?{parts.query}"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        self._key = base64.b64encode(os.urandom(16)).decode()
        host_header = host if parts.port is None else f"{host}:{port}"
        self._request = (f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Key: {self._key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
        self.socket.readyRead.connect(self._read)
        self.socket.disconnected.connect(self._gone)
        failed = self.socket.errorOccurred if hasattr(self.socket, "errorOccurred") else self.socket.error
        failed.connect(lambda _e: self._gone())
        if secure:
            self.socket.encrypted.connect(self._send_request)
            self.socket.connectToHostEncrypted(host, port)
        else:
            self.socket.connected.connect(self._send_request)
            self.socket.connectToHost(host, port)

    def _send_request(self) -> None:
        self.socket.write(self._request)

    def _gone(self) -> None:
        if self.socket is None:
            return
        socket, self.socket = self.socket, None
        socket.deleteLater()
        self.disconnected.emit()

    def close(self) -> None:
        if self.socket is not None:
            if self._open:
                self._send(0x8, b"")
            socket, self.socket = self.socket, None
            try:
                socket.disconnected.disconnect(self._gone)
            except (TypeError, RuntimeError):
                pass
            socket.abort()
            socket.deleteLater()

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.socket.write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self) -> None:
        if self.socket is None:
            return
        self._buffer += bytes(self.socket.readAll())
        if not self._open:
            end = self._buffer.find(b"\r\n\r\n")
            if end < 0:
                if len(self._buffer) > 65536:
                    self._gone()
                return
            head, self._buffer = self._buffer[:end].decode("latin-1"), self._buffer[end + 4:]
            if not head.split("\r\n", 1)[0].split(" ")[1:2] == ["101"]:
                self._gone()
                return
            self._open = True
            self.connected.emit()
        while self.socket is not None:
            frame = self._frame()
            if frame is None:
                return
            fin, opcode, payload = frame
            if opcode == 0x8:
                self._gone()
                return
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode in (0x1, 0x2, 0x0):
                self._fragments += payload
                if len(self._fragments) > MAX_MESSAGE * 4:
                    self._fragments = b""
                    continue
                if fin:
                    data, self._fragments = self._fragments, b""
                    self.textMessageReceived.emit(data.decode("utf-8", errors="replace"))

    def _frame(self):
        b = self._buffer
        if len(b) < 2:
            return None
        fin, opcode = b[0] & 0x80, b[0] & 0x0F
        masked, n, at = b[1] & 0x80, b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, at = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, at = struct.unpack(">Q", b[2:10])[0], 10
        mask = b""
        if masked:
            mask, at = b[at:at + 4], at + 4
        if len(b) < at + n:
            return None
        payload = b[at:at + n]
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self._buffer = b[at + n:]
        return bool(fin), opcode, payload


def new_websocket(parent: QObject):
    """Qt's QWebSocket when PyQt has it, else MiniWebSocket."""
    if QWebSocket is not None and not os.environ.get("SATOBS_MINI_WEBSOCKET"):
        return QWebSocket("", 13, parent)
    return MiniWebSocket(parent)


class FeedConnection(QObject):
    """An open feed: `message(data, received_at_ms)`, `status(state, detail)` and, for a Stream Service, `fields(dict)`."""
    message = pyqtSignal(object, float)
    status = pyqtSignal(str, str)
    fields = pyqtSignal(dict)

    def __init__(self, feed: FeedConfig, base: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.feed = feed
        self.closed = False
        self.dropped = 0
        self.state = "connecting"
        self._allow = RateLimiter(MAX_RATE)
        self._wait = 2000
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._socket = None
        self._reply: Optional[QNetworkReply] = None
        self._sse_buffer = b""
        self._sse_data: list[str] = []
        try:
            self.url = feed_url(feed.url, feed.type, base)
        except ValueError as e:
            self.url = ""
            QTimer.singleShot(0, lambda: self._say("error", str(e)))
            return
        start = {"websocket": lambda: self._socket_open(self.url), "sse": self._sse_open, "poll": self._poll,
                 "esri-stream": self._stream_open}[feed.type]
        QTimer.singleShot(0, start)

    def _say(self, state: str, detail: str = "") -> None:
        if self.closed and state != "closed":
            return
        self.state = state
        self.status.emit(state, detail)

    def take(self, text: str) -> None:
        """Reads one message (dropped when too large, too many or not JSON)."""
        if self.closed:
            return
        if len(text) > MAX_MESSAGE or not self._allow():
            self.dropped += 1
            return
        try:
            data = json.loads(text)
        except ValueError:
            self.dropped += 1
            return
        self.message.emit(data, time.time() * 1000)

    def _again(self, start) -> None:
        """Connects again after a break (2 s, 4 s… up to a minute)."""
        if self.closed:
            return
        self._say("retrying", f"{round(self._wait / 1000)} 秒後に再接続")
        try:
            self._timer.timeout.disconnect()
        except TypeError:
            pass
        self._timer.timeout.connect(start)
        self._timer.start(self._wait)
        self._wait = min(60_000, self._wait * 2)

    # WebSocket.

    def _socket_open(self, url: str) -> None:
        if self.closed:
            return
        self._say("connecting")
        socket = self._socket = new_websocket(self)

        def opened():
            self._wait = 2000
            self._say("open")

        def gone():
            if socket is self._socket:
                self._socket = None
                self._again(lambda: self._socket_open(url))

        socket.connected.connect(opened)
        socket.disconnected.connect(gone)
        socket.textMessageReceived.connect(self.take)
        if hasattr(socket, "binaryMessageReceived"):
            socket.binaryMessageReceived.connect(lambda data: self.take(bytes(data).decode("utf-8", errors="replace")))
        socket.open(QUrl(url))

    # Server-Sent Events.

    def _sse_open(self) -> None:
        if self.closed:
            return
        self._say("connecting")
        request = QNetworkRequest(QUrl(self.url))
        request.setRawHeader(b"Accept", b"text/event-stream")
        request.setRawHeader(b"Cache-Control", b"no-cache")
        reply = self._reply = QgsNetworkAccessManager.instance().get(request)
        self._sse_buffer, self._sse_data = b"", []

        def read():
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if status is not None and int(status) >= 400:
                return
            if self.state != "open":
                self._wait = 2000
                self._say("open")
            self._sse_buffer += bytes(reply.readAll())
            *lines, self._sse_buffer = self._sse_buffer.split(b"\n")
            for raw in lines:
                line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
                if not line:
                    if self._sse_data:
                        self.take("\n".join(self._sse_data))
                    self._sse_data = []
                elif line.startswith("data:"):
                    self._sse_data.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
            if len(self._sse_buffer) > MAX_MESSAGE:
                self._sse_buffer = b""
                self.dropped += 1

        def finished():
            if reply is not self._reply:
                return
            self._reply = None
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            reply.deleteLater()
            if self.closed:
                return
            if status is not None and int(status) >= 400:
                self._say("error", f"サーバーからエラー応答（HTTP {int(status)}）")
                return
            self._again(self._sse_open)

        reply.readyRead.connect(read)
        reply.finished.connect(finished)

    # Polling.

    def _poll(self) -> None:
        if self.closed:
            return
        if self.state != "open":
            self._say("connecting")
        request = QNetworkRequest(QUrl(self.url))
        request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute, QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        reply = self._reply = QgsNetworkAccessManager.instance().get(request)
        interval = max(MIN_INTERVAL, self.feed.interval or 10) * 1000

        def finished():
            if reply is not self._reply:
                return
            self._reply = None
            reply.deleteLater()
            if self.closed:
                return
            try:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if reply.error() != QNetworkReply.NetworkError.NoError and status is None:
                    raise ValueError(f"接続失敗（{reply.errorString()}）")
                if status is not None and int(status) >= 400:
                    raise ValueError(f"サーバーからエラー応答（HTTP {int(status)}）")
                body = bytes(reply.readAll())
                if len(body) > MAX_POLL:
                    raise ValueError(f"応答が大きすぎ（{round(len(body) / 1e6)} MB）")
                try:
                    data = json.loads(body.decode("utf-8-sig"))
                except (UnicodeDecodeError, ValueError):
                    raise ValueError("応答が JSON 以外")
                self._say("open")
                self.message.emit(data, time.time() * 1000)
            except ValueError as e:
                self._say("error", str(e))
            if not self.closed:
                try:
                    self._timer.timeout.disconnect()
                except TypeError:
                    pass
                self._timer.timeout.connect(self._poll)
                self._timer.start(int(interval))

        reply.finished.connect(finished)

    # ArcGIS Stream Service.

    def _stream_open(self) -> None:
        """Its description names the WebSocket (…/subscribe) and the track id and time fields."""
        self._say("connecting")
        parts = urlsplit(self.url)
        query = dict(parse_qsl(parts.query))
        token = query.get("token")
        info = {"f": "json", **({"token": token} if token else {})}
        reply = self._reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(urlunsplit(parts._replace(query=urlencode(info))))))

        def finished():
            if reply is not self._reply:
                return
            self._reply = None
            reply.deleteLater()
            if self.closed:
                return
            try:
                ws, fields = stream_socket(bytes(reply.readAll()), reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute), self.url)
            except ValueError as e:
                self._say("error", str(e))
                return
            if fields:
                self.fields.emit(fields)
            self._socket_open(ws)

        reply.finished.connect(finished)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._timer.stop()
        if self._reply is not None:
            reply, self._reply = self._reply, None
            reply.abort()
            reply.deleteLater()
        if self._socket is not None:
            socket, self._socket = self._socket, None
            try:
                socket.disconnected.disconnect()
            except (TypeError, RuntimeError):
                pass
            socket.close()
        self._say("closed")


def stream_socket(body: bytes, status, url: str) -> tuple[str, dict]:
    """The WebSocket of an ArcGIS Stream Service, and its track id and time fields, from its description."""
    if status is not None and int(status) >= 400:
        raise ValueError(f"Stream Service からエラー応答（HTTP {int(status)}）")
    try:
        info = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("Stream Service の説明が JSON 以外")
    if not isinstance(info, dict):
        raise ValueError("Stream Service の説明が JSON 以外")
    if isinstance(info.get("error"), dict):
        raise ValueError(f"Stream Service からエラー応答: {str(info['error'].get('message', ''))[:200]}")
    entry = next((s for s in info.get("streamUrls") or [] if isinstance(s, dict) and s.get("transport") == "ws" and s.get("urls")), None)
    base = next((u for u in (entry or {}).get("urls", []) if isinstance(u, str)), None)
    if not base:
        raise ValueError("Stream Service に WebSocket の URL（streamUrls）なし")
    ws = base if base.rstrip("/").lower().endswith("/subscribe") else f"{base.rstrip('/')}/subscribe"
    parts, typed = urlsplit(ws), urlsplit(url)
    token = entry.get("token") or (dict(parse_qsl(typed.query)).get("token") if parts.hostname == typed.hostname else None)
    if token:
        query = dict(parse_qsl(parts.query))
        query["token"] = token
        ws = urlunsplit(parts._replace(query=urlencode(query)))
    ws = feed_url(ws, "websocket")
    fields = {}
    time_info = info.get("timeInfo") or {}
    if isinstance(time_info.get("trackIdField"), str):
        fields["id"] = time_info["trackIdField"]
    if isinstance(time_info.get("startTimeField"), str):
        fields["time"] = time_info["startTimeField"]
    return ws, fields
