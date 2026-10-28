"""
移動体（AIS・ADS-B）in QGIS: feeds over a WebSocket (Qt's or the module's
own client), Server-Sent Events and polling against local servers; the feed
layer at a time; the dock with a history file and an image's time; and the
check of detections against a feed.
"""
import base64
import hashlib
import http.server
import json
import socket
import struct
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer,
    QgsSettings, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, QTimeZone  # noqa: E402

from satellite_observation.core.moving import FeedConfig  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.moving_feeds import FeedConnection, stream_socket  # noqa: E402
from satellite_observation.qgis_io.moving_layer import OPEN, MovingLayer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000


def websocket_server(messages):
    """A one-connection WebSocket server sending `messages` (text frames, the last in two fragments), then waiting."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def frame(opcode, payload, fin=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
        return head + payload

    def serve():
        conn, _ = server.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = next(line.split(":", 1)[1].strip() for line in request.decode().split("\r\n") if line.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        conn.sendall(frame(0x9, b"hi"))
        for text in messages[:-1]:
            conn.sendall(frame(0x1, text.encode()))
        last = messages[-1].encode()
        conn.sendall(frame(0x1, last[:5], fin=False) + frame(0x0, last[5:]))
        try:
            conn.settimeout(10)
            conn.recv(4096)
        except OSError:
            pass
        conn.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return f"ws://127.0.0.1:{server.getsockname()[1]}/stream"


def test_websocket_feed_with_the_modules_own_client(monkeypatch):
    monkeypatch.setenv("SATOBS_MINI_WEBSOCKET", "1")
    url = websocket_server([json.dumps({"mmsi": 1, "lat": 35, "lon": 139}), "not json", json.dumps([{"mmsi": 2, "lat": 35.1, "lon": 139.1}])])
    got, states = [], []
    connection = FeedConnection(FeedConfig("ws", "websocket", url))
    connection.message.connect(lambda data, at: got.append(data))
    connection.status.connect(lambda state, detail: states.append(state))
    assert wait_for(lambda: len(got) == 2, 10000)
    assert got == [{"mmsi": 1, "lat": 35, "lon": 139}, [{"mmsi": 2, "lat": 35.1, "lon": 139.1}]]
    assert states[:2] == ["connecting", "open"] and connection.dropped == 1
    connection.close()
    assert states[-1] == "closed"


@pytest.fixture
def http_server():
    """Serves /poll.json (counts the requests), /events (two events) and /bad."""
    hits = {"poll": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/poll.json"):
                hits["poll"] += 1
                body = json.dumps({"type": "FeatureCollection", "features": [
                    {"type": "Feature", "properties": {"mmsi": 7}, "geometry": {"type": "Point", "coordinates": [139, 35]}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"mmsi": 1,\ndata: "lat": 35, "lon": 139}\n\n: comment\n\ndata: {"mmsi": 2, "lat": 1, "lon": 2}\n\n')
                self.wfile.flush()
            else:
                self.send_response(500)
                self.end_headers()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", hits
    httpd.shutdown()


def test_polling_and_server_sent_events(http_server):
    base, hits = http_server
    got = []
    poll = FeedConnection(FeedConfig("p", "poll", f"{base}/poll.json", interval=2))
    poll.message.connect(lambda data, at: got.append(data))
    assert wait_for(lambda: got, 10000) and got[0]["type"] == "FeatureCollection" and poll.state == "open"
    poll.close()
    events = []
    sse = FeedConnection(FeedConfig("s", "sse", f"{base}/events"))
    sse.message.connect(lambda data, at: events.append(data))
    assert wait_for(lambda: len(events) == 2, 10000)
    assert events == [{"mmsi": 1, "lat": 35, "lon": 139}, {"mmsi": 2, "lat": 1, "lon": 2}]
    sse.close()
    states = []
    bad = FeedConnection(FeedConfig("b", "poll", f"{base}/bad"))
    bad.status.connect(lambda state, detail: states.append((state, detail)))
    assert wait_for(lambda: any(s == "error" for s, _ in states), 10000)
    assert "HTTP 500" in next(d for s, d in states if s == "error")
    bad.close()


def test_a_feed_url_that_cannot_be_used_says_why():
    states = []
    connection = FeedConnection(FeedConfig("x", "poll", "ftp://example.com/feed"))
    connection.status.connect(lambda state, detail: states.append((state, detail)))
    assert wait_for(lambda: states, 5000)
    assert states[0][0] == "error" and states[0][1]
    connection.close()


def test_stream_service_description():
    body = json.dumps({"streamUrls": [{"transport": "ws", "urls": ["wss://geo.example/arcgis/ws/services/Ships/StreamServer"], "token": "abc"}],
                       "timeInfo": {"trackIdField": "TRACKID", "startTimeField": "TIME"}}).encode()
    ws, fields = stream_socket(body, 200, "https://geo.example/arcgis/rest/services/Ships/StreamServer?token=typed")
    assert ws == "wss://geo.example/arcgis/ws/services/Ships/StreamServer/subscribe?token=abc"
    assert fields == {"id": "TRACKID", "time": "TIME"}
    with pytest.raises(ValueError, match="streamUrls"):
        stream_socket(b"{}", 200, "https://a/StreamServer")
    with pytest.raises(ValueError, match="HTTP 403"):
        stream_socket(b"", 403, "https://a/StreamServer")


def history(minutes_lon_lat, ident="a"):
    return [{"mmsi": ident, "timestamp": datetime.fromtimestamp((T0 + m * 60_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "lat": lat, "lon": lon, "cog": 90, "flag": True} for m, lon, lat in minutes_lon_lat]


def test_a_feed_layer_at_a_time():
    layer = MovingLayer(FeedConfig("履歴", "poll", "x.csv"), history=True)
    try:
        layer.add_rows(history([(0, 139.0, 35), (20, 139.1, 35)]) + history([(5, 140, 36)], "b"))
        assert layer.kind == "ship" and layer.points.featureCount() == 2 and layer.trails.featureCount() == 1
        layer.set_time(T0 + 10 * 60_000)
        a = next(f for f in layer.points.getFeatures() if f["id"] == "a")
        assert a.geometry().asPoint().x() == pytest.approx(139.05) and a["state"] == "補間" and a["dir"] == 90 and a["flag"] == "true"
        assert next(f for f in layer.points.getFeatures() if f["id"] == "b")["state"] == "最新"
        # Before b's first report and long after a's last: neither is shown.
        layer.set_time(T0 - 60_000)
        assert layer.points.featureCount() == 0
        assert layer.status_text().startswith("履歴・2 件・2026-10-17 08:59:00 UTC の位置")
        assert OPEN[layer.points.id()] is layer
    finally:
        layer.close()
    assert layer.points.id() not in OPEN


@pytest.fixture
def dock(tmp_path):
    from satellite_observation import classFactory

    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p.api.moving, iface, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")


def test_the_dock_opens_a_history_file_and_follows_an_images_time(dock):
    moving, iface, tmp_path = dock
    path = tmp_path / "ais.csv"
    rows = history([(0, 139.0, 35), (20, 139.1, 35)])
    path.write_text("mmsi,timestamp,lat,lon\n" + "".join(f"{r['mmsi']},{r['timestamp']},{r['lat']},{r['lon']}\n" for r in rows))
    layer = moving.open_history(str(path))
    assert layer is not None and moving.list.topLevelItemCount() == 1 and "1 件" in moving.note.text()
    assert QgsProject.instance().mapLayer(layer.points.id()) is layer.points
    # An image taken at 09:10 UTC: the temporal controller to 08:40–09:10, the ship halfway.
    image_path = str(tmp_path / "scene_20261017T181000.tif")
    ds = gdal.GetDriverByName("GTiff").Create(image_path, 4, 4, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([139, 0.01, 0, 35.1, 0, -0.01])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetMetadata({"ACQUISITION_DATETIME": "2026-10-17T09:10:00Z"})
    ds = None
    image = QgsRasterLayer(image_path, "scene")
    QgsProject.instance().addMapLayer(image)
    assert moving.align_to_image(image)
    canvas = iface.mapCanvas()
    canvas.mapSettings().setIsTemporal(True)
    moving.follow_time()
    assert layer.time == T0 + 10 * 60_000
    assert next(layer.points.getFeatures()).geometry().asPoint().x() == pytest.approx(139.05)
    moving.go_live()
    assert layer.time is None
    moving.list.setCurrentItem(moving.list.topLevelItem(0))
    moving.close_selected()
    assert moving.list.topLevelItemCount() == 0 and not OPEN


def test_detections_checked_against_a_feed(dock):
    moving, iface, tmp_path = dock
    path = tmp_path / "ais.csv"
    rows = history([(0, 139.0, 35), (20, 139.1, 35)])
    path.write_text("mmsi,shipname,timestamp,lat,lon\n" + "".join(f"{r['mmsi']},A,{r['timestamp']},{r['lat']},{r['lon']}\n" for r in rows))
    feed = moving.open_history(str(path))
    detections = QgsVectorLayer("Polygon?crs=EPSG:4326&field=image_time:string&field=score:double", "検出", "memory")
    features = []
    for wkt, when in (("POLYGON((139.049 35.0,139.051 35.0,139.051 35.002,139.049 35.002,139.049 35.0))", "2026-10-17T09:10:00Z"),
                      ("POLYGON((139.2 35.0,139.201 35.0,139.201 35.001,139.2 35.001,139.2 35.0))", "2026-10-17T09:10:00Z"),
                      ("POLYGON((139.0 35.0,139.001 35.0,139.001 35.001,139.0 35.001,139.0 35.0))", None)):
        f = QgsFeature(detections.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes([when, 0.9])
        features.append(f)
    detections.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(detections)
    from satellite_observation.processing_provider.moving_match_algorithm import default_inputs

    assert default_inputs(list(QgsProject.instance().mapLayers().values()), detections) == {"INPUT": detections.id(), "FEED": feed.points.id()}
    algorithm = QgsApplication.processingRegistry().algorithmById("satobs:moving_match")
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    results, ok = algorithm.run({"INPUT": detections.id(), "FEED": feed.points.id(), "OUTPUT": "memory:"}, context, QgsProcessingFeedback())
    assert ok
    out = context.getMapLayer(results["OUTPUT"])
    rows = [(f["ais_status"], f["ais_id"], f["ais_name"], f["ais_dt_s"], f["score"]) for f in out.getFeatures()]
    assert rows == [("一致", "a", "A", 600, 0.9), ("なし", None, None, None, 0.9), ("時刻なし", None, None, None, 0.9)]
    assert 0 <= next(out.getFeatures())["ais_dist_m"] < 200
    # A typed time for every detection; and a layer that is not a feed is refused.
    typed = QDateTime.fromMSecsSinceEpoch(int(T0), QTimeZone.utc())
    results, ok = algorithm.run({"INPUT": detections.id(), "FEED": feed.points.id(), "TIME_FROM": 2, "TIME": typed, "OUTPUT": "memory:"},
                                context, QgsProcessingFeedback())
    assert ok and [f["ais_status"] for f in context.getMapLayer(results["OUTPUT"]).getFeatures()] == ["なし", "なし", "一致"]
    _, ok = algorithm.run({"INPUT": detections.id(), "FEED": detections.id(), "OUTPUT": "memory:"}, context, QgsProcessingFeedback())
    assert not ok
