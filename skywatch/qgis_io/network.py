"""
Requests through QGIS's network stack, so QGIS's proxy settings and
authentication configurations (`authcfg`: OAuth2, ESRI token, basic …)
apply. Requests that wait for their answer are made only off the main
thread, inside a QgsTask (and stop when it is cancelled); on the main
thread they are refused.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Optional

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QByteArray, QThread, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import QApplication

from ..core.esri import EsriError, UnreachableError, form_body
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


class _Exchange(QThread):
    """
    One request in a thread of its own, made and waited for by QGIS in C++
    (QgsNetworkAccessManager.blockingGet / blockingPost, with that thread's
    own network access manager). QGIS lets go of Python's lock (the GIL) for
    the whole request, and no Qt object of the request is touched from Python.

    Before, the request was driven from Python here (the reply, a local event
    loop, a timer, their connections), which held the GIL while Qt took its
    own locks (sending, connecting, signals to the main thread). When the main
    thread, deleting widgets, held one of those Qt locks and waited for the
    GIL, both waited for good and QGIS froze (rare, under load). The Python
    wrappers of these short-lived objects were also at times taken for new
    objects made at the same address ("wrapped C/C++ object … has been
    deleted", answers "cut off").
    """

    def __init__(self, request: QNetworkRequest, authcfg: Optional[str], body: Optional[bytes]):
        super().__init__()
        self.request, self.authcfg, self.body = request, authcfg or "", body
        #: QgsNetworkReplyContent: what came back (status, error, body), copied by QGIS.
        self.reply = None
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            if self.body is None:
                self.reply = QgsNetworkAccessManager.blockingGet(self.request, self.authcfg, True)
            else:
                self.reply = QgsNetworkAccessManager.blockingPost(self.request, QByteArray(self.body), self.authcfg, True)
        except BaseException as error:  # noqa: BLE001 (handed to the waiting thread)
            self.error = error


#: Requests given up on (cancelled, timed out) whose thread is still ending: a running QThread must not be deleted.
_ending: list[_Exchange] = []
_ending_lock = threading.Lock()


def _let_end(exchange: _Exchange) -> None:
    """Keeps `exchange` until its thread has ended (it ends at once: its event loop was quit)."""
    with _ending_lock:
        _ending[:] = [e for e in _ending if not e.wait(0)]
        if not exchange.wait(0):
            _ending.append(exchange)


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
    Makes a request and waits for its answer; it stops as soon as the task
    running it is cancelled or it takes too long. The wait is watched from
    here (QgsBlockingNetworkRequest's own cancel could leave it waiting for
    good): quitting the request's thread ends the event loop QGIS waits in,
    and the request is dropped with it.
    """
    feedback = current_feedback()
    if feedback is not None and feedback.isCanceled():
        raise Cancelled()
    url = request.url().toString()
    request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
    limit = _time_limit_ms(url)
    exchange = _Exchange(request, authcfg, body)
    started = time.monotonic()
    exchange.start()
    while not exchange.wait(50):
        cancelled = feedback is not None and feedback.isCanceled()
        if cancelled or limit > 0 and (time.monotonic() - started) * 1000 > limit:
            exchange.quit()
            _let_end(exchange)
            if cancelled:
                raise Cancelled()
            raise UnreachableError(f"応答なし（タイムアウト）（{_address(url)}）")
    if feedback is not None and feedback.isCanceled():
        raise Cancelled()
    if exchange.error is not None:
        raise exchange.error
    return _answer(exchange.reply, url, authcfg)


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
        raise (UnreachableError if unreachable else EsriError)(f"{reason}（{_address(url)}{'：' + detail if detail else ''}）")
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
