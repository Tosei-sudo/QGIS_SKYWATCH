"""The plugin: its menu, toolbar and docks."""
from __future__ import annotations

import os
from typing import Optional

from qgis.core import QgsApplication, QgsMapLayerType, QgsProject
from qgis.PyQt.QtCore import QCoreApplication, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter
from qgis.PyQt.QtWidgets import QDockWidget, QToolButton

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from .core.obs.api import ObservationApi
from .processing_provider.pansharpen_algorithm import default_inputs
from .processing_provider.provider import SatObsProvider
from .qgis_io.catalog_io import FEATURE
from .qgis_io.context import PluginContext
from .qgis_io.geometry import to_wgs84
from .ui.basemap_switch import BaseMapSwitch
from .ui.catalog_dock import CatalogDock
from .ui.coordinate_box import CoordinateBox
from .ui.docks import StubDock
from .ui.enhance_dock import EnhanceDock
from .ui.imaging_plan_dock import ImagingPlanDock
from .ui.moving_target_dialog import MovingTargetDialog
from .ui.new_layer_dialog import NewLayerDialog, ScratchDialogHook, offered_templates
from .ui.notices_dock import NoticesDock
from .ui.observation_dock import ObservationDock
from .ui.satellite_pass_dock import SatellitePassDock
from .ui.saved_plans_dock import SavedPlansDock
from .ui.settings_dialog import SettingsDialog
from .ui.shadow_dock import ShadowHeightDock
from .ui.spectral_dock import SpectralDock
from .ui.startup import StartupLayers
from .ui.swipe import SwipeCompare
from .ui.taxonomy_dock import TaxonomyDock

HERE = os.path.dirname(__file__)
MENU = "衛星撮像計画・観測記録"
#: Where the docks start: each group docked on one side, as tabs (the first in front).
DOCK_GROUPS = [
    (Qt.DockWidgetArea.RightDockWidgetArea, ["SatObsImagingPlan", "SatObsSatellitePass", "SatObsSavedPlans"]),
    (Qt.DockWidgetArea.LeftDockWidgetArea, ["SatObsImageCatalog", "SatObsNotices", "SatObsEnhance", "SatObsShadowHeight", "SatObsSpectral"]),
    (Qt.DockWidgetArea.RightDockWidgetArea, ["SatObsObservations", "SatObsTaxonomy"]),
]
#: The docks open when the plugin starts without a saved layout (the others from the menu or toolbar).
INITIAL_DOCKS = ("SatObsImageCatalog", "SatObsImagingPlan")


def badged(icon: QIcon, n: int) -> QIcon:
    """The icon with the number of unread notices in a red circle (the icon as it is for none)."""
    if n <= 0:
        return icon
    pixmap = icon.pixmap(64, 64)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#ffffff"))
    painter.setBrush(QColor("#dc2626"))
    painter.drawEllipse(QRectF(26, 0, 38, 38))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(26 if n < 10 else 20)
    painter.setFont(font)
    painter.drawText(QRectF(26, 0, 38, 38), int(Qt.AlignmentFlag.AlignCenter), str(n) if n < 100 else "99+")
    painter.end()
    return QIcon(pixmap)


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

    @property
    def image_catalog(self) -> "CatalogDock":
        """The image catalog dock (its `search()`, `records`, `choose()`, `open_records()`, `find_similar()`, `add_layer()`)."""
        return self._plugin.docks["SatObsImageCatalog"]

    @property
    def notices(self) -> "NoticesDock":
        """The notices dock (its `store()`, `check()`, `watch_conditions()`, `watch_point()`, `open_notices()`)."""
        return self._plugin.docks["SatObsNotices"]

    @property
    def image_enhance(self) -> "EnhanceDock":
        """The image correction dock (its `enhance(layer, correction, bands)`, `pipeline()`, `set_pipeline()`, `apply()`)."""
        return self._plugin.docks["SatObsEnhance"]

    @property
    def shadow_height(self) -> "ShadowHeightDock":
        """The dock 「影から高さ」 (its `measurements`, `measure(foot, tip)`, `set_sun()`, `set_time()`, `clear()`, `add_layer()`)."""
        return self._plugin.docks["SatObsShadowHeight"]

    @property
    def coordinates(self) -> "CoordinateBox":
        """The jump field and the cursor position (its `jump(text)`, `search`, `go_to_place()`, `set_notation()`)."""
        return self._plugin.coordinates

    @property
    def spectral(self) -> "SpectralDock":
        """The dock 「スペクトル」 (its `series`, `labels`, `add_point(point)`, `add_box(rect)`, `csv_text()`, `save_csv(path)`, `clear()`)."""
        return self._plugin.docks["SatObsSpectral"]

    @property
    def swipe(self) -> "SwipeCompare":
        """「スワイプ比較」 (its `set_active()`, `set_target(layer or group)`, `set_position(0-1)`, `set_orientation()`)."""
        return self._plugin.swipe

    def new_scratch_layer(self, template: Optional[str] = None, source: str = "builtin") -> "NewLayerDialog":
        """Opens 「新規スクラッチレイヤー（テンプレート）」 (with a template: `source` builtin, config or saved); its `create()`."""
        return self._plugin.open_new_layer(source, template)


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
        self.similar_action: Optional[QAction] = None
        self.notices_action: Optional[QAction] = None
        self.startup_layers: Optional[StartupLayers] = None
        self.base_maps: Optional[BaseMapSwitch] = None
        self.coordinates: Optional[CoordinateBox] = None
        self.new_layer: Optional[NewLayerDialog] = None
        self.new_layer_action: Optional[QAction] = None
        self.provider: Optional[SatObsProvider] = None
        self.scratch_hook: Optional[ScratchDialogHook] = None
        self.swipe: Optional[SwipeCompare] = None

    def initGui(self) -> None:  # noqa: N802 (QGIS's name)
        self.context = PluginContext(self.iface.mainWindow())
        # 「座標・地名へ移動」 on its own toolbar, the cursor position in the status bar.
        self.coordinates = CoordinateBox(self.iface, self.context, self.iface.mainWindow())
        self.toolbar = self.iface.addToolBar(tr("衛星撮像計画・観測記録"))
        self.toolbar.setObjectName("SatelliteObservationToolbar")

        def icon(name: str) -> QIcon:
            return QIcon(os.path.join(HERE, "resources", f"{name}.svg"))

        #: Each feature's own icon (the plugin's icon.svg stays for the plugin manager).
        icons = {"SatObsImagingPlan": "imaging_plan", "SatObsSavedPlans": "saved_plans", "SatObsSatellitePass": "satellite_pass",
                 "SatObsObservations": "observations", "SatObsTaxonomy": "taxonomy",
                 "SatObsImageCatalog": "catalog", "SatObsNotices": "notices", "SatObsEnhance": "enhance",
                 "SatObsShadowHeight": "shadow_height", "SatObsSpectral": "spectral"}

        specs = [
            ("SatObsImageCatalog", tr("画像カタログ"), None),
            ("SatObsImagingPlan", tr("撮像計画"), None),
            ("SatObsSavedPlans", tr("保存した撮像計画"), None),
            ("SatObsSatellitePass", tr("衛星パス"), None),
            ("SatObsObservations", tr("観測記録"), None),
            ("SatObsTaxonomy", tr("分類体系の編集"), None),
            ("SatObsNotices", tr("通知"), None),
            ("SatObsEnhance", tr("画像補正"), None),
            ("SatObsShadowHeight", tr("影から高さ"), None),
            ("SatObsSpectral", tr("スペクトル"), None),
        ]
        area = {name: a for a, names in DOCK_GROUPS for name in names}
        for name, title, stage in specs:
            if name == "SatObsImagingPlan":
                dock = ImagingPlanDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsObservations":
                dock = ObservationDock(self.iface, self.context, self.open_settings, self.iface.mainWindow(), edit_taxonomy=self.edit_taxonomy)
            elif name == "SatObsSatellitePass":
                dock = SatellitePassDock(self.iface, self.context, self.open_settings, self.docks["SatObsImagingPlan"], self.iface.mainWindow())
            elif name == "SatObsTaxonomy":
                dock = TaxonomyDock(self.iface, self.context, self.open_settings, self.docks["SatObsObservations"], self.iface.mainWindow())
            elif name == "SatObsImageCatalog":
                dock = CatalogDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsNotices":
                dock = NoticesDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsEnhance":
                dock = EnhanceDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsShadowHeight":
                dock = ShadowHeightDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsSpectral":
                dock = SpectralDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsSavedPlans":
                dock = SavedPlansDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            else:
                dock = StubDock(name, title, stage, self.context, self.open_settings, self.iface.mainWindow())
            # QGIS shows a dock when it is added: only the first ones stay open (QGIS then restores the layout it saved).
            # Its signals wait meanwhile, so a dock shown only for that moment reads nothing.
            dock.blockSignals(True)
            self.iface.addDockWidget(area[name], dock)
            dock.setVisible(name in INITIAL_DOCKS)
            dock.blockSignals(False)
            self.docks[name] = dock
            action = dock.toggleViewAction()
            action.setText(title)
            action.setIcon(icon(icons[name]))
            self.iface.addPluginToMenu(MENU, action)
            self.toolbar.addAction(action)
            self.actions.append(action)

        self.place_docks()
        # The docks left open now hear that they are shown (their signals waited while they were added).
        for name in INITIAL_DOCKS:
            self.docks[name].visibilityChanged.emit(True)
        self.docks["SatObsImagingPlan"].open_saved = self.docks["SatObsSavedPlans"].open
        self._wire_catalog()
        moving = QAction(icon("moving_targets"), tr("移動目標撮像計画…"), self.iface.mainWindow())
        moving.triggered.connect(lambda: self.open_moving_targets())
        self.iface.addPluginToMenu(MENU, moving)
        self.toolbar.addAction(moving)
        self.actions.append(moving)
        self._add_new_layer(icon("new_layer"))
        # 「パンシャープン…」: the Processing algorithm's dialog (the algorithm also in the Processing toolbox).
        self.provider = SatObsProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)
        pansharpen = QAction(icon("pansharpen"), tr("パンシャープン…"), self.iface.mainWindow())
        pansharpen.triggered.connect(lambda: self.open_pansharpen())
        self.iface.addPluginToMenu(MENU, pansharpen)
        self.actions.append(pansharpen)
        change = QAction(icon("change"), tr("変化抽出（2 時期）…"), self.iface.mainWindow())
        change.triggered.connect(lambda: self.open_change())
        self.iface.addPluginToMenu(MENU, change)
        self.actions.append(change)
        align = QAction(icon("align"), tr("自動位置合わせ…"), self.iface.mainWindow())
        align.triggered.connect(lambda: self.open_align())
        self.iface.addPluginToMenu(MENU, align)
        self.actions.append(align)
        classify = QAction(icon("classify"), tr("土地被覆分類…"), self.iface.mainWindow())
        classify.triggered.connect(lambda: self.open_classify())
        self.iface.addPluginToMenu(MENU, classify)
        self.actions.append(classify)
        ortho = QAction(icon("simple_ortho"), tr("簡易オルソ補正…"), self.iface.mainWindow())
        ortho.triggered.connect(lambda: self.open_simple_ortho())
        self.iface.addPluginToMenu(MENU, ortho)
        self.actions.append(ortho)
        # The settings file's `layers`: added to an empty project at start, or from the menu.
        self.startup_layers = StartupLayers(self.iface, self.context, self.iface.mainWindow())
        add_layers = QAction(icon("startup_layers"), tr("設定ファイルのレイヤーを追加"), self.iface.mainWindow())
        add_layers.triggered.connect(lambda: self.startup_layers.add())
        self.iface.addPluginToMenu(MENU, add_layers)
        self.actions.append(add_layers)
        # 「背景地図」: the toolbar button opens the list; in the menu, a submenu.
        base_maps = QAction(icon("base_map"), tr("背景地図"), self.iface.mainWindow())
        self.base_maps = BaseMapSwitch(self.iface, self.context, base_maps, self.iface.mainWindow())
        self.iface.addPluginToMenu(MENU, base_maps)
        self.toolbar.addAction(base_maps)
        button = self.toolbar.widgetForAction(base_maps)
        if isinstance(button, QToolButton):
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.actions.append(base_maps)
        # 「スワイプ比較」: the active layer (or group) on one side of a line only.
        swipe = QAction(icon("swipe"), tr("スワイプ比較"), self.iface.mainWindow())
        swipe.setObjectName("SatObsSwipe")
        self.swipe = SwipeCompare(self.iface, swipe, self.iface.mainWindow())
        self.iface.addPluginToMenu(MENU, swipe)
        self.toolbar.addAction(swipe)
        self.actions.append(swipe)
        home = QAction(icon("dock_layout"), tr("ドックを初期位置に戻す"), self.iface.mainWindow())
        home.triggered.connect(lambda: self.place_docks())
        self.iface.addPluginToMenu(MENU, home)
        self.actions.append(home)
        settings = QAction(icon("settings"), tr("設定…"), self.iface.mainWindow())
        settings.triggered.connect(self.open_settings)
        self.iface.addPluginToMenu(MENU, settings)
        self.actions.append(settings)
        # 「ここを撮像計画」 in the map's right-click menu.
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)
        self.context.reload()

    def _add_new_layer(self, new_icon: QIcon) -> None:
        """「新規スクラッチレイヤー（テンプレート）…」: in the plugin's menu, and in Layer ▸ Create Layer after QGIS's own scratch layer."""
        action = QAction(new_icon, tr("新規スクラッチレイヤー（テンプレート）…"), self.iface.mainWindow())
        action.setObjectName("SatObsNewScratchLayer")
        action.triggered.connect(lambda: self.open_new_layer())
        self.iface.addPluginToMenu(MENU, action)
        self.actions.append(action)
        self.new_layer_action = action
        menu = self.iface.newLayerMenu() if hasattr(self.iface, "newLayerMenu") else None
        if menu is not None:
            actions = menu.actions()
            native = next((a for a in actions if a.objectName() == "mActionNewMemoryLayer"), None)
            after = actions[actions.index(native) + 1] if native is not None and actions.index(native) + 1 < len(actions) else None
            menu.insertAction(after, action) if after is not None else menu.addAction(action)
        self.scratch_hook = ScratchDialogHook(self.open_new_layer, self._templates, self.iface.mainWindow())

    def _templates(self) -> list:
        return offered_templates(self.context.config.layer_templates)

    def open_new_layer(self, source: str = "builtin", template: Optional[str] = None) -> NewLayerDialog:
        """Opens 「新規スクラッチレイヤー（テンプレート）」 (once; kept while the plugin is loaded)."""
        if self.new_layer is None:
            self.new_layer = NewLayerDialog(self.iface, lambda: self.context.config.layer_templates, self.iface.mainWindow())
        return self.new_layer.open_with(source, template)

    def place_docks(self) -> None:
        """
        Docks every dock where it starts (DOCK_GROUPS): each group on its side
        as tabs, none floating. Open ones stay open, the group's first open one in front.
        """
        window = self.iface.mainWindow()
        for area, names in DOCK_GROUPS:
            docks = [self.docks[n] for n in names if n in self.docks]
            shown = [d for d in docks if d.isVisible()]
            for dock in docks:
                dock.setFloating(False)
                window.addDockWidget(area, dock)
            for dock in docks[1:]:
                window.tabifyDockWidget(docks[0], dock)
            if shown:
                shown[0].raise_()

    def _canvas_menu(self, menu, event) -> None:
        canvas = self.iface.mapCanvas()
        point = event.mapPoint()
        action = menu.addAction(tr("ここを撮像計画"))

        def plan() -> None:
            p = to_wgs84(canvas.mapSettings().destinationCrs()).transform(point)
            self.docks["SatObsImagingPlan"].plan_at(p.x(), p.y())

        action.triggered.connect(plan)
        watch = menu.addAction(tr("この地点の新着画像を監視…"))

        def watch_here() -> None:
            p = to_wgs84(canvas.mapSettings().destinationCrs()).transform(point)
            self.docks["SatObsNotices"].watch_point(p.x(), p.y())

        watch.triggered.connect(watch_here)

    def _wire_catalog(self) -> None:
        """The image catalog with the notices, the imaging plan's reminders and the layers' right-click menu."""
        catalog, notices = self.docks["SatObsImageCatalog"], self.docks["SatObsNotices"]
        catalog.watch_requested = notices.watch_conditions
        notices.choose_folder = catalog.choose_folder
        self.notices_action = notices.toggleViewAction()

        bell = self.notices_action.icon()

        def unread(n: int) -> None:
            self.notices_action.setText(tr("通知") + (f"（{n}）" if n else ""))
            self.notices_action.setIcon(badged(bell, n))

        notices.unread_changed = unread
        self.docks["SatObsImagingPlan"].on_reminder = notices.add_plan_notice
        # 「類似画像の検索…」 in the right-click menu of layers opened from the catalog.
        self.similar_action = QAction(QIcon(os.path.join(HERE, "resources", "catalog.svg")), tr("類似画像の検索…"), self.iface.mainWindow())
        self.similar_action.triggered.connect(lambda: catalog.find_similar_of_layer(self.iface.activeLayer()))
        self.iface.addCustomActionForLayerType(self.similar_action, "", QgsMapLayerType.RasterLayer, False)
        QgsProject.instance().layersAdded.connect(self._layers_added)
        self._layers_added(QgsProject.instance().mapLayers().values())

    def _layers_added(self, layers) -> None:
        for layer in layers:
            if layer.customProperty(FEATURE):
                self.iface.addCustomActionForLayer(self.similar_action, layer)

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

    def open_pansharpen(self) -> None:
        """The algorithm's dialog, with the finest one-band image as pan and a multi-band one as ms."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("satobs:pansharpen", default_inputs(layers, self.iface.activeLayer()))

    def open_change(self) -> None:
        """The algorithm's dialog, with the two earliest images (else the lowest two in the list), the earlier as before."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.change_algorithm import default_inputs as change_inputs
        from .qgis_io.obs_images import observed_image

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("satobs:change", change_inputs(layers, lambda layer: observed_image(layer).time))

    def open_align(self) -> None:
        """The algorithm's dialog, with the active image to align and the finest other as the reference."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.align_algorithm import default_inputs as align_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("satobs:align", align_inputs(layers, self.iface.activeLayer()))

    def open_classify(self) -> None:
        """The algorithm's dialog, with the active image and a polygon layer for the training."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.classify_algorithm import default_inputs as classify_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("satobs:classify", classify_inputs(layers, self.iface.activeLayer()))

    def open_simple_ortho(self) -> None:
        """The algorithm's dialog, with the active image and a DEM-like layer."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.ortho_algorithm import default_inputs as ortho_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("satobs:simple_ortho", ortho_inputs(layers, self.iface.activeLayer()))

    def open_settings(self) -> None:
        SettingsDialog(self.context, self.iface.mainWindow()).exec()

    def unload(self) -> None:
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        try:
            self.iface.mapCanvas().contextMenuAboutToShow.disconnect(self._canvas_menu)
        except (TypeError, RuntimeError):
            pass
        if getattr(self, "similar_action", None) is not None:
            try:
                QgsProject.instance().layersAdded.disconnect(self._layers_added)
            except (TypeError, RuntimeError):
                pass
            self.iface.removeCustomActionForLayerType(self.similar_action)
            self.similar_action = None
        if self.swipe is not None:
            self.swipe.close()
            self.swipe.deleteLater()
            self.swipe = None
        if self.base_maps is not None:
            self.base_maps.close()
            self.base_maps = None
        if self.scratch_hook is not None:
            self.scratch_hook.close_down()
            self.scratch_hook.deleteLater()
            self.scratch_hook = None
        if self.new_layer_action is not None and hasattr(self.iface, "newLayerMenu") and self.iface.newLayerMenu() is not None:
            self.iface.newLayerMenu().removeAction(self.new_layer_action)
        self.new_layer_action = None
        if self.new_layer is not None:
            self.new_layer.close()
            self.new_layer.deleteLater()
            self.new_layer = None
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
        if self.startup_layers is not None:
            self.startup_layers.close()
            self.startup_layers.deleteLater()
            self.startup_layers = None
        if self.coordinates is not None:
            self.coordinates.close()
            self.coordinates.deleteLater()
            self.coordinates = None
        if self.context is not None:
            self.context.close()
            self.context.deleteLater()
            self.context = None
