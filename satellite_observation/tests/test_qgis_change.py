"""
変化抽出 in QGIS: two files compared on the finer grid (the other
resampled, reprojected when needed), the outlines of what changed, and the
Processing algorithm with its blue-white-red layer.
"""
import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsVectorLayer  # noqa: E402

from satellite_observation.core.change import method_of  # noqa: E402
from satellite_observation.qgis_io import change_io as io  # noqa: E402


def write(path, array, gt, epsg=32654, no_data=None):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    if no_data is not None:
        band.SetNoDataValue(no_data)
    band.WriteArray(array)
    ds = None
    return str(path)


def pair(tmp_path):
    """Before: 100 × 100 px of 10 m, all 100 (a no-data corner). After: 50 × 50 px of 20 m, a bright 10 × 10 block (200 m) and a dark one."""
    before = np.full((100, 100), 100, np.float32)
    before[:4, :4] = -9999
    after = np.full((50, 50), 100, np.float32)
    after[10:20, 10:20] = 300
    after[30:35, 30:35] = 20
    return (write(tmp_path / "before.tif", before, [500000, 10, 0, 4000000, 0, -10], no_data=-9999),
            write(tmp_path / "after.tif", after, [500000, 20, 0, 4000000, 0, -20]))


def test_two_files_compared_on_the_finer_grid_with_the_changes_outlined(tmp_path):
    before, after = pair(tmp_path)
    assert io.describe(before, after) == "重なる範囲 100 × 100 px を比較。"
    out = str(tmp_path / "change.tif")
    r = io.change_files(before, 1, after, 1, out, method_of("difference"), resample="nearest", min_pixels=9)
    ds = gdal.Open(out)
    assert (ds.RasterXSize, ds.RasterYSize, ds.GetGeoTransform()[1]) == (100, 100, 10)
    a = ds.GetRasterBand(1).ReadAsArray()
    assert np.isnan(a[0, 0]) and a[50, 50] == 0 and a[25, 25] == 200 and a[65, 65] == -80
    assert r.thresholds.high < 200 and r.thresholds.low > -80
    ways = sorted((way, pixels) for _, way, pixels in r.polygons)
    assert ways == [("decrease", 100), ("increase", 400)]
    rings = next(rings for rings, way, _ in r.polygons if way == "increase")
    xs, ys = [x for x, _ in rings[0]], [y for _, y in rings[0]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (500200, 500400, 3999600, 3999800)


def test_small_changes_and_a_typed_threshold(tmp_path):
    before, after = pair(tmp_path)
    # Only the larger area is at least 200 px; the threshold 150 leaves the dark one out anyway.
    r = io.change_files(before, 1, after, 1, str(tmp_path / "c.tif"), method_of("difference"), resample="nearest", min_pixels=200)
    assert [(w, p) for _, w, p in r.polygons] == [("increase", 400)]
    r = io.change_files(before, 1, after, 1, str(tmp_path / "d.tif"), method_of("difference"), resample="nearest", threshold=150, direction="decrease")
    assert r.polygons == [] and r.thresholds.low == -150


def test_another_crs_is_reprojected_and_no_overlap_says_so(tmp_path):
    before, _ = pair(tmp_path)
    far = write(tmp_path / "far.tif", np.ones((10, 10), np.float32), [900000, 10, 0, 4000000, 0, -10])
    with pytest.raises(io.ChangeError, match="重なりなし"):
        io.describe(before, far)
    # The same place in WGS 84: compared on the before image's UTM grid.
    wgs = write(tmp_path / "wgs.tif", np.full((200, 200), 150, np.float32), [140.99, 0.0001, 0, 36.14, 0, -0.0001], epsg=4326)
    r = io.change_files(before, 1, wgs, 1, str(tmp_path / "w.tif"), method_of("ratio"), outline=False)
    assert gdal.Open(str(tmp_path / "w.tif")).GetGeoTransform()[1] == 10 and r.polygons == []


def test_the_algorithm_makes_the_image_and_the_outlines(tmp_path):
    from satellite_observation.processing_provider.change_algorithm import default_inputs
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    try:
        algorithm = QgsApplication.processingRegistry().algorithmById("satobs:change")
        before_path, after_path = pair(tmp_path)
        before, after = QgsRasterLayer(before_path, "before"), QgsRasterLayer(after_path, "after")
        QgsProject.instance().addMapLayers([after, before])
        times = {before.id(): 1, after.id(): 2}
        assert default_inputs([after, before], lambda layer: times[layer.id()]) == {"BEFORE": before.id(), "AFTER": after.id()}
        out = str(tmp_path / "result.tif")
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        context.addLayerToLoadOnCompletion(out, QgsProcessingContext.LayerDetails("result", QgsProject.instance(), "OUTPUT"))
        feedback = QgsProcessingFeedback()
        results, ok = algorithm.run({"BEFORE": before.id(), "AFTER": after.id(), "RESAMPLE": 1, "OUTPUT": out,
                                     "OUTLINES": "memory:"}, context, feedback)
        assert ok and results["OUTPUT"] == out
        outlines = context.getMapLayer(results["OUTLINES"])
        assert isinstance(outlines, QgsVectorLayer) and outlines.featureCount() == 2
        areas = sorted((f["方向"], f["画素数"], round(f["面積_m2"], -2)) for f in outlines.getFeatures())
        assert [a[:2] for a in areas] == [("増加", 400), ("減少", 100)] and 39000 <= areas[0][2] <= 41000
        layer = QgsRasterLayer(out, "x")
        context.layerToLoadOnCompletionDetails(out).postProcessor().postProcessLayer(layer, context, feedback)
        assert layer.name() == "before_after_difference" and layer.renderer().type() == "singlebandpseudocolor"
        shader = layer.renderer().shader().rasterShaderFunction()
        assert shader.minimumValue() == -shader.maximumValue() and shader.maximumValue() > 0
    finally:
        QgsApplication.processingRegistry().removeProvider(provider)
        QgsProject.instance().removeAllMapLayers()


def test_the_algorithm_refuses_the_same_band_twice(tmp_path):
    # Apart from the run above: the refused run's exception keeps the frame that called it alive until Python exits, and a
    # raster layer still referenced from it would be deleted after GDAL is gone.
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    try:
        algorithm = QgsApplication.processingRegistry().algorithmById("satobs:change")
        before = QgsRasterLayer(pair(tmp_path)[0], "before")
        QgsProject.instance().addMapLayer(before)
        _, ok = algorithm.run({"BEFORE": before.id(), "AFTER": before.id(), "OUTPUT": str(tmp_path / "x.tif")}, QgsProcessingContext(),
                              QgsProcessingFeedback())
        assert not ok
    finally:
        QgsApplication.processingRegistry().removeProvider(provider)
        QgsProject.instance().removeAllMapLayers()
