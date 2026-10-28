"""
Height from a shadow (影から高さを測る, the viewer's shadow-height.ts): the
foot of a building, a tower or a tree and the tip of its shadow are both on
the ground, so the height is the shadow's length times the tangent of the
Sun's elevation, whatever the satellite's viewing angle (the ground taken as
flat between the two points).

The Sun's angles come, best first, from what the image says (GDAL metadata
items such as SUN_ELEVATION, Maxar's MEANSUNEL, Planet's sun_elevation, NITF
USE00A), else from when it was taken and where the foot is
(core.plan.orbit.sun_angles), else as typed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

#: Item names that hold the Sun's elevation (or zenith) and azimuth, best first (as the viewer).
ELEVATION = [re.compile(r"^(mean_?)?sun_?el(evation)?(_angle)?$", re.I), re.compile(r"^nitf_use00a_sun_el$", re.I),
             re.compile(r"sun_?elev|solar_?elev|sun_?el\b|sunel", re.I)]
ZENITH = [re.compile(r"^(mean_?)?(sun|solar)_?zenith(_angle)?$", re.I), re.compile(r"(sun|solar)_?zen", re.I)]
AZIMUTH = [re.compile(r"^(mean_?)?sun_?az(imuth)?(_angle)?$", re.I), re.compile(r"^nitf_use00a_sun_az$", re.I),
           re.compile(r"sun_?azi|solar_?azi|sun_?az\b|sunaz", re.I)]


@dataclass
class Sun:
    """The Sun when the image was taken, degrees; `azimuth` clockwise from north, None when unknown."""
    elevation: float
    azimuth: Optional[float] = None
    #: The metadata item it was read from, '' when not from the metadata.
    item: str = ""


@dataclass
class ShadowMeasurement:
    """One shadow measured: the foot and the tip (lon, lat), the shadow's length and the height in metres."""
    foot: tuple
    tip: tuple
    shadow: float
    height: float
    sun_elevation: float
    sun_azimuth: Optional[float]


def _number(raw: str) -> Optional[float]:
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(re.split(r"[\s,]+", text)[0])
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _item_value(items: Sequence[tuple[str, str]], patterns) -> Optional[tuple[str, float]]:
    """The first item whose name matches one of `patterns` (in their order) and whose value is a number."""
    for pattern in patterns:
        for name, raw in items:
            if not pattern.search(name):
                continue
            value = _number(raw)
            if value is not None:
                return name, value
    return None


def sun_from_metadata(items: Iterable[tuple[str, str]]) -> Optional[Sun]:
    """The Sun's angles in an image's metadata items (name, value), or None when it does not say."""
    items = list(items)
    el = _item_value(items, ELEVATION)
    zenith = None if el else _item_value(items, ZENITH)
    elevation = el[1] if el else 90 - zenith[1] if zenith else math.nan
    # NITF writes 999.9 when it does not know.
    if not math.isfinite(elevation) or elevation <= 0 or elevation > 90:
        return None
    az = _item_value(items, AZIMUTH)
    azimuth = az[1] % 360 if az and 0 <= az[1] <= 360 else None
    return Sun(elevation, azimuth, (el or zenith)[0])


def height_from_shadow(shadow: float, elevation: float) -> float:
    """Height of what casts a shadow of `shadow` length with the Sun `elevation` degrees up."""
    return shadow * math.tan(math.radians(elevation))


def shadow_azimuth(sun_azimuth: float) -> float:
    """Which way shadows fall: away from the Sun, clockwise from north."""
    return (sun_azimuth + 180) % 360


def step_toward(lon_lat: Sequence[float], azimuth: float, metres: float) -> tuple[float, float]:
    """A point `metres` from `lon_lat` toward `azimuth` (degrees), close enough for the few metres a direction needs."""
    lon, lat = lon_lat[0], lon_lat[1]
    a = math.radians(azimuth)
    per_degree = 111_320
    return lon + math.sin(a) * metres / (per_degree * math.cos(math.radians(lat))), lat + math.cos(a) * metres / per_degree


def snap_to_ray(start: Sequence[float], direction: Sequence[float], tip: Sequence[float]) -> tuple[float, float]:
    """`tip` moved onto the ray from `start` along `direction`: the nearest point of the ray (never behind `start`)."""
    length = math.hypot(direction[0], direction[1])
    if not length:
        return tip[0], tip[1]
    ux, uy = direction[0] / length, direction[1] / length
    t = max(0.0, (tip[0] - start[0]) * ux + (tip[1] - start[1]) * uy)
    return start[0] + ux * t, start[1] + uy * t


def format_metres(value: float) -> str:
    """A length or a height as the tool shows it: `12.3 m`, `1.234 km`."""
    if value < 1000:
        return f"{value:.1f} m"
    km = value / 1000
    return f"{km:,.3f} km" if km < 100 else f"{km:,.1f} km"
