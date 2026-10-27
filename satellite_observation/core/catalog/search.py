"""
Searching an image catalog (the viewer's catalog.ts): the layer's field
types, the sensors to choose from, the images matching a search (newest
first), their roles, their order in the list and the satellite's view for
the simple orthorectification. Features stay Esri JSON in WGS 84.
"""
from __future__ import annotations

import functools
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from ..esri import EsriClient, EsriError
from ..plan.output import natural_key
from ..util import js_string, time_of
from .config import CatalogConfig
from .query import CatalogSearch, FieldTypes, time_text, where_of


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
    """The catalog layer's field types, whether it can order and page results, and its object id field."""
    types: FieldTypes
    paging: bool
    object_id_field: str = "OBJECTID"


def read_catalog_layer(client: EsriClient, catalog: CatalogConfig, token: Optional[str]) -> CatalogLayerInfo:
    info = client.layer(catalog.url, token)
    caps = info.raw.get("advancedQueryCapabilities")
    paging = caps.get("supportsPagination") is not False and caps.get("supportsOrderBy") is not False if isinstance(caps, dict) else True
    return CatalogLayerInfo(info.field_types, paging, info.object_id_field)


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
    """At most `max_results` images, newest first, and how many match in all (None when unknown)."""
    records: list[CatalogRecord]
    total: Optional[int]


def _clamp(v: float, limit: float) -> float:
    return max(-limit, min(limit, v))


def search_catalog(client: EsriClient, catalog: CatalogConfig, layer: CatalogLayerInfo, search: CatalogSearch,
                   box: Optional[Any], token: Optional[str]) -> CatalogResult:
    """
    The images matching `search` (over `box` when given: `[xmin, ymin, xmax,
    ymax]` or an Esri polygon `{"rings": …}` drawn on the map, WGS 84), newest
    first. Services that cannot order or page results are asked for
    everything and sorted here.
    """
    where = where_of(catalog, layer.types, search)
    spatial: dict[str, str] = {}
    if isinstance(box, dict):
        spatial = {
            "geometry": json.dumps({"rings": box["rings"], "spatialReference": {"wkid": 4326}}),
            "geometryType": "esriGeometryPolygon", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        }
    elif box is not None:
        xmin, ymin, xmax, ymax = box
        spatial = {
            "geometry": json.dumps({"xmin": _clamp(xmin, 180), "ymin": _clamp(ymin, 90), "xmax": _clamp(xmax, 180), "ymax": _clamp(ymax, 90), "spatialReference": {"wkid": 4326}}),
            "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        }
    order = catalog.fields.get("acquired") or catalog.fields.get("registered")
    base = {"where": where, **spatial, "outFields": "*", "returnGeometry": "true", "outSR": "4326"}
    url = f"{catalog.url}/query"
    try:
        count = client.ask(url, {"where": where, **spatial, "returnCountOnly": "true"}, token).get("count")
        total: Optional[int] = int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None
    except EsriError:
        total = None
    if layer.paging:
        try:
            answer = client.ask(url, {**base, **({"orderByFields": f"{order} DESC"} if order else {}), "resultRecordCount": str(catalog.max_results)}, token)
        except EsriError:
            # Older services refuse ordering or paging: ask plainly.
            layer.paging = False
            answer = client.ask(url, base, token)
    else:
        answer = client.ask(url, base, token)
    oid = answer.get("objectIdFieldName") or layer.object_id_field
    records = [record_of(f, catalog.fields, oid) for f in answer.get("features") or [] if isinstance(f, dict)]
    if not layer.paging:
        records = sort_records(records, "acquired", True)[:catalog.max_results]
    return CatalogResult(records, total if total is not None else (None if answer.get("exceededTransferLimit") else len(records)))


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
