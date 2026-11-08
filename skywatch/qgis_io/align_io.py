"""
Automatic alignment of two image files with GDAL, as the viewer's 自動位置合わせ
dialog: a band of each is sampled (bilinear, NaN on no data) on the
reference's grid where the two overlap, first a coarse look at the whole
overlap (512 px across at most), then patches of 128 px at the reference's
resolution, each shifted by the coarse result; the matches give tie points
fitted as a shift or an affine transformation (core.align). The aligned copy
is the image to align with its georeferencing corrected by the inverse
transformation, warped onto the reference's grid lattice over where it
lands, every band kept.

Unlike the viewer, the image to align may be in another CRS: it is first
reprojected onto the reference's CRS (a VRT).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from osgeo import gdal

from ..core.align import Affine, TiePoint, invert_affine, phase_correlate, robust_fit
from .change_io import _bounds, _srs

#: Pixels across the coarse look, and across a patch; a match is kept when its peak stands this far above the rest.
COARSE = 512
PATCH = 128
MIN_STRENGTH = 1.3
RESAMPLING = {"bilinear": "bilinear", "nearest": "near"}


class AlignError(Exception):
    """A reason the images cannot be aligned (shown as it is)."""


@dataclass
class Alignment:
    """From where a point is (the reference's CRS) to where the image to align shows it; the tie points kept and tried; RMS in reference pixels."""
    transform: Affine
    ties: list = field(default_factory=list)
    tried: int = 0
    rms_pixels: float = math.nan
    pixel: float = 1.0
    wkt: str = ""


def _largest_power_of_two(v: float) -> int:
    return 2 ** math.floor(math.log2(max(1, v)))


def _in_crs(ds, wkt: str):
    """The image in the CRS `wkt` (itself when it already is)."""
    src = _srs(ds)
    if not wkt or src is None:
        return ds
    from osgeo import osr

    to = osr.SpatialReference()
    to.ImportFromWkt(wkt)
    if src.IsSame(to):
        return ds
    out = gdal.Warp("", ds, format="VRT", dstSRS=wkt, resampleAlg="bilinear")
    if out is None:
        raise AlignError("合わせる画像を基準の座標系に再投影できず")
    return out


def overlap(reference, target) -> tuple:
    """The reference's pixels the target covers (x0, y0, x1, y1)."""
    gt = reference.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0 or gt[1] <= 0 or gt[5] >= 0:
        raise AlignError("北が上でない（回転のある）基準の画像は未対応")
    xmin, ymin, xmax, ymax = _bounds(target, _srs(reference))
    x0 = max(0, math.floor((xmin - gt[0]) / gt[1] + 1e-6))
    x1 = min(reference.RasterXSize, math.ceil((xmax - gt[0]) / gt[1] - 1e-6))
    y0 = max(0, math.floor((ymax - gt[3]) / gt[5] + 1e-6))
    y1 = min(reference.RasterYSize, math.ceil((ymin - gt[3]) / gt[5] - 1e-6))
    if x1 <= x0 or y1 <= y0:
        raise AlignError("2 つの画像の重なりなし")
    return x0, y0, x1, y1


def describe(reference_path: str, target_path: str) -> str:
    reference, target = gdal.Open(reference_path), gdal.Open(target_path)
    if reference is None or target is None:
        raise AlignError("画像を開けず")
    x0, y0, x1, y1 = overlap(reference, target)
    return f"重なる範囲 {x1 - x0:,} × {y1 - y0:,} px で照合。"


def sample(ds, band: int, x: float, y: float, rx: float, ry: float, width: int, height: int) -> np.ndarray:
    """A band on a grid (top-left corner x, y; pixel rx, ry < 0), bilinear, NaN outside and on no data."""
    one = gdal.Translate("", ds, format="VRT", bandList=[band])
    options = dict(format="MEM", outputBounds=(x, y + height * ry, x + width * rx, y), width=width, height=height,
                   resampleAlg="bilinear", outputType=gdal.GDT_Float64, dstNodata=float("nan"), errorThreshold=0)
    no_data = ds.GetRasterBand(band).GetNoDataValue()
    if no_data is not None:
        options["srcNodata"] = no_data
    out = gdal.Warp("", one, **options)
    return out.GetRasterBand(1).ReadAsArray()


def find_alignment(reference_path: str, reference_band: int, target_path: str, target_band: int, how: str = "shift",
                   patches: int = 4, progress: Optional[Callable[[float], None]] = None,
                   cancelled: Callable[[], bool] = lambda: False) -> Alignment:
    """How far the target is off the reference: a coarse shift over the whole overlap, then tie points in patches × patches patches."""
    say = progress or (lambda _p: None)
    reference, target = gdal.Open(reference_path), gdal.Open(target_path)
    if reference is None or target is None:
        raise AlignError("画像を開けず")
    for ds, band, what in ((reference, reference_band, "基準"), (target, target_band, "合わせる")):
        if not 1 <= band <= ds.RasterCount:
            raise AlignError(f"{what}画像にバンド {band} なし")
    wkt = reference.GetProjection()
    tgt = _in_crs(target, wkt)
    x0, y0, x1, y1 = overlap(reference, tgt)
    gt = reference.GetGeoTransform()
    rx, ry = gt[1], gt[5]
    width, height = x1 - x0, y1 - y0
    ox, oy = gt[0] + x0 * rx, gt[3] + y0 * ry
    side = min(width, height)
    n = min(COARSE, _largest_power_of_two(side))
    if n < 16:
        raise AlignError("重なる範囲が小さすぎ")
    f = side / n
    cx0, cy0 = ox + (width - side) / 2 * rx, oy + (height - side) / 2 * ry
    a = sample(reference, reference_band, cx0, cy0, rx * f, ry * f, n, n)
    b = sample(tgt, target_band, cx0, cy0, rx * f, ry * f, n, n)
    first = phase_correlate(a, b, n)
    # In whole reference pixels, so the patches read the image to align without blurring it (the patches find the fraction).
    matched = first is not None and first.strength >= MIN_STRENGTH
    coarse = (first.dx * rx * f, first.dy * ry * f) if matched else (0.0, 0.0)
    shift = (round(first.dx * f) * rx, round(first.dy * f) * ry) if matched else (0.0, 0.0)
    say(0.2)
    m = min(PATCH, _largest_power_of_two(side / 2))
    centers = []
    for j in range(patches):
        for i in range(patches):
            px, py = (i + 0.5) / patches * width, (j + 0.5) / patches * height
            if px - m / 2 < 0 or py - m / 2 < 0 or px + m / 2 > width or py + m / 2 > height:
                continue
            centers.append((px, py))
    ties = []
    for k, (px, py) in enumerate(centers):
        if cancelled():
            raise AlignError("中止")
        left, top = ox + (px - m / 2) * rx, oy + (py - m / 2) * ry
        pa = sample(reference, reference_band, left, top, rx, ry, m, m)
        pb = sample(tgt, target_band, left + shift[0], top + shift[1], rx, ry, m, m)
        c = phase_correlate(pa, pb, m)
        say(0.2 + 0.8 * (k + 1) / len(centers))
        if c is None or c.strength < MIN_STRENGTH or abs(c.dx) > m / 4 or abs(c.dy) > m / 4:
            continue
        x, y = ox + px * rx, oy + py * ry
        ties.append(TiePoint(x, y, x + shift[0] + c.dx * rx, y + shift[1] + c.dy * ry))
    pixel = abs(rx)
    fit = robust_fit(ties, "affine" if how == "affine" and len(ties) >= 6 else "shift", pixel / 2) if ties else None
    if fit is None:
        if first is None or first.strength < MIN_STRENGTH:
            raise AlignError("一致する模様なし（重なりが少ないか、模様の少ない画像）")
        return Alignment((coarse[0], 1, 0, coarse[1], 0, 1), [], len(centers), math.nan, pixel, wkt)
    transform, kept, residual = fit
    return Alignment(transform, kept, len(centers), residual / pixel, pixel, wkt)


def aligned_copy(reference_path: str, target_path: str, alignment: Alignment, out_path: str, resample: str = "bilinear") -> tuple[int, int]:
    """Writes the target, every band, corrected by the alignment, on the reference's grid lattice over where it lands; (width, height)."""
    reference, target = gdal.Open(reference_path), gdal.Open(target_path)
    if reference is None or target is None:
        raise AlignError("画像を開けず")
    back = invert_affine(alignment.transform)
    if back is None:
        raise AlignError("算出した変換が不正")
    tgt = _in_crs(target, alignment.wkt)
    corrected = gdal.Translate("", tgt, format="VRT")
    g = corrected.GetGeoTransform()
    a, b, c, d, e, f = back
    corrected.SetGeoTransform((a + b * g[0] + c * g[3], b * g[1] + c * g[4], b * g[2] + c * g[5],
                               d + e * g[0] + f * g[3], e * g[1] + f * g[4], e * g[2] + f * g[5]))
    rg = reference.GetGeoTransform()
    xmin, ymin, xmax, ymax = _bounds(corrected, None)
    left = rg[0] + math.floor((xmin - rg[0]) / rg[1] + 1e-6) * rg[1]
    right = rg[0] + math.ceil((xmax - rg[0]) / rg[1] - 1e-6) * rg[1]
    top = rg[3] + math.floor((ymax - rg[3]) / rg[5] + 1e-6) * rg[5]
    bottom = rg[3] + math.ceil((ymin - rg[3]) / rg[5] - 1e-6) * rg[5]
    width, height = round((right - left) / rg[1]), round((bottom - top) / rg[5])
    no_data = target.GetRasterBand(1).GetNoDataValue()
    options = dict(format="GTiff", outputBounds=(left, bottom, right, top), width=width, height=height,
                   resampleAlg=RESAMPLING[resample], multithread=True,
                   creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
    if alignment.wkt:
        options["dstSRS"] = alignment.wkt
    if no_data is not None:
        options.update(srcNodata=no_data, dstNodata=no_data)
    else:
        options["dstAlpha"] = True
    out = gdal.Warp(out_path, corrected, **options)
    if out is None:
        raise AlignError(f"{out_path} を作成できず")
    for i in range(1, target.RasterCount + 1):
        src = target.GetRasterBand(i)
        if src.GetDescription():
            out.GetRasterBand(i).SetDescription(src.GetDescription())
    out = None
    return width, height
