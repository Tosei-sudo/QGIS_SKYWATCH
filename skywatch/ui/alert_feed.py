"""
通知 ▸ 通報: the rows of the 通報 log table (`alertReports.table`) of the
last `live.days` days, newest first, read again every `live.seconds` while
the dock is shown.

Esri's REST services push nothing, so this is polling, not real time: each
poll is one small request for the object ids (then the new rows by their
ids), in a task kept out of the status bar. A poll that fails waits longer
before the next (up to 5 minutes); one that works goes back to the interval.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.obs.alerts import AlertSettings, LogRow, poll_log
from ..core.request.export import format_time
from ..core.time_zone import UTC
from ..qgis_io.tasks import run_task
from .docks import plain_label, plain_tip

DAY_MS = 86_400_000
#: The longest wait after polls failing one after another (ms).
MAX_BACKOFF_MS = 300_000
COLUMNS = ("通報日時", "フォーマット", "通報者", "通報文", "記録ID")


def _now() -> float:
    return time.time() * 1000


class AlertFeed(QWidget):
    #: The number of rows come in since the tab was last looked at.
    unseen_changed = pyqtSignal(int)

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.rows: dict[int, LogRow] = {}
        #: Object ids come in since the tab was last looked at (shown bold).
        self.unseen: set[int] = set()
        #: Records newly in the log (the observation dock highlights them), and told when some left it.
        self.on_reported: Optional[Callable[[set], None]] = None
        self.on_removed: Optional[Callable[[], None]] = None
        #: Opens a record by id (the observation dock's; set by the plugin).
        self.open_record: Optional[Callable[[str], None]] = None
        #: Opens 「通報記録」 of the alert dialog (set by the plugin).
        self.open_log: Optional[Callable[[], None]] = None
        self.active = False
        self._closed = False
        self._polling = False
        self._again = False
        self._first = True
        self._failures = 0
        self._client = None
        self._settings: Optional[AlertSettings] = None
        self._task = None

        v = QVBoxLayout(self)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.cellDoubleClicked.connect(lambda r, c: self.go(r))
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, tip, slot in [
                (self.tr("今すぐ更新"), self.tr("通報テーブルを今すぐ読み直す"), lambda: self.poll(force=True)),
                (self.tr("記録へ移動"), self.tr("選んだ行の観測記録を開く（ダブルクリックでも）"), lambda: self.go(self.table.currentRow())),
                (self.tr("選択をコピー"), self.tr("選んだ行の通報文をクリップボードへ（選んでいなければ一覧すべて）"), self.copy),
                (self.tr("通報記録の確認…"), self.tr("期間・フォーマットで絞って一覧、削除（再通報可に）"), lambda: self.open_log and self.open_log())]:
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        v.addLayout(row)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.poll())

    # --- Settings ---------------------------------------------------------------------------------

    def settings(self) -> Optional[AlertSettings]:
        s = self.context.config.alert_reports
        return s if s is not None and s.table is not None else None

    def interval_ms(self) -> int:
        s = self.settings()
        return int(s.live.seconds * 1000) if s else 0

    def reset(self) -> None:
        """The settings file read again: the list starts over (another table, field names or token)."""
        self._client = None
        self._settings = None
        self.rows, self.unseen = {}, set()
        self._first = True
        self._failures = 0
        self.timer.stop()
        self._render()
        s = self.settings()
        if s is None:
            self.status.setText(self.tr("設定ファイルの alertReports.table（通報テーブル）がないため使用不可"))
        elif s.live.seconds <= 0:
            self.status.setText(self.tr("自動更新なし（alertReports.live）。「今すぐ更新」で読む"))
        else:
            self.status.setText("")
        if self.active:
            QTimer.singleShot(0, lambda: self.poll())

    def set_active(self, on: bool) -> None:
        """Polls while the dock is shown (at once when it shows again); stops while hidden."""
        self.active = on
        if not on:
            self.timer.stop()
        elif not self._polling:
            self.poll()

    # --- Polling ----------------------------------------------------------------------------------

    def _next(self) -> None:
        interval = self.interval_ms()
        if not self.active or self._closed or interval <= 0:
            return
        if self._failures:
            interval = min(MAX_BACKOFF_MS, max(interval, interval * 2 ** self._failures))
        self.timer.start(interval)

    def poll(self, force: bool = False) -> None:
        """Asks the log table for what changed since the last poll (in a hidden task)."""
        if self._closed or self.context.loading:
            return
        s = self.settings()
        if s is None:
            return
        if self._polling:
            self._again = self._again or force
            return
        # Without the interval (live: false), the list is read once when first shown, then by hand.
        if not force and (not self.active or (s.live.seconds <= 0 and not self._first)):
            return
        self.timer.stop()
        if self._client is None or self._settings is not s:
            self._client, self._settings = self.context.esri(), s
        client, token = self._client, self.context.token_for(s.table.url, s.table.token)
        known = set(self.rows)
        since = _now() - s.live.days * DAY_MS if s.live.days > 0 else None
        self._polling = True

        def done(changes) -> None:
            self._polling = False
            if self._settings is not s:  # the settings changed meanwhile: that poll is dropped
                self._after()
                return
            self._failures = 0
            gone = {i for i in changes.gone if i in self.rows}
            removed = [self.rows.pop(i) for i in gone]
            self.unseen -= gone
            for row in changes.added:
                if row.oid is not None:
                    self.rows[row.oid] = row
            new = [r for r in changes.added if r.oid is not None]
            if not self._first and new:
                self.unseen |= {r.oid for r in new}
            first, self._first = self._first, False
            if new or removed or first:
                self._render()
            if new and self.on_reported:
                self.on_reported({r.obs_id for r in new if r.obs_id})
            if removed and self.on_removed and any((r.reported_at or 0) >= (since or 0) for r in removed):
                self.on_removed()
            self.status.setText(self.tr("通報 {n} 件（{t} 確認{every}）").format(
                n=len(self.rows), t=format_time(_now(), "%H:%M:%S", self._zone()),
                every=self.tr("、{s:g} 秒ごと").format(s=s.live.seconds) if s.live.seconds > 0 else ""))
            self.unseen_changed.emit(len(self.unseen))
            self._after()

        def failed(message: str) -> None:
            self._polling = False
            self._failures += 1
            self.status.setText(self.tr("通報テーブルの読み込みに失敗: {m}").format(m=message))
            self._after()

        self._task = run_task(self.tr("通報の確認"), lambda _t: poll_log(client, s, known, since, token), done, failed, hidden=True)

    def _after(self) -> None:
        self._task = None
        again, self._again = self._again, False
        if again:
            self.poll(force=True)
        else:
            self._next()

    # --- The list ---------------------------------------------------------------------------------

    def _zone(self):
        s = self.settings()
        fmt = s.format() if s else None
        return fmt.time_zone if fmt else UTC

    def shown(self) -> list[LogRow]:
        return sorted(self.rows.values(), key=lambda r: (-(r.reported_at or 0), -(r.oid or 0)))

    def _render(self) -> None:
        zone = self._zone()
        bold = QFont()
        bold.setBold(True)
        rows = self.shown()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            when = "" if row.reported_at is None else str(format_time(row.reported_at, "%Y-%m-%d %H:%M:%S", zone))
            for c, v in enumerate((when, row.format, row.reported_by, row.text, row.obs_id)):
                item = QTableWidgetItem(v)
                item.setData(Qt.ItemDataRole.UserRole, row.oid)
                if row.oid in self.unseen:
                    item.setFont(bold)
                if c == 3:
                    item.setToolTip(plain_tip(v))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()

    def seen(self) -> None:
        """The tab looked at: the rows come in are no longer new (bold stays until the next render)."""
        if self.unseen:
            self.unseen = set()
            self.unseen_changed.emit(0)
            self._render()

    def go(self, r: int) -> None:
        rows = self.shown()
        if 0 <= r < len(rows) and rows[r].obs_id and self.open_record:
            self.open_record(rows[r].obs_id)

    def copy(self) -> None:
        rows = self.shown()
        picked = sorted({i.row() for i in self.table.selectedIndexes()})
        chosen = [rows[r] for r in picked if r < len(rows)] or rows
        QApplication.clipboard().setText("".join(r.text + "\r\n" for r in chosen))
        self.status.setText(self.tr("{n} 行をクリップボードへコピー").format(n=len(chosen)))

    def close_down(self) -> None:
        self._closed = True
        self.timer.stop()
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
