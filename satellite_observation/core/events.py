"""
Events (イベント): a disaster or incident (an earthquake, a flood …) that
bundles the images, observation records and imaging requests made for it, so
they are found, shown and handed over together.

Both live in Esri (the settings file's `events` only says where and the
attribute names, [[config policy]]):

- the event layer (a polygon layer, or a table when no area is kept): one row
  per event — its id, name, kind, status (対応中 / 終了), period and note,
  the area as its geometry;
- the link table: one row per item in an event — the event's id, the item's
  type (`image`, `observation`, `request`), its id, a label and time to show
  without asking its source, where it comes from (the catalog's or store's URL)
  and a note.

An item can be in several events, and an event holds any number of items
(tsudo, 2026-11-07: 多対多). A link is unique per (event, type, id): adding
the same item again does nothing.
"""
from __future__ import annotations

import csv
import io
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional, Sequence

from . import edit_stamp
from .esri import EsriClient, EsriError, fit_value, quote
from .obs.alerts import EsriSource, _source_of
from .util import is_number, is_record, js_string, ms_of

#: The item types, and what the plugin calls them.
TYPES = ("image", "observation", "request")
TYPE_LABELS = {"image": "画像", "observation": "観測記録", "request": "撮像依頼"}
#: The statuses: an event 対応中 is offered for adding items; 終了 ones are listed on demand.
OPEN, CLOSED = "対応中", "終了"
STATUSES = (OPEN, CLOSED)
#: Kinds offered when the layer has none yet (the ones in the layer come first; any text can be typed).
SUGGESTED_KINDS = ("地震", "津波", "風水害", "火山", "火災", "事故", "その他")

#: The event layer's attributes by role.
DEFAULT_EVENT_FIELDS = {
    "id": "EVENT_ID", "name": "EVENT_NAME", "kind": "EVENT_KIND", "status": "STATUS", "start": "START_AT", "end": "END_AT",
    "note": "NOTE",
}
#: The link table's attributes by role.
DEFAULT_LINK_FIELDS = {
    "event": "EVENT_ID", "type": "ITEM_TYPE", "item": "ITEM_ID", "label": "ITEM_LABEL", "time": "ITEM_TIME", "source": "SOURCE",
    "note": "NOTE",
}


@dataclass
class EventSettings:
    """`events`: the event layer and the link table."""
    layer: EsriSource
    links: EsriSource


@dataclass
class Event:
    """One event (times in ms since 1970; the area as rings in WGS 84)."""
    id: str
    name: str
    kind: str = ""
    status: str = OPEN
    start: Optional[int] = None
    end: Optional[int] = None
    note: str = ""
    rings: Optional[list] = None
    oid: Optional[int] = None
    created_by: str = ""
    updated_at: Optional[int] = None

    @property
    def open(self) -> bool:
        return self.status != CLOSED


@dataclass
class LinkItem:
    """An item to put in an event."""
    type: str
    item_id: str
    label: str = ""
    time: Optional[int] = None
    source: str = ""


@dataclass
class EventLink:
    """A row of the link table: one item in one event."""
    event_id: str
    type: str
    item_id: str
    label: str = ""
    time: Optional[int] = None
    source: str = ""
    note: str = ""
    oid: Optional[int] = None
    linked_by: str = ""
    linked_at: Optional[int] = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.event_id, self.type, self.item_id


def events_settings_of(value: Any, problems: list[str]) -> Optional[EventSettings]:
    """`events`; None when it cannot be used."""
    at = "events"
    if not is_record(value):
        problems.append(f"{at} はオブジェクト（layer・links）で指定")
        return None
    for k in value:
        if k not in ("layer", "links"):
            problems.append(f"{at}.{k} は使用不可のキー（layer・links）")
    layer = _source_of(value.get("layer"), f"{at}.layer", DEFAULT_EVENT_FIELDS, problems, editable=True)
    links = _source_of(value.get("links"), f"{at}.links", DEFAULT_LINK_FIELDS, problems, editable=True)
    if layer is None or links is None:
        return None
    return EventSettings(layer=layer, links=links)


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def _token(source: EsriSource, token: Optional[str]) -> Optional[str]:
    return token or source.token


def _where(source: EsriSource, *clauses: str) -> str:
    parts = [c for c in (source.where, *clauses) if c]
    return " AND ".join(f"({c})" for c in parts) or "1=1"


def _need(types: dict[str, str], fields: dict[str, str], roles: Iterable[str], what: str) -> None:
    missing = [fields[r] for r in roles if fields[r] not in types]
    if missing:
        raise EsriError(f"{what}に属性 {'・'.join(missing)} なし")


# ---- events --------------------------------------------------------------------------------------


def event_of(feature: dict[str, Any], fields: dict[str, str]) -> Optional[Event]:
    a = feature.get("attributes") or {}
    event_id = _text(a.get(fields["id"]))
    if not event_id:
        return None
    rings = (feature.get("geometry") or {}).get("rings") or None
    tracking = edit_stamp.tracking().fields
    status = _text(a.get(fields["status"]))
    return Event(
        id=event_id, name=_text(a.get(fields["name"])) or event_id, kind=_text(a.get(fields["kind"])),
        status=CLOSED if status == CLOSED else OPEN, start=ms_of(a.get(fields["start"])), end=ms_of(a.get(fields["end"])),
        note=_text(a.get(fields["note"])), rings=rings,
        oid=next((int(v) for k, v in a.items() if k.upper() in ("OBJECTID", "FID", "OID") and is_number(v)), None),
        created_by=_text(a.get(tracking.get("createdBy", ""))), updated_at=ms_of(a.get(tracking.get("updatedAt", ""))),
    )


def load_events(client: EsriClient, settings: EventSettings, token: Optional[str] = None) -> list[Event]:
    """Every event (newest start first; those without a start last)."""
    s = settings.layer
    token = _token(s, token)
    info = client.layer(s.url, token, refresh=True)
    _need(info.field_types, s.fields, ("id", "name"), "イベントのレイヤー")
    polygon = info.geometry_type == "esriGeometryPolygon"
    rows = client.query_all(s.url, _where(s), token, geometry=polygon, limit=100_000)
    oid = info.object_id_field
    out = []
    for f in rows:
        e = event_of(f, s.fields)
        if e is None:
            continue
        a = f.get("attributes") or {}
        e.oid = int(a[oid]) if is_number(a.get(oid)) else e.oid
        out.append(e)
    return sorted(out, key=lambda e: (e.start is None, -(e.start or 0), e.name))


def new_event(name: str, **values: Any) -> Event:
    return Event(id=str(uuid.uuid4()), name=name.strip(), **values)


def save_event(client: EsriClient, settings: EventSettings, event: Event, token: Optional[str] = None) -> Event:
    """Adds the event (no oid yet) or updates it; the event as saved (with its oid)."""
    s = settings.layer
    token = _token(s, token)
    info = client.layer(s.url, token, refresh=True)
    types = info.field_types
    _need(types, s.fields, ("id", "name"), "イベントのレイヤー")
    f = s.fields
    values = {f["id"]: event.id, f["name"]: event.name, f["kind"]: event.kind, f["status"]: event.status,
              f["start"]: event.start, f["end"]: event.end, f["note"]: event.note}
    attributes = {k: fit_value(v, types[k]) for k, v in values.items() if k in types}
    feature: dict[str, Any] = {"attributes": attributes}
    if info.geometry_type == "esriGeometryPolygon" and (event.rings or event.oid is not None):
        # No area on an update: an empty polygon, so the area is taken away.
        feature["geometry"] = {"rings": event.rings or [], "spatialReference": {"wkid": 4326}}
    if event.oid is None:
        # Saved already (an answer lost on the way, then 保存 again): updated, not added twice.
        there = client.find(s.url, f["id"], [event.id], token, out_fields=info.object_id_field + "," + f["id"])
        if event.id in there and is_number(there[event.id].get(info.object_id_field)):
            return save_event(client, settings, replace(event, oid=int(there[event.id][info.object_id_field])), token)
    if event.oid is None:
        result = client.apply_edits(s.url, adds=[feature], token=token)
        r = (result.add_results or [{}])[0]
        if not r.get("success"):
            raise EsriError("イベントの追加失敗（" + ((r.get("error") or {}).get("description") or "理由不明") + "）")
        return replace(event, oid=int(r["objectId"]) if is_number(r.get("objectId")) else None)
    attributes[info.object_id_field] = event.oid
    result = client.apply_edits(s.url, updates=[feature], token=token)
    r = (result.update_results or [{}])[0]
    if not r.get("success"):
        raise EsriError("イベントの更新失敗（" + ((r.get("error") or {}).get("description") or "ほかの人が削除済みか") + "）")
    return event


def set_status(client: EsriClient, settings: EventSettings, event: Event, status: str, now: Optional[int] = None,
               token: Optional[str] = None) -> Event:
    """Closes (with an end, now when it had none) or reopens an event."""
    if status not in STATUSES:
        raise ValueError(status)
    changed = replace(event, status=status)
    if status == CLOSED and changed.end is None:
        changed.end = int(now if now is not None else time.time() * 1000)
    return save_event(client, settings, changed, token)


def delete_event(client: EsriClient, settings: EventSettings, event: Event, token: Optional[str] = None,
                 links_token: Optional[str] = None) -> int:
    """
    Deletes the event, then its links (the items themselves stay); how many
    links went. The event goes first: should the links then fail, they are
    left pointing at no event (never listed) rather than an event left without them.
    """
    links = load_links(client, settings, [event.id], links_token)
    if event.oid is not None:
        results = client.delete_features(settings.layer.url, [event.oid], _token(settings.layer, token))
        if results and not all(r.get("success") for r in results):
            raise EsriError("イベントの削除失敗（" + ((results[0].get("error") or {}).get("description") or "理由不明") + "）")
    return remove_links(client, settings, links, links_token)


# ---- links ---------------------------------------------------------------------------------------


def _link_types(client: EsriClient, settings: EventSettings, token: Optional[str]) -> dict[str, str]:
    s = settings.links
    types = client.layer(s.url, _token(s, token), refresh=True).field_types
    _need(types, s.fields, ("event", "type", "item"), "イベントの紐付けテーブル")
    return types


def load_links(client: EsriClient, settings: EventSettings, event_ids: Optional[Sequence[str]] = None,
               token: Optional[str] = None) -> list[EventLink]:
    """The links of these events (all when None), oldest first."""
    s = settings.links
    token = _token(s, token)
    _link_types(client, settings, token)
    f = s.fields
    oid = client.layer(s.url, token).object_id_field
    tracking = edit_stamp.tracking().fields
    if event_ids is not None:
        ids = list(dict.fromkeys(i for i in event_ids if i))
        chunks = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    else:
        chunks = [None]
    out = []
    for chunk in chunks:
        clause = f"{f['event']} IN ({','.join(quote(i) for i in chunk)})" if chunk else ""
        for r in client.query_all(s.url, _where(s, clause), token, geometry=False, limit=1_000_000):
            a = r.get("attributes") or {}
            link = EventLink(
                event_id=_text(a.get(f["event"])), type=_text(a.get(f["type"])), item_id=_text(a.get(f["item"])),
                label=_text(a.get(f["label"])), time=ms_of(a.get(f["time"])), source=_text(a.get(f["source"])),
                note=_text(a.get(f["note"])), oid=int(a[oid]) if is_number(a.get(oid)) else None,
                linked_by=_text(a.get(tracking.get("createdBy", ""))), linked_at=ms_of(a.get(tracking.get("createdAt", ""))),
            )
            if link.event_id and link.item_id and link.type in TYPES:
                out.append(link)
    return sorted(out, key=lambda x: (x.linked_at or 0, x.oid or 0))


def add_links(client: EsriClient, settings: EventSettings, event_ids: Sequence[str], items: Sequence[LinkItem],
              note: str = "", token: Optional[str] = None) -> tuple[int, int]:
    """
    Puts the items in the events; (added, already there). An item already in
    an event (the same type and id) is left as it is.
    """
    s = settings.links
    token = _token(s, token)
    types = _link_types(client, settings, token)
    f = s.fields
    have = {x.key for x in load_links(client, settings, event_ids, token)}
    adds, seen, skipped = [], set(), 0
    for event_id in event_ids:
        for item in items:
            if item.type not in TYPES:
                raise ValueError(item.type)
            key = (event_id, item.type, item.item_id)
            if key in have or key in seen:
                skipped += 1
                continue
            seen.add(key)
            values = {f["event"]: event_id, f["type"]: item.type, f["item"]: item.item_id, f["label"]: item.label,
                      f["time"]: item.time, f["source"]: item.source, f["note"]: note}
            adds.append({"attributes": {k: fit_value(v, types[k]) for k, v in values.items() if k in types}})
    added, failures = 0, []
    for start in range(0, len(adds), 200):
        for r in client.apply_edits(s.url, adds[start:start + 200], token=token).add_results:
            if r.get("success"):
                added += 1
            else:
                failures.append((r.get("error") or {}).get("description") or "追加失敗")
    if failures:
        raise EsriError(f"イベントへの追加: {added} 件成功・{len(failures)} 件失敗（{failures[0]}）")
    return added, skipped


def set_link_note(client: EsriClient, settings: EventSettings, links: Sequence[EventLink], note: str,
                  token: Optional[str] = None) -> int:
    """Sets the note of links; how many were changed."""
    s = settings.links
    token = _token(s, token)
    types = _link_types(client, settings, token)
    if s.fields["note"] not in types:
        raise EsriError(f"イベントの紐付けテーブルに属性 {s.fields['note']} なし")
    oid = client.layer(s.url, token).object_id_field
    updates = [{"attributes": {oid: x.oid, s.fields["note"]: fit_value(note, types[s.fields["note"]])}} for x in links if x.oid is not None]
    if not updates:
        return 0
    results = client.apply_edits(s.url, updates=updates, token=token).update_results
    failed = [r for r in results if not r.get("success")]
    if failed:
        raise EsriError(f"メモの保存: {len(failed)} 件失敗")
    return len(updates)


def remove_links(client: EsriClient, settings: EventSettings, links: Sequence[EventLink], token: Optional[str] = None) -> int:
    """Takes items out of their events (the link rows deleted); how many."""
    ids = [x.oid for x in links if x.oid is not None]
    if not ids:
        return 0
    removed = 0
    for start in range(0, len(ids), 500):
        results = client.delete_features(settings.links.url, ids[start:start + 500], _token(settings.links, token))
        failed = [r for r in results if not r.get("success")]
        if failed:
            raise EsriError(f"紐付けの解除: {len(failed)} 件失敗（{(failed[0].get('error') or {}).get('description') or ''}）")
        removed += len(results) or len(ids[start:start + 500])
    return removed


def counts(links: Iterable[EventLink]) -> dict[str, dict[str, int]]:
    """How many items of each type each event holds."""
    out: dict[str, dict[str, int]] = {}
    for x in links:
        c = out.setdefault(x.event_id, {t: 0 for t in TYPES})
        c[x.type] = c.get(x.type, 0) + 1
    return out


def events_of_item(links: Iterable[EventLink], type_: str, item_id: str) -> list[str]:
    """The ids of the events an item is in."""
    return list(dict.fromkeys(x.event_id for x in links if x.type == type_ and x.item_id == item_id))


def matches(event: Event, text: str = "", status: str = "") -> bool:
    """Whether an event is listed for a search text (its name, kind, note) and status ('' any)."""
    if status and event.status != status:
        return False
    words = [w for w in text.lower().split() if w]
    hay = " ".join((event.name, event.kind, event.note)).lower()
    return all(w in hay for w in words)


# ---- export --------------------------------------------------------------------------------------


def _local(ms: Optional[int]) -> str:
    return "" if ms is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


EXPORT_HEADER = ("イベント", "種別", "状態", "項目の種類", "ID", "名称", "日時", "出典", "メモ", "追加者", "追加日時")


def export_rows(event: Event, links: Sequence[EventLink]) -> list[list[str]]:
    """The event's items as rows (EXPORT_HEADER), images first."""
    order = {t: i for i, t in enumerate(TYPES)}
    rows = []
    for x in sorted((x for x in links if x.event_id == event.id), key=lambda x: (order.get(x.type, 9), x.time or 0, x.label)):
        rows.append([event.name, event.kind, event.status, TYPE_LABELS.get(x.type, x.type), x.item_id, x.label, _local(x.time),
                     x.source, x.note, x.linked_by, _local(x.linked_at)])
    return rows


def export_csv(event: Event, links: Sequence[EventLink]) -> str:
    """The event's items as CSV (with a header row; write it as UTF-8 with a BOM for Excel)."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(EXPORT_HEADER)
    for row in export_rows(event, links):
        w.writerow([_safe_cell(v) for v in row])
    return out.getvalue()


def _safe_cell(value: str) -> str:
    """A cell Excel won't run as a formula (`=`, `+`, `-`, `@` first are written after a quote)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def summary_text(event: Event, links: Sequence[EventLink]) -> str:
    """A few lines about the event (to paste into a report or a handover)."""
    c = counts(links).get(event.id, {t: 0 for t in TYPES})
    period = f"{_local(event.start)} 〜 {_local(event.end)}".strip()
    lines = [f"【{event.name}】{('（' + event.kind + '）') if event.kind else ''} {event.status}",
             f"期間: {period}" if event.start or event.end else "",
             "　".join(f"{TYPE_LABELS[t]} {c.get(t, 0)} 件" for t in TYPES),
             f"メモ: {event.note}" if event.note else ""]
    return "\n".join(line for line in lines if line)
