"""
Writing the day's requests for each system: a CSV whose columns are named
by keys, or a JSON built from (and checked against) the system's JSON
Schema, whose properties say what goes in them with `x-source`.

What can go in (`source` / `x-source`): see SOURCES. A request's `start`
and `end` are the window for the day: from the day's start (or the
request's, if later) to the day's end for 毎回, or to the request's end for
期間内1回; `periodStart` / `periodEnd` are the request's own.

Times are written in the system's `timeZone` by `dateFormat` (or the
column's `format`): `iso` (default), `iso-date`, `unix`, `unixms`, or a
strftime pattern (`%Y%m%d`). Numbers (and text that is a number) take a
printf format (`%03d`, `%.1f`), see `printf`.

Requests put into one shot (まとめ, core/request/shots.py) are one row:
the first one's (the most important), its `geometry` and box the outline
of the shot's scenes, `mergedIds` / `mergedNames` / `mergedCount` the
requests in it. `passTime` (the shot's middle), `passOffNadir`,
`passIncidence`, `passDirection` (昇交 / 降交) and `passLookSide` (右 / 左)
are the pass the orbits gave; empty when fitted by counting.

Some sources are lists (LIST_SOURCES): an array in JSON where the schema
says `type: array`, otherwise (CSV, or a string property) joined by commas.
A value that is empty (none, "" or []) is written as the column's or
property's `default` when it has one (`[]`, `[""]`, `["On"]` …).

A system that takes at most so many requests in one file (`maxPerFile`,
tsudo 2026-11-11) gets the day's requests in several files of that many
(in the order they were fitted), each a whole file (the CSV's header, the
JSON's file-level properties; `count` is that file's): `{part}` (1, 2 …)
and `{parts}` in the file name number them, or `_1`, `_2` … go before the
extension when the name has no `{part}` (one file keeps its plain name).
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from ..time_zone import TimeZone, clock_of
from .daily import TAKEN, Candidate
from .model import bbox_of, center_of, day_bounds
from .schema import resolve, validate
from .systems import Column, ExportSystem, PriorityRule

#: The sources a column or property can take.
SOURCES = (
    "id", "name", "managementNo", "identificationNo", "category", "achieve", "start", "end", "periodStart", "periodEnd", "repeat",
    "priority", "priorityRaw", "priorityBase", "boost", "kind", "series", "satellites", "modes", "seriesSatellites",
    "seriesSatelliteIds", "satellite", "satelliteId", "mode", "sensor",
    "offNadirMin", "offNadirMax", "incidenceMin", "incidenceMax", "lookSide", "orbitDir", "resolution", "timeOfDay",
    "cloudMax", "cloudForecast", "weatherFlag", "scenes", "geometry", "centerLat", "centerLon",
    "bboxWest", "bboxSouth", "bboxEast", "bboxNorth", "requester", "group", "note", "system", "runDate", "day", "count", "warnings",
    "part", "parts",
    "passTime", "passOffNadir", "passIncidence", "passDirection", "passLookSide", "mergedIds", "mergedNames", "mergedCount",
)
TIME_SOURCES = ("start", "end", "periodStart", "periodEnd", "runDate", "passTime")
#: Sources whose value is a list of names.
LIST_SOURCES = ("series", "satellites", "modes", "seriesSatellites", "seriesSatelliteIds", "mergedIds", "mergedNames")
GEOMETRIES = ("wkt", "geojson", "bbox", "center", "centerRadius", "vertices", "centerGeojson")
_MISSING = object()
#: Text that is a plain number (a printf number format is applied to it as to a number).
_NUMBER_TEXT = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)\s*$")
#: The printf conversions that take a number (whole ones rounded half up first).
_NUMBER_FORMAT = re.compile(r"%[-+ 0#]*\d*(\.\d+)?[diouxXeEfF]")
_WHOLE_FORMAT = re.compile(r"%[-+ 0#]*\d*(\.\d+)?[diouxX]")


def format_time(ms: float, fmt: str, zone: TimeZone) -> Any:
    """A moment written by a format: iso, iso-date, unix, unixms or strftime, in `zone`."""
    if ms is None or not math.isfinite(ms):
        return None
    if fmt == "unix":
        return int(ms // 1000)
    if fmt == "unixms":
        return int(ms)
    offset = zone.offset(ms)
    clock = datetime.fromtimestamp(clock_of(ms, zone) / 1000, tz=timezone.utc).replace(tzinfo=timezone(timedelta(milliseconds=offset)))
    if fmt == "iso-date":
        return clock.strftime("%Y-%m-%d")
    if fmt in ("iso", ""):
        text = clock.strftime("%Y-%m-%dT%H:%M:%S")
        if offset == 0:
            return text + "Z"
        sign = "+" if offset > 0 else "-"
        m = int(abs(offset) // 60000)
        return f"{text}{sign}{m // 60:02d}:{m % 60:02d}"
    return clock.strftime(fmt)


def _round(v: float, digits: int) -> float:
    r = round(v, digits)
    return int(r) if digits == 0 else r


def geometry_value(rings: Sequence, kind: str, digits: int, as_object: bool = False) -> Any:
    """Rings (longitude, latitude) written as WKT, GeoJSON, a box, a centre (with a radius) or a vertex list."""
    if not rings:
        return None
    r = [[[_round(p[0], digits), _round(p[1], digits)] for p in ring] for ring in rings]
    box = bbox_of(rings)
    mid = center_of(rings)
    if kind in ("geojson", "centerGeojson"):
        if kind == "geojson":
            value = {"type": "Polygon", "coordinates": r}
        else:
            # The middle of the area's box, as for `center` and centerLat / centerLon.
            value = {"type": "Point", "coordinates": [_round(mid[0], digits), _round(mid[1], digits)]}
        return value if as_object else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if kind == "bbox":
        return ",".join(f"{_round(v, digits)}" for v in box)
    if kind in ("center", "centerRadius"):
        text = f"{_round(mid[1], digits)},{_round(mid[0], digits)}"
        if kind == "centerRadius":
            w, s, e, n = box
            km_lon = 111.320 * math.cos(math.radians(mid[1]))
            radius = math.hypot((e - w) / 2 * km_lon, (n - s) / 2 * 110.574)
            text += f",{round(radius, 3)}"
        return text
    if kind == "vertices":
        return ",".join(f"{p[1]},{p[0]}" for p in r[0])
    def ring_text(ring):
        return "(" + ", ".join(f"{p[0]:.{digits}f} {p[1]:.{digits}f}" for p in ring) + ")"
    return "POLYGON (" + ", ".join(ring_text(ring) for ring in r) + ")"


def convert_priority(p: int, rule: PriorityRule) -> Any:
    """1 … 1000 as a system writes it (a band's value, a linear range), then its format."""
    value: Any = p
    if rule.table:
        value = next((v for top, v in rule.table if p <= top), rule.table[-1][1])
    elif rule.linear:
        low, high = rule.linear["min"], rule.linear["max"]
        t = (p - 1) / 999
        value = high - t * (high - low) if rule.linear["higherIsLarger"] else low + t * (high - low)
        if float(low).is_integer() and float(high).is_integer():
            value = int(math.floor(value + 0.5))
    if rule.format and (isinstance(value, (int, float)) and not isinstance(value, bool) or isinstance(value, str) and value):
        value = printf(rule.format, value)
    return value


def printf(fmt: str, value: Any) -> Any:
    """
    One printf conversion (`%03d`, `%.1f`, `%+d`, `%5.1f`, `%x`, `%e`, `%s`) with
    text around it (`P%02d`, `%d%%`) applied to a value: whole conversions
    (d i o u x X) round half up first (12.5 → 13). Text that is a number is
    taken as the number by a number conversion. The value as it is when the
    format does not fit (not a number, none or two conversions).
    """
    try:
        if isinstance(value, str) and _NUMBER_FORMAT.search(fmt):
            if not _NUMBER_TEXT.match(value):
                return value
            value = float(value)
        if _WHOLE_FORMAT.search(fmt):
            return fmt % int(math.floor(float(value) + 0.5))
        return fmt % value
    except (TypeError, ValueError, OverflowError):
        return value


def _empty(value: Any) -> bool:
    """None, "", [] (or a number that is not one): what a `default` stands in for."""
    return value is None or value == "" or value == [] or isinstance(value, float) and not math.isfinite(value)


def _names(sats: Sequence, key: str) -> list:
    out: list = []
    for s in sats:
        v = getattr(s, key) or ""
        if v and v not in out:
            out.append(v)
    return out


def series_satellites(c: Candidate, key: str = "name") -> list:
    """
    A request given by satellite series (衛星シリーズ): the satellites of its
    system in those series that can take it (catalog order, one per
    satellite, its modes together); [] for a request not given by series.
    """
    if not c.request.series or c.system is None:
        return []
    return _names(c.able.get(c.system.system, ()), key)


@dataclass
class Context:
    """What the values of one file are made from."""
    system: ExportSystem
    day: date
    run: float
    count: int = 0
    #: Which of the system's files of the day this is (1 …), of how many (`maxPerFile`).
    part: int = 1
    parts: int = 1


def raw_value(c: Optional[Candidate], source: str, ctx: Context, geometry: str = "wkt", digits: Optional[int] = None,
              as_object: bool = False) -> Any:
    """The value of a source for one candidate (or the file, `c` None), before maps and formats."""
    if source == "system":
        return ctx.system.system
    if source == "runDate":
        return ctx.run
    if source == "day":
        return ctx.day.isoformat()
    if source == "count":
        return ctx.count
    if source == "part":
        return ctx.part
    if source == "parts":
        return ctx.parts
    if c is None:
        return _MISSING
    req = c.request
    first, last = day_bounds(ctx.day)
    digits = ctx.system.digits if digits is None else digits
    shot = c.shots[0] if c.shots else None
    together = c.together
    # Requests put into one shot are handed out as one, its area the outline of the shot's scenes (tsudo, 2026-11-11).
    rings = [shot.ring()] if together and shot is not None else req.rings
    box = bbox_of(rings)
    mid = center_of(rings)
    look = shot.look if shot is not None else None
    values = {
        "id": req.id, "name": req.name, "managementNo": req.management_no, "identificationNo": req.identification_no,
        "category": req.category, "achieve": req.achieve,
        "start": max(req.start, first) if math.isfinite(req.start) else first,
        "end": (min(req.end, last) if req.achieve == "毎回" else req.end) if math.isfinite(req.end) else last,
        "periodStart": req.start, "periodEnd": req.end, "repeat": req.repeat,
        "priority": convert_priority(c.priority, ctx.system.priority), "priorityRaw": c.priority,
        "priorityBase": req.priority, "boost": c.boost, "kind": req.kind,
        "series": list(req.series), "satellites": list(req.satellites), "modes": list(req.modes),
        "seriesSatellites": series_satellites(c), "seriesSatelliteIds": series_satellites(c, "id"),
        "satellite": c.satellite.name if c.satellite else "", "satelliteId": c.satellite.id if c.satellite else "",
        "mode": (c.satellite.mode or "") if c.satellite else "", "sensor": (c.satellite.sensor or "") if c.satellite else "",
        "offNadirMin": req.off_nadir_min, "offNadirMax": req.off_nadir_max,
        "incidenceMin": req.incidence_min, "incidenceMax": req.incidence_max,
        "lookSide": req.look_side, "orbitDir": req.orbit_dir, "resolution": req.resolution, "timeOfDay": req.time_of_day,
        "cloudMax": req.cloud_no_go, "cloudForecast": None if c.cloud is None else int(math.floor(c.cloud + 0.5)),
        "weatherFlag": c.weather_flag or "", "scenes": c.scenes,
        "centerLat": _round(mid[1], digits) if mid else None, "centerLon": _round(mid[0], digits) if mid else None,
        "bboxWest": _round(box[0], digits) if box else None, "bboxSouth": _round(box[1], digits) if box else None,
        "bboxEast": _round(box[2], digits) if box else None, "bboxNorth": _round(box[3], digits) if box else None,
        "requester": req.requester, "group": req.group, "note": req.note, "warnings": "・".join(c.warnings),
        "passTime": shot.middle if shot is not None else None,
        "passOffNadir": round(look["offNadir"], 2) if look else None, "passIncidence": round(look["incidence"], 2) if look else None,
        "passDirection": ("昇交" if shot.ascending else "降交") if shot is not None else "",
        "passLookSide": shot.side if shot is not None else "",
        "mergedIds": [m.request.id for m in together], "mergedNames": [m.request.name for m in together],
        "mergedCount": len(together) or 1,
    }
    if source == "geometry":
        return geometry_value(rings, geometry if geometry in GEOMETRIES else "wkt", digits, as_object)
    return values.get(source, _MISSING)


def shaped(value: Any, source: str, ctx: Context, fmt: Optional[str], mapping: dict) -> Any:
    """A value with the system's map of its source, the column's map, and its format (each element of a list)."""
    if value is _MISSING:
        return value
    if isinstance(value, list):
        return [shaped(v, source, ctx, fmt, mapping) for v in value]
    system_map = ctx.system.values.get(source) or {}
    if isinstance(value, str) and value in system_map:
        value = system_map[value]
    if isinstance(value, (str, int, float)) and str(value) in {str(k) for k in mapping}:
        value = next(v for k, v in mapping.items() if str(k) == str(value))
    if source in TIME_SOURCES and isinstance(value, (int, float)):
        return format_time(value, fmt or ctx.system.date_format, ctx.system.time_zone)
    if fmt and (isinstance(value, (int, float)) and not isinstance(value, bool) or isinstance(value, str) and value):
        return printf(fmt, value)
    return value


def cell_value(c: Candidate, col: Column, ctx: Context) -> Any:
    """A column's value: fixed, or its source shaped (its `default` when that is empty)."""
    if col.source is None:
        return col.value
    v = raw_value(c, col.source, ctx, col.geometry or "wkt", col.digits)
    v = shaped(v, col.source, ctx, col.format, col.map)
    if v is not _MISSING and col.default is not None and _empty(v):
        return col.default
    return v


def cell_text(v: Any) -> Any:
    """A value as a CSV cell: a list joined by commas, an object as JSON, none as empty."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ",".join(str(cell_text(x)) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


@dataclass
class Written:
    """One system's file (one of its files, when split by `maxPerFile`): its text, and what was found wrong."""
    system: ExportSystem
    file_name: str
    text: str
    count: int
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    #: Which of the system's files this is (1 …), of how many; the ids of the requests in it.
    part: int = 1
    parts: int = 1
    ids: tuple = ()


def csv_text(cands: Sequence[Candidate], ctx: Context) -> tuple[str, list[str]]:
    """The CSV of a system's requests (CRLF lines), and the values not allowed."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    columns: list[Column] = ctx.system.columns
    if ctx.system.header:
        writer.writerow([c.key for c in columns])
    warnings = []
    for cand in cands:
        row = []
        for col in columns:
            v = cell_value(cand, col, ctx)
            if v is _MISSING:
                warnings.append(f"{col.key}: 項目 {col.source} なし")
                v = ""
            v = cell_text(v)
            if col.allowed is not None and v not in col.allowed and str(v) not in {str(a) for a in col.allowed}:
                warnings.append(f"{cand.request.name} の {col.key}: {v!r} は許可された値以外")
            row.append(v)
        writer.writerow(row)
    return out.getvalue(), sorted(set(warnings), key=warnings.index)


def _coerce(value: Any, schema: dict, root: Optional[dict] = None) -> Any:
    """A value turned to the schema's type where that is plain (text ↔ number; a list → an array, or text joined by commas)."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    if isinstance(value, list):
        if "array" not in types:
            return cell_text(value)
        items = resolve(schema.get("items"), root or schema)
        return [_coerce(v, items, root) for v in value] if isinstance(items, dict) else value
    if value is None or not types:
        return value
    if "string" in types and isinstance(value, (int, float)) and not isinstance(value, bool) and not any(x in types for x in ("integer", "number")):
        return str(value)
    if isinstance(value, str) and ("integer" in types or "number" in types) and "string" not in types:
        try:
            n = float(value)
        except ValueError:
            return value
        return int(n) if "integer" in types and n.is_integer() else n
    if "integer" in types and isinstance(value, float) and value.is_integer() and "number" not in types:
        return int(value)
    return value


def build(schema: Any, root: dict, c: Optional[Candidate], cands: Sequence[Candidate], ctx: Context) -> Any:
    """The value a schema (with `x-source`) asks for; _MISSING for nothing."""
    schema = resolve(schema, root)
    if not isinstance(schema, dict):
        return _MISSING
    source = schema.get("x-source")
    if source == "requests":
        items = schema.get("items") or {}
        return [v for v in (build(items, root, cand, cands, ctx) for cand in cands) if v is not _MISSING]
    if isinstance(source, str):
        v = raw_value(c, source, ctx, schema.get("x-geometry") or "geojson",
                      schema.get("x-digits") if isinstance(schema.get("x-digits"), int) else None, as_object=True)
        v = shaped(v, source, ctx, schema.get("x-format"), schema.get("x-map") if isinstance(schema.get("x-map"), dict) else {})
        if v is _MISSING:
            return v
        if _empty(v) and _writes_default(schema):
            # Empty (none, "", []): the property's default as written (`[]`, `[""]`, `["On"]` …), e.g. for a required one.
            return schema["default"]
        return _coerce(v, schema, root)
    if "const" in schema:
        return schema["const"]
    if _writes_default(schema):
        return schema["default"]
    props = schema.get("properties")
    if isinstance(props, dict):
        out = {}
        for name, sub in props.items():
            v = build(sub, root, c, cands, ctx)
            if v is _MISSING:
                continue
            if v is None and not _allows_null(resolve(sub, root)) and name not in (schema.get("required") or []):
                continue
            out[name] = v
        return out if out or schema.get("required") else _MISSING
    return _MISSING


def _writes_default(schema: dict) -> bool:
    """Whether a property's `default` is written (not with `x-default: false`, which leaves it to the system)."""
    return "default" in schema and bool(schema.get("x-default", True))


def _allows_null(schema: Any) -> bool:
    t = schema.get("type") if isinstance(schema, dict) else None
    return t is None or t == "null" or isinstance(t, list) and "null" in t


def json_text(cands: Sequence[Candidate], schema: dict, ctx: Context) -> tuple[str, list[str]]:
    """The JSON of a system's requests built from its schema, and what the schema finds wrong with it."""
    value = build(schema, schema, None, cands, ctx)
    if value is _MISSING:
        value = {}
    errors = validate(value, schema)
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n", errors


_PLACE = re.compile(r"\{(\w+)(?::([^}]*))?\}")
_PART = re.compile(r"\{part(?::[^}]*)?\}")
_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def _numbered(text: str, part: int) -> str:
    """`_{part}` before a name's extension (`a.csv` → `a_2.csv`)."""
    root, ext = os.path.splitext(text)
    return f"{root}_{part}{ext}"


def file_name_of(system: ExportSystem, day: date, run: float, part: int = 1, parts: int = 1) -> str:
    """
    The file's name: `{system}`, `{date}` / `{date:%Y%m%d}` (the day),
    `{run:%H%M}` (when written, JST), `{ext}`, and for a day split into
    files (`maxPerFile`) `{part}` / `{part:%02d}` (1, 2 …) and `{parts}`;
    a split name without `{part}` takes `_1`, `_2` … before its extension.
    """
    name = "".join(_UNSAFE.sub("_", p) for p in _pieces(system, day, run, part, parts))
    if parts > 1 and not _PART.search(system.file_name):
        name = _numbered(name, part)
    return name


def _pieces(system: ExportSystem, day: date, run: float, part: Optional[int], parts: Optional[int]) -> list:
    """The name in pieces: text, and the values of its `{…}`; `{part}` / `{parts}` None (left as None) when not given."""
    from .model import JST

    ran = datetime.fromtimestamp(run / 1000, tz=JST)

    def value(key: str, fmt: Optional[str], whole: str) -> Any:
        if key == "system":
            return system.system
        if key == "ext":
            return system.format
        if key == "date":
            return day.strftime(fmt or "%Y%m%d")
        if key == "run":
            return ran.strftime(fmt or "%Y%m%d%H%M")
        if key in ("part", "parts"):
            n = part if key == "part" else parts
            return None if n is None else str(printf(fmt, n)) if fmt else str(n)
        return whole

    out: list = []
    at = 0
    for m in _PLACE.finditer(system.file_name):
        out.append(system.file_name[at:m.start()])
        out.append(value(m.group(1), m.group(2), m.group(0)))
        at = m.end()
    out.append(system.file_name[at:])
    return out


def leftover_files(system: ExportSystem, day: date, run: float, names: Sequence[str], listed: Sequence[str]) -> list[str]:
    """
    Files of `listed` (a folder's names) named as this system's files of the
    day with another number, which this writing (`names`) does not replace:
    those of an earlier writing split into more files (or not split, or
    split when this one is not). Left in the folder, they would be handed
    to the system again.
    """
    merged: list = []
    for p in _pieces(system, day, run, None, None):
        p = None if p is None else _UNSAFE.sub("_", p)
        if p is not None and merged and merged[-1] is not None:
            merged[-1] += p
        else:
            merged.append(p)
    tail = ""
    if not _PART.search(system.file_name):
        # Numbered by `_1`, `_2` … before the extension, or not numbered at all.
        last = merged[-1] if merged[-1] is not None else ""
        ext = os.path.splitext(last)[1]
        if ext:
            merged[-1] = last[:-len(ext)]
        tail = r"(_\d+)?" + re.escape(ext)
    found = re.compile("".join(r"\d+" if p is None else re.escape(p) for p in merged) + tail, re.IGNORECASE)
    mine = {n.casefold() for n in names}
    return sorted(n for n in listed if found.fullmatch(n) and n.casefold() not in mine)


SchemaLoader = Callable[[str], dict]


def split(cands: Sequence[Candidate], most: int) -> list[list[Candidate]]:
    """Requests in runs of at most `most` (in their order); all in one when `most` is 0."""
    if most <= 0 or len(cands) <= most:
        return [list(cands)]
    return [list(cands[i:i + most]) for i in range(0, len(cands), most)]


def ids_of(rows: Sequence[Candidate]) -> tuple:
    """The requests in a file's rows: each row's, and those put into its shot (まとめ) with it."""
    out: list = []
    for c in rows:
        out.extend(m.request.id for m in (c.together or [c]))
    return tuple(out)


def render(cands: Sequence[Candidate], systems: Sequence[ExportSystem], day: date, run: float,
           load_schema: Optional[SchemaLoader] = None) -> list[Written]:
    """
    The files of the day: one per system with requests taken (in the
    candidates' order), or several of at most `maxPerFile` requests each.
    """
    out = []
    for system in systems:
        # A request put into another's shot goes in that one's row (mergedIds), not a row of its own.
        mine = [c for c in cands if c.result == TAKEN and c.system is system and not c.member]
        if not mine:
            continue
        schema = system.schema
        if system.format == "json":
            problem = None
            if isinstance(schema, str):
                try:
                    schema = load_schema(schema) if load_schema else None
                except (OSError, ValueError) as error:
                    problem = f"スキーマ {system.schema} の読み込み不可（{error}）"
            if problem is None and not isinstance(schema, dict):
                problem = "JSON Schema なし"
            if problem:
                out.append(Written(system, file_name_of(system, day, run), "", len(mine), [problem], ids=ids_of(mine)))
                continue
        runs = split(mine, system.max_per_file)
        for part, some in enumerate(runs, 1):
            ctx = Context(system, day, run, len(some), part, len(runs))
            name = file_name_of(system, day, run, part, len(runs))
            ids = ids_of(some)
            if system.format == "json":
                text, errors = json_text(some, schema, ctx)
                out.append(Written(system, name, text, len(some), errors, part=part, parts=len(runs), ids=ids))
            else:
                text, warnings = csv_text(some, ctx)
                out.append(Written(system, name, text, len(some), [], warnings, part=part, parts=len(runs), ids=ids))
    return out


def files_of(written: Sequence[Written]) -> dict:
    """
    What was written, for the history (workflow.history_of): system name →
    its file's name, or for a system split into files {request id: the
    name of the file it is in}.
    """
    out: dict = {}
    for w in written:
        if w.parts <= 1:
            out[w.system.system] = w.file_name
        else:
            names = out.setdefault(w.system.system, {})
            if isinstance(names, dict):
                names.update((i, w.file_name) for i in w.ids)
    return out


def encoded(w: Written) -> tuple[bytes, list[str]]:
    """A file's bytes in its system's encoding; characters it cannot hold become ? (and are told)."""
    try:
        return w.text.encode(w.system.encoding), []
    except UnicodeEncodeError:
        return w.text.encode(w.system.encoding, errors="replace"), [f"{w.system.encoding} で書けない文字を ? に置き換え"]


def write(w: Written, folder: str) -> str:
    """Writes a file into a folder (made when missing); its path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, w.file_name)
    data, _ = encoded(w)
    with open(path, "wb") as f:
        f.write(data)
    return path
