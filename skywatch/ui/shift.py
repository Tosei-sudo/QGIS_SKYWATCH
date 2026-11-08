"""
「位置ずらし」 (the viewer's shift): the active image is moved on the map by
hand, to line up an image whose georeferencing is off by some metres. While
the tool is on, dragging the map moves the image (a copy of it follows the
mouse, the move is made on release) and the arrow keys move it by a screen
pixel (Shift: 10); Esc puts the tool away. How far it has been moved shows
in metres east and north. 「位置ずらしの解除」 puts it back where its file
says. The move itself is qgis_io.shift_io.
"""
from __future__ import annotations

import math
from typing import Optional

from qgis.core import (
    QgsCoordinateTransform, QgsDistanceArea, QgsMapRendererParallelJob, QgsMapSettings, QgsPointXY, QgsProject, QgsRectangle,
)
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem, QgsMapTool
from qgis.PyQt.QtCore import QObject, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPen

from ..qgis_io.shift_io import can_shift, get_shift, set_shift


def format_shift(east: float, north: float) -> str:
    """`ずらし量: 東 +12.0 m、北 −3.5 m`"""
    def f(v: float) -> str:
        return f"{'+' if v >= 0 else '−'}{abs(v):.1f} m"
    return f"ずらし量: 東 {f(east)}、北 {f(north)}"


class ShiftOverlay(QgsMapCanvasItem):
    """The image as drawn when the drag began, painted where the mouse has moved it, with its outline."""

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.image: Optional[QImage] = None
        self.offset = QPointF(0, 0)
        self.setZValue(900)

    def changed(self) -> None:
        self.setPos(0, 0)
        self.prepareGeometryChange()
        self.update()

    def updatePosition(self) -> None:  # noqa: N802 (QGIS's name)
        self.setPos(0, 0)

    def boundingRect(self) -> QRectF:  # noqa: N802
        size = self.canvas_.size()
        return QRectF(0, 0, size.width(), size.height())

    def paint(self, painter, option=None, widget=None) -> None:
        if self.image is None:
            return
        size = self.canvas_.size()
        target = QRectF(self.offset.x(), self.offset.y(), size.width(), size.height())
        painter.setOpacity(0.85)
        painter.drawImage(target, self.image, QRectF(0, 0, self.image.width(), self.image.height()))
        painter.setOpacity(1)
        painter.setPen(QPen(QColor("#2563eb"), 1.5, Qt.PenStyle.DashLine))
        painter.drawRect(target.adjusted(1, 1, -1, -1))


class ShiftTool(QgsMapTool):
    """Drags the image (moved on release), arrow keys move it, Esc ends."""

    def __init__(self, canvas, shift: "LayerShift"):
        super().__init__(canvas)
        self.shift = shift
        self._start = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def canvasPressEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() != Qt.MouseButton.LeftButton or self.shift.layer() is None:
            return
        self._start = event.pos()
        self.shift.begin_drag()

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            self.shift.overlay.offset = QPointF(event.pos() - self._start)
            self.shift.overlay.changed()

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if self._start is None:
            return
        start, self._start = self._start, None
        to_map = self.canvas().getCoordinateTransform()
        a, b = to_map.toMapCoordinates(start), to_map.toMapCoordinates(event.pos())
        self.shift.end_drag()
        if start != event.pos():
            self.shift.move_by_map(b.x() - a.x(), b.y() - a.y())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = (10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1) * self.canvas().mapUnitsPerPixel()
        moves = {Qt.Key.Key_Left: (-step, 0), Qt.Key.Key_Right: (step, 0), Qt.Key.Key_Up: (0, step), Qt.Key.Key_Down: (0, -step)}
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.shift.set_active(False)
        elif key in moves and self.shift.layer() is not None:
            self.shift.move_by_map(*moves[key])
        else:
            event.ignore()
            return
        event.accept()


class LayerShift(QObject):
    """The shift: its toggle `action`, the active image it moves, the tool and the overlay."""

    def __init__(self, iface, action, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.canvas: QgsMapCanvas = iface.mapCanvas()
        self.action = action
        action.setCheckable(True)
        action.toggled.connect(self.set_active)
        self.active = False
        self.overlay = ShiftOverlay(self.canvas)
        self.overlay.hide()
        self.tool = ShiftTool(self.canvas, self)
        self._previous_tool = None
        self.canvas.mapToolSet.connect(self._tool_set)
        self.iface.currentLayerChanged.connect(self._current_changed)

    def layer(self):
        """The image moved: the active layer when it is a file image."""
        layer = self.iface.activeLayer()
        return layer if can_shift(layer) else None

    # On and off.

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        if self.action.isChecked() != active:
            self.action.blockSignals(True)
            self.action.setChecked(active)
            self.action.blockSignals(False)
        if active:
            if self.canvas.mapTool() is not self.tool:
                self._previous_tool = self.canvas.mapTool()
                self.canvas.setMapTool(self.tool)
            layer = self.layer()
            self.say(self.tr("{name} を地図のドラッグか矢印キー（Shift で 10 倍）で移動。Esc で終了。{shift}").format(
                name=layer.name(), shift=self.readout(layer)) if layer else self.tr("ファイルの画像を選ぶと、ドラッグで位置をずらせる"))
        else:
            self.end_drag()
            if self.canvas.mapTool() is self.tool:
                try:
                    self.canvas.setMapTool(self._previous_tool) if self._previous_tool is not None else self.canvas.unsetMapTool(self.tool)
                except RuntimeError:  # the tool went with its plugin
                    self.canvas.unsetMapTool(self.tool)

    def _tool_set(self, new, old=None) -> None:
        if self.active and new is not self.tool:
            self.set_active(False)

    def _current_changed(self, *_) -> None:
        if self.active and self.layer() is not None:
            self.show_shift()

    # The drag's copy of the image.

    def begin_drag(self) -> None:
        layer = self.layer()
        if layer is None:
            return
        settings = QgsMapSettings(self.canvas.mapSettings())
        settings.setLayers([layer])
        settings.setBackgroundColor(QColor(0, 0, 0, 0))
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        self.overlay.image = job.renderedImage()
        self.overlay.offset = QPointF(0, 0)
        self.overlay.show()
        self.overlay.changed()

    def end_drag(self) -> None:
        self.overlay.hide()
        self.overlay.image = None

    # The move.

    def move_by_map(self, dx: float, dy: float) -> None:
        """Moves the image by dx, dy in the map's CRS (turned into the image's own CRS where it lies)."""
        layer = self.layer()
        if layer is None:
            return
        to = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), layer.crs(), QgsProject.instance())
        centre = to.transform(layer.extent().center(), QgsCoordinateTransform.TransformDirection.ReverseTransform)
        a = to.transform(centre)
        b = to.transform(QgsPointXY(centre.x() + dx, centre.y() + dy))
        x, y = get_shift(layer)
        self.set_shift(layer, x + b.x() - a.x(), y + b.y() - a.y())

    def set_shift(self, layer, dx: float, dy: float) -> None:
        """Moves the image to dx, dy (its CRS's units) from where its file puts it."""
        try:
            set_shift(layer, dx, dy)
        except OSError as e:
            self.iface.messageBar().pushWarning(self.tr("位置ずらし"), str(e))
            return
        self.show_shift()

    def set_shift_metres(self, layer, east: float, north: float) -> None:
        """Moves the image to `east`, `north` metres from where its file puts it."""
        mx, my = self.metres_per_unit(layer)
        self.set_shift(layer, east / mx, north / my)

    def reset(self) -> None:
        """Puts the active image back where its file says."""
        layer = self.iface.activeLayer()
        if not can_shift(layer):
            return
        if get_shift(layer) == (0.0, 0.0):
            self.say(self.tr("{name} はずらしなし").format(name=layer.name()))
            return
        self.set_shift(layer, 0, 0)

    def metres_per_unit(self, layer) -> tuple[float, float]:
        """Ground metres per unit of the image's CRS east and north, at its middle."""
        measure = QgsDistanceArea()
        measure.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
        measure.setEllipsoid("EPSG:7030")
        extent: QgsRectangle = layer.extent()
        c = extent.center()
        d = max(extent.width(), extent.height()) / 100 or 1
        try:
            east = measure.measureLine(c, QgsPointXY(c.x() + d, c.y())) / d
            north = measure.measureLine(c, QgsPointXY(c.x(), c.y() + d)) / d
        except Exception:  # noqa: BLE001 (outside the CRS's area)
            return 1.0, 1.0
        return (east if math.isfinite(east) and east > 0 else 1.0), (north if math.isfinite(north) and north > 0 else 1.0)

    def shift_metres(self, layer) -> tuple[float, float]:
        """How far the image has been moved, metres east and north."""
        dx, dy = get_shift(layer)
        mx, my = self.metres_per_unit(layer)
        return dx * mx, dy * my

    def readout(self, layer) -> str:
        return format_shift(*self.shift_metres(layer))

    def show_shift(self) -> None:
        layer = self.layer()
        if layer is None:
            return
        text = f"{layer.name()}: {self.readout(layer)}"
        bar = self.iface.statusBarIface() if hasattr(self.iface, "statusBarIface") else None
        if bar is not None:
            bar.showMessage(text, 10000)
        self.last_readout = text

    def say(self, text: str) -> None:
        self.iface.messageBar().pushInfo(self.tr("位置ずらし"), text)

    def close(self) -> None:
        self.set_active(False)
        for signal, slot in ((self.canvas.mapToolSet, self._tool_set), (self.iface.currentLayerChanged, self._current_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.canvas.scene().removeItem(self.overlay)
