"""
着色 in the 画像補正 dock, as the viewer's coloring dialog: a one-band image
(SAR, panchromatic, one band of a multispectral image) in a colour scale,
red from black for example, laid over the layers under it in a blend mode.
Two dates in red and cyan, the later screened over the earlier, show white
where nothing changed and their own colour where only one is bright; one of
the two is always the dock's target image, the other is chosen. While the
two are laid over each other, their colouring is locked (on either image)
until 着色の解除, which takes it off both.

The image can also be shown by classes of its values: equal intervals,
quantiles, bounds typed in, or one colour per value for a classified image
(land cover codes). The classes are listed with their colours, names and
shares of the image, and go to the layer's legend.

Nothing is computed for the map: the renderer colours what it draws, after
the correction (classes take the values as read, without it).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsLayerTreeLayer, QgsProject, QgsRasterLayer
from qgis.gui import QgsColorButton
import json

from qgis.PyQt.QtCore import QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.enhance.color_classes import (
    auto_label, class_shares, color_classes, format_class_value, recolor, resize_classes, set_bound,
)
from ..qgis_io import enhance_renderer as er
from ..qgis_io.obs_images import observed_image
from ..qgis_io.tasks import run_task

#: The scales offered, in order: one colour from black, then ramps.
SCALES = [
    ("red", "赤"), ("green", "緑"), ("blue", "青"), ("cyan", "シアン（水色）"), ("magenta", "マゼンタ"), ("yellow", "黄"),
    ("orange", "オレンジ"), ("inferno", "熱（黒→赤→黄）"), ("viridis", "ビリディス（紫→緑→黄）"), ("turbo", "虹（青→赤）"),
    ("blueWhiteRed", "青→白→赤"),
]
#: Blend modes: the viewer's names (as CSS mix-blend-mode) and QGIS's composition modes.
BLENDS = [
    ("normal", "通常", QPainter.CompositionMode.CompositionMode_SourceOver),
    ("screen", "スクリーン（明るさを足す・時期の比較に）", QPainter.CompositionMode.CompositionMode_Screen),
    ("difference", "差（変化箇所のみ明るく）", QPainter.CompositionMode.CompositionMode_Difference),
    ("lighten", "比較（明）", QPainter.CompositionMode.CompositionMode_Lighten),
    ("darken", "比較（暗）", QPainter.CompositionMode.CompositionMode_Darken),
    ("multiply", "乗算", QPainter.CompositionMode.CompositionMode_Multiply),
]
MODES = [("", "連続（なめらかに）"), ("classes", "区分（値の範囲ごとに色分け）"), ("unique", "値ごとに色分け（分類画像）")]
METHODS = [("equal", "等間隔"), ("quantile", "分位数（面積が同じ）"), ("manual", "手動（境界の値を入力）")]
CUSTOM_COLOR = "#ff8800"
#: Layer property (kept in the project) of the two images laid over each other: {"role": "before"|"after", "with": other layer id}.
PAIR_PROPERTY = "satobs/pair"
#: The colour each role gets.
PAIR_SCALES = {"before": "red", "after": "cyan"}
#: Which of the two dates the target image is.
ORDERS = [("auto", "撮像日時の順"), ("before", "対象が前（赤）"), ("after", "対象が後（シアン）")]


def blend_of(layer: QgsRasterLayer) -> str:
    mode = layer.blendMode()
    return next((key for key, _label, m in BLENDS if m == mode), "normal")


def set_blend(layer: QgsRasterLayer, blend: str) -> None:
    layer.setBlendMode(next((m for key, _label, m in BLENDS if key == blend), BLENDS[0][2]))
    layer.triggerRepaint()


def one_color(color: QColor) -> list:
    """Black to `color`."""
    return [[0, [0, 0, 0]], [1, [color.red(), color.green(), color.blue()]]]


class GradientBar(QWidget):
    """The colour scale left (dark) to right (bright), over a checkerboard where it fades; for classes, a band per class."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.color: Optional[dict] = None
        self.setFixedHeight(16)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_color(self, color: Optional[dict]) -> None:
        self.color = color
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        p = QPainter(self)
        w, h = self.width(), self.height()
        for x in range(0, w, 8):
            for y in range(0, h, 8):
                p.fillRect(x, y, 8, 8, QColor(200, 200, 200) if (x // 8 + y // 8) % 2 else QColor(255, 255, 255))
        color = self.color or {"scale": "gray"}
        classes = color.get("classes")
        if classes and classes["classes"]:
            items = sorted(classes["classes"], key=lambda c: c["value"])
            step = w / len(items)
            for i, c in enumerate(items):
                p.fillRect(QRectF(i * step, 0, step, h), QColor(*er._rgba_of(c["color"])))
        else:
            palette = er.palette_of(color)
            for v in range(256):
                p.fillRect(QRectF(v * w / 256, 0, w / 256 + 1, h), QColor(*(int(x) for x in palette[v])))
        p.setPen(self.palette().mid().color())
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


class ColorizeBox(QGroupBox):
    """The colouring of the dock's layer; `changed` when the colour scale changes (the dock draws it)."""

    changed = pyqtSignal()
    #: Layers other than the dock's were coloured (two dates).
    colored = pyqtSignal()

    def __init__(self, say: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.setTitle(self.tr("着色（単バンド画像）"))
        self.say = say
        self.layer: Optional[QgsRasterLayer] = None
        #: The classes the form shows (None: a smooth scale).
        self.classes: Optional[dict] = None
        #: The value histogram of the layer, for the classes and their shares: (layer id, histogram).
        self.histogram: Optional[tuple] = None
        self._request = 0
        self._loading = False

        v = QVBoxLayout(self)
        form = QFormLayout()
        self.scale = QComboBox()
        self.scale.addItem(self.tr("なし（白黒）"), "")
        for key, label in SCALES:
            self.scale.addItem(self.tr(label), key)
        self.scale.addItem(self.tr("好きな色…"), "custom")
        self.scale.currentIndexChanged.connect(self._recolor)
        form.addRow(self.tr("カラースケール"), self.scale)
        self.custom = QgsColorButton()
        self.custom.setAllowOpacity(False)
        self.custom.setColor(QColor(CUSTOM_COLOR))
        self.custom.colorChanged.connect(self._recolor)
        self.custom_label = QLabel(self.tr("色"))
        form.addRow(self.custom_label, self.custom)
        self.mode = QComboBox()
        for key, label in MODES:
            self.mode.addItem(self.tr(label), key)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow(self.tr("表示方法"), self.mode)
        v.addLayout(form)
        self.preview = GradientBar()
        v.addWidget(self.preview)

        self.classes_box = QWidget()
        cv = QVBoxLayout(self.classes_box)
        cv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.method_label = QLabel(self.tr("分け方"))
        row.addWidget(self.method_label)
        self.method = QComboBox()
        for key, label in METHODS:
            self.method.addItem(self.tr(label), key)
        self.method.currentIndexChanged.connect(self.rebuild)
        row.addWidget(self.method, 1)
        self.count_label = QLabel(self.tr("クラス数"))
        row.addWidget(self.count_label)
        self.count = QSpinBox()
        self.count.setRange(2, 32)
        self.count.setValue(5)
        self.count.valueChanged.connect(self.rebuild)
        row.addWidget(self.count)
        cv.addLayout(row)
        self.legend = QTableWidget(0, 4)
        self.legend.setHorizontalHeaderLabels([self.tr("色"), self.tr("値"), self.tr("名前"), self.tr("面積")])
        header = self.legend.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.legend.verticalHeader().hide()
        self.legend.setMinimumHeight(120)
        cv.addWidget(self.legend)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        cv.addWidget(self.hint)
        v.addWidget(self.classes_box)

        self.reverse = QCheckBox(self.tr("反転（暗い所を明るい色に）"))
        self.reverse.toggled.connect(self._recolor)
        v.addWidget(self.reverse)
        self.fade = QCheckBox(self.tr("暗い所を透明に"))
        self.fade.toggled.connect(self._apply)
        v.addWidget(self.fade)
        form = QFormLayout()
        self.blend = QComboBox()
        for key, label, _mode in BLENDS:
            self.blend.addItem(self.tr(label), key)
        self.blend.currentIndexChanged.connect(self._blend_changed)
        form.addRow(self.tr("下のレイヤーとの重ね方"), self.blend)
        v.addLayout(form)
        self.scale_hint = QLabel(self.tr("選ぶとすぐ地図に反映。明るさ・コントラストなどの補正は着色前に適用。"))
        self.scale_hint.setWordWrap(True)
        v.addWidget(self.scale_hint)

        self.pair_box = QGroupBox(self.tr("2 時期の色分け重ね合わせ"))
        self.pair_box.setCheckable(True)
        self.pair_box.setChecked(False)
        pv = QVBoxLayout(self.pair_box)
        pf = QFormLayout()
        self.target_label = QLabel()
        self.target_label.setWordWrap(True)
        pf.addRow(self.tr("対象の画像"), self.target_label)
        self.other = QComboBox()
        self.other.setToolTip(self.tr("対象の画像と重ねる別の時期の画像"))
        self.other.currentIndexChanged.connect(self._show_order)
        pf.addRow(self.tr("比べる画像"), self.other)
        self.order = QComboBox()
        for key, label in ORDERS:
            self.order.addItem(self.tr(label), key)
        self.order.currentIndexChanged.connect(self._show_order)
        pf.addRow(self.tr("前後"), self.order)
        self.order_label = QLabel()
        self.order_label.setWordWrap(True)
        pf.addRow("", self.order_label)
        pv.addLayout(pf)
        pair_hint = QLabel(self.tr("前を赤、後をシアンとし、後をスクリーンで重ね合わせ。変化なしの所は白黒、"
                                   "後で明るくなった所はシアン、暗くなった所は赤。"))
        pair_hint.setWordWrap(True)
        pv.addWidget(pair_hint)
        self.pair_button = QPushButton(self.tr("赤とシアンで重ね合わせ"))
        self.pair_button.clicked.connect(self.pair)
        pv.addWidget(self.pair_button)
        self.pair_note = QLabel()
        self.pair_note.setWordWrap(True)
        pv.addWidget(self.pair_note)
        self._pair_body = [w for w in self.pair_box.findChildren(QWidget) if w.parent() is self.pair_box]
        self.pair_box.toggled.connect(self._pair_toggled)
        self._pair_toggled(False)
        v.addWidget(self.pair_box)

        self.lock_note = QLabel()
        self.lock_note.setWordWrap(True)
        self.lock_note.setStyleSheet("color: #b45309;")
        self.lock_note.hide()
        v.addWidget(self.lock_note)
        self.clear_button = QPushButton(self.tr("着色の解除"))
        self.clear_button.clicked.connect(self.clear)
        v.addWidget(self.clear_button)
        # Long choices must not widen the dock.
        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(6)
        self._show_parts()

    # ---- the form ----

    def show_layer(self, layer: Optional[QgsRasterLayer], color: Optional[dict]) -> None:
        """Shows the layer's colouring."""
        self.layer = layer
        self._request += 1
        self._loading = True
        try:
            self.setEnabled(layer is not None)
            scale = (color or {}).get("scale")
            if not color:
                self.scale.setCurrentIndex(0)
            elif isinstance(scale, str):
                self.scale.setCurrentIndex(max(0, self.scale.findData(scale)))
            else:
                self.scale.setCurrentIndex(self.scale.findData("custom"))
                last = (scale or [[1, [255, 136, 0]]])[-1][1]
                self.custom.setColor(QColor(*[int(x) for x in last[:3]]))
            self.reverse.setChecked(bool((color or {}).get("reverse")))
            self.fade.setChecked(bool((color or {}).get("fade")))
            self.blend.setCurrentIndex(max(0, self.blend.findData(blend_of(layer) if layer is not None else "normal")))
            classes = (color or {}).get("classes")
            self.classes = {**classes, "classes": [dict(c) for c in classes["classes"]]} if classes else None
            self.mode.setCurrentIndex(self.mode.findData("" if not classes else "unique" if classes["kind"] == "exact" else "classes"))
            if classes and classes["kind"] == "range":
                self.method.setCurrentIndex(max(0, self.method.findData(classes.get("method") if classes.get("method") in ("quantile", "manual")
                                                                        else "equal")))
                self.count.setValue(len(classes["classes"]))
        finally:
            self._loading = False
        self._fill_pair()
        self._show_parts()
        self.show_legend()
        self._show_lock()

    def scale_value(self):
        """The scale chosen (turbo when none is, for classes)."""
        key = self.scale.currentData()
        return one_color(self.custom.color()) if key == "custom" else (key or "turbo")

    def color(self) -> Optional[dict]:
        """The colour scale the form sets (the viewer's LayerColorScale), or None."""
        if not self.scale.currentData():
            return None
        out: dict = {"scale": self.scale_value()}
        if self.reverse.isChecked():
            out["reverse"] = True
        if self.mode.currentData() and self.classes:
            out["classes"] = self.classes
        elif self.fade.isChecked():
            out["fade"] = True
        return out

    def _show_parts(self) -> None:
        custom = self.scale.currentData() == "custom"
        self.custom.setVisible(custom)
        self.custom_label.setVisible(custom)
        mode = self.mode.currentData()
        self.classes_box.setVisible(bool(mode))
        self.fade.setVisible(not mode)
        self.scale_hint.setVisible(not mode)
        for w in (self.method, self.method_label, self.count, self.count_label):
            w.setVisible(mode == "classes")
        self.preview.set_color(self.color() or ({"scale": self.scale_value()} if mode else None))

    def _apply(self, *_args) -> None:
        if self._loading:
            return
        self._show_parts()
        self.changed.emit()

    def _blend_changed(self, *_args) -> None:
        if not self._loading and self.layer is not None:
            set_blend(self.layer, self.blend.currentData())

    def _recolor(self, *_args) -> None:
        """A new scale or direction: classes take their colours from it again."""
        if self._loading:
            return
        if self.mode.currentData() and not self.scale.currentData():
            self._loading = True
            self.mode.setCurrentIndex(0)
            self._loading = False
        if self.mode.currentData() and self.classes:
            self.classes = recolor(self.classes, self.scale_value(), self.reverse.isChecked())
        self._apply()
        if self.mode.currentData() and not self.classes:
            self.rebuild()
        else:
            self.show_legend()

    def _mode_changed(self, *_args) -> None:
        if self._loading:
            return
        if self.mode.currentData() and not self.scale.currentData():
            self._loading = True
            self.scale.setCurrentIndex(self.scale.findData("turbo"))
            self._loading = False
        self.rebuild()

    def clear(self) -> None:
        """No colour; while two dates are laid over each other, off both of them."""
        partner = paired_with(self.layer) if self.layer is not None else None
        if partner is not None:
            r = er.enhance_renderer(partner)
            if r is not None:
                er.apply(partner, replace(r.settings, color=None))
            set_blend(partner, "normal")
            partner.removeCustomProperty(PAIR_PROPERTY)
        if self.layer is not None:
            self.layer.removeCustomProperty(PAIR_PROPERTY)
        self._loading = True
        self.scale.setCurrentIndex(0)
        self.mode.setCurrentIndex(0)
        self.blend.setCurrentIndex(0)
        self._loading = False
        self.classes = None
        if self.layer is not None:
            set_blend(self.layer, "normal")
        self._apply()
        self.show_legend()
        self._show_lock()

    def locked(self) -> bool:
        """Whether the colouring is locked: the layer is one of two dates laid over each other."""
        return self.layer is not None and paired_with(self.layer) is not None

    def _show_lock(self) -> None:
        partner = paired_with(self.layer) if self.layer is not None else None
        on = partner is not None
        for w in (self.scale, self.custom, self.mode, self.classes_box, self.reverse, self.fade, self.blend, self.pair_box):
            w.setEnabled(not on)
        self.lock_note.setVisible(on)
        if on:
            self.lock_note.setText(self.tr("2 時期の重ね合わせ中（相手: {other}）。着色を変えるには「着色の解除」（両方の画像の着色を解除）")
                                   .format(other=partner.name()))

    # ---- classes ----

    def rebuild(self, *_args):
        """Makes the classes again from the image's values (a new mode, method or count), then colours the layer; the task, if one started."""
        if self._loading:
            return None
        self._request += 1
        request = self._request
        mode = self.mode.currentData()
        layer = self.layer
        if layer is None or not mode:
            self.classes = None
            self._apply()
            self.show_legend()
            return None
        count = self.count.value()
        scale, reverse = self.scale_value(), self.reverse.isChecked()
        if mode == "classes" and self.method.currentData() == "manual" and self.classes and self.classes["kind"] == "range":
            # By hand: the bounds stay; a new count adds or removes classes at the top.
            self.classes = recolor(resize_classes(self.classes, count), scale, reverse)
            self._apply()
            self.show_legend()
            return None
        method = "unique" if mode == "unique" else ("quantile" if self.method.currentData() == "quantile" else "equal")
        self._show_parts()

        def made(hist):
            if request != self._request:
                return
            classes = color_classes(hist, method=method, count=count, scale=scale, reverse=reverse) if hist else None
            if classes is None:
                self.hint.setText(self.tr("値の種類が多すぎ（64 種類まで）。「区分」で色分け") if mode == "unique" and hist
                                  else self.tr("画像の値の読み込みに失敗"))
                return
            self.classes = classes
            self._apply()
            self.show_legend()

        if self.histogram and self.histogram[0] == layer.id():
            made(self.histogram[1])
            return None
        self.hint.setText(self.tr("画像の値を読み込み中…"))
        return self._read_histogram(layer, made)

    def _read_histogram(self, layer: QgsRasterLayer, then: Callable):
        r = er.enhance_renderer(layer)
        settings = r.settings if r else er.default_settings(layer)
        provider = layer.dataProvider().clone()
        layer_id = layer.id()

        def done(hist):
            self.histogram = (layer_id, hist)
            then(hist)

        def failed(message: str):
            if message != "取消":
                self.hint.setText(self.tr("画像の値の読み込みに失敗（{why}）").format(why=message))

        return run_task(self.tr("画像の値の読み込み"), lambda _task: er.value_histogram(provider, settings), done, failed)

    def show_legend(self) -> None:
        """A row per class: its colour, bound or value, name and share of the image."""
        classes = self.classes if self.mode.currentData() else None
        self.legend.setRowCount(0)
        if not classes:
            self.hint.setText("")
            return
        order = sorted(range(len(classes["classes"])), key=lambda i: classes["classes"][i]["value"])
        values = [classes["classes"][i]["value"] for i in order]
        lo = classes.get("min") if classes.get("min") is not None else values[0]
        width = (values[-1] - lo) / len(values) if len(values) > 1 else 1
        hist = self.histogram[1] if self.histogram and self.layer is not None and self.histogram[0] == self.layer.id() else None
        shares = class_shares(hist, classes) if hist else None
        self.legend.setRowCount(len(order))
        for k, i in enumerate(order):
            c = classes["classes"][i]
            button = QgsColorButton()
            button.setFixedWidth(44)
            button.setShowMenu(False)
            button.setColor(QColor(*er._rgba_of(c["color"])))
            button.setToolTip(self.tr("クラス {n} の色").format(n=k + 1))
            button.colorChanged.connect(lambda q, i=i: self._set_class(i, color=[q.red(), q.green(), q.blue(), q.alpha()]))
            self.legend.setCellWidget(k, 0, button)
            if classes["kind"] == "exact":
                self.legend.setItem(k, 1, QTableWidgetItem(format_class_value(c["value"], width)))
            else:
                cell = QWidget()
                h = QHBoxLayout(cell)
                h.setContentsMargins(2, 0, 2, 0)
                h.addWidget(QLabel(f"{format_class_value(lo if k == 0 else values[k - 1], width)} – "))
                bound = QLineEdit(str(float(f"{c['value']:.12g}")).removesuffix(".0"))
                bound.setMinimumWidth(30)
                bound.setToolTip(self.tr("クラス {n} の上限").format(n=k + 1))
                bound.editingFinished.connect(lambda i=i, bound=bound: self._bound(i, bound.text()))
                h.addWidget(bound, 1)
                self.legend.setCellWidget(k, 1, cell)
            name = QLineEdit(c.get("label") or "")
            name.setMinimumWidth(30)
            name.setToolTip(self.tr("クラス {n} の名前").format(n=k + 1))
            name.editingFinished.connect(lambda i=i, name=name: self._name(i, name.text()))
            self.legend.setCellWidget(k, 2, name)
            share = "" if shares is None else f"{shares[i] * 100:.{1 if shares[i] < 0.1 else 0}f}%"
            item = QTableWidgetItem(share)
            item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            self.legend.setItem(k, 3, item)
        self.hint.setText(self.tr("選ぶとすぐ地図に反映。画像の値ごとに着色、名前の入力で凡例に表示。") if classes["kind"] == "exact"
                          else self.tr("色分けは画像の値で決定、明るさ・コントラストなどの補正は適用外。上限の書き換えで手動に切り替え。"))
        if hist is None and self.layer is not None:
            request = self._request

            def then(_hist):
                if request == self._request:
                    self.show_legend()

            self._read_histogram(self.layer, then)

    def _set_class(self, index: int, **change) -> None:
        if not self.classes:
            return
        self.classes = {**self.classes, "classes": [dict(c, **change) if j == index else c for j, c in enumerate(self.classes["classes"])]}
        self._apply()

    def _name(self, index: int, text: str) -> None:
        if self.classes:
            self._set_class(index, label=text.strip() or auto_label(self.classes, index))

    def _bound(self, index: int, text: str) -> None:
        try:
            value = float(text)
        except ValueError:
            self.show_legend()
            return
        if not self.classes or self.classes["classes"][index]["value"] == value:
            return
        self.classes = set_bound(self.classes, index, value)
        self._loading = True
        self.method.setCurrentIndex(self.method.findData("manual"))
        self._loading = False
        self._apply()
        self.show_legend()

    # ---- two dates ----

    def _pair_toggled(self, on: bool) -> None:
        for w in self._pair_body:
            w.setVisible(on)
        if on:
            self._fill_pair()

    def _fill_pair(self) -> None:
        """The other image: every raster layer but the target; the one-band image nearest in time to it (else the next in the layer list) unless one is already chosen."""
        target = self.layer
        self.target_label.setText(target.name() if target is not None else self.tr("ラスターレイヤーの選択なし"))
        layers = [lay for lay in raster_layers() if target is None or lay.id() != target.id()]
        chosen = self.other.currentData()
        self.other.blockSignals(True)
        self.other.clear()
        for layer in layers:
            self.other.addItem(layer.name(), layer.id())
        index = self.other.findData(chosen) if chosen else -1
        if index < 0 and layers:
            other = nearest_image(target, layers)
            index = self.other.findData(other.id()) if other is not None else 0
        self.other.setCurrentIndex(index)
        self.other.blockSignals(False)
        self.pair_box.setEnabled(target is not None and bool(layers))
        self._show_order()

    def pair_layers(self) -> tuple:
        """(before, after) for the target and the other image chosen; None in place of the missing ones."""
        target = self.layer
        other = QgsProject.instance().mapLayer(self.other.currentData() or "")
        if target is None or other is None or other.id() == target.id():
            return None, None
        order = self.order.currentData()
        if order == "auto":
            order = "before" if earlier_first(target, other) else "after"
        return (target, other) if order == "before" else (other, target)

    def _show_order(self, *_args) -> None:
        before, after = self.pair_layers()
        self.order_label.setText(self.tr("前（赤）: {before}\n後（シアン）: {after}").format(before=before.name(), after=after.name())
                                 if before is not None else "")

    def pair(self) -> bool:
        """The earlier image in red, the later in cyan screened over it, both shown; False when there is no target or other image."""
        project = QgsProject.instance()
        before, after = self.pair_layers()
        if before is None or after is None:
            self.pair_note.setText(self.tr("対象の画像と比べる画像を選択"))
            return False
        root = project.layerTreeRoot()
        # The later image goes right above the earlier one.
        node = root.findLayer(after.id())
        under = root.findLayer(before.id())
        if node is not None and under is not None:
            clone = node.clone()
            parent = under.parent()
            parent.insertChildNode(parent.children().index(under), clone)
            node.parent().removeChildNode(node)
        # A pair either image was in before ends: its partner loses the colour.
        for layer in (before, after):
            old = paired_with(layer)
            if old is not None and old.id() not in (before.id(), after.id()):
                r = er.enhance_renderer(old)
                if r is not None:
                    er.apply(old, replace(r.settings, color=None))
                set_blend(old, "normal")
                old.removeCustomProperty(PAIR_PROPERTY)
        for layer, other, role, blend in ((before, after, "before", "normal"), (after, before, "after", "screen")):
            scale = PAIR_SCALES[role]
            r = er.enhance_renderer(layer)
            settings = r.settings if r else er.default_settings(layer)
            r = er.apply(layer, replace(settings, color={"scale": scale}))
            r.setOpacity(1.0)
            set_blend(layer, blend)
            layer.setCustomProperty(PAIR_PROPERTY, json.dumps({"role": role, "with": other.id()}))
            tree = root.findLayer(layer.id())
            if isinstance(tree, QgsLayerTreeLayer):
                tree.setItemVisibilityChecked(True)
        self.pair_note.setText("")
        self.say(self.tr("{before} を赤、{after} をシアンで重ね合わせ").format(before=before.name(), after=after.name()))
        self.colored.emit()
        return True


def paired_with(layer: QgsRasterLayer) -> Optional[QgsRasterLayer]:
    """The other image `layer` is laid over (or under) as two dates, while both still have their pair colour; else None."""
    def mark(lay):
        try:
            m = json.loads(lay.customProperty(PAIR_PROPERTY) or "null")
        except (TypeError, ValueError):
            return None
        return m if isinstance(m, dict) and m.get("role") in PAIR_SCALES else None

    def colored(lay, role):
        r = er.enhance_renderer(lay)
        return r is not None and (r.settings.color or {}).get("scale") == PAIR_SCALES[role]

    m = mark(layer)
    if m is None:
        return None
    other = QgsProject.instance().mapLayer(str(m.get("with") or ""))
    n = mark(other) if isinstance(other, QgsRasterLayer) else None
    if n is None or n.get("with") != layer.id() or n["role"] == m["role"]:
        return None
    return other if colored(layer, m["role"]) and colored(other, n["role"]) else None


def earlier_first(a: QgsRasterLayer, b: QgsRasterLayer) -> bool:
    """Whether `a` is the earlier of the two: by their times when both have one, else the lower in the layer list."""
    ta, tb = observed_image(a).time, observed_image(b).time
    if ta is not None and tb is not None and ta != tb:
        return ta < tb
    ids = [lay.id() for lay in raster_layers()]
    if a.id() in ids and b.id() in ids:
        return ids.index(a.id()) > ids.index(b.id())
    return True


def nearest_image(target: Optional[QgsRasterLayer], layers: list) -> Optional[QgsRasterLayer]:
    """The image to compare `target` with: of the one-band images when it is one-band and there are some, the nearest in time when all
    have a time, else the next below it in the layer list (else the one right above)."""
    if not layers:
        return None
    if target is None:
        return layers[0]
    pool = [lay for lay in layers if lay.bandCount() == 1] if target.bandCount() == 1 else []
    pool = pool or layers
    t = observed_image(target).time
    times = {lay.id(): observed_image(lay).time for lay in pool}
    if t is not None and all(v is not None for v in times.values()):
        return min(pool, key=lambda lay: abs(times[lay.id()] - t))
    order = [lay.id() for lay in raster_layers()]
    if target.id() in order:
        at = order.index(target.id())
        below = [lay for lay in pool if lay.id() in order and order.index(lay.id()) > at]
        if below:
            return min(below, key=lambda lay: order.index(lay.id()))
        above = [lay for lay in pool if lay.id() in order]
        if above:
            return max(above, key=lambda lay: order.index(lay.id()))
    return pool[0]


def raster_layers() -> list:
    """The project's raster layers in the order of the layer list, top first."""
    root = QgsProject.instance().layerTreeRoot()
    return [node.layer() for node in root.findLayers() if isinstance(node.layer(), QgsRasterLayer) and node.layer().isValid()]
