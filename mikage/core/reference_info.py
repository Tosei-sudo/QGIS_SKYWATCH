"""
出典つきの参考情報 (tsudo, 2026-11-24: 「4,5,7を実装して」; 設計方針案「画像の評価・
システムの障害・参考情報」の 7): what was learnt from outside the images —
a press release, a notice to mariners, a news item, a report from another
office — kept with where it came from and how sure it is, at a place and a
time, so the person reading an image of that place and time sees it.

Both live in Esri (the settings file's `referenceInfo` only says where,
[[config policy]]):

- the info layer (a point layer, TBL_REFERENCE_INFO): one row per info —
  its id (`REF-20261124-103000-1a2b`), title, the time of what happened
  (INFO_TIME) and of the announcement, the source (出典) and its document
  number or URL, how sure it is (確からしさ: the levels of 観測記録's 確度),
  a summary, a class of the classification (分類), the 管理番号 of the
  named area it is about and a note; the place as its point (none when not
  known);
- the link table (TBL_REFERENCE_LINK, no shape): one row per item an info is
  linked to — an event (`event`), a thing (`thing`, the 観測記録's モノ) or a
  record (`observation`) — with a label to show without asking its source.
  The same info and item twice stays one row.

Everyone reads and adds; the creator (CREATED_USER of the edit tracking) or
an administrator deletes (DELETE_ANY). A row whose creator is not known can
be deleted by anyone (the plugin cannot tell whose it is).

An image is shown the infos within `days` (7) of its time, placed in its
footprint or about a named area it shows (near_image).
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from . import edit_stamp
from .esri import EsriClient, EsriError, fit_value, quote
from .shapes import esri_rings
from .sources import EsriSource, esri_source_of
from .sql import timestamp
from .util import is_number, is_record, js_string, ms_of

#: The info layer's attributes by role.
DEFAULT_INFO_FIELDS = {
    "id": "INFO_ID", "title": "TITLE", "time": "INFO_TIME", "published": "PUBLISHED_AT", "source": "SOURCE",
    "sourceRef": "SOURCE_REF", "reliability": "RELIABILITY", "summary": "SUMMARY", "kind": "KIND", "areaId": "NAMED_AREA_ID",
    "note": "NOTE",
}
#: The link table's attributes by role.
DEFAULT_LINK_FIELDS = {"info": "INFO_ID", "type": "ITEM_TYPE", "item": "ITEM_ID", "label": "LABEL"}
#: What an info is linked to, and what the plugin calls them.
EVENT, THING, OBSERVATION = "event", "thing", "observation"
ITEM_TYPES = (EVENT, THING, OBSERVATION)
ITEM_LABELS = {EVENT: "イベント", THING: "モノ", OBSERVATION: "観測記録"}
#: Days before and after an image's time its infos are shown (`referenceInfo.days`).
DAYS = 7.0
#: The roles' feature (core/roles.py): deleting another person's info.
DELETE_ANY = "references.delete_any"
#: The attributes without which a table cannot be used.
NEEDED_INFO = ("id", "title", "time")
NEEDED_LINK = ("info", "type", "item")
#: Ids asked at a time.
CHUNK = 100
#: Infos read at most (more is an error: a shorter period).
MOST = 20_000


@dataclass
class ReferenceSettings:
    """`referenceInfo`: the info layer, the link table (None: no links) and the days around an image."""
    layer: EsriSource
    links: Optional[EsriSource] = None
    days: float = DAYS


@dataclass
class ReferenceInfo:
    """One info (times in ms since 1970; the place in WGS 84, None when not known)."""
    id: str
    title: str
    time: Optional[int] = None
    published: Optional[int] = None
    source: str = ""
    source_ref: str = ""
    reliability: str = ""
    summary: str = ""
    kind: str = ""
    area_id: str = ""
    note: str = ""
    lon: Optional[float] = None
    lat: Optional[float] = None
    oid: Optional[int] = None
    created_by: str = ""
    created_at: Optional[int] = None
    updated_by: str = ""
    updated_at: Optional[int] = None

    @property
    def has_place(self) -> bool:
        return self.lon is not None and self.lat is not None

    @property
    def url(self) -> str:
        """The document number when it is a web address ('' otherwise)."""
        return self.source_ref if is_url(self.source_ref) else ""


@dataclass
class LinkTarget:
    """An item to link infos to."""
    type: str
    item_id: str
    label: str = ""


@dataclass
class ReferenceLink:
    """A row of the link table: one info linked to one item."""
    info_id: str
    type: str
    item_id: str
    label: str = ""
    oid: Optional[int] = None
    linked_by: str = ""
    linked_at: Optional[int] = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.info_id, self.type, self.item_id


def is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def reference_settings_of(value: Any, problems: list[str]) -> Optional[ReferenceSettings]:
    """`referenceInfo`: `{ "layer", "links", "days" }` (the layer's URL alone too); None when it cannot be used."""
    at = "referenceInfo"
    if isinstance(value, str):
        value = {"layer": value}
    if not is_record(value):
        problems.append(f"{at} はオブジェクト（layer・links・days）で指定")
        return None
    for k in value:
        if k not in ("layer", "links", "days"):
            problems.append(f"{at}.{k} は使えないキー（layer・links・days）")
    days = DAYS
    if "days" in value:
        raw = value["days"]
        if is_number(raw) and 0 < float(raw) <= 366:
            days = float(raw)
        else:
            problems.append(f"{at}.days は 1〜366 の日数で指定（既定 {DAYS:g}）")
    layer = esri_source_of(value.get("layer"), f"{at}.layer", DEFAULT_INFO_FIELDS, problems, editable=True)
    links = None
    if value.get("links") is not None:
        links = esri_source_of(value.get("links"), f"{at}.links", DEFAULT_LINK_FIELDS, problems, editable=True)
    if layer is None:
        return None
    return ReferenceSettings(layer=layer, links=links, days=days)


def new_id(now: Optional[float] = None) -> str:
    """`REF-20261124-103000-1a2b` (the moment in JST and a few random letters)."""
    at = datetime.fromtimestamp((now if now is not None else time.time() * 1000) / 1000, timezone(timedelta(hours=9)))
    return f"REF-{at:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _where(source: EsriSource, *clauses: str) -> str:
    parts = [c for c in (source.where, *clauses) if c]
    return " AND ".join(f"({c})" for c in parts) or "1=1"


def _need(types: dict[str, str], fields: dict[str, str], roles: Iterable[str], what: str) -> None:
    missing = [fields[r] for r in roles if fields[r] not in types]
    if missing:
        raise EsriError(f"{what}に必要な項目がない（管理者に連絡: {'・'.join(missing)}）", code="E704")


def _oid(a: dict, oid_field: str) -> Optional[int]:
    v = a.get(oid_field)
    if is_number(v):
        return int(v)
    return next((int(x) for k, x in a.items() if k.upper() in ("OBJECTID", "FID", "OID") and is_number(x)), None)


# ---- infos ---------------------------------------------------------------------------------------


def info_of(feature: dict[str, Any], fields: dict[str, str], oid_field: str = "OBJECTID") -> Optional[ReferenceInfo]:
    """A row as an info; None without its id."""
    a = feature.get("attributes") or {}

    def get(role: str) -> Any:
        name = fields.get(role)
        return a.get(name) if name else None

    info_id = _text(get("id"))
    if not info_id:
        return None
    g = feature.get("geometry") or {}
    x, y = g.get("x"), g.get("y")
    tracking = edit_stamp.tracking().fields
    return ReferenceInfo(
        id=info_id, title=_text(get("title")) or info_id, time=ms_of(get("time")), published=ms_of(get("published")),
        source=_text(get("source")), source_ref=_text(get("sourceRef")), reliability=_text(get("reliability")),
        summary=_text(get("summary")), kind=_text(get("kind")), area_id=_text(get("areaId")), note=_text(get("note")),
        lon=float(x) if is_number(x) else None, lat=float(y) if is_number(y) else None, oid=_oid(a, oid_field),
        created_by=_text(a.get(tracking.get("createdBy", ""))), created_at=ms_of(a.get(tracking.get("createdAt", ""))),
        updated_by=_text(a.get(tracking.get("updatedBy", ""))), updated_at=ms_of(a.get(tracking.get("updatedAt", ""))),
    )


def attributes_of(info: ReferenceInfo, fields: dict[str, str], types: dict[str, str],
                  lengths: Optional[dict[str, int]] = None) -> dict:
    """The info as the layer takes it (only the attributes it has; text cut to their lengths)."""
    values = {
        "id": info.id, "title": info.title, "time": info.time, "published": info.published, "source": info.source or None,
        "sourceRef": info.source_ref or None, "reliability": info.reliability or None, "summary": info.summary or None,
        "kind": info.kind or None, "areaId": info.area_id or None, "note": info.note or None,
    }
    lengths = lengths or {}
    out = {}
    for role, v in values.items():
        name = fields.get(role)
        if not name or name not in types:
            continue
        if isinstance(v, str) and lengths.get(name):
            v = v[:lengths[name]]
        out[name] = fit_value(v, types[name])
    return out


def _info_layer(client: EsriClient, settings: ReferenceSettings, token: Optional[str], refresh: bool = False):
    s = settings.layer
    info = client.layer(s.url, token, refresh=refresh)
    _need(info.field_types, s.fields, NEEDED_INFO, "参考情報のレイヤー")
    return info


def load_infos(client: EsriClient, settings: ReferenceSettings, token: Optional[str] = None, since: Optional[float] = None,
               until: Optional[float] = None, ids: Optional[Sequence[str]] = None, area_ids: Optional[Sequence[str]] = None,
               footprint: Optional[Sequence] = None, most: int = MOST, titles: Optional[Sequence[str]] = None) -> list[ReferenceInfo]:
    """
    The infos of a period (INFO_TIME from `since` to `until`, ms; None open),
    of these ids, named areas or titles (one of them), placed in a footprint
    (WGS 84 rings), newest first. More than `most` is an error (TooManyError:
    a shorter period).
    """
    s = settings.layer
    token = token
    info = _info_layer(client, settings, token, refresh=True)
    f = s.fields
    clauses = []
    if since is not None:
        clauses.append(f"{f['time']} >= {timestamp(since)}")
    if until is not None:
        clauses.append(f"{f['time']} <= {timestamp(until)}")
    extra = None
    if footprint:
        extra = {"geometry": json.dumps({"rings": esri_rings(footprint), "spatialReference": {"wkid": 4326}}),
                 "geometryType": "esriGeometryPolygon", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
    chunks: list[Optional[list[str]]] = [None]
    key = None
    wanted = next(((role, v) for role, v in (("id", ids), ("areaId", area_ids), ("title", titles)) if v is not None), None)
    if wanted is not None:
        key = f[wanted[0]]
        if key not in info.field_types:
            return []
        values = list(dict.fromkeys(v for v in wanted[1] or [] if v))
        if not values:
            return []
        # Titles are long: fewer to a question.
        size = CHUNK // 2 if wanted[0] == "title" else CHUNK
        chunks = [values[i:i + size] for i in range(0, len(values), size)]
    out: dict[str, ReferenceInfo] = {}
    for chunk in chunks:
        where = _where(s, *clauses, f"{key} IN ({','.join(quote(v) for v in chunk)})" if chunk else "")
        for row in client.query_all(s.url, where, token, geometry=True, extra=extra, limit=most):
            x = info_of(row, f, info.object_id_field)
            if x is not None:
                out.setdefault(x.id, x)
    return sorted(out.values(), key=lambda x: (x.time is None, -(x.time or 0), x.title))


def near_image(client: EsriClient, settings: ReferenceSettings, image_time: Optional[int], footprint: Optional[Sequence],
               area_ids: Sequence[str] = (), token: Optional[str] = None) -> list[ReferenceInfo]:
    """
    The infos an image is shown: within `days` of its time (any time when it
    has none), placed in its footprint or about a named area it shows.
    """
    days_ms = settings.days * 86_400_000
    since = image_time - days_ms if image_time is not None else None
    until = image_time + days_ms if image_time is not None else None
    found: dict[str, ReferenceInfo] = {}
    if footprint:
        for x in load_infos(client, settings, token, since, until, footprint=footprint):
            found[x.id] = x
    if area_ids:
        for x in load_infos(client, settings, token, since, until, area_ids=list(area_ids)):
            found.setdefault(x.id, x)
    return sorted(found.values(), key=lambda x: (x.time is None, -(x.time or 0), x.title))


def feature_of(info: ReferenceInfo, fields: dict[str, str], layer) -> dict[str, Any]:
    """
    The info as a feature of the layer (its layer information: the attributes
    it has, their lengths and its shape); with its object id when it has one.
    """
    feature: dict[str, Any] = {"attributes": attributes_of(info, fields, layer.field_types, layer.field_lengths)}
    if layer.geometry_type == "esriGeometryPoint":
        # No place on an update: an empty point, so the place is taken away.
        if info.has_place:
            feature["geometry"] = {"x": info.lon, "y": info.lat, "spatialReference": {"wkid": 4326}}
        elif info.oid is not None:
            feature["geometry"] = {"x": None, "y": None, "spatialReference": {"wkid": 4326}}
    if info.oid is not None:
        feature["attributes"][layer.object_id_field] = info.oid
    return feature


def save_info(client: EsriClient, settings: ReferenceSettings, info: ReferenceInfo, token: Optional[str] = None) -> ReferenceInfo:
    """Adds the info (no oid yet) or updates it; the info as saved (with its oid)."""
    s = settings.layer
    token = token
    layer = _info_layer(client, settings, token, refresh=True)
    if not info.title.strip():
        raise ValueError("題名がない")
    feature = feature_of(info, s.fields, layer)
    if info.oid is None:
        # Saved already (an answer lost on the way, then 保存 again): updated, not added twice.
        there = client.find(s.url, s.fields["id"], [info.id], token, out_fields=layer.object_id_field + "," + s.fields["id"])
        if info.id in there and is_number(there[info.id].get(layer.object_id_field)):
            return save_info(client, settings, replace(info, oid=int(there[info.id][layer.object_id_field])), token)
        result = client.apply_edits(s.url, adds=[feature], token=token)
        r = (result.add_results or [{}])[0]
        if not r.get("success"):
            raise EsriError(code="E305", detail="参考情報の追加: " + ((r.get("error") or {}).get("description") or ""))
        return replace(info, oid=int(r["objectId"]) if is_number(r.get("objectId")) else None)
    result = client.apply_edits(s.url, updates=[feature], token=token)
    r = (result.update_results or [{}])[0]
    if not r.get("success"):
        raise EsriError(code="E305", detail="参考情報の更新: " + ((r.get("error") or {}).get("description") or "ほかの人が削除済みか"))
    return info


def can_delete(info: ReferenceInfo, user: str, admin: bool) -> bool:
    """The creator (or anyone when it is not known) and the administrators delete an info."""
    return admin or not info.created_by or info.created_by.casefold() == (user or "").casefold()


def delete_info(client: EsriClient, settings: ReferenceSettings, info: ReferenceInfo, token: Optional[str] = None,
                links_token: Optional[str] = None) -> int:
    """
    Deletes the info, then its links (the items themselves stay); how many
    links went. The info goes first: links left by a failure point at no info (never listed).
    """
    if info.oid is not None:
        results = client.delete_features(settings.layer.url, [info.oid], token)
        if results and not all(r.get("success") for r in results):
            raise EsriError(code="E305", detail="参考情報の削除: " + ((results[0].get("error") or {}).get("description") or ""))
    if settings.links is None:
        return 0
    return remove_links(client, settings, load_links(client, settings, [info.id], token=links_token), links_token)


# ---- links ---------------------------------------------------------------------------------------


def _link_layer(client: EsriClient, settings: ReferenceSettings, token: Optional[str]):
    s = settings.links
    if s is None:
        raise EsriError("参考情報の紐付けは使えない（管理者に連絡: referenceInfo.links）", code="E703")
    info = client.layer(s.url, token, refresh=True)
    _need(info.field_types, s.fields, NEEDED_LINK, "参考情報の紐付けテーブル")
    return info


def load_links(client: EsriClient, settings: ReferenceSettings, info_ids: Optional[Sequence[str]] = None,
               items: Optional[Sequence[tuple[str, str]]] = None, token: Optional[str] = None) -> list[ReferenceLink]:
    """The links of these infos, or of these items ((type, id) pairs); all when neither, oldest first."""
    s = settings.links
    if s is None:
        return []
    token = token
    layer = _link_layer(client, settings, token)
    f = s.fields
    tracking = edit_stamp.tracking().fields
    clauses: list[str] = []
    if info_ids is not None:
        ids = list(dict.fromkeys(i for i in info_ids if i))
        clauses = [f"{f['info']} IN ({','.join(quote(i) for i in ids[i:i + CHUNK])})" for i in range(0, len(ids), CHUNK)]
        if not clauses:
            return []
    elif items is not None:
        by_type: dict[str, list[str]] = {}
        for t, i in items:
            if t in ITEM_TYPES and i:
                by_type.setdefault(t, [])
                if i not in by_type[t]:
                    by_type[t].append(i)
        for t, ids in by_type.items():
            clauses += [f"{f['type']} = {quote(t)} AND {f['item']} IN ({','.join(quote(i) for i in ids[i:i + CHUNK])})"
                        for i in range(0, len(ids), CHUNK)]
        if not clauses:
            return []
    out: dict[tuple, ReferenceLink] = {}
    for clause in clauses or [""]:
        for r in client.query_all(s.url, _where(s, clause), token, geometry=False):
            a = r.get("attributes") or {}
            link = ReferenceLink(
                info_id=_text(a.get(f["info"])), type=_text(a.get(f["type"])), item_id=_text(a.get(f["item"])),
                label=_text(a.get(f.get("label", ""))), oid=_oid(a, layer.object_id_field),
                linked_by=_text(a.get(tracking.get("createdBy", ""))), linked_at=ms_of(a.get(tracking.get("createdAt", ""))),
            )
            if link.info_id and link.item_id and link.type in ITEM_TYPES:
                out.setdefault((link.oid, *link.key), link)
    return sorted(out.values(), key=lambda x: (x.linked_at or 0, x.oid or 0))


def add_links(client: EsriClient, settings: ReferenceSettings, info_ids: Sequence[str], items: Sequence[LinkTarget],
              token: Optional[str] = None) -> tuple[int, int]:
    """Links the infos to the items; (added, already there). The same info and item twice stays one row."""
    return add_link_pairs(client, settings, [(i, item) for i in info_ids for item in items], token)


def add_link_pairs(client: EsriClient, settings: ReferenceSettings, pairs: Sequence[tuple[str, LinkTarget]],
                   token: Optional[str] = None) -> tuple[int, int]:
    """Links each info (by id) to its item; (added, already there). The same info and item twice stays one row."""
    s = settings.links
    if s is None:
        raise EsriError("参考情報の紐付けは使えない（管理者に連絡: referenceInfo.links）", code="E703")
    token = token
    layer = _link_layer(client, settings, token)
    types, lengths, f = layer.field_types, layer.field_lengths, s.fields
    have = {x.key for x in load_links(client, settings, list(dict.fromkeys(i for i, _item in pairs)), token=token)}
    adds, seen, skipped = [], set(), 0
    for info_id, item in pairs:
        if item.type not in ITEM_TYPES:
            raise ValueError(item.type)
        key = (info_id, item.type, item.item_id)
        if key in have or key in seen:
            skipped += 1
            continue
        seen.add(key)
        values = {f["info"]: info_id, f["type"]: item.type, f["item"]: item.item_id, f.get("label", ""): item.label or None}
        attributes = {}
        for k, v in values.items():
            if k in types:
                attributes[k] = fit_value(v[:lengths[k]] if isinstance(v, str) and lengths.get(k) else v, types[k])
        adds.append({"attributes": attributes})
    added, failures = 0, []
    for start in range(0, len(adds), 200):
        for r in client.apply_edits(s.url, adds[start:start + 200], token=token).add_results:
            if r.get("success"):
                added += 1
            else:
                failures.append((r.get("error") or {}).get("description") or "追加失敗")
    if failures:
        raise EsriError(f"{len(failures)} 件をサーバーが受け付けない（{added} 件は追加済み）", code="E305", detail=failures[0])
    return added, skipped


def remove_links(client: EsriClient, settings: ReferenceSettings, links: Sequence[ReferenceLink], token: Optional[str] = None) -> int:
    """Takes links away (their rows deleted; the infos and items stay); how many."""
    ids = list(dict.fromkeys(x.oid for x in links if x.oid is not None))
    if not ids or settings.links is None:
        return 0
    removed = 0
    for start in range(0, len(ids), 500):
        results = client.delete_features(settings.links.url, ids[start:start + 500], token)
        failed = [r for r in results if not r.get("success")]
        if failed:
            raise EsriError(f"紐付け {len(failed)} 件の解除をサーバーが受け付けない", code="E305",
                            detail=(failed[0].get("error") or {}).get("description") or "")
        removed += len(results) or len(ids[start:start + 500])
    return removed


# ---- showing -------------------------------------------------------------------------------------


def matches(info: ReferenceInfo, text: str = "", box: Optional[Sequence[float]] = None) -> bool:
    """
    Whether an info is listed for a search text (its title, summary, source,
    document number and class) and a box (west, south, east, north: placed in it).
    """
    if box is not None:
        if not info.has_place:
            return False
        w, s, e, n = box
        if not (w <= info.lon <= e and s <= info.lat <= n):
            return False
    words = [w for w in text.casefold().split() if w]
    hay = " ".join((info.title, info.summary, info.source, info.source_ref, info.kind)).casefold()
    return all(w in hay for w in words)


def local_text(ms: Optional[int]) -> str:
    return "" if ms is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def source_text(info: ReferenceInfo) -> str:
    """`海上保安庁（航行警報 第123号）` — the source and its document number (not a URL, shown on its own)."""
    ref = info.source_ref if info.source_ref and not info.url else ""
    if info.source and ref:
        return f"{info.source}（{ref}）"
    return info.source or ref


def detail_lines(info: ReferenceInfo, area_name: str = "", links: Sequence[ReferenceLink] = ()) -> list[str]:
    """A few lines about an info (the dock's text under its list, a copy to paste)."""
    head = f"【{info.title}】" + (f" {info.reliability}" if info.reliability else "")
    lines = [head, f"出来事: {local_text(info.time)}" + (f"　発表: {local_text(info.published)}" if info.published else "")]
    if info.source or info.source_ref:
        lines.append("出典: " + (source_text(info) or info.source_ref) + (f"　{info.url}" if info.url and info.source else ""))
    if area_name or info.area_id:
        lines.append(f"地名: {area_name or info.area_id}")
    if info.kind:
        lines.append(f"分類: {info.kind}")
    if info.summary:
        lines.append(info.summary)
    if info.note:
        lines.append(f"メモ: {info.note}")
    if links:
        lines.append("紐付け: " + "、".join(f"{ITEM_LABELS.get(x.type, x.type)}「{x.label or x.item_id}」" for x in links))
    return lines
