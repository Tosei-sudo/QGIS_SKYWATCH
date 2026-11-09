"""
A satellite system's answer file (採否の結果ファイル, `requestExports[].resultFile`):
which of the requests handed out it took into its plan (採用) and which
not (不採用), and the plans it made that were not asked for (要求外, e.g.
its routine tasks). CSV with a header, or JSON (an array of objects, at
`records`). Columns are named in the settings; the rest is ignored.

    "resultFile": {
      "format": "csv", "encoding": "cp932",
      "key": "REQ_ID", "keySource": "id",          // or managementNo / identificationNo
      "result": "STATUS", "adopted": ["OK"], "rejected": ["NG"],
      "missing": "不採用",                          // handed out but not in the file
      "reason": "COMMENT", "plannedAt": "PLAN_TIME", "timeFormat": "%Y%m%d%H%M",
      "satellite": "SAT", "name": "NAME", "externalId": "TASK_ID",
      "geometry": {"wkt": "FOOTPRINT"}               // or geojson / lon+lat(+sizeKm) / bbox, for 要求外
    }

Without `result`, every row listed is 採用 (and, unless `missing` says
otherwise, a request handed out but not listed is 不採用). Times without a
zone are the system's `timeZone`.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..time_zone import UTC, TimeZone, instant_of, read_text_instant
from ..util import is_number, is_record, parse_iso

ADOPTED, REJECTED, WAITING = "採用", "不採用", "不明"
#: What a request's key in the file is: its id, 管理番号 or 識別番号.
KEY_SOURCES = ("id", "managementNo", "identificationNo")
FORMATS = ("csv", "json")
#: What a `missing` request (handed out, not in the file) becomes.
MISSING = (REJECTED, WAITING)
DEFAULT_ADOPTED = ("採用", "OK", "○", "〇", "1", "TRUE", "YES", "Y", "ACCEPTED", "ADOPTED", "PLANNED")
DEFAULT_REJECTED = ("不採用", "NG", "×", "✕", "0", "FALSE", "NO", "N", "REJECTED", "DECLINED")
GEOMETRY_KINDS = ("wkt", "geojson", "lon", "bbox")
_ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "utf-8-sig": "utf-8-sig", "utf-8-bom": "utf-8-sig",
              "cp932": "cp932", "shift_jis": "cp932", "sjis": "cp932", "shift-jis": "cp932"}


@dataclass
class ResultFile:
    """How one system's answer file is read."""
    format: str = "csv"
    encoding: str = "utf-8"
    #: JSON: the dot path of the array of records ("": the file is the array).
    records: str = ""
    #: The column of the request's key, and what that key is (KEY_SOURCES).
    key: str = ""
    key_source: str = "id"
    #: The column of 採用 / 不採用 ("": every row listed is 採用), and the values that mean each.
    result: str = ""
    adopted: tuple = DEFAULT_ADOPTED
    rejected: tuple = DEFAULT_REJECTED
    #: A request handed out and not in the file: 不採用 or 不明 (left as it was).
    missing: str = REJECTED
    reason: str = ""
    planned_at: str = ""
    #: strptime format of `planned_at` ("": ISO 8601, `2026/11/10 10:30` too).
    time_format: str = ""
    satellite: str = ""
    name: str = ""
    #: The system's own id of the row (default: the key's column).
    external_id: str = ""
    #: Where 要求外 rows are: {"wkt": col} | {"geojson": col} | {"lon": col, "lat": col, "sizeKm": n} | {"bbox": [w, s, e, n]}.
    geometry: dict = field(default_factory=dict)
    time_zone: TimeZone = UTC


@dataclass
class Answer:
    """One row of a file."""
    line: int
    key: str
    answer: str
    reason: str = ""
    planned_at: Optional[float] = None
    satellite: str = ""
    name: str = ""
    external_id: str = ""
    #: Longitude / latitude rings (WGS 84), [] when the file gives none.
    rings: list = field(default_factory=list)


def _column(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _values(v: Any, fallback: tuple) -> tuple:
    if isinstance(v, list):
        out = tuple(str(x).strip().upper() for x in v if isinstance(x, (str, int, float)) and str(x).strip())
        return out or fallback
    if isinstance(v, (str, int, float)) and str(v).strip():
        return (str(v).strip().upper(),)
    return fallback


def result_file_of(value: Any, at: str, problems: list[str], zone: TimeZone = UTC) -> Optional[ResultFile]:
    """`resultFile` of a system; None (why in `problems`) when it cannot be used."""
    if value is None:
        return None
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    fmt = str(value.get("format", "csv")).lower()
    if fmt not in FORMATS:
        problems.append(f"{at}.format は csv・json のどちらかで指定")
        return None
    encoding = _ENCODINGS.get(str(value.get("encoding", "utf-8")).lower())
    if encoding is None:
        problems.append(f"{at}.encoding は utf-8・utf-8-sig・cp932 のどれかで指定")
        encoding = "utf-8"
    key = _column(value.get("key"))
    if not key:
        problems.append(f"{at} に key（依頼を表す列）なし")
        return None
    source = value.get("keySource", "id")
    if source not in KEY_SOURCES:
        problems.append(f"{at}.keySource は id・managementNo・identificationNo のどれかで指定")
        source = "id"
    result = _column(value.get("result"))
    missing = value.get("missing", WAITING if result else REJECTED)
    if missing not in MISSING:
        problems.append(f"{at}.missing は 不採用・不明 のどちらかで指定")
        missing = WAITING if result else REJECTED
    geometry = value.get("geometry") or {}
    if not is_record(geometry) or (geometry and not any(k in geometry for k in GEOMETRY_KINDS)):
        problems.append(f"{at}.geometry は wkt・geojson・lon と lat・bbox のどれかで指定")
        geometry = {}
    return ResultFile(
        format=fmt, encoding=encoding, records=_column(value.get("records")), key=key, key_source=source, result=result,
        adopted=_values(value.get("adopted"), DEFAULT_ADOPTED), rejected=_values(value.get("rejected"), DEFAULT_REJECTED),
        missing=missing, reason=_column(value.get("reason")), planned_at=_column(value.get("plannedAt")),
        time_format=_column(value.get("timeFormat")), satellite=_column(value.get("satellite")), name=_column(value.get("name")),
        external_id=_column(value.get("externalId")) or key, geometry=dict(geometry), time_zone=zone,
    )


# --- Reading -----------------------------------------------------------------------------------

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
        else:
            value = None
            break
    if value is not None:
        return value
    lower = column.lower()
    return next((v for k, v in record.items() if isinstance(k, str) and k.strip().lower() == lower), None)


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def time_value(v: Any, spec: ResultFile) -> Optional[float]:
    """ms of a time in the file: epoch (ms, or s when small), ISO text, or `timeFormat`; naive times in the system's zone."""
    if is_number(v):
        n = float(v)
        return n * 1000 if abs(n) < 1e11 else n
    t = _text(v)
    if not t:
        return None
    if spec.time_format:
        try:
            clock = datetime.strptime(t, spec.time_format)
        except ValueError:
            return None
        if clock.tzinfo is not None:
            return clock.timestamp() * 1000
        return instant_of((clock - datetime(1970, 1, 1)).total_seconds() * 1000, spec.time_zone)
    t = re.sub(r"^(\d{4})/(\d{1,2})/(\d{1,2})", lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", t)
    if re.search(r"\d{2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|z|[+-]\d{2}:?\d{2})$", t):
        # Its own zone said.
        ms = parse_iso(re.sub(r"\s+(?=[Zz+-]\d*:?\d*$)", "", t.replace(" ", "T", 1)))
        if math.isfinite(ms):
            return ms
    found = read_text_instant(t, spec.time_zone)
    if found is not None:
        return found
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return read_text_instant(f"{t}T00:00", spec.time_zone)
    return None


def _square(lon: float, lat: float, size_km: float) -> list:
    half_lat = size_km / 2 / 110.574
    half_lon = size_km / 2 / (111.320 * max(0.01, math.cos(math.radians(lat))))
    w, e, s, n = lon - half_lon, lon + half_lon, lat - half_lat, lat + half_lat
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


def _number(v: Any) -> Optional[float]:
    if is_number(v):
        return float(v)
    try:
        n = float(_text(v))
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _wkt_rings(text: str) -> list:
    """Rings of a WKT POLYGON / MULTIPOLYGON (outer rings and holes, as they come); a POINT is not an area."""
    t = text.strip().upper()
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


def _geojson_rings(v: Any) -> list:
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
    except (TypeError, ValueError, IndexError):
        return []
    return []


def rings_of(record: dict, spec: ResultFile) -> list:
    """The area of a row (for 要求外), by `geometry`; [] when it has none."""
    g = spec.geometry
    if not g:
        return []
    if g.get("wkt"):
        return _wkt_rings(_text(_get(record, g["wkt"])))
    if g.get("geojson"):
        return _geojson_rings(_get(record, g["geojson"]))
    if g.get("lon") and g.get("lat"):
        lon, lat = _number(_get(record, g["lon"])), _number(_get(record, g["lat"]))
        size = g.get("sizeKm") if is_number(g.get("sizeKm")) and g["sizeKm"] > 0 else 10
        return _square(lon, lat, float(size)) if lon is not None and lat is not None else []
    if isinstance(g.get("bbox"), list) and len(g["bbox"]) == 4:
        w, s, e, n = (_number(_get(record, c)) for c in g["bbox"])
        if None in (w, s, e, n) or not (w < e and s < n):
            return []
        return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
    return []


def _records(data: bytes, spec: ResultFile, problems: list[str]) -> list[tuple[int, dict]]:
    try:
        text = data.decode(spec.encoding)
    except UnicodeDecodeError:
        problems.append(f"文字コード {spec.encoding} で読めない")
        return []
    text = text.lstrip("﻿")
    if spec.format == "csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            problems.append("見出しの行なし")
            return []
        return [(i + 2, {(k or "").strip(): v for k, v in row.items()}) for i, row in enumerate(reader)]
    try:
        value = json.loads(text)
    except ValueError as error:
        problems.append(f"JSON として読めない（{error}）")
        return []
    if spec.records:
        for part in spec.records.split("."):
            value = value.get(part) if isinstance(value, dict) else None
    if not isinstance(value, list):
        problems.append(f"{spec.records or 'ファイル'} が配列でない")
        return []
    return [(i + 1, r) for i, r in enumerate(value) if isinstance(r, dict)]


def read_answers(data: bytes, spec: ResultFile) -> tuple[list[Answer], list[str]]:
    """The rows of a file, and what could not be read (by row: 2 = the first data row of a CSV, 1 = the first JSON record)."""
    problems: list[str] = []
    out = []
    for line, record in _records(data, spec, problems):
        key = _text(_get(record, spec.key))
        if spec.result:
            raw = _text(_get(record, spec.result))
            word = raw.upper()
            if word in spec.adopted:
                answer = ADOPTED
            elif word in spec.rejected:
                answer = REJECTED
            else:
                problems.append(f"{line} 行目: 採否「{raw}」が不明（adopted・rejected に書く）")
                continue
        else:
            answer = ADOPTED
        planned = time_value(_get(record, spec.planned_at), spec) if spec.planned_at else None
        if spec.planned_at and planned is None and _text(_get(record, spec.planned_at)):
            problems.append(f"{line} 行目: 撮像予定「{_text(_get(record, spec.planned_at))}」を日時として読めない")
        out.append(Answer(
            line=line, key=key, answer=answer, reason=_text(_get(record, spec.reason))[:1000],
            planned_at=planned, satellite=_text(_get(record, spec.satellite)), name=_text(_get(record, spec.name)),
            external_id=_text(_get(record, spec.external_id)), rings=rings_of(record, spec)))
    return out, problems
