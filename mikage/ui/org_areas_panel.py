"""
「関心地域・地点」 ▸ 「組織」 (tsudo, 2026-11-24): the organisation's areas of
interest, kept in the named area layer (core/named_areas.py,
qgis_io/org_areas_io.py), which everyone may edit (tsudo 11:20Z).

The list shows the rows by カテゴリー (the category table's tree), by 上位の
地域 (PARENT_ID) or as one list, narrowed by a word (名前・よみがな・別名・管理
番号・メモ), by 用途 (関心地域 by default: the place names are thousands) and by
国. A chosen row is edited in the form below: every attribute the layer has
(an attribute the layer lacks has no field), and its shape: 「地図で描き直す」
(a polygon drawn), 「選んだ地物から」 (the feature selected in the current
layer) or 「頂点を直す」 (QGIS's vertex tool on a copy of the shape, taken
back when the button is let go or the area saved). 「新しい地域」 draws a new
one, which waits in the form until it has a name. 管理番号 left empty is given
on saving (NA-000123); 「管理番号のない n 件に付番…」 numbers the rows from
before. 「地図に表示」 keeps the areas of interest as the layer 「組織の関心地域」.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer, QgsWkbTypes
from qgis.gui import QgsColorButton, QgsRubberBand
from qgis.PyQt.QtCore import QDateTime, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.request.api import area_links, links_of
from ..core.named_areas import (
    AREA_USES, ORG_ROLES, PRIORITY_DEFAULT, PRIORITY_MAX, USE_BOTH, USE_INTEREST, USE_PLACE, OrgArea, alias_text, aliases_of,
    blank_count, category_names, category_tree, children_of,
)
from ..qgis_io import map_tools
from ..qgis_io.geometry import from_wgs84, shape_of, shape_rings, to_wgs84
from ..qgis_io.org_areas_io import OrgAreaBook, box_of, geometry_of, layer_of, remove_layer, select_on_map, show_layer
from ..qgis_io.tasks import run_task
from .catalog_overlay import DrawTool
from .docks import ask_no, plain_label, plain_tip
from .flow_layout import FlowLayout

#: Whether the areas of interest are kept on the map (the person's choice; off until turned on).
ON_MAP_KEY = "mikage/orgAreasOnMap"
#: The list's ways (並び).
BY_CATEGORY, BY_PARENT, FLAT = "category", "parent", "flat"
WAYS = ((BY_CATEGORY, "カテゴリー別"), (BY_PARENT, "上位の地域別"), (FLAT, "一覧"))
#: 用途 shown: the areas of interest (関心地域・両方), the place names (地名・両方) or all.
SHOW_INTEREST, SHOW_PLACE, SHOW_ALL = "interest", "place", "all"
SHOWS = ((SHOW_INTEREST, "関心地域"), (SHOW_PLACE, "地名"), (SHOW_ALL, "すべて"))
COLUMNS = ("名前", "管理番号", "用途", "カテゴリー", "国", "種類", "担当グループ", "重要度", "依頼", "更新日時")
_REQUESTS = COLUMNS.index("依頼")
USE_TIPS = {USE_PLACE: "地名だけ（通報・観測記録の場所の名前。関心地域の一覧やカードには出ない）",
            USE_INTEREST: "関心地域だけ（新着画像のカードと地図に出る。通報・観測記録の場所の名前には使わない）",
            USE_BOTH: "地名にも関心地域にも使う"}
#: The vertex tool's copy of the shape (taken back into the form).
EDIT_LAYER = "mikage/orgAreaEditing"


def _local(ms: Optional[int]) -> str:
    return QDateTime.fromMSecsSinceEpoch(int(ms)).toString("yyyy-MM-dd HH:mm") if ms else ""


def _same(a: OrgArea, b: OrgArea) -> bool:
    keys = ("id", "name", "use", "kana", "aliases", "kind", "country", "category", "parent", "owner_group", "priority", "color",
            "note", "name_en", "kind_en", "active")
    return all(getattr(a, k) == getattr(b, k) for k in keys)


class OrgAreasPanel(QWidget):
    """The 「組織」 tab: the list, its buttons and the form."""

    def __init__(self, iface, context, book: OrgAreaBook, reveal: Callable[[QWidget], None] = lambda _w: None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.context = context
        self.book = book
        self.reveal = reveal
        self.shown: list[OrgArea] = []
        self.draft: Optional[OrgArea] = None
        self.shape_changed = False
        self._saving = False
        self._removing = False
        self._tool_for: Optional[str] = None
        self._edit_layer: Optional[QgsVectorLayer] = None
        self.group_names: list[str] = []
        self._groups_asked = False
        self._ledger_asked = False
        self._items: dict[str, QTreeWidgetItem] = {}
        self._picked_name = ""
        self.dialog = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.note = plain_label(QLabel())
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("名前・よみがな・別名・管理番号・メモで絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.render())
        self.reload_button = QPushButton(self.tr("最新を取得"))
        self.reload_button.setToolTip(self.tr("地名のレイヤーとカテゴリーを読み直す（ほかの PC や ArcGIS Pro での変更も）"))
        self.reload_button.clicked.connect(lambda: self.book.load())
        top.addWidget(self.search, 1)
        top.addWidget(self.reload_button)
        layout.addLayout(top)
        filters = FlowLayout()
        self.show_select = QComboBox()
        for key, text in SHOWS:
            self.show_select.addItem(self.tr(text), key)
        self.show_select.setToolTip(self.tr("用途で絞り込み（関心地域・地名は「両方」の地域を含む）"))
        self.way_select = QComboBox()
        for key, text in WAYS:
            self.way_select.addItem(self.tr(text), key)
        self.country_select = QComboBox()
        self.country_select.setToolTip(self.tr("国で絞り込み"))
        for w in (self.show_select, self.way_select, self.country_select):
            w.currentIndexChanged.connect(lambda _i: self.render())
            filters.addWidget(w)
        filter_row = QWidget()
        filter_row.setLayout(filters)
        layout.addWidget(filter_row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels([self.tr(c) for c in COLUMNS])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self._chosen_changed)
        self.tree.itemDoubleClicked.connect(lambda _i, _c: self.zoom())
        self.tree.setMinimumHeight(160)
        layout.addWidget(self.tree, 1)

        self.buttons: dict[str, QPushButton] = {}
        flow = FlowLayout()
        for key, text, tip, slot, checkable in [
            ("new", self.tr("新しい地域"), self.tr("地図に多角形を描いて新しい組織の関心地域。クリックで頂点、ダブルクリックか右クリックで確定、Esc でやり直し"),
             self._new_mode, True),
            ("selection", self.tr("選んだ地物から"), self.tr("作業中のレイヤーで選んだ面の地物の形から新しく作成"), self.from_selection, False),
            ("request", self.tr("撮像依頼…"), self.tr("選んだ地域（Ctrl・Shift + クリックで複数）の撮像依頼を作成。名称・範囲・依頼グループ・"
                                                     "優先度は地域のもの。地域の形が直されたら依頼の範囲も合わせる"), self.make_requests, False),
            ("zoom", self.tr("移動"), self.tr("選んだ地域へ地図を移動（ダブルクリックでも）"), self.zoom, False),
            ("map", self.tr("地図に表示"), self.tr("関心地域を「組織の関心地域」のレイヤーとして地図に出す（次に QGIS を開いたときも）"),
             self._map_toggled, True),
            ("categories", self.tr("カテゴリー…"), self.tr("地域のカテゴリーの一覧と追加・変更（全員が編集できる）"), self.open_categories, False),
            ("number", self.tr("付番…"), self.tr("管理番号のない行に番号を付ける（NA-000123 の形で、いちばん大きい番号の次から）"),
             self.number_blanks, False),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            if checkable:
                b.toggled.connect(lambda on, slot=slot: slot(on))
            else:
                b.clicked.connect(lambda _on=False, slot=slot: slot())
            flow.addWidget(b)
            self.buttons[key] = b
        holder = QWidget()
        holder.setLayout(flow)
        layout.addWidget(holder)

        self._build_form()
        layout.addWidget(self.form)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.draw_tool = DrawTool(self.canvas, self._drawn)
        self.band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.band.setStrokeColor(QColor("#0ea5e9"))
        self.band.setFillColor(QColor(14, 165, 233, 40))
        self.band.setWidth(2)
        self.band.setLineStyle(Qt.PenStyle.DashLine)
        self.band.setZValue(955)

        project = QgsProject.instance()
        self._hooks = [(project.layersWillBeRemoved, self._layers_going), (project.readProject, self._project_changed),
                       (iface.newProjectCreated, self._project_changed), (self.canvas.mapToolSet, self._tool_changed)]
        for signal, slot in self._hooks:
            signal.connect(slot)
        book.changed.connect(self._book_changed)
        context.configChanged.connect(self._config_changed)
        self._config_changed()
        if self.wanted_on_map() and book.settings is not None:
            book.want()

    # ---- the form ------------------------------------------------------------------------------

    def _build_form(self) -> None:
        self.form = QGroupBox()
        form = QFormLayout(self.form)
        # In a narrow dock the field goes under its name (nothing cut off at the right).
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.rows: dict[str, tuple[QLabel, QWidget]] = {}

        def row(role: str, label: str, widget: QWidget, tip: str = "") -> QWidget:
            text = QLabel(self.tr(label))
            if tip:
                text.setToolTip(tip)
                widget.setToolTip(tip)
            form.addRow(text, widget)
            self.rows[role] = (text, widget)
            return widget

        self.name = row("name", "名前", QLineEdit(), self.tr("カード・地図・依頼に出る名前"))
        self.kana = row("kana", "よみがな", QLineEdit(), self.tr("ひらがな（並べ替えと検索に使う）"))
        self.alias = row("alias", "別名", QLineEdit(), self.tr("カンマ区切りでいくつでも（略称・旧名・英語の通称など。検索に使う）"))
        self.area_id = row("id", "管理番号", QLineEdit(), self.tr("組織の管理番号（依頼・記録との紐付けに使う。全体で重ならない）。空なら保存時に自動で付番"))
        self.area_id.setPlaceholderText(self.tr("空なら保存時に自動で付番（NA-000123）"))
        self.use = row("use", "用途", QComboBox())
        for u in AREA_USES:
            self.use.addItem(u, u)
            self.use.setItemData(self.use.count() - 1, self.tr(USE_TIPS[u]), Qt.ItemDataRole.ToolTipRole)
        self.kind = row("kind", "種類", QComboBox(), self.tr("階層の管理（都道府県・市町村・港・施設など）。通報の placeKinds にも使う"))
        self.kind.setEditable(True)
        self.category = row("category", "カテゴリー", QComboBox(), self.tr("地域のカテゴリー（「カテゴリー…」で追加・変更）"))
        country_row = QWidget()
        country_layout = QHBoxLayout(country_row)
        country_layout.setContentsMargins(0, 0, 0, 0)
        self.country = QComboBox()
        self.country.setEditable(True)
        self.country_button = QPushButton(self.tr("形から"))
        self.country_button.setToolTip(self.tr("形の中の点がある国を国のレイヤーから入れる"))
        self.country_button.clicked.connect(lambda _on=False: self.country_from_shape())
        country_layout.addWidget(self.country, 1)
        country_layout.addWidget(self.country_button)
        row("country", "国", country_row, self.tr("国コード（国のレイヤーの COUNTRY_CODE）。1 地域に 1 つ"))
        self.parent_area = row("parent", "上位の地域", QComboBox(), self.tr("この地域を含む地域（その管理番号を入れる）。一覧の「上位の地域別」の木になる"))
        self.parent_area.setEditable(True)
        self.owner_group = row("ownerGroup", "担当グループ", QComboBox(), self.tr("この地域を受け持つ依頼グループ（地域から撮像依頼を出すときの既定）"))
        self.owner_group.setEditable(True)
        self.priority = row("priority", "重要度", QSpinBox(), self.tr("1（最も高い）〜1000。空なら依頼の既定と同じ 500"))
        self.priority.setRange(0, PRIORITY_MAX)
        self.priority.setSpecialValueText(self.tr("既定（{n}）").format(n=PRIORITY_DEFAULT))
        self.color = row("color", "色", QgsColorButton(), self.tr("地図とカードの色。なしならカテゴリーの色"))
        self.color.setAllowOpacity(False)
        self.color.setShowNoColor(True)
        self.color.setNoColorString(self.tr("カテゴリーの色"))
        self.active = row("active", "有効", QCheckBox(self.tr("使用中")), self.tr("外すと関心地域・地名のどちらにも使わない（行は残る）"))
        self.name_en = row("nameEn", "英語名", QLineEdit())
        self.kind_en = row("kindEn", "種類（英語）", QLineEdit())
        self.memo = row("note", "メモ", QPlainTextEdit())
        self.memo.setMaximumHeight(60)
        shape_row = QWidget()
        shape_layout = QVBoxLayout(shape_row)
        shape_layout.setContentsMargins(0, 0, 0, 0)
        self.shape = plain_label(QLabel())
        self.redraw_button = QPushButton(self.tr("描き直す"))
        self.redraw_button.setCheckable(True)
        self.redraw_button.setToolTip(self.tr("地図に多角形を描き直す（保存で決まる）"))
        self.redraw_button.toggled.connect(lambda on: self._use_tool("redraw" if on else None))
        self.reselect_button = QPushButton(self.tr("選んだ地物から"))
        self.reselect_button.setToolTip(self.tr("作業中のレイヤーで選んだ面の地物の形にする（保存で決まる）"))
        self.reselect_button.clicked.connect(lambda _on=False: self.shape_from_selection())
        self.vertex_button = QPushButton(self.tr("頂点を直す"))
        self.vertex_button.setCheckable(True)
        self.vertex_button.setToolTip(self.tr("QGIS の頂点ツールで頂点を動かす・足す・消す。終わったらもう一度押す（保存でも決まる）"))
        self.vertex_button.toggled.connect(self._vertex_mode)
        shape_layout.addWidget(self.shape)
        shape_buttons = FlowLayout()
        for b in (self.redraw_button, self.reselect_button, self.vertex_button):
            shape_buttons.addWidget(b)
        shape_layout.addLayout(shape_buttons)
        row("shape", "形", shape_row)
        for w in (self.name, self.kana, self.alias, self.area_id, self.name_en, self.kind_en):
            w.textEdited.connect(lambda _t: self._form_edited())
        self.name.returnPressed.connect(self.save)
        for w in (self.use, self.category):
            w.currentIndexChanged.connect(lambda _i: self._form_edited())
        for w in (self.kind, self.country, self.parent_area, self.owner_group):
            w.editTextChanged.connect(lambda _t: self._form_edited())
            w.currentIndexChanged.connect(lambda _i: self._form_edited())
        self.priority.valueChanged.connect(lambda _v: self._form_edited())
        self.color.colorChanged.connect(lambda _c: self._form_edited())
        self.active.toggled.connect(lambda _on: self._form_edited())
        self.memo.textChanged.connect(self._form_edited)
        actions = QHBoxLayout()
        self.problem = plain_label(QLabel())
        self.problem.setStyleSheet("color: #b91c1c")
        self.problem.setWordWrap(True)
        self.save_button = QPushButton(self.tr("保存"))
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda _on=False: self.save())
        self.cancel_button = QPushButton(self.tr("取り消し"))
        self.cancel_button.setToolTip(self.tr("新しいものは捨て、選んだものは保存した状態に戻す"))
        self.cancel_button.clicked.connect(lambda _on=False: self.cancel())
        actions.addWidget(self.problem, 1)
        actions.addWidget(self.save_button)
        actions.addWidget(self.cancel_button)
        form.addRow(actions)
        self.form.setVisible(False)

    def _fill_choices(self) -> None:
        """The form's lists: kinds, categories, countries, areas and groups as read."""
        areas = self.book.areas or []
        for combo, values in ((self.kind, sorted({a.kind for a in areas if a.kind})),
                              (self.owner_group, sorted({*(a.owner_group for a in areas if a.owner_group), *self.group_names}))):
            text = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("")
            combo.addItems(values)
            combo.setEditText(text)
            combo.blockSignals(False)
        self.category.blockSignals(True)
        chosen = self.category.currentData()
        self.category.clear()
        self.category.addItem(self.tr("（なし）"), "")
        for depth, c in category_tree(self.book.categories):
            self.category.addItem("　" * depth + c.name + ("" if c.active else self.tr("（停止）")), c.id)
        i = self.category.findData(chosen)
        self.category.setCurrentIndex(max(0, i))
        self.category.blockSignals(False)
        self.country.blockSignals(True)
        text = self.country.currentText()
        self.country.clear()
        self.country.addItem("", "")
        for code, name in (self.book.list.countries if self.book.list is not None else []):
            self.country.addItem(f"{code} {name}" if name != code else code, code)
        self.country.setEditText(text)
        self.country.blockSignals(False)
        self.parent_area.blockSignals(True)
        text = self.parent_area.currentText()
        self.parent_area.clear()
        self.parent_area.addItem("", "")
        for a in areas:
            if a.id:
                self.parent_area.addItem(f"{a.id} {a.name}", a.id)
        completer = QCompleter(self.parent_area.model(), self.parent_area)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.parent_area.setCompleter(completer)
        self.parent_area.setEditText(text)
        self.parent_area.blockSignals(False)

    @staticmethod
    def _code(combo: QComboBox) -> str:
        """The value of an editable list: the chosen item's, or the first word typed (an id or a code)."""
        text = combo.currentText().strip()
        i = combo.findText(text)
        if i >= 0 and combo.itemData(i) is not None:
            return str(combo.itemData(i))
        return text.split()[0] if text else ""

    @staticmethod
    def _show_code(combo: QComboBox, value: str) -> None:
        i = combo.findData(value) if value else 0
        if i >= 0:
            combo.setCurrentIndex(i)
            combo.setEditText(combo.itemText(i))
        else:
            combo.setEditText(value)

    def _fill(self, area: OrgArea, shape_changed: bool = False) -> None:
        self.draft = area
        self.shape_changed = shape_changed
        saved = area.oid is not None
        self.form.setTitle(self.tr("編集: {name}").format(name=area.name) if saved else self.tr("新しい組織の関心地域"))
        widgets = (self.name, self.kana, self.alias, self.area_id, self.use, self.kind, self.category, self.country, self.parent_area,
                   self.owner_group, self.priority, self.color, self.active, self.name_en, self.kind_en, self.memo)
        for w in widgets:
            w.blockSignals(True)
        self.name.setText(area.name)
        self.kana.setText(area.kana)
        self.alias.setText(alias_text(area.aliases))
        self.area_id.setText(area.id)
        self.use.setCurrentIndex(max(0, self.use.findData(area.use)))
        self.kind.setEditText(area.kind)
        self.category.setCurrentIndex(max(0, self.category.findData(area.category)))
        if area.category and self.category.findData(area.category) < 0:
            self.category.addItem(self.tr("{id}（カテゴリーの表にない）").format(id=area.category), area.category)
            self.category.setCurrentIndex(self.category.count() - 1)
        self._show_code(self.country, area.country)
        self._show_code(self.parent_area, area.parent)
        self.owner_group.setEditText(area.owner_group)
        self.priority.setValue(area.priority or 0)
        if area.color and QColor(area.color).isValid():
            self.color.setColor(QColor(area.color))
        else:
            self.color.setToNoColor()
        self.active.setChecked(area.active)
        self.name_en.setText(area.name_en)
        self.kind_en.setText(area.kind_en)
        self.memo.setPlainText(area.note)
        for w in widgets:
            w.blockSignals(False)
        self.problem.setText("")
        self._show_shape()
        self._update_buttons()
        if not saved:
            self.reveal(self.form)
            self.name.setFocus()

    def _read_form(self) -> Optional[OrgArea]:
        d = self.draft
        if d is None:
            return None
        colour = self.color.color()
        return replace(
            d, name=self.name.text().strip(), kana=self.kana.text().strip(), aliases=aliases_of(self.alias.text()),
            id=self.area_id.text().strip(), use=self.use.currentData() or USE_PLACE, kind=self.kind.currentText().strip(),
            category=self.category.currentData() or "", country=self._code(self.country), parent=self._code(self.parent_area),
            owner_group=self.owner_group.currentText().strip(), priority=self.priority.value() or None,
            color=colour.name() if colour.isValid() and colour.alpha() > 0 else "", active=self.active.isChecked(),
            name_en=self.name_en.text().strip(), kind_en=self.kind_en.text().strip(), note=self.memo.toPlainText().strip())

    def _form_edited(self) -> None:
        if self.draft is None:
            return
        self.draft = self._read_form()
        self.problem.setText("")
        self._update_buttons()

    def _clear_form(self) -> None:
        self._end_vertex_edit(keep=False)
        self.draft = None
        self.shape_changed = False
        self.redraw_button.setChecked(False)
        self._show_shape()
        self._update_buttons()

    def _changed(self) -> bool:
        d = self.draft
        if d is None:
            return False
        saved = self.book.find(d.key) if d.oid is not None else None
        if saved is None:
            return True
        return self.shape_changed or not _same(d, saved)

    def _show_shape(self) -> None:
        d = self.draft
        self.shape.setText(self.tr(d.shape_text()) if d is not None else "")
        self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        if d is None or not d.has_shape or (not self.shape_changed and layer_of() is not None and d.is_interest):
            return
        g = geometry_of(d)
        if g is None:
            return
        g = QgsGeometry(g)
        g.transform(from_wgs84(self.canvas.mapSettings().destinationCrs()))
        self.band.setToGeometry(g, None)

    # ---- state ---------------------------------------------------------------------------------

    @property
    def settings(self):
        return self.book.settings

    def say(self, text: str) -> None:
        self.status.setText(text)

    def editable(self) -> bool:
        lst = self.book.list
        return self.settings is not None and lst is not None and lst.editable

    @staticmethod
    def wanted_on_map() -> bool:
        return QgsSettings().value(ON_MAP_KEY, False, type=bool)

    def chosen(self) -> list[OrgArea]:
        out = []
        for item in self.tree.selectedItems():
            key = item.data(0, Qt.ItemDataRole.UserRole)
            a = self.book.find(key) if key else None
            if a is not None and a not in out:
                out.append(a)
        return out

    def choose(self, key: str) -> None:
        it = self._items.get(key)
        if it is not None:
            self.tree.setCurrentItem(it)
            self.tree.scrollToItem(it)

    def _config_changed(self) -> None:
        s = self.settings
        if s is None:
            self.note.setText(self.tr("組織の関心地域は使えない（管理者に連絡: namedAreas）［E703］"))
        else:
            self._note()
        self._clear_form()
        self.render()
        if s is not None and self.isVisible():
            self.book.want()

    def _note(self) -> None:
        s, lst = self.settings, self.book.list
        if s is None:
            return
        parts = [self.tr("組織の関心地域（地名のレイヤーに保存。全員が編集できる）") if s.own
                 else self.tr("地名のレイヤー（通報の設定 alertReports.namedAreas）を組織の関心地域として使用")]
        if lst is not None:
            missing = lst.missing(s.areas.fields, ORG_ROLES)
            if missing:
                parts.append(self.tr("地名のレイヤーに {f} がないので、その入力欄は出さない（管理者に連絡: 地名のレイヤーに項目を追加）").format(
                    f="・".join(missing)))
            if not lst.editable:
                parts.append(self.tr("地名のレイヤーがマップサービスなので変更できない（管理者に連絡: namedAreas に …/FeatureServer/<n>）"))
            if s.categories is None:
                parts.append(self.tr("カテゴリーの表が未設定（namedAreas.categories）"))
            elif lst.category_failure:
                parts.append(self.tr("カテゴリーを取得できない: {m}").format(m=lst.category_failure))
        self.note.setText("。".join(parts))

    def shown_now(self) -> None:
        """The tab came into view: read when not read yet."""
        self._ledger_asked = False
        self.book.want()
        self._want_groups()
        self.render()

    def _book_changed(self) -> None:
        if self.draft is not None and self.draft.oid is not None and self.book.areas is not None and self.book.find(self.draft.key) is None:
            self._clear_form()  # gone meanwhile
        self._note()
        self._fill_choices()
        self.render()
        self._place_layer(True)

    def _want_groups(self) -> None:
        """The 依頼グループ names, as hints for 担当グループ (read once; a failure only leaves no hints)."""
        if self._groups_asked:
            return
        store = next((s for s in self.context.config.request_stores if s.groups), None)
        if store is None:
            return
        self._groups_asked = True
        from ..core.request.api import load_groups

        client, token = self.context.esri(), self.context.token_for(store.url, getattr(store, "token", None))

        def work(_task):
            return [g.name for g in load_groups(client, store.groups, store.group_fields, token) if g.active]

        def done(names) -> None:
            self.group_names = list(names)
            self._fill_choices()

        run_task(self.tr("依頼グループの取得"), work, done, lambda _m: None)

    # ---- the list ------------------------------------------------------------------------------

    def _wanted(self, a: OrgArea, show: str, country: str) -> bool:
        if show == SHOW_INTEREST and not a.is_interest:
            return False
        if show == SHOW_PLACE and not a.is_place:
            return False
        if country and a.country.casefold() != country.casefold():
            return False
        return a.matches(self.search.text())

    def _fill_countries(self) -> None:
        codes = sorted({a.country for a in self.book.areas or [] if a.country})
        names = dict(self.book.list.countries) if self.book.list is not None else {}
        now = self.country_select.currentData()
        self.country_select.blockSignals(True)
        self.country_select.clear()
        self.country_select.addItem(self.tr("すべての国"), "")
        for c in codes:
            self.country_select.addItem(f"{c} {names[c]}" if c in names and names[c] != c else c, c)
        self.country_select.setCurrentIndex(max(0, self.country_select.findData(now)))
        self.country_select.blockSignals(False)

    def render(self) -> None:
        book = self.book
        keep = {a.key for a in self.chosen()}
        self._fill_countries()
        show, way, country = self.show_select.currentData(), self.way_select.currentData(), self.country_select.currentData() or ""
        self.shown = [a for a in book.areas or [] if self._wanted(a, show, country)]
        names = category_names(book.categories)
        countries = dict(book.list.countries) if book.list is not None else {}
        self._items = {}
        links = self.links()
        self.tree.setColumnHidden(_REQUESTS, links is None)
        self.tree.blockSignals(True)
        self.tree.clear()
        grey = QBrush(QColor("#6b7280"))

        def area_item(a: OrgArea) -> QTreeWidgetItem:
            count = links_of(links, a.id) if links is not None and a.id else None
            cells = [a.name, a.id or self.tr("（未付番）"), a.use, names.get(a.category.casefold(), a.category), a.country, a.kind,
                     a.owner_group, str(a.priority) if a.priority else "", count.text() if count is not None and count.total else "",
                     _local(a.updated_at)]
            it = QTreeWidgetItem(cells)
            if count is not None and count.total:
                it.setToolTip(_REQUESTS, self.tr("有効な依頼 {a} 件・全部で {t} 件（うち地域の形に合わせる {f} 件）").format(
                    a=count.active, t=count.total, f=count.following))
            it.setData(0, Qt.ItemDataRole.UserRole, a.key)
            colour = QColor(book.color(a))
            if colour.isValid():
                it.setIcon(0, _swatch(colour))
            tips = [a.name, *(t for t in (a.kana, alias_text(a.aliases)) if t)]
            if a.note:
                tips.append(a.note)
            if a.updated_by:
                tips.append(self.tr("更新: {who} {when}").format(who=a.updated_by, when=_local(a.updated_at)))
            it.setToolTip(0, plain_tip("\n".join(tips)))
            if a.country in countries:
                it.setToolTip(4, countries[a.country])
            if not a.active:
                for c in range(len(COLUMNS)):
                    it.setForeground(c, grey)
                it.setToolTip(0, plain_tip(self.tr("停止中") + "\n" + "\n".join(tips)))
            self._items[a.key] = it
            return it

        bold = QFont()
        bold.setBold(True)
        if way == BY_CATEGORY:
            by: dict[str, list[OrgArea]] = {}
            known = {c.id.casefold() for c in book.categories}
            for a in self.shown:
                by.setdefault(a.category.casefold() if a.category.casefold() in known else "", []).append(a)
            nodes: dict[str, QTreeWidgetItem] = {}
            tree = category_tree(book.categories)
            counts: dict[str, int] = {}
            parents = {c.id.casefold(): c.parent.casefold() for c in book.categories}
            for key, areas in by.items():
                k, seen = key, set()
                while k and k not in seen:
                    seen.add(k)
                    counts[k] = counts.get(k, 0) + len(areas)
                    k = parents.get(k, "")
            for depth, c in tree:
                k = c.id.casefold()
                if not counts.get(k):
                    continue
                node = QTreeWidgetItem([f"{c.name}（{counts[k]}）"])
                node.setFont(0, bold)
                node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                if c.color:
                    node.setIcon(0, _swatch(QColor(c.color)))
                p = parents.get(k, "")
                (nodes[p].addChild(node) if p in nodes else self.tree.addTopLevelItem(node))
                nodes[k] = node
            for k, node in nodes.items():  # the categories under a category first, then its areas
                for a in by.get(k, []):
                    node.addChild(area_item(a))
            if by.get(""):
                node = QTreeWidgetItem([self.tr("（カテゴリーなし）（{n}）").format(n=len(by[""]))])
                node.setFont(0, bold)
                node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.tree.addTopLevelItem(node)
                for a in by[""]:
                    node.addChild(area_item(a))
            self.tree.expandAll()
        elif way == BY_PARENT:
            kids = children_of(self.shown)
            done: set[str] = set()

            def add(parent_item: Optional[QTreeWidgetItem], key: str) -> None:
                for a in kids.get(key, []):
                    if a.key in done:
                        continue
                    done.add(a.key)
                    it = area_item(a)
                    (parent_item.addChild(it) if parent_item is not None else self.tree.addTopLevelItem(it))
                    if a.id:
                        add(it, a.id.casefold())

            add(None, "")
            for a in self.shown:  # in a loop of parents: at the top
                if a.key not in done:
                    done.add(a.key)
                    self.tree.addTopLevelItem(area_item(a))
            self.tree.expandAll()
        else:
            for a in self.shown:
                self.tree.addTopLevelItem(area_item(a))
        for key in keep:
            if key in self._items:
                self._items[key].setSelected(True)
        self.tree.blockSignals(False)
        if book.settings is None:
            self.say("")
        elif book.loading:
            self.say(self.tr("取得中…"))
        elif book.list is None and book.failure:
            self.say(self.tr("取得に失敗: {m}").format(m=book.failure))
        elif book.list is not None and not self._saving:
            interest = sum(1 for a in book.areas if a.is_interest)
            self.say(self.tr("表示 {n} 件（関心地域 {i} 件・地名のレイヤー全体 {all} 件）").format(n=len(self.shown), i=interest, all=len(book.areas)))
        self._update_buttons()

    def _update_buttons(self) -> None:
        s = self.settings
        ready = s is not None and self.book.list is not None
        editing = ready and self.editable()
        chosen = self.chosen()
        idle = not self._saving
        for key in ("new", "selection"):
            self.buttons[key].setVisible(editing or not ready)
            self.buttons[key].setEnabled(editing and idle)
        self.buttons["zoom"].setEnabled(bool(chosen) or (self.draft is not None and self.draft.has_shape))
        port = self.requests()
        self.buttons["request"].setVisible(port is not None and port.store is not None)
        self.buttons["request"].setEnabled(bool(chosen) and idle)
        # 管理番号: not changed while requests are linked to the area (they find it by it).
        linked = self._linked(self.draft)
        self.area_id.setEnabled(not linked)
        self.area_id.setToolTip(self.tr("撮像依頼 {n} 件がつながっているので変えられない").format(n=linked) if linked
                                else self.tr("組織の管理番号（依頼・記録との紐付けに使う。全体で重ならない）。空なら保存時に自動で付番"))
        self.buttons["map"].setEnabled(s is not None)
        self.buttons["map"].blockSignals(True)
        self.buttons["map"].setChecked(layer_of() is not None)
        self.buttons["map"].blockSignals(False)
        self.buttons["categories"].setEnabled(ready and s.categories is not None)
        blanks = blank_count(self.book.areas or []) if ready and "id" in s.areas.fields and s.areas.fields["id"] in self.book.list.fields else 0
        self.buttons["number"].setVisible(editing and blanks > 0)
        self.buttons["number"].setText(self.tr("管理番号のない {n} 件に付番…").format(n=blanks))
        self.buttons["number"].setEnabled(idle)
        self.reload_button.setEnabled(s is not None and not self.book.loading)
        self.form.setVisible(self.draft is not None)
        if self.book.list is not None and s is not None:
            have = self.book.list.fields
            for role, (label, widget) in self.rows.items():
                on = role in ("shape",) or (role in s.areas.fields and s.areas.fields[role] in have)
                label.setVisible(on)
                widget.setVisible(on)
        self.save_button.setVisible(editing)
        self.save_button.setEnabled(self.draft is not None and idle and self._changed())
        for b in (self.redraw_button, self.reselect_button, self.vertex_button):
            b.setVisible(editing)
            b.setEnabled(self.draft is not None and idle)
        self.country_button.setVisible(editing and s is not None and s.countries is not None)
        self.country_button.setEnabled(self.draft is not None and self.draft.has_shape and idle)

    def _chosen_changed(self) -> None:
        chosen = self.chosen()
        select_on_map([a.key for a in chosen])
        if len(chosen) == 1 and not self._editing_vertices():
            self._fill(replace(chosen[0]))
        elif self.draft is not None and self.draft.oid is not None and not chosen:
            self._clear_form()
        self._update_buttons()

    # ---- 撮像依頼 (the requests made from the areas) ------------------------------------------------

    #: The 「撮像依頼」 dock (ui/ports.RequestsPort, None without it): features/images.py sets it.
    requests: Callable[[], Optional[object]] = staticmethod(lambda: None)

    def links(self) -> Optional[dict]:
        """Each area's requests (core/request/area_link.AreaLinks by 管理番号), None while the ledger is not read or there is none."""
        port = self.requests()
        if port is None or port.store is None:
            return None
        if not port.loaded:
            if self.isVisible() and not self._ledger_asked:
                self._ledger_asked = True
                port.reload()
            return None
        return area_links(port.requests)

    def _linked(self, area: Optional[OrgArea]) -> int:
        """How many requests are linked to a saved area (by its 管理番号 as saved)."""
        saved = self._saved(area)
        if saved is None:
            return 0
        area_id = saved.id
        links = self.links() if area_id else None
        return links_of(links, area_id).total if links is not None else 0

    def _saved(self, area: Optional[OrgArea]) -> Optional[OrgArea]:
        """The area as read (by its object id: its 管理番号 may be the one being changed)."""
        if area is None or area.oid is None:
            return None
        return next((a for a in self.book.areas or [] if a.oid == area.oid), None)

    def requests_read(self) -> None:
        """The ledger was read in 撮像依頼: each area's requests counted again."""
        try:
            if self.book.list is not None:
                self.render()
        except RuntimeError:  # the panel is gone
            pass

    def make_requests(self):
        """「撮像依頼…」: the chosen areas' requests in the 「撮像依頼」 form (one each)."""
        port = self.requests()
        chosen = [a for a in self.chosen() if a.active]
        if port is None or not chosen:
            if self.chosen() and not chosen:
                self.say(self.tr("停止中の地域からは依頼を作らない"))
            return None
        unnumbered = [a.name for a in chosen if not a.id]
        if unnumbered:
            self.say(self.tr("管理番号のない地域は先に付番（{names}）").format(names="、".join(unnumbered[:3])))
            return None
        return port.requests_from_areas(chosen)

    # ---- saving --------------------------------------------------------------------------------

    def save(self) -> None:
        if self._editing_vertices():
            self._end_vertex_edit(keep=True)
        d = self._read_form()
        if d is None or self._saving:
            return
        self.draft = d
        from ..core.named_areas import check_area

        problems = check_area(d, self.book.areas or [], self.settings)
        saved = self._saved(d)
        linked = self._linked(d)
        if saved is not None and linked and d.id.strip().upper() != saved.id.strip().upper():
            problems.append(self.tr("撮像依頼 {n} 件がつながっているので管理番号は変えられない").format(n=linked))
        if problems:
            self.problem.setText("、".join(problems))
            return
        self._saving = True
        self.say(self.tr("保存中…"))
        self._update_buttons()

        def done(saved: OrgArea) -> None:
            self._saving = False
            self.draft = None
            self.shape_changed = False
            self.render()
            self.choose(saved.key)
            kept = self.book.find(saved.key)
            if kept is not None:
                self._fill(replace(kept))
            self._show_shape()
            numbered = not d.id and saved.id
            self.say(self.tr("「{name}」を保存").format(name=saved.name)
                     + (self.tr("（管理番号 {id}）").format(id=saved.id) if numbered else ""))

        def failed(message: str) -> None:
            self._saving = False
            self._update_buttons()
            self.problem.setText(self.tr("保存に失敗: {m}").format(m=message))
            self.say("")

        self.book.save(d, self.shape_changed or d.oid is None, done, failed)

    def cancel(self) -> None:
        self._end_vertex_edit(keep=False)
        self.redraw_button.setChecked(False)
        d = self.draft
        saved = self.book.find(d.key) if d is not None and d.oid is not None else None
        if saved is None:
            self._clear_form()
            self.say(self.tr("新しい地域を取り消し") if d is not None else "")
        else:
            self._fill(replace(saved))

    def start_new(self, name: str = "", rings: Optional[list] = None, color: str = "", note: str = "") -> None:
        """A new area in the form (as from a person's own area: 「組織の地域にする…」), waiting for its name and save."""
        if self.settings is None:
            return
        self.tree.clearSelection()
        self._fill(OrgArea(name=name, use=USE_INTEREST, rings=rings, color=color, note=note), shape_changed=True)
        self.say(self.tr("カテゴリーなどを入れて保存（管理番号は空なら自動で付番）"))

    def number_blanks(self, ask: bool = True) -> None:
        n = blank_count(self.book.areas or [])
        if not n or self._saving:
            return
        if ask and not ask_no(self, self.tr("管理番号の付番"), self.tr(
                "管理番号のない {n} 件に、いちばん大きい番号の次から番号を付ける（地名だけの行も。付けた番号は通報・記録の place.id に出る）").format(n=n),
                yes=self.tr("付番")):
            return
        self._saving = True
        self.say(self.tr("付番中…"))
        self._update_buttons()

        def done(count: int, problems: list) -> None:
            self._saving = False
            self.say(self.tr("{n} 件に付番").format(n=count) + ("。" + " / ".join(problems[:3]) if problems else ""))
            self._update_buttons()

        def failed(message: str) -> None:
            self._saving = False
            self.say(self.tr("付番に失敗: {m}").format(m=message))
            self._update_buttons()

        self.book.number_blanks(done, failed)

    def country_from_shape(self) -> None:
        d, s = self.draft, self.settings
        if d is None or not d.has_shape or s is None or s.countries is None:
            return
        g = geometry_of(d)
        if g is None:
            return
        p = g.pointOnSurface().asPoint()
        client, source = self.context.esri(), s.countries
        token = self.context.token_for(source.url, source.token)
        from ..core.obs.api import areas_at

        def work(_task):
            return [a.code for a in areas_at(client, source, [(p.x(), p.y())], token) if a.code]

        def done(codes) -> None:
            if not codes:
                self.say(self.tr("形の中の点はどの国にも入っていない"))
                return
            if self.draft is not None:
                self._show_code(self.country, codes[0])
                self._form_edited()
                self.say(self.tr("国を {c} に").format(c=codes[0]))

        run_task(self.tr("国の判定"), work, done, lambda m: self.say(self.tr("国を判定できない: {m}").format(m=m)))

    # ---- drawing -------------------------------------------------------------------------------

    def _use_tool(self, which: Optional[str]) -> None:
        self._tool_for = which
        for key, button in (("new", self.buttons["new"]), ("redraw", self.redraw_button)):
            if key != which and button.isChecked():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
        if which is None:
            if self.canvas.mapTool() is self.draw_tool:
                self.canvas.unsetMapTool(self.draw_tool)
            return
        self._end_vertex_edit(keep=True)
        map_tools.take(self.canvas, self.draw_tool)
        self.say(self.tr("地図で頂点をクリック、ダブルクリックか右クリックで確定（Esc でやり直し）"))

    def _new_mode(self, on: bool) -> None:
        self._use_tool("new" if on else None)

    def _tool_changed(self, new_tool, _old=None) -> None:
        if new_tool is not self.draw_tool and self._tool_for is not None:
            self._use_tool(None)

    def _drawn(self, corners: list) -> None:
        rings = [[list(p) for p in corners] + [list(corners[0])]]
        which = self._tool_for
        self._use_tool(None)
        if which == "redraw" and self.draft is not None:
            self.draft = replace(self._read_form(), rings=rings)
            self.shape_changed = True
            self._show_shape()
            self._update_buttons()
            return
        self.start_new(rings=rings)
        self.say(self.tr("名前を入れて保存"))

    def _selected_rings(self) -> Optional[list]:
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.selectedFeatureCount():
            self.say(self.tr("作業中のレイヤーで面の地物を 1 つ選んでから"))
            return None
        feature = next(layer.getSelectedFeatures())
        shape = shape_of(feature.geometry(), to_wgs84(layer.crs()))
        if shape is None or not shape.polygons:
            self.say(self.tr("面（ポリゴン）の地物を選ぶ"))
            return None
        self._picked_name = next((str(feature[f]) for f in ("name", "NAME", "名前", "名称") if f in feature.fields().names() and feature[f]), "")
        return shape_rings(shape)

    def from_selection(self) -> None:
        rings = self._selected_rings()
        if rings is None:
            return
        self.start_new(name=self._picked_name, rings=rings)
        self.say(self.tr("名前を確かめて保存"))

    def shape_from_selection(self) -> None:
        if self.draft is None:
            return
        rings = self._selected_rings()
        if rings is None:
            return
        self.draft = replace(self._read_form(), rings=rings)
        self.shape_changed = True
        self._show_shape()
        self._update_buttons()
        self.say(self.tr("形を選んだ地物にした（保存で決まる）"))

    # ---- 頂点を直す: QGIS's vertex tool on a copy -----------------------------------------------------

    def _editing_vertices(self) -> bool:
        return self._edit_layer is not None

    def _vertex_mode(self, on: bool) -> None:
        if not on:
            self._end_vertex_edit(keep=True)
            return
        d = self.draft
        if d is None:
            self.vertex_button.setChecked(False)
            return
        if not d.has_shape:
            # A place name's shape (not read with the list): read, then edited.
            self.say(self.tr("形を取得中…"))

            def got(area: OrgArea) -> None:
                if self.draft is not None and self.draft.key == area.key:
                    self.draft = replace(self.draft, rings=area.rings)
                    self._start_vertex_edit()

            self.book.shape(d, got, lambda m: (self.vertex_button.setChecked(False), self.say(self.tr("形を取得できない: {m}").format(m=m))))
            return
        self._start_vertex_edit()

    def _start_vertex_edit(self) -> None:
        d = self.draft
        if d is None or not d.has_shape or self._edit_layer is not None:
            return
        self._use_tool(None)
        layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", self.tr("編集中: {name}").format(name=d.name or self.tr("新しい地域")), "memory")
        layer.setCustomProperty(EDIT_LAYER, 1)
        layer.setCustomProperty("skipMemoryLayersCheck", 1)
        g = QgsGeometry(geometry_of(d))
        if not g.isMultipart():
            g.convertToMultiType()
        f = QgsFeature(layer.fields())
        f.setGeometry(g)
        layer.dataProvider().addFeatures([f])
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        project.layerTreeRoot().insertLayer(0, layer)
        layer.startEditing()
        self._edit_layer = layer
        self.iface.setActiveLayer(layer)
        action = getattr(self.iface, "actionVertexTool", None)
        if callable(action) and action() is not None:
            action().trigger()
        self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.say(self.tr("頂点をドラッグで移動・辺をダブルクリックで追加・選んで Delete で削除。終わったら「頂点を直す」をもう一度押す"))

    def _end_vertex_edit(self, keep: bool) -> None:
        layer, self._edit_layer = self._edit_layer, None
        if layer is None:
            return
        try:
            if keep and self.draft is not None:
                feature = next(layer.getFeatures(), None)
                shape = shape_of(feature.geometry(), to_wgs84(layer.crs())) if feature is not None else None
                rings = shape_rings(shape) if shape is not None and shape.polygons else None
                if rings and rings != self.draft.rings:
                    self.draft = replace(self._read_form(), rings=rings)
                    self.shape_changed = True
            layer.rollBack()
            QgsProject.instance().removeMapLayer(layer.id())
        except RuntimeError:
            pass
        self.vertex_button.blockSignals(True)
        self.vertex_button.setChecked(False)
        self.vertex_button.blockSignals(False)
        self._show_shape()
        self._update_buttons()

    # ---- moving, the map -----------------------------------------------------------------------

    def zoom(self) -> None:
        areas = self.chosen() or ([self.draft] if self.draft is not None else [])
        missing = [a for a in areas if not a.has_shape and a.oid is not None]
        if missing:
            self.book.shape(missing[0], lambda _a: self.zoom(), lambda m: self.say(self.tr("形を取得できない: {m}").format(m=m)))
            return
        box = box_of(self.book, areas) if areas and areas[0] is not self.draft else (
            geometry_of(self.draft).boundingBox() if self.draft is not None and self.draft.has_shape else None)
        if box is None:
            return
        g = QgsGeometry.fromRect(box)
        g.transform(from_wgs84(self.canvas.mapSettings().destinationCrs()))
        extent = g.boundingBox()
        extent.scale(1.3)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def _map_toggled(self, on: bool) -> None:
        QgsSettings().setValue(ON_MAP_KEY, on)
        if on:
            self._place_layer(False)
        else:
            self._removing = True
            try:
                remove_layer()
            finally:
                self._removing = False
        self._update_buttons()

    def _place_layer(self, auto: bool) -> None:
        if self.settings is None or not self.wanted_on_map():
            self._update_buttons()
            return
        if self.book.list is None:
            self.book.want()
            return
        if auto and layer_of() is None and not self.book.interest_areas():
            self._update_buttons()
            return
        project = QgsProject.instance()
        clean = not project.isDirty()
        show_layer(self.book)
        select_on_map([a.key for a in self.chosen()])
        if auto and clean:
            from ..qgis_io.startup_layers import leave_unchanged
            leave_unchanged()
        self._show_shape()
        self._update_buttons()

    def _project_changed(self, *_args) -> None:
        self._edit_layer = None
        QTimer.singleShot(0, lambda: self._place_layer(True))

    def _layers_going(self, ids) -> None:
        layer = layer_of()
        if self._removing or layer is None or layer.id() not in set(ids):
            return
        if len(ids) < len(QgsProject.instance().mapLayers()):
            QgsSettings().setValue(ON_MAP_KEY, False)
        QTimer.singleShot(0, self._update_buttons)

    def open_categories(self) -> None:
        from .area_category_dialog import AreaCategoryDialog

        if self.dialog is None:
            self.dialog = AreaCategoryDialog(self.book, self.window())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.render()

    def close_down(self) -> None:
        for signal, slot in self._hooks:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._end_vertex_edit(keep=False)
        self._use_tool(None)
        self.canvas.scene().removeItem(self.band)
        self.draw_tool.remove()
        if self.dialog is not None:
            self.dialog.close()


def _swatch(colour: QColor):
    from qgis.PyQt.QtGui import QIcon, QPixmap

    pix = QPixmap(12, 12)
    pix.fill(colour)
    return QIcon(pix)
