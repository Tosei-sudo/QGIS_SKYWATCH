"""Events (イベント): the settings, the event layer and the link table (many to many), export."""
from satellite_observation.core import edit_stamp
from satellite_observation.core.config import parse_config
from satellite_observation.core.esri import EsriClient, EsriError
from satellite_observation.core.events import (
    CLOSED, OPEN, LinkItem, add_links, counts, delete_event, events_of_item, export_csv, load_events, load_links, matches,
    new_event, remove_links, save_event, set_link_note, set_status, summary_text,
)
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer

import pytest

EVENTS = "https://x/arcgis/rest/services/Events/FeatureServer/0"
LINKS = "https://x/arcgis/rest/services/Events/FeatureServer/1"
S, T = "esriFieldTypeString", "esriFieldTypeDate"


def setup():
    fake = FakeEsri()
    events = fake.add_layer(EVENTS, FakeLayer({"EVENT_ID": S, "EVENT_NAME": S, "EVENT_KIND": S, "STATUS": S, "START_AT": T, "END_AT": T,
                                               "NOTE": S, "CREATED_USER": S, "CREATED_DATE": T, "UPDATED_USER": S, "UPDATED_DATE": T},
                                              "esriGeometryPolygon"))
    links = fake.add_layer(LINKS, FakeLayer({"EVENT_ID": S, "ITEM_TYPE": S, "ITEM_ID": S, "ITEM_LABEL": S, "ITEM_TIME": T, "SOURCE": S,
                                             "NOTE": S, "CREATED_USER": S, "CREATED_DATE": T}, ""))
    config, problems = parse_config({"events": {"layer": EVENTS, "links": {"url": LINKS}}})
    assert problems == []
    return EsriClient(fake), config.events, events, links


def test_settings_need_both_editable_layers():
    _c, problems = parse_config({"events": {"layer": EVENTS}})
    assert _c.events is None and any("events.links" in p for p in problems)
    c, problems = parse_config({"events": {"layer": {"url": EVENTS, "fields": {"name": "TITLE", "bogus": "X"}}, "links": LINKS, "kinds": []}})
    assert c.events.layer.fields["name"] == "TITLE" and c.events.links.fields["item"] == "ITEM_ID"
    assert any("bogus" in p for p in problems) and any("events.kinds" in p for p in problems)
    c, problems = parse_config({"events": {"layer": "https://x/arcgis/rest/services/Events/MapServer/0", "links": LINKS}})
    assert c.events is None and problems


def test_events_are_saved_closed_reopened_and_deleted_with_their_links(monkeypatch):
    monkeypatch.setenv("USERNAME", "佐藤")
    client, s, events, links = setup()
    ring = [[140.0, 37.0], [140.0, 38.0], [141.0, 38.0], [141.0, 37.0], [140.0, 37.0]]
    e = save_event(client, s, new_event("〇〇沖地震", kind="地震", start=1_700_000_000_000, note="津波注意報あり", rings=[ring]))
    assert e.oid == 1 and events.rows[0]["attributes"]["EVENT_NAME"] == "〇〇沖地震"
    assert events.rows[0]["attributes"]["CREATED_USER"] == "佐藤" and events.rows[0]["geometry"]["rings"] == [ring]
    save_event(client, s, new_event("豪雨", kind="風水害", start=1_710_000_000_000))
    listed = load_events(client, s)
    assert [x.name for x in listed] == ["豪雨", "〇〇沖地震"] and listed[1].rings == [ring] and listed[1].created_by == "佐藤"
    quake = listed[1]
    closed = set_status(client, s, quake, CLOSED, now=1_700_500_000_000)
    assert closed.end == 1_700_500_000_000 and events.rows[0]["attributes"]["STATUS"] == CLOSED
    again = set_status(client, s, closed, OPEN)
    assert again.end == 1_700_500_000_000 and load_events(client, s)[1].open
    # The area taken away: an empty polygon.
    save_event(client, s, type(again)(**{**again.__dict__, "rings": None}))
    assert events.rows[0]["geometry"]["rings"] == []
    add_links(client, s, [quake.id], [LinkItem("image", "IMG1", "IMG1 WV03", 1_700_100_000_000, "https://cat")])
    assert delete_event(client, s, quake) == 1
    assert [r["attributes"]["EVENT_NAME"] for r in events.rows] == ["豪雨"] and links.rows == []


def test_items_link_many_to_many_once_each():
    client, s, _events, links = setup()
    a = save_event(client, s, new_event("地震A"))
    b = save_event(client, s, new_event("地震B"))
    items = [LinkItem("image", "IMG1", "IMG1", 1, "cat"), LinkItem("observation", "OBS1", "船A", 2, "local"),
             LinkItem("request", "REQ1", "依頼", None, "req")]
    assert add_links(client, s, [a.id], items) == (3, 0)
    # Again, and into the second event too: the first ones are left as they are.
    assert add_links(client, s, [a.id, b.id], items[:2]) == (2, 2)
    got = load_links(client, s)
    assert len(got) == 5 and counts(got)[a.id] == {"image": 1, "observation": 1, "request": 1}
    assert counts(got)[b.id] == {"image": 1, "observation": 1, "request": 0}
    assert sorted(events_of_item(got, "image", "IMG1")) == sorted([a.id, b.id])
    assert [x.type for x in load_links(client, s, [b.id])] == ["image", "observation"]
    assert set_link_note(client, s, [x for x in got if x.event_id == b.id], "被害確認用") == 2
    assert {x.note for x in load_links(client, s, [b.id])} == {"被害確認用"}
    assert remove_links(client, s, [x for x in got if x.event_id == a.id and x.type == "request"]) == 1
    assert len(load_links(client, s)) == 4
    with pytest.raises(ValueError):
        add_links(client, s, [a.id], [LinkItem("alert", "X")])


def test_a_missing_attribute_is_said():
    fake = FakeEsri()
    fake.add_layer(EVENTS, FakeLayer({"NAME": S}, ""))
    fake.add_layer(LINKS, FakeLayer({"EVENT_ID": S}, ""))
    config, _p = parse_config({"events": {"layer": EVENTS, "links": LINKS}})
    with pytest.raises(EsriError, match="EVENT_ID"):
        load_events(EsriClient(fake), config.events)
    with pytest.raises(EsriError, match="ITEM_TYPE"):
        load_links(EsriClient(fake), config.events)


def test_search_export_and_summary():
    e = new_event("〇〇沖地震", kind="地震", start=1_700_000_000_000, note="沿岸部を優先")
    assert matches(e, "沖 地震") and matches(e, "沿岸") and not matches(e, "豪雨") and not matches(e, "", CLOSED)
    client, s, _ev, _links = setup()
    saved = save_event(client, s, e)
    add_links(client, s, [saved.id], [LinkItem("observation", "OBS1", "=HYPERLINK(\"x\")", None, "local"),
                                      LinkItem("image", "IMG1", "IMG1", 1_700_000_100_000, "cat")])
    links = load_links(client, s)
    text = export_csv(saved, links)
    lines = text.splitlines()
    assert lines[0].startswith("イベント,種別,状態,項目の種類") and lines[1].split(",")[3] == "画像"
    # A cell Excel would run as a formula is written as text.
    assert "'=HYPERLINK" in lines[2]
    summary = summary_text(saved, links)
    assert summary.splitlines()[0] == "【〇〇沖地震】（地震） 対応中" and "画像 1 件　観測記録 1 件　撮像依頼 0 件" in summary


def teardown_function(_f):
    edit_stamp.set_tracking(edit_stamp.DEFAULT_EDIT_TRACKING)
