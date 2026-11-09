"""
「開く画像の選択」: which of a catalog record's images (the files of its
folder that match `imagePatterns`) to open. Every image is chosen at first;
the filter narrows the list (a part of the path, case ignored). A double click
on a row opens that image alone and closes the window, whatever is checked.
"""
from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)


def _tr(text: str) -> str:
    return QCoreApplication.translate("ImageFilesDialog", text)


class ImageFilesDialog(QDialog):
    """The images of one record, each checkable."""

    def __init__(self, record_id: str, files: list[str], more: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._only: Optional[str] = None
        self.setWindowTitle(_tr("開く画像の選択"))
        self.resize(520, 420)
        layout = QVBoxLayout(self)
        count = _tr("{n} 件以上（先頭のみ）").format(n=len(files)) if more else _tr("{n} 件").format(n=len(files))
        layout.addWidget(QLabel(_tr("{id} のフォルダの画像 {count}").format(id=record_id or _tr("画像"), count=count)))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(_tr("絞り込み（パスの一部）"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._narrow)
        layout.addWidget(self.filter)
        self.list = QListWidget()
        for f in files:
            item = QListWidgetItem(f)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda _: self._count())
        self.list.itemDoubleClicked.connect(self._open_only)
        layout.addWidget(self.list, 1)
        hint = QLabel(_tr("ダブルクリックでその画像だけを開く"))
        hint.setEnabled(False)
        layout.addWidget(hint)
        row = QHBoxLayout()
        for text, state in ((_tr("表示中をすべて選択"), Qt.CheckState.Checked), (_tr("表示中をすべて解除"), Qt.CheckState.Unchecked)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, s=state: self._set_shown(s))
            row.addWidget(button)
        row.addStretch(1)
        self.counter = QLabel()
        row.addWidget(self.counter)
        layout.addLayout(row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(_tr("開く"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._count()

    def _items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count())]

    def _narrow(self, text: str) -> None:
        key = text.strip().casefold()
        for item in self._items():
            item.setHidden(bool(key) and key not in item.text().casefold())

    def _set_shown(self, state) -> None:
        for item in self._items():
            if not item.isHidden():
                item.setCheckState(state)

    def _count(self) -> None:
        n = len(self.chosen())
        self.counter.setText(_tr("{n} 件を選択").format(n=n))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(n > 0)

    def _open_only(self, item: QListWidgetItem) -> None:
        """The double-clicked image alone, whatever is checked; the window closes."""
        self._only = item.text()
        self.accept()

    def chosen(self) -> list[str]:
        if self._only is not None:
            return [self._only]
        return [item.text() for item in self._items() if item.checkState() == Qt.CheckState.Checked]


def choose_image_files(parent: Optional[QWidget], record, files: list[str], more: bool) -> Optional[list[str]]:
    """The images of a record the person chose to open; None when cancelled."""
    dialog = ImageFilesDialog(record.id, files, more, parent)
    return dialog.chosen() if dialog.exec() == QDialog.DialogCode.Accepted else None
