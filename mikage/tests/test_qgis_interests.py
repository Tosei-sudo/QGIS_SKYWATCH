"""
個人の関心地域・地点 in QGIS (tsudo, 2026-11-14): each person's own points and
areas (Esri, the settings file's `interests`) named first in the 新着画像
cards' 「写っている地物」, and edited in their dock 「関心地域・地点」.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402

from mikage.qgis_io import qsettings  # noqa: E402
from mikage.qgis_io.interests_io import layers_of  # noqa: E402
from mikage.tests.fake_esri import FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_notice_feed import config_of, loaded, plugin, write_config  # noqa: E402,F401
from mikage.ui.interests_dock import ON_MAP_KEY  # noqa: E402

POINTS = "https://example.com/arcgis/rest/services/MIKAGE/FeatureServer/25"
AREAS = "https://example.com/arcgis/rest/services/MIKAGE/FeatureServer/26"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
FIELDS = {"INTEREST_ID": S, "INTEREST_NAME": S, "OWNER_USER": S, "COLOR": S, "NOTE": S, "UPDATED_DATE": T, "CREATED_USER": S}


@pytest.fixture
def interests(plugin, monkeypatch):  # noqa: F811
    p, iface, fake, tmp_path = plugin
    monkeypatch.setenv("USERNAME", "tsudo")
    points = fake.add_layer(POINTS, FakeLayer(dict(FIELDS), "esriGeometryPoint"))
    areas = fake.add_layer(AREAS, FakeLayer(dict(FIELDS), "esriGeometryPolygon"))
    # Card A covers 139.6–139.8 / 35.5–35.7, B 140.0–140.2, C 139.5–139.55 / 35.0–35.05.
    points.add({"INTEREST_ID": "p1", "INTEREST_NAME": "横須賀の岸壁", "OWNER_USER": "TSUDO", "COLOR": "#1e3a8a"}, {"x": 139.66, "y": 35.6})
    points.add({"INTEREST_ID": "p2", "INTEREST_NAME": "佐藤さんの港", "OWNER_USER": "sato", "COLOR": "#dc2626"}, {"x": 139.7, "y": 35.6})
    areas.add({"INTEREST_ID": "a1", "INTEREST_NAME": "房総の沖", "OWNER_USER": "tsudo", "COLOR": "pink"},
              {"rings": [[[140.15, 35.65], [140.15, 35.9], [140.4, 35.9], [140.4, 35.65], [140.15, 35.65]]]})
    config = config_of(tmp_path)
    config["interests"] = {"points": POINTS, "areas": AREAS, "label": "私の関心"}
    write_config(tmp_path, config)
    p.api.reload_config()
    assert p.api.problems == [], p.api.problems
    QgsSettings().remove(qsettings.key(ON_MAP_KEY))
    yield p, iface, points, areas, fake
    QgsSettings().remove(qsettings.key(ON_MAP_KEY))


def test_the_cards_name_my_points_and_areas_first(interests):
    p, _iface, _points, _areas, fake = interests
    feed = p.notice_feed
    p.toggle_notice_feed()
    assert loaded(feed)
    a = feed.cards["w|A"].contents.text()
    # Mine only (another person's in the same layer never), in its colour, before the catalog's layers.
    assert "私の関心（1 件）" in a and "<td bgcolor='#1e3a8a' style='color:#ffffff'>横須賀の岸壁</td>" in a
    assert "佐藤さん" not in a and a.index("私の関心") < a.index("施設")
    b = feed.cards["w|B"].contents.text()
    assert "私の関心（1 件）" in b and "<td bgcolor='#ffc0cb' style='color:#111111'>房総の沖</td>" in b
    assert "私の関心（0 件）" in feed.cards["w|C"].contents.text()
    # Added in the dock: every card at once (nothing asked of Esri per card).
    dock = p.api.interests
    dock.show()
    assert wait_for(lambda: dock.table.rowCount() == 2)
    asked = len(fake.asked)
    dock._clicked(QgsPointXY(139.52, 35.02))
    dock.name.setText("小さな島")
    dock._form_edited()
    dock.save()
    assert wait_for(lambda: "小さな島" in feed.cards["w|C"].contents.text())
    assert "私の関心（1 件）" in feed.cards["w|C"].contents.text()
    # Only the save went to Esri (layer, find, applyEdits): no query per card.
    assert all("/query" not in u or "returnIdsOnly" not in f and "geometry" not in f for u, f in fake.asked[asked:])
    assert not [u for u, _ in fake.asked[asked:] if "Places" in u or "Images" in u]


def test_the_dock_adds_edits_and_deletes_my_items(interests):
    p, iface, points, areas, _fake = interests
    dock = p.api.interests
    dock.show()
    assert wait_for(lambda: dock.table.rowCount() == 2)
    assert [dock.table.item(r, 0).text() for r in range(2)] == ["房総の沖", "横須賀の岸壁"]
    assert "tsudo さん" in dock.note.text() and dock.buttons["point"].isEnabled() and dock.buttons["area"].isEnabled()
    # 地点を追加: a click, then a name, then 保存.
    dock.buttons["point"].setChecked(True)
    assert iface.canvas.mapTool() is dock.point_tool
    dock._clicked(QgsPointXY(139.9, 35.4))
    assert iface.canvas.mapTool() is not dock.point_tool and not dock.buttons["point"].isChecked()
    assert dock.form.isVisible() and dock.form.title() == "新しい地点" and dock.save_button.isEnabled()
    dock.save()
    assert "名前を入れて" in dock.status.text() and len(points.rows) == 2
    dock.name.setText("観音崎")
    dock.memo.setPlainText("夜も見る")
    dock._form_edited()
    dock.save()
    assert wait_for(lambda: dock.table.rowCount() == 3 and not dock._saving)
    row = next(r for r in points.rows if r["attributes"]["INTEREST_NAME"] == "観音崎")
    assert row["attributes"]["OWNER_USER"] == "tsudo" and row["attributes"]["NOTE"] == "夜も見る" and row["geometry"]["x"] == pytest.approx(139.9)
    assert [i.name for i in dock.chosen()] == ["観音崎"] and dock.form.title() == "編集: 観音崎"
    # 地域を追加: a polygon drawn.
    dock.buttons["area"].setChecked(True)
    assert iface.canvas.mapTool() is dock.draw_tool
    dock._drawn([[139.0, 35.0], [139.2, 35.0], [139.2, 35.2]])
    assert dock.form.title() == "新しい地域" and dock.shape.text() == "地域（頂点 3）"
    dock.name.setText("相模湾")
    dock._form_edited()
    dock.save()
    assert wait_for(lambda: dock.table.rowCount() == 4 and not dock._saving)
    assert len(areas.rows) == 2
    # Edited: the colour and the shape (形を描き直す), the same row.
    dock.choose(next(i.id for i in dock.shown if i.name == "相模湾"))
    dock.color.setColor(dock.color.color().fromRgb(0, 128, 0))
    dock.reshape_button.setChecked(True)
    assert iface.canvas.mapTool() is dock.draw_tool
    dock._drawn([[139.0, 35.0], [139.3, 35.0], [139.3, 35.3], [139.0, 35.3]])
    assert dock.shape.text() == "地域（頂点 4）" and dock.save_button.isEnabled()
    dock.save()
    assert wait_for(lambda: not dock._saving and dock.book.find(dock.chosen()[0].id).color == "#008000")
    assert len(areas.rows) == 2 and areas.rows[1]["attributes"]["COLOR"] == "#008000"
    # 取り消し on a chosen one: back to what is saved.
    dock.name.setText("別の名前")
    dock._form_edited()
    dock.cancel()
    assert dock.name.text() == "相模湾"
    # 削除: the chosen ones.
    dock.choose(next(i.id for i in dock.shown if i.name == "観音崎"))
    dock.delete(confirm=False)
    assert wait_for(lambda: dock.table.rowCount() == 3 and not dock._saving)
    assert [r["attributes"]["INTEREST_NAME"] for r in points.rows] == ["横須賀の岸壁", "佐藤さんの港"]


def test_new_from_the_selected_feature(interests):
    p, iface, _points, _areas, _fake = interests
    dock = p.api.interests
    dock.show()
    assert wait_for(lambda: dock.table.rowCount() == 2)
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "港湾区域", "memory")
    f = QgsFeature(layer.fields())
    f.setAttributes(["横浜港"])
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((139.6 35.4, 139.7 35.4, 139.7 35.5, 139.6 35.4))"))
    layer.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(layer)
    iface.setActiveLayer(layer)
    dock.from_selection()
    assert "選んでから" in dock.status.text() and dock.draft is None
    layer.selectAll()
    dock.from_selection()
    assert dock.draft.kind == "area" and dock.name.text() == "横浜港" and dock.shape.text() == "地域（頂点 3）"


def test_on_the_map_as_two_layers(interests):
    p, iface, _points, _areas, _fake = interests
    dock = p.api.interests
    project = QgsProject.instance()
    dock.show()
    assert wait_for(lambda: dock.table.rowCount() == 2)
    # On by default once the person has items: 「関心地点」「関心地域」, each item in its colour, labelled.
    assert wait_for(lambda: set(layers_of()) == {"point", "area"})
    shown = layers_of()
    assert [f["name"] for f in shown["point"].getFeatures()] == ["横須賀の岸壁"]
    assert shown["area"].featureCount() == 1 and shown["point"].customProperty("skipMemoryLayersCheck") == 1
    assert shown["point"].readOnly() and shown["point"].labelsEnabled() and dock.buttons["map"].isChecked()
    # Chosen in the dock: selected on the map.
    dock.choose("p1")
    assert shown["point"].selectedFeatureCount() == 1 and shown["area"].selectedFeatureCount() == 0
    # A new project (every layer goes): back again.
    other = QgsVectorLayer("Point?crs=EPSG:4326", "ほか", "memory")
    project.addMapLayer(other)
    project.removeAllMapLayers()
    iface.newProjectCreated.emit()
    assert wait_for(lambda: set(layers_of()) == {"point", "area"})
    # Taken away by hand (other layers stay): not shown again until 「地図に表示」.
    project.addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", "ほか", "memory"))
    project.removeMapLayer(layers_of()["point"].id())
    assert not QgsSettings().value(qsettings.key(ON_MAP_KEY), True, type=bool)
    iface.newProjectCreated.emit()
    wait_for(lambda: False, ms=300)
    assert set(layers_of()) == {"area"}
    dock.buttons["map"].setChecked(False)
    assert layers_of() == {}
    dock.buttons["map"].setChecked(True)
    assert set(layers_of()) == {"point", "area"} and QgsSettings().value(qsettings.key(ON_MAP_KEY), True, type=bool)


def test_without_the_settings_or_a_user_name(plugin, monkeypatch):  # noqa: F811
    p, *_ = plugin
    dock = p.api.interests
    dock.show()
    assert "interests" in dock.note.text() and not dock.buttons["point"].isEnabled()
    # No interests: the cards as before (no section).
    feed = p.notice_feed
    p.toggle_notice_feed()
    assert loaded(feed)
    assert "関心" not in feed.cards["w|A"].contents.text()
