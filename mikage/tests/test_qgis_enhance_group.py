"""
画像補正 on a layer-tree group: the dock works on every raster under the
group chosen in the layer panel, and 「グループで範囲を共通にする」 has the
rasters take one range from all of them (no seams between tiles).
"""
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsLayerTreeModel, QgsMapRendererParallelJob, QgsMapSettings, QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.gui import QgsLayerTreeView  # noqa: E402

from mikage.core.enhance import Pipeline  # noqa: E402
from mikage.core.enhance.raster import compute_raster_stretch, sample_raster_histogram, stretch_to_rgba, valid_mask  # noqa: E402
from mikage.qgis_io import enhance_group as eg  # noqa: E402
from mikage.qgis_io import enhance_renderer as er  # noqa: E402

gdal.UseExceptions()


def tile(path, x0, mean, bands=3, dtype=gdal.GDT_UInt16, size=(40, 50), seed=0):
    """A tile of 16-bit bands at x0 (1 m pixels), values about `mean`."""
    h, w = size
    rng = np.random.default_rng(seed)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), w, h, bands, dtype)
    ds.SetGeoTransform([x0, 1, 0, 2000, 0, -1])
    arrays = []
    for i in range(bands):
        a = np.clip(rng.normal(mean + 200 * i, 150, (h, w)), 1, 60000).astype(np.uint16 if dtype == gdal.GDT_UInt16 else np.uint8)
        ds.GetRasterBand(i + 1).WriteArray(a)
        arrays.append(a)
    ds = None
    layer = QgsRasterLayer(str(path), os.path.basename(str(path)))
    assert layer.isValid()
    return layer, arrays


def draw(r: er.EnhanceRenderer, extent: QgsRectangle, w: int, h: int) -> np.ndarray:
    block = r.block(1, extent, w, h)
    argb = np.frombuffer(bytes(block.data()), dtype=np.uint32).reshape(h, w)
    return np.dstack([(argb >> 16) & 255, (argb >> 8) & 255, argb & 255, argb >> 24]).astype(np.uint8)


def test_a_shared_range_is_taken_over_every_member(tmp_path):
    dark, a = tile(tmp_path / "a.tif", 1000, 2000, seed=1)
    bright, b = tile(tmp_path / "b.tif", 1050, 6000, seed=2)
    project = QgsProject.instance()
    project.addMapLayers([dark, bright])
    try:
        # Each on its own: both stretched on themselves, so the dark tile looks as bright as the other.
        rd = er.apply(dark, er.EnhanceSettings(bands=(1, 2, 3)))
        rb = er.apply(bright, er.EnhanceSettings(bands=(1, 2, 3)))
        own = draw(rd, dark.extent(), 50, 40)
        assert abs(int(own[..., 0].mean()) - int(draw(rb, bright.extent(), 50, 40)[..., 0].mean())) < 20

        # Shared: one stretch from both tiles' pixels (the whole images without a view).
        rd = er.apply(dark, er.EnhanceSettings(bands=(1, 2, 3), share="k1"))
        rb = er.apply(bright, er.EnhanceSettings(bands=(1, 2, 3), share="k1"))
        assert sorted(er.share_members("k1")) == sorted([dark.id(), bright.id()])
        both = [np.concatenate([x.astype(np.float64).ravel(), y.astype(np.float64).ravel()]).reshape(1, -1) for x, y in zip(a, b)]
        valid = valid_mask(both)
        s = compute_raster_stretch(sample_raster_histogram(both, valid), er.WHOLE_STRETCH)
        got = draw(rd, dark.extent(), 50, 40)
        picture = [x.astype(np.float64) for x in a]
        assert np.array_equal(got, stretch_to_rgba(picture, valid_mask(picture), s["black"], s["white"]))
        assert got[..., 0].mean() + 100 < draw(rb, bright.extent(), 50, 40)[..., 0].mean()
        # QGIS's own renderer in the project gets the shared stretch too.
        fallback = rd.fallback()
        assert fallback.redContrastEnhancement().minimumValue() == pytest.approx(s["black"][0])

        # DRA: the view over both tiles, min..max of the two together.
        p = Pipeline().add("autoStretch", {"method": "minMax"}).to_json()
        view = QgsRectangle(1000, 1960, 1100, 2000)
        for layer in (dark, bright):
            er.apply(layer, er.EnhanceSettings(bands=(1, 2, 3), pipeline=p, share="k1"))
            er.set_view(layer, view)
        lo = min(int(a[0].min()), int(b[0].min()))
        hi = max(int(a[0].max()), int(b[0].max()))
        black, white, _ops = er.enhance_renderer(dark).ranges(dark.extent(), Pipeline.from_json(p))
        assert (black[0], white[0]) == (pytest.approx(lo, abs=(hi - lo) / 1000), pytest.approx(hi, abs=(hi - lo) / 1000))
        assert er.enhance_renderer(bright).ranges(bright.extent(), Pipeline.from_json(p))[:2] == (black, white)

        # Drawn by QGIS in parallel threads: the tiles meet with one range (the dark one stays darker).
        ms = QgsMapSettings()
        ms.setLayers([dark, bright])
        ms.setDestinationCrs(dark.crs())
        ms.setExtent(view)
        ms.setOutputSize(QSize(100, 40))
        job = QgsMapRendererParallelJob(ms)
        job.start()
        job.waitForFinished()
        assert not job.errors()
        img = job.renderedImage()
        assert img.pixelColor(20, 20).red() + 60 < img.pixelColor(80, 20).red()

        # The setting is kept with the layer; off or removed, the layer leaves the share.
        assert er.EnhanceSettings.from_json(dark.customProperty(er.PROPERTY)).share == "k1"
        er.remove(bright)
        assert er.share_members("k1") == [dark.id()]
        er.apply(dark, er.EnhanceSettings(bands=(1, 2, 3)))
        assert er.share_members("k1") == []
    finally:
        project.removeAllMapLayers()


def make_dock():
    from mikage.qgis_io.context import PluginContext
    from mikage.tests.qgis_fakes import FakeIface
    from mikage.ui.enhance_dock import EnhanceDock

    iface = FakeIface()
    model = QgsLayerTreeModel(QgsProject.instance().layerTreeRoot())
    view = QgsLayerTreeView()
    view.setModel(model)
    iface.layerTreeView = lambda: view
    iface._tree = (model, view)
    return iface, EnhanceDock(iface, PluginContext(iface.window), lambda: None, iface.window)


def choose(iface, node):
    """Chooses a node in the layer panel, as a click does (a layer also becomes the active one)."""
    _model, view = iface._tree
    view.setCurrentIndex(view.node2index(node))
    iface.setActiveLayer(view.currentLayer())


def test_the_dock_corrects_every_raster_of_the_chosen_group(tmp_path):
    from mikage.tests.qgis_fakes import wait_for

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    t1, _ = tile(tmp_path / "t1.tif", 1000, 3000, seed=1)
    t2, _ = tile(tmp_path / "t2.tif", 1050, 3200, seed=2)
    t3, _ = tile(tmp_path / "t3.tif", 1100, 3400, seed=3)
    points = QgsVectorLayer("Point?crs=EPSG:3857", "points", "memory")
    iface, dock = make_dock()
    try:
        group = root.addGroup("scene")
        for layer in (t1, points, t2):
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        inner = group.addGroup("more")
        project.addMapLayer(t3, False)
        inner.addLayer(t3)
        iface.canvas.setDestinationCrs(t1.crs())
        iface.canvas.setLayers([t1, t2, t3])
        iface.canvas.setExtent(QgsRectangle(1000, 1960, 1150, 2000))

        # The group: its rasters (the nested group's too), alike, so one range by default.
        choose(iface, group)
        assert dock.group is group and [x.id() for x in dock.targets] == [t1.id(), t2.id(), t3.id()]
        assert dock.layer is t1
        assert dock.layer_label.text().startswith("対象: グループ「scene」のラスター 3 枚")
        assert not dock.share.isHidden() and dock.share.isEnabled() and dock.share.isChecked()
        assert dock.colorize_box.pair_box.isHidden()

        row = next(x for x in dock.rows if x.op == "contrast")
        row.slider.setValue(row.slider.value() + 20)
        dock.enabled.setChecked(True)
        for combo, band in zip(dock.band_combos, (3, 2, 1)):
            combo.setCurrentIndex(band - 1)
        dock.apply()
        key = eg.share_key(group)
        assert key
        for layer in (t1, t2, t3):
            s = er.enhance_renderer(layer).settings
            assert s.bands == (3, 2, 1) and s.share == key
            assert s.correction.get("contrast") == {"op": "contrast", "amount": pytest.approx(0.2)}
            assert er.enhance_renderer(layer).state.view is not None
        assert sorted(er.share_members(key)) == sorted([t1.id(), t2.id(), t3.id()])
        assert len(dock.layer_label.text().splitlines()) == 1

        # The histogram counts the group's rasters in view, added up.
        dock.histogram_box.setChecked(True)
        dock.histograms = None
        assert dock.take_histogram() is not None and wait_for(lambda: dock.histograms is not None)
        assert sum(int(c.sum()) for c in dock.histograms["before"][:1]) == 3 * 40 * 50
        assert dock.histogram_note.text() == "表示範囲の 3 枚の 6,000 画素・補正後"

        # One layer chosen: its own correction, changed alone, still in the shared range.
        choose(iface, root.findLayer(t2.id()))
        assert dock.group is None and dock.layer is t2 and dock.share.isHidden()
        row.slider.setValue(row.slider.value() + 10)
        dock.apply()
        assert er.enhance_renderer(t2).settings.share == key
        assert er.enhance_renderer(t1).settings.correction.get("contrast")["amount"] == pytest.approx(0.2)
        choose(iface, group)
        assert "補正の違うレイヤーあり" in dock.layer_label.text()

        # Moved out of the group: the layer takes its own range again.
        node = root.findLayer(t3.id())
        root.insertChildNode(0, node.clone())
        inner.removeChildNode(node)
        dock._tree_changed()
        assert er.enhance_renderer(t3).settings.share == ""
        assert [x.id() for x in dock.targets] == [t1.id(), t2.id()]

        # Turned off: kept on the group, so the box comes back unticked.
        dock.share.setChecked(False)
        dock.apply()
        assert group.customProperty(eg.SHARE_PROPERTY) == eg.OFF
        assert all(er.enhance_renderer(x).settings.share == "" for x in (t1, t2))
        choose(iface, root.findLayer(t1.id()))
        choose(iface, group)
        assert not dock.share.isChecked()

        # 補正 off: every raster of the group back to QGIS's renderer.
        dock.enabled.setChecked(False)
        dock.apply()
        assert er.enhance_renderer(t1) is None and er.enhance_renderer(t2) is None

        # A layer leaving the project leaves the dock's targets.
        project.removeMapLayer(t2.id())
        assert [x.id() for x in dock.targets] == [t1.id()]
    finally:
        dock.close_down()
        project.removeAllMapLayers()
        root.removeAllChildren()


def test_unlike_rasters_keep_their_own_bands_and_range(tmp_path):
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    color, _ = tile(tmp_path / "c.tif", 1000, 3000, bands=4, seed=1)
    gray, _ = tile(tmp_path / "g.tif", 1050, 100, bands=1, dtype=gdal.GDT_Byte, seed=2)
    iface, dock = make_dock()
    try:
        group = root.addGroup("mixed")
        for layer in (color, gray):
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        er.apply(color, er.EnhanceSettings(bands=(4, 3, 2)))
        iface.canvas.setDestinationCrs(color.crs())
        iface.canvas.setExtent(QgsRectangle(1000, 1960, 1100, 2000))

        choose(iface, group)
        assert not eg.alike(dock.targets)
        assert dock.bands_box.isHidden() and not dock.share.isEnabled() and not dock.share.isChecked()
        assert "バンド構成の違う画像あり" in dock.layer_label.text()

        dock.use_preset("vivid")
        dock.apply()
        assert er.enhance_renderer(color).settings.bands == (4, 3, 2)
        assert er.enhance_renderer(gray).settings.bands == (1,)
        assert er.enhance_renderer(color).settings.share == er.enhance_renderer(gray).settings.share == ""
        assert group.customProperty(eg.SHARE_PROPERTY) is None
        ops = [op["op"] for op in er.enhance_renderer(gray).settings.correction.ops]
        assert ops == [op["op"] for op in er.enhance_renderer(color).settings.correction.ops]

        # A colour scale chosen on the top (colour) image goes to the colour ones only.
        dock.colorize(gray, "turbo")
        choose(iface, group)
        dock.colorize_box.scale.setCurrentIndex(dock.colorize_box.scale.findData("red"))
        dock.apply()
        assert er.enhance_renderer(color).settings.color == {"scale": "red"}
        assert er.enhance_renderer(gray).settings.color == {"scale": "turbo"}
    finally:
        dock.close_down()
        project.removeAllMapLayers()
        root.removeAllChildren()


def test_a_shared_range_comes_back_with_the_project(tmp_path):
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    t1, _ = tile(tmp_path / "p1.tif", 1000, 3000, seed=1)
    t2, _ = tile(tmp_path / "p2.tif", 1050, 5000, seed=2)
    iface, dock = make_dock()
    try:
        group = root.addGroup("scene")
        for layer in (t1, t2):
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        dock.follow_group(group)
        dock.enabled.setChecked(True)
        dock.apply()
        key = eg.share_key(group)
        ids = sorted([t1.id(), t2.id()])
        assert sorted(er.share_members(key)) == ids
        path = str(tmp_path / "p.qgz")
        assert project.write(path)
        project.clear()
        assert er.share_members(key) == []
        assert project.read(path)
        assert sorted(er.share_members(key)) == ids
        assert eg.share_key(project.layerTreeRoot().findGroup("scene")) == key
    finally:
        dock.close_down()
        project.clear()
