"""
「分類体系の取り込み」 (core/obs/taxonomy_import.py): a table (a CSV or Excel
file, or a layer of the project) with the levels in columns from 大分類 on,
each column's use chosen over a preview (guessed from the names), 追加・更新 or
置き換え, a dry run (試行) that shows the classification after the import and
what happens to each class, then the import into the classification of the
observation dock's chosen store (the classification as it was kept first).
"""
from __future__ import annotations

import os
from typing import Optional

from qgis.core import QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QRadioButton,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core.obs.api import kind_of
from ..core.obs.api import taxonomy_import as ti
from ..qgis_io import taxonomy_import_io as tio
from .docks import ask_no, plain_label
from .fit import fit_text
from .record_import_dialog import VECTOR_LAYERS

#: Rows of the table shown under the columns' uses (all are read).
PREVIEW_ROWS = 50
#: Each result's colour (readable on light and dark backgrounds).
COLOURS = {ti.NEW: "#15803d", ti.CHANGED: "#b45309", ti.REMOVED: "#b91c1c", ti.SAME: "#64748b", ti.KEPT: "#64748b"}
NOTE_COLOUR = "#b45309"


class TaxonomyImportDialog(QDialog):
    def __init__(self, dock, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.dock = dock
        self.setWindowTitle(self.tr("分類体系の取り込み"))
        self.resize(900, 900)
        self.columns: list[str] = []
        self.rows: list = []
        self.source_name = ""
        self.file_path = ""
        self.uses: list[ti.Use] = []
        self.choices: list[ti.Use] = []
        self.combos: list[QComboBox] = []
        #: The dry run's plan and the request it was made from (取り込む saves that request, if nothing changed meanwhile).
        self.plan: Optional[ti.ImportPlan] = None
        self.request: Optional[ti.ImportRequest] = None
        self._running = False

        main = QVBoxLayout(self)
        self.store_label = plain_label(QLabel())
        self.store_label.setWordWrap(True)
        main.addWidget(self.store_label)

        source = QGroupBox(self.tr("取り込む元"))
        sl = QVBoxLayout(source)
        layer_row = QHBoxLayout()
        self.from_layer = QRadioButton(self.tr("レイヤー（プロジェクトの表）"))
        self.layer_select = QgsMapLayerComboBox()
        self.layer_select.setFilters(VECTOR_LAYERS)
        self.layer_select.setAllowEmptyLayer(True)
        self.layer_select.setLayer(None)
        self.layer_select.layerChanged.connect(lambda _l: self._source_changed(layer=True))
        self.selected_only = QCheckBox(self.tr("選択した地物のみ"))
        self.selected_only.toggled.connect(lambda _on: self.from_layer.isChecked() and self._source_changed())
        layer_row.addWidget(self.from_layer)
        layer_row.addWidget(self.layer_select, 1)
        layer_row.addWidget(self.selected_only)
        sl.addLayout(layer_row)
        file_row = QHBoxLayout()
        self.from_file = QRadioButton(self.tr("ファイル（CSV・Excel）"))
        self.from_file.setChecked(True)
        self.file_label = plain_label(QLabel(self.tr("（未選択）")))
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        choose = QPushButton(self.tr("選ぶ…"))
        choose.setAutoDefault(False)
        choose.clicked.connect(lambda: self.choose_file())
        file_row.addWidget(self.from_file)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(choose)
        sl.addLayout(file_row)
        sheet_row = QHBoxLayout()
        sheet_row.addSpacing(24)
        self.sheet_title = QLabel(self.tr("シート"))
        self.sheet = QComboBox()
        self.sheet.currentIndexChanged.connect(lambda _i: self._source_changed())
        sheet_row.addWidget(self.sheet_title)
        sheet_row.addWidget(self.sheet)
        sheet_row.addSpacing(16)
        self.source_note = plain_label(QLabel())
        self.source_note.setWordWrap(True)
        sheet_row.addWidget(self.source_note, 1)
        sl.addLayout(sheet_row)
        self.from_layer.toggled.connect(lambda _on: self._source_changed())
        main.addWidget(source)

        columns = QGroupBox(self.tr("列の使い方（列名から推測。違うときは上の欄で選び直す）"))
        cl = QVBoxLayout(columns)
        self.preview = QTableWidget(0, 0)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.preview.horizontalHeader().setStretchLastSection(True)
        row = self.preview.fontMetrics().height() + 8
        self.preview.verticalHeader().setDefaultSectionSize(row)
        # The names' row, the uses' row and about six rows of the table.
        self.preview.setMinimumHeight(self.preview.horizontalHeader().sizeHint().height() + 2 * row + 6 * row + 6)
        cl.addWidget(self.preview, 1)
        self.uses_note = plain_label(QLabel())
        self.uses_note.setWordWrap(True)
        self.uses_note.setStyleSheet(f"color: {COLOURS[ti.REMOVED]};")
        cl.addWidget(self.uses_note)
        self.fill_down = QCheckBox(self.tr("左側の空欄は、上の行と同じ分類とみなす（Excel の結合セルや、同じ名前を省いた書き方）"))
        self.fill_down.setChecked(True)
        self.fill_down.toggled.connect(lambda _on: self._invalidate())
        cl.addWidget(self.fill_down)
        self.level_names = QCheckBox()
        self.level_names.setChecked(True)
        self.level_names.toggled.connect(lambda _on: self._invalidate())
        cl.addWidget(self.level_names)
        main.addWidget(columns, 1)

        how = QGroupBox(self.tr("取り込み方"))
        hl = QVBoxLayout(how)
        self.add_mode = QRadioButton(self.tr("追加・更新: 表の分類を加え、値のある欄だけ書き換える。表にない分類はそのまま残す"))
        self.add_mode.setChecked(True)
        self.replace_mode = QRadioButton(self.tr("置き換え: 表のとおりに変更。表にない分類は削除（記録のある分類は残す）"))
        self.add_mode.toggled.connect(lambda _on: self._invalidate())
        hl.addWidget(self.add_mode)
        hl.addWidget(self.replace_mode)
        main.addWidget(how)

        run_row = QHBoxLayout()
        self.try_button = QPushButton(self.tr("試行（保存しない）"))
        self.try_button.setToolTip(self.tr("取り込んだあとの分類体系と、各分類がどうなるか（新規・変更・削除）を表示。まだ保存しない"))
        self.try_button.setAutoDefault(False)
        self.try_button.clicked.connect(lambda: self.try_run())
        self.import_button = QPushButton(self.tr("取り込む"))
        self.import_button.setToolTip(self.tr("試行の結果を保存（取り込む前の分類体系は控えの JSON に残す）"))
        self.import_button.setAutoDefault(False)
        self.import_button.clicked.connect(lambda: self.run_import())
        self.import_button.setEnabled(False)
        self.summary = plain_label(QLabel())
        self.summary.setWordWrap(True)
        run_row.addWidget(self.try_button)
        run_row.addWidget(self.import_button)
        run_row.addWidget(self.summary, 1)
        main.addLayout(run_row)

        results = QGroupBox(self.tr("試行の結果（取り込んだあとの分類体系）"))
        rl = QVBoxLayout(results)
        self.changed_only = QCheckBox(self.tr("新規・変更・削除・注意のある分類だけ表示"))
        self.changed_only.toggled.connect(lambda _on: self._render())
        rl.addWidget(self.changed_only)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([self.tr("分類"), self.tr("結果"), self.tr("内容")])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        rl.addWidget(self.tree, 2)
        self.errors_title = plain_label(QLabel(self.tr("取り込まない行")))
        self.errors_title.setStyleSheet(f"color: {COLOURS[ti.REMOVED]}; font-weight: bold;")
        rl.addWidget(self.errors_title)
        self.errors = QTableWidget(0, 2)
        self.errors.setHorizontalHeaderLabels([self.tr("行"), self.tr("理由")])
        self.errors.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.errors.verticalHeader().hide()
        self.errors.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.errors.horizontalHeader().setStretchLastSection(True)
        rl.addWidget(self.errors)
        main.addWidget(results, 2)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        main.addWidget(self.status)
        self.reset()

    # ---- the source ------------------------------------------------------------------------------

    def reset(self) -> None:
        book = self.dock.book
        if book is None:
            self.store_label.setText(self.tr("取り込み先: （保存先なし）"))
        else:
            self.store_label.setText(self.tr("取り込み先: {s}（いま {n} 分類）").format(s=book.taxonomy_store.label, n=len(book.taxonomy())))
        self._source_changed()

    def choose_file(self, path: Optional[str] = None) -> bool:
        if path is None:
            path, _f = QFileDialog.getOpenFileName(self, self.tr("取り込む表"), os.path.dirname(self.file_path), self.tr(tio.FILE_FILTER))
            if not path:
                return False
        self.file_path = path
        self.file_label.setText(path)
        self.sheet.blockSignals(True)
        self.sheet.clear()
        if tio.is_sheet_file(path):
            try:
                self.sheet.addItems(tio.sheets(path))
            except (OSError, ValueError, RuntimeError):
                pass  # told when it is read
        self.sheet.blockSignals(False)
        self.from_file.setChecked(True)
        self._source_changed()
        return True

    def set_layer(self, layer: Optional[QgsVectorLayer]) -> None:
        self.layer_select.setLayer(layer)
        self.from_layer.setChecked(True)
        self._source_changed()

    def _source_changed(self, layer: bool = False) -> None:
        if layer and self.layer_select.currentLayer() is not None and not self.from_layer.isChecked():
            self.from_layer.blockSignals(True)
            self.from_layer.setChecked(True)
            self.from_layer.blockSignals(False)
        sheets = self.from_file.isChecked() and tio.is_sheet_file(self.file_path) and self.sheet.count() > 0
        self.sheet_title.setVisible(bool(sheets))
        self.sheet.setVisible(bool(sheets))
        self.columns, self.rows, self.source_name = [], [], ""
        note = ""
        try:
            if self.from_file.isChecked():
                if self.file_path:
                    self.columns, self.rows = tio.read_file(self.file_path, self.sheet.currentText() if sheets else None)
                    self.source_name = os.path.basename(self.file_path) + (f"（{self.sheet.currentText()}）" if sheets else "")
                    note = self.tr("{r:,} 行・{c} 列を読み込み（1 行目を列名とみなす）").format(r=len(self.rows), c=len(self.columns))
                else:
                    note = self.tr("ファイルを選ぶ（CSV は UTF-8 か Shift_JIS、Excel は 1 行目が列名）")
            else:
                layer = self.layer_select.currentLayer()
                if isinstance(layer, QgsVectorLayer) and layer.isValid():
                    self.columns, self.rows = tio.layer_table(layer, self.selected_only.isChecked())
                    self.source_name = layer.name()
                    note = self.tr("{r:,} 件・{c} 列を読み込み").format(r=len(self.rows), c=len(self.columns))
                else:
                    note = self.tr("レイヤーを選ぶ（Esri や Oracle の表、CSV を読み込んだレイヤーも）")
        except (OSError, ValueError, RuntimeError) as error:
            self.columns, self.rows = [], []
            note = self.tr("読めない: {m}").format(m=error)
        self.source_note.setText(note)
        book = self.dock.book
        taxonomy, fields = (book.taxonomy(), book.spec_fields) if book is not None else ([], [])
        self.uses = ti.guess_uses(self.columns, taxonomy, fields)
        self.choices = ti.use_choices(len(self.columns), fields)
        for u in self.uses:
            if u not in self.choices:
                self.choices.insert(len(self.choices) - 1, u)
        self._fill_preview()
        self._uses_changed()

    def _fill_preview(self) -> None:
        self.preview.clear()
        self.combos = []
        shown = self.rows[:PREVIEW_ROWS]
        self.preview.setColumnCount(len(self.columns))
        self.preview.setRowCount(1 + len(shown) if self.columns else 0)
        self.preview.setHorizontalHeaderLabels(self.columns)
        self.preview.setVerticalHeaderLabels([self.tr("使い方"), *(str(r.number) for r in shown)])
        for c, column in enumerate(self.columns):
            box = QComboBox()
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(7)
            for u in self.choices:
                box.addItem(self.tr(u.label))
            box.setCurrentIndex(self.choices.index(self.uses[c]))
            box.currentIndexChanged.connect(lambda i, c=c: self.set_use(c, self.choices[i]))
            self.preview.setCellWidget(0, c, box)
            self.combos.append(box)
            for r, row in enumerate(shown, start=1):
                self.preview.setItem(r, c, QTableWidgetItem(ti.cell_text(row.values.get(column))))
        if self.columns:
            self.preview.setRowHeight(0, max(self.combos[0].sizeHint().height() + 4, self.preview.verticalHeader().defaultSectionSize()))
        self.preview.resizeColumnsToContents()
        for c in range(len(self.columns)):
            self.preview.setColumnWidth(c, min(max(self.preview.columnWidth(c), self.combos[c].sizeHint().width()), 260))

    # ---- the columns' uses -----------------------------------------------------------------------

    def set_use(self, column: int, use: ti.Use) -> None:
        """A column's use (as chosen in its box)."""
        if not 0 <= column < len(self.uses):
            return
        self.uses[column] = use
        box = self.combos[column]
        if use in self.choices and box.currentIndex() != self.choices.index(use):
            box.blockSignals(True)
            box.setCurrentIndex(self.choices.index(use))
            box.blockSignals(False)
        self._uses_changed()

    def _uses_changed(self) -> None:
        problem = ti.check_uses(self.uses) if self.columns else None
        self.uses_note.setText(self.tr(problem) if problem else "")
        self.uses_note.setVisible(bool(problem))
        names = ti.level_names_of(self.columns, self.uses)
        if ti.Use(ti.LEVEL_NAMES) in self.uses:
            self.level_names.setText(self.tr("列名を大分類の階層名に設定（「階層名」の列があるので、その列の値を使う）"))
            self.level_names.setEnabled(False)
        elif names and any(ti.generic_level_name(n) for n in names):
            self.level_names.setText(self.tr("列名を大分類の階層名に設定（列名が「分類1」などの番号だけなので使わない）"))
            self.level_names.setEnabled(False)
        else:
            shown = " › ".join(names) if names else self.tr("階層の列の名前")
            self.level_names.setText(self.tr("列名を、階層名がまだない大分類の階層名に設定（{n}）").format(n=shown))
            self.level_names.setEnabled(bool(names))
        self._invalidate()

    def request_now(self) -> Optional[ti.ImportRequest]:
        if not self.columns or ti.check_uses(self.uses):
            return None
        return ti.ImportRequest(list(self.columns), list(self.rows), list(self.uses), ti.REPLACE if self.replace_mode.isChecked() else ti.ADD,
                                self.fill_down.isChecked(), self.level_names.isChecked() and self.level_names.isEnabled(), self.source_name)

    # ---- dry run and import -----------------------------------------------------------------------

    def _invalidate(self) -> None:
        self.plan = None
        self.request = None
        self.import_button.setEnabled(False)
        self.try_button.setEnabled(not self._running and self.request_now() is not None)
        self.summary.setText("")
        self.tree.clear()
        self.errors.setRowCount(0)
        self.errors_title.hide()
        self.errors.hide()
        if not self._running:
            self.status.setText("")

    def try_run(self) -> bool:
        """What the import would do (the classification read again first; nothing saved). False when it could not start."""
        book = self.dock.book
        if book is None:
            self.status.setText(self.tr("観測記録の保存先がない"))
            return False
        request = self.request_now()
        if request is None:
            self.status.setText(self.tr("取り込む表と、階層の列を選ぶ"))
            return False
        if self._running:
            return False
        if not self.dock.records_ready(self.try_run):
            # Not read yet (no image chosen in 「観測記録」): tried once they are (the classes records use are kept).
            self.status.setText(self.tr("観測記録を取得中…（取得後に試行）"))
            return True
        self._running = True
        self.try_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.status.setText(self.tr("分類体系を読み直して試行中…"))

        def work(b):
            try:
                return b.plan_taxonomy_import(request)
            except ValueError as error:
                return str(error)

        def after(plan) -> None:
            self._running = False
            if isinstance(plan, str):
                self._invalidate()
                self.status.setText(plan)
                return
            self._show(plan, request)

        def failed(message: str) -> None:
            self._running = False
            self._invalidate()
            self.status.setText(self.tr("試行できない: {m}").format(m=message))

        self.dock.do(self.tr("分類体系の取り込みの試行"), work, after, failed=failed)
        return True

    def _show(self, plan: ti.ImportPlan, request: ti.ImportRequest) -> None:
        self.plan, self.request = plan, request
        self.summary.setText(plan.summary())
        self.summary.setStyleSheet("font-weight: bold;")
        self.try_button.setEnabled(True)
        self.import_button.setEnabled(plan.changed)
        if plan.changed:
            self.status.setText(self.tr("試行では未保存。「取り込む」で保存（取り込む前の分類体系は、自動で控えの JSON に保存）"))
        else:
            self.status.setText(self.tr("取り込む変更なし（分類体系は表のとおり）"))
        self._render()

    def _render(self) -> None:
        self.tree.clear()
        plan = self.plan
        if plan is None:
            return
        classes = plan.classes
        if self.changed_only.isChecked():
            wanted = set()
            for c in classes:
                if c.status in (ti.NEW, ti.CHANGED, ti.REMOVED, ti.KEPT) or c.notes:
                    wanted.update(kind_of(c.path[:i]) for i in range(1, len(c.path) + 1))
            classes = [c for c in classes if c.kind in wanted]
        items: dict[str, QTreeWidgetItem] = {}
        for c in classes:
            item = QTreeWidgetItem([c.path[-1], self.tr(ti.STATUS_LABELS[c.status]), c.text()])
            item.setForeground(1, QBrush(QColor(COLOURS[c.status])))
            if c.notes:
                item.setForeground(2, QBrush(QColor(NOTE_COLOUR)))
            if c.status == ti.REMOVED:
                font = item.font(0)
                font.setStrikeOut(True)
                item.setFont(0, font)
                item.setForeground(0, QBrush(QColor(COLOURS[ti.REMOVED])))
            item.setToolTip(2, c.text())
            parent = items.get(kind_of(c.path[:-1]))
            if parent is not None:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            items[c.kind] = item
        self.tree.expandAll()
        self.errors.setRowCount(len(plan.errors))
        for i, (number, reason) in enumerate(plan.errors):
            self.errors.setItem(i, 0, QTableWidgetItem(self.tr("行 {n}").format(n=number)))
            why = QTableWidgetItem(reason)
            why.setForeground(QBrush(QColor(COLOURS[ti.REMOVED])))
            self.errors.setItem(i, 1, why)
        self.errors_title.setText(self.tr("取り込まない行（{n}）").format(n=len(plan.errors)))
        self.errors_title.setVisible(bool(plan.errors))
        self.errors.setVisible(bool(plan.errors))
        # As tall as its rows (four at most; the rest scrolled), the tree taking the room.
        rows = sum(self.errors.rowHeight(i) for i in range(min(len(plan.errors), 4)))
        self.errors.setFixedHeight(self.errors.horizontalHeader().sizeHint().height() + rows + 2 * self.errors.frameWidth() + 2)

    def run_import(self, confirm: bool = True) -> bool:
        """Saves what the dry run showed (planned again; nothing saved when the classification changed meanwhile)."""
        plan, request, book = self.plan, self.request, self.dock.book
        if plan is None or request is None or not plan.changed or self._running or book is None:
            return False
        removed = plan.removed()
        if confirm and removed:
            names = "・".join(c.kind for c in removed[:5]) + (self.tr(" ほか {n} 件").format(n=len(removed) - 5) if len(removed) > 5 else "")
            question = self.tr("表にない分類 {n} 件（{k}）を削除して取り込み。"
                               "記録のある分類は残り、取り込む前の分類体系は控えの JSON に保存").format(n=len(removed), k=names)
            if not ask_no(self, self.tr("分類体系の取り込み（置き換え）"), question, yes=self.tr("取り込み")):
                return False
        tried = plan.signature()
        self._running = True
        self.try_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.status.setText(self.tr("取り込み中…"))

        def after(saved) -> None:
            self._running = False
            message = self.dock.book.status if self.dock.book is not None else ""
            if saved is None:
                self._invalidate()
                self.status.setText(message)
                return
            self.plan = None
            self.try_button.setEnabled(True)
            self.summary.setText(self.tr("取り込み済み: {s}").format(s=saved.summary()))
            self.status.setText(message)

        def failed(message: str) -> None:
            self._running = False
            self._invalidate()
            self.status.setText(self.tr("取り込めない: {m}").format(m=message))

        self.dock.do(self.tr("分類体系の取り込み"), lambda b: b.import_taxonomy(request, tried, tio.write_backup), after, failed=failed)
        return True
