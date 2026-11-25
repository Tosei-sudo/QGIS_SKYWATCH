"""
観測記録ドックの一覧 (ui/obs_dock, docs/architecture.md 段階7): the list of records: its rows, the selection, the
summary, the fields offered and the bulk edit. A part of ObservationDock (ui/observation_dock.py): its
methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from qgis.PyQt.QtCore import QItemSelection, QItemSelectionModel, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication, QHBoxLayout, QTableWidgetItem, QToolButton, QWidget

from ...core.obs.api import Observation, class_by_name, find, kind_of, path_of
from ..text_assist_ui import SummaryDialog
from ..obs_overlay import UNSEEN_COLOR
from .common import REPORTED_ROW, WAITING_ROW


class TablePart:
    """観測記録ドックの一覧."""

    def _set_head(self, *names: str) -> None:
        self.table.clear()
        self.table.setColumnCount(len(names))
        self.table.setHorizontalHeaderLabels(list(names))
        self.table.setRowCount(0)
        self._row_actions = []
        self._row_records = []
        self._row_keys = []
        self.bulk_button.hide()
        self.remove_chosen_button.hide()
        self.alert_add_button.hide()

    def _row(self, cells: list, on_click: Optional[Callable] = None, on_double: Optional[Callable] = None,
             style: str = "", widget: Optional[QWidget] = None, selected: bool = False,
             records: Optional[list[Observation]] = None, reported: bool = False, waiting: bool = False, key: str = "") -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for i, c in enumerate(cells):
            item = QTableWidgetItem("" if c is None else str(c))
            if style == "deleted":
                font = item.font()
                font.setStrikeOut(True)
                item.setFont(font)
                item.setForeground(QColor("#888888"))
            elif style == "ghost":
                item.setForeground(QColor("#555555"))
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            elif style == "inside":
                item.setForeground(QColor("#0f766e"))
            elif style == "area":
                item.setForeground(QColor("#0f766e"))
            elif style == "unseen":
                # 不可 (a record, a thing in a 不可範囲, the area): grey.
                item.setForeground(QColor(UNSEEN_COLOR))
            if reported and style != "deleted":
                item.setBackground(QColor(REPORTED_ROW))
                item.setForeground(QColor("#1f2937"))
                item.setToolTip(self.tr("通報済み"))
            if waiting:
                # Not the local store: saved offline, on its way to this store's server.
                item.setBackground(QColor(WAITING_ROW))
                item.setForeground(QColor("#1e3a8a"))
                item.setToolTip(self.tr("送信待ち（まだ送っていない）"))
            self.table.setItem(r, i, item)
        if widget is not None:
            self.table.setCellWidget(r, self.table.columnCount() - 1, widget)
        self._row_actions.append((on_click, on_double))
        self._row_records.append(list(records or []))
        self._row_keys.append(key)
        if selected:
            self.table.selectRow(r)

    def _chosen_rows(self) -> tuple[set[str], str]:
        """What the rows chosen now stand for, and the current row (where the keyboard and Shift + click go on from)."""
        keys = self._row_keys
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        current = self.table.currentRow()
        return ({keys[r] for r in rows if 0 <= r < len(keys) and keys[r]},
                keys[current] if 0 <= current < len(keys) else "")

    def _choose_rows(self, keys: set[str], current: str = "") -> None:
        """
        The rows standing for `keys` chosen again after the list was shown
        anew (else a click on a record let go of it as soon as the record was
        read again in a task, and Ctrl + click choices went at every refresh).
        """
        model, last = self.table.model(), self.table.columnCount() - 1
        chosen = QItemSelection()
        for r, key in enumerate(self._row_keys):
            if key and key in keys:
                chosen.select(model.index(r, 0), model.index(r, last))
        sel = self.table.selectionModel()
        if not chosen.isEmpty():
            sel.select(chosen, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        if current and current in self._row_keys:
            sel.setCurrentIndex(model.index(self._row_keys.index(current), 0), QItemSelectionModel.SelectionFlag.NoUpdate)

    def show_row(self, key: str) -> None:
        """
        The row standing for `key` chosen alone and scrolled into view: a
        record opened from its mark on the map is the one chosen in the list
        too (操作性点検 4, tsudo 2026-11-23: the form opened, no row chosen).
        """
        if not key or key not in self._row_keys:
            return
        r = self._row_keys.index(key)
        index = self.table.model().index(r, 0)
        flags = QItemSelectionModel.SelectionFlag
        self.table.selectionModel().setCurrentIndex(index, flags.ClearAndSelect | flags.Rows)
        self.table.scrollTo(index)

    def _row_clicked(self, row: int, _col: int) -> None:
        if QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return  # choosing several, not opening one
        if 0 <= row < len(self._row_actions) and self._row_actions[row][0]:
            self._row_actions[row][0]()

    def _row_double_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._row_actions) and self._row_actions[row][1]:
            self._row_actions[row][1]()

    def selected_records(self) -> list[Observation]:
        """The living records of the rows chosen (a thing's row: all its records), each once."""
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        out: dict[str, Observation] = {}
        for r in rows:
            for o in self._row_records[r] if 0 <= r < len(self._row_records) else []:
                if o.deleted_at is None:
                    out.setdefault(o.id, o)
        return list(out.values())

    def _selection_changed(self) -> None:
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        self.bulk_button.setVisible(len(rows) > 1 and bool(self.selected_records()))
        self.remove_chosen_button.setVisible(len(rows) > 1 and bool(self.selected_records()))
        self.alert_add_button.setVisible(bool(rows) and self.context.config.alert_reports is not None and bool(self.selected_records()))
        self.event_add_button.setVisible(bool(rows) and self.context.config.events is not None and bool(self.selected_records()))
        refs = self.context.config.reference_info
        self.reference_button.setVisible(bool(rows) and refs is not None and refs.links is not None and bool(self.selected_records()))
        self.summary_button.setVisible(bool(rows) and self.context.text_assist.has("summary") and bool(self.selected_records()))

    def summarize(self, records: Optional[list[Observation]] = None) -> Optional[SummaryDialog]:
        """「まとめ…」: the records chosen (or `records`) in a few sentences, written in a window (文章の手伝い)."""
        records = self.selected_records() if records is None else records
        if not records or not self.context.text_assist.has("summary"):
            return None
        from ...core.obs.api import local_time

        dialog = SummaryDialog(self.context.text_assist, records, local_time, self.tr("選んだ記録"), self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        dialog.start()
        self._summary = dialog
        return dialog

    def fields_choices(self) -> dict:
        """What the model may choose from for the record's fields: the kinds, each 大分類's activities, the 確度 levels."""
        book = self.book
        taxa = book.taxonomy() if book is not None else []
        roots = [t.path[0] for t in taxa if len(t.path) == 1]

        def resolve(text: str) -> Optional[list[str]]:
            path = path_of(text)
            if path and find(taxa, path):
                return list(path)
            return class_by_name(taxa, text) if text.strip() else None

        return {
            # All of them: the model is named those the 備考 names and the 大分類's first (text_tasks.kinds_for).
            "kinds": [kind_of(t.path) for t in taxa],
            "activities": {r: book.activities_for(r)[0] for r in roots} if book is not None else {},
            "confidences": [c.label for c in book.confidences()] if book is not None else [],
            "resolve_kind": resolve,
            "activities_of": lambda kind: book.activity_words(kind) if book is not None else [],
        }

    def fields_values(self) -> dict:
        """The record form's fields as they are (for 項目の提案: only the empty ones are filled)."""
        return {"kind": self.kind_input.text().strip(), "activity": self.activity_input.text().strip(),
                "confidence": self.confidence_select.currentData() or "", "length": self.length_input.text().strip(),
                "width": self.width_input.text().strip(), "heading": self.heading_input.text().strip(),
                "object": self.object_input.text().strip()}

    def apply_fields(self, values: dict) -> None:
        """Puts the values taken from 項目の提案 in the form (as if chosen; nothing saved)."""
        if "kind" in values:
            self.choose_kind(list(values["kind"]))
        if "activity" in values:
            self._pick_activity(values["activity"])
        if "confidence" in values:
            i = self.confidence_select.findData(values["confidence"])
            if i >= 0:
                self.confidence_select.setCurrentIndex(i)
        for key, edit in (("length", self.length_input), ("width", self.width_input), ("heading", self.heading_input)):
            if key in values:
                edit.setText(f"{values[key]:g}")
        if any(k in values for k in ("length", "width", "heading")):
            self._render_measure()
            self._render_candidates()
        if "object" in values:
            self.object_input.setText(values["object"])
            self._object_typed()

    def bulk_edit(self, records: Optional[list[Observation]] = None):
        """Asks what to set on the chosen records (the kind, the activity, the 確度), then saves it on all of them."""
        from ..bulk_edit_dialog import BulkEditDialog, BulkField

        book = self.book
        records = self.selected_records() if records is None else records
        if book is None or not records:
            return None
        things = len({o.object_id for o in records})
        first = records[0]
        fields = [
            BulkField("kind", self.tr("種別"), words=self.kinds_model.stringList(), value=first.kind,
                      tip=self.tr("「大分類 > 種別 > 級」の形（下のボタンで分類体系から選べる）"), taxonomy=book.taxonomy()),
            BulkField("activity", self.tr("活動"), words=book.activity_words(first.kind), value=first.activity,
                      tip=self.tr("消失の記録は活動なしのまま")),
            BulkField("confidence", self.tr("確度"), choices=["", *(c.label for c in book.confidences())], value=first.confidence),
        ]
        what = self.tr("選んだ記録 {n} 件（モノ {m} 件）").format(n=len(records), m=things)
        dialog = BulkEditDialog(self.tr("観測記録の一括編集"), what, fields, self)
        ids = [o.id for o in records]
        dialog.accepted.connect(lambda: self.apply_bulk(ids, dialog.values()))
        dialog.finished.connect(lambda _r: dialog.deleteLater())
        dialog.open()
        return dialog

    def apply_bulk(self, ids: list[str], values: dict) -> Any:
        """Sets the values on the records (in a task for an Esri store); what went through and what not is told."""
        values = {k: v for k, v in values.items() if k in ("kind", "activity", "confidence")}
        if not values:
            return None
        return self.do(self.tr("観測記録の一括編集"), lambda book: book.update_many(ids, **values), watch=True)

    def _small(self, *buttons: tuple) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        for text, tip, slot, *on in buttons:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            if on:
                # A toggle (「流れ」): pressed while what it shows is shown.
                b.setCheckable(True)
                b.setChecked(bool(on[0]))
                b.setStyleSheet("QToolButton:checked { background: #dbeafe; border: 1px solid #2563eb; color: #1e3a8a; font-weight: bold; }")
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            lay.addWidget(b)
        return box
