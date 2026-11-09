"""
「計画と実績の照合」: the requests handed out in the daily plan files (the
出力履歴) set against the image catalog, over a span of target days. 「照合」
asks the catalog about the rows not yet settled (as 「計算」 does) and shows
every row: 撮像済み with the image, 未撮像 (carried over and offered a
raise at the next 「計算」), or 未判定 (still waiting). A row can be set by
hand when the catalog is late or wrong; the ledger follows (raise undone,
期間内1回 完了, or back to 有効).
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional

from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPushButton,
    QTableView, QVBoxLayout,
)

from ..core.request.follow import follow_and_save, found_text, in_days, marked, next_step, save_marked, tally
from ..core.request.model import jst_day
from ..core.request.store import history_where, load_history, load_requests
from ..qgis_io.tasks import run_task
from .list_views import column_menus

COLUMNS = ("対象日", "システム", "衛星", "依頼ID", "依頼", "撮像", "画像ID", "この先")
SHOWN = ("すべて", "未撮像", "撮像済み", "未判定")
COLORS = {"撮像済み": "#16a34a", "未撮像": "#dc2626", "未判定": "#6b7280"}


def _now_ms() -> float:
    return time.time() * 1000


def _qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


class PlanResultsDialog(QDialog):
    """The handed-out requests of some days and whether they were taken."""

    def __init__(self, dock, parent=None):
        super().__init__(parent or dock)
        self.dock = dock
        self.context = dock.context
        self.setWindowTitle(self.tr("計画と実績の照合"))
        self.resize(900, 520)
        self.task = None
        self.history: list = []
        self.requests: dict = {}
        self.rows: list = []
        #: The target days whose history was read (first, last), once read.
        self.loaded: Optional[tuple] = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("対象日")))
        today = jst_day(_now_ms())
        self.first = QDateEdit(_qdate(today - timedelta(days=14)))
        self.last = QDateEdit(_qdate(today))
        for d in (self.first, self.last):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
            d.dateChanged.connect(lambda _=None: self._span_changed())
        top.addWidget(self.first)
        top.addWidget(QLabel("〜"))
        top.addWidget(self.last)
        self.shown = QComboBox()
        self.shown.addItems([self.tr(s) for s in SHOWN])
        self.shown.currentIndexChanged.connect(lambda _=None: self.render())
        top.addWidget(self.shown)
        self.run_button = QPushButton(self.tr("照合"))
        self.run_button.setToolTip(self.tr("未判定の依頼を画像カタログと照合し、出力履歴と台帳に保存"))
        self.run_button.clicked.connect(lambda: self.run())
        top.addWidget(self.run_button)
        top.addStretch(1)
        layout.addLayout(top)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.model = QStandardItemModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        layout.addWidget(self.table, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.table_columns = column_menus("plan-results", self.table, self)
        hand = QHBoxLayout()
        self.taken_button = QPushButton(self.tr("撮像済みにする"))
        self.taken_button.setToolTip(self.tr("選んだ依頼を撮像済みに（繰り上げを戻し、期間内1回は完了）"))
        self.taken_button.clicked.connect(lambda: self.mark("撮像済み"))
        self.missed_button = QPushButton(self.tr("未撮像にする"))
        self.missed_button.setToolTip(self.tr("選んだ依頼を未撮像に（次回の計算で持ち越し・繰り上げ候補）"))
        self.missed_button.clicked.connect(lambda: self.mark("未撮像"))
        for b in (self.taken_button, self.missed_button):
            hand.addWidget(b)
        hand.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        hand.addWidget(close)
        layout.addLayout(hand)
        self._update_buttons()

    # --- Reading and following up ----------------------------------------------------------------

    def run(self, follow: bool = True) -> None:
        """Reads the ledger and history again, and (`follow`) asks the catalog about the rows not settled."""
        dock, store = self.dock, self.dock.store
        if self.task is not None:
            return
        if store is None:
            self.status.setText(self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし"))
            return
        if not store.history:
            self.status.setText(self.tr("{s} に出力履歴（history）なし").format(s=store.label))
            return
        catalog = dock.catalog() if follow else None
        client, token = self.context.esri(), dock.token()
        catalog_token = self.context.token_for(catalog.url, catalog.token) if catalog else None
        now = _now_ms()
        first, last = self.span()
        # The span's rows and those still open (followed up as 「計算」 does), not the whole table.
        where = (f"({history_where(store, first=first.isoformat(), last=last.isoformat())}) OR "
                 f"({history_where(store, open_rows=True)})")
        self.status.setText(self.tr("出力履歴を読み込み、画像カタログと照合中…") if follow else self.tr("出力履歴を読み込み中…"))

        def work(task):
            notes = []
            requests = load_requests(client, store, token, task.isCanceled)
            history = load_history(client, store, token, where, cancelled=task.isCanceled)
            by_id = {r.id: r for r in requests}
            if catalog is not None:
                names = self.context.satellite_names(client, task.isCanceled)
                problems = []
                found, changed = follow_and_save(client, store, token, catalog, catalog_token, history, by_id, now, names, task.isCanceled,
                                                 problems)
                notes.append(found_text(found) if found else "新たに判定できた依頼なし")
                if problems:
                    notes.append("照合結果の保存に一部失敗（" + "、".join(problems[:3]) + "）")
                if changed:
                    requests = load_requests(client, store, token, task.isCanceled)
            elif follow:
                notes.append("画像カタログがないため照合なし")
            return requests, history, notes

        def done(result):
            self.task = None
            requests, history, notes = result
            self.requests = {r.id: r for r in requests}
            self.history = history
            self.loaded = (first, last)
            dock.requests_changed(requests)
            self.status.setText("。".join(notes))
            self.render()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("照合失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("計画と実績の照合"), work, done, failed)
        self._update_buttons()

    def _span_changed(self) -> None:
        """Shows the new span: from what was read when it is inside it, else read again (without following up)."""
        first, last = self.span()
        if self.loaded is not None and not (self.loaded[0] <= first and last <= self.loaded[1]) and self.task is None:
            self.run(follow=False)
            return
        self.render()

    def span(self) -> tuple[date, date]:
        a, b = self.first.date(), self.last.date()
        first, last = date(a.year(), a.month(), a.day()), date(b.year(), b.month(), b.day())
        return (first, last) if first <= last else (last, first)

    # --- The table ------------------------------------------------------------------------------

    def render(self) -> None:
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        first, last = self.span()
        handed = [r for r in in_days(self.history, first, last) if r.result == "採用"]
        shown = SHOWN[self.shown.currentIndex()] if self.shown.currentIndex() >= 0 else SHOWN[0]
        today = jst_day(_now_ms())
        self.rows = [r for r in reversed(handed) if shown == "すべて" or r.captured == shown]
        for r in self.rows:
            req = self.requests.get(r.request_id)
            items = [QStandardItem(t) for t in (r.day, r.system, r.satellite, r.request_id, req.name if req else "",
                                                 r.captured, r.image_id, next_step(r, req, today))]
            items[5].setForeground(QBrush(QColor(COLORS.get(r.captured, "#6b7280"))))
            self.model.appendRow(items)
        tallies = tally(handed)
        self.summary.setText("\n".join(f"{t.system}: {t.text}" for t in tallies) if tallies
                             else self.tr("この期間に出力した依頼なし"))
        self._update_buttons()

    def selected(self) -> list:
        picked = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.rows[i] for i in picked if 0 <= i < len(self.rows)]

    def _update_buttons(self) -> None:
        busy = self.task is not None
        self.run_button.setEnabled(not busy)
        any_ = bool(self.selected()) if self.rows else False
        self.taken_button.setEnabled(not busy and any_)
        self.missed_button.setEnabled(not busy and any_)

    # --- By hand --------------------------------------------------------------------------------

    def mark(self, captured: str, image_id: Optional[str] = None) -> None:
        """Sets the selected rows' outcome by hand (asking for the image id when made 撮像済み)."""
        rows = self.selected()
        findings = marked(rows, captured)
        if not findings or self.task is not None:
            return
        if captured == "撮像済み" and image_id is None:
            image_id, ok = QInputDialog.getText(self, self.tr("撮像済みにする"), self.tr("画像ID（分かれば）"))
            if not ok:
                return
        if image_id:
            findings = marked(rows, captured, image_id.strip())
        dock, store = self.dock, self.dock.store
        client, token, now = self.context.esri(), dock.token(), _now_ms()
        requests, history = self.requests, self.history

        def work(task):
            # Every row of those requests (not only the span's): whether another is still 撮像済み.
            rows = load_history(client, store, token, history_where(store, request_ids=[f.row.request_id for f in findings]),
                                cancelled=task.isCanceled)
            problems = []
            changed = save_marked(client, store, token, findings, requests, rows or history, now, problems)
            return (load_requests(client, store, token, task.isCanceled) if changed else None), problems

        def done(result):
            self.task = None
            result, problems = result
            if result is not None:
                self.requests = {r.id: r for r in result}
                dock.requests_changed(result)
            text = (self.tr("{n} 件を{c}に変更").format(n=len(findings), c=captured) if not problems
                    else self.tr("変更の一部を保存できず: {m}").format(m="、".join(problems[:3])))
            self.status.setText(text)
            self.render()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("変更の保存に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("照合結果の変更"), work, done, failed)
        self._update_buttons()
