"""
クイック幾何補正 on a layer (core/geo_fix.py has the arithmetic): the image's
file is shown where it really is, for the terrain (a DEM) and the tie
points, without a copy of it.

How: the model of where the image shows each place (core.geo_fix) is fitted
as an RPC model of the file's own pixels and written into a VRT of the file
(its GCPs and geotransform taken out); GDAL warps that with the DEM onto the
map (a warped VRT, RPC_DEM), as it warps any RPC image, each time QGIS
draws; the layer is pointed at the warped VRT (in the QGIS profile's
mikage/geofix folder), keeping its CRS (that of the file's own placing), its
style, its correction (画像補正) and its 位置ずらし (shift_io: the shift is part of
the model, so the two make one warp). The file and the correction are kept
in the layer's custom properties (shift_io.SOURCE and FIX), so a new shift
or new points start from the file again.

What places the file (as QGIS does): GCPs (SAR images in the radar's
geometry: the terrain needs the satellite's direction, from the file's
metadata, the Sentinel-1 annotation, or typed); a geotransform (a SAR image
geocoded onto the ellipsoid: the same); an RPC model (its own geometry: the
DEM's heights go into it). An image with no placing at all is refused.

The pixels of the records (pixel_place) are found through the same RPC
model and DEM (`placement`), so they stay the file's own.
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence
from xml.etree import ElementTree

import numpy as np
from osgeo import gdal, osr

from ..core.errors import Coded
from ..core.geo_fix import (
    IDENTITY, M_LAT, M_LON, FixReport, Frame, GeoFix, LookGeometry, apply_affine, fit_ground, fit_rpc, fix_from_json, kind_name,
    look_from_text, toward_from_columns,
)
from .dem_io import Heights

#: The layer's correction: {"fix": GeoFix as JSON, "rpc": the RPC VRT, "dem": the DEM, "missing": height off the DEM, "geoid": N}.
FIX = "mikage/geoFix"
#: The RPC fit's grid: places across, heights.
GRID = 25
LEVELS = 5
#: How far outside the image (a share of its size) the fit still follows the model.
MARGIN = 0.15


class GeoFixError(Coded):
    """A reason the image cannot be corrected (shown as it is)."""


def fix_folder() -> str:
    from qgis.core import QgsApplication

    folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "mikage", "geofix")
    os.makedirs(folder, exist_ok=True)
    return folder


def _wgs84() -> osr.SpatialReference:
    s = osr.SpatialReference()
    s.ImportFromEPSG(4326)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


# ---- the file as its own georeferencing places it -------------------------------------------------

def metadata_text(ds, path: str = "") -> str:
    """Every metadata item of the file (all domains, the XML ones too) and the maker's small files beside it, as text."""
    texts = []
    for domain in [""] + [d for d in (ds.GetMetadataDomainList() or []) if d]:
        items = ds.GetMetadata(domain)
        if isinstance(items, dict):
            texts += [f"{k} = {v}" for k, v in items.items()]
        elif isinstance(items, list):
            texts += [str(v) for v in items]
    if path and os.path.exists(path):
        folder, name = os.path.split(path)
        stem = os.path.splitext(name)[0].lower()
        try:
            for other in sorted(os.listdir(folder or ".")):
                low = other.lower()
                if low != name.lower() and low.startswith(stem) and low.endswith((".xml", ".json", ".txt", ".imd")):
                    full = os.path.join(folder, other)
                    if os.path.getsize(full) <= 5_000_000:
                        with open(full, encoding="utf-8", errors="replace") as f:
                            texts.append(f.read())
        except OSError:
            pass
    return "\n".join(texts)


def sentinel1_look(path: str) -> Optional[LookGeometry]:
    """
    A Sentinel-1 SAFE measurement's incidence angles from its annotation
    (…/annotation/<same name>.xml): near and far across the columns (range);
    the direction from the columns (filled in by the caller).
    """
    folder, name = os.path.split(path)
    if os.path.basename(folder).lower() != "measurement":
        return None
    xml = os.path.join(os.path.dirname(folder), "annotation", os.path.splitext(name)[0] + ".xml")
    if not os.path.exists(xml):
        return None
    try:
        root = ElementTree.parse(xml).getroot()
    except (ElementTree.ParseError, OSError):
        return None
    rows = []
    for p in root.iter("geolocationGridPoint"):
        try:
            rows.append((float(p.findtext("pixel")), float(p.findtext("incidenceAngle"))))
        except (TypeError, ValueError):
            continue
    if not rows:
        return None
    first, last = min(r[0] for r in rows), max(r[0] for r in rows)
    near = float(np.mean([a for x, a in rows if x == first]))
    far = float(np.mean([a for x, a in rows if x == last]))
    return LookGeometry(toward=0.0, incidence=(near + far) / 2, near=near, far=far, source="file")


@dataclass
class Base:
    """The file: its size, what places it, in which CRS (WKT; WGS 84 for RPC), its RPC items, its metadata text."""
    path: str
    width: int
    height: int
    how: str
    wkt: str
    rpc: dict = field(default_factory=dict)
    gcp_heights: tuple = ()
    text: str = ""
    no_data: Optional[float] = None
    integer: bool = True

    def __post_init__(self):
        ds = gdal.Open(self.path)
        options = []
        if self.how == "rpc":
            height = self.rpc.get("HEIGHT_OFF")
            if height:
                options.append(f"RPC_HEIGHT={str(height).split()[0]}")
        self._ds = ds
        #: As QGIS places the file (its own transformer).
        self.placing = gdal.Transformer(ds, None, options)
        #: For an RPC file: the model with the height given (z).
        self.rpc_model = gdal.Transformer(ds, None, ["METHOD=RPC"]) if self.how == "rpc" else None
        srs = osr.SpatialReference()
        if self.wkt:
            srs.ImportFromWkt(self.wkt)
        else:
            srs = _wgs84()
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.srs = srs
        self.geographic = bool(srs.IsSame(_wgs84()))
        self._to = None if self.geographic else osr.CoordinateTransformation(_wgs84(), srs)
        self._from = None if self.geographic else osr.CoordinateTransformation(srs, _wgs84())
        self._href = _height_surface(self.gcp_heights)

    # ---- coordinates ----

    def to_crs(self, lon, lat):
        if self._to is None:
            return np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)
        return _points(self._to, lon, lat)

    def from_crs(self, x, y):
        if self._from is None:
            return np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        return _points(self._from, x, y)

    def ground(self, x, y):
        """Where the file places pixels (WGS 84)."""
        gx, gy = _transform(self.placing, False, x, y)
        return self.from_crs(gx, gy)

    def pixels_flat(self, lon, lat):
        """The pixels the file places at places (WGS 84)."""
        bx, by = self.to_crs(lon, lat)
        return _transform(self.placing, True, bx, by)

    def reference_height(self, x, y):
        """The ellipsoidal height the file placed a pixel at (its GCPs' heights, else 0 or the scene height it says)."""
        if self._href is not None:
            return self._href(np.asarray(x, dtype=np.float64) / max(1, self.width), np.asarray(y, dtype=np.float64) / max(1, self.height))
        return np.full(np.shape(x), _scene_height(self.text), dtype=np.float64)

    def footprint(self) -> Optional[tuple]:
        """The file's area (west, south, east, north; WGS 84) from points along its edges."""
        t = np.linspace(0, 1, 9)
        xs = np.concatenate([t * self.width, np.full(9, self.width), t[::-1] * self.width, np.zeros(9)])
        ys = np.concatenate([np.zeros(9), t * self.height, np.full(9, self.height), t[::-1] * self.height])
        lon, lat = self.ground(xs, ys)
        ok = np.isfinite(lon) & np.isfinite(lat)
        if ok.sum() < 3:
            return None
        return float(lon[ok].min()), float(lat[ok].min()), float(lon[ok].max()), float(lat[ok].max())

    def column_direction(self) -> Optional[float]:
        """For a file placed by GCPs: the direction to the satellite, against the columns' way on the ground."""
        cx, cy = self.width / 2, self.height / 2
        d = max(1.0, self.width / 100)
        lon, lat = self.ground(np.array([cx - d, cx + d]), np.array([cy, cy]))
        if not np.isfinite(lon).all() or not np.isfinite(lat).all():
            return None
        east = (lon[1] - lon[0]) * M_LON * math.cos(math.radians(lat.mean()))
        north = (lat[1] - lat[0]) * M_LAT
        return toward_from_columns(east, north)


def _height_surface(gcp_heights: tuple):
    """A smooth surface (second order, least squares) through the GCPs' heights over the image (u, v in 0–1), None when they are all 0."""
    if not gcp_heights:
        return None
    a = np.array(gcp_heights, dtype=np.float64)
    if not np.any(np.abs(a[:, 2]) > 1e-6):
        return None
    u, v, z = a[:, 0], a[:, 1], a[:, 2]
    if len(z) < 6:
        mean = float(z.mean())
        return lambda uu, vv: np.full(np.shape(uu), mean)
    m = np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=1)
    c, *_ = np.linalg.lstsq(m, z, rcond=None)
    return lambda uu, vv: c[0] + c[1] * uu + c[2] * vv + c[3] * uu * uu + c[4] * uu * vv + c[5] * vv * vv


def _scene_height(text: str) -> float:
    """The height a geocoded image says it was placed at (SICD's SCP HAE, a maker's average scene height), else 0 (the ellipsoid)."""
    for pattern in (r"<SCP>.*?<HAE>\s*(-?\d+(?:\.\d+)?)", r"(?:avg|average|mean|reference|scene|terrain)_?(?:scene_?)?height"
                    r"[\"'\s:=>]{1,6}(-?\d+(?:\.\d+)?)"):
        m = re.search(pattern, text or "", re.IGNORECASE | re.DOTALL)
        if m:
            value = float(m.group(1))
            if -500 <= value <= 9000:
                return value
    return 0.0


def _points(ct, x, y):
    x, y = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    if x.size == 0:
        return x, y
    out = np.array(ct.TransformPoints(np.stack([x.ravel(), y.ravel()], axis=1).tolist()), dtype=np.float64)
    return out[:, 0].reshape(x.shape), out[:, 1].reshape(x.shape)


def _transform(t, inverse: bool, x, y, z=None):
    """GDAL's transformer over arrays (NaN where it fails); inverse: places to pixels."""
    x, y = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    if x.size == 0:
        return x, y
    zz = np.zeros(x.size) if z is None else np.broadcast_to(np.asarray(z, dtype=np.float64), x.shape).ravel()
    ok_in = np.isfinite(x.ravel()) & np.isfinite(y.ravel()) & np.isfinite(zz)
    pts = np.stack([np.where(ok_in, x.ravel(), 0), np.where(ok_in, y.ravel(), 0), np.where(ok_in, zz, 0)], axis=1)
    out, ok = t.TransformPoints(1 if inverse else 0, pts.tolist())
    a = np.array(out, dtype=np.float64)
    bad = ~(np.array(ok, dtype=bool) & ok_in)
    a[bad] = np.nan
    return a[:, 0].reshape(x.shape), a[:, 1].reshape(x.shape)


def open_base(path: str) -> Base:
    """The file at `path` (GDAL's path: a VRT of a complex SAR image too)."""
    from .shift_io import placed_by

    try:
        ds = gdal.Open(path)
    except RuntimeError as e:
        raise GeoFixError("画像を開けない", code="E503", detail=f"{path}：{e}") from e
    if ds is None:
        raise GeoFixError("画像を開けない", code="E503", detail=path)
    how = placed_by(ds)
    if how == "pixels":
        raise GeoFixError("位置情報（GCP・ジオトランスフォーム・RPC）のない画像は幾何補正できない", code="E508")
    if how == "gcps":
        wkt = ds.GetGCPProjection() or ""
    elif how == "rpc":
        wkt = _wgs84().ExportToWkt()
    else:
        wkt = ds.GetProjection() or ""
    gcps = tuple((g.GCPPixel / max(1, ds.RasterXSize), g.GCPLine / max(1, ds.RasterYSize), g.GCPZ) for g in ds.GetGCPs() or ()) \
        if how == "gcps" else ()
    band = ds.GetRasterBand(1)
    real = path
    if path.lower().endswith(".vrt"):
        # A complex SAR image's amplitude VRT: the maker's files are beside the image itself.
        files = ds.GetFileList() or []
        real = next((f for f in files[1:] if os.path.exists(f)), path)
    base = Base(path=path, width=ds.RasterXSize, height=ds.RasterYSize, how=how, wkt=wkt, rpc=dict(ds.GetMetadata("RPC") or {}),
                gcp_heights=gcps, text=metadata_text(ds, real), no_data=band.GetNoDataValue(),
                integer=band.DataType not in (gdal.GDT_Float32, gdal.GDT_Float64))
    base.real = real
    return base


def file_look(base: Base) -> Optional[LookGeometry]:
    """The satellite's direction as the file (or what comes with it) says, None when it does not."""
    box = base.footprint()
    lat = (box[1] + box[3]) / 2 if box else None
    s1 = sentinel1_look(getattr(base, "real", base.path))
    if s1 is not None:
        toward = base.column_direction()
        if toward is not None:
            return LookGeometry(toward=toward, incidence=s1.incidence, near=s1.near, far=s1.far, source="file")
    look = look_from_text(base.text, lat)
    if look is not None and base.how != "gcps" and look.near is not None:
        # Across the columns only for an image in the radar's geometry: a geocoded one gets the middle angle.
        look = LookGeometry(toward=look.toward, incidence=look.incidence, source=look.source)
    if look is None and base.how == "gcps":
        # The direction from the columns, the angle not known: only a hint for typing.
        return None
    return look


def guessed_toward(base: Base) -> Optional[float]:
    """A hint for the direction to type: the file's, else the columns' (an image placed by GCPs)."""
    look = file_look(base)
    if look is not None:
        return look.toward
    return base.column_direction() if base.how == "gcps" else None


# ---- the model --------------------------------------------------------------------------------------

class Model:
    """Where the file shows places (WGS 84, DEM height): its pixels, for a correction, the DEM, the geoid height and a shift."""

    def __init__(self, base: Base, fix: GeoFix, heights: Optional[Heights], geoid: float = 0.0,
                 shift: tuple = (0.0, 0.0), look: Optional[LookGeometry] = None):
        self.base = base
        self.fix = fix
        self.heights = heights if fix.terrain else None
        self.geoid = geoid
        self.shift = (float(shift[0]), float(shift[1]))
        self.look = look
        self.terrain = bool(fix.terrain and heights is not None and (base.how == "rpc" or look is not None))
        box = base.footprint()
        if box is None:
            raise GeoFixError("画像の範囲が求められない", code="E508")
        self.box = box
        self.frame = Frame((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        self.ground_fix = IDENTITY
        self.residuals: list = []
        self._fit_points()

    def _dem(self, lon, lat):
        """The DEM's heights (NaN where it has none)."""
        if self.heights is None:
            return np.full(np.shape(lon), np.nan)
        return self.heights.at(lon, lat)

    def _towards(self, lon, lat, x, y, h):
        """The terrain's move (east, north metres) of places shown at pixels x, y, h their DEM height; 0 where unknown."""
        look = self.look
        inc = np.radians(np.clip(look.incidence_at(np.asarray(x) / max(1, self.base.width)), 5, 85))
        d = (np.asarray(h) + self.geoid - self.base.reference_height(x, y)) / np.tan(inc)
        d = np.where(np.isfinite(d), d, 0.0)
        ue, un = look.unit()
        return d * ue, d * un

    def _model_ground(self, x, y, h):
        """For pixels and their true heights: the places (WGS 84) the model takes before the tie points' correction."""
        if self.base.how == "rpc":
            z = np.where(np.isfinite(h), h + self.geoid, float(str(self.base.rpc.get("HEIGHT_OFF", "0")).split()[0])) \
                if self.terrain else float(str(self.base.rpc.get("HEIGHT_OFF", "0")).split()[0])
            return _transform(self.base.rpc_model, False, x, y, z)
        lon, lat = self.base.ground(x, y)
        if not self.terrain:
            return lon, lat
        de, dn = self._towards(lon, lat, x, y, h)
        return lon - de / (M_LON * np.cos(np.radians(lat))), lat - dn / M_LAT

    def _fit_points(self) -> None:
        points = self.fix.points
        if not points:
            return
        x = np.array([p.x for p in points])
        y = np.array([p.y for p in points])
        lon = np.array([p.lon for p in points])
        lat = np.array([p.lat for p in points])
        h = self._dem(lon, lat)
        mlon, mlat = self._model_ground(x, y, h)
        ok = np.isfinite(mlon) & np.isfinite(mlat)
        if not ok.any():
            raise GeoFixError("目印の点の位置が求められない", code="E508")
        se, sn = self.frame.metres(lon[ok], lat[ok])
        de, dn = self.frame.metres(mlon[ok], mlat[ok])
        src, dst = list(zip(se, sn)), list(zip(de, dn))
        self.ground_fix = fit_ground(src, dst)
        u, v = apply_affine(self.ground_fix, se, sn)
        self.residuals = [float(r) for r in np.hypot(u - de, v - dn)]

    def pixels(self, lon, lat, h=None):
        """The file's pixels (GDAL's) that show places (WGS 84; h their DEM heights, read from the DEM when None)."""
        lon, lat = np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)
        if h is None:
            h = self._dem(lon, lat)
        h = np.asarray(h, dtype=np.float64)
        if self.shift != (0.0, 0.0):
            bx, by = self.base.to_crs(lon, lat)
            lon, lat = self.base.from_crs(bx - self.shift[0], by - self.shift[1])
        e, n = self.frame.metres(lon, lat)
        e, n = apply_affine(self.ground_fix, e, n)
        ylon, ylat = self.frame.degrees(e, n)
        if self.base.how == "rpc":
            base_h = float(str(self.base.rpc.get("HEIGHT_OFF", "0")).split()[0])
            z = np.where(np.isfinite(h), h + self.geoid, base_h) if self.terrain else np.full(np.shape(ylon), base_h)
            bx, by = self.base.to_crs(ylon, ylat)
            return _transform(self.base.rpc_model, True, bx, by, z)
        x, y = self.base.pixels_flat(ylon, ylat)
        if not self.terrain:
            return x, y
        de, dn = self._towards(ylon, ylat, np.where(np.isfinite(x), x, self.base.width / 2), np.where(np.isfinite(y), y, 0), h)
        slon = ylon + de / (M_LON * np.cos(np.radians(ylat)))
        slat = ylat + dn / M_LAT
        return self.base.pixels_flat(slon, slat)

    def fit(self) -> tuple:
        """The RPC model of the file's pixels over its area and heights (core.geo_fix.fit_rpc), and the heights used (low, high)."""
        west, south, east, north = self.box
        mx, my = (east - west) * MARGIN + 0.002, (north - south) * MARGIN + 0.002
        box = (west - mx, south - my, east + mx, north + my)
        low = high = 0.0
        if self.terrain:
            got = self.heights.range_over(box)
            low, high = got if got is not None else (0.0, 0.0)
            low, high = low - 50, high + 50
        levels = np.linspace(low, high, LEVELS) if high > low else np.array([low])
        lon, lat, h = np.meshgrid(np.linspace(box[0], box[2], GRID), np.linspace(box[1], box[3], GRID), levels, indexing="ij")
        x, y = self.pixels(lon, lat, h)
        w, hh = self.base.width, self.base.height
        keep = np.isfinite(x) & np.isfinite(y) & (x > -MARGIN * w) & (x < (1 + MARGIN) * w) & (y > -MARGIN * hh) & (y < (1 + MARGIN) * hh)
        if keep.sum() < 40:
            raise GeoFixError("画像の範囲で補正のモデルが求められない", code="E508")
        return fit_rpc(lon[keep], lat[keep], h[keep], x[keep], y[keep]), (low, high)

    def largest_move(self) -> float:
        """The most the terrain moves a place over the image (metres), for the report."""
        if not self.terrain or self.base.how == "rpc" or self.look is None:
            return 0.0
        got = self.heights.range_over(self.box)
        if got is None:
            return 0.0
        inc = min(self.look.near or self.look.incidence, self.look.far or self.look.incidence, self.look.incidence)
        href = float(np.nanmean(self.base.reference_height(np.array([0, self.base.width]), np.array([0, self.base.height]))))
        return max(abs(got[0] + self.geoid - href), abs(got[1] + self.geoid - href)) / math.tan(math.radians(max(5, inc)))


# ---- the VRTs ------------------------------------------------------------------------------------------

@dataclass
class Built:
    rpc_path: str
    path: str
    report: FixReport
    options: list
    missing: float


def _rpc_vrt(source: str, items: dict, path: str) -> None:
    """A VRT of the file without its GCPs and geotransform, with the RPC model."""
    try:
        ds = gdal.Translate(path, source.split("|")[0], format="VRT")
    except RuntimeError as e:
        raise GeoFixError("画像の VRT を作れない", code="E503", detail=f"{source}：{e}") from e
    if ds is None:
        raise GeoFixError("画像の VRT を作れない", code="E503", detail=source)
    ds = None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"\s*<GCPList.*?</GCPList>", "", text, flags=re.DOTALL)
    text = re.sub(r"\s*<GCPList[^>]*/>", "", text)
    text = re.sub(r"\s*<GeoTransform>.*?</GeoTransform>", "", text, flags=re.DOTALL)
    text = re.sub(r"\s*<SRS[^>]*>.*?</SRS>", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r'\s*<Metadata domain="RPC">.*?</Metadata>', "", text, flags=re.DOTALL)
    from xml.sax.saxutils import escape

    mdi = "".join(f'\n    <MDI key="{k}">{escape(str(v))}</MDI>' for k, v in items.items())
    text = text.replace("<VRTRasterBand", f'<Metadata domain="RPC">{mdi}\n  </Metadata>\n  <VRTRasterBand', 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build(source: str, fix: GeoFix, dem_path: str = "", geoid: float = 0.0, shift: tuple = (0.0, 0.0),
          folder: Optional[str] = None) -> Built:
    """The corrected image of `source` (GDAL's path): its RPC VRT and the warped VRT to show; how it came out."""
    base = open_base(source.split("|")[0])
    look = fix.look() or (file_look(base) if base.how != "rpc" else None)
    heights = None
    notes = []
    if fix.terrain:
        if not dem_path:
            notes.append("DEM の設定（dem）がないので地形は補正しない")
        else:
            heights = Heights(dem_path)
        if base.how != "rpc" and look is None:
            notes.append("撮像方向（入射角・衛星の方角）が分からないので地形は補正しない（パネルで入力）")
    model = Model(base, fix, heights, geoid, shift, look)
    rpc, (low, high) = model.fit()
    folder = folder or fix_folder()
    name = uuid.uuid4().hex
    rpc_path = os.path.join(folder, f"{name}.rpc.vrt")
    path = os.path.join(folder, f"{name}.vrt")
    _rpc_vrt(source, rpc.items, rpc_path)
    missing = (low + high) / 2 if model.terrain else 0.0
    options = ["METHOD=RPC"]
    if model.terrain:
        options += [f"RPC_DEM={os.path.abspath(heights.path)}", "RPC_DEMINTERPOLATION=bilinear", f"RPC_DEM_MISSING_VALUE={missing!r}",
                    "RPC_DEM_APPLY_VDATUM_SHIFT=FALSE"]
    no_data = base.no_data
    if no_data is None:
        no_data = 0 if base.integer else float("nan")
    try:
        out = gdal.Warp(path, rpc_path, format="VRT", dstSRS=base.srs.ExportToWkt(), transformerOptions=options, resampleAlg="near",
                        srcNodata=base.no_data, dstNodata=no_data)
    except RuntimeError as e:
        raise GeoFixError("補正した画像（VRT）を作れない", code="E508", detail=str(e)) from e
    if out is None:
        raise GeoFixError("補正した画像（VRT）を作れない", code="E508")
    out = None
    report = FixReport(kind=kind_name(len(fix.points)), ground=model.ground_fix, look=look if model.terrain else None,
                       terrain=model.terrain, rpc_rms=rpc.rms, rpc_largest=rpc.largest, residuals=model.residuals,
                       largest_move=model.largest_move(), heights=(low, high) if model.terrain else (), notes=notes)
    return Built(rpc_path=rpc_path, path=path, report=report, options=options, missing=missing)


# ---- on a layer -----------------------------------------------------------------------------------------

def record_of(layer) -> Optional[dict]:
    """The layer's correction record (FIX), None when it has none."""
    try:
        text = layer.customProperty(FIX)
    except (AttributeError, RuntimeError):
        return None
    if not text:
        return None
    try:
        value = json.loads(str(text))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def is_fixed(layer) -> bool:
    return record_of(layer) is not None


def fix_of(layer) -> Optional[GeoFix]:
    """The correction shown on the layer, None when it has none."""
    record = record_of(layer)
    return fix_from_json(json.dumps(record.get("fix"))) if record and isinstance(record.get("fix"), dict) else None


def report_of(layer) -> dict:
    record = record_of(layer)
    return dict(record.get("report") or {}) if record else {}


def placement(layer) -> Optional[tuple]:
    """For pixel_place: (the RPC VRT, GDAL's transformer options) of a corrected layer, None for another."""
    record = record_of(layer)
    if not record or not record.get("rpc") or not os.path.exists(str(record["rpc"])):
        return None
    # (A pixel's place is GDAL's iteration over the DEM: to a thousandth of a pixel, not its 0.1.)
    return str(record["rpc"]), tuple(str(o) for o in record.get("options") or ()) + ("RPC_PIXEL_ERROR_THRESHOLD=0.001",)


def ours(path: str) -> bool:
    try:
        return os.path.abspath(path).startswith(os.path.abspath(fix_folder()))
    except OSError:
        return False


def forget_files(path: str) -> None:
    """A corrected image's VRTs, once not shown."""
    if not path or not ours(path):
        return
    for p in (path, path[:-4] + ".rpc.vrt" if path.endswith(".vrt") else ""):
        if p:
            try:
                os.remove(p)
            except OSError:
                pass


def apply(layer, fix: GeoFix, dem_path: str = "", geoid: float = 0.0) -> FixReport:
    """
    Shows the layer corrected (main thread: reads only local files): the
    file's correction with its 位置ずらし kept; FixReport. GeoFixError when
    it cannot be made.
    """
    from . import shift_io

    source = shift_io.original_source(layer)
    shift = shift_io.get_shift(layer)
    built = build(source, fix, dem_path, geoid, shift)
    report = built.report
    record = {"fix": json.loads(fix.to_json()), "rpc": built.rpc_path, "dem": dem_path, "geoid": geoid, "options": built.options,
              "missing": built.missing, "report": {"text": report.text(), "residual": report.residual, "rms": report.rpc_rms,
                                                   "notes": report.notes, "terrain": report.terrain}}
    shift_io.point_at(layer, built.path, source, shift, {FIX: json.dumps(record, ensure_ascii=False)})
    return report


def reshift(layer, dx: float, dy: float) -> None:
    """A corrected layer moved by 位置ずらし: the same correction with the new shift."""
    from . import shift_io

    record = record_of(layer) or {}
    fix = fix_of(layer)
    if fix is None:
        return
    source = shift_io.original_source(layer)
    built = build(source, fix, str(record.get("dem") or ""), float(record.get("geoid") or 0.0), (dx, dy))
    record.update(rpc=built.rpc_path, options=built.options, missing=built.missing)
    shift_io.point_at(layer, built.path, source, (dx, dy), {FIX: json.dumps(record, ensure_ascii=False)})


def remove(layer) -> None:
    """The layer shown as its file (and its 位置ずらし) places it again."""
    from . import shift_io

    if not is_fixed(layer):
        return
    source = shift_io.original_source(layer)
    dx, dy = shift_io.get_shift(layer)
    target = source if not dx and not dy else shift_io.moved_vrt(source, dx, dy)
    shift_io.point_at(layer, target, source, (dx, dy), {FIX: None})


def pixel_at(layer, lon: float, lat: float) -> Optional[tuple]:
    """The file's pixel shown at a place (WGS 84) on the layer as it is now (corrected or not), None when not found."""
    from .pixel_place import placement_of

    p = placement_of(layer)
    return p.pixel(lon, lat) if p is not None else None
