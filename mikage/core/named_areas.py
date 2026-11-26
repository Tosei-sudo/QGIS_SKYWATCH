"""
組織の関心地域 (tsudo, 2026-11-24: 「個人のAOIはあるんだけど、組織のAOIを整理
したいので、ネームドエリアを拡張する感じで、属性を横展開したい」): the named
area layer (TBL_NAMED_AREA: the 地名 of 通報 and 観測記録) also holds the
organisation's areas of interest, with more attributes. No new layer: one row
per area, its 用途 (AREA_USE) saying what it is for: 地名 (a place name, also
when empty: the rows from before), 関心地域 (an area of interest only: never
the place of a record or a report) or 両方 (both).

The attributes (roles of DEFAULT_PLACE_FIELDS, names changeable in the
settings file): 名前 NAME, よみがな NAME_KANA, 別名 NAME_ALIAS (several, by
commas), 管理番号 NAMED_AREA_ID (required and unique: the link of requests
and records), 種類 KIND (the level, as 都道府県 and 市町村: tsudo 11:06Z), 国
COUNTRY_CODE (TBL_COUNTRY's code), カテゴリー CATEGORY_ID (TBL_AREA_CATEGORY),
上位の地域 PARENT_ID (the 管理番号 of the area it is part of), 担当グループ
OWNER_GROUP (a 依頼グループ), 重要度 PRIORITY (1–1000, 500 when empty, as
requests), 色 COLOR (the category's when empty), メモ NOTE, 有効 ACTIVE and
the English NAME_EN and KIND_EN.

Everyone may edit the areas and the categories (tsudo 11:20Z: no role).

管理番号 is given when a row has none (tsudo 11:52Z: every row, NA-000123):
by the geodatabase's attribute rule on the Oracle sequence SEQ_NAMED_AREA_ID
(tools/arcgis/skywatch_named_area_ids.py, docs/arcgis-setup.md), else by
the plugin: the largest number of the prefix plus one, then checked again
after saving (two PCs saving at once: the later one is numbered again).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional, Sequence

from . import edit_stamp
from .esri import EsriClient, EsriError, fit_value, quote
from .obs.api import (
    AREA_USES, DEFAULT_COUNTRY_FIELDS, DEFAULT_PLACE_FIELDS, USE_BOTH, USE_INTEREST, USE_PLACE, area_use_of,
)
from .shapes import in_rings, ring_area
from .sources import EsriSource, esri_source_of
from .util import is_color, is_number, is_record, js_string, ms_of

__all__ = [
    "AREA_USES", "USE_BOTH", "USE_INTEREST", "USE_PLACE", "area_use_of",
    "AreaCategory", "NamedAreaSettings", "OrgArea", "AreaList",
]

#: The category table's attributes by role.
DEFAULT_CATEGORY_FIELDS = {
    "id": "CATEGORY_ID", "name": "CATEGORY_NAME", "nameEn": "CATEGORY_NAME_EN", "parent": "PARENT_ID",
    "order": "SORT_ORDER", "color": "COLOR", "active": "ACTIVE", "note": "NOTE",
}
#: 管理番号 as the plugin gives it: the prefix and the digits (NA-000123).
DEFAULT_ID_PREFIX = "NA-"
DEFAULT_ID_DIGITS = 6
#: The heading on the cards and the dock's tab.
DEFAULT_LABEL = "組織の関心地域"
#: An area's colour when neither it nor its category has one.
DEFAULT_COLOR = "#0ea5e9"
#: 重要度: as a request's (1 the highest … 1000; empty is 500).
PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT = 1, 1000, 500
#: More rows than this is an error (never cut silently).
LIMIT = 200_000
#: Shapes read at a time (by object ids).
SHAPE_BATCH = 200
#: A category's id: letters, digits, - and _ (PORT-NAVAL), as a code typed into other tables.
CATEGORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}")
#: 別名 are written with commas (or 、 ; and new lines).
_ALIAS_SPLIT = re.compile(r"[,、，;；\n]+")


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def _active(v: Any) -> bool:
    if v is None or v == "":
        return True
    if v is False or (is_number(v) and v == 0):
        return False
    return not re.fullmatch(r"(0|false|no|off|いいえ|無効|停止)", _text(v), re.IGNORECASE)


def aliases_of(value: Any) -> tuple[str, ...]:
    """別名 of a cell or a typed text: by commas (、 ; new lines too), each once, empty ones left out."""
    out: list[str] = []
    for part in _ALIAS_SPLIT.split(_text(value)):
        part = part.strip()
        if part and part.casefold() not in {o.casefold() for o in out}:
            out.append(part)
    return tuple(out)


def alias_text(aliases: Iterable[str]) -> str:
    return ", ".join(a for a in aliases if a)


def priority_of(value: Any) -> Optional[int]:
    """重要度 of a cell: a whole number 1–1000, None when empty or not one."""
    if not is_number(value) or isinstance(value, bool):
        try:
            value = float(_text(value))
        except ValueError:
            return None
    n = int(round(float(value)))
    return n if PRIORITY_MIN <= n <= PRIORITY_MAX else None


# ---- settings -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NamedAreaSettings:
    """
    Where the named areas are (`namedAreas` of the settings file, else the
    layer of 通報 or 観測記録の出力), the categories and the countries.
    """
    areas: EsriSource
    categories: Optional[EsriSource] = None
    countries: Optional[EsriSource] = None
    id_prefix: str = DEFAULT_ID_PREFIX
    id_digits: int = DEFAULT_ID_DIGITS
    label: str = DEFAULT_LABEL
    #: Whether the settings file has `namedAreas` itself (False: alertReports' or observationExports' layer).
    own: bool = True

    def id_text(self, number: int) -> str:
        return f"{self.id_prefix}{number:0{self.id_digits}d}"

    def id_number(self, text: str) -> Optional[int]:
        """The number of a 管理番号 of the plugin's form (NA-000123 → 123), None for another."""
        t = text.strip()
        if not t.upper().startswith(self.id_prefix.upper()):
            return None
        rest = t[len(self.id_prefix):]
        return int(rest) if rest.isdigit() else None


_KEYS = ("url", "fields", "where", "categories", "countries", "idPrefix", "idDigits", "label")


def named_area_settings_of(value: Any, problems: list[str]) -> Optional[NamedAreaSettings]:
    """
    `namedAreas`: the named area layer's URL, or `{ "url", "fields", "where",
    "categories", "countries", "idPrefix", "idDigits", "label" }`
    (`categories` the TBL_AREA_CATEGORY table, `countries` TBL_COUNTRY);
    None when it cannot be used.
    """
    at = "namedAreas"
    if isinstance(value, str):
        value = {"url": value}
    if not is_record(value):
        problems.append(f'{at} は地名のレイヤーの URL か {{ "url", "categories", "countries" }} で指定')
        return None
    for k in value:
        if k not in _KEYS and k != "token":  # (a token: said once for the whole file, config.token_problem)
            problems.append(f"{at}.{k} は使えないキー（{'・'.join(_KEYS)}）")
    areas = esri_source_of({k: v for k, v in value.items() if k in ("url", "fields", "where")}, at,
                           DEFAULT_PLACE_FIELDS, problems)
    if areas is None:
        return None
    categories = countries = None
    if value.get("categories") not in (None, False, ""):
        categories = esri_source_of(value["categories"], f"{at}.categories", DEFAULT_CATEGORY_FIELDS, problems)
    if value.get("countries") not in (None, False, ""):
        countries = esri_source_of(value["countries"], f"{at}.countries", DEFAULT_COUNTRY_FIELDS, problems)
    prefix, digits = value.get("idPrefix", DEFAULT_ID_PREFIX), value.get("idDigits", DEFAULT_ID_DIGITS)
    if not (isinstance(prefix, str) and re.fullmatch(r"[A-Za-z0-9_\-]{0,16}", prefix)):
        problems.append(f"{at}.idPrefix は英数字と - _ の 16 文字まで（NA- など）")
        prefix = DEFAULT_ID_PREFIX
    if not (isinstance(digits, int) and not isinstance(digits, bool) and 1 <= digits <= 12):
        problems.append(f"{at}.idDigits は 1〜12 の整数（番号の桁数）")
        digits = DEFAULT_ID_DIGITS
    label = value.get("label")
    if label is not None and not (isinstance(label, str) and label.strip()):
        problems.append(f"{at}.label は文字列で指定（カードの見出し）")
    return NamedAreaSettings(areas=areas, categories=categories, countries=countries, id_prefix=prefix, id_digits=digits,
                             label=label.strip() if isinstance(label, str) and label.strip() else DEFAULT_LABEL)


def settings_for(config: Any) -> Optional[NamedAreaSettings]:
    """
    The settings used: `namedAreas` of the file, else the named area layer
    (and countries) of `alertReports`, else of `observationExports`.
    """
    own = getattr(config, "named_areas", None)
    alerts = getattr(config, "alert_reports", None)
    exports = getattr(config, "observation_exports", None)
    countries = getattr(alerts, "countries", None)
    if own is not None:
        return own if own.countries is not None or countries is None else replace(own, countries=countries)
    source = getattr(alerts, "named_areas", None) or getattr(exports, "named_areas", None)
    if source is None:
        return None
    return NamedAreaSettings(areas=source, countries=countries, own=False)


# ---- areas ------------------------------------------------------------------------------------------


@dataclass
class OrgArea:
    """One row of the named area layer (`rings`: WGS 84, None while not read)."""
    oid: Optional[int] = None
    id: str = ""
    name: str = ""
    use: str = USE_INTEREST
    kana: str = ""
    aliases: tuple[str, ...] = ()
    kind: str = ""
    country: str = ""
    category: str = ""
    parent: str = ""
    owner_group: str = ""
    priority: Optional[int] = None
    color: str = ""
    note: str = ""
    name_en: str = ""
    kind_en: str = ""
    active: bool = True
    rings: Optional[list] = None
    updated_at: Optional[int] = None
    updated_by: str = ""

    @property
    def is_interest(self) -> bool:
        """An organisation's area of interest (関心地域 or 両方)."""
        return self.use in (USE_INTEREST, USE_BOTH)

    @property
    def is_place(self) -> bool:
        return self.use in (USE_PLACE, USE_BOTH)

    @property
    def has_shape(self) -> bool:
        return bool(self.rings and any(len(r) >= 3 for r in self.rings))

    @property
    def key(self) -> str:
        """What the plugin tells areas apart by: the 管理番号, else the object id (a row not numbered yet)."""
        return self.id or (f"#{self.oid}" if self.oid is not None else "")

    def names(self) -> list[str]:
        """Every name it may be looked for by (名前・よみがな・別名・英語名), each once."""
        out: list[str] = []
        for n in (self.name, self.kana, *self.aliases, self.name_en):
            if n and n.casefold() not in {o.casefold() for o in out}:
                out.append(n)
        return out

    def matches(self, text: str) -> bool:
        """The dock's 絞り込み: a word in a name, the よみがな, an alias, the 管理番号 or the note (case aside)."""
        t = text.strip().casefold()
        if not t:
            return True
        return any(t in n.casefold() for n in (*self.names(), self.id, self.note))

    def shape_text(self) -> str:
        if self.rings is None:
            return "形は未取得" if self.oid is not None else "形なし"
        if not self.has_shape:
            return "形なし"
        corners = sum(max(0, len(r) - (1 if len(r) > 1 and r[0] == r[-1] else 0)) for r in self.rings)
        parts = len(self.rings)
        return f"頂点 {corners}" if parts == 1 else f"{parts} 輪・頂点 {corners}"

    def esri_geometry(self) -> Optional[dict[str, Any]]:
        from .shapes import esri_rings

        if not self.has_shape:
            return None
        return {"rings": esri_rings(self.rings or []), "spatialReference": {"wkid": 4326}}


def area_of(attributes: dict, fields: dict[str, str], oid_field: str = "OBJECTID", rings: Optional[list] = None) -> OrgArea:
    """A row of the layer as an OrgArea (`rings` given separately: the list is read without shapes)."""
    a = attributes

    def text(role: str) -> str:
        return _text(a.get(fields[role])) if role in fields else ""

    tracking = edit_stamp.tracking().fields
    oid = a.get(oid_field)
    if not is_number(oid):
        oid = next((v for k, v in a.items() if str(k).upper() in ("OBJECTID", "FID", "OID") and is_number(v)), None)
    color = text("color")
    return OrgArea(
        oid=int(oid) if is_number(oid) else None, id=text("id"), name=text("name"), use=area_use_of(a.get(fields.get("use", ""))),
        kana=text("kana"), aliases=aliases_of(a.get(fields.get("alias", ""))), kind=text("kind"), country=text("country"),
        category=text("category"), parent=text("parent"), owner_group=text("ownerGroup"),
        priority=priority_of(a.get(fields.get("priority", ""))), color=color if is_color(color) else "", note=text("note"),
        name_en=text("nameEn"), kind_en=text("kindEn"),
        active=_active(a.get(fields["active"])) if "active" in fields and fields["active"] in a else True,
        rings=rings, updated_at=ms_of(a.get(tracking.get("updatedAt", ""))) or ms_of(a.get(tracking.get("createdAt", ""))),
        updated_by=_text(a.get(tracking.get("updatedBy", ""))) or _text(a.get(tracking.get("createdBy", ""))),
    )


@dataclass
class AreaList:
    """What was read: the rows (shapes of the areas of interest), the layer's attributes and the categories and countries."""
    areas: list[OrgArea]
    #: The attribute names the layer has (a role whose attribute is missing is not shown in the form nor written).
    fields: set[str]
    oid_field: str = "OBJECTID"
    categories: Optional[list["AreaCategory"]] = None
    #: Why the categories could not be read ("" when they were, or there is no table).
    category_failure: str = ""
    #: The countries to choose: (code, name), by code.
    countries: list[tuple[str, str]] = field(default_factory=list)
    #: Whether the layer takes edits (a FeatureServer).
    editable: bool = True

    def missing(self, fields: dict[str, str], roles: Sequence[str]) -> list[str]:
        """The attribute names of `roles` the layer lacks."""
        return [fields[r] for r in roles if r in fields and fields[r] not in self.fields]


#: The roles of the new attributes (2026-11-24): without them the layer is read but the form shows less.
ORG_ROLES = ("use", "kana", "alias", "country", "category", "parent", "ownerGroup", "priority", "color", "note")


def _layer(client: EsriClient, source: EsriSource, token: Optional[str]):
    info = client.layer(source.url, token, refresh=True)
    if info.geometry_type != "esriGeometryPolygon":
        raise EsriError(f"地名のレイヤーがポリゴンのレイヤーでない（管理者に連絡: {info.geometry_type or '形状なし'}）", code="E705")
    if source.fields["name"] not in info.field_types:
        raise EsriError(f"地名のレイヤーに必要な項目がない（管理者に連絡: {source.fields['name']}）", code="E704")
    return info


def read_shapes(client: EsriClient, source: EsriSource, oids: Sequence[int], token: Optional[str] = None,
                oid_field: str = "OBJECTID", cancelled: Optional[Callable[[], bool]] = None) -> dict[int, list]:
    """The rings (WGS 84) of the rows `oids`, by object id."""
    out: dict[int, list] = {}
    wanted = list(dict.fromkeys(int(o) for o in oids))
    for i in range(0, len(wanted), SHAPE_BATCH):
        if cancelled and cancelled():
            break
        chunk = wanted[i:i + SHAPE_BATCH]
        rows = client.query_all(source.url, f"{oid_field} IN ({','.join(str(o) for o in chunk)})", token,
                                out_fields=oid_field, geometry=True, limit=len(chunk) + 10)
        for r in rows:
            oid = (r.get("attributes") or {}).get(oid_field)
            rings = (r.get("geometry") or {}).get("rings")
            if is_number(oid) and isinstance(rings, list):
                out[int(oid)] = rings
    return out


def size_of(rings: Optional[list]) -> float:
    """An area's size to put areas in order by (degrees², the holes taken off; 0 without a shape)."""
    rings = [r for r in (rings or []) if len(r) >= 3]
    total = 0.0
    for r in rings:
        a = abs(ring_area(r))
        x, y = r[0][0], r[0][1]
        # A ring inside an odd number of others is a hole.
        holes = sum(1 for other in rings if other is not r and in_rings([other], x, y))
        total += -a if holes % 2 else a
    return max(total, 0.0)


def areas_at(client: EsriClient, settings: NamedAreaSettings, lon: float, lat: float, token: Optional[str] = None) -> list[OrgArea]:
    """
    The active rows (any 用途) whose shape holds a point, with their shapes,
    the smallest first (「この地域で撮像依頼…」 on the map).
    """
    source = settings.areas
    info = _layer(client, source, token)
    extra = {"geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}), "geometryType": "esriGeometryPoint", "inSR": "4326",
             "spatialRel": "esriSpatialRelIntersects"}
    rows = client.query_all(source.url, source.where or "1=1", token, extra=extra, limit=1000, cut=True)
    out = []
    for r in rows:
        rings = (r.get("geometry") or {}).get("rings")
        if not isinstance(rings, list) or not in_rings(rings, lon, lat):
            continue
        area = area_of(r.get("attributes") or {}, source.fields, info.object_id_field, rings)
        if area.active:
            out.append(area)
    return sorted(out, key=lambda a: (size_of(a.rings), area_sort_key(a)))


def areas_by_id(client: EsriClient, settings: NamedAreaSettings, ids: Iterable[str], token: Optional[str] = None,
                cancelled: Optional[Callable[[], bool]] = None) -> dict[str, list[OrgArea]]:
    """
    The rows of each 管理番号 (upper case → the rows, with their shapes; an id
    with no row is left out, one with two rows has both): the areas the
    requests linked to them follow (地域の形に合わせる, core/request/area_follow.py).
    """
    source = settings.areas
    wanted = sorted({i.strip().upper() for i in ids if i and i.strip()})
    out: dict[str, list[OrgArea]] = {}
    if not wanted:
        return out
    info = _layer(client, source, token)
    f = source.fields
    for k in range(0, len(wanted), SHAPE_BATCH):
        if cancelled and cancelled():
            break
        chunk = wanted[k:k + SHAPE_BATCH]
        where = f"UPPER({f['id']}) IN ({','.join(quote(i) for i in chunk)})"
        if source.where:
            where = f"({source.where}) AND {where}"
        for r in client.query_all(source.url, where, token, geometry=True, limit=len(chunk) * 4 + 10, cancelled=cancelled):
            rings = (r.get("geometry") or {}).get("rings")
            area = area_of(r.get("attributes") or {}, f, info.object_id_field, rings if isinstance(rings, list) else [])
            if area.id:
                out.setdefault(area.id.strip().upper(), []).append(area)
    return out


def layer_stamp(client: EsriClient, settings: NamedAreaSettings, token: Optional[str] = None) -> Optional[float]:
    """When the named area layer says its data last changed (ms; None when it does not say)."""
    from .analysis_cache import stamp_of

    # As the server says it now (not a minute old: core/esri.py LayerInfos).
    info = client.layer(settings.areas.url, token, now=True)
    return stamp_of(info.raw)


def load_areas(client: EsriClient, settings: NamedAreaSettings, tokens: Optional[dict[str, Optional[str]]] = None,
               cancelled: Optional[Callable[[], bool]] = None) -> AreaList:
    """
    Every row of the named area layer (its attributes: a city's border is
    not read for the list), the shapes of the areas of interest (the cards
    and the map need them), the categories and the countries' names.
    """
    tokens = tokens or {}
    source = settings.areas
    token = tokens.get("areas")
    info = _layer(client, source, token)
    oid = info.object_id_field
    rows = client.query_all(source.url, source.where or "1=1", token, geometry=False, limit=LIMIT, cancelled=cancelled)
    areas = [area_of(r.get("attributes") or {}, source.fields, oid) for r in rows]
    shapes = read_shapes(client, source, [a.oid for a in areas if a.is_interest and a.oid is not None], token, oid, cancelled)
    for a in areas:
        if a.is_interest and a.oid is not None:
            a.rings = shapes.get(a.oid, [])
    areas.sort(key=area_sort_key)
    out = AreaList(areas=areas, fields=set(info.field_types), oid_field=oid,
                   editable=bool(re.search(r"/FeatureServer/\d+/?$", source.url, re.IGNORECASE)))
    if settings.categories is not None:
        try:
            out.categories = load_categories(client, settings.categories, tokens.get("categories"), cancelled)
        except EsriError as error:
            out.categories, out.category_failure = None, str(error)
    if settings.countries is not None:
        try:
            out.countries = load_country_names(client, settings.countries, tokens.get("countries"))
        except EsriError:
            out.countries = []
    return out


def area_sort_key(a: OrgArea) -> tuple:
    return ((a.kana or a.name).casefold(), a.name.casefold(), a.id)


def load_country_names(client: EsriClient, source: EsriSource, token: Optional[str] = None) -> list[tuple[str, str]]:
    """The countries' codes and names (no borders), by code."""
    f = source.fields
    rows = client.query_all(source.url, source.where or "1=1", token, geometry=False, limit=10_000,
                            out_fields=",".join(dict.fromkeys(v for k, v in f.items() if k in ("code", "name", "active"))))
    out: dict[str, str] = {}
    for r in rows:
        a = r.get("attributes") or {}
        code = _text(a.get(f.get("code", "")))
        if code and ("active" not in f or f["active"] not in a or _active(a.get(f["active"]))):
            out.setdefault(code, _text(a.get(f.get("name", ""))) or code)
    return sorted(out.items(), key=lambda kv: kv[0])


def check_area(area: OrgArea, others: Iterable[OrgArea], settings: Optional[NamedAreaSettings] = None) -> list[str]:
    """Why the area cannot be saved as it is ([] when it can): a name, a shape, a 管理番号 no other row has…"""
    problems = []
    if not area.name.strip():
        problems.append("名前が必要")
    if area.oid is None and not area.has_shape:
        problems.append("形が必要（地図に描くか、選んだ地物から）")
    if area.id and not re.fullmatch(r"[^'\"\s]{1,255}", area.id):
        problems.append("管理番号に空白や引用符は使えない")
    if area.id:
        same = [o for o in others if o.id.casefold() == area.id.casefold() and (area.oid is None or o.oid != area.oid)]
        if same:
            problems.append(f"管理番号「{same[0].id}」は「{same[0].name}」が使用中")
    if area.parent and area.id and area.parent.casefold() == area.id.casefold():
        problems.append("上位の地域に自分自身は選べない")
    if area.priority is not None and not PRIORITY_MIN <= area.priority <= PRIORITY_MAX:
        problems.append(f"重要度は {PRIORITY_MIN}〜{PRIORITY_MAX}")
    return problems


def _values(area: OrgArea, source: EsriSource, types: dict[str, str], lengths: dict[str, int]) -> dict[str, Any]:
    f = source.fields
    values = {
        "name": area.name.strip(), "id": area.id.strip() or None, "use": area.use, "kana": area.kana.strip() or None,
        "alias": alias_text(area.aliases) or None, "kind": area.kind.strip() or None, "country": area.country.strip() or None,
        "category": area.category.strip() or None, "parent": area.parent.strip() or None,
        "ownerGroup": area.owner_group.strip() or None, "priority": area.priority, "color": area.color or None,
        "note": area.note.strip() or None, "nameEn": area.name_en.strip() or None, "kindEn": area.kind_en.strip() or None,
        "active": 1 if area.active else 0,
    }
    out = {}
    for role, v in values.items():
        name = f.get(role)
        if not name or name not in types:
            continue
        if isinstance(v, str) and lengths.get(name):
            v = v[:lengths[name]]
        out[name] = fit_value(v, types[name])
    return out


def ids_in_use(client: EsriClient, source: EsriSource, token: Optional[str] = None) -> list[tuple[int, str]]:
    """Every row's (object id, 管理番号), whatever the settings' `where` (an id is unique in the whole layer)."""
    info = client.layer(source.url, token)
    oid, name = info.object_id_field, source.fields["id"]
    rows = client.query_all(source.url, "1=1", token, out_fields=f"{oid},{name}", geometry=False, limit=LIMIT)
    out = []
    for r in rows:
        a = r.get("attributes") or {}
        if is_number(a.get(oid)):
            out.append((int(a[oid]), _text(a.get(name))))
    return out


def same_id(client: EsriClient, source: EsriSource, area_id: str, token: Optional[str] = None) -> list[OrgArea]:
    """The rows whose 管理番号 is `area_id` (case aside), whatever the settings' `where`."""
    info = client.layer(source.url, token)
    f = source.fields
    rows = client.query_all(source.url, f"UPPER({f['id']}) = {quote(area_id.strip().upper())}", token,
                            out_fields=",".join(dict.fromkeys((info.object_id_field, f["id"], f["name"]))), geometry=False, limit=100)
    return [area_of(r.get("attributes") or {}, f, info.object_id_field) for r in rows]


def next_number(settings: NamedAreaSettings, ids: Iterable[str]) -> int:
    """One past the largest number of the plugin's form (NA-000123) in use; 1 for the first."""
    numbers = [n for n in (settings.id_number(i) for i in ids) if n is not None]
    return max(numbers, default=0) + 1


def save_area(client: EsriClient, settings: NamedAreaSettings, area: OrgArea, token: Optional[str] = None,
              shape_changed: bool = True) -> OrgArea:
    """
    Adds the area (no oid yet) or updates it (its shape too when
    `shape_changed`); the area as saved, with its 管理番号 (the geodatabase's
    rule's, else one the plugin gave: see `number_area`).
    """
    source = settings.areas
    if "id" not in source.fields:
        raise EsriError("地名のレイヤーの管理番号の役割（id）がない（管理者に連絡: namedAreas.fields.id）", code="E704")
    info = _layer(client, source, token)
    types, oid_field = info.field_types, info.object_id_field
    problems = check_area(area, same_id(client, source, area.id, token) if area.id.strip() else [], settings)
    if problems:
        raise ValueError("、".join(problems))
    attributes = _values(area, source, types, info.field_lengths)
    feature: dict[str, Any] = {"attributes": attributes}
    if area.oid is None or shape_changed:
        if not area.has_shape:
            raise ValueError("形が必要（地図に描くか、選んだ地物から）")
        feature["geometry"] = area.esri_geometry()
    if area.oid is None:
        result = client.apply_edits(source.url, adds=[feature], token=token)
        r = (result.add_results or [{}])[0]
        if not r.get("success"):
            raise EsriError(code="E305", detail="追加: " + ((r.get("error") or {}).get("description") or ""))
        saved = replace(area, oid=int(r["objectId"]) if is_number(r.get("objectId")) else None, updated_at=edit_stamp.now_ms(),
                        updated_by=edit_stamp.user_name())
    else:
        attributes[oid_field] = area.oid
        result = client.apply_edits(source.url, updates=[feature], token=token)
        r = (result.update_results or [{}])[0]
        if not r.get("success"):
            raise EsriError(code="E305", detail="更新: " + ((r.get("error") or {}).get("description") or "別の PC で削除済みか"))
        saved = replace(area, updated_at=edit_stamp.now_ms(), updated_by=edit_stamp.user_name())
    if not saved.id and saved.oid is not None and source.fields["id"] in types:
        saved = number_area(client, settings, saved, token)
    return saved


def _id_of(client: EsriClient, source: EsriSource, oid: int, token: Optional[str]) -> str:
    info = client.layer(source.url, token)
    rows = client.query_all(source.url, f"{info.object_id_field} = {int(oid)}", token,
                            out_fields=f"{info.object_id_field},{source.fields['id']}", geometry=False, limit=10)
    return _text((rows[0].get("attributes") or {}).get(source.fields["id"])) if rows else ""


def number_area(client: EsriClient, settings: NamedAreaSettings, area: OrgArea, token: Optional[str] = None,
                tries: int = 5) -> OrgArea:
    """
    Gives a saved row without a 管理番号 one: the one the geodatabase's
    attribute rule wrote when it has one, else the largest of the plugin's
    form plus one, read again after writing it (another PC numbering at the
    same moment: the row with the larger object id takes the next number).
    """
    source = settings.areas
    if area.oid is None:
        return area
    there = _id_of(client, source, area.oid, token)
    if there:
        return replace(area, id=there)
    info = client.layer(source.url, token)
    name, oid_field = source.fields["id"], info.object_id_field
    for _ in range(tries):
        taken = ids_in_use(client, source, token)
        new = settings.id_text(next_number(settings, (i for _o, i in taken)))
        result = client.apply_edits(source.url, updates=[{"attributes": {oid_field: area.oid, name: new}}], token=token)
        r = (result.update_results or [{}])[0]
        if not r.get("success"):
            raise EsriError(code="E305", detail="管理番号: " + ((r.get("error") or {}).get("description") or ""))
        same = sorted(o for o, i in ids_in_use(client, source, token) if i.casefold() == new.casefold())
        if not same or same[0] == area.oid:
            return replace(area, id=new)
    raise EsriError(code="E305", detail="管理番号を付けられない（ほかの PC と同時に付番が続いた）。少し待って保存し直す")


def number_blanks(client: EsriClient, settings: NamedAreaSettings, token: Optional[str] = None,
                  cancelled: Optional[Callable[[], bool]] = None) -> tuple[int, list[str]]:
    """
    Gives every row without a 管理番号 one, in object id order (tsudo
    11:52Z: 「全てに付番すること」); how many, and why some were not.
    """
    source = settings.areas
    info = _layer(client, source, token)
    if source.fields["id"] not in info.field_types:
        raise EsriError(f"地名のレイヤーに管理番号の項目がない（管理者に連絡: {source.fields['id']}）", code="E704")
    taken = ids_in_use(client, source, token)
    blank = sorted(o for o, i in taken if not i)
    number = next_number(settings, (i for _o, i in taken))
    done, problems = 0, []
    oid_field, name = info.object_id_field, source.fields["id"]
    for k in range(0, len(blank), 100):
        if cancelled and cancelled():
            break
        chunk = blank[k:k + 100]
        updates = [{"attributes": {oid_field: oid, name: settings.id_text(number + j)}} for j, oid in enumerate(chunk)]
        number += len(chunk)
        result = client.apply_edits(source.url, updates=updates, token=token)
        for r in result.update_results:
            if r.get("success"):
                done += 1
            else:
                problems.append(f"#{r.get('objectId')}: " + ((r.get("error") or {}).get("description") or "更新失敗"))
    dup = duplicate_ids(ids_in_use(client, source, token))
    if dup:
        problems.append("管理番号が重なっている: " + "、".join(dup[:5]) + ("…" if len(dup) > 5 else ""))
    return done, problems


def duplicate_ids(pairs: Iterable[tuple[int, str]]) -> list[str]:
    """The 管理番号 that more than one row has (case aside)."""
    seen: dict[str, int] = {}
    for _o, i in pairs:
        if i:
            seen[i.casefold()] = seen.get(i.casefold(), 0) + 1
    shown: dict[str, str] = {}
    for _o, i in pairs:
        if i and seen[i.casefold()] > 1:
            shown.setdefault(i.casefold(), i)
    return sorted(shown.values())


def blank_count(areas: Iterable[OrgArea]) -> int:
    return sum(1 for a in areas if not a.id)


def area_color(area: OrgArea, categories: Optional[Sequence["AreaCategory"]] = None, default: str = DEFAULT_COLOR) -> str:
    """The area's colour: its own, else its category's (or that category's parent's), else `default`."""
    if area.color:
        return area.color
    by = {c.id.casefold(): c for c in categories or []}
    seen = set()
    c = by.get(area.category.casefold()) if area.category else None
    while c is not None and c.id.casefold() not in seen:
        if c.color:
            return c.color
        seen.add(c.id.casefold())
        c = by.get(c.parent.casefold()) if c.parent else None
    return default


def children_of(areas: Sequence[OrgArea]) -> dict[str, list[OrgArea]]:
    """The areas under each 管理番号 (PARENT_ID), casefolded keys; the tops under ""."""
    ids = {a.id.casefold() for a in areas if a.id}
    out: dict[str, list[OrgArea]] = {}
    for a in areas:
        p = a.parent.casefold() if a.parent and a.parent.casefold() in ids and a.parent.casefold() != a.id.casefold() else ""
        out.setdefault(p, []).append(a)
    return out


def section_lines(areas: Iterable[OrgArea], categories: Optional[Sequence["AreaCategory"]] = None,
                  default: str = DEFAULT_COLOR) -> list[tuple[str, str]]:
    """A card's lines: each area's name (and category) and colour, by name."""
    names = category_names(categories or [])
    out = []
    for a in sorted(areas, key=area_sort_key):
        c = names.get(a.category.casefold(), "") if a.category else ""
        out.append((f"{a.name}（{c}）" if c else a.name, area_color(a, categories, default)))
    return out


# ---- categories -------------------------------------------------------------------------------------


@dataclass
class AreaCategory:
    """One row of TBL_AREA_CATEGORY."""
    id: str
    name: str
    name_en: str = ""
    parent: str = ""
    order: Optional[float] = None
    color: str = ""
    active: bool = True
    note: str = ""
    oid: Optional[int] = None


def category_of(a: dict, fields: dict[str, str], oid_field: str = "OBJECTID") -> Optional[AreaCategory]:
    def text(role: str) -> str:
        return _text(a.get(fields[role])) if role in fields else ""

    cid = text("id")
    if not cid:
        return None
    order = a.get(fields.get("order", ""))
    color = text("color")
    oid = a.get(oid_field)
    return AreaCategory(id=cid, name=text("name") or cid, name_en=text("nameEn"), parent=text("parent"),
                        order=float(order) if is_number(order) else None, color=color if is_color(color) else "",
                        active=_active(a.get(fields["active"])) if "active" in fields and fields["active"] in a else True,
                        note=text("note"), oid=int(oid) if is_number(oid) else None)


def category_sort_key(c: AreaCategory) -> tuple:
    return (c.order is None, c.order or 0.0, c.name.casefold(), c.id.casefold())


def load_categories(client: EsriClient, source: EsriSource, token: Optional[str] = None,
                    cancelled: Optional[Callable[[], bool]] = None) -> list[AreaCategory]:
    info = client.layer(source.url, token, refresh=True)
    if source.fields["id"] not in info.field_types:
        raise EsriError(f"カテゴリーの表に必要な項目がない（管理者に連絡: {source.fields['id']}）", code="E704")
    rows = client.query_all(source.url, source.where or "1=1", token, geometry=False, limit=10_000, cancelled=cancelled)
    out = [c for c in (category_of(r.get("attributes") or {}, source.fields, info.object_id_field) for r in rows) if c is not None]
    return sorted(out, key=category_sort_key)


def category_names(categories: Iterable[AreaCategory]) -> dict[str, str]:
    """Each category's name by its id (casefolded)."""
    return {c.id.casefold(): c.name for c in categories}


def category_tree(categories: Sequence[AreaCategory]) -> list[tuple[int, AreaCategory]]:
    """The categories in tree order with their depth (0 the tops); a parent not there (or a loop) puts one at the top."""
    by = {c.id.casefold(): c for c in categories}
    kids: dict[str, list[AreaCategory]] = {}
    for c in categories:
        p = c.parent.casefold() if c.parent and c.parent.casefold() in by and c.parent.casefold() != c.id.casefold() else ""
        kids.setdefault(p, []).append(c)
    out: list[tuple[int, AreaCategory]] = []
    seen: set[str] = set()

    def walk(key: str, depth: int) -> None:
        for c in sorted(kids.get(key, []), key=category_sort_key):
            k = c.id.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append((depth, c))
            walk(k, depth + 1)

    walk("", 0)
    for c in sorted(categories, key=category_sort_key):  # in a loop: shown at the top
        if c.id.casefold() not in seen:
            seen.add(c.id.casefold())
            out.append((0, c))
            walk(c.id.casefold(), 1)
    return out


def descendants(categories: Sequence[AreaCategory], cid: str) -> set[str]:
    """The ids (casefolded) of the categories under `cid` (not itself)."""
    out: set[str] = set()
    todo = [cid.casefold()]
    while todo:
        k = todo.pop()
        for c in categories:
            if c.parent and c.parent.casefold() == k and c.id.casefold() not in out and c.id.casefold() != cid.casefold():
                out.add(c.id.casefold())
                todo.append(c.id.casefold())
    return out


def check_category(c: AreaCategory, categories: Sequence[AreaCategory], original: Optional[AreaCategory] = None) -> list[str]:
    problems = []
    if not CATEGORY_ID.fullmatch(c.id.strip()):
        problems.append("ID は英数字と - _ の 64 文字まで（PORT-NAVAL など）")
    if not c.name.strip():
        problems.append("名前が必要")
    others = [o for o in categories if original is None or o.id.casefold() != original.id.casefold()]
    if any(o.id.casefold() == c.id.strip().casefold() for o in others):
        problems.append(f"ID「{c.id.strip()}」は使用中")
    if c.parent:
        if c.parent.casefold() == c.id.strip().casefold():
            problems.append("上位に自分自身は選べない")
        elif original is not None and c.parent.casefold() in descendants(categories, original.id):
            problems.append("上位に自分の下のカテゴリーは選べない")
    if c.color and not is_color(c.color):
        problems.append("色は #rrggbb で指定")
    return problems


def save_category(client: EsriClient, source: EsriSource, c: AreaCategory, token: Optional[str] = None,
                  original: Optional[AreaCategory] = None) -> AreaCategory:
    """Adds or updates the category (by its object id, else its id); the category as saved."""
    info = client.layer(source.url, token, refresh=True)
    types, lengths, f = info.field_types, info.field_lengths, source.fields
    values = {"id": c.id.strip(), "name": c.name.strip(), "nameEn": c.name_en.strip() or None, "parent": c.parent.strip() or None,
              "order": c.order, "color": c.color or None, "active": 1 if c.active else 0, "note": c.note.strip() or None}
    attributes = {}
    for role, v in values.items():
        name = f.get(role)
        if name and name in types:
            if isinstance(v, str) and lengths.get(name):
                v = v[:lengths[name]]
            attributes[name] = fit_value(v, types[name])
    oid = c.oid if c.oid is not None else (original.oid if original is not None else None)
    if oid is None:
        there = client.find(source.url, f["id"], [c.id.strip()], token, out_fields=f"{info.object_id_field},{f['id']}")
        hit = there.get(c.id.strip())
        if hit is not None and is_number(hit.get(info.object_id_field)):
            oid = int(hit[info.object_id_field])
    if oid is None:
        result = client.apply_edits(source.url, adds=[{"attributes": attributes}], token=token)
        r = (result.add_results or [{}])[0]
        if not r.get("success"):
            raise EsriError(code="E305", detail="追加: " + ((r.get("error") or {}).get("description") or ""))
        return replace(c, oid=int(r["objectId"]) if is_number(r.get("objectId")) else None)
    attributes[info.object_id_field] = oid
    result = client.apply_edits(source.url, updates=[{"attributes": attributes}], token=token)
    r = (result.update_results or [{}])[0]
    if not r.get("success"):
        raise EsriError(code="E305", detail="更新: " + ((r.get("error") or {}).get("description") or ""))
    return replace(c, oid=oid)


def rename_category_in_areas(client: EsriClient, settings: NamedAreaSettings, old: str, new: str,
                             token: Optional[str] = None) -> int:
    """The areas of category `old` put in `new` (an id changed); how many."""
    source = settings.areas
    name = source.fields.get("category")
    info = client.layer(source.url, token)
    if not name or name not in info.field_types or old == new:
        return 0
    rows = client.query_all(source.url, f"{name} = {quote(old)}", token, out_fields=info.object_id_field, geometry=False, limit=LIMIT)
    oids = [(r.get("attributes") or {}).get(info.object_id_field) for r in rows]
    updates = [{"attributes": {info.object_id_field: int(o), name: new}} for o in oids if is_number(o)]
    if not updates:
        return 0
    result = client.apply_edits(source.url, updates=updates, token=token)
    return sum(1 for r in result.update_results if r.get("success"))


def category_counts(areas: Iterable[OrgArea]) -> dict[str, int]:
    """How many areas each category (casefolded id) has."""
    out: dict[str, int] = {}
    for a in areas:
        if a.category:
            out[a.category.casefold()] = out.get(a.category.casefold(), 0) + 1
    return out


def category_order_changes(categories: Sequence[AreaCategory], index: int, step: int) -> list[AreaCategory]:
    """
    The categories whose SORT_ORDER changes when the one at `index` (of
    those of the same parent, in their order) moves up (-1) or down (+1):
    the siblings numbered 1, 2, 3… in the new order.
    """
    if not 0 <= index < len(categories):
        return []
    moving = categories[index]
    key = moving.parent.casefold()
    siblings = sorted((c for c in categories if c.parent.casefold() == key), key=category_sort_key)
    at = next(i for i, c in enumerate(siblings) if c is moving)
    to = at + step
    if not 0 <= to < len(siblings):
        return []
    siblings[at], siblings[to] = siblings[to], siblings[at]
    return [replace(c, order=float(i + 1)) for i, c in enumerate(siblings) if c.order != float(i + 1)]


def delete_categories(client: EsriClient, source: EsriSource, categories: Sequence[AreaCategory],
                      counts: dict[str, int], token: Optional[str] = None) -> tuple[list[str], list[str]]:
    """Deletes the categories no area has (the others are refused); the ids deleted and why the others were not."""
    gone, problems = [], []
    free = [c for c in categories if not counts.get(c.id.casefold())]
    for c in categories:
        if counts.get(c.id.casefold()):
            problems.append(f"{c.name}: 地域が {counts[c.id.casefold()]} 件あるので削除できない（使わないなら「有効」を外す）")
    oids = [c.oid for c in free if c.oid is not None]
    if oids:
        results = client.delete_features(source.url, oids, token)
        by = {r.get("objectId"): r for r in results if isinstance(r, dict)}
        for c in free:
            r = by.get(c.oid) or {"success": all(x.get("success") for x in results if isinstance(x, dict))}
            if r.get("success"):
                gone.append(c.id)
            else:
                problems.append(f"{c.name}: " + ((r.get("error") or {}).get("description") or "削除失敗"))
    problems.extend(f"{c.name}: 未保存" for c in free if c.oid is None)
    return gone, problems
