"""Templates for a new scratch layer: ported from the viewer's layer-templates.test.ts and the design checks of new-layer.test.ts."""
import os

from satellite_observation.core.config import read_config_file
from satellite_observation.core.layer_templates import (
    BUILTIN_TEMPLATES, FieldDesign, date_text, design_fields, parse_codes, template_json, template_of,
)
from satellite_observation.core.plan.moving import guess_target_fields

HERE = os.path.dirname(__file__)


def test_the_built_in_templates_all_make_valid_layers():
    for t in BUILTIN_TEMPLATES:
        result = design_fields(t.title, t.fields)
        assert not (result and isinstance(result[0], str)), (t.name, result)
    assert BUILTIN_TEMPLATES[0].name == "空のレイヤー"
    assert [t.name for t in BUILTIN_TEMPLATES] == ["空のレイヤー", "航跡（生存圏計算用）", "調査地点", "目標・物体", "関心区域（AOI）", "被害状況", "経路・測線", "注記"]


def test_the_track_template_is_one_the_moving_target_plan_reads_without_mapping():
    track = next(t for t in BUILTIN_TEMPLATES if t.name.startswith("航跡"))
    assert track.geometryType == "Point"
    assert guess_target_fields([f.name for f in track.fields]) == {"id": "track_id", "time": "time", "course": "course", "speed": "speed", "name": "name"}


def test_reads_templates_from_json_and_says_what_is_wrong():
    problems = []
    t = template_of({"name": " 巡回 ", "geometryType": "LineString", "fields": [{"name": "kind", "codes": ["A", "B"]}, {"name": "n", "type": "integer", "min": 0, "defaultValue": 1}]},
                    problems, "layerTemplates[0]")
    assert (t.name, t.title, t.geometryType, t.description) == ("巡回", "巡回", "LineString", "")
    assert t.fields == [
        FieldDesign("kind", "string", "", False, None, None, None, "A, B", ""),
        FieldDesign("n", "integer", "", False, None, 0, None, "", "1"),
    ]
    assert problems == []
    assert template_of({"name": "x", "geometryType": "Circle", "fields": []}, problems, "a") is None
    assert template_of({"fields": []}, problems, "b") is None
    assert template_of({"name": "y", "fields": [{"name": "z", "type": "bool"}]}, problems, "c").fields == []
    assert problems == [
        "a の geometryType は Point・LineString・Polygon・MultiPoint・MultiLineString・MultiPolygon・null のいずれかで指定",
        "b の name は文字列で指定",
        "c.fields[0] は name と type（string・integer・double・date）で指定",
    ]


def test_a_template_goes_to_json_and_back():
    for t in BUILTIN_TEMPLATES:
        assert template_of(template_json(t), [], "x") == t


def test_the_settings_file_gives_the_organisations_templates():
    config, problems = read_config_file(os.path.join(HERE, "data", "viewer-config.jsonc"))
    assert problems == []
    [t] = config.layer_templates
    assert (t.name, t.title, t.geometryType, t.description) == ("巡回記録", "巡回", "LineString", "巡回の経路と結果")
    assert [(f.name, f.type, f.alias, f.required, f.codes, f.defaultValue) for f in t.fields] == [
        ("team", "string", "班", True, "", ""), ("result", "integer", "", False, "1=異常なし, 2=要確認", "1"), ("patrolled", "date", "実施日", False, "", "")]


def test_checks_the_design_as_the_viewer_does():
    bad = design_fields("", [
        FieldDesign(""), FieldDesign("fid"), FieldDesign("a"), FieldDesign("A"), FieldDesign('q"'),
        FieldDesign("s", length=0), FieldDesign("n", "integer", min=5, max=1), FieldDesign("c", "integer", codes="1=a, x"),
        FieldDesign("d", "integer", codes="1=a, 2=b", defaultValue="3"), FieldDesign("t", "date", defaultValue="昨日"),
        FieldDesign("r", "double", min=0, max=10, defaultValue="11"),
    ])
    assert bad == [
        "レイヤーの名前を入力", "1 番目の属性の名前を入力", "属性「fid」は書き出しで使う名前なので、別の名前で指定",
        "属性「A」が重複（大文字・小文字の区別なし）", '属性「q"」の名前に使用不可の文字あり', "属性「s」の文字数の上限は 1 以上の整数で指定",
        "属性「n」の範囲は 最小 ≦ 最大 で指定", "属性「c」: 候補値「x」は整数で指定", "属性「d」の既定値は候補値から選択",
        "既定値: t は日時で入力", "既定値: r は 0〜10 の範囲で入力",
    ]
    [s] = design_fields("x", [FieldDesign("confidence", "integer", "確度", codes="1=確実, 2=可能性大", defaultValue="2")])
    assert (s.alias, s.codes, s.default) == ("確度", [(1, "確実"), (2, "可能性大")], 2)
    assert design_fields("x", []) == []


def test_reads_allowed_values_and_dates():
    assert parse_codes("赤、青\n緑", "string") == [("赤", "赤"), ("青", "青"), ("緑", "緑")]
    assert parse_codes("1=a, 1=b", "integer") == "候補値「1」が重複"
    assert parse_codes("1.5=a", "double") == [(1.5, "a")]
    assert date_text("2026-10-17") == "2026-10-17T00:00:00Z"
    assert date_text("2026/1/7 9:30") == "2026-01-07T09:30:00"
    assert date_text("2026-10-17T09:30:05+0900") == "2026-10-17T09:30:05+09:00"
    assert date_text("2026-02-30") is None
