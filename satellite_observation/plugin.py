"""The plugin: its menu, toolbar and docks."""
from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDockWidget

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from .qgis_io.context import PluginContext
from .ui.docks import StubDock
from .ui.imaging_plan_dock import ImagingPlanDock
from .ui.settings_dialog import SettingsDialog

HERE = os.path.dirname(__file__)
MENU = "衛星撮像計画・観測記録"


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class PluginApi:
    """The plugin's Python API (`plugin.api`), for the Python console and Processing scripts; filled stage by stage."""

    def __init__(self, plugin: "SatelliteObservationPlugin"):
        self._plugin = plugin

    @property
    def config(self):
        """The settings file as read (core.config.PluginConfig)."""
        return self._plugin.context.config

    @property
    def problems(self) -> list[str]:
        """What was wrong in the settings file."""
        return list(self._plugin.context.problems)

    def reload_config(self) -> None:
        """Reads the settings file again."""
        self._plugin.context.reload()

    @property
    def imaging_plan(self) -> "ImagingPlanDock":
        """The imaging plan dock (its `plan`, `shown`, `run()`, `set_point()`, `csv_text()`, `add_layer()`)."""
        return self._plugin.docks["SatObsImagingPlan"]


class SatelliteObservationPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.context: Optional[PluginContext] = None
        self.actions: list[QAction] = []
        self.docks: dict[str, QDockWidget] = {}
        self.toolbar = None
        self.api = PluginApi(self)

    def initGui(self) -> None:  # noqa: N802 (QGIS's name)
        self.context = PluginContext(self.iface.mainWindow())
        self.toolbar = self.iface.addToolBar(tr("衛星撮像計画・観測記録"))
        self.toolbar.setObjectName("SatelliteObservationToolbar")
        icon = QIcon(os.path.join(HERE, "resources", "icon.svg"))

        specs = [
            ("SatObsImagingPlan", tr("撮像計画"), None, Qt.DockWidgetArea.RightDockWidgetArea),
            ("SatObsSatellitePass", tr("衛星パス"), tr("段階 5"), Qt.DockWidgetArea.RightDockWidgetArea),
            ("SatObsObservations", tr("観測記録"), tr("段階 6〜7"), Qt.DockWidgetArea.RightDockWidgetArea),
            ("SatObsTaxonomy", tr("分類体系の編集"), tr("段階 7"), Qt.DockWidgetArea.LeftDockWidgetArea),
        ]
        for name, title, stage, area in specs:
            if stage is None:
                dock = ImagingPlanDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            else:
                dock = StubDock(name, title, stage, self.context, self.open_settings, self.iface.mainWindow())
            dock.hide()
            self.iface.addDockWidget(area, dock)
            self.docks[name] = dock
            action = dock.toggleViewAction()
            action.setText(title)
            action.setIcon(icon)
            self.iface.addPluginToMenu(MENU, action)
            self.toolbar.addAction(action)
            self.actions.append(action)

        settings = QAction(icon, tr("設定…"), self.iface.mainWindow())
        settings.triggered.connect(self.open_settings)
        self.iface.addPluginToMenu(MENU, settings)
        self.actions.append(settings)
        self.context.reload()

    def open_settings(self) -> None:
        SettingsDialog(self.context, self.iface.mainWindow()).exec()

    def unload(self) -> None:
        for action in self.actions:
            self.iface.removePluginMenu(MENU, action)
        self.actions = []
        for dock in self.docks.values():
            if hasattr(dock, "close_down"):
                dock.close_down()
            self.iface.removeDockWidget(dock)
            dock.deleteLater()
        self.docks = {}
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        if self.context is not None:
            self.context.close()
            self.context.deleteLater()
            self.context = None
