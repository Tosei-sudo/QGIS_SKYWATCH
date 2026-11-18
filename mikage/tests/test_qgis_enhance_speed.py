"""
画像補正 drawn faster, the same pixels: a correction of per-channel steps is
one table lookup per band from the raw values to the colour QGIS draws, the
premultiplied ARGB comes straight from the tables, and a drawing QGIS calls
off (a newer one on its way) stops early.
"""
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal  # noqa: E402
from qgis.core import QgsProject, QgsRasterBlockFeedback, QgsRasterLayer  # noqa: E402

from mikage.core.enhance import Pipeline  # noqa: E402
from mikage.qgis_io import enhance_renderer as er  # noqa: E402

gdal.UseExceptions()


def tif(path, arrays, dtype, no_data=None):
    h, w = arrays[0].shape
    ds = gdal.GetDriverByName("GTiff").Create(str(path), w, h, len(arrays), dtype)
    ds.SetGeoTransform([1000, 1, 0, 2000, 0, -1])
    for i, a in enumerate(arrays):
        band = ds.GetRasterBand(i + 1)
        if no_data is not None:
            band.SetNoDataValue(no_data)
        band.WriteArray(a)
    ds = None
    layer = QgsRasterLayer(str(path), os.path.basename(str(path)))
    assert layer.isValid()
    return layer


def packed(rgba, opacity):
    """Premultiplied ARGB32 as the renderer packed it before (the reference)."""
    alpha = rgba[..., 3].astype(np.uint32)
    if opacity < 1:
        alpha = (alpha * opacity + 0.5).astype(np.uint32)
    r, g, b = (rgba[..., c].astype(np.uint32) * alpha // 255 for c in range(3))
    return (alpha << 24) | (r << 16) | (g << 8) | b


def drawn(r, layer, w, h):
    block = r.block(1, layer.extent(), w, h)
    return np.frombuffer(bytes(block.data()), dtype=np.uint32).reshape(h, w)


DRA = {"method": "percentClip", "lowPercent": 1, "highPercent": 1, "linked": False}


def test_the_lookup_draws_what_the_correction_gives(tmp_path):
    rng = np.random.default_rng(3)
    h, w = 300, 140  # parts in threads
    y, x = np.mgrid[0:h, 0:w]
    u16 = [np.clip(2000 + 9 * x + 4 * y + rng.normal(0, 60, (h, w)) + 300 * c, 1, 60000).astype(np.uint16) for c in range(3)]
    u16[0][:20, :30] = 0
    u8 = [np.clip(40 + x + rng.normal(0, 9, (h, w)) + 20 * c, 0, 255).astype(np.uint8) for c in range(3)]
    i16 = [np.clip(-500 + 6 * x + rng.normal(0, 40, (h, w)), -32768, 32767).astype(np.int16)]
    f32 = [(np.sin(x / 9.0) * np.cos(y / 7.0) * 30 - 12).astype(np.float32)]
    f32[0][50:60, 40:80] = np.nan
    layers = {
        "u16": tif(tmp_path / "u16.tif", u16, gdal.GDT_UInt16, no_data=0),
        "u8": tif(tmp_path / "u8.tif", u8, gdal.GDT_Byte),
        "i16": tif(tmp_path / "i16.tif", i16, gdal.GDT_Int16),
        "f32": tif(tmp_path / "f32.tif", f32, gdal.GDT_Float32),
    }
    QgsProject.instance().addMapLayers(list(layers.values()))
    per_channel = Pipeline().add("autoStretch", DRA).add("contrast", 0.25).add("gamma", 1.3).add("temperature", 0.2)
    classes = {"kind": "ranges", "classes": [{"value": -400, "color": [0, 0, 255]}, {"value": 0, "color": [0, 255, 0]},
                                             {"value": 300, "color": [255, 0, 0, 128]}]}
    cases = [
        # (layer, bands, correction, colour, opacity, lookup)
        ("u16", (1, 2, 3), per_channel, None, 1.0, True),
        ("u16", (3, 2, 1), Pipeline().add("exposure", 0.4), None, 0.45, True),
        ("u16", (2,), per_channel, {"scale": "turbo", "reverse": True}, 1.0, True),
        ("u16", (1,), Pipeline().add("levels", {"inBlack": 0.1, "inWhite": 0.9}), {"scale": "red", "fade": True}, 0.6, True),
        ("u8", (1, 2, 3), Pipeline().add("autoStretch", DRA).add("brightness", 0.1), None, 1.0, True),
        ("u8", (1, 2, 3), Pipeline(), None, 0.3, True),
        ("i16", (1,), Pipeline(), {"scale": "turbo", "classes": classes}, 1.0, True),
        ("f32", (1,), per_channel, {"scale": "viridis"}, 1.0, True),
        ("f32", (1,), Pipeline().add("contrast", 0.2), None, 0.8, True),
        # Steps that mix channels or pixels: the correction as before.
        ("u16", (1, 2, 3), per_channel.add("saturation", 0.4), None, 0.7, False),
        ("u16", (1, 2, 3), per_channel.add("sharpen", {"amount": 1.5, "radius": 2}), None, 1.0, False),
        ("u16", (1, 2, 3), Pipeline().add("contrast", 0.2), {"scale": "turbo"}, 1.0, False),
    ]
    try:
        for name, bands, correction, color, opacity, lookup in cases:
            layer = layers[name]
            r = er.apply(layer, er.EnhanceSettings(bands=bands, pipeline=correction.to_json(), color=color))
            r.setOpacity(opacity)
            er.set_view(layer, layer.extent())
            got = drawn(r, layer, w, h)
            want = packed(r.render(layer.extent(), w, h), opacity)
            assert np.array_equal(got, want), (name, bands, correction.ops, color, opacity)
            black, white, ops = r.ranges(layer.extent(), Pipeline() if r.settings.classes else r.settings.correction)
            assert (r._luts(r._drawing(layer.extent(), w, h)[0], black, white, ops, opacity) is not None) == lookup
        # Something was drawn (not all transparent), and no data stays transparent.
        r = er.apply(layers["u16"], er.EnhanceSettings(bands=(1, 2, 3), pipeline=per_channel.to_json()))
        r.setOpacity(1.0)
        got = drawn(r, layers["u16"], w, h)
        assert (got[:20, :30] == 0).all() and (got[100:] >> 24 == 255).all()
    finally:
        QgsProject.instance().removeAllMapLayers()


def test_a_drawing_called_off_stops_early(tmp_path, monkeypatch):
    rng = np.random.default_rng(5)
    arrays = [rng.integers(100, 4000, (200, 150)).astype(np.uint16) for _ in range(3)]
    layer = tif(tmp_path / "c.tif", arrays, gdal.GDT_UInt16)
    QgsProject.instance().addMapLayer(layer)
    try:
        r = er.apply(layer, er.EnhanceSettings(bands=(1, 2, 3), pipeline=Pipeline().add("saturation", 0.3).to_json()))
        feedback = QgsRasterBlockFeedback()
        feedback.cancel()
        assert r.argb(layer.extent(), 150, 200, feedback) is None
        block = r.block(1, layer.extent(), 150, 200, feedback)
        # Transparent (QGIS throws it away and draws the newer one).
        assert not np.frombuffer(bytes(block.data()), dtype=np.uint32).any()

        # Called off while the range is taken: no correction runs after it.
        corrected = []
        monkeypatch.setattr(er, "process", lambda *a, **k: corrected.append(1))
        ranges = er.EnhanceRenderer.ranges
        feedback = QgsRasterBlockFeedback()

        def then_off(self, *args):
            out = ranges(self, *args)
            feedback.cancel()
            return out

        monkeypatch.setattr(er.EnhanceRenderer, "ranges", then_off)
        assert r.argb(layer.extent(), 150, 200, feedback) is None
        assert not corrected
    finally:
        QgsProject.instance().removeAllMapLayers()
