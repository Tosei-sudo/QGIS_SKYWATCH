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
from typing import Any, Callable, Optional, Sequence

from ..esri import ConflictError, EsriClient, EsriError, deleted_count, quote
from ..util import is_number, is_record, layer_url, time_of
from .model import (
    ACHIEVES, CATEGORIES, KINDS, LOOK_SIDES, ORBIT_DIRS, SHAPE_KINDS, STATUSES, TIMES_OF_DAY, ImagingRequest, clamp_priority,
)

#: The attribute names used when the settings do not map a role.
DEFAULT_REQUEST_FIELDS: dict[str, str] = {
    "id": "REQ_ID", "name": "REQ_NAME", "category": "CATEGORY", "achieve": "ACHIEVE",
    "start": "START_AT", "end": "END_AT", "repeat": "REPEAT", "status": "STATUS",
    "priority": "PRIORITY", "boost": "BOOST", "kind": "KIND", "series": "SERIES", "satellites": "SATELLITES",
    "modes": "SENSOR_MODES", "offNadirMin": "OFF_NADIR_MIN", "offNadirMax": "OFF_NADIR_MAX",
    "incidenceMin": "INCIDENCE_MIN", "incidenceMax": "INCIDENCE_MAX", "lookSide": "LOOK_SIDE", "orbitDir": "ORBIT_DIR",
    "resolution": "RESOLUTION_MAX", "timeOfDay": "TIME_OF_DAY", "cloudNoGo": "CLOUD_NO_GO", "systems": "SYSTEMS",
    "shapeKind": "SHAPE_KIND", "requester": "REQUESTER", "note": "NOTE", "created": "CREATED_AT", "updated": "UPDATED_AT",
}
DEFAULT_HISTORY_FIELDS: dict[str, str] = {
    "runId": "RUN_ID", "runDate": "RUN_DATE", "day": "TARGET_DAY", "requestId": "REQ_ID", "system": "SYSTEM",
    "satellite": "SATELLITE", "priority": "PRIORITY_OUT", "cloud": "CLOUD", "scenes": "SCENES", "result": "RESULT",
    "reason": "REASON", "file": "FILE", "captured": "CAPTURED", "imageId": "IMAGE_ID", "boostDone": "BOOST_DONE",
}
#: What a history row says about whether the request was taken.
CAPTURED = ("未判定", "撮像済み", "未撮像")
_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)
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
    )


def new_request_id() -> str:
    return str(uuid.uuid4()).upper()


# --- Attributes ------------------------------------------------------------------------------

def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


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
        f["id"]: req.id, f["name"]: req.name[:250], f["category"]: req.category, f["achieve"]: req.achieve,
        f["start"]: req.start if math.isfinite(req.start) else None, f["end"]: req.end if math.isfinite(req.end) else None,
        f["repeat"]: req.repeat, f["status"]: req.status, f["priority"]: clamp_priority(req.priority), f["boost"]: int(req.boost),
        f["kind"]: req.kind, f["series"]: join(req.series), f["satellites"]: join(req.satellites), f["modes"]: join(req.modes),
        f["offNadirMin"]: req.off_nadir_min, f["offNadirMax"]: req.off_nadir_max,
        f["incidenceMin"]: req.incidence_min, f["incidenceMax"]: req.incidence_max,
        f["lookSide"]: req.look_side, f["orbitDir"]: req.orbit_dir, f["resolution"]: req.resolution,
        f["timeOfDay"]: req.time_of_day, f["cloudNoGo"]: req.cloud_no_go, f["systems"]: join(req.systems),
        f["shapeKind"]: req.shape_kind, f["requester"]: req.requester[:250], f["note"]: req.note[:1000],
        f["created"]: req.created, f["updated"]: req.updated,
    }


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
        shape_kind=_choice(a.get(f["shapeKind"]), SHAPE_KINDS, "範囲"),
        requester=_text(a.get(f["requester"])), note=_text(a.get(f["note"])),
        created=_number(a.get(f["created"])), updated=_number(a.get(f["updated"])),
        rings=[[list(p[:2]) for p in r] for r in rings] if isinstance(rings, list) else [],
        object_id=int(a[oid]) if is_number(a.get(oid)) else None,
    )


# --- The ledger ------------------------------------------------------------------------------

def _layer(client: EsriClient, url: str, token: Optional[str], label: str, needed: Sequence[str]) -> tuple[set, str]:
    info = client.layer(url, token, refresh=True)
    names = set(info.field_types)
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
    return sorted(out, key=lambda r: (r.name, r.id))


#: How far apart two 更新日時 may be and still be the same save (services that keep whole seconds).
STAMP_SLACK_MS = 1000


def _same_stamp(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(a - b) <= STAMP_SLACK_MS


def save_request(client: EsriClient, store: RequestStoreConfig, token: Optional[str], req: ImagingRequest, now: float) -> ImagingRequest:
    """
    Adds a new request (no object id) or changes one; the request as saved
    (its object id set). The ledger is asked first: a request whose id is
    already there (an earlier save whose answer was lost, #73) is changed,
    not added again; one that someone else saved (or deleted) since it was
    read is not overwritten (ConflictError, #74).
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
                and not _same_stamp(server_stamp, req.pending_update)):
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
    """Deletes a request from the ledger; how many features went (EsriError when the server kept any, #81)."""
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


def history_attributes(row: HistoryRow, f: dict) -> dict:
    return {
        f["runId"]: row.run_id, f["runDate"]: row.run_date, f["day"]: row.day, f["requestId"]: row.request_id,
        f["system"]: row.system, f["satellite"]: row.satellite, f["priority"]: row.priority, f["cloud"]: row.cloud,
        f["scenes"]: row.scenes, f["result"]: row.result, f["reason"]: row.reason[:250], f["file"]: row.file[:250],
        f["captured"]: row.captured, f["imageId"]: row.image_id, f["boostDone"]: row.boost_done,
    }


def add_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], rows: Sequence[HistoryRow]) -> Saved:
    """Adds history rows; how many were saved, and why the others were not (none when the store keeps no history)."""
    if not store.history or not rows:
        return Saved()
    hf = store.history_fields
    names, _ = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"], hf["day"]))
    adds = [{"attributes": _only(history_attributes(r, hf), names)} for r in rows]
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


def replaced_where(store: RequestStoreConfig, day: str) -> str:
    """The rows of a target day that writing the day again replaces: all but those already followed up."""
    hf = store.history_fields
    captured = hf["captured"]
    return (f"{hf['day']} = {quote(day)} AND ({captured} IS NULL OR {captured} = '' OR {captured} = '未判定')")


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


def load_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], where: str = "1=1",
                 cancelled: Optional[Callable[[], bool]] = None) -> list[HistoryRow]:
    """History rows (a condition by the table's names), oldest day first."""
    if not store.history:
        return []
    hf = store.history_fields
    _, oid = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"], hf["day"]))
    rows = client.query_all(store.history, where, token, geometry=False, limit=100000, cancelled=cancelled)
    out = []
    for row in rows:
        a = row.get("attributes") or {}
        rid = _text(a.get(hf["requestId"]))
        if not rid:
            continue
        cloud, scenes, done = _number(a.get(hf["cloud"])), _number(a.get(hf["scenes"])), _number(a.get(hf["boostDone"]))
        out.append(HistoryRow(
            run_id=_text(a.get(hf["runId"])), run_date=time_of(a.get(hf["runDate"])), day=_text(a.get(hf["day"]))[:10],
            request_id=rid, system=_text(a.get(hf["system"])), satellite=_text(a.get(hf["satellite"])),
            priority=_text(a.get(hf["priority"])), cloud=cloud, scenes=int(scenes) if scenes is not None else None,
            result=_text(a.get(hf["result"])) or "採用", reason=_text(a.get(hf["reason"])), file=_text(a.get(hf["file"])),
            captured=_choice(a.get(hf["captured"]), CAPTURED, "未判定"), image_id=_text(a.get(hf["imageId"])),
            boost_done=int(done or 0), object_id=int(a[oid]) if is_number(a.get(oid)) else None))
    return sorted(out, key=lambda r: (r.day, r.run_date if math.isfinite(r.run_date) else 0))


def update_history(client: EsriClient, store: RequestStoreConfig, token: Optional[str], changes: Sequence[tuple[int, dict]]) -> Saved:
    """Changes history rows (object id, {role: value}); how many were changed, which (`ok`), and why the others were not."""
    if not store.history or not changes:
        return Saved()
    hf = store.history_fields
    names, oid = _layer(client, store.history, token, f"{store.label} の出力履歴", (hf["requestId"],))
    updates = [{"attributes": _only({oid: o, **{hf[k]: v for k, v in values.items()}}, names | {oid})} for o, values in changes]
    return _batches(lambda batch: client.apply_edits(store.history, updates=batch, token=token), updates,
                    lambda r: r.update_results, "出力履歴の更新")
