"""The viewer's gp.test.ts, for core/gp.py, and runs of the client against a scripted server."""
import json
from urllib.parse import parse_qs

import pytest

from satellite_observation.core.config import parse_config_text
from satellite_observation.core.gp import (
    FeatureInput, GpCancelled, GpClient, GpError, GpTask, check_values, classify_url, esri_field, esri_job_state, esri_messages, esri_output,
    esri_params, esri_srs, ogc_field, ogc_inputs, ogc_job_state, ogc_outputs, ogc_processes_url, parse_esri_service, parse_esri_task,
    parse_ogc_process, parse_ogc_process_list, safe_url, same_origin, service_root,
)


def p(**extra):
    return {"direction": "esriGPParameterDirectionInput", "parameterType": "esriGPParameterTypeRequired", **extra}


def test_only_http_urls_are_kept():
    assert safe_url("https://a.example/x") == "https://a.example/x"
    assert safe_url("results/out", "https://a.example/jobs/1/") == "https://a.example/jobs/1/results/out"
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("file:///etc/passwd") is None
    assert safe_url("data:text/html,x") is None
    assert safe_url(42) is None


def test_gpservers_tasks_and_ogc_endpoints_told_apart():
    assert classify_url("https://h/arcgis/rest/services/Tools/GPServer/") == {"kind": "esri-service", "root": "https://h/arcgis/rest/services/Tools/GPServer"}
    assert classify_url("https://h/arcgis/rest/services/Tools/GPServer/Buffer%20Points?f=json") == {
        "kind": "esri-task", "root": "https://h/arcgis/rest/services/Tools/GPServer", "task": "Buffer Points"}
    assert classify_url("https://h/ogc/processes") == {"kind": "ogc", "url": "https://h/ogc/processes"}
    assert service_root("https://h/ogc/processes/buffer") == "https://h/ogc"
    assert service_root("https://h/arcgis/rest/services/Tools/GPServer/Buffer") == "https://h/arcgis/rest/services/Tools/GPServer"


def test_tokens_go_to_the_same_origin_only():
    assert same_origin("https://h/a/jobs/1", "https://h/a")
    assert not same_origin("https://evil.example/a", "https://h/a")
    assert not same_origin("http://h/a", "https://h/a")


def test_every_esri_data_type():
    def f(**kw):
        return esri_field(p(**kw))

    assert (f(name="s", dataType="GPString", defaultValue="x").type, f(name="s", dataType="GPString").required) == ("string", True)
    c = f(name="c", dataType="GPString", choiceList=["a", "b"], defaultValue="b")
    assert (c.type, c.choices, c.default) == ("choice", ["a", "b"], "b")
    assert (f(name="l", dataType="GPLong", defaultValue=3).type, f(name="l", dataType="GPLong", defaultValue=3).default) == ("integer", 3)
    assert f(name="d", dataType="GPDouble").type == "number"
    assert (f(name="b", dataType="GPBoolean", defaultValue=True).type, f(name="b", dataType="GPBoolean", defaultValue=True).default) == ("boolean", True)
    assert f(name="t", dataType="GPDate", defaultValue=0).default == "1970-01-01T00:00:00Z"
    u = f(name="u", dataType="GPLinearUnit", defaultValue={"distance": 5, "units": "esriKilometers"})
    assert (u.type, u.default, u.units) == ("linear-unit", 5, "esriKilometers")
    feats = f(name="f", dataType="GPFeatureRecordSetLayer", defaultValue={"geometryType": "esriGeometryPolygon", "fields": [{"name": "A", "type": "esriFieldTypeString"}]})
    assert (feats.type, feats.geometry, feats.fields) == ("features", "Polygon", [{"name": "A", "type": "esriFieldTypeString"}])
    assert f(name="r", dataType="GPRecordSet").type == "table"
    img = f(name="img", dataType="GPRasterDataLayer", defaultValue={"url": "javascript:x"})
    assert (img.type, img.default) == ("raster", None)
    assert f(name="file", dataType="GPDataFile").type == "file"
    m = f(name="m", dataType="GPMultiValue:GPString", defaultValue=["a", "b"])
    assert (m.type, m.default) == ("multi", "a, b")
    assert not f(name="o", dataType="GPString", parameterType="esriGPParameterTypeOptional").required


def test_outputs_and_derived_parameters_skipped():
    assert esri_field({"name": "out", "dataType": "GPFeatureRecordSetLayer", "direction": "esriGPParameterDirectionOutput"}) is None
    assert esri_field(p(name="x", dataType="GPString", parameterType="esriGPParameterTypeDerived")) is None
    assert esri_field("nonsense") is None


def test_a_task_and_its_service():
    task = parse_esri_task("https://h/s/Folder/Tools/GPServer/Viewshed", {
        "name": "Viewshed", "displayName": "可視領域", "executionType": "esriExecutionTypeAsynchronous",
        "parameters": [p(name="Point", dataType="GPFeatureRecordSetLayer"),
                       {"name": "Out", "dataType": "GPFeatureRecordSetLayer", "displayName": "結果", "direction": "esriGPParameterDirectionOutput"}],
    }, {"tasks": ["Viewshed"], "resultMapServerName": "Tools"})
    assert (task.kind, task.id, task.title, task.run_async, task.result_map_server) == ("esri", "Viewshed", "可視領域", True, "Tools")
    assert [(o.name, o.label) for o in task.outputs] == [("Out", "結果")] and len(task.inputs) == 1
    assert parse_esri_service("https://h/s/Tools/GPServer", {"tasks": ["Buffer", "A B"]}).tasks == [
        ("Buffer", "Buffer", "https://h/s/Tools/GPServer/Buffer"), ("A B", "A B", "https://h/s/Tools/GPServer/A%20B")]
    with pytest.raises(GpError):
        parse_esri_service("https://h", {})
    with pytest.raises(GpError):
        parse_esri_task("https://h", {"name": "x"})


def test_ogc_schemas():
    s = ogc_field("s", {"title": "名前", "schema": {"type": "string"}})
    assert (s.type, s.label, s.required) == ("string", "名前", True)
    e = ogc_field("e", {"schema": {"type": "string", "enum": ["a", "b"], "default": "a"}, "minOccurs": 0})
    assert (e.type, e.choices, e.default, e.required) == ("choice", ["a", "b"], "a", False)
    n = ogc_field("n", {"schema": {"type": "number", "minimum": 0, "maximum": 10}})
    assert (n.type, n.minimum, n.maximum) == ("number", 0, 10)
    i = ogc_field("i", {"schema": {"type": "integer", "default": 2}})
    assert (i.type, i.default, i.required) == ("integer", 2, False)
    assert ogc_field("b", {"schema": {"type": "boolean"}}).type == "boolean"
    assert ogc_field("d", {"schema": {"type": "string", "format": "date-time"}}).type == "date"
    g = ogc_field("g", {"schema": {"$ref": "http://schemas.opengis.net/ogcapi/features/part1/1.0/openapi/schemas/geometryGeoJSON.yaml"}})
    assert (g.type, g.shape) == ("features", "geometry")
    assert ogc_field("fc", {"schema": {"format": "geojson-feature-collection"}}).shape == "collection"
    assert ogc_field("f", {"schema": {"oneOf": [{"type": "string"}, {"$ref": "featureGeoJSON.yaml"}]}}).shape == "feature"
    assert ogc_field("x", {"schema": {"type": "object"}}).type == "json"


def test_a_process_list_a_process_and_a_landing_page():
    listed = parse_ogc_process_list("https://h/ogc/processes", {"processes": [{"id": "buffer", "title": "Buffer"}, {"title": "no id"}]})
    assert listed.tasks == [("buffer", "Buffer", "https://h/ogc/processes/buffer")]
    assert ogc_processes_url("https://h/ogc/", {"links": [{"rel": "http://www.opengis.net/def/rel/ogc/1.0/processes", "href": "processes"}]}) == "https://h/ogc/processes"
    assert ogc_processes_url("https://h/ogc/", {"links": [{"rel": "processes", "href": "javascript:x"}]}) is None
    process = parse_ogc_process("https://h/ogc/processes/buffer", {
        "id": "buffer", "jobControlOptions": ["sync-execute", "async-execute"], "inputs": {"distance": {"schema": {"type": "number"}}},
        "outputs": {"result": {"title": "結果"}}})
    assert (process.kind, process.run_async, [(o.name, o.label) for o in process.outputs]) == ("ogc", True, [("result", "結果")])


SQUARE_ESRI = {"rings": [[[15473409.2, 4163881.1], [15584728.7, 4163881.1], [15584728.7, 4300621.4], [15473409.2, 4163881.1]]]}
SQUARE_GEOJSON = {"type": "Polygon", "coordinates": [[[139, 35], [140, 35], [140, 36], [139, 35]]]}


def square():
    return FeatureInput(esri=[{"geometry": SQUARE_ESRI, "attributes": {"name": "a", "n": 1}}],
                        geojson=[{"type": "Feature", "geometry": SQUARE_GEOJSON, "properties": {"name": "a", "n": 1}}],
                        fields=[{"name": "name", "type": "esriFieldTypeString", "alias": "name"}], geometry_type="esriGeometryPolygon")


ESRI_TASK = GpTask("esri", "https://h/GPServer/T", "T", "T", [
    esri_field(p(name="In", dataType="GPFeatureRecordSetLayer")),
    esri_field(p(name="Dist", dataType="GPLinearUnit")),
    esri_field(p(name="Flag", dataType="GPBoolean")),
    esri_field(p(name="When", dataType="GPDate", parameterType="esriGPParameterTypeOptional")),
    esri_field(p(name="Img", dataType="GPRasterDataLayer", parameterType="esriGPParameterTypeOptional")),
    esri_field(p(name="Tags", dataType="GPMultiValue:GPString", parameterType="esriGPParameterTypeOptional")),
], [])


def test_esri_parameters_written():
    params = esri_params(ESRI_TASK, {"In": square(), "Dist": {"distance": 100, "units": "esriMeters"}, "Flag": False,
                                     "When": "2024-01-01T00:00:00.000Z", "Img": "https://h/img.tif", "Tags": "a, b"})
    assert params["env:outSR"] == "102100"
    feature_set = json.loads(params["In"])
    assert (feature_set["geometryType"], feature_set["spatialReference"]["wkid"]) == ("esriGeometryPolygon", 102100)
    assert feature_set["fields"] == [{"name": "name", "type": "esriFieldTypeString", "alias": "name"}]
    # Only listed attributes are sent.
    assert feature_set["features"][0]["attributes"] == {"name": "a"} and len(feature_set["features"][0]["geometry"]["rings"][0]) == 4
    assert json.loads(params["Dist"]) == {"distance": 100, "units": "esriMeters"}
    assert params["Flag"] == "false" and params["When"] == "1704067200000"
    assert json.loads(params["Img"]) == {"url": "https://h/img.tif"} and json.loads(params["Tags"]) == ["a", "b"]


def test_missing_required_values_and_bad_urls_refused():
    with pytest.raises(GpError, match="「In」"):
        esri_params(ESRI_TASK, {"In": None, "Dist": None, "Flag": False})
    assert check_values(ESRI_TASK, {"In": square(), "Dist": {"distance": 1, "units": "esriMeters"}, "Flag": True, "Img": "javascript:alert(1)"}) == [
        "「Img」は http(s) の URL で指定"]


def test_ogc_inputs_written():
    task = GpTask("ogc", "https://h/processes/p", "p", "p", [
        ogc_field("geom", {"schema": {"format": "geojson-geometry"}}), ogc_field("fc", {"schema": {"format": "geojson-feature-collection"}}),
        ogc_field("mode", {"schema": {"type": "integer", "enum": [1, 2]}}), ogc_field("href", {"schema": {"type": "string", "format": "uri"}, "minOccurs": 0}),
    ], [])
    inputs = ogc_inputs(task, {"geom": square(), "fc": square(), "mode": "2", "href": "https://h/a.tif"})
    assert inputs["geom"] == SQUARE_GEOJSON and inputs["fc"]["type"] == "FeatureCollection"
    assert inputs["mode"] == 2 and inputs["href"] == {"href": "https://h/a.tif"}


def test_esri_feature_sets_read_in_their_spatial_reference():
    out = esri_output({"paramName": "Out", "dataType": "GPFeatureRecordSetLayer", "value": {
        "geometryType": "esriGeometryPoint", "spatialReference": {"wkid": 4326},
        "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "NAME", "type": "esriFieldTypeString", "alias": "名前"}],
        "features": [{"geometry": {"x": 139.7, "y": 35.6}, "attributes": {"OBJECTID": 1, "NAME": "東京"}}]}}, "結果")
    assert (out.kind, out.label, out.srs, out.format, out.geometry_type) == ("features", "結果", "EPSG:4326", "esri", "esriGeometryPoint")
    assert [f["name"] for f in out.fields] == ["OBJECTID", "NAME"] and out.features[0]["attributes"]["NAME"] == "東京"


def test_the_features_read_are_capped():
    features = [{"geometry": {"x": i, "y": 0}, "attributes": {}} for i in range(50_005)]
    out = esri_output({"paramName": "Out", "dataType": "GPFeatureRecordSetLayer", "value": {"features": features}})
    assert len(out.features) == 50_000 and out.truncated


def test_rasters_files_and_values():
    r = esri_output({"paramName": "R", "dataType": "GPRasterDataLayer", "value": {"url": "https://h/out.tif"}})
    assert (r.kind, r.url) == ("image", "https://h/out.tif")
    assert esri_output({"paramName": "F", "dataType": "GPDataFile", "value": {"url": "https://h/out.zip"}}).kind == "link"
    assert esri_output({"paramName": "F", "dataType": "GPDataFile", "value": {"url": "javascript:alert(1)"}}).kind == "value"
    assert esri_output({"paramName": "N", "dataType": "GPDouble", "value": 1.5}).text == "1.5"
    assert esri_output({"paramName": "U", "dataType": "GPLinearUnit", "value": {"distance": 3, "units": "esriKilometers"}}).text == "3 km"
    assert len(esri_output({"paramName": "S", "dataType": "GPString", "value": "x" * 5000}).text) < 2100
    assert esri_output("x") is None


def test_spatial_references():
    assert esri_srs({"wkid": 102100, "latestWkid": 3857}) == "EPSG:3857"
    assert esri_srs({"wkid": 6677}) == "EPSG:6677"
    assert esri_srs({"wkt": 'PROJCS["x"]'}) == 'PROJCS["x"]'
    assert esri_srs(None) is None


def test_ogc_outputs():
    task = GpTask("ogc", "u", "p", "p", [], [type("O", (), {"name": "buffered", "label": "バッファ"})()])
    [features] = ogc_outputs(task, {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [139, 35]},
                                                                               "properties": {"a": 1, "b": "x", "c": 1.5}}]})
    assert (features.kind, features.name, features.label, features.fields) == ("features", "buffered", "バッファ", [("a", "integer"), ("b", "string"), ("c", "double")])
    outputs = ogc_outputs(task, {"area": {"value": 12, "mediaType": "application/json"}, "image": {"href": "https://h/r.tif", "type": "image/tiff; application=geotiff"},
                                 "doc": {"href": "https://h/r.pdf"}, "bad": {"href": "javascript:alert(1)"}, "links": []}, "https://h/jobs/1/results")
    assert [(o.name, o.kind) for o in outputs] == [("area", "value"), ("image", "image"), ("doc", "link"), ("bad", "value")]
    [one] = ogc_outputs(task, {"outputs": [{"id": "buffered", "value": 3}]})
    assert (one.kind, one.text, one.label) == ("value", "3", "バッファ")


def test_job_states_and_messages():
    assert [esri_job_state(s) for s in ("esriJobSucceeded", "esriJobTimedOut", "esriJobExecuting", "esriJobSubmitted")] == ["succeeded", "failed", "running", "waiting"]
    assert [ogc_job_state(s) for s in ("successful", "dismissed", "accepted")] == ["succeeded", "cancelled", "waiting"]
    assert esri_messages([{"type": "esriJobMessageTypeError", "description": "bad"}, {"type": "esriJobMessageTypeInformative", "description": "ok"}, "junk"]) == ["エラー: bad", "ok"]


def test_gp_services_in_the_settings_file():
    config, problems = parse_config_text(json.dumps({"gpServices": ["https://h/GPServer", {"label": "解析", "url": "https://h/ogc", "token": "t"}, {"url": "ftp://x"}]}))
    assert [(s.label, s.url, s.token) for s in config.gp_services] == [("https://h/GPServer", "https://h/GPServer", ""), ("解析", "https://h/ogc", "t")]
    assert problems == ["gpServices[2] の url は http(s) の URL で指定"]


class Server:
    """A scripted server: (method, url) → (status, headers, json); records what was asked."""

    def __init__(self, routes):
        self.routes = routes
        self.asked = []

    def __call__(self, method, url, body, headers):
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()} if body and headers.get("content-type", "").startswith("application/x-www") else (json.loads(body) if body else None)
        self.asked.append((method, url, form, headers))
        answer = self.routes[(method, url.split("?")[0])]
        status, extra, value = answer(form) if callable(answer) else answer
        return status, extra, json.dumps(value).encode() if value is not None else b""


def test_an_esri_job_followed_to_its_results_with_the_token_only_at_home():
    task_url = "https://h/arcgis/rest/services/Tools/GPServer/Buffer"
    polls = iter([{"jobStatus": "esriJobExecuting", "progress": {"percent": 50, "message": "半分"}},
                  {"jobStatus": "esriJobSucceeded", "results": {"Out": {"paramUrl": "results/Out"}, "Evil": {"paramUrl": "https://evil.example/x"}},
                   "messages": [{"type": "esriJobMessageTypeInformative", "description": "完了"}]}])
    server = Server({
        ("POST", task_url): (200, {}, {"name": "Buffer", "executionType": "esriExecutionTypeAsynchronous",
                                       "parameters": [p(name="Distance", dataType="GPDouble"),
                                                      {"name": "Out", "displayName": "結果", "dataType": "GPFeatureRecordSetLayer", "direction": "esriGPParameterDirectionOutput"}]}),
        ("POST", "https://h/arcgis/rest/services/Tools/GPServer"): (200, {}, {"tasks": ["Buffer"], "resultMapServerName": "Tools"}),
        ("POST", f"{task_url}/submitJob"): (200, {}, {"jobId": "j1", "jobStatus": "esriJobSubmitted"}),
        ("POST", f"{task_url}/jobs/j1"): lambda form: (200, {}, next(polls)),
        ("POST", f"{task_url}/jobs/j1/results/Out"): (200, {}, {"paramName": "Out", "dataType": "GPFeatureRecordSetLayer",
                                                               "value": {"geometryType": "esriGeometryPoint", "spatialReference": {"wkid": 102100}, "features": [{"geometry": {"x": 1, "y": 2}, "attributes": {}}]}}),
    })
    client = GpClient(server, token="secret", sleep=lambda s: None, poll_seconds=0)
    task = client.read(task_url)
    assert task.title == "Buffer" and task.result_map_server == "Tools"
    seen = []
    result = client.run(task, {"Distance": 5}, lambda text, percent: seen.append((text, percent)))
    assert ("ジョブ 実行中: 半分", 50) in seen
    assert [(o.label, o.kind, len(o.features)) for o in result.outputs] == [("結果", "features", 1)]
    assert result.messages == ["完了"] and result.map_server == "https://h/arcgis/rest/services/Tools/MapServer/jobs/j1"
    assert all(form.get("token") == "secret" for _, url, form, _ in server.asked if url.startswith("https://h/"))
    assert not any("evil" in url for _, url, _, _ in server.asked)
    submitted = next(form for _, url, form, _ in server.asked if url.endswith("/submitJob"))
    assert (submitted["Distance"], submitted["env:outSR"]) == ("5", "102100")


def test_an_esri_job_cancelled():
    task_url = "https://h/GPServer/Slow"
    task = GpTask("esri", task_url, "Slow", "Slow", [], [], run_async=True)
    server = Server({("POST", f"{task_url}/submitJob"): (200, {}, {"jobId": "j2", "jobStatus": "esriJobSubmitted"}),
                     ("POST", f"{task_url}/jobs/j2/cancel"): (200, {}, {"jobStatus": "esriJobCancelling"})})
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(GpCancelled):
        GpClient(server, sleep=lambda s: None, cancelled=cancelled, poll_seconds=1).run(task, {})
    assert server.asked[-1][1].endswith("/jobs/j2/cancel")


def test_an_ogc_job_with_a_location_and_a_bearer_token():
    process = "https://h/ogc/processes/buffer"
    polls = iter([{"jobID": "a", "status": "running", "progress": 10}, {"jobID": "a", "status": "successful", "links": [{"rel": "results", "href": "https://h/ogc/jobs/a/results"}]}])
    server = Server({
        ("GET", "https://h/ogc"): (200, {}, {"links": [{"rel": "processes", "href": "https://h/ogc/processes"}]}),
        ("GET", "https://h/ogc/processes"): (200, {}, {"processes": [{"id": "buffer", "title": "バッファ"}]}),
        ("GET", process): (200, {}, {"id": "buffer", "jobControlOptions": ["async-execute"], "inputs": {"d": {"schema": {"type": "number"}}},
                                     "outputs": {"result": {"title": "結果"}}}),
        ("POST", f"{process}/execution"): (201, {"location": "https://h/ogc/jobs/a"}, {"jobID": "a", "status": "accepted"}),
        ("GET", "https://h/ogc/jobs/a"): lambda body: (200, {}, next(polls)),
        ("GET", "https://h/ogc/jobs/a/results"): (200, {}, {"result": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [139, 35]}, "properties": {"n": 1}}]}}),
    })
    client = GpClient(server, token="tok", sleep=lambda s: None, poll_seconds=0)
    service = client.read("https://h/ogc")
    assert service.tasks == [("buffer", "バッファ", process)]
    task = client.read_task("ogc", process)
    result = client.run(task, {"d": 3})
    assert [(o.label, o.kind, o.format) for o in result.outputs] == [("結果", "features", "geojson")]
    execution = next(a for a in server.asked if a[1].endswith("/execution"))
    assert execution[2] == {"inputs": {"d": 3}} and execution[3]["prefer"] == "respond-async"
    assert all(h.get("authorization") == "Bearer tok" for _, _, _, h in server.asked)


def test_server_errors_in_the_persons_words():
    server = Server({("POST", "https://h/GPServer"): (200, {}, {"error": {"code": 499, "message": "Token Required"}}),
                     ("GET", "https://h/ogc/processes/x"): (500, {}, {"title": "壊れた", "detail": "原因"})})
    with pytest.raises(GpError, match="このサービスには認証が必要"):
        GpClient(server).read("https://h/GPServer")
    with pytest.raises(GpError, match=r"HTTP 500.*壊れた: 原因"):
        GpClient(server).read_task("ogc", "https://h/ogc/processes/x")
