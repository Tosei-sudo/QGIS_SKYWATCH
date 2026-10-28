"""
土地被覆分類 in QGIS: an image file classified by k-means or from training
polygons into a class-number GeoTIFF with its colours, the classes' areas,
outlines, and the Processing algorithm with its paletted layer.
"""
import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsVectorLayer,
)

from satellite_observation.qgis_io import classify_io as io  # noqa: E402

GT = [500000, 10, 0, 4000000, 0, -10]


def image(tmp_path, no_data=True):
    """60 × 60 px of 10 m, 3 bands: water (left third), forest (middle), bare soil (right), a little noise; a no-data corner."""
    rng = np.random.default_rng(3)
    looks = [(10, 20, 30), (40, 120, 50), (200, 180, 160)]
    data = np.zeros((3, 60, 60), np.float32)
    for k, look in enumerate(looks):
        for b in range(3):
            data[b, :, 20 * k:20 * (k + 1)] = look[b] + rng.normal(0, 2, (60, 20))
    if no_data:
        data[:, :3, :3] = -9999
    path = str(tmp_path / "scene.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 60, 60, 3, gdal.GDT_Float32)
    ds.SetGeoTransform(GT)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32654)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(3):
        band = ds.GetRasterBand(b + 1)
        band.SetNoDataValue(-9999)
        band.WriteArray(data[b])
    ds = None
    return path


def box(x0, y0, x1, y1):
    """A rectangle over pixels x0…x1, y0…y1, as WKT in the image's CRS."""
    l, r, t, b = GT[0] + x0 * 10, GT[0] + x1 * 10, GT[3] - y0 * 10, GT[3] - y1 * 10
    return f"POLYGON(({l} {t},{r} {t},{r} {b},{l} {b},{l} {t}))"


def test_k_means_into_three_classes_darkest_first(tmp_path):
    out = str(tmp_path / "k.tif")
    r = io.classify_file(image(tmp_path), [1, 2, 3], out, "kmeans", classes=3, pixel_area=lambda x, y: 100.0)
    ds = gdal.Open(out)
    band = ds.GetRasterBand(1)
    a = band.ReadAsArray()
    assert a[0, 0] == 0 and a[30, 10] == 1 and a[30, 30] == 2 and a[30, 50] == 3
    assert band.GetNoDataValue() == 0 and band.GetColorTable().GetColorEntry(1)[:3] == (31, 119, 180)
    assert band.GetCategoryNames() == ["データなし", "クラス 1", "クラス 2", "クラス 3"]
    assert [c.pixels for c in r.classes] == [1191, 1200, 1200] and r.classes[1].area == 120000
    assert ds.GetGeoTransform() == tuple(GT)


def test_supervised_from_training_polygons(tmp_path):
    training = [io.TrainingClass("水", [box(5, 10, 15, 20)]), io.TrainingClass("森", [box(25, 30, 35, 40), box(22, 50, 28, 55)]),
                io.TrainingClass("裸地", [box(45, 40, 55, 50)])]
    for kind in ("likelihood", "distance"):
        out = str(tmp_path / f"{kind}.tif")
        r = io.classify_file(image(tmp_path), [1, 2, 3], out, kind, training=training)
        ds = gdal.Open(out)
        a = ds.GetRasterBand(1).ReadAsArray()
        assert (a[5:, :20] == 1).all() and (a[:, 20:40] == 2).all() and (a[:, 40:] == 3).all()
        assert [c.name for c in r.classes] == ["水", "森", "裸地"]
    with pytest.raises(io.ClassifyError, match="不足のクラス: 水"):
        io.classify_file(image(tmp_path), [1, 2, 3], str(tmp_path / "x.tif"), "likelihood",
                         training=[io.TrainingClass("水", [box(5, 10, 6, 11)]), training[1]])
    far = "POLYGON((900000 4000000,900100 4000000,900100 3999900,900000 3999900,900000 4000000))"
    with pytest.raises(io.ClassifyError, match="範囲になし"):
        io.classify_file(image(tmp_path), [1], str(tmp_path / "y.tif"), "distance", training=[io.TrainingClass("a", [far]), io.TrainingClass("b", [far])])


def test_classes_outlined(tmp_path):
    out = str(tmp_path / "k.tif")
    io.classify_file(image(tmp_path, no_data=False), [1, 2, 3], out, "kmeans", classes=3)
    polygons, truncated = io.outline_classes(out, 3, 9)
    assert sorted((c, pixels) for _, c, pixels in polygons) == [(1, 1200), (2, 1200), (3, 1200)] and not truncated


def test_the_algorithm_makes_the_classes_table_and_outlines(tmp_path):
    from satellite_observation.processing_provider.classify_algorithm import default_inputs
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    try:
        algorithm = QgsApplication.processingRegistry().algorithmById("satobs:classify")
        layer = QgsRasterLayer(image(tmp_path), "scene")
        training = QgsVectorLayer("Polygon?crs=EPSG:32654&field=種類:string", "見本", "memory")
        features = []
        for name, wkt in (("水", box(5, 10, 15, 20)), ("森", box(25, 30, 35, 40)), ("裸地", box(45, 40, 55, 50)), ("", box(0, 0, 60, 60))):
            f = QgsFeature(training.fields())
            f.setGeometry(QgsGeometry.fromWkt(wkt))
            f.setAttributes([name])
            features.append(f)
        training.dataProvider().addFeatures(features)
        QgsProject.instance().addMapLayers([layer, training])
        assert default_inputs([layer, training], layer) == {"INPUT": layer.id(), "TRAINING": training.id()}
        out = str(tmp_path / "result.tif")
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        context.addLayerToLoadOnCompletion(out, QgsProcessingContext.LayerDetails("result", QgsProject.instance(), "OUTPUT"))
        feedback = QgsProcessingFeedback()
        results, ok = algorithm.run({"INPUT": layer.id(), "METHOD": 1, "TRAINING": training.id(), "FIELD": "種類", "OUTLINE": True,
                                     "OUTPUT": out, "TABLE": "memory:", "OUTLINES": "memory:"}, context, feedback)
        assert ok and results["OUTPUT"] == out
        table = context.getMapLayer(results["TABLE"])
        rows = [(f["クラス"], f["画素数"], f["色"]) for f in table.getFeatures()]
        assert rows == [("水", 1191, "#1f77b4"), ("森", 1200, "#2ca02c"), ("裸地", 1200, "#d62728")]
        assert abs(sum(f["割合_%"] for f in table.getFeatures()) - 100) < 0.02
        assert 0.118 < next(table.getFeatures())["面積_km2"] < 0.121
        outlines = context.getMapLayer(results["OUTLINES"])
        assert sorted(f["クラス"] for f in outlines.getFeatures()) == ["森", "水", "裸地"]
        result = QgsRasterLayer(out, "x")
        context.layerToLoadOnCompletionDetails(out).postProcessor().postProcessLayer(result, context, feedback)
        assert result.name() == "scene_classes" and result.renderer().type() == "paletted"
        assert [c.label for c in result.renderer().classes()] == ["水", "森", "裸地"]
        # k-means: the number of classes alone.
        results, ok = algorithm.run({"INPUT": layer.id(), "CLASSES": 4, "BANDS": [2], "OUTPUT": str(tmp_path / "k.tif")}, context, QgsProcessingFeedback())
        ds = gdal.Open(results["OUTPUT"])
        assert ok and ds.GetRasterBand(1).ReadAsArray().max() == 4
        # Supervised without an attribute: refused. The refused run's exception keeps this frame alive until Python exits, so
        # nothing in it may still hold a layer or dataset GDAL would have to close after its own clean-up.
        del result, ds
        _, ok = algorithm.run({"INPUT": layer.id(), "METHOD": 2, "TRAINING": training.id(), "OUTPUT": str(tmp_path / "z.tif")}, context, QgsProcessingFeedback())
        assert not ok
    finally:
        QgsApplication.processingRegistry().removeProvider(provider)
        QgsProject.instance().removeAllMapLayers()
