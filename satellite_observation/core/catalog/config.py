"""
Image catalogs (`imageCatalogs`): Esri layers whose features are images, as
the viewer reads them (catalog.ts `catalogOf`). The search, the similar
images and the watches build on this.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..util import is_name, is_number, is_record, is_url, layer_url

ROLE_LABELS = {"id": "画像ID", "acquired": "撮像日時", "registered": "登録日時", "systemRegistered": "システム登録日時", "sensor": "センサー", "angle": "角度", "azimuth": "方位角"}
ANGLE_KINDS = ("offNadir", "incidence", "elevation")
_LAYER = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)


@dataclass
class CatalogConfig:
    """One entry of `imageCatalogs`."""
    label: str
    url: str
    #: Attribute names by role; `source` is a list of names.
    fields: dict[str, Any]
    labels: dict[str, str] = field(default_factory=dict)
    columns: list[dict[str, str]] = field(default_factory=list)
    where: Optional[str] = None
    max_results: int = 500
    angle_kind: str = "offNadir"
    orbit_height: float = 600
    token: Optional[str] = None
    authcfg: Optional[str] = None

    def label_of(self, role: str) -> str:
        """The heading of a role: the settings file's `labels`, else the viewer's default."""
        return self.labels.get(role) or ROLE_LABELS[role]

    def to_json(self) -> dict[str, Any]:
        """The catalog as a watch keeps it, without its token (the settings file's token is used when checking)."""
        return {
            "label": self.label, "url": self.url, "fields": dict(self.fields), "labels": dict(self.labels),
            "columns": [dict(c) for c in self.columns], "where": self.where, "maxResults": self.max_results,
            "angleKind": self.angle_kind, "orbitHeight": self.orbit_height,
        }

    @staticmethod
    def from_json(value: dict[str, Any]) -> "CatalogConfig":
        fields = dict(value.get("fields") or {})
        fields["source"] = list(fields.get("source") or [])
        return CatalogConfig(
            label=str(value.get("label") or ""), url=str(value.get("url") or ""), fields=fields,
            labels=dict(value.get("labels") or {}), columns=[dict(c) for c in value.get("columns") or []],
            where=value.get("where") or None, max_results=int(value.get("maxResults") or 500),
            angle_kind=value.get("angleKind") if value.get("angleKind") in ANGLE_KINDS else "offNadir",
            orbit_height=float(value.get("orbitHeight") or 600),
        )


def catalog_of(value: Any, problems: list[str], index: int) -> Optional[CatalogConfig]:
    """The catalog of one `imageCatalogs` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"imageCatalogs[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url = value.get("url")
    if not is_url(url) or not _LAYER.search(url.split("?", 1)[0]):
        problems.append(f"{at} の url は …/FeatureServer/0 のようなレイヤーの URL で指定")
        return None
    fields = value.get("fields")
    if not is_record(fields):
        problems.append(f"{at} に fields（属性名の対応）なし")
        return None
    mapped: dict[str, Any] = {"source": []}
    for role in ("id", "acquired", "registered", "systemRegistered", "sensor", "angle", "azimuth"):
        name = fields.get(role)
        if name is None or name == "":
            continue
        if is_name(name):
            mapped[role] = name
        else:
            problems.append(f"{at} の fields.{role} は属性名（英数字と _）で指定")
    sources = fields.get("source") if isinstance(fields.get("source"), list) else [fields.get("source")]
    mapped["source"] = [s for s in sources if is_name(s)]
    if not mapped["source"]:
        problems.append(f"{at} に fields.source（COG の URL かローカルパスの入った属性名）なし")
        return None

    headings: dict[str, str] = {}
    labels = value.get("labels")
    if is_record(labels):
        for role in ROLE_LABELS:
            if isinstance(labels.get(role), str) and labels[role]:
                headings[role] = labels[role]
    extra: list[dict[str, str]] = []
    columns = value.get("columns")
    if isinstance(columns, list):
        for i, c in enumerate(columns):
            name = c if isinstance(c, str) else c.get("field") if is_record(c) else None
            if not is_name(name):
                problems.append(f'{at} の columns[{i}] は属性名か {{ "field", "label" }} で指定')
                continue
            heading = c["label"] if is_record(c) and isinstance(c.get("label"), str) and c["label"] else name
            extra.append({"field": name, "label": heading})
    elif "columns" in value:
        problems.append(f"{at} の columns は配列で指定")
    angle_kind, height = value.get("angleKind"), value.get("orbitHeight")
    if "angleKind" in value and angle_kind not in ANGLE_KINDS:
        problems.append(f"{at} の angleKind は {' / '.join(ANGLE_KINDS)} のいずれかで指定")
    if "orbitHeight" in value and not (is_number(height) and height > 0):
        problems.append(f"{at} の orbitHeight は衛星の高度（km）で指定")
    label, token, where, most = value.get("label"), value.get("token"), value.get("where"), value.get("maxResults")
    return CatalogConfig(
        label=label if isinstance(label, str) and label else f"カタログ {index + 1}",
        url=layer_url(url),
        token=token if isinstance(token, str) and token else None,
        fields=mapped,
        labels=headings,
        columns=extra,
        where=where.strip() if isinstance(where, str) and where.strip() else None,
        max_results=min(most, 5000) if isinstance(most, int) and not isinstance(most, bool) and most > 0 else 500,
        angle_kind=angle_kind if angle_kind in ANGLE_KINDS else "offNadir",
        orbit_height=height if is_number(height) and height > 0 else 600,
    )


#: How often new images are checked for, in minutes (`watchInterval`).
DEFAULT_WATCH_INTERVAL = 10


def watch_interval_of(value: Any, problems: list[str]) -> int | float:
    """`watchInterval`: 1 to 1440 minutes."""
    if is_number(value) and 1 <= value <= 24 * 60:
        return value
    problems.append("watchInterval は新着画像を確認する間隔（1〜1440 分）で指定")
    return DEFAULT_WATCH_INTERVAL
