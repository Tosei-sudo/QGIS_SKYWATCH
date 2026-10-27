"""
What the imaging plan draws on the map while its dock is open, as the
viewer's overlay: the targets, the chosen pass (its ground track, reach,
scenes and the satellite), and in a combination the other passes numbered.
Rubber bands and canvas items only: nothing is added to the project.
"""
from __future__ import annotations

from typing import Optional, Sequence

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontMetricsF, QPainterPath, QPen

from ..core.plan.output import split_antimeridian
from ..qgis_io.geometry import WGS84, from_wgs84, line_of, multipolygon_of, polygon_of

STYLES = {
    # kind: (stroke, width, dash, fill)
    "target": ("#f97316", 2, None, QColor(249, 115, 22, 20)),
    "target_point": ("#f97316", 2, None, QColor("#f97316")),
    "track": ("#475569", 2, Qt.PenStyle.DashLine, None),
    "reach": ("#0ea5e9", 1, Qt.PenStyle.DotLine, None),
    "scene": ("#2563eb", 2, None, QColor(37, 99, 235, 64)),
    "step": ("#7c3aed", 1, Qt.PenStyle.DashLine, QColor(124, 58, 237, 31)),
}


class LabelItem(QgsMapCanvasItem):
    """A text on the map with a white halo (the numbers of a combination's passes, the satellite's name)."""

    def __init__(self, canvas: QgsMapCanvas, point: QgsPointXY, text: str, color: str, bold: bool = True, offset_y: float = 0,
                 beside: bool = False):
        super().__init__(canvas)
        self._map_point = point
        self._text = text
        self._color = QColor(color)
        self._font = QFont()
        self._font.setBold(bold)
        self._font.setPointSizeF(10)
        self._offset_y = offset_y
        #: Beside the point (to its right) rather than centred on it.
        self._beside = beside
        # Above the rubber bands, so a number on a scene is not under its fill.
        self.setZValue(2000)
        self.updatePosition()

    def updatePosition(self) -> None:  # noqa: N802 (Qt's name)
        self.setPos(self.toCanvasCoordinates(self._map_point))

    def boundingRect(self) -> QRectF:  # noqa: N802
        w = QFontMetricsF(self._font).horizontalAdvance(self._text) + 8
        return QRectF(6 if self._beside else -w / 2, -14 + self._offset_y, w, 20)

    def paint(self, painter, option=None, widget=None) -> None:
        path = QPainterPath()
        w = QFontMetricsF(self._font).horizontalAdvance(self._text)
        path.addText(QPointF(10 if self._beside else -w / 2, 4 + self._offset_y), self._font, self._text)
        painter.setPen(QPen(QColor("white"), 3))
        painter.drawPath(path)
        painter.fillPath(path, self._color)


def _geometry_type(kind: str):
    if kind == "target_point":
        return QgsWkbTypes.GeometryType.PointGeometry
    if kind in ("track", "reach"):
        return QgsWkbTypes.GeometryType.LineGeometry
    return QgsWkbTypes.GeometryType.PolygonGeometry


class PlanOverlay:
    def __init__(self, canvas: QgsMapCanvas):
        self.canvas = canvas
        self._bands: dict[str, QgsRubberBand] = {}
        self._items: list = []
        self._extent: dict[str, QgsRectangle] = {}
        #: Rubber bands each with its own colour (the revisit map's cells, the saved plan's shots).
        self._painted: list[QgsRubberBand] = []
        self._painted_items: list = []

    def _band(self, kind: str) -> QgsRubberBand:
        band = self._bands.get(kind)
        if band is None:
            stroke, width, dash, fill = STYLES[kind]
            band = QgsRubberBand(self.canvas, _geometry_type(kind))
            band.setStrokeColor(QColor("white") if kind == "target_point" else QColor(stroke))
            if kind == "target_point":
                band.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
                band.setIconSize(10)
            band.setWidth(width)
            if dash is not None:
                band.setLineStyle(dash)
            band.setFillColor(fill if fill is not None else QColor(0, 0, 0, 0))
            band.setZValue(1000 + list(STYLES).index(kind))
            self._bands[kind] = band
        return band

    def _add(self, kind: str, geometry: QgsGeometry) -> None:
        self._band(kind).addGeometry(geometry, WGS84)
        box = geometry.boundingBox()
        self._extent.setdefault(kind, QgsRectangle(box)).combineExtentWith(box)

    def clear(self, kinds: Optional[Sequence[str]] = None) -> None:
        if kinds is None:
            self.unpaint()
        for kind, band in self._bands.items():
            if kinds is None or kind in kinds:
                band.reset(_geometry_type(kind))
                self._extent.pop(kind, None)
        if kinds is None or "label" in kinds:
            for item in self._items:
                self.canvas.scene().removeItem(item)
            self._items = []

    def paint(self, polygons: Sequence[tuple], z: float = 900) -> None:
        """
        Polygons each in its own colour: (rings, colour, width, label) with
        rings in WGS84 and the label (or '') at the middle of the first ring.
        Replaces what was painted before.
        """
        self.unpaint()
        for rings, color, width, label in polygons:
            band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
            c = QColor(color)
            band.setStrokeColor(c)
            fill = QColor(c)
            fill.setAlpha(80)
            band.setFillColor(fill)
            band.setWidth(int(width))
            band.setZValue(z)
            geometry = polygon_of(rings[:1]) if len(rings) == 1 else multipolygon_of(rings)
            band.addGeometry(geometry, WGS84)
            self._painted.append(band)
            box = geometry.boundingBox()
            self._extent.setdefault("painted", QgsRectangle(box)).combineExtentWith(box)
            if label:
                c = polygon_of(rings[:1]).pointOnSurface().asPoint()
                p = from_wgs84(self.canvas.mapSettings().destinationCrs()).transform(c)
                self._painted_items.append(LabelItem(self.canvas, p, label, "#111111", False))

    def unpaint(self) -> None:
        for band in self._painted:
            self.canvas.scene().removeItem(band)
        for item in self._painted_items:
            self.canvas.scene().removeItem(item)
        self._painted, self._painted_items = [], []
        self._extent.pop("painted", None)

    def remove(self) -> None:
        self.unpaint()
        self.clear()
        for band in self._bands.values():
            self.canvas.scene().removeItem(band)
        self._bands = {}

    def targets(self, geometries: Sequence[QgsGeometry], crs) -> None:
        """The targets' features (in `crs`)."""
        self.clear(["target", "target_point"])
        for g in geometries:
            point = g.type() == QgsWkbTypes.GeometryType.PointGeometry
            self._band("target_point" if point else "target").addGeometry(g, crs)

    def point_target(self, lon: float, lat: float) -> None:
        self.clear(["target", "target_point"])
        self._band("target_point").addGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)), WGS84)

    def _marker(self, lon: float, lat: float, color: str) -> QgsVertexMarker:
        marker = QgsVertexMarker(self.canvas)
        p = from_wgs84(self.canvas.mapSettings().destinationCrs()).transform(QgsPointXY(lon, lat))
        marker.setCenter(p)
        marker.setColor(QColor("white"))
        marker.setFillColor(QColor(color))
        marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        marker.setIconSize(12)
        marker.setPenWidth(2)
        self._items.append(marker)
        return marker

    def _label(self, lon: float, lat: float, text: str, color: str, bold: bool = True, offset_y: float = 0, beside: bool = False) -> None:
        p = from_wgs84(self.canvas.mapSettings().destinationCrs()).transform(QgsPointXY(lon, lat))
        self._items.append(LabelItem(self.canvas, p, text, color, bold, offset_y, beside))

    def chosen(self, shapes, label: str, steps: Sequence[tuple] = ()) -> None:
        """A pass (`shapes` of core.plan.scene.pass_shapes) and the satellite's name; `steps`: (number, scenes) of the other passes."""
        self.clear(["track", "reach", "scene", "step", "label"])
        self._extent.pop("satellite", None)
        for part in split_antimeridian(shapes.track):
            self._add("track", line_of(part))
        for edge in shapes.reach:
            for part in split_antimeridian(edge):
                self._add("reach", line_of(part))
        for ring in shapes.scenes:
            self._add("scene", polygon_of([ring]))
        for number, rings in steps:
            for k, ring in enumerate(rings):
                self._add("step", polygon_of([ring]))
                # The number once, on the middle scene.
                if k == len(rings) // 2:
                    c = polygon_of([ring]).centroid().asPoint()
                    self._label(c.x(), c.y(), str(number), "#4c1d95")
        self._marker(shapes.position[0], shapes.position[1], "#1e3a5f")
        self._extent["satellite"] = QgsRectangle(shapes.position[0], shapes.position[1], shapes.position[0], shapes.position[1])
        self._label(shapes.position[0], shapes.position[1], label, "#111111", False, 0, True)

    def extent(self, kinds: Sequence[str]) -> Optional[QgsRectangle]:
        """The extent (WGS84) of what is drawn of `kinds`."""
        out: Optional[QgsRectangle] = None
        for kind in kinds:
            box = self._extent.get(kind)
            if box is None:
                continue
            if out is None:
                out = QgsRectangle(box)
            else:
                out.combineExtentWith(box)
        return out
