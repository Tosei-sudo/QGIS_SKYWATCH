"""
「参考情報の取り込み（JSON）」 (core/reference_import.py): a JSON file of infos
(the shape: resources/schemas/reference-info.schema.json), tried at once
(what each row would become: 新規・更新・変更なし・重複・エラー, with what
changes and why), then 「取り込む」 into the info layer. The dock then lists
the infos imported on their own.
"""
from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core import reference_import as ri
from ..core.reference_info import local_text
from .docks import plain_label, plain_tip
from .fit import fit_text

COLUMNS = ("行", "動作", "題名", "出来事の日時", "場所", "内容")
#: The actions' colours in the table.
COLOURS = {ri.NEW: "#15803d", ri.UPDATE: "#1d4ed8", ri.SAME: "#6b7280", ri.DUPLICATE: "#6b7280", ri.ERROR: "#b91c1c"}
WARNING_COLOUR = "#b45309"
#: Rows shown in the table (all are counted and imported).
SHOWN_ROWS = 2000
FILE_FILTER = "JSON (*.json);;すべてのファイル (*)"


class ReferenceImportDialog(QDialog):
    def __init__(self, dock, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.dock = dock
        self.path = ""
        self.data: Optional[ri.ImportFile] = None
        self.plan: Optional[ri.ImportPlan] = None
        self.result: Optional[ri.ImportResult] = None
        self._running = False
        self.setWindowTitle(self.tr("参考情報の取り込み（JSON）"))
        self.resize(940, 600)
        main = QVBoxLayout(self)
        intro = plain_label(QLabel(self.tr(
            "JSON ファイルの参考情報をまとめて登録・更新します。id が同じものは更新（書いたキーだけ）、id のないものは"
            "題名・出来事の日時・出典が同じなら重複として飛ばします。形は JSON Schema "
            "mikage/resources/schemas/reference-info.schema.json のとおり。")))
        intro.setWordWrap(True)
        main.addWidget(intro)
        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText(self.tr("取り込む JSON ファイル"))
        choose = QPushButton(self.tr("ファイルを選ぶ…"))
        choose.clicked.connect(lambda: self.choose_file())
        self.try_button = QPushButton(self.tr("もう一度試行"))
        self.try_button.setToolTip(self.tr("ファイルを読み直して、いまの参考情報と照らし合わせる（保存はしない）"))
        self.try_button.clicked.connect(lambda: self.try_run())
        self.try_button.setEnabled(False)
        row.addWidget(self.file_edit, 1)
        row.addWidget(choose)
        row.addWidget(self.try_button)
        main.addLayout(row)
        self.summary = plain_label(QLabel())
        self.summary.setStyleSheet("font-weight: bold;")
        self.summary.setWordWrap(True)
        main.addWidget(self.summary)
        self.notes = plain_label(QLabel())
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet(f"color: {WARNING_COLOUR};")
        self.notes.hide()
        main.addWidget(self.notes)
        self.problems_only = QCheckBox(self.tr("エラーと注意の行だけ"))
        self.problems_only.toggled.connect(lambda _on: self._render())
        main.addWidget(self.problems_only)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column, width in ((2, 160), (4, 185)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        main.addWidget(self.table, 1)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        main.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.import_button = buttons.addButton(self.tr("取り込む"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(lambda: self.run_import())
        close = buttons.addButton(self.tr("閉じる"), QDialogButtonBox.ButtonRole.RejectRole)
        close.clicked.connect(self.reject)
        main.addWidget(buttons)

    # ---- the file and the dry run --------------------------------------------------------------

    def choose_file(self, path: Optional[str] = None) -> bool:
        """The file to import (asked when not given), tried at once."""
        if path is None:
            start = os.path.dirname(self.path) if self.path else ""
            path, _f = QFileDialog.getOpenFileName(self, self.tr("取り込む参考情報（JSON）"), start, self.tr(FILE_FILTER))
            if not path:
                return False
        self.path = path
        self.file_edit.setText(path)
        self.try_button.setEnabled(True)
        return self.try_run()

    def _clear(self) -> None:
        self.plan = None
        self.result = None
        self.import_button.setEnabled(False)
        self.import_button.setText(self.tr("取り込む"))
        self.notes.hide()
        self.table.setRowCount(0)

    def try_run(self) -> bool:
        """What the import would do (the file read again; nothing saved). False when it could not start."""
        if not self.path or self._running:
            return False
        self._clear()
        self.data = ri.read_file(self.path)
        if not self.data.usable:
            self.summary.setText(self.tr("このファイルは取り込めない"))
            self.notes.setText("\n".join(self.data.problems[:20]) + (self.tr("\nほか {n} 件").format(n=len(self.data.problems) - 20)
                                                                    if len(self.data.problems) > 20 else ""))
            self.notes.setStyleSheet(f"color: {COLOURS[ri.ERROR]};")
            self.notes.show()
            self.status.setText("")
            return False
        self._running = True
        self.try_button.setEnabled(False)
        self.summary.setText(self.tr("{n} 件を試行中…").format(n=len(self.data.rows)))
        self.status.setText(self.tr("いまの参考情報を読んで照らし合わせ中（保存はしない）"))

        def done(plan: ri.ImportPlan) -> None:
            self._running = False
            self.try_button.setEnabled(True)
            self._show(plan)

        def failed(message: str) -> None:
            self._running = False
            self.try_button.setEnabled(True)
            self.summary.setText(self.tr("試行できない"))
            self.status.setText(message)

        self.dock.run_import(self.data, False, done, failed)
        return True

    def _show(self, plan: ri.ImportPlan) -> None:
        self.plan = plan
        self.summary.setText(plan.summary())
        self.notes.setStyleSheet(f"color: {WARNING_COLOUR};")
        self.notes.setText("\n".join(plan.notes))
        self.notes.setVisible(bool(plan.notes))
        n = len(plan.to_save)
        errors = plan.counts()[ri.ERROR]
        self.import_button.setText(self.tr("取り込む（{n} 件）").format(n=n) if n else self.tr("取り込む"))
        self.import_button.setEnabled(n > 0)
        if n:
            self.status.setText(self.tr("試行では未保存。「取り込む」で保存") +
                                (self.tr("（エラーの {e} 件は取り込まない）").format(e=errors) if errors else ""))
        else:
            self.status.setText(self.tr("取り込むものなし"))
        self._render()

    def _render(self) -> None:
        plan = self.plan
        if plan is None:
            self.table.setRowCount(0)
            return
        rows = [p for p in plan.rows if not self.problems_only.isChecked() or p.action == ri.ERROR or p.warnings]
        shown = rows[:SHOWN_ROWS]
        self.table.setRowCount(len(shown))
        for i, p in enumerate(shown):
            info = p.info
            place = ""
            if info is not None:
                parts = []
                if info.area_id:
                    parts.append(self.dock.area_name(info.area_id) or info.area_id)
                if info.has_place:
                    parts.append(f"{info.lat:.4f}, {info.lon:.4f}")
                place = "　".join(parts)
            values = (str(p.number), self.tr(p.label), p.title, local_text(p.time), place, p.text)
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                if j == 1:
                    item.setForeground(QBrush(QColor(COLOURS[p.action])))
                if j in (2, 4, 5):
                    item.setToolTip(plain_tip(v))
                if j == 5:
                    if p.action == ri.ERROR:
                        item.setForeground(QBrush(QColor(COLOURS[ri.ERROR])))
                    elif p.warnings:
                        item.setForeground(QBrush(QColor(WARNING_COLOUR)))
                if j == 0:
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                self.table.setItem(i, j, item)
        if len(rows) > SHOWN_ROWS:
            self.status.setText(self.tr("表は初めの {n} 行だけ（取り込むのはすべて）").format(n=SHOWN_ROWS))

    # ---- the import ----------------------------------------------------------------------------

    def run_import(self) -> bool:
        """Saves what the dry run showed (the file planned again against what is there now)."""
        if self.plan is None or not self.plan.to_save or self._running or self.data is None:
            return False
        self._running = True
        self.import_button.setEnabled(False)
        self.try_button.setEnabled(False)
        self.status.setText(self.tr("取り込み中…"))

        def done(result: ri.ImportResult) -> None:
            self._running = False
            self.try_button.setEnabled(True)
            self.result = result
            self.plan = result.plan
            self._render()
            self.summary.setText(self.tr("取り込み済み: {s}").format(s=result.summary()))
            if result.failures:
                lines = [self.tr("行 {n}: {m}").format(n=n, m=m) if n else m for n, m in result.failures[:10]]
                self.notes.setStyleSheet(f"color: {COLOURS[ri.ERROR]};")
                self.notes.setText(self.tr("保存できなかったもの（もう一度取り込めば続きから入る）:\n") + "\n".join(lines))
                self.notes.show()
            self.import_button.setText(self.tr("取り込む"))
            self.status.setText(self.tr("ドック「参考情報」に取り込んだものを出しています（「すべてに戻す」で期間の一覧）"))

        def failed(message: str) -> None:
            self._running = False
            self.try_button.setEnabled(True)
            self.import_button.setEnabled(True)
            self.status.setText(self.tr("取り込めない: {m}").format(m=message))

        self.dock.run_import(self.data, True, done, failed)
        return True
