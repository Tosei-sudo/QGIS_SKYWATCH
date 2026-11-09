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
from qgis.PyQt.QtCore import Qt, QTimer  # noqa: E402

from skywatch.core.esri import EsriClient  # noqa: E402
from skywatch.core.catalog.query import EMPTY_SEARCH  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.qgis_io.context import PluginContext  # noqa: E402
from skywatch.qgis_io.obs_images import IMAGE_ID  # noqa: E402
from skywatch.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from skywatch.tests.test_qgis_observations import geotiff  # noqa: E402

IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
TYPES = {"IMG_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
         "OFF_NADIR": "esriFieldTypeDouble", "AZ": "esriFieldTypeDouble", "PATH": "esriFieldTypeString"}
DAY = 86400000


def ring(x0, y0, x1, y1):
    # Clockwise, as Esri polygons have their outer rings.
    return {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsSettings().remove("skywatch")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    config = {
        "imageCatalogs": [{"label": "衛星画像", "url": IMAGES, "fields": {
            "id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR", "angle": "OFF_NADIR", "azimuth": "AZ", "source": "PATH"},
            # A record whose path is a folder: its images by sensor.
            "imagePatterns": {"S1": "^PAN/.*_P\\.tif$", "*": "\\.jp2$"}}],
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
    from skywatch import classFactory

    iface = FakeIface()
    iface.canvas.setDestinationCrs(QgsProject.instance().crs())
    p = classFactory(iface)
    # The docks open at start read their catalogs right away.
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    # Each test opens the docks it uses.
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, folder, fake, layer
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("skywatch")


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


def test_the_footprints_filled_as_outlines_or_hidden(plugin):
    """「フットプリント」 (tsudo, 2026-11-09: filled footprints made the image under them hard to see)."""
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    # Outlines unless chosen otherwise.
    assert dock.footprint_box.currentData() == "outline" and settings.footprint_style() == "outline"
    searched(dock)
    overlay = dock.overlay
    assert overlay.style == "outline"
    assert overlay.all.numberOfVertices() > 0 and overlay.all.fillColor().alpha() == 0
    dock._picked(139.61, 35.55, 0.001, False)
    assert [r.id for r in dock.chosen()] == ["A"]
    assert overlay.chosen.numberOfVertices() > 0 and overlay.chosen.fillColor().alpha() == 0
    # The id above the footprint's top corner (35.7), not over the middle of the image.
    [label] = overlay.labels
    assert label._text == "A" and label._map_point.y() == pytest.approx(35.7) and label._offset_y < 0
    assert label._map_point.x() == pytest.approx(139.7)
    # Filled: as before, the id in the middle.
    dock.footprint_box.setCurrentIndex(dock.footprint_box.findData("fill"))
    assert settings.footprint_style() == "fill" and overlay.style == "fill"
    assert overlay.all.fillColor().alpha() > 0 and overlay.chosen.fillColor().alpha() > 0
    assert overlay.chosen.numberOfVertices() > 0
    [label] = overlay.labels
    assert 35.5 < label._map_point.y() < 35.7
    # Hidden: nothing on the map, the choice and a click on the map still work.
    dock.set_footprint_style("hidden")
    assert dock.footprint_box.currentData() == "hidden" and settings.footprint_style() == "hidden"
    assert overlay.all.numberOfVertices() == overlay.chosen.numberOfVertices() == 0 and overlay.labels == []
    assert [r.id for r in dock.chosen()] == ["A"]
    dock._picked(139.72, 35.62, 0.001, True)
    assert sorted(r.id for r in dock.chosen()) == ["A", "C"] and overlay.chosen.numberOfVertices() == 0
    # The area drawn for the search stays (a frame).
    dock.set_drawn([[139.71, 35.61], [139.74, 35.61], [139.74, 35.64], [139.71, 35.61]])
    assert overlay.area.numberOfVertices() > 0 and overlay.area.fillColor().alpha() == 0
    # Shown again: the choice drawn again; the dock opened again keeps the style.
    dock.set_footprint_style("outline")
    assert overlay.all.numberOfVertices() > 0 and overlay.chosen.numberOfVertices() > 0 and len(overlay.labels) == 2
    dock.set_footprint_style("hidden")
    dock.hide()
    assert dock.overlay is None
    dock.show()
    assert dock.overlay.style == "hidden" and dock.overlay.all.numberOfVertices() == 0


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
    # Forgetting the folders: asked first, いいえ keeps them.
    from skywatch.ui.docks import QMessageBox as Box
    answers = []
    monkeypatch.setattr(Box, "question", lambda *a: answers.append(a) or a[4])
    dock.clear_folders()
    assert settings.path_folder("\\\\nas\\images") is not None and answers[0][4] == Box.StandardButton.No
    monkeypatch.setattr(Box, "question", lambda *a: Box.StandardButton.Yes)
    dock.clear_folders()
    assert settings.path_folder("\\\\nas\\images") is None


def test_the_extent_by_default_and_an_area_drawn_on_the_map(plugin, monkeypatch):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    assert dock.area_box.currentData() == "extent"
    dock.show()
    # 「描いた範囲」 without one starts drawing.
    dock._area_chosen(dock.area_box.findData("drawn"))
    assert dock.draw_button.isChecked() and iface.canvas.mapTool() is dock.draw_tool
    # Corners clicked (map units are degrees here), then closed: only C lies inside.
    from qgis.core import QgsPointXY

    dock.draw_tool.points = [QgsPointXY(139.71, 35.61), QgsPointXY(139.74, 35.61), QgsPointXY(139.74, 35.64)]
    dock.draw_tool.finish()
    assert dock.area_box.currentData() == "drawn" and not dock.draw_button.isChecked()
    assert dock.drawn[0] == dock.drawn[-1] and len(dock.drawn) == 4
    assert dock.overlay.area.numberOfVertices() > 0
    fake.asked.clear()
    dock.search()
    assert wait_for(lambda: dock.search_button.isEnabled() and dock.status.text().endswith("件"))
    form = next(f for u, f in fake.asked if u.endswith("/query") and "returnCountOnly" not in f)
    assert form["geometryType"] == "esriGeometryPolygon" and json.loads(form["geometry"])["rings"][0][0] == [139.71, 35.61]
    # The watch dialog offers the area drawn first.
    from skywatch.ui.watch_dialog import WatchDialog

    dialog = WatchDialog(p.api.config.image_catalogs[0], EMPTY_SEARCH, iface.canvas, None, None, dock.drawn)
    assert dialog.area.currentData() == "drawn" and dialog.areas()[0]["kind"] == "polygon"
    # Forgotten: back to the extent.
    dock.set_drawn(None)
    assert dock.area_box.currentData() == "extent" and dock.overlay.area.numberOfVertices() == 0


def test_the_list_as_a_layer_with_its_time(plugin):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    layer = dock.add_layer()
    assert layer.featureCount() == 3 and layer.name() == "画像カタログ 衛星画像"
    assert layer.temporalProperties().isActive() and layer.temporalProperties().startField() == "ACQ"
    assert {f["IMG_ID"] for f in layer.getFeatures()} == {"A", "B", "C"}


def test_watches_notify_new_images_only(plugin, monkeypatch):
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
    from skywatch.ui.docks import QMessageBox as Box
    monkeypatch.setattr(Box, "question", lambda *a: Box.StandardButton.No)
    notices.delete_chosen_watch()
    assert len(notices.store().watches()) == 1
    monkeypatch.setattr(Box, "question", lambda *a: Box.StandardButton.Yes)
    notices.delete_chosen_watch()
    assert notices.store().watches() == [] and notices.store().notices() == []


def test_a_check_asked_for_while_one_runs_is_not_lost(plugin, monkeypatch):
    """The cause of the old flakiness: 「確認」 asked for while a check was ending was dropped."""
    import threading

    from skywatch.ui import notices_dock

    p, iface, folder, fake, images = plugin
    notices = p.api.notices
    watch = notices.add_watch(p.api.config.image_catalogs[0], TYPES, EMPTY_SEARCH.with_(sensor="S1"), [], "S1 の新着")
    assert wait_for(lambda: notices.store().watch(watch.id).last_checked is not None and not notices._checking)
    # A check that has asked the catalog and not yet ended …
    asked = threading.Event()
    release = threading.Event()
    real = notices_dock.check_due

    def held(*args, **kwargs):
        added = real(*args, **kwargs)
        asked.set()
        release.wait(10)
        return added

    monkeypatch.setattr(notices_dock, "check_due", held)
    notices.check(force=True)
    assert wait_for(asked.is_set)
    # … when a new image comes and 「確認」 is asked for again.
    images.add({"IMG_ID": "D", "ACQ": time.time() * 1000 - DAY, "SENSOR": "S1", "PATH": None}, ring(139, 35, 140, 36))
    notices.check(force=True)
    release.set()
    assert wait_for(lambda: notices.notices.topLevelItemCount() == 1)
    assert notices.notices.topLevelItem(0).text(2) == "D"


def test_a_point_watch_and_the_plan_reminders(plugin, monkeypatch):
    p, iface, folder, fake, images = plugin
    notices = p.api.notices
    from skywatch.ui import notices_dock

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
    from skywatch.core.plan.remind import PlanReminder

    r = PlanReminder("x|1", time.time() * 1000 + 600000, time.time() * 1000, "OPT-1", "東京", 10.0, [[[139, 35], [139, 36], [140, 36], [139, 35]]])
    notices.add_plan_notice(r, "10/23 10:00 OPT-1 が撮像（あと 10 分）・東京・オフナディア角 10°")
    item = notices.notices.topLevelItem(0)
    assert item.text(1) == "撮像計画" and item.text(2).startswith("10/23 10:00 OPT-1")
    assert p.notices_action.text() == "通知（1）"


def test_closing_the_docks_cancels_their_tasks(plugin):
    p, iface, folder, fake, images = plugin
    for dock in (p.api.image_catalog, p.api.notices):
        ended = []

        def work(task):
            while not task.isCanceled():
                time.sleep(0.01)
            task.check()

        dock._start("止まらない作業", work, lambda r: ended.append("done"), lambda m: ended.append(m))
        dock.close_down()
        assert wait_for(lambda: ended, 5000)
        assert ended == ["取消"]


def _layer(kind: str, crs: str, wkts) -> "QgsVectorLayer":
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

    layer = QgsVectorLayer(f"{kind}?crs={crs}", "範囲", "memory")
    features = []
    for wkt in wkts:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    return layer


def _asked_shape(fake):
    form = next(f for u, f in fake.asked if u.endswith("/query") and "returnCountOnly" not in f)
    return form["geometryType"], json.loads(form["geometry"])


def test_searching_over_a_layers_geometry_as_it_is(plugin):
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry

    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    dock.show()
    # A triangle in Web Mercator over C only; and a second polygon overlapping it: joined into one.
    to_3857 = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())

    def mercator(wkt):
        g = QgsGeometry.fromWkt(wkt)
        g.transform(to_3857)
        return g.asWkt()

    polygons = _layer("Polygon", "EPSG:3857", [mercator("POLYGON((139.71 35.61, 139.74 35.61, 139.74 35.64, 139.71 35.61))"),
                                               mercator("POLYGON((139.72 35.61, 139.73 35.61, 139.73 35.60, 139.72 35.61))")])
    # 「レイヤー」 chosen: the active layer is taken, and the row with the layer shows.
    iface.setActiveLayer(polygons)
    i = dock.area_box.findData("layer")
    dock.area_box.setCurrentIndex(i)
    dock._area_chosen(i)
    assert dock.area_layer.currentLayer() is polygons and not dock.layer_row.isHidden()
    fake.asked.clear()
    dock.search()
    assert wait_for(lambda: dock.search_button.isEnabled() and dock.status.text().endswith("件"))
    kind, shape = _asked_shape(fake)
    assert kind == "esriGeometryPolygon" and len(shape["rings"]) == 1
    ring = shape["rings"][0]
    # The shape itself in WGS 84 (not its extent): the union's corners, the outer ring clockwise.
    assert ring[0] == ring[-1] and len(ring) > 5
    assert any(abs(x - 139.71) < 1e-6 and abs(y - 35.61) < 1e-6 for x, y in ring)
    assert any(abs(x - 139.73) < 1e-6 and abs(y - 35.60) < 1e-6 for x, y in ring)
    assert sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) < 0
    assert {r.id for r in dock.records} == {"A", "B", "C"}

    # Only the chosen features: none chosen says so and asks nothing.
    dock.selected_only.setChecked(True)
    fake.asked.clear()
    dock.search()
    assert dock.status.text() == "「範囲」に選択地物なし" and not fake.asked
    polygons.selectByIds([next(polygons.getFeatures()).id()])
    dock.search()
    assert wait_for(lambda: dock.search_button.isEnabled() and dock.status.text().endswith("件"))
    assert len(_asked_shape(fake)[1]["rings"][0]) == 4

    # A line layer: a polyline; a point layer: a multipoint.
    lines = _layer("LineString", "EPSG:4326", ["LINESTRING(139.71 35.62, 139.74 35.63)", "LINESTRING(139.60 35.52, 139.61 35.53)"])
    dock.use_layer(lines)
    fake.asked.clear()
    dock.search()
    assert wait_for(lambda: dock.search_button.isEnabled() and dock.status.text().endswith("件"))
    kind, shape = _asked_shape(fake)
    assert kind == "esriGeometryPolyline" and sorted(shape["paths"]) == [[[139.6, 35.52], [139.61, 35.53]], [[139.71, 35.62], [139.74, 35.63]]]
    points = _layer("Point", "EPSG:4326", ["POINT(139.72 35.62)", "POINT(139.79 35.69)"])
    dock.use_layer(points)
    fake.asked.clear()
    dock.search()
    assert wait_for(lambda: dock.search_button.isEnabled() and dock.status.text().endswith("件"))
    kind, shape = _asked_shape(fake)
    assert kind == "esriGeometryMultipoint" and shape["points"] == [[139.72, 35.62], [139.79, 35.69]]

    # The layer removed: nothing chosen, and the search says so.
    QgsProject.instance().removeMapLayer(points.id())
    dock.area_layer.setLayer(None)
    fake.asked.clear()
    dock.search()
    assert dock.status.text() == "範囲のレイヤーが未選択" and not fake.asked
    # Back to the extent: the layer row hides.
    dock.area_box.setCurrentIndex(dock.area_box.findData("extent"))
    assert dock.layer_row.isHidden()


def test_a_record_whose_path_is_a_folder_opens_the_images_its_sensor_names(plugin, monkeypatch):
    from skywatch.core.catalog.search import record_of
    from skywatch.qgis_io.catalog_io import IMAGE_FILE

    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    scene = folder / "2026" / "Scene_D"
    for rel in ("PAN/b_P.TIF", "PAN/a_P.tif", "PAN/sub/c_P.tif", "MS/a_M.tif", "PAN/a_P.tif.aux.xml"):
        (scene / rel).parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".xml"):
            (scene / rel).write_text("<PAMDataset/>")
        else:
            geotiff(scene / rel)
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    catalog = dock._shown_catalog
    record = record_of({"attributes": {"IMG_ID": "D", "SENSOR": "S1", "PATH": "\\\\nas\\images\\2026\\scene_d"}}, catalog.fields)
    asked = []
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: asked.append((r.id, files, more)) or files[:2])
    layers = dock.open_records([record])
    # S1's pattern: the PAN images at any depth (not MS, nor the .aux.xml), in natural order; the two chosen open.
    assert asked == [("D", ["PAN/a_P.tif", "PAN/b_P.TIF", "PAN/sub/c_P.tif"], False)]
    assert [l.name() for l in layers] == ["PAN/a_P.tif", "PAN/b_P.TIF"]
    assert all(l.isValid() and l.customProperty(IMAGE_ID) == "D" for l in layers)
    assert [l.customProperty(IMAGE_FILE) for l in layers] == ["PAN/a_P.tif", "PAN/b_P.TIF"]
    group = QgsProject.instance().layerTreeRoot().findGroup("D")
    assert group is not None and [n.layerId() for n in group.findLayers()] == [l.id() for l in layers]
    # Cancelled: nothing opens.
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: None)
    assert dock.open_records([record]) == []
    # Another sensor: the `*` pattern (.jp2), none here, so the reason is shown.
    other = record_of({"attributes": {"IMG_ID": "E", "SENSOR": "S7", "PATH": "\\\\nas\\images\\2026\\scene_d"}}, catalog.fields)
    assert dock.open_records([other]) == []
    assert "E のフォルダ" in dock.status.text() and "画像なし" in dock.status.text()
    # One image only: opened without asking, by itself (no group).
    (scene / "only").mkdir()
    geotiff(scene / "only" / "x.jp2")
    one = record_of({"attributes": {"IMG_ID": "F", "SENSOR": "S7", "PATH": "\\\\nas\\images\\2026\\scene_d\\only"}}, catalog.fields)
    asked.clear()
    [layer] = dock.open_records([one])
    assert layer.name() == "F x.jp2" and layer.customProperty(IMAGE_FILE) == "x.jp2"
    assert QgsProject.instance().layerTreeRoot().findGroup("F") is None


def test_a_rewritten_path_is_opened_when_it_is_there_and_the_path_as_written_otherwise(tmp_path):
    """`pathMappings` `pattern` / `to`: //(host)/(any folders)/(image ID) → (drive):/(image ID), else the path as written."""
    from skywatch.core.catalog import catalog_of
    from skywatch.core.catalog.paths import PathError, path_mappings_of
    from skywatch.core.catalog.search import record_of
    from skywatch.qgis_io.catalog_io import locate_record

    QgsSettings().remove("skywatch")
    local, nas = tmp_path / "local", tmp_path / "nas"
    for rel in ("local/SCENE_X/PAN/a.tif", "local/SCENE_Z/notes.txt", "local/b.tif",
                "nas/2026/11/SCENE_Y/a.tif", "nas/2026/11/SCENE_Z/a.tif", "nas/2026/11/b.tif"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".tif"):
            geotiff(tmp_path / rel)
        else:
            (tmp_path / rel).write_text("")
    problems = []
    mappings = path_mappings_of([
        {"pattern": "^//[^/]+/(?:[^/]+/)*([^/]+)$", "to": local.as_posix() + "/$1", "label": "手元の複製"},
        {"prefix": "\\\\nas\\images", "label": "画像サーバー"},
    ], problems)
    assert not problems
    settings.set_path_folder("\\\\nas\\images", str(nas))
    catalog = catalog_of({"url": IMAGES, "fields": {"id": "IMG_ID", "source": "PATH"}}, problems, 0)

    def where(path):
        record = record_of({"attributes": {"IMG_ID": "R", "PATH": path}}, catalog.fields)
        return locate_record(record, catalog, mappings, lambda mapping: pytest.fail("no folder to choose"))

    # There: the copy opens (a folder with its images, a file), however deep the path was.
    found = where("\\\\NAS\\images\\2026\\11\\SCENE_X")
    assert found.place == (local / "SCENE_X").as_posix() and found.files == ["PAN/a.tif"]
    assert where("\\\\nas\\images\\2026\\11\\b.tif").place == (local / "b.tif").as_posix()
    # Written as //host/… (a protocol-relative URL otherwise): the copy too.
    assert where("//nas/images/2026/11/SCENE_X").place == (local / "SCENE_X").as_posix()
    # Not there, or a folder without images: the path as written (here through the folder chosen for the NAS).
    assert where("\\\\nas\\images\\2026\\11\\SCENE_Y").place == str(nas / "2026" / "11" / "SCENE_Y")
    found = where("\\\\nas\\images\\2026\\11\\SCENE_Z")
    assert found.place == str(nas / "2026" / "11" / "SCENE_Z") and found.files == ["a.tif"]
    url = where("//nas/images/2026/11/SCENE_Y")
    assert url.is_url and url.place == "https://nas/images/2026/11/SCENE_Y"
    # Nowhere: both are in the reason.
    with pytest.raises(PathError) as e:
        where("\\\\nas\\images\\2026\\11\\SCENE_W")
    assert "2026/11/SCENE_W なし" in str(e.value) and f"読み替え先も: {(local / 'SCENE_W').as_posix()} なし" in str(e.value)
    with pytest.raises(PathError, match="SCENE_Z に画像なし"):
        where("\\\\nas\\images\\SCENE_Z")


def test_the_dialog_choosing_a_records_images():
    from skywatch.ui.image_files_dialog import ImageFilesDialog

    dialog = ImageFilesDialog("D", ["PAN/a.tif", "PAN/b.tif", "MS/a.tif"], more=True)
    assert dialog.chosen() == ["PAN/a.tif", "PAN/b.tif", "MS/a.tif"]
    dialog.filter.setText("ms/")
    dialog._set_shown(Qt.CheckState.Unchecked)
    assert dialog.chosen() == ["PAN/a.tif", "PAN/b.tif"]
    dialog.filter.setText("")
    dialog._set_shown(Qt.CheckState.Unchecked)
    assert dialog.chosen() == [] and not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()


def test_a_double_click_in_the_dialog_opens_that_image_alone():
    from qgis.PyQt.QtTest import QTest
    from qgis.PyQt.QtWidgets import QDialog

    from skywatch.ui.image_files_dialog import ImageFilesDialog

    dialog = ImageFilesDialog("D", ["PAN/a.tif", "PAN/b.tif", "MS/a.tif"])
    dialog.list.item(2).setCheckState(Qt.CheckState.Unchecked)  # whatever is checked
    dialog.show()
    QTest.qWaitForWindowExposed(dialog)
    at = dialog.list.visualItemRect(dialog.list.item(2)).center()
    # What a window system sends for a double click: press, release, then the double click.
    QTest.mouseClick(dialog.list.viewport(), Qt.MouseButton.LeftButton, pos=at)
    QTest.mouseDClick(dialog.list.viewport(), Qt.MouseButton.LeftButton, pos=at)
    assert dialog.result() == QDialog.DialogCode.Accepted and not dialog.isVisible()
    assert dialog.chosen() == ["MS/a.tif"]


def test_a_long_look_in_a_folder_keeps_qgis_answering_and_can_be_cancelled(tmp_path):
    """#76: the folder is looked through off the main thread; cancelling the window stops waiting."""
    import threading

    from qgis.PyQt.QtWidgets import QApplication, QProgressDialog

    from skywatch.core.catalog.images import FolderImages
    from skywatch.qgis_io.catalog_io import Cancelled, scan_folder

    assert scan_folder(str(tmp_path), ["x"], find=lambda folder, patterns, cancelled: FolderImages(["a.tif"])).files == ["a.tif"]
    release = threading.Event()

    def slow(folder, patterns, cancelled):
        release.wait(5)
        return FolderImages([], stopped="取消" if cancelled() else "")

    def cancel_soon():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QProgressDialog) and w.isVisible():
                w.cancel()
                return
        QTimer.singleShot(20, cancel_soon)

    QTimer.singleShot(50, cancel_soon)
    with pytest.raises(Cancelled):
        scan_folder(str(tmp_path), ["x"], find=slow, wait_before_dialog=0.01)
    release.set()


def test_the_list_says_who_works_each_image_and_shows_the_unworked_only(plugin, tmp_path):
    """分担 column (`assignments`): 未着手 / 作業中（名前） / 完了, and 「未着手のみ」."""
    from skywatch.core.obs.assignments import assignment_source_of, take

    p, iface, folder, fake, _ = plugin
    work_url = "https://x/arcgis/rest/services/Work/FeatureServer/0"
    s, d = "esriFieldTypeString", "esriFieldTypeDate"
    fake.add_layer(work_url, FakeLayer({"IMAGE_ID": s, "IMAGE_NAME": s, "IMAGE_TIME": d, "ASSIGNEE": s, "STATUS": s, "NOTE": s,
                                        "TAKEN_AT": d, "DONE_AT": d}, ""))
    take(EsriClient(fake), assignment_source_of(work_url, []), "B", "鈴木", 1000)
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["assignments"] = work_url
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.context.reload()
    dock = p.api.image_catalog
    searched(dock)
    headings = [dock.model.headerData(i, Qt.Orientation.Horizontal) for i in range(dock.model.columnCount())]
    assert headings[:2] == ["画像ID", "分担"]
    column = 1
    assert wait_for(lambda: dock.model.index(0, column).data() != "…")
    cells = {r.id: dock.model.index(i, column).data() for i, r in enumerate(dock.records)}
    assert cells == {"A": "未着手", "B": "作業中（鈴木）", "C": "未着手"}
    assert dock.unworked_only.isVisibleTo(dock)
    dock.unworked_only.setChecked(True)
    hidden = {r.id for i, r in enumerate(dock.records) if dock.table.isRowHidden(i)}
    assert hidden == {"B"}
    # Sorting keeps the filter on the right rows.
    dock.table.sortByColumn(3, Qt.SortOrder.DescendingOrder)
    assert {r.id for i, r in enumerate(dock.records) if dock.table.isRowHidden(i)} == {"B"}
    # Someone takes A: read again (every minute; asked here).
    take(EsriClient(fake), assignment_source_of(work_url, []), "A", "田中", 2000)
    dock.load_work()
    assert wait_for(lambda: {r.id for i, r in enumerate(dock.records) if dock.table.isRowHidden(i)} == {"A", "B"})
    dock.unworked_only.setChecked(False)
    assert not any(dock.table.isRowHidden(i) for i in range(len(dock.records)))


def test_the_free_word_box_of_a_catalog_with_keyword_fields(plugin, tmp_path):
    """「フリーワード」: shown only for `fields.keywords`, Enter searches, partly in any of them; views and watches keep it."""
    p, iface, folder, fake, layer = plugin
    dock = p.api.image_catalog
    dock.show()
    # The catalog of the settings file has no keyword fields: no box.
    assert not dock.keyword.isVisibleTo(dock) and not dock.keyword_label.isVisibleTo(dock)
    layer.types["NOTE"] = "esriFieldTypeString"
    for row, note in zip(layer.rows, ["横須賀港 定期観測", "横須賀港 臨時観測（ＥＶ０１）", "東京港 臨時"]):
        row["attributes"]["NOTE"] = note
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["imageCatalogs"][0]["fields"]["keywords"] = ["NOTE", "IMG_ID"]
    config["imageCatalogs"][0]["columns"] = [{"field": "NOTE", "label": "備考"}]
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.context.reload()
    assert p.api.problems == []
    assert dock.keyword.isVisibleTo(dock) and dock.keyword_label.isVisibleTo(dock)
    assert dock.keyword.placeholderText() == "備考・画像ID に部分一致"
    dock.area_box.setCurrentIndex(dock.area_box.findData("none"))

    def found(text):
        dock.keyword.setText(text)
        dock.model.set_records(dock.catalog(), [])
        dock.keyword.returnPressed.emit()
        assert wait_for(lambda: dock.search_button.isEnabled() and dock.status.text().endswith("件"))
        return sorted(r.id for r in dock.records)

    assert found("臨時") == ["B", "C"]
    assert found("ev01 横須賀") == ["B"]
    assert found("x' OR '1'='1") == []
    assert found("") == ["A", "B", "C"]
    # The query asked: each word in either attribute, the word a value.
    def asked_where():
        return [form["where"] for url, form in fake.asked if url.endswith("/query") and "returnCountOnly" not in form][-1]

    found("臨時")
    assert asked_where() == "(UPPER(NOTE) LIKE '%臨時%' ESCAPE '\\' OR UPPER(IMG_ID) LIKE '%臨時%' ESCAPE '\\')"
    found("o'k")
    assert asked_where().startswith("(UPPER(NOTE) LIKE '%O''K%' ESCAPE '\\' OR ")
    # A view keeps the word; 「この条件で監視…」 gets it.
    dock.keyword.setText("臨時")
    state = dock.views.state()
    dock.keyword.setText("")
    dock.views.apply(state)
    assert dock.keyword.text() == "臨時"
    asked = []
    dock.watch_requested = lambda catalog, types, conditions, drawn: asked.append(conditions)
    dock._watch()
    assert asked and asked[0].keyword == "臨時"
    # Another settings file without keyword fields: the box goes, and the word is not asked.
    del config["imageCatalogs"][0]["fields"]["keywords"]
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.context.reload()
    assert not dock.keyword.isVisibleTo(dock) and dock.conditions().keyword == ""


def _in_view(iface, box) -> bool:
    from qgis.core import QgsRectangle

    from skywatch.qgis_io.geometry import to_wgs84

    view = to_wgs84(iface.canvas.mapSettings().destinationCrs()).transformBoundingBox(iface.canvas.extent())
    return view.contains(QgsRectangle(*box))


def test_images_without_their_files_are_recorded_on_from_the_catalog(plugin):
    """
    「ファイルなしで記録」 (tsudo, 2026-11-09: the raw data in another system
    only, the catalog here): the catalog's id, time and footprint into
    観測記録, without typing them; offered when an image cannot be opened.
    """
    from qgis.PyQt.QtWidgets import QPushButton

    from skywatch.core.obs.book import FormValues
    from skywatch.core.util import ms_of

    p, iface, folder, fake, _ = plugin
    dock, obs = p.api.image_catalog, p.api.observation_dock
    searched(dock)
    # A usual catalog: 「開く」 as before.
    assert dock.buttons["open"].isVisibleTo(dock) and not dock.buttons["record"].isVisibleTo(dock)
    # C has no place: the reason, with 「ファイルなしで記録」 beside it.
    c = next(r for r in dock.records if r.id == "C")
    iface.canvas.setExtent(iface.canvas.fullExtent())
    assert dock.open_records([c]) == []
    widget = dock.unopened_message
    assert "C に画像の場所なし" in dock.status.text() and any(w is widget for w in iface.bar.items())
    [button] = [b for b in widget.findChildren(QPushButton) if b.text() == "ファイルなしで記録"]
    button.click()
    image = obs.book.current
    assert (image.id, image.name, image.file, image.time) == ("C", "C", False, ms_of(c.acquired))
    assert image.box == pytest.approx([139.7, 35.6, 139.75, 35.65])
    assert obs.isVisible() and not any(w is widget for w in iface.bar.items())
    assert obs.image_select.currentText().endswith("C（ファイルなし）")
    # The map at its footprint; recorded on the background map, with the catalog's id and time.
    assert _in_view(iface, image.box)
    rec = obs.book.submit(139.72, 35.62, FormValues(object="船 1", kind="船舶"))
    assert (rec.image_id, rec.image_name, rec.image_time) == ("C", "C", ms_of(c.acquired))
    # Several at once: the first one chosen in the list is recorded on; ◀ ▶ moves the map to the next when it is out of view.
    dock.choose([r for r in dock.records if r.id in ("A", "B")])
    entry = dock.record_without_files()
    assert entry.id == "A" and dock.status.text().startswith("2 件を観測記録へ（ファイルなし）")
    assert {i.id for i in obs.book.images if not i.file} == {"A", "B", "C"}
    from qgis.core import QgsRectangle

    from skywatch.qgis_io.geometry import from_wgs84

    def look_far_away():
        crs = iface.canvas.mapSettings().destinationCrs()
        iface.canvas.setExtent(from_wgs84(crs).transformBoundingBox(QgsRectangle(0, 0, 1, 1)))
        assert not _in_view(iface, [139.6, 35.5, 139.8, 35.7])

    look_far_away()
    stepped = obs.step(-1) if obs.book.images.index(obs.book.current) > 0 else obs.step(1)
    assert stepped.id in ("B", "C") and _in_view(iface, stepped.box)
    # In view already: the map stays where it is.
    iface.canvas.setExtent(from_wgs84(iface.canvas.mapSettings().destinationCrs()).transformBoundingBox(QgsRectangle(139, 35, 141, 37)))
    before = iface.canvas.extent()
    obs.step(1)
    assert iface.canvas.extent() == before
    # Without an id: nothing to list.
    assert dock.record_without_files([]) is None and dock.status.text().startswith("画像 ID のある画像を一覧で選択")


def test_a_catalog_without_image_files_records_instead_of_opening(plugin, tmp_path, monkeypatch):
    """`imageFiles: false`: no 「開く」 (nor paths, folders, 場所); 「ファイルなしで記録」 always, a double click and the notices too."""
    p, iface, folder, fake, images = plugin
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["imageCatalogs"][0]["imageFiles"] = False
    del config["imageCatalogs"][0]["fields"]["source"]
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.context.reload()
    assert p.api.problems == []
    dock, obs = p.api.image_catalog, p.api.observation_dock
    searched(dock)
    headings = [dock.model.headerData(i, Qt.Orientation.Horizontal) for i in range(dock.model.columnCount())]
    assert headings == ["画像ID", "撮像日時", "センサー", "角度", "方位角"]
    for key in ("open", "copy", "folders"):
        assert not dock.buttons[key].isVisibleTo(dock), key
    assert dock.buttons["record"].isVisibleTo(dock)
    a = next(r for r in dock.records if r.id == "A")
    assert dock.buttons["record"].isEnabled() is False
    dock.choose([a])
    assert dock.buttons["record"].isEnabled()
    # A double click (and 「開く」 asked by a script) records without the file; no layer is added.
    dock._activated(a)
    assert obs.book.current.id == "A" and not obs.book.current.file
    assert dock.open_records([next(r for r in dock.records if r.id == "B")]) == [] and obs.book.current.id == "B"
    from qgis.core import QgsRasterLayer

    assert not any(isinstance(layer, QgsRasterLayer) for layer in QgsProject.instance().mapLayers().values())
    # A new image noticed: 「開く」 in 通知 records it without its file too.
    notices = p.api.notices
    watch = notices.add_watch(p.api.config.image_catalogs[0], TYPES, EMPTY_SEARCH, [], "新着")
    assert wait_for(lambda: notices.store().watch(watch.id).last_checked is not None)
    now = time.time() * 1000
    images.add({"IMG_ID": "D", "ACQ": now - DAY, "SENSOR": "S1"}, ring(139, 35, 140, 36))
    notices.check(force=True)
    assert wait_for(lambda: notices.notices.topLevelItemCount() == 1)
    [notice] = notices.store().notices()
    assert notices.open_notices([notice]) == []
    assert obs.book.current.id == "D" and obs.book.current.box == pytest.approx([139, 35, 140, 36])
    # Back to a catalog with files: 「開く」 again.
    config["imageCatalogs"][0]["imageFiles"] = True
    config["imageCatalogs"][0]["fields"]["source"] = "PATH"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    p.context.reload()
    assert dock.buttons["open"].isVisibleTo(dock) and not dock.buttons["record"].isVisibleTo(dock)
