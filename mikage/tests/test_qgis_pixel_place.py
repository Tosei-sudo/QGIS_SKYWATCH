"""
Where a record is in its image's file (観測記録の PIXEL_X・PIXEL_Y・IMAGE_FILE,
qgis_io/pixel_place.py; tsudo 2026-11-19): the file's own pixel as QGIS
places the image (geotransform, GCPs, RPC), the same on an image moved by
位置ずらし, outside the image too (below 0 or past its size), off the main
thread; the records made, moved and carried over in the dock; AI training
placing their boxes by it.
"""
import json
import threading

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPoint, QgsPointXY, QgsProject, QgsRasterDataProvider, QgsRasterLayer,
    QgsSettings,
)

from mikage.qgis_io import pixel_place, settings, shift_io  # noqa: E402
from mikage.qgis_io.obs_images import IMAGE_FILE, IMAGE_ID, file_name, observed_image  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
TO_IMAGE = QgsRasterDataProvider.TransformType.TransformLayerToImage


def _srs(epsg: int) -> str:
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s.ExportToWkt()


def geo_image(path, west=139.6, north=35.7, size=(400, 300), step=0.0005, epsg=4326):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), size[0], size[1], 1, gdal.GDT_Byte)
    ds.SetGeoTransform([west, step, 0, north, 0, -step])
    ds.SetProjection(_srs(epsg))
    ds.GetRasterBand(1).WriteArray((np.arange(size[0] * size[1]) % 251).reshape(size[1], size[0]).astype(np.uint8))
    ds = None
    return str(path)


def gcp_image(path):
    """An image placed by GCPs (as SAR L1): slanted, not a north-up grid."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 400, 300, 1, gdal.GDT_Byte)
    gcps = [gdal.GCP(139.6 + px * 5e-4 + ln * 1e-4, 35.7 - ln * 5e-4 + px * 1e-4, 0, px, ln)
            for px, ln in ((0, 0), (400, 0), (0, 300), (400, 300), (200, 150))]
    ds.SetGCPs(gcps, _srs(4326))
    ds.GetRasterBand(1).Fill(90)
    ds = None
    return str(path)


def rpc_image(path):
    """An image placed by an RPC model (as optical L1), bent a little (not affine)."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 400, 300, 1, gdal.GDT_Byte)

    def coeffs(**terms):
        names = ["1", "L", "P", "H", "LP", "LH", "PH", "LL", "PP", "HH", "PLH", "LLL", "LPP", "LHH", "LLP", "PPP", "PHH", "LLH", "PPH", "HHH"]
        return " ".join(str(terms.get(n, 0.0)) for n in names)

    ds.SetMetadata({
        "LINE_OFF": "150", "SAMP_OFF": "200", "LAT_OFF": "35.6", "LONG_OFF": "139.7", "HEIGHT_OFF": "10",
        "LINE_SCALE": "150", "SAMP_SCALE": "200", "LAT_SCALE": "0.075", "LONG_SCALE": "0.1", "HEIGHT_SCALE": "100",
        "LINE_NUM_COEFF": coeffs(P=-1.0, LP=0.05), "LINE_DEN_COEFF": coeffs(**{"1": 1.0}),
        "SAMP_NUM_COEFF": coeffs(L=1.0, PP=0.03), "SAMP_DEN_COEFF": coeffs(**{"1": 1.0}),
    }, "RPC")
    ds.GetRasterBand(1).Fill(120)
    ds = None
    return str(path)


def qgis_pixel(layer, lon, lat):
    """QGIS's own answer: the layer's provider (on the main thread)."""
    p = QgsCoordinateTransform(WGS84, layer.crs(), QgsProject.instance()).transform(QgsPointXY(lon, lat))
    q = layer.dataProvider().transformCoordinates(QgsPoint(p.x(), p.y()), TO_IMAGE)
    return q.x(), q.y()


@pytest.mark.parametrize("make", [geo_image, gcp_image, rpc_image])
def test_finds_the_files_pixel_as_qgis_places_the_image_moved_or_not(tmp_path, make):
    path = make(tmp_path / "img.tif")
    layer = QgsRasterLayer(path, "img")
    assert layer.isValid()
    at = (139.66, 35.64)
    placement = pixel_place.placement_of(layer)
    assert placement is not None and placement.file == "img.tif" and (placement.width, placement.height) == (400, 300)
    x, y = placement.pixel(*at)
    assert (x, y) == pytest.approx(qgis_pixel(layer, *at), abs=1e-6)
    assert 0 < x < 400 and 0 < y < 300
    # And back: the place of that pixel (an RPC model's inverse is found to 0.1 pixel, 1e-4° here).
    assert placement.place(x, y) == pytest.approx(at, abs=1e-4 if make is rpc_image else 1e-9)
    # Moved by 位置ずらし: the thing (now 30 m east and 20 m north on the map) is still that pixel of the file.
    dx, dy = 3e-4, 2e-4
    shift_io.set_shift(layer, dx, dy, folder=str(tmp_path))
    moved = pixel_place.placement_of(layer)
    assert moved.file == "img.tif"
    assert moved.pixel(at[0] + dx, at[1] + dy) == pytest.approx((x, y), abs=1e-6)
    assert moved.pixel(at[0] + dx, at[1] + dy) == pytest.approx(qgis_pixel(layer, at[0] + dx, at[1] + dy), abs=1e-6)
    # The same place on the map is another pixel of it now.
    assert moved.pixel(*at)[0] != pytest.approx(x, abs=0.1)


@pytest.mark.parametrize("make", [geo_image, rpc_image])
def test_an_image_not_read_here_is_placed_as_qgis_places_it(tmp_path, make, monkeypatch):
    """A URL is not opened again on the main thread: QGIS's own placing (a geotransform, else a grid of it; its RPC inverse is found to 0.1 pixel)."""
    layer = QgsRasterLayer(make(tmp_path / "u.tif"), "u")
    monkeypatch.setattr(pixel_place, "gdal_path", lambda _layer: "/vsicurl/https://example.com/u.tif")
    placement = pixel_place.placement_of(layer)
    assert (placement.geotransform is not None) == (make is geo_image) and placement.method == ("" if make is geo_image else "GCP_TPS")
    for at in ((139.66, 35.64), (139.71, 35.58)):
        assert placement.pixel(*at) == pytest.approx(qgis_pixel(layer, *at), abs=1e-6 if make is geo_image else 0.15)


def test_a_place_outside_the_image_gives_pixels_below_zero_or_past_its_size(tmp_path):
    """tsudo 2026-11-19: 「誤って画像範囲外に作った場合にクラッシュしないように。とりあえずマイナス値とかでいいよ」"""
    layer = QgsRasterLayer(geo_image(tmp_path / "a.tif"), "a")
    p = pixel_place.placement_of(layer)
    assert p.pixel(139.5995, 35.7005) == pytest.approx((-1.0, -1.0))
    x, y = p.pixel(139.85, 35.5)
    assert x == pytest.approx(500) and y == pytest.approx(400)
    # Far off the model of an RPC image, or outside what the CRS reaches: none, never an error.
    rpc = pixel_place.placement_of(QgsRasterLayer(rpc_image(tmp_path / "r.tif"), "r"))
    for lon, lat in ((-170.0, -80.0), (float("nan"), 35.6)):
        found = rpc.pixel(lon, lat)
        assert found is None or all(np.isfinite(found))
    utm = QgsRasterLayer(geo_image(tmp_path / "u.tif", west=380000, north=3950000, step=10, epsg=32654), "u")
    assert pixel_place.placement_of(utm).pixel(139.7, 35.6) is not None
    assert pixel_place.pixel_in([pixel_place.placement_of(utm)], float("inf"), 0) is None


def test_finds_pixels_off_the_main_thread_with_what_was_read_on_it(tmp_path):
    layer = QgsRasterLayer(gcp_image(tmp_path / "g.tif"), "g")
    placement = pixel_place.placement_of(layer)
    expected = qgis_pixel(layer, 139.65, 35.66)
    got = []
    worker = threading.Thread(target=lambda: got.append(placement.pixel(139.65, 35.66)))
    worker.start()
    worker.join()
    assert got[0] == pytest.approx(expected, abs=1e-6)


def test_an_image_moved_keeps_its_name_and_time(tmp_path):
    """Before 2026-11-19 an image outside the catalog moved by 位置ずらし was 「<uuid>.vrt」 without its time."""
    path = geo_image(tmp_path / "SCENE_20261003T010000.tif")
    layer = QgsRasterLayer(path, "s")
    before = observed_image(layer)
    shift_io.set_shift(layer, 1e-4, 0, folder=str(tmp_path))
    assert layer.source().endswith(".vrt")
    after = observed_image(layer)
    assert file_name(layer) == "SCENE_20261003T010000.tif"
    assert (after.id, after.name, after.time) == (before.id, before.name, before.time) and before.time is not None


def test_a_catalog_records_file_below_its_folder_is_the_image_file(tmp_path):
    layer = QgsRasterLayer(geo_image(tmp_path / "b.tif"), "b")
    layer.setCustomProperty(IMAGE_ID, "SCENE_A")
    layer.setCustomProperty(IMAGE_FILE, "IMG/b.tif")
    assert pixel_place.placement_of(layer).file == "IMG/b.tif"


# ---- in the dock ----

URL = "https://example.com/arcgis/rest/services/Obs/FeatureServer/0"
FIELDS = ["OBS_ID", "IMAGE_ID", "IMAGE_NAME", "IMAGE_TIME", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE",
          "LENGTH_M", "HEADING", "CONFIDENCE", "OBSERVER", "UPDATED_BY", "CREATED_AT", "UPDATED_AT", "DELETED_AT"]
TYPES = {"IMAGE_TIME": "esriFieldTypeDate", "CREATED_AT": "esriFieldTypeDate", "UPDATED_AT": "esriFieldTypeDate",
         "DELETED_AT": "esriFieldTypeDate", "PRESENT": "esriFieldTypeSmallInteger", "LENGTH_M": "esriFieldTypeDouble",
         "HEADING": "esriFieldTypeDouble", "PIXEL_X": "esriFieldTypeDouble", "PIXEL_Y": "esriFieldTypeDouble"}


def small(path, day):
    """20 × 20 pixels of 0.01° from 139.6, 35.7, taken on 2026-10-0<day>."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 20, 20, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([139.6, 0.01, 0, 35.7, 0, -0.01])
    ds.SetProjection(_srs(4326))
    ds.SetMetadataItem("ACQUISITION_START_TIME", f"2026-10-0{day}T01:00:00Z")
    ds.GetRasterBand(1).Fill(100)
    ds = None
    return str(path)


@pytest.fixture
def plugin(tmp_path):
    from mikage.tests.qgis_fakes import FakeIface

    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    iface = FakeIface()
    p = small_plugin(iface, Observations)
    p.initGui()
    layers = [QgsRasterLayer(small(tmp_path / f"d{day}.tif", day), f"d{day}") for day in (1, 2)]
    QgsProject.instance().addMapLayers(layers)
    iface.canvas.setDestinationCrs(layers[0].crs())
    iface.canvas.setLayers(layers)
    iface.canvas.setExtent(layers[0].extent())
    dock = p.api.observation_dock
    dock.show()
    yield p, iface, dock, layers, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def _record(dock, lon, lat, name="あおば"):
    dock.set_active(True)
    dock.map_clicked(lon, lat, None)
    dock.object_input.setText(name)
    dock.kind_input.setText("船舶")
    dock.submit()


def test_a_record_on_a_moved_image_keeps_the_files_pixel_moved_or_carried(plugin):
    p, iface, dock, layers, tmp_path = plugin
    book = dock.book
    # d1 lay 0.02° west and 0.01° north of where it is: moved there by hand.
    shift_io.set_shift(layers[0], 0.02, -0.01, folder=str(tmp_path))
    dock.refresh_images()
    assert book.current.id == "d1.tif"
    # The ship at file pixel (10.5, 9.5) shows at 139.725, 35.595 on the map now.
    _record(dock, 139.725, 35.595)
    [a] = book.living()
    assert (a.image_id, a.lon, a.lat) == ("d1.tif", 139.725, 35.595)
    assert (a.image_file, a.pixel_x, a.pixel_y) == ("d1.tif", pytest.approx(10.5), pytest.approx(9.5))
    # Dragged a little: found again.
    dock.map_dragged(dock.scene().marks[0], 139.735, 35.595, True)
    assert book.record(a.id).pixel_x == pytest.approx(11.5)
    # Carried into d2 (not moved): its own pixel there.
    dock.step(1)
    dock.carry(a.object_id, True)
    [b] = book.here()
    assert (b.image_id, b.image_file, b.pixel_x, b.pixel_y) == ("d2.tif", "d2.tif", pytest.approx(13.5), pytest.approx(10.5))
    # Made by mistake outside the image: kept, its pixels below 0 (tsudo 2026-11-19).
    _record(dock, 139.595, 35.705, "外")
    out = next(o for o in book.here() if o.object_name == "外")
    assert (out.pixel_x, out.pixel_y) == (pytest.approx(-0.5), pytest.approx(-0.5))
    # In the layers made from the records and the export's sources.
    from mikage.core.obs.output import observation_layers

    [points, *_] = observation_layers(book.records, book.areas)
    row = next(values for _xy, values in points.features if values["obs_id"] == a.id)
    assert (row["pixel_x"], row["pixel_y"], row["image_file"]) == (pytest.approx(11.5), pytest.approx(9.5), "d1.tif")


def test_an_esri_store_keeps_the_pixel_where_it_has_the_attributes_and_is_quiet_without(plugin, tmp_path):
    from mikage.core.esri import EsriClient
    from mikage.tests.fake_esri import FakeEsri, FakeLayer
    from mikage.tests.qgis_fakes import wait_for

    p, iface, dock, layers, _ = plugin
    for fields, kept in ((FIELDS + ["PIXEL_X", "PIXEL_Y", "IMAGE_FILE"], True), (FIELDS, False)):
        fake = FakeEsri()
        layer = fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in fields}))
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
        settings.set_config_source(str(config))
        p.context.esri = lambda fake=fake: EsriClient(fake)
        p.context.reload()
        assert wait_for(lambda: dock.book is not None and dock.book.label == "共有" and dock.book.loaded)
        _record(dock, 139.705, 35.605, "共有のモノ")
        assert wait_for(lambda: len(layer.rows) == 1)
        assert wait_for(lambda: dock.status.text() == "共有のモノを記録")
        attributes = layer.rows[0]["attributes"]
        [mine] = dock.book.living()
        if kept:
            assert (attributes["PIXEL_X"], attributes["PIXEL_Y"], attributes["IMAGE_FILE"]) == (
                pytest.approx(10.5), pytest.approx(9.5), "d1.tif")
            assert (mine.pixel_x, mine.image_file) == (pytest.approx(10.5), "d1.tif")
        else:
            # A layer made before them: not sent, and the record read back is the one saved (not someone else's change).
            assert "PIXEL_X" not in attributes and (mine.pixel_x, mine.image_file) == (None, "")
        dock.refresh(force=True)
        assert wait_for(lambda: not dock._refreshing)
        assert mine.id not in dock.book.changed_by_others
        dock.close_form()
