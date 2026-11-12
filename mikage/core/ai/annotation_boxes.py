"""
「アノテーション変換」: point annotations (an object marked by one click)
made into boxes turned to the object, for training detectors that want a
box (AI 学習, or YOLO OBB / DOTA elsewhere).

The orientation and the long and short sides come from, in order: the
annotation's own attributes (heading, length, width: what a two-, three- or
four-point observation measured), else the object's outline round the
point, found by Segment Anything (SAM) or by a simple brightness threshold;
the length can be given (for all, or by class), the width given, taken as a
ratio of the length, or measured on the outline (its ratio to the length,
or itself).

Everything here works in an image's pixels (x right, y down, north up), with
metres per pixel to turn lengths in metres into pixels. NumPy only.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from .mask_outline import mask_outline, ring_area
from .sam import part_at

#: How the width is found.
WIDTH_MODES = ("aspect", "measured", "ratio", "fixed")
#: What becomes of a point whose direction is not known.
UNKNOWN_MODES = ("square", "north", "skip")
#: The length looked round when it is not known (metres).
SEARCH_LENGTH = 50.0


class ConvertError(Exception):
    pass


@dataclass
class Shape:
    """An object's outline as its smallest turned rectangle (pixels): centre, long and short side, the long side's direction."""
    cx: float
    cy: float
    length: float
    width: float
    #: Degrees clockwise from the image's up (north), 0–180 (the outline does not tell front from back).
    angle: float


def convex_hull(points: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    """The convex hull (Andrew's monotone chain), counter-clockwise in x right / y up terms."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_area_rect(points: Sequence[Sequence[float]]) -> Optional[Shape]:
    """The smallest rectangle round `points` (one of its sides lies along a side of their hull)."""
    hull = convex_hull(points)
    if len(hull) < 3:
        return None
    h = np.asarray(hull, np.float64)
    best = None
    for i in range(len(h)):
        e = h[(i + 1) % len(h)] - h[i]
        n = math.hypot(*e)
        if n == 0:
            continue
        u = e / n
        v = np.array([-u[1], u[0]])
        a, b = h @ u, h @ v
        area = (a.max() - a.min()) * (b.max() - b.min())
        if best is None or area < best[0]:
            best = (area, u, v, a.min(), a.max(), b.min(), b.max())
    if best is None:
        return None
    _, u, v, a0, a1, b0, b1 = best
    centre = u * (a0 + a1) / 2 + v * (b0 + b1) / 2
    la, lb = a1 - a0, b1 - b0
    axis = u if la >= lb else v
    # Clockwise from up on a y-down picture: up is (0, −1), right (1, 0).
    angle = math.degrees(math.atan2(axis[0], -axis[1])) % 180
    return Shape(float(centre[0]), float(centre[1]), float(max(la, lb)), float(min(la, lb)), angle)


def mask_shape(mask: np.ndarray, x: float, y: float, max_share: float = 0.5) -> Optional[Shape]:
    """
    The object's shape in a mask (1 = object) round the point (`x`, `y`,
    pixels): the part holding the point (else the whole mask), as its
    smallest turned rectangle. None when nothing is there, or when the part
    is no single object (most of the window, or reaching across it).
    """
    mask = np.asarray(mask)
    if not mask.any():
        return None
    part = part_at(mask, x, y)
    h, w = part.shape
    if part.sum() > max_share * h * w:
        return None
    rows, cols = np.any(part, axis=1), np.any(part, axis=0)
    if (rows[0] and rows[-1]) or (cols[0] and cols[-1]):
        return None
    polygons = mask_outline(part, 0.7, 1)
    if not polygons:
        return None
    outer = max((p[0] for p in polygons), key=lambda r: abs(ring_area(r)))
    return min_area_rect(outer)


def gray(rgba: np.ndarray) -> np.ndarray:
    """Brightness (0–255) of an RGBA picture; transparent pixels NaN."""
    rgb = rgba[..., :3].astype(np.float32)
    out = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
    if rgba.shape[-1] > 3:
        out[rgba[..., 3] == 0] = np.nan
    return out


def otsu(values: np.ndarray) -> float:
    """Otsu's threshold of `values` (NaN left out)."""
    v = values[np.isfinite(values)]
    if not len(v):
        return 0.0
    hist, edges = np.histogram(v, bins=256, range=(float(v.min()), float(v.max()) + 1e-6))
    p = hist.astype(np.float64) / max(hist.sum(), 1)
    centres = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(p)
    m0 = np.cumsum(p * centres)
    mt = m0[-1]
    w1 = 1 - w0
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mt * w0 - m0) ** 2 / (w0 * w1)
    between[~np.isfinite(between)] = -1
    return float(edges[int(np.argmax(between)) + 1])


def threshold_mask(rgba: np.ndarray, x: float, y: float) -> np.ndarray:
    """
    The simple outline without a model: the picture split by brightness
    (Otsu), the side the point is on (a bright ship on dark sea, a dark
    aircraft on a bright apron), judged by the mean round the point.
    """
    g = gray(rgba)
    t = otsu(g)
    h, w = g.shape
    xi, yi = min(max(int(x), 0), w - 1), min(max(int(y), 0), h - 1)
    near = g[max(yi - 1, 0):yi + 2, max(xi - 1, 0):xi + 2]
    bright = bool(np.nanmean(near) >= t) if np.isfinite(near).any() else True
    with np.errstate(invalid="ignore"):
        mask = (g >= t) if bright else (g < t)
    mask &= np.isfinite(g)
    return mask.astype(np.uint8)


def parse_lengths(text: str) -> dict[str, float]:
    """`船舶=120, 車両=5` (also `:`) as class → length in metres."""
    out: dict[str, float] = {}
    for part in re.split(r"[,、，;\n]+", text or ""):
        if not part.strip():
            continue
        m = re.match(r"^\s*(.+?)\s*(?:=|:|→|->)\s*([0-9.]+)\s*(?:m)?\s*$", part)
        if not m:
            raise ConvertError(f"クラスごとの長さは「クラス=長さ（m）」で指定: {part.strip()}")
        out[m.group(1)] = float(m.group(2))
    return out


def length_for(cls: Sequence[str], lengths: dict[str, float]) -> Optional[float]:
    """The length given for a class: by any of its names (the annotation's, its kind), then by the kind's upper levels."""
    for name in cls:
        if name in lengths:
            return lengths[name]
    for name in cls:
        parts = [p.strip() for p in str(name).split(">")]
        for depth in range(len(parts) - 1, 0, -1):
            key = " > ".join(parts[:depth])
            if key in lengths:
                return lengths[key]
    return None


@dataclass
class Rules:
    """How sizes and directions are decided (lengths in metres)."""
    #: The length of every object (0: measured on the outline).
    length: float = 0.0
    #: Length by class (the annotation's class name or kind).
    lengths: dict[str, float] = field(default_factory=dict)
    width_mode: str = "aspect"
    #: The width with width_mode "fixed".
    width: float = 0.0
    #: Width / length with width_mode "ratio", and when the outline is not found.
    ratio: float = 0.25
    unknown: str = "square"
    #: Move the box to the outline's centre (the point may be off the middle).
    recenter: bool = True


@dataclass
class Box:
    """A converted annotation: the box in pixels, and its size in metres."""
    cx: float
    cy: float
    #: Pixels.
    length: float
    width: float
    #: Degrees clockwise from north (the image's up); None for the square round any direction.
    angle: Optional[float]
    length_m: float
    width_m: float
    #: Where they came from: 属性・入力値・クラス別・SAM・しきい値・比率 / 属性・SAM・しきい値・不明.
    size_from: str
    dir_from: str

    def corners(self) -> list[tuple[float, float]]:
        """The four corners (pixels), rear left, front left, front right, rear right (front = along the angle)."""
        if self.angle is None:
            half = max(self.length, self.width) / 2
            return [(self.cx - half, self.cy + half), (self.cx - half, self.cy - half), (self.cx + half, self.cy - half), (self.cx + half, self.cy + half)]
        a = math.radians(self.angle)
        ux, uy = math.sin(a), -math.cos(a)
        vx, vy = -uy, ux
        hl, hw = self.length / 2, self.width / 2
        return [(self.cx + ux * sl * hl + vx * sw * hw, self.cy + uy * sl * hl + vy * sw * hw)
                for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _positive(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and math.isfinite(f) else None


def convert(x: float, y: float, metres_per_pixel: float, rules: Rules, shape: Optional[Shape] = None, method: str = "SAM",
            cls: Sequence[str] = (), length=None, width=None, heading=None) -> tuple[Optional[Box], str]:
    """
    One point (`x`, `y`, pixels) as a box: its attributes (`length`,
    `width` in metres, `heading` in degrees from north) first, then `rules`
    with the outline's `shape` (found by `method`). Gives the box, or None
    and why it is left out.
    """
    mpp = metres_per_pixel if metres_per_pixel and metres_per_pixel > 0 else 1.0
    length, width = _positive(length), _positive(width)
    try:
        heading = float(heading) if heading not in (None, "") else None
    except (TypeError, ValueError):
        heading = None
    if heading is not None and not math.isfinite(heading):
        heading = None
    # The direction.
    if heading is not None:
        angle, dir_from = heading % 360, "属性"
    elif shape is not None:
        angle, dir_from = shape.angle, method
    else:
        angle, dir_from = None, "不明"
    # The length.
    if length:
        size_from = "属性"
    elif length_for(cls, rules.lengths):
        length, size_from = length_for(cls, rules.lengths), "クラス別"
    elif rules.length > 0:
        length, size_from = rules.length, "入力値"
    elif shape is not None:
        length, size_from = shape.length * mpp, method
    else:
        return None, "長さが決まらない（輪郭なし・長さ未入力）"
    # The width.
    if width:
        width_from = "属性"
    elif rules.width_mode == "fixed" and rules.width > 0:
        width, width_from = rules.width, "入力値"
    elif rules.width_mode == "aspect" and shape is not None and shape.length > 0:
        width, width_from = length * shape.width / shape.length, f"{method}の縦横比"
    elif rules.width_mode == "measured" and shape is not None:
        width, width_from = shape.width * mpp, method
    else:
        width, width_from = length * rules.ratio, "比率"
    if angle is None:
        if rules.unknown == "skip":
            return None, "向きが決まらない"
        if rules.unknown == "north":
            angle, dir_from = 0.0, "不明（北向き）"
    cx, cy = x, y
    if rules.recenter and shape is not None and dir_from != "属性":
        cx, cy = shape.cx, shape.cy
    return Box(cx, cy, length / mpp, width / mpp, angle, length, width, f"{size_from}・幅 {width_from}", dir_from), ""


def window_at(x: float, y: float, side: int, width: int, height: int) -> tuple[int, int]:
    """
    The top left of the `side`-pixel window to look at round (`x`, `y`):
    on a grid of half windows (so points near each other share one, and each
    is at least a quarter window from its edges).
    """
    step = max(side // 2, 1)
    x0 = int(round((x - side / 2) / step)) * step
    y0 = int(round((y - side / 2) / step)) * step
    return x0, y0


def window_side(length_px: Optional[float], metres_per_pixel: float, lowest: int = 64, highest: int = 4096) -> int:
    """How many pixels to look at round a point: four object lengths (else four times SEARCH_LENGTH metres)."""
    base = length_px if length_px and length_px > 0 else SEARCH_LENGTH / (metres_per_pixel if metres_per_pixel > 0 else 1.0)
    side = int(math.ceil(base * 4))
    side = max(lowest, min(highest, side))
    return side + side % 2


def is_point_box(x1: float, y1: float, x2: float, y2: float, most: float) -> bool:
    """Whether a box (pixels) is a point annotation: both sides at most `most` pixels."""
    return abs(x2 - x1) <= most and abs(y2 - y1) <= most


def dota_row(corners: Sequence[Sequence[float]], name: str) -> str:
    """A DOTA annotation row: `x1 y1 … x4 y4 class 0` (pixels)."""
    return " ".join(f"{v:.2f}" for c in corners for v in c) + f" {name.replace(' ', '_')} 0"
