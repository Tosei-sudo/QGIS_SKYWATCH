"""
The plugin used as a person uses it in a day, then unloaded and started again
(QGIS updating it): bugs that only show after a certain order of steps.

tsudo 2026-11-24: 「他に似たような、手順を踏まないと発生しない不具合が無いか」.
The tests of one feature start it fresh each time; these go through every
screen and entry with images open, close them, remove the images and open
others, start a new project, unload, and start again. They fail on the
bugs found so far:

- PR #308: クイック幾何補正's panel closed, its image removed, then each image
  opened raised a Python error (a controller outliving its panel kept the
  removed layer);
- the map tools used in turn (位置ずらし → クイック幾何補正 → off brought
  位置ずらし's tool back with its button off; after unload a tool of the
  plugin was still the map's: tests/test_qgis_map_tools.py has each order);
- anything of the plugin still listening after it is unloaded (a signal
  left connected, a timer, a map tool): after unload, firing every signal of
  QGIS the plugin listens to runs none of its code.
"""
import gc
import os
import sys
import traceback
import types

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsDateTimeRange, QgsMessageLog, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings,
    QgsVectorLayer,
)
from qgis.gui import QgsMapToolPan  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, QEvent, QPoint, Qt, QTimer  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QDialog, QMessageBox, QWidget  # noqa: E402

import mikage  # noqa: E402
from mikage.qgis_io import map_tools, settings  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(os.path.abspath(mikage.__file__))
#: Entries that reach the network beyond the tests' Esri stand-in (the settings file's example.com layers) or open a submenu.
NOT_RUN = {"設定ファイルのレイヤーを追加", "ArcGIS Web マップを開く…", "背景地図"}
#: The map tools turned on in turn (each turns the one before off).
TOOLS = ("スワイプ比較", "位置ずらし", "クイック幾何補正")


def geotiff(folder, name: str) -> str:
    path = os.path.join(folder, name + ".tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 64, 64, 3, gdal.GDT_Byte)
    ds.SetGeoTransform((139.7, 0.0001, 0, 35.7, 0, -0.0001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for band in range(3):
        ds.GetRasterBand(band + 1).WriteArray((np.arange(64 * 64).reshape(64, 64) % 251).astype(np.uint8))
    ds = None
    return path


def image(folder, name: str) -> QgsRasterLayer:
    layer = QgsRasterLayer(geotiff(folder, name), name)
    QgsProject.instance().addMapLayers([layer])
    return layer


def pump(ms: int = 30) -> None:
    wait_for(lambda: False, ms)


def close_modals() -> None:
    """Whatever asks (a dialog's exec, a question) answered with Cancel, as nobody is there to answer."""
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, (QDialog, QMessageBox)) and widget.isModal() and widget.isVisible():
            widget.reject()


def click_map(iface) -> None:
    viewport = iface.canvas.viewport()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 200))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(320, 210))
    QTest.mouseMove(viewport, QPoint(360, 230))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(360, 230))


class PluginCalls:
    """The plugin's functions called (by Python's profiler): {"file:line name": count}."""

    def __init__(self):
        self.seen: dict = {}

    def __call__(self, frame, event, _arg):
        if event == "call":
            path = frame.f_code.co_filename
            if path.startswith(PLUGIN) and os.sep + "tests" + os.sep not in path:
                key = f"{os.path.relpath(path, PLUGIN)}:{frame.f_code.co_firstlineno} {frame.f_code.co_name}"
                self.seen[key] = self.seen.get(key, 0) + 1


def fire_everything(iface, folder) -> None:
    """Every signal of QGIS the plugin listens to, fired (the project, the map, the active layer, the log, the focus)."""
    project = QgsProject.instance()
    layer = image(folder, "z")
    vector = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "points", "memory")
    project.addMapLayers([vector])
    iface.setActiveLayer(layer)
    iface.setActiveLayer(vector)
    project.layerTreeRoot().findLayer(layer.id()).setName("renamed")
    iface.canvas.setExtent(QgsRectangle(139.0, 35.0, 140.0, 36.0))
    iface.canvas.refresh()
    pump(200)
    pan = QgsMapToolPan(iface.canvas)
    iface.canvas.setMapTool(pan)
    click_map(iface)
    iface.canvas.unsetMapTool(pan)
    iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    now = QDateTime.currentDateTime()
    iface.canvas.setTemporalRange(QgsDateTimeRange(now.addDays(-1), now))
    iface.canvas.xyCoordinates.emit(QgsPointXY(139.7, 35.7))
    QgsMessageLog.logMessage("after unload", "MIKAGE")
    a, b = QWidget(), QWidget()
    a.show()
    b.show()
    a.setFocus()
    b.setFocus()
    pump(20)
    a.close()
    b.close()
    project.setDirty(True)
    project.setDirty(False)
    project.removeMapLayers([layer.id()])
    iface.newProject()
    path = os.path.join(folder, "after.qgz")
    project.write(path)
    project.read(path)
    project.clear()
    # The plugin's timers (a second's wait at most).
    pump(1200)


def use_everything(plugin, iface, folder) -> None:
    """Every dock and entry with images open, the map tools in turn, the images closed and others opened, a new project."""
    project = QgsProject.instance()
    a, b = image(folder, "a"), image(folder, "b")
    points = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "points", "memory")
    project.addMapLayers([points])
    iface.setActiveLayer(a)
    iface.canvas.setExtent(QgsRectangle(139.70, 35.69, 139.71, 35.70))
    pump()
    for dock in plugin.docks.values():
        dock.show()
        dock.raise_()
        for layer in (b, points, a):
            iface.setActiveLayer(layer)
        pump(10)
        dock.hide()
    for text in plugin.menu_texts():
        if text in NOT_RUN:
            continue
        plugin.run_menu_entry(text)
        pump(20)
        iface.setActiveLayer(b)
        close_modals()
    for widget in QApplication.topLevelWidgets():
        if widget is not iface.window and widget.isVisible() and widget.windowTitle():
            widget.close()
    iface.setActiveLayer(a)
    for text in TOOLS:
        plugin.entry_action(text).trigger()
        click_map(iface)
    pan = QgsMapToolPan(iface.canvas)
    iface.canvas.setMapTool(pan)
    # The images closed (as the catalog opening the next in their place), others opened and looked at.
    project.removeMapLayers([a.id(), b.id()])
    pump(50)
    for i in range(2):
        other = image(folder, f"c{i}")
        iface.setActiveLayer(other)
        iface.canvas.setExtent(QgsRectangle(139.70 + i * 0.001, 35.69, 139.71, 35.70))
        iface.canvas.refresh()
        pump(60)
    for dock in plugin.docks.values():
        dock.show()
        pump(5)
        dock.hide()
    for text in TOOLS:
        plugin.entry_action(text).trigger()
        click_map(iface)
    iface.newProject()
    pump(100)
    iface.canvas.unsetMapTool(pan)
    # The last tool left on: the plugin unloads with it.
    plugin.entry_action(TOOLS[0]).trigger()


@pytest.fixture
def day(tmp_path, monkeypatch):
    """Python errors in Qt's slots and threads collected; QGIS's Processing stood in for; asked questions answered."""
    from mikage.ui import map_orient

    # A map orienter an earlier test in this process left held (a dock of its own never unloaded) answers the
    # project's signals: let go, so what answers QGIS after this test's unload is this plugin's alone.
    for orienter in list(map_orient._SHARED.values()):
        for user in list(orienter.users):
            map_orient._let_go(orienter, user)
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    settings.set_startup_layers_on(False)
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    errors: list = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: errors.append("".join(traceback.format_exception(kind, value, tb))))
    # QGIS's Processing plugin (its dialogs are its own): asked to open an algorithm's dialog, nothing more.
    processing = types.ModuleType("processing")
    processing.execAlgorithmDialog = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "processing", processing)
    answer = QTimer()
    answer.timeout.connect(close_modals)
    answer.start(100)
    yield FakeIface(), errors, str(tmp_path)
    answer.stop()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def _unload(plugin) -> None:
    plugin.unload()
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(10)
    gc.collect()


def test_a_day_of_use_raises_nothing_and_leaves_nothing_after_unload_and_a_second_start(day):
    from mikage.plugin import MikagePlugin

    iface, errors, folder = day
    plugin = MikagePlugin(iface)
    plugin.initGui()
    pump(100)
    use_everything(plugin, iface, folder)
    assert errors == [], "\n".join(errors)
    _unload(plugin)
    del plugin
    # Unloaded: none of its tools is the map's, and none of its code answers QGIS any more.
    assert not map_tools.is_ours(iface.canvas.mapTool())
    calls = PluginCalls()
    sys.setprofile(calls)
    try:
        fire_everything(iface, folder)
    finally:
        sys.setprofile(None)
    assert calls.seen == {}, "\n".join(calls.seen)
    assert errors == [], "\n".join(errors)
    # Started again (QGIS updating the plugin), on the same QGIS: it works as the first time.
    plugin = MikagePlugin(iface)
    plugin.initGui()
    pump(100)
    layer = image(folder, "again")
    iface.setActiveLayer(layer)
    for text in TOOLS:
        plugin.entry_action(text).trigger()
        click_map(iface)
    for dock in plugin.docks.values():
        dock.show()
        pump(5)
        dock.hide()
    _unload(plugin)
    assert errors == [], "\n".join(errors)
    assert not map_tools.is_ours(iface.canvas.mapTool())
