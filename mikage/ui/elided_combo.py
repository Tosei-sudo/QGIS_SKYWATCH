"""
A drop-down as wide as where it is, not as its longest item (tsudo,
2026-11-25: 「観測結果の画像選択プルダウン、長いIDのとき幅がすごいことになる」).

Qt makes a combo box as wide as its longest item the first time it is shown
(AdjustToContentsOnFirstShow), and a dock as wide as what is in it: one
long image ID (S1A_IW_GRDH_1SDV_…_9F3C) made the 観測記録 dock about 1,000
pixels wide, its row of buttons scrolled out of sight. `ElidedCombo` instead

* is as wide as its row gives it (at least `chars` characters), never wider;
* shows the chosen item shortened to that width: an item added with
  `add_parts(head, middle, tail)` keeps its head and tail whole and loses the
  middle of its middle (「2026-10-02 10:00  S2B_MSIL2A_2026…_20261002T034512
  （Sentinel-2B）」: the time, the ID's start and end, the satellite); when
  narrower, its tail goes first (「2026-10-02 10:00  S2B_MS…034512」), then
  its end; any other item loses its end;
* opens its list as wide as the longest item (at most the screen's width,
  toward the left when there is no room on the right), so every item is
  read whole there;
* with `full_tip`, has the chosen item in full above its own tooltip (the
  観測記録 dock's tip names the image already).
"""
from __future__ import annotations

from typing import Any, Optional

from qgis.PyQt.QtCore import QEvent, QRect, Qt
from qgis.PyQt.QtWidgets import QApplication, QComboBox, QSizePolicy, QStyle, QStyleOptionComboBox, QStylePainter, QToolTip, QWidget

#: The item role holding (head, middle, tail) of an item added with `add_parts`.
PARTS_ROLE = Qt.ItemDataRole.UserRole + 41
#: The least of the middle kept (in characters' widths) before the whole text is shortened at its end instead.
LEAST_MIDDLE = 6


def shortened(fm, text: str, width: int, parts: Optional[tuple] = None) -> str:
    """`text` within `width` pixels: whole if it fits, else the middle of parts' middle cut, else its end cut."""
    if fm.horizontalAdvance(text) <= width:
        return text
    if parts:
        head, middle, tail = parts
        least = fm.horizontalAdvance("M") * LEAST_MIDDLE
        # The tail too when there is room for it, else without it (it is in the list and the tooltip).
        for end in (tail, ""):
            room = width - fm.horizontalAdvance(head) - fm.horizontalAdvance(end)
            if middle and room >= least:
                return head + fm.elidedText(middle, Qt.TextElideMode.ElideMiddle, room) + end
    return fm.elidedText(text, Qt.TextElideMode.ElideRight, max(width, 0))


class ElidedCombo(QComboBox):
    """A combo box as wide as its row lets it be (at least `chars` characters), its text shortened to fit."""

    def __init__(self, chars: int = 20, parent: Optional[QWidget] = None, full_tip: bool = False):
        super().__init__(parent)
        self.full_tip = full_tip
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(chars)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def add_parts(self, head: str, middle: str, tail: str = "", data: Any = None) -> None:
        """An item read as head + middle + tail, its middle cut first when the box is narrow."""
        self.addItem(head + middle + tail, data)
        self.setItemData(self.count() - 1, (head, middle, tail), PARTS_ROLE)

    def field_width(self) -> int:
        """The width of the text in the box, in pixels."""
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        field = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt, QStyle.SubControl.SC_ComboBoxEditField, self)
        return max(10, field.width() - 4)

    def shown_text(self) -> str:
        """The chosen item as the box shows it (shortened to its width)."""
        index = self.currentIndex()
        parts = self.itemData(index, PARTS_ROLE) if index >= 0 else None
        return shortened(self.fontMetrics(), self.currentText(), self.field_width(), tuple(parts) if parts else None)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText = self.shown_text()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)

    def tip(self) -> str:
        """The tooltip shown: with `full_tip`, the chosen item in full above the box's own tip."""
        own = self.toolTip()
        if not self.full_tip or self.currentIndex() < 0:
            return own
        return f"{self.currentText()}\n{own}" if own else self.currentText()

    def event(self, event) -> bool:
        if self.full_tip and event.type() == QEvent.Type.ToolTip:
            QToolTip.showText(event.globalPos(), self.tip(), self)
            return True
        return super().event(event)

    def list_width(self) -> int:
        """The width the open list needs for its longest item (its frame and scroll bar included)."""
        view = self.view()
        fm = view.fontMetrics()
        icon = self.iconSize().width() + 4
        texts = [fm.horizontalAdvance(self.itemText(i)) + (0 if self.itemIcon(i).isNull() else icon) for i in range(self.count())]
        # The item's margins in the list (as Qt's own popups leave), its frame.
        width = max(texts, default=0) + 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, view) + 12
        width += 2 * view.frameWidth()
        if self.count() > self.maxVisibleItems():
            width += self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, view)
        return width

    def showPopup(self) -> None:  # noqa: N802 (Qt's name)
        super().showPopup()
        popup = self.view().window()
        if popup is None or popup is self.window():
            return
        screen = popup.screen() if hasattr(popup, "screen") else None
        area = screen.availableGeometry() if screen is not None else QApplication.primaryScreen().availableGeometry()
        geometry = popup.geometry()
        width = min(max(geometry.width(), self.list_width()), area.width())
        # Toward the left when the right has no room (a dock on the right of the window).
        left = max(area.left(), min(geometry.left(), area.right() + 1 - width))
        wanted = QRect(left, geometry.top(), width, geometry.height())
        if wanted != geometry:
            popup.setGeometry(wanted)
