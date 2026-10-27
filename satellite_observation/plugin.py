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

from .core.obs.api import ObservationApi
from .qgis_io.context import PluginContext
from .qgis_io.geometry import to_wgs84
from .ui.docks import StubDock
from .ui.imaging_plan_dock import ImagingPlanDock
from .ui.moving_target_dialog import MovingTargetDialog
from .ui.observation_dock import ObservationDock
from .ui.satellite_pass_dock import SatellitePassDock
from .ui.saved_plans_dock import SavedPlansDock
from .ui.settings_dialog import SettingsDialog
from .ui.taxonomy_dock import TaxonomyDock

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
    def observation_dock(self) -> "ObservationDock":
        """The observations dock (its `book`, `save_csv(path)`, `to_layers()`, `set_observer(name)`…)."""
        return self._plugin.docks["SatObsObservations"]

    @property
    def imaging_plan(self) -> "ImagingPlanDock":
        """The imaging plan dock (its `plan`, `shown`, `run()`, `set_point()`, `csv_text()`, `add_layer()`)."""
        return self._plugin.docks["SatObsImagingPlan"]

    def moving_targets(self, layer=None) -> "MovingTargetDialog":
        """Opens 「移動目標撮像計画」 for a point layer (or the active one); its `targets()`, `add_places()`, `plan()`."""
        return self._plugin.open_moving_targets(layer)

    @property
    def satellite_pass(self) -> "SatellitePassDock":
        """The satellite pass dock (its `tracks`, `choose()`, `set_time()`, `draw()`, `add_layer()`)."""
        return self._plugin.docks["SatObsSatellitePass"]

    @property
    def saved_plans(self) -> "SavedPlansDock":
        """The saved plans dock (its `plans`, `shots`, `reload()`, `choose()`, `track()`)."""
        return self._plugin.docks["SatObsSavedPlans"]


def _observation_function(name: str):
    """A function of the observations for scripts (core.obs.api.ObservationApi), on the dock's chosen store."""
    def call(self, *args, **kwargs):
        return getattr(self._plugin.docks["SatObsObservations"].api(), name)(*args, **kwargs)

    call.__name__ = name
    call.__doc__ = getattr(ObservationApi, name).__doc__
    return call


#: As the viewer's scripts have them (viewer.observations → api.observations, viewer.addObservation → api.add_observation…).
OBSERVATION_FUNCTIONS = [
    "observations", "observed_things", "observation_images", "observe_image", "add_observation", "update_observation",
    "rename_observed_thing", "delete_observations", "observation_candidates", "confidences", "checked_areas", "add_checked_area",
    "delete_checked_areas", "observation_report",
]
for _name in OBSERVATION_FUNCTIONS:
    setattr(PluginApi, _name, _observation_function(_name))


class SatelliteObservationPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.context: Optional[PluginContext] = None
        self.actions: list[QAction] = []
        self.docks: dict[str, QDockWidget] = {}
        self.toolbar = None
        self.moving_targets: Optional[MovingTargetDialog] = None
        self.api = PluginApi(self)

    def initGui(self) -> None:  # noqa: N802 (QGIS's name)
        self.context = PluginContext(self.iface.mainWindow())
        self.toolbar = self.iface.addToolBar(tr("衛星撮像計画・観測記録"))
        self.toolbar.setObjectName("SatelliteObservationToolbar")

        def icon(name: str) -> QIcon:
            return QIcon(os.path.join(HERE, "resources", f"{name}.svg"))

        #: Each feature's own icon (the plugin's icon.svg stays for the plugin manager).
        icons = {"SatObsImagingPlan": "imaging_plan", "SatObsSavedPlans": "saved_plans", "SatObsSatellitePass": "satellite_pass",
                 "SatObsObservations": "observations", "SatObsTaxonomy": "taxonomy"}

        specs = [
            ("SatObsImagingPlan", tr("撮像計画"), None, Qt.DockWidgetArea.RightDockWidgetArea),
            ("SatObsSavedPlans", tr("保存した撮像計画"), None, Qt.DockWidgetArea.RightDockWidgetArea),
            ("SatObsSatellitePass", tr("衛星パス"), None, Qt.DockWidgetArea.RightDockWidgetArea),
            ("SatObsObservations", tr("観測記録"), None, Qt.DockWidgetArea.RightDockWidgetArea),
            ("SatObsTaxonomy", tr("分類体系の編集"), None, Qt.DockWidgetArea.LeftDockWidgetArea),
        ]
        for name, title, stage, area in specs:
            if name == "SatObsImagingPlan":
                dock = ImagingPlanDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsObservations":
                dock = ObservationDock(self.iface, self.context, self.open_settings, self.iface.mainWindow(), edit_taxonomy=self.edit_taxonomy)
            elif name == "SatObsSatellitePass":
                dock = SatellitePassDock(self.iface, self.context, self.open_settings, self.docks["SatObsImagingPlan"], self.iface.mainWindow())
            elif name == "SatObsTaxonomy":
                dock = TaxonomyDock(self.iface, self.context, self.open_settings, self.docks["SatObsObservations"], self.iface.mainWindow())
            elif name == "SatObsSavedPlans":
                dock = SavedPlansDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            else:
                dock = StubDock(name, title, stage, self.context, self.open_settings, self.iface.mainWindow())
            dock.hide()
            self.iface.addDockWidget(area, dock)
            self.docks[name] = dock
            action = dock.toggleViewAction()
            action.setText(title)
            action.setIcon(icon(icons[name]))
            self.iface.addPluginToMenu(MENU, action)
            self.toolbar.addAction(action)
            self.actions.append(action)

        self.docks["SatObsImagingPlan"].open_saved = self.docks["SatObsSavedPlans"].open
        moving = QAction(icon("moving_targets"), tr("移動目標撮像計画…"), self.iface.mainWindow())
        moving.triggered.connect(lambda: self.open_moving_targets())
        self.iface.addPluginToMenu(MENU, moving)
        self.toolbar.addAction(moving)
        self.actions.append(moving)
        settings = QAction(icon("settings"), tr("設定…"), self.iface.mainWindow())
        settings.triggered.connect(self.open_settings)
        self.iface.addPluginToMenu(MENU, settings)
        self.actions.append(settings)
        # 「ここを撮像計画」 in the map's right-click menu.
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)
        self.context.reload()

    def _canvas_menu(self, menu, event) -> None:
        canvas = self.iface.mapCanvas()
        point = event.mapPoint()
        action = menu.addAction(tr("ここを撮像計画"))

        def plan() -> None:
            p = to_wgs84(canvas.mapSettings().destinationCrs()).transform(point)
            self.docks["SatObsImagingPlan"].plan_at(p.x(), p.y())

        action.triggered.connect(plan)

    def edit_taxonomy(self, path=None) -> None:
        """Opens the classification editor (at a class)."""
        dock = self.docks.get("SatObsTaxonomy")
        if dock is None:
            return
        dock.show()
        dock.raise_()
        if hasattr(dock, "select"):
            dock.select(path)

    def open_moving_targets(self, layer=None) -> MovingTargetDialog:
        """Opens 「移動目標撮像計画」 (once; kept while the plugin is loaded)."""
        if self.moving_targets is None:
            self.moving_targets = MovingTargetDialog(self.iface, self.docks["SatObsImagingPlan"].plan_moving, self.iface.mainWindow())
        self.moving_targets.open_for(layer)
        return self.moving_targets

    def open_settings(self) -> None:
        SettingsDialog(self.context, self.iface.mainWindow()).exec()

    def unload(self) -> None:
        try:
            self.iface.mapCanvas().contextMenuAboutToShow.disconnect(self._canvas_menu)
        except (TypeError, RuntimeError):
            pass
        for action in self.actions:
            self.iface.removePluginMenu(MENU, action)
        self.actions = []
        for dock in self.docks.values():
            if hasattr(dock, "close_down"):
                dock.close_down()
            self.iface.removeDockWidget(dock)
            dock.deleteLater()
        self.docks = {}
        if self.moving_targets is not None:
            self.moving_targets.close()
            self.moving_targets.deleteLater()
            self.moving_targets = None
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        if self.context is not None:
            self.context.close()
            self.context.deleteLater()
            self.context = None
