"""
「範囲の変更記録」 (地域の形に合わせる, 組織の関心地域, tsudo 2026-11-24): what
MIKAGE did to the requests made from an area when the area's shape was
changed (更新・保留・対象外, and those not following it: 形が違う), as just
done after an area was saved in 組織の関心地域, or as logged in the change
table (`requestStores[].areaChanges`, TBL_REQUEST_AREA_CHANGE). A 更新, the
request's last change, can be undone: the request's area before comes back
and it no longer follows its area (core/request/area_follow.py).
"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.request.api import (
    AreaChange, FOLLOW_CHANGED, FOLLOW_HOLD, FOLLOW_SKIP, FOLLOW_UNDO, FOLLOW_UPDATE, FollowResult, load_changes, undo_change,
)
from ..core.request.model import JST
from ..qgis_io.tasks import run_task
from .docks import ask_no, plain_label, plain_tip
from .fit import fit_text

COLUMNS = ("日時", "結果", "依頼", "地域", "理由", "記録者")
_REASON = COLUMNS.index("理由")
COLORS = {FOLLOW_UPDATE: "#2563eb", FOLLOW_HOLD: "#b45309", FOLLOW_SKIP: "#6b7280", FOLLOW_UNDO: "#7c3aed", FOLLOW_CHANGED: "#6b7280"}


def _now_ms() -> float:
    return time.time() * 1000


def when_text(at: Optional[float]) -> str:
    return datetime.fromtimestamp(at / 1000, tz=JST).strftime("%Y-%m-%d %H:%M") if at else ""


def latest_updates(changes: list[AreaChange]) -> set[str]:
    """The change ids of the 更新 that are their request's last change (the newest first): those that can be undone."""
    seen: set[str] = set()
    out: set[str] = set()
    for c in changes:
        if c.request_id in seen:
            continue
        seen.add(c.request_id)
        if c.undoable:
            out.add(c.change_id)
    return out


def result_changes(result: FollowResult, at: Optional[float] = None, by: str = "") -> list[AreaChange]:
    """What a run did (at `at`, by `by`), as rows (those not logged too: 形が違う)."""
    rows = [replace(c, at=c.at or at, by=c.by or by) for c in result.changes]
    for f in result.changed:
        rows.append(AreaChange(request_id=f.request.id, name=f.request.name, area_id=f.area_id, action=FOLLOW_CHANGED,
                               reason=f.reason, at=at, by=by))
    return rows


class AreaChangeDialog(QDialog):
    """
    The changes of the requests' areas: those given (`changes`: just done),
    or else the log read from the change table. `dock`: the 「撮像依頼」 dock
    (its store, its requests, and its list read again after an undo).
    """

    def __init__(self, dock, changes: Optional[list] = None, title: str = "", text: str = "", parent=None):
        super().__init__(parent or dock)
        fit_text(self)
        self.dock = dock
        self.store = dock.store
        self.given = changes is not None
        self.changes: list[AreaChange] = list(changes or [])
        self.task = None
        #: The changes undone here (when the list is the one given: not read again).
        self.undone: set[str] = set()
        self.setWindowTitle(title or self.tr("範囲の変更記録"))
        layout = QVBoxLayout(self)
        self.text = plain_label(QLabel(text or self.tr(
            "地域の形が変わって、MIKAGE が依頼の範囲を合わせた記録（新しい順）。「更新」は、その依頼の最後の変更なら元に戻せる"
            "（元の範囲に戻し、地域の形に合わせるはオフ）")))
        self.text.setWordWrap(True)
        layout.addWidget(self.text)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("依頼の名称・地域・管理番号で絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.render())
        top.addWidget(self.search, 1)
        layout.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels([self.tr(c) for c in COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(_REASON, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda _i, _c: self.select_request())
        layout.addWidget(self.tree, 1)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.select_button = QPushButton(self.tr("依頼の一覧で選ぶ"))
        self.select_button.setToolTip(self.tr("この依頼を「撮像依頼」の一覧で選び、地図で表示"))
        self.select_button.clicked.connect(self.select_request)
        self.undo_button = QPushButton(self.tr("元に戻す…"))
        self.undo_button.setToolTip(self.tr("依頼の範囲を、地域の形に合わせる前に戻す（その依頼は地域の形に合わせるをオフ）"))
        self.undo_button.clicked.connect(self.undo)
        self.reload_button = QPushButton(self.tr("最新を取得"))
        self.reload_button.clicked.connect(self.load)
        self.reload_button.setVisible(bool(self.store and self.store.area_changes) and not self.given)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.reject)
        for b in (self.select_button, self.undo_button, self.reload_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.resize(860, 420)
        self.undo_button.setVisible(dock.allows("requests.edit"))
        if self.given:
            self.render()
        else:
            self.load()

    # ---- reading -------------------------------------------------------------------------------

    def load(self) -> None:
        store = self.store
        if store is None or not store.area_changes or self.task is not None:
            if store is not None and not store.area_changes:
                self.status.setText(self.tr("変更記録の表が未設定（管理者に連絡: requestStores[].areaChanges）"))
            return
        repo = self.dock.repository()
        self.status.setText(self.tr("変更記録を取得中…"))

        def work(task):
            return load_changes(repo.client, store, repo.token, cancelled=task.isCanceled)

        def done(changes):
            self.task = None
            self.given = False
            self.changes = changes
            self.render()
            self.status.setText(self.tr("{n} 件").format(n=len(changes)))

        def failed(message):
            self.task = None
            self.status.setText(self.tr("変更記録を取得できない: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("依頼の範囲の変更記録の取得"), work, done, failed)
        self._update_buttons()

    # ---- the list ------------------------------------------------------------------------------

    def shown(self) -> list[AreaChange]:
        words = self.search.text().strip().casefold()
        if not words:
            return list(self.changes)
        names = getattr(self.dock, "area_name", lambda i: i)
        return [c for c in self.changes if words in f"{c.name} {c.area_id} {names(c.area_id)} {c.request_id}".casefold()]

    def render(self) -> None:
        undoable = latest_updates(self.changes) if not self.given else {c.change_id for c in self.changes if c.undoable}
        undoable -= self.undone
        self.tree.clear()
        names = getattr(self.dock, "area_name", lambda i: i)
        for c in self.shown():
            action = c.action + (self.tr("（元に戻した）") if c.change_id in self.undone else "")
            item = QTreeWidgetItem([when_text(c.at), action, c.name or c.request_id, names(c.area_id) if c.area_id else "",
                                    c.reason, c.by])
            item.setData(0, Qt.ItemDataRole.UserRole, c)
            item.setForeground(1, QBrush(QColor(COLORS.get(c.action, "#111827"))))
            item.setToolTip(_REASON, plain_tip(c.reason))
            item.setToolTip(3, self.tr("管理番号 {id}").format(id=c.area_id) if c.area_id else "")
            item.setData(1, Qt.ItemDataRole.UserRole, c.change_id in undoable)
            self.tree.addTopLevelItem(item)
        self._update_buttons()

    def chosen(self) -> Optional[AreaChange]:
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if items else None

    def _undoable(self) -> bool:
        items = self.tree.selectedItems()
        return bool(items and items[0].data(1, Qt.ItemDataRole.UserRole))

    def _update_buttons(self) -> None:
        c = self.chosen()
        self.select_button.setEnabled(c is not None)
        self.undo_button.setEnabled(c is not None and self._undoable() and self.task is None and self.dock.task is None)
        self.reload_button.setEnabled(self.task is None)

    def select_request(self) -> None:
        c = self.chosen()
        if c is None:
            return
        dock = self.dock
        if not any(r.id == c.request_id for r in dock.shown):
            self.status.setText(self.tr("「{name}」は一覧にない（絞り込みを外すか、削除済み）").format(name=c.name or c.request_id))
            return
        dock.select([c.request_id])
        dock.chosen = next(r for r in dock.shown if r.id == c.request_id)
        dock.draw()
        dock.zoom()

    # ---- undoing -------------------------------------------------------------------------------

    def undo(self, ask: bool = True, then: Optional[Callable[[], None]] = None) -> bool:
        c = self.chosen()
        dock = self.dock
        if c is None or not self._undoable() or dock.denied("requests.edit"):
            return False
        request = next((r for r in dock.requests if r.id == c.request_id), None)
        if request is None:
            self.status.setText(self.tr("「{name}」は台帳になし（削除済み）").format(name=c.name or c.request_id))
            return False
        if ask and not ask_no(self, self.tr("元に戻す"), self.tr(
                "「{name}」の範囲を、地域の形に合わせる前に戻す。この依頼は地域の形に合わせるをオフにする"
                "（地域の形がまた変わっても、範囲はそのまま）").format(name=request.name), yes=self.tr("元に戻す")):
            return False
        repo = dock.repository()
        store = self.store
        self.status.setText(self.tr("元に戻し中…"))

        def work(task):
            return undo_change(repo.client, store, repo.token, c, request, _now_ms())

        def done(result):
            self.task = None
            saved, problems = result
            text = "。".join([self.tr("「{name}」の範囲を元に戻した（地域の形に合わせるはオフ）").format(name=saved.name), *problems])
            self.status.setText(text)
            dock.reload(lambda: (dock.select([saved.id]), then() if then else None))
            if self.given:
                self.undone.add(c.change_id)
                self.render()
            else:
                self.load()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("元に戻せない: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("依頼の範囲を元に戻す"), work, done, failed)
        self._update_buttons()
        return True
