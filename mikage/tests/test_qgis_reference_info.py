"""
出典つきの参考情報 in QGIS (tsudo, 2026-11-24: 「4,5,7を実装して」の 7): the
dock (the period, the words, 「地図の範囲だけ」, the points on the map), its
form (the place by a click, a named area or the map's centre), who may delete,
「参考情報 n 件」 under 観測記録's image, the records' and things' links
(「参考情報 ▾」) and イベント's tab 「参考情報」.
"""
import json
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QMenu  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.events import new_event  # noqa: E402
from mikage.core.reference_info import DELETE_ANY, EVENT, OBSERVATION, THING  # noqa: E402
from mikage.features.events import Events  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402

INFOS = "https://example.com/arcgis/rest/services/Events/FeatureServer/2"
LINKS = "https://example.com/arcgis/rest/services/Events/FeatureServer/3"
EVENTS = "https://example.com/arcgis/rest/services/Events/FeatureServer/0"
EVENT_LINKS = "https://example.com/arcgis/rest/services/Events/FeatureServer/1"
AREAS = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/1"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
DAY = 86_400_000
#: The image's time (geotiff's ACQUISITION_START_TIME).
SHOT = "2026-10-01T01:00:00Z"
SHOT_MS = int(datetime(2026, 10, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
INFO_TYPES = {"INFO_ID": S, "TITLE": S, "INFO_TIME": T, "PUBLISHED_AT": T, "SOURCE": S, "SOURCE_REF": S, "RELIABILITY": S, "SUMMARY": S,
              "KIND": S, "NAMED_AREA_ID": S, "NOTE": S, "CREATED_USER": S, "CREATED_DATE": T, "UPDATED_USER": S, "UPDATED_DATE": T}


def box(w, s, e, n):
    return {"rings": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    monkeypatch.setenv("USERNAME", "田中")
    config = {"referenceInfo": {"layer": INFOS, "links": LINKS}, "events": {"layer": EVENTS, "links": EVENT_LINKS},
              "namedAreas": AREAS}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    infos = fake.add_layer(INFOS, FakeLayer(dict(INFO_TYPES), "esriGeometryPoint"))
    links = fake.add_layer(LINKS, FakeLayer({"INFO_ID": S, "ITEM_TYPE": S, "ITEM_ID": S, "LABEL": S, "CREATED_USER": S, "CREATED_DATE": T}, ""))
    fake.add_layer(EVENTS, FakeLayer({"EVENT_ID": S, "EVENT_NAME": S, "EVENT_KIND": S, "STATUS": S, "START_AT": T, "END_AT": T, "NOTE": S},
                                     "esriGeometryPolygon"))
    fake.add_layer(EVENT_LINKS, FakeLayer({"EVENT_ID": S, "ITEM_TYPE": S, "ITEM_ID": S, "ITEM_LABEL": S, "ITEM_TIME": T, "SOURCE": S,
                                           "NOTE": S}, ""))
    areas = fake.add_layer(AREAS, FakeLayer({"NAME": S, "KIND": S, "NAMED_AREA_ID": S, "ACTIVE": "esriFieldTypeSmallInteger",
                                             "AREA_USE": S}, "esriGeometryPolygon"))
    # 東京湾 crosses the image (139.6–139.8, 35.5–35.7); 相模湾 is outside it.
    areas.add({"NAME": "東京湾", "KIND": "海域", "NAMED_AREA_ID": "NA-000001", "ACTIVE": 1, "AREA_USE": "地名"}, box(139.65, 35.3, 140.1, 35.65))
    areas.add({"NAME": "相模湾", "KIND": "海域", "NAMED_AREA_ID": "NA-000002", "ACTIVE": 1, "AREA_USE": "地名"}, box(139.1, 34.9, 139.5, 35.3))
    now = int(time.time() * 1000)
    # In the image, 2 days before it; about 東京湾 (placed outside the image) 3 days after it; in the image but a month before;
    # recent, somewhere else, by another person.
    infos.add({"INFO_ID": "REF-1", "TITLE": "湾内で貨物船が座礁", "INFO_TIME": SHOT_MS - 2 * DAY, "SOURCE": "海上保安部", "SOURCE_REF": "航行警報 第12号",
               "RELIABILITY": "確定", "CREATED_USER": "佐藤"}, {"x": 139.7, "y": 35.6})
    infos.add({"INFO_ID": "REF-2", "TITLE": "東京湾で演習の予告", "INFO_TIME": SHOT_MS + 3 * DAY, "SOURCE": "〇〇新聞",
               "SOURCE_REF": "https://news.example/2", "RELIABILITY": "推定", "NAMED_AREA_ID": "NA-000001", "CREATED_USER": "佐藤"},
              {"x": 139.9, "y": 35.4})
    infos.add({"INFO_ID": "REF-3", "TITLE": "先月の工事", "INFO_TIME": SHOT_MS - 30 * DAY, "CREATED_USER": "佐藤"}, {"x": 139.7, "y": 35.6})
    infos.add({"INFO_ID": "REF-4", "TITLE": "相模湾の新しい岸壁", "INFO_TIME": now - 2 * DAY, "SOURCE": "〇〇新聞", "CREATED_USER": "佐藤"},
              {"x": 139.3, "y": 35.1})
    iface = FakeIface()
    p = small_plugin(iface, Events, Images, Observations)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, infos, links, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def idle(dock):
    return wait_for(lambda: dock.task is None and not dock._tasks)


def test_the_dock_lists_the_period_and_draws_points(plugin):
    p, iface, infos, links, tmp_path = plugin
    dock = p.docks["SatObsReferences"]
    dock.show()
    # 30 days by default: the recent one only.
    assert wait_for(lambda: dock.infos is not None and idle(dock))
    assert [x.title for x in dock.shown] == ["相模湾の新しい岸壁"]
    dock.period.setCurrentIndex(dock.period.findData(None))
    assert wait_for(lambda: len(dock.infos or []) == 4 and idle(dock))
    assert [dock.table.item(i, 1).text() for i in range(4)] == ["相模湾の新しい岸壁", "東京湾で演習の予告", "湾内で貨物船が座礁", "先月の工事"]
    assert dock.table.item(2, 2).text() == "海上保安部（航行警報 第12号）" and dock.table.item(2, 3).text() == "確定"
    assert dock.points.numberOfVertices() == 4
    dock.search.setText("新聞")
    assert [x.id for x in dock.shown] == ["REF-4", "REF-2"]
    dock.search.setText("")
    # 「地図の範囲だけ」: the map over the image.
    iface.canvas.setExtent(iface.canvas.extent().__class__(139.6, 35.5, 139.8, 35.7))
    dock.in_view.setChecked(True)
    assert [x.id for x in dock.shown] == ["REF-1", "REF-3"]
    dock.in_view.setChecked(False)
    dock.choose("REF-2")
    assert dock.buttons["url"].isEnabled() and "出典: 〇〇新聞" in dock.detail.text()
    assert dock.chosen_point.numberOfVertices() == 1
    dock.hide()
    assert dock.points.numberOfVertices() == 0


def test_the_form_saves_a_place_from_the_map_or_a_named_area(plugin):
    p, iface, infos, links, tmp_path = plugin
    dock = p.docks["SatObsReferences"]
    dock.show()
    assert wait_for(lambda: dock.infos is not None and idle(dock))
    form = dock.create()
    assert form.reliability.findText("推定") > 0 and "海上保安部" not in dock.sources()  # (older than the period)
    assert form.place is not None  # the map's centre first
    form._accept()
    assert form.problem.text() == "題名を入力"
    form.title_edit.setText("港内でクレーン設置")
    form.source_edit.setText("〇〇新聞")
    form.reliability.setCurrentText("推定")
    form.summary_edit.setPlainText("大型クレーン 2 基")
    book = dock.area_book()
    book.load()
    assert wait_for(lambda: book.areas is not None)
    form.choose_area(book.find_id("NA-000001"))
    assert wait_for(lambda: form.area_id == "NA-000001")
    assert "地名: 東京湾" in form.place_text.text()
    lon, lat = form.place
    assert 139.65 <= lon <= 140.1 and 35.3 <= lat <= 35.65
    form._accept()
    assert wait_for(lambda: any(r["attributes"]["TITLE"] == "港内でクレーン設置" for r in infos.rows) and idle(dock))
    row = next(r for r in infos.rows if r["attributes"]["TITLE"] == "港内でクレーン設置")
    assert row["attributes"]["NAMED_AREA_ID"] == "NA-000001" and row["attributes"]["CREATED_USER"] == "田中"
    assert row["attributes"]["INFO_ID"].startswith("REF-") and row["geometry"]["x"] == pytest.approx(lon)
    assert dock.chosen().title == "港内でクレーン設置" and dock.table.item(dock.shown.index(dock.chosen()), 4).text() == "東京湾"
    # A click on the map moves it.
    edit = dock.edit()
    edit.click_button.setChecked(True)
    assert iface.canvas.mapTool() is edit.tool
    edit._clicked(iface.canvas.extent().center().__class__(139.71, 35.61))
    assert not edit.click_button.isChecked() and edit.place == pytest.approx((139.71, 35.61))
    edit._accept()
    assert wait_for(lambda: row["geometry"]["x"] == pytest.approx(139.71) and idle(dock))


def test_only_the_creator_or_an_administrator_deletes(plugin):
    p, iface, infos, links, tmp_path = plugin
    dock = p.docks["SatObsReferences"]
    dock.show()
    assert wait_for(lambda: dock.infos is not None and idle(dock))
    dock.set_allows(lambda feature: feature != DELETE_ANY)  # not an administrator
    dock.choose("REF-4")
    dock.delete(confirm=False)
    assert "作成者（佐藤）か管理者だけ" in dock.status.text() and len(infos.rows) == 4
    dock.set_allows(lambda feature: True)  # 管理者
    dock.delete(confirm=False)
    assert wait_for(lambda: len(infos.rows) == 3 and idle(dock))


def _image(p, iface, tmp_path):
    layer = QgsRasterLayer(geotiff(tmp_path / "d1.tif", SHOT), "d1")
    QgsProject.instance().addMapLayers([layer])
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    obs = p.api.observation_dock
    obs.show()
    obs.render()
    return obs


def test_the_image_line_counts_its_infos_and_lists_them(plugin):
    p, iface, infos, links, tmp_path = plugin
    obs = _image(p, iface, tmp_path)
    line = obs.reference_line
    # In the image within 7 days, and about 東京湾 which it shows (placed outside it); not the month before.
    assert wait_for(lambda: line.text() == "参考情報 2 件")
    assert line.isVisibleTo(obs) and "湾内で貨物船が座礁" in line.toolTip() and "先月の工事" not in line.toolTip()
    dock = line.open()
    assert dock is p.docks["SatObsReferences"] and dock.isVisible()
    assert {x.id for x in dock.shown} == {"REF-1", "REF-2"} and dock.focus_row.isVisibleTo(dock)
    assert dock.focus_label.text() == "画像「d1.tif」の前後 7 日: 2 件"
    assert wait_for(lambda: dock.infos is not None and idle(dock))
    dock.set_focus(None)
    assert not dock.focus_row.isVisibleTo(dock) and [x.id for x in dock.shown] == ["REF-4"]


def test_records_and_things_are_linked_and_their_infos_listed(plugin):
    p, iface, infos, links, tmp_path = plugin
    dock = p.docks["SatObsReferences"]
    dock.show()
    dock.period.setCurrentIndex(dock.period.findData(None))
    assert wait_for(lambda: len(dock.infos or []) == 4 and idle(dock))
    obs = _image(p, iface, tmp_path)
    a = obs.book.add(139.7, 35.6, object="船A", kind="船舶 > 貨物船", activity="停泊")
    obs.render()
    row = next(i for i, records in enumerate(obs._row_records) if any(o.id == a.id for o in records))
    obs.table.selectRow(row)
    assert obs.reference_button.isVisibleTo(obs)
    menu = obs.reference_menu()
    assert isinstance(menu, QMenu)
    texts = [x.text() for x in menu.actions() if x.text()]
    assert texts == ["記録を参考情報に紐付け…（1 件）", "モノを参考情報に紐付け…（1 件）", "この記録・モノの参考情報"]
    # The record: REF-1 chosen in the window.
    menu.actions()[0].trigger()
    menu.close()
    pick = dock._pick
    assert pick.table.rowCount() == 4
    pick.table.selectRow(next(i for i, x in enumerate(pick.shown) if x.id == "REF-1"))
    pick.accept()
    assert wait_for(lambda: len(links.rows) == 1 and idle(dock))
    assert (links.rows[0]["attributes"]["ITEM_TYPE"], links.rows[0]["attributes"]["ITEM_ID"]) == (OBSERVATION, a.id)
    assert links.rows[0]["attributes"]["LABEL"].startswith("船A / 貨物船")
    # The thing: REF-2, and REF-1 again (twice the same pair stays one row).
    things = obs.reference_targets(THING, [a])
    assert [t.item_id for t in things] == [a.object_id]
    dock.link(["REF-2", "REF-1"], things)
    assert wait_for(lambda: len(links.rows) == 3 and idle(dock))
    dock.link(["REF-2"], things)
    assert wait_for(lambda: "紐付け済み" in dock.status.text() and idle(dock))
    assert len(links.rows) == 3
    # 「この記録・モノの参考情報」.
    menu = obs.reference_menu()
    menu.actions()[-1].trigger()
    menu.close()
    assert wait_for(lambda: dock.focus is not None and idle(dock))
    assert {x.id for x in dock.shown} == {"REF-1", "REF-2"} and "記録「船A" in dock.focus_label.text()
    # The chosen info's links, under the list and in its form (外す).
    dock.choose("REF-1")
    assert wait_for(lambda: "紐付け: " in dock.detail.text())
    assert "モノ「船A / 貨物船」" in dock.detail.text()
    form = dock.edit()
    assert form.links.count() == 2
    form.links.item(0).setSelected(True)
    form.unlink()
    assert wait_for(lambda: len(links.rows) == 2 and form.links.count() == 1)
    form.reject()


def test_the_events_tab(plugin):
    p, iface, infos, links, tmp_path = plugin
    dock = p.docks["SatObsReferences"]
    dock.show()
    dock.period.setCurrentIndex(dock.period.findData(None))
    assert wait_for(lambda: len(dock.infos or []) == 4 and idle(dock))
    events = p.api.events
    events.show()
    events.raise_()  # the tab beside 「参考情報」 in front
    assert wait_for(lambda: events.events == [] and events.task is None)
    quake = new_event("〇〇沖の座礁")
    events.save(quake)
    assert wait_for(lambda: events.chosen() is not None and events.task is None)
    assert events.tabs.isTabVisible(events.reference_tab)
    events.tabs.setCurrentIndex(events.reference_tab)
    assert events.buttons["ref_link"].isVisibleTo(events) and not events.buttons["open"].isVisibleTo(events)
    assert events.current_type() == "" and events.chosen_links() == []
    pick = events.link_references()
    pick.table.selectRow(next(i for i, x in enumerate(pick.shown) if x.id == "REF-1"))
    pick.accept()
    assert wait_for(lambda: events.reference_table.rowCount() == 1)
    assert links.rows[0]["attributes"]["ITEM_TYPE"] == EVENT and links.rows[0]["attributes"]["LABEL"] == "〇〇沖の座礁"
    assert events.reference_table.item(0, 1).text() == "湾内で貨物船が座礁" and events.reference_table.item(0, 4).text() == "田中"
    assert events.tabs.tabText(events.reference_tab) == "参考情報（1）"
    events.show_references()
    assert wait_for(lambda: dock.focus is not None and idle(dock))
    assert [x.id for x in dock.shown] == ["REF-1"] and "イベント「〇〇沖の座礁」" in dock.focus_label.text()
    events.reference_table.selectRow(0)
    events.unlink_references(confirm=False)
    assert wait_for(lambda: events.reference_table.rowCount() == 0 and not links.rows)


def test_json_import_tries_then_imports_and_lists_what_came_in(plugin):
    """「JSON 取り込み…」 (2026-11-25): the dry run in the window, then the import; the dock lists the infos imported."""
    p, iface, infos, links, tmp_path = plugin
    dock = p.docks["SatObsReferences"]
    dock.show()
    assert wait_for(lambda: dock.infos is not None and idle(dock))
    path = tmp_path / "警報.json"
    path.write_text(json.dumps({"defaults": {"source": "海上保安部", "reliability": "確定"}, "infos": [
        # An update of REF-1 (its summary), a new one in a named area (by its name), a duplicate of REF-4, an error.
        {"id": "REF-1", "title": "湾内で貨物船が座礁", "time": "2026-09-29T01:00:00Z", "sourceRef": "航行警報 第12号", "summary": "離礁作業中"},
        {"title": "浦賀水道で工事", "time": "2026-11-20T09:00", "area": "東京湾", "links": [{"type": "event", "id": "EV-1", "label": "工事"}]},
        {"title": "相模湾の新しい岸壁", "time": infos.rows[3]["attributes"]["INFO_TIME"] and _iso(infos.rows[3]["attributes"]["INFO_TIME"]),
         "source": "〇〇新聞"},
        {"title": "地名違い", "time": "2026-11-20", "area": "伊勢湾"},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert dock.buttons["import"].isEnabled()
    dialog = dock.open_import(str(path))
    assert wait_for(lambda: dialog.plan is not None and idle(dock))
    assert dialog.summary.text() == "新規 1・更新 1・重複（飛ばす） 1・エラー 1"
    assert [dialog.table.item(i, 1).text() for i in range(4)] == ["更新", "新規", "重複（飛ばす）", "エラー"]
    assert dialog.table.item(0, 5).text() == "変わる: 要旨" and dialog.table.item(1, 4).text().startswith("東京湾")
    assert dialog.table.item(3, 5).text() == "地名「伊勢湾」が見つからない"
    assert dialog.import_button.text() == "取り込む（2 件）" and dialog.import_button.isEnabled() and len(infos.rows) == 4
    dialog.problems_only.setChecked(True)
    assert dialog.table.rowCount() == 1
    dialog.problems_only.setChecked(False)
    assert dialog.run_import()
    assert wait_for(lambda: dialog.result is not None and idle(dock) and dock.focus is not None)
    assert dialog.summary.text() == "取り込み済み: 追加 1・更新 1・紐付け 1・重複（飛ばす） 1・エラー 1"
    assert len(infos.rows) == 5 and infos.rows[0]["attributes"]["SUMMARY"] == "離礁作業中"
    new = infos.rows[4]["attributes"]
    assert new["NAMED_AREA_ID"] == "NA-000001" and new["SOURCE"] == "海上保安部" and new["CREATED_USER"] == "田中"
    assert [(r["attributes"]["ITEM_ID"], r["attributes"]["INFO_ID"]) for r in links.rows] == [("EV-1", new["INFO_ID"])]
    assert dock.focus[0] == "取り込んだ参考情報" and {x.title for x in dock.shown} == {"湾内で貨物船が座礁", "浦賀水道で工事"}
    # The same file again: nothing to import.
    dialog.try_run()
    assert wait_for(lambda: dialog.plan is not None and idle(dock))
    assert dialog.summary.text() == "変更なし 1・重複（飛ばす） 2・エラー 1" and not dialog.import_button.isEnabled()
    # A file that does not fit the schema is refused as a whole.
    bad = tmp_path / "bad.json"
    bad.write_text('{"items": []}', encoding="utf-8")
    assert not dialog.choose_file(str(bad))
    assert dialog.summary.text() == "このファイルは取り込めない" and "知らないキー「items」" in dialog.notes.text()
    # The Python API: tried, then saved.
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"infos": [{"title": "API から", "time": "2026-11-21"}]}, ensure_ascii=False), encoding="utf-8")
    r = p.api.import_reference_infos(str(other))
    assert r["summary"] == "新規 1" and r["rows"][0]["action"] == "new" and len(infos.rows) == 5
    r = p.api.import_reference_infos(str(other), save=True)
    assert (r["added"], r["failures"]) == (1, []) and len(infos.rows) == 6
    assert p.api.import_reference_infos(str(bad))["problems"]


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_without_settings_the_dock_says_so(tmp_path, monkeypatch):
    QgsSettings().remove("mikage")
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    settings.set_config_source(str(path))
    iface = FakeIface()
    p = small_plugin(iface, Events, Images, Observations)
    p.initGui()
    try:
        dock = p.docks["SatObsReferences"]
        dock.show()
        assert "referenceInfo" in dock.note.text() and not dock.buttons["new"].isEnabled() and not dock.buttons["import"].isEnabled()
        assert not p.docks["SatObsEvents"].tabs.isTabVisible(p.docks["SatObsEvents"].reference_tab)
        assert p.api.observation_dock.reference_line.isHidden()
    finally:
        p.unload()
        QgsSettings().remove("mikage")
