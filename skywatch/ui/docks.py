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
from typing import Callable

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import QDockWidget, QFrame, QLabel, QMessageBox, QScrollArea, QVBoxLayout, QWidget

from ..qgis_io.context import PluginContext


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
    """
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def ask_no(parent, title: str, text: str) -> bool:
    """
    Asks before something that can't be undone; Enter (and Esc) answer いいえ,
    so a key pressed by habit never deletes anything. True for はい.
    """
    yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
    return QMessageBox.question(parent, title, plain_tip(text), yes | no, no) == yes


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


class StubDock(PluginDock):
    """A dock whose work area comes in a later stage."""

    def __init__(self, object_name: str, title: str, stage: str, context: PluginContext, open_settings: Callable[[], None], parent: QWidget | None = None):
        super().__init__(object_name, title, context, open_settings, parent)
        note = QLabel(self.tr("準備中（{stage}）").format(stage=stage))
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(note, 1)
