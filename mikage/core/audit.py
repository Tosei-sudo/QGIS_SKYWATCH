"""
監査ログ (tsudo, 2026-11-11: 「査証機能を強化したいので、ログ出力と、自動送信
(コンフィグ指定のフォルダに)」, 「今の軽快な操作感に影響を全く与えないように」):
who did what and when, one line per operation, in a file of the day on this
computer, copied from time to time to the settings file's folder (a shared
folder that whoever audits reads).

What is recorded (KINDS):

- session: the plugin started / stopped (its version, QGIS's)
- config: the settings file read (where from, its SHA-256, how many problems)
- edit: every change sent to an Esri layer or table (applyEdits,
  addFeatures, updateFeatures, deleteFeatures…): the layer, add / update /
  delete, the object ids, how many went through, the error; with `values`
  the attributes written. Saves of ArcGIS layers QGIS edits itself (counts).
- offline: work kept to send later (送信待ち), and work thrown away
- open: an image opened from the catalog (its image id and files)
- export: a file written (its path, size and SHA-256)
- copy: text put on the clipboard (通報文, 分析の結果…: how much, its SHA-256)
- error (2026-11-11, tsudo 「エラーログって取れる？」): what went wrong — the
  plugin's warnings and errors in QGIS's log (with their traceback), tasks
  that failed, Python errors raised in the plugin's code, warnings and errors
  in the message bar, the settings file's problems, files that could not be
  saved. The same error again within ERROR_REPEAT seconds of its line is
  counted, not written: one line after (detail `repeated`: how many more,
  at the time of the last) tells them.

Never a token or a password: URLs lose their query and user, `token=`-like
text is masked, the settings file is known by its hash only.

No weight on the screen: `record` only puts a tuple in a queue (no file, no
JSON, no hash on the caller's thread). A writer thread turns them into lines
and appends them to the day's file; a sender thread copies what is new to
the folder every `sendMinutes` (the first a minute after the start, and the
last when the plugin stops, if the folder answers within a second). Neither
calls Qt. A folder that cannot be reached keeps the lines here, sent once
it answers.

Each line carries its session (one start of the plugin), its number in it
(`seq`) and the SHA-256 of the session's line before it (`prev`): a line
taken out or changed breaks the chain (`verify`: `python -m
mikage.core.audit <folder>`, tools/audit_check.py). What was sent is never
sent again nor overwritten: each send is a new file (opened with "x"), so a
folder where people may add files but not change them keeps the log as sent.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from . import edit_stamp
from .util import encodable

#: What can be recorded, with its name on screen.
KINDS: dict[str, str] = {
    "session": "起動・終了",
    "config": "設定ファイルの読み込み",
    "edit": "Esri への書き込み",
    "offline": "送信待ち・破棄",
    "open": "画像を開く",
    "export": "ファイル出力",
    "copy": "クリップボードへコピー",
    "error": "エラー",
}
FORMATS = ("jsonl", "csv")
#: A line's items, in this order (the CSV's columns).
COLUMNS = ("time", "user", "computer", "session", "seq", "kind", "action", "target", "result", "count", "ids", "detail", "error", "prev")
ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "utf-8-sig": "utf-8-sig", "utf-8-bom": "utf-8-sig",
             "cp932": "cp932", "shift_jis": "cp932", "sjis": "cp932", "shift-jis": "cp932"}
#: The names of the files sent (and of the day's files here: audit-YYYYMMDD.jsonl).
PREFIX = "mikage-audit"
_LOCAL = re.compile(r"^audit-(\d{8})\.(jsonl|csv)$")
STATE = "sent.json"
LOCK = "send.lock"
#: A send lock older than this (s) was left by a QGIS that stopped while sending.
STALE_LOCK = 300
#: The first send after the start (s): after QGIS's own start.
FIRST_SEND = 60
#: How long stopping the plugin waits for the last lines to be written and sent (s).
EXIT_WAIT = 1.0
#: Lines kept in memory while the day's file cannot be written (the oldest are lost after).
MAX_UNWRITTEN = 50_000
#: Attribute values recorded per request at most (`values`).
MAX_VALUES = 1000
#: An edit's features longer than this (characters) are not read back (json.loads holds Python's lock all along):
#: their count comes from the answer, their values are left out.
MAX_PARSE = 2_000_000
#: The same error (action, target, message) again within this many seconds is counted, not written.
ERROR_REPEAT = 60.0
#: A traceback is cut to its last this many characters (where it went wrong), an error's message to its first.
MAX_TRACE = 8000
MAX_MESSAGE = 4000
#: The folder's placeholders.
PLACEHOLDERS = ("user", "computer", "yyyy", "mm", "dd")

_EDIT = re.compile(r"/(applyEdits|addFeatures|updateFeatures|deleteFeatures|calculate|append)/?$", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")
_SECRET = re.compile(r"(?i)\b(token|access_token|refresh_token|password|passwd|pwd|secret|api[_-]?key|apikey)=([^&\s\"',;]+)")
_USERINFO = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@\s]*@", re.IGNORECASE)
#: A user (and password) in a URL anywhere in a text (an error message), and an Authorization header's value.
_USERINFO_ANY = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/@\s\"'<>]+@", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_OID = ("objectid", "oid", "fid", "objectid_1")
_UNSAFE = re.compile(r"[\\/:*?\"<>|\s]+")


@dataclass(frozen=True)
class AuditSettings:
    """The settings file's `auditLog`."""
    enabled: bool = True
    #: Where the lines are sent ("" : kept here only). May hold {user} {computer} {yyyy} {mm} {dd}.
    folder: str = ""
    #: Where the day's files are kept here ("": the profile's mikage/audit).
    local_folder: str = ""
    format: str = "jsonl"
    #: "" : utf-8 for jsonl, utf-8-sig (Excel) for csv.
    encoding: str = ""
    send_minutes: float = 10
    keep_days: float = 90
    events: tuple = tuple(KINDS)
    #: The attributes of the features added and updated are recorded too.
    values: bool = False

    @property
    def file_encoding(self) -> str:
        return self.encoding or ("utf-8-sig" if self.format == "csv" else "utf-8")


DEFAULT_AUDIT = AuditSettings()


def audit_log_of(value: Any, problems: list[str]) -> AuditSettings:
    """
    `auditLog`: false (nothing recorded), a folder (sent there), or
    `{ "folder", "localFolder", "format", "encoding", "sendMinutes",
    "keepDays", "events", "values", "enabled" }` (each optional).
    """
    if value is False:
        return AuditSettings(enabled=False)
    if value is None or value is True:
        return DEFAULT_AUDIT
    if isinstance(value, str):
        return AuditSettings(folder=value.strip())
    if not isinstance(value, dict):
        problems.append("auditLog は false・送信先フォルダ（文字列）・オブジェクトのどれかで指定")
        return DEFAULT_AUDIT
    out: dict[str, Any] = {}
    if "enabled" in value:
        if isinstance(value["enabled"], bool):
            out["enabled"] = value["enabled"]
        else:
            problems.append("auditLog.enabled は true か false で指定")
    for key, attr in (("folder", "folder"), ("localFolder", "local_folder")):
        v = value.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            out[attr] = v.strip()
        else:
            problems.append(f"auditLog.{key} はフォルダのパス（文字列）で指定")
    folder = out.get("folder", "")
    for name in re.findall(r"\{([^{}]*)\}", folder):
        if name not in PLACEHOLDERS:
            problems.append(f"auditLog.folder の {{{name}}} は使えない（{'・'.join('{' + p + '}' for p in PLACEHOLDERS)}）")
    if "format" in value and value["format"] is not None:
        f = str(value["format"]).strip().lower()
        if f in FORMATS:
            out["format"] = f
        else:
            problems.append("auditLog.format は jsonl か csv で指定")
    if "encoding" in value and value["encoding"] not in (None, ""):
        e = ENCODINGS.get(str(value["encoding"]).strip().lower())
        if e:
            out["encoding"] = e
        else:
            problems.append("auditLog.encoding は utf-8・utf-8-sig・cp932 のどれかで指定")
    for key, attr, low in (("sendMinutes", "send_minutes", 1), ("keepDays", "keep_days", 1)):
        v = value.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= low:
            out[attr] = float(v)
        else:
            problems.append(f"auditLog.{key} は {low} 以上の数で指定")
    if "events" in value and value["events"] is not None:
        events = value["events"]
        if isinstance(events, list) and all(isinstance(e, str) for e in events):
            unknown = [e for e in events if e not in KINDS]
            if unknown:
                problems.append(f"auditLog.events に不明な種類「{'・'.join(unknown)}」（{'・'.join(KINDS)}）")
            out["events"] = tuple(e for e in KINDS if e in events)
        else:
            problems.append("auditLog.events は種類の名前の配列で指定")
    if "values" in value:
        if isinstance(value["values"], bool):
            out["values"] = value["values"]
        else:
            problems.append("auditLog.values は true か false で指定")
    return AuditSettings(**out)


# ---------------------------------------------------------------- recording (any thread)

_log: Optional["AuditLog"] = None


def set_log(log: Optional["AuditLog"]) -> None:
    """The log `record` writes to (None: nothing recorded)."""
    global _log
    _log = log


def current() -> Optional["AuditLog"]:
    return _log


def record(kind: str, action: str, target: str = "", *, result: str = "ok", count: Optional[int] = None,
           ids: Optional[Iterable[Any]] = None, detail: Optional[dict] = None, error: str = "",
           file: Optional[str] = None, text: Optional[str] = None) -> None:
    """
    Records an operation (from any thread; costs a queue put): `file`, a
    file just written (its size and SHA-256 are added by the writer), `text`,
    text handed on (its length and SHA-256).
    """
    log = _log
    if log is not None:
        log.put(kind, action, {"target": target, "result": result, "count": count, "ids": ids, "detail": detail,
                               "error": error, "file": file, "text": text})


def error(action: str, target: str = "", message: str = "", *, detail: Optional[dict] = None, trace: Optional[str] = None,
          markup: bool = False) -> None:
    """
    Records something that went wrong (any thread; a queue put): `trace`,
    its traceback (else one in `message` is found by the writer); `markup`,
    the message is a message bar's HTML.
    """
    log = _log
    if log is not None:
        log.put("error", action, {"target": target, "result": "error", "error": message, "detail": detail, "trace": trace,
                                  "html": markup})


TRACEBACK = "Traceback (most recent call last)"


def split_trace(message: str) -> tuple[str, Optional[str]]:
    """
    A logged message holding a Python traceback: what is written before it
    (else the traceback's last line, the error) and the traceback; else the
    message alone.
    """
    at = message.find(TRACEBACK)
    if at < 0:
        return message, None
    head = message[:at].strip()
    if not head:
        lines = [line for line in message[at:].splitlines() if line.strip()]
        head = lines[-1].strip() if lines else ""
    return head, message[at:]


def plain_text(text: str) -> str:
    """A message bar's text without its HTML (links, bold): what was read on the screen."""
    if "<" not in text and "&" not in text:
        return text
    return " ".join(html.unescape(_TAG.sub(" ", text)).split())


def is_edit(url: str) -> bool:
    return bool(_EDIT.search(url.split("?", 1)[0]))


def esri_request(url: str, params: dict, answer: Optional[dict], error: str = "", layer: str = "") -> None:
    """An edit request to Esri and its answer (or error): read by the writer into add / update / delete lines."""
    log = _log
    if log is not None:
        log.put("edit", "", {"url": url, "params": params, "answer": answer, "error": error, "layer": layer})


# ---------------------------------------------------------------- lines

def clean_url(url: str) -> str:
    """A URL without its query, fragment and user (`https://u:p@host`)."""
    text = str(url or "")
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE) or text.startswith("/vsicurl/"):
        text = _USERINFO.sub(r"\1", text.split("#", 1)[0].split("?", 1)[0])
    return mask(text)


def mask(text: str) -> str:
    """`token=…`, `password=…` and the like masked (and a URL's user, an Authorization header's value)."""
    if "=" in text:
        text = _SECRET.sub(lambda m: f"{m.group(1)}=…", text)
    if "@" in text:
        text = _USERINFO_ANY.sub(r"\1", text)
    if "earer" in text or "asic" in text:
        text = _BEARER.sub(lambda m: f"{m.group(1)} …", text)
    return text


def _clean(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return mask(value)
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v, depth + 1) for k, v in value.items() if str(k).lower() not in ("token", "password")}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v, depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_digest(path: str) -> tuple[int, str]:
    """The size and SHA-256 of a file."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict,)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value else ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return re.sub(r"[\r\n]+", " ", str(value))


def line_of(rec: dict[str, Any], fmt: str) -> str:
    """A record as one line (no newline in it) of `fmt`."""
    if fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow([_cell(rec.get(c)) for c in COLUMNS])
        return buf.getvalue()
    obj = {c: rec[c] for c in COLUMNS if c in rec and rec[c] not in (None, "", [], {})}
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def header_of(fmt: str) -> str:
    return ",".join(COLUMNS) if fmt == "csv" else ""


def _object_id(attributes: Any) -> Any:
    if not isinstance(attributes, dict):
        return None
    for k, v in attributes.items():
        if str(k).lower() in _OID:
            return v
    return None


def _features(text: Any) -> Optional[list]:
    """The features of an edit's form value; None when it is too long to read (MAX_PARSE)."""
    if isinstance(text, list):
        return text
    if isinstance(text, str) and len(text) > MAX_PARSE:
        return None
    try:
        value = json.loads(text) if isinstance(text, str) and text.strip() else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _id_list(text: Any) -> list:
    if isinstance(text, list):
        return text
    out = []
    for part in str(text or "").replace("[", "").replace("]", "").split(","):
        part = part.strip()
        if part:
            out.append(int(part) if part.lstrip("-").isdigit() else part)
    return out


def edit_records(url: str, params: dict, answer: Optional[dict], error: str, layer: str, values: bool) -> list[dict[str, Any]]:
    """The lines of an edit request: one per action it holds (add, update, delete)."""
    base = url.split("?", 1)[0].rstrip("/")
    target, _, op = base.rpartition("/")
    op = op.lower()
    answer = answer if isinstance(answer, dict) else None
    asked: list[tuple[str, Any, Optional[list], dict]] = []
    if op == "applyedits":
        for action, key, results in (("add", "adds", "addResults"), ("update", "updates", "updateResults"),
                                     ("delete", "deletes", "deleteResults")):
            given = params.get(key)
            if given in (None, "", "[]"):
                continue
            asked.append((action, given, (answer or {}).get(results), {}))
    elif op in ("addfeatures", "updatefeatures"):
        action = "add" if op == "addfeatures" else "update"
        asked.append((action, params.get("features"), (answer or {}).get(f"{action}Results"), {}))
    elif op == "deletefeatures":
        extra = {"where": params["where"]} if params.get("where") else {}
        asked.append(("delete", params.get("objectIds"), (answer or {}).get("deleteResults"), extra))
    else:
        asked.append((op, None, None, {}))
    out = []
    for action, given, results, extra in asked:
        detail: dict[str, Any] = {}
        if layer:
            detail["layer"] = layer
        detail.update(extra)
        if action == "delete":
            items = _id_list(given)
            given_ids = list(items)
        elif action in ("add", "update") and (error or not isinstance(results, list) or values):
            # Read back only when the answer does not say it all (or the values are wanted).
            items = _features(given)
            if items is None:
                items, detail["bytes"] = [], len(given)
            given_ids = [i for i in (_object_id(f.get("attributes") if isinstance(f, dict) else None) for f in items) if i is not None]
        else:
            items, given_ids = [], []
        rec: dict[str, Any] = {"kind": "edit", "action": action, "target": clean_url(target)}
        if error:
            rec.update(result="error", count=len(items) or None, ids=given_ids, error=mask(error))
        elif isinstance(results, list):
            done = [r for r in results if isinstance(r, dict) and r.get("success")]
            failed = [r for r in results if isinstance(r, dict) and not r.get("success")]
            ids = [r.get("objectId") for r in done if r.get("objectId") is not None] or (given_ids if not failed else [])
            rec.update(result="ok" if not failed else ("failed" if not done else "partial"), count=len(results), ids=ids)
            if failed:
                detail["failed"] = len(failed)
                why = ((failed[0].get("error") or {}).get("description") or "") if isinstance(failed[0].get("error"), dict) else ""
                if why:
                    rec["error"] = mask(str(why))
        else:
            ok = answer is None or answer.get("success") is not False
            rec.update(result="ok" if ok else "failed", count=len(items) or None, ids=given_ids)
        if values and action in ("add", "update") and items:
            kept = [_clean(f.get("attributes")) for f in items[:MAX_VALUES] if isinstance(f, dict)]
            detail["values"] = kept
            if len(items) > MAX_VALUES:
                detail["valuesCut"] = len(items) - MAX_VALUES
        rec["detail"] = detail
        out.append(rec)
    return out


def computer_name() -> str:
    name = os.environ.get("COMPUTERNAME", "").strip()
    if name:
        return name
    try:
        return socket.gethostname().split(".", 1)[0]
    except OSError:
        return ""


def safe_name(text: str) -> str:
    """A part of a file name: no path separators, colons, blanks…"""
    return _UNSAFE.sub("_", (text or "").strip()).strip("._") or "unknown"


def folder_of(pattern: str, when: float, user: str, computer: str) -> str:
    """The folder sent to, its placeholders filled in."""
    if not pattern:
        return ""
    d = datetime.fromtimestamp(when)
    values = {"user": safe_name(user), "computer": safe_name(computer), "yyyy": f"{d.year:04d}", "mm": f"{d.month:02d}", "dd": f"{d.day:02d}"}
    return re.sub(r"\{(user|computer|yyyy|mm|dd)\}", lambda m: values[m.group(1)], pattern)


def local_time(when: float) -> str:
    return datetime.fromtimestamp(when).astimezone().isoformat(timespec="milliseconds")


def day_of(when: float) -> str:
    return datetime.fromtimestamp(when).strftime("%Y%m%d")


# ---------------------------------------------------------------- the log

@dataclass
class AuditStatus:
    """How the writing and sending go (read by the screen; set by the threads)."""
    last_sent: Optional[float] = None
    #: The bytes the last send sent.
    sent_bytes: int = 0
    send_error: str = ""
    failing_since: Optional[float] = None
    write_error: str = ""
    #: Lines lost (memory full while the day's file could not be written).
    lost: int = 0
    written: int = 0


_STOP = object()
_SEND = object()


@dataclass
class SendResult:
    files: list[str] = field(default_factory=list)
    bytes: int = 0
    error: str = ""
    #: Another QGIS of this profile was sending.
    busy: bool = False


class AuditLog:
    """The day's files here and what of them was sent (see the module's description)."""

    def __init__(self, default_folder: str, settings: Optional[AuditSettings] = None, session: Optional[str] = None,
                 computer: Optional[str] = None, user: Optional[str] = None):
        self.default_folder = default_folder
        self.settings = settings or DEFAULT_AUDIT
        self.session = session or secrets.token_hex(6)
        self._computer = computer
        self._user = user
        self.seq = 0
        self.prev = ""
        self.status = AuditStatus()
        self.queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._configured = threading.Event()
        self._send_now = threading.Event()
        self._stopping = False
        self._unwritten: list[tuple[str, str, str]] = []
        self._writer: Optional[threading.Thread] = None
        self._sender: Optional[threading.Thread] = None
        self._first_send = 0.0
        self._tidied = ""
        #: (action, target, message) of an error written → [when, how many alike since, the last's time, its fields].
        self._recent_errors: dict[tuple, list] = {}
        if settings is not None:
            self._configured.set()

    # ------------------------------------------------ settings, any thread

    def configure(self, settings: AuditSettings) -> None:
        """The settings file was read: what is recorded and where."""
        self.settings = settings
        self._configured.set()

    @property
    def folder(self) -> str:
        """The folder of the day's files here."""
        return self.settings.local_folder or self.default_folder

    def wants(self, kind: str) -> bool:
        s = self.settings
        return not self._configured.is_set() or (s.enabled and kind in s.events)

    def put(self, kind: str, action: str, fields: dict) -> None:
        if self.wants(kind):
            self.queue.put((time.time(), kind, action, fields))

    def user(self) -> str:
        return self._user if self._user is not None else edit_stamp.user_name()

    def computer(self) -> str:
        if self._computer is None:
            self._computer = computer_name()
        return self._computer

    # ------------------------------------------------ threads

    def start(self, first_send: float = FIRST_SEND) -> None:
        self._first_send = time.time() + first_send
        self._writer = threading.Thread(target=self._write_loop, name="MIKAGE audit writer", daemon=True)
        self._sender = threading.Thread(target=self._send_loop, name="MIKAGE audit sender", daemon=True)
        self._writer.start()
        self._sender.start()

    def send_soon(self) -> None:
        """Writes what is queued, then sends (「今すぐ送信」)."""
        self.queue.put(_SEND)

    def stop(self, wait: float = EXIT_WAIT) -> None:
        """Writes what is queued and sends it, waiting at most `wait` s (the threads go on alone after)."""
        self._configured.set()
        self.queue.put(_STOP)
        deadline = time.monotonic() + wait
        for thread in (self._writer, self._sender):
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))

    def _write_loop(self) -> None:
        # The lines wait for the settings file (where they go, in what format).
        self._configured.wait()
        while True:
            items = [self.queue.get()]
            while True:
                try:
                    items.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            records = [i for i in items if i is not _STOP and i is not _SEND]
            try:
                self.write(records, final=any(i is _STOP for i in items))
            except Exception as error:  # noqa: BLE001 (the log never stops the plugin)
                self.status.write_error = str(error)
            if any(i is _STOP for i in items):
                self._stopping = True
                self._send_now.set()
                return
            if any(i is _SEND for i in items):
                self._send_now.set()

    def _send_loop(self) -> None:
        next_at = self._first_send
        while True:
            self._send_now.wait(max(0.0, next_at - time.time()))
            self._send_now.clear()
            if self._configured.is_set():
                self.send_safely()
            if self._stopping:
                return
            next_at = time.time() + max(60.0, self.settings.send_minutes * 60)

    # ------------------------------------------------ writing (the writer thread, or a test)

    def write_queued(self) -> None:
        """Writes what is queued, here and now (tests; a log whose threads were not started)."""
        items = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                break
        self.write([i for i in items if i is not _STOP and i is not _SEND])

    def records_of(self, kind: str, action: str, fields: dict) -> list[dict[str, Any]]:
        s = self.settings
        if kind == "edit" and not action:
            return edit_records(fields.get("url") or "", fields.get("params") or {}, fields.get("answer"),
                                fields.get("error") or "", fields.get("layer") or "", s.values)
        detail = dict(_clean(fields.get("detail") or {}))
        path = fields.get("file")
        if path:
            try:
                detail["bytes"], detail["sha256"] = file_digest(path)
            except OSError as error:
                detail["unreadable"] = error.strerror or str(error)
        text = fields.get("text")
        if text is not None:
            detail["chars"] = len(text)
            detail["sha256"] = sha256_text(text)
        trace = fields.get("trace")
        if trace:
            trace = mask(str(trace))
            detail["traceback"] = trace if len(trace) <= MAX_TRACE else "…" + trace[-MAX_TRACE:]
        ids = fields.get("ids")
        rec = {"kind": kind, "action": action, "target": clean_url(fields.get("target") or ""), "result": fields.get("result") or "ok",
               "count": fields.get("count"), "ids": [i for i in ids] if ids is not None else None, "detail": detail,
               "error": mask(str(fields.get("error") or ""))}
        return [rec]

    def write(self, items: list, final: bool = False) -> None:
        """Appends the lines of `items` ((time, kind, action, fields)) to the day's files; `final`, the last write."""
        s = self.settings
        lines: list[tuple[str, str, str]] = list(self._unwritten)
        self._unwritten = []
        if self._recent_errors:
            items = self._repeats(time.time(), final) + list(items)
        if s.enabled:
            user, computer = self.user(), self.computer()
            for when, kind, action, fields in items:
                if kind not in s.events:
                    continue
                if kind == "error" and not fields.get("repeated"):
                    fields = self._unless_repeated(when, action, fields)
                    if fields is None:
                        continue
                for rec in self.records_of(kind, action, fields):
                    self.seq += 1
                    rec.update(time=local_time(when), user=user, computer=computer, session=self.session, seq=self.seq, prev=self.prev)
                    text = line_of(rec, s.format)
                    if s.file_encoding == "cp932":
                        text = encodable(text, "cp932")[0]
                    self.prev = sha256_text(text)
                    lines.append((os.path.join(self.folder, f"audit-{day_of(when)}.{s.format}"), s.format, text))
        if not lines:
            return
        by_path: dict[str, tuple[str, list[str]]] = {}
        for path, fmt, text in lines:
            by_path.setdefault(path, (fmt, []))[1].append(text)
        failed: list[tuple[str, str, str]] = []
        error = ""
        for path, (fmt, texts) in by_path.items():
            try:
                append_lines(path, fmt, texts)
                self.status.written += len(texts)
            except OSError as e:
                error = f"{path}: {e.strerror or e}"
                failed.extend((path, fmt, t) for t in texts)
        if len(failed) > MAX_UNWRITTEN:
            self.status.lost += len(failed) - MAX_UNWRITTEN
            failed = failed[-MAX_UNWRITTEN:]
        self._unwritten = failed
        self.status.write_error = error

    def _unless_repeated(self, when: float, action: str, fields: dict) -> Optional[dict]:
        """The error's fields to write (with how many alike were left out before it), or None for one met just now."""
        message = str(fields.get("error") or "")
        if fields.get("html"):
            message = plain_text(message)
        if not fields.get("trace") and TRACEBACK in message:
            message, trace = split_trace(message)
            fields = {**fields, "trace": trace}
        if len(message) > MAX_MESSAGE:
            message = message[:MAX_MESSAGE] + "…"
        fields = {**fields, "error": message}
        key = (action, str(fields.get("target") or ""), message[:300])
        seen = self._recent_errors.get(key)
        if seen is not None and when - seen[0] < ERROR_REPEAT:
            seen[1] += 1
            seen[2] = when
            return None
        if len(self._recent_errors) >= 1000:
            return fields  # a storm of different errors: each written
        self._recent_errors[key] = [when, 0, when, {"target": fields.get("target"), "result": "error", "error": message}]
        return fields

    def _repeats(self, now: float, final: bool) -> list:
        """The lines telling how many more of an error were left out, for those whose ERROR_REPEAT has passed (all when `final`)."""
        out = []
        for key, (first, skipped, last, fields) in list(self._recent_errors.items()):
            if final or now - first >= ERROR_REPEAT:
                del self._recent_errors[key]
                if skipped:
                    out.append((last, "error", key[0], {**fields, "repeated": True, "detail": {"repeated": skipped}}))
        return out

    # ------------------------------------------------ sending (the sender thread, or a test)

    def send_safely(self, now: Optional[float] = None) -> SendResult:
        try:
            result = self.send(now)
        except Exception as error:  # noqa: BLE001 (the log never stops the plugin)
            result = SendResult(error=str(error))
            self._failed(result.error, now)
        return result

    def _failed(self, error: str, now: Optional[float]) -> None:
        self.status.send_error = error
        if self.status.failing_since is None:
            self.status.failing_since = now if now is not None else time.time()

    def local_files(self) -> list[str]:
        try:
            return sorted(n for n in os.listdir(self.folder) if _LOCAL.match(n))
        except OSError:
            return []

    def read_state(self) -> dict[str, int]:
        try:
            with open(os.path.join(self.folder, STATE), encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError):
            return {}
        sent = value.get("sent") if isinstance(value, dict) else None
        return {str(k): int(v) for k, v in (sent or {}).items() if isinstance(v, int)}

    def _write_state(self, state: dict[str, int]) -> None:
        path = os.path.join(self.folder, STATE)
        part = path + ".tmp"
        with open(part, "w", encoding="utf-8") as f:
            json.dump({"sent": state}, f, ensure_ascii=False)
        os.replace(part, path)

    def unsent(self) -> int:
        """Bytes of the day's files not sent yet (0 without a folder to send to)."""
        if not self.settings.folder:
            return 0
        state = self.read_state()
        total = 0
        for name in self.local_files():
            try:
                size = os.path.getsize(os.path.join(self.folder, name))
            except OSError:
                continue
            done = state.get(name, 0)
            total += size - done if size >= done else size
        return total

    def _lock(self) -> Optional[str]:
        path = os.path.join(self.folder, LOCK)
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(path) < STALE_LOCK:
                        return None
                    os.remove(path)
                except OSError:
                    return None
                continue
            except OSError:
                return None
            try:
                os.write(fd, f"{os.getpid()} {self.session}".encode("ascii"))
            finally:
                os.close(fd)
            return path
        return None

    def send(self, now: Optional[float] = None) -> SendResult:
        """
        Copies what the day's files hold beyond what was sent to the folder,
        one new file per day's file (only whole lines), then forgets the
        day's files older than `keepDays` once they are all sent.
        """
        now = time.time() if now is None else now
        s = self.settings
        result = SendResult()
        if not os.path.isdir(self.folder):
            return result
        lock = self._lock()
        if lock is None:
            result.busy = True
            return result
        try:
            state = self.read_state()
            target = folder_of(s.folder, now, self.user(), self.computer())
            if target:
                try:
                    self._send_files(target, state, now, result)
                except (OSError, ValueError) as error:
                    result.error = f"{getattr(error, 'filename', '') or target}: {getattr(error, 'strerror', None) or error}"
            if result.error:
                self._failed(result.error, now)
            elif target:
                self.status.last_sent, self.status.sent_bytes = now, result.bytes
                self.status.send_error, self.status.failing_since = "", None
            if self._tidied != day_of(now):
                self._tidied = day_of(now)
                self._tidy(state, now, bool(target))
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
        return result

    def _send_files(self, target: str, state: dict[str, int], now: float, result: SendResult) -> None:
        s = self.settings
        for name in self.local_files():
            path = os.path.join(self.folder, name)
            size = os.path.getsize(path)
            done = state.get(name, 0)
            if size < done:
                done = 0  # The file is shorter than what was sent: it was replaced; sent again whole.
            if size == done:
                continue
            with open(path, "rb") as f:
                f.seek(done)
                data = f.read(size - done)
            cut = data.rfind(b"\n") + 1
            if not cut:
                continue
            text = data[:cut].decode("utf-8", errors="replace")
            day, fmt = _LOCAL.match(name).groups()
            if fmt == "csv" and done > 0:
                text = header_of(fmt) + "\n" + text
            payload = text.encode(s.file_encoding, errors="replace")
            os.makedirs(target, exist_ok=True)
            stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
            sent = new_file(target, f"{PREFIX}_{safe_name(self.computer())}_{safe_name(self.user())}_{day}_{stamp}", fmt, payload)
            state[name] = done + cut
            self._write_state(state)
            result.files.append(sent)
            result.bytes += len(payload)

    def _tidy(self, state: dict[str, int], now: float, sending: bool) -> None:
        """Deletes the day's files older than keepDays (when there is a folder: only those sent whole)."""
        oldest = day_of(now - self.settings.keep_days * 86400)
        changed = False
        for name in self.local_files():
            day = _LOCAL.match(name).group(1)
            if day >= oldest or day >= day_of(now):
                continue
            path = os.path.join(self.folder, name)
            try:
                if sending and state.get(name, 0) < os.path.getsize(path):
                    continue
                os.remove(path)
            except OSError:
                continue
            changed = state.pop(name, None) is not None or changed
        if changed:
            try:
                self._write_state(state)
            except OSError:
                pass


def append_lines(path: str, fmt: str, texts: list[str]) -> None:
    """Appends lines to a day's file (UTF-8; a new CSV file starts with its header)."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    head = ""
    if fmt == "csv":
        try:
            new = os.path.getsize(path) == 0
        except OSError:
            new = True
        if new:
            head = header_of(fmt) + "\n"
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(head + "\n".join(texts) + "\n")


def new_file(folder: str, stem: str, ext: str, payload: bytes) -> str:
    """Writes a new file (never one already there: _2, _3… after the name); its path."""
    for n in range(1, 1000):
        path = os.path.join(folder, f"{stem}{'' if n == 1 else f'_{n}'}.{ext}")
        try:
            f = open(path, "xb")
        except FileExistsError:
            continue
        try:
            with f:
                f.write(payload)
        except OSError:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        return path
    raise OSError(f"{folder} に {stem} の名前の空きなし")


# ---------------------------------------------------------------- checking the lines sent

@dataclass
class SessionCheck:
    session: str
    user: str = ""
    computer: str = ""
    first: str = ""
    last: str = ""
    lines: int = 0
    ended: bool = False
    problems: list[str] = field(default_factory=list)


def parse_line(text: str) -> Optional[dict[str, Any]]:
    """A line of a log (JSON Lines or CSV) as a record; None for a header or a blank."""
    t = text.strip("\r\n")
    if not t.strip():
        return None
    if t.lstrip("﻿").startswith("{"):
        try:
            value = json.loads(t.lstrip("﻿"))
        except ValueError:
            return {"_bad": True}
        return value if isinstance(value, dict) else {"_bad": True}
    try:
        row = next(csv.reader([t.lstrip("﻿")]))
    except (csv.Error, StopIteration):
        return {"_bad": True}
    if row[:2] == list(COLUMNS[:2]):
        return None
    if len(row) != len(COLUMNS):
        return {"_bad": True}
    rec = dict(zip(COLUMNS, row))
    try:
        rec["seq"] = int(rec["seq"])
    except ValueError:
        return {"_bad": True}
    return rec


def verify(lines: Iterable[tuple[str, str]]) -> tuple[list[SessionCheck], list[str]]:
    """
    Checks the chains of (where, line) pairs: per session, numbers 1, 2, …
    without a gap and each line's `prev` the SHA-256 of the one before. The
    same line twice (sent again) is fine. The sessions and the lines that
    could not be read.
    """
    by_session: dict[str, dict[int, list[tuple[str, str, dict]]]] = {}
    bad: list[str] = []
    for where, raw in lines:
        text = raw.rstrip("\r\n").lstrip("﻿")
        rec = parse_line(text)
        if rec is None:
            continue
        if rec.get("_bad") or not rec.get("session") or not isinstance(rec.get("seq"), int):
            bad.append(where)
            continue
        by_session.setdefault(str(rec["session"]), {}).setdefault(rec["seq"], []).append((sha256_text(text), where, rec))
    out = []
    for session, by_seq in by_session.items():
        check = SessionCheck(session)
        seqs = sorted(by_seq)
        previous_hash: Optional[str] = None
        expected = 1
        for seq in seqs:
            versions = by_seq[seq]
            hashes = {h for h, _, _ in versions}
            h, where, rec = versions[0]
            if len(hashes) > 1:
                check.problems.append(f"{seq} 番に内容の違う行が 2 つ以上（{where}）")
            if seq != expected:
                check.problems.append(f"{expected}〜{seq - 1} 番の行がない" if seq - 1 > expected else f"{expected} 番の行がない")
            elif previous_hash is not None and (rec.get("prev") or "") != previous_hash:
                check.problems.append(f"{seq - 1} 番の行が書き換えられている（{seq} 番の prev と合わない、{where}）")
            elif previous_hash is None and seq == 1 and rec.get("prev"):
                check.problems.append(f"1 番の行の prev が空でない（{where}）")
            previous_hash = h
            expected = seq + 1
            check.lines += 1
            check.user = check.user or str(rec.get("user") or "")
            check.computer = check.computer or str(rec.get("computer") or "")
            t = str(rec.get("time") or "")
            check.first = min(check.first, t) if check.first else t
            check.last = max(check.last, t)
            if rec.get("kind") == "session" and rec.get("action") == "end":
                check.ended = True
        out.append(check)
    out.sort(key=lambda c: (c.first, c.session))
    return out, bad


def read_folder(folder: str) -> Iterable[tuple[str, str]]:
    """The lines of the logs in a folder (and the folders under it): (file:line, text)."""
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if not name.lower().endswith((".jsonl", ".csv")):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp932", errors="replace")
            for i, line in enumerate(text.split("\n"), 1):
                if line.strip():
                    yield f"{os.path.relpath(path, folder)}:{i}", line


def check_text(folder: str) -> tuple[str, bool]:
    """The report of a folder's logs, and whether all is well."""
    sessions, bad = verify(read_folder(folder))
    problems = sum(len(s.problems) for s in sessions) + len(bad)
    out = [f"監査ログの点検: {folder}", f"セッション {len(sessions)} 件・行 {sum(s.lines for s in sessions)} 行・問題 {problems} 件", ""]
    for s in sessions:
        mark = "問題" if s.problems else "OK"
        out.append(f"[{mark}] {s.session}  {s.user}@{s.computer}  {s.first} 〜 {s.last}  {s.lines} 行"
                   + ("" if s.ended else "（終了の行なし: 動作中か、送られていない・異常終了）"))
        out.extend(f"  - {p}" for p in s.problems)
    if bad:
        out.append("")
        out.append(f"読めない行 {len(bad)} 行:")
        out.extend(f"  - {w}" for w in bad[:50])
    return "\n".join(out) + "\n", problems == 0


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("使い方: python -m mikage.core.audit <監査ログのフォルダ>")
        return 2
    ok = True
    for folder in args:
        text, fine = check_text(folder)
        sys.stdout.write(text)
        ok = ok and fine
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
