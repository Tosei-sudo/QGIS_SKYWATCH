"""
Watches on image catalogs (the viewer's catalog-watch.ts and
watch-areas.ts): conditions saved in advance (points or areas, the sensor,
an angle limit, a SQL condition, a free word) that the catalog is asked again and again,
each image that newly matches becoming a notice in 「通知」.

Every image matching a watch is listed, those already there when it was set
too (tsudo, 2026-11-18: 「条件に合うやつはすべからく過去も未来もカードテーブルに」):
the watch's first check reads them all (`past`), as notices already read
(`earlier`, not announced as new), dated by when they were registered so that
they sit in their place in time; later checks add the new ones. A watch kept
before this had its past read on its next check; an area added or changed has
the images already over it read the same way.

QGIS checks while it is open, as it starts (for what came while it was
closed) and every `watchInterval` minutes. Watches and notices are kept in
the local GeoPackage (watch_store.py). A watch keeps its catalog without the
token: the settings file's catalog of the same URL (and its token) is used
to ask.

A watch keeps its areas as copies (WGS 84), also those taken from a layer's
features: it goes on being checked with the project closed or the layer
gone, and a layer edited later changes nothing until the area is taken
again (`replace_areas`, 「範囲の変更…」).
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Sequence

from .. import sql
from ..util import js_string, new_id, time_of, utc_text
from .config import CatalogConfig
from .query import EMPTY_SEARCH, CatalogSearch, FieldTypes, where_of

#: How many image keys a watch remembers.
MAX_SEEN = 5000
#: The most images one check asks for.
MAX_FOUND = 1000
#: The most notices kept; the oldest go first. Notices are listed from light columns (the image's feature read
#: only for the cards made), so this many keep the lists quick; it bounds a watch with no area over a big catalog.
MAX_NOTICES = 50000
#: The most object ids asked for in one request of the past (and never more than the layer's maxRecordCount).
PAST_CHUNK = 1000
DAY = 24 * 60 * 60 * 1000

#: Asks an Esri layer (`url`, form params) for JSON with the catalog's sign-in; raises with the reason when it cannot.
EsriAsk = Callable[[str, dict], dict]


def conditions_of(search: CatalogSearch) -> CatalogSearch:
    """The conditions of a watch: the search's sensor, angle, azimuth, SQL and free word, without its dates (what is new matters)."""
    return replace(EMPTY_SEARCH, sensor=search.sensor, max_angle=search.max_angle, azimuth_from=search.azimuth_from,
                   azimuth_to=search.azimuth_to, where=search.where, keyword=search.keyword)


def _number(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _conditions_from(d: Any) -> CatalogSearch:
    d = d if isinstance(d, dict) else {}
    return replace(EMPTY_SEARCH, sensor=str(d.get("sensor") or ""), max_angle=_number(d.get("maxAngle")),
                   azimuth_from=_number(d.get("azimuthFrom")), azimuth_to=_number(d.get("azimuthTo")), where=str(d.get("where") or ""),
                   keyword=str(d.get("keyword") or ""))


@dataclass
class CatalogWatch:
    id: str
    name: str
    #: The catalog as the settings file had it when the watch was made (without its token).
    catalog: CatalogConfig
    #: The catalog layer's field types, for the date condition.
    types: FieldTypes
    conditions: CatalogSearch
    #: `{"kind": "points", "points": [[lon, lat], …]}`, `{"kind": "polygon", "rings": […]}` or `{"kind": "lines", "paths": […]}`,
    #: asked one after another; empty: anywhere.
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
    #: The layer the areas were taken from (a copy of its features' shapes when taken), see `layer_origin`; None otherwise.
    area_layer: Optional[dict[str, Any]] = None
    #: The images already there still to be read (by its next check): `{"areas": [...] or None (all), "before": ms or None}`
    #: (`before`: only those registered before it, for a watch kept before the past was read); None once read.
    past: Optional[dict[str, Any]] = field(default_factory=lambda: {"areas": None, "before": None})
    #: How many images already there were left out when the past was read (more than MAX_NOTICES); 0 normally.
    past_left: int = 0

    def to_json(self) -> dict[str, Any]:
        c = self.conditions
        return {
            "id": self.id, "name": self.name, "catalog": self.catalog.to_json(), "types": self.types,
            "conditions": {"sensor": c.sensor, "maxAngle": c.max_angle, "azimuthFrom": c.azimuth_from, "azimuthTo": c.azimuth_to, "where": c.where,
                           **({"keyword": c.keyword} if c.keyword else {})},
            "areas": self.areas, "enabled": self.enabled, "created": self.created, "lastChecked": self.last_checked,
            "lastTried": self.last_tried, "lastError": self.last_error, "seen": self.seen,
            **({"areaLayer": self.area_layer} if self.area_layer else {}),
            # Always written: a watch without it was kept before the past was read (see from_json).
            "past": self.past, **({"pastLeft": self.past_left} if self.past_left else {}),
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "CatalogWatch":
        return CatalogWatch(
            id=str(d["id"]), name=str(d.get("name") or ""), catalog=CatalogConfig.from_json(d.get("catalog") or {}),
            types=dict(d.get("types") or {}), conditions=_conditions_from(d.get("conditions")), areas=list(d.get("areas") or []),
            enabled=d.get("enabled") is not False, created=float(d.get("created") or 0), last_checked=d.get("lastChecked"),
            last_tried=d.get("lastTried"), last_error=d.get("lastError"), seen=[str(s) for s in d.get("seen") or []],
            area_layer=d.get("areaLayer") if isinstance(d.get("areaLayer"), dict) else None,
            past=_past_from(d), past_left=int(d.get("pastLeft") or 0),
        )


def _past_from(d: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    The past still to be read: a watch kept before it was read (no `past`)
    has the images registered before it was set read (those after came as notices).
    """
    if "past" not in d:
        created = float(d.get("created") or 0)
        return {"areas": None, "before": created or None}
    past = d.get("past")
    if not isinstance(past, dict):
        return None
    areas = past.get("areas")
    before = past.get("before")
    return {"areas": list(areas) if isinstance(areas, list) else None,
            "before": float(before) if isinstance(before, (int, float)) and not isinstance(before, bool) else None}


def more_past(past: Optional[dict[str, Any]], areas: Optional[Sequence[dict]]) -> dict[str, Any]:
    """The past still to be read with `areas` more (None: all the watch's areas)."""
    if areas is None or (past is not None and past.get("areas") is None):
        return {"areas": None, "before": None}
    have = list(past["areas"]) if past else []
    return {"areas": join_areas(have, areas), "before": None}


#: The watch area kinds by the key of their coordinates, with the Esri geometry type of more than one point.
_KINDS = (("points", "points", "esriGeometryMultipoint"), ("polygon", "rings", "esriGeometryPolygon"), ("lines", "paths", "esriGeometryPolyline"))


def areas_of_shape(shape: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """An Esri shape (`{"rings": …}`, `{"paths": …}` or `{"points": …}`, WGS 84; a layer's geometry) as watch areas."""
    if not isinstance(shape, dict):
        return []
    return [{"kind": kind, key: shape[key]} for kind, key, _ in _KINDS if shape.get(key)]


def layer_origin(name: str, layer_id: str, kind: str, features: Optional[int], selected_only: bool) -> dict[str, Any]:
    """Where areas were taken from: the layer's name and id (not its source, which can hold a password), its shapes' kind and how many features."""
    return {"name": name, "id": layer_id, "kind": kind, "features": features, "selectedOnly": selected_only}


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
    #: What was found out about the image later and kept with it: `contents`, the features of the catalog's
    #: `footprintLayers` over it by layer key (「写っている地物」 of the 新着画像 cards).
    extra: dict[str, Any] = field(default_factory=dict)
    #: Already there when the watch (or its area) was set: read from the start, `found` when it was registered.
    earlier: bool = False
    #: False for a notice listed without its feature and extra (`WatchStore.notices`; `WatchStore.complete` reads them).
    complete: bool = True

    def head(self) -> tuple:
        """What the lists show of it (all but the feature and extra): whether a card must be shown again."""
        return (self.id, self.watch_id, self.watch_name, self.catalog_label, self.found, self.read, self.image_id,
                js_string(self.acquired) if self.acquired is not None else None, self.sensor, self.kind, self.earlier)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "watchId": self.watch_id, "watchName": self.watch_name, "catalogLabel": self.catalog_label,
                "found": self.found, "read": self.read, "imageId": self.image_id, "acquired": self.acquired,
                "sensor": self.sensor, "feature": self.feature, "kind": self.kind, **({"extra": self.extra} if self.extra else {}),
                **({"earlier": True} if self.earlier else {})}

    @staticmethod
    def from_json(d: dict[str, Any]) -> "WatchNotice":
        return WatchNotice(
            id=str(d["id"]), watch_id=str(d.get("watchId") or ""), watch_name=str(d.get("watchName") or ""),
            catalog_label=str(d.get("catalogLabel") or ""), found=float(d.get("found") or 0), read=bool(d.get("read")),
            image_id=str(d.get("imageId") or ""), acquired=d.get("acquired"), sensor=str(d.get("sensor") or ""),
            feature=d.get("feature") or {}, kind=d.get("kind"), extra=d.get("extra") if isinstance(d.get("extra"), dict) else {},
            earlier=d.get("earlier") is True,
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
        return f"{name} >= {sql.date_of(t)}"
    if field_type and re.search(r"Integer|Double|Single", field_type):
        return f"{name} >= {math.floor(t)}"
    if field_type == "esriFieldTypeString":
        return f"{name} >= '{utc_text(t)[:10]}'"
    return f"{name} >= {sql.timestamp(t)}"


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
        if area["kind"] == "points" and len(area["points"]) == 1:
            geometry = {"x": area["points"][0][0], "y": area["points"][0][1], "spatialReference": {"wkid": 4326}}
            kind = "esriGeometryPoint"
        else:
            key, kind = next((k, t) for name, k, t in _KINDS if name == area["kind"])
            geometry = {key: area[key], "spatialReference": {"wkid": 4326}}
        params.update(geometry=json.dumps(geometry), geometryType=kind, inSR="4326", spatialRel="esriSpatialRelIntersects")
    return params


def _key_of(feature: dict[str, Any], catalog: CatalogConfig, object_id_field: Optional[str]) -> str:
    """The key that tells one image from another: its id, else its object id."""
    a = feature.get("attributes") or {}
    value = a.get(catalog.fields["id"]) if catalog.fields.get("id") else None
    for k in (object_id_field, "OBJECTID", "FID", "objectid"):
        if value in (None, "") and k:
            value = a.get(k)
    return json.dumps(a, ensure_ascii=False, sort_keys=True) if value in (None, "") else js_string(value)


class Cancelled(Exception):
    """The check was stopped (QGIS closing) while the past was read: read again next time."""


@dataclass
class CheckResult:
    """What one check found: the watch as it is after it, its new notices and those of the images already there (`earlier`)."""
    watch: CatalogWatch
    notices: list[WatchNotice]
    earlier: list[WatchNotice] = field(default_factory=list)


def _notice_of(watch: CatalogWatch, key: str, feature: dict[str, Any], found: float, earlier: bool = False) -> WatchNotice:
    a = feature.get("attributes") or {}
    f = watch.catalog.fields
    image_id = a.get(f["id"]) if f.get("id") else None
    sensor = a.get(f["sensor"]) if f.get("sensor") else None
    return WatchNotice(
        id=f"{watch.id}|{key}", watch_id=watch.id, watch_name=watch.name, catalog_label=watch.catalog.label, found=found,
        read=earlier, image_id=key if image_id is None else js_string(image_id),
        acquired=a.get(f["acquired"]) if f.get("acquired") else None,
        sensor="" if sensor is None else js_string(sensor), feature=feature, earlier=earlier,
    )


def _taken(n: WatchNotice) -> float:
    t = time_of(n.acquired)
    return 0 if math.isnan(t) else t


def check_watch(watch: CatalogWatch, ask: EsriAsk, now: Optional[float] = None,
                cancelled: Optional[Callable[[], bool]] = None) -> CheckResult:
    """
    Asks the catalog once for `watch`. The first check only notes the images
    registered lately (so that later ones tell what is new); later ones turn
    each image not seen before into a notice. While the watch has its past
    to read, every image matching it is read too (`read_past`).
    A failure is kept in `last_error` (and `last_checked` stays as it was, so
    nothing is missed once it works again; the past is read again).
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
            notices.append(_notice_of(watch, key, feature, now))

    # Newest first, as the catalog lists them.
    notices.sort(key=_taken, reverse=True)
    after = replace(watch, last_checked=now, last_tried=now, last_error=None, seen=(watch.seen + keys)[-MAX_SEEN:])
    if watch.past is None:
        return CheckResult(after, notices)
    try:
        earlier, past_keys, left = read_past(after, ask, cancelled)
    except Cancelled:
        return CheckResult(after, notices)  # read next time
    except Exception as error:  # noqa: BLE001 (read again next time)
        return CheckResult(replace(after, last_error=f"前からある画像を読めず: {str(error) or type(error).__name__}"), notices)
    new = {n.id for n in notices}
    earlier = [n for n in earlier if n.id not in new]
    # The keys of the images already there before those noted lately (kept the longest, see check_watch).
    have = set(after.seen)
    seen_now = ([k for k in past_keys if k not in have] + after.seen)[-MAX_SEEN:]
    return CheckResult(replace(after, seen=seen_now, past=None, past_left=left), notices, earlier)


def _registered(feature: dict[str, Any], catalog: CatalogConfig) -> float:
    """When an image was registered (else taken: the field `newness_of` uses), ms; NaN when not told."""
    newness = newness_of(catalog)
    if not newness:
        return math.nan
    return time_of((feature.get("attributes") or {}).get(newness[0]))


def read_past(watch: CatalogWatch, ask: EsriAsk, cancelled: Optional[Callable[[], bool]] = None) -> tuple[list[WatchNotice], list[str], int]:
    """
    Every image matching `watch` over the areas of its `past` (all its areas
    when None), whenever registered, as notices already read (`earlier`),
    found when they were registered (so that they sit in their place in time;
    when the watch was set without that time): the notices, the images' keys
    (oldest first) and how many were left out (more than MAX_NOTICES: the
    newest kept, the object ids being given in the order rows were added).

    Asked by object ids (returnIdsOnly, then the features `maxRecordCount` at
    a time): every service answers that, and nothing depends on ordering or
    paging. One that refuses ids is asked plainly (what one answer holds).
    """
    past = watch.past or {"areas": None, "before": None}
    areas = past.get("areas")
    before = past.get("before")
    catalog = watch.catalog
    url = f"{catalog.url}/query"
    size = PAST_CHUNK
    try:
        info = ask(catalog.url, {})
        limit = info.get("maxRecordCount") if isinstance(info, dict) else None
        if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
            size = max(1, min(size, int(limit)))
    except Exception:  # noqa: BLE001 (the layer's information not given: the usual size)
        pass
    features: list[dict[str, Any]] = []
    oid: Optional[str] = None
    ids: dict[int, None] = {}
    plain: list[dict[str, Any]] = []
    for area in (areas if areas is not None else watch.areas) or [None]:
        params = watch_query(watch, None, area)
        try:
            answer = ask(url, {**{k: v for k, v in params.items() if k not in ("outFields", "returnGeometry", "outSR")}, "returnIdsOnly": "true"})
        except Exception:  # noqa: BLE001 (ids refused: what one answer holds)
            answer = None
        if not isinstance(answer, dict) or not isinstance(answer.get("objectIds"), list):
            plain.append(params)
            continue
        oid = answer.get("objectIdFieldName") or oid
        for i in answer["objectIds"]:
            if isinstance(i, (int, float)) and not isinstance(i, bool):
                ids[int(i)] = None
    wanted = sorted(ids)
    left = max(0, len(wanted) - MAX_NOTICES)
    wanted = wanted[left:]
    queue = [wanted[i:i + size] for i in range(0, len(wanted), size)]
    while queue:
        if cancelled and cancelled():
            raise Cancelled()
        chunk = queue.pop(0)
        answer = ask(url, {"objectIds": ",".join(str(i) for i in chunk), "outFields": "*", "returnGeometry": "true", "outSR": "4326"})
        oid = answer.get("objectIdFieldName") or oid
        page = [f for f in answer.get("features") or [] if isinstance(f, dict)]
        features.extend(page)
        if answer.get("exceededTransferLimit") and page and len(page) < len(chunk):
            # The service's own limit lower than it says: the rest asked again, as many at a time as it gave.
            got = {(f.get("attributes") or {}).get(oid or "OBJECTID") for f in page}
            rest = [i for i in chunk if i not in got]
            queue[:0] = [rest[i:i + len(page)] for i in range(0, len(rest), len(page))]
    for params in plain:
        answer = ask(url, params)
        oid = answer.get("objectIdFieldName") or oid
        features.extend(f for f in answer.get("features") or [] if isinstance(f, dict))
    out: dict[str, WatchNotice] = {}
    for feature in features:
        key = _key_of(feature, catalog, oid)
        if key in out:
            continue
        t = _registered(feature, catalog)
        if before is not None and not math.isnan(t) and t >= before:
            continue  # came after the watch was set: a notice then (or deleted since)
        found = watch.created if math.isnan(t) else min(t, watch.last_checked or watch.created)
        out[key] = _notice_of(watch, key, feature, found, earlier=True)
    notices = sorted(out.values(), key=lambda n: (n.found, _taken(n)), reverse=True)
    return notices, [n.id.split("|", 1)[1] for n in reversed(notices)], left


def join_areas(areas: Sequence[dict], more: Sequence[dict]) -> list[dict[str, Any]]:
    """`areas` and `more` as one list: points joined into one set of points, rings into one polygon, paths into one polyline."""
    both = [*areas, *more]
    out = []
    for kind, key, _ in _KINDS:
        joined = [c for a in both if a["kind"] == kind for c in a[key]]
        if joined:
            out.append({"kind": kind, key: joined})
    return out


def _noted_over(watch: CatalogWatch, areas: Sequence[dict], ask: EsriAsk, now: Optional[float]) -> list[str]:
    """The keys of the images already over `areas` (since the watch's last check, as its first check would note them); raises when the catalog cannot be asked."""
    first = check_watch(replace(watch, areas=list(areas), last_checked=None, past=None,
                                created=watch.last_checked if watch.last_checked is not None else watch.created), ask, now)
    if first.watch.last_error:
        raise RuntimeError(first.watch.last_error)
    return first.watch.seen


def add_areas(watch: CatalogWatch, more: Sequence[dict], ask: EsriAsk, now: Optional[float] = None) -> CatalogWatch:
    """
    `watch` with `more` areas: the images registered lately over them are
    noted first, so that only images new to them are notified, and those
    already there are read by its next check (`past`, listed as already read).
    """
    if not watch.areas:
        return watch
    noted = _noted_over(watch, more, ask, now)
    return replace(watch, areas=join_areas(watch.areas, more), seen=list(dict.fromkeys(watch.seen + noted))[-MAX_SEEN:],
                   past=more_past(watch.past, more))


def replace_areas(watch: CatalogWatch, areas: Sequence[dict], ask: EsriAsk, now: Optional[float] = None,
                  area_layer: Optional[dict[str, Any]] = None) -> CatalogWatch:
    """
    `watch` over `areas` instead of its own (empty: anywhere; `area_layer`:
    the layer they were taken from). The images registered lately over them
    are noted first, so that only images new to them are notified, and those
    already there are read by its next check (`past`); a watch not yet
    checked just takes them (its first check reads what is there).
    """
    areas = [dict(a) for a in areas]
    if watch.last_checked is None:
        return replace(watch, areas=areas, area_layer=area_layer)
    noted = _noted_over(watch, areas, ask, now)
    return replace(watch, areas=areas, area_layer=area_layer, seen=list(dict.fromkeys(watch.seen + noted))[-MAX_SEEN:],
                   past=more_past(None, None))


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


def layer_text(origin: dict[str, Any]) -> str:
    """Where a watch's areas were taken from, in words: 「レイヤー「港」（12 地物）」, 「レイヤー「港」の選択地物（3 件）」."""
    name, n = origin.get("name") or "", origin.get("features")
    count = f"{n} 件" if origin.get("selectedOnly") else f"{n} 地物"
    return f"レイヤー「{name}」" + (("の選択地物" if origin.get("selectedOnly") else "") + (f"（{count}）" if isinstance(n, int) else ""))


def conditions_text(catalog: CatalogConfig, conditions: CatalogSearch, areas: Sequence[dict], with_areas: bool = True,
                    area_layer: Optional[dict[str, Any]] = None) -> str:
    """A short description of a watch's conditions (`area_layer`: the layer its areas were taken from)."""
    parts = [catalog.label]
    if with_areas:
        from_layer = area_layer.get("kind") if area_layer and areas else None
        if from_layer:
            parts.append(layer_text(area_layer))
        for area in areas:
            if area["kind"] == from_layer:
                continue
            if area["kind"] == "polygon":
                parts.append("範囲")
            elif area["kind"] == "lines":
                parts.append(f"線 {len(area['paths'])} 本")
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
    if c.keyword.strip() and catalog.keyword_fields:
        parts.append(f"フリーワード「{' '.join(c.keyword.split())}」")
    return " ・ ".join(parts)


def new_watch(name: str, catalog: CatalogConfig, types: FieldTypes, conditions: CatalogSearch, areas: list[dict],
              now: Optional[float] = None, area_layer: Optional[dict[str, Any]] = None) -> CatalogWatch:
    return CatalogWatch(id=new_id(), name=name, catalog=catalog, types=dict(types), conditions=conditions_of(conditions),
                        areas=areas, enabled=True, created=time.time() * 1000 if now is None else now,
                        area_layer=area_layer if areas else None)
