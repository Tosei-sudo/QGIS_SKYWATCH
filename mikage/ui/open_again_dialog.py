"""
「前回のファイルで開く」 (開いた画像の履歴, core/catalog/opened.py): an image
opened before opens again with the files of last time, after one look —
the catalog is not asked, its folder not looked through, the files not chosen
again (tsudo 2026-11-18: 確認の上開く; 確認画面は毎回出す).

The files found then are listed, those opened last time ticked; each says
whether it is still there. 「開く」 (Enter) opens the ticked ones,
「カタログで探し直す」 goes the usual way (the catalog asked by the image id).
"""
from __future__ import annotations

import time
from typing import Optional, Union

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from ..core.catalog.opened import OpenedImage
from .docks import plain_label
from .fit import fit_text

#: What 「カタログで探し直す」 answers.
FROM_CATALOG = "catalog"


def _when(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else ""


class OpenAgainDialog(QDialog):
    def __init__(self, entry: OpenedImage, parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.entry = entry
        self.answer: Union[None, str, list] = None
        self.setWindowTitle(self.tr("前回のファイルで開く"))
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        lead = plain_label(QLabel(self.tr("{id} を前回と同じファイルで開きます。").format(id=entry.image_id)))
        font = lead.font()
        font.setBold(True)
        lead.setFont(font)
        lead.setWordWrap(True)
        layout.addWidget(lead)
        info = QFormLayout()
        info.addRow(self.tr("カタログ"), plain_label(QLabel(entry.catalog_label or entry.catalog_url)))
        if entry.opened_at:
            info.addRow(self.tr("前回開いた日時"), QLabel(_when(entry.opened_at)))
        place = plain_label(QLabel(entry.place))
        place.setWordWrap(True)
        place.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info.addRow(self.tr("場所"), place)
        layout.addLayout(info)
        #: The files of a folder (ticked: opened last time); none for one file or a URL.
        self.files = QListWidget()
        missing = set(entry.missing(entry.files)) if entry.files else set()
        opened = set(entry.opened)
        for rel in entry.files:
            gone = rel in missing
            item = QListWidgetItem(f"{rel}    {self.tr('なし') if gone else self.tr('あり')}")
            item.setData(Qt.ItemDataRole.UserRole, rel)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if rel in opened and not gone else Qt.CheckState.Unchecked)
            if gone:
                item.setForeground(QColor("#b91c1c"))
                item.setToolTip(self.tr("前回の場所に見つかりません"))
            self.files.addItem(item)
        self.files.setVisible(bool(entry.files))
        self.files.itemChanged.connect(lambda _i: self._update())
        layout.addWidget(self.files, 1)
        self.note = plain_label(QLabel())
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox()
        self.open_button = buttons.addButton(self.tr("開く"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.catalog_button = QPushButton(self.tr("カタログで探し直す"))
        self.catalog_button.setToolTip(self.tr("前回のファイルを使わず、画像カタログを画像 ID で検索して開きます"))
        buttons.addButton(self.catalog_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.tr("やめる"), QDialogButtonBox.ButtonRole.RejectRole)
        self.open_button.setDefault(True)
        self.open_button.setAutoDefault(True)
        self.open_button.clicked.connect(self._open)
        self.catalog_button.clicked.connect(self._from_catalog)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update()

    def chosen(self) -> list[str]:
        """The files ticked (a folder's), [] for one file or a URL."""
        out = []
        for i in range(self.files.count()):
            item = self.files.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _update(self) -> None:
        e = self.entry
        if e.files:
            gone = e.missing(self.chosen())
            ok = bool(self.chosen()) and not gone
            self.note.setText(self.tr("チェックは前回開いたファイルです。Enter で開きます。") if ok else
                              self.tr("開くファイルにチェックを付けてください。") if not self.chosen() else
                              self.tr("チェックしたファイルが見つかりません。「カタログで探し直す」で探せます。"))
        else:
            gone = e.missing()
            ok = not gone
            self.note.setText(self.tr("Enter で開きます。") if ok else
                              self.tr("前回の場所に画像が見つかりません。「カタログで探し直す」で探せます。"))
        self.open_button.setEnabled(ok)
        if not ok:
            self.catalog_button.setDefault(True)

    def _open(self) -> None:
        self.answer = self.chosen()
        self.accept()

    def _from_catalog(self) -> None:
        self.answer = FROM_CATALOG
        self.accept()


def ask_open_again(parent: Optional[QWidget], entry: OpenedImage) -> Union[None, str, list]:
    """
    The files to open again (a folder's chosen files, [] for one file or a
    URL), FROM_CATALOG for 「カタログで探し直す」, None when not opened.
    """
    dialog = OpenAgainDialog(entry, parent)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.answer
    finally:
        dialog.deleteLater()
