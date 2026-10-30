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
    assert trained.classes == ["船舶"] and trained.input.mode == "rgb" and trained.arch == "res"
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
        "IMAGES": [layer.id()], "BANDS": bands, "NETWORK": 1, "EPOCHS": 12, "BATCH": 4, "TILE": 0, "VALIDATION": 0, "REMEMBER": False, "OUTPUT": model})
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
    _, ok, _ = run("satobs:train_detector", {"IMAGES": [layer.id()], "DEPTH": 1, "NETWORK": 1, "EPOCHS": 2, "TILE": 0, "VALIDATION": 0,
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
    # The old class first, the new one added; the network (軽量) and input size of the base model kept.
    assert trained.classes == ["船舶", "航空機"] and trained.tile == 128 and trained.arch == "fpn4"
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


# Training datasets (教師データセット) of images outside the catalog.

def png(path, centres, side=10, bars=()):
    """An image without a place on the map (its own pixels), the squares (and bars) on noise."""
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    rng = np.random.default_rng(len(centres) + 100)
    mem = gdal.GetDriverByName("MEM").Create("", SIZE, SIZE, 3, gdal.GDT_Byte)
    for b in range(3):
        a = rng.normal(40, 8, (SIZE, SIZE))
        for x, y in centres:
            a[y - side // 2:y + side // 2, x - side // 2:x + side // 2] = 230
        for x, y in bars:
            a[y - 3:y + 3, x - 12:x + 12] = 230
        mem.GetRasterBand(b + 1).WriteArray(a.clip(0, 255))
    gdal.GetDriverByName("PNG").CreateCopy(str(path), mem)
    return str(path)


def yolo_set(root, seeds=(11, 12, 13), side=10):
    """YOLO folders: images/*.png, labels/*.txt (class 0 = ship), classes.txt; and an image without annotations."""
    for seed in seeds:
        centres = objects(seed)
        png(root / "images" / f"s{seed}.png", centres)
        os.makedirs(root / "labels", exist_ok=True)
        with open(root / "labels" / f"s{seed}.txt", "w") as f:
            f.writelines(f"0 {x / SIZE} {y / SIZE} {side / SIZE} {side / SIZE}\n" for x, y in centres)
    png(root / "images" / "empty.png", [])
    with open(root / "labels" / "classes.txt", "w") as f:
        f.write("ship\n")


def make_dataset(params):
    results, ok, _ = run("satobs:make_dataset", params)
    assert ok
    return results


def test_a_yolo_dataset_outside_the_catalog_trains_a_model(provider, tmp_path):
    import json

    yolo_set(tmp_path)
    path = str(tmp_path / "ships.json")
    results = make_dataset({"IMAGES": str(tmp_path / "images"), "LABELS": str(tmp_path / "labels"), "CLASS_MAP": "ship=船舶 > 支援船",
                            "BACKGROUND": True, "NAME": "外部の船", "OUTPUT": path})
    assert results["OUTPUT"] == path and results["COUNT"] == 4
    data = json.load(open(path, encoding="utf-8"))
    [group] = data["groups"]
    assert data["name"] == "外部の船" and group["format"] == "yolo" and group["names"] == ["ship"]
    assert data["stats"] == {"images": 4, "objects": 42, "classes": {"ship": 42}}
    assert all(os.path.isabs(i["image"]) for i in group["items"]) and {i["labels"] for i in group["items"] if "empty" in i["image"]} == {""}
    # Run again on the same file: nothing new.
    assert make_dataset({"IMAGES": str(tmp_path / "images"), "LABELS": str(tmp_path / "labels"), "OUTPUT": path})["COUNT"] == 0

    # Trained on the dataset alone (no image on the map), at depth 1.
    model = str(tmp_path / "ships.onnx")
    results, ok, _ = run("satobs:train_detector", {
        "DATASETS": [path], "DEPTH": 1, "EPOCHS": 12, "BATCH": 4, "TILE": 0, "VALIDATION": 0, "REMEMBER": False, "OUTPUT": model})
    assert ok
    trained = load_model(open(model, "rb").read())
    history = trained.history[0]
    assert trained.classes == ["船舶"] and history["objects"] == {"船舶": 42} and history["source"] == "データセット「外部の船」"
    assert sorted(history["images"]) == ["empty.png", "s11.png", "s12.png", "s13.png"]
    # It finds the squares on an image on the map.
    test_image = QgsRasterLayer(tiff(tmp_path / "test.tif", objects(2)), "test")
    QgsProject.instance().addMapLayer(test_image)
    assert found_at(detect(test_image, model), objects(2)) >= 11


def test_a_dataset_with_the_catalogs_images_and_records(provider, tmp_path):
    import json

    # The catalog's image with its records (squares, 船舶), and a VOC dataset of bars (plane → 航空機 > 輸送機).
    centres = objects(14, 6)
    layer = QgsRasterLayer(tiff(tmp_path / "train.tif", centres), "train")
    QgsProject.instance().addMapLayer(layer)
    provider.set_book(lambda: Book([record(i, "train.tif", x, y) for i, (x, y) in enumerate(centres)]))
    bars = [(60, 300), (200, 330), (320, 60)]
    png(tmp_path / "voc" / "bars.png", [], bars=bars)
    objects_xml = "".join(f"<object><name>plane</name><bndbox><xmin>{x - 11}</xmin><ymin>{y - 2}</ymin><xmax>{x + 12}</xmax>"
                          f"<ymax>{y + 3}</ymax></bndbox></object>" for x, y in bars)
    with open(tmp_path / "voc" / "bars.xml", "w") as f:
        f.write(f"<annotation>{objects_xml}</annotation>")
    path = str(tmp_path / "planes.json")
    make_dataset({"IMAGES": str(tmp_path / "voc"), "FORMAT": 3, "CLASS_MAP": "plane=航空機 > 輸送機", "OUTPUT": path})
    assert json.load(open(path, encoding="utf-8"))["groups"][0]["format"] == "voc"
    model = str(tmp_path / "both.onnx")
    results, ok, context = run("satobs:train_detector", {
        "IMAGES": [layer.id()], "DATASETS": [path], "DEPTH": 1, "NETWORK": 1, "EPOCHS": 1, "TILE": 0, "VALIDATION": 0, "REMEMBER": False,
        "OUTPUT": model, "BOXES": "memory:"})
    assert ok
    history = load_model(open(model, "rb").read()).history[0]
    assert history["objects"] == {"船舶": 6, "航空機": 3} and history["images"] == ["train.tif", "bars.png"]
    assert history["source"] == "観測記録（テスト）・データセット「planes」"
    # The boxes on the map: the catalog image's (the PNG has no place on it).
    boxes = context.takeResultLayer(results["BOXES"])
    assert sorted(f["image"] for f in boxes.getFeatures()) == ["train.tif"] * 6


def test_a_vector_dataset_on_georeferenced_images(provider, tmp_path):
    import json

    centres = objects(15, 8)
    os.makedirs(tmp_path / "geo")
    tiff(tmp_path / "geo" / "a.tif", centres)
    features = [{"type": "Feature", "properties": {"種別": "船舶"},
                 "geometry": {"type": "Point", "coordinates": [X0 + x, Y0 - y]}} for x, y in centres]
    labels = tmp_path / "ships.geojson"
    labels.write_text(json.dumps({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:32654"}},
                                  "features": features}), encoding="utf-8")
    path = str(tmp_path / "geo.json")
    make_dataset({"IMAGES": str(tmp_path / "geo"), "LABELS_FILE": str(labels), "OUTPUT": path})
    group = json.load(open(path, encoding="utf-8"))["groups"][0]
    assert group["format"] == "vector" and group["class_field"] == "種別"
    results, ok, context = run("satobs:train_detector", {
        "DATASETS": [path], "DEFAULT_SIZE": 10, "NETWORK": 1, "EPOCHS": 1, "TILE": 0, "VALIDATION": 0, "REMEMBER": False,
        "OUTPUT": str(tmp_path / "m.onnx"), "BOXES": "memory:"})
    assert ok
    boxes = context.takeResultLayer(results["BOXES"])
    # On the map, where the points are (a 10 m box: 船舶 has no 全長 in the classification).
    assert boxes.featureCount() == 8 and boxes.crs().authid() == "EPSG:32654"
    assert found_at(boxes, centres) == 8


def test_missing_files_are_left_out(provider, tmp_path):
    yolo_set(tmp_path, seeds=(16, 17))
    path = str(tmp_path / "set.json")
    make_dataset({"IMAGES": str(tmp_path / "images"), "LABELS": str(tmp_path / "labels"), "CLASS_MAP": "ship=船舶", "OUTPUT": path})
    os.remove(tmp_path / "images" / "s16.png")
    model = str(tmp_path / "m.onnx")
    results, ok, _ = run("satobs:train_detector", {"DATASETS": [path], "NETWORK": 1, "EPOCHS": 1, "TILE": 0, "VALIDATION": 0,
                                                     "REMEMBER": False, "OUTPUT": model})
    assert ok and load_model(open(model, "rb").read()).history[0]["images"] == ["s17.png"]
    # Neither images nor a dataset; not a dataset.
    _, ok, _ = run("satobs:train_detector", {"EPOCHS": 1, "REMEMBER": False, "OUTPUT": model})
    assert not ok
    (tmp_path / "x.json").write_text("{}")
    _, ok, _ = run("satobs:train_detector", {"DATASETS": [str(tmp_path / "x.json")], "EPOCHS": 1, "REMEMBER": False, "OUTPUT": model})
    assert not ok
