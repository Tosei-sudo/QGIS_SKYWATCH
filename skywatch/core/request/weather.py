"""
The weather of a request's day (optical only): the forecast cloud cover over
its area in the day's daylight hours, and what it does to the priority.

Cover is the mean of the hourly forecasts at the grid points (0.25°, as the
imaging plan's forecasts) inside the area, from `hours[0]` to `hours[1]`
local solar time (longitude / 15). Pass times are not used: satellites
that pass often (constellations) make them no more telling.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional, Sequence

from ..plan.weather import GRID, CloudSeries, cloud_at, key_of
from .model import ImagingRequest, bbox_of, center_of, clamp_priority, day_bounds, in_rings

#: Local solar hours whose cover counts.
DAYLIGHT = (9, 15)
#: The most grid points asked for one area.
MOST_POINTS = 25


def places_of(req: ImagingRequest) -> list[tuple[float, float]]:
    """The places (longitude, latitude) whose forecasts tell an area's cover: grid points inside it, or its middle."""
    box = bbox_of(req.rings)
    middle = center_of(req.rings)
    if box is None or middle is None:
        return []
    w, s, e, n = box
    points = []
    x = math.ceil(w / GRID) * GRID
    while x <= e + 1e-9:
        y = math.ceil(s / GRID) * GRID
        while y <= n + 1e-9:
            if in_rings(req.rings, x, y):
                points.append((round(x, 6), round(y, 6)))
            y += GRID
        x += GRID
    if not points:
        return [middle]
    if len(points) > MOST_POINTS:
        step = len(points) / MOST_POINTS
        points = [points[int(i * step)] for i in range(MOST_POINTS)]
    return points


def daytime_cloud(req: ImagingRequest, forecasts: dict[str, CloudSeries], day: date,
                  hours: Sequence[float] = DAYLIGHT) -> Optional[float]:
    """The mean cover (percent) over a request's area in the day's daylight hours; None without forecasts."""
    first, _ = day_bounds(day)
    values = []
    for lon, lat in places_of(req):
        series = forecasts.get(key_of((lon, lat)))
        if series is None:
            continue
        # Local solar time = UTC + longitude / 15 h; the JST day starts at 15:00 UTC the day before.
        utc_midnight = first + 9 * 3600000
        shift = lon / 15 * 3600000
        h = float(hours[0])
        while h <= float(hours[1]) + 1e-9:
            v = cloud_at(series, utc_midnight + h * 3600000 - shift)
            if v is not None and math.isfinite(v):
                values.append(v)
            h += 1
    if not values:
        return None
    return sum(values) / len(values)


def cloud_step(cover: float) -> int:
    """The tenth a cover falls in: 0–9 % → 0 … 90 % or more → 9."""
    return int(min(9, max(0, math.floor(cover / 10))))


def with_cloud(priority: int, cover: Optional[float]) -> int:
    """The priority with its ones digit set to the cloud step (unchanged without a forecast)."""
    if cover is None:
        return clamp_priority(priority)
    return clamp_priority(priority // 10 * 10 + cloud_step(cover))
