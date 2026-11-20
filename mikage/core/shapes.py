"""
Shapes in WGS 84 degrees as the features hold them: a feature's geometry (Shape), its rings
grouped into polygons with their holes, a ring's area and whether a point is inside rings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass
class Shape:
    """A feature's geometry in WGS84 degrees."""
    #: Every vertex, in order (as the viewer's flat coordinates).
    vertices: list
    #: Its polygons, each a list of rings (outer first, then holes); empty for points and lines.
    polygons: list = field(default_factory=list)
    #: A point inside a single polygon (where it is aimed); None otherwise.
    interior: Optional[tuple] = None


def ring_area(ring: Sequence) -> float:
    """A ring's signed area (degrees², anticlockwise positive)."""
    return sum(ring[i - 1][0] * ring[i][1] - ring[i][0] * ring[i - 1][1] for i in range(len(ring))) / 2


def in_rings(rings: Sequence, x: float, y: float) -> bool:
    """Whether a point is inside rings (even-odd)."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
                inside = not inside
            j = i
    return inside


def polygons_of(rings: Sequence) -> list[list]:
    """
    Rings grouped into polygons, each its outer ring first and then its
    holes, by how they lie in one another (whichever way they turn): a ring
    inside an even number of others is an outer ring, one inside an odd
    number a hole of the smallest outer ring holding it. Rings of fewer than
    three points are left out; the order of the rings is kept otherwise.
    """
    rs = [r for r in rings if len(r) >= 3]
    areas = [abs(ring_area(r)) for r in rs]
    holders = []
    for i, r in enumerate(rs):
        # A point of the ring not on another ring's edge (the first vertex mostly is not).
        x, y = r[0][0], r[0][1]
        holders.append([j for j, other in enumerate(rs) if j != i and in_rings([other], x, y)])
    depth = [len(h) for h in holders]
    out: dict = {}
    order = []
    for i, r in enumerate(rs):
        if depth[i] % 2 == 0:
            out[i] = [r]
            order.append(i)
    for i, r in enumerate(rs):
        if depth[i] % 2 == 1:
            outer = [j for j in holders[i] if depth[j] == depth[i] - 1]
            if outer:
                out[min(outer, key=lambda j: areas[j])].append(r)
            else:
                out[i] = [r]
                order.append(i)
    return [out[i] for i in sorted(order)]
