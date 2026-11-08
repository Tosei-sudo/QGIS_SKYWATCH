"""
「未保存の記録」 (観測記録 dock): the records whose save did not go through,
kept on this computer (core/obs/drafts.py). Sent again, opened in the form
again (to correct them), or thrown away (asked first).
"""
from __future__ import annotations

from datetime import datetime

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .docks import ask_no


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def _time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m/%d %H:%M") if ms else ""


class DraftsDialog(QDialog):
    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.setWindowTitle(tr("未保存の記録"))
        self.resize(720, 380)
        layout = QVBoxLayout(self)
        note = QLabel(tr("保存できなかった記録です（この PC に保管）。つながったら「送り直す」。ほかの人が後で変えた記録は送らないので、「フォームで開く」で直してください。"))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("保管"), tr("画像"), tr("モノ"), tr("内容"), tr("理由")])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self.open_chosen())
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.send_button = QPushButton(tr("送り直す"))
        self.send_button.setToolTip(tr("選んだ記録（選んでいなければすべて）を保存先へ送る"))
        self.send_button.clicked.connect(lambda _on=False: self.send())
        self.open_button = QPushButton(tr("フォームで開く"))
        self.open_button.setToolTip(tr("選んだ記録をその画像のフォームで開いて直す（画像を開いておく）"))
        self.open_button.clicked.connect(lambda _on=False: self.open_chosen())
        self.drop_button = QPushButton(tr("捨てる…"))
        self.drop_button.clicked.connect(lambda _on=False: self.drop())
        for b in (self.send_button, self.open_button, self.drop_button):
            row.addWidget(b)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

    def render(self) -> None:
        book = self.dock.book
        drafts = book.drafts() if book is not None else []
        chosen = set(self.chosen_ids())
        self.tree.clear()
        for d in drafts:
            r = d.record
            what = tr("変更") if d.editing else tr("継続") if d.source_id else tr("新規")
            text = f"{what}: {r.kind}" + (f"（{r.activity}）" if r.activity else "") + ("" if r.present else tr("（不在）"))
            item = QTreeWidgetItem([_time(d.kept_at), r.image_name or r.image_id, r.object_name, text, d.reason])
            item.setData(0, Qt.ItemDataRole.UserRole, d.id)
            item.setToolTip(4, d.reason)
            self.tree.addTopLevelItem(item)
            item.setSelected(d.id in chosen)
        for i in range(4):
            self.tree.resizeColumnToContents(i)
        for b in (self.send_button, self.open_button, self.drop_button):
            b.setEnabled(bool(drafts))

    def chosen_ids(self) -> list[str]:
        return [i.data(0, Qt.ItemDataRole.UserRole) for i in self.tree.selectedItems()]

    def _all_ids(self) -> list[str]:
        return [self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) for i in range(self.tree.topLevelItemCount())]

    def send(self):
        return self.dock.send_drafts(self.chosen_ids() or self._all_ids())

    def open_chosen(self) -> bool:
        ids = self.chosen_ids()
        if len(ids) != 1:
            return False
        opened = self.dock.open_draft(ids[0])
        if opened:
            self.hide()
        return opened

    def drop(self, ask: bool = True) -> int:
        ids = self.chosen_ids()
        if not ids:
            return 0
        if ask and not ask_no(self, tr("未保存の記録を捨てる"), tr("選んだ {n} 件を捨てますか？保存先には送られず、元に戻せません。").format(n=len(ids))):
            return 0
        return self.dock.drop_drafts(ids)
