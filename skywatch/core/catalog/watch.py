"""
Watches on image catalogs (the viewer's catalog-watch.ts and
watch-areas.ts): conditions saved in advance (points or areas, the sensor,
an angle limit, a SQL condition) that the catalog is asked again and again,
each image that newly matches becoming a notice in 「通知」.

QGIS checks while it is open, as it starts (for what came while it was
closed) and every `watchInterval` minutes. Watches and notices are kept in
the local GeoPackage (watch_store.py). A watch keeps its catalog without the
token: the settings file's catalog of the same URL (and its token) is used
to ask.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Sequence

from ..util import js_string, new_id, time_of, utc_text
from .config import CatalogConfig
from .query import EMPTY_SEARCH, CatalogSearch, FieldTypes, where_of

#: How many image keys a watch remembers.
MAX_SEEN = 5000
#: The most images one check asks for.
MAX_FOUND = 1000
#: The most notices kept; the oldest go first.
MAX_NOTICES = 500
DAY = 24 * 60 * 60 * 1000

#: Asks an Esri layer (`url`, form params) for JSON with the catalog's sign-in; raises with the reason when it cannot.
EsriAsk = Callable[[str, dict], dict]


def conditions_of(search: CatalogSearch) -> CatalogSearch:
    """The conditions of a watch: the search's sensor, angle, azimuth and SQL, without its dates (what is new matters)."""
    return replace(EMPTY_SEARCH, sensor=search.sensor, max_angle=search.max_angle, azimuth_from=search.azimuth_from,
                   azimuth_to=search.azimuth_to, where=search.where)


def _number(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _conditions_from(d: Any) -> CatalogSearch:
    d = d if isinstance(d, dict) else {}
    return replace(EMPTY_SEARCH, sensor=str(d.get("sensor") or ""), max_angle=_number(d.get("maxAngle")),
                   azimuth_from=_number(d.get("azimuthFrom")), azimuth_to=_number(d.get("azimuthTo")), where=str(d.get("where") or ""))


@dataclass
class CatalogWatch:
    id: str
    name: str
    #: The catalog as the settings file had it when the watch was made (without its token).
    catalog: CatalogConfig
    #: The catalog layer's field types, for the date condition.
    types: FieldTypes
    conditions: CatalogSearch
    #: `{"kind": "points", "points": [[lon, lat], …]}` or `{"kind": "polygon", "rings": […]}`, asked one after another; empty: anywhere.
    areas: list[dict[str, Any]]
    enabled: bool
    created: float
    #: When the catalog was last asked; None before the first check (which only notes what is already there).
    last_checked: Optional[float] = None
    #: When a check was last tried (worked or not), for when the next is due.
    last_tried: Optional[float] = None
    #: Why the last check failed; None when it worked.
    last_error: Optional[str] = None
    #: The keys of the images already seen, oldest first.
    seen: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        c = self.conditions
        return {
            "id": self.id, "name": self.name, "catalog": self.catalog.to_json(), "types": self.types,
            "conditions": {"sensor": c.sensor, "maxAngle": c.max_angle, "azimuthFrom": c.azimuth_from, "azimuthTo": c.azimuth_to, "where": c.where},
            "areas": self.areas, "enabled": self.enabled, "created": self.created, "lastChecked": self.last_checked,
            "lastTried": self.last_tried, "lastError": self.last_error, "seen": self.seen,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "CatalogWatch":
        return CatalogWatch(
            id=str(d["id"]), name=str(d.get("name") or ""), catalog=CatalogConfig.from_json(d.get("catalog") or {}),
            types=dict(d.get("types") or {}), conditions=_conditions_from(d.get("conditions")), areas=list(d.get("areas") or []),
            enabled=d.get("enabled") is not False, created=float(d.get("created") or 0), last_checked=d.get("lastChecked"),
            last_tried=d.get("lastTried"), last_error=d.get("lastError"), seen=[str(s) for s in d.get("seen") or []],
        )


@dataclass
class WatchNotice:
    """An image that newly matched a watch (or, `kind` 「plan」, a reminder of a planned shot)."""
    #: `watch id|image key`.
    id: str
    watch_id: str
    watch_name: str
    catalog_label: str
    #: When it was found (ms).
    found: float
    read: bool
    image_id: str
    acquired: Any
    sensor: str
    #: The image as an Esri feature (WGS 84).
    feature: dict[str, Any]
    kind: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "watchId": self.watch_id, "watchName": self.watch_name, "catalogLabel": self.catalog_label,
                "found": self.found, "read": self.read, "imageId": self.image_id, "acquired": self.acquired,
                "sensor": self.sensor, "feature": self.feature, "kind": self.kind}

    @staticmethod
    def from_json(d: dict[str, Any]) -> "WatchNotice":
        return WatchNotice(
            id=str(d["id"]), watch_id=str(d.get("watchId") or ""), watch_name=str(d.get("watchName") or ""),
            catalog_label=str(d.get("catalogLabel") or ""), found=float(d.get("found") or 0), read=bool(d.get("read")),
            image_id=str(d.get("imageId") or ""), acquired=d.get("acquired"), sensor=str(d.get("sensor") or ""),
            feature=d.get("feature") or {}, kind=d.get("kind"),
        )


def newness_of(catalog: CatalogConfig) -> Optional[tuple[str, float]]:
    """The field telling how new an image is, and how far back a check looks before its last one (registration can lag)."""
    f = catalog.fields
    name = f.get("systemRegistered") or f.get("registered")
    if name:
        return name, 3 * DAY
    # Images are often registered days after they were taken.
    if f.get("acquired"):
        return f["acquired"], 30 * DAY
    return None


def since_condition(name: str, field_type: Optional[str], t: float) -> str:
    """`name` at or after `t`, written for the field's type."""
    if field_type == "esriFieldTypeDateOnly":
        return f"{name} >= DATE '{utc_text(t)[:10]}'"
    if field_type and re.search(r"Integer|Double|Single", field_type):
        return f"{name} >= {math.floor(t)}"
    if field_type == "esriFieldTypeString":
        return f"{name} >= '{utc_text(t)[:10]}'"
    return f"{name} >= TIMESTAMP '{utc_text(t)}'"


def watch_query(watch: CatalogWatch, since: Optional[float], area: Any = "first") -> dict[str, str]:
    """The query of one check over `area` (the watch's first by default, anywhere when None): its conditions and the images since `since` (ms)."""
    if area == "first":
        area = watch.areas[0] if watch.areas else None
    parts = [w for w in [where_of(watch.catalog, watch.types, watch.conditions)] if w != "1=1"]
    newness = newness_of(watch.catalog)
    if newness and since is not None:
        parts.append(since_condition(newness[0], watch.types.get(newness[0]), since - newness[1]))
    params = {"where": " AND ".join(parts) if parts else "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326"}
    if area:
        one = area["kind"] == "points" and len(area["points"]) == 1
        if one:
            geometry = {"x": area["points"][0][0], "y": area["points"][0][1], "spatialReference": {"wkid": 4326}}
        elif area["kind"] == "points":
            geometry = {"points": area["points"], "spatialReference": {"wkid": 4326}}
        else:
            geometry = {"rings": area["rings"], "spatialReference": {"wkid": 4326}}
        params.update(
            geometry=json.dumps(geometry),
            geometryType="esriGeometryPoint" if one else "esriGeometryMultipoint" if area["kind"] == "points" else "esriGeometryPolygon",
            inSR="4326", spatialRel="esriSpatialRelIntersects",
        )
    return params


def _key_of(feature: dict[str, Any], catalog: CatalogConfig, object_id_field: Optional[str]) -> str:
    """The key that tells one image from another: its id, else its object id."""
    a = feature.get("attributes") or {}
    value = a.get(catalog.fields["id"]) if catalog.fields.get("id") else None
    for k in (object_id_field, "OBJECTID", "FID", "objectid"):
        if value in (None, "") and k:
            value = a.get(k)
    return json.dumps(a, ensure_ascii=False, sort_keys=True) if value in (None, "") else js_string(value)


@dataclass
class CheckResult:
    """What one check found: the watch as it is after it, and its new notices."""
    watch: CatalogWatch
    notices: list[WatchNotice]


def check_watch(watch: CatalogWatch, ask: EsriAsk, now: Optional[float] = None) -> CheckResult:
    """
    Asks the catalog once for `watch`. The first check only notes the images
    already there; later ones turn each image not seen before into a notice.
    A failure is kept in `last_error` (and `last_checked` stays as it was, so
    nothing is missed once it works again).
    """
    now = time.time() * 1000 if now is None else now
    catalog = watch.catalog
    since = watch.last_checked if watch.last_checked is not None else watch.created
    newness = newness_of(catalog)
    url = f"{catalog.url}/query"
    answers: list[dict[str, Any]] = []
    try:
        # Points and polygons are separate geometries of the query: one question each.
        for area in watch.areas or [None]:
            params = watch_query(watch, since, area)
            try:
                answers.append(ask(url, {**params, **({"orderByFields": f"{newness[0]} DESC"} if newness else {}), "resultRecordCount": str(MAX_FOUND)}))
            except Exception:  # noqa: BLE001
                # Older services refuse ordering or paging.
                answers.append(ask(url, params))
    except Exception as error:  # noqa: BLE001
        return CheckResult(replace(watch, last_tried=now, last_error=str(error) or type(error).__name__), [])
    seen = set(watch.seen)
    keys: list[str] = []
    notices: list[WatchNotice] = []
    for answer in answers:
        oid = answer.get("objectIdFieldName")
        for feature in answer.get("features") or []:
            if not isinstance(feature, dict):
                continue
            key = _key_of(feature, catalog, oid)
            if key in seen:
                continue
            seen.add(key)
            keys.append(key)
            if watch.last_checked is None:
                continue
            a = feature.get("attributes") or {}
            f = catalog.fields
            image_id = a.get(f["id"]) if f.get("id") else None
            sensor = a.get(f["sensor"]) if f.get("sensor") else None
            notices.append(WatchNotice(
                id=f"{watch.id}|{key}", watch_id=watch.id, watch_name=watch.name, catalog_label=catalog.label, found=now,
                read=False, image_id=key if image_id is None else js_string(image_id),
                acquired=a.get(f["acquired"]) if f.get("acquired") else None,
                sensor="" if sensor is None else js_string(sensor), feature=feature,
            ))

    # Newest first, as the catalog lists them.
    def taken(n: WatchNotice) -> float:
        t = time_of(n.acquired)
        return 0 if math.isnan(t) else t

    notices.sort(key=taken, reverse=True)
    return CheckResult(replace(watch, last_checked=now, last_tried=now, last_error=None, seen=(watch.seen + keys)[-MAX_SEEN:]), notices)


def join_areas(areas: Sequence[dict], more: Sequence[dict]) -> list[dict[str, Any]]:
    """`areas` and `more` as one list: points joined into one set of points, rings into one polygon."""
    both = [*areas, *more]
    points = [p for a in both if a["kind"] == "points" for p in a["points"]]
    rings = [r for a in both if a["kind"] == "polygon" for r in a["rings"]]
    return ([{"kind": "points", "points": points}] if points else []) + ([{"kind": "polygon", "rings": rings}] if rings else [])


def add_areas(watch: CatalogWatch, more: Sequence[dict], ask: EsriAsk, now: Optional[float] = None) -> CatalogWatch:
    """`watch` with `more` areas: the images already over them are noted first, so only images new to them are notified."""
    if not watch.areas:
        return watch
    first = check_watch(replace(watch, areas=list(more), last_checked=None,
                                created=watch.last_checked if watch.last_checked is not None else watch.created), ask, now)
    if first.watch.last_error:
        raise RuntimeError(first.watch.last_error)
    return replace(watch, areas=join_areas(watch.areas, more), seen=list(dict.fromkeys(watch.seen + first.watch.seen))[-MAX_SEEN:])


def _signed_area(ring: Sequence[Sequence[float]]) -> float:
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) / 2


def _oriented(ring: Sequence[Sequence[float]], clockwise: bool) -> list[list[float]]:
    out = [[p[0], p[1]] for p in ring]
    if out and out[0] != out[-1]:
        out.append(list(out[0]))
    return out[::-1] if (_signed_area(out) < 0) != clockwise else out


def areas_of(points: Sequence[Sequence[float]], polygons: Sequence[Sequence[Sequence[Sequence[float]]]]) -> list[dict[str, Any]]:
    """
    Points and polygons (each its rings, outer first; longitude / latitude) as
    watch areas: the points as one set, every ring in one Esri polygon (outer
    rings clockwise, holes anticlockwise, as Esri polygons have them).
    """
    rings = [_oriented(ring, i == 0) for polygon in polygons for i, ring in enumerate(polygon) if len(ring) >= 3]
    pts = [[p[0], p[1]] for p in points]
    return ([{"kind": "points", "points": pts}] if pts else []) + ([{"kind": "polygon", "rings": rings}] if rings else [])


def conditions_text(catalog: CatalogConfig, conditions: CatalogSearch, areas: Sequence[dict], with_areas: bool = True) -> str:
    """A short description of a watch's conditions."""
    parts = [catalog.label]
    if with_areas:
        for area in areas:
            if area["kind"] == "polygon":
                parts.append("範囲")
            elif len(area["points"]) == 1:
                lon, lat = area["points"][0][:2]
                parts.append(f"地点 {lat:.4f}, {lon:.4f}")
            else:
                parts.append(f"地点 {len(area['points'])} か所")
        if not areas:
            parts.append("範囲なし")
    c = conditions
    if c.sensor:
        parts.append(f"{catalog.label_of('sensor')} {c.sensor}")
    if c.max_angle is not None:
        parts.append(f"{catalog.label_of('angle')} {js_string(c.max_angle)}° 以下")
    if c.azimuth_from is not None or c.azimuth_to is not None:
        a = c.azimuth_from if c.azimuth_from is not None else 0
        b = c.azimuth_to if c.azimuth_to is not None else 360
        parts.append(f"{catalog.label_of('azimuth')} {js_string(a)}〜{js_string(b)}°")
    if c.where.strip():
        parts.append(c.where.strip())
    return " ・ ".join(parts)


def new_watch(name: str, catalog: CatalogConfig, types: FieldTypes, conditions: CatalogSearch, areas: list[dict],
              now: Optional[float] = None) -> CatalogWatch:
    return CatalogWatch(id=new_id(), name=name, catalog=catalog, types=dict(types), conditions=conditions_of(conditions),
                        areas=areas, enabled=True, created=time.time() * 1000 if now is None else now)
