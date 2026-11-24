"""
地域の形に合わせる (組織の関心地域, tsudo 2026-11-24: 「ネームドエリアの形状が
更新されたら、撮像依頼も連動して更新されるようなパイプラインが欲しい」):
which linked requests take their area's new shape, the saving (one changed by
someone else first held back), the change log and its undo, the look before
「計算」 only when the area layer was edited, and the rows handed out with the
area before (出し直し候補, kept when adopted, 照合 by their own area).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

from mikage.core.esri import EsriClient
from mikage.core.named_areas import NamedAreaSettings, areas_by_id, layer_stamp
from mikage.core.obs.alerts import DEFAULT_PLACE_FIELDS
from mikage.core.sources import EsriSource
from mikage.core.request.area_follow import (
    CHANGED, HOLD, KEPT, REDO, SKIP, UNDO, UPDATE, Follow, apply_follows, follow_area, follow_areas, follows, load_changes,
    redo_count, redo_mark, redo_text, undo_change,
)
from mikage.core.request.model import ImagingRequest
from mikage.core.request.store import (
    DEFAULT_AREA_CHANGE_FIELDS, DEFAULT_REQUEST_FIELDS, HistoryRow, RequestStoreConfig, load_requests, request_store_of, save_request,
)
from mikage.core.request.workflow import keep_answers
from mikage.tests.fake_esri import FakeEsri, FakeLayer

PLACES = "https://example.com/arcgis/rest/services/Alerts/FeatureServer/3"
LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"
CHANGES = "https://example.com/arcgis/rest/services/Req/FeatureServer/34"
S = "esriFieldTypeString"
RING = [[139.65, 35.55], [139.65, 35.60], [139.70, 35.60], [139.70, 35.55], [139.65, 35.55]]
#: The area made bigger (about 900 m east).
BIGGER = [[139.65, 35.55], [139.65, 35.60], [139.71, 35.60], [139.71, 35.55], [139.65, 35.55]]
NOW = 1_795_000_000_000.0


@dataclass
class Area:
    id: str
    name: str
    rings: Optional[list]
    active: bool = True


def req(rid="r1", status="有効", follow=True, area="NA-000101", rings=None, **kw) -> ImagingRequest:
    return ImagingRequest(id=rid, name=f"依頼{rid}", status=status, named_area_id=area, area_follow=follow and bool(area),
                          rings=[list(map(list, rings or RING))], **kw)


def test_only_the_linked_requests_following_an_area_whose_shape_moved_take_it():
    areas = {"NA-000101": [Area("NA-000101", "横須賀港", [BIGGER])]}
    out = {f.request.id: f for f in follows([
        req("a"),                                   # follows: 更新
        req("b", follow=False),                     # does not follow: shown only
        req("c", status="完了"),                     # 完了: left as it is
        req("d", status="停止"),                     # 停止 follows too
        req("e", rings=BIGGER),                     # already the area's shape
        req("f", area=""),                          # not made from an area
        req("g", area="na-000101"),                 # the 管理番号 in another case
    ], areas)}
    assert set(out) == {"a", "b", "d", "g"}
    assert (out["a"].action, out["a"].rings, out["a"].area_name) == (UPDATE, [BIGGER], "横須賀港")
    assert out["b"].action == CHANGED and "オフ" in out["b"].reason
    assert out["d"].action == UPDATE and out["g"].action == UPDATE


def test_a_stopped_gone_doubled_or_shapeless_area_is_not_followed_and_says_why():
    stopped = {"NA-000101": [Area("NA-000101", "横須賀港", [BIGGER], active=False)]}
    (f,) = follows([req()], stopped)
    assert f.action == SKIP and "使用停止" in f.reason
    # Not moved: nothing to say even when stopped.
    assert follows([req(rings=BIGGER)], stopped) == []
    (f,) = follows([req()], {}, {"NA-000101"})
    assert f.action == SKIP and "地名の表にない" in f.reason
    two = {"NA-000101": [Area("NA-000101", "横須賀港", [BIGGER]), Area("NA-000101", "横須賀港（旧）", [RING])]}
    (f,) = follows([req()], two)
    assert f.action == SKIP and "2 件" in f.reason
    (f,) = follows([req()], {"NA-000101": [Area("NA-000101", "横須賀港", [])]})
    assert f.action == SKIP and "形がない" in f.reason
    # Only the areas asked about.
    assert follows([req()], {}, set()) == []


# ---- with the services ----------------------------------------------------------------------------


def services(edited: Optional[float] = 1000.0):
    fake = FakeEsri()
    places = fake.add_layer(PLACES, FakeLayer({**{v: S for v in DEFAULT_PLACE_FIELDS.values()}, "ACTIVE": "esriFieldTypeSmallInteger"},
                                              "esriGeometryPolygon"))
    if edited is not None:
        places.extra["editingInfo"] = {"lastEditDate": edited}
    types = {n: S for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    types.update({n: "esriFieldTypeDouble" for n in ("PRIORITY", "BOOST", "CLOUD_NO_GO")})
    types[DEFAULT_REQUEST_FIELDS["areaFollow"]] = "esriFieldTypeSmallInteger"
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    changes = fake.add_layer(CHANGES, FakeLayer({**{v: S for v in DEFAULT_AREA_CHANGE_FIELDS.values()},
                                                 "CREATED_DATE": "esriFieldTypeDate", "CREATED_USER": S,
                                                 "UPDATED_DATE": "esriFieldTypeDate", "UPDATED_USER": S}, "esriGeometryPolygon"))
    store = RequestStoreConfig(label="撮像依頼", url=LEDGER, area_changes=CHANGES)
    settings = NamedAreaSettings(areas=EsriSource(url=PLACES, fields=dict(DEFAULT_PLACE_FIELDS)))
    return EsriClient(fake), store, settings, places, ledger, changes


def area(places, rings, area_id="NA-000101", name="横須賀港", active=1):
    return places.add({"NAME": name, "NAMED_AREA_ID": area_id, "AREA_USE": "関心地域", "ACTIVE": active}, {"rings": rings})


def test_the_areas_are_read_by_their_ids_with_the_layers_last_edit_time():
    client, _store, settings, places, _ledger, _changes = services(edited=1234.0)
    area(places, [RING])
    area(places, [RING], "NA-000102", "長浦")
    area(places, [RING], "na-000102", "長浦（重なり）")
    found = areas_by_id(client, settings, ["na-000101", "NA-000102", "NA-999"])
    assert sorted(found) == ["NA-000101", "NA-000102"] and len(found["NA-000102"]) == 2
    assert found["NA-000101"][0].rings == [RING]
    assert layer_stamp(client, settings) == 1234.0


def test_the_followers_take_the_new_shape_and_the_area_before_is_logged():
    client, store, settings, places, ledger, changes = services()
    area(places, [BIGGER])
    a = save_request(client, store, None, req("a"), NOW)
    save_request(client, store, None, req("b", follow=False), NOW)
    save_request(client, store, None, req("c", status="完了"), NOW)
    result = follow_area(client, store, None, settings, "na-000101", NOW + 1)
    assert [r.id for r in result.saved] == ["a"] and result.saved[0].rings == [BIGGER]
    assert [f.request.id for f in result.changed] == ["b"] and not result.held and not result.problems
    assert result.text() == "地域の形に合わせた依頼 1 件"
    after = {r.id: r for r in load_requests(client, store, None)}
    assert after["a"].rings == [BIGGER] and after["a"].area_follow
    assert after["b"].rings == [RING] and after["c"].rings == [RING]
    # The log: the request's area before, as a polygon.
    (row,) = changes.rows
    f = DEFAULT_AREA_CHANGE_FIELDS
    assert (row["attributes"][f["requestId"]], row["attributes"][f["action"]], row["attributes"][f["areaId"]]) == ("a", UPDATE, "NA-000101")
    assert row["geometry"]["rings"] == [RING]
    # Read back, newest first, with who and when.
    (c,) = load_changes(client, store, None)
    assert (c.request_id, c.name, c.action, c.rings, c.undoable) == ("a", a.name, UPDATE, [RING], True)
    assert c.by and c.at is not None
    # Again: nothing left to do.
    assert follow_area(client, store, None, settings, "NA-000101", NOW + 2).text() == ""


def test_a_stopped_area_saved_in_mikage_is_logged_as_not_followed():
    client, store, settings, places, _ledger, changes = services()
    area(places, [BIGGER], active=0)
    save_request(client, store, None, req("a"), NOW)
    result = follow_area(client, store, None, settings, "NA-000101", NOW + 1)
    assert not result.saved and [f.request.id for f in result.skipped] == ["a"]
    assert result.text() == "地域の形に合わせた依頼 0 件（対象外 1 件）"
    assert [r["attributes"]["CHANGE_ACTION"] for r in changes.rows] == [SKIP]


def test_one_changed_by_someone_else_first_is_held_and_tried_again():
    client, store, _settings, places, ledger, changes = services()
    area(places, [BIGGER])
    a = save_request(client, store, None, req("a"), NOW)
    # Someone else changed it after it was read.
    ledger.rows[0]["attributes"]["UPDATED_AT"] = NOW + 60_000
    todo = follows([a], {"NA-000101": [Area("NA-000101", "横須賀港", [BIGGER])]})
    result = apply_follows(client, store, None, todo, NOW + 120_000)
    assert not result.saved and [f.request.id for f, _why in result.held] == ["a"]
    assert "ほかの人が先に" in result.held[0][1] and result.text() == "地域の形に合わせた依頼 0 件（保留 1 件）"
    assert [r["attributes"]["CHANGE_ACTION"] for r in changes.rows] == [HOLD]
    assert load_requests(client, store, None)[0].rings == [RING]


def test_without_a_log_the_requests_still_follow():
    client, store, settings, places, _ledger, changes = services()
    store = replace(store, area_changes=None)
    area(places, [BIGGER])
    save_request(client, store, None, req("a"), NOW)
    result = follow_area(client, store, None, settings, "NA-000101", NOW + 1)
    assert len(result.saved) == 1 and result.changes and not changes.rows and not result.problems
    assert load_changes(client, store, None) == []


def test_an_update_undone_brings_the_area_before_back_and_stops_following():
    client, store, settings, places, _ledger, changes = services()
    area(places, [BIGGER])
    save_request(client, store, None, req("a"), NOW)
    follow_area(client, store, None, settings, "NA-000101", NOW + 1)
    (c,) = load_changes(client, store, None)
    now = load_requests(client, store, None)[0]
    saved, problems = undo_change(client, store, None, c, now, NOW + 2)
    assert not problems and saved.rings == [RING] and not saved.area_follow and saved.named_area_id == "NA-000101"
    back = load_requests(client, store, None)[0]
    assert back.rings == [RING] and not back.area_follow
    logged = load_changes(client, store, None, ["a"])
    assert {x.action for x in logged} == {UPDATE, UNDO}
    undo = next(x for x in logged if x.action == UNDO)
    assert undo.rings == [BIGGER] and not undo.undoable
    # No longer following: the next look leaves it (only marked).
    result = follow_area(client, store, None, settings, "NA-000101", NOW + 3)
    assert not result.saved and [f.action for f in result.changed] == [CHANGED]


def test_before_the_calculation_the_areas_are_looked_at_only_when_the_layer_was_edited():
    client, store, settings, places, _ledger, _changes = services(edited=1000.0)
    area(places, [BIGGER])
    a = save_request(client, store, None, req("a"), NOW)
    b = save_request(client, store, None, req("b", area="NA-000102"), NOW)
    other = ImagingRequest(id="x", name="ほか", rings=[RING])
    # Not edited since: nothing read, nothing changed.
    run = follow_areas(client, store, None, settings, [a, b, other], NOW + 1, since=1000.0, checked=NOW + 0.5)
    assert run.unchanged and run.result is None and run.stamp == 1000.0 and run.requests[0].rings == [RING]
    # Edited since: followed, the request replaced in the day's list.
    run = follow_areas(client, store, None, settings, [a, b, other], NOW + 1, since=900.0, checked=NOW + 0.5)
    assert not run.unchanged and run.stamp == 1000.0
    assert [r.id for r in run.requests] == ["a", "b", "x"] and run.requests[0].rings == [BIGGER]
    assert [f.request.id for f in run.result.skipped] == ["b"]  # NA-000102 is not in the layer
    # A request changed since the last look (follow ticked again, say) is looked at even when the layer was not edited.
    c = save_request(client, store, None, req("c"), NOW + 5)
    run = follow_areas(client, store, None, settings, [c], NOW + 6, since=1000.0, checked=NOW + 2)
    assert not run.unchanged and run.requests[0].rings == [BIGGER]
    # Nothing following: the layer not asked.
    assert follow_areas(client, store, None, settings, [other], NOW).stamp is None
    assert follow_areas(client, store, None, None, [a], NOW).result is None


def test_a_layer_not_telling_its_edits_is_always_looked_at():
    client, store, settings, places, _ledger, _changes = services(edited=None)
    area(places, [BIGGER])
    a = save_request(client, store, None, req("a"), NOW)
    run = follow_areas(client, store, None, settings, [a], NOW + 1, since=5000.0, checked=NOW + 0.5)
    assert run.stamp is None and run.requests[0].rings == [BIGGER]


# ---- what was handed out with the area before --------------------------------------------------------


def row(day="2026-11-25", adoption="", rings=None, **kw) -> HistoryRow:
    values = {"named_area_id": "NA-000101", "result": "採用", **kw}
    return HistoryRow(run_id="x", run_date=1.0, day=day, request_id="a", adoption=adoption, rings=[list(map(list, rings or RING))],
                      **values)


def test_rows_handed_out_with_the_area_before_are_marked():
    today = date(2026, 11, 24)
    assert redo_mark(row(), [BIGGER], today) == REDO
    assert redo_mark(row(adoption="不明"), [BIGGER], today) == REDO
    assert redo_mark(row(adoption="採用"), [BIGGER], today) == KEPT
    assert redo_mark(row(adoption="不採用"), [BIGGER], today) == ""
    assert redo_mark(row(day="2026-11-24"), [BIGGER], today) == REDO
    assert redo_mark(row(day="2026-11-23"), [BIGGER], today) == ""
    assert redo_mark(row(), [RING], today) == ""
    assert redo_mark(row(named_area_id=""), [BIGGER], today) == ""
    assert redo_mark(row(result="見送り"), [BIGGER], today) == ""
    assert redo_mark(row(part=2), [BIGGER], today) == ""
    assert redo_mark(row(), None, today) == ""
    assert "書き出し直す" in redo_text(REDO) and "採用済み" in redo_text(KEPT) and redo_text("") == ""
    rows = [row(), row(adoption="採用"), row(day="2026-11-01")]
    assert redo_count(rows, [ImagingRequest(id="a", name="", rings=[BIGGER])], today) == 1


def test_an_answer_for_the_area_before_is_not_carried_to_the_new_area():
    old = row(adoption="採用", object_id=5, system="SYS-A")
    new = replace(row(rings=BIGGER, system="SYS-A"), object_id=None)
    out = keep_answers([old], [new])
    assert out.carried == 0 and out.rows[0].adoption == "" and out.kept == 1 and out.gone == []
    # The same area: carried as before.
    out = keep_answers([old], [replace(new, rings=[RING])])
    assert out.carried == 1 and out.rows[0].adoption == "採用"


def test_the_settings_file_names_the_change_log():
    problems: list = []
    s = request_store_of({"url": LEDGER, "areaChanges": CHANGES, "areaChangeFields": {"reason": "WHY"}}, problems, 0)
    assert not problems and s.area_changes == CHANGES and s.area_change_fields["reason"] == "WHY"
    assert s.area_change_fields["action"] == "CHANGE_ACTION"
    problems = []
    s = request_store_of({"url": LEDGER, "areaChanges": "https://x/y"}, problems, 0)
    assert s.area_changes is None and any("areaChanges" in p for p in problems)
    assert request_store_of({"url": LEDGER}, [], 0).area_changes is None


def test_follow_records_hold_the_request_and_its_area():
    f = Follow(req("a"), "NA-000101", UPDATE, "x", [BIGGER], "横須賀港")
    assert f.request.id == "a" and f.rings == [BIGGER]
