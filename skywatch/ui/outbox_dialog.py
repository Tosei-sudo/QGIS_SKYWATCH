"""
「送信待ち」: everything kept on this computer that is not on the server yet,
in one list (tsudo, 2026-11-09: 「未保存の記録」 and the work kept offline were
one job kept twice). Each row says what it is, its store and why it waits:
a change made while the server was down (sent when it answers again), or an
observation record whose save did not go through (the network, the sign-in,
someone else's change). Rows are sent (the chosen ones, or every one that
goes without being chosen), an observation record opened in the form again
(to correct it over someone else's change), or thrown away (asked first).

The list belongs to the status bar's 「送信待ち」 (ui/offline_status.py); the
観測記録 dock opens the same one.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core.obs.drafts import KIND as DRAFT
from ..core.offline import CHECK, KINDS, Pending
from .docks import ask_no, plain_label, plain_tip
from .list_views import column_menus

#: Rows that need the person (someone else changed the record): orange.
CHECK_COLOR = "#b45309"


def tr(text: str) -> str:
    return QCoreApplication.translate("OfflineWork", text)


def _time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m/%d %H:%M") if ms else ""


def kind_text(p: Pending) -> str:
    return KINDS["observation"] if p.kind == DRAFT else KINDS.get(p.kind, p.kind)


def state_text(p: Pending) -> str:
    """Why an item waits, as the list says it."""
    if p.kind == DRAFT:
        head = tr("要確認") if p.op == CHECK else tr("保存できず")
        return f"{head}: {p.note}" if p.note else head
    head = tr("オフライン中の削除") if p.op == "remove" else tr("オフライン中の変更")
    return f"{head}（{tr('前回送れず')}: {p.note}）" if p.note else head


class OutboxDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle(tr("送信待ち"))
        self.resize(820, 400)
        layout = QVBoxLayout(self)
        note = QLabel(tr("サーバーにまだ送られていない変更です（この PC に保管）。オフライン中に作業したものと、通信やサインインの失敗・"
                         "ほかの人の変更で保存できなかった観測記録が並びます。「送信」は選んだもの（選んでいなければ、要確認以外のすべて）を送ります。"
                         "「要確認」はほかの人が後で変えた記録なので、「フォームで開く」で確かめて保存してください。"))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("保管"), tr("種類"), tr("保存先"), tr("内容"), tr("状態・理由")])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(lambda _item, _c: self.open_chosen())
        self.tree.itemSelectionChanged.connect(self._enable)
        layout.addWidget(self.tree, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.tree_columns = column_menus("outbox", self.tree, self)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.send_button = QPushButton(tr("送信"))
        self.send_button.setToolTip(tr("選んだもの（選んでいなければ要確認以外のすべて）をサーバーへ送る"))
        self.send_button.clicked.connect(lambda _on=False: self.send())
        self.open_button = QPushButton(tr("フォームで開く"))
        self.open_button.setToolTip(tr("選んだ観測記録（保存できなかったもの）を「観測記録」ドックのフォームで開いて直す（画像を開いておく）"))
        self.open_button.clicked.connect(lambda _on=False: self.open_chosen())
        self.drop_button = QPushButton(tr("捨てる…"))
        self.drop_button.setToolTip(tr("選んだものを送らずに消す（元に戻せない）"))
        self.drop_button.clicked.connect(lambda _on=False: self.drop())
        for b in (self.send_button, self.open_button, self.drop_button):
            row.addWidget(b)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

    # ---- the list ---------------------------------------------------------

    def items(self) -> list[Pending]:
        return self.controller.work.pending()

    def render(self) -> None:
        chosen = set(self.chosen_keys())
        self.tree.blockSignals(True)
        self.tree.clear()
        for p in self.items():
            item = QTreeWidgetItem([_time(p.at), kind_text(p), p.store, p.title or p.item_id, state_text(p)])
            item.setData(0, Qt.ItemDataRole.UserRole, (p.kind, p.store, p.item_id))
            item.setToolTip(4, plain_tip(state_text(p)))  # a server's message, a record's name
            if p.kind == DRAFT and p.op == CHECK:
                for c in range(5):
                    item.setForeground(c, QBrush(QColor(CHECK_COLOR)))
            self.tree.addTopLevelItem(item)
            item.setSelected((p.kind, p.store, p.item_id) in chosen)
        for i in range(4):
            self.tree.resizeColumnToContents(i)
        self.tree.blockSignals(False)
        self._enable()

    def _enable(self) -> None:
        busy = self.controller.uploading
        any_rows = self.tree.topLevelItemCount() > 0
        keys = self.chosen_keys()
        self.send_button.setEnabled(any_rows and not busy)
        self.open_button.setEnabled(len(keys) == 1 and keys[0][0] == DRAFT)
        self.drop_button.setEnabled(bool(keys) and not busy)

    def chosen_keys(self) -> list[tuple[str, str, str]]:
        return [tuple(i.data(0, Qt.ItemDataRole.UserRole)) for i in self.tree.selectedItems()]

    # ---- what is done -----------------------------------------------------

    def send(self) -> None:
        keys = self.chosen_keys()
        self.status.setText(tr("送信中…"))

        def done(report) -> None:
            try:
                self.status.setText(report.text())
                self.render()
            except RuntimeError:  # closed meanwhile
                pass

        self.controller.upload(then=done, only=set(keys) if keys else None)
        self._enable()

    def open_chosen(self) -> bool:
        keys = self.chosen_keys()
        if len(keys) != 1 or keys[0][0] != DRAFT:
            return False
        _kind, store, item_id = keys[0]
        opened = self.controller.open_draft(store, item_id)
        if opened:
            self.hide()
        return opened

    def drop(self, ask: bool = True) -> int:
        keys = self.chosen_keys()
        if not keys:
            return 0
        if ask and not ask_no(self, tr("送信待ちを捨てる"), tr("選んだ {n} 件を送らずに捨てますか？元に戻せません。").format(n=len(keys))):
            return 0
        n = self.controller.drop(keys)
        self.render()
        return n


__all__ = ["OutboxDialog", "state_text", "kind_text"]
