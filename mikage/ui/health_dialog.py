"""
「接続と表の点検…」 (plugin menu): every ArcGIS service of the settings file
asked once (core/health.py), in a task; the results listed problems first,
to copy for whoever looks after the services.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core.health import NOTE, OK, PROBLEM, Result, check_all, report_text
from ..qgis_io.tasks import run_task
from .docks import plain_label
from .fit import fit_text

COLORS = {PROBLEM: "#b91c1c", NOTE: "#b45309", OK: "#15803d"}


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class HealthDialog(QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.context = context
        self.results: list[Result] = []
        self.checked_at = ""
        self._task = None
        self.setWindowTitle(tr("接続と表の点検"))
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("設定にある ArcGIS のサービスに 1 回ずつ問い合わせ、応答・サインイン・属性・編集できるかを確認（書き込みはしない）"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.summary = plain_label(QLabel())
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("結果"), tr("設定の場所"), tr("内容")])
        self.tree.setRootIsDecorated(True)
        self.tree.setWordWrap(True)
        # A fixed set of columns: not dragged about by their header (Qt lets a tree's be).
        self.tree.header().setSectionsMovable(False)
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.again_button = QPushButton(tr("もう一度点検"))
        self.again_button.clicked.connect(lambda _on=False: self.run())
        row.addWidget(self.again_button)
        self.copy_button = QPushButton(tr("結果をコピー"))
        self.copy_button.setToolTip(tr("結果を文章としてクリップボードへ（管理者に送るとき）"))
        self.copy_button.clicked.connect(lambda _on=False: self.copy())
        row.addWidget(self.copy_button)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)
        self._sign_in_item = None
        context.sign_in.changed.connect(self._show_sign_in)

    def _signed(self) -> list:
        """(level, line) of each `signIn` entry, as qgis_io/sign_in_io.py knows them now."""
        out = []
        for state in self.context.sign_in.states.values():
            level = PROBLEM if state.problem else NOTE if state.asking or (state.rule.method == "iwa" and state.user is None) else OK
            out.append((level, state.text()))
        return out

    def _show_sign_in(self) -> None:
        """The sign-in at the top: who the portals say is signed in, or why not."""
        if self._sign_in_item is not None:
            index = self.tree.indexOfTopLevelItem(self._sign_in_item)
            if index >= 0:
                self.tree.takeTopLevelItem(index)
            self._sign_in_item = None
        signed = self._signed()
        if not signed:
            return
        levels = {level for level, _ in signed}
        level = PROBLEM if PROBLEM in levels else NOTE if NOTE in levels else OK
        item = QTreeWidgetItem([level, tr("サインイン（signIn）"), ""])
        item.setForeground(0, QBrush(QColor(COLORS[level])))
        children = []
        for child_level, line in signed:
            child = QTreeWidgetItem([line])
            child.setForeground(0, QBrush(QColor(COLORS[child_level])))
            child.setToolTip(0, line)
            item.addChild(child)
            children.append(child)
        self.tree.insertTopLevelItem(0, item)
        for child in children:
            child.setFirstColumnSpanned(True)
        item.setExpanded(True)
        self._sign_in_item = item

    def done(self, result: int) -> None:
        try:
            self.context.sign_in.changed.disconnect(self._show_sign_in)
        except (TypeError, RuntimeError):
            pass
        super().done(result)

    def run(self) -> None:
        """Asks every service (in a task)."""
        if self._task is not None:
            return
        self.context.sign_in.check()
        config = self.context.config
        client = self.context.esri()
        self.tree.clear()
        self._sign_in_item = None
        self._show_sign_in()
        self.copy_button.setEnabled(False)
        self.again_button.setEnabled(False)
        self.summary.setText(tr("点検中…"))

        def finished(results) -> None:
            self._task = None
            self.again_button.setEnabled(True)
            self.show_results(results)

        def failed(message: str) -> None:
            self._task = None
            self.again_button.setEnabled(True)
            self.summary.setText(tr("点検に失敗: {m}").format(m=message))

        self._task = run_task(tr("接続と表の点検"), lambda task: check_all(client, config, task.isCanceled),
                              finished, failed)

    def show_results(self, results: list[Result]) -> None:
        self.results = results
        self.checked_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        problems = sum(1 for r in results if r.level == PROBLEM)
        notes = sum(1 for r in results if r.level == NOTE)
        if not results:
            self.summary.setText(tr("ArcGIS のサービスが未設定"))
        else:
            self.summary.setText(tr("{n} 件: 問題 {p}・注意 {w}・OK {ok}（{at}）").format(
                n=len(results), p=problems, w=notes, ok=len(results) - problems - notes, at=self.checked_at))
        self.tree.clear()
        self._sign_in_item = None
        self._show_sign_in()
        order = {PROBLEM: 0, NOTE: 1, OK: 2}
        for r in sorted(results, key=lambda r: order[r.level]):
            item = QTreeWidgetItem([r.level, r.target.name, r.target.url])
            item.setForeground(0, QBrush(QColor(COLORS[r.level])))
            item.setToolTip(2, r.target.url)
            children = []
            for f in r.findings:
                child = QTreeWidgetItem([f"{f.level}: {f.text}" if f.level != OK else f.text])
                child.setForeground(0, QBrush(QColor(COLORS[f.level])))
                child.setToolTip(0, f.text)
                item.addChild(child)
                children.append(child)
            self.tree.addTopLevelItem(item)
            for child in children:
                # The finding across the columns (long texts are not cut at the first column).
                child.setFirstColumnSpanned(True)
            item.setExpanded(r.level != OK)
        for i in range(2):
            self.tree.resizeColumnToContents(i)
        self.copy_button.setEnabled(bool(results))

    def text(self) -> str:
        return report_text(self.results, self.checked_at, self.context.source or "", [line for _level, line in self._signed()])

    def copy(self) -> Optional[str]:
        if not self.results:
            return None
        text = self.text()
        QApplication.clipboard().setText(text)
        self.summary.setText(self.summary.text().split("（コピー")[0] + tr("（コピー済み）"))
        return text
