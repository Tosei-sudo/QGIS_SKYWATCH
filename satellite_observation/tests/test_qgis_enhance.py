"""
The correction drawn by QGIS: the renderer on 8-bit and 16-bit GeoTIFFs,
DRA from the view, sharpening without seams between parts, no data, and the
project round trip (QGIS's own renderer in the file, the correction back
with the plugin).
"""
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsMapRendererParallelJob, QgsMapSettings, QgsMultiBandColorRenderer, QgsSingleBandGrayRenderer, QgsProject, QgsRasterLayer, QgsRectangle,
)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

from satellite_observation.core.enhance import Pipeline, process  # noqa: E402
from satellite_observation.core.enhance.raster import compute_raster_stretch, sample_raster_histogram, stretch_to_rgba, valid_mask  # noqa: E402
from satellite_observation.qgis_io import enhance_renderer as er  # noqa: E402

gdal.UseExceptions()


def write_tif(path, arrays, dtype=gdal.GDT_Byte, no_data=None):
    h, w = arrays[0].shape
    ds = gdal.GetDriverByName("GTiff").Create(str(path), w, h, len(arrays), dtype)
    ds.SetGeoTransform([1000, 1, 0, 2000, 0, -1])
    for i, a in enumerate(arrays):
        band = ds.GetRasterBand(i + 1)
        if no_data is not None:
            band.SetNoDataValue(no_data)
        band.WriteArray(a)
    ds = None
    return str(path)


def color8(h=60, w=80, seed=1):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    return [np.clip(x * 2 + 40 + rng.normal(0, 12, (h, w)), 0, 255).astype(np.uint8),
            np.clip(y * 2 + 50 + rng.normal(0, 12, (h, w)), 0, 255).astype(np.uint8),
            np.clip((x + y) + 30 + rng.normal(0, 12, (h, w)), 0, 255).astype(np.uint8)]


def draw(r: er.EnhanceRenderer, extent: QgsRectangle, w: int, h: int) -> np.ndarray:
    """The renderer's block as H×W×4 RGBA (un-premultiplied for opaque pixels)."""
    block = r.block(1, extent, w, h)
    argb = np.frombuffer(bytes(block.data()), dtype=np.uint32).reshape(h, w)
    return np.dstack([(argb >> 16) & 255, (argb >> 8) & 255, argb & 255, argb >> 24]).astype(np.uint8)


def layer_of(path):
    layer = QgsRasterLayer(path, os.path.basename(path))
    assert layer.isValid()
    return layer


def test_eight_bit_correction_is_the_engine_s(tmp_path):
    arrays = color8()
    layer = layer_of(write_tif(tmp_path / "c.tif", arrays))
    p = Pipeline().add("exposure", 0.4).add("saturation", 0.3).add("contrast", 0.2)
    r = er.apply(layer, er.EnhanceSettings(bands=(1, 2, 3), pipeline=p.to_json()))
    got = draw(r, layer.extent(), 80, 60)
    rgba = np.dstack(arrays + [np.full((60, 80), 255, np.uint8)])
    assert np.array_equal(got, process(rgba, p))


def test_dra_takes_the_view_and_locks(tmp_path):
    a = color8()
    a[0][:, :40] = 10   # dark left half
    layer = layer_of(write_tif(tmp_path / "d.tif", a))
    p = Pipeline().add("autoStretch", {"method": "minMax"})
    r = er.apply(layer, er.EnhanceSettings(bands=(1, 2, 3), pipeline=p.to_json()))
    right = QgsRectangle(1040, 1940, 1080, 2000)
    er.set_view(layer, right)
    on_right = draw(r, right, 40, 60)
    # The right half alone, stretched min..max on itself.
    assert on_right[..., 0].min() == 0 and on_right[..., 0].max() == 255
    # Locked: the whole image keeps the right half's range (the dark half clips to 0).
    r.settings = er.EnhanceSettings(bands=(1, 2, 3), pipeline=p.to_json(), locked=True)
    er.set_view(layer, layer.extent())
    whole = draw(r, layer.extent(), 80, 60)
    assert (whole[:, :40, 0] == 0).all()
    r.settings = er.EnhanceSettings(bands=(1, 2, 3), pipeline=p.to_json(), locked=False)
    whole = draw(r, layer.extent(), 80, 60)
    assert whole[:, :40, 0].max() == 0 and whole[:, 40:, 0].min() > 0


def test_sixteen_bit_stretches_raw_values(tmp_path):
    rng = np.random.default_rng(2)
    bands = [rng.normal(8000 + 500 * i, 400, (50, 70)).astype(np.uint16) for i in range(4)]
    bands[0][:5, :] = 0
    layer = layer_of(write_tif(tmp_path / "s.tif", bands, gdal.GDT_UInt16, no_data=0))
    r = er.apply(layer, er.EnhanceSettings(bands=(3, 2, 1)))
    got = draw(r, layer.extent(), 70, 50)
    # Without DRA: the whole image's 2–98 %, from the raw values.
    picture = [b.astype(np.float64) for b in (bands[2], bands[1], bands[0])]
    valid = valid_mask(picture, [0])
    s = compute_raster_stretch(sample_raster_histogram(picture, valid), er.WHOLE_STRETCH)
    assert np.array_equal(got, stretch_to_rgba(picture, valid, s["black"], s["white"]))
    assert (got[:5, :, 3] == 0).all() and (got[5:, :, 3] == 255).all()


def test_sharpen_has_no_seams_between_parts(tmp_path):
    arrays = color8(64, 64)
    layer = layer_of(write_tif(tmp_path / "p.tif", arrays))
    p = Pipeline().add("sharpen", {"amount": 1.5, "radius": 1.2})
    r = er.apply(layer, er.EnhanceSettings(bands=(1, 2, 3), pipeline=p.to_json()))
    whole = draw(r, layer.extent(), 64, 64)
    left = draw(r, QgsRectangle(1000, 1936, 1032, 2000), 32, 64)
    right = draw(r, QgsRectangle(1032, 1936, 1064, 2000), 32, 64)
    assert np.array_equal(np.hstack([left, right]), whole)


def test_single_band_gray_and_the_map_render(tmp_path):
    a = color8()[0]
    layer = layer_of(write_tif(tmp_path / "g.tif", [a]))
    er.apply(layer, er.EnhanceSettings(bands=(1,), pipeline=Pipeline().add("contrast", 0.5).to_json()))
    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setExtent(layer.extent())
    ms.setOutputSize(QSize(80, 60))
    ms.setBackgroundColor(QColor(0, 0, 0))
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()
    c = img.pixelColor(40, 30)
    assert c.red() == c.green() == c.blue()
    assert not job.errors()


def test_project_keeps_qgis_renderer_and_the_correction(tmp_path):
    rng = np.random.default_rng(3)
    path = write_tif(tmp_path / "r.tif", [rng.normal(3000, 200, (40, 40)).astype(np.uint16) for _ in range(3)], gdal.GDT_UInt16)
    layer = layer_of(path)
    project = QgsProject()
    project.addMapLayer(layer)
    settings = er.EnhanceSettings(bands=(1, 2, 3), pipeline=Pipeline().add("temperature", 0.3).to_json())
    er.apply(layer, settings)
    qgs = str(tmp_path / "p.qgs")
    assert project.write(qgs)

    other = QgsProject()
    assert other.read(qgs)
    opened = list(other.mapLayers().values())[0]
    # Without the plugin: QGIS's own renderer with the same stretch.
    assert isinstance(opened.renderer(), QgsMultiBandColorRenderer)
    ce = opened.renderer().redContrastEnhancement()
    assert 2000 < ce.minimumValue() < ce.maximumValue() < 4000
    assert er.restore_layer(opened)
    assert er.enhance_renderer(opened).settings == settings

    er.remove(opened)
    assert isinstance(opened.renderer(), QgsMultiBandColorRenderer)
    assert not opened.customProperty(er.PROPERTY)


def test_the_dock_corrects_the_active_layer_and_follows_the_view(tmp_path):
    from satellite_observation.qgis_io.context import PluginContext
    from satellite_observation.tests.qgis_fakes import FakeIface
    from satellite_observation.ui.enhance_dock import EnhanceDock

    iface = FakeIface()
    rng = np.random.default_rng(4)
    layer = layer_of(write_tif(tmp_path / "m.tif", [rng.normal(2000 + 300 * i, 200, (50, 60)).astype(np.uint16) for i in range(4)], gdal.GDT_UInt16))
    project = QgsProject.instance()
    project.addMapLayer(layer)
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    iface.canvas.setExtent(layer.extent())
    dock = EnhanceDock(iface, PluginContext(iface.window), lambda: None, iface.window)
    try:
        iface.setActiveLayer(layer)
        assert dock.layer is layer and not dock.enabled.isChecked()
        assert [dock.band_combos[i].currentData() for i in range(3)] == [1, 2, 3]
        assert dock.band_combos[0].count() == 4

        # A preset turns the correction on; its steps land on the sliders and DRA.
        dock.use_preset("vivid")
        dock.apply()
        r = er.enhance_renderer(layer)
        assert r is not None
        assert [op["op"] for op in r.settings.correction.ops] == ["autoStretch", "contrast", "saturation", "sharpen"]
        assert r.state.view is not None

        # Sliders, bands (4-3-2) and DRA settings, as the viewer's panel builds them.
        row = next(x for x in dock.rows if x.op == "exposure")
        row.slider.setValue(row.slider.value() + 10)
        for combo, band in zip(dock.band_combos, (4, 3, 2)):
            combo.setCurrentIndex(band - 1)
        dock.dra_linked.setChecked(True)
        dock.apply()
        s = er.enhance_renderer(layer).settings
        ops = s.correction.ops
        assert s.bands == (4, 3, 2)
        assert ops[0]["op"] == "autoStretch" and ops[0]["linked"] is True
        assert ops[1] == {"op": "exposure", "ev": pytest.approx(0.5)}

        # The same band thrice: gray, and the colour sliders hide.
        for combo in dock.band_combos:
            combo.setCurrentIndex(0)
        dock.apply()
        assert er.enhance_renderer(layer).settings.bands == (1,)
        assert next(x for x in dock.rows if x.op == "saturation").label.isHidden()

        # A tone curve from a script survives the sliders.
        dock.enhance(layer, {"version": 1, "ops": [{"op": "curve", "points": [[0, 0], [0.5, 0.6], [1, 1]]}, {"op": "contrast", "amount": 0.1}]})
        assert not dock.extra_label.isHidden()
        dock.apply()
        assert [op["op"] for op in er.enhance_renderer(layer).settings.correction.ops] == ["contrast", "curve"]

        # Off: QGIS's own renderer again (one band shown: single-band gray).
        dock.enabled.setChecked(False)
        dock.apply()
        assert er.enhance_renderer(layer) is None
        assert isinstance(layer.renderer(), QgsSingleBandGrayRenderer)
    finally:
        dock.close_down()
        project.removeAllMapLayers()


def test_strips_in_threads_give_the_whole_image_s_pixels(tmp_path):
    arrays = color8(600, 300, seed=9)
    layer = layer_of(write_tif(tmp_path / "t.tif", arrays))
    p = Pipeline().add("contrast", 0.2).add("saturation", 0.3).add("sharpen", {"amount": 1, "radius": 2}).add("gamma", 1.2)
    r = er.apply(layer, er.EnhanceSettings(bands=(1, 2, 3), pipeline=p.to_json()))
    got = draw(r, layer.extent(), 300, 600)
    rgba = np.dstack(arrays + [np.full((600, 300), 255, np.uint8)])
    assert er.THREADS == 1 or 600 // er.STRIP > 1
    assert np.array_equal(got, process(rgba, p))
