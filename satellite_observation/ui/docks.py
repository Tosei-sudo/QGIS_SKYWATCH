"""
The plugin's docks. The settings file is chosen only from the plugin's menu
(「設定…」); the docks don't repeat it (tsudo, 2026-11-07). Problems found when
it is read go to QGIS's message bar instead (plugin.py).
"""
from __future__ import annotations

from typing import Callable

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QVBoxLayout, QWidget

from ..qgis_io.context import PluginContext


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
        self.setWidget(body)
        self.topLevelChanged.connect(lambda _: QTimer.singleShot(0, self.keep_on_screen))

    def showEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        super().showEvent(event)
        QTimer.singleShot(0, self.keep_on_screen)

    def keep_on_screen(self) -> None:
        """Docks a floating dock again when its title bar is off every screen (as after a screen is unplugged)."""
        if not self.isFloating():
            return
        frame = self.frameGeometry()
        title = frame.adjusted(0, 0, 0, -max(0, frame.height() - 30))
        if not any(s.availableGeometry().intersects(title) for s in QGuiApplication.screens()):
            self.setFloating(False)


class StubDock(PluginDock):
    """A dock whose work area comes in a later stage."""

    def __init__(self, object_name: str, title: str, stage: str, context: PluginContext, open_settings: Callable[[], None], parent: QWidget | None = None):
        super().__init__(object_name, title, context, open_settings, parent)
        note = QLabel(self.tr("準備中（{stage}）").format(stage=stage))
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(note, 1)
