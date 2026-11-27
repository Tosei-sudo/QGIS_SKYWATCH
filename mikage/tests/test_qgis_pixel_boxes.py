"""
観測記録をピクセル座標で書き出す (tsudo 2026-11-27) in QGIS: the records of
an image as their boxes in its file's pixels (centre, length, width, angle,
corners), written as GeoJSON (EPSG:4326) placed by the file's own
georeferencing: on a north-up image, on one moved by 位置ずらし (the pixel the
record kept; the polygon where the raw file puts it), on pixels not square
on the ground (SAR), on an image placed by GCPs; records left out; the file
opened on the map.
"""
import json
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,
    QgsProject, QgsRasterLayer, QgsVectorLayer,
)

from mikage.core.obs.model import Observation, default_taxonomy  # noqa: E402
from mikage.core.obs.taxonomy import Taxon  # noqa: E402
from mikage.processing_provider.pixel_box_algorithm import last_result  # noqa: E402

X0, Y0 = 380000, 3950000
UTM = QgsCoordinateReferenceSystem("EPSG:32654")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: UTM's grid north is off true north here (meridian convergence, under a degree): angles are checked to this.
GRID_DEGREES = 1.5


def utm_image(path, step_x=1.0, step_y=1.0, size=(400, 300)):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), size[0], size[1], 1, gdal.GDT_Byte)
    ds.SetGeoTransform([X0, step_x, 0, Y0, 0, -step_y])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32654)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray((np.arange(size[0] * size[1]) % 251).reshape(size[1], size[0]).astype(np.uint8))
    ds = None
    return str(path)


def lonlat(x, y, step_x=1.0, step_y=1.0):
    to = QgsCoordinateTransform(UTM, WGS84, QgsProject.instance())
    p = to.transform(QgsPointXY(X0 + x * step_x, Y0 - y * step_y))
    return p.x(), p.y()


def record(i, image, lon, lat, kind="船舶 > 支援船", **more):
    return Observation(f"r{i}", image, image, 1764201600000, f"o{i}", f"モノ{i}", kind, "停泊", True, "", lon, lat, 0, 0, **more)


class Book:
    """What the algorithm reads of the observation book."""

    def __init__(self, records):
        self.records = list(records)
        self.areas = []
        self.label = "テスト"

    def taxonomy(self):
        return default_taxonomy() + [Taxon(["船舶", "支援船"]), Taxon(["航空機", "輸送機"], specs={"全長": "24", "全幅": "6"})]


@pytest.fixture
def provider():
    from mikage.processing_provider import train_algorithm
    from mikage.processing_provider.provider import SatObsProvider

    p = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(p)
    yield train_algorithm
    train_algorithm.set_book(lambda: None)
    QgsApplication.processingRegistry().removeProvider(p)
    QgsProject.instance().clear()


def export(layers, path, **params):
    algorithm = QgsApplication.processingRegistry().algorithmById("mikage:records_to_pixel_boxes").create()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    results, ok = algorithm.run({"IMAGES": [l.id() for l in layers], "OUTPUT": str(path), **params}, context, QgsProcessingFeedback())
    assert ok
    with open(results["OUTPUT"], encoding="utf-8") as f:
        data = json.load(f)
    return data, {f["properties"]["obs_id"]: f for f in data["features"]}, context, algorithm


def centre_of(feature):
    ring = feature["geometry"]["coordinates"][0][:-1]
    return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)


def add(path, name="img"):
    layer = QgsRasterLayer(str(path), name)
    assert layer.isValid()
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_a_record_is_its_box_in_the_files_pixels_placed_by_the_file(provider, tmp_path):
    layer = add(utm_image(tmp_path / "a.tif"))
    lon, lat = lonlat(100.5, 150.5)
    provider.set_book(lambda: Book([record(1, "a.tif", lon, lat, length=40.0, heading=90.0, width=10.0,
                                           pixel_x=100.5, pixel_y=150.5, image_file="a.tif")]))
    data, by_id, context, _ = export([layer], tmp_path / "out.geojson")
    p = by_id["r1"]["properties"]
    assert (p["cx"], p["cy"]) == (100.5, 150.5)
    assert p["length_px"] == pytest.approx(40, abs=0.1) and p["width_px"] == pytest.approx(10, abs=0.1)
    assert p["angle"] == pytest.approx(90, abs=GRID_DEGREES)
    assert (p["length_m"], p["width_m"], p["heading"]) == (40.0, 10.0, 90.0)
    assert p["size_from"] == "記録（幅も）" and p["position_from"] == "記録時の画素"
    assert (p["image_id"], p["image_file"], p["image_width"], p["image_height"]) == ("a.tif", "a.tif", 400, 300)
    assert p["kind"] == "船舶 > 支援船" and p["object_name"] == "モノ1" and p["image_time"] == "2025-11-27T00:00:00Z"
    # Rear-left first: facing east, the rear is to the west and the left to the north (y smaller).
    rlx, rly, flx, fly, frx, fry, rrx, rry = p["corners_px"]
    assert rlx < 100.5 < flx and rly < 150.5 < rry
    # The polygon: those pixels by the file's georeferencing, on the record's place.
    assert centre_of(by_id["r1"]) == pytest.approx((lon, lat), abs=1e-7)
    ring = by_id["r1"]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    assert data["mikage"]["images"] == [{"image_id": "a.tif", "image_name": "img", "image_file": "a.tif", "width": 400, "height": 300,
                                         "georeferenced": True}]
    # The file is opened on the map when done, as EPSG:4326 polygons.
    out = str(tmp_path / "out.geojson")
    assert context.willLoadLayerOnCompletion(out)
    opened = QgsVectorLayer(out, "out", "ogr")
    assert opened.isValid() and opened.featureCount() == 1 and opened.crs().authid() == "EPSG:4326"
    # Its corners a list of numbers QGIS reads as one (shown in the attribute table).
    assert list(next(opened.getFeatures())["corners_px"]) == p["corners_px"]


def test_on_a_moved_image_the_box_is_where_the_thing_is_in_the_raw_file(provider, tmp_path):
    """
    A record made while the image was moved 30 m east and 20 m north keeps
    the file's pixel; the polygon is where the raw file puts that pixel (not
    the moved picture). A record without its pixel (older) is found from its
    place on the image as it lies now.
    """
    from mikage.qgis_io import shift_io

    layer = add(utm_image(tmp_path / "a.tif"))
    shift_io.set_shift(layer, 30, 20, folder=str(tmp_path))
    kept = record(1, "a.tif", *lonlat(130, 80), length=20.0, heading=0.0, pixel_x=100.0, pixel_y=100.0, image_file="a.tif")
    older = record(2, "a.tif", *lonlat(230, 180), length=20.0, heading=0.0)
    provider.set_book(lambda: Book([kept, older]))
    _, by_id, _, algorithm = export([layer], tmp_path / "out.geojson")
    one, two = by_id["r1"]["properties"], by_id["r2"]["properties"]
    assert (one["cx"], one["cy"], one["position_from"]) == (100.0, 100.0, "記録時の画素")
    assert (two["cx"], two["cy"]) == pytest.approx((200, 200), abs=0.01) and two["position_from"] == "記録の経緯度"
    assert one["angle"] == pytest.approx(0, abs=GRID_DEGREES) or one["angle"] == pytest.approx(360, abs=GRID_DEGREES)
    assert centre_of(by_id["r1"]) == pytest.approx(lonlat(100, 100), abs=1e-7)
    assert centre_of(by_id["r2"]) == pytest.approx(lonlat(200, 200), abs=1e-7)
    assert last_result().positions == {"記録時の画素": 1, "記録の経緯度": 1}


@pytest.mark.parametrize("heading, length_px, width_px", [(0.0, 10.0, 6.0), (90.0, 30.0, 2.0)])
def test_pixels_longer_on_the_ground_one_way_as_sar(provider, tmp_path, heading, length_px, width_px):
    """1 m across, 3 m along the image's y: a 30 × 6 m ship heading north is 10 × 6 px; heading east 30 × 2 px."""
    layer = add(utm_image(tmp_path / "s.tif", step_y=3.0))
    lon, lat = lonlat(200, 100, step_y=3.0)
    provider.set_book(lambda: Book([record(1, "s.tif", lon, lat, length=30.0, heading=heading, width=6.0)]))
    _, by_id, _, _ = export([layer], tmp_path / "out.geojson")
    p = by_id["r1"]["properties"]
    assert (p["cx"], p["cy"]) == pytest.approx((200, 100), abs=0.01)
    assert p["length_px"] == pytest.approx(length_px, rel=0.05) and p["width_px"] == pytest.approx(width_px, rel=0.1)
    assert min(abs(p["angle"] - heading), 360 - abs(p["angle"] - heading)) < GRID_DEGREES * 3


def test_an_image_placed_by_gcps(provider, tmp_path):
    """As SAR L1: a slanted grid of GCPs; the box is turned in the pixels as the ground is, the polygon on the record's place."""
    ds = gdal.GetDriverByName("GTiff").Create(str(tmp_path / "g.tif"), 400, 300, 1, gdal.GDT_Byte)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetGCPs([gdal.GCP(139.6 + px * 5e-5 + ln * 1e-5, 35.7 - ln * 5e-5 + px * 1e-5, 0, px, ln)
                for px, ln in ((0, 0), (400, 0), (0, 300), (400, 300), (200, 150))], srs.ExportToWkt())
    ds.GetRasterBand(1).Fill(90)
    ds = None
    layer = add(tmp_path / "g.tif")
    from mikage.qgis_io.pixel_place import cached_placement

    lon, lat = cached_placement(layer).place(150, 120)
    provider.set_book(lambda: Book([record(1, "g.tif", lon, lat, length=30.0, heading=0.0, width=8.0)]))
    _, by_id, _, _ = export([layer], tmp_path / "out.geojson")
    p = by_id["r1"]["properties"]
    assert (p["cx"], p["cy"]) == pytest.approx((150, 120), abs=0.01)
    # North on the ground is up and a little right in this grid (y down: lines go south, pixels east and a little north).
    assert 0 < p["angle"] < 30
    assert centre_of(by_id["r1"]) == pytest.approx((lon, lat), abs=1e-7)


def test_a_record_not_measured_takes_its_class_sizes_upright_or_is_left_out(provider, tmp_path):
    layer = add(utm_image(tmp_path / "a.tif"))
    lon, lat = lonlat(50, 60)
    provider.set_book(lambda: Book([record(1, "a.tif", lon, lat, kind="航空機 > 輸送機"), record(2, "a.tif", lon, lat, kind="")]))
    _, by_id, _, algorithm = export([layer], tmp_path / "out.geojson")
    p = by_id["r1"]["properties"]
    assert p["size_from"] == "諸元" and p["angle"] is None and p["heading"] is None
    # Upright, holding the 24 m aircraft at any heading.
    assert p["length_px"] == pytest.approx(24, abs=0.5) and p["width_px"] == pytest.approx(24, abs=0.5)
    assert by_id["r2"]["properties"]["size_from"] == "既定" and by_id["r2"]["properties"]["length_px"] == pytest.approx(20, abs=0.5)
    _, by_id, _, algorithm = export([layer], tmp_path / "measured.geojson", UNMEASURED=1)
    assert by_id == {} and last_result().skipped == {"長さを測っていない記録": 2}


def test_records_left_out(provider, tmp_path):
    layer = add(utm_image(tmp_path / "a.tif"))
    lon, lat = lonlat(50, 60)
    base = record(0, "a.tif", lon, lat, length=20.0, heading=45.0)
    records = [
        base,
        replace(base, id="gone", present=False),
        replace(base, id="cloud", present=False, unseen="雲"),
        replace(base, id="many", quantity=30),
        replace(base, id="deleted", deleted_at=5),
        replace(base, id="plane", kind="航空機 > 輸送機"),
        replace(base, id="other", image_id="b.tif"),
    ]
    provider.set_book(lambda: Book(records))
    _, by_id, _, algorithm = export([layer], tmp_path / "out.geojson")
    assert set(by_id) == {"r0", "plane"}
    assert last_result().skipped == {"消失の記録": 1, "不可の記録": 1, "数量 2 以上の記録（まとめて記録）": 1}
    _, by_id, _, algorithm = export([layer], tmp_path / "ships.geojson", KINDS="船舶")
    assert set(by_id) == {"r0"} and last_result().skipped["対象外の種別"] == 1


def test_several_images_in_one_file(provider, tmp_path):
    a = add(utm_image(tmp_path / "a.tif"), "a")
    b = add(utm_image(tmp_path / "b.tif"), "b")
    provider.set_book(lambda: Book([record(1, "a.tif", *lonlat(10, 10), length=10.0, heading=0.0),
                                    record(2, "b.tif", *lonlat(20, 20), length=10.0, heading=0.0)]))
    data, by_id, _, _ = export([a, b], tmp_path / "out")
    assert set(by_id) == {"r1", "r2"}
    assert [i["image_id"] for i in data["mikage"]["images"]] == ["a.tif", "b.tif"]
    assert (tmp_path / "out.geojson").exists()


def test_no_book_no_run(provider, tmp_path):
    layer = add(utm_image(tmp_path / "a.tif"))
    algorithm = QgsApplication.processingRegistry().algorithmById("mikage:records_to_pixel_boxes").create()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    _, ok = algorithm.run({"IMAGES": [layer.id()], "OUTPUT": str(tmp_path / "x.geojson")}, context, QgsProcessingFeedback())
    assert not ok and not (tmp_path / "x.geojson").exists()


def test_the_dialog_starts_with_the_active_image(tmp_path):
    from mikage.processing_provider.pixel_box_algorithm import default_inputs

    a = QgsRasterLayer(utm_image(tmp_path / "a.tif"), "a")
    b = QgsRasterLayer(utm_image(tmp_path / "b.tif"), "b")
    assert default_inputs([a, b], b) == {"IMAGES": [b.id()]}
    assert default_inputs([a, b], None) == {"IMAGES": [a.id()]}
    assert default_inputs([], None) == {}
