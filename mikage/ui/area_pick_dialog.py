"""
「地域から…」 (組織の関心地域, 2026-11-24): an area of the named area layer
chosen by its name, よみがな, alias or 管理番号, for a request's area. The
organisation's areas of interest first, then the place names (any of them
can be a request's area); the active ones only.
"""
from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

from .docks import plain_label
from .fit import fit_text

#: The most rows listed (a place name layer may have 100,000s): the rest are found by typing.
MOST = 300
#: The rows offered: the areas of interest (関心地域・両方) only, or every row.
INTEREST, EVERY = "interest", "all"
COLUMNS = ("名前", "管理番号", "カテゴリー", "用途", "種類")


class AreaPickDialog(QDialog):
    """Chooses one area of the book (qgis_io/org_areas_io.OrgAreaBook): `chosen()` once accepted."""

    def __init__(self, book, parent=None, title: str = ""):
        super().__init__(parent)
        fit_text(self)
        self.book = book
        self.setWindowTitle(title or self.tr("地域から"))
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("名前・よみがな・別名・管理番号で絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.render())
        self.show_select = QComboBox()
        self.show_select.addItem(self.tr("すべて（地名も）"), EVERY)
        self.show_select.addItem(self.tr("組織の関心地域"), INTEREST)
        self.show_select.currentIndexChanged.connect(lambda _i: self.render())
        top.addWidget(self.search, 1)
        top.addWidget(self.show_select)
        layout.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels([self.tr(c) for c in COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._update)
        self.tree.itemDoubleClicked.connect(lambda _i, _c: self.accept() if self.chosen() is not None else None)
        layout.addWidget(self.tree, 1)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("この地域にする"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("閉じる"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._areas: dict[int, object] = {}
        self.resize(560, 420)
        book.changed.connect(self.render)
        self.finished.connect(self._closed)
        if book.areas is None:
            book.load()
        self.render()

    def _closed(self, _result=None) -> None:
        try:
            self.book.changed.disconnect(self.render)
        except (TypeError, RuntimeError):
            pass

    def render(self) -> None:
        areas = self.book.areas
        self.tree.clear()
        self._areas = {}
        if areas is None:
            failure = getattr(self.book, "failure", "")
            self.status.setText(self.tr("地名のレイヤーを取得できない: {m}").format(m=failure) if failure and not self.book.loading
                                else self.tr("地名のレイヤーを取得中…"))
            self._update()
            return
        names = {c.id: c.name for c in self.book.categories}
        text = self.search.text()
        interest_only = self.show_select.currentData() == INTEREST
        found = [a for a in areas if a.active and (a.is_interest or not interest_only) and a.matches(text)]
        # The areas of interest first (in the list's order), then the place names.
        found.sort(key=lambda a: not a.is_interest)
        for i, a in enumerate(found[:MOST]):
            item = QTreeWidgetItem([a.name, a.id or self.tr("（未付番）"), names.get(a.category, a.category), a.use, a.kind])
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            if a.kana or a.aliases:
                item.setToolTip(0, "・".join(n for n in (a.kana, *a.aliases) if n))
            self.tree.addTopLevelItem(item)
            self._areas[i] = a
        if len(found) > MOST:
            self.status.setText(self.tr("{n} 件のうち {m} 件を表示（名前などで絞り込み）").format(n=len(found), m=MOST))
        else:
            self.status.setText(self.tr("{n} 件").format(n=len(found)) if found else self.tr("当てはまる地域なし"))
        if len(found) == 1:
            self.tree.topLevelItem(0).setSelected(True)
        self._update()

    def chosen(self) -> Optional[object]:
        items = self.tree.selectedItems()
        return self._areas.get(items[0].data(0, Qt.ItemDataRole.UserRole)) if items else None

    def _update(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.chosen() is not None)
