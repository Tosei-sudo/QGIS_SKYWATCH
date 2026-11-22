"""
判読の練習's marks on the map (ui/exercise_run.py): the exercise's area (an
amber dashed outline), the numbered red rings of the things to look at
(見分ける・測る), and after the review the answers as they came out — green
(正しい), orange (種別・活動・測る・変化のちがい), red dashed (見落とし) and a grey
× (誤り: a record of nothing). Painted over the map (not a layer: nothing
of it is kept in the project). `fit_into` / `free_part` move the map so that
what is to be seen is beside the exercise's windows, not under them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from qgis.core import QgsPointXY, QgsRectangle
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem, QgsMapTool
from qgis.PyQt.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPainterPath, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QWidget

from ..qgis_io.geometry import from_wgs84, to_wgs84

AREA_COLOR = QColor("#d97706")
COLORS = {"target": QColor("#dc2626"), "right": QColor("#16a34a"), "wrong": QColor("#ea580c"), "missed": QColor("#dc2626"),
          "extra": QColor("#6b7280")}
#: The ring's radius on the screen (pixels).
RADIUS = 15


def global_rect(widget: QWidget) -> QRect:
    """A widget's rectangle on the screen (a window's with its frame: its title bar covers the map too)."""
    if widget.isWindow():
        return widget.frameGeometry()
    return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())


def free_part(canvas: QgsMapCanvas, windows: Iterable[Optional[QWidget]]) -> QRect:
    """
    The part of the map (its pixels) no window covers (nor a widget over it: 練習モード's band): beside each, the
    largest side left (the left of one at the right, below one at the top); one that would leave too little is not
    minded.
    """
    free = QRect(0, 0, canvas.width(), canvas.height())
    for window in windows:
        try:
            if window is None or not window.isVisible():
                continue
            g = global_rect(window)
            cut = QRect(canvas.mapFromGlobal(g.topLeft()), g.size()).intersected(free)
        except RuntimeError:
            continue
        if cut.isEmpty():
            continue
        sides = [QRect(free.left(), free.top(), cut.left() - free.left(), free.height()),
                 QRect(cut.right() + 1, free.top(), free.right() - cut.right(), free.height()),
                 QRect(free.left(), free.top(), free.width(), cut.top() - free.top()),
                 QRect(free.left(), cut.bottom() + 1, free.width(), free.bottom() - cut.bottom())]
        best = max(sides, key=lambda r: max(r.width(), 0) * max(r.height(), 0))
        if best.width() * best.height() >= 0.3 * free.width() * free.height():
            free = best
    return free


def fit_into(canvas: QgsMapCanvas, west: float, south: float, east: float, north: float, part: Optional[QRect] = None) -> None:
    """The map moved to show a box (longitude / latitude) in a part of it (pixels; None: all of it)."""
    box = from_wgs84(canvas.mapSettings().destinationCrs()).transformBoundingBox(QgsRectangle(west, south, east, north))
    w, h = max(canvas.width(), 1), max(canvas.height(), 1)
    part = part if part is not None and not part.isEmpty() else QRect(0, 0, w, h)
    units = max(box.width() / max(part.width(), 1), box.height() / max(part.height(), 1)) or canvas.mapUnitsPerPixel()
    # (the box's middle at the part's middle)
    x = box.center().x() - (part.left() + part.width() / 2 - w / 2) * units
    y = box.center().y() + (part.top() + part.height() / 2 - h / 2) * units
    canvas.setExtent(QgsRectangle(x - w / 2 * units, y - h / 2 * units, x + w / 2 * units, y + h / 2 * units))
    canvas.refresh()


def pixel_of(canvas: QgsMapCanvas, lon: float, lat: float) -> Optional[QPointF]:
    """A place's pixel on the map (None when the map's CRS cannot show it)."""
    try:
        p = from_wgs84(canvas.mapSettings().destinationCrs()).transform(QgsPointXY(lon, lat))
    except Exception:  # noqa: BLE001
        return None
    q = canvas.getCoordinateTransform().transform(p)
    return QPointF(q.x(), q.y())


@dataclass
class Spot:
    lon: float
    lat: float
    label: str
    #: target (to look at), right, wrong, missed or extra (×).
    style: str = "target"


class ExerciseMarks(QgsMapCanvasItem):
    """The exercise's area and spots, over the whole map."""

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.area: list = []
        self.spots: list[Spot] = []
        #: The spot chosen in the review (drawn thicker), None for none.
        self.chosen: Optional[int] = None
        self.setZValue(1090)

    def show_marks(self, area: Sequence, spots: Sequence[Spot], chosen: Optional[int] = None) -> None:
        self.area, self.spots, self.chosen = list(area), list(spots), chosen
        self.updatePosition()
        self.update()

    def remove(self) -> None:
        try:
            self.canvas_.scene().removeItem(self)
        except RuntimeError:  # (QGIS closing)
            pass

    def updatePosition(self) -> None:  # noqa: N802 (QGIS's name)
        self.setPos(0, 0)
        self.prepareGeometryChange()

    def boundingRect(self) -> QRectF:  # noqa: N802
        size = self.canvas_.size()
        return QRectF(0, 0, size.width(), size.height())

    def to_pixel(self, lon: float, lat: float) -> QPointF:
        try:
            p = from_wgs84(self.canvas_.mapSettings().destinationCrs()).transform(QgsPointXY(lon, lat))
        except Exception:  # noqa: BLE001 (a place the CRS cannot show)
            return QPointF(-1e6, -1e6)
        return self.toCanvasCoordinates(p)

    def paint(self, painter, option=None, widget=None) -> None:
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        if self.area:
            path = QPainterPath()
            for ring in self.area:
                path.addPolygon(QPolygonF([self.to_pixel(x, y) for x, y in ring]))
                path.closeSubpath()
            pen = QPen(AREA_COLOR, 2.4, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        font = QFont(painter.font())
        font.setBold(True)
        font.setPointSizeF(max(9.0, font.pointSizeF()))
        painter.setFont(font)
        for i, spot in enumerate(self.spots):
            c = self.to_pixel(spot.lon, spot.lat)
            color = COLORS.get(spot.style, COLORS["target"])
            width = 4.0 if i == self.chosen else 2.6
            if spot.style == "extra":
                d = RADIUS * 0.85
                # (white under the grey: seen over the dark sea and the record's own point)
                for pen in (QPen(QColor(255, 255, 255, 230), width + 2.5), QPen(color, width)):
                    painter.setPen(pen)
                    painter.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
                    painter.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
            else:
                painter.setPen(QPen(color, width, Qt.PenStyle.DashLine if spot.style == "missed" else Qt.PenStyle.SolidLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                r = RADIUS + (3 if i == self.chosen else 0)
                painter.drawEllipse(c, r, r)
            if spot.label:
                # (below right of the ring: the observation records' own words are above their points)
                at = QPointF(c.x() + RADIUS * 0.6, c.y() + RADIUS + 15)
                # A light halo under the words (read over a dark sea and a light land).
                painter.setPen(QPen(QColor(255, 255, 255, 230), 3.2))
                outline = QPainterPath()
                outline.addText(at, font, spot.label)
                painter.drawPath(outline)
                painter.setPen(QPen(color))
                painter.drawText(at, spot.label)


class ClickTool(QgsMapTool):
    """
    判読の練習の問題を作る's 「地図で足す」: each click on the map is a place (`clicked(lon, lat)`, WGS 84); Esc or the
    right button ends it (`ended()`).
    """

    def __init__(self, canvas: QgsMapCanvas, clicked, ended=None):
        super().__init__(canvas)
        self._clicked, self._ended = clicked, ended
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.button() == Qt.MouseButton.RightButton:
            if self._ended is not None:
                self._ended()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        p = to_wgs84(self.canvas().mapSettings().destinationCrs()).transform(event.mapPoint())
        self._clicked(p.x(), p.y())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._ended is not None:
            self._ended()
