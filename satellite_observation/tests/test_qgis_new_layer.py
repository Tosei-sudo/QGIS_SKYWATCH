"""
「新規スクラッチレイヤー（テンプレート）」 in QGIS (the viewer's new-layer spec):
templates built in, from the settings file and kept by the person; the
attributes' aliases, 必須, ranges, allowed values and defaults on the made
memory layer; copying another layer's attributes; and the 「テンプレート」 row
in QGIS's own scratch layer dialog.
"""
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsEditorWidgetSetup, QgsFieldConstraints, QgsProject, QgsSettings, QgsVectorLayer, QgsVectorLayerUtils, QgsWkbTypes,
)
from qgis.gui import QgsNewMemoryLayerDialog  # noqa: E402
from qgis.PyQt.QtWidgets import QComboBox, QWidget  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

HERE = os.path.dirname(__file__)


@pytest.fixture
def plugin():
    QgsSettings().remove("satellite_observation")
    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    assert p.api.problems == []
    yield p, iface
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def names(dialog):
    return [dialog.template.itemText(i) for i in range(dialog.template.count())]


def test_sits_after_qgis_own_scratch_layer_in_the_create_layer_menu(plugin):
    p, iface = plugin
    assert [a.text() for a in iface.new_layer_menu.actions()] == [
        "新規 GeoPackage レイヤー…", "新規一時スクラッチレイヤー…", "新規スクラッチレイヤー（テンプレート）…", "新規仮想レイヤー…"]
    p.unload()
    assert [a.objectName() for a in iface.new_layer_menu.actions()] == ["mActionNewGeoPackageLayer", "mActionNewMemoryLayer", "mActionNewVirtualLayer"]
    p.initGui()


def test_lists_the_templates_by_where_they_come_from_and_opens_with_the_blank_one(plugin):
    p, _ = plugin
    dialog = p.api.new_scratch_layer()
    assert names(dialog) == ["―― 標準 ――", "空のレイヤー", "航跡（生存圏計算用）", "調査地点", "目標・物体", "関心区域（AOI）", "被害状況", "経路・測線", "注記",
                             "―― 組織の設定 ――", "巡回記録"]
    assert not dialog.template.model().item(0).isEnabled()
    assert dialog.title.text() == "新規レイヤー" and dialog.geometry.currentData() == "Point"
    assert [f.name for f in dialog.fields()] == ["name"]
    assert dialog.template_note.text() == "テキスト属性 name のみ"


def test_makes_the_layer_of_a_template_with_aliases_constraints_widgets_and_defaults(plugin):
    p, iface = plugin
    added = []
    iface.add_feature.triggered.connect(lambda: added.append(True))
    dialog = p.api.new_scratch_layer("目標・物体")
    assert dialog.title.text() == "目標"
    layer = dialog.create()
    assert layer is not None and not dialog.isVisible()
    assert QgsProject.instance().mapLayer(layer.id()) is layer and iface.activeLayer() is layer
    assert layer.providerType() == "memory" and QgsWkbTypes.displayString(layer.wkbType()) == "Point"
    assert layer.isEditable() and added == [True]
    fields = layer.fields()
    assert [f.name() for f in fields] == ["kind", "count", "confidence", "observed", "image", "note"]
    assert [fields.at(i).alias() for i in range(fields.count())] == ["種別", "数", "確度", "撮影日時", "画像名", "備考"]
    assert fields.at(0).constraints().constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull
    assert fields.at(1).editorWidgetSetup().type() == "Range" and fields.at(1).editorWidgetSetup().config()["Min"] == 0
    confidence = fields.at(2).editorWidgetSetup()
    assert confidence.type() == "ValueMap" and confidence.config()["map"] == [{"確実": "1"}, {"可能性大": "2"}, {"可能性あり": "3"}]
    assert fields.at(3).isDateOrTime()
    feature = QgsVectorLayerUtils.createFeature(layer)
    assert feature["count"] == 1 and feature["confidence"] == 2
    # The constraints check what is typed.
    feature["kind"] = "戦車"
    feature["confidence"] = 9
    ok, errors = QgsVectorLayerUtils.validateAttribute(layer, feature, 2)
    assert not ok
    feature["confidence"] = 3
    assert QgsVectorLayerUtils.validateAttribute(layer, feature, 2)[0]


def test_a_template_without_geometry_and_a_date_default(plugin):
    p, _ = plugin
    dialog = p.api.new_scratch_layer("注記")
    assert dialog.geometry.currentData() == "Point" and "ジオメトリ指定なし" in dialog.template_note.text()
    dialog.geometry.setCurrentIndex(dialog.geometry.findData("MultiPolygon"))
    r = dialog.add_field(dialog.fields()[0].__class__("seen", "date", "確認日", defaultValue="2026-10-17 09:30"))
    assert r == 3
    dialog.draw.setChecked(False)
    layer = dialog.create()
    assert QgsWkbTypes.displayString(layer.wkbType()) == "MultiPolygon" and not layer.isEditable()
    value = QgsVectorLayerUtils.createFeature(layer)["seen"]
    assert value.toString("yyyy-MM-dd HH:mm") == "2026-10-17 09:30"


def test_the_organisations_template_from_the_settings_file(plugin):
    p, _ = plugin
    dialog = p.api.new_scratch_layer("巡回記録", "config")
    assert dialog.title.text() == "巡回" and dialog.geometry.currentData() == "LineString"
    layer = dialog.create()
    assert [f.name() for f in layer.fields()] == ["team", "result", "patrolled"]
    assert QgsVectorLayerUtils.createFeature(layer)["result"] == 1


def test_says_what_is_wrong_and_stays_open(plugin):
    p, _ = plugin
    dialog = p.api.new_scratch_layer()
    dialog.title.setText(" ")
    dialog.add_field(dialog.fields()[0].__class__("NAME"))
    assert dialog.create() is None and dialog.isVisible()
    assert dialog.note.text() == "レイヤーの名前を入力。属性「NAME」が重複（大文字・小文字の区別なし）"
    assert QgsProject.instance().mapLayers() == {}


def test_keeps_and_removes_the_persons_templates(plugin):
    p, _ = plugin
    dialog = p.api.new_scratch_layer("調査地点")
    dialog.title.setText("私の地点")
    dialog.geometry.setCurrentIndex(dialog.geometry.findData("MultiPoint"))
    assert dialog.save_template()
    assert names(dialog)[-2:] == ["―― 保存済み ――", "私の地点"]
    assert dialog.chosen()[0] == "saved" and dialog.drop_button.isVisibleTo(dialog)
    assert dialog.note.text() == "テンプレート「私の地点」を保存"
    dialog.close()
    # Kept in the profile: a new dialog (and QGIS's own dialog's row) offers it.
    dialog = p.api.new_scratch_layer("私の地点", "saved")
    assert dialog.geometry.currentData() == "MultiPoint" and [f.name for f in dialog.fields()] == ["name", "kind", "surveyed", "note"]
    assert dialog.template_note.text() == "マルチポイント・属性 4 個"
    dialog.drop_template()
    assert "私の地点" not in names(dialog) and dialog.note.text() == "テンプレート「私の地点」を削除（入力中の設計はそのまま）"
    assert dialog.title.text() == "私の地点"


def test_copies_the_attributes_of_an_open_layer(plugin):
    p, _ = plugin
    source = QgsVectorLayer("Polygon?crs=EPSG:6677&field=fid:integer&field=label:string(20)&field=level:integer&field=area:double&field=day:date", "区域", "memory")
    source.setFieldAlias(1, "名称")
    source.setFieldConstraint(1, QgsFieldConstraints.Constraint.ConstraintNotNull)
    source.setEditorWidgetSetup(2, QgsEditorWidgetSetup("ValueMap", {"map": [{"高": "1"}, {"低": "2"}]}))
    source.setEditorWidgetSetup(3, QgsEditorWidgetSetup("Range", {"Min": 0.0, "Max": 100.0}))
    QgsProject.instance().addMapLayer(source)
    dialog = p.api.new_scratch_layer()
    assert dialog.copy.itemText(1) == "区域"
    dialog.copy_from(source.id())
    assert [(f.name, f.type, f.alias, f.required, f.length, f.min, f.max, f.codes) for f in dialog.fields()] == [
        ("label", "string", "名称", True, 20, None, None, ""),
        ("level", "integer", "", False, None, None, None, "1=高, 2=低"),
        ("area", "double", "", False, None, 0, 100, ""),
        ("day", "date", "", False, None, None, None, ""),
    ]
    assert dialog.geometry.currentData() == "Polygon" and dialog.crs.crs().authid() == "EPSG:6677"
    assert dialog.note.text() == "「区域」の属性 4 個をコピー（地物はコピー対象外）"
    layer = dialog.create()
    assert layer.crs().authid() == "EPSG:6677" and layer.featureCount() == 0


def test_qgis_own_scratch_layer_dialog_gets_a_template_row(plugin):
    p, iface = plugin
    native = QgsNewMemoryLayerDialog(iface.window)
    row = p.scratch_hook.attach(native)
    assert row is not None and native.layout().itemAtPosition(0, 0).widget() is row
    assert native.layout().itemAtPosition(1, 1).widget().objectName() == "mNameLineEdit"
    combo = row.findChild(QComboBox)
    assert combo.itemText(0) == "使わない（QGIS 標準）" and combo.itemText(9) == "巡回記録"
    native.show()
    combo.setCurrentIndex(4)
    combo.activated.emit(4)
    assert not native.isVisible()
    assert wait_for(lambda: p.new_layer is not None and p.new_layer.isVisible())
    assert p.new_layer.title.text() == "目標"
    # Focus moving into QGIS's dialog is what adds the row.
    other = QgsNewMemoryLayerDialog(iface.window)
    p.scratch_hook._focus(None, other.findChild(QWidget, "mNameLineEdit"))
    assert other.findChild(QWidget, "SatObsTemplateRow") is not None
