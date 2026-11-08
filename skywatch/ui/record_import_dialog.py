"""
「観測記録の一括取込み」 (core/obs/record_import.py): a CSV file or a layer of
the project, which column is which item, a dry run (試行) that shows what each
row would become, then the import into the observation dock's chosen store.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsMapLayerProxyModel, QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QRadioButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.obs import record_import as ri
from ..qgis_io import record_import_io
from .docks import ask_no, plain_label

try:
    VECTOR_LAYERS = QgsMapLayerProxyModel.Filter.VectorLayer
except AttributeError:  # older bindings: the enum unscoped
    VECTOR_LAYERS = QgsMapLayerProxyModel.VectorLayer
CHANGE_LABELS = {"kind": "種別", "activity": "活動", "present": "有無", "confidence": "確度", "note": "備考", "length": "長さ",
                 "heading": "向き", "width": "幅", "lon": "経度", "lat": "緯度", "object_name": "モノ"}
#: Rows shown in the dry run's table (all are counted and imported).
SHOWN_ROWS = 2000


class RecordImportDialog(QDialog):
    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.setWindowTitle(self.tr("観測記録の一括取込み"))
        self.resize(820, 720)
        self.columns: list[str] = []
        self.rows: list[ri.SourceRow] = []
        self.csv_path = ""
        self.planned: list[ri.Planned] = []
        self.boxes: dict[str, QComboBox] = {}
        self._running = False

        main = QVBoxLayout(self)
        self.store_label = QLabel()
        self.store_label.setWordWrap(True)
        main.addWidget(self.store_label)

        source = QGroupBox(self.tr("取り込む元"))
        sl = QFormLayout(source)
        layer_row = QHBoxLayout()
        self.from_layer = QRadioButton(self.tr("レイヤー"))
        self.from_layer.setChecked(True)
        self.layer_select = QgsMapLayerComboBox()
        self.layer_select.setFilters(VECTOR_LAYERS)
        self.layer_select.setAllowEmptyLayer(True)
        self.layer_select.setLayer(None)
        self.layer_select.layerChanged.connect(lambda _l: self._source_changed(layer=True))
        self.selected_only = QCheckBox(self.tr("選択した地物のみ"))
        self.selected_only.toggled.connect(lambda _on: self._source_changed())
        layer_row.addWidget(self.from_layer)
        layer_row.addWidget(self.layer_select, 1)
        layer_row.addWidget(self.selected_only)
        sl.addRow(layer_row)
        csv_row = QHBoxLayout()
        self.from_csv = QRadioButton(self.tr("CSV ファイル"))
        self.csv_label = QLabel(self.tr("（未選択）"))
        self.csv_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        choose = QPushButton(self.tr("選ぶ…"))
        choose.clicked.connect(lambda: self.choose_csv())
        csv_row.addWidget(self.from_csv)
        csv_row.addWidget(self.csv_label, 1)
        csv_row.addWidget(choose)
        sl.addRow(csv_row)
        self.from_layer.toggled.connect(lambda _on: self._source_changed())
        self.source_note = plain_label(QLabel())
        self.source_note.setWordWrap(True)
        sl.addRow(self.source_note)
        main.addWidget(source)

        mapping = QGroupBox(self.tr("列の対応（名前から推測。標準の CSV・JSON の書き出しはそのまま読める）"))
        ml = QVBoxLayout(mapping)
        holder = QWidget()
        form = QFormLayout(holder)
        for key, label, _names in ri.TARGETS:
            box = QComboBox()
            box.currentIndexChanged.connect(lambda _i: self._invalidate())
            form.addRow(self.tr(label), box)
            self.boxes[key] = box
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        scroll.setMinimumHeight(180)
        ml.addWidget(scroll)
        self.update_existing = QCheckBox(self.tr("観測ID が同じ既存の記録を更新する（列の値で上書き。ほかの人が先に変えた記録は上書きしない）"))
        self.update_existing.toggled.connect(lambda _on: self._invalidate())
        ml.addWidget(self.update_existing)
        main.addWidget(mapping, 1)

        run_row = QHBoxLayout()
        self.try_button = QPushButton(self.tr("試行（保存しない）"))
        self.try_button.setToolTip(self.tr("各行がどうなるか（新規・更新・重複・エラー）を表示。まだ保存しない"))
        self.try_button.clicked.connect(lambda: self.try_run())
        self.import_button = QPushButton(self.tr("取り込む"))
        self.import_button.setToolTip(self.tr("試行の結果の新規・更新を保存先に保存（重複・エラーの行は飛ばす）"))
        self.import_button.clicked.connect(lambda: self.run_import())
        self.import_button.setEnabled(False)
        self.summary = plain_label(QLabel())
        self.summary.setWordWrap(True)
        run_row.addWidget(self.try_button)
        run_row.addWidget(self.import_button)
        run_row.addWidget(self.summary, 1)
        main.addLayout(run_row)
        self.errors_only = QCheckBox(self.tr("エラー・重複・注意のある行だけ表示"))
        self.errors_only.toggled.connect(lambda _on: self._render())
        main.addWidget(self.errors_only)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([self.tr("行"), self.tr("結果"), self.tr("モノ・種別"), self.tr("理由・変更"), self.tr("注意")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        main.addWidget(self.table, 2)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        main.addWidget(self.status)
        self.reset()

    # ---- the source ------------------------------------------------------------------------------

    def reset(self) -> None:
        book = self.dock.book
        self.store_label.setText(self.tr("取り込み先: {s}").format(s=book.label if book is not None else self.tr("（保存先なし）")))
        self._source_changed()

    def choose_csv(self, path: Optional[str] = None) -> bool:
        if path is None:
            path, _f = QFileDialog.getOpenFileName(self, self.tr("取り込む CSV"), "", self.tr("CSV (*.csv *.txt *.tsv);;すべて (*)"))
            if not path:
                return False
        self.csv_path = path
        self.csv_label.setText(path)
        self.from_csv.setChecked(True)
        self._source_changed()
        return True

    def set_layer(self, layer: Optional[QgsVectorLayer]) -> None:
        self.layer_select.setLayer(layer)
        self.from_layer.setChecked(True)
        self._source_changed()

    def _source_changed(self, layer: bool = False) -> None:
        if layer and self.layer_select.currentLayer() is not None:
            self.from_layer.blockSignals(True)
            self.from_layer.setChecked(True)
            self.from_layer.blockSignals(False)
        self.columns, self.rows = [], []
        note = ""
        try:
            if self.from_csv.isChecked():
                if self.csv_path:
                    self.columns, self.rows = ri.read_csv(self.csv_path)
                    note = self.tr("{n:,} 行（位置は経度・緯度の列）").format(n=len(self.rows))
                else:
                    note = self.tr("CSV を選ぶ")
            else:
                layer = self.layer_select.currentLayer()
                if isinstance(layer, QgsVectorLayer) and layer.isValid():
                    self.columns, self.rows = record_import_io.layer_rows(layer, self.selected_only.isChecked())
                    where = self.tr("位置は地物の点（線・面は内側の点）") if layer.isSpatial() else self.tr("位置は経度・緯度の列")
                    note = self.tr("{n:,} 件（{w}）").format(n=len(self.rows), w=where)
                else:
                    note = self.tr("レイヤーを選ぶ")
        except (OSError, ValueError) as error:
            note = self.tr("読めない: {m}").format(m=error)
        self.source_note.setText(note)
        guessed = ri.guess_mapping(self.columns)
        for key, box in self.boxes.items():
            box.blockSignals(True)
            box.clear()
            box.addItem(self.tr("（なし）"), "")
            for c in self.columns:
                box.addItem(c, c)
            i = box.findData(guessed.get(key, ""))
            box.setCurrentIndex(max(i, 0))
            box.blockSignals(False)
        self._invalidate()

    def mapping(self) -> dict[str, str]:
        return {k: box.currentData() for k, box in self.boxes.items() if box.currentData()}

    def set_mapping(self, key: str, column: str) -> None:
        box = self.boxes[key]
        box.setCurrentIndex(max(box.findData(column or ""), 0))

    # ---- dry run and import -----------------------------------------------------------------------

    def _invalidate(self) -> None:
        self.planned = []
        self.import_button.setEnabled(False)
        self.summary.setText("")
        self.table.setRowCount(0)

    def try_run(self) -> list[ri.Planned]:
        """What each row would become (nothing saved)."""
        book = self.dock.book
        if book is None:
            self.status.setText(self.tr("観測記録の保存先がない"))
            return []
        mapping = self.mapping()
        if not mapping.get("image_id") and not mapping.get("id"):
            self.status.setText(self.tr("画像ID の列を選ぶ"))
            return []
        records = list(book.records)
        times = {o.image_id: o.image_time for o in records if o.image_time is not None}
        names = {o.image_id: o.image_name for o in records if o.image_name}
        for image in book.images:
            if image.time is not None:
                times[image.id] = image.time
            names.setdefault(image.id, image.name)
        self.planned = ri.plan(self.rows, mapping, records, [c.label for c in book.confidences()], book.kept_taxonomy(),
                               self.update_existing.isChecked(), times, names)
        counts = ri.counts(self.planned)
        self.summary.setText(ri.summary(self.planned))
        self.import_button.setEnabled(bool(counts[ri.NEW] or counts[ri.UPDATE]) and not self._running)
        self.status.setText("")
        self._render()
        return self.planned

    def _render(self) -> None:
        rows = [p for p in self.planned if not self.errors_only.isChecked() or p.action in (ri.ERROR, ri.DUPLICATE) or p.warnings]
        shown = rows[:SHOWN_ROWS]
        self.table.setRowCount(len(shown))
        colours = {ri.ERROR: Qt.GlobalColor.red, ri.DUPLICATE: Qt.GlobalColor.darkYellow, ri.NEW: Qt.GlobalColor.darkGreen,
                   ri.UPDATE: Qt.GlobalColor.blue}
        for i, p in enumerate(shown):
            o = p.record
            what = f"{o.object_name}（{o.kind}）" if o is not None and o.kind else (o.object_name if o is not None else "")
            detail = p.reason
            if p.changes:
                detail = "、".join(f"{CHANGE_LABELS.get(k, k)}: {'' if a is None else a} → {'' if b is None else b}" for k, (a, b) in p.changes.items())
            cells = [str(p.number), p.label, what, detail, "、".join(p.warnings)]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if j == 1 and p.action in colours:
                    item.setForeground(colours[p.action])
                self.table.setItem(i, j, item)
        if len(rows) > SHOWN_ROWS:
            self.status.setText(self.tr("表示は先頭 {n:,} 行（取り込みはすべて）").format(n=SHOWN_ROWS))

    def run_import(self, confirm: bool = True) -> bool:
        """Saves the dry run's new records and changes (in a task for an Esri store)."""
        if not self.planned or self._running:
            return False
        counts = ri.counts(self.planned)
        n_new, n_update = counts[ri.NEW], counts[ri.UPDATE]
        if not (n_new or n_update):
            return False
        book = self.dock.book
        question = self.tr("{s} に新規 {a} 件・更新 {u} 件を保存する？（重複・エラー {x} 件は飛ばす）").format(
            s=book.label if book is not None else "", a=n_new, u=n_update, x=counts[ri.DUPLICATE] + counts[ri.ERROR] + counts[ri.EXISTING])
        if confirm and not ask_no(self, self.tr("観測記録の一括取込み"), question):
            return False
        planned = list(self.planned)
        self._running = True
        self.import_button.setEnabled(False)
        self.try_button.setEnabled(False)
        self.status.setText(self.tr("保存中…"))

        def after(outcome) -> None:
            self._finish(outcome.text() if outcome is not None else "")

        def failed(message: str) -> None:
            self._finish(self.tr("保存できない: {m}").format(m=message))

        result = self.dock.do(self.tr("観測記録の一括取込み"), lambda b: ri.apply(b, planned), after, watch=True, failed=failed)
        return result is not None or self._running

    def _finish(self, text: str) -> None:
        self._running = False
        self.try_button.setEnabled(True)
        self.planned = []
        self.import_button.setEnabled(False)
        self.status.setText(text)
        self.dock.render()
