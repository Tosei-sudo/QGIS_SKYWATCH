"""
外部から図形を受け取る (2026-11-15, tsudo: 「AutoMap から、ミカゲに送る流れにしたい」,
registry writes impossible there, so no URL scheme): a tiny HTTP server on
this computer only (127.0.0.1), which takes shapes (core/link_shapes.py) and
shows them on the map. ui/shape_receiver.py runs it in QGIS's main thread;
here is what needs no Qt: the settings file's `shapeReceiver`, reading a
request, who may send, and the answers.

- `GET /status` (or `/`): whether MIKAGE listens (`{"app": "MIKAGE", "ok": true}`).
- `GET /show?geom=…&label=…`: shapes in a link (the viewer's parameters),
  answered with a short page ("sent, this tab can be closed").
- `POST /show`: the same parameters as a JSON object (`geojson` may be an
  object) or as a form (`geom=…&label=…`), answered with JSON
  `{"ok", "count", "layer", "errors"}`. AutoMap sends this way.
- `OPTIONS`: CORS preflight for the origins allowed.

Who may send (the rest get 403 and nothing is shown):
- a request whose `Origin` is in `shapeReceiver.origins` (by default any
  browser extension, `chrome-extension://*`: AutoMap sends from its service
  worker; a web page cannot claim an extension's origin);
- a program on this computer (no `Origin` and no `Sec-Fetch-*` headers:
  curl, PowerShell, Python);
- a browser navigation the person made (`Sec-Fetch-Site: none`: typed,
  a bookmark, a link opened from another program), or one from a page of an
  origin allowed (its `Referer`).
A page's image, script, frame or fetch never gets through (a page could
otherwise put shapes on someone's map), and the `Host` must be this computer
(no DNS rebinding).
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .link_shapes import (
    MAX_BODY_VALUE, MAX_VALUE, LinkError, has_shapes, params_of_json, params_of_query,
)

#: The port when the settings file names none (AutoMap's default too).
DEFAULT_PORT = 47631
#: Who may send when the settings file says nothing: any browser extension (AutoMap).
DEFAULT_ORIGINS = ("chrome-extension://*",)
#: The most a request's line and headers may take, and its body (bytes).
MAX_HEADER = 16 * 1024
MAX_BODY = 8 * 1024 * 1024
#: Connections open at once at most, and how long one may take to send its request (ms).
MAX_CONNECTIONS = 16
READ_TIMEOUT_MS = 10_000
#: The app's name in the answers (AutoMap checks it).
APP = "MIKAGE"

_ORIGIN = re.compile(r"^([a-z][a-z0-9+.-]*)://(\*|(?:\*\.)?[a-z0-9_.-]+|\[[0-9a-f:.]+\])(?::(\d{1,5}))?$")
_REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            411: "Length Required", 413: "Payload Too Large", 415: "Unsupported Media Type", 422: "Unprocessable Entity",
            431: "Request Header Fields Too Large", 500: "Internal Server Error", 501: "Not Implemented", 503: "Service Unavailable"}


@dataclass(frozen=True)
class ReceiverSettings:
    """`shapeReceiver`: true / false, or {enabled, port, origins}."""
    enabled: bool = True
    port: int = DEFAULT_PORT
    #: Origins whose pages / extensions may send (`https://intra.example.jp`, `https://*.example.jp`, `chrome-extension://<id>`, `chrome-extension://*`).
    origins: tuple = DEFAULT_ORIGINS


DEFAULT_RECEIVER = ReceiverSettings()


def receiver_settings_of(value: Any, problems: list[str]) -> ReceiverSettings:
    if isinstance(value, bool):
        return ReceiverSettings(enabled=value)
    if not isinstance(value, dict):
        problems.append("shapeReceiver は true / false か {enabled, port, origins} で指定")
        return DEFAULT_RECEIVER
    enabled, port, origins = True, DEFAULT_PORT, DEFAULT_ORIGINS
    if "enabled" in value:
        if isinstance(value["enabled"], bool):
            enabled = value["enabled"]
        else:
            problems.append("shapeReceiver.enabled は true か false で指定")
    if "port" in value:
        p = value["port"]
        if isinstance(p, int) and not isinstance(p, bool) and 1024 <= p <= 65535:
            port = p
        else:
            problems.append(f"shapeReceiver.port は 1024〜65535 の整数で指定（既定 {DEFAULT_PORT}）")
    if "origins" in value:
        v = value["origins"]
        if not isinstance(v, list):
            problems.append("shapeReceiver.origins は配列で指定")
        else:
            kept = []
            for o in v:
                pattern = origin_pattern(o)
                if pattern is None:
                    problems.append(f"shapeReceiver.origins の「{o}」は送信元（https://host、chrome-extension://ID など）として読めない")
                elif pattern in ("*", "http://*", "https://*"):
                    problems.append(f"shapeReceiver.origins に「{o}」は指定できない（どのサイトからでも図形を受け取ることになる）")
                else:
                    kept.append(pattern)
            origins = tuple(kept)
    return ReceiverSettings(enabled=enabled, port=port, origins=origins)


def origin_pattern(text: Any) -> Optional[str]:
    """An origin pattern as kept (lower case, no `/` at the end), or None when it is none."""
    if not isinstance(text, str):
        return None
    t = text.strip().lower().rstrip("/")
    if t == "*":
        return t
    m = _ORIGIN.match(t)
    if not m or (m.group(3) is not None and not 0 < int(m.group(3)) <= 65535):
        return None
    return t


def origin_allowed(origin: str, patterns: tuple) -> bool:
    """Whether an `Origin` (`https://intra.example.jp`, `chrome-extension://abc…`) is one the patterns allow."""
    m = _ORIGIN.match(origin.strip().lower())
    if not m or m.group(2).startswith("*"):
        return False
    scheme, host, port = m.groups()
    for p in patterns:
        pm = _ORIGIN.match(p)
        if not pm or pm.group(1) != scheme or pm.group(3) != port:
            continue
        ph = pm.group(2)
        if ph == "*" or ph == host or (ph.startswith("*.") and host.endswith(ph[1:])):
            return True
    return False


# --- Requests -------------------------------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Request:
    method: str
    path: str
    query: str
    #: Lower-case names.
    headers: dict[str, str]
    body: bytes = b""


def parse_request(data: bytes) -> Optional[Request]:
    """The request in what came so far; None while it is not all there; HttpError when it is not one we take."""
    end = data.find(b"\r\n\r\n")
    if end < 0:
        if len(data) > MAX_HEADER:
            raise HttpError(431, "ヘッダーが長すぎ")
        return None
    if end > MAX_HEADER:
        raise HttpError(431, "ヘッダーが長すぎ")
    try:
        head = data[:end].decode("ascii")
    except UnicodeDecodeError as error:
        raise HttpError(400, "ヘッダーに ASCII 以外の文字") from error
    lines = head.split("\r\n")
    m = re.fullmatch(r"([A-Z]{1,10}) (\S{1,8000}) HTTP/1\.[01]", lines[0])
    if not m:
        raise HttpError(400, "要求行として読めない")
    method, target = m.groups()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[0] in " \t" or ":" not in line:
            raise HttpError(400, "ヘッダーとして読めない")
        name, value = line.split(":", 1)
        name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name):
            raise HttpError(400, "ヘッダー名として読めない")
        if name in headers and name in ("host", "content-length", "origin", "content-type", "transfer-encoding"):
            raise HttpError(400, f"{name} が重複")
        headers[name] = value.strip()
    if "transfer-encoding" in headers:
        raise HttpError(501, "Transfer-Encoding には非対応（Content-Length で送る）")
    length = 0
    if "content-length" in headers:
        if not re.fullmatch(r"\d{1,10}", headers["content-length"]):
            raise HttpError(400, "Content-Length が数でない")
        length = int(headers["content-length"])
    elif method == "POST":
        raise HttpError(411, "Content-Length なし")
    if length > MAX_BODY:
        raise HttpError(413, f"大きすぎ（{MAX_BODY // (1024 * 1024)} MB まで）")
    body = data[end + 4:]
    if len(body) < length:
        return None
    if not target.startswith("/"):
        raise HttpError(400, "パスは / から")
    path, _, query = target.partition("?")
    return Request(method, path.split("#", 1)[0], query.split("#", 1)[0], headers, bytes(body[:length]))


# --- Who sends --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sender:
    #: Shown in the message bar and kept in the layer ("chrome-extension://…", "このパソコンのプログラム"…).
    text: str
    #: The origin answered with CORS headers ("" for none).
    cors: str = ""


def host_ok(request: Request, port: int) -> bool:
    """The `Host` names this computer at our port (a page of another name that resolves here is refused: DNS rebinding)."""
    host = request.headers.get("host", "").strip().lower()
    return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")


def sender_of(request: Request, settings: ReceiverSettings) -> Sender:
    """Who sends, when they may (HttpError 403 otherwise)."""
    h = request.headers
    origin = h.get("origin")
    if origin is not None:
        if origin.strip().lower() != "null" and origin_allowed(origin, settings.origins):
            return Sender(origin.strip(), cors=origin.strip())
        raise HttpError(403, f"送信元 {origin} からは受け取らない（設定ファイル shapeReceiver.origins）")
    site, mode = h.get("sec-fetch-site"), h.get("sec-fetch-mode")
    if site is None and mode is None:
        return Sender("このパソコンのプログラム")
    if site == "none":
        # Typed in, a bookmark, a link opened from another program, or a browser extension's own request.
        return Sender("ブラウザ")
    if mode == "navigate" and request.method == "GET":
        referer = h.get("referer", "")
        parts = urlsplit(referer) if referer else None
        ref_origin = f"{parts.scheme}://{parts.netloc}" if parts and parts.scheme and parts.netloc else ""
        if ref_origin and origin_allowed(ref_origin, settings.origins):
            return Sender(ref_origin)
        raise HttpError(403, f"ページ {ref_origin or '（送信元不明）'} のリンクからは受け取らない（設定ファイル shapeReceiver.origins）")
    raise HttpError(403, "ページの中からの読み込みでは受け取らない")


# --- Answers ----------------------------------------------------------------------------------------------------

@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        lines = [f"HTTP/1.1 {self.status} {_REASONS.get(self.status, 'Error')}"]
        headers = {
            "Content-Type": self.content_type,
            "Content-Length": str(len(self.body)),
            "Connection": "close",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            **self.headers,
        }
        if self.content_type.startswith("text/html"):
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        if not self.body:
            headers.pop("Content-Type", None)
        lines += [f"{k}: {v}" for k, v in headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + self.body


def json_response(status: int, value: dict, sender: Optional[Sender] = None) -> Response:
    r = Response(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if sender is not None and sender.cors:
        r.headers.update({"Access-Control-Allow-Origin": sender.cors, "Vary": "Origin"})
    return r


def html_response(status: int, title: str, text: str) -> Response:
    page = ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\"><title>{t}</title>"
            "<style>body{{font:15px/1.6 system-ui,'Segoe UI','Meiryo',sans-serif;margin:3em auto;max-width:36em;padding:0 1em;color:#1f2328}}"
            "h1{{font-size:18px}}</style></head><body><h1>{t}</h1><p>{x}</p></body></html>").format(
        t=html.escape(title), x=html.escape(text).replace("\n", "<br>"))
    return Response(status, page.encode("utf-8"), "text/html; charset=utf-8")


#: What the plugin does with the shapes: (parameters, longest value read, sender) → (how many shown, layer name, what could not be read).
Show = Callable[[list, int, Sender], tuple[int, str, list[str]]]


def handle(request: Request, settings: ReceiverSettings, show: Show) -> Response:
    """The answer to a request (`show` called only for a sender allowed with shapes)."""
    wants_page = request.method == "GET" and request.headers.get("sec-fetch-dest") == "document"
    try:
        if not host_ok(request, settings.port):
            raise HttpError(403, "Host が 127.0.0.1 / localhost でない")
        sender = sender_of(request, settings)
    except HttpError as error:
        if wants_page:
            return html_response(error.status, "MIKAGE: 受け取らなかった", error.message)
        return json_response(error.status, {"app": APP, "ok": False, "error": error.message})
    if request.method == "OPTIONS":
        if not sender.cors:
            return json_response(400, {"app": APP, "ok": False, "error": "Origin なし"})
        headers = {"Access-Control-Allow-Origin": sender.cors, "Vary": "Origin", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                   "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"}
        if request.headers.get("access-control-request-private-network", "").lower() == "true":
            headers["Access-Control-Allow-Private-Network"] = "true"
        return Response(204, headers=headers)
    if request.path in ("/", "/status"):
        if request.method != "GET":
            return json_response(405, {"app": APP, "ok": False, "error": "GET で"}, sender)
        return json_response(200, {"app": APP, "ok": True}, sender)
    if request.path != "/show":
        return json_response(404, {"app": APP, "ok": False, "error": "パスは /show か /status"}, sender)
    try:
        if request.method == "GET":
            params = params_of_query(request.query)
            max_value = MAX_VALUE
        elif request.method == "POST":
            params = params_of_body(request)
            max_value = MAX_BODY_VALUE
        else:
            raise HttpError(405, "GET か POST で")
        if not has_shapes(params):
            raise HttpError(400, "図形がない（geom・line・polygon・geojson のどれかを）")
    except LinkError as error:
        return _answer(wants_page, 400, {"app": APP, "ok": False, "error": str(error)}, sender)
    except HttpError as error:
        return _answer(wants_page, error.status, {"app": APP, "ok": False, "error": error.message}, sender)
    try:
        count, layer, errors = show(params, max_value, sender)
    except Exception as error:  # noqa: BLE001 (answered: the sender says what went wrong)
        return _answer(wants_page, 500, {"app": APP, "ok": False, "error": f"図形の表示に失敗: {error}"}, sender)
    value = {"app": APP, "ok": count > 0, "count": count, "layer": layer, "errors": errors}
    if not count:
        value["error"] = "読める図形なし" + (f"（{' / '.join(errors[:5])}）" if errors else "")
    return _answer(wants_page, 200 if count else 422, value, sender)


def _answer(page: bool, status: int, value: dict, sender: Sender) -> Response:
    if not page:
        return json_response(status, value, sender)
    if value.get("ok"):
        skipped = f"\n読めなかったもの: {' / '.join(value['errors'][:10])}" if value.get("errors") else ""
        return html_response(status, "MIKAGE に送りました", f"図形 {value['count']:,} 個をレイヤー「{value['layer']}」に追加しました。"
                                                           f"このタブは閉じてかまいません。{skipped}")
    return html_response(status, "MIKAGE: 図形を表示できなかった", value.get("error", ""))


def params_of_body(request: Request) -> list:
    """A POST's parameters: a JSON object, or a form (`geom=…&label=…`)."""
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        text = request.body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HttpError(400, "本文は UTF-8 で") from error
    if kind == "application/json":
        try:
            value = json.loads(text)
        except (ValueError, RecursionError) as error:
            raise HttpError(400, "JSON として読めない") from error
        return params_of_json(value)
    if kind in ("", "application/x-www-form-urlencoded", "text/plain"):
        return params_of_query(text.strip())
    raise HttpError(415, "本文は application/json か application/x-www-form-urlencoded で")
