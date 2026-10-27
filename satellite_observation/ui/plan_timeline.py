"""The imaging plan's timeline (core/plan/timeline.py painted); a click on a bar chooses that shot."""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.PyQt.QtCore import QEvent, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPalette, QPen
from qgis.PyQt.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..core.plan.access import Opportunity
from ..core.plan.satellites import SatelliteSpec
from ..core.plan.timeline import HEAD, ROW, Layout, bar_at, layout


class PlanTimeline(QWidget):
    #: A bar was clicked.
    picked = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._ops: Sequence[Opportunity] = []
        self._sats: Sequence[SatelliteSpec] = []
        self._span = (0.0, 0.0)
        self._chosen: Optional[Opportunity] = None
        self._title: Callable[[Opportunity], str] = lambda op: ""
        self._layout: Optional[Layout] = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, ops, sats, start: float, end: float, chosen: Optional[Opportunity], title: Callable[[Opportunity], str]) -> None:
        self._ops, self._sats, self._span, self._chosen, self._title = list(ops), sats, (start, end), chosen, title
        self._relayout()

    def choose(self, op: Optional[Opportunity]) -> None:
        self._chosen = op
        self._relayout()

    def rows(self) -> int:
        return 0 if self._layout is None else len(self._layout.labels)

    def _relayout(self) -> None:
        self._layout = layout(self._ops, self._sats, self._span[0], self._span[1], self.width(), self._chosen)
        self.setFixedHeight(int(self._layout.height) if self._layout else 0)
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        self._relayout()

    def paintEvent(self, event) -> None:  # noqa: N802
        lay = self._layout
        if lay is None:
            return
        palette = self.palette()
        fg = palette.color(QPalette.ColorRole.WindowText)
        muted = QColor(fg)
        muted.setAlpha(140)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPointSizeF(8)
        p.setFont(font)
        band = QColor(fg)
        band.setAlpha(10)
        for i, (y, name, _) in enumerate(lay.labels):
            if i % 2:
                p.fillRect(QRectF(0, y, lay.width, ROW), band)
        p.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
        for x, _ in lay.days:
            p.drawLine(int(x), HEAD - 4, int(x), int(lay.height))
        p.setPen(muted)
        for x, text in lay.days:
            if text:
                p.drawText(int(x + 2), HEAD - 6, text)
        p.setPen(fg)
        for y, name, _ in lay.labels:
            p.drawText(4, int(y + ROW - 6), name)
        busy = QColor(muted)
        busy.setAlpha(64)
        for x, y, w in lay.busy:
            p.fillRect(QRectF(x, y, w, ROW - 6), busy)
        for bar in lay.bars:
            rect = QRectF(bar.x - 1.5, bar.y + 3, 3, ROW - 6)
            p.fillRect(rect, QColor(bar.colour))
            if bar.chosen:
                p.setPen(QPen(fg, 2))
                p.drawRect(rect)
        p.end()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip and self._layout is not None:
            pos = event.pos()
            op = bar_at(self._layout, pos.x(), pos.y())
            text = self._title(op) if op else next((tip for y, _, tip in self._layout.labels if y <= pos.y() < y + ROW and pos.x() < 120), "")
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._layout is None or event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        op = bar_at(self._layout, pos.x(), pos.y())
        if op is not None:
            self.picked.emit(op)

    def cursor_over(self, x: float, y: float) -> bool:
        return self._layout is not None and bar_at(self._layout, x, y) is not None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position() if hasattr(event, "position") else event.pos()
        self.setCursor(Qt.CursorShape.PointingHandCursor if self.cursor_over(pos.x(), pos.y()) else Qt.CursorShape.ArrowCursor)
