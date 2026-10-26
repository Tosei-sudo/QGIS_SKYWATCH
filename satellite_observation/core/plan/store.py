"""
Imaging plans kept in an Esri feature service (`planStores`), as the viewer
(imaging-plan-store.ts): one polygon feature per planned shot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..util import is_number, is_record, layer_url

#: The attribute names used when the settings do not map a role.
DEFAULT_PLAN_FIELDS: dict[str, str] = {
    "planId": "PLAN_ID",
    "planName": "PLAN_NAME",
    "shotTime": "SHOT_TIME",
    "satellite": "SATELLITE",
    "satelliteId": "SATELLITE_ID",
    "mode": "MODE",
    "target": "TARGET",
    "offNadir": "OFF_NADIR",
    "incidence": "INCIDENCE",
    "pass": "PASS_DIR",
    "side": "LOOK_SIDE",
    "status": "STATUS",
    "imageId": "IMAGE_ID",
    "created": "CREATED_AT",
}
#: The statuses of a shot.
STATUSES = ("予定", "撮像済み", "未撮像", "取消")
#: Their colours on the map and in the table.
STATUS_COLORS = {"予定": "#2563eb", "撮像済み": "#16a34a", "未撮像": "#dc2626", "取消": "#6b7280"}
_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)
_NAME = re.compile(r"^[\w.]+$", re.ASCII)


@dataclass
class PlanStoreConfig:
    """One entry of `planStores`."""
    label: str
    url: str
    fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PLAN_FIELDS))
    token: Optional[str] = None
    #: The image catalog (its label) asked whether shots were taken; the first one when absent.
    catalog: Optional[str] = None
    match_minutes: float = 30
    miss_after_days: float = 3
    authcfg: Optional[str] = None


def plan_store_of(value: Any, problems: list[str], index: int) -> Optional[PlanStoreConfig]:
    """The store of one `planStores` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"planStores[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if not isinstance(url, str) or not _FEATURE_LAYER.search(url.split("?", 1)[0]):
        problems.append(f"{at} の url は編集できる …/FeatureServer/0 のようなレイヤーの URL で指定")
        return None
    mapped = dict(DEFAULT_PLAN_FIELDS)
    fields = value.get("fields")
    if is_record(fields):
        for key in DEFAULT_PLAN_FIELDS:
            v = fields.get(key)
            if isinstance(v, str) and _NAME.match(v):
                mapped[key] = v
            elif key in fields:
                problems.append(f"{at} の fields.{key} は属性名で指定")

    def number(v: Any, fallback: float) -> float:
        return v if is_number(v) and v > 0 else fallback

    label, token, catalog = value.get("label"), value.get("token"), value.get("catalog")
    return PlanStoreConfig(
        label=label if isinstance(label, str) and label else f"計画の保存先 {index + 1}",
        url=layer_url(url),
        token=token if isinstance(token, str) and token else None,
        fields=mapped,
        catalog=catalog if isinstance(catalog, str) and catalog else None,
        match_minutes=number(value.get("matchMinutes"), 30),
        miss_after_days=number(value.get("missAfterDays"), 3),
    )
