"""
Windows and scroll areas that never draw their rows over one another
(tsudo, 2026-11-15: 「表示が重なる不具合」, found by
scripts/help_shots/check_layout.py in a real QGIS).

* `fit_text(window)`: Qt gives a window no minimum size at all once a
  word-wrapped label is in it (its height depends on its width, which a
  window manager can't keep), so the window opens, or is dragged, lower or
  narrower than its rows and draws them over one another. The window here
  keeps the size its rows need at its width (at most the screen's height).
* `VerticalScroll`: a scroll area that scrolls up and down only, as wide as
  what is in it (a QScrollArea without a horizontal scroll bar cuts off the
  right of what is wider than it instead).
"""
from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, QObject, QSize
from qgis.PyQt.QtWidgets import QScrollArea, QStyle, QWidget

#: The most of the screen's height a window is made tall for its rows (the rest is the user's to drag).
SCREEN_SHARE = 0.95


def needed_size(window: QWidget) -> QSize | None:
    """The smallest size of the window at its width that its layout's rows need (None: Qt keeps it already)."""
    layout = window.layout()
    if layout is None or not layout.hasHeightForWidth():
        return None
    minimum = layout.totalMinimumSize()
    # The least height at this width: wrapped text whole, lists and tables at their smallest (not the preferred height).
    least = layout.minimumHeightForWidth(max(window.width(), minimum.width()))
    if least < 0:
        return None
    margins = window.contentsMargins()
    height = least + margins.top() + margins.bottom()
    screen = window.screen() if hasattr(window, "screen") else None
    if screen is not None:
        height = min(height, int(screen.availableGeometry().height() * SCREEN_SHARE))
    return QSize(minimum.width(), max(minimum.height(), height))


class _TextFit(QObject):
    def __init__(self, window: QWidget):
        super().__init__(window)
        self._busy = False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt's name)
        if not self._busy and event.type() in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._busy = True
            try:
                need = needed_size(obj)
                if need is not None and obj.minimumSize() != need:
                    obj.setMinimumSize(need)
            finally:
                self._busy = False
        return False


def fit_text(window: QWidget) -> QWidget:
    """The window (a dialog) never lower or narrower than its rows need; call once, before or after its layout is made."""
    window.installEventFilter(_TextFit(window))
    return window


class VerticalScroll(QScrollArea):
    """A scroll area for a form: scrolls up and down, and is never narrower than the form (no part of it cut off at the right)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        from qgis.PyQt.QtCore import Qt

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def setWidget(self, widget: QWidget) -> None:  # noqa: N802 (Qt's name)
        super().setWidget(widget)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt's name)
        if obj is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    def _width_for(self, inner: int) -> int:
        bar = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, self)
        return inner + bar + 2 * self.frameWidth()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt's name)
        hint = super().minimumSizeHint()
        widget = self.widget()
        if widget is None:
            return hint
        return QSize(max(hint.width(), self._width_for(widget.minimumSizeHint().width())), hint.height())

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt's name)
        hint = super().sizeHint()
        widget = self.widget()
        if widget is None:
            return hint
        return QSize(max(hint.width(), self._width_for(widget.sizeHint().width())), hint.height())
