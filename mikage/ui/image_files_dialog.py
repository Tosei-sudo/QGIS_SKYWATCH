"""
「開く画像の選択」: which of a catalog record's images (the files of its
folder that match `imagePatterns`) to open. Every image is chosen at first;
the filter narrows the list (a part of the path, case ignored). A double click
on a row opens that image alone and closes the window, whatever is checked.
Each row shows the file's name first, then its path below the record's folder
(tsudo, 2026-11-16: 「1列目にファイル名、次に今のパス表示」).
"""
from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .docks import name_cancel
from .fit import fit_text


def _tr(text: str) -> str:
    return QCoreApplication.translate("ImageFilesDialog", text)


#: The columns: the file's name, then its path below the record's folder (as it was shown alone before).
NAME, PATH = 0, 1


def file_name(path: str) -> str:
    """The last part of a path below a record's folder (`PAN/a.tif` → `a.tif`)."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or path


class ImageFilesDialog(QDialog):
    """The images of one record, each checkable."""

    def __init__(self, record_id: str, files: list[str], more: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self._only: Optional[str] = None
        self.setWindowTitle(_tr("開く画像の選択"))
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        count = _tr("{n} 件以上（先頭のみ）").format(n=len(files)) if more else _tr("{n} 件").format(n=len(files))
        layout.addWidget(QLabel(_tr("{id} のフォルダの画像 {count}").format(id=record_id or _tr("画像"), count=count)))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(_tr("絞り込み（ファイル名・パスの一部）"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._narrow)
        layout.addWidget(self.filter)
        self.list = QTreeWidget()
        self.list.setColumnCount(2)
        self.list.setHeaderLabels([_tr("ファイル名"), _tr("パス")])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.list.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Interactive)
        for f in files:
            item = QTreeWidgetItem([file_name(f), f])
            item.setData(NAME, Qt.ItemDataRole.UserRole, f)
            item.setToolTip(NAME, f)
            item.setToolTip(PATH, f)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(NAME, Qt.CheckState.Checked)
            self.list.addTopLevelItem(item)
        # The names whole when the window has room for them (the paths take the rest; drag the line between to change).
        self.list.resizeColumnToContents(NAME)
        header.resizeSection(NAME, min(header.sectionSize(NAME) + 8, 360))
        self.list.itemChanged.connect(lambda *_: self._count())
        self.list.itemDoubleClicked.connect(lambda item, _column: self._open_only(item))
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
        name_cancel(self.buttons)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(_tr("開く"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._count()

    def _items(self) -> list[QTreeWidgetItem]:
        return [self.list.topLevelItem(i) for i in range(self.list.topLevelItemCount())]

    @staticmethod
    def path_of(item: QTreeWidgetItem) -> str:
        """The row's path below the record's folder (what is opened)."""
        return item.data(NAME, Qt.ItemDataRole.UserRole)

    def _narrow(self, text: str) -> None:
        key = text.strip().casefold()
        for item in self._items():
            item.setHidden(bool(key) and key not in self.path_of(item).casefold())

    def _set_shown(self, state) -> None:
        for item in self._items():
            if not item.isHidden():
                item.setCheckState(NAME, state)

    def _count(self) -> None:
        n = len(self.chosen())
        self.counter.setText(_tr("{n} 件を選択").format(n=n))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(n > 0)

    def _open_only(self, item: QTreeWidgetItem) -> None:
        """The double-clicked image alone, whatever is checked; the window closes."""
        self._only = self.path_of(item)
        self.accept()

    def chosen(self) -> list[str]:
        if self._only is not None:
            return [self._only]
        return [self.path_of(item) for item in self._items() if item.checkState(NAME) == Qt.CheckState.Checked]


def choose_image_files(parent: Optional[QWidget], record, files: list[str], more: bool) -> Optional[list[str]]:
    """The images of a record the person chose to open; None when cancelled."""
    dialog = ImageFilesDialog(record.id, files, more, parent)
    return dialog.chosen() if dialog.exec() == QDialog.DialogCode.Accepted else None
