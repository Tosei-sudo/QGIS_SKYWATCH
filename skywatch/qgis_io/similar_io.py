"""
「似たものを探す」 in QGIS (core.obs.similar): the image drawn as the map
shows it (its style and 色調補正) on a plane in metres round the 見本, north
up, over the map's view, and the 見本 drawn the same way; then what is found
made a layer like 「物体検出（AI）」's (`class`, `score`, `image_id`,
`model`, `detected_at`, with the 見本's `heading`, `length_m`, `width_m`,
`activity` and `confidence`), which the dock 「AI結果」 lists for checking
(正・誤検出) and records as observations.

Drawing is quick and done on the main thread (prepare); the search itself
runs in a task (core.obs.similar.find_similar on the pictures).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, QVariant

from ..core.obs.flows import outline_of
from ..core.obs.similar import Found, gray_of, half_sizes
from ..core.obs.stamps import Sample
from ..core.obs.taxonomy import path_of
from .ai_io import Grid, render_rgba

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: The model name the found ones carry (their notes say it).
MODEL = "見本照合"
#: The 見本 is drawn about this many pixels long (finer than the image's own pixels is no better).
TARGET_PIXELS = 24
#: Under this many pixels long the 見本 is too small to compare.
FEWEST_PIXELS = 8
#: The most pixels searched at once (about 1600 × 1600).
MOST_PIXELS = 2_600_000


class SimilarError(Exception):
    """Why the search cannot be made (said as it is)."""


@dataclass
class SearchPictures:
    """What the search compares: the view and the 見本 drawn alike, and what is already recorded."""
    crs: QgsCoordinateReferenceSystem
    grid: Grid
    gray: np.ndarray
    valid: np.ndarray
    sample_gray: np.ndarray
    sample_valid: np.ndarray
    #: The 見本's centre in its own picture (pixels).
    sx: float
    sy: float
    #: Metres per pixel, and the 見本's length and width in pixels.
    mpp: float
    length_px: float
    width_px: Optional[float]
    #: The places of this image's records in the searched picture (the 見本 among them).
    taken: list = field(default_factory=list)


def local_crs(lon: float, lat: float) -> QgsCoordinateReferenceSystem:
    """A plane in metres round a place, north up there."""
    return QgsCoordinateReferenceSystem.fromProj(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")


def native_mpp(layer, crs: QgsCoordinateReferenceSystem, context) -> float:
    """Metres per pixel of an image layer's own pixels (0 when unknown)."""
    try:
        extent = layer.extent()
        width, height = layer.width(), layer.height()
    except (AttributeError, RuntimeError):
        return 0.0
    if not width or not height or extent.isEmpty():
        return 0.0
    try:
        local = QgsCoordinateTransform(layer.crs(), crs, context).transformBoundingBox(extent)
    except Exception:  # noqa: BLE001
        return 0.0
    return math.sqrt(max(local.width() / width, 1e-12) * max(local.height() / height, 1e-12))


def _array_place(grid: Grid, mx: float, my: float) -> tuple[float, float]:
    """A map place as an array place (pixel centres at whole numbers)."""
    x, y = grid.to_pixel(mx, my)
    return x - 0.5, y - 0.5


def prepare(layer, view: QgsRectangle, view_crs: QgsCoordinateReferenceSystem, sample: Sample,
            records: Sequence = (), context=None) -> SearchPictures:
    """
    The pictures for a search of the map's view (`view` in `view_crs`) on an
    image layer, the 見本 (measured, on this image) among them; `records`: the
    image's records, not found again.
    """
    if not sample.measured or sample.lon is None or sample.lat is None:
        raise SimilarError("見本に長さと向きが必要（2〜4 点で測って記録した記録を見本に）")
    context = context or QgsProject.instance().transformContext()
    crs = local_crs(sample.lon, sample.lat)
    try:
        area = QgsCoordinateTransform(view_crs, crs, context).transformBoundingBox(view)
        image = QgsCoordinateTransform(layer.crs(), crs, context).transformBoundingBox(layer.extent())
    except Exception as error:  # noqa: BLE001
        raise SimilarError(f"表示範囲を画像の座標に変換できません: {error}") from error
    area = area.intersect(image)
    if area.isEmpty() or area.width() <= 0 or area.height() <= 0:
        raise SimilarError("地図の表示範囲に画像がありません")
    length = float(sample.length)
    width = float(sample.width) if sample.width and sample.width > 0 else None
    mpp = max(length / TARGET_PIXELS, native_mpp(layer, crs, context))
    pixels = (area.width() / mpp) * (area.height() / mpp)
    if pixels > MOST_PIXELS:
        mpp *= math.sqrt(pixels / MOST_PIXELS)
    if length / mpp < FEWEST_PIXELS:
        if pixels > MOST_PIXELS:
            raise SimilarError(f"表示範囲が広すぎます（見本が {length / mpp:.0f} 画素）。地図を拡大してから")
        raise SimilarError(f"見本が小さすぎます（画像の画素で {length / mpp:.0f} 画素）。もっと細かい画像で")
    w = max(1, int(math.ceil(area.width() / mpp)))
    h = max(1, int(math.ceil(area.height() / mpp)))
    grid = Grid(area.xMinimum(), area.yMaximum(), mpp, mpp, w, h)
    gray, valid = gray_of(render_rgba(layer, grid.extent_of(0, 0, w, h), w, h, crs, context))
    # The 見本 alone, drawn alike (it may lie outside the view).
    length_px = length / mpp
    width_px = width / mpp if width else None
    half_l, half_w, _ = half_sizes(length_px, width_px)
    r = int(math.ceil(math.hypot(half_l, half_w))) + 3
    patch = Grid(-(r + 0.5) * mpp, (r + 0.5) * mpp, mpp, mpp, 2 * r + 1, 2 * r + 1)
    sample_gray, sample_valid = gray_of(render_rgba(layer, patch.extent_of(0, 0, 2 * r + 1, 2 * r + 1), 2 * r + 1, 2 * r + 1, crs, context))
    if not sample_valid[r, r]:
        raise SimilarError("見本の場所に画像がありません（この画像の上の記録を見本に）")
    to_local = QgsCoordinateTransform(WGS84, crs, context)
    taken = []
    for o in records:
        try:
            p = to_local.transform(QgsPointXY(o.lon, o.lat))
        except Exception:  # noqa: BLE001
            continue
        x, y = _array_place(grid, p.x(), p.y())
        if -r <= x <= w + r and -r <= y <= h + r:
            taken.append((x, y))
    sx, sy = _array_place(grid, 0.0, 0.0)
    taken.append((sx, sy))
    return SearchPictures(crs, grid, gray, valid, sample_gray, sample_valid, float(r), float(r), mpp, length_px, width_px, taken)


def found_places(found: Sequence[Found], pictures: SearchPictures, context=None) -> list[tuple[float, float, float, float]]:
    """What was found as (lon, lat, heading, score)."""
    context = context or QgsProject.instance().transformContext()
    to_wgs84 = QgsCoordinateTransform(pictures.crs, WGS84, context)
    out = []
    for f in found:
        mx, my = pictures.grid.to_map(f.x + 0.5, f.y + 0.5)
        p = to_wgs84.transform(QgsPointXY(mx, my))
        out.append((p.x(), p.y(), f.heading, f.score))
    return out


def found_layer(places: Sequence[tuple[float, float, float, float]], sample: Sample, image_id: str, image_name: str,
                image_time: Optional[int] = None) -> QgsVectorLayer:
    """
    What was found as a layer the dock 「AI結果」 reads as a detection: the
    見本's outline at each place (a point when the 見本 has no width).
    """
    outlined = bool(sample.width and sample.width > 0)
    kind = (path_of(sample.kind) or ["モノ"])[-1]
    layer = QgsVectorLayer(f"{'Polygon' if outlined else 'Point'}?crs=EPSG:4326", f"似たもの（{kind}・{image_name or image_id}）", "memory")
    fields = QgsFields()
    for name, kind_ in (("class", QVariant.String), ("class_id", QVariant.Int), ("score", QVariant.Double), ("image", QVariant.String),
                        ("image_id", QVariant.String), ("image_time", QVariant.DateTime), ("model", QVariant.String),
                        ("detected_at", QVariant.DateTime), ("note", QVariant.String), ("heading", QVariant.Double),
                        ("length_m", QVariant.Double), ("width_m", QVariant.Double), ("activity", QVariant.String),
                        ("confidence", QVariant.String)):
        fields.append(QgsField(name, kind_))
    layer.dataProvider().addAttributes(fields.toList())
    layer.updateFields()
    now = QDateTime.currentDateTimeUtc()
    # To the second, as a saved layer keeps it (the run's tag).
    now = QDateTime.fromSecsSinceEpoch(now.toSecsSinceEpoch())
    when = QDateTime.fromMSecsSinceEpoch(image_time) if image_time is not None else None
    note = f"見本: {sample.name}" if sample.name else ""
    features = []
    for lon, lat, heading, score in places:
        f = QgsFeature(layer.fields())
        if outlined:
            ring = outline_of(lon, lat, sample.length, sample.width, heading) or []
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in [*ring, ring[0]]]]))
        else:
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        f.setAttributes([sample.kind, 0, round(score, 3), image_name, image_id, when, MODEL, now, note, round(heading % 360, 1),
                         sample.length, sample.width if outlined else None, sample.activity, sample.confidence])
        features.append(f)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer
