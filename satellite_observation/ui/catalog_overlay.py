"""
What the image catalog draws on the map while its dock is open, as the
viewer's footprint layer: the listed images' footprints (blue), the chosen
ones (orange, with their id), the area drawn for the search (green,
dashed) and, for 「類似画像の検索」, the image compared
with (red, dashed). Rubber bands only: nothing is added to the project. A
map tool picks footprints by a click (Ctrl / Shift: adds); another draws
the area.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

from ..qgis_io.geometry import WGS84, from_wgs84, to_wgs84
from .plan_overlay import LabelItem


def _band(canvas: QgsMapCanvas, stroke: str, width: int, fill: QColor, z: float, dash=None) -> QgsRubberBand:
    band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
    band.setStrokeColor(QColor(stroke))
    band.setWidth(width)
    band.setFillColor(fill)
    if dash is not None:
        band.setLineStyle(dash)
    band.setZValue(z)
    return band


class FootprintOverlay:
    def __init__(self, canvas: QgsMapCanvas):
        self.canvas = canvas
        self.all = _band(canvas, "#2563eb", 1, QColor(37, 99, 235, 10), 950)
        self.chosen = _band(canvas, "#f59e0b", 3, QColor(245, 158, 11, 46), 952)
        self.reference = _band(canvas, "#dc2626", 2, QColor(0, 0, 0, 0), 951, Qt.PenStyle.DashLine)
        self.area = _band(canvas, "#16a34a", 2, QColor(22, 163, 74, 20), 949, Qt.PenStyle.DashLine)
        self._labels: list = []
        self._geometries: list[Optional[QgsGeometry]] = []

    def show(self, geometries: Sequence[Optional[QgsGeometry]], reference: Optional[QgsGeometry] = None) -> None:
        """Draws the footprints of the list (WGS 84; None for an image without one) and the image compared with."""
        self._geometries = list(geometries)
        self.all.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for g in self._geometries:
            if g is not None:
                self.all.addGeometry(g, WGS84)
        self.reference.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        if reference is not None:
            self.reference.addGeometry(reference, WGS84)
        self.choose([], [])

    def choose(self, indexes: Sequence[int], labels: Sequence[str]) -> None:
        """Marks the chosen footprints with their labels."""
        self.chosen.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for item in self._labels:
            self.canvas.scene().removeItem(item)
        self._labels = []
        crs = self.canvas.mapSettings().destinationCrs()
        for i, label in zip(indexes, labels):
            g = self._geometries[i] if 0 <= i < len(self._geometries) else None
            if g is None:
                continue
            self.chosen.addGeometry(g, WGS84)
            if label:
                p = from_wgs84(crs).transform(g.pointOnSurface().asPoint())
                self._labels.append(LabelItem(self.canvas, p, label, "#111111", False))

    def extent(self, indexes: Sequence[int]) -> Optional[QgsRectangle]:
        """The WGS 84 extent of some footprints."""
        box: Optional[QgsRectangle] = None
        for i in indexes:
            g = self._geometries[i] if 0 <= i < len(self._geometries) else None
            if g is None:
                continue
            if box is None:
                box = QgsRectangle(g.boundingBox())
            else:
                box.combineExtentWith(g.boundingBox())
        return box

    def at(self, lon: float, lat: float, tolerance: float) -> Optional[int]:
        """The footprint under a point (the smallest when they overlap); None for none."""
        point = QgsGeometry.fromPointXY(QgsPointXY(lon, lat)).buffer(tolerance, 4)
        hits = [(g.area(), i) for i, g in enumerate(self._geometries) if g is not None and g.intersects(point)]
        return min(hits)[1] if hits else None

    def show_area(self, ring: Optional[Sequence[Sequence[float]]]) -> None:
        """The area drawn for the search (green, dashed); None hides it."""
        self.area.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        if ring:
            self.area.addGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]]), WGS84)

    def clear(self) -> None:
        self.show([])

    def remove(self) -> None:
        self.clear()
        for band in (self.all, self.chosen, self.reference, self.area):
            self.canvas.scene().removeItem(band)


class FootprintTool(QgsMapTool):
    """A click picks the footprint under it; Ctrl or Shift adds it to the ones chosen."""

    def __init__(self, canvas: QgsMapCanvas, picked: Callable[[float, float, float, bool], None]):
        super().__init__(canvas)
        self._picked = picked
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        canvas = self.canvas()
        crs = canvas.mapSettings().destinationCrs()
        p = to_wgs84(crs).transform(event.mapPoint())
        # Three pixels around the click, in degrees.
        q = to_wgs84(crs).transform(QgsPointXY(event.mapPoint().x() + 3 * canvas.mapUnitsPerPixel(), event.mapPoint().y()))
        add = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
        self._picked(p.x(), p.y(), abs(q.x() - p.x()) or 1e-6, add)


class DrawTool(QgsMapTool):
    """
    Draws a polygon on the map: a click adds a corner, a double click or a
    right click closes it (three corners at least), Esc gives up. `drawn` is
    told its corners (WGS 84, longitude / latitude).
    """

    def __init__(self, canvas: QgsMapCanvas, drawn: Callable[[list], None]):
        super().__init__(canvas)
        self._drawn = drawn
        self.points: list[QgsPointXY] = []
        self.band = _band(canvas, "#16a34a", 2, QColor(22, 163, 74, 30), 953, Qt.PenStyle.DashLine)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _show(self, moving: Optional[QgsPointXY] = None) -> None:
        self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for p in self.points + ([moving] if moving is not None else []):
            self.band.addPoint(p, False)
        self.band.updatePosition()
        self.band.update()

    def canvasMoveEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if self.points:
            self._show(event.mapPoint())

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.finish()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.points.append(QgsPointXY(event.mapPoint()))
            self._show()

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        # The double click's second press already added the last corner.
        self.finish()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.points = []
            self._show()

    def finish(self) -> None:
        unique = [p for i, p in enumerate(self.points) if i == 0 or p != self.points[i - 1]]
        self.points = []
        self._show()
        if len(unique) < 3:
            return
        transform = to_wgs84(self.canvas().mapSettings().destinationCrs())
        corners = [transform.transform(p) for p in unique]
        self._drawn([[p.x(), p.y()] for p in corners])

    def deactivate(self) -> None:
        self.points = []
        self._show()
        super().deactivate()

    def remove(self) -> None:
        self.canvas().scene().removeItem(self.band)
