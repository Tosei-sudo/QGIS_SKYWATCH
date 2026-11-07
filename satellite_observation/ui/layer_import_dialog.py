"""
「レイヤーから取り込み」: which layer, which attribute names the requests,
and whether only the selected features. Only the shapes and names come from
the layer; the rest (period, priority, satellites …) is filled in once in
the request form for all of them (tsudo, 2026-11-07).

「台帳と同じ属性も読む」 instead reads every item from the layer's attributes
(a layer written out by 「レイヤーへ書き出し」, taken back in).
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsMapLayerProxyModel, QgsVectorLayer
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QRadioButton, QVBoxLayout,
)

#: Vector layers with geometries (polygons, points, lines).
try:
    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.Filter.HasGeometry
except AttributeError:  # older bindings: the enum unscoped
    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.HasGeometry

_NAME_HINTS = ("name", "名称", "名前", "req_name", "地名", "title")


class LayerImportDialog(QDialog):
    """Chooses the layer and its name attribute for 「レイヤーから取り込み」."""

    def __init__(self, layer: Optional[QgsVectorLayer], point_km: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("レイヤーから取り込み"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(_GEOMETRY_LAYERS)
        form.addRow(self.tr("レイヤー"), self.layer)
        self.name_field = QgsFieldComboBox()
        self.name_field.setAllowEmptyFieldName(True)
        self.name_field.setToolTip(self.tr("依頼の名称にする属性（空なら「レイヤー名 連番」）"))
        form.addRow(self.tr("名称の属性"), self.name_field)
        self.selected_only = QCheckBox(self.tr("選択中の地物のみ"))
        form.addRow("", self.selected_only)
        self.point_km = QDoubleSpinBox()
        self.point_km.setRange(0.1, 500)
        self.point_km.setValue(point_km)
        self.point_km.setSuffix(" km")
        self.point_km.setToolTip(self.tr("点の地物の範囲（正方形）の一辺"))
        form.addRow(self.tr("点の範囲の一辺"), self.point_km)
        layout.addLayout(form)
        self.simple = QRadioButton(self.tr("範囲と名称だけ取り込み、ほかの項目は入力画面でまとめて指定"))
        self.simple.setChecked(True)
        self.full = QRadioButton(self.tr("台帳と同じ属性も読む（「レイヤーへ書き出し」したものを戻すとき。依頼ID が同じものは上書き）"))
        layout.addWidget(self.simple)
        layout.addWidget(self.full)
        self.count = QLabel()
        layout.addWidget(self.count)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("次へ"))
        box.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("閉じる"))
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        self.ok = box.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(box)
        self.layer.layerChanged.connect(self._layer_changed)
        self.selected_only.toggled.connect(lambda _on: self._update())
        self.full.toggled.connect(lambda _on: self._update())
        if layer is not None:
            self.layer.setLayer(layer)
        self._layer_changed(self.layer.currentLayer())

    def chosen_layer(self) -> Optional[QgsVectorLayer]:
        layer = self.layer.currentLayer()
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _layer_changed(self, layer) -> None:
        self.name_field.setLayer(layer)
        if isinstance(layer, QgsVectorLayer):
            names = [f.name() for f in layer.fields()]
            guess = next((n for hint in _NAME_HINTS for n in names if n.lower() == hint), "") or next(
                (n for n in names if "名" in n or "name" in n.lower()), "")
            self.name_field.setField(guess)
            self.selected_only.setChecked(layer.selectedFeatureCount() > 0)
        self._update()

    def _update(self) -> None:
        layer = self.chosen_layer()
        self.name_field.setEnabled(not self.full.isChecked())
        if layer is None:
            self.count.setText(self.tr("取り込むレイヤー（ポリゴン・点・線）を選択"))
            self.ok.setEnabled(False)
            return
        self.selected_only.setEnabled(layer.selectedFeatureCount() > 0)
        n = layer.selectedFeatureCount() if self.selected_only.isChecked() and layer.selectedFeatureCount() else layer.featureCount()
        self.count.setText(self.tr("地物 {n} 件").format(n=n))
        self.ok.setEnabled(n > 0)
