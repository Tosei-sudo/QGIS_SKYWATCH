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

Known individuals (既知の個体) are rows of the tree under their class (◆):
chosen, their own form is beside it — name, class, other names (別名),
their own specifications (the class's show faintly until they say
otherwise), photos and a note (2026-11-27). Renamed with F2, dragged to
another class or place, deleted in two presses like a class.

Every change goes through the 観測記録 dock's book, so renaming, moving or
deleting a class rewrites the kinds of the records under it, and is kept
where that store keeps its classification.
"""
from __future__ import annotations

import re

import mimetypes
import os
from typing import Callable, Optional

from qgis.PyQt.QtCore import QByteArray, QPointF, QTimer, Qt, QUrl
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPainter, QPixmap, QPolygonF
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTableWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.obs.api import LOCAL_LABEL, LocalPhotoStore, is_local_photo, photo_url
from ..core.obs.api import (
    ALIAS_FIELD, SpecField, Taxon, activities_at, children_of, find, kind_of, level_name, member_names, member_photos_at, member_specs_at,
    name_label_of, path_of, photos_at, same_name, spec_text, specs_at, within,
)
from ..qgis_io.context import PluginContext
from . import shortcuts
from .docks import PluginDock, plain_tip
from .flow_layout import FlowLayout
from .fold import Fold

KIND_ROLE = Qt.ItemDataRole.UserRole
ADDING_ROLE = Qt.ItemDataRole.UserRole + 1
#: A known individual's row: its name (KIND_ROLE is its class's kind).
MEMBER_ROLE = Qt.ItemDataRole.UserRole + 2
FIELD_TYPES = [("text", "文字"), ("number", "数値"), ("list", "複数（読点区切り）"), ("url", "URL")]
FIELD_USES = [("both", "分類・個体"), ("class", "分類だけ"), ("member", "個体だけ")]
#: How known individuals' rows look apart from classes.
MEMBER_COLOR = "#0f766e"
#: A class with more known individuals than this is folded when first shown.
MEMBERS_UNFOLDED = 50


def member_icon() -> QIcon:
    """The ◆ before a known individual's row."""
    pix = QPixmap(14, 14)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(MEMBER_COLOR)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygonF([QPointF(7, 1), QPointF(13, 7), QPointF(7, 13), QPointF(1, 7)]))
    p.end()
    return QIcon(pix)


def _split(text: str) -> list[str]:
    import re

    return [w.strip() for w in re.split(r"[,、\n]", text) if w.strip()]


class ClassTree(QTreeWidget):
    """The tree; a drop calls `dropped(kind dragged, kind of the row, 'before' | 'after' | 'inside' | 'root', individual
    dragged or None, individual of the row or None)` instead of moving rows itself."""

    def __init__(self, dropped: Callable[..., None], parent: Optional[QWidget] = None):
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
        member = source.data(0, MEMBER_ROLE)
        if target is None or where == QAbstractItemView.DropIndicatorPosition.OnViewport:
            self._dropped(kind, None, "root", member, None)
            return
        zone = {QAbstractItemView.DropIndicatorPosition.AboveItem: "before", QAbstractItemView.DropIndicatorPosition.BelowItem: "after"}.get(where, "inside")
        self._dropped(kind, target.data(0, KIND_ROLE), zone, member, target.data(0, MEMBER_ROLE))


class TaxonomyDock(PluginDock):
    """Editing the classification of the chosen observation store."""

    def __init__(self, iface, context: PluginContext, open_settings: Callable[[], None], observations, parent: Optional[QWidget] = None):
        super().__init__("SatObsTaxonomy", "分類体系の編集", context, open_settings, parent)
        self.iface = iface
        self.obs = observations
        self.selected: Optional[str] = None
        #: The chosen known individual (of the class `selected`), else None.
        self.member: Optional[str] = None
        self.folded: set[str] = set()
        #: The classes with many individuals folded once already (unfolded since by the person, they stay so).
        self._many_seen: set[str] = set()
        self._member_spec_inputs: dict[str, QLineEdit] = {}
        self._member_armed: Optional[tuple[str, str]] = None
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
        bar.addWidget(self._button(tr("更新"), tr("最新の分類体系を取得（ほかの人の編集も）"), lambda: self.obs.reload_taxonomy()))
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
        for name, label, keys, slot in (("TaxonUp", "上へ", "Alt+Up", lambda: self.reorder(-1)),
                                        ("TaxonDown", "下へ", "Alt+Down", lambda: self.reorder(1)),
                                        ("TaxonAdd", "下に追加", "Ins", self._add_below), ("TaxonDelete", "削除", "Del", self._delete_key)):
            shortcuts.dock_key(self.tree, name, f"分類体系の編集（一覧内）: {label}", keys, slot, widget_only=True)

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
        members = QWidget()
        ml = QHBoxLayout(members)
        ml.setContentsMargins(0, 0, 0, 0)
        self.members_line = QLabel()
        self.members_line.setWordWrap(True)
        self.members_line.setToolTip(tr("この分類の既知の個体（実名）。記録時にモノ欄へ名前か別名を入力で分類も自動入力。"
                                        "別名・諸元はツリーで個体を選んで編集"))
        ml.addWidget(self.members_line, 1)
        self.add_members_button = self._button(tr("＋ 個体を追加…"), tr("既知の個体を追加（1 行に 1 つ、読点区切りも可）"), self.ask_members)
        ml.addWidget(self.add_members_button)
        f.addRow(tr("名前"), self.name_input)
        f.addRow(tr("上位"), self.parent_select)
        f.addRow(tr("この枝の階層名"), self.levels_input)
        f.addRow(tr("個体名の呼称"), self.label_input)
        self.members_label = QLabel(tr("既知の個体"))
        f.addRow(self.members_label, members)
        self.activities_input = QLineEdit()
        self.activities_input.setToolTip(tr("この分類以下で選べる活動、読点区切り（大分類に設定すれば大分類ごとに切り替え）。空なら上位の分類、なければ既定の活動"))
        f.addRow(tr("活動"), self.activities_input)
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
        actions.addWidget(self._button(tr("この画像で絞り込み"), tr("観測記録の一覧（この画像）を、この分類以下の記録だけに"), self._filter_obs))
        actions.addWidget(self._button(tr("下位分類の追加"), tr("下位分類の追加（Insert）"), self._add_below))
        self.delete_button = self._button(tr("削除"), "", self._delete_key)
        actions.addWidget(self.delete_button)
        f.addRow(actions)
        for edit in (self.name_input, self.levels_input, self.label_input, self.activities_input):
            edit.returnPressed.connect(self.apply)
        self.detail.addWidget(self.form)
        self._build_member_form()
        self.detail.addWidget(self.member_form)
        self.detail.addStretch(1)
        scroll.setWidget(detail)
        split.addWidget(scroll)
        self.body.addWidget(split, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        # 「諸元の項目」.
        self.fields_box = Fold(tr("諸元の項目"))
        fl = QVBoxLayout(self.fields_box.body)
        fl.setContentsMargins(0, 0, 0, 0)
        self.fields_table = QTableWidget(0, 6)
        self.fields_table.setHorizontalHeaderLabels([tr("項目"), tr("種類"), tr("単位"), tr("使う所"), tr("大分類"), ""])
        self.fields_table.horizontalHeaderItem(3).setToolTip(tr("分類・個体のどちらの欄に出すか"))
        self.fields_table.horizontalHeaderItem(4).setToolTip(tr("この大分類の分類・個体だけに出す（読点区切り。空: すべて）"))
        self.fields_table.verticalHeader().hide()
        self.fields_table.horizontalHeader().setStretchLastSection(True)
        fl.addWidget(self.fields_table)
        row = QHBoxLayout()
        row.addWidget(self._button(tr("項目の追加"), "", self._add_field))
        row.addWidget(self._button(tr("項目の保存"), "", self.save_fields))
        row.addWidget(self._button(tr("取消"), "", self._reset_fields))
        fl.addLayout(row)
        self.fields_box.opened.connect(self._fields_toggled)
        self.body.addWidget(self.fields_box)

        foot = QHBoxLayout()
        self.note = QLabel()
        self.note.setWordWrap(True)
        # The note gives way (wraps) before the buttons are squeezed.
        self.note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        foot.addWidget(self.note, 1)
        # 「取り込み ▾」「書き出し ▾」 (tsudo 2026-11-22: the table import and export beside the JSON ones).
        self.import_button = self._menu_button(tr("取り込み"), tr("分類体系の取り込み（表・JSON）"), [
            (tr("表から取り込み（CSV・Excel・レイヤー）…"), tr("大分類から階層ごとの列がある表を取り込む（試行してから保存）"),
             lambda: self.obs.open_taxonomy_import()),
            (tr("JSON から読み込み（今の分類体系と置き換え）…"), tr("分類体系を JSON から読み込み（今の分類と置き換え）"), lambda: self.obs.load_taxonomy_file()),
        ])
        self.export_button = self._menu_button(tr("書き出し"), tr("分類体系の書き出し（表・JSON）"), [
            (tr("表に書き出し（CSV。取り込みのひな形にも）…"), tr("1 行 1 分類、階層ごとの列の CSV（Excel で直して取り込める）"),
             lambda: self.obs.save_taxonomy_table_file()),
            (tr("JSON に保存（設定ファイルに書ける形）…"), tr("分類体系の JSON 保存（config.json の observationVocabulary.taxonomy に記載可）"),
             lambda: self.obs.save_taxonomy_file()),
        ])
        foot.addWidget(self.import_button)
        foot.addWidget(self.export_button)
        self.body.addLayout(foot)

    def _menu_button(self, text: str, tip: str, entries: list) -> QToolButton:
        """A button whose menu has the entries (text, tip, slot)."""
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        menu = QMenu(b)
        menu.setToolTipsVisible(True)
        for label, hint, slot in entries:
            action = menu.addAction(label)
            action.setToolTip(hint)
            action.triggered.connect(lambda _on=False, slot=slot: slot())
        b.setMenu(menu)
        return b

    def _build_member_form(self) -> None:
        """The chosen known individual's form: name, class, other names, its own specifications, photos and note."""
        tr = self.tr
        self.member_form = QGroupBox()
        f = QFormLayout(self.member_form)
        self.member_note = QLabel()
        self.member_note.setWordWrap(True)
        self.member_note.setStyleSheet("color: palette(placeholder-text);")
        f.addRow(self.member_note)
        self.member_name_input = QLineEdit()
        self.member_name_input.setToolTip(tr("正式な名前（記録のモノの名前に入る）。変えると、古い名前を使った記録があれば古い名前を別名に残す"))
        f.addRow(tr("名前"), self.member_name_input)
        self.member_class_select = QComboBox()
        self.member_class_select.setToolTip(tr("この個体の分類（変えると移動。記録の種別はそのまま）"))
        f.addRow(tr("分類"), self.member_class_select)
        self.member_alias_input = QLineEdit()
        self.member_alias_input.setPlaceholderText(tr("艦番号・英名・旧名など"))
        self.member_alias_input.setToolTip(tr("ほかの名前（読点区切り）。観測記録のモノ欄にこの名前で入れても、この個体として分かる"))
        f.addRow(tr("別名（読点区切り）"), self.member_alias_input)
        self.member_specs_box = QGroupBox(tr("諸元（空欄は分類の値）"))
        self.member_specs_form = QFormLayout(self.member_specs_box)
        f.addRow(self.member_specs_box)
        self.member_photos_box = QGroupBox(tr("参考写真"))
        pl = QVBoxLayout(self.member_photos_box)
        self.member_photo_list = QWidget()
        self.member_photo_layout = FlowLayout(self.member_photo_list)
        pl.addWidget(self.member_photo_list)
        row = QHBoxLayout()
        self.member_photo_url = QLineEdit()
        self.member_photo_url.setPlaceholderText(tr("画像の URL（共有向け）"))
        self.member_photo_url.returnPressed.connect(self._add_photo_url)
        row.addWidget(self.member_photo_url, 1)
        row.addWidget(self._button(tr("URL 追加"), "", self._add_photo_url))
        row.addWidget(self._button(tr("ファイル追加"), tr("このコンピューターの画像（ローカルの GeoPackage のみに保存）"), self._add_photo_files))
        pl.addLayout(row)
        self.member_photo_note = QLabel()
        self.member_photo_note.setWordWrap(True)
        pl.addWidget(self.member_photo_note)
        f.addRow(self.member_photos_box)
        self.member_note_input = QPlainTextEdit()
        self.member_note_input.setFixedHeight(52)
        self.member_note_input.setToolTip(tr("この個体の備考（識別のメモなど）"))
        f.addRow(tr("備考"), self.member_note_input)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("変更"), "", self.apply_member))
        actions.addWidget(self._button(tr("個体を追加…"), tr("この個体の分類に既知の個体を追加"), self.ask_members))
        self.member_delete_button = self._button(tr("削除"), "", self._delete_key)
        actions.addWidget(self.member_delete_button)
        actions.addStretch(1)
        f.addRow(actions)
        for edit in (self.member_name_input, self.member_alias_input):
            edit.returnPressed.connect(self.apply_member)
        self.member_form.hide()

    # ---- what changes ----------------------------------------------------

    def _say(self, message: str) -> None:
        self.status.setText(message)

    def _changed(self) -> None:
        if self.isVisible() and self.tree.state() != QAbstractItemView.State.EditingState:
            self.render()

    def select(self, path: Optional[list], member: Optional[str] = None) -> None:
        """Chooses a class (None: none), or a known individual of it."""
        self.selected = kind_of(path) if path else None
        self.member = member if path else None
        if path and member:
            self.folded.discard(kind_of(path))
        self.render()
        item = self.tree.currentItem()
        if item is not None:
            self.tree.scrollToItem(item)
            self.tree.setFocus()

    def move(self, path: list[str], parent: list[str], before: Optional[list[str]]) -> None:
        """Moves a class below `parent` ([] the top), before a sibling (None: last); the records under it follow."""
        if parent and within(kind_of(parent), path):
            self._say(self.tr("自分の下位には移動できない"))
            return
        self.selected = kind_of([*parent, path[-1]])
        self.member = None
        self.obs.do(self.tr("分類の移動"), lambda b: b.update_class(path, parent=parent, before=before))

    def move_member(self, path: list[str], name: str, dst: list[str], before: Optional[str] = None) -> None:
        """Moves a known individual to a class (or another place in its own), before one of its individuals (None: last)."""
        self.selected, self.member = kind_of(dst), name
        self.folded.discard(kind_of(dst))
        self.obs.do(self.tr("個体の移動"), lambda b: b.move_member(path, name, dst, before))

    def _dropped(self, kind: str, target: Optional[str], zone: str, member: Optional[str] = None,
                 target_member: Optional[str] = None) -> None:
        if self.book is None or not kind:
            return
        path = path_of(kind)
        if member:
            # An individual: onto a class, into it (last); before or after another individual, there.
            if not target:
                return
            to = path_of(target)
            if not target_member or zone == "inside":
                self.move_member(path, member, to)
                return
            if zone == "before":
                self.move_member(path, member, to, target_member)
                return
            t = find(self.book.taxonomy(), to)
            others = [m for m in ((t.members if t else None) or []) if not (kind_of(to) == kind and m == member)]
            i = others.index(target_member) if target_member in others else len(others)
            self.move_member(path, member, to, others[i + 1] if i + 1 < len(others) else None)
            return
        if target_member:
            self._say(self.tr("分類は個体の下には置けない"))
            return
        if zone == "root":
            self.move(path, [], None)
            return
        to = path_of(target or "")
        if within(target or "", path):
            self._say(self.tr("自分の下位には移動できない"))
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
        """Alt+↑↓: the chosen class one place up or down among its siblings (a known individual, among its class's)."""
        if self.book is None or not self.selected:
            return
        if self.member:
            path = path_of(self.selected)
            names = list((self._taxon().members if self._taxon() else None) or [])
            i = names.index(self.member) if self.member in names else -1
            if by < 0 and i > 0:
                self.move_member(path, self.member, path, names[i - 1])
            elif by > 0 and 0 <= i < len(names) - 1:
                self.move_member(path, self.member, path, names[i + 2] if i + 2 < len(names) else None)
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
        member = item.data(0, MEMBER_ROLE)
        if member:
            if text and text != member:
                self.selected, self.member = kind, text
                self.obs.do(self.tr("個体の変更"), lambda b: b.update_member(path, member, new_name=text))
            else:
                self.render()
            return
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
        if self.member:
            self.ask_members()
        elif self.selected:
            self.start_add(self.selected)

    def ask_members(self) -> None:
        """「＋ 個体を追加…」: names typed (one a line, or 読点区切り) added to the chosen class."""
        t = self._taxon()
        if t is None:
            return
        label = name_label_of(self.book.taxonomy(), list(t.path)) or self.tr("個体名")
        text, ok = QInputDialog.getMultiLineText(self, self.tr("個体の追加"),
                                                 self.tr("「{k}」に追加する{l}（1 行に 1 つ、読点区切りも可）").format(k=kind_of(t.path), l=label))
        if ok:
            self.add_members(list(t.path), _split(text))

    def add_members(self, path: list[str], names: list[str]) -> None:
        """Adds known individuals to a class; the first one added is chosen."""
        if not names:
            return
        self.selected = kind_of(path)
        self.folded.discard(self.selected)

        def after(added) -> None:
            if added:
                self.member = added[0]
                self.render()

        self.obs.do(self.tr("個体の追加"), lambda b: b.add_members(path, names), after)

    def _delete_key(self) -> None:
        """Two presses delete a class and its branch (the records under it keep the class above), or a known individual."""
        if self.book is None or not self.selected:
            return
        if self.member:
            key = (self.selected, self.member)
            if self._member_armed != key:
                self._member_armed = key
                count = sum(1 for o in self.book.living() if o.object_name == self.member)
                self.member_delete_button.setText(self.tr("削除?"))
                self._say(self.tr("もう一度で個体「{m}」を削除（その名前の記録 {n} 件はそのまま）").format(m=self.member, n=count))
                QTimer.singleShot(4000, self._disarm)
                return
            self._member_armed = None
            path, name = path_of(self.selected), self.member
            self.member = None
            self.obs.do(self.tr("個体の削除"), lambda b: b.remove_member(path, name))
            return
        path = path_of(self.selected)
        if self._armed != self.selected:
            self._armed = self.selected
            count = sum(1 for o in self.book.living() if within(o.kind, path))
            self.delete_button.setText(self.tr("削除?"))
            if self.book.scoped:
                # A shared store: every record under it follows (asked of it then), not only those read here.
                self._say(self.tr("もう一度で「{k}」を削除（その分類の記録の種別は上位の分類に）").format(k=self.selected))
            else:
                self._say(self.tr("もう一度で「{k}」を削除（記録 {n} 件の種別は上位の分類に）").format(k=self.selected, n=count))
            QTimer.singleShot(4000, self._disarm)
            return
        self._armed = None
        self.selected = kind_of(path[:-1]) or None
        self.obs.do(self.tr("分類の削除"), lambda b: b.remove_class(path))

    def _disarm(self) -> None:
        self._armed = None
        self._member_armed = None
        self.delete_button.setText(self.tr("削除"))
        self.member_delete_button.setText(self.tr("削除"))

    def _filter_obs(self) -> None:
        """「この画像で絞り込み」: 観測記録's list limited to this class and below."""
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
        own = {"levels": _split(self.levels_input.text()), "name_label": self.label_input.text().strip(), "specs": specs,
               "activities": _split(self.activities_input.text())}
        self.selected = kind_of([*parent, name])
        self.obs.do(self.tr("分類の変更"), lambda b: b.update_class(path, name=name, parent=parent, **own))

    def apply_member(self) -> None:
        """「変更」 of a known individual: its name, class, other names, specifications and note."""
        t = self._taxon()
        if self.book is None or t is None or not self.member or self.member not in (t.members or []):
            return
        path, name = list(t.path), self.member
        new_name = self.member_name_input.text().strip() or name
        dst = path_of(self.member_class_select.currentData() or kind_of(path))
        kept = self.book.members_kept
        specs = None
        note = None
        if kept:
            specs = {k: e.text().strip() for k, e in self._member_spec_inputs.items() if e.text().strip()}
            aliases = _split(self.member_alias_input.text())
            if aliases:
                specs[ALIAS_FIELD] = "、".join(dict.fromkeys(aliases))
            note = self.member_note_input.toPlainText()
        self.selected, self.member = kind_of(path), new_name

        def done(to) -> None:
            if to and kind_of(dst) != kind_of(path):
                self.move_member(path, to, dst)

        self.obs.do(self.tr("個体の変更"), lambda b: b.update_member(path, name, new_name=new_name, specs=specs, note=note), done)

    # ---- photos ------------------------------------------------------------

    def _photos(self) -> LocalPhotoStore:
        return LocalPhotoStore(self.context.gpkg())

    def _own_photos(self) -> Optional[list[str]]:
        """The chosen class's (or known individual's) own photos; None when nothing is chosen."""
        t = self._taxon()
        if t is None:
            return None
        if self.member:
            return list(t.member(self.member).photos or [])
        return list(t.photos or [])

    def _save_photos(self, photos: list[str], done: str) -> None:
        path = path_of(self.selected or "")
        if not path:
            return

        def after(to) -> None:
            if to:
                self._say(done)

        name = self.member
        if name:
            self.obs.do(self.tr("参考写真の保存"), lambda b: b.update_member(path, name, photos=photos), after)
        else:
            self.obs.do(self.tr("参考写真の保存"), lambda b: b.update_class(path, photos=photos), after)

    def _add_photo_url(self) -> None:
        edit = self.member_photo_url if self.member else self.photo_url
        value = edit.text().strip()
        own = self._own_photos()
        if own is None:
            return
        if not value.lower().startswith(("http://", "https://")):
            self._say(self.tr("写真の URL は http(s):// で指定"))
            return
        edit.clear()
        self._save_photos([*own, value], self.tr("参考写真を追加"))

    def add_photo_files(self, paths: list[str]) -> None:
        own = self._own_photos()
        if own is None:
            return
        refs = []
        for p in paths:
            mime = mimetypes.guess_type(p)[0] or ""
            if not mime.startswith("image/"):
                continue
            with open(p, "rb") as f:
                refs.append(self._photos().add(f.read(), os.path.basename(p), mime))
        if refs:
            self._save_photos([*own, *refs], self.tr("参考写真 {n} 枚を追加").format(n=len(refs)))

    def _add_photo_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, self.tr("参考写真の追加"), "", self.tr("画像 (*.png *.jpg *.jpeg *.gif *.webp *.bmp)"))
        if paths:
            self.add_photo_files(paths)

    def _remove_photo(self, ref: str) -> None:
        own = self._own_photos()
        if own is None:
            return
        self._save_photos([r for r in own if r != ref], self.tr("参考写真を削除"))
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
            # Only a web page: a photo is shared text, and a file, a UNC path or another scheme would be run by the system.
            web = photo_url(ref) or ""
            if re.match(r"^https?://", web, re.IGNORECASE):
                thumb.clicked.connect(lambda _on=False, web=web: QDesktopServices.openUrl(QUrl(web)))
            else:
                thumb.setEnabled(False)
        thumb.setToolTip(plain_tip(title if pixmap is not None else f"{title}: {ref}"))
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
        if on:
            self._render_fields()

    def _render_fields(self) -> None:
        if self.book is None:
            return
        if self._fields_draft is None:
            self._fields_draft = [SpecField(f.name, f.type, f.unit, f.use, list(f.roots) if f.roots else None) for f in self.book.spec_fields]
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
            use = QComboBox()
            for v, label in FIELD_USES:
                use.addItem(label, v)
            use.setCurrentIndex(max(0, use.findData(f.use)))
            use.currentIndexChanged.connect(lambda _i, f=f, use=use: setattr(f, "use", use.currentData()))
            roots = QLineEdit("、".join(f.roots or []))
            roots.setPlaceholderText(self.tr("すべて"))
            roots.editingFinished.connect(lambda f=f, roots=roots: setattr(f, "roots", _split(roots.text()) or None))
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
            for c, w in enumerate((name, kind, unit, use, roots, tools)):
                t.setCellWidget(i, c, w)

    def _read_fields(self) -> None:
        """What is typed but not yet left (no editingFinished)."""
        for i, f in enumerate(self._fields_draft or []):
            name, unit, roots = self.fields_table.cellWidget(i, 0), self.fields_table.cellWidget(i, 2), self.fields_table.cellWidget(i, 4)
            if isinstance(name, QLineEdit):
                f.name = name.text().strip() or f.name
            if isinstance(unit, QLineEdit):
                f.unit = unit.text().strip() or None
            if isinstance(roots, QLineEdit):
                f.roots = _split(roots.text()) or None

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
        member = current.data(0, MEMBER_ROLE) or None
        if kind and (kind != self.selected or member != self.member):
            self.selected, self.member = kind, member
            self._render_detail()

    def render(self) -> None:
        """The tree and the chosen class."""
        book = self.book
        # Not read yet (no image chosen in 「観測記録」): read first (the classification, and the records the classes
        # count); from an Esri store the tree is shown once they are (its classes; only the records around the image).
        waiting = book is not None and not self.obs.records_ready(self.render)
        self._rendering = True
        try:
            self.tree.clear()
            if book is None:
                self.note.setText(self.tr("保存先なし"))
                return
            if waiting:
                self.note.setText(self.tr("観測記録を取得中…"))
                return
            taxa = book.taxonomy()
            if self.selected and find(taxa, path_of(self.selected)) is None:
                self.selected = None
            here = find(taxa, path_of(self.selected)) if self.selected else None
            if self.member and (here is None or self.member not in (here.members or [])):
                self.member = None
            self.note.setText(self.tr("保存先: {s}・{n} 分類").format(s=book.taxonomy_store.label, n=len(taxa))
                              + (self.tr("（モノと記録の数は取得した分だけ: 開いた画像とそのまわり）") if book.scoped else ""))
            flows = book.flows()
            records = book.living()
            q = self.query.text().strip().lower()
            qn = same_name(q)

            def member_hit(t: Taxon, m: str) -> bool:
                said = t.member(m)
                words = [m, *member_names(t, m, book.spec_fields), *((said.specs or {}).values()), said.note or ""]
                return any(q in w.lower() or (qn and qn in same_name(w)) for w in words)

            hits = {kind_of(t.path): [m for m in t.members or [] if member_hit(t, m)] for t in taxa} if q else {}
            matches = [t for t in taxa if any(q in v.lower() for v in [t.path[-1], *((t.specs or {}).values())]) or hits.get(kind_of(t.path))] if q else taxa
            shown = {kind_of(t.path) for t in taxa if any(within(kind_of(m.path), t.path) for m in matches)} if q else None
            current = None
            icon = member_icon()
            teal = QBrush(QColor(MEMBER_COLOR))
            named: dict[str, int] = {}
            for o in records:
                if o.object_name:
                    named[o.object_name] = named.get(o.object_name, 0) + 1

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
                        if len(t.members) > MEMBERS_UNFOLDED and kind not in self._many_seen:
                            # Many individuals: folded when first shown (unfolded by the person, it stays so).
                            self._many_seen.add(kind)
                            if not (kind == self.selected and self.member):
                                self.folded.add(kind)
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
                    if kind == self.selected and not self.member:
                        current = item
                    walk(item, list(t.path))
                    label = name_label_of(taxa, list(t.path))
                    # Searching: all of a class found by its own name or specifications, else those found.
                    own_hit = q and any(q in v.lower() for v in [t.path[-1], *((t.specs or {}).values())])
                    for m in t.members or []:
                        if q and not own_hit and m not in hits.get(kind, []):
                            continue
                        said = t.member(m)
                        n = named.get(m, 0)
                        row = QTreeWidgetItem([m, f"{label}（個体）" if label else "個体", f"記録 {n}" if n else ""])
                        row.setIcon(0, icon)
                        row.setForeground(0, teal)
                        row.setForeground(1, teal)
                        row.setData(0, KIND_ROLE, kind)
                        row.setData(0, MEMBER_ROLE, m)
                        others = member_names(t, m, book.spec_fields)
                        tip = f"{kind} の個体「{m}」" + (f"（別名: {'、'.join(others)}）" if others else "")
                        if said.note:
                            tip += f"\n{said.note}"
                        row.setToolTip(0, plain_tip(tip + "\nダブルクリック・F2 で名前の変更、ドラッグで別の分類へ"))
                        row.setFlags((row.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled) & ~Qt.ItemFlag.ItemIsDropEnabled)
                        if q and m in hits.get(kind, []):
                            font = row.font(0)
                            font.setBold(True)
                            row.setFont(0, font)
                        item.addChild(row)
                        if kind == self.selected and m == self.member:
                            current = row
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
        if self.fields_box.is_open():
            self._render_fields()

    def _render_detail(self) -> None:
        book = self.book
        t = self._taxon()
        if t is not None and self.member and self.member not in (t.members or []):
            self.member = None
        self.form.setVisible(t is not None and not self.member)
        self.member_form.setVisible(t is not None and bool(self.member))
        if book is None:
            return
        if t is not None and self.member:
            self.hint.hide()
            self._render_member(t, self.member)
            return
        if t is None:
            self.hint.setText(self.tr("分類・個体（◆）の選択で詳細を表示（モノ {n}）。ドラッグで移動（行の間で前後、行の上で下位）、Alt+↑↓ で並べ替え、F2 で名前の変更、Insert で下位分類の追加").format(n=len(book.flows())))
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
        members = t.members or []
        self.members_label.setText(self.tr("既知の個体（{n}）").format(n=len(members)) if members else self.tr("既知の個体"))
        shown = "・".join(members[:12]) + (self.tr(" ほか {n}").format(n=len(members) - 12) if len(members) > 12 else "")
        self.members_line.setText(shown or self.tr("なし（{l}を登録すると、記録のモノ欄で選べる）").format(l=label or self.tr("個体名")))
        self.activities_input.setText("、".join(t.activities or []))
        above, at = activities_at(taxa, path[:-1])
        self.activities_input.setPlaceholderText(f"{'、'.join(above)}（{at[-1]}）" if above else "、".join(book.vocabulary.activities) + "（既定）")
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
        fields = [f for f in book.spec_fields if f.offered(path, False) or f.name in (t.specs or {})]
        extra = [k for k in (t.specs or {}) if not any(f.name == k for f in book.spec_fields)]
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

    def _render_member(self, t: Taxon, name: str) -> None:
        """The chosen known individual's form: its own values, the class's faintly where it says nothing."""
        book = self.book
        taxa = book.taxonomy()
        path = list(t.path)
        said = t.member(name)
        kept = book.members_kept
        if self._member_armed != (self.selected, name):
            self._member_armed = None
            self.member_delete_button.setText(self.tr("削除"))
        label = name_label_of(taxa, path)
        self.member_form.setTitle(f"{kind_of(path)} ▸ {name}（{label or self.tr('個体')}）")
        note = self.tr("分類「{c}」の個体。記録のモノ欄にこの名前か別名を入れると、この個体・分類で記録").format(c=path[-1])
        if not kept:
            note += self.tr("。保存先（{s}）に個体の表がないので、別名・諸元・写真・備考は保存できない（管理者に連絡: taxonomyTable.members）").format(
                s=book.taxonomy_store.label)
        self.member_note.setText(note)
        self.member_name_input.setText(name)
        self.member_class_select.clear()
        for x in taxa:
            self.member_class_select.addItem(kind_of(x.path), kind_of(x.path))
        self.member_class_select.setCurrentIndex(max(0, self.member_class_select.findData(kind_of(path))))
        own = said.specs or {}
        self.member_alias_input.setText(own.get(ALIAS_FIELD, ""))
        while self.member_specs_form.rowCount():
            self.member_specs_form.removeRow(0)
        inherited = member_specs_at(taxa, path, name)
        fields = [f for f in book.spec_fields if f.name != ALIAS_FIELD and (f.offered(path, True) or f.name in own)]
        extra = [k for k in own if k != ALIAS_FIELD and not any(f.name == k for f in book.spec_fields)]
        self._member_spec_inputs = {}
        for f in [*fields, *(SpecField(k, "text") for k in extra)]:
            box = QLineEdit(own.get(f.name, ""))
            above = inherited.get(f.name)
            if above and kind_of(above[1]) != kind_of([*path, name]):
                box.setPlaceholderText(f"{spec_text(f, above[0])}（{above[1][-1]}）")
                box.setToolTip(f"分類「{kind_of(above[1])}」の値。入力でこの個体だけ上書き")
            elif f.type == "list":
                box.setPlaceholderText("読点区切り")
            box.returnPressed.connect(self.apply_member)
            box.setEnabled(kept)
            self.member_specs_form.addRow(f"{f.name}（{f.unit}）" if f.unit else f.name, box)
            self._member_spec_inputs[f.name] = box
        if not self._member_spec_inputs:
            self.member_specs_form.addRow(QLabel(self.tr("項目なし（下の「諸元の項目」で追加）")))
        for w in (self.member_alias_input, self.member_note_input, self.member_photos_box):
            w.setEnabled(kept)
        self.member_note_input.setPlainText(said.note or "")
        self.member_photo_layout.clear()
        photos, source = member_photos_at(taxa, path, name)
        mine = kind_of(source) == kind_of([*path, name])
        for n, ref in enumerate(photos):
            title = f"{kind_of(source) if not mine else name} の参考写真 {n + 1}{'（ローカルのみ）' if is_local_photo(ref) else ''}"
            self.member_photo_layout.addWidget(self._photo_widget(ref, title, mine))
        if photos and not mine:
            self.member_photo_note.setText(self.tr("この個体の写真なし。分類「{c}」の写真を表示（追加でこの個体のものに）").format(c=source[-1]))
        elif not photos:
            self.member_photo_note.setText(self.tr("写真なし（クリックで拡大）"))
        elif book.taxonomy_store.label != LOCAL_LABEL and any(is_local_photo(r) for r in photos):
            self.member_photo_note.setText(self.tr("ファイルの写真はこの QGIS のみ（共有には URL）"))
        else:
            self.member_photo_note.setText(self.tr("クリックで拡大"))
