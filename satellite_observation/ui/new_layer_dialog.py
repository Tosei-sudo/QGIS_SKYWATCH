"""
「新規スクラッチレイヤー（テンプレート）」, as the viewer's new-layer-dialog.ts:
an empty memory layer designed by the person, started from a template
(built in, from the settings file's `layerTemplates` or kept by the person)
and changed from there. They name it, choose what it holds (points, lines,
polygons or their multi forms) and its CRS, and define its attributes (name,
alias, type, 必須, a default, a length limit, a range and allowed values).
Attributes can also be copied from an open vector layer. The layer is added
to the project and, when asked, editing starts with the add-feature tool.

QGIS's own 「新規一時スクラッチレイヤー」 dialog gets a 「テンプレート」 row
too (ScratchDialogHook): picking one there opens this dialog with it.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsMapLayer, QgsProject
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.layer_templates import (
    BUILTIN_TEMPLATES, GEOMETRY_NAMES, TYPE_NAMES, FieldDesign, LayerTemplate, copy_template, design_fields, summary,
)
from ..qgis_io import scratch_layer as store
from ..qgis_io.scratch_layer import field_design_of, geometry_of, scratch_layer

#: The table's columns: key, heading, tooltip.
COLUMNS = [
    ("name", "属性名", ""),
    ("alias", "エイリアス", "属性テーブル・フォームでの表示名（空欄は属性名のまま）"),
    ("type", "型", ""),
    ("required", "必須", "値の空欄を不可に"),
    ("defaultValue", "既定値", "新しい地物に最初から入る値（日付は 2026-10-17 や 2026-10-17 09:30）"),
    ("length", "文字数の上限", "文字列のみ"),
    ("min", "最小", "数値のみ"),
    ("max", "最大", "数値のみ"),
    ("codes", "候補値", "例: 1=住宅, 2=店舗（値だけでも可）。フォームで選べる値"),
]
COL = {key: i for i, (key, _, _) in enumerate(COLUMNS)}
#: Where a template comes from, as the list groups them.
SOURCES = {"builtin": "標準", "config": "組織の設定", "saved": "保存済み"}


def offered_templates(config_templates: list) -> list[tuple[str, LayerTemplate]]:
    """The templates offered: built in, from the settings file, then kept by the person (where each comes from, the template)."""
    return ([("builtin", t) for t in BUILTIN_TEMPLATES] + [("config", t) for t in config_templates]
            + [("saved", t) for t in store.saved_templates()])


def _number_text(v) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else str(v)


class NewLayerDialog(QDialog):
    """The form: a template, the layer's name, geometry and CRS, and its attributes."""

    def __init__(self, iface, templates: Callable[[], list], parent=None):
        super().__init__(parent)
        self.iface = iface
        #: The organisation's templates (the settings file's), asked for each time the list is filled.
        self.config_templates = templates
        self.templates: list[tuple[str, LayerTemplate]] = []
        self._layers: list[str] = []
        self.setWindowTitle(self.tr("新規スクラッチレイヤー（テンプレート）"))
        self.setObjectName("SatObsNewLayer")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.template = QComboBox()
        self.template.setObjectName("template")
        self.template.activated.connect(lambda _: self.apply_template())
        form.addRow(self.tr("テンプレート"), self.template)
        self.template_note = QLabel()
        self.template_note.setWordWrap(True)
        form.addRow("", self.template_note)
        self.title = QLineEdit()
        form.addRow(self.tr("名前"), self.title)
        self.geometry = QComboBox()
        for value, name in GEOMETRY_NAMES.items():
            self.geometry.addItem(name, value)
        form.addRow(self.tr("ジオメトリ"), self.geometry)
        self.crs = QgsProjectionSelectionWidget()
        form.addRow(self.tr("座標系"), self.crs)
        layout.addLayout(form)

        layout.addWidget(QLabel(self.tr("属性")))
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([self.tr(h) for _, h, _ in COLUMNS])
        for i, (_, _, tip) in enumerate(COLUMNS):
            if tip:
                self.table.horizontalHeaderItem(i).setToolTip(self.tr(tip))
        self.table.horizontalHeader().setSectionResizeMode(COL["codes"], QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)
        self.empty = QLabel(self.tr("属性なし（属性なしでも作成可能）"))
        layout.addWidget(self.empty)
        row = QHBoxLayout()
        add = QPushButton(self.tr("＋ 属性を追加"))
        add.clicked.connect(lambda: self._edit_new_row())
        row.addWidget(add)
        self.remove_button = QPushButton(self.tr("選択した属性を削除"))
        self.remove_button.clicked.connect(self.remove_selected)
        row.addWidget(self.remove_button)
        self.copy = QComboBox()
        self.copy.setObjectName("copy")
        self.copy.activated.connect(lambda i: self.copy_from(self.copy.itemData(i)) if self.copy.itemData(i) else None)
        row.addWidget(self.copy, 1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.save_button = QPushButton(self.tr("テンプレートとして保存"))
        self.save_button.setToolTip(self.tr("名前・ジオメトリ・属性をこの QGIS のユーザープロファイルに保存（レイヤーの名前がテンプレート名、同名は上書き）"))
        self.save_button.clicked.connect(lambda: self.save_template())
        row.addWidget(self.save_button)
        self.drop_button = QPushButton(self.tr("テンプレートを削除"))
        self.drop_button.setToolTip(self.tr("選択中の保存済みテンプレートを削除"))
        self.drop_button.clicked.connect(lambda: self.drop_template())
        row.addWidget(self.drop_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.draw = QCheckBox(self.tr("作成後すぐ地図に作図開始"))
        self.draw.setChecked(True)
        layout.addWidget(self.draw)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(self.tr("キャンセル"))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.create_button = QPushButton(self.tr("作成"))
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(lambda: self.create())
        buttons.addWidget(self.create_button)
        layout.addLayout(buttons)

    # The templates.

    def all_templates(self) -> list[tuple[str, LayerTemplate]]:
        """The templates offered: built in, from the settings file, then kept by the person."""
        return offered_templates(self.config_templates())

    def list_templates(self) -> None:
        """Fills the template list, grouped by where they come from (a heading for each group)."""
        self.templates = self.all_templates()
        self.template.clear()
        for source, label in SOURCES.items():
            group = [(i, t) for i, (s, t) in enumerate(self.templates) if s == source]
            if not group:
                continue
            self.template.addItem(f"―― {self.tr(label)} ――", None)
            self.template.model().item(self.template.count() - 1).setEnabled(False)
            for i, t in group:
                self.template.addItem(t.name, i)
                if t.description:
                    self.template.setItemData(self.template.count() - 1, t.description, Qt.ItemDataRole.ToolTipRole)

    def choose(self, source: str, name: str) -> bool:
        """Picks a template by where it comes from and its name, and fills the form with it."""
        for row in range(self.template.count()):
            i = self.template.itemData(row)
            if i is not None and self.templates[i][0] == source and self.templates[i][1].name == name:
                self.template.setCurrentIndex(row)
                self.apply_template()
                return True
        return False

    def chosen(self) -> Optional[tuple[str, LayerTemplate]]:
        i = self.template.currentData()
        return self.templates[i] if i is not None and i < len(self.templates) else None

    def apply_template(self) -> None:
        """Replaces the name, geometry and attributes with those of the chosen template, to change from there."""
        chosen = self.chosen()
        if chosen is None:
            return
        source, template = chosen
        template = copy_template(template)
        self.title.setText(template.title)
        note = template.description
        if template.geometryType:
            self.geometry.setCurrentIndex(self.geometry.findData(template.geometryType))
        else:
            self.geometry.setCurrentIndex(self.geometry.findData("Point"))
            note = (note + "。" if note else "") + self.tr("ジオメトリ指定なしのテンプレート（QGIS のレイヤーは 1 種類のため、ここで選択）")
        self.set_fields(template.fields)
        self.template_note.setText(note)
        self.template_note.setVisible(bool(note))
        self.drop_button.setVisible(source == "saved")
        self._say("")

    # The attributes.

    def set_fields(self, fields: list[FieldDesign]) -> None:
        self.table.setRowCount(0)
        for f in fields:
            self.add_field(f)
        self._update()

    def add_field(self, design: FieldDesign) -> int:
        """Adds an attribute row; its number."""
        r = self.table.rowCount()
        self.table.insertRow(r)
        texts = {"name": design.name, "alias": design.alias, "defaultValue": design.defaultValue, "length": _number_text(design.length),
                 "min": _number_text(design.min), "max": _number_text(design.max), "codes": design.codes}
        for key, text in texts.items():
            self.table.setItem(r, COL[key], QTableWidgetItem(text))
        required = QTableWidgetItem()
        required.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        required.setCheckState(Qt.CheckState.Checked if design.required else Qt.CheckState.Unchecked)
        self.table.setItem(r, COL["required"], required)
        kind = QComboBox()
        for value, name in TYPE_NAMES.items():
            kind.addItem(self.tr(name), value)
        kind.setCurrentIndex(max(0, kind.findData(design.type)))
        kind.currentIndexChanged.connect(lambda _, k=kind: self._type_changed(k))
        self.table.setCellWidget(r, COL["type"], kind)
        self._shade(r)
        self._update()
        return r

    def _edit_new_row(self) -> None:
        r = self.add_field(FieldDesign(""))
        self.table.setCurrentCell(r, COL["name"])
        self.table.editItem(self.table.item(r, COL["name"]))

    def _type_changed(self, combo: QComboBox) -> None:
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, COL["type"]) is combo:
                self._shade(r)

    def _shade(self, r: int) -> None:
        """Greys out (and locks) the cells the row's type does not use."""
        type_ = self.table.cellWidget(r, COL["type"]).currentData()
        used = {"length": type_ == "string", "min": type_ in ("integer", "double"), "max": type_ in ("integer", "double"), "codes": type_ != "date"}
        for key, on in used.items():
            item = self.table.item(r, COL[key])
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            item.setFlags(flags | Qt.ItemFlag.ItemIsEditable if on else Qt.ItemFlag.ItemIsSelectable)

    def remove_selected(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)
        self._update()

    def fields(self) -> list[FieldDesign]:
        """The attributes as filled in (the cells the type does not use are left out)."""
        out = []
        for r in range(self.table.rowCount()):
            text = {key: (self.table.item(r, COL[key]).text() if self.table.item(r, COL[key]) else "") for key in COL if key not in ("type", "required")}
            type_ = self.table.cellWidget(r, COL["type"]).currentData()

            def number(key: str):
                t = text[key].strip()
                if not t:
                    return None
                try:
                    return float(t)
                except ValueError:
                    return float("nan")

            out.append(FieldDesign(
                name=text["name"], type=type_, alias=text["alias"],
                required=self.table.item(r, COL["required"]).checkState() == Qt.CheckState.Checked,
                length=number("length") if type_ == "string" else None,
                min=number("min") if type_ in ("integer", "double") else None,
                max=number("max") if type_ in ("integer", "double") else None,
                codes="" if type_ == "date" else text["codes"],
                defaultValue=text["defaultValue"],
            ))
        return out

    def design(self) -> LayerTemplate:
        """The design as filled in, as a template named after the layer."""
        title = self.title.text().strip()
        return LayerTemplate(title, title, self.geometry.currentData(), self.fields())

    # Copying from an open layer.

    def list_layers(self) -> None:
        layers = [layer for layer in QgsProject.instance().mapLayers().values()
                  if layer.type() == QgsMapLayer.LayerType.VectorLayer and layer.fields().count() > 0]
        self.copy.clear()
        self.copy.addItem(self.tr("ほかのレイヤーの属性をコピー…"), None)
        for layer in sorted(layers, key=lambda layer: layer.name()):
            self.copy.addItem(layer.name(), layer.id())
        self.copy.setVisible(bool(layers))

    def copy_from(self, layer_id: str) -> None:
        """Replaces the attributes with those of a layer, and takes its geometry type and CRS (not its features)."""
        layer = QgsProject.instance().mapLayer(layer_id)
        self.copy.setCurrentIndex(0)
        if layer is None:
            return
        provider = layer.dataProvider()
        keys = set(provider.pkAttributeIndexes()) if provider else set()
        fields = [field_design_of(layer, i) for i in range(layer.fields().count())
                  if i not in keys and layer.fields().fieldOrigin(i) != layer.fields().FieldOrigin.OriginExpression
                  and layer.fields().at(i).name().lower() not in ("fid", "geom", "geometry", "shape")]
        self.set_fields(fields)
        geometry = geometry_of(layer)
        if geometry:
            self.geometry.setCurrentIndex(self.geometry.findData(geometry))
        if layer.crs().isValid():
            self.crs.setCrs(layer.crs())
        self._say(self.tr("「{0}」の属性 {1} 個をコピー（地物はコピー対象外）").format(layer.name(), len(fields)))

    # Saving designs.

    def save_template(self) -> bool:
        """Keeps the design as filled in as a template, named after the layer."""
        design = self.design()
        if not design.name:
            self._say(self.tr("テンプレート名としてレイヤーの名前を入力"), error=True)
            return False
        design.description = summary(design.geometryType, design.fields)
        store.save_template(design)
        self.list_templates()
        self.choose("saved", design.name)
        self._say(self.tr("テンプレート「{0}」を保存").format(design.name))
        return True

    def drop_template(self) -> None:
        """Removes the chosen kept template; the design being filled in stays."""
        chosen = self.chosen()
        if chosen is None or chosen[0] != "saved":
            return
        store.remove_template(chosen[1].name)
        self.list_templates()
        self.template.setCurrentIndex(1)
        self.drop_button.setVisible(False)
        self.template_note.setText(BUILTIN_TEMPLATES[0].description)
        self._say(self.tr("テンプレート「{0}」を削除（入力中の設計はそのまま）").format(chosen[1].name))

    # Opening and making.

    def open_with(self, source: str = "builtin", name: Optional[str] = None) -> "NewLayerDialog":
        """Opens the dialog with a fresh design: the blank template, or the one named."""
        self.list_templates()
        self.list_layers()
        crs = QgsProject.instance().crs()
        self.crs.setCrs(crs if crs.isValid() else QgsCoordinateReferenceSystem("EPSG:4326"))
        if not (name and self.choose(source, name)):
            self.template.setCurrentIndex(1)
            self.apply_template()
        self.show()
        self.raise_()
        self.activateWindow()
        self.title.selectAll()
        self.title.setFocus()
        return self

    def create(self):
        """Makes the layer and adds it to the project (the layer); keeps the dialog open to show what is wrong (None)."""
        design = self.design()
        specs = design_fields(self.title.text(), design.fields)
        if specs and isinstance(specs[0], str):
            self._say("。".join(specs), error=True)
            return None
        crs = self.crs.crs()
        if not crs.isValid():
            self._say(self.tr("座標系を選択"), error=True)
            return None
        layer = scratch_layer(self.title.text(), design.geometryType, crs, specs)
        QgsProject.instance().addMapLayer(layer)
        self.iface.setActiveLayer(layer)
        if self.draw.isChecked():
            layer.startEditing()
            add = getattr(self.iface, "actionAddFeature", None)
            if add is not None:
                add().trigger()
        self.accept()
        return layer

    def _update(self) -> None:
        self.empty.setVisible(self.table.rowCount() == 0)
        self.table.setVisible(self.table.rowCount() > 0)

    def _say(self, text: str, error: bool = False) -> None:
        self.note.setText(text)
        self.note.setStyleSheet("color: #dc2626;" if error else "")


class ScratchDialogHook(QObject):
    """
    Adds a 「テンプレート」 row to QGIS's own 「新規一時スクラッチレイヤー」
    dialog (found when focus moves into it); picking a template there closes
    it and opens 「新規スクラッチレイヤー（テンプレート）」 with that template.
    """

    CLASS = "QgsNewMemoryLayerDialog"
    MARK = "SatObsTemplateRow"

    def __init__(self, open_with: Callable[[str, str], None], templates: Callable[[], list], parent=None):
        super().__init__(parent)
        self.open_with = open_with
        self.templates = templates
        QApplication.instance().focusChanged.connect(self._focus)

    def close_down(self) -> None:
        try:
            QApplication.instance().focusChanged.disconnect(self._focus)
        except (TypeError, RuntimeError):
            pass
        for widget in QApplication.topLevelWidgets():
            row = widget.findChild(QWidget, self.MARK)
            if row is not None:
                row.deleteLater()

    def _focus(self, _old, new) -> None:
        if new is None:
            return
        window = new.window()
        if window.metaObject().className() == self.CLASS and window.findChild(QWidget, self.MARK) is None:
            self.attach(window)

    def attach(self, dialog: QWidget) -> Optional[QWidget]:
        """Puts the row at the top of `dialog` (nothing when its layout is not the expected one)."""
        layout = dialog.layout()
        if layout is None or not hasattr(layout, "insertWidget") and not hasattr(layout, "addWidget"):
            return None
        row = QWidget(dialog)
        row.setObjectName(self.MARK)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel(self.tr("テンプレート")))
        combo = QComboBox(row)
        combo.addItem(self.tr("使わない（QGIS 標準）"), None)
        for source, template in self.templates():
            combo.addItem(template.name, (source, template.name))
            if template.description:
                combo.setItemData(combo.count() - 1, template.description, Qt.ItemDataRole.ToolTipRole)
        combo.setToolTip(self.tr("選ぶとテンプレート付きの作成画面に切り替え（エイリアス・必須・候補値・既定値も設定）"))
        h.addWidget(combo, 1)

        def picked(i: int) -> None:
            data = combo.itemData(i)
            if not data:
                return
            dialog.reject()
            QTimer.singleShot(0, lambda: self.open_with(*data))

        combo.activated.connect(picked)
        try:
            # QGridLayout (QGIS 3.34's): a row above the others.
            if hasattr(layout, "addWidget") and hasattr(layout, "rowCount") and hasattr(layout, "itemAtPosition"):
                self._grid_insert(layout, row)
            else:
                layout.insertWidget(0, row)
        except Exception:  # noqa: BLE001 (QGIS's dialog may change; the row is a convenience)
            row.deleteLater()
            return None
        return row

    @staticmethod
    def _grid_insert(layout, row: QWidget) -> None:
        """Moves every item of a grid one row down and puts `row` at the top, across all columns."""
        items = []
        for i in reversed(range(layout.count())):
            r, c, rs, cs = layout.getItemPosition(i)
            items.append((layout.takeAt(i), r, c, rs, cs))
        columns = max([c + cs for _, _, c, _, cs in items] or [1])
        layout.addWidget(row, 0, 0, 1, columns)
        for item, r, c, rs, cs in reversed(items):
            layout.addItem(item, r + 1, c, rs, cs)
