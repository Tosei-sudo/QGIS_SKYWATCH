"""Imaging requests (撮像依頼台帳) and the day's files (日次出力): core/request."""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import replace
from datetime import date, datetime

import pytest

from satellite_observation.core.catalog import catalog_of
from satellite_observation.core.config import parse_config, parse_config_text
from satellite_observation.core.esri import EsriClient, EsriError, TooManyError
from satellite_observation.core.plan.names import SatelliteNames
from satellite_observation.core.plan.satellites import SatelliteSpec
from satellite_observation.core.plan.store import between_condition
from satellite_observation.core.time_zone import parse_time_zone
from satellite_observation.core.plan.weather import CloudSeries, key_of
from satellite_observation.core.request.daily import HELD, TAKEN, allocate, carries_over, gather, room_text
from satellite_observation.core.request.export import (
    Context, convert_priority, csv_text, file_name_of, format_time, geometry_value, json_text, render,
)
from satellite_observation.core.request.follow import (
    Finding, accepted_names, fitting_images, follow_and_save, follow_up, history_changes, in_days, marked, missed_ids, next_step,
    request_changes, save_marked, tally, undone_changes,
)
from satellite_observation.core.request.model import (
    JST, ImagingRequest, day_bounds, days_left, level_of, next_boost, occurs_on, priority_text, repeat_of, repeat_text,
    square_rings,
)
from satellite_observation.core.request.route import fitting, scenes_of, system_of_satellite, why_not
from satellite_observation.core.request.schema import validate
from satellite_observation.core.request.store import (
    DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, HistoryRow, add_history, count_history, delete_history, history_where, load_history,
    replaced_where, load_requests, request_store_of, save_request,
    update_history, update_requests,
)
from satellite_observation.core.request.systems import export_systems_of
from satellite_observation.core.request.weather import cloud_step, daytime_cloud, places_of, with_cloud
from satellite_observation.core.request.workflow import (
    boost_changes, clouds_of, forecast_places, history_of, missed_done, not_written, plan_day,
)
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer

DAY = date(2026, 11, 8)
FIRST, LAST = day_bounds(DAY)
H = 3600000
D = 86400000
TOKYO = [[[139.8, 35.45], [139.9, 35.45], [139.9, 35.55], [139.8, 35.55], [139.8, 35.45]]]


def sat(name, series, sar=False, resolution=None, per_day=0, system=None, look="both", mode=None, swath=12.0):
    return SatelliteSpec(name=name, id=name, tle1="", tle2="", maxOffNadir=30, minOffNadir=0, swath=swath, length=swath,
                         scenesPerPass=1, minInterval=0, maxPerDay=per_day, leadTime=0, series=series, lookSide=look, sar=sar,
                         source="test", resolution=resolution, system=system, mode=mode)


def request(rid, **kw):
    base = dict(id=rid, name=rid, category="臨時", achieve="期間内1回", start=FIRST - 5 * D, end=LAST + 10 * D,
                priority=500, kind="光学", rings=TOKYO, created=FIRST - 30 * D)
    base.update(kw)
    return ImagingRequest(**base)


SYSTEMS_JSON = [
    {"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT-X"], "order": 1, "limit": {"count": 2},
     "quota": {"臨時": 1, "定点": 5}, "sceneKm": [12, 12], "format": "csv", "encoding": "cp932", "timeZone": "Asia/Tokyo",
     "dateFormat": "%Y/%m/%d %H:%M",
     "priority": {"table": [{"max": 200, "value": "High"}, {"max": 600, "value": "Middle"}, {"max": 1000, "value": "Low"}]},
     "columns": [{"key": "ID", "source": "id"}, {"key": "SAT", "source": "satellite"}, {"key": "START", "source": "start"},
                 {"key": "AOI", "source": "geometry", "geometry": "wkt", "digits": 2},
                 {"key": "PRIO", "source": "priority", "allowed": ["High", "Middle", "Low"]},
                 {"key": "CLOUD", "source": "cloudForecast", "format": "%d"}, {"key": "FIX", "value": "1"},
                 {"key": "CAT", "source": "category", "map": {"定点": "FIX", "臨時": "TMP"}}]},
    {"system": "SYS-B", "label": "B", "kind": "SAR", "series": ["SAR-Y"], "order": 2, "limit": {"count": 30, "scenes": 2}, "sceneKm": [15, 15],
     "format": "csv", "timeZone": "UTC", "dateFormat": "unix",
     "priority": {"linear": {"min": 0, "max": 99, "higherIsLarger": True}, "format": "%02d"},
     "values": {"lookSide": {"右": "R", "左": "L", "任意": "B"}},
     "columns": [{"key": "ID", "source": "id"}, {"key": "T0", "source": "start"}, {"key": "PRIO", "source": "priority"},
                 {"key": "LOOK", "source": "lookSide"}, {"key": "N", "source": "scenes"}]},
    {"system": "SYS-C", "label": "C", "kind": "光学", "series": ["CONST-Z"], "order": 3, "format": "json", "timeZone": "UTC",
     "priority": {"table": [{"max": 200, "value": 1}, {"max": 400, "value": 2}, {"max": 600, "value": 3}, {"max": 800, "value": 4}, {"max": 1000, "value": 5}]},
     "schema": {
         "type": "object", "required": ["version", "issuedAt", "tasks"], "additionalProperties": False,
         "properties": {
             "version": {"type": "string", "const": "1.0"},
             "issuedAt": {"type": "string", "format": "date-time", "x-source": "runDate"},
             "count": {"type": "integer", "x-source": "count"},
             "tasks": {"type": "array", "x-source": "requests", "items": {"$ref": "#/$defs/task"}},
         },
         "$defs": {"task": {
             "type": "object", "required": ["taskId", "window", "aoi", "priority"], "additionalProperties": False,
             "properties": {
                 "taskId": {"type": "string", "pattern": "^R-", "x-source": "id"},
                 "type": {"type": "string", "enum": ["MONITORING", "AD_HOC"], "x-source": "category", "x-map": {"定点": "MONITORING", "臨時": "AD_HOC"}},
                 "window": {"type": "object", "properties": {"start": {"type": "string", "format": "date-time", "x-source": "start"},
                                                              "end": {"type": "string", "format": "date-time", "x-source": "end"}}},
                 "aoi": {"type": "object", "x-source": "geometry", "x-geometry": "geojson", "x-digits": 3},
                 "priority": {"type": "integer", "minimum": 1, "maximum": 5, "x-source": "priority"},
                 "maxCloud": {"type": ["integer", "null"], "x-source": "cloudMax"},
                 "gsd": {"type": "number", "x-source": "resolution"},
             }}},
     }},
]


def systems():
    problems = []
    out = export_systems_of(SYSTEMS_JSON, problems)
    assert problems == []
    return out


SATS = [
    sat("OPT-X 1", "OPT-X", resolution=0.5, per_day=1), sat("OPT-X 2", "OPT-X", resolution=0.8),
    sat("SAR-Y 1", "SAR-Y", sar=True, resolution=1.0, look="right"), sat("CONST-Z 7", "CONST-Z", resolution=3.0),
]


# --- The request itself ----------------------------------------------------------------------

def test_priority_levels_and_raises():
    assert [level_of(p) for p in (1, 200, 201, 500, 601, 1000)] == ["最優先", "最優先", "高", "中", "低", "最低"]
    assert priority_text(342) == "高（342）"
    r = request("R", priority=742)
    steps = []
    while (b := next_boost(r)) is not None:
        r.boost = b
        steps.append(r.effective)
    # 100 at a time, stopping once in the top band; the last two digits stay.
    assert steps == [642, 542, 442, 342, 242, 142]


def test_repeats_and_days():
    assert repeat_text(repeat_of("曜日:月水金")) == "曜日:月水金"
    assert repeat_of("間隔:3").every == 3 and repeat_of("何か").kind == "毎日"
    sunday = date(2026, 11, 8)
    assert sunday.weekday() == 6
    fixed = request("F", category="定点", repeat="曜日:月水金")
    assert not occurs_on(fixed, sunday) and occurs_on(fixed, date(2026, 11, 9))
    every3 = request("E", category="定点", repeat="間隔:3", start=day_bounds(date(2026, 11, 2))[0])
    assert [occurs_on(every3, date(2026, 11, d)) for d in range(2, 9)] == [True, False, False, True, False, False, True]
    # A 臨時 request ignores the repeat; days outside the period, stopped or done ones are out.
    assert occurs_on(request("T", repeat="曜日:月"), sunday)
    assert not occurs_on(request("X", end=FIRST - 1), sunday)
    assert not occurs_on(request("S", status="停止"), sunday)
    assert days_left(request("L", end=LAST + 2 * D), DAY) == 2


# --- Satellites and systems ------------------------------------------------------------------

def test_satellites_belong_to_systems_by_catalog_or_series():
    sys_ = systems()
    assert system_of_satellite(SATS[0], sys_).system == "SYS-A"
    assert system_of_satellite(SATS[2], sys_).system == "SYS-B"
    # The catalog's own `system` wins over the series.
    assert system_of_satellite(replace(SATS[0], system="SYS-C"), sys_).system == "SYS-C"
    assert system_of_satellite(sat("Other 1", "Other"), sys_) is None


def test_conditions_choose_satellites():
    assert why_not(request("R", kind="SAR"), SATS[0]) == "種別"
    assert why_not(request("R", series=["CONST-Z"]), SATS[0]) == "シリーズ"
    assert why_not(request("R", satellites=["OPT-X 2"]), SATS[0]) == "衛星"
    assert why_not(request("R", resolution=0.6), SATS[1]) == "分解能"
    assert why_not(request("R", kind="SAR", look_side="左"), SATS[2]) == "観測方向"
    assert why_not(request("R", off_nadir_min=35), SATS[0]) == "オフナディア角"
    assert [s.name for s in fitting(request("R", kind="任意", resolution=1.0), SATS)] == ["OPT-X 1", "OPT-X 2", "SAR-Y 1"]


def test_scenes_of_an_area():
    assert scenes_of(request("P", shape_kind="点"), 12, 12) == 1
    # About 9 × 11 km: one 12 km scene; 3 km scenes take a grid of 4 × 4.
    assert scenes_of(request("A"), 12, 12) == 1
    assert scenes_of(request("A"), 3, 3) == 16
    assert scenes_of(request("A", rings=square_rings(139.8, 35.5, 5)), 12, 12) == 1


# --- Weather -----------------------------------------------------------------------------------

def series(cover):
    times = [FIRST + h * H for h in range(-12, 48)]
    return CloudSeries(times, [float(cover)] * len(times))


def test_daytime_cloud_and_the_ones_digit():
    req = request("R", rings=[[[139.6, 35.4], [140.1, 35.4], [140.1, 35.8], [139.6, 35.8], [139.6, 35.4]]])
    points = places_of(req)
    assert len(points) == 4 and (139.75, 35.5) in points
    forecasts = {key_of(p): series(40 if i % 2 else 60) for i, p in enumerate(points)}
    assert daytime_cloud(req, forecasts, DAY) == pytest.approx(50)
    assert daytime_cloud(req, {}, DAY) is None
    # A small area falls back to its middle.
    small = request("S", rings=square_rings(139.81, 35.51, 2))
    assert places_of(small) == [pytest.approx((139.81, 35.51))]
    assert [cloud_step(c) for c in (0, 9.9, 10, 42, 89.9, 90, 100)] == [0, 0, 1, 4, 8, 9, 9]
    assert with_cloud(300, 42) == 304 and with_cloud(742, 95) == 749 and with_cloud(5, 0) == 1 and with_cloud(1000, 50) == 1000
    assert with_cloud(342, None) == 342


def test_gather_holds_cloudy_days_unless_the_deadline_is_near():
    reqs = [request("R-1", cloud_no_go=70), request("R-2", cloud_no_go=70, end=LAST + 2 * D),
            request("R-3", kind="SAR"), request("R-4", cloud_no_go=70, category="定点", achieve="毎回")]
    cands = gather(reqs, DAY, SATS, {"R-1": 80, "R-2": 80, "R-4": 10}, missed={"R-3"})
    by = {c.request.id: c for c in cands}
    assert by["R-1"].held.startswith("雲量 80%") and by["R-1"].weather_flag == "雲多"
    assert by["R-2"].held == "" and "期限間近" in by["R-2"].warnings[0]
    assert by["R-2"].boost_offer == 100 and by["R-2"].boost_reason == "期限まで 2 日"
    assert not by["R-3"].weather and by["R-3"].boost_offer == 100 and by["R-3"].boost_reason == "未撮像"
    assert by["R-4"].priority == 501 and by["R-4"].boost_offer is None
    # A raise not taken leaves the priority.
    by["R-3"].boost_taken = False
    assert by["R-3"].priority == 500


# --- Fitting -------------------------------------------------------------------------------------

def test_fitting_limits_quota_spill_and_satellites():
    sys_ = systems()
    reqs = [
        request("R-1", priority=100, category="定点", achieve="毎回"),
        request("R-2", priority=300, category="定点", achieve="毎回"),
        request("R-3", priority=200),                       # 臨時, but SYS-A's 臨時 quota is 1 …
        request("R-4", priority=400),                       # … so this one goes on to SYS-C
        request("R-5", kind="SAR", priority=500, rings=[[[139.0, 35.0], [139.2, 35.0], [139.2, 35.1], [139.0, 35.1], [139.0, 35.0]]]),
        request("R-6", kind="SAR", priority=600),           # SYS-B's scenes are used up by R-5
        request("R-7", series=["OPT-X"], satellites=["OPT-X 2"], priority=50),
        request("R-8", kind="光学", resolution=0.1),        # no satellite this fine
    ]
    cands = allocate(gather(reqs, DAY, SATS, {}, set()), sys_, SATS)
    got = {c.request.id: (c.result, c.system.system if c.system else c.reason, c.satellite.name if c.satellite else "") for c in cands}
    assert [c.request.id for c in cands][:3] == ["R-7", "R-1", "R-3"]
    # R-7 names its satellite; R-1 takes the finer OPT-X 1 (its 1 a day); SYS-A's 2 a day are then full.
    assert got["R-7"] == (TAKEN, "SYS-A", "OPT-X 2")
    assert got["R-1"] == (TAKEN, "SYS-A", "OPT-X 1")
    assert got["R-3"] == (TAKEN, "SYS-C", "CONST-Z 7")
    assert got["R-2"] == (TAKEN, "SYS-C", "CONST-Z 7")
    assert got["R-4"] == (TAKEN, "SYS-C", "CONST-Z 7")
    assert got["R-5"][:2] == (TAKEN, "SYS-B") and next(c for c in cands if c.request.id == "R-5").scenes == 2
    assert got["R-6"] == (HELD, "枠超過", "")
    assert got["R-8"] == (HELD, "該当システムなし", "")
    assert room_text(cands, sys_)["SYS-A"] == "2/2 件・2 枚"


def test_changes_by_hand():
    sys_ = systems()
    reqs = [request("R-1", priority=100), request("R-2", priority=300, satellites=["OPT-X 1"]), request("R-3", priority=200, cloud_no_go=50)]
    plan = plan_day(reqs, DAY, sys_, SATS, {"R-1": 0, "R-2": 0, "R-3": 60})
    by = {c.request.id: c for c in plan.candidates}
    assert by["R-3"].result == HELD and by["R-3"].reason.startswith("雲量")
    # Taken in by hand despite the clouds; R-1 left out; R-2 moved first.
    by["R-3"].manual = TAKEN
    by["R-1"].manual = HELD
    by["R-2"].rank = -1
    plan.refit()
    assert [c.request.id for c in plan.candidates] == ["R-2", "R-1", "R-3"]
    assert by["R-1"].reason == "手動で見送り"
    assert by["R-3"].result == TAKEN and any("手動で採用" in w for w in by["R-3"].warnings)


# --- Writing ---------------------------------------------------------------------------------

def test_times_priorities_and_geometry():
    sys_ = systems()
    jst, utc = sys_[0].time_zone, sys_[1].time_zone
    t = FIRST + 9 * H  # 2026-11-08 09:00 JST
    assert format_time(t, "iso", utc) == "2026-11-08T00:00:00Z"
    assert format_time(t, "iso", jst) == "2026-11-08T09:00:00+09:00"
    assert format_time(t, "iso-date", jst) == "2026-11-08"
    assert format_time(t, "unix", jst) == 1794096000 and format_time(t, "unixms", utc) == 1794096000000
    assert format_time(t, "%Y%m%d", jst) == "20261108"
    assert [convert_priority(p, sys_[0].priority) for p in (1, 200, 201, 600, 1000)] == ["High", "High", "Middle", "Middle", "Low"]
    assert [convert_priority(p, sys_[1].priority) for p in (1, 100, 505, 1000)] == ["99", "89", "49", "00"]
    assert geometry_value(TOKYO, "bbox", 2) == "139.8,35.45,139.9,35.55"
    assert geometry_value(TOKYO, "center", 3) == "35.5,139.85"
    assert geometry_value(TOKYO, "wkt", 1).startswith("POLYGON ((139.8 35.5, 139.9 35.5")
    assert json.loads(geometry_value(TOKYO, "geojson", 2))["type"] == "Polygon"


def test_csv_and_json_files():
    sys_ = systems()
    reqs = [request("R-0001", priority=300, category="定点", achieve="毎回"), request("R-0002", kind="SAR", priority=100, look_side="右"),
            request("R-0003", priority=500, series=["CONST-Z"], cloud_no_go=70, resolution=5)]
    plan = plan_day(reqs, DAY, sys_, SATS, {"R-0001": 42, "R-0003": 18})
    run = FIRST + 8 * H
    files = {w.system.system: w for w in render(plan.candidates, sys_, DAY, run)}
    assert set(files) == {"SYS-A", "SYS-B", "SYS-C"}
    a = list(csv.reader(io.StringIO(files["SYS-A"].text)))
    assert a[0] == ["ID", "SAT", "START", "AOI", "PRIO", "CLOUD", "FIX", "CAT"]
    assert a[1][:3] == ["R-0001", "OPT-X 1", "2026/11/08 00:00"]
    assert a[1][3].startswith("POLYGON ((139.80 35.45") and a[1][4:] == ["Middle", "42", "1", "FIX"]
    assert files["SYS-A"].text.endswith("\r\n") and files["SYS-A"].warnings == []
    b = list(csv.reader(io.StringIO(files["SYS-B"].text)))
    assert b[1] == ["R-0002", str(int(FIRST // 1000)), "89", "R", "1"]
    c = json.loads(files["SYS-C"].text)
    assert files["SYS-C"].errors == []
    assert c["version"] == "1.0" and c["count"] == 1 and c["issuedAt"] == "2026-11-07T23:00:00Z"
    task = c["tasks"][0]
    # 500 with 18 % cloud → 501 → band 3; the window runs from the day's start to the request's end (期間内1回).
    assert task["taskId"] == "R-0003" and task["type"] == "AD_HOC" and task["priority"] == 3
    assert task["window"] == {"start": "2026-11-07T15:00:00Z", "end": "2026-11-18T14:59:59Z"}
    assert task["aoi"]["coordinates"][0][0] == [139.8, 35.45]
    assert task["maxCloud"] == 70 and task["gsd"] == 5.0
    assert file_name_of(sys_[0], DAY, run) == "SYS-A_20261108.csv"


def test_schema_errors_stop_a_file():
    sys_ = systems()
    reqs = [request("BAD-1", priority=500, series=["CONST-Z"])]
    plan = plan_day(reqs, DAY, sys_, SATS, {})
    w = render(plan.candidates, sys_, DAY, FIRST)[0]
    assert w.system.system == "SYS-C"
    assert any("tasks[0].taskId" in e for e in w.errors)


def test_validator():
    schema = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "integer", "minimum": 1}, "b": {"type": "string", "enum": ["x", "y"]},
        "c": {"type": "array", "maxItems": 1, "items": {"type": "number"}}, "d": {"anyOf": [{"type": "null"}, {"type": "string", "format": "date"}]}},
        "additionalProperties": False}
    assert validate({"a": 1, "b": "x", "c": [1.5], "d": "2026-11-08"}, schema) == []
    errors = validate({"a": 0, "b": "z", "c": [1, 2], "d": "11/08", "e": 1}, schema)
    assert len(errors) == 5
    assert validate({}, schema) == ["$: 必須の a なし"]


# --- The ledger and its history in Esri ------------------------------------------------------

LEDGER = "https://gis.example/rest/services/R/FeatureServer/0"
HISTORY = "https://gis.example/rest/services/R/FeatureServer/1"
CATALOG = "https://gis.example/rest/services/I/FeatureServer/0"


def server():
    fake = FakeEsri()
    types = {name: "esriFieldTypeString" for name in DEFAULT_REQUEST_FIELDS.values()}
    for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT"):
        types[n] = "esriFieldTypeDate"
    for n in ("PRIORITY", "BOOST", "OFF_NADIR_MIN", "OFF_NADIR_MAX", "INCIDENCE_MIN", "INCIDENCE_MAX", "RESOLUTION_MAX", "CLOUD_NO_GO"):
        types[n] = "esriFieldTypeDouble"
    fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    htypes = {name: "esriFieldTypeString" for name in DEFAULT_HISTORY_FIELDS.values()}
    htypes.update(RUN_DATE="esriFieldTypeDate", CLOUD="esriFieldTypeDouble", SCENES="esriFieldTypeInteger", BOOST_DONE="esriFieldTypeInteger")
    fake.add_layer(HISTORY, FakeLayer(htypes, "esriGeometryNone"))
    images = fake.add_layer(CATALOG, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString", "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    images.add({"IMAGE_ID": "IMG-1", "ACQ": FIRST + 10 * H, "SENSOR": "OPT-X 1"},
               {"rings": [[[139.7, 35.4], [139.7, 35.6], [140.0, 35.6], [140.0, 35.4], [139.7, 35.4]]], "spatialReference": {"wkid": 4326}})
    return fake


def test_ledger_round_trip_history_and_follow_up():
    problems = []
    store = request_store_of({"label": "依頼", "url": LEDGER, "history": HISTORY, "catalog": "画像", "fields": {"name": "TITLE?"}}, problems, 0)
    assert problems == ["requestStores[0].fields.name は属性名で指定"]
    assert parse_config({"requestStores": [{"url": "https://x/MapServer/0"}]})[1][0].startswith("requestStores[0] の url")
    fake = server()
    client = EsriClient(fake)
    r1 = request("R-0001", priority=742, series=["OPT-X"], satellites=["OPT-X 1"], systems=["SYS-A"], cloud_no_go=70, note="港")
    r2 = request("R-0002", kind="SAR", priority=100, look_side="右", rings=[[[135.4, 34.6], [135.5, 34.6], [135.5, 34.7], [135.4, 34.7], [135.4, 34.6]]])
    save_request(client, store, None, r1, FIRST - D)
    save_request(client, store, None, r2, FIRST - D)
    assert r1.object_id == 1 and r1.updated == FIRST - D
    ring = fake.layers[LEDGER].rows[0]["geometry"]["rings"][0]
    assert ring[1][1] > ring[0][1]  # clockwise outside
    loaded = {r.id: r for r in load_requests(client, store, None)}
    got = loaded["R-0001"]
    assert (got.priority, got.series, got.satellites, got.systems, got.cloud_no_go, got.note, got.start) == (742, ["OPT-X"], ["OPT-X 1"], ["SYS-A"], 70, "港", r1.start)
    r1.name = "東京湾"
    save_request(client, store, None, r1, FIRST)
    assert next(r for r in load_requests(client, store, None) if r.id == "R-0001").name == "東京湾"

    sys_ = systems()
    plan = plan_day(list(loaded.values()), DAY, sys_, SATS, {"R-0001": 30})
    rows = history_of(plan, FIRST + 8 * H, {"SYS-A": "SYS-A_20261108.csv", "SYS-B": "SYS-B_20261108.json"}, "RUN1")
    assert add_history(client, store, None, rows) == 2
    history = load_history(client, store, None)
    assert {(h.request_id, h.system, h.result, h.file, h.captured) for h in history} == {
        ("R-0001", "SYS-A", "採用", "SYS-A_20261108.csv", "未判定"), ("R-0002", "SYS-B", "採用", "SYS-B_20261108.json", "未判定")}

    catalog = catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}, [], 0)
    by_id = {r.id: r for r in load_requests(client, store, None)}
    # The day after: Tokyo found; Osaka not yet (3 days to wait).
    found = follow_up(client, catalog, None, history, by_id, LAST + D, store.miss_after_days)
    assert [(f.row.request_id, f.captured, f.image_id) for f in found] == [("R-0001", "撮像済み", "IMG-1")]
    later = follow_up(client, catalog, None, history, by_id, LAST + 4 * D, store.miss_after_days)
    assert {(f.row.request_id, f.captured) for f in later} == {("R-0001", "撮像済み"), ("R-0002", "未撮像")}
    assert update_history(client, store, None, [(f.row.object_id, {"captured": f.captured, "imageId": f.image_id}) for f in later]) == 2
    changes = request_changes(later, by_id)
    assert changes == [(by_id["R-0001"].object_id, {"boost": 0, "status": "完了"})]
    assert update_requests(client, store, None, changes, LAST + 4 * D) == 1

    # The next day: R-0001 is done; R-0002 was missed and is offered a raise.
    history = load_history(client, store, None)
    assert missed_ids(history) == {"R-0002"}
    requests = load_requests(client, store, None)
    nxt = date(2026, 11, 9)
    plan = plan_day(requests, nxt, sys_, SATS, {}, history)
    assert [c.request.id for c in plan.candidates] == ["R-0002"]
    assert plan.candidates[0].boost_offer is None  # already in the top band (100)
    r3 = request("R-0003", kind="SAR", priority=742)
    r3.object_id = 99
    missed = next(h for h in history if h.captured == "未撮像")
    plan2 = plan_day([r3], nxt, sys_, SATS, {}, [replace(missed, request_id="R-0003")])
    assert boost_changes(plan2) == [(99, {"boost": 100})]
    assert missed_done(plan2) == [(missed.object_id, {"boostDone": 1})]


def image(sensor, iid="IMG"):
    return {"attributes": {"SENSOR": sensor, "IMAGE_ID": iid}}


def test_images_must_come_from_a_named_satellite():
    names = SatelliteNames.of([("WorldView-3", "WV-3", "WV03")])
    row = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-1", "SYS-A", "WorldView-3")
    found = [image("SkySat-C12", "S1"), image("WV03", "W1")]
    # Any satellite will do: every image counts (the planned one preferred by follow_up).
    assert accepted_names(row, request("R-1")) == set()
    assert fitting_images(found, "SENSOR", row, request("R-1"), names) == found
    # Satellites named: only images of them (or of the planned one, by its other names).
    named = request("R-1", satellites=["WorldView-3"])
    assert "wv03" in accepted_names(row, named, names)
    assert [f["attributes"]["IMAGE_ID"] for f in fitting_images(found, "SENSOR", row, named, names)] == ["W1"]
    assert fitting_images([image("SkySat-C12")], "SENSOR", row, named, names) == []
    # A series named: its members' images count (the name inside the sensor's).
    assert len(fitting_images([image("OPT-X 2")], "SENSOR", replace(row, satellite="OPT-X 1"), request("R-1", series=["OPT-X"]))) == 1


def test_follow_up_leaves_another_satellites_image_unsettled():
    problems = []
    store = request_store_of({"label": "依頼", "url": LEDGER, "history": HISTORY, "catalog": "画像"}, problems, 0)
    fake = server()
    client = EsriClient(fake)
    catalog = catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}, [], 0)
    other = request("R-0009", satellites=["OPT-X 2"], object_id=9)
    row = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-0009", "SYS-A", "OPT-X 2", object_id=1)
    # Only an image of OPT-X 1 over the area: not this request's, so 未撮像 once the wait is over.
    assert follow_up(client, catalog, None, [row], {"R-0009": other}, LAST + D, 3) == []
    assert [f.captured for f in follow_up(client, catalog, None, [row], {"R-0009": other}, LAST + 4 * D, 3)] == ["未撮像"]
    anyone = replace(other, satellites=[])
    assert [(f.captured, f.image_id) for f in follow_up(client, catalog, None, [row], {"R-0009": anyone}, LAST + D, 3)] == [("撮像済み", "IMG-1")]
    # Followed up and saved: the history row and the ledger (raise undone, 期間内1回 完了).
    fake.layers[HISTORY].add({"REQ_ID": "R-0009", "TARGET_DAY": DAY.isoformat(), "RESULT": "採用", "CAPTURED": "未判定"})
    save_request(client, store, None, replace(anyone, boost=200, object_id=None), FIRST - D)
    history = load_history(client, store, None)
    by_id = {r.id: r for r in load_requests(client, store, None)}
    found, changed = follow_and_save(client, store, None, catalog, None, history, by_id, LAST + D)
    assert [f.captured for f in found] == ["撮像済み"] and changed and history[0].captured == "撮像済み"
    saved = load_requests(client, store, None)[0]
    assert (saved.boost, saved.status) == (0, "完了")
    assert load_history(client, store, None)[0].image_id == "IMG-1"
    # By hand back to 未撮像: the request is 有効 again and the row offered its raise again.
    history = load_history(client, store, None)
    history[0].boost_done = 1
    by_id = {r.id: r for r in load_requests(client, store, None)}
    findings = marked(history, "未撮像")
    assert history_changes(findings) == [(history[0].object_id, {"captured": "未撮像", "imageId": "", "boostDone": 0})]
    assert undone_changes(findings, by_id, history) == [(by_id["R-0009"].object_id, {"status": "有効"})]
    assert save_marked(client, store, None, findings, by_id, history, LAST + 2 * D)
    assert load_requests(client, store, None)[0].status == "有効"
    assert (load_history(client, store, None)[0].captured, history[0].boost_done) == ("未撮像", 0)
    assert marked(history, "未撮像") == []  # already so


def test_missed_requests_carry_over_to_the_next_plan():
    weekly = request("R-W", category="定点", achieve="毎回", repeat="曜日:日", object_id=5)  # DAY is a Sunday
    nxt = date(2026, 11, 11)  # a Wednesday
    assert occurs_on(weekly, DAY) and not occurs_on(weekly, nxt)
    assert carries_over(weekly, nxt) and not carries_over(replace(weekly, status="停止"), nxt)
    assert not carries_over(weekly, date(2026, 11, 30))  # past its period
    assert gather([weekly], nxt, SATS, {}, set()) == []
    [c] = gather([weekly], nxt, SATS, {"R-W": 10}, {"R-W"})
    assert c.carried and "未撮像の持ち越し" in c.warnings and c.boost_offer == 100 and c.boost_reason == "未撮像"
    row = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-W", "SYS-A", "OPT-X 1", captured="未撮像", object_id=3)
    plan = plan_day([weekly], nxt, systems(), SATS, {"R-W": 10}, [row])
    assert [(c.request.id, c.result) for c in plan.candidates] == [("R-W", TAKEN)]
    assert boost_changes(plan) == [(5, {"boost": 100})] and missed_done(plan) == [(3, {"boostDone": 1})]


def test_tally_and_what_comes_next():
    rows = [HistoryRow("R", FIRST, "2026-11-06", "A", "SYS-A", captured="撮像済み"),
            HistoryRow("R", FIRST, "2026-11-07", "B", "SYS-A", captured="未撮像"),
            HistoryRow("R", FIRST, "2026-11-08", "C", "SYS-B", captured="未判定"),
            HistoryRow("R", FIRST, "2026-11-08", "D", "SYS-B", result="見送り", captured="")]
    t = {x.system: x for x in tally(rows)}
    assert (t["SYS-A"].taken, t["SYS-A"].missed, t["SYS-B"].pending, t["計"].total) == (1, 1, 1, 3)
    assert t["SYS-A"].text == "撮像済み 1・未撮像 1・未判定 0（撮像率 50%）"
    assert t["SYS-B"].text == "撮像済み 0・未撮像 0・未判定 1"
    assert [r.request_id for r in in_days(rows, date(2026, 11, 7), DAY)] == ["B", "C", "D"]
    reqs = {"A": request("A", status="完了"), "B": request("B"), "C": request("C")}
    assert next_step(rows[0], reqs["A"], DAY) == "完了"
    assert next_step(rows[1], reqs["B"], DAY) == "次回の計算で持ち越し・繰り上げ候補"
    assert next_step(replace(rows[1], boost_done=1), reqs["B"], DAY) == "繰り上げ確認済み"
    assert next_step(rows[1], reqs["B"], date(2026, 12, 1)) == "期間終了"
    assert next_step(rows[1], replace(reqs["B"], status="停止"), DAY) == "依頼は停止"
    assert next_step(rows[2], reqs["C"], DAY) == "撮像待ち"
    assert next_step(rows[2], None, DAY) == "台帳に依頼なし"
    assert next_step(rows[3], None, DAY) == ""


def test_forecast_places_and_clouds():
    reqs = [request("R-1"), request("R-2", kind="SAR"), request("R-3", status="停止")]
    places = forecast_places(reqs, DAY, SATS)
    assert places == places_of(reqs[0])
    clouds = clouds_of(reqs, DAY, {key_of(p): series(25) for p in places})
    assert clouds["R-1"] == pytest.approx(25)


def test_sample_config_reads():
    import os

    path = os.path.join(os.path.dirname(__file__), "data", "request_config.jsonc")
    with open(path, encoding="utf-8") as f:
        config, problems = parse_config_text(f.read())
    assert problems == []
    assert [s.system for s in config.request_exports] == ["SYS-A", "SYS-B", "SYS-C"]
    assert config.request_stores[0].history


def test_a_system_not_written_is_not_handed_out():
    """#68: a candidate taken for a system whose file was not written is 見送り in the history, not 採用・未判定."""
    sys_ = systems()
    r1 = request("R-0001", priority=742, series=["OPT-X"], satellites=["OPT-X 1"], systems=["SYS-A"], object_id=1)
    r2 = request("R-0002", kind="SAR", priority=100, look_side="右", object_id=2,
                 rings=[[[135.4, 34.6], [135.5, 34.6], [135.5, 34.7], [135.4, 34.7], [135.4, 34.6]]])
    plan = plan_day([r1, r2], DAY, sys_, SATS, {"R-0001": 30})
    assert sorted(c.request.id for c in plan.candidates if c.result == TAKEN) == ["R-0001", "R-0002"]
    rows = history_of(plan, FIRST, {"SYS-A": "SYS-A_20261108.csv"}, "RUN1", failures={"SYS-B": "必須項目 tasks なし"})
    got = {r.request_id: (r.result, r.captured, r.file, r.reason) for r in rows}
    assert got["R-0001"] == ("採用", "未判定", "SYS-A_20261108.csv", "")
    assert got["R-0002"] == ("見送り", "", "", "書き出し失敗（必須項目 tasks なし）")
    assert not_written(plan, {"SYS-A": "x"}) == {"R-0002"}
    # Its raise and its 未撮像 row are left for the next day.
    plan.candidates[[c.request.id for c in plan.candidates].index("R-0002")].boost_offer = 100
    plan.missed_rows = [HistoryRow("RUN0", FIRST - D, "2026-11-07", "R-0002", "SYS-B", captured="未撮像", object_id=7)]
    assert boost_changes(plan, {"R-0002"}) == [] and missed_done(plan, {"R-0002"}) == []
    assert boost_changes(plan) == [(2, {"boost": 100})] and missed_done(plan) == [(7, {"boostDone": 1})]


def test_history_reads_only_what_is_needed():
    """#69: the history is read by condition (open rows, a span, some requests), not the whole table."""
    problems = []
    store = request_store_of({"label": "依頼", "url": LEDGER, "history": HISTORY}, problems, 0)
    fake = server()
    client = EsriClient(fake)
    history = fake.layers[HISTORY]
    for day, rid, result, captured, done in (
            ("2026-01-01", "OLD", "採用", "撮像済み", 0), ("2026-01-02", "OLD2", "見送り", "", 0),
            ("2026-11-01", "MISS", "採用", "未撮像", 0), ("2026-11-02", "RAISED", "採用", "未撮像", 1),
            ("2026-11-07", "OPEN", "採用", "未判定", 0), ("2026-11-08", "HELD", "見送り", "", 0)):
        history.add({"TARGET_DAY": day, "REQ_ID": rid, "RESULT": result, "CAPTURED": captured, "BOOST_DONE": done})
    open_rows = load_history(client, store, None, history_where(store, open_rows=True))
    assert {r.request_id for r in open_rows} == {"MISS", "OPEN"}
    span = load_history(client, store, None, history_where(store, first="2026-11-02", last="2026-11-07"))
    assert [r.request_id for r in span] == ["RAISED", "OPEN"]
    assert {r.request_id for r in load_history(client, store, None, history_where(store, request_ids=["OLD", "HELD", "OLD"]))} == {"OLD", "HELD"}
    assert count_history(client, store, None, replaced_where(store, "2026-11-08")) == 1
    assert int(delete_history(client, store, None, replaced_where(store, "2026-11-08"))) == 1
    assert count_history(client, store, None, "1=1") == 5


def test_query_all_never_cuts_silently():
    """#69: more rows than the limit is an error (paging in object id order), unless a cut is asked for."""
    fake = FakeEsri()
    for pagination in (True, False):
        url = f"https://example.com/arcgis/rest/services/T{int(pagination)}/FeatureServer/0"
        layer = fake.add_layer(url, FakeLayer({"N": "esriFieldTypeInteger"}, "esriGeometryNone", max_record_count=3, pagination=pagination))
        for i in range(7):
            layer.add({"N": i})
        client = EsriClient(fake)
        assert [f["attributes"]["N"] for f in client.query_all(url, geometry=False, limit=7)] == list(range(7))
        with pytest.raises(TooManyError):
            client.query_all(url, geometry=False, limit=6)
        assert [f["attributes"]["N"] for f in client.query_all(url, geometry=False, limit=5, cut=True)] == list(range(5))
        if pagination:
            asked = [form for u, form in fake.asked if u == f"{url}/query" and "resultOffset" in form]
            assert all(form.get("orderByFields") == "OBJECTID" for form in asked)


def test_saves_tell_what_failed():
    """#71: a batch that fails (or rows refused) is told, not counted as saved; follow-ups settle only saved rows."""
    problems = []
    store = request_store_of({"label": "依頼", "url": LEDGER, "history": HISTORY}, problems, 0)
    fake = server()
    client = EsriClient(fake)
    rows = [HistoryRow("RUN", FIRST, DAY.isoformat(), f"R-{i}") for i in range(250)]
    calls = {"n": 0}
    real = fake.answer

    def flaky(url, form):
        if url.endswith("/applyEdits"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise EsriError("タイムアウト")
        return real(url, form)

    fake.answer = flaky
    saved = add_history(client, store, None, rows)
    assert saved == 200 and saved.failed and "50 件を保存できず（タイムアウト）" in saved.problems[0]
    # An update of a row that is gone: refused, so not settled.
    fake.answer = real
    oids = [r["attributes"]["OBJECTID"] for r in fake.layers[HISTORY].rows[:2]]
    done = update_history(client, store, None, [(oids[0], {"captured": "撮像済み"}), (9999, {"captured": "撮像済み"})])
    assert done == 1 and done.ok == {oids[0]} and done.problems == ["出力履歴の更新: 見つからない地物"]
    a = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-0", object_id=oids[0])
    b = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-1", object_id=9999)
    out = []
    save_marked(client, store, None, marked([a, b], "撮像済み", "IMG"), {}, [a, b], FIRST, out)
    assert (a.captured, b.captured) == ("撮像済み", "未判定") and out


def test_rows_without_a_request_are_not_settled():
    """#77: a history row whose request left the ledger is not followed up (no area: any image would do)."""
    fake = server()
    client = EsriClient(fake)
    catalog = catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}, [], 0)
    row = HistoryRow("RUN", FIRST, DAY.isoformat(), "GONE", "SYS-A", "OPT-X 1", object_id=1)
    assert follow_up(client, catalog, None, [row], {}, LAST + D, 3) == []
    assert follow_up(client, catalog, None, [row], {}, LAST + 10 * D, 3) == []
    no_area = request("GONE", rings=[])
    assert follow_up(client, catalog, None, [row], {"GONE": no_area}, LAST + D, 3) == []
    assert next_step(replace(row, captured="未判定"), None, DAY) == "台帳に依頼なし"


def test_model_numbers_are_other_satellites():
    """#78: KOMPSAT-3 is not KOMPSAT-3A, ALOS-2 not ALOS; a series still takes its members, a satellite its products."""
    row = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-1", "SYS-A", "KOMPSAT-3")
    k3 = request("R-1", satellites=["KOMPSAT-3"])
    ids = lambda found, req, r=row: [f["attributes"]["IMAGE_ID"] for f in fitting_images(found, "SENSOR", r, req)]
    assert ids([image("KOMPSAT-3A", "A"), image("KOMPSAT-3", "B"), image("KOMPSAT 3", "C")], k3) == ["B", "C"]
    alos2 = request("R-1", satellites=["ALOS-2"])
    assert ids([image("ALOS", "A"), image("ALOS-2", "B")], alos2, replace(row, satellite="ALOS-2")) == ["B"]
    assert ids([image("WorldView-3 PAN", "W")], request("R-1", satellites=["WorldView-3"]), replace(row, satellite="WorldView-3")) == ["W"]
    assert ids([image("KOMPSAT-3A", "A"), image("OTHER", "O")], request("R-1", series=["KOMPSAT"]), replace(row, satellite="")) == ["A"]
    # An alias given in satelliteAliases still counts.
    names = SatelliteNames.of([("KOMPSAT-3", "K3")])
    assert [f["attributes"]["IMAGE_ID"] for f in fitting_images([image("K3", "X")], "SENSOR", row, k3, names)] == ["X"]


def test_text_dates_are_compared_as_the_catalog_writes_them():
    """#79: a text 撮像日時 is compared in the catalog's own shape (T or space, its zone), the end an open bound."""
    first, last = day_bounds(DAY)  # 2026-11-08 JST
    assert between_condition("ACQ", "esriFieldTypeString", first, last, "2026-11-01T00:00:00Z") == \
        "ACQ >= '2026-11-07T15:00:00' AND ACQ < '2026-11-08T15:00:00'"
    assert between_condition("ACQ", "esriFieldTypeString", first, last, "2026-11-01 00:00:00") == \
        "ACQ >= '2026-11-07 15:00:00' AND ACQ < '2026-11-08 15:00:00'"
    assert between_condition("ACQ", "esriFieldTypeString", first, last, "2026-11-01T09:00:00+09:00") == \
        "ACQ >= '2026-11-08T00:00:00' AND ACQ < '2026-11-09T00:00:00'"
    tokyo = parse_time_zone("Asia/Tokyo")
    assert between_condition("ACQ", "esriFieldTypeString", first, last, "2026-11-01 09:00:00", tokyo) == \
        "ACQ >= '2026-11-08 00:00:00' AND ACQ < '2026-11-09 00:00:00'"
    # The catalog end to end: ISO text in UTC, the image at 01:00Z on the 8th is the JST 8th's, the 7th's (00:00Z) is not.
    fake = FakeEsri()
    url = "https://example.com/arcgis/rest/services/Text/FeatureServer/0"
    layer = fake.add_layer(url, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeString", "URL": "esriFieldTypeString"},
                                          "esriGeometryPolygon"))
    area = {"rings": [[[139.7, 35.4], [139.7, 35.6], [140.0, 35.6], [140.0, 35.4], [139.7, 35.4]]], "spatialReference": {"wkid": 4326}}
    layer.add({"IMAGE_ID": "BEFORE", "ACQ": "2026-11-07T00:00:00Z"}, area)
    layer.add({"IMAGE_ID": "ON", "ACQ": "2026-11-08T01:00:00Z"}, area)
    catalog = catalog_of({"label": "画像", "url": url, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "source": "URL"}}, [], 0)
    row = HistoryRow("RUN", FIRST, DAY.isoformat(), "R-1", "SYS-A", object_id=1)
    req = request("R-1", rings=area["rings"])
    found = follow_up(EsriClient(fake), catalog, None, [row], {"R-1": req}, LAST + D, 3)
    assert [(f.captured, f.image_id) for f in found] == [("撮像済み", "ON")]
