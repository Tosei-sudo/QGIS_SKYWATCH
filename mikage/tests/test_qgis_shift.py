"""
「位置ずらし」 in QGIS: the active image moved by a drag or the arrow keys
(through a VRT of its file, its style kept), the move read in metres, and
put back.
"""
import os

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings, QgsSingleBandGrayRenderer  # noqa: E402
from qgis.gui import QgsMapToolPan  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QPointF, Qt  # noqa: E402
from qgis.PyQt.QtGui import QKeyEvent, QMouseEvent  # noqa: E402

from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io import shift_io  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402
from mikage.features.enhance import Enhance  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.ui.shift import format_shift  # noqa: E402


def image(path):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 50, 50, 3, gdal.GDT_Byte)
    ds.SetGeoTransform([500000, 10, 0, 4000000, 0, -10])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32654)
    ds.SetProjection(srs.ExportToWkt())
    for i in range(3):
        ds.GetRasterBand(i + 1).WriteArray(np.full((50, 50), 100 + i, np.uint8))
    ds = None
    return str(path)


def test_set_shift_points_the_layer_at_a_moved_vrt_and_back(tmp_path):
    path = image(tmp_path / "a.tif")
    layer = QgsRasterLayer(path, "a")
    layer.setRenderer(QgsSingleBandGrayRenderer(layer.dataProvider(), 2))
    shift_io.set_shift(layer, 30, -20, folder=str(tmp_path))
    assert layer.isValid() and layer.source().endswith(".vrt") and shift_io.get_shift(layer) == (30, -20)
    assert layer.extent().xMinimum() == 500030 and layer.extent().yMaximum() == 3999980
    assert layer.renderer().type() == "singlebandgray" and layer.renderer().grayBand() == 2
    first = layer.source()
    # Moving again starts from the file; the VRT moved from is removed.
    shift_io.set_shift(layer, 10, 0, folder=str(tmp_path))
    assert layer.extent().xMinimum() == 500010 and layer.extent().yMaximum() == 4000000 and not os.path.exists(first)
    assert shift_io.original_source(layer) == path
    shift_io.set_shift(layer, 0, 0, folder=str(tmp_path))
    assert layer.source() == path and shift_io.get_shift(layer) == (0, 0) and layer.customProperty(shift_io.SOURCE) is None
    assert format_shift(12, -3.46) == "ずらし量: 東 +12.0 m、北 −3.5 m"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(shift_io, "shifts_folder", lambda: str(tmp_path))
    QgsSettings().remove("mikage")
    settings.set_config_source("")
    iface = FakeIface()
    p = small_plugin(iface, Enhance)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    layer = QgsRasterLayer(image(tmp_path / "scene.tif"), "scene")
    QgsProject.instance().addMapLayers([layer])
    canvas = iface.canvas
    canvas.setDestinationCrs(layer.crs())
    canvas.setLayers([layer])
    canvas.resize(400, 400)
    canvas.setExtent(QgsRectangle(499800, 3999300, 500700, 4000200))
    iface.setActiveLayer(layer)
    yield p, iface, layer
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def at(canvas, kind, x, y):
    from qgis.gui import QgsMapMouseEvent

    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    return QgsMapMouseEvent(canvas, QMouseEvent(kind, QPointF(x, y), Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier))


def test_drag_and_keys_move_the_active_image(plugin):
    p, iface, layer = plugin
    shift = p.api.shift
    canvas = iface.canvas
    shift.action.trigger()
    assert shift.active and canvas.mapTool() is shift.tool
    per_pixel = canvas.mapUnitsPerPixel()
    tool = shift.tool
    tool.canvasPressEvent(at(canvas, QEvent.Type.MouseButtonPress, 100, 100))
    assert shift.overlay.image is not None and shift.overlay.isVisible()
    tool.canvasMoveEvent(at(canvas, QEvent.Type.MouseMove, 110, 95))
    assert shift.overlay.offset == QPointF(10, -5)
    tool.canvasReleaseEvent(at(canvas, QEvent.Type.MouseButtonRelease, 110, 95))
    assert not shift.overlay.isVisible()
    dx, dy = shift_io.get_shift(layer)
    assert dx == pytest.approx(10 * per_pixel) and dy == pytest.approx(5 * per_pixel)
    east, north = shift.shift_metres(layer)
    assert east == pytest.approx(dx, rel=0.01) and north == pytest.approx(dy, rel=0.01)
    tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier))
    assert shift_io.get_shift(layer)[0] == pytest.approx(0)
    shift.set_shift_metres(layer, 100, 0)
    assert layer.extent().xMinimum() == pytest.approx(500100, abs=1.5)
    shift.reset()
    assert shift_io.get_shift(layer) == (0, 0) and layer.extent().xMinimum() == 500000
    tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert not shift.active and not shift.action.isChecked()
    # Another tool puts it away too.
    shift.action.trigger()
    canvas.setMapTool(QgsMapToolPan(canvas))
    assert not shift.active



def placed(path, how):
    """A 16-bit SAR-like image placed by GCPs, an RPC model or nothing (QGIS's pixel grid)."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 40, 30, 1, gdal.GDT_UInt16)
    ds.GetRasterBand(1).WriteArray((np.random.default_rng(1).random((30, 40)) * 3000 + 1).astype(np.uint16))
    if how == "gcps":
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetGCPs([gdal.GCP(139.70, 35.70, 0, 0, 0), gdal.GCP(139.74, 35.71, 0, 40, 0),
                    gdal.GCP(139.69, 35.67, 0, 0, 30), gdal.GCP(139.73, 35.68, 0, 40, 30)], srs.ExportToWkt())
    elif how == "rpc":
        zeros = " ".join(["0"] * 17)
        ds.SetMetadata({
            "LINE_OFF": "15", "SAMP_OFF": "20", "LAT_OFF": "35.69", "LONG_OFF": "139.715", "HEIGHT_OFF": "0",
            "LINE_SCALE": "15", "SAMP_SCALE": "20", "LAT_SCALE": "0.02", "LONG_SCALE": "0.025", "HEIGHT_SCALE": "100",
            "LINE_NUM_COEFF": "0 0 -1 " + zeros, "LINE_DEN_COEFF": "1 " + " ".join(["0"] * 19),
            "SAMP_NUM_COEFF": "0 1 0 " + zeros, "SAMP_DEN_COEFF": "1 " + " ".join(["0"] * 19),
        }, "RPC")
    ds = None
    return str(path)


@pytest.mark.parametrize("how", ["gcps", "rpc", "pixels"])
def test_an_image_placed_by_gcps_rpc_or_nothing_moves_where_qgis_shows_it(tmp_path, how):
    # Writing a geotransform into the VRT made QGIS place a GCP or RPC image by it alone: 1° pixels from
    # (0, 0), the whole map one gray pixel (tsudo 2026-11-18). The move goes where the file keeps its place.
    path = placed(tmp_path / f"{how}.tif", how)
    layer = QgsRasterLayer(path, how)
    assert layer.isValid()
    before = QgsRectangle(layer.extent())
    d = 0.001 if how != "pixels" else 5
    shift_io.set_shift(layer, d, d / 2, folder=str(tmp_path))
    # (An image with nothing gets QGIS's own pixel grid written, y down.)
    assert shift_io.placed_by(gdal.Open(layer.source())) == ("geotransform" if how == "pixels" else how)
    after = layer.extent()
    assert layer.isValid() and after.width() == pytest.approx(before.width(), rel=1e-3)
    assert after.height() == pytest.approx(before.height(), rel=1e-3)
    assert after.xMinimum() == pytest.approx(before.xMinimum() + d, abs=d / 100)
    assert after.yMaximum() == pytest.approx(before.yMaximum() + d / 2, abs=d / 100)
    shift_io.set_shift(layer, 0, 0, folder=str(tmp_path))
    assert layer.source() == path and layer.extent() == before


def test_a_moved_gcp_image_is_drawn_only_where_it_lies(tmp_path):
    from qgis.core import QgsMapRendererParallelJob, QgsMapSettings
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor

    from mikage.qgis_io import enhance_renderer as er

    layer = QgsRasterLayer(placed(tmp_path / "sar.tif", "gcps"), "sar")
    er.apply(layer, er.EnhanceSettings(bands=(1,)))
    shift_io.set_shift(layer, 0.002, 0, folder=str(tmp_path))
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(layer.crs())
    settings.setExtent(QgsRectangle(139.60, 35.60, 139.80, 35.80))
    settings.setOutputSize(QSize(200, 200))
    settings.setBackgroundColor(QColor(255, 0, 0))
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    picture = job.renderedImage()
    # The corners stay the map's (red); the image is a small part in the middle.
    for x, y in ((2, 2), (197, 2), (2, 197), (197, 197)):
        assert QColor(picture.pixel(x, y)).name() == "#ff0000"
    assert QColor(picture.pixel(100, 125)).name() != "#ff0000"


def test_the_layers_crs_and_its_correction_state_stay_across_a_move(tmp_path):
    from qgis.core import QgsCoordinateReferenceSystem

    from mikage.core.enhance import Pipeline
    from mikage.qgis_io import enhance_renderer as er

    path = str(tmp_path / "nocrs.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 50, 50, 3, gdal.GDT_UInt16)
    ds.SetGeoTransform([500000, 10, 0, 4000000, 0, -10])
    for i in range(3):
        ds.GetRasterBand(i + 1).WriteArray((np.random.default_rng(i).random((50, 50)) * 3000 + 1).astype(np.uint16))
    ds = None
    layer = QgsRasterLayer(path, "nocrs")
    layer.setCrs(QgsCoordinateReferenceSystem("EPSG:32654"))
    pipeline = Pipeline.from_json({"version": 1, "ops": [{"op": "autoStretch", "params": {}}]})
    settings = er.EnhanceSettings(bands=(1, 2, 3), pipeline=pipeline.to_json(), locked=True)
    renderer = er.apply(layer, settings)
    view = QgsRectangle(500100, 3999600, 500300, 3999900)
    er.set_view(layer, view)
    renderer.block(1, view, 20, 30)
    state = renderer.state
    kept = state.kept
    assert kept is not None
    shift_io.set_shift(layer, 30, -20, folder=str(tmp_path))
    assert layer.crs().authid() == "EPSG:32654"
    moved = er.enhance_renderer(layer)
    assert moved is not None and moved.state is state and state.view == view and state.kept == kept
    assert moved.settings == settings
    shift_io.set_shift(layer, 0, 0, folder=str(tmp_path))
    assert layer.crs().authid() == "EPSG:32654" and er.enhance_renderer(layer).state is state


@pytest.fixture
def group_plugin(tmp_path, monkeypatch):
    """The plugin with QGIS's layer panel and a group 「scene」 of two images (the second hidden) and a vector layer."""
    from qgis.core import QgsLayerTreeModel, QgsVectorLayer
    from qgis.gui import QgsLayerTreeView


    monkeypatch.setattr(shift_io, "shifts_folder", lambda: str(tmp_path))
    QgsSettings().remove("mikage")
    settings.set_config_source("")
    iface = FakeIface()
    project = QgsProject.instance()
    model = QgsLayerTreeModel(project.layerTreeRoot())
    view = QgsLayerTreeView()
    view.setModel(model)
    iface.layerTreeView = lambda: view
    iface._tree = (model, view)
    p = small_plugin(iface, Enhance)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    a = QgsRasterLayer(image(tmp_path / "a.tif"), "a")
    b = QgsRasterLayer(image(tmp_path / "b.tif"), "b")
    points = QgsVectorLayer("Point?crs=EPSG:32654", "points", "memory")
    alone = QgsRasterLayer(image(tmp_path / "alone.tif"), "alone")
    group = project.layerTreeRoot().addGroup("scene")
    for layer in (a, points, b):
        project.addMapLayer(layer, False)
        group.addLayer(layer)
    group.findLayer(b.id()).setItemVisibilityChecked(False)
    project.addMapLayer(alone)
    canvas = iface.canvas
    canvas.setDestinationCrs(a.crs())
    canvas.setLayers([a, alone])
    canvas.resize(400, 400)
    canvas.setExtent(QgsRectangle(499800, 3999300, 500700, 4000200))
    yield p, iface, group, (a, b, alone)
    p.unload()
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()
    QgsSettings().remove("mikage")


def choose(iface, node):
    """Chooses a node in the layer panel, as a click does (a layer also becomes the active one, a group none)."""
    _model, view = iface._tree
    view.setCurrentIndex(view.node2index(node))
    iface.setActiveLayer(view.currentLayer())


def test_with_a_group_chosen_every_image_under_it_moves_by_the_same(group_plugin):
    p, iface, group, (a, b, alone) = group_plugin
    shift = p.api.shift
    canvas = iface.canvas
    shift_io.set_shift(b, 5, 0)  # b was moved on its own before
    choose(iface, group)
    assert iface.activeLayer() is None and shift.group() is group and shift.layer() is None
    assert [layer.id() for layer in shift.targets()] == [a.id(), b.id()]
    shift.action.trigger()
    assert "グループ「scene」の画像 2 枚" in iface.messageBar().items()[-1].text()
    per_pixel = canvas.mapUnitsPerPixel()
    tool = shift.tool
    tool.canvasPressEvent(at(canvas, QEvent.Type.MouseButtonPress, 100, 100))
    assert shift.overlay.image is not None
    tool.canvasReleaseEvent(at(canvas, QEvent.Type.MouseButtonRelease, 120, 90))
    # Each by the same, from its own move so far; the image outside the group stays.
    assert shift_io.get_shift(a) == pytest.approx((20 * per_pixel, 10 * per_pixel))
    assert shift_io.get_shift(b) == pytest.approx((5 + 20 * per_pixel, 10 * per_pixel))
    assert shift_io.get_shift(alone) == (0, 0)
    assert "ずらし量は画像ごとに違う" in shift.readout_of(shift.targets())
    tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
    assert shift_io.get_shift(a)[0] == pytest.approx(21 * per_pixel)
    assert shift_io.get_shift(b)[0] == pytest.approx(5 + 21 * per_pixel)
    # 「位置ずらしの解除」 puts them all back.
    shift.reset()
    assert shift_io.get_shift(a) == (0, 0) and shift_io.get_shift(b) == (0, 0)
    assert a.extent().xMinimum() == 500000 and b.extent().xMinimum() == 500000
    # On a map in another CRS, the images of one CRS still move by exactly the same.
    from qgis.core import QgsCoordinateReferenceSystem

    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(canvas.fullExtent())
    tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier))
    assert shift_io.get_shift(a) == shift_io.get_shift(b) and shift_io.get_shift(a)[0] < 0
    shift.reset()
    canvas.setDestinationCrs(a.crs())
    canvas.setExtent(QgsRectangle(499800, 3999300, 500700, 4000200))
    per_pixel = canvas.mapUnitsPerPixel()
    # A layer chosen again: that layer alone.
    choose(iface, group.findLayer(a.id()))
    assert shift.group() is None and shift.targets() == [a]
    tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier))
    assert shift_io.get_shift(a)[1] == pytest.approx(per_pixel) and shift_io.get_shift(b) == (0, 0)


def test_an_image_in_quick_pansharpen_moves_with_its_pan(tmp_path, monkeypatch):
    """クイックパンシャープン (tsudo 2026-11-27): the picture moves as one, still sharpened, whichever of the two is chosen."""
    from mikage.qgis_io import enhance_renderer as er
    from mikage.qgis_io import quick_pan as qp
    from mikage.tests.test_qgis_quick_pan import X0, Y0, images, shown, started

    monkeypatch.setattr(shift_io, "shifts_folder", lambda: str(tmp_path))
    QgsSettings().remove("mikage")
    settings.set_config_source("")
    iface = FakeIface()
    p = small_plugin(iface, Enhance)
    p.initGui()
    try:
        for dock in p.docks.values():
            dock.hide()
        pan, ms = images(tmp_path)
        link = started(ms, pan)
        canvas = iface.canvas
        canvas.setDestinationCrs(ms.crs())
        canvas.setLayers([ms])
        canvas.resize(400, 400)
        canvas.setExtent(QgsRectangle(X0, Y0 - 240, X0 + 240, Y0))
        iface.setActiveLayer(ms)
        shift = p.api.shift
        shift.action.trigger()
        per_pixel = canvas.mapUnitsPerPixel()
        tool = shift.tool
        tool.canvasPressEvent(at(canvas, QEvent.Type.MouseButtonPress, 100, 100))
        # The drag's copy: the picture as the map shows it (sharpened).
        assert shift.overlay.image is not None
        tool.canvasReleaseEvent(at(canvas, QEvent.Type.MouseButtonRelease, 110, 95))
        assert shift_io.get_shift(ms) == pytest.approx((10 * per_pixel, 5 * per_pixel))
        assert shift_io.get_shift(pan) == shift_io.get_shift(ms)
        # Still sharpened, the pan hidden, the model as it was (the two where they were to each other).
        assert qp.link_of(ms) is link and not shown(pan) and er.enhance_renderer(ms).state.pan is link
        assert link.as_fitted() and not link.fitting
        assert "ずらし量: 東 +" in shift.readout_of(shift.targets())
        # The (hidden) pan chosen: the image moves with it.
        iface.setActiveLayer(pan)
        tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        assert shift_io.get_shift(pan)[0] == pytest.approx(11 * per_pixel) and shift_io.get_shift(ms) == shift_io.get_shift(pan)
        # By metres (the API) too.
        shift.set_shift_metres(ms, 20, 0)
        assert shift_io.get_shift(pan) == pytest.approx(shift_io.get_shift(ms)) and shift_io.get_shift(ms)[1] == 0
        assert link.as_fitted()
        # 「位置ずらしの解除」: both back where their files say, still sharpened.
        iface.setActiveLayer(ms)
        shift.reset()
        assert shift_io.get_shift(ms) == (0, 0) and shift_io.get_shift(pan) == (0, 0)
        assert ms.extent().xMinimum() == X0 and pan.extent().xMinimum() == X0
        assert qp.link_of(ms) is link and link.as_fitted() and not shown(pan)
        # Once it has ended, the image moves alone.
        qp.stop(ms)
        tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        assert shift_io.get_shift(ms)[0] == pytest.approx(per_pixel) and shift_io.get_shift(pan) == (0, 0)
    finally:
        p.unload()
        QgsProject.instance().removeAllMapLayers()
        QgsSettings().remove("mikage")
