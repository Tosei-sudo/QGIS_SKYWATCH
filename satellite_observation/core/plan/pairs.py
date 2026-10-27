"""
Pairs of shots of one target, as the viewer (imaging-plan-pairs.ts): stereo
pairs (two looks from directions far enough apart to measure heights) and
repeat pairs for SAR interferometry (the same satellite or series coming back
on the same track, looking the same way at the same angle).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from .access import Opportunity, PlanTarget
from .orbit import ecef_of
from .satellites import SatelliteSpec

#: The most pairs one target gives.
MAX_PER_TARGET = 200


@dataclass(eq=False)
class PlanPair:
    """Two shots of one target, in time order, and how they relate."""
    a: Opportunity
    b: Opportunity
    #: Angle between the two lines of sight at the target, degrees.
    convergence: float
    #: Base-to-height ratio of the two looks (2 tan(γ/2)).
    bh: float
    days: float
    #: Baseline across the line of sight, metres.
    baseline: float


@dataclass
class StereoOptions:
    #: Convergence angle, degrees (15–45 by default: about B/H 0.26–0.83).
    minAngle: float = 15
    maxAngle: float = 45
    maxDays: float = 7
    #: Only satellites of the same series together.
    sameSeries: bool = True


@dataclass
class RepeatOptions:
    maxIncidence: float = 1
    maxAzimuth: float = 2
    #: The longest perpendicular baseline, metres; None for any.
    maxBaseline: Optional[float] = None
    #: Satellites of the same series count as one; otherwise only the same satellite.
    sameSeries: bool = False


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(a):
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


def relate(a: Opportunity, b: Opportunity, target: PlanTarget) -> PlanPair:
    """How two shots of `target` relate: their convergence angle, B/H, days apart and perpendicular baseline."""
    t = ecef_of(target.center)
    ua = _unit(_sub(a.satEcef, t))
    ub = _unit(_sub(b.satEcef, t))
    convergence = math.acos(max(-1.0, min(1.0, _dot(ua, ub)))) * 180 / math.pi
    d = _sub(b.satEcef, a.satEcef)
    along = _dot(d, ua)
    across = math.sqrt(sum((d[i] - along * ua[i]) ** 2 for i in range(3)))
    first, second = (a, b) if a.time <= b.time else (b, a)
    return PlanPair(first, second, convergence, 2 * math.tan(convergence / 2 * math.pi / 180), abs(b.time - a.time) / 86400000, across * 1000)


def _group_key(sat: SatelliteSpec, same_series: bool) -> str:
    return f"s:{sat.series}" if same_series else f"n:{sat.source}\n{sat.id}"


def stereo_pairs(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget],
                 options: StereoOptions = StereoOptions()) -> list[PlanPair]:
    """
    Stereo pairs: two shots of the same target from directions `minAngle`–
    `maxAngle` apart within `maxDays`, both of the same kind of sensor (and
    series when asked). Earliest second shot first.
    """
    by_target: dict[int, list[Opportunity]] = {}
    for op in ops:
        by_target.setdefault(op.target, []).append(op)
    pairs: list[PlanPair] = []
    for target, items in by_target.items():
        items.sort(key=lambda o: o.time)
        found: list[PlanPair] = []
        for i in range(len(items)):
            if len(found) >= MAX_PER_TARGET:
                break
            for j in range(i + 1, len(items)):
                if len(found) >= MAX_PER_TARGET:
                    break
                a, b = items[i], items[j]
                if b.time - a.time > options.maxDays * 86400000:
                    break
                sa, sb = sats[a.satellite], sats[b.satellite]
                if sa.sar != sb.sar or (options.sameSeries and sa.series != sb.series):
                    continue
                pair = relate(a, b, targets[target])
                if options.minAngle <= pair.convergence <= options.maxAngle:
                    found.append(pair)
        pairs += found
    return sorted(pairs, key=lambda p: (p.b.time, p.a.time))


def _azimuth_gap(x: float, y: float) -> float:
    """The difference of two azimuths, degrees (0–180)."""
    d = math.fmod(abs(x - y), 360)
    return 360 - d if d > 180 else d


def repeat_pairs(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget],
                 options: RepeatOptions = RepeatOptions()) -> list[PlanPair]:
    """
    Repeat pairs for SAR interferometry: for each SAR shot, the next shot of
    the same target by the same satellite (or series) on a pass going the
    same way, looking to the same side at an incidence within `maxIncidence`
    and an azimuth within `maxAzimuth`, with a perpendicular baseline no
    longer than `maxBaseline`. In time order.
    """
    groups: dict[str, list[Opportunity]] = {}
    for op in ops:
        sat = sats[op.satellite]
        if not sat.sar:
            continue
        key = f"{_group_key(sat, options.sameSeries)}|{op.target}|{op.ascending}|{op.side}"
        groups.setdefault(key, []).append(op)
    pairs: list[PlanPair] = []
    for items in groups.values():
        items.sort(key=lambda o: o.time)
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if abs(b.incidence - a.incidence) > options.maxIncidence or _azimuth_gap(a.azimuth, b.azimuth) > options.maxAzimuth:
                    continue
                pair = relate(a, b, targets[a.target])
                if options.maxBaseline is not None and pair.baseline > options.maxBaseline:
                    continue
                pairs.append(pair)
                break
    return sorted(pairs, key=lambda p: (p.a.time, p.b.time))
