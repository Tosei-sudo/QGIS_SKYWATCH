"""
The QGIS side of 「座標・地名へ移動」: the field reads coordinates and
looks up places (a CSV next to the settings file), the cursor position
shows in the status bar in the notation chosen, and the map's right-click
menu copies the point.
"""
import json

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRectangle, QgsSettings  # noqa: E402
from qgis.gui import QgsMapMouseEvent  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtGui import QGuiApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QMenu  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


@pytest.fixture
def plugin(tmp_path):
    from satellite_observation import classFactory

    QgsSettings().remove("satellite_observation")
    (tmp_path / "places.csv").write_bytes("名称,種別,緯度,経度\n横須賀港,港,35.28,139.67\n横浜港,港,35.45,139.65\nヨコハマ駅,駅,35.46,139.62\n横浜港の灯台,,35.44,139.66\n".encode("cp932"))
    (tmp_path / "config.json").write_text(json.dumps({"geocoders": [{"type": "csv", "label": "地名表", "url": "./places.csv"}]}), encoding="utf-8")
    settings.set_config_source(str(tmp_path / "config.json"))
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    canvas = iface.canvas
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(-2e6, -2e6, 2e6, 2e6))
    canvas.refresh()
    yield p, iface
    p.unload()
    QgsSettings().remove("satellite_observation")


def lon_lat_of(canvas, point):
    p = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WGS84, QgsProject.instance()).transform(point)
    return p.x(), p.y()


def test_the_field_reads_coordinates_and_goes_there_with_a_mark(plugin):
    p, iface = plugin
    box, canvas = p.api.coordinates, iface.canvas
    assert box.toolbar.windowTitle() == "座標・地名へ移動"
    assert box.field.placeholderText() == "緯度経度・MGRS・UTM・地名・住所"
    box.field.setText("54SUE8843349290")
    box.submit()
    lon, lat = lon_lat_of(canvas, canvas.center())
    assert (lon, lat) == pytest.approx((139.7671, 35.6812), abs=1e-4)
    assert lon_lat_of(canvas, box.marked) == pytest.approx((139.7671, 35.6812), abs=1e-4)
    assert canvas.scale() <= 8531
    assert box.message == "移動: 35.681204, 139.767101"
    # Already closer: the scale stays.
    canvas.zoomScale(1000)
    box.jump("北緯35度 東経139度")
    assert canvas.scale() == pytest.approx(1000, rel=0.01)
    assert lon_lat_of(canvas, canvas.center()) == pytest.approx((139, 35), abs=1e-6)
    assert box.jump("35.6, 200") is None
    assert box.message == "座標として読み込み失敗: 35.6, 200（緯度経度・MGRS・UTM に対応）"


def test_places_from_a_csv_next_to_the_settings_file(plugin):
    p, iface = plugin
    box, canvas = p.api.coordinates, iface.canvas
    box.field.setText("よこはま")
    box._typed()
    assert wait_for(lambda: box._shown_for == "よこはま")
    assert [(x.name, x.source, x.detail) for x in box.places_found] == [("ヨコハマ駅", "地名表", "駅")]
    box.field.setText("横浜港")
    box._typed()
    assert wait_for(lambda: box._shown_for == "横浜港")
    assert [x.name for x in box.places_found] == ["横浜港", "横浜港の灯台"]
    assert box.model.item(0).text() == "横浜港　地名表　港"
    box.field.setText("横須賀")
    box._typed()
    assert wait_for(lambda: box._shown_for == "横須賀")
    box.go_to_place(box.places_found[0])
    assert lon_lat_of(canvas, canvas.center()) == pytest.approx((139.67, 35.28), abs=1e-6)
    assert box.field.text() == "横須賀港" and box.message == "移動: 横須賀港（地名表）"
    # Enter on a name not shown yet: the first place found.
    box.field.setText("横浜")
    box.submit()
    assert wait_for(lambda: box.message == "移動: 横浜港（地名表）")
    box.field.setText("どこにもない")
    box.submit()
    assert wait_for(lambda: box.message.startswith("該当なし: どこにもない"))
    # A coordinate is never looked up as a name.
    box.field.setText("35, 135")
    box._typed()
    assert not box.timer.isActive()


def test_without_geocoders_only_coordinates(plugin):
    p, _ = plugin
    settings.set_config_source("")
    p.context.reload()
    box = p.api.coordinates
    assert not box.search.enabled
    assert box.field.placeholderText() == "緯度経度・MGRS・UTM"
    box.field.setText("横浜")
    box.submit()
    assert box.message == "座標として読み込み失敗: 横浜（緯度経度・MGRS・UTM に対応）"


def test_the_cursor_position_in_the_chosen_notation_and_the_right_click_menu(plugin):
    p, iface = plugin
    box, canvas = p.api.coordinates, iface.canvas
    to_map = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
    tokyo = to_map.transform(QgsPointXY(139.7671, 35.6812))
    canvas.xyCoordinates.emit(tokyo)
    assert box.position.text() == "35.681200, 139.767100" and not box.position.isHidden()
    # A point a world to the east: longitude within ±180°.
    canvas.xyCoordinates.emit(QgsPointXY(tokyo.x() + 2 * 20037508.342789244, tokyo.y()))
    assert box.position.text() == "35.681200, 139.767100"

    canvas.setCenter(tokyo)
    canvas.zoomScale(5000)
    event = QgsMapMouseEvent(canvas, QEvent.Type.MouseButtonPress, QPoint(400, 300), Qt.MouseButton.RightButton)
    menu = QMenu()
    canvas.contextMenuAboutToShow.emit(menu, event)
    [copy] = [a.menu() for a in menu.actions() if a.text() == "緯度経度・MGRS・UTM のコピー"]
    labels = [a.text().split("　")[0] for a in copy.actions()]
    assert labels == ["緯度, 経度", "MGRS", "UTM", "EPSG:3857"]
    copy.actions()[1].trigger()
    assert QGuiApplication.clipboard().text().startswith("54S UE ")
    assert box.message.startswith("MGRS をコピー: 54S UE ")
    [shown] = [a.menu() for a in menu.actions() if a.text() == "ステータスバーの表示"]
    assert [a.text() for a in shown.actions()] == ["10進", "度分秒", "MGRS", "UTM"]
    [dms] = [a for a in shown.actions() if a.text() == "度分秒"]
    dms.trigger()
    canvas.xyCoordinates.emit(tokyo)
    assert box.position.text() == "35°40'52.32\"N 139°46'01.56\"E"
    # Kept for the next time.
    from satellite_observation.ui.coordinate_box import cursor_notation

    assert cursor_notation() == "dms"
    # Off the map: hidden.
    QGuiApplication.sendEvent(canvas.viewport(), QEvent(QEvent.Type.Leave))
    assert box.position.isHidden()


def test_unload_takes_everything_away(plugin):
    p, iface = plugin
    box = p.api.coordinates
    box.jump("35, 135")
    marker = box._marker
    assert marker.scene() is iface.canvas.scene()
    p.unload()
    assert marker.scene() is None
    assert p.coordinates is None
    p.initGui()  # the fixture unloads again
