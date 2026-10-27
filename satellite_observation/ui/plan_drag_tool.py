"""
Moving the chosen pass's scenes on the map (the viewer's drag on the
overlay): pressed on a scene, the drag moves them along and across the track
within what the satellite can reach (core.plan.scene.shift_scene).
"""
from __future__ import annotations

from typing import Callable

from qgis.core import QgsPointXY
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt

from ..core.plan.targets import in_polygon
from ..qgis_io.geometry import to_wgs84


class PlanDragTool(QgsMapTool):
    """`scenes()` gives the chosen pass's scenes (WGS84 rings); `begin(lon, lat)` → bool, `move(lon, lat)`, `end()`."""

    def __init__(self, canvas, scenes: Callable[[], list], begin: Callable[[float, float], bool],
                 move: Callable[[float, float], None], end: Callable[[], None]):
        super().__init__(canvas)
        self._scenes, self._begin, self._move, self._end = scenes, begin, move, end
        self._dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _lon_lat(self, event) -> QgsPointXY:
        return to_wgs84(self.canvas().mapSettings().destinationCrs()).transform(event.mapPoint())

    def on_scene(self, lon: float, lat: float) -> bool:
        return any(in_polygon([ring], lon, lat) for ring in self._scenes())

    def canvasPressEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        p = self._lon_lat(event)
        if self.on_scene(p.x(), p.y()) and self._begin(p.x(), p.y()):
            self._dragging = True
            self.canvas().setCursor(Qt.CursorShape.ClosedHandCursor)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        p = self._lon_lat(event)
        if self._dragging:
            self._move(p.x(), p.y())
        else:
            self.canvas().setCursor(Qt.CursorShape.SizeAllCursor if self.on_scene(p.x(), p.y()) else Qt.CursorShape.ArrowCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._dragging = False
            self._end()
            self.canvas().setCursor(Qt.CursorShape.OpenHandCursor)
