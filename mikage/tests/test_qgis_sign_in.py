"""
サインイン (`signIn`) in QGIS (qgis_io/sign_in_io.py, network.py,
settings.auth_for): an `iwa` portal's token on the plugin's requests and on
QGIS's own (its API-header authentication configuration, the layers' authcfg)
to its domains only, its portal pages asked with the Windows logon, a `saml`
portal's authentication configuration, the person's own choice for the other
hosts, no token left after unload.
"""
import http.server
import json
import threading

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsNetworkAccessManager, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal  # noqa: E402
from qgis.PyQt.QtNetwork import QNetworkRequest  # noqa: E402

from mikage.core import sign_in, windows_auth  # noqa: E402
from mikage.core.config import parse_config  # noqa: E402
from mikage.core.esri import EsriClient, EsriError  # noqa: E402
from mikage.qgis_io import settings, sign_in_io  # noqa: E402
from mikage.qgis_io.network import qgis_transport  # noqa: E402
from mikage.qgis_io.tasks import run_task  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402

LAYER = {"objectIdField": "OBJECTID", "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]}


class Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def _answer(self):
        size = int(self.headers.get("content-length", 0) or 0)
        self.rfile.read(size)
        Handler.seen.append((self.path, self.headers.get("X-Esri-Authorization"), self.headers.get("Referer")))
        answer = json.dumps(LAYER).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.fixture()
def port():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    Handler.seen = []
    yield httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture(autouse=True)
def clean():
    manager = QgsApplication.authManager()
    if not manager.masterPasswordIsSet():
        manager.setMasterPassword("test", True)
    QgsSettings().remove("mikage")
    yield
    QgsSettings().remove("mikage")
    sign_in.set_rules([])
    sign_in.set_tokens(None)
    sign_in.set_authcfgs({})


class Context(QObject):
    """What SignInKeeper reads of the plugin's context."""
    configChanged = pyqtSignal()

    def __init__(self, value):
        super().__init__()
        self.config, self.problems = parse_config(value)


class Windows:
    """The Windows logon's exchange with the portal (core/windows_auth.ask), as the portal answers it."""

    def __init__(self):
        self.asked: list[str] = []
        self.tokens = iter(["T1", "T2", "T3"])

    def ask(self, url, form=None, body=None, content_type=None, timeout=None, ssl_context=None):
        self.asked.append(url)
        if url.endswith("/generateToken"):
            return json.dumps({"token": next(self.tokens), "expires": 4_102_444_800_000}).encode()
        if url.endswith("/community/self"):
            return json.dumps({"username": "tsudo@CORP", "fullName": "須藤"}).encode()
        return json.dumps({"portalName": "社内ポータル"}).encode()


def _in_task(work):
    out = {}
    run_task("test", lambda task: work(), done=lambda r: out.setdefault("ok", r), failed=lambda m: out.setdefault("error", m))
    assert wait_for(lambda: out)
    return out


def _qgis_get(url: str) -> None:
    """A request QGIS itself makes with the authcfg the plugin gives a layer of the URL (as its ArcGIS providers do)."""
    request = QNetworkRequest(QUrl(url))
    authcfg = settings.auth_for(url)
    if authcfg:
        assert QgsApplication.authManager().updateNetworkRequest(request, authcfg)[0]
    reply = QgsNetworkAccessManager.instance().get(request)
    assert wait_for(lambda: reply.isFinished())
    reply.deleteLater()


def _headers_of(authcfg: str) -> dict:
    config = QgsAuthMethodConfig()
    assert QgsApplication.authManager().loadAuthenticationConfig(authcfg, config, True)
    assert config.method() == "APIHeader"
    return dict(config.configMap())


def test_an_iwa_portals_token_goes_to_its_domains_only_and_nothing_stays_after_unload(port, monkeypatch):
    windows = Windows()
    monkeypatch.setattr(windows_auth, "available", lambda: True)
    monkeypatch.setattr(windows_auth, "ask", windows.ask)
    monkeypatch.setenv("USERNAME", "tsudo")
    portal = f"http://127.0.0.1:{port}/portal"
    ours = f"http://127.0.0.1:{port}/arcgis/rest/services/S/FeatureServer/0"
    other = f"http://localhost:{port}/arcgis/rest/services/S/FeatureServer/0"
    context = Context({"signIn": [{"method": "iwa", "portal": portal, "label": "社内ポータル"}]})
    assert context.problems == []
    keeper = sign_in_io.SignInKeeper(context)
    context.configChanged.emit()
    # Asked once the settings are read: the token, and who is signed in (for 「設定…」 and the check).
    assert wait_for(lambda: "サインイン中" in "".join(keeper.lines()))
    assert keeper.lines() == ["127.0.0.1: 統合 Windows 認証（IWA） ・ サインイン中: tsudo@CORP（須藤）"]
    assert windows.asked == [f"{portal}/sharing/rest/generateToken", f"{portal}/sharing/rest/community/self"]

    # The plugin's requests: the token in ArcGIS's header (never in the form), the referer it is bound to.
    client = EsriClient(qgis_transport(settings.auth_for), renew=sign_in.renew_for)
    assert _in_task(lambda: (client.ask(ours), client.ask(other))) == {"ok": (LAYER, LAYER)}
    assert Handler.seen == [
        ("/arcgis/rest/services/S/FeatureServer/0", "Bearer T1", f"http://127.0.0.1:{port}"),
        ("/arcgis/rest/services/S/FeatureServer/0", None, None),
    ]
    # The portal's own pages: with the Windows logon, not through QGIS.
    assert _in_task(lambda: client.ask(f"{portal}/sharing/rest/portals/self")) == {"ok": {"portalName": "社内ポータル"}}
    assert windows.asked[-1] == f"{portal}/sharing/rest/portals/self" and len(Handler.seen) == 2

    # QGIS's own requests (a layer added to the map) get the same token, from the portal's API-header authcfg.
    authcfg = settings.auth_for(ours)
    assert authcfg and settings.auth_for(other) is None
    assert _headers_of(authcfg) == {"X-Esri-Authorization": "Bearer T1", "Referer": f"http://127.0.0.1:{port}"}
    _qgis_get(ours + "/query?where=1%3D1&f=json")
    _qgis_get(other + "?f=json")
    assert Handler.seen[2:] == [
        ("/arcgis/rest/services/S/FeatureServer/0/query?where=1%3D1&f=json", "Bearer T1", f"http://127.0.0.1:{port}"),
        ("/arcgis/rest/services/S/FeatureServer/0?f=json", None, None),
    ]

    # 「確認し直す」: a new token.
    sign_in.tokens().forget()
    assert keeper.check()
    assert wait_for(lambda: "サインイン中" in "".join(keeper.lines()))
    assert _in_task(lambda: client.ask(ours)) == {"ok": LAYER} and Handler.seen[-1][1] == "Bearer T2"
    assert wait_for(lambda: _headers_of(authcfg).get("X-Esri-Authorization") == "Bearer T2")
    # The next token is asked a little before this one ends (the layers on the map keep theirs).
    assert keeper._renew.isActive()

    # Unloaded: no rule, no token; the authcfg (kept for the layers of saved projects) without its token.
    keeper.close()
    assert sign_in.rules() == [] and sign_in.tokens() is None and not keeper._renew.isActive()
    assert _headers_of(authcfg) == {"Referer": f"http://127.0.0.1:{port}"}
    # Started again: the same authcfg (a saved project's layers keep finding it).
    keeper = sign_in_io.SignInKeeper(context)
    context.configChanged.emit()
    assert sign_in.authcfg_for(sign_in.rule_for(ours)) == authcfg
    assert wait_for(lambda: _headers_of(authcfg).get("X-Esri-Authorization") == "Bearer T3")
    keeper.close()


def test_iwa_off_windows_says_so_and_its_domains_requests_fail_with_e204(port, monkeypatch):
    monkeypatch.setattr(windows_auth, "available", lambda: False)
    context = Context({"signIn": [{"method": "iwa", "portal": f"http://127.0.0.1:{port}/portal"}]})
    keeper = sign_in_io.SignInKeeper(context)
    context.configChanged.emit()
    assert keeper.lines() == ["127.0.0.1: 統合 Windows 認証（IWA）（Windows 以外では使えない）"]
    assert not keeper.check()
    client = EsriClient(qgis_transport(settings.auth_for))
    out = _in_task(lambda: client.ask(f"http://127.0.0.1:{port}/arcgis/rest/services/S/FeatureServer/0"))
    assert "error" in out and "E204" in out["error"] and Handler.seen == []
    keeper.close()


def test_a_hosts_sign_in_wins_over_the_persons_choice_which_stays_for_the_others():
    context = Context({"signIn": [{"method": "none", "domains": ["*.open.example.org"]},
                                  {"method": "iwa", "portal": "https://gis.example.co.jp/portal"}]})
    settings.set_auth_map({"https://a.open.example.org/arcgis": "abc1234", "https://elsewhere.example.com/arcgis": "def5678",
                           "https://gis.example.co.jp/server": "ghi9012"})
    keeper = sign_in_io.SignInKeeper(context)
    context.configChanged.emit()
    assert settings.auth_for("https://a.open.example.org/arcgis/rest/services/X/FeatureServer/0") is None
    assert settings.auth_for("https://gis.example.co.jp/server/rest/services/X/FeatureServer/0") is None
    assert settings.auth_for("https://elsewhere.example.com/arcgis/rest/services/X/FeatureServer/0") == "def5678"
    keeper.close()
    # signIn gone: the person's choices apply again.
    assert settings.auth_for("https://gis.example.co.jp/server/rest/services/X/FeatureServer/0") == "ghi9012"


def test_a_saml_portal_gets_one_oauth2_configuration_kept_up_to_date():
    rule = sign_in.SignInRule("saml", "https://map.example.com/portal", "AbC123", ("map.example.com",), "地図ポータル")
    manager = QgsApplication.authManager()
    if sign_in_io.OAUTH2_METHOD not in manager.authMethodsKeys():
        # (This QGIS has no OAuth2 method: the reason is told, nothing is made.)
        assert sign_in_io.make_saml_config(rule) == (None, "QGIS に認証方式 OAuth2 がない（QGIS の入れ直しを管理者に）")
        context = Context({"signIn": [{"method": "saml", "portal": rule.portal, "clientId": "AbC123", "label": "地図ポータル"}]})
        keeper = sign_in_io.SignInKeeper(context)
        context.configChanged.emit()
        assert keeper.lines() == ["map.example.com: SAML（OAuth2）（QGIS に認証方式 OAuth2 がない（QGIS の入れ直しを管理者に））"]
        assert sign_in.authcfg_for(sign_in.rule_for("https://map.example.com/server/rest/services")) is None
        keeper.close()
        return
    if not manager.masterPasswordIsSet():
        manager.setMasterPassword("test", True)
    made, why = sign_in_io.make_saml_config(rule)
    assert made and why == ""
    again, _ = sign_in_io.make_saml_config(rule)
    assert again == made
    changed, _ = sign_in_io.make_saml_config(sign_in.SignInRule("saml", rule.portal, "Other9", rule.domains, rule.label))
    assert changed == made
    from qgis.core import QgsAuthMethodConfig

    config = QgsAuthMethodConfig()
    assert manager.loadAuthenticationConfig(made, config, True)
    assert config.method() == "OAuth2" and config.name() == "MIKAGE: 地図ポータル" and '"clientId": "Other9"' in config.config("oauth2config")


def test_the_portals_refusal_is_an_e203_and_the_next_requests_do_not_wait_for_it(port, monkeypatch):
    def refused(url, form=None, **_kw):
        raise EsriError(code="E203", detail="ポータルのトークン：401")

    monkeypatch.setattr(windows_auth, "available", lambda: True)
    monkeypatch.setattr(windows_auth, "ask", refused)
    context = Context({"signIn": [{"method": "iwa", "portal": f"http://127.0.0.1:{port}/portal"}]})
    keeper = sign_in_io.SignInKeeper(context)
    context.configChanged.emit()
    assert wait_for(lambda: "401" in "".join(keeper.lines()))
    client = EsriClient(qgis_transport(settings.auth_for))
    out = _in_task(lambda: client.ask(f"http://127.0.0.1:{port}/arcgis/rest/services/S/FeatureServer/0"))
    assert "E203" in out["error"] and Handler.seen == []
    # QGIS's own request is still made (without a token): the server says what it needs.
    _qgis_get(f"http://127.0.0.1:{port}/arcgis/rest/services/S/FeatureServer/0?f=json")
    assert Handler.seen == [("/arcgis/rest/services/S/FeatureServer/0?f=json", None, f"http://127.0.0.1:{port}")]
    # Asked again a little later (the portal may be back).
    assert keeper._renew.isActive() and keeper._renew.remainingTime() > 60_000
    keeper.close()
