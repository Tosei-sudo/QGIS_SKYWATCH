"""
Requests through QGIS's network stack, so QGIS's proxy settings and
authentication configurations (`authcfg`: OAuth2, ESRI token, basic …)
apply. Requests that wait for their answer are made only off the main
thread, inside a QgsTask (and stop when it is cancelled); on the main
thread they are refused.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from qgis.core import QgsApplication, QgsNetworkAccessManager
from qgis.PyQt.QtCore import QByteArray, QEventLoop, QThread, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import QApplication

from ..core.esri import EsriError, form_body
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
    """One request in a thread of its own (with its own network access manager), as QgsBlockingNetworkRequest makes it."""

    def __init__(self, request: QNetworkRequest, authcfg: Optional[str], body: Optional[bytes], feedback):
        super().__init__()
        self.request, self.authcfg, self.body, self.feedback = request, authcfg, body, feedback
        self.result: Optional[bytes] = None
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            self.result = _exchange(self.request, self.authcfg, self.body, self.feedback)
        except BaseException as error:  # noqa: BLE001 (handed to the task's thread)
            self.error = error


def _send(request: QNetworkRequest, authcfg: Optional[str], body: Optional[bytes] = None) -> bytes:
    """
    Makes a request and waits for its answer; it stops as soon as the task
    running it is cancelled (QgsBlockingNetworkRequest's own cancel could leave
    it waiting for good).
    """
    feedback = current_feedback()
    if feedback is not None and feedback.isCanceled():
        raise Cancelled()
    exchange = _Exchange(request, authcfg, body, feedback)
    exchange.start()
    while not exchange.wait(100):
        pass
    if exchange.error is not None:
        raise exchange.error
    return exchange.result


def _exchange(request: QNetworkRequest, authcfg: Optional[str], body: Optional[bytes], feedback) -> bytes:
    """The request, waited for in a local event loop that aborts it when `feedback` is cancelled or it takes too long."""
    url = request.url().toString()
    if authcfg and not QgsApplication.authManager().updateNetworkRequest(request, authcfg):
        raise EsriError(f"認証設定（{authcfg}）を使えず（{_address(url)}）")
    request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
    manager = QgsNetworkAccessManager.instance()
    # Kept until the answer: Qt reads the body while sending it.
    payload = QByteArray(body) if body is not None else None
    reply = manager.post(request, payload) if payload is not None else manager.get(request)
    if authcfg:
        QgsApplication.authManager().updateNetworkReply(reply, authcfg)
    loop = QEventLoop()
    reply.finished.connect(loop.quit)
    timer = QTimer()
    timeout = QgsNetworkAccessManager.timeout()
    started = time.monotonic()
    state = {"timed_out": False}

    def watch() -> None:
        if reply.isFinished():
            loop.quit()
        elif feedback is not None and feedback.isCanceled():
            reply.abort()
        elif timeout > 0 and (time.monotonic() - started) * 1000 > timeout:
            state["timed_out"] = True
            reply.abort()

    timer.timeout.connect(watch)
    timer.start(50)
    if not reply.isFinished():
        loop.exec()
    timer.stop()
    try:
        if feedback is not None and feedback.isCanceled():
            raise Cancelled()
        if state["timed_out"]:
            raise EsriError(f"応答なし（タイムアウト）（{_address(url)}）")
        if reply.error() != QNetworkReply.NetworkError.NoError:
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            reason = "サーバーのエラー" if isinstance(status, int) and status >= 500 else "接続不可"
            # Qt's message repeats the address with its query: a token may be in it.
            detail = re.sub(r"\?\S*", "", reply.errorString())
            raise EsriError(f"{reason}（{_address(url)}{'：' + detail if detail else ''}）")
        return bytes(reply.readAll())
    finally:
        reply.deleteLater()


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
