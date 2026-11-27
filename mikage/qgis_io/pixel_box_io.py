"""
観測記録をピクセル座標で書き出す (core/ai/pixel_boxes.py has the arithmetic):
each record of an image as its box in the image file's own pixels, and that
box placed on the ground by the file's own georeferencing (tsudo 2026-11-27:
「生画像の位置情報で置く」), as a GeoJSON feature.

Which pixel: the one the record kept (PIXEL_X・PIXEL_Y of its IMAGE_FILE: the
file's pixel whatever the image's 位置ずらし or クイック幾何補正 when it was
recorded, qgis_io/pixel_place), else its longitude and latitude on the image
as it lies now. The box's shape: the record's ground rectangle (its length,
heading and width; else the classification's 全長・全幅, else a default)
through the image as it lies now, about the record's place (a move shifts
the image, not its shape). The polygon: the box's corners through the file's
own placing (before any move or correction), so the file opened anywhere
shows it on the thing.

Placements (pixel_place.Placement) are read on the main thread; everything
here may run in any thread.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from ..core.ai.api import METRES_PER_DEGREE, footprint, kind_path, object_size
from ..core.ai.api import METRE_DIGITS, PixelRect, box_properties, feature, turned_box, upright_box
from ..core.obs.api import quantity_of
from .pixel_place import Placement, choose
from .shift_io import PIXEL_GRID

#: 「位置の出どころ」: the pixel the record kept, or the one found from its place.
FROM_PIXEL = "記録時の画素"
FROM_PLACE = "記録の経緯度"
#: What a record not measured is drawn as (the classification's sizes, else the default), or left out.
SIZE_FROM_MEASURE = ("記録", "記録（幅も）")


@dataclass
class ImageFile:
    """One file of an image (a layer): how it lies on the map now, and how its own georeferencing places it."""
    image_id: str
    image_name: str
    now: Placement
    #: The file's own placing (pixel_place.original_placement); None when it places itself nowhere (its pixels only).
    raw: Optional[Placement] = None
    #: The image moved (位置ずらし) or corrected (クイック幾何補正) when read: the polygons are off its picture then.
    moved: bool = False

    @property
    def file(self) -> str:
        return self.now.file

    def info(self) -> dict:
        """The image as the file's `mikage.images` lists it."""
        return {"image_id": self.image_id, "image_name": self.image_name, "image_file": self.file, "width": self.now.width,
                "height": self.now.height, "georeferenced": self.raw is not None}


def placed(p: Optional[Placement]) -> Optional[Placement]:
    """The placement, None when it places the file nowhere (QGIS's bare pixel grid)."""
    if p is None:
        return None
    if not p.gcps and not p.rpc and not p.options and (p.geotransform is None or tuple(p.geotransform) == PIXEL_GRID):
        return None
    return p


@dataclass
class Written:
    """What was made of the records: the features, and what was left out (by reason)."""
    features: list[dict] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    #: Features of each `position_from`, of each `size_from`.
    positions: dict[str, int] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    #: Features without a polygon (an image placed nowhere, a corner its model cannot reach).
    unplaced: int = 0

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _metres(v) -> Optional[float]:
    return round(float(v), METRE_DIGITS) if v is not None and math.isfinite(v) else None


def file_for(o, files: Sequence[ImageFile]) -> tuple[Optional[ImageFile], Optional[tuple[float, float]], str]:
    """
    The file of an image a record is in, its pixel there and where that
    came from: the pixel it kept when its file is one of these, else its
    place on the file over it (as the image lies now).
    """
    if o.pixel_x is not None and o.pixel_y is not None:
        for f in files:
            if o.image_file == f.file or (not o.image_file and len(files) == 1):
                return f, (float(o.pixel_x), float(o.pixel_y)), FROM_PIXEL
    now = choose([f.now for f in files], o.lon, o.lat)
    f = next((f for f in files if f.now is now), None)
    if f is None:
        return None, None, ""
    found = f.now.pixel(o.lon, o.lat)
    return f, found, FROM_PLACE


def pixel_box(o, size, f: ImageFile, centre: tuple[float, float]) -> Optional[PixelRect]:
    """The record's box in the file's pixels round `centre`: its ground rectangle through the image as it lies now."""
    at = f.now.pixel(o.lon, o.lat)
    if at is None:
        return None
    if size.heading is None:
        r = max(size.length, size.width) / 2
        k = max(math.cos(math.radians(o.lat)), 1e-6)
        east = f.now.pixel(o.lon + r / (METRES_PER_DEGREE * k), o.lat)
        north = f.now.pixel(o.lon, o.lat + r / METRES_PER_DEGREE)
        if east is None or north is None:
            return None
        return upright_box(centre[0], centre[1], (east[0] - at[0], east[1] - at[1]), (north[0] - at[0], north[1] - at[1]))
    offsets = []
    for lon, lat in footprint(o.lon, o.lat, size):
        p = f.now.pixel(lon, lat)
        if p is None:
            return None
        offsets.append((p[0] - at[0], p[1] - at[1]))
    return turned_box(centre[0], centre[1], offsets)


def ring_of(box: PixelRect, raw: Optional[Placement]) -> Optional[list[tuple[float, float]]]:
    """The box's corners as longitudes and latitudes by the file's own placing; None when it has none or misses one."""
    if raw is None:
        return None
    ring = []
    for x, y in box.corners:
        p = raw.place(x, y)
        if p is None:
            return None
        ring.append(p)
    return ring


def records_as_features(records, files: Sequence[ImageFile], taxonomy, wanted=None, default_size: float = 20.0,
                        measured_only: bool = False) -> Written:
    """
    The records of the images (`files`: their open files) as GeoJSON
    features of their boxes in the files' pixels. Left out: deleted, 消失 and
    不可 records (no thing to draw), those of several things (数量 2 or more:
    one box is not 30 cars), those `wanted` (kind → bool) refuses, those not
    measured when `measured_only`, and those whose pixel cannot be found.
    """
    out = Written()
    by_image: dict[str, list[ImageFile]] = {}
    for f in files:
        by_image.setdefault(f.image_id, []).append(f)
    for o in records:
        image_files = by_image.get(o.image_id)
        if not image_files:
            continue
        if o.deleted_at is not None:
            continue
        if getattr(o, "unseen", ""):
            out.skip("不可の記録")
            continue
        if not o.present:
            out.skip("消失の記録")
            continue
        if quantity_of(o) >= 2:
            out.skip("数量 2 以上の記録（まとめて記録）")
            continue
        if wanted is not None and not wanted(o.kind):
            out.skip("対象外の種別")
            continue
        size = object_size(kind_path(o.kind, taxonomy), taxonomy, o.length, o.heading, default_size, o.width)
        if measured_only and size.source not in SIZE_FROM_MEASURE:
            out.skip("長さを測っていない記録")
            continue
        f, centre, position_from = file_for(o, image_files)
        if f is None or centre is None:
            out.skip("画像の上で位置が求まらない記録")
            continue
        box = pixel_box(o, size, f, centre)
        if box is None:
            out.skip("画像の上で位置が求まらない記録")
            continue
        ring = ring_of(box, f.raw)
        if ring is None:
            out.unplaced += 1
        properties = {
            "obs_id": o.id,
            "image_id": o.image_id,
            "image_file": f.file,
            **box_properties(box),
            "length_m": _metres(size.length),
            "width_m": _metres(size.width),
            "heading": _metres(size.heading) if size.heading is not None else None,
            "kind": o.kind,
            "object_id": o.object_id,
            "object_name": o.object_name,
            "size_from": size.source,
            "position_from": position_from,
            "image_name": o.image_name,
            "image_width": f.now.width,
            "image_height": f.now.height,
            "image_time": _iso(o.image_time),
            "activity": o.activity,
            "confidence": o.confidence,
            "satellite": getattr(o, "satellite", "") or "",
            "recorded_by": o.recorded_by,
            "lon": round(o.lon, 8),
            "lat": round(o.lat, 8),
        }
        out.features.append(feature(properties, ring))
        out.positions[position_from] = out.positions.get(position_from, 0) + 1
        out.sizes[size.source] = out.sizes.get(size.source, 0) + 1
    return out
