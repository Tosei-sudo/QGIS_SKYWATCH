"""The plugin: its menu, toolbar and docks."""
from __future__ import annotations

import os
from typing import Optional

from qgis.core import Qgis, QgsApplication, QgsMapLayerType, QgsMessageLog, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QCursor, QIcon, QPainter
from qgis.PyQt.QtWidgets import QDockWidget, QMenu, QPushButton

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from .core.obs.api import ObservationApi
from .processing_provider.pansharpen_algorithm import default_inputs
from .processing_provider import analysis_algorithm, cloud_algorithm, detect_algorithm, train_algorithm
from .processing_provider.provider import SatObsProvider
from .qgis_io.catalog_io import FEATURE
from .qgis_io import edit_stamp_layers, expression_functions, time_zone_fields
from .qgis_io.context import PluginContext
from .qgis_io.preload import LOG_TAG, Preloader
from .core.preload import ITEMS as PRELOAD_ITEMS, StartupClock
from .core.enhance.rules import image_rule_for
from .qgis_io import complex_sar, overviews_io, tasks
from .qgis_io.geometry import to_wgs84
from .ui.ai_results_dock import AiResultsDock
from .ui.basemap_switch import BaseMapSwitch
from .ui.catalog_dock import CatalogDock
from .ui.cloud_dock import CloudDock
from .ui.coordinate_box import CoordinateBox
from .ui.dashboard_dock import DashboardDock
from .ui.docks import StubDock, plain_tip
from .ui.dock_layouts import DockLayoutMenu, float_docks
from .ui import first_setup
from .ui.gp_dialog import RemoteProcessingDialog
from .ui.enhance_dock import EnhanceDock
from .ui.events_dock import EventsDock
from .ui.analysis_dock import AnalysisDock
from .ui.imaging_plan_dock import ImagingPlanDock
from .ui.moving_target_dialog import MovingTargetDialog
from .ui.new_layer_dialog import NewLayerDialog, ScratchDialogHook, offered_templates
from .ui.notices_dock import NoticesDock
from .ui.offline_status import OfflineController
from .ui.observation_dock import ObservationDock
from .ui.satellite_pass_dock import SatellitePassDock
from .ui.segment_tool import SegmentTool
from .ui.transfer_dialog import TransferDialog
from .ui.plan_file_dock import PlanFileDock
from .ui.request_dock import RequestDock
from .ui.schedule_dock import ScheduleDock
from .ui.settings_dialog import SettingsDialog
from .ui.shadow_dock import ShadowHeightDock
from .ui.spectral_dock import SpectralDock
from .ui.startup import StartupLayers
from .ui.moving_dock import MovingDock
from .ui.shift import LayerShift
from .ui.shortcuts import Shortcuts
from .ui.swipe import SwipeCompare
from .ui.taxonomy_dock import TaxonomyDock
from .ui.toolbar_buttons import ToolbarButtons, ToolbarButtonsDialog
from .ui.webmap_dialog import WebMapDialogs

HERE = os.path.dirname(__file__)
MENU = "SKYWATCH"
#: The plugin's menu by area (tsudo, 2026-11-07): each group a submenu, its entries by their text in this order
#: (None: a separator). The entries after the groups stay in the menu itself, 「設定…」 last.
MENU_GROUPS = [
    ("撮像計画", ["撮像計画", "衛星パス", "移動目標撮像計画…", None, "雲量マップ", "雲量を属性に付与…"]),
    ("撮像依頼", ["撮像依頼", "撮像計画ファイル出力", "撮像予定"]),
    ("観測記録", ["観測記録", "分類体系の編集", "通報文の作成…", "通報記録の確認…", "観測記録のエクスポート…", "観測記録の一括取込み…", "分担…", "引き継ぎメモ…", "移動体（AIS・ADS-B）", "ダッシュボード"]),
    ("画像", ["画像カタログ", "通知", None, "画像補正", "影から高さ", "スペクトル", None, "スワイプ比較", "位置ずらし", "位置ずらしの解除"]),
    ("解析・処理", ["データ分析", None, "パンシャープン…", "変化抽出（2 時期）…", "自動位置合わせ…", "土地被覆分類…", "簡易オルソ補正…", None,
                "測地線バッファ…", "正距方位での重心…", "移動体と検出の照合…", None, "サーバ処理…"]),
    ("AI", ["物体検出（AI）…", "クリック抽出（AI）", "AI結果", "検出結果を DB へ転記…", None, "教師データセットの作成…", "アノテーション変換（点→矩形）…", "アノテーション変換（教師データセット）…",
            "AI 学習（物体検出）…"]),
    ("地図・レイヤー", ["背景地図", "設定ファイルのレイヤーを追加", "新規スクラッチレイヤー（テンプレート）…", None,
                   "ArcGIS Web マップを開く…", "ArcGIS Web マップとして保存…"]),
]
#: In the menu itself, after the groups (behind a separator).
MENU_TOP = ["イベント", "担当と個人設定…", "ツールバーのボタン…", "ドックのレイアウト", "ドックを初期位置に戻す", "接続と表の点検…", "使い方（ヘルプ）…", "Python API リファレンス…", "設定…"]
#: Where the docks start: each group docked on one side, as tabs (the first in front).
DOCK_GROUPS = [
    (Qt.DockWidgetArea.RightDockWidgetArea, ["SatObsImagingPlan", "SatObsSatellitePass", "SatObsRequests", "SatObsPlanFiles",
                                             "SatObsSchedule", "SatObsCloud"]),
    (Qt.DockWidgetArea.LeftDockWidgetArea, ["SatObsImageCatalog", "SatObsNotices", "SatObsEnhance", "SatObsShadowHeight", "SatObsSpectral", "SatObsDashboard", "SatObsAnalysis"]),
    (Qt.DockWidgetArea.RightDockWidgetArea, ["SatObsObservations", "SatObsTaxonomy", "SatObsMoving", "SatObsAiResults", "SatObsEvents"]),
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
    return QCoreApplication.translate("Skywatch", text)


class PluginApi:
    """The plugin's Python API (`plugin.api`), for the Python console and Processing scripts; filled stage by stage."""

    def __init__(self, plugin: "SkywatchPlugin"):
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
    def startup_times(self) -> dict:
        """How long the startup took (`steps`, `total`) and each read ahead (`reads`), in seconds."""
        return self._plugin.clock.times()

    def preload(self) -> None:
        """Reads ahead now what the settings file's `preload` lists (in the background)."""
        if self._plugin.preloader is not None:
            self._plugin.preloader.run()

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
    def requests(self) -> "RequestDock":
        """The imaging requests dock (its `requests`, `reload()`, `edit()`, `import_layer()`, `export_layer()`)."""
        return self._plugin.docks["SatObsRequests"]

    @property
    def plan_files(self) -> "PlanFileDock":
        """The imaging plan files dock (its `run()`, `plan`, `write()`, `paths`)."""
        return self._plugin.docks["SatObsPlanFiles"]

    @property
    def schedule(self) -> "ScheduleDock":
        """The imaging schedule dock: a day's plan as the systems answered (its `rows`, `set_day()`, `reload()`, `answer()`,
        `import_file()`, `to_layer()`)."""
        return self._plugin.docks["SatObsSchedule"]

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
    def dashboard(self) -> "DashboardDock":
        """The dock 「ダッシュボード」 (its `figures`, `categories`, `click_value(v)`, `click_bin(i)`, `click_step(i)`, `set_heatmap(on)`, `clear_filter()`)."""
        return self._plugin.docks["SatObsDashboard"]

    @property
    def moving(self) -> "MovingDock":
        """The dock 「移動体（AIS・ADS-B）」 (its `connect_feed(FeedConfig)`, `open_history(path)`, `layers`, `align_to_image()`, `go_live()`)."""
        return self._plugin.docks["SatObsMoving"]

    @property
    def events(self) -> "EventsDock":
        """The dock 「イベント」 (its `events`, `links`, `reload()`, `link_items(event_ids, items)`, `choose(id)`)."""
        return self._plugin.docks["SatObsEvents"]

    @property
    def analysis(self) -> "AnalysisDock":
        """The dock 「データ分析」 (its `tables`, `set_sql(text)`, `set_value(name, v)`, `run(then)`, `result`, `save(name)`, `load(name)`, `add_layer()`)."""
        return self._plugin.docks["SatObsAnalysis"]

    @property
    def ai_results(self) -> "AiResultsDock":
        """The dock 「AI結果」 (its `load(image_id)`, `rows`, `selected_rows()`, `import_rows(rows)`, `zoom_to(rows)`)."""
        return self._plugin.docks["SatObsAiResults"]

    @property
    def swipe(self) -> "SwipeCompare":
        """「スワイプ比較」 (its `set_active()`, `set_target(layer or group)`, `set_position(0-1)`, `set_orientation()`)."""
        return self._plugin.swipe

    @property
    def shift(self) -> "LayerShift":
        """「位置ずらし」 (its `set_active()`, `set_shift_metres(layer, east, north)`, `shift_metres(layer)`, `reset()`)."""
        return self._plugin.shift

    @property
    def segment(self) -> "SegmentTool":
        """「クリック抽出（AI）」 (its `set_active()`, `add_model_files([encoder, decoder])`, `click(point)`, `undo()`, `keep()`, `outlines()`)."""
        return self._plugin.segment

    def new_scratch_layer(self, template: Optional[str] = None, source: str = "builtin") -> "NewLayerDialog":
        """Opens 「新規スクラッチレイヤー（テンプレート）」 (with a template: `source` builtin, config or saved); its `create()`."""
        return self._plugin.open_new_layer(source, template)


def _observation_function(name: str):
    """A function of the observations for scripts (core.obs.api.ObservationApi), on the dock's chosen store."""
    def call(self, *args, **kwargs):
        return getattr(self._plugin.docks["SatObsObservations"].api(), name)(*args, **kwargs)

    call.__name__ = name
    call.__doc__ = getattr(ObservationApi, name).__doc__
    # Its arguments as ObservationApi has them (inspect.signature, the API reference).
    call.__wrapped__ = getattr(ObservationApi, name)
    return call


#: As the viewer's scripts have them (viewer.observations → api.observations, viewer.addObservation → api.add_observation…).
OBSERVATION_FUNCTIONS = [
    "observations", "observed_things", "observation_images", "observe_image", "add_observation", "update_observation",
    "rename_observed_thing", "delete_observations", "clear_observations", "observation_candidates", "confidences", "checked_areas", "add_checked_area",
    "delete_checked_areas", "observation_report",
]
for _name in OBSERVATION_FUNCTIONS:
    setattr(PluginApi, _name, _observation_function(_name))


class SkywatchPlugin:
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
        self.webmaps: Optional[WebMapDialogs] = None
        self.remote_dialog: Optional[RemoteProcessingDialog] = None
        self.coordinates: Optional[CoordinateBox] = None
        self.new_layer: Optional[NewLayerDialog] = None
        self.new_layer_action: Optional[QAction] = None
        self.provider: Optional[SatObsProvider] = None
        self.scratch_hook: Optional[ScratchDialogHook] = None
        self.swipe: Optional[SwipeCompare] = None
        self.shift: Optional[LayerShift] = None
        self.segment: Optional[SegmentTool] = None
        self.transfer_dialog: Optional[TransferDialog] = None
        self.offline: Optional[OfflineController] = None
        #: The menu's entries as added, its submenus (MENU_GROUPS) and what sits in the menu itself.
        self._menu_entries: list[QAction] = []
        self._submenus: list[QMenu] = []
        self._menu_top: list[QAction] = []
        self._menu_separator: Optional[QAction] = None
        self.setup_dialog: Optional[first_setup.FirstSetupDialog] = None
        self.toolbar_buttons: Optional[ToolbarButtons] = None
        self.toolbar_dialog: Optional[ToolbarButtonsDialog] = None
        #: How long the startup took (classFactory gives the one started before the plugin was imported).
        self.clock = StartupClock()
        #: 「先読み」: the Esri tables read in the background once QGIS has started (qgis_io/preload.py).
        self.preloader: Optional[Preloader] = None
        self._qgis_started = False
        self._startup_told = False

    def initGui(self) -> None:  # noqa: N802 (QGIS's name)
        # 「最初の設定」 only the first time in this profile (asked before anything is written; not after an update).
        self.clock.lap()
        # The tasks' Python hands the GIL to QGIS's main thread sooner (qgis_io/tasks.py): the map and docks keep moving.
        tasks.share_gil_faster()
        self._first_start = first_setup.is_first_start()
        self.context = PluginContext(self.iface.mainWindow())
        # 一時的なオフライン作業: the status-bar button, the checks while a server is down, and the question when it is back.
        self.offline = OfflineController(self.iface, self.context, self._reload_after_offline, self.iface.mainWindow())
        self.preloader = Preloader(self.context, self.clock, self.iface.mainWindow())
        self.context.configChanged.connect(self._overviews_changed)
        # The docks no longer show the settings file's state: problems go to the message bar, once per file and list.
        self._problems_shown: Optional[tuple] = None
        self.context.configChanged.connect(self._config_problems)
        # Images added in any way get overviews too (`overviews.scope` "all"), once the settings file is read;
        # complex SAR images are shown as amplitude first (`complexImages`). Before the docks' own layersAdded (imageRules).
        self._overviews_ready = False
        self._overviews_waiting: list = []
        QgsProject.instance().layersAdded.connect(self._overview_layers_added)
        # 「座標・地名へ移動」 on its own toolbar, the cursor position in the status bar.
        self.coordinates = CoordinateBox(self.iface, self.context, self.iface.mainWindow())
        self.toolbar = self.iface.addToolBar(tr("SKYWATCH"))
        self.toolbar.setObjectName("SkywatchToolbar")

        def icon(name: str) -> QIcon:
            return QIcon(os.path.join(HERE, "resources", f"{name}.svg"))

        #: Each feature's own icon (the plugin's icon.svg stays for the plugin manager).
        icons = {"SatObsImagingPlan": "imaging_plan", "SatObsSatellitePass": "satellite_pass",
                 "SatObsObservations": "observations", "SatObsTaxonomy": "taxonomy",
                 "SatObsImageCatalog": "catalog", "SatObsNotices": "notices", "SatObsEnhance": "enhance",
                 "SatObsShadowHeight": "shadow_height", "SatObsSpectral": "spectral", "SatObsMoving": "moving",
                 "SatObsDashboard": "dashboard", "SatObsAiResults": "ai_results", "SatObsRequests": "requests",
                 "SatObsPlanFiles": "plan_files", "SatObsSchedule": "schedule", "SatObsEvents": "events", "SatObsCloud": "cloud_map",
                 "SatObsAnalysis": "analysis"}

        specs = [
            ("SatObsImageCatalog", tr("画像カタログ"), None),
            ("SatObsImagingPlan", tr("撮像計画"), None),
            ("SatObsRequests", tr("撮像依頼"), None),
            ("SatObsPlanFiles", tr("撮像計画ファイル出力"), None),
            ("SatObsSchedule", tr("撮像予定"), None),
            ("SatObsSatellitePass", tr("衛星パス"), None),
            ("SatObsCloud", tr("雲量マップ"), None),
            ("SatObsObservations", tr("観測記録"), None),
            ("SatObsTaxonomy", tr("分類体系の編集"), None),
            ("SatObsNotices", tr("通知"), None),
            ("SatObsEnhance", tr("画像補正"), None),
            ("SatObsShadowHeight", tr("影から高さ"), None),
            ("SatObsSpectral", tr("スペクトル"), None),
            ("SatObsMoving", tr("移動体（AIS・ADS-B）"), None),
            ("SatObsDashboard", tr("ダッシュボード"), None),
            ("SatObsAiResults", tr("AI結果"), None),
            ("SatObsEvents", tr("イベント"), None),
            ("SatObsAnalysis", tr("データ分析"), None),
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
            elif name == "SatObsMoving":
                dock = MovingDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsDashboard":
                dock = DashboardDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsAiResults":
                dock = AiResultsDock(self.iface, self.context, self.open_settings, lambda: self.docks.get("SatObsObservations"),
                                     self.iface.mainWindow())
            elif name == "SatObsEvents":
                dock = EventsDock(self.iface, self.context, self.open_settings, lambda: self.docks, self.iface.mainWindow())
            elif name == "SatObsAnalysis":
                dock = AnalysisDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsCloud":
                dock = CloudDock(self.iface, self.context, self.open_settings, lambda: self.open_cloud_cover(), self.iface.mainWindow())
            elif name == "SatObsRequests":
                plan_dock = self.docks["SatObsImagingPlan"]

                def satellites(then, failed=None, plan_dock=plan_dock):
                    plan_dock.load_catalog(lambda: then([*plan_dock.catalog_sats]), failed)

                dock = RequestDock(self.iface, self.context, self.open_settings, satellites, self.iface.mainWindow())
            elif name == "SatObsSchedule":
                dock = ScheduleDock(self.iface, self.context, self.open_settings, self.iface.mainWindow())
            elif name == "SatObsPlanFiles":
                plan_dock, request_dock = self.docks["SatObsImagingPlan"], self.docks["SatObsRequests"]

                def satellites(then, failed=None, plan_dock=plan_dock):
                    plan_dock.load_catalog(lambda: then([*plan_dock.catalog_sats]), failed)

                def requests_changed(requests=None, request_dock=request_dock):
                    # The 「撮像依頼」 dock shows the ledger as read here, or reads it again when it was changed;
                    # the 「撮像予定」 dock the day's rows again once files are written.
                    schedule = self.docks.get("SatObsSchedule")
                    if requests is None and schedule is not None and schedule._loaded and schedule.isVisible():
                        schedule.reload()
                    if request_dock.store is None or request_dock.task is not None:
                        return
                    if requests is not None:
                        request_dock.requests = requests
                        request_dock.render()
                    elif request_dock._loaded:
                        request_dock.reload()

                dock = PlanFileDock(self.iface, self.context, self.open_settings, satellites, requests_changed, self.iface.mainWindow())
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
            self._add_to_menu(action)
            self.actions.append(action)

        self.clock.step("ドックの作成")
        self.place_docks()
        # 「イベントに追加」 of the images, the records and the requests goes to 「イベント」.
        for name in ("SatObsImageCatalog", "SatObsObservations", "SatObsRequests"):
            self.docks[name].event_linker = lambda: self.docks.get("SatObsEvents")
        # 「ファイルなしで記録」 of images whose files are elsewhere goes to 「観測記録」.
        for name in ("SatObsImageCatalog", "SatObsNotices", "SatObsEvents"):
            self.docks[name].observations = lambda: self.docks.get("SatObsObservations")
        # The docks left open now hear that they are shown (their signals waited while they were added).
        for name in INITIAL_DOCKS:
            self.docks[name].visibilityChanged.emit(True)
        # 「臨時依頼にする」: what the plan finds goes to the ledger, through the 「撮像依頼」 form (tsudo, 2026-11-09).
        self.docks["SatObsImagingPlan"].make_requests = self._requests_from_plan
        self._wire_catalog()
        moving = QAction(icon("moving_targets"), tr("移動目標撮像計画…"), self.iface.mainWindow())
        moving.triggered.connect(lambda: self.open_moving_targets())
        self._add_to_menu(moving)
        self.actions.append(moving)
        self._add_new_layer(icon("new_layer"))
        # 「通報」 (alertReports): the notable records as delimited text, and what was reported.
        for name, text, tab in (("alert", "通報文の作成…", 0), ("alert_log", "通報記録の確認…", 1)):
            action = QAction(icon(name), tr(text), self.iface.mainWindow())
            action.setObjectName("SatObsAlert" if tab == 0 else "SatObsAlertLog")
            action.triggered.connect(lambda _on=False, tab=tab: self.docks["SatObsObservations"].open_alerts(tab))
            self._add_to_menu(action)
            self.actions.append(action)
        # 「観測記録のエクスポート」: the records of a period and a place as CSV / JSON (observationExports).
        export = QAction(icon("obs_export"), tr("観測記録のエクスポート…"), self.iface.mainWindow())
        export.setObjectName("SatObsObservationExport")
        export.triggered.connect(lambda _on=False: self.docks["SatObsObservations"].open_export())
        self._add_to_menu(export)
        self.actions.append(export)
        # 「観測記録の一括取込み」: a CSV or a layer into the chosen store, after a dry run.
        record_import = QAction(icon("obs_import"), tr("観測記録の一括取込み…"), self.iface.mainWindow())
        record_import.setObjectName("SatObsObservationImport")
        record_import.triggered.connect(lambda _on=False: self.docks["SatObsObservations"].open_import())
        self._add_to_menu(record_import)
        self.actions.append(record_import)
        # 「分担」 (assignments: who holds which image) and 「引き継ぎメモ」 (the shift handover).
        for name, text, tab in (("assignments", "分担…", 0), ("handover", "引き継ぎメモ…", 1)):
            action = QAction(icon(name), tr(text), self.iface.mainWindow())
            action.setObjectName("SatObsAssignments" if tab == 0 else "SatObsHandover")
            action.triggered.connect(lambda _on=False, tab=tab: self.docks["SatObsObservations"].open_handover(tab))
            self._add_to_menu(action)
            self.actions.append(action)
        # 「パンシャープン…」: the Processing algorithm's dialog (the algorithm also in the Processing toolbox).
        context = self.context
        detect_algorithm.set_models(lambda: (context.config.ai_models, context.source))
        cloud_algorithm.set_forecast(lambda: context.config.cloud_forecast)
        train_algorithm.set_book(lambda: getattr(self.docks.get("SatObsObservations"), "book", None))
        # 保存した分析 (the project's analyses of 「データ分析」) as algorithms, following the project.
        analysis_algorithm.set_context(lambda: context)
        self.provider = SatObsProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)
        for signal in (QgsProject.instance().readProject, QgsProject.instance().cleared):
            signal.connect(self._analyses_changed)
        pansharpen = QAction(icon("pansharpen"), tr("パンシャープン…"), self.iface.mainWindow())
        pansharpen.triggered.connect(lambda: self.open_pansharpen())
        self._add_to_menu(pansharpen)
        self.actions.append(pansharpen)
        change = QAction(icon("change"), tr("変化抽出（2 時期）…"), self.iface.mainWindow())
        change.triggered.connect(lambda: self.open_change())
        self._add_to_menu(change)
        self.actions.append(change)
        align = QAction(icon("align"), tr("自動位置合わせ…"), self.iface.mainWindow())
        align.triggered.connect(lambda: self.open_align())
        self._add_to_menu(align)
        self.actions.append(align)
        classify = QAction(icon("classify"), tr("土地被覆分類…"), self.iface.mainWindow())
        classify.triggered.connect(lambda: self.open_classify())
        self._add_to_menu(classify)
        self.actions.append(classify)
        ortho = QAction(icon("simple_ortho"), tr("簡易オルソ補正…"), self.iface.mainWindow())
        ortho.triggered.connect(lambda: self.open_simple_ortho())
        self._add_to_menu(ortho)
        self.actions.append(ortho)
        for name, algorithm, label in (("geodesic_buffer", "skywatch:geodesic_buffer", "測地線バッファ…"),
                                       ("geodesic_centroid", "skywatch:geodesic_centroids", "正距方位での重心…"),
                                       ("moving_match", "skywatch:moving_match", "移動体と検出の照合…")):
            action = QAction(icon(name), tr(label), self.iface.mainWindow())
            action.triggered.connect(lambda _=False, a=algorithm: self.open_vector_algorithm(a))
            self._add_to_menu(action)
            self.actions.append(action)
        # 「雲量を属性に付与…」: each feature's cover at its time and place (the 「雲量マップ」 dock shows the forecast).
        cloud_cover = QAction(icon("cloud_cover"), tr("雲量を属性に付与…"), self.iface.mainWindow())
        cloud_cover.triggered.connect(lambda: self.open_cloud_cover())
        self._add_to_menu(cloud_cover)
        self.actions.append(cloud_cover)
        # 「サーバ処理…」: a task of a geoprocessing / OGC Processes service as a Processing algorithm.
        remote = QAction(icon("gp"), tr("サーバ処理…"), self.iface.mainWindow())
        remote.triggered.connect(lambda: self.open_remote_processing())
        self._add_to_menu(remote)
        self.actions.append(remote)
        # The AI tools: 「物体検出（AI）…」 as a Processing algorithm (its models set above), 「クリック抽出（AI）」 as a map tool.
        self.context.configChanged.connect(self._refresh_algorithms)
        detect = QAction(icon("ai_detect"), tr("物体検出（AI）…"), self.iface.mainWindow())
        detect.triggered.connect(lambda: self.open_detect())
        self._add_to_menu(detect)
        self.actions.append(detect)
        train = QAction(icon("ai_train"), tr("AI 学習（物体検出）…"), self.iface.mainWindow())
        train.triggered.connect(lambda: self.open_train())
        self._add_to_menu(train)
        self.actions.append(train)
        dataset = QAction(icon("ai_dataset"), tr("教師データセットの作成…"), self.iface.mainWindow())
        dataset.triggered.connect(lambda: self.open_dataset())
        self._add_to_menu(dataset)
        self.actions.append(dataset)
        # 「アノテーション変換」: point annotations as boxes turned to the object (a layer, or a training dataset).
        boxes = QAction(icon("ai_boxes"), tr("アノテーション変換（点→矩形）…"), self.iface.mainWindow())
        boxes.triggered.connect(lambda: self.open_points_to_boxes())
        self._add_to_menu(boxes)
        self.actions.append(boxes)
        dataset_boxes = QAction(icon("ai_dataset_boxes"), tr("アノテーション変換（教師データセット）…"), self.iface.mainWindow())
        dataset_boxes.triggered.connect(lambda: self.open_dataset_points_to_boxes())
        self._add_to_menu(dataset_boxes)
        self.actions.append(dataset_boxes)
        segment = QAction(icon("ai_segment"), tr("クリック抽出（AI）"), self.iface.mainWindow())
        segment.setObjectName("SatObsSegment")
        self.segment = SegmentTool(self.iface, self.context, segment, self.iface.mainWindow())
        self._add_to_menu(segment)
        self.actions.append(segment)
        transfer = QAction(icon("ai_transfer"), tr("検出結果を DB へ転記…"), self.iface.mainWindow())
        transfer.triggered.connect(lambda: self.open_transfer())
        self._add_to_menu(transfer)
        self.actions.append(transfer)
        # The settings file's `layers`: added to an empty project at start, or from the menu.
        self.startup_layers = StartupLayers(self.iface, self.context, self.iface.mainWindow())
        add_layers = QAction(icon("startup_layers"), tr("設定ファイルのレイヤーを追加"), self.iface.mainWindow())
        add_layers.triggered.connect(lambda: self.startup_layers.add())
        self._add_to_menu(add_layers)
        self.actions.append(add_layers)
        # 「背景地図」: the toolbar button opens the list; in the menu, a submenu.
        base_maps = QAction(icon("base_map"), tr("背景地図"), self.iface.mainWindow())
        self.base_maps = BaseMapSwitch(self.iface, self.context, base_maps, self.iface.mainWindow())
        self._add_to_menu(base_maps)
        self.actions.append(base_maps)
        # ArcGIS Web Maps: opened as layers, and the project saved as one.
        self.webmaps = WebMapDialogs(self.iface, self.context, self.base_maps.choose, self.iface.mainWindow())
        for name, label, show in (("webmap_open", "ArcGIS Web マップを開く…", self.webmaps.show_open),
                                  ("webmap_save", "ArcGIS Web マップとして保存…", self.webmaps.show_save)):
            action = QAction(icon(name), tr(label), self.iface.mainWindow())
            action.triggered.connect(lambda _=False, f=show: f())
            self._add_to_menu(action)
            self.actions.append(action)
        # 「スワイプ比較」: the active layer (or group) on one side of a line only.
        swipe = QAction(icon("swipe"), tr("スワイプ比較"), self.iface.mainWindow())
        swipe.setObjectName("SatObsSwipe")
        self.swipe = SwipeCompare(self.iface, swipe, self.iface.mainWindow())
        self._add_to_menu(swipe)
        self.actions.append(swipe)
        # 「位置ずらし」: the active image moved by hand.
        shift = QAction(icon("shift"), tr("位置ずらし"), self.iface.mainWindow())
        shift.setObjectName("SatObsShift")
        self.shift = LayerShift(self.iface, shift, self.iface.mainWindow())
        self._add_to_menu(shift)
        self.actions.append(shift)
        unshift = QAction(icon("unshift"), tr("位置ずらしの解除"), self.iface.mainWindow())
        unshift.triggered.connect(lambda: self.shift.reset())
        self._add_to_menu(unshift)
        self.actions.append(unshift)
        home = QAction(icon("dock_layout"), tr("ドックを初期位置に戻す"), self.iface.mainWindow())
        home.triggered.connect(lambda: self.place_docks())
        self._add_to_menu(home)
        self.actions.append(home)
        # 「ドックのレイアウト」: named layouts per kind of work (ui/dock_layouts.py).
        self.dock_layouts = DockLayoutMenu(self.iface.mainWindow(), lambda: self.docks,
                                           lambda text: self.iface.messageBar().pushSuccess(tr("ドックのレイアウト"), text), self.iface.mainWindow(),
                                           floating=first_setup.docks_floating)
        layouts = self.dock_layouts.menuAction()
        layouts.setIcon(icon("dock_layouts"))
        self._add_to_menu(layouts)
        self.actions.append(layouts)
        # 「担当と個人設定」: the role's docks, docks in windows of their own, and the person's settings (ui/first_setup.py).
        setup = QAction(icon("first_setup"), tr("担当と個人設定…"), self.iface.mainWindow())
        setup.triggered.connect(lambda: self.open_first_setup())
        self._add_to_menu(setup)
        self.actions.append(setup)
        # 「ツールバーのボタン…」: the toolbar's buttons for the role, and the person's own (ui/toolbar_buttons.py).
        buttons = QAction(icon("toolbar_buttons"), tr("ツールバーのボタン…"), self.iface.mainWindow())
        buttons.setObjectName("SatObsToolbarButtons")
        buttons.triggered.connect(lambda: self.open_toolbar_buttons())
        self._add_to_menu(buttons)
        self.actions.append(buttons)
        # 「接続と表の点検」: every ArcGIS service of the settings file asked once (ui/health_dialog.py).
        health = QAction(icon("health_check"), tr("接続と表の点検…"), self.iface.mainWindow())
        health.triggered.connect(lambda: self.open_health_check())
        self._add_to_menu(health)
        self.actions.append(health)
        # 「使い方（ヘルプ）」: the help for everyday users, bundled HTML (help/index.html, ui/help_dialog.py).
        help_action = QAction(icon("help"), tr("使い方（ヘルプ）…"), self.iface.mainWindow())
        help_action.setObjectName("SatObsHelp")
        help_action.triggered.connect(lambda: self.open_help())
        self._add_to_menu(help_action)
        self.actions.append(help_action)
        # Also in QGIS's Help ▸ Plugins.
        help_menu = self.iface.pluginHelpMenu() if hasattr(self.iface, "pluginHelpMenu") else None
        if help_menu is not None:
            help_menu.addAction(help_action)
        self._help_menu = help_menu
        self.help_action = help_action
        # 「Python API リファレンス」: plugin.api in Japanese with examples (ui/api_reference_dialog.py).
        reference = QAction(icon("api_reference"), tr("Python API リファレンス…"), self.iface.mainWindow())
        reference.setObjectName("SatObsApiReference")
        reference.triggered.connect(lambda: self.open_api_reference())
        self._add_to_menu(reference)
        self.actions.append(reference)
        settings = QAction(icon("settings"), tr("設定…"), self.iface.mainWindow())
        settings.triggered.connect(self.open_settings)
        self._add_to_menu(settings)
        self.actions.append(settings)
        # 「ここを撮像計画」 in the map's right-click menu.
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)
        self._build_menu()
        # The toolbar: the role's buttons (or the person's own), from the entries in menu order.
        self.toolbar_buttons = ToolbarButtons(self.toolbar, self._entries_in_menu_order(), first_setup.role)
        self.toolbar_buttons.apply()
        # Keys for frequent actions, in QGIS's shortcut settings (ui/shortcuts.py).
        self.shortcuts = Shortcuts(self.iface, self.docks)
        self.clock.step("メニュー・ツールバー")
        self._add_preload_items()
        self.context.configChanged.connect(self._config_read_for_preload)
        self._config_asked = self.clock.now()
        self.context.reload()
        started = getattr(self.iface, "initializationCompleted", None)
        if self.iface.mainWindow().isVisible() or started is None:
            # Installed or turned on while QGIS runs (or no such signal): started already.
            self._qgis_ready()
        else:
            started.connect(self._qgis_ready)
        if self._first_start and first_setup.ENABLED:
            # Once QGIS has started (and put back its own window layout), or now when installed while it runs.
            started = getattr(self.iface, "initializationCompleted", None)
            if self.iface.mainWindow().isVisible() or started is None:
                QTimer.singleShot(300, lambda: self.open_first_setup(first=True))
            else:
                started.connect(self._first_setup_after_start)

    # ---- 「先読み」 and the startup's timings ------------------------------------------------------------

    def _add_preload_items(self) -> None:
        """The reads ahead (core/preload.ITEMS), each through its dock's own read."""
        docks, config = self.docks, lambda: self.context.config

        def satellites(then, failed) -> bool:
            dock = docks.get("SatObsImagingPlan")
            if dock is None or not config().satellite_catalogs or dock._loaded:
                return False
            dock.load_catalog(then, failed)
            return True

        def requests(then, failed) -> bool:
            dock = docks.get("SatObsRequests")
            if dock is None or dock.store is None or dock._loaded:
                return False
            dock.reload(then, failed)
            return True

        def events(then, failed) -> bool:
            dock = docks.get("SatObsEvents")
            if dock is None or dock.settings is None or dock.events is not None:
                return False
            dock.reload(then, failed=failed)
            return True

        for key, start in (("satellites", satellites), ("requests", requests), ("events", events)):
            self.preloader.add(key, PRELOAD_ITEMS[key], start)
        self.preloader.add("offlineCache", tr("オフライン用の控え（観測記録の保存先）"), self._warm_offline_cache, always=True)
        self.preloader.finished.connect(self._preload_finished)

    def _qgis_ready(self) -> None:
        """QGIS has started (its window shown): the reads ahead begin once the settings file is read."""
        try:
            self.iface.initializationCompleted.disconnect(self._qgis_ready)
        except (TypeError, RuntimeError, AttributeError):
            pass
        if self.context is None or self._qgis_started:
            return
        self._qgis_started = True
        if not self.context.loading:
            self.preloader.schedule()

    def _config_read_for_preload(self) -> None:
        if getattr(self, "_config_asked", None) is not None:
            # The first settings file read (a URL is read in a task): how long it took.
            self.clock.step("設定ファイルの読み込み", self.clock.now() - self._config_asked)
            self._config_asked = None
        if self._qgis_started and self.preloader is not None:
            self.preloader.schedule()

    def _preload_finished(self) -> None:
        """The first round of reads ahead ended: the startup's timings in QGIS's log (tab SKYWATCH), once."""
        if self._startup_told:
            return
        self._startup_told = True
        self.clock.ready()
        QgsMessageLog.logMessage(self.clock.report(), LOG_TAG, Qgis.MessageLevel.Info, notifyUser=False)

    def _first_setup_after_start(self) -> None:
        try:
            self.iface.initializationCompleted.disconnect(self._first_setup_after_start)
        except (TypeError, RuntimeError):
            pass
        QTimer.singleShot(300, lambda: self.open_first_setup(first=True))

    def open_help(self, section: Optional[str] = None):
        """「使い方（ヘルプ）…」: the help for everyday users (not blocking QGIS), at a section (its id) if given."""
        from .ui.help_dialog import HelpDialog

        if getattr(self, "help_dialog", None) is None:
            self.help_dialog = HelpDialog(self.run_menu_entry, self.iface.mainWindow())
        self.help_dialog.show()
        self.help_dialog.raise_()
        if section:
            self.help_dialog.go(section)
        return self.help_dialog

    def run_menu_entry(self, text: str) -> bool:
        """The help's 「…を開く」 links: the menu entry of that text (a dock comes to the front, never closes)."""
        action = self._entries_in_menu_order().get(tr(text))
        if action is None:
            return False
        dock = next((d for d in self.docks.values() if d.toggleViewAction() is action), None)
        if dock is not None:
            dock.show()
            dock.raise_()
            return True
        if action.menu() is not None:  # a submenu (背景地図): shown where the mouse is
            action.menu().popup(QCursor.pos())
            return True
        action.trigger()
        return True

    def open_api_reference(self):
        """「Python API リファレンス…」: what plugin.api has, with examples (not blocking QGIS)."""
        from .ui.api_reference_dialog import ApiReferenceDialog

        if getattr(self, "api_reference", None) is None:
            self.api_reference = ApiReferenceDialog(self.api, self.iface.mainWindow())
        self.api_reference.show()
        self.api_reference.raise_()
        return self.api_reference

    def open_health_check(self):
        """「接続と表の点検…」: the dialog (not blocking QGIS), checking right away."""
        from .ui.health_dialog import HealthDialog

        if self.context is None:
            return None
        if getattr(self, "health_dialog", None) is None:
            self.health_dialog = HealthDialog(self.context, self.iface.mainWindow())
        self.health_dialog.show()
        self.health_dialog.raise_()
        self.health_dialog.run()
        return self.health_dialog

    def open_first_setup(self, first: bool = False) -> Optional[first_setup.FirstSetupDialog]:
        """「最初の設定」 (first) or 「担当と個人設定」: the page, open beside the main window (not blocking it)."""
        if self.context is None:  # unloaded meanwhile
            return None
        if self.setup_dialog is not None:
            self.setup_dialog.deleteLater()
        self.setup_dialog = first_setup.FirstSetupDialog(
            self.iface.mainWindow(), lambda: self.docks, first=first,
            observation_dock=lambda: self.docks.get("SatObsObservations"), coordinates=lambda: self.coordinates,
            open_settings=self.open_settings)
        # A new role: its toolbar buttons too.
        self.setup_dialog.accepted.connect(lambda: self.toolbar_buttons.apply() if self.context is not None else None)
        self.setup_dialog.open()
        return self.setup_dialog

    def open_toolbar_buttons(self):
        """「ツールバーのボタン…」: the buttons for the role in use (beside the main window, not blocking it)."""
        if self.context is None:
            return None
        if self.toolbar_dialog is not None:
            self.toolbar_dialog.deleteLater()
        self.toolbar_dialog = ToolbarButtonsDialog(self.toolbar_buttons, self.iface.mainWindow())
        self.toolbar_dialog.open()
        return self.toolbar_dialog

    def _entries_in_menu_order(self) -> dict:
        """The menu's entries (text → action) in the order the menu shows them (「通知」 without its unread count)."""
        by_text = {(tr("通知") if a is self.notices_action else a.text()): a for a in self._menu_entries}
        order = [t for _title, texts in MENU_GROUPS for t in texts if t] + MENU_TOP
        entries = {tr(t): by_text[tr(t)] for t in order if tr(t) in by_text}
        entries.update((t, a) for t, a in by_text.items() if t not in entries)
        return entries

    def _add_to_menu(self, action: QAction) -> None:
        """An entry of the plugin's menu: placed in its group by `_build_menu` (MENU_GROUPS)."""
        self._menu_entries.append(action)

    def _build_menu(self) -> None:
        """
        The plugin's menu: a submenu per area (MENU_GROUPS), then MENU_TOP. An entry
        in neither (a new one not yet listed) goes after the groups, so it can still be reached.
        """
        by_text = {a.text(): a for a in self._menu_entries}
        placed: set = set()
        window = self.iface.mainWindow()
        for title, texts in MENU_GROUPS:
            entries = [by_text.get(tr(t)) if t is not None else None for t in texts]
            if not any(entries):
                continue
            menu = QMenu(tr(title), window)
            menu.setObjectName(f"SatObsMenu{len(self._submenus)}")
            last_separator = True
            for entry in entries:
                if entry is None:
                    if not last_separator:
                        menu.addSeparator()
                        last_separator = True
                    continue
                menu.addAction(entry)
                placed.add(entry)
                last_separator = False
            self._submenus.append(menu)
            self.iface.addPluginToMenu(MENU, menu.menuAction())
        top = [by_text[tr(t)] for t in MENU_TOP if tr(t) in by_text]
        rest = [a for a in self._menu_entries if a not in placed and a not in top]
        separator = QAction(window)
        separator.setSeparator(True)
        self._menu_separator = separator
        self.iface.addPluginToMenu(MENU, separator)
        for action in rest + top:
            self.iface.addPluginToMenu(MENU, action)
            self._menu_top.append(action)

    def _add_new_layer(self, new_icon: QIcon) -> None:
        """「新規スクラッチレイヤー（テンプレート）…」: in the plugin's menu, and in Layer ▸ Create Layer after QGIS's own scratch layer."""
        action = QAction(new_icon, tr("新規スクラッチレイヤー（テンプレート）…"), self.iface.mainWindow())
        action.setObjectName("SatObsNewScratchLayer")
        action.triggered.connect(lambda: self.open_new_layer())
        self._add_to_menu(action)
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
        as tabs, none floating (each in its own window with 「別ウィンドウで開く」). Open ones stay open, the group's first open one in front.
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
        # 「別ウィンドウで開く」 (「担当と個人設定」): each in a window of its own, cascaded beside the main window.
        if first_setup.docks_floating():
            float_docks(window, self.docks)

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
        notices.choose_images = catalog.choose_images
        self.notices_action = notices.toggleViewAction()

        bell = self.notices_action.icon()

        def unread(n: int) -> None:
            self.notices_action.setText(tr("通知") + (f"（{n}）" if n else ""))
            self.notices_action.setIcon(badged(bell, n))

        notices.unread_changed = unread
        self.docks["SatObsImagingPlan"].on_reminder = notices.add_plan_notice
        # 通知 ▸ 通報: reports polled from the log table are highlighted in 観測記録 and open there.
        observations = self.docks["SatObsObservations"]
        feed = notices.alerts
        feed.on_reported = observations.mark_reported
        feed.on_removed = observations.refresh_reported

        def open_reported(record_id: str) -> None:
            o = observations.book.record(record_id) if observations.book else None
            if o is None:
                self.iface.messageBar().pushWarning(tr("通報"), plain_tip(tr("観測記録が見つからない（別の保存先か削除済み）: {i}").format(i=record_id)))
                return
            observations.show()
            observations.raise_()
            observations.open_record(o)

        feed.open_record = open_reported

        def open_log() -> None:
            dialog = observations.open_alerts(1)
            if dialog.log_button.isEnabled():
                dialog.read_log()

        feed.open_log = open_log
        # 「類似画像の検索…」 in the right-click menu of layers opened from the catalog.
        self.similar_action = QAction(QIcon(os.path.join(HERE, "resources", "catalog.svg")), tr("類似画像の検索…"), self.iface.mainWindow())
        self.similar_action.triggered.connect(lambda: catalog.find_similar_of_layer(self.iface.activeLayer()))
        self.iface.addCustomActionForLayerType(self.similar_action, "", QgsMapLayerType.RasterLayer, False)
        QgsProject.instance().layersAdded.connect(self._layers_added)
        self._layers_added(QgsProject.instance().mapLayers().values())
        # QGIS の式の関数 「SKYWATCH」: the records the observation dock read and the named areas.
        expression_functions.set_book(lambda: getattr(self.docks.get("SatObsObservations"), "book", None))
        expression_functions.register()
        self.place_reader = expression_functions.PlaceReader(self.context)
        self.place_reader.start()
        # Layers whose dates are in another zone than the service says (the settings file's timeZone): a corrected field per date.
        time_zone_fields.register()
        self.time_zone_fields = time_zone_fields.TimeZoneFields(
            self.context.esri, lambda url: self.context.token_for(url, self.context.token_of(url)),
            lambda text: self.iface.messageBar().pushWarning(tr("タイムゾーン"), text))
        QgsProject.instance().layersAdded.connect(self.time_zone_fields.layers_added)
        self.context.configChanged.connect(self._time_zones_changed)
        # Esri layers QGIS edits itself: who and when in their tracking fields (the settings file's editTracking).
        self.edit_stamp_layers = edit_stamp_layers.EditStampLayers()
        QgsProject.instance().layersAdded.connect(self.edit_stamp_layers.layers_added)
        QgsProject.instance().layersRemoved.connect(self.edit_stamp_layers.layers_removed)
        self.edit_stamp_layers.layers_added(QgsProject.instance().mapLayers().values())

    def _time_zones_changed(self) -> None:
        if getattr(self, "time_zone_fields", None) is not None:
            self.time_zone_fields.layers_added(QgsProject.instance().mapLayers().values())

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
        processing.execAlgorithmDialog("skywatch:pansharpen", default_inputs(layers, self.iface.activeLayer()))

    def open_change(self) -> None:
        """The algorithm's dialog, with the two earliest images (else the lowest two in the list), the earlier as before."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.change_algorithm import default_inputs as change_inputs
        from .qgis_io.obs_images import observed_image

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:change", change_inputs(layers, lambda layer: observed_image(layer).time))

    def open_align(self) -> None:
        """The algorithm's dialog, with the active image to align and the finest other as the reference."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.align_algorithm import default_inputs as align_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:align", align_inputs(layers, self.iface.activeLayer()))

    def open_classify(self) -> None:
        """The algorithm's dialog, with the active image and a polygon layer for the training."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.classify_algorithm import default_inputs as classify_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:classify", classify_inputs(layers, self.iface.activeLayer()))

    def open_simple_ortho(self) -> None:
        """The algorithm's dialog, with the active image and a DEM-like layer."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.ortho_algorithm import default_inputs as ortho_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:simple_ortho", ortho_inputs(layers, self.iface.activeLayer()))

    def open_cloud_cover(self) -> None:
        """「雲量を属性に付与」's dialog, with the active vector layer and its date-time field."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.cloud_algorithm import default_inputs as cloud_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:cloud_cover", cloud_inputs(layers, self.iface.activeLayer()))

    def open_vector_algorithm(self, algorithm: str) -> None:
        """A vector algorithm's dialog, with the active vector layer as its input."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        layer = self.iface.activeLayer()
        if algorithm == "skywatch:moving_match":
            from .processing_provider.moving_match_algorithm import default_inputs as match_inputs

            inputs = match_inputs(list(QgsProject.instance().mapLayers().values()), layer)
        else:
            inputs = {"INPUT": layer.id()} if isinstance(layer, QgsVectorLayer) else {}
        processing.execAlgorithmDialog(algorithm, inputs)

    def _analyses_changed(self, *_args) -> None:
        if self.provider is not None:
            analysis_algorithm.refresh()

    def _refresh_algorithms(self) -> None:
        """The settings file's AI models into the algorithm's choice."""
        if self.provider is not None:
            self.provider.refreshAlgorithms()

    def open_detect(self) -> None:
        """The algorithm's dialog, with the active image (else the first), the map's extent and the screen's pixel size."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.detect_algorithm import default_inputs as detect_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:detect", detect_inputs(layers, self.iface.activeLayer(), self.iface.mapCanvas()))

    def open_train(self) -> None:
        """The training algorithm's dialog, with the active image (else the first)."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.train_algorithm import default_inputs as train_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:train_detector", train_inputs(layers, self.iface.activeLayer()))

    def open_dataset(self) -> None:
        """The dialog making a training dataset of images outside the catalog."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        processing.execAlgorithmDialog("skywatch:make_dataset", {})

    def open_points_to_boxes(self) -> None:
        """「アノテーション変換（点→矩形）」 with the active point layer and image."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from .processing_provider.annotation_algorithm import default_inputs as box_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("skywatch:points_to_boxes", box_inputs(layers, self.iface.activeLayer()))

    def open_dataset_points_to_boxes(self) -> None:
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        processing.execAlgorithmDialog("skywatch:dataset_points_to_boxes", {})

    def open_transfer(self) -> None:
        if self.transfer_dialog is None:
            self.transfer_dialog = TransferDialog(self.context, lambda text: self.iface.messageBar().pushInfo(tr("検出結果を DB へ転記"), text),
                                                  self.iface.mainWindow())
        self.transfer_dialog.prepare(self.iface.activeLayer())
        self.transfer_dialog.show()
        self.transfer_dialog.raise_()

    def open_remote_processing(self) -> None:
        if self.remote_dialog is None:
            self.remote_dialog = RemoteProcessingDialog(self.context, self.iface.mainWindow())
        self.remote_dialog.prepare()
        self.remote_dialog.show()
        self.remote_dialog.raise_()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.context, self.iface.mainWindow())
        try:
            dialog.exec()
        finally:
            # Made anew each time: the closed one goes, with its auth lists (#87).
            dialog.deleteLater()

    def _config_problems(self) -> None:
        """「設定ファイルの問題 n 件」 in the message bar after a read that found any (the list is in 「設定…」)."""
        c = self.context
        if c is None or c.loading:
            return
        key = (c.source, tuple(c.problems))
        if not c.problems:
            self._problems_shown = None
            return
        if key == self._problems_shown:
            return
        self._problems_shown = key
        bar = self.iface.messageBar()
        item = bar.createMessage(tr("設定ファイル"), tr("問題 {n} 件（一覧はメニュー「設定…」）").format(n=len(c.problems)))
        button = QPushButton(tr("設定…"), item)
        button.clicked.connect(self.open_settings)
        item.layout().addWidget(button)
        item.setToolTip("\n".join(c.problems))
        bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)

    def _overviews_changed(self) -> None:
        if self.context is None:
            return
        overviews_io.configure_cache(self.context.config.overviews)
        if not self._overviews_ready:
            self._overviews_ready = True
            # Those added before the settings file was read (a project opened at start): its `location` and `scope` apply.
            ids = set(QgsProject.instance().mapLayers())
            waiting = [layer for layer_id, layer in self._overviews_waiting if layer_id in ids]
            self._overviews_waiting = []
            if waiting:
                self._raster_layers_added(waiting)

    def _overview_layers_added(self, layers) -> None:
        if not self._overviews_ready:
            self._overviews_waiting += [(layer.id(), layer) for layer in layers]
            return
        self._raster_layers_added(layers)

    def _complex_mode(self, layer):
        """How a complex SAR image is shown: the first imageRules entry for its file name with `complexImages`, else the settings file's."""
        config = self.context.config if self.context else None
        if config is None:
            return None
        path = overviews_io.local_path(layer)
        rule = image_rule_for(config.image_rules, path) if path and config.image_rules else None
        if rule is not None and rule.complex_images is not None:
            return rule.complex_images or None
        return config.complex_images

    def _raster_layers_added(self, layers) -> None:
        """Complex SAR images first get their amplitude VRT (`complexImages`), whose overviews are then built, not those of I and Q."""
        layers = list(layers)
        complex_sar.layers_added(layers, self._complex_mode, self.iface.messageBar())
        overviews_io.layers_added(layers, self.iface.messageBar())

    def _warm_offline_cache(self, then, failed) -> bool:
        """
        With the reads ahead (unless `preload` is false): every observation store's records, checked areas and
        classification are read once, so they are in the offline cache even for a store not chosen today.
        """
        context = self.context
        config = context.config
        if not config.offline_work.enabled or not config.observation_stores or not getattr(config, "preload", True):
            return False
        from .core.obs.setup import store_sets

        def work(task) -> int:
            sets = store_sets(config.observation_stores, config.observation_vocabulary, context.gpkg, context.esri,
                              context.token_for, local=False)
            n = 0
            for s in sets:
                # Only the server's tables (an area or classification kept in the GeoPackage is not read from this thread).
                reads = [s.records.load] + ([s.areas.load] if s.config.areas else []) + \
                    ([s.taxonomy.load, s.taxonomy.load_fields] if s.config.taxonomy_table else [])
                for read in reads:
                    if task.isCanceled():
                        return n
                    try:
                        read()
                        n += 1
                    except Exception:  # noqa: BLE001 (one table unreadable: the others are still kept)
                        pass
            return n

        tasks.run_task(tr("オフライン用の控えの更新"), work, lambda _n: then(), failed, hidden=True)
        return True

    def _requests_from_plan(self, ops: list, sats: list, targets: list):
        """The 「撮像依頼」 dock shown, its form opened with 臨時 requests of the plan's opportunities."""
        dock = self.docks["SatObsRequests"]
        dock.show()
        dock.raise_()
        return dock.from_plan(ops, sats, targets)

    def _reload_after_offline(self) -> None:
        """The work kept offline was sent or thrown away (or a server is back): the docks that show it read again."""
        for name in ("SatObsObservations", "SatObsRequests"):
            dock = self.docks.get(name)
            if dock is not None:
                try:
                    dock.reload()
                except RuntimeError:
                    pass

    def unload(self) -> None:
        if getattr(self, "offline", None) is not None:
            self.offline.unload()
            self.offline = None
        if self.preloader is not None:
            self.preloader.close()
            try:
                self.context.configChanged.disconnect(self._config_read_for_preload)
            except (TypeError, RuntimeError, AttributeError):
                pass
            try:
                self.iface.initializationCompleted.disconnect(self._qgis_ready)
            except (TypeError, RuntimeError, AttributeError):
                pass
            self.preloader.deleteLater()
            self.preloader = None
        if getattr(self, "shortcuts", None) is not None:
            self.shortcuts.unload()
            self.shortcuts = None
        overviews_io.queue.cancel_all()
        # Before the docks and the local store go: a task ending later would write into them (#88).
        tasks.cancel_all()
        tasks.restore_gil_sharing()
        try:
            QgsProject.instance().layersAdded.disconnect(self._overview_layers_added)
        except (TypeError, RuntimeError):
            pass
        if "SatObsMoving" in self.docks:
            self.docks["SatObsMoving"].close_all()
        if "SatObsDashboard" in self.docks:
            self.docks["SatObsDashboard"].close_all()
        if "SatObsAnalysis" in self.docks:
            self.docks["SatObsAnalysis"].close_all()
        for name in ("SatObsRequests", "SatObsPlanFiles", "SatObsSchedule"):
            if name in self.docks:
                self.docks[name].close_all()
        try:
            self.context.configChanged.disconnect(self._refresh_algorithms)
        except (TypeError, RuntimeError, AttributeError):
            pass
        for signal in (QgsProject.instance().readProject, QgsProject.instance().cleared):
            try:
                signal.disconnect(self._analyses_changed)
            except (TypeError, RuntimeError):
                pass
        analysis_algorithm.set_context(lambda: None)
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        detect_algorithm.set_models(lambda: ([], ""))
        cloud_algorithm.set_forecast(lambda: None)
        if "SatObsCloud" in self.docks:
            self.docks["SatObsCloud"].unload()
        train_algorithm.set_book(lambda: None)
        if self.segment is not None:
            self.segment.close()
            self.segment.deleteLater()
            self.segment = None
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
        if getattr(self, "edit_stamp_layers", None) is not None:
            for signal, slot in ((QgsProject.instance().layersAdded, self.edit_stamp_layers.layers_added),
                                 (QgsProject.instance().layersRemoved, self.edit_stamp_layers.layers_removed)):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self.edit_stamp_layers.active = False
            self.edit_stamp_layers = None
        if getattr(self, "time_zone_fields", None) is not None:
            try:
                QgsProject.instance().layersAdded.disconnect(self.time_zone_fields.layers_added)
            except (TypeError, RuntimeError):
                pass
            self.time_zone_fields = None
            time_zone_fields.unregister()
        if getattr(self, "place_reader", None) is not None:
            self.place_reader.stop()
            self.place_reader = None
        expression_functions.unregister()
        if getattr(self, "help_dialog", None) is not None:
            self.help_dialog.close()
            self.help_dialog.deleteLater()
            self.help_dialog = None
        if getattr(self, "_help_menu", None) is not None:
            try:
                self._help_menu.removeAction(self.help_action)
            except RuntimeError:
                pass
            self._help_menu = None
        if getattr(self, "api_reference", None) is not None:
            self.api_reference.close()
            self.api_reference.deleteLater()
            self.api_reference = None
        if self.swipe is not None:
            self.swipe.close()
            self.swipe.deleteLater()
            self.swipe = None
        if self.shift is not None:
            self.shift.close()
            self.shift.deleteLater()
            self.shift = None
        if self.remote_dialog is not None:
            self.remote_dialog.close()
            self.remote_dialog.deleteLater()
            self.remote_dialog = None
        if self.transfer_dialog is not None:
            self.transfer_dialog.close()
            self.transfer_dialog.deleteLater()
            self.transfer_dialog = None
        if self.webmaps is not None:
            self.webmaps.close()
            self.webmaps.deleteLater()
            self.webmaps = None
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
        for action in self._menu_top:
            self.iface.removePluginMenu(MENU, action)
        if self._menu_separator is not None:
            self.iface.removePluginMenu(MENU, self._menu_separator)
            self._menu_separator = None
        for menu in self._submenus:
            self.iface.removePluginMenu(MENU, menu.menuAction())
            menu.deleteLater()
        self._submenus, self._menu_top, self._menu_entries = [], [], []
        if getattr(self, "setup_dialog", None) is not None:
            self.setup_dialog.close()
            self.setup_dialog.deleteLater()
            self.setup_dialog = None
        if self.toolbar_dialog is not None:
            self.toolbar_dialog.close()
            self.toolbar_dialog.deleteLater()
            self.toolbar_dialog = None
        self.toolbar_buttons = None
        if getattr(self, "health_dialog", None) is not None:
            self.health_dialog.close()
            self.health_dialog.deleteLater()
            self.health_dialog = None
        if getattr(self, "dock_layouts", None) is not None:
            self.dock_layouts.deleteLater()
            self.dock_layouts = None
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
