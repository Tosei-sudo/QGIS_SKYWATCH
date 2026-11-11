"""
The request ledger (撮像依頼台帳) and its history (出力履歴) in Esri feature
services (`requestStores`): one polygon feature per request, one table row
per request handed out (or held back) on a day.
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional, Sequence

from ..esri import ConflictError, EsriClient, EsriError, UnreachableError, deleted_count, quote
from ..offline import OfflineWork, Pending, UploadReport, offline_of, tried_of
from ..util import is_number, is_record, layer_url, time_of, use_legacy_names
from .groups import DEFAULT_GROUP_FIELDS
from .model import (
    ACHIEVES, CATEGORIES, KINDS, LOOK_SIDES, ORBIT_DIRS, SHAPE_KINDS, STATUSES, TIMES_OF_DAY, ImagingRequest, clamp_priority,
    day_bounds,
)

#: The attribute names used when the settings do not map a role.
DEFAULT_REQUEST_FIELDS: dict[str, str] = {
    "id": "REQ_ID", "name": "REQ_NAME", "managementNo": "MANAGEMENT_NO", "identificationNo": "IDENTIFICATION_NO",
    "group": "REQUEST_GROUP", "category": "CATEGORY", "achieve": "ACHIEVE",
    "start": "START_AT", "end": "END_AT", "repeat": "REPEAT", "status": "STATUS",
    "priority": "PRIORITY", "boost": "BOOST", "kind": "KIND", "series": "SERIES", "satellites": "SATELLITES",
    "modes": "SENSOR_MODES", "offNadirMin": "OFF_NADIR_MIN", "offNadirMax": "OFF_NADIR_MAX",
    "incidenceMin": "INCIDENCE_MIN", "incidenceMax": "INCIDENCE_MAX", "lookSide": "LOOK_SIDE", "orbitDir": "ORBIT_DIR",
    "resolution": "RESOLUTION_MAX", "timeOfDay": "TIME_OF_DAY", "cloudNoGo": "CLOUD_NO_GO",
    "timesMin": "TIMES_MIN", "timesMax": "TIMES_MAX", "timesGap": "TIMES_GAP", "systems": "SYSTEMS",
    "shapeKind": "SHAPE_KIND", "requester": "REQUESTER", "note": "NOTE", "created": "CREATED_AT", "updated": "UPDATED_AT",
}
DEFAULT_HISTORY_FIELDS: dict[str, str] = {
    "runId": "RUN_ID", "runDate": "RUN_DATE", "day": "TARGET_DAY", "targetDate": "TARGET_DATE", "requestId": "REQ_ID",
    "name": "REQ_NAME", "managementNo": "MANAGEMENT_NO", "requester": "REQUESTER", "group": "REQUEST_GROUP", "system": "SYSTEM",
    "satellite": "SATELLITE", "priority": "PRIORITY_OUT", "cloud": "CLOUD", "scenes": "SCENES", "result": "RESULT",
    "reason": "REASON", "file": "OUTPUT_FILE", "captured": "CAPTURED", "imageId": "IMAGE_ID", "boostDone": "BOOST_DONE",
    "adoption": "ADOPTION", "adoptionReason": "ADOPTION_REASON", "plannedAt": "PLANNED_AT", "externalId": "EXTERNAL_ID",
    "adoptionSource": "ADOPTION_SOURCE", "mergedInto": "MERGED_INTO", "shotId": "SHOT_ID", "occurrence": "OCCURRENCE",
}
#: The shot layer's names (TBL_REQUEST_SHOT: one row a row of a file, its polygon the outline of the scenes taken).
DEFAULT_SHOT_FIELDS: dict[str, str] = {
    "shotId": "SHOT_ID", "runId": "RUN_ID", "runDate": "RUN_DATE", "day": "TARGET_DAY", "targetDate": "TARGET_DATE",
    "system": "SYSTEM", "satellite": "SATELLITE", "passTime": "PASS_TIME", "offNadir": "OFF_NADIR", "incidence": "INCIDENCE",
    "orbitDir": "ORBIT_DIR", "lookSide": "LOOK_SIDE", "scenes": "SCENES", "shots": "SHOT_COUNT", "coverage": "COVERAGE",
    "requestId": "REQ_ID", "requestIds": "REQ_IDS", "names": "REQ_NAMES", "count": "REQ_COUNT", "file": "OUTPUT_FILE",
}
#: The history's 採否 (the system's own answer to what was handed out; core/request/adoption.py).
ADOPTION_ROLES = ("adoption", "adoptionReason", "plannedAt", "externalId", "adoptionSource")
#: The RESULT of a row the system planned without being asked (要求外, e.g. its routine tasks; no REQ_ID).
UNREQUESTED = "要求外"
#: ADOPTION_SOURCE of an answer given by hand.
MANUAL = "手入力"
#: ADOPTION_SOURCE of a row found taken (an image of it came in): it was in the system's plan after all
#: (tsudo, 2026-11-09: 「採否がわからないやつもある。入ってきたら、採用されてたんだなとなる」).
IMAGE_ARRIVED = "画像の入着"
#: Default names before 2026-11-08 (FILE is an Oracle / SQL Server reserved word), still read from a table that has them.
LEGACY_HISTORY_FIELDS: dict[str, str] = {"file": "FILE"}
#: What a history row says about whether the request was taken.
CAPTURED = ("未判定", "撮像済み", "未撮像")
_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)
#: A table only read (the groups): a feature service's or a map service's.
_READ_TABLE = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)
_NAME = re.compile(r"^[\w.]+$", re.ASCII)


@dataclass
class RequestStoreConfig:
    """One entry of `requestStores`."""
    label: str
    url: str
    #: The history table (None: nothing is kept, and nothing is followed up or raised).
    history: Optional[str] = None
    fields: dict = field(default_factory=lambda: dict(DEFAULT_REQUEST_FIELDS))
    history_fields: dict = field(default_factory=lambda: dict(DEFAULT_HISTORY_FIELDS))
    token: Optional[str] = None
    #: The image catalog (its label) asked whether requests were taken; the first one when absent.
    catalog: Optional[str] = None
    #: Days after its day a request handed out and not found in the catalog is 「未撮像」.
    miss_after_days: float = 3
    authcfg: Optional[str] = None
    #: The 依頼グループ table (core/request/groups.py; None: groups are typed freely).
    groups: Optional[str] = None
    group_fields: dict = field(default_factory=lambda: dict(DEFAULT_GROUP_FIELDS))
    #: The shot layer (撮像の範囲; None: the shots are not kept, the history alone tells what was put together).
    shots: Optional[str] = None
    shot_fields: dict = field(default_factory=lambda: dict(DEFAULT_SHOT_FIELDS))


def _fields(value: Any, defaults: dict, at: str, problems: list[str]) -> dict:
    mapped = dict(defaults)
    if is_record(value):
        for key in defaults:
            v = value.get(key)
            if isinstance(v, str) and _NAME.match(v):
                mapped[key] = v
            elif key in value:
                problems.append(f"{at}.{key} は属性名で指定")
    return mapped


def request_store_of(value: Any, problems: list[str], index: int) -> Optional[RequestStoreConfig]:
    """The ledger of one `requestStores` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"requestStores[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if not isinstance(url, str) or not _FEATURE_LAYER.search(url.split("?", 1)[0]):
        problems.append(f"{at} の url は編集できる …/FeatureServer/0 のようなレイヤーの URL で指定")
        return None
    history = value.get("history")
    if history is not None and (not isinstance(history, str) or not _FEATURE_LAYER.search(history.split("?", 1)[0])):
        problems.append(f"{at} の history は …/FeatureServer/1 のようなテーブルの URL で指定")
        history = None
    groups = value.get("groups")
    if groups is not None and (not isinstance(groups, str) or not _READ_TABLE.search(groups.split("?", 1)[0])):
        problems.append(f"{at} の groups は依頼グループのテーブルの URL（…/FeatureServer/2 など）で指定")
        groups = None
    shots = value.get("shots")
    if shots is not None and (not isinstance(shots, str) or not _FEATURE_LAYER.search(shots.split("?", 1)[0])):
        problems.append(f"{at} の shots は撮像の範囲のポリゴンレイヤーの URL（…/FeatureServer/22 など）で指定")
        shots = None
    label, token, catalog = value.get("label"), value.get("token"), value.get("catalog")
    miss = value.get("missAfterDays")
    return RequestStoreConfig(
        label=label if isinstance(label, str) and label else f"撮像依頼 {index + 1}",
        url=layer_url(url),
        history=layer_url(history) if history else None,
        fields=_fields(value.get("fields"), DEFAULT_REQUEST_FIELDS, f"{at}.fields", problems),
        history_fields=_fields(value.get("historyFields"), DEFAULT_HISTORY_FIELDS, f"{at}.historyFields", problems),
        token=token if isinstance(token, str) and token else None,
        catalog=catalog if isinstance(catalog, str) and catalog else None,
        miss_after_days=float(miss) if is_number(miss) and miss > 0 else 3,
        groups=layer_url(groups) if groups else None,
        group_fields=_fields(value.get("groupFields"), DEFAULT_GROUP_FIELDS, f"{at}.groupFields", problems),
        shots=layer_url(shots) if shots else None,
        shot_fields=_fields(value.get("shotFields"), DEFAULT_SHOT_FIELDS, f"{at}.shotFields", problems),
    )


def new_request_id() -> str:
    return str(uuid.uuid4()).upper()


# --- Attributes ------------------------------------------------------------------------------

def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _code(v: Any) -> str:
    """A number kept as text (管理番号・識別番号): a whole number from a numeric field without its 「.0」."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return _text(v)


def _number(v: Any) -> Optional[float]:
    if is_number(v):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            n = float(v)
        except ValueError:
            return None
        return n if math.isfinite(n) else None
    return None


def day_start_ms(day: str) -> Optional[float]:
    """The first moment (ms) of a JST day `2026-11-10`; None when not a day."""
    try:
        return day_bounds(date.fromisoformat(day[:10]))[0]
    except (TypeError, ValueError):
        return None


def _choice(v: Any, choices: Sequence[str], fallback: str) -> str:
    t = _text(v)
    return t if t in choices else fallback


def _list(v: Any) -> list:
    return [s.strip() for s in re.split(r"[,、]", _text(v)) if s.strip()]


def _signed_area(ring: Sequence[Sequence[float]]) -> float:
    a = 0.0
    j = len(ring) - 1
    for i in range(len(ring)):
        a += (ring[j][0] - ring[i][0]) * (ring[j][1] + ring[i][1])
        j = i
    return a


def esri_rings(rings: Sequence) -> list:
    """Rings with outer rings clockwise, as Esri wants them."""
    return [list(reversed(r)) if _signed_area(r) > 0 else [list(p) for p in r] for r in rings]


def attributes_of(req: ImagingRequest, f: dict) -> dict[str, Any]:
    """A request's attributes by the layer's names."""
    def join(items):
        return ",".join(items) or None

    return {
        f["id"]: req.id, f["name"]: req.name[:250], f["managementNo"]: req.management_no[:250] or None,
        f["identificationNo"]: req.identification_no[:250] or None, f["group"]: req.group[:250] or None,
        f["category"]: req.category, f["achieve"]: req.achieve,
        f["start"]: req.start if math.isfinite(req.start) else None, f["end"]: req.end if math.isfinite(req.end) else None,
        f["repeat"]: req.repeat, f["status"]: req.status, f["priority"]: clamp_priority(req.priority), f["boost"]: int(req.boost),
        f["kind"]: req.kind, f["series"]: join(req.series), f["satellites"]: join(req.satellites), f["modes"]: join(req.modes),
        f["offNadirMin"]: req.off_nadir_min, f["offNadirMax"]: req.off_nadir_max,
        f["incidenceMin"]: req.incidence_min, f["incidenceMax"]: req.incidence_max,
        f["lookSide"]: req.look_side, f["orbitDir"]: req.orbit_dir, f["resolution"]: req.resolution,
        f["timeOfDay"]: req.time_of_day, f["cloudNoGo"]: req.cloud_no_go,
        f["timesMin"]: req.times_min if req.times_min and req.times_min > 1 else None, f["timesMax"]: req.times_max,
        f["timesGap"]: req.times_gap, f["systems"]: join(req.systems),
        f["shapeKind"]: req.shape_kind, f["requester"]: req.requester[:250], f["note"]: req.note[:1000],
        f["created"]: req.created, f["updated"]: req.updated,
    }


def _times(v: Any) -> Optional[int]:
    """A count of shots a day (1 or more); None when not given or not a count."""
    n = _number(v)
    return int(n) if n is not None and n >= 1 else None


def _hours(v: Any) -> Optional[float]:
    n = _number(v)
    return float(n) if n is not None and n >= 0 else None


def request_of(a: dict, geometry: Any, f: dict, oid: str) -> Optional[ImagingRequest]:
    """The request of a feature; None without an id."""
    rid = _text(a.get(f["id"]))
    if not rid:
        return None
    rings = geometry.get("rings") if isinstance(geometry, dict) else None
    p = _number(a.get(f["priority"]))
    boost = _number(a.get(f["boost"]))
    return ImagingRequest(
        id=rid, name=_text(a.get(f["name"])) or rid,
        management_no=_code(a.get(f["managementNo"])), identification_no=_code(a.get(f["identificationNo"])),
        group=_text(a.get(f["group"])),
        category=_choice(a.get(f["category"]), CATEGORIES, "臨時"),
        achieve=_choice(a.get(f["achieve"]), ACHIEVES, "期間内1回"),
        start=time_of(a.get(f["start"])), end=time_of(a.get(f["end"])),
        repeat=_text(a.get(f["repeat"])) or "毎日",
        status=_choice(a.get(f["status"]), STATUSES, "有効"),
        priority=clamp_priority(p if p is not None else 500), boost=max(0, int(boost or 0)),
        kind=_choice(a.get(f["kind"]), KINDS, "任意"),
        series=_list(a.get(f["series"])), satellites=_list(a.get(f["satellites"])), modes=_list(a.get(f["modes"])),
        off_nadir_min=_number(a.get(f["offNadirMin"])), off_nadir_max=_number(a.get(f["offNadirMax"])),
        incidence_min=_number(a.get(f["incidenceMin"])), incidence_max=_number(a.get(f["incidenceMax"])),
        look_side=_choice(a.get(f["lookSide"]), LOOK_SIDES, "任意"),
        orbit_dir=_choice(a.get(f["orbitDir"]), ORBIT_DIRS, "任意"),
        resolution=_number(a.get(f["resolution"])),
        time_of_day=_choice(a.get(f["timeOfDay"]), TIMES_OF_DAY, "任意"),
        cloud_no_go=_number(a.get(f["cloudNoGo"])), systems=_list(a.get(f["systems"])),
        times_min=_times(a.get(f["timesMin"])) or 1, times_max=_times(a.get(f["timesMax"])),
        times_gap=_hours(a.get(f["timesGap"])),
        shape_kind=_choice(a.get(f["shapeKind"]), SHAPE_KINDS, "範囲"),
        requester=_text(a.get(f["requester"])), note=_text(a.get(f["note"])),
        created=_number(a.get(f["created"])), updated=_number(a.get(f["updated"])),
        rings=[[list(p[:2]) for p in r] for r in rings] if isinstance(rings, list) else [],
        object_id=int(a[oid]) if is_number(a.get(oid)) else None,
    )


# --- The ledger ------------------------------------------------------------------------------

def _layer(client: EsriClient, url: str, token: Optional[str], label: str, needed: Sequence[str],
           history: Optional[dict] = None) -> tuple[set, str]:
    info = client.layer(url, token, refresh=True)
    names = set(info.field_types)
    if history is not None:
        use_legacy_names(history, names, DEFAULT_HISTORY_FIELDS, LEGACY_HISTORY_FIELDS)
    missing = [n for n in needed if n not in names]
    if missing:
        raise EsriError(f"{label} に属性 {'・'.join(missing)} なし")
    return names, info.object_id_field


def _only(attributes: dict, names: set) -> dict:
    return {k: v for k, v in attributes.items() if k in names}


def load_requests(client: EsriClient, store: RequestStoreConfig, token: Optional[str],
                  cancelled: Optional[Callable[[], bool]] = None) -> list[ImagingRequest]:
    """Every request of the ledger, by name."""
    f = store.fields
    _, oid = _layer(client, store.url, token, store.label, (f["id"],))
    rows = client.query_all(store.url, "1=1", token, limit=50000, cancelled=cancelled)
    out = [r for r in (request_of(row.get("attributes") or {}, row.get("geometry"), f, oid) for row in rows) if r]
    work = offline_of(client)
    if work is not None:
        out = _with_waiting(work, store, out)
    return sorted(out, key=lambda r: (r.name, r.id))


# --- While the ledger cannot be reached (core/offline.py) --------------------------------------

KIND = "request"
_REQUEST_FIELDS = set(ImagingRequest.__dataclass_fields__)


def waiting_request(p: Pending) -> Optional[ImagingRequest]:
    value = p.data.get("request") or {}
    try:
        req = ImagingRequest(**{k: v for k, v in value.items() if k in _REQUEST_FIELDS and k != "pending_update"})
    except TypeError:
        return None
    req.start = math.nan if req.start is None else req.start
    req.end = math.nan if req.end is None else req.end
    return req


def waiting_ids(client: EsriClient, store: RequestStoreConfig) -> set[str]:
    """The requests of this ledger changed while it could not be reached, waiting to be sent."""
    work = offline_of(client)
    return work.ids(KIND, store.label) if work is not None else set()


def _with_waiting(work: OfflineWork, store: RequestStoreConfig, requests: list[ImagingRequest]) -> list[ImagingRequest]:
    out = {r.id: r for r in requests}
    for p in work.pending(KIND, store.label):
        if p.op == "remove":
            out.pop(p.item_id, None)
            continue
        req = waiting_request(p)
        if req is not None:
            out[req.id] = req
    return list(out.values())


def _keep_request(work: OfflineWork, store: RequestStoreConfig, req: ImagingRequest, now: float) -> ImagingRequest:
    """Keeps a request saved while the ledger cannot be reached; it is sent when it answers again."""
    if req.created is None:
        req.created = now
    # What it was on the server before (its 更新日時): the first change made offline keeps it (OfflineWork.keep).
    base = req.updated if req.object_id is not None else None
    req.updated = now
    data = {k: getattr(req, k) for k in _REQUEST_FIELDS if k != "pending_update"}
    kept = {"request": data}
    if req.pending_update is not None:
        # The save that found the ledger down timed out: it may be there (not someone else's change when sent).
        kept["tried"] = [req.pending_update]
    work.keep(KIND, store.label, req.id, "save", kept, base, req.object_id is not None, req.name)
    req.pending_update = None
    return req


def upload_requests(client: EsriClient, store: RequestStoreConfig, token: Optional[str], report: UploadReport,
                    now: float, only: Optional[set] = None) -> None:
    """
    Sends the requests of this ledger kept while it could not be reached (all,
    or those in `only`: (kind, store, item id)). One someone else changed (or
    deleted) meanwhile is not saved over: it stays waiting, with why (to be
    thrown away, or changed again after a read).
    """
    work = offline_of(client)
    if work is None:
        return
    for p in work.pending(KIND, store.label):
        if only is not None and (p.kind, p.store, p.item_id) not in only:
            continue
        req = waiting_request(p)
        if req is None:
            work.drop(KIND, store.label, [p.item_id])
            continue
        try:
            if p.op == "remove":
                _send_removal(client, store, token, req, p)
            else:
                # Checked against what it was before the change made offline (#74).
                req.updated = p.base if p.known else None
                req.pending_update = None
                _save_online(client, store, token, req, now, tried_of(p))
        except UnreachableError as error:
            work.add_tried(KIND, store.label, p.item_id, [req.pending_update])
            report.kept.append(f"撮像依頼（{store.label}）: {error}")
            return
        except EsriError as error:
            work.add_tried(KIND, store.label, p.item_id, [req.pending_update])
            work.note(p, str(error))
            report.kept.append(f"撮像依頼「{req.name}」: {error}")
            continue
        work.drop(KIND, store.label, [p.item_id])
        report.add(KIND)


def _send_removal(client: EsriClient, store: RequestStoreConfig, token: Optional[str], req: ImagingRequest, p: Pending) -> None:
    f = store.fields
    names, oid = _layer(client, store.url, token, store.label, (f["id"],))
    found = client.find(store.url, f["id"], [req.id], token).get(req.id)
    if found is None:
        return  # already gone
    stamp = _number(found.get(f["updated"])) if f["updated"] in names else None
    if p.base is not None and stamp is not None and not _same_stamp(stamp, p.base):
        raise ConflictError(f"「{req.name}」はオフライン中にほかの人が更新したため削除せず")
    _online_delete(client, store, token, req)


#: How far apart two 更新日時 may be and still be the same save (services that keep whole seconds).
STAMP_SLACK_MS = 1000


def _same_stamp(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(a - b) <= STAMP_SLACK_MS


def save_request(client: EsriClient, store: RequestStoreConfig, token: Optional[str], req: ImagingRequest, now: float) -> ImagingRequest:
    """
    Saves a request (`_save_online`); while the ledger cannot be reached it is
    kept to be sent once it answers (core/offline.py), and returned as saved.
    """
    work = offline_of(client)
    if work is not None and work.down(store.url):
        return _keep_request(work, store, req, now)
    waiting = work.item(KIND, store.label, req.id) if work is not None else None
    tried: list[float] = []
    if waiting is not None:
        # Changed offline and not sent yet (the ledger answers again): checked against the ledger as it was
        # before that change (#74), as sending it would be; what waited is then superseded by this save.
        req.updated = waiting.base if waiting.known else None
        req.pending_update = None
        tried = tried_of(waiting)
    try:
        saved = _save_online(client, store, token, req, now, tried)
    except EsriError:
        # Cut off on the way (its reads may then have come from the cache): kept to send later.
        if work is None or not work.down(store.url):
            raise
        return _keep_request(work, store, req, now)
    if waiting is not None:
        work.drop(KIND, store.label, [req.id])
    return saved


def _save_online(client: EsriClient, store: RequestStoreConfig, token: Optional[str], req: ImagingRequest, now: float,
                 tried: Sequence[float] = ()) -> ImagingRequest:
    """
    Adds a new request (no object id) or changes one; the request as saved
    (its object id set). The ledger is asked first: a request whose id is
    already there (an earlier save whose answer was lost, #73) is changed,
    not added again; one that someone else saved (or deleted) since it was
    read is not overwritten (ConflictError, #74). `tried`: the 更新日時 of
    earlier saves of it whose answer was lost while offline (not a conflict).
    """
    f = store.fields
    names, oid = _layer(client, store.url, token, store.label, (f["id"],))
    found = client.find(store.url, f["id"], [req.id], token).get(req.id)
    tracked = f["updated"] in names
    if found is not None:
        server_oid = int(found[oid]) if is_number(found.get(oid)) else None
        server_stamp = _number(found.get(f["updated"])) if tracked else None
        if req.object_id is None:
            # Already there: an earlier save went through (or the same id came in again from a layer).
            req.object_id = server_oid
            if req.pending_update is None and req.updated is None:
                req.updated = server_stamp
        elif server_oid is not None and server_oid != req.object_id:
            req.object_id = server_oid
        if (tracked and req.updated is not None and server_stamp is not None and not _same_stamp(server_stamp, req.updated)
                and not any(_same_stamp(server_stamp, s) for s in (req.pending_update, *tried))):
            raise ConflictError(f"「{req.name}」はほかの人が読み込み後に更新したため保存を中止（読み込み直してから変更）")
    elif req.object_id is not None:
        raise ConflictError(f"「{req.name}」はほかの人が台帳から削除済み（読み込み直して確認）")
    baseline = req.updated
    if req.created is None:
        req.created = now
    req.updated = now
    feature = {"attributes": _only(attributes_of(req, f), names),
               "geometry": {"rings": esri_rings(req.rings), "spatialReference": {"wkid": 4326}}}
    try:
        if req.object_id is None:
            result = client.apply_edits(store.url, adds=[feature], token=token)
            r = (result.add_results or [{}])[0]
            if not r.get("success"):
                raise EsriError((r.get("error") or {}).get("description") or "保存失敗")
            req.object_id = int(r.get("objectId"))
        else:
            feature["attributes"][oid] = req.object_id
            result = client.apply_edits(store.url, updates=[feature], token=token)
            r = (result.update_results or [{}])[0]
            if not r.get("success"):
                raise EsriError((r.get("error") or {}).get("description") or "保存失敗")
    except EsriError:
        # Kept as read: saving again checks the ledger, which may or may not hold this attempt.
        req.updated, req.pending_update = baseline, now
        raise
    req.pending_update = None
    return req


class Saved(int):
    """
    How many edits were saved (an int, as before), with why the others were
    not (`problems`) and the object ids saved (`ok`, for updates).
    """
    problems: list
    ok: set

    def __new__(cls, done: int = 0, problems: Optional[list] = None, ok: Optional[set] = None):
        value = super().__new__(cls, done)
        value.problems = list(problems or [])
        value.ok = set(ok or ())
        return value

    @property
    def failed(self) -> bool:
        return bool(self.problems)


def _batches(send: Callable[[list], Any], items: Sequence, results: Callable[[Any], list], label: str) -> Saved:
    """
    Sends `items` 200 at a time; a batch that cannot be sent stops the rest
    (the rows not sent are told in `problems`, not lost silently).
    """
    done, problems, ok = 0, [], set()
    for i in range(0, len(items), 200):
        batch = list(items[i:i + 200])
        try:
            answer = send(batch)
        except EsriError as error:
            problems.append(f"{label} {len(items) - i} 件を保存できず（{error}）")
            break
        got = results(answer)
        for r in got:
            if r.get("success"):
                done += 1
                if is_number(r.get("objectId")):
                    ok.add(int(r["objectId"]))
            else:
                problems.append(f"{label}: {(r.get('error') or {}).get('description') or '保存失敗'}")
        if len(got) < len(batch):
            problems.append(f"{label} {len(batch) - len(got)} 件の結果なし")
    return Saved(done, problems, ok)


def update_requests(client: EsriClient, store: RequestStoreConfig, token: Optional[str], changes: Sequence[tuple[int, dict]],
                    now: Optional[float] = None) -> Saved:
    """Changes some attributes (by role) of requests (object id, {role: value}); how many were changed (and why not)."""
    if not changes:
        return Saved()
    names, oid = _layer(client, store.url, token, store.label, (store.fields["id"],))
    f = store.fields
    updates = []
    for object_id, values in changes:
        attributes = {oid: object_id, **{f[k]: v for k, v in values.items()}}
        if now is not None:
            attributes[f["updated"]] = now
        updates.append({"attributes": _only(attributes, names | {oid})})
    return _batches(lambda batch: client.apply_edits(store.url, updates=batch, token=token), updates,
                    lambda r: r.update_results, "台帳の更新")


def delete_request(client: EsriClient, store: RequestStoreConfig, token: Optional[str], req: ImagingRequest) -> int:
    """
    Deletes a request from the ledger; how many features went (EsriError when
    the server kept any, #81). While the ledger cannot be reached the deletion
    waits to be sent (a request made offline is just forgotten).
    """
    work = offline_of(client)

    def keep() -> int:
        before = work.item(KIND, store.label, req.id)
        known = before.known if before is not None else req.object_id is not None
        if not known:
            work.drop(KIND, store.label, [req.id])
            return 1
        data = {k: getattr(req, k) for k in _REQUEST_FIELDS if k != "pending_update"}
        work.keep(KIND, store.label, req.id, "remove", {"request": data}, req.updated, True, req.name)
        return 1

    if work is not None:
        before = work.item(KIND, store.label, req.id)
        if before is not None and not before.known:
            work.drop(KIND, store.label, [req.id])  # made offline, never sent
            return 1
        if work.down(store.url):
            return keep()
        if before is not None:
            work.drop(KIND, store.label, [req.id])
    try:
        return _online_delete(client, store, token, req)
    except EsriError:
        if work is None or not work.down(store.url):
            raise
        return keep()


def _online_delete(client: EsriClient, store: RequestStoreConfig, token: Optional[str], req: ImagingRequest) -> int:
    value = client.ask(f"{store.url}/deleteFeatures", {"where": f"{store.fields['id']} = {quote(req.id)}"}, token)
    return deleted_count(value, "依頼の削除")


# --- History ---------------------------------------------------------------------------------

@dataclass
class HistoryRow:
    """One request handed out (or held back) on a day."""
    run_id: str
    run_date: float
    #: The JST day, `2026-11-08`.
    day: str
    request_id: str
    system: str = ""
    satellite: str = ""
    priority: str = ""
    cloud: Optional[float] = None
    scenes: Optional[int] = None
    result: str = "採用"
    reason: str = ""
    file: str = ""
    captured: str = "未判定"
    image_id: str = ""
    boost_done: int = 0
    object_id: Optional[int] = None
    #: The request's 名称・管理番号・依頼者・依頼グループ when written (so a dashboard lists and filters them without
    #: joining the ledger).
    name: str = ""
    management_no: str = ""
    requester: str = ""
    group: str = ""
    #: The system's answer (採否): 不明 / 採用 / 不採用 for one handed out ("" for one held back); 採用 for 要求外.
    adoption: str = ""
    adoption_reason: str = ""
    #: When the system plans to take it (ms), as its answer says.
    planned_at: Optional[float] = None
    #: The system's own id of it, and where the answer came from (the result file's name, or 手入力).
    external_id: str = ""
    adoption_source: str = ""
    #: The area (longitude / latitude rings), kept when the table has polygons (the request's, or the file's for 要求外).
    rings: list = field(default_factory=list)
    #: The request whose row in the file carried this one, put into its shot (まとめ); "" for its own row.
    merged_into: str = ""
    #: The shot it is taken in (the shot layer's SHOT_ID; "" when fitted without the orbits).
    shot_id: str = ""
    #: Which of the request's shots of the day (1 …), for a request taken several times a day (1 日の回数).
    occurrence: int = 1

    @property
    def key(self) -> tuple:
        """(request id, occurrence): one row of a day's file."""
        return self.request_id, self.occurrence


def history_attributes(row: HistoryRow, f: dict) -> dict:
    return {
        f["runId"]: row.run_id, f["runDate"]: row.run_date, f["day"]: row.day, f["targetDate"]: day_start_ms(row.day),
        f["requestId"]: row.request_id, f["name"]: row.name[:250] or None, f["managementNo"]: row.management_no[:250] or None,
        f["requester"]: row.requester[:250] or None, f["group"]: row.group[:250] or None, f["system"]: row.system, f["satellite"]: row.satellite,
        f["priority"]: row.priority, f["cloud"]: row.cloud, f["scenes"]: row.scenes, f["result"]: row.result,
        f["reason"]: row.reason[:250], f["file"]: row.file[:250], f["captured"]: row.captured, f["imageId"]: row.image_id,
        f["boostDone"]: row.boost_done, f["adoption"]: row.adoption or None, f["adoptionReason"]: row.adoption_reason[:1000] or None,
        f["plannedAt"]: row.planned_at, f["externalId"]: row.external_id[:250] or None,
        f["adoptionSource"]: row.adoption_source[:1000] or None, f["mergedInto"]: row.merged_into[:250] or None,
        f["shotId"]: row.shot_id[:64] or None, f["occurrence"]: row.occurrence,
    }


def _polygons(client: EsriClient, url: str, token: Optional[str]) -> bool:
    """Whether a history table keeps areas (a polygon layer; a table without shapes does not)."""
    return client.layer(url, token).geometry_type == "esriGeometryPolygon"


def add_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], rows: Sequence[HistoryRow]) -> Saved:
    """Adds history rows; how many were saved, and why the others were not (none when the store keeps no history)."""
    if not store.history or not rows:
        return Saved()
    hf = store.history_fields
    names, _ = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"], hf["day"]), history=hf)
    shapes = _polygons(client, store.history, token)
    adds = []
    for r in rows:
        feature: dict = {"attributes": _only(history_attributes(r, hf), names)}
        if shapes and r.rings:
            feature["geometry"] = {"rings": esri_rings(r.rings), "spatialReference": {"wkid": 4326}}
        adds.append(feature)
    return _batches(lambda batch: client.apply_edits(store.history, adds=batch, token=token), adds,
                    lambda r: r.add_results, "出力履歴の追加")


def history_where(store: RequestStoreConfig, *, open_rows: bool = False, first: Optional[str] = None,
                  last: Optional[str] = None, request_ids: Sequence[str] = ()) -> str:
    """
    A condition on the history table (its names) so that only the rows a
    use needs are read, not the whole table: `open_rows` those still to be
    followed up (採用・未判定) or raised for (未撮像, not yet offered); `first`
    / `last` the target days (`2026-11-08`); `request_ids` some requests.
    The parts given are joined by AND.
    """
    hf = store.history_fields
    parts = []
    if open_rows:
        captured, result, done = hf["captured"], hf["result"], hf["boostDone"]
        parts.append(f"(({result} = '採用' AND ({captured} = '未判定' OR {captured} IS NULL)) OR "
                     f"({captured} = '未撮像' AND ({done} = 0 OR {done} IS NULL)))")
    if first:
        parts.append(f"{hf['day']} >= {quote(first)}")
    if last:
        parts.append(f"{hf['day']} <= {quote(last)}")
    if request_ids:
        parts.append(f"{hf['requestId']} IN ({','.join(quote(i) for i in dict.fromkeys(request_ids))})")
    return " AND ".join(parts) or "1=1"


def count_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], where: str) -> int:
    """How many history rows match `where` (0 without a history)."""
    if not store.history:
        return 0
    value = client.ask(f"{store.history}/query", {"where": where, "returnCountOnly": "true"}, token)
    n = value.get("count")
    return int(n) if is_number(n) else 0


def replaced_where(store: RequestStoreConfig, day: str, system: Optional[str] = None) -> str:
    """
    The rows of a target day that writing the day again replaces: all but
    those already followed up and the systems' own plans (要求外); only those
    of `system` when one system is written (the others are another person's,
    2026-11-09). The systems' answers among them are carried over or kept
    (workflow.keep_answers).
    """
    hf = store.history_fields
    captured = hf["captured"]
    where = (f"{hf['day']} = {quote(day)} AND ({captured} IS NULL OR {captured} = '' OR {captured} = '未判定')"
             f" AND ({hf['result']} IS NULL OR {hf['result']} <> {quote(UNREQUESTED)})")
    return where + (f" AND {hf['system']} = {quote(system)}" if system else "")


def handed_where(store: RequestStoreConfig, day: str) -> str:
    """The rows of a target day handed out (採用) and not yet followed up: what each system's file holds."""
    hf = store.history_fields
    return f"{replaced_where(store, day)} AND {hf['result']} = '採用'"


def delete_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], where: str) -> Saved:
    """Deletes the history rows matching `where`; how many were deleted (and why not)."""
    if not store.history:
        return Saved()
    value = client.ask(f"{store.history}/deleteFeatures", {"where": where}, token)
    results = value.get("deleteResults")
    if results is None:
        return Saved(1 if value.get("success") else 0, [] if value.get("success", True) else ["出力履歴の削除失敗"])
    ok = [r for r in results if r.get("success")]
    return Saved(len(ok), [f"出力履歴の削除: {(r.get('error') or {}).get('description') or '削除失敗'}" for r in results if not r.get("success")])


def history_row_of(a: dict, geometry: Any, hf: dict, oid: str) -> HistoryRow:
    """A history row of a feature's attributes (by the table's names)."""
    cloud, scenes, done = _number(a.get(hf["cloud"])), _number(a.get(hf["scenes"])), _number(a.get(hf["boostDone"]))
    rings = geometry.get("rings") if isinstance(geometry, dict) else None
    return HistoryRow(
        run_id=_text(a.get(hf["runId"])), run_date=time_of(a.get(hf["runDate"])), day=_text(a.get(hf["day"]))[:10],
        request_id=_text(a.get(hf["requestId"])), name=_text(a.get(hf["name"])), management_no=_code(a.get(hf["managementNo"])),
        requester=_text(a.get(hf["requester"])), group=_text(a.get(hf["group"])), system=_text(a.get(hf["system"])), satellite=_text(a.get(hf["satellite"])),
        priority=_text(a.get(hf["priority"])), cloud=cloud, scenes=int(scenes) if scenes is not None else None,
        result=_text(a.get(hf["result"])) or "採用", reason=_text(a.get(hf["reason"])), file=_text(a.get(hf["file"])),
        captured=_choice(a.get(hf["captured"]), CAPTURED, "未判定"), image_id=_text(a.get(hf["imageId"])),
        boost_done=int(done or 0), object_id=int(a[oid]) if is_number(a.get(oid)) else None,
        adoption=_text(a.get(hf["adoption"])), adoption_reason=_text(a.get(hf["adoptionReason"])),
        planned_at=_number(a.get(hf["plannedAt"])), external_id=_code(a.get(hf["externalId"])),
        adoption_source=_text(a.get(hf["adoptionSource"])), merged_into=_text(a.get(hf["mergedInto"])),
        shot_id=_text(a.get(hf["shotId"])), occurrence=_times(a.get(hf["occurrence"])) or 1,
        rings=[[list(p[:2]) for p in r] for r in rings] if isinstance(rings, list) else [])


def load_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], where: str = "1=1",
                 cancelled: Optional[Callable[[], bool]] = None) -> list[HistoryRow]:
    """History rows of requests (a condition by the table's names; 要求外 rows left out), oldest day first."""
    if not store.history:
        return []
    hf = store.history_fields
    _, oid = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"], hf["day"]), history=hf)
    rows = client.query_all(store.history, where, token, geometry=False, limit=100000, cancelled=cancelled)
    out = []
    for row in rows:
        a = row.get("attributes") or {}
        # 要求外 rows are the systems' own plans, not requests handed out: never followed up or raised.
        if not _text(a.get(hf["requestId"])) or _text(a.get(hf["result"])) == UNREQUESTED:
            continue
        out.append(history_row_of(a, None, hf, oid))
    return sorted(out, key=lambda r: (r.day, r.run_date if math.isfinite(r.run_date) else 0))


def load_day(client: EsriClient, store: RequestStoreConfig, token: Optional[str], day: str,
             cancelled: Optional[Callable[[], bool]] = None) -> list[HistoryRow]:
    """Every row of a target day (`2026-11-10`): requests handed out or held back and 要求外, with their areas."""
    if not store.history:
        return []
    hf = store.history_fields
    _, oid = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"], hf["day"]), history=hf)
    shapes = _polygons(client, store.history, token)
    rows = client.query_all(store.history, f"{hf['day']} = {quote(day)}", token, geometry=shapes, limit=100000, cancelled=cancelled)
    out = [history_row_of(row.get("attributes") or {}, row.get("geometry"), hf, oid) for row in rows]
    return sorted(out, key=lambda r: (r.system, r.planned_at or 0, r.name, r.run_date if math.isfinite(r.run_date) else 0))


def day_counts(client: EsriClient, store: RequestStoreConfig, token: Optional[str], first: str, last: str) -> dict[str, dict]:
    """
    How many rows each target day from `first` to `last` has, by what they
    are (採用 = in the system's plan, 要求外 included; 不採用; 不明;
    見送り): for marking the calendar.
    """
    if not store.history:
        return {}
    hf = store.history_fields
    names, _ = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"], hf["day"]), history=hf)
    fields = ",".join(n for n in (hf["day"], hf["result"], hf["adoption"], hf["captured"]) if n in names)
    rows = client.query_all(store.history, f"{hf['day']} >= {quote(first)} AND {hf['day']} <= {quote(last)}", token,
                            out_fields=fields, geometry=False, limit=200000)
    out: dict[str, dict] = {}
    for row in rows:
        a = row.get("attributes") or {}
        day = _text(a.get(hf["day"]))[:10]
        state = state_of(_text(a.get(hf["result"])) or "採用", _text(a.get(hf["adoption"])), _text(a.get(hf["captured"])))
        tally = out.setdefault(day, {})
        tally[state] = tally.get(state, 0) + 1
    return out


#: What a row is in the day's plan.
PLANNED, DECLINED, AWAITING, HELD_BACK = "採用", "不採用", "不明", "見送り"
STATES = (PLANNED, AWAITING, DECLINED, HELD_BACK, UNREQUESTED)


def state_of(result: str, adoption: str, captured: str = "") -> str:
    """
    A row's place in the day's plan: 要求外 (planned by the system itself),
    or for a request handed out its answer (採用 / 不採用), 不明 while none is
    known (some systems never say), and 採用 once its image came in
    (撮像済み: it was planned after all); or 見送り.
    """
    if result == UNREQUESTED:
        return UNREQUESTED
    if result != "採用":
        return HELD_BACK
    if captured == "撮像済み":
        return PLANNED
    return adoption if adoption in (PLANNED, DECLINED) else AWAITING


def unrequested_where(store: RequestStoreConfig, day: str, system: str, by_hand: bool) -> str:
    """The 要求外 rows of a day and system read from a file (`by_hand`: those added by hand)."""
    hf = store.history_fields
    source = hf["adoptionSource"]
    which = f"{source} = {quote(MANUAL)}" if by_hand else f"({source} IS NULL OR {source} <> {quote(MANUAL)})"
    return (f"{hf['day']} = {quote(day)} AND {hf['system']} = {quote(system)} AND {hf['result']} = {quote(UNREQUESTED)}"
            f" AND {which}")


def delete_history_rows(client: EsriClient, store: RequestStoreConfig, token: Optional[str], object_ids: Sequence[int]) -> Saved:
    """Deletes history rows by object id (要求外 rows taken off the day's plan); how many went (and why not)."""
    if not store.history or not object_ids:
        return Saved()
    results = client.delete_features(store.history, object_ids, token)
    ok = [r for r in results if r.get("success")]
    return Saved(len(ok), [f"出力履歴の削除: {(r.get('error') or {}).get('description') or '削除失敗'}" for r in results if not r.get("success")])


def history_has(client: EsriClient, store: RequestStoreConfig, token: Optional[str], roles: Sequence[str]) -> list[str]:
    """The roles' fields the history table lacks (their names; [] when it has them all, or keeps no history)."""
    if not store.history:
        return []
    names = {n.lower() for n in client.layer(store.history, token, refresh=True).field_types}
    return [store.history_fields[r] for r in roles if store.history_fields[r].lower() not in names]


def update_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], changes: Sequence[tuple[int, dict]]) -> Saved:
    """Changes history rows (object id, {role: value}); how many were changed, which (`ok`), and why the others were not."""
    if not store.history or not changes:
        return Saved()
    hf = store.history_fields
    names, oid = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"],), history=hf)
    updates = [{"attributes": _only({oid: o, **{hf[k]: v for k, v in values.items()}}, names | {oid})} for o, values in changes]
    return _batches(lambda batch: client.apply_edits(store.history, updates=batch, token=token), updates,
                    lambda r: r.update_results, "出力履歴の更新")


# --- Shots (撮像の範囲) ---------------------------------------------------------------------------

@dataclass
class ShotRow:
    """
    One row of a system's file as the satellite takes it (tsudo, 2026-11-11:
    「マージした結果、どの範囲になったかも見たい」): its shots' outline, when,
    how, and the requests in it (the first is the one whose row it is). The
    history keeps each request's own row and area; its SHOT_ID says which
    shot takes it.
    """
    shot_id: str
    run_id: str
    run_date: float
    #: The JST day, `2026-11-08`.
    day: str
    system: str = ""
    satellite: str = ""
    #: The middle of its (first) shot (ms).
    pass_time: Optional[float] = None
    off_nadir: Optional[float] = None
    incidence: Optional[float] = None
    #: 昇交 / 降交, 右 / 左.
    orbit_dir: str = ""
    look_side: str = ""
    scenes: Optional[int] = None
    #: How many shots (passes): more than 1 only for an area too big for one.
    shots: int = 1
    #: The share of the request's area its shots take (an area too big for one shot); None when whole.
    coverage: Optional[float] = None
    request_id: str = ""
    request_ids: list = field(default_factory=list)
    names: list = field(default_factory=list)
    file: str = ""
    #: The outlines (longitude / latitude rings), one a shot.
    rings: list = field(default_factory=list)
    object_id: Optional[int] = None

    @property
    def count(self) -> int:
        return len(self.request_ids) or 1


def shot_attributes(row: ShotRow, f: dict) -> dict:
    return {
        f["shotId"]: row.shot_id, f["runId"]: row.run_id, f["runDate"]: row.run_date, f["day"]: row.day,
        f["targetDate"]: day_start_ms(row.day), f["system"]: row.system[:250] or None, f["satellite"]: row.satellite[:250] or None,
        f["passTime"]: row.pass_time, f["offNadir"]: row.off_nadir, f["incidence"]: row.incidence, f["orbitDir"]: row.orbit_dir or None,
        f["lookSide"]: row.look_side or None, f["scenes"]: row.scenes, f["shots"]: row.shots,
        f["coverage"]: None if row.coverage is None else round(row.coverage * 100, 1), f["requestId"]: row.request_id,
        f["requestIds"]: ",".join(row.request_ids)[:4000] or None, f["names"]: "、".join(row.names)[:4000] or None,
        f["count"]: row.count, f["file"]: row.file[:1000] or None,
    }


def shot_row_of(a: dict, geometry: Any, f: dict, oid: str) -> ShotRow:
    """A shot row of a feature's attributes (by the layer's names)."""
    rings = geometry.get("rings") if isinstance(geometry, dict) else None
    scenes, shots, coverage = _number(a.get(f["scenes"])), _number(a.get(f["shots"])), _number(a.get(f["coverage"]))
    names = _text(a.get(f["names"]))
    return ShotRow(
        shot_id=_text(a.get(f["shotId"])), run_id=_text(a.get(f["runId"])), run_date=time_of(a.get(f["runDate"])),
        day=_text(a.get(f["day"]))[:10], system=_text(a.get(f["system"])), satellite=_text(a.get(f["satellite"])),
        pass_time=time_of(a.get(f["passTime"])) if a.get(f["passTime"]) is not None else None,
        off_nadir=_number(a.get(f["offNadir"])), incidence=_number(a.get(f["incidence"])),
        orbit_dir=_text(a.get(f["orbitDir"])), look_side=_text(a.get(f["lookSide"])),
        scenes=int(scenes) if scenes is not None else None, shots=int(shots) if shots else 1,
        coverage=None if coverage is None else coverage / 100, request_id=_text(a.get(f["requestId"])),
        request_ids=[i.strip() for i in _text(a.get(f["requestIds"])).split(",") if i.strip()],
        names=[n for n in names.split("、") if n] if names else [], file=_text(a.get(f["file"])),
        rings=[[list(p[:2]) for p in r] for r in rings] if isinstance(rings, list) else [],
        object_id=int(a[oid]) if is_number(a.get(oid)) else None)


def _shot_layer(client: EsriClient, store: RequestStoreConfig, token: Optional[str]) -> tuple[set, str]:
    sf = store.shot_fields
    return _layer(client, store.shots, token, f"{store.label} の撮像の範囲", (sf["shotId"], sf["day"]))


def add_shots(client: EsriClient, store: RequestStoreConfig, token: Optional[str], rows: Sequence[ShotRow]) -> Saved:
    """Adds shot rows with their outlines; how many were saved, and why the others were not (none without a shot layer)."""
    if not store.shots or not rows:
        return Saved()
    names, _ = _shot_layer(client, store, token)
    adds = []
    for r in rows:
        feature: dict = {"attributes": _only(shot_attributes(r, store.shot_fields), names)}
        if r.rings:
            feature["geometry"] = {"rings": esri_rings(r.rings), "spatialReference": {"wkid": 4326}}
        adds.append(feature)
    return _batches(lambda batch: client.apply_edits(store.shots, adds=batch, token=token), adds,
                    lambda r: r.add_results, "撮像の範囲の追加")


def load_shots(client: EsriClient, store: RequestStoreConfig, token: Optional[str], day: str,
               cancelled: Optional[Callable[[], bool]] = None) -> list[ShotRow]:
    """The shots of a target day (`2026-11-10`), with their outlines ([] without a shot layer)."""
    if not store.shots:
        return []
    sf = store.shot_fields
    _, oid = _shot_layer(client, store, token)
    rows = client.query_all(store.shots, f"{sf['day']} = {quote(day)}", token, geometry=True, limit=100000, cancelled=cancelled)
    out = [shot_row_of(row.get("attributes") or {}, row.get("geometry"), sf, oid) for row in rows]
    return sorted(out, key=lambda r: (r.system, r.pass_time or 0, r.shot_id))


def shots_replaced_where(store: RequestStoreConfig, day: str, system: Optional[str] = None, keep: Sequence[str] = ()) -> str:
    """
    The shots that writing a day again replaces: the day's (of `system` when
    one system is written), but those that history rows kept as they were
    (answered by the system, workflow.keep_answers) still name.
    """
    sf = store.shot_fields
    where = f"{sf['day']} = {quote(day)}"
    if system:
        where += f" AND {sf['system']} = {quote(system)}"
    kept = [k for k in dict.fromkeys(keep) if k]
    if kept:
        where += f" AND {sf['shotId']} NOT IN ({','.join(quote(k) for k in kept)})"
    return where


def delete_shots(client: EsriClient, store: RequestStoreConfig, token: Optional[str], where: str) -> Saved:
    """Deletes the shot rows matching `where`; how many were deleted (and why not)."""
    if not store.shots:
        return Saved()
    value = client.ask(f"{store.shots}/deleteFeatures", {"where": where}, token)
    results = value.get("deleteResults")
    if results is None:
        return Saved(1 if value.get("success") else 0, [] if value.get("success", True) else ["撮像の範囲の削除失敗"])
    ok = [r for r in results if r.get("success")]
    return Saved(len(ok), [f"撮像の範囲の削除: {(r.get('error') or {}).get('description') or '削除失敗'}"
                           for r in results if not r.get("success")])
