"""サインイン (`signIn`): the entries, matching a request's host, the portal tokens (iwa) and the Windows exchange."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from mikage.core import sign_in, windows_auth
from mikage.core.config import parse_config
from mikage.core.esri import EsriError
from mikage.core.sign_in import PortalTokens, SignInRule, match, same_person, sign_in_rules_of, under_portal

PORTAL = "https://gis.example.co.jp/portal"


def rules(value):
    problems = []
    return sign_in_rules_of(value, problems), problems


def test_entries_and_what_is_wrong_with_them():
    got, problems = rules([
        {"method": "iwa", "portal": PORTAL + "/sharing/rest", "domains": ["*.gis.example.co.jp", "Server.Example.co.jp"]},
        {"method": "SAML", "portal": "https://map.example.com/portal/home", "clientId": "AbC123", "label": "地図"},
        {"method": "none", "domains": "open.example.org"},
        {"method": "saml", "portal": "https://x.example.com/portal"},
        {"method": "kerberos", "portal": PORTAL},
        {"method": "iwa", "portal": "http://gis.example.co.jp/portal"},
        {"method": "none", "domains": ["bad host", "ok.example.org"]},
        {"method": "none"},
        "iwa",
    ])
    iwa, saml, none, ok = got
    assert iwa == SignInRule("iwa", PORTAL, "", ("gis.example.co.jp", "*.gis.example.co.jp", "server.example.co.jp"), "gis.example.co.jp")
    assert saml.portal == "https://map.example.com/portal" and saml.client_id == "AbC123" and saml.label == "地図"
    assert saml.domains == ("map.example.com",)
    assert none == SignInRule("none", "", "", ("open.example.org",), "open.example.org")
    assert ok.domains == ("ok.example.org",)
    assert problems == [
        "signIn[3] の clientId（ポータルに登録したアプリのクライアント ID）なし",
        "signIn[4] の method は iwa・saml・none のどれか",
        "signIn[5] の portal はポータルの https の URL（https://gis.example.co.jp/portal など）で指定",
        "signIn[6] の domains「bad host」はホスト名（gis.example.co.jp）か *.example.co.jp で指定",
        "signIn[7] は portal か domains が必要",
        "signIn[8] はオブジェクトで指定",
    ]


def test_a_domain_named_twice_keeps_the_first():
    got, problems = rules([{"method": "iwa", "portal": PORTAL}, {"method": "none", "domains": ["gis.example.co.jp", "a.example.org"]}])
    assert [r.domains for r in got] == [("gis.example.co.jp",), ("a.example.org",)]
    assert problems == ["signIn[1] の domains「gis.example.co.jp」は signIn[0] にもある（前のものを使う）"]


def test_the_most_precise_domain_wins():
    got, _ = rules([
        {"method": "iwa", "portal": PORTAL, "domains": ["*.example.co.jp"]},
        {"method": "none", "domains": ["*.open.example.co.jp", "plain.example.co.jp"]},
    ])
    iwa, none = got
    assert match(got, "https://gis.example.co.jp/server/rest/services/A/FeatureServer/0") is iwa
    assert match(got, "https://other.example.co.jp/x") is iwa
    assert match(got, "https://a.open.example.co.jp/x") is none
    assert match(got, "https://plain.example.co.jp:8443/x") is none
    # `*.` is the hosts under the domain, not the domain itself.
    assert match(got, "https://example.co.jp/x") is None
    assert match(got, "https://elsewhere.org/x") is None
    assert match(got, "not a url") is None


def test_the_portals_own_pages():
    rule = rules([{"method": "iwa", "portal": PORTAL}])[0][0]
    assert under_portal(rule, PORTAL + "/sharing/rest/generateToken")
    assert under_portal(rule, "HTTPS://GIS.example.co.jp/Portal/sharing/rest/content/items/x?f=json")
    assert not under_portal(rule, "https://gis.example.co.jp/server/rest/services/A/FeatureServer")
    assert not under_portal(rule, "https://gis.example.co.jp/portalx/sharing")


def test_the_settings_file_reads_it():
    config, problems = parse_config({"signIn": [{"method": "iwa", "portal": PORTAL}]})
    assert problems == [] and config.sign_in[0].method == "iwa"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def portal_answers(clock, log):
    def ask(url, form):
        log.append((url, dict(form)))
        if url.endswith("/generateToken"):
            return json.dumps({"token": f"t{len(log)}", "expires": clock.now + 60 * 60_000}).encode()
        if url.endswith("/community/self"):
            return json.dumps({"username": "tsudo@CORP", "fullName": "須藤", "role": "org_user"}).encode()
        return b'{"error": {"code": 400, "message": "?"}}'
    return ask


def test_portal_tokens_are_asked_once_and_again_before_they_end():
    clock, log = Clock(), []
    tokens = PortalTokens(portal_answers(clock, log), clock)
    rule = SignInRule("iwa", PORTAL, "", ("gis.example.co.jp",))
    assert tokens.token(rule) == "t1"
    assert tokens.token(rule) == "t1"
    url, form = log[0]
    assert url == PORTAL + "/sharing/rest/generateToken"
    assert form == {"request": "getToken", "client": "referer", "referer": "https://gis.example.co.jp", "expiration": "120", "f": "json"}
    clock.now += 56 * 60_000  # 4 minutes left
    assert tokens.token(rule) == "t2"
    assert len(log) == 2


def test_a_refused_token_is_asked_again_once_for_requests_refused_together():
    clock, log = Clock(), []
    tokens = PortalTokens(portal_answers(clock, log), clock)
    rule = SignInRule("iwa", PORTAL, "", ("gis.example.co.jp",))
    tokens.token(rule)
    clock.now += 60_000
    assert tokens.renew(rule) == "t2"
    assert tokens.renew(rule) == "t2"
    clock.now += 31_000
    assert tokens.renew(rule) == "t3"


def test_the_portal_user():
    clock, log = Clock(), []
    tokens = PortalTokens(portal_answers(clock, log), clock)
    rule = SignInRule("iwa", PORTAL, "", ("gis.example.co.jp",))
    user = tokens.user(rule)
    assert (user.username, user.full_name) == ("tsudo@CORP", "須藤")
    assert tokens.user(rule) is user and len(log) == 1
    assert same_person("tsudo@CORP", "tsudo") and same_person("CORP\\Tsudo", "tsudo")
    assert same_person("tsudo@corp.example.co.jp", "CORP\\tsudo")
    assert not same_person("tanaka@CORP", "tsudo")


def test_a_portal_that_refuses_says_so():
    rule = SignInRule("iwa", PORTAL, "", ("gis.example.co.jp",))
    tokens = PortalTokens(lambda url, form: b'{"error": {"code": 498, "message": "Invalid token."}}')
    with pytest.raises(EsriError) as caught:
        tokens.token(rule)
    assert caught.value.code == "E203"


def test_headers_for_the_services_of_an_iwa_portal():
    clock, log = Clock(), []
    sign_in.set_rules(rules([{"method": "iwa", "portal": PORTAL, "domains": ["*.example.co.jp"]}, {"method": "none", "domains": ["a.org"]}])[0])
    sign_in.set_tokens(PortalTokens(portal_answers(clock, log), clock))
    try:
        assert sign_in.headers_for("https://gis.example.co.jp/server/rest/services/A/FeatureServer/0/query") == {
            "X-Esri-Authorization": "Bearer t1", "Referer": "https://gis.example.co.jp"}
        assert sign_in.headers_for("https://maps.example.co.jp/arcgis/rest/services") == {
            "X-Esri-Authorization": "Bearer t1", "Referer": "https://gis.example.co.jp"}
        # The portal's own pages go with the Windows logon itself; other hosts get nothing.
        assert sign_in.headers_for(PORTAL + "/sharing/rest/community/self") == {}
        assert sign_in.headers_for("https://a.org/x") == {}
        assert sign_in.headers_for("https://b.org/x") == {}
        assert sign_in.renew_for("https://gis.example.co.jp/server/rest/services") == "t1"
        assert sign_in.renew_for("https://a.org/x") is None
        sign_in.set_tokens(None)
        with pytest.raises(EsriError):
            sign_in.headers_for("https://gis.example.co.jp/server/rest/services")
    finally:
        sign_in.set_rules([])
        sign_in.set_tokens(None)


# ---------------------------------------------------------------- the Windows exchange


class FakeContext(windows_auth.Context):
    """Stands in for SSPI: tokens `<scheme>-1`, then `<scheme>-2:<challenge>`."""

    made = []

    def __init__(self, scheme, target):
        self.scheme, self.target, self.n = scheme, target, 0
        FakeContext.made.append(self)
        self.closed = False

    def step(self, token):
        self.n += 1
        return f"{self.scheme}-{self.n}".encode() + (b":" + token if token else b"")

    def close(self):
        self.closed = True


def b64(text):
    return base64.b64encode(text.encode() if isinstance(text, str) else text).decode()


class Iis(BaseHTTPRequestHandler):
    """IIS with Windows authentication: NTLM takes two legs on one connection, Kerberos one."""

    protocol_version = "HTTP/1.1"
    offer = ("Negotiate", "NTLM")
    kerberos = False
    seen = []

    def log_message(self, *args):
        pass

    def _answer(self, status, body=b"", challenge=None):
        self.send_response(status)
        if status == 401:
            for scheme in ([challenge] if challenge else self.offer):
                self.send_header("WWW-Authenticate", scheme)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, form):
        auth = self.headers.get("Authorization")
        Iis.seen.append((self.path, auth, id(self.connection)))
        if not auth:
            return self._answer(401)
        scheme, token = auth.split(" ", 1)
        token = base64.b64decode(token).decode()
        if scheme not in self.offer:
            return self._answer(401)
        if token == f"{scheme}-1" and not self.kerberos:
            self.leg = id(self.connection)
            return self._answer(401, challenge=f"{scheme} {b64('challenge')}")
        good = token == f"{scheme}-1" if self.kerberos else (token == f"{scheme}-2:challenge" and getattr(self, "leg", None) == id(self.connection))
        if not good:
            return self._answer(401)
        self._answer(200, json.dumps({"ok": True, "form": form}).encode())

    def do_GET(self):
        self._handle({})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._handle({k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()})


@pytest.fixture
def iis():
    Iis.seen = []
    FakeContext.made = []
    windows_auth._asked.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Iis)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    Iis.offer, Iis.kerberos = ("Negotiate", "NTLM"), False
    windows_auth._asked.clear()


def test_ntlm_takes_two_legs_on_one_connection(iis):
    body = windows_auth.ask(iis + "/portal/sharing/rest/generateToken", {"request": "getToken", "f": "json"}, context=FakeContext)
    assert json.loads(body) == {"ok": True, "form": {"request": "getToken", "f": "json"}}
    assert [(a.split(" ")[0] if a else None) for _p, a, _c in Iis.seen] == [None, "Negotiate", "Negotiate"]
    assert len({c for _p, _a, c in Iis.seen}) == 1
    made = FakeContext.made[0]
    assert (made.scheme, made.target, made.closed) == ("Negotiate", "HTTP/127.0.0.1", True)
    # The next exchange with the host starts signed (no unsigned round trip).
    windows_auth.ask(iis + "/portal/sharing/rest/community/self?f=json", context=FakeContext)
    assert Iis.seen[3][1].startswith("Negotiate ")


def test_kerberos_takes_one_leg_and_ntlm_alone_is_used_when_it_is_all_offered(iis):
    Iis.kerberos = True
    windows_auth.ask(iis + "/a", {}, context=FakeContext)
    assert len(Iis.seen) == 2
    windows_auth._asked.clear()
    Iis.seen, Iis.offer, Iis.kerberos = [], ("NTLM",), False
    windows_auth.ask(iis + "/b", context=FakeContext)
    assert [a.split(" ")[0] for _p, a, _c in Iis.seen[1:]] == ["NTLM", "NTLM"]


def test_a_refused_sign_in_is_e203_and_windows_errors_too(iis):
    class Wrong(FakeContext):
        def step(self, token):
            return b"wrong"

    with pytest.raises(EsriError) as caught:
        windows_auth.ask(iis + "/a", context=Wrong)
    assert caught.value.code == "E203"

    def broken(scheme, target):
        raise windows_auth.SspiError("SEC_E_NO_CREDENTIALS")

    windows_auth._asked.clear()
    with pytest.raises(EsriError) as caught:
        windows_auth.ask(iis + "/a", context=broken)
    assert caught.value.code == "E203" and "SEC_E_NO_CREDENTIALS" in caught.value.detail


def test_only_https_away_from_this_pc():
    with pytest.raises(EsriError):
        windows_auth.ask("http://gis.example.co.jp/portal/sharing/rest/generateToken", {}, context=FakeContext)


def test_a_portal_down_is_not_asked_again_for_a_minute():
    clock, asked = Clock(), []

    def down(url, form):
        asked.append(url)
        raise EsriError(code="E101", detail="down")

    tokens = PortalTokens(down, clock)
    rule = SignInRule("iwa", PORTAL, "", ("gis.example.co.jp",))
    for _ in range(3):
        with pytest.raises(EsriError):
            tokens.token(rule)
    assert len(asked) == 1
    clock.now += 61_000
    with pytest.raises(EsriError):
        tokens.renew(rule)
    assert len(asked) == 2
