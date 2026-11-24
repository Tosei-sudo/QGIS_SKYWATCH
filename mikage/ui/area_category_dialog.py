"""
「地域のカテゴリー」 (tsudo, 2026-11-24: 「新しいテーブルとして、カテゴリーを
整理しよう」): the categories of the organisation's areas of interest
(TBL_AREA_CATEGORY, `namedAreas.categories`, core/named_areas.py) as a tree,
and their form. Everyone may edit them (tsudo 11:20Z).

- An area names its category by the category's ID (CATEGORY_ID), not by its
  name: a category renamed keeps its areas. Its ID changed, its areas are
  given the new ID as it is saved.
- A category no area has can be deleted; one in use is stopped (有効 off)
  instead: no longer offered, its areas keep it.
- 上へ・下へ move a category among those of the same parent (SORT_ORDER).
"""
from __future__ import annotations

from typing import Optional

from qgis.gui import QgsColorButton
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core.named_areas import AreaCategory, category_counts, category_order_changes, category_tree, check_category, descendants
from .docks import ask_no, plain_label, plain_tip
from .fit import fit_text

COLUMNS = ("名前", "ID", "英語名", "地域", "状態", "メモ")


class AreaCategoryForm(QDialog):
    """One category: its ID, name, English name, parent, colour, whether in use and note."""

    def __init__(self, categories: list[AreaCategory], category: Optional[AreaCategory] = None, count: int = 0,
                 parent_id: str = "", parent=None):
        super().__init__(parent)
        fit_text(self)
        self.categories = list(categories)
        self.original = category
        self.count = count
        self.setWindowTitle(self.tr("カテゴリーを作成") if category is None else self.tr("カテゴリーを編集"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.cid = QLineEdit(category.id if category else "")
        self.cid.setMaxLength(64)
        self.cid.setPlaceholderText(self.tr("PORT-NAVAL など（英数字と - _）"))
        self.cid.setToolTip(self.tr("地域が持つカテゴリーの値（CATEGORY_ID）。名前を変えても地域とのつながりは変わらない"))
        self.cid.textChanged.connect(lambda _t: self._note())
        form.addRow(self.tr("ID"), self.cid)
        self.name = QLineEdit(category.name if category else "")
        self.name.setPlaceholderText(self.tr("軍港、空港・飛行場 など"))
        form.addRow(self.tr("名前"), self.name)
        self.name_en = QLineEdit(category.name_en if category else "")
        form.addRow(self.tr("英語名"), self.name_en)
        self.parent_select = QComboBox()
        self.parent_select.addItem(self.tr("（いちばん上）"), "")
        below = descendants(self.categories, category.id) if category else set()
        for depth, c in category_tree(self.categories):
            if category is not None and (c.id.casefold() == category.id.casefold() or c.id.casefold() in below):
                continue
            self.parent_select.addItem("　" * depth + c.name, c.id)
        start = category.parent if category else parent_id
        self.parent_select.setCurrentIndex(max(0, self.parent_select.findData(start)))
        form.addRow(self.tr("上位"), self.parent_select)
        self.color = QgsColorButton()
        self.color.setAllowOpacity(False)
        self.color.setShowNoColor(True)
        self.color.setNoColorString(self.tr("なし（上位の色）"))
        if category and category.color and QColor(category.color).isValid():
            self.color.setColor(QColor(category.color))
        else:
            self.color.setToNoColor()
        self.color.setToolTip(self.tr("この地域の色（地域に色がないとき）"))
        form.addRow(self.tr("色"), self.color)
        self.active = QCheckBox(self.tr("有効（地域の入力で選べる）"))
        self.active.setChecked(category.active if category else True)
        form.addRow(self.tr("状態"), self.active)
        self.note = QLineEdit(category.note if category else "")
        self.note.setMaxLength(1000)
        form.addRow(self.tr("メモ"), self.note)
        self.renamed = plain_label(QLabel())
        self.renamed.setWordWrap(True)
        self.renamed.setStyleSheet("color: #b45309")
        layout.addWidget(self.renamed)
        self.problem = plain_label(QLabel())
        self.problem.setStyleSheet("color: #b91c1c")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("保存"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("取消"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._note()
        self.resize(520, self.sizeHint().height())

    def _note(self) -> None:
        o = self.original
        text = ""
        if o is not None and self.cid.text().strip() and self.cid.text().strip() != o.id and self.count:
            text = self.tr("保存すると、このカテゴリーの地域 {n} 件の CATEGORY_ID も「{new}」に変更").format(n=self.count, new=self.cid.text().strip())
        self.renamed.setText(text)
        self.renamed.setVisible(bool(text))

    def category(self) -> AreaCategory:
        o = self.original
        colour = self.color.color()
        return AreaCategory(id=self.cid.text().strip(), name=self.name.text().strip(), name_en=self.name_en.text().strip(),
                            parent=self.parent_select.currentData() or "", order=o.order if o else None,
                            color=colour.name() if colour.isValid() and colour.alpha() > 0 else "", active=self.active.isChecked(),
                            note=self.note.text().strip(), oid=o.oid if o else None)

    def _accept(self) -> None:
        problems = check_category(self.category(), self.categories, self.original)
        if problems:
            self.problem.setText("、".join(problems))
            return
        self.accept()


class AreaCategoryDialog(QDialog):
    """The categories as a tree, with 新規・編集・上へ・下へ・削除 (for everyone)."""

    def __init__(self, book, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.book = book
        self.busy = False
        self.note = ""
        self.form: Optional[AreaCategoryForm] = None
        self.setWindowTitle(self.tr("地域のカテゴリー"))
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels([self.tr(c) for c in COLUMNS])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda _i, _c: self.edit())
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.add_button = QPushButton(self.tr("新規…"))
        self.add_button.setToolTip(self.tr("カテゴリーを作成（選んでいるカテゴリーの下に）"))
        self.add_button.clicked.connect(lambda: self.add())
        self.edit_button = QPushButton(self.tr("編集…"))
        self.edit_button.clicked.connect(lambda: self.edit())
        self.up_button = QPushButton(self.tr("上へ"))
        self.up_button.setToolTip(self.tr("同じ上位の中で 1 つ上に"))
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button = QPushButton(self.tr("下へ"))
        self.down_button.setToolTip(self.tr("同じ上位の中で 1 つ下に"))
        self.down_button.clicked.connect(lambda: self.move(1))
        self.delete_button = QPushButton(self.tr("削除…"))
        self.delete_button.setToolTip(self.tr("地域が 1 件もないカテゴリーを削除（使わなくなったものは「編集…」で有効を外す）"))
        self.delete_button.clicked.connect(lambda: self.delete())
        for b in (self.add_button, self.edit_button, self.up_button, self.down_button, self.delete_button):
            row.addWidget(b)
        row.addStretch(1)
        self.reload_button = QPushButton(self.tr("最新を取得"))
        self.reload_button.clicked.connect(lambda: self.book.load())
        row.addWidget(self.reload_button)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)
        book.changed.connect(self.render)
        self.render()

    def counts(self) -> dict[str, int]:
        return category_counts(self.book.areas or [])

    def render(self) -> None:
        try:
            chosen = {c.id.casefold() for c in self.selected()}
        except RuntimeError:  # closed
            return
        counts = self.counts()
        self.tree.blockSignals(True)
        self.tree.clear()
        items: dict[str, QTreeWidgetItem] = {}
        grey = QBrush(QColor("#6b7280"))
        for _depth, c in category_tree(self.book.categories):
            n = counts.get(c.id.casefold(), 0)
            it = QTreeWidgetItem([c.name, c.id, c.name_en, str(n), self.tr("有効") if c.active else self.tr("停止"), c.note])
            it.setData(0, Qt.ItemDataRole.UserRole, c.id)
            if c.color and QColor(c.color).isValid():
                from .org_areas_panel import _swatch

                it.setIcon(0, _swatch(QColor(c.color)))
            if c.note:
                it.setToolTip(5, plain_tip(c.note))
            if not c.active:
                for col in range(len(COLUMNS)):
                    it.setForeground(col, grey)
            p = c.parent.casefold()
            (items[p].addChild(it) if p in items else self.tree.addTopLevelItem(it))
            items[c.id.casefold()] = it
            if c.id.casefold() in chosen:
                it.setSelected(True)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        s = self.book.settings
        if s is None or s.categories is None:
            text = self.tr("カテゴリーの表が未設定（管理者に連絡: namedAreas.categories）［E703］")
        elif self.book.loading:
            text = self.tr("取得中…")
        elif self.book.list is not None and self.book.list.category_failure:
            text = self.tr("取得に失敗: {m}").format(m=self.book.list.category_failure)
        else:
            cats = self.book.categories
            text = self.tr("カテゴリー {n} 件（有効 {m} 件）").format(n=len(cats), m=sum(1 for c in cats if c.active))
        self.status.setText("。".join(t for t in (self.note, text) if t))
        self.note = ""
        self._update_buttons()

    def selected(self) -> list[AreaCategory]:
        by = {c.id.casefold(): c for c in self.book.categories}
        return [by[k] for k in (str(i.data(0, Qt.ItemDataRole.UserRole)).casefold() for i in self.tree.selectedItems()) if k in by]

    def ready(self) -> bool:
        s = self.book.settings
        return s is not None and s.categories is not None and self.book.list is not None and self.book.list.categories is not None

    def _update_buttons(self) -> None:
        idle = not self.busy and not self.book.loading and self.ready()
        chosen = self.selected()
        one = len(chosen) == 1
        self.add_button.setEnabled(idle)
        self.edit_button.setEnabled(idle and one)
        cats = self.book.categories
        self.up_button.setEnabled(idle and one and bool(category_order_changes(cats, cats.index(chosen[0]), -1)) if one else False)
        self.down_button.setEnabled(idle and one and bool(category_order_changes(cats, cats.index(chosen[0]), 1)) if one else False)
        self.delete_button.setEnabled(idle and bool(chosen))
        self.reload_button.setEnabled(not self.book.loading)

    def _done(self, what: str):
        def done(*_a) -> None:
            self.busy = False
            self.note = what
            self.render()
        return done

    def _failed(self, message: str) -> None:
        self.busy = False
        self.status.setText(self.tr("保存に失敗: {m}").format(m=message))
        self._update_buttons()

    def add(self) -> Optional[AreaCategoryForm]:
        if not self.ready() or self.busy:
            return None
        chosen = self.selected()
        form = AreaCategoryForm(self.book.categories, None, 0, chosen[0].id if len(chosen) == 1 else "", self)
        self.form = form

        def save() -> None:
            c = form.category()
            siblings = [x.order for x in self.book.categories if x.parent.casefold() == c.parent.casefold() and x.order is not None]
            c.order = float(int(max(siblings, default=0.0)) + 1)
            self.busy = True
            self.book.save_category(c, None, lambda saved, _n: self._done(self.tr("「{name}」を作成").format(name=saved.name))(),
                                    self._failed)
            self._update_buttons()

        form.accepted.connect(save)
        form.open()
        return form

    def edit(self) -> Optional[AreaCategoryForm]:
        chosen = self.selected()
        if len(chosen) != 1 or not self.ready() or self.busy:
            return None
        before = chosen[0]
        form = AreaCategoryForm(self.book.categories, before, self.counts().get(before.id.casefold(), 0), "", self)
        self.form = form

        def save() -> None:
            self.busy = True

            def done(saved: AreaCategory, moved: int) -> None:
                what = self.tr("「{name}」を更新").format(name=saved.name)
                if moved:
                    what += self.tr("（地域 {n} 件のカテゴリーも変更）").format(n=moved)
                self._done(what)()

            self.book.save_category(form.category(), before, done, self._failed)
            self._update_buttons()

        form.accepted.connect(save)
        form.open()
        return form

    def move(self, step: int) -> None:
        chosen = self.selected()
        if len(chosen) != 1 or self.busy:
            return
        cats = self.book.categories
        changes = category_order_changes(cats, cats.index(chosen[0]), step)
        if not changes:
            return
        self.busy = True
        self.book.save_category_orders(changes, self._done(""), self._failed)
        self._update_buttons()

    def delete(self, ask: bool = True) -> None:
        chosen = self.selected()
        if not chosen or self.busy:
            return
        counts = self.counts()
        used = [c for c in chosen if counts.get(c.id.casefold())]
        if used:
            names = "、".join(f"{c.name}（{counts[c.id.casefold()]} 件）" for c in used[:5])
            self.status.setText(self.tr("地域があるカテゴリーは削除できない: {names}。使わなくなったものは「編集…」で「有効」を外す").format(names=names))
            return
        under = [c for c in self.book.categories if c.parent and c.parent.casefold() in {x.id.casefold() for x in chosen}
                 and c.id.casefold() not in {x.id.casefold() for x in chosen}]
        if under:
            self.status.setText(self.tr("下にカテゴリーがあるものは削除できない: {names}").format(
                names="、".join(c.name for c in under[:5])))
            return
        names = "、".join(c.name for c in chosen[:5])
        if ask and not ask_no(self, self.tr("カテゴリーの削除"), self.tr("{names} を削除（元に戻せない）").format(names=names), yes=self.tr("削除")):
            return
        self.busy = True

        def done(gone: list, problems: list) -> None:
            self._done("。".join(t for t in (self.tr("{n} 件を削除").format(n=len(gone)) if gone else "", "、".join(problems[:3])) if t))()

        self.book.delete_categories(chosen, done, self._failed)
        self._update_buttons()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.form is not None:
            try:
                self.form.close()
            except RuntimeError:
                pass
        super().closeEvent(event)
