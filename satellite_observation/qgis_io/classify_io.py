"""
Land cover classification of an image file with GDAL, as the viewer's
土地被覆分類 dialog: the chosen bands are read a strip at a time on the
image's own grid, first to sample the pixels for k-means or to gather the
statistics of the training polygons (burnt into the strip, a pixel taken
when its centre is inside), then to put every pixel in a class (core.classify).
The result is one 8-bit GeoTIFF of class numbers (0 = no data) carrying the
classes' colours and names (a colour table and category names), with the
pixels and the area of each class. The classes can be outlined as polygons
(read reduced to at most 4 million pixels, as the viewer's), the regions
smaller than the least size dropped.

Unlike the viewer, no colour (RGBA) copy is written: QGIS shows the class
numbers in their colours itself.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np
from osgeo import gdal, ogr, osr

from ..core.classify import MAX_CLASSES, MAX_SAMPLES, ClassModel, ClassStats, accumulate, class_color, classify, k_means, random, sample_pixels, train_model
from .change_io import MAX_POLYGONS, MAX_TRACE_PIXELS, outline_mask

#: Rows read at once.
STRIP = 512


class ClassifyError(Exception):
    """A reason the image cannot be classified (shown as it is)."""


@dataclass
class TrainingClass:
    """Training polygons of one class, as WKT in the image's CRS."""
    name: str
    polygons: list = field(default_factory=list)


@dataclass
class ClassSummary:
    name: str
    color: tuple
    pixels: int = 0
    #: Square metres (by the pixels' sizes where they lie), NaN when the CRS cannot say.
    area: float = 0.0


@dataclass
class ClassifyResult:
    width: int
    height: int
    model: ClassModel
    classes: list = field(default_factory=list)


def read_strip(ds, bands: Sequence[int], y: int, height: int) -> np.ndarray:
    """The bands of rows y … y + height as (dims, n) float64, NaN on no data."""
    rows = []
    for b in bands:
        band = ds.GetRasterBand(b)
        values = band.ReadAsArray(0, y, ds.RasterXSize, height).astype(np.float64).ravel()
        no_data = band.GetNoDataValue()
        if no_data is not None:
            values[values == no_data] = np.nan
        rows.append(values)
    return np.vstack(rows)


def _training_layer(ds, training: Sequence[TrainingClass]):
    """The training polygons in an OGR memory layer, with their 1-based class in `label`; and their bounding box (xmin, ymin, xmax, ymax)."""
    source = ogr.GetDriverByName("Memory").CreateDataSource("")
    srs = None
    if ds.GetProjection():
        srs = osr.SpatialReference()
        srs.ImportFromWkt(ds.GetProjection())
    layer = source.CreateLayer("training", srs=srs, geom_type=ogr.wkbMultiPolygon)
    layer.CreateField(ogr.FieldDefn("label", ogr.OFTInteger))
    for k, c in enumerate(training):
        for wkt in c.polygons:
            geometry = ogr.CreateGeometryFromWkt(wkt)
            if geometry is None or geometry.IsEmpty():
                continue
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetGeometry(geometry)
            feature.SetField("label", k + 1)
            layer.CreateFeature(feature)
    if not layer.GetFeatureCount():
        return source, layer, None
    xmin, xmax, ymin, ymax = layer.GetExtent()
    return source, layer, (xmin, ymin, xmax, ymax)


def _labels(ds, layer, y: int, height: int) -> np.ndarray:
    """The training class of each pixel of rows y … y + height (0 where none)."""
    gt = ds.GetGeoTransform()
    mem = gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, height, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((gt[0] + y * gt[2], gt[1], gt[2], gt[3] + y * gt[5], gt[4], gt[5]))
    if ds.GetProjection():
        mem.SetProjection(ds.GetProjection())
    gdal.RasterizeLayer(mem, [1], layer, options=["ATTRIBUTE=label"])
    return mem.GetRasterBand(1).ReadAsArray().ravel()


def _rows_of(ds, box: tuple) -> tuple[int, int]:
    """The rows a bounding box covers (y0, y1)."""
    gt = ds.GetGeoTransform()
    ys = [(v - gt[3]) / gt[5] for v in (box[1], box[3])]
    return max(0, math.floor(min(ys))), min(ds.RasterYSize, math.ceil(max(ys)))


def classify_file(path: str, bands: Sequence[int], out_path: str, method: str, classes: int = 6,
                  training: Sequence[TrainingClass] = (), seed: int = 1, iterations: int = 30,
                  pixel_area: Optional[Callable[[float, float], float]] = None,
                  progress: Optional[Callable[[float, str], None]] = None,
                  cancelled: Callable[[], bool] = lambda: False) -> ClassifyResult:
    """
    Classifies the bands of `path` into `out_path`: `method` is 'kmeans'
    (into `classes` classes), 'distance' or 'likelihood' (with `training`).
    `pixel_area(x, y)` is the ground area of a pixel at grid point (x, y), m².
    """
    say = progress or (lambda _p, _t: None)
    ds = gdal.Open(path)
    if ds is None:
        raise ClassifyError("画像を開けず")
    if not bands:
        raise ClassifyError("バンドを 1 つ以上選択")
    for b in bands:
        if not 1 <= b <= ds.RasterCount:
            raise ClassifyError(f"画像にバンド {b} なし")
    width, height = ds.RasterXSize, ds.RasterYSize
    total = width * height
    dims = len(bands)
    strips = [(y, min(STRIP, height - y)) for y in range(0, height, STRIP)]

    def check():
        if cancelled():
            raise ClassifyError("中止")

    if method == "kmeans":
        if not 2 <= classes <= MAX_CLASSES:
            raise ClassifyError(f"クラス数は 2 から {MAX_CLASSES} まで")
        samples = []
        for i, (y, h) in enumerate(strips):
            check()
            # A seed of its own per strip, as the viewer's per block, so a strip's picks do not depend on the others.
            values = read_strip(ds, bands, y, h)
            samples.append(sample_pixels(values, math.ceil(MAX_SAMPLES * width * h / total), random(seed + y)))
            say(40 * (i + 1) / len(strips), "画素を抽出中…")
        everything = np.vstack(samples) if samples else np.zeros((0, dims))
        if not len(everything):
            raise ClassifyError("値のある画素なし")
        say(40, "クラスタリング中…")
        try:
            means = k_means(everything, classes, iterations, seed)
        except ValueError as e:
            raise ClassifyError(str(e))
        model = ClassModel("distance", means)
        names = [f"クラス {c + 1}" for c in range(classes)]
    else:
        if len(training) < 2:
            raise ClassifyError("学習用のクラスが 2 つ以上必要")
        if len(training) > MAX_CLASSES:
            raise ClassifyError(f"クラスは {MAX_CLASSES} まで（指定 {len(training)}）")
        source, layer, box = _training_layer(ds, training)
        y0, y1 = _rows_of(ds, box) if box else (0, 0)
        if y1 <= y0:
            raise ClassifyError("学習用のポリゴンが画像の範囲になし")
        stats = ClassStats.empty(len(training), dims)
        # Only the strips the polygons cross are read.
        crossed = [(y, h) for y, h in strips if y < y1 and y + h > y0]
        for i, (y, h) in enumerate(crossed):
            check()
            accumulate(stats, read_strip(ds, bands, y, h), _labels(ds, layer, y, h))
            say(40 * (i + 1) / len(crossed), "学習用の画素を集計中…")
        if not stats.counts.any():
            raise ClassifyError("学習用のポリゴンが画像の範囲になし")
        names = [c.name for c in training]
        try:
            model = train_model(stats, method, names)
        except ValueError as e:
            raise ClassifyError(str(e))

    out = gdal.GetDriverByName("GTiff").Create(out_path, width, height, 1, gdal.GDT_Byte,
                                               options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
    if out is None:
        raise ClassifyError(f"{out_path} を作成できず")
    out.SetGeoTransform(ds.GetGeoTransform())
    if ds.GetProjection():
        out.SetProjection(ds.GetProjection())
    band_out = out.GetRasterBand(1)
    band_out.SetNoDataValue(0)
    colors = [class_color(c + 1) for c in range(len(names))]
    table = gdal.ColorTable()
    table.SetColorEntry(0, (0, 0, 0, 0))
    for c, rgb in enumerate(colors):
        table.SetColorEntry(c + 1, (*rgb, 255))
    band_out.SetRasterColorTable(table)
    band_out.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
    band_out.SetCategoryNames(["データなし", *names])
    summaries = [ClassSummary(name, rgb) for name, rgb in zip(names, colors)]
    for i, (y, h) in enumerate(strips):
        if cancelled():
            out = None
            gdal.Unlink(out_path)
            raise ClassifyError("中止")
        found = classify(read_strip(ds, bands, y, h), model)
        band_out.WriteArray(found.reshape(h, width), 0, y)
        counts = np.bincount(found, minlength=len(names) + 1)[1:]
        area = pixel_area(width / 2, y + h / 2) if pixel_area else math.nan
        for c, n in enumerate(counts):
            summaries[c].pixels += int(n)
            summaries[c].area += float(n) * area
        say(40 + 60 * (i + 1) / len(strips), "分類中…")
    out = None
    if not any(s.pixels for s in summaries):
        gdal.Unlink(out_path)
        raise ClassifyError("値のある画素なし")
    return ClassifyResult(width, height, model, summaries)


def outline_classes(path: str, classes: int, min_pixels: int = 9) -> tuple[list, bool]:
    """The regions of each class of a class-number file as polygons [(rings, class (1-based), pixels)], and whether some were left out."""
    ds = gdal.Open(path)
    width, height = ds.RasterXSize, ds.RasterYSize
    scale = max(1.0, math.sqrt(width * height / MAX_TRACE_PIXELS))
    w, h = max(1, round(width / scale)), max(1, round(height / scale))
    values = ds.GetRasterBand(1).ReadAsArray(buf_xsize=w, buf_ysize=h, resample_alg=gdal.GRIORA_NearestNeighbour)
    sx, sy = width / w, height / h
    gt = ds.GetGeoTransform()
    transform = (gt[0], gt[1] * sx, 0, gt[3], 0, gt[5] * sy)
    least = max(1, round(max(1, int(min_pixels)) / (sx * sy)))
    found, truncated = [], False
    for c in range(1, classes + 1):
        room = MAX_POLYGONS - len(found)
        if room <= 0:
            truncated = True
            break
        polygons, more = outline_mask((values == c).astype(np.uint8), transform, ds.GetProjection(), least)
        truncated |= more or len(polygons) > room
        found += [(rings, c, round(pixels * sx * sy)) for rings, pixels in polygons[:room]]
    return found, truncated


def format_area(m2: float) -> str:
    """`12.3 km²` from 1 km² up, else `4,560 m²`; `—` when unknown."""
    if not math.isfinite(m2):
        return "—"
    return f"{m2 / 1e6:,.2f} km²" if m2 >= 1e6 else f"{m2:,.0f} m²"
