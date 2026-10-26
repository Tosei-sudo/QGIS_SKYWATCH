"""The Esri client against the fake server: paging, ids, edits, tokens."""
import pytest

from satellite_observation.core.esri import EsriClient, EsriError, InvalidTokenError, fit_value, point_geometry

from .fake_esri import FakeEsri, FakeLayer

URL = "https://gis.example/arcgis/rest/services/Obs/FeatureServer/0"


def server(n=0, **kw):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({"OBS_ID": "esriFieldTypeString", "T": "esriFieldTypeDate"}, **kw))
    for i in range(n):
        layer.add({"OBS_ID": f"o{i}", "T": 1000 * i}, point_geometry(139 + i / 1000, 35))
    return fake, layer


def test_reads_every_feature_page_by_page():
    fake, _ = server(2500, max_record_count=1000)
    features = EsriClient(fake).query_all(URL)
    assert [f["attributes"]["OBS_ID"] for f in features] == [f"o{i}" for i in range(2500)]
    pages = [form for url, form in fake.asked if url.endswith("/query")]
    assert [p["resultOffset"] for p in pages] == ["0", "1000", "2000"]
    assert all(p["outSR"] == "4326" and p["f"] == "json" for p in pages)


def test_reads_by_ids_when_the_layer_does_not_page():
    fake, _ = server(2500, max_record_count=1000, pagination=False)
    features = EsriClient(fake).query_all(URL, where="1=1")
    assert len(features) == 2500
    pages = [form for url, form in fake.asked if url.endswith("/query")]
    assert pages[0]["returnIdsOnly"] == "true"
    assert len(pages) == 4


def test_where_filters():
    fake, _ = server(10)
    assert [f["attributes"]["OBS_ID"] for f in EsriClient(fake).query_all(URL, where="OBS_ID = 'o3'")] == ["o3"]


def test_adds_updates_and_deletes():
    fake, layer = server()
    client = EsriClient(fake)
    result = client.apply_edits(URL, adds=[{"attributes": {"OBS_ID": "a"}, "geometry": point_geometry(1, 2)}, {"attributes": {"OBS_ID": "b"}}])
    ids = [r["objectId"] for r in result.add_results]
    assert ids == [1, 2]
    result = client.apply_edits(URL, updates=[{"attributes": {"OBJECTID": 1, "OBS_ID": "a2"}}, {"attributes": {"OBJECTID": 99}}], deletes=[2])
    assert result.failures() == ["見つからない地物"]
    assert [r["attributes"]["OBS_ID"] for r in layer.rows] == ["a2"]
    assert client.delete_features(URL, [1]) == [{"objectId": 1, "success": True}]
    assert layer.rows == []


def test_tokens_are_sent_renewed_once_and_never_in_messages():
    fake, layer = server(1, token="good")
    with pytest.raises(EsriError, match="このサービスにはトークンが必要"):
        EsriClient(fake).query_all(URL)
    with pytest.raises(InvalidTokenError) as caught:
        EsriClient(fake).ask(URL, {}, token="secret-old")
    assert "secret-old" not in str(caught.value)
    renewed = []
    client = EsriClient(fake, renew=lambda url: renewed.append(url) or "good")
    assert client.ask(URL, {}, token="old")["objectIdField"] == "OBJECTID"
    assert renewed == [URL]
    assert fake.asked[-1][1]["token"] == "good"


def test_errors_and_answers_that_are_not_json():
    fake, _ = server()
    client = EsriClient(fake)
    fake.fail_next = {"error": {"code": 400, "message": "Invalid query", "details": ["'X' is not a field"]}}
    with pytest.raises(EsriError, match="サービスからエラー応答: Invalid query 'X' is not a field"):
        client.ask(URL)
    with pytest.raises(EsriError, match="応答が JSON 以外"):
        EsriClient(lambda url, form: b"<html>").ask(URL)
    with pytest.raises(EsriError, match="接続不可"):
        client.ask("https://elsewhere/FeatureServer/0")


def test_layer_information():
    fake, _ = server()
    info = EsriClient(fake).layer(URL)
    assert info.object_id_field == "OBJECTID"
    assert info.field_types["T"] == "esriFieldTypeDate"
    assert info.supports_pagination
    assert info.has("OBS_ID") and not info.has("NOPE")


def test_values_fit_their_fields():
    assert fit_value(True, "esriFieldTypeSmallInteger") == 1
    assert fit_value(False, "esriFieldTypeString") == "なし"
    assert fit_value(1759281825000, "esriFieldTypeString") == "2025-10-01T01:23:45.000Z"
    assert fit_value(1759281825000, "esriFieldTypeDate") == 1759281825000
    assert fit_value(None, "esriFieldTypeString") is None
