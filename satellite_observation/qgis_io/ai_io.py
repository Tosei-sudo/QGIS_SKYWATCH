"""
The AI tools on QGIS layers: an image layer drawn as it is shown (its
renderer: bands, contrast, a WMS as it comes) into RGBA pixels, the
detection model run over them in tiles (core.ai.detect) and the objects
turned into polygons on the map, as the viewer's 物体検出 does over its view.

Unlike the viewer, which analyses the screen, a run covers an extent at a
pixel size (by default the image's own pixels), drawn block by block so
memory stays bounded however large the extent.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsMapLayer, QgsMapRendererCustomPainterJob, QgsMapSettings, QgsPointXY, QgsRectangle,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter

from ..core.ai.detect import (
    Detection, blocks_of, box_corners, class_name, crop_of, detect_tile, is_empty, nms, offset, tiles_of, wanted,
)
from ..core.ai.model_config import DetectorSpec
from .ai_runtime import AiError, LoadedModel

#: The most pixels (width × height) one run may cover.
MAX_PIXELS = 40_000 * 40_000


def image_rgba(image: QImage) -> np.ndarray:
    """A QImage's pixels as RGBA (height × width × 4, uint8, not premultiplied)."""
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = image.width(), image.height()
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    rows = np.frombuffer(ptr, np.uint8).reshape(height, image.bytesPerLine())
    return rows[:, :width * 4].reshape(height, width, 4).copy()


def render_rgba(layer: QgsMapLayer, extent: QgsRectangle, width: int, height: int, crs: QgsCoordinateReferenceSystem,
                transform_context=None) -> np.ndarray:
    """The layer drawn alone over `extent` (in `crs`) at `width` × `height` pixels, nothing drawn transparent."""
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(crs)
    settings.setExtent(extent)
    settings.setOutputSize(QSize(width, height))
    settings.setOutputDpi(96)
    settings.setBackgroundColor(QColor(0, 0, 0, 0))
    settings.setFlag(QgsMapSettings.Flag.Antialiasing, False)
    if transform_context is not None:
        settings.setTransformContext(transform_context)
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.renderSynchronously()
    painter.end()
    return image_rgba(image)


@dataclass
class Grid:
    """The pixels of a run: the extent's corner and the pixel size (map units), and its size in pixels."""
    x0: float
    y0: float
    size_x: float
    size_y: float
    width: int
    height: int

    def to_map(self, x: float, y: float) -> tuple[float, float]:
        return self.x0 + x * self.size_x, self.y0 - y * self.size_y

    def to_pixel(self, mx: float, my: float) -> tuple[float, float]:
        return (mx - self.x0) / self.size_x, (self.y0 - my) / self.size_y

    def extent_of(self, x: int, y: int, w: int, h: int) -> QgsRectangle:
        ax, ay = self.to_map(x, y)
        bx, by = self.to_map(x + w, y + h)
        return QgsRectangle(ax, by, bx, ay)


def grid_of(extent: QgsRectangle, pixel_size: float) -> Grid:
    """The pixels covering `extent` at `pixel_size` (map units), the pixels square."""
    if not (pixel_size > 0) or extent.isEmpty():
        raise AiError("解析する範囲と画素の大きさが必要")
    width = max(1, math.ceil(extent.width() / pixel_size - 1e-9))
    height = max(1, math.ceil(extent.height() / pixel_size - 1e-9))
    if width * height > MAX_PIXELS:
        raise AiError(f"範囲が広すぎ（{width:,} × {height:,} 画素）。範囲を狭めるか画素を大きく")
    return Grid(extent.xMinimum(), extent.yMaximum(), pixel_size, pixel_size, width, height)


@dataclass
class DetectRun:
    """What a detection run found, and how it went."""
    detections: list[Detection] = field(default_factory=list)
    tiles: int = 0
    blocks: int = 0
    seconds: float = 0.0


def detect_layer(model: LoadedModel, spec: DetectorSpec, render: Callable[[QgsRectangle, int, int], np.ndarray], grid: Grid,
                 overlap: float = 0.2, classes: Sequence[str] = (), progress: Optional[Callable[[str, Optional[float]], None]] = None,
                 cancelled: Callable[[], bool] = lambda: False) -> DetectRun:
    """
    Runs the model over the grid: drawn a block at a time (`render(extent,
    width, height)` gives its RGBA pixels), each block cut into overlapping
    tiles of the model's input, the objects found merged across tiles with
    NMS and kept when of a class asked for (all when none).
    """
    started = time.monotonic()
    tw, th = spec.input_size
    names = model.input_names
    if not names:
        raise AiError("モデルに入力なし")
    feed_name = names[0]
    types = model.output_types()

    def run(tensor: np.ndarray) -> dict:
        return model.run({feed_name: tensor})

    blocks = blocks_of(grid.width, grid.height, max(spec.input_size))
    found: list[Detection] = []
    tiles_done = 0
    for b, (bx, by, bw, bh) in enumerate(blocks):
        if cancelled():
            raise AiError("中止")
        where = f"ブロック {b + 1} / {len(blocks)}・" if len(blocks) > 1 else ""
        if progress:
            progress(f"{where}画像を描画中…", None)
        rgba = render(grid.extent_of(bx, by, bw, bh), bw, bh)
        tiles = [(x, y) for x, y in tiles_of(bw, bh, spec.input_size, overlap) if not is_empty(rgba, x, y, tw, th)]
        for i, (x0, y0) in enumerate(tiles):
            if cancelled():
                raise AiError("中止")
            objects = detect_tile(run, types, crop_of(rgba, x0, y0, tw, th), 0, 0, spec)
            found += [offset(d, bx + x0, by + y0) for d in objects]
            if progress:
                progress(f"{where}解析中… {i + 1} / {len(tiles)} タイル（{model.backend}）", (b + (i + 1) / len(tiles)) / len(blocks))
        tiles_done += len(tiles)
    kept = [d for d in nms(found, spec.iou) if wanted(d, classes, spec.classes)]
    return DetectRun(kept, tiles_done, len(blocks), time.monotonic() - started)


def detection_rings(d: Detection, grid: Grid) -> list[list[QgsPointXY]]:
    """A detection's outline (its mask's, else its box's corners) as closed rings of map points."""
    rings = d.outline or [box_corners(d)]
    out = []
    for ring in rings:
        points = [QgsPointXY(*grid.to_map(x, y)) for x, y in ring]
        out.append(points + points[:1])
    return out


def summary(detections: Sequence[Detection], names: Sequence[str]) -> str:
    """The five commonest classes and their counts: `ship 12・plane 3`."""
    counts: dict[str, int] = {}
    for d in detections:
        n = class_name(d.cls, names)
        counts[n] = counts.get(n, 0) + 1
    return "・".join(f"{c} {n}" for c, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5])


def angle_degrees(d: Detection) -> Optional[float]:
    """An oriented box's rotation in degrees (to 0.1), as the viewer writes it."""
    return None if d.angle is None else round(d.angle * 180 / math.pi, 1)
