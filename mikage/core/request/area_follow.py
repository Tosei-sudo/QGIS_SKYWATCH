"""
地域の形に合わせる (組織の関心地域, tsudo 2026-11-24: 「ネームドエリアの形状が
更新されたら、撮像依頼も連動して更新されるようなパイプラインが欲しい」;
設計 §3, decided: MIKAGE applies it itself, and what was handed to a
system keeps the shape it was handed with).

A request made from an area keeps its 管理番号 (`named_area_id`) and, while
`area_follow` is on, takes the area's shape whenever the area's shape is
changed by a metre or more (area_link.rings_moved):

- `follows`: what each linked request needs: 更新 (a 有効・停止 request
  following its area), 対象外 (its area is stopped, gone, or its 管理番号 is
  on two rows: nothing changed, the reason shown), 形が違う (a request not
  following its area: shown only). 完了 requests are left as they are.
- `apply_follows`: the 更新 saved (each on its own: one someone else
  changed first is 保留, tried again next time) and logged with the
  request's area before (TBL_REQUEST_AREA_CHANGE, `requestStores[].areaChanges`).
- `follow_areas`: the whole of it before 「計算」 of 撮像計画ファイル出力
  (the areas changed outside MIKAGE, e.g. in ArcGIS Pro), only when the
  named area layer's last edit time moved since last time.
- `undo_change`: a 更新 undone (the area before back, following off).
- `redo_mark`: a row handed out with the area before (出し直し候補 while
  the system has not answered; kept as adopted when it has).
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from ..esri import ConflictError, EsriClient, EsriError
from ..shapes import esri_rings
from .area_link import rings_moved
from .model import ImagingRequest
from .store import HistoryRow, RequestStoreConfig, _layer, _only, load_requests, save_request, save_requests

#: What was done to a request (the change log's ACTION).
UPDATE, HOLD, SKIP, UNDO = "更新", "保留", "対象外", "元に戻す"
#: A request whose area is not its area's any more, not following it (shown, never logged).
CHANGED = "形が違う"
#: The requests whose area follows their area's (完了 ones keep theirs).
FOLLOWED = ("有効", "停止")
#: How many changes the log list reads at most.
MOST_CHANGES = 2000


@dataclass
class Follow:
    """What a linked request needs."""
    request: ImagingRequest
    #: Its area's 管理番号 (upper case) and name.
    area_id: str
    action: str
    reason: str = ""
    #: The area's shape now (更新: the request's area after).
    rings: list = field(default_factory=list)
    area_name: str = ""


def _key(area_id: str) -> str:
    return (area_id or "").strip().upper()


def follows(requests: Sequence[ImagingRequest], areas: Mapping[str, Sequence[Any]], asked: Optional[set] = None) -> list[Follow]:
    """
    What each linked request whose area was asked about (`asked`: upper-case
    ids; every id in `areas` when None) needs, from the areas as read
    (`areas`: upper-case id → the rows of it, each with id, name, rings and
    active). Requests needing nothing are left out.
    """
    asked = set(areas) if asked is None else {_key(i) for i in asked}
    out: list[Follow] = []
    for r in requests:
        key = _key(r.named_area_id)
        if not key or key not in asked or r.status not in FOLLOWED:
            continue
        rows = list(areas.get(key) or [])
        if not rows:
            out.append(Follow(r, key, SKIP, f"地域 {r.named_area_id} が地名の表にない（削除か管理番号の変更）"))
            continue
        if len(rows) > 1:
            out.append(Follow(r, key, SKIP, f"管理番号 {r.named_area_id} の地域が {len(rows)} 件（重なりを直すまで合わせない）",
                              area_name=rows[0].name))
            continue
        a = rows[0]
        if not a.rings:
            out.append(Follow(r, key, SKIP, f"地域「{a.name}」に形がない", area_name=a.name))
            continue
        if not rings_moved(r.rings, a.rings):
            continue
        new = [[list(p[:2]) for p in ring] for ring in a.rings]
        if not r.area_follow:
            out.append(Follow(r, key, CHANGED, f"地域「{a.name}」の形が変わった（地域の形に合わせるがオフなので、依頼の範囲はそのまま）",
                              new, a.name))
        elif not a.active:
            out.append(Follow(r, key, SKIP, f"地域「{a.name}」が使用停止（依頼の範囲はそのまま。依頼を止めるかは担当が決める）", new, a.name))
        else:
            out.append(Follow(r, key, UPDATE, f"地域「{a.name}」の形に合わせた", new, a.name))
    return out


@dataclass
class AreaChange:
    """A row of the change log (or one not logged: no table, or not saved)."""
    request_id: str
    name: str
    area_id: str
    action: str
    reason: str = ""
    #: The request's area before the change (更新・保留・対象外), or before it was undone (元に戻す).
    rings: list = field(default_factory=list)
    change_id: str = ""
    at: Optional[float] = None
    by: str = ""
    object_id: Optional[int] = None

    @property
    def undoable(self) -> bool:
        return self.action == UPDATE and bool(self.rings)


@dataclass
class FollowResult:
    """What `apply_follows` did."""
    #: The requests saved with their area's shape, and the changes (as logged).
    saved: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    #: Not saved (someone else changed them first, or the ledger refused): tried again next time.
    held: list = field(default_factory=list)
    #: Not changed (対象外) and not followed (形が違う).
    skipped: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    #: What could not be done (the log not written).
    problems: list = field(default_factory=list)

    def text(self) -> str:
        """「地域の形に合わせた依頼 3 件（保留 1 件・対象外 1 件）」, "" when nothing was to be done."""
        if not (self.saved or self.held or self.skipped):
            return ""
        more = [f"{label} {len(items)} 件" for label, items in (("保留", self.held), ("対象外", self.skipped)) if items]
        return f"地域の形に合わせた依頼 {len(self.saved)} 件" + (f"（{'・'.join(more)}）" if more else "")


def change_of(f: Follow, action: str, reason: str, rings: Optional[list] = None) -> AreaChange:
    r = f.request
    return AreaChange(request_id=r.id, name=r.name, area_id=f.area_id, action=action, reason=reason,
                      rings=[[list(p[:2]) for p in ring] for ring in (r.rings if rings is None else rings)],
                      change_id=str(uuid.uuid4()).upper())


def apply_follows(client: EsriClient, store: RequestStoreConfig, token: Optional[str], todo: Sequence[Follow], now: float,
                  log_skips: bool = False, cancelled: Callable[[], bool] = lambda: False) -> FollowResult:
    """
    The 更新 of `todo` saved (their area the area's shape; one someone else
    changed first is 保留) and every change logged (with `log_skips`, the
    対象外 too: an area saved in MIKAGE, once; not at every 「計算」).
    """
    out = FollowResult(skipped=[f for f in todo if f.action == SKIP], changed=[f for f in todo if f.action == CHANGED])
    updates = [f for f in todo if f.action == UPDATE]
    logged: list[AreaChange] = []
    if updates:
        by_id = {f.request.id: f for f in updates}
        sent = save_requests(client, store, token, [replace(f.request, rings=f.rings) for f in updates], now, cancelled)
        for r in sent.saved:
            f = by_id[r.id]
            out.saved.append(r)
            logged.append(change_of(f, UPDATE, f.reason))
        for r, error in sent.failed:
            f = by_id[r.id]
            why = ("ほかの人が先に依頼を変更（次の計算でまた試す）" if isinstance(error, ConflictError)
                   else f"保存できない（{error}。次の計算でまた試す）")
            out.held.append((f, why))
            logged.append(change_of(f, HOLD, why))
    if log_skips:
        logged += [change_of(f, SKIP, f.reason) for f in out.skipped]
    out.changes = logged
    if logged and store.area_changes:
        try:
            add_changes(client, store, token, logged)
        except EsriError as error:
            out.problems.append(f"依頼の範囲の変更記録を保存できない（{error}）")
    return out


def add_changes(client: EsriClient, store: RequestStoreConfig, token: Optional[str], changes: Sequence[AreaChange]) -> int:
    """The changes added to the log (`requestStores[].areaChanges`); how many went in."""
    if not store.area_changes or not changes:
        return 0
    f = store.area_change_fields
    names, _oid = _layer(client, store.area_changes, token, f"{store.label} の依頼の範囲の変更記録", (f["requestId"],))
    adds = []
    for c in changes:
        attributes = {f["id"]: c.change_id or str(uuid.uuid4()).upper(), f["areaId"]: c.area_id[:255] or None,
                      f["requestId"]: c.request_id, f["name"]: c.name[:255] or None, f["action"]: c.action,
                      f["reason"]: c.reason[:1000] or None}
        feature: dict = {"attributes": _only(attributes, names)}
        if c.rings:
            feature["geometry"] = {"rings": esri_rings(c.rings), "spatialReference": {"wkid": 4326}}
        adds.append(feature)
    result = client.apply_edits(store.area_changes, adds=adds, token=token)
    done = sum(1 for r in result.add_results if r.get("success"))
    if done < len(adds):
        raise EsriError(f"{len(adds) - done} 件を保存できない", code="E305")
    return done


def load_changes(client: EsriClient, store: RequestStoreConfig, token: Optional[str], request_ids: Optional[Sequence[str]] = None,
                 cancelled: Optional[Callable[[], bool]] = None) -> list[AreaChange]:
    """The log (of `request_ids` only, when given), the newest first (MOST_CHANGES at most)."""
    from .. import edit_stamp
    from ..esri import quote

    if not store.area_changes:
        return []
    f = store.area_change_fields
    _layer(client, store.area_changes, token, f"{store.label} の依頼の範囲の変更記録", (f["requestId"],))
    info = client.layer(store.area_changes, token)
    where = "1=1"
    if request_ids is not None:
        ids = sorted(set(request_ids))
        if not ids:
            return []
        where = f"{f['requestId']} IN ({','.join(quote(i) for i in ids[:500])})"
    tracked = edit_stamp.tracked_fields(info.field_types)
    at_field = tracked.get("createdAt", ("", ""))[0]
    by_field = tracked.get("createdBy", ("", ""))[0]
    rows = client.query_all(store.area_changes, where, token, geometry=True, limit=MOST_CHANGES * 5, cancelled=cancelled)
    out = []
    for row in rows:
        a = row.get("attributes") or {}

        def text(role: str) -> str:
            v = a.get(f[role])
            return "" if v is None else str(v).strip()

        rings = (row.get("geometry") or {}).get("rings")
        at = a.get(at_field) if at_field else None
        oid = a.get(info.object_id_field)
        out.append(AreaChange(request_id=text("requestId"), name=text("name"), area_id=text("areaId"), action=text("action"),
                              reason=text("reason"), rings=rings if isinstance(rings, list) else [], change_id=text("id"),
                              at=float(at) if isinstance(at, (int, float)) and math.isfinite(at) else None,
                              by=str(a.get(by_field) or "") if by_field else "",
                              object_id=int(oid) if isinstance(oid, (int, float)) else None))
    out.sort(key=lambda c: (c.at or 0, c.object_id or 0), reverse=True)
    return out[:MOST_CHANGES]


def undo_change(client: EsriClient, store: RequestStoreConfig, token: Optional[str], change: AreaChange, request: ImagingRequest,
                now: float) -> tuple[ImagingRequest, list[str]]:
    """
    A 更新 undone: the request's area before it back, and no longer following
    its area (else the next 「計算」 would follow it again); logged as 元に戻す
    with the area it had. The request as saved, and what could not be logged.
    """
    if not change.undoable:
        raise EsriError("元に戻せるのは「更新」の記録だけ")
    before = [[list(p[:2]) for p in ring] for ring in request.rings]
    saved = save_request(client, store, token, replace(request, rings=[[list(p[:2]) for p in ring] for ring in change.rings],
                                                       area_follow=False), now)
    problems: list[str] = []
    when = f"（{change.change_id[:8]}）" if change.change_id else ""
    log = AreaChange(request_id=request.id, name=request.name, area_id=change.area_id, action=UNDO,
                     reason=f"地域の形に合わせた変更{when}を元に戻した。地域の形に合わせるはオフ", rings=before,
                     change_id=str(uuid.uuid4()).upper())
    if store.area_changes:
        try:
            add_changes(client, store, token, [log])
        except EsriError as error:
            problems.append(f"依頼の範囲の変更記録を保存できない（{error}）")
    return saved, problems


@dataclass
class FollowRun:
    """`follow_areas`: the requests as they are after, what was done, and the layer's last edit time read."""
    requests: list
    result: Optional[FollowResult] = None
    stamp: Optional[float] = None
    #: Not looked at: the layer did not change since `since`.
    unchanged: bool = False


def follow_areas(client: EsriClient, store: RequestStoreConfig, token: Optional[str], settings: Any, requests: Sequence[ImagingRequest],
                 now: float, since: Optional[float] = None, area_token: Optional[str] = None,
                 cancelled: Optional[Callable[[], bool]] = None, checked: Optional[float] = None) -> FollowRun:
    """
    Before a day is worked out: the requests following an area whose shape
    changed take it (saved and logged). When the named area layer says it
    was not edited since `since` (its last edit time, as `FollowRun.stamp`
    told last time, at `checked`), only the requests changed since `checked`
    are looked at (one set to follow its area again, say). The requests as
    they are after (the changed ones replaced) for the day's plan.
    """
    from ..named_areas import areas_by_id, layer_stamp

    linked = [r for r in requests if r.named_area_id and r.area_follow and r.status in FOLLOWED]
    if not linked or settings is None:
        return FollowRun(list(requests))
    stamp = layer_stamp(client, settings, area_token)
    if since is not None and stamp is not None and stamp <= since:
        linked = [r for r in linked if checked is None or r.updated is None or r.updated > checked]
        if not linked:
            return FollowRun(list(requests), stamp=stamp, unchanged=True)
    ids = {_key(r.named_area_id) for r in linked}
    areas = areas_by_id(client, settings, ids, area_token, cancelled)
    result = apply_follows(client, store, token, follows(linked, areas, ids), now, cancelled=cancelled or (lambda: False))
    saved = {r.id: r for r in result.saved}
    return FollowRun([saved.get(r.id, r) for r in requests], result, stamp)


def follow_area(client: EsriClient, store: RequestStoreConfig, token: Optional[str], settings: Any, area_id: str, now: float,
                area_token: Optional[str] = None, cancelled: Optional[Callable[[], bool]] = None) -> FollowResult:
    """An area saved in MIKAGE: its requests follow its shape at once (the 対象外 logged too)."""
    from ..named_areas import areas_by_id

    key = _key(area_id)
    requests = [r for r in load_requests(client, store, token, cancelled) if _key(r.named_area_id) == key]
    if not requests:
        return FollowResult()
    areas = areas_by_id(client, settings, {key}, area_token, cancelled)
    return apply_follows(client, store, token, follows(requests, areas, {key}), now, log_skips=True,
                         cancelled=cancelled or (lambda: False))


#: A row handed out with an area its request no longer has (地域の形が変わった後).
REDO, KEPT = "出し直し候補", "前の形で採用済み"


def redo_mark(row: HistoryRow, current: Optional[Sequence], today: date) -> str:
    """
    What a row of today or later handed out with the area before says
    (`current`: its request's area now): 出し直し候補 while its system has
    not answered (writing the day again hands out the new area), 前の形で
    採用済み when it adopted it (kept: the answer is not carried to the new
    area). "" for any other row.
    """
    if not row.named_area_id or row.result != "採用" or row.part or not row.rings or not current:
        return ""
    try:
        if date.fromisoformat(row.day) < today:
            return ""
    except ValueError:
        return ""
    if not rings_moved(row.rings, current):
        return ""
    if row.adoption in ("", "不明"):
        return REDO
    if row.adoption == "採用":
        return KEPT
    return ""


def redo_text(mark: str) -> str:
    return {REDO: "出し直し候補（地域の形が変わった。書き出し直すと新しい形で出る）",
            KEPT: "システムには前の形で採用済み（地域の形が変わった。書き出し直しても採否は新しい形に引き継がない）"}.get(mark, "")


def redo_count(rows: Sequence[HistoryRow], requests: Sequence[ImagingRequest], today: date) -> int:
    """How many of `rows` are 出し直し候補 (their request's area changed since they were handed out)."""
    current = {r.id: r.rings for r in requests}
    return sum(1 for row in rows if redo_mark(row, current.get(row.request_id), today) == REDO)
