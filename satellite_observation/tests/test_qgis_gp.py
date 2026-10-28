"""
サーバ処理 in QGIS, against a local server playing an ArcGIS geoprocessing
service and an OGC API – Processes one: requests through QGIS's network
access, a layer's features sent in both forms, a task as a Processing
algorithm whose results open as layers, and the dialog that reads a service.
"""
import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
    QgsVectorLayer,
)

from satellite_observation.core.config import parse_config_text  # noqa: E402
from satellite_observation.core.gp import GpClient, GpError, GpOutput  # noqa: E402
from satellite_observation.processing_provider import gp_algorithm  # noqa: E402
from satellite_observation.qgis_io import gp_io  # noqa: E402
from satellite_observation.tests.qgis_fakes import wait_for  # noqa: E402

TOOLS = "/arcgis/rest/services/Tools/GPServer"


def p(**extra):
    return {"direction": "esriGPParameterDirectionInput", "parameterType": "esriGPParameterTypeRequired", **extra}


BUFFER = {
    "name": "Buffer", "displayName": "バッファ", "executionType": "esriExecutionTypeSynchronous", "description": "点を広げる",
    "parameters": [
        p(name="Input Points", displayName="点", dataType="GPFeatureRecordSetLayer", defaultValue={"geometryType": "esriGeometryPoint"}),
        p(name="Distance", displayName="距離", dataType="GPLinearUnit", defaultValue={"distance": 2, "units": "esriKilometers"}),
        p(name="Shape", displayName="形", dataType="GPString", choiceList=["円", "四角"], defaultValue="円"),
        p(name="Count", displayName="回数", dataType="GPLong", parameterType="esriGPParameterTypeOptional"),
        {"name": "Output", "displayName": "結果", "dataType": "GPFeatureRecordSetLayer", "direction": "esriGPParameterDirectionOutput"},
    ],
}
SQUARE = {"rings": [[[15584728, 4257087], [15586728, 4257087], [15586728, 4259087], [15584728, 4257087]]]}


class Server:
    """Answers by (method, path); records what it was asked (method, path, form or JSON, headers)."""

    def __init__(self):
        self.asked = []
        self.polls = 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, method):
                path = urlparse(self.path).path
                size = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(size).decode() if size else ""
                body = {k: v[0] for k, v in parse_qs(raw).items()} if "urlencoded" in (self.headers.get("content-type") or "") else (
                    json.loads(raw) if raw else None)
                owner.asked.append((method, path, body, {k.lower(): v for k, v in self.headers.items()}))
                status, headers, value = owner.route(method, path, body, f"http://127.0.0.1:{self.server.server_port}")
                data = json.dumps(value).encode()
                self.send_response(status)
                for k, v in {"content-type": "application/json", **headers}.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self.answer("GET")

            def do_POST(self):  # noqa: N802
                self.answer("POST")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.root = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, method, path, body, root):
        if path == TOOLS:
            return 200, {}, {"tasks": ["Buffer"], "serviceDescription": "道具"}
        if path == f"{TOOLS}/Buffer":
            return 200, {}, BUFFER
        if path == f"{TOOLS}/Buffer/execute":
            return 200, {}, {"results": [
                {"paramName": "Output", "dataType": "GPFeatureRecordSetLayer", "value": {
                    "geometryType": "esriGeometryPolygon", "spatialReference": {"wkid": 102100, "latestWkid": 3857},
                    "fields": [{"name": "NAME", "type": "esriFieldTypeString", "alias": "名前"}, {"name": "WHEN", "type": "esriFieldTypeDate"}],
                    "features": [{"geometry": SQUARE, "attributes": {"NAME": "東京", "WHEN": 1700000000000}}]}},
                {"paramName": "Note", "dataType": "GPString", "value": "完了しました"},
            ], "messages": [{"type": "esriJobMessageTypeInformative", "description": "処理 1 件"}]}
        if path == f"{TOOLS}/Broken":
            return 200, {}, {"error": {"code": 400, "message": "Unable to complete operation", "details": ["入力が不正"]}}
        if path == "/ogc/processes":
            return 200, {}, {"processes": [{"id": "count", "title": "数える"}]}
        if path == "/ogc/processes/count":
            return 200, {}, {"id": "count", "title": "数える", "jobControlOptions": ["async-execute"],
                             "inputs": {"area": {"title": "範囲", "schema": {"format": "geojson-feature-collection"}},
                                        "when": {"title": "日時", "schema": {"type": "string", "format": "date-time"}, "minOccurs": 0}},
                             "outputs": {"points": {"title": "点"}, "n": {"title": "件数"}}}
        if path == "/ogc/processes/count/execution":
            return 201, {"location": f"{root}/ogc/jobs/7"}, {"jobID": "7", "status": "accepted"}
        if path == "/ogc/jobs/7":
            self.polls += 1
            return 200, {}, {"jobID": "7", "status": "successful", "message": "数え終わり",
                             "links": [{"rel": "results", "href": f"{root}/ogc/jobs/7/results"}]}
        if path == "/ogc/jobs/7/results":
            return 200, {}, {"points": {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [139.7, 35.6]}, "properties": {"n": 1, "name": "a"}},
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [139.8, 35.7]}, "properties": {"n": 2, "name": "b"}}]},
                "n": 2}
        return 404, {}, {"title": "なし"}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


@pytest.fixture
def provider():
    from satellite_observation.processing_provider.provider import SatObsProvider

    provider = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    yield provider
    QgsApplication.processingRegistry().removeProvider(provider)
    QgsProject.instance().removeAllMapLayers()
    gp_algorithm._tasks.clear()


def points_layer():
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string&field=n:integer", "点", "memory")
    features = []
    for name, n, (x, y) in (("東京", 1, (139.7, 35.68)), ("横浜", 2, (139.64, 35.44))):
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes([name, n])
        features.append(f)
    layer.dataProvider().addFeatures(features)
    return layer


def test_requests_through_qgis_answer_even_on_errors(server):
    status, headers, body = gp_io.http("GET", f"{server.root}/nowhere", None, {"accept": "application/json"})
    assert status == 404 and headers["content-type"] == "application/json" and json.loads(body) == {"title": "なし"}
    status, _, body = gp_io.http("POST", f"{server.root}{TOOLS}", b"f=json", {"content-type": "application/x-www-form-urlencoded"})
    assert status == 200 and json.loads(body)["tasks"] == ["Buffer"] and server.asked[-1][2] == {"f": "json"}
    with pytest.raises(GpError, match="接続不可"):
        gp_io.http("GET", "http://127.0.0.1:9/x?token=secret", None, {})


def test_a_layers_features_in_both_forms():
    layer = points_layer()
    value = gp_io.feature_input(layer, layer.crs())
    assert len(value) == 2 and value.geometry_type == "esriGeometryPoint"
    assert [f["name"] for f in value.fields] == ["name", "n"] and value.fields[0]["alias"] == "name"
    first = value.esri[0]
    assert first["attributes"] == {"name": "東京", "n": 1}
    assert abs(first["geometry"]["x"] - 15551332) < 5 and abs(first["geometry"]["y"] - 4256679) < 5
    assert value.geojson[0]["geometry"] == {"type": "Point", "coordinates": [139.7, 35.68]}
    rows = gp_io.feature_input(layer, layer.crs(), table=True)
    assert rows.geometry_type == "" and "geometry" not in rows.esri[0] and rows.geojson[0]["geometry"] is None


def test_results_as_layers():
    esri = GpOutput("features", "Output", "結果", format="esri", geometry_type="esriGeometryPolygon", srs="EPSG:3857",
                    fields=[{"name": "NAME", "type": "esriFieldTypeString", "alias": "名前"}, {"name": "WHEN", "type": "esriFieldTypeDate"}],
                    features=[{"geometry": SQUARE, "attributes": {"NAME": "東京", "WHEN": 1700000000000}}])
    layer = gp_io.features_layer(esri, "結果")
    assert layer.isValid() and layer.crs().authid() == "EPSG:3857" and layer.featureCount() == 1
    f = next(layer.getFeatures())
    assert f["NAME"] == "東京" and layer.fields().field("NAME").alias() == "名前" and f["WHEN"].toMSecsSinceEpoch() == 1700000000000
    assert f.geometry().type() == 2 and abs(f.geometry().area() - 2e6) < 1
    # No geometry type or fields: told by the first geometry, attributes typed by value.
    loose = GpOutput("features", "o", "o", format="esri", features=[{"geometry": {"x": 1, "y": 2}, "attributes": {"a": 1.5, "b": "x"}}])
    layer = gp_io.features_layer(loose, "o")
    assert layer.geometryType() == 0 and [f.name() for f in layer.fields()] == ["a", "b"] and next(layer.getFeatures())["a"] == 1.5
    geojson = GpOutput("features", "p", "点", format="geojson", srs="EPSG:4326", fields=[("n", "integer"), ("name", "string")], features=[
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [139.7, 35.6]}, "properties": {"n": 1, "name": "a"}}])
    layer = gp_io.features_layer(geojson, "点")
    f = next(layer.getFeatures())
    assert layer.crs().authid() == "EPSG:4326" and (f["n"], f["name"]) == (1, "a") and f.geometry().asWkt(1) == "MultiPoint ((139.7 35.6))"
    rows = GpOutput("features", "t", "表", format="esri", fields=[{"name": "A", "type": "esriFieldTypeInteger"}], features=[{"attributes": {"A": 3}}])
    layer = gp_io.features_layer(rows, "表")
    assert not layer.isSpatial() and next(layer.getFeatures())["A"] == 3


def test_an_esri_task_as_an_algorithm_whose_results_open_as_layers(server, provider):
    task = GpClient(gp_io.http).read(f"{server.root}{TOOLS}/Buffer")
    algorithm_id = gp_algorithm.register(task, "secret")
    algorithm = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    assert algorithm is not None and algorithm.displayName() == "バッファ" and algorithm.group() == "サーバ処理"
    names = [d.name() for d in algorithm.parameterDefinitions()]
    assert names == ["Input_Points", "Distance", "Distance_UNITS", "Shape", "Count"]
    assert algorithm.parameterDefinition("Distance").defaultValue() == 2 and algorithm.parameterDefinition("Distance_UNITS").defaultValue() == 1
    assert algorithm.parameterDefinition("Shape").options() == ["円", "四角"] and algorithm.parameterDefinition("Count").flags() & 8
    assert "点を広げる" in algorithm.shortHelpString()
    layer = points_layer()
    QgsProject.instance().addMapLayer(layer)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run({"Input_Points": layer.id(), "Distance": 500, "Distance_UNITS": 0, "Shape": 1, "Count": None}, context, feedback)
    assert ok
    sent = next(body for method, path, body, _ in server.asked if path.endswith("/execute"))
    assert sent["token"] == "secret" and sent["Shape"] == "四角" and "Count" not in sent
    assert json.loads(sent["Distance"]) == {"distance": 500.0, "units": "esriMeters"}
    points = json.loads(sent["Input Points"])
    assert points["geometryType"] == "esriGeometryPoint" and len(points["features"]) == 2 and points["features"][1]["attributes"]["name"] == "横浜"
    assert results["RESULTS"].splitlines() == ["Note: 完了しました", "処理 1 件"]
    [layer_id] = results["LAYERS"]
    out = context.getMapLayer(layer_id)
    assert out.name() == "バッファ - 結果" and out.featureCount() == 1 and context.layerToLoadOnCompletionDetails(layer_id).outputName == "LAYERS"


def test_an_ogc_job_with_geojson_in_and_out(server, provider):
    task = GpClient(gp_io.http).read_task("ogc", f"{server.root}/ogc/processes/count")
    algorithm = QgsApplication.processingRegistry().algorithmById(gp_algorithm.register(task))
    layer = points_layer()
    QgsProject.instance().addMapLayer(layer)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    results, ok = algorithm.run({"area": layer.id()}, context, QgsProcessingFeedback())
    assert ok and server.polls == 1
    method, _, payload, headers = next(a for a in server.asked if a[1].endswith("/execution"))
    assert headers["prefer"] == "respond-async" and payload["inputs"]["area"]["type"] == "FeatureCollection"
    assert "when" not in payload["inputs"] and payload["inputs"]["area"]["features"][0]["properties"] == {"name": "東京", "n": 1}
    assert results["RESULTS"].splitlines() == ["件数: 2", "数え終わり"]
    out = context.getMapLayer(results["LAYERS"][0])
    assert out.name() == "数える - 点" and out.featureCount() == 2 and out.crs().authid() == "EPSG:4326"


def test_a_server_error_fails_the_run(server, provider):
    # Apart from the runs above: QGIS keeps a failed run's exception, and with it the frame that called it, until Python exits.
    from satellite_observation.core.gp import GpTask

    task = GpTask("esri", f"{server.root}{TOOLS}/Broken", "Broken", "壊れた処理", [], [])
    algorithm = QgsApplication.processingRegistry().algorithmById(gp_algorithm.register(task))
    feedback = QgsProcessingFeedback()
    _, ok = algorithm.run({}, QgsProcessingContext(), feedback)
    assert not ok


def test_the_dialog_reads_a_service_and_opens_a_task(server, provider):
    from satellite_observation.ui.gp_dialog import RemoteProcessingDialog

    class Context:
        config, _ = parse_config_text(json.dumps({"gpServices": [{"label": "道具", "url": f"{server.root}{TOOLS}", "token": "t1"}]}))

    opened = []
    dialog = RemoteProcessingDialog(Context(), None, opened.append)
    dialog.prepare()
    assert dialog.url.currentText() == "道具" and dialog.chosen_url() == f"{server.root}{TOOLS}"
    dialog.load()
    assert wait_for(lambda: dialog.tasks.count() == 1, 10000)
    assert dialog.tasks.item(0).text() == "Buffer" and "1 件" in dialog.status.text()
    dialog.open_selected()
    assert wait_for(lambda: opened, 10000)
    assert opened == [f"satobs:{gp_algorithm.algorithm_name(gp_algorithm._tasks[opened[0].split(':')[1]][0])}"]
    task, token = gp_algorithm._tasks[opened[0].split(":")[1]]
    assert task.title == "バッファ" and token == "t1"
    assert QgsApplication.processingRegistry().algorithmById(opened[0]) is not None
    # A process URL typed: opened at once; a bad one: said.
    dialog.url.setEditText(f"{server.root}/ogc/processes/count")
    dialog.load()
    assert wait_for(lambda: len(opened) == 2, 10000)
    dialog.url.setEditText(f"{server.root}/ogc/processes/none")
    dialog.load()
    assert wait_for(lambda: "読み込み失敗" in dialog.status.text(), 10000)
    dialog.url.setEditText("ftp://x")
    dialog.load()
    assert dialog.status.text() == "http(s) の URL を入力"
    dialog.close()
