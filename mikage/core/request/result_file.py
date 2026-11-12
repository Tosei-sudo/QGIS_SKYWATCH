"""
A satellite system's answer file (採否の結果ファイル, `requestExports[].resultFile`):
which of the requests handed out it took into its plan (採用), which not
(不採用) or not yet (不明), and the plans it made that were not asked for
(要求外, e.g. its routine tasks).

Its form is as free as the order files' (tsudo, 2026-11-10: 「オーダーファイル
みたいに様式をフルカスタムできると嬉しい」):

- CSV (or tab, `;`, `|` separated text): `columns`, each a column of the file
  by its heading (`key`) or its place (`column`, 1 = the left one; files
  without headings take the columns in order), and what is in it (`target`,
  TARGETS). Lines before the headings are skipped by `skipLines`.
- JSON: the same `columns` with dot paths (`request.id`) in the records at
  `records`, or the system's JSON Schema whose properties say what they are
  with `x-target` (the array of `x-target: records` is the list of records,
  properties outside it are for every record); the file is checked against it.

Each value is read through the column's `pattern` (a regular expression: its
first group, or the match), `map` (the file's value → the plugin's: `OK` →
`採用`; `""` stands for an empty cell, `*` for any other value), `default`
(when empty) and `format` (times: iso, iso-date, unix, unixms or strptime).
`only` / `skip` keep or drop rows by a column's value (as in the file).
Columns of one target are joined by a space (a date and a time in two columns:
`plannedAt` twice, `format` `%Y%m%d %H%M`). A fixed `value` stands for every row.

    "resultFile": {
      "format": "csv", "encoding": "cp932", "delimiter": ",", "header": true, "skipLines": 0,
      "columns": [
        {"key": "REQ_ID", "target": "id"},
        {"key": "STATUS", "target": "result", "map": {"OK": "採用", "NG": "不採用", "HOLD": "不明"}},
        {"key": "PLAN_DATE", "target": "plannedAt"}, {"key": "PLAN_TIME", "target": "plannedAt", "format": "%Y%m%d %H%M"},
        {"key": "FOOTPRINT", "target": "geometry", "geometry": "wkt"},
        {"key": "KIND", "only": ["PLAN"]}
      ],
      "missing": "不採用", "sizeKm": 10
    }

The first (2026-11-09) form of one name per item (`key`, `keySource`,
`result`, `adopted`, `rejected`, `reason`, `plannedAt`, `timeFormat`,
`satellite`, `name`, `externalId`, `geometry`) is read as those columns.

A column of target `linkId` holds the 外部連携ID the order file gave the row
(export source `linkId`, core/request/link_id.py; through a `pattern` when
the system writes it inside a name, `SW-7K3Q…`): such a row answers the very
history row of that id, found among the system's rows of the day read and of
the `linkDays` days before it (default 7; and the day after), the nearest
day first (an id is not unique for ever). A row whose id is not found is
matched by its request's keys as before. A file may name its rows by the id
alone.

Without a `result` column every row listed is 採用 (and, unless `missing`
says otherwise, a request handed out but not listed is 不採用; with one, it
is left 不明). Times without a zone are the system's `timeZone`.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional, Sequence

from ..time_zone import UTC, TimeZone, instant_of, read_text_instant
from ..util import is_number, is_record, parse_iso
from .link_id import DEFAULT_LINK_DAYS
from .schema import resolve, validate

ADOPTED, REJECTED, WAITING = "採用", "不採用", "不明"
ANSWERS = (ADOPTED, REJECTED, WAITING)
#: What a request's key in the file is: its id, 管理番号 or 識別番号 (tried in this order).
KEY_SOURCES = ("id", "managementNo", "identificationNo")
#: What a file's row is matched by: the 外部連携ID of the row it answers (tried first), or its request's keys.
LINK_ID = "linkId"
MATCH_KEYS = (LINK_ID,) + KEY_SOURCES
#: The most days before the day read that an id is looked for in (`linkDays`).
MAX_LINK_DAYS = 60
#: What a column (or a schema's property, `x-target`) can be.
TARGETS = (
    "id", "managementNo", "identificationNo", "linkId", "result", "reason", "plannedAt", "satellite", "name", "externalId",
    "geometry", "centerLat", "centerLon", "bboxWest", "bboxSouth", "bboxEast", "bboxNorth", "day", "system",
)
#: Targets whose columns are joined by a space (a date and a time in two columns).
JOINED = ("plannedAt", "day", "reason", "name")
NUMBER_TARGETS = ("centerLat", "centerLon", "bboxWest", "bboxSouth", "bboxEast", "bboxNorth")
#: How a `geometry` column is written (the order files' kinds, §8.4); WKT and GeoJSON are known by themselves.
GEOMETRIES = ("wkt", "geojson", "bbox", "center", "centerRadius", "vertices", "centerGeojson")
FORMATS = ("csv", "json")
#: What a `missing` request (handed out, not in the file) becomes.
MISSING = (REJECTED, WAITING)
DEFAULT_ADOPTED = ("採用", "OK", "○", "〇", "1", "TRUE", "YES", "Y", "ACCEPTED", "ADOPTED", "PLANNED")
DEFAULT_REJECTED = ("不採用", "NG", "×", "✕", "0", "FALSE", "NO", "N", "REJECTED", "DECLINED")
DEFAULT_WAITING = ("不明", "未定", "保留", "回答待ち", "PENDING", "WAITING", "UNKNOWN", "TBD", "HOLD")
#: The keys of the first form (one name per item), read as columns.
LEGACY_KEYS = ("key", "keySource", "result", "adopted", "rejected", "reason", "plannedAt", "timeFormat", "satellite", "name",
               "externalId", "geometry")
LEGACY_GEOMETRY = ("wkt", "geojson", "lon", "bbox")
ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "utf-8-sig": "utf-8-sig", "utf-8-bom": "utf-8-sig",
             "cp932": "cp932", "shift_jis": "cp932", "sjis": "cp932", "shift-jis": "cp932",
             "euc-jp": "euc-jp", "eucjp": "euc-jp", "utf-16": "utf-16", "utf16": "utf-16", "unicode": "utf-16"}
DELIMITERS = {",": ",", "\t": "\t", "tab": "\t", "\\t": "\t", ";": ";", "|": "|", " ": " ", "space": " "}
_SCHEMA_KEYS = ("x-target", "x-format", "x-map", "x-pattern", "x-geometry", "x-only", "x-skip")


@dataclass
class ResultColumn:
    """One column of a file (or a property of its schema): where it is, what it is, how its value is read."""
    #: The heading (CSV) or the dot path (JSON); a name only, for a file without headings.
    key: str = ""
    #: Its place (1 = the left one), instead of the heading.
    position: Optional[int] = None
    target: Optional[str] = None
    #: A fixed value for every row (nothing read).
    fixed: bool = False
    value: Any = None
    #: Times: iso (the default, lenient), iso-date, unix, unixms or a strptime format.
    format: str = ""
    #: The file's value (upper case, trimmed; "" empty, "*" any other) → the plugin's.
    map: dict = field(default_factory=dict)
    pattern: Optional[re.Pattern] = None
    geometry: str = "wkt"
    default: Any = None
    #: Rows kept only when the file's value is one of these / dropped when it is one of these (upper case).
    only: Optional[tuple] = None
    skip: Optional[tuple] = None


@dataclass
class ResultFile:
    """How one system's answer file is read."""
    format: str = "csv"
    encoding: str = "utf-8"
    #: JSON with columns: the dot path of the array of records ("": the file is the array).
    records: str = ""
    header: bool = True
    delimiter: str = ","
    skip_lines: int = 0
    columns: list = field(default_factory=list)
    #: JSON: the JSON Schema with `x-target` (an object) or where it is (a path, read when a file is read).
    schema: Any = None
    #: A request handed out and not in the file: 不採用 or 不明 (left as it was); None: by whether the file says 採否.
    missing_value: Optional[str] = None
    #: The side (km) of the square drawn round a point.
    size_km: float = 10.0
    time_zone: TimeZone = UTC
    #: The system's `values` turned round ({target: {written: plugin's}}), for values a column's map does not name.
    reverse: dict = field(default_factory=dict)
    #: Days before the day read whose rows a 外部連携ID is looked for in (`linkDays`).
    link_days: int = DEFAULT_LINK_DAYS

    @property
    def missing(self) -> str:
        if self.missing_value is not None:
            return self.missing_value
        return WAITING if self.says_result() is not False else REJECTED

    @missing.setter
    def missing(self, value: str) -> None:
        self.missing_value = value

    def says_result(self) -> Optional[bool]:
        """Whether the file has a 採否 column (None: its schema is in a file, not read yet)."""
        if isinstance(self.schema, str) and self.format == "json":
            return None
        cols = schema_columns(self.schema) if isinstance(self.schema, dict) else self.columns
        return any(c.target == "result" and not c.fixed for c in cols)

    def targets(self) -> set:
        cols = schema_columns(self.schema) if isinstance(self.schema, dict) else self.columns
        return {c.target for c in cols if c.target}


@dataclass
class Answer:
    """One row of a file."""
    line: int
    #: The request's key as the file gives it (the first of id, 管理番号, 識別番号), for messages.
    key: str
    answer: str
    reason: str = ""
    planned_at: Optional[float] = None
    satellite: str = ""
    name: str = ""
    external_id: str = ""
    #: Longitude / latitude rings (WGS 84), [] when the file gives none.
    rings: list = field(default_factory=list)
    #: {linkId | id | managementNo | identificationNo: the file's value}.
    keys: dict = field(default_factory=dict)
    #: The JST day (`2026-11-10`) and the system the row is for, when the file says ("" for any).
    day: str = ""
    system: str = ""
    #: Whether the file says the 採否 (not taken as 採用 for being listed).
    stated: bool = False


# --- The settings ------------------------------------------------------------------------------

def _column(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _word(v: Any) -> str:
    """A value as the text it is compared by: trimmed, upper case (true → TRUE, 1.0 → 1)."""
    return _text(v).upper()


def _values(v: Any, fallback: tuple) -> tuple:
    if isinstance(v, list):
        out = tuple(_word(x) for x in v if isinstance(x, (str, int, float)) and _text(x))
        return out or fallback
    if isinstance(v, (str, int, float)) and _text(v):
        return (_word(v),)
    return fallback


def _words(v: Any) -> Optional[tuple]:
    if isinstance(v, list):
        return tuple(_word(x) for x in v if isinstance(x, (str, int, float, bool)))
    if isinstance(v, (str, int, float, bool)):
        return (_word(v),)
    return None


def _map_of(v: Any) -> dict:
    return {str(k).strip().upper(): x for k, x in v.items()} if is_record(v) else {}


def _pattern(v: Any, at: str, problems: list[str]) -> Optional[re.Pattern]:
    if not isinstance(v, str) or not v:
        return None
    try:
        return re.compile(v)
    except re.error as error:
        problems.append(f"{at}.pattern を正規表現として読めない（{error}）")
        return None


def result_column_of(v: Any, at: str, problems: list[str]) -> Optional[ResultColumn]:
    """One entry of `columns`; None (why in `problems`) when it cannot be used."""
    if not is_record(v):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    key = _column(v.get("key"))
    position = v.get("column")
    if position is not None and not (is_number(position) and position >= 1 and float(position).is_integer()):
        problems.append(f"{at}.column は 1 からの列の番号で指定")
        position = None
    fixed = "value" in v
    if not key and position is None and not fixed:
        problems.append(f"{at} に key（列名）・column（何列目）・value（固定値）のどれもなし")
        return None
    target = v.get("target")
    if target is not None and target not in TARGETS:
        problems.append(f"{at}.target「{target}」は使えない（{'・'.join(TARGETS)}）")
        target = None
    geometry = v.get("geometry", "wkt")
    if geometry not in GEOMETRIES:
        problems.append(f"{at}.geometry は {'・'.join(GEOMETRIES)} のどれかで指定")
        geometry = "wkt"
    return ResultColumn(
        key=key, position=int(position) if position is not None else None, target=target, fixed=fixed, value=v.get("value"),
        format=v["format"].strip() if isinstance(v.get("format"), str) else "", map=_map_of(v.get("map")),
        pattern=_pattern(v.get("pattern"), at, problems), geometry=geometry, default=v.get("default"),
        only=_words(v.get("only")), skip=_words(v.get("skip")))


def _legacy_columns(value: dict, at: str, problems: list[str]) -> Optional[list[ResultColumn]]:
    """The columns of the first form (one name per item)."""
    key = _column(value.get("key"))
    if not key:
        problems.append(f"{at} に key（依頼を表す列）なし")
        return None
    source = value.get("keySource", "id")
    if source not in MATCH_KEYS:
        problems.append(f"{at}.keySource は id・managementNo・identificationNo・linkId のどれかで指定")
        source = "id"
    cols = [ResultColumn(key=key, target=source)]
    result = _column(value.get("result"))
    if result:
        adopted = _values(value.get("adopted"), DEFAULT_ADOPTED)
        rejected = _values(value.get("rejected"), DEFAULT_REJECTED)
        words = {w: REJECTED for w in rejected}
        words.update({w: ADOPTED for w in adopted})
        cols.append(ResultColumn(key=result, target="result", map=words))
    time_format = _column(value.get("timeFormat"))
    for name in ("reason", "plannedAt", "satellite", "name"):
        if _column(value.get(name)):
            cols.append(ResultColumn(key=_column(value[name]), target=name, format=time_format if name == "plannedAt" else ""))
    cols.append(ResultColumn(key=_column(value.get("externalId")) or key, target="externalId"))
    geometry = value.get("geometry") or {}
    if not is_record(geometry) or (geometry and not any(k in geometry for k in LEGACY_GEOMETRY)):
        problems.append(f"{at}.geometry は wkt・geojson・lon と lat・bbox のどれかで指定")
        geometry = {}
    if geometry.get("wkt") or geometry.get("geojson"):
        kind = "wkt" if geometry.get("wkt") else "geojson"
        cols.append(ResultColumn(key=_column(geometry[kind]), target="geometry", geometry=kind))
    elif geometry.get("lon") and geometry.get("lat"):
        cols += [ResultColumn(key=_column(geometry["lon"]), target="centerLon"), ResultColumn(key=_column(geometry["lat"]), target="centerLat")]
    elif isinstance(geometry.get("bbox"), list) and len(geometry["bbox"]) == 4:
        cols += [ResultColumn(key=_column(c), target=t) for c, t in zip(geometry["bbox"], ("bboxWest", "bboxSouth", "bboxEast", "bboxNorth"))]
    return cols


def _has_key(cols: Sequence[ResultColumn]) -> bool:
    return any(c.target in MATCH_KEYS for c in cols)


def result_file_of(value: Any, at: str, problems: list[str], zone: TimeZone = UTC,
                   values: Optional[dict] = None) -> Optional[ResultFile]:
    """`resultFile` of a system (`values`: the system's value maps); None (why in `problems`) when it cannot be used."""
    if value is None:
        return None
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    fmt = str(value.get("format", "csv")).lower()
    if fmt not in FORMATS:
        problems.append(f"{at}.format は csv・json のどちらかで指定")
        return None
    encoding = ENCODINGS.get(str(value.get("encoding", "utf-8")).lower())
    if encoding is None:
        problems.append(f"{at}.encoding は utf-8・utf-8-sig・cp932・euc-jp・utf-16 のどれかで指定")
        encoding = "utf-8"
    raw_delimiter = value.get("delimiter", ",")
    delimiter = DELIMITERS.get(raw_delimiter if isinstance(raw_delimiter, str) else "")
    if delimiter is None:
        problems.append(f"{at}.delimiter は ,・tab・;・|・space のどれかで指定")
        delimiter = ","
    skip = value.get("skipLines", 0)
    if not (is_number(skip) and skip >= 0 and float(skip).is_integer()):
        problems.append(f"{at}.skipLines は 0 以上の整数で指定")
        skip = 0
    size = value.get("sizeKm")
    if size is not None and not (is_number(size) and size > 0):
        problems.append(f"{at}.sizeKm は正の数（km）で指定")
        size = None
    schema = value.get("schema")
    new_form = "columns" in value or schema is not None
    columns: list[ResultColumn] = []
    if new_form:
        old = [k for k in LEGACY_KEYS if k in value]
        if old:
            problems.append(f"{at} の {'・'.join(old)} は columns か schema があるときは使わない（列に書く）")
        if schema is not None:
            if fmt != "json":
                problems.append(f"{at}.schema は format が json のときだけ")
                schema = None
            elif not (is_record(schema) or isinstance(schema, str) and schema.strip()):
                problems.append(f"{at}.schema は JSON Schema かそのファイルのパスで指定")
                return None
        if isinstance(value.get("columns"), list):
            columns = [c for c in (result_column_of(c, f"{at}.columns[{i}]", problems) for i, c in enumerate(value["columns"])) if c]
        elif "columns" in value:
            problems.append(f"{at}.columns は配列で指定")
        if is_record(schema):
            if not any(c.target == "records" for c in _schema_nodes(schema)):
                problems.append(f"{at}.schema に x-target: records の配列（結果の一覧）なし")
                return None
            if not _has_key(schema_columns(schema)):
                problems.append(f"{at}.schema に依頼を表すプロパティ（x-target が id・managementNo・identificationNo・linkId）なし")
                return None
        elif schema is None and not _has_key(columns):
            problems.append(f"{at}.columns に依頼を表す列（target が id・managementNo・identificationNo・linkId）なし")
            return None
    else:
        legacy = _legacy_columns(value, at, problems)
        if legacy is None:
            return None
        columns = legacy
    link_days = value.get("linkDays", DEFAULT_LINK_DAYS)
    if not (is_number(link_days) and 0 <= link_days <= MAX_LINK_DAYS and float(link_days).is_integer()):
        problems.append(f"{at}.linkDays は 0 から {MAX_LINK_DAYS} の整数（日）で指定")
        link_days = DEFAULT_LINK_DAYS
    missing = value.get("missing")
    if missing is not None and missing not in MISSING:
        problems.append(f"{at}.missing は 不採用・不明 のどちらかで指定")
        missing = None
    size_km = float(size) if size is not None else 10.0
    if not new_form and is_record(value.get("geometry")) and is_number(value["geometry"].get("sizeKm")) and value["geometry"]["sizeKm"] > 0:
        size_km = float(value["geometry"]["sizeKm"])
    reverse = {t: {str(w).strip().upper(): k for k, w in m.items() if isinstance(w, (str, int, float))}
               for t, m in (values or {}).items() if t in TARGETS and is_record(m)}
    return ResultFile(
        format=fmt, encoding=encoding, records=_column(value.get("records")), header=value.get("header") is not False,
        delimiter=delimiter, skip_lines=int(skip), columns=columns, schema=schema, missing_value=missing, size_km=size_km,
        time_zone=zone, reverse=reverse, link_days=int(link_days))


# --- Values ------------------------------------------------------------------------------------

def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v).strip()


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or isinstance(v, str) and not v.strip() or isinstance(v, float) and not math.isfinite(v)


def _get(record: dict, column: str) -> Any:
    """A record's value by column (a dot path in JSON; the name without case as a fallback)."""
    if not column:
        return None
    if column in record:
        return record[column]
    value: Any = record
    for part in column.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, dict):
            lower = part.lower()
            value = next((v for k, v in value.items() if isinstance(k, str) and k.strip().lower() == lower), None)
            if value is None:
                break
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            value = None
            break
    return value


def _shaped(col: ResultColumn, raw: Any, reverse: dict) -> tuple[Any, bool]:
    """A file's value through the column's pattern, map (or the system's values turned round) and default; and whether a map named it."""
    v = col.value if col.fixed else raw
    if col.fixed:
        return v, True
    if col.pattern is not None and not _empty(v):
        m = col.pattern.search(_text(v))
        v = ((m.group(1) if m.groups() else m.group(0)) or "") if m else ""
    named = False
    if col.map:
        w = "" if _empty(v) else _word(v)
        if w in col.map:
            v, named = col.map[w], True
        elif w and "*" in col.map:
            v, named = col.map["*"], True
    if not named and col.target in reverse and not _empty(v) and _word(v) in reverse[col.target]:
        v = reverse[col.target][_word(v)]
    if _empty(v) and col.default is not None:
        v, named = col.default, True
    return v, named


def _date_text(t: str) -> Optional[str]:
    """`2026-11-10`, `2026/11/10`, `2026.11.10` or `20261110` as an ISO date; None for anything else."""
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})|(\d{4})(\d{2})(\d{2})", t.strip())
    if not m:
        return None
    y, mo, d = (m[1], m[2], m[3]) if m[1] else (m[4], m[5], m[6])
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _compact(t: str, zone: TimeZone) -> Optional[float]:
    """Digits only: 10 = epoch s, 13 = epoch ms, 8 = yyyymmdd, 12 = yyyymmddHHMM, 14 = yyyymmddHHMMSS (in `zone`)."""
    if len(t) == 10:
        return float(t) * 1000
    if len(t) == 13:
        return float(t)
    if len(t) in (8, 12, 14):
        y, mo, d = t[:4], t[4:6], t[6:8]
        h, mi, sec = (t[8:10], t[10:12], t[12:14] or "00") if len(t) > 8 else ("00", "00", "00")
        return read_text_instant(f"{y}-{mo}-{d}T{h}:{mi}:{sec}", zone)
    return None


def time_value(v: Any, spec: ResultFile, fmt: str = "") -> Optional[float]:
    """
    ms of a time in the file by `fmt`: iso (the default: ISO 8601, `2026/11/10 10:30`, epoch
    numbers in s or ms, digits as `202611101030`), iso-date, unix, unixms or strptime;
    naive times in the system's zone.
    """
    fmt = fmt.strip()
    lenient = fmt in ("", "iso")
    if fmt in ("unix", "unixms") or lenient and is_number(v):
        n = float(v) if is_number(v) else _number(v)
        if n is None:
            return None
        if fmt == "unix" or lenient and abs(n) < 1e11:
            return n * 1000
        return n
    t = _text(v)
    if not t:
        return None
    if fmt == "iso-date":
        day = _date_text(t)
        return read_text_instant(f"{day}T00:00", spec.time_zone) if day else None
    if not lenient:
        try:
            clock = datetime.strptime(t, fmt)
        except ValueError:
            return None
        if clock.tzinfo is not None:
            return clock.timestamp() * 1000
        return instant_of((clock - datetime(1970, 1, 1)).total_seconds() * 1000, spec.time_zone)
    if t.isdigit():
        return _compact(t, spec.time_zone)
    # 2026/11/8 9:30 (as Excel writes it) → 2026-11-08 09:30.
    t = re.sub(r"^(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", t)
    t = re.sub(r"^(\d{4}-\d{2}-\d{2})([T ])(\d):", r"\g<1>\g<2>0\g<3>:", t)
    if re.search(r"\d{2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|z|[+-]\d{2}:?\d{2})$", t):
        # Its own zone said.
        ms = parse_iso(re.sub(r"\s+(?=[Zz+-]\d*:?\d*$)", "", t.replace(" ", "T", 1)))
        if math.isfinite(ms):
            return ms
    found = read_text_instant(t, spec.time_zone)
    if found is not None:
        return found
    day = _date_text(t)
    if day:
        return read_text_instant(f"{day}T00:00", spec.time_zone)
    return None


def day_value(v: Any, spec: ResultFile, fmt: str = "") -> Optional[str]:
    """The JST day (`2026-11-10`) a row is for: a date as written, or the JST day of a time."""
    t = _text(v)
    if not t:
        return None
    if not fmt or fmt in ("iso", "iso-date"):
        plain = _date_text(t)
        if plain:
            return plain
    elif "%H" not in fmt and "%I" not in fmt and fmt not in ("unix", "unixms"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            return None
    ms = time_value(v, spec, fmt)
    if ms is None:
        return None
    return (datetime(1970, 1, 1) + timedelta(milliseconds=ms) + timedelta(hours=9)).date().isoformat()


def _square(lon: float, lat: float, size_km: float) -> list:
    half_lat = size_km / 2 / 110.574
    half_lon = size_km / 2 / (111.320 * max(0.01, math.cos(math.radians(lat))))
    w, e, s, n = lon - half_lon, lon + half_lon, lat - half_lat, lat + half_lat
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


def _box(w: float, s: float, e: float, n: float) -> list:
    if not (w < e and s < n):
        return []
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


def _number(v: Any) -> Optional[float]:
    if is_number(v):
        return float(v)
    try:
        n = float(_text(v))
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _numbers(text: str) -> list[float]:
    out = []
    for part in re.split(r"[,\s;]+", text.strip()):
        if part:
            n = _number(part)
            if n is None:
                return []
            out.append(n)
    return out


def _wkt_rings(text: str, size_km: float = 10.0) -> list:
    """Rings of a WKT POLYGON / MULTIPOLYGON (outer rings and holes, as they come); a POINT as a square."""
    t = text.strip().upper()
    if t.startswith("POINT"):
        nums = _numbers(re.sub(r"[^\d.\-+eE,\s]", " ", text[5:]))
        return _square(nums[0], nums[1], size_km) if len(nums) >= 2 else []
    if not (t.startswith("POLYGON") or t.startswith("MULTIPOLYGON")):
        return []
    rings = []
    for ring in re.findall(r"\(([^()]+)\)", text):
        pts = []
        for pair in ring.split(","):
            nums = pair.split()
            if len(nums) >= 2:
                try:
                    pts.append([float(nums[0]), float(nums[1])])
                except ValueError:
                    return []
        if len(pts) >= 3:
            rings.append(pts)
    return rings


def _geojson_rings(v: Any, size_km: float = 10.0) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    if not isinstance(v, dict):
        return []
    if v.get("type") == "Feature":
        v = v.get("geometry") or {}
    kind, coords = v.get("type"), v.get("coordinates")
    try:
        if kind == "Polygon":
            return [[[float(p[0]), float(p[1])] for p in r] for r in coords]
        if kind == "MultiPolygon":
            return [[[float(p[0]), float(p[1])] for p in r] for poly in coords for r in poly]
        if kind == "Point":
            return _square(float(coords[0]), float(coords[1]), size_km)
    except (TypeError, ValueError, IndexError):
        return []
    return []


def geometry_rings(v: Any, kind: str, size_km: float = 10.0) -> list:
    """An area as a column writes it (WKT and GeoJSON known by themselves; else `kind`: bbox, center, centerRadius, vertices)."""
    if isinstance(v, dict):
        return _geojson_rings(v, size_km)
    if isinstance(v, list):
        nums = [_number(x) for x in v]
        v = ",".join("" if n is None else str(n) for n in nums)
    t = _text(v)
    if not t:
        return []
    if t.startswith("{"):
        return _geojson_rings(t, size_km)
    if re.match(r"^\s*(MULTI)?(POLYGON|POINT)", t, re.IGNORECASE):
        return _wkt_rings(t, size_km)
    if kind in ("wkt", "geojson", "centerGeojson"):
        return []
    nums = _numbers(t)
    if kind == "bbox" and len(nums) == 4:
        return _box(*nums)
    if kind == "center" and len(nums) >= 2:
        return _square(nums[1], nums[0], size_km)
    if kind == "centerRadius" and len(nums) >= 3 and nums[2] > 0:
        return _square(nums[1], nums[0], nums[2] * 2)
    if kind == "vertices" and len(nums) >= 6 and len(nums) % 2 == 0:
        pts = [[nums[i + 1], nums[i]] for i in range(0, len(nums), 2)]
        if pts[0] != pts[-1]:
            pts.append(list(pts[0]))
        return [pts]
    return []


# --- Records -----------------------------------------------------------------------------------

Pairs = list  # [(ResultColumn, raw value)]


def _schema_nodes(schema: Any, root: Any = None, path: str = "", depth: int = 0):
    """The properties of a schema that say `x-target`, as columns (`key` = their path), walking `$ref`s."""
    root = schema if root is None else root
    if depth > 30:
        return
    node = resolve(schema, root)
    if not isinstance(node, dict):
        return
    target = node.get("x-target")
    if isinstance(target, str):
        yield _schema_column(node, path)
        if target == "records":
            yield from _schema_nodes(node.get("items"), root, path + "[]", depth + 1)
        return
    if isinstance(node.get("properties"), dict):
        for name, sub in node["properties"].items():
            yield from _schema_nodes(sub, root, f"{path}.{name}" if path else name, depth + 1)
    if isinstance(node.get("items"), dict):
        yield from _schema_nodes(node["items"], root, path + "[]", depth + 1)
    for k in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(k) or []:
            yield from _schema_nodes(sub, root, path, depth + 1)


def _schema_column(node: dict, path: str) -> ResultColumn:
    target = node.get("x-target")
    geometry = node.get("x-geometry") if node.get("x-geometry") in GEOMETRIES else "geojson"
    pattern = None
    if isinstance(node.get("x-pattern"), str) and node["x-pattern"]:
        try:
            pattern = re.compile(node["x-pattern"])
        except re.error:
            pattern = None
    return ResultColumn(
        key=path, target=target if target in TARGETS or target == "records" else None,
        format=node["x-format"].strip() if isinstance(node.get("x-format"), str) else "", map=_map_of(node.get("x-map")),
        pattern=pattern, geometry=geometry, default=node.get("default") if node.get("x-default", True) else None,
        only=_words(node.get("x-only")), skip=_words(node.get("x-skip")))


def schema_columns(schema: Any) -> list[ResultColumn]:
    """The columns a schema reads (its `x-target`s but `records`)."""
    return [c for c in _schema_nodes(schema) if c.target and c.target != "records"]


def _walk(schema: Any, value: Any, root: dict, pairs: Pairs, records: list, depth: int = 0, path: str = "") -> None:
    """The values a schema's `x-target`s find in a JSON value (`records`: [(items schema, record)] of the list of records)."""
    if depth > 30:
        return
    node = resolve(schema, root)
    if not isinstance(node, dict):
        return
    target = node.get("x-target")
    if target == "records":
        items = node.get("items") if isinstance(node.get("items"), dict) else {}
        if isinstance(value, list):
            records.extend((items, v) for v in value)
        elif isinstance(value, dict):
            records.extend((items, v) for v in value.values())
        return
    if isinstance(target, str):
        pairs.append((_schema_column(node, path), value))
        return
    if isinstance(value, dict) and isinstance(node.get("properties"), dict):
        for name, sub in node["properties"].items():
            _walk(sub, _get(value, name) if name not in value else value[name], root, pairs, records, depth + 1, f"{path}.{name}" if path else name)
    elif isinstance(value, list) and isinstance(node.get("items"), dict) and value:
        # An array that is not the list of records: its first element.
        _walk(node["items"], value[0], root, pairs, records, depth + 1, path + "[0]")
    for k in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(k) or []:
            _walk(sub, value, root, pairs, records, depth + 1, path)


def _decoded(data: bytes, spec: ResultFile, problems: list[str]) -> Optional[str]:
    try:
        text = data.decode(spec.encoding)
    except UnicodeDecodeError:
        problems.append(f"文字コード {spec.encoding} で読めない")
        return None
    return text.lstrip("﻿")


def _csv_rows(text: str, spec: ResultFile, problems: list[str]) -> list[tuple[int, Pairs]]:
    stream = io.StringIO(text)
    for _ in range(spec.skip_lines):
        stream.readline()
    reader = csv.reader(stream, delimiter=spec.delimiter)
    first = spec.skip_lines + 1
    names: list[str] = []
    if spec.header:
        heading = next(reader, None)
        if not heading or not any(h.strip() for h in heading):
            problems.append("見出しの行なし")
            return []
        names = [h.strip().lstrip("﻿") for h in heading]
        first += 1
    lower = {n.lower(): i for i, n in reversed(list(enumerate(names)))}
    places: list[Optional[int]] = []
    order = 0
    for c in spec.columns:
        if c.fixed:
            places.append(None)
        elif c.position is not None:
            places.append(c.position - 1)
        elif spec.header:
            i = names.index(c.key) if c.key in names else lower.get(c.key.lower())
            if i is None:
                problems.append(f"列「{c.key}」が見出しにない")
            places.append(i)
        else:
            places.append(order)
        if not c.fixed and c.position is None:
            order += 1
    out = []
    for n, row in enumerate(reader):
        if not any(cell.strip() for cell in row):
            continue
        pairs = [(c, c.value if c.fixed else row[i] if i is not None and i < len(row) else None) for c, i in zip(spec.columns, places)]
        out.append((first + n, pairs))
    return out


def _json_rows(text: str, spec: ResultFile, schema: Optional[dict], problems: list[str]) -> list[tuple[int, Pairs]]:
    try:
        value = json.loads(text)
    except ValueError as error:
        problems.append(f"JSON として読めない（{error}）")
        return []
    if schema is not None:
        errors = validate(value, schema)
        problems += [f"スキーマ: {e}" for e in errors[:20]] + ([f"スキーマ: ほか {len(errors) - 20} 件"] if len(errors) > 20 else [])
        common: Pairs = []
        records: list = []
        _walk(schema, value, schema, common, records)
        out = []
        for n, (items, record) in enumerate(records):
            pairs: Pairs = []
            _walk(items, record, schema, pairs, [])
            out.append((n + 1, pairs + common))
        return out
    if spec.records:
        for part in spec.records.split("."):
            value = _get(value, part) if isinstance(value, (dict, list)) else None
    if isinstance(value, dict) and not spec.records:
        value = [value]
    if not isinstance(value, list):
        problems.append(f"{spec.records or 'ファイル'} が配列でない")
        return []
    out = []
    seen: set = set()
    for n, record in enumerate(value):
        if not isinstance(record, dict):
            continue
        pairs = []
        for c in spec.columns:
            v = c.value if c.fixed else _get(record, c.key)
            if v is not None:
                seen.add(id(c))
            pairs.append((c, v))
        out.append((n + 1, pairs))
    for c in spec.columns:
        if out and not c.fixed and id(c) not in seen:
            problems.append(f"項目「{c.key}」がどの記録にもない")
    return out


def _grouped(pairs: Pairs, spec: ResultFile) -> dict:
    """{target: [(column, shaped value, named by a map)]} of a row's non-empty values, in the columns' order."""
    out: dict = {}
    for col, raw in pairs:
        if not col.target:
            continue
        v, named = _shaped(col, raw, spec.reverse)
        if not _empty(v):
            out.setdefault(col.target, []).append((col, v, named))
    return out


def _first(group: dict, target: str) -> Any:
    found = group.get(target)
    return found[0][1] if found else None


def _joined(group: dict, target: str) -> tuple[str, str]:
    """The text of a target's columns joined by a space, and the first column's format that says one."""
    found = group.get(target) or []
    fmt = next((c.format for c, _v, _n in found if c.format), "")
    if len(found) == 1:
        return found[0][1], fmt
    return " ".join(_text(v) for _c, v, _n in found), fmt


def _rings(group: dict, spec: ResultFile) -> list:
    for col, v, _n in group.get("geometry") or []:
        rings = geometry_rings(v, col.geometry, spec.size_km)
        if rings:
            return rings
    nums = {t: _number(_first(group, t)) for t in NUMBER_TARGETS}
    if None not in (nums["bboxWest"], nums["bboxSouth"], nums["bboxEast"], nums["bboxNorth"]):
        rings = _box(nums["bboxWest"], nums["bboxSouth"], nums["bboxEast"], nums["bboxNorth"])
        if rings:
            return rings
    if nums["centerLon"] is not None and nums["centerLat"] is not None:
        return _square(nums["centerLon"], nums["centerLat"], spec.size_km)
    return []


def _answer_of(group: dict) -> tuple[Optional[str], str]:
    """採用 / 不採用 / 不明 of a row's 採否 columns (None: not one), and the value as written."""
    found = group.get("result") or []
    for col, v, named in found:
        if isinstance(v, bool):
            return (ADOPTED if v else REJECTED), _text(v)
        w = _word(v)
        canon = next((a for a in ANSWERS if a == _text(v)), None)
        if canon:
            return canon, _text(v)
        if named or not col.map:
            for words, answer in ((DEFAULT_ADOPTED, ADOPTED), (DEFAULT_REJECTED, REJECTED), (DEFAULT_WAITING, WAITING)):
                if w in words:
                    return answer, _text(v)
    return None, _text(found[0][1]) if found else ""


def _kept(pairs: Pairs) -> bool:
    """Whether a row passes the columns' `only` / `skip` (by the file's value)."""
    for col, raw in pairs:
        if col.only is None and col.skip is None:
            continue
        w = _word(col.value if col.fixed else raw)
        if col.only is not None and w not in col.only:
            return False
        if col.skip is not None and w in col.skip:
            return False
    return True


SchemaLoader = Callable[[str], dict]


def read_answers(data: bytes, spec: ResultFile, load_schema: Optional[SchemaLoader] = None) -> tuple[list[Answer], list[str]]:
    """
    The rows of a file, and what could not be read (by line: the file's line
    of a CSV, the record's number in JSON, 1 = the first). A schema given as
    a path is read by `load_schema`.
    """
    problems: list[str] = []
    text = _decoded(data, spec, problems)
    if text is None:
        return [], problems
    schema = spec.schema if spec.format == "json" else None
    if isinstance(schema, str):
        try:
            schema = load_schema(schema) if load_schema else None
        except (OSError, ValueError) as error:
            return [], [f"スキーマ {spec.schema} の読み込み不可（{error}）"]
        if not isinstance(schema, dict):
            return [], [f"スキーマ {spec.schema} の読み込み不可"]
        if not _has_key(schema_columns(schema)):
            return [], [f"スキーマ {spec.schema} に依頼を表すプロパティ（x-target が id・managementNo・identificationNo・linkId）なし"]
    rows = _json_rows(text, spec, schema, problems) if spec.format == "json" else _csv_rows(text, spec, problems)
    says = any(c.target == "result" and not c.fixed for c in (schema_columns(schema) if schema else spec.columns))
    out = []
    dropped = 0
    for line, pairs in rows:
        if not _kept(pairs):
            dropped += 1
            continue
        group = _grouped(pairs, spec)
        keys = {k: _text(_first(group, k)) for k in MATCH_KEYS if _text(_first(group, k))}
        # For messages: the request's key, else the row's id.
        key = next((keys[k] for k in KEY_SOURCES if k in keys), keys.get(LINK_ID, ""))
        if "result" in group:
            answer, raw = _answer_of(group)
            if answer is None:
                shown = raw or '""'
                problems.append(f"{line} 行目: 採否「{raw}」が不明（列の読み替え map に {shown}=採用 のように書く）")
                continue
            stated = says
        elif says:
            problems.append(f"{line} 行目: 採否が空（列の読み替え map に \"\"=不明 のように書くか default）")
            continue
        else:
            answer, stated = ADOPTED, False
        planned = None
        if "plannedAt" in group:
            raw_time, fmt = _joined(group, "plannedAt")
            planned = time_value(raw_time, spec, fmt)
            if planned is None:
                problems.append(f"{line} 行目: 撮像予定「{_text(raw_time)}」を日時として読めない")
        day = ""
        if "day" in group:
            raw_day, fmt = _joined(group, "day")
            day = day_value(raw_day, spec, fmt) or ""
            if not day:
                problems.append(f"{line} 行目: 対象日「{_text(raw_day)}」を日付として読めない（この行は日を問わず読む）")
        out.append(Answer(
            line=line, key=key, answer=answer, reason=_text(_joined(group, "reason")[0])[:1000], planned_at=planned,
            satellite=_text(_first(group, "satellite")), name=_text(_joined(group, "name")[0]),
            external_id=_text(_first(group, "externalId")), rings=_rings(group, spec), keys=keys, day=day,
            system=_text(_first(group, "system")), stated=stated))
    if dropped:
        problems.append(f"only・skip で {dropped} 行を読み飛ばし")
    return out, problems
