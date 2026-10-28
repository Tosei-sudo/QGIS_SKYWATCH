"""
自動位置合わせ in QGIS: a copy of an image shifted (or reprojected) is found
off by the right amount, and the aligned copy lines up with the reference.
"""
import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer  # noqa: E402

from satellite_observation.qgis_io import align_io as io  # noqa: E402

RNG = np.random.default_rng(7)
#: Texture of 600 × 600 px (smoothed noise), the reference cut from it at (50, 50).
BIG = RNG.random((600, 600))
BIG = (BIG + np.roll(BIG, 1, 0) + np.roll(BIG, 1, 1) + np.roll(BIG, -1, 0) + np.roll(BIG, -1, 1)) / 5 * 1000


def write(path, array, gt, epsg=32654, bands=1):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], bands, gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(bands):
        ds.GetRasterBand(b + 1).WriteArray(array * (b + 1))
    ds = None
    return str(path)


def pair(tmp_path, dx=7, dy=-4):
    """Reference: BIG[50:450, 50:450] at 2 m. Target: the same ground with its georeferencing off by (dx, dy) px east, north."""
    ref = write(tmp_path / "ref.tif", BIG[50:450, 50:450], [400000, 2, 0, 3900000, 0, -2])
    # Cut at column 40, row 60: rightly placed at (399980, 3899980); its georeferencing is off by (dx, dy) px.
    tgt = write(tmp_path / "tgt.tif", BIG[60:460, 40:440], [399980 + 2 * dx, 2, 0, 3899980 + 2 * dy, 0, -2], bands=2)
    return ref, tgt


def test_finds_the_shift_and_lines_the_copy_up(tmp_path):
    ref, tgt = pair(tmp_path)
    assert io.describe(ref, tgt).startswith("重なる範囲 ")
    a = io.find_alignment(ref, 1, tgt, 1, "shift")
    assert a.transform[0] == pytest.approx(14, abs=0.3) and a.transform[3] == pytest.approx(-8, abs=0.3)
    # 4 × 4 patches of 128 px over 386 px: the 4 inner ones fit.
    assert len(a.ties) == 4 and a.rms_pixels < 0.2
    out = str(tmp_path / "aligned.tif")
    io.aligned_copy(ref, tgt, a, out, "bilinear")
    ds = gdal.Open(out)
    assert ds.RasterCount == 2 + 1 and ds.GetGeoTransform()[1] == 2  # every band, plus alpha (no no-data value)
    # The aligned copy matches the reference where both are.
    ref_ds = gdal.Open(ref)
    gt, rgt = ds.GetGeoTransform(), ref_ds.GetGeoTransform()
    # Reference pixel (C, R) is the copy's (C + co, R + ro).
    co, ro = round((rgt[0] - gt[0]) / 2), round((gt[3] - rgt[3]) / 2)
    copy = ds.GetRasterBand(1).ReadAsArray()
    ref_ds = gdal.Open(ref)
    reference = ref_ds.GetRasterBand(1).ReadAsArray()
    assert np.abs(copy[100 + ro:300 + ro, 100 + co:300 + co] - reference[100:300, 100:300]).mean() < 5


def test_an_affine_and_a_flat_image(tmp_path):
    ref, tgt = pair(tmp_path, dx=3, dy=2)
    a = io.find_alignment(ref, 1, tgt, 1, "affine", patches=4)
    assert a.transform[1] == pytest.approx(1, abs=1e-3) and a.transform[0] + a.transform[2] * 3900000 != 0
    flat = write(tmp_path / "flat.tif", np.full((400, 400), 5.0), [400000, 2, 0, 3900000, 0, -2])
    with pytest.raises(io.AlignError, match="一致する模様なし"):
        io.find_alignment(ref, 1, flat, 1)


def test_the_algorithm(tmp_path):
    from satellite_observation.processing_provider.align_algorithm import default_inputs
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    try:
        algorithm = QgsApplication.processingRegistry().algorithmById("satobs:align")
        ref_path, tgt_path = pair(tmp_path)
        ref, tgt = QgsRasterLayer(ref_path, "ref"), QgsRasterLayer(tgt_path, "tgt")
        QgsProject.instance().addMapLayers([ref, tgt])
        assert default_inputs([ref, tgt], tgt) == {"TARGET": tgt.id(), "REFERENCE": ref.id()}
        out = str(tmp_path / "result.tif")
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        context.addLayerToLoadOnCompletion(out, QgsProcessingContext.LayerDetails("result", QgsProject.instance(), "OUTPUT"))
        infos = []

        class Feedback(QgsProcessingFeedback):
            def pushInfo(self, info):  # noqa: N802 (QGIS's name)
                infos.append(info)

        results, ok = algorithm.run({"REFERENCE": ref.id(), "TARGET": tgt.id(), "OUTPUT": out}, context, Feedback())
        assert ok and gdal.Open(out) is not None
        assert any(i.startswith("ずれ 東 14.0 m・北 -8.0 m") for i in infos)
        layer = QgsRasterLayer(out, "x")
        context.layerToLoadOnCompletionDetails(out).postProcessor().postProcessLayer(layer, context, QgsProcessingFeedback())
        assert layer.name() == "tgt_aligned"
    finally:
        QgsApplication.processingRegistry().removeProvider(provider)
        QgsProject.instance().removeAllMapLayers()
