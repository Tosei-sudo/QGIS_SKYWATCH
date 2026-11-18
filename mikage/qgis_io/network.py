"""
Requests through QGIS's network stack, so QGIS's proxy settings and
authentication configurations (`authcfg`: OAuth2, ESRI token, basic …)
apply. Requests that wait for their answer are made only off the main
thread, inside a QgsTask (and stop when it is cancelled); on the main
thread they are refused.
"""
from __future__ import annotations

import atexit
import queue
import re
import threading
import time
from typing import Callable, Optional

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QByteArray, QThread, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import QApplication

from ..core.esri import BusyError, EsriError, UnreachableError, form_body
from .tasks import Cancelled, current_feedback

#: The authcfg for a URL (the plugin settings' choice for the service it belongs to).
AuthFor = Callable[[str], Optional[str]]

def on_main_thread() -> bool:
    app = QApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def _request(url: str) -> QNetworkRequest:
    request = QNetworkRequest(QUrl(url))
    request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute, QNetworkRequest.CacheLoadControl.AlwaysNetwork)
    return request


class _Line(QThread):
    """
    A thread kept for the plugin's requests, one request at a time, each
    made and waited for by QGIS in C++ (QgsNetworkAccessManager.blockingGet /
    blockingPost). QGIS keeps a network access manager per thread, and that
    keeps the connections it opened: the next request to the same server
    goes on the open connection, without a new connection, TLS handshake or
    Windows sign-in (NTLM / Kerberos) of its own (2026-11-18: each request
    had a thread, and so a connection, of its own; on a slow or far server
    that was a round trip or more each time, and work for the server too).

    QGIS lets go of Python's lock (the GIL) for the whole request, and no Qt
    object of the request is touched from Python. Before (2026-11-09), the
    request was driven from Python here (the reply, a local event loop, a
    timer, their connections), which held the GIL while Qt took its own locks
    (sending, connecting, signals to the main thread). When the main thread,
    deleting widgets, held one of those Qt locks and waited for the GIL, both
    waited for good and QGIS froze (rare, under load). The Python wrappers of
    these short-lived objects were also at times taken for new objects made
    at the same address ("wrapped C/C++ object … has been deleted", answers
    "cut off").
    """

    def __init__(self):
        super().__init__()
        self._jobs: "queue.SimpleQueue[Optional[_Job]]" = queue.SimpleQueue()
        #: When it last ended a request (time.monotonic()).
        self.used = time.monotonic()

    def hand(self, job: "_Job") -> None:
        self._jobs.put(job)

    def stop(self) -> None:
        """It ends once its request (if any) has."""
        self._jobs.put(None)

    def run(self) -> None:
        while True:
            job = self._jobs.get()  # waits with the GIL let go
            if job is None:
                return
            job.run()


class _Job:
    """One request and what came back."""

    def __init__(self, request: QNetworkRequest, authcfg: Optional[str], body: Optional[bytes]):
        self.request, self.authcfg, self.body = request, authcfg or "", body
        #: QgsNetworkReplyContent: what came back (status, error, body), copied by QGIS.
        self.reply = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()

    def run(self) -> None:
        try:
            if self.body is None:
                self.reply = QgsNetworkAccessManager.blockingGet(self.request, self.authcfg, True)
            else:
                self.reply = QgsNetworkAccessManager.blockingPost(self.request, QByteArray(self.body), self.authcfg, True)
        except BaseException as error:  # noqa: BLE001 (handed to the waiting thread)
            self.error = error
        finally:
            self.done.set()


#: Lines waiting for a request (the one used last at the end): at most KEEP_LINES, each for at most IDLE_SECONDS.
_idle: list[_Line] = []
_lines_lock = threading.Lock()
KEEP_LINES = 6
#: A line idle longer ends, and its connections with it (servers close theirs after a minute or two anyway).
IDLE_SECONDS = 100.0
#: The plugin is unloaded: lines are not kept (close_lines; open_lines when it is loaded again).
_closed = False

#: Lines that are ending (a request given up on, a line let go): a running QThread must not be deleted.
_ending: list[_Line] = []
_ending_lock = threading.Lock()


def _let_end(line: _Line) -> None:
    """Keeps `line` until its thread has ended."""
    with _ending_lock:
        _ending[:] = [e for e in _ending if not e.wait(0)]
        if not line.wait(0):
            _ending.append(line)


def _retire(line: _Line, quit_request: bool = False) -> None:
    """
    Lets a line end. `quit_request`: its request is given up on: quitting
    the thread ends the event loop QGIS waits in (and any it starts after),
    and the request is dropped with it; the line is never used again.
    """
    if quit_request:
        line.quit()
    line.stop()
    _let_end(line)


def _take() -> _Line:
    """A line for a request: one kept (its connections open), else a new one."""
    with _lines_lock:
        now = time.monotonic()
        stale = [line for line in _idle if now - line.used > IDLE_SECONDS]
        _idle[:] = [line for line in _idle if line not in stale]
        line = _idle.pop() if _idle else None
    for old in stale:
        _retire(old)
    if line is None:
        line = _Line()
        line.start()
    return line


def _give_back(line: _Line) -> None:
    """The request of `line` has ended: it waits for the next (unless enough wait already)."""
    line.used = time.monotonic()
    with _lines_lock:
        if not _closed and len(_idle) < KEEP_LINES:
            _idle.append(line)
            return
    _retire(line)


def open_lines() -> None:
    """The plugin is loaded: lines are kept between requests."""
    global _closed
    _closed = False


def close_lines(wait_ms: int = 2000) -> None:
    """The plugin is unloaded (or Python ends): the lines waiting end, and their connections; waits up to `wait_ms`."""
    global _closed
    with _lines_lock:
        _closed = True
        idle, _idle[:] = list(_idle), []
    for line in idle:
        _retire(line)
    deadline = time.monotonic() + wait_ms / 1000
    with _ending_lock:
        ending = list(_ending)
    for line in ending:
        left = int((deadline - time.monotonic()) * 1000)
        if left <= 0 or not line.wait(left):
            break
    with _ending_lock:
        _ending[:] = [e for e in _ending if not e.wait(0)]


# A thread still running when Python ends would take QGIS (or the tests) down with it.
atexit.register(close_lines)


#: Edit requests (applyEdits and the like) wait at least this long (ms).
EDIT_TIMEOUT_MS = 180_000
_EDITS = re.compile(r"/(applyEdits|addFeatures|updateFeatures|deleteFeatures)/?$", re.IGNORECASE)


def _time_limit_ms(url: str) -> int:
    """How long a request may take (0: no limit of its own; QGIS's network timeout still applies)."""
    timeout = QgsNetworkAccessManager.timeout()
    if timeout > 0 and _EDITS.search(url.split("?", 1)[0]):
        # Edits wait longer than reads: a large batch on a busy server is still being saved when a read would give up (#73).
        timeout = max(timeout * 3, EDIT_TIMEOUT_MS)
    return timeout


def _send(request: QNetworkRequest, authcfg: Optional[str], body: Optional[bytes] = None) -> bytes:
    """
    Makes a request (on a line kept for requests) and waits for its answer;
    it stops as soon as the task running it is cancelled or it takes too
    long. The wait is watched from here (QgsBlockingNetworkRequest's own
    cancel could leave it waiting for good): quitting the line's thread ends
    the event loop QGIS waits in, and the request is dropped with it (the
    line is not used again).
    """
    feedback = current_feedback()
    if feedback is not None and feedback.isCanceled():
        raise Cancelled()
    url = request.url().toString()
    request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
    limit = _time_limit_ms(url)
    job = _Job(request, authcfg, body)
    line = _take()
    started = time.monotonic()
    line.hand(job)
    while not job.done.wait(0.05):
        cancelled = feedback is not None and feedback.isCanceled()
        if cancelled or limit > 0 and (time.monotonic() - started) * 1000 > limit:
            _retire(line, quit_request=True)
            if cancelled:
                raise Cancelled()
            raise BusyError(f"応答なし（タイムアウト）（{_address(url)}）")
    _give_back(line)
    if feedback is not None and feedback.isCanceled():
        raise Cancelled()
    if job.error is not None:
        raise job.error
    return _answer(job.reply, url, authcfg)


def _answer(reply, url: str, authcfg: Optional[str]) -> bytes:
    """The body of `reply` (a QgsNetworkReplyContent), or the error it is."""
    error = reply.error()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if error == QNetworkReply.NetworkError.NoError and not isinstance(status, int):
        # Nothing was sent: QGIS could not apply the authentication (it says so in its log), or the answer was lost.
        if authcfg:
            raise EsriError(f"認証設定（{authcfg}）を使えず（{_address(url)}）")
        raise UnreachableError(f"接続不可（{_address(url)}：応答が途中で切れた）")
    if error != QNetworkReply.NetworkError.NoError:
        reason = "サーバーのエラー" if isinstance(status, int) and status >= 500 else "接続不可"
        # Qt's message repeats the address with its query: a token may be in it.
        detail = re.sub(r"\?\S*", "", reply.errorString())
        # Not reached (or the server is down): the plugin works offline until it answers (core/offline.py).
        # A 4xx answer is the server's refusal, not an outage.
        unreachable = not isinstance(status, int) or status <= 0 or status >= 500
        # A 5xx (and a timeout, above) may be a server only busy: BusyError (core/offline.py waits for a second one).
        kind = BusyError if isinstance(status, int) and status >= 500 else UnreachableError if unreachable else EsriError
        raise kind(f"{reason}（{_address(url)}{'：' + detail if detail else ''}）")
    return bytes(reply.content())


def _address(url: str) -> str:
    """The address without its query: a token may be in it."""
    return url.split("?", 1)[0]


def qgis_transport(auth_for: Optional[AuthFor] = None) -> Callable[[str, dict], bytes]:
    """A `Transport` for EsriClient: form POSTs through QGIS's network access manager."""

    def send(url: str, form: dict[str, str]) -> bytes:
        if on_main_thread():
            raise RuntimeError("Esri への要求は QgsTask の中で実行")
        request = _request(url)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/x-www-form-urlencoded")
        return _send(request, auth_for(url) if auth_for else None, form_body(form))

    return send


def get_bytes(url: str, authcfg: Optional[str] = None) -> bytes:
    """GETs a URL (off the main thread); raises EsriError when it cannot."""
    if on_main_thread():
        raise RuntimeError("通信は QgsTask の中で実行")
    return _send(_request(url), authcfg)
