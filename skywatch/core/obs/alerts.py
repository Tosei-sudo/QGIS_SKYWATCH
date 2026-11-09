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

Each record picked gets an importance score (0–100, `score`): the weighted
mean of its 確度 (where it stands among the levels), its place (the
heaviest geofence it is in, a WEIGHT column of the layer), the rule it met
(the rules table's WEIGHT) and its rarity (fewer records of its class near
it in the days before, the rarer). The lines go out highest score first.
The weights and the rarity's window are `alertReports.score`; the
per-place and per-rule weights are Esri data.

A record can also be named by where it is: an optional polygon layer of
named areas (`namedAreas`: place and facility names, maybe a kind). The
smallest area holding the record is its place (`place`, `placeKind`; all of
them smallest first `places`); a field can say what to write when it is in
none (`empty`), or leave itself out (`omitEmpty`), and add text around a
value only when there is one (`prefix`, `suffix`).
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError, fit_value, quote
from ..time_zone import UTC, TimeZone, parse_time_zone
from ..util import encodable, is_name, is_number, is_record, js_string, layer_url, ms_of, utc_text
from ..request.export import format_time
from .geo import get_distance, to_fixed
from .model import ConfidenceLevel, Observation
from .taxonomy import kind_of, path_of, within

#: What a field can take (`source`).
SOURCES = (
    "id", "imageId", "imageName", "imageTime", "objectId", "objectName", "kind", "activity", "present", "confidence", "note",
    "lon", "lat", "length", "heading", "width", "recordedBy", "updatedBy", "created", "updated",
    "area", "rule", "manual", "report", "reportedAt", "reportedBy", "index", "count", "score", "similar",
    "place", "placeKind", "places",
)
TIME_SOURCES = ("imageTime", "created", "updated", "reportedAt")
NUMBER_SOURCES = ("lon", "lat", "length", "heading", "width", "score")
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
    "active": "ACTIVE", "note": "NOTE", "weight": "WEIGHT",
}
#: The geofence layer's attributes by role.
DEFAULT_FENCE_FIELDS = {"name": "NAME", "active": "ACTIVE", "weight": "WEIGHT"}
#: The named area layer's attributes by role (place and facility names).
DEFAULT_PLACE_FIELDS = {"name": "NAME", "kind": "KIND", "active": "ACTIVE"}
#: How long the named areas read are kept (seconds).
PLACE_CACHE_SECONDS = 600
#: The parts of the score and their weights when the settings give none.
DEFAULT_WEIGHTS = {"confidence": 1.0, "area": 1.0, "rule": 1.0, "rarity": 1.0}
SCORE_PARTS = {"confidence": "確度", "area": "場所", "rule": "条件", "rarity": "珍しさ"}
#: The log table's attributes by role.
DEFAULT_LOG_FIELDS = {
    "obsId": "OBS_ID", "report": "REPORT_NAME", "text": "TEXT", "reportedAt": "REPORTED_AT", "reportedBy": "REPORTED_BY",
    "batch": "BATCH_ID", "manual": "MANUAL",
}
#: What `rule` says of a record added by hand (outside the conditions).
MANUAL_RULE = "手動"
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
    #: Times: the zone written in; the report's timeZone when None.
    time_zone: Optional[TimeZone] = None
    #: Numbers: decimals.
    digits: Optional[int] = None
    #: `kind`: the depth (1 the top, -1 the last); the whole path when None.
    level: Optional[int] = None
    #: Values replaced (`{"true": "在", "false": "不在"}`), compared as text.
    map: dict[str, str] = field(default_factory=dict)
    #: Written when the value is empty.
    empty: str = ""
    #: Written before / after the value, only when there is one.
    prefix: str = ""
    suffix: str = ""
    #: In a line, left out (with its delimiter) when empty.
    omit_empty: bool = False


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
class ScoreSettings:
    """`alertReports.score`: how the importance score is made."""
    #: Each part's weight (0 leaves it out).
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    #: Rarity: the records of the same class (to `level`; the whole path when None) within
    #: `radius_km` (0 anywhere) in the `days` before a record.
    days: float = 30.0
    radius_km: float = 50.0
    level: Optional[int] = None
    #: Records picked by the conditions below this are left out (those added by hand stay).
    min_score: float = 0.0


@dataclass
class LiveSettings:
    """`alertReports.live`: the 通報 tab of 通知, the log table read again every `seconds`."""
    #: How often the log table is asked for changes (0: only by hand).
    seconds: float = 10.0
    #: The rows of the last `days` days are shown (by REPORTED_AT; the newest rows when the table has none).
    days: float = 7.0


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
    #: Place and facility names (polygons): a record's place is the smallest one it is in.
    named_areas: Optional[EsriSource] = None
    #: The Esri log table (the main output; also what was reported).
    table: Optional[EsriSource] = None
    #: A record saved that meets the conditions (and is not reported yet) is said in QGIS's message bar.
    notify_on_save: bool = True
    #: The importance score (None: no score, lines in image time order).
    score: Optional[ScoreSettings] = field(default_factory=ScoreSettings)
    #: The 通報 tab of 通知 (polled while the dock is shown).
    live: LiveSettings = field(default_factory=LiveSettings)

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
    #: How much meeting it weighs (0–1, the WEIGHT column; 1 when empty).
    weight: float = 1.0


@dataclass
class Geofence:
    """A polygon of the geofence layer (WGS 84 rings)."""
    name: str
    rings: list
    #: How much being inside weighs (0–1, the WEIGHT column; 1 when empty).
    weight: float = 1.0


@dataclass
class NamedArea:
    """A polygon of the named area layer (WGS 84 rings) and its size."""
    name: str
    rings: list
    kind: str = ""
    #: The size (km², roughly: the rings on a plane scaled by the latitude), the smaller the more precise.
    size: float = 0.0
    #: The rings' extent (west, south, east, north), to skip the far ones fast.
    box: tuple = (0.0, 0.0, 0.0, 0.0)


@dataclass
class AlertItem:
    """A notable record, the geofences it is in and the rules it met."""
    record: Observation
    areas: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    #: Added by hand (whether or not it meets the conditions).
    manual: bool = False
    #: The importance score (0–100; None when not scored) and its parts (0–1 each).
    score: Optional[float] = None
    parts: dict[str, float] = field(default_factory=dict)
    #: The records of the same class near it in the days before (for the rarity).
    similar: int = 0
    #: The heaviest geofence and rule it met (0–1; None when there are none to meet).
    area_weight: Optional[float] = None
    rule_weight: Optional[float] = None
    #: The named areas it is in, smallest first (the first is its place).
    places: list[NamedArea] = field(default_factory=list)


def manual_items(records: Iterable[Observation], fences: Sequence[Geofence] = (), settings: Optional[AlertSettings] = None,
                 levels: Sequence[ConfidenceLevel] = (), history: Iterable[Observation] = (),
                 named: Sequence["NamedArea"] = ()) -> list[AlertItem]:
    """Records chosen by hand: reported whatever the conditions say (their geofences and places still named; scored by `settings`)."""
    out = []
    for o in records:
        if o.deleted_at is not None:
            continue
        inside = [g for g in fences if inside_rings(o.lon, o.lat, g.rings)]
        out.append(AlertItem(o, list(dict.fromkeys(g.name for g in inside)), [MANUAL_RULE], True,
                             area_weight=max((g.weight for g in inside), default=0.0) if fences else None, rule_weight=1.0))
    name_places(out, named)
    if settings is not None and settings.score is not None:
        score_items(out, settings.score, levels, history)
    return out


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
    zone = None
    if "timeZone" in v and v["timeZone"] not in (None, ""):
        zone = parse_time_zone(v["timeZone"])
        if zone is None:
            problems.append(f"{at} の timeZone は Asia/Tokyo・+09:00・UTC のように指定")
        elif source not in TIME_SOURCES:
            problems.append(f"{at} の timeZone は日時の項目（{'・'.join(TIME_SOURCES)}）だけに指定")
            zone = None
    return AlertField(
        source=source,
        format=fmt if isinstance(fmt, str) and fmt else None,
        time_zone=zone,
        digits=int(digits) if is_number(digits) and 0 <= digits <= 12 else None,
        level=int(level) if is_number(level) and int(level) == level and level != 0 else None,
        map={str(k): js_string(x) for k, x in mapping.items()} if is_record(mapping) else {},
        empty=js_string(v["empty"]) if "empty" in v and v["empty"] is not None else "",
        prefix=_affix(v, "prefix", at, problems),
        suffix=_affix(v, "suffix", at, problems),
        omit_empty=_bool(v, "omitEmpty", at, problems, False),
    )


def _affix(v: dict, key: str, at: str, problems: list[str]) -> str:
    if key not in v or v[key] is None:
        return ""
    if isinstance(v[key], str):
        return v[key]
    problems.append(f"{at}.{key} は文字列で指定")
    return ""


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


#: An Esri layer or table of the settings file (URL, or { url, fields, where, token }); also used by other settings.
esri_source_of = _source_of


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
    _check_places(fmt.header, f"{at}.header", problems)
    _check_places(fmt.footer, f"{at}.footer", problems)
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
            _check_places(fmt.file.path, f"{at}.file", problems)
    return fmt


def _check_places(template: str, at: str, problems: list[str]) -> None:
    """The time zones of a header's, footer's or path's `{now:…@zone}` places."""
    for m in _PLACE.finditer(template):
        _, zone = _place_parts(m.group(2))
        if m.group(1) in ("now", "date") and zone is not None and parse_time_zone(zone) is None:
            problems.append(f"{at} の {m.group(0)} のタイムゾーン「{zone}」は Asia/Tokyo・+09:00・UTC のように指定")


def _number(v: dict, key: str, at: str, problems: list[str], default: float, low: float = 0.0, high: float = math.inf) -> float:
    if key not in v:
        return default
    x = v[key]
    if is_number(x) and math.isfinite(x) and low <= x <= high:
        return float(x)
    problems.append(f"{at}.{key} は {low:g}" + (f"〜{high:g}" if math.isfinite(high) else " 以上") + " の数で指定")
    return default


def score_settings_of(value: Any, at: str, problems: list[str]) -> Optional[ScoreSettings]:
    """`alertReports.score`: false for no score, else { weights, rarity: { days, radiusKm, level }, minScore }."""
    if value is False:
        return None
    score = ScoreSettings()
    if value is True or value is None:
        return score
    if not is_record(value):
        problems.append(f"{at} は false か {{ weights, rarity, minScore }} で指定")
        return score
    weights = value.get("weights")
    if "weights" in value and not is_record(weights):
        problems.append(f"{at}.weights は {{ {'・'.join(DEFAULT_WEIGHTS)}: 重み }} で指定")
    for k, w in (weights if is_record(weights) else {}).items():
        if k not in DEFAULT_WEIGHTS:
            problems.append(f"{at}.weights.{k} は使用不可（{'・'.join(DEFAULT_WEIGHTS)}）")
        elif is_number(w) and math.isfinite(w) and w >= 0:
            score.weights[k] = float(w)
        else:
            problems.append(f"{at}.weights.{k} は 0 以上の数で指定")
    rarity = value.get("rarity")
    if "rarity" in value and not is_record(rarity):
        problems.append(f"{at}.rarity は {{ days, radiusKm, level }} で指定")
    if is_record(rarity):
        score.days = _number(rarity, "days", f"{at}.rarity", problems, score.days, 0.0)
        score.radius_km = _number(rarity, "radiusKm", f"{at}.rarity", problems, score.radius_km, 0.0)
        if "level" in rarity:
            level = rarity["level"]
            if is_number(level) and int(level) == level and level != 0:
                score.level = int(level)
            elif level is not None:
                problems.append(f"{at}.rarity.level は 1（最上位）からの階層、または -1（最下位）で指定")
    score.min_score = _number(value, "minScore", at, problems, 0.0, 0.0, 100.0)
    return score


#: The shortest polling interval (s): a shorter one would only load the service.
MIN_LIVE_SECONDS = 2.0


def live_settings_of(value: Any, at: str, problems: list[str]) -> LiveSettings:
    """`alertReports.live`: false (by hand only), seconds, or { seconds, days }."""
    live = LiveSettings()
    if value is False or value == 0:
        live.seconds = 0.0
        return live
    if value is True or value is None:
        return live
    if is_number(value) and not isinstance(value, bool):
        value = {"seconds": value}
    if not is_record(value):
        problems.append(f"{at} は false か秒数か {{ seconds, days }} で指定")
        return live
    seconds = _number(value, "seconds", at, problems, live.seconds, 0.0)
    live.seconds = 0.0 if seconds == 0 else max(MIN_LIVE_SECONDS, seconds)
    live.days = _number(value, "days", at, problems, live.days, 0.0)
    return live


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
    settings.notify_on_save = _bool(value, "notifyOnSave", at, problems, True)
    if "live" in value:
        settings.live = live_settings_of(value["live"], f"{at}.live", problems)
    if "score" in value:
        settings.score = score_settings_of(value["score"], f"{at}.score", problems)
    if "rules" in value:
        settings.rules = _source_of(value["rules"], f"{at}.rules", DEFAULT_RULE_FIELDS, problems)
    if "geofences" in value:
        settings.geofences = _source_of(value["geofences"], f"{at}.geofences", DEFAULT_FENCE_FIELDS, problems)
    if "namedAreas" in value and value["namedAreas"] not in (None, False, ""):
        settings.named_areas = _source_of(value["namedAreas"], f"{at}.namedAreas", DEFAULT_PLACE_FIELDS, problems)
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


def weight_of(v: Any) -> float:
    """A WEIGHT value (0–1, clipped; a percent when over 1 and up to 100); 1 when empty or not a number."""
    if isinstance(v, str):
        try:
            v = float(v.strip().rstrip("%")) if v.strip() else None
        except ValueError:
            v = None
    if not is_number(v) or not math.isfinite(v):
        return 1.0
    v = float(v)
    if v > 1:
        v = v / 100
    return min(1.0, max(0.0, v))


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
            weight=weight_of(a.get(fields["weight"])),
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
        out.append(Geofence(name=_text(a.get(f["name"])), rings=rings, weight=weight_of(a.get(f["weight"]))))
    return out


def ring_size(rings: Sequence) -> float:
    """
    Roughly how large polygon rings are (km²): each ring on a plane, the
    longitudes scaled by the latitude's cosine; holes (the other winding) take off.
    """
    points = [p for ring in rings for p in ring]
    if not points:
        return 0.0
    lat0 = math.radians(sum(p[1] for p in points) / len(points))
    kx, ky = 111.32 * math.cos(lat0), 110.57
    total = 0.0
    for ring in rings:
        n = len(ring)
        total += sum(ring[i][0] * ring[(i + 1) % n][1] - ring[(i + 1) % n][0] * ring[i][1] for i in range(n)) / 2
    return abs(total) * kx * ky


def named_areas_of_features(features: Iterable[dict], fields: dict[str, str]) -> list[NamedArea]:
    """The active named areas of the layer's features (polygons, WGS 84); those without a name or rings left out."""
    out = []
    for feature in features:
        a = feature.get("attributes") or {}
        rings = (feature.get("geometry") or {}).get("rings") or []
        name = _text(a.get(fields["name"]))
        if not rings or not name or (fields["active"] in a and not _active(a.get(fields["active"]))):
            continue
        xs = [p[0] for ring in rings for p in ring]
        ys = [p[1] for ring in rings for p in ring]
        out.append(NamedArea(name=name, rings=rings, kind=_text(a.get(fields["kind"])), size=ring_size(rings),
                             box=(min(xs), min(ys), max(xs), max(ys))))
    return out


_place_cache: dict[tuple, tuple[float, list[NamedArea]]] = {}
_place_lock = threading.Lock()


def load_named_areas(client: EsriClient, source: Optional[EsriSource], token: Optional[str] = None,
                     max_age: float = PLACE_CACHE_SECONDS, clock: Callable[[], float] = time.monotonic) -> list[NamedArea]:
    """
    The active named areas (polygons, WGS 84); read once and kept `max_age`
    seconds (they change seldom; every report and save asks for them).
    """
    if source is None:
        return []
    key = (source.url, source.where, tuple(sorted(source.fields.items())))
    now = clock()
    with _place_lock:
        hit = _place_cache.get(key)
    if hit is not None and now - hit[0] < max_age:
        return hit[1]
    rows = client.query_all(source.url, source.where or "1=1", token or source.token, limit=100_000)
    areas = named_areas_of_features(rows, source.fields)
    with _place_lock:
        _place_cache[key] = (now, areas)
    return areas


def forget_named_areas() -> None:
    """Drops the named areas kept (read again next time)."""
    with _place_lock:
        _place_cache.clear()


def places_of(lon: float, lat: float, areas: Sequence[NamedArea]) -> list[NamedArea]:
    """The named areas a point is in, smallest first (same size: the layer's order)."""
    inside = [a for a in areas
              if a.box[0] <= lon <= a.box[2] and a.box[1] <= lat <= a.box[3] and inside_rings(lon, lat, a.rings)]
    return sorted(inside, key=lambda a: a.size)


def name_places(items: Iterable[AlertItem], areas: Sequence[NamedArea]) -> None:
    """Sets each item's places (the named areas it is in, smallest first)."""
    for item in items:
        item.places = places_of(item.record.lon, item.record.lat, areas) if areas else []


def _log_types(client: EsriClient, settings: AlertSettings, token: Optional[str]) -> dict[str, str]:
    table = settings.table
    types = client.layer(table.url, token or table.token, refresh=True).field_types
    if table.fields["obsId"] not in types:
        raise EsriError(f"通報テーブルに属性 {table.fields['obsId']} なし")
    return types


def reported_ids(client: EsriClient, settings: AlertSettings, token: Optional[str] = None,
                 ids: Optional[Iterable[str]] = None) -> set[str]:
    """
    The ids of the records already reported (in any format, from the log
    table); with `ids`, only which of those (a few records just saved: the
    whole log is not read each time).
    """
    table = settings.table
    if table is None:
        return set()
    f = table.fields
    _log_types(client, settings, token)
    if ids is None:
        wheres = [table.where or "1=1"]
    else:
        wanted = sorted({i for i in ids if i})
        if not wanted:
            return set()
        wheres = [" AND ".join(f"({c})" for c in (table.where, f"{f['obsId']} IN ({', '.join(quote(i) for i in wanted[k:k + 100])})") if c)
                  for k in range(0, len(wanted), 100)]
    out: set[str] = set()
    for where in wheres:
        rows = client.query_all(table.url, where, token or table.token, out_fields=f["obsId"], geometry=False, limit=1_000_000)
        out |= {_text((r.get("attributes") or {}).get(f["obsId"])) for r in rows}
    return out - {""}


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


def _log_row(a: dict[str, Any], f: dict[str, str], oid: str) -> LogRow:
    return LogRow(
        obs_id=_text(a.get(f["obsId"])), format=_text(a.get(f["report"])), text=_text(a.get(f["text"])),
        reported_at=ms_of(a.get(f["reportedAt"])), reported_by=_text(a.get(f["reportedBy"])), batch=_text(a.get(f["batch"])),
        oid=int(a[oid]) if is_number(a.get(oid)) else None,
    )


@dataclass
class LogChanges:
    """What `poll_log` found: the rows added since, and the object ids no longer there."""
    added: list[LogRow]
    gone: set[int]
    #: Object ids of the rows now in the window (to pass as `known` next time).
    ids: set[int]


def poll_log(client: EsriClient, settings: AlertSettings, known: Iterable[int], since: Optional[float] = None,
             token: Optional[str] = None, limit: int = 2000) -> LogChanges:
    """
    The changes of the log table since a poll: one request for the object
    ids of the rows reported since `since` (all rows when None, or when the
    table has no time attribute), then the rows not `known` by their ids (so
    each poll moves only what changed; rows deleted are told by their ids).
    The first poll (nothing known) reads at most the `limit` newest rows.
    """
    table = settings.table
    if table is None:
        return LogChanges([], set(), set())
    token = token or table.token
    info = client.layer(table.url, token)
    types = info.field_types
    f = table.fields
    if f["obsId"] not in types:
        raise EsriError(f"通報テーブルに属性 {f['obsId']} なし")
    clauses = [table.where] if table.where else []
    if since is not None and f["reportedAt"] in types:
        clauses.append(f"{f['reportedAt']} >= TIMESTAMP '{utc_text(since)}'")
    where = " AND ".join(f"({c})" for c in clauses) or "1=1"
    answer = client.ask(f"{table.url}/query", {"where": where, "returnIdsOnly": "true"}, token)
    ids = {int(i) for i in answer.get("objectIds") or [] if is_number(i)}
    known = set(known)
    new = sorted(ids - known, reverse=True)[:limit]
    size = max(1, min(500, info.max_record_count or 500))
    added: list[LogRow] = []
    oid = info.object_id_field
    for k in range(0, len(new), size):
        chunk = new[k:k + size]
        rows = client.ask(f"{table.url}/query", {"objectIds": ",".join(str(i) for i in chunk), "outFields": "*",
                                                 "returnGeometry": "false"}, token).get("features") or []
        added += [_log_row(r.get("attributes") or {}, f, oid) for r in rows]
    added.sort(key=lambda x: (-(x.reported_at or 0), -(x.oid or 0)))
    return LogChanges(added, known - ids, ids)


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
    out = [_log_row(r.get("attributes") or {}, f, oid) for r in rows]
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
    history: Optional[Iterable[Observation]] = None,
    named: Sequence[NamedArea] = (),
) -> list[AlertItem]:
    """
    The notable records: living ones of the period (`by` 登録日時 `created`
    or 撮像日時 `imageTime`), meeting the fixed conditions and one
    of the rules (every record when there is no rules table: `rules` None),
    and not reported yet; highest score first (`settings.score`; the rarity
    counted among `history`, the records themselves when None), else in
    image time order. Each is given its places among the named areas `named`.
    """
    records = list(records)
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
        inside = [g for g in fences if inside_rings(o.lon, o.lat, g.rings)]
        areas = list(dict.fromkeys(g.name for g in inside))
        area_weight = max((g.weight for g in inside), default=0.0) if fences else None
        if rules is None:
            out.append(AlertItem(o, areas, [], area_weight=area_weight))
            continue
        met = [r for r in rules if rule_matches(r, o, areas, levels)]
        if met:
            out.append(AlertItem(o, areas, list(dict.fromkeys(r.note for r in met if r.note)), area_weight=area_weight,
                                 rule_weight=max(r.weight for r in met)))
    out.sort(key=_time_key)
    name_places(out, named)
    if settings.score is None:
        return out
    score_items(out, settings.score, levels, records if history is None else history)
    return ranked([i for i in out if i.score >= settings.score.min_score])


def _time_key(i: AlertItem) -> tuple:
    o = i.record
    return (o.image_time if o.image_time is not None else o.created_at, o.created_at)


def ranked(items: Iterable[AlertItem]) -> list[AlertItem]:
    """Highest score first (then image time order); the ones not scored last, in time order."""
    return sorted(items, key=lambda i: (i.score is None, -(i.score or 0.0), *_time_key(i)))


# ---- the importance score ----------------------------------------------------------


def confidence_part(label: str, levels: Sequence[ConfidenceLevel]) -> Optional[float]:
    """Where a 確度 stands among the levels (surest first): 1 the surest, 0 the least or unknown; None with no levels."""
    order = [c.label for c in levels]
    if not order:
        return None
    if label not in order:
        return 0.0
    return 1.0 if len(order) == 1 else 1 - order.index(label) / (len(order) - 1)


def _same_kind(path: list[str], level: Optional[int]) -> list[str]:
    if level is None:
        return path
    n = level if level > 0 else len(path) + level + 1
    return path[:max(1, n)]


def similar_count(o: Observation, history: Sequence[Observation], score: ScoreSettings) -> int:
    """The living records of the same class (to `score.level`) within the radius in the days before `o` (itself not counted)."""
    t = o.image_time if o.image_time is not None else o.created_at
    if t is None:
        return 0
    kind = _same_kind(path_of(o.kind), score.level)
    window = score.days * 86_400_000
    n = 0
    for x in history:
        if x.id == o.id or x.deleted_at is not None or not x.present:
            continue
        tx = x.image_time if x.image_time is not None else x.created_at
        if tx is None or tx > t or tx < t - window:
            continue
        if kind and path_of(x.kind)[:len(kind)] != kind:
            continue
        if score.radius_km > 0 and get_distance((o.lon, o.lat), (x.lon, x.lat)) > score.radius_km * 1000:
            continue
        n += 1
    return n


def score_items(items: Sequence[AlertItem], score: ScoreSettings, levels: Sequence[ConfidenceLevel] = (),
                history: Iterable[Observation] = ()) -> None:
    """
    Sets each item's score: 100 × the weighted mean of its parts (確度, place,
    rule, rarity = 1 / (1 + similar records)); a part with nothing to
    measure (no levels, no geofences, no rules table) is left out of the mean.
    """
    history = list(history)
    for item in items:
        item.similar = similar_count(item.record, history, score) if score.weights.get("rarity", 0) > 0 else 0
        parts = {
            "confidence": confidence_part(item.record.confidence, levels),
            "area": item.area_weight,
            "rule": item.rule_weight,
            "rarity": 1 / (1 + item.similar),
        }
        item.parts = {k: v for k, v in parts.items() if v is not None}
        total = sum(score.weights.get(k, 0.0) for k in item.parts)
        item.score = round(100 * sum(score.weights.get(k, 0.0) * v for k, v in item.parts.items()) / total, 1) if total > 0 else 0.0


def score_note(item: AlertItem) -> str:
    """The score's parts as text (確度 100・場所 50・条件 100・珍しさ 33（類似 2 件）)."""
    if item.score is None:
        return ""
    text = "・".join(f"{SCORE_PARTS[k]} {round(v * 100)}" for k, v in item.parts.items())
    return text + (f"（類似 {item.similar} 件）" if "rarity" in item.parts else "")


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
        "reportedBy": at.user, "index": index + 1, "count": at.count, "manual": item.manual, "score": item.score,
        "similar": item.similar if item.score is not None else None,
        "place": item.places[0].name if item.places else "", "placeKind": item.places[0].kind if item.places else "",
        "places": "・".join(dict.fromkeys(a.name for a in item.places)),
    }.get(s or "")


def written(f: AlertField, item: AlertItem, index: int, report: AlertFormat, at: Moment) -> str:
    """A field as text."""
    if f.source is None:
        return f.text or ""
    v = raw_value(f, item, index, report, at)
    if f.source in TIME_SOURCES:
        text = "" if v is None else js_string(format_time(v, f.format or report.date_format, f.time_zone or report.time_zone))
    elif f.source in NUMBER_SOURCES:
        text = "" if v is None or not math.isfinite(v) else (to_fixed(v, f.digits) if f.digits is not None else js_string(v))
    elif f.source == "present":
        text = "あり" if v else "なし"
    elif f.source == "manual":
        text = MANUAL_RULE if v else ""
    else:
        text = "" if v is None else js_string(v)
    if f.map:
        key = js_string(v).lower() if isinstance(v, bool) else text
        text = f.map.get(key, f.map.get(text, text))
    return f.prefix + text + f.suffix if text != "" else f.empty


def escaped(text: str, report: AlertFormat) -> str:
    """A value without the delimiter or line breaks in it."""
    text = re.sub(r"\r\n|\r|\n", report.escape, text)
    return text.replace(report.delimiter, report.escape) if report.delimiter else text


def line_of(item: AlertItem, index: int, report: AlertFormat, at: Moment) -> str:
    """A record's line (the fields `omitEmpty` that are empty left out, with their delimiter)."""
    values = ((f, written(f, item, index, report, at)) for f in report.fields)
    return report.delimiter.join(escaped(text, report) for f, text in values if text != "" or not f.omit_empty)


_PLACE = re.compile(r"\{(\w+)([:@][^}]*)?\}")


def _place_parts(rest: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """A place's format and time zone from what follows its name: `:format`, `@zone`, `:format@zone`."""
    if not rest:
        return None, None
    if rest[0] == "@":
        return None, rest[1:].strip()
    body = rest[1:]
    if "@" in body:
        fmt, zone = body.rsplit("@", 1)
        return fmt or None, zone.strip()
    return body or None, None


def fill(template: str, report: AlertFormat, at: Moment, extra: Optional[dict[str, str]] = None) -> str:
    """
    A header, footer or file path with its places filled: `{name}`,
    `{count}`, `{user}`, `{date}` (now by dateFormat), `{now:%Y%m%d}` (now
    by its own format), in the report's time zone or the one after `@`
    (`{now:%H:%M@UTC}`, `{date@+09:00}`); unknown places stay as written.
    """
    def one(m: re.Match) -> str:
        key = m.group(1)
        fmt, zone_text = _place_parts(m.group(2))
        if key in ("now", "date"):
            zone = report.time_zone if zone_text is None else parse_time_zone(zone_text)
            if zone is None:
                return m.group(0)
            return js_string(format_time(at.now, fmt or report.date_format, zone))
        values = {"name": report.name, "count": str(at.count), "user": at.user, **(extra or {})}
        return values[key] if key in values and not (m.group(2) or "").startswith("@") else m.group(0)

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
            f["reportedBy"]: at.user, f["batch"]: batch, f["manual"]: 1 if item.manual else 0,
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


def write_file(text: str, report: AlertFormat, at: Moment, opener: Callable = open) -> tuple[str, bool]:
    """
    Writes (or appends) the text to the report's file: its path, and True when
    characters the file's encoding cannot hold (cp932: —, é, emoji…) became ?.
    """
    if report.file is None:
        return "", False
    path = file_path(report, at)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    text, replaced = encodable(text, report.file.encoding)
    with opener(path, "a" if report.file.append else "w", encoding=report.file.encoding, newline="") as out:
        out.write(text)
    return path, replaced

