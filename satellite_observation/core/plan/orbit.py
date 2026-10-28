"""
Orbits and the Earth, as the viewer computes them with satellite.js
(imaging-plan.ts): SGP4 (the `sgp4` package, or the pure-Python copy
bundled in `ext/` when it is not installed), Greenwich sidereal time, the
Sun's position (Vallado's low-precision formula, as satellite.js `sunPos`),
and the frames used to look across and along a ground track.

Plain Python and numpy; no ephemeris files, so it works without a network.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np


def _windows_sgp4() -> None:
    """The Windows zip's sgp4 (C++ accelerated, ext/win64, put there by scripts/build_zip.py --win64) on the path, when there is one."""
    import sys

    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ext", "win64")
    if os.path.isdir(os.path.join(folder, "sgp4")) and folder not in sys.path:
        sys.path.append(folder)


try:  # The installed sgp4 (C++ accelerated when its wheel has it), else the Windows zip's; SATELLITE_OBSERVATION_SGP4=bundled forces the copy (tests).
    if os.environ.get("SATELLITE_OBSERVATION_SGP4") == "bundled":
        raise ImportError
    _windows_sgp4()
    from sgp4.api import Satrec  # type: ignore
    from sgp4.api import accelerated as SGP4_ACCELERATED  # type: ignore

    SGP4_SOURCE = "sgp4"
except ImportError:  # The bundled pure-Python copy.
    from ...ext.sgp4.api import Satrec  # type: ignore
    from ...ext.sgp4.api import accelerated as SGP4_ACCELERATED  # type: ignore

    SGP4_SOURCE = "bundled"

Vec = tuple  # (x, y, z)

DEG = 180 / math.pi
RAD = math.pi / 180
TWO_PI = 2 * math.pi
#: Mean Earth radius, km.
R = 6371.0088
#: The Earth's rotation, rad/s.
OMEGA = 7.2921159e-5
E2 = 0.00669437999014
#: WGS 84 as satellite.js.
_A = 6378.137
_B = 6356.7523142
_F = (_A - _B) / _A
_E2_SJS = 2 * _F - _F * _F
#: The Earth's polar radius (km).
POLAR_RADIUS = 6356.752
MU = 398600.4418


def julian(ms: float) -> float:
    """Julian date of a time in ms."""
    return ms / 86400000 + 2440587.5


def ms_of_julian(jd: float) -> float:
    return (jd - 2440587.5) * 86400000


def gstime(jd: float) -> float:
    """Greenwich mean sidereal time (rad), as satellite.js (IAU-82)."""
    tut1 = (jd - 2451545.0) / 36525.0
    temp = -6.2e-6 * tut1 * tut1 * tut1 + 0.093104 * tut1 * tut1 + (876600.0 * 3600 + 8640184.812866) * tut1 + 67310.54841
    temp = math.fmod(temp * RAD / 240.0, TWO_PI)
    return temp + TWO_PI if temp < 0 else temp


def gstime_array(jd: np.ndarray) -> np.ndarray:
    tut1 = (jd - 2451545.0) / 36525.0
    temp = -6.2e-6 * tut1 * tut1 * tut1 + 0.093104 * tut1 * tut1 + (876600.0 * 3600 + 8640184.812866) * tut1 + 67310.54841
    temp = np.fmod(temp * RAD / 240.0, TWO_PI)
    return np.where(temp < 0, temp + TWO_PI, temp)


def sun_position(jd: float) -> tuple[float, float, float]:
    """The Sun's direction (ECI, au), as satellite.js `sunPos`."""
    tut1 = (jd - 2451545) / 36525
    meanlong = math.fmod(280.46 + 36000.77 * tut1, 360)
    meananomaly = math.fmod((357.5277233 + 35999.05034 * tut1) * RAD, TWO_PI)
    if meananomaly < 0:
        meananomaly += TWO_PI
    eclplong = math.fmod(meanlong + 1.914666471 * math.sin(meananomaly) + 0.019994643 * math.sin(2.0 * meananomaly), 360.0) * RAD
    obliquity = (23.439291 - 0.0130042 * tut1) * RAD
    magr = 1.000140612 - 0.016708617 * math.cos(meananomaly) - 0.000139589 * math.cos(2.0 * meananomaly)
    return (
        magr * math.cos(eclplong),
        magr * math.cos(obliquity) * math.sin(eclplong),
        magr * math.sin(obliquity) * math.sin(eclplong),
    )


def eci_to_ecf(p: Sequence[float], gmst: float) -> tuple[float, float, float]:
    c, s = math.cos(gmst), math.sin(gmst)
    return (p[0] * c + p[1] * s, p[0] * -s + p[1] * c, p[2])


def eci_to_geodetic(p: Sequence[float], gmst: float) -> tuple[float, float, float]:
    """Longitude, latitude (rad) and height (km) of an ECI position, as satellite.js."""
    x, y, z = p
    r = math.sqrt(x * x + y * y)
    pi = math.pi
    longitude = math.fmod(math.fmod(math.atan2(y, x) - gmst + pi, TWO_PI) + TWO_PI, TWO_PI) - pi
    latitude = math.atan2(z, math.sqrt(x * x + y * y))
    c = 0.0
    for _ in range(20):
        c = 1 / math.sqrt(1 - _E2_SJS * (math.sin(latitude) * math.sin(latitude)))
        latitude = math.atan2(z + _A * c * _E2_SJS * math.sin(latitude), r)
    height = r / math.cos(latitude) - _A * c
    return longitude, latitude, height


def geodetic_to_ecf(lon: float, lat: float, height: float = 0.0) -> tuple[float, float, float]:
    """ECEF (km) of a geodetic place (radians), as satellite.js."""
    normal = _A / math.sqrt(1 - _E2_SJS * (math.sin(lat) * math.sin(lat)))
    return (
        (normal + height) * math.cos(lat) * math.cos(lon),
        (normal + height) * math.cos(lat) * math.sin(lon),
        (normal * (1 - _E2_SJS) + height) * math.sin(lat),
    )


# --- Vectors -------------------------------------------------------------------------


def dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def unit(a):
    n = norm(a)
    return (a[0] / n, a[1] / n, a[2] / n)


def ecef_of(lon_lat: Sequence[float]):
    """A place on the WGS 84 ellipsoid (degrees) as ECEF (km)."""
    return geodetic_to_ecf(lon_lat[0] * RAD, lon_lat[1] * RAD, 0)


def up_of(lon_lat: Sequence[float]):
    """The geodetic up direction at a place."""
    lon, lat = lon_lat[0] * RAD, lon_lat[1] * RAD
    return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))


def lon_lat_of(p) -> tuple[float, float]:
    """Longitude and geodetic latitude (degrees) of a direction from the Earth's centre."""
    lon = math.atan2(p[1], p[0]) * DEG
    geocentric = math.atan2(p[2], math.hypot(p[0], p[1]))
    return (lon, math.atan(math.tan(geocentric) / (1 - E2)) * DEG)


def azimuth_of(lon_lat: Sequence[float], v) -> float:
    """The azimuth (degrees clockwise from north) of a direction `v` seen from a place."""
    lon, lat = lon_lat[0] * RAD, lon_lat[1] * RAD
    east = (-math.sin(lon), math.cos(lon), 0)
    north = (-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat))
    return (math.atan2(dot(v, east), dot(v, north)) * DEG + 360) % 360


def sun_angles(lon_lat: Sequence[float], ms: float) -> tuple[float, float]:
    """Elevation and azimuth (degrees) of the Sun seen from a place."""
    jd = julian(ms)
    to_sun = unit(eci_to_ecf(sun_position(jd), gstime(jd)))
    lon, lat = lon_lat[0] * RAD, lon_lat[1] * RAD
    east = (-math.sin(lon), math.cos(lon), 0)
    north = (-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat))
    elevation = math.asin(max(-1.0, min(1.0, dot(to_sun, up_of(lon_lat))))) * DEG
    azimuth = (math.atan2(dot(to_sun, east), dot(to_sun, north)) * DEG + 360) % 360
    return elevation, azimuth


def sun_elevation(lon_lat: Sequence[float], ms: float) -> float:
    """Elevation of the Sun at a place, degrees."""
    return sun_angles(lon_lat, ms)[0]


def off_nadir_for(central_angle: float, distance: float) -> float:
    """Off-nadir angle (degrees) of a point `central_angle` radians from the sub-satellite point, from `distance` km."""
    return math.atan2(R * math.sin(central_angle), distance - R * math.cos(central_angle)) * DEG


def ground_range(off_nadir: float, distance: float) -> float:
    """Ground distance (km) from the sub-satellite point seen `off_nadir` degrees off nadir (the horizon when it cannot)."""
    s = (distance / R) * math.sin(off_nadir * RAD)
    lam = math.acos(R / distance) if s >= 1 else math.asin(s) - off_nadir * RAD
    return R * lam


def track_rate(orbit_radius: float) -> float:
    """How fast a ground track moves, radians of the Earth per second."""
    return math.sqrt(MU / orbit_radius ** 3)


# --- SGP4 ------------------------------------------------------------------------------


@dataclass
class State:
    """A satellite at a moment: ECEF position and velocity (km, km/s), whether it moves north, GMST and the ECI position."""
    r: tuple
    v: tuple
    north: bool
    gmst: float
    eci: tuple


class Orbit:
    """A TLE ready to propagate."""

    def __init__(self, tle1: str, tle2: str):
        self.satrec = Satrec.twoline2rv(tle1, tle2)
        jd = self.satrec.jdsatepoch + self.satrec.jdsatepochF
        #: The TLE's epoch, ms.
        self.epoch = ms_of_julian(jd)
        self.ok = not self.satrec.error and math.isfinite(self.satrec.no_kozai) and self.satrec.no_kozai > 0

    def state(self, ms: float) -> Optional[State]:
        """The state at `ms`; None when SGP4 fails."""
        error, p, v = self.satrec.sgp4_tsince((ms - self.epoch) / 60000.0)
        if error or not math.isfinite(p[0]):
            return None
        gmst = gstime(julian(ms))
        r = eci_to_ecf(p, gmst)
        vr = eci_to_ecf(v, gmst)
        # The Earth turns under the satellite: v_ecef = R·v_eci − ω × r.
        return State(r, (vr[0] + OMEGA * r[1], vr[1] - OMEGA * r[0], vr[2]), v[2] > 0, gmst, tuple(p))

    def states(self, times: np.ndarray):
        """ECEF positions and velocities (n × 3) at `times` (ms); NaN rows where SGP4 fails."""
        tsince = (times - self.epoch) / 60000.0
        jd = np.full(len(times), self.satrec.jdsatepoch)
        fr = self.satrec.jdsatepochF + tsince / 1440.0
        error, p, v = self.satrec.sgp4_array(jd, fr)
        p = np.asarray(p, dtype=float)
        v = np.asarray(v, dtype=float)
        gmst = gstime_array(julian(times))
        c, s = np.cos(gmst), np.sin(gmst)
        r = np.empty_like(p)
        r[:, 0] = p[:, 0] * c + p[:, 1] * s
        r[:, 1] = -p[:, 0] * s + p[:, 1] * c
        r[:, 2] = p[:, 2]
        vr = np.empty_like(v)
        vr[:, 0] = v[:, 0] * c + v[:, 1] * s + OMEGA * r[:, 1]
        vr[:, 1] = -v[:, 0] * s + v[:, 1] * c - OMEGA * r[:, 0]
        vr[:, 2] = v[:, 2]
        bad = (np.asarray(error) != 0) | ~np.isfinite(r[:, 0])
        r[bad] = np.nan
        vr[bad] = np.nan
        return r, vr


# --- Frames ------------------------------------------------------------------------------


@dataclass
class Frame:
    """The orbit's frame at a moment: the normal of its plane, along the track, and up."""
    n: tuple
    a: tuple
    w: tuple


def frame_of(r, v) -> Frame:
    n = unit(cross(r, v))
    a = unit(cross(n, r))
    w = cross(a, n)
    return Frame(n, a, w)


def track_angles(p, f: Frame) -> tuple[float, float]:
    """The angles (radians) of a point across the track (left positive) and along it."""
    return (math.asin(max(-1.0, min(1.0, dot(p, f.n)))), math.atan2(dot(p, f.a), dot(p, f.w)))


def point_at(f: Frame, x: float, y: float):
    """The point `x` radians across the track and `y` along it."""
    c = math.cos(x)
    sx = math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    return tuple(sx * f.n[i] + c * (cy * f.w[i] + sy * f.a[i]) for i in range(3))
