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

import math
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional

from qgis.core import Qgis, QgsFeatureRequest, QgsGeometry, QgsMapLayer, QgsMessageLog, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer
from qgis.gui import QgsCollapsibleGroupBox, QgsMapToolEmitPoint
from qgis.PyQt.QtCore import QAbstractTableModel, QDateTime, QModelIndex, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QBrush, QColor, QKeySequence, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableView,
    QVBoxLayout, QWidget,
)

try:  # Qt6
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QShortcut

from ..core.plan.access import Opportunity, PlanOptions, PlanResult, PlanTarget, plan_access
from ..core.plan.catalogs import load_satellites, load_sensor_modes
from ..core.plan.cover import cover_plan
from ..core.plan.modes import sample_sensor_modes, with_modes
from ..core.plan.output import (
    NO_OPPORTUNITY, cover_text, cover_title, describe_problems, first_only, is_old_tle, js_round, local_text, mode_facts,
    natural_key, percent, plan_csv, plan_features, side_text, sorted_ops, summary_all, summary_cover,
)
from ..core.plan.moving import MAX_HORIZON, MAX_SHIPS, ShipState, plan_ships
from ..core.plan.pairs import RepeatOptions, StereoOptions, repeat_pairs, stereo_pairs
from ..core.plan.remind import due_reminders, merge_reminders, new_reminders, plan_ics, reminder_of, reminder_text
from ..core.plan.revisit import (
    box_of, cell_targets, grid_cells, hours_text, revisit_color, revisit_csv, revisit_layer, revisit_stats,
)
from ..core.plan.satellites import BUILT_IN_DEFAULTS, SatelliteSpec, sample_satellites, satellite_label, satellites_from_text
from ..core.plan.scene import pass_shapes
from ..core.plan.scene import shift_scene, track_place
from ..core.plan.store import add_shots, new_plan_id, shot_features
from ..core.plan.targets import MAX_TARGETS, name_of, priority_of, target_of
from ..core.plan.weather import apply_clouds, fetch_clouds, places_for
from ..qgis_io import settings
from ..qgis_io.network import get_bytes
from ..qgis_io.geometry import WGS84, from_wgs84, shape_of, to_wgs84
from ..qgis_io.plan_layer import memory_layer
from ..qgis_io.tasks import run_task
from .docks import PluginDock
from .plan_drag_tool import PlanDragTool
from .plan_overlay import PlanOverlay
from .plan_timeline import PlanTimeline
from .satellite_tree import SatelliteTree

#: The most opportunities listed as rows (the CSV and the layer take them all).
MAX_ROWS = 2000
OLD_COLOR = QColor("#b45309")
DAYS = (1, 3, 7, 14, 30)
#: How often the reminders are checked, ms.
REMIND_EVERY = 20000


def _plain(value):
    """An attribute value as Python (a NULL QVariant as None)."""
    if value is None:
        return None
    if hasattr(value, "isNull") and value.isNull():
        return None
    return value


def _now_ms() -> float:
    return time.time() * 1000


class Column:
    def __init__(self, key: str, label: str, text: Callable[[Opportunity], str], title: Optional[Callable[[Opportunity], str]] = None):
        self.key, self.label, self.text, self.title = key, label, text, title


class OpportunityModel(QAbstractTableModel):
    """The listed opportunities, as the viewer's table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns: list[Column] = []
        self.ops: list[Opportunity] = []
        self.sats: list[SatelliteSpec] = []
        self.rest = 0

    def set(self, columns: list[Column], ops: list[Opportunity], sats: list[SatelliteSpec]) -> None:
        self.beginResetModel()
        self.columns, self.sats = columns, sats
        self.ops = ops[:MAX_ROWS]
        self.rest = max(0, len(ops) - MAX_ROWS)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 (Qt's name)
        return 0 if parent.isValid() else len(self.ops) + (1 if self.rest else 0)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and section < len(self.columns):
            return self.columns[section].label
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, column = index.row(), self.columns[index.column()]
        if row >= len(self.ops):
            if role == Qt.ItemDataRole.DisplayRole and index.column() == 0:
                return self.tr("ほか {n} 件（並べ替えで先頭が変化。CSV とレイヤーにはすべて出力）").format(n=f"{self.rest:,}")
            return None
        op = self.ops[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.text(op)
        if role == Qt.ItemDataRole.ToolTipRole and column.title:
            return column.title(op) or None
        if role == Qt.ItemDataRole.ForegroundRole and column.key == "satellite" and is_old_tle(op, self.sats[op.satellite]):
            return QBrush(OLD_COLOR)
        return None

    def op_at(self, row: int) -> Optional[Opportunity]:
        return self.ops[row] if 0 <= row < len(self.ops) else None


class ImagingPlanDock(PluginDock):
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
        #: Who waits for the catalog being read.
        self._waiting: list = []
        self.plan_task = None
        self.clicked: Optional[tuple] = None
        self.plan: Optional[dict] = None
        self.cover: Optional[tuple] = None
        self.shown: list[Opportunity] = []
        self.chosen: Optional[Opportunity] = None
        self.sort = ("time", False)
        self._previous_tool = None
        self._scenes: list = []
        self._drag: Optional[tuple] = None
        self.cloud_task = None
        self.revisit_task = None
        self.store_task = None
        #: The pairs of the last plan in a pair view, for the conditions they were found with.
        self._pairs: Optional[tuple] = None
        #: The revisit map of the last plan: grid size, cells, their stats, the chosen cell.
        self.revisit: Optional[dict] = None
        #: Moving targets from 「移動目標撮像計画」: {"ships": [ShipState], "label": str}.
        self.moving: Optional[dict] = None
        #: Opens the 「保存した計画」 dock (set by the plugin).
        self.open_saved: Optional[Callable[[], None]] = None
        self._build()
        context.configChanged.connect(self._config_changed)
        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        iface.currentLayerChanged.connect(self._layer_changed)
        self._watched: Optional[QgsVectorLayer] = None
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()
        # Reminders are checked while QGIS runs, whether the dock is open or not.
        self.remind_timer = QTimer(self)
        self.remind_timer.setInterval(REMIND_EVERY)
        self.remind_timer.timeout.connect(self.fire_reminders)
        self.remind_timer.start()

    # --- The form ---------------------------------------------------------------------------

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
        self.tree = SatelliteTree()
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
        self.paste_lead.setToolTip(self.tr("オーダー可能な撮像の最短時間（何時間先から）。それより近い機会は除外。0 は制約なし"))
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
        self.status = QLabel()
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

        actions = QHBoxLayout()
        self.csv_button = QPushButton(self.tr("CSV 保存"))
        self.csv_button.setToolTip(self.tr("一覧を CSV で保存"))
        self.csv_button.clicked.connect(self.save_csv)
        self.layer_button = QPushButton(self.tr("レイヤーとして追加"))
        self.layer_button.setToolTip(self.tr("撮像範囲をレイヤーとして追加（撮像時刻付き。時系列コントローラーで絞り込み可能）"))
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
        bottom_layout.addWidget(self.save_row)
        self._update_buttons()

        # Shortcuts while the dock has the focus only.
        for keys, slot in (("Ctrl+Return", self.run), ("Ctrl+Enter", self.run), ("Esc", self.cancel)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

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
        forecast = config.cloud_forecast
        self._hide_rows()
        if forecast is not None:
            self.cloud.setText(self.tr("雲量予報（{label}）").format(label=forecast.label))
        self.store_choice.clear()
        for store in config.plan_stores:
            self.store_choice.addItem(store.label)
        self.store_choice.setVisible(len(config.plan_stores) > 1)
        for button in (self.store_button, self.saved_button):
            button.setVisible(bool(config.plan_stores))
        if not config.plan_stores and self.store_button.isChecked():
            self.store_button.setChecked(False)
        if not config.satellite_catalogs:
            # Without a catalog, two fixed sample satellites stand in (the SAR one with three made-up modes, unless the settings have sensors).
            self.catalog_sats = with_modes(sample_satellites(), [] if config.sensor_catalogs else sample_sensor_modes())
            self._loaded = True
            self.list_sats()
            self.status.setText(self.tr(
                "設定ファイルに衛星カタログ（satelliteCatalogs）がないため、サンプル衛星 2 機（光学・SAR。SAR はストリップマップ・"
                "スポットライト・スキャンSAR の 3 モード。架空の衛星）で計算。「詳細条件」で TLE を貼り付けて追加可能"))
        else:
            self.list_sats()
            self.status.setText("")
            if self.isVisible():
                self.load_catalog()

    def _catalog_chosen(self, index: int) -> None:
        if index < 0:
            return
        self.catalog_index = index
        self._loaded = False
        self.load_catalog()

    def load_catalog(self, then: Optional[Callable[[], None]] = None) -> None:
        """Reads the chosen catalog and the sensors' modes in a task; `then` once read."""
        config = self.context.config
        if self._loaded or not config.satellite_catalogs:
            if then:
                then()
            return
        if self.loading_task is not None:
            # Read once: whoever else waits (the satellite pass dock) is told too.
            if then:
                self._waiting.append(then)
            return
        catalog = config.satellite_catalogs[self.catalog_index]
        sensors = config.sensor_catalogs
        token_for = self.context.token_for
        client = self.context.esri()
        self.status.setText(self.tr("衛星カタログを読み込み中…"))

        def work(task):
            sats, problems = load_satellites(client, catalog, token_for, task.isCanceled)
            task.progress(70)
            modes, more = load_sensor_modes(client, sensors, token_for, task.isCanceled)
            return sats, modes, problems + more

        def done(result):
            self.loading_task = None
            sats, modes, problems = result
            self.catalog_sats = with_modes(sats, modes)
            self._loaded = True
            self.list_sats()
            n_modes = len(self.tree.choice.mode_names())
            text = self.tr("{label}: {n} 機").format(label=catalog.label, n=len(sats))
            if n_modes:
                text += self.tr("（撮像モード {n} 種）").format(n=n_modes)
            if problems:
                text += self.tr("（読めなかったもの {n} 件: {list}）").format(n=len(problems), list="、".join(problems[:3]) + (" …" if len(problems) > 3 else ""))
            self.status.setText(text + self.tr("。対象と期間を選んで「計算」"))
            waiting, self._waiting = self._waiting, []
            for f in ([then] if then else []) + waiting:
                f()

        def failed(message):
            self.loading_task = None
            self._waiting = []
            self.status.setText(self.tr("衛星カタログの読み込みに失敗: {message}").format(message=message))

        self.loading_task = run_task(self.tr("衛星カタログの読み込み"), work, done, failed)

    def pasted(self) -> list[SatelliteSpec]:
        return satellites_from_text(
            self.tle.toPlainText(),
            maxOffNadir=self.paste_max.value() or BUILT_IN_DEFAULTS["maxOffNadir"],
            minOffNadir=0,
            swath=self.paste_swath.value() or BUILT_IN_DEFAULTS["swath"],
            minInterval=self.paste_gap.value(),
            maxPerDay=self.paste_per_day.value(),
            leadTime=self.paste_lead.value(),
            sar=self.paste_sar.isChecked(),
        )

    def list_sats(self) -> None:
        self.tree.set([*self.catalog_sats, *self.pasted()])

    # --- Targets ----------------------------------------------------------------------------

    def _vector_layers(self) -> list[QgsVectorLayer]:
        return [l for l in QgsProject.instance().mapLayers().values()
                if l.type() == QgsMapLayer.LayerType.VectorLayer and l.isSpatial() and l.isValid()]

    def list_targets(self) -> None:
        """Fills the target choice: the selected features, each vector layer, a clicked point."""
        before = self.target.currentData()
        self.target.blockSignals(True)
        self.target.clear()
        active = self.iface.activeLayer()
        if isinstance(active, QgsVectorLayer) and active.isSpatial() and active.selectedFeatureCount():
            self.target.addItem(self.tr("選択中の地物（{n}）").format(n=active.selectedFeatureCount()), "selection")
        for layer in sorted(self._vector_layers(), key=lambda l: l.name()):
            self.target.addItem(f"{layer.name()}（{layer.featureCount():,}）", f"layer:{layer.id()}")
        if self.moving:
            self.target.addItem(self.tr("移動目標: {label}（{n}）").format(label=self.moving["label"], n=len(self.moving["ships"])), "moving")
        self.target.addItem(self.tr("地図でクリックした地点"), "click")
        values = [self.target.itemData(i) for i in range(self.target.count())]
        self.target.setCurrentIndex(values.index(before) if before in values else 0)
        self.target.blockSignals(False)
        self._target_changed()

    def _layers_changed(self, *_args) -> None:
        if self.isVisible():
            self.list_targets()

    def _layer_changed(self, layer) -> None:
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(self._selection_changed)
            except (TypeError, RuntimeError):
                pass
        self._watched = layer if isinstance(layer, QgsVectorLayer) else None
        if self._watched is not None:
            self._watched.selectionChanged.connect(self._selection_changed)
        self._selection_changed()

    def _selection_changed(self, *_args) -> None:
        if self.isVisible():
            self.list_targets()

    def _target_layer(self) -> Optional[QgsVectorLayer]:
        choice = self.target.currentData() or ""
        if choice == "selection":
            layer = self.iface.activeLayer()
            return layer if isinstance(layer, QgsVectorLayer) else None
        if choice.startswith("layer:"):
            layer = QgsProject.instance().mapLayer(choice[len("layer:"):])
            return layer if isinstance(layer, QgsVectorLayer) else None
        return None

    def _features(self, limit: Optional[int] = None) -> list:
        layer = self._target_layer()
        if layer is None:
            return []
        request = QgsFeatureRequest()
        if limit:
            request.setLimit(limit)
        if self.target.currentData() == "selection":
            # In feature id order (the selection itself has none).
            return sorted((f for f in layer.getSelectedFeatures(request) if f.hasGeometry()), key=lambda f: f.id())
        return [f for f in layer.getFeatures(request) if f.hasGeometry()]

    def _target_changed(self, *_args) -> None:
        click = self.target.currentData() == "click"
        self.pick.setVisible(click)
        if not click and self.pick.isChecked():
            self.pick.setChecked(False)
        layer = self._target_layer()
        names = [f.name() for f in layer.fields()] if layer is not None else []
        before = self.name_field.currentData()
        self.name_field.blockSignals(True)
        self.name_field.clear()
        self.name_field.addItem(self.tr("自動"), "")
        for i, n in enumerate(names):
            self.name_field.addItem(layer.attributeDisplayName(i), n)
        self.name_field.setCurrentIndex(max(0, self.name_field.findData(before)))
        self.name_field.blockSignals(False)
        self._list_priority_fields(layer)
        self.show_targets()

    def _list_priority_fields(self, layer: Optional[QgsVectorLayer]) -> None:
        """The attributes that hold numbers, offered as the priority when features are planned one by one."""
        before = self.priority.currentData()
        features = self._features(MAX_TARGETS) if layer is not None else []
        numeric: list[str] = []
        if features:
            names = layer.fields().names()
            for i, name in enumerate(names):
                if any(priority_of(_plain(f.attributes()[i])) is not None for f in features):
                    numeric.append(name)
        self.priority.clear()
        self.priority.addItem(self.tr("なし"), "")
        for name in sorted(numeric, key=natural_key):
            self.priority.addItem(layer.attributeDisplayName(layer.fields().indexOf(name)), name)
        index = self.priority.findData(before) if before else -1
        if index < 0:
            guess = next((n for n in numeric if re.search(r"priority|優先", n, re.IGNORECASE)), "")
            index = self.priority.findData(guess)
        self.priority.setCurrentIndex(max(0, index))
        self._priority_shown = self.each.isChecked() and len(features) > 1 and bool(numeric)
        self._hide_rows()

    def _hide_rows(self) -> None:
        """Shows the rows of 「詳細条件」 that apply: the priority with features to rank, the forecast with a forecast service."""
        if self.more.isCollapsed():
            return
        for w in (self.priority, self.priority_label, self.priority_order, self.priority_order_label):
            w.setVisible(self._priority_shown)
        forecast = self.context.config.cloud_forecast is not None
        self.cloud_row.setVisible(forecast)
        self.cloud_label.setVisible(forecast)

    def targets(self) -> list[PlanTarget] | str:
        """The targets the form asks for (in WGS84), or the reason there are none (as the viewer)."""
        choice = self.target.currentData()
        if choice == "moving":
            ships = self.ships()
            return [PlanTarget(s.name, (s.lon, s.lat)) for s in ships] if ships else self.tr("移動目標なし")
        if choice == "click":
            if not self.clicked:
                return self.tr("地図をクリックして地点を指定")
            lon, lat = self.clicked
            return [PlanTarget(f"{js_round(lat, 5)}, {js_round(lon, 5)}", (lon, lat))]
        layer = self._target_layer()
        if layer is None:
            return self.tr("対象のレイヤーなし")
        name = self.tr("選択中の地物") if choice == "selection" else layer.name()
        each = self.each.isChecked()
        count = layer.selectedFeatureCount() if choice == "selection" else layer.featureCount()
        if each and count > MAX_TARGETS:
            return self.tr("地物 {n} 件。{max} 件以下を選択するか、「詳細条件」の「地物ごとに計算」を外して一括計算").format(n=f"{count:,}", max=MAX_TARGETS)
        features = self._features()
        if not features:
            return self.tr("対象に図形なし")
        transform = to_wgs84(layer.crs())
        field = self.name_field.currentData() or ""
        names = layer.fields().names()

        def attributes(f) -> dict:
            return dict(zip(names, f.attributes()))

        if not each or len(features) == 1:
            shapes = [s for s in (shape_of(f.geometry(), transform) for f in features) if s]
            label = name_of(attributes(features[0]), features[0].id(), 0, field) if len(features) == 1 else name
            t = target_of(shapes, label)
            return [t] if t else self.tr("対象に図形なし")
        priority = self.priority.currentData() if self._priority_shown else ""
        sign = 1 if self.priority_order.currentData() == "large" else -1
        out = []
        for i, f in enumerate(features):
            shape = shape_of(f.geometry(), transform)
            t = target_of([shape], name_of(attributes(f), f.id(), i, field)) if shape else None
            if not t:
                continue
            value = _plain(f[priority]) if priority else None
            v = priority_of(value)
            out.append(t if v is None else replace(t, priority=sign * v, priority_text=str(value)))
        return out or self.tr("対象に図形なし")

    def show_targets(self) -> None:
        """Draws the targets (and the chosen pass) on the map."""
        if self.target.currentData() == "click":
            self.overlay.clear(["target", "target_point"])
            if self.clicked:
                self.overlay.point_target(*self.clicked)
        elif self.target.currentData() == "moving":
            # Each moving target where it was last reported.
            ships = self.ships() or []
            self.overlay.targets([QgsGeometry.fromPointXY(QgsPointXY(s.lon, s.lat)) for s in ships], WGS84)
        else:
            layer = self._target_layer()
            features = self._features(MAX_TARGETS) if layer is not None else []
            self.overlay.targets([f.geometry() for f in features], layer.crs() if layer else WGS84)
        if self.chosen is not None and self.plan:
            self.draw_pass(self.chosen)

    # --- Moving the chosen scenes ------------------------------------------------------

    def _move_toggled(self, on: bool) -> None:
        if on:
            if self.pick.isChecked():
                self.pick.setChecked(False)
            self._previous_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.drag_tool)
        elif self.canvas.mapTool() is self.drag_tool:
            self.canvas.unsetMapTool(self.drag_tool)
            if self._previous_tool is not None:
                self.canvas.setMapTool(self._previous_tool)

    def drag_begin(self, lon: float, lat: float) -> bool:
        """Starts moving the chosen pass's scenes from a place on them."""
        op, plan = self.chosen, self.plan
        if op is None or not plan:
            return False
        start = track_place(plan["sats"][op.satellite], op.time, (lon, lat))
        if start is None:
            return False
        self._drag = (op, replace(op), start)
        return True

    def drag_move(self, lon: float, lat: float) -> None:
        if self._drag is None or not self.plan:
            return
        op, origin, start = self._drag
        plan = self.plan
        sat = plan["sats"][op.satellite]
        at = track_place(sat, op.time, (lon, lat))
        if at is None:
            return
        o = plan["options"]
        moved = shift_scene(origin, sat, plan["targets"][op.target], at[0] - start[0], at[1] - start[1],
                            o.maxOffNadir, o.minIncidence, o.maxIncidence, o.side)
        # The opportunity itself changes, so the list, the CSV and the layer follow.
        for name, value in vars(moved).items():
            setattr(op, name, value)
        self.draw_pass(op)

    def drag_end(self) -> None:
        drag, self._drag = self._drag, None
        if drag is None or not self.plan:
            return
        op = drag[0]
        self.render()
        cover = self.tr("被覆 {p}").format(p=percent(op.coverage)) if op.cells is not None else (self.tr("対象を含む") if op.coverage else self.tr("対象外"))
        self.status.setText(self.tr("撮像範囲を移動: オフナディア角 {a}°・入射角 {i}°・{cover}").format(
            a=js_round(op.offNadir), i=js_round(op.incidence), cover=cover))
        self.chosenChanged.emit()

    def _pick_toggled(self, on: bool) -> None:
        if on:
            if self.move_button.isChecked():
                self.move_button.setChecked(False)
            self._previous_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.map_tool)
        elif self.canvas.mapTool() is self.map_tool:
            self.canvas.unsetMapTool(self.map_tool)
            if self._previous_tool is not None:
                self.canvas.setMapTool(self._previous_tool)

    def _map_clicked(self, point: QgsPointXY, _button=None) -> None:
        p = to_wgs84(self.canvas.mapSettings().destinationCrs()).transform(point)
        self.set_point(p.x(), p.y())
        self.pick.setChecked(False)

    def set_point(self, lon: float, lat: float) -> None:
        """Makes a point (WGS84) the target."""
        self.clicked = (lon, lat)
        index = self.target.findData("click")
        if index >= 0 and self.target.currentIndex() != index:
            self.target.setCurrentIndex(index)
        self.show_targets()
        self.status.setText(self.tr("地点 {lat}, {lon} を対象に設定。「計算」で撮像機会を検索").format(lat=js_round(lat, 5), lon=js_round(lon, 5)))

    def plan_at(self, lon: float, lat: float) -> None:
        """Opens the dock with a point (WGS84) as the target and plans it (the map's right-click menu)."""
        self.show()
        self.raise_()
        self.list_targets()
        self.set_point(lon, lat)
        self.run()

    def _relabel(self) -> None:
        """Names the targets of the last plan again by the chosen attribute, when the form still asks for the same targets."""
        plan = self.plan
        if not plan:
            return
        targets = self.targets()
        if isinstance(targets, str) or len(targets) != len(plan["targets"]) or any(t.center != p.center for t, p in zip(targets, plan["targets"])):
            return
        plan["targets"] = [replace(p, label=t.label) for t, p in zip(targets, plan["targets"])]
        self.render()

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

        def work(task):
            def progress(done: float) -> None:
                task.check()
                task.progress(done * 100)

            return plan_access(sats, targets, options, progress)

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
            self.status.setText(self.tr("計算の取消") if message == "取消" else self.tr("計算失敗: {m}").format(m=message))

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

            return plan_ships(sats, ships, options, progress, task.isCanceled)

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
            self.status.setText(self.tr("計算の取消") if message == "取消" else self.tr("計算失敗: {m}").format(m=message))

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

    def combining(self) -> bool:
        return self.view.currentData() == "cover"

    def cover_plans(self) -> list:
        plan = self.plan
        goal = min(1.0, max(0.1, self.goal.value() / 100))
        share = self.share.isChecked()
        top = self.max_cloud_value()
        if self.cover is None or self.cover[:3] != (goal, share, top):
            self.cover = (goal, share, top, cover_plan(self.candidates(), plan["targets"], plan["sats"], goal, 0.02, share))
        return self.cover[3]

    def show_view(self) -> None:
        """Lists the last plan in the chosen view, with what it found in the status line."""
        plan = self.plan
        if not plan:
            return
        combining = self.combining()
        pairing = self.pairing()
        revisit = self.view.currentData() == "revisit"
        self.goal.setVisible(combining)
        self.share.setVisible(combining and sum(1 for t in plan["targets"] if not t.cells) >= 2)
        self.first.setVisible(not combining and not pairing and not revisit)
        self.timeline_box.setVisible(not revisit)
        self.stereo_bar.setVisible(pairing == "stereo")
        self.repeat_bar.setVisible(pairing == "repeat")
        self.revisit_bar.setVisible(revisit)
        self.table.setVisible(not revisit)
        self.revisit_table.setVisible(revisit)
        self.chosen = None
        if revisit:
            self.overlay.clear(["track", "reach", "scene", "step", "label"])
            self.show_targets()
            self.show_revisit()
            return
        self.overlay.unpaint()
        if not any(c.key == self.sort[0] for c in self.columns()):
            self.sort = ("time", False)
        self.render()
        result, sats, targets = plan["result"], plan["sats"], plan["targets"]
        problems = describe_problems(result.problems, result.tooSoon)
        if not result.opportunities:
            self.status.setText(self.tr(NO_OPPORTUNITY) + problems)
        elif pairing:
            pairs = self.pairs()
            what = self.tr("ステレオペア") if pairing == "stereo" else self.tr("干渉ペア")
            if pairs:
                first = pairs[0].b if pairing == "stereo" else pairs[0].a
                self.status.setText(self.tr("{what} {n} 組。最初の 2 回目は {time}。行を選ぶと 1 回目（青）と 2 回目（番号 2）の撮像範囲を地図に表示").format(
                    what=what, n=f"{len(pairs):,}", time=local_text(first.time)) + problems)
            else:
                hint = (self.tr("交会角・日数の条件の緩和、またはオフナディア角の上限の引き上げが必要") if pairing == "stereo"
                        else self.tr("SAR 衛星を選び、期間を回帰日数より長くするか、入射角の差の条件の緩和が必要"))
                self.status.setText(self.tr("条件に合う{what}なし。{hint}").format(what=what, hint=hint) + problems)
        elif not combining:
            self.status.setText(summary_all(result.opportunities, sats, problems))
        else:
            self.status.setText(summary_cover(self.cover_plans(), targets, problems))
        self.show_targets()
        if self.shown:
            self.choose(self.shown[0])
        else:
            self.overlay.clear(["track", "reach", "scene", "step", "label"])

    def columns(self) -> list[Column]:
        plan = self.plan
        sats, targets, options = plan["sats"], plan["targets"], plan["options"]
        many = len(targets) > 1
        tr = self.tr

        def time_title(op: Opportunity) -> str:
            sat = sats[op.satellite]
            parts = [tr("発注期限 {t}").format(t=local_text(op.time - sat.leadTime * 3600000)) if sat.leadTime > 0 else "",
                     tr("撮像範囲を地図で移動済み（✎）") if op.adjusted else ""]
            return "・".join(x for x in parts if x)

        def satellite_title(op: Opportunity) -> str:
            sat = sats[op.satellite]
            text = tr("{source}{mode}・TLE {days} 日").format(source=sat.source, mode=mode_facts(sat), days=js_round(op.tleAge, 0))
            return text + (tr("（古い TLE。予測がずれる場合あり）") if is_old_tle(op, sat) else "")

        def downlink_title(op: Opportunity) -> str:
            if not op.downlink:
                return tr("撮像から 24 時間以内の地上局上空の通過なし")
            return tr("{station}・撮像から {minutes} 分（衛星が地上局の上に来る時刻。データの処理・配信の時間は含まず）").format(
                station=options.stations[op.downlink[1]].name, minutes=math.floor((op.downlink[0] - op.time) / 60000 + 0.5))

        cols = [
            Column("time", tr("日時"), lambda op: local_text(op.time) + (" ✎" if op.adjusted else ""), time_title),
            Column("satellite", tr("衛星"), lambda op: satellite_label(sats[op.satellite]), satellite_title),
        ]
        if many:
            cols.append(Column("target", tr("対象"), lambda op: targets[op.target].label))
        if many and any(t.priority is not None for t in targets):
            cols.append(Column("priority", tr("優先度"), lambda op: targets[op.target].priority_text or "",
                               lambda op: tr("対象の優先度（属性の値）。並べ替えると優先度の高い順")))
        if any(t.uncertainty is not None for t in targets):
            cols.append(Column(
                "uncertainty", tr("位置の誤差"), lambda op: f"±{js_round(targets[op.target].uncertainty or 0)} km",
                lambda op: tr("最後の報告から {h} 後の推測位置（針路・速力から）。誤差の円のうち撮像範囲に入る割合が被覆").format(
                    h=hours_text((targets[op.target].guessed or 0) / 3600000))))
        cols += [
            Column("offNadir", tr("オフナディア角"), lambda op: f"{js_round(op.offNadir)}°",
                   lambda op: tr("入射角 {i}°・距離 {d} km").format(i=js_round(op.incidence), d=js_round(op.range, 0))),
            Column("side", tr("方向"), side_text, lambda op: tr("衛星の進行方向に対して対象が右か左か、北行（昇交）か南行（降交）か")),
            Column("sun", tr("太陽高度"), lambda op: f"{js_round(op.sunElevation, 0)}°"),
        ]
        if any(op.cloud is not None for op in plan["result"].opportunities):
            label = self.context.config.cloud_forecast.label if self.context.config.cloud_forecast else ""
            cols.append(Column("cloud", tr("雲量"), lambda op: "—" if op.cloud is None else f"{js_round(op.cloud, 0)}%",
                               lambda op: (tr("SAR は雲を通して撮像") if sats[op.satellite].sar else tr("予報の範囲外（16 日先まで）")) if op.cloud is None
                               else tr("{label} の雲量予報").format(label=label)))
        if options.stations:
            cols.append(Column("downlink", tr("受信見込み"), lambda op: local_text(op.downlink[0]) if op.downlink else tr("1 日以内なし"), downlink_title))
        if self.pairing():
            cols += self.pair_columns()
        elif self.combining():
            cols += [
                Column("gain", tr("追加"), lambda op: f"+{percent(op.gain or 0)}", lambda op: tr("この回で新たに撮像できる対象の割合")),
                Column("cumulative", tr("累計"), lambda op: percent(op.cumulative or 0), lambda op: tr("この回までに撮像できた対象の割合")),
            ]
            if many:
                cols.append(Column(
                    "shot", tr("シーン"),
                    lambda op: f"#{'' if op.shot is None else op.shot}" + (tr("（{n} 地点）").format(n=op.shotTargets) if (op.shotTargets or 1) > 1 else ""),
                    lambda op: tr("この {n} 地点は 1 回の撮像（1 シーン）でまとめて撮像可能").format(n=op.shotTargets) if (op.shotTargets or 1) > 1 else ""))
        else:
            cols.append(Column("cover", tr("被覆"), cover_text, lambda op: cover_title(op, sats[op.satellite])))
        return cols

    def sorted(self) -> list[Opportunity]:
        plan = self.plan
        if self.pairing():
            ops = [replace(pair.a, pair=pair) for pair in self.pairs()]
        elif self.combining():
            ops = [s for p in self.cover_plans() for s in p.steps]
        else:
            ops = self.candidates()
        if not self.combining() and not self.pairing() and self.first.isChecked():
            ops = first_only(ops)
        key, descending = self.sort
        return sorted_ops(ops, key, descending, plan["sats"], plan["targets"])

    def render(self) -> None:
        if not self.plan:
            return
        columns = self.columns()
        self.shown = self.sorted()
        self.model.set(columns, self.shown, self.plan["sats"])
        key, descending = self.sort
        index = next((i for i, c in enumerate(columns) if c.key == key), -1)
        self.table.horizontalHeader().setSortIndicator(index, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
        self._select_chosen()
        self._update_buttons()
        self.draw_timeline()

    def draw_timeline(self) -> None:
        """Draws the timeline of the listed shots (when it is asked for)."""
        plan = self.plan
        on = self.timeline_box.isChecked() and bool(plan) and bool(self.shown)
        self.timeline.setVisible(on)
        if not on:
            return
        many = len(plan["targets"]) > 1

        def title(op: Opportunity) -> str:
            target = f" → {plan['targets'][op.target].label}" if many else ""
            return self.tr("{time} {name}{target}・オフナディア角 {angle}°").format(
                time=local_text(op.time), name=plan["sats"][op.satellite].name, target=target, angle=js_round(op.offNadir))

        self.timeline.set(self.shown, plan["sats"], plan["options"].start, plan["options"].end, self.chosen, title)

    def _sort_by(self, section: int) -> None:
        columns = self.model.columns
        if section >= len(columns):
            return
        key = columns[section].key
        self.sort = (key, not self.sort[1] if self.sort[0] == key else False)
        self.render()

    def _row_chosen(self, row: int) -> None:
        op = self.model.op_at(row)
        if op is not None and op is not self.chosen:
            self.choose(op)

    def _select_chosen(self) -> None:
        if self.chosen is None:
            return
        row = next((i for i, op in enumerate(self.model.ops) if op is self.chosen), -1)
        if row >= 0:
            self.table.selectRow(row)
            self.table.scrollTo(self.model.index(row, 0))

    def choose(self, op: Opportunity) -> None:
        """Chooses an opportunity: its row, and its pass on the map."""
        self.chosen = op
        self._select_chosen()
        self.draw_pass(op)
        self._update_buttons()
        if self.timeline.isVisible():
            self.timeline.choose(op)
        self.chosenChanged.emit()

    def draw_pass(self, op: Opportunity) -> None:
        plan = self.plan
        sat = plan["sats"][op.satellite]
        shapes = pass_shapes(sat, plan["targets"][op.target], op)
        self._scenes = shapes.scenes if shapes else []
        if shapes is None:
            return
        steps = []
        if op.pair is not None:
            # The pair's second shot, numbered 2.
            second = pass_shapes(plan["sats"][op.pair.b.satellite], plan["targets"][op.pair.b.target], op.pair.b)
            steps.append((2, second.scenes if second else []))
        elif self.combining():
            for i, step in enumerate(self.cover_plans()[op.target].steps):
                if step.time == op.time and step.satellite == op.satellite:
                    continue
                other = pass_shapes(plan["sats"][step.satellite], plan["targets"][step.target], step)
                steps.append((i + 1, other.scenes if other else []))
        target = plan["targets"][op.target]
        # A moving target: the circle its guessed place may be off by, at the moment of the pass.
        circle = [*target.points, target.points[0]] if target.uncertainty is not None and len(target.points) > 2 else None
        self.overlay.chosen(shapes, f"{satellite_label(sat)} {local_text(op.time)[5:]}", steps, circle)
        self.canvas.refresh()

    def zoom_to_chosen(self) -> None:
        """Fits the map to the chosen opportunity's target, scenes and satellite (or the combination's passes)."""
        op = self.chosen
        if op is None or not self.plan:
            return
        # A combination shows the target and all its passes; one pass, also where the satellite is.
        if self.combining():
            box = self.overlay.extent(["scene", "step"])
        else:
            # A pair shows its second shot too.
            box = self.overlay.extent(["scene", "satellite", "step"])
        if box is None:
            return
        t = self.plan["targets"][op.target]
        for x, y in [t.center, *t.points]:
            box.combineExtentWith(x, y)
        box = box.buffered(max(box.width(), box.height()) * 0.1 + 0.01)
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        self.canvas.setExtent(from_wgs84(canvas_crs).transformBoundingBox(box))
        self.canvas.refresh()

    def _update_buttons(self) -> None:
        any_ = bool(self.shown)
        cells = self.revisit_shown()
        self.csv_button.setEnabled(any_ or cells)
        self.layer_button.setEnabled(any_ or cells)
        for button in (self.remind_button, self.store_button):
            button.setEnabled(any_)
            if not any_ and button.isChecked():
                button.setChecked(False)
        self.zoom_button.setEnabled(self.chosen is not None)
        self.move_button.setEnabled(self.chosen is not None)
        if self.chosen is None and self.move_button.isChecked():
            self.move_button.setChecked(False)

    # --- Pairs, the revisit map and the later actions (their widgets) --------------------------

    def _pair_options(self) -> QWidget:
        """The conditions of the pair views and the revisit map's grid, under the view choice."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)

        def spin(low: float, high: float, value: float, decimals: int = 0, suffix: str = "") -> QDoubleSpinBox:
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(decimals)
            box.setValue(value)
            box.setSuffix(suffix)
            box.valueChanged.connect(self.show_view)
            return box

        def part(tooltip: str, *widgets) -> QWidget:
            w = QWidget()
            layout = QHBoxLayout(w)
            layout.setContentsMargins(0, 0, 0, 0)
            for x in widgets:
                layout.addWidget(QLabel(x) if isinstance(x, str) else x)
            w.setToolTip(tooltip)
            row.addWidget(w)
            w.hide()
            return w

        self.stereo_min = spin(1, 90, 15)
        self.stereo_max = spin(1, 90, 45, suffix="°")
        self.stereo_days = spin(0.01, 60, 7, 2)
        self.stereo_series = QCheckBox(self.tr("同じシリーズ"))
        self.stereo_series.setChecked(True)
        self.stereo_series.toggled.connect(self.show_view)
        self.stereo_bar = part(self.tr("2 回の撮像の視線のなす角（交会角）。15〜45° が DSM・建物の高さに好適"),
                               self.tr("交会角"), self.stereo_min, "〜", self.stereo_max, "・", self.stereo_days, self.tr("日以内"), self.stereo_series)
        self.repeat_incidence = spin(0.01, 10, 1, 2, "°")
        self.repeat_baseline = spin(0, 1e6, 0, 0, " m")
        self.repeat_baseline.setSpecialValueText(self.tr("制限なし"))
        self.repeat_series = QCheckBox(self.tr("同じシリーズ"))
        self.repeat_series.setToolTip(self.tr("同じシリーズの衛星（同じ軌道のコンステレーション）同士もペア化"))
        self.repeat_series.toggled.connect(self.show_view)
        self.repeat_bar = part(self.tr("同じ衛星（シリーズ）が同じ軌道（北行・南行、向き、入射角、方位）で戻ってきた 2 回"),
                               self.tr("入射角の差"), self.repeat_incidence, self.tr("以内・基線"), self.repeat_baseline, self.tr("以下"), self.repeat_series)
        self.revisit_grid = QSpinBox()
        self.revisit_grid.setRange(2, 30)
        self.revisit_grid.setValue(10)
        self.revisit_grid.setPrefix(self.tr("格子 "))
        self.revisit_grid.setSuffix(" ×")
        self.revisit_grid.valueChanged.connect(self.show_view)
        self.revisit_bar = part(self.tr("対象を囲む範囲をこの数 × この数のセルに分け、セルごとに撮像機会の間隔を算出"), self.revisit_grid)
        row.addStretch(1)
        return bar

    def _later_actions(self) -> QHBoxLayout:
        """「予定・通知…」, 「保存・追跡…」 and 「保存した計画…」, and the rows they open."""
        actions = QHBoxLayout()
        self.remind_button = QPushButton(self.tr("予定・通知…"))
        self.remind_button.setCheckable(True)
        self.remind_button.setToolTip(self.tr("撮像前の通知・予定表（.ics）への保存"))
        self.store_button = QPushButton(self.tr("保存・追跡…"))
        self.store_button.setCheckable(True)
        self.store_button.setToolTip(self.tr("計画をフィーチャーサービスに保存し、撮れたかを追跡"))
        self.saved_button = QPushButton(self.tr("保存した計画…"))
        self.saved_button.setToolTip(self.tr("保存した計画の一覧と撮像の成否"))
        self.saved_button.clicked.connect(lambda: self.open_saved and self.open_saved())
        for b in (self.remind_button, self.store_button, self.saved_button):
            actions.addWidget(b)
        actions.addStretch(1)

        self.remind_row = QWidget()
        remind = QHBoxLayout(self.remind_row)
        remind.setContentsMargins(0, 0, 0, 0)
        self.remind_minutes = QSpinBox()
        self.remind_minutes.setRange(0, 1440)
        self.remind_minutes.setValue(30)
        self.remind_minutes.setPrefix(self.tr("撮像の "))
        self.remind_minutes.setSuffix(self.tr(" 分前"))
        notify = QPushButton(self.tr("通知を設定"))
        notify.setToolTip(self.tr("一覧の撮像の前に、QGIS のメッセージバーで通知（QGIS の起動中のみ）"))
        notify.clicked.connect(self.set_reminders)
        ics = QPushButton(self.tr("予定表 (.ics)"))
        ics.setToolTip(self.tr("一覧の撮像を予定表（iCalendar .ics）で保存。Outlook などに取り込み可能（設定した分前のアラーム付き）"))
        ics.clicked.connect(lambda: self.save_ics())
        clear = QPushButton(self.tr("通知をすべて解除"))
        clear.clicked.connect(self.clear_reminders)
        self.remind_count = QLabel()
        for w in (self.remind_minutes, notify, ics, clear, self.remind_count):
            remind.addWidget(w)
        remind.addStretch(1)
        self.remind_row.hide()
        self.remind_button.toggled.connect(lambda on: (self.remind_row.setVisible(on), self.count_reminders()))

        self.save_row = QWidget()
        save = QHBoxLayout(self.save_row)
        save.setContentsMargins(0, 0, 0, 0)
        self.store_choice = QComboBox()
        self.store_choice.setToolTip(self.tr("保存先"))
        self.plan_name = QLineEdit()
        self.plan_name.setPlaceholderText(self.tr("計画の名前"))
        save_button = QPushButton(self.tr("保存"))
        save_button.setToolTip(self.tr("一覧の撮像を保存先に保存。撮像時刻の後に画像カタログで撮れたかを確認可能"))
        save_button.clicked.connect(lambda: self.save_to_store())
        for w in (self.store_choice, self.plan_name, save_button):
            save.addWidget(w)
        save.setStretch(1, 1)
        self.save_row.hide()
        self.store_button.toggled.connect(self._store_toggled)
        return actions

    def _store_toggled(self, on: bool) -> None:
        self.save_row.setVisible(on)
        if on and not self.plan_name.text() and self.plan:
            self.plan_name.setText(self.tr("撮像計画 {day}").format(day=local_text(self.plan["options"].start)[:10]))

    # --- Cloud forecasts --------------------------------------------------------------------

    def _cloud_toggled(self, on: bool) -> None:
        settings.set_plan_cloud(on)
        if not self.plan:
            return
        if on:
            self.load_clouds()
        else:
            for op in self.plan["result"].opportunities:
                op.cloud = None
            self.cover = None
            self._pairs = None
            self.show_view()

    def _max_cloud_changed(self) -> None:
        self.cover = None
        self._pairs = None
        self.show_view()

    def max_cloud_value(self) -> Optional[float]:
        """The largest cloud cover asked for (percent), or None for any."""
        v = self.max_cloud.value()
        return None if v < 0 else float(v)

    def candidates(self) -> list[Opportunity]:
        """The last plan's opportunities that pass the cloud cover asked for (those without a forecast stay)."""
        ops = self.plan["result"].opportunities if self.plan else []
        top = self.max_cloud_value()
        return list(ops) if top is None else [op for op in ops if op.cloud is None or op.cloud <= top]

    def load_clouds(self) -> None:
        """Asks the forecast service for the cloud cover of the last plan's optical shots (in a task), and lists them with it."""
        plan = self.plan
        config = self.context.config.cloud_forecast
        if not plan or not config or self.cloud_task is not None:
            return
        places = places_for(plan["result"].opportunities, plan["sats"], plan["targets"], _now_ms())
        before = self.status.text()
        if not places:
            self.status.setText(before + self.tr("（雲量予報は 16 日先までの光学衛星の機会のみ）"))
            return
        self.status.setText(self.tr("{label} に雲量予報を問い合わせ中…").format(label=config.label))
        authcfg = settings.auth_for(config.url)

        def work(task):
            return fetch_clouds(config, places, lambda url: get_bytes(url, authcfg), task.isCanceled)

        def done(forecasts):
            self.cloud_task = None
            if self.plan is not plan:
                return
            n = apply_clouds(plan["result"].opportunities, plan["sats"], plan["targets"], forecasts)
            self.cover = None
            self._pairs = None
            self.show_view()
            if self.view.currentData() != "revisit":
                self.status.setText(before + self.tr("（雲量予報 {n} 回分: {label}）").format(n=n, label=config.label))

        def failed(message):
            self.cloud_task = None
            self.status.setText(before + self.tr("（雲量予報の取得に失敗: {m}）").format(m=message))

        self.cloud_task = run_task(self.tr("雲量予報の取得"), work, done, failed)

    # --- Pairs ---------------------------------------------------------------------------------

    def pairing(self) -> Optional[str]:
        """The pair view shown: `stereo` or `repeat`; None in the other views."""
        v = self.view.currentData()
        return v if v in ("stereo", "repeat") else None

    def pairs(self) -> list:
        """The pairs of the last plan for the conditions in the form (found again when they change)."""
        plan, kind = self.plan, self.pairing()
        if not plan or not kind:
            return []
        if kind == "stereo":
            options = StereoOptions(self.stereo_min.value(), self.stereo_max.value(), self.stereo_days.value(), self.stereo_series.isChecked())
        else:
            baseline = self.repeat_baseline.value()
            options = RepeatOptions(self.repeat_incidence.value(), 2, baseline if baseline > 0 else None, self.repeat_series.isChecked())
        key = (kind, options, self.max_cloud_value())
        if self._pairs is None or self._pairs[0] != key:
            find = stereo_pairs if kind == "stereo" else repeat_pairs
            self._pairs = (key, find(self.candidates(), plan["sats"], plan["targets"], options))
        return self._pairs[1]

    def pair_columns(self) -> list[Column]:
        """The columns of a pair view: the second shot, how the two looks relate, and the days between."""
        sats, tr = self.plan["sats"], self.tr
        cols = [Column("second", tr("2 回目"), lambda op: f"{local_text(op.pair.b.time)} {satellite_label(sats[op.pair.b.satellite])}",
                       lambda op: tr("オフナディア角 {a}°・入射角 {i}°・{side}").format(a=js_round(op.pair.b.offNadir), i=js_round(op.pair.b.incidence), side=side_text(op.pair.b)))]
        if self.pairing() == "stereo":
            cols.append(Column("angle", tr("交会角"), lambda op: f"{js_round(op.pair.convergence)}°",
                               lambda op: tr("B/H {bh}（2 回の視線のなす角。大きいほど高さの精度が向上、大きすぎると対応づけが困難）").format(bh=js_round(op.pair.bh, 2))))
        else:
            cols.append(Column("baseline", tr("垂直基線"), lambda op: f"{math.floor(op.pair.baseline + 0.5):,} m",
                               lambda op: tr("入射角の差 {d}°（基線が短いほど干渉しやすく、長いほど地形の高さに敏感）").format(d=js_round(abs(op.pair.b.incidence - op.incidence), 2))))
        cols.append(Column("days", tr("間隔"), lambda op: hours_text(op.pair.days * 24), lambda op: tr("1 回目から 2 回目までの時間")))
        return cols

    # --- The revisit map -----------------------------------------------------------------------

    def show_revisit(self) -> None:
        """The revisit map of the last plan: planned again over a grid of cells around the targets (in a task), then listed and drawn."""
        plan = self.plan
        size = self.revisit_grid.value()
        self.shown = []
        self.timeline.hide()
        self._update_buttons()
        if self.revisit is not None and self.revisit["size"] == size:
            self._revisit_ready()
            return
        self.revisit = None
        self.revisit_model.clear()
        self.overlay.unpaint()
        if self.revisit_task is not None:
            self.revisit_task.cancel()
        cells = grid_cells(box_of(plan["targets"]), size)
        sats = plan["sats"]
        options = replace(plan["options"], stations=[])
        what = self.tr("再訪間隔を {n} セルで計算中…").format(n=len(cells))
        self.status.setText(what)

        def work(task):
            def progress(done: float) -> None:
                task.check()
                task.progress(done * 100)

            return plan_access(sats, cell_targets(cells), options, progress)

        def done(result: PlanResult):
            if self.revisit_task is not task:
                return
            self.revisit_task = None
            if self.plan is not plan or self.view.currentData() != "revisit":
                return
            self.revisit = {"size": size, "cells": cells, "chosen": None,
                            "stats": revisit_stats(result.opportunities, len(cells), options.start, options.end)}
            self._revisit_ready()

        def failed(message: str):
            if self.revisit_task is not task:
                return
            self.revisit_task = None
            self.status.setText(self.tr("計算の取消") if message == "取消" else self.tr("計算失敗: {m}").format(m=message))

        task = run_task(what, work, done, failed)
        self.revisit_task = task

    def _revisit_ready(self) -> None:
        revisit, plan = self.revisit, self.plan
        stats = revisit["stats"]
        self.render_revisit()
        self.draw_revisit()
        seen = [s for s in stats if s.count > 0]
        days = (plan["options"].end - plan["options"].start) / 86400000
        if seen:
            per_day = sum(s.count for s in stats) / len(stats) / days
            self.status.setText(self.tr("{n} セルのうち {seen} セルで撮像機会あり。最長の待ちは {worst}、平均で {per} 回/日（緑ほど早く撮像、赤ほど長い待ち）。行の選択でセルを表示").format(
                n=len(stats), seen=len(seen), worst=hours_text(max(s.maxGap for s in stats)), per=js_round(per_day, 2)))
        else:
            self.status.setText(self.tr("この期間、範囲内のどのセルにも撮像機会なし。期間の延長か条件の見直しが必要"))
        self._update_buttons()

    def render_revisit(self) -> None:
        """Lists the revisit map's cells, longest wait first."""
        revisit = self.revisit
        stats, cells = revisit["stats"], revisit["cells"]
        self.revisit_model.clear()
        self.revisit_model.setHorizontalHeaderLabels([self.tr("セル"), self.tr("回数"), self.tr("平均間隔"), self.tr("最長の待ち")])
        order = sorted(range(len(stats)), key=lambda i: (-stats[i].maxGap, i))
        for i in order[:MAX_ROWS]:
            s = stats[i]
            items = [QStandardItem(text) for text in (str(i + 1), str(s.count), hours_text(s.meanGap), hours_text(s.maxGap))]
            for item in items:
                item.setData(i, Qt.ItemDataRole.UserRole)
                item.setToolTip(self.tr("中心 {lat}, {lon}").format(lat=js_round(cells[i].center[1], 4), lon=js_round(cells[i].center[0], 4)))
            self.revisit_model.appendRow(items)

    def draw_revisit(self) -> None:
        """Draws the revisit map's cells, coloured by their longest wait."""
        revisit = self.revisit
        if revisit is None:
            return
        stats = revisit["stats"]
        seen = [s.maxGap for s in stats if s.count > 0]
        shortest, longest = (min(seen), max(seen)) if seen else (0, 0)
        # Every cell named only on a small grid (the viewer names up to 12 × 12; on the canvas the texts would overlap).
        many = len(revisit["cells"]) > 36
        polygons = []
        for i, (cell, s) in enumerate(zip(revisit["cells"], stats)):
            chosen = revisit["chosen"] == i
            label = "" if many and not chosen else (hours_text(s.maxGap) if s.count else self.tr("なし"))
            polygons.append(([cell.ring], QColor(*revisit_color(s, shortest, longest)).name(), 3 if chosen else 1, label))
        self.overlay.paint(polygons)
        self.canvas.refresh()

    def _cell_chosen(self, row: int) -> None:
        if self.revisit is None or row < 0:
            return
        index = self.revisit_model.index(row, 0)
        self.revisit["chosen"] = self.revisit_model.data(index, Qt.ItemDataRole.UserRole)
        self.draw_revisit()

    def zoom_to_cell(self) -> None:
        """Centres the map on the chosen cell."""
        revisit = self.revisit
        if revisit is None or revisit["chosen"] is None:
            return
        x0, y0 = revisit["cells"][revisit["chosen"]].ring[0]
        x1, y1 = revisit["cells"][revisit["chosen"]].ring[2]
        box = QgsRectangle(x0, y0, x1, y1)
        crs = self.canvas.mapSettings().destinationCrs()
        self.canvas.setCenter(from_wgs84(crs).transform(box.center()))
        self.canvas.refresh()

    # --- Reminders and the calendar ----------------------------------------------------------

    def _rings_of(self, op: Opportunity) -> list:
        plan = self.plan
        shapes = pass_shapes(plan["sats"][op.satellite], plan["targets"][op.target], op)
        return [[list(p) for p in ring] for ring in shapes.scenes] if shapes else []

    def reminders(self) -> list:
        return [r for r in (reminder_of(v) for v in settings.plan_reminders()) if r is not None]

    def count_reminders(self) -> None:
        n = sum(1 for r in self.reminders() if r.time > _now_ms())
        self.remind_count.setText(self.tr("通知予定 {n} 件").format(n=n) if n else "")

    def set_reminders(self) -> int:
        """Sets reminders before the listed shots."""
        plan = self.plan
        if not plan or not self.shown:
            return 0
        fresh = new_reminders(self.shown, plan["sats"], plan["targets"], self.remind_minutes.value(), self._rings_of, _now_ms())
        settings.set_plan_reminders([r.to_json() for r in merge_reminders(self.reminders(), fresh)])
        self.count_reminders()
        bar = self.iface.messageBar()
        if fresh:
            bar.pushInfo(self.tr("撮像計画"), self.tr("撮像 {n} 件の {m} 分前に通知（QGIS の起動中のみ）").format(n=len(fresh), m=self.remind_minutes.value()))
        else:
            bar.pushInfo(self.tr("撮像計画"), self.tr("今後の撮像がないため、通知の設定なし"))
        return len(fresh)

    def clear_reminders(self) -> None:
        settings.set_plan_reminders([])
        self.count_reminders()
        self.iface.messageBar().pushInfo(self.tr("撮像計画"), self.tr("撮像の通知をすべて解除"))

    def fire_reminders(self, now: Optional[float] = None) -> int:
        """Shows the reminders that are due in the message bar and the log; returns how many."""
        now = _now_ms() if now is None else now
        kept = self.reminders()
        due, rest = due_reminders(kept, now)
        if len(rest) == len(kept):
            return 0
        settings.set_plan_reminders([r.to_json() for r in rest])
        for r in due:
            text = reminder_text(r, now)
            QgsMessageLog.logMessage(text, self.tr("撮像計画"), Qgis.MessageLevel.Info)
            self._notice(text, r.rings)
        if self.remind_row.isVisible():
            self.count_reminders()
        return len(due)

    def _notice(self, text: str, rings: list) -> None:
        """A notice in the message bar, with 「移動」 to the shot's scenes."""
        bar = self.iface.messageBar()
        widget = bar.createMessage(self.tr("撮像の通知"), text)
        if rings:
            go = QPushButton(self.tr("移動"))

            def zoom() -> None:
                box = QgsRectangle()
                box.setNull()
                for ring in rings:
                    for x, y in ring:
                        box.combineExtentWith(x, y)
                crs = self.canvas.mapSettings().destinationCrs()
                self.canvas.setExtent(from_wgs84(crs).transformBoundingBox(box.buffered(max(box.width(), box.height()) * 0.5 + 0.01)))
                self.canvas.refresh()

            go.clicked.connect(zoom)
            widget.layout().addWidget(go)
        bar.pushWidget(widget, Qgis.MessageLevel.Info, 0)

    def default_ics_name(self) -> str:
        return self.default_file_name()[:-4] + ".ics"

    def save_ics(self, path: Optional[str] = None) -> None:
        """Saves the listed shots as an iCalendar file."""
        plan = self.plan
        if not plan or not self.shown:
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("予定表 (.ics)"), self.default_ics_name(), self.tr("iCalendar (*.ics)"))
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(plan_ics(self.shown, plan["sats"], plan["targets"], self.remind_minutes.value(), _now_ms()))
        self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("撮像の予定を予定表（.ics）で保存"))

    # --- Saving to a plan store -------------------------------------------------------------

    def save_to_store(self, then: Optional[Callable[[Optional[str]], None]] = None) -> None:
        """Saves the listed shots to the chosen plan layer (in a task); `then` hears the plan's id (None when nothing was saved)."""
        plan = self.plan
        stores = self.context.config.plan_stores
        index = max(0, self.store_choice.currentIndex())
        if not plan or not self.shown or not stores or self.store_task is not None:
            return
        store = stores[min(index, len(stores) - 1)]
        name = self.plan_name.text().strip() or self.tr("撮像計画 {day}").format(day=local_text(plan["options"].start)[:10])
        plan_id = new_plan_id()
        features = shot_features(self.shown, plan["sats"], plan["targets"], self._rings_of, plan_id, name, store.fields, _now_ms())
        client = self.context.esri()
        token = self.context.token_for(store.url, store.token)
        self.status.setText(self.tr("{label} に {n} 回の撮像を保存中…").format(label=store.label, n=len(features)))

        def work(task):
            return add_shots(client, store, token, features, task.isCanceled)

        def done(result):
            self.store_task = None
            saved, failed_ = result
            more = self.tr("。保存失敗 {n} 回: {why}").format(n=len(failed_), why=failed_[0]) if failed_ else ""
            self.status.setText(self.tr("「{name}」を {label} に保存（{n} 回{more}）。「保存した計画…」で撮像の成否を確認可能").format(
                name=name, label=store.label, n=saved, more=more))
            self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("撮像計画「{name}」を保存").format(name=name))
            if then:
                then(plan_id if saved else None)

        def failed(message):
            self.store_task = None
            self.status.setText(self.tr("保存失敗: {m}").format(m=message))
            if then:
                then(None)

        self.store_task = run_task(self.tr("撮像計画の保存"), work, done, failed)

    # --- Output ---------------------------------------------------------------------------

    def revisit_shown(self) -> bool:
        return self.revisit is not None and self.view.currentData() == "revisit"

    def csv_text(self) -> str:
        plan = self.plan
        if self.revisit_shown():
            return revisit_csv(self.revisit["cells"], self.revisit["stats"])
        return plan_csv(self.shown, plan["sats"], plan["targets"], plan["options"].stations)

    def default_file_name(self) -> str:
        options = self.plan["options"]

        def day(ms: float) -> str:
            return datetime.fromtimestamp(ms / 1000).strftime("%Y%m%d")

        if self.revisit_shown():
            return "revisit.csv"
        return f"imaging-plan_{day(options.start)}-{day(options.end)}.csv"

    def save_csv(self, path: Optional[str] = None) -> None:
        if not self.plan or not (self.shown or self.revisit_shown()):
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("CSV 保存"), self.default_file_name(), self.tr("CSV (*.csv)"))
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(self.csv_text())
        if self.revisit_shown():
            self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("再訪間隔 {n} セルを CSV で保存").format(n=len(self.revisit["cells"])))
        else:
            self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("撮像機会 {n} 件を CSV で保存").format(n=len(self.shown)))

    def layer_name(self) -> str:
        options = self.plan["options"]
        days = math.floor((options.end - options.start) / 86400000 + 0.5)
        return self.tr("撮像計画 {start} から {days} 日（{n} 回）").format(start=local_text(options.start), days=days, n=len(self.shown))

    def add_layer(self) -> Optional[QgsVectorLayer]:
        if self.plan and self.revisit_shown():
            options = self.plan["options"]
            days = math.floor((options.end - options.start) / 86400000 + 0.5)
            layer = memory_layer(revisit_layer(self.revisit["cells"], self.revisit["stats"]),
                                 self.tr("再訪間隔 {start} から {days} 日").format(start=local_text(options.start), days=days))
            QgsProject.instance().addMapLayer(layer)
            return layer
        if not self.plan or not self.shown:
            return None
        plan = self.plan
        layer = memory_layer(plan_features(self.shown, plan["sats"], plan["targets"], self.combining(), plan["options"].stations), self.layer_name())
        QgsProject.instance().addMapLayer(layer)
        return layer

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
