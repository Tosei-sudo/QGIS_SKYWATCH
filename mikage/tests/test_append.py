"""アペンド（既存レイヤーへ追加） without QGIS (core/append.py): the field map, the values, the plan, an Esri target's write and its taking back."""
import json
from datetime import date, datetime, timezone

from mikage.core import append as ap
from mikage.core.esri import EsriClient, LayerInfo
from mikage.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://example.com/arcgis/rest/services/Things/FeatureServer/0"


def schema(**extra):
    fields = [
        ap.TargetField("OBJECTID", ap.INTEGER, editable=False, automatic="サーバーが付ける"),
        ap.TargetField("NAME", ap.STRING, "名称", 8, nullable=False),
        ap.TargetField("KIND", ap.INTEGER, "種別", domain=ap.Domain(coded={1: "船", 2: "車"})),
        ap.TargetField("SIZE_M", ap.DOUBLE, "大きさ", domain=ap.Domain(low=0, high=500)),
        ap.TargetField("SEEN_AT", ap.DATETIME, "日時"),
        ap.TargetField("NOTE", ap.STRING, "備考", 20),
    ]
    return ap.TargetSchema(fields, ap.POINT, "モノ", **extra)


SOURCE = [ap.SourceField("name"), ap.SourceField("kind", ap.STRING), ap.SourceField("size", ap.DOUBLE), ap.SourceField("Seen At", ap.DATETIME),
          ap.SourceField("memo", ap.STRING, "備考")]


def row(number, **values):
    return ap.SourceRow("in", number, values, geometry={"x": 1, "y": 2})


def test_the_field_map_starts_from_names_case_aliases_and_marks_aside():
    m = ap.auto_map(schema(), [("in", SOURCE)])
    got = {e.target: e.rule("in").fields for e in m.entries}
    # OBJECTID is the server's: not in the map; SIZE_M has no field of its name.
    assert got == {"NAME": ["name"], "KIND": ["kind"], "SIZE_M": [], "SEEN_AT": ["Seen At"], "NOTE": ["memo"]}
    assert m.entry("SIZE_M").rule("in").source == ap.EMPTY
    assert m.entry("note").rule("in").text() == "memo"


def test_a_field_map_goes_to_json_and_back_and_fits_other_inputs():
    m = ap.auto_map(schema(), [("in", SOURCE)])
    e = m.entry("NOTE")
    e.rules["in"] = ap.InputRule(ap.FIELDS, ["name", "memo"], start=1, end=4)
    e.merge, e.delimiter = "join", " / "
    m.entry("SIZE_M").rules["in"] = ap.InputRule(ap.EXPRESSION, expression='"size" * 2')
    m.entry("KIND").rules["in"] = ap.InputRule(ap.VALUE, value="1")
    text = m.text()
    back = ap.map_from_json(json.loads(text), [])
    assert back.to_json() == m.to_json()
    assert back.entry("NOTE").rule("in").text() == "name + memo [1〜4文字目]"
    # Another input with other names: its rules by position, fields it lacks left out and told.
    other = [("other", [ap.SourceField("NAME"), ap.SourceField("memo")])]
    fitted, problems = ap.fit_map(back, schema(), other)
    assert fitted.entry("NOTE").rule("other").fields == ["NAME", "memo"]
    assert fitted.entry("SEEN_AT").rule("other").source == ap.EMPTY
    assert any("列「Seen At」がない" in p for p in problems)
    # A plain {target: rule} object and names alone (the Processing tool's short form).
    short = ap.map_from_json('{"NAME": "memo", "NOTE": {"from": "value", "value": "取込"}, "NOPE": "x"}', [])
    fitted, problems = ap.fit_map(short, schema(), [("in", SOURCE)])
    assert fitted.entry("NAME").rule("in").fields == ["memo"] and fitted.entry("NOTE").rule("in").value == "取込"
    # Fields the map does not name: by the names.
    assert fitted.entry("KIND").rule("in").fields == ["kind"]
    assert problems == ["追加先に列「NOPE」がない（無視）"]
    problems = []
    assert ap.map_from_json("{not json", problems).entries == [] and "JSON として読めない" in problems[0]


def test_merge_rules_and_parts_of_text():
    assert ap.merged(["a", None, "b"], "join", "・") == ("a・b", "")
    assert ap.merged([None, "b"], "first") == (None, "")
    assert ap.merged(["a", "b"], "last") == ("b", "")
    assert ap.merged([1, "2", None], "sum") == (3.0, "")
    assert ap.merged([1, "x", 3], "mean") == (2.0, "数値でない値を除いて計算")
    assert ap.merged([3, 1, 2], "median")[0] == 2
    assert ap.merged([3, 1, 2], "min")[0] == 1 and ap.merged([3, 1, 2], "max")[0] == 3 and ap.merged([3, 1, 5], "range")[0] == 4
    assert ap.merged(["a", "b", "a"], "mode")[0] == "a"
    assert ap.merged(["a", None, ""], "count")[0] == 1
    assert ap.merged([2, 4, 4, 4, 5, 5, 7, 9], "stddev")[0] > 2 and ap.merged([1], "stddev")[0] is None
    m = ap.FieldMap([ap.FieldEntry("NOTE", "join", "-", {"in": ap.InputRule(ap.FIELDS, ["a", "b"], start=2, end=3)})])
    assert ap.mapped_value(ap.SourceRow("in", 1, {"a": "ABCD", "b": 12345}), m.entries[0], m.entries[0].rule("in"))[0] == "BC-23"


def test_values_take_the_fields_type_and_length():
    f = ap.TargetField("N", ap.INTEGER)
    assert ap.convert("１２", f).value == 12
    assert ap.convert(2.6, f).value == 3 and "丸め" in ap.convert(2.6, f).note
    assert "数値でない" in ap.convert("abc", f).problem
    assert ap.convert("", f).value is None
    text = ap.TargetField("T", ap.STRING, length=3)
    assert ap.convert(12.0, text).value == "12"
    assert "文字数が多すぎる" in ap.convert("abcd", text).problem
    assert ap.convert("abcd", text, truncate=True).value == "abc"
    when = ap.TargetField("W", ap.DATETIME)
    assert ap.convert("2026-11-24T00:00:00Z", when).value == datetime(2026, 11, 24, tzinfo=timezone.utc)
    assert ap.convert(1_763_942_400_000, when).value == datetime(2025, 11, 24, tzinfo=timezone.utc)
    assert "日付として読めない" in ap.convert("someday", when).problem
    day = ap.TargetField("D", ap.DATE)
    assert ap.convert("2026/11/24", day).value == date(2026, 11, 24)
    assert ap.convert(date(2026, 1, 2), day).value == date(2026, 1, 2)
    yes = ap.TargetField("B", ap.BOOL)
    assert ap.convert("はい", yes).value is True and ap.convert(0, yes).value is False and ap.convert("たぶん", yes).problem


def test_domains_codes_by_their_names_and_ranges():
    coded = ap.Domain(coded={1: "船", 2: "車"})
    assert ap.in_domain(2, coded) == (2, True)
    assert ap.in_domain("車", coded) == (2, True)
    assert ap.in_domain(2.0, coded) == (2, True)
    assert ap.in_domain(3, coded)[1] is False
    assert ap.in_domain(None, coded)[1] is True
    span = ap.Domain(low=0, high=10)
    assert ap.in_domain(10, span)[1] and not ap.in_domain(11, span)[1]


def test_the_plan_adds_and_skips_with_reasons():
    m = ap.auto_map(schema(), [("in", SOURCE)])
    m.entry("SIZE_M").rules["in"] = ap.InputRule(ap.FIELDS, ["size"])
    rows = [
        row(1, name="ふね", kind="船", size=10, memo="ok"),
        row(2, name="とても長い名前の船です", kind="1"),
        row(3, name="くるま", kind=3),
        row(4, name=None, kind=1),
        row(5, name="おおきい", kind=2, size=900),
        ap.SourceRow("in", 6, {"name": "ずれ"}, geometry_problem="形状がポイントでない（ライン）"),
    ]
    p = ap.plan(rows, m, schema(), ap.Options())
    actions = [(r.number, r.action) for r in p.rows]
    assert actions == [(1, ap.ADD), (2, ap.SKIP), (3, ap.SKIP), (4, ap.SKIP), (5, ap.SKIP), (6, ap.SKIP)]
    assert p.rows[0].values == {"NAME": "ふね", "KIND": 1, "SIZE_M": 10.0, "NOTE": "ok"}
    assert "文字数が多すぎる" in p.rows[1].reasons[0]
    assert "ドメインにない" in p.rows[2].reasons[0] and "1（船）" in p.rows[2].reasons[0]
    assert p.rows[3].reasons == ["必須の列「NAME」が空"]
    assert p.summary() == "追加 1 件・飛ばす 5 件"
    assert set(p.reason_counts()) >= {"必須の列「NAME」が空", "形状がポイントでない"}
    # Values outside a domain may go as they are; long text cut.
    p = ap.plan(rows[:3] + rows[4:5], m, schema(), ap.Options(enforce_domains=False, truncate=True))
    assert [r.action for r in p.rows] == [ap.ADD] * 4
    assert p.rows[1].values["NAME"] == "とても長い名前の" and any("切り詰め" in n for n in p.rows[1].notes)
    assert p.rows[2].values["KIND"] == 3 and any("ドメインにない" in n for n in p.rows[2].notes)
    # Text that is none of the names cannot go into the coded number field either way.
    p = ap.plan([row(7, name="ひこうき", kind="飛行機")], m, schema(), ap.Options(enforce_domains=False))
    assert p.rows[0].reasons == ["「KIND」の値がドメインにない（飛行機。使える値: 1（船）、2（車））"]


def test_expressions_and_fixed_values():
    m = ap.auto_map(schema(), [("in", SOURCE)])
    m.entry("NOTE").rules["in"] = ap.InputRule(ap.EXPRESSION, expression="upper(name)")
    m.entry("KIND").rules["in"] = ap.InputRule(ap.VALUE, value="車")
    rows = [ap.SourceRow("in", 1, {"name": "a"}, {"NOTE": "A"}), ap.SourceRow("in", 2, {"name": "b"}, {"NOTE": ap.ExpressionError("0 で割った")})]
    p = ap.plan(rows, m, schema(), ap.Options())
    assert p.rows[0].values == {"NAME": "a", "KIND": 2, "NOTE": "A"}
    assert p.rows[1].action == ap.SKIP and p.rows[1].reasons == ["「NOTE」の式の誤り（0 で割った）"]


def test_schema_types_stop_or_skip_inputs_that_do_not_match():
    s = schema()
    same = [ap.SourceField("name"), ap.SourceField("kind", ap.INTEGER), ap.SourceField("size_m", ap.DOUBLE),
            ap.SourceField("seen_at", ap.DATETIME), ap.SourceField("note")]
    assert ap.schema_problems(s, same) == []
    problems = ap.schema_problems(s, SOURCE)
    assert "列「KIND」の型が違う（文字列 → 整数）" in problems and "列「SIZE_M」がない" in problems and "追加先に列「memo」がない" in problems
    rows = [ap.SourceRow("good", 1, {"name": "a"}), ap.SourceRow("bad", 1, {"name": "b"})]
    m = ap.auto_map(s, [("good", same), ("bad", SOURCE)])
    p = ap.plan(rows, m, s, ap.Options(schema_type=ap.TEST_AND_SKIP), skipped_inputs={"bad": "列「SIZE_M」がない"})
    assert [r.input for r in p.rows] == ["good"] and "飛ばした入力 1 個" in p.summary()
    p = ap.plan(rows, m, s, ap.Options(schema_type=ap.TEST), skipped_inputs={"bad": "列「SIZE_M」がない"})
    assert p.stopped and p.rows == [] and "中止" in p.summary()


def test_matching_keys_update_the_target_feature_and_only_the_mapped_fields():
    m = ap.auto_map(schema(), [("in", SOURCE)])
    existing = {("ふね",): [ap.Existing(7, {"NAME": "ふね", "NOTE": "前"})], ("二つ",): [ap.Existing(8), ap.Existing(9)]}
    rows = [row(1, name="ふね", memo="新"), row(2, name="くるま"), row(3, name="二つ"), row(4, name="ふね", memo="重複")]
    p = ap.plan(rows, m, schema(), ap.Options(match_fields=["NAME"]), existing)
    assert [(r.action, r.target_id) for r in p.rows] == [(ap.UPDATE, 7), (ap.ADD, None), (ap.SKIP, None), (ap.SKIP, None)]
    # Mapped fields are sent (an empty source empties them); SIZE_M has no source and is left as it is.
    assert p.rows[0].values == {"NAME": "ふね", "KIND": None, "SEEN_AT": None, "NOTE": "新"}
    assert p.rows[0].geometry is None  # (not asked to update the geometry)
    assert p.rows[2].reasons == ["追加先に同じキーの地物が 2 件ある"]
    assert p.rows[3].reasons == ["入力の中でキーが重なる（先の 1 件だけ）"]
    assert p.before[7].attributes["NOTE"] == "前"
    p = ap.plan(rows[:1], m, schema(), ap.Options(match_fields=["NAME"], update_geometry=True), existing)
    assert p.rows[0].geometry == {"x": 1, "y": 2}
    assert ap.key_text(1.0) == ap.key_text(1) == ap.key_text(" 1 ") == "1" and ap.key_text("") is None


def _esri():
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({"NAME": "esriFieldTypeString", "KIND": "esriFieldTypeInteger", "NOTE": "esriFieldTypeString",
                                           "SEEN_AT": "esriFieldTypeDate"}))
    layer.extra = {"name": "モノ", "extent": {"spatialReference": {"wkid": 102100, "latestWkid": 3857}}}
    return fake, layer, EsriClient(fake)


def test_an_esri_layers_fields_with_lengths_domains_and_what_the_server_fills():
    info = LayerInfo({"objectIdField": "OBJECTID", "geometryType": "esriGeometryPolygon", "name": "面", "hasZ": True, "fields": [
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "NAME", "type": "esriFieldTypeString", "alias": "名称", "length": 20, "nullable": False},
        {"name": "KIND", "type": "esriFieldTypeSmallInteger", "domain": {"type": "codedValue", "name": "種別",
                                                                         "codedValues": [{"name": "船", "code": 1}]}},
        {"name": "DEPTH", "type": "esriFieldTypeDouble", "domain": {"type": "range", "range": [0, 100]}, "defaultValue": 0},
        {"name": "DAY", "type": "esriFieldTypeDateOnly"},
        {"name": "GlobalID", "type": "esriFieldTypeGlobalID"},
        {"name": "Shape__Area", "type": "esriFieldTypeDouble", "editable": False},
        {"name": "CREATED_USER", "type": "esriFieldTypeString"},
    ]})
    s = ap.esri_schema(info, tracked=["CREATED_USER"])
    assert s.geometry == ap.POLYGON and s.has_z and s.name == "面"
    assert [f.name for f in s.mappable] == ["NAME", "KIND", "DEPTH", "DAY"]
    assert s.field("name").length == 20 and not s.field("NAME").nullable and s.field("NAME").alias == "名称"
    assert s.field("KIND").domain.coded == {1: "船"} and s.field("DEPTH").domain.high == 100 and s.field("DEPTH").has_default
    assert s.field("CREATED_USER").automatic == "作成者・更新者（自動）" and s.field("GlobalID").automatic == "サーバーが付ける"
    assert ap.esri_value(date(2026, 11, 24), s.field("DAY")) == "2026-11-24"
    assert ap.esri_value(datetime(2026, 11, 24, tzinfo=timezone.utc), ap.TargetField("W", ap.DATETIME)) == 1_795_478_400_000


def test_an_esri_target_is_written_in_batches_and_taken_back():
    fake, layer, client = _esri()
    layer.add({"NAME": "ふね", "NOTE": "前"}, {"x": 0, "y": 0})
    s = ap.esri_schema(client.layer(URL))
    m = ap.auto_map(s, [("in", [ap.SourceField("NAME"), ap.SourceField("NOTE"), ap.SourceField("SEEN_AT", ap.DATETIME)])])
    rows = [ap.SourceRow("in", i, {"NAME": f"n{i}", "NOTE": "新", "SEEN_AT": datetime(2026, 11, 24, tzinfo=timezone.utc)}, geometry={"x": i, "y": 1})
            for i in range(450)]
    rows.append(ap.SourceRow("in", 999, {"NAME": "ふね", "NOTE": "更新"}, geometry={"x": 5, "y": 5}))
    existing = ap.esri_existing(client, URL, None, s, ["NAME"], {("ふね",), ("n1",)}, ["NAME", "NOTE"], True)
    assert list(existing) == [("ふね",)] and existing[("ふね",)][0].attributes == {"NAME": "ふね", "NOTE": "前"}
    p = ap.plan(rows, m, s, ap.Options(match_fields=["NAME"], update_geometry=True), existing)
    assert p.count(ap.ADD) == 450 and p.count(ap.UPDATE) == 1
    seen = []
    applied = ap.write_esri(client, URL, None, p, s, progress=seen.append)
    assert len(applied.added) == 450 and len(applied.updated) == 1 and not applied.failed and not applied.rolled_back
    assert seen[-1] == 100.0 and len([a for a in fake.asked if a[0].endswith("applyEdits")]) == 3
    assert len(layer.rows) == 451
    first = next(r for r in layer.rows if r["attributes"]["NAME"] == "n0")
    assert first["attributes"]["SEEN_AT"] == 1_795_478_400_000 and first["geometry"] == {"x": 0, "y": 1}
    old = next(r for r in layer.rows if r["attributes"]["OBJECTID"] == 1)
    assert old["attributes"]["NOTE"] == "更新" and old["geometry"] == {"x": 5, "y": 5}
    assert "追加 450 件・更新 1 件" == applied.text()
    # Taken back: the added deleted, the updated given back its values and geometry.
    assert ap.undo_esri(client, URL, None, applied) == []
    assert len(layer.rows) == 1 and layer.rows[0]["attributes"]["NOTE"] == "前" and layer.rows[0]["geometry"] == {"x": 0, "y": 0}


class Refusing(FakeEsri):
    """Refuses the adds whose NAME is 「だめ」."""

    def _apply(self, layer, form):
        adds = json.loads(form.get("adds") or "[]")
        bad = [i for i, f in enumerate(adds) if f["attributes"].get("NAME") == "だめ"]
        if not bad:
            return super()._apply(layer, form)
        good = [f for i, f in enumerate(adds) if i not in bad]
        answer = super()._apply(layer, {**form, "adds": json.dumps(good)})
        results = iter(answer["addResults"])
        answer["addResults"] = [{"success": False, "error": {"description": "値が不正"}} if i in bad else next(results) for i in range(len(adds))]
        return answer


def test_a_refused_feature_takes_back_the_whole_write_unless_asked_not_to():
    fake = Refusing()
    layer = fake.add_layer(URL, FakeLayer({"NAME": "esriFieldTypeString"}))
    client = EsriClient(fake)
    s = ap.esri_schema(client.layer(URL))
    m = ap.auto_map(s, [("in", [ap.SourceField("NAME")])])
    rows = [ap.SourceRow("in", i, {"NAME": "だめ" if i == 250 else f"n{i}"}) for i in range(300)]
    p = ap.plan(rows, m, s, ap.Options())
    applied = ap.write_esri(client, URL, None, p, s, all_or_nothing=True)
    assert applied.rolled_back and layer.rows == [] and applied.failed[0][1] == "値が不正"
    assert applied.text().startswith("1 件が失敗したため、今回の分をすべて取り消し（最初の理由: 値が不正）")
    applied = ap.write_esri(client, URL, None, p, s, all_or_nothing=False)
    assert not applied.rolled_back and len(layer.rows) == 299 and len(applied.failed) == 1
    assert applied.text() == "追加 299 件・失敗 1 件"


def test_a_failed_request_and_a_cancel_take_back_what_went_in():
    fake, layer, client = _esri()
    s = ap.esri_schema(client.layer(URL))
    m = ap.auto_map(s, [("in", [ap.SourceField("NAME")])])
    p = ap.plan([ap.SourceRow("in", i, {"NAME": f"n{i}"}) for i in range(500)], m, s, ap.Options())
    calls = []

    class Cancelled(Exception):
        pass

    def check():
        calls.append(1)
        if len(calls) == 3:
            raise Cancelled()

    applied = ap.write_esri(client, URL, None, p, s, check=check)
    assert applied.cancelled and applied.rolled_back and layer.rows == []
    applied = ap.write_esri(client, URL, None, p, s, all_or_nothing=False, check=lambda: calls.append(1) if len(calls) < 5 else
                            (_ for _ in ()).throw(Cancelled()))
    assert applied.cancelled and not applied.rolled_back and 0 < len(layer.rows) < 500 and "途中で取消" in applied.text()
    layer.rows.clear()
    fake.fail_next = {"error": {"code": 500, "message": "boom"}}
    applied = ap.write_esri(client, URL, None, p, s)
    assert applied.rolled_back and layer.rows == []


def test_the_same_table_whatever_the_case_or_slash():
    assert ap.same_table(URL + "/", URL.upper() + "?token=x")
    assert not ap.same_table(URL, URL.replace("/0", "/1")) and not ap.same_table("", "")
