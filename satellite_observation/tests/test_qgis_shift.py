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

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io import shift_io  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface  # noqa: E402
from satellite_observation.ui.shift import format_shift  # noqa: E402


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
    from satellite_observation import classFactory

    monkeypatch.setattr(shift_io, "shifts_folder", lambda: str(tmp_path))
    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
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
    QgsSettings().remove("satellite_observation")


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

