"""
クイック幾何補正 (tsudo, 2026-11-23: 「SAR画像の位置ずれ…がすごく、そのまま記録作る
とずれる」, then 「DEMを含めた補正で再設計して実装して。DEMはエスリのイメージサー
バーを使えること」): an image placed by its file (GCPs, a geotransform, RPC) is put
where it really is on the ground, for the terrain with a DEM and for what is
left with a few tie points (a landmark in the image dragged to where it is).

The terrain: a SAR image shows a point `h` metres above the height its file
was placed at `(h − h_ref) / tan(θ)` metres towards the satellite (θ the
incidence angle), the higher the further; an optical image with an RPC model
knows its own geometry and only needs the height. So where the image shows a
place X (true, at DEM height h) is, for an image placed flat by its file

    pixel(X) = f⁻¹( T(X) + (h + N − h_ref) / tan(θ) · u )

with f the file's placing (as QGIS does it), u the unit vector from the
ground towards the satellite, N the geoid height (the DEM's sea-level heights
to the ellipsoidal ones of the file) and T the correction the tie points
give (none: the identity; 1 point: a shift; 2: shift, rotation and scale; 3
or more: an affine, least squares). For an image with an RPC model it is
RPC(T(X), h + N).

That model is written as an RPC model of the image's own pixels (fitted to
it on a grid of places and heights, RPC00B, third order) for GDAL to warp
the image with the DEM on the fly (qgis_io/geo_fix_io.py), so QGIS shows the
image corrected without a copy of it, and the image's pixels stay the file's.

This module is the arithmetic and the settings: the satellite's direction
from the file's metadata, the fits, the correction record kept per image
(shared in the Esri table `imageCorrections`, else on this PC) and `dem`.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Sequence

import numpy as np

from .util import is_number, is_record, js_string, ms_of

# ---- the satellite's direction ---------------------------------------------------------------

#: Typical inclination of the SAR satellites' sun-synchronous orbits (degrees), for the heading from the pass direction alone.
SUN_SYNCHRONOUS = 97.7


@dataclass(frozen=True)
class LookGeometry:
    """
    How the satellite saw the ground: `toward`, the direction from the ground
    to the satellite (degrees clockwise from north); the incidence angle
    (degrees from the vertical), or `near` and `far` across the image's
    columns (range) when it varies so; where it was found.
    """
    toward: float
    incidence: float
    near: Optional[float] = None
    far: Optional[float] = None
    source: str = ""

    def incidence_at(self, fraction):
        """The incidence angle at a fraction (0 first column … 1 last) across the image (an array too)."""
        if self.near is None or self.far is None:
            return np.full(np.shape(fraction), self.incidence, dtype=np.float64) if np.ndim(fraction) else self.incidence
        f = np.clip(fraction, -0.25, 1.25)
        return self.near + (self.far - self.near) * f

    def unit(self) -> tuple[float, float]:
        """East and north of the unit vector towards the satellite."""
        a = math.radians(self.toward)
        return math.sin(a), math.cos(a)

    def text(self) -> str:
        """`入射角 34.5°（28.1〜41.0°）、衛星は西（263°）`"""
        if self.near is not None and self.far is not None:
            angle = f"入射角 {self.incidence:.1f}°（{self.near:.1f}〜{self.far:.1f}°）"
        else:
            angle = f"入射角 {self.incidence:.1f}°"
        return f"{angle}、衛星は{compass(self.toward)}（{self.toward:.0f}°）"


def compass(azimuth: float) -> str:
    """The 8 directions in Japanese: 北・北東・東…"""
    names = ("北", "北東", "東", "南東", "南", "南西", "西", "北西")
    return names[int(((azimuth % 360) + 22.5) // 45) % 8]


def _angles(text: str, word: str) -> list[tuple[str, float]]:
    """Every `<key containing word> … number` of the text (key, value)."""
    out = []
    for m in re.finditer(rf"([A-Za-z_.]*{word}[A-Za-z_.]*)[\"'\s:=>]{{1,6}}(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", text, re.IGNORECASE):
        try:
            out.append((m.group(1).lower(), float(m.group(2))))
        except ValueError:
            continue
    return out


def _side(text: str) -> Optional[str]:
    """'R' or 'L': which side of its track the radar looked to, when the text says."""
    m = re.search(r"(?:look_?side|side_?of_?track|antenna_?pointing|look_?direction|pointing|observation_?direction)"
                  r"[\"'\s:=>]{1,6}[\"']?(right|left|R|L)\b", text, re.IGNORECASE)
    return m.group(1)[0].upper() if m else None


def _pass(text: str) -> Optional[str]:
    """'A' or 'D': the orbit's direction when the text says (ascending, descending)."""
    m = re.search(r"\b(ascending|descending)\b", text, re.IGNORECASE)
    if m:
        return m.group(1)[0].upper()
    m = re.search(r"(?:orbit_?direction|pass_?direction|pass)[\"'\s:=>]{1,6}[\"']?(A|D|ASC|DESC)\b", text, re.IGNORECASE)
    return m.group(1)[0].upper() if m else None


def heading_of_pass(direction: str, lat: float, inclination: float = SUN_SYNCHRONOUS) -> float:
    """The ground track's heading (degrees from north) of a pass at a latitude: sin ψ = cos i / cos φ (ascending)."""
    c = math.cos(math.radians(lat))
    s = max(-1.0, min(1.0, math.cos(math.radians(inclination)) / max(c, 1e-6)))
    psi = math.degrees(math.asin(s))
    return (psi % 360) if direction == "A" else (180 - psi) % 360


def look_side(text: str) -> Optional[str]:
    """'R' or 'L': which side of its track the radar looked to, when the metadata text says (None: not said)."""
    return _side(text or "")


def heading_from_text(text: str, lat: Optional[float] = None) -> Optional[float]:
    """
    The satellite's way over the ground (degrees clockwise from north) as the
    metadata text says: a heading or track angle, else the pass direction
    (ascending, descending) at `lat`; None when it says neither.
    """
    if not text:
        return None
    for key, value in _angles(text, "heading") + _angles(text, "track_?angle"):
        if -360 <= value <= 360 and "sun" not in key:
            return value % 360
    direction = _pass(text)
    if direction is not None and lat is not None:
        return heading_of_pass(direction, lat)
    return None


def look_from_text(text: str, lat: Optional[float] = None) -> Optional[LookGeometry]:
    """
    The incidence angle and the direction to the satellite from metadata text
    (GDAL items, SICD XML, a JSON or XML of the maker), None without an
    incidence angle or without a direction. The direction: SICD's AzimAng
    (ground to the radar); else the track's heading (or the pass direction,
    at `lat`) turned 90° to the side the radar looked from (right-looking
    when not said).
    """
    if not text:
        return None
    near = far = centre = None
    for key, value in _angles(text, "incidence"):
        if not 0 < value < 90:
            continue
        if "near" in key or key.endswith(("_min", "min")):
            near = near if near is not None else value
        elif "far" in key or key.endswith(("_max", "max")):
            far = far if far is not None else value
        elif centre is None:
            centre = value
    if centre is None and near is not None and far is not None:
        centre = (near + far) / 2
    if centre is None:
        return None
    toward = None
    for key, value in _angles(text, "azimang"):
        if 0 <= value <= 360:
            toward = value % 360
            break
    if toward is None:
        heading = None
        for key, value in _angles(text, "heading") + _angles(text, "track_?angle"):
            if 0 <= value <= 360 and "sun" not in key:
                heading = value % 360
                break
        if heading is None:
            direction = _pass(text)
            if direction is not None and lat is not None:
                heading = heading_of_pass(direction, lat)
        if heading is not None:
            toward = (heading + (90 if _side(text) == "L" else -90)) % 360
    if toward is None:
        return None
    both = near is not None and far is not None and near != far
    return LookGeometry(toward=toward, incidence=centre, near=near if both else None, far=far if both else None, source="file")


def toward_from_columns(east: float, north: float) -> Optional[float]:
    """
    The direction to the satellite of an image in the radar's own geometry
    (columns = range, the nearest first, as SAR images placed by GCPs are):
    against the way the columns go on the ground (`east`, `north` per column).
    """
    if not (math.isfinite(east) and math.isfinite(north)) or (east == 0 and north == 0):
        return None
    return math.degrees(math.atan2(-east, -north)) % 360


# ---- a local metric frame ---------------------------------------------------------------------

#: Metres per degree of latitude (and of longitude at the equator), as the other tools use them.
M_LAT = 110_574.0
M_LON = 111_320.0


@dataclass(frozen=True)
class Frame:
    """East and north metres about a place (lon0, lat0), for small areas (an image)."""
    lon0: float
    lat0: float

    @property
    def m_lon(self) -> float:
        return M_LON * math.cos(math.radians(self.lat0))

    def metres(self, lon, lat):
        return (np.asarray(lon, dtype=np.float64) - self.lon0) * self.m_lon, (np.asarray(lat, dtype=np.float64) - self.lat0) * M_LAT

    def degrees(self, east, north):
        return self.lon0 + np.asarray(east, dtype=np.float64) / self.m_lon, self.lat0 + np.asarray(north, dtype=np.float64) / M_LAT


# ---- the tie points' correction ------------------------------------------------------------------

#: [a, b, c, d, e, f]: u = a + b·x + c·y, v = d + e·x + f·y.
IDENTITY = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
#: What the correction is, by the number of points.
KIND_NAMES = {0: "なし", 1: "平行移動", 2: "平行移動・回転・縮尺", 3: "アフィン"}


def kind_name(n: int) -> str:
    return KIND_NAMES[min(3, max(0, n))]


def apply_affine(t, x, y):
    return t[0] + t[1] * x + t[2] * y, t[3] + t[4] * x + t[5] * y


def fit_ground(src: Sequence[tuple[float, float]], dst: Sequence[tuple[float, float]]) -> tuple:
    """
    The correction taking `src` (true places, metres) to `dst` (where the
    model wants them): none for no point, a shift for 1, a similarity
    (shift, rotation, one scale) for 2, a least-squares affine for 3 or more
    (a similarity when the 3+ lie on a line).
    """
    n = min(len(src), len(dst))
    if n == 0:
        return IDENTITY
    x = np.array([p[0] for p in src[:n]], dtype=np.float64)
    y = np.array([p[1] for p in src[:n]], dtype=np.float64)
    u = np.array([p[0] for p in dst[:n]], dtype=np.float64)
    v = np.array([p[1] for p in dst[:n]], dtype=np.float64)
    if n == 1:
        return (float(u[0] - x[0]), 1.0, 0.0, float(v[0] - y[0]), 0.0, 1.0)
    cx, cy, cu, cv = x.mean(), y.mean(), u.mean(), v.mean()
    if n >= 3:
        m = np.stack([np.ones(n), x - cx, y - cy], axis=1)
        normal = m.T @ m
        scale = max(1.0, float(np.abs(normal).max()))
        if abs(np.linalg.det(normal / scale)) > 1e-9:
            su = np.linalg.solve(normal, m.T @ u)
            sv = np.linalg.solve(normal, m.T @ v)
            return (float(su[0] - su[1] * cx - su[2] * cy), float(su[1]), float(su[2]),
                    float(sv[0] - sv[1] * cx - sv[2] * cy), float(sv[1]), float(sv[2]))
    # Similarity: u + iv = (a + ib)(x + iy) + c, least squares about the centroids.
    zx = (x - cx) + 1j * (y - cy)
    zu = (u - cu) + 1j * (v - cv)
    denom = float(np.sum(np.abs(zx) ** 2))
    if denom < 1e-9:
        return (float(cu - cx), 1.0, 0.0, float(cv - cy), 0.0, 1.0)
    k = complex(np.sum(np.conj(zx) * zu) / denom)
    a, b = k.real, k.imag
    return (float(cu - (a * cx - b * cy)), a, -b, float(cv - (b * cx + a * cy)), b, a)


def describe_ground(t) -> str:
    """The correction in words: the shift at the origin, and rotation and scale when there are."""
    parts = [f"東 {t[0]:+.1f} m、北 {t[3]:+.1f} m"]
    rotation = math.degrees(math.atan2(t[4] - t[2], t[1] + t[5]))
    scale = math.sqrt(abs(t[1] * t[5] - t[2] * t[4]))
    if abs(rotation) >= 0.01:
        parts.append(f"回転 {rotation:+.2f}°")
    if abs(scale - 1) >= 1e-4:
        parts.append(f"縮尺 {scale:.4f}")
    return "・".join(parts)


# ---- the RPC model ---------------------------------------------------------------------------------

RPC_KEYS = ("LINE_OFF", "SAMP_OFF", "LAT_OFF", "LONG_OFF", "HEIGHT_OFF", "LINE_SCALE", "SAMP_SCALE", "LAT_SCALE", "LONG_SCALE",
            "HEIGHT_SCALE")


def rpc_terms(lon, lat, h) -> np.ndarray:
    """The 20 terms of RPC00B (GDAL's order) of normalized longitude L, latitude P and height H, … × 20."""
    L, P, H = (np.asarray(a, dtype=np.float64) for a in (lon, lat, h))
    one = np.ones_like(L)
    return np.stack([one, L, P, H, L * P, L * H, P * H, L * L, P * P, H * H, P * L * H, L ** 3, L * P * P, L * H * H, L * L * P,
                     P ** 3, P * H * H, L * L * H, P * P * H, H ** 3], axis=-1)


@dataclass
class RpcFit:
    """An RPC model as GDAL's metadata items (domain RPC), and how well it follows what it was fitted to (pixels)."""
    items: dict
    rms: float
    largest: float


def fit_rpc(lon, lat, h, x, y) -> RpcFit:
    """
    The RPC model (denominators 1: a third-order polynomial, enough for the
    smooth placing of a satellite image) through places (lon, lat, height)
    and the pixels x, y (GDAL's: the top-left corner at 0, 0) that show them.
    """
    lon, lat, h, x, y = (np.asarray(a, dtype=np.float64).ravel() for a in (lon, lat, h, x, y))
    keep = np.isfinite(lon) & np.isfinite(lat) & np.isfinite(h) & np.isfinite(x) & np.isfinite(y)
    lon, lat, h, x, y = lon[keep], lat[keep], h[keep], x[keep], y[keep]
    if lon.size < 20:
        raise ValueError("RPC の当てはめに点が足りない")

    def off_scale(a):
        lo, hi = float(a.min()), float(a.max())
        return (lo + hi) / 2, max((hi - lo) / 2, 1e-9)

    lon_off, lon_scale = off_scale(lon)
    lat_off, lat_scale = off_scale(lat)
    h_off, h_scale = off_scale(h)
    if h_scale < 1:
        h_scale = 1.0
    # RPC pixels have the first pixel's centre at 0, 0 (GDAL adds the half pixel).
    samp, line = x - 0.5, y - 0.5
    s_off, s_scale = off_scale(samp)
    l_off, l_scale = off_scale(line)
    terms = rpc_terms((lon - lon_off) / lon_scale, (lat - lat_off) / lat_scale, (h - h_off) / h_scale)
    cs, *_ = np.linalg.lstsq(terms, (samp - s_off) / s_scale, rcond=None)
    cl, *_ = np.linalg.lstsq(terms, (line - l_off) / l_scale, rcond=None)
    err = np.hypot((terms @ cs) * s_scale + s_off - samp, (terms @ cl) * l_scale + l_off - line)
    den = " ".join(["1"] + ["0"] * 19)
    items = {
        "LINE_OFF": repr(l_off), "SAMP_OFF": repr(s_off), "LAT_OFF": repr(lat_off), "LONG_OFF": repr(lon_off),
        "HEIGHT_OFF": repr(h_off), "LINE_SCALE": repr(l_scale), "SAMP_SCALE": repr(s_scale), "LAT_SCALE": repr(lat_scale),
        "LONG_SCALE": repr(lon_scale), "HEIGHT_SCALE": repr(h_scale),
        "LINE_NUM_COEFF": " ".join(repr(float(c)) for c in cl), "LINE_DEN_COEFF": den,
        "SAMP_NUM_COEFF": " ".join(repr(float(c)) for c in cs), "SAMP_DEN_COEFF": den,
    }
    return RpcFit(items, float(np.sqrt(np.mean(err ** 2))), float(err.max()))


def rpc_pixel(items: dict, lon, lat, h) -> tuple:
    """The pixel (GDAL's) an RPC model gives (numerators over denominators), for checks."""
    def f(k):
        return float(str(items[k]).split()[0])

    def coeffs(k):
        return np.array([float(v) for v in str(items[k]).split()], dtype=np.float64)

    t = rpc_terms((np.asarray(lon) - f("LONG_OFF")) / f("LONG_SCALE"), (np.asarray(lat) - f("LAT_OFF")) / f("LAT_SCALE"),
                  (np.asarray(h) - f("HEIGHT_OFF")) / f("HEIGHT_SCALE"))
    s = (t @ coeffs("SAMP_NUM_COEFF")) / (t @ coeffs("SAMP_DEN_COEFF"))
    line = (t @ coeffs("LINE_NUM_COEFF")) / (t @ coeffs("LINE_DEN_COEFF"))
    return s * f("SAMP_SCALE") + f("SAMP_OFF") + 0.5, line * f("LINE_SCALE") + f("LINE_OFF") + 0.5


# ---- the correction kept per image -------------------------------------------------------------

#: At most this many tie points (they are kept as text in one attribute).
MAX_POINTS = 40


@dataclass(frozen=True)
class TiePoint:
    """A landmark: its pixel in the image's file (GDAL's, x right, y down) and where it really is (WGS 84)."""
    x: float
    y: float
    lon: float
    lat: float


@dataclass(frozen=True)
class GeoFix:
    """
    One image file's correction: its tie points, whether the terrain is
    corrected with the DEM, the satellite's direction typed by hand (None:
    from the file), who made it and when, the tie points' residual then (m).
    """
    image_id: str
    image_file: str = ""
    points: tuple = ()
    terrain: bool = True
    toward: Optional[float] = None
    incidence: Optional[float] = None
    by: str = ""
    at: Optional[int] = None
    residual: Optional[float] = None
    oid: Optional[int] = None

    @property
    def key(self) -> tuple:
        return key_of(self.image_id, self.image_file)

    @property
    def typed_look(self) -> bool:
        return self.toward is not None and self.incidence is not None

    def look(self) -> Optional[LookGeometry]:
        """The direction typed by hand, as a geometry; None when it is the file's."""
        if not self.typed_look:
            return None
        return LookGeometry(toward=float(self.toward) % 360, incidence=float(self.incidence), source="typed")

    def is_empty(self) -> bool:
        """Nothing to correct: no point and no terrain."""
        return not self.points and not self.terrain

    def points_text(self) -> str:
        return json.dumps([[round(p.x, 3), round(p.y, 3), round(p.lon, 8), round(p.lat, 8)] for p in self.points],
                          separators=(",", ":"))

    def summary(self) -> str:
        """`地形（DEM）・目印 4 点（アフィン）`"""
        parts = []
        if self.terrain:
            parts.append("地形（DEM）")
        if self.points:
            parts.append(f"目印 {len(self.points)} 点（{kind_name(len(self.points))}）")
        return "・".join(parts) or "補正なし"

    def to_json(self) -> str:
        return json.dumps({"imageId": self.image_id, "imageFile": self.image_file, "points": json.loads(self.points_text()),
                           "terrain": self.terrain, "toward": self.toward, "incidence": self.incidence, "by": self.by,
                           "at": self.at, "residual": self.residual}, ensure_ascii=False)


def key_of(image_id: str, image_file: str) -> tuple:
    return (str(image_id or "").strip(), str(image_file or "").strip().casefold())


def points_of(value: Any) -> tuple:
    """Tie points from their text (or list): [[x, y, lon, lat], …]; bad entries skipped."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return ()
    if not isinstance(value, list):
        return ()
    out = []
    for p in value[:MAX_POINTS]:
        if isinstance(p, (list, tuple)) and len(p) >= 4 and all(is_number(v) for v in p[:4]):
            x, y, lon, lat = (float(v) for v in p[:4])
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                out.append(TiePoint(x, y, lon, lat))
    return tuple(out)


def fix_from_json(text: str) -> Optional[GeoFix]:
    try:
        v = json.loads(text or "")
    except ValueError:
        return None
    if not is_record(v) or not isinstance(v.get("imageId"), str):
        return None

    def number(k):
        return float(v[k]) if is_number(v.get(k)) else None

    return GeoFix(image_id=v["imageId"], image_file=str(v.get("imageFile") or ""), points=points_of(v.get("points")),
                  terrain=bool(v.get("terrain", True)), toward=number("toward"), incidence=number("incidence"),
                  by=str(v.get("by") or ""), at=ms_of(v.get("at")), residual=number("residual"))


# ---- the settings: `imageCorrections` and `dem` --------------------------------------------------------

#: The table's attributes by role.
DEFAULT_FIX_FIELDS = {
    "imageId": "IMAGE_ID", "imageFile": "IMAGE_FILE", "points": "TIE_POINTS", "terrain": "USE_DEM", "toward": "LOOK_AZIMUTH",
    "incidence": "INCIDENCE_ANGLE", "residual": "RESIDUAL_M", "by": "CORRECTED_BY", "at": "CORRECTED_AT",
}
#: Roles without which the table is not used.
NEEDED = ("imageId", "points")


def fix_source_of(value: Any, problems: list[str]):
    """`imageCorrections`: the URL of an editable table, or { url, fields, where, token }."""
    from .sources import esri_source_of

    return esri_source_of(value, "imageCorrections", DEFAULT_FIX_FIELDS, problems, editable=True)


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def fix_of_row(a: dict, f: dict, oid: str = "OBJECTID") -> GeoFix:
    def number(role):
        v = a.get(f[role])
        return float(v) if is_number(v) else None

    terrain = a.get(f["terrain"])
    return GeoFix(image_id=_text(a.get(f["imageId"])), image_file=_text(a.get(f["imageFile"])), points=points_of(a.get(f["points"])),
                  terrain=True if terrain is None else bool(int(terrain)) if is_number(terrain) else str(terrain).strip() not in ("", "0"),
                  toward=number("toward"), incidence=number("incidence"), residual=number("residual"), by=_text(a.get(f["by"])),
                  at=ms_of(a.get(f["at"])), oid=int(a[oid]) if is_number(a.get(oid)) else None)


def attributes_of(x: GeoFix, f: dict) -> dict:
    return {f["imageId"]: x.image_id, f["imageFile"]: x.image_file, f["points"]: x.points_text(), f["terrain"]: 1 if x.terrain else 0,
            f["toward"]: x.toward, f["incidence"]: x.incidence, f["residual"]: x.residual, f["by"]: x.by, f["at"]: x.at}


@dataclass(frozen=True)
class DemSettings:
    """
    `dem`: the heights the terrain is corrected with: an ArcGIS image service
    (…/ImageServer, read with exportImage for the image's area, kept on this
    PC) or a file; the geoid height (m) that turns its heights above sea level
    into the ellipsoidal heights images are placed with; the service's raster
    function (none: its values as they are); the most DEM pixels across.
    """
    url: str = ""
    file: str = ""
    token: Optional[str] = None
    geoid_height: float = 0.0
    rendering_rule: str = '{"rasterFunction":"None"}'
    mosaic_rule: str = ""
    max_size: int = 4096

    @property
    def configured(self) -> bool:
        return bool(self.url or self.file)

    @property
    def label(self) -> str:
        return self.url or self.file


IMAGE_SERVER = re.compile(r"/ImageServer/?$", re.IGNORECASE)


def dem_settings_of(value: Any, problems: list[str]) -> Optional[DemSettings]:
    """`dem`: an …/ImageServer URL or a file path, or { url | file, geoidHeight, renderingRule, mosaicRule, maxSize, token }."""
    if value is None:
        return None
    if isinstance(value, str):
        value = {"url": value} if value.lower().startswith(("http://", "https://")) else {"file": value}
    if not is_record(value):
        problems.append("dem はイメージサーバーの URL（…/ImageServer）か標高ファイルのパスで指定")
        return None
    url = str(value.get("url") or "").strip()
    file = str(value.get("file") or "").strip()
    if url and not IMAGE_SERVER.search(url.split("?", 1)[0]):
        problems.append("dem.url は …/ImageServer の URL で指定")
        url = ""
    if not url and not file:
        problems.append("dem は url（イメージサーバー）か file（標高ファイル）を指定")
        return None
    geoid = value.get("geoidHeight", 0)
    if not is_number(geoid) or not -150 <= float(geoid) <= 150:
        problems.append("dem.geoidHeight はジオイド高（m、-150〜150）で指定")
        geoid = 0
    rule = value.get("renderingRule", {"rasterFunction": "None"})
    rule = json.dumps(rule, ensure_ascii=False) if is_record(rule) else str(rule or "")
    mosaic = value.get("mosaicRule")
    mosaic = json.dumps(mosaic, ensure_ascii=False) if is_record(mosaic) else str(mosaic or "")
    size = value.get("maxSize", 4096)
    if not is_number(size) or not 256 <= int(size) <= 16384:
        problems.append("dem.maxSize は 256〜16384 の画素数で指定")
        size = 4096
    token = value.get("token")
    return DemSettings(url=url.rstrip("/"), file=file, token=token if isinstance(token, str) and token else None,
                       geoid_height=float(geoid), rendering_rule=rule, mosaic_rule=mosaic, max_size=int(size))


# ---- reading and writing the table ---------------------------------------------------------------

def _need(types: dict, source) -> None:
    from .esri import EsriError

    missing = [source.fields[r] for r in NEEDED if source.fields[r] not in types]
    if missing:
        raise EsriError(f"幾何補正の表に必要な項目がない（管理者に連絡: {'・'.join(missing)}）", code="E704")


def load_fixes(client, source, image_ids: Sequence[str], token: Optional[str] = None,
               cancelled: Optional[Callable[[], bool]] = None) -> list[GeoFix]:
    """The corrections of some images (all their files), the newest first."""
    from .esri import quote

    token = token or source.token
    info = client.layer(source.url, token, refresh=True)
    _need(info.field_types, source)
    f = source.fields
    ids = sorted({i for i in image_ids if i})
    if not ids:
        return []
    out: list[GeoFix] = []
    for start in range(0, len(ids), 100):
        part = ids[start:start + 100]
        where = f"{f['imageId']} IN ({','.join(quote(i) for i in part)})"
        if source.where:
            where = f"({source.where}) AND ({where})"
        rows = client.query_all(source.url, where, token, geometry=False, limit=10_000, cancelled=cancelled)
        out += [fix_of_row(r.get("attributes") or {}, f, info.object_id_field) for r in rows]
    return sorted(out, key=lambda x: -(x.at or 0))


def save_fix(client, source, fix: GeoFix, token: Optional[str] = None) -> GeoFix:
    """Writes an image file's correction: over its row when it has one (the first), else a new row."""
    from .esri import EsriError, fit_value

    token = token or source.token
    info = client.layer(source.url, token, refresh=True)
    types = info.field_types
    _need(types, source)
    same = [x for x in load_fixes(client, source, [fix.image_id], token) if x.key == fix.key and x.oid is not None]
    values = {k: fit_value(v, types[k]) for k, v in attributes_of(fix, source.fields).items() if k in types}
    oid = info.object_id_field
    if same:
        first = min(same, key=lambda x: x.oid)
        r = (client.apply_edits(source.url, updates=[{"attributes": {**values, oid: first.oid}}], token=token).update_results or [{}])[0]
        if not r.get("success"):
            raise EsriError(code="E305", detail="幾何補正の保存: " + ((r.get("error") or {}).get("description") or ""))
        extra = [x.oid for x in same if x.oid != first.oid]
        if extra:
            client.delete_features(source.url, extra, token)
        return replace(fix, oid=first.oid)
    r = (client.apply_edits(source.url, adds=[{"attributes": values}], token=token).add_results or [{}])[0]
    if not r.get("success"):
        raise EsriError(code="E305", detail="幾何補正の保存: " + ((r.get("error") or {}).get("description") or ""))
    return replace(fix, oid=int(r["objectId"]) if is_number(r.get("objectId")) else None)


def delete_fix(client, source, fix: GeoFix, token: Optional[str] = None) -> int:
    """Deletes an image file's correction (every row of it); how many rows went."""
    token = token or source.token
    rows = [x.oid for x in load_fixes(client, source, [fix.image_id], token) if x.key == fix.key and x.oid is not None]
    if not rows:
        return 0
    results = client.delete_features(source.url, rows, token)
    return sum(1 for r in results or [] if r.get("success"))


def newest_for(fixes: Sequence[GeoFix], image_id: str, image_file: str) -> Optional[GeoFix]:
    """The correction of an image file, of those read (its own file's, else one kept for the image without a file)."""
    key = key_of(image_id, image_file)
    own = [x for x in fixes if x.key == key]
    if own:
        return max(own, key=lambda x: x.at or 0)
    loose = [x for x in fixes if x.key == key_of(image_id, "")]
    return max(loose, key=lambda x: x.at or 0) if loose else None


@dataclass
class FixReport:
    """How a correction came out: what it did, the RPC model's fit (pixels), the tie points' residuals (m), the most the terrain moved (m)."""
    kind: str = ""
    ground: tuple = IDENTITY
    look: Optional[LookGeometry] = None
    terrain: bool = False
    rpc_rms: float = 0.0
    rpc_largest: float = 0.0
    residuals: list = field(default_factory=list)
    largest_move: float = 0.0
    heights: tuple = ()
    notes: list = field(default_factory=list)

    @property
    def residual(self) -> Optional[float]:
        """The tie points' RMS residual (m), None without points beyond what fits them exactly (1 or 2)."""
        if len(self.residuals) < 3:
            return None
        return float(math.sqrt(sum(r * r for r in self.residuals) / len(self.residuals)))

    def text(self) -> str:
        parts = []
        if self.terrain and self.look is not None:
            parts.append(f"地形: {self.look.text()}、最大 {self.largest_move:.0f} m 戻す")
        elif self.terrain:
            parts.append("地形: 画像の撮像モデル（RPC）と DEM")
        if self.residuals:
            parts.append(f"目印 {len(self.residuals)} 点（{self.kind}: {describe_ground(self.ground)}）")
            if self.residual is not None:
                parts.append(f"残差 {self.residual:.1f} m")
        return "、".join(parts) or "補正なし"
