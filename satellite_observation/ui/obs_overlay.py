"""
What the observations draw on the map while their dock is open, as the
viewer's overlay: this image's records (filled marks, the ring in the colour
of their 確度, a diamond for 「不在」), the things carried over from other
images (hollow marks), records measured by two points (a line with an arrow
at the front), the chosen thing's track, the checked areas, the place
clicked and the line to the pointer while measuring.

One canvas item paints everything (nothing is added to the project); the
places are WGS 84 and follow the canvas's CRS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from qgis.core import QgsPointXY
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainterPath, QPen, QPolygonF

from ..qgis_io.geometry import from_wgs84

#: Colours of the top classes, in the order they come (as the viewer).
PALETTE = ["#e11d48", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5"]


@dataclass
class Mark:
    """A thing on the map: a record of this image (`obs`), one carried over (`ghost`), a step of the chosen flow (`step`)."""
    role: str
    lon: float
    lat: float
    color: str = "#111111"
    label: str = ""
    present: bool = True
    #: The ring's colour (its 確度's), else white.
    ring: Optional[str] = None
    #: The front and rear when measured by two points.
    ends: Optional[tuple] = None
    heading: Optional[float] = None
    editing: bool = False
    #: What it stands for (a record id), for clicks and drags.
    key: Optional[str] = None


@dataclass
class Scene:
    marks: list[Mark] = field(default_factory=list)
    #: (ring, label) of the checked areas of this image.
    areas: list[tuple] = field(default_factory=list)
    track: list[tuple] = field(default_factory=list)
    #: More tracks (the report map: every thing's).
    tracks: list[list[tuple]] = field(default_factory=list)
    pending: Optional[tuple] = None
    #: The area being made.
    area_pending: Optional[list] = None
    #: The front clicked while measuring, the pointer, and the line's label.
    front: Optional[tuple] = None
    cursor: Optional[tuple] = None
    rubber_label: str = ""


def _halo_text(painter, at: QPointF, text: str, color: str, font: QFont, align_left: bool = False) -> None:
    if not text:
        return
    w = QFontMetricsF(font).horizontalAdvance(text)
    path = QPainterPath()
    path.addText(QPointF(at.x() if align_left else at.x() - w / 2, at.y()), font, text)
    painter.setPen(QPen(QColor("white"), 3))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.fillPath(path, QColor(color))


class ScenePainter:
    """Paints a Scene through `to_pixel(lon, lat)` → QPointF (the canvas's, or a report map's)."""

    def __init__(self, to_pixel: Callable[[float, float], QPointF], scale: float = 1.0):
        self.to_pixel = to_pixel
        self.scale = scale
        self._font = QFont()
        self._font.setPointSizeF(9 * scale)
        self._bold = QFont(self._font)
        self._bold.setBold(True)
        #: Where each mark was painted, for hits.
        self.painted: list[tuple[QPointF, Mark]] = []

    def paint(self, painter, s: Scene) -> None:
        k = self.scale
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        self.painted = []
        for ring, label in s.areas:
            self._area(painter, ring, label, "#0f766e", QColor(15, 118, 110, 18))
        if s.area_pending:
            self._area(painter, s.area_pending, "", "#e11d48", QColor(225, 29, 72, 20))
        for track in [s.track, *s.tracks]:
            if len(track) < 2:
                continue
            points = QPolygonF([self.to_pixel(*p) for p in track])
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("white"), 5 * k))
            painter.drawPolyline(points)
            pen = QPen(QColor("#111111"), 2 * k)
            pen.setDashPattern([3, 2])
            painter.setPen(pen)
            painter.drawPolyline(points)
        for mark in s.marks:
            if mark.ends:
                self._line(painter, mark)
        for mark in s.marks:
            self._mark(painter, mark)
        if s.pending:
            self._cross(painter, self.to_pixel(*s.pending))
        if s.front:
            front = self.to_pixel(*s.front)
            self._cross(painter, front)
            if s.cursor:
                cursor = self.to_pixel(*s.cursor)
                painter.setPen(QPen(QColor("white"), 5 * k))
                painter.drawLine(front, cursor)
                pen = QPen(QColor("#e11d48"), 2 * k)
                pen.setDashPattern([3, 2])
                painter.setPen(pen)
                painter.drawLine(front, cursor)
                mid = QPointF((front.x() + cursor.x()) / 2, (front.y() + cursor.y()) / 2 - 10 * k)
                _halo_text(painter, mid, s.rubber_label, "#111111", self._bold)

    def _area(self, painter, ring, label: str, color: str, fill: QColor) -> None:
        points = [self.to_pixel(p[0], p[1]) for p in ring]
        pen = QPen(QColor(color), 2 * self.scale)
        pen.setDashPattern([4, 2])
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawPolygon(QPolygonF(points))
        if label:
            # Its name at its top left corner, clear of the marks inside.
            corner = QPointF(min(p.x() for p in points) + 2, min(p.y() for p in points) - 4)
            _halo_text(painter, corner, label, color, self._font, align_left=True)

    def _cross(self, painter, at: QPointF) -> None:
        r = 10 * self.scale
        painter.setPen(QPen(QColor("#e11d48"), 2 * self.scale))
        painter.drawLine(QPointF(at.x() - r, at.y()), QPointF(at.x() + r, at.y()))
        painter.drawLine(QPointF(at.x(), at.y() - r), QPointF(at.x(), at.y() + r))

    def _line(self, painter, mark: Mark) -> None:
        k = self.scale
        front = self.to_pixel(*mark.ends[0])
        rear = self.to_pixel(*mark.ends[1])
        ghost = mark.role == "ghost"
        painter.setPen(QPen(QColor("white"), (4 if ghost else 6) * k))
        painter.drawLine(front, rear)
        pen = QPen(QColor(mark.color), (2 if ghost else 3) * k)
        if ghost:
            pen.setDashPattern([2, 2])
        painter.setPen(pen)
        painter.drawLine(front, rear)
        # An arrow at the front, along the line on the screen.
        angle = math.atan2(front.y() - rear.y(), front.x() - rear.x())
        size = (6 if ghost else 7) * k
        tip = QPointF(front.x() + math.cos(angle) * size, front.y() + math.sin(angle) * size)
        left = QPointF(front.x() + math.cos(angle + 2.4) * size, front.y() + math.sin(angle + 2.4) * size)
        right = QPointF(front.x() + math.cos(angle - 2.4) * size, front.y() + math.sin(angle - 2.4) * size)
        painter.setPen(QPen(QColor(mark.color if ghost else "white"), 1.5 * k))
        painter.setBrush(QBrush(QColor(255, 255, 255, 150) if ghost else QColor(mark.color)))
        painter.drawPolygon(QPolygonF([tip, left, right]))

    def _mark(self, painter, mark: Mark) -> None:
        k = self.scale
        at = self.to_pixel(mark.lon, mark.lat)
        self.painted.append((at, mark))
        color = QColor(mark.color)
        if mark.role == "obs":
            if mark.present:
                r = (9 if mark.editing else 7) * k
                ring = QColor(mark.ring) if mark.ring else QColor("white")
                painter.setPen(QPen(ring, (3 if mark.ring else 2) * k))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(at, r, r)
            else:
                d = 8 * k
                painter.setPen(QPen(color, 3 * k))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolygon(QPolygonF([QPointF(at.x(), at.y() - d), QPointF(at.x() + d, at.y()), QPointF(at.x(), at.y() + d), QPointF(at.x() - d, at.y())]))
            _halo_text(painter, QPointF(at.x(), at.y() - 12 * k), mark.label, "#111111", self._bold)
        elif mark.role == "ghost":
            pen = QPen(color, 2 * k)
            pen.setDashPattern([2, 2])
            painter.setPen(pen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 90)))
            painter.drawEllipse(at, 7 * k, 7 * k)
            _halo_text(painter, QPointF(at.x(), at.y() - 12 * k), mark.label, "#555555", self._font)
        elif mark.role == "step":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor("#111111" if mark.present else "#dc2626")))
            painter.drawEllipse(at, 3 * k, 3 * k)
            _halo_text(painter, QPointF(at.x(), at.y() + 16 * k), mark.label, "#111111", self._font)


class ObservationOverlay(QgsMapCanvasItem):
    """Paints a Scene over the whole canvas."""

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.scene_ = Scene()
        self.setZValue(1100)
        self._painter = ScenePainter(self.to_pixel)

    @property
    def painted(self) -> list[tuple[QPointF, Mark]]:
        return self._painter.painted

    def set_scene(self, scene: Scene) -> None:
        self.scene_ = scene
        self.updatePosition()
        self.update()

    def updatePosition(self) -> None:  # noqa: N802 (QGIS's name)
        self.setPos(0, 0)
        self.prepareGeometryChange()

    def boundingRect(self) -> QRectF:  # noqa: N802
        size = self.canvas_.size()
        return QRectF(0, 0, size.width(), size.height())

    def to_pixel(self, lon: float, lat: float) -> QPointF:
        transform = from_wgs84(self.canvas_.mapSettings().destinationCrs())
        try:
            p = transform.transform(QgsPointXY(lon, lat))
        except Exception:  # noqa: BLE001 (a place the CRS cannot show)
            return QPointF(-1e6, -1e6)
        return self.toCanvasCoordinates(p)

    def hit(self, x: float, y: float, roles: Sequence[str] = ("obs", "ghost"), tolerance: float = 8) -> Optional[Mark]:
        """The mark painted nearest a pixel, within `tolerance`."""
        best, best_d = None, tolerance
        for p, mark in self.painted:
            if mark.role not in roles:
                continue
            d = math.hypot(p.x() - x, p.y() - y)
            if d <= best_d:
                best, best_d = mark, d
        return best

    def paint(self, painter, option=None, widget=None) -> None:
        self._painter.paint(painter, self.scene_)


def color_of(kind_top: str, kinds: Sequence[str]) -> str:
    """A colour per top class, so a branch reads as one."""
    i = list(kinds).index(kind_top) if kind_top in kinds else len(kinds)
    return PALETTE[i % len(PALETTE)]
