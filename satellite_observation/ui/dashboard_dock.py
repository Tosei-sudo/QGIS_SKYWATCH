"""
「ダッシュボード」 (the viewer's dashboard.ts): the project's layers summed
up in a dock.

- An overview of every layer: what it is and how many features it shows.
- For one vector layer (the active one, or the one chosen): counts (all, in
  the view, in the temporal controller's range, after the dashboard's
  filter, selected), features (or a sum or a mean of a number) per value of
  an attribute, the distribution of a number, and the count over time (by
  the layer's temporal settings).
- Where they are: a heatmap layer of the features shown.

The charts filter the layer: clicking a value (Ctrl / Shift: several) or a
bin of the number shows only those features, on the map and in the
attribute table (the dashboard adds its condition to the layer's filter
while it is open, and the project keeps the layer's own filter only);
clicking a step of the time chart sets the temporal controller to it. Each
chart counts the features the other filters leave.
"""
from __future__ import annotations

import json
import math
from typing import Callable, Optional

from qgis.core import (
    QgsCoordinateTransform, QgsDateTimeRange, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeature, QgsFeatureRequest,
    QgsField, QgsGeometry, QgsHeatmapRenderer, QgsMapLayer, QgsProject, QgsProviderRegistry, QgsReadWriteContext,
    QgsStyle, QgsTemporalNavigationObject, QgsUnitTypes, QgsVectorLayer, QgsVectorLayerTemporalContext, QgsVectorLayerTemporalProperties,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QRectF, QSize, Qt, QTimer, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPalette
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.dashboard import (
    OTHERS, STATISTICS, bin_chosen, categories, category_value, choose_range, choose_value, filter_expression, format_time, histogram,
    in_values, number_value, pick_group, pick_measure, short, time_bins,
)
from .docks import PluginDock

HEATMAP = "satobs/dashboard_heatmap"
STATE_TAG = "SatObsDashboard"
ACCENT = QColor("#2563eb")


def _tr(text: str) -> str:
    from qgis.PyQt.QtCore import QCoreApplication

    return QCoreApplication.translate("DashboardDock", text)


def field_kind(field: QgsField) -> str:
    """string, integer, double, date or other, as the viewer's field types."""
    t = field.type()
    if t in (QVariant.Int, QVariant.LongLong, QVariant.UInt, QVariant.ULongLong):
        return "integer"
    if t == QVariant.Double:
        return "double"
    if t == QVariant.String:
        return "string"
    if t in (QVariant.Date, QVariant.DateTime):
        return "date"
    return "other"


def plain(value):
    """An attribute as a Python value (NULL as None, dates as datetime / date)."""
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return None
    if isinstance(value, QDateTime):
        return value.toPyDateTime() if value.isValid() else None
    if isinstance(value, QDate):
        return value.toPyDate() if value.isValid() else None
    if isinstance(value, QVariant):
        return None
    return value


def time_ms(value) -> Optional[float]:
    """A time attribute in ms (a date-time, a date, ms, or ISO text)."""
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return None
    if isinstance(value, QDateTime):
        return float(value.toMSecsSinceEpoch()) if value.isValid() else None
    if isinstance(value, QDate):
        return float(QDateTime(value).toMSecsSinceEpoch()) if value.isValid() else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        t = QDateTime.fromString(value.strip(), Qt.DateFormat.ISODate)
        return float(t.toMSecsSinceEpoch()) if t.isValid() else None
    return None


class BarChart(QWidget):
    """Rows of a label, a bar and a figure; a click says which row and with which keys."""

    clicked = pyqtSignal(int, object)
    ROW = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        #: (label, figure text, fraction, chosen, dimmed, tooltip)
        self.rows: list = []
        self.note = ""
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_rows(self, rows: list, note: str = "") -> None:
        self.rows, self.note = rows, note
        self.setFixedHeight(max(1, len(rows)) * self.ROW + 4)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt's name)
        return QSize(240, max(1, len(self.rows)) * self.ROW + 4)

    def row_at(self, y: float) -> int:
        i = int(y // self.ROW)
        return i if 0 <= i < len(self.rows) else -1

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = self.palette().color(QPalette.ColorRole.WindowText)
        if not self.rows:
            p.setPen(text)
            p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.note)
            return
        w = self.width()
        label_w, value_w = int(w * 0.36), 56
        track_x, track_w = label_w + 6, max(10, w - label_w - value_w - 12)
        for i, (label, figure, fraction, chosen, dimmed, _) in enumerate(self.rows):
            y = i * self.ROW
            color = QColor(text)
            if dimmed:
                color.setAlphaF(0.45)
            p.setPen(color)
            metrics = p.fontMetrics()
            p.drawText(QRectF(0, y, label_w, self.ROW), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       metrics.elidedText(label, Qt.TextElideMode.ElideRight, label_w))
            back = QColor(text)
            back.setAlphaF(0.08)
            p.fillRect(QRectF(track_x, y + 4, track_w, self.ROW - 8), back)
            fill = QColor(ACCENT if chosen or not dimmed else "#94a3b8")
            if dimmed:
                fill.setAlphaF(0.5)
            p.fillRect(QRectF(track_x, y + 4, track_w * max(0.0, min(1.0, fraction)), self.ROW - 8), fill)
            if chosen:
                p.setPen(ACCENT)
                p.drawRect(QRectF(track_x, y + 4, track_w, self.ROW - 8))
                p.setPen(color)
            p.drawText(QRectF(w - value_w, y, value_w, self.ROW), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), figure)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        i = self.row_at(event.pos().y())
        if i >= 0:
            QToolTip.showText(event.globalPos(), self.rows[i][5], self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        i = self.row_at(event.pos().y())
        if i >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(i, event.modifiers())


class ColumnChart(QWidget):
    """Columns over an axis (its two ends labelled); a click says which column and with which keys."""

    clicked = pyqtSignal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        #: (fraction, chosen, dimmed, tooltip)
        self.columns: list = []
        self.ends = ("", "")
        self.note = ""
        self.setMouseTracking(True)
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_columns(self, columns: list, ends: tuple = ("", ""), note: str = "") -> None:
        self.columns, self.ends, self.note = columns, ends, note
        self.update()

    def column_at(self, x: float) -> int:
        if not self.columns:
            return -1
        i = int(x / (self.width() / len(self.columns)))
        return i if 0 <= i < len(self.columns) else -1

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        p = QPainter(self)
        text = self.palette().color(QPalette.ColorRole.WindowText)
        p.setPen(text)
        if not self.columns:
            p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.note)
            return
        axis = 16
        h = self.height() - axis - 2
        width = self.width() / len(self.columns)
        for i, (fraction, chosen, dimmed, _) in enumerate(self.columns):
            color = QColor(ACCENT if chosen or not dimmed else "#94a3b8")
            if dimmed:
                color.setAlphaF(0.5)
            top = h * (1 - max(0.0, min(1.0, fraction)))
            p.fillRect(QRectF(i * width + 1, top, max(1.0, width - 2), h - top), color)
        p.drawLine(0, h, self.width(), h)
        p.drawText(QRectF(0, h + 1, self.width() / 2, axis), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.ends[0])
        p.drawText(QRectF(self.width() / 2, h + 1, self.width() / 2, axis), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                   self.ends[1])

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        i = self.column_at(event.pos().x())
        if i >= 0:
            QToolTip.showText(event.globalPos(), self.columns[i][3], self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        i = self.column_at(event.pos().x())
        if i >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(i, event.modifiers())


def _several(modifiers) -> bool:
    keys = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.MetaModifier
    return bool(int(modifiers) & int(keys))


def _shift(modifiers) -> bool:
    return bool(int(modifiers) & int(Qt.KeyboardModifier.ShiftModifier))


class DashboardDock(PluginDock):
    def __init__(self, iface, context, open_settings: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__("SatObsDashboard", _tr("ダッシュボード"), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.project = QgsProject.instance()
        #: The layer chosen in the list (its id); None: follow the active layer.
        self.pinned: Optional[str] = None
        self.layer: Optional[QgsVectorLayer] = None
        self.group: Optional[str] = None
        self.statistic = "count"
        self.measure: Optional[str] = None
        self.values: set = set()
        self.range: Optional[tuple[float, float]] = None
        self.in_view = False
        #: The values listed by name in the chart (the rest are 「その他」).
        self.listed: set = set()
        #: The figures shown, by tile label (for scripts and tests).
        self.figures: dict = {}
        self.categories: list = []
        self.hist = None
        self.bins = None
        self.heat_layer: Optional[QgsVectorLayer] = None
        # The layer's features, read without the dashboard's condition.
        self._features: list = []
        self._dirty = True
        # The layer filtered now: its id, its own filter and source before the dashboard's condition.
        self._filtered: Optional[str] = None
        self._own_subset = ""
        self._own_source = ""
        self._applying = False
        self._connected: list = []

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self.render)
        self._heat_timer = QTimer(self)
        self._heat_timer.setSingleShot(True)
        self._heat_timer.setInterval(80)
        self._heat_timer.timeout.connect(self._build_heatmap)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        self.overview_head = QLabel()
        box.addWidget(self.overview_head)
        self.overview = QTreeWidget()
        self.overview.setHeaderLabels([_tr("レイヤー"), _tr("種類"), _tr("件数")])
        self.overview.setRootIsDecorated(False)
        self.overview.setMaximumHeight(130)
        self.overview.itemClicked.connect(self._overview_clicked)
        box.addWidget(self.overview)
        row = QHBoxLayout()
        row.addWidget(QLabel(_tr("集計するレイヤー")))
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(self._layer_chosen)
        row.addWidget(self.layer_combo, 1)
        box.addLayout(row)

        self.detail = QWidget()
        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(0, 0, 0, 0)
        self.tiles = QGridLayout()
        detail.addLayout(self.tiles)
        tools = QGridLayout()
        self.in_view_box = QCheckBox(_tr("表示範囲内だけ集計"))
        self.in_view_box.toggled.connect(self._in_view_toggled)
        self.heat_box = QCheckBox(_tr("ヒートマップ"))
        self.heat_box.toggled.connect(self.set_heatmap)
        self.radius = QSlider(Qt.Orientation.Horizontal)
        self.radius.setRange(4, 60)
        self.radius.setValue(16)
        self.radius.setToolTip(_tr("ヒートマップの半径（画素）"))
        self.radius.setVisible(False)
        self.radius.valueChanged.connect(self._radius_changed)
        self.clear_button = QPushButton(_tr("絞り込みを解除"))
        self.clear_button.setVisible(False)
        self.clear_button.clicked.connect(self.clear_filter)
        self.select_button = QPushButton(_tr("地物を選択"))
        self.select_button.setToolTip(_tr("絞り込んだ地物を選択（属性テーブル・書き出し・プロセッシングで利用可能）"))
        self.select_button.clicked.connect(self.select_shown)
        tools.addWidget(self.in_view_box, 0, 0)
        tools.addWidget(self.heat_box, 0, 1)
        tools.addWidget(self.radius, 1, 1)
        tools.addWidget(self.clear_button, 2, 0)
        tools.addWidget(self.select_button, 2, 1)
        detail.addLayout(tools)
        self.edit_note = QLabel()
        self.edit_note.setWordWrap(True)
        self.edit_note.setVisible(False)
        detail.addWidget(self.edit_note)

        detail.addWidget(self._heading(_tr("属性別")))
        row = QHBoxLayout()
        self.group_combo = QComboBox()
        self.group_combo.setToolTip(_tr("集計する属性"))
        self.group_combo.currentIndexChanged.connect(self._group_chosen)
        self.stat_combo = QComboBox()
        self.stat_combo.setToolTip(_tr("集計の方法"))
        for key, name in STATISTICS.items():
            self.stat_combo.addItem(_tr(name), key)
        self.stat_combo.currentIndexChanged.connect(self._statistic_chosen)
        row.addWidget(self.group_combo, 1)
        row.addWidget(self.stat_combo)
        detail.addLayout(row)
        self.bar_chart = BarChart()
        self.bar_chart.clicked.connect(lambda i, m: self.click_value(self.categories[i].value, _several(m)))
        detail.addWidget(self.bar_chart)

        detail.addWidget(self._heading(_tr("数値の分布")))
        self.measure_combo = QComboBox()
        self.measure_combo.setToolTip(_tr("数値の属性"))
        self.measure_combo.currentIndexChanged.connect(self._measure_chosen)
        detail.addWidget(self.measure_combo)
        self.hist_chart = ColumnChart()
        self.hist_chart.clicked.connect(lambda i, m: self.click_bin(i, _shift(m)))
        detail.addWidget(self.hist_chart)
        self.hist_note = QLabel()
        detail.addWidget(self.hist_note)

        detail.addWidget(self._heading(_tr("時間の推移")))
        self.time_chart = ColumnChart()
        self.time_chart.clicked.connect(lambda i, m: self.click_step(i))
        detail.addWidget(self.time_chart)
        box.addWidget(self.detail)
        self.empty = QLabel(_tr("ベクターレイヤーを開くと、件数・属性別の集計・時間の推移・ヒートマップを表示。"))
        self.empty.setWordWrap(True)
        box.addWidget(self.empty)
        box.addStretch(1)
        scroll.setWidget(inner)
        self.body.addWidget(scroll, 1)

        self.visibilityChanged.connect(self._shown)
        self.toggleViewAction().triggered.connect(lambda on: on and self.raise_())
        #: What the dock listens to outside itself, let go when the plugin unloads.
        self._outside = [(iface.currentLayerChanged, self.refresh), (self.project.layersAdded, self.refresh),
                         (self.project.layersWillBeRemoved, self._layers_going), (self.project.layerTreeRoot().layerOrderChanged, self.schedule),
                         (self.project.writeMapLayer, self._write_layer), (self.project.writeProject, self._write_state),
                         (self.project.readProject, self._read_state), (self.project.cleared, self._project_cleared),
                         (self.canvas.extentsChanged, self._extent_changed), (self.canvas.temporalRangeChanged, self.schedule)]
        for signal, slot in self._outside:
            signal.connect(slot)

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(f"<b>{text}</b>")
        label.setTextFormat(Qt.TextFormat.RichText)
        return label

    # ---- opening and the layer ----

    def is_open(self) -> bool:
        return self.isVisible()

    def _shown(self, visible: bool) -> None:
        if visible:
            self._set_layer(self._chosen_layer(), keep=True)
        else:
            self._apply_filter()
            self._remove_heatmap()

    def _usable(self, layer) -> bool:
        return isinstance(layer, QgsVectorLayer) and layer.isValid()

    def _chosen_layer(self) -> Optional[QgsVectorLayer]:
        """The layer chosen, else the active one when it is a vector layer, else the one summed up, else the top one."""
        if self.pinned:
            layer = self.project.mapLayer(self.pinned)
            if self._usable(layer):
                return layer
            self.pinned = None
        active = self.iface.activeLayer()
        if self._usable(active) and active is not self.heat_layer:
            return active
        if self.layer is not None and self._usable(self.layer) and self.project.mapLayer(self.layer.id()) is not None:
            return self.layer
        return next((l for l in self._layers() if self._usable(l) and l is not self.heat_layer), None)

    def _layers(self) -> list:
        return [l for l in self.project.layerTreeRoot().layerOrder() if l is not None]

    def _extent_changed(self) -> None:
        if self.in_view:
            self.schedule()

    def refresh(self, *_) -> None:
        """Layers were added or removed, or another one became active."""
        if not self.isVisible():
            return
        layer = self._chosen_layer()
        if layer is not self.layer:
            self._set_layer(layer)
        else:
            self.schedule()

    def _layers_going(self, ids) -> None:
        ids = set(ids)
        if self.heat_layer is not None and self.heat_layer.id() in ids:
            self.heat_layer = None
        if self.layer is not None and self.layer.id() in ids:
            self._disconnect()
            self._filtered = None if self._filtered in ids else self._filtered
            self.layer = None
            self._features = []
            QTimer.singleShot(0, self.refresh)
        if self.pinned in ids:
            self.pinned = None

    def choose_layer(self, layer: Optional[QgsVectorLayer]) -> None:
        """Sums up `layer` (None: follow the active layer)."""
        self.pinned = layer.id() if layer is not None else None
        self._set_layer(self._chosen_layer())

    def _set_layer(self, layer: Optional[QgsVectorLayer], keep: bool = False) -> None:
        if layer is self.layer and keep:
            self._apply_filter()
            self.schedule()
            return
        changed = layer is not self.layer
        if changed:
            # The filter belongs to the layer it was made on.
            self._restore_filter()
            self._disconnect()
            self.layer = layer
            self.values, self.range, self.group, self.measure = set(), None, None, None
            self._features, self._dirty = [], True
            if layer is not None:
                self._connect(layer)
                self._read()
                fields = self._fields()
                coded = [f.name() for f in layer.fields() if f.editorWidgetSetup().type() == "ValueMap"]
                sample = self._features[:2000]
                index = {f.name(): i for i, f in enumerate(layer.fields())}
                self.group = pick_group(fields, lambda name: [plain(f.attributes()[index[name]]) for f in sample], coded)
                self.measure = pick_measure(fields)
        self._filter_changed()

    def _connect(self, layer: QgsVectorLayer) -> None:
        def dirty(*_):
            if not self._applying:
                self._dirty = True
                self.schedule()

        pairs = [(layer.featureAdded, dirty), (layer.featuresDeleted, dirty), (layer.attributeValueChanged, dirty),
                 (layer.geometryChanged, dirty), (layer.dataChanged, dirty), (layer.selectionChanged, lambda *_: self.schedule()),
                 (layer.subsetStringChanged, self._subset_changed), (layer.editingStarted, self._editing_changed),
                 (layer.editingStopped, self._editing_changed), (layer.updatedFields, dirty)]
        for signal, slot in pairs:
            signal.connect(slot)
        self._connected = [(layer, signal, slot) for signal, slot in pairs]

    def _disconnect(self) -> None:
        for _, signal, slot in self._connected:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connected = []

    def _fields(self) -> list:
        if self.layer is None:
            return []
        pk = set(self.layer.primaryKeyAttributes())
        return [(f.name(), field_kind(f)) for i, f in enumerate(self.layer.fields()) if i not in pk]

    def _read(self) -> None:
        """The layer's features without the dashboard's condition (it is lifted for the reading)."""
        layer = self.layer
        if layer is None:
            self._features = []
            return
        lifted = self._filtered == layer.id() and layer.subsetString() != self._own_subset
        if lifted:
            self._set_subset(self._own_subset)
        try:
            self._features = list(layer.getFeatures(QgsFeatureRequest()))
        finally:
            if lifted:
                self._set_subset(self._filter_subset())
        self._dirty = False

    # ---- the filter ----

    def _subset_changed(self) -> None:
        if self._applying or self.layer is None:
            return
        # The layer's own filter changed: it is the new base, without the dashboard's condition.
        self._filtered = None
        self.values, self.range = set(), None
        self._dirty = True
        self._apply_filter()
        self.schedule()

    def _editing_changed(self) -> None:
        self._apply_filter()
        self.schedule()

    def _condition(self) -> Optional[str]:
        """The values and range chosen as a condition on the layer; None when nothing is chosen."""
        if self.layer is None:
            return None
        group_numeric = False
        if self.group:
            field = self.layer.fields().field(self.group) if self.layer.fields().indexOf(self.group) >= 0 else None
            group_numeric = field is not None and field_kind(field) in ("integer", "double")
        arcgis = self.layer.providerType() == "arcgisfeatureserver"
        return filter_expression(self.group, self.values if self.group else set(), self.listed, self.measure, self.range if self.measure else None,
                                 group_numeric, (lambda name: name) if arcgis else (lambda name: '"' + name.replace('"', '""') + '"'))

    def _filter_subset(self) -> str:
        condition = self._condition()
        own = self._own_subset if self._filtered else (self.layer.subsetString() if self.layer is not None else "")
        if not condition:
            return own
        return f"({own}) AND ({condition})" if own else condition

    def _set_subset(self, subset: str) -> bool:
        self._applying = True
        try:
            ok = self.layer.setSubsetString(subset)
        finally:
            self._applying = False
        return ok

    def _apply_filter(self) -> None:
        """Puts the dashboard's condition on the layer while the dock is open (and takes it off otherwise)."""
        layer = self.layer
        condition = self._condition() if self.isVisible() else None
        self.clear_button.setVisible(bool(self.values or self.range))
        if layer is None:
            return
        if layer.isEditable():
            # QGIS does not change a layer's filter while it is edited.
            self.edit_note.setText(_tr("編集中は地図・属性テーブルの絞り込みなし（集計のみ）"))
            self.edit_note.setVisible(bool(condition))
            return
        self.edit_note.setVisible(False)
        if not condition:
            self._restore_filter()
            return
        if self._filtered != layer.id():
            self._own_subset, self._own_source, self._filtered = layer.subsetString(), layer.source(), layer.id()
        wanted = self._filter_subset()
        if layer.subsetString() != wanted:
            if not self._set_subset(wanted):
                self.edit_note.setText(_tr("このレイヤーは絞り込み不可（集計のみ）"))
                self.edit_note.setVisible(True)
                self._filtered = None
                return
            layer.triggerRepaint()

    def _restore_filter(self) -> None:
        layer = self.project.mapLayer(self._filtered) if self._filtered else None
        if isinstance(layer, QgsVectorLayer) and layer.subsetString() != self._own_subset and not layer.isEditable():
            self._applying = True
            try:
                layer.setSubsetString(self._own_subset)
            finally:
                self._applying = False
            layer.triggerRepaint()
        self._filtered = None

    def _filter_changed(self) -> None:
        self._apply_filter()
        self.schedule()

    def _write_layer(self, layer: QgsMapLayer, element, document) -> None:
        """The project keeps the layer's own filter, not the dashboard's condition."""
        if self._filtered != layer.id():
            return
        source = element.firstChildElement("datasource")
        if source.isNull():
            return
        context = QgsReadWriteContext()
        context.setPathResolver(self.project.pathResolver())
        text = QgsProviderRegistry.instance().absoluteToRelativeUri(layer.providerType(), self._own_source, context)
        while source.hasChildNodes():
            source.removeChild(source.firstChild())
        source.appendChild(document.createTextNode(text))

    # ---- what is chosen ----

    def click_value(self, value: str, several: bool = False) -> None:
        self.values = choose_value(self.values, value, several)
        self._filter_changed()

    def click_bin(self, index: int, extend: bool = False) -> None:
        if self.hist is None or not 0 <= index < len(self.hist.counts):
            return
        low, high = self.hist.bin_range(index)
        self.range = choose_range(self.range, low, high, extend)
        self._filter_changed()

    def click_step(self, index: int) -> None:
        """The temporal controller set to a step of the time chart."""
        if self.bins is None or not 0 <= index < len(self.bins.starts):
            return
        span = QgsDateTimeRange(QDateTime.fromMSecsSinceEpoch(int(self.bins.starts[index])),
                                QDateTime.fromMSecsSinceEpoch(int(self.bins.end_of(index))))
        controller = self.canvas.temporalController()
        if isinstance(controller, QgsTemporalNavigationObject):
            controller.setNavigationMode(QgsTemporalNavigationObject.NavigationMode.FixedRange)
            controller.setTemporalExtents(span)
        self.canvas.setTemporalRange(span)
        self.canvas.mapSettings().setIsTemporal(True)
        panel = self.iface.mainWindow().findChild(QDockWidget, "Temporal Controller") if self.iface.mainWindow() else None
        if panel is not None:
            panel.show()
        self.schedule()

    def set_values(self, values) -> None:
        self.values = set(values)
        self._filter_changed()

    def set_range(self, chosen: Optional[tuple[float, float]]) -> None:
        self.range = chosen
        self._filter_changed()

    def clear_filter(self) -> None:
        self.values, self.range = set(), None
        self._filter_changed()

    def _group_chosen(self) -> None:
        if self.group_combo.signalsBlocked():
            return
        self.group = self.group_combo.currentData() or None
        self.values = set()
        self._filter_changed()

    def _measure_chosen(self) -> None:
        if self.measure_combo.signalsBlocked():
            return
        self.measure = self.measure_combo.currentData() or None
        self.range = None
        self._filter_changed()

    def _statistic_chosen(self) -> None:
        if self.stat_combo.signalsBlocked():
            return
        self.statistic = self.stat_combo.currentData()
        self.schedule()

    def _layer_chosen(self) -> None:
        if self.layer_combo.signalsBlocked():
            return
        layer_id = self.layer_combo.currentData()
        self.choose_layer(self.project.mapLayer(layer_id) if layer_id else None)

    def _overview_clicked(self, item: QTreeWidgetItem) -> None:
        layer = self.project.mapLayer(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if self._usable(layer) and layer is not self.heat_layer:
            self.choose_layer(layer)

    def _in_view_toggled(self, on: bool) -> None:
        self.in_view = on
        self.schedule()

    def select_shown(self) -> None:
        """Selects the features shown (the dashboard's filter and the temporal range)."""
        if self.layer is None:
            return
        ok_time = self._time_test()
        ids = [f.id() for f in self._features if self._passes(f) and ok_time(f)]
        self.layer.selectByIds(ids)
        self.iface.setActiveLayer(self.layer)

    # ---- the heatmap ----

    def set_heatmap(self, on: bool) -> None:
        if self.heat_box.isChecked() != on:
            self.heat_box.setChecked(on)
            return
        self.radius.setVisible(on)
        if not on:
            self._remove_heatmap()
        self.schedule()

    def _radius_changed(self, value: int) -> None:
        if self.heat_layer is not None and isinstance(self.heat_layer.renderer(), QgsHeatmapRenderer):
            self.heat_layer.renderer().setRadius(value)
            self.heat_layer.triggerRepaint()

    def _remove_heatmap(self) -> None:
        self._heat_timer.stop()
        if self.heat_layer is not None:
            layer, self.heat_layer = self.heat_layer, None
            if self.project.mapLayer(layer.id()) is not None:
                self.project.removeMapLayer(layer.id())

    def heatmap_features(self) -> int:
        return self.heat_layer.featureCount() if self.heat_layer is not None else 0

    def _build_heatmap(self) -> None:
        layer = self.layer
        if layer is None or not self.heat_box.isChecked() or not self.isVisible():
            self._remove_heatmap()
            return
        heat = self.heat_layer
        if heat is None or heat.crs() != layer.crs():
            self._remove_heatmap()
            heat = QgsVectorLayer("Point", _tr("ヒートマップ"), "memory")
            heat.setCrs(layer.crs())
            heat.setCustomProperty(HEATMAP, True)
            renderer = QgsHeatmapRenderer()
            renderer.setRadius(self.radius.value())
            renderer.setRadiusUnit(QgsUnitTypes.RenderUnit.RenderPixels)
            ramp = QgsStyle.defaultStyle().colorRamp("Turbo") or QgsStyle.defaultStyle().colorRamp("Spectral")
            if ramp is not None:
                renderer.setColorRamp(ramp)
            heat.setRenderer(renderer)
            heat.setOpacity(0.75)
            self.project.addMapLayer(heat, False)
            self.project.layerTreeRoot().insertLayer(0, heat)
            self.heat_layer = heat
        heat.setName(_tr("ヒートマップ（{name}）").format(name=layer.name()))
        ok_time = self._time_test()
        points = []
        for f in self._features:
            if not (self._passes(f) and ok_time(f)):
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            point = g if g.type() == QgsWkbTypes.GeometryType.PointGeometry and not g.isMultipart() else (
                g.pointOnSurface() if g.type() == QgsWkbTypes.GeometryType.PolygonGeometry else QgsGeometry.fromPointXY(g.boundingBox().center()))
            if point is None or point.isEmpty():
                continue
            feature = QgsFeature()
            feature.setGeometry(point)
            points.append(feature)
        provider = heat.dataProvider()
        provider.truncate()
        provider.addFeatures(points)
        heat.updateExtents()
        heat.triggerRepaint()

    # ---- drawing ----

    def schedule(self, *_) -> None:
        if self.isVisible():
            self._timer.start()

    def _passes(self, feature: QgsFeature) -> bool:
        """Whether a feature passes the values and range chosen."""
        return self._in_values(feature) and self._in_range(feature)

    def _value_of(self, feature: QgsFeature, name: str):
        i = feature.fieldNameIndex(name)
        return plain(feature.attributes()[i]) if i >= 0 else None

    def _in_values(self, feature: QgsFeature) -> bool:
        if not (self.values and self.group):
            return True
        return in_values(category_value(self._value_of(feature, self.group)), self.values, self.listed)

    def _in_range(self, feature: QgsFeature) -> bool:
        if not (self.range and self.measure):
            return True
        n = number_value(self._value_of(feature, self.measure))
        return n is not None and self.range[0] <= n <= self.range[1]

    def _view_test(self):
        extent = self.canvas.extent()
        try:
            transform = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), self.layer.crs(), self.project)
            extent = transform.transformBoundingBox(extent)
        except Exception:  # noqa: BLE001 (a view that cannot be transformed counts everything)
            return lambda f: True
        return lambda f: f.hasGeometry() and f.geometry().boundingBox().intersects(extent)

    def _time_active(self) -> bool:
        props = self.layer.temporalProperties() if self.layer is not None else None
        return bool(props and props.isActive() and self.canvas.mapSettings().isTemporal() and self.canvas.temporalRange().begin().isValid())

    def _time_test(self):
        if not self._time_active():
            return lambda f: True
        context = QgsVectorLayerTemporalContext()
        context.setLayer(self.layer)
        text = self.layer.temporalProperties().createFilterString(context, self.canvas.temporalRange())
        if not text:
            return lambda f: True
        expression = QgsExpression(text)
        expression_context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(self.layer))
        expression.prepare(expression_context)

        def test(f: QgsFeature) -> bool:
            expression_context.setFeature(f)
            return bool(expression.evaluate(expression_context))

        return test

    def _start_of(self):
        """The start time (ms) of a feature by the layer's temporal settings; None when it has none."""
        props = self.layer.temporalProperties()
        modes = (QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField,
                 QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeStartAndEndFromFields,
                 QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeStartAndDurationFromFields)
        if props.mode() in modes and props.startField() and self.layer.fields().indexOf(props.startField()) >= 0:
            name = props.startField()
            return lambda f: time_ms(f[name])
        if props.mode() == QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeStartAndEndFromExpressions and props.startExpression():
            expression = QgsExpression(props.startExpression())
            context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(self.layer))
            expression.prepare(context)

            def start(f):
                context.setFeature(f)
                return time_ms(expression.evaluate(context))

            return start
        return None

    def render(self) -> None:
        if not self.isVisible():
            return
        self._render_overview()
        self._render_choices()
        layer = self.layer
        self.detail.setVisible(layer is not None)
        self.empty.setVisible(layer is None)
        if layer is None:
            self.figures = {}
            self._remove_heatmap()
            return
        if self._dirty:
            self._read()
        features = self._features
        in_view = self._view_test()
        ok_view = in_view if self.in_view else (lambda f: True)
        ok_time = self._time_test()
        filtering = bool(self.values and self.group) or bool(self.range and self.measure)
        # Each chart counts what the other filters leave.
        base = [f for f in features if ok_view(f) and ok_time(f)]
        shown = [f for f in base if self._passes(f)] if filtering else base
        tiles = [(_tr("地物"), len(features)), (_tr("表示範囲内"), sum(1 for f in features if in_view(f)))]
        if self._time_active():
            tiles.append((_tr("期間内"), sum(1 for f in features if ok_time(f))))
        if filtering:
            tiles.append((_tr("絞り込み後"), len(shown)))
        tiles.append((_tr("選択中"), layer.selectedFeatureCount()))
        self._render_tiles(tiles)
        self._render_categories([f for f in base if self._in_range(f)])
        self._render_histogram([f for f in base if self._in_values(f)])
        self._render_time([f for f in features if ok_view(f) and self._passes(f)])
        if self.heat_box.isChecked():
            self._heat_timer.start()

    def _render_overview(self) -> None:
        layers = self._layers()
        vectors = [l for l in layers if isinstance(l, QgsVectorLayer)]
        self.overview_head.setText(_tr("レイヤー {n}（ベクター {v}・画像など {o}）").format(n=len(layers), v=len(vectors), o=len(layers) - len(vectors)))
        self.overview.clear()
        for layer in layers:
            if isinstance(layer, QgsVectorLayer):
                kind = layer.providerType() if layer.providerType() not in ("ogr", "memory") else (_tr("ベクター") if layer.providerType() == "ogr" else _tr("メモリ"))
                n = layer.featureCount()
                total = len(self._features) if layer is self.layer and self._filtered == layer.id() else n
                count = _tr("{n} 件").format(n=short(n)) if n == total else _tr("{n} / {t} 件").format(n=short(n), t=short(total))
            else:
                kind = _tr("画像") if layer.type() == QgsMapLayer.LayerType.RasterLayer else _tr("レイヤー")
                node = self.project.layerTreeRoot().findLayer(layer.id())
                count = "" if node is None or node.isVisible() else _tr("非表示")
            item = QTreeWidgetItem([layer.name(), kind, count])
            item.setData(0, Qt.ItemDataRole.UserRole, layer.id())
            item.setToolTip(0, layer.name())
            if layer is self.layer:
                font = item.font(0)
                font.setBold(True)
                for c in range(3):
                    item.setFont(c, font)
            if not isinstance(layer, QgsVectorLayer) or layer is self.heat_layer:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setToolTip(1, _tr("このレイヤーを集計"))
            self.overview.addTopLevelItem(item)
        for c in range(3):
            self.overview.resizeColumnToContents(c)

    def _render_choices(self) -> None:
        for combo in (self.layer_combo, self.group_combo, self.measure_combo, self.stat_combo):
            combo.blockSignals(True)
        try:
            self.layer_combo.clear()
            self.layer_combo.addItem(_tr("アクティブなレイヤー"), "")
            for layer in self._layers():
                if self._usable(layer) and layer is not self.heat_layer:
                    self.layer_combo.addItem(layer.name(), layer.id())
            self.layer_combo.setCurrentIndex(max(0, self.layer_combo.findData(self.pinned)) if self.pinned else 0)
            fields = self._fields()
            alias = {f.name(): f.displayName() for f in self.layer.fields()} if self.layer is not None else {}
            self.group_combo.clear()
            self.group_combo.addItem(_tr("（なし）"), "")
            for name, _ in fields:
                self.group_combo.addItem(alias.get(name, name), name)
            self.group_combo.setCurrentIndex(max(0, self.group_combo.findData(self.group or "")))
            self.measure_combo.clear()
            self.measure_combo.addItem(_tr("（なし）"), "")
            for name, kind in fields:
                if kind in ("integer", "double"):
                    self.measure_combo.addItem(alias.get(name, name), name)
            self.measure_combo.setCurrentIndex(max(0, self.measure_combo.findData(self.measure or "")))
            # A sum or a mean needs a number.
            model = self.stat_combo.model()
            for i in range(self.stat_combo.count()):
                model.item(i).setEnabled(self.stat_combo.itemData(i) == "count" or bool(self.measure))
            if not self.measure:
                self.statistic = "count"
            self.stat_combo.setCurrentIndex(self.stat_combo.findData(self.statistic))
        finally:
            for combo in (self.layer_combo, self.group_combo, self.measure_combo, self.stat_combo):
                combo.blockSignals(False)

    def _render_tiles(self, tiles: list) -> None:
        while self.tiles.count():
            item = self.tiles.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.figures = {}
        for i, (label, n) in enumerate(tiles):
            tile = QLabel(f"<span style='font-size:15pt;font-weight:600'>{short(n)}</span><br/>{label}")
            tile.setTextFormat(Qt.TextFormat.RichText)
            tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tile.setFrameShape(QFrame.Shape.StyledPanel)
            self.tiles.addWidget(tile, i // 3, i % 3)
            self.figures[label] = n

    def _render_categories(self, features: list) -> None:
        group = self.group
        if not group or self.layer.fields().indexOf(group) < 0:
            self.categories, self.listed = [], set()
            self.bar_chart.set_rows([], _tr("属性を選ぶと、値ごとに集計"))
            return
        measure = self.measure if self.statistic != "count" else None
        rows = [(category_value(self._value_of(f, group)), number_value(self._value_of(f, measure)) if measure else None) for f in features]
        self.categories, self.listed = categories(rows, self.statistic, self.values)
        if OTHERS in self.values and self._filtered:
            # 「その他」 depends on what is listed: the layer's condition follows it.
            self._apply_filter()
        setup = self.layer.editorWidgetSetup(self.layer.fields().indexOf(group))
        codes = {}
        if setup.type() == "ValueMap":
            for entry in setup.config().get("map") or []:
                if isinstance(entry, dict):
                    codes.update({str(v): k for k, v in entry.items()})
            if isinstance(setup.config().get("map"), dict):
                codes.update({str(v): k for k, v in setup.config()["map"].items()})

        def name_of(v: str) -> str:
            return _tr("その他") if v == OTHERS else _tr("（空）") if v == "" else codes.get(v, v)

        figures = [abs(c.figure) if not math.isnan(c.figure) else 0 for c in self.categories]
        most = max(figures, default=0) or 1
        chosen = bool(self.values)
        statistic = _tr(STATISTICS[self.statistic])
        self.bar_chart.set_rows([
            (name_of(c.value), short(c.figure), f / most, c.value in self.values, chosen and c.value not in self.values,
             _tr("{name}: {stat} {value}（{n} 件）— クリックで絞り込み、Ctrl / Shift で複数").format(
                 name=name_of(c.value), stat=statistic, value=short(c.figure), n=f"{c.count:,}"))
            for c, f in zip(self.categories, figures)
        ], _tr("地物なし"))

    def _render_histogram(self, features: list) -> None:
        measure = self.measure
        if not measure or self.layer.fields().indexOf(measure) < 0:
            self.hist = None
            self.hist_chart.set_columns([], note=_tr("数値の属性なし"))
            self.hist_note.setText("")
            return
        values = [n for n in (number_value(self._value_of(f, measure)) for f in features) if n is not None]
        self.hist = histogram(values, self.range)
        if self.hist is None:
            self.hist_chart.set_columns([], note=_tr("値なし"))
            self.hist_note.setText("")
            return
        h = self.hist
        most = max(h.counts) or 1
        columns = []
        for i, c in enumerate(h.counts):
            low, high = h.bin_range(i)
            on = bin_chosen(self.range, low, high)
            columns.append((c / most, on, self.range is not None and not on,
                            _tr("{a} 〜 {b}: {n} 件 — クリックで絞り込み、Shift で範囲を拡張").format(a=short(low), b=short(high), n=f"{c:,}")))
        self.hist_chart.set_columns(columns, (short(h.low), short(h.high)))
        self.hist_note.setText(_tr("件数 {n}・最小 {lo}・平均 {mean}・最大 {hi}").format(n=f"{h.n:,}", lo=short(h.low), mean=short(h.mean), hi=short(h.high)))

    def _render_time(self, features: list) -> None:
        start_of = self._start_of()
        if start_of is None:
            self.bins = None
            self.time_chart.set_columns([], note=_tr("時間の属性なし（レイヤーのプロパティの「時系列」で設定）"))
            return
        times = [t for t in (start_of(f) for f in features) if t is not None]
        self.bins = time_bins(times)
        if self.bins is None:
            self.time_chart.set_columns([], note=_tr("時間のある地物なし"))
            return
        b = self.bins
        most = max(b.counts) or 1
        window = self.canvas.temporalRange() if self._time_active() else None
        lo = window.begin().toMSecsSinceEpoch() if window is not None else None
        hi = window.end().toMSecsSinceEpoch() if window is not None and window.end().isValid() else math.inf
        columns = []
        for i, start in enumerate(b.starts):
            end = b.end_of(i)
            inside = window is not None and start <= hi and end >= lo
            columns.append((b.counts[i] / most, inside, window is not None and not inside,
                            _tr("{t}: {n} 件 — クリックで時系列コントローラーをこの期間に").format(t=format_time(start, b.unit), n=f"{b.counts[i]:,}")))
        self.time_chart.set_columns(columns, (format_time(b.first, b.unit), format_time(b.last, b.unit)))

    # ---- the project ----

    def get_state(self) -> dict:
        return {"open": self.isVisible(), "layer": self.pinned, "groupBy": self.group, "statistic": self.statistic, "measure": self.measure,
                "values": sorted(v for v in self.values if v != OTHERS), "range": list(self.range) if self.range else None,
                "inView": self.in_view, "heatmap": self.heat_box.isChecked(), "radius": self.radius.value()}

    def set_state(self, state: dict) -> None:
        layer = self.project.mapLayer(state.get("layer") or "") if isinstance(state.get("layer"), str) else None
        self.pinned = layer.id() if self._usable(layer) else None
        if state.get("statistic") in STATISTICS:
            self.statistic = state["statistic"]
        if isinstance(state.get("inView"), bool):
            self.in_view_box.setChecked(state["inView"])
        if isinstance(state.get("radius"), (int, float)):
            self.radius.setValue(int(state["radius"]))
        if isinstance(state.get("open"), bool):
            self.setVisible(state["open"])
        self._set_layer(self._chosen_layer())
        names = {name for name, _ in self._fields()}
        if state.get("groupBy") is None or state.get("groupBy") in names:
            self.group = state.get("groupBy")
        if state.get("measure") is None or state.get("measure") in names:
            self.measure = state.get("measure")
        self.values = {str(v) for v in state.get("values") or []}
        chosen = state.get("range")
        self.range = (float(chosen[0]), float(chosen[1])) if isinstance(chosen, list) and len(chosen) == 2 and all(
            isinstance(x, (int, float)) for x in chosen) else None
        if isinstance(state.get("heatmap"), bool):
            self.set_heatmap(state["heatmap"])
        self._filter_changed()

    def _write_state(self, document) -> None:
        element = document.createElement(STATE_TAG)
        element.appendChild(document.createTextNode(json.dumps(self.get_state(), ensure_ascii=False)))
        document.documentElement().appendChild(element)

    def _read_state(self, document) -> None:
        # Heatmaps left in the project file are rebuilt when needed.
        stale = [l.id() for l in self.project.mapLayers().values() if l.customProperty(HEATMAP)]
        if stale:
            self.project.removeMapLayers(stale)
        element = document.documentElement().firstChildElement(STATE_TAG)
        if element.isNull():
            return
        try:
            state = json.loads(element.text())
        except ValueError:
            return
        if isinstance(state, dict):
            QTimer.singleShot(0, lambda: self.set_state(state))

    def _project_cleared(self) -> None:
        self._disconnect()
        self.layer, self.pinned, self.heat_layer, self._filtered = None, None, None, None
        self._features, self.values, self.range = [], set(), None

    def close_all(self) -> None:
        """Takes the condition off the layer and the heatmap away (the plugin unloads)."""
        self._restore_filter()
        self._remove_heatmap()
        self._disconnect()
        for signal, slot in self._outside:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
