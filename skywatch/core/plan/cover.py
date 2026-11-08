"""
Combinations of passes (組み合わせ（全域を最短で）): the viewer's `coverPlan`
(imaging-plan.ts), ported line for line.

Going through the opportunities in time order, a pass is taken when its
scenes, placed over what is still uncovered, add at least `min_gain` of an
area, until `goal` of it is covered; a point needs one pass. Satellites are
shared between targets (`minInterval`, `maxPerDay`), more important targets
are planned first, and points close together on a pass can share a scene.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence

from .access import Opportunity, PlanTarget, best_scene, look_at
from .orbit import R, track_rate
from .satellites import SatelliteSpec, platform_key

#: Seconds apart (ms) that two modes' opportunities of one satellite and target are one pass.
SAME_PASS = 120000


@dataclass
class CoverPlan:
    """One target's combination of passes."""
    target: int
    steps: list = field(default_factory=list)
    coverage: float = 0.0
    #: When the goal is reached; None when the passes found do not reach it.
    done: Optional[int] = None
    #: Passes skipped because the satellite was busy (its interval or daily limit).
    blocked: int = 0


def _mid(o: Opportunity) -> float:
    return (o.window[0] + o.window[1]) / 2


def shared_scene(op: Opportunity, candidates: list, sat: SatelliteSpec):
    """The points one scene of `op`'s satellite takes together with it, and where across the track the scene goes."""
    width = sat.swath / R
    x0 = _mid(op)
    span = ((sat.length * sat.scenesPerPass) / R / track_rate(op.orbitRadius)) * 1000
    near = [op, *(c for c in candidates if c.time >= op.time and c.time - op.time <= span and abs(_mid(c) - x0) <= width)]
    best = [op]
    for left in (x for x in map(_mid, near) if x0 - width <= x <= x0):
        inside = [o for o in near if left - 1e-12 <= _mid(o) <= left + width + 1e-12]
        if len(inside) > len(best):
            best = inside
    xs = [_mid(o) for o in best]
    mid = (min(xs) + max(xs)) / 2
    return best, (mid - width / 2, mid + width / 2)


def _best_mode(op: Opportunity, ordered: list, i: int, sats: Sequence[SatelliteSpec], weighed: set, cov: Optional[bytearray], need: int) -> Opportunity:
    key = platform_key(sats[op.satellite])
    siblings = [op]
    j = i + 1
    while j < len(ordered) and ordered[j].time - op.time <= SAME_PASS:
        o = ordered[j]
        if o.target == op.target and o.satellite != op.satellite and platform_key(sats[o.satellite]) == key and id(o) not in weighed:
            siblings.append(o)
        j += 1
    if len(siblings) == 1:
        return op
    for o in siblings:
        weighed.add(id(o))

    def resolution(o: Opportunity) -> float:
        r = sats[o.satellite].resolution
        return math.inf if r is None else r

    by_resolution = sorted(siblings, key=resolution)
    if cov is None:
        return by_resolution[0]

    def taken(o: Opportunity) -> int:
        sat = sats[o.satellite]
        if o.cells is None or o.cellsAlong is None:
            return -1
        scene = best_scene(_floats(o.cells), _floats(o.cellsAlong), sat.swath / R, sat.length / R, sat.scenesPerPass, lambda c: not cov[c])
        return scene.count if scene else -1

    best = by_resolution[0]
    most = -math.inf
    for o in by_resolution:
        n = taken(o)
        if n >= max(1, need):
            return o
        if n > most:
            best, most = o, n
    return best




def _floats(array) -> list:
    """A float32 array's values as Python floats (as JavaScript reads a Float32Array)."""
    return array.tolist()


def cover_plan(opportunities: Sequence[Opportunity], targets: Sequence[PlanTarget], sats: Sequence[SatelliteSpec],
               goal: float = 0.95, min_gain: float = 0.02, share: bool = True) -> list[CoverPlan]:
    """Strings passes of any satellite together to cover the targets, earliest first (see the module)."""
    plans = [CoverPlan(target=ti) for ti in range(len(targets))]
    covered = [bytearray(len(t.cells or [])) for t in targets]
    totals = [0] * len(targets)
    platforms: dict[str, list[int]] = {}
    shots = []
    for sat in sats:
        shots.append(platforms.setdefault(platform_key(sat), []))

    def busy(s: int, time: int) -> bool:
        sat = sats[s]
        taken = shots[s]
        if sat.minInterval > 0 and any(abs(time - t) < sat.minInterval * 60000 for t in taken):
            return True
        if sat.maxPerDay <= 0:
            return False
        near = [t for t in taken if abs(time - t) < 86400000]
        starts = [time, *(t for t in near if time - 86400000 < t <= time)]
        return any(sum(1 for t in near if start <= t < start + 86400000) + 1 > sat.maxPerDay for start in starts)

    ordered = sorted(opportunities, key=lambda o: o.time)
    floats: dict[int, tuple] = {}

    def cells_of(o: Opportunity):
        got = floats.get(id(o))
        if got is None:
            got = floats[id(o)] = (_floats(o.cells), _floats(o.cellsAlong))
        return got

    shot = 0

    def is_point(o: Opportunity) -> bool:
        return not len(covered[o.target]) and o.width == 0

    def rank(t: int) -> float:
        p = targets[t].priority if t < len(targets) else None
        return -math.inf if p is None else p

    tiers = sorted(set(rank(t) for t in range(len(targets))), reverse=True)
    weighed: set = set()
    for tier in tiers:
        for i, first in enumerate(ordered):
            if rank(first.target) != tier or id(first) in weighed:
                continue
            plan = plans[first.target] if first.target < len(plans) else None
            if plan is None or plan.done is not None:
                continue
            n = len(covered[first.target])
            op = _best_mode(first, ordered, i, sats, weighed, covered[first.target] if n else None, math.ceil(goal * n - totals[first.target] - 1e-9))
            sat = sats[op.satellite] if op.satellite < len(sats) else None
            if not n:
                if busy(op.satellite, op.time):
                    plan.blocked += 1
                    continue
                shots[op.satellite].append(op.time)
                shot += 1
                others: list[Opportunity] = []
                if share and sat and is_point(op):
                    j = i + 1
                    while j < len(ordered) and ordered[j].time - op.time < 600000:
                        o = ordered[j]
                        if o.satellite == op.satellite and o.target != op.target and is_point(o) and plans[o.target].done is None and not any(x.target == o.target for x in others):
                            others.append(o)
                        j += 1
                members, window = shared_scene(op, others, sat) if others else ([op], op.window)
                times = [m.time for m in members]
                scene = (min(times), max(times)) if len(members) > 1 else None
                for m in members:
                    p = plans[m.target]
                    extra = {}
                    if len(members) > 1:
                        mid = (window[0] + window[1]) / 2
                        extra = dict(window=window, scene=scene, **look_at(mid, m.orbitRadius), side="left" if mid > 0 else "right")
                    p.steps.append(replace(m, **extra, gain=1, cumulative=1, shot=shot, shotTargets=len(members)))
                    p.coverage = 1
                    p.done = m.time
                continue
            if op.cells is None or op.cellsAlong is None or sat is None:
                continue
            cov = covered[op.target]
            across, along = cells_of(op)
            best = best_scene(across, along, sat.swath / R, sat.length / R, sat.scenesPerPass, lambda c: not cov[c])
            if not best or best.count / n < min(min_gain, goal - totals[op.target] / n - 1e-9):
                continue
            if busy(op.satellite, op.time):
                plan.blocked += 1
                continue
            a, b = best.window
            c, d = best.along
            gain = 0
            for k, x in enumerate(across):
                y = along[k]
                if not cov[k] and a <= x <= b and c <= y <= d:
                    cov[k] = 1
                    gain += 1
            shots[op.satellite].append(op.time)
            shot += 1
            totals[op.target] += gain
            total = totals[op.target] / n
            mid = (a + b) / 2
            plan.steps.append(replace(op, window=best.window, along=best.along, **look_at(mid, op.orbitRadius), side="left" if mid > 0 else "right",
                                      gain=gain / n, cumulative=total, shot=shot, shotTargets=1))
            plan.coverage = total
            if total >= goal:
                plan.done = op.time
    if len(tiers) > 1:
        order = sorted(set(s.shot for p in plans for s in p.steps))
        times = {}
        for p in plans:
            for s in p.steps:
                times[s.shot] = s.time
        order.sort(key=lambda n: (times[n], n))
        renumber = {n: i + 1 for i, n in enumerate(order)}
        for p in plans:
            for s in p.steps:
                s.shot = renumber[s.shot]
    return plans
