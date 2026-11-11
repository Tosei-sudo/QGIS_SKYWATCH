"""
Exporting the observation records (観測記録のエクスポート): the records of a
period, a place, some 大分類 and 確度 written as CSV or JSON by a named
format.

Which records: the period (by 撮像日時, 登録日時 or 更新日時), the place (the
map's extent, a polygon drawn, a layer's polygons, a named area — any
polygons, WGS 84), the 大分類 (all, or those chosen), the least 確度, 有無,
and whether deleted records go too.

How they are written (`observationExports.formats`, the default chosen by
`default`): like the files of the imaging request systems (requestExports),
a CSV whose columns are named by keys, or a JSON — an array of objects of
the same columns, or a JSON Schema whose properties say what goes in them
(`x-source`, the records' array `"x-source": "records"`), checked against
it. Two formats are built in and always offered: 「標準（CSV）」 (the CSV
written before, with the place names when there are named areas) and
「標準（JSON）」.

Besides a record's own values a column can take what is found from where it
is: its place (the smallest named area holding it, as the 通報: `place`,
`placeKind`, all of them smallest first `places`; among the areas of some
kinds only with `placeKinds`, schema `x-placeKinds`), the geofences it is in
(`geofences`), its coordinate in MGRS, UTM or degrees-minutes-seconds, and
of the file: when and by whom it was written, the period and place asked.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from ..coords import format_dms, format_lat_lon, format_mgrs, format_utm
from ..time_zone import TimeZone, clock_of, parse_time_zone
from ..util import formula_safe, is_number, is_record, js_string
from ..request.export import _coerce, format_time, printf
from ..request.schema import resolve, validate
from .alerts import (
    DEFAULT_FENCE_FIELDS, DEFAULT_PLACE_FIELDS, EsriSource, Geofence, NamedArea, at_least, esri_source_of, inside_rings, of_kinds,
    place_kinds_of, places_of,
)
from .csv import observations_csv
from .flows import time_key
from .geo import to_fixed
from .model import ConfidenceLevel, Observation
from .taxonomy import kind_of, path_of

#: What a column (or a schema's `x-source`) can take.
RECORD_SOURCES = (
    "id", "objectId", "objectName", "kind", "activity", "present", "confidence", "imageTime", "imageId", "imageName",
    "lon", "lat", "length", "heading", "width", "note", "recordedBy", "updatedBy", "created", "updated", "deleted",
    "place", "placeKind", "places", "geofences", "latLon", "dms", "mgrs", "utm", "geometry", "index",
)
#: Of the file, the same for every record.
FILE_SOURCES = ("count", "exportedAt", "exportedBy", "periodStart", "periodEnd", "periodBasis", "areaName", "format")
SOURCES = RECORD_SOURCES + FILE_SOURCES
TIME_SOURCES = ("imageTime", "created", "updated", "deleted", "exportedAt", "periodStart", "periodEnd")
NUMBER_SOURCES = ("lon", "lat", "length", "heading", "width")
#: The sources that need the named areas or the geofences read.
PLACE_SOURCES = ("place", "placeKind", "places")
FENCE_SOURCES = ("geofences",)
GEOMETRIES = ("geojson", "wkt", "latlon")
FORMATS = ("csv", "json")
ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "utf-8-sig": "utf-8-sig", "utf-8-bom": "utf-8-sig",
             "cp932": "cp932", "shift_jis": "cp932", "sjis": "cp932", "shift-jis": "cp932"}
NEWLINES = {"crlf": "\r\n", "lf": "\n"}
#: The period's basis: which time of a record is in it.
BASES = {"image": "撮像日時", "created": "登録日時", "updated": "更新日時"}
BUILTIN_CSV = "標準（CSV）"
BUILTIN_JSON = "標準（JSON）"
DEFAULT_TIME_ZONE = "Asia/Tokyo"
_MISSING = object()


@dataclass
class ExportColumn:
    """A CSV column or a JSON property: its key, what goes in (a source or a fixed value), how it is written."""
    key: str
    source: Optional[str] = None
    value: Any = None
    #: Times: iso, iso-date, unix, unixms or strftime; numbers: printf (`%.3f`).
    format: Optional[str] = None
    #: Values replaced (compared as text: `{"true": "在"}`).
    map: dict = field(default_factory=dict)
    digits: Optional[int] = None
    #: `kind`: the depth (1 the top, -1 the last); the whole path when None.
    level: Optional[int] = None
    #: `geometry`: geojson, wkt or latlon.
    geometry: Optional[str] = None
    #: Written when the value is empty; text around a value only when there is one.
    empty: Any = None
    prefix: str = ""
    suffix: str = ""
    #: `place`, `placeKind`, `places`: only the named areas of these kinds (KIND, any case); all when empty.
    place_kinds: tuple[str, ...] = ()


@dataclass
class ExportFormat:
    """A named format of `observationExports.formats` (or a built-in one)."""
    name: str
    format: str = "csv"
    columns: list[ExportColumn] = field(default_factory=list)
    #: JSON: the JSON Schema (an object) or where it is (a path); None writes an array of the columns.
    schema: Any = None
    file_name: str = "observations_{now:%Y%m%d_%H%M}.{ext}"
    encoding: str = "utf-8"
    header: bool = True
    delimiter: str = ","
    newline: str = "\r\n"
    time_zone: TimeZone = field(default_factory=lambda: parse_time_zone(DEFAULT_TIME_ZONE))
    date_format: str = "iso"
    #: Decimals of the coordinates when a column does not say.
    digits: int = 7
    builtin: bool = False

    def sources(self) -> set[str]:
        """The sources it writes (a schema's `x-source` too, when the schema is in the settings)."""
        out = {c.source for c in self.columns if c.source}
        if isinstance(self.schema, dict):
            out |= set(_schema_sources(self.schema))
        return out

    def needs(self, sources: Sequence[str]) -> bool:
        """Whether it writes one of the sources (a built-in one or a schema in a file: always)."""
        if self.builtin or isinstance(self.schema, str):
            return True
        return bool(self.sources() & set(sources))


def _schema_sources(schema: Any) -> Iterable[str]:
    if isinstance(schema, dict):
        if isinstance(schema.get("x-source"), str):
            yield schema["x-source"]
        for v in schema.values():
            yield from _schema_sources(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _schema_sources(v)


@dataclass
class ExportSettings:
    """`observationExports`: the named formats (the built-in ones first) and where places are found."""
    formats: list[ExportFormat] = field(default_factory=list)
    default: str = BUILTIN_CSV
    named_areas: Optional[EsriSource] = None
    geofences: Optional[EsriSource] = None

    def format(self, name: str = "") -> Optional[ExportFormat]:
        name = name or self.default
        return next((f for f in self.formats if f.name == name), self.formats[0] if self.formats else None)


#: The built-in JSON's properties (UPPERCASE_UNDERSCORE, as the Esri fields).
STANDARD_COLUMNS = (
    ("OBS_ID", "id"), ("OBJECT_ID", "objectId"), ("OBJECT_NAME", "objectName"), ("KIND", "kind"), ("ACTIVITY", "activity"),
    ("PRESENT", "present"), ("CONFIDENCE", "confidence"), ("IMAGE_TIME", "imageTime"), ("IMAGE_ID", "imageId"),
    ("IMAGE_NAME", "imageName"), ("LON", "lon"), ("LAT", "lat"), ("LENGTH_M", "length"), ("HEADING_DEG", "heading"),
    ("WIDTH_M", "width"), ("NOTE", "note"), ("RECORDED_BY", "recordedBy"), ("UPDATED_BY", "updatedBy"),
    ("CREATED_AT", "created"), ("UPDATED_AT", "updated"), ("DELETED_AT", "deleted"),
)
PLACE_COLUMNS = (("PLACE", "place"), ("PLACE_KIND", "placeKind"), ("PLACES", "places"))
FENCE_COLUMNS = (("GEOFENCES", "geofences"),)


def builtin_formats(places: bool = False, fences: bool = False) -> list[ExportFormat]:
    """「標準（CSV）」 and 「標準（JSON）」 (the place names and geofences in them when there are some)."""
    cols = [*STANDARD_COLUMNS, *(PLACE_COLUMNS if places else ()), *(FENCE_COLUMNS if fences else ())]
    return [
        ExportFormat(BUILTIN_CSV, "csv", file_name="observations_{now:%Y%m%d}.csv", encoding="utf-8-sig", builtin=True),
        ExportFormat(BUILTIN_JSON, "json", [ExportColumn(k, s) for k, s in cols], file_name="observations_{now:%Y%m%d}.json",
                     builtin=True),
    ]


# ---- the settings file ------------------------------------------------------------


def _column_of(v: Any, at: str, problems: list[str]) -> Optional[ExportColumn]:
    if isinstance(v, str):
        v = {"key": v, "source": v}
    if not is_record(v) or not isinstance(v.get("key"), str) or not v["key"].strip():
        problems.append(f"{at} は key（列名）を持つオブジェクトで指定")
        return None
    source = v.get("source")
    if source is not None and source not in SOURCES:
        problems.append(f"{at} の source「{js_string(source)}」は使用不可（{'・'.join(SOURCES)}、または value）")
        return None
    if source is None and "value" not in v:
        problems.append(f"{at} に source か value なし")
        return None
    digits, level, geometry = v.get("digits"), v.get("level"), v.get("geometry")
    if "digits" in v and not (is_number(digits) and 0 <= digits <= 12):
        problems.append(f"{at} の digits は 0〜12 で指定")
    if "level" in v and not (is_number(level) and int(level) == level and level != 0):
        problems.append(f"{at} の level は 1（最上位）からの階層、または -1（最下位）で指定")
    if "geometry" in v and geometry not in GEOMETRIES:
        problems.append(f"{at} の geometry は {'・'.join(GEOMETRIES)} のどれかで指定")
    if "map" in v and not is_record(v.get("map")):
        problems.append(f"{at} の map はオブジェクトで指定")
    for key in ("prefix", "suffix"):
        if key in v and not isinstance(v[key], str):
            problems.append(f"{at}.{key} は文字列で指定")
    kinds = place_kinds_of(v.get("placeKinds"), at, problems)
    if kinds and source not in PLACE_SOURCES:
        problems.append(f"{at} の placeKinds は地名の項目（{'・'.join(PLACE_SOURCES)}）だけに指定")
        kinds = ()
    return ExportColumn(
        key=v["key"].strip(), source=source, value=v.get("value"),
        format=v["format"] if isinstance(v.get("format"), str) and v["format"] else None,
        map={str(k): x for k, x in v["map"].items()} if is_record(v.get("map")) else {},
        digits=int(digits) if is_number(digits) and 0 <= digits <= 12 else None,
        level=int(level) if is_number(level) and int(level) == level and level != 0 else None,
        geometry=geometry if geometry in GEOMETRIES else None,
        empty=v.get("empty"),
        prefix=v["prefix"] if isinstance(v.get("prefix"), str) else "",
        suffix=v["suffix"] if isinstance(v.get("suffix"), str) else "",
        place_kinds=kinds,
    )


def export_format_of(value: Any, problems: list[str], at: str) -> Optional[ExportFormat]:
    """One `observationExports.formats` entry; None (the reason in `problems`) when it cannot be used."""
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{at} に name（フォーマットの名前）なし")
        return None
    if name.strip() in (BUILTIN_CSV, BUILTIN_JSON):
        problems.append(f"{at} の name「{name.strip()}」は組み込みのフォーマット名")
        return None
    fmt = str(value.get("format", "csv")).lower()
    if fmt not in FORMATS:
        problems.append(f"{at}.format は csv か json で指定")
        fmt = "csv"
    out = ExportFormat(name=name.strip(), format=fmt)
    cols = value.get("columns")
    if cols is not None:
        if isinstance(cols, list):
            out.columns = [c for c in (_column_of(c, f"{at}.columns[{i}]", problems) for i, c in enumerate(cols)) if c]
        else:
            problems.append(f"{at}.columns は列の配列で指定")
    schema = value.get("schema")
    if "schema" in value:
        if fmt != "json":
            problems.append(f"{at}.schema は format が json のときのみ")
        elif is_record(schema) or (isinstance(schema, str) and schema.strip()):
            out.schema = schema.strip() if isinstance(schema, str) else schema
        else:
            problems.append(f"{at}.schema は JSON Schema（オブジェクト）かそのファイルのパスで指定")
    if not out.columns and out.schema is None:
        problems.append(f"{at} に columns（列）" + ("か schema" if fmt == "json" else "") + " なし")
        return None
    out.file_name = value["fileName"] if isinstance(value.get("fileName"), str) and value["fileName"].strip() else \
        "observations_{now:%Y%m%d_%H%M}.{ext}"
    encoding = ENCODINGS.get(str(value.get("encoding", "utf-8-sig" if fmt == "csv" else "utf-8")).lower())
    if encoding is None:
        problems.append(f"{at}.encoding は utf-8・utf-8-sig・cp932（shift_jis）のどれかで指定")
        encoding = "utf-8"
    out.encoding = encoding
    if "header" in value and not isinstance(value["header"], bool):
        problems.append(f"{at}.header は true か false で指定")
    out.header = value.get("header") is not False
    if "delimiter" in value:
        if isinstance(value["delimiter"], str) and len(value["delimiter"]) == 1:
            out.delimiter = value["delimiter"]
        else:
            problems.append(f"{at}.delimiter は 1 文字で指定（タブは \"\\t\"）")
    if "newline" in value:
        nl = NEWLINES.get(str(value["newline"]).lower())
        if nl is None:
            problems.append(f"{at}.newline は crlf か lf で指定")
        else:
            out.newline = nl
    if "timeZone" in value:
        zone = parse_time_zone(value["timeZone"])
        if zone is None:
            problems.append(f"{at}.timeZone は Asia/Tokyo・+09:00・UTC のように指定")
        else:
            out.time_zone = zone
    if "dateFormat" in value:
        if isinstance(value["dateFormat"], str) and value["dateFormat"]:
            out.date_format = value["dateFormat"]
        else:
            problems.append(f"{at}.dateFormat は iso・iso-date・unix・unixms・%Y-%m-%d %H:%M のように指定")
    if "digits" in value:
        d = value["digits"]
        if is_number(d) and 0 <= d <= 12:
            out.digits = int(d)
        else:
            problems.append(f"{at}.digits は 0〜12 で指定")
    return out


def export_settings_of(value: Any, problems: list[str]) -> ExportSettings:
    """`observationExports` (the built-in formats are added by `settings_for`)."""
    at = "observationExports"
    settings = ExportSettings()
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return settings
    raw = value.get("formats", [])
    if not isinstance(raw, list):
        problems.append(f"{at}.formats はフォーマットの配列で指定")
        raw = []
    for i, v in enumerate(raw):
        f = export_format_of(v, problems, f"{at}.formats[{i}]")
        if f is None:
            continue
        if any(x.name == f.name for x in settings.formats):
            problems.append(f"{at}.formats の name「{f.name}」が重複")
            continue
        settings.formats.append(f)
    default = value.get("default")
    names = [BUILTIN_CSV, BUILTIN_JSON, *(f.name for f in settings.formats)]
    if default is not None:
        if isinstance(default, str) and default.strip() in names:
            settings.default = default.strip()
        else:
            problems.append(f"{at}.default は formats の name（または {BUILTIN_CSV}・{BUILTIN_JSON}）で指定")
    for key, defaults, attr in (("namedAreas", DEFAULT_PLACE_FIELDS, "named_areas"), ("geofences", DEFAULT_FENCE_FIELDS, "geofences")):
        if key in value and value[key] not in (None, False, ""):
            setattr(settings, attr, esri_source_of(value[key], f"{at}.{key}", defaults, problems))
    return settings


def settings_for(own: Optional[ExportSettings], alerts: Any = None) -> ExportSettings:
    """
    The settings used: the built-in formats, then those of the file; the
    named areas and geofences of `observationExports`, else those of
    `alertReports` (the same layers as the 通報).
    """
    own = own or ExportSettings()
    named = own.named_areas or getattr(alerts, "named_areas", None)
    fences = own.geofences or getattr(alerts, "geofences", None)
    formats = [*builtin_formats(named is not None, fences is not None), *(f for f in own.formats if not f.builtin)]
    return ExportSettings(formats=formats, default=own.default, named_areas=named, geofences=fences)


# ---- which records ------------------------------------------------------------


@dataclass
class ExportFilter:
    """Which records go out."""
    #: The period (ms, inclusive); None: open.
    start: Optional[float] = None
    end: Optional[float] = None
    #: Which time is in the period (BASES).
    basis: str = "image"
    #: The place: polygons (each a list of rings, WGS 84 longitude / latitude); None anywhere.
    polygons: Optional[list] = None
    #: What the place is called in the file (`areaName`).
    area_name: str = ""
    #: Only these 大分類 (None all).
    roots: Optional[list[str]] = None
    #: The least 確度 ('' any).
    confidence: str = ""
    #: "all", "present" (あり) or "absent" (なし).
    presence: str = "all"
    deleted: bool = False


def basis_time(o: Observation, basis: str) -> Optional[float]:
    if basis == "created":
        return o.created_at
    if basis == "updated":
        return o.updated_at if o.updated_at is not None else o.created_at
    return time_key(o)


def in_polygons(lon: float, lat: float, polygons: Sequence) -> bool:
    """Whether a point is in one of the polygons (each its rings, holes outside)."""
    for rings in polygons:
        if not rings:
            continue
        xs = [p[0] for ring in rings for p in ring]
        ys = [p[1] for ring in rings for p in ring]
        if min(xs) <= lon <= max(xs) and min(ys) <= lat <= max(ys) and inside_rings(lon, lat, rings):
            return True
    return False


def select_records(records: Iterable[Observation], flt: ExportFilter, levels: Sequence[ConfidenceLevel] = (),
                   path: Callable[[str], list[str]] = path_of) -> list[Observation]:
    """The records the filter takes, by thing and in flow order (as the CSV always was)."""
    out = []
    for o in records:
        if o.deleted_at is not None and not flt.deleted:
            continue
        t = basis_time(o, flt.basis)
        if flt.start is not None and (t is None or t < flt.start):
            continue
        if flt.end is not None and (t is None or t > flt.end):
            continue
        if flt.roots is not None and (path(o.kind) or [""])[0] not in flt.roots:
            continue
        if flt.confidence and not at_least(o.confidence or "", flt.confidence, levels):
            continue
        if flt.presence == "present" and not o.present or flt.presence == "absent" and o.present:
            continue
        if flt.polygons is not None and not in_polygons(o.lon, o.lat, flt.polygons):
            continue
        out.append(o)
    return sorted(out, key=lambda o: (o.object_id, time_key(o), o.created_at))


# ---- writing ------------------------------------------------------------


@dataclass
class ExportItem:
    """A record and what was found from where it is."""
    record: Observation
    #: The named areas it is in, smallest first (the first its place).
    places: list[NamedArea] = field(default_factory=list)
    #: The geofences it is in.
    fences: list[str] = field(default_factory=list)


def items_of(records: Iterable[Observation], named: Sequence[NamedArea] = (), fences: Sequence[Geofence] = ()) -> list[ExportItem]:
    out = []
    for o in records:
        out.append(ExportItem(
            o, places_of(o.lon, o.lat, named) if named else [],
            list(dict.fromkeys(g.name for g in fences if inside_rings(o.lon, o.lat, g.rings))) if fences else [],
        ))
    return out


@dataclass
class ExportContext:
    """What the file-wide values are made from."""
    format: ExportFormat
    filter: ExportFilter = field(default_factory=ExportFilter)
    now: float = 0.0
    user: str = ""
    count: int = 0
    #: Whether the named areas / geofences were looked up (the built-in CSV then has their columns).
    places: bool = False
    fences: bool = False


def raw_value(item: Optional[ExportItem], col: ExportColumn, index: int, ctx: ExportContext, as_object: bool = False) -> Any:
    """A source's value before maps and formats (times ms, numbers float); _MISSING for none."""
    s = col.source
    f = ctx.filter
    files = {
        "count": ctx.count, "exportedAt": ctx.now, "exportedBy": ctx.user, "periodStart": f.start, "periodEnd": f.end,
        "periodBasis": BASES.get(f.basis, f.basis), "areaName": f.area_name, "format": ctx.format.name,
    }
    if s in files:
        return files[s]
    if item is None:
        return _MISSING
    o = item.record
    if s == "kind":
        p = path_of(o.kind)
        if col.level is None:
            return kind_of(p)
        i = col.level - 1 if col.level > 0 else len(p) + col.level
        return p[i] if 0 <= i < len(p) else ""
    digits = col.digits if col.digits is not None else ctx.format.digits
    if s == "geometry":
        kind = col.geometry or ("geojson" if as_object else "wkt")
        lon, lat = round(o.lon, digits), round(o.lat, digits)
        if kind == "geojson":
            value = {"type": "Point", "coordinates": [lon, lat]}
            return value if as_object else json.dumps(value, separators=(",", ":"))
        if kind == "latlon":
            return f"{to_fixed(o.lat, digits)},{to_fixed(o.lon, digits)}"
        return f"POINT ({to_fixed(o.lon, digits)} {to_fixed(o.lat, digits)})"
    point = (o.lon, o.lat)
    places = of_kinds(item.places, col.place_kinds) if col.place_kinds else item.places
    return {
        "id": o.id, "objectId": o.object_id, "objectName": o.object_name, "activity": o.activity, "present": o.present,
        "confidence": o.confidence or "", "imageTime": o.image_time, "imageId": o.image_id, "imageName": o.image_name,
        "lon": o.lon, "lat": o.lat, "length": o.length, "heading": o.heading, "width": o.width, "note": o.note,
        "recordedBy": o.recorded_by or "", "updatedBy": o.updated_by or "", "created": o.created_at, "updated": o.updated_at,
        "deleted": o.deleted_at,
        "place": places[0].name if places else "", "placeKind": places[0].kind if places else "",
        "places": "・".join(dict.fromkeys(a.name for a in places)), "geofences": "・".join(item.fences),
        "latLon": format_lat_lon(point), "dms": format_dms(point), "mgrs": format_mgrs(point) or "", "utm": format_utm(point) or "",
        "index": index + 1,
    }.get(s or "", _MISSING)


def shaped(value: Any, col: ExportColumn, ctx: ExportContext, as_text: bool) -> Any:
    """A value with its map, format, decimals, prefix / suffix and empty text."""
    if value is _MISSING:
        return value
    s = col.source
    if col.map:
        key = js_string(value).lower() if isinstance(value, bool) else js_string(value)
        if key in col.map:
            value = col.map[key]
    if s in TIME_SOURCES and is_number(value):
        value = format_time(value, col.format or ctx.format.date_format, ctx.format.time_zone)
    elif s in NUMBER_SOURCES and is_number(value) and not isinstance(value, bool):
        if not math.isfinite(value):
            value = None
        elif col.format:
            value = printf(col.format, value)
        else:
            digits = col.digits if col.digits is not None else (ctx.format.digits if s in ("lon", "lat") else 1)
            value = round(value, digits) if not as_text else to_fixed(value, digits)
    elif s == "present" and isinstance(value, bool) and as_text:
        value = "あり" if value else "なし"
    elif col.format and is_number(value) and not isinstance(value, bool):
        value = printf(col.format, value)
    empty = value is None or value == ""
    if empty:
        return col.empty if col.empty is not None else ("" if as_text else value)
    if col.prefix or col.suffix:
        value = col.prefix + js_string(value) + col.suffix
    return value


def column_value(item: Optional[ExportItem], col: ExportColumn, index: int, ctx: ExportContext, as_text: bool,
                 as_object: bool = False) -> Any:
    if col.source is None:
        return col.value
    return shaped(raw_value(item, col, index, ctx, as_object), col, ctx, as_text)


def _cell(v: Any) -> str:
    text = "" if v is None else js_string(v) if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)
    # Notes and names are other people's text: never a formula in a spreadsheet.
    return formula_safe(text)


def csv_text(items: Sequence[ExportItem], ctx: ExportContext) -> str:
    """The CSV of a format's columns."""
    fmt = ctx.format
    out = io.StringIO()
    writer = csv.writer(out, delimiter=fmt.delimiter, lineterminator=fmt.newline)
    if fmt.header:
        writer.writerow([c.key for c in fmt.columns])
    for i, item in enumerate(items):
        writer.writerow([_cell(column_value(item, c, i, ctx, as_text=True)) for c in fmt.columns])
    return out.getvalue()


def build(schema: Any, root: dict, item: Optional[ExportItem], index: int, items: Sequence[ExportItem], ctx: ExportContext) -> Any:
    """The value a schema (with `x-source`) asks for; _MISSING for nothing."""
    schema = resolve(schema, root)
    if not isinstance(schema, dict):
        return _MISSING
    source = schema.get("x-source")
    if source == "records":
        sub = schema.get("items") or {}
        return [v for v in (build(sub, root, it, i, items, ctx) for i, it in enumerate(items)) if v is not _MISSING]
    if isinstance(source, str):
        mapping = schema.get("x-map") if is_record(schema.get("x-map")) else {}
        col = ExportColumn(
            key="", source=source, format=schema.get("x-format") if isinstance(schema.get("x-format"), str) else None,
            map={str(k): v for k, v in mapping.items()},
            digits=schema.get("x-digits") if isinstance(schema.get("x-digits"), int) else None,
            level=schema.get("x-level") if isinstance(schema.get("x-level"), int) and schema.get("x-level") else None,
            geometry=schema.get("x-geometry") if schema.get("x-geometry") in GEOMETRIES else None,
            empty=schema.get("x-empty"),
            place_kinds=place_kinds_of(schema.get("x-placeKinds"), "", [], "x-placeKinds") if source in PLACE_SOURCES else (),
        )
        types = schema.get("type")
        types = types if isinstance(types, list) else [types] if types else []
        v = column_value(item, col, index, ctx, as_text=types == ["string"], as_object=True)
        return v if v is _MISSING else _coerce(v, schema)
    if "const" in schema:
        return schema["const"]
    if "default" in schema and schema.get("x-default", True):
        return schema["default"]
    props = schema.get("properties")
    if isinstance(props, dict):
        out = {}
        for name, sub in props.items():
            v = build(sub, root, item, index, items, ctx)
            if v is _MISSING:
                continue
            t = resolve(sub, root).get("type") if isinstance(resolve(sub, root), dict) else None
            if v is None and not (t is None or t == "null" or isinstance(t, list) and "null" in t) and name not in (schema.get("required") or []):
                continue
            out[name] = v
        return out if out or schema.get("required") else _MISSING
    return _MISSING


def json_text(items: Sequence[ExportItem], ctx: ExportContext, schema: Optional[dict] = None) -> tuple[str, list[str]]:
    """The JSON: an array of the columns, or the schema's value (and what the schema finds wrong with it)."""
    if schema is None:
        value: Any = [{c.key: column_value(it, c, i, ctx, as_text=False, as_object=True) for c in ctx.format.columns}
                      for i, it in enumerate(items)]
        errors: list[str] = []
    else:
        value = build(schema, schema, None, 0, items, ctx)
        if value is _MISSING:
            value = {}
        errors = validate(value, schema)
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n", errors


def file_name_of(fmt: ExportFormat, now: float) -> str:
    """The file's name: `{now:%Y%m%d}` (when written, the format's time zone), `{name}`, `{ext}`."""
    clock = datetime.fromtimestamp(clock_of(now, fmt.time_zone) / 1000, tz=timezone.utc)

    def sub(m: re.Match) -> str:
        key, f = m.group(1), m.group(2)
        if key in ("now", "date"):
            return clock.strftime(f or "%Y%m%d")
        if key == "name":
            return fmt.name
        if key == "ext":
            return fmt.format
        return m.group(0)

    name = re.sub(r"\{(\w+)(?::([^}]*))?\}", sub, fmt.file_name)
    return re.sub(r'[\\/:*?"<>|]', "_", name)


@dataclass
class Exported:
    """A file: its text, its bytes, and what was found wrong."""
    file_name: str
    text: str
    data: bytes
    count: int
    errors: list[str] = field(default_factory=list)


SchemaLoader = Callable[[str], dict]


def render(items: Sequence[ExportItem], ctx: ExportContext, load_schema: Optional[SchemaLoader] = None) -> Exported:
    """The file of the records in a format."""
    fmt = ctx.format
    ctx.count = len(items)
    name = file_name_of(fmt, ctx.now)
    errors: list[str] = []
    if fmt.builtin and fmt.format == "csv":
        text = observations_csv(
            [it.record for it in items],
            places={it.record.id: it.places for it in items} if ctx.places else None,
            fences={it.record.id: it.fences for it in items} if ctx.fences else None,
        )
    elif fmt.format == "json":
        schema = fmt.schema
        if isinstance(schema, str):
            try:
                schema = load_schema(schema) if load_schema else None
            except (OSError, ValueError) as error:
                return Exported(name, "", b"", len(items), [f"スキーマ {fmt.schema} の読み込み不可（{error}）"])
            if not isinstance(schema, dict):
                return Exported(name, "", b"", len(items), [f"スキーマ {fmt.schema} の読み込み不可"])
        text, errors = json_text(items, ctx, schema)
    else:
        text = csv_text(items, ctx)
    # The built-in CSV's text has its BOM already; utf-8-sig adds one.
    body = text[1:] if text.startswith("\ufeff") and fmt.encoding == "utf-8-sig" else text
    try:
        data = body.encode(fmt.encoding)
    except UnicodeEncodeError:
        data = body.encode(fmt.encoding, errors="replace")
        errors.append(f"{fmt.encoding} で書けない文字を ? に置き換え")
    return Exported(name, text, data, len(items), errors)

