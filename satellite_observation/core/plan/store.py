"""
Imaging plans kept in an Esri feature service (`planStores`), as the viewer
(imaging-plan-store.ts): one polygon feature per planned shot.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..catalog import CatalogConfig
from ..esri import EsriClient, EsriError, quote
from ..util import is_number, is_record, layer_url, time_of, utc_text
from .access import Opportunity, PlanTarget
from .remind import shots_of
from .satellites import SatelliteSpec

#: The attribute names used when the settings do not map a role.
DEFAULT_PLAN_FIELDS: dict[str, str] = {
    "planId": "PLAN_ID",
    "planName": "PLAN_NAME",
    "shotTime": "SHOT_TIME",
    "satellite": "SATELLITE",
    "satelliteId": "SATELLITE_ID",
    "mode": "MODE",
    "target": "TARGET",
    "offNadir": "OFF_NADIR",
    "incidence": "INCIDENCE",
    "pass": "PASS_DIR",
    "side": "LOOK_SIDE",
    "status": "STATUS",
    "imageId": "IMAGE_ID",
    "created": "CREATED_AT",
}
#: The statuses of a shot.
STATUSES = ("予定", "撮像済み", "未撮像", "取消")
#: Their colours on the map and in the table.
STATUS_COLORS = {"予定": "#2563eb", "撮像済み": "#16a34a", "未撮像": "#dc2626", "取消": "#6b7280"}
_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)
_NAME = re.compile(r"^[\w.]+$", re.ASCII)


@dataclass
class PlanStoreConfig:
    """One entry of `planStores`."""
    label: str
    url: str
    fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PLAN_FIELDS))
    token: Optional[str] = None
    #: The image catalog (its label) asked whether shots were taken; the first one when absent.
    catalog: Optional[str] = None
    match_minutes: float = 30
    miss_after_days: float = 3
    authcfg: Optional[str] = None


def plan_store_of(value: Any, problems: list[str], index: int) -> Optional[PlanStoreConfig]:
    """The store of one `planStores` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"planStores[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if not isinstance(url, str) or not _FEATURE_LAYER.search(url.split("?", 1)[0]):
        problems.append(f"{at} の url は編集できる …/FeatureServer/0 のようなレイヤーの URL で指定")
        return None
    mapped = dict(DEFAULT_PLAN_FIELDS)
    fields = value.get("fields")
    if is_record(fields):
        for key in DEFAULT_PLAN_FIELDS:
            v = fields.get(key)
            if isinstance(v, str) and _NAME.match(v):
                mapped[key] = v
            elif key in fields:
                problems.append(f"{at} の fields.{key} は属性名で指定")

    def number(v: Any, fallback: float) -> float:
        return v if is_number(v) and v > 0 else fallback

    label, token, catalog = value.get("label"), value.get("token"), value.get("catalog")
    return PlanStoreConfig(
        label=label if isinstance(label, str) and label else f"計画の保存先 {index + 1}",
        url=layer_url(url),
        token=token if isinstance(token, str) and token else None,
        fields=mapped,
        catalog=catalog if isinstance(catalog, str) and catalog else None,
        match_minutes=number(value.get("matchMinutes"), 30),
        miss_after_days=number(value.get("missAfterDays"), 3),
    )


@dataclass
class SavedShot:
    """A shot as the layer holds it."""
    objectId: int
    planId: str
    planName: str
    time: float
    satellite: str
    satelliteId: str
    target: str
    offNadir: Optional[float]
    incidence: Optional[float]
    pass_: str
    side: str
    status: str
    imageId: str
    created: Optional[float]
    #: The scenes, longitude / latitude rings.
    rings: list
    mode: str = ""


@dataclass
class SavedPlan:
    """A plan in the layer: its shots counted."""
    id: str
    name: str
    created: Optional[float]
    shots: int = 0
    first: float = math.inf
    last: float = -math.inf
    #: Shots by status.
    statuses: dict = field(default_factory=dict)


@dataclass
class ShotCheck:
    """What a follow-up check found for one shot."""
    objectId: int
    status: str
    imageId: Optional[str] = None


def layer_of(client: EsriClient, store: PlanStoreConfig, token: Optional[str]) -> tuple[set, dict, str]:
    """The layer's field names, their types and its object id field; raises when the plan id or time is missing."""
    info = client.layer(store.url, token, refresh=True)
    types = info.field_types
    names = set(types)
    for role in ("planId", "shotTime"):
        if store.fields[role] not in names:
            raise EsriError(f"{store.label} に属性 {store.fields[role]} なし")
    return names, types, info.object_id_field


def new_plan_id() -> str:
    """A new id for a plan."""
    return str(uuid.uuid4()).upper()


def _signed_area(ring: Sequence[Sequence[float]]) -> float:
    """Twice the signed area of a ring (positive when anticlockwise)."""
    a = 0.0
    j = len(ring) - 1
    for i in range(len(ring)):
        a += (ring[j][0] - ring[i][0]) * (ring[j][1] + ring[i][1])
        j = i
    return a


def _round2(v: float) -> float:
    return math.floor(v * 100 + 0.5) / 100


def shot_features(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget],
                  rings_of: Callable[[Opportunity], list], plan_id: str, plan_name: str, fields: dict[str, str],
                  now: float) -> list[dict[str, Any]]:
    """
    The features of a plan's shots (one per shot; a scene shared by several
    targets once, naming them all): attributes by role, and the scenes as an
    Esri polygon in WGS 84 with outer rings clockwise.
    """
    out = []
    for shot in shots_of(ops, sats, targets):
        op, sat = shot.op, shot.sat
        rings = [[list(p) for p in ring] for ring in rings_of(op)]
        out.append({
            "attributes": {
                fields["planId"]: plan_id,
                fields["planName"]: plan_name,
                fields["shotTime"]: op.time,
                fields["satellite"]: sat.name,
                fields["satelliteId"]: sat.id,
                fields["mode"]: sat.mode,
                fields["target"]: "、".join(shot.targets)[:250],
                fields["offNadir"]: _round2(op.offNadir),
                fields["incidence"]: _round2(op.incidence),
                fields["pass"]: "北行" if op.ascending else "南行",
                fields["side"]: "右" if op.side == "right" else "左",
                fields["status"]: "予定",
                fields["imageId"]: None,
                fields["created"]: now,
            },
            "geometry": {"rings": [list(reversed(r)) if _signed_area(r) > 0 else r for r in rings], "spatialReference": {"wkid": 4326}},
        })
    return out


def _only(attributes: dict[str, Any], names: set) -> dict[str, Any]:
    """The attributes the layer has."""
    return {k: v for k, v in attributes.items() if k in names}


def add_shots(client: EsriClient, store: PlanStoreConfig, token: Optional[str], features: Sequence[dict[str, Any]],
              cancelled: Optional[Callable[[], bool]] = None) -> tuple[int, list[str]]:
    """Adds features to the layer (200 at a time); how many were saved, and why others were not."""
    names, _, _ = layer_of(client, store, token)
    saved, failed = 0, []
    for i in range(0, len(features), 200):
        if cancelled and cancelled():
            break
        batch = [{**f, "attributes": _only(f["attributes"], names)} for f in features[i:i + 200]]
        result = client.apply_edits(store.url, adds=batch, token=token)
        for r in result.add_results:
            if r.get("success"):
                saved += 1
            else:
                failed.append((r.get("error") or {}).get("description") or "保存失敗")
    return saved, failed


def _number_or(v: Any) -> Optional[float]:
    if is_number(v):
        return float(v)
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _text(v: Any) -> str:
    return "" if v is None else str(v)


def _status_of(v: Any) -> str:
    return _text(v) if _text(v) in STATUSES else "予定"


def list_plans(client: EsriClient, store: PlanStoreConfig, token: Optional[str],
               cancelled: Optional[Callable[[], bool]] = None) -> list[SavedPlan]:
    """The plans in the layer, newest first."""
    f = store.fields
    names, _, _ = layer_of(client, store, token)
    wanted = [n for n in (f["planId"], f["planName"], f["shotTime"], f["status"], f["created"]) if n in names]
    rows = client.query_all(store.url, "1=1", token, out_fields=",".join(wanted), geometry=False, limit=20000, cancelled=cancelled)
    plans: dict[str, SavedPlan] = {}
    for row in rows:
        a = row.get("attributes") or {}
        pid = _text(a.get(f["planId"]))
        if not pid:
            continue
        plan = plans.get(pid)
        if plan is None:
            plan = plans[pid] = SavedPlan(pid, _text(a.get(f["planName"])) or pid, _number_or(a.get(f["created"])))
        plan.shots += 1
        t = time_of(a.get(f["shotTime"]))
        if math.isfinite(t):
            plan.first = min(plan.first, t)
            plan.last = max(plan.last, t)
        status = _status_of(a.get(f["status"]))
        plan.statuses[status] = plan.statuses.get(status, 0) + 1
    return sorted(plans.values(), key=lambda p: -(p.created if p.created is not None else p.first))


def load_shots(client: EsriClient, store: PlanStoreConfig, token: Optional[str], plan_id: str,
               cancelled: Optional[Callable[[], bool]] = None) -> list[SavedShot]:
    """The shots of a plan, in time order."""
    f = store.fields
    _, _, oid = layer_of(client, store, token)
    rows = client.query_all(store.url, f"{f['planId']} = {quote(plan_id)}", token, limit=20000, cancelled=cancelled)
    shots = []
    for row in rows:
        a = row.get("attributes") or {}
        geometry = row.get("geometry") or {}
        shots.append(SavedShot(
            objectId=int(a.get(oid)), planId=_text(a.get(f["planId"])), planName=_text(a.get(f["planName"])),
            time=time_of(a.get(f["shotTime"])), satellite=_text(a.get(f["satellite"])), satelliteId=_text(a.get(f["satelliteId"])),
            target=_text(a.get(f["target"])), offNadir=_number_or(a.get(f["offNadir"])), incidence=_number_or(a.get(f["incidence"])),
            pass_=_text(a.get(f["pass"])), side=_text(a.get(f["side"])), status=_status_of(a.get(f["status"])),
            imageId=_text(a.get(f["imageId"])), created=_number_or(a.get(f["created"])),
            rings=geometry.get("rings") or [] if isinstance(geometry, dict) else [], mode=_text(a.get(f["mode"]))))
    return sorted(shots, key=lambda s: s.time)


def update_shots(client: EsriClient, store: PlanStoreConfig, token: Optional[str], changes: Sequence[ShotCheck]) -> int:
    """Changes shots' status (and image id) in the layer; how many were changed."""
    if not changes:
        return 0
    names, _, oid = layer_of(client, store, token)
    f = store.fields
    done = 0
    for i in range(0, len(changes), 200):
        updates = []
        for c in changes[i:i + 200]:
            attributes = {oid: c.objectId, f["status"]: c.status}
            if c.imageId is not None:
                attributes[f["imageId"]] = c.imageId
            updates.append({"attributes": _only(attributes, names | {oid})})
        result = client.apply_edits(store.url, updates=updates, token=token)
        done += sum(1 for r in result.update_results if r.get("success"))
    return done


def delete_plan(client: EsriClient, store: PlanStoreConfig, token: Optional[str], plan_id: str) -> None:
    """Deletes a plan's shots from the layer."""
    client.ask(f"{store.url}/deleteFeatures", {"where": f"{store.fields['planId']} = {quote(plan_id)}"}, token)


def between_condition(name: str, field_type: Optional[str], start: float, end: float) -> str:
    """`name` between two moments, written for its type."""
    if field_type and re.search(r"Integer|Double|Single", field_type):
        return f"{name} >= {math.floor(start)} AND {name} <= {math.ceil(end)}"
    if field_type == "esriFieldTypeString":
        return f"{name} >= '{utc_text(start)}' AND {name} <= '{utc_text(end)}'"
    return f"{name} >= TIMESTAMP '{utc_text(start)}' AND {name} <= TIMESTAMP '{utc_text(end)}'"


def _plain(s: str) -> str:
    """Letters and digits only, lower case: `WorldView-3` → `worldview3`."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def track_shots(client: EsriClient, store: PlanStoreConfig, catalog: CatalogConfig, catalog_token: Optional[str],
                shots: Sequence[SavedShot], now: float, cancelled: Optional[Callable[[], bool]] = None) -> list[ShotCheck]:
    """
    Follows up the shots still 「予定」 whose moment has passed: an image of
    `catalog` taken within `match_minutes` of the shot over its scenes makes
    it 「撮像済み」 (an image whose sensor names the satellite preferred);
    none after `miss_after_days` makes it 「未撮像」; otherwise it waits.
    Returns the changes only.
    """
    acquired = catalog.fields.get("acquired")
    if not acquired:
        raise EsriError(f"{catalog.label} に撮像日時の属性（fields.acquired）なし")
    types = client.layer(catalog.url, catalog_token).field_types
    window = store.match_minutes * 60000
    changes: list[ShotCheck] = []
    for shot in shots:
        if cancelled and cancelled():
            break
        if shot.status != "予定" or not math.isfinite(shot.time) or shot.time > now:
            continue
        where = " AND ".join([between_condition(acquired, types.get(acquired), shot.time - window, shot.time + window),
                              *([f"({catalog.where})"] if catalog.where else [])])
        params = {"where": where, "outFields": "*", "returnGeometry": "false"}
        if shot.rings:
            params.update(geometry=json.dumps({"rings": shot.rings, "spatialReference": {"wkid": 4326}}),
                          geometryType="esriGeometryPolygon", inSR="4326", spatialRel="esriSpatialRelIntersects")
        found = client.ask(f"{catalog.url}/query", params, catalog_token).get("features") or []
        if found:
            names = [n for n in (_plain(shot.satellite), _plain(shot.satelliteId)) if n]
            sensor = catalog.fields.get("sensor")

            def named(feature: dict) -> bool:
                s = _plain(_text((feature.get("attributes") or {}).get(sensor))) if sensor else ""
                return s != "" and any(s in n or n in s for n in names)

            best = next((x for x in found if named(x)), found[0])
            a = best.get("attributes") or {}
            image_id = catalog.fields.get("id")
            value = a.get(image_id) if image_id else None
            if value is None:
                value = next((a[k] for k in ("OBJECTID", "objectid", "FID") if a.get(k) is not None), None)
            changes.append(ShotCheck(shot.objectId, "撮像済み", _text(value)))
        elif now - shot.time > store.miss_after_days * 86400000:
            changes.append(ShotCheck(shot.objectId, "未撮像"))
    return changes


def progress_text(statuses: dict) -> str:
    """How a plan is going: shots taken of those whose outcome is known, and those still to come."""
    taken = statuses.get("撮像済み", 0)
    missed = statuses.get("未撮像", 0)
    known = taken + missed
    parts = [f"撮像済み {taken} / {known}（{math.floor(taken / known * 100 + 0.5)}%）" if known else "",
             f"未撮像 {missed}" if missed else "",
             f"予定 {statuses['予定']}" if statuses.get("予定") else "",
             f"取消 {statuses['取消']}" if statuses.get("取消") else ""]
    return "・".join(p for p in parts if p) or "—"
