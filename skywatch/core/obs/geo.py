"""
Distances and bearings on the sphere, as the viewer measures them
(OpenLayers' ol/sphere: `getDistance`, `offset`, the mean Earth radius).
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

#: The mean Earth radius (m) OpenLayers uses.
RADIUS = 6371008.8


def get_distance(c1: Sequence[float], c2: Sequence[float], radius: float = RADIUS) -> float:
    """The great-circle distance (m) between two longitude / latitude places (haversine)."""
    lat1 = math.radians(c1[1])
    lat2 = math.radians(c2[1])
    d_lat = (lat2 - lat1) / 2
    d_lon = math.radians(c2[0] - c1[0]) / 2
    a = math.sin(d_lat) ** 2 + math.sin(d_lon) ** 2 * math.cos(lat1) * math.cos(lat2)
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def offset(c1: Sequence[float], distance: float, bearing: float, radius: float = RADIUS) -> list[float]:
    """The place `distance` m from `c1` along `bearing` (radians from north, clockwise)."""
    lat1 = math.radians(c1[1])
    lon1 = math.radians(c1[0])
    d = distance / radius
    lat = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(bearing))
    lon = lon1 + math.atan2(math.sin(bearing) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat))
    return [math.degrees(lon), math.degrees(lat)]


def bearing(start: Sequence[float], end: Sequence[float]) -> float:
    """The bearing (degrees from north, clockwise, 0–360) from one place to another."""
    l1, p1, l2, p2 = (math.radians(v) for v in (start[0], start[1], end[0], end[1]))
    y = math.sin(l2 - l1) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(l2 - l1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def km_text(m: float) -> str:
    """`850 m`, `1.2 km`, `12 km` (as the viewer's lists)."""
    if m >= 1000:
        return f"{to_fixed(m / 1000, 0 if m >= 10000 else 1)} km"
    return f"{js_round(m)} m"


def js_round(x: float) -> int:
    """`Math.round`: halves go up."""
    return math.floor(x + 0.5)


def to_fixed(x: float, digits: int) -> str:
    """JavaScript's `toFixed`: the exact binary value rounded, halves away from zero."""
    return str(Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))
