"""
「クイック幾何補正」 in QGIS: a SAR image placed by GCPs corrected for the
terrain (a DEM: a file, or an ArcGIS image service's exportImage) and for
landmarks dragged to their places; the corrected image is GDAL's warp of the
file's pixels, so what is drawn, the pixels the records keep and
位置ずらし agree; the correction kept and put back on the image opened again;
the records made before moved.
"""
import json
import math
import os
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings  # noqa: E402

from mikage.core.geo_fix import DemSettings, GeoFix, TiePoint  # noqa: E402
from mikage.features.enhance import Enhance  # noqa: E402
from mikage.qgis_io import dem_io, geo_fix_io, geo_fix_keeper, settings, shift_io  # noqa: E402
from mikage.qgis_io.pixel_place import placement_of  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

W, H = 300, 200
LON0, LAT0 = 140.0, 35.5
HILL = 600.0
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def utm():
    s = osr.SpatialReference()
    s.ImportFromEPSG(32654)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def wgs():
    s = osr.SpatialReference()
    s.ImportFromEPSG(4326)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def to_utm(lon, lat):
    x, y, _ = osr.CoordinateTransformation(wgs(), utm()).TransformPoint(lon, lat)
    return x, y


def to_wgs(x, y):
    lon, lat, _ = osr.CoordinateTransformation(utm(), wgs()).TransformPoint(x, y)
    return lon, lat


def scene(folder):
    """A SAR image (10 m pixels, a little turned) placed by GCPs in UTM 54N at the ellipsoid; each pixel's value is its own (column + 1000 × row + 1)."""
    e0, n0 = to_utm(LON0, LAT0)
    turn = math.radians(5)

    def place(c, r):
        e, n = c * 10.0, -r * 10.0
        return e0 + e * math.cos(turn) - n * math.sin(turn), n0 + e * math.sin(turn) + n * math.cos(turn)

    path = os.path.join(folder, "sar.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, W, H, 1, gdal.GDT_Int32)
    rows, cols = np.mgrid[0:H, 0:W]
    ds.GetRasterBand(1).WriteArray((cols + 1000 * rows + 1).astype(np.int32))
    ds.SetGCPs([gdal.GCP(*place(W * i / 4, H * j / 4), 0, W * i / 4, H * j / 4) for i in range(5) for j in range(5)], utm().ExportToWkt())
    ds = None
    return path, place


def hill_dem(folder, centre):
    """The heights (WGS 84): a hill of HILL m (σ 400 m) at `centre`, flat (0) elsewhere."""
    lon_c, lat_c = centre
    cols, rows = 400, 400
    gt = (lon_c - 0.05, 0.1 / cols, 0, lat_c + 0.05, 0, -0.1 / rows)
    gy, gx = np.mgrid[0:rows, 0:cols]
    lon = gt[0] + (gx + 0.5) * gt[1]
    lat = gt[3] + (gy + 0.5) * gt[5]
    e = (lon - lon_c) * 111_320 * math.cos(math.radians(lat_c))
    n = (lat - lat_c) * 110_574
    h = HILL * np.exp(-(e * e + n * n) / (2 * 400.0 ** 2))
    path = os.path.join(folder, "dem.tif")
    dem_io.write_dem(path, h, gt)
    return path


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(shift_io, "shifts_folder", lambda: str(tmp_path))
    monkeypatch.setattr(geo_fix_io, "fix_folder", lambda: str(tmp_path))
    sar, place = scene(str(tmp_path))
    centre = to_wgs(*place(W / 2, H / 2))
    dem = hill_dem(str(tmp_path), centre)
    yield SimpleNamespace(sar=sar, dem=dem, centre=centre, place=place, folder=str(tmp_path))
    QgsProject.instance().removeAllMapLayers()


def metres(a, b):
    """East and north metres from a to b (WGS 84)."""
    return ((b[0] - a[0]) * 111_320 * math.cos(math.radians(a[1])), (b[1] - a[1]) * 110_574)


def moved(p, east, north):
    return p[0] + east / (111_320 * math.cos(math.radians(p[1]))), p[1] + north / 110_574


def same(a, b):
    """Tie points alike (as kept: to 8 decimals, a millimetre)."""
    return len(a) == len(b) and all(np.allclose(tuple(p.__dict__.values()), tuple(q.__dict__.values()), atol=1e-6) for p, q in zip(a, b))


SHOWN_WEST = HILL / math.tan(math.radians(35))


def test_the_terrain_correction_puts_the_hilltop_where_it_is_and_comes_off(files):
    layer = QgsRasterLayer(files.sar, "sar")
    crs = layer.crs()
    before = placement_of(layer)
    # The file shows the hilltop towards the satellite (west), by its height over tan(incidence).
    top = before.pixel(*moved(files.centre, -SHOWN_WEST, 0))
    flat = (10.0, 10.0)
    fix = GeoFix("sar", "sar.tif", terrain=True, toward=270.0, incidence=35.0)
    report = geo_fix_io.apply(layer, fix, files.dem)
    assert layer.isValid() and layer.source().startswith(files.folder) and layer.source().endswith(".vrt")
    assert layer.crs() == crs and shift_io.original_source(layer) == files.sar and geo_fix_io.fix_of(layer) == fix
    assert report.terrain and report.rpc_rms < 0.05 and report.residual is None
    # QGIS warps an image placed by GCPs with an alpha band; the corrected VRT has none (outside it: no data).
    assert layer.bandCount() == 1 and layer.renderer().alphaBand() == -1
    assert abs(report.largest_move - SHOWN_WEST) < 40 and "地形" in report.text()
    after = placement_of(layer)
    assert after.model_crs == WGS84
    e, n = metres(files.centre, after.place(*top))
    assert abs(e) < 3 and abs(n) < 3
    e, n = metres(before.place(*flat), after.place(*flat))
    assert abs(e) < 0.5 and abs(n) < 0.5
    # What is drawn is the file's pixel there: the warped image's pixel at the hilltop holds the pixel found for it.
    ds = gdal.Open(layer.source())
    gt = ds.GetGeoTransform()
    x, y = to_utm(*files.centre)
    col, row = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
    value = int(ds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)[0, 0]) - 1
    drawn = (value % 1000, value // 1000)
    centre = to_wgs(gt[0] + (col + 0.5) * gt[1], gt[3] + (row + 0.5) * gt[5])
    found = after.pixel(*centre)
    assert abs(drawn[0] - math.floor(found[0])) <= 1 and abs(drawn[1] - math.floor(found[1])) <= 1
    assert math.hypot(drawn[0] - top[0], drawn[1] - top[1]) < 2
    ds = None
    vrt, rpc = layer.source(), geo_fix_io.record_of(layer)["rpc"]
    geo_fix_io.remove(layer)
    assert layer.source() == files.sar and not geo_fix_io.is_fixed(layer) and layer.customProperty(shift_io.SOURCE) is None
    assert layer.crs() == crs and not os.path.exists(vrt) and not os.path.exists(rpc)
    assert layer.bandCount() == 2 and layer.renderer().alphaBand() == 2


def test_landmarks_and_a_shift_go_with_the_correction(files):
    layer = QgsRasterLayer(files.sar, "sar")
    before = placement_of(layer)
    # Two landmarks: the image is really 25 m east and 15 m south of where the file (on flat ground) put it.
    a, b = (20.0, 30.0), (280.0, 170.0)
    points = tuple(TiePoint(x, y, *moved(before.place(x, y), 25, -15)) for x, y in (a, b))
    fix = GeoFix("sar", "sar.tif", points=points, terrain=False)
    report = geo_fix_io.apply(layer, fix)
    assert report.kind == "平行移動・回転・縮尺" and not report.terrain and max(report.residuals) < 0.01
    after = placement_of(layer)
    for x, y in (a, b, (150.0, 100.0)):
        e, n = metres(before.place(x, y), after.place(x, y))
        assert abs(e - 25) < 0.3 and abs(n + 15) < 0.3
    # 位置ずらし on a corrected image: the same correction, moved (in the layer's CRS: metres).
    shift_io.set_shift(layer, 30, -20)
    assert geo_fix_io.is_fixed(layer) and shift_io.get_shift(layer) == (30, -20) and layer.isValid()
    p0, p1 = after.place(150, 100), placement_of(layer).place(150, 100)
    u0, u1 = to_utm(*p0), to_utm(*p1)
    assert abs(u1[0] - u0[0] - 30) < 0.3 and abs(u1[1] - u0[1] + 20) < 0.3
    geo_fix_io.remove(layer)
    # Taken off: where the file and the shift put it.
    assert shift_io.get_shift(layer) == (30, -20) and not geo_fix_io.is_fixed(layer) and layer.source().endswith(".vrt")
    u2 = to_utm(*placement_of(layer).place(150, 100))
    u3 = to_utm(*before.place(150, 100))
    assert abs(u2[0] - u3[0] - 30) < 0.01 and abs(u2[1] - u3[1] + 20) < 0.01


def test_the_dem_is_read_from_an_image_service_in_tiles_and_kept(tmp_path):
    url = "https://gis.example/arcgis/rest/services/DEM/ImageServer"
    asked, posted = [], []

    def ask(u, params):
        asked.append(u)
        return {"pixelSizeX": 1 / 3600, "pixelSizeY": 1 / 3600, "spatialReference": {"wkid": 4326}, "maxImageWidth": 300, "maxImageHeight": 300}

    def post(u, form):
        posted.append((u, form))
        west, south, east, north = (float(v) for v in form["bbox"].split(","))
        cols, rows = (int(v) for v in form["size"].split(","))
        gt = (west, (east - west) / cols, 0, north, 0, -(north - south) / rows)
        gy, gx = np.mgrid[0:rows, 0:cols]
        lon, lat = gt[0] + (gx + 0.5) * gt[1], gt[3] + (gy + 0.5) * gt[5]
        values = (lon - 139) * 1000 + (lat - 35) * 500
        values[:2, :2] = float(form["noData"])
        name = str(tmp_path / f"tile{len(posted)}.tif")
        ds = gdal.GetDriverByName("GTiff").Create(name, cols, rows, 1, gdal.GDT_Float32)
        ds.SetGeoTransform(gt)
        ds.SetProjection(wgs().ExportToWkt())
        ds.GetRasterBand(1).SetNoDataValue(float(form["noData"]))
        ds.GetRasterBand(1).WriteArray(values.astype(np.float32))
        ds = None
        with open(name, "rb") as f:
            return f.read()

    dem = DemSettings(url=url)
    box = (139.10, 35.10, 139.25, 35.20)
    path = dem_io.fetch_dem(dem, box, ask, post, "TOKEN", folder=str(tmp_path))
    assert asked == [url] and len(posted) > 1 and all(u == f"{url}/exportImage" for u, _ in posted)
    form = posted[0][1]
    assert (form["f"], form["format"], form["pixelType"], form["bboxSR"], form["imageSR"]) == ("image", "tiff", "F32", "4326", "4326")
    assert form["renderingRule"] == '{"rasterFunction":"None"}' and form["token"] == "TOKEN"
    assert max(int(v) for _, f in posted for v in f["size"].split(",")) <= 300
    heights = dem_io.Heights(path)
    west, south, east, north = box
    lon, lat = np.meshgrid(np.linspace(west, east, 7), np.linspace(south, north, 5))
    assert np.allclose(heights.at(lon, lat), (lon - 139) * 1000 + (lat - 35) * 500, atol=0.01)
    low, high = heights.range_over(box)
    assert 149.9 < low < 150.1 and 349.9 < high < 350.1
    # Asked again for the area: the file kept.
    assert dem_io.fetch_dem(dem, box, ask, lambda *_: pytest.fail("read again"), folder=str(tmp_path)) == path
    # An error answer, or a file set instead of a service.
    with pytest.raises(dem_io.DemError, match="標高を返さない"):
        dem_io.fetch_dem(dem, (10, 10, 10.1, 10.1), ask, lambda *_: b'{"error": {"code": 400, "message": "Invalid"}}', folder=str(tmp_path))
    assert dem_io.fetch_dem(DemSettings(file=path), box, ask, post) == path
    with pytest.raises(dem_io.DemError, match="見つからない"):
        dem_io.fetch_dem(DemSettings(file=str(tmp_path / "none.tif")), box, ask, post)


@pytest.fixture
def plugin(files, tmp_path, monkeypatch):
    monkeypatch.setattr(geo_fix_keeper, "local_path", lambda: str(tmp_path / "image_corrections.json"))
    monkeypatch.setenv("USERNAME", "tsudo")
    QgsSettings().remove("mikage")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"dem": {"file": files.dem}}), encoding="utf-8")
    settings.set_config_source(str(config))
    iface = FakeIface()
    p = small_plugin(iface, Enhance)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    layer = QgsRasterLayer(files.sar, "sar")
    QgsProject.instance().addMapLayers([layer])
    canvas = iface.canvas
    canvas.setDestinationCrs(layer.crs())
    canvas.setLayers([layer])
    canvas.resize(400, 400)
    canvas.setExtent(layer.extent())
    iface.setActiveLayer(layer)
    yield p, iface, layer
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    settings.set_config_source("")
    QgsSettings().remove("mikage")


def test_the_tool_corrects_the_active_image_keeps_it_and_puts_it_back(plugin, files, tmp_path):
    p, iface, layer = plugin
    tool = p.geo_fix
    tool.set_active(True)
    assert tool.layer is layer and tool.fix.terrain and not geo_fix_io.is_fixed(layer)
    assert iface.canvas.mapTool() is tool.tool and tool.panel.isVisible()
    # The satellite's direction typed: corrected for the terrain once the DEM is read.
    tool.set_look(270.0, 35.0)
    assert wait_for(lambda: geo_fix_io.is_fixed(layer))
    assert geo_fix_io.record_of(layer)["dem"] == files.dem and tool.dirty
    # A landmark dragged 30 m east of where it is shown.
    to_map = QgsCoordinateTransform(WGS84, layer.crs(), QgsProject.instance())
    shown = placement_of(layer).place(60, 50)
    start = to_map.transform(QgsPointXY(*shown))
    end = to_map.transform(QgsPointXY(*moved(shown, 30, 0)))
    tool.add_point(start, end)
    assert len(tool.fix.points) == 1 and same(geo_fix_io.fix_of(layer).points, tool.fix.points)
    e, n = metres(shown, placement_of(layer).place(60, 50))
    assert abs(e - 30) < 0.5 and abs(n) < 0.5
    tool.undo()
    assert tool.fix.points == () and geo_fix_io.is_fixed(layer)
    tool.add_point(start, end)
    # Kept (on this PC: no imageCorrections), stamped.
    tool.save()
    assert not tool.dirty
    kept = json.loads((tmp_path / "image_corrections.json").read_text(encoding="utf-8"))["fixes"]
    assert len(kept) == 1 and kept[0]["imageId"] == "sar.tif" and kept[0]["by"] == "tsudo" and len(kept[0]["points"]) == 1
    tool.set_active(False)
    assert iface.canvas.mapTool() is not tool.tool
    # The image opened again: corrected as kept.
    again = QgsRasterLayer(files.sar, "again")
    QgsProject.instance().addMapLayers([again])
    assert wait_for(lambda: geo_fix_io.is_fixed(again))
    assert same(geo_fix_io.fix_of(again).points, tool.fix.points) and geo_fix_io.fix_of(again).toward == 270.0
    e, n = metres(placement_of(layer).place(60, 50), placement_of(again).place(60, 50))
    assert abs(e) < 0.05 and abs(n) < 0.05
    # This screen only: taken off, the kept one stays.
    iface.setActiveLayer(again)
    tool.set_active(True)
    assert tool.layer is again and not tool.dirty
    tool.take_off()
    assert not geo_fix_io.is_fixed(again) and p.geo_fix_keeper.known("sar.tif", "sar.tif") is not None
    tool.set_active(False)


def test_a_project_whose_corrected_image_lost_its_vrt_makes_it_again(plugin, files):
    p, iface, layer = plugin
    fix = GeoFix("sar.tif", "sar.tif", terrain=True, toward=270.0, incidence=35.0)
    geo_fix_io.apply(layer, fix, files.dem)
    record = geo_fix_io.record_of(layer)
    # As a project read on a PC without the VRT: the layer points at a file that is not there.
    source, kept = layer.source(), {key: layer.customProperty(key) for key in (geo_fix_io.FIX, shift_io.SOURCE)}
    QgsProject.instance().removeMapLayer(layer.id())
    geo_fix_io.forget_files(source)
    lost = QgsRasterLayer(source, "lost")
    for key, value in kept.items():
        lost.setCustomProperty(key, value)
    assert not lost.isValid()
    QgsProject.instance().addMapLayers([lost])
    assert wait_for(lambda: lost.isValid() and geo_fix_io.is_fixed(lost) and os.path.exists(lost.source()))
    assert geo_fix_io.fix_of(lost) == fix and geo_fix_io.record_of(lost)["dem"] == record["dem"]


def test_the_records_made_before_are_moved_to_the_corrected_image(plugin, files):
    p, iface, layer = plugin
    tool = p.geo_fix
    before = placement_of(layer)
    old = before.place(100, 80)
    other_image = SimpleNamespace(id="z", deleted_at=None, image_id="other", image_file="", pixel_x=100.0, pixel_y=80.0, lon=old[0],
                                  lat=old[1], recorded_by="tsudo")
    records = [
        SimpleNamespace(id="a", deleted_at=None, image_id="sar.tif", image_file="sar.tif", pixel_x=100.0, pixel_y=80.0, lon=old[0], lat=old[1],
                        recorded_by="tsudo"),
        # Without its pixels: from where the file showed it.
        SimpleNamespace(id="b", deleted_at=None, image_id="sar.tif", image_file="", pixel_x=None, pixel_y=None, lon=old[0], lat=old[1],
                        recorded_by="TSUDO"),
        SimpleNamespace(id="c", deleted_at=None, image_id="sar.tif", image_file="sar.tif", pixel_x=100.0, pixel_y=80.0, lon=old[0], lat=old[1],
                        recorded_by="ito"),
        SimpleNamespace(id="d", deleted_at=1, image_id="sar.tif", image_file="sar.tif", pixel_x=100.0, pixel_y=80.0, lon=old[0], lat=old[1],
                        recorded_by="tsudo"),
        other_image,
    ]
    moves_done = []
    book = SimpleNamespace(records=records, move_many=lambda moves: moves_done.append(dict(moves)) or SimpleNamespace(saved=list(moves), failed=0))
    dock = SimpleNamespace(book=book, do=lambda label, work, after: after(work(book)))
    tool.observations = lambda: dock
    tool.set_active(True)
    assert tool.records_to_move() == ({}, 0, 0)
    points = (TiePoint(100, 80, *moved(old, 40, 10)),)
    tool.fix = GeoFix("sar.tif", "sar.tif", points=points, terrain=False)
    tool._show()
    moves, own, others = tool.records_to_move()
    assert sorted(moves) == ["a", "b", "c"] and (own, others) == (2, 1)
    for place in moves.values():
        e, n = metres(old, place)
        assert abs(e - 40) < 0.2 and abs(n - 10) < 0.2
    tool.move_records(moves)
    assert [sorted(m) for m in moves_done] == [["a", "b"]]
    tool.move_records(moves, others=True)
    assert sorted(moves_done[-1]) == ["a", "b", "c"]
    tool.set_active(False)


def test_a_drag_on_the_map_adds_a_landmark_the_image_faint_meanwhile(plugin):
    from qgis.gui import QgsMapMouseEvent
    from qgis.PyQt.QtCore import QEvent, QPointF, Qt
    from qgis.PyQt.QtGui import QKeyEvent, QMouseEvent

    from mikage.ui.geo_fix import FAINT

    def at(kind, x, y):
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
        return QgsMapMouseEvent(canvas, QMouseEvent(kind, QPointF(x, y), Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier))

    p, iface, layer = plugin
    canvas = iface.canvas
    tool = p.geo_fix
    tool.set_active(True)
    tool.set_terrain(False)
    drag = tool.tool
    drag.canvasPressEvent(at(QEvent.Type.MouseButtonPress, 200, 200))
    assert layer.opacity() == FAINT
    drag.canvasMoveEvent(at(QEvent.Type.MouseMove, 210, 200))
    drag.canvasReleaseEvent(at(QEvent.Type.MouseButtonRelease, 220, 200))
    assert layer.opacity() == 1 and len(tool.fix.points) == 1 and geo_fix_io.is_fixed(layer) and len(tool._markers) == 1
    # Where the drag started is now shown where it ended.
    start = canvas.getCoordinateTransform().toMapCoordinates(200, 200)
    end = canvas.getCoordinateTransform().toMapCoordinates(220, 200)
    to_wgs84 = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance())
    p0 = to_wgs84.transform(end)
    pixel = tool.fix.points[0]
    e, n = metres((p0.x(), p0.y()), placement_of(layer).place(pixel.x, pixel.y))
    assert abs(e) < 0.5 and abs(n) < 0.5 and end.x() - start.x() > 0
    drag.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
    assert tool.fix.points == () and not geo_fix_io.is_fixed(layer) and tool._markers == []
    drag.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert not tool.active and canvas.mapTool() is not drag


def test_an_image_with_rpc_is_corrected_with_its_own_model_and_the_dem(files):
    from mikage.core.geo_fix import fit_rpc, rpc_pixel

    # An image whose RPC leans with the height (a satellite to the west): GDAL's pixels for a place and height.
    lon, lat, h = np.meshgrid(np.linspace(LON0 - 0.01, LON0 + 0.04, 9), np.linspace(LAT0 - 0.03, LAT0 + 0.01, 9), np.linspace(-50, 800, 4),
                              indexing="ij")
    x = (lon - LON0) * 9000 + h * 0.13 + 0.5
    y = (LAT0 + 0.01 - lat) * 9000 + 0.5
    items = fit_rpc(lon, lat, h, x, y).items
    path = os.path.join(files.folder, "rpc.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 400, 350, 1, gdal.GDT_UInt16)
    ds.GetRasterBand(1).WriteArray(np.full((350, 400), 7, np.uint16))
    ds.SetMetadata({k: str(v) for k, v in items.items()}, "RPC")
    ds = None
    layer = QgsRasterLayer(path, "rpc")
    report = geo_fix_io.apply(layer, GeoFix("rpc.tif", "rpc.tif", terrain=True), files.dem)
    assert report.terrain and report.look is None and "撮像モデル（RPC）" in report.text()
    after = placement_of(layer)
    heights = dem_io.Heights(files.dem)
    for p in (files.centre, moved(files.centre, 500, -300), moved(files.centre, -1500, 800)):
        h = float(heights.at(np.array([p[0]]), np.array([p[1]]))[0])
        want = rpc_pixel(items, np.array([p[0]]), np.array([p[1]]), np.array([h]))
        got = after.pixel(*p)
        assert abs(got[0] - float(np.ravel(want[0])[0])) < 0.05 and abs(got[1] - float(np.ravel(want[1])[0])) < 0.05
    assert placement_of(layer).place(*after.pixel(*files.centre)) == pytest.approx(files.centre, abs=1e-6)


def test_after_the_panel_is_closed_and_its_image_closed_opening_images_raises_nothing(plugin, files, monkeypatch):
    """tsudo 2026-11-24: 「ウィンドウを閉じたら、それ以降画像を開くたびにPythonエラーが起きる」 (the closed image read by each open's refresh)."""
    import sys

    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: errors.append(a))
    p, iface, layer = plugin
    tool = p.geo_fix
    tool.set_active(True)
    assert tool.layer is layer
    tool.panel.close()
    assert not tool.active and not tool.panel.isVisible()
    # As the catalog opening the next image in the place of the one before.
    QgsProject.instance().removeMapLayer(layer.id())
    assert tool.layer is None and tool.saved() is None
    for i in range(3):
        again = QgsRasterLayer(files.sar, f"next{i}")
        QgsProject.instance().addMapLayers([again])
        iface.setActiveLayer(again)
        wait_for(lambda: False, 150)
    assert errors == [], errors
    # Opened again: on the active image.
    tool.set_active(True)
    assert tool.layer is again and tool.panel.isVisible() and tool.panel.image.text() == "画像: next2"
    # The image closed while the panel is open: the panel says to choose one, nothing raises.
    QgsProject.instance().removeMapLayer(again.id())
    wait_for(lambda: False, 150)
    assert tool.layer is None and errors == [] and tool.panel.image.text() == "ファイルの画像を選ぶと補正できる"
    tool.save()
    tool.take_off()
    assert tool.records_to_move() == ({}, 0, 0) and errors == []
    tool.set_active(False)
