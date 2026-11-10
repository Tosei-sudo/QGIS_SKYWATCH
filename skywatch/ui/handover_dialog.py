"""
「分担と引き継ぎ」 (kept by the observation dock, one at a time).

Tab 「分担」: who holds which image (core/obs/assignments.py), each with its
state and how many records were made in it since; one's own rows (or, after
asking, someone else's) can change state, get a note, be handed to someone
else or given back. Double-click: the image in the observation dock.

Tab 「引き継ぎメモ」: the last memo kept (when the settings file's
`handover.table` is set), the period (from where the last memo ended, else
the shift's length back), a 申し送り by hand; 「作成」 reads the records,
the 通報 sent, the open requests and the assignments (in a task) and writes
the memo (core/obs/handover.py), which can be edited before 「保存」 (the
table, the file) or 「コピー」.
"""
from __future__ import annotations

import time
from typing import Optional

from qgis.PyQt.QtCore import QDateTime
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core import edit_stamp
from ..core.obs.assignments import STATUSES, Assignment, progress_of, progress_text, same_person, update, with_status
from ..core.obs.handover import HOUR_MS, HandoverData, handover_text, keep_memo, last_memo, open_requests, write_file
from ..core.request.export import format_time
from ..qgis_io.tasks import run_task
from .list_views import column_menus

DAY_MS = 86_400_000


def _time_edit(ms: float) -> QDateTimeEdit:
    e = QDateTimeEdit(QDateTime.fromMSecsSinceEpoch(int(ms)))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd HH:mm")
    return e


class HandoverDialog(QDialog):
    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.setWindowTitle(self.tr("分担と引き継ぎ"))
        self.resize(900, 620)
        self.shown_items: list[Assignment] = []
        self.last = None
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # 分担.
        page = QWidget()
        g = QVBoxLayout(page)
        row = QHBoxLayout()
        self.mine_check = QCheckBox(self.tr("自分の担当のみ"))
        self.done_check = QCheckBox(self.tr("完了も表示（7 日以内）"))
        for c in (self.mine_check, self.done_check):
            c.toggled.connect(lambda _on: self.render_assignments())
            row.addWidget(c)
        row.addStretch(1)
        self.reload_button = QPushButton(self.tr("読み込み直し"))
        self.reload_button.clicked.connect(lambda: self.bar.reload())
        row.addWidget(self.reload_button)
        g.addLayout(row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([self.tr("画像"), self.tr("撮像日時"), self.tr("担当者"), self.tr("状態"), self.tr("進捗"),
                                              self.tr("担当にした日時"), self.tr("メモ")])
        for c in range(6):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._go(r))
        g.addWidget(self.table, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.table_columns = column_menus("handover", self.table, self)
        row = QHBoxLayout()
        self.status_select = QComboBox()
        for s in STATUSES:
            self.status_select.addItem(s)
        self.status_button = QPushButton(self.tr("状態を変更"))
        self.status_button.clicked.connect(lambda _on=False: self.change_status())
        self.note_button = QPushButton(self.tr("メモ…"))
        self.note_button.clicked.connect(lambda _on=False: self.change_note())
        self.hand_button = QPushButton(self.tr("ほかの人に渡す…"))
        self.hand_button.setToolTip(self.tr("選んだ画像の担当者を変更（交代で続きを任せるときなど）"))
        self.hand_button.clicked.connect(lambda _on=False: self.hand_over())
        self.release_button = QPushButton(self.tr("手放す"))
        self.release_button.clicked.connect(lambda _on=False: self.give_back())
        row.addWidget(self.status_select)
        for b in (self.status_button, self.note_button, self.hand_button, self.release_button):
            row.addWidget(b)
        row.addStretch(1)
        g.addLayout(row)
        self.tabs.addTab(page, self.tr("分担"))

        # 引き継ぎメモ.
        page = QWidget()
        h = QVBoxLayout(page)
        self.last_label = QLabel()
        self.last_label.setWordWrap(True)
        h.addWidget(self.last_label)
        self.last_text = QPlainTextEdit()
        self.last_text.setReadOnly(True)
        self.last_text.setMaximumHeight(140)
        h.addWidget(self.last_text)
        form = QFormLayout()
        now = time.time() * 1000
        self.start_edit, self.end_edit = _time_edit(now - self.settings.hours * HOUR_MS), _time_edit(now)
        period = QHBoxLayout()
        period.addWidget(self.start_edit, 1)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.end_edit, 1)
        self.now_button = QPushButton(self.tr("終わりを今に"))
        self.now_button.clicked.connect(lambda: self.end_edit.setDateTime(QDateTime.currentDateTime()))
        period.addWidget(self.now_button)
        form.addRow(self.tr("期間"), period)
        self.memo_edit = QPlainTextEdit()
        self.memo_edit.setPlaceholderText(self.tr("申し送り（次の担当者へ伝えること）"))
        self.memo_edit.setMaximumHeight(90)
        form.addRow(self.tr("申し送り"), self.memo_edit)
        h.addLayout(form)
        row = QHBoxLayout()
        self.make_button = QPushButton(self.tr("作成"))
        self.make_button.setToolTip(self.tr("期間の観測記録・通報・未処理の撮像依頼・分担を読んでメモを作成"))
        self.make_button.clicked.connect(self.make)
        row.addWidget(self.make_button)
        row.addStretch(1)
        h.addLayout(row)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(self.tr("引き継ぎメモ（作成後に編集可）"))
        h.addWidget(self.text_edit, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.copy_button = QPushButton(self.tr("コピー"))
        self.copy_button.clicked.connect(self.copy)
        self.save_button = QPushButton(self.tr("保存"))
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.copy_button)
        row.addWidget(self.save_button)
        h.addLayout(row)
        self.tabs.addTab(page, self.tr("引き継ぎメモ"))
        self.tabs.currentChanged.connect(self._tab_changed)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.bar.changed.connect(self.render_assignments)
        self.reset()

    # ---- the settings ------------------------------------------------------------

    @property
    def bar(self):
        return self.dock.assignment_bar

    @property
    def settings(self):
        return self.dock.context.config.handover

    def _say(self, text: str) -> None:
        self.status.setText(text)

    def reset(self) -> None:
        """The settings changed."""
        has = self.bar.source is not None
        self.tabs.setTabEnabled(0, has)
        if not has and self.tabs.currentIndex() == 0:
            self.tabs.setCurrentIndex(1)
        self.save_button.setToolTip(self.tr("引き継ぎメモのテーブル・ファイル（設定ファイルの handover）へ保存し、コピー"))
        self.last = None
        self.render_assignments()
        self._read_last()

    def _tab_changed(self, index: int) -> None:
        if index == 0 and self.bar.source is not None:
            self.bar.reload()

    # ---- 分担 ------------------------------------------------------------------------

    def render_assignments(self) -> None:
        user = edit_stamp.user_name()
        # The progress is counted from the records: not read yet (no image chosen), shown again once they are.
        if self.isVisible():
            self.dock.records_ready(self.render_assignments)
        book = self.dock.book
        records = list(book.records) if book else []
        items = [x for x in self.bar.items if (self.done_check.isChecked() or not x.done)
                 and (not self.mine_check.isChecked() or same_person(x.assignee, user))]
        self.shown_items = items
        zone = self.settings.time_zone
        self.table.setRowCount(0)
        for r, x in enumerate(items):
            self.table.insertRow(r)
            when = (lambda ms: "" if ms is None else str(format_time(ms, "%Y-%m-%d %H:%M", zone)))
            values = (x.label, when(x.image_time), x.assignee, x.status, progress_text(progress_of(x, records)), when(x.taken_at), x.note)
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if same_person(x.assignee, user):
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(r, c, item)
        if self.bar.source is None:
            self._say(self.tr("設定ファイルに assignments（分担のテーブル）なし"))
        elif self.bar.error:
            self._say(self.tr("分担の読み込みの失敗: {m}").format(m=self.bar.error))
        self._update_buttons()

    def selected(self) -> Optional[Assignment]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return self.shown_items[rows[0]] if rows and rows[0] < len(self.shown_items) else None

    def _update_buttons(self) -> None:
        x = self.selected()
        for b in (self.status_button, self.note_button, self.hand_button, self.release_button):
            b.setEnabled(x is not None)
        if x is not None:
            self.status_select.setCurrentText(x.status)

    def _go(self, row: int) -> None:
        if row >= len(self.shown_items):
            return
        x = self.shown_items[row]
        if not self.dock.go_to(x.image_id):
            self._say(self.tr("{name} は観測記録のドックで開いていない画像").format(name=x.label))

    def _mine_or_ask(self, x: Assignment, what: str) -> bool:
        """Whether one may change a row: one's own, or someone else's after asking."""
        if same_person(x.assignee, edit_stamp.user_name()):
            return True
        return QMessageBox.question(
            self, what, self.tr("{name} は {who} さんの担当です。変更しますか。").format(name=x.label, who=x.assignee or self.tr("（名前なし）")),
        ) == QMessageBox.StandardButton.Yes

    def _write(self, x: Assignment, new: Assignment, done: str) -> None:
        source, client, token = self.bar.source, self.dock.context.esri(), self.bar.token()

        def finished(_r) -> None:
            self._say(done)
            self.bar.read_at = 0.0
            self.bar.reload()

        def failed(message: str) -> None:
            self._say(message)
            self.bar.reload()

        run_task(self.tr("分担の更新"), lambda _t: update(client, source, new, x, token), finished, failed)

    def change_status(self, confirm: bool = True) -> None:
        x = self.selected()
        status = self.status_select.currentText()
        if x is None or status == x.status or (confirm and not self._mine_or_ask(x, self.tr("状態の変更"))):
            return
        self._write(x, with_status(x, status, int(time.time() * 1000)), self.tr("{name}: {s}").format(name=x.label, s=status))

    def change_note(self, text: Optional[str] = None) -> None:
        x = self.selected()
        if x is None:
            return
        if text is None:
            text, ok = QInputDialog.getMultiLineText(self, self.tr("分担のメモ"), x.label, x.note)
            if not ok:
                return
        from dataclasses import replace

        self._write(x, replace(x, note=text.strip()), self.tr("{name} のメモを保存").format(name=x.label))

    def hand_over(self, to: Optional[str] = None, confirm: bool = True) -> None:
        x = self.selected()
        if x is None or (confirm and not self._mine_or_ask(x, self.tr("担当の変更"))):
            return
        if to is None:
            to, ok = QInputDialog.getText(self, self.tr("ほかの人に渡す"), self.tr("{name} の新しい担当者（ユーザー名）").format(name=x.label))
            if not ok:
                return
        to = to.strip()
        if not to or same_person(to, x.assignee):
            return
        from dataclasses import replace

        self._write(x, replace(x, assignee=to), self.tr("{name} を {who} さんに").format(name=x.label, who=to))

    def give_back(self, confirm: bool = True) -> None:
        x = self.selected()
        if x is None:
            return
        if confirm and not same_person(x.assignee, edit_stamp.user_name()) and not self._mine_or_ask(x, self.tr("分担を手放す")):
            return
        self.bar.give_back(x, confirm=confirm)

    # ---- 引き継ぎメモ ------------------------------------------------------------------------

    def _token(self, source) -> Optional[str]:
        return self.dock.context.token_for(source.url, source.token) if source else None

    def _read_last(self) -> None:
        """The last memo kept (its end becomes this one's start)."""
        table = self.settings.table
        if table is None:
            self.last_label.setText(self.tr("前回の引き継ぎメモ: 保存先なし（設定ファイルの handover.table）"))
            self.last_text.setVisible(False)
            return
        self.last_text.setVisible(True)
        client, token = self.dock.context.esri(), self._token(table)
        since = time.time() * 1000 - 30 * DAY_MS

        def finished(memo) -> None:
            self.last = memo
            if memo is None:
                self.last_label.setText(self.tr("前回の引き継ぎメモ: なし（30 日以内）"))
                self.last_text.setPlainText("")
                return
            zone = self.settings.time_zone
            when = "" if memo.written_at is None else str(format_time(memo.written_at, "%Y-%m-%d %H:%M", zone))
            self.last_label.setText(self.tr("前回の引き継ぎメモ（{who}・{when}）").format(who=memo.written_by or "—", when=when))
            self.last_text.setPlainText(memo.text.replace("\r\n", "\n"))
            if memo.end is not None and memo.end < time.time() * 1000:
                self.start_edit.setDateTime(QDateTime.fromMSecsSinceEpoch(int(memo.end)))

        run_task(self.tr("前回の引き継ぎメモ"), lambda _t: last_memo(client, table, token, since), finished,
                 lambda m: self.last_label.setText(self.tr("前回の引き継ぎメモの読み込みの失敗: {m}").format(m=m)))

    def make(self) -> None:
        """Reads what goes in (in a task) and writes the memo."""
        from ..core.obs.alerts import load_log
        from ..core.obs.assignments import load_assignments
        from ..core.request.store import load_requests

        start = self.start_edit.dateTime().toMSecsSinceEpoch()
        end = self.end_edit.dateTime().toMSecsSinceEpoch()
        if end <= start:
            self._say(self.tr("期間の終わりが始まりより前"))
            return
        if not self.dock.records_ready(self.make):
            # Not read yet (no image chosen in 「観測記録」): made once they are.
            self._say(self.tr("観測記録を読み込み中…（読み終えたら作成）"))
            return
        config, client = self.dock.context.config, self.dock.context.esri()
        book = self.dock.book
        records = list(book.records) if book else []
        images = [(i.id, i.name) for i in book.images] if book else []
        stores = [(s, self._token(s)) for s in config.request_stores]
        alerts = config.alert_reports if config.alert_reports and config.alert_reports.table else None
        alert_token = self._token(alerts.table) if alerts else None
        source, assign_token = self.bar.source, self.bar.token()
        now = time.time() * 1000
        data = HandoverData(start=start, end=end, now=now, user=edit_stamp.user_name(), records=records, images=images,
                            memo=self.memo_edit.toPlainText())
        settings = self.settings

        def work(task):
            problems = []
            if alerts:
                try:
                    data.alerts = load_log(client, alerts, start, end, alert_token)
                except Exception as error:  # noqa: BLE001 (the other parts still go in)
                    problems.append(f"通報記録: {error}")
            if stores:
                data.requests = []
                for store, token in stores:
                    try:
                        data.requests += open_requests(load_requests(client, store, token, task.isCanceled), now, store.label)
                    except Exception as error:  # noqa: BLE001
                        problems.append(f"撮像依頼（{store.label}）: {error}")
            if source:
                try:
                    data.assignments = load_assignments(client, source, assign_token, done_since=start)
                except Exception as error:  # noqa: BLE001
                    problems.append(f"分担: {error}")
            data.problems = problems
            return handover_text(data, settings)

        def finished(text: str) -> None:
            self.make_button.setEnabled(True)
            self.text_edit.setPlainText(text.replace("\r\n", "\n"))
            self._data = data
            self._say(self.tr("引き継ぎメモを作成（編集してから保存・コピー）") if not data.problems
                      else self.tr("一部を読めずに作成: {p}").format(p="・".join(data.problems)))

        def failed(message: str) -> None:
            self.make_button.setEnabled(True)
            self._say(self.tr("引き継ぎメモの作成の失敗: {m}").format(m=message))

        if not records and book is None:
            self._say(self.tr("観測記録の保存先なし（記録の欄は 0 件）"))
        self.make_button.setEnabled(False)
        self._say(self.tr("作成中…"))
        run_task(self.tr("引き継ぎメモの作成"), work, finished, failed)

    def _text(self) -> str:
        return self.text_edit.toPlainText().replace("\n", self.settings.newline)

    def copy(self) -> None:
        if not self.text_edit.toPlainText().strip():
            self._say(self.tr("メモなし（「作成」で作る）"))
            return
        QApplication.clipboard().setText(self._text())
        self._say(self.tr("クリップボードへコピー"))

    def save(self) -> None:
        """Keeps the memo (table, file) and copies it."""
        data = getattr(self, "_data", None)
        if data is None or not self.text_edit.toPlainText().strip():
            self._say(self.tr("メモなし（「作成」で作る）"))
            return
        s, text = self.settings, self._text()
        data.memo = self.memo_edit.toPlainText()
        said = []
        QApplication.clipboard().setText(text)
        said.append(self.tr("クリップボードへコピー"))
        if s.file:
            try:
                path, replaced = write_file(text, s, data.now)
                said.append(self.tr("ファイル {p}").format(p=path))
                if replaced:
                    said.append(self.tr("{enc} で書けない文字を ? に置き換え").format(enc=s.file.encoding))
            except (OSError, ValueError) as error:
                said.append(self.tr("ファイルの書き込み失敗: {e}").format(e=getattr(error, "strerror", None) or error))
        if s.table is None:
            self._say("・".join(said))
            return
        client, token = self.dock.context.esri(), self._token(s.table)

        def finished(cut) -> None:
            self.save_button.setEnabled(True)
            kept = self.tr("引き継ぎメモのテーブルに保存")
            if cut:
                kept += self.tr("（長すぎるため切り詰め: {f}。全文はコピー・ファイルに）").format(f="・".join(cut))
            self._say("・".join([kept] + said))
            self._read_last()

        def failed(message: str) -> None:
            self.save_button.setEnabled(True)
            self._say(self.tr("テーブルへの保存の失敗: {m}").format(m=message) + "（" + "・".join(said) + "）")

        self.save_button.setEnabled(False)
        run_task(self.tr("引き継ぎメモの保存"), lambda _t: keep_memo(client, s.table, text, data, token), finished, failed)
