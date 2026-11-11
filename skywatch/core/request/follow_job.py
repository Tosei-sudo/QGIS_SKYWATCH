"""
定期照合 (tsudo, 2026-11-11: 「定期的に照合したいので、QGISに依存せず、照合を
実行するPythonツールを入れてほしい。Windowsのタスクスケジューラに入れる」):
the follow-up of the requests handed out (出力履歴 against the image catalog:
撮像済み・未撮像, and on the ledger the raise undone and 期間内1回 完了), run
without QGIS, e.g. every 5 minutes from the Windows Task Scheduler.

It does what 「計算」 and 「照合…」 of the 撮像計画ファイル出力 dock do first
(follow.follow_and_save), for every `requestStores` entry with a history,
and nothing else (no files written, no raise offered: the next 「計算」 does).

    "C:\\Program Files\\QGIS 3.34.12\\bin\\python-qgis-ltr.bat" "<plugin folder>\\scheduled_follow.py" --config "\\\\server\\gis\\config.json"

Plain Python with NumPy (the core's imports need it): QGIS's own Python has
both, QGIS itself is not started. Requests go through urllib: the system's
proxy and certificates (on Windows its certificate store). A service's
sign-in is the settings file's `token`, or a token from the server's
generateToken with a user name and password (Windows' credential manager,
or an environment variable). QGIS's authentication configurations (authcfg)
cannot be used outside QGIS, nor Windows integrated authentication.

One run at a time (a lock the system lets go of when the process ends), a
time limit, a log file a day, the audit log's lines for its edits
(`auditLog`), and an exit code: 0 done (or nothing to do), 1 a ledger could
not be followed up or saved, 2 the settings or the options are wrong.
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date
from typing import Callable, Optional, Sequence
from urllib.parse import urljoin

from .. import audit, edit_stamp, time_zone
from ..catalog import CatalogConfig
from ..config import PluginConfig, parse_config_text, read_config_file
from ..esri import EsriClient, EsriError, UnreachableError, form_body, quote
from ..plan.names import NO_NAMES, SatelliteNames, load_satellite_names
from .follow import follow_and_save, found_text, with_settled_times
from .model import ImagingRequest, day_bounds
from .store import HistoryRow, RequestStoreConfig, history_where, load_history, load_requests

#: The plugin's folder (skywatch/), where a settings file may be shipped (config.json / config.jsonc).
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUNDLED_NAMES = ("config.json", "config.jsonc")
#: Seconds a read may take, and an edit (applyEdits…: a large batch on a busy server takes longer, as in qgis_io/network.py).
READ_TIMEOUT = 60
EDIT_TIMEOUT = 180
_EDITS = re.compile(r"/(applyEdits|addFeatures|updateFeatures|deleteFeatures)/?$", re.IGNORECASE)
USER_AGENT = "SKYWATCH-scheduled-follow"
#: The environment variable the password is read from (with --user).
PASSWORD_VARIABLE = "SKYWATCH_PASSWORD"
#: Minutes a token from generateToken lasts (a run takes a few; asked again each run).
TOKEN_MINUTES = 60
#: Request ids asked in one query of the ledger.
IDS_PER_QUERY = 200
LOG_PREFIX = "follow-"
LOCK_NAME = "follow.lock"

log = logging.getLogger("skywatch.scheduled_follow")


class UsageError(Exception):
    """The options or the settings file are wrong: nothing was tried (exit code 2)."""


# ---------------------------------------------------------------- requests (urllib)

def _address(url: str) -> str:
    """The address without its query (a token may be in it)."""
    return url.split("?", 1)[0]


class _NoPostRedirect(urllib.request.HTTPRedirectHandler):
    """A POST sent elsewhere would arrive as a GET without its form (urllib's way): said instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() == "POST":
            raise EsriError(f"転送された（{_address(req.full_url)} → {_address(newurl)}）。設定ファイルの URL を転送先に直す")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(context: Optional[ssl.SSLContext] = None) -> urllib.request.OpenerDirector:
    handlers: list = [_NoPostRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _open(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: float) -> bytes:
    """The body of the answer; UnreachableError when the server cannot be reached or is down, EsriError when it refuses."""
    address = _address(request.full_url)
    try:
        with opener.open(request, timeout=timeout) as reply:
            return reply.read()
    except urllib.error.HTTPError as error:
        status = error.code
        if status >= 500:
            raise UnreachableError(f"サーバーのエラー（{address}：HTTP {status}）") from None
        hint = ""
        if status == 401:
            schemes = " ".join(error.headers.get_all("WWW-Authenticate") or []) if error.headers else ""
            if re.search(r"negotiate|ntlm", schemes, re.IGNORECASE):
                hint = "。統合 Windows 認証のサービスはこのツールからは使えない（トークンを使う）"
            else:
                hint = "。サインインが要る（設定ファイルの token か --user・--credential）"
        raise EsriError(f"接続不可（{address}：HTTP {status}{hint}）") from None
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise UnreachableError(f"応答なし（タイムアウト）（{address}）") from None
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise EsriError(f"接続不可（{address}：サーバーの証明書を確かめられない。社内の認証局なら --ca-file で指定）") from None
        raise UnreachableError(f"接続不可（{address}：{reason}）") from None
    except (socket.timeout, TimeoutError):
        raise UnreachableError(f"応答なし（タイムアウト）（{address}）") from None
    except (OSError, http.client.HTTPException) as error:
        raise UnreachableError(f"接続不可（{address}：{error or type(error).__name__}）") from None


def url_transport(context: Optional[ssl.SSLContext] = None, timeout: float = READ_TIMEOUT,
                  edit_timeout: float = EDIT_TIMEOUT) -> Callable[[str, dict], bytes]:
    """A `Transport` for EsriClient: form POSTs through urllib (the system's proxy; `context`: the certificates trusted)."""
    opener = _opener(context)

    def send(url: str, form: dict[str, str]) -> bytes:
        request = urllib.request.Request(url, data=form_body(form), method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
        return _open(opener, request, edit_timeout if _EDITS.search(_address(url)) else timeout)

    return send


def get_text(url: str, context: Optional[ssl.SSLContext] = None, timeout: float = READ_TIMEOUT) -> str:
    """A text file at a URL (the settings file), read as UTF-8."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return _open(_opener(context), request, timeout).decode("utf-8-sig", errors="replace")


# ---------------------------------------------------------------- sign-in

def server_root(url: str) -> str:
    """Where a server's REST API is: https://gis.example/arcgis/rest/services/… → https://gis.example/arcgis."""
    m = re.match(r"^(.*?)/rest/services(/|$)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.match(r"^([a-z][a-z0-9+.-]*://[^/]+)", url, re.IGNORECASE)
    return m.group(1) if m else url


def _answer(body: bytes, what: str) -> dict:
    try:
        value = json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise EsriError(f"{what} の応答が JSON 以外") from None
    if not isinstance(value, dict):
        raise EsriError(f"{what} の応答が JSON 以外")
    error = value.get("error")
    if isinstance(error, dict):
        details = [str(d) for d in error.get("details") or [] if d]
        raise EsriError(f"{what} がエラー応答: " + " ".join([str(error.get("message") or ""), *details]).strip())
    return value


class SignIn:
    """
    Tokens of the servers' generateToken for a user name and password, one
    per server (asked the first time a service of it is used, again when the
    server says it is no longer good). The password goes only over https.
    """

    def __init__(self, transport: Callable[[str, dict], bytes], user: str, password: str, minutes: int = TOKEN_MINUTES):
        self.transport, self.user, self.password, self.minutes = transport, user, password, minutes
        self._tokens: dict[str, Optional[str]] = {}

    def token(self, url: str) -> Optional[str]:
        """The token for a service's URL (None: its server does not sign in with tokens)."""
        root = server_root(url)
        if root not in self._tokens:
            self._tokens[root] = self._generate(root)
        return self._tokens[root]

    def renew(self, url: str) -> Optional[str]:
        """A new token (EsriClient's `renew`: the one sent was refused)."""
        self._tokens.pop(server_root(url), None)
        return self.token(url)

    def _generate(self, root: str) -> Optional[str]:
        info = _answer(self.transport(f"{root}/rest/info", {"f": "json"}), f"{root}/rest/info")
        auth = info.get("authInfo") if isinstance(info.get("authInfo"), dict) else {}
        token_url = auth.get("tokenServicesUrl")
        if not isinstance(token_url, str) or not token_url.strip():
            if auth.get("isTokenBasedSecurity") is False:
                return None
            raise EsriError(f"{root} の情報にトークンの発行先（tokenServicesUrl）がない")
        token_url = urljoin(root + "/", token_url.strip())
        if not token_url.lower().startswith("https://"):
            raise EsriError(f"トークンの発行先が https でないためパスワードを送らない（{_address(token_url)}）")
        value = _answer(self.transport(token_url, {"username": self.user, "password": self.password, "client": "requestip",
                                                   "expiration": str(self.minutes), "f": "json"}), "サインイン")
        token = value.get("token")
        if not isinstance(token, str) or not token:
            raise EsriError(f"トークンが発行されなかった（{_address(token_url)}）")
        return token


def password_of(blob: bytes) -> str:
    """
    The password of a credential: UTF-16 (cmdkey, the control panel), or
    UTF-8 as some tools keep it (no zero byte, which UTF-16 of Latin
    letters always has, and good UTF-8).
    """
    if b"\x00" not in blob:
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return blob.decode("utf-16-le", errors="replace")


def read_windows_credential(target: str) -> tuple[str, str]:
    """
    (user name, password) of a generic credential of Windows' credential
    manager (`cmdkey /generic:<target> /user:<name> /pass`, as the account
    the task runs as).
    """
    if os.name != "nt":
        raise UsageError("--credential は Windows でのみ使える（ほかは --user と環境変数 SKYWATCH_PASSWORD）")
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    read = advapi32.CredReadW
    read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    read.restype = wintypes.BOOL
    free = advapi32.CredFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not read(target, 1, 0, ctypes.byref(pointer)):  # 1: CRED_TYPE_GENERIC
        code = ctypes.get_last_error()
        why = "登録がない（タスクを実行するアカウントで cmdkey /generic で登録）" if code == 1168 else f"エラー {code}"
        raise UsageError(f"資格情報「{target}」を読めない（{why}）")
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize) if credential.CredentialBlobSize else b""
        user = credential.UserName or ""
    finally:
        free(pointer)
    if not user or not blob:
        raise UsageError(f"資格情報「{target}」にユーザー名かパスワードがない")
    return user, password_of(blob)


def credentials(args: argparse.Namespace, environ=None) -> Optional[tuple[str, str]]:
    """(user name, password) for generateToken; None: the settings file's tokens only."""
    environ = os.environ if environ is None else environ
    if args.credential:
        return read_windows_credential(args.credential)
    if not args.user:
        return None
    password = environ.get(PASSWORD_VARIABLE) or ""
    if not password:
        raise UsageError(f"--user のパスワードがない（環境変数 {PASSWORD_VARIABLE}、Windows なら --credential）")
    return args.user, password


# ---------------------------------------------------------------- one run at a time

class RunLock:
    """A lock on a file: held while a run goes on, let go of by the system when the process ends (even killed)."""

    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode("ascii"))
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None


# ---------------------------------------------------------------- folders and the log

def default_folder(environ=None) -> str:
    """Where the log, the lock and the audit log's own files are kept (--folder)."""
    environ = os.environ if environ is None else environ
    if os.name == "nt":
        base = environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "SKYWATCH", "scheduled_follow")
    base = environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "skywatch", "scheduled_follow")


def bundled_config(folder: str = PLUGIN_DIR) -> str:
    """The settings file shipped in the plugin's folder ('' when there is none)."""
    for name in BUNDLED_NAMES:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return ""


def start_log(folder: str, keep_days: float, quiet: bool = False) -> list[logging.Handler]:
    """A file a day (follow-YYYYMMDD.log, UTF-8) in `folder`, the old ones forgotten; also to the console unless `quiet`."""
    os.makedirs(folder, exist_ok=True)
    oldest = time.strftime("%Y%m%d", time.localtime(time.time() - keep_days * 86400))
    for name in os.listdir(folder):
        m = re.match(rf"^{LOG_PREFIX}(\d{{8}})\.log$", name)
        if m and m.group(1) < oldest:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    form = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.FileHandler(os.path.join(folder, f"{LOG_PREFIX}{time.strftime('%Y%m%d')}.log"),
                                                           encoding="utf-8")]
    if not quiet and sys.stderr is not None:
        try:
            sys.stderr.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(form)
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return handlers


def stop_log(handlers: Sequence[logging.Handler]) -> None:
    for handler in handlers:
        log.removeHandler(handler)
        handler.close()


# ---------------------------------------------------------------- the audit log

def plugin_version() -> str:
    try:
        with open(os.path.join(PLUGIN_DIR, "metadata.txt"), encoding="utf-8") as f:
            m = re.search(r"^version\s*=\s*(.+)$", f.read(), re.MULTILINE)
    except OSError:
        return ""
    return m.group(1).strip() if m else ""


class RunAudit:
    """
    The audit log (`auditLog`) of a run: its edits between a session's start
    and end, written only when the run changed something (a run every few
    minutes that finds nothing leaves no line), sent to the folder every
    `sendMinutes` at most (else as many files as runs). Kept here in the
    tool's own folder (not `localFolder`, the plugin's on the same PC).
    """

    def __init__(self, settings: "audit.AuditSettings", folder: str):
        self.log = audit.AuditLog(os.path.join(folder, "audit"), replace(settings, local_folder=""))
        audit.set_log(self.log)
        audit.record("session", "start", detail={"tool": "scheduled_follow", "plugin": plugin_version()})

    def finish(self, now: Optional[float] = None) -> None:
        audit.record("session", "end")
        audit.set_log(None)
        items = []
        while True:
            try:
                items.append(self.log.queue.get_nowait())
            except Exception:  # noqa: BLE001 (queue.Empty)
                break
        if any(kind != "session" for _, kind, _, _ in items):
            try:
                self.log.write(items)
            except Exception as error:  # noqa: BLE001 (the log never stops the work)
                log.info(f"注意: 監査ログを書けない（{error}）")
                return
        # Also the lines an earlier run kept here (not due then), though this one changed nothing.
        if self.due(now) and self.log.unsent() > 0:
            result = self.log.send_safely(now)
            if result.error:
                log.info(f"注意: 監査ログを送れない（{result.error}）。次の回に送る")

    def due(self, now: Optional[float] = None) -> bool:
        """Whether the lines kept here are sent now: `sendMinutes` since the last send (or never sent)."""
        s = self.log.settings
        if not s.enabled or not s.folder:
            return False
        now = time.time() if now is None else now
        try:
            last = os.path.getmtime(os.path.join(self.log.folder, audit.STATE))
        except OSError:
            return True
        return now - last >= max(60.0, s.send_minutes * 60)


# ---------------------------------------------------------------- the follow-up

def catalog_of_store(config: PluginConfig, store: RequestStoreConfig) -> Optional[CatalogConfig]:
    """The image catalog a ledger is followed up against (as the dock: its `catalog`, else the first one)."""
    catalogs = config.image_catalogs
    if store.catalog:
        return next((c for c in catalogs if c.label == store.catalog), None)
    return catalogs[0] if catalogs else None


def due_rows(history: Sequence[HistoryRow], now: float) -> list[HistoryRow]:
    """The rows a follow-up asks the catalog about (follow.follow_up's): 採用, 未判定, their day begun."""
    out = []
    for r in history:
        if r.result != "採用" or r.captured != "未判定":
            continue
        try:
            first, _ = day_bounds(date.fromisoformat(r.day))
        except ValueError:
            continue
        if now >= first:
            out.append(r)
    return out


def requests_of(client: EsriClient, store: RequestStoreConfig, token: Optional[str], ids: Sequence[str],
                cancelled: Optional[Callable[[], bool]] = None) -> dict[str, ImagingRequest]:
    """The requests of some ids (not the whole ledger, read every few minutes), by id."""
    out: dict[str, ImagingRequest] = {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), IDS_PER_QUERY):
        part = ids[i:i + IDS_PER_QUERY]
        where = f"{store.fields['id']} IN ({','.join(quote(x) for x in part)})"
        out.update((r.id, r) for r in load_requests(client, store, token, cancelled, where=where))
    return out


def follow_store(client: EsriClient, config: PluginConfig, store: RequestStoreConfig, token_for: Callable[[str, Optional[str]], Optional[str]],
                 names_of: Callable[[], SatelliteNames], now: float, cancelled: Optional[Callable[[], bool]] = None) -> tuple[str, list[str]]:
    """Follows one ledger up and saves what was found: what it says (for the log), and what could not be saved."""
    catalog = catalog_of_store(config, store)
    if catalog is None:
        raise EsriError(f"照合に使う画像カタログ「{store.catalog}」が imageCatalogs にない" if store.catalog else "画像カタログ（imageCatalogs）がない")
    token = token_for(store.url, store.token)
    history = load_history(client, store, token, history_where(store, open_rows=True), cancelled=cancelled)
    due = due_rows(history, now)
    if not due:
        return "照合する行なし", []
    requests = requests_of(client, store, token, [r.request_id for r in due], cancelled)
    rows = with_settled_times(client, store, token, history, requests, cancelled)
    catalog_token = token_for(catalog.url, catalog.token)
    problems: list[str] = []
    found, changed = follow_and_save(client, store, token, catalog, catalog_token, rows, requests, now, names_of(), cancelled, problems)
    if not found:
        text = f"{len(due)} 行を照合、新たに判定できた行なし"
    else:
        text = f"{len(due)} 行を照合、{found_text(found)}" + ("（台帳を更新）" if changed else "")
    return text, problems


def run(args: argparse.Namespace, folder: str) -> int:
    started = time.monotonic()
    deadline = started + args.time_limit * 60

    def cancelled() -> bool:
        return time.monotonic() > deadline

    context = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else None
    source = args.config or bundled_config()
    if not source:
        raise UsageError("設定ファイルを --config で指定（プラグインのフォルダに config.json もない）")
    if re.match(r"^https?://", source, re.IGNORECASE):
        try:
            text = get_text(source, context)
        except EsriError as error:
            log.info(f"失敗: 設定ファイルを読めない（{error}）")
            return 1
        config, problems = parse_config_text(text)
    else:
        if not os.path.isfile(source):
            raise UsageError(f"設定ファイルがない（{source}）")
        config, problems = read_config_file(source)
    if problems:
        log.info(f"注意: 設定ファイルの問題 {len(problems)} 件（{problems[0]}{' ほか' if len(problems) > 1 else ''}）")
    time_zone.set_rules(config.time_zones)
    tracking = config.edit_tracking
    if args.editor:
        tracking = replace(tracking, user=args.editor)
    edit_stamp.set_tracking(tracking)
    labels = {s.label for s in config.request_stores}
    unknown = [label for label in args.store if label not in labels]
    if unknown:
        raise UsageError(f"requestStores にない台帳: {'・'.join(unknown)}（{'・'.join(sorted(labels)) or 'なし'}）")
    stores = [s for s in config.request_stores if s.history and (not args.store or s.label in args.store)]
    if not stores:
        raise UsageError("照合する台帳がない（requestStores に history のある台帳がない）")
    signed = credentials(args)
    transport = url_transport(context)
    sign_in = SignIn(transport, *signed) if signed else None
    client = EsriClient(transport, renew=sign_in.renew if sign_in else None)

    def token_for(url: str, token: Optional[str]) -> Optional[str]:
        return (sign_in.token(url) if sign_in else None) or token

    names: list[SatelliteNames] = []

    def names_of() -> SatelliteNames:
        if not names:
            names.append(NO_NAMES)
            if config.satellite_aliases:
                try:
                    found, failed = load_satellite_names(client, config.satellite_aliases, token_for, cancelled)
                except Exception as error:  # noqa: BLE001 (names only make matches better)
                    found, failed = NO_NAMES, [str(error)]
                names[0] = found
                for p in failed:
                    log.info(f"注意: 衛星名の対応表（{p}）")
        return names[0]

    record = RunAudit(config.audit_log, folder)
    failed = False
    try:
        for store in stores:
            if cancelled():
                log.info(f"失敗: {store.label}: 時間切れ（--time-limit {args.time_limit:g} 分）のため照合しない")
                failed = True
                continue
            if store.authcfg and not store.token and sign_in is None:
                log.info(f"注意: {store.label} は QGIS の認証設定（authcfg）を使う設定。このツールでは使えないため token か --user・--credential が要る")
            try:
                text, problems = follow_store(client, config, store, token_for, names_of, time.time() * 1000, cancelled)
            except UsageError:
                raise
            except EsriError as error:
                late = f"（時間切れ: --time-limit {args.time_limit:g} 分）" if cancelled() else ""
                log.info(f"失敗: {store.label}: {error}{late}")
                failed = True
                continue
            log.info(f"{store.label}: {text}")
            if problems:
                failed = True
                log.info(f"失敗: {store.label}: 保存できなかった行あり（{'、'.join(problems[:3])}）。次の回にもう一度")
    finally:
        record.finish()
    log.info(f"終了（{time.monotonic() - started:.1f} 秒）" if not failed else f"一部失敗で終了（{time.monotonic() - started:.1f} 秒）")
    return 1 if failed else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scheduled_follow.py",
        description="出力履歴を画像カタログと照合し、撮像済み・未撮像と台帳（繰り上げの戻し・完了）を更新する（QGIS なし）。"
                    "Windows のタスクスケジューラなどから定期的に実行する。")
    p.add_argument("--config", default="", help="設定ファイル（パスか http(s) の URL）。省略時はプラグインのフォルダの config.json")
    p.add_argument("--store", action="append", default=[], metavar="ラベル",
                   help="照合する台帳（requestStores の label）。何度でも指定可。省略時は history のある台帳すべて")
    p.add_argument("--user", default="", help=f"generateToken でサインインするユーザー名（パスワードは環境変数 {PASSWORD_VARIABLE}）")
    p.add_argument("--credential", default="", metavar="名前",
                   help="Windows の資格情報マネージャーの汎用資格情報（cmdkey /generic:名前）からユーザー名とパスワードを読む")
    p.add_argument("--editor", default="", metavar="名前", help="更新者（UPDATED_USER・監査ログの user）に書く名前。省略時は実行するアカウント")
    p.add_argument("--folder", default="", help="ログ・ロック・監査ログの控えの置き場所。省略時は %%LOCALAPPDATA%%\\SKYWATCH\\scheduled_follow")
    p.add_argument("--keep-days", type=float, default=30, help="ログを残す日数（既定 30）")
    p.add_argument("--time-limit", type=float, default=15, help="1 回の実行の上限（分、既定 15）。過ぎたら残りの台帳は次の回")
    p.add_argument("--ca-file", default="", help="信頼する認証局の証明書（PEM）。社内の認証局のサーバー用")
    p.add_argument("--quiet", action="store_true", help="画面には出さず、ログファイルにだけ書く")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    if args.time_limit <= 0 or args.keep_days < 1:
        print("--time-limit は 0 より大きく、--keep-days は 1 以上で指定", file=sys.stderr)
        return 2
    folder = os.path.abspath(args.folder or default_folder())
    try:
        handlers = start_log(folder, args.keep_days, args.quiet)
    except OSError as error:
        print(f"ログのフォルダを使えない（{folder}: {error.strerror or error}）", file=sys.stderr)
        return 2
    lock = RunLock(os.path.join(folder, LOCK_NAME))
    try:
        if not lock.acquire():
            log.info("前の回の照合がまだ動いているため、今回は実行しない")
            return 0
        try:
            return run(args, folder)
        except UsageError as error:
            log.info(f"失敗: {error}")
            return 2
        except Exception as error:  # noqa: BLE001 (said in the log, with where it happened)
            log.exception(f"失敗: 思わぬエラー（{type(error).__name__}: {audit.mask(str(error))}）")
            return 1
        finally:
            lock.release()
    finally:
        stop_log(handlers)


if __name__ == "__main__":
    sys.exit(main())
