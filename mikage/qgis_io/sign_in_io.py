"""
サインイン (`signIn`, core/sign_in.py) in QGIS: set when the settings file
is read, before any other part of the plugin asks a server.

- `iwa`: the portal tokens are asked with the Windows logon
  (core/windows_auth.py) and kept in memory. The plugin's own requests get
  them in qgis_io/network.py. QGIS's own requests to the entry's domains
  (the layers the plugin adds to the map, read by QGIS's ArcGIS providers)
  get them from an API-header authentication configuration of QGIS made for
  the portal (「MIKAGE: <label>（統合 Windows 認証）」, its id kept in the
  plugin's settings), its token header written again with each new token and
  taken out when the plugin is unloaded; tokens are asked again before they
  end while QGIS runs. (QgsNetworkAccessManager.setRequestPreprocessor is
  not used: on QGIS 3.34 with Python 3.12 it crashes QGIS as soon as it is
  called.) A token is never in a layer's source or a project.
- `saml`: an OAuth2 authentication configuration of QGIS made (and kept up
  to date) for the portal, named 「MIKAGE: <label>」; its id is kept in the
  plugin's settings. QGIS opens the browser for the portal's sign-in the
  first time, keeps the token (and its refresh) and adds it to the requests.
- Who is signed in (`community/self`) is asked for the `iwa` entries once
  the settings are read, for 「設定…」 and 「接続と表の点検」.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
import time
from typing import Callable, Optional

from qgis.core import QgsApplication, QgsAuthMethodConfig
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..core import edit_stamp, sign_in, windows_auth
from ..core.esri import EsriError
from ..core.sign_in import PortalTokens, PortalUser, SignInRule
from .qsettings import pref, set_pref
from .tasks import run_task

#: Seconds a portal token request may take.
TOKEN_TIMEOUT = 15
#: Where QGIS's OAuth2 sign-in comes back to (the portal's app needs `http://127.0.0.1:7070/` as a redirect URI).
REDIRECT_PORT = 7070
OAUTH2_METHOD = "OAuth2"
HEADER_METHOD = "APIHeader"
#: A token is asked again this long before it ends (ms), for the layers on the map (within RENEW_BEFORE_MS of core/sign_in.py).
RENEW_AHEAD_MS = 4 * 60 * 1000
#: After a token could not be asked, asked again after this (ms).
RETRY_MS = 2 * 60 * 1000
#: How often a token asked by a request (on its own thread) is looked for, to write it into the portal's authcfg (ms).
WATCH_MS = 5 * 1000


def oauth2_config(rule: SignInRule) -> str:
    """The OAuth2 configuration (QgsAuthOAuth2Config's JSON) of a `saml` portal: authorization code, no secret, kept token."""
    rest = f"{rule.portal}/sharing/rest/oauth2"
    value = {
        "accessMethod": 0,  # Header
        "apiKey": "",
        "clientId": rule.client_id,
        "clientSecret": "",
        "configType": 1,  # Custom
        # ArcGIS's own header (QGIS older than 3.34 sends Authorization: Bearer, which ArcGIS Enterprise 11 also reads).
        "customHeader": "X-Esri-Authorization",
        "description": "MIKAGE の設定ファイルの signIn から作成（変えると次の起動で戻ります）",
        "grantFlow": 0,  # Authorization Code
        "id": "",
        "name": f"MIKAGE: {rule.label}",
        "password": "",
        "persistToken": True,
        "queryPairs": {},
        "redirectPort": REDIRECT_PORT,
        "redirectUrl": "",
        "refreshTokenUrl": f"{rest}/token",
        "requestTimeout": 30,
        "requestUrl": f"{rest}/authorize",
        "scope": "",
        "tokenUrl": f"{rest}/token",
        "username": "",
        "version": 1,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _pref_key(rule: SignInRule, kind: str = "oauth2") -> str:
    return f"signIn/{kind}/" + rule.portal.replace("://", "/").replace("/", "_")


def _auth_ready(method: str) -> str:
    """Why QGIS cannot keep an authentication configuration of `method` ("" when it can; main thread: may ask the master password)."""
    manager = QgsApplication.authManager()
    if manager.isDisabled():
        return "QGIS の認証の仕組みが使えない"
    if method not in manager.authMethodsKeys():
        return f"QGIS に認証方式 {method} がない（QGIS の入れ直しを管理者に）"
    if not manager.masterPasswordIsSet() and not manager.setMasterPassword(True):
        return "QGIS のマスターパスワードが必要（入れると使える）"
    return ""


def _keep_config(key: str, method: str, name: str, uri: str, values: dict[str, str],
                 same: Callable[[QgsAuthMethodConfig], bool]) -> tuple[Optional[str], str]:
    """
    The authcfg kept under the plugin's setting `key`, made or brought up to
    date (`same`: whether the one kept needs nothing); (None, why) when it
    cannot be.
    """
    why = _auth_ready(method)
    if why:
        return None, why
    manager = QgsApplication.authManager()
    kept = pref(key)
    config = QgsAuthMethodConfig()
    if kept and kept in manager.configIds() and manager.loadAuthenticationConfig(kept, config, True):
        if config.method() == method and config.name() == name and config.uri() == uri and same(config):
            return kept, ""
        config.setMethod(method)
        config.setName(name)
        config.setUri(uri)
        config.setConfigMap(values)
        if manager.updateAuthenticationConfig(config):
            return kept, ""
        return None, "認証設定を更新できない"
    config = QgsAuthMethodConfig(method)
    config.setName(name)
    config.setUri(uri)
    config.setConfigMap(values)
    stored = manager.storeAuthenticationConfig(config)
    ok = stored[0] if isinstance(stored, tuple) else stored
    if not ok or not config.id():
        return None, "認証設定を作れない"
    set_pref(key, config.id())
    return config.id(), ""


def make_saml_config(rule: SignInRule) -> tuple[Optional[str], str]:
    """
    The authcfg of a `saml` portal, made or brought up to date (main thread:
    QGIS may ask for its master password); (None, why) when it cannot be.
    (Left as it is while it says the same: updating it would drop the sign-in QGIS keeps for it.)
    """
    wanted = oauth2_config(rule)
    return _keep_config(_pref_key(rule), OAUTH2_METHOD, f"MIKAGE: {rule.label}", rule.portal, {"oauth2config": wanted},
                        lambda c: c.config("oauth2config", "") == wanted)


def token_headers(rule: SignInRule, token: Optional[str]) -> dict[str, str]:
    """The API headers of an `iwa` portal's authcfg: its token (none before one is had) and the referer it is bound to."""
    headers = {"Referer": sign_in.referer_of(rule)}
    if token:
        headers["X-Esri-Authorization"] = f"Bearer {token}"
    return headers


def make_header_config(rule: SignInRule, token: Optional[str] = None) -> tuple[Optional[str], str]:
    """
    The API-header authcfg of an `iwa` portal (QGIS's own requests to its
    domains), made or written with `token` (None: the token taken out);
    (None, why) when it cannot be (main thread).
    """
    values = token_headers(rule, token)
    return _keep_config(_pref_key(rule, "header"), HEADER_METHOD, f"MIKAGE: {rule.label}（統合 Windows 認証）", rule.portal, values,
                        lambda c: dict(c.configMap()) == values)


@dataclass(frozen=True)
class SignInState:
    """What a screen says of one entry."""
    rule: SignInRule
    #: Who the portal says is signed in (None: not asked, or could not be).
    user: Optional[PortalUser] = None
    #: Why it does not work ("" when it does, or it was not asked yet).
    problem: str = ""
    #: Being asked.
    asking: bool = False
    #: What works only in part (the layers on the map get no token) ("" when nothing).
    note: str = ""

    def text(self) -> str:
        r = self.rule
        where = r.domains[0] + (f" ほか {len(r.domains) - 1} 件" if len(r.domains) > 1 else "")
        line = f"{where}: {r.shown}"
        if self.asking:
            return line + "（確認中…）"
        if self.problem:
            return line + f"（{self.problem}）"
        if self.user is not None:
            name = self.user.username + (f"（{self.user.full_name}）" if self.user.full_name else "")
            line += f" ・ サインイン中: {name}"
            me = edit_stamp.user_name()
            if me and not sign_in.same_person(self.user.username, me):
                line += f"（注意: Windows の利用者名 {me} と違う）"
        if self.note:
            line += f"（注意: {self.note}）"
        return line


class SignInKeeper(QObject):
    """The settings file's `signIn`, set for the requests when it is read (main thread)."""

    #: What the screens say of the entries changed.
    changed = pyqtSignal()

    def __init__(self, context):
        super().__init__(context)
        self.context = context
        self.states: dict[str, SignInState] = {}
        #: iwa portal → its API-header authcfg (the token for QGIS's own requests)
        self._headers: dict[str, str] = {}
        self._rules: dict[str, SignInRule] = {}
        self._generation = 0
        self._renew = QTimer(self)
        self._renew.setSingleShot(True)
        self._renew.timeout.connect(self.check)
        #: iwa portal → the token last written into its authcfg
        self._written: dict[str, str] = {}
        # A token asked by any request (on its thread) is written from here, on the main thread: no signal is emitted
        # from the requests' threads (a queued emit holding the GIL can deadlock with Qt's locks: qgis_io/network.py).
        self._watch = QTimer(self)
        self._watch.setInterval(WATCH_MS)
        self._watch.timeout.connect(self._write_tokens)
        context.configChanged.connect(self._config_changed)

    def _config_changed(self) -> None:
        self._generation += 1
        self._renew.stop()
        rules = list(self.context.config.sign_in)
        sign_in.set_rules(rules)
        iwa = [r for r in rules if r.method == "iwa"]
        usable = bool(iwa) and windows_auth.available()
        tokens = PortalTokens(lambda url, form: windows_auth.ask(url, form, timeout=TOKEN_TIMEOUT)) if usable else None
        sign_in.set_tokens(tokens)
        self._written = {}
        states = {r.portal or r.domains[0]: SignInState(r) for r in rules}
        if iwa and not usable:
            for r in iwa:
                states[r.portal] = SignInState(r, problem="Windows 以外では使えない")
        authcfgs = {}
        before, self._rules = self._rules, ({r.portal: r for r in iwa} if usable else {})
        for portal in set(self._headers) - set(self._rules):
            self._take_out(portal, before.get(portal))
        for r in rules:
            if r.method == "saml":
                authcfg, why = make_saml_config(r)
                if authcfg:
                    authcfgs[r.portal] = authcfg
                else:
                    states[r.portal] = SignInState(r, problem=why)
            elif r.portal in self._rules:
                authcfg, why = make_header_config(r)
                if authcfg:
                    authcfgs[r.portal] = self._headers[r.portal] = authcfg
                else:
                    states[r.portal] = SignInState(r, note=f"地図に足したレイヤーにはトークンが付かない: {why}")
        sign_in.set_authcfgs(authcfgs)
        self.states = states
        self.changed.emit()
        if self._headers:
            self._watch.start()
        else:
            self._watch.stop()
        if usable:
            # Once every other part has taken the new settings: the token first, so the layers read at start have it.
            generation = self._generation
            QTimer.singleShot(0, lambda: self.check() if generation == self._generation else None)

    def _write_tokens(self) -> None:
        """The `iwa` portals' new tokens (asked by any request) into their authcfgs (QGIS's own requests), and asked again before they end."""
        tokens = sign_in.tokens()
        if tokens is None:
            return
        wrote = False
        for portal in list(self._headers):
            rule = self._rules.get(portal)
            token = tokens.cached(rule) if rule is not None else None
            if token and token != self._written.get(portal):
                authcfg, _why = make_header_config(rule, token)
                if authcfg:
                    self._written[portal] = token
                    wrote = True
        if wrote:
            self._plan_renewal()

    def _plan_renewal(self, after_ms: Optional[float] = None) -> None:
        """The next token asked a little before the first kept ends (or after `after_ms`)."""
        tokens = sign_in.tokens()
        if tokens is None or not self._rules:
            return
        if after_ms is None:
            ends = [e for e in (tokens.ends(r) for r in self._rules.values()) if e is not None]
            if not ends:
                return
            after_ms = min(ends) - RENEW_AHEAD_MS - time.time() * 1000
        self._renew.start(int(max(60_000, min(after_ms, 24 * 3600 * 1000))))

    def _take_out(self, portal: str, rule: Optional[SignInRule]) -> None:
        """An `iwa` portal's authcfg without its token (the plugin unloaded, or the portal no longer in signIn)."""
        authcfg = self._headers.pop(portal, None)
        manager = QgsApplication.authManager()
        if authcfg is None or rule is None or not manager.masterPasswordIsSet():
            return
        config = QgsAuthMethodConfig()
        if manager.loadAuthenticationConfig(authcfg, config, True):
            config.setConfigMap(token_headers(rule, None))
            manager.updateAuthenticationConfig(config)

    def check(self) -> bool:
        """Asks the `iwa` portals for a token and who is signed in, in a task; False when there is nothing to ask."""
        tokens = sign_in.tokens()
        rules = [s.rule for s in self.states.values() if s.rule.method == "iwa" and not s.problem]
        if tokens is None or not rules:
            return False
        generation = self._generation
        notes = {s.rule.portal: s.note for s in self.states.values()}
        for r in rules:
            self.states[r.portal] = SignInState(r, asking=True, note=notes.get(r.portal, ""))
        self.changed.emit()

        def work(task):
            out = {}
            for r in rules:
                try:
                    tokens.token(r)
                    out[r.portal] = SignInState(r, user=tokens.user(r, again=True), note=notes.get(r.portal, ""))
                except EsriError as error:
                    said = f"{error}: {error.detail}" if error.detail else str(error)
                    out[r.portal] = SignInState(r, problem=said, note=notes.get(r.portal, ""))
            return out

        def done(found: dict) -> None:
            if generation == self._generation:
                self.states.update(found)
                self.changed.emit()
                self._write_tokens()
                self._plan_renewal(RETRY_MS if any(s.problem for s in found.values()) else None)

        def failed(message: str) -> None:
            if generation == self._generation:
                for r in rules:
                    self.states[r.portal] = SignInState(r, problem=message, note=notes.get(r.portal, ""))
                self.changed.emit()
                self._plan_renewal(RETRY_MS)

        run_task("サインインの確認", work, done, failed)
        return True

    def lines(self) -> list[str]:
        """One line per entry, for the screens."""
        return [s.text() for s in self.states.values()]

    def close(self) -> None:
        """The plugin is unloaded: nothing of `signIn` stays in QGIS's requests (the iwa authcfgs without their token)."""
        self._generation += 1
        self._renew.stop()
        self._watch.stop()
        for portal in list(self._headers):
            self._take_out(portal, self._rules.get(portal))
        self._written = {}
        self._rules = {}
        sign_in.set_rules([])
        sign_in.set_tokens(None)
        sign_in.set_authcfgs({})
