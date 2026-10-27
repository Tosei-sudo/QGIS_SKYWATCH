"""
Observations (観測記録), as the viewer (observations.ts): one record per
thing per image — when, where, what thing (モノ) and what it was doing
(活動), or that it was no longer there (不在).

This module holds the record, the vocabulary (活動・確度・分類) and the
`observationStores` / `observationVocabulary` entries of the settings file.
"""
from __future__ import annotations

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
