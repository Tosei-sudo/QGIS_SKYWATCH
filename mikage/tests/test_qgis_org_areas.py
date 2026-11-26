"""
組織の関心地域 in QGIS (tsudo, 2026-11-24): the named area layer's areas of
interest named on the 新着画像 cards after the person's own, and edited by
everyone in the 「組織」 tab of 「関心地域・地点」 (and its categories).
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

from mikage.qgis_io import qsettings  # noqa: E402
from mikage.core.obs.alerts import DEFAULT_PLACE_FIELDS  # noqa: E402
from mikage.core.named_areas import AreaCategory  # noqa: E402
from mikage.qgis_io.org_areas_io import layer_of  # noqa: E402
from mikage.tests.fake_esri import FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_interests import AREAS as MY_AREAS, FIELDS as MY_FIELDS, POINTS as MY_POINTS  # noqa: E402
from mikage.tests.test_qgis_notice_feed import config_of, loaded, plugin, write_config  # noqa: E402,F401
from mikage.ui import org_areas_panel  # noqa: E402
from mikage.ui.interests_dock import TAB_KEY  # noqa: E402

PLACES = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/3"
CATEGORIES = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/5"
S, I = "esriFieldTypeString", "esriFieldTypeInteger"
FIELDS = {**{v: S for v in DEFAULT_PLACE_FIELDS.values()}, "ACTIVE": "esriFieldTypeSmallInteger", "PRIORITY": I,
          "UPDATED_DATE": "esriFieldTypeDate", "UPDATED_USER": S}
CATEGORY_FIELDS = {"CATEGORY_ID": S, "CATEGORY_NAME": S, "CATEGORY_NAME_EN": S, "PARENT_ID": S, "SORT_ORDER": I, "COLOR": S,
                   "ACTIVE": "esriFieldTypeSmallInteger", "NOTE": S}


def box(w, s, e, n):
    return {"rings": [[[w, s], [w, n], [e, n], [e, s], [w, s]]]}


@pytest.fixture
def org(plugin, monkeypatch):  # noqa: F811
    p, iface, fake, tmp_path = plugin
    monkeypatch.setenv("USERNAME", "tsudo")
    places = fake.add_layer(PLACES, FakeLayer(dict(FIELDS), "esriGeometryPolygon"))
    categories = fake.add_layer(CATEGORIES, FakeLayer(dict(CATEGORY_FIELDS), None))
    mine = fake.add_layer(MY_AREAS, FakeLayer(dict(MY_FIELDS), "esriGeometryPolygon"))
    fake.add_layer(MY_POINTS, FakeLayer(dict(MY_FIELDS), "esriGeometryPoint"))
    # Card A covers 139.6–139.8 / 35.5–35.7, B 140.0–140.2, C 139.5–139.55 / 35.0–35.05.
    places.add({"NAME": "東京湾", "KIND": "海域", "NAMED_AREA_ID": "NA-000001"}, box(139.0, 34.5, 140.5, 36.0))
    places.add({"NAME": "横須賀港", "NAMED_AREA_ID": "NA-000101", "AREA_USE": "両方", "CATEGORY_ID": "PORT", "PARENT_ID": "NA-000001",
                "NAME_KANA": "よこすかこう", "OWNER_GROUP": "第1班", "PRIORITY": 300}, box(139.65, 35.55, 139.70, 35.60))
    places.add({"NAME": "長浦", "AREA_USE": "関心地域", "CATEGORY_ID": "PORT-NAVAL", "COLOR": "#dc2626"}, box(140.05, 35.5, 140.1, 35.6))
    categories.add({"CATEGORY_ID": "PORT", "CATEGORY_NAME": "港湾", "SORT_ORDER": 1, "COLOR": "#0ea5e9", "ACTIVE": 1})
    categories.add({"CATEGORY_ID": "PORT-NAVAL", "CATEGORY_NAME": "軍港", "PARENT_ID": "PORT", "SORT_ORDER": 1, "ACTIVE": 1})
    mine.add({"INTEREST_ID": "a1", "INTEREST_NAME": "房総の沖", "OWNER_USER": "tsudo", "COLOR": "#ec4899"},
             box(140.15, 35.65, 140.4, 35.9))
    config = config_of(tmp_path)
    config["namedAreas"] = {"url": PLACES, "categories": CATEGORIES, "label": "組織の関心"}
    config["interests"] = {"points": MY_POINTS, "areas": MY_AREAS, "label": "私の関心"}
    write_config(tmp_path, config)
    p.api.reload_config()
    assert p.api.problems == [], p.api.problems
    QgsSettings().remove(qsettings.key(org_areas_panel.ON_MAP_KEY))
    QgsSettings().remove(qsettings.key(TAB_KEY))
    yield p, iface, places, categories, fake
    QgsSettings().remove(qsettings.key(org_areas_panel.ON_MAP_KEY))
    QgsSettings().remove(qsettings.key(TAB_KEY))


def panel_of(p):
    dock = p.api.interests
    dock.show()
    dock.tabs.setCurrentWidget(dock.org)
    panel = dock.org
    assert wait_for(lambda: panel.book.list is not None and not panel.book.loading)
    return dock, panel


def test_the_cards_name_the_organisations_areas_after_mine(org):
    p, _iface, _places, _c, _fake = org
    feed = p.notice_feed
    p.toggle_notice_feed()
    assert loaded(feed)
    assert wait_for(lambda: "組織の関心（1 件）" in feed.cards["w|A"].contents.text())
    a = feed.cards["w|A"].contents.text()
    # 横須賀港 (両方) in its category's colour; 東京湾 (地名 only) never; after the person's own.
    assert "横須賀港（港湾）" in a and "#0ea5e9" in a and "東京湾" not in a and a.index("私の関心") < a.index("組織の関心")
    b = feed.cards["w|B"].contents.text()
    assert "組織の関心（1 件）" in b and "長浦（軍港）" in b and "#dc2626" in b
    assert "組織の関心（0 件）" in feed.cards["w|C"].contents.text()


def test_the_list_by_category_parent_and_use(org):
    p, _iface, _places, _c, _fake = org
    dock, panel = panel_of(p)
    tree = panel.tree
    # 関心地域 by category: 港湾 ▸ 横須賀港, 軍港 ▸ 長浦 (東京湾 is a place name only).
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] == ["港湾（2）"]
    port = tree.topLevelItem(0)
    assert [port.child(i).text(0) for i in range(port.childCount())] == ["軍港（1）", "横須賀港"]
    assert port.child(0).child(0).text(0) == "長浦" and port.child(0).child(0).text(1) == "（未付番）"
    panel.show_select.setCurrentIndex(panel.show_select.findData(org_areas_panel.SHOW_ALL))
    panel.way_select.setCurrentIndex(panel.way_select.findData(org_areas_panel.BY_PARENT))
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] == ["東京湾", "長浦"]
    assert tree.topLevelItem(0).child(0).text(0) == "横須賀港"
    panel.search.setText("よこすか")
    assert tree.topLevelItemCount() == 1 and tree.topLevelItem(0).text(0) == "横須賀港"
    assert "表示 1 件" in panel.status.text()
    # The tab is remembered.
    assert QgsSettings().value(qsettings.key(TAB_KEY), 0, type=int) == 1


def test_everyone_adds_and_edits_areas_and_numbers_them(org):
    p, iface, places, _c, _fake = org
    dock, panel = panel_of(p)
    assert panel.buttons["number"].isVisibleTo(panel) and "1 件" in panel.buttons["number"].text()
    # 新しい地域: drawn, a name, then saved: numbered after the largest (NA-000101 → NA-000102).
    panel.buttons["new"].setChecked(True)
    assert iface.canvas.mapTool() is panel.draw_tool
    panel._drawn([[139.70, 35.60], [139.75, 35.60], [139.75, 35.65]])
    assert panel.form.isVisibleTo(panel) and panel.form.title() == "新しい組織の関心地域" and panel.use.currentData() == "関心地域"
    panel.save()
    assert panel.problem.text() == "名前が必要"
    panel.name.setText("観音崎")
    panel.kana.setText("かんのんざき")
    panel.alias.setText("Kannonzaki, 観音崎灯台")
    panel.category.setCurrentIndex(panel.category.findData("PORT"))
    panel.owner_group.setEditText("第2班")
    panel.priority.setValue(200)
    panel._form_edited()
    panel.save()
    assert wait_for(lambda: not panel._saving and len(places.rows) == 4)
    row = places.rows[-1]["attributes"]
    assert row["NAMED_AREA_ID"] == "NA-000102" and row["NAME_ALIAS"] == "Kannonzaki, 観音崎灯台" and row["OWNER_GROUP"] == "第2班"
    assert row["PRIORITY"] == 200 and row["AREA_USE"] == "関心地域" and row["CATEGORY_ID"] == "PORT"
    assert "管理番号 NA-000102" in panel.status.text() and panel.form.title() == "編集: 観音崎"
    # A number some other row has is refused before saving.
    panel.area_id.setText("NA-000101")
    panel._form_edited()
    panel.save()
    assert "横須賀港" in panel.problem.text()
    panel.cancel()
    # Edited: 用途 and colour; the shape not sent.
    panel.choose("NA-000101")
    assert panel.name.text() == "横須賀港" and panel.parent_area.currentText().startswith("NA-000001")
    panel.color.setColor(QColor("#16a34a"))
    panel.active.setChecked(False)
    panel._form_edited()
    panel.save()
    assert wait_for(lambda: not panel._saving and places.rows[1]["attributes"]["COLOR"] == "#16a34a")
    assert places.rows[1]["attributes"]["ACTIVE"] == 0 and places.rows[1]["geometry"] == box(139.65, 35.55, 139.70, 35.60)
    # 付番: the row without one (長浦).
    panel.number_blanks(ask=False)
    assert wait_for(lambda: not panel._saving and places.rows[2]["attributes"]["NAMED_AREA_ID"] == "NA-000103")
    assert not panel.buttons["number"].isVisibleTo(panel)


def test_the_form_fits_a_narrow_dock(org):
    from qgis.PyQt.QtCore import Qt
    from mikage.tests.layout_probe import problems, summary

    p, iface, _places, _c, _fake = org
    dock, panel = panel_of(p)
    panel.choose("NA-000101")
    window = iface.window
    window.resize(1400, 900)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    for width in (440, 300):
        window.resizeDocks([dock], [width], Qt.Orientation.Horizontal)
        for _ in range(10):
            APP.processEvents()
        scroll = dock.widget()
        # Nothing wider than the dock: no scrolling sideways, nothing drawn over anything else.
        assert scroll.widget().minimumSizeHint().width() <= scroll.viewport().width(), width
        assert problems(dock) == [], summary(problems(dock))


def test_the_shape_from_a_feature_and_the_vertex_tool(org):
    p, iface, places, _c, _fake = org
    dock, panel = panel_of(p)
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "港湾区域", "memory")
    f = QgsFeature(layer.fields())
    f.setAttributes(["横浜港"])
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((139.6 35.4, 139.7 35.4, 139.7 35.5, 139.6 35.4))"))
    layer.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(layer)
    iface.setActiveLayer(layer)
    layer.selectAll()
    panel.from_selection()
    assert panel.name.text() == "横浜港" and panel.shape.text() == "頂点 3" and panel.draft.oid is None
    # 頂点を直す: a copy in edit mode, its shape taken back when the button is let go.
    panel.vertex_button.setChecked(True)
    editing = panel._edit_layer
    assert editing is not None and editing.isEditable()
    editing_id = editing.id()
    fid = next(editing.getFeatures()).id()
    editing.changeGeometry(fid, QgsGeometry.fromWkt("MULTIPOLYGON(((139.6 35.4, 139.8 35.4, 139.8 35.6, 139.6 35.6, 139.6 35.4)))"))
    panel.vertex_button.setChecked(False)
    assert panel._edit_layer is None and panel.shape.text() == "頂点 4" and editing_id not in QgsProject.instance().mapLayers()
    panel.save()
    assert wait_for(lambda: not panel._saving and len(places.rows) == 4)
    assert len(places.rows[-1]["geometry"]["rings"][0]) == 5


def test_a_personal_area_becomes_the_organisations(org):
    p, _iface, places, _c, _fake = org
    dock = p.api.interests
    dock.show()
    dock.tabs.setCurrentIndex(0)
    assert wait_for(lambda: dock.table.rowCount() == 1)
    dock.choose("a1")
    assert dock.buttons["org"].isEnabled()
    dock.to_org()
    panel = dock.org
    assert dock.tabs.currentWidget() is panel and panel.name.text() == "房総の沖" and panel.draft.rings
    assert panel.color.color().name() == "#ec4899"
    # The tab shown: the rows read (the form kept as it is).
    assert wait_for(lambda: panel.book.list is not None and panel.save_button.isVisibleTo(panel))
    assert panel.name.text() == "房総の沖"
    panel.save()
    assert wait_for(lambda: not panel._saving and len(places.rows) == 4)
    assert places.rows[-1]["attributes"]["NAME"] == "房総の沖" and places.rows[-1]["attributes"]["AREA_USE"] == "関心地域"


def test_on_the_map(org):
    p, _iface, _places, _c, _fake = org
    dock, panel = panel_of(p)
    assert layer_of() is None and not panel.buttons["map"].isChecked()  # off until turned on
    panel.buttons["map"].setChecked(True)
    layer = layer_of()
    assert layer is not None and sorted(f["name"] for f in layer.getFeatures()) == ["横須賀港", "長浦"]
    assert {f["name"]: f["color"] for f in layer.getFeatures()} == {"横須賀港": "#0ea5e9", "長浦": "#dc2626"}
    panel.choose("NA-000101")
    assert layer.selectedFeatureCount() == 1
    panel.buttons["map"].setChecked(False)
    assert layer_of() is None


def test_the_categories_dialog(org):
    p, _iface, places, categories, _fake = org
    dock, panel = panel_of(p)
    panel.open_categories()
    d = panel.dialog
    assert d.tree.topLevelItemCount() == 1 and d.tree.topLevelItem(0).child(0).text(0) == "軍港"
    assert d.tree.topLevelItem(0).text(3) == "1" and "カテゴリー 2 件" in d.status.text()
    # 新規 under the chosen one, then its ID changed: the areas follow.
    d.tree.topLevelItem(0).setSelected(True)
    form = d.add()
    assert form.parent_select.currentData() == "PORT"
    form.cid.setText("AIR")
    form.name.setText("空港")
    form.parent_select.setCurrentIndex(0)
    form._accept()
    assert wait_for(lambda: not d.busy and len(categories.rows) == 3)
    assert categories.rows[-1]["attributes"]["SORT_ORDER"] == 2
    d.tree.clearSelection()
    d.tree.topLevelItem(0).child(0).setSelected(True)
    form = d.edit()
    form.cid.setText("NAVAL")
    assert "1 件の CATEGORY_ID" in form.renamed.text()
    form._accept()
    assert wait_for(lambda: not d.busy and places.rows[2]["attributes"]["CATEGORY_ID"] == "NAVAL")
    # In use: not deleted.
    d.tree.clearSelection()
    d.tree.topLevelItem(0).setSelected(True)
    d.delete(ask=False)
    assert "地域があるカテゴリーは削除できない" in d.status.text() and len(categories.rows) == 3
    d.tree.clearSelection()
    item = next(d.tree.topLevelItem(i) for i in range(d.tree.topLevelItemCount()) if d.tree.topLevelItem(i).text(1) == "AIR")
    item.setSelected(True)
    d.delete(ask=False)
    assert wait_for(lambda: not d.busy and len(categories.rows) == 2)
    assert AreaCategory  # (imported for the reader)
