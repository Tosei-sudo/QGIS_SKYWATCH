"""
「スワイプ比較」 in QGIS: the active layer on one side of the line only, the
map without it on the other, the line moved and turned, the tool put away.
"""
import numpy as np
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings  # noqa: E402
from qgis.gui import QgsMapToolPan  # noqa: E402
from qgis.PyQt.QtCore import QEvent, Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QKeyEvent  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.ui.swipe import HORIZONTAL, VERTICAL, swipe_rect  # noqa: E402


def rgb(path, color):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 50, 50, 3, gdal.GDT_Byte)
    ds.SetGeoTransform([0, 1, 0, 50, 0, -1])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    ds.SetProjection(srs.ExportToWkt())
    for i, v in enumerate(color):
        ds.GetRasterBand(i + 1).WriteArray(np.full((50, 50), v, np.uint8))
    ds = None
    layer = QgsRasterLayer(str(path), path.stem)
    assert layer.isValid()
    return layer


@pytest.fixture
def plugin(tmp_path):
    from satellite_observation import classFactory

    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    red, blue = rgb(tmp_path / "red.tif", (255, 0, 0)), rgb(tmp_path / "blue.tif", (0, 0, 255))
    QgsProject.instance().addMapLayers([red, blue])
    canvas = iface.canvas
    canvas.setDestinationCrs(red.crs())
    canvas.setLayers([red, blue])
    canvas.setExtent(QgsRectangle(5, 5, 45, 45))
    yield p, iface, red, blue
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def color_at(canvas, x_share, y_share):
    canvas.viewport().repaint()
    image = canvas.grab().toImage()
    return QColor(image.pixel(int(image.width() * x_share), int(image.height() * y_share))).name()


def test_swipe_rect():
    assert swipe_rect(VERTICAL, 0.25, 800, 600) == (0, 0, 200, 600)
    assert swipe_rect(HORIZONTAL, 1.5, 800, 600) == (0, 0, 800, 600)


def test_the_active_layer_shows_left_of_the_line_and_the_layers_beneath_right(plugin):
    p, iface, red, blue = plugin
    canvas, swipe = iface.canvas, p.api.swipe
    canvas.refresh()
    assert wait_for(lambda: not canvas.isDrawing())
    iface.setActiveLayer(red)
    pan = QgsMapToolPan(canvas)
    canvas.setMapTool(pan)
    swipe.action.trigger()
    assert swipe.active and canvas.mapTool() is swipe.tool and swipe.swiped_layers() == [red]
    assert wait_for(swipe.ready)
    assert color_at(canvas, 0.25, 0.5) == "#ff0000" and color_at(canvas, 0.75, 0.5) == "#0000ff"
    # Moved and turned: red above the line.
    swipe.set_orientation(HORIZONTAL)
    swipe.set_position(0.3)
    assert color_at(canvas, 0.5, 0.15) == "#ff0000" and color_at(canvas, 0.5, 0.6) == "#0000ff"
    # Keys move it.
    canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_End, Qt.KeyboardModifier.NoModifier))
    assert swipe.overlay.position == 1
    # Another active layer: it follows (blue swiped: red, beneath, shows below the line).
    swipe.set_position(0.5)
    iface.setActiveLayer(blue)
    assert wait_for(swipe.ready)
    assert color_at(canvas, 0.5, 0.75) == "#ff0000"
    # Esc: off, and the tool before comes back.
    canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert not swipe.active and not swipe.action.isChecked() and canvas.mapTool() is pan
    assert not swipe.overlay.isVisible()


def test_another_tool_turns_it_off(plugin):
    p, iface, red, blue = plugin
    swipe = p.api.swipe
    iface.setActiveLayer(red)
    swipe.set_active(True)
    assert swipe.action.isChecked()
    iface.canvas.setMapTool(QgsMapToolPan(iface.canvas))
    assert not swipe.active and not swipe.action.isChecked()
