"""
「関心地域・地点」 (tsudo, 2026-11-14: 「このレイヤーを編集するドックが欲しい」): the
person's own points and areas of interest (core/interests.py; in Esri, the
settings file's `interests`), which the 新着画像 cards name in 「写っている地物」
when an image covers them.

The dock lists them (名前・種類・色・メモ・更新日時; narrowed by a word).
「地点を追加」 is a click on the map, 「地域を追加」 a polygon drawn on it (a
click a corner, a double or right click closes it, Esc starts again),
「選んだ地物から」 the shape of the feature selected in the current layer. The
new item waits in the form below the list (its shape drawn on the map) until
it has a name and is saved. A chosen item is edited in the same form: name,
colour, note, 「形を描き直す」. 「削除」 deletes the chosen ones (asked first),
「移動」 (or a double click) shows them on the map. 「地図に表示」 keeps them as
two layers 「関心地点」「関心地域」, each item in its colour (remembered: shown
again when QGIS starts or another project opens, once the person has items).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from qgis.core import QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer, QgsWkbTypes
from qgis.gui import QgsColorButton, QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import QDateTime, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QWidget,
)

from ..core.interests import AREA, KIND_LABELS, POINT, Interest, matches, new_interest
from ..qgis_io.geometry import from_wgs84, shape_of, shape_rings, to_wgs84
from ..qgis_io.interests_io import InterestBook, box_of, geometry_of, layers_of, remove_layers, select_on_map, show_layers
from .catalog_overlay import DrawTool
from .docks import PluginDock, ask_no, plain_label, plain_tip
from .flow_layout import FlowLayout
from .list_views import column_menus

#: Whether the items are kept on the map (the person's choice; on until turned off).
ON_MAP_KEY = "mikage/interestsOnMap"


def _local(ms: Optional[int]) -> str:
    return QDateTime.fromMSecsSinceEpoch(int(ms)).toString("yyyy-MM-dd HH:mm") if ms else ""


class InterestsDock(PluginDock):
    """「関心地域・地点」."""

    def __init__(self, iface, context, open_settings: Callable[[], None], book: InterestBook, parent=None):
        super().__init__("SatObsInterests", "関心地域・地点", context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.book = book
        self.shown: list[Interest] = []
        #: The item in the form (a copy: a new one, or the chosen one as edited), None when the form is empty.
        self.draft: Optional[Interest] = None
        self._saving = False
        #: The layers are being taken away by the plugin itself (not the person turning them off).
        self._removing = False

        self.note = plain_label(QLabel())
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("名前・メモで絞り込み"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.render())
        self.reload_button = QPushButton(self.tr("最新を取得"))
        self.reload_button.setToolTip(self.tr("最新の関心地域と地点を取得（ほかの PC での変更も）"))
        self.reload_button.clicked.connect(lambda: self.book.load())
        top.addWidget(self.search, 1)
        top.addWidget(self.reload_button)
        self.body.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([self.tr("名前"), self.tr("種類"), self.tr("色"), self.tr("メモ"), self.tr("更新日時")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._chosen_changed)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.zoom())
        self.body.addWidget(self.table, 1)
        self.table_columns = column_menus("interests", self.table, self)

        self.buttons: dict[str, QPushButton] = {}
        flow = FlowLayout()
        for key, text, tip, slot, checkable in [
            ("point", self.tr("地点を追加"), self.tr("地図をクリックした所に新しい地点（名前を入れて保存）"), self._point_mode, True),
            ("area", self.tr("地域を追加"), self.tr("地図に多角形を描いて新しい地域。クリックで頂点、ダブルクリックか右クリックで確定、Esc でやり直し"),
             self._area_mode, True),
            ("selection", self.tr("選んだ地物から"), self.tr("作業中のレイヤーで選んだ地物の形から新しく作成（点は地点、面は地域）"), self.from_selection, False),
            ("delete", self.tr("削除"), self.tr("選んだものを削除"), self.delete, False),
            ("zoom", self.tr("移動"), self.tr("選んだものへ地図を移動（ダブルクリックでも）"), self.zoom, False),
            ("map", self.tr("地図に表示"), self.tr("「関心地点」「関心地域」のレイヤーとして地図に出す（次に QGIS を開いたときも）"), self._map_toggled, True),
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
        self.body.addWidget(holder)

        # The form: the new item, or the chosen one.
        self.form = QGroupBox()
        form = QFormLayout(self.form)
        self.name = QLineEdit()
        self.name.setPlaceholderText(self.tr("名前（カードの「写っている地物」に出る）"))
        self.name.textEdited.connect(lambda _t: self._form_edited())
        self.name.returnPressed.connect(self.save)
        self.color = QgsColorButton()
        self.color.setAllowOpacity(False)
        self.color.setShowNoColor(False)
        self.color.colorChanged.connect(lambda _c: self._form_edited())
        self.memo = QPlainTextEdit()
        self.memo.setMaximumHeight(64)
        self.memo.textChanged.connect(self._form_edited)
        shape_row = QHBoxLayout()
        self.shape = plain_label(QLabel())
        self.reshape_button = QPushButton(self.tr("形を描き直す"))
        self.reshape_button.setCheckable(True)
        self.reshape_button.setToolTip(self.tr("地点はクリックした所へ、地域は描き直した多角形に（保存で決まる）"))
        self.reshape_button.toggled.connect(self._reshape_mode)
        shape_row.addWidget(self.shape, 1)
        shape_row.addWidget(self.reshape_button)
        form.addRow(self.tr("名前"), self.name)
        form.addRow(self.tr("色"), self.color)
        form.addRow(self.tr("メモ"), self.memo)
        form.addRow(self.tr("形"), shape_row)
        actions = QHBoxLayout()
        self.save_button = QPushButton(self.tr("保存"))
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda _on=False: self.save())
        self.cancel_button = QPushButton(self.tr("取り消し"))
        self.cancel_button.setToolTip(self.tr("新しいものは捨て、選んだものは保存した状態に戻す"))
        self.cancel_button.clicked.connect(lambda _on=False: self.cancel())
        actions.addStretch(1)
        actions.addWidget(self.save_button)
        actions.addWidget(self.cancel_button)
        form.addRow(actions)
        self.body.addWidget(self.form)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        # Map tools: a click for a point, a polygon for an area; the item in the form drawn meanwhile.
        self.point_tool = QgsMapToolEmitPoint(self.canvas)
        self.point_tool.canvasClicked.connect(self._clicked)
        self.draw_tool = DrawTool(self.canvas, self._drawn)
        self._tool_for: Optional[str] = None  # "point", "area" or "reshape"
        self.draft_band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.draft_band.setStrokeColor(QColor("#f97316"))
        self.draft_band.setFillColor(QColor(249, 115, 22, 40))
        self.draft_band.setWidth(2)
        self.draft_band.setLineStyle(Qt.PenStyle.DashLine)
        self.draft_band.setZValue(955)
        self.draft_point = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PointGeometry)
        self.draft_point.setColor(QColor("#f97316"))
        self.draft_point.setIconSize(12)
        self.draft_point.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        self.draft_point.setWidth(3)
        self.draft_point.setZValue(955)

        project = QgsProject.instance()
        self._hooks = [(project.layersWillBeRemoved, self._layers_going), (project.readProject, self._project_changed),
                       (iface.newProjectCreated, self._project_changed), (self.canvas.mapToolSet, self._tool_changed)]
        for signal, slot in self._hooks:
            signal.connect(slot)
        book.changed.connect(self._book_changed)
        self.visibilityChanged.connect(lambda on: self._shown() if on else None)
        context.configChanged.connect(self._config_changed)
        self._config_changed()
        # Shown on the map again at QGIS's start (read in a task, which waits until QGIS is up).
        if self.wanted_on_map() and book.settings is not None:
            book.want()

    # ---- state ---------------------------------------------------------------------------------

    @property
    def settings(self):
        return self.book.settings

    def say(self, text: str) -> None:
        self.status.setText(text)

    def chosen(self) -> list[Interest]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.shown[r] for r in rows if 0 <= r < len(self.shown)]

    def choose(self, item_id: str) -> None:
        for i, item in enumerate(self.shown):
            if item.id == item_id:
                self.table.selectRow(i)
                return

    @staticmethod
    def wanted_on_map() -> bool:
        return QgsSettings().value(ON_MAP_KEY, True, type=bool)

    def _config_changed(self) -> None:
        s = self.settings
        user = self.book.user()
        if s is None:
            self.note.setText(self.tr("関心地域・地点は使えない（管理者に連絡: interests）"))
        elif not user:
            self.note.setText(self.tr("利用者名が分からないので使えない（管理者に連絡: editTracking.user）"))
        else:
            self.note.setText(self.tr("{user} さんの関心地域・地点（Esri に保存。どの PC でも同じ）").format(user=user))
        self.note.setVisible(True)
        self._clear_form()
        self.render()
        if s is not None and self.isVisible():
            self.book.want()

    def _shown(self) -> None:
        self.book.want()
        self.render()

    def _book_changed(self) -> None:
        if self.draft is not None and self.draft.oid is not None and self.book.items is not None and self.book.find(self.draft.id) is None:
            self._clear_form()  # deleted meanwhile
        self.render()
        self._place_layers(True)

    # ---- the list ------------------------------------------------------------------------------

    def render(self) -> None:
        book = self.book
        keep = [i.id for i in self.chosen()]
        self.shown = [i for i in book.items or [] if matches(i, self.search.text())]
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self.shown))
        for r, item in enumerate(self.shown):
            for c, text in enumerate([item.name, KIND_LABELS[item.kind], item.color, item.note.replace("\n", " "), _local(item.updated_at)]):
                cell = QTableWidgetItem(text)
                if c == 2:
                    colour = QColor(item.color)
                    if colour.isValid():
                        cell.setBackground(colour)
                        light = 0.299 * colour.red() + 0.587 * colour.green() + 0.114 * colour.blue() >= 140
                        cell.setForeground(QColor("#111111" if light else "#ffffff"))
                if c == 3 and item.note:
                    cell.setToolTip(plain_tip(item.note))
                self.table.setItem(r, c, cell)
        selection = self.table.selectionModel()
        flags = selection.SelectionFlag.Select | selection.SelectionFlag.Rows
        for r, item in enumerate(self.shown):
            if item.id in keep:
                selection.select(self.table.model().index(r, 0), flags)
        self.table.blockSignals(False)
        if book.settings is None:
            self.say("")
        elif book.loading:
            self.say(self.tr("取得中…"))
        elif book.items is None and book.failure:
            self.say(self.tr("取得に失敗: {m}").format(m=book.failure))
        elif book.items is not None and not self._saving:
            points = sum(1 for i in book.items if i.kind == POINT)
            self.say(self.tr("地点 {p} 件・地域 {a} 件").format(p=points, a=len(book.items) - points))
        self._update_buttons()

    def _update_buttons(self) -> None:
        s = self.settings
        ready = s is not None and self.book.items is not None and bool(self.book.user())
        chosen = self.chosen()
        self.buttons["point"].setEnabled(ready and POINT in s.kinds if s else False)
        self.buttons["area"].setEnabled(ready and AREA in s.kinds if s else False)
        self.buttons["selection"].setEnabled(ready)
        self.buttons["delete"].setEnabled(ready and bool(chosen) and not self._saving)
        self.buttons["zoom"].setEnabled(bool(chosen) or (self.draft is not None and self.draft.has_shape))
        self.buttons["map"].setEnabled(s is not None)
        self.buttons["map"].blockSignals(True)
        self.buttons["map"].setChecked(bool(layers_of()))
        self.buttons["map"].blockSignals(False)
        self.reload_button.setEnabled(s is not None and not self.book.loading)
        self.form.setVisible(self.draft is not None)
        self.save_button.setEnabled(self.draft is not None and not self._saving and self._changed())
        self.reshape_button.setEnabled(self.draft is not None and not self._saving)

    def _chosen_changed(self) -> None:
        chosen = self.chosen()
        select_on_map([i.id for i in chosen])
        if len(chosen) == 1:
            self._fill(replace(chosen[0]))
        elif self.draft is not None and self.draft.oid is not None:
            self._clear_form()
        self._update_buttons()

    # ---- the form ------------------------------------------------------------------------------

    def _fill(self, item: Interest) -> None:
        self.draft = item
        saved = item.oid is not None
        self.form.setTitle(self.tr("編集: {name}").format(name=item.name) if saved else self.tr("新しい{kind}").format(kind=KIND_LABELS[item.kind]))
        for w in (self.name, self.color, self.memo):
            w.blockSignals(True)
        self.name.setText(item.name)
        self.color.setColor(QColor(item.color) if QColor(item.color).isValid() else QColor(self.settings.color if self.settings else "#f97316"))
        self.memo.setPlainText(item.note)
        for w in (self.name, self.color, self.memo):
            w.blockSignals(False)
        self._show_shape()
        self._update_buttons()
        if not saved:
            self.reveal(self.form)
            self.name.setFocus()

    def _clear_form(self) -> None:
        self.draft = None
        self.reshape_button.setChecked(False)
        self._show_shape()
        self._update_buttons()

    def _form_edited(self) -> None:
        if self.draft is None:
            return
        self.draft = replace(self.draft, name=self.name.text().strip(), color=self.color.color().name(), note=self.memo.toPlainText().strip())
        self._update_buttons()

    def _changed(self) -> bool:
        """Whether the form differs from what is saved (a new item always does)."""
        d = self.draft
        saved = self.book.find(d.id) if d is not None else None
        if d is None:
            return False
        if saved is None:
            return True
        return (d.name, d.color, d.note, d.x, d.y, d.rings) != (saved.name, saved.color, saved.note, saved.x, saved.y, saved.rings)

    def _show_shape(self) -> None:
        d = self.draft
        self.shape.setText(d.shape_text() if d is not None else "")
        self.draft_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.draft_point.reset(QgsWkbTypes.GeometryType.PointGeometry)
        # Only what is not saved as it is shows here (the layer has the rest).
        if d is None or not d.has_shape or not self._changed():
            return
        g = geometry_of(d)
        if g is None:
            return
        g = QgsGeometry(g)
        g.transform(from_wgs84(self.canvas.mapSettings().destinationCrs()))
        if d.kind == POINT:
            self.draft_point.addPoint(g.asPoint())
        else:
            self.draft_band.setToGeometry(g, None)

    def save(self) -> None:
        d = self.draft
        if d is None or self._saving:
            return
        if not d.name.strip():
            self.say(self.tr("名前を入れてから保存"))
            self.name.setFocus()
            return
        if not d.has_shape:
            self.say(self.tr("形がない（「形を描き直す」で地図に）"))
            return
        self._saving = True
        self.say(self.tr("保存中…"))
        self._update_buttons()

        def done(saved: Interest) -> None:
            self._saving = False
            self.draft = None
            self.render()
            self.choose(saved.id)
            kept = self.book.find(saved.id)
            if kept is not None and [i.id for i in self.chosen()] == [saved.id]:
                self._fill(replace(kept))  # the form goes on showing it, as saved
            self._show_shape()
            self.say(self.tr("「{name}」を保存").format(name=saved.name))

        def failed(message: str) -> None:
            self._saving = False
            self._update_buttons()
            self.say(self.tr("保存に失敗: {m}").format(m=message))

        self.book.save(d, done, failed)

    def cancel(self) -> None:
        d = self.draft
        saved = self.book.find(d.id) if d is not None else None
        self.reshape_button.setChecked(False)
        if saved is None:
            self._clear_form()
            self.say(self.tr("新しい{kind}を取り消し").format(kind=KIND_LABELS[d.kind]) if d else "")
        else:
            self._fill(replace(saved))

    # ---- drawing -------------------------------------------------------------------------------

    def _use_tool(self, which: Optional[str]) -> None:
        """One of the map tools on (the other buttons off), or none."""
        self._tool_for = which
        for key, button in (("point", self.buttons["point"]), ("area", self.buttons["area"]), ("reshape", self.reshape_button)):
            if key != which and button.isChecked():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
        if which is None:
            for tool in (self.point_tool, self.draw_tool):
                if self.canvas.mapTool() is tool:
                    self.canvas.unsetMapTool(tool)
            return
        kind = self.draft.kind if which == "reshape" and self.draft is not None else (POINT if which == "point" else AREA)
        self.canvas.setMapTool(self.point_tool if kind == POINT else self.draw_tool)
        self.say(self.tr("地図をクリック（Esc かボタンでやめる）") if kind == POINT
                 else self.tr("地図で頂点をクリック、ダブルクリックか右クリックで確定（Esc でやり直し）"))

    def _point_mode(self, on: bool) -> None:
        self._use_tool("point" if on else None)

    def _area_mode(self, on: bool) -> None:
        self._use_tool("area" if on else None)

    def _reshape_mode(self, on: bool) -> None:
        self._use_tool("reshape" if on and self.draft is not None else None)

    def _tool_changed(self, new_tool, _old=None) -> None:
        # Another tool taken (QGIS's pan, another dock's): the buttons let go.
        if new_tool not in (self.point_tool, self.draw_tool) and self._tool_for is not None:
            self._use_tool(None)

    def _wgs(self):
        return to_wgs84(self.canvas.mapSettings().destinationCrs())

    def _clicked(self, point, _button=None) -> None:
        p = self._wgs().transform(point)
        which = self._tool_for
        self._use_tool(None)
        if which == "reshape" and self.draft is not None:
            self.draft = replace(self.draft, x=p.x(), y=p.y())
            self._show_shape()
            self._update_buttons()
            return
        self.table.clearSelection()
        self._fill(new_interest(POINT, self.settings, x=p.x(), y=p.y()))
        self.say(self.tr("名前を入れて保存"))

    def _drawn(self, corners: list) -> None:
        rings = [[list(p) for p in corners] + [list(corners[0])]]
        which = self._tool_for
        self._use_tool(None)
        if which == "reshape" and self.draft is not None:
            self.draft = replace(self.draft, rings=rings)
            self._show_shape()
            self._update_buttons()
            return
        self.table.clearSelection()
        self._fill(new_interest(AREA, self.settings, rings=rings))
        self.say(self.tr("名前を入れて保存"))

    def from_selection(self) -> None:
        """A new item of the shape of the feature selected in the current layer (a point: 地点, a polygon: 地域)."""
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.selectedFeatureCount():
            self.say(self.tr("作業中のレイヤーで地物を 1 つ選んでから"))
            return
        feature = next(layer.getSelectedFeatures())
        shape = shape_of(feature.geometry(), to_wgs84(layer.crs()))
        if shape is None:
            self.say(self.tr("選んだ地物に形がない"))
            return
        kinds = self.settings.kinds
        if shape.polygons and AREA in kinds:
            item = new_interest(AREA, self.settings, rings=shape_rings(shape))
        elif len(shape.vertices) == 1 and POINT in kinds:
            item = new_interest(POINT, self.settings, x=shape.vertices[0][0], y=shape.vertices[0][1])
        else:
            self.say(self.tr("点（地点）か面（地域）の地物を選ぶ（線は使えない）"))
            return
        name = next((str(feature[f]) for f in ("name", "NAME", "名前", "名称") if f in feature.fields().names() and feature[f]), "")
        item.name = name
        self.table.clearSelection()
        self._fill(item)
        more = layer.selectedFeatureCount() - 1
        self.say(self.tr("名前を確かめて保存") + (self.tr("（選択 {n} 件のうち最初の 1 件）").format(n=more + 1) if more else ""))

    # ---- deleting, moving, the map ---------------------------------------------------------------

    def delete(self, confirm: bool = True) -> None:
        chosen = self.chosen()
        if not chosen:
            return
        names = "、".join(i.name for i in chosen[:5]) + (self.tr(" ほか {n} 件").format(n=len(chosen) - 5) if len(chosen) > 5 else "")
        if confirm and not ask_no(self, self.tr("関心地域・地点の削除"), self.tr("{names} を削除（復元できない）").format(names=names), yes=self.tr("削除")):
            return
        self._saving = True
        self.say(self.tr("削除中…"))
        self._update_buttons()

        def done(gone: list, not_gone: list) -> None:
            self._saving = False
            self.render()
            self.say(self.tr("{n} 件を削除").format(n=len(gone)) + ("。" + self.tr("削除に失敗: ") + " / ".join(not_gone) if not_gone else ""))

        def failed(message: str) -> None:
            self._saving = False
            self._update_buttons()
            self.say(self.tr("削除に失敗: {m}").format(m=message))

        self.book.delete(chosen, done, failed)

    def zoom(self) -> None:
        items = self.chosen() or ([self.draft] if self.draft is not None else [])
        box = box_of(items)
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
            self._place_layers(False)
        else:
            self._removing = True
            try:
                remove_layers()
            finally:
                self._removing = False
        self._update_buttons()

    def _place_layers(self, auto: bool) -> None:
        """
        The items as the 「関心地点」「関心地域」 layers, when wanted: updated
        when there, else added (`auto`: by the plugin itself, as on QGIS's
        start or another project, only for a person with items, leaving the
        project unchanged).
        """
        s, items = self.settings, self.book.items
        if s is None or not self.wanted_on_map():
            self._update_buttons()
            return
        if items is None:
            self.book.want()
            return
        there = bool(layers_of())
        if auto and not there and not items:
            self._update_buttons()
            return
        project = QgsProject.instance()
        clean = not project.isDirty()
        show_layers(items, s, add=True)
        select_on_map([i.id for i in self.chosen()])
        if auto and clean:
            # Made or filled again by the plugin itself (as a project kept them empty): the project not changed by it.
            from ..qgis_io.startup_layers import leave_unchanged
            leave_unchanged()
        self._update_buttons()

    def _project_changed(self, *_args) -> None:
        # Once QGIS has set the project up: the layers back (or filled again, when the project kept them empty).
        QTimer.singleShot(0, lambda: self._place_layers(True))

    def _layers_going(self, ids) -> None:
        """The person took the layers away in the layer panel: not shown again until 「地図に表示」 (a whole project closing aside)."""
        mine = {layer.id() for layer in layers_of().values()}
        if self._removing or not mine or not mine & set(ids):
            return
        if len(ids) < len(QgsProject.instance().mapLayers()):
            QgsSettings().setValue(ON_MAP_KEY, False)
        QTimer.singleShot(0, self._update_buttons)

    def close_down(self) -> None:
        for signal, slot in self._hooks:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._use_tool(None)
        for band in (self.draft_band, self.draft_point):
            self.canvas.scene().removeItem(band)
        self.draw_tool.remove()
