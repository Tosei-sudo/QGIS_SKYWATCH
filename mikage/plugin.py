"""
The plugin: its menu, toolbar and docks, made of its features (mikage/features, docs/architecture.md).

What the plugin itself holds: the services every feature uses (the settings file, 監査ログ,
一時的なオフライン作業, 先読み, 担当), the layout of the screens (the menu's groups, where each
dock starts, the map's right-click menu), the Processing provider and QGIS's start and unload.
Each feature hands it its docks, menu entries and commands (features/base.py).
"""
from __future__ import annotations

import gc
from typing import Optional, Sequence

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsProject
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import QDockWidget, QMenu

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from .core import edition, win64
from .core.preload import StartupClock
from .core.roles import ALGORITHM_FEATURES, refusal as role_refusal
from .features import ai, analysis, command, enhance, events, images, layers, maps, observations, plan, platform, rendering, requests, training
from .features.base import Feature, disconnect, icon
from .plugin_api import OBSERVATION_FUNCTIONS, PluginApi  # noqa: F401 (scripts and tests import them from here)
from .processing_provider import analysis_algorithm, append_algorithm, cloud_algorithm, detect_algorithm
from .processing_provider.provider import SatObsProvider
from .qgis_io import local_copy_io, map_tools, network, overviews_io, tasks
from .qgis_io.context import PluginContext
from .qgis_io.geometry import to_wgs84
from .qgis_io.preload import LOG_TAG, Preloader
from .qgis_io.roles_io import RoleKeeper
from .qgis_io.stall_watch import StallWatch
from .ui import first_setup
from .ui.audit_status import AuditKeeper
from .ui.dock_layouts import float_docks
from .ui.offline_status import OfflineController
from .ui.ports import PluginEvents, Ports
from .ui.shortcuts import Shortcuts
from .ui.toolbar_buttons import ToolbarButtons

#: The entries hidden in MIKAGE 練習版 whatever the settings (core/edition.py): the audit log is not kept there.
NOT_IN_TRAINING_EDITION = ("監査ログ…",)
#: The plugin's menu by area (tsudo, 2026-11-07): each group a submenu, its entries by their text in this order
#: (None: a separator). The entries after the groups stay in the menu itself, 「設定…」 last. 2026-11-28 (点検 13):
#: 移動体 with 画像's reading tools (as the help's 判読), ダッシュボード with データ分析, so 観測記録 holds the records' only.
MENU_GROUPS = [
    ("練習モード", ["練習モードを始める", "チュートリアル…", "判読の練習…", "練習データを最初に戻す…", "練習モードを終える", None,
               "判読の練習の問題を作る…", "判読の練習の成績…"]),
    ("撮像計画", ["撮像計画", "衛星パス", "移動目標撮像計画…", None, "雲量マップ", "雲量を属性に付与…"]),
    ("撮像依頼", ["撮像依頼", "撮像計画ファイル出力", "撮像予定", "地点ごとの撮像予定", "依頼グループ…", "年間撮像上限…", "衛星・システムの障害情報…"]),
    ("観測記録", ["観測記録", "自分の記録…", "分類体系の編集", "分類体系の取り込み…", "通報文の作成…", "通報記録の確認…", "観測記録の書き出し…", "観測記録の一括取り込み…", "分担…", "引き継ぎメモ…"]),
    ("画像", ["画像カタログ", "通知", "新着画像（大きな画面）", "関心地域・地点", None, "画像補正", "影から高さ", "スペクトル", "移動体（AIS・ADS-B）", None, "スワイプ比較", "位置ずらし", "位置ずらしの解除", "クイック幾何補正"]),
    ("解析・処理", ["データ分析", "データに質問", "ダッシュボード", None, "パンシャープン…", "クイックパンシャープン", "変化抽出（2 時期）…", "自動位置合わせ…", "土地被覆分類…", "簡易オルソ補正…", None,
                "測地線バッファ…", "正距方位での重心…", "移動体と検出の照合…", None, "サーバ処理…"]),
    ("AI", ["物体検出（AI）…", "クリック抽出（AI）", "AI結果", "検出結果を DB へ転記…", None, "教師データセットの作成…", "観測記録をピクセル座標で書き出す…", "アノテーション変換（点→矩形）…", "アノテーション変換（教師データセット）…",
            "AI 学習（物体検出）…"]),
    ("地図・レイヤー", ["背景地図", "設定ファイルのレイヤーを追加", "新規スクラッチレイヤー（テンプレート）…", "アペンド（既存レイヤーへ追加）…", None,
                   "ArcGIS Web マップを開く…", "ArcGIS Web マップとして保存…"]),
]
#: In the menu itself, after the groups (behind a separator).
MENU_TOP = ["イベント", "参考情報", "担当と個人設定…", "ツールバーのボタン…", "ドックのレイアウト", "ドックを初期位置に戻す", "接続と表の点検…", "監査ログ…", "使い方（ヘルプ）…", "Python API リファレンス…", "設定…"]
#: Where the docks start: each group docked on one side, as tabs (the first in front).
DOCK_GROUPS = [
    (Qt.DockWidgetArea.RightDockWidgetArea, ["SatObsImagingPlan", "SatObsSatellitePass", "SatObsRequests", "SatObsPlanFiles",
                                             "SatObsSchedule", "SatObsCloud"]),
    (Qt.DockWidgetArea.LeftDockWidgetArea, ["SatObsImageCatalog", "SatObsNotices", "SatObsInterests", "SatObsEnhance", "SatObsShadowHeight", "SatObsSpectral",
                                            "SatObsDashboard", "SatObsAnalysis", "SatObsDataChat"]),
    (Qt.DockWidgetArea.RightDockWidgetArea, ["SatObsObservations", "SatObsTaxonomy", "SatObsMoving", "SatObsAiResults", "SatObsEvents",
                                             "SatObsReferences"]),
    # A timeline: under the map, as wide as it (tsudo, 2026-11-14: 「複数地点は縦に並べ、横に時間が伸びる」).
    (Qt.DockWidgetArea.BottomDockWidgetArea, ["SatObsPlaceSchedule"]),
]
#: The docks open when the plugin starts without a saved layout (the others from the menu or toolbar).
INITIAL_DOCKS = ("SatObsImageCatalog", "SatObsImagingPlan")

#: The order the docks are made in (and QGIS's Panels menu lists them): a dock may use the ones before it.
DOCK_ORDER = (
    "SatObsImageCatalog", "SatObsImagingPlan", "SatObsRequests", "SatObsPlanFiles", "SatObsSchedule", "SatObsPlaceSchedule",
    "SatObsSatellitePass", "SatObsCloud", "SatObsObservations", "SatObsTaxonomy", "SatObsNotices", "SatObsInterests",
    "SatObsEnhance", "SatObsShadowHeight", "SatObsSpectral", "SatObsMoving", "SatObsDashboard", "SatObsAiResults",
    "SatObsEvents", "SatObsReferences", "SatObsAnalysis", "SatObsDataChat",
)
#: The map's right-click menu, at the point clicked (the features' entries by their text).
MAP_MENU = ("ここを撮像計画", "この地点の新着画像を監視…", "この地点の撮像予定", "この地域で撮像依頼…")
#: The reads ahead after 担当, in this order (core/preload.ITEMS, then the features' own).
PRELOAD_ORDER = ("satellites", "requests", "events", "offlineCache")
#: The features (mikage/features): each phase (start, docks, wiring, entries, unload) goes through them in this order.
FEATURES: tuple[type[Feature], ...] = (
    rendering.Rendering, platform.Platform, training.Training, images.Images, plan.Plan, observations.Observations, requests.Requests,
    events.Events, analysis.Analysis, ai.Ai, maps.Maps, enhance.Enhance, layers.Layers,
    # 文で操作 last: its operations open the others' docks and entries.
    command.Command,
)


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


def menu_name() -> str:
    """The plugin's menu in QGIS's Plugins menu: 「MIKAGE」, or 「MIKAGE 練習版」 (core/edition.py)."""
    return edition.name()


def other_running() -> bool:
    """
    Whether the other version of the plugin runs in this QGIS (MIKAGE and MIKAGE 練習版 may both be installed,
    and both turned on): they share their Processing provider, expression functions, shortcuts and docks' names.
    """
    try:
        from qgis import utils
    except ImportError:
        return False
    return edition.other_id() in (getattr(utils, "active_plugins", None) or [])


def _log_library(text: str, problem: bool) -> None:
    """A line of the Windows zip's libraries (core/win64) in the log: where one was loaded from, or why it was not."""
    QgsMessageLog.logMessage(text, LOG_TAG, Qgis.MessageLevel.Warning if problem else Qgis.MessageLevel.Info, notifyUser=False)


class MikagePlugin:
    def __init__(self, iface, features: Optional[Sequence[type[Feature]]] = None):
        """
        `features`: only these of FEATURES (in FEATURES' order), for the tests of a feature in a small assembly
        (tests/feature_harness.py); the others' docks, entries and ports are not there (their ports give None).
        """
        self.iface = iface
        self.context: Optional[PluginContext] = None
        self.actions: list[QAction] = []
        self.docks: dict[str, QDockWidget] = {}
        self.toolbar = None
        self.api = PluginApi(self)
        #: The features' ports to each other (ui/ports.py: each set by the feature that has it) and their notices.
        self.ports = Ports()
        self.signals = PluginEvents()
        #: 「画面が止まった」 in the log (qgis_io/stall_watch.py), from once QGIS has started.
        self.stall_watch: Optional[StallWatch] = None
        self.provider: Optional[SatObsProvider] = None
        self.offline: Optional[OfflineController] = None
        #: 監査ログ: the log of operations kept and sent to the settings file's folder (ui/audit_status.py, core/audit.py).
        self.audit: Optional[AuditKeeper] = None
        #: The menu's entries as added, its submenus (MENU_GROUPS) and what sits in the menu itself.
        self._menu_entries: list[QAction] = []
        self._submenus: list[QMenu] = []
        self._menu_top: list[QAction] = []
        self._menu_separator: Optional[QAction] = None
        #: The docks' entries by their title (「通知」's text carries the unread count).
        self._entry_titles: dict = {}
        self.toolbar_buttons: Optional[ToolbarButtons] = None
        self.shortcuts: Optional[Shortcuts] = None
        #: How long the startup took (classFactory gives the one started before the plugin was imported).
        self.clock = StartupClock()
        #: 「先読み」: the Esri tables read in the background once QGIS has started (qgis_io/preload.py).
        self.preloader: Optional[Preloader] = None
        #: 担当: the roles the person ticked, which decide the features shown (core/roles.py, qgis_io/roles_io.py).
        self.role_keeper: Optional[RoleKeeper] = None
        #: Each dock's feature: its menu entry's text (core/roles.FEATURES).
        self._dock_features: dict[str, str] = {}
        #: Entries (by their text) hidden whatever the roles: 練習モード's when the settings file's `training` is false
        #: (some always in MIKAGE 練習版).
        self.hidden_entries: set[str] = {tr(t) for t in NOT_IN_TRAINING_EDITION} if edition.training_only() else set()
        #: The other version of the plugin was running when this one was turned on: this one did nothing.
        self.stood_aside = False
        #: Entries that cannot be used now (block_entries: 練習モード): their text before, and whether it was a dock's title.
        self._blocked: dict = {}
        self._algorithms_allowed: Optional[tuple] = None
        self._qgis_started = False
        self._startup_told = False
        #: The pages beside the map (features/images.py), if that feature is there.
        self.central = None
        #: The features (FEATURES), and the commands they answer to (`plugin.open_detect()`…).
        kinds = FEATURES if features is None else [f for f in FEATURES if f in features]
        if features is not None and set(features) - set(FEATURES):
            raise ValueError(f"not features of the plugin: {set(features) - set(FEATURES)}")
        self._all_features = features is None
        self.features: list[Feature] = [feature(self) for feature in kinds]
        self._features_started = False
        commands: dict = {}
        for feature in self.features:
            for name in feature.COMMANDS:
                if name in commands:
                    raise ValueError(f"two features answer to {name}")
                commands[name] = getattr(feature, name)
        self._commands = commands

    def __getattr__(self, name: str):
        # A feature's command (plugin.open_quotas() → features/requests.py's); only names not found otherwise.
        commands = self.__dict__.get("_commands")
        if commands is not None and name in commands:
            return commands[name]
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def feature(self, kind: type) -> Feature:
        """The feature of that class (features/<name>.py)."""
        return next(f for f in self.features if isinstance(f, kind))

    def initProcessing(self) -> None:  # noqa: N802 (QGIS's name)
        """
        The Processing provider 「MIKAGE」 and what its algorithms read (the settings file's AI models and cloud
        forecast, the project's saved analyses). QGIS calls it alone for `qgis_process` (metadata.txt
        hasProcessingProvider=yes: no window, no docks, the settings file read here); initGui calls it too.
        """
        if self.provider is not None or self.stood_aside:
            return
        if other_running():
            # (the other version's provider is 「mikage」 too)
            self.stood_aside = True
            return
        if self.context is None:
            # qgis_process: only the settings file, as the plugin reads it (a URL is read in a task, so a local file is surer).
            self.context = PluginContext()
            self.context.reload()
        context = self.context
        detect_algorithm.set_models(lambda: (context.config.ai_models, context.source))
        cloud_algorithm.set_forecast(lambda: context.config.cloud_forecast)
        # 保存した分析 (the project's analyses of 「データ分析」) as algorithms, following the project.
        analysis_algorithm.set_context(lambda: context)
        # アペンド: an Esri target's client and token, and which tables are the plugin's own.
        append_algorithm.set_context(lambda: context)
        self.provider = SatObsProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)
        for signal in (QgsProject.instance().readProject, QgsProject.instance().cleared):
            signal.connect(self._analyses_changed)

    def initGui(self) -> None:  # noqa: N802 (QGIS's name)
        if other_running():
            # MIKAGE and MIKAGE 練習版 turned on together: the one started first runs, this one does nothing.
            self.stood_aside = True
            self.iface.messageBar().pushWarning(
                tr(edition.name()), tr("{o} が有効なため、{n} は動かさない。どちらか一方だけを有効にして QGIS を起動し直す（プラグインの管理）")
                .format(o=edition.other_name(), n=edition.name()))
            return
        # 「最初の設定」 only the first time in this profile (asked before anything is written; not after an update).
        self.clock.lap()
        # The tasks' Python hands the GIL to QGIS's main thread sooner (qgis_io/tasks.py): the map and docks keep moving.
        tasks.share_gil_faster()
        tasks.collect_less_often()
        # Requests keep their connections to the servers between them (qgis_io/network.py).
        network.open_lines()
        # Where the Windows zip's DLLs were loaded from (outside the plugin's folder, core/win64), in the log.
        win64.set_reporter(_log_library)
        # Loaded as QGIS starts: the tasks wait until QGIS has (another plugin still being imported while a task's
        # Python ran crashed QGIS). At most a minute, should QGIS never say so.
        started = getattr(self.iface, "initializationCompleted", None)
        self.stall_watch = StallWatch()
        if started is not None and not self.iface.mainWindow().isVisible():
            tasks.hold_tasks()
            started.connect(self._release_tasks)
            QTimer.singleShot(60000, tasks.release_tasks)
        else:
            self.stall_watch.start()
        self._first_start = first_setup.is_first_start()
        window = self.iface.mainWindow()
        self.context = PluginContext(window)
        # 監査ログ before anything that records (its writing and sending run in threads of their own); none in
        # MIKAGE 練習版 (only practice: nothing of the production done).
        self.audit = None if edition.training_only() else AuditKeeper(self.iface, self.context, window)
        # 一時的なオフライン作業: the status-bar button, the checks while a server is down, and the question when it is back.
        self.offline = OfflineController(self.iface, self.context, lambda: self._after_offline(True), window,
                                         refresh_docks=lambda: self._after_offline(False))
        self.preloader = Preloader(self.context, self.clock, window)
        # 担当: what the roles ticked do not use is not shown; kept in this profile and in the settings file's `userRoles`.
        self.role_keeper = RoleKeeper(self.context, window)
        self.role_keeper.said.connect(lambda text: self.iface.messageBar().pushWarning(tr("担当"), text))
        # What listens from the start (the settings file's options, the coordinates' toolbar…), before the plugin's toolbar.
        self._features_started = True
        for feature in self.features:
            feature.start()
        self.toolbar = self.iface.addToolBar(tr(edition.name()))
        self.toolbar.setObjectName("MikageToolbar")
        self._make_docks()
        self.clock.step("ドックの作成")
        self.place_docks()
        # The docks with each other (「イベントに追加」, 「ファイルなしで記録」, the notices with the catalog…).
        for feature in self.features:
            feature.connect()
        # The docks left open now hear that they are shown (their signals waited while they were added).
        for name in INITIAL_DOCKS:
            if name in self.docks:
                self.docks[name].visibilityChanged.emit(True)
        self.initProcessing()
        # The settings file's AI models into 「物体検出（AI）」's choice.
        self.context.configChanged.connect(self._refresh_algorithms)
        for feature in self.features:
            feature.add_actions()
        # The features' entries in the map's right-click menu (MAP_MENU).
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)
        self._build_menu()
        # The toolbar: the roles' buttons (or the person's own), from the entries in menu order.
        self.toolbar_buttons = ToolbarButtons(self.toolbar, self._entries_in_menu_order(),
                                              lambda: self.role_keeper.key if self.role_keeper is not None else "", self.allows)
        # Keys for frequent actions, in QGIS's shortcut settings (ui/shortcuts.py).
        self.shortcuts = Shortcuts(self.iface, self.docks, toggle_feed=self._commands.get("toggle_notice_feed"))
        # What the roles in use do not use put away (and again whenever they change: chosen, or read from `userRoles`).
        self.role_keeper.changed.connect(self.apply_roles)
        self.apply_roles()
        self.clock.step("メニュー・ツールバー")
        self._add_preload_items()
        self.context.configChanged.connect(self._config_read_for_preload)
        self._config_asked = self.clock.now()
        self.context.reload()
        # The pages beside the map open when QGIS was last closed (behind the map): once QGIS has started
        # and put back its own window and map area (plugins load before).
        started = getattr(self.iface, "initializationCompleted", None)
        if self.central is not None:
            if self.iface.mainWindow().isVisible() or started is None:
                self.central.restore()
            else:
                started.connect(self._restore_views)
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

    def _make_docks(self) -> None:
        """The features' docks in DOCK_ORDER, each docked where DOCK_GROUPS puts it, its entry in the menu."""
        specs: dict = {}
        for feature in self.features:
            for name, spec in feature.dock_specs().items():
                if name in specs:
                    raise ValueError(f"two docks named {name}")
                specs[name] = spec
        if set(specs) != set(DOCK_ORDER) and (self._all_features or set(specs) - set(DOCK_ORDER)):
            raise ValueError(f"DOCK_ORDER and the features' docks differ: {sorted(set(specs) ^ set(DOCK_ORDER))}")
        area = {name: a for a, names in DOCK_GROUPS for name in names}
        for name in [n for n in DOCK_ORDER if n in specs]:
            spec = specs[name]
            title = tr(spec.title)
            dock = spec.create()
            # QGIS shows a dock when it is added: only the first ones stay open (QGIS then restores the layout it saved).
            # Its signals wait meanwhile, so a dock shown only for that moment reads nothing.
            dock.blockSignals(True)
            self.iface.addDockWidget(area[name], dock)
            dock.setVisible(name in INITIAL_DOCKS)
            dock.blockSignals(False)
            self.docks[name] = dock
            self._dock_features[name] = title
            # Shown although the roles in use do not have it (QGIS putting back its layout, its Panels menu): put away again.
            dock.visibilityChanged.connect(lambda on, name=name: self._keep_away(name) if on else None)
            action = dock.toggleViewAction()
            action.setText(title)
            action.setIcon(icon(spec.icon))
            self._entry_titles[action] = title
            self.add_entry(action)

    def add_entry(self, action: QAction) -> None:
        """An entry of the plugin's menu (placed in its group by `_build_menu`, MENU_GROUPS) and its list of actions."""
        self._menu_entries.append(action)
        self.actions.append(action)

    def _entry_text(self, action: QAction) -> str:
        """An entry's text as the menu and the roles know it (a dock's title, without 「通知」's unread count)."""
        return self._entry_titles.get(action, action.text())

    # ---- 「先読み」 and the startup's timings ------------------------------------------------------------

    def _add_preload_items(self) -> None:
        """The reads ahead (core/preload.ITEMS and the features' own), each through its dock's own read."""
        # 担当 first: the menus follow what the table holds for the person (or it is sent what was chosen here).
        self.preloader.add("userRoles", tr("担当"), lambda then, failed: self.role_keeper.load(then, failed), always=True)
        items = {item[0]: item for feature in self.features for item in feature.preload()}
        for key in [k for k in PRELOAD_ORDER if k in items] + [k for k in items if k not in PRELOAD_ORDER]:
            _key, label, start, always = items[key]
            self.preloader.add(key, label, start, always=always)
        self.preloader.finished.connect(self._preload_finished)

    def _release_tasks(self) -> None:
        try:
            self.iface.initializationCompleted.disconnect(self._release_tasks)
        except (TypeError, RuntimeError, AttributeError):
            pass
        tasks.release_tasks()
        # QGIS's own start (the project, the other plugins) is not a stop of the screen worth telling.
        if self.stall_watch is not None:
            self.stall_watch.start()

    def _restore_views(self) -> None:
        try:
            self.iface.initializationCompleted.disconnect(self._restore_views)
        except (TypeError, RuntimeError, AttributeError):
            pass
        # A moment later: QGIS has put back its own window layout and map area by then.
        QTimer.singleShot(0, lambda: self.central.restore() if self.central is not None else None)

    def _qgis_ready(self) -> None:
        """QGIS has started (its window shown): the reads ahead begin once the settings file is read."""
        try:
            self.iface.initializationCompleted.disconnect(self._qgis_ready)
        except (TypeError, RuntimeError, AttributeError):
            pass
        if self.context is None or self._qgis_started:
            return
        self._qgis_started = True
        for feature in self.features:
            feature.qgis_started()
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
        """The first round of reads ahead ended: the startup's timings in QGIS's log (tab MIKAGE), once."""
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

    # ---- 担当: the features of the roles in use --------------------------------------------------------

    def allows(self, feature: str) -> bool:
        """Whether the roles in use (担当) have `feature` (a menu entry's text or a dock's key, core/roles.FEATURES)."""
        if feature in self.hidden_entries:
            return False
        return self.role_keeper is None or self.role_keeper.allows(feature)

    def block_entries(self, notes: dict) -> None:
        """
        The menu entries (by their text) that cannot be used now, disabled with why after their text
        ({text: note}; {}: all of them usable again). Their buttons are the same actions: disabled too.
        """
        for action, (text, titled) in list(self._blocked.items()):
            if self._entry_text(action) not in notes:
                action.setText(text)
                action.setEnabled(True)
                if not titled:
                    self._entry_titles.pop(action, None)
                del self._blocked[action]
        for action in self._menu_entries:
            text = self._entry_text(action)
            if text in notes and action not in self._blocked:
                self._blocked[action] = (action.text(), action in self._entry_titles)
                self._entry_titles.setdefault(action, text)
                action.setText(text + notes[text])
                action.setEnabled(False)

    def dock_entry_text(self, name: str) -> str:
        """A dock's menu entry's text (its title, its 担当 feature), "" for none of that name."""
        return self._dock_features.get(name, "")

    def allowed_docks(self) -> dict:
        """The docks the roles in use have (the layouts and 「担当と個人設定」 open only these)."""
        return {n: d for n, d in self.docks.items() if self.allows(self._dock_features.get(n, ""))}

    def apply_roles(self) -> None:
        """
        The roles in use changed: the menu entries, toolbar buttons, keys,
        docks, their buttons and the Processing tools of features they do not
        have are put away; the others come back.
        """
        if self.context is None:
            return
        for action in self._menu_entries:
            action.setVisible(self.allows(self._entry_text(action)))
        for menu in self._submenus:
            menu.menuAction().setVisible(any(a.isVisible() and not a.isSeparator() for a in menu.actions()))
        for name, dock in self.docks.items():
            if not self.allows(self._dock_features.get(name, "")):
                dock.hide()
            if hasattr(dock, "set_allows"):
                dock.set_allows(self.allows)
        if self.toolbar_buttons is not None:
            self.toolbar_buttons.apply()
        if self.shortcuts is not None:
            self.shortcuts.apply(self.allows)
        if self.provider is not None:
            self.provider.allowed = self.allows
            allowed = tuple(self.allows(f) for f in (*ALGORITHM_FEATURES.values(), "サーバ処理…"))
            # (Loaded first with every tool, before the roles were known.)
            if allowed != self._algorithms_allowed and (self._algorithms_allowed is not None or not all(allowed)):
                self.provider.refreshAlgorithms()
            self._algorithms_allowed = allowed
        for feature in self.features:
            feature.roles_changed()

    def _keep_away(self, name: str) -> None:
        if self.role_keeper is not None and not self.allows(self._dock_features.get(name, "")):
            dock = self.docks.get(name)
            QTimer.singleShot(0, lambda: dock.hide() if dock is not None and name in self.docks else None)

    def entry_action(self, text: str) -> Optional[QAction]:
        """The menu entry of this text (its button on the toolbar is the same action); None when there is none."""
        return self._entries_in_menu_order().get(tr(text))

    def run_menu_entry(self, text: str) -> bool:
        """The help's 「…を開く」 links: the menu entry of that text (a dock comes to the front, never closes)."""
        action = self._entries_in_menu_order().get(tr(text))
        if action is None:
            return False
        if not self.allows(tr(text)):
            # The help links to every feature: one the roles in use do not have says how to get it.
            self.iface.messageBar().pushInfo(tr(text), tr(role_refusal(tr(text))))
            return False
        dock = next((d for d in self.docks.values() if d.toggleViewAction() is action), None)
        if dock is not None:
            dock.show()
            dock.raise_()
            return True
        if action.menu() is not None:  # a submenu (背景地図): shown where the mouse is
            action.menu().popup(QCursor.pos())
            return True
        if action.objectName() == "SatObsNoticeFeed":  # the page, not back to the map when it is in front
            self.show_notice_feed()
            return True
        action.trigger()
        return True

    # ---- the menu, the docks' places and the map's right-click menu --------------------------------------

    def text_operations(self) -> dict:
        """文で操作's operations the features offer now ({key: run(reading, place)}, features/base.Feature.text_operations)."""
        runs: dict = {}
        for feature in self.features:
            for key, run in feature.text_operations().items():
                if key in runs:
                    raise ValueError(f"two features run the operation {key}")
                runs[key] = run
        return runs

    def menu_texts(self) -> list[str]:
        """The menu's entries the roles in use have, by their text in the menu's order (文で操作's 「画面を開く」)."""
        return [t for t, a in self._entries_in_menu_order().items() if self.allows(t) and a.menu() is None]

    def _entries_in_menu_order(self) -> dict:
        """The menu's entries (text → action) in the order the menu shows them (「通知」 without its unread count)."""
        by_text = {self._entry_text(a): a for a in self._menu_entries}
        order = [t for _title, texts in MENU_GROUPS for t in texts if t] + MENU_TOP
        entries = {tr(t): by_text[tr(t)] for t in order if tr(t) in by_text}
        entries.update((t, a) for t, a in by_text.items() if t not in entries)
        return entries

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
            self.iface.addPluginToMenu(menu_name(), menu.menuAction())
        top = [by_text[tr(t)] for t in MENU_TOP if tr(t) in by_text]
        rest = [a for a in self._menu_entries if a not in placed and a not in top]
        separator = QAction(window)
        separator.setSeparator(True)
        self._menu_separator = separator
        self.iface.addPluginToMenu(menu_name(), separator)
        for action in rest + top:
            self.iface.addPluginToMenu(menu_name(), action)
            self._menu_top.append(action)

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
        """The features' entries (MAP_MENU), each at the point clicked in WGS 84."""
        canvas = self.iface.mapCanvas()
        point = event.mapPoint()
        entries: dict = {}
        for feature in self.features:
            entries.update(feature.map_menu())
        for text in [t for t in MAP_MENU if t in entries] + [t for t in entries if t not in MAP_MENU]:
            action = menu.addAction(tr(text))

            def at_point(_on=False, run=entries[text]) -> None:
                p = to_wgs84(canvas.mapSettings().destinationCrs()).transform(point)
                run(p.x(), p.y())

            action.triggered.connect(at_point)

    # ---- the Processing provider -------------------------------------------------------------------------

    def _analyses_changed(self, *_args) -> None:
        if self.provider is not None:
            analysis_algorithm.refresh()

    def _refresh_algorithms(self) -> None:
        """The settings file's AI models into the algorithm's choice."""
        if self.provider is not None:
            self.provider.refreshAlgorithms()

    def _close_processing(self) -> None:
        if self.context is not None:
            disconnect(self.context.configChanged, self._refresh_algorithms)
        for signal in (QgsProject.instance().readProject, QgsProject.instance().cleared):
            disconnect(signal, self._analyses_changed)
        analysis_algorithm.set_context(lambda: None)
        append_algorithm.set_context(lambda: None)
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        detect_algorithm.set_models(lambda: ([], ""))
        cloud_algorithm.set_forecast(lambda: None)

    def _after_offline(self, sent: bool) -> None:
        """
        The work kept offline was sent or thrown away (`sent`), or a server is back with nothing sent:
        the docks that show it read again (観測記録 then only what others changed meanwhile).
        """
        for feature in self.features:
            feature.after_offline(sent)

    def unload(self) -> None:
        if self.stood_aside:
            return
        if self.offline is not None:
            self.offline.unload()
            self.offline = None
        if self.preloader is not None:
            self.preloader.close()
            if self.context is not None:
                disconnect(self.context.configChanged, self._config_read_for_preload)
            disconnect(getattr(self.iface, "initializationCompleted", None), self._qgis_ready)
            disconnect(getattr(self.iface, "initializationCompleted", None), self._restore_views)
            self.preloader.deleteLater()
            self.preloader = None
        if self.shortcuts is not None:
            self.shortcuts.unload()
            self.shortcuts = None
        overviews_io.queue.cancel_all()
        local_copy_io.queue.cancel_all()
        # overviews.location "temp": its .ovr go as QGIS closes (what is still open, at the next start); localCopy's too.
        for end in (overviews_io.end_session, local_copy_io.end_session):
            try:
                end()
            except Exception:  # noqa: BLE001 (never keeps the plugin from unloading)
                pass
        # Before the docks and the local store go: a task ending later would write into them (#88).
        tasks.cancel_all()
        from .qgis_io import obs_images, pixel_place

        pixel_place.forget()
        obs_images.forget()
        self._let_models_go()
        tasks.release_tasks()  # nothing held any more (cancelled above): only no longer holding
        disconnect(getattr(self.iface, "initializationCompleted", None), self._release_tasks)
        tasks.restore_gil_sharing()
        tasks.restore_collection()
        if self.stall_watch is not None:
            self.stall_watch.stop()
            self.stall_watch.deleteLater()
            self.stall_watch = None
        self._close_processing()
        disconnect(self.iface.mapCanvas().contextMenuAboutToShow, self._canvas_menu)
        features = self.features if self._features_started else []
        self._features_started = False
        for feature in features:
            feature.unload()
        for action in self._menu_top:
            self.iface.removePluginMenu(menu_name(), action)
        if self._menu_separator is not None:
            self.iface.removePluginMenu(menu_name(), self._menu_separator)
            self._menu_separator = None
        for menu in self._submenus:
            self.iface.removePluginMenu(menu_name(), menu.menuAction())
            menu.deleteLater()
        self._submenus, self._menu_top, self._menu_entries = [], [], []
        self._entry_titles = {}
        self.toolbar_buttons = None
        self.actions = []
        for dock in self.docks.values():
            if hasattr(dock, "close_down"):
                dock.close_down()
            self.iface.removeDockWidget(dock)
            dock.deleteLater()
        self.docks = {}
        for feature in features:
            feature.unload_after_docks()
        # None of the plugin's map tools stays the map's (the person's own back): a tool of an unloaded plugin
        # would still answer the map's clicks (qgis_io/map_tools.py).
        map_tools.leave(self.iface.mapCanvas())
        if self.role_keeper is not None:
            self.role_keeper.deleteLater()
            self.role_keeper = None
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        if self.audit is not None:
            self.audit.unload()
            self.audit = None
        if self.context is not None:
            self.context.close()
            self.context.deleteLater()
            self.context = None
        # Last: the request threads kept with their connections end (the tasks that used them were cancelled above).
        network.close_lines()
        win64.set_reporter(None)

    def _let_models_go(self) -> None:
        """
        The AI models let go (the tasks using them cancelled before): QGIS
        unloads the plugin just before it updates or removes its folder, and
        a loaded model keeps its files open (ONNX Runtime maps a model's
        external data, model.onnx.data, while it is loaded), which Windows
        does not let QGIS overwrite or remove. A reloaded plugin would also
        load them again beside the old ones (a 3B model is about 2 GB).
        """
        from .qgis_io import ai_runtime

        released = ai_runtime.forget_models()
        assistant = getattr(self.context, "text_assist", None) if self.context is not None else None
        if assistant is not None and assistant.writer is not None:
            assistant.unload()
            released += 1
        if released:
            gc.collect()
