"""
External AI results without QGIS (core/ai_results.py): the settings, the
query of one image, a feature read as a result, what it is recorded as, and
several recorded at once (ObservationBook.add_many).
"""
from satellite_observation.core.ai_results import (
    ai_result_source_of, image_where, import_entry, kind_for, recorded_tags, result_of, result_tag,
)
from satellite_observation.core.config import parse_config
from satellite_observation.tests.test_obs_book import book  # noqa: F401 (the fixture)

URL = "https://example.com/arcgis/rest/services/AI/FeatureServer/0"


def source(**more):
    problems = []
    s = ai_result_source_of({"label": "外部AI", "url": URL, "fields": {"imageId": "IMAGE_ID", "class": "CLS", "score": "SCORE", "id": "DET_ID"},
                             "kinds": {"ship": "船舶 > 支援船"}, **more}, problems, 0)
    return s, problems


def test_the_settings_need_a_layer_and_the_image_id():
    s, problems = source(where="STATUS <> 'NG'", minScore=0.3, confidence="推定")
    assert problems == [] and s.url == URL and s.fields["imageId"] == "IMAGE_ID" and s.min_score == 0.3 and s.confidence == "推定"
    config, problems = parse_config({"aiResultSources": [
        {"url": URL, "fields": {"class": "CLS"}},
        {"url": "https://example.com/arcgis/rest/services/AI/FeatureServer", "fields": {"imageId": "IMAGE_ID"}},
        {"url": URL, "fields": {"imageId": "IMAGE_ID", "colour": "C", "score": "a b"}, "minScore": 3},
    ]})
    assert [s.label for s in config.ai_result_sources] == ["AI結果 3"]
    assert problems == [
        "aiResultSources[0] に fields.imageId（画像IDの入った属性名）なし",
        "aiResultSources[1] の url は …/FeatureServer/0 のようなレイヤーの URL で指定",
        "aiResultSources[2] の fields.colour は使用不可の役割（imageId / id / class / score / model / detectedAt / name / kind / activity / length / heading / note）",
        "aiResultSources[2] の fields.score は属性名（英数字と _）で指定",
        "aiResultSources[2] の minScore は 0〜1 で指定",
    ]


def test_the_query_of_an_image():
    s, _ = source()
    assert image_where(s, "A'1") == "IMAGE_ID = 'A''1'"
    assert image_where(s, "42", "esriFieldTypeInteger") == "IMAGE_ID = 42"
    assert image_where(s, "x42", "esriFieldTypeInteger") == "IMAGE_ID = 'x42'"
    s, _ = source(where="STATUS = 'OK'")
    assert image_where(s, "A") == "IMAGE_ID = 'A' AND (STATUS = 'OK')"


def test_a_feature_as_a_result_and_what_it_is_recorded_as():
    s, _ = source()
    r = result_of(s, {"attributes": {"OBJECTID": 7, "image_id": "A", "CLS": "Ship", "SCORE": 87}, "geometry": {"x": 139.7, "y": 35.6}}, "OBJECTID")
    # Attribute names in any case; a percentage as a share; its own id missing: the object id.
    assert (r.image_id, r.cls, r.score, r.key) == ("A", "Ship", 0.87, "7")
    assert kind_for(r, s) == "船舶 > 支援船"
    e = import_entry(r, s, 139.7, 35.6, 120.5)
    assert (e.kind, e.length, e.object) == ("船舶 > 支援船", 120.5, "")
    assert e.note == "外部AI 外部AI: Ship 0.87 [AI:外部AI#7]"
    assert recorded_tags(["前の記録", e.note, "x [AI:別#1] y"]) == {"[AI:外部AI#7]", "[AI:別#1]"}
    assert result_tag("a]#b", "1") == "[AI:ab#1]"


def test_several_results_recorded_at_once_are_named_in_turn(book):  # noqa: F811
    s, _ = source()
    results = [result_of(s, {"attributes": {"OBJECTID": i, "IMAGE_ID": "d1", "CLS": "ship", "SCORE": 0.9}}, "OBJECTID") for i in (1, 2)]
    results.append(result_of(s, {"attributes": {"OBJECTID": 3, "IMAGE_ID": "d1", "CLS": "car"}}, "OBJECTID"))
    saved = book.add_many([import_entry(r, s, 139.7, 35.6) for r in results], image="d1")
    assert [(o.object_name, o.kind, o.image_id) for o in saved] == [("支援船-1", "船舶 > 支援船", "d1"), ("支援船-2", "船舶 > 支援船", "d1"), ("car-1", "car", "d1")]
    assert len({o.object_id for o in saved}) == 3
    assert book.said[-1] == "3 件を記録"
    assert book.add_many([], image="nope") is None and book.said[-1] == "画像なし（取り込む画像を開いて）"
