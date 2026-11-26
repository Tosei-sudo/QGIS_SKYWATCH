"""
Buttons painted in the last column of a list (QTableWidget), not made as
widgets: a list of a few hundred records made a widget and its buttons for
every row each time it was shown (tsudo 2026-11-26: 観測記録が 100 件以上
でカクつく; 300 rows: 330 ms, of which the widgets were 50 ms and adding the
rows one by one most of the rest). They look and lie as the tool buttons did
(the same style and style sheet, the same places in a stretched column),
show their tips and answer a click (on its release over the same button);
a press on one neither chooses the row nor opens it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from qgis.PyQt.QtCore import QEvent, QObject, QRect, QSize, Qt
from qgis.PyQt.QtWidgets import (
    QStyle, QStyledItemDelegate, QStyleOptionToolButton, QTableWidget, QToolButton, QToolTip,
)

#: A pressed toggle (「流れ」): as the tool buttons were styled.
CHECKED_STYLE = "QToolButton:checked { background: #dbeafe; border: 1px solid #2563eb; color: #1e3a8a; font-weight: bold; }"


@dataclass
class RowButton:
    text: str
    tip: str
    slot: Callable[[], None]
    #: A toggle's state (None: not a toggle).
    checked: Optional[bool] = None


class _Painter(QStyledItemDelegate):
    """The last column's buttons painted over the cell (the cell itself as the list paints it)."""

    def __init__(self, buttons: "RowButtons"):
        super().__init__(buttons.table)
        self.buttons = buttons

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        self.buttons.paint_row(painter, index.row(), option.rect)


class RowButtons(QObject):
    """The buttons of each row of `table` (its last column): set with `set_row`, emptied with `clear`."""

    def __init__(self, table: QTableWidget):
        super().__init__(table)
        self.table = table
        self._rows: list[list[RowButton]] = []
        #: The button under the pointer, and the one pressed: (row, index).
        self._hover: Optional[tuple[int, int]] = None
        self._pressed: Optional[tuple[int, int]] = None
        # One tool button per kind (a plain one, a toggle up and down styled as the toggles were), never shown:
        # what they look like and how big they are (only their text changes: a button keeps its size for it).
        self._looks: dict[Optional[bool], QToolButton] = {}
        for checked in (None, False, True):
            look = QToolButton(table)
            if checked is not None:
                look.setCheckable(True)
                look.setChecked(checked)
                look.setStyleSheet(CHECKED_STYLE)
            look.hide()
            self._looks[checked] = look
        self._sizes: dict[tuple, QSize] = {}
        # Only the last column's cells are painted here (the others as the list paints them, without Python).
        self._painter = _Painter(self)
        self._painted_column = -1
        table.viewport().setMouseTracking(True)
        table.viewport().installEventFilter(self)

    # ---- the rows' buttons ---------------------------------------------------

    def column(self) -> int:
        return self.table.columnCount() - 1

    def clear(self) -> None:
        self._rows = []
        self._hover = self._pressed = None

    def set_row(self, row: int, buttons: list[RowButton]) -> None:
        """A row's buttons; its last cell (an item) is sized for them, so the columns fit them as they fitted the widgets."""
        while len(self._rows) <= row:
            self._rows.append([])
        self._rows[row] = list(buttons)
        column = self.column()
        if column != self._painted_column:
            if self._painted_column >= 0:
                self.table.setItemDelegateForColumn(self._painted_column, None)
            self.table.setItemDelegateForColumn(column, self._painter)
            self._painted_column = column
        item = self.table.item(row, column)
        size = self.size_of(row)
        if item is not None and size is not None:
            item.setSizeHint(size)

    def of(self, row: int) -> list[RowButton]:
        return self._rows[row] if 0 <= row < len(self._rows) else []

    def click(self, row: int, text: str) -> bool:
        """The button `text` of a row pressed (for scripts and tests); whether there was one."""
        b = next((b for b in self.of(row) if b.text == text), None)
        if b is None:
            return False
        b.slot()
        return True

    # ---- where they lie ------------------------------------------------------

    def _look(self, b: RowButton) -> QToolButton:
        look = self._looks[b.checked]
        if look.text() != b.text:
            look.setText(b.text)
        return look

    def _size(self, b: RowButton) -> QSize:
        key = (b.text, b.checked)
        size = self._sizes.get(key)
        if size is None:
            look = self._look(b)
            look.ensurePolished()
            size = self._sizes[key] = look.sizeHint()
        return size

    def size_of(self, row: int) -> Optional[QSize]:
        buttons = self.of(row)
        if not buttons:
            return None
        sizes = [self._size(b) for b in buttons]
        spacing = self._spacing()
        return QSize(sum(s.width() for s in sizes) + spacing * (len(sizes) - 1), max(s.height() for s in sizes))

    def _spacing(self) -> int:
        """The room between two buttons, as a box of them got it from the style."""
        style = self.table.style()
        spacing = style.pixelMetric(QStyle.PixelMetric.PM_LayoutHorizontalSpacing, None, self.table)
        if spacing < 0:
            kind = self._looks[None].sizePolicy().controlType()
            spacing = style.layoutSpacing(kind, kind, Qt.Orientation.Horizontal, None, self.table)
        return max(0, spacing)

    def rects(self, row: int, cell: Optional[QRect] = None) -> list[QRect]:
        """
        Where a row's buttons lie in the list (its viewport), as a row of
        fixed-size buttons in a box lay in the cell: the room left over
        shared out before, between and after them.
        """
        buttons = self.of(row)
        if not buttons:
            return []
        if cell is None:
            cell = self.table.visualRect(self.table.model().index(row, self.column()))
        sizes = [self._size(b) for b in buttons]
        spacing = self._spacing()
        widths = [s.width() for s in sizes]
        free = cell.width() - sum(widths) - spacing * (len(sizes) - 1)
        if free < 0:
            # A narrow column: the buttons squeezed alike (their letters cut), as the box squeezed them.
            room = max(0, cell.width() - spacing * (len(sizes) - 1))
            total = sum(widths) or 1
            squeezed = [room * w // total for w in widths]
            for i in range(room - sum(squeezed)):
                squeezed[i % len(squeezed)] += 1
            widths, free = squeezed, 0
        # As the box shared the room left: the same whole pixels before each button, what is left after the last.
        gap = free // (len(sizes) + 1)
        out, x = [], cell.left()
        for s, w in zip(sizes, widths):
            x += gap
            h = min(s.height(), cell.height())
            out.append(QRect(x, cell.top() + (cell.height() - h) // 2, w, h))
            x += w + spacing
        return out

    def _at(self, pos) -> Optional[tuple[int, int]]:
        index = self.table.indexAt(pos)
        if not index.isValid() or index.column() != self.column():
            return None
        for i, r in enumerate(self.rects(index.row())):
            if r.contains(pos):
                return index.row(), i
        return None

    # ---- painted ---------------------------------------------------------------

    def paint_row(self, painter, row: int, cell: QRect) -> None:
        buttons = self.of(row)
        if not buttons:
            return
        painter.save()
        painter.setClipRect(cell)
        for i, (b, rect) in enumerate(zip(buttons, self.rects(row, cell))):
            look = self._look(b)
            option = QStyleOptionToolButton()
            look.initStyleOption(option)
            option.rect = rect
            painter.setClipRect(rect.intersected(cell))
            state = option.state & ~(QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken | QStyle.StateFlag.State_HasFocus)
            if self._hover == (row, i):
                state |= QStyle.StateFlag.State_MouseOver
                if self._pressed == (row, i):
                    state = (state | QStyle.StateFlag.State_Sunken) & ~QStyle.StateFlag.State_Raised
            option.state = state
            look.style().drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option, painter, look)
        painter.restore()

    def _update_row(self, at: Optional[tuple[int, int]]) -> None:
        if at is not None:
            self.table.viewport().update(self.table.visualRect(self.table.model().index(at[0], self.column())))

    # ---- answered --------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt's name)
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            at = self._at(event.pos())
            if at != self._hover:
                self._update_row(self._hover)
                self._hover = at
                self._update_row(at)
            return self._pressed is not None
        if kind == QEvent.Type.Leave:
            self._update_row(self._hover)
            self._hover = None
            return False
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            at = self._at(event.pos())
            if at is None:
                return False
            self._pressed = self._hover = at
            self._update_row(at)
            return True
        if kind == QEvent.Type.MouseButtonRelease and self._pressed is not None:
            pressed, self._pressed = self._pressed, None
            self._update_row(pressed)
            if event.button() == Qt.MouseButton.LeftButton and self._at(event.pos()) == pressed:
                buttons = self.of(pressed[0])
                if pressed[1] < len(buttons):
                    buttons[pressed[1]].slot()
            return True
        if kind == QEvent.Type.ToolTip:
            at = self._at(event.pos())
            if at is not None:
                b = self.of(at[0])[at[1]]
                QToolTip.showText(event.globalPos(), b.tip, self.table.viewport(), self.rects(at[0])[at[1]])
                return True
        return False
