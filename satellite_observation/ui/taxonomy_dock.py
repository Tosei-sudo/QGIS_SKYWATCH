"""
「分類体系の編集」 (the viewer's taxonomy-editor.ts): the classification of
the observations' kinds as a tree to edit in place — drag a class onto
another to put it below, between two to put it before or after; rename it
where it stands (F2 or a double click), add a class below (＋, Insert),
delete one with its branch (two presses), Alt+↑↓ to reorder, search by name,
known individual or specification. The chosen class's own settings are
beside it: its branch's level names, what an individual is called, its
known individuals, its specifications (諸元; the fields are the user's,
under 「諸元の項目」) and reference photos (files kept in the local
GeoPackage, or URLs).

Every change goes through the 観測記録 dock's book, so renaming, moving or
deleting a class rewrites the kinds of the records under it, and is kept
where that store keeps its classification.
"""
from __future__ import annotations

import mimetypes
import os
from typing import Callable, Optional

from qgis.PyQt.QtCore import QByteArray, QTimer, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QKeySequence, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

try:  # Qt6
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QShortcut

from ..core.obs.stores import LOCAL_LABEL, LocalPhotoStore, is_local_photo, photo_url
from ..core.obs.taxonomy import (
    SpecField, Taxon, children_of, find, kind_of, level_name, name_label_of, path_of, photos_at, spec_text, specs_at, within,
)
from ..qgis_io.context import PluginContext
from .docks import PluginDock
from .flow_layout import FlowLayout

KIND_ROLE = Qt.ItemDataRole.UserRole
ADDING_ROLE = Qt.ItemDataRole.UserRole + 1
FIELD_TYPES = [("text", "文字"), ("number", "数値"), ("list", "複数（読点区切り）"), ("url", "URL")]


def _split(text: str) -> list[str]:
    import re

    return [w.strip() for w in re.split(r"[,、\n]", text) if w.strip()]


class ClassTree(QTreeWidget):
    """The tree; a drop calls `dropped(kind dragged, kind of the row, 'before' | 'after' | 'inside' | 'root')` instead of moving rows itself."""

    def __init__(self, dropped: Callable[[str, Optional[str], str], None], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._dropped = dropped
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        source = self.currentItem()
        try:
            pos = event.position().toPoint()
        except AttributeError:  # Qt5
            pos = event.pos()
        target = self.itemAt(pos)
        where = self.dropIndicatorPosition()
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if source is None:
            return
        kind = source.data(0, KIND_ROLE)
        if target is None or where == QAbstractItemView.DropIndicatorPosition.OnViewport:
            self._dropped(kind, None, "root")
            return
        zone = {QAbstractItemView.DropIndicatorPosition.AboveItem: "before", QAbstractItemView.DropIndicatorPosition.BelowItem: "after"}.get(where, "inside")
        self._dropped(kind, target.data(0, KIND_ROLE), zone)


class TaxonomyDock(PluginDock):
    """Editing the classification of the chosen observation store."""

    def __init__(self, iface, context: PluginContext, open_settings: Callable[[], None], observations, parent: Optional[QWidget] = None):
        super().__init__("SatObsTaxonomy", "分類体系の編集", context, open_settings, parent)
        self.iface = iface
        self.obs = observations
        self.selected: Optional[str] = None
        self.folded: set[str] = set()
        self._rendering = False
        self._armed: Optional[str] = None
        self._fields_draft: Optional[list[SpecField]] = None
        self._spec_inputs: dict[str, QLineEdit] = {}
        self._build()
        observations.changed.connect(self._changed)
        observations.said.connect(self._say)
        self.visibilityChanged.connect(lambda on: on and self.render())

    @property
    def book(self):
        return self.obs.book

    # ---- building --------------------------------------------------------

    def _button(self, text: str, tip: str, slot: Callable) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setAutoDefault(False)
        b.clicked.connect(lambda _on=False: slot())
        return b

    def _build(self) -> None:
        tr = self.tr
        bar = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(tr("分類・個体名の検索"))
        self.query.textChanged.connect(lambda _t: self.render())
        bar.addWidget(self.query, 1)
        bar.addWidget(self._button(tr("＋ 大分類"), tr("大分類の追加（「船舶 > 支援船」で階層ごと）"), lambda: self.start_add("")))
        bar.addWidget(self._button(tr("展開"), tr("すべて展開"), self._expand_all))
        bar.addWidget(self._button(tr("折りたたみ"), tr("すべて折りたたみ"), self._fold_all))
        self.body.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = ClassTree(self._dropped)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([tr("分類"), tr("階層"), tr("件数")])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.DoubleClicked)
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemExpanded.connect(lambda item: self.folded.discard(item.data(0, KIND_ROLE)))
        self.tree.itemCollapsed.connect(lambda item: self.folded.add(item.data(0, KIND_ROLE)))
        self.tree.itemDelegate().closeEditor.connect(self._editor_closed)
        split.addWidget(self.tree)
        for keys, slot in (("Alt+Up", lambda: self.reorder(-1)), ("Alt+Down", lambda: self.reorder(1)), ("Ins", self._add_below),
                           ("Del", self._delete_key)):
            s = QShortcut(QKeySequence(keys), self.tree)
            s.setContext(Qt.ShortcutContext.WidgetShortcut)
            s.activated.connect(slot)

        # The chosen class.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        detail = QWidget()
        self.detail = QVBoxLayout(detail)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.detail.addWidget(self.hint)
        self.form = QGroupBox()
        f = QFormLayout(self.form)
        self.levels_note = QLabel()
        self.levels_note.setWordWrap(True)
        f.addRow(self.levels_note)
        self.name_input = QLineEdit()
        self.parent_select = QComboBox()
        self.levels_input = QLineEdit()
        self.levels_input.setPlaceholderText("大分類、種別、級")
        self.levels_input.setToolTip(tr("大分類から順に、読点区切り。この分類より下の枝はここが優先"))
        self.label_input = QLineEdit()
        self.label_input.setPlaceholderText("艦名・機番")
        self.label_input.setToolTip(tr("最下位の個体（実名）の呼び方"))
        self.members_input = QPlainTextEdit()
        self.members_input.setToolTip(tr("この分類の既知の個体（実名）。記録時にモノ欄へ入力で分類も自動入力"))
        self.members_input.setFixedHeight(70)
        f.addRow(tr("名前"), self.name_input)
        f.addRow(tr("上位"), self.parent_select)
        f.addRow(tr("この枝の階層名"), self.levels_input)
        f.addRow(tr("個体名の呼称"), self.label_input)
        f.addRow(tr("既知の個体（1 行に 1 つ、読点区切りも可）"), self.members_input)
        self.specs_box = QGroupBox(tr("諸元"))
        self.specs_form = QFormLayout(self.specs_box)
        f.addRow(self.specs_box)
        self.photos_box = QGroupBox(tr("参考写真"))
        pl = QVBoxLayout(self.photos_box)
        self.photo_list = QWidget()
        self.photo_layout = FlowLayout(self.photo_list)
        pl.addWidget(self.photo_list)
        row = QHBoxLayout()
        self.photo_url = QLineEdit()
        self.photo_url.setPlaceholderText(tr("画像の URL（共有向け）"))
        self.photo_url.returnPressed.connect(self._add_photo_url)
        row.addWidget(self.photo_url, 1)
        row.addWidget(self._button(tr("URL 追加"), "", self._add_photo_url))
        row.addWidget(self._button(tr("ファイル追加"), tr("このコンピューターの画像（ローカルの GeoPackage のみに保存）"), self._add_photo_files))
        pl.addLayout(row)
        self.photo_note = QLabel()
        self.photo_note.setWordWrap(True)
        pl.addWidget(self.photo_note)
        f.addRow(self.photos_box)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("変更"), "", self.apply))
        actions.addWidget(self._button(tr("モノ一覧"), tr("この分類以下のモノを観測記録の一覧に表示"), self._things))
        actions.addWidget(self._button(tr("下位分類の追加"), tr("下位分類の追加（Insert）"), self._add_below))
        self.delete_button = self._button(tr("削除"), "", self._delete_key)
        actions.addWidget(self.delete_button)
        f.addRow(actions)
        for edit in (self.name_input, self.levels_input, self.label_input):
            edit.returnPressed.connect(self.apply)
        self.detail.addWidget(self.form)
        self.detail.addStretch(1)
        scroll.setWidget(detail)
        split.addWidget(scroll)
        self.body.addWidget(split, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        # 「諸元の項目」.
        self.fields_box = QGroupBox(tr("諸元の項目"))
        self.fields_box.setCheckable(True)
        self.fields_box.setChecked(False)
        fl = QVBoxLayout(self.fields_box)
        self.fields_table = QTableWidget(0, 4)
        self.fields_table.setHorizontalHeaderLabels([tr("項目"), tr("種類"), tr("単位"), ""])
        self.fields_table.verticalHeader().hide()
        self.fields_table.horizontalHeader().setStretchLastSection(True)
        fl.addWidget(self.fields_table)
        row = QHBoxLayout()
        row.addWidget(self._button(tr("項目の追加"), "", self._add_field))
        row.addWidget(self._button(tr("項目の保存"), "", self.save_fields))
        row.addWidget(self._button(tr("取消"), "", self._reset_fields))
        fl.addLayout(row)
        self.fields_box.toggled.connect(self._fields_toggled)
        self._fields_toggled(False)
        self.body.addWidget(self.fields_box)

        foot = QHBoxLayout()
        self.note = QLabel()
        foot.addWidget(self.note, 1)
        foot.addWidget(self._button(tr("JSON 保存"), tr("分類体系の JSON 保存（config.json の observationVocabulary.taxonomy に記載可）"),
                                    lambda: self.obs.save_taxonomy_file()))
        foot.addWidget(self._button(tr("JSON 読み込み"), tr("分類体系の JSON 読み込み（現在の分類と置き換え）"), lambda: self.obs.load_taxonomy_file()))
        self.body.addLayout(foot)

    # ---- what changes ----------------------------------------------------

    def _say(self, message: str) -> None:
        self.status.setText(message)

    def _changed(self) -> None:
        if self.isVisible() and self.tree.state() != QAbstractItemView.State.EditingState:
            self.render()

    def select(self, path: Optional[list]) -> None:
        """Chooses a class (None: none)."""
        self.selected = kind_of(path) if path else None
        self.render()
        item = self.tree.currentItem()
        if item is not None:
            self.tree.scrollToItem(item)
            self.tree.setFocus()

    def move(self, path: list[str], parent: list[str], before: Optional[list[str]]) -> None:
        """Moves a class below `parent` ([] the top), before a sibling (None: last); the records under it follow."""
        if parent and within(kind_of(parent), path):
            self._say(self.tr("自身の下位への移動は不可"))
            return
        self.selected = kind_of([*parent, path[-1]])
        self.obs.do(self.tr("分類の移動"), lambda b: b.update_class(path, parent=parent, before=before))

    def _dropped(self, kind: str, target: Optional[str], zone: str) -> None:
        if self.book is None or not kind:
            return
        path = path_of(kind)
        if zone == "root":
            self.move(path, [], None)
            return
        to = path_of(target or "")
        if within(target or "", path):
            self._say(self.tr("自身の下位への移動は不可"))
            return
        if zone == "inside":
            self.folded.discard(kind_of(to))
            self.move(path, to, None)
            return
        parent = to[:-1]
        if zone == "before":
            self.move(path, parent, to)
            return
        # After: before the next sibling other than itself, else last.
        siblings = [t for t in children_of(self.book.taxonomy(), parent) if kind_of(t.path) != kind]
        i = next((n for n, t in enumerate(siblings) if kind_of(t.path) == kind_of(to)), len(siblings))
        nxt = siblings[i + 1] if i + 1 < len(siblings) else None
        self.move(path, parent, list(nxt.path) if nxt else None)

    def reorder(self, by: int) -> None:
        """Alt+↑↓: the chosen class one place up or down among its siblings."""
        if self.book is None or not self.selected:
            return
        current = path_of(self.selected)
        parent = current[:-1]
        siblings = children_of(self.book.taxonomy(), parent)
        i = next((n for n, t in enumerate(siblings) if kind_of(t.path) == self.selected), -1)
        if by < 0 and i > 0:
            self.move(current, parent, list(siblings[i - 1].path))
        elif by > 0 and 0 <= i < len(siblings) - 1:
            after = siblings[i + 2] if i + 2 < len(siblings) else None
            self.move(current, parent, list(after.path) if after else None)

    def start_add(self, kind: str) -> None:
        """Starts adding a class below one ('' the top): a row to type its name in."""
        if self.book is None:
            return
        parent_item = self._item_of(kind) if kind else None
        if kind:
            self.folded.discard(kind)
        taxa = self.book.taxonomy()
        parent = path_of(kind)
        self._rendering = True
        item = QTreeWidgetItem()
        item.setData(0, ADDING_ROLE, kind)
        item.setData(0, KIND_ROLE, "")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setToolTip(0, f"{level_name(taxa, parent, len(parent)) or ('下位分類' if parent else '大分類')}の名前（Enter で追加、「A > B」で階層ごと）")
        if parent_item is not None:
            parent_item.addChild(item)
            parent_item.setExpanded(True)
        else:
            self.tree.addTopLevelItem(item)
        self._rendering = False
        self.tree.setCurrentItem(item)
        self.tree.editItem(item, 0)

    def start_rename(self, kind: Optional[str] = None) -> None:
        item = self._item_of(kind or self.selected or "")
        if item is not None:
            self.tree.editItem(item, 0)

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._rendering or column != 0 or self.book is None:
            return
        text = item.text(0).strip()
        adding = item.data(0, ADDING_ROLE)
        if adding is not None:
            names = path_of(text)
            if not names:
                return
            path = [*path_of(adding), *names]
            self.selected = kind_of(path)
            self.obs.do(self.tr("分類の追加"), lambda b: b.add_class(path))
            self.render()
            return
        kind = item.data(0, KIND_ROLE)
        path = path_of(kind)
        if text and text != path[-1]:
            self.selected = kind_of([*path[:-1], text])
            self.obs.do(self.tr("分類の変更"), lambda b: b.update_class(path, name=text))
        else:
            self.render()

    def _editor_closed(self, *_args) -> None:
        # A row for a new class left empty goes away.
        QTimer.singleShot(0, lambda: self.render() if self._has_empty_adder() else None)

    def _has_empty_adder(self) -> bool:
        def walk(item) -> bool:
            for i in range(item.childCount()):
                c = item.child(i)
                if c.data(0, ADDING_ROLE) is not None or walk(c):
                    return True
            return False

        return walk(self.tree.invisibleRootItem())

    def _add_below(self) -> None:
        if self.selected:
            self.start_add(self.selected)

    def _delete_key(self) -> None:
        """Two presses delete a class and its branch (the records under it keep the class above)."""
        if self.book is None or not self.selected:
            return
        path = path_of(self.selected)
        if self._armed != self.selected:
            self._armed = self.selected
            count = sum(1 for o in self.book.living() if within(o.kind, path))
            self.delete_button.setText(self.tr("削除?"))
            self._say(self.tr("もう一度で「{k}」を削除（記録 {n} 件の種別は上位の分類に）").format(k=self.selected, n=count))
            QTimer.singleShot(4000, self._disarm)
            return
        self._armed = None
        self.selected = kind_of(path[:-1]) or None
        self.obs.do(self.tr("分類の削除"), lambda b: b.remove_class(path))

    def _disarm(self) -> None:
        self._armed = None
        self.delete_button.setText(self.tr("削除"))

    def _things(self) -> None:
        if self.selected:
            self.obs.show()
            self.obs.raise_()
            self.obs.filter_class(path_of(self.selected))

    def apply(self) -> None:
        """「変更」: the chosen class's name, parent, levels, individuals and specifications."""
        if self.book is None or not self.selected:
            return
        path = path_of(self.selected)
        name = self.name_input.text().strip() or path[-1]
        parent = path_of(self.parent_select.currentData() or "")
        specs = {k: e.text().strip() for k, e in self._spec_inputs.items() if e.text().strip()}
        own = {"levels": _split(self.levels_input.text()), "name_label": self.label_input.text().strip(),
               "members": _split(self.members_input.toPlainText()), "specs": specs}
        self.selected = kind_of([*parent, name])
        self.obs.do(self.tr("分類の変更"), lambda b: b.update_class(path, name=name, parent=parent, **own))

    # ---- photos ------------------------------------------------------------

    def _photos(self) -> LocalPhotoStore:
        return LocalPhotoStore(self.context.gpkg())

    def _save_photos(self, photos: list[str], done: str) -> None:
        path = path_of(self.selected or "")
        if not path:
            return

        def after(to) -> None:
            if to:
                self._say(done)

        self.obs.do(self.tr("参考写真の保存"), lambda b: b.update_class(path, photos=photos), after)

    def _add_photo_url(self) -> None:
        value = self.photo_url.text().strip()
        t = self._taxon()
        if t is None:
            return
        if not value.lower().startswith(("http://", "https://")):
            self._say(self.tr("写真の URL は http(s):// で指定"))
            return
        self.photo_url.clear()
        self._save_photos([*(t.photos or []), value], self.tr("参考写真を追加"))

    def add_photo_files(self, paths: list[str]) -> None:
        t = self._taxon()
        if t is None:
            return
        refs = []
        for p in paths:
            mime = mimetypes.guess_type(p)[0] or ""
            if not mime.startswith("image/"):
                continue
            with open(p, "rb") as f:
                refs.append(self._photos().add(f.read(), os.path.basename(p), mime))
        if refs:
            self._save_photos([*(t.photos or []), *refs], self.tr("参考写真 {n} 枚を追加").format(n=len(refs)))

    def _add_photo_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, self.tr("参考写真の追加"), "", self.tr("画像 (*.png *.jpg *.jpeg *.gif *.webp *.bmp)"))
        if paths:
            self.add_photo_files(paths)

    def _remove_photo(self, ref: str) -> None:
        t = self._taxon()
        if t is None:
            return
        self._save_photos([r for r in (t.photos or []) if r != ref], self.tr("参考写真を削除"))
        self._photos().remove(ref)

    def _pixmap(self, ref: str) -> Optional[QPixmap]:
        """A photo kept locally or written in the classification (data:); URLs are not fetched (only opened when clicked)."""
        data = self._photos().get(ref) if is_local_photo(ref) else None
        if data is None and ref.startswith("data:image/"):
            import base64

            try:
                data = base64.b64decode(ref.split(",", 1)[1])
            except (IndexError, ValueError):
                data = None
        if data is None:
            return None
        pixmap = QPixmap()
        pixmap.loadFromData(QByteArray(data))
        return None if pixmap.isNull() else pixmap

    def _photo_widget(self, ref: str, title: str, own: bool) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        pixmap = self._pixmap(ref)
        thumb = QToolButton()
        if pixmap is not None:
            from qgis.PyQt.QtGui import QIcon

            thumb.setIcon(QIcon(pixmap))
            thumb.setIconSize(pixmap.scaled(96, 72, Qt.AspectRatioMode.KeepAspectRatio).size())
            thumb.clicked.connect(lambda _on=False: self._show_photo(pixmap, title))
        else:
            thumb.setText(self.tr("URL を開く"))
            thumb.clicked.connect(lambda _on=False: QDesktopServices.openUrl(QUrl(photo_url(ref) or ref)))
        thumb.setToolTip(title if pixmap is not None else f"{title}: {ref}")
        lay.addWidget(thumb)
        if own:
            remove = QToolButton()
            remove.setText("×")
            remove.setToolTip(self.tr("写真の削除"))
            remove.clicked.connect(lambda _on=False: self._remove_photo(ref))
            lay.addWidget(remove)
        return box

    def _show_photo(self, pixmap: QPixmap, title: str) -> None:
        from qgis.PyQt.QtWidgets import QDialog

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        lay = QVBoxLayout(dialog)
        label = QLabel()
        label.setPixmap(pixmap.scaled(900, 700, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(label)
        dialog.exec()

    # ---- 「諸元の項目」 ------------------------------------------------------

    def _fields_toggled(self, on: bool) -> None:
        for w in self.fields_box.findChildren(QWidget):
            w.setVisible(on)
        if on:
            self._render_fields()

    def _render_fields(self) -> None:
        if self.book is None:
            return
        if self._fields_draft is None:
            self._fields_draft = [SpecField(f.name, f.type, f.unit) for f in self.book.spec_fields]
        draft = self._fields_draft
        t = self.fields_table
        t.setRowCount(0)
        for i, f in enumerate(draft):
            t.insertRow(i)
            name = QLineEdit(f.name)
            name.editingFinished.connect(lambda f=f, name=name: setattr(f, "name", name.text().strip() or f.name))
            kind = QComboBox()
            for v, label in FIELD_TYPES:
                kind.addItem(label, v)
            kind.setCurrentIndex(max(0, kind.findData(f.type)))
            kind.currentIndexChanged.connect(lambda _i, f=f, kind=kind: setattr(f, "type", kind.currentData()))
            unit = QLineEdit(f.unit or "")
            unit.editingFinished.connect(lambda f=f, unit=unit: setattr(f, "unit", unit.text().strip() or None))
            tools = QWidget()
            tl = QHBoxLayout(tools)
            tl.setContentsMargins(0, 0, 0, 0)
            for text, tip, run in (("↑", self.tr("上へ"), lambda i=i: self._move_field(i, -1)), ("↓", self.tr("下へ"), lambda i=i: self._move_field(i, 1)),
                                   ("×", self.tr("項目の削除（入力済みの値は分類に残存）"), lambda i=i: self._drop_field(i))):
                b = QToolButton()
                b.setText(text)
                b.setToolTip(tip)
                b.clicked.connect(lambda _on=False, run=run: run())
                tl.addWidget(b)
            for c, w in enumerate((name, kind, unit, tools)):
                t.setCellWidget(i, c, w)

    def _read_fields(self) -> None:
        """What is typed but not yet left (no editingFinished)."""
        for i, f in enumerate(self._fields_draft or []):
            name, unit = self.fields_table.cellWidget(i, 0), self.fields_table.cellWidget(i, 2)
            if isinstance(name, QLineEdit):
                f.name = name.text().strip() or f.name
            if isinstance(unit, QLineEdit):
                f.unit = unit.text().strip() or None

    def _move_field(self, i: int, by: int) -> None:
        self._read_fields()
        d = self._fields_draft or []
        j = i + by
        if 0 <= j < len(d):
            d[i], d[j] = d[j], d[i]
        self._render_fields()

    def _drop_field(self, i: int) -> None:
        self._read_fields()
        if self._fields_draft and 0 <= i < len(self._fields_draft):
            del self._fields_draft[i]
        self._render_fields()

    def _add_field(self) -> None:
        self._read_fields()
        d = self._fields_draft if self._fields_draft is not None else []
        n = len(d) + 1
        while any(f.name == f"項目{n}" for f in d):
            n += 1
        d.append(SpecField(f"項目{n}", "text"))
        self._fields_draft = d
        self._render_fields()

    def save_fields(self) -> None:
        self._read_fields()
        seen: set[str] = set()
        fields = []
        for f in self._fields_draft or []:
            if f.name and f.name not in seen:
                seen.add(f.name)
                fields.append(f)
        self._fields_draft = None
        self.obs.do(self.tr("諸元の項目の保存"), lambda b: b.set_spec_fields(fields))

    def _reset_fields(self) -> None:
        self._fields_draft = None
        self._render_fields()

    # ---- showing -------------------------------------------------------------

    def _taxon(self) -> Optional[Taxon]:
        return find(self.book.taxonomy(), path_of(self.selected)) if self.book and self.selected else None

    def _item_of(self, kind: str) -> Optional[QTreeWidgetItem]:
        def walk(item) -> Optional[QTreeWidgetItem]:
            for i in range(item.childCount()):
                c = item.child(i)
                if c.data(0, KIND_ROLE) == kind:
                    return c
                found = walk(c)
                if found is not None:
                    return found
            return None

        return walk(self.tree.invisibleRootItem())

    def _expand_all(self) -> None:
        self.folded.clear()
        self.render()

    def _fold_all(self) -> None:
        if self.book:
            taxa = self.book.taxonomy()
            self.folded = {kind_of(t.path) for t in taxa if children_of(taxa, t.path)}
        self.render()

    def _current_changed(self, current, _previous) -> None:
        if self._rendering or current is None:
            return
        kind = current.data(0, KIND_ROLE)
        if kind and kind != self.selected:
            self.selected = kind
            self._render_detail()

    def render(self) -> None:
        """The tree and the chosen class."""
        book = self.book
        self._rendering = True
        try:
            self.tree.clear()
            if book is None:
                self.note.setText(self.tr("保存先なし"))
                return
            taxa = book.taxonomy()
            if self.selected and find(taxa, path_of(self.selected)) is None:
                self.selected = None
            self.note.setText(self.tr("保存先: {s}・{n} 分類").format(s=book.taxonomy_store.label, n=len(taxa)))
            flows = book.flows()
            records = book.living()
            q = self.query.text().strip().lower()
            matches = [t for t in taxa if any(q in v.lower() for v in [t.path[-1], *(t.members or []), *((t.specs or {}).values())])] if q else taxa
            shown = {kind_of(t.path) for t in taxa if any(within(kind_of(m.path), t.path) for m in matches)} if q else None
            current = None

            def walk(parent_item, parent: list[str]) -> None:
                nonlocal current
                for t in children_of(taxa, parent):
                    kind = kind_of(t.path)
                    if shown is not None and kind not in shown:
                        continue
                    depth = len(t.path) - 1
                    things = sum(1 for f in flows if within(f.kind, t.path))
                    count = sum(1 for o in records if within(o.kind, t.path))
                    counts = f"モノ {things}・記録 {count}" if things or count else ""
                    if t.members:
                        counts += f"{'・' if counts else ''}個体 {len(t.members)}"
                    item = QTreeWidgetItem([t.path[depth], level_name(taxa, t.path, depth), counts])
                    item.setData(0, KIND_ROLE, kind)
                    item.setToolTip(0, f"{kind}（ダブルクリック・F2 で名前の変更）")
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
                    if q and t in matches:
                        font = item.font(0)
                        font.setBold(True)
                        item.setFont(0, font)
                    if parent_item is None:
                        self.tree.addTopLevelItem(item)
                    else:
                        parent_item.addChild(item)
                    if kind == self.selected:
                        current = item
                    walk(item, list(t.path))
                    item.setExpanded(bool(q) or kind not in self.folded)

            walk(None, [])
            if not taxa or (q and not matches):
                empty = QTreeWidgetItem([self.tr("該当なし") if q else self.tr("分類なし（「＋ 大分類」から）")])
                empty.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.tree.addTopLevelItem(empty)
            if current is not None:
                self.tree.setCurrentItem(current)
        finally:
            self._rendering = False
        self._render_detail()
        if self.fields_box.isChecked():
            self._render_fields()

    def _render_detail(self) -> None:
        book = self.book
        t = self._taxon()
        self.form.setVisible(t is not None)
        if book is None:
            return
        if t is None:
            self.hint.setText(self.tr("分類の選択で詳細を表示（モノ {n}）。ドラッグで移動（行の間で前後、行の上で下位）、Alt+↑↓ で並べ替え、F2 で名前の変更、Insert で下位分類の追加").format(n=len(book.flows())))
            self.hint.show()
            return
        self.hint.hide()
        taxa = book.taxonomy()
        path = list(t.path)
        depth = len(path) - 1
        if self._armed != self.selected:
            self._disarm()
        levels = [level_name(taxa, path, i) or "—" for i in range(len(path))]
        i = len(path)
        while level_name(taxa, path, i):
            levels.append(level_name(taxa, path, i))
            i += 1
        label = name_label_of(taxa, path)
        self.form.setTitle(f"{kind_of(path)}（{level_name(taxa, path, depth) or f'第 {depth + 1} 階層'}）")
        self.levels_note.setText(f"階層: {' › '.join(levels)}{f' › {label}' if label else ''}")
        self.name_input.setText(path[-1])
        self.levels_input.setText("、".join(t.levels or []))
        self.label_input.setText(t.name_label or "")
        self.members_input.setPlainText("\n".join(t.members or []))
        self.parent_select.clear()
        self.parent_select.addItem(self.tr("（なし: 大分類）"), "")
        for x in taxa:
            if not within(kind_of(x.path), path):
                self.parent_select.addItem(kind_of(x.path), kind_of(x.path))
        self.parent_select.setCurrentIndex(max(0, self.parent_select.findData(kind_of(path[:-1]))))
        # Specifications: a field per row; what a class above says shows faintly until this class says otherwise.
        while self.specs_form.rowCount():
            self.specs_form.removeRow(0)
        inherited = specs_at(taxa, path)
        fields = list(book.spec_fields)
        extra = [k for k in (t.specs or {}) if not any(f.name == k for f in fields)]
        self._spec_inputs = {}
        for f in [*fields, *(SpecField(k, "text") for k in extra)]:
            box = QLineEdit((t.specs or {}).get(f.name, ""))
            above = inherited.get(f.name)
            if above and kind_of(above[1]) != kind_of(path):
                box.setPlaceholderText(f"{spec_text(f, above[0])}（{above[1][-1]}）")
                box.setToolTip(f"上位の分類「{kind_of(above[1])}」の値。入力でこの分類以下を上書き")
            elif f.type == "list":
                box.setPlaceholderText("読点区切り")
            box.returnPressed.connect(self.apply)
            self.specs_form.addRow(f"{f.name}（{f.unit}）" if f.unit else f.name, box)
            self._spec_inputs[f.name] = box
        if not self._spec_inputs:
            self.specs_form.addRow(QLabel(self.tr("項目なし（下の「諸元の項目」で追加）")))
        # Photos: its own (× takes one away), or those of a class above.
        self.photo_layout.clear()
        own = list(t.photos or [])
        above_photos, source = ([], []) if own else photos_at(taxa, path)
        for n, ref in enumerate(own or above_photos):
            where = kind_of(path if own else source)
            title = f"{where} の参考写真 {n + 1}{'（ローカルのみ）' if is_local_photo(ref) else ''}"
            self.photo_layout.addWidget(self._photo_widget(ref, title, bool(own)))
        shared = book.taxonomy_store.label != LOCAL_LABEL
        if above_photos:
            note = f"上位の分類「{kind_of(source)}」の写真（追加でこの分類のものに）"
        elif not own:
            note = "写真なし（クリックで拡大）"
        elif shared and any(is_local_photo(r) for r in own):
            note = "ファイルの写真はこの QGIS のみ（共有には URL）"
        else:
            note = "クリックで拡大"
        self.photo_note.setText(note)
