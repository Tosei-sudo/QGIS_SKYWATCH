"""
Checked areas (確認範囲) of the observations, as the viewer
(observation-areas.ts): a part of an image that was looked over, with every
thing of a kind there recorded.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..util import is_record
from .taxonomy import kind_of, path_of, within

#: The area's attributes by role, the names used when the settings do not map them.
DEFAULT_AREA_FIELDS: dict[str, str] = {
    "id": "AREA_ID",
    "imageId": "IMAGE_ID",
    "imageName": "IMAGE_NAME",
    "imageTime": "IMAGE_TIME",
    "kind": "KIND",
    "note": "NOTE",
    "recordedBy": "OBSERVER",
    "created": "CREATED_AT",
    "updated": "UPDATED_AT",
    "deleted": "DELETED_AT",
}
_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+$", re.IGNORECASE)
_NAME = re.compile(r"^[\w.]+$", re.ASCII)


@dataclass
class CheckedArea:
    """A checked part of one image."""
    id: str
    image_id: str
    image_name: str
    image_time: Optional[int]
    #: Its outline: longitude / latitude, closed.
    ring: list[list[float]]
    #: The kind looked for (`船舶 > 支援船`); '' for everything.
    kind: str
    note: str
    created_at: int
    updated_at: int
    deleted_at: Optional[int] = None
    recorded_by: str = ""


@dataclass
class AreaStoreConfig:
    """An observation store's `areas`: an editable polygon layer."""
    url: str
    fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_AREA_FIELDS))
    soft_delete: bool = True
    token: Optional[str] = None


def area_store_of(value: Any, problems: list[str], at: str) -> Optional[AreaStoreConfig]:
    """`areas`: the layer's URL, or `{url, token, fields, softDelete}`; None (the reason in `problems`) when it cannot be used."""
    v = {"url": value} if isinstance(value, str) else value if is_record(value) else None
    url = v["url"].split("?", 1)[0].rstrip("/") if v and isinstance(v.get("url"), str) else ""
    if not _FEATURE_LAYER.search(url):
        problems.append(f"{at} は編集できる …/FeatureServer/1 のようなポリゴンレイヤーの URL で指定")
        return None
    fields = dict(DEFAULT_AREA_FIELDS)
    if is_record(v.get("fields")):
        for key, name in v["fields"].items():
            if key not in DEFAULT_AREA_FIELDS:
                problems.append(f"{at} の fields.{key} は使用不可の役割")
            elif isinstance(name, str) and _NAME.match(name):
                fields[key] = name
            else:
                problems.append(f"{at} の fields.{key} は属性名で指定")
    token = v.get("token")
    return AreaStoreConfig(url=url, fields=fields, soft_delete=v.get("softDelete") is not False, token=token if isinstance(token, str) and token else None)


def new_area_id() -> str:
    """A new area id."""
    return str(uuid.uuid4())


def box_ring(box) -> list[list[float]]:
    """The closed outline of a longitude / latitude box [west, south, east, north]."""
    w, s, e, n = box
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]


def area_contains(area: CheckedArea, lon: float, lat: float) -> bool:
    """Whether a place is inside an area's outline (even-odd)."""
    ring = area.ring
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def covers_kind(area: CheckedArea, kind: str) -> bool:
    """Whether an area was checked for things of a kind ('' kind: any)."""
    return not area.kind or within(kind, path_of(area.kind))


def covering_areas(areas: list[CheckedArea], image_id: str, lon: float, lat: float, kind: str) -> list[CheckedArea]:
    """The living areas of an image that cover a thing of `kind` at a place."""
    return [a for a in areas if a.deleted_at is None and a.image_id == image_id and covers_kind(a, kind) and area_contains(a, lon, lat)]


def area_label(area: CheckedArea) -> str:
    """What an area is called in lists: 「確認範囲（船舶）」."""
    return f"確認範囲（{kind_of(path_of(area.kind)) if area.kind else 'すべて'}）"
