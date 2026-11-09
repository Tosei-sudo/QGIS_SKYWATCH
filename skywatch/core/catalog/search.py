"""
Searching an image catalog (the viewer's catalog.ts): the layer's field
types, the sensors to choose from, the images matching a search (newest
first), their roles, their order in the list and the satellite's view for
the simple orthorectification. Features stay Esri JSON in WGS 84.

A search reads `maxResults` images at a time, newest first, in as many
requests as the service's `maxRecordCount` needs (`resultOffset`), and
「続きを読む」 asks for the next `maxResults` from where the last one ended.
"""
from __future__ import annotations

import functools
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..esri import EsriClient, EsriError, UnreachableError
from ..plan.output import natural_key
from ..time_query import text_sample
from ..util import js_string, time_of
from .config import CatalogConfig
from .query import CatalogSearch, FieldTypes, time_text, timed_text_fields, where_of


@dataclass(frozen=True)
class CatalogSource:
    """Where an image is: a URL (`url`) opened as a COG, or a path (`path`) on this computer or the network."""
    kind: str
    value: str


def source_of(value: Any) -> Optional[CatalogSource]:
    """A COG URL (http, https or protocol-relative); anything else (`\\\\nas\\…`, `C:\\…`, `/mnt/…`, `file://…`) is a local path."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.match(r"^(https?:)?//[^/]", text, re.IGNORECASE):
        return CatalogSource("url", text)
    return CatalogSource("path", text)


@dataclass(eq=False)
class CatalogRecord:
    """One image of the catalog: its feature (Esri JSON, WGS 84) and its roles."""
    attributes: dict[str, Any]
    geometry: Optional[dict[str, Any]]
    id: str
    acquired: Any
    registered: Any
    system_registered: Any
    sensor: str
    angle: Optional[float]
    azimuth: Optional[float]
    source: Optional[CatalogSource]
    object_id: Any = None

    def feature(self) -> dict[str, Any]:
        """The record as an Esri feature (for keeping in a notice or on a layer)."""
        return {"attributes": self.attributes, **({"geometry": self.geometry} if self.geometry else {})}


def _degrees(v: Any) -> Optional[float]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def record_of(feature: dict[str, Any], fields: dict[str, Any], object_id_field: Optional[str] = None) -> CatalogRecord:
    """The roles of a feature's attributes."""
    a = feature.get("attributes") or {}

    def get(role: str) -> Any:
        name = fields.get(role)
        return a.get(name) if name else None

    oid = a.get(object_id_field) if object_id_field else None
    if oid is None:
        oid = next((a[k] for k in ("OBJECTID", "objectid", "FID") if a.get(k) is not None), None)
    image_id = get("id")
    if image_id is None:
        image_id = oid
    sensor = get("sensor")
    sources = [source_of(a.get(n)) for n in fields.get("source") or []]
    return CatalogRecord(
        attributes=a,
        geometry=feature.get("geometry"),
        id=js_string(image_id) if image_id is not None else "",
        acquired=get("acquired"),
        registered=get("registered"),
        system_registered=get("systemRegistered"),
        sensor="" if sensor is None else js_string(sensor),
        angle=_degrees(get("angle")),
        azimuth=_degrees(get("azimuth")),
        source=next((s for s in sources if s is not None), None),
        object_id=oid,
    )


@dataclass
class CatalogLayerInfo:
    """
    The catalog layer's field types, whether it can order and page results,
    its object id field and how many features one answer holds at most.
    """
    types: FieldTypes
    paging: bool
    object_id_field: str = "OBJECTID"
    max_record_count: int = 1000
    #: A value of each text date field asked with a time (`text_samples`), by name; "" for none read.
    samples: dict[str, str] = field(default_factory=dict)


def read_catalog_layer(client: EsriClient, catalog: CatalogConfig, token: Optional[str]) -> CatalogLayerInfo:
    info = client.layer(catalog.url, token)
    caps = info.raw.get("advancedQueryCapabilities")
    paging = caps.get("supportsPagination") is not False and caps.get("supportsOrderBy") is not False if isinstance(caps, dict) else True
    return CatalogLayerInfo(info.field_types, paging, info.object_id_field, info.max_record_count)


def sensors_of(client: EsriClient, catalog: CatalogConfig, token: Optional[str]) -> list[str]:
    """The values of the sensor field, for the search's choices; empty when the service cannot list them."""
    name = catalog.fields.get("sensor")
    if not name:
        return []
    try:
        answer = client.ask(f"{catalog.url}/query", {
            "where": catalog.where or "1=1", "outFields": name, "returnDistinctValues": "true",
            "returnGeometry": "false", "orderByFields": name,
        }, token)
    except EsriError:
        return []
    values = {js_string(v) for v in ((f.get("attributes") or {}).get(name) for f in answer.get("features") or []) if v is not None and v != ""}
    return sorted(values, key=natural_key)


@dataclass
class CatalogResult:
    """
    At most `max_results` images, newest first, and how many match in all
    (None when unknown). `next_offset`: where 「続きを読む」 asks from (the
    images the service gave so far), None when there are no more (or the
    service cannot page).
    """
    records: list[CatalogRecord]
    total: Optional[int]
    next_offset: Optional[int] = None


def _clamp(v: float, limit: float) -> float:
    return max(-limit, min(limit, v))


#: The Esri geometry types of the shapes a search can be over, by their key.
_SHAPES = (("rings", "esriGeometryPolygon"), ("paths", "esriGeometryPolyline"), ("points", "esriGeometryMultipoint"))


def spatial_filter(box: Optional[Any]) -> dict[str, str]:
    """
    The spatial part of a query over `box`: `[xmin, ymin, xmax, ymax]`, or an
    Esri polygon `{"rings": …}`, polyline `{"paths": …}` or multipoint
    `{"points": …}` (WGS 84), the image footprints intersecting it; {} for anywhere.
    """
    if box is None:
        return {}
    if isinstance(box, dict):
        for key, kind in _SHAPES:
            if box.get(key):
                return {"geometry": json.dumps({key: box[key], "spatialReference": {"wkid": 4326}}),
                        "geometryType": kind, "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
        return {}
    xmin, ymin, xmax, ymax = box
    return {
        "geometry": json.dumps({"xmin": _clamp(xmin, 180), "ymin": _clamp(ymin, 90), "xmax": _clamp(xmax, 180), "ymax": _clamp(ymax, 90), "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
    }


def text_samples(client: EsriClient, catalog: CatalogConfig, layer: CatalogLayerInfo, search: CatalogSearch,
                 token: Optional[str]) -> dict[str, str]:
    """
    A value of each text date field whose period has a time (read once per
    layer), so that the times are written as the field writes them (`T` or
    a space, its offset). Date fields need none: they compare timestamps.
    """
    for name in timed_text_fields(catalog, layer.types, search):
        if name not in layer.samples:
            layer.samples[name] = text_sample(client, catalog.url, name, layer.types.get(name), token, catalog.where)
    return layer.samples


def search_catalog(client: EsriClient, catalog: CatalogConfig, layer: CatalogLayerInfo, search: CatalogSearch,
                   box: Optional[Any], token: Optional[str], offset: int = 0,
                   cancelled: Optional[Callable[[], bool]] = None) -> CatalogResult:
    """
    The images matching `search` (over `box` when given: see
    `spatial_filter`; the map's extent, the area drawn or a layer's
    geometry), newest first: `max_results` of them from `offset` on, asked
    `maxRecordCount` at a time (a service that answers 1000 at most still
    gives 5000 when `maxResults` says so). Ties in time go by object id, so
    that the pages neither skip nor repeat images. Services that cannot
    order or page results are asked for everything (what one answer holds)
    and sorted here; they have no 「続き」.
    """
    where = where_of(catalog, layer.types, search, text_samples(client, catalog, layer, search, token))
    spatial = spatial_filter(box)
    order = catalog.fields.get("acquired") or catalog.fields.get("registered")
    base = {"where": where, **spatial, "outFields": "*", "returnGeometry": "true", "outSR": "4326"}
    url = f"{catalog.url}/query"
    try:
        count = client.ask(url, {"where": where, **spatial, "returnCountOnly": "true"}, token).get("count")
        total: Optional[int] = int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None
    except EsriError:
        total = None
    features: list[dict[str, Any]] = []
    oid = layer.object_id_field
    next_offset: Optional[int] = None
    if layer.paging:
        try:
            features, more, oid = _pages(client, url, base, order, layer, catalog.max_results, offset, token, cancelled)
            at = offset + len(features)
            next_offset = at if more and (total is None or at < total) else None
        except _Refused:
            # Older services refuse ordering or paging: ask plainly.
            layer.paging = False
    if not layer.paging:
        answer = client.ask(url, base, token)
        oid = answer.get("objectIdFieldName") or layer.object_id_field
        features = [f for f in answer.get("features") or [] if isinstance(f, dict)]
        if total is None and not answer.get("exceededTransferLimit"):
            total = len(features)
    records = [record_of(f, catalog.fields, oid) for f in features]
    if not layer.paging:
        records = sort_records(records, "acquired", True)[:catalog.max_results]
    if total is None and next_offset is None and layer.paging:
        # Not counted, but every one read: they are all.
        total = offset + len(features)
    return CatalogResult(records, total, next_offset)


class _Refused(EsriError):
    """The first answer of a new search refused ordering or paging (not a lost connection)."""


def _pages(client: EsriClient, url: str, base: dict[str, str], order: Optional[str], layer: CatalogLayerInfo, wanted: int,
           offset: int, token: Optional[str], cancelled: Optional[Callable[[], bool]]) -> tuple[list[dict[str, Any]], bool, str]:
    """
    Up to `wanted` features from `offset`, newest first, in answers of
    `maxRecordCount` at most; whether more may follow; the object id field.
    The first answer of a new search (`offset` 0) refused: `_Refused`.
    """
    oid = layer.object_id_field
    ordering = f"{order} DESC, {oid} DESC" if order else f"{oid} DESC"
    size = max(1, min(wanted, layer.max_record_count))
    out: list[dict[str, Any]] = []
    more = False
    while len(out) < wanted:
        if cancelled and cancelled():
            raise EsriError("取消")
        count = min(size, wanted - len(out))
        params = {**base, "orderByFields": ordering, "resultOffset": str(offset + len(out)), "resultRecordCount": str(count)}
        try:
            answer = client.ask(url, params, token)
        except UnreachableError:
            raise
        except EsriError as error:
            if offset == 0 and not out:
                raise _Refused(str(error)) from None
            raise
        oid = answer.get("objectIdFieldName") or oid
        page = [f for f in answer.get("features") or [] if isinstance(f, dict)]
        out.extend(page)
        # Servers that page say so; some leave the flag out on a full page. One answering fewer than asked
        # with the flag (its own limit lower than it says) is asked on from where it stopped.
        more = bool(page) and (bool(answer.get("exceededTransferLimit")) or len(page) >= count)
        if not more:
            break
    return out, more, oid


#: The roles that are times.
TIME_KEYS = ("acquired", "registered", "systemRegistered")


def _role_value(record: CatalogRecord, role: str) -> Any:
    return {"acquired": record.acquired, "registered": record.registered, "systemRegistered": record.system_registered}[role]


def sort_value(record: CatalogRecord, key: str) -> Any:
    """The value of a record for a sort key (a role or `column:<field>`); None when empty (always last)."""
    if key in TIME_KEYS:
        t = time_of(_role_value(record, key))
        return None if math.isnan(t) else t
    if key in ("angle", "azimuth"):
        return getattr(record, key)
    if key in ("id", "sensor"):
        return getattr(record, key) or None
    v = record.attributes.get(key[len("column:"):]) if key.startswith("column:") else None
    if v is None or v == "":
        return None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else js_string(v)


def compare_values(x: Any, y: Any) -> int:
    """Numbers as numbers, text in natural order."""
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return (x > y) - (x < y)
    a, b = natural_key(js_string(x)), natural_key(js_string(y))
    return (a > b) - (a < b)


def sort_records(records: Sequence[CatalogRecord], key: str, descending: bool) -> list[CatalogRecord]:
    """The records ordered by `key`; empty values last whichever way."""
    def compare(a: CatalogRecord, b: CatalogRecord) -> int:
        x, y = sort_value(a, key), sort_value(b, key)
        if x is None or y is None:
            return 0 if x is y else (1 if x is None else -1)
        order = compare_values(x, y)
        return -order if descending else order

    return sorted(records, key=functools.cmp_to_key(compare))


EARTH_RADIUS = 6371.0


def sensor_view_of(record: CatalogRecord, catalog: CatalogConfig) -> Optional[dict[str, float]]:
    """
    Where the satellite was seen from the ground, for the simple orthorectification: the azimuth as it is,
    the elevation from the angle by `angle_kind` (an off-nadir angle through the Earth's curvature
    at `orbit_height`). None without both.
    """
    azimuth, angle = record.azimuth, record.angle
    if azimuth is None or angle is None:
        return None
    if catalog.angle_kind == "elevation":
        incidence = 90 - angle
    elif catalog.angle_kind == "incidence":
        incidence = angle
    else:
        sine = (EARTH_RADIUS + catalog.orbit_height) / EARTH_RADIUS * math.sin(math.radians(abs(angle)))
        if sine >= 1:
            return None
        incidence = math.degrees(math.asin(sine))
    elevation = math.floor((90 - abs(incidence)) * 100 + 0.5) / 100
    if not 0 < elevation <= 90:
        return None
    return {"azimuth": math.floor((azimuth % 360) * 100 + 0.5) / 100, "elevation": elevation}


def catalog_info(record: CatalogRecord, catalog: CatalogConfig) -> list[tuple[str, str]]:
    """The rows of an opened image's information: the catalog and the roles and columns with a value."""
    f = catalog.fields
    rows = [("カタログ", catalog.label)]
    if f.get("id") and record.id:
        rows.append((catalog.label_of("id"), record.id))
    for role in TIME_KEYS:
        value = _role_value(record, role)
        if f.get(role) and value is not None:
            rows.append((catalog.label_of(role), time_text(value)))
    if f.get("sensor") and record.sensor:
        rows.append((catalog.label_of("sensor"), record.sensor))
    if f.get("angle") and record.angle is not None:
        rows.append((catalog.label_of("angle"), f"{js_string(record.angle)}°"))
    if f.get("azimuth") and record.azimuth is not None:
        rows.append((catalog.label_of("azimuth"), f"{js_string(record.azimuth)}°"))
    for c in catalog.columns:
        v = record.attributes.get(c["field"])
        if v is not None and v != "":
            rows.append((c["label"], js_string(v)))
    return rows


@dataclass(frozen=True)
class Column:
    """A column of the result list: its sort key (a role or `column:<field>`) and heading."""
    key: str
    label: str


def list_columns(catalog: CatalogConfig) -> list[Column]:
    """The list's columns: the roles the catalog maps (画像ID・撮像日時・登録日時・システム登録日時・センサー・角度・方位角), then its `columns`."""
    roles = [Column(r, catalog.label_of(r)) for r in ("id", "acquired", "registered", "systemRegistered", "sensor", "angle", "azimuth") if catalog.fields.get(r)]
    return roles + [Column(f"column:{c['field']}", c["label"]) for c in catalog.columns]


def angle_text(v: Optional[float]) -> str:
    return "" if v is None else f"{js_string(math.floor(v * 10 + 0.5) / 10)}°"


def cell_text(record: CatalogRecord, key: str) -> str:
    """A record's text in a column."""
    if key in TIME_KEYS:
        return time_text(_role_value(record, key))
    if key in ("angle", "azimuth"):
        return angle_text(getattr(record, key))
    if key in ("id", "sensor"):
        return getattr(record, key)
    v = record.attributes.get(key[len("column:"):]) if key.startswith("column:") else None
    return "" if v is None else js_string(v)


def place_text(record: CatalogRecord) -> str:
    """Where the image is, for the list: 「COG」・「ローカル」・「なし」."""
    return {"url": "COG", "path": "ローカル"}.get(record.source.kind if record.source else "", "なし")


def footprint_box(geometry: Optional[dict[str, Any]]) -> Optional[list[float]]:
    """[west, south, east, north] of an Esri polygon or envelope (WGS 84), None without one."""
    if not isinstance(geometry, dict):
        return None
    points = [p for ring in geometry.get("rings") or [] for p in ring
              if isinstance(p, (list, tuple)) and len(p) >= 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p[:2])]
    if points:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return [min(xs), min(ys), max(xs), max(ys)]
    corners = [geometry.get(k) for k in ("xmin", "ymin", "xmax", "ymax")]
    if all(isinstance(v, (int, float)) and math.isfinite(v) for v in corners):
        return [float(v) for v in corners]
    return None


def listed_image(record: CatalogRecord):
    """
    「ファイルなしで記録」: a record as an image of 観測記録 without its file
    (its raw data in another system only): the catalog's id (the same as the
    image opened from the catalog would have), its time and its footprint's
    extent; None without an id.
    """
    from ..obs.model import ListedImage
    from ..util import ms_of

    if not record.id:
        return None
    return ListedImage(record.id, record.id, ms_of(record.acquired), footprint_box(record.geometry))
