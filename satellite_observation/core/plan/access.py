"""
When can satellites take a picture of a target: the viewer's `planAccess`
(imaging-plan.ts), ported line for line so the same input gives the same
opportunities.

The orbit is sampled every `step` seconds (60 by default); a group of
nearby targets only looks at the samples when the satellite may be above
its horizon; each pass's closest moment (the smallest off-nadir angle) is
found on the Hermite curve between samples to 50 ms; the pass is an
opportunity when the angle, side, incidence and daylight allow.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

import numpy as np

from .orbit import (
    DEG, POLAR_RADIUS, R, Orbit, azimuth_of, dot, ecef_of, frame_of, ground_range,
    lon_lat_of, norm, off_nadir_for, sub, sun_elevation, track_angles, unit, up_of,
)
from .satellites import GroundStation, SatelliteSpec, side_for

#: Slack (radians) on the horizon test of a group of targets.
HORIZON_MARGIN = 0.5 * (math.pi / 180)
#: Size (degrees) of the cells that group targets for the horizon test.
GROUP_CELL = 4
#: How long after a shot to look for a ground station, and the step of that search.
DOWNLINK_WINDOW = 86400000
DOWNLINK_STEP = 30


@dataclass
class PlanTarget:
    """A place or area to plan for (longitude, latitude, WGS 84)."""
    label: str
    #: Where the satellite is aimed: a point, or the middle of an area.
    center: tuple
    #: Points of the area's outline (for how wide it is); empty for a point.
    points: list = field(default_factory=list)
    #: Points spread evenly inside an area, each an equal share of it; None or empty for a point.
    cells: Optional[list] = None
    #: How much it matters (larger first); None counts as the least.
    priority: Optional[float] = None
    priority_text: Optional[str] = None
    #: For a moving target: the radius (km) of where it may be, and how long (ms) after its last report.
    uncertainty: Optional[float] = None
    guessed: Optional[float] = None


@dataclass
class PlanOptions:
    """The period (ms) and conditions of a plan."""
    start: float
    end: float
    #: A tighter limit on the off-nadir angle than the satellites' own; None for none.
    maxOffNadir: Optional[float] = None
    #: `optical`: satellites that are not SAR only in daylight. `none`: day and night.
    daylight: str = "optical"
    minSunElevation: float = 10
    #: Seconds between the coarse samples of each orbit.
    step: float = 60
    #: When the plan is made (ms), for lead times; None: not checked.
    now: Optional[float] = None
    minIncidence: Optional[float] = None
    maxIncidence: Optional[float] = None
    #: `ascending` (north) or `descending`; None for both.
    pass_: Optional[str] = None
    #: `left` or `right`; None for both.
    side: Optional[str] = None
    stations: list = field(default_factory=list)


@dataclass
class Opportunity:
    """One chance to take a picture (as the viewer's Opportunity)."""
    satellite: int
    target: int
    #: The moment of closest approach, ms.
    time: int
    offNadir: float
    incidence: float
    elevation: float
    side: str
    ascending: bool
    sunElevation: float
    range: float
    width: float
    strips: int
    length: float
    frames: int
    coverage: float
    #: Where the scenes are across the track (radians, left positive) and along it (radians, forward positive).
    window: tuple
    along: tuple
    tleAge: float
    azimuth: float
    satEcef: tuple
    orbitRadius: float
    #: For an area: each cell's angle across the track (NaN where it cannot be seen) and along it (float32, as the viewer).
    cells: Optional[np.ndarray] = None
    cellsAlong: Optional[np.ndarray] = None
    gain: Optional[float] = None
    cumulative: Optional[float] = None
    shot: Optional[int] = None
    shotTargets: Optional[int] = None
    #: For a scene shared by several points: from and to along the track, as moments (ms).
    scene: Optional[tuple] = None
    adjusted: bool = False
    #: (time ms, station index) when the satellite next comes over a ground station.
    downlink: Optional[tuple] = None
    cloud: Optional[float] = None
    pair: Any = None


@dataclass
class PlanResult:
    opportunities: list
    problems: list
    #: Opportunities left out because they are too soon to order.
    tooSoon: int = 0


def satrec_of(sat: SatelliteSpec):
    """The orbit of a satellite; a problem (text) when its TLE does not read."""
    try:
        orbit = Orbit(sat.tle1, sat.tle2)
    except Exception:  # noqa: BLE001 (any unreadable TLE)
        return f"{sat.name}: TLE の読み込み不可"
    if not orbit.ok:
        return f"{sat.name}: TLE の読み込み不可"
    return orbit


def tle_epoch(sat: SatelliteSpec) -> float:
    """The epoch of a satellite's TLE (ms); NaN when it does not read."""
    orbit = satrec_of(sat)
    return math.nan if isinstance(orbit, str) else orbit.epoch


@dataclass
class _Prepared:
    r: tuple
    up: tuple
    points: list
    cells: list
    radius: float


def _prepare(t: PlanTarget) -> _Prepared:
    r = ecef_of(t.center)
    middle = unit(r)
    points = [unit(ecef_of(p)) for p in t.points]
    cells = [unit(ecef_of(p)) for p in (t.cells or [])]
    radius = 0.0
    for p in [*points, *cells]:
        radius = max(radius, math.acos(min(1.0, dot(p, middle))))
    return _Prepared(r, up_of(t.center), points, cells, radius)


def _group_targets(prepared: list[_Prepared]):
    keys: dict[str, list[int]] = {}
    for i, t in enumerate(prepared):
        lon, lat = lon_lat_of(t.r)
        keys.setdefault(f"{math.floor(lat / GROUP_CELL)},{math.floor(lon / GROUP_CELL)}", []).append(i)
    group_of = [0] * len(prepared)
    groups = []
    for g, members in enumerate(keys.values()):
        total = [0.0, 0.0, 0.0]
        for i in members:
            u = unit(prepared[i].r)
            for j in range(3):
                total[j] += u[j]
            group_of[i] = g
        direction = unit(total) if norm(total) > 0 else unit(prepared[members[0]].r)
        radius = 0.0
        for i in members:
            radius = max(radius, math.acos(max(-1.0, min(1.0, dot(unit(prepared[i].r), direction)))) + prepared[i].radius)
        groups.append((direction, radius))
    return groups, group_of


def _look(r, t: _Prepared):
    d = sub(t.r, r)
    rng = norm(d)
    nadir = unit((-r[0], -r[1], -r[2]))
    cos_off = dot(nadir, d) / rng
    elevation = math.asin(-dot(d, t.up) / rng) * DEG
    return cos_off, elevation, rng, d


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def plan_access(sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget], options: PlanOptions,
                on_progress: Optional[Callable[[float], None]] = None) -> PlanResult:
    """
    Every opportunity of the satellites over the targets between
    `options.start` and `options.end`, in time order. `on_progress` is told
    the share done (0–1) after each satellite (and may raise to cancel).
    """
    problems: list[str] = []
    too_soon = 0
    opportunities: list[Opportunity] = []
    step = (options.step or 60) * 1000
    count = max(2, math.ceil((options.end - options.start) / step) + 1)
    prepared = [_prepare(t) for t in targets]
    groups, group_of = _group_targets(prepared)
    times = options.start + np.arange(count, dtype=float) * step
    smooth = step <= 120000
    h = step / 1000

    def progress(s: int) -> None:
        if on_progress:
            on_progress((s + 1) / len(sats))

    sampled = ""
    spans: Optional[list] = None
    xs = ys = zs = rs = None
    vxs = vys = vzs = None
    nx = ny = nz = None
    xl = yl = zl = vxl = vyl = vzl = None

    def between(ms: float):
        u = (ms - options.start) / step
        k = min(count - 2, max(0, math.floor(u)))
        if math.isnan(xl[k]) or math.isnan(xl[k + 1]):
            return None
        f = u - k
        h00 = (1 + 2 * f) * (1 - f) * (1 - f)
        h10 = f * (1 - f) * (1 - f) * h
        h01 = f * f * (3 - 2 * f)
        h11 = f * f * (f - 1) * h
        return (
            h00 * xl[k] + h10 * vxl[k] + h01 * xl[k + 1] + h11 * vxl[k + 1],
            h00 * yl[k] + h10 * vyl[k] + h01 * yl[k + 1] + h11 * vyl[k + 1],
            h00 * zl[k] + h10 * vzl[k] + h01 * zl[k + 1] + h11 * vzl[k + 1],
        )

    for s, sat in enumerate(sats):
        orbit = satrec_of(sat)
        if isinstance(orbit, str):
            problems.append(orbit)
            progress(s)
            continue
        limit = min(sat.maxOffNadir, options.maxOffNadir if options.maxOffNadir is not None else math.inf)
        if limit < sat.minOffNadir or not side_for(sat, options.side):
            progress(s)
            continue
        epoch = orbit.epoch
        key = f"{sat.tle1}\n{sat.tle2}"
        if key != sampled:
            sampled = key
            r, v = orbit.states(times)
            xs, ys, zs = r[:, 0], r[:, 1], r[:, 2]
            vxs, vys, vzs = v[:, 0], v[:, 1], v[:, 2]
            rs = np.sqrt(xs * xs + ys * ys + zs * zs)
            cx = ys * vzs - zs * vys
            cy = zs * vxs - xs * vzs
            cz = xs * vys - ys * vxs
            cn = np.sqrt(cx * cx + cy * cy + cz * cz)
            nx, ny, nz = cx / cn, cy / cn, cz / cn
            xl, yl, zl = xs.tolist(), ys.tolist(), zs.tolist()
            vxl, vyl, vzl = vxs.tolist(), vys.tolist(), vzs.tolist()
            valid = ~np.isnan(xs)
            if not valid.any():
                problems.append(f"{sat.name}: 軌道の計算不可（TLE が古すぎるか、落下済みの可能性あり）")
                spans = None
            else:
                above = rs > POLAR_RADIUS
                reach = float(np.max(np.arccos(POLAR_RADIUS / rs[above]))) if above.any() else 0.0
                spans = []
                for direction, radius in groups:
                    cosine = math.cos(min(math.pi, reach + radius + HORIZON_MARGIN))
                    gx, gy, gz = direction
                    near = (xs * gx + ys * gy + zs * gz) > cosine * rs  # NaN compares False
                    spans.append(_runs(near, count))
        if spans is None:
            progress(s)
            continue

        for ti, t in enumerate(prepared):
            tx, ty, tz = t.r
            ux, uy, uz = t.up
            level = tx * ux + ty * uy + tz * uz
            span = spans[group_of[ti]]
            for i in range(0, len(span), 2):
                a0, b0 = span[i], span[i + 1]
                ks = np.arange(a0 - 1, b0 + 2)
                inside = (ks >= 0) & (ks < count)
                kk = np.clip(ks, 0, count - 1)
                x, y, z = xs[kk], ys[kk], zs[kk]
                up = (x * ux + y * uy + z * uz) > level
                dx, dy, dz = tx - x, ty - y, tz - z
                with np.errstate(invalid="ignore", divide="ignore"):
                    c = -(x * dx + y * dy + z * dz) / (rs[kk] * np.sqrt(dx * dx + dy * dy + dz * dz))
                c = np.where(inside & up, c, -2.0)
                here, prev, nxt = c[1:-1], c[:-2], c[2:]
                peaks = np.nonzero((here > -2) & (here >= prev) & (here > nxt))[0]
                for j in peaks.tolist():
                    k = a0 + j
                    rk = (xl[k], yl[k], zl[k])
                    n = (float(nx[k]), float(ny[k]), float(nz[k]))
                    across = abs(math.asin(max(-1.0, min(1.0, dot(unit(t.r), n)))))
                    if off_nadir_for(max(0.0, across - t.radius), norm(rk)) > limit + 1:
                        continue
                    a = options.start + (k - 1) * step
                    b = options.start + (k + 1) * step
                    if smooth and 0 < k < count - 1 and not math.isnan(xl[k - 1]) and not math.isnan(xl[k + 1]):
                        position = between
                    else:
                        def position(ms: float, orbit=orbit):
                            st = orbit.state(ms)
                            return st.r if st else None
                    found = _refine(position, t, a, b)
                    if found is None:
                        continue
                    op = _judge(sat, orbit, s, ti, t, found, limit, options, epoch)
                    if op and sat.leadTime > 0 and options.now is not None and op.time - sat.leadTime * 3600000 < options.now:
                        too_soon += 1
                    elif op:
                        opportunities.append(op)
        if options.stations:
            mine = [op for op in opportunities if op.satellite == s]
            if mine:
                add_downlinks(orbit, mine, options.stations)
        progress(s)
    opportunities.sort(key=lambda o: o.time)
    return PlanResult(opportunities, problems, too_soon)


def _runs(near: np.ndarray, count: int) -> list[int]:
    """The sample ranges (from, to, …) where a group may be above the horizon, with a sample either side (as the viewer)."""
    out: list[int] = []
    flags = near.tolist()
    k = 0
    while k < count:
        if not flags[k]:
            k += 1
            continue
        start = max(0, k - 1)
        to = k + 1
        while to < count and flags[to]:
            to += 1
        last = min(count - 1, to)
        if out and out[-1] >= start - 1:
            out[-1] = last
        else:
            out.extend((start, last))
        k = to + 1
    return out


def _refine(position: Callable[[float], Optional[tuple]], t: _Prepared, a: float, b: float) -> Optional[int]:
    """The moment between `a` and `b` with the smallest off-nadir angle to the target (golden-section search to 50 ms)."""
    g = (math.sqrt(5) - 1) / 2

    def f(ms: float) -> float:
        r = position(ms)
        return _look(r, t)[0] if r else -2

    x1 = b - g * (b - a)
    x2 = a + g * (b - a)
    f1, f2 = f(x1), f(x2)
    while b - a > 50:
        if f1 > f2:
            b = x2
            x2, f2 = x1, f1
            x1 = b - g * (b - a)
            f1 = f(x1)
        else:
            a = x1
            x1, f1 = x2, f2
            x2 = a + g * (b - a)
            f2 = f(x2)
    best = _js_round((a + b) / 2)
    return best if f(best) > -2 else None


def add_downlinks(orbit: Orbit, ops: list[Opportunity], stations: Sequence[GroundStation]) -> None:
    """Tells each opportunity (of one satellite) when it next comes over a station high enough (to within a second)."""
    from .orbit import RAD

    sites = [(ecef_of((g.lon, g.lat)), up_of((g.lon, g.lat)), math.sin(g.min_elevation * RAD)) for g in stations]

    def in_view(ms: float) -> int:
        st = orbit.state(ms)
        if not st:
            return -1
        for i, (r, up, sn) in enumerate(sites):
            d = sub(st.r, r)
            if dot(d, up) / norm(d) >= sn:
                return i
        return -1

    step = DOWNLINK_STEP * 1000
    seen: dict[float, int] = {}

    def sample(ms: float) -> int:
        if ms not in seen:
            seen[ms] = in_view(ms)
        return seen[ms]

    for op in sorted(ops, key=lambda o: o.time):
        if sample(op.time) >= 0:
            op.downlink = (op.time, sample(op.time))
            continue
        first = math.ceil(op.time / step) * step
        t = first
        while t <= op.time + DOWNLINK_WINDOW:
            station = sample(t)
            if station >= 0:
                a = max(op.time, t - step)
                b = t
                while b - a > 1000:
                    m = _js_round((a + b) / 2)
                    if in_view(m) >= 0:
                        b = m
                    else:
                        a = m
                v = in_view(b)
                op.downlink = (b, v if v >= 0 else station)
                break
            t += step


# --- Scenes -------------------------------------------------------------------------


def best_window(across: Sequence[float], width: float, open_: Optional[Callable[[int], bool]] = None):
    """Where to put `width` radians across the track to take the most cells: (count, (from, to)), or None."""
    xs = sorted(x for i, x in enumerate(across) if not math.isnan(x) and (open_ is None or open_(i)))
    if not xs:
        return None
    best = 0
    start = 0
    j = 0
    for i in range(len(xs)):
        while xs[i] - xs[j] > width:
            j += 1
        if i - j + 1 > best:
            best = i - j + 1
            start = j
    mid = (xs[start] + xs[start + best - 1]) / 2
    return best, (mid - width / 2, mid + width / 2)


def framed(start: float, end: float, length: float, most: int) -> tuple:
    """Whole scene lengths along the track over `start`–`end`: as many as it takes, at most `most`, centred on it."""
    k = min(most, max(1, math.ceil((end - start) / length - 1e-6)))
    mid = (start + end) / 2
    return (mid - k * length / 2, mid + k * length / 2)


@dataclass
class Scene:
    count: int
    window: tuple
    along: tuple
    members: list


def best_scene(across: Sequence[float], along: Sequence[float], width: float, length: float, most: int,
               open_: Optional[Callable[[int], bool]] = None) -> Optional[Scene]:
    """Where to put one pass's scenes (`width` across, up to `most` of `length` along) to take the most cells."""
    idx = [i for i in range(len(across)) if not math.isnan(across[i]) and (open_ is None or open_(i))]
    if not idx:
        return None
    idx.sort(key=lambda i: across[i])
    reach = length * most
    best: list[int] = []
    for j in range(len(idx)):
        if j > 0 and across[idx[j]] == across[idx[j - 1]]:
            continue
        inside = []
        k = j
        while k < len(idx) and across[idx[k]] - across[idx[j]] <= width:
            inside.append(idx[k])
            k += 1
        if len(inside) <= len(best):
            continue
        inside.sort(key=lambda i: along[i])
        p = 0
        for q in range(len(inside)):
            while along[inside[q]] - along[inside[p]] > reach:
                p += 1
            if q - p + 1 > len(best):
                best = inside[p:q + 1]
    xs = [across[i] for i in best]
    ys = [along[i] for i in best]
    mid = (min(xs) + max(xs)) / 2
    return Scene(len(best), (mid - width / 2, mid + width / 2), framed(min(ys), max(ys), length, most), best)


def look_at(x: float, distance: float) -> dict:
    """Looking `x` radians across the track from `distance` km: offNadir, incidence, elevation (degrees), range (km)."""
    lam = abs(x)
    off = off_nadir_for(lam, distance)
    incidence = off + lam * DEG
    return {"offNadir": off, "incidence": incidence, "elevation": 90 - incidence, "range": math.hypot(R * math.sin(lam), distance - R * math.cos(lam))}


def central_angle_for(incidence: float, distance: float) -> float:
    from .orbit import RAD

    off = math.asin(min(1.0, (R / distance) * math.sin(incidence * RAD)))
    return max(0.0, incidence * RAD - off)


def reach_of(sat: SatelliteSpec, limit: float, distance: float, min_incidence: Optional[float], max_incidence: Optional[float]) -> tuple:
    """How far across the track (radians, either side) the satellite can look: (near, far)."""
    near = ground_range(sat.minOffNadir, distance) / R if sat.minOffNadir > 0 else 0.0
    far = ground_range(limit, distance) / R
    if min_incidence is not None:
        near = max(near, central_angle_for(min_incidence, distance))
    if max_incidence is not None:
        far = min(far, central_angle_for(max_incidence, distance))
    return near, far


def frames_for(length: float, scene_length: float) -> int:
    return max(1, math.ceil(length / scene_length - 1e-6)) if scene_length > 0 else 1


def _spread(r, v, t: _Prepared):
    f = frame_of(r, v)
    angles = [track_angles(p, f) for p in [unit(t.r), *t.points, *t.cells]]
    across = [a[0] for a in angles]
    along = [a[1] for a in angles]
    x0, x1 = min(across), max(across)
    return f, x0, x1, min(along), max(along), (x1 - x0) * R


def _judge(sat: SatelliteSpec, orbit: Orbit, s: int, ti: int, t: _Prepared, time: int, limit: float,
           options: PlanOptions, epoch: float) -> Optional[Opportunity]:
    st = orbit.state(time)
    if options.pass_ and st.north != (options.pass_ == "ascending"):
        return None
    look_side = side_for(sat, options.side)
    if not look_side:
        return None
    center = lon_lat_of(t.r)
    sun = sun_elevation(center, time)
    if options.daylight == "optical" and not sat.sar and sun < options.minSunElevation:
        return None
    f = frame_of(st.r, st.v)
    distance = norm(st.r)
    _, _, _, y0, y1, width = _spread(st.r, st.v, t)
    along = (y1 - y0) * R
    sw = sat.swath / R
    sl = sat.length / R
    common = dict(
        satellite=s, target=ti, time=time, ascending=st.north, sunElevation=sun, width=width,
        strips=max(1, math.ceil(width / sat.swath - 1e-9)) if width > 0 else 1,
        length=along, frames=frames_for(along, sat.length), tleAge=(time - epoch) / 86400000,
        orbitRadius=distance, azimuth=azimuth_of(center, sub(st.r, t.r)), satEcef=tuple(st.r),
    )
    if not t.cells:
        cos_off, elevation, rng, d = _look(st.r, t)
        if elevation <= 0:
            return None
        off = math.acos(min(1.0, cos_off)) * DEG
        if off > limit or off < sat.minOffNadir:
            return None
        incidence = 90 - elevation
        if (options.minIncidence is not None and incidence < options.minIncidence) or (options.maxIncidence is not None and incidence > options.maxIncidence):
            return None
        side = "left" if dot(d, f.n) > 0 else "right"
        if look_side != "both" and look_side != side:
            return None
        x = track_angles(unit(t.r), f)[0]
        return Opportunity(**common, offNadir=off, incidence=incidence, elevation=elevation, side=side, range=rng,
                           coverage=1, window=(x - sw / 2, x + sw / 2), along=framed(y0, y1, sl, sat.scenesPerPass))

    near, far = reach_of(sat, limit, distance, options.minIncidence, options.maxIncidence)
    across = np.empty(len(t.cells), dtype=np.float32)
    alongs = np.empty(len(t.cells), dtype=np.float32)
    for i, p in enumerate(t.cells):
        x, y = track_angles(p, f)
        ok = near <= abs(x) <= far and (look_side == "both" or (x > 0) == (look_side == "left"))
        across[i] = x if ok else math.nan
        alongs[i] = y
    best = best_scene(across.tolist(), alongs.tolist(), sw, sl, sat.scenesPerPass)
    if not best:
        return None
    mid = (best.window[0] + best.window[1]) / 2
    return Opportunity(**common, **look_at(mid, distance), side="left" if mid > 0 else "right",
                       coverage=best.count / len(t.cells), window=best.window, along=best.along, cells=across, cellsAlong=alongs)
