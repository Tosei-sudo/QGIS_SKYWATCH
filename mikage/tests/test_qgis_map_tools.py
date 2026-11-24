"""
The plugin's map tools in turn (qgis_io/map_tools.py): whichever two are used
one after the other, turning the second off gives the map back to the
person's own tool, never to the first (whose button is off by then).

tsudo 2026-11-24: 「手順を踏まないと発生しない不具合」. 位置ずらし →
クイック幾何補正 → クイック幾何補正 off left 位置ずらし's tool on the map
with its button off, and a drag moved the image; the catalog's 地図で選択 →
スワイプ比較 turned the swipe off as it started; after the plugin was
unloaded one of its tools was still the map's.
"""
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMapToolEmitPoint, QgsMapToolPan  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402

from mikage.qgis_io import map_tools, settings, shift_io  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def geotiff(path):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 20, 20, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([139.6, 0.01, 0, 35.7, 0, -0.01])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetMetadataItem("ACQUISITION_START_TIME", "2026-10-01T01:00:00Z")
    ds.GetRasterBand(1).Fill(100)
    ds = None
    return str(path)


# ---- each tool: how it is turned on and off, and whether it is on -------------------------------------

def _entry(text):
    def on(p):
        p.entry_action(text).trigger()

    def off(p):
        if p.entry_action(text).isChecked():
            p.entry_action(text).trigger()

    return on, off, lambda p: p.entry_action(text).isChecked()


def _button(dock, get):
    def on(p):
        p.docks[dock].show()
        get(p.docks[dock]).click()

    def off(p):
        if get(p.docks[dock]).isChecked():
            get(p.docks[dock]).click()

    return on, off, lambda p: get(p.docks[dock]).isChecked()


def _recording():
    def on(p):
        p.api.observation_dock.show()
        p.api.observation_dock.set_active(True)

    return on, lambda p: p.api.observation_dock.set_active(False), lambda p: p.api.observation_dock.active


TOOLS = {
    "スワイプ比較": _entry("スワイプ比較"),
    "位置ずらし": _entry("位置ずらし"),
    "クイック幾何補正": _entry("クイック幾何補正"),
    "クリック抽出": _entry("クリック抽出（AI）"),
    "スペクトルの取得": _button("SatObsSpectral", lambda d: d.measure_button),
    "影から高さの計測": _button("SatObsShadowHeight", lambda d: d.measure_button),
    "カタログの範囲を描く": _button("SatObsImageCatalog", lambda d: d.draw_button),
    "カタログの地図で選択": _button("SatObsImageCatalog", lambda d: d.buttons["pick"]),
    "撮像計画の地図で指定": _button("SatObsImagingPlan", lambda d: d.pick),
    "地点ごとの撮像予定の地図で追加": _button("SatObsPlaceSchedule", lambda d: d.pick),
    "クリック記録": _recording(),
}


@pytest.fixture
def session(tmp_path):
    from mikage.plugin import MikagePlugin

    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")
    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    settings.set_startup_layers_on(False)
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")
    iface = FakeIface()
    p = MikagePlugin(iface)
    p.initGui()
    layer = QgsRasterLayer(geotiff(tmp_path / "a.tif"), "a")
    QgsProject.instance().addMapLayers([layer])
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    iface.canvas.setExtent(layer.extent())
    iface.setActiveLayer(layer)
    yield p, iface, layer, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def _name(tool) -> str:
    return type(tool).__name__ if tool is not None else "None"


def _in_turn(p, iface, layer, first: str, second: str) -> list:
    """`first` on, then `second` on and off: what went wrong (nothing: [])."""
    pan = QgsMapToolPan(iface.canvas)
    iface.canvas.setMapTool(pan)
    iface.setActiveLayer(layer)
    on1, off1, is_on1 = TOOLS[first]
    on2, off2, is_on2 = TOOLS[second]
    wrong = []
    try:
        on1(p)
        tool1 = iface.canvas.mapTool()
        if not (is_on1(p) and map_tools.is_ours(tool1)):
            return [f"{first} not on ({_name(tool1)})"]
        on2(p)
        tool2 = iface.canvas.mapTool()
        # The second on (not turned off as it started), the first off.
        if not (is_on2(p) and map_tools.is_ours(tool2) and tool2 is not tool1):
            wrong.append(f"{first} → {second}: {second} not on ({_name(tool2)})")
        if is_on1(p):
            wrong.append(f"{first} → {second}: {first} still on")
        off2(p)
        # Back to the person's pan: not the first's tool, whose button is off.
        if iface.canvas.mapTool() is not pan:
            wrong.append(f"{first} → {second} → off: the map's tool is {_name(iface.canvas.mapTool())}")
    finally:
        off2(p)
        off1(p)
        iface.canvas.setMapTool(pan)
    return wrong


def test_the_second_tool_turned_off_gives_the_map_back_to_the_persons_tool(session):
    """Every two of the tools in turn (110 orders)."""
    p, iface, layer, _tmp = session
    wrong = [w for first in TOOLS for second in TOOLS if first != second for w in _in_turn(p, iface, layer, first, second)]
    assert wrong == [], "\n".join(wrong)


def test_shift_turned_off_never_moves_the_image_on_a_drag(session, monkeypatch):
    """位置ずらし → クイック幾何補正 → クイック幾何補正 off (Esc): a drag pans, the image stays where it is."""
    p, iface, layer, tmp = session
    monkeypatch.setattr(shift_io, "shifts_folder", lambda: str(tmp))
    pan = QgsMapToolPan(iface.canvas)
    iface.canvas.setMapTool(pan)
    iface.setActiveLayer(layer)
    before = layer.extent().toString()
    shift, fix = p.entry_action("位置ずらし"), p.entry_action("クイック幾何補正")
    shift.trigger()
    fix.trigger()
    assert not shift.isChecked() and fix.isChecked()
    QTest.keyClick(iface.canvas, Qt.Key.Key_Escape)
    assert not fix.isChecked() and iface.canvas.mapTool() is pan
    viewport = iface.canvas.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 200))
    QTest.mouseMove(viewport, QPoint(360, 240))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(360, 240))
    wait_for(lambda: False, 100)
    assert layer.extent().toString() == before and shift_io.get_shift(layer) == (0.0, 0.0)


def test_after_the_plugin_is_unloaded_none_of_its_tools_is_the_maps(tmp_path):
    """Tools used in turn, the last still on as the plugin unloads (QGIS updating it): the person's pan is the map's tool."""
    from mikage.plugin import MikagePlugin

    QgsSettings().remove("mikage")
    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    settings.set_startup_layers_on(False)
    iface = FakeIface()
    p = MikagePlugin(iface)
    p.initGui()
    layer = QgsRasterLayer(geotiff(tmp_path / "a.tif"), "a")
    QgsProject.instance().addMapLayers([layer])
    iface.setActiveLayer(layer)
    pan = QgsMapToolPan(iface.canvas)
    iface.canvas.setMapTool(pan)
    try:
        for text in ("スワイプ比較", "位置ずらし", "クイック幾何補正"):
            p.entry_action(text).trigger()
        assert map_tools.is_ours(iface.canvas.mapTool())
    finally:
        p.unload()
        QgsProject.instance().removeAllMapLayers()
    assert iface.canvas.mapTool() is pan


def test_a_tool_whose_owner_stays_on_is_given_the_map_back_until_forgotten():
    """撮像依頼's form keeps its button pressed when another tool takes the map: its tool comes back, until the form closes."""
    canvas = QgsMapCanvas()
    pan, form, swipe = QgsMapToolPan(canvas), QgsMapToolEmitPoint(canvas), QgsMapToolEmitPoint(canvas)
    canvas.setMapTool(pan)
    map_tools.take(canvas, form, turns_off=False)
    map_tools.take(canvas, swipe)
    map_tools.give_back(canvas, swipe)
    assert canvas.mapTool() is form
    map_tools.take(canvas, swipe)
    map_tools.forget(canvas, form)
    map_tools.give_back(canvas, swipe)
    assert canvas.mapTool() is None
    # Another's tool taken while a tool is being given the map: nothing given back meanwhile.
    canvas.setMapTool(pan)
    other = QgsMapToolEmitPoint(canvas)
    map_tools.take(canvas, swipe)
    swipe.deactivated.connect(lambda: map_tools.give_back(canvas, swipe))
    map_tools.take(canvas, other)
    assert canvas.mapTool() is other
    map_tools.give_back(canvas, other)
    assert canvas.mapTool() is pan
