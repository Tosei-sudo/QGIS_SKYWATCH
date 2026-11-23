"""
MIKAGE の速い描画（試作, qgis_io/fast_draw.py): the very pixels QGIS's multiband colour and single band gray renderers
give (byte for byte, every enhancement, opacity, alpha band, no data, the layer's empty transparency), what is left to
QGIS, the layers of the project drawn so (added later, after a style change, saved as QGIS's own), and the switch in
「地図の描画の速さ」.
"""
import gc
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal  # noqa: E402
from qgis.core import (  # noqa: E402
    Qgis, QgsContrastEnhancement, QgsMapRendererSequentialJob, QgsMapSettings, QgsMultiBandColorRenderer, QgsProject,
    QgsRasterLayer, QgsRasterTransparency, QgsRectangle, QgsSettings, QgsSingleBandGrayRenderer,
)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

from mikage.qgis_io import fast_draw  # noqa: E402
from mikage.qgis_io.fast_draw import FastGray, FastMultiBand  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.features.rendering import Rendering  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402

CE = QgsContrastEnhancement.ContrastEnhancementAlgorithm
ALGORITHMS = [CE.NoEnhancement, CE.StretchToMinimumMaximum, CE.StretchAndClipToMinimumMaximum, CE.ClipToMinimumMaximum]
TYPES = [(np.uint8, gdal.GDT_Byte, Qgis.DataType.Byte), (np.uint16, gdal.GDT_UInt16, Qgis.DataType.UInt16),
         (np.int16, gdal.GDT_Int16, Qgis.DataType.Int16)]
if hasattr(gdal, "GDT_Int8") and hasattr(Qgis.DataType, "Int8"):
    TYPES.append((np.int8, gdal.GDT_Int8, Qgis.DataType.Int8))


@pytest.fixture(autouse=True)
def clean():
    QgsSettings().remove("mikage/" + fast_draw.KEY)
    yield
    QgsProject.instance().clear()
    QgsSettings().remove("mikage/" + fast_draw.KEY)


def image(path, dtype, gdal_type, bands=3, size=(64, 48), no_data=None, seed=1):
    """Random values over the type's whole range, with many zeros and small values (where stretches and clips turn)."""
    w, h = size
    rng = np.random.default_rng(seed)
    info = np.iinfo(dtype)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), w, h, bands, gdal_type)
    ds.SetGeoTransform((1000, 1, 0, 2000, 0, -1))
    for b in range(bands):
        a = rng.integers(info.min, info.max, size=(h, w), endpoint=True, dtype=np.int64)
        a[rng.random((h, w)) < 0.15] = 0
        small = rng.random((h, w)) < 0.15
        a[small] = rng.integers(max(info.min, -50), min(info.max, 400), size=int(small.sum()))
        if bands == 4 and b == 3:
            a = rng.choice([0, 255, 128, 37], size=(h, w))  # an alpha band
        band = ds.GetRasterBand(b + 1)
        band.WriteArray(a.astype(dtype))
        if no_data is not None:
            band.SetNoDataValue(no_data)
    ds = None
    layer = QgsRasterLayer(str(path), os.path.basename(str(path)))
    assert layer.isValid()
    return layer


def enhancement(data_type, algorithm, low, high):
    ce = QgsContrastEnhancement(data_type)
    ce.setContrastEnhancementAlgorithm(algorithm)
    ce.setMinimumValue(low)
    ce.setMaximumValue(high)
    return ce


def pixels(renderer, extent, w, h) -> bytes:
    block = renderer.block(1, extent, w, h)  # (held while its bytes are read: data() is a view of it)
    return bytes(block.data())


def same(renderer, extent, w, h, drawn=True):
    """QGIS's own renderer and MIKAGE's give the same bytes (and MIKAGE drew them, unless `drawn` is False)."""
    if isinstance(renderer, QgsMultiBandColorRenderer):
        fast, argb_of = fast_draw.multiband_copy(renderer, FastMultiBand), fast_draw.multiband_argb
    else:
        fast, argb_of = fast_draw.gray_copy(renderer, FastGray), fast_draw.gray_argb
    expected = pixels(renderer, extent, w, h)
    assert pixels(fast, extent, w, h) == expected
    if drawn:
        assert argb_of(fast, extent, w, h).tobytes() == expected
    else:
        with pytest.raises(fast_draw.Unsupported):
            argb_of(fast, extent, w, h)
    return expected


@pytest.mark.parametrize("dtype,gdal_type,data_type", TYPES, ids=[np.dtype(t[0]).name for t in TYPES])
def test_multiband_pixels_are_qgis_own(tmp_path, dtype, gdal_type, data_type):
    info = np.iinfo(dtype)
    lo, hi = float(info.min), float(info.max)
    ranges = [(lo, hi), (0.0, 200.0), (lo + (hi - lo) * 0.2, lo + (hi - lo) * 0.7), (13.4, 251.6)]
    plain = image(tmp_path / "plain.tif", dtype, gdal_type)
    nodata = image(tmp_path / "nodata.tif", dtype, gdal_type, no_data=0, seed=2)
    alpha = image(tmp_path / "alpha.tif", dtype, gdal_type, bands=4, seed=3)
    outside = QgsRectangle(980, 1940, 1070, 2010)  # partly off the image: QGIS's no-data bitmap
    drawn = 0
    for n, (algorithm, (low, high)) in enumerate([(a, r) for a in ALGORITHMS for r in ranges]):
        for layer, bands, alpha_band in ((plain, (1, 2, 3), -1), (nodata, (3, 1, 2), -1), (alpha, (1, 2, 3), 4),
                                         (plain, (2, -1, 1), -1)):
            r = QgsMultiBandColorRenderer(layer.dataProvider(), *bands)
            # Each band its own enhancement (QGIS asks them all whether the *red* value is displayable).
            r.setRedContrastEnhancement(enhancement(data_type, algorithm, low, high))
            r.setGreenContrastEnhancement(enhancement(data_type, ALGORITHMS[(n + 1) % 4], *ranges[(n + 2) % 4]))
            if n % 3:
                r.setBlueContrastEnhancement(enhancement(data_type, ALGORITHMS[(n + 3) % 4], *ranges[(n + 1) % 4]))
            r.setAlphaBand(alpha_band)
            r.setOpacity((1.0, 0.6, 0.3, 0.9999999)[n % 4])
            if n % 2:
                r.setRasterTransparency(QgsRasterTransparency())  # what the layer's properties leave (opacity as an int of 255)
            if n % 5 == 0:
                r.setNodataColor(QColor(10, 200, 30, 120))
            same(r, layer.extent(), 64, 48)
            same(r, outside, 50, 40)
            drawn += 1
    assert drawn == 64
    # Three bands without enhancement (QGIS's short way), and a big area drawn in parts by threads.
    r = QgsMultiBandColorRenderer(plain.dataProvider(), 3, 2, 1)
    same(r, plain.extent(), 64, 48)
    r.setOpacity(0.5)
    same(r, plain.extent(), 64, 48)
    r = plain.renderer().clone()  # QGIS's own default for the image (a cumulative cut stretch)
    r.setInput(plain.dataProvider())
    assert fast_draw.PART * 2 <= 900 * 400
    out = same(r, plain.extent(), 900, 400)
    assert len(set(np.frombuffer(out, np.uint32).tolist())) > 1000


@pytest.mark.parametrize("dtype,gdal_type,data_type", TYPES, ids=[np.dtype(t[0]).name for t in TYPES])
def test_gray_pixels_are_qgis_own(tmp_path, dtype, gdal_type, data_type):
    info = np.iinfo(dtype)
    lo, hi = float(info.min), float(info.max)
    plain = image(tmp_path / "plain.tif", dtype, gdal_type, bands=1)
    nodata = image(tmp_path / "nodata.tif", dtype, gdal_type, bands=1, no_data=int(info.max), seed=4)
    alpha = image(tmp_path / "alpha.tif", dtype, gdal_type, bands=4, seed=5)
    outside = QgsRectangle(980, 1940, 1070, 2010)
    for n, algorithm in enumerate(ALGORITHMS + [None]):
        for low, high in [(lo, hi), (0.0, 200.0), (13.4, 251.6)]:
            for layer, band, alpha_band in ((plain, 1, -1), (nodata, 1, -1), (alpha, 2, 4), (alpha, 4, 4)):
                r = QgsSingleBandGrayRenderer(layer.dataProvider(), band)
                if algorithm is not None:
                    r.setContrastEnhancement(enhancement(data_type, algorithm, low, high))
                r.setAlphaBand(alpha_band)
                r.setGradient(QgsSingleBandGrayRenderer.Gradient.WhiteToBlack if n % 2 else QgsSingleBandGrayRenderer.Gradient.BlackToWhite)
                r.setOpacity((1.0, 0.6, 0.3)[(n + band) % 3])
                if band == 2:
                    r.setRasterTransparency(QgsRasterTransparency())
                same(r, layer.extent(), 64, 48)
                same(r, outside, 50, 40)


def test_what_is_left_to_qgis(tmp_path):
    layer = image(tmp_path / "u16.tif", np.uint16, gdal.GDT_UInt16)
    # A stretch over no range (QGIS divides by zero) and transparent pixel values: QGIS's own drawing, same pixels.
    r = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
    r.setRedContrastEnhancement(enhancement(Qgis.DataType.UInt16, CE.StretchToMinimumMaximum, 300, 300))
    same(r, layer.extent(), 64, 48, drawn=False)
    r = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
    transparency = QgsRasterTransparency()
    pixel = QgsRasterTransparency.TransparentThreeValuePixel()
    pixel.red, pixel.green, pixel.blue, pixel.percentTransparent = 0, 0, 0, 100
    transparency.setTransparentThreeValuePixelList([pixel])
    r.setRasterTransparency(transparency)
    same(r, layer.extent(), 64, 48, drawn=False)
    # An enhancement made for another type than the band's (the probes catch it).
    r = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    r.setContrastEnhancement(enhancement(Qgis.DataType.Byte, CE.StretchToMinimumMaximum, 0, 200))
    same(r, layer.extent(), 64, 48, drawn=False)
    # Floats are not taken at all.
    path = tmp_path / "f32.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 32, 16, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((1000, 1, 0, 2000, 0, -1))
    ds.GetRasterBand(1).WriteArray(np.linspace(0, 1, 512, dtype=np.float32).reshape(16, 32))
    ds = None
    floats = QgsRasterLayer(str(path), "f32")
    assert isinstance(floats.renderer(), QgsSingleBandGrayRenderer)
    assert not fast_draw.wrap(floats) and type(floats.renderer()) is QgsSingleBandGrayRenderer


def draw(layer, w=200, h=150):
    s = QgsMapSettings()
    s.setLayers([layer])
    s.setDestinationCrs(layer.crs())
    s.setOutputSize(QSize(w, h))
    s.setExtent(layer.extent())
    job = QgsMapRendererSequentialJob(s)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def test_the_project_layers_are_drawn_by_mikage_and_saved_as_qgis_own(tmp_path, monkeypatch):
    project = QgsProject.instance()
    rgb = image(tmp_path / "rgb.tif", np.uint16, gdal.GDT_UInt16, size=(120, 90))
    gray = image(tmp_path / "gray.tif", np.int16, gdal.GDT_Int16, bands=1, size=(120, 90), seed=7)
    rgb.renderer().setOpacity(0.7)
    expected = {layer.name(): draw(layer) for layer in (rgb, gray)}
    project.addMapLayers([rgb, gray])
    controller = fast_draw.FastDraw()
    assert controller.start() == 2
    assert type(rgb.renderer()) is FastMultiBand and type(gray.renderer()) is FastGray
    assert rgb.renderer().opacity() == 0.7
    for layer in (rgb, gray):
        assert draw(layer) == expected[layer.name()]
    # Added later: drawn so too.
    later = image(tmp_path / "later.tif", np.uint8, gdal.GDT_Byte, size=(40, 30), seed=8)
    project.addMapLayer(later)
    assert type(later.renderer()) is FastMultiBand and controller.count() == 3
    # A new style (the symbology page sets a new QGIS renderer): MIKAGE's again, with the new settings.
    r = QgsMultiBandColorRenderer(rgb.dataProvider(), 3, 2, 1)
    r.setRedContrastEnhancement(enhancement(Qgis.DataType.UInt16, CE.StretchAndClipToMinimumMaximum, 100, 30000))
    rgb.setRenderer(r)
    assert wait_for(lambda: type(rgb.renderer()) is FastMultiBand, ms=3000)
    assert rgb.renderer().redBand() == 3 and rgb.renderer().redContrastEnhancement().maximumValue() == 30000
    # Saved as QGIS's own renderers: the project opens without the plugin as before.
    path = str(tmp_path / "p.qgs")
    assert project.write(path)
    text = open(path, encoding="utf-8").read()
    assert 'type="multibandcolor"' in text and 'type="singlebandgray"' in text
    # Opened again: drawn by MIKAGE again.
    project.clear()
    assert project.read(path)
    layers = project.mapLayers().values()
    assert len(layers) == 3 and all(fast_draw.fast(layer) for layer in layers)
    # Layers QGIS made (no Python of their own): still MIKAGE's drawing when drawn, Python's references gone.
    del layers
    gc.collect()
    used = []
    real = fast_draw.multiband_argb
    monkeypatch.setattr(fast_draw, "multiband_argb", lambda *a, **k: used.append(1) or real(*a, **k))
    assert draw(project.mapLayersByName("later.tif")[0]) is not None and used
    # Off: QGIS's own renderers with the same settings.
    controller.stop()
    reopened = project.mapLayersByName("rgb.tif")[0]
    assert type(reopened.renderer()) is QgsMultiBandColorRenderer and reopened.renderer().redBand() == 3
    assert reopened.renderer().redContrastEnhancement().contrastEnhancementAlgorithm() == CE.StretchAndClipToMinimumMaximum
    assert controller.count() == 0
    project.addMapLayer(image(tmp_path / "after.tif", np.uint8, gdal.GDT_Byte, size=(40, 30), seed=9))
    assert controller.count() == 0


def test_the_switch_in_the_dialog_stays_in_the_profile(tmp_path):
    project = QgsProject.instance()
    layer = image(tmp_path / "rgb.tif", np.uint16, gdal.GDT_UInt16, size=(60, 40))
    project.addMapLayer(layer)
    iface = FakeIface()
    plugin = small_plugin(iface, Rendering)
    plugin.initGui()
    assert not fast_draw.on() and type(layer.renderer()) is QgsMultiBandColorRenderer  # off unless the person turns it on
    dialog = plugin.open_raster_speed()
    assert dialog.fast.isVisible() and not dialog.fast.isChecked()
    status = dialog.set_fast(True)
    assert "1 件" in status and fast_draw.on() and type(layer.renderer()) is FastMultiBand
    assert "今 MIKAGE が描いている画像: 1 件" in dialog.fast_note.text()
    from mikage.qgis_io import raster_speed

    assert "MIKAGE の速い描画（試作）" in raster_speed.summary()
    plugin.unload()
    assert type(layer.renderer()) is QgsMultiBandColorRenderer  # unloaded: QGIS's own again
    # On at the next start; off from the dialog puts QGIS's renderer back.
    plugin = small_plugin(iface, Rendering)
    plugin.initGui()
    assert type(layer.renderer()) is FastMultiBand
    dialog = plugin.open_raster_speed()
    assert dialog.fast.isChecked()
    assert "QGIS の描画に変更" in dialog.set_fast(False)
    assert not fast_draw.on() and type(layer.renderer()) is QgsMultiBandColorRenderer
    plugin.unload()
