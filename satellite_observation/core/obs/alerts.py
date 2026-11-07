"""
Alerts (通報): the notable observation records written as one line each,
the values joined by a delimiter, to paste or pass on as text.

Which records are notable is data kept in Esri (it changes often): a rules
table (one row per rule: a class and below, its activities, the least
確度, a geofence) and a polygon layer of geofences. The settings file
(`alertReports`) only says where they are, their attribute names, and how
the text is written: the fields and their order, the delimiter, the header
and footer, the date format and time zone, and where it goes (an Esri
table first, then the clipboard and a file).

The settings name several formats (one the default); the rules, the
geofences and the log are shared by them. A record goes out once: the Esri
log table keeps each record reported (its id, the format's name, the line,
when and by whom), and the records already there are left out next time.
"""
from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError, fit_value, quote
from ..time_zone import UTC, TimeZone, parse_time_zone
from ..util import is_name, is_number, is_record, js_string, layer_url, ms_of, utc_text
from ..request.export import format_time
from .geo import to_fixed
from .model import ConfidenceLevel, Observation
from .taxonomy import kind_of, path_of, within

#: What a field can take (`source`).
SOURCES = (
    "id", "imageId", "imageName", "imageTime", "objectId", "objectName", "kind", "activity", "present", "confidence", "note",
    "lon", "lat", "length", "heading", "width", "recordedBy", "updatedBy", "created", "updated",
    "area", "rule", "report", "reportedAt", "reportedBy", "index", "count",
)
TIME_SOURCES = ("imageTime", "created", "updated", "reportedAt")
NUMBER_SOURCES = ("lon", "lat", "length", "heading", "width")
#: The fields written when the settings give none.
DEFAULT_FIELDS = ("imageTime", "kind", "objectName", "activity", "confidence", "lat", "lon", "area", "note")
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M"
DEFAULT_TIME_ZONE = "Asia/Tokyo"
ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "utf-8-sig": "utf-8-sig", "utf-8-bom": "utf-8-sig",
             "cp932": "cp932", "shift_jis": "cp932", "sjis": "cp932", "shift-jis": "cp932"}
NEWLINES = {"crlf": "\r\n", "lf": "\n"}

#: The rules table's attributes by role.
DEFAULT_RULE_FIELDS = {
    "kind": "KIND", "activities": "ACTIVITIES", "minConfidence": "MIN_CONFIDENCE", "area": "AREA",
    "active": "ACTIVE", "note": "NOTE",
}
#: The geofence layer's attributes by role.
DEFAULT_FENCE_FIELDS = {"name": "NAME", "active": "ACTIVE"}
#: The log table's attributes by role.
DEFAULT_LOG_FIELDS = {
    "obsId": "OBS_ID", "report": "REPORT_NAME", "text": "TEXT", "reportedAt": "REPORTED_AT", "reportedBy": "REPORTED_BY",
    "batch": "BATCH_ID",
}
_FEATURE_LAYER = re.compile(r"/(Feature|Map)Server/\d+/?$", re.IGNORECASE)
_EDITABLE = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)


@dataclass
class AlertField:
    """One value of the line: a source (or a fixed text) and how it is written."""
    source: Optional[str] = None
    #: A fixed text instead of a source.
    text: Optional[str] = None
    #: Times: strftime (`%Y%m%d %H%M`), `iso`, `iso-date`, `unix`, `unixms`; the report's dateFormat when None.
    format: Optional[str] = None
    #: Numbers: decimals.
    digits: Optional[int] = None
    #: `kind`: the depth (1 the top, -1 the last); the whole path when None.
    level: Optional[int] = None
    #: Values replaced (`{"true": "在", "false": "不在"}`), compared as text.
    map: dict[str, str] = field(default_factory=dict)
    #: Written when the value is empty.
    empty: str = ""


@dataclass
class EsriSource:
    """An Esri layer or table and its attribute names by role."""
    url: str
    fields: dict[str, str]
    token: Optional[str] = None
    where: Optional[str] = None


@dataclass
class FileOutput:
    """`output.file`: where the text is written."""
    #: May hold `{name}` and `{now:%Y%m%d}` (the report's time zone).
    path: str
    encoding: str = "utf-8"
    append: bool = False


@dataclass
class AlertFormat:
    """One of `alertReports.formats`: how the text is written, and where it goes."""
    name: str
    fields: list[AlertField] = field(default_factory=lambda: [AlertField(source=s) for s in DEFAULT_FIELDS])
    delimiter: str = "|"
    #: What the delimiter and line breaks inside a value become.
    escape: str = " "
    header: str = ""
    footer: str = ""
    date_format: str = DEFAULT_DATE_FORMAT
    time_zone: TimeZone = field(default_factory=lambda: parse_time_zone(DEFAULT_TIME_ZONE) or UTC)
    time_zone_name: str = DEFAULT_TIME_ZONE
    newline: str = "\r\n"
    #: The log table's own columns for this format: attribute name → field (a source as `fields` takes).
    columns: dict[str, AlertField] = field(default_factory=dict)
    clipboard: bool = True
    file: Optional[FileOutput] = None


@dataclass
class AlertSettings:
    """`alertReports`: which records are notable, the log, and the named formats (one the default)."""
    formats: list[AlertFormat] = field(default_factory=list)
    #: The format chosen first (a name of `formats`).
    default: str = ""
    #: Fixed conditions: the least 確度 ('' any), and whether 不在 records are left out.
    min_confidence: str = ""
    present_only: bool = True
    rules: Optional[EsriSource] = None
    geofences: Optional[EsriSource] = None
    #: The Esri log table (the main output; also what was reported).
    table: Optional[EsriSource] = None

    def format(self, name: str = "") -> Optional[AlertFormat]:
        """The format of a name, else the default one, else the first."""
        by = {f.name: f for f in self.formats}
        return by.get(name) or by.get(self.default) or (self.formats[0] if self.formats else None)


@dataclass
class AlertRule:
    """One row of the rules table."""
    kind: list[str] = field(default_factory=list)
    #: The activities ([] any).
    activities: list[str] = field(default_factory=list)
    min_confidence: str = ""
    #: A geofence's name ('' anywhere, `*` inside any).
    area: str = ""
    note: str = ""


@dataclass
class Geofence:
    """A polygon of the geofence layer (WGS 84 rings)."""
    name: str
    rings: list


@dataclass
class AlertItem:
    """A notable record, the geofences it is in and the rules it met."""
    record: Observation
    areas: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)


# ---- the settings file ------------------------------------------------------------


def _field_of(v: Any, at: str, problems: list[str]) -> Optional[AlertField]:
    if isinstance(v, str):
        v = {"source": v}
    if not is_record(v):
        problems.append(f"{at} は項目名か {{ source, … }} で指定")
        return None
    if "text" in v and "source" not in v:
        return AlertField(text=js_string(v["text"]))
    source = v.get("source")
    if source not in SOURCES:
        problems.append(f"{at} の source「{js_string(source)}」は使用不可（{'・'.join(SOURCES)}、または text）")
        return None
    digits, level = v.get("digits"), v.get("level")
    if "digits" in v and not (is_number(digits) and 0 <= digits <= 12):
        problems.append(f"{at} の digits は 0〜12 で指定")
    if "level" in v and not (is_number(level) and int(level) == level and level != 0):
        problems.append(f"{at} の level は 1（最上位）からの階層、または -1（最下位）で指定")
    fmt = v.get("format")
    mapping = v.get("map")
    if "map" in v and not is_record(mapping):
        problems.append(f"{at} の map はオブジェクトで指定")
    return AlertField(
        source=source,
        format=fmt if isinstance(fmt, str) and fmt else None,
        digits=int(digits) if is_number(digits) and 0 <= digits <= 12 else None,
        level=int(level) if is_number(level) and int(level) == level and level != 0 else None,
        map={str(k): js_string(x) for k, x in mapping.items()} if is_record(mapping) else {},
        empty=js_string(v["empty"]) if "empty" in v and v["empty"] is not None else "",
    )


def _source_of(v: Any, at: str, defaults: dict[str, str], problems: list[str], editable: bool = False,
               ) -> Optional[EsriSource]:
    v = {"url": v} if isinstance(v, str) else v
    pattern = _EDITABLE if editable else _FEATURE_LAYER
    if not is_record(v) or not isinstance(v.get("url"), str) or not pattern.search(v["url"].split("?", 1)[0]):
        what = "編集できる …/FeatureServer/0" if editable else "…/FeatureServer/0"
        problems.append(f"{at} は {what} のようなレイヤー・テーブルの URL で指定")
        return None
    fields = dict(defaults)
    if is_record(v.get("fields")):
        for k, name in v["fields"].items():
            if k not in defaults:
                problems.append(f"{at}.fields.{k} は使用不可の役割（{'・'.join(defaults)}）")
            elif is_name(name):
                fields[k] = name
            else:
                problems.append(f"{at}.fields.{k} は属性名で指定")
    token, where = v.get("token"), v.get("where")
    return EsriSource(
        url=layer_url(v["url"]), fields=fields,
        token=token if isinstance(token, str) and token else None,
        where=where.strip() if isinstance(where, str) and where.strip() else None,
    )


def _lines_of(v: Any, at: str, problems: list[str]) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return "\n".join(v)
    problems.append(f"{at} は文字列か文字列の配列（1 行ずつ）で指定")
    return ""


def _bool(value: dict, key: str, at: str, problems: list[str], default: bool) -> bool:
    if key not in value:
        return default
    if isinstance(value[key], bool):
        return value[key]
    problems.append(f"{at}.{key} は true か false で指定")
    return default


def alert_format_of(value: Any, problems: list[str], at: str) -> Optional[AlertFormat]:
    """One `alertReports.formats` entry; None (the reason in `problems`) when it cannot be used."""
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{at} に name（フォーマットの名前）なし")
        return None
    fmt = AlertFormat(name=name.strip())
    if "fields" in value:
        if isinstance(value["fields"], list) and value["fields"]:
            fmt.fields = [f for f in (_field_of(v, f"{at}.fields[{i}]", problems) for i, v in enumerate(value["fields"])) if f]
        else:
            problems.append(f"{at}.fields は項目の配列で指定")
    for key in ("delimiter", "escape"):
        if key in value:
            if isinstance(value[key], str) and (value[key] or key == "escape"):
                setattr(fmt, key, value[key])
            else:
                problems.append(f"{at}.{key} は文字列で指定")
    fmt.header = _lines_of(value.get("header"), f"{at}.header", problems)
    fmt.footer = _lines_of(value.get("footer"), f"{at}.footer", problems)
    if "dateFormat" in value:
        if isinstance(value["dateFormat"], str) and value["dateFormat"]:
            fmt.date_format = value["dateFormat"]
        else:
            problems.append(f"{at}.dateFormat は %Y-%m-%d %H:%M のような書式で指定")
    if "timeZone" in value:
        zone = parse_time_zone(value["timeZone"])
        if zone is None:
            problems.append(f"{at}.timeZone は Asia/Tokyo・+09:00・UTC のように指定")
        else:
            fmt.time_zone, fmt.time_zone_name = zone, str(value["timeZone"]).strip()
    if "newline" in value:
        nl = NEWLINES.get(str(value["newline"]).lower())
        if nl is None:
            problems.append(f"{at}.newline は crlf か lf で指定")
        else:
            fmt.newline = nl
    columns = value.get("columns")
    if "columns" in value and not is_record(columns):
        problems.append(f"{at}.columns は {{ 属性名: 項目 }} で指定")
    for attr, v in (columns if is_record(columns) else {}).items():
        if not is_name(attr):
            problems.append(f"{at}.columns の「{attr}」は属性名で指定")
            continue
        f = _field_of(v, f"{at}.columns.{attr}", problems)
        if f is not None:
            fmt.columns[attr] = f
    fmt.clipboard = _bool(value, "clipboard", at, problems, True)
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
            fmt.file = FileOutput(path=f["path"].strip(), encoding=encoding, append=_bool(f, "append", f"{at}.file", problems, False))
    return fmt


def alert_settings_of(value: Any, problems: list[str]) -> Optional[AlertSettings]:
    """`alertReports`; None when it cannot be used (no format)."""
    at = "alertReports"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    settings = AlertSettings()
    raw = value.get("formats")
    if not isinstance(raw, list) or not raw:
        problems.append(f"{at}.formats はフォーマットの配列で指定")
        raw = []
    for i, v in enumerate(raw):
        f = alert_format_of(v, problems, f"{at}.formats[{i}]")
        if f is None:
            continue
        if any(x.name == f.name for x in settings.formats):
            problems.append(f"{at}.formats の name「{f.name}」が重複")
            continue
        settings.formats.append(f)
    if not settings.formats:
        return None
    default = value.get("default")
    if default is not None:
        if isinstance(default, str) and any(f.name == default.strip() for f in settings.formats):
            settings.default = default.strip()
        else:
            problems.append(f"{at}.default は formats の name のどれかで指定")
    settings.default = settings.default or settings.formats[0].name
    if "minConfidence" in value:
        if isinstance(value["minConfidence"], str):
            settings.min_confidence = value["minConfidence"].strip()
        else:
            problems.append(f"{at}.minConfidence は確度の名前で指定")
    settings.present_only = _bool(value, "presentOnly", at, problems, True)
    if "rules" in value:
        settings.rules = _source_of(value["rules"], f"{at}.rules", DEFAULT_RULE_FIELDS, problems)
    if "geofences" in value:
        settings.geofences = _source_of(value["geofences"], f"{at}.geofences", DEFAULT_FENCE_FIELDS, problems)
    if "table" in value:
        settings.table = _source_of(value["table"], f"{at}.table", DEFAULT_LOG_FIELDS, problems, editable=True)
    return settings


# ---- reading the rules, the geofences and the log ------------------------------------------------


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def _active(v: Any) -> bool:
    if v is None or v == "":
        return True
    if v is False or (is_number(v) and v == 0):
        return False
    return not re.fullmatch(r"(0|false|no|off|いいえ|無効|停止)", _text(v), re.IGNORECASE)


def _list(v: Any) -> list[str]:
    return [s.strip() for s in re.split(r"[,、，\n]", _text(v)) if s.strip()]


def rules_of_rows(rows: Iterable[dict[str, Any]], fields: dict[str, str]) -> list[AlertRule]:
    """The active rules of the table's rows (attributes)."""
    out = []
    for a in rows:
        if fields["active"] in a and not _active(a.get(fields["active"])):
            continue
        activities = _list(a.get(fields["activities"]))
        out.append(AlertRule(
            kind=path_of(_text(a.get(fields["kind"]))),
            activities=[] if "*" in activities else activities,
            min_confidence=_text(a.get(fields["minConfidence"])),
            area=_text(a.get(fields["area"])),
            note=_text(a.get(fields["note"])),
        ))
    return out


def load_rules(client: EsriClient, source: EsriSource, token: Optional[str] = None) -> list[AlertRule]:
    """The active rules of the rules table."""
    rows = client.query_all(source.url, source.where or "1=1", token or source.token, geometry=False, limit=100_000)
    return rules_of_rows([f.get("attributes") or {} for f in rows], source.fields)


def load_geofences(client: EsriClient, source: EsriSource, token: Optional[str] = None) -> list[Geofence]:
    """The active geofences (polygons, WGS 84)."""
    out = []
    f = source.fields
    for feature in client.query_all(source.url, source.where or "1=1", token or source.token, limit=100_000):
        a = feature.get("attributes") or {}
        rings = (feature.get("geometry") or {}).get("rings") or []
        if not rings or (f["active"] in a and not _active(a.get(f["active"]))):
            continue
        out.append(Geofence(name=_text(a.get(f["name"])), rings=rings))
    return out


def _log_types(client: EsriClient, settings: AlertSettings, token: Optional[str]) -> dict[str, str]:
    table = settings.table
    types = client.layer(table.url, token or table.token, refresh=True).field_types
    if table.fields["obsId"] not in types:
        raise EsriError(f"通報テーブルに属性 {table.fields['obsId']} なし")
    return types


def reported_ids(client: EsriClient, settings: AlertSettings, token: Optional[str] = None) -> set[str]:
    """The ids of the records already reported (in any format, from the log table)."""
    table = settings.table
    if table is None:
        return set()
    f = table.fields
    _log_types(client, settings, token)
    rows = client.query_all(table.url, table.where or "1=1", token or table.token, out_fields=f["obsId"], geometry=False,
                            limit=1_000_000)
    return {_text((r.get("attributes") or {}).get(f["obsId"])) for r in rows} - {""}


@dataclass
class LogRow:
    """A row of the log table: one record reported."""
    obs_id: str
    format: str = ""
    text: str = ""
    reported_at: Optional[int] = None
    reported_by: str = ""
    batch: str = ""
    oid: Optional[int] = None


def load_log(client: EsriClient, settings: AlertSettings, start: Optional[float] = None, end: Optional[float] = None,
             token: Optional[str] = None, limit: int = 20_000) -> list[LogRow]:
    """The rows reported in a period (newest first; the whole log when the table has no time attribute)."""
    table = settings.table
    if table is None:
        return []
    token = token or table.token
    types = _log_types(client, settings, token)
    f = table.fields
    oid = client.layer(table.url, token).object_id_field
    clauses = [table.where] if table.where else []
    if f["reportedAt"] in types:
        # UTC literals: the client shifts them for services keeping local times (time_zone.to_service).
        if start is not None:
            clauses.append(f"{f['reportedAt']} >= TIMESTAMP '{utc_text(start)}'")
        if end is not None:
            clauses.append(f"{f['reportedAt']} <= TIMESTAMP '{utc_text(end)}'")
    rows = client.query_all(table.url, " AND ".join(f"({c})" for c in clauses) or "1=1", token, geometry=False,
                            limit=limit, cut=True)
    out = []
    for r in rows:
        a = r.get("attributes") or {}
        out.append(LogRow(
            obs_id=_text(a.get(f["obsId"])), format=_text(a.get(f["report"])), text=_text(a.get(f["text"])),
            reported_at=ms_of(a.get(f["reportedAt"])), reported_by=_text(a.get(f["reportedBy"])), batch=_text(a.get(f["batch"])),
            oid=int(a[oid]) if is_number(a.get(oid)) else None,
        ))
    return sorted(out, key=lambda x: -(x.reported_at or 0))


def remove_log(client: EsriClient, settings: AlertSettings, rows: Sequence[LogRow], token: Optional[str] = None) -> int:
    """Deletes log rows (so their records can be reported again); how many were deleted."""
    table = settings.table
    ids = [r.oid for r in rows if r.oid is not None]
    if table is None or not ids:
        return 0
    results = client.delete_features(table.url, ids, token or table.token)
    failed = [r for r in results if not r.get("success")]
    if failed:
        raise EsriError(f"通報記録の削除: {len(failed)} 件失敗（{(failed[0].get('error') or {}).get('description') or ''}）")
    return len(ids)


# ---- choosing ---------------------------------------------------------------


def inside_rings(lon: float, lat: float, rings: Sequence) -> bool:
    """Whether a point is inside polygon rings (even-odd: holes are outside)."""
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def at_least(label: str, least: str, levels: Sequence[ConfidenceLevel]) -> bool:
    """Whether 確度 `label` is `least` or surer (levels surest first); any when `least` is ''."""
    if not least:
        return True
    order = [c.label for c in levels]
    if least not in order:
        return label == least
    return label in order and order.index(label) <= order.index(least)


def rule_matches(rule: AlertRule, o: Observation, areas: list[str], levels: Sequence[ConfidenceLevel]) -> bool:
    """Whether a record meets a rule (its class and below, activity, least 確度, geofence)."""
    if rule.kind and not within(o.kind, rule.kind):
        return False
    if rule.activities and o.activity not in rule.activities:
        return False
    if not at_least(o.confidence, rule.min_confidence, levels):
        return False
    if rule.area == "*":
        return bool(areas)
    return not rule.area or rule.area in areas


def select(
    records: Iterable[Observation],
    settings: AlertSettings,
    rules: Optional[list[AlertRule]] = None,
    fences: Sequence[Geofence] = (),
    levels: Sequence[ConfidenceLevel] = (),
    reported: Iterable[str] = (),
    start: Optional[float] = None,
    end: Optional[float] = None,
    by: str = "created",
) -> list[AlertItem]:
    """
    The notable records: living ones of the period (`by` 登録日時 `created`
    or 撮像日時 `imageTime`), meeting the fixed conditions and one
    of the rules (every record when there is no rules table: `rules` None),
    and not reported yet; in image time order.
    """
    done = set(reported)
    out = []
    for o in records:
        if o.deleted_at is not None or o.id in done:
            continue
        if settings.present_only and not o.present:
            continue
        if not at_least(o.confidence, settings.min_confidence, levels):
            continue
        t = o.image_time if by == "imageTime" else o.created_at
        if (start is not None or end is not None) and t is None:
            continue
        if (start is not None and t < start) or (end is not None and t > end):
            continue
        areas = list(dict.fromkeys(g.name for g in fences if inside_rings(o.lon, o.lat, g.rings)))
        if rules is None:
            out.append(AlertItem(o, areas, []))
            continue
        met = [r for r in rules if rule_matches(r, o, areas, levels)]
        if met:
            out.append(AlertItem(o, areas, list(dict.fromkeys(r.note for r in met if r.note))))
    return sorted(out, key=lambda i: (i.record.image_time if i.record.image_time is not None else i.record.created_at, i.record.created_at))


# ---- writing ---------------------------------------------------------------


@dataclass
class Moment:
    """When and by whom a report is made."""
    now: float
    user: str = ""
    count: int = 0


def raw_value(f: AlertField, item: AlertItem, index: int, report: AlertFormat, at: Moment) -> Any:
    """A field's value before it is written (times ms, numbers float)."""
    o = item.record
    s = f.source
    if s == "kind":
        path = path_of(o.kind)
        if f.level is None:
            return kind_of(path)
        i = f.level - 1 if f.level > 0 else len(path) + f.level
        return path[i] if 0 <= i < len(path) else ""
    return {
        "id": o.id, "imageId": o.image_id, "imageName": o.image_name, "imageTime": o.image_time, "objectId": o.object_id,
        "objectName": o.object_name, "activity": o.activity, "present": o.present, "confidence": o.confidence,
        "note": o.note, "lon": o.lon, "lat": o.lat, "length": o.length, "heading": o.heading, "width": o.width,
        "recordedBy": o.recorded_by, "updatedBy": o.updated_by, "created": o.created_at, "updated": o.updated_at,
        "area": "・".join(item.areas), "rule": "・".join(item.rules), "report": report.name, "reportedAt": at.now,
        "reportedBy": at.user, "index": index + 1, "count": at.count,
    }.get(s or "")


def written(f: AlertField, item: AlertItem, index: int, report: AlertFormat, at: Moment) -> str:
    """A field as text."""
    if f.source is None:
        return f.text or ""
    v = raw_value(f, item, index, report, at)
    if f.source in TIME_SOURCES:
        text = "" if v is None else js_string(format_time(v, f.format or report.date_format, report.time_zone))
    elif f.source in NUMBER_SOURCES:
        text = "" if v is None or not math.isfinite(v) else (to_fixed(v, f.digits) if f.digits is not None else js_string(v))
    elif f.source == "present":
        text = "あり" if v else "なし"
    else:
        text = "" if v is None else js_string(v)
    if f.map:
        key = js_string(v).lower() if isinstance(v, bool) else text
        text = f.map.get(key, f.map.get(text, text))
    return text if text != "" else f.empty


def escaped(text: str, report: AlertFormat) -> str:
    """A value without the delimiter or line breaks in it."""
    text = re.sub(r"\r\n|\r|\n", report.escape, text)
    return text.replace(report.delimiter, report.escape) if report.delimiter else text


def line_of(item: AlertItem, index: int, report: AlertFormat, at: Moment) -> str:
    """A record's line."""
    return report.delimiter.join(escaped(written(f, item, index, report, at), report) for f in report.fields)


_PLACE = re.compile(r"\{(\w+)(?::([^}]*))?\}")


def fill(template: str, report: AlertFormat, at: Moment, extra: Optional[dict[str, str]] = None) -> str:
    """
    A header, footer or file path with its places filled: `{name}`,
    `{count}`, `{user}`, `{date}` (now by dateFormat), `{now:%Y%m%d}` (now
    by its own format); unknown places stay as written.
    """
    def one(m: re.Match) -> str:
        key, fmt = m.group(1), m.group(2)
        if key in ("now", "date"):
            return js_string(format_time(at.now, fmt or report.date_format, report.time_zone))
        values = {"name": report.name, "count": str(at.count), "user": at.user, **(extra or {})}
        return values[key] if key in values else m.group(0)

    return _PLACE.sub(one, template)


def report_text(lines: Sequence[str], report: AlertFormat, at: Moment) -> str:
    """The whole text: the header, a line per record, the footer (each ending in the report's newline)."""
    parts = []
    if report.header:
        parts += fill(report.header, report, at).split("\n")
    parts += list(lines)
    if report.footer:
        parts += fill(report.footer, report, at).split("\n")
    return "".join(p + report.newline for p in parts)


def build_lines(items: Sequence[AlertItem], report: AlertFormat, at: Moment) -> list[str]:
    """Each item's line."""
    return [line_of(item, i, report, at) for i, item in enumerate(items)]


def write_log(client: EsriClient, settings: AlertSettings, report: AlertFormat, items: Sequence[AlertItem], lines: Sequence[str], at: Moment,
              token: Optional[str] = None) -> int:
    """Adds a row per record to the Esri log table; the rows added (an EsriError if any failed)."""
    table = settings.table
    if table is None or not items:
        return 0
    types = _log_types(client, settings, token)
    token = token or table.token
    f = table.fields
    batch = str(uuid.uuid4())
    adds = []
    for i, (item, line) in enumerate(zip(items, lines)):
        values: dict[str, Any] = {
            f["obsId"]: item.record.id, f["report"]: report.name, f["text"]: line, f["reportedAt"]: int(at.now),
            f["reportedBy"]: at.user, f["batch"]: batch,
        }
        for attr, col in report.columns.items():
            raw = raw_value(col, item, i, report, at) if col.source else col.text
            values[attr] = written(col, item, i, report, at) if types.get(attr) == "esriFieldTypeString" else raw
        adds.append({"attributes": {k: fit_value(v, types[k]) for k, v in values.items() if k in types}})
    added = 0
    failures: list[str] = []
    for start in range(0, len(adds), 200):
        result = client.apply_edits(table.url, adds[start:start + 200], token=token)
        for r in result.add_results:
            if r.get("success"):
                added += 1
            else:
                failures.append((r.get("error") or {}).get("description") or "追加失敗")
    if failures:
        raise EsriError(f"通報テーブルへの追加: {added} 件成功・{len(failures)} 件失敗（{failures[0]}）")
    return added


def file_path(report: AlertFormat, at: Moment) -> str:
    """The file the text goes to, its places filled."""
    return fill(report.file.path, report, at) if report.file else ""


def write_file(text: str, report: AlertFormat, at: Moment, opener: Callable = open) -> str:
    """Writes (or appends) the text to the report's file; its path."""
    if report.file is None:
        return ""
    path = file_path(report, at)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with opener(path, "a" if report.file.append else "w", encoding=report.file.encoding, newline="") as out:
        out.write(text)
    return path

