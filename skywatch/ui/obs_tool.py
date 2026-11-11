"""
Recording on the map (the viewer's clicks on its overlay): a click gives a
place (or the record or carried-over thing under it), a press on one of this
image's records drags it to a new place, and the pointer's moves feed the
line drawn while measuring by two points. Esc on the map steps back as Esc in
the dock does (the last point, the area corner, then the form), and Enter on
the map saves the open form (or the area) as Enter in the dock does.

While 「連続」 is on (`stamping()`, ui/obs_many.py), a double click is told
from a click (tsudo, 2026-11-09): the click waits the system's double-click
time and is dropped when the second one comes; a press elsewhere than on a
record and dragged turns what is put down (`turned`). Backspace (or Ctrl+Z)
takes the last put down back, ＋ / − change how many a row has.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from qgis.core import QgsPointXY
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import QApplication

from ..qgis_io.geometry import to_wgs84
from .obs_overlay import Mark, ObservationOverlay


class ObservationTool(QgsMapTool):
    """
    `clicked(lon, lat, mark)` on a click (`mark`: the record or carried-over
    thing hit, else None), `hovered(lon, lat)` on moves, `dragged(mark, lon,
    lat, done)` while a record is dragged, `escaped()` on Esc and `entered()` on
    Enter while the map has the focus (`entered` says whether it took the key);
    `can_drag()` says whether records may move now.
    """

    def __init__(self, canvas, overlay: ObservationOverlay, clicked: Callable[[float, float, Optional[Mark]], None],
                 hovered: Callable[[float, float], None], dragged: Callable[[Mark, float, float, bool], None],
                 can_drag: Callable[[], bool], escaped: Optional[Callable[[], None]] = None,
                 entered: Optional[Callable[[], bool]] = None,
                 double_clicked: Optional[Callable[[float, float, Optional[Mark]], None]] = None,
                 turned: Optional[Callable[[float, float, float, float, bool], None]] = None,
                 stamping: Optional[Callable[[], bool]] = None, undo: Optional[Callable[[], bool]] = None,
                 stepped: Optional[Callable[[int], bool]] = None):
        super().__init__(canvas)
        self.overlay = overlay
        self._clicked, self._hovered, self._dragged, self._can_drag = clicked, hovered, dragged, can_drag
        self._escaped = escaped
        self._entered = entered
        self._double_clicked, self._turned, self._stamping = double_clicked, turned, stamping
        self._undo, self._stepped = undo, stepped
        self._press: Optional[tuple] = None
        self._drag: Optional[Mark] = None
        self._turning = False
        #: A click waiting to be told from a double click: (lon, lat, mark).
        self._waiting: Optional[tuple] = None
        self._click_timer = QTimer()
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._click_waited)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _lon_lat(self, event) -> QgsPointXY:
        return to_wgs84(self.canvas().mapSettings().destinationCrs()).transform(event.mapPoint())

    def _hit(self, x: float, y: float) -> Optional[Mark]:
        # A record or carried-over thing first, then what is being put down (its places; not the corners'
        # marks over the first and last of a row, nor the one following the pointer while 連続).
        return self.overlay.hit(x, y) or self.overlay.hit(x, y, ("slot",), keyed=True)

    def _stamps(self) -> bool:
        return self._stamping is not None and self._stamping()

    def canvasPressEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = event.pos().x(), event.pos().y()
        # Checked areas lie under the records: only marks are hit.
        self._press = (x, y, self._hit(x, y), self._lon_lat(event))
        self._drag = None
        self._turning = False

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        p = self._lon_lat(event)
        press = self._press
        if press is not None:
            x, y, mark, start = press
            moved = math.hypot(event.pos().x() - x, event.pos().y() - y) > 3
            if self._drag is None and not self._turning and moved:
                if mark is not None and mark.role == "obs" and mark.key and self._can_drag():
                    self._drag = mark
                    self.canvas().setCursor(Qt.CursorShape.ClosedHandCursor)
                elif self._turned is not None and self._stamps():
                    # 連続: what is put down at the press turns to the pointer.
                    self._turning = True
                    self._cancel_click()
            if self._drag is not None:
                self._dragged(self._drag, p.x(), p.y(), False)
                return
            if self._turning:
                self._turned(start.x(), start.y(), p.x(), p.y(), False)
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
        if self._turning:
            self._turning = False
            self._turned(press[3].x(), press[3].y(), p.x(), p.y(), True)
            return
        if self._double_clicked is not None and self._stamps():
            # A double click may follow: the click waits for it.
            self._waiting = (p.x(), p.y(), press[2])
            self._click_timer.start(max(100, QApplication.doubleClickInterval()))
            return
        self._clicked(p.x(), p.y(), press[2])

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        # Qt sends the second press of a double click as this alone (its release follows, with no press).
        if event.button() != Qt.MouseButton.LeftButton or self._double_clicked is None or not self._stamps():
            return
        self._cancel_click()
        self._press = None
        p = self._lon_lat(event)
        self._double_clicked(p.x(), p.y(), self._hit(event.pos().x(), event.pos().y()))

    def _cancel_click(self) -> None:
        self._click_timer.stop()
        self._waiting = None

    def _click_waited(self) -> None:
        waiting, self._waiting = self._waiting, None
        if waiting is not None:
            self._clicked(*waiting)

    def deactivate(self) -> None:
        self._cancel_click()
        self._turning = False
        super().deactivate()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        # The dock's Esc key works only while the dock has the focus; on the map it comes here.
        if key == Qt.Key.Key_Escape and self._escaped is not None and not event.isAutoRepeat():
            # A record being dragged stays where it was (the dock forgets the drag), a turn is dropped.
            self._drag = self._press = None
            self._turning = False
            self._cancel_click()
            self.canvas().setCursor(Qt.CursorShape.CrossCursor)
            self._escaped()
            event.accept()
        elif (self._undo is not None and (key == Qt.Key.Key_Backspace or (key == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier))
              and self._press is None and self._undo()):
            event.accept()
        elif (self._stepped is not None and key in (Qt.Key.Key_Plus, Qt.Key.Key_Minus, Qt.Key.Key_Equal)
              and self._stepped(-1 if key == Qt.Key.Key_Minus else 1)):
            event.accept()
        elif (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._entered is not None
              and not event.isAutoRepeat() and self._press is None and self._entered()):
            # The form (or the area) saved; with nothing open the key goes on to QGIS.
            event.accept()
        else:
            event.ignore()
