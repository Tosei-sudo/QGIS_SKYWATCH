"""
Requests through QGIS's network stack, so QGIS's proxy settings and
authentication configurations (`authcfg`: OAuth2, ESRI token, basic …)
apply. Blocking requests (QgsBlockingNetworkRequest) are made only off the
main thread, inside a QgsTask; on the main thread they are refused.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QByteArray, QThread, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtWidgets import QApplication

from ..core.esri import EsriError, form_body

#: The authcfg for a URL (the plugin settings' choice for the service it belongs to).
AuthFor = Callable[[str], Optional[str]]

_ERRORS = {
    QgsBlockingNetworkRequest.ErrorCode.NetworkError: "接続不可",
    QgsBlockingNetworkRequest.ErrorCode.TimeoutError: "応答なし（タイムアウト）",
    QgsBlockingNetworkRequest.ErrorCode.ServerExceptionError: "サーバーのエラー",
}


def on_main_thread() -> bool:
    app = QApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def _request(url: str) -> QNetworkRequest:
    request = QNetworkRequest(QUrl(url))
    request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute, QNetworkRequest.CacheLoadControl.AlwaysNetwork)
    return request


def _fail(blocking: QgsBlockingNetworkRequest, url: str, code) -> EsriError:
    reason = _ERRORS.get(code, "接続不可")
    detail = blocking.errorMessage()
    # The address without its query: a token may be in it.
    return EsriError(f"{reason}（{url.split('?', 1)[0]}{'：' + detail if detail else ''}）")


def qgis_transport(auth_for: Optional[AuthFor] = None) -> Callable[[str, dict], bytes]:
    """A `Transport` for EsriClient: form POSTs through QgsBlockingNetworkRequest."""

    def send(url: str, form: dict[str, str]) -> bytes:
        if on_main_thread():
            raise RuntimeError("Esri への要求は QgsTask の中で実行")
        blocking = QgsBlockingNetworkRequest()
        authcfg = auth_for(url) if auth_for else None
        if authcfg:
            blocking.setAuthCfg(authcfg)
        request = _request(url)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/x-www-form-urlencoded")
        code = blocking.post(request, QByteArray(form_body(form)))
        if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
            raise _fail(blocking, url, code)
        return bytes(blocking.reply().content())

    return send


def get_bytes(url: str, authcfg: Optional[str] = None) -> bytes:
    """GETs a URL (off the main thread); raises EsriError when it cannot."""
    if on_main_thread():
        raise RuntimeError("通信は QgsTask の中で実行")
    blocking = QgsBlockingNetworkRequest()
    if authcfg:
        blocking.setAuthCfg(authcfg)
    code = blocking.get(_request(url))
    if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise _fail(blocking, url, code)
    return bytes(blocking.reply().content())
