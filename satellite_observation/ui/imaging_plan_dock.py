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
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional

from qgis.core import QgsFeatureRequest, QgsMapLayer, QgsPointXY, QgsProject, QgsVectorLayer
from qgis.gui import QgsCollapsibleGroupBox, QgsMapToolEmitPoint
from qgis.PyQt.QtCore import QAbstractTableModel, QDateTime, QModelIndex, Qt, pyqtSignal
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QBrush, QColor, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableView,
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
    percent, plan_csv, plan_features, side_text, sorted_ops, summary_all, summary_cover,
)
from ..core.plan.satellites import BUILT_IN_DEFAULTS, SatelliteSpec, sample_satellites, satellite_label, satellites_from_text
from ..core.plan.scene import pass_shapes
from ..core.plan.targets import MAX_TARGETS, name_of, target_of
from ..qgis_io.geometry import WGS84, from_wgs84, shape_of, to_wgs84
from ..qgis_io.plan_layer import memory_layer
from ..qgis_io.tasks import run_task
from .docks import PluginDock
from .plan_overlay import PlanOverlay
from .satellite_tree import SatelliteTree

#: The most opportunities listed as rows (the CSV and the layer take them all).
MAX_ROWS = 2000
OLD_COLOR = QColor("#b45309")
DAYS = (1, 3, 7, 14, 30)


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
        self.plan_task = None
        self.clicked: Optional[tuple] = None
        self.plan: Optional[dict] = None
        self.cover: Optional[tuple] = None
        self.shown: list[Opportunity] = []
        self.chosen: Optional[Opportunity] = None
        self.sort = ("time", False)
        self._previous_tool = None
        self._build()
        context.configChanged.connect(self._config_changed)
        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        iface.currentLayerChanged.connect(self._layer_changed)
        self._watched: Optional[QgsVectorLayer] = None
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()

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
        self.each = QCheckBox(self.tr("地物ごとに計算"))
        self.each.setChecked(True)
        self.each.toggled.connect(self.show_targets)
        options.addRow(self.each)
        self.name_field = QComboBox()
        self.name_field.setToolTip(self.tr("一覧・CSV・レイヤーで対象の名前に使う属性。自動は name・名称 などを探し、なければ ID"))
        self.name_field.currentIndexChanged.connect(self._relabel)
        options.addRow(self.tr("名前の属性"), self.name_field)
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
        for w in (self.view, self.goal, self.share, self.first):
            view.addWidget(w)
        view.addStretch(1)
        self.view_bar = QWidget()
        self.view_bar.setLayout(view)
        view.setContentsMargins(0, 0, 0, 0)
        self.view_bar.hide()
        bottom_layout.addWidget(self.view_bar)

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

        actions = QHBoxLayout()
        self.csv_button = QPushButton(self.tr("CSV 保存"))
        self.csv_button.setToolTip(self.tr("一覧を CSV で保存"))
        self.csv_button.clicked.connect(self.save_csv)
        self.layer_button = QPushButton(self.tr("レイヤーとして追加"))
        self.layer_button.setToolTip(self.tr("撮像範囲をレイヤーとして追加（撮像時刻付き。時系列コントローラーで絞り込み可能）"))
        self.layer_button.clicked.connect(self.add_layer)
        self.zoom_button = QPushButton(self.tr("範囲へ移動"))
        self.zoom_button.clicked.connect(self.zoom_to_chosen)
        for b in (self.csv_button, self.layer_button, self.zoom_button):
            actions.addWidget(b)
        bottom_layout.addLayout(actions)
        self._update_buttons()

        # Shortcuts while the dock has the focus only.
        for keys, slot in (("Ctrl+Return", self.run), ("Ctrl+Enter", self.run), ("Esc", self.cancel)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        self.map_tool = QgsMapToolEmitPoint(self.canvas)
        self.map_tool.canvasClicked.connect(self._map_clicked)
        self.map_tool.deactivated.connect(lambda: self.pick.setChecked(False))

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
            if then:
                then()

        def failed(message):
            self.loading_task = None
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
        self.show_targets()

    def targets(self) -> list[PlanTarget] | str:
        """The targets the form asks for (in WGS84), or the reason there are none (as the viewer)."""
        choice = self.target.currentData()
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
        out = []
        for i, f in enumerate(features):
            shape = shape_of(f.geometry(), transform)
            t = target_of([shape], name_of(attributes(f), f.id(), i, field)) if shape else None
            if t:
                out.append(t)
        return out or self.tr("対象に図形なし")

    def show_targets(self) -> None:
        """Draws the targets (and the chosen pass) on the map."""
        if self.target.currentData() == "click":
            self.overlay.clear(["target", "target_point"])
            if self.clicked:
                self.overlay.point_target(*self.clicked)
        else:
            layer = self._target_layer()
            features = self._features(MAX_TARGETS) if layer is not None else []
            self.overlay.targets([f.geometry() for f in features], layer.crs() if layer else WGS84)
        if self.chosen is not None and self.plan:
            self.draw_pass(self.chosen)

    def _pick_toggled(self, on: bool) -> None:
        if on:
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
        targets = self.targets()
        if isinstance(targets, str):
            self.status.setText(targets)
            return
        if not sats:
            self.more.setCollapsed(False)
            self.status.setText(self.tr("衛星が未選択（詳細条件で選択）") if self.tree.items() else self.tr("衛星なし。衛星カタログを確認するか、「詳細条件」に TLE を貼り付け"))
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
        self.view_bar.setVisible(bool(plan["result"].opportunities))
        self.view.blockSignals(True)
        self.view.setCurrentIndex(max(0, self.view.findData(view)))
        self.view.blockSignals(False)
        self.show_view()

    def combining(self) -> bool:
        return self.view.currentData() == "cover"

    def cover_plans(self) -> list:
        plan = self.plan
        goal = min(1.0, max(0.1, self.goal.value() / 100))
        share = self.share.isChecked()
        if self.cover is None or self.cover[0] != goal or self.cover[1] != share:
            self.cover = (goal, share, cover_plan(plan["result"].opportunities, plan["targets"], plan["sats"], goal, 0.02, share))
        return self.cover[2]

    def show_view(self) -> None:
        """Lists the last plan in the chosen view, with what it found in the status line."""
        plan = self.plan
        if not plan:
            return
        combining = self.combining()
        self.goal.setVisible(combining)
        self.share.setVisible(combining and sum(1 for t in plan["targets"] if not t.cells) >= 2)
        self.first.setVisible(not combining)
        self.chosen = None
        if not any(c.key == self.sort[0] for c in self.columns()):
            self.sort = ("time", False)
        self.render()
        result, sats, targets = plan["result"], plan["sats"], plan["targets"]
        problems = describe_problems(result.problems, result.tooSoon)
        if not result.opportunities:
            self.status.setText(self.tr(NO_OPPORTUNITY) + problems)
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
        cols += [
            Column("offNadir", tr("オフナディア角"), lambda op: f"{js_round(op.offNadir)}°",
                   lambda op: tr("入射角 {i}°・距離 {d} km").format(i=js_round(op.incidence), d=js_round(op.range, 0))),
            Column("side", tr("方向"), side_text, lambda op: tr("衛星の進行方向に対して対象が右か左か、北行（昇交）か南行（降交）か")),
            Column("sun", tr("太陽高度"), lambda op: f"{js_round(op.sunElevation, 0)}°"),
        ]
        if options.stations:
            cols.append(Column("downlink", tr("受信見込み"), lambda op: local_text(op.downlink[0]) if op.downlink else tr("1 日以内なし"), downlink_title))
        if self.combining():
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
        ops = [s for p in self.cover_plans() for s in p.steps] if self.combining() else list(plan["result"].opportunities)
        if not self.combining() and self.first.isChecked():
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
        try:
            row = self.model.ops.index(self.chosen)
        except ValueError:
            return
        self.table.selectRow(row)

    def choose(self, op: Opportunity) -> None:
        """Chooses an opportunity: its row, and its pass on the map."""
        self.chosen = op
        self._select_chosen()
        self.draw_pass(op)
        self._update_buttons()
        self.chosenChanged.emit()

    def draw_pass(self, op: Opportunity) -> None:
        plan = self.plan
        sat = plan["sats"][op.satellite]
        shapes = pass_shapes(sat, plan["targets"][op.target], op)
        if shapes is None:
            return
        steps = []
        if self.combining():
            for i, step in enumerate(self.cover_plans()[op.target].steps):
                if step.time == op.time and step.satellite == op.satellite:
                    continue
                other = pass_shapes(plan["sats"][step.satellite], plan["targets"][step.target], step)
                steps.append((i + 1, other.scenes if other else []))
        self.overlay.chosen(shapes, f"{satellite_label(sat)} {local_text(op.time)[5:]}", steps)
        self.canvas.refresh()

    def zoom_to_chosen(self) -> None:
        """Fits the map to the chosen opportunity's target, scenes and satellite (or the combination's passes)."""
        op = self.chosen
        if op is None or not self.plan:
            return
        # A combination shows the target and all its passes; one pass, also where the satellite is.
        box = self.overlay.extent(["scene", "step"] if self.combining() else ["scene", "satellite"])
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
        self.csv_button.setEnabled(any_)
        self.layer_button.setEnabled(any_)
        self.zoom_button.setEnabled(self.chosen is not None)

    # --- Output ---------------------------------------------------------------------------

    def csv_text(self) -> str:
        plan = self.plan
        return plan_csv(self.shown, plan["sats"], plan["targets"], plan["options"].stations)

    def default_file_name(self) -> str:
        options = self.plan["options"]

        def day(ms: float) -> str:
            return datetime.fromtimestamp(ms / 1000).strftime("%Y%m%d")

        return f"imaging-plan_{day(options.start)}-{day(options.end)}.csv"

    def save_csv(self, path: Optional[str] = None) -> None:
        if not self.plan or not self.shown:
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("CSV 保存"), self.default_file_name(), self.tr("CSV (*.csv)"))
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(self.csv_text())
        self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("撮像機会 {n} 件を CSV で保存").format(n=len(self.shown)))

    def layer_name(self) -> str:
        options = self.plan["options"]
        days = math.floor((options.end - options.start) / 86400000 + 0.5)
        return self.tr("撮像計画 {start} から {days} 日（{n} 回）").format(start=local_text(options.start), days=days, n=len(self.shown))

    def add_layer(self) -> Optional[QgsVectorLayer]:
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
            if self.pick.isChecked():
                self.pick.setChecked(False)

    def close_down(self) -> None:
        """Called when the plugin unloads."""
        self.cancel()
        if self.pick.isChecked():
            self.pick.setChecked(False)
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
