"""
The outline of a mask (1 = inside) as polygons, as the viewer draws it
(ai/mask-outline.ts): the edges between inside and outside pixels joined
into rings, holes included, then simplified (Douglas–Peucker) so a smooth
edge is not a staircase of pixel corners.

The edges are found with NumPy; only the walk round them is Python, so the
time grows with the outline's length, not the mask's size.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

#: A polygon: its outer ring, then its holes; points are (x, y) in pixels (corners, y down).
PixelPolygon = list[list[tuple[float, float]]]

# Directions on the pixel-corner grid, clockwise on the picture (y down): right, down, left, up.
DX = (1, 0, -1, 0)
DY = (0, 1, 0, -1)


def ring_area(ring: Sequence[Sequence[float]]) -> float:
    """Area of a ring (positive when clockwise on a y-down picture)."""
    total = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i - 1]
        x1, y1 = ring[i]
        total += x0 * y1 - x1 * y0
    return total / 2


def _contains(ring: Sequence[Sequence[float]], p: Sequence[float]) -> bool:
    """Whether `p` is inside `ring` (even–odd), testing a point nudged off the grid so corners are never on an edge."""
    x, y = p[0] + 0.25, p[1] + 0.125
    inside = False
    n = len(ring)
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[i - 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


def mask_outline(mask: np.ndarray, tolerance: float = 0.7, min_area: float = 4) -> list[PixelPolygon]:
    """
    The polygons of a mask (height × width, non-zero inside). Rings smaller
    than `min_area` pixels are left out; `tolerance` (pixels) is how far the
    simplified outline may stray from the pixel edges.
    """
    inside = np.asarray(mask) != 0
    height, width = inside.shape
    W = width + 1
    padded = np.zeros((height + 2, width + 2), bool)
    padded[1:-1, 1:-1] = inside
    # Each boundary edge, oriented with the inside on its right (clockwise round the inside), in the
    # viewer's order: pixel by pixel, row by row, then top, right, bottom, left.
    sides = np.stack([
        inside & ~padded[:-2, 1:-1],
        inside & ~padded[1:-1, 2:],
        inside & ~padded[2:, 1:-1],
        inside & ~padded[1:-1, :-2],
    ], axis=-1)
    ys, xs, dirs = np.nonzero(sides)
    if not len(dirs):
        return []
    # The start corner of each edge.
    sx = xs + np.array([0, 1, 1, 0])[dirs]
    sy = ys + np.array([0, 0, 1, 1])[dirs]
    starts = sy * W + sx
    # A corner starts two edges at most (where two pixels touch diagonally): the first and second of them.
    size = W * (height + 1)
    first = np.full(size, -1, np.int64)
    second = np.full(size, -1, np.int64)
    order = np.argsort(starts, kind="stable")
    ordered = starts[order]
    new = np.ones(len(order), bool)
    new[1:] = ordered[1:] != ordered[:-1]
    first[ordered[new]] = order[new]
    second[ordered[~new]] = order[~new]
    dirs_l = dirs.tolist()
    starts_l = starts.tolist()
    used = bytearray(len(dirs_l))
    rings: list[list[tuple[float, float]]] = []
    for e in range(len(dirs_l)):
        if used[e]:
            continue
        ring: list[tuple[float, float]] = []
        edge = e
        at = starts_l[e]
        while not used[edge]:
            used[edge] = 1
            d = dirs_l[edge]
            x = at % W
            y = at // W
            ring.append((x, y))
            at = (y + DY[d]) * W + (x + DX[d])
            # At a diagonal pinch, turn right (keep to the pixel just walked round), so rings never cross.
            a = int(first[at])
            b = int(second[at])
            if b < 0 or used[b]:
                edge = a if a >= 0 and not used[a] else b
            elif used[a]:
                edge = b
            else:
                turn = (d + 1) % 4
                edge = a if dirs_l[a] == turn else b if dirs_l[b] == turn else a
            if edge < 0:
                break
        simple = simplify_ring(_drop_straight(ring), tolerance)
        if len(simple) >= 3:
            rings.append(simple)

    # Outer rings run clockwise (positive area), holes the other way; a hole goes to the smallest outer ring round it.
    areas = [ring_area(r) for r in rings]
    outers = [(r, a) for r, a in zip(rings, areas) if a >= min_area]
    polygons: list[PixelPolygon] = [[r] for r, _ in outers]
    for ring, area in zip(rings, areas):
        if area > -min_area:
            continue
        best = -1
        for j, (outer, outer_area) in enumerate(outers):
            if outer_area > -area and _contains(outer, ring[0]) and (best < 0 or outer_area < outers[best][1]):
                best = j
        if best >= 0:
            polygons[best].append(ring)
    return polygons


def _drop_straight(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The ring without points in the middle of straight runs."""
    n = len(ring)
    if n < 4:
        return ring
    out = []
    for i, p in enumerate(ring):
        a = ring[i - 1]
        b = ring[(i + 1) % n]
        if (p[0] - a[0]) * (b[1] - p[1]) - (p[1] - a[1]) * (b[0] - p[0]) != 0:
            out.append(p)
    return out


def simplify_ring(ring: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Douglas–Peucker on a closed ring: split at the point farthest from the first, simplify both halves."""
    if len(ring) <= 4 or tolerance <= 0:
        return ring
    far, best = 0, -1.0
    x0, y0 = ring[0]
    for i in range(1, len(ring)):
        d = (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2
        if d > best:
            best, far = d, i
    a = _simplify_line(ring[:far + 1], tolerance)
    b = _simplify_line(ring[far:] + [ring[0]], tolerance)
    return a[:-1] + b[:-1]


def _simplify_line(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    keep = bytearray(len(points))
    keep[0] = keep[-1] = 1
    stack = [(0, len(points) - 1)]
    t2 = tolerance * tolerance
    while stack:
        i, j = stack.pop()
        ax, ay = points[i]
        bx, by = points[j]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        far, best = -1, t2
        for k in range(i + 1, j):
            px, py = points[k]
            if len2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                cross = dx * (py - ay) - dy * (px - ax)
                d = cross * cross / len2
            if d > best:
                best, far = d, k
        if far >= 0:
            keep[far] = 1
            stack += [(i, far), (far, j)]
    return [p for p, k in zip(points, keep) if k]
