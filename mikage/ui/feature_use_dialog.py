"""
「機能の使われ方…」 (管理者): how often each feature was used over the days
chosen, by the people of a role, least used first: the entries of the menu
(and toolbar, keys…) and the controls inside the docks, with the ones that
role has but nobody used at 0. Read from the settings file's `featureUse`
table (the server adds the rows up), or this PC's counts when there is none.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Iterable

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core import feature_use as fu
from ..core import roles
from ..qgis_io.tasks import run_task
from .docks import plain_label, save_text
from .fit import fit_text

#: The days offered (0: every day kept).
PERIODS = ((30, "直近 30 日"), (7, "直近 7 日"), (90, "直近 90 日"), (365, "直近 1 年"), (0, "すべて"))
#: What is listed.
KINDS = (("", "すべて"), ("entries", "メニュー・ツールバー・キー"), ("docks", "ドックの中"))
COLUMNS = ("機能", "ドック", "回数", "使った人", "割合", "主にどこから", "表示した回数", "表示（分）")
#: The columns sorted as numbers.
NUMBERS = {2, 3, 4, 6, 7}
#: Features nobody used: grey.
UNUSED_COLOR = "#6b7280"


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class _Item(QTreeWidgetItem):
    """A row sorted by the numbers of its number columns (the text of the others)."""

    def __lt__(self, other) -> bool:  # noqa: D105
        column = self.treeWidget().sortColumn() if self.treeWidget() is not None else 0
        if column in NUMBERS:
            return (self.data(column, Qt.ItemDataRole.UserRole) or 0) < (other.data(column, Qt.ItemDataRole.UserRole) or 0)
        return self.text(column) < other.text(column)


class FeatureUseDialog(QDialog):
    def __init__(self, keeper, known: Callable[[str], Iterable[tuple[str, str]]], parent=None):
        super().__init__(parent)
        fit_text(self)
        self.keeper = keeper
        self.known = known
        self.summary = fu.UseSummary([], 0)
        self._task = None
        self.setWindowTitle(tr("機能の使われ方"))
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("メニュー・ツールバー・キーから選んだ機能と、ドックの中のボタン・タブ・リストを使った回数（日と担当ごとの合計）。"
                          "使った人の少ない順で、その担当で見えているのに使われていない機能は 0。"
                          "入力した文字や選んだ値、画像や記録の中身、利用者名は記録しない"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("期間")))
        self.period = QComboBox()
        for days, text in PERIODS:
            self.period.addItem(tr(text), days)
        row.addWidget(self.period)
        row.addWidget(QLabel(tr("担当")))
        self.role = QComboBox()
        self.role.addItem(tr("すべての人"), "")
        for r in roles.ROLES:
            self.role.addItem(tr(r.name), r.code)
        self.role.addItem(tr("担当を選んでいない人"), fu.NO_ROLE)
        row.addWidget(self.role)
        row.addWidget(QLabel(tr("表示")))
        self.kind = QComboBox()
        for key, text in KINDS:
            self.kind.addItem(tr(text), key)
        row.addWidget(self.kind)
        self.reload_button = QPushButton(tr("読み込み"))
        self.reload_button.clicked.connect(lambda _on=False: self.load())
        row.addWidget(self.reload_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr(c) for c in COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree, 1)
        self.source_label = plain_label(QLabel())
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        bottom = QHBoxLayout()
        self.export_button = QPushButton(tr("CSV に書き出し…"))
        self.export_button.clicked.connect(lambda _on=False: self.export())
        bottom.addWidget(self.export_button)
        bottom.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)
        self.period.activated.connect(lambda _i: self.load())
        self.role.activated.connect(lambda _i: self.load())
        self.kind.activated.connect(lambda _i: self.render())

    # ---- reading ---------------------------------------------------------------------------------------

    def where_from(self) -> str:
        k = self.keeper
        s = k.settings
        if not s.enabled:
            return tr("記録: なし（管理者の設定: featureUse が false）")
        lines = []
        if s.source is not None:
            lines.append(tr("読み込み元: 使われ方の表（全員分） {u}").format(u=s.source.url))
            sent = datetime.fromtimestamp(k.last_sent).strftime("%Y-%m-%d %H:%M") if k.last_sent else tr("この起動ではまだ")
            lines.append(tr("この PC の送信: {w}（{m:g} 分ごと）・未送信 {n} 行").format(w=sent, m=s.send_minutes, n=k.store.waiting()))
            if k.last_error:
                lines.append(tr("送信のエラー: {e}").format(e=k.last_error))
        else:
            lines.append(tr("読み込み元: この PC の記録だけ（設定ファイルに使われ方の表 featureUse がないため）"))
        return "\n".join(lines)

    def load(self) -> None:
        """The sums of the days and role chosen (in a task: the table may be far), this PC's counts saved first."""
        if self._task is not None:
            return
        self.reload_button.setEnabled(False)
        self.status.setText(tr("読み込み中…"))
        self.source_label.setText(self.where_from())
        first, last = fu.period(int(self.period.currentData() or 0))
        role = self.role.currentData() or ""
        k = self.keeper
        source = k.settings.source
        client = k.context.esri() if source is not None else None
        local = [] if source is not None else k.snapshot()
        store, user = k.store, k.user_key

        def work(_task):
            if source is not None:
                return fu.table_groups(client, source, first, last, role)
            return fu.local_groups(store.rows() + local, user, first, last, role), False

        def done(value) -> None:
            self._task = None
            groups, capped = value
            known = list(self.known(role))
            self.summary = fu.summarize(groups, known)
            self.summary.capped = capped
            self.reload_button.setEnabled(True)
            self.render()

        def failed(message: str) -> None:
            self._task = None
            self.reload_button.setEnabled(True)
            self.status.setText(tr("読み込めない: {m}").format(m=message))

        self._task = run_task(tr("機能の使われ方の読み込み"), work, done, failed)

    # ---- the list --------------------------------------------------------------------------------------

    def lines(self) -> list:
        kind = self.kind.currentData() or ""
        lines = self.summary.lines
        if kind == "entries":
            return [line for line in lines if not line.dock]
        if kind == "docks":
            return [line for line in lines if line.dock]
        return list(lines)

    def render(self) -> None:
        s = self.summary
        lines = self.lines()
        unused = sum(1 for line in lines if not line.users)
        text = tr("使った人 {p} 人・機能 {n} 件（だれも使っていない {u} 件）・使った回数 {c} 回").format(
            p=s.people, n=len(lines), u=unused, c=sum(line.count for line in lines))
        if s.capped:
            text += "\n" + tr("サーバーの上限で一部だけ（期間か担当を絞る）")
        self.status.setText(text)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for line in lines:
            share = line.users / s.people if s.people else 0.0
            values = [line.feature, line.dock, line.count, line.users, share, fu.PLACES.get(line.main_place, line.main_place),
                      line.shown, round(line.minutes)]
            item = _Item()
            for column, value in enumerate(values):
                if column == 4:
                    item.setText(column, f"{value:.0%}" if s.people else "")
                elif column in NUMBERS:
                    item.setText(column, str(value) if value or column in (2, 3) else "")
                else:
                    item.setText(column, str(value))
                if column in NUMBERS:
                    item.setData(column, Qt.ItemDataRole.UserRole, value)
                    item.setTextAlignment(column, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            if not line.users:
                for column in range(len(COLUMNS)):
                    item.setForeground(column, QBrush(QColor(UNUSED_COLOR)))
            self.tree.addTopLevelItem(item)
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(3, Qt.SortOrder.AscendingOrder)
        for column in range(len(COLUMNS)):
            self.tree.resizeColumnToContents(column)
        self.tree.setColumnWidth(0, min(self.tree.columnWidth(0), 320))
        self.export_button.setEnabled(bool(lines))

    def export(self) -> None:
        path, _f = QFileDialog.getSaveFileName(self, tr("機能の使われ方を保存"), "feature_use.csv", tr("CSV (*.csv)"))
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv"
        summary = fu.UseSummary(self.lines(), self.summary.people, self.summary.capped)
        problem = save_text(path, fu.csv_text(summary), encoding="utf-8-sig", newline="", what="feature-use", count=len(summary.lines))
        self.status.setText(problem or tr("保存した: {p}").format(p=path))
