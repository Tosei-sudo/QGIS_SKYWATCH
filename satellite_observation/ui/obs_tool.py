"""
Recording on the map (the viewer's clicks on its overlay): a click gives a
place (or the record or carried-over thing under it), a press on one of this
image's records drags it to a new place, and the pointer's moves feed the
line drawn while measuring by two points.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from qgis.core import QgsPointXY
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt

from ..qgis_io.geometry import to_wgs84
from .obs_overlay import Mark, ObservationOverlay


class ObservationTool(QgsMapTool):
    """
    `clicked(lon, lat, mark)` on a click (`mark`: the record or carried-over
    thing hit, else None), `hovered(lon, lat)` on moves, `dragged(mark, lon,
    lat, done)` while a record is dragged; `can_drag()` says whether records may move now.
    """

    def __init__(self, canvas, overlay: ObservationOverlay, clicked: Callable[[float, float, Optional[Mark]], None],
                 hovered: Callable[[float, float], None], dragged: Callable[[Mark, float, float, bool], None],
                 can_drag: Callable[[], bool]):
        super().__init__(canvas)
        self.overlay = overlay
        self._clicked, self._hovered, self._dragged, self._can_drag = clicked, hovered, dragged, can_drag
        self._press: Optional[tuple] = None
        self._drag: Optional[Mark] = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _lon_lat(self, event) -> QgsPointXY:
        return to_wgs84(self.canvas().mapSettings().destinationCrs()).transform(event.mapPoint())

    def canvasPressEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = event.pos().x(), event.pos().y()
        # Checked areas lie under the records: only marks are hit.
        self._press = (x, y, self.overlay.hit(x, y))
        self._drag = None

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        p = self._lon_lat(event)
        press = self._press
        if press is not None:
            x, y, mark = press
            moved = math.hypot(event.pos().x() - x, event.pos().y() - y) > 3
            if self._drag is None and moved and mark is not None and mark.role == "obs" and self._can_drag():
                self._drag = mark
                self.canvas().setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._drag is not None:
                self._dragged(self._drag, p.x(), p.y(), False)
                return
        else:
            over = self.overlay.hit(event.pos().x(), event.pos().y())
            self.canvas().setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.CrossCursor)
        self._hovered(p.x(), p.y())

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        press, self._press = self._press, None
        p = self._lon_lat(event)
        if self._drag is not None:
            mark, self._drag = self._drag, None
            self.canvas().setCursor(Qt.CursorShape.CrossCursor)
            self._dragged(mark, p.x(), p.y(), True)
            return
        self._clicked(p.x(), p.y(), press[2])
