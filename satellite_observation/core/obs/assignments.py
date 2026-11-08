"""
分担 (work assignments): who is looking over which image, so that no image
is worked twice. Each person takes (取得) the images they will look over
themselves; an image someone else holds is not taken again.

The assignments change every shift, so they are data kept in an Esri table
(one row per image taken); the settings file (`assignments`) only says
where it is and its attribute names. Who someone is comes from
editTracking's user name (core/edit_stamp.py, the environment variable
USERNAME by default), as the records' 記録者.

Taking checks twice, as the stores' conflict checks (#72–#74): before
adding, whether a row of the image is held by someone else; after adding,
whether someone else added one at the same moment — the first row (by its
object id) keeps the image and the later one is deleted again.

Progress is the holder's own 状態 (作業中・完了・保留) and, read from the
observation records, how many records were made in the image since it was
taken.
"""
from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields, replace
from typing import Any, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError, fit_value, quote
from ..util import is_number, js_string, ms_of, utc_text
from .alerts import EsriSource, esri_source_of
from .model import Observation

#: The states of an assignment; 完了 is done (the image no longer held: anyone may take it again, e.g. a second look).
STATUSES = ("作業中", "保留", "完了")
DONE = "完了"
#: The table's attributes by role.
DEFAULT_ASSIGNMENT_FIELDS = {
    "imageId": "IMAGE_ID", "imageName": "IMAGE_NAME", "imageTime": "IMAGE_TIME", "assignee": "ASSIGNEE", "status": "STATUS",
    "note": "NOTE", "takenAt": "TAKEN_AT", "doneAt": "DONE_AT",
}


@dataclass
class Assignment:
    """One row of the assignment table: an image someone took."""
    image_id: str
    assignee: str
    image_name: str = ""
    image_time: Optional[int] = None
    status: str = STATUSES[0]
    note: str = ""
    taken_at: Optional[int] = None
    done_at: Optional[int] = None
    oid: Optional[int] = None

    @property
    def done(self) -> bool:
        return self.status == DONE

    @property
    def label(self) -> str:
        return self.image_name or self.image_id


class TakenError(EsriError):
    """The image is held by someone else."""

    def __init__(self, holder: Assignment):
        super().__init__(f"{holder.label} は {holder.assignee or '（名前なし）'} さんが担当中（{holder.status}）")
        self.holder = holder


@dataclass
class AssignmentSource(EsriSource):
    """`assignments`: the table, and whether starting 「クリック記録」 takes the image."""
    #: `claimOnRecord`: starting 「クリック記録」 on an image takes it (refused when someone else holds it).
    claim_on_record: bool = True


def assignment_source_of(value: Any, problems: list[str]) -> Optional[AssignmentSource]:
    """`assignments`: the URL of an editable table, or { url, fields, where, token, claimOnRecord }."""
    source = esri_source_of(value, "assignments", DEFAULT_ASSIGNMENT_FIELDS, problems, editable=True)
    if source is None:
        return None
    claim = value.get("claimOnRecord", True) if isinstance(value, dict) else True
    if not isinstance(claim, bool):
        problems.append("assignments.claimOnRecord は true か false で指定")
        claim = True
    return AssignmentSource(**{f.name: getattr(source, f.name) for f in dataclass_fields(EsriSource)}, claim_on_record=claim)


def same_person(a: str, b: str) -> bool:
    """Whether two user names are the same person (case and spaces aside, as Windows' USERNAME)."""
    return bool(a.strip()) and a.strip().casefold() == b.strip().casefold()


# ---- reading and writing ---------------------------------------------------------


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def assignment_of(a: dict[str, Any], f: dict[str, str], oid: str) -> Assignment:
    status = _text(a.get(f["status"]))
    return Assignment(
        image_id=_text(a.get(f["imageId"])), assignee=_text(a.get(f["assignee"])), image_name=_text(a.get(f["imageName"])),
        image_time=ms_of(a.get(f["imageTime"])), status=status if status in STATUSES else (status or STATUSES[0]),
        note=_text(a.get(f["note"])), taken_at=ms_of(a.get(f["takenAt"])), done_at=ms_of(a.get(f["doneAt"])),
        oid=int(a[oid]) if is_number(a.get(oid)) else None,
    )


def attributes_of(x: Assignment, f: dict[str, str]) -> dict[str, Any]:
    return {
        f["imageId"]: x.image_id, f["imageName"]: x.image_name, f["imageTime"]: x.image_time, f["assignee"]: x.assignee,
        f["status"]: x.status, f["note"]: x.note, f["takenAt"]: x.taken_at, f["doneAt"]: x.done_at,
    }


def _types(client: EsriClient, source: EsriSource, token: Optional[str]) -> dict[str, str]:
    types = client.layer(source.url, token, refresh=True).field_types
    for role in ("imageId", "assignee"):
        if source.fields[role] not in types:
            raise EsriError(f"分担のテーブルに属性 {source.fields[role]} なし")
    return types


def _where(source: EsriSource, extra: str) -> str:
    return " AND ".join(f"({c})" for c in (source.where, extra) if c) or "1=1"


def load_assignments(client: EsriClient, source: EsriSource, token: Optional[str] = None, where: str = "",
                     done_since: Optional[float] = None) -> list[Assignment]:
    """
    The assignments (held first, then the newest taken). `done_since`: of
    the 完了 rows (which only grow), only those done since then (when the
    table has the state and done-time attributes).
    """
    token = token or source.token
    types = _types(client, source, token)
    oid = client.layer(source.url, token).object_id_field
    f = source.fields
    if done_since is not None and f["status"] in types and f["doneAt"] in types:
        # UTC literal: the client shifts it for services keeping local times.
        recent = (f"{f['status']} IS NULL OR {f['status']} <> {quote(DONE)} OR {f['doneAt']} IS NULL"
                  f" OR {f['doneAt']} >= TIMESTAMP '{utc_text(done_since)}'")
        where = " AND ".join(f"({c})" for c in (where, recent) if c)
    rows = client.query_all(source.url, _where(source, where), token, geometry=False, limit=100_000)
    out = [assignment_of(r.get("attributes") or {}, source.fields, oid) for r in rows]
    return sorted(out, key=lambda x: (x.done, -(x.taken_at or 0), -(x.oid or 0)))


def holders(client: EsriClient, source: EsriSource, image_id: str, token: Optional[str] = None) -> list[Assignment]:
    """The rows holding an image (not 完了), first taken first."""
    f = source.fields
    rows = load_assignments(client, source, token, f"{f['imageId']} = {quote(image_id)}")
    return sorted((x for x in rows if not x.done), key=lambda x: x.oid if x.oid is not None else 0)


def holder_of(items: Iterable[Assignment], image_id: str) -> Optional[Assignment]:
    """Who holds an image, of rows already read (the first taken)."""
    held = [x for x in items if x.image_id == image_id and not x.done]
    return min(held, key=lambda x: x.oid if x.oid is not None else 0) if held else None


def take(client: EsriClient, source: EsriSource, image_id: str, user: str, now: int, image_name: str = "",
         image_time: Optional[int] = None, note: str = "", token: Optional[str] = None) -> Assignment:
    """
    Takes an image for `user`: a TakenError when someone else holds it (also
    when they took it at the same moment); the row held when `user` holds it already.
    """
    token = token or source.token
    for x in holders(client, source, image_id, token):
        if not same_person(x.assignee, user):
            raise TakenError(x)
        return x
    types = _types(client, source, token)
    mine = Assignment(image_id=image_id, assignee=user, image_name=image_name, image_time=image_time, note=note, taken_at=now)
    values = {k: fit_value(v, types[k]) for k, v in attributes_of(mine, source.fields).items() if k in types}
    r = (client.apply_edits(source.url, adds=[{"attributes": values}], token=token).add_results or [{}])[0]
    if not r.get("success"):
        raise EsriError(f"分担の取得の失敗: {(r.get('error') or {}).get('description') or '理由不明'}")
    mine = replace(mine, oid=int(r["objectId"]) if is_number(r.get("objectId")) else None)
    # Someone else may have taken it between the check and the add: the first row keeps it.
    rows = holders(client, source, image_id, token)
    first = rows[0] if rows else mine
    if first.oid != mine.oid and not same_person(first.assignee, user):
        if mine.oid is not None:
            client.delete_features(source.url, [mine.oid], token)
        raise TakenError(first)
    return mine


def update(client: EsriClient, source: EsriSource, x: Assignment, before: Assignment, token: Optional[str] = None) -> Assignment:
    """
    Writes what changed since `before` (the row as read), so the holder's
    state and someone's note do not undo each other. Handing an image to
    someone else (a new assignee) fails when the row is no longer theirs
    to hand: it was handed or finished meanwhile.
    """
    token = token or source.token
    types = _types(client, source, token)
    f = source.fields
    old, new = attributes_of(before, f), attributes_of(x, f)
    values = {k: fit_value(v, types[k]) for k, v in new.items() if old.get(k) != v and k in types}
    if not values or x.oid is None:
        return x
    if f["assignee"] in values:
        now = [r for r in load_assignments(client, source, token, f"{client.layer(source.url, token).object_id_field} = {int(x.oid)}")]
        if not now or not same_person(now[0].assignee, before.assignee) or now[0].done != before.done:
            raise EsriError(f"{x.label} の分担はほかの人が変更済み（読み込み直して確認）")
    oid = client.layer(source.url, token).object_id_field
    r = (client.apply_edits(source.url, updates=[{"attributes": {**values, oid: x.oid}}], token=token).update_results or [{}])[0]
    if not r.get("success"):
        raise EsriError(f"分担の保存の失敗: {(r.get('error') or {}).get('description') or '理由不明'}")
    return x


def release(client: EsriClient, source: EsriSource, x: Assignment, token: Optional[str] = None) -> None:
    """Gives an image back (its row deleted): anyone may take it."""
    if x.oid is None:
        return
    results = client.delete_features(source.url, [x.oid], token or source.token)
    if not results or not all(r.get("success") for r in results):
        raise EsriError(f"分担の解除の失敗: {((results or [{}])[0].get('error') or {}).get('description') or '理由不明'}")


def with_status(x: Assignment, status: str, now: int) -> Assignment:
    """The assignment in a new state (完了 notes when; leaving 完了 forgets it)."""
    return replace(x, status=status, done_at=(x.done_at or now) if status == DONE else None)


# ---- progress ----------------------------------------------------------------


@dataclass
class Progress:
    """What the records say of an assignment."""
    #: Living records of the image made since it was taken.
    records: int = 0
    #: Of those, by the holder.
    by_assignee: int = 0
    #: The last one's time (ms).
    last: Optional[int] = None


def progress_of(x: Assignment, records: Iterable[Observation]) -> Progress:
    p = Progress()
    for o in records:
        if o.deleted_at is not None or o.image_id != x.image_id or (x.taken_at is not None and o.created_at < x.taken_at):
            continue
        p.records += 1
        p.by_assignee += same_person(o.recorded_by, x.assignee)
        p.last = max(p.last or 0, o.created_at)
    return p


def progress_text(p: Progress) -> str:
    """`記録 5 件・うち担当者 3 件`."""
    if not p.records:
        return "記録なし"
    if p.by_assignee == p.records:
        return f"記録 {p.records} 件"
    return f"記録 {p.records} 件・うち担当者 {p.by_assignee} 件"


def assignees(items: Sequence[Assignment]) -> list[str]:
    """The people assigned, in the order first met."""
    return list(dict.fromkeys(x.assignee for x in items if x.assignee))
