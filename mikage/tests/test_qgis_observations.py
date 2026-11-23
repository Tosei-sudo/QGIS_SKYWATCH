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
from qgis.core import QgsMapLayerType, QgsProject, QgsRasterLayer, QgsSettings, QgsVectorLayerTemporalProperties  # noqa: E402
from qgis.PyQt.QtCore import QDateTime, QItemSelectionModel, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QPushButton, QToolButton  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.flows import living  # noqa: E402
from mikage.core.obs.taxonomy import Taxon  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.obs_images import IMAGE_ID, image_layer, observed_image  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.ui.obs_report_dialog import ReportDialog  # noqa: E402

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
def plugin(tmp_path, request):
    # Layers another test of this process left in the project would be images here too.
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")

    iface = FakeIface()
    p = small_plugin(iface, Observations, request=request)
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
    QgsSettings().remove("mikage")


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
    from mikage.qgis_io.obs_images import IMAGE_FILE

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
    # One image of the record, named by its id (tsudo 2026-11-10; it was its folder's name), its footprint around the files.
    assert [i.name for i in book.images] == ["d1.tif", "d2.tif", "d3.tif", "R"]
    scene = book.images[3]
    assert scene.id == "R" and scene.layer_ids == tuple(x.id() for x in tiles) and scene.time == ms(4)
    assert scene.box == pytest.approx([139.6, 35.5, 140.2, 35.7])
    assert dock.image_select.count() == 4 and dock.image_select.itemText(3).endswith("  R（3 ファイル）")
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
    assert (a.image_id, a.image_name, a.image_time) == ("R", "R", ms(4))
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
    assert [m.label for m in dock.scene().marks] == ["あおば（消失）（推定）"]
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
    # Cannot be undone: the administrators' only (担当「管理者」 with the records'), not shown otherwise.
    assert not dock.clear_button.isVisible()
    p.role_keeper.choose(["OBSERVER", "ADMIN"])
    assert dock.clear_button.isVisible() and dock.clear_button.isEnabled()
    dock.more.setChecked(False)
    dock.more.setChecked(True)
    assert dock.clear_button.isVisible()
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
    assert dock.measure_note.text() == "長さ（m）は数字で入力（「120m くらい」は読めない）"
    assert dock.book.living()[0].length == 120.5
    dock.length_input.setText("130 m")
    dock.submit()
    assert not dock.form.isVisible() and dock.book.living()[0].length == 130
    # A file that can't be written (open in Excel, a folder gone): said in the dock, not a Python error.
    assert dock.save_csv(str(tmp_path / "gone" / "records.csv")) is None
    assert dock.status.text().startswith("CSV の保存に失敗: ")
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
    assert dock.rear is None and dock.status.text() == "後端を取り消し"
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
    assert esc_on_map() and dock.rear is None and dock.status.text() == "後端を取り消し"
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
    assert dock.placed == [(139.71, 35.6)] and dock.status.text() == "側面を取り消し"
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

    from mikage.ui.obs_overlay import ScenePainter

    image = QImage(200, 200, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    scene_painter = ScenePainter(lambda lon, lat: QPointF(lon, lat), style=style)
    scene_painter.paint(painter, scene)
    painter.end()
    return image, scene_painter


def test_the_marks_painted_as_outlines_leave_what_they_mark_clear():
    """「印」 枠だけ (tsudo, 2026-11-09): nothing filled over the thing; 非表示: only the input being made."""
    from mikage.ui.obs_overlay import Mark, Scene

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
    from mikage.ui.observation_dock import ObservationDock

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
    assert dock.absent_inside_button.text() == "確認範囲内 1 件を消失に"
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


def test_a_cloud_and_one_thing_not_seen_are_recorded_neither_there_nor_gone(plugin):
    # 不可 (tsudo 2026-11-19, 案A): a 不可範囲 records the things carried into it at once; the form's 「不可」 one thing.
    p, iface, dock, layers, _ = plugin
    book = dock.book
    dock.set_active(True)
    for name, lon, lat in (("A", 139.70, 35.60), ("B", 139.75, 35.65), ("C", 139.78, 35.68)):
        dock.map_clicked(lon, lat, None)
        dock.object_input.setText(name)
        dock.kind_input.setText("船舶")
        dock._pick_activity("停泊")
        dock.submit()
    dock.step(1)
    assert dock.unseen_area_button.isVisible() and not dock.unseen_inside_button.isVisible()
    dock.set_area_mode(True, unseen=True)
    assert dock.area_form.title() == "不可範囲（雲など）" and dock.area_submit_button.text() == "不可範囲を記録（Enter）"
    assert dock.area_reason.isVisible() and not dock.area_kind.isVisible() and dock.area_reason.currentText() == "雲"
    assert dock.status.text().startswith("不可範囲（雲など）:") and not dock.area_button.isChecked()
    dock.map_clicked(139.69, 35.59, None)
    dock.map_hovered(139.72, 35.62)
    assert dock.scene().area_pending is not None and dock.scene().area_pending_unseen
    dock.map_clicked(139.72, 35.62, None)
    dock.area_reason.setCurrentText("霞")
    dock._submit_area()
    assert not dock.area_mode and dock.status.text() == "不可範囲（霞）を記録（中の前後の画像のモノ 1 件を不可として記録）"
    [a] = [o for o in book.here() if o.unseen]
    assert (a.object_name, a.unseen, a.present, a.activity) == ("A", "霞", False, "")
    scene = dock.scene()
    assert len(scene.unseen_areas) == 1 and scene.areas == []
    assert [(m.label, m.unseen) for m in scene.marks if m.role == "obs"] == [("A（不可（霞））", True)]
    # 「すべて継続」 leaves nothing in the cloud; the form's 「不可」 (▼ a reason) records one thing alone.
    ghost = next(m for m in dock.scene().marks if m.role == "ghost" and m.lon == 139.75)
    dock.map_clicked(ghost.lon, ghost.lat, ghost)
    assert dock.unseen_button.isVisible() and [x.text() for x in dock.unseen_button.menu().actions()] == ["雲", "霞", "影", "画像の欠け", "その他"]
    dock.unseen_button.menu().actions()[2].trigger()
    assert dock.status.text() == "B（不可（影））を記録"
    dock.carry_all()
    assert sorted((o.object_name, o.present, o.unseen) for o in book.here()) == [("A", False, "霞"), ("B", False, "影"), ("C", True, "")]
    # The flow says so, and the next image carries A and B from where they were last seen.
    flow = book.flow(a.object_id)
    assert flow.unseen == "霞" and flow.activity == "停泊"
    # Its steps on the map: the 不可 record where A was last seen, one mark with both labels.
    dock.chosen = a.object_id
    assert [m.label for m in dock.scene().marks if m.role == "step"] == ["10-01 01:00・10-02 01:00（不可）"]
    dock.chosen = None
    dock.step(1)
    assert sorted(c.source.object_name for c in book.carried()) == ["A", "B", "C"]
    # Deleting the cloud deletes its 不可 record too.
    dock.step(-1)
    [area] = book.image_areas()
    assert book.remove_areas([area]) and [o.object_name for o in book.here() if o.unseen] == ["B"]


def test_lists_an_image_without_its_file_and_types_a_time(plugin):
    p, iface, dock, layers, _ = plugin
    dock.list_button.setChecked(True)
    dock.list_id.setText("EXT-1")
    dock.list_name.setText("外部")
    dock._list_from_form()
    book = dock.book
    assert book.current.id == "EXT-1" and not book.current.file and dock.time_row.isVisible()
    assert dock.status.text() == "ファイルのない画像 外部 を追加"
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
    assert wait_for(lambda: dock.status.text().endswith("件を取得"))
    # The local store is the other choice, with its own records.
    dock.store_select.setCurrentIndex(1)
    assert dock.book.label == "ローカル" and dock.book.living() == []
    dock.store_select.setCurrentIndex(0)
    assert [o.object_name for o in living(dock.book.records)] == ["共有のモノ"]


def _click_row(table, row: int, modifier=Qt.KeyboardModifier.NoModifier) -> None:
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, modifier, table.visualRect(table.model().index(row, 1)).center())


def _chosen_rows(dock) -> list[int]:
    return sorted({i.row() for i in dock.table.selectionModel().selectedRows()})


def _flow_button(table, row: int):
    """The 「流れ」 button of a row of 「この画像」."""
    [button] = [b for b in table.cellWidget(row, table.columnCount() - 1).findChildren(QToolButton) if b.text() == "流れ"]
    return button


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
    # Qt answers QApplication.keyboardModifiers() with the last ones it saw (Ctrl): a plain click below the rows lets go
    # of them, or a row clicked in a later test of this process reads as Ctrl + click.
    viewport = dock.table.viewport()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(2, viewport.height() - 2))


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
    # 「流れ」 of a row: its thing's flow below the list, the button pressed; the rows chosen stay. Pressed again, it closes.
    _click_row(table, 3)
    _flow_button(table, 3).click()
    chosen = dock.chosen
    assert chosen and dock.flow_box.isVisible() and _flow_button(table, 3).isChecked() and not _flow_button(table, 4).isChecked()
    dock.render()
    assert dock.chosen == chosen and _flow_button(table, 3).isChecked() and _chosen_rows(dock) == [3]
    _flow_button(table, 3).click()
    assert dock.chosen is None and not dock.flow_box.isVisible() and not _flow_button(table, 3).isChecked()


def test_a_things_flow_opens_below_the_list_and_a_row_of_it_clicked_only_goes_to_its_image(plugin):
    # tsudo 2026-11-18: 「モノタブは流れボタンを押したときにそれだけ見れれば十分じゃない？」 (no 「モノ」 tab: a row's 「流れ」
    # shows the flow below the list and the track on the map), and 「ものタブを選んで、下のテーブルで行を選択したら、
    # 入力モードになるんだけど、これは仕様？」 (a look at a flow is not an edit).
    p, iface, dock, layers, _ = plugin
    book = dock.book
    assert book.current.name == "d1.tif"
    first = book.add(139.7, 35.6, object="船A", kind="船舶", activity="停泊")
    dock.step(2)
    assert book.current.name == "d3.tif"
    book.add(139.72, 35.6, object="船A", kind="船舶", activity="航行")
    book.add(139.75, 35.55, object="船B", kind="船舶", activity="停泊")
    dock.render()
    APP.processEvents()
    table = dock.table
    assert not dock.flow_box.isVisible() and not dock.scene().track
    names = [table.item(r, 0).text() for r in range(table.rowCount())]
    _flow_button(table, names.index("船A")).click()
    assert dock.flow_box.isVisible() and dock.flow_box.title().startswith("船A") and dock.steps_table.rowCount() == 2
    # Its track on the map, and where it was in each image.
    assert dock.scene().track == [(139.7, 35.6), (139.72, 35.6)] and len([m for m in dock.scene().marks if m.role == "step"]) == 2
    # Another row's 「流れ」: that thing's flow instead.
    _flow_button(table, names.index("船B")).click()
    assert dock.flow_box.title().startswith("船B") and dock.steps_table.rowCount() == 1 and not dock.scene().track
    _flow_button(table, names.index("船A")).click()
    # A row of the flow clicked goes to that image and shows the record; recording and the form stay off.
    steps = dock.steps_table
    QTest.mouseClick(steps.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, steps.visualRect(steps.model().index(0, 1)).center())
    assert book.current.name == "d1.tif"
    assert not dock.active and not dock.form.isVisible() and not dock.record_button.isChecked()
    assert dock.flow_box.isVisible() and dock.chosen == first.object_id
    assert {i.row() for i in steps.selectionModel().selectedRows()} == {0}
    assert "の画像へ移動。ダブルクリックでこの記録を編集" in dock.status.text()
    # A double click opens the record in the form (and recording, to move it), as before.
    steps.cellDoubleClicked.emit(0, 0)
    assert dock.active and dock.form.isVisible() and dock.pending[2] is not None and dock.pending[2].id == first.id
    dock.set_active(False)
    # × closes it, and its track.
    dock.flow_close.click()
    assert not dock.flow_box.isVisible() and dock.chosen is None and not dock.scene().track


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
    # Named by the catalog's id too (tsudo 2026-11-10: the records' image names were file names for some images).
    assert (info.id, info.name, info.time, info.origin) == ("CAT-9", "CAT-9", ms(5), "layer")
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
    assert dock.status.text().endswith("分類 2 件を取得")


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
    assert p.api.delete_observations(a["id"]) == 1 and p.api.observations(object=a["objectId"]) == []
    # A shared store is not read whole (2026-11-18): a script names what it wants.
    with pytest.raises(ValueError, match="全件は読まない"):
        p.api.observations()
    assert [o["id"] for o in p.api.observations(object=a["objectId"], deleted=True)] == [a["id"]]


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
    assert any("送信待ちに保管" in w.text() for w in iface.bar.items())
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
    # Its row opens the record's form, under the table (above a thing's flow).
    assert dock.table.rowCount() == 1
    dock._row_clicked(0, 0)
    assert dock.form.isVisible() and dock.form.title() == "あおば"
    work = dock.work_area.layout()
    order = [work.itemAt(i).widget() for i in range(work.count())]
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

    # The count where the tab said it (tsudo 2026-11-18: no tabs).
    assert dock.class_filter_row.isVisible() and dock.count_label.text() == "この画像 3 件"
    combo = dock.root_filter
    roots = [combo.itemData(i) for i in range(combo.count())]
    assert roots[0] == "" and {"船舶", "航空機", "車両"} <= set(roots)
    assert names() == ["あおば", "はやて", "みずほ"]
    # Only the ships, of how many.
    combo.setCurrentIndex(combo.findData("船舶"))
    combo.activated.emit(combo.currentIndex())
    assert names() == ["あおば", "みずほ"] and dock.count_label.text() == "この画像 2 / 3 件"
    dock.filter_root("航空機")
    assert names() == ["はやて"]
    # A class below (「分類体系の編集」's 「この画像で絞り込み」) shows its top class and the path.
    editor = p.docks["SatObsTaxonomy"]
    editor.select(["船舶", "護衛艦"])
    [narrow] = [b for b in editor.findChildren(QPushButton) if b.text() == "この画像で絞り込み"]
    narrow.click()
    assert dock.class_filter == ["船舶", "護衛艦"]
    assert names() == ["あおば"] and combo.currentData() == "船舶" and "護衛艦" in dock.class_filter_label.text()
    # Nothing of a top class: says so; × shows all again.
    dock.filter_root("車両")
    assert dock.table.rowCount() == 1 and "絞り込み" in dock.table.item(0, 0).text() and dock.count_label.text() == "この画像 0 / 3 件"
    dock.unfilter_button.click()
    assert names() == ["あおば", "はやて", "みずほ"] and combo.currentData() == "" and dock.count_label.text() == "この画像 3 件"
    # The classes are edited in 「分類体系の編集」, opened from 「詳細」 (at the class shown).
    dock.filter_root("船舶")
    dock.more.setChecked(True)
    assert dock.taxonomy_button.isVisible() and dock.taxonomy_button.text() == "分類体系の編集…"
    editor.hide()
    dock.taxonomy_button.click()
    assert editor.isVisible() and editor.selected == "船舶"


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
    assert dock.carry_label.text() == "前後の画像のモノ 2 件" and dock.absent_inside_button.text() == "確認範囲内 1 件を消失に"
    dock.carry_all()
    dock.absent_inside()
    assert sorted((o.object_name, o.present) for o in book.here()) == [("A", False), ("C", True)]
    # Unfiltered again: the aircraft are still to be done.
    dock.filter_root(None)
    assert dock.carry_label.text() == "前後の画像のモノ 2 件" and dock.carry_all_button.isVisibleTo(dock)
    dock.carry_all()
    dock.absent_inside()
    assert sorted((o.object_name, o.present) for o in book.here()) == [("A", False), ("B", False), ("C", True), ("D", True)]


def test_absent_all_records_every_thing_shown_as_gone(plugin):
    """「すべて消失」: every carried-over thing the list shows is recorded as gone here (in checked areas too)."""
    p, iface, dock, layers, _ = plugin
    book = dock.book
    dock.set_active(True)
    for name, kind, lon in (("A", "船舶", 139.70), ("B", "航空機", 139.71), ("C", "船舶", 139.76), ("D", "航空機", 139.77)):
        dock.map_clicked(lon, 35.60, None)
        dock.object_input.setText(name)
        dock.kind_input.setText(kind)
        dock.submit()
    dock.step(1)
    assert dock.absent_all_button.isVisibleTo(dock) and dock.absent_all_button.text() == "すべて消失"
    # A checked area (all kinds) over A and B.
    dock.set_area_mode(True)
    dock.map_clicked(139.69, 35.59, None)
    dock.map_clicked(139.72, 35.62, None)
    dock._submit_area()
    dock.filter_root("船舶")
    assert dock.carry_label.text() == "前後の画像のモノ 2 件" and dock.absent_all_button.isVisibleTo(dock)
    dock.absent_all()
    assert dock.status.text() == "2 のモノを消失として記録"
    assert sorted((o.object_name, o.present) for o in book.here()) == [("A", False), ("C", False)]
    # Unfiltered again: the aircraft are still to be done; only B (in the area) left → only 「確認範囲内 1 件を消失に」.
    dock.filter_root(None)
    assert dock.carry_label.text() == "前後の画像のモノ 2 件" and dock.absent_all_button.isVisibleTo(dock)
    dock.carry(next(c.source.object_id for c in book.carried() if c.source.object_name == "D"))
    assert not dock.absent_all_button.isVisibleTo(dock) and not dock.carry_all_button.isVisibleTo(dock)
    assert dock.absent_inside_button.isVisibleTo(dock) and dock.absent_inside_button.text() == "確認範囲内 1 件を消失に"
    dock.absent_inside()
    assert sorted((o.object_name, o.present) for o in book.here()) == [("A", False), ("B", False), ("C", False), ("D", True)]
    assert dock.carry_label.text() == "" and not dock.absent_all_button.isVisibleTo(dock)


def test_others_records_are_read_quietly(plugin, tmp_path):
    """観測記録の読み直し: another computer's record shows after a refresh; a form opened on a record they changed is filled again."""
    from dataclasses import replace

    from mikage.core.obs.flows import now_ms
    from mikage.core.obs.model import observation_store_of
    from mikage.core.obs.stores import EsriObservationStore

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


def test_a_read_asked_while_one_reads_is_done_after_it(plugin, tmp_path, monkeypatch):
    """
    観測記録の読み直し: a read asked (a record opened to change, after a
    conflict…) while a quiet read is still going, which read before what is
    asked for, is done once more after it, not dropped; another store chosen
    meanwhile is not held back by it.
    """
    import threading

    from mikage.core.obs.flows import now_ms
    from mikage.core.obs.model import observation_store_of
    from mikage.core.obs.stores import EsriObservationStore
    from mikage.tests.test_obs import obs

    p, iface, dock, layers, _ = plugin
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    assert wait_for(lambda: dock.book.loaded)
    book = dock.book
    theirs = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))
    # The first read is slow to end (a busy computer): it has read, and ends only once let go.
    read, let_go, real, reads = threading.Event(), threading.Event(), book.refresh, []

    def slow():
        said = real()
        reads.append(said)
        if len(reads) == 1:
            read.set()
            let_go.wait(10)
        return said

    monkeypatch.setattr(book, "refresh", slow)
    dock.refresh(force=True)
    assert wait_for(read.is_set)
    theirs.save([obs(id="theirs-1", object_id="B", object_name="鈴木のモノ", updated_by="鈴木", updated_at=now_ms())])
    dock.refresh(force=True)  # asked while the first one has not ended
    let_go.set()
    assert wait_for(lambda: any(o.object_name == "鈴木のモノ" for o in book.records))
    assert wait_for(lambda: not dock._refreshing and not dock._running and not dock._queue) and len(reads) == 2
    # Another store chosen while one reads: its own reads are not held back.
    read.clear(), let_go.clear(), reads.clear()
    dock.refresh(force=True)
    assert wait_for(read.is_set) and dock._refreshing
    dock.store_select.setCurrentIndex(1)
    assert dock.book is not book and not dock._refreshing
    let_go.set()
    assert wait_for(lambda: not dock._running and not dock._queue) and not dock._refreshing


def test_records_not_saved_are_kept_sent_again_and_never_twice(plugin, monkeypatch):
    """Records not saved: kept in 送信待ち when the save fails, even with another form open; sent again with the same id."""
    from mikage.core.obs.book import ObservationBook
    from mikage.core.obs.drafts import OutboxDrafts
    from mikage.qgis_io import settings as qs

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
    assert all(dialog.tree.topLevelItem(i).text(4).startswith("保存に失敗") for i in range(2))
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


@pytest.fixture
def bare(tmp_path):
    """The plugin with no image open: nothing chosen in 「観測記録」."""
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")

    iface = FakeIface()
    p = small_plugin(iface, Observations)
    p.initGui()
    dock = p.api.observation_dock
    dock.show()
    yield p, iface, dock, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def _listed(image_id: str, day: int):
    from mikage.core.obs.model import ListedImage

    return ListedImage(image_id, image_id, ms(day), [139.6, 35.5, 139.8, 35.7])


def _idle(dock) -> bool:
    """Only the note shown: no list, no form, no recording (the image row and ＋ stay)."""
    return (dock.idle_note.isVisible() and not dock.work_area.isVisible() and not dock.table.isVisible() and not dock.class_filter_row.isVisible()
            and not dock.form.isVisible() and not dock.record_button.isVisible() and not dock.more.isVisible()
            and dock.list_button.isVisible() and not dock.active)


def test_with_no_image_chosen_only_a_note_is_shown_and_the_records_are_not_read(bare):
    """
    tsudo 2026-11-10: with no image chosen the dock showed empty lists (every
    record read for 「モノ」) and could be worked on: now only a note, and the
    records are read once an image is chosen (or something asks for them all).
    """
    p, iface, dock, tmp_path = bare
    book = dock.book
    assert book.current is None and not book.loaded
    assert _idle(dock) and "ここに記録の一覧と入力欄を表示" in dock.idle_note.text() and not dock.only.isEnabled()
    # Nothing to record on: R and A say so (in the note).
    dock.set_active(True)
    dock.set_area_mode(True)
    assert not dock.active and not dock.area_mode and "画像なし" not in dock.idle_note.text()
    # 「再読み込み」 (and the work kept offline sent) leaves them unread too.
    dock.reload()
    assert not book.loaded and dock.overlay.scene_.marks == []
    # 「ファイルなしで記録」 from the catalog: the image chosen, its list and the recording shown, the records read.
    dock.list_catalog_images([_listed("IMG1", 1)])
    assert book.loaded and book.current.id == "IMG1"
    assert dock.work_area.isVisible() and dock.table.isVisible() and not dock.idle_note.isVisible() and dock.only.isEnabled()
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.submit()
    assert [o.object_name for o in book.living()] == ["あおば"]
    # A form open and recording on, then the image taken off the list: all of it ends, the note again.
    dock.map_clicked(139.71, 35.61, None)
    assert dock.form.isVisible() and dock.active
    dock._unlist()
    assert book.current is None and _idle(dock) and dock.pending is None
    assert iface.canvas.mapTool() is not dock.tool


def test_what_uses_every_record_reads_them_when_none_is_chosen(bare):
    """エクスポート, 分類体系, scripts…: the records not read (no image chosen) are read for them, then they go on."""
    p, iface, dock, tmp_path = bare
    dock.list_catalog_images([_listed("IMG1", 1)])
    dock.set_active(True)
    for name, lon in (("あおば", 139.7), ("いぶき", 139.75)):
        dock.map_clicked(lon, 35.6, None)
        dock.object_input.setText(name)
        dock.submit()
    dock._unlist()
    dock.reset_stores()  # as in a later session: the store's records not read
    assert _idle(dock) and not dock.book.loaded
    # The local store reads at once: エクスポート counts them.
    export = dock.open_export()
    assert dock.book.loaded and export.count_label.text() == "該当する記録 2 件"
    export.close()
    dock.reset_stores()
    assert len(p.api.observations()) == 2 and dock.book.loaded
    dock.reset_stores()
    assert len(dock.read_now().records) == 2
    assert _idle(dock)


def test_an_esri_store_is_not_asked_until_an_image_is_chosen(bare):
    p, iface, dock, tmp_path = bare
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    assert dock.store_set.remote and _idle(dock)

    def queries() -> int:
        return sum(1 for url, _form in fake.asked if url == URL + "/query")

    APP.processEvents()
    assert queries() == 0 and not dock.book.loaded
    dock.list_catalog_images([_listed("IMG1", 1)])
    assert wait_for(lambda: dock.book.loaded) and queries() > 0
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("共有のモノ")
    dock.submit()
    assert wait_for(lambda: len(layer.rows) == 1)
    assert wait_for(lambda: not dock._running and not dock._queue)
    dock._unlist()
    dock.reset_stores()
    asked = queries()
    APP.processEvents()
    assert _idle(dock) and not dock.book.loaded and queries() == asked
    # エクスポート: read in a task, counted once read (a shared store: from today by default, so this image's period here).
    export = dock.open_export()
    export.start_edit.setDateTime(QDateTime.fromMSecsSinceEpoch(ms(1) - 3_600_000))
    assert export.count_label.text() in ("観測記録を取得中…", "該当する記録 1 件")
    assert wait_for(lambda: export.count_label.text() == "該当する記録 1 件")
    export.close()
    # The classification editor: its tree once read (a class's records are renamed with it).
    dock.reset_stores()
    taxonomy = p.docks["SatObsTaxonomy"]
    dock.edit_taxonomy(None)
    # (Read in a task: 「観測記録を取得中…」 with an empty tree until then.)
    assert taxonomy.isVisible() and (taxonomy.note.text() == "観測記録を取得中…") == (taxonomy.tree.topLevelItemCount() == 0)
    assert wait_for(lambda: dock.book.loaded) and wait_for(lambda: taxonomy.tree.topLevelItemCount() > 0)
    # (A shared store: the counts are of the records read, around the images opened.)
    assert taxonomy.note.text().startswith("保存先: ") and taxonomy.note.text().endswith("分類（モノと記録の数は取得した分だけ: 開いた画像とそのまわり）")


def test_a_background_map_is_not_an_image(bare):
    """
    tsudo 2026-11-11: with only a background map (tiles) in the project the
    dock listed it as 「日時不明  {y}.png」, chose it, showed the lists and read
    every record. Tiles, WMS and map services are not images; the note stays.
    """
    from mikage.qgis_io.obs_images import open_images

    p, iface, dock, tmp_path = bare
    project = QgsProject.instance()
    tiles = QgsRasterLayer("type=xyz&url=https://example.com/xyz/std/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=18&zmin=2", "地理院地図", "wms")
    assert tiles.isValid()
    project.addMapLayer(tiles)
    dock.refresh_images()
    assert open_images() == [] and dock.book.images == [] and dock.image_select.count() == 0
    assert _idle(dock) and not dock.book.loaded
    # The background map chosen in the layers panel: still no image.
    iface.setActiveLayer(tiles)
    assert dock.book.current is None and _idle(dock)
    # An image opened over it: that one only.
    image = QgsRasterLayer(geotiff(tmp_path / "d1.tif", "2026-10-01T01:00:00Z"), "d1")
    project.addMapLayer(image)
    assert [i.name for i in dock.book.images] == ["d1.tif"] and dock.book.current.layer_id == image.id()
    assert dock.work_area.isVisible() and dock.book.loaded
    # A catalog image that a server draws (its id set when opened) is still one.
    served = QgsRasterLayer("type=xyz&url=https://example.com/img/%7Bz%7D/%7Bx%7D/%7By%7D.png", "S1", "wms")
    served.setCustomProperty(IMAGE_ID, "S1")
    project.addMapLayer(served)
    assert sorted(i.id for i in dock.book.images) == ["S1", "d1.tif"]


# ---- 操作性点検 (tsudo 2026-11-23): which raster is the image, the image seen, the form and the list ----------------


def test_a_background_geotiff_is_not_an_image_unless_made_one(bare):
    """
    操作性点検 1: a GeoTIFF without a time (a background map) was the image
    recorded on (「日時不明 basemap.tif」), reached by ▶ and hidden by
    「この画像のみ」. Now an image is one from the catalog, one whose time can
    be read, or one made so in the layer panel (「観測記録の画像にする」).
    """
    p, iface, dock, tmp_path = bare
    book = dock.book
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    base = QgsRasterLayer(geotiff(tmp_path / "basemap.tif"), "背景地図")
    project.addMapLayer(base)
    assert book.images == [] and book.current is None and _idle(dock)
    iface.setActiveLayer(base)
    assert book.current is None and not dock.record_on_action.isChecked()
    # The right-click entry is on the rasters it applies to.
    assert (dock.record_on_action, QgsMapLayerType.RasterLayer) in iface.layer_actions
    assert base in iface.layer_action_layers[id(dock.record_on_action)]
    # An image over it: that one only; 「この画像のみ」 leaves the background as it is.
    image = QgsRasterLayer(geotiff(tmp_path / "d1.tif", "2026-10-01T01:00:00Z"), "d1")
    project.addMapLayer(image)
    assert [i.name for i in book.images] == ["d1.tif"] and book.current.layer_id == image.id()
    iface.setActiveLayer(base)
    assert book.current.layer_id == image.id()
    dock.only.setChecked(True)
    assert root.findLayer(base.id()).itemVisibilityChecked() and root.findLayer(image.id()).itemVisibilityChecked()
    dock.only.setChecked(False)
    # Made an image: listed without a time (to type one), and recorded on.
    dock.record_on_action.trigger()
    assert dock.record_on_action.isChecked() and [i.name for i in book.images] == ["d1.tif", "basemap.tif"]
    assert book.current.layer_id == base.id() and book.current.time is None and dock.time_row.isVisible()
    # And a background again.
    dock.record_on_action.trigger()
    assert [i.name for i in book.images] == ["d1.tif"] and book.current.layer_id == image.id()
    # An image with a time made a background.
    iface.setActiveLayer(image)
    assert dock.record_on_action.isChecked()
    dock.record_on_action.trigger()
    assert book.images == [] and book.current is None


def test_files_of_the_same_name_are_told_apart_by_their_layers(bare):
    """Two files of one name in different folders: the image chooser adds each layer's name."""
    p, iface, dock, tmp_path = bare
    project = QgsProject.instance()
    for folder, name in (("x", "東京湾 A"), ("y", "東京湾 B")):
        (tmp_path / folder).mkdir()
        project.addMapLayer(QgsRasterLayer(geotiff(tmp_path / folder / "scene.tif", "2026-10-01T01:00:00Z"), name))
    texts = sorted(dock.image_select.itemText(i) for i in range(dock.image_select.count()))
    assert len(texts) == 2 and texts[0].endswith("scene.tif（東京湾 A）") and texts[1].endswith("scene.tif（東京湾 B）")


def test_the_image_recorded_on_is_drawn_over_the_others(plugin, tmp_path):
    """
    操作性点検 10: ◀ ▶ (or the layer panel) changed the image recorded on while
    another image was drawn over it. Now it comes over those that cover it.
    """
    from qgis.PyQt.QtCore import QCoreApplication

    p, iface, dock, layers, _ = plugin
    book = dock.book
    project = QgsProject.instance()
    root = project.layerTreeRoot()

    def order() -> list:
        QCoreApplication.processEvents()
        return [layer.id() for layer in root.layerOrder()]

    def above(a, b) -> bool:
        now = order()
        return now.index(a.id()) < now.index(b.id())

    for step, layer in ((1, layers[1]), (1, layers[2]), (-2, layers[0])):
        dock.step(step)
        assert book.current.layer_id == layer.id() and iface.activeLayer() is layer
        assert all(above(layer, other) for other in layers if other is not layer)
    # Chosen in the layers panel: the same.
    iface.setActiveLayer(layers[2])
    assert all(above(layers[2], other) for other in layers[:2])
    # 「この画像のみ」 (the others hidden): nothing moves.
    dock.only.setChecked(True)
    before = order()
    dock.step(-1)
    assert order() == before
    dock.only.setChecked(False)
    # An image elsewhere (not under another): nothing moves.
    far = QgsRasterLayer(geotiff(tmp_path / "far.tif", "2026-10-04T01:00:00Z", west=141.0), "far")
    project.addMapLayer(far, False)
    root.addLayer(far)  # (at the bottom)
    before = order()
    dock.go_to("far.tif")
    assert book.current.layer_id == far.id() and order() == before
    # A custom layer order (「レイヤ順序」): that order changes, not the tree.
    root.setHasCustomLayerOrder(True)
    tree = root.findLayerIds()
    dock.go_to("d1.tif")
    assert root.findLayerIds() == tree and all(above(layers[0], other) for other in layers[1:])
    root.setHasCustomLayerOrder(False)
    # Images in groups of one's own (操作性点検の再検証 I4): the image's group comes up, the image stays in it.
    a, b = root.insertGroup(0, "作業 A"), root.insertGroup(1, "作業 B")
    for layer, group in ((layers[0], a), (layers[1], b)):
        node = root.findLayer(layer.id())
        group.insertChildNode(0, node.clone())
        node.parent().removeChildNode(node)
    dock.go_to("d1.tif")
    assert above(layers[0], layers[1])
    dock.go_to("d2.tif")
    assert root.findLayer(layers[1].id()).parent().name() == "作業 B" and root.findLayer(layers[0].id()).parent().name() == "作業 A"
    assert above(layers[1], layers[0]) and [n.name() for n in root.children()][:2] == ["作業 B", "作業 A"]
    assert set(project.mapLayers()) >= {x.id() for x in layers}


def test_something_typed_is_asked_about_before_the_image_changes(plugin, monkeypatch):
    """
    操作性点検 2: the form closed without a word when another image was chosen.
    Now 保存 (on the form's image), 破棄 or 元の画像に戻る is asked, only when
    something was typed.
    """
    p, iface, dock, layers, _ = plugin
    book = dock.book
    asked = []
    answer = ["back"]
    monkeypatch.setattr(dock, "ask_unsaved", lambda title, name, back: asked.append((title, name, back)) or answer[0])
    dock.set_active(True)
    # Opened, nothing typed: the image changes as before.
    dock.map_clicked(139.7, 35.6, None)
    dock.step(1)
    assert asked == [] and not dock.form.isVisible() and book.current.name == "d2.tif"
    dock.step(-1)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    # 元の画像に戻る: by ◀ ▶, the image chooser and the layers panel alike.
    dock.step(1)
    assert asked == [("新規の記録", "d1.tif", True)]
    assert book.current.name == "d1.tif" and iface.activeLayer() is layers[0] and dock.form.isVisible() and dock.object_input.text() == "あおば"
    dock.image_select.setCurrentIndex(2)
    dock.image_select.activated.emit(2)
    assert len(asked) == 2 and book.current.name == "d1.tif" and dock.image_select.currentIndex() == 0
    # (the layer panel: asked once QGIS is done with the change)
    iface.setActiveLayer(layers[1])
    assert len(asked) == 2
    assert wait_for(lambda: len(asked) == 3)
    assert book.current.name == "d1.tif" and iface.activeLayer() is layers[0] and dock.form.isVisible()
    # 保存: recorded on the form's image, then on to the other.
    dock.kind_input.setText("船舶 > 護衛艦")
    answer[0] = "save"
    iface.setActiveLayer(layers[2])
    assert wait_for(lambda: len(asked) == 4)
    [a] = book.living()
    assert (a.object_name, a.image_name) == ("あおば", "d1.tif")
    assert book.current.name == "d3.tif" and not dock.form.isVisible()
    # 破棄: nothing recorded, on to the other.
    dock.map_clicked(139.72, 35.62, None)
    dock.object_input.setText("はるな")
    answer[0] = "discard"
    dock.step(-1)
    assert [o.object_name for o in book.living()] == ["あおば"] and book.current.name == "d2.tif" and not dock.form.isVisible()
    # ◀ on the first image, ▶ on the last, the image chosen again: nothing changes, the form being typed stays.
    dock.step(-1)
    dock.map_clicked(139.72, 35.62, None)
    dock.object_input.setText("こんごう")
    asked.clear()
    dock.step(-1)
    dock.image_select.activated.emit(dock.image_select.currentIndex())
    assert asked == [] and book.current.name == "d1.tif" and dock.form.isVisible() and dock.object_input.text() == "こんごう"


def test_the_form_image_being_removed_is_not_offered_to_go_back_to(plugin, monkeypatch):
    """操作性点検の再検証 F10: the image's layer removed with something typed: 保存 or 破棄, not 元の画像に戻る."""
    p, iface, dock, layers, _ = plugin
    book = dock.book
    asked = []
    monkeypatch.setattr(dock, "ask_unsaved", lambda title, name, back: asked.append((name, back)) or "save")
    dock.set_active(True)
    assert book.current.name == "d1.tif"
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.kind_input.setText("船舶 > 護衛艦")
    project = QgsProject.instance()

    root = project.layerTreeRoot()

    def choose_another(*_args):
        # As QGIS's layer panel does: another layer chosen while the node is still there (Qt moves the current row first).
        iface.setActiveLayer(layers[1])

    root.willRemoveChildren.connect(choose_another)
    try:
        project.removeMapLayer(layers[0].id())
    finally:
        root.willRemoveChildren.disconnect(choose_another)
    assert wait_for(lambda: asked)
    assert asked == [("d1.tif", False)]
    [a] = book.living()
    assert (a.object_name, a.image_name) == ("あおば", "d1.tif") and book.current.name == "d2.tif" and not dock.form.isVisible()


def test_a_record_opened_from_its_mark_is_chosen_in_the_list(plugin):
    """操作性点検 4: a mark clicked opened its form, but its row was not chosen in the list."""
    p, iface, dock, layers, _ = plugin
    book = dock.book
    dock.set_active(True)
    for name, lat in (("あおば", 35.6), ("はるな", 35.65)):
        dock.map_clicked(139.7, lat, None)
        dock.object_input.setText(name)
        dock.kind_input.setText("船舶 > 護衛艦")
        dock.submit()
    second = next(o for o in book.living() if o.object_name == "はるな")
    dock.table.clearSelection()
    mark = next(m for m in dock.scene().marks if m.key == second.id)
    dock.map_clicked(mark.lon, mark.lat, mark)
    rows = {i.row() for i in dock.table.selectionModel().selectedRows()}
    assert dock.form.isVisible() and dock.object_input.text() == "はるな"
    assert [dock._row_keys[r] for r in rows] == [f"o:{second.id}"]
