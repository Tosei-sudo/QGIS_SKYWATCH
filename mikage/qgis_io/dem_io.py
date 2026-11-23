"""
The DEM of クイック幾何補正 (`dem`, core/geo_fix.py): the heights over an area as
a GeoTIFF on this PC (WGS 84, float32, no data -32768), read from an ArcGIS
image service with exportImage (tsudo 2026-11-23: 「DEMはエスリのイメージサーバーを
使えること」) or taken from a file.

From the service, the area is read in tiles no larger than the service gives
at once (its maxImageWidth・maxImageHeight, at most 2048), at its own pixel
size or coarser, so that the whole is at most `maxSize` pixels across; the
values as they are (the raster function "None", unless the settings file
says another), bilinear. The file is kept by the service, the area (snapped
outward to a grid of the pixel size, so that a nearby image finds it) and the
pixel size, and read again from there; GDAL's warp reads it each time the
image is drawn, so it must stay (in the QGIS profile's mikage/dem folder).

Off the main thread (network.py): `fetch` and `ask` are given by the caller.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from typing import Callable, Optional, Sequence

import numpy as np
from osgeo import gdal, osr

from ..core.errors import Coded
from ..core.geo_fix import DemSettings

#: At most this many pixels across one exportImage.
TILE = 2048
NO_DATA = -32768.0
#: Geographic coordinate systems the services are often in (their pixel size is then in degrees).
GEOGRAPHIC = {4326, 4612, 6668, 4269, 4258, 4019, 4301}


class DemError(Coded):
    """A reason the heights cannot be had (shown as it is)."""


def dem_folder() -> str:
    from qgis.core import QgsApplication

    folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "mikage", "dem")
    os.makedirs(folder, exist_ok=True)
    return folder


def _wgs84() -> osr.SpatialReference:
    s = osr.SpatialReference()
    s.ImportFromEPSG(4326)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def service_pixel_degrees(info: dict) -> Optional[float]:
    """The service's own pixel in degrees (from its pixelSizeX・Y and spatialReference), None when it does not say."""
    sizes = [float(info.get(k)) for k in ("pixelSizeX", "pixelSizeY") if isinstance(info.get(k), (int, float)) and info.get(k) > 0]
    if not sizes:
        return None
    size = min(sizes)
    sr = info.get("spatialReference") or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid in GEOGRAPHIC or (isinstance(sr.get("wkt"), str) and sr["wkt"].upper().startswith(("GEOGCS", "GEOGCRS"))):
        return size
    # Metres (most projected services): at the equator; finer than needed elsewhere, never coarser.
    return size / 111_320.0


def grid_for(box: Sequence[float], pixel: float, max_size: int) -> tuple:
    """The area snapped outward to multiples of 64 pixels of `pixel` degrees (coarser if it would be over `max_size` across): (west, south, east, north, pixel, cols, rows)."""
    west, south, east, north = box
    span = max(east - west, north - south, 1e-6)
    pixel = max(pixel, span / max_size)
    # A round size (powers of 2 of a second of arc), so that areas near one another share a file.
    second = 1 / 3600
    pixel = second * 2 ** math.ceil(math.log2(max(pixel / second, 1 / 64)))
    step = pixel * 64
    west, south = math.floor(west / step) * step, math.floor(south / step) * step
    east, north = math.ceil(east / step) * step, math.ceil(north / step) * step
    cols, rows = max(1, round((east - west) / pixel)), max(1, round((north - south) / pixel))
    return west, south, east, north, pixel, cols, rows


def cache_path(settings: DemSettings, grid: tuple, folder: Optional[str] = None) -> str:
    key = json.dumps([settings.url, settings.rendering_rule, settings.mosaic_rule, [round(v, 9) for v in grid[:5]]])
    return os.path.join(folder or dem_folder(), hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".tif")


def _tile_values(body: bytes, west: float, south: float, east: float, north: float, cols: int, rows: int) -> np.ndarray:
    """An exportImage answer (a TIFF) on the tile's grid, NaN where it has no value (DemError for an error answer)."""
    if body[:1] in (b"{", b"<") or not body:
        try:
            error = json.loads(body.decode("utf-8-sig")).get("error") or {}
            said = f"{error.get('code', '')} {error.get('message', '')} {' '.join(map(str, error.get('details') or []))}".strip()
        except (ValueError, UnicodeDecodeError, AttributeError):
            said = body[:200].decode("utf-8", errors="replace")
        raise DemError("イメージサーバーが標高を返さない", code="E509", detail=said)
    name = f"/vsimem/mikage-dem-{uuid.uuid4().hex}.tif"
    gdal.FileFromMemBuffer(name, body)
    try:
        ds = gdal.Open(name)
        if ds is None:
            raise DemError("イメージサーバーの応答を標高として読めない", code="E509")
        band = ds.GetRasterBand(1)
        nd = band.GetNoDataValue()
        if ds.GetGeoTransform(can_return_null=True) is None or not ds.GetProjection():
            # Without georeferencing: the tile asked for, as it is.
            values = band.ReadAsArray(buf_xsize=cols, buf_ysize=rows).astype(np.float64)
        else:
            out = gdal.Warp("", ds, format="MEM", outputBounds=(west, south, east, north), width=cols, height=rows,
                            dstSRS=_wgs84().ExportToWkt(), resampleAlg="bilinear", outputType=gdal.GDT_Float64,
                            srcNodata=nd if nd is not None else None, dstNodata=float("nan"))
            values = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
        if nd is not None:
            values[values == nd] = np.nan
        values[(values < -1000) | (values > 10000)] = np.nan
        return values
    finally:
        gdal.Unlink(name)


def fetch_dem(settings: DemSettings, box: Sequence[float], ask: Callable[[str, dict], dict], post: Callable[[str, dict], bytes],
              token: Optional[str] = None, folder: Optional[str] = None, cancelled: Callable[[], bool] = lambda: False,
              progress: Callable[[float], None] = lambda _p: None) -> str:
    """
    The heights over `box` (west, south, east, north; WGS 84) as a GeoTIFF
    on this PC; its path. `ask(url, params)` gives a service's JSON (Esri
    client), `post(url, form)` an answer's bytes.
    """
    if settings.file:
        if not os.path.exists(settings.file) and "://" not in settings.file and not settings.file.startswith("/vsi"):
            raise DemError("標高ファイルが見つからない", code="E509", detail=settings.file)
        return settings.file
    url = settings.url
    try:
        info = ask(url, {})
    except Exception as error:  # noqa: BLE001 (said as it is)
        raise DemError("イメージサーバーに接続できない", code="E509", detail=f"{url}：{error}") from error
    own = service_pixel_degrees(info) or 1 / 3600
    grid = grid_for(box, own, settings.max_size)
    path = cache_path(settings, grid, folder)
    if os.path.exists(path) and gdal.Open(path) is not None:
        return path
    west, south, east, north, pixel, cols, rows = grid
    tile = max(256, min(TILE, int(info.get("maxImageWidth") or TILE), int(info.get("maxImageHeight") or TILE)))
    values = np.full((rows, cols), np.nan, dtype=np.float32)
    pieces = [(x0, y0, min(cols, x0 + tile), min(rows, y0 + tile)) for y0 in range(0, rows, tile) for x0 in range(0, cols, tile)]
    for i, (x0, y0, x1, y1) in enumerate(pieces):
        if cancelled():
            raise DemError("中止")
        tw, ts = west + x0 * pixel, north - y1 * pixel
        te, tn = west + x1 * pixel, north - y0 * pixel
        form = {"bbox": f"{tw!r},{ts!r},{te!r},{tn!r}", "bboxSR": "4326", "imageSR": "4326", "size": f"{x1 - x0},{y1 - y0}",
                "format": "tiff", "pixelType": "F32", "noData": repr(NO_DATA), "noDataInterpretation": "esriNoDataMatchAny",
                "interpolation": "RSP_BilinearInterpolation", "f": "image"}
        if settings.rendering_rule:
            form["renderingRule"] = settings.rendering_rule
        if settings.mosaic_rule:
            form["mosaicRule"] = settings.mosaic_rule
        if token:
            form["token"] = token
        try:
            body = post(f"{url}/exportImage", form)
        except Exception as error:  # noqa: BLE001
            raise DemError("イメージサーバーから標高を取得できない", code="E509", detail=f"{url}：{error}") from error
        part = _tile_values(bytes(body), tw, ts, te, tn, x1 - x0, y1 - y0)
        part[part == NO_DATA] = np.nan
        values[y0:y1, x0:x1] = part.astype(np.float32)
        progress((i + 1) / len(pieces))
    if not np.isfinite(values).any():
        raise DemError("画像の範囲にイメージサーバーの標高なし", code="E509", detail=url)
    write_dem(path, values, (west, pixel, 0.0, north, 0.0, -pixel))
    return path


def write_dem(path: str, values: np.ndarray, geotransform: Sequence[float]) -> None:
    """A float32 GeoTIFF in WGS 84, no data -32768 (written beside, then put in place: a file half written is never read)."""
    part = f"{path}.{uuid.uuid4().hex[:8]}.part.tif"
    ds = gdal.GetDriverByName("GTiff").Create(part, values.shape[1], values.shape[0], 1, gdal.GDT_Float32,
                                              options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3"])
    if ds is None:
        raise DemError("標高の保存に失敗", code="E509", detail=path)
    ds.SetGeoTransform(list(geotransform))
    ds.SetProjection(_wgs84().ExportToWkt())
    band = ds.GetRasterBand(1)
    # (A number, not NaN: GDAL's RPC warp compares the DEM's values with it.)
    band.SetNoDataValue(NO_DATA)
    band.WriteArray(np.where(np.isfinite(values), values, NO_DATA).astype(np.float32))
    ds = None
    os.replace(part, path)


class Heights:
    """A DEM read whole (its area is an image's), its heights at places (WGS 84), bilinear; NaN off it or where it has none."""

    def __init__(self, path: str, geoid_height: float = 0.0):
        ds = gdal.Open(path)
        if ds is None:
            raise DemError("標高データを開けない", code="E509", detail=path)
        self.path = path
        self.geoid_height = geoid_height
        band = ds.GetRasterBand(1)
        self.values = band.ReadAsArray().astype(np.float64)
        nd = band.GetNoDataValue()
        if nd is not None and math.isfinite(nd):
            self.values[self.values == nd] = np.nan
        self.gt = ds.GetGeoTransform()
        srs = osr.SpatialReference()
        wkt = ds.GetProjection()
        self._to = None
        if wkt:
            srs.ImportFromWkt(wkt)
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not srs.IsSame(_wgs84()):
                self._to = osr.CoordinateTransformation(_wgs84(), srs)

    def at(self, lon, lat) -> np.ndarray:
        lon = np.asarray(lon, dtype=np.float64)
        lat = np.asarray(lat, dtype=np.float64)
        x, y = lon.ravel(), lat.ravel()
        if self._to is not None:
            pts = np.array(self._to.TransformPoints(np.stack([x, y], axis=1).tolist()))
            x, y = pts[:, 0], pts[:, 1]
        gt = self.gt
        det = gt[1] * gt[5] - gt[2] * gt[4]
        dx, dy = x - gt[0], y - gt[3]
        col = (gt[5] * dx - gt[2] * dy) / det
        row = (-gt[4] * dx + gt[1] * dy) / det
        from ..core.simple_ortho import sample

        return sample(self.values, col, row, "bilinear").reshape(lon.shape)

    def range_over(self, box: Sequence[float]) -> Optional[tuple[float, float]]:
        """The lowest and highest heights over an area (WGS 84 box), None where the DEM has none."""
        west, south, east, north = box
        lon, lat = np.meshgrid(np.linspace(west, east, 64), np.linspace(south, north, 64))
        h = self.at(lon, lat)
        if not np.isfinite(h).any():
            return None
        return float(np.nanmin(h)), float(np.nanmax(h))
