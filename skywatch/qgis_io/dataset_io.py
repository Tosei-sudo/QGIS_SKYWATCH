"""
Training datasets (core.ai.train.dataset) in QGIS: their images opened as
raster layers (a GeoTIFF on the map, or a PNG/JPEG in its own pixels), their
annotations as boxes in the pixels the model is trained at, and vector
annotation files read as layers.

An image of a dataset is taken as fully annotated: everything not annotated
on it is background, as in the datasets these formats come from.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
from qgis.core import QgsGeometry, QgsPointXY, QgsRasterLayer, QgsVectorLayer

from ..core.ai.train.dataset import Dataset, DatasetError, Group, Item, PixelBox, Reader, count_classes, load_dataset, missing_files
from ..core.ai.train.labels import class_of
from ..core.obs.taxonomy import Taxon
from .ai_io import Grid, grid_of
from .train_io import Gathered, labels_from_features

#: Fields a vector annotation file may hold its class in, when none is given.
CLASS_FIELDS = ("class", "CLASS", "kind", "KIND", "種別", "label", "name", "category", "type")


@dataclass
class DatasetImage:
    """One image of a dataset to train on, with its objects (pixel annotations) or the labels of its vector file."""
    dataset: str
    group: Group
    item: Item
    #: (class, box) in the image's pixels or fractions.
    boxes: list[tuple[str, PixelBox]] = field(default_factory=list)
    #: The vector file's labels (shared by the images it covers).
    labels: Optional[Gathered] = None

    @property
    def name(self) -> str:
        return os.path.basename(self.item.image)


@dataclass
class DatasetLabels:
    images: list[DatasetImage] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    #: Files not there.
    missing: list[str] = field(default_factory=list)
    #: Datasets read: name → images.
    read: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str, n: int = 1) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + n

    @property
    def classes(self) -> set[str]:
        out = {c for i in self.images for c, _ in i.boxes}
        for i in self.images:
            if i.labels is not None:
                out |= {l.cls for l in i.labels.labels}
        return out


def read_datasets(paths: Sequence[str]) -> list[tuple[str, Dataset]]:
    """The datasets of `paths` (name, dataset); raises DatasetError for one that cannot be read."""
    out = []
    for p in paths:
        d = load_dataset(p)
        out.append((d.name or os.path.splitext(os.path.basename(p))[0], d))
    return out


def vector_layer(path: str) -> QgsVectorLayer:
    layer = QgsVectorLayer(path, os.path.basename(path), "ogr")
    if not layer.isValid():
        raise DatasetError(f"ベクタの注釈を開けず: {path}")
    return layer


def class_field_of(layer: QgsVectorLayer, wanted: str = "") -> str:
    names = [f.name() for f in layer.fields()]
    if wanted:
        if wanted not in names:
            raise DatasetError(f"クラスのフィールド「{wanted}」なし: {os.path.basename(layer.source().split('|')[0])}")
        return wanted
    found = next((n for n in CLASS_FIELDS if n in names), "")
    if not found:
        raise DatasetError(f"クラスのフィールドが分からない（{', '.join(names) or 'フィールドなし'}）: {os.path.basename(layer.source())}")
    return found


def dataset_labels(datasets: Sequence[tuple[str, Dataset]], taxonomy: Sequence[Taxon], depth: int, kinds: Sequence[str],
                   default_size: float, transform_context, cancelled=lambda: False) -> DatasetLabels:
    """The objects of every image of the datasets, by class (as 「AI 学習」 trains them); missing files left out."""
    out = DatasetLabels()
    reader = Reader()
    vectors: dict[tuple, Gathered] = {}
    for name, dataset in datasets:
        missing = set(missing_files(dataset))
        out.missing += [m for m in missing if m not in out.missing]
        count = 0
        for group, item in dataset.items:
            if cancelled():
                return out
            if item.image in missing or (item.labels and item.labels in missing):
                continue
            image = DatasetImage(name, group, item)
            if group.format == "vector":
                key = (item.labels, group.class_field, tuple(sorted(group.class_map.items())))
                if key not in vectors:
                    layer = vector_layer(item.labels)
                    vectors[key] = labels_from_features(layer.getFeatures(), layer.fields(), layer.crs(), transform_context,
                                                        class_field_of(layer, group.class_field), taxonomy, depth, kinds, default_size,
                                                        group.class_map)
                    for why, n in vectors[key].skipped.items():
                        out.skip(why, n)
                image.labels = vectors[key]
            else:
                for box in reader.boxes(group, item):
                    cls = class_of(group.kind_of(box.name), taxonomy, depth, kinds)
                    if cls:
                        image.boxes.append((cls, box))
                    else:
                        out.skip("対象外のクラス")
            out.images.append(image)
            count += 1
        out.read[name] = count
    return out


def open_image(path: str) -> QgsRasterLayer:
    layer = QgsRasterLayer(path, os.path.basename(path))
    if not layer.isValid():
        raise DatasetError(f"画像を開けず: {path}")
    return layer


def image_grid(layer: QgsRasterLayer, pixel_size: float = 0) -> tuple[Grid, Grid]:
    """
    The image's own pixels, and those to train at: its own, or `pixel_size`
    (map units) when the image is on the map (an image without a CRS keeps
    its pixels: they are its only units).
    """
    extent = layer.extent()
    native = Grid(extent.xMinimum(), extent.yMaximum(), layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY(), layer.width(),
                  layer.height())
    if pixel_size > 0 and layer.crs().isValid():
        return native, grid_of(extent, pixel_size)
    return native, native


def pixel_rows(boxes: Sequence[tuple[str, PixelBox]], native: Grid, grid: Grid, classes: Sequence[str]) -> np.ndarray:
    """Annotations in the image's pixels as rows of centre x, centre y, width, height (pixels of `grid`), class index."""
    index = {c: i for i, c in enumerate(classes)}
    rows = []
    for cls, b in boxes:
        if cls not in index:
            continue
        b = b.in_pixels(native.width, native.height)
        x1, y1 = grid.to_pixel(*native.to_map(min(b.x1, b.x2), min(b.y1, b.y2)))
        x2, y2 = grid.to_pixel(*native.to_map(max(b.x1, b.x2), max(b.y1, b.y2)))
        x1, x2 = max(x1, 0.0), min(x2, float(grid.width))
        y1, y2 = max(y1, 0.0), min(y2, float(grid.height))
        if x2 <= x1 or y2 <= y1:
            continue
        rows.append(((x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, 1.0), max(y2 - y1, 1.0), index[cls]))
    return np.array(rows, np.float32).reshape(-1, 5)


def whole_image(grid: Grid) -> QgsGeometry:
    """The image as a checked area, in its pixels."""
    w, h = grid.width, grid.height
    return QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h), (0, 0))]])


def class_counts(dataset: Dataset, transform_context=None) -> dict[str, int]:
    """The objects of a dataset by its annotations' class names (the vector files' by their class field)."""
    counts: dict[str, int] = {}
    reader = Reader()
    seen = set()
    for group, item in dataset.items:
        if group.format == "vector":
            if item.labels in seen:
                continue
            seen.add(item.labels)
            layer = vector_layer(item.labels)
            index = layer.fields().indexFromName(class_field_of(layer, group.class_field))
            for f in layer.getFeatures():
                value = f.attribute(index)
                name = "" if value is None or (hasattr(value, "isNull") and value.isNull()) else str(value)
                counts[name] = counts.get(name, 0) + 1
        else:
            count_classes(reader.boxes(group, item), counts)
    return counts
