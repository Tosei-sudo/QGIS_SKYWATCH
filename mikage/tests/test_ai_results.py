"""
External AI results without QGIS (core/ai_results.py): the settings, the
query of one image, a feature read as a result, what it is recorded as, and
several recorded at once (ObservationBook.add_many).
"""
from dataclasses import replace
from types import SimpleNamespace

from mikage.core.ai_results import (
    AiResult, AiResultSource, ai_result_source_of, image_where, import_entry, kind_for, recorded_tags, result_of, result_tag,
)
from mikage.core.config import parse_config
from mikage.core.obs.ai_origin import AI_DETECTED, AI_EXTERNAL, AiOrigin
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
        "aiResultSources[2] の fields.colour は使えない役割（imageId / id / class / score / model / detectedAt / name / kind / activity / length / heading / width / note）",
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
    # Where it came from in its AI columns; the note is the person's (only the result's own note, none here: 2026-11-27).
    assert e.note == ""
    assert e.ai == AiOrigin(source=AI_EXTERNAL, layer="外部AI", result_id="7", score=0.87, model="", detected_at=None)
    records = [SimpleNamespace(ai=None, note="前の記録 [AI:古い#1]"), SimpleNamespace(ai=e.ai), SimpleNamespace(ai=AiOrigin(AI_DETECTED, "別", "1"))]
    assert recorded_tags(records) == {"[AI:外部AI#7]", "[AI:別#1]"}
    assert result_tag("a]#b", "1") == "[AI:ab#1]"
    # The result's own note is the record's.
    with_note = ai_result_source_of({"label": "外部AI", "url": URL, "fields": {"imageId": "IMAGE_ID", "note": "NOTE"}}, [], 0)
    noted = result_of(with_note, {"attributes": {"OBJECTID": 8, "IMAGE_ID": "A", "NOTE": "船首に損傷"}}, "OBJECTID")
    assert import_entry(noted, with_note, 139.7, 35.6).note == "船首に損傷"


def test_several_results_recorded_at_once_are_things_of_their_class(book):  # noqa: F811
    s, _ = source()
    results = [result_of(s, {"attributes": {"OBJECTID": i, "IMAGE_ID": "d1", "CLS": "ship", "SCORE": 0.9}}, "OBJECTID") for i in (1, 2)]
    results.append(result_of(s, {"attributes": {"OBJECTID": 3, "IMAGE_ID": "d1", "CLS": "car"}}, "OBJECTID"))
    saved = book.add_many([import_entry(r, s, 139.7, 35.6) for r in results], image="d1")
    # Named after their class, each a thing of its own (only individuals are told apart by name, 2026-11-18).
    assert [(o.object_name, o.kind, o.image_id) for o in saved] == [("支援船", "船舶 > 支援船", "d1"), ("支援船", "船舶 > 支援船", "d1"), ("car", "car", "d1")]
    assert len({o.object_id for o in saved}) == 3
    assert book.said[-1] == "3 件を記録"
    assert book.add_many([], image="nope") is None and book.said[-1] == "画像なし。取り込む画像を開いてから取り込み"


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
    e = import_entry(r, db, 139.7, 35.6)
    assert e.note == "" and e.ai == AiOrigin(source=AI_DETECTED, layer="検出DB", result_id="7", score=0.8, model="港湾モデル",
                                              detected_at=1794013200000)


def test_a_detection_layers_feature_is_named_by_its_layer_and_run_until_it_is_sent():
    from mikage.core.ai_results import same_run
    from mikage.core.obs.ai_origin import of_layer_run

    layer = AiResultSource(label="検出結果 yolo", url="", fields={"imageId": "image_id"}, local=True)
    r = AiResult(source="検出@1794013200000", oid=3, key="3", image_id="A", cls="boat", score=0.5, model="yolo", detected_at=1794013200000,
                 name="", kind="", activity="", length=None, heading=None, note="", geometry=None, attributes={})
    ai = import_entry(r, layer, 139.7, 35.6).ai
    assert (ai.source, ai.layer, ai.result_id, ai.detected_at) == (AI_DETECTED, "検出結果 yolo", "3", 1794013200000)
    # The same feature of the same run (to the second), whatever the layer is called now; not a table's row.
    assert of_layer_run(ai, "3", 1794013200400) and of_layer_run(ai, 3, 1794013200000, layer="改名")
    assert not of_layer_run(ai, "3", 1794013202000) and not of_layer_run(ai, "4", 1794013200000)
    assert not of_layer_run(ai, "3", 1794013200000, tables={"検出結果 yolo"})
    # A run without a time: the layer's name.
    timeless = replace(ai, detected_at=None)
    assert of_layer_run(timeless, "3", None, layer="検出結果 yolo") and not of_layer_run(timeless, "3", None, layer="別")
    # Sent to a table before it was recorded: the table's row.
    sent = import_entry(r, layer, 139.7, 35.6, sent=("検出DB", "120")).ai
    assert (sent.layer, sent.result_id) == ("検出DB", "120")
    # To the second (a database may keep no less), and either unknown.
    assert same_run(1794013200000, 1794013200999) and not same_run(1794013202000, 1794013200000)
    assert same_run(None, 5) and same_run(5, None)


def test_how_results_are_matched_is_set_in_the_settings_file():
    from mikage.core.ai_results import output_source, rules_for
    from mikage.core.ai.transfer import detection_output_of

    rules = [{"label": "外形の中", "centerInRecord": True, "kind": "sibling"},
             {"near": 50, "kind": "related", "lengthGap": 0.3, "headingGap": 30, "minScore": 0.6}]
    config, problems = parse_config({"aiContinuation": {"rules": rules, "candidateRadius": 200, "unique": False}})
    assert problems == []
    s, _ = source()
    r = rules_for(s, config)
    assert (r.on, r.candidate_radius, r.unique, [x.label for x in r.rules]) == (True, 200, False, ["外形の中", ""])
    first, second = r.rules
    assert (first.center_in_record, first.kind, first.near) == (True, "sibling", None)
    assert (second.near, second.length_gap, second.heading_gap, second.min_score, second.default) == (50, 0.3, 30, 0.6, False)
    assert second.text == "50 m 以内・同系の分類・長さの差 30% 以内・向きの差 30° 以内・確信度 0.60 以上"
    # None written: as before rules could be set.
    r = rules_for(s, parse_config({})[0])
    assert (r.on, r.candidate_radius, r.unique) == (True, 300, True) and [(x.near, x.default) for x in r.rules] == [(50, True)]
    # A source's own keys win, key by key; the older ones too.
    own, problems = source(continuation={"rules": [{"recordInAi": True}]})
    assert problems == []
    r = rules_for(own, config)
    assert (r.candidate_radius, r.unique, [x.record_in_ai for x in r.rules]) == (200, False, [True])
    old, _ = source(continueRadius=80)
    r = rules_for(old, config)
    assert [(x.near, x.default) for x in r.rules] == [(80, True)] and r.candidate_radius == 200
    assert not rules_for(source(candidateRadius=0)[0], config).on
    assert not rules_for(source(continuation=False)[0], config).on
    assert not rules_for(s, parse_config({"aiContinuation": False})[0]).on
    # The plugin's own detections and the tables read back follow aiContinuation.
    out = detection_output_of({"label": "転記先", "url": URL, "fields": {"imageId": "IMAGE_ID"}}, [], 0)
    table = output_source(out)
    assert table is not None and rules_for(table, config).candidate_radius == 200


def test_what_is_wrong_in_a_matching_setting():
    _, problems = parse_config({"aiContinuation": {"rules": [{"kind": "same"}, {"near": -1}, {"centerInRecord": "yes", "overlap": 2},
                                                             {"near": 10, "kind": "all", "lengthGap": 3, "extra": 1}, "x"],
                                                   "candidateRadius": "a", "unique": 1, "radius": 5}})
    at = "aiContinuation"
    assert problems == [
        f"{at} の radius は使えないキー（rules / candidateRadius / unique）",
        f"{at} の rules[0] に位置の条件（near・centerInRecord・recordInAi・overlap のどれか）なし",
        f"{at} の rules[1] の near は 0〜100000（m）で指定",
        f"{at} の rules[1] に位置の条件（near・centerInRecord・recordInAi・overlap のどれか）なし",
        f"{at} の rules[2] の overlap は 0〜1 で指定",
        f"{at} の rules[2] の centerInRecord は true か false で指定",
        f"{at} の rules[2] に位置の条件（near・centerInRecord・recordInAi・overlap のどれか）なし",
        f"{at} の rules[3] の extra は使えない条件（label / near / centerInRecord / recordInAi / overlap / kind / lengthGap / headingGap / minScore）",
        f"{at} の rules[3] の lengthGap は 0〜1 で指定",
        f"{at} の rules[3] の kind は same / related / sibling / any で指定",
        f"{at} の rules[4] は条件のオブジェクトで指定",
        f"{at} の candidateRadius は 0〜100000（m）で指定",
        f"{at} の unique は true か false で指定",
    ]
    _, problems = source(continuation=[1])
    assert problems == ["aiResultSources[0] の continuation は false かオブジェクト（rules・candidateRadius・unique）で指定"]
