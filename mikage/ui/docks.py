"""
The plugin's docks. The settings file is chosen only from the plugin's menu
(「設定…」); the docks don't repeat it (tsudo, 2026-11-07). Problems found when
it is read go to QGIS's message bar instead (plugin.py).

Every dock's body sits in a scroll area (tsudo, 2026-11-07): without it a dock
can't be smaller than everything in it, and QGIS's window then grows past the
screen when several docks are stacked on one side (all docks open: about
1250 × 1055 at least), so the lower docks and the window's bottom are out of
reach. In the scroll area a dock gets as small as the user drags it and its
body scrolls; at its full size nothing changes.
"""
from __future__ import annotations

import html
from typing import Callable, Optional

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import QDockWidget, QFrame, QLabel, QMessageBox, QScrollArea, QVBoxLayout, QWidget

from ..core import audit, errors, roles
from ..qgis_io.context import PluginContext
from . import error_help


def plain_tip(text: str) -> str:
    """
    Other people's text (a name, a note, a URL) for a tooltip, a message box
    or the message bar, which Qt reads as HTML when it looks like it: shown as
    it is, never as a link or markup.
    """
    if not any(c in text for c in "<>&"):
        return text
    return "<p style='white-space:pre-wrap'>" + html.escape(text).replace("\n", "<br>") + "</p>"


def plain_label(label: QLabel) -> QLabel:
    """
    A label that shows its text as it is, never as HTML (Qt reads a label's
    text as HTML when it looks like it): for labels that show other people's
    text (names, notes) or a server's message. Security re-check 2026-11-09:
    markup there could load an image from a share (\\\\host\\…), which makes
    Windows sign in to that host.

    A line whose text has an エラーコード (「［E101］」) opens the help at it on a
    click (ui/error_help.py): no link in the text, which stays plain.
    """
    label.setTextFormat(Qt.TextFormat.PlainText)
    return error_help.watch(label)


def save_text(path: str, text: str, encoding: str = "utf-8", newline: Optional[str] = None, what: str = "file",
              count: Optional[int] = None) -> Optional[str]:
    """
    Writes a file the user chose to save: None when it worked, else why not
    (the CSV still open in Excel, a folder that can't be written), to be told
    in the dock instead of a Python error. Recorded in the audit log as
    `what` (an export, with how many rows when `count`).
    """
    try:
        with open(path, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
    except OSError as error:
        reason, said = errors.describe(error)
        audit.error("file-save", path, reason, detail={"what": what, "said": said})
        return reason
    audit.record("export", what, path, count=count, file=path)
    return None


def ask_no(parent, title: str, text: str, yes: str = "") -> bool:
    """
    Asks before something that can't be undone; Enter (and Esc) answer いいえ,
    so a key pressed by habit never deletes anything. True for はい.

    With `yes` (an action such as 「削除」, tsudo 2026-11-23): the text says what
    will happen (言い切り, no question) and the buttons are that action and 「取消」
    instead of はい・いいえ; Enter and Esc still answer 取消.
    """
    if not yes:
        ok, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        return QMessageBox.question(parent, title, plain_tip(text), ok | no, no) == ok
    box = QMessageBox(QMessageBox.Icon.Question, title, plain_tip(text), parent=parent)
    go = box.addButton(yes, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is go


class PluginDock(QDockWidget):
    """A dock with a body. `open_settings` is kept for the docks that link to the settings dialog."""

    def __init__(self, object_name: str, title: str, context: PluginContext, open_settings: Callable[[], None], parent: QWidget | None = None):
        super().__init__(title, parent)
        self.setObjectName(object_name)
        self.context = context
        self.open_settings = open_settings
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        body = QWidget()
        self.body = QVBoxLayout(body)
        scroll = QScrollArea()
        scroll.setObjectName(object_name + "Scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        self.setWidget(scroll)
        self.scroll = scroll
        self.topLevelChanged.connect(lambda _: QTimer.singleShot(0, self.keep_on_screen))

    #: Whether the person's roles (担当, core/roles.py) use a feature: the plugin sets it (`set_allows`).
    allows: Callable[[str], bool] = staticmethod(lambda _feature: True)

    def set_allows(self, allows: Callable[[str], bool]) -> None:
        self.allows = allows
        self.roles_changed()

    def roles_changed(self) -> None:
        """The roles in use changed: a dock shows only the buttons of the features they use (each dock its own)."""

    def denied(self, feature: str) -> bool:
        """
        True when the roles in use do not have `feature` (its buttons are not
        shown, but a double click or a script may still ask): why, in the dock's status.
        """
        if self.allows(feature):
            return False
        status = getattr(self, "status", None)
        if isinstance(status, QLabel):
            status.setText(roles.refusal(feature))
        return True

    def reveal(self, widget: QWidget) -> None:
        """Scrolls the dock so that a part just shown (as a form under a list) is in view, once it is laid out."""
        def show() -> None:
            try:
                if widget.isVisible():
                    self.scroll.ensureWidgetVisible(widget, 0, 0)
            except RuntimeError:  # closed meanwhile
                pass

        QTimer.singleShot(0, show)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().showEvent(event)
        QTimer.singleShot(0, self.keep_on_screen)

    def keep_on_screen(self) -> None:
        """
        Docks a floating dock again when its title bar is off every screen (as after a screen is unplugged),
        and makes one larger than its screen (as one saved on a larger screen) fit in it.
        """
        if not self.isFloating():
            return
        frame = self.frameGeometry()
        title = frame.adjusted(0, 0, 0, -max(0, frame.height() - 30))
        screens = [s.availableGeometry() for s in QGuiApplication.screens()]
        if not any(s.intersects(title) for s in screens):
            self.setFloating(False)
            return
        screen = next(s for s in screens if s.intersects(title))
        if frame.width() <= screen.width() and frame.height() <= screen.height():
            return
        extra_w, extra_h = frame.width() - self.width(), frame.height() - self.height()
        w = min(frame.width(), screen.width()) - extra_w
        h = min(frame.height(), screen.height()) - extra_h
        x = min(max(frame.x(), screen.x()), screen.right() + 1 - (w + extra_w))
        y = min(max(frame.y(), screen.y()), screen.bottom() + 1 - (h + extra_h))
        self.setGeometry(x + (self.geometry().x() - frame.x()), y + (self.geometry().y() - frame.y()), w, h)
