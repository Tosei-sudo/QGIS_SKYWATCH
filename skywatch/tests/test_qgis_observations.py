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
from qgis.PyQt.QtCore import QItemSelectionModel, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QPushButton, QToolButton  # noqa: E402

from skywatch.core.esri import EsriClient  # noqa: E402
from skywatch.core.obs.flows import living  # noqa: E402
from skywatch.core.obs.taxonomy import Taxon  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.qgis_io.obs_images import IMAGE_ID, image_layer, observed_image  # noqa: E402
from skywatch.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from skywatch.ui.obs_report_dialog import ReportDialog  # noqa: E402

URL = "https://example.com/arcgis/rest/services/Obs/FeatureServer/0"
URL2 = "https://example.com/arcgis/rest/services/Obs2/FeatureServer/0"
FIELDS = ["OBS_ID", "IMAGE_ID", "IMAGE_NAME", "IMAGE_TIME", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE",
          "LENGTH_M", "HEADING", "CONFIDENCE", "OBSERVER", "UPDATED_BY", "CREATED_AT", "UPDATED_AT", "DELETED_AT"]
TYPES = {"IMAGE_TIME": "esriFieldTypeDate", "CREATED_AT": "esriFieldTypeDate", "UPDATED_AT": "esriFieldTypeDate",
         "DELETED_AT": "esriFieldTypeDate", "PRESENT": "esriFieldTypeSmallInteger", "LENGTH_M": "esriFieldTypeDouble", "HEADING": "esriFieldTypeDouble"}


def ms(day: int, hour: int = 1) -> int:
    return int(datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def geotiff(path, when: str = "", name_only: bool = False, west: float = 139.6):
    """A small GeoTIFF over Tokyo Bay (139.6–139.8, 35.5–35.7; `west` its west edge), its acquisition time in the GDAL metadata."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 20, 20, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([west, 0.01, 0, 35.7, 0, -0.01])
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
    QgsSettings().remove("skywatch")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    from skywatch import classFactory

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
    QgsSettings().remove("skywatch")


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


def test_a_records_images_in_its_folder_are_one_image_to_record_on(plugin):
    """tsudo 2026-11-09: a catalog record opened from its folder (several files) is one image in the list, recorded on as the record."""
    from skywatch.qgis_io.obs_images import IMAGE_FILE

    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    (tmp_path / "SCENE_R" / "PAN").mkdir(parents=True)
    group = root.insertGroup(0, "R")
    tiles = []
    for i, rel in enumerate(("PAN/R1C1.tif", "PAN/R1C2.tif", "PAN/R1C3.tif")):
        layer = image_layer(geotiff(tmp_path / "SCENE_R" / rel, west=139.6 + 0.2 * i), rel, image_id="R", time=ms(4))
        layer.setCustomProperty(IMAGE_FILE, rel)
        project.addMapLayer(layer, False)
        group.addLayer(layer)
        tiles.append(layer)
    dock.refresh_images()
    # One image of the record, named as its folder, its footprint around the files.
    assert [i.name for i in book.images] == ["d1.tif", "d2.tif", "d3.tif", "SCENE_R"]
    scene = book.images[3]
    assert scene.id == "R" and scene.layer_ids == tuple(x.id() for x in tiles) and scene.time == ms(4)
    assert scene.box == pytest.approx([139.6, 35.5, 140.2, 35.7])
    assert dock.image_select.count() == 4 and dock.image_select.itemText(3).endswith("SCENE_R（3 ファイル）")
    iface.setActiveLayer(tiles[1])
    assert book.current.id == "R"
    dock.set_active(True)
    dock.map_clicked(139.95, 35.6, None)
    assert dock.form.isVisible()
    # Another of its files chosen in the layers panel: the same image, the form stays open.
    iface.setActiveLayer(tiles[2])
    assert dock.form.isVisible() and book.current.id == "R"
    dock.object_input.setText("あおば")
    dock.kind_input.setText("船舶 > 護衛艦")
    dock.submit()
    [a] = book.living()
    assert (a.image_id, a.image_name, a.image_time) == ("R", "SCENE_R", ms(4))
    # 「この画像のみ」: all its files show; ◀ ▶ go image by image (to its first file).
    dock.only.setChecked(True)
    assert [root.findLayer(x.id()).itemVisibilityChecked() for x in layers + tiles] == [False] * 3 + [True] * 3
    dock.step(-1)
    assert book.current.name == "d3.tif" and iface.activeLayer() is layers[2]
    assert [root.findLayer(x.id()).itemVisibilityChecked() for x in layers + tiles] == [False, False, True] + [False] * 3
    dock.step(1)
    assert book.current.id == "R" and iface.activeLayer() is tiles[0]
    dock.only.setChecked(False)
    # One of its files closed: still the image recorded on, with its records.
    project.removeMapLayer(tiles[2].id())
    assert book.current.id == "R" and book.current.layer_ids == (tiles[0].id(), tiles[1].id())
    assert [o.id for o in book.here()] == [a.id]


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


def test_clears_the_local_records_after_asking(plugin, monkeypatch):
    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    dock.set_active(True)
    for name in ("あおば", "しらせ"):
        dock.map_clicked(139.7, 35.6, None)
        dock.object_input.setText(name)
        dock.kind_input.setText("船舶")
        dock.submit()
    dock.remove([o for o in book.living() if o.object_name == "しらせ"])
    dock.more.setChecked(True)
    assert dock.clear_button.isVisible() and dock.clear_button.isEnabled()
    answers = [None, True, False]
    asked = []
    monkeypatch.setattr(dock, "ask_clear", lambda b: asked.append(len(b.records)) or answers.pop(0))
    # Cancelled: nothing goes.
    assert dock.clear_local() is None and len(book.records) == 2
    # Only the deleted one.
    assert dock.clear_local() == (1, 0)
    assert [o.object_name for o in book.records] == ["あおば"] and dock.status.text() == "ローカル の削除済みの記録 1 件・確認範囲 0 件を消去"
    # Everything, for good: a new book reads nothing back.
    assert dock.clear_local() == (1, 0)
    assert asked == [2, 2, 1] and book.records == [] and dock.scene().marks == []
    dock.reset_stores()
    assert dock.book.records == []


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


def test_the_kind_is_chosen_level_by_level_with_every_top_class_shown(plugin):
    p, iface, dock, layers, tmp_path = plugin
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.choose_kind(["船舶", "護衛艦"])
    # 大分類 stays on show with the chosen one pressed, so another one is a click away.
    rows = dock.kind_level_rows()
    assert rows[0][0] == "大分類" and rows[0][2] == "船舶" and {"船舶", "航空機", "車両"} <= set(rows[0][1])
    assert rows[1][2] == "護衛艦"
    top = dock.kind_levels.findChildren(QToolButton)
    [plane] = [b for b in top if b.text() == "航空機"]
    plane.click()
    assert dock.kind_input.text() == "航空機"
    # The pressed one again clears that level and the ones below.
    [ship] = [b for b in dock.kind_levels.findChildren(QToolButton) if b.text() == "航空機"]
    assert ship.isChecked()
    ship.click()
    assert dock.kind_input.text() == "" and dock.kind_level_rows()[0][2] == ""
    # A kind typed outside the classes still shows on its level.
    dock.kind_input.setText("船舶 > 未知の艦")
    dock._kind_changed()
    assert dock.kind_level_rows()[1][2] == "未知の艦" and "未知の艦" in dock.kind_level_rows()[1][1]
    dock.close_form()


def test_the_activities_switch_with_the_top_class(plugin):
    p, iface, dock, layers, tmp_path = plugin
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)

    def chips():
        layout = dock.chips_layout
        return [layout.itemAt(i).widget().text().split(" ", 1)[1] for i in range(layout.count())]

    dock.choose_kind(["船舶"])
    assert "航行" in chips() and "駐機" not in chips()
    dock._pick_activity("航行")
    dock.choose_kind(["航空機"])
    assert "駐機" in chips() and "航行" not in chips()
    # Not one of the aircraft's: left out.
    assert dock.activity_input.text() == ""
    dock._chip(1)
    assert dock.activity_input.text() == "駐機"
    # A class's own activities, changed in the 分類 tab's way.
    assert dock.book.update_class(["航空機"], activities=["給油", "駐機"])
    dock._kind_changed()
    assert chips()[:2] == ["給油", "駐機"] and dock.activity_input.text() == "駐機"
    dock.close_form()


def test_a_length_typed_full_width_or_with_a_unit_is_kept_and_an_unreadable_one_keeps_the_form(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    dock.set_two_point(True)
    dock.map_clicked(139.7, 35.5985, None)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("タンカーB")
    dock.kind_input.setText("船舶")
    # Typed with the IME on (full-width digits and point) and with a unit: kept, not quietly dropped.
    dock.length_input.setText("１２０．５")
    dock.heading_input.setText("９０°")
    dock.submit()
    [a] = dock.book.living()
    assert a.length == 120.5 and a.heading == 90
    # Changed to something that is not a number: the form stays open and says which, and the record keeps its length.
    dock.set_two_point(False)
    dock.open_form(a.lon, a.lat, editing=a)
    dock.length_input.setText("120m くらい")
    dock.submit()
    assert dock.form.isVisible() and dock.pending is not None
    assert dock.measure_note.text() == "長さ（m）の「120m くらい」を読めません（数で）"
    assert dock.book.living()[0].length == 120.5
    dock.length_input.setText("130 m")
    dock.submit()
    assert not dock.form.isVisible() and dock.book.living()[0].length == 130
    # A file that can't be written (open in Excel, a folder gone): said in the dock, not a Python error.
    assert dock.save_csv(str(tmp_path / "gone" / "records.csv")) is None
    assert dock.status.text().startswith("CSV を保存できない: ")
    assert dock.save_csv(str(tmp_path / "records.csv")) == str(tmp_path / "records.csv")


def test_measures_by_two_points_and_offers_the_same_thing(plugin):
    p, iface, dock, layers, _ = plugin
    dock.set_two_point(True)
    assert dock.active and dock.status.text().startswith("後端 →")
    # The rear first, then the front.
    dock.map_clicked(139.7, 35.5985, None)
    assert dock.rear == (139.7, 35.5985) and dock.status.text() == "前端をクリック（Esc で取消）"
    dock.map_hovered(139.7, 35.5995)
    assert dock.scene().rubber_label == "111 m・000°"
    dock.map_clicked(139.7, 35.6, None)
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
    # Esc steps back: the first point, then the form.
    dock.set_two_point(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.escape()
    assert dock.rear is None and dock.status.text() == "後端の取消"
    # Esc with the map focused (the dock's key is not active then) does the same.
    from qgis.PyQt.QtCore import QEvent, Qt
    from qgis.PyQt.QtGui import QKeyEvent

    def esc_on_map():
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        dock.canvas.keyPressEvent(event)
        return event.isAccepted()

    assert dock.canvas.mapTool() is dock.tool
    dock.map_clicked(139.7, 35.6, None)
    assert dock.rear is not None
    assert esc_on_map() and dock.rear is None and dock.status.text() == "後端の取消"
    dock.set_area_mode(True)
    dock.map_clicked(139.7, 35.6, None)
    assert dock.area_corner is not None
    esc_on_map()
    assert dock.area_corner is None and dock.area_mode
    esc_on_map()
    assert not dock.area_mode
    # A drag under way: left where it was.
    dock.dragging = ("x", 1.0, 2.0)
    esc_on_map()
    assert dock.dragging is None
    # Other keys go on to QGIS.
    other = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_X, Qt.KeyboardModifier.NoModifier)
    dock.tool.keyPressEvent(other)
    assert not other.isAccepted()
    # Enter with the map focused saves the open form (both Enter keys), else goes on to QGIS.
    def enter_on_map(key=Qt.Key.Key_Return):
        event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        dock.canvas.keyPressEvent(event)
        return event.isAccepted()

    dock.close_form()
    assert not enter_on_map()
    before = len(dock.book.here())
    dock.set_two_point(False)
    dock.map_clicked(139.71, 35.61, None)
    assert dock.form.isVisible()
    dock.object_input.setText("タグボートB")
    dock.canvas.setFocus()
    assert enter_on_map(Qt.Key.Key_Enter) and not dock.form.isVisible()
    assert len(dock.book.here()) == before + 1 and dock.book.flow_by_name("タグボートB") is not None
    # The area too.
    dock.set_area_mode(True)
    dock._area_view()
    assert enter_on_map()
    assert not dock.area_mode


def test_measures_the_width_by_three_or_four_points(plugin):
    p, iface, dock, layers, _ = plugin
    dock.set_points(3)
    assert dock.active and dock.points_select.currentData() == 3 and dock.status.text().startswith("後端 → 前端 → 側面")
    dock.map_clicked(139.7, 35.5985, None)
    dock.map_clicked(139.7, 35.6, None)
    assert dock.placed == [(139.7, 35.6)] and not dock.form.isVisible()
    assert dock.status.text() == "側面（舷側）をクリック（Esc で取消）"
    # The outline follows the pointer.
    dock.map_hovered(139.7002, 35.5992)
    scene = dock.scene()
    assert scene.rubber_outline is not None and len(scene.rubber_outline) == 4 and " × " in scene.rubber_label
    dock.map_clicked(139.7002, 35.5992, None)
    assert dock.width_input.isVisible() and float(dock.width_input.text()) == pytest.approx(36.2, abs=0.3)
    assert float(dock.length_input.text()) == pytest.approx(166.8, abs=0.2) and dock.heading_input.text() == "0.0"
    dock.object_input.setText("タンカーB")
    dock.kind_input.setText("船舶")
    dock.submit()
    [a] = dock.book.living()
    assert a.width == pytest.approx(36.2, abs=0.3) and a.lon == pytest.approx(139.7, abs=1e-9)
    [m] = dock.scene().marks
    assert m.outline is not None and len(m.outline) == 4
    # Four points: both sides, the centre halfway between them.
    dock.set_points(4)
    for lon, lat in ((139.71, 35.5985), (139.71, 35.6), (139.7102, 35.5992)):
        dock.map_clicked(lon, lat, None)
    assert dock.status.text() == "反対側の側面をクリック（Esc で取消）" and len(dock.placed) == 2
    # Esc takes back the last point only.
    dock.escape()
    assert dock.placed == [(139.71, 35.6)] and dock.status.text() == "側面の取消"
    dock.map_clicked(139.7102, 35.5992, None)
    dock.map_clicked(139.7099, 35.5992, None)
    assert float(dock.width_input.text()) == pytest.approx(27.15, abs=0.3)
    dock.object_input.setText("タンカーC")
    dock.submit()
    c = next(o for o in dock.book.living() if o.object_name == "タンカーC")
    assert c.width == pytest.approx(27.15, abs=0.3) and c.lon > 139.71
    # 「L」 goes round 1 → 2 → 3 → 4 → 1.
    dock.set_points(4)
    dock.set_points(dock.points % 4 + 1)
    assert dock.points == 1 and dock.points_select.currentData() == 1


def _painted(scene, style):
    """The scene painted (pixels = lon / lat as given) in a style, on a transparent image."""
    from qgis.PyQt.QtCore import QPointF, Qt
    from qgis.PyQt.QtGui import QImage, QPainter

    from skywatch.ui.obs_overlay import ScenePainter

    image = QImage(200, 200, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    scene_painter = ScenePainter(lambda lon, lat: QPointF(lon, lat), style=style)
    scene_painter.paint(painter, scene)
    painter.end()
    return image, scene_painter


def test_the_marks_painted_as_outlines_leave_what_they_mark_clear():
    """「印」 枠だけ (tsudo, 2026-11-09): nothing filled over the thing; 非表示: only the input being made."""
    from skywatch.ui.obs_overlay import Mark, Scene

    def scene():
        # A record by one click (reported), one with a width (front at the top), a thing carried over and a checked area.
        return Scene(marks=[
            Mark("obs", 50, 50, "#e11d48", "A", ring="#16a34a", key="a", reported=True),
            Mark("obs", 150, 100, "#2563eb", "B", key="b", ends=((150, 60), (150, 140)),
                 outline=[(140, 60), (160, 60), (160, 140), (140, 140)]),
            Mark("ghost", 50, 150, "#16a34a", "C", key="c"),
        ], areas=[([(5, 5), (195, 5), (195, 195), (5, 195), (5, 5)], "")], pending=(100, 20))

    def alpha(image, x, y):
        return image.pixelColor(x, y).alpha()

    normal, _ = _painted(scene(), "normal")
    outline, outline_painter = _painted(scene(), "outline")
    hidden, hidden_painter = _painted(scene(), "hidden")
    # The middle of a mark: filled as before; clear as outlines (the area's fill gone too).
    assert alpha(normal, 50, 50) == 255 and alpha(outline, 50, 50) == 0
    # Its rings (the class's colour inside the 確度's) and the reported ring are still there.
    assert outline.pixelColor(57, 50).red() > 150 and outline.pixelColor(59, 50).green() > 100
    assert alpha(outline, 64, 50) > 0 and alpha(outline, 61, 50) < alpha(normal, 61, 50)
    # A record with a width: its outline is the frame, nothing along its middle line or inside.
    # (Below its middle: its name is above.)
    assert alpha(normal, 150, 120) > 0 and alpha(outline, 150, 120) == 0 and alpha(outline, 145, 120) == 0
    assert alpha(outline, 140, 120) > 0 and alpha(outline, 150, 57) > 0
    # The thing carried over: no white fill.
    assert alpha(normal, 50, 150) > 0 and alpha(outline, 50, 150) == 0
    assert alpha(outline, 10, 100) == 0 and alpha(normal, 10, 100) > 0
    assert {m.key for _p, m in outline_painter.painted} == {"a", "b", "c"}
    # Hidden: no marks (none to click), the area gone, the place clicked still shown.
    assert alpha(hidden, 57, 50) == 0 and alpha(hidden, 140, 80) == 0 and alpha(hidden, 6, 100) == 0
    assert alpha(hidden, 100, 20) > 0 and hidden_painter.painted == []
    # …but the record open in the form stays.
    s = scene()
    s.marks[1].editing = True
    _image, painter = _painted(s, "hidden")
    assert [m.key for _p, m in painter.painted] == ["b"]


def test_the_marks_style_is_chosen_kept_and_toggled_by_a_key(plugin):
    p, iface, dock, layers, _ = plugin
    assert dock.mark_style_select.currentData() == "normal" and dock.overlay.style == "normal"
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.kind_input.setText("船舶")
    dock.submit()
    [a] = dock.book.living()
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QImage, QPainter

    def paint():
        image = QImage(800, 600, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        dock.overlay.paint(painter)
        painter.end()
        at = dock.overlay.to_pixel(a.lon, a.lat)
        return image.pixelColor(int(at.x()), int(at.y())).alpha(), dock.overlay.hit(at.x(), at.y())

    alpha, hit = paint()
    assert alpha == 255 and hit.key == a.id
    dock.mark_style_select.setCurrentIndex(dock.mark_style_select.findData("outline"))
    assert settings.mark_style() == "outline" and dock.overlay.style == "outline"
    alpha, hit = paint()
    assert alpha == 0 and hit.key == a.id
    # 「H」: hidden (nothing to click: a click records a new thing), then back to the outlines.
    dock.toggle_marks()
    assert dock.mark_style_select.currentData() == "hidden" and settings.mark_style() == "hidden"
    alpha, hit = paint()
    assert alpha == 0 and hit is None
    dock.toggle_marks()
    assert dock.mark_style_select.currentData() == "outline"
    # Ctrl+Alt+D anywhere does the same.
    p.shortcuts.actions["ToggleMarks"].trigger()
    assert dock.mark_style_select.currentData() == "hidden"
    p.shortcuts.actions["ToggleMarks"].trigger()
    assert dock.mark_style_select.currentData() == "outline"
    # Kept for the next session.
    dock.set_mark_style("hidden")
    from skywatch.ui.observation_dock import ObservationDock

    again = ObservationDock(iface, dock.context, lambda: None)
    try:
        assert again.mark_style_select.currentData() == "hidden" and again.overlay.style == "hidden"
        again.toggle_marks()
        assert again.mark_style_select.currentData() == "normal"
    finally:
        again.close_down()


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
    # Another settings file chooses its first store; the Esri stores come first.
    assert dock.store_select.count() == 2 and dock.store_select.currentText() == "共有" and dock.store_set.remote
    assert wait_for(lambda: dock.book.loaded)
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("共有のモノ")
    dock.submit()
    assert wait_for(lambda: len(layer.rows) == 1)
    assert wait_for(lambda: dock.status.text() == "共有のモノを記録")
    assert layer.rows[0]["attributes"]["OBSERVER"] == "田中"
    # The classification read again in a task too.
    dock.reload_taxonomy()
    assert wait_for(lambda: dock.status.text().endswith("件を読み込み"))
    # The local store is the other choice, with its own records.
    dock.store_select.setCurrentIndex(1)
    assert dock.book.label == "ローカル" and dock.book.living() == []
    dock.store_select.setCurrentIndex(0)
    assert [o.object_name for o in living(dock.book.records)] == ["共有のモノ"]


def _click_row(table, row: int, modifier=Qt.KeyboardModifier.NoModifier) -> None:
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, modifier, table.visualRect(table.model().index(row, 1)).center())


def _chosen_rows(dock) -> list[int]:
    return sorted({i.row() for i in dock.table.selectionModel().selectedRows()})


def test_a_record_clicked_stays_chosen_after_the_read_in_a_task(plugin, tmp_path):
    # 2026-11-09 tsudo: one row clicked in the list was chosen for a moment, then let go. The record opened in the form is
    # read again (an Esri store, in a task), and the list shown anew forgot which row was chosen (and where it was scrolled).
    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    assert wait_for(lambda: dock.book.loaded)
    for name, lon in (("あおば", 139.65), ("いぶき", 139.7), ("うらが", 139.75)):
        dock.set_active(True)
        dock.map_clicked(lon, 35.6, None)
        dock.object_input.setText(name)
        dock.submit()
        assert wait_for(lambda: not dock._running and not dock._queue)
    assert dock.table.rowCount() == 3
    _click_row(dock.table, 1)
    assert dock.form.isVisible() and dock.pending[2].object_name == "いぶき"
    assert wait_for(lambda: not dock._refreshing and not dock._running and not dock._queue)
    assert _chosen_rows(dock) == [1] and dock.table.currentRow() == 1
    # Ctrl + click adds one; both stay chosen whenever the list is shown again (a save, a refresh, the 通報 read).
    _click_row(dock.table, 2, Qt.KeyboardModifier.ControlModifier)
    dock.render()
    assert _chosen_rows(dock) == [1, 2] and dock.bulk_button.isVisible()
    assert sorted(o.object_name for o in dock.selected_records()) == ["いぶき", "うらが"]


def test_the_rows_chosen_and_the_scroll_stay_while_the_same_list_is_shown_again(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    dock.resize(500, 700)
    for i in range(60):
        book.add(139.6 + i * 0.002, 35.6, object=f"モノ{i:02d}", kind="船舶", activity="停泊")
    dock.render()
    APP.processEvents()
    table, bar = dock.table, dock.table.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    scrolled = bar.value()
    names = [table.item(r, 0).text() for r in range(table.rowCount())]
    sel = table.selectionModel()
    for name in ("モノ57", "モノ59"):
        sel.select(table.model().index(names.index(name), 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    # A record added elsewhere moves the rows: the same records are chosen again, not the same row numbers.
    book.add(139.5, 35.6, object="あ", kind="船舶")
    dock.render()
    assert table.item(0, 0).text() == "あ" and bar.value() == scrolled
    assert sorted(o.object_name for o in dock.selected_records()) == ["モノ57", "モノ59"]
    # Another image: its own list, nothing chosen.
    dock.step(1)
    assert _chosen_rows(dock) == [] and bar.value() == 0
    dock.step(-1)
    assert _chosen_rows(dock) == []
    # 「モノ」: a thing clicked is chosen (its flow below) and stays so; clicked again, its flow closes and the row is let go.
    dock.tabs.setCurrentIndex(1)
    APP.processEvents()
    _click_row(table, 3)
    chosen = dock.chosen
    assert chosen and _chosen_rows(dock) == [3] and dock.flow_box.isVisible()
    dock.render()
    assert _chosen_rows(dock) == [3]
    _click_row(table, 3)
    assert dock.chosen is None and _chosen_rows(dock) == [] and not dock.flow_box.isVisible()


def test_records_saved_offline_are_told_and_the_dock_keeps_drawing(plugin, tmp_path):
    # Re-check 2026-11-09: the note on what waits once broke every render of the dock (and the map's marks) while offline.
    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}], "localObservations": False}), encoding="utf-8")
    settings.set_config_source(str(config))

    def esri():
        client = EsriClient(p.context.offline.transport(fake))
        client.offline = p.context.offline
        return client

    p.context.esri = esri
    p.context.reload()
    assert wait_for(lambda: dock.book is not None and dock.book.loaded)
    p.offline.act_forced.setChecked(True)
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("オフラインのモノ")
    dock.submit()
    assert wait_for(lambda: dock.waiting_ids())
    dock.render()
    assert dock.offline_note.isVisibleTo(dock) and "送信待ち 1 件" in dock.offline_note.text()
    assert layer.rows == [] and [o.object_name for o in living(dock.book.records)] == ["オフラインのモノ"]


def test_the_store_chosen_follows_the_settings_file(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    for url in (URL, URL2):
        fake.add_layer(url, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    p.context.esri = lambda: EsriClient(fake)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    b.write_text(json.dumps({"observationStores": [{"label": "部署", "url": URL2}]}), encoding="utf-8")
    assert dock.store_select.currentText() == "ローカル"
    settings.set_config_source(str(a))
    p.context.reload()
    assert dock.store_select.currentText() == "共有" and dock.book.label == "共有"
    # The same file read again keeps what was chosen by hand.
    dock.store_select.setCurrentIndex(1)
    p.context.reload()
    assert dock.store_select.currentText() == "ローカル" and dock.book.label == "ローカル"
    # Another file: its first store, though the local one is in it too.
    settings.set_config_source(str(b))
    p.context.reload()
    assert dock.store_select.currentText() == "部署" and dock.book.label == "部署"
    # The same file with other stores in it: its first store again.
    dock.store_select.setCurrentIndex(1)
    b.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}, {"label": "部署", "url": URL2}]}),
                 encoding="utf-8")
    p.context.reload()
    assert dock.store_select.currentText() == "共有" and dock.store_select.count() == 3
    # No settings file: the local store.
    settings.set_config_source("")
    p.context.reload()
    assert dock.store_select.count() == 1 and dock.book.label == "ローカル"
    assert wait_for(lambda: not dock._running)


def test_local_records_can_be_turned_off_in_the_settings(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}], "localObservations": False}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    # Only the Esri store, chosen in place of the local one.
    assert dock.store_select.count() == 1 and dock.store_set.remote and dock.book.label == "共有"
    assert not dock.store_select.isVisible() and not dock.clear_button.isEnabled()
    # With no Esri store either, nothing can be recorded.
    config.write_text(json.dumps({"localObservations": False}), encoding="utf-8")
    p.context.reload()
    assert dock.sets == [] and dock.book is None
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    assert dock.book is None
    # Turned on again, the local store is back.
    config.write_text(json.dumps({}), encoding="utf-8")
    p.context.reload()
    assert dock.book.label == "ローカル"


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
    # 「更新」: the classification as someone else saved it since.
    dock.book.taxonomy_store.save([Taxon(["船舶"]), Taxon(["船舶", "新型"])])
    editor.select(["車両"])
    [refresh] = [b for b in editor.findChildren(QPushButton) if b.text() == "更新"]
    refresh.click()
    assert editor._item_of("船舶 > 新型") is not None and editor.selected is None
    assert dock.status.text().endswith("分類 2 件を読み込み")


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


def test_the_classification_editor_sets_a_classs_activities(plugin):
    p, iface, dock, layers, tmp_path = plugin
    p.edit_taxonomy(["船舶"])
    editor = p.docks["SatObsTaxonomy"]
    assert editor.activities_input.text().startswith("停泊")
    editor.activities_input.setText("停泊、曳航")
    editor.apply()
    assert dock.book.activities_for("船舶 > 護衛艦") == (["停泊", "曳航"], ["船舶"])
    dock.book.add_class(["船舶", "護衛艦"])
    p.edit_taxonomy(["船舶", "護衛艦"])
    # Empty: the class above's, shown faintly.
    assert editor.activities_input.text() == "" and editor.activities_input.placeholderText() == "停泊、曳航（船舶）"


def test_a_record_that_could_not_be_saved_comes_back_in_the_form(plugin, monkeypatch):
    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.kind_input.setText("船舶 > 護衛艦")
    dock.note_input.setText("入港直後")
    # The store refuses (as an Esri store does when the network or the token fails).
    monkeypatch.setattr(type(book), "submit", lambda self, *a, **k: None)
    dock.submit()
    assert dock.form.isVisible() and dock.object_input.text() == "あおば" and dock.note_input.text() == "入港直後"
    assert any("送信待ちに保管しました" in w.text() for w in iface.bar.items())
    monkeypatch.undo()
    dock.submit()
    assert not dock.form.isVisible() and [o.note for o in book.living()] == ["入港直後"]


def test_the_record_chosen_in_the_list_opens_below_the_list(plugin):
    p, iface, dock, layers, _ = plugin
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.submit()
    dock.parentWidget().resize(400, 300)
    dock.resize(400, 300)
    APP.processEvents()
    # 「この画像」 tab: its row opens the record's form, under the table (as a thing's and a class's details).
    assert dock.tabs.currentIndex() == 0 and dock.table.rowCount() == 1
    dock._row_clicked(0, 0)
    assert dock.form.isVisible() and dock.form.title() == "あおば"
    order = [dock.body.itemAt(i).widget() for i in range(dock.body.count())]
    assert order.index(dock.table) < order.index(dock.form) < order.index(dock.flow_box)
    APP.processEvents()
    APP.processEvents()
    assert dock.form.y() > dock.table.y()
    # Scrolled to it when the dock is too small to show it all.
    bar = dock.scroll.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() > 0


def test_the_lists_are_filtered_by_a_top_class(plugin):
    p, iface, dock, layers, _ = plugin
    dock.set_active(True)
    for name, kind, lon in (("あおば", ["船舶", "護衛艦"], 139.7), ("はやて", ["航空機"], 139.8), ("みずほ", ["船舶"], 139.9)):
        dock.map_clicked(lon, 35.6, None)
        dock.object_input.setText(name)
        dock.choose_kind(kind)
        dock.submit()

    def names():
        return sorted(dock.table.item(r, 0).text() for r in range(dock.table.rowCount()))

    assert dock.tabs.currentIndex() == 0 and dock.class_filter_row.isVisible()
    combo = dock.root_filter
    roots = [combo.itemData(i) for i in range(combo.count())]
    assert roots[0] == "" and {"船舶", "航空機", "車両"} <= set(roots)
    assert names() == ["あおば", "はやて", "みずほ"]
    # 「この画像」: only the ships, and it stays on the tab.
    combo.setCurrentIndex(combo.findData("船舶"))
    combo.activated.emit(combo.currentIndex())
    assert dock.tabs.currentIndex() == 0 and names() == ["あおば", "みずほ"]
    # 「モノ」 keeps the same filter.
    dock.tabs.setCurrentIndex(1)
    assert names() == ["あおば", "みずほ"] and combo.currentData() == "船舶"
    dock.filter_root("航空機")
    assert names() == ["はやて"]
    # A class below (分類 tab's double click) shows its top class and the path.
    dock.filter_class(["船舶", "護衛艦"])
    assert names() == ["あおば"] and combo.currentData() == "船舶" and "護衛艦" in dock.class_filter_label.text()
    # Nothing of a top class: says so; × shows all again.
    dock.tabs.setCurrentIndex(0)
    dock.filter_root("車両")
    assert dock.table.rowCount() == 1 and "絞り込み" in dock.table.item(0, 0).text()
    dock.unfilter_button.click()
    assert names() == ["あおば", "はやて", "みずほ"] and combo.currentData() == ""
    # Not offered in 「分類」.
    dock.tabs.setCurrentIndex(2)
    assert not dock.class_filter_row.isVisible()


def test_carry_all_and_absent_inside_keep_to_the_top_class_shown(plugin):
    p, iface, dock, layers, _ = plugin
    book = dock.book
    dock.set_active(True)
    for name, kind, lon in (("A", "船舶", 139.70), ("B", "航空機", 139.71), ("C", "船舶", 139.76), ("D", "航空機", 139.77)):
        dock.map_clicked(lon, 35.60, None)
        dock.object_input.setText(name)
        dock.kind_input.setText(kind)
        dock.submit()
    dock.step(1)
    # A checked area (all kinds) over A and B.
    dock.set_area_mode(True)
    dock.map_clicked(139.69, 35.59, None)
    dock.map_clicked(139.72, 35.62, None)
    dock._submit_area()
    dock.filter_root("船舶")
    # Only the ships count, and only they are recorded; the aircraft stay as they were.
    assert dock.carry_label.text() == "前後の画像のモノ 2 件" and dock.absent_inside_button.text() == "確認範囲内 1 件を不在"
    dock.carry_all()
    dock.absent_inside()
    assert sorted((o.object_name, o.present) for o in book.here()) == [("A", False), ("C", True)]
    # Unfiltered again: the aircraft are still to be done.
    dock.filter_root(None)
    assert dock.carry_label.text() == "前後の画像のモノ 2 件" and dock.carry_all_button.isVisibleTo(dock)
    dock.carry_all()
    dock.absent_inside()
    assert sorted((o.object_name, o.present) for o in book.here()) == [("A", False), ("B", False), ("C", True), ("D", True)]


def test_others_records_are_read_quietly(plugin, tmp_path):
    """観測記録の読み直し: another computer's record shows after a refresh; a form opened on a record they changed is filled again."""
    from dataclasses import replace

    from skywatch.core.obs.flows import now_ms
    from skywatch.core.obs.model import observation_store_of
    from skywatch.core.obs.stores import EsriObservationStore

    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL, "refreshMinutes": 2}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    assert wait_for(lambda: dock.book.loaded)
    assert dock._refresh_timer.isActive() and dock._refresh_timer.interval() == 120_000
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("共有のモノ")
    dock.submit()
    assert wait_for(lambda: dock.status.text() == "共有のモノを記録")
    theirs = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))
    [mine] = theirs.load()
    theirs.save([replace(mine, note="鈴木の備考", updated_by="鈴木", updated_at=now_ms() + 1000)])
    # Opened before their change, nothing typed yet: filled again with theirs.
    dock.open_form(mine.lon, mine.lat, editing=dock.book.record(mine.id))
    assert wait_for(lambda: dock.note_input.text() == "鈴木の備考")
    assert dock.pending[2].note == "鈴木の備考"
    dock.close_form()
    # A record of theirs: shown after the quiet read (on another image, at most every 30 s; forced here).
    theirs.save([replace(mine, id="theirs-1", object_id="B", object_name="鈴木のモノ", updated_at=now_ms())])
    dock.refresh(force=True)
    assert wait_for(lambda: any(o.object_name == "鈴木のモノ" for o in dock.book.records))


def test_records_not_saved_are_kept_sent_again_and_never_twice(plugin, monkeypatch):
    """Records not saved: kept in 送信待ち when the save fails, even with another form open; sent again with the same id."""
    from skywatch.core.obs.book import ObservationBook
    from skywatch.core.obs.drafts import OutboxDrafts
    from skywatch.qgis_io import settings as qs

    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    real_save = book.store.save

    def refuse(records):
        raise OSError("ネットワークに接続できません")

    monkeypatch.setattr(book.store, "save", refuse)
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.note_input.setText("一つ目")
    dock.submit()
    assert dock.form.isVisible() and dock.note_input.text() == "一つ目"
    assert dock.drafts_button.isVisible() and dock.drafts_button.text() == "送信待ち 1 件…"
    first = book.drafts()[0]
    assert first.record.object_name == "あおば" and "ネットワーク" in first.reason
    # The same list as the status bar's: the outbox of the offline work.
    assert p.offline.work.counts() == {"draft": 1} and p.offline.button.text() == "送信待ち 1 件"
    # Sent again from the form: the same record (not a second one), still failing.
    dock.submit()
    assert [d.id for d in book.drafts()] == [first.id]
    # Given up in the form (Esc): still kept; another one failing is kept beside it.
    dock.close_form()
    dock.map_clicked(139.71, 35.61, None)
    dock.object_input.setText("いなづま")
    dock.note_input.setText("二つ目")
    dock.submit()
    assert len(book.drafts()) == 2 and dock.drafts_button.text() == "送信待ち 2 件…"
    dock.close_form()
    # Kept in the offline work's file: another book of the same store (QGIS started again) has them.
    again = ObservationBook(book.store, book.area_store, book.taxonomy_store, book.vocabulary, prefs=qs.SettingsPrefs(),
                            drafts=OutboxDrafts(p.offline.work))
    assert [d.record.note for d in again.drafts()] == ["一つ目", "二つ目"]
    # The network is back: one opened in the form and saved, the other sent from the list.
    monkeypatch.setattr(book.store, "save", real_save)
    dialog = dock.open_drafts()
    assert dialog is p.offline.dialog and dialog.tree.topLevelItemCount() == 2
    assert all(dialog.tree.topLevelItem(i).text(4).startswith("保存できず") for i in range(2))
    assert dock.open_draft(first.id) and dock.note_input.text() == "一つ目"
    dock.submit()
    assert [o.id for o in book.living()] == [first.id]
    assert len(book.drafts()) == 1 and dialog.tree.topLevelItemCount() == 1
    reports = []
    dialog.controller.upload(then=reports.append)
    assert wait_for(lambda: reports and not p.offline.uploading)
    assert reports[0].sent == {"observation": 1} and p.offline.work.total() == 0
    assert wait_for(lambda: sorted(o.note for o in dock.book.living()) == ["一つ目", "二つ目"])
    assert not dock.drafts_button.isVisible() and not p.offline.button.isVisible()
