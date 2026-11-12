"""
「アノテーション変換」 in QGIS (core.ai.annotation_boxes): the outline of
the object round a point, found on an image layer (drawn as shown) by a
Segment Anything model or by a brightness threshold, and the image's
metres per pixel.

A window of a few object lengths round each point is drawn at the encoder's
input size (1024), so a small object is seen enlarged; windows lie on a grid
of half windows, and the last few encoded are kept, so points near each
other share the encoder's work.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import Callable, Optional

import numpy as np
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsUnitTypes

from ..core.ai.annotation_boxes import Shape, mask_shape, threshold_mask, window_at, window_side
from ..core.ai.model_config import SamModelConfig
from ..core.ai.sam import SamPoint, sam_mask, sam_prompt, sam_tensor, sam_hwc, SAM_MEAN, SAM_STD
from .ai_io import Grid, render_rgba
from .ai_runtime import LoadedModel, local_model, model_from, name_of

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: Windows kept encoded.
KEEP = 6
#: Pixels a window is drawn at for the threshold (no model).
THRESHOLD_SIDE = 512


def metres_per_pixel(grid: Grid, crs: QgsCoordinateReferenceSystem, transform_context, fallback: float = 0.0) -> float:
    """
    Metres per pixel of an image: its pixel size in its CRS's units turned
    into metres (degrees at the image's middle latitude); `fallback` for an
    image without a CRS (its pixels are its only units), 0 for none.
    """
    if not crs.isValid():
        return fallback
    size = (grid.size_x + grid.size_y) / 2
    if crs.isGeographic():
        cx, cy = grid.to_map(grid.width / 2, grid.height / 2)
        try:
            p = QgsCoordinateTransform(crs, WGS84, transform_context).transform(QgsPointXY(cx, cy))
            lat = p.y()
        except Exception:  # noqa: BLE001
            lat = cy
        return size * 111_320.0 * math.sqrt(max(math.cos(math.radians(lat)), 1e-6))
    factor = QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), QgsUnitTypes.DistanceUnit.DistanceMeters)
    return size * factor if factor > 0 else fallback


class Outliner:
    """Finds the shape of the object round a point of an image (pixels of `grid`)."""

    method = ""

    def __init__(self, layer, grid: Grid, crs, transform_context):
        self.layer = layer
        self.grid = grid
        self.crs = crs
        self.transform_context = transform_context
        self.runs = 0

    def draw(self, x0: int, y0: int, side: int, out: int) -> np.ndarray:
        return render_rgba(self.layer, self.grid.extent_of(x0, y0, side, side), out, out, self.crs, self.transform_context)

    def shape_at(self, x: float, y: float, length_px: Optional[float], mpp: float) -> Optional[Shape]:
        side = window_side(length_px, mpp)
        x0, y0 = window_at(x, y, side, self.grid.width, self.grid.height)
        out = self.side_for(side)
        k = out / side
        found = self.find(x0, y0, side, out, (x - x0) * k, (y - y0) * k)
        if found is None:
            return None
        return Shape(x0 + found.cx / k, y0 + found.cy / k, found.length / k, found.width / k, found.angle)

    def side_for(self, side: int) -> int:
        return min(side, THRESHOLD_SIDE)

    def find(self, x0: int, y0: int, side: int, out: int, x: float, y: float) -> Optional[Shape]:
        raise NotImplementedError


class ThresholdOutliner(Outliner):
    """The simple outline (no model): brightness split by Otsu's threshold, the part under the point."""

    method = "しきい値"

    def find(self, x0, y0, side, out, x, y) -> Optional[Shape]:
        rgba = self.draw(x0, y0, side, out)
        self.runs += 1
        return mask_shape(threshold_mask(rgba, x, y), x, y)


class SamOutliner(Outliner):
    """Segment Anything: the encoder sees the window once, the decoder is prompted with the point."""

    method = "SAM"

    def __init__(self, layer, grid, crs, transform_context, encoder: LoadedModel, decoder: LoadedModel,
                 config: Optional[SamModelConfig] = None):
        super().__init__(layer, grid, crs, transform_context)
        self.encoder = encoder
        self.decoder = decoder
        self.config = config
        self.side = (config.input_size if config and config.input_size else None) or 1024
        self.cache: OrderedDict = OrderedDict()

    def side_for(self, side: int) -> int:
        return self.side

    def embeddings(self, x0: int, y0: int, side: int) -> np.ndarray:
        key = (x0, y0, side)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        rgba = self.draw(x0, y0, side, self.side)
        first = self.encoder.signature.inputs[0]
        if len(first.dims) == 3 and first.dims[2] == 3:
            feed = sam_hwc(rgba)
        else:
            c = self.config
            feed = sam_tensor(rgba, self.side, (c.mean if c else None) or SAM_MEAN, (c.std if c else None) or SAM_STD)[None]
        value = self.encoder.run({first.name: feed})[self.encoder.output_names[0]]
        self.runs += 1
        self.cache[key] = value
        while len(self.cache) > KEEP:
            self.cache.popitem(last=False)
        return value

    def find(self, x0, y0, side, out, x, y) -> Optional[Shape]:
        embeddings = self.embeddings(x0, y0, side)
        coords, labels = sam_prompt([SamPoint(x, y)], 1.0)
        names = self.decoder.input_names
        feeds: dict = {}
        for name, value in (("point_coords", coords), ("point_labels", labels), ("mask_input", np.zeros((1, 1, 256, 256), np.float32)),
                            ("has_mask_input", np.zeros(1, np.float32)), ("orig_im_size", np.array([out, out], np.float32))):
            if name in names:
                feeds[name] = value
        feeds[name_of(names, "image_embeddings", 0)] = embeddings
        outputs = self.decoder.run(feeds)
        o = self.decoder.output_names
        scores = "iou_predictions" if "iou_predictions" in o else next((n for n in o if outputs[n].ndim == 2), None)
        mask = sam_mask(outputs[name_of(o, "masks", 0)], outputs[scores] if scores else None, out, out, 1.0, self.side)
        return mask_shape(mask, x, y)


def load_sam(encoder: str, decoder: str, base: str = "", label: str = "SAM",
             cancelled: Callable[[], bool] = lambda: False) -> tuple[LoadedModel, LoadedModel]:
    """The encoder and decoder (paths, or URLs downloaded once)."""
    return (model_from(local_model(encoder, base, cancelled=cancelled), f"{label}（エンコーダー）"),
            model_from(local_model(decoder, base, cancelled=cancelled), f"{label}（デコーダー）"))
