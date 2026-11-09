"""
画像カタログの期間を時刻まで（撮像日時・登録日時・システム登録日時）: the
boxes read a day or a day and a time, each field type is asked for it, text
dates are written as the field writes them, and a source's `timeZone` is
kept. Times are local (as the list shows them), so the expectations are
made from local clocks and hold in any zone the tests run in.
"""
from datetime import datetime, timedelta, timezone

import pytest

from skywatch.core.catalog import catalog_of
from skywatch.core.catalog.query import EMPTY_SEARCH, day_range, has_time, local_day, local_time, timed_text_fields, where_of
from skywatch.core.catalog.search import read_catalog_layer, search_catalog
from skywatch.core.esri import EsriClient
from skywatch.core.time_query import between_condition, text_at
from skywatch.core.time_zone import clock_of, instant_of, parse_time_zone, set_rules, source_rule
from skywatch.core.util import read_clock, read_day_time, utc_text
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://gis.example.com/arcgis/rest/services/Catalog/FeatureServer/0"
TOKYO = parse_time_zone("Asia/Tokyo")


def local(*args) -> int:
    """A local clock time as ms."""
    return int(datetime(*args).timestamp() * 1000)


def tokyo(*args) -> int:
    """A clock time in Tokyo as the instant (ms)."""
    clock = datetime(*args, tzinfo=timezone.utc).timestamp() * 1000
    return int(instant_of(clock, TOKYO))


def typed(instant: float) -> str:
    """An instant as it is typed in the box (local clock)."""
    return datetime.fromtimestamp(instant / 1000).strftime("%Y-%m-%d %H:%M")


@pytest.fixture(autouse=True)
def no_rules():
    set_rules([])
    yield
    set_rules([])


# ---- Reading the boxes

def test_a_day_or_a_day_and_a_time_is_read_in_many_ways():
    for text in ("2026-11-09", "2026/11/9", "20261109", "２０２６年１１月９日"):
        assert read_day_time(text) == "2026-11-09", text
    for text in ("2026-11-09 10:30", "2026/11/9 10:30", "2026-11-09T10:30", "20261109 1030", "2026年11月9日10時30分",
                 "2026年11月9日 10時30分", "２０２６／１１／９　１０：３０", " 2026-11-09  10:30 "):
        assert read_day_time(text) == "2026-11-09 10:30", text
    # Seconds when typed, the hour alone with 時.
    assert read_day_time("2026-11-09 9:05:07") == "2026-11-09 09:05:07"
    assert read_day_time("2026-11-09 10時") == "2026-11-09 10:00"
    assert read_day_time("2026/11/09 10:23") == "2026-11-09 10:23"  # as the list shows it, copied
    assert read_day_time("") == read_day_time("   ") == ""
    # A time alone is on the day given (the end of a period, on its start's day); without one it is not a day.
    assert read_day_time("12:00", "2026-11-09") == "2026-11-09 12:00"
    assert read_day_time("１２時", "2026-11-09") == "2026-11-09 12:00"
    for text in ("12:00", "2026-11-0910:30", "2026-11-09 24:00", "2026-11-09 10:60", "2026-11-09 10:30:61", "2026-02-30 10:00",
                 "2026-11-09 朝", "2026-11-09 10:30Z", "十時"):
        with pytest.raises(ValueError):
            read_day_time(text)
    assert read_clock("0930") == "09:30" and read_clock("093015") == "09:30:15" and read_clock("") == ""


def test_a_bound_is_its_start_or_the_instant_after_it_as_far_as_it_is_written():
    assert local_time("2026-11-09") == local_day("2026-11-09")
    assert local_time("2026-11-09", end=True) == local_day("2026-11-10")
    assert local_time("2026-11-09 10:30") == local(2026, 11, 9, 10, 30)
    # 〜 12:00 takes in 12:00:59; 〜 12:00:15 takes in 12:00:15.
    assert local_time("2026-11-09 12:00", end=True) == local(2026, 11, 9, 12, 1)
    assert local_time("2026-11-09 12:00:15", end=True) == local(2026, 11, 9, 12, 0, 16)
    assert local_time("2026-11-09 23:59", end=True) == local(2026, 11, 10)
    assert has_time("2026-11-09 10:30") and not has_time("2026-11-09") and not has_time("")


# ---- The conditions

def test_times_are_written_for_the_field_type():
    a, b = local(2026, 11, 9, 10, 30), local(2026, 11, 9, 12, 1)
    assert day_range("T", "esriFieldTypeDate", "2026-11-09 10:30", "2026-11-09 12:00") == \
        [f"T >= TIMESTAMP '{utc_text(a)}'", f"T < TIMESTAMP '{utc_text(b)}'"]
    assert day_range("T", "esriFieldTypeTimestampOffset", "", "2026-11-09 12:00") == [f"T < TIMESTAMP '{utc_text(b)}'"]
    assert day_range("T", "esriFieldTypeDouble", "2026-11-09 10:30", "2026-11-09 12:00") == [f"T >= {a}", f"T < {b}"]
    assert day_range("T", "esriFieldTypeInteger", "2026-11-09 10:30", "") == [f"T >= {a}"]
    # A date alone has no time: the days the period touches.
    assert day_range("T", "esriFieldTypeDateOnly", "2026-11-09 10:30", "2026-11-10 01:00") == ["T >= DATE '2026-11-09'", "T < DATE '2026-11-11'"]
    # Days alone are asked as before.
    assert day_range("T", "esriFieldTypeDate", "2026-11-09", "2026-11-09") == \
        [f"T >= TIMESTAMP '{utc_text(local_day('2026-11-09'))}'", f"T < TIMESTAMP '{utc_text(local_day('2026-11-10'))}'"]
    assert day_range("T", "esriFieldTypeString", "2026-11-09", "2026-11-09") == ["T >= '2026-11-09'", "T < '2026-11-10'"]


def test_text_times_are_written_as_the_field_writes_them():
    a, b = local(2026, 11, 9, 10, 30), local(2026, 11, 9, 12, 1)

    def utc_clock(t: float, sep: str, seconds: bool = True) -> str:
        return datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime(f"%Y-%m-%d{sep}%H:%M" + (":%S" if seconds else ""))

    period = ("2026-11-09 10:30", "2026-11-09 12:00")
    # No value read: ISO with T, in UTC (as text without a zone is read).
    assert day_range("T", "esriFieldTypeString", *period) == [f"T >= '{utc_clock(a, 'T')}'", f"T < '{utc_clock(b, 'T')}'"]
    # With a Z, or a space: the same clock, the field's separator, no suffix (a value's Z sorts after it).
    assert day_range("T", "esriFieldTypeString", *period, "2026-11-01T00:00:00.5Z") == [f"T >= '{utc_clock(a, 'T')}'", f"T < '{utc_clock(b, 'T')}'"]
    assert day_range("T", "esriFieldTypeString", *period, "2026-11-01 00:00:00") == [f"T >= '{utc_clock(a, ' ')}'", f"T < '{utc_clock(b, ' ')}'"]
    # Values without seconds: none written either (10:30 is not before 10:30).
    assert day_range("T", "esriFieldTypeString", *period, "2026-11-01 00:00") == \
        [f"T >= '{utc_clock(a, ' ', False)}'", f"T < '{utc_clock(b, ' ', False)}'"]
    # An offset on the values: their clock.
    nine = datetime.fromtimestamp(a / 1000, tz=timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M:%S")
    assert day_range("T", "esriFieldTypeString", period[0], "", "2026-11-01T09:00:00+09:00") == [f"T >= '{nine}'"]
    # The source's timeZone over everything.
    assert day_range("T", "esriFieldTypeString", period[0], "", "2026-11-01T00:00:00Z", TOKYO) == [f"T >= '{nine}'"]
    # Seconds kept when written.
    assert text_at(local(2026, 11, 9, 10, 30, 15), "2026-11-01 00:00") == utc_clock(local(2026, 11, 9, 10, 30, 15), " ")


def test_the_follow_up_condition_is_unchanged_for_values_with_seconds():
    first, last = tokyo(2026, 11, 1), tokyo(2026, 11, 1, 23, 59, 59, 999)
    assert between_condition("ACQ", "esriFieldTypeString", first, last, "2026-11-01 09:00:00", TOKYO) == \
        "ACQ >= '2026-11-01 00:00:00' AND ACQ < '2026-11-02 00:00:00'"
    # Without seconds, none written (it was `00:00:00`, after `2026-11-01 00:00`).
    assert between_condition("ACQ", "esriFieldTypeString", first, last, "2026-11-01 09:00", TOKYO) == \
        "ACQ >= '2026-11-01 00:00' AND ACQ < '2026-11-02 00:00'"


def test_where_of_takes_the_catalogs_zone_and_the_samples():
    catalog = catalog_of({"label": "画像", "url": URL, "fields": {"id": "ID", "acquired": "ACQ", "registered": "REG", "source": "URL"}}, [], 0)
    types = {"ACQ": "esriFieldTypeString", "REG": "esriFieldTypeDate"}
    at = tokyo(2026, 11, 9, 10, 30)
    search = EMPTY_SEARCH.with_(acquired_from=typed(at), registered_from=typed(at))
    assert timed_text_fields(catalog, types, search) == ["ACQ"]
    assert timed_text_fields(catalog, types, EMPTY_SEARCH.with_(acquired_from="2026-11-09")) == []
    set_rules([source_rule(URL, TOKYO)])
    assert where_of(catalog, types, search, {"ACQ": "2026-11-01 00:00:00"}) == \
        f"ACQ >= '2026-11-09 10:30:00' AND REG >= TIMESTAMP '{utc_text(at)}'"


# ---- Searches

def _catalog(types, rows, entry=None):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer(types, "esriGeometryPolygon"))
    for row in rows:
        layer.add(row, {"rings": [[[139, 35], [139, 36], [140, 36], [140, 35], [139, 35]]]})
    catalog = catalog_of({"label": "画像", "url": URL, "fields": {"id": "IMG_ID", "acquired": "ACQ", "source": "URL"}, **(entry or {})}, [], 0)
    return fake, catalog


def test_a_search_finds_the_images_of_the_minutes_typed():
    times = {"BEFORE": local(2026, 11, 9, 10, 29, 59), "START": local(2026, 11, 9, 10, 30), "IN": local(2026, 11, 9, 11, 15),
             "LAST": local(2026, 11, 9, 12, 0, 59), "AFTER": local(2026, 11, 9, 12, 1)}
    fake, catalog = _catalog({"IMG_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "URL": "esriFieldTypeString"},
                             [{"IMG_ID": k, "ACQ": v, "URL": f"https://x/{k}.tif"} for k, v in times.items()])
    client = EsriClient(fake)
    layer = read_catalog_layer(client, catalog, None)
    result = search_catalog(client, catalog, layer, EMPTY_SEARCH.with_(acquired_from="2026-11-09 10:30", acquired_to="2026-11-09 12:00"), None, None)
    assert sorted(r.id for r in result.records) == ["IN", "LAST", "START"] and result.total == 3
    # The day alone: all of them, as before.
    result = search_catalog(client, catalog, layer, EMPTY_SEARCH.with_(acquired_from="2026-11-09", acquired_to="2026-11-09"), None, None)
    assert result.total == 5
    # Date fields need no value read first.
    assert not any(f.get("resultRecordCount") == "1" for _, f in fake.asked)


def test_a_search_keeps_the_sources_time_zone_for_dates():
    # The service keeps Tokyo clocks as if UTC (its stored ms are 9 hours ahead of the instant).
    shown = {"MORNING": tokyo(2026, 11, 9, 9, 0), "NOON": tokyo(2026, 11, 9, 12, 0), "EVENING": tokyo(2026, 11, 9, 18, 0)}
    stored = {k: int(clock_of(v, TOKYO)) for k, v in shown.items()}
    fake, catalog = _catalog({"IMG_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "URL": "esriFieldTypeString"},
                             [{"IMG_ID": k, "ACQ": v, "URL": "https://x/a.tif"} for k, v in stored.items()])
    set_rules([source_rule(URL, TOKYO)])
    client = EsriClient(fake)
    layer = read_catalog_layer(client, catalog, None)
    search = EMPTY_SEARCH.with_(acquired_from=typed(tokyo(2026, 11, 9, 11, 0)), acquired_to=typed(tokyo(2026, 11, 9, 13, 0)))
    result = search_catalog(client, catalog, layer, search, None, None)
    assert [r.id for r in result.records] == ["NOON"]
    # The record's time is the instant (what the list shows), the one searched for.
    assert result.records[0].acquired == shown["NOON"]


def test_a_search_writes_text_times_like_the_values_and_reads_one_once():
    rows = [{"IMG_ID": k, "ACQ": v, "URL": "https://x/a.tif"} for k, v in
            (("EARLY", "2026-11-09 08:00:00"), ("ON", "2026-11-09 10:30:00"), ("LATE", "2026-11-09 12:00:30"), ("NEXT", "2026-11-09 12:01:00"))]
    fake, catalog = _catalog({"IMG_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeString", "URL": "esriFieldTypeString"}, rows)
    set_rules([source_rule(URL, TOKYO)])
    client = EsriClient(fake)
    layer = read_catalog_layer(client, catalog, None)
    search = EMPTY_SEARCH.with_(acquired_from=typed(tokyo(2026, 11, 9, 10, 30)), acquired_to=typed(tokyo(2026, 11, 9, 12, 0)))
    result = search_catalog(client, catalog, layer, search, None, None)
    assert sorted(r.id for r in result.records) == ["LATE", "ON"]
    wheres = [f["where"] for u, f in fake.asked if u.endswith("/query") and "returnCountOnly" in f]
    assert wheres == ["ACQ >= '2026-11-09 10:30:00' AND ACQ < '2026-11-09 12:01:00'"]
    samples = [f for _, f in fake.asked if f.get("resultRecordCount") == "1"]
    assert len(samples) == 1 and layer.samples == {"ACQ": "2026-11-09 08:00:00"}
    # Asked again (「続きを読む」, another time): the value read is kept.
    search_catalog(client, catalog, layer, search.with_(acquired_to=""), None, None)
    assert len([f for _, f in fake.asked if f.get("resultRecordCount") == "1"]) == 1
