"""
地図の投影法と向きを開いた画像に合わせる (tsudo, 2026-11-25: 「画像カタログから開くとき、
投影法を画像に合わせるオプション…回転も、北を上じゃなくて、撮像方向からみた向きに」,
with 「衛星が上の向きも足す」): the arithmetic, without QGIS.

The map's way up (`ORIENTATIONS`): north (as before), the way the satellite
looked (撮像方向: from the satellite to the ground, the satellite at the
bottom of the screen; an oblique optical image's buildings lean away, up),
the satellite at the top (撮像方向が下: a SAR image's shadows fall down the
screen), or the satellite's way over the ground (進行方向, SAR).

Where those come from (qgis_io/map_view_io.py finds them, in this order):
the catalog's 方位角 (`fields.azimuth`: the satellite seen from the ground),
the direction typed in クイック幾何補正, the file's metadata (as クイック幾何
補正 and 簡易オルソ補正 read it), and for a SAR image in the radar's own
geometry (placed by GCPs) its columns. The way over the ground: the
metadata's heading, else a radar's look turned 90° to the side it looked
from (right unless said), else the way the image's rows go on the ground
(an image placed by GCPs or RPC: its lines are taken one after another as
the satellite flies).

The map's CRS (「投影法を画像に合わせる」): the image's when it is projected;
an image in latitude and longitude (or placed by GCPs, RPC) gets the UTM
zone of its centre, since a map in degrees is stretched east–west and
skews when turned. A map in degrees is put in that zone also when it is
only turned.

QGIS turns its map by `rotation` degrees clockwise: north is shown that many
degrees clockwise from the top, so to put a bearing B (of the map's grid)
up the rotation is −B.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

#: The map's way up when an image opens: as before, the look, the satellite, the flight.
ORIENTATIONS = ("north", "look", "satellite", "flight")
ORIENTATION_LABELS = {
    "north": "北が上",
    "look": "撮像方向が上（衛星が下）",
    "satellite": "衛星が上（撮像方向が下）",
    "flight": "進行方向が上（SAR）",
}
#: An image taken from nearly straight down (off-nadir below this, degrees): its satellite's direction means little.
NEAR_NADIR = 3.0
EARTH_RADIUS = 6371.0
#: Where a direction was found, as the dock says it.
SOURCES = {"catalog": "カタログの方位角", "typed": "幾何補正で入力した方角", "file": "画像のメタデータ",
           "columns": "画像の列の向き", "rows": "画像の行の向き", "look": "観測の向きから"}


@dataclass(frozen=True)
class Directions:
    """
    What is known of how an image was taken: `toward`, the satellite seen
    from the ground (degrees clockwise from north); `heading`, the
    satellite's way over the ground; `off_nadir`, its angle from straight
    down (degrees); each with where it was found (SOURCES' keys).
    """
    toward: Optional[float] = None
    toward_from: str = ""
    heading: Optional[float] = None
    heading_from: str = ""
    off_nadir: Optional[float] = None


def bearing_up(orientation: str, d: Directions) -> tuple[Optional[float], str]:
    """
    The bearing (degrees clockwise from true north) to put at the top of the
    map, with where it came from; None and why when it is not known (or north).
    """
    if orientation not in ORIENTATIONS or orientation == "north":
        return None, ""
    if orientation == "flight":
        if d.heading is None:
            return None, "進行方向がわからない（画像のメタデータ・観測の向き・画像の行の向きのどれもなし）"
        return d.heading % 360, SOURCES.get(d.heading_from, d.heading_from)
    if d.toward is None:
        return None, "衛星の方位がわからない（カタログの方位角・画像のメタデータになし）"
    if d.off_nadir is not None and abs(d.off_nadir) < NEAR_NADIR:
        return None, f"ほぼ真下から撮った画像（オフナディア {abs(d.off_nadir):.1f}°）"
    up = d.toward if orientation == "satellite" else d.toward + 180
    return up % 360, SOURCES.get(d.toward_from, d.toward_from)


def rotation_for(grid_bearing: float) -> float:
    """QGIS's map rotation (degrees clockwise, −180 < r ≤ 180) that puts a bearing of the map's grid at the top."""
    r = (-grid_bearing) % 360
    return r - 360 if r > 180 else r


def utm_epsg(lon: float, lat: float) -> int:
    """The EPSG code of WGS 84 / UTM's zone at a place (326zz north of the equator, 327zz south)."""
    zone = int(math.floor((((lon + 180) % 360) + 360) % 360 / 6)) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


def off_nadir_of(angle: Optional[float], kind: str = "offNadir", orbit_height: float = 600) -> Optional[float]:
    """The off-nadir angle of a catalog's `angle` read by its `angleKind` (an incidence through the Earth's curvature)."""
    if angle is None or not math.isfinite(angle):
        return None
    if kind == "offNadir":
        return abs(angle)
    incidence = 90 - abs(angle) if kind == "elevation" else abs(angle)
    if not 0 <= incidence < 90:
        return None
    return math.degrees(math.asin(EARTH_RADIUS / (EARTH_RADIUS + orbit_height) * math.sin(math.radians(incidence))))


def bearing_of(east: float, north: float) -> Optional[float]:
    """The bearing (degrees clockwise from north) of a step east and north, None for none."""
    if not (math.isfinite(east) and math.isfinite(north)) or (east == 0 and north == 0):
        return None
    return math.degrees(math.atan2(east, north)) % 360


def heading_from_look(toward: float, side: Optional[str]) -> float:
    """A radar's way over the ground from the satellite's direction: it looks right of its track unless `side` is 'L'."""
    return (toward + (-90 if side == "L" else 90)) % 360


def screen_axes(rotation: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """The screen's right and up, as unit steps of the map's grid (x, y), for a map turned by `rotation` (degrees clockwise)."""
    a = math.radians(rotation)
    return (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))


def fit_box(points: Iterable[Sequence[float]], rotation: float, margin: float = 1.05) -> Optional[tuple[float, float, float, float]]:
    """
    The extent to give QGIS (centre x, centre y, width, height in the map's
    units, before its rotation) for the points (x, y in the map's CRS) to fit
    a map turned by `rotation`, with a margin; None without points.
    """
    pts = [(float(x), float(y)) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    if not pts:
        return None
    (rx, ry), (ux, uy) = screen_axes(rotation)
    across = [x * rx + y * ry for x, y in pts]
    up = [x * ux + y * uy for x, y in pts]
    a0, a1, u0, u1 = min(across), max(across), min(up), max(up)
    ma, mu = (a0 + a1) / 2, (u0 + u1) / 2
    cx, cy = ma * rx + mu * ux, ma * ry + mu * uy
    return cx, cy, (a1 - a0) * margin, (u1 - u0) * margin


def turned_text(rotation: float) -> str:
    """`37°` / `−37°`: the map's rotation as the dock says it."""
    r = round(rotation)
    return f"−{abs(r)}°" if r < 0 else f"{r}°"
