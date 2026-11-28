"""
「機能の使われ方…」 (管理者): how often each feature was used over the days
chosen, by the people of a role, least used first: the entries of the menu
(and toolbar, keys…) and the controls inside the docks, with the ones that
role has but nobody used at 0. Read from the settings file's `featureUse`
table (the server adds the rows up), or this PC's counts when there is none.
The tab 「迷い」: how long before each feature was chosen or used, how often it
was closed unused, the help opened from it and the error codes in it, the
most hesitated over first; 「ヘルプ」: the sections read and the searches that
found nothing.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Iterable

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
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
WAIT_COLUMNS = ("機能", "ドック", "迷い", "メニューで選ぶまで", "開いてから使うまで", "30 秒を超えた", "使わずに閉じた", "5 秒以内に閉じた",
                "ヘルプを開いた", "エラー", "人")
HELP_COLUMNS = ("種類", "節・検索語", "回数", "人")
#: The tabs.
USES, WAITS, HELP = 0, 1, 2


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class _Item(QTreeWidgetItem):
    """A row sorted by the value kept in a column (a number, a band's order), else by its text."""

    def __lt__(self, other) -> bool:  # noqa: D105
        column = self.treeWidget().sortColumn() if self.treeWidget() is not None else 0
        a, b = self.data(column, Qt.ItemDataRole.UserRole), other.data(column, Qt.ItemDataRole.UserRole)
        if a is not None or b is not None:
            return (a or 0) < (b or 0)
        return self.text(column) < other.text(column)


def _band_order(text: str) -> int:
    """A band's place in BANDS (0: none) for sorting."""
    return next((i for i, (_m, _n, t) in enumerate(fu.BANDS, 1) if t == text), 0)


def _tree(columns) -> QTreeWidget:
    tree = QTreeWidget()
    tree.setHeaderLabels([tr(c) for c in columns])
    tree.setRootIsDecorated(False)
    tree.setSortingEnabled(True)
    tree.setUniformRowHeights(True)
    return tree


def _row(values, numbers) -> _Item:
    item = _Item()
    for column, value in enumerate(values):
        if column in numbers:
            item.setText(column, str(value) if value else "")
            item.setData(column, Qt.ItemDataRole.UserRole, value)
            item.setTextAlignment(column, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
        else:
            item.setText(column, str(value))
    return item


class FeatureUseDialog(QDialog):
    def __init__(self, keeper, known: Callable[[str], Iterable[tuple[str, str]]], parent=None):
        super().__init__(parent)
        fit_text(self)
        self.keeper = keeper
        self.known = known
        self.summary = fu.UseSummary([], 0)
        self.waits = fu.WaitSummary([], [])
        self._task = None
        self.setWindowTitle(tr("機能の使われ方"))
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("メニュー・ツールバー・キーから選んだ機能と、ドックの中のボタン・タブ・リストを使った回数（日と担当ごとの合計）。"
                          "使った人の少ない順で、その担当で見えているのに使われていない機能は 0。"
                          "入力した文字（ヘルプで見つからなかった検索語のほか）や選んだ値、画像や記録の中身、利用者名は記録しない"))
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
        self.tabs = QTabWidget()
        self.tree = _tree(COLUMNS)
        self.tabs.addTab(self.tree, tr("使われ方"))
        waits = QWidget()
        box = QVBoxLayout(waits)
        box.setContentsMargins(0, 4, 0, 0)
        note = QLabel(tr("メニューで選ぶまで・ドックや画面を開いてから使う（OK する）までの時間の真ん中あたり（QGIS が手前にあった間だけ。"
                         "5 分を超えたものは席を外していたとみて除く）、使わずに閉じた回数、そこからヘルプを開いた回数、出たエラーコード。"
                         "迷いは 30 秒を超えた・使わずに閉じた・ヘルプ・エラーの合計で、多い順"))
        note.setWordWrap(True)
        box.addWidget(note)
        self.wait_tree = _tree(WAIT_COLUMNS)
        box.addWidget(self.wait_tree, 1)
        self.tabs.addTab(waits, tr("迷い"))
        self.help_tree = _tree(HELP_COLUMNS)
        header = self.help_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.help_tree, tr("ヘルプ"))
        self.tabs.currentChanged.connect(lambda _i: self._tab_changed())
        layout.addWidget(self.tabs, 1)
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
            self.waits = fu.summarize_waits(groups)
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

    def status_text(self) -> str:
        s, w = self.summary, self.waits
        tab = self.tabs.currentIndex()
        if tab == WAITS:
            text = tr("迷いのあった機能 {n} 件・30 秒を超えた {s} 回・使わずに閉じた {g} 回（5 秒以内 {q} 回）・ヘルプを開いた {h} 回・エラー {e} 回").format(
                n=sum(1 for line in w.lines if line.score), s=sum(line.slow for line in w.lines), g=sum(line.given_up for line in w.lines),
                q=sum(line.quick for line in w.lines), h=sum(line.help_from for line in w.lines), e=sum(line.error_count for line in w.lines))
        elif tab == HELP:
            text = tr("ヘルプの検索 {n} 回（見つからなかった {m} 回）・読まれた節 {r} 件").format(
                n=w.searches, m=w.misses, r=sum(1 for h in w.helps if h.kind == fu.HELP_PAGE))
        else:
            lines = self.lines()
            unused = sum(1 for line in lines if not line.users)
            text = tr("使った人 {p} 人・機能 {n} 件（だれも使っていない {u} 件）・使った回数 {c} 回").format(
                p=s.people, n=len(lines), u=unused, c=sum(line.count for line in lines))
        if s.capped:
            text += "\n" + tr("サーバーの上限で一部だけ（期間か担当を絞る）")
        return text

    def _tab_changed(self) -> None:
        self.kind.setEnabled(self.tabs.currentIndex() == USES)
        self.status.setText(self.status_text())
        self.export_button.setEnabled(self._has_lines())

    def _has_lines(self) -> bool:
        tab = self.tabs.currentIndex()
        if tab == USES:
            return bool(self.lines())
        return bool(self.waits.lines or self.waits.helps)

    def render(self) -> None:
        s = self.summary
        lines = self.lines()
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
        self._render_waits()
        self.status.setText(self.status_text())
        self.export_button.setEnabled(self._has_lines())

    def _render_waits(self) -> None:
        w = self.waits
        tree = self.wait_tree
        tree.setSortingEnabled(False)
        tree.clear()
        for line in w.lines:
            pick, first = fu.middle(line.pick), fu.middle(line.first)
            item = _row([line.feature, line.dock, line.score, pick, first, line.slow, line.given_up, line.quick, line.help_from,
                         fu.errors_text(line.errors), len(line.people)], {2, 5, 6, 7, 8, 10})
            item.setData(3, Qt.ItemDataRole.UserRole, _band_order(pick))
            item.setData(4, Qt.ItemDataRole.UserRole, _band_order(first))
            item.setData(9, Qt.ItemDataRole.UserRole, line.error_count)
            tree.addTopLevelItem(item)
        tree.setSortingEnabled(True)
        tree.sortItems(2, Qt.SortOrder.DescendingOrder)
        for column in range(len(WAIT_COLUMNS)):
            tree.resizeColumnToContents(column)
        tree.setColumnWidth(0, min(tree.columnWidth(0), 280))
        helps = self.help_tree
        helps.setSortingEnabled(False)
        helps.clear()
        for h in w.helps:
            kind = tr("見つからなかった検索") if h.kind == fu.HELP_MISS else tr("読んだ節")
            helps.addTopLevelItem(_row([kind, h.text, h.count, len(h.people)], {2, 3}))
        # The searches that found nothing first (the most people first within each kind: a stable sort keeps it).
        helps.setSortingEnabled(True)
        helps.sortItems(0, Qt.SortOrder.AscendingOrder)
        for column in (0, 2, 3):
            helps.resizeColumnToContents(column)

    def export(self) -> None:
        name = "feature_use.csv" if self.tabs.currentIndex() == USES else "feature_waits.csv"
        path, _f = QFileDialog.getSaveFileName(self, tr("機能の使われ方を保存"), name, tr("CSV (*.csv)"))
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv"
        if self.tabs.currentIndex() == USES:
            summary = fu.UseSummary(self.lines(), self.summary.people, self.summary.capped)
            text, count = fu.csv_text(summary), len(summary.lines)
        else:
            text, count = fu.wait_csv(self.waits), len(self.waits.lines) + len(self.waits.helps)
        problem = save_text(path, text, encoding="utf-8-sig", newline="", what="feature-use", count=count)
        self.status.setText(problem or tr("保存した: {p}").format(p=path))
