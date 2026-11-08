"""
What a pass looks like on the ground, and scenes moved by hand: the viewer's
`passShapes`, `trackPlace`, `shiftScene`, `scenesTaken`, `subSatellitePoint`
and `groundStateAt` (imaging-plan.ts). Coordinates are longitude, latitude
(WGS84 degrees).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional, Sequence

from .access import Opportunity, PlanTarget, framed, look_at, reach_of, satrec_of
from .orbit import DEG, Orbit, R, eci_to_geodetic, ecef_of, frame_of, ground_range, lon_lat_of, norm, point_at, track_angles, track_rate, unit
from .satellites import SatelliteSpec, side_for


@dataclass
class PassShapes:
    #: The sub-satellite track around the moment.
    track: list
    #: The edges of what the satellite can reach (its largest off-nadir angle), one line per side it can look to.
    reach: list
    #: The scenes the pass takes (closed rings).
    scenes: list
    #: Where the satellite is at the moment.
    position: tuple


def _sub_point(st) -> tuple:
    lon, lat, _ = eci_to_geodetic(st.eci, st.gmst)
    return (lon * DEG, lat * DEG)


def _orbit(sat: SatelliteSpec) -> Optional[Orbit]:
    orbit = satrec_of(sat)
    return None if isinstance(orbit, str) else orbit


def pass_shapes(sat: SatelliteSpec, target: PlanTarget, op: Opportunity, minutes: int = 5) -> Optional[PassShapes]:
    """The ground track, reach and scenes of an opportunity (`minutes` before and after it); None when its TLE does not read."""
    orbit = _orbit(sat)
    if orbit is None:
        return None
    track = []
    sides = ["left", "right"] if sat.lookSide == "both" else [sat.lookSide]
    reach: list = [[] for _ in sides]
    for s in range(-minutes * 60, minutes * 60 + 1, 10):
        st = orbit.state(op.time + s * 1000)
        if st is None:
            continue
        track.append(_sub_point(st))
        f = frame_of(st.r, st.v)
        lam = ground_range(sat.maxOffNadir, norm(st.r)) / R
        for i, side in enumerate(sides):
            reach[i].append(lon_lat_of(point_at(f, lam if side == "left" else -lam, 0)))
    st = orbit.state(op.time)
    if st is None:
        return None
    frame = frame_of(st.r, st.v)
    scene_length = sat.length / R
    a, b = op.window
    if op.scene:
        rate = track_rate(norm(st.r))
        from0, to0 = framed((op.scene[0] - op.time) / 1000 * rate, (op.scene[1] - op.time) / 1000 * rate, scene_length, sat.scenesPerPass)
    else:
        from0, to0 = op.along
    frames = max(1, math.floor((to0 - from0) / scene_length + 0.5))
    scenes = []
    for k in range(frames):
        start = from0 + k * scene_length
        end = start + scene_length
        ring = []
        for x, y0, y1 in ((a, start, end), (b, end, start)):
            # Each edge in a few pieces, so the scenes follow the curve of the track.
            ring.extend(lon_lat_of(point_at(frame, x, y0 + (y1 - y0) * i / 4)) for i in range(5))
        ring.append(ring[0])
        scenes.append(ring)
    return PassShapes(track, reach, scenes, _sub_point(st))


def along_of(op: Opportunity, sat: SatelliteSpec) -> tuple:
    """Where an opportunity's scenes are along the track (radians); for a shared scene, from its first point to its last."""
    if not op.scene:
        return op.along
    rate = track_rate(op.orbitRadius)
    return framed((op.scene[0] - op.time) / 1000 * rate, (op.scene[1] - op.time) / 1000 * rate, sat.length / R, sat.scenesPerPass)


def track_place(sat: SatelliteSpec, time: float, lon_lat: Sequence[float]) -> Optional[tuple]:
    """Where a place is seen from a satellite's orbit at `time`: radians across the track (left positive) and along it."""
    orbit = _orbit(sat)
    st = orbit.state(time) if orbit else None
    return track_angles(unit(ecef_of(lon_lat)), frame_of(st.r, st.v)) if st else None


def shift_scene(op: Opportunity, sat: SatelliteSpec, target: PlanTarget, dx: float, dy: float,
                max_off_nadir: Optional[float] = None, min_incidence: Optional[float] = None,
                max_incidence: Optional[float] = None, side: Optional[str] = None) -> Opportunity:
    """
    An opportunity with its scenes moved `dx` radians across the track and `dy`
    along it (dragged on the map), kept where the satellite can look, with its
    angles and coverage of `target` worked out again.
    """
    limit = min(sat.maxOffNadir, math.inf if max_off_nadir is None else max_off_nadir)
    near, far = reach_of(sat, limit, op.orbitRadius, min_incidence, max_incidence)
    look_side = side_for(sat, side) or sat.lookSide
    half = (op.window[1] - op.window[0]) / 2
    mid = (op.window[0] + op.window[1]) / 2 + dx
    sign = (1 if mid >= 0 else -1) if look_side == "both" else (1 if look_side == "left" else -1)
    mid = sign * min(far, max(near, sign * mid))
    base = along_of(op, sat)
    along = (base[0] + dy, base[1] + dy)
    window = (mid - half, mid + half)

    def inside(x: float, y: float) -> bool:
        return window[0] <= x <= window[1] and along[0] <= y <= along[1]

    if op.cells is not None and op.cellsAlong is not None:
        xs, ys = op.cells.tolist(), op.cellsAlong.tolist()
        n = sum(1 for x, y in zip(xs, ys) if not math.isnan(x) and inside(x, y))
        coverage = n / len(xs)
    else:
        at = track_place(sat, op.time, target.center)
        coverage = 1 if at and inside(*at) else 0
    return replace(op, scene=None, window=window, along=along, **look_at(mid, op.orbitRadius), side="left" if mid > 0 else "right",
                   coverage=coverage, adjusted=True)


def scenes_taken(op: Opportunity, sat: SatelliteSpec) -> int:
    """How many scenes an opportunity takes, one after another along the pass."""
    return max(1, math.floor((op.along[1] - op.along[0]) * R / sat.length + 0.5))


def sub_satellite_point(sat: SatelliteSpec, ms: float) -> Optional[tuple]:
    """Where a satellite is at a moment (longitude, latitude)."""
    orbit = _orbit(sat)
    st = orbit.state(ms) if orbit else None
    return _sub_point(st) if st else None


@dataclass
class GroundState:
    lon_lat: tuple
    #: Height above the ellipsoid, km.
    height: float
    ascending: bool
    #: The points `off_nadirs` degrees across the track (left positive).
    across: list


def ground_state_at(orbit: Orbit, ms: float, off_nadirs: Sequence[float]) -> Optional[GroundState]:
    """The satellite at `ms`, with the points `off_nadirs` degrees across its track; None when SGP4 fails."""
    st = orbit.state(ms)
    if st is None:
        return None
    lon, lat, height = eci_to_geodetic(st.eci, st.gmst)
    f = frame_of(st.r, st.v)
    distance = norm(st.r)
    across = [lon_lat_of(point_at(f, math.copysign(1, a) * ground_range(abs(a), distance) / R if a else 0.0, 0)) for a in off_nadirs]
    return GroundState((lon * DEG, lat * DEG), height, st.north, across)
