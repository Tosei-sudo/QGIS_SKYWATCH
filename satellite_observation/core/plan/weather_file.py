"""
Cloud cover forecasts read from a file brought into a closed network
(`cloudForecast.file` / `cloudForecast.folder`): a GeoTIFF of GFS total cloud
cover, one band per forecast time (docs/cloud-forecast-file.md). The places
are taken to their 0.25° grid cells (`key_of`) and each cell's pixel is read
across the bands; the cover between bands is found as for the service.

GDAL (`osgeo`, which QGIS has) is imported only when a file is read.
"""
from __future__ import annotations

import math
import os
import re
import struct
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..util import time_of
from .weather import CloudForecastConfig, CloudForecasts, CloudSeries, key_of

#: `gfs_tcdc_YYYYMMDDHH.tif`: the forecast's initial time (UTC) in the name.
_NAMED = re.compile(r"^gfs_tcdc_(\d{4})(\d{2})(\d{2})(\d{2})\.tiff?$", re.IGNORECASE)
_TIF = re.compile(r"\.tiff?$", re.IGNORECASE)
_LEADING_INT = re.compile(r"^\s*(-?\d+)")


@dataclass
class CloudGrid:
    """What a forecast file holds, without its pixels."""
    #: GDAL's geotransform (north up: no rotation terms).
    transform: tuple
    width: int
    height: int
    #: The bands' times (ms), in band order (NaN for a band without one).
    times: list
    #: When the forecast was made (ms; NaN when the file does not say).
    issued: float = math.nan
    nodata: Optional[float] = None
    scale: float = 1.0
    offset: float = 0.0


def named_time(name: str) -> float:
    """The initial time (ms) in a `gfs_tcdc_YYYYMMDDHH.tif` name; NaN for other names."""
    m = _NAMED.match(name)
    return time_of(f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:00:00Z") if m else math.nan


def newest_file(folder: str, listdir: Callable[[str], list] = os.listdir,
                mtime: Callable[[str], float] = os.path.getmtime) -> Optional[str]:
    """
    The file of a folder to read: the `gfs_tcdc_YYYYMMDDHH.tif` with the
    latest time in its name, or else the `.tif` changed last; None when there
    is none.
    """
    names = [n for n in listdir(folder) if _TIF.search(n)]
    if not names:
        return None
    named = [(named_time(n), n) for n in names]
    named = [(t, n) for t, n in named if math.isfinite(t)]
    if named:
        return os.path.join(folder, max(named)[1])
    return os.path.join(folder, max(names, key=lambda n: mtime(os.path.join(folder, n))))


def band_time(metadata: dict, description: str = "") -> float:
    """
    A band's forecast time (ms): `VALID_TIME` (ISO 8601), else
    `GRIB_VALID_TIME` (seconds since 1970, as GDAL tells GRIB bands), else
    the description as ISO 8601; NaN when none says.
    """
    t = time_of(metadata.get("VALID_TIME", ""))
    if math.isfinite(t):
        return t
    m = _LEADING_INT.match(str(metadata.get("GRIB_VALID_TIME", "")))
    if m:
        return int(m[1]) * 1000.0
    return time_of(description or "")


def pixel_of(grid: CloudGrid, lon: float, lat: float) -> Optional[tuple[int, int]]:
    """The pixel (column, row) holding a place, the longitude taken round the globe; None outside the grid."""
    x0, dx, _, y0, _, dy = grid.transform
    if not dx or not dy:
        return None
    row = math.floor((lat - y0) / dy)
    if not 0 <= row < grid.height:
        return None
    for shift in (0, 360, -360):
        col = math.floor((lon + shift - x0) / dx)
        if 0 <= col < grid.width:
            return col, row
    return None


def series_of(grid: CloudGrid, values: Sequence[float]) -> Optional[CloudSeries]:
    """A pixel's series from its values in band order (no data and values outside 0–100 left out); None when empty."""
    pairs = []
    for t, v in zip(grid.times, values):
        if not math.isfinite(t) or v is None:
            continue
        v = float(v)
        if grid.nodata is not None and v == grid.nodata:
            continue
        v = v * grid.scale + grid.offset
        if math.isfinite(v) and -0.5 <= v <= 100.5:
            pairs.append((t, min(100.0, max(0.0, v))))
    if not pairs:
        return None
    pairs.sort()
    return CloudSeries([t for t, _ in pairs], [v for _, v in pairs])


def clouds_from_grid(grid: CloudGrid, places: Sequence[Sequence[float]],
                     sample: Callable[[int, int], Sequence[float]],
                     cancelled: Optional[Callable[[], bool]] = None) -> CloudForecasts:
    """The series of the places' grid cells, `sample(column, row)` giving a pixel's values in band order."""
    out = CloudForecasts()
    out.issued = grid.issued
    for p in places:
        key = key_of(p)
        if key in out:
            continue
        if cancelled and cancelled():
            break
        lat, lon = (float(x) for x in key.split(","))
        px = pixel_of(grid, lon, lat)
        series = series_of(grid, sample(*px)) if px else None
        if series:
            out[key] = series
    return out


def forecast_file(config: CloudForecastConfig) -> str:
    """The file to read for the setting; raises with the reason when there is none."""
    if config.file:
        if not os.path.isfile(config.file):
            raise ValueError(f"雲量予報のファイルがない: {config.file}")
        return config.file
    if not os.path.isdir(config.folder):
        raise ValueError(f"雲量予報のフォルダがない: {config.folder}")
    path = newest_file(config.folder)
    if not path:
        raise ValueError(f"雲量予報のフォルダに .tif がない: {config.folder}")
    return path


def read_clouds(config: CloudForecastConfig, places: Sequence[Sequence[float]],
                cancelled: Optional[Callable[[], bool]] = None) -> CloudForecasts:
    """The forecasts for places (longitude, latitude), by grid cell, from the forecast file (GDAL)."""
    from osgeo import gdal

    path = forecast_file(config)
    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
    except RuntimeError:  # GDAL raising (gdal.UseExceptions)
        ds = None
    if ds is None:
        raise ValueError(f"雲量予報のファイルを開けない: {os.path.basename(path)}")
    try:
        transform = tuple(ds.GetGeoTransform(can_return_null=True) or ())
        if len(transform) != 6 or transform[2] or transform[4]:
            raise ValueError(f"雲量予報のファイルが北が上の経緯度の格子でない: {os.path.basename(path)}")
        bands = [ds.GetRasterBand(i + 1) for i in range(ds.RasterCount)]
        times = [band_time(b.GetMetadata() or {}, b.GetDescription()) for b in bands]
        if not any(math.isfinite(t) for t in times):
            raise ValueError(f"雲量予報のファイルのバンドに時刻（VALID_TIME）がない: {os.path.basename(path)}")
        first = bands[0]
        meta = ds.GetMetadata() or {}
        issued = time_of(meta.get("INIT_TIME", ""))
        if not math.isfinite(issued):
            issued = named_time(os.path.basename(path))
        grid = CloudGrid(transform, ds.RasterXSize, ds.RasterYSize, times, issued,
                         first.GetNoDataValue(), first.GetScale() or 1.0, first.GetOffset() or 0.0)

        def sample(col: int, row: int) -> list:
            values = ds.ReadRaster(col, row, 1, 1, buf_type=gdal.GDT_Float64)
            return list(struct.unpack(f"{ds.RasterCount}d", values))

        return clouds_from_grid(grid, places, sample, cancelled)
    finally:
        ds = None
