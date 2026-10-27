"""
The plugin's docks. Each shows the state of the settings file at its top
(問題 n 件, opening the settings); the work areas are filled stage by stage.
"""
from __future__ import annotations

from typing import Callable

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import QDockWidget, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..qgis_io.context import PluginContext


class ConfigBar(QWidget):
    """A line saying where the settings came from and how many problems they had."""

    def __init__(self, context: PluginContext, open_settings: Callable[[], None], parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self.label = QLabel()
        self.label.setWordWrap(True)
        button = QPushButton(self.tr("設定…"))
        button.clicked.connect(open_settings)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label, 1)
        layout.addWidget(button)
        context.configChanged.connect(self.update_text)
        self.update_text()

    def update_text(self) -> None:
        c = self.context
        if c.loading:
            text = self.tr("設定ファイルの読み込み中")
        elif not c.source:
            text = self.tr("設定ファイルなし")
        elif c.problems:
            text = self.tr("設定ファイルの問題 {n} 件").format(n=len(c.problems))
        elif getattr(c, "bundled", False):
            text = self.tr("同梱の設定ファイル読み込み済み")
        else:
            text = self.tr("設定ファイル読み込み済み")
        self.label.setText(text)
        self.label.setToolTip("\n".join(c.problems) or c.source)


class PluginDock(QDockWidget):
    """A dock with the settings bar on top and a body below."""

    def __init__(self, object_name: str, title: str, context: PluginContext, open_settings: Callable[[], None], parent: QWidget | None = None):
        super().__init__(title, parent)
        self.setObjectName(object_name)
        self.context = context
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.addWidget(ConfigBar(context, open_settings))
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
