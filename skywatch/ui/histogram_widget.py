"""
The 画像補正 dock's histogram, as the viewer's histogram panel: bars of the
0..255 codes of the area in view per R, G, B (added, so where they overlap
they turn white) or of the gray level, and a table of each channel's
smallest, mean, median and largest code. Pointing at it reads the counts of
one value.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.PyQt.QtWidgets import QHeaderView, QSizePolicy, QTableWidget, QTableWidgetItem, QWidget

from ..core.enhance.histogram import bar_heights, channel_stats

COLORS = [QColor("#e5484d"), QColor("#30a46c"), QColor("#3e7bfa")]
GRAY = QColor("#9aa0a6")
BACKGROUND = QColor("#1e1f22")
NAMES = ["R", "G", "B"]


def channel_names(count: int) -> list:
    return ["輝度"] if count == 1 else NAMES[:count]


class HistogramPlot(QWidget):
    """256 bars per channel on one scale; the vertical scale is linear or logarithmic."""

    def __init__(self, read: Optional[Callable[[str], None]] = None, parent=None):
        super().__init__(parent)
        self.channels: list = []
        self.log = False
        self._read = read
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def set_channels(self, channels: list) -> None:
        self.channels = channels
        self.update()

    def set_log(self, log: bool) -> None:
        self.log = log
        self.update()

    def heights(self) -> list:
        return bar_heights(self.channels, self.log)

    def value_at(self, x: float) -> int:
        return min(255, max(0, int(x / max(1, self.width()) * 256)))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if self._read is None or not self.channels or not sum(int(c.sum()) for c in self.channels):
            return
        v = self.value_at(event.pos().x())
        names = channel_names(len(self.channels))
        self._read(f"値 {v}: " + "・".join(f"{names[c]} {int(bins[v]):,}" for c, bins in enumerate(self.channels)))

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, BACKGROUND)
        heights = self.heights()
        gray = len(heights) == 1
        # Added: where R, G and B overlap the bars turn white, as in photo editors.
        if not gray:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        step = w / 256
        for c, bars in enumerate(heights):
            color = QColor(GRAY if gray else COLORS[c])
            if not gray:
                color.setAlphaF(0.85)
            for v, y in enumerate(bars):
                if y > 0:
                    p.fillRect(QRectF(v * step, h - y * h, step, y * h), color)
        p.end()


class StatsTable(QTableWidget):
    """最小・平均・中央・最大 per channel."""

    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels([self.tr("最小"), self.tr("平均"), self.tr("中央"), self.tr("最大")])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_channels(self, channels: list) -> None:
        self.setRowCount(len(channels))
        self.setVerticalHeaderLabels(channel_names(len(channels)))
        for row, text in enumerate(summary(channels)):
            for col, value in enumerate(text):
                item = QTableWidgetItem(value)
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                self.setItem(row, col, item)
        rows = sum(self.rowHeight(r) for r in range(self.rowCount()))
        self.setFixedHeight(self.horizontalHeader().sizeHint().height() + rows + 2 * self.frameWidth())


def summary(channels: list) -> list:
    """The table's rows as text: 最小・平均・中央・最大 (- for a channel that counted nothing)."""
    out = []
    for bins in channels:
        s = channel_stats(bins)
        out.append([str(s["min"]), f"{s['mean']:.1f}", str(s["median"]), str(s["max"])] if s else ["-"] * 4)
    return out
