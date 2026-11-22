"""
練習モード's sample images (core/training/sample.py makes 512 px cartoons
without GDAL, for the server alone): the same files, bounds and ships, drawn
at 1600 px with a fixed coast, piers, a town, ships with a deck, a bridge
and a shadow (optical), or speckle and bright returns (SAR); and
basemap.tif, a plain map of the same coast over the bay (練習用の背景地図:
the practice works without the internet). Made once, the first time
練習モード starts (qgis_io/training_io.py), and for the help's pictures
(scripts/help_shots/scenes.py, as before this moved here on 2026-11-22).

Needs GDAL's Python bindings, NumPy and Qt (QGIS has them). Only QImage and
QPainter: may run in a task.
"""
from __future__ import annotations

import json
import math
import os
import random

import numpy as np
from osgeo import gdal, osr

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)

#: Pixels of a side (the mock's images are 512).
SIZE = 1600
#: The mock's sample area (sample.AREA): west, south, east, north.
AREA = (139.72, 35.27, 139.78, 35.33)


def coast_lon(lat: float) -> float:
    """The coast (the same in every image; the mock's own moves with each image's seed)."""
    y = (AREA[3] - lat) / ((AREA[3] - AREA[1]) / 512)
    return AREA[0] + (60 + 18 * math.sin(y / 90) + 6 * math.sin(y / 17)) * (AREA[2] - AREA[0]) / 512


def smooth_noise(rnd: np.random.Generator, size: int, cells: int, amp: float) -> np.ndarray:
    grid = rnd.normal(0, 1, (cells + 2, cells + 2)).astype(np.float32)
    img = QImage(grid.shape[1], grid.shape[0], QImage.Format.Format_Grayscale8)
    lo, hi = grid.min(), grid.max()
    g8 = ((grid - lo) / (hi - lo) * 255).astype(np.uint8)
    for y in range(g8.shape[0]):
        for x in range(g8.shape[1]):
            img.setPixel(x, y, int(g8[y, x]) * 0x010101)
    # Smooth scaling gives a 32-bit image back: one byte a pixel again.
    big = img.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    big = big.convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = big.constBits()
    ptr.setsize(big.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(size, big.bytesPerLine())[:, :size].astype(np.float32)
    return (arr / 255 - 0.5) * 2 * amp


class Frame:
    def __init__(self, box):
        self.west, self.south, self.east, self.north = box
        self.dx = (self.east - self.west) / SIZE
        self.dy = (self.north - self.south) / SIZE

    def px(self, lon: float, lat: float) -> QPointF:
        return QPointF((lon - self.west) / self.dx, (self.north - lat) / self.dy)

    def metres(self) -> float:
        """Metres of a pixel (across)."""
        return self.dx * 111_320 * math.cos(math.radians((self.north + self.south) / 2))


def land_path(f: Frame) -> QPainterPath:
    path = QPainterPath(QPointF(-10, -10))
    for i in range(0, SIZE + 21, 4):
        lat = f.north - (i - 10) * f.dy
        path.lineTo(f.px(coast_lon(lat), lat).x(), i - 10)
    path.lineTo(-10, SIZE + 10)
    path.closeSubpath()
    return path


#: Piers (lat of the root, length m, width m, bearing from the coast).
PIERS = [(35.322, 420, 40, 95), (35.311, 300, 30, 80), (35.296, 520, 45, 100), (35.281, 260, 30, 90)]


def pier_polygons(f: Frame) -> list[QPolygonF]:
    out = []
    m = f.metres()
    for lat, length, width, bearing in PIERS:
        root = f.px(coast_lon(lat) - 0.0004, lat)
        t = QTransform().translate(root.x(), root.y()).rotate(bearing - 90)
        out.append(t.map(QPolygonF(QRectF(0, -width / m / 2, length / m, width / m))))
    return out


def ship_path(length: float, width: float) -> QPainterPath:
    """A hull along +x (bow at +x), centred on 0."""
    half, w = length / 2, width / 2
    p = QPainterPath(QPointF(-half, -w * 0.85))
    p.lineTo(half * 0.62, -w)
    p.quadTo(half * 0.95, -w * 0.7, half, 0)
    p.quadTo(half * 0.95, w * 0.7, half * 0.62, w)
    p.lineTo(-half, w * 0.85)
    p.quadTo(-half * 1.03, 0, -half, -w * 0.85)
    return p


def draw_optical(f: Frame, ships: list, seed: int) -> np.ndarray:
    rnd = np.random.default_rng(1234)
    sea = np.zeros((SIZE, SIZE, 3), np.float32)
    base = np.array([26, 60, 86], np.float32)
    sea[:] = base
    sea += smooth_noise(rnd, SIZE, 6, 7)[..., None] * np.array([0.6, 0.9, 1.0], np.float32)
    sea += rnd.normal(0, 2.2, (SIZE, SIZE, 1)).astype(np.float32)
    img = QImage(np.clip(sea, 0, 255).astype(np.uint8).tobytes(), SIZE, SIZE, SIZE * 3, QImage.Format.Format_RGB888).copy()
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    land = land_path(f)
    # Shallow water along the coast.
    for width, alpha in ((60, 18), (34, 22), (16, 30)):
        p.setPen(QPen(QColor(70, 120, 120, alpha), width))
        p.drawPath(land)
    p.setPen(QPen(QColor(205, 190, 150), 7))
    p.setBrush(QColor(92, 104, 70))
    p.drawPath(land)
    # The town: blocks on the land, roads between them.
    r = random.Random(7)
    p.setClipPath(land)
    p.setPen(Qt.PenStyle.NoPen)
    for gy in range(0, SIZE, 26):
        for gx in range(0, 260, 26):
            lat = f.north - gy * f.dy
            if f.px(coast_lon(lat), lat).x() - gx < 40:
                continue
            for _ in range(r.randint(1, 3)):
                shade = r.choice([(150, 148, 140), (175, 170, 160), (120, 118, 112), (190, 120, 90), (110, 125, 85)])
                w, h = r.randint(6, 14), r.randint(6, 14)
                p.setBrush(QColor(*shade))
                p.drawRect(QRectF(gx + r.randint(2, 24 - min(w, 20)), gy + r.randint(2, 24 - min(h, 20)), w, h))
    p.setClipping(False)
    # Piers.
    p.setPen(QPen(QColor(120, 120, 115), 1))
    p.setBrush(QColor(178, 176, 168))
    for poly in pier_polygons(f):
        p.drawPolygon(poly)
    # Small boats.
    for _ in range(14):
        x, y = r.uniform(300, SIZE - 20), r.uniform(20, SIZE - 20)
        p.setBrush(QColor(225, 225, 220))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(x, y), 1.6, 1.1)
    m = f.metres()
    for i, ship in enumerate(ships):
        c = f.px(ship["lon"], ship["lat"])
        length, width = ship["length"] / m, ship["width"] / m
        p.save()
        p.translate(c)
        p.rotate(ship["heading"] - 90)
        hull = ship_path(length, width)
        if i % 2 == 1:
            # Under way: a wake behind.
            grad = QLinearGradient(QPointF(-length / 2, 0), QPointF(-length * 2.2, 0))
            grad.setColorAt(0, QColor(230, 240, 240, 150))
            grad.setColorAt(1, QColor(230, 240, 240, 0))
            wake = QPolygonF([QPointF(-length / 2, -width * 0.45), QPointF(-length * 2.2, -width * 1.6),
                              QPointF(-length * 2.2, width * 1.6), QPointF(-length / 2, width * 0.45)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawPolygon(wake)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(5, 15, 25, 120))
        p.drawPath(hull.translated(1.5, 2.5))
        p.setBrush(QColor(160, 60, 50) if i % 3 == 0 else QColor(70, 75, 85))
        p.drawPath(hull)
        p.setBrush(QColor(206, 204, 196))
        p.drawPath(ship_path(length * 0.9, width * 0.72))
        if ship["length"] >= 150:
            # Containers.
            colours = [(170, 60, 50), (60, 100, 150), (200, 160, 60), (90, 130, 90), (220, 220, 215)]
            cell = max(2.0, length / 14)
            for k in range(7):
                for j in (-1, 0, 1):
                    p.setBrush(QColor(*colours[(k * 3 + j + i) % len(colours)]))
                    p.drawRect(QRectF(-length * 0.12 + k * cell, j * width * 0.22 - width * 0.1, cell * 0.9, width * 0.2))
        # The bridge, white, at the stern.
        p.setBrush(QColor(245, 245, 240))
        p.drawRect(QRectF(-length * 0.42, -width * 0.36, length * 0.13, width * 0.72))
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRect(QRectF(-length * 0.29, -width * 0.36, length * 0.025, width * 0.72))
        p.restore()
    p.end()
    return qimage_rgb(img)


def draw_sar(f: Frame, ships: list, seed: int) -> np.ndarray:
    rnd = np.random.default_rng(99 + seed)
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255))
    p.drawPath(land_path(f))
    for poly in pier_polygons(f):
        p.drawPolygon(poly)
    p.end()
    mask = qimage_rgb(img)[..., 0].astype(np.float32) / 255
    texture = 0.55 + smooth_noise(rnd, SIZE, 30, 0.25)
    sea = 0.09 + smooth_noise(rnd, SIZE, 5, 0.025)
    amp = sea * (1 - mask) + texture * mask
    # Town and piers: strong, uneven returns.
    town = (rnd.random((SIZE, SIZE)) > 0.985) * mask
    amp = amp + town * 1.6
    m = f.metres()
    ships_img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    ships_img.fill(QColor(0, 0, 0))
    p = QPainter(ships_img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    for ship in ships:
        c = f.px(ship["lon"], ship["lat"])
        length, width = ship["length"] / m, ship["width"] / m
        p.save()
        p.translate(c)
        p.rotate(ship["heading"] - 90)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(200, 200, 200))
        p.drawPath(ship_path(length, width))
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(-length * 0.35, 0), width * 0.4, width * 0.4)
        p.restore()
        # Side lobes along the image's axes.
        p.setPen(QPen(QColor(110, 110, 110), 1))
        p.drawLine(QPointF(c.x() - length * 0.9, c.y()), QPointF(c.x() + length * 0.9, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - length * 0.6), QPointF(c.x(), c.y() + length * 0.6))
    p.end()
    amp = amp + qimage_rgb(ships_img)[..., 0].astype(np.float32) / 255 * 2.2
    speckle = rnd.gamma(4.0, 0.25, (SIZE, SIZE)).astype(np.float32)
    value = np.clip(np.sqrt(amp * speckle) * 190, 0, 255).astype(np.uint8)
    return np.repeat(value[..., None], 3, axis=2)


def qimage_rgb(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width() * 3].reshape(img.height(), img.width(), 3).copy()


def write(path: str, rgb: np.ndarray, f: Frame) -> None:
    tmp = path + ".new.tif"
    ds = gdal.GetDriverByName("GTiff").Create(tmp, SIZE, SIZE, 3, gdal.GDT_Byte, ["COMPRESS=DEFLATE", "TILED=YES", "PHOTOMETRIC=RGB"])
    if ds is None:  # (GDAL's exceptions are left as QGIS has them: not turned on for the whole of QGIS)
        raise OSError(f"画像を書けません: {tmp}")
    ds.SetGeoTransform((f.west, f.dx, 0, f.north, 0, -f.dy))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for band in range(3):
        ds.GetRasterBand(band + 1).WriteArray(rgb[..., band])
    ds.FlushCache()
    ds = None
    os.replace(tmp, path)


#: The background map's area (west, south, east, north) and its size in pixels.
BASEMAP = (139.45, 34.95, 140.10, 35.65)
BASEMAP_SIZE = (2400, 2600)


def draw_basemap() -> np.ndarray:
    """A plain map of the same made-up coast (land, sea, piers) under the images, for the planning steps."""
    w, h = BASEMAP_SIZE
    west, south, east, north = BASEMAP
    dx, dy = (east - west) / w, (north - south) / h
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(205, 225, 238))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath(QPointF(-10, -10))
    for y in range(-10, h + 11, 3):
        lat = north - y * dy
        path.lineTo((coast_lon(lat) - west) / dx, y)
    path.lineTo(-10, h + 10)
    path.closeSubpath()
    p.setPen(QPen(QColor(150, 170, 185), 1.5))
    p.setBrush(QColor(240, 236, 226))
    p.drawPath(path)
    p.setBrush(QColor(225, 222, 214))
    m = dx * 111_320 * math.cos(math.radians(35.3))
    for lat, length, width, bearing in PIERS:
        x, y = (coast_lon(lat) - 0.0004 - west) / dx, (north - lat) / dy
        t = QTransform().translate(x, y).rotate(bearing - 90)
        p.drawPolygon(t.map(QPolygonF(QRectF(0, -width / m / 2, length / m, width / m))))
    p.end()
    return qimage_rgb(img)


def write_basemap(path: str) -> None:
    w, h = BASEMAP_SIZE
    west, south, east, north = BASEMAP
    rgb = draw_basemap()
    tmp = path + ".new.tif"
    ds = gdal.GetDriverByName("GTiff").Create(tmp, w, h, 3, gdal.GDT_Byte, ["COMPRESS=DEFLATE", "TILED=YES", "PHOTOMETRIC=RGB"])
    if ds is None:
        raise OSError(f"背景地図を書けません: {tmp}")
    ds.SetGeoTransform((west, (east - west) / w, 0, north, 0, -(north - south) / h))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for band in range(3):
        ds.GetRasterBand(band + 1).WriteArray(rgb[..., band])
    ds = None
    os.replace(tmp, path)


def draw_images(data: str, log=lambda _text: None, cancelled=lambda: False) -> int:
    """
    The images of the server's data folder (`data`: files/catalog/images.json
    says which) drawn over the cartoons, and basemap.tif; how many images.
    """
    folder = os.path.join(data, "files", "catalog")
    with open(os.path.join(folder, "images.json"), encoding="utf-8") as fh:
        plans = json.load(fh)
    for plan in plans:
        if cancelled():
            return 0
        f = Frame(plan["box"])
        rgb = draw_sar(f, plan["ships"], plan["seed"]) if plan["sar"] else draw_optical(f, plan["ships"], plan["seed"])
        write(os.path.join(folder, plan["id"] + ".tif"), rgb, f)
        log(f"画像: {plan['id']}")
    write_basemap(basemap_path(data))
    return len(plans)


def basemap_path(data: str) -> str:
    """練習用の背景地図 of the data folder."""
    return os.path.join(data, "basemap.tif")
