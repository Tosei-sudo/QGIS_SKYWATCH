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


def _signed_area(ring: Sequence[Sequence[float]]) -> float:
    a = 0.0
    j = len(ring) - 1
    for i in range(len(ring)):
        a += (ring[j][0] - ring[i][0]) * (ring[j][1] + ring[i][1])
        j = i
    return a


def esri_rings(rings: Sequence) -> list:
    """Rings as Esri wants them: outer rings clockwise, holes anticlockwise (polygons_of tells which is which)."""
    out = []
    for polygon in polygons_of(rings):
        for i, r in enumerate(polygon):
            clockwise = _signed_area(r) < 0
            out.append([list(p) for p in r] if clockwise == (i == 0) else list(reversed(r)))
    return out


def _box(rings: Sequence) -> tuple[float, float, float, float]:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, -1.0, -1.0)


def _cross(a, b, c, d) -> bool:
    """Whether segments ab and cd cross (touching counts)."""
    def side(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2, d3, d4 = side(c, d, a), side(c, d, b), side(a, b, c), side(a, b, d)
    if ((d1 > 0) != (d2 > 0) or d1 == 0 or d2 == 0) and ((d3 > 0) != (d4 > 0) or d3 == 0 or d4 == 0):
        return (min(a[0], b[0]) <= max(c[0], d[0]) and min(c[0], d[0]) <= max(a[0], b[0])
                and min(a[1], b[1]) <= max(c[1], d[1]) and min(c[1], d[1]) <= max(a[1], b[1]))
    return False


def rings_overlap(a: Sequence, b: Sequence) -> bool:
    """
    Whether two shapes (rings, even-odd) share some area or an edge: their
    edges cross, or a vertex of one is inside the other.
    """
    a = [r for r in a if len(r) >= 3]
    b = [r for r in b if len(r) >= 3]
    if not a or not b:
        return False
    ba, bb = _box(a), _box(b)
    if ba[0] > bb[2] or bb[0] > ba[2] or ba[1] > bb[3] or bb[1] > ba[3]:
        return False
    if any(in_rings(b, p[0], p[1]) for r in a for p in r) or any(in_rings(a, p[0], p[1]) for r in b for p in r):
        return True
    return any(_cross(r[i - 1], r[i], s[j - 1], s[j]) for r in a for i in range(len(r)) for s in b for j in range(len(s)))
