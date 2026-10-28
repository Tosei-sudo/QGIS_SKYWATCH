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

from satellite_observation.core.enhance import PRESETS, Pipeline, process  # noqa: E402
from satellite_observation.core.enhance.histogram import channel_stats  # noqa: E402
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
        assert dock.curve_box.isChecked() and dock.curve.step()["points"] == [[0, 0], [0.5, 0.6], [1, 1]]
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


def make_dock():
    from satellite_observation.qgis_io.context import PluginContext
    from satellite_observation.tests.qgis_fakes import FakeIface
    from satellite_observation.ui.enhance_dock import EnhanceDock

    iface = FakeIface()
    return iface, EnhanceDock(iface, PluginContext(iface.window), lambda: None, iface.window)


def test_image_rules_open_images_with_their_correction_bands_and_no_data(tmp_path):
    from satellite_observation.core.config import parse_config

    config, problems = parse_config({"imageRules": [
        {"label": "Landsat", "match": r"^LC0[89]_", "preset": "satellite", "bands": [4, 3, 2]},
        {"label": "名前", "match": r"_MS\.tif$", "bands": ["NIR", "Red", "Green"], "noData": {"auto": False, "values": [7]}},
        {"label": "足りない", "match": r"_few\.tif$", "bands": [5, 4, 3], "enhance": {"contrast": 0.2}},
    ]})
    assert not problems
    rng = np.random.default_rng(5)
    four = [rng.normal(2000 + 300 * i, 200, (30, 40)).astype(np.uint16) for i in range(4)]
    landsat = write_tif(tmp_path / "LC09_L2SP_107035.tif", four, gdal.GDT_UInt16)
    named = write_tif(tmp_path / "scene_MS.tif", four, gdal.GDT_UInt16, no_data=0)
    ds = gdal.Open(named, gdal.GA_Update)
    for i, name in enumerate(["Blue", "Green", "Red", "NIR"]):
        ds.GetRasterBand(i + 1).SetDescription(name)
    ds = None
    few = write_tif(tmp_path / "x_few.tif", four, gdal.GDT_UInt16)
    other = write_tif(tmp_path / "other.tif", four, gdal.GDT_UInt16)

    iface, dock = make_dock()
    dock.context.config = config
    messages = []
    iface.bar.pushWarning = lambda _title, text: messages.append(text)
    project = QgsProject.instance()
    try:
        layers = {p: layer_of(p) for p in (landsat, named, few, other)}
        project.addMapLayers(list(layers.values()))
        r = er.enhance_renderer(layers[landsat])
        assert r.settings.bands == (4, 3, 2)
        assert r.settings.correction.ops == PRESETS["satellite"].ops
        r = er.enhance_renderer(layers[named])
        assert r.settings.bands == (4, 3, 2) and r.settings.no_data == (7.0,)
        assert not layers[named].dataProvider().useSourceNoDataValue(1)
        # Bands it lacks: the correction still, on the layer's own bands, with a warning.
        r = er.enhance_renderer(layers[few])
        assert r.settings.bands == (1, 2, 3) and r.settings.correction.get("contrast")
        assert any("足りない" in m and "バンド 5 なし" in m for m in messages)
        assert er.enhance_renderer(layers[other]) is None

        # The dock keeps the rule's no-data values when its sliders change.
        iface.setActiveLayer(layers[named])
        dock.apply()
        assert er.enhance_renderer(layers[named]).settings.no_data == (7.0,)

        # A project's layers keep their own look: no rule while it is read.
        er.remove(layers[landsat])
        qgs = str(tmp_path / "rules.qgs")
        assert project.write(qgs)
        project.clear()
        assert project.read(qgs)
        opened = next(lay for lay in project.mapLayers().values() if lay.source() == landsat)
        assert er.enhance_renderer(opened) is None
        opened_named = next(lay for lay in project.mapLayers().values() if lay.source() == named)
        assert er.enhance_renderer(opened_named).settings.no_data == (7.0,)
    finally:
        dock.close_down()
        project.clear()


def test_the_histogram_of_the_view_before_and_after(tmp_path):
    from satellite_observation.tests.qgis_fakes import wait_for

    arrays = color8()
    layer = layer_of(write_tif(tmp_path / "h.tif", arrays))
    iface, dock = make_dock()
    project = QgsProject.instance()
    try:
        project.addMapLayer(layer)
        iface.canvas.setDestinationCrs(layer.crs())
        iface.canvas.setLayers([layer])
        iface.canvas.setExtent(layer.extent())
        iface.setActiveLayer(layer)
        p = Pipeline().add("contrast", 0.6)
        dock.enhance(layer, p)

        def wait(task):
            assert task is not None and wait_for(lambda: dock.histograms is not None)

        dock.histogram_box.setChecked(True)
        dock.histograms = None
        wait(dock.take_histogram())
        rgba = np.dstack(arrays + [np.full((60, 80), 255, np.uint8)])
        before, after = dock.histograms["before"], dock.histograms["after"]
        assert len(before) == len(after) == 3
        for c in range(3):
            assert np.array_equal(before[c], np.bincount(rgba[..., c].ravel(), minlength=256))
        # More contrast spreads the codes.
        assert np.std(np.repeat(np.arange(256), after[0])) > np.std(np.repeat(np.arange(256), before[0]))
        s = channel_stats(after[0])
        assert dock.histogram_table.rowCount() == 3 and dock.histogram_table.item(0, 0).text() == str(s["min"])
        assert dock.histogram_note.text() == "表示範囲の 4,800 画素・補正後"

        dock.histogram_after.setCurrentIndex(1)
        assert dock.histogram_table.item(0, 3).text() == str(rgba[..., 0].max())
        dock.histogram_log.setChecked(True)
        assert max(float(v.max()) for v in dock.histogram_plot.heights()) == pytest.approx(1.0)
        dock.histogram_plot.grab()

        # Gray: one channel.
        dock.enhance(layer, p, bands=(2,))
        dock.histograms = None
        wait(dock.take_histogram())
        assert len(dock.histograms["after"]) == 1 and dock.histogram_table.rowCount() == 1
    finally:
        dock.close_down()
        project.removeAllMapLayers()


def test_color_scales_and_classes_are_drawn_kept_and_in_the_legend(tmp_path):
    from qgis.core import QgsLayerTreeModel, QgsSingleBandPseudoColorRenderer

    from satellite_observation.core.enhance.color_scale import apply_palette, color_scale_palette

    a = color8()[0]
    layer = layer_of(write_tif(tmp_path / "sar.tif", [a]))
    p = Pipeline().add("contrast", 0.3)
    r = er.apply(layer, er.EnhanceSettings(bands=(1,), pipeline=p.to_json(), color={"scale": "inferno", "reverse": True}))
    rgba = np.dstack([a, a, a, np.full(a.shape, 255, np.uint8)])
    assert np.array_equal(draw(r, layer.extent(), 80, 60), apply_palette(process(rgba, p, color_mode="gray"),
                                                                           color_scale_palette("inferno", reverse=True)))

    # Classes of the values as read (no correction), in the legend and, without the plugin, QGIS's pseudo-colour renderer.
    codes = np.repeat(np.array([[1, 2, 3, 7]], np.uint8), 10, axis=0)
    land = layer_of(write_tif(tmp_path / "land.tif", [codes]))
    classes = {"kind": "exact", "classes": [{"value": v, "color": c, "label": n} for v, c, n in
                                            ((1, [255, 0, 0], "市街地"), (2, [0, 128, 0], "森林"), (3, [0, 0, 255], "水域"))]}
    r = er.apply(land, er.EnhanceSettings(bands=(1,), pipeline=p.to_json(), color={"scale": "turbo", "classes": classes}))
    got = draw(r, land.extent(), 4, 10)
    assert [list(got[0, x]) for x in range(4)] == [[255, 0, 0, 255], [0, 128, 0, 255], [0, 0, 255, 255], [0, 0, 0, 0]]
    project = QgsProject()
    project.addMapLayer(land)
    model = QgsLayerTreeModel(project.layerTreeRoot())
    assert [n.data(0) for n in model.layerLegendNodes(project.layerTreeRoot().findLayer(land.id()))] == ["市街地", "森林", "水域"]
    qgs = str(tmp_path / "colors.qgs")
    assert project.write(qgs)
    other = QgsProject()
    assert other.read(qgs)
    opened = list(other.mapLayers().values())[0]
    assert isinstance(opened.renderer(), QgsSingleBandPseudoColorRenderer)
    assert [label for label, _c in opened.renderer().legendSymbologyItems()] == ["市街地", "森林", "水域"]
    assert er.restore_layer(opened) and er.enhance_renderer(opened).settings.classes == classes


def test_the_dock_colors_classes_and_two_dates(tmp_path):
    from qgis.PyQt.QtGui import QPainter

    from satellite_observation.tests.qgis_fakes import wait_for

    rng = np.random.default_rng(6)
    dem = rng.normal(500, 100, (40, 50)).astype(np.float32)
    iface, dock = make_dock()
    project = QgsProject.instance()
    try:
        layer = layer_of(write_tif(tmp_path / "dem.tif", [dem], gdal.GDT_Float32))
        project.addMapLayer(layer)
        iface.setActiveLayer(layer)
        box = dock.colorize_box
        assert not dock.enabled.isChecked() and box.color() is None

        # A scale turns the correction on and colours after it.
        box.scale.setCurrentIndex(box.scale.findData("viridis"))
        dock.apply()
        assert dock.enabled.isChecked() and er.enhance_renderer(layer).settings.color == {"scale": "viridis"}
        box.blend.setCurrentIndex(box.blend.findData("multiply"))
        assert layer.blendMode() == QPainter.CompositionMode.CompositionMode_Multiply

        # Classes from the image's values (read in a task), with their shares.
        box.mode.setCurrentIndex(box.mode.findData("classes"))
        assert wait_for(lambda: box.classes is not None)
        dock.apply()
        classes = er.enhance_renderer(layer).settings.classes
        assert classes["kind"] == "range" and len(classes["classes"]) == 5
        assert wait_for(lambda: box.legend.item(0, 3) is not None and box.legend.item(0, 3).text().endswith("%"))
        assert box.legend.rowCount() == 5
        box.count.setValue(3)
        assert wait_for(lambda: len(box.classes["classes"]) == 3)
        box._bound(0, "450")
        dock.apply()
        classes = er.enhance_renderer(layer).settings.classes
        assert classes["method"] == "manual" and sorted(c["value"] for c in classes["classes"])[0] == 450
        assert box.method.currentData() == "manual"

        # Back to a smooth scale, then none.
        box.clear()
        dock.apply()
        assert er.enhance_renderer(layer).settings.color is None
        assert layer.blendMode() == QPainter.CompositionMode.CompositionMode_SourceOver

        # Two dates: the earlier red, the later cyan screened right above it.
        earlier = layer_of(write_tif(tmp_path / "d1.tif", [dem]))
        later = layer_of(write_tif(tmp_path / "d2.tif", [dem]))
        project.addMapLayers([earlier, later])
        box.pair_box.setChecked(True)
        box.before.setCurrentIndex(box.before.findData(earlier.id()))
        box.after.setCurrentIndex(box.after.findData(later.id()))
        assert box.pair()
        ids = [n.layerId() for n in project.layerTreeRoot().findLayers()]
        assert ids.index(later.id()) == ids.index(earlier.id()) - 1
        assert er.enhance_renderer(earlier).settings.color == {"scale": "red"}
        assert er.enhance_renderer(later).settings.color == {"scale": "cyan"}
        assert later.blendMode() == QPainter.CompositionMode.CompositionMode_Screen
        box.before.setCurrentIndex(box.before.findData(earlier.id()))
        box.after.setCurrentIndex(box.after.findData(earlier.id()))
        assert not box.pair() and box.pair_note.text() == "別々の画像を選択"

        # For scripts.
        dock.colorize(layer, "red", blend="screen", fade=True)
        assert er.enhance_renderer(layer).settings.color == {"scale": "red", "fade": True}
        assert box.fade.isChecked() and box.scale.currentData() == "red"
    finally:
        dock.close_down()
        project.removeAllMapLayers()


def test_the_tone_curve_is_edited_on_its_plot(tmp_path):
    from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt
    from qgis.PyQt.QtGui import QMouseEvent
    from qgis.PyQt.QtTest import QTest
    from qgis.PyQt.QtWidgets import QApplication

    from satellite_observation.ui.curve_editor import CurveEditor

    editor = CurveEditor()
    editor.resize(220, 300)
    editor.show()
    plot = editor.plot
    plot.resize(200, 200)
    edits = []
    editor.changed.connect(lambda: edits.append(1))
    assert editor.step() is None

    def move(pos):
        # QTest.mouseMove sends no button held (Qt 5): a drag needs the event itself.
        QApplication.sendEvent(plot, QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), Qt.MouseButton.LeftButton,
                                                 Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))

    def at(x, y):
        q = plot.to_screen(x, y)
        return QPoint(round(q.x()), round(q.y()))

    # A click off the points adds one on the curve; dragging it bends the curve.
    QTest.mousePress(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0.5, 0.2))
    move(at(0.5, 0.7))
    QTest.mouseRelease(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0.5, 0.7))
    pts = editor.step()["points"]
    assert len(pts) == 3 and pts[1][0] == pytest.approx(0.5, abs=0.01) and pts[1][1] == pytest.approx(0.7, abs=0.01)
    assert edits
    # The ends move only up and down.
    QTest.mousePress(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0, 0))
    move(at(0.2, 0.1))
    QTest.mouseRelease(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0.2, 0.1))
    assert editor.step()["points"][0] == [0.0, pytest.approx(0.1, abs=0.01)]
    # A double-click removes a point; a channel of its own.
    QTest.mouseDClick(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(*editor.step()["points"][1]))
    assert len(editor.step()["points"]) == 2
    editor.channel.setCurrentIndex(editor.channel.findData("red"))
    QTest.mousePress(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0.25, 0.25))
    move(at(0.25, 0.5))
    QTest.mouseRelease(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0.25, 0.5))
    step = editor.step()
    assert len(step["red"]) == 3 and step["green"] == [[0.0, 0.0], [1.0, 1.0]]
    # Dragged out of the box: removed.
    QTest.mousePress(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(*step["red"][1]))
    move(QPoint(-60, -60))
    QTest.mouseRelease(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(-60, -60))
    assert len(editor.step()["red"]) == 2
    editor.reset_channel()
    editor.channel.setCurrentIndex(0)
    editor.reset_channel()
    assert editor.step() is None
    editor.set_gray(True)
    assert not editor.channel.model().item(1).isEnabled()
    plot.grab()
    editor.close()
