"""
Observations (観測記録), as the viewer (observations.ts): one record per
thing per image — when, where, what thing (モノ) and what it was doing
(活動), or that it was no longer there (不在).

This module holds the record, the vocabulary (活動・確度・分類) and the
`observationStores` / `observationVocabulary` entries of the settings file.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..util import is_number, is_record, layer_url, words
from .areas import AreaStoreConfig, area_store_of
from .taxonomy import DEFAULT_SPEC_FIELDS, SpecField, Taxon, TaxonomyTableConfig, from_kinds, spec_fields_of, taxonomy_of, taxonomy_table_of


@dataclass
class Observation:
    """One observation of one thing in one image (times in ms since the epoch)."""
    id: str
    #: The image's catalog id, else its file name.
    image_id: str
    image_name: str
    #: When the image was taken, when known.
    image_time: Optional[int]
    #: The thing's id, the same in every image it is recorded in.
    object_id: str
    object_name: str
    #: 種別: the path of its class (`船舶 > 支援船 > 〇〇級`).
    kind: str
    #: 活動.
    activity: str
    #: False for 「不在」.
    present: bool
    note: str
    lon: float
    lat: float
    created_at: int
    updated_at: int
    deleted_at: Optional[int] = None
    #: m, front to rear, when measured by two points.
    length: Optional[float] = None
    #: Degrees from north, clockwise, rear to front, when measured.
    heading: Optional[float] = None
    #: 確度 ('' none).
    confidence: str = ""
    recorded_by: str = ""
    updated_by: str = ""


#: The record's attributes by role, the names used when the settings do not map them.
DEFAULT_OBSERVATION_FIELDS: dict[str, str] = {
    "id": "OBS_ID",
    "imageId": "IMAGE_ID",
    "imageName": "IMAGE_NAME",
    "imageTime": "IMAGE_TIME",
    "objectId": "OBJECT_ID",
    "objectName": "OBJECT_NAME",
    "kind": "KIND",
    "activity": "ACTIVITY",
    "present": "PRESENT",
    "note": "NOTE",
    "length": "LENGTH_M",
    "heading": "HEADING",
    "confidence": "CONFIDENCE",
    "recordedBy": "OBSERVER",
    "updatedBy": "UPDATED_BY",
    "created": "CREATED_AT",
    "updated": "UPDATED_AT",
    "deleted": "DELETED_AT",
}


@dataclass
class ConfidenceLevel:
    """One level of 確度, the surest first."""
    label: str
    color: Optional[str] = None
    note: Optional[str] = None


#: The levels offered when the settings give none.
DEFAULT_CONFIDENCES = [
    ConfidenceLevel("確定", "#15803d", "識別済み"),
    ConfidenceLevel("推定", "#d97706", "特徴からの推定"),
    ConfidenceLevel("不明", "#6b7280", "識別不可"),
]
#: Things farther than this (m) are not offered as the same one.
MATCH_METRES = 2000.0
DEFAULT_ACTIVITIES = ["停泊", "航行", "駐機", "移動", "工事", "集積", "活動なし"]


def default_taxonomy() -> list[Taxon]:
    """The classes offered at first: each branch with its own levels."""
    return [
        Taxon(["船舶"], levels=["大分類", "種別", "級"], name_label="船名"),
        Taxon(["航空機"], levels=["大分類", "種別", "型式"], name_label="機番"),
        Taxon(["車両"], levels=["大分類", "種別", "車種"]),
        Taxon(["建物"], levels=["大分類", "用途"]),
        Taxon(["構造物"], levels=["大分類", "種別"]),
        Taxon(["その他"]),
    ]


@dataclass
class ObservationVocabulary:
    """The kinds, activities and levels offered first (`observationVocabulary`)."""
    kinds: list[str] = field(default_factory=lambda: [t.path[0] for t in default_taxonomy()])
    activities: list[str] = field(default_factory=lambda: list(DEFAULT_ACTIVITIES))
    confidences: list[ConfidenceLevel] = field(default_factory=lambda: list(DEFAULT_CONFIDENCES))
    match_radius: float = MATCH_METRES
    taxonomy: list[Taxon] = field(default_factory=default_taxonomy)
    spec_fields: list[SpecField] = field(default_factory=lambda: list(DEFAULT_SPEC_FIELDS))


@dataclass
class ObservationStoreConfig:
    """One entry of `observationStores`."""
    label: str
    url: str
    fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_OBSERVATION_FIELDS))
    token: Optional[str] = None
    where: Optional[str] = None
    #: False: deleting removes the feature instead of marking it.
    soft_delete: bool = True
    kinds: Optional[list[str]] = None
    activities: Optional[list[str]] = None
    confidences: Optional[list[ConfidenceLevel]] = None
    areas: Optional[AreaStoreConfig] = None
    taxonomy: Optional[list[Taxon]] = None
    taxonomy_table: Optional[TaxonomyTableConfig] = None
    authcfg: Optional[str] = None


_COLOR = re.compile(r"^(#[0-9a-f]{3,8}|[a-z]+|(rgb|hsl)a?\([\d\s.,%]+\))$", re.IGNORECASE)
_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)
_NAME = re.compile(r"^[\w.]+$", re.ASCII)


def confidences_of(value: Any, problems: list[str], at: str) -> Optional[list[ConfidenceLevel]]:
    """The levels of 確度 (`["確定", …]` or `[{label, color, note}, …]`); None when none can be used."""
    if not isinstance(value, list):
        problems.append(f"{at} は配列で指定")
        return None
    out: list[ConfidenceLevel] = []
    for i, v in enumerate(value):
        item = {"label": v} if isinstance(v, str) else v if is_record(v) else None
        label = item["label"].strip() if item and isinstance(item.get("label"), str) else ""
        if not label:
            problems.append(f"{at}[{i}] は文字列か {{ label }} で指定")
            continue
        if any(c.label == label for c in out):
            problems.append(f"{at}[{i}] の「{label}」は重複")
            continue
        raw = item.get("color")
        color = raw.strip() if isinstance(raw, str) and _COLOR.match(raw.strip()) else None
        if "color" in item and not color:
            problems.append(f"{at}[{i}] の color は CSS の色で指定")
        note = item.get("note")
        out.append(ConfidenceLevel(label, color, note.strip() if isinstance(note, str) and note.strip() else None))
    return out or None


def observation_store_of(value: Any, problems: list[str], index: int) -> Optional[ObservationStoreConfig]:
    """One `observationStores` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"observationStores[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if not isinstance(url, str) or not _FEATURE_LAYER.search(url.split("?", 1)[0]):
        problems.append(f"{at} の url は編集できる …/FeatureServer/0 のようなポイントレイヤーの URL で指定")
        return None
    kinds = words(value.get("kinds"))
    if "taxonomy" in value:
        classes = taxonomy_of(value["taxonomy"], problems, f"{at}.taxonomy")
    else:
        classes = from_kinds(kinds) if kinds else None
    table = taxonomy_table_of(value["taxonomyTable"], problems, f"{at}.taxonomyTable") if "taxonomyTable" in value else None
    levels = confidences_of(value["confidences"], problems, f"{at}.confidences") if "confidences" in value else None
    areas = area_store_of(value["areas"], problems, f"{at}.areas") if "areas" in value else None
    mapped = dict(DEFAULT_OBSERVATION_FIELDS)
    fields = value.get("fields")
    if is_record(fields):
        for key, v in fields.items():
            if key not in DEFAULT_OBSERVATION_FIELDS:
                problems.append(f"{at} の fields.{key} は使用不可の役割")
            elif isinstance(v, str) and _NAME.match(v):
                mapped[key] = v
            else:
                problems.append(f"{at} の fields.{key} は属性名で指定")
    label, token, where = value.get("label"), value.get("token"), value.get("where")
    activities = words(value.get("activities"))
    return ObservationStoreConfig(
        label=label if isinstance(label, str) and label else f"観測の保存先 {index + 1}",
        url=layer_url(url),
        token=token if isinstance(token, str) and token else None,
        fields=mapped,
        where=where.strip() if isinstance(where, str) and where.strip() else None,
        soft_delete=value.get("softDelete") is not False,
        kinds=kinds or None,
        activities=activities or None,
        confidences=levels,
        areas=areas,
        taxonomy=classes or None,
        taxonomy_table=table,
    )


def vocabulary_of(value: Any, problems: list[str]) -> ObservationVocabulary:
    """`observationVocabulary`."""
    if not is_record(value):
        problems.append("observationVocabulary はオブジェクトで指定")
        return ObservationVocabulary()
    kinds = words(value.get("kinds"))
    if "taxonomy" in value:
        taxonomy = taxonomy_of(value["taxonomy"], problems, "observationVocabulary.taxonomy")
    else:
        taxonomy = from_kinds(kinds) if kinds else None
    base = ObservationVocabulary()
    activities = words(value.get("activities"))
    spec_fields = spec_fields_of(value["specFields"], problems, "observationVocabulary.specFields") if "specFields" in value else None
    confidences = confidences_of(value["confidences"], problems, "observationVocabulary.confidences") if "confidences" in value else None
    return ObservationVocabulary(
        kinds=list(dict.fromkeys(t.path[0] for t in taxonomy)) if taxonomy else kinds if kinds else base.kinds,
        activities=activities if activities else base.activities,
        taxonomy=taxonomy if taxonomy else base.taxonomy,
        spec_fields=spec_fields if spec_fields is not None else base.spec_fields,
        confidences=confidences if confidences is not None else base.confidences,
        match_radius=_match_radius_of(value.get("matchRadius"), "matchRadius" in value, problems),
    )


def _match_radius_of(value: Any, given: bool, problems: list[str]) -> float:
    if not given:
        return MATCH_METRES
    if is_number(value) and value > 0:
        return value
    problems.append("observationVocabulary.matchRadius は正の数（m）で指定")
    return MATCH_METRES


def new_observation_id() -> str:
    """A new record id."""
    return str(uuid.uuid4())


def new_object_id() -> str:
    """A new thing id: short, to be read and typed (`OBJ-K3F9Q2TX`)."""
    return "OBJ-" + uuid.uuid4().hex[-8:].upper()


#: The viewer's names of the record's values (for JSON shared with the viewer and its scripts).
_JSON_NAMES = {
    "id": "id", "image_id": "imageId", "image_name": "imageName", "image_time": "imageTime", "object_id": "objectId",
    "object_name": "objectName", "kind": "kind", "activity": "activity", "present": "present", "note": "note",
    "lon": "lon", "lat": "lat", "length": "length", "heading": "heading", "confidence": "confidence",
    "recorded_by": "recordedBy", "updated_by": "updatedBy", "created_at": "createdAt", "updated_at": "updatedAt",
    "deleted_at": "deletedAt",
}


def observation_json(o: Observation) -> dict[str, Any]:
    """A record as the viewer writes it (camelCase)."""
    return {name: getattr(o, attr) for attr, name in _JSON_NAMES.items()}


def observation_from_json(value: Any) -> Optional[Observation]:
    """A record written as the viewer writes it; None when it is not one."""
    if not is_record(value):
        return None
    v = {attr: value.get(name) for attr, name in _JSON_NAMES.items()}
    if not isinstance(v["id"], str) or not isinstance(v["object_id"], str) or not is_number(v["lon"]) or not is_number(v["lat"]):
        return None
    created = int(v["created_at"]) if is_number(v["created_at"]) else 0

    def text(key: str) -> str:
        return v[key] if isinstance(v[key], str) else ""

    def number(key: str) -> Optional[float]:
        return float(v[key]) if is_number(v[key]) else None

    return Observation(
        id=v["id"], image_id=text("image_id"), image_name=text("image_name"),
        image_time=int(v["image_time"]) if is_number(v["image_time"]) else None,
        object_id=v["object_id"], object_name=text("object_name"), kind=text("kind"), activity=text("activity"),
        present=v["present"] is not False, note=text("note"), lon=float(v["lon"]), lat=float(v["lat"]),
        created_at=created, updated_at=int(v["updated_at"]) if is_number(v["updated_at"]) else created,
        deleted_at=int(v["deleted_at"]) if is_number(v["deleted_at"]) else None,
        length=number("length"), heading=number("heading"), confidence=text("confidence"),
        recorded_by=text("recorded_by"), updated_by=text("updated_by"),
    )


@dataclass
class ListedImage:
    """An image recorded on without its file: one outside the catalog, or not opened here (id, name, time, footprint typed)."""
    id: str
    name: str
    time: Optional[int] = None
    #: [west, south, east, north] (WGS 84), when known.
    box: Optional[list[float]] = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "time": self.time, "box": self.box}


def recorded_images(records: list[Observation]) -> list[ListedImage]:
    """The images the records name, newest first: for picking one of them again."""
    by_id: dict[str, ListedImage] = {}
    for o in records:
        if o.deleted_at is not None:
            continue
        known = by_id.get(o.image_id)
        if known is None:
            by_id[o.image_id] = ListedImage(o.image_id, o.image_name or o.image_id, o.image_time, None)
        elif known.time is None and o.image_time is not None:
            known.time = o.image_time
    return sorted(by_id.values(), key=lambda i: (-(i.time if i.time is not None else -math.inf), i.id))


def parse_listed_images(text: Optional[str]) -> list[ListedImage]:
    """Listed images read back (bad entries left out, one per id)."""
    try:
        value = json.loads(text if text is not None else "[]")
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[ListedImage] = []
    for v in value:
        if not is_record(v):
            continue
        id_, name, t, box = v.get("id"), v.get("name"), v.get("time"), v.get("box")
        if not isinstance(id_, str) or not id_.strip() or id_ in seen:
            continue
        seen.add(id_)
        ok_box = list(box) if isinstance(box, list) and len(box) == 4 and all(is_number(n) for n in box) else None
        out.append(ListedImage(id_, name if isinstance(name, str) and name.strip() else id_, t if is_number(t) else None, ok_box))
    return out
