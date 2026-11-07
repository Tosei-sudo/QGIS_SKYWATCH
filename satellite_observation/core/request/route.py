"""
Which satellites (and so which systems) can take a request, and how many
scenes it needs.

A satellite belongs to a system by the satellite catalog's `system`
attribute when it has one; otherwise by the system's `series` and
`satellites` in `requestExports`.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

from ..plan.satellites import SatelliteSpec
from .model import ImagingRequest, bbox_of, in_rings
from .systems import ExportSystem


def _plain(s: Optional[str]) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _named(names: Sequence[str], *values: Optional[str]) -> bool:
    wanted = {_plain(n) for n in names if _plain(n)}
    return any(_plain(v) in wanted for v in values if _plain(v))


def system_of_satellite(sat: SatelliteSpec, systems: Sequence[ExportSystem]) -> Optional[ExportSystem]:
    """The system a satellite belongs to (the catalog's `system`, then the systems' series and names)."""
    own = getattr(sat, "system", None)
    if own:
        return next((s for s in systems if _plain(s.system) == _plain(own)), None)
    for s in systems:
        if s.kind == "光学" and sat.sar or s.kind == "SAR" and not sat.sar:
            continue
        if _named(s.satellites, sat.name, sat.id) or _named(s.series, sat.series):
            return s
    return None


def why_not(req: ImagingRequest, sat: SatelliteSpec) -> Optional[str]:
    """Why a satellite (mode) cannot take a request; None when it can."""
    if req.kind == "光学" and sat.sar:
        return "種別"
    if req.kind == "SAR" and not sat.sar:
        return "種別"
    if req.satellites and not _named(req.satellites, sat.name, sat.id):
        return "衛星"
    if req.series and not _named(req.series, sat.series):
        return "シリーズ"
    if req.modes and not _named(req.modes, sat.mode, sat.sensor, f"{sat.sensor or ''}{sat.mode or ''}"):
        return "モード"
    # The request's off-nadir range has to meet the satellite's reach.
    if req.off_nadir_max is not None and req.off_nadir_max < sat.minOffNadir:
        return "オフナディア角"
    if req.off_nadir_min is not None and req.off_nadir_min > sat.maxOffNadir:
        return "オフナディア角"
    if req.look_side == "右" and sat.lookSide == "left" or req.look_side == "左" and sat.lookSide == "right":
        return "観測方向"
    if req.resolution is not None and sat.resolution is not None and sat.resolution > req.resolution:
        return "分解能"
    if req.time_of_day == "夜" and not sat.sar:
        return "時間帯"
    return None


def fitting(req: ImagingRequest, sats: Sequence[SatelliteSpec]) -> list[SatelliteSpec]:
    """The satellites (modes) that can take a request."""
    return [s for s in sats if why_not(req, s) is None]


def scenes_of(req: ImagingRequest, across_km: float, along_km: float) -> int:
    """
    The scenes a request needs: 1 for a point; for an area, the cells of a
    grid of scenes laid over its box that the area touches (cell centres and
    corners tested), at least 1.
    """
    if req.shape_kind == "点" or not req.rings:
        return 1
    box = bbox_of(req.rings)
    if box is None:
        return 1
    w, s, e, n = box
    lat = (s + n) / 2
    km_lon = 111.320 * max(0.01, math.cos(math.radians(lat)))
    km_lat = 110.574
    width_km, height_km = (e - w) * km_lon, (n - s) * km_lat
    across, along = max(0.1, across_km), max(0.1, along_km)
    cols, rows = max(1, math.ceil(width_km / across - 1e-9)), max(1, math.ceil(height_km / along - 1e-9))
    if cols * rows == 1:
        return 1
    if cols * rows > 10000:
        return cols * rows
    dx, dy = (e - w) / cols, (n - s) / rows
    count = 0
    for i in range(cols):
        for j in range(rows):
            x0, y0 = w + i * dx, s + j * dy
            probes = [(x0 + dx / 2, y0 + dy / 2), (x0, y0), (x0 + dx, y0), (x0, y0 + dy), (x0 + dx, y0 + dy)]
            if any(in_rings(req.rings, x, y) for x, y in probes) or any(
                    x0 <= p[0] <= x0 + dx and y0 <= p[1] <= y0 + dy for r in req.rings for p in r):
                count += 1
    return max(1, count)
