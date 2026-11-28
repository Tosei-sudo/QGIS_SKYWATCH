"""
画面の一部をしまう枠 (tsudo, 2026-11-28: ニールセンの 10 原則の点検 5・15「詳細条件がチェック・▶・「詳細」と
3 通り」「閉じた枠が中身のない灰色の枠に見える」を直す): every part of a dock or a dialog kept behind its
heading is this one kind, QGIS's own collapsible group (▶ to open, ▼ to close), closed at first.

* What is in it works whether it is open or closed: closing only hides it. A part that holds conditions
  says in its closed heading those that are set (`set_summary`: 「詳細条件（角度 20° まで・SAR）」), so a
  condition never works out of sight.
* Closed, it is its heading only (a line, no empty frame).
* `remember`: open or closed kept per user (QgsSettings `mikage/folds/<remember>`), as it was left.

QGIS hides the group's own children when it closes and shows them all again when it opens, so a
part that hides some of its rows itself (by role, by the settings file) keeps them in `body`, the
group's only child: those rows stay as the part left them. QGIS also ignores an opening or closing
while the group is hidden once it has been shown (its dock behind another): `set_open` keeps it for
the next show.
"""
from __future__ import annotations

from typing import Optional

from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget

from ..qgis_io import qsettings


def _key(remember: str) -> str:
    return f"folds/{remember}"


class Fold(QgsCollapsibleGroupBox):
    """A part behind its heading: closed at first (or as the user left it, `remember`), its widgets in `body`."""

    #: Opened (True) or closed (False), by the user or the code.
    opened = pyqtSignal(bool)

    def __init__(self, title: str, remember: str = "", parent: Optional[QWidget] = None):
        super().__init__(title, parent)
        # QGIS's own keeping needs an object name and keeps it per window; this keeps it per user instead.
        self.setSaveCollapsedState(False)
        self.setSaveCheckedState(False)
        self._title = title
        self._summary = ""
        self._remember = remember
        #: Opened or closed while hidden: put on screen when it is shown next.
        self._pending: Optional[bool] = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 2, 6, 4)
        #: The widgets of the part: lay them out in its layout (`body.setLayout`, or `QFormLayout(fold.body)`).
        self.body = QWidget(self)
        outer.addWidget(self.body)
        self.setCollapsed(not self._kept_open())
        self.collapsedStateChanged.connect(self._changed)

    def _kept_open(self) -> bool:
        if not self._remember:
            return False
        return qsettings.value(_key(self._remember), "0") in ("1", 1, True, "true")

    def is_open(self) -> bool:
        return not self.isCollapsed()

    def set_open(self, on: bool) -> None:
        on = bool(on)
        if self.isVisible():
            self._pending = None
            if self.isCollapsed() == on:
                self.setCollapsed(not on)
            return
        if self.isCollapsed() != on:
            return
        self.setCollapsed(not on)
        self._pending = on
        self._changed(not on)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().showEvent(event)
        if self._pending is not None:
            on, self._pending = self._pending, None
            # Already its state: this only lays it out as that (QGIS skipped it while hidden).
            self.setCollapsed(not on)

    def heading(self) -> str:
        """Its name without what is set inside (the title shows that too while it is closed)."""
        return self._title

    def set_summary(self, text: str) -> None:
        """What is set inside, shown in the heading while it is closed ("" for nothing)."""
        self._summary = text
        self._show_title()

    def _show_title(self) -> None:
        shown = self._title + (f"（{self._summary}）" if self._summary and self.isCollapsed() else "")
        if self.title() != shown:
            self.setTitle(shown)

    def _changed(self, collapsed: bool) -> None:
        if self._remember:
            qsettings.set_value(_key(self._remember), "0" if collapsed else "1")
        self._show_title()
        self.opened.emit(not collapsed)
