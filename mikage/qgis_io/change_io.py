"""
Change detection of two image files with GDAL, as the viewer's 変化抽出
dialog: a band of the image before and one of the image after are put on one
grid (the finer image's, where the two overlap; the other is resampled and,
unlike the viewer, reprojected when its CRS differs), compared pixel by
pixel into a one-band Float32 GeoTIFF (NaN where either has no data), and
the pixels that changed more than the thresholds are outlined as polygons
(read reduced to at most 4 million pixels, as the viewer's): regions of
4-connected pixels, those smaller than the least size dropped and holes
that small filled (GDAL's sieve), with their direction, geodesic area and
pixel count.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from osgeo import gdal, ogr, osr

from ..core.change import ChangeMethod, Thresholds, change_mask, compare, spread_of, thresholds_of

#: Output rows made at once.
STRIP = 1024
#: Most pixels the outlines are traced over, and most polygons kept (the largest).
MAX_TRACE_PIXELS = 4_000_000
MAX_POLYGONS = 20_000
RESAMPLING = {"bilinear": "bilinear", "nearest": "near"}


class ChangeError(Exception):
    """A reason the two images cannot be compared (shown as it is)."""


@dataclass
class ChangeResult:
    width: int
    height: int
    #: The change image's values read reduced, and how many original pixels each stands for.
    values: np.ndarray = None
    scale: float = 1.0
    #: The reduced grid's geotransform and the result's WKT.
    transform: tuple = ()
    wkt: str = ""
    spread: float = 1.0
    thresholds: Optional[Thresholds] = None
    #: (ring list in the result's CRS, direction 'increase'/'decrease', pixels) per changed area, largest first per direction.
    polygons: list = field(default_factory=list)
    truncated: bool = False


def _srs(ds) -> Optional[osr.SpatialReference]:
    wkt = ds.GetProjection()
    if not wkt:
        return None
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _bounds(ds, to: Optional[osr.SpatialReference]) -> tuple:
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    steps = np.linspace(0, 1, 21)
    edge = [(s * w, 0) for s in steps] + [(s * w, h) for s in steps] + [(0, s * h) for s in steps] + [(w, s * h) for s in steps]
    pts = [(gt[0] + c * gt[1] + r * gt[2], gt[3] + c * gt[4] + r * gt[5]) for c, r in edge]
    src = _srs(ds)
    if to is not None and src is not None and not src.IsSame(to):
        t = osr.CoordinateTransformation(src, to)
        pts = [t.TransformPoint(x, y)[:2] for x, y in pts]
    xs = [p[0] for p in pts if math.isfinite(p[0])]
    ys = [p[1] for p in pts if math.isfinite(p[1])]
    if not xs or not ys:
        raise ChangeError("画像の範囲を座標変換できず")
    return min(xs), min(ys), max(xs), max(ys)


def pixel_size(ds) -> float:
    """The side of a pixel, in metres when the CRS says how (degrees at the image's middle latitude), else in its units."""
    gt = ds.GetGeoTransform()
    size = math.sqrt(abs(gt[1] * gt[5] - gt[2] * gt[4]))
    srs = _srs(ds)
    if srs is not None and srs.IsGeographic():
        lat = gt[3] + gt[5] * ds.RasterYSize / 2
        return size * 111_320 * math.sqrt(max(1e-6, math.cos(math.radians(lat))))
    if srs is not None and srs.IsProjected():
        return size * srs.GetLinearUnits()
    return size


def plan(before, after) -> tuple:
    """The grid compared on: the finer image (the reference) and the window of it the other covers, (ref, x0, y0, x1, y1)."""
    ref, other = (after, before) if pixel_size(after) < pixel_size(before) else (before, after)
    gt = ref.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0 or gt[1] <= 0 or gt[5] >= 0:
        raise ChangeError("北が上でない（回転のある）画像は未対応")
    xmin, ymin, xmax, ymax = _bounds(other, _srs(ref))
    x0 = max(0, math.floor((xmin - gt[0]) / gt[1] + 1e-6))
    x1 = min(ref.RasterXSize, math.ceil((xmax - gt[0]) / gt[1] - 1e-6))
    y0 = max(0, math.floor((ymax - gt[3]) / gt[5] + 1e-6))
    y1 = min(ref.RasterYSize, math.ceil((ymin - gt[3]) / gt[5] - 1e-6))
    if x1 <= x0 or y1 <= y0:
        raise ChangeError("2 つの画像の重なりなし")
    return ref, x0, y0, x1, y1


def describe(before_path: str, after_path: str) -> str:
    """What will be compared: `重なる範囲 1,200 × 800 px を比較。`"""
    before, after = gdal.Open(before_path), gdal.Open(after_path)
    if before is None or after is None:
        raise ChangeError("画像を開けず")
    _, x0, y0, x1, y1 = plan(before, after)
    return f"重なる範囲 {x1 - x0:,} × {y1 - y0:,} px を比較。"


def _on_grid(ds, band: int, bounds: tuple, width: int, height: int, wkt: str, resample: str):
    """The band warped onto the grid (a VRT), with its no data as NaN when read through `read_float`."""
    no_data = ds.GetRasterBand(band).GetNoDataValue()
    one = gdal.Translate("", ds, format="VRT", bandList=[band])
    options = dict(format="VRT", outputBounds=bounds, width=width, height=height, resampleAlg=RESAMPLING[resample],
                   outputType=gdal.GDT_Float64, dstNodata=float("nan"), multithread=True)
    if wkt:
        options["dstSRS"] = wkt
    if no_data is not None:
        options["srcNodata"] = no_data
    warped = gdal.Warp("", one, **options)
    if warped is None:
        raise ChangeError("画像の再サンプリングに失敗")
    warped.keep = one
    return warped


def change_files(before_path: str, before_band: int, after_path: str, after_band: int, out_path: str, method: ChangeMethod,
                 resample: str = "bilinear", outline: bool = True, threshold: Optional[float] = None, direction: str = "both",
                 min_pixels: int = 9, progress: Optional[Callable[[float, str], None]] = None,
                 cancelled: Callable[[], bool] = lambda: False) -> ChangeResult:
    """Compares the two bands into the GeoTIFF `out_path`; the reduced values, colours' spread and outlines in the result."""
    say = progress or (lambda _p, _t: None)
    before, after = gdal.Open(before_path), gdal.Open(after_path)
    if before is None or after is None:
        raise ChangeError("画像を開けず")
    for ds, band, what in ((before, before_band, "前"), (after, after_band, "後")):
        if not 1 <= band <= ds.RasterCount:
            raise ChangeError(f"{what}の画像にバンド {band} なし")
    ref, x0, y0, x1, y1 = plan(before, after)
    gt = ref.GetGeoTransform()
    width, height = x1 - x0, y1 - y0
    bounds = (gt[0] + x0 * gt[1], gt[3] + y1 * gt[5], gt[0] + x1 * gt[1], gt[3] + y0 * gt[5])
    wkt = ref.GetProjection()
    a = _on_grid(before, before_band, bounds, width, height, wkt, resample)
    b = _on_grid(after, after_band, bounds, width, height, wkt, resample)
    out = gdal.GetDriverByName("GTiff").Create(out_path, width, height, 1, gdal.GDT_Float32,
                                               options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3", "BIGTIFF=IF_SAFER"])
    if out is None:
        raise ChangeError(f"{out_path} を作成できず")
    transform = (bounds[0], gt[1], 0, bounds[3], 0, gt[5])
    out.SetGeoTransform(transform)
    if wkt:
        out.SetProjection(wkt)
    band_out = out.GetRasterBand(1)
    band_out.SetNoDataValue(float("nan"))
    band_out.SetDescription(method.label)
    for y in range(0, height, STRIP):
        if cancelled():
            out = None
            gdal.Unlink(out_path)
            raise ChangeError("中止")
        h = min(STRIP, height - y)
        band_out.WriteArray(compare(a.ReadAsArray(0, y, width, h), b.ReadAsArray(0, y, width, h), method), 0, y)
        say(80 * (y + h) / height, "比較中…")
    band_out.FlushCache()

    # The change image, read reduced: for the symmetric colours and the outlines.
    scale = max(1.0, math.sqrt(width * height / MAX_TRACE_PIXELS))
    w, h = max(1, round(width / scale)), max(1, round(height / scale))
    values = band_out.ReadAsArray(buf_xsize=w, buf_ysize=h, resample_alg=gdal.GRIORA_NearestNeighbour)
    out = None
    sx, sy = width / w, height / h
    result = ChangeResult(width, height, values, sx * sy, (bounds[0], gt[1] * sx, 0, bounds[3], 0, gt[5] * sy), wkt,
                          spread_of(values, method.center))
    if outline:
        say(85, "変化域をポリゴン化中…")
        result.thresholds = thresholds_of(values, method.center, threshold if threshold and threshold > 0 else None)
        ways = ["increase", "decrease"] if direction == "both" else [direction]
        least = max(1, round(max(1, int(min_pixels)) / (sx * sy)))
        for way in ways:
            found, truncated = outline_mask(change_mask(values, result.thresholds, way), result.transform, wkt, least)
            result.truncated |= truncated
            result.polygons += [(rings, way, round(pixels * sx * sy)) for rings, pixels in found]
    say(100, "")
    return result


def outline_mask(mask: np.ndarray, transform: tuple, wkt: str, least: int) -> tuple[list, bool]:
    """
    The regions of set pixels (4-connected) as polygons [(rings, pixels)],
    rings in the grid's CRS; regions and holes of fewer than `least` pixels
    dropped or filled; the largest MAX_POLYGONS kept.
    """
    h, w = mask.shape
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(transform)
    if wkt:
        mem.SetProjection(wkt)
    band = mem.GetRasterBand(1)
    band.WriteArray(mask)
    if least > 1:
        gdal.SieveFilter(band, None, band, least, 4)
    vectors = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = vectors.CreateLayer("change", srs=_srs(mem))
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0, [], callback=None)
    pixel_area = abs(transform[1] * transform[5])
    found = []
    for feature in layer:
        if feature.GetField("v") != 1:
            continue
        geometry = feature.GetGeometryRef()
        pixels = round(geometry.GetArea() / pixel_area)
        rings = [[(geometry.GetGeometryRef(i).GetX(k), geometry.GetGeometryRef(i).GetY(k)) for k in range(geometry.GetGeometryRef(i).GetPointCount())]
                 for i in range(geometry.GetGeometryCount())]
        found.append((rings, pixels))
    found.sort(key=lambda f: -f[1])
    truncated = len(found) > MAX_POLYGONS
    return found[:MAX_POLYGONS], truncated
