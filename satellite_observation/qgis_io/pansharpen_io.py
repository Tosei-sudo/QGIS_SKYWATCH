"""
Pan-sharpening of two image files with GDAL, as the viewer's パンシャープン
dialog: the multispectral image is resampled onto the panchromatic grid where
the two overlap, the weights and gains are fitted on the whole overlap (up to
2048 × 2048 px) or on 8 × 8 windows of 256 px spread over it, and the result
is made 2048 px a block at a time with that one model, so the blocks join
without seams. The result keeps the multispectral bands, sample type, no data
and alpha band, at the panchromatic resolution, as a tiled GeoTIFF with
overviews.

Unlike the viewer, the multispectral image may be in another CRS: GDAL
reprojects it onto the panchromatic grid.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from osgeo import gdal, osr

from ..core.enhance.pansharpen import PanSharpenModel, fit_model, pan_sharpen, valid_pixels

#: Output pixels made at once, and the fit on the whole overlap up to this many pixels.
BLOCK = 2048
FIT_WHOLE = 2048 * 2048
TILE = 256
RESAMPLING = {"bilinear": "bilinear", "bicubic": "cubic"}


class PanSharpenError(Exception):
    """A reason the two images cannot be pan-sharpened (shown as it is)."""


@dataclass
class Overlap:
    """The panchromatic pixels `[x0, x1) × [y0, y1)` under the multispectral image."""
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


def _srs(ds) -> Optional[osr.SpatialReference]:
    wkt = ds.GetProjection()
    if not wkt:
        return None
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _bounds(ds, to: Optional[osr.SpatialReference]) -> tuple:
    """The image's extent (xmin, ymin, xmax, ymax), in `to` (points along the edges, as reprojected)."""
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
        raise PanSharpenError("マルチスペクトル画像の範囲を座標変換できず")
    return min(xs), min(ys), max(xs), max(ys)


def overlap_of(pan, ms) -> Overlap:
    """The overlap on the panchromatic grid (as the viewer's planPanSharpen)."""
    gt = pan.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0 or gt[1] <= 0 or gt[5] >= 0:
        raise PanSharpenError("パンクロ画像が北が上の画像でない（回転あり）")
    xmin, ymin, xmax, ymax = _bounds(ms, _srs(pan))
    x0 = max(0, math.floor((xmin - gt[0]) / gt[1] + 1e-6))
    x1 = min(pan.RasterXSize, math.ceil((xmax - gt[0]) / gt[1] - 1e-6))
    y0 = max(0, math.floor((ymax - gt[3]) / gt[5] + 1e-6))
    y1 = min(pan.RasterYSize, math.ceil((ymin - gt[3]) / gt[5] - 1e-6))
    if x1 <= x0 or y1 <= y0:
        raise PanSharpenError("2 つの画像の重なりなし")
    return Overlap(x0, y0, x1, y1)


def blocks_of(o: Overlap, size: int = BLOCK) -> list:
    """Output windows (x0, y0, x1, y1) in panchromatic pixels, row by row."""
    return [(x, y, min(o.x1, x + size), min(o.y1, y + size)) for y in range(o.y0, o.y1, size) for x in range(o.x0, o.x1, size)]


def sample_windows(o: Overlap, count: int = 8, size: int = 256) -> list:
    """Windows spread evenly over the overlap, `count` across and down at most (the viewer's sampleWindows)."""
    def along(a: int, b: int) -> list:
        n = max(1, min(count, (b - a) // size))
        s = min(size, b - a)
        starts = [a + (math.floor((b - a - s) * i / (n - 1) + 0.5) if n > 1 else (b - a - s) // 2) for i in range(n)]
        return [(t, t + s) for t in starts]
    return [(x0, y0, x1, y1) for y0, y1 in along(o.y0, o.y1) for x0, x1 in along(o.x0, o.x1)]


def has_alpha(ds) -> bool:
    return ds.RasterCount > 1 and ds.GetRasterBand(ds.RasterCount).GetColorInterpretation() == gdal.GCI_AlphaBand


def _no_data(band) -> Optional[float]:
    v = band.GetNoDataValue()
    return None if v is None else v


def pan_sharpen_files(pan_path: str, ms_path: str, out_path: str, pan_band: int = 1, method: str = "gram-schmidt",
                      resample: str = "bilinear", strength: float = 1.0, weights="auto",
                      progress: Optional[Callable[[float, str], None]] = None,
                      cancelled: Callable[[], bool] = lambda: False) -> dict:
    """
    Pan-sharpens `ms_path` with band `pan_band` of `pan_path` into the GeoTIFF
    `out_path`. Returns {"width", "height", "bands", "model"}; raises
    PanSharpenError with a reason to show.
    """
    say = progress or (lambda _p, _t: None)
    pan = gdal.Open(pan_path)
    ms = gdal.Open(ms_path)
    if pan is None or ms is None:
        raise PanSharpenError("画像を開けず")
    if not 1 <= pan_band <= pan.RasterCount:
        raise PanSharpenError(f"パンクロ画像にバンド {pan_band} なし")
    o = overlap_of(pan, ms)
    gt = pan.GetGeoTransform()
    alpha = has_alpha(ms)
    count = ms.RasterCount
    if count - alpha < 1:
        raise PanSharpenError("マルチスペクトル画像に値のバンドなし")
    pan_b = pan.GetRasterBand(pan_band)
    pan_no = _no_data(pan_b)
    ms_no = _no_data(ms.GetRasterBand(1))
    dtype = ms.GetRasterBand(1).DataType
    bounds = (gt[0] + o.x0 * gt[1], gt[3] + o.y1 * gt[5], gt[0] + o.x1 * gt[1], gt[3] + o.y0 * gt[5])
    warp = dict(format="VRT", outputBounds=bounds, width=o.width, height=o.height, resampleAlg=RESAMPLING[resample],
                dstAlpha=alpha, multithread=True)
    if pan.GetProjection():
        warp["dstSRS"] = pan.GetProjection()
    if ms_no is not None:
        warp.update(srcNodata=ms_no, dstNodata=ms_no)
    elif gdal.GetDataTypeName(dtype).startswith("Float"):
        warp["dstNodata"] = float("nan")
    warped = gdal.Warp("", ms, **warp)
    if warped is None:
        raise PanSharpenError("マルチスペクトル画像の再サンプリングに失敗")

    def read(window) -> tuple:
        x0, y0, x1, y1 = window
        p = pan_b.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        m = warped.ReadAsArray(x0 - o.x0, y0 - o.y0, x1 - x0, y1 - y0)
        return p, m.reshape((count,) + p.shape)

    # The weights and gains, from the whole overlap when it is small, else from windows spread over it (one under another).
    say(0, "色の関係を調査中…")
    fit_on = [(o.x0, o.y0, o.x1, o.y1)] if o.width * o.height <= FIT_WHOLE else sample_windows(o)
    pieces = [read(w) for w in fit_on]
    fit_pan = np.concatenate([p.ravel() for p, _ in pieces])[:, None]
    fit_ms = np.concatenate([m.reshape(count, -1) for _, m in pieces], axis=1)[:, :, None]
    bands = [b for b in range(count) if not (alpha and b == count - 1)]
    valid = valid_pixels(fit_pan, fit_ms, bands, pan_no, ms_no, count - 1 if alpha else None)
    if not valid.any():
        raise PanSharpenError("両方に値のある画素なし")
    model: PanSharpenModel = fit_model(fit_pan, fit_ms, bands, valid, method, weights)
    del pieces, fit_pan, fit_ms

    options = ["TILED=YES", f"BLOCKXSIZE={TILE}", f"BLOCKYSIZE={TILE}", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"]
    if alpha:
        options.append("ALPHA=YES")
    out = gdal.GetDriverByName("GTiff").Create(out_path, o.width, o.height, count, dtype, options=options)
    if out is None:
        raise PanSharpenError(f"{out_path} を作成できず")
    out.SetGeoTransform((bounds[0], gt[1], 0, bounds[3], 0, gt[5]))
    if pan.GetProjection():
        out.SetProjection(pan.GetProjection())
    for b in range(count):
        src = ms.GetRasterBand(b + 1)
        dst = out.GetRasterBand(b + 1)
        if src.GetDescription():
            dst.SetDescription(src.GetDescription())
        dst.SetColorInterpretation(src.GetColorInterpretation())
    lows = [math.inf] * count
    highs = [-math.inf] * count
    no_data = None
    blocks = blocks_of(o)
    for i, window in enumerate(blocks):
        if cancelled():
            out = None
            gdal.Unlink(out_path)
            raise PanSharpenError("中止")
        p, m = read(window)
        sharp, no_data, _ = pan_sharpen(p, m, method, weights, bands, strength, model, pan_no, ms_no, alpha)
        out.WriteArray(sharp, window[0] - o.x0, window[1] - o.y0)
        for b in range(count):
            v = sharp[b]
            ok = np.isfinite(v) if v.dtype.kind == "f" else v != no_data
            if alpha and b != count - 1:
                ok &= sharp[count - 1] != 0
            if ok.any():
                lows[b] = min(lows[b], float(v[ok].min()))
                highs[b] = max(highs[b], float(v[ok].max()))
        say(90 * (i + 1) / len(blocks), f"パンシャープン中… {100 * (i + 1) // len(blocks)}%")
    for b in range(count):
        band = out.GetRasterBand(b + 1)
        if no_data is not None and not (alpha and b == count - 1):
            band.SetNoDataValue(no_data)
        if lows[b] <= highs[b]:
            band.SetMetadataItem("STATISTICS_MINIMUM", repr(lows[b]))
            band.SetMetadataItem("STATISTICS_MAXIMUM", repr(highs[b]))
    say(90, "オーバービューを作成中…")
    # Halves until the image fits in one tile.
    factors = []
    f = 2
    while max(o.width, o.height) * 2 / f > TILE:
        factors.append(f)
        f *= 2
    if factors:
        out.BuildOverviews("AVERAGE", factors)
    out.FlushCache()
    out = None
    say(100, "完了")
    return {"width": o.width, "height": o.height, "bands": count, "model": model}
