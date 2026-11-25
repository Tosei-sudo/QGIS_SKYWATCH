"""
参考情報の JSON 取り込み (tsudo, 2026-11-25): the schema (the sample fits it;
its lengths and link types are the plugin's), the file checked with it, the
dry run (新規・更新・変更なし・重複・エラー, the place from lon/lat or a named
area, warnings) and the import (infos added and updated, links added once).
"""
import json
import os

from mikage.core import reference_import as ri
from mikage.core.config import parse_config
from mikage.core.esri import EsriClient
from mikage.core.json_schema import validate
from mikage.core.named_areas import OrgArea
from mikage.core.reference_info import ITEM_TYPES, LinkTarget, add_links, load_infos, load_links
from mikage.core.shapes import in_rings
from mikage.core.time_text import parse_time
from mikage.tests.fake_esri import FakeEsri, FakeLayer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE = os.path.join(ROOT, "docs", "reference-info", "sample.json")
INFOS = "https://x/arcgis/rest/services/Events/FeatureServer/2"
LINKS = "https://x/arcgis/rest/services/Events/FeatureServer/3"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
INFO_TYPES = {"INFO_ID": S, "TITLE": S, "INFO_TIME": T, "PUBLISHED_AT": T, "SOURCE": S, "SOURCE_REF": S, "RELIABILITY": S, "SUMMARY": S,
              "KIND": S, "NAMED_AREA_ID": S, "NOTE": S, "CREATED_USER": S, "CREATED_DATE": T, "UPDATED_USER": S, "UPDATED_DATE": T}
#: The default table's lengths (docs/admin/esri-tables.md).
LENGTHS = {"INFO_ID": 64, "TITLE": 255, "SOURCE": 255, "SOURCE_REF": 1000, "RELIABILITY": 64, "SUMMARY": 4000, "KIND": 1000,
           "NAMED_AREA_ID": 255, "NOTE": 2000}
BAY = [[[139.65, 35.3], [140.1, 35.3], [140.1, 35.65], [139.65, 35.65], [139.65, 35.3]]]
AREAS = [OrgArea(oid=1, id="NA-000001", name="東京湾", aliases=("江戸湾",), rings=None),
         OrgArea(oid=2, id="NA-000002", name="相模湾"), OrgArea(oid=3, id="NA-000003", name="大島"), OrgArea(oid=4, id="NA-000004", name="大島")]


def setup(links=True, lengths=None):
    fake = FakeEsri()
    infos = fake.add_layer(INFOS, FakeLayer(dict(INFO_TYPES), "esriGeometryPoint"))
    infos.extra["fields"] = [{"name": "OBJECTID", "type": "esriFieldTypeOID"}] + [
        {"name": n, "type": t, **({"length": (lengths or LENGTHS)[n]} if n in (lengths or LENGTHS) else {})} for n, t in INFO_TYPES.items()]
    table = fake.add_layer(LINKS, FakeLayer({"INFO_ID": S, "ITEM_TYPE": S, "ITEM_ID": S, "LABEL": S, "CREATED_USER": S, "CREATED_DATE": T}, ""))
    config, problems = parse_config({"referenceInfo": {"layer": INFOS, **({"links": LINKS} if links else {})}})
    assert problems == []
    return EsriClient(fake), config.reference_info, infos, table


def known(**more):
    asked = []

    def shapes(areas):
        asked.append([a.oid for a in areas])
        return {1: BAY}

    return ri.Known(areas=AREAS, shapes=shapes, reliabilities=("確定", "推定", "不明"), kinds=("船舶", "船舶 > 支援船"), **more), asked


def file(*infos, **top):
    return ri.parse_text(json.dumps({"infos": list(infos), **top}, ensure_ascii=False), "test.json")


def test_the_sample_fits_the_schema_and_the_schema_the_plugin():
    schema = ri.schema()
    with open(SAMPLE, encoding="utf-8") as f:
        sample = json.load(f)
    assert validate(sample, schema) == []
    data = ri.read_file(SAMPLE)
    assert data.usable and data.row_problems == {} and len(data.rows) == 3
    # The defaults go into the rows without those keys only.
    assert [r.get("source") for r in data.rows] == ["海上保安庁", "〇〇新聞", "海上保安庁"]
    info = schema["$defs"]["info"]["properties"]
    defs = schema["$defs"]
    lengths = {"id": info["id"]["maxLength"], "title": info["title"]["maxLength"],
               **{k: defs[k]["maxLength"] for k in ("source", "sourceRef", "reliability", "summary", "kind", "note", "area")}}
    assert lengths == {"id": 64, "title": 255, "source": 255, "sourceRef": 1000, "reliability": 64, "summary": 4000, "kind": 1000,
                       "note": 2000, "area": 255}
    assert tuple(defs["link"]["properties"]["type"]["enum"]) == ITEM_TYPES
    assert set(schema["properties"]["defaults"]["properties"]) == set(ri.DEFAULT_KEYS)
    assert set(info) == set(ri.KEY_LABELS) - {"place"}
    assert schema["properties"]["format"]["const"] == ri.FORMAT and schema["properties"]["version"]["const"] == ri.VERSION
    # Every time the pattern takes, the plugin reads.
    for t in ("2026-11-24", "2026-11-24T10:30", "2026-11-24 10:30:15", "2026-11-24T10:30:00.5+09:00", "2026-11-24T01:30:00Z"):
        assert validate(t, defs["time"]) == [] and parse_time(t) is not None, t
    for t in ("2026/11/24", "2026-11-24T10", "24-11-2026", "2026-11-24Z"):
        assert validate(t, defs["time"]) != [], t


def test_a_file_is_refused_as_a_whole_or_row_by_row():
    assert ri.parse_text("{ infos: [] }").problems[0].startswith("JSON として読めない（1 行 3 文字目")
    assert ri.parse_text("[]").problems == ['全体が { "infos": [ … ] } の形のオブジェクトでない']
    assert ri.parse_text('{"infos": []}').problems == ["infos: 参考情報が 1 件未満"]
    assert ri.parse_text('{"infos": [{"title": "a", "time": "2026-01-01"}], "version": 2, "extra": 1}').problems == [
        "version: 1 以外の値", "知らないキー「extra」"]
    assert ri.parse_text('{"infos": [{"title": "a", "time": "2026-01-01"}], "defaults": {"title": "x"}}').problems == [
        "defaults に書けないキー「title」"]
    data = file({"title": " ", "time": "2026/01/01"}, {"title": "a", "time": "2026-01-01", "lon": "139", "sumary": "x"},
                {"title": "b", "time": "2026-01-01", "links": [{"type": "events", "id": ""}, {"id": "x"}]}, 3)
    assert data.usable
    assert data.row_problems == {
        0: ["題名: 空", "出来事の日時: " + ri.TIME_WORDS],
        1: ["経度と緯度は両方書く", "経度: 数でない", "知らないキー「sumary」"],
        2: ["紐付け 1 番目の種類: event・thing・observation のどれか", "紐付け 1 番目の ID: 空", "紐付け 2 番目: type がない"],
        3: ["オブジェクトでない"],
    }


def test_new_rows_their_places_and_what_is_wrong(monkeypatch):
    monkeypatch.setenv("USERNAME", "佐藤")
    client, s, infos, links = setup()
    k, asked = known()
    data = file(
        {"id": "NAV-1", "title": "射撃訓練", "time": "2026-11-28T09:00:00+09:00", "source": "海上保安庁", "lon": 139.7, "lat": 35.6,
         "kind": "船舶>支援船", "reliability": "確定", "links": [{"type": "event", "id": "EV-1", "label": "演習"}]},
        {"title": "湾内の工事", "time": "2026-11-20", "area": "江戸湾"},
        {"title": "大島の発表", "time": "2026-11-20", "area": "大島"},
        {"title": "ない地名", "time": "2026-11-20", "area": "NA-999"},
        {"title": "2 月 30 日", "time": "2026-02-30"},
        {"title": "片方だけ消す", "time": "2026-11-20", "lon": None, "lat": 35.0},
        {"id": "NAV-1", "title": "同じ ID", "time": "2026-11-20"},
        {"title": "x" * 10, "time": "2026-11-20", "reliability": "たぶん", "kind": "航空機"},
    )
    plan = ri.plan_import(client, s, data, k)
    actions = [p.action for p in plan.rows]
    assert actions == [ri.NEW, ri.NEW, ri.ERROR, ri.ERROR, ri.ERROR, ri.ERROR, ri.ERROR, ri.NEW]
    first, bay = plan.rows[0].info, plan.rows[1].info
    assert first.id == "NAV-1" and (first.lon, first.lat) == (139.7, 35.6) and first.kind == "船舶 > 支援船"
    assert first.time == parse_time("2026-11-28T09:00:00+09:00") and plan.rows[0].links == [LinkTarget("event", "EV-1", "演習")]
    # A named area by an alias: its 管理番号, and a point inside it (its shape read once, the others' not asked).
    assert bay.area_id == "NA-000001" and bay.has_place and in_rings(BAY, bay.lon, bay.lat) and asked == [[1]]
    assert bay.id.startswith("REF-")
    assert plan.rows[2].problems == ["「大島」という地名が 2 つある（管理番号で書く）"]
    assert plan.rows[3].problems == ["地名「NA-999」が見つからない"]
    assert plan.rows[4].problems == ["出来事の日時: 2026-02-30 は日時として読めない"]
    assert plan.rows[5].problems == ["経度と緯度の片方だけ（消すときは両方 null）"]
    assert plan.rows[6].problems == ["同じ情報ID「NAV-1」がファイルの前の行にもある"]
    assert plan.rows[7].warnings == ["確からしさ「たぶん」は確度の段階（確定・推定・不明）にない", "分類「航空機」は分類体系にない"]
    assert plan.summary() == "新規 3・エラー 5（注意 1 件）"
    assert infos.rows == []

    result = ri.apply_import(client, s, data, k)
    assert (result.added, result.updated, result.links, result.failures) == (3, 0, 1, [])
    saved = {x.id: x for x in load_infos(client, s)}
    assert set(saved) == {x.id for x in result.saved} and len(saved) == 3
    assert saved["NAV-1"].created_by == "佐藤" and saved["NAV-1"].source == "海上保安庁"
    assert [(x.type, x.item_id, x.label) for x in load_links(client, s, ["NAV-1"])] == [("event", "EV-1", "演習")]


def test_updates_change_only_what_is_written_and_duplicates_are_skipped():
    client, s, infos, links = setup()
    k, _asked = known()
    first = file({"id": "NAV-1", "title": "射撃訓練", "time": "2026-11-28T09:00", "source": "海上保安庁", "summary": "要旨", "lon": 139.7,
                  "lat": 35.6, "area": "NA-000002", "links": [{"type": "event", "id": "EV-1"}]},
                 {"title": "報道", "time": "2026-11-20T10:30:00", "source": "〇〇新聞"})
    assert ri.apply_import(client, s, first, k).added == 2
    # The same file again: nothing changes, nothing is added.
    plan = ri.plan_import(client, s, first, k)
    assert [p.action for p in plan.rows] == [ri.SAME, ri.DUPLICATE] and plan.rows[1].was.title == "報道"
    assert "同じ題名・日時・出典の参考情報がある（REF-" in plan.rows[1].text
    # A duplicate: the same title (width and case aside), minute and source; another source is another info.
    plan = ri.plan_import(client, s, file({"title": "報道 ", "time": "2026-11-20T10:30:40", "source": "〇〇新聞"},
                                          {"title": "報道", "time": "2026-11-20T10:30", "source": "△△新聞"},
                                          {"title": "報道", "time": "2026-11-20T10:30", "source": "△△新聞"}), k)
    assert [p.action for p in plan.rows] == [ri.DUPLICATE, ri.NEW, ri.DUPLICATE]
    # An update: only the keys written; "" and null clear; a new link added, the one there not twice.
    second = file({"id": "NAV-1", "title": "射撃訓練（延期）", "time": "2026-11-28T09:00", "summary": "", "lon": None, "lat": None,
                   "links": [{"type": "event", "id": "EV-1"}, {"type": "thing", "id": "OBJ-1", "label": "あおば"}]})
    plan = ri.plan_import(client, s, second, k)
    p = plan.rows[0]
    assert p.action == ri.UPDATE and p.changes == ["題名", "要旨", "地点", "紐付け +1"] and p.links == [LinkTarget("thing", "OBJ-1", "あおば")]
    result = ri.apply_import(client, s, second, k)
    assert (result.added, result.updated, result.links) == (0, 1, 1)
    got = load_infos(client, s, ids=["NAV-1"])[0]
    assert (got.title, got.summary, got.source, got.area_id, got.has_place) == ("射撃訓練（延期）", "", "海上保安庁", "NA-000002", False)
    assert len(infos.rows) == 2 and len(links.rows) == 2
    # A named area written without lon/lat: the point inside it; links only: an update that writes no info.
    plan = ri.plan_import(client, s, file({"id": "NAV-1", "title": "射撃訓練（延期）", "time": "2026-11-28T09:00", "area": "東京湾"},
                                          {"id": "NAV-1x", "title": "射撃訓練（延期）", "time": "2026-11-28T09:00", "source": "海上保安庁"}), k)
    assert plan.rows[0].changes == ["地名", "地点"] and in_rings(BAY, plan.rows[0].info.lon, plan.rows[0].info.lat)
    assert plan.rows[1].action == ri.NEW and plan.rows[1].warnings == ["同じ題名・日時・出典の参考情報がある（NAV-1）"]
    only_links = file({"id": "NAV-1", "title": "射撃訓練（延期）", "time": "2026-11-28T09:00", "links": [{"type": "observation", "id": "OBS-9"}]})
    p = ri.plan_import(client, s, only_links, k).rows[0]
    assert p.action == ri.UPDATE and not p.write and p.changes == ["紐付け +1"]
    result = ri.apply_import(client, s, only_links, k)
    assert (result.updated, result.links, [x.id for x in result.saved]) == (0, 1, ["NAV-1"])


def test_without_the_link_table_or_the_named_areas_and_with_shorter_attributes():
    client, s, infos, _links = setup(links=False, lengths={**LENGTHS, "TITLE": 20, "SUMMARY": 5})
    data = file({"title": "紐付けつき", "time": "2026-11-20", "area": "NA-123", "links": [{"type": "event", "id": "EV-1"}]},
                {"title": "長い要旨", "time": "2026-11-20", "summary": "123456"},
                {"title": "あ" * 21, "time": "2026-11-20"})
    plan = ri.plan_import(client, s, data, ri.Known())
    assert plan.notes == ["紐付けは使えない設定なので、links は取り込まない（管理者に連絡: referenceInfo.links）",
                          "地名のレイヤーがないので、area は確かめずに管理番号として保存する"]
    assert plan.rows[0].action == ri.NEW and plan.rows[0].info.area_id == "NA-123" and plan.rows[0].links == []
    assert plan.rows[0].warnings == ["紐付けは使えない設定なので links は飛ばす"]
    # Longer than the layer's attribute: an error, never cut.
    assert plan.rows[1].problems == ["要旨が 5 文字を超える（6 文字）"] and plan.rows[2].problems == ["題名が 20 文字を超える（21 文字）"]
    result = ri.apply_import(client, s, data, ri.Known())
    assert (result.added, result.links) == (1, 0) and len(infos.rows) == 1


def test_a_row_the_server_refuses_is_told_and_the_others_stay(monkeypatch):
    client, s, infos, links = setup()
    data = file({"id": "A", "title": "通る", "time": "2026-11-20", "links": [{"type": "event", "id": "EV-1"}]},
                {"id": "B", "title": "通らない", "time": "2026-11-20", "links": [{"type": "event", "id": "EV-2"}]})
    real = client.apply_edits

    def refusing(url, adds=(), updates=(), deletes=(), token=None):
        answer = real(url, adds, updates, deletes, token)
        if url == INFOS:
            answer.add_results[1] = {"success": False, "error": {"description": "拒否"}}
        return answer

    monkeypatch.setattr(client, "apply_edits", refusing)
    result = ri.apply_import(client, s, data, ri.Known())
    assert (result.added, result.failures, result.links) == (1, [(2, "拒否")], 1)
    assert [x.item_id for x in load_links(client, s, ["A", "B"])] == ["EV-1"]
    assert "保存できず 1" in result.summary()


def test_add_links_still_links_every_info_to_every_item():
    client, s, _infos, links = setup()
    assert add_links(client, s, ["A", "B"], [LinkTarget("event", "E1"), LinkTarget("thing", "T1")]) == (4, 0)
    assert add_links(client, s, ["A"], [LinkTarget("event", "E1")]) == (0, 1)
    assert len(links.rows) == 4


def test_find_area():
    assert ri.find_area("na-000002", AREAS)[0].name == "相模湾"
    assert ri.find_area("ＥＤＯ湾", AREAS)[0] is None
    assert ri.find_area("江戸湾", AREAS)[0].id == "NA-000001"
    inactive = [OrgArea(oid=5, id="NA-5", name="大島", active=False), OrgArea(oid=6, id="NA-6", name="大島")]
    assert ri.find_area("大島", inactive)[0].id == "NA-6"
