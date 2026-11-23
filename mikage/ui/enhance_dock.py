"""
画像補正: the viewer's correction panel for the active raster layer —
補正 on or off, a preset, DRA (method, clip, RGB 連動, 範囲を固定), the
bands shown as R, G, B, opacity and the sliders. Changes draw after a short
pause; the correction is kept on the layer (so in the project) and comes
back when a project is opened.

The correction is built as the viewer's panel builds it: DRA first, then one
step per kind in the order of its first slider, neutral steps left out, then
the tone curve (トーンカーブ, edited on its own plot). Steps that have no
control (white balance or a stretch from `imageRules` or a saved correction)
are kept after them.

With a group chosen in the layer panel (no active layer), the dock works on
every raster under it (nested groups and hidden layers too, not background
maps): it shows the top one's correction and any change writes the whole
correction to all of them. The band assignment is shared only when their
bands are alike (`enhance_group.alike`), else each keeps its own; a colour
scale goes to the layers shown as the top one is (one band or three); two
dates stay a one-layer tool. 「グループで範囲を共通化」 (on by default for
alike rasters) has DRA and the 16-bit stretch taken over all of them
together, so tiles meet without seams.

The order (tsudo, 2026-11-18: the colour scale was often changed by mistake,
the red and cyan comparison is used often): the preset, DRA, then two dates
(open; under DRA, tsudo the same day); then bands, the sliders and the tone curve; the colour
scale folded near the bottom, by the histogram. The wheel scrolls the dock
and changes a choice or slider only once it was clicked (wheel_guard).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsCoordinateTransform, QgsLayerTreeGroup, QgsMapLayerType, QgsProject, QgsProviderRegistry, QgsRasterLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider,
    QVBoxLayout, QWidget,
)

from ..core.enhance.api import OP_INFO
from ..core.enhance.api import PRESET_NAMES, PRESETS, Pipeline
from ..core.enhance.api import image_rule_for, resolve_bands
from ..qgis_io import enhance_group as eg
from ..qgis_io import enhance_renderer as er
from ..qgis_io import complex_sar
from ..qgis_io import quick_pan
from ..qgis_io.tasks import run_task
from .colorize_box import ColorizeBox, paired_with, set_blend
from .curve_editor import CurveEditor
from .docks import PluginDock
from .histogram_widget import HistogramPlot, StatsTable
from .quick_pan import QuickPanBox
from .wheel_guard import guard_wheel

TITLE = "画像補正"

#: The sliders: (step, parameter, label, min, max, step). The viewer's defaultEnhanceSliders, with shadows, highlights and tint added.
SLIDERS = [
    ("exposure", "ev", "露出 (EV)", -3, 3, 0.05),
    ("brightness", "amount", "明るさ", -1, 1, 0.01),
    ("contrast", "amount", "コントラスト", -1, 1, 0.01),
    ("gamma", "gamma", "ガンマ", 0.2, 3, 0.01),
    ("shadows", "amount", "シャドウ", -1, 1, 0.01),
    ("highlights", "amount", "ハイライト", -1, 1, 0.01),
    ("levels", "inBlack", "レベル 黒", 0, 0.5, 0.005),
    ("levels", "inWhite", "レベル 白", 0.5, 1, 0.005),
    ("levels", "gamma", "レベル 中間", 0.2, 3, 0.01),
    ("temperature", "amount", "色温度", -1, 1, 0.01),
    ("tint", "amount", "色かぶり", -1, 1, 0.01),
    ("saturation", "amount", "彩度", -1, 1, 0.01),
    ("sharpen", "amount", "シャープ 量", 0, 3, 0.05),
    ("sharpen", "radius", "シャープ 半径", 0.3, 5, 0.1),
]
SLIDER_OPS = {s[0] for s in SLIDERS}
METHODS = [("percentClip", "パーセントクリップ"), ("minMax", "最小〜最大"), ("standardDeviation", "標準偏差（±2σ）")]
#: The pause after a change before drawing, ms.
DELAY = 150
#: The pause after the view or correction changes before the histogram is taken again, ms.
HISTOGRAM_DELAY = 300
#: The pause after the layer tree changes before the group's rasters and shared ranges are looked at again, ms.
TREE_DELAY = 300


def source_name(layer) -> str:
    """What the layer was opened from (a path, or the URL behind /vsicurl/; a complex SAR image's own path), else its name."""
    shown = complex_sar.source_of(layer)
    if shown:
        return shown
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
        path = str(parts.get("path") or parts.get("url") or "")
    except Exception:  # noqa: BLE001 (an unusual source: the layer's name)
        path = ""
    return re.sub(r"^/vsi\w+/", "", path) or layer.name()


def neutral(op: str, param: str) -> float:
    """A slider's neutral value: the parameter's default (0 for the sharpen amount)."""
    return 0.0 if (op, param) == ("sharpen", "amount") else float(OP_INFO[op]["params"][param]["default"])


class SliderRow:
    """A slider with its value box; double-clicking the name puts it back."""

    def __init__(self, op: str, param: str, label: str, lo: float, hi: float, step: float, changed: Callable[[], None]):
        self.op, self.param, self.lo, self.step = op, param, lo, step
        self.neutral = neutral(op, param)
        self.label = QLabel(label)
        self.label.setToolTip("ダブルクリックで元の値")
        self.label.mouseDoubleClickEvent = lambda _e: (self.set(self.neutral), changed())
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, round((hi - lo) / step))
        self.box = QDoubleSpinBox()
        self.box.setRange(lo, hi)
        self.box.setSingleStep(step)
        self.box.setDecimals(min(3, max(0, -math.floor(math.log10(step) + 1e-9))))
        self.box.setKeyboardTracking(False)
        self._quiet = False
        self.slider.valueChanged.connect(lambda i: self._from_slider(i, changed))
        self.box.valueChanged.connect(lambda v: self._from_box(v, changed))
        self.widget = QWidget()
        row = QHBoxLayout(self.widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.slider, 1)
        row.addWidget(self.box)
        self.set(self.neutral)

    def value(self) -> float:
        return float(self.box.value())

    def set(self, v: float) -> None:
        self._quiet = True
        self.box.setValue(v)
        self.slider.setValue(round((v - self.lo) / self.step))
        self._quiet = False

    def _from_slider(self, i: int, changed) -> None:
        if self._quiet:
            return
        self._quiet = True
        self.box.setValue(self.lo + i * self.step)
        self._quiet = False
        changed()

    def _from_box(self, v: float, changed) -> None:
        if self._quiet:
            return
        self._quiet = True
        self.slider.setValue(round((v - self.lo) / self.step))
        self._quiet = False
        changed()

    def show(self, shown: bool) -> None:
        self.label.setVisible(shown)
        self.widget.setVisible(shown)


class EnhanceDock(PluginDock):
    def __init__(self, iface, context, open_settings: Callable[[], None], parent=None):
        super().__init__("SatObsEnhance", TITLE, context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        #: The layer whose correction the dock shows (with a group, its top raster).
        self.layer: Optional[QgsRasterLayer] = None
        #: The layer panel's group the dock works on, or None (the active layer).
        self.group: Optional[QgsLayerTreeGroup] = None
        #: The layers changes go to: the group's rasters, or the layer.
        self.targets: list = []
        #: Whether the targets' bands are alike (one band assignment fits all).
        self._same = True
        self._loading = False
        #: Steps without a slider, kept after the sliders' steps.
        self._extra: list = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DELAY)
        self._timer.timeout.connect(self.apply)

        self.layer_label = QLabel()
        self.layer_label.setWordWrap(True)
        self.body.addWidget(self.layer_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        v = QVBoxLayout(page)
        scroll.setWidget(page)
        self.body.addWidget(scroll, 1)

        top = QHBoxLayout()
        self.enabled = QCheckBox(self.tr("補正"))
        self.enabled.toggled.connect(self.changed)
        top.addWidget(self.enabled)
        top.addWidget(QLabel(self.tr("プリセット")))
        self.preset = QComboBox()
        self.preset.addItem(self.tr("（選択）"), "")
        for name, label in PRESET_NAMES.items():
            self.preset.addItem(label, name)
        self.preset.activated.connect(lambda _i: self.use_preset(self.preset.currentData()))
        top.addWidget(self.preset, 1)
        v.addLayout(top)
        self.share = QCheckBox(self.tr("グループで範囲を共通化"))
        self.share.setToolTip(self.tr("グループ内の画像を合わせて明るさの範囲（DRA・16 bit 画像の引き伸ばし）を決め、"
                                      "タイルの境目などで明るさが変わらないように表示"))
        self.share.toggled.connect(self._share_toggled)
        self.share.hide()
        v.addWidget(self.share)
        # クイックパンシャープン: shown only while the layer is in it (started from the layer panel or the menu).
        self.quick_box = QuickPanBox()
        v.addWidget(self.quick_box)


        dra = QGroupBox(self.tr("DRA（表示範囲で自動レンジ）"))
        form = QFormLayout(dra)
        self.dra_on = QCheckBox(self.tr("オン"))
        self.dra_method = QComboBox()
        for key, label in METHODS:
            self.dra_method.addItem(label, key)
        self.dra_clip = QDoubleSpinBox()
        self.dra_clip.setRange(0, 25)
        self.dra_clip.setSingleStep(0.1)
        self.dra_clip.setDecimals(2)
        self.dra_clip.setValue(0.5)
        self.dra_linked = QCheckBox(self.tr("RGB 連動"))
        self.dra_locked = QCheckBox(self.tr("範囲を固定"))
        for w in (self.dra_on, self.dra_linked, self.dra_locked):
            w.toggled.connect(self.changed)
        self.dra_method.currentIndexChanged.connect(self.changed)
        self.dra_clip.valueChanged.connect(self.changed)
        form.addRow(self.dra_on)
        form.addRow(self.tr("方式"), self.dra_method)
        form.addRow(self.tr("クリップ (%)"), self.dra_clip)
        form.addRow(self.dra_linked)
        form.addRow(self.dra_locked)
        v.addWidget(dra)

        # Two dates (used often) under DRA; the colour scale itself further down, folded (below).
        self.colorize_box = ColorizeBox(lambda text: self.iface.messageBar().pushInfo(TITLE, text))
        self.colorize_box.changed.connect(self._colorize_changed)
        self.colorize_box.colored.connect(self.refresh)
        v.addWidget(self.colorize_box.pair_box)

        self.bands_box = QGroupBox(self.tr("バンド割当"))
        form = QFormLayout(self.bands_box)
        self.band_combos = []
        for label in ("R（赤）", "G（緑）", "B（青）"):
            combo = QComboBox()
            combo.currentIndexChanged.connect(self.changed)
            form.addRow(self.tr(label), combo)
            self.band_combos.append(combo)
        v.addWidget(self.bands_box)

        adjust = QGroupBox(self.tr("補正"))
        form = QFormLayout(adjust)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(100)
        self.opacity.valueChanged.connect(self.changed)
        form.addRow(self.tr("不透明度"), self.opacity)
        self.rows = [SliderRow(*s, changed=self.changed) for s in SLIDERS]
        for row in self.rows:
            form.addRow(row.label, row.widget)
        self.extra_label = QLabel()
        self.extra_label.setWordWrap(True)
        form.addRow(self.extra_label)
        v.addWidget(adjust)

        self.curve_box = QGroupBox(self.tr("トーンカーブ"))
        self.curve_box.setCheckable(True)
        self.curve_box.setChecked(False)
        cv = QVBoxLayout(self.curve_box)
        self.curve = CurveEditor()
        self.curve.changed.connect(self.changed)
        cv.addWidget(self.curve)
        self.curve_box.toggled.connect(self.curve.setVisible)
        self.curve.setVisible(False)
        v.addWidget(self.curve_box)

        buttons = QHBoxLayout()
        reset = QPushButton(self.tr("リセット"))
        reset.clicked.connect(self.reset)
        buttons.addWidget(reset)
        self.off_button = QPushButton(self.tr("QGIS の表示に戻す"))
        self.off_button.clicked.connect(lambda: self.enabled.setChecked(False))
        buttons.addWidget(self.off_button)
        v.addLayout(buttons)

        v.addWidget(self.colorize_box)

        self.histogram_box = QGroupBox(self.tr("ヒストグラム（表示範囲）"))
        self.histogram_box.setCheckable(True)
        self.histogram_box.setChecked(False)
        hv = QVBoxLayout(self.histogram_box)
        row = QHBoxLayout()
        self.histogram_after = QComboBox()
        self.histogram_after.addItem(self.tr("補正後"), True)
        self.histogram_after.addItem(self.tr("補正前"), False)
        self.histogram_after.currentIndexChanged.connect(self.show_histogram)
        row.addWidget(self.histogram_after)
        self.histogram_log = QCheckBox(self.tr("対数"))
        row.addWidget(self.histogram_log)
        row.addStretch(1)
        hv.addLayout(row)
        self.histogram_log.setToolTip(self.tr("縦軸を対数にして、少ない値も表示"))
        self.histogram_readout = QLabel()
        self.histogram_plot = HistogramPlot(self.histogram_readout.setText)
        self.histogram_log.toggled.connect(self.histogram_plot.set_log)
        hv.addWidget(self.histogram_plot)
        hv.addWidget(self.histogram_readout)
        self.histogram_table = StatsTable()
        hv.addWidget(self.histogram_table)
        self.histogram_note = QLabel()
        self.histogram_note.setWordWrap(True)
        hv.addWidget(self.histogram_note)
        self._histogram_body = [self.histogram_after, self.histogram_log, self.histogram_plot, self.histogram_readout,
                                self.histogram_table, self.histogram_note]
        self.histogram_box.toggled.connect(self._histogram_toggled)
        self._histogram_toggled(False)
        v.addWidget(self.histogram_box)
        v.addStretch(1)
        #: The wheel scrolls the dock; a choice or slider takes it only once clicked.
        self.wheel_guard = guard_wheel(page)

        #: The last histograms ({"before", "after"}, 256 counts per channel), and the number of the request whose answer is still wanted.
        self.histograms: Optional[dict] = None
        self._histogram_corrected = False
        #: The number of a group's rasters the histogram counts (0: one layer).
        self._histogram_layers = 0
        self._histogram_request = 0
        self._histogram_timer = QTimer(self)
        self._histogram_timer.setSingleShot(True)
        self._histogram_timer.setInterval(HISTOGRAM_DELAY)
        self._histogram_timer.timeout.connect(self.take_histogram)

        iface.currentLayerChanged.connect(self.follow_layer)
        #: The layer panel, for a group chosen there (QGIS's; tests may have none).
        self.view = iface.layerTreeView() if hasattr(iface, "layerTreeView") else None
        selection = self.view.selectionModel() if self.view is not None else None
        if selection is not None:
            selection.currentChanged.connect(self._tree_current_changed)
        self._tree_timer = QTimer(self)
        self._tree_timer.setSingleShot(True)
        self._tree_timer.setInterval(TREE_DELAY)
        self._tree_timer.timeout.connect(self._tree_changed)
        root = QgsProject.instance().layerTreeRoot()
        root.addedChildren.connect(self._tree_later)
        root.removedChildren.connect(self._tree_later)
        self.canvas.extentsChanged.connect(self.update_views)
        self.canvas.destinationCrsChanged.connect(self.update_views)
        self.canvas.mapCanvasRefreshed.connect(self.histogram_later)
        project = QgsProject.instance()
        #: True while a project is being read: its layers keep their own look (no imageRules).
        self._reading = False
        project.layersAdded.connect(self.layers_added)
        project.layersWillBeRemoved.connect(self.layers_removed)
        project.loadingLayer.connect(self._start_reading)
        project.readProject.connect(self._project_read)
        quick_pan.signals().changed.connect(self._quick_pan_changed)
        self.restore_layers(project.mapLayers().values())
        self.follow_layer(iface.activeLayer())

    def close_down(self) -> None:
        self._timer.stop()
        self.quick_box.close_down()
        self._histogram_timer.stop()
        self._tree_timer.stop()
        self._histogram_request += 1
        selection = self.view.selectionModel() if self.view is not None and not sip.isdeleted(self.view) else None
        root = QgsProject.instance().layerTreeRoot()
        for signal, slot in [(self.canvas.mapCanvasRefreshed, self.histogram_later),
                             (root.addedChildren, self._tree_later), (root.removedChildren, self._tree_later),
                             *([(selection.currentChanged, self._tree_current_changed)] if selection is not None else []),
                             (self.iface.currentLayerChanged, self.follow_layer), (self.canvas.extentsChanged, self.update_views),
                             (self.canvas.destinationCrsChanged, self.update_views),
                             (QgsProject.instance().layersAdded, self.layers_added),
                             (QgsProject.instance().layersWillBeRemoved, self.layers_removed),
                             (QgsProject.instance().loadingLayer, self._start_reading),
                             (quick_pan.signals().changed, self._quick_pan_changed),
                             (QgsProject.instance().readProject, self._project_read)]:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    # ---- layers ----

    def _quick_pan_changed(self, ms_id: str) -> None:
        """クイックパンシャープン started, changed or ended: shown again when it is the layer shown (or its pan)."""
        if self.group is not None or self.layer is None or sip.isdeleted(self.layer):
            return
        link = quick_pan.link_of(self.layer) or quick_pan.used_as_pan(self.layer)
        if self.layer.id() == ms_id or link is not None or not self.quick_box.isHidden():
            self.refresh()

    def _start_reading(self, *_args) -> None:
        self._reading = True

    def _project_read(self, *_args) -> None:
        self._reading = False
        self.restore_layers(QgsProject.instance().mapLayers().values())
        eg.sync_shares()

    def layers_added(self, layers) -> None:
        """Layers from a project get their correction back; images opened now get their `imageRules`."""
        layers = list(layers)
        self.restore_layers(layers)
        if self._reading:
            return
        for layer in layers:
            if isinstance(layer, QgsRasterLayer) and er.enhance_renderer(layer) is None and not layer.customProperty(er.PROPERTY):
                self.apply_rule(layer)

    def layers_removed(self, ids) -> None:
        ids = set(ids)
        er.forget(ids)
        if self.group is not None:
            if any(layer.id() in ids for layer in self.targets if not sip.isdeleted(layer)):
                self.show_target(None, self.group if not sip.isdeleted(self.group) else None, skip=ids)
        elif self.layer is not None and self.layer.id() in ids:
            self.follow_layer(None)

    def _tree_later(self, *_args) -> None:
        self._tree_timer.start()

    def _tree_changed(self) -> None:
        """The layer tree changed: layers moved out of a group leave its shared range; the group's rasters again."""
        if self._reading:
            return
        eg.sync_shares()
        if self.group is None:
            return
        if sip.isdeleted(self.group):
            self.follow_layer(self.iface.activeLayer())
        elif [layer.id() for layer in eg.group_rasters(self.group)] != [layer.id() for layer in self.targets]:
            self.refresh()

    def _tree_current_changed(self, *_args) -> None:
        """A node chosen in the layer panel: a group is followed here (a layer comes as the active layer)."""
        group = self.chosen_group()
        if group is not None and group is not self.group:
            self.show_target(None, group)
        elif group is None and self.group is not None:
            self.follow_layer(self.iface.activeLayer())

    def chosen_group(self) -> Optional[QgsLayerTreeGroup]:
        """The layer panel's current node when it is a group (not the root), else None."""
        view = self.view
        if view is None or sip.isdeleted(view):
            return None
        group = view.currentGroupNode()
        if isinstance(group, QgsLayerTreeGroup) and group.parent() is not None and view.currentNode() is group:
            return group
        return None

    def _is_target(self, layer) -> bool:
        return any(t is layer or (not sip.isdeleted(t) and t.id() == layer.id()) for t in self.targets)

    def apply_rule(self, layer: QgsRasterLayer):
        """Opens `layer` with the first of `imageRules` matching its file name; the rule, or None."""
        rules = self.context.config.image_rules if self.context.config else []
        rule = image_rule_for(rules, source_name(layer)) if rules else None
        if rule is None or not layer.isValid():
            return None
        if rule.pipeline is None and rule.bands is None and rule.no_data is None:
            # Only `complexImages` (the plugin's conversion when the image was added): the look it gave stays.
            return rule
        provider = layer.dataProvider()
        settings = er.default_settings(layer)
        problem = None
        if rule.bands:
            try:
                names = [provider.bandDescription(b) for b in range(1, layer.bandCount() + 1)]
                settings = replace(settings, bands=resolve_bands(rule.bands, layer.bandCount(), names))
            except ValueError as error:
                problem = self.tr("設定「{label}」のバンド割り当ての使用に失敗（{why}）").format(label=rule.label, why=error)
        if rule.no_data:
            settings = replace(settings, no_data=tuple(rule.no_data.get("values", ())))
            if rule.no_data.get("auto") is False:
                for b in range(1, layer.bandCount() + 1):
                    provider.setUseSourceNoDataValue(b, False)
        if rule.pipeline is not None:
            settings = er.with_pipeline(settings, rule.pipeline)
        er.apply(layer, settings)
        self.set_view(layer)
        bar = self.iface.messageBar()
        bar.pushInfo(TITLE, self.tr("{name}: 設定「{label}」を適用").format(name=layer.name(), label=rule.label))
        if problem:
            bar.pushWarning(TITLE, problem)
        if self._is_target(layer):
            self.refresh()
        return rule

    def restore_layers(self, layers) -> None:
        """Puts the correction kept on layers (opened from a project) back."""
        for layer in list(layers):
            if er.restore_layer(layer):
                self.set_view(layer)
        if self.layer is not None or self.group is not None:
            self.refresh()

    def update_views(self) -> None:
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and er.enhance_renderer(layer) is not None:
                self.set_view(layer)

    def view_of(self, layer: QgsRasterLayer):
        """The canvas's view in the layer's CRS, or None (no transform)."""
        settings = self.canvas.mapSettings()
        try:
            ct = QgsCoordinateTransform(settings.destinationCrs(), layer.crs(), QgsProject.instance())
            return ct.transformBoundingBox(settings.visibleExtent())
        except Exception:  # noqa: BLE001 (no transform: DRA uses the area being drawn)
            return None

    def set_view(self, layer: QgsRasterLayer) -> None:
        """The canvas's view in the layer's CRS, for DRA."""
        er.set_view(layer, self.view_of(layer))

    def follow_layer(self, layer) -> None:
        """
        Shows the active raster layer's correction; with no active layer, that
        of the group chosen in the layer panel. Another layer chosen (a vector
        one: 作業メモ, 関心地域, a request's area) leaves the image worked on as
        it is (操作性点検 5, tsudo 2026-11-23: the dock went empty while the
        image was still on the map and in 観測記録).
        """
        if layer is not None and getattr(layer, "type", lambda: None)() != QgsMapLayerType.RasterLayer:
            if self._target_there():
                return
            layer = None
        self.show_target(layer, self.chosen_group() if layer is None else None)

    def _target_there(self) -> bool:
        """Whether what the dock works on (a layer, a group) is still in the project."""
        if self.group is not None:
            return not sip.isdeleted(self.group)
        layer = self.layer
        return layer is not None and not sip.isdeleted(layer) and QgsProject.instance().mapLayer(layer.id()) is layer

    def follow_group(self, group: QgsLayerTreeGroup) -> None:
        """Works on every raster under `group` (as when it is chosen in the layer panel)."""
        self.show_target(None, group)

    def refresh(self) -> None:
        """Shows the current target's correction again (after it changed elsewhere)."""
        if self.group is not None:
            if sip.isdeleted(self.group):
                self.follow_layer(self.iface.activeLayer())
            else:
                self.show_target(None, self.group)
        else:
            self.show_target(self.layer if self.layer is not None and not sip.isdeleted(self.layer) else None, None)

    def show_target(self, layer, group: Optional[QgsLayerTreeGroup], skip=()) -> None:
        """Shows the correction of `layer`, or of `group`'s top raster for all its rasters (but those in `skip`, ids)."""
        if self._timer.isActive():
            # A change still waiting goes to what it was made on.
            self.apply()
        self.group = group
        if group is not None:
            self.targets = [lay for lay in eg.group_rasters(group) if lay.id() not in skip]
            layer = self.targets[0] if self.targets else None
        else:
            self.targets = [layer] if layer is not None else []
        self.layer = layer
        self._same = group is None or eg.alike(self.targets)
        self._loading = True
        try:
            has = layer is not None
            self.layer_label.setText(self._target_text())
            self.enabled.setEnabled(has)
            r = er.enhance_renderer(layer) if has else None
            settings = r.settings if r else (er.default_settings(layer) if has else er.EnhanceSettings())
            self.enabled.setChecked(r is not None)
            self._fill_bands(layer, settings.bands)
            self.dra_locked.setChecked(settings.locked)
            self.opacity.setValue(round((layer.renderer().opacity() if has and layer.renderer() else 1.0) * 100))
            self.set_pipeline(settings.correction)
            self.share.setVisible(group is not None and has)
            self.share.setEnabled(self._same)
            self.share.setChecked(eg.share_wanted(group, self.targets) if group is not None else False)
            self.colorize_box.set_targets(self.targets, group is not None)
            self.colorize_box.show_layer(layer, settings.color if r else None)
            self.quick_box.show_layer(layer if group is None else None)
        finally:
            self._loading = False
        self._show_color_rows()
        self.histogram_later()

    def _target_text(self) -> str:
        layer, group = self.layer, self.group
        if group is None:
            return self.tr("対象: {name}").format(name=layer.name()) if layer is not None else self.tr("ラスターレイヤーの選択なし")
        if layer is None:
            return self.tr("グループ「{name}」にラスターなし").format(name=group.name())
        lines = [self.tr("対象: グループ「{name}」のラスター {n} 枚").format(name=group.name(), n=len(self.targets))]
        if len({self._look(t) for t in self.targets}) > 1:
            lines.append(self.tr("補正の違うレイヤーあり。変更すると全部この補正にそろう"))
        if not self._same:
            lines.append(self.tr("バンド構成の違う画像あり。バンド割当と範囲の共通化は使えない（バンド割当は各レイヤーのまま）"))
        return "\n".join(lines)

    def _look(self, layer) -> tuple:
        """What a change in the dock sets the same on every layer of a group: compared to tell when they differ."""
        r = er.enhance_renderer(layer)
        if r is None:
            return ("off",)
        s = r.settings
        return (json.dumps(s.pipeline, sort_keys=True), s.locked, round(r.opacity(), 2),
                tuple(s.bands) if self._same else len(s.bands), json.dumps(s.color, sort_keys=True))

    def _fill_bands(self, layer, bands) -> None:
        n = layer.bandCount() if layer is not None else 0
        shown = list(bands) * 3 if len(bands) == 1 else list(bands)
        for i, combo in enumerate(self.band_combos):
            combo.clear()
            for b in range(1, n + 1):
                name = layer.dataProvider().bandDescription(b) if layer is not None else ""
                combo.addItem(self.tr("バンド {n} ({name})").format(n=b, name=name) if name else self.tr("バンド {n}").format(n=b), b)
            combo.setCurrentIndex(max(0, min(n, shown[i] if i < len(shown) else 1) - 1))
        self.bands_box.setVisible(n >= 2 and self._same)

    # ---- the correction ----

    def bands(self) -> tuple:
        """The bands shown: three for R, G, B, or one (gray) when there is one band or the same band thrice."""
        if self.layer is None or self.layer.bandCount() < 2:
            return (1,)
        chosen = tuple(int(c.currentData() or 1) for c in self.band_combos)
        return chosen[:1] if len(set(chosen)) == 1 else chosen

    def pipeline(self) -> Pipeline:
        """The correction the dock sets now (as the viewer's panel builds it)."""
        p = Pipeline()
        if self.dra_on.isChecked():
            clip = self.dra_clip.value()
            p = p.add("autoStretch", {"method": self.dra_method.currentData(), "lowPercent": clip, "highPercent": clip,
                                      "linked": self.dra_linked.isChecked()})
        steps: dict = {}
        for row in self.rows:
            steps.setdefault(row.op, {})[row.param] = row.value()
        for op, params in steps.items():
            if not Pipeline().set(op, params).is_identity:
                p = p.set(op, params)
        curve = self.curve.step()
        if curve is not None:
            p = Pipeline([*p.ops, curve])
        for op in self._extra:
            p = Pipeline([*p.ops, op])
        return p

    def set_pipeline(self, p: Pipeline) -> None:
        """Moves the sliders and DRA to `p`; steps without a slider are kept as they are."""
        quiet, self._loading = self._loading, True
        try:
            for row in self.rows:
                step = p.get(row.op)
                row.set(step[row.param] if step and isinstance(step.get(row.param), (int, float)) else row.neutral)
            auto = p.get("autoStretch")
            self.dra_on.setChecked(auto is not None)
            if auto:
                self.dra_method.setCurrentIndex(max(0, self.dra_method.findData(auto["method"])))
                self.dra_clip.setValue(auto["lowPercent"])
                self.dra_linked.setChecked(auto["linked"])
            curve = p.get("curve")
            self.curve.set_step(curve)
            if curve is not None and not self.curve_box.isChecked():
                self.curve_box.setChecked(True)
            self._extra = [op for op in p.ops if op["op"] not in SLIDER_OPS and op["op"] not in ("autoStretch", "curve")]
            names = {"whiteBalance": "ホワイトバランス", "stretch": "ストレッチ"}
            self.extra_label.setText(self.tr("スライダー外の補正: {s}").format(s="・".join(names.get(o["op"], o["op"]) for o in self._extra))
                                     if self._extra else "")
            self.extra_label.setVisible(bool(self._extra))
        finally:
            self._loading = quiet

    def use_preset(self, name: str) -> None:
        if not name:
            return
        self.set_pipeline(PRESETS[name])
        self.preset.setCurrentIndex(0)
        if not self.enabled.isChecked():
            self.enabled.setChecked(True)
        self.changed()

    def reset(self) -> None:
        """Every slider back to neutral and DRA off; the bands stay."""
        self.set_pipeline(Pipeline())
        self.dra_clip.setValue(0.5)
        self.dra_method.setCurrentIndex(0)
        self.dra_linked.setChecked(False)
        self.changed()

    def changed(self, *_args) -> None:
        if self._loading:
            return
        self._show_color_rows()
        self._timer.start()

    def _show_color_rows(self) -> None:
        gray = len(self.bands()) == 1
        for row in self.rows:
            row.show(not (gray and OP_INFO[row.op]["colorOnly"]))
        self.dra_linked.setEnabled(not gray)
        self.curve.set_gray(gray)

    def settings(self) -> er.EnhanceSettings:
        current = er.enhance_renderer(self.layer) if self.layer is not None else None
        return er.EnhanceSettings(bands=self.bands(), pipeline=self.pipeline().to_json(), locked=self.dra_locked.isChecked(),
                                  no_data=current.settings.no_data if current else (), color=self.colorize_box.color(),
                                  share=current.settings.share if current else "")

    def settings_for(self, layer: QgsRasterLayer, share: str) -> er.EnhanceSettings:
        """
        The dock's correction for one raster of the group: its own bands when
        the group's are not alike, the colour scale when it is shown as the top
        one is (else its own; a layer of two dates keeps its colours).
        """
        current = er.enhance_renderer(layer)
        base = current.settings if current else er.default_settings(layer)
        bands = self.bands() if self._same else base.bands
        same_kind = (len(bands) == 1) == (len(self.bands()) == 1)
        color = self.colorize_box.color() if same_kind and paired_with(layer) is None else base.color
        return er.EnhanceSettings(bands=tuple(bands), pipeline=self.pipeline().to_json(), locked=self.dra_locked.isChecked(),
                                  no_data=base.no_data, color=color, share=share)

    def _share_toggled(self, on: bool) -> None:
        """A shared range needs the plugin's renderer: 補正 turns on with it."""
        if self._loading:
            return
        if on and not self.enabled.isChecked():
            self.enabled.setChecked(True)
        self.changed()

    def _colorize_changed(self) -> None:
        """A colour scale needs the plugin's renderer: 補正 turns on with it."""
        if self.colorize_box.color() is not None and not self.enabled.isChecked():
            self.enabled.setChecked(True)
        self.changed()

    def apply(self) -> None:
        """Draws the active layer (the group's rasters) with the dock's correction (or QGIS's own renderer when 補正 is off)."""
        self._timer.stop()
        if self.group is not None:
            self._apply_group()
            return
        layer = self.layer
        if layer is None:
            return
        if not self.enabled.isChecked():
            if er.enhance_renderer(layer) is not None:
                er.remove(layer)
            return
        renderer = er.apply(layer, self.settings())
        renderer.setOpacity(self.opacity.value() / 100)
        self.set_view(layer)
        layer.triggerRepaint()

    def _apply_group(self) -> None:
        targets = [layer for layer in self.targets if not sip.isdeleted(layer)]
        if not targets or sip.isdeleted(self.group):
            return
        if not self.enabled.isChecked():
            for layer in targets:
                if er.enhance_renderer(layer) is not None:
                    er.remove(layer)
            return
        share = eg.set_share(self.group, self.share.isChecked()) if self._same else ""
        opacity = self.opacity.value() / 100
        for layer in targets:
            renderer = er.apply(layer, self.settings_for(layer, share))
            renderer.setOpacity(opacity)
            self.set_view(layer)
            layer.triggerRepaint()
        self.layer_label.setText(self._target_text())

    # ---- histogram ----

    def _histogram_toggled(self, on: bool) -> None:
        for w in self._histogram_body:
            w.setVisible(on)
        if on:
            self.histogram_later()

    def histogram_later(self, *_args) -> None:
        """Takes the histogram again after a short pause (only while it is shown)."""
        if self.histogram_box.isChecked():
            self._histogram_timer.start()

    def take_histogram(self):
        """
        Starts a task reading the view coarsely (at most 512 px a side) for the
        histogram (a group's rasters shown in the view added up); the task, or None.
        """
        self._histogram_timer.stop()
        self._histogram_request += 1
        request = self._histogram_request
        items, corrected = self._histogram_items()
        if not items:
            self.histograms = None
            self.show_histogram()
            self.histogram_note.setText(self.tr("レイヤーを選ぶと、表示範囲のヒストグラムを表示") if self.layer is None
                                        else self.tr("表示範囲に表示中の画像なし") if self.group is not None
                                        else self.tr("表示範囲の取得に失敗"))
            return None
        layers = len(items) if self.group is not None else 0

        def done(result: dict) -> None:
            if request != self._histogram_request:
                return
            self.histograms = result
            self._histogram_corrected = corrected
            self._histogram_layers = layers
            self.show_histogram()

        def failed(message: str) -> None:
            if request == self._histogram_request and message != "取消":
                self.histogram_note.setText(self.tr("ヒストグラムの取得に失敗（{why}）").format(why=message))

        if len(items) == 1:
            return run_task(self.tr("ヒストグラム"), lambda _task: er.view_histograms(*items[0]), done, failed)
        return run_task(self.tr("ヒストグラム"), lambda _task: er.sum_histograms([er.view_histograms(*item) for item in items]),
                        done, failed)

    def _histogram_items(self) -> tuple:
        """What each histogram read needs ((provider clone, settings, state, view) a layer), and whether any is corrected."""
        if self.group is not None:
            root = QgsProject.instance().layerTreeRoot()
            layers = []
            for layer in self.targets:
                node = root.findLayer(layer.id()) if not sip.isdeleted(layer) else None
                if node is not None and node.isVisible():
                    layers.append(layer)
        else:
            layers = [self.layer] if self.layer is not None else []
        items, corrected = [], False
        for layer in layers:
            view = self.view_of(layer) if layer.isValid() else None
            if view is None or view.isEmpty() or (self.group is not None and not view.intersects(layer.extent())):
                continue
            r = er.enhance_renderer(layer)
            corrected = corrected or r is not None
            items.append((layer.dataProvider().clone(), r.settings if r else er.default_settings(layer),
                          r.state if r else er.EnhanceState(), view))
        return items, corrected

    def show_histogram(self, *_args) -> None:
        after = bool(self.histogram_after.currentData())
        channels = (self.histograms or {}).get("after" if after else "before", [])
        count = int(channels[0].sum()) if channels else 0
        if self.histograms is not None and count == 0:
            channels = []
        self.histogram_plot.set_channels(channels)
        self.histogram_table.set_channels(channels)
        before = (self.histograms or {}).get("before") or None
        self.curve.set_histogram(before)
        self.histogram_readout.setText("")
        if self.histograms is None:
            self.histogram_note.setText("")
        elif count == 0:
            self.histogram_note.setText(self.tr("表示範囲に画素なし"))
        else:
            which = self.tr("補正前（補正なし）") if after and not self._histogram_corrected else self.histogram_after.currentText()
            if self._histogram_layers:
                self.histogram_note.setText(self.tr("表示範囲の {n} 枚の {count} 画素・{which}").format(
                    n=self._histogram_layers, count=f"{count:,}", which=which))
            else:
                self.histogram_note.setText(self.tr("表示範囲の {count} 画素・{which}").format(count=f"{count:,}", which=which))

    # ---- for scripts ----

    def enhance(self, layer: QgsRasterLayer, correction=None, bands=None) -> None:
        """Corrects `layer` with `correction` (a Pipeline, its JSON or a preset name), showing `bands` (1-based)."""
        if isinstance(correction, str) and correction in PRESETS:
            correction = PRESETS[correction]
        elif correction is not None and not isinstance(correction, Pipeline):
            correction = Pipeline.from_json(correction)
        current = er.enhance_renderer(layer)
        base = current.settings if current else er.default_settings(layer)
        settings = er.EnhanceSettings(bands=tuple(bands) if bands else base.bands,
                                      pipeline=(correction or base.correction).to_json(), locked=base.locked, no_data=base.no_data,
                                      color=base.color, share=base.share)
        er.apply(layer, settings)
        self.set_view(layer)
        if self._is_target(layer):
            self.refresh()

    def colorize(self, layer: QgsRasterLayer, scale=None, blend: Optional[str] = None, reverse: bool = False, fade: bool = False,
                 classes: Optional[dict] = None) -> None:
        """
        Colours `layer` with a colour scale (a name such as "red" or "turbo", or
        stops [[position, [r, g, b]], ...]; None removes it), or with `classes`
        (the viewer's ColorClasses), and blends it with the layers under it
        ("normal", "screen", "difference", "lighten", "darken", "multiply").
        """
        current = er.enhance_renderer(layer)
        base = current.settings if current else er.default_settings(layer)
        color = None
        if scale is not None or classes:
            color = {"scale": scale or "turbo", **({"reverse": True} if reverse else {}),
                     **({"classes": classes} if classes else {"fade": True} if fade else {})}
        er.apply(layer, replace(base, color=color))
        if blend is not None:
            set_blend(layer, blend)
        self.set_view(layer)
        if self._is_target(layer):
            self.refresh()
