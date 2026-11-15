"""
The QGIS side of 「外部から図形を受け取る」: the plugin listens at 127.0.0.1
(the settings file's port), what an extension posts becomes layers in
「受け取った図形」 and the map goes there; pages are refused; a port taken
is only told in the log; unloading closes the port.
"""
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsRectangle, QgsSettings, QgsWkbTypes,
)

from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.link_shapes_io import GROUP, SENDER_PROPERTY  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui import shape_receiver  # noqa: E402

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
EXTENSION = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    from mikage import classFactory

    monkeypatch.setattr(shape_receiver.ShapeReceiver, "LISTEN", True)
    QgsSettings().remove("mikage")
    port = free_port()
    (tmp_path / "config.json").write_text(json.dumps({"shapeReceiver": {"port": port}}), encoding="utf-8")
    settings.set_config_source(str(tmp_path / "config.json"))
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    canvas = iface.canvas
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(-2e6, -2e6, 2e6, 2e6))
    yield p, iface, port
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsProject.instance().layerTreeRoot().removeAllChildren()
    QgsSettings().remove("mikage")


def send(port, method="POST", path="/show", body=None, headers=None):
    """A request from another thread (the server runs in this one), answered as (status, JSON or text)."""
    out = {}

    def run():
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                out["result"] = (r.status, r.read().decode("utf-8"), dict(r.headers))
        except urllib.error.HTTPError as e:
            out["result"] = (e.code, e.read().decode("utf-8"), dict(e.headers))
        except OSError as e:
            out["result"] = (None, str(e), {})

    thread = threading.Thread(target=run)
    thread.start()
    assert wait_for(lambda: "result" in out)
    thread.join()
    status, text, h = out["result"]
    return status, (json.loads(text) if text.startswith("{") else text), h


def test_an_extension_sends_points_and_polygons_and_the_map_goes_there(plugin):
    p, iface, port = plugin
    receiver = p.api.shape_receiver
    assert receiver.listening and receiver.url == f"http://127.0.0.1:{port}" and receiver.problem == ""
    assert send(port, "GET", "/status", headers={"Origin": EXTENSION})[:2] == (200, {"app": "MIKAGE", "ok": True})
    collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [139.7671, 35.6812]}, "properties": {"label": "35.6812, 139.7671", "kind": "latlon"}},
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[139.70, 35.60], [139.80, 35.60], [139.80, 35.70], [139.70, 35.60]]]},
         "properties": {"label": "WKT の面", "kind": "wkt"}},
    ]}
    body = json.dumps({"geojson": collection, "name": "AutoMap: 報告書"}).encode()
    status, answer, headers = send(port, body=body, headers={"Origin": EXTENSION, "Content-Type": "application/json"})
    assert status == 200 and answer == {"app": "MIKAGE", "ok": True, "count": 2, "layer": "AutoMap: 報告書", "errors": []}
    assert headers["Access-Control-Allow-Origin"] == EXTENSION
    group = QgsProject.instance().layerTreeRoot().findGroup(GROUP)
    assert group is not None and QgsProject.instance().layerTreeRoot().children()[0] is group
    layers = [n.layer() for n in group.findLayers()]
    assert [layer.name() for layer in layers] == ["AutoMap: 報告書（点）", "AutoMap: 報告書（面）"]
    points, polygons = layers
    assert points.geometryType() == QgsWkbTypes.GeometryType.PointGeometry and polygons.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry
    assert [f["label"] for f in points.getFeatures()] == ["35.6812, 139.7671"] and [f["kind"] for f in polygons.getFeatures()] == ["wkt"]
    assert points.labelsEnabled() and points.customProperty(SENDER_PROPERTY) == EXTENSION
    # The map shows both.
    canvas = iface.canvas
    extent = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WGS84, QgsProject.instance()).transformBoundingBox(canvas.extent())
    assert extent.contains(QgsRectangle(139.70, 35.60, 139.80, 35.70)) and extent.width() < 1
    assert "から図形 2 個を受け取り、レイヤー「AutoMap: 報告書」に追加" in iface.bar.currentItem().text()
    # A single point: centred at the zoom given.
    status, answer, _ = send(port, body=json.dumps({"geom": "54SUE8843349290", "label": "MGRS", "zoom": 16}).encode(),
                             headers={"Origin": EXTENSION, "Content-Type": "application/json"})
    assert status == 200 and answer["count"] == 1 and canvas.scale() == pytest.approx(8530.9, rel=0.01)
    assert [n.layer().name() for n in group.findLayers()][0] == "受け取った図形"


def test_pages_are_refused_and_nothing_is_added(plugin):
    p, iface, port = plugin
    status, answer, headers = send(port, body=b"geom=35,139", headers={"Origin": "https://evil.example"})
    assert status == 403 and "送信元 https://evil.example からは受け取らない" in answer["error"] and "Access-Control-Allow-Origin" not in headers
    status, _, _ = send(port, "GET", "/show?geom=35,139", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})
    assert status == 403
    status, answer, _ = send(port, "GET", "/show?geom=abc")
    assert status == 422 and answer["errors"] == ["geom=abc: 座標として読み込み不可: abc"]
    assert QgsProject.instance().layerTreeRoot().findGroup(GROUP) is None
    # Python's own way in.
    assert p.api.shape_receiver.show_link(f"http://127.0.0.1:{port}/show?line=35,139;36,140&name=線") == (1, "線", [])
    assert [n.layer().name() for n in QgsProject.instance().layerTreeRoot().findGroup(GROUP).findLayers()] == ["線"]


def test_a_port_taken_is_told_in_the_log_and_unloading_closes_the_port(plugin, tmp_path):
    p, iface, port = plugin
    receiver = p.shape_receiver
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    other = taken.getsockname()[1]
    (tmp_path / "config.json").write_text(json.dumps({"shapeReceiver": {"port": other}}), encoding="utf-8")
    p.context.reload()
    assert wait_for(lambda: not receiver.listening)
    assert f"ポート {other} で待ち受けられない" in receiver.problem
    taken.close()
    p.context.reload()
    assert wait_for(lambda: receiver.listening and receiver.port == other)
    (tmp_path / "config.json").write_text(json.dumps({"shapeReceiver": False}), encoding="utf-8")
    p.context.reload()
    assert wait_for(lambda: not receiver.listening) and receiver.problem == ""
    (tmp_path / "config.json").write_text(json.dumps({"shapeReceiver": {"port": port}}), encoding="utf-8")
    p.context.reload()
    assert wait_for(lambda: receiver.listening)
    p.unload()
    s = socket.socket()
    s.bind(("127.0.0.1", port))  # free again
    s.close()
    # (the fixture unloads once more: nothing left to do)
    p.unload = lambda: None
