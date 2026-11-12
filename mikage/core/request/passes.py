"""
The passes of the day (撮像機会): when each satellite (mode) can take each
request, worked out from the satellites' orbits (TLE) as the imaging plan
does (core/plan/access.py). With them the day's fitting (daily.allocate)
knows which pass takes a request, puts requests close together on one
pass into one shot (連続シーン, core/request/shots.py) and keeps each
satellite's interval between shots (`minInterval`) and its shots a day
(`maxPerDay`) (tsudo, 2026-11-11).

A satellite whose TLE does not read (or has none) is not known here: the
fitting counts requests for it as before.

A request's own conditions narrow its passes: the off-nadir range, the
incidence, the look side, the orbit direction (昇交 / 降交), the time of
day (昼: the sun at least 10° up; 夜: below the horizon; optical
satellites only in daylight in any case) and the window of the day it is
asked for. A satellite's `leadTime` (hours before a shot that it has to be
ordered) leaves out the passes too soon to order, as in the imaging plan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional, Sequence

from ..plan.access import Opportunity, PlanOptions, PlanTarget, plan_access, satrec_of
from ..plan.orbit import Orbit, ecef_of, unit
from ..plan.satellites import SatelliteSpec
from ..plan.targets import MAX_POINTS, spread_out
from .model import ImagingRequest, bbox_of, center_of, day_bounds, in_rings
from .route import why_not

#: About how many points sample an area larger than one shot (fewer than the imaging plan's 600: every request,
#: every satellite of the day). An area that fits in one shot of every satellite that can take it is planned
#: as its middle (with its outline for its reach), which is much quicker.
AREA_CELLS = 60
#: The sun's height (degrees) a pass needs in daylight (as the imaging plan's default).
DAYLIGHT_SUN = 10
ORBIT_DIRS = {"昇交": "ascending", "降交": "descending"}
LOOK_SIDES = {"右": "right", "左": "left"}


class _Stopped(Exception):
    """The work was stopped (`cancelled`): what was found so far is given back, and the caller checks."""


@dataclass
class Passes:
    """Each request's passes, and what is known of the satellites' orbits."""
    #: request id → its opportunities (`satellite` is the index into the satellites given), in time order.
    ops: dict = field(default_factory=dict)
    #: The indices of the satellites whose orbits are known (their TLE reads).
    known: frozenset = frozenset()
    #: The orbits by satellite index.
    orbits: dict = field(default_factory=dict)
    #: request id → the target planned for (where it is, its outline and cells).
    targets: dict = field(default_factory=dict)
    #: request id → the unit vectors of the points that say how far it reaches (the point, or the outline's vertices).
    points: dict = field(default_factory=dict)
    #: Satellites whose TLE did not read (names).
    problems: list = field(default_factory=list)
    #: When it was worked out (ms), for lead times; None when not.
    now: Optional[float] = None

    def of(self, req: ImagingRequest, day: date, sat: Optional[int] = None) -> list[Opportunity]:
        """A request's opportunities on a day (within its window that day), of one satellite when given."""
        first, last = window_of(req, day)
        return [o for o in self.ops.get(req.id, ()) if first <= o.time <= last and (sat is None or o.satellite == sat)]


def window_of(req: ImagingRequest, day: date) -> tuple[float, float]:
    """The part of a JST day a request is asked for (its period's start or end inside the day)."""
    first, last = day_bounds(day)
    start = max(first, req.start) if math.isfinite(req.start) else first
    end = min(last, req.end) if math.isfinite(req.end) else last
    return start, end


def grid_cells(rings: Sequence, most: int) -> list:
    """About `most` points spread evenly inside rings (even-odd), square on the ground; finer while too few fall inside."""
    box = bbox_of(rings)
    if box is None:
        return []
    w, s, e, n = box
    k = math.cos((s + n) / 2 * math.pi / 180)
    width, height = max((e - w) * k, 1e-9), max(n - s, 1e-9)
    step = math.sqrt(width * height / most)
    cells: list = []
    for _ in range(4):
        nx, ny = max(1, math.floor(width / step + 0.5)), max(1, math.floor(height / step + 0.5))
        cells = [(w + (e - w) * (i + 0.5) / nx, s + (n - s) * (j + 0.5) / ny) for i in range(nx) for j in range(ny)]
        cells = [c for c in cells if in_rings(rings, *c)]
        if len(cells) >= most / 4:
            break
        step /= 2
    return spread_out(cells, most * 4)


def size_km(req: ImagingRequest) -> float:
    """The diagonal of a request's box (km)."""
    box = bbox_of(req.rings)
    if box is None:
        return 0.0
    w, s, e, n = box
    k = math.cos(math.radians((s + n) / 2))
    return math.hypot((e - w) * 111.320 * k, (n - s) * 110.574)


def target_of(req: ImagingRequest, shot_km: float = math.inf) -> Optional[PlanTarget]:
    """
    What a request is planned as: a point (its middle), an area no larger
    than `shot_km` (its middle, with its outline), or a larger area (its
    outline and cells); None without a shape.
    """
    mid = center_of(req.rings)
    if mid is None:
        return None
    if req.shape_kind == "点":
        return PlanTarget(req.id, tuple(mid), [])
    outline = spread_out([tuple(p[:2]) for r in req.rings for p in r], MAX_POINTS)
    rings = [[list(p[:2]) for p in r] for r in req.rings]
    if size_km(req) <= shot_km:
        return PlanTarget(req.id, tuple(mid), outline, None, rings=rings)
    cells = grid_cells(req.rings, AREA_CELLS) or [tuple(mid)]
    center = tuple(mid) if in_rings(req.rings, *mid) else cells[len(cells) // 2]
    return PlanTarget(req.id, center, outline, cells, rings=rings)


def reach_points(target: PlanTarget) -> list:
    """The unit vectors that say how far a target reaches: its point, or its outline's vertices."""
    return [unit(ecef_of(p)) for p in (target.points or [target.center])]


def options_key(req: ImagingRequest) -> tuple:
    """The conditions of a request that the orbit search itself takes (requests alike are searched together)."""
    return (req.off_nadir_max, req.incidence_min, req.incidence_max, ORBIT_DIRS.get(req.orbit_dir), LOOK_SIDES.get(req.look_side))


def _keeps(req: ImagingRequest, sat: SatelliteSpec, op: Opportunity, now: Optional[float]) -> bool:
    """Whether a pass suits the request beyond what the search checks: off-nadir floor, time of day, lead time."""
    if req.off_nadir_min is not None and op.offNadir < req.off_nadir_min - 1e-9:
        return False
    if req.time_of_day == "昼" and op.sunElevation < DAYLIGHT_SUN:
        return False
    if req.time_of_day == "夜" and op.sunElevation >= 0:
        return False
    if sat.leadTime > 0 and now is not None and op.time - sat.leadTime * 3600000 < now:
        return False
    return True


def find_passes(requests: Sequence[ImagingRequest], start: float, end: float, sats: Sequence[SatelliteSpec],
                now: Optional[float] = None, cancelled: Optional[Callable[[], bool]] = None) -> Passes:
    """
    The passes of the satellites over the requests between `start` and `end`
    (ms): each request's, by the satellites that can take it (route.why_not),
    within its period and its own conditions.
    """
    out = Passes(now=now)
    tles: dict = {}
    known = set()
    for i, sat in enumerate(sats):
        if not (sat.tle1 or "").strip() or not (sat.tle2 or "").strip():
            continue
        key = (sat.tle1, sat.tle2)
        if key not in tles:
            orbit = satrec_of(sat)
            tles[key] = orbit
            if isinstance(orbit, str) and orbit not in out.problems:
                out.problems.append(orbit)
        if isinstance(tles[key], Orbit):
            known.add(i)
            out.orbits[i] = tles[key]
    out.known = frozenset(known)
    groups: dict = {}
    for req in requests:
        if req.status != "有効" or not math.isfinite(req.start) or not math.isfinite(req.end) or req.end < start or req.start > end:
            continue
        able = [i for i in sorted(known) if why_not(req, sats[i]) is None]
        shot = min((min(sats[i].swath, sats[i].length * max(1, sats[i].scenesPerPass)) for i in able), default=math.inf)
        target = target_of(req, shot)
        if target is None:
            continue
        out.ops[req.id] = []
        if not able:
            continue
        out.targets[req.id] = target
        out.points[req.id] = reach_points(target)
        groups.setdefault(options_key(req), []).append((req, able))

    def progress(_share: float) -> None:
        if cancelled and cancelled():
            raise _Stopped()

    try:
        for key, members in groups.items():
            _search(out, key, members, start, end, sats, now, progress)
    except _Stopped:
        pass
    for ops in out.ops.values():
        ops.sort(key=lambda o: o.time)
    return out


def _search(out: Passes, key: tuple, members: list, start: float, end: float, sats: Sequence[SatelliteSpec],
            now: Optional[float], progress: Callable[[float], None]) -> None:
    """The passes of requests alike (`members`: (request, the satellites that can take it)) into `out`."""
    max_off, inc_min, inc_max, pass_, side = key
    progress(0)
    reqs = [r for r, _ in members]
    used = sorted({i for _, able in members for i in able})
    local = [sats[i] for i in used]
    first = max(start, min(r.start for r in reqs))
    last = min(end, max(r.end for r in reqs))
    options = PlanOptions(first, last, max_off, "optical", DAYLIGHT_SUN, minIncidence=inc_min, maxIncidence=inc_max,
                          pass_=pass_, side=side)
    result = plan_access(local, [out.targets[r.id] for r in reqs], options, progress)
    ables = [set(able) for _, able in members]
    for op in result.opportunities:
        req = reqs[op.target]
        index = used[op.satellite]
        if index not in ables[op.target] or not req.start <= op.time <= req.end or not _keeps(req, sats[index], op, now):
            continue
        op.satellite = index
        out.ops[req.id].append(op)


def day_passes(requests: Sequence[ImagingRequest], day: date, sats: Sequence[SatelliteSpec], now: Optional[float] = None,
               cancelled: Optional[Callable[[], bool]] = None) -> Passes:
    """The passes of one JST day."""
    first, last = day_bounds(day)
    return find_passes(requests, first, last, sats, now, cancelled)
