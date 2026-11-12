"""
Simple orthorectification of an image file with GDAL, as the viewer's
簡易オルソ補正: the DEM is warped onto the image's grid (bilinear), and a
strip at a time each output pixel reads the image `(h − h0) / tan(e)` ground
metres away from itself, away from the satellite (core.simple_ortho), every
band. Ground metres are turned into the CRS's units where the strip lies.
The output is the image's own grid; what moves in from beyond its edge is
no data (or transparent, by an alpha band, when the image has no no-data
value).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from osgeo import gdal, osr

from ..core.simple_ortho import SensorView, displacement, is_imd_name, sample, sensor_view_from_text
from .change_io import _srs

#: Rows made at once.
STRIP = 256


class OrthoError(Exception):
    """A reason the image cannot be corrected (shown as it is)."""


@dataclass
class OrthoResult:
    width: int
    height: int
    reference: float
    #: Share of the pixels the DEM covered, 0–1.
    coverage: float
    #: Most ground metres a pixel moved.
    largest: float


def sensor_view_of(path: str) -> Optional[SensorView]:
    """The satellite's direction from the image's metadata (every domain GDAL reads) or a .IMD file beside it."""
    ds = gdal.Open(path)
    texts = []
    if ds is not None:
        for domain in [""] + [d for d in (ds.GetMetadataDomainList() or []) if d and not d.startswith("xml:")]:
            items = ds.GetMetadata(domain)
            if isinstance(items, dict):
                texts += [f"{k} = {v}" for k, v in items.items()]
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0].lower()
    try:
        for other in sorted(os.listdir(folder or ".")):
            if is_imd_name(other) and os.path.splitext(other)[0].lower() == stem:
                with open(os.path.join(folder, other), encoding="utf-8", errors="replace") as f:
                    texts.append(f.read())
    except OSError:
        pass
    return sensor_view_from_text("\n".join(texts))


def _dem_on_grid(ds, dem_path: str):
    dem = gdal.Open(dem_path)
    if dem is None:
        raise OrthoError("標高データを開けず")
    gt = ds.GetGeoTransform()
    bounds = (gt[0], gt[3] + ds.RasterYSize * gt[5], gt[0] + ds.RasterXSize * gt[1], gt[3])
    options = dict(format="VRT", outputBounds=bounds, width=ds.RasterXSize, height=ds.RasterYSize, resampleAlg="bilinear",
                   outputType=gdal.GDT_Float64, dstNodata=float("nan"))
    if ds.GetProjection():
        options["dstSRS"] = ds.GetProjection()
    no_data = dem.GetRasterBand(1).GetNoDataValue()
    if no_data is not None:
        options["srcNodata"] = no_data
    one = gdal.Translate("", dem, format="VRT", bandList=[1])
    out = gdal.Warp("", one, **options)
    if out is None:
        raise OrthoError("標高データを画像の格子に載せられず")
    out.keep = (dem, one)
    return out


def _metres_per_unit(ds, x: float, y: float) -> tuple[float, float]:
    """Ground metres per CRS unit east and north at (x, y)."""
    srs = _srs(ds)
    if srs is None:
        return 1.0, 1.0
    if srs.IsGeographic():
        return 111_320 * math.cos(math.radians(y)), 110_574.0
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to = osr.CoordinateTransformation(srs, wgs)
    d = abs(ds.GetGeoTransform()[1]) * 16
    lon0, lat0 = to.TransformPoint(x, y)[:2]
    lon1, lat1 = to.TransformPoint(x + d, y)[:2]
    lon2, lat2 = to.TransformPoint(x, y + d)[:2]

    def metres(lon_a, lat_a, lon_b, lat_b):
        return math.hypot((lon_b - lon_a) * 111_320 * math.cos(math.radians(lat_a)), (lat_b - lat_a) * 110_574)
    return metres(lon0, lat0, lon1, lat1) / d, metres(lon0, lat0, lon2, lat2) / d


def mean_height(ds, dem_path: str) -> Optional[float]:
    """The mean DEM height over the image (read reduced), None where the DEM covers none of it."""
    dem = _dem_on_grid(ds, dem_path)
    scale = max(1.0, math.sqrt(ds.RasterXSize * ds.RasterYSize / 1e6))
    w, h = max(1, round(ds.RasterXSize / scale)), max(1, round(ds.RasterYSize / scale))
    values = dem.GetRasterBand(1).ReadAsArray(buf_xsize=w, buf_ysize=h)
    return float(np.nanmean(values)) if np.isfinite(values).any() else None


def simple_orthorectify(path: str, dem_path: str, out_path: str, view: SensorView, reference: Optional[float] = None,
                        resample: str = "bilinear", progress: Optional[Callable[[float], None]] = None,
                        cancelled: Callable[[], bool] = lambda: False) -> OrthoResult:
    """Corrects `path` for the terrain of `dem_path` seen from `view` into the GeoTIFF `out_path`; `reference` None: the mean DEM height."""
    say = progress or (lambda _p: None)
    ds = gdal.Open(path)
    if ds is None:
        raise OrthoError("画像を開けず")
    gt = ds.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0:
        raise OrthoError("北が上でない（回転のある）画像は未対応")
    if reference is None:
        reference = mean_height(ds, dem_path)
        if reference is None:
            raise OrthoError("画像の範囲に標高データなし")
    dem_ds = _dem_on_grid(ds, dem_path)
    dem = dem_ds.GetRasterBand(1)
    width, height, count = ds.RasterXSize, ds.RasterYSize, ds.RasterCount
    no_data = ds.GetRasterBand(1).GetNoDataValue()
    alpha = no_data is None
    out = gdal.GetDriverByName("GTiff").Create(out_path, width, height, count + (1 if alpha else 0), ds.GetRasterBand(1).DataType,
                                               options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"] + (["ALPHA=YES"] if alpha else []))
    if out is None:
        raise OrthoError(f"{out_path} を作成できず")
    out.SetGeoTransform(gt)
    if ds.GetProjection():
        out.SetProjection(ds.GetProjection())
    for b in range(1, count + 1):
        src = ds.GetRasterBand(b)
        if src.GetNoDataValue() is not None:
            out.GetRasterBand(b).SetNoDataValue(src.GetNoDataValue())
        if src.GetDescription():
            out.GetRasterBand(b).SetDescription(src.GetDescription())
        out.GetRasterBand(b).SetColorInterpretation(src.GetColorInterpretation())
    if alpha:
        out.GetRasterBand(count + 1).SetColorInterpretation(gdal.GCI_AlphaBand)
    ex, ny = displacement(view)
    integer = ds.GetRasterBand(1).DataType not in (gdal.GDT_Float32, gdal.GDT_Float64)
    covered = 0
    largest = 0.0
    xs = np.arange(width) + 0.5
    for y in range(0, height, STRIP):
        if cancelled():
            out = None
            gdal.Unlink(out_path)
            raise OrthoError("中止")
        h = min(STRIP, height - y)
        heights = dem.ReadAsArray(0, y, width, h)
        known = np.isfinite(heights)
        covered += int(known.sum())
        rise = np.where(known, heights - reference, 0.0)
        largest = max(largest, float(np.abs(rise).max()) * math.hypot(ex, ny) if rise.size else 0.0)
        mx, my = _metres_per_unit(ds, gt[0] + width / 2 * gt[1], gt[3] + (y + h / 2) * gt[5])
        # In pixels: east is +x, north is −y (gt[5] < 0).
        px = xs[None, :] + rise * ex / mx / gt[1]
        py = (np.arange(h) + y + 0.5)[:, None] + rise * ny / my / gt[5]
        top = max(0, int(math.floor(py.min())) - 2)
        bottom = min(height, int(math.ceil(py.max())) + 2)
        valid = np.ones((h, width), bool)
        for b in range(1, count + 1):
            band = ds.GetRasterBand(b)
            if bottom <= top:
                values = np.full((h, width), math.nan)
            else:
                window = band.ReadAsArray(0, top, width, bottom - top).astype(np.float64)
                nd = band.GetNoDataValue()
                if nd is not None:
                    window[window == nd] = math.nan
                values = sample(window, px, py - top, resample)
            valid &= np.isfinite(values)
            fill = band.GetNoDataValue() if band.GetNoDataValue() is not None else 0
            if integer:
                values = np.rint(values)
            out.GetRasterBand(b).WriteArray(np.where(np.isfinite(values), values, fill), 0, y)
        if alpha:
            out.GetRasterBand(count + 1).WriteArray((valid * 255).astype(np.uint8), 0, y)
        say((y + h) / height)
    out = None
    return OrthoResult(width, height, reference, covered / (width * height), largest)
