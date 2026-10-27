"""
The imaging plan's timeline (the viewer's imaging-plan-timeline.ts): the
listed shots as bars over the planned period, one row per satellite, with
the time each satellite is busy after a shot (its interval) shaded behind
it. Here only the layout, in pixels; ui/plan_timeline.py paints it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .access import Opportunity
from .output import natural_key
from .satellites import SatelliteSpec, platform_key

ROW = 20
HEAD = 18
LABEL = 120
#: Colours of the targets, in turn.
COLOURS = ("#2563eb", "#ea580c", "#16a34a", "#9333ea", "#db2777", "#0891b2", "#ca8a04", "#4f46e5", "#dc2626", "#65a30d")


@dataclass
class TimelineRow:
    satellite: int
    ops: list = field(default_factory=list)


def timeline_rows(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec]) -> list[TimelineRow]:
    """The satellites with shots, by name, each with its shots in time order (all its modes in one row)."""
    rows: dict[str, TimelineRow] = {}
    for op in ops:
        key = platform_key(sats[op.satellite])
        rows.setdefault(key, TimelineRow(op.satellite)).ops.append(op)
    for row in rows.values():
        row.ops.sort(key=lambda o: o.time)
    return sorted(rows.values(), key=lambda r: natural_key(sats[r.satellite].name))


@dataclass
class Bar:
    op: Opportunity
    x: float
    y: float
    colour: str
    chosen: bool


@dataclass
class Layout:
    width: float
    height: float
    #: (x, label or "") of each local midnight.
    days: list
    #: (y, name, tooltip) of each row; odd rows shaded.
    labels: list
    #: (x, y, width) of the busy times after shots.
    busy: list
    bars: list


def shorten(name: str) -> str:
    return f"{name[:15]}…" if len(name) > 16 else name


def layout(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], start: float, end: float, width: float,
           chosen: Optional[Opportunity] = None) -> Optional[Layout]:
    """Where everything goes in a timeline `width` pixels wide; None when there is nothing to show."""
    rows = timeline_rows(ops, sats)
    if not rows or end <= start:
        return None
    width = max(320.0, width or 640.0)
    plot = width - LABEL - 8
    height = HEAD + len(rows) * ROW + 4

    def x(t: float) -> float:
        return LABEL + (min(end, max(start, t)) - start) / (end - start) * plot

    days = []
    day = datetime.fromtimestamp(start / 1000).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    n_days = math.ceil((end - start) / 86400000)
    every = max(1, math.ceil(n_days * 46 / plot))
    k = 0
    while day.timestamp() * 1000 < end:
        t = day.timestamp() * 1000
        days.append((x(t), f"{day.month}/{day.day}" if k % every == 0 else ""))
        day += timedelta(days=1)
        k += 1
    colours: dict[int, str] = {}

    def colour_of(target: int) -> str:
        if target not in colours:
            colours[target] = COLOURS[len(colours) % len(COLOURS)]
        return colours[target]

    labels, busy, bars = [], [], []
    for i, row in enumerate(rows):
        sat = sats[row.satellite]
        y = HEAD + i * ROW
        limits = "・".join(p for p in [f"撮像間隔 {_num(sat.minInterval)} 分" if sat.minInterval > 0 else "",
                                         f"1 日 {sat.maxPerDay} 回まで" if sat.maxPerDay > 0 else ""] if p)
        labels.append((y, shorten(sat.name), f"{sat.name}（{len(row.ops)} 回）" + (f"・{limits}" if limits else "")))
        if sat.minInterval > 0:
            for op in row.ops:
                busy.append((x(op.time), y + 3, max(1.0, x(op.time + sat.minInterval * 60000) - x(op.time))))
        for op in row.ops:
            bars.append(Bar(op, x(op.time), y, colour_of(op.target), op is chosen))
    return Layout(width, height, days, labels, busy, bars)


def _num(v: float) -> str:
    return str(int(v)) if v == int(v) else repr(v)


def bar_at(lay: Layout, px: float, py: float, slop: float = 3) -> Optional[Opportunity]:
    """The shot whose bar is at a pixel (the nearest within `slop` pixels across)."""
    best, distance = None, slop + 1.5
    for bar in lay.bars:
        if bar.y + 3 <= py <= bar.y + ROW - 3 and abs(bar.x - px) <= distance:
            best, distance = bar.op, abs(bar.x - px)
    return best
