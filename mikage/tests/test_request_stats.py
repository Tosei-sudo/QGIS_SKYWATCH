"""通信の記録 (core/request_stats.py): the plugin's requests counted by layer and kind, and the log's line."""
from mikage.core import request_stats
from mikage.core.esri import EsriClient, LayerInfos
from mikage.core.request_stats import RequestStats, describe, kind_of, layer_of
from mikage.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://gis.example/arcgis/rest/services/Obs/FeatureServer/2"


def test_layer_and_kind_of_a_request():
    assert layer_of(URL + "/query") == URL
    assert layer_of(URL + "/applyEdits?token=x") == URL
    assert layer_of(URL) == URL
    assert layer_of("https://portal.example/sharing/rest/community/self") == "https://portal.example/sharing/rest/community/self"
    assert kind_of(URL, {}) == "情報"
    assert kind_of(URL + "/query", {"where": "1=1"}) == "検索"
    assert kind_of(URL + "/query", {"returnCountOnly": "true"}) == "件数"
    assert kind_of(URL + "/query", {"returnIdsOnly": "true"}) == "ID"
    assert kind_of(URL + "/applyEdits", {}) == "編集"
    assert kind_of("https://portal.example/sharing/rest/generateToken", {}) == "その他"


def test_noted_by_layer_and_kind_and_taken_once():
    t = [100.0]
    stats = RequestStats(clock=lambda: t[0])
    stats.note(URL + "/query", {"where": "1=1"}, 1.5, 2_000_000)
    stats.note(URL + "/query", {"where": "IMAGE_ID = 'A'"}, 0.5, 1000, ok=False)
    stats.note(URL, {}, 0.1, 500, name="観測記録")
    stats.note("https://gis.example/arcgis/rest/services/Other/FeatureServer/0/query", {"where": "A = 1"}, 0.1, 10)
    t[0] = 700.0
    summary = stats.take()
    assert summary.count == 4 and summary.failed == 1 and round(summary.total, 1) == 2.2
    rows = {(r.name, r.kind): r for r in summary.rows}
    # A name told by any of a layer's requests names all its rows; else the service and number.
    search = rows[("観測記録", "検索")]
    assert search.count == 2 and search.whole == 1 and search.longest == 1.5 and search.size == 2_001_000
    assert rows[("観測記録", "情報")].count == 1 and rows[("Other/0", "検索")].count == 1
    line = describe(summary)
    assert line.startswith("通信（10 分）: 4 回・2.0 MB・合計 2.2 秒・失敗 1 回。時間のかかった表: 観測記録 検索 2 回（全件 1）計 2.0 秒"), line
    assert stats.take().count == 0


def test_the_client_notes_each_request_by_the_layers_name():
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({"IMAGE_ID": "esriFieldTypeString"}))
    layer.name = "観測記録"
    layer.add({"IMAGE_ID": "A"})
    request_stats.STATS.take()
    client = EsriClient(fake, layers=LayerInfos())
    client.query_all(URL, geometry=False)
    rows = {r.kind: r for r in request_stats.STATS.take().rows}
    assert rows["情報"].count == 1 and rows["検索"].count >= 1
    # The layer's information was read first: its name names the search.
    assert rows["検索"].name == "観測記録" and rows["情報"].name == "観測記録"
