"""
アペンド（既存レイヤーへ追加） in QGIS: the window (inputs, target, field map
and its cells, kept maps, checks, the write and its taking back) on QGIS
layers and an ArcGIS feature layer (a fake one), the CRS and the kinds of
geometry, the plugin's own tables kept for 管理者, and `mikage:append` in the
Processing toolbox.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsSettings, QgsVectorLayer,
)

from mikage.core import append as ap  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.features.maps import Maps  # noqa: E402
from mikage.qgis_io import append_io, settings  # noqa: E402
from mikage.qgis_io.prefs import maps as prefs  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui import append_dialog  # noqa: E402

URL = "https://example.com/arcgis/rest/services/Things/FeatureServer/0"
GLOSSARY = "https://example.com/arcgis/rest/services/Glossary/FeatureServer/0"


@pytest.fixture
def plugin(tmp_path):
    QgsSettings().remove("mikage")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"glossary": GLOSSARY, "layers": [{"type": "esri", "url": URL[:-2], "layer": "0"}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    iface = FakeIface()
    p = small_plugin(iface, Maps)
    p.initGui()
    yield p, iface, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def layer(kind, fields, rows, name, crs="EPSG:4326"):
    lyr = QgsVectorLayer(f"{kind}?crs={crs}&" + "&".join(f"field={f}" for f in fields), name, "memory")
    features = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setAttributes(attrs)
        if wkt:
            f.setGeometry(QgsGeometry.fromWkt(wkt))
        features.append(f)
    lyr.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def ships():
    return layer("MultiPoint", ["name:string", "kind:integer", "size:double", "memo:string"], [
        ("MultiPoint((139 35))", ["ふね", 1, 120.5, "港"]),
        ("MultiPoint((139.1 35.1))", ["くるま", 2, 4.2, None]),
        ("MultiPoint((139.2 35),(139.3 35))", ["ふたつ", 1, 1, "2 点"]),
        (None, ["かたちなし", 1, 2, ""]),
    ], "入力")


def things(crs="EPSG:3857"):
    lyr = layer("Point", ["NAME:string(8)", "KIND:integer", "SIZE_M:double", "NOTE:string(20)"], [], "追加先", crs)
    return lyr


def dialog_of(p):
    d = p.open_append()
    assert isinstance(d, append_dialog.AppendDialog) and d.isVisible()
    return d


def checked(d):
    assert d.check()
    assert wait_for(lambda: d.plan is not None or "失敗" in d.status.text())
    return d.plan


def test_the_menu_entry_opens_the_window_with_the_active_layer_offered(plugin):
    p, iface, _ = plugin
    source = ships()
    iface.setActiveLayer(source) if hasattr(iface, "setActiveLayer") else None
    action = next(a for a in p.actions if a.text() == "アペンド（既存レイヤーへ追加）…")
    action.trigger()
    d = p.append_dialog
    assert d.isVisible() and d.windowTitle() == "アペンド（既存レイヤーへ追加）"
    texts = [d.target_select.itemText(i) for i in range(d.target_select.count())]
    # The project's layers, the settings file's ArcGIS tables, then a URL.
    assert "レイヤー: 入力" in texts and texts[-1] == "ArcGIS フィーチャレイヤーの URL を入力…"
    assert "設定ファイルの表: Things/0（起動時レイヤー）" in texts and "設定ファイルの表: 備考の単語集（単語集）" in texts
    assert p.open_append() is d


def test_appends_into_a_qgis_layer_through_the_field_map_and_takes_it_back(plugin):
    p, _, _ = plugin
    source, target = ships(), things()
    d = dialog_of(p)
    assert d.add_input(source) and not d.add_input(source)
    d.set_target_layer(target)
    assert d.target_select.currentText() == "レイヤー: 追加先"
    # By the names: NAME ← name, KIND ← kind; SIZE_M and NOTE have no field of their name.
    assert d.cell_text("NAME", "入力") == "name" and d.cell_text("SIZE_M", "入力") == "（空）"
    assert [d.map_table.horizontalHeaderItem(i).text() for i in range(d.map_table.columnCount())] == ["追加先の列", "型", "まとめ方", "入力"]
    d.set_rule("SIZE_M", "入力", ap.InputRule(ap.FIELDS, ["size"]))
    d.set_rule("NOTE", "入力", ap.InputRule(ap.EXPRESSION, expression="\"name\" || ':' || coalesce(\"memo\", '-')"))
    d.set_rule("KIND", "入力", ap.InputRule(ap.VALUE, value="7"))
    assert d.cell_text("NOTE", "入力").startswith("式: ")
    plan = checked(d)
    assert [r.action for r in plan.rows] == [ap.ADD, ap.ADD, ap.SKIP, ap.ADD]
    assert plan.rows[2].reasons == ["マルチの形状（2 個）は 1 つのポイントに入らない"]
    assert d.run_button.isEnabled() and "追加 3 件・飛ばす 1 件" in d.summary.text()
    assert d.run(confirm=False)
    assert target.featureCount() == 3 and not target.isEditable()
    rows = sorted((f["NAME"], f["KIND"], f["SIZE_M"], f["NOTE"]) for f in target.getFeatures())
    assert rows == [("かたちなし", 7, 2.0, "かたちなし:"), ("くるま", 7, 4.2, "くるま:-"), ("ふね", 7, 120.5, "ふね:港")]
    # Into the target's CRS (Web Mercator), the multi point of one point made single.
    f = next(f for f in target.getFeatures() if f["NAME"] == "ふね")
    assert f.geometry().wkbType() == 1 and abs(f.geometry().asPoint().x() - 15473409) < 1
    assert "追加 3 件" in d.status.text() and d.undo_button.isEnabled()
    assert d.undo(confirm=False)
    assert target.featureCount() == 0 and d.status.text() == "今回の追加を取り消し" and not d.undo_button.isEnabled()


def test_matching_keys_update_and_are_given_back(plugin):
    p, _, _ = plugin
    source, target = ships(), things("EPSG:4326")
    target.dataProvider().addFeatures([_feature(target, "POINT(1 1)", ["ふね", 9, 5.0, "前"])])
    d = dialog_of(p)
    d.add_input(source)
    d.set_target_layer(target)
    d.update_box.setChecked(True)
    d.key_field.setCurrentIndex(d.key_field.findData("NAME"))
    d.update_geometry.setChecked(True)
    d.set_rule("NOTE", "入力", ap.InputRule(ap.FIELDS, ["memo"]))
    plan = checked(d)
    assert [(r.action, r.target_id) for r in plan.rows] == [(ap.UPDATE, 1), (ap.ADD, None), (ap.SKIP, None), (ap.ADD, None)]
    assert d.run(confirm=False)
    old = target.getFeature(1)
    assert (old["NOTE"], old["KIND"], old["SIZE_M"]) == ("港", 1, 5.0) and old.geometry().asWkt(1) == "Point (139 35)"
    assert target.featureCount() == 3
    d.undo(confirm=False)
    old = target.getFeature(1)
    assert target.featureCount() == 1 and (old["NOTE"], old["KIND"], old.geometry().asWkt(0)) == ("前", 9, "Point (1 1)")


def _feature(lyr, wkt, attrs):
    f = QgsFeature(lyr.fields())
    f.setAttributes(attrs)
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    return f


def test_a_layer_being_edited_gets_the_features_in_its_edit_buffer(plugin):
    p, _, _ = plugin
    source, target = ships(), things()
    target.startEditing()
    d = dialog_of(p)
    d.add_input(source)
    d.set_target_layer(target)
    assert "編集中のレイヤー" in d.target_note.text()
    checked(d)
    d.run(confirm=False)
    assert target.isEditable() and len(target.editBuffer().addedFeatures()) == 3 and target.dataProvider().featureCount() == 0
    assert "保存は手動" in d.status.text()
    d.undo(confirm=False)
    assert len(target.editBuffer().addedFeatures()) == 0
    target.rollBack()


def test_a_cell_is_changed_in_its_window_several_fields_merged(plugin):
    p, _, _ = plugin
    source, target = ships(), things()
    d = dialog_of(p)
    d.add_input(source)
    d.set_target_layer(target)
    row = [f.name for f in d.target.schema.fields].index("NOTE")
    rule = append_dialog.RuleDialog(d.target.schema.field("NOTE"), d.field_map.entry("NOTE"), "入力", source,
                                    append_io.fields_of(source), d)
    rule.kind_buttons[ap.FIELDS].setChecked(True)
    rule.set_checked(["name", "memo"])
    rule.merge.setCurrentIndex(rule.merge.findData("join"))
    rule.delimiter.setText("／")
    rule.end.setValue(3)
    assert rule.fields_box.isVisibleTo(rule) and not rule.expression_box.isVisibleTo(rule) and rule.delimiter.isEnabled()
    d.edit_cell(row, append_dialog.FIRST_INPUT, rule, exec_=False)
    assert d.cell_text("NOTE", "入力") == "name + memo [1〜3文字目]"
    assert d.map_table.item(row, append_dialog.MERGE_COLUMN).text() == "連結（／）"
    plan = checked(d)
    assert plan.rows[0].values["NOTE"] == "ふね／港" and plan.rows[1].values["NOTE"] == "くるま"
    # The server's / provider's own fields cannot be changed; the schema types other than the map take the names.
    d.set_schema_type(ap.TEST_AND_SKIP)
    assert not d.map_box.isEnabled() and d.edit_cell(row, append_dialog.FIRST_INPUT, rule, exec_=False) is None
    plan = checked(d)
    assert plan.rows == [] and "飛ばした入力 1 個" in d.summary.text() and "列が一致しない入力: 入力" in d.summary.text()
    d.set_schema_type(ap.TEST)
    plan = checked(d)
    assert plan.stopped and not d.run_button.isEnabled() and "中止" in d.summary.text()


def test_field_maps_are_kept_by_name_by_target_and_in_files(plugin):
    p, _, tmp_path = plugin
    source, target = ships(), things()
    d = dialog_of(p)
    d.add_input(source)
    d.set_target_layer(target)
    d.set_rule("NOTE", "入力", ap.InputRule(ap.FIELDS, ["memo"]))
    assert d.save_map("港の船")
    assert "港の船" in prefs.append_maps()
    path = tmp_path / "map.json"
    assert d.export_map(str(path))
    assert json.loads(path.read_text(encoding="utf-8"))["fields"][3] == {"target": "NOTE", "inputs": {"入力": {"from": "fields", "fields": ["memo"]}}}
    d.remap()
    assert d.cell_text("NOTE", "入力") == "（空）"
    assert d.load_map("港の船") and d.cell_text("NOTE", "入力") == "memo"
    d.clear_map()
    assert d.cell_text("NAME", "入力") == "（空）"
    assert d.import_map(str(path)) and d.cell_text("NOTE", "入力") == "memo" and d.cell_text("NAME", "入力") == "name"
    d.delete_map("港の船", confirm=False)
    assert prefs.append_maps() == {}
    # The map last run with comes back for that target.
    checked(d)
    d.run(confirm=False)
    d.set_target_layer(None)
    d.set_target_layer(target)
    assert d.cell_text("NOTE", "入力") == "memo"
    # A second input gets its own column, by its names.
    other = layer("Point", ["NAME:string", "note:string"], [("POINT(1 1)", ["x", "y"])], "入力")
    d.add_input(other)
    assert [d.map_table.horizontalHeaderItem(i).text() for i in range(3, 5)] == ["入力", "入力 (2)"]
    assert d.cell_text("NOTE", "入力 (2)") == "note" and d.cell_text("NOTE", "入力") == "memo"
    d.remove_input(0)
    assert d.map_table.columnCount() == 4 and d.cell_text("NAME", "入力") == "NAME"


def _esri(p, extra=None):
    fake = FakeEsri()
    lyr = fake.add_layer(URL, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeSmallInteger", "CREATED_USER": "esriFieldTypeString",
                                         "CREATED_DATE": "esriFieldTypeDate"}))
    lyr.extra = {"name": "モノ", "extent": {"spatialReference": {"wkid": 4326}}, **(extra or {})}
    p.context.esri = lambda: EsriClient(fake)
    return fake, lyr


def test_appends_into_an_esri_layer_with_who_and_when_and_takes_it_back(plugin):
    p, _, _ = plugin
    fake, lyr = _esri(p)
    source = ships()
    d = dialog_of(p)
    d.add_input(source)
    d.set_target_url(URL)
    assert wait_for(lambda: d.target is not None)
    assert d.target.is_esri and "ArcGIS へ送信" in d.target_note.text() and d.target.name == "モノ（Things/0）"
    row = [f.name for f in d.target.schema.fields].index("CREATED_USER")
    assert d.map_table.item(row, append_dialog.MERGE_COLUMN).text() == "自動（作成者・更新者（自動））"
    d.set_rule("KIND", "入力", ap.InputRule(ap.VALUE, value="3"))
    plan = checked(d)
    assert plan.count(ap.ADD) == 3
    assert d.run(confirm=False)
    assert wait_for(lambda: d.applied is not None)
    assert len(lyr.rows) == 3 and "追加 3 件" in d.status.text()
    a = lyr.rows[0]["attributes"]
    assert a["NAME"] == "ふね" and a["KIND"] == 3 and a["CREATED_USER"] and a["CREATED_DATE"]
    assert lyr.rows[0]["geometry"] == {"x": 139.0, "y": 35.0, "spatialReference": {"wkid": 4326}}
    assert lyr.rows[2]["geometry"] is None
    assert d.undo(confirm=False)
    assert wait_for(lambda: d.applied is None)
    assert lyr.rows == []


def test_the_plugins_own_tables_are_for_administrators_only(plugin):
    p, _, _ = plugin
    fake = FakeEsri()
    fake.add_layer(GLOSSARY, FakeLayer({"TERM": "esriFieldTypeString"}, geometry=""))
    p.context.esri = lambda: EsriClient(fake)
    source = ships()
    d = append_dialog.AppendDialog(p.context, lambda feature: feature != "append.own_tables")
    d.add_input(source)
    d.set_target_url(GLOSSARY)
    assert wait_for(lambda: d.target is not None)
    assert "［E206］" in d.target_note.text() and "単語集" in d.target_note.text()
    assert not d.check() and "E206" in d.status.text()
    admin = append_dialog.AppendDialog(p.context, lambda feature: True)
    admin.add_input(source)
    admin.set_target_url(GLOSSARY)
    assert wait_for(lambda: admin.target is not None)
    assert admin.target_select.currentText() == "設定ファイルの表: 備考の単語集（単語集）"
    assert "注意: ミカゲ自身の「単語集」の表" in admin.target_note.text()
    assert checked(admin).count(ap.ADD) == 4
    for x in (d, admin):
        x.close_all()
        x.deleteLater()


def run(params):
    algorithm = QgsApplication.processingRegistry().algorithmById("mikage:append")
    assert algorithm is not None
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run(params, context, feedback)
    return results, ok, feedback


def test_the_processing_tool_appends_with_a_field_map_and_keys(plugin):
    p, _, _ = plugin
    source, target = ships(), things("EPSG:4326")
    target.dataProvider().addFeatures([_feature(target, "POINT(1 1)", ["ふね", 1, 5.0, "前"])])
    field_map = json.dumps({"NOTE": {"from": "expression", "expression": "upper(\"memo\")"}, "SIZE_M": "size"})
    results, ok, _ = run({"INPUTS": [source.id()], "TARGET": target.id(), "FIELD_MAP": field_map, "MATCH_FIELDS": "NAME"})
    assert ok and (results["APPENDED"], results["UPDATED"], results["SKIPPED"], results["FAILED"]) == (2, 1, 1, 0)
    assert results["OUTPUT"] == target.id()
    assert target.getFeature(1)["NOTE"] == "港" and target.getFeature(1)["SIZE_M"] == 120.5
    # A kept map by its name; nothing matches TEST: stopped, nothing added.
    prefs.set_append_map("大きさだけ", {"fields": [{"target": "SIZE_M", "inputs": {"*": "size"}}]})
    results, ok, _ = run({"INPUTS": [source.id()], "TARGET": target.id(), "FIELD_MAP": "大きさだけ", "SELECTED": False})
    assert ok and results["APPENDED"] == 3 and target.featureCount() == 6
    results, ok, feedback = run({"INPUTS": [source.id()], "TARGET": target.id(), "SCHEMA_TYPE": 1})
    assert not ok and target.featureCount() == 6
    results, ok, _ = run({"INPUTS": [source.id()], "TARGET": target.id(), "FIELD_MAP": "ないもの"})
    assert not ok
    results, ok, _ = run({"INPUTS": [target.id()], "TARGET": target.id()})
    assert not ok


def test_the_processing_tool_appends_into_an_esri_layer_by_its_url(plugin):
    p, _, _ = plugin
    fake, lyr = _esri(p)
    source = ships()
    results, ok, _ = run({"INPUTS": [source.id()], "TARGET_URL": URL, "ALL_OR_NOTHING": True})
    assert ok and results["APPENDED"] == 3 and len(lyr.rows) == 3 and results["OUTPUT"] == URL
    # The plugin's own table, for a role without 管理: refused.
    p.provider.allowed = lambda feature: feature != "append.own_tables"
    fake.add_layer(GLOSSARY, FakeLayer({"TERM": "esriFieldTypeString"}, geometry=""))
    results, ok, _ = run({"INPUTS": [source.id()], "TARGET_URL": GLOSSARY})
    assert not ok
    p.provider.allowed = lambda feature: True
    results, ok, _ = run({"INPUTS": [source.id()], "TARGET_URL": GLOSSARY, "FIELD_MAP": json.dumps({"TERM": "name"})})
    assert ok and results["APPENDED"] == 4


def test_only_the_selected_features_a_condition_and_a_geometry_of_another_kind(plugin):
    p, _, _ = plugin
    source = ships()
    lines = layer("LineString", ["NAME:string"], [], "線")
    d = dialog_of(p)
    d.add_input(source)
    d.set_target_layer(lines)
    plan = checked(d)
    assert plan.count(ap.ADD) == 1 and plan.rows[0].reasons == ["形状がラインでない（マルチポイント）"]
    source.selectByIds([1, 2])
    d.set_selected_only(0, True)
    d.condition.setText("\"kind\" = 1")
    plan = checked(d)
    assert [r.number for r in plan.rows] == [1]
    d.condition.setText("\"nothere\" = 1")
    assert not d.check() and "絞り込みの式" in d.status.text()
