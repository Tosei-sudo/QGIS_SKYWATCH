"""
画像カタログ「投影法を画像に合わせる」・「向き」 (tsudo, 2026-11-25; ui/map_orient.py,
qgis_io/map_view_io.py): opening an image puts the map in its CRS and turns
it so the way it was taken is up; closing the images puts it back; the
north arrow; a turned map's 「開いた画像へ移動」 and 「表示範囲」; the options
one to a row under the buttons. 画像補正ドック「地図の向き」 (tsudo,
2026-11-25/26; ui/orient_row.py): its icon buttons turn the map for the
image worked on, with the catalog's orienter.
"""
import math

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject  # noqa: E402

from mikage.features.enhance import Enhance  # noqa: E402
from mikage.features.images import Images  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.map_view_io import directions_of, file_directions  # noqa: E402
from mikage.tests.test_qgis_catalog import plugin, ring, searched  # noqa: E402,F401 (the fixture)
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: The catalog's plugin with 画像補正 (its 「地図の向き」).
PLUGIN_FEATURES = (Images, Observations, Enhance)


def screen_way(canvas, lon, lat, bearing):
    """Where a true bearing at a place shows on the map (degrees clockwise from the top of the screen)."""
    s = canvas.mapSettings()
    t = QgsCoordinateTransform(WGS84, s.destinationCrs(), QgsProject.instance())
    a = math.radians(bearing)
    p = t.transform(QgsPointXY(lon, lat))
    q = t.transform(QgsPointXY(lon + 0.002 * math.sin(a) / math.cos(math.radians(lat)), lat + 0.002 * math.cos(a)))
    m = s.mapToPixel()
    pp, qq = m.transform(p), m.transform(q)
    return math.degrees(math.atan2(qq.x() - pp.x(), -(qq.y() - pp.y())))


def utm_tiff(path):
    """A small image in WGS 84 / UTM 54N over Tokyo Bay."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 20, 20, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([380000, 100, 0, 3945000, 0, -100])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32654)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).Fill(90)
    ds = None
    return str(path)


def gcp_tiff(path, metadata=None):
    """A SAR-like image placed by GCPs: its columns go east (139.70 → 139.80), its rows south (35.70 → 35.60)."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 40, 40, 1, gdal.GDT_Byte)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    gcps = [gdal.GCP(139.70, 35.70, 0, 0, 0), gdal.GCP(139.80, 35.70, 0, 40, 0), gdal.GCP(139.70, 35.60, 0, 0, 40),
            gdal.GCP(139.80, 35.60, 0, 40, 40)]
    ds.SetGCPs(gcps, srs.ExportToWkt())
    for k, v in (metadata or {}).items():
        ds.SetMetadataItem(k, v)
    ds.GetRasterBand(1).Fill(50)
    ds = None
    return str(path)


@pytest.fixture
def opened(plugin, monkeypatch):  # noqa: F811 (the fixture imported above)
    p, iface, folder, fake, layer = plugin
    images = folder / "2026"
    geotiff(images / "d.tif", west=139.9)
    utm_tiff(images / "u.tif")
    for attrs, box in (
            ({"IMG_ID": "D", "SENSOR": "S1", "OFF_NADIR": 15, "AZ": 200, "PATH": "\\\\nas\\images\\2026\\d.tif"}, (139.9, 35.5, 140.1, 35.7)),
            ({"IMG_ID": "U", "SENSOR": "S1", "OFF_NADIR": 20, "AZ": 30, "PATH": "\\\\nas\\images\\2026\\u.tif"}, (139.6, 35.6, 139.65, 35.65)),
            ({"IMG_ID": "N", "SENSOR": "S1", "OFF_NADIR": 1, "AZ": 50, "PATH": "\\\\nas\\images\\2026\\A.TIF"}, (139.6, 35.5, 139.8, 35.7)),
            ({"IMG_ID": "R", "SENSOR": "SAR-X", "OFF_NADIR": 30, "AZ": 260, "PATH": "\\\\nas\\images\\2026\\d.tif"}, (139.9, 35.5, 140.1, 35.7)),
    ):
        layer.add({"ACQ": 0, **attrs}, ring(*box))
    dock = p.api.image_catalog
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    searched(dock)
    canvas = iface.canvas
    project_crs = QgsProject.instance().crs()
    canvas.setDestinationCrs(WGS84)
    QgsProject.instance().setCrs(WGS84)
    canvas.setRotation(0)
    yield dock, canvas, {r.id: r for r in dock.records}
    # The project's CRS as it was (the next tests of this process read the map in it).
    QgsProject.instance().removeAllMapLayers()
    QgsProject.instance().setCrs(project_crs)


def test_off_by_default_nothing_changes(opened):
    dock, canvas, records = opened
    assert not settings.match_crs() and settings.map_orientation() == "north"
    assert not dock.match_crs.isChecked() and dock.orientation_box.currentData() == "north"
    [layer] = dock.open_records([records["A"]])
    assert canvas.mapSettings().destinationCrs() == WGS84 and canvas.rotation() == 0
    assert not dock.orienter.arrow.isVisibleTo(canvas)


def test_the_map_in_the_images_crs_turned_to_the_look_and_put_back_when_closed(opened):
    """A in degrees: the UTM zone of its middle; the catalog's 方位角 100° (the satellite to the east): west up."""
    dock, canvas, records = opened
    dock.match_crs.setChecked(True)
    dock.orientation_box.setCurrentIndex(dock.orientation_box.findData("look"))
    assert settings.match_crs() and settings.map_orientation() == "look"
    [layer] = dock.open_records([records["A"]])
    assert canvas.mapSettings().destinationCrs().authid() == "EPSG:32654"
    assert QgsProject.instance().crs().authid() == "EPSG:32654"
    # The look (from the satellite, 100° + 180°) points straight up; true north (not the grid's) shows 80° clockwise.
    assert abs(screen_way(canvas, 139.7, 35.6, 280)) < 0.2
    assert abs(screen_way(canvas, 139.7, 35.6, 0) - 80) < 0.2
    assert dock.status.text() == "地図: WGS 84 / UTM zone 54N、撮像方向が上（回転 79°、カタログの方位角から）", dock.status.text()
    assert dock.orienter.arrow.isVisibleTo(canvas)
    assert abs(dock.orienter.arrow.north_angle() - 80) < 0.5
    # The whole image in the turned view (「開いた画像へ移動」).
    polygon = canvas.mapSettings().visiblePolygon()
    to_map = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
    from qgis.core import QgsGeometry

    view = QgsGeometry.fromPolygonXY([[QgsPointXY(q.x(), q.y()) for q in [*polygon, polygon[0]]]])
    for lon, lat in ((139.6, 35.5), (139.8, 35.5), (139.8, 35.7), (139.6, 35.7)):
        assert view.contains(QgsGeometry.fromPointXY(to_map.transform(QgsPointXY(lon, lat)))), (lon, lat)
    # Opened again (the layer chosen): said under that. (The map's grid north is 0.8° off true north there.)
    dock.open_records([records["A"]])
    assert dock.status.text().split("\n")[0] == "A は開いている画像。そのレイヤーを選択"
    assert "撮像方向が上（回転 79°" in dock.status.text().split("\n")[1]
    # The image closed: the map as it was.
    QgsProject.instance().removeMapLayer(layer.id())
    assert canvas.mapSettings().destinationCrs() == WGS84 and canvas.rotation() == 0
    assert QgsProject.instance().crs() == WGS84
    assert not dock.orienter.arrow.isVisibleTo(canvas)


def test_the_satellite_up_the_flight_and_why_not(opened):
    dock, canvas, records = opened
    dock.orientation_box.setCurrentIndex(dock.orientation_box.findData("satellite"))
    [a] = dock.open_records([records["A"]])
    # Turned without 「投影法を画像に合わせる」: a map in degrees still goes to UTM (it would skew).
    assert canvas.mapSettings().destinationCrs().authid() == "EPSG:32654"
    assert abs(screen_way(canvas, 139.7, 35.6, 100)) < 0.2
    # The flight of a radar (its sensor's name): right-looking, so 90° on from the satellite (260° → 350°).
    dock.orientation_box.setCurrentIndex(dock.orientation_box.findData("flight"))
    [r] = dock.open_records([records["R"]])
    assert abs(screen_way(canvas, 140.0, 35.6, 350)) < 0.2
    assert "観測の向きから" in dock.status.text()
    # Not a radar, nothing in its file: the flight is not known, the map north up again.
    QgsProject.instance().removeMapLayer(r.id())
    QgsProject.instance().removeMapLayer(a.id())
    assert canvas.rotation() == 0 and canvas.mapSettings().destinationCrs() == WGS84
    [a] = dock.open_records([records["A"]])
    assert canvas.rotation() == 0 and "進行方向がわからない" in dock.status.text()
    # Optical from nearly straight down (off-nadir 1°): north up, said why.
    dock.orientation_box.setCurrentIndex(dock.orientation_box.findData("look"))
    QgsProject.instance().removeMapLayer(a.id())
    [n] = dock.open_records([records["N"]])
    assert canvas.rotation() == 0 and "ほぼ真下" in dock.status.text()


def test_a_projected_image_keeps_its_crs_and_the_first_of_several_decides(opened):
    dock, canvas, records = opened
    dock.match_crs.setChecked(True)
    layers = dock.open_records([records["U"], records["D"]])
    assert len(layers) == 2
    assert canvas.mapSettings().destinationCrs().authid() == "EPSG:32654" and canvas.rotation() == 0
    assert "WGS 84 / UTM zone 54N" in dock.status.text()
    # Put back only when both are closed.
    QgsProject.instance().removeMapLayer(layers[0].id())
    assert canvas.mapSettings().destinationCrs().authid() == "EPSG:32654"
    QgsProject.instance().removeMapLayer(layers[1].id())
    assert canvas.mapSettings().destinationCrs() == WGS84


def test_one_after_another_and_the_persons_own_rotation_is_kept(opened):
    dock, canvas, records = opened
    dock.close_previous.setChecked(True)
    dock.orientation_box.setCurrentIndex(dock.orientation_box.findData("look"))
    dock.open_records([records["A"]])
    assert abs(screen_way(canvas, 139.7, 35.6, 280)) < 0.2
    # The next image (the previous one closes): turned for it, not put back in between.
    [d] = dock.open_records([records["D"]])
    from qgis.core import QgsRasterLayer

    assert len([x for x in QgsProject.instance().mapLayers().values() if isinstance(x, QgsRasterLayer)]) == 1
    assert abs(screen_way(canvas, 140.0, 35.6, 20)) < 0.2
    # North up by the arrow: the person's; closing then puts back only the CRS.
    dock.orienter.arrow.click()
    assert canvas.rotation() == 0
    canvas.setRotation(12)
    QgsProject.instance().removeMapLayer(d.id())
    assert canvas.rotation() == 12 and canvas.mapSettings().destinationCrs() == WGS84


def test_a_turned_maps_view_is_searched_as_it_is(opened, plugin):  # noqa: F811
    dock, canvas, records = opened
    *_, fake, _layer = plugin
    dock.orientation_box.setCurrentIndex(dock.orientation_box.findData("look"))
    dock.open_records([records["A"]])
    dock.area_box.setCurrentIndex(dock.area_box.findData("extent"))
    area = dock.area()
    assert isinstance(area, dict) and len(area["rings"][0]) == 5
    # Its corners: the turned view's (not the box around it).
    xs = sorted(round(p[0], 3) for p in area["rings"][0][:4])
    assert len(set(xs)) == 4


def test_the_files_own_directions(tmp_path):
    from qgis.core import QgsRasterLayer

    # Its metadata: the radar's incidence, heading and side.
    path = gcp_tiff(tmp_path / "sar.tif", {"INCIDENCE_ANGLE": "35", "PLATFORM_HEADING": "350", "LOOK_SIDE": "right"})
    layer = QgsRasterLayer(path, "sar")
    assert layer.isValid()
    f = file_directions(layer)
    assert f["radar"] and f["toward"] == 260 and f["heading"] == 350 and 25 < f["off_nadir"] < 35
    assert abs(f["rows"] - 180) < 1
    # Nothing said: a radar's (by its catalog) look from its columns (east, so the satellite west), its flight its rows.
    bare = QgsRasterLayer(gcp_tiff(tmp_path / "bare.tif"), "bare")
    d = directions_of(bare, None, None, "flight", read_file=lambda layer, radar: file_directions(layer, True))
    assert abs(d.toward - 270) < 1 and d.toward_from == "columns"
    assert abs(d.heading - 180) < 1 and d.heading_from == "rows"
    # Typed in クイック幾何補正 before the file (the catalog first).
    d = directions_of(bare, None, 123.0, "look", read_file=lambda *a: pytest.fail("the file read"))
    assert d.toward == 123.0 and d.toward_from == "typed"


def test_the_options_are_one_to_a_row_under_the_buttons(opened):
    dock, canvas, records = opened
    dock.resize(420, 900)
    dock.show()
    APP.processEvents()
    holder = dock.buttons["open"].parentWidget()
    options = [dock.close_previous, dock.zoom_opened, dock.match_crs, dock.orientation_row, dock.footprint_box.parentWidget()]
    assert all(o.parentWidget() is not holder for o in options)
    tops = [o.mapTo(dock, o.rect().topLeft()).y() for o in options]
    assert tops == sorted(tops) and len(set(tops)) == len(tops)
    assert tops[0] > max(b.mapTo(dock, b.rect().bottomLeft()).y() for b in dock.buttons.values() if b.isVisible())
    # Not shown for a catalog without image files.
    catalog = dock._shown_catalog
    catalog.image_files = False
    dock._show_file_buttons()
    assert dock.match_crs.isHidden() and dock.orientation_row.isHidden()
    catalog.image_files = True
    dock._show_file_buttons()


def test_the_enhance_docks_buttons_turn_the_map_for_its_image(opened, plugin):  # noqa: F811
    dock, canvas, records = opened
    p, iface, *_ = plugin
    enhance = p.api.image_enhance
    row = enhance.orient_row
    # One orienter for the map: one north arrow, one map to put back.
    from mikage.ui.map_orient import NorthArrow

    assert row.orienter is dock.orienter and len(canvas.findChildren(NorthArrow)) == 1
    # No image worked on: only 北が上.
    enhance.follow_layer(None)
    assert [b.isEnabled() for b in row.buttons.values()] == [True, False, False, False]
    assert row.buttons["north"].isChecked()
    [a] = dock.open_records([records["A"]])
    assert canvas.rotation() == 0
    iface.setActiveLayer(a)
    assert enhance.layer is a and all(b.isEnabled() for b in row.buttons.values())
    # 撮: the catalog's 方位角 100° → west up, the map in degrees put in UTM.
    row.buttons["look"].click()
    assert canvas.mapSettings().destinationCrs().authid() == "EPSG:32654"
    assert abs(screen_way(canvas, 139.7, 35.6, 280)) < 0.2
    assert row.note.text() == "回転 79°（撮像方向が上、カタログの方位角から）。地図を WGS 84 / UTM zone 54N に", row.note.text()
    assert [w for w, b in row.buttons.items() if b.isChecked()] == ["look"]
    # 衛: the satellite up; the map already in UTM.
    row.buttons["satellite"].click()
    assert abs(screen_way(canvas, 139.7, 35.6, 100)) < 0.2
    assert row.note.text() == "回転 −101°（衛星が上、カタログの方位角から）", row.note.text()
    assert [w for w, b in row.buttons.items() if b.isChecked()] == ["satellite"]
    # 進: not known for this image: the map left as it is, said why.
    row.buttons["flight"].click()
    assert abs(screen_way(canvas, 139.7, 35.6, 100)) < 0.2
    assert row.note.text().startswith("回さなかった: 進行方向がわからない")
    assert [w for w, b in row.buttons.items() if b.isChecked()] == ["satellite"]
    # Turned by hand: no button down, nothing said.
    canvas.setRotation(33)
    assert not any(b.isChecked() for b in row.buttons.values()) and row.note.isHidden()
    # N: north up, the CRS as it is.
    row.buttons["north"].click()
    assert canvas.rotation() == 0 and canvas.mapSettings().destinationCrs().authid() == "EPSG:32654"
    assert [w for w, b in row.buttons.items() if b.isChecked()] == ["north"]
    # Turned again, then the image closed: the map as it was before (degrees, north up), said in both docks.
    row.buttons["look"].click()
    QgsProject.instance().removeMapLayer(a.id())
    assert canvas.mapSettings().destinationCrs() == WGS84 and canvas.rotation() == 0
    assert row.note.text() == dock.status.text() == "開いた画像を閉じたので、地図の投影法と向きを元に戻した"
    assert row.buttons["north"].isChecked()


def test_the_enhance_row_sits_under_the_target_and_the_icons(opened, plugin):  # noqa: F811
    from qgis.PyQt.QtGui import QColor

    from mikage.ui.orient_icons import LOOKS, orient_pixmap

    p, *_ = plugin
    enhance = p.api.image_enhance
    enhance.resize(360, 700)
    enhance.show()
    APP.processEvents()
    label, row = enhance.layer_label, enhance.orient_row
    # Under 対象, out of the scrolling part: there however far the dock is scrolled.
    assert row.mapTo(enhance, row.rect().topLeft()).y() > label.mapTo(enhance, label.rect().topLeft()).y()
    from qgis.PyQt.QtWidgets import QScrollArea

    inner = next(w for w in _ancestors(enhance.enabled) if isinstance(w, QScrollArea))
    assert inner not in list(_ancestors(row))
    # Icons in a row (the dock wide enough), each with its full name in the tooltip.
    tops = {b.mapTo(enhance, b.rect().topLeft()).y() for b in row.buttons.values()}
    assert len(tops) == 1
    assert row.buttons["look"].toolTip().startswith("撮像方向が上（衛星が下）")
    # A thick arrow of its colour, a white letter in it.
    for way, (_letter, colour) in LOOKS.items():
        image = orient_pixmap(way, 32).toImage()
        assert image.pixelColor(16, 6) == QColor(colour), way
        assert image.pixelColor(2, 28).alpha() == 0
        assert any(image.pixelColor(x, y).lightness() > 240 for x in range(8, 24) for y in range(14, 30)), way


def _ancestors(widget):
    w = widget.parentWidget()
    while w is not None:
        yield w
        w = w.parentWidget()


def test_one_orienter_for_a_map_until_the_last_dock_lets_go():
    from qgis.gui import QgsMapCanvas

    from mikage.ui.map_orient import NorthArrow, release_orienter, shared_orienter

    canvas = QgsMapCanvas()
    catalog, enhance = object(), object()
    o = shared_orienter(canvas, catalog)
    assert shared_orienter(canvas, enhance) is o and len(canvas.findChildren(NorthArrow)) == 1
    release_orienter(o, catalog)
    assert shared_orienter(canvas, catalog) is o
    release_orienter(o, catalog)
    release_orienter(o, enhance)
    APP.processEvents()
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not canvas.findChildren(NorthArrow)
    again = shared_orienter(canvas, catalog)
    assert again is not o
    release_orienter(again, catalog)
    # A dock deleted without letting go: let go then (no orienter left answering the project for it).
    from qgis.PyQt.QtWidgets import QWidget

    dock = QWidget()
    held = shared_orienter(canvas, dock)
    dock.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not held.users and shared_orienter(canvas, catalog) is not held
    release_orienter(shared_orienter(canvas, catalog), catalog)


def test_the_toolbar_of_its_own_turns_the_map_and_switches_the_correction_and_dra(opened, plugin):  # noqa: F811
    from qgis.PyQt.QtWidgets import QToolBar

    from mikage.qgis_io import enhance_renderer as er

    dock, canvas, records = opened
    p, iface, *_ = plugin
    enhance = p.api.image_enhance
    bars = {b.objectName(): b for b in iface.mainWindow().findChildren(QToolBar)}
    # A toolbar of its own (not MIKAGE's): N 撮 衛 進, then 補正 and DRA.
    assert "MikageOrientToolbar" in bars and "MikageToolbar" in bars
    bar = p.orient_toolbar
    assert bar.toolbar is bars["MikageOrientToolbar"] and bar.toolbar.windowTitle() == "地図の向き・補正"
    assert [a.objectName() for a in bar.toolbar.actions() if a.objectName()] == [
        "MikageOrientAction_north", "MikageOrientAction_look", "MikageOrientAction_satellite", "MikageOrientAction_flight"]
    assert not any(a.objectName().startswith("MikageOrientAction") for a in bars["MikageToolbar"].actions())
    # No image worked on: only 北が上; the boxes off and greyed.
    enhance.follow_layer(None)
    assert [a.isEnabled() for a in bar.actions.values()] == [True, False, False, False]
    assert not bar.enhance_box.isEnabled() and not bar.dra_box.isEnabled()
    [a] = dock.open_records([records["A"]])
    iface.setActiveLayer(a)
    assert all(x.isEnabled() for x in bar.actions.values()) and bar.enhance_box.isEnabled()
    # 撮 on the toolbar: turned as by the dock's button, both shown down.
    bar.actions["look"].trigger()
    assert abs(screen_way(canvas, 139.7, 35.6, 280)) < 0.2
    assert [w for w, x in bar.actions.items() if x.isChecked()] == ["look"]
    assert enhance.orient_row.buttons["look"].isChecked()
    # The dock's 衛: the toolbar follows.
    enhance.orient_row.buttons["satellite"].click()
    assert [w for w, x in bar.actions.items() if x.isChecked()] == ["satellite"]
    # Not known: the map as it is, said in the message bar (the dock may be closed).
    bar.actions["flight"].trigger()
    assert [w for w, x in bar.actions.items() if x.isChecked()] == ["satellite"]
    assert "進行方向がわからない" in iface.messageBar().currentItem().text()
    # DRA on: the correction too (DRA is a step of it), drawn on the image.
    assert not bar.enhance_box.isChecked() and not bar.dra_box.isChecked()
    bar.dra_box.click()
    assert enhance.dra_on.isChecked() and enhance.enabled.isChecked() and bar.enhance_box.isChecked()
    enhance.apply()
    r = er.enhance_renderer(a)
    assert r is not None and r.settings.pipeline["ops"][0]["op"] == "autoStretch"
    # 補正 off on the toolbar: QGIS's own drawing again; the dock's box follows, and the toolbar the dock's.
    bar.enhance_box.click()
    enhance.apply()
    assert not enhance.enabled.isChecked() and er.enhance_renderer(a) is None
    enhance.enabled.setChecked(True)
    assert bar.enhance_box.isChecked()
    # Roles without 画像補正: put away.
    bar.set_allowed(False)
    assert bar.toolbar.isHidden() and not bar.toolbar.toggleViewAction().isVisible()
    bar.set_allowed(True)
    assert bar.toolbar.toggleViewAction().isVisible()

