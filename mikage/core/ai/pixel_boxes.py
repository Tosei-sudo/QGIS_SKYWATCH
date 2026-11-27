"""
観測記録をピクセル座標で書き出す (tsudo 2026-11-27: 「観測記録を、生画像上のポリゴン
(つまり、ピクセル座標と幅、長さ、角度)として出力するツールをAI関連の中に作って欲しい」
「GeoJSON(EPSG4326)で欲しい」, and the polygon placed by the raw image's own
georeferencing): the arithmetic, without QGIS.

A record's box on the ground (its length along its heading, its width across
it: core.ai.train.labels.footprint) is found in the image file's pixels by
its corners (qgis_io.pixel_place does the map ↔ pixel); `turned_box` makes
of them the rectangle along the length's direction in the pixels that holds
them (an image whose pixels are not square on the ground, a SAR image in its
radar geometry, shows a ground rectangle as a parallelogram). A record whose
heading is not known is the box along the image's own axes that holds the
object at any heading (`upright_box`).

Pixels: the file's full-resolution pixels from its top-left corner, x right,
y down. Angle: the box's front (rear → front of the two points), in degrees
clockwise from the image's up (−y), 0 ≤ a < 360. Corners: rear-left,
front-left, front-right, rear-right as seen in the image (clockwise),
written x1, y1, … x4, y4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

#: Decimals kept: of a pixel (1/100, as a record's PIXEL_X), of an angle, of metres, of a longitude or latitude (about 1 cm).
PIXEL_DIGITS = 2
ANGLE_DIGITS = 2
METRE_DIGITS = 2
DEGREE_DIGITS = 8
#: The file's top-level member saying what it is (GeoJSON allows such members, RFC 7946 6.1).
FORMAT = "mikage-pixel-boxes"
VERSION = 1


@dataclass
class PixelBox:
    """A record's box in an image file's pixels."""
    cx: float
    cy: float
    #: Along the front (px), and across it.
    length: float
    width: float
    #: Degrees clockwise from the image's up, of the front; None when the record's heading is not known.
    angle: Optional[float]
    #: Rear-left, front-left, front-right, rear-right (as seen in the image).
    corners: list[tuple[float, float]]


def angle_of(dx: float, dy: float) -> float:
    """The direction of a pixel offset (x right, y down) in degrees clockwise from the image's up, 0 ≤ a < 360."""
    a = math.degrees(math.atan2(dx, -dy)) % 360.0
    return 0.0 if a >= 360.0 else a


def _rectangle(cx: float, cy: float, ux: float, uy: float, length: float, width: float) -> list[tuple[float, float]]:
    # Right of the front, as seen in the image (y down): the front turned a quarter clockwise.
    vx, vy = -uy, ux
    hl, hw = length / 2, width / 2
    return [(cx + sl * hl * ux + sw * hw * vx, cy + sl * hl * uy + sw * hw * vy) for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def turned_box(cx: float, cy: float, offsets: Sequence[tuple[float, float]]) -> Optional[PixelBox]:
    """
    The box round the centre (cx, cy) along the record's length: `offsets`
    are its ground rectangle's corners in pixels from the centre (rear-left,
    front-left, front-right, rear-right); the box is the rectangle along the
    rear → front direction (between the middles of the rear and front sides)
    that holds the four. None when the corners say no direction.
    """
    if len(offsets) != 4:
        return None
    (rlx, rly), (flx, fly), (frx, fry), (rrx, rry) = offsets
    ax, ay = (flx + frx - rlx - rrx) / 2, (fly + fry - rly - rry) / 2
    n = math.hypot(ax, ay)
    if not (n > 1e-9 and math.isfinite(n)):
        return None
    ux, uy = ax / n, ay / n
    vx, vy = -uy, ux
    along = [x * ux + y * uy for x, y in offsets]
    across = [x * vx + y * vy for x, y in offsets]
    length, width = max(along) - min(along), max(across) - min(across)
    # (A parallelogram round the centre holds it in the middle: the shift is for one that is not.)
    mid_a, mid_b = (max(along) + min(along)) / 2, (max(across) + min(across)) / 2
    x, y = cx + mid_a * ux + mid_b * vx, cy + mid_a * uy + mid_b * vy
    return PixelBox(x, y, length, width, angle_of(ux, uy), _rectangle(x, y, ux, uy, length, width))


def upright_box(cx: float, cy: float, east: tuple[float, float], north: tuple[float, float]) -> PixelBox:
    """
    The box along the image's axes (its front the image's up) holding a
    circle on the ground round the centre: `east` and `north` are the pixel
    offsets of the circle's radius to the east and to the north (the
    object's heading not known: it may lie any way within the circle).
    """
    half_x = math.hypot(east[0], north[0])
    half_y = math.hypot(east[1], north[1])
    return PixelBox(cx, cy, 2 * half_y, 2 * half_x, None, _rectangle(cx, cy, 0.0, -1.0, 2 * half_y, 2 * half_x))


def counterclockwise(ring: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """A ring of longitudes and latitudes turned counterclockwise (GeoJSON's outer ring, RFC 7946 3.1.6), closed."""
    points = list(ring)
    if points and points[0] == points[-1]:
        points = points[:-1]
    twice = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))
    if twice < 0:
        points = points[:1] + points[1:][::-1]
    return points + points[:1]


def _round(v: Optional[float], digits: int) -> Optional[float]:
    if v is None or not math.isfinite(v):
        return None
    out = round(float(v), digits)
    return 0.0 if out == 0 else out


def box_properties(box: PixelBox) -> dict:
    """The box's members of a feature's properties."""
    return {
        "cx": _round(box.cx, PIXEL_DIGITS),
        "cy": _round(box.cy, PIXEL_DIGITS),
        "length_px": _round(box.length, PIXEL_DIGITS),
        "width_px": _round(box.width, PIXEL_DIGITS),
        "angle": _round(box.angle, ANGLE_DIGITS) if box.angle is not None else None,
        # x1, y1, … x4, y4 in one list (as DOTA's and YOLO OBB's rows; QGIS shows a flat list of numbers in its table).
        "corners_px": [_round(v, PIXEL_DIGITS) for corner in box.corners for v in corner],
    }


def feature(properties: dict, ring: Optional[Sequence[tuple[float, float]]]) -> dict:
    """A GeoJSON feature: its polygon (longitudes, latitudes; None for an image placed nowhere) and properties."""
    geometry = None
    if ring:
        closed = counterclockwise([(float(x), float(y)) for x, y in ring])
        geometry = {"type": "Polygon", "coordinates": [[[_round(x, DEGREE_DIGITS), _round(y, DEGREE_DIGITS)] for x, y in closed]]}
    return {"type": "Feature", "geometry": geometry, "properties": dict(properties)}


#: What the file says of its numbers (its top-level `mikage` member).
NOTES = {
    "pixels": "画像ファイルのフル解像度の画素。左上が原点、x は右、y は下（cx・cy・length_px・width_px・corners_px）",
    "angle": "前（2 点入力の後端→前端）の向き。画像の上（-y）から時計回りの度、0〜360。向きの分からない記録は null（枠は画像の軸にそろえる）",
    "corners": "corners_px は 4 隅の x1, y1, x2, y2, x3, y3, x4, y4。画像で見て後ろ左・前左・前右・後ろ右の順（時計回り）。geometry の外周は RFC 7946 に従い反時計回り",
    "geometry": "画素の 4 隅を、画像ファイル自身の位置情報（位置ずらし・クイック幾何補正の前）で経緯度（EPSG:4326）にしたもの",
    "heading": "記録の向き（北から時計回りの度）",
}


def collection(features: Sequence[dict], images: Sequence[dict], name: str = "", created: Optional[str] = None) -> dict:
    """The GeoJSON file: its features, and (member `mikage`) what it is, the images and what its numbers mean."""
    created = created or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    out: dict = {"type": "FeatureCollection"}
    if name:
        out["name"] = name
    out["mikage"] = {"format": FORMAT, "version": VERSION, "created": created, "images": [dict(i) for i in images], "notes": dict(NOTES)}
    out["features"] = list(features)
    return out
