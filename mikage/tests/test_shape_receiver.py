"""外部から図形を受け取る (core/shape_receiver.py): settings, reading requests, who may send, the answers."""
import json

import pytest

from mikage.core.config import parse_config
from mikage.core.shape_receiver import (
    DEFAULT_PORT, HttpError, ReceiverSettings, Request, Sender, handle, origin_allowed, parse_request, receiver_settings_of,
)

PORT = DEFAULT_PORT
SETTINGS = ReceiverSettings(origins=("chrome-extension://*", "https://intra.example.jp", "https://*.example.org"))


def test_settings():
    config, problems = parse_config({})
    assert config.shape_receiver == ReceiverSettings() and config.shape_receiver.enabled and problems == []
    assert receiver_settings_of(False, []) == ReceiverSettings(enabled=False)
    problems = []
    s = receiver_settings_of({"port": 80, "origins": ["https://Intra.Example.jp/", "*", "https://*", "ftp:/x", 3, "chrome-extension://abc"],
                              "enabled": "yes"}, problems)
    assert s == ReceiverSettings(enabled=True, port=PORT, origins=("https://intra.example.jp", "chrome-extension://abc"))
    assert problems == [
        "shapeReceiver.enabled は true か false で指定",
        "shapeReceiver.port は 1024〜65535 の整数で指定（既定 47631）",
        "shapeReceiver.origins に「*」は指定できない（どのサイトからでも図形を受け取ることになる）",
        "shapeReceiver.origins に「https://*」は指定できない（どのサイトからでも図形を受け取ることになる）",
        "shapeReceiver.origins の「ftp:/x」は送信元（https://host、chrome-extension://ID など）として読めない",
        "shapeReceiver.origins の「3」は送信元（https://host、chrome-extension://ID など）として読めない",
    ]
    assert receiver_settings_of("on", problems := []) == ReceiverSettings() and problems


def test_origins():
    patterns = SETTINGS.origins
    assert origin_allowed("chrome-extension://abcdefghijklmnop", patterns)
    assert origin_allowed("https://intra.example.jp", patterns) and origin_allowed("https://a.b.example.org", patterns)
    assert not origin_allowed("https://intra.example.jp:8443", patterns)
    assert not origin_allowed("http://intra.example.jp", patterns)
    assert not origin_allowed("https://example.org", patterns)
    assert not origin_allowed("https://evilexample.org", patterns)
    assert not origin_allowed("https://evil.example", patterns)
    assert not origin_allowed("null", patterns)


def raw(method="GET", target="/status", headers=None, body=b""):
    h = {"Host": f"127.0.0.1:{PORT}", **(headers or {})}
    if body:
        h["Content-Length"] = str(len(body))
    return (f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body


def test_reading_requests():
    assert parse_request(b"GET / HTTP/1.1\r\nHost: x") is None
    data = raw("POST", "/show?a=1", {"Content-Type": "application/json"}, b'{"geom": "1,2"}')
    assert parse_request(data[:-3]) is None
    r = parse_request(data)
    assert (r.method, r.path, r.query, r.body) == ("POST", "/show", "a=1", b'{"geom": "1,2"}')
    assert r.headers["content-type"] == "application/json"
    for data, status in ((b"x" * 20000, 431), (b"GET / HTTP/2\r\n\r\n", 400), (raw("POST", "/show"), 411),
                         (raw("POST", "/show", {"Content-Length": "99999999"}), 413), (raw(headers={"Transfer-Encoding": "chunked"}), 501),
                         (b"GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n", 400), (b"GET http://x/ HTTP/1.1\r\n\r\n", 400),
                         (b"GET / HTTP/1.1\r\n folded\r\n\r\n", 400)):
        with pytest.raises(HttpError) as e:
            parse_request(data)
        assert e.value.status == status, data[:40]


class Shown:
    def __init__(self):
        self.calls = []

    def __call__(self, params, max_value, sender):
        self.calls.append((params, max_value, sender))
        return len([k for k, _ in params if k in ("geom", "line", "polygon", "geojson")]), "テスト", []


def answer(data, settings=SETTINGS, show=None):
    show = show or Shown()
    response = handle(parse_request(data), settings, show)
    text = response.to_bytes().decode("utf-8")
    head, _, body = text.partition("\r\n\r\n")
    return response, head, body, show


def test_an_extension_posts_json_and_gets_cors_headers():
    body = json.dumps({"geojson": {"type": "Point", "coordinates": [139.7, 35.6]}, "label": "東京", "name": "AutoMap"}).encode()
    origin = "chrome-extension://abcdefghijklmnop"
    response, head, text, show = answer(raw("POST", "/show", {"Origin": origin, "Content-Type": "application/json", "Sec-Fetch-Site": "none",
                                                              "Sec-Fetch-Mode": "cors"}, body))
    assert response.status == 200 and json.loads(text) == {"app": "MIKAGE", "ok": True, "count": 1, "layer": "テスト", "errors": []}
    assert f"Access-Control-Allow-Origin: {origin}" in head and "Connection: close" in head and "Cache-Control: no-store" in head
    (params, max_value, sender), = show.calls
    assert params[0][0] == "geojson" and isinstance(params[0][1], dict) and max_value > 200_000 and sender == Sender(origin, origin)
    # A form body too (no preflight needed for a program).
    response, _, _, show = answer(raw("POST", "/show", {"Content-Type": "application/x-www-form-urlencoded"}, b"geom=35,139&label=a"))
    assert response.status == 200 and show.calls[0][0] == [("geom", "35,139"), ("label", "a")]
    assert show.calls[0][2] == Sender("このパソコンのプログラム")


def test_preflight_for_allowed_origins_only():
    response, head, _, _ = answer(raw("OPTIONS", "/show", {"Origin": "https://intra.example.jp", "Access-Control-Request-Method": "POST",
                                                          "Access-Control-Request-Private-Network": "true"}))
    assert response.status == 204
    for line in ("Access-Control-Allow-Origin: https://intra.example.jp", "Access-Control-Allow-Methods: GET, POST, OPTIONS",
                 "Access-Control-Allow-Headers: Content-Type", "Access-Control-Allow-Private-Network: true"):
        assert line in head
    response, head, _, _ = answer(raw("OPTIONS", "/show", {"Origin": "https://evil.example"}))
    assert response.status == 403 and "Access-Control" not in head


def test_pages_cannot_send_but_people_and_programs_can():
    query = "/show?geom=35,139"
    # A page's image / fetch / frame / form, a foreign Host (DNS rebinding), a null origin: refused, nothing shown.
    for headers in ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
                    {"Origin": "https://evil.example", "Sec-Fetch-Mode": "cors"},
                    {"Origin": "null"},
                    {"Host": f"evil.example:{PORT}"},
                    {"Host": "127.0.0.1:1234"},
                    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Referer": "https://evil.example/a"},
                    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}):
        response, _, _, show = answer(raw("GET", query, headers))
        assert response.status == 403 and show.calls == [], headers
    # Typed in or opened from another program: a short page.
    response, head, text, show = answer(raw("GET", query, {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}))
    assert response.status == 200 and "text/html" in head and "default-src 'none'" in head
    assert "図形 1 個をレイヤー「テスト」に追加" in text and show.calls[0][2] == Sender("ブラウザ")
    # A link on a page of an origin allowed.
    response, _, _, show = answer(raw("GET", query, {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                                                     "Referer": "https://intra.example.jp/report/1"}))
    assert response.status == 200 and show.calls[0][2] == Sender("https://intra.example.jp")
    # A refusal as a page says why.
    response, _, text, _ = answer(raw("GET", query, {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}))
    assert response.status == 403 and "受け取らない" in text and "<" not in text.split("<p>")[1].split("</p>")[0]


def test_status_errors_and_nothing_to_show():
    response, _, text, _ = answer(raw("GET", "/status"))
    assert response.status == 200 and json.loads(text) == {"app": "MIKAGE", "ok": True}
    assert answer(raw("GET", "/other"))[0].status == 404
    assert answer(raw("POST", "/status", body=b"x"))[0].status == 405
    response, _, text, show = answer(raw("GET", "/show?label=a"))
    assert response.status == 400 and show.calls == []
    assert answer(raw("POST", "/show", {"Content-Type": "application/xml"}, b"<x/>"))[0].status == 415
    assert answer(raw("POST", "/show", {"Content-Type": "application/json"}, b"{"))[0].status == 400
    assert answer(raw("POST", "/show", {"Content-Type": "application/json"}, b'{"geom": 1}'))[0].status == 400

    def nothing(params, max_value, sender):
        return 0, "x", ["geom=abc: 座標として読めない: abc"]

    response, _, text, _ = answer(raw("GET", "/show?geom=abc"), show=nothing)
    assert response.status == 422 and json.loads(text)["error"] == "読める図形なし（geom=abc: 座標として読めない: abc）"

    def broken(params, max_value, sender):
        raise RuntimeError("壊れた")

    response, _, text, _ = answer(raw("GET", "/show?geom=1,2"), show=broken)
    assert response.status == 500 and "壊れた" in json.loads(text)["error"]
