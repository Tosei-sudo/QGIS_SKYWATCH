"""
トーンカーブ in the 画像補正 dock: the `curve` step drawn and edited as in a
photo editor. The curve runs through its points by the same monotone cubic
the correction uses (core.enhance.ops.curve_function); the points are
sRGB-encoded inputs and outputs from 0 to 1.

- Drag a point to move it (the ends move only up and down); click off the
  points to add one on the curve under the pointer; double-click a point, or
  drag it out of the box, to remove it.
- One curve for all channels (RGB), and one per red, green and blue on top
  of it (colour images only).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..core.enhance.ops import curve_function, is_identity_curve, normalize_curve

CHANNELS = [("points", "RGB（全体）"), ("red", "赤"), ("green", "緑"), ("blue", "青")]
COLORS = {"points": QColor(40, 40, 40), "red": QColor("#e5484d"), "green": QColor("#30a46c"), "blue": QColor("#3e7bfa")}
#: How near (px) the pointer must be to take a point, and how far out of the box a dragged point is removed.
GRAB = 8
DROP = 24
#: The least gap between neighbouring inputs.
GAP = 0.01


def identity() -> list:
    return [[0.0, 0.0], [1.0, 1.0]]


class CurvePlot(QWidget):
    """The curves on a square, with the histogram behind; edits the current channel's points."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.curves = {key: identity() for key, _ in CHANNELS}
        self.channel = "points"
        self.histogram: Optional[list] = None
        self._drag: Optional[int] = None
        self._removing = False
        self.setMinimumSize(160, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def heightForWidth(self, w: int) -> int:  # noqa: N802 (Qt's name)
        return w

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt's name)
        return True

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        if self.height() != self.width():
            self.setFixedHeight(max(160, self.width()))

    # ---- geometry ----

    def box(self) -> QRectF:
        return QRectF(6, 6, self.width() - 12, self.height() - 12)

    def to_screen(self, x: float, y: float) -> QPointF:
        b = self.box()
        return QPointF(b.left() + x * b.width(), b.bottom() - y * b.height())

    def to_curve(self, p: QPointF) -> tuple:
        b = self.box()
        return (p.x() - b.left()) / b.width(), (b.bottom() - p.y()) / b.height()

    def points(self) -> list:
        return self.curves[self.channel]

    def _near(self, pos: QPointF) -> Optional[int]:
        best, pick = GRAB + 1.0, None
        for i, (x, y) in enumerate(self.points()):
            q = self.to_screen(x, y)
            d = ((q.x() - pos.x()) ** 2 + (q.y() - pos.y()) ** 2) ** 0.5
            if d < best:
                best, pick = d, i
        return pick

    # ---- mouse ----

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = QPointF(event.pos())
        i = self._near(pos)
        if i is None:
            x, _y = self.to_curve(pos)
            if not 0 < x < 1:
                return
            pts = self.points()
            if any(abs(px - x) < GAP for px, _ in pts):
                return
            y = float(np.clip(curve_function(pts)(np.array([x]))[0], 0, 1))
            pts.append([x, y])
            pts.sort(key=lambda p: p[0])
            i = next(k for k, p in enumerate(pts) if p[0] == x)
            self.changed.emit()
        self._drag = i
        self._removing = False
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if self._drag is None:
            self.setCursor(Qt.CursorShape.PointingHandCursor if self._near(QPointF(event.pos())) is not None
                           else Qt.CursorShape.CrossCursor)
            return
        pts = self.points()
        i = self._drag
        x, y = self.to_curve(QPointF(event.pos()))
        b = self.box()
        outside = not b.adjusted(-DROP, -DROP, DROP, DROP).contains(QPointF(event.pos()))
        self._removing = outside and 0 < i < len(pts) - 1
        if i == 0 or i == len(pts) - 1:
            x = pts[i][0]
        else:
            x = min(max(x, pts[i - 1][0] + GAP), pts[i + 1][0] - GAP)
        pts[i] = [float(x), float(min(1.0, max(0.0, y)))]
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        if self._drag is not None and self._removing:
            del self.points()[self._drag]
            self.changed.emit()
        self._drag = None
        self._removing = False
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        i = self._near(QPointF(event.pos()))
        pts = self.points()
        if i is not None and 0 < i < len(pts) - 1:
            del pts[i]
            self._drag = None
            self.changed.emit()
            self.update()

    # ---- drawing ----

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        b = self.box()
        p.fillRect(self.rect(), self.palette().window())
        p.fillRect(b, self.palette().base())
        if self.histogram:
            total = np.sum(self.histogram, axis=0) if len(self.histogram) > 1 else np.asarray(self.histogram[0])
            if total.max() > 0:
                h = np.log1p(total) / np.log1p(total.max())
                path = QPainterPath(self.to_screen(0, 0))
                for v in range(256):
                    path.lineTo(self.to_screen(v / 255, float(h[v]) * 0.9))
                path.lineTo(self.to_screen(1, 0))
                path.closeSubpath()
                p.fillPath(path, QColor(150, 150, 150, 70))
        grid = QPen(self.palette().mid().color(), 1, Qt.PenStyle.DotLine)
        p.setPen(grid)
        for k in (1, 2, 3):
            p.drawLine(self.to_screen(k / 4, 0), self.to_screen(k / 4, 1))
            p.drawLine(self.to_screen(0, k / 4), self.to_screen(1, k / 4))
        p.drawLine(self.to_screen(0, 0), self.to_screen(1, 1))
        p.setPen(QPen(self.palette().mid().color(), 1))
        p.drawRect(b)
        xs = np.linspace(0, 1, 129)
        order = [k for k, _ in CHANNELS if k != self.channel] + [self.channel]
        for key in order:
            pts = self.curves[key]
            if key != self.channel and is_identity_curve(pts):
                continue
            ys = np.clip(curve_function(pts)(xs), 0, 1)
            path = QPainterPath(self.to_screen(0, float(ys[0])))
            for x, y in zip(xs[1:], ys[1:]):
                path.lineTo(self.to_screen(float(x), float(y)))
            color = QColor(COLORS[key] if key != "points" else self.palette().text().color())
            if key != self.channel:
                color.setAlpha(110)
            p.setPen(QPen(color, 2 if key == self.channel else 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        color = QColor(COLORS[self.channel] if self.channel != "points" else self.palette().text().color())
        for i, (x, y) in enumerate(self.points()):
            q = self.to_screen(x, y)
            removing = i == self._drag and self._removing
            p.setPen(QPen(color, 1.5))
            p.setBrush(self.palette().base() if not removing else QColor(0, 0, 0, 0))
            p.drawEllipse(q, 4.5, 4.5)
        p.end()


class CurveEditor(QWidget):
    """The plot with the channel choice and a reset; `changed` on every edit."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.channel = QComboBox()
        for key, label in CHANNELS:
            self.channel.addItem(self.tr(label), key)
        self.channel.currentIndexChanged.connect(self._channel_changed)
        row.addWidget(self.channel, 1)
        reset = QPushButton(self.tr("直線に戻す"))
        reset.clicked.connect(self.reset_channel)
        row.addWidget(reset)
        v.addLayout(row)
        self.plot = CurvePlot()
        self.plot.changed.connect(self.changed)
        v.addWidget(self.plot)
        hint = QLabel(self.tr("ドラッグで移動、クリックで点の追加、ダブルクリックか枠の外へのドラッグで削除。"))
        hint.setWordWrap(True)
        v.addWidget(hint)

    def _channel_changed(self, *_args) -> None:
        self.plot.channel = self.channel.currentData()
        self.plot.update()

    def set_gray(self, gray: bool) -> None:
        """One-band images have the one curve."""
        model = self.channel.model()
        for i in range(1, self.channel.count()):
            item = model.item(i)
            item.setEnabled(not gray)
        if gray and self.channel.currentIndex() != 0:
            self.channel.setCurrentIndex(0)

    def reset_channel(self) -> None:
        self.plot.curves[self.plot.channel] = identity()
        self.plot.update()
        self.changed.emit()

    def set_step(self, step: Optional[dict]) -> None:
        """Shows a `curve` step (None: straight lines)."""
        self.plot.curves = {key: [list(map(float, pt)) for pt in normalize_curve((step or {}).get(key))] for key, _ in CHANNELS}
        self.plot._drag = None
        self.plot.update()

    def step(self) -> Optional[dict]:
        """The `curve` step, or None when every curve is straight."""
        curves = {key: normalize_curve(self.plot.curves[key]) for key, _ in CHANNELS}
        if all(is_identity_curve(c) for c in curves.values()):
            return None
        return {"op": "curve", **curves}

    def set_histogram(self, channels: Optional[list]) -> None:
        self.plot.histogram = channels
        self.plot.update()
