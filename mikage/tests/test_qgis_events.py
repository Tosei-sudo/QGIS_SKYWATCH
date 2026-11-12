"""「イベント」 in QGIS: events listed, items added from the catalog / records / requests docks, opened, shown, exported."""
import json
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QMenu  # noqa: E402

from mikage.core.esri import EsriClient, EsriError  # noqa: E402
from mikage.core.events import CLOSED, LinkItem, new_event  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402

IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
EVENTS = "https://example.com/arcgis/rest/services/Events/FeatureServer/0"
LINKS = "https://example.com/arcgis/rest/services/Events/FeatureServer/1"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
DAY = 86400000


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    config = {
        "imageCatalogs": [{"label": "衛星画像", "url": IMAGES, "fields": {"id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "PATH"}}],
        "events": {"layer": EVENTS, "links": LINKS},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    images = fake.add_layer(IMAGES, FakeLayer({"IMG_ID": S, "ACQ": T, "SENSOR": S, "PATH": S}, "esriGeometryPolygon"))
    now = time.time() * 1000
    for i, x in enumerate((140.0, 140.2)):
        images.add({"IMG_ID": f"IMG{i}", "ACQ": now - (i + 1) * DAY, "SENSOR": "WV03", "PATH": None},
                   {"rings": [[[x, 37.0], [x, 37.1], [x + 0.1, 37.1], [x + 0.1, 37.0], [x, 37.0]]]})
    events = fake.add_layer(EVENTS, FakeLayer({"EVENT_ID": S, "EVENT_NAME": S, "EVENT_KIND": S, "STATUS": S, "START_AT": T, "END_AT": T,
                                               "NOTE": S}, "esriGeometryPolygon"))
    links = fake.add_layer(LINKS, FakeLayer({"EVENT_ID": S, "ITEM_TYPE": S, "ITEM_ID": S, "ITEM_LABEL": S, "ITEM_TIME": T, "SOURCE": S,
                                             "NOTE": S, "CREATED_USER": S, "CREATED_DATE": T}, ""))
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, events, links, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def idle(dock):
    return wait_for(lambda: dock.task is None and not dock._tasks)


def saved(dock, event):
    dock.save(event)
    assert wait_for(lambda: any(e.id == event.id for e in dock.events or []) and dock.task is None)


def test_events_and_their_items_from_the_other_docks(plugin, monkeypatch):
    p, iface, events, links, tmp_path = plugin
    monkeypatch.setenv("USERNAME", "田中")
    dock = p.api.events
    dock.show()
    assert wait_for(lambda: dock.events == [] and dock.task is None)
    ring = [[139.9, 36.9], [139.9, 37.2], [140.4, 37.2], [140.4, 36.9], [139.9, 36.9]]
    quake = new_event("〇〇沖地震", kind="地震", start=int(time.time() * 1000) - 5 * DAY, note="沿岸部を優先", rings=[ring])
    saved(dock, quake)
    saved(dock, new_event("豪雨", kind="風水害"))
    # The one saved last is chosen.
    assert dock.table.rowCount() == 2 and dock.chosen().name == "豪雨"
    dock.choose(quake.id)
    assert events.rows[0]["geometry"]["rings"] == [ring]

    # 画像カタログ: 「イベントに追加」 lists the open events, the one chosen in 「イベント」 first.
    catalog = p.api.image_catalog
    catalog.show()
    catalog.area_box.setCurrentIndex(catalog.area_box.findData("none"))
    catalog.search()
    assert wait_for(lambda: catalog.model.rowCount() == 2)
    assert catalog.buttons["event"].isVisibleTo(catalog)
    catalog.table.selectAll()
    menu = catalog.add_to_event()
    assert isinstance(menu, QMenu)
    texts = [a.text() for a in menu.actions()]
    assert texts[:2] == ["〇〇沖地震（選択中）", "豪雨"] and texts[-1] == "新しいイベント…"
    menu.actions()[0].trigger()
    menu.close()
    assert wait_for(lambda: len(links.rows) == 2 and dock.task is None)
    assert {r["attributes"]["ITEM_ID"] for r in links.rows} == {"IMG0", "IMG1"} and links.rows[0]["attributes"]["SOURCE"] == IMAGES
    # The same images again: nothing more.
    dock.link_items([quake.id], catalog.event_items())
    assert wait_for(lambda: "2 件は追加済み" in dock.status.text())
    assert len(links.rows) == 2

    # 観測記録: the chosen records.
    from qgis.core import QgsRasterLayer

    layer = QgsRasterLayer(geotiff(tmp_path / "d1.tif", "2026-10-01T01:00:00Z"), "d1")
    QgsProject.instance().addMapLayers([layer])
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    obs = p.api.observation_dock
    obs.show()
    book = obs.book
    a = book.add(140.05, 37.05, object="船A", kind="船舶 > 貨物船", activity="停泊")
    book.add(140.25, 37.05, object="船B", kind="船舶", activity="航行")
    items = obs.event_items([a])
    assert items[0].type == "observation" and items[0].label.startswith("船A / 貨物船 / 停泊") and items[0].source == "local"
    dock.link_items([quake.id], items)
    assert wait_for(lambda: len(links.rows) == 3 and dock.task is None)
    # 撮像依頼 (no ledger here): its items, as given.
    dock.link_items([quake.id], [LinkItem("request", "REQ1", "沿岸 臨時", None, "https://req")])
    assert wait_for(lambda: len(links.rows) == 4 and dock.task is None)
    assert [dock.table.item(0, j).text() for j in (5, 6, 7)] == ["2", "1", "1"]
    assert dock.tabs.tabText(0) == "画像（2）" and dock.item_tables["observation"].item(0, 0).text().startswith("船A")
    assert dock.item_tables["observation"].item(0, 3).text() == "田中"

    # 開く: a record opens in 観測記録.
    dock.tabs.setCurrentIndex(1)
    dock.item_tables["observation"].selectRow(0)
    opened = []
    monkeypatch.setattr(type(obs), "open_record", lambda self, o: opened.append(o.id))
    dock.open_items("observation")
    assert opened == [a.id]

    # 地図に表示: the area, the footprints, the record.
    dock.show_on_map()
    group = "イベント: 〇〇沖地震"
    assert wait_for(lambda: QgsProject.instance().layerTreeRoot().findGroup(group) is not None)
    names = sorted(l.name() for l in QgsProject.instance().mapLayers().values() if l.name() != "d1")
    assert names == ["画像の範囲", "範囲", "観測記録"]
    footprints = next(l for l in QgsProject.instance().mapLayers().values() if l.name() == "画像の範囲")
    assert footprints.featureCount() == 2
    # Again: the group is replaced, not doubled.
    dock.show_on_map()
    assert wait_for(lambda: idle(dock))
    assert len(QgsProject.instance().mapLayers()) == 4

    # カタログで表示.
    catalog.show_records(catalog._shown_catalog, [])
    dock.show_in_catalog()
    assert wait_for(lambda: catalog.model.rowCount() == 2)
    assert "〇〇沖地震" in catalog.status.text()

    # 画像を検索: over the event's area and period.
    dock.search_images()
    assert catalog.area_box.currentData() == "drawn" and catalog.drawn[0] == ring[0]
    assert catalog.acquired_from.text() and not catalog.acquired_to.text()
    assert wait_for(lambda: catalog.search_button.isEnabled())

    # Export, summary.
    out = dock.export(str(tmp_path / "quake.csv"))
    text = open(out, encoding="utf-8-sig").read()
    assert text.splitlines()[0].startswith("イベント,種別") and len(text.splitlines()) == 5
    assert dock.copy_summary() == QApplication.clipboard().text() and "画像 2 件　観測記録 1 件　撮像依頼 1 件" in dock.copy_summary()

    # A note on items, then taking one out.
    dock.tabs.setCurrentIndex(0)
    dock.item_tables["image"].selectRow(0)
    dock.edit_item_note("被害なし")
    assert wait_for(lambda: dock.links_of(dock.chosen(), "image")[0].note == "被害なし")
    dock.item_tables["image"].selectRow(0)
    dock.unlink(confirm=False)
    assert wait_for(lambda: len(links.rows) == 3 and dock.task is None)

    # Closing: out of the menus and of the 対応中 list; 再開 brings it back.
    dock.choose(quake.id)
    dock.toggle_status()
    assert wait_for(lambda: dock.task is None and any(e.id == quake.id and e.status == CLOSED for e in dock.events or []))
    assert dock.status_filter.currentData() == "" and dock.buttons["close"].text() == "再開"
    catalog.table.selectAll()
    menu = catalog.add_to_event()
    assert [a.text() for a in menu.actions()][0] == "豪雨"
    menu.close()
    dock.status_filter.setCurrentIndex(dock.status_filter.findData("対応中"))
    assert [e.name for e in dock.shown] == ["豪雨"]

    # Deleting takes its links too.
    dock.status_filter.setCurrentIndex(dock.status_filter.findData(""))
    dock.choose(quake.id)
    dock.delete(confirm=False)
    assert wait_for(lambda: dock.task is None and dock.events is not None and len(dock.events) == 1)
    assert links.rows == []


def test_without_settings_the_dock_says_so(tmp_path):
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_config_source("")
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    try:
        dock = p.api.events
        dock.show()
        assert "events" in dock.note.text() and not dock.buttons["new"].isEnabled()
        assert not p.api.image_catalog.buttons["event"].isVisibleTo(p.api.image_catalog)
    finally:
        p.unload()


def test_a_failed_save_keeps_the_form_and_a_reload_asked_meanwhile_is_not_lost(plugin, monkeypatch):
    p, iface, events, links, tmp_path = plugin
    dock = p.api.events
    dock.show()
    assert wait_for(lambda: dock.events == [] and dock.task is None)
    import mikage.ui.events_dock as module

    def broken(*_a, **_k):
        raise EsriError("ネットワーク")

    real = module.save_event
    monkeypatch.setattr(module, "save_event", broken)
    form = dock.create()
    form.name.setText("地震")
    form.has_start.setChecked(False)
    form._accept()
    assert wait_for(lambda: "保存できませんでした" in form.problem.text())
    assert form.isVisible() and form.name.text() == "地震" and form.values()["start"] is None
    monkeypatch.setattr(module, "save_event", real)
    form._accept()
    from qgis.PyQt import sip

    assert wait_for(lambda: (sip.isdeleted(form) or not form.isVisible()) and dock.events and dock.events[0].name == "地震")
    assert dock.events[0].start is None
    # An add from another dock while a reload runs: the list is read again after it.
    events.add({"EVENT_ID": "x", "EVENT_NAME": "外で作成", "STATUS": "対応中"})
    dock.reload()
    dock.reload()
    assert wait_for(lambda: dock.task is None and any(e.name == "外で作成" for e in dock.events or []))


def test_images_outside_the_catalog_go_into_events(plugin, monkeypatch):
    """An image opened by hand (by its path) and one listed without its file (「⚑」 of 観測記録), opened and shown from the event."""
    from qgis.core import QgsRasterLayer

    from mikage.core.events import LISTED

    p, iface, events, links, tmp_path = plugin
    dock = p.api.events
    dock.show()
    assert wait_for(lambda: dock.events == [] and dock.task is None)
    quake = new_event("〇〇沖地震")
    saved(dock, quake)
    path = geotiff(tmp_path / "hand.tif", "2026-10-01T01:00:00Z")
    layer = QgsRasterLayer(path, "hand")
    QgsProject.instance().addMapLayers([layer])
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    obs = p.api.observation_dock
    obs.show()
    obs.render()
    assert obs.image_event_button.isVisibleTo(obs)
    [item] = obs.image_event_items()
    assert (item.type, item.item_id, item.source) == ("image", "hand.tif", path) and item.time is not None
    menu = obs.add_image_to_event()
    assert isinstance(menu, QMenu)
    menu.actions()[0].trigger()
    menu.close()
    assert wait_for(lambda: len(links.rows) == 1 and dock.task is None)
    # Listed without its file: its source says so.
    obs.book.list_image("EXT-001", "外部 1", None, [140.0, 37.0, 140.1, 37.1])
    [listed] = obs.image_event_items()
    assert (listed.item_id, listed.label, listed.source) == ("EXT-001", "外部 1", LISTED)
    dock.link_items([quake.id], [listed])
    assert wait_for(lambda: len(links.rows) == 2 and dock.task is None)
    assert {r["attributes"]["SOURCE"] for r in links.rows} == {path, LISTED}

    # 地図に表示: both footprints, without asking a catalog (none says it misses them).
    dock.choose(quake.id)
    dock.show_on_map()
    assert wait_for(lambda: QgsProject.instance().layerTreeRoot().findGroup("イベント: 〇〇沖地震") is not None)
    footprints = next(l for l in QgsProject.instance().mapLayers().values() if l.name() == "画像の範囲")
    assert sorted(f["image_id"] for f in footprints.getFeatures()) == ["EXT-001", "hand.tif"]
    assert "カタログに見つからない" not in dock.status.text()

    # 開く: the image file opens again once its layer is gone; the listed one says where to choose it.
    QgsProject.instance().removeMapLayer(layer.id())
    warnings = []
    monkeypatch.setattr(iface.messageBar(), "pushWarning", lambda title, text, *a: warnings.append(text), raising=False)
    dock.open_items("image", dock.links_of(dock.chosen(), "image"))
    opened = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsRasterLayer)]
    assert [l.name() for l in opened] == ["hand.tif"] and iface.activeLayer() is opened[0]
    assert any("ファイルなしの画像" in w for w in warnings)
    # Opened again: the open layer is shown, not a second one.
    dock.open_items("image", [x for x in dock.links_of(dock.chosen(), "image") if x.source == path])
    assert len([l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsRasterLayer)]) == 1
    dock.show_in_catalog()
    assert "カタログの画像なし" in dock.status.text()
