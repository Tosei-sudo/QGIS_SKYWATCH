"""
個人の関心地域・地点 (tsudo, 2026-11-14: 「写っている地物、個人の関心地域・地点を
任意に設定できるレイヤーを用意して、それを組み込みたい」): points and areas each
person sets for themselves. A new image's card (新着画像) names the ones it
covers in 「写っている地物」; the dock 「関心地域・地点」 adds, edits and deletes
them and shows them on the map.

They live in Esri (tsudo chose Esri over this PC, so that they follow the
person to any PC): a point layer and a polygon layer with the same
attributes, one row per item, the owner column holding the user name (the
one `editTracking` writes: the environment variable USERNAME by default).
Each person reads and writes only their own rows (compared in capitals, as
Windows user names are).

The settings file's `interests` is written its own way (「このレイヤーだけ
コンフィグも特別な書き方」), not as one of a catalog's `footprintLayers`: the two
layers' URLs once, for every catalog's cards; no `fields` or `format` to list
(the plugin knows the attributes, their names changeable by role).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional, Sequence

from . import edit_stamp
from .esri import EsriClient, EsriError, fit_value, quote
from .obs.alerts import EsriSource, esri_source_of
from .util import is_number, is_record, js_string, ms_of

#: The two kinds, and what the plugin calls them.
POINT, AREA = "point", "area"
KINDS = (POINT, AREA)
KIND_LABELS = {POINT: "地点", AREA: "地域"}
#: The heading on the cards (and the dock's default wording).
DEFAULT_LABEL = "関心地域・地点"
#: A new item's colour when the settings file names none.
DEFAULT_COLOR = "#f97316"
#: The attributes by role (both layers alike).
DEFAULT_INTEREST_FIELDS = {"id": "INTEREST_ID", "name": "INTEREST_NAME", "owner": "OWNER_USER", "color": "COLOR", "note": "NOTE"}
#: Roles without which a layer is not used.
NEEDED = ("id", "name", "owner")
#: The geometry each kind's layer has.
GEOMETRY = {POINT: "esriGeometryPoint", AREA: "esriGeometryPolygon"}
#: More than this of one person's items is an error (never cut silently).
LIMIT = 10_000


@dataclass(frozen=True)
class InterestSettings:
    """`interests`: the point layer and the area layer (either may be left out)."""
    points: Optional[EsriSource] = None
    areas: Optional[EsriSource] = None
    label: str = DEFAULT_LABEL
    color: str = DEFAULT_COLOR

    def source(self, kind: str) -> Optional[EsriSource]:
        return self.points if kind == POINT else self.areas

    @property
    def kinds(self) -> tuple[str, ...]:
        """The kinds the settings file has a layer for."""
        return tuple(k for k in KINDS if self.source(k) is not None)


@dataclass
class Interest:
    """One point or area (WGS 84: `x`/`y` for a point, Esri rings for an area)."""
    id: str
    kind: str
    name: str
    color: str = DEFAULT_COLOR
    note: str = ""
    x: Optional[float] = None
    y: Optional[float] = None
    rings: Optional[list] = None
    oid: Optional[int] = None
    owner: str = ""
    updated_at: Optional[int] = None

    @property
    def has_shape(self) -> bool:
        if self.kind == POINT:
            return is_number(self.x) and is_number(self.y)
        return bool(self.rings and any(len(r) >= 3 for r in self.rings))

    def shape_text(self) -> str:
        """The shape in words: 「地点 139.65000, 35.28000」, 「地域（頂点 5）」, or 「形なし」."""
        if not self.has_shape:
            return "形なし"
        if self.kind == POINT:
            return f"地点 {self.x:.5f}, {self.y:.5f}"
        corners = sum(max(0, len(r) - (1 if len(r) > 1 and r[0] == r[-1] else 0)) for r in self.rings or [])
        parts = len(self.rings or [])
        return f"地域（頂点 {corners}）" if parts == 1 else f"地域（{parts} 輪・頂点 {corners}）"

    def esri_geometry(self) -> Optional[dict[str, Any]]:
        from .request.store import esri_rings

        if not self.has_shape:
            return None
        if self.kind == POINT:
            return {"x": self.x, "y": self.y, "spatialReference": {"wkid": 4326}}
        return {"rings": esri_rings(self.rings or []), "spatialReference": {"wkid": 4326}}


def _color_of(value: Any) -> Optional[str]:
    from .catalog.thumbnail import is_color

    return value.strip() if is_color(value) else None


def interests_settings_of(value: Any, problems: list[str]) -> Optional[InterestSettings]:
    """
    `interests`: `{ "points": URL, "areas": URL, "label", "color" }` (each layer
    a URL or `{ "url", "fields", "where", "token" }`); None when it cannot be used.
    """
    at = "interests"
    if not is_record(value):
        problems.append(f'{at} は {{ "points": 地点のレイヤーの URL, "areas": 地域のレイヤーの URL }} で指定')
        return None
    for k in value:
        if k not in ("points", "areas", "label", "color"):
            problems.append(f"{at}.{k} は使用不可のキー（points・areas・label・color）")
    sources = {}
    for key in ("points", "areas"):
        if value.get(key) is not None:
            sources[key] = esri_source_of(value[key], f"{at}.{key}", DEFAULT_INTEREST_FIELDS, problems, editable=True)
    if not any(sources.values()):
        if not sources:
            problems.append(f"{at} は points（地点のレイヤー）か areas（地域のレイヤー）のどちらかが必要")
        return None
    label = value.get("label")
    if label is not None and not (isinstance(label, str) and label.strip()):
        problems.append(f"{at}.label は文字列で指定（カードの見出し）")
    color = value.get("color")
    if color is not None and _color_of(color) is None:
        problems.append(f"{at}.color は #rrggbb か色の名前（orange など）で指定")
    return InterestSettings(
        points=sources.get("points"), areas=sources.get("areas"),
        label=label.strip() if isinstance(label, str) and label.strip() else DEFAULT_LABEL,
        color=_color_of(color) or DEFAULT_COLOR,
    )


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def owner_where(source: EsriSource, user: str) -> str:
    """The rows of `user` (compared in capitals), within the settings file's own `where`."""
    mine = f"UPPER({source.fields['owner']}) = {quote(user.strip().upper())}"
    return f"({source.where}) AND ({mine})" if source.where else mine


def _need(types: dict[str, str], source: EsriSource, what: str) -> None:
    missing = [source.fields[r] for r in NEEDED if source.fields[r] not in types]
    if missing:
        raise EsriError(f"{what}に属性 {'・'.join(missing)} なし")


def _layer(client: EsriClient, source: EsriSource, kind: str, token: Optional[str]):
    info = client.layer(source.url, token or source.token, refresh=True)
    what = f"関心{KIND_LABELS[kind]}のレイヤー"
    if info.geometry_type != GEOMETRY[kind]:
        raise EsriError(f"{what}は{'ポイント' if kind == POINT else 'ポリゴン'}のレイヤーで（{info.geometry_type or '形状なし'}）")
    _need(info.field_types, source, what)
    return info


def interest_of(feature: dict[str, Any], kind: str, fields: dict[str, str], oid_field: str = "OBJECTID",
                default_color: str = DEFAULT_COLOR) -> Optional[Interest]:
    a = feature.get("attributes") or {}
    item_id = _text(a.get(fields["id"]))
    if not item_id:
        return None
    g = feature.get("geometry") or {}
    x = g.get("x") if is_number(g.get("x")) else None
    y = g.get("y") if is_number(g.get("y")) else None
    rings = g.get("rings") if isinstance(g.get("rings"), list) and g.get("rings") else None
    tracking = edit_stamp.tracking().fields
    oid = a.get(oid_field)
    if not is_number(oid):
        oid = next((v for k, v in a.items() if k.upper() in ("OBJECTID", "FID", "OID") and is_number(v)), None)
    return Interest(
        id=item_id, kind=kind, name=_text(a.get(fields["name"])) or item_id,
        color=_color_of(_text(a.get(fields["color"]))) or default_color, note=_text(a.get(fields["note"])),
        x=x if kind == POINT else None, y=y if kind == POINT else None, rings=rings if kind == AREA else None,
        oid=int(oid) if is_number(oid) else None, owner=_text(a.get(fields["owner"])),
        updated_at=ms_of(a.get(tracking.get("updatedAt", ""))) or ms_of(a.get(tracking.get("createdAt", ""))),
    )


def load_interests(client: EsriClient, settings: InterestSettings, user: str,
                   tokens: Optional[dict[str, Optional[str]]] = None) -> list[Interest]:
    """`user`'s points and areas, by name (areas and points mixed)."""
    if not user.strip():
        raise EsriError("利用者名が分からない（環境変数 USERNAME がない。設定ファイルの editTracking.user で指定）")
    out: list[Interest] = []
    for kind in settings.kinds:
        source = settings.source(kind)
        token = (tokens or {}).get(kind) or source.token
        info = _layer(client, source, kind, token)
        rows = client.query_all(source.url, owner_where(source, user), token, geometry=True, limit=LIMIT)
        out.extend(i for i in (interest_of(f, kind, source.fields, info.object_id_field, settings.color) for f in rows) if i is not None)
    return sorted(out, key=lambda i: (i.name.casefold(), i.kind, i.id))


def new_interest(kind: str, settings: Optional[InterestSettings] = None, **values: Any) -> Interest:
    if kind not in KINDS:
        raise ValueError(kind)
    values.setdefault("color", settings.color if settings else DEFAULT_COLOR)
    return Interest(id=str(uuid.uuid4()), kind=kind, name=str(values.pop("name", "")).strip(), **values)


def _attributes(item: Interest, source: EsriSource, user: str, types: dict[str, str], lengths: dict[str, int]) -> dict[str, Any]:
    f = source.fields
    values = {f["id"]: item.id, f["name"]: item.name.strip(), f["owner"]: user.strip(), f["color"]: item.color or None,
              f["note"]: item.note.strip() or None}
    out = {}
    for k, v in values.items():
        if k not in types:
            continue
        if isinstance(v, str) and lengths.get(k):
            v = v[:lengths[k]]
        out[k] = fit_value(v, types[k])
    return out


def save_interest(client: EsriClient, settings: InterestSettings, item: Interest, user: str,
                  token: Optional[str] = None) -> Interest:
    """Adds the item (no oid yet) or updates it; the item as saved (with its oid). Its owner is `user`."""
    if not item.name.strip():
        raise ValueError("名前が必要")
    if not item.has_shape:
        raise ValueError("形が必要（地図でクリックか描画）")
    if not user.strip():
        raise EsriError("利用者名が分からない（設定ファイルの editTracking.user で指定）")
    source = settings.source(item.kind)
    if source is None:
        raise EsriError(f"設定ファイルの interests に{KIND_LABELS[item.kind]}のレイヤー（{'points' if item.kind == POINT else 'areas'}）なし")
    token = token or source.token
    info = _layer(client, source, item.kind, token)
    attributes = _attributes(item, source, user, info.field_types, info.field_lengths)
    feature: dict[str, Any] = {"attributes": attributes, "geometry": item.esri_geometry()}
    saved = replace(item, owner=user.strip())
    if item.oid is None:
        # Saved already (an answer lost on the way, then 保存 again): updated, not added twice.
        there = client.find(source.url, source.fields["id"], [item.id], token, out_fields=info.object_id_field + "," + source.fields["id"])
        if item.id in there and is_number(there[item.id].get(info.object_id_field)):
            return save_interest(client, settings, replace(item, oid=int(there[item.id][info.object_id_field])), user, token)
        result = client.apply_edits(source.url, adds=[feature], token=token)
        r = (result.add_results or [{}])[0]
        if not r.get("success"):
            raise EsriError("追加失敗（" + ((r.get("error") or {}).get("description") or "理由不明") + "）")
        return replace(saved, oid=int(r["objectId"]) if is_number(r.get("objectId")) else None, updated_at=edit_stamp.now_ms())
    attributes[info.object_id_field] = item.oid
    result = client.apply_edits(source.url, updates=[feature], token=token)
    r = (result.update_results or [{}])[0]
    if not r.get("success"):
        raise EsriError("更新失敗（" + ((r.get("error") or {}).get("description") or "別の PC で削除済みか") + "）")
    return replace(saved, updated_at=edit_stamp.now_ms())


def delete_interests(client: EsriClient, settings: InterestSettings, items: Sequence[Interest],
                     tokens: Optional[dict[str, Optional[str]]] = None) -> tuple[list[str], list[str]]:
    """Deletes the items; the ids deleted and why the others were not."""
    done: list[str] = []
    failed: list[str] = []
    for kind in KINDS:
        mine = [i for i in items if i.kind == kind and i.oid is not None]
        source = settings.source(kind)
        if not mine or source is None:
            continue
        token = (tokens or {}).get(kind) or source.token
        try:
            results = client.delete_features(source.url, [i.oid for i in mine], token)
        except EsriError as error:
            failed.extend(f"{i.name}: {error}" for i in mine)
            continue
        by_oid = {r.get("objectId"): r for r in results if isinstance(r, dict)}
        for i in mine:
            r = by_oid.get(i.oid) or {"success": all(x.get("success") for x in results if isinstance(x, dict))}
            if r.get("success"):
                done.append(i.id)
            else:
                failed.append(f"{i.name}: " + ((r.get("error") or {}).get("description") or "削除失敗"))
    failed.extend(f"{i.name}: 未保存" for i in items if i.oid is None)
    return done, failed


def matches(item: Interest, text: str) -> bool:
    """The dock's 絞り込み: the text in the name or the note (case aside)."""
    t = text.strip().casefold()
    return not t or t in item.name.casefold() or t in item.note.casefold()


def section_lines(items: Iterable[Interest]) -> list[tuple[str, str]]:
    """A card's lines: each item's name and colour, areas and points by name (one line each, no ×n: each is its own)."""
    return [(i.name, i.color) for i in sorted(items, key=lambda i: (i.name.casefold(), i.kind))]
