"""
引き継ぎメモ (shift handover): at the end of a shift, the text handed to the
next one, made from what is already kept: the observation records made,
changed and deleted in the shift, the 通報 sent (alertReports' log), the
imaging requests still open, and the 分担 (who holds which image, its state
and how many records it has), with the images open but held by nobody, and
a 申し送り written by hand.

The settings file's `handover` (all optional) says how times are written,
how long a shift is by default, how many lines a list shows, a file to
write, and an Esri table where the memos are kept: the next shift reads the
last one there, and its period starts where that one ended.
"""
from __future__ import annotations

import math
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError, fit_value
from ..request.export import format_time
from ..request.model import ImagingRequest
from ..time_zone import UTC, TimeZone, parse_time_zone
from ..util import encodable, is_number, is_record, js_string, ms_of, utc_text
from .alerts import ENCODINGS, NEWLINES, EsriSource, FileOutput, LogRow, esri_source_of
from .assignments import Assignment, Progress, assignees, progress_of, progress_text, same_person
from .model import Observation
from .taxonomy import path_of

HOUR_MS = 3_600_000
DEFAULT_TIME_ZONE = "Asia/Tokyo"
#: The memo table's attributes by role.
DEFAULT_HANDOVER_FIELDS = {
    "text": "TEXT", "memo": "MEMO", "start": "PERIOD_START", "end": "PERIOD_END", "writtenAt": "WRITTEN_AT", "writtenBy": "WRITTEN_BY",
}


@dataclass
class HandoverSettings:
    """`handover`."""
    date_format: str = "%m/%d %H:%M"
    time_zone: TimeZone = field(default_factory=lambda: parse_time_zone(DEFAULT_TIME_ZONE) or UTC)
    #: A shift's length: the period's start when no memo was kept before.
    hours: float = 12.0
    #: Lines a list shows at most (the rest counted).
    limit: int = 20
    #: Requests ending within this many hours are marked.
    due_hours: float = 24.0
    newline: str = "\r\n"
    table: Optional[EsriSource] = None
    file: Optional[FileOutput] = None


def handover_settings_of(value: Any, problems: list[str]) -> HandoverSettings:
    at = "handover"
    s = HandoverSettings()
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return s
    if "dateFormat" in value:
        if isinstance(value["dateFormat"], str) and value["dateFormat"]:
            s.date_format = value["dateFormat"]
        else:
            problems.append(f"{at}.dateFormat は %m/%d %H:%M のような書式で指定")
    if "timeZone" in value:
        zone = parse_time_zone(value["timeZone"])
        if zone is None:
            problems.append(f"{at}.timeZone は Asia/Tokyo・+09:00・UTC のように指定")
        else:
            s.time_zone = zone
    for key, attr, low, high in (("hours", "hours", 0.5, 168), ("limit", "limit", 1, 1000), ("dueHours", "due_hours", 0, 720)):
        if key in value:
            v = value[key]
            if is_number(v) and low <= v <= high:
                setattr(s, attr, int(v) if attr == "limit" else float(v))
            else:
                problems.append(f"{at}.{key} は {low:g}〜{high:g} の数で指定")
    if "newline" in value:
        nl = NEWLINES.get(str(value["newline"]).lower())
        if nl is None:
            problems.append(f"{at}.newline は crlf か lf で指定")
        else:
            s.newline = nl
    if "table" in value:
        s.table = esri_source_of(value["table"], f"{at}.table", DEFAULT_HANDOVER_FIELDS, problems, editable=True)
    if "file" in value:
        f = value["file"]
        f = {"path": f} if isinstance(f, str) else f
        if not is_record(f) or not isinstance(f.get("path"), str) or not f["path"].strip():
            problems.append(f"{at}.file は path（ファイルのパス）で指定")
        else:
            encoding = ENCODINGS.get(str(f.get("encoding", "utf-8")).lower())
            if encoding is None:
                problems.append(f"{at}.file.encoding は utf-8・utf-8-sig・cp932（shift_jis）のどれかで指定")
                encoding = "utf-8"
            append = f.get("append", False)
            s.file = FileOutput(path=f["path"].strip(), encoding=encoding, append=append is True)
    return s


# ---- what goes in ---------------------------------------------------------------


@dataclass
class OpenRequest:
    """A request still to be taken, and its ledger."""
    request: ImagingRequest
    store: str = ""


@dataclass
class HandoverData:
    """Everything the memo is made of."""
    start: float
    end: float
    now: float
    user: str = ""
    records: Sequence[Observation] = ()
    #: None: no alertReports log (the section is left out).
    alerts: Optional[Sequence[LogRow]] = None
    #: None: no request ledger (left out).
    requests: Optional[Sequence[OpenRequest]] = None
    #: None: no assignment table (left out).
    assignments: Optional[Sequence[Assignment]] = None
    #: Images open in the observation dock (id, name), to say which nobody holds.
    images: Sequence[tuple[str, str]] = ()
    memo: str = ""
    #: Problems met while reading (a part that could not be read).
    problems: Sequence[str] = ()


def open_requests(requests: Iterable[ImagingRequest], now: float, store: str = "") -> list[OpenRequest]:
    """Requests 有効 whose period has not ended."""
    out = []
    for r in requests:
        if r.status != "有効":
            continue
        if is_number(r.end) and math.isfinite(r.end) and r.end < now:
            continue
        out.append(OpenRequest(r, store))
    return out


# ---- writing -------------------------------------------------------------------


def _time(ms: Optional[float], s: HandoverSettings, fmt: Optional[str] = None) -> str:
    if ms is None or not is_number(ms) or not math.isfinite(ms):
        return ""
    return js_string(format_time(ms, fmt or s.date_format, s.time_zone))


def _counts(values: Iterable[str]) -> str:
    c = Counter(v or "（なし）" for v in values)
    return "・".join(f"{k} {n}" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def _cut(lines: list[str], limit: int) -> list[str]:
    return lines if len(lines) <= limit else lines[:limit] + [f"  …ほか {len(lines) - limit} 件"]


def _in(t: Optional[float], start: float, end: float) -> bool:
    return t is not None and start <= t <= end


def records_section(d: HandoverData, s: HandoverSettings) -> list[str]:
    made = [o for o in d.records if o.deleted_at is None and _in(o.created_at, d.start, d.end)]
    changed = [o for o in d.records if o.deleted_at is None and not _in(o.created_at, d.start, d.end) and _in(o.updated_at, d.start, d.end)]
    gone = [o for o in d.records if _in(o.deleted_at, d.start, d.end) and not _in(o.created_at, d.start, d.end)]
    absent = sum(1 for o in made if not o.present)
    out = [f"■ 観測記録: 新規 {len(made)} 件" + (f"（うち不在 {absent} 件）" if absent else "") + f"・更新 {len(changed)} 件・削除 {len(gone)} 件"]
    if made:
        out.append("  分類: " + _counts((path_of(o.kind) or [""])[0] for o in made))
        out.append("  記録者: " + _counts(o.recorded_by for o in made))
        out.append("  画像: " + _counts(o.image_name or o.image_id for o in made))
    return out


def alerts_section(d: HandoverData, s: HandoverSettings) -> list[str]:
    rows = [r for r in d.alerts or () if r.reported_at is None or _in(r.reported_at, d.start, d.end)]
    out = [f"■ 通報: {len(rows)} 件" + (f"（{_counts(r.format for r in rows)}）" if rows else "")]
    lines = [f"  {_time(r.reported_at, s)} {r.text}".rstrip() for r in sorted(rows, key=lambda r: r.reported_at or 0)]
    return out + _cut(lines, s.limit)


def requests_section(d: HandoverData, s: HandoverSettings) -> list[str]:
    items = sorted(d.requests or (), key=lambda x: (-(x.request.priority + x.request.boost),
                                                    x.request.end if math.isfinite(x.request.end) else math.inf))
    soon = d.now + s.due_hours * HOUR_MS
    due = [x for x in items if math.isfinite(x.request.end) and x.request.end <= soon]
    due_ids = {id(x) for x in due}
    out = [f"■ 未処理の撮像依頼: {len(items)} 件" + (f"（うち {s.due_hours:g} 時間以内に期限 {len(due)} 件）" if due else "")]
    lines = []
    for x in items:
        r = x.request
        end = _time(r.end, s) if math.isfinite(r.end) else "期限なし"
        mark = "【期限間近】" if id(x) in due_ids else ""
        lines.append(f"  {mark}{r.name or r.id}（{r.category}・{r.kind}・優先度 {r.priority + r.boost}・期限 {end}）")
    return out + _cut(lines, s.limit)


def assignments_section(d: HandoverData, s: HandoverSettings) -> list[str]:
    items = list(d.assignments or ())
    held = [x for x in items if not x.done]
    done = [x for x in items if x.done and _in(x.done_at, d.start, d.end)]
    out = [f"■ 分担: 担当中 {len(held)} 件・期間内に完了 {len(done)} 件"]
    progress: dict[int, Progress] = {id(x): progress_of(x, d.records) for x in held + done}
    for person in assignees(held + done):
        mine = [x for x in held if same_person(x.assignee, person)]
        finished = [x for x in done if same_person(x.assignee, person)]
        out.append(f"  {person}: 担当中 {len(mine)} 件・完了 {len(finished)} 件")
        lines = []
        for x in mine + finished:
            note = f"・{x.note}" if x.note else ""
            lines.append(f"    {x.label}（{x.status}・{progress_text(progress[id(x)])}{note}）")
        out += _cut(lines, s.limit)
    held_ids = {x.image_id for x in held} | {x.image_id for x in items if x.done}
    free = [name or i for i, name in d.images if i not in held_ids]
    if free:
        out.append(f"  担当者なしの画像（観測記録で開いている画像）: {len(free)} 件")
        out += _cut([f"    {n}" for n in free], s.limit)
    return out


def handover_text(d: HandoverData, s: HandoverSettings) -> str:
    """The memo (each line ending in the settings' newline)."""
    lines = [f"引き継ぎメモ {_time(d.start, s)}〜{_time(d.end, s)}（作成 {d.user or '—'}・{_time(d.now, s)}）", ""]
    lines += records_section(d, s) + [""]
    if d.alerts is not None:
        lines += alerts_section(d, s) + [""]
    if d.requests is not None:
        lines += requests_section(d, s) + [""]
    if d.assignments is not None:
        lines += assignments_section(d, s) + [""]
    if d.problems:
        lines += ["■ 読み込めなかったもの"] + [f"  {p}" for p in d.problems] + [""]
    memo = d.memo.strip()
    lines += ["■ 申し送り"] + ([f"  {line}" for line in memo.splitlines()] if memo else ["  なし"])
    return "".join(line + s.newline for line in lines)


# ---- the memo table and the file ------------------------------------------------------


@dataclass
class KeptMemo:
    """A memo kept in the table."""
    text: str
    memo: str = ""
    start: Optional[int] = None
    end: Optional[int] = None
    written_at: Optional[int] = None
    written_by: str = ""


def _types(client: EsriClient, table: EsriSource, token: Optional[str]) -> dict[str, str]:
    types = client.layer(table.url, token, refresh=True).field_types
    if table.fields["text"] not in types:
        raise EsriError(f"引き継ぎメモのテーブルに属性 {table.fields['text']} なし")
    return types


def last_memo(client: EsriClient, table: EsriSource, token: Optional[str] = None, since: Optional[float] = None) -> Optional[KeptMemo]:
    """The newest memo kept (looked for in the last 30 days when the table has a time attribute)."""
    token = token or table.token
    types = _types(client, table, token)
    f = table.fields
    clauses = [table.where] if table.where else []
    if f["writtenAt"] in types and since is not None:
        clauses.append(f"{f['writtenAt']} >= TIMESTAMP '{utc_text(since)}'")
    rows = client.query_all(table.url, " AND ".join(f"({c})" for c in clauses) or "1=1", token, geometry=False, limit=10_000, cut=True)
    memos = []
    for r in rows:
        a = r.get("attributes") or {}
        memos.append(KeptMemo(
            text=js_string(a.get(f["text"]) or ""), memo=js_string(a.get(f["memo"]) or ""), start=ms_of(a.get(f["start"])),
            end=ms_of(a.get(f["end"])), written_at=ms_of(a.get(f["writtenAt"])), written_by=js_string(a.get(f["writtenBy"]) or ""),
        ))
    return max(memos, key=lambda m: (m.written_at or 0, m.end or 0), default=None)


def keep_memo(client: EsriClient, table: EsriSource, text: str, d: HandoverData, token: Optional[str] = None) -> list[str]:
    """
    Adds the memo to the table; the attributes cut to the length the table
    takes (said back, so the person knows the file and the clipboard have it whole).
    """
    token = token or table.token
    types = _types(client, table, token)
    lengths = client.layer(table.url, token).field_lengths
    f = table.fields
    values = {f["text"]: text, f["memo"]: d.memo, f["start"]: int(d.start), f["end"]: int(d.end), f["writtenAt"]: int(d.now),
              f["writtenBy"]: d.user}
    cut = []
    for k, v in values.items():
        n = lengths.get(k)
        if isinstance(v, str) and n and len(v) > n:
            mark = "…（以下省略）"
            values[k] = v[:max(0, n - len(mark))] + mark[:n]
            cut.append(f"{k}（{n} 文字まで）")
    r = (client.apply_edits(table.url, adds=[{"attributes": {k: fit_value(v, types[k]) for k, v in values.items() if k in types}}],
                            token=token).add_results or [{}])[0]
    if not r.get("success"):
        raise EsriError(f"引き継ぎメモの保存の失敗: {(r.get('error') or {}).get('description') or '理由不明'}")
    return cut


def file_path(s: HandoverSettings, now: float) -> str:
    """The file's path, `{now:%Y%m%d}` filled (the settings' time zone)."""
    if s.file is None:
        return "", False
    import re

    return re.sub(r"\{now(?::([^}]*))?\}", lambda m: _time(now, s, m.group(1) or "%Y%m%d_%H%M"), s.file.path)


def write_file(text: str, s: HandoverSettings, now: float, opener: Callable = open) -> tuple[str, bool]:
    """
    Writes (or appends) the memo to the settings' file: its path, and True when
    characters the file's encoding cannot hold (cp932: —, é, emoji…) became ?.
    """
    if s.file is None:
        return "", False
    path = file_path(s, now)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    text, replaced = encodable(text, s.file.encoding)
    with opener(path, "a" if s.file.append else "w", encoding=s.file.encoding, newline="") as out:
        out.write(text)
    return path, replaced
