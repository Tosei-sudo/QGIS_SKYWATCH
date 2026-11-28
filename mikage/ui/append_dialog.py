"""
「アペンド（既存レイヤーへ追加）…」 (core/append.py, qgis_io/append_io.py), as
ArcGIS's Append: the inputs (layers, their selection, a condition), the
target (a layer of the project, the settings file's ArcGIS tables, or a
URL), the schema type, the field map (追加先の列ごとに、入力ごとの元の列・
式・固定値・空, merge rules, part of the text; kept under a name, written to
and read from a JSON file, the last one per target remembered), the update
of matching features, the checks, then 「内容を確認」 (nothing written) and
「追加を実行」, and 「今回の追加を取り消し」 while the window stays open.

The plugin's own tables (観測記録・撮像依頼…) are a target for 管理者 only
(`append.own_tables`): written past their screens' checks.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from qgis.core import QgsMapLayerProxyModel, QgsProject, QgsVectorLayer
from qgis.gui import QgsFieldExpressionWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QRadioButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from ..core import append as ap
from ..core.errors import tagged, text as code_text
from ..qgis_io import append_io
from ..qgis_io.prefs import maps as prefs
from ..qgis_io.tasks import error_text, run_task
from .docks import ask_no, plain_label
from .fit import fit_text

try:
    VECTOR_LAYERS = QgsMapLayerProxyModel.Filter.VectorLayer
except AttributeError:  # older bindings: the enum unscoped
    VECTOR_LAYERS = QgsMapLayerProxyModel.VectorLayer
#: Rows of the check shown (all are counted and written).
SHOWN_ROWS = 2000
#: The field map's first columns (then one per input).
TARGET_COLUMN, TYPE_COLUMN, MERGE_COLUMN, FIRST_INPUT = 0, 1, 2, 3
SCHEMAS = [ap.NO_TEST, ap.TEST, ap.TEST_AND_SKIP]
GREY = QColor("#6b7280")


def _names(layers: list) -> list:
    """Each input's name in the field map: its layer's name (a second one of the same name numbered)."""
    out, seen = [], set()
    for layer in layers:
        name, n = layer.name(), 2
        while name in seen:
            name, n = f"{layer.name()} ({n})", n + 1
        seen.add(name)
        out.append(name)
    return out


class RuleDialog(QDialog):
    """Where one target field takes its value from, for one input: its fields (and how they merge), an expression, a value, or nothing."""

    def __init__(self, target: ap.TargetField, entry: ap.FieldEntry, input_name: str, layer: Optional[QgsVectorLayer],
                 fields: list, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("{t} の値（{i}）").format(t=target.name, i=input_name))
        rule = entry.rule(input_name) or ap.InputRule(ap.EMPTY)
        layout = QVBoxLayout(self)
        head = plain_label(QLabel(self.tr("追加先の列「{t}」（{k}）に入れる値を、入力「{i}」から選択").format(
            t=target.label(), k=ap.TYPE_NAMES.get(target.type, target.type), i=input_name)))
        head.setWordWrap(True)
        layout.addWidget(head)
        kinds = QHBoxLayout()
        self.group = QButtonGroup(self)
        self.kind_buttons = {}
        for key in (ap.FIELDS, ap.EXPRESSION, ap.VALUE, ap.EMPTY):
            b = QRadioButton(self.tr(ap.SOURCE_NAMES[key]))
            self.group.addButton(b)
            self.kind_buttons[key] = b
            kinds.addWidget(b)
            b.toggled.connect(lambda _on: self._shown())
        kinds.addStretch(1)
        layout.addLayout(kinds)

        self.fields_box = QGroupBox(self.tr("元の列（チェックした列を上から順に使用）"))
        fl = QVBoxLayout(self.fields_box)
        row = QHBoxLayout()
        self.field_list = QListWidget()
        self.field_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        chosen = [f for f in rule.fields] if rule.source == ap.FIELDS else []
        ordered = [f for f in fields if f.name in chosen]
        ordered.sort(key=lambda f: chosen.index(f.name))
        for f in ordered + [f for f in fields if f.name not in chosen]:
            item = QListWidgetItem(f"{f.name}（{f.alias}）" if f.alias and f.alias != f.name else f.name)
            item.setData(Qt.ItemDataRole.UserRole, f.name)
            item.setToolTip(ap.TYPE_NAMES.get(f.type, f.type))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if f.name in chosen else Qt.CheckState.Unchecked)
            self.field_list.addItem(item)
        row.addWidget(self.field_list, 1)
        moves = QVBoxLayout()
        for label, step in ((self.tr("上へ"), -1), (self.tr("下へ"), 1)):
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, s=step: self.move(s))
            moves.addWidget(b)
        moves.addStretch(1)
        row.addLayout(moves)
        fl.addLayout(row)
        form = QFormLayout()
        merge_row = QHBoxLayout()
        self.merge = QComboBox()
        for key, label in ap.MERGE_RULES.items():
            self.merge.addItem(self.tr(label), key)
        self.merge.setCurrentIndex(max(0, self.merge.findData(entry.merge)))
        self.merge.setToolTip(self.tr("列を 2 つ以上選んだときのまとめ方（この追加先の列のすべての入力に共通）"))
        self.delimiter = QLineEdit(entry.delimiter)
        self.delimiter.setMaximumWidth(80)
        self.delimiter.setToolTip(self.tr("連結の区切り文字"))
        self.merge.currentIndexChanged.connect(lambda _i: self._shown())
        merge_row.addWidget(self.merge)
        merge_row.addWidget(QLabel(self.tr("区切り")))
        merge_row.addWidget(self.delimiter)
        merge_row.addStretch(1)
        form.addRow(self.tr("まとめ方"), merge_row)
        part_row = QHBoxLayout()
        self.start = QSpinBox()
        self.start.setRange(0, 100000)
        self.start.setSpecialValueText(self.tr("はじめ"))
        self.start.setValue(rule.start or 0)
        self.end = QSpinBox()
        self.end.setRange(0, 100000)
        self.end.setSpecialValueText(self.tr("おわり"))
        self.end.setValue(rule.end or 0)
        part_row.addWidget(self.start)
        part_row.addWidget(QLabel(self.tr("文字目から")))
        part_row.addWidget(self.end)
        part_row.addWidget(QLabel(self.tr("文字目まで")))
        part_row.addStretch(1)
        form.addRow(self.tr("文字の範囲"), part_row)
        fl.addLayout(form)
        layout.addWidget(self.fields_box, 1)

        self.expression_box = QGroupBox(self.tr("式（QGIS の式。入力の地物ごとに計算）"))
        el = QVBoxLayout(self.expression_box)
        self.expression = QgsFieldExpressionWidget()
        if layer is not None:
            self.expression.setLayer(layer)
        self.expression.setExpression(rule.expression if rule.source == ap.EXPRESSION else "")
        el.addWidget(self.expression)
        layout.addWidget(self.expression_box)

        self.value_box = QGroupBox(self.tr("固定値（すべての地物に同じ値）"))
        vl = QVBoxLayout(self.value_box)
        self.value = QLineEdit(rule.value if rule.source == ap.VALUE else "")
        self.value.setPlaceholderText(self.tr("例: 2026-11-24、123、調査A"))
        vl.addWidget(self.value)
        layout.addWidget(self.value_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("決定"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        source = rule.source if not (rule.source == ap.FIELDS and not rule.fields) else ap.EMPTY
        self.kind_buttons[source].setChecked(True)
        self._shown()
        self.resize(520, 520)

    def kind(self) -> str:
        return next(k for k, b in self.kind_buttons.items() if b.isChecked())

    def _shown(self) -> None:
        kind = self.kind()
        self.fields_box.setVisible(kind == ap.FIELDS)
        self.expression_box.setVisible(kind == ap.EXPRESSION)
        self.value_box.setVisible(kind == ap.VALUE)
        self.delimiter.setEnabled(self.merge.currentData() == "join")

    def move(self, step: int) -> None:
        row = self.field_list.currentRow()
        if row < 0 or not 0 <= row + step < self.field_list.count():
            return
        item = self.field_list.takeItem(row)
        self.field_list.insertItem(row + step, item)
        self.field_list.setCurrentRow(row + step)

    def checked(self) -> list:
        return [self.field_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.field_list.count())
                if self.field_list.item(i).checkState() == Qt.CheckState.Checked]

    def set_checked(self, names: list) -> None:
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in names else Qt.CheckState.Unchecked)

    def result_rule(self) -> tuple:
        """(the rule, the merge rule, its delimiter) as chosen."""
        kind = self.kind()
        if kind == ap.FIELDS:
            names = self.checked()
            rule = ap.InputRule(ap.FIELDS, names, self.start.value() or None, self.end.value() or None) if names else ap.InputRule(ap.EMPTY)
        elif kind == ap.EXPRESSION:
            expression = self.expression.expression().strip()
            rule = ap.InputRule(ap.EXPRESSION, expression=expression) if expression else ap.InputRule(ap.EMPTY)
        elif kind == ap.VALUE:
            rule = ap.InputRule(ap.VALUE, value=self.value.text())
        else:
            rule = ap.InputRule(ap.EMPTY)
        return rule, self.merge.currentData(), self.delimiter.text()


class AppendDialog(QDialog):
    def __init__(self, context, allows: Callable[[str], bool] = lambda _f: True, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.context = context
        self.allows = allows
        self.setWindowTitle(self.tr("アペンド（既存レイヤーへ追加）"))
        self.resize(1000, 820)
        #: The inputs: (layer id, selected features only).
        self.inputs: list = []
        self.target: Optional[append_io.Target] = None
        self.field_map = ap.FieldMap()
        self.map_problems: list = []
        self.plan: Optional[ap.Plan] = None
        self.job: Optional[append_io.Job] = None
        #: The last write (and its job), taken back by 「今回の追加を取り消し」.
        self.applied: Optional[ap.Applied] = None
        self.applied_job: Optional[append_io.Job] = None
        self._running = False
        self._blocked = ""
        self._asking = None

        main = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(splitter, 1)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(top)
        splitter.addWidget(scroll)

        # ---- the inputs
        inputs = QGroupBox(self.tr("入力（追加する地物のレイヤー）"))
        il = QVBoxLayout(inputs)
        row = QHBoxLayout()
        self.input_select = QgsMapLayerComboBox()
        self.input_select.setFilters(VECTOR_LAYERS)
        self.input_select.setAllowEmptyLayer(True)
        self.input_select.setLayer(None)
        add = QPushButton(self.tr("入力に追加"))
        add.clicked.connect(lambda: self.add_input())
        self.remove_button = QPushButton(self.tr("選んだ入力を外す"))
        self.remove_button.clicked.connect(lambda: self.remove_input())
        row.addWidget(self.input_select, 1)
        row.addWidget(add)
        row.addWidget(self.remove_button)
        il.addLayout(row)
        self.input_table = QTableWidget(0, 3)
        self.input_table.setHorizontalHeaderLabels([self.tr("レイヤー"), self.tr("選択した地物のみ"), self.tr("地物数")])
        self.input_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.input_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.input_table.verticalHeader().hide()
        self.input_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.input_table.setMaximumHeight(120)
        il.addWidget(self.input_table)
        condition_row = QHBoxLayout()
        condition_row.addWidget(QLabel(self.tr("絞り込みの式")))
        self.condition = QLineEdit()
        self.condition.setPlaceholderText(self.tr("例: \"type\" = 'ship'（空ならすべて。どの入力にも同じ式）"))
        self.condition.textChanged.connect(lambda _t: self._invalidate())
        condition_row.addWidget(self.condition, 1)
        il.addLayout(condition_row)
        tl.addWidget(inputs)

        # ---- the target
        target = QGroupBox(self.tr("追加先"))
        gl = QVBoxLayout(target)
        row = QHBoxLayout()
        self.target_select = QComboBox()
        self.target_select.setMinimumContentsLength(30)
        self.target_select.activated.connect(lambda _i: self.choose_target())
        row.addWidget(self.target_select, 1)
        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setToolTip(self.tr("レイヤーの一覧を更新"))
        refresh.clicked.connect(lambda: self.refresh_targets())
        row.addWidget(refresh)
        gl.addLayout(row)
        url_row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…/FeatureServer/0")
        self.url_button = QPushButton(self.tr("列を取得"))
        self.url_button.clicked.connect(lambda: self.set_target_url(self.url.text()))
        self.url.returnPressed.connect(lambda: self.set_target_url(self.url.text()))
        url_row.addWidget(self.url, 1)
        url_row.addWidget(self.url_button)
        self.url_holder = QWidget()
        self.url_holder.setLayout(url_row)
        url_row.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(self.url_holder)
        self.target_note = plain_label(QLabel())
        self.target_note.setWordWrap(True)
        gl.addWidget(self.target_note)
        tl.addWidget(target)

        # ---- the schema type
        schema = QGroupBox(self.tr("列の合わせ方"))
        sl = QHBoxLayout(schema)
        self.schema_buttons = []
        self.schema_group = QButtonGroup(self)
        for key in SCHEMAS:
            b = QRadioButton(self.tr(ap.SCHEMA_NAMES[key]))
            self.schema_group.addButton(b)
            self.schema_buttons.append(b)
            sl.addWidget(b)
        self.schema_buttons[0].setChecked(True)
        for b in self.schema_buttons:
            b.toggled.connect(lambda _on: self._schema_changed())
        sl.addStretch(1)
        tl.addWidget(schema)

        # ---- the field map
        self.map_box = QGroupBox(self.tr("フィールドマップ（追加先の列ごとに入れる値。セルをダブルクリックで変更）"))
        ml = QVBoxLayout(self.map_box)
        row = QHBoxLayout()
        again = QPushButton(self.tr("列名で合わせ直す"))
        again.setToolTip(self.tr("追加先と入力の列を名前（大文字小文字・エイリアスも）で対応"))
        again.clicked.connect(lambda: self.remap())
        empty = QPushButton(self.tr("すべて空に"))
        empty.clicked.connect(lambda: self.clear_map())
        self.map_menu_button = QToolButton()
        self.map_menu_button.setText(self.tr("フィールドマップ ▾"))
        self.map_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.map_menu = QMenu(self)
        self.map_menu.aboutToShow.connect(self._fill_map_menu)
        self.map_menu_button.setMenu(self.map_menu)
        row.addWidget(again)
        row.addWidget(empty)
        row.addWidget(self.map_menu_button)
        row.addStretch(1)
        ml.addLayout(row)
        self.map_table = QTableWidget(0, FIRST_INPUT)
        self.map_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.map_table.verticalHeader().hide()
        self.map_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.map_table.horizontalHeader().setStretchLastSection(True)
        self.map_table.cellDoubleClicked.connect(lambda r, c: self.edit_cell(r, c))
        self.map_table.setMinimumHeight(200)
        ml.addWidget(self.map_table, 1)
        self.map_note = plain_label(QLabel())
        self.map_note.setWordWrap(True)
        ml.addWidget(self.map_note)
        tl.addWidget(self.map_box, 1)

        # ---- updates and options
        self.update_box = QGroupBox(self.tr("キー列が一致する地物は更新（一致しない地物だけ追加）"))
        self.update_box.setCheckable(True)
        self.update_box.setChecked(False)
        self.update_box.toggled.connect(lambda _on: self._invalidate())
        ul = QHBoxLayout(self.update_box)
        ul.addWidget(QLabel(self.tr("キー列")))
        self.key_field = QComboBox()
        self.key_field.setToolTip(self.tr("追加先の列。フィールドマップでこの列に入る値が同じ地物を、同じものとみなす"))
        self.key_field.currentIndexChanged.connect(lambda _i: self._invalidate())
        ul.addWidget(self.key_field)
        self.update_geometry = QCheckBox(self.tr("形状も更新"))
        self.update_geometry.toggled.connect(lambda _on: self._invalidate())
        ul.addWidget(self.update_geometry)
        ul.addStretch(1)
        tl.addWidget(self.update_box)
        options = QGroupBox(self.tr("値のチェックと失敗したとき"))
        ol = QVBoxLayout(options)
        self.enforce_domains = QCheckBox(self.tr("ドメイン（使える値の一覧・範囲）に合わない値の地物は飛ばす"))
        self.enforce_domains.setChecked(True)
        self.truncate = QCheckBox(self.tr("長すぎる文字列は切り詰めて追加（オフなら、その地物は飛ばす）"))
        self.all_or_nothing = QCheckBox(self.tr("1 件でも失敗したら、今回の分をすべて取り消し"))
        self.all_or_nothing.setChecked(True)
        for b in (self.enforce_domains, self.truncate, self.all_or_nothing):
            b.toggled.connect(lambda _on: self._invalidate())
            ol.addWidget(b)
        tl.addWidget(options)

        # ---- check, run, take back; the check's rows
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        run_row = QHBoxLayout()
        self.check_button = QPushButton(self.tr("内容を確認"))
        self.check_button.setToolTip(self.tr("各地物が追加・更新・飛ばすのどれになるかを表示（まだ追加しない）"))
        self.check_button.clicked.connect(lambda: self.check())
        self.run_button = QPushButton(self.tr("追加を実行"))
        self.run_button.setToolTip(self.tr("確認した内容で追加先に追加・更新（飛ばす地物は入れない）"))
        self.run_button.clicked.connect(lambda: self.run())
        self.run_button.setEnabled(False)
        self.undo_button = QPushButton(self.tr("今回の追加を取り消し"))
        self.undo_button.setToolTip(self.tr("直前の実行で追加した地物を削除し、更新した地物を元の値に戻す"))
        self.undo_button.clicked.connect(lambda: self.undo())
        self.undo_button.setEnabled(False)
        self.summary = plain_label(QLabel())
        self.summary.setWordWrap(True)
        run_row.addWidget(self.check_button)
        run_row.addWidget(self.run_button)
        run_row.addWidget(self.undo_button)
        run_row.addWidget(self.summary, 1)
        bl.addLayout(run_row)
        self.problems_only = QCheckBox(self.tr("飛ばす・注意のある地物だけ表示"))
        self.problems_only.toggled.connect(lambda _on: self._render_plan())
        bl.addWidget(self.problems_only)
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels([self.tr("入力"), self.tr("地物"), self.tr("結果"), self.tr("理由・注意")])
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.verticalHeader().hide()
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        bl.addWidget(self.result_table, 1)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        bl.addWidget(self.status)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        # (a scroll area asks for little height: the settings get most of the window at first)
        scroll.setMinimumHeight(240)
        self.result_table.setMinimumHeight(90)
        splitter.setSizes([560, 220])

        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        self.prepare()

    # ---- opening, closing --------------------------------------------------------------------------------

    def prepare(self, layer: Optional[QgsVectorLayer] = None) -> None:
        """Before it is shown: the targets listed again, the active layer offered as an input."""
        self.refresh_targets()
        if layer is not None and not self.inputs and isinstance(layer, QgsVectorLayer):
            self.input_select.setLayer(layer)

    def close_all(self) -> None:
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved):
            try:
                signal.disconnect(self._layers_changed)
            except (TypeError, RuntimeError):
                pass

    def _layers_changed(self, *_args) -> None:
        try:
            self.refresh_targets()
            self._render_inputs()
        except RuntimeError:
            pass  # the window is gone

    # ---- the inputs ----------------------------------------------------------------------------------------

    def input_layers(self) -> list:
        """The inputs still in the project: (layer, selected only, its name in the field map)."""
        project = QgsProject.instance()
        present = [(project.mapLayer(i), s) for i, s in self.inputs]
        present = [(layer, s) for layer, s in present if isinstance(layer, QgsVectorLayer)]
        names = _names([layer for layer, _s in present])
        return [(layer, s, n) for (layer, s), n in zip(present, names)]

    def add_input(self, layer: Optional[QgsVectorLayer] = None, selected_only: bool = False) -> bool:
        layer = layer or self.input_select.currentLayer()
        if not isinstance(layer, QgsVectorLayer):
            self.status.setText(self.tr("入力にするレイヤーを選択"))
            return False
        if any(i == layer.id() for i, _s in self.inputs):
            self.status.setText(self.tr("「{n}」は入力に追加済み").format(n=layer.name()))
            return False
        self.inputs.append((layer.id(), bool(selected_only)))
        self.status.setText("")
        self._render_inputs()
        self._inputs_changed()
        return True

    def remove_input(self, index: Optional[int] = None) -> None:
        rows = [index] if index is not None else sorted({i.row() for i in self.input_table.selectedIndexes()}, reverse=True)
        if not rows and self.inputs:
            rows = [len(self.inputs) - 1]
        for r in rows:
            if 0 <= r < len(self.inputs):
                del self.inputs[r]
        self._render_inputs()
        self._inputs_changed()

    def set_selected_only(self, index: int, on: bool) -> None:
        if 0 <= index < len(self.inputs):
            self.inputs[index] = (self.inputs[index][0], bool(on))
            self._render_inputs()
            self._invalidate()

    def _render_inputs(self) -> None:
        project = QgsProject.instance()
        self.inputs = [(i, s) for i, s in self.inputs if isinstance(project.mapLayer(i), QgsVectorLayer)]
        self.input_table.setRowCount(len(self.inputs))
        for r, (layer, selected, name) in enumerate(self.input_layers()):
            self.input_table.setItem(r, 0, QTableWidgetItem(name))
            box = QCheckBox()
            box.setChecked(selected)
            box.toggled.connect(lambda on, row=r: self.set_selected_only(row, on))
            self.input_table.setCellWidget(r, 1, box)
            count = layer.selectedFeatureCount() if selected else layer.featureCount()
            self.input_table.setItem(r, 2, QTableWidgetItem(f"{count:,}"))

    def _inputs_changed(self) -> None:
        """The field map's columns follow the inputs (what each had chosen kept)."""
        if self.target is not None and self.target.schema is not None:
            sources = [(n, append_io.fields_of(layer)) for layer, _s, n in self.input_layers()]
            self.field_map, problems = ap.fit_map(self.field_map, self.target.schema, sources)
            self.map_problems = problems
        self._render_map()
        self._invalidate()

    # ---- the target ----------------------------------------------------------------------------------------

    def refresh_targets(self) -> None:
        """The targets: the project's vector layers, the settings file's ArcGIS tables, then 「URL を入力…」."""
        current = self.target_select.currentData()
        self.target_select.blockSignals(True)
        self.target_select.clear()
        self.target_select.addItem(self.tr("（追加先を選択）"), None)
        layers = sorted((x for x in QgsProject.instance().mapLayers().values() if isinstance(x, QgsVectorLayer)), key=lambda x: x.name())
        for layer in layers:
            self.target_select.addItem(self.tr("レイヤー: {n}").format(n=layer.name()), ("layer", layer.id()))
        for label, role, url in append_io.config_tables(self.context):
            self.target_select.addItem(self.tr("設定ファイルの表: {l}（{r}）").format(l=label, r=role), ("esri", url))
        self.target_select.addItem(self.tr("ArcGIS フィーチャレイヤーの URL を入力…"), ("url", ""))
        self.target_select.setCurrentIndex(max(0, self._target_index(current)))
        self.target_select.blockSignals(False)
        self.url_holder.setVisible((self.target_select.currentData() or ("",))[0] == "url")

    def _target_index(self, data) -> int:
        """The target list's row of `data` (compared in Python: Qt does not compare the tuples it holds)."""
        if data is None:
            return 0
        return next((i for i in range(self.target_select.count()) if self.target_select.itemData(i) == data), -1)

    def choose_target(self) -> None:
        data = self.target_select.currentData()
        self.url_holder.setVisible(bool(data) and data[0] == "url")
        if not data:
            self._set_target(None)
        elif data[0] == "layer":
            layer = QgsProject.instance().mapLayer(data[1])
            self.set_target_layer(layer if isinstance(layer, QgsVectorLayer) else None)
        elif data[0] == "esri":
            self.set_target_url(data[1])
        else:
            self.url.setFocus()

    def set_target_layer(self, layer: Optional[QgsVectorLayer]) -> None:
        if layer is None:
            self._set_target(None)
            return
        i = self._target_index(("layer", layer.id()))
        if i >= 0:
            self.target_select.setCurrentIndex(i)
        self.url_holder.setVisible(False)
        target = append_io.target_for_layer(layer)
        if target.is_esri:
            self._describe(target)
        else:
            self._set_target(target)

    def set_target_url(self, url: str) -> None:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            self.status.setText(self.tr("ArcGIS フィーチャレイヤーの URL（https://…/FeatureServer/0）を入力"))
            return
        i = self._target_index(("esri", url))
        if i >= 0:
            self.target_select.setCurrentIndex(i)
            self.url_holder.setVisible(False)
        self._describe(append_io.target_for_url(url))

    def _token(self, url: str):
        return None

    def _describe(self, target: append_io.Target) -> None:
        """An Esri target's fields asked (in a task), then shown."""
        if self.context is None:
            self.status.setText(self.tr("ArcGIS へ追加するには設定ファイルが必要"))
            return
        self._set_target(None, keep_note=True)
        self.target_note.setText(self.tr("追加先の列を取得中…"))
        client, token = self.context.esri(), self._token(target.url)
        self._asking = target

        def done(t) -> None:
            if self._asking is target:
                self._asking = None
                self._set_target(t)

        def failed(message: str) -> None:
            if self._asking is target:
                self._asking = None
                self.target_note.setText(self.tr("追加先の取得に失敗: {m}").format(m=message))

        run_task(self.tr("アペンドの追加先の取得"), lambda task: append_io.describe_esri(target, client, token), done, failed)

    def _set_target(self, target: Optional[append_io.Target], keep_note: bool = False) -> None:
        self.target = target
        self._blocked = ""
        if target is None:
            if not keep_note:
                self.target_note.setText("")
            self.field_map = ap.FieldMap()
            self._render_map()
            self._fill_keys()
            self._invalidate()
            return
        schema = target.schema
        note = self.tr("{g}・書き込める列 {n} 個").format(g=ap.GEOMETRY_NAMES.get(schema.geometry, ""), n=len(schema.mappable))
        if schema.geometry != ap.NO_GEOMETRY and target.crs.isValid():
            note += self.tr("・座標系 {c}（入力から変換）").format(c=target.crs.authid() or target.crs.description())
        if target.is_esri:
            note += self.tr("。ArcGIS へ送信（作成者・更新者の列と監査ログはミカゲの書き込みと同じ）")
            role = append_io.own_table_of(target.url, self.context)
            if role:
                if self.allows("append.own_tables"):
                    note += self.tr("。注意: ミカゲ自身の「{r}」の表（画面のチェックを通らずに書き込み）").format(r=role)
                else:
                    self._blocked = tagged(code_text("E206", self.tr("（「{r}」はミカゲの画面で書く表。追加先にできるのは管理者の担当だけ。"
                                                                    "観測記録は「観測記録の一括取り込み」を使用）").format(r=role)), "E206")
        else:
            layer = append_io.target_layer(target)
            if layer is not None and layer.isEditable():
                note += self.tr("。編集中のレイヤー: 追加は編集バッファへ（保存は手動）")
        self.target_note.setText(self._blocked or note)
        sources = [(n, append_io.fields_of(layer)) for layer, _s, n in self.input_layers()]
        kept = prefs.append_last_map(target.key())
        if kept:
            problems: list = []
            self.field_map, fitted = ap.fit_map(ap.map_from_json(kept, problems), schema, sources)
            self.map_problems = problems + fitted
        else:
            self.field_map, self.map_problems = ap.auto_map(schema, sources), []
        self._render_map()
        self._fill_keys()
        self._invalidate()

    def _fill_keys(self) -> None:
        current = self.key_field.currentData()
        self.key_field.blockSignals(True)
        self.key_field.clear()
        if self.target is not None:
            for f in self.target.schema.mappable:
                self.key_field.addItem(f.label(), f.name)
        i = self.key_field.findData(current)
        self.key_field.setCurrentIndex(max(0, i))
        self.key_field.blockSignals(False)

    # ---- the field map -------------------------------------------------------------------------------------

    def schema_type(self) -> str:
        return next(k for k, b in zip(SCHEMAS, self.schema_buttons) if b.isChecked())

    def set_schema_type(self, key: str) -> None:
        self.schema_buttons[SCHEMAS.index(key)].setChecked(True)

    def _schema_changed(self) -> None:
        by_names = self.schema_type() != ap.NO_TEST
        self.map_box.setEnabled(not by_names)
        self.map_box.setTitle(self.tr("フィールドマップ（列名で対応。「フィールドマップで合わせる」のときだけ変更できる）") if by_names
                              else self.tr("フィールドマップ（追加先の列ごとに入れる値。セルをダブルクリックで変更）"))
        self._invalidate()

    def _sources(self) -> list:
        return [(n, append_io.fields_of(layer)) for layer, _s, n in self.input_layers()]

    def remap(self) -> None:
        if self.target is not None:
            self.field_map, self.map_problems = ap.auto_map(self.target.schema, self._sources()), []
            self._render_map()
            self._invalidate()

    def clear_map(self) -> None:
        for entry in self.field_map.entries:
            for name in list(entry.rules):
                entry.rules[name] = ap.InputRule(ap.EMPTY)
        self._render_map()
        self._invalidate()

    def set_rule(self, target: str, input_name: str, rule: ap.InputRule, merge: Optional[str] = None, delimiter: Optional[str] = None) -> None:
        """One cell of the field map (and the field's merge rule)."""
        entry = self.field_map.entry(target)
        if entry is None:
            return
        entry.rules[input_name] = rule
        if merge is not None:
            entry.merge = merge
        if delimiter is not None:
            entry.delimiter = delimiter
        self._render_map()
        self._invalidate()

    def _render_map(self) -> None:
        inputs = self.input_layers()
        self.map_table.setColumnCount(FIRST_INPUT + len(inputs))
        self.map_table.setHorizontalHeaderLabels([self.tr("追加先の列"), self.tr("型"), self.tr("まとめ方")] + [n for _l, _s, n in inputs])
        fields = self.target.schema.fields if self.target is not None else []
        self.map_table.setRowCount(len(fields))
        for r, f in enumerate(fields):
            required = f.editable and not f.nullable and not f.has_default
            name = QTableWidgetItem(f.label() + (" ＊" if required else ""))
            tips = [self.tr("必須（空にできない）")] if required else []
            if f.domain is not None:
                tips.append(self.tr("使える値: {d}").format(d=f.domain.text()))
            name.setToolTip("\n".join(tips))
            kind = ap.TYPE_NAMES.get(f.type, f.type) + (f"({f.length})" if f.length else "")
            type_item = QTableWidgetItem(kind)
            type_item.setToolTip(f.type_text)
            entry = self.field_map.entry(f.name)
            merge = QTableWidgetItem("")
            if not f.editable:
                for item in (name, type_item):
                    item.setForeground(QBrush(GREY))
                merge.setText(self.tr("自動（{w}）").format(w=f.automatic))
                merge.setForeground(QBrush(GREY))
            elif entry is not None:
                many = any(len(rule.fields) > 1 for rule in entry.rules.values() if rule.source == ap.FIELDS)
                text = ap.MERGE_RULES.get(entry.merge, entry.merge) + (f"（{entry.delimiter}）" if entry.merge == "join" else "")
                merge.setText(text if many or entry.merge != "first" else "")
            self.map_table.setItem(r, TARGET_COLUMN, name)
            self.map_table.setItem(r, TYPE_COLUMN, type_item)
            self.map_table.setItem(r, MERGE_COLUMN, merge)
            for c, (_layer, _s, input_name) in enumerate(inputs):
                if not f.editable:
                    cell = QTableWidgetItem("—")
                    cell.setForeground(QBrush(GREY))
                else:
                    rule = entry.rule(input_name) if entry is not None else None
                    cell = QTableWidgetItem(rule.text() if rule is not None else "（空）")
                    if rule is None or rule.source == ap.EMPTY or (rule.source == ap.FIELDS and not rule.fields):
                        cell.setForeground(QBrush(GREY))
                self.map_table.setItem(r, FIRST_INPUT + c, cell)
        self.map_table.resizeColumnsToContents()
        notes = []
        if self.target is None:
            notes.append(self.tr("追加先を選ぶと、列の一覧が出る"))
        elif not inputs:
            notes.append(self.tr("入力を追加すると、入力ごとの列が出る"))
        notes += self.map_problems[:5]
        self.map_note.setText("\n".join(notes))

    def cell_text(self, target: str, input_name: str) -> str:
        """What the field map's table shows for a target field and an input."""
        names = [n for _l, _s, n in self.input_layers()]
        fields = [f.name for f in self.target.schema.fields] if self.target is not None else []
        if target not in fields or input_name not in names:
            return ""
        return self.map_table.item(fields.index(target), FIRST_INPUT + names.index(input_name)).text()

    def edit_cell(self, row: int, column: int, dialog: Optional[RuleDialog] = None, exec_: bool = True) -> Optional[RuleDialog]:
        """A cell's rule (the merge column: the first input's), in its window."""
        if self.target is None or column < MERGE_COLUMN or not self.map_box.isEnabled():
            return None
        fields = self.target.schema.fields
        inputs = self.input_layers()
        if not 0 <= row < len(fields) or not fields[row].editable or not inputs:
            return None
        index = max(0, column - FIRST_INPUT)
        if index >= len(inputs):
            return None
        layer, _s, input_name = inputs[index]
        entry = self.field_map.entry(fields[row].name)
        if entry is None:
            return None
        dialog = dialog or RuleDialog(fields[row], entry, input_name, layer, append_io.fields_of(layer), self)
        if exec_ and dialog.exec() != QDialog.DialogCode.Accepted:
            return dialog
        rule, merge, delimiter = dialog.result_rule()
        self.set_rule(entry.target, input_name, rule, merge, delimiter)
        return dialog

    # ---- kept field maps -------------------------------------------------------------------------------------

    def _fill_map_menu(self) -> None:
        menu = self.map_menu
        menu.clear()
        menu.addAction(self.tr("名前を付けて保存…"), lambda: self.save_map())
        kept = prefs.append_maps()
        load = menu.addMenu(self.tr("保存したものを呼び出す"))
        remove = menu.addMenu(self.tr("保存したものを削除"))
        for name in sorted(kept):
            load.addAction(name, lambda n=name: self.load_map(n))
            remove.addAction(name, lambda n=name: self.delete_map(n))
        load.setEnabled(bool(kept))
        remove.setEnabled(bool(kept))
        menu.addSeparator()
        menu.addAction(self.tr("ファイルに書き出す…"), lambda: self.export_map())
        menu.addAction(self.tr("ファイルから読み込む…"), lambda: self.import_map())

    def save_map(self, name: Optional[str] = None) -> bool:
        if not self.field_map.entries:
            self.status.setText(self.tr("保存するフィールドマップがない（追加先と入力を選択）"))
            return False
        if name is None:
            name, ok = QInputDialog.getText(self, self.tr("フィールドマップを保存"), self.tr("名前"))
            if not ok:
                return False
        name = (name or "").strip()
        if not name:
            self.status.setText(self.tr("フィールドマップの名前を入力"))
            return False
        prefs.set_append_map(name, self.field_map.to_json())
        self.status.setText(self.tr("フィールドマップ「{n}」を保存").format(n=name))
        return True

    def _use_map(self, value, what: str) -> bool:
        if self.target is None:
            self.status.setText(self.tr("先に追加先を選択"))
            return False
        problems: list = []
        kept = ap.map_from_json(value, problems)
        self.field_map, fitted = ap.fit_map(kept, self.target.schema, self._sources())
        self.map_problems = problems + fitted
        self._render_map()
        self._invalidate()
        self.status.setText(what)
        return True

    def load_map(self, name: str) -> bool:
        kept = prefs.append_maps().get(name)
        if kept is None:
            self.status.setText(self.tr("フィールドマップ「{n}」がない").format(n=name))
            return False
        return self._use_map(kept, self.tr("フィールドマップ「{n}」を呼び出し").format(n=name))

    def delete_map(self, name: str, confirm: bool = True) -> None:
        if confirm and not ask_no(self, self.tr("フィールドマップを削除"), self.tr("保存したフィールドマップ「{n}」を削除").format(n=name),
                                  yes=self.tr("削除")):
            return
        prefs.delete_append_map(name)
        self.status.setText(self.tr("フィールドマップ「{n}」を削除").format(n=name))

    def export_map(self, path: Optional[str] = None) -> bool:
        if not self.field_map.entries:
            self.status.setText(self.tr("書き出すフィールドマップがない（追加先と入力を選択）"))
            return False
        if path is None:
            path, _f = QFileDialog.getSaveFileName(self, self.tr("フィールドマップを書き出し"), "", self.tr("JSON (*.json)"))
            if not path:
                return False
        try:
            with open(path, "w", encoding="utf-8") as h:
                h.write(self.field_map.text())
        except OSError as error:
            self.status.setText(self.tr("書き出しに失敗: {m}").format(m=error_text(error)))
            return False
        self.status.setText(self.tr("フィールドマップを書き出し: {p}").format(p=path))
        return True

    def import_map(self, path: Optional[str] = None) -> bool:
        if path is None:
            path, _f = QFileDialog.getOpenFileName(self, self.tr("フィールドマップを読み込み"), "", self.tr("JSON (*.json);;すべて (*)"))
            if not path:
                return False
        try:
            with open(path, encoding="utf-8-sig") as h:
                value = json.load(h)
        except (OSError, ValueError) as error:
            self.status.setText(self.tr("読み込みに失敗: {m}").format(m=error_text(error)))
            return False
        return self._use_map(value, self.tr("フィールドマップを読み込み: {p}").format(p=path))

    # ---- check, run, take back --------------------------------------------------------------------------

    def options(self) -> ap.Options:
        keys = [self.key_field.currentData()] if self.update_box.isChecked() and self.key_field.currentData() else []
        return ap.Options(self.schema_type(), keys, self.update_geometry.isChecked() and bool(keys), self.enforce_domains.isChecked(),
                          self.truncate.isChecked(), self.all_or_nothing.isChecked())

    def _invalidate(self) -> None:
        self.plan = None
        self.job = None
        self.run_button.setEnabled(False)
        self.summary.setText("")
        self.result_table.setRowCount(0)

    def _job(self) -> Optional[append_io.Job]:
        """The append as the window says it now (its layers taken on the main thread), or None and why in the status line."""
        if self._blocked:
            self.status.setText(self._blocked)
            return None
        if self.target is None:
            self.status.setText(self.tr("追加先を選択"))
            return None
        inputs = self.input_layers()
        if not inputs:
            self.status.setText(self.tr("入力のレイヤーを追加"))
            return None
        if not self.target.is_esri and any(layer.id() == self.target.layer_id for layer, _s, _n in inputs):
            self.status.setText(self.tr("追加先と同じレイヤーは入力にできない"))
            return None
        sources = []
        for layer, selected, name in inputs:
            source = append_io.InputSource(layer, selected, self.condition.text(), name)
            problem = source.condition_problem()
            if problem:
                self.status.setText(problem)
                return None
            if selected and not source.selection:
                self.status.setText(self.tr("「{n}」に選択した地物がない").format(n=name))
                return None
            sources.append(source)
        target = self.target
        if not target.is_esri:
            layer = append_io.target_layer(target)
            if layer is None:
                self.status.setText(self.tr("追加先のレイヤーが閉じられた"))
                return None
            # Its features as they are now (keys for updates).
            target = append_io.target_for_layer(layer)
            target.schema = self.target.schema
        return append_io.Job(sources, target, self.field_map.copy(), self.options(), list(self.map_problems))

    def check(self) -> bool:
        """What each feature becomes (in a task; nothing written)."""
        if self._running:
            return False
        job = self._job()
        if job is None:
            return False
        self._invalidate()
        self._running = True
        self._buttons()
        self.status.setText(self.tr("確認中…"))
        client = self.context.esri() if job.target.is_esri else None
        token = self._token(job.target.url) if job.target.is_esri else None

        def done(the_plan) -> None:
            self._running = False
            self.job, self.plan = job, the_plan
            self._buttons()
            self.status.setText("")
            self._render_plan()

        def failed(message: str) -> None:
            self._running = False
            self._buttons()
            self.status.setText(self.tr("確認に失敗: {m}").format(m=message))

        run_task(self.tr("アペンドの確認"), lambda task: append_io.prepare(job, client, token, task.check, task.progress), done, failed)
        return True

    def _buttons(self) -> None:
        writes = self.plan.writes if self.plan is not None else []
        self.check_button.setEnabled(not self._running)
        self.run_button.setEnabled(bool(writes) and not self._running and not self._blocked)
        self.undo_button.setEnabled(self.applied is not None and not self.applied.rolled_back and not self._running
                                    and bool(self.applied.added or self.applied.updated))

    def _render_plan(self) -> None:
        p = self.plan
        if p is None:
            return
        text = p.summary()
        counts = p.reason_counts()
        if counts:
            text += self.tr("。飛ばす理由: {r}").format(r="、".join(f"{k} {n:,} 件" for k, n in list(counts.items())[:3]))
        if p.skipped_inputs:
            text += self.tr("。列が一致しない入力: {i}").format(i="、".join(f"{n}（{w}）" for n, w in p.skipped_inputs.items()))
        self.summary.setText(text)
        rows = [r for r in p.rows if not self.problems_only.isChecked() or r.action == ap.SKIP or r.notes]
        shown = rows[:SHOWN_ROWS]
        self.result_table.setRowCount(len(shown))
        colours = {ap.SKIP: Qt.GlobalColor.red, ap.ADD: Qt.GlobalColor.darkGreen, ap.UPDATE: Qt.GlobalColor.blue}
        for i, r in enumerate(shown):
            cells = [r.input, str(r.number), r.label, "、".join(r.reasons + r.notes)]
            for j, t in enumerate(cells):
                item = QTableWidgetItem(t)
                if j == 2:
                    item.setForeground(colours[r.action])
                self.result_table.setItem(i, j, item)
        if len(rows) > SHOWN_ROWS:
            self.status.setText(self.tr("表示は先頭 {n:,} 件（追加はすべて）").format(n=SHOWN_ROWS))
        self._buttons()

    def run(self, confirm: bool = True) -> bool:
        """The checked plan written (an Esri target in a task, a QGIS layer here)."""
        if self.plan is None or self.job is None or self._running or not self.plan.writes:
            return False
        p, job = self.plan, self.job
        question = self.tr("「{t}」に追加 {a:,} 件・更新 {u:,} 件（飛ばす {s:,} 件）").format(
            t=job.target.name, a=p.count(ap.ADD), u=p.count(ap.UPDATE), s=p.count(ap.SKIP))
        if confirm and not ask_no(self, self.tr("アペンド"), question, yes=self.tr("追加")):
            return False
        prefs.set_append_last_map(job.target.key(), job.field_map.to_json())
        if not job.target.is_esri:
            try:
                applied = append_io.write_layer(job, p)
            except ValueError as error:
                self.status.setText(self.tr("追加に失敗: {m}").format(m=error_text(error)))
                return False
            self._written(job, applied)
            return True
        self._running = True
        self._buttons()
        self.status.setText(self.tr("追加中…"))
        client, token = self.context.esri(), self._token(job.target.url)

        def done(applied) -> None:
            self._running = False
            append_io.reload_shown(job.target)
            self._written(job, applied)

        def failed(message: str) -> None:
            self._running = False
            self._buttons()
            self.status.setText(self.tr("追加に失敗: {m}").format(m=message))

        run_task(self.tr("アペンド"), lambda task: append_io.write_esri(job, p, client, token, task.check, task.progress), done, failed)
        return True

    def _written(self, job: append_io.Job, applied: ap.Applied) -> None:
        self.applied, self.applied_job = applied, job
        self.plan, self.job = None, None
        self.run_button.setEnabled(False)
        failed = "".join(self.tr("\n失敗: {i} の {n}: {w}").format(i=p.input, n=p.number, w=why) for p, why in applied.failed[:5])
        self.status.setText(applied.text() + failed)
        self._buttons()

    def undo(self, confirm: bool = True) -> bool:
        """The last write taken back: what it added deleted, what it updated given back."""
        a, job = self.applied, self.applied_job
        if a is None or job is None or self._running or a.rolled_back:
            return False
        question = self.tr("「{t}」に今回追加した {a:,} 件を削除し、更新した {u:,} 件を元の値に戻す").format(
            t=job.target.name, a=len(a.added), u=len(a.updated))
        if confirm and not ask_no(self, self.tr("今回の追加を取り消し"), question, yes=self.tr("取り消し")):
            return False

        def finished(problems: list) -> None:
            self._running = False
            self.applied = None if not problems else self.applied
            self.status.setText(self.tr("今回の追加を取り消し") if not problems else
                                self.tr("取り消しに失敗: {m}").format(m="、".join(problems[:3])))
            self._buttons()

        if not job.target.is_esri:
            finished(append_io.undo_layer(job, a))
            return True
        self._running = True
        self._buttons()
        self.status.setText(self.tr("取り消し中…"))
        client, token = self.context.esri(), self._token(job.target.url)

        def done(problems) -> None:
            append_io.reload_shown(job.target)
            finished(problems)

        def failed(message: str) -> None:
            finished([message])

        run_task(self.tr("アペンドの取り消し"), lambda task: ap.undo_esri(client, job.target.url, token, a), done, failed)
        return True
