"""
エラーコードから対処法へ (tsudo 2026-11-23): a message of the plugin's that
carries an エラーコード (core/errors.py, 「…に失敗: サーバーに接続できない［E101］」)
leads to its row in the help's 「エラーコード一覧」.

* The main window's message bar: a 「対処法」 button on the item (BarHelp).
* A dock's status line (a label made by `ui.docks.plain_label`): a click on
  the line, which shows the pointing hand and the tip 「クリックで対処法」 while
  its text has a code (`watch`). The line stays plain text: no link, since
  what it shows may come from a server (security re-check 2026-11-09).

The help is opened by what the plugin gives `set_opener` (its 「使い方（ヘルプ）」);
before that (and after the plugin is unloaded) nothing reacts.

A code shown (a status line painted with a new text that has one, a message
bar item with one) is told to what `set_seen` was given: 機能の使われ方's 迷い
counts the codes by screen (ui/feature_use.py).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from qgis.gui import QgsMessageBarItem
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QObject, Qt
from qgis.PyQt.QtWidgets import QLabel, QPushButton, QToolTip

from ..core import errors

#: The code as the plugin shows it (full-width brackets): another plugin's 「[E101]」 is not taken for one.
_SHOWN = re.compile(r"［(E\d{3})］")
#: The label property that says the pointing hand is this module's (so a cursor of the label's own is left alone).
_HAND = "mikage_error_hand"

_opener: Optional[Callable[[str], object]] = None
#: What is told of each code shown: (code, the status line, or None for the message bar).
_seen: Optional[Callable[[str, object], None]] = None
#: The text (a line's) or the code (a bar item's) last told.
_SEEN = "mikage_error_seen"


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def shown_code(text: str) -> Optional[str]:
    """The first エラーコード of the list that a text shows (「［E101］」), if any."""
    for code in _SHOWN.findall(text or ""):
        if code in errors.CODES:
            return code
    return None


def set_opener(open_help: Optional[Callable[[str], object]]) -> None:
    """What opens the help at a code (the plugin's 「使い方（ヘルプ）」); None: nothing opens it."""
    global _opener
    _opener = open_help


def set_seen(seen: Optional[Callable[[str, object], None]]) -> None:
    """What is told of each error code shown (None: nothing)."""
    global _seen
    _seen = seen


def _tell(code: str, widget) -> None:
    try:
        if _seen is not None:
            _seen(code, widget)
    except Exception:  # noqa: BLE001 (counting never breaks a message)
        pass


def open_code(code: str) -> bool:
    """The help at the code's row; False when there is no help to open."""
    if _opener is None or code not in errors.CODES:
        return False
    _opener(code)
    return True


def tip(code: str) -> str:
    return tr("クリックで対処法（{c} {t}）").format(c=code, t=errors.CODES[code].text)


class _LabelClicks(QObject):
    """The status lines' clicks (one object for every watched label)."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt's name)
        if not isinstance(watched, QLabel):
            return False
        kind = event.type()
        if kind == QEvent.Type.Paint:
            # Shown with a new text (a hidden line is not painted).
            if _seen is not None:
                text = watched.text()
                if watched.property(_SEEN) != text:
                    watched.setProperty(_SEEN, text)
                    code = shown_code(text)
                    if code is not None:
                        _tell(code, watched)
        elif kind == QEvent.Type.Enter:
            self._cursor(watched)
        elif kind == QEvent.Type.ToolTip:
            code = shown_code(watched.text()) if _opener is not None else None
            if code is not None and not watched.toolTip():
                QToolTip.showText(event.globalPos(), tip(code), watched)
                return True
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            watched.setProperty("mikage_press", event.pos())
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            pressed = watched.property("mikage_press")
            watched.setProperty("mikage_press", None)
            # A click, not a drag that selects part of the text.
            if pressed is not None and (event.pos() - pressed).manhattanLength() <= 4 and not watched.hasSelectedText():
                code = shown_code(watched.text())
                if code is not None and open_code(code):
                    return True
        return False

    @staticmethod
    def _cursor(label: QLabel) -> None:
        """The pointing hand while the line's text has a code (its text changes: set on each entry)."""
        has = _opener is not None and shown_code(label.text()) is not None
        if has:
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.setProperty(_HAND, True)
        elif label.property(_HAND):
            label.unsetCursor()
            label.setProperty(_HAND, False)


_clicks: Optional[_LabelClicks] = None


def watch(label: QLabel) -> QLabel:
    """A label whose text, when it has a code, leads to its row in the help on a click."""
    global _clicks
    if _clicks is None:
        _clicks = _LabelClicks(QCoreApplication.instance())
    label.installEventFilter(_clicks)
    return label


#: The 「対処法」 button's object name, and the property with the code it opens.
BUTTON = "mikageErrorHelp"
_CODE = "mikage_error_code"


class BarHelp(QObject):
    """The 「対処法」 button on the message bar's items that carry a code.

    The bar says `widgetAdded` each time an item comes on top, not only when it
    is pushed: a message under a newer one comes back when that one is closed or
    its time is up. So an item keeps one button, found again then, for the code
    its text has at that time (two or more side by side before, tsudo 2026-11-27).
    """

    def __init__(self, bar, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.bar = bar
        bar.widgetAdded.connect(self._shown)

    def _shown(self, widget) -> None:
        try:
            item = sip.cast(widget, QgsMessageBarItem)
            code = shown_code(f"{item.title()} {item.text()}")
            layout = item.layout()
            if layout is None:
                return
            button = item.findChild(QPushButton, BUTTON, Qt.FindChildOption.FindDirectChildrenOnly)
            if code is None:
                if button is not None:
                    layout.removeWidget(button)
                    button.deleteLater()
                return
            if button is None:
                button = QPushButton(tr("対処法"), item)
                button.setObjectName(BUTTON)
                button.clicked.connect(lambda _checked=False, b=button: open_code(b.property(_CODE) or ""))
                layout.addWidget(button)
            button.setProperty(_CODE, code)
            button.setToolTip(tr("{c} の原因と対処法をヘルプで見る").format(c=code))
            if item.property(_SEEN) != code:
                item.setProperty(_SEEN, code)
                _tell(code, None)
        except (TypeError, RuntimeError, AttributeError):
            pass

    def unload(self) -> None:
        try:
            self.bar.widgetAdded.disconnect(self._shown)
        except (TypeError, RuntimeError):
            pass
