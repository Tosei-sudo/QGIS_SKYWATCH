"""
Simple orthorectification without an RPC model (the viewer's simple-ortho.ts):
an image already georeferenced onto a flat ground (one reference height) is
corrected for the terrain with a DEM and the direction the satellite looked
from (its azimuth and elevation angle).

A point `h` metres above the reference height, seen at elevation angle `e`,
shows up `(h − h0) / tan(e)` metres from where it is, away from the
satellite. So each output pixel reads the image that far away from itself,
in the direction the satellite is not. Where the DEM has no value, nothing
is moved.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class SensorView:
    """Where the satellite was, seen from the ground, in degrees: clockwise from north (0–360), above the horizon (90 straight above)."""
    azimuth: float
    elevation: float


def displacement(view: SensorView) -> tuple[float, float]:
    """Ground metres, per metre above the reference height, east and north (away from the satellite)."""
    az = math.radians(view.azimuth)
    cot = 1 / math.tan(math.radians(max(1.0, min(90.0, view.elevation))))
    return -math.sin(az) * cot, -math.cos(az) * cot


def sensor_view_from_text(text: str) -> Optional[SensorView]:
    """
    The satellite's azimuth and elevation from metadata text: a DigitalGlobe /
    Maxar .IMD (`meanSatAz`, `meanSatEl`), GDAL metadata (the same items), or
    keys like `satellite_azimuth` and `view_angle` / `off_nadir` (elevation
    then taken as 90° − off-nadir). None when the azimuth or both angles are missing.
    """
    def find(keys: str) -> Optional[float]:
        m = re.search(rf"(?:{keys})[^\d\n-]{{0,40}}?(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
        return float(m.group(1)) if m else None

    azimuth = find("meanSatAz|sat_?azimuth|satellite_?azimuth|SATAZIMUTH")
    elevation = find("meanSatEl|sat_?elevation|satellite_?elevation|SATELEVATION")
    if elevation is None:
        off_nadir = find("meanOffNadirViewAngle|off_?nadir(?:_?angle)?|view_angle")
        if off_nadir is not None:
            elevation = 90 - abs(off_nadir)
    if azimuth is None or elevation is None or not 0 < elevation <= 90:
        return None
    return SensorView(azimuth if 0 <= azimuth < 360 else azimuth % 360, elevation)


def is_imd_name(name: str) -> bool:
    """A DigitalGlobe / Maxar image metadata file."""
    return name.lower().endswith(".imd")


def sample(values: np.ndarray, x: np.ndarray, y: np.ndarray, resample: str = "bilinear", fill: float = math.nan) -> np.ndarray:
    """
    `values` (rows × cols) at pixel positions x, y (pixel corners at integers,
    so a pixel's centre is at +0.5): bilinear between centres, or the nearest
    pixel; `fill` outside, and where a neighbour used is NaN.
    """
    h, w = values.shape
    if resample == "nearest":
        i, j = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
        inside = (i >= 0) & (i < w) & (j >= 0) & (j < h)
        out = np.full(x.shape, fill, np.float64)
        out[inside] = values[j[inside], i[inside]]
        return out
    fx, fy = x - 0.5, y - 0.5
    i, j = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
    tx, ty = fx - i, fy - j
    # At the outer half pixel the edge pixel is used alone.
    inside = (fx >= -0.5) & (fx <= w - 0.5) & (fy >= -0.5) & (fy <= h - 0.5)
    i0, i1 = np.clip(i, 0, w - 1), np.clip(i + 1, 0, w - 1)
    j0, j1 = np.clip(j, 0, h - 1), np.clip(j + 1, 0, h - 1)
    v = (values[j0, i0] * (1 - tx) + values[j0, i1] * tx) * (1 - ty) + (values[j1, i0] * (1 - tx) + values[j1, i1] * tx) * ty
    return np.where(inside & ~np.isnan(v), v, fill)
