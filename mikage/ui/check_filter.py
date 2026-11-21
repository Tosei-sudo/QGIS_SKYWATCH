"""
A list's filter as a drop-down of check boxes (tsudo, 2026-11-16: 「撮像計画系
の画面において、状態を複数（例えば、採用と不採用の 2 つ）選択して表示する
需要があるので、状態によるフィルター、依頼グループによるフィルターを
チェックボックス形式にしてほしい」).

It looks like the other filters (a combo box, as narrow), and its list ticks
items instead of choosing one: the rows of any item ticked are shown, every
row while none is (the first entry, 「すべての状態」, is then ticked; a click
on it takes the ticks off). The list stays open while items are ticked
(a click, or Space / Enter), and closes on a click outside it or Esc. A
preset (「計画に入ったもの（採用・要求外）」) ticks its items in one click.
The box shows what is ticked (「採用・不採用」), shortened to its width, in
full in its tooltip.

The choice can be kept per user (QgsSettings, `key`): one value as it is
(as the single-choice filters kept it, so an older plugin still reads it),
several as a JSON list. A 「ビュー」 of a list (ui/list_views.py) keeps it
as {"checked": [...]}, and still reads what the single-choice box kept
({"data": ...} / {"text": ...}).
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional, Sequence

from qgis.PyQt.QtCore import QEvent, QPoint, QRect, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import QComboBox, QStyle, QStyledItemDelegate, QStyleOptionComboBox, QStylePainter

from ..qgis_io import settings
from .docks import plain_tip

#: An item's value, the entry's role (a check box, the first 「すべて」, a preset), a preset's values.
_VALUE = Qt.ItemDataRole.UserRole + 1
_ROLE = Qt.ItemDataRole.UserRole + 2
_PRESET = Qt.ItemDataRole.UserRole + 3
ITEM, ALL_ROW, PRESET, LINE = "item", "all", "preset", "line"
#: Between the ticked items' names in the box.
JOIN = "・"
#: The size of the colour beside a name (pixels).
SWATCH = 12


def _swatch(colour: str) -> QIcon:
    """A small square of a colour (the one the list and the map give the item)."""
    pixmap = QPixmap(SWATCH, SWATCH)
    pixmap.fill(QColor(0, 0, 0, 0))
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    p.drawRoundedRect(QRect(1, 1, SWATCH - 2, SWATCH - 2), 2, 2)
    p.end()
    return QIcon(pixmap)


def kept_values(key: str) -> Optional[list]:
    """
    The values kept under `key` (QgsSettings): [] for all, None when nothing
    was kept. One value is kept as it is, several as a JSON list.
    """
    text = settings.pref(key, "\0")
    if text == "\0":
        return None
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list) and all(isinstance(v, str) for v in data):
            return data
    return [text]


def keep_values(key: str, values: Sequence[str]) -> None:
    """Keeps the values ticked under `key` (none: "" for all)."""
    values = [str(v) for v in values]
    settings.set_pref(key, "" if not values else values[0] if len(values) == 1 else json.dumps(values, ensure_ascii=False))


class _Delegate(QStyledItemDelegate):
    """The list's entries: check boxes drawn from their check state (whatever the style), lines between the parts."""

    def paint(self, painter, option, index):
        if index.data(_ROLE) == LINE:
            painter.save()
            painter.setPen(option.palette.color(QPalette.ColorRole.Mid))
            y = option.rect.center().y()
            painter.drawLine(option.rect.left() + 4, y, option.rect.right() - 4, y)
            painter.restore()
            return
        super().paint(painter, option, index)

    def sizeHint(self, option, index):  # noqa: N802 (Qt's name)
        if index.data(_ROLE) == LINE:
            return QSize(10, 7)
        size = super().sizeHint(option, index)
        # Rows tall enough to hit with the mouse (the same with or without a colour).
        return QSize(size.width(), max(size.height(), option.fontMetrics.height() + 8))


class CheckFilter(QComboBox):
    """
    A filter of check boxes: `all_text` the first entry (no narrowing),
    `items` (text, value[, tip[, colour]]) the values, `presets` (text, values) ticked in
    one click, `key` where the choice is kept (none: not kept), `default`
    the values ticked when nothing was kept.
    """

    #: The ticks changed (by the user or by `choose`).
    changed = pyqtSignal()

    def __init__(self, all_text: str, items: Iterable[tuple] = (), *, presets: Iterable[tuple] = (), key: Optional[str] = None,
                 default: Sequence[str] = (), tip: str = "", parent=None):
        super().__init__(parent)
        self.all_text = all_text
        self.key = key
        self.presets: list[tuple[str, tuple]] = [(t, tuple(v)) for t, v in presets]
        self._items: list[tuple[str, Any, str]] = []
        #: The items' colours (by their place in the list), shown beside their names.
        self._colors: dict = {}
        self._ticked: list = []
        self._tip = tip
        self._pressed = False
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setItemDelegate(_Delegate(self))
        self.view().viewport().installEventFilter(self)
        self.view().installEventFilter(self)
        self.view().setIconSize(QSize(SWATCH, SWATCH))
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(14)
        kept = kept_values(key) if key else None
        self._ticked = list(default) if kept is None else kept
        items = list(items)
        if items:
            self.set_items(items)
        else:
            # The values come later (set_items): the ticks kept wait for them.
            self._fill()

    # --- The entries ----------------------------------------------------------------------------

    def set_items(self, items: Iterable[tuple], keep_unknown: bool = False) -> None:
        """
        The values to tick: (text, value), (text, value, tip) or (text, value,
        tip, colour such as "#16a34a", shown beside the name). The ticks of
        values no longer listed go, unless `keep_unknown` (they are then
        listed at the end, under their own name).
        """
        items = list(items)
        self._colors = {i: t[3] for i, t in enumerate(items) if len(t) > 3 and t[3]}
        items = [(t[0], t[1], t[2] if len(t) > 2 else "") for t in items]
        known = [v for _t, v, _tip in items]
        if keep_unknown:
            items += [(str(v), v, "") for v in self._ticked if not any(self.same(v, k) for k in known)]
        else:
            self._ticked = [v for v in self._ticked if any(self.same(v, k) for k in known)]
        self._items = items
        self._fill()

    def _fill(self) -> None:
        self.blockSignals(True)
        self._model.clear()
        first = QStandardItem(self.all_text)
        first.setData(ALL_ROW, _ROLE)
        first.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        first.setToolTip(plain_tip(self.tr("絞り込まない（チェックをすべて外す）")))
        self._model.appendRow(first)
        for text, values in self.presets:
            row = QStandardItem(text)
            row.setData(PRESET, _ROLE)
            row.setData(list(values), _PRESET)
            row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            font = row.font()
            font.setItalic(True)
            row.setFont(font)
            row.setToolTip(plain_tip(self.tr("{v} だけにチェック").format(v=JOIN.join(self._text_of(v) for v in values))))
            self._model.appendRow(row)
        if self._items:
            line = QStandardItem("")
            line.setData(LINE, _ROLE)
            line.setFlags(Qt.ItemFlag.NoItemFlags)
            self._model.appendRow(line)
        for i, (text, value, tip) in enumerate(self._items):
            row = QStandardItem(text)
            row.setData(ITEM, _ROLE)
            row.setData(value, _VALUE)
            if i in self._colors:
                # The colour the list and the map give it.
                row.setData(_swatch(self._colors[i]), Qt.ItemDataRole.DecorationRole)
            row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            if tip:
                row.setToolTip(plain_tip(tip))
            self._model.appendRow(row)
        self.setCurrentIndex(0)
        self.blockSignals(False)
        # The list as wide as its longest name (not cut to the box's width).
        metrics = self.view().fontMetrics()
        longest = max((metrics.horizontalAdvance(self._model.item(i).text()) for i in range(self._model.rowCount())), default=0)
        self.view().setMinimumWidth(longest + 3 * SWATCH + 30)
        self._show_ticks()

    def items(self) -> list[tuple[str, Any]]:
        """The values listed (text, value), in their order."""
        return [(t, v) for t, v, _tip in self._items]

    @staticmethod
    def same(a, b) -> bool:
        """Whether two values are one (a subclass compares names without case)."""
        return a == b

    def _text_of(self, value) -> str:
        return next((t for t, v, _tip in self._items if self.same(v, value)), str(value))

    # --- The choice -----------------------------------------------------------------------------

    def chosen(self) -> list:
        """The values ticked, in the list's order ([] for no narrowing)."""
        listed = [v for _t, v, _tip in self._items if any(self.same(v, x) for x in self._ticked)]
        return listed + [v for v in self._ticked if not any(self.same(v, x) for x in listed)]

    def is_all(self) -> bool:
        """Whether nothing narrows the list (no tick)."""
        return not self._ticked

    def passes(self, value) -> bool:
        """Whether a row of this value is shown."""
        return not self._ticked or any(self.same(value, v) for v in self._ticked)

    def choose(self, values) -> None:
        """Ticks these values only (one value, a list, or None / [] for no narrowing); told as a click is."""
        if values is None:
            values = []
        elif isinstance(values, (str, int, float)):
            values = [values]
        values = list(values)
        if values == self._ticked:
            return
        self._ticked = values
        self._changed()

    def toggle(self, value) -> None:
        """Ticks a value, or takes its tick off."""
        if any(self.same(value, v) for v in self._ticked):
            self._ticked = [v for v in self._ticked if not self.same(value, v)]
        else:
            self._ticked.append(value)
            # In the list's order.
            self._ticked = self.chosen()
        self._changed()

    def _changed(self) -> None:
        self._show_ticks()
        if self.key:
            keep_values(self.key, [str(v) for v in self.chosen()])
        self.changed.emit()

    def text(self) -> str:
        """What the box shows: 「すべての…」, a preset's name, or the names ticked."""
        chosen = self.chosen()
        if not chosen:
            return self.all_text
        for text, values in self.presets:
            if len(values) == len(chosen) and all(any(self.same(v, c) for c in chosen) for v in values):
                return text
        return JOIN.join(self._text_of(v) for v in chosen)

    def _show_ticks(self) -> None:
        chosen = self.chosen()
        for i in range(self._model.rowCount()):
            row = self._model.item(i)
            role = row.data(_ROLE)
            if role == ALL_ROW:
                on = not chosen
            elif role == PRESET:
                values = row.data(_PRESET) or []
                on = bool(chosen) and len(values) == len(chosen) and all(any(self.same(v, c) for c in chosen) for v in values)
            elif role == ITEM:
                on = any(self.same(row.data(_VALUE), c) for c in chosen)
            else:
                continue
            row.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        tip = self._tip
        shown = self.tr("表示中: {t}").format(t=self.text())
        super().setToolTip(plain_tip(f"{tip}\n{shown}" if tip else shown))
        self.update()

    def setToolTip(self, tip: str) -> None:  # noqa: N802 (Qt's name)
        """The box's own words (what is ticked is added under them)."""
        self._tip = tip
        self._show_ticks()

    # --- 「ビュー」 (ui/list_views.py) ------------------------------------------------------------

    def view_state(self) -> dict:
        return {"checked": [v for v in self.chosen() if isinstance(v, (str, int, float, bool))]}

    def set_view_state(self, state) -> None:
        """Puts a view's choice back: {"checked": [...]}, or what a single-choice box kept ({"data": v} / {"text": t})."""
        if not isinstance(state, dict):
            return
        if isinstance(state.get("checked"), list):
            self.choose(state["checked"])
        elif "data" in state:
            self.choose([] if state["data"] in (None, "") else [state["data"]])
        elif "text" in state:
            text = str(state.get("text", ""))
            if text == self.all_text:
                self.choose([])
                return
            preset = next((v for t, v in self.presets if t == text), None)
            if preset is not None:
                self.choose(list(preset))
                return
            value = next((v for t, v, _tip in self._items if t == text), None)
            if value is not None:
                self.choose([value])

    # --- The list kept open ---------------------------------------------------------------------

    def _clicked(self, index) -> None:
        if not index.isValid():
            return
        role = index.data(_ROLE)
        if role == ALL_ROW:
            self.choose([])
        elif role == PRESET:
            values = list(index.data(_PRESET) or [])
            on = self._model.itemFromIndex(index).checkState() == Qt.CheckState.Checked
            self.choose([] if on else values)
        elif role == ITEM:
            self.toggle(index.data(_VALUE))

    def eventFilter(self, watched, event):  # noqa: N802 (Qt's name)
        view = self.view()
        kind = event.type()
        if watched is view.viewport():
            if kind == QEvent.Type.MouseButtonPress:
                self._pressed = True
                return False
            if kind == QEvent.Type.MouseButtonRelease:
                # Only a click made in the list (not the release of the press that opened it).
                pressed, self._pressed = self._pressed, False
                if pressed and event.button() == Qt.MouseButton.LeftButton:
                    pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                    self._clicked(view.indexAt(pos))
                return True
        elif watched is view and kind == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select):
                self._clicked(view.currentIndex())
                return True
        return super().eventFilter(watched, event)

    def showPopup(self) -> None:  # noqa: N802 (Qt's name)
        self._pressed = False
        super().showPopup()
        # Under the box (some styles open the list over it), so that what is ticked shows in the box meanwhile.
        popup = self.view().window()
        below = self.mapToGlobal(QPoint(0, self.height()))
        screen = self.screen().availableGeometry() if hasattr(self, "screen") and self.screen() is not None else None
        if popup is not self.window() and popup.y() < below.y() and (screen is None or below.y() + popup.height() <= screen.bottom()):
            popup.move(popup.x(), below.y())

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        # Not a list to scroll through one by one (it would choose an entry).
        event.ignore()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home,
                           Qt.Key.Key_End, Qt.Key.Key_Space):
            self.showPopup()
            return
        super().keyPressEvent(event)

    # --- The box --------------------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        field = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt, QStyle.SubControl.SC_ComboBoxEditField, self)
        opt.currentText = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(10, field.width() - 2))
        opt.currentIcon = QIcon()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)
