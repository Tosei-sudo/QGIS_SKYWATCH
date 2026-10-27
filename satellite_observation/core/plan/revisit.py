"""
A revisit map, as the viewer (imaging-plan-revisit.ts): the area is cut into
a grid of cells, each cell's middle is planned as a point, and each cell is
coloured by how long it waits at most between two chances.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from .access import Opportunity, PlanTarget

#: The most cells a side.
MAX_GRID = 30
#: The colour of a cell never seen.
UNSEEN = (148, 163, 184)


@dataclass
class RevisitCell:
    ring: list
    center: tuple


@dataclass
class RevisitStats:
    #: Chances in the period (several satellites within a minute count once).
    count: int
    #: The mean time between two chances, hours; None with fewer than two.
    meanGap: Optional[float]
    #: The longest wait, from the start, between chances or until the end, hours.
    maxGap: float


def box_of(targets: Sequence[PlanTarget], least: float = 0.5) -> tuple:
    """The box (west, south, east, north) around the targets, at least `least` degrees a side."""
    w = s = math.inf
    e = n = -math.inf
    for t in targets:
        for x, y in (t.points or [t.center]):
            w, e, s, n = min(w, x), max(e, x), min(s, y), max(n, y)

    def grow(lo: float, hi: float) -> tuple:
        return (lo, hi) if hi - lo >= least else ((lo + hi) / 2 - least / 2, (lo + hi) / 2 + least / 2)

    w, e = grow(w, e)
    s, n = grow(s, n)
    return (w, max(-89.0, s), e, min(89.0, n))


def grid_cells(box: Sequence[float], size: float) -> list[RevisitCell]:
    """The box cut into `size` × `size` cells (rows from the north)."""
    w, s, e, n = box
    k = max(1, min(MAX_GRID, math.floor(size + 0.5)))
    dx, dy = (e - w) / k, (n - s) / k
    cells = []
    for j in range(k):
        for i in range(k):
            x0, y1 = w + i * dx, n - j * dy
            x1, y0 = x0 + dx, y1 - dy
            cells.append(RevisitCell([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], ((x0 + x1) / 2, (y0 + y1) / 2)))
    return cells


def cell_targets(cells: Sequence[RevisitCell]) -> list[PlanTarget]:
    """The cells as point targets to plan for."""
    return [PlanTarget(f"セル {i + 1}", c.center, []) for i, c in enumerate(cells)]


def revisit_stats(ops: Sequence[Opportunity], cells: int, start: float, end: float) -> list[RevisitStats]:
    """How often each of `cells` is seen, from the opportunities planned for cell_targets between `start` and `end`."""
    times: list[list[float]] = [[] for _ in range(cells)]
    for op in ops:
        if 0 <= op.target < cells:
            times[op.target].append(op.time)
    hour = 3600000
    out = []
    for items in times:
        items.sort()
        distinct = [t for i, t in enumerate(items) if i == 0 or t - items[i - 1] > 60000]
        edges = [start, *distinct, end]
        longest = max((edges[i] - edges[i - 1] for i in range(1, len(edges))), default=0)
        longest = max(0, longest)
        n = len(distinct)
        out.append(RevisitStats(n, (distinct[-1] - distinct[0]) / (n - 1) / hour if n >= 2 else None, longest / hour))
    return out


def revisit_color(stats: RevisitStats, shortest: float, longest: float) -> tuple:
    """Green for the shortest wait of the map through yellow to red for the longest; grey for a cell never seen."""
    if not stats.count:
        return UNSEEN
    f = max(0.0, min(1.0, (stats.maxGap - shortest) / (longest - shortest))) if longest > shortest else 0.0
    stops = [(22, 163, 74), (234, 179, 8), (220, 38, 38)]
    x = f * 2
    a, b = (stops[0], stops[1]) if x <= 1 else (stops[1], stops[2])
    t = x if x <= 1 else x - 1
    return tuple(math.floor(v + (b[i] - v) * t + 0.5) for i, v in enumerate(a))


def hours_text(hours: Optional[float]) -> str:
    """Hours as days and hours (「2 日 5 時間」)."""
    if hours is None:
        return "—"
    if hours < 24:
        v = math.floor(hours * 10 + 0.5) / 10 if hours < 10 else math.floor(hours + 0.5)
        return f"{v:g} 時間"
    d = math.floor(hours / 24)
    h = math.floor(hours - d * 24 + 0.5)
    return f"{d} 日 {h} 時間" if h else f"{d} 日"
