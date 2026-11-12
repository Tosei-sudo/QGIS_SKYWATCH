"""
External AI results without QGIS (core/ai_results.py): the settings, the
query of one image, a feature read as a result, what it is recorded as, and
several recorded at once (ObservationBook.add_many).
"""
from mikage.core.ai_results import (
    ai_result_source_of, image_where, import_entry, kind_for, recorded_tags, result_of, result_tag,
)
from mikage.core.config import parse_config
from mikage.core.obs.book import FormValues
from mikage.tests.test_obs_book import book  # noqa: F401 (the fixture)

URL = "https://example.com/arcgis/rest/services/AI/FeatureServer/0"


def _carried(entry, record):
    entry.carry = record
    return entry


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
        "aiResultSources[2] の fields.colour は使用不可の役割（imageId / id / class / score / model / detectedAt / name / kind / activity / length / heading / width / note）",
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


def test_a_result_near_a_thing_of_the_image_before_continues_it(book):  # noqa: F811
    s, problems = source(continueRadius=80, candidateRadius=500)
    assert problems == [] and (s.continue_radius, s.candidate_radius) == (80, 500)
    a = book.submit(139.7, 35.6, FormValues(object="あおば", kind="船舶 > 支援船", activity="停泊", length="100"))
    book.step(1)
    r = result_of(s, {"attributes": {"OBJECTID": 1, "IMAGE_ID": "d3", "CLS": "ship"}}, "OBJECTID")
    e = import_entry(r, s, 139.7003, 35.6, None)
    e.carry = book.record(a.id)
    # Twice: the second is the same thing again, so a new one.
    saved = book.add_many([e, _carried(import_entry(r, s, 139.7004, 35.6), book.record(a.id))], image="d3")
    first, second = saved
    assert (first.object_id, first.object_name, first.kind, first.activity, first.length) == (a.object_id, "あおば", "船舶 > 支援船", "停泊", 100)
    assert second.object_id != a.object_id
    assert book.said[-1] == "2 件を記録（継続 1 件）"
    _, problems = source(continueRadius=-1)
    assert problems == ["aiResultSources[0] の continueRadius は 0〜100000（m）で指定"]


def test_the_kind_of_a_class_found_by_the_plugins_own_detection():
    from mikage.core.ai.model_config import ai_model_of
    from mikage.core.ai_results import detected_kind
    from mikage.core.obs.taxonomy import Taxon

    taxonomy = [Taxon(path=["船舶"]), Taxon(path=["船舶", "支援船"], specs={"別名": "tender, 補給艦"})]
    kinds = {"Boat": "船舶 > 支援船"}
    assert detected_kind("boat", kinds, taxonomy) == "船舶 > 支援船"
    # A model trained here: its classes are the classification's kinds.
    assert detected_kind("船舶 > 支援船", {}, taxonomy) == "船舶 > 支援船"
    # Another name of a class.
    assert detected_kind("補給艦", {}, taxonomy) == "船舶 > 支援船"
    assert detected_kind("car", {}, taxonomy) == "car" and detected_kind("", kinds, taxonomy) == ""

    problems = []
    model = ai_model_of({"url": "./a.onnx", "kinds": {"boat": "船舶", "x": 3}}, problems, 0)
    assert model.kinds == {"boat": "船舶"} and problems == ["aiModels[0] の kinds.x は種別（「船舶 > 支援船」など）で指定"]


def test_the_detection_tables_are_read_back_unless_left_out():
    """detectionOutputs (「検出結果を DB へ転記」) read by 「AI結果」 too, its roles as the result's (tsudo, 2026-11-09)."""
    from mikage.core.ai_results import result_sources

    tables = "https://example.com/arcgis/rest/services/Detections/FeatureServer/"
    config, problems = parse_config({
        "aiResultSources": [{"label": "外部AI", "url": URL, "fields": {"imageId": "IMAGE_ID"}}],
        "detectionOutputs": [
            {"label": "検出DB", "url": tables + "0", "fields": {"imageId": "IMAGE_ID", "class": "CLASS", "score": "CONFIDENCE",
                                                                "model": "MODEL", "detectedAt": "DETECTED_AT", "status": "STATUS",
                                                                "angle": "ANGLE", "length": "LONG_M"}},
            {"label": "外部AI", "url": tables + "1", "fields": {"imageId": "IMG"}},
            {"label": "画像IDなし", "url": tables + "2", "fields": {"class": "CLASS"}},
            {"label": "読まない", "url": tables + "3", "fields": {"imageId": "IMAGE_ID"}, "aiResults": False},
            {"label": "同じ表", "url": URL, "fields": {"imageId": "IMAGE_ID"}},
            {"label": "書き方違い", "url": tables + "4", "fields": {"imageId": "IMAGE_ID"}, "aiResults": "no"},
        ]})
    assert problems == ["detectionOutputs[5] の aiResults は true / false で指定"]
    sources = result_sources(config)
    assert [(s.label, s.detections) for s in sources] == [("外部AI", False), ("検出DB", True), ("外部AI（転記先）", True), ("書き方違い", True)]
    db = sources[1]
    assert db.fields == {"imageId": "IMAGE_ID", "class": "CLASS", "score": "CONFIDENCE", "model": "MODEL", "detectedAt": "DETECTED_AT",
                         "length": "LONG_M"}
    r = result_of(db, {"attributes": {"OBJECTID": 7, "IMAGE_ID": "IMG1", "CLASS": "boat", "CONFIDENCE": 0.8, "MODEL": "港湾モデル",
                                      "DETECTED_AT": 1794013200000}, "geometry": {"x": 139.7, "y": 35.6}}, "OBJECTID")
    assert (r.key, r.tag, r.detected_at) == ("7", "[AI:検出DB#7]", 1794013200000)
    note = import_entry(r, db, 139.7, 35.6, also=["[AI:検出@1#2]"]).note
    assert note == "AI検出（検出DB）: boat 0.80 港湾モデル [AI:検出DB#7] [AI:検出@1#2]"
    assert recorded_tags([note]) == {"[AI:検出DB#7]", "[AI:検出@1#2]"}


def test_a_detection_layers_tags_name_its_run():
    from mikage.core.ai_results import detection_runs, same_run

    assert detection_runs("AI検出: boat [AI:検出@1794013200000#3] [AI:検出DB#7] [AI:検出@layer_1#2]") == [
        ("[AI:検出@1794013200000#3]", 1794013200000), ("[AI:検出@layer_1#2]", None)]
    # To the second (a database may keep no less), and either unknown.
    assert same_run(1794013200000, 1794013200999) and not same_run(1794013202000, 1794013200000)
    assert same_run(None, 5) and same_run(5, None)
