"""
The observations dock in QGIS (the viewer's observations.spec.ts): images
opened as raster layers in time order, records made by a click and by two
points, carried over, marked gone, dragged, deleted and brought back,
checked areas, CSV, layers and the report — on the local GeoPackage and on a
fake Esri store (answered in tasks).
"""
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer, QgsSettings, QgsVectorLayerTemporalProperties  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.core.obs.flows import living  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.obs_images import IMAGE_ID, image_layer, observed_image  # noqa: E402
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.ui.obs_report_dialog import ReportDialog  # noqa: E402

URL = "https://example.com/arcgis/rest/services/Obs/FeatureServer/0"
FIELDS = ["OBS_ID", "IMAGE_ID", "IMAGE_NAME", "IMAGE_TIME", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE",
          "LENGTH_M", "HEADING", "CONFIDENCE", "OBSERVER", "UPDATED_BY", "CREATED_AT", "UPDATED_AT", "DELETED_AT"]
TYPES = {"IMAGE_TIME": "esriFieldTypeDate", "CREATED_AT": "esriFieldTypeDate", "UPDATED_AT": "esriFieldTypeDate",
         "DELETED_AT": "esriFieldTypeDate", "PRESENT": "esriFieldTypeSmallInteger", "LENGTH_M": "esriFieldTypeDouble", "HEADING": "esriFieldTypeDouble"}


def ms(day: int, hour: int = 1) -> int:
    return int(datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def geotiff(path, when: str = "", name_only: bool = False):
    """A small GeoTIFF over Tokyo Bay (139.6–139.8, 35.5–35.7), its acquisition time in the GDAL metadata."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 20, 20, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([139.6, 0.01, 0, 35.7, 0, -0.01])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    if when:
        ds.SetMetadataItem("ACQUISITION_START_TIME", when)
    ds.GetRasterBand(1).Fill(100)
    ds = None
    return str(path)


@pytest.fixture
def plugin(tmp_path):
    QgsSettings().remove("satellite_observation")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    project = QgsProject.instance()
    layers = []
    for day in (1, 2, 3):
        layer = QgsRasterLayer(geotiff(tmp_path / f"d{day}.tif", f"2026-10-0{day}T01:00:00Z"), f"d{day}")
        assert layer.isValid()
        layers.append(layer)
    project.addMapLayers(layers)
    iface.canvas.setDestinationCrs(layers[0].crs())
    iface.canvas.setLayers(layers)
    iface.canvas.setExtent(layers[0].extent())
    dock = p.api.observation_dock
    dock.show()
    yield p, iface, dock, layers, tmp_path
    p.unload()
    project.removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def test_reads_the_images_in_time_order_and_follows_the_chosen_layer(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    assert book.label == "ローカル" and not dock.store_select.isVisible()
    assert [(i.name, i.origin) for i in book.images] == [("d1.tif", "metadata"), ("d2.tif", "metadata"), ("d3.tif", "metadata")]
    assert book.images[0].time == ms(1) and book.images[0].box == pytest.approx([139.6, 35.5, 139.8, 35.7])
    assert dock.image_select.count() == 3
    dock.step(1)
    assert book.current.name == "d2.tif" and iface.activeLayer() is layers[1]
    iface.setActiveLayer(layers[2])
    assert book.current.name == "d3.tif"
    # 「この画像のみ」: the others are hidden, then shown again.
    root = QgsProject.instance().layerTreeRoot()
    dock.only.setChecked(True)
    assert [root.findLayer(x.id()).itemVisibilityChecked() for x in layers] == [False, False, True]
    dock.step(-1)
    assert [root.findLayer(x.id()).itemVisibilityChecked() for x in layers] == [False, True, False]
    dock.only.setChecked(False)
    assert all(root.findLayer(x.id()).itemVisibilityChecked() for x in layers)


def test_records_by_a_click_carries_over_marks_gone_drags_deletes_and_restores(plugin):
    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    assert book.current.name == "d1.tif"
    dock.set_active(True)
    assert dock.canvas.mapTool() is dock.tool and dock.status.text() == "地図クリックで記録（白抜きの印は継続）"
    dock.map_clicked(139.7, 35.6, None)
    assert dock.form.isVisible() and dock.submit_button.text() == "記録（Enter）"
    dock.object_input.setText("あおば")
    dock.kind_input.setText("船舶 > 護衛艦")
    dock._pick_activity("停泊")
    dock.confidence_select.setCurrentIndex(dock.confidence_select.findData("推定"))
    dock.submit()
    [a] = book.living()
    assert (a.object_name, a.kind, a.activity, a.confidence, a.recorded_by, a.image_name) == ("あおば", "船舶 > 護衛艦", "停泊", "推定", "田中", "d1.tif")
    assert not dock.form.isVisible() and dock.status.text() == "あおば（停泊）を記録"
    marks = dock.scene().marks
    assert [(m.role, m.label, m.ring) for m in marks] == [("obs", "あおば・停泊（推定）", "#d97706")]
    # The next image: carried over (a hollow mark), then still there.
    dock.step(1)
    assert dock.carry_label.text() == "前後の画像のモノ 1 件" and dock.carry_all_button.isVisible()
    assert [m.role for m in dock.scene().marks] == ["ghost"]
    ghost = dock.scene().marks[0]
    dock.map_clicked(ghost.lon, ghost.lat, ghost)
    assert dock.submit_button.text() == "継続（Enter）" and dock.object_input.text() == "あおば" and dock.absent_button.isVisible()
    dock._pick_activity("航行")
    dock.submit()
    assert [o.activity for o in book.flow(a.object_id).observations] == ["停泊", "航行"]
    # The last image: gone.
    dock.step(1)
    dock.carry(a.object_id, False)
    assert book.flow(a.object_id).present is False
    assert [m.label for m in dock.scene().marks] == ["あおば（不在）（推定）"]
    # Dragged: its place corrected.
    [here] = book.here()
    mark = dock.scene().marks[0]
    dock.map_dragged(mark, 139.71, 35.61, False)
    assert dock.scene().marks[0].lon == 139.71 and book.record(here.id).lon == here.lon
    dock.map_dragged(mark, 139.71, 35.61, True)
    assert book.record(here.id).lon == 139.71 and dock.status.text() == "あおば の位置を修正"
    # Deleted, then brought back from 「削除済みも表示」.
    dock.map_clicked(139.71, 35.61, dock.scene().marks[0])
    assert dock.delete_button.isVisible() and dock.submit_button.text() == "更新（Enter）"
    dock._delete_pending()
    assert book.here() == [] and dock.status.text() == "1 件の記録を削除"
    dock.deleted_check.setChecked(True)
    assert dock.table.item(0, 0).font().strikeOut()
    dock.restore(book.here(deleted=True))
    assert len(book.here()) == 1
    # Kept in the GeoPackage: a new book reads it back.
    dock.reset_stores()
    assert len(dock.book.living()) == 3 and dock.book is not book


def test_the_kind_chosen_is_the_next_new_records_default_in_later_sessions_too(plugin):
    p, iface, dock, layers, tmp_path = plugin
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    assert dock.kind_input.text() == "船舶"
    dock.kind_input.setText("船舶 > 護衛艦")
    dock.submit()
    dock.map_clicked(139.71, 35.61, None)
    assert dock.kind_input.text() == "船舶 > 護衛艦"
    # Chosen in the form (by its class buttons or typed), then left without recording: still the next one's.
    dock.kind_input.setText("航空機")
    dock._kind_changed()
    dock.escape()
    dock.map_clicked(139.72, 35.62, None)
    assert dock.kind_input.text() == "航空機"
    dock.close_form()
    # The stores read again (the settings file reloaded, or QGIS started again): kept.
    dock.reset_stores()
    dock.set_active(True)
    dock.map_clicked(139.72, 35.62, None)
    assert dock.kind_input.text() == "航空機"
    # A record changed or carried over shows its own kind and does not change the default by being opened.
    dock.kind_input.setText("船舶 > 護衛艦")
    dock._kind_changed()
    dock.submit()
    [o] = [o for o in dock.book.living() if o.kind == "船舶 > 護衛艦" and o.lon == 139.72]
    dock.kind_input.setText("")
    dock.map_clicked(o.lon, o.lat, next(m for m in dock.scene().marks if m.key == o.id))
    assert dock.pending[2] is not None
    dock.kind_input.setText("車両")
    dock._kind_changed()
    dock.escape()
    dock.map_clicked(139.65, 35.65, None)
    assert dock.kind_input.text() == "船舶 > 護衛艦"


def test_measures_by_two_points_and_offers_the_same_thing(plugin):
    p, iface, dock, layers, _ = plugin
    dock.set_two_point(True)
    assert dock.active and dock.status.text().startswith("前端")
    dock.map_clicked(139.7, 35.6, None)
    assert dock.front == (139.7, 35.6) and dock.status.text() == "後端をクリック（Esc で取消）"
    dock.map_hovered(139.7, 35.599)
    assert dock.scene().rubber_label == "111 m・000°"
    dock.map_clicked(139.7, 35.5985, None)
    assert dock.length_input.isVisible() and float(dock.length_input.text()) == pytest.approx(166.8, abs=0.2)
    assert dock.heading_input.text() == "0.0"
    dock.object_input.setText("タンカーA")
    dock.kind_input.setText("船舶")
    dock.submit()
    [a] = dock.book.living()
    assert a.length == pytest.approx(166.8, abs=0.2) and a.heading == 0
    [m] = dock.scene().marks
    assert m.ends is not None
    # Painted over the canvas: the mark where the record is, found again by a click there.
    from qgis.PyQt.QtGui import QImage, QPainter

    image = QImage(800, 600, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    dock.overlay.paint(painter)
    painter.end()
    at = dock.overlay.to_pixel(a.lon, a.lat)
    assert dock.overlay.hit(at.x() + 2, at.y() - 2).key == a.id
    # The next image: the same thing offered near, picked with Alt+1.
    dock.step(1)
    dock.set_two_point(False)
    dock.map_clicked(139.7001, 35.5993, None)
    assert [c.name for c in dock.offered] == ["タンカーA"] and dock.candidates_box.isVisible()
    dock._candidate_key(1)
    assert dock.object_input.text() == "タンカーA" and dock.kind_input.text() == "船舶"
    dock.submit()
    assert len(dock.book.flow(a.object_id).observations) == 2
    # Esc steps back: the front, then the form.
    dock.set_two_point(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.escape()
    assert dock.front is None and dock.status.text() == "2 点目の取消"


def test_checked_areas_carry_all_and_absent_inside(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    dock.set_active(True)
    for name, lon, lat in (("A", 139.70, 35.60), ("B", 139.75, 35.65)):
        dock.map_clicked(lon, lat, None)
        dock.object_input.setText(name)
        dock.kind_input.setText("船舶")
        dock.submit()
    dock.step(1)
    dock.set_area_mode(True)
    assert dock.area_form.isVisible() and dock.status.text().startswith("確認範囲:")
    dock.map_clicked(139.69, 35.59, None)
    dock.map_hovered(139.72, 35.62)
    assert dock.scene().area_pending is not None
    dock.map_clicked(139.72, 35.62, None)
    assert dock.area_range.text().startswith("範囲: ")
    dock.area_kind.setText("船舶")
    dock.area_note.setText("港内")
    dock._submit_area()
    assert not dock.area_mode and "確認範囲（船舶）を記録（範囲内で記録のない前後の画像のモノ 1 件）" == dock.status.text()
    assert dock.absent_inside_button.text() == "確認範囲内 1 件を不在"
    dock.carry_all()
    assert dock.status.text() == "1 のモノを継続として記録（確認範囲内の 1 件を除く）"
    dock.absent_inside()
    assert sorted((o.object_name, o.present) for o in book.here()) == [("A", False), ("B", True)]
    assert len(dock.scene().areas) == 1
    # The image's whole footprint, and the view.
    dock.set_area_mode(True)
    dock._area_image()
    assert dock.area_ring[0] == pytest.approx([139.6, 35.5])
    dock.escape()
    assert not dock.area_mode


def test_lists_an_image_without_its_file_and_types_a_time(plugin):
    p, iface, dock, layers, _ = plugin
    dock.list_button.setChecked(True)
    dock.list_id.setText("EXT-1")
    dock.list_name.setText("外部")
    dock._list_from_form()
    book = dock.book
    assert book.current.id == "EXT-1" and not book.current.file and dock.time_row.isVisible()
    assert dock.status.text() == "外部 を追加（ファイルなし）"
    from qgis.PyQt.QtCore import QDateTime

    dock.time_input.setDateTime(QDateTime.fromMSecsSinceEpoch(ms(2, 12)))
    dock._set_image_time()
    assert [i.id for i in book.images] == ["d1.tif", "d2.tif", "EXT-1", "d3.tif"]
    assert not dock.time_row.isVisible()
    assert "EXT-1" in settings.SettingsPrefs().get("listed_images/ローカル")


def test_saves_csv_adds_layers_and_makes_a_report(plugin):
    p, iface, dock, layers, tmp_path = plugin
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("A")
    dock.kind_input.setText("船舶 > 漁船")
    dock.submit()
    dock.step(1)
    dock.carry(dock.book.living()[0].object_id, True)
    path = dock.save_csv(str(tmp_path / "o.csv"))
    text = open(path, encoding="utf-8").read()
    assert text.startswith("﻿") and text.count("\n") == 3
    made = dock.to_layers()
    assert [x.name() for x in made] == ["観測記録", "観測の流れ"]
    points, lines = made
    assert points.featureCount() == 2 and "kind_2" in points.fields().names()
    assert points.temporalProperties().isActive() and points.temporalProperties().startField() == "image_time"
    assert lines.temporalProperties().mode() == QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeStartAndEndFromFields
    # The report, its map drawn off the main thread.
    dialog = ReportDialog(dock, dock.book, dock.canvas, dock)
    assert dialog.count_label.text() == "モノ 1・記録 2・画像 2・確認範囲 0"
    got = []
    dialog.make(lambda html, o, image: got.append((html, image)))
    assert wait_for(lambda: got, 10000)
    html, image = got[0]
    assert "<h2>位置</h2>" in html and 'src="data:image/jpeg;base64,' in html and image is not None
    pdf = tmp_path / "r.pdf"
    dialog._pdf(html, dialog.options(), image, str(pdf))
    assert pdf.stat().st_size > 1000


def test_saves_and_reads_an_esri_store_in_tasks(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    # The store chosen before stays chosen; the Esri stores come first.
    assert dock.store_select.currentText() == "ローカル"
    dock.store_select.setCurrentIndex(0)
    assert dock.store_select.count() == 2 and dock.store_select.currentText() == "共有" and dock.store_set.remote
    assert wait_for(lambda: dock.book.loaded)
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("共有のモノ")
    dock.submit()
    assert wait_for(lambda: len(layer.rows) == 1)
    assert wait_for(lambda: dock.status.text() == "共有のモノを記録")
    assert layer.rows[0]["attributes"]["OBSERVER"] == "田中"
    # The local store is the other choice, with its own records.
    dock.store_select.setCurrentIndex(1)
    assert dock.book.label == "ローカル" and dock.book.living() == []
    dock.store_select.setCurrentIndex(0)
    assert [o.object_name for o in living(dock.book.records)] == ["共有のモノ"]


def test_opens_a_catalog_image_signed_in_by_an_authcfg(tmp_path):
    path = geotiff(tmp_path / "c.tif")
    layer = image_layer(path, "カタログ画像", image_id="CAT-9", time=ms(5), authcfg="abc1234")
    assert "authcfg='abc1234'" in layer.source() and layer.customProperty(IMAGE_ID) == "CAT-9"
    assert "token" not in layer.source()
    info = observed_image(layer)
    assert (info.id, info.name, info.time, info.origin) == ("CAT-9", "c.tif", ms(5), "layer")
    url = image_layer("https://example.com/c.tif", "遠隔", authcfg="abc1234")
    assert url.source().startswith("/vsicurl/https://example.com/c.tif")


def test_edits_the_classification_as_a_tree(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    editor = p.docks["SatObsTaxonomy"]
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("A")
    dock.kind_input.setText("船舶 > 支援船")
    dock.submit()
    dock.edit_taxonomy(["船舶", "支援船"])
    assert editor.isVisible() and editor.selected == "船舶 > 支援船"
    assert editor.tree.currentItem().text(0) == "支援船" and editor.form.isVisible()
    assert editor.form.title() == "船舶 > 支援船（種別）" and editor.levels_note.text() == "階層: 大分類 › 種別 › 級 › 船名"
    # Renamed where it stands: the record follows.
    editor.tree.currentItem().setText(0, "補助艦")
    assert dock.book.living()[0].kind == "船舶 > 補助艦" and editor.selected == "船舶 > 補助艦"
    # A class added below (＋ / Insert), by typing its name in a new row.
    editor.start_add("船舶 > 補助艦")
    adding = editor.tree.currentItem()
    adding.setText(0, "〇〇級")
    assert editor.selected == "船舶 > 補助艦 > 〇〇級" and editor._item_of("船舶 > 補助艦 > 〇〇級") is not None
    # Dragged onto another class: moved below it, the records too.
    editor._dropped("船舶 > 補助艦", "航空機", "inside")
    assert dock.book.living()[0].kind == "航空機 > 補助艦"
    # Reordered among its siblings with Alt+↑.
    editor.select(["車両"])
    editor.reorder(-1)
    tops = [editor.tree.topLevelItem(i).text(0) for i in range(editor.tree.topLevelItemCount())]
    assert tops.index("車両") < tops.index("航空機")
    # Specifications: what a class above says shows until this class says otherwise.
    editor.select(["航空機"])
    editor._spec_inputs["全長"].setText("40")
    editor.apply()
    editor.select(["航空機", "補助艦"])
    assert editor._spec_inputs["全長"].placeholderText() == "40 m（航空機）"
    editor.members_input.setPlainText("一号\n二号")
    editor.apply()
    assert dock.book.taxonomy()[[t.path for t in dock.book.taxonomy()].index(["航空機", "補助艦"])].members == ["一号", "二号"]
    # A photo from a file, kept in the local GeoPackage.
    from qgis.PyQt.QtGui import QColor, QImage

    png = tmp_path / "p.png"
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    image.save(str(png))
    editor.add_photo_files([str(png)])
    [ref] = [t for t in dock.book.taxonomy() if t.path == ["航空機", "補助艦"]][0].photos
    assert ref.startswith("local:") and editor._pixmap(ref) is not None and editor.photo_layout.count() == 1
    # Search: the matching classes and those above them.
    editor.query.setText("二号")
    assert editor.tree.topLevelItemCount() == 1 and editor.tree.topLevelItem(0).child(0).text(0) == "補助艦"
    editor.query.clear()
    # Deleted in two presses: the records keep the class above.
    editor.select(["航空機", "補助艦"])
    editor._delete_key()
    assert editor._item_of("航空機 > 補助艦") is not None and "もう一度で" in editor.status.text()
    editor._delete_key()
    assert editor._item_of("航空機 > 補助艦") is None and dock.book.living()[0].kind == "航空機"
    # 「諸元の項目」: one added and saved.
    editor.fields_box.setChecked(True)
    editor._add_field()
    editor.save_fields()
    assert dock.book.spec_fields[-1].name == "項目12" and dock.status.text() == "諸元の項目 12 件を設定"


def test_gives_scripts_the_observations_through_plugin_api(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    api = p.api
    a = api.add_observation(139.7, 35.6, object="A", kind="船舶")
    assert a["imageName"] == "d1.tif" and [m.label for m in dock.scene().marks] == ["A"]
    assert api.observe_image("d2.tif") and dock.book.current.name == "d2.tif"
    area = api.add_checked_area(bbox=[139.69, 35.59, 139.72, 35.62])
    assert area["imageId"] == "d2.tif"
    out = tmp_path / "r.html"
    report = api.observation_report(title="週報", save=str(out))
    assert report["things"] == 1 and 'src="data:image/jpeg;base64,' in out.read_text(encoding="utf-8")
    assert api.observation_report.__doc__.startswith("The report")


def test_gives_scripts_an_esri_store_waiting_for_its_tasks(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    dock.store_select.setCurrentIndex(0)
    a = p.api.add_observation(139.7, 35.6, object="共有A")
    assert len(layer.rows) == 1 and a["objectName"] == "共有A"
    assert p.api.delete_observations(a["id"]) == 1 and p.api.observations() == []
