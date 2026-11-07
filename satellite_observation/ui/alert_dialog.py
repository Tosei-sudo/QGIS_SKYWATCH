"""
「通報」: the notable observation records as one line each (core/obs/alerts.py).

Tab 「通報文の作成」: a format (the settings file's default first), the
period (登録日時 or 撮像日時), then 「抽出」 reads the rules, the geofences
and what was already reported from Esri (in a task) and lists a line per
record: untick one to leave it out, edit a line by hand. 「通報」 adds the
lines to the Esri log table, then copies the text (header, lines, footer)
and writes the file, as the format says; 「コピーのみ」 copies without
recording anything.

Tab 「通報記録」: the log table's rows of a period (and a format): copy
them, go to their record, or delete them so their records can be reported again.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QDateTime, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core import edit_stamp
from ..core.obs.alerts import (
    AlertItem, AlertSettings, LogRow, Moment, build_lines, load_geofences, load_log, load_rules, remove_log, report_text,
    reported_ids, select, write_file, write_log,
)
from ..core.request.export import format_time
from ..qgis_io.tasks import run_task

DAY_MS = 86_400_000


def _ms(edit: QDateTimeEdit) -> int:
    return int(edit.dateTime().toMSecsSinceEpoch())


def _time_edit(ms: int) -> QDateTimeEdit:
    e = QDateTimeEdit(QDateTime.fromMSecsSinceEpoch(int(ms)))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd HH:mm")
    return e


class AlertDialog(QDialog):
    """「通報」 (kept by the observation dock, one at a time)."""

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.setWindowTitle(self.tr("通報"))
        self.resize(900, 600)
        self.items: list[AlertItem] = []
        self.log_rows: list[LogRow] = []
        self._filling = False
        layout = QVBoxLayout(self)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # 通報文の作成.
        make = QWidget()
        m = QVBoxLayout(make)
        form = QFormLayout()
        self.format_select = QComboBox()
        self.format_select.currentIndexChanged.connect(lambda _i: self._render_lines())
        form.addRow(self.tr("フォーマット"), self.format_select)
        now = QDateTime.currentMSecsSinceEpoch()
        self.by_select = QComboBox()
        self.by_select.addItem(self.tr("登録日時"), "created")
        self.by_select.addItem(self.tr("撮像日時"), "imageTime")
        self.start_edit, self.end_edit = _time_edit(now - DAY_MS), _time_edit(now)
        period = QHBoxLayout()
        period.addWidget(self.by_select)
        period.addWidget(self.start_edit, 1)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.end_edit, 1)
        form.addRow(self.tr("期間"), period)
        self.again_check = QCheckBox(self.tr("通報済みも含める"))
        self.again_check.setToolTip(self.tr("通報テーブルにすでにある記録も抽出（再通報）"))
        form.addRow(self.again_check)
        m.addLayout(form)
        row = QHBoxLayout()
        self.pick_button = QPushButton(self.tr("抽出"))
        self.pick_button.setToolTip(self.tr("Esri の通報条件・ジオフェンス・通報済みを読み、条件に合う記録を抽出"))
        self.pick_button.clicked.connect(self.pick)
        row.addWidget(self.pick_button)
        row.addStretch(1)
        m.addLayout(row)
        self.lines_table = QTableWidget(0, 2)
        self.lines_table.setHorizontalHeaderLabels([self.tr("通報"), self.tr("行（編集可）")])
        self.lines_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.lines_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.lines_table.itemChanged.connect(lambda _item: self._render_text())
        self.lines_table.cellDoubleClicked.connect(lambda r, _c: self._go(self.items[r].record.id) if r < len(self.items) else None)
        m.addWidget(self.lines_table, 2)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText(self.tr("通報文（ヘッダー・行・フッター）"))
        m.addWidget(self.preview, 1)
        row = QHBoxLayout()
        self.send_button = QPushButton(self.tr("通報"))
        self.send_button.clicked.connect(lambda: self.send(record=True))
        self.copy_button = QPushButton(self.tr("コピーのみ"))
        self.copy_button.setToolTip(self.tr("通報テーブルに記録せずクリップボードへコピー"))
        self.copy_button.clicked.connect(lambda: self.send(record=False))
        row.addStretch(1)
        row.addWidget(self.copy_button)
        row.addWidget(self.send_button)
        m.addLayout(row)
        self.tabs.addTab(make, self.tr("通報文の作成"))

        # 通報記録.
        log = QWidget()
        g = QVBoxLayout(log)
        form = QFormLayout()
        self.log_start, self.log_end = _time_edit(now - 7 * DAY_MS), _time_edit(now + DAY_MS)
        period = QHBoxLayout()
        period.addWidget(self.log_start, 1)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.log_end, 1)
        form.addRow(self.tr("通報日時"), period)
        self.log_format = QComboBox()
        self.log_format.currentIndexChanged.connect(lambda _i: self._render_log())
        form.addRow(self.tr("フォーマット"), self.log_format)
        g.addLayout(form)
        row = QHBoxLayout()
        self.log_button = QPushButton(self.tr("読み込み"))
        self.log_button.clicked.connect(self.read_log)
        row.addWidget(self.log_button)
        row.addStretch(1)
        g.addLayout(row)
        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels([self.tr("通報日時"), self.tr("フォーマット"), self.tr("通報者"), self.tr("行"), self.tr("観測ID")])
        for c in (0, 1, 2):
            self.log_table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.log_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.cellDoubleClicked.connect(lambda r, _c: self._go(self._shown_log()[r].obs_id))
        g.addWidget(self.log_table, 1)
        row = QHBoxLayout()
        self.log_copy = QPushButton(self.tr("選択をコピー"))
        self.log_copy.clicked.connect(self.copy_log)
        self.log_remove = QPushButton(self.tr("選択を削除（再通報可にする）"))
        self.log_remove.clicked.connect(self.remove_log)
        row.addStretch(1)
        row.addWidget(self.log_copy)
        row.addWidget(self.log_remove)
        g.addLayout(row)
        self.tabs.addTab(log, self.tr("通報記録"))

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.reset()

    # ---- settings ------------------------------------------------------------

    @property
    def settings(self) -> Optional[AlertSettings]:
        return self.dock.context.config.alert_reports

    def _token(self, source) -> Optional[str]:
        return self.dock.context.token_for(source.url, source.token) if source else None

    def reset(self) -> None:
        """The formats of the settings file (again), the default chosen."""
        s = self.settings
        for select_, everything in ((self.format_select, False), (self.log_format, True)):
            was = select_.currentData()
            select_.blockSignals(True)
            select_.clear()
            if everything:
                select_.addItem(self.tr("すべて"), "")
            for f in s.formats if s else []:
                select_.addItem(f.name + (self.tr("（既定）") if f.name == s.default else ""), f.name)
            i = select_.findData(was) if was else -1
            if i < 0 and not everything and s:
                i = select_.findData(s.default)
            select_.setCurrentIndex(max(0, i))
            select_.blockSignals(False)
        if s is None:
            self.note.setText(self.tr("設定ファイルに alertReports（通報の条件・フォーマット）なし"))
        else:
            parts = [self.tr("通報条件: ") + (self.tr("Esri の条件テーブル") if s.rules else self.tr("なし（固定の条件のみ）"))]
            if s.geofences:
                parts.append(self.tr("ジオフェンスあり"))
            parts.append(self.tr("通報テーブル: ") + (self.tr("あり") if s.table else self.tr("なし（通報済みの管理なし）")))
            self.note.setText("・".join(parts))
        for w in (self.pick_button, self.send_button, self.copy_button, self.format_select):
            w.setEnabled(s is not None)
        for w in (self.log_button, self.log_copy, self.log_remove):
            w.setEnabled(bool(s and s.table))
        self._render_lines()

    def _format(self):
        s = self.settings
        return s.format(self.format_select.currentData() or "") if s else None

    def _say(self, text: str) -> None:
        self.status.setText(text)

    def _go(self, record_id: str) -> None:
        book = self.dock.book
        o = book.record(record_id) if book else None
        if o is None:
            self._say(self.tr("観測記録が見つからない（別の保存先か削除済み）: {i}").format(i=record_id))
            return
        self.dock.open_record(o)

    # ---- making ---------------------------------------------------------------

    def pick(self) -> None:
        """Reads the rules, geofences and log (in a task), then lists the notable records."""
        s, book = self.settings, self.dock.book
        if s is None or book is None:
            self._say(self.tr("観測記録の保存先なし"))
            return
        client = self.dock.context.esri()
        records, levels = list(book.records), book.confidences()
        start, end, by = _ms(self.start_edit), _ms(self.end_edit), self.by_select.currentData()
        again = self.again_check.isChecked()
        tokens = {k: self._token(getattr(s, k)) for k in ("rules", "geofences", "table")}

        def work(_task):
            rules = load_rules(client, s.rules, tokens["rules"]) if s.rules else None
            fences = load_geofences(client, s.geofences, tokens["geofences"]) if s.geofences else []
            done = set() if again or not s.table else reported_ids(client, s, tokens["table"])
            return select(records, s, rules, fences, levels, done, start, end, by), rules

        def finished(result) -> None:
            self.pick_button.setEnabled(True)
            self.items, rules = result
            self._render_lines()
            rule_note = "" if rules is None else self.tr("（条件 {n} 件）").format(n=len(rules))
            self._say(self.tr("{n} 件を抽出{r}").format(n=len(self.items), r=rule_note))

        def failed(message: str) -> None:
            self.pick_button.setEnabled(True)
            self._say(self.tr("抽出の失敗: {m}").format(m=message))

        self.pick_button.setEnabled(False)
        self._say(self.tr("抽出中…"))
        run_task(self.tr("通報の抽出"), work, finished, failed)

    def _moment(self, count: int) -> Moment:
        return Moment(now=QDateTime.currentMSecsSinceEpoch(), user=edit_stamp.user_name(), count=count)

    def _render_lines(self) -> None:
        fmt = self._format()
        self._filling = True
        self.lines_table.blockSignals(True)
        self.lines_table.setRowCount(0)
        lines = build_lines(self.items, fmt, self._moment(len(self.items))) if fmt else []
        for r, line in enumerate(lines):
            self.lines_table.insertRow(r)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked)
            self.lines_table.setItem(r, 0, check)
            self.lines_table.setItem(r, 1, QTableWidgetItem(line))
        self.lines_table.blockSignals(False)
        self._filling = False
        self._render_text()

    def chosen(self) -> tuple[list[AlertItem], list[str]]:
        """The ticked records and their lines (as edited)."""
        items, lines = [], []
        for r, item in enumerate(self.items):
            check, cell = self.lines_table.item(r, 0), self.lines_table.item(r, 1)
            if check is not None and check.checkState() == Qt.CheckState.Checked:
                items.append(item)
                lines.append(cell.text() if cell else "")
        return items, lines

    def text(self, at: Optional[Moment] = None) -> str:
        fmt = self._format()
        items, lines = self.chosen()
        return report_text(lines, fmt, at or self._moment(len(items))) if fmt else ""

    def _render_text(self) -> None:
        if not self._filling:
            self.preview.setPlainText(self.text().replace("\r\n", "\n"))

    def send(self, record: bool = True, after: Optional[Callable[[str], None]] = None) -> None:
        """
        「通報」: the lines to the Esri log table (in a task), then the
        clipboard and the file as the format says; 「コピーのみ」 (record
        False): the clipboard only.
        """
        s, fmt = self.settings, self._format()
        items, lines = self.chosen()
        if s is None or fmt is None:
            return
        if not items:
            self._say(self.tr("通報する記録なし（抽出してチェック）"))
            return
        at = self._moment(len(items))
        text = report_text(lines, fmt, at)
        if not record:
            QApplication.clipboard().setText(text)
            self._say(self.tr("{n} 件をクリップボードへコピー（通報テーブルには記録せず）").format(n=len(items)))
            return

        def outputs(logged: int) -> None:
            said = [self.tr("通報テーブルに {n} 件").format(n=logged)] if s.table else []
            if fmt.clipboard:
                QApplication.clipboard().setText(text)
                said.append(self.tr("クリップボードへコピー"))
            if fmt.file:
                try:
                    said.append(self.tr("ファイル {p}").format(p=write_file(text, fmt, at)))
                except OSError as error:
                    said.append(self.tr("ファイルの書き込み失敗: {e}").format(e=error.strerror or error))
            self.send_button.setEnabled(True)
            sent = {i.record.id for i in items}
            self.items = [i for i in self.items if i.record.id not in sent] if s.table else self.items
            self._render_lines()
            message = self.tr("{n} 件を通報: ").format(n=len(items)) + "・".join(said or [self.tr("出力先なし")])
            self._say(message)
            if after:
                after(message)

        if not s.table:
            outputs(0)
            return
        client, token = self.dock.context.esri(), self._token(s.table)

        def failed(message: str) -> None:
            self.send_button.setEnabled(True)
            self._say(self.tr("通報テーブルへの記録の失敗（コピー・ファイル出力もせず）: {m}").format(m=message))

        self.send_button.setEnabled(False)
        run_task(self.tr("通報の記録"), lambda _t: write_log(client, s, fmt, items, lines, at, token), outputs, failed)

    # ---- the log ------------------------------------------------------------

    def read_log(self) -> None:
        s = self.settings
        if s is None or s.table is None:
            return
        client, token = self.dock.context.esri(), self._token(s.table)
        start, end = _ms(self.log_start), _ms(self.log_end)

        def finished(rows) -> None:
            self.log_button.setEnabled(True)
            self.log_rows = rows
            self._render_log()
            self._say(self.tr("通報記録 {n} 件").format(n=len(rows)))

        def failed(message: str) -> None:
            self.log_button.setEnabled(True)
            self._say(self.tr("通報記録の読み込みの失敗: {m}").format(m=message))

        self.log_button.setEnabled(False)
        run_task(self.tr("通報記録の読み込み"), lambda _t: load_log(client, s, start, end, token), finished, failed)

    def _shown_log(self) -> list[LogRow]:
        name = self.log_format.currentData() or ""
        return [r for r in self.log_rows if not name or r.format == name]

    def _render_log(self) -> None:
        s = self.settings
        fmt = s.format() if s else None
        self.log_table.setRowCount(0)
        for r, row in enumerate(self._shown_log()):
            self.log_table.insertRow(r)
            when = "" if row.reported_at is None or fmt is None else str(format_time(row.reported_at, "%Y-%m-%d %H:%M:%S", fmt.time_zone))
            for c, v in enumerate((when, row.format, row.reported_by, row.text, row.obs_id)):
                self.log_table.setItem(r, c, QTableWidgetItem(v))

    def _selected_log(self) -> list[LogRow]:
        shown = self._shown_log()
        rows = sorted({i.row() for i in self.log_table.selectedIndexes()})
        return [shown[r] for r in rows if r < len(shown)]

    def copy_log(self) -> None:
        rows = self._selected_log() or self._shown_log()
        QApplication.clipboard().setText("".join(r.text + "\r\n" for r in rows))
        self._say(self.tr("{n} 行をクリップボードへコピー").format(n=len(rows)))

    def remove_log(self, confirm: bool = True) -> None:
        s, rows = self.settings, self._selected_log()
        if s is None or not rows:
            self._say(self.tr("削除する行を選択"))
            return
        if confirm and QMessageBox.question(
                self, self.tr("通報記録の削除"),
                self.tr("選択した {n} 行を通報テーブルから削除します（その記録は再び抽出されます）。").format(n=len(rows))) != QMessageBox.StandardButton.Yes:
            return
        client, token = self.dock.context.esri(), self._token(s.table)

        def finished(n: int) -> None:
            gone = {id(r) for r in rows}
            self.log_rows = [r for r in self.log_rows if id(r) not in gone]
            self._render_log()
            self._say(self.tr("通報記録 {n} 行を削除").format(n=n))

        run_task(self.tr("通報記録の削除"), lambda _t: remove_log(client, s, rows, token), finished,
                 lambda m: self._say(self.tr("通報記録の削除の失敗: {m}").format(m=m)))
