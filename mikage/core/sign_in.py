"""
サインイン (`signIn`): how the plugin signs in to the servers, chosen per
domain in the settings file (tsudo 2026-11-26: 「認証設定のコンフィグで、ポータル
サーバー（各通信はドメインで判定）ごとに、IWA、SAML、無しを選べるように」;
the settings file's `token` keys were dropped at the same time).

    "signIn": [
      { "method": "iwa",  "portal": "https://gis.example.co.jp/portal", "domains": ["*.gis.example.co.jp"] },
      { "method": "saml", "portal": "https://map.example.com/portal", "clientId": "AbC123" },
      { "method": "none", "domains": ["open.example.org"] }
    ]

Every request is matched by its URL's host: a domain is a host
(`gis.example.co.jp`) or every host under one (`*.example.co.jp`); the most
precise match wins (a host before `*.`, a longer `*.` before a shorter), and
an entry's portal host is one of its domains without being written. A host
that no entry names is not signed in to by the settings file (the person's
own QGIS authentication configuration chosen in 「設定…」 still applies).

- `iwa` 統合 Windows 認証 (the portal behind IIS's Windows authentication,
  web-tier): the plugin asks the portal's generateToken with the Windows
  logon (Kerberos / NTLM, core/windows_auth.py) and sends that portal token
  to every service of the entry's domains (federated ArcGIS Server, hosted
  services); the portal's own pages (`…/portal/sharing/rest/…`) are asked
  with the Windows logon itself. Tokens are asked again a few minutes before
  they end and when a server refuses one (498, 499). The plugin's own
  requests take them from memory; QGIS's own (the layers on the map) from
  an API-header authentication configuration kept up to date with the
  token (qgis_io/sign_in_io.py).
- `saml` (SAML or OIDC behind the portal, portal-tier): a QGIS OAuth2
  authentication configuration made from `portal` and `clientId` (the app
  registered in the portal), qgis_io/sign_in_io.py.
- `none`: nothing is added.

The rules are module state (like time_zone's): set when the settings file
is read, read by the requests on any thread.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlsplit

from .esri import EsriError
from .util import is_record

#: The methods, and how the screens name them.
METHODS = {"iwa": "統合 Windows 認証（IWA）", "saml": "SAML（OAuth2）", "none": "サインインなし"}
#: Minutes a portal token is asked for (the portal may give less: it says when it ends).
TOKEN_MINUTES = 120
#: A token this close to its end (ms) is asked for again before it is sent.
RENEW_BEFORE_MS = 5 * 60 * 1000
#: A token asked this recently (ms) is not asked again when a server refuses it (the requests refused together).
RECENT_MS = 30_000
#: After a token could not be asked, requests within this (ms) fail at once rather than ask again (a portal down
#: does not hold every request, nor QGIS's main thread for a layer it reads there, for its whole timeout).
FAILED_MS = 60_000

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_HOST = re.compile(r"^(\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$")


@dataclass(frozen=True)
class SignInRule:
    """One entry of `signIn`."""
    method: str
    #: The portal's root (`https://gis.example.co.jp/portal`, without /sharing/rest); "" for none (`none` only).
    portal: str = ""
    #: The id of the app registered in the portal (`saml`).
    client_id: str = ""
    #: Hosts (`gis.example.co.jp`) and host suffixes (`*.example.co.jp`), lower case; the portal's host first.
    domains: tuple = ()
    #: The name the screens show (the portal's host when none is written).
    label: str = ""

    @property
    def shown(self) -> str:
        return METHODS.get(self.method, self.method)


def portal_root(url: str) -> Optional[str]:
    """A portal's root from what is written (https, or http on this PC); None when it cannot be one."""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return None
    if not u.scheme or not u.hostname or u.username or u.password or u.query or u.fragment:
        return None
    if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in _LOCAL_HOSTS)):
        return None
    root = f"{u.scheme}://{u.netloc.lower()}{u.path or ''}".rstrip("/")
    return re.sub(r"/(home|sharing(/rest)?)$", "", root, flags=re.IGNORECASE)


def host_of(url: str) -> str:
    """The lower-case host of a URL ("" when it has none)."""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def sign_in_rule_of(value: Any, problems: list[str], index: int) -> Optional[SignInRule]:
    """One `signIn` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"signIn[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    method = value.get("method")
    method = method.strip().lower() if isinstance(method, str) else ""
    if method not in METHODS:
        problems.append(f"{at} の method は {'・'.join(METHODS)} のどれか")
        return None
    portal = ""
    raw = value.get("portal")
    if raw is not None or method != "none":
        portal = portal_root(raw) if isinstance(raw, str) else None
        if not portal:
            problems.append(f"{at} の portal はポータルの https の URL（https://gis.example.co.jp/portal など）で指定")
            return None
    client_id = value.get("clientId")
    if method == "saml":
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id.strip()):
            problems.append(f"{at} の clientId（ポータルに登録したアプリのクライアント ID）なし")
            return None
        client_id = client_id.strip()
    else:
        client_id = ""
    domains: list[str] = [host_of(portal)] if portal else []
    listed = value.get("domains", [])
    if isinstance(listed, str):
        listed = [listed]
    if not isinstance(listed, list):
        problems.append(f"{at} の domains はホスト名の配列で指定")
        listed = []
    for d in listed:
        name = d.strip().lower().rstrip(".") if isinstance(d, str) else ""
        if not _HOST.match(name):
            problems.append(f"{at} の domains「{d}」はホスト名（gis.example.co.jp）か *.example.co.jp で指定")
            continue
        if name not in domains:
            domains.append(name)
    if not domains:
        problems.append(f"{at} は portal か domains が必要")
        return None
    label = value.get("label")
    label = label.strip() if isinstance(label, str) and label.strip() else domains[0]
    return SignInRule(method, portal, client_id, tuple(domains), label)


def sign_in_rules_of(value: Any, problems: list[str]) -> list[SignInRule]:
    """`signIn`: its entries, a domain named twice kept for the first."""
    if is_record(value):
        value = [value]
    if not isinstance(value, list):
        problems.append("signIn は配列で指定")
        return []
    rules: list[SignInRule] = []
    seen: dict[str, int] = {}
    for i, v in enumerate(value):
        rule = sign_in_rule_of(v, problems, i)
        if rule is None:
            continue
        kept = []
        for d in rule.domains:
            if d in seen:
                problems.append(f"signIn[{i}] の domains「{d}」は signIn[{seen[d]}] にもある（前のものを使う）")
            else:
                seen[d] = i
                kept.append(d)
        if kept:
            rules.append(SignInRule(rule.method, rule.portal, rule.client_id, tuple(kept), rule.label))
    return rules


def _precision(domain: str, host: str) -> int:
    """How precisely `domain` names `host` (0: not at all)."""
    if domain.startswith("*."):
        return len(domain) if host.endswith(domain[1:]) and host != domain[2:] else 0
    return 10_000 if host == domain else 0


def match(rules: Sequence[SignInRule], url: str) -> Optional[SignInRule]:
    """The entry whose domain names the URL's host most precisely; None when none does."""
    host = host_of(url)
    if not host:
        return None
    best, best_at = None, 0
    for rule in rules:
        for d in rule.domains:
            p = _precision(d, host)
            if p > best_at:
                best, best_at = rule, p
    return best


def under_portal(rule: SignInRule, url: str) -> bool:
    """Whether the URL is one of the portal's own pages (`…/portal/sharing/rest/…`), not a service of its servers."""
    if not rule.portal:
        return False
    base = rule.portal.lower()
    clean = url.split("?", 1)[0].split("#", 1)[0].lower()
    return clean == base or clean.startswith(base + "/")


_rules: list[SignInRule] = []


def set_rules(rules: Sequence[SignInRule]) -> None:
    """Sets the settings file's `signIn` (every request after this is matched against it)."""
    global _rules
    _rules = list(rules)


def rules() -> list[SignInRule]:
    return list(_rules)


def rule_for(url: Optional[str]) -> Optional[SignInRule]:
    """The `signIn` entry of a request's URL, or None."""
    return match(_rules, url) if url else None


#: The QGIS authentication configuration made for each `saml` portal (its OAuth2) and `iwa` portal (an API header holding
#: its token, for QGIS's own requests): portal → authcfg id, qgis_io/sign_in_io.py.
_authcfgs: dict[str, str] = {}


def set_authcfgs(mapping: dict[str, str]) -> None:
    global _authcfgs
    _authcfgs = dict(mapping)


def authcfg_for(rule: Optional[SignInRule]) -> Optional[str]:
    """
    The authentication configuration a request of the entry goes with (a
    layer's source, QGIS's own requests): a `saml` portal's OAuth2, an `iwa`
    portal's API header with its token (the plugin's own requests get the
    token's headers instead: qgis_io/network.py); None until it is made,
    and for `none`. The person's own choice never applies to them.
    """
    return _authcfgs.get(rule.portal) if rule is not None and rule.method in ("saml", "iwa") else None


# ---------------------------------------------------------------- portal tokens (iwa)

#: Asks a portal page with the Windows logon: (url, form) → the body (raises EsriError when it cannot).
WindowsAsk = Callable[[str, dict], bytes]


@dataclass(frozen=True)
class PortalUser:
    """Who the portal says is signed in (`…/sharing/rest/community/self`)."""
    username: str
    full_name: str = ""
    role: str = ""


def _json(body: bytes, what: str) -> dict:
    try:
        value = json.loads(body.decode("utf-8-sig") if isinstance(body, (bytes, bytearray)) else body)
    except (ValueError, UnicodeDecodeError):
        raise EsriError(code="E105", detail=f"{what}：JSON 以外の応答") from None
    if not isinstance(value, dict):
        raise EsriError(code="E105", detail=f"{what}：JSON のオブジェクト以外の応答")
    error = value.get("error")
    if isinstance(error, dict):
        said = f"{what}：{error.get('code')} " + " ".join([str(error.get("message") or ""), *[str(d) for d in error.get("details") or [] if d]])
        raise EsriError(code="E203" if error.get("code") in (401, 498, 499) else "E104", detail=said.strip())
    return value


def referer_of(rule: SignInRule) -> str:
    """The referer the portal's tokens are bound to (and sent with them): the portal's own origin."""
    u = urlsplit(rule.portal)
    return f"{u.scheme}://{u.netloc}"


class PortalTokens:
    """
    The `iwa` entries' portal tokens: asked from the portal's generateToken
    with the Windows logon the first time a request needs one, kept in
    memory, asked again when they are about to end or a server refused one.
    One at a time per portal (the requests waiting take the one asked).
    """

    def __init__(self, ask: WindowsAsk, clock: Callable[[], float] = lambda: time.time() * 1000, minutes: int = TOKEN_MINUTES):
        self.ask, self.clock, self.minutes = ask, clock, minutes
        #: portal → (token, the ms it ends)
        self._tokens: dict[str, tuple[str, float]] = {}
        #: portal → when its token was asked (ms)
        self._asked: dict[str, float] = {}
        #: portal → (when the last ask failed (ms), the error)
        self._failed: dict[str, tuple[float, EsriError]] = {}
        self._users: dict[str, PortalUser] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        #: Told of each new token (the rule; on the thread that asked it).
        self.listener: Optional[Callable[[SignInRule], None]] = None

    def _lock_of(self, portal: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(portal, threading.Lock())

    def cached(self, rule: SignInRule) -> Optional[str]:
        """The token kept for the entry's portal while it is still good, without asking (None: none kept)."""
        kept = self._tokens.get(rule.portal)
        if kept and kept[1] - self.clock() > RENEW_BEFORE_MS:
            return kept[0]
        return None

    def ends(self, rule: SignInRule) -> Optional[float]:
        """When the token kept for the entry's portal ends (ms), None when none is kept."""
        kept = self._tokens.get(rule.portal)
        return kept[1] if kept else None

    def token(self, rule: SignInRule) -> str:
        """The portal token for the entry (asked when none is kept or it is about to end); raises EsriError."""
        token = self.cached(rule)
        if token:
            return token
        with self._lock_of(rule.portal):
            token = self.cached(rule)  # Another request may have just asked.
            if token:
                return token
            return self._generate(rule)

    def renew(self, rule: SignInRule) -> str:
        """
        A new token, the one sent having been refused; one asked in the last
        RECENT_MS is taken as the new one (requests refused together ask once).
        """
        with self._lock_of(rule.portal):
            kept = self._tokens.get(rule.portal)
            if kept and self.clock() - self._asked.get(rule.portal, 0.0) < RECENT_MS:
                return kept[0]
            self._tokens.pop(rule.portal, None)
            return self._generate(rule)

    def forget(self) -> None:
        """Every token and user name kept is dropped (the settings file was read again)."""
        with self._lock:
            self._tokens.clear()
            self._asked.clear()
            self._failed.clear()
            self._users.clear()

    def _generate(self, rule: SignInRule) -> str:
        failed = self._failed.get(rule.portal)
        if failed and self.clock() - failed[0] < FAILED_MS:
            error = failed[1]
            raise type(error)(error.message, code=error.code, detail=error.detail)
        try:
            token, ends = self._ask_token(rule)
        except EsriError as error:
            self._failed[rule.portal] = (self.clock(), error)
            raise
        self._failed.pop(rule.portal, None)
        now = self.clock()
        self._tokens[rule.portal] = (token, ends)
        self._asked[rule.portal] = now
        if self.listener is not None:
            self.listener(rule)
        return token

    def _ask_token(self, rule: SignInRule) -> tuple[str, float]:
        url = f"{rule.portal}/sharing/rest/generateToken"
        value = _json(self.ask(url, {"request": "getToken", "client": "referer", "referer": referer_of(rule),
                                     "expiration": str(self.minutes), "f": "json"}), "ポータルのトークン")
        token = value.get("token")
        if not isinstance(token, str) or not token:
            raise EsriError(code="E203", detail=f"{rule.portal}：トークンが発行されなかった")
        expires = value.get("expires")
        now = self.clock()
        return token, (float(expires) if isinstance(expires, (int, float)) and expires > now else now + self.minutes * 60_000)

    def user(self, rule: SignInRule, again: bool = False) -> PortalUser:
        """Who the portal says the Windows logon is (asked once); raises EsriError."""
        kept = None if again else self._users.get(rule.portal)
        if kept is not None:
            return kept
        value = _json(self.ask(f"{rule.portal}/sharing/rest/community/self", {"f": "json"}), "ポータルの利用者")
        name = value.get("username")
        if not isinstance(name, str) or not name:
            raise EsriError(code="E203", detail=f"{rule.portal}：サインインした利用者が分からない")
        user = PortalUser(name, str(value.get("fullName") or ""), str(value.get("role") or ""))
        self._users[rule.portal] = user
        return user


def same_person(portal_name: str, user_name: str) -> bool:
    """
    Whether a portal user name is the Windows user name (`tsudo@CORP`,
    `CORP\\tsudo`, `tsudo@corp.example.co.jp` are `tsudo`): the screens tell
    when they differ (someone else's PC, or a shared account).
    """
    name = portal_name.strip().lower()
    name = name.rsplit("\\", 1)[-1].split("@", 1)[0]
    return bool(name) and name == user_name.strip().lower().rsplit("\\", 1)[-1]


#: The tokens of this process (qgis_io/sign_in_io.py and the 定期照合 tool set one); None: no `iwa` entry can be used.
_tokens: Optional[PortalTokens] = None


def set_tokens(tokens: Optional[PortalTokens]) -> None:
    global _tokens
    _tokens = tokens


def tokens() -> Optional[PortalTokens]:
    return _tokens


def headers_for(url: str, rule: Optional[SignInRule] = None) -> dict[str, str]:
    """
    The headers a request to a service of an `iwa` entry gets: its portal
    token (`X-Esri-Authorization`, ArcGIS's own header, so a token never goes
    into a URL) and the referer the token is bound to. {} for any other URL.
    Raises EsriError when the token cannot be had.
    """
    rule = rule or rule_for(url)
    if rule is None or rule.method != "iwa" or under_portal(rule, url):
        return {}
    if _tokens is None:
        raise EsriError(code="E204", detail=f"{rule.label}：統合 Windows 認証はこの PC では使えない")
    return {"X-Esri-Authorization": f"Bearer {_tokens.token(rule)}", "Referer": referer_of(rule)}


def renew_for(url: str) -> Optional[str]:
    """A new token for a URL of an `iwa` entry whose token was refused (EsriClient's `renew`); None for other URLs."""
    rule = rule_for(url)
    if rule is None or rule.method != "iwa" or _tokens is None:
        return None
    try:
        return _tokens.renew(rule)
    except EsriError:
        return None
