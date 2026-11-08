"""
Targets from features (the viewer's `targetOf`, `cellsOf`, `priorityOf` and
`nameOf`, imaging-plan-panel.ts), on plain shapes in WGS84 so they need no
QGIS: qgis_io turns QgsGeometry into `Shape`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .access import PlanTarget

#: The most features planned one by one.
MAX_TARGETS = 2000
#: The most points of an area used for its width.
MAX_POINTS = 256
#: About how many cells sample an area.
CELLS = 600

Ring = Sequence[tuple]


@dataclass
class Shape:
    """A feature's geometry in WGS84 degrees."""
    #: Every vertex, in order (as the viewer's flat coordinates).
    vertices: list
    #: Its polygons, each a list of rings (outer first, then holes); empty for points and lines.
    polygons: list = field(default_factory=list)
    #: A point inside a single polygon (where it is aimed); None otherwise.
    interior: Optional[tuple] = None


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def spread_out(items: list, most: int) -> list:
    """Up to `most` of `items`, evenly spread."""
    if len(items) <= most:
        return items
    return [items[(i * len(items)) // most] for i in range(most)]


def _in_ring(ring: Ring, x: float, y: float) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_polygon(rings: Sequence[Ring], x: float, y: float) -> bool:
    """Whether a point is inside a polygon's outer ring and outside its holes."""
    return bool(rings) and _in_ring(rings[0], x, y) and not any(_in_ring(h, x, y) for h in rings[1:])


def _extent(points) -> Optional[list]:
    e = [math.inf, math.inf, -math.inf, -math.inf]
    for p in points:
        e[0], e[1] = min(e[0], p[0]), min(e[1], p[1])
        e[2], e[3] = max(e[2], p[0]), max(e[3], p[1])
    return e if e[0] <= e[2] else None


def cells_of(polygons: Sequence[Sequence[Ring]]) -> list:
    """
    About CELLS points evenly spread inside polygons: a grid over their
    extent, square on the ground, keeping the points inside; finer by half
    while too few fall inside (thin or scattered polygons).
    """
    if not polygons:
        return []
    extent = _extent(p for rings in polygons for p in rings[0])
    if extent is None:
        return []
    w, s, e, n = extent
    k = math.cos((s + n) / 2 * math.pi / 180)
    width = max((e - w) * k, 1e-9)
    height = max(n - s, 1e-9)
    step = math.sqrt(width * height / CELLS)
    cells: list = []
    for _ in range(4):
        cells = []
        nx = max(1, _js_round(width / step))
        ny = max(1, _js_round(height / step))
        for i in range(nx):
            x = w + (e - w) * (i + 0.5) / nx
            for j in range(ny):
                y = s + (n - s) * (j + 0.5) / ny
                if any(in_polygon(rings, x, y) for rings in polygons):
                    cells.append((x, y))
        if len(cells) >= CELLS / 4:
            break
        step /= 2
    return spread_out(cells, CELLS * 4)


def target_of(shapes: Sequence[Shape], label: str) -> Optional[PlanTarget]:
    """A target for features: one feature, or several planned as one area; None without geometry."""
    points: list = []
    polygons: list = []
    center = None
    for shape in shapes:
        points.extend(shape.vertices)
        polygons.extend(shape.polygons)
        # One polygon: aim inside it (its middle can fall outside a bent one).
        if len(shapes) == 1 and len(shape.polygons) == 1 and shape.interior:
            center = shape.interior
    extent = _extent(points)
    if extent is None:
        return None
    if center is None:
        center = ((extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2)
    unique = [] if len(points) == 1 else spread_out(points, MAX_POINTS)
    cells = cells_of(polygons)
    return PlanTarget(label, tuple(center[:2]), [tuple(p[:2]) for p in unique], cells or None)


def priority_of(value: Any) -> Optional[float]:
    """An attribute value as a priority number; None when it is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        n = float(value.strip())
    except ValueError:
        return None
    return n if math.isfinite(n) else None


NAME_KEYS = ("name", "NAME", "Name", "名称", "title", "TITLE")


def name_of(attributes: dict, fid: Any, index: int, name_field: str = "") -> str:
    """A feature's name for the list: the chosen attribute, a name-like one, its id, or its number."""
    for key in ([name_field] if name_field else []) + list(NAME_KEYS):
        v = attributes.get(key)
        if v is not None and v != "":
            return str(v)
    return f"地物 {index + 1}" if fid is None else str(fid)
