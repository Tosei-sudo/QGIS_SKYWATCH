"""
パンシャープン: the computation (core.enhance.pansharpen; the library's own
results are compared in scripts/compare_viewer/compare_pansharpen.py), the
files made with GDAL a block at a time, and the Processing algorithm with
its result layer keeping the multispectral layer's 画像補正.
"""
import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer,
)

from satellite_observation.core.enhance.pansharpen import PanSharpenModel, pan_sharpen  # noqa: E402
from satellite_observation.qgis_io import enhance_renderer as er  # noqa: E402
from satellite_observation.qgis_io import pansharpen_io as io  # noqa: E402

gdal.UseExceptions()


def scene(h, w, seed=3):
    """Four smooth bands and a sharp panchromatic band made from them with fine detail."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    base = [2000 + 700 * np.sin(x / (17 + 6 * k) + k) + 400 * np.cos(y / (29 - 4 * k) - k) for k in range(4)]
    detail = 250 * np.sign(np.sin(x / 2.5) * np.cos(y / 3))
    pan = 0.1 * base[0] + 0.2 * base[1] + 0.3 * base[2] + 0.4 * base[3] + detail + rng.normal(0, 5, (h, w))
    return pan, np.stack(base)


def write(path, arrays, gt, dtype=gdal.GDT_UInt16, no_data=None, epsg=32654, alpha=False):
    count, h, w = arrays.shape
    ds = gdal.GetDriverByName("GTiff").Create(str(path), w, h, count, dtype)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    for i in range(count):
        band = ds.GetRasterBand(i + 1)
        if no_data is not None:
            band.SetNoDataValue(no_data)
        band.SetDescription(f"B{i + 1}")
        band.WriteArray(arrays[i])
    if alpha:
        ds.GetRasterBand(count).SetColorInterpretation(gdal.GCI_AlphaBand)
    ds = None
    return str(path)


def pair(tmp_path, h=240, w=320, no_data=0, alpha=False):
    """pan at 1 m and ms at 4 m (made by averaging 4 × 4), ms shifted 20 m east so they overlap in part."""
    pan, base = scene(h, w)
    ms = base.reshape(4, h // 4, 4, w // 4, 4).mean(axis=(2, 4))
    ms[:, :2, :] = 0  # no data along the top
    if alpha:
        ms = np.concatenate([ms[:3] / 16, np.full((1, h // 4, w // 4), 255.0)])
        ms[3, 10:12, 10:12] = 0
    pan_path = write(tmp_path / "pan.tif", np.clip(pan, 0, 65535).astype(np.uint16)[None], [500020, 1, 0, 4000000, 0, -1], no_data=no_data)
    dtype, kind = (gdal.GDT_Byte, np.uint8) if alpha else (gdal.GDT_UInt16, np.uint16)
    ms_path = write(tmp_path / "ms.tif", np.clip(ms, 0, 65535).astype(kind), [500040, 4, 0, 4000000, 0, -4], dtype=dtype,
                    no_data=None if alpha else no_data, alpha=alpha)
    return pan_path, ms_path


# ---- the computation ----

def test_strength_zero_is_the_resampled_image_and_the_detail_comes_in_with_it():
    pan, base = scene(64, 64)
    ms = np.clip(np.repeat(np.repeat(base[:, ::4, ::4], 4, axis=1), 4, axis=2), 0, 65535).astype(np.uint16)
    pan16 = np.clip(pan, 0, 65535).astype(np.uint16)
    same, _, _ = pan_sharpen(pan16, ms, strength=0)
    assert np.array_equal(same, ms)
    for method in ("gram-schmidt", "ihs", "brovey"):
        sharp, no_data, model = pan_sharpen(pan16, ms, method)
        assert sharp.dtype == np.uint16 and no_data == 0
        # The fine detail of pan is in every band now.
        for b in range(4):
            assert np.corrcoef(sharp[b].astype(float) - ms[b], pan - pan.mean())[0, 1] > 0.5
    assert sum(model.weights) == pytest.approx(1)


def test_auto_weights_find_the_mix_of_bands_pan_was_made_of():
    pan, base = scene(128, 128)
    _, _, model = pan_sharpen(pan.astype(np.float32), base.astype(np.float32))
    assert model.weights == pytest.approx([0.1, 0.2, 0.3, 0.4], abs=0.03)
    _, _, equal = pan_sharpen(pan.astype(np.float32), base.astype(np.float32), weights="equal")
    assert equal.weights == [0.25] * 4


def test_no_data_stays_and_a_valid_pixel_never_becomes_no_data():
    pan, base = scene(32, 32)
    ms = np.clip(base / 10, 0, 255).astype(np.uint8)
    ms[:, 5, 5] = 9
    ms[:, 0, :] = 9
    out, no_data, _ = pan_sharpen(np.clip(pan / 10, 0, 255).astype(np.uint8), ms, "ihs", strength=2, ms_no_data=9)
    assert no_data == 9
    assert (out[:, 0, :] == 9).all() and (out[:, 5, 5] == 9).all()
    rest = np.ones((32, 32), bool)
    rest[0, :] = False
    rest[5, 5] = False
    assert (out[:, rest] != 9).all()


def test_one_model_for_every_block_joins_without_seams():
    pan, base = scene(64, 96)
    whole, _, model = pan_sharpen(pan, base, "gram-schmidt")
    left, _, _ = pan_sharpen(pan[:, :40], base[:, :, :40], "gram-schmidt", model=model)
    right, _, _ = pan_sharpen(pan[:, 40:], base[:, :, 40:], "gram-schmidt", model=model)
    assert np.allclose(np.concatenate([left, right], axis=2), whole)
    with pytest.raises(ValueError):
        pan_sharpen(pan, base, model=PanSharpenModel([1], [1], 1, 0))


# ---- files ----

def test_the_file_covers_the_overlap_at_pan_resolution_with_the_ms_bands(tmp_path):
    pan_path, ms_path = pair(tmp_path)
    out = str(tmp_path / "out.tif")
    steps = []
    result = io.pan_sharpen_files(pan_path, ms_path, out, progress=lambda p, t: steps.append(t))
    ds = gdal.Open(out)
    # ms starts 20 m east of pan: pan columns 20.. (ms ends east of pan, so to pan's edge).
    assert (ds.RasterXSize, ds.RasterYSize, ds.RasterCount) == (300, 240, 4) == (result["width"], result["height"], result["bands"])
    assert ds.GetGeoTransform() == (500040, 1, 0, 4000000, 0, -1)
    band = ds.GetRasterBand(1)
    assert band.DataType == gdal.GDT_UInt16 and band.GetNoDataValue() == 0 and band.GetDescription() == "B1"
    assert band.GetBlockSize() == [256, 256] and band.GetOverviewCount() >= 1
    assert band.GetMetadataItem("STATISTICS_MINIMUM") is not None
    data = ds.ReadAsArray()
    # The ms rows of no data stay no data (the 8 pan rows under them), the rest has values.
    assert (data[:, :6, :] == 0).all() and (data[:, 12:, 4:-4] > 0).all()
    assert steps[0] == "色の関係を調査中…" and steps[-1] == "完了"


def test_blocks_and_fit_windows_give_the_same_file_as_one_go(tmp_path, monkeypatch):
    pan_path, ms_path = pair(tmp_path)
    one = str(tmp_path / "one.tif")
    io.pan_sharpen_files(pan_path, ms_path, one, method="ihs")
    monkeypatch.setattr(io, "BLOCK", 64)
    blocks = str(tmp_path / "blocks.tif")
    io.pan_sharpen_files(pan_path, ms_path, blocks, method="ihs")
    assert np.array_equal(gdal.Open(one).ReadAsArray(), gdal.Open(blocks).ReadAsArray())
    # A large overlap is fitted on windows spread over it: nearly the same model.
    monkeypatch.setattr(io, "FIT_WHOLE", 100)
    windows = io.pan_sharpen_files(pan_path, ms_path, str(tmp_path / "windows.tif"), method="ihs")["model"]
    whole = io.pan_sharpen_files(pan_path, ms_path, str(tmp_path / "whole.tif"), method="ihs")
    assert io.sample_windows(io.Overlap(0, 0, 300, 240)) == [(22, 0, 278, 240)]
    assert len(io.sample_windows(io.Overlap(0, 0, 5000, 3000))) == 64
    assert windows.weights == pytest.approx(whole["model"].weights, abs=0.05)


def test_an_alpha_band_is_kept_and_another_crs_is_reprojected(tmp_path):
    pan_path, ms_path = pair(tmp_path, alpha=True)
    out = str(tmp_path / "alpha.tif")
    io.pan_sharpen_files(pan_path, ms_path, out, resample="bicubic")
    ds = gdal.Open(out)
    assert ds.RasterCount == 4 and ds.GetRasterBand(4).GetColorInterpretation() == gdal.GCI_AlphaBand
    assert ds.GetRasterBand(1).DataType == gdal.GDT_Byte
    # ms in WGS 84 over the same ground.
    ll = str(tmp_path / "ll.tif")
    gdal.Warp(ll, ms_path, dstSRS="EPSG:4326")
    io.pan_sharpen_files(pan_path, ll, str(tmp_path / "ll_out.tif"))
    assert gdal.Open(str(tmp_path / "ll_out.tif")).RasterXSize > 250


def test_no_overlap_says_so(tmp_path):
    pan, base = scene(40, 40)
    pan_path = write(tmp_path / "p.tif", pan.astype(np.uint16)[None], [0, 1, 0, 100, 0, -1])
    ms_path = write(tmp_path / "m.tif", base[:, ::4, ::4].astype(np.uint16), [1000, 4, 0, 100, 0, -4])
    with pytest.raises(io.PanSharpenError, match="重なりなし"):
        io.pan_sharpen_files(pan_path, ms_path, str(tmp_path / "o.tif"))


# ---- the Processing algorithm ----

@pytest.fixture
def algorithm():
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    yield QgsApplication.processingRegistry().algorithmById("satobs:pansharpen")
    QgsApplication.processingRegistry().removeProvider(provider)
    QgsProject.instance().removeAllMapLayers()


def test_the_algorithm_makes_the_file_and_the_layer_keeps_the_ms_correction(tmp_path, algorithm):
    from satellite_observation.processing_provider.pansharpen_algorithm import default_inputs

    pan_path, ms_path = pair(tmp_path)
    pan = QgsRasterLayer(pan_path, "pan")
    ms = QgsRasterLayer(ms_path, "ms")
    QgsProject.instance().addMapLayers([pan, ms])
    er.apply(ms, er.EnhanceSettings(bands=(3, 2, 1), pipeline={"version": 1, "ops": [{"op": "saturation", "amount": 0.4}]}))
    inputs = default_inputs([ms, pan], None)
    assert inputs == {"PAN": pan.id(), "MS": ms.id()}

    out = str(tmp_path / "result.tif")
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    context.addLayerToLoadOnCompletion(out, QgsProcessingContext.LayerDetails("result", QgsProject.instance(), "OUTPUT"))
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run({**inputs, "METHOD": 1, "STRENGTH": 80, "OUTPUT": out}, context, feedback)
    assert ok and results["OUTPUT"] == out
    assert gdal.Open(out).RasterXSize == 300
    post = context.layerToLoadOnCompletionDetails(out).postProcessor()
    layer = QgsRasterLayer(out, "result")
    QgsProject.instance().addMapLayer(layer)
    post.postProcessLayer(layer, context, feedback)
    assert layer.name() == "ms_pansharpen"
    kept = er.enhance_renderer(layer)
    assert kept is not None and kept.settings.bands == (3, 2, 1)
    assert kept.settings.pipeline == er.enhance_renderer(ms).settings.pipeline


def test_the_algorithm_wants_two_different_images(tmp_path, algorithm):
    pan_path, _ = pair(tmp_path)
    pan = QgsRasterLayer(pan_path, "pan")
    QgsProject.instance().addMapLayer(pan)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    errors = []

    class Feedback(QgsProcessingFeedback):
        def reportError(self, error, fatal=False):  # noqa: N802 (QGIS's name)
            errors.append(error)

    _, ok = algorithm.run({"PAN": pan.id(), "MS": pan.id(), "OUTPUT": str(tmp_path / "x.tif")}, context, Feedback())
    assert not ok and errors == ["別々のパンクロ画像とマルチスペクトル画像を選択"]
