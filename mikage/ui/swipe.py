"""
「スワイプ比較」 (the viewer's swipe.ts): the active layer (or the group
chosen in the layers panel) shows on one side of a line across the map only,
so the layers beneath show on the other side.

The canvas draws every layer as usual; the other side of the line is covered
with the map drawn again without the swiped layers (a render job of its own,
started whenever the canvas draws), so no layer is hidden or changed in the
project. While the tool is on, dragging the line moves it and dragging
elsewhere pans; a double click turns it between a vertical line (the layer
on the left) and a horizontal one (the layer on top); the arrow keys (Shift:
farther), Home and End move it, Esc puts it away.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsLayerTreeGroup, QgsMapLayer, QgsMapRendererParallelJob, QgsMapSettings
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem, QgsMapTool
from qgis.PyQt.QtCore import QObject, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QImage, QPen

VERTICAL, HORIZONTAL = "vertical", "horizontal"
#: Pixels from the line where a press grabs it.
GRAB = 8


def swipe_rect(orientation: str, position: float, width: float, height: float) -> tuple:
    """The part of a map of `width` × `height` where the swiped layer shows (left, top, right, bottom), the line at `position` (0-1)."""
    p = min(1.0, max(0.0, position))
    return (0, 0, round(width * p), height) if orientation == VERTICAL else (0, 0, width, round(height * p))


class SwipeOverlay(QgsMapCanvasItem):
    """The map without the swiped layers, painted on the far side of the line, and the line with its handle."""

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.canvas_ = canvas
        self.position = 0.5
        self.orientation = VERTICAL
        #: The map drawn without the swiped layers, and the extent and size it was drawn for.
        self.image: Optional[QImage] = None
        self.drawn_for: Optional[tuple] = None
        self.setZValue(900)

    def changed(self) -> None:
        self.updatePosition()
        self.update()

    def updatePosition(self) -> None:  # noqa: N802 (QGIS's name)
        self.setPos(0, 0)
        self.prepareGeometryChange()

    def boundingRect(self) -> QRectF:  # noqa: N802
        size = self.canvas_.size()
        return QRectF(0, 0, size.width(), size.height())

    def line_at(self) -> float:
        """Where the line is in canvas pixels (x for a vertical line, y for a horizontal one)."""
        size = self.canvas_.size()
        return (size.width() if self.orientation == VERTICAL else size.height()) * self.position

    def current(self) -> bool:
        """Whether the image was drawn for what the canvas shows now."""
        s = self.canvas_.mapSettings()
        return self.image is not None and self.drawn_for == (s.visibleExtent().toString(), s.outputSize(), s.rotation())

    def paint(self, painter, option=None, widget=None) -> None:
        size = self.canvas_.size()
        w, h = size.width(), size.height()
        _, _, right, bottom = swipe_rect(self.orientation, self.position, w, h)
        far = QRectF(right, 0, w - right, h) if self.orientation == VERTICAL else QRectF(0, bottom, w, h - bottom)
        if self.current() and far.width() > 0 and far.height() > 0:
            sx, sy = self.image.width() / max(1, w), self.image.height() / max(1, h)
            painter.drawImage(far, self.image, QRectF(far.x() * sx, far.y() * sy, far.width() * sx, far.height() * sy))
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        a, b = (QPointF(right, 0), QPointF(right, h)) if self.orientation == VERTICAL else (QPointF(0, bottom), QPointF(w, bottom))
        painter.setPen(QPen(QColor(0, 0, 0, 0x99), 4))
        painter.drawLine(a, b)
        painter.setPen(QPen(QColor("white"), 2))
        painter.drawLine(a, b)
        middle = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
        painter.setPen(QPen(QColor(0, 0, 0, 0x99), 1.5))
        painter.setBrush(QBrush(QColor("white")))
        painter.drawEllipse(middle, 12, 12)
        painter.setPen(QPen(QColor("#334155"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for d in (-1, 1):
            if self.orientation == VERTICAL:
                painter.drawPolyline([QPointF(middle.x() + d * 3, middle.y() - 4), QPointF(middle.x() + d * 7, middle.y()), QPointF(middle.x() + d * 3, middle.y() + 4)])
            else:
                painter.drawPolyline([QPointF(middle.x() - 4, middle.y() + d * 3), QPointF(middle.x(), middle.y() + d * 7), QPointF(middle.x() + 4, middle.y() + d * 3)])


class SwipeTool(QgsMapTool):
    """Drags the line (elsewhere pans), a double click turns it, keys move it; `done()` on Esc."""

    def __init__(self, canvas, swipe: "SwipeCompare"):
        super().__init__(canvas)
        self.swipe = swipe
        self._dragging = False
        self._panning = False

    def _along(self, event) -> float:
        p = event.pos()
        return p.x() if self.swipe.overlay.orientation == VERTICAL else p.y()

    def _span(self) -> float:
        size = self.canvas().size()
        return max(1, size.width() if self.swipe.overlay.orientation == VERTICAL else size.height())

    def near_line(self, event) -> bool:
        return abs(self._along(event) - self.swipe.overlay.line_at()) <= GRAB

    def canvasPressEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.near_line(event):
            self._dragging = True
        else:
            self._panning = True

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.swipe.set_position(self._along(event) / self._span())
        elif self._panning and event.buttons() & Qt.MouseButton.LeftButton:
            self.canvas().panAction(event)
        else:
            vertical = self.swipe.overlay.orientation == VERTICAL
            shape = (Qt.CursorShape.SplitHCursor if vertical else Qt.CursorShape.SplitVCursor) if self.near_line(event) else Qt.CursorShape.OpenHandCursor
            self.canvas().setCursor(shape)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if self._panning:
            self.canvas().panActionEnd(event.pos())
        self._dragging = self._panning = False

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.swipe.set_orientation(HORIZONTAL if self.swipe.overlay.orientation == VERTICAL else VERTICAL)
        self.swipe.say(self.swipe.tr("スワイプを{kind}に変更").format(
            kind=self.swipe.tr("縦線（左右で比較）") if self.swipe.overlay.orientation == VERTICAL else self.swipe.tr("横線（上下で比較）")))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = 0.1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.01
        vertical = self.swipe.overlay.orientation == VERTICAL
        back, forward = (Qt.Key.Key_Left, Qt.Key.Key_Right) if vertical else (Qt.Key.Key_Up, Qt.Key.Key_Down)
        key, position = event.key(), self.swipe.overlay.position
        if key == back:
            self.swipe.set_position(position - step)
        elif key == forward:
            self.swipe.set_position(position + step)
        elif key == Qt.Key.Key_Home:
            self.swipe.set_position(0)
        elif key == Qt.Key.Key_End:
            self.swipe.set_position(1)
        elif key == Qt.Key.Key_Escape:
            self.swipe.set_active(False)
        else:
            event.ignore()
            return
        event.accept()


class SwipeCompare(QObject):
    """The swipe: its toggle `action`, the layer it follows (the active one, or the group chosen), the overlay and the tool."""

    def __init__(self, iface, action, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.canvas: QgsMapCanvas = iface.mapCanvas()
        self.action = action
        action.setCheckable(True)
        action.toggled.connect(self.set_active)
        self.active = False
        self.overlay = SwipeOverlay(self.canvas)
        self.overlay.hide()
        self.tool = SwipeTool(self.canvas, self)
        self._previous_tool = None
        #: What is swiped: a layer, or a group of the layer tree.
        self.target = None
        self.job: Optional[QgsMapRendererParallelJob] = None
        self._again = False
        self.canvas.mapToolSet.connect(self._tool_set)
        self.canvas.renderStarting.connect(self.draw)
        self.iface.currentLayerChanged.connect(self._current_changed)
        view = iface.layerTreeView() if hasattr(iface, "layerTreeView") else None
        if view is not None:
            view.currentLayerChanged.connect(self._current_changed)

    # What is swiped.

    def chosen(self):
        """The layer tree's current group when one is chosen, else the active layer."""
        view = self.iface.layerTreeView() if hasattr(self.iface, "layerTreeView") else None
        group = view.currentGroupNode() if view is not None else None
        if isinstance(group, QgsLayerTreeGroup) and group.parent() is not None and view.currentNode() is group:
            return group
        return self.iface.activeLayer()

    def swiped_layers(self) -> list[QgsMapLayer]:
        if isinstance(self.target, QgsLayerTreeGroup):
            return [n.layer() for n in self.target.findLayers() if n.layer() is not None]
        return [self.target] if isinstance(self.target, QgsMapLayer) else []

    def target_name(self) -> str:
        if isinstance(self.target, QgsLayerTreeGroup):
            return self.target.name()
        return self.target.name() if isinstance(self.target, QgsMapLayer) else ""

    def set_target(self, target) -> None:
        """Swipes a layer or a group (None: nothing)."""
        self.target = target
        self.overlay.image = None
        if self.active:
            self.draw()
            self.overlay.changed()

    def _current_changed(self, *_) -> None:
        target = self.chosen()
        if target is not self.target:
            self.set_target(target)

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
            self.target = self.chosen()
            self.overlay.show()
            if self.canvas.mapTool() is not self.tool:
                self._previous_tool = self.canvas.mapTool()
                self.canvas.setMapTool(self.tool)
            self.draw()
            self.overlay.changed()
            side = self.tr("左側") if self.overlay.orientation == VERTICAL else self.tr("上側")
            self.say(self.tr("{name} を線の{side}のみに表示中。下のレイヤーと比較可能").format(name=self.target_name(), side=side)
                     if self.swiped_layers() else self.tr("レイヤーを選ぶと、線の片側のみに表示して比較可能"))
        else:
            self._cancel_job()
            self.overlay.hide()
            self.overlay.image = None
            if self.canvas.mapTool() is self.tool:
                try:
                    self.canvas.setMapTool(self._previous_tool) if self._previous_tool is not None else self.canvas.unsetMapTool(self.tool)
                except RuntimeError:  # the tool went with its plugin
                    self.canvas.unsetMapTool(self.tool)

    def _tool_set(self, new, old=None) -> None:
        if self.active and new is not self.tool:
            self.set_active(False)

    def set_position(self, position: float) -> None:
        """Moves the line, 0-1 from the left (or the top)."""
        self.overlay.position = min(1.0, max(0.0, float(position)))
        self.overlay.changed()

    def set_orientation(self, orientation: str) -> None:
        """`vertical` (the layer on the left) or `horizontal` (the layer on top)."""
        self.overlay.orientation = HORIZONTAL if orientation == HORIZONTAL else VERTICAL
        self.overlay.changed()

    # The map without the swiped layers.

    def draw(self) -> None:
        """Draws the map again without the swiped layers, for the far side of the line."""
        if not self.active:
            return
        if self.job is not None:
            self._again = True
            self.job.cancelWithoutBlocking()
            return
        swiped = {layer.id() for layer in self.swiped_layers()}
        settings = QgsMapSettings(self.canvas.mapSettings())
        settings.setLayers([layer for layer in self.canvas.layers() if layer.id() not in swiped])
        drawn_for = (settings.visibleExtent().toString(), settings.outputSize(), settings.rotation())
        self.job = QgsMapRendererParallelJob(settings)

        def finished() -> None:
            job, self.job = self.job, None
            if job is None:
                return
            if self._again:
                self._again = False
                self.draw()
                return
            if self.active:
                self.overlay.image = job.renderedImage()
                self.overlay.drawn_for = drawn_for
                self.overlay.changed()

        self.job.finished.connect(finished)
        self.job.start()

    def _cancel_job(self) -> None:
        if self.job is not None:
            job, self.job = self.job, None
            self._again = False
            job.cancel()

    def ready(self) -> bool:
        """Whether the far side shows what the canvas shows now (for scripts and tests)."""
        return self.job is None and self.overlay.current()

    def say(self, text: str) -> None:
        self.iface.messageBar().pushInfo(self.tr("スワイプ比較"), text)

    def close(self) -> None:
        self.set_active(False)
        for signal, slot in ((self.canvas.mapToolSet, self._tool_set), (self.canvas.renderStarting, self.draw),
                             (self.iface.currentLayerChanged, self._current_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        view = self.iface.layerTreeView() if hasattr(self.iface, "layerTreeView") else None
        if view is not None:
            try:
                view.currentLayerChanged.disconnect(self._current_changed)
            except (TypeError, RuntimeError):
                pass
        self.canvas.scene().removeItem(self.overlay)
