"""
「検出結果を DB へ転記…」, as the viewer's ai-transfer.ts: an AI layer's
features (as the model made them, corrected, or added by hand) written to an
Esri feature layer of the settings file's `detectionOutputs`, its attributes
mapped by role. New features are added, features changed since they were
sent are updated, the rest are left; what was sent is kept with the layer
(and the project), so the next transfer carries on from there.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.core import QgsFeatureRequest, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core.ai.transfer import TRANSFER_ROLES, DetectionOutput, Plan, Target, missing_fields, target_of
from ..qgis_io import ai_transfer_io
from ..qgis_io.tasks import run_task


def _tr(text: str) -> str:
    return QCoreApplication.translate("TransferDialog", text)


class TransferDialog(QDialog):
    def __init__(self, context, say: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.context = context
        self.say = say
        #: The target layers' descriptions, read once each (by URL).
        self.targets: dict[str, Target] = {}
        self.layers: list[QgsVectorLayer] = []
        self._asking: set[str] = set()
        self.running = False
        self.setWindowTitle(_tr("検出結果を DB へ転記"))
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source = QComboBox()
        self.source.currentIndexChanged.connect(lambda _: self.describe())
        form.addRow(_tr("転記するレイヤー"), self.source)
        self.target = QComboBox()
        self.target.currentIndexChanged.connect(lambda _: self.describe())
        form.addRow(_tr("転記先"), self.target)
        self.selected_only = QCheckBox(_tr("選択中の地物だけ"))
        self.selected_only.toggled.connect(lambda _: self.describe())
        form.addRow("", self.selected_only)
        layout.addLayout(form)
        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        self.plan_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.plan_label)
        self.mapping_box = QGroupBox(_tr("属性の対応"))
        self.mapping_box.setCheckable(True)
        self.mapping_box.setChecked(False)
        box = QVBoxLayout(self.mapping_box)
        self.mapping = QTreeWidget()
        self.mapping.setHeaderLabels([_tr("役割"), _tr("転記先の属性")])
        self.mapping.setRootIsDecorated(False)
        self.mapping.setVisible(False)
        self.mapping_box.toggled.connect(self.mapping.setVisible)
        box.addWidget(self.mapping)
        layout.addWidget(self.mapping_box, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton(_tr("閉じる"))
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        self.run_button = QPushButton(_tr("転記"))
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.run)
        buttons.addWidget(self.run_button)
        layout.addLayout(buttons)

    # Choices.

    def prepare(self, active=None) -> None:
        """The AI layers (the active one chosen) and the settings file's outputs."""
        self.layers = [lyr for lyr in QgsProject.instance().mapLayers().values() if ai_transfer_io.is_ai_layer(lyr)]
        for combo in (self.source, self.target):
            combo.blockSignals(True)
            combo.clear()
        for lyr in self.layers:
            self.source.addItem(lyr.name(), lyr.id())
        if active is not None and active in self.layers:
            self.source.setCurrentIndex(self.layers.index(active))
        for o in self.context.config.detection_outputs:
            self.target.addItem(o.label)
        for combo in (self.source, self.target):
            combo.blockSignals(False)
        layer = self.layer()
        self.selected_only.setChecked(bool(layer is not None and layer.selectedFeatureCount()))
        self.note.setText("")
        self.describe()

    def layer(self) -> Optional[QgsVectorLayer]:
        i = self.source.currentIndex()
        if not 0 <= i < len(self.layers):
            return None
        layer = self.layers[i]
        try:
            layer.id()
        except RuntimeError:  # removed from the project since
            return None
        return layer

    def output(self) -> Optional[DetectionOutput]:
        outputs = self.context.config.detection_outputs
        i = self.target.currentIndex()
        return outputs[i] if 0 <= i < len(outputs) else None

    def features(self, layer: QgsVectorLayer) -> list:
        if self.selected_only.isChecked():
            return list(layer.getSelectedFeatures())
        return list(layer.getFeatures(QgsFeatureRequest()))

    def _token(self, output: DetectionOutput) -> Optional[str]:
        return self.context.token_for(output.url, output.token)

    # What a transfer would do.

    def describe(self) -> None:
        """Shows what a transfer would do, and how the attributes are mapped (reading the target first, once)."""
        output, layer = self.output(), self.layer()
        count = layer.selectedFeatureCount() if layer is not None else 0
        self.selected_only.blockSignals(True)
        self.selected_only.setEnabled(count > 0)
        if count == 0:
            self.selected_only.setChecked(False)
        self.selected_only.setText(_tr("選択中の地物だけ（{n} 件）").format(n=f"{count:,}"))
        self.selected_only.blockSignals(False)
        self.run_button.setEnabled(False)
        self.mapping.clear()
        if output is None:
            self.plan_label.setText(_tr("転記先なし。設定ファイルの detectionOutputs に Esri フィーチャーレイヤーと属性の対応を記述"))
            return
        if layer is None:
            self.plan_label.setText(_tr("転記できるレイヤーなし（先に「物体検出（AI）」か「クリック抽出（AI）」で結果を作成）"))
            return
        target = self.targets.get(output.url)
        if target is None:
            self.plan_label.setText(_tr("転記先を確認中…"))
            self._read_target(output)
            return
        missing = set(missing_fields(output, target.fields))
        rows = [(TRANSFER_ROLES[role], name) for role, name in output.fields.items()]
        rows += [(_tr("固定値 {v}").format(v=repr(v) if not isinstance(v, str) else f"「{v}」"), name) for name, v in output.constants.items()]
        for role, name in rows:
            item = QTreeWidgetItem([role, _tr("{name}（転記先にない・書けない属性）").format(name=name) if name in missing else name])
            if name in missing:
                item.setForeground(1, Qt.GlobalColor.red)
            self.mapping.addTopLevelItem(item)
        self.mapping.resizeColumnToContents(0)
        if not target.can_create:
            self.plan_label.setText(_tr("{name} は追加不可のレイヤー（サービスで作成を許可）").format(name=target.name))
            return
        if target.geometry_type not in ("esriGeometryPolygon", "esriGeometryPoint"):
            self.plan_label.setText(_tr("{name} はポリゴンかポイントのレイヤーで指定").format(name=target.name))
            return
        todo = ai_transfer_io.plan(layer, self.features(layer), output, target, int(time.time() * 1000))
        self.plan_label.setText(todo.describe(target, len(missing)))
        self.run_button.setEnabled(not self.running and (bool(todo.adds) or (bool(todo.updates) and target.can_update)))

    def _read_target(self, output: DetectionOutput) -> None:
        if output.url in self._asking:
            return
        self._asking.add(output.url)
        token = self._token(output)

        def work(task):
            return target_of(output.url, self.context.esri().layer(output.url, token, refresh=True).raw)

        def done(target: Target) -> None:
            self._asking.discard(output.url)
            self.targets[output.url] = target
            self.describe()

        def failed(message: str) -> None:
            self._asking.discard(output.url)
            if self.output() is output:
                self.plan_label.setText(_tr("転記先の読み込みに失敗: {e}").format(e=message))

        run_task(_tr("転記先の確認"), work, done, failed)

    # Sending.

    def run(self) -> None:
        """Sends the new and changed features (in a task), then remembers what was sent on the layer."""
        output, layer = self.output(), self.layer()
        target = self.targets.get(output.url) if output is not None else None
        if output is None or layer is None or target is None or self.running:
            return
        todo: Plan = ai_transfer_io.plan(layer, self.features(layer), output, target, int(time.time() * 1000))
        token = self._token(output)
        self.running = True
        self.run_button.setEnabled(False)
        self.note.setText(_tr("転記中…"))

        def work(task):
            total = max(len(todo.adds), len(todo.updates))
            return ai_transfer_io.send(self.context.esri(), output, token, target, todo,
                                       lambda done, n: task.setProgress(100 * done / max(n, 1)) if total else None)

        def done(result) -> None:
            self.running = False
            sent, added, updated, failures = result
            try:
                ai_transfer_io.mark_sent(layer, output.url, sent)
            except RuntimeError:  # the layer was removed meanwhile
                pass
            self._finish(output, added, updated, failures)

        def failed(message: str) -> None:
            self.running = False
            self._finish(output, 0, 0, [message])

        run_task(_tr("検出結果の転記"), work, done, failed)

    def _finish(self, output: DetectionOutput, added: int, updated: int, failures: list[str]) -> None:
        text = _tr("{label} へ {a} 件を追加、{u} 件を更新").format(label=output.label, a=added, u=updated)
        if failures:
            unique = list(dict.fromkeys(failures))
            text += _tr("（失敗 {n} 件: {why}）").format(n=len(failures), why="、".join(unique[:3]))
        self.say(text)
        self.note.setText(text)
        self.describe()
