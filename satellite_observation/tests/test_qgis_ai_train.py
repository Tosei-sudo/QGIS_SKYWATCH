"""
AI 学習（物体検出） in QGIS: a model trained on a synthetic image's
observation records (bright squares recorded as 船舶 > 支援船, trained at
depth 1 as 船舶) finds them again through 物体検出（AI）; a model trained on
band values (one band, several) is run on those bands; a model is trained
further with a new class from a vector layer.
"""
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
pytest.importorskip("onnxruntime")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,
    QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer,
)

from satellite_observation.core.ai.train.trainer import load_model  # noqa: E402
from satellite_observation.core.obs.areas import CheckedArea  # noqa: E402
from satellite_observation.core.obs.model import Observation, default_taxonomy  # noqa: E402
from satellite_observation.core.obs.taxonomy import Taxon  # noqa: E402

SIZE = 384
X0, Y0 = 380000, 3950000
UTM = QgsCoordinateReferenceSystem("EPSG:32654")


def objects(seed: int, count: int = 14, side: int = 10):
    """Centres (pixels) of squares well apart."""
    rng = np.random.default_rng(seed)
    out = []
    while len(out) < count:
        x, y = rng.integers(20, SIZE - 20, 2)
        if all(abs(x - a) > 3 * side or abs(y - b) > 3 * side for a, b in out):
            out.append((int(x), int(y)))
    return out


def tiff(path, centres, bands=3, dtype=gdal.GDT_Byte, side=10, band_of_objects=None, bars=()):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), SIZE, SIZE, bands, dtype)
    ds.SetGeoTransform([X0, 1, 0, Y0, 0, -1])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32654)
    ds.SetProjection(srs.ExportToWkt())
    rng = np.random.default_rng(len(centres))
    for b in range(bands):
        a = rng.normal(40, 8, (SIZE, SIZE))
        if band_of_objects is None or b + 1 == band_of_objects:
            for x, y in centres:
                a[y - side // 2:y + side // 2, x - side // 2:x + side // 2] = 230
            for x, y in bars:
                a[y - 3:y + 3, x - 12:x + 12] = 230
        ds.GetRasterBand(b + 1).WriteArray(a.clip(0, 255) if dtype == gdal.GDT_Byte else a * 10)
    ds = None
    return str(path)


def lonlat(x, y):
    to = QgsCoordinateTransform(UTM, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    p = to.transform(QgsPointXY(X0 + x, Y0 - y))
    return p.x(), p.y()


def record(i, image, x, y, kind="船舶 > 支援船", length=10.0):
    lon, lat = lonlat(x, y)
    return Observation(f"r{i}", image, image, None, f"o{i}", "", kind, "", True, "", lon, lat, 0, 0, length=length)


class Book:
    """What the algorithm reads of the observation book."""

    def __init__(self, records, areas=()):
        self.records = list(records)
        self.areas = list(areas)
        self.label = "テスト"

    def taxonomy(self):
        return default_taxonomy() + [Taxon(["船舶", "支援船"]), Taxon(["航空機", "輸送機"], specs={"全長": "24", "全幅": "6"})]


@pytest.fixture
def provider():
    from satellite_observation.processing_provider import train_algorithm
    from satellite_observation.processing_provider.provider import SatObsProvider

    p = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(p)
    yield train_algorithm
    train_algorithm.set_book(lambda: None)
    QgsApplication.processingRegistry().removeProvider(p)
    QgsProject.instance().clear()


def run(algorithm_id, params):
    algorithm = QgsApplication.processingRegistry().algorithmById(algorithm_id).create()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run(params, context, feedback)
    return results, ok, context


def detect(layer, model, score=0.3):
    results, ok, context = run("satobs:detect", {"INPUT": layer.id(), "MODEL": 0, "MODEL_FILE": model, "SCORE": score, "OUTPUT": "memory:"})
    assert ok
    return context.takeResultLayer(results["OUTPUT"])


def found_at(out, centres, side=10):
    """How many of the squares a detection's box centre falls on (within half a square)."""
    hits = 0
    boxes = [f.geometry().boundingBox() for f in out.getFeatures()]
    for x, y in centres:
        mx, my = X0 + x, Y0 - y
        if any(abs(b.center().x() - mx) <= side / 2 and abs(b.center().y() - my) <= side / 2 for b in boxes):
            hits += 1
    return hits


def test_trained_on_records_it_finds_the_squares_again(provider, tmp_path):
    centres = objects(1)
    layer = QgsRasterLayer(tiff(tmp_path / "train.tif", centres), "train")
    QgsProject.instance().addMapLayer(layer)
    # Records of this image (and one of another image, left out), and a checked area over the whole image.
    records = [record(i, "train.tif", x, y) for i, (x, y) in enumerate(centres)] + [record(99, "other.tif", 50, 50)]
    ring = [list(lonlat(x, y)) for x, y in ((0, 0), (SIZE, 0), (SIZE, SIZE), (0, SIZE), (0, 0))]
    provider.set_book(lambda: Book(records, [CheckedArea("a1", "train.tif", "train.tif", None, ring, "", "", 0, 0)]))
    model = str(tmp_path / "ships.onnx")
    results, ok, _ = run("satobs:train_detector", {
        "IMAGES": [layer.id()], "DEPTH": 1, "EPOCHS": 12, "BATCH": 4, "TILE": 0, "VALIDATION": 0, "REMEMBER": False, "OUTPUT": model,
        "BOXES": "memory:"})
    assert ok and results["OUTPUT"] == model and os.path.isfile(model)
    trained = load_model(open(model, "rb").read())
    assert trained.classes == ["船舶"] and trained.input.mode == "rgb"
    history = trained.history
    assert len(history) == 1 and history[0]["objects"] == {"船舶": 14} and history[0]["images"] == ["train.tif"]
    assert history[0]["epochs"] == 12 and history[0]["source"] == "観測記録（テスト）"
    # Found again by 物体検出（AI）, with the class of the classification.
    test_image = QgsRasterLayer(tiff(tmp_path / "test.tif", objects(2)), "test")
    QgsProject.instance().addMapLayer(test_image)
    out = detect(test_image, model)
    assert {f["class"] for f in out.getFeatures()} == {"船舶"}
    assert found_at(out, objects(2)) >= 11


def test_trained_boxes_follow_the_records_sizes(provider, tmp_path):
    centres = objects(3, 4)
    layer = QgsRasterLayer(tiff(tmp_path / "train.tif", centres), "train")
    QgsProject.instance().addMapLayer(layer)
    records = [record(0, "train.tif", *centres[0], length=10.0), record(1, "train.tif", *centres[1], kind="航空機 > 輸送機", length=None),
               record(2, "train.tif", *centres[2], kind="", length=None), record(3, "train.tif", *centres[3], kind="その他", length=None)]
    records[3].present = False
    provider.set_book(lambda: Book(records))
    results, ok, context = run("satobs:train_detector", {
        "IMAGES": [layer.id()], "EPOCHS": 1, "TILE": 0, "VALIDATION": 0, "REMEMBER": False, "OUTPUT": str(tmp_path / "m.onnx"),
        "BOXES": "memory:"})
    assert ok
    boxes = context.takeResultLayer(results["BOXES"])
    sizes = {f["class"]: (round(f.geometry().boundingBox().width()), round(f.geometry().boundingBox().height())) for f in boxes.getFeatures()}
    # The record's length (heading unknown: the square), the class' 全長 24 m (no heading: 24 m square).
    assert sizes == {"船舶 > 支援船": (10, 10), "航空機 > 輸送機": (24, 24)}


@pytest.mark.parametrize("bands", ["4", "1-4"])
def test_trained_on_band_values(provider, tmp_path, bands):
    centres = objects(4)
    # The squares only in band 4 (float values); bands 1–3 noise.
    layer = QgsRasterLayer(tiff(tmp_path / "train.tif", centres, bands=4, dtype=gdal.GDT_Float32, band_of_objects=4), "train")
    QgsProject.instance().addMapLayer(layer)
    provider.set_book(lambda: Book([record(i, "train.tif", x, y) for i, (x, y) in enumerate(centres)]))
    model = str(tmp_path / "bands.onnx")
    results, ok, _ = run("satobs:train_detector", {
        "IMAGES": [layer.id()], "BANDS": bands, "EPOCHS": 12, "BATCH": 4, "TILE": 0, "VALIDATION": 0, "REMEMBER": False, "OUTPUT": model})
    assert ok
    trained = load_model(open(model, "rb").read())
    wanted = [4] if bands == "4" else [1, 2, 3, 4]
    assert trained.input.mode == "bands" and trained.input.bands == wanted and len(trained.input.mean) == len(wanted)
    test_image = QgsRasterLayer(tiff(tmp_path / "test.tif", objects(5), bands=4, dtype=gdal.GDT_Float32, band_of_objects=4), "test")
    QgsProject.instance().addMapLayer(test_image)
    out = detect(test_image, model)
    assert found_at(out, objects(5)) >= 11


def test_a_band_model_refuses_an_image_without_its_bands(provider, tmp_path):
    from satellite_observation.core.ai.train.trainer import InputSpec, new_model

    model = str(tmp_path / "bands.onnx")
    with open(model, "wb") as f:
        f.write(new_model(["船舶"], InputSpec("bands", [4], [0.0], [1.0]), 128, "small").onnx())
    rgb = QgsProject.instance().addMapLayer(QgsRasterLayer(tiff(tmp_path / "rgb.tif", objects(6, 3)), "rgb")).id()
    _, ok, _ = run("satobs:detect", {"INPUT": rgb, "MODEL": 0, "MODEL_FILE": model, "OUTPUT": "memory:"})
    assert not ok


def test_trained_further_with_a_new_class_from_a_vector_layer(provider, tmp_path):
    centres = objects(7, 10)
    layer = QgsRasterLayer(tiff(tmp_path / "train.tif", centres), "train")
    QgsProject.instance().addMapLayer(layer)
    provider.set_book(lambda: Book([record(i, "train.tif", x, y) for i, (x, y) in enumerate(centres)]))
    base = str(tmp_path / "base.onnx")
    _, ok, _ = run("satobs:train_detector", {"IMAGES": [layer.id()], "DEPTH": 1, "EPOCHS": 2, "TILE": 0, "VALIDATION": 0,
                                                "REMEMBER": False, "OUTPUT": base})
    assert ok
    bars = [(60, 300), (200, 330), (320, 60)]
    image = QgsRasterLayer(tiff(tmp_path / "bars.tif", centres, bars=bars), "bars")
    QgsProject.instance().addMapLayer(image)
    # Points: the bars as 輸送機 (a class' own name, found in the classification; its 全長 24 m), the squares as 船舶.
    labels = QgsVectorLayer("Point?crs=EPSG:32654&field=class:string", "labels", "memory")
    for (x, y), kind in [(b, "輸送機") for b in bars] + [(c, "船舶") for c in centres]:
        f = QgsFeature(labels.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + x, Y0 - y)))
        f.setAttributes([kind])
        labels.dataProvider().addFeature(f)
    QgsProject.instance().addMapLayer(labels)
    further = str(tmp_path / "further.onnx")
    results, ok, _ = run("satobs:train_detector", {
        "IMAGES": [image.id()], "SOURCE": 1, "LABELS": labels.id(), "CLASS_FIELD": "class", "DEPTH": 1, "BASE_MODEL": base, "EPOCHS": 2,
        "TILE": 2, "VALIDATION": 0, "REMEMBER": False, "OUTPUT": further})
    assert ok
    trained = load_model(open(further, "rb").read())
    # The old class first, the new one added; the input size of the base model kept.
    assert trained.classes == ["船舶", "航空機"] and trained.tile == 128
    history = trained.history
    assert len(history) == 2 and history[1]["base"] == "base.onnx" and history[1]["newClasses"] == ["航空機"]
    assert history[1]["objects"] == {"船舶": 10, "航空機": 3}


def test_polygon_labels_keep_their_outline(provider, tmp_path):
    bars = [(60, 300), (200, 330), (320, 60)]
    layer = QgsRasterLayer(tiff(tmp_path / "bars.tif", [], bars=bars), "bars")
    QgsProject.instance().addMapLayer(layer)
    labels = QgsVectorLayer("Polygon?crs=EPSG:32654&field=KIND:string&field=image_id:string", "labels", "memory")
    for i, (x, y) in enumerate(bars):
        f = QgsFeature(labels.fields())
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(X0 + x - 12, Y0 - y - 3, X0 + x + 12, Y0 - y + 3)))
        # The last one belongs to another image.
        f.setAttributes(["航空機 > 輸送機", "other.tif" if i == 2 else "bars.tif"])
        labels.dataProvider().addFeature(f)
    QgsProject.instance().addMapLayer(labels)
    results, ok, context = run("satobs:train_detector", {
        "IMAGES": [layer.id()], "SOURCE": 1, "LABELS": labels.id(), "EPOCHS": 1, "TILE": 0, "VALIDATION": 0, "REMEMBER": False,
        "OUTPUT": str(tmp_path / "m.onnx"), "BOXES": "memory:"})
    assert ok
    boxes = context.takeResultLayer(results["BOXES"])
    assert [(f["class"], round(f.geometry().boundingBox().width()), round(f.geometry().boundingBox().height())) for f in boxes.getFeatures()] \
        == [("航空機 > 輸送機", 24, 6)] * 2


def test_a_trained_model_is_listed_for_detection(provider, tmp_path):
    from satellite_observation.processing_provider.detect_algorithm import detect_models
    from satellite_observation.qgis_io import settings

    centres = objects(8, 4)
    layer = QgsRasterLayer(tiff(tmp_path / "train.tif", centres), "train")
    QgsProject.instance().addMapLayer(layer)
    provider.set_book(lambda: Book([record(i, "train.tif", x, y) for i, (x, y) in enumerate(centres)]))
    model = str(tmp_path / "listed.onnx")
    try:
        _, ok, _ = run("satobs:train_detector", {"IMAGES": [layer.id()], "EPOCHS": 1, "TILE": 0, "VALIDATION": 0, "OUTPUT": model})
        assert ok
        models, _ = detect_models()
        assert [(m.label, m.url) for m in models][:1] == [("学習済み: listed", os.path.abspath(model))]
    finally:
        settings._set("trained_models", "[]")


def test_without_records_of_the_image(provider, tmp_path):
    layer = QgsRasterLayer(tiff(tmp_path / "train.tif", objects(9, 3)), "train")
    QgsProject.instance().addMapLayer(layer)
    provider.set_book(lambda: Book([record(0, "other.tif", 50, 50)]))
    _, ok, _ = run("satobs:train_detector", {"IMAGES": [layer.id()], "EPOCHS": 1, "TILE": 0, "REMEMBER": False,
                                                "OUTPUT": str(tmp_path / "m.onnx")})
    assert not ok


def test_bands_as_written():
    from qgis.core import QgsProcessingException

    from satellite_observation.processing_provider.train_algorithm import parse_bands

    assert parse_bands("") == [] and parse_bands("4") == [4] and parse_bands("1,2, 3") == [1, 2, 3]
    assert parse_bands("1-4") == [1, 2, 3, 4] and parse_bands("4 3 2") == [4, 3, 2] and parse_bands("1〜3、5") == [1, 2, 3, 5]
    for bad in ("赤", "0", "1,1"):
        with pytest.raises(QgsProcessingException):
            parse_bands(bad)
