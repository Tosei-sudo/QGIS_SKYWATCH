"""
The records as layers (the viewer's 「レイヤー化」): the living records as
points, the checked areas as polygons, each thing's flow as a line, with a
field per level of the kinds (kind_1 the top class) to style and filter a
branch. Times are ms (UTC); the QGIS side makes them dates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..plan.output import LayerField
from .areas import CheckedArea
from .flows import flows, living
from .geo import js_round
from .model import Observation
from .taxonomy import path_of


@dataclass
class ObservationLayer:
    """Features for a layer: each a geometry (a point (lon, lat), a line of them, or a ring) and its attributes."""
    title: str
    #: `point`, `line` or `polygon`.
    geometry: str
    fields: list[LayerField]
    features: list[tuple]
    #: The field the temporal controller follows.
    time_field: Optional[str] = None
    #: Its end (a span from `time_field`), when it lasts.
    end_field: Optional[str] = None


def _levels(depth: int) -> list[LayerField]:
    return [LayerField(f"kind_{i + 1}", "string", f"分類{i + 1}") for i in range(depth)]


def _level_values(kind: str, depth: int) -> dict[str, str]:
    path = path_of(kind)
    return {f"kind_{i + 1}": path[i] if i < len(path) else "" for i in range(depth)}


def observation_layers(records: Iterable[Observation], areas: Iterable[CheckedArea]) -> list[ObservationLayer]:
    """観測記録 (points), 確認範囲 (polygons, when any) and 観測の流れ (lines, of things seen twice or more); none without records."""
    rows = living(records)
    if not rows:
        return []
    depth = max(1, *(len(path_of(o.kind)) for o in rows))
    levels = _levels(depth)
    points = ObservationLayer("観測記録", "point", [
        LayerField("obs_id", "string", "観測ID"), LayerField("object_id", "string", "モノID"), LayerField("object", "string", "モノ"),
        LayerField("kind", "string", "種別"), *levels, LayerField("activity", "string", "活動"), LayerField("present", "string", "有無"),
        LayerField("confidence", "string", "確度"), LayerField("image_time", "date", "撮像日時"), LayerField("image_id", "string", "画像ID"),
        LayerField("image", "string", "画像"), LayerField("length_m", "double", "長さ（m）"), LayerField("heading_deg", "double", "向き（度）"),
        LayerField("note", "string", "備考"), LayerField("recorded_by", "string", "記録者"), LayerField("created_at", "date", "登録日時"),
        LayerField("updated_at", "date", "更新日時"),
    ], [((o.lon, o.lat), {
        **_level_values(o.kind, depth), "obs_id": o.id, "object_id": o.object_id, "object": o.object_name, "kind": o.kind,
        "activity": o.activity, "present": "あり" if o.present else "なし", "confidence": o.confidence or "",
        "recorded_by": o.recorded_by or "", "image_time": o.image_time, "image_id": o.image_id, "image": o.image_name,
        "length_m": o.length, "heading_deg": o.heading, "note": o.note, "created_at": o.created_at, "updated_at": o.updated_at,
    }) for o in rows], time_field="image_time")
    out = [points]
    shown = [a for a in areas if a.deleted_at is None]
    if shown:
        out.append(ObservationLayer("確認範囲", "polygon", [
            LayerField("area_id", "string", "確認範囲ID"), LayerField("kind", "string", "対象の種別"), LayerField("note", "string", "備考"),
            LayerField("image_time", "date", "撮像日時"), LayerField("image_id", "string", "画像ID"), LayerField("image", "string", "画像"),
            LayerField("recorded_by", "string", "記録者"), LayerField("created_at", "date", "登録日時"),
        ], [([tuple(p[:2]) for p in a.ring], {
            "area_id": a.id, "kind": a.kind, "note": a.note, "image_time": a.image_time, "image_id": a.image_id,
            "image": a.image_name, "recorded_by": a.recorded_by or "", "created_at": a.created_at,
        }) for a in shown], time_field="image_time"))
    lines = []
    for flow in flows(rows):
        seen = [o for o in flow.observations if o.present]
        if len(seen) < 2:
            continue
        lines.append(([(o.lon, o.lat) for o in seen], {
            **_level_values(flow.kind, depth), "object_id": flow.object_id, "object": flow.name, "kind": flow.kind,
            "first": flow.first, "last": flow.last, "images": flow.seen, "distance_m": js_round(flow.distance), "activity": flow.activity,
        }))
    if lines:
        out.append(ObservationLayer("観測の流れ", "line", [
            LayerField("object_id", "string", "モノID"), LayerField("object", "string", "モノ"), LayerField("kind", "string", "種別"), *levels,
            LayerField("first", "date", "初出"), LayerField("last", "date", "最終"), LayerField("images", "integer", "画像数"),
            LayerField("distance_m", "double", "移動距離（m）"), LayerField("activity", "string", "最新の活動"),
        ], lines, time_field="first", end_field="last"))
    return out
