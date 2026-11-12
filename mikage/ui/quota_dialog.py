"""
「年間撮像上限」: what each sensor's (or mode's) yearly quota has used and
has left (core/request/quota.py), and the shots of its year, one a row of a
system's file (the requests put together in it on the same row), and its
pace (「ペース」: the share of the limit used over the share of the year
gone, 100% on pace; 改修案 6). A shot
taken that the system does not count (noise, jamming …) is marked here
(「数えない」, on the shot layer: the image catalogs are not ours to write;
tsudo, 2026-11-11), with how many of its scenes and why.

The quotas themselves (limits, the month a year starts, what was used
before the plugin counted) are rows of the quotas table, edited in the
satellite editor (tools/satellite_editor.html) or ArcGIS.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSpinBox,
    QSplitter, QTableView, QVBoxLayout, QWidget,
)

from ..core.request.quota import (
    MISSED, NO_ROWS, PACE_AHEAD, PENDING, REJECTED, UNCOUNTED_REASONS, USED, QuotaData, Usage, read_quota_data,
)
from ..core.request.shot_view import _time
from ..core.request.store import RequestStoreConfig, update_shots
from ..qgis_io.tasks import run_task
from .docks import plain_label, plain_tip
from .list_views import column_menus

QUOTA_COLUMNS = ("センサー", "モード", "年間上限", "年の始まり", "期間", "当初", "撮像済み", "数えない", "見込み", "残り", "ペース", "メモ")
SHOT_COLUMNS = ("対象日", "撮像時刻", "システム", "衛星", "モード", "依頼", "枚数", "状態", "数えない", "理由", "画像ID")
#: How far back the years go (0: the year of the day chosen).
YEARS_BACK = 3
COLORS = {USED: "#16a34a", PENDING: "#2563eb", MISSED: "#6b7280", REJECTED: "#6b7280", NO_ROWS: "#6b7280"}


def _back(day: date, years: int) -> date:
    """The same day `years` years before (the 28th for a 29 February)."""
    return date(day.year - years, day.month, min(day.day, 28))


class UncountedDialog(QDialog):
    """How many scenes of a shot (or all of the shots chosen) the system does not count, and why."""

    def __init__(self, shots: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("数えない"))
        form = QFormLayout(self)
        self.count = QSpinBox()
        if len(shots) == 1:
            most = max(1, shots[0].scenes)
            self.count.setRange(1, most)
            self.count.setValue(shots[0].uncounted or most)
            self.count.setSuffix(self.tr(" 枚（{n} 枚のうち）").format(n=shots[0].scenes))
            form.addRow(self.tr("数えない枚数"), self.count)
        else:
            self.count.hide()
            form.addRow(self.tr("数えない枚数"), QLabel(self.tr("選んだ {n} 件の撮像の枚数すべて").format(n=len(shots))))
        self.reason = QComboBox()
        self.reason.setEditable(True)
        self.reason.addItems([self.tr(r) for r in UNCOUNTED_REASONS])
        old = next((s.shot.uncounted_reason for s in shots if s.shot.uncounted_reason), "")
        if old:
            self.reason.setCurrentText(old)
        self.reason.setToolTip(self.tr("システムが年間の枚数に数えない理由（選ぶか書く）"))
        form.addRow(self.tr("理由"), self.reason)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class QuotaDialog(QDialog):
    """The yearly quotas of a store and the shots of their years."""

    def __init__(self, context, store: RequestStoreConfig, day: date, parent=None):
        super().__init__(parent)
        self.context = context
        self.store = store
        self.day = day
        self.setWindowTitle(self.tr("年間撮像上限"))
        self.resize(1000, 600)
        self.task = None
        self.data: Optional[QuotaData] = None
        self.usages: list = []
        #: The shots the lower table shows (row → quota.ShotUse).
        self.shown: list = []
        #: What was just saved, said again once read again.
        self.note = ""
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("年")))
        self.year = QComboBox()
        for k in range(YEARS_BACK):
            self.year.addItem(self.tr("{d} を含む年").format(d=_back(self.day, k).isoformat()) if k else
                              self.tr("{d} を含む年（今年）").format(d=self.day.isoformat()), k)
        self.year.setToolTip(self.tr("センサー・モードごとの年の始まりの月で区切った 1 年"))
        self.year.currentIndexChanged.connect(lambda _=None: self.run())
        top.addWidget(self.year)
        self.reload_button = QPushButton(self.tr("読み直し"))
        self.reload_button.clicked.connect(lambda: self.run())
        top.addWidget(self.reload_button)
        top.addStretch(1)
        layout.addLayout(top)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        split = QSplitter(Qt.Orientation.Vertical)
        self.quota_model = QStandardItemModel(self)
        self.quota_table = self._table(self.quota_model, QAbstractItemView.SelectionMode.SingleSelection)
        self.quota_table.selectionModel().currentRowChanged.connect(lambda current, _: self.render_shots())
        split.addWidget(self.quota_table)
        lower = QWidget()
        box = QVBoxLayout(lower)
        box.setContentsMargins(0, 0, 0, 0)
        self.shot_title = plain_label(QLabel())
        box.addWidget(self.shot_title)
        self.shot_model = QStandardItemModel(self)
        self.shot_table = self._table(self.shot_model, QAbstractItemView.SelectionMode.ExtendedSelection)
        self.shot_table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        box.addWidget(self.shot_table, 1)
        split.addWidget(lower)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        self.quota_columns = column_menus("quotas", self.quota_table, self)
        self.shot_columns = column_menus("quota-shots", self.shot_table, self)
        hand = QHBoxLayout()
        self.uncount_button = QPushButton(self.tr("数えない…"))
        self.uncount_button.setToolTip(self.tr("選んだ撮像の枚数を年間の消費に数えない（ノイズ・ジャミングなどでシステムが数えないもの）。撮像の範囲に記録"))
        self.uncount_button.clicked.connect(self.uncount)
        self.count_button = QPushButton(self.tr("数えるに戻す"))
        self.count_button.setToolTip(self.tr("選んだ撮像の「数えない」を消す"))
        self.count_button.clicked.connect(self.count_again)
        for b in (self.uncount_button, self.count_button):
            hand.addWidget(b)
        hand.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        hand.addWidget(close)
        layout.addLayout(hand)
        self._update_buttons()

    def _table(self, model, mode) -> QTableView:
        t = QTableView()
        t.setModel(model)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(mode)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().hide()
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        return t

    def token(self) -> Optional[str]:
        return self.context.token_for(self.store.url, self.store.token)

    def chosen_day(self) -> date:
        return _back(self.day, int(self.year.currentData() or 0))

    # --- Reading ----------------------------------------------------------------------------------

    def run(self) -> None:
        """Reads the quotas and the shots of their years (the year chosen)."""
        if self.task is not None:
            return
        if not self.store.quotas:
            self.status.setText(self.tr("{s} に年間撮像上限のテーブル（quotas）なし").format(s=self.store.label))
            self.render()
            return
        store, client, token, day = self.store, self.context.esri(), self.token(), self.chosen_day()
        self.status.setText(self.tr("年間撮像上限と撮像の範囲を読み込み中…"))
        self.reload_button.setEnabled(False)

        def work(task):
            return read_quota_data(client, store, token, [day], task.isCanceled)

        def done(data):
            self.task = None
            self.reload_button.setEnabled(True)
            self.data = data
            self.usages = data.usages(day)
            text = self.tr("{n} 件の上限").format(n=len(data.quotas)) if data.quotas else self.tr("年間撮像上限のテーブルに行なし（どのセンサーも上限なし）")
            self.status.setText("。".join([t for t in (self.note, text, *data.problems) if t]))
            self.note = ""
            self.render()

        def failed(message):
            self.task = None
            self.reload_button.setEnabled(True)
            self.status.setText(self.tr("読み込み失敗: {m}").format(m=message))

        self.task = run_task(self.tr("年間撮像上限の読み込み"), work, done, failed)

    # --- Showing ----------------------------------------------------------------------------------

    def render(self) -> None:
        was = self.quota_table.currentIndex().row()
        self.quota_model.clear()
        self.quota_model.setHorizontalHeaderLabels([self.tr(c) for c in QUOTA_COLUMNS])
        day = self.chosen_day()
        for u in self.usages:
            q = u.quota
            left = u.remaining
            # ペース (改修案 6): the share of the limit used over the share of the year gone (100% on pace).
            pace = u.pace(min(day, u.window.last))
            cells = [q.sensor, q.mode or self.tr("（全モード）"), "" if q.limit is None else str(q.limit), self.tr("{m} 月から").format(m=q.start_month),
                     u.window.text, str(u.initial) if u.initial else "", str(u.used), str(u.uncounted) if u.uncounted else "",
                     str(u.pending) if u.pending else "", self.tr("上限なし") if left is None else str(left),
                     "" if pace is None else f"{round(pace * 100)}%", q.note]
            items = [QStandardItem(c) for c in cells]
            if left is not None and u.low():
                items[QUOTA_COLUMNS.index("残り")].setForeground(QBrush(QColor("#b91c1c" if left <= 0 else "#b45309")))
            if pace is not None:
                cell = items[QUOTA_COLUMNS.index("ペース")]
                cell.setToolTip(plain_tip(u.pace_text(min(day, u.window.last)) + self.tr(
                    "。{p}% を超えると、優先度「低」「最低」の依頼はほかのモードを先に試す").format(p=round(PACE_AHEAD * 100))))
                if u.ahead(min(day, u.window.last)):
                    cell.setForeground(QBrush(QColor("#b45309")))
            items[0].setToolTip(plain_tip(u.text()))
            if q.count_from is not None:
                items[QUOTA_COLUMNS.index("当初")].setToolTip(self.tr("{d} より前の消費枚数（{d} から撮像の範囲で数える）").format(d=q.count_from.isoformat()))
            self.quota_model.appendRow(items)
        if self.usages:
            self.quota_table.selectRow(min(max(was, 0), len(self.usages) - 1))
        self.render_shots()

    def chosen_usage(self) -> Optional[Usage]:
        row = self.quota_table.currentIndex().row()
        return self.usages[row] if 0 <= row < len(self.usages) else None

    def render_shots(self) -> None:
        self.shot_model.clear()
        self.shot_model.setHorizontalHeaderLabels([self.tr(c) for c in SHOT_COLUMNS])
        u = self.chosen_usage()
        self.shown = list(u.shots) if u else []
        self.shot_title.setText(u.text() if u else "")
        for use in self.shown:
            s = use.shot
            names = "、".join(s.names or s.request_ids)
            if s.count > 1:
                names = self.tr("まとめ {n} 件: {names}").format(n=s.count, names=names)
            state = use.state + (self.tr("（再計算中の日）") if use.replanned else "")
            cells = [s.day, _time(s.pass_time), s.system, s.satellite, s.mode, names, str(use.scenes), state,
                     str(use.uncounted) if use.uncounted else "", s.uncounted_reason, s.image_id]
            items = [QStandardItem(c) for c in cells]
            items[SHOT_COLUMNS.index("状態")].setForeground(QBrush(QColor(COLORS.get(use.state, "#6b7280"))))
            items[SHOT_COLUMNS.index("依頼")].setToolTip(plain_tip(names))
            self.shot_model.appendRow(items)
        self._update_buttons()

    def selected(self) -> list:
        rows = sorted({i.row() for i in self.shot_table.selectionModel().selectedRows()})
        return [self.shown[r] for r in rows if 0 <= r < len(self.shown) and self.shown[r].shot.object_id is not None]

    def _update_buttons(self) -> None:
        chosen = self.selected()
        idle = self.task is None
        self.uncount_button.setEnabled(idle and any(u.scenes for u in chosen))
        self.count_button.setEnabled(idle and any(u.shot.uncounted for u in chosen))

    # --- 数えない ----------------------------------------------------------------------------------

    def uncount(self) -> None:
        chosen = [u for u in self.selected() if u.scenes]
        if not chosen:
            return
        ask = UncountedDialog(chosen, self)
        if ask.exec() != QDialog.DialogCode.Accepted:
            return
        reason = ask.reason.currentText().strip() or None
        one = len(chosen) == 1
        self._save([(u.shot.object_id, {"uncounted": ask.count.value() if one else u.scenes, "uncountedReason": reason}) for u in chosen],
                   self.tr("{n} 件の撮像を数えないに").format(n=len(chosen)))

    def count_again(self) -> None:
        chosen = [u for u in self.selected() if u.shot.uncounted]
        if chosen:
            self._save([(u.shot.object_id, {"uncounted": None, "uncountedReason": None}) for u in chosen],
                       self.tr("{n} 件の撮像を数えるに戻した").format(n=len(chosen)))

    def _save(self, changes: list, what: str) -> None:
        if self.task is not None:
            return
        store, client, token = self.store, self.context.esri(), self.token()

        def work(task):
            return update_shots(client, store, token, changes)

        def done(saved):
            self.task = None
            text = what if saved == len(changes) else self.tr("{what}（{n}/{m} 件）").format(what=what, n=int(saved), m=len(changes))
            if saved.problems:
                text += "。" + "、".join(saved.problems[:3])
            self.note = text
            self.run()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("保存できず: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("撮像の範囲の更新"), work, done, failed)
        self._update_buttons()
