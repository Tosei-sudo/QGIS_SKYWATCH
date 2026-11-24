"""
出典つきの参考情報 (tsudo, 2026-11-24: 「4,5,7を実装して」の 7): the settings,
the info point layer (saved, listed by period, ids, named areas and
footprint), the link table (one row per pair), who may delete, and the infos
an image is shown (within `days`, in its footprint or about a named area it shows).
"""
import pytest

from mikage.core.config import parse_config
from mikage.core.esri import EsriClient, EsriError
from mikage.core.reference_info import (
    DAYS, EVENT, OBSERVATION, THING, LinkTarget, ReferenceInfo, add_links, can_delete, delete_info, detail_lines, load_infos,
    load_links, matches, near_image, new_id, remove_links, save_info, source_text,
)
from mikage.tests.fake_esri import FakeEsri, FakeLayer

INFOS = "https://x/arcgis/rest/services/Events/FeatureServer/2"
LINKS = "https://x/arcgis/rest/services/Events/FeatureServer/3"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
DAY = 86_400_000
NOW = 1_764_000_000_000
INFO_TYPES = {"INFO_ID": S, "TITLE": S, "INFO_TIME": T, "PUBLISHED_AT": T, "SOURCE": S, "SOURCE_REF": S, "RELIABILITY": S, "SUMMARY": S,
              "KIND": S, "NAMED_AREA_ID": S, "NOTE": S, "CREATED_USER": S, "CREATED_DATE": T, "UPDATED_USER": S, "UPDATED_DATE": T}


def setup(links=True):
    fake = FakeEsri()
    infos = fake.add_layer(INFOS, FakeLayer(dict(INFO_TYPES), "esriGeometryPoint"))
    table = fake.add_layer(LINKS, FakeLayer({"INFO_ID": S, "ITEM_TYPE": S, "ITEM_ID": S, "LABEL": S, "CREATED_USER": S, "CREATED_DATE": T}, ""))
    config, problems = parse_config({"referenceInfo": {"layer": INFOS, **({"links": LINKS} if links else {})}})
    assert problems == []
    return EsriClient(fake), config.reference_info, infos, table


def info(title, t, lon=None, lat=None, **values):
    return ReferenceInfo(id=new_id(t), title=title, time=t, lon=lon, lat=lat, **values)


def test_settings():
    c, problems = parse_config({"referenceInfo": INFOS})
    assert problems == [] and c.reference_info.layer.url == INFOS and c.reference_info.links is None and c.reference_info.days == DAYS
    c, problems = parse_config({"referenceInfo": {"layer": {"url": INFOS, "fields": {"title": "HEADLINE"}}, "links": LINKS, "days": 3}})
    assert problems == [] and c.reference_info.layer.fields["title"] == "HEADLINE" and c.reference_info.layer.fields["time"] == "INFO_TIME"
    assert c.reference_info.links.fields["item"] == "ITEM_ID" and c.reference_info.days == 3
    c, problems = parse_config({"referenceInfo": {"layer": INFOS, "days": 0, "bogus": 1}})
    assert c.reference_info.days == DAYS and any("referenceInfo.days" in p for p in problems) and any("bogus" in p for p in problems)
    c, problems = parse_config({"referenceInfo": {"layer": "https://x/arcgis/rest/services/Events/MapServer/2"}})
    assert c.reference_info is None and problems
    assert new_id(NOW).startswith("REF-20251125-") and len(new_id(NOW)) == len("REF-20251125-010000-1a2b")


def test_infos_are_saved_listed_by_period_and_edited(monkeypatch):
    monkeypatch.setenv("USERNAME", "佐藤")
    client, s, infos, _links = setup()
    a = save_info(client, s, info("〇〇港でクレーン設置", NOW - 2 * DAY, 139.7, 35.6, source="〇〇新聞", source_ref="https://news.example/1",
                                  reliability="推定", summary="大型クレーン 2 基", kind="構造物 > クレーン", area_id="NA-000101"))
    assert a.oid == 1
    row = infos.rows[0]
    assert row["attributes"]["TITLE"] == "〇〇港でクレーン設置" and row["attributes"]["CREATED_USER"] == "佐藤"
    assert row["geometry"]["x"] == 139.7 and row["attributes"]["NAMED_AREA_ID"] == "NA-000101"
    save_info(client, s, info("古い発表", NOW - 60 * DAY, 139.0, 35.0, source="△△部"))
    save_info(client, s, info("場所の分からない報道", NOW - DAY))
    listed = load_infos(client, s)
    assert [x.title for x in listed] == ["場所の分からない報道", "〇〇港でクレーン設置", "古い発表"]
    got = listed[1]
    assert (got.lon, got.lat, got.created_by, got.url, got.reliability) == (139.7, 35.6, "佐藤", "https://news.example/1", "推定")
    assert [x.title for x in load_infos(client, s, since=NOW - 30 * DAY)] == ["場所の分からない報道", "〇〇港でクレーン設置"]
    assert [x.title for x in load_infos(client, s, ids=[got.id])] == ["〇〇港でクレーン設置"]
    assert [x.title for x in load_infos(client, s, area_ids=["NA-000101"])] == ["〇〇港でクレーン設置"]
    # Placed in a footprint: the point inside (an info without a place is in none).
    box = [[[139.6, 35.5], [139.8, 35.5], [139.8, 35.7], [139.6, 35.7], [139.6, 35.5]]]
    assert [x.title for x in load_infos(client, s, footprint=box)] == ["〇〇港でクレーン設置"]
    # Edited: the same row (its id and creator kept), the place taken away.
    edited = save_info(client, s, ReferenceInfo(**{**got.__dict__, "title": "〇〇港でクレーン 2 基", "lon": None, "lat": None}))
    assert edited.oid == got.oid and len(infos.rows) == 3
    again = load_infos(client, s, ids=[got.id])[0]
    assert again.title == "〇〇港でクレーン 2 基" and not again.has_place and again.created_by == "佐藤"
    # Saved again without its oid (an answer lost on the way): updated, not added twice.
    save_info(client, s, ReferenceInfo(**{**again.__dict__, "oid": None, "note": "再送"}))
    assert len(infos.rows) == 3 and load_infos(client, s, ids=[got.id])[0].note == "再送"
    with pytest.raises(ValueError):
        save_info(client, s, info("  ", NOW))


def test_a_layer_without_its_attributes_is_refused():
    fake = FakeEsri()
    fake.add_layer(INFOS, FakeLayer({"INFO_ID": S, "TITLE": S}, "esriGeometryPoint"))
    config, _p = parse_config({"referenceInfo": INFOS})
    with pytest.raises(EsriError) as e:
        load_infos(EsriClient(fake), config.reference_info)
    assert "INFO_TIME" in str(e.value)


def test_links_stay_one_row_per_pair_and_go_with_their_info():
    client, s, infos, links = setup()
    a = save_info(client, s, info("A", NOW, 139.7, 35.6))
    b = save_info(client, s, info("B", NOW, 139.7, 35.6))
    event = LinkTarget(EVENT, "EV-1", "〇〇沖地震")
    thing = LinkTarget(THING, "OBJ-1", "船A / 貨物船")
    record = LinkTarget(OBSERVATION, "OBS-1", "船A / 貨物船 / 航行")
    assert add_links(client, s, [a.id], [event, thing]) == (2, 0)
    assert add_links(client, s, [a.id, b.id], [event, record]) == (3, 1)
    assert len(links.rows) == 5
    assert {(x.info_id, x.type, x.item_id) for x in load_links(client, s, [a.id])} == {
        (a.id, EVENT, "EV-1"), (a.id, THING, "OBJ-1"), (a.id, OBSERVATION, "OBS-1")}
    assert {x.info_id for x in load_links(client, s, items=[(EVENT, "EV-1")])} == {a.id, b.id}
    assert {x.info_id for x in load_links(client, s, items=[(THING, "OBJ-1"), (OBSERVATION, "OBS-1")])} == {a.id, b.id}
    assert load_links(client, s, items=[("image", "X")]) == []
    assert links.rows[0]["attributes"]["LABEL"] == "〇〇沖地震"
    # Taken away: the row only.
    assert remove_links(client, s, [x for x in load_links(client, s, [b.id]) if x.type == EVENT]) == 1
    assert [x.type for x in load_links(client, s, [b.id])] == [OBSERVATION]
    # The info deleted: its links with it (the other's stay).
    assert delete_info(client, s, a) == 3
    assert [x.id for x in load_infos(client, s)] == [b.id] and len(links.rows) == 1


def test_without_the_link_table():
    client, s, infos, _links = setup(links=False)
    a = save_info(client, s, info("A", NOW))
    assert load_links(client, s, [a.id]) == [] and delete_info(client, s, a) == 0 and infos.rows == []
    with pytest.raises(EsriError):
        add_links(client, s, [a.id], [LinkTarget(EVENT, "EV-1")])


def test_who_may_delete():
    mine = ReferenceInfo(id="R", title="A", created_by="Sato")
    assert can_delete(mine, "sato", False) and not can_delete(mine, "tanaka", False) and can_delete(mine, "tanaka", True)
    assert can_delete(ReferenceInfo(id="R", title="A"), "tanaka", False)  # the creator not known


def test_an_image_is_shown_the_infos_of_its_days_footprint_and_named_areas():
    client, s, _infos, _links = setup()
    image_time = NOW
    inside = save_info(client, s, info("範囲内・3 日前", NOW - 3 * DAY, 139.7, 35.6))
    save_info(client, s, info("範囲内・10 日前", NOW - 10 * DAY, 139.7, 35.6))
    named = save_info(client, s, info("地名の情報・範囲外", NOW + 2 * DAY, 139.0, 35.0, area_id="NA-000001"))
    save_info(client, s, info("範囲外", NOW, 139.0, 35.0))
    footprint = [[[139.6, 35.5], [139.8, 35.5], [139.8, 35.7], [139.6, 35.7], [139.6, 35.5]]]
    found = near_image(client, s, image_time, footprint, ["NA-000001"])
    assert [x.id for x in found] == [named.id, inside.id]
    assert [x.id for x in near_image(client, s, image_time, footprint)] == [inside.id]
    s.days = 14
    assert len(near_image(client, s, image_time, footprint)) == 2


def test_texts():
    x = ReferenceInfo(id="R", title="〇〇港でクレーン設置", time=NOW, source="〇〇新聞", source_ref="朝刊 3 面", reliability="推定",
                      summary="大型クレーン 2 基", kind="構造物", area_id="NA-1")
    assert source_text(x) == "〇〇新聞（朝刊 3 面）"
    assert source_text(ReferenceInfo(id="R", title="A", source="〇〇新聞", source_ref="https://n.example/1")) == "〇〇新聞"
    lines = detail_lines(x, "横須賀港")
    assert lines[0] == "【〇〇港でクレーン設置】 推定" and "地名: 横須賀港" in lines and "大型クレーン 2 基" in lines
    assert matches(x, "クレーン 新聞") and not matches(x, "飛行機")
    assert not matches(x, box=(139, 35, 140, 36))  # no place: not in the map's view
    assert matches(ReferenceInfo(id="R", title="A", lon=139.5, lat=35.5), box=(139, 35, 140, 36))
