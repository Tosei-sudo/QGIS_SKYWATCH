"""
The image catalog and the notices in QGIS (the viewer's catalog.spec.ts,
catalog-similar.spec.ts and catalog-watch.spec.ts, as far as they go):
searching, the list and its footprints, opening a local image through
`pathMappings`, 「類似画像の検索…」 from a layer, the list as a layer, and
watches turning new images into notices. Every service is a fake.
"""
import json
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.core.catalog.query import EMPTY_SEARCH  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.obs_images import IMAGE_ID  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_observations import geotiff  # noqa: E402

IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
TYPES = {"IMG_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
         "OFF_NADIR": "esriFieldTypeDouble", "AZ": "esriFieldTypeDouble", "PATH": "esriFieldTypeString"}
DAY = 86400000


def ring(x0, y0, x1, y1):
    # Clockwise, as Esri polygons have their outer rings.
    return {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}


@pytest.fixture
def plugin(tmp_path):
    QgsSettings().remove("satellite_observation")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    config = {
        "imageCatalogs": [{"label": "衛星画像", "url": IMAGES, "fields": {
            "id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR", "angle": "OFF_NADIR", "azimuth": "AZ", "source": "PATH"}}],
        "pathMappings": [{"prefix": "\\\\nas\\images", "label": "画像サーバー"}],
        "watchInterval": 30,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    layer = fake.add_layer(IMAGES, FakeLayer(TYPES, "esriGeometryPolygon"))
    now = time.time() * 1000
    layer.add({"IMG_ID": "A", "ACQ": now - 10 * DAY, "SENSOR": "S1", "OFF_NADIR": 10, "AZ": 100, "PATH": "\\\\nas\\images\\2026\\a.tif"},
              ring(139.6, 35.5, 139.8, 35.7))
    layer.add({"IMG_ID": "B", "ACQ": now - 20 * DAY, "SENSOR": "S1", "OFF_NADIR": 12, "AZ": 110, "PATH": "https://x.example/b.tif"},
              ring(139.65, 35.5, 139.8, 35.7))
    layer.add({"IMG_ID": "C", "ACQ": now - 15 * DAY, "SENSOR": "S2", "OFF_NADIR": 30, "AZ": 300, "PATH": None},
              ring(139.7, 35.6, 139.75, 35.65))
    folder = tmp_path / "nas"
    (folder / "2026").mkdir(parents=True)
    geotiff(folder / "2026" / "A.TIF")
    from satellite_observation import classFactory

    iface = FakeIface()
    iface.canvas.setDestinationCrs(QgsProject.instance().crs())
    p = classFactory(iface)
    p.initGui()
    p.context.esri = lambda: EsriClient(fake)
    assert p.api.problems == [], p.api.problems
    yield p, iface, folder, fake, layer
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def searched(dock):
    dock.show()
    dock.area_box.setCurrentIndex(dock.area_box.findData("none"))
    dock.search()
    assert wait_for(lambda: dock.search_button.isEnabled() and dock.model.rowCount() > 0)


def test_search_list_footprints_and_sorting(plugin):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    # As the service orders them (the fake keeps its own order).
    assert [r.id for r in dock.records] == ["A", "B", "C"]
    assert dock.status.text() == "3 件"
    headings = [dock.model.headerData(i, Qt.Orientation.Horizontal) for i in range(dock.model.columnCount())]
    assert headings == ["画像ID", "撮像日時", "センサー", "角度", "方位角", "場所"]
    assert [dock.model.index(r, 5).data() for r in range(3)] == ["ローカル", "COG", "なし"]
    # A heading sorts; empty values stay last.
    dock.table.sortByColumn(5, Qt.SortOrder.DescendingOrder)
    assert [r.id for r in dock.records][-1] == "C"
    dock.table.sortByColumn(3, Qt.SortOrder.DescendingOrder)
    assert [r.id for r in dock.records] == ["C", "B", "A"]
    # The footprints are drawn; a click on the map chooses (Ctrl adds), and the list follows.
    assert dock.overlay is not None and dock.overlay.all.numberOfVertices() > 0
    dock._picked(139.72, 35.62, 0.001, False)
    assert [r.id for r in dock.chosen()] == ["C"]
    dock._picked(139.61, 35.55, 0.001, True)
    assert sorted(r.id for r in dock.chosen()) == ["A", "C"]
    assert dock.overlay.chosen.numberOfVertices() > 0
    dock.copy_paths()
    from qgis.PyQt.QtWidgets import QApplication

    assert QApplication.clipboard().text() == "\\\\nas\\images\\2026\\a.tif"
    # The search asks for the newest first, with the count.
    asked = [form for url, form in fake.asked if url.endswith("/query") and "returnDistinctValues" not in form]
    assert asked[0]["returnCountOnly"] == "true" and asked[1]["orderByFields"] == "ACQ DESC"


def test_a_local_image_opens_through_the_folder_chosen_and_finds_similar_images(plugin, monkeypatch):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    asked = []
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: asked.append(mapping.prefix) or str(folder))
    [layer] = dock.open_records([r for r in dock.records if r.id == "A"])
    assert asked == ["\\\\nas\\images"] and settings.path_folder("\\\\nas\\images") == str(folder)
    assert layer.isValid() and layer.source().endswith("A.TIF")
    assert layer.customProperty(IMAGE_ID) == "A" and layer.customProperty("satobs/catalog") == "衛星画像"
    assert layer.customProperty("satobs/sensorAzimuth") == 100 and layer.customProperty("satobs/sensorElevation") > 0
    assert "撮像日時" in layer.metadata().abstract()
    # Opened again: the folder is remembered.
    dock.open_records([r for r in dock.records if r.id == "A"])
    assert asked == ["\\\\nas\\images"]
    # An image without a place says why.
    assert dock.open_records([r for r in dock.records if r.id == "C"]) == []
    assert "C に画像の場所なし" in dock.status.text()
    # The layer's right-click menu has 「類似画像の検索…」.
    assert layer in iface.layer_action_layers[id(p.similar_action)]
    iface.setActiveLayer(layer)
    p.similar_action.trigger()
    assert wait_for(lambda: dock.status.text().startswith("類似画像 "))
    # Same sensor, angle ±5°, azimuth ±20°: B only (C is another sensor); 重なり shown.
    assert [r.id for r in dock.records] == ["B"]
    assert dock.model.headerData(dock.model.columnCount() - 1, Qt.Orientation.Horizontal) == "重なり"
    assert dock.model.index(0, dock.model.columnCount() - 1).data() == "75%"
    assert dock.overlay.reference.numberOfVertices() > 0
    assert settings.similar_tolerance()["angle"] == 5
    dock.normal_search()
    assert wait_for(lambda: dock.status.text() == "3 件") and dock.similar.isHidden()
    # Forgetting the folders.
    dock.clear_folders()
    assert settings.path_folder("\\\\nas\\images") is None


def test_the_list_as_a_layer_with_its_time(plugin):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    layer = dock.add_layer()
    assert layer.featureCount() == 3 and layer.name() == "画像カタログ 衛星画像"
    assert layer.temporalProperties().isActive() and layer.temporalProperties().startField() == "ACQ"
    assert {f["IMG_ID"] for f in layer.getFeatures()} == {"A", "B", "C"}


def test_watches_notify_new_images_only(plugin):
    p, iface, folder, fake, images = plugin
    notices = p.api.notices
    catalog = p.api.config.image_catalogs[0]
    watch = notices.add_watch(catalog, TYPES, EMPTY_SEARCH.with_(sensor="S1"), [], "S1 の新着")
    assert wait_for(lambda: notices.store().watch(watch.id).last_checked is not None)
    # The first check only notes what is there.
    assert notices.store().notices() == [] and sorted(notices.store().watch(watch.id).seen) == ["A", "B"]
    assert notices.store().watch(watch.id).catalog.token is None
    now = time.time() * 1000
    images.add({"IMG_ID": "D", "ACQ": now - DAY, "SENSOR": "S1", "OFF_NADIR": 5, "AZ": 90, "PATH": "https://x.example/d.tif"}, ring(139, 35, 140, 36))
    images.add({"IMG_ID": "E", "ACQ": now - DAY, "SENSOR": "S2", "PATH": None}, ring(139, 35, 140, 36))
    notices.check(force=True)
    assert wait_for(lambda: notices.notices.topLevelItemCount() == 1)
    assert notices.notices.topLevelItem(0).text(2) == "D"
    assert p.notices_action.text() == "通知（1）"
    # Checked again: nothing new.
    notices.check(force=True)
    assert wait_for(lambda: not notices._checking)
    assert len(notices.store().notices()) == 1
    # Moving to it marks it read.
    notices.notices.topLevelItem(0).setSelected(True)
    notices.zoom_to_chosen()
    assert p.notices_action.text() == "通知"
    # Off, renamed and deleted.
    item = notices.watches.topLevelItem(0)
    item.setCheckState(0, Qt.CheckState.Unchecked)
    assert not notices.store().watch(watch.id).enabled
    notices.watches.setCurrentItem(item)
    notices.delete_chosen_watch()
    assert notices.store().watches() == [] and notices.store().notices() == []


def test_a_point_watch_and_the_plan_reminders(plugin, monkeypatch):
    p, iface, folder, fake, images = plugin
    notices = p.api.notices
    from satellite_observation.ui import notices_dock

    class Accept:
        def __init__(self, lon, lat, catalogs, watches, parent):
            self.name = type("N", (), {"text": lambda self: "東京湾"})()
            self._watches = watches

        def exec(self):
            return notices_dock.QDialog.DialogCode.Accepted

        def chosen_watch(self):
            return self._watches[0].id if self._watches else None

        def chosen_catalog(self):
            return p.api.config.image_catalogs[0]

    monkeypatch.setattr(notices_dock, "PointWatchDialog", Accept)
    notices.watch_point(139.7, 35.6)
    assert wait_for(lambda: notices.store().watches() and notices.store().watches()[0].last_checked is not None)
    [watch] = notices.store().watches()
    assert watch.name == "東京湾" and watch.areas == [{"kind": "points", "points": [[139.7, 35.6]]}]
    # Another point added to it: the images there already are noted first.
    notices.watch_point(10.5, 10.5)
    assert wait_for(lambda: len(notices.store().watch(watch.id).areas[0]["points"]) == 2)
    # A reminder of the imaging plan becomes a notice.
    from satellite_observation.core.plan.remind import PlanReminder

    r = PlanReminder("x|1", time.time() * 1000 + 600000, time.time() * 1000, "OPT-1", "東京", 10.0, [[[139, 35], [139, 36], [140, 36], [139, 35]]])
    notices.add_plan_notice(r, "10/23 10:00 OPT-1 が撮像（あと 10 分）・東京・オフナディア角 10°")
    item = notices.notices.topLevelItem(0)
    assert item.text(1) == "撮像計画" and item.text(2).startswith("10/23 10:00 OPT-1")
    assert p.notices_action.text() == "通知（1）"
