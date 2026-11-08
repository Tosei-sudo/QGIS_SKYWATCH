"""
アノテーション変換 in QGIS: points on bright bars (turned various ways)
become boxes along the bars, by the brightness threshold and by a stand-in
SAM (tests/data/ai: sam-encoder + sam-bright-decoder); attributes
(heading, length, width) come first; a training dataset (YOLO points on a
PNG, vector points on a GeoTIFF) becomes a new dataset of DOTA boxes and a
GeoPackage that 「AI 学習」 reads.
"""
import json
import math
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProcessingContext,
    QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant  # noqa: E402

from skywatch.core.ai.train.dataset import Dataset, Group, Item, Reader, load_dataset, read_dota, save_dataset  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data", "ai")
SIZE = 400
X0, Y0 = 380000, 3950000
#: (x, y, angle) of the bars, pixels of 1 m.
BARS = [(80, 80, 0), (300, 90, 30), (100, 300, 120), (300, 300, 75)]


def bars_array(bars=BARS, length=40, width=10, size=SIZE):
    ys, xs = np.mgrid[0:size, 0:size] + 0.5
    a = np.full((size, size), 35.0)
    for cx, cy, angle in bars:
        r = math.radians(angle)
        ux, uy = math.sin(r), -math.cos(r)
        along = (xs - cx) * ux + (ys - cy) * uy
        across = (xs - cx) * -uy + (ys - cy) * ux
        a[(abs(along) <= length / 2) & (abs(across) <= width / 2)] = 225
    return a.astype(np.uint8)


def tiff(path, bars=BARS):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), SIZE, SIZE, 3, gdal.GDT_Byte)
    ds.SetGeoTransform([X0, 1, 0, Y0, 0, -1])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32654)
    ds.SetProjection(srs.ExportToWkt())
    a = bars_array(bars)
    for b in range(3):
        ds.GetRasterBand(b + 1).WriteArray(a)
    ds = None
    return str(path)


def png(path, bars=BARS):
    mem = gdal.GetDriverByName("MEM").Create("", SIZE, SIZE, 3, gdal.GDT_Byte)
    a = bars_array(bars)
    for b in range(3):
        mem.GetRasterBand(b + 1).WriteArray(a)
    gdal.GetDriverByName("PNG").CreateCopy(str(path), mem)
    return str(path)


def points_layer(points, extra=()):
    layer = QgsVectorLayer("Point?crs=EPSG:32654", "点", "memory")
    fields = [QgsField("class", QVariant.String)] + [QgsField(n, QVariant.Double) for n in extra]
    layer.dataProvider().addAttributes(fields)
    layer.updateFields()
    for p in points:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + p[0], Y0 - p[1])))
        f.setAttributes(["船舶 > 支援船"] + list(p[2:]))
        layer.dataProvider().addFeature(f)
    QgsProject.instance().addMapLayer(layer)
    return layer


@pytest.fixture
def provider():
    from skywatch.processing_provider.provider import SatObsProvider

    p = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(p)
    yield p
    QgsApplication.processingRegistry().removeProvider(p)
    QgsProject.instance().clear()


def run(algorithm_id, params):
    algorithm = QgsApplication.processingRegistry().algorithmById(algorithm_id).create()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run(params, context, feedback)
    return results, ok, context


def boxes(params):
    results, ok, context = run("skywatch:points_to_boxes", dict(params, OUTPUT="memory:"))
    assert ok, results
    return list(context.takeResultLayer(results["OUTPUT"]).getFeatures())


def axis_gap(a, b):
    return abs(((a - b + 90) % 180) - 90)


@pytest.mark.parametrize("method", [1, 0])
def test_points_on_bars_become_boxes_along_them(provider, tmp_path, method):
    if method == 0:
        pytest.importorskip("onnxruntime")
    image = QgsRasterLayer(tiff(tmp_path / "bars.tif"), "bars")
    QgsProject.instance().addMapLayer(image)
    # The points a little off the bars' middles.
    layer = points_layer([(x + 3, y - 2) for x, y, _ in BARS])
    params = {"INPUT": layer.id(), "RASTER": image.id(), "METHOD": method, "LENGTH": 0, "WIDTH_MODE": 1}
    if method == 0:
        params.update(ENCODER=os.path.join(DATA, "sam-encoder.onnx"), DECODER=os.path.join(DATA, "sam-bright-decoder.onnx"))
    out = boxes(params)
    assert len(out) == len(BARS)
    # The stand-in SAM sees the window in cells of 1/16 (its embeddings): coarser.
    tolerance = (6, 4, 3) if method == 1 else (15, 12, 8)
    for f, (x, y, angle) in zip(out, BARS):
        assert axis_gap(f["box_heading"], angle) < tolerance[0], (f["box_heading"], angle)
        assert f["box_length_m"] == pytest.approx(40, abs=tolerance[1]) and f["box_width_m"] == pytest.approx(10, abs=tolerance[2])
        c = f.geometry().centroid().asPoint()
        assert abs(c.x() - (X0 + x)) < 3 and abs(c.y() - (Y0 - y)) < 3
        assert f["box_dir_from"] == ("しきい値" if method == 1 else "SAM") and f["class"] == "船舶 > 支援船"
        assert f.geometry().area() == pytest.approx(f["box_length_m"] * f["box_width_m"], rel=0.02)


def test_the_length_given_and_the_width_by_ratio_or_outline(provider, tmp_path):
    image = QgsRasterLayer(tiff(tmp_path / "bars.tif"), "bars")
    QgsProject.instance().addMapLayer(image)
    layer = points_layer([(x, y) for x, y, _ in BARS])
    out = boxes({"INPUT": layer.id(), "RASTER": image.id(), "METHOD": 1, "LENGTHS": "船舶=100", "WIDTH_MODE": 0})
    # 100 m long, as wide as the outline's 1 : 4.
    assert [round(f["box_length_m"]) for f in out] == [100] * 4
    assert all(f["box_width_m"] == pytest.approx(25, abs=5) for f in out)
    assert out[0]["box_size_from"] == "クラス別・幅 しきい値の縦横比"
    out = boxes({"INPUT": layer.id(), "RASTER": image.id(), "METHOD": 1, "LENGTH": 30, "WIDTH_MODE": 2, "RATIO": 0.5})
    assert all(f["box_width_m"] == pytest.approx(15) for f in out)


def test_attributes_need_no_image(provider):
    # As observation records measured them: length, width (3–4 points) and heading (2 points).
    layer = points_layer([(100, 100, 50.0, 8.0, 90.0), (200, 200, None, None, None)], ("length_m", "width_m", "heading_deg"))
    out = boxes({"INPUT": layer.id(), "METHOD": 2, "LENGTH": 20})
    a, b = out
    assert (a["box_length_m"], a["box_width_m"], a["box_heading"], a["box_dir_from"]) == (50, 8, 90, "属性")
    box = a.geometry().boundingBox()
    # Turned east (true north: UTM's grid is a little turned here): 50 m east–west, 8 m north–south.
    assert box.width() == pytest.approx(50, abs=0.5) and box.height() == pytest.approx(8, abs=1.5)
    assert a.geometry().area() == pytest.approx(400, rel=0.01)
    # No direction: the square round any (20 m × 5 m by the ratio → 20 m square).
    assert b["box_dir_from"] == "不明"
    bb = b.geometry().boundingBox()
    assert bb.width() == pytest.approx(20, abs=0.5) and bb.height() == pytest.approx(20, abs=0.5)


def test_refused_without_image_or_model(provider):
    layer = points_layer([(100, 100)])
    results, ok, _ = run("skywatch:points_to_boxes", {"INPUT": layer.id(), "METHOD": 1, "OUTPUT": "memory:"})
    assert not ok
    results, ok, _ = run("skywatch:points_to_boxes", {"INPUT": layer.id(), "METHOD": 2, "WIDTH_MODE": 3, "WIDTH": 0, "OUTPUT": "memory:"})
    assert not ok


def test_a_dataset_of_points_becomes_one_of_boxes(provider, tmp_path):
    # YOLO points (w = h = 0) on a PNG of no CRS, and one ordinary box kept as it is.
    picture = png(tmp_path / "a.png")
    labels = tmp_path / "a.txt"
    rows = [f"0 {(x + 2) / SIZE:.5f} {(y + 1) / SIZE:.5f} 0 0" for x, y, _ in BARS] + ["1 0.5 0.5 0.1 0.05"]
    labels.write_text("\n".join(rows) + "\n")
    # Vector points on a GeoTIFF, one with its own heading, one off the image.
    geo = tiff(tmp_path / "b.tif")
    vector = str(tmp_path / "points.gpkg")
    layer = points_layer([(x, y, None) for x, y, _ in BARS] + [(150, 200, 45.0), (5000, 5000, None)], ("heading",))
    QgsVectorFileWriter.writeAsVectorFormatV3(layer, vector, QgsProject.instance().transformContext(), QgsVectorFileWriter.SaveVectorOptions())
    source = tmp_path / "set.json"
    save_dataset(Dataset("テスト", [Group("yolo", [Item(picture, str(labels))], ["ship", "small car"], class_map={"ship": "船舶"}),
                                    Group("vector", [Item(geo, vector)], class_field="class")]), str(source))
    target = tmp_path / "out" / "boxes.json"
    target.parent.mkdir()
    results, ok, _ = run("skywatch:dataset_points_to_boxes", {"DATASET": str(source), "METHOD": 1, "LENGTH": 0, "WIDTH_MODE": 1,
                                                              "PIXEL_SIZE": 0.5, "LENGTHS": "船舶 > 支援船=30",
                                                              "OUTPUT": str(target)})
    assert ok, results
    assert results["COUNT"] == 4 + 6
    out = load_dataset(str(target))
    assert out.name == "テスト（矩形）" and [g.format for g in out.groups] == ["dota", "vector"]
    dota = out.groups[0]
    assert dota.class_map["ship"] == "船舶" and dota.class_map["small_car"] == "small car"
    assert dota.items[0].image == picture and dota.items[0].labels.startswith(str(tmp_path / "out" / "boxes_labels"))
    text = open(dota.items[0].labels, encoding="utf-8").read()
    got = read_dota(text)
    assert [b.name for b in got] == ["ship"] * 4 + ["small_car"]
    # Turned as the bars: the rows' first side runs along each bar (pixels, PNG's own; 40 px long at 0.5 m → 20 m).
    for line, (x, y, angle) in zip(text.splitlines(), BARS):
        v = [float(t) for t in line.split()[:8]]
        side = math.hypot(v[2] - v[0], v[3] - v[1])
        heading = math.degrees(math.atan2(v[2] - v[0], -(v[3] - v[1]))) % 180
        assert side == pytest.approx(40, abs=4) and axis_gap(heading, angle) < 6
    # The box kept: 40 × 20 px round the middle.
    last = [float(t) for t in text.splitlines()[-1].split()[:8]]
    assert last[:2] == pytest.approx([180, 190]) and last[4:6] == pytest.approx([220, 210])
    # The vector file as a GeoPackage of polygons, read as the trainer reads it.
    vec = out.groups[1]
    assert vec.class_field == "class" and vec.items[0].labels.endswith(".gpkg")
    polys = QgsVectorLayer(vec.items[0].labels, "v", "ogr")
    feats = list(polys.getFeatures())
    assert len(feats) == 6 and polys.geometryType() == 2
    by_dir = sorted(f["box_dir_from"] for f in feats)
    assert by_dir == ["しきい値"] * 4 + ["不明", "属性"]
    own = next(f for f in feats if f["box_dir_from"] == "属性")
    assert own["box_heading"] == 45 and own["box_length_m"] == 30
    # The vector's bars: 30 m by class, turned as the outline.
    assert all(f["box_length_m"] == 30 for f in feats)
    # 「AI 学習」 reads the new dataset.
    assert len(Reader().boxes(dota, dota.items[0])) == 5
    assert json.load(open(target, encoding="utf-8"))["stats"]["converted"] == 10
