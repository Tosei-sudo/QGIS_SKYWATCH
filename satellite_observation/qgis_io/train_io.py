"""
Training data from QGIS: the labelled objects (observation records of an
image, or the features of a vector layer) as boxes in an image's pixels,
and the windows of the image read around them (as shown, RGBA, or the
values of chosen bands) for core.ai.train.

Labels are gathered on the main thread (`Label` holds plain values and a
geometry copy); the windows are read in the algorithm's thread from a copy
of the image layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes,
)

from ..core.ai.train.labels import class_of, footprint, kind_path, object_size
from ..core.ai.train.trainer import Window
from ..core.obs.taxonomy import Taxon, path_of, within
from .ai_io import Grid

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: Bytes of windows held at once at most (they are read before training): more are thinned out.
MAX_BYTES = 1_500_000_000
#: Negative windows (from checked areas) per positive one, at most.
NEGATIVE_SHARE = 0.3


@dataclass
class Label:
    """One labelled object: its class and outline (in `crs`), and the image it belongs to (None: any it lies on)."""
    cls: str
    geometry: QgsGeometry
    crs: QgsCoordinateReferenceSystem
    image_id: Optional[str] = None
    #: Where the size came from (記録・諸元・既定・形状).
    size_from: str = "形状"


@dataclass
class Area:
    """A checked area (確認範囲): every object of the classes trained is labelled inside it."""
    geometry: QgsGeometry
    crs: QgsCoordinateReferenceSystem
    image_id: Optional[str] = None


@dataclass
class Gathered:
    labels: list[Label] = field(default_factory=list)
    areas: list[Area] = field(default_factory=list)
    #: Objects left out, by reason.
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def labels_from_book(records, areas, taxonomy: Sequence[Taxon], depth: int = 0, within_kinds: Sequence[str] = (),
                     default_size: float = 20.0) -> Gathered:
    """
    The observation records (not 不在, not deleted) as labels: each box from
    the record's length and heading, else the class' 全長・全幅, else
    `default_size` metres; and the checked areas that cover every class trained.
    """
    out = Gathered()
    for o in records:
        if o.deleted_at is not None:
            continue
        if not o.present:
            out.skip("不在の記録")
            continue
        cls = class_of(o.kind, taxonomy, depth, within_kinds)
        if not cls:
            out.skip("種別なし" if not o.kind else "対象外の種別")
            continue
        size = object_size(kind_path(o.kind, taxonomy), taxonomy, o.length, o.heading, default_size)
        ring = [QgsPointXY(x, y) for x, y in footprint(o.lon, o.lat, size)]
        out.labels.append(Label(cls, QgsGeometry.fromPolygonXY([ring + ring[:1]]), WGS84, o.image_id or None, size.source))
    classes = {l.cls for l in out.labels}
    for a in areas:
        if a.deleted_at is not None or len(a.ring) < 3:
            continue
        # An area checked for one kind holds every object of the classes below it only.
        if a.kind and not all(within(c, path_of(a.kind)) for c in classes):
            continue
        ring = [QgsPointXY(x, y) for x, y in a.ring]
        out.areas.append(Area(QgsGeometry.fromPolygonXY([ring]), WGS84, a.image_id or None))
    return out


#: Fields that may say which image a feature belongs to (as AI layers and observation layers write it).
IMAGE_FIELDS = ("image_id", "IMAGE_ID")


def labels_from_features(features, fields, crs: QgsCoordinateReferenceSystem, transform_context, class_field: str,
                         taxonomy: Sequence[Taxon], depth: int = 0, within_kinds: Sequence[str] = (), default_size: float = 20.0) -> Gathered:
    """
    Features (of a vector layer, in `crs`) as labels: polygons by their
    outline, points by a box of the class' 全長・全幅 (else `default_size`
    metres); the class from `class_field` (a kind, or a class' name). A
    feature with an image id (image_id field) labels that image only.
    """
    out = Gathered()
    index = fields.indexFromName(class_field) if class_field else -1
    if index < 0:
        raise ValueError(f"クラスのフィールド「{class_field}」なし")
    image_index = next((fields.indexFromName(n) for n in IMAGE_FIELDS if fields.indexFromName(n) >= 0), -1)
    to_wgs = QgsCoordinateTransform(crs, WGS84, transform_context)
    for f in features:
        g = f.geometry()
        if g is None or g.isEmpty():
            out.skip("形状なし")
            continue
        value = f.attribute(index)
        cls = class_of(str(value) if value is not None else "", taxonomy, depth, within_kinds)
        if not cls:
            out.skip("クラスなし" if value in (None, "") else "対象外のクラス")
            continue
        image = f.attribute(image_index) if image_index >= 0 else None
        image = str(image) if image not in (None, "") and not (hasattr(image, "isNull") and image.isNull()) else None
        if g.type() == QgsWkbTypes.GeometryType.PointGeometry:
            p = to_wgs.transform(g.centroid().asPoint())
            size = object_size(kind_path(str(value), taxonomy), taxonomy, None, None, default_size)
            ring = [QgsPointXY(x, y) for x, y in footprint(p.x(), p.y(), size)]
            out.labels.append(Label(cls, QgsGeometry.fromPolygonXY([ring + ring[:1]]), WGS84, image, size.source))
        else:
            out.labels.append(Label(cls, QgsGeometry(g), crs, image))
    return out


def pixel_boxes(labels: Sequence[Label], grid: Grid, crs: QgsCoordinateReferenceSystem, transform_context, classes: Sequence[str],
                image_id: Optional[str]) -> np.ndarray:
    """The labels of an image as rows of centre x, centre y, width, height (the grid's pixels), class index."""
    rows = []
    index = {c: i for i, c in enumerate(classes)}
    extent = QgsRectangle(grid.x0, grid.y0 - grid.height * grid.size_y, grid.x0 + grid.width * grid.size_x, grid.y0)
    for label in labels:
        if label.image_id is not None and image_id is not None and label.image_id != image_id:
            continue
        if label.cls not in index:
            continue
        g = QgsGeometry(label.geometry)
        if label.crs != crs:
            try:
                g.transform(QgsCoordinateTransform(label.crs, crs, transform_context))
            except Exception:  # noqa: BLE001 (outside the image CRS's area)
                continue
        box = g.boundingBox()
        if not box.intersects(extent):
            continue
        x1, y1 = grid.to_pixel(box.xMinimum(), box.yMaximum())
        x2, y2 = grid.to_pixel(box.xMaximum(), box.yMinimum())
        w, h = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
        rows.append(((x1 + x2) / 2, (y1 + y2) / 2, w, h, index[label.cls]))
    return np.array(rows, np.float32).reshape(-1, 5)


def area_geometries(areas: Sequence[Area], grid: Grid, crs: QgsCoordinateReferenceSystem, transform_context,
                    image_id: Optional[str]) -> list[QgsGeometry]:
    """The checked areas of an image in its pixels (x right, y down)."""
    out = []
    for a in areas:
        if a.image_id is not None and image_id is not None and a.image_id != image_id:
            continue
        g = QgsGeometry(a.geometry)
        try:
            if a.crs != crs:
                g.transform(QgsCoordinateTransform(a.crs, crs, transform_context))
        except Exception:  # noqa: BLE001
            continue
        ring = g.asPolygon()
        if not ring:
            continue
        out.append(QgsGeometry.fromPolygonXY([[QgsPointXY(*grid.to_pixel(p.x(), p.y())) for p in ring[0]]]))
    return out


@dataclass
class ImageWindows:
    windows: list[Window] = field(default_factory=list)
    #: Objects too big for the model's input (the pixels should be larger).
    too_big: int = 0
    negatives: int = 0


def window_starts(boxes: np.ndarray, tile: int, width: int, height: int) -> list[tuple[int, int]]:
    """
    The windows to read (top left corners, 1.5 tiles a side): one per tile
    of the image holding objects' centres, reaching a quarter tile around it.
    """
    cells = sorted({(int(cx // tile), int(cy // tile)) for cx, cy, *_ in boxes if 0 <= cx < width and 0 <= cy < height})
    return [(gx * tile - tile // 4, gy * tile - tile // 4) for gx, gy in cells]


def windows_of(read: Callable[[QgsRectangle, int, int], np.ndarray], grid: Grid, boxes: np.ndarray, areas: Sequence[QgsGeometry], tile: int,
               rng: np.random.Generator, image: str = "", cancelled: Callable[[], bool] = lambda: False) -> ImageWindows:
    """
    The windows of one image: around its objects (each window with every
    object in it), and from its checked areas, some windows anywhere inside
    (where whatever is not labelled is background).
    """
    out = ImageWindows()
    big = (boxes[:, 2] > tile * 0.9) | (boxes[:, 3] > tile * 0.9)
    out.too_big = int(big.sum())
    boxes = boxes[~big]
    side = tile + tile // 2

    def window_at(x0: int, y0: int) -> Window:
        pixels = read(grid.extent_of(x0, y0, side, side), side, side)
        inside = boxes[(boxes[:, 0] + boxes[:, 2] / 2 > x0) & (boxes[:, 0] - boxes[:, 2] / 2 < x0 + side)
                       & (boxes[:, 1] + boxes[:, 3] / 2 > y0) & (boxes[:, 1] - boxes[:, 3] / 2 < y0 + side)].copy()
        inside[:, 0] -= x0
        inside[:, 1] -= y0
        return Window(pixels, inside, image)

    for x0, y0 in window_starts(boxes, tile, grid.width, grid.height):
        if cancelled():
            return out
        w = window_at(x0, y0)
        if (w.pixels[..., -1] > 0).any():
            out.windows.append(w)
    wanted = max(2, math.ceil(len(out.windows) * NEGATIVE_SHARE)) if areas else 0
    tries = 0
    while out.negatives < wanted and tries < wanted * 20 and areas:
        tries += 1
        area = areas[int(rng.integers(len(areas)))]
        box = area.boundingBox()
        x, y = rng.uniform(box.xMinimum(), box.xMaximum()), rng.uniform(box.yMinimum(), box.yMaximum())
        if not area.contains(QgsPointXY(x, y)) or cancelled():
            continue
        w = window_at(int(x) - side // 2, int(y) - side // 2)
        if (w.pixels[..., -1] > 0).mean() > 0.5:
            out.windows.append(w)
            out.negatives += 1
    return out


def thin(windows: list[Window], rng: np.random.Generator, limit: int = MAX_BYTES) -> tuple[list[Window], int]:
    """At most `limit` bytes of windows (a random part kept); gives how many were left out."""
    total = sum(w.pixels.nbytes for w in windows)
    if total <= limit or not windows:
        return windows, 0
    keep = max(1, int(len(windows) * limit / total))
    order = rng.permutation(len(windows))[:keep]
    return [windows[i] for i in sorted(order)], len(windows) - keep
