"""
The 「地点ごとの撮像予定」 timeline (core/request/place_plans.layout painted): a
line per place, time to the right. A click on a mark chooses that plan (the
dock draws it on the map), a click on a place's name goes to the place.
"""
from __future__ import annotations

from typing import Optional, Sequence

from qgis.PyQt.QtCore import QEvent, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from qgis.PyQt.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..core.request.place_plans import HEAD, LABEL, REQUEST, ROW, Lane, Layout, Mark, lane_at, layout, mark_tip, marks_at
from ..core.request.store import AWAITING, DECLINED, HELD_BACK, PLANNED, UNREQUESTED
from .docks import plain_tip

#: Each state's colour (as the 「撮像予定」 dock's), and the requests' days still to be planned.
COLORS = {PLANNED: "#16a34a", UNREQUESTED: "#7c3aed", AWAITING: "#2563eb", DECLINED: "#dc2626", HELD_BACK: "#9ca3af",
          REQUEST: "#38bdf8"}
#: The most marks one tooltip tells of.
TIP_MARKS = 4


class PlaceTimeline(QWidget):
    #: A plan (or a request's day) was clicked: the mark and its line's index.
    picked = pyqtSignal(object, int)
    #: A place's name was clicked: its line's index.
    place_clicked = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.lanes: list[Lane] = []
        self._span = (0.0, 1.0)
        self._now: Optional[float] = None
        self.chosen: Optional[Mark] = None
        self.empty_text = ""
        self._layout: Optional[Layout] = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(LABEL + 240)

    def set(self, lanes: Sequence[Lane], start: float, end: float, now: Optional[float], empty_text: str = "") -> None:
        self.lanes, self._span, self._now, self.empty_text = list(lanes), (start, end), now, empty_text
        if self.chosen is not None and not any(self.chosen is m for lane in self.lanes for m in lane.marks):
            self.chosen = None
        self._relayout()

    def choose(self, mark: Optional[Mark]) -> None:
        self.chosen = mark
        self.update()

    @property
    def lay(self) -> Optional[Layout]:
        return self._layout

    def _relayout(self) -> None:
        start, end = self._span
        self._layout = layout(self.lanes, start, end, self.width(), self._now) if self.lanes and end > start else None
        self.setFixedHeight(int(self._layout.height) if self._layout else 60)
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        self._relayout()

    # --- Painting -------------------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        palette = self.palette()
        fg = palette.color(QPalette.ColorRole.WindowText)
        muted = QColor(fg)
        muted.setAlpha(150)
        lay = self._layout
        if lay is None:
            p.setPen(muted)
            p.drawText(QRectF(8, 0, self.width() - 16, 60), int(Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap), self.empty_text)
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        bold = QFont(self.font())
        bold.setBold(True)
        # Lines' bands, weekends a little darker.
        band = QColor(fg)
        band.setAlpha(12)
        for i, (y, _name, _next, _tip) in enumerate(lay.labels):
            if i % 2:
                p.fillRect(QRectF(0, y, lay.width, ROW), band)
        weekend = QColor(fg)
        weekend.setAlpha(10)
        for k, (x, _text, holiday) in enumerate(lay.days):
            if holiday:
                x1 = lay.days[k + 1][0] if k + 1 < len(lay.days) else lay.width - 8
                p.fillRect(QRectF(x, HEAD - 4, x1 - x, lay.height - HEAD + 4), weekend)
        p.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
        for x, _text, _w in lay.days:
            p.drawLine(int(x), HEAD - 4, int(x), int(lay.height))
        p.drawLine(LABEL, HEAD - 4, LABEL, int(lay.height))
        p.setFont(small)
        day_metrics = QFontMetrics(small)
        for x, text, holiday in lay.days:
            # Not cut at the right edge.
            if text and x + 3 + day_metrics.horizontalAdvance(text) <= lay.width:
                p.setPen(QColor("#dc2626") if holiday else muted)
                p.drawText(int(x + 3), HEAD - 7, text)
        # The places' names and what is next for each.
        name_metrics, small_metrics = QFontMetrics(bold), QFontMetrics(small)
        for i, (y, name, next_text, _tip) in enumerate(lay.labels):
            p.setFont(bold)
            p.setPen(fg)
            p.drawText(6, int(y + 15), name_metrics.elidedText(name, Qt.TextElideMode.ElideRight, LABEL - 12))
            p.setFont(small)
            nxt = self.lanes[i].next
            p.setPen(QColor(COLORS.get(nxt.state, fg.name())) if nxt is not None else muted)
            p.drawText(6, int(y + ROW - 6), small_metrics.elidedText(next_text, Qt.TextElideMode.ElideRight, LABEL - 12))
        # The marks: requests' days at the bottom of each line, plans over them (those without a time as their day).
        for x0, x1, y0, y1, m in lay.marks:
            colour = QColor(COLORS.get(m.state, "#64748b"))
            rect = QRectF(x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))
            chosen = m is self.chosen
            if m.state == REQUEST:
                fill = QColor(colour)
                fill.setAlpha(130)
                p.fillRect(rect.adjusted(0.5, 0, -0.5, 0), fill)
            elif m.timed:
                p.setPen(QPen(QColor(fg) if chosen else colour.darker(130), 2 if chosen else 1))
                if m.by_shot:
                    fill = QColor(colour)
                    fill.setAlpha(60)
                    p.setBrush(fill)
                else:
                    p.setBrush(colour)
                p.drawRoundedRect(rect, 1.5, 1.5)
            else:
                fill = QColor(colour)
                fill.setAlpha(45 if m.by_shot else 80)
                pen = QPen(QColor(fg) if chosen else colour, 2 if chosen else 1)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(fill)
                p.drawRect(rect)
            if chosen and m.state == REQUEST:
                p.setPen(QPen(QColor(fg), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect)
        if lay.now is not None:
            p.setPen(QPen(QColor("#ef4444"), 1.5))
            p.drawLine(int(lay.now), HEAD - 6, int(lay.now), int(lay.height))

    # --- The mouse ------------------------------------------------------------------------------

    def tip_at(self, x: float, y: float) -> str:
        """What the tooltip says at a pixel (plain text): the marks there, else the place."""
        lay = self._layout
        if lay is None:
            return ""
        i = lane_at(lay, y)
        if i < 0:
            return ""
        if x < LABEL:
            return lay.labels[i][3]
        marks = marks_at(lay, x, y)
        place = self.lanes[i].place
        tips = [mark_tip(m, place) for m in marks[:TIP_MARKS]]
        if len(marks) > TIP_MARKS:
            tips.append(f"ほか {len(marks) - TIP_MARKS} 件")
        return "\n\n".join(tips)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            text = self.tip_at(event.pos().x(), event.pos().y())
            if text:
                # Names and notes from the ledger and the systems' files: shown as written.
                QToolTip.showText(event.globalPos(), plain_tip(text) if any(c in text for c in "<>&") else text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def click(self, x: float, y: float) -> None:
        lay = self._layout
        if lay is None:
            return
        i = lane_at(lay, y)
        if i < 0:
            return
        if x < LABEL:
            self.place_clicked.emit(i)
            return
        marks = marks_at(lay, x, y)
        if marks:
            # Clicked again on the chosen one: the next one under it (several plans at one moment).
            k = next((n for n, m in enumerate(marks) if m is self.chosen), -1)
            mark = marks[(k + 1) % len(marks)]
            self.choose(mark)
            self.picked.emit(mark, i)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.click(event.pos().x(), event.pos().y())
        super().mouseReleaseEvent(event)
