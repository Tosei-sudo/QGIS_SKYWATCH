"""
Cloud cover for features and for the map (「雲量を属性に付与」 and
「雲量マップ」): the cover of the forecast (`cloudForecast`, the service or the
GFS files of docs/cloud-forecast-file.md) at a feature's time and place, and
the grid of places a map of the service's forecast is asked for.

Pure Python: the reading is in core.plan.weather / weather_file, the layers
in qgis_io.cloud_layer.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Optional, Sequence

from .plan.weather import GRID, CloudSeries, cloud_at, key_of

HOUR = 3600000
#: A time this close before the first or after the last forecast time takes that time's cover.
EDGE = 3 * HOUR
#: The most grid cells read for one polygon (more are thinned out evenly).
CELLS_PER_SHAPE = 100
#: The ways to take the cover at a time.
INTERPOLATE, NEAREST = "interpolate", "nearest"

_ZONED = re.compile(r"(?:[zZ]|[+-]\d{2}(?::?\d{2})?|UTC|GMT|JST)\s*$")


def has_zone(text: str) -> bool:
    """Whether a time's text says its time zone (`Z`, `+09:00`, `UTC`, …)."""
    return bool(_ZONED.search(text.strip()))


def cover_at(series: Optional[CloudSeries], ms: Optional[float], method: str = INTERPOLATE) -> Optional[float]:
    """
    The cover (percent) of a series at `ms`: between the forecast times either
    side (INTERPOLATE) or at the nearest (NEAREST). A time up to EDGE outside
    the forecast takes its first or last cover; further out, None.
    """
    if series is None or ms is None or not series.times or not math.isfinite(ms):
        return None
    times, cover = series.times, series.cover
    if ms < times[0]:
        return cover[0] if times[0] - ms <= EDGE and math.isfinite(cover[0]) else None
    if ms > times[-1]:
        return cover[-1] if ms - times[-1] <= EDGE and math.isfinite(cover[-1]) else None
    if method == NEAREST:
        best = min(range(len(times)), key=lambda i: (abs(times[i] - ms), not math.isfinite(cover[i])))
        v = cover[best]
        return v if math.isfinite(v) else cloud_at(series, ms)
    return cloud_at(series, ms)


def mean_cover(forecasts: dict, places: Sequence[Sequence[float]], ms: Optional[float],
               method: str = INTERPOLATE) -> Optional[float]:
    """The mean cover of places (longitude, latitude) at `ms`, over those the forecast tells; None when none."""
    values = []
    seen = set()
    for p in places:
        key = key_of(p)
        if key in seen:
            continue
        seen.add(key)
        v = cover_at(forecasts.get(key), ms, method)
        if v is not None:
            values.append(v)
    return sum(values) / len(values) if values else None


def cells_in(bounds: Sequence[float], contains: Callable[[float, float], bool], limit: int = CELLS_PER_SHAPE) -> list[tuple[float, float]]:
    """
    The forecast's grid points (GRID degrees) inside a shape: those in its
    bounds (west, south, east, north) that `contains(lon, lat)`, thinned out
    evenly to `limit`.
    """
    west, south, east, north = bounds
    i0, i1 = math.ceil(west / GRID), math.floor(east / GRID)
    j0, j1 = math.ceil(south / GRID), math.floor(north / GRID)
    n = max(0, i1 - i0 + 1) * max(0, j1 - j0 + 1)
    if n == 0:
        return []
    # Thinned out on both axes before testing, so a huge shape costs no more than a small one.
    stride = max(1, math.ceil(math.sqrt(n / (limit * 4)))) if n > limit * 4 else 1
    out = [(i * GRID, j * GRID) for j in range(j0, j1 + 1, stride) for i in range(i0, i1 + 1, stride)
           if contains(i * GRID, j * GRID)]
    if len(out) > limit:
        step = len(out) / limit
        out = [out[int(k * step)] for k in range(limit)]
    return out


def map_grid(west: float, south: float, east: float, north: float, side: int = 20) -> tuple[float, list[float], list[float]]:
    """
    The places a map of the service's forecast is asked for over an extent
    (degrees): at most `side` × `side`, on a multiple of GRID. Returns the
    step and the longitudes (west to east) and latitudes (north to south).
    """
    south, north = max(-90.0, min(south, north)), min(90.0, max(south, north))
    if east < west:
        west, east = east, west
    span = max(east - west, north - south, GRID)
    step = max(GRID, math.ceil(span / side / GRID) * GRID)

    def axis(lo: float, hi: float) -> list[float]:
        a, b = math.floor(lo / step), math.ceil(hi / step)
        return [k * step for k in range(a, b + 1)]

    lons = axis(west, east)
    lats = [v for v in reversed(axis(south, north)) if -90 <= v <= 90]
    return step, lons, lats


