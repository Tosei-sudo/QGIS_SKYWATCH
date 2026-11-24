"""
The 「撮像計画」 dock (the viewer's imaging-plan-panel.ts): when can the
satellites of a catalog next take a picture of the features of a layer, the
selected features, or a point clicked on the map? It lists the opportunities
in time order; choosing one draws its ground track, reach and scenes. For
areas each pass shows how much of it it covers, and 「組み合わせ」 strings
passes together until the area is covered. The list saves as CSV, and the
scenes can be added as a layer with their times.

The calculation runs in a QgsTask (its progress in the status bar, where it
can be cancelled); the catalogs are read in tasks too.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsCollapsibleGroupBox, QgsMapToolEmitPoint
from qgis.PyQt.QtCore import QDateTime, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFormLayout, QGridLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableView,
    QVBoxLayout, QWidget,
)

from ..core.plan.api import Opportunity, PlanOptions, PlanResult, plan_access
from ..core.plan.api import load_satellites, load_sensor_modes
from ..core.plan.api import sample_sensor_modes, with_modes
from ..core.plan.api import MAX_HORIZON, MAX_SHIPS, ShipState, plan_ships
from ..core.plan.api import attach, leave_out
from ..core.plan.api import BUILT_IN_DEFAULTS, SatelliteSpec, sample_satellites
from ..qgis_io import settings
from ..qgis_io.tasks import run_task
from . import shortcuts
from .docks import PluginDock, plain_label
from .plan_drag_tool import PlanDragTool
from .plan_overlay import PlanOverlay
from .plan_timeline import PlanTimeline
from .last_inputs import InputMemory
from .list_views import column_menus
from .satellite_tree import SatelliteTree
from .plan_dock.satellites import SatellitesPart
from .plan_dock.targets import TargetsPart
from .plan_dock.results import ResultsPart
from .plan_dock.clouds import CloudsPart
from .plan_dock.pairs import PairsPart
from .plan_dock.reminders import RemindersPart
from .plan_dock.outputs import OutputsPart
from .plan_dock.common import (  # noqa: F401 (read from here too)
    MAX_ROWS, OLD_COLOR, DAYS, REMIND_EVERY, _plain, _now_ms, Column, OpportunityModel,
)


class ImagingPlanDock(
        SatellitesPart, TargetsPart, ResultsPart, CloudsPart, PairsPart, RemindersPart, OutputsPart,
        PluginDock):
    """The imaging plan."""

    #: The chosen opportunity changed (for other parts: the 3D view, later stages).
    chosenChanged = pyqtSignal()

    def __init__(self, iface, context, open_settings, parent=None):
        super().__init__("SatObsImagingPlan", QCoreApplication.translate("ImagingPlanDock", "撮像計画"), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.overlay = PlanOverlay(self.canvas)
        self.catalog_sats: list[SatelliteSpec] = []
        self.catalog_index = 0
        self.loading_task = None
        #: Counts the settings read (a catalog read under earlier settings is read again).
        self._settings_read = 0
        #: Who waits for the catalog being read.
        self._waiting: list = []
        #: Told when the catalog cannot be read (those waiting get their buttons back).
        self._waiting_failed: list = []
        self.plan_task = None
        self.clicked: Optional[tuple] = None
        self.plan: Optional[dict] = None
        self.cover: Optional[tuple] = None
        self.shown: list[Opportunity] = []
        self.chosen: Optional[Opportunity] = None
        self.sort = ("time", False)
        self._scenes: list = []
        self._drag: Optional[tuple] = None
        self.cloud_task = None
        self.revisit_task = None
        #: The pairs of the last plan in a pair view, for the conditions they were found with.
        self._pairs: Optional[tuple] = None
        #: The revisit map of the last plan: grid size, cells, their stats, the chosen cell.
        self.revisit: Optional[dict] = None
        #: Moving targets from 「移動目標撮像計画」: {"ships": [ShipState], "label": str}.
        self.moving: Optional[dict] = None
        #: Opens the 「撮像依頼」 form for 臨時 requests of opportunities (ops, sats, targets; set by the plugin).
        self.make_requests: Optional[Callable[[list, list, list], object]] = None
        #: Told each reminder shown (reminder, text, now): the 通知 dock keeps it as a notice.
        self.on_reminder: Optional[Callable] = None
        self._build()
        context.configChanged.connect(self._config_changed)
        # 衛星の障害情報 entered or read again: the satellites' marks (and their next plan) follow.
        context.outagesChanged.connect(self._outages_changed)
        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        iface.currentLayerChanged.connect(self._layer_changed)
        self._watched: Optional[QgsVectorLayer] = None
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()
        #: The conditions last used are offered first (入力の初期値の記憶).
        self.memory = InputMemory("imaging_plan", {
            "days": self.days, "max_off_nadir": self.max_off_nadir, "daylight": self.daylight, "min_sun": self.min_sun,
            "min_incidence": self.min_incidence, "max_incidence": self.max_incidence, "pass": self.pass_, "side": self.side,
            "max_cloud": self.max_cloud, "each": self.each, "priority_order": self.priority_order,
            "goal": self.goal, "share": self.share, "first": self.first,
        })
        self.memory.restore()
        self.memory.watch()
        # Reminders are checked while QGIS runs, whether the dock is open or not.
        self.remind_timer = QTimer(self)
        self.remind_timer.setInterval(REMIND_EVERY)
        self.remind_timer.timeout.connect(self.fire_reminders)
        self.remind_timer.start()

    # --- The form ---------------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        """Whether the satellite catalog was read (先読み asks before reading it)."""
        return self._loaded

    def _build(self) -> None:
        form = QFormLayout()
        self.catalogs = QComboBox()
        self.catalogs.currentIndexChanged.connect(self._catalog_chosen)
        self.catalog_row = QLabel(self.tr("衛星カタログ"))
        form.addRow(self.catalog_row, self.catalogs)
        self.target = QComboBox()
        self.target.currentIndexChanged.connect(self._target_changed)
        self.pick = QPushButton(self.tr("地図で指定"))
        self.pick.setCheckable(True)
        self.pick.setToolTip(self.tr("地図をクリックして対象の地点を指定"))
        self.pick.toggled.connect(self._pick_toggled)
        target_row = QHBoxLayout()
        target_row.addWidget(self.target, 1)
        target_row.addWidget(self.pick)
        form.addRow(self.tr("対象"), target_row)
        self.start = QDateTimeEdit(QDateTime.currentDateTime())
        self.start.setDisplayFormat("yyyy/MM/dd HH:mm")
        self.start.setCalendarPopup(True)
        form.addRow(self.tr("開始"), self.start)
        self.days = QComboBox()
        for d in DAYS:
            self.days.addItem(self.tr("{n} 日").format(n=d), d)
        self.days.setCurrentIndex(DAYS.index(7))
        form.addRow(self.tr("期間"), self.days)
        self.max_off_nadir = QDoubleSpinBox()
        self.max_off_nadir.setRange(-1, 80)
        self.max_off_nadir.setDecimals(1)
        self.max_off_nadir.setSpecialValueText(self.tr("衛星の上限"))
        self.max_off_nadir.setValue(-1)
        self.max_off_nadir.setSuffix("°")
        form.addRow(self.tr("オフナディア角（以下）"), self.max_off_nadir)
        # The form scrolls above; the button, the status and the list stay below it.
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addLayout(form)

        more = QgsCollapsibleGroupBox(self.tr("詳細条件"))
        more.setCollapsed(True)
        self.more = more
        # Expanding shows every row again: those that do not apply are hidden once more.
        more.collapsedStateChanged.connect(lambda collapsed: collapsed or self._hide_rows())
        self._priority_shown = False
        more_layout = QVBoxLayout(more)
        self.tree = SatelliteTree(remember_as="imaging_plan")
        more_layout.addWidget(self.tree)
        options = QFormLayout()
        self.daylight = QComboBox()
        self.daylight.addItem(self.tr("光学衛星は日中のみ"), "optical")
        self.daylight.addItem(self.tr("考慮なし"), "none")
        options.addRow(self.tr("日照条件"), self.daylight)
        self.min_sun = QDoubleSpinBox()
        self.min_sun.setRange(-18, 90)
        self.min_sun.setDecimals(1)
        self.min_sun.setValue(10)
        self.min_sun.setSuffix("°")
        options.addRow(self.tr("太陽高度（以上）"), self.min_sun)
        self.min_incidence = self._angle(self.tr("下限"))
        self.max_incidence = self._angle(self.tr("上限"))
        incidence = QHBoxLayout()
        incidence.addWidget(self.min_incidence)
        incidence.addWidget(QLabel("〜"))
        incidence.addWidget(self.max_incidence)
        label = QLabel(self.tr("入射角"))
        label.setToolTip(self.tr("地表から見た衛星の視線と鉛直のなす角。範囲をそろえると見え方（SAR の明るさ・建物の倒れ込み）も一定に"))
        options.addRow(label, incidence)
        self.pass_ = QComboBox()
        self.pass_.addItem(self.tr("北行・南行"), None)
        self.pass_.addItem(self.tr("北行のみ"), "ascending")
        self.pass_.addItem(self.tr("南行のみ"), "descending")
        options.addRow(self.tr("軌道"), self.pass_)
        self.side = QComboBox()
        self.side.addItem(self.tr("左右とも"), None)
        self.side.addItem(self.tr("右のみ"), "right")
        self.side.addItem(self.tr("左のみ"), "left")
        options.addRow(self.tr("向き"), self.side)
        self.cloud = QCheckBox()
        self.cloud.setToolTip(self.tr("光学衛星の撮像機会ごとに、その時刻・地点の雲量予報（16 日先まで）を表示。対象の位置を予報サービスへ送信"))
        self.cloud.setChecked(settings.plan_cloud())
        self.cloud.toggled.connect(self._cloud_toggled)
        self.max_cloud = QSpinBox()
        self.max_cloud.setRange(-1, 100)
        self.max_cloud.setSpecialValueText(self.tr("制限なし"))
        self.max_cloud.setValue(-1)
        self.max_cloud.setSuffix("%")
        self.max_cloud.setToolTip(self.tr("雲量予報がこの値を超える光学衛星の機会を除外（予報のない機会は残す）"))
        self.max_cloud.valueChanged.connect(self._max_cloud_changed)
        cloud_row = QHBoxLayout()
        cloud_row.setContentsMargins(0, 0, 0, 0)
        cloud_row.addWidget(self.cloud)
        cloud_row.addWidget(QLabel(self.tr("雲量（以下）")))
        cloud_row.addWidget(self.max_cloud)
        cloud_row.addStretch(1)
        self.cloud_row = QWidget()
        self.cloud_row.setLayout(cloud_row)
        self.cloud_label = QLabel(self.tr("天候"))
        options.addRow(self.cloud_label, self.cloud_row)
        self.each = QCheckBox(self.tr("地物ごとに計算"))
        self.each.setChecked(True)
        self.each.toggled.connect(self._target_changed)
        options.addRow(self.each)
        self.name_field = QComboBox()
        self.name_field.setToolTip(self.tr("一覧・CSV・レイヤーで対象の名前に使う属性。自動は name・名称 などを探し、なければ ID"))
        self.name_field.currentIndexChanged.connect(self._relabel)
        options.addRow(self.tr("名前の属性"), self.name_field)
        self.priority = QComboBox()
        self.priority.setToolTip(self.tr("組み合わせで、優先度の高い地物から衛星の撮像を割り当て（撮像間隔・1 日の上限で競合するときは高い方を優先）"))
        self.priority_label = QLabel(self.tr("優先度の属性"))
        self.priority_label.setToolTip(self.priority.toolTip())
        options.addRow(self.priority_label, self.priority)
        self.priority_order = QComboBox()
        self.priority_order.addItem(self.tr("小さいほど優先（1 が最優先）"), "small")
        self.priority_order.addItem(self.tr("大きいほど優先"), "large")
        self.priority_order_label = QLabel(self.tr("優先する値"))
        options.addRow(self.priority_order_label, self.priority_order)
        more_layout.addLayout(options)
        self.tle = QPlainTextEdit()
        self.tle.setPlaceholderText("ISS (ZARYA)\n1 25544U 98067A   …\n2 25544  51.6416 …")
        self.tle.setFixedHeight(64)
        self.tle.textChanged.connect(self.list_sats)
        more_layout.addWidget(QLabel(self.tr("TLE を貼り付けて追加")))
        more_layout.addWidget(self.tle)
        pasted = QGridLayout()
        self.paste_max = self._number(0, 80, BUILT_IN_DEFAULTS["maxOffNadir"], "°")
        self.paste_swath = self._number(0.1, 10000, BUILT_IN_DEFAULTS["swath"], " km")
        self.paste_gap = self._number(0, 100000, 0, self.tr(" 分"))
        self.paste_gap.setToolTip(self.tr("1 回撮像してから次に撮像できるまでの時間。0 は制約なし"))
        self.paste_per_day = QSpinBox()
        self.paste_per_day.setRange(0, 1000)
        self.paste_per_day.setToolTip(self.tr("24 時間に撮像できる回数。0 は制約なし"))
        self.paste_lead = self._number(0, 10000, 0, self.tr(" 時間"))
        self.paste_lead.setToolTip(self.tr("オーダーできる撮像の最短時間（何時間先から）。それより近い機会は除外。0 は制約なし"))
        self.paste_sar = QCheckBox(self.tr("SAR（夜間も撮像）"))
        for i, (text, widget) in enumerate([
            (self.tr("最大オフナディア角"), self.paste_max), (self.tr("観測幅"), self.paste_swath),
            (self.tr("撮像間隔"), self.paste_gap), (self.tr("1 日の上限（回）"), self.paste_per_day),
            (self.tr("発注リード"), self.paste_lead),
        ]):
            pasted.addWidget(QLabel(text), i // 2, (i % 2) * 2)
            pasted.addWidget(widget, i // 2, (i % 2) * 2 + 1)
            widget.valueChanged.connect(self.list_sats)
        pasted.addWidget(self.paste_sar, 2, 2, 1, 2)
        self.paste_sar.toggled.connect(self.list_sats)
        more_layout.addLayout(pasted)
        top_layout.addWidget(more)
        top_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(top)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(scroll)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setChildrenCollapsible(False)
        self.body.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        self.run_button = QPushButton(self.tr("計算"))
        self.run_button.setToolTip(self.tr("計算（Ctrl+Enter）"))
        self.run_button.clicked.connect(self.run)
        self.cancel_button = QPushButton(self.tr("取消"))
        self.cancel_button.setToolTip(self.tr("計算の取消（Esc）"))
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        buttons.addWidget(self.run_button, 1)
        buttons.addWidget(self.cancel_button)
        bottom_layout.addLayout(buttons)
        # Target names from layers and the services' messages: shown as written, never as HTML.
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom_layout.addWidget(self.status)

        view = QHBoxLayout()
        self.view = QComboBox()
        self.view.addItem(self.tr("すべての機会"), "all")
        self.view.addItem(self.tr("組み合わせ（全域を最短で）"), "cover")
        self.view.addItem(self.tr("ステレオペア"), "stereo")
        self.view.addItem(self.tr("干渉ペア（同じ軌道・SAR）"), "repeat")
        self.view.addItem(self.tr("再訪間隔マップ"), "revisit")
        self.view.currentIndexChanged.connect(self.show_view)
        self.goal = QSpinBox()
        self.goal.setRange(10, 100)
        self.goal.setValue(95)
        self.goal.setPrefix(self.tr("目標 "))
        self.goal.setSuffix("%")
        self.goal.setToolTip(self.tr("面の対象をどこまで覆えば完了とするか"))
        self.goal.valueChanged.connect(self.show_view)
        self.share = QCheckBox(self.tr("近い地点を 1 シーンに集約"))
        self.share.setChecked(True)
        self.share.setToolTip(self.tr("同じパスで 1 シーン（観測幅 × シーン長）に収まる地点を、1 回の撮像でまとめて撮像"))
        self.share.toggled.connect(self.show_view)
        self.first = QCheckBox(self.tr("衛星・対象ごとに最初の機会のみ"))
        self.first.toggled.connect(self.render)
        self.timeline_box = QCheckBox(self.tr("タイムライン"))
        self.timeline_box.setToolTip(self.tr("衛星ごとの撮像を時間軸に並べて表示（薄い帯は撮像間隔で次を撮れない時間）"))
        self.timeline_box.toggled.connect(self.draw_timeline)
        for w in (self.view, self.goal, self.share, self.first, self.timeline_box):
            view.addWidget(w)
        view.addStretch(1)
        self.view_bar = QWidget()
        self.view_bar.setLayout(view)
        view.setContentsMargins(0, 0, 0, 0)
        self.view_bar.hide()
        bottom_layout.addWidget(self.view_bar)
        bottom_layout.addWidget(self._pair_options())
        self.timeline = PlanTimeline()
        self.timeline.hide()
        self.timeline.picked.connect(self.choose)
        bottom_layout.addWidget(self.timeline)

        self.model = OpportunityModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().sectionClicked.connect(self._sort_by)
        self.table.selectionModel().currentRowChanged.connect(lambda current, _: self._row_chosen(current.row()))
        self.table.doubleClicked.connect(lambda _: self.zoom_to_chosen())
        bottom_layout.addWidget(self.table, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.table_columns = column_menus("imaging-plan", self.table, self)
        self.revisit_model = QStandardItemModel(self)
        self.revisit_table = QTableView()
        self.revisit_table.setModel(self.revisit_model)
        self.revisit_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.revisit_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.revisit_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.revisit_table.verticalHeader().hide()
        self.revisit_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.revisit_table.selectionModel().currentRowChanged.connect(lambda current, _: self._cell_chosen(current.row()))
        self.revisit_table.doubleClicked.connect(lambda _: self.zoom_to_cell())
        self.revisit_table.hide()
        bottom_layout.addWidget(self.revisit_table, 1)
        self.revisit_columns = column_menus("imaging-plan-revisit", self.revisit_table, self)

        actions = QHBoxLayout()
        self.csv_button = QPushButton(self.tr("CSV 保存"))
        self.csv_button.setToolTip(self.tr("一覧を CSV で保存"))
        self.csv_button.clicked.connect(self.save_csv)
        self.layer_button = QPushButton(self.tr("レイヤーとして追加"))
        self.layer_button.setToolTip(self.tr("撮像範囲をレイヤーとして追加（撮像時刻付き。時系列コントローラーで絞り込める）"))
        self.layer_button.clicked.connect(self.add_layer)
        self.zoom_button = QPushButton(self.tr("範囲へ移動"))
        self.zoom_button.clicked.connect(self.zoom_to_chosen)
        self.move_button = QPushButton(self.tr("撮像範囲を移動"))
        self.move_button.setCheckable(True)
        self.move_button.setToolTip(self.tr("選んだ回の撮像範囲（青）を地図上でドラッグして移動。衛星が向けられる範囲内で、角度と被覆を再計算"))
        self.move_button.toggled.connect(self._move_toggled)
        for b in (self.csv_button, self.layer_button, self.move_button, self.zoom_button):
            actions.addWidget(b)
        bottom_layout.addLayout(actions)
        bottom_layout.addLayout(self._later_actions())
        bottom_layout.addWidget(self.remind_row)
        self._update_buttons()

        # Shortcuts while the dock has the focus only.
        shortcuts.dock_key(self, "PlanRun", "撮像計画（ドック内）: 計算", "Ctrl+Return", self.run)
        shortcuts.dock_key(self, "PlanRunKeypad", "撮像計画（ドック内）: 計算（テンキーの Enter）", "Ctrl+Enter", self.run)
        shortcuts.dock_key(self, "PlanCancel", "撮像計画（ドック内）: 中止", "Esc", self.cancel)

        self.map_tool = QgsMapToolEmitPoint(self.canvas)
        self.map_tool.canvasClicked.connect(self._map_clicked)
        self.map_tool.deactivated.connect(lambda: self.pick.setChecked(False))
        self.drag_tool = PlanDragTool(self.canvas, lambda: self._scenes, self.drag_begin, self.drag_move, self.drag_end)
        self.drag_tool.deactivated.connect(lambda: self.move_button.setChecked(False))

    @staticmethod
    def _angle(placeholder: str) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(-1, 89)
        box.setDecimals(1)
        box.setSpecialValueText(placeholder)
        box.setValue(-1)
        box.setSuffix("°")
        return box

    @staticmethod
    def _number(low: float, high: float, value: float, suffix: str) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(1)
        box.setValue(value)
        box.setSuffix(suffix)
        return box

    # --- Satellites -------------------------------------------------------------------------

    def _config_changed(self) -> None:
        config = self.context.config
        self.catalogs.blockSignals(True)
        self.catalogs.clear()
        for c in config.satellite_catalogs:
            self.catalogs.addItem(c.label)
        self.catalogs.blockSignals(False)
        many = len(config.satellite_catalogs) > 1
        self.catalogs.setVisible(many)
        self.catalog_row.setVisible(many)
        self.catalog_index = 0
        self.catalog_sats = []
        self._loaded = False
        # A read still running is of the settings before: it reads again when done.
        self._settings_read += 1
        forecast = config.cloud_forecast
        self._hide_rows()
        if forecast is not None:
            self.cloud.setText(self.tr("雲量予報（{label}）").format(label=forecast.label))
            self.cloud.setToolTip(self.tr("光学衛星の撮像機会ごとに、その時刻と地点の雲量予報（16 日先まで）を表示。取り込んだ予報ファイルを使用（どこにも送信しない）") if forecast.local else
                                  self.tr("光学衛星の撮像機会ごとに、その時刻・地点の雲量予報（16 日先まで）を表示。対象の位置を予報サービスへ送信"))
        self.request_button.setVisible(bool(config.request_stores) and self.allows("requests.edit"))
        if not config.satellite_catalogs:
            # Without a catalog, two fixed sample satellites stand in (the SAR one with three made-up modes, unless the settings have sensors).
            self.catalog_sats = with_modes(sample_satellites(), [] if config.sensor_catalogs else sample_sensor_modes())
            self._loaded = True
            self.list_sats()
            self.status.setText(self.tr(
                "衛星カタログが未設定のため、架空のサンプル衛星 2 機（光学と SAR）で計算。"
                "「詳細条件」で TLE を貼り付けると追加できる（管理者に連絡: satelliteCatalogs）［E703］"))
        else:
            self.list_sats()
            self.status.setText("")
            if self.isVisible():
                self.load_catalog()

    def load_catalog(self, then: Optional[Callable[[], None]] = None,
                     failed: Optional[Callable[[str], None]] = None) -> None:
        """
        Reads the chosen catalog and the sensors' modes in a task; `then` once
        read, `failed` (with the reason) when it cannot be: whoever waits is
        always told, so its buttons come back (#70).
        """
        config = self.context.config
        if self._loaded or not config.satellite_catalogs:
            if then:
                then()
            return
        if self.loading_task is not None:
            # Read once: whoever else waits (the satellite pass dock) is told too.
            if then:
                self._waiting.append(then)
            if failed:
                self._waiting_failed.append(failed)
            return
        catalog = config.satellite_catalogs[self.catalog_index]
        sensors = config.sensor_catalogs
        token_for = self.context.token_for
        client = self.context.esri()
        read = self._settings_read
        self.status.setText(self.tr("衛星カタログを取得中…"))

        def work(task):
            sats, problems = load_satellites(client, catalog, token_for, task.isCanceled)
            names = self.context.satellite_names(client, task.isCanceled)
            if names:
                sats = [replace(s, aliases=tuple(sorted(names.expand(s.name, s.id)))) for s in sats]
            task.progress(70)
            modes, more = load_sensor_modes(client, sensors, token_for, task.isCanceled)
            outages, problem = self.context.read_outages(client, task.isCanceled)
            return sats, modes, problems + more + ([problem] if problem else []), outages

        def done(result):
            self.loading_task = None
            if read != self._settings_read:
                # The settings changed meanwhile (their satellites, defaults): read again; whoever waits is told then.
                self.load_catalog(then, failed)
                return
            sats, modes, problems, outages = result
            # Each mode with the outages that hit it (衛星の障害情報): marked in the list, its passes left out.
            self.catalog_sats = attach(with_modes(sats, modes), outages)
            self._loaded = True
            self.context.set_outages(outages)
            self.list_sats()
            n_modes = len(self.tree.choice.mode_names())
            text = self.tr("{label}: {n} 機").format(label=catalog.label, n=len(sats))
            if n_modes:
                text += self.tr("（撮像モード {n} 種）").format(n=n_modes)
            if problems:
                text += self.tr("（読めなかったもの {n} 件: {list}）").format(n=len(problems), list="、".join(problems[:3]) + (" …" if len(problems) > 3 else ""))
            self.status.setText(text + self.tr("。対象と期間を選んで「計算」"))
            waiting, self._waiting, self._waiting_failed = self._waiting, [], []
            for f in ([then] if then else []) + waiting:
                f()

        def not_read(message):
            self.loading_task = None
            told, self._waiting, self._waiting_failed = self._waiting_failed, [], []
            self.status.setText(self.tr("衛星カタログの取得に失敗: {message}").format(message=message))
            for f in ([failed] if failed else []) + told:
                f(message)

        self.loading_task = run_task(self.tr("衛星カタログの取得"), work, done, not_read)


    # --- Targets ----------------------------------------------------------------------------


    # --- Moving the chosen scenes ------------------------------------------------------


    # --- Planning -----------------------------------------------------------------------------

    def plan_options(self) -> PlanOptions:
        start = self.start.dateTime().toMSecsSinceEpoch()
        days = self.days.currentData() or 7

        def angle(box: QDoubleSpinBox) -> Optional[float]:
            return None if box.value() < 0 else box.value()

        return PlanOptions(
            start=start,
            end=start + days * 86400000,
            maxOffNadir=angle(self.max_off_nadir),
            daylight=self.daylight.currentData(),
            minSunElevation=self.min_sun.value(),
            now=_now_ms(),
            minIncidence=angle(self.min_incidence),
            maxIncidence=angle(self.max_incidence),
            pass_=self.pass_.currentData(),
            side=self.side.currentData(),
            stations=list(self.context.config.ground_stations),
        )

    def run(self) -> None:
        """Plans the checked satellites over the targets, in a task."""
        if not self._loaded:
            self.load_catalog(self.run)
            return
        sats = self.tree.checked()
        ships = self.ships()
        targets = [] if ships else self.targets()
        if isinstance(targets, str):
            self.status.setText(targets)
            return
        if not sats:
            self.more.setCollapsed(False)
            self.status.setText(self.tr("衛星が未選択（詳細条件で選択）") if self.tree.items() else self.tr("衛星なし。衛星カタログを確認するか、「詳細条件」に TLE を貼り付け"))
            return
        if ships:
            self.run_ships(sats, ships)
            return
        options = self.plan_options()
        self.cancel()
        what = self.tr("{s} 機 × {t} 対象を計算中…").format(s=len(sats), t=len(targets))
        self.status.setText(what)
        client = self.context.esri()

        def work(task):
            def progress(done: float) -> None:
                task.check()
                task.progress(done * 100)

            # The outages as they are now (one just entered counts): their passes left out (衛星の障害情報).
            outages, _problem = self.context.read_outages(client, task.isCanceled)
            planned = attach(sats, outages)
            result = plan_access(planned, targets, options, progress)
            leave_out(result, planned, time.time() * 1000)
            return result

        def done(result: PlanResult):
            if self.plan_task is not task:
                return
            self.plan_task = None
            self._running(False)
            # An area no single pass covers: start with the combination that covers it.
            best: dict[int, float] = {}
            for op in result.opportunities:
                if op.cells is not None:
                    best[op.target] = max(best.get(op.target, 0), op.coverage)
            self.show_plan({"sats": sats, "targets": targets, "result": result, "options": options},
                           "cover" if any(c < 0.95 for c in best.values()) else "all")

        def failed(message: str):
            if self.plan_task is not task:
                return
            self.plan_task = None
            self._running(False)
            self.status.setText(self.tr("計算の取消") if message == "取消" else self.tr("計算に失敗: {m}").format(m=message))

        task = run_task(what, work, done, failed)
        self.plan_task = task
        self._running(True)

    def ships(self) -> Optional[list[ShipState]]:
        """The moving targets when the target choice holds them; None otherwise."""
        if self.target.currentData() == "moving" and self.moving:
            return list(self.moving["ships"])
        return None

    def plan_moving(self, ships: list[ShipState], label: str) -> None:
        """
        Plans moving targets (from 「移動目標撮像計画」): they become the target
        choice, and the plan is made at once when satellites are chosen.
        """
        self.moving = {"ships": list(ships), "label": label}
        self.show()
        self.raise_()
        self.list_targets()
        self.target.setCurrentIndex(max(0, self.target.findData("moving")))
        # Reports older than the horizon: plan from the latest of them, as nothing can be guessed later.
        latest = max(s.t for s in ships)
        if latest + MAX_HORIZON * 3600000 < self.start.dateTime().toMSecsSinceEpoch():
            self.start.setDateTime(QDateTime.fromMSecsSinceEpoch(int(latest)))
        self.show_targets()
        self.run()

    def run_ships(self, sats: list[SatelliteSpec], ships: list[ShipState]) -> None:
        """
        Plans moving targets in a task: where each will be (carried on along
        its course at its speed, up to 72 hours after its last report) is
        planned every hour, then each pass found is aimed again at the circle
        around where the target is at that moment.
        """
        options = self.plan_options()
        self.cancel()
        what = self.tr("{s} 機 × 移動目標 {n} 件の推測位置を計算中…").format(s=len(sats), n=min(len(ships), MAX_SHIPS))
        self.status.setText(what)

        def work(task):
            def progress(done: float) -> None:
                task.check()
                task.progress(done * 100)

            out = plan_ships(sats, ships, options, progress, task.isCanceled)
            if not isinstance(out, str):
                leave_out(out[1], sats, time.time() * 1000)
            return out

        def done(out):
            if self.plan_task is not task:
                return
            self.plan_task = None
            self._running(False)
            if isinstance(out, str):
                self.status.setText(out)
                return
            circles, result, planned = out
            self.show_plan({"sats": sats, "targets": circles, "result": result, "options": planned}, "all")

        def failed(message: str):
            if self.plan_task is not task:
                return
            self.plan_task = None
            self._running(False)
            self.status.setText(self.tr("計算の取消") if message == "取消" else self.tr("計算に失敗: {m}").format(m=message))

        task = run_task(what, work, done, failed)
        self.plan_task = task
        self._running(True)

    def cancel(self) -> None:
        task = self.plan_task
        if task is not None:
            self.plan_task = None
            task.cancel()
            self._running(False)
            self.status.setText(self.tr("計算の取消"))

    def _running(self, on: bool) -> None:
        self.cancel_button.setEnabled(on)
        self.run_button.setEnabled(not on)

    def show_plan(self, plan: dict, view: str) -> None:
        self.plan = plan
        self.cover = None
        self._pairs = None
        self.revisit = None
        if self.revisit_task is not None:
            self.revisit_task.cancel()
            self.revisit_task = None
        self.overlay.unpaint()
        self.view_bar.setVisible(bool(plan["result"].opportunities))
        self.view.blockSignals(True)
        self.view.setCurrentIndex(max(0, self.view.findData(view)))
        self.view.blockSignals(False)
        self.show_view()
        if self.cloud.isChecked() and self.context.config.cloud_forecast is not None:
            self.load_clouds()

    def _update_buttons(self) -> None:
        any_ = bool(self.shown)
        cells = self.revisit_shown()
        self.csv_button.setEnabled(any_ or cells)
        self.layer_button.setEnabled(any_ or cells)
        self.remind_button.setEnabled(any_)
        if not any_ and self.remind_button.isChecked():
            self.remind_button.setChecked(False)
        self.request_button.setEnabled(any_)
        self.request_chosen.setEnabled(self.chosen is not None)
        self.zoom_button.setEnabled(self.chosen is not None)
        self.move_button.setEnabled(self.chosen is not None)
        if self.chosen is None and self.move_button.isChecked():
            self.move_button.setChecked(False)

    # --- Pairs, the revisit map and the later actions (their widgets) --------------------------


    # --- Cloud forecasts --------------------------------------------------------------------


    # --- Pairs ---------------------------------------------------------------------------------


    # --- The revisit map -----------------------------------------------------------------------


    # --- Reminders and the calendar ----------------------------------------------------------


    # --- Saving to a plan store -------------------------------------------------------------

    def roles_changed(self) -> None:
        """「臨時依頼を作成」: for those who ask for images or plan (core/roles.py)."""
        self.request_button.setVisible(bool(self.context.config.request_stores) and self.allows("requests.edit"))


    # --- Output ---------------------------------------------------------------------------


    # --- Showing and hiding ---------------------------------------------------------------

    def _visibility(self, visible: bool) -> None:
        if visible:
            self.list_targets()
            if self.context.config.satellite_catalogs and not self._loaded:
                self.load_catalog()
        else:
            self.overlay.clear()
            for button in (self.pick, self.move_button):
                if button.isChecked():
                    button.setChecked(False)

    def close_down(self) -> None:
        """Called when the plugin unloads."""
        self.remind_timer.stop()
        self.cancel()
        for task in (self.revisit_task, self.cloud_task):
            if task is not None:
                task.cancel()
        self.revisit_task = self.cloud_task = None
        for button in (self.pick, self.move_button):
            if button.isChecked():
                button.setChecked(False)
        self.overlay.remove()
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved):
            try:
                signal.disconnect(self._layers_changed)
            except (TypeError, RuntimeError):
                pass
        try:
            self.iface.currentLayerChanged.disconnect(self._layer_changed)
        except (TypeError, RuntimeError):
            pass
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(self._selection_changed)
            except (TypeError, RuntimeError):
                pass
