"""
The viewer's imaging-plan-store.test.ts, imaging-plan-weather.test.ts,
imaging-plan-pairs.test.ts and the calendar part of
imaging-plan-extras.test.ts, ported.
"""
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from satellite_observation.core.catalog import catalog_of
from satellite_observation.core.config import parse_config
from satellite_observation.core.esri import EsriClient
from satellite_observation.core.plan.access import Opportunity, PlanOptions, PlanTarget, plan_access
from satellite_observation.core.plan.orbit import ecef_of
from satellite_observation.core.plan.pairs import RepeatOptions, StereoOptions, relate, repeat_pairs, stereo_pairs
from satellite_observation.core.plan.remind import (
    PlanReminder, due_reminders, merge_reminders, new_reminders, plan_ics, reminder_of, reminder_text, shots_of,
)
from satellite_observation.core.plan.revisit import (
    box_of, cell_targets, grid_cells, hours_text, revisit_color, revisit_stats, RevisitStats,
)
from satellite_observation.core.plan.satellites import satellites_from_text
from satellite_observation.core.plan.store import (
    DEFAULT_PLAN_FIELDS, ShotCheck, add_shots, between_condition, delete_plan, list_plans, load_shots, plan_store_of,
    progress_text, shot_features, track_shots, update_shots,
)
from satellite_observation.core.plan.weather import (
    DEFAULT_CLOUD_FORECAST, CloudSeries, apply_clouds, cloud_at, cloud_forecast_of, fetch_clouds, places_for,
)
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer

LINE1 = "1 25544U 98067A   26280.50000000  .00016717  00000-0  10270-3 0  9994"
LINE2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49815399432717"
EPOCH = 1767225600000 + 279.5 * 86400000
DAY = 86400000


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def sat(name="ISS", **spec):
    return replace(satellites_from_text(f"{name}\n{LINE1}\n{LINE2}", 45, 0, 10)[0], **spec)


def op(time, sat_ecef=(0, 0, 6800), **extra):
    values = dict(satellite=0, target=0, time=time, offNadir=10, incidence=11, elevation=79, side="left", ascending=False,
                  sunElevation=30, range=500, width=0, strips=1, length=0, frames=1, coverage=1, window=(0, 0), along=(0, 0),
                  tleAge=1, azimuth=0, satEcef=tuple(sat_ecef), orbitRadius=6800)
    values.update(extra)
    return Opportunity(**values)


# --- plan stores -------------------------------------------------------------------------

PLAN_URL = "https://gis.example/arcgis/rest/services/Plans/FeatureServer/0"
CATALOG_URL = "https://gis.example/arcgis/rest/services/Images/FeatureServer/0"
T0 = utc(2026, 10, 10, 1, 0)
STORE_SATS = [sat("WorldView-3", id="WorldView-3"), sat("SAR-1", id="SAR-1")]
STORE_TARGETS = [PlanTarget("東京", (139.7, 35.7), []), PlanTarget("大阪", (135.5, 34.7), [])]


def box(lon, lat):
    return [[[lon - 0.05, lat - 0.05], [lon + 0.05, lat - 0.05], [lon + 0.05, lat + 0.05], [lon - 0.05, lat + 0.05], [lon - 0.05, lat - 0.05]]]


def image_rings(w, s, e, n):
    return {"rings": [[[w, s], [w, n], [e, n], [e, s], [w, s]]], "spatialReference": {"wkid": 4326}}


def plan_server():
    fake = FakeEsri()
    fields = {name: "esriFieldTypeString" for name in DEFAULT_PLAN_FIELDS.values()}
    fields.update(SHOT_TIME="esriFieldTypeDate", CREATED_AT="esriFieldTypeDate", OFF_NADIR="esriFieldTypeDouble", INCIDENCE="esriFieldTypeDouble")
    del fields["MODE"]
    fake.add_layer(PLAN_URL, FakeLayer(fields, "esriGeometryPolygon"))
    images = fake.add_layer(CATALOG_URL, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString", "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    # Taken 10 minutes after the Tokyo shot, over it, by the planned satellite (and another one at the same time).
    images.add({"IMAGE_ID": "WV3-001", "ACQ": T0 + 600000, "SENSOR": "WV-3 / WorldView3"}, image_rings(139.6, 35.6, 139.8, 35.8))
    images.add({"IMAGE_ID": "OTHER", "ACQ": T0 + 600000, "SENSOR": "Pleiades"}, image_rings(139.6, 35.6, 139.8, 35.8))
    # Over Osaka, but a week late.
    images.add({"IMAGE_ID": "LATE", "ACQ": T0 + 8 * DAY, "SENSOR": "SAR-1"}, image_rings(135.4, 34.6, 135.6, 34.8))
    return fake


def test_plan_stores_from_settings():
    problems = []
    store = plan_store_of({"label": "計画", "url": PLAN_URL, "fields": {"status": "STATE"}, "catalog": "画像"}, problems, 0)
    assert store.fields == {**DEFAULT_PLAN_FIELDS, "status": "STATE"}
    assert store.match_minutes == 30 and store.miss_after_days == 3
    assert plan_store_of({"url": "https://x/MapServer"}, problems, 1) is None
    assert len(problems) == 1
    assert len(parse_config({"planStores": [{"url": PLAN_URL}]})[0].plan_stores) == 1


def test_save_list_follow_up_and_delete_a_plan():
    store = plan_store_of({"url": PLAN_URL}, [], 0)
    fake = plan_server()
    client = EsriClient(fake)
    ops = [op(T0, satellite=0, target=0), op(T0 + DAY, satellite=1, target=1), op(T0 + 20 * DAY, satellite=0, target=1)]
    features = shot_features(ops, STORE_SATS, STORE_TARGETS, lambda o: box(*STORE_TARGETS[o.target].center), "P1", "十月の計画", store.fields, T0 - DAY)
    assert len(features) == 3
    a = features[0]["attributes"]
    assert (a["PLAN_ID"], a["SHOT_TIME"], a["SATELLITE"], a["STATUS"], a["PASS_DIR"]) == ("P1", T0, "WorldView-3", "予定", "南行")
    assert add_shots(client, store, None, features) == (3, [])
    # Outer rings go clockwise; attributes the layer lacks (MODE) are left out.
    ring = fake.layers[PLAN_URL].rows[0]["geometry"]["rings"][0]
    assert ring[1][1] > ring[0][1]
    assert "MODE" not in fake.layers[PLAN_URL].rows[0]["attributes"]

    plans = list_plans(client, store, None)
    assert [(p.id, p.name, p.shots, p.first, p.statuses) for p in plans] == [("P1", "十月の計画", 3, T0, {"予定": 3})]
    shots = load_shots(client, store, None, "P1")
    assert [s.target for s in shots] == ["東京", "大阪", "大阪"]
    assert shots[0].rings

    catalog = catalog_of({"label": "画像", "url": CATALOG_URL, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}, [], 0)
    changes = track_shots(client, store, catalog, None, shots, T0 + 5 * DAY)
    # Tokyo: the satellite's own image, not the other one; Osaka: nothing within 30 minutes and 3 days gone; the last shot is ahead.
    assert changes == [ShotCheck(shots[0].objectId, "撮像済み", "WV3-001"), ShotCheck(shots[1].objectId, "未撮像")]
    assert update_shots(client, store, None, changes) == 2
    after = list_plans(client, store, None)
    assert after[0].statuses == {"撮像済み": 1, "未撮像": 1, "予定": 1}
    assert progress_text(after[0].statuses) == "撮像済み 1 / 2（50%）・未撮像 1・予定 1"

    delete_plan(client, store, None, "P1")
    assert list_plans(client, store, None) == []


def test_time_conditions_for_the_field_type():
    assert between_condition("T", "esriFieldTypeDate", 0, 60000) == "T >= TIMESTAMP '1970-01-01 00:00:00' AND T <= TIMESTAMP '1970-01-01 00:01:00'"
    assert between_condition("T", "esriFieldTypeDouble", 0.5, 10.2) == "T >= 0 AND T <= 11"


# --- calendar and reminders ---------------------------------------------------------------

CAL_SATS = [sat("A, 1", id="A1", leadTime=24), sat("B", id="B")]
CAL_TARGETS = [PlanTarget("東京", (139.7, 35.7), []), PlanTarget("横浜", (139.6, 35.4), [])]
CAL_OPS = [
    op(utc(2026, 10, 23, 1, 0), ascending=True, satellite=0, target=0, shot=1, shotTargets=2),
    op(utc(2026, 10, 23, 1, 0, 3), ascending=True, satellite=0, target=1, shot=1, shotTargets=2),
    op(utc(2026, 10, 24, 2, 0), ascending=True, satellite=1, target=1, shot=2, shotTargets=1),
]


def test_a_shared_scene_is_taken_once():
    shots = shots_of(CAL_OPS, CAL_SATS, CAL_TARGETS)
    assert len(shots) == 2
    assert shots[0].targets == ["東京", "横浜"]


def test_icalendar_file_with_an_event_and_alarm_per_shot():
    ics = plan_ics(CAL_OPS, CAL_SATS, CAL_TARGETS, 30, utc(2026, 10, 22))
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == 2
    assert "DTSTART:20261023T010000Z" in ics
    assert "TRIGGER:-PT30M" in ics
    # Commas escaped; long lines folded at 75 octets.
    assert "A\\, 1" in ics
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert ics.endswith("END:VCALENDAR\r\n")
    assert "発注期限: 2026-10-22T01:00:00.000Z" in ics.replace("\r\n ", "")


def test_reminders_are_set_replaced_and_fired():
    now = utc(2026, 10, 22)
    fresh = new_reminders(CAL_OPS, CAL_SATS, CAL_TARGETS, 30, lambda o: box(139.7, 35.7), now)
    assert [r.target for r in fresh] == ["東京、横浜", "横浜"]
    assert fresh[0].notifyAt == CAL_OPS[0].time - 30 * 60000
    # Set again: the same shots replaced, not doubled; read back from the settings as JSON.
    kept = [reminder_of(json.loads(json.dumps(r.to_json()))) for r in merge_reminders(fresh, fresh)]
    assert len(kept) == 2 and isinstance(kept[0], PlanReminder)
    assert reminder_of({"id": 1}) is None
    # Nothing due yet; 20 minutes before the first shot it is.
    assert due_reminders(kept, now) == ([], kept)
    at = CAL_OPS[0].time - 20 * 60000
    due, rest = due_reminders(kept, at)
    assert [r.target for r in due] == ["東京、横浜"] and len(rest) == 1
    assert "あと 20 分" in reminder_text(due[0], at)
    # A shot long past is dropped without a notice.
    assert due_reminders(kept, CAL_OPS[0].time + 3600000) == ([], kept[1:])
    # Shots already past are not reminded of.
    assert new_reminders(CAL_OPS, CAL_SATS, CAL_TARGETS, 30, lambda o: [], CAL_OPS[2].time) == []


# --- cloud forecasts -----------------------------------------------------------------------

def test_cover_between_hours():
    series = CloudSeries([0, 3600000, 7200000], [0, 100, 50])
    assert cloud_at(series, 1800000) == 50
    assert cloud_at(series, 7200000) == 50
    assert cloud_at(series, 9000000) is None


def test_cloud_forecast_settings():
    problems = []
    assert cloud_forecast_of({"url": "https://wx.example/v1/forecast", "label": "社内"}, problems) == replace(DEFAULT_CLOUD_FORECAST, url="https://wx.example/v1/forecast", label="社内")
    assert cloud_forecast_of(False, problems) is None
    assert cloud_forecast_of("x", problems) == DEFAULT_CLOUD_FORECAST
    assert len(problems) == 1
    assert parse_config({})[0].cloud_forecast == DEFAULT_CLOUD_FORECAST
    assert parse_config({"cloudForecast": False})[0].cloud_forecast is None


def test_places_of_optical_shots_are_asked_for_sharing_nearby_ones():
    now = utc(2026, 10, 22)
    sats = [sat(), sat(sar=True)]
    targets = [PlanTarget("a", (139.7, 35.7), []), PlanTarget("b", (139.71, 35.71), []), PlanTarget("c", (135.5, 34.7), [])]
    ops = [op(now + 3600000, satellite=0, target=0), op(now + 5400000, satellite=0, target=1),
           op(now + 3600000, satellite=1, target=2), op(now + 40 * DAY, satellite=0, target=2)]
    places = places_for(ops, sats, targets, now)
    assert len(places) == 2
    asked = []

    def fetch(url):
        asked.append(url)
        n = len(parse_qs(urlsplit(url).query)["latitude"][0].split(","))
        one = {"hourly": {"time": [now / 1000, now / 1000 + 7200], "cloud_cover": [20, 60]}}
        return json.dumps(one if n == 1 else [one] * n).encode()

    forecasts = fetch_clouds(DEFAULT_CLOUD_FORECAST, places, fetch)
    # a and b share a grid cell: one place asked for.
    assert len(asked) == 1
    assert parse_qs(urlsplit(asked[0]).query)["hourly"] == ["cloud_cover"]
    assert apply_clouds(ops, sats, targets, forecasts) == 2
    assert (ops[0].cloud, ops[1].cloud, ops[2].cloud, ops[3].cloud) == (40, 50, None, None)


# --- pairs ---------------------------------------------------------------------------------

ZERO = PlanTarget("t", (0, 0), [])


def pair_op(time, sat_ecef, **extra):
    values = dict(offNadir=20, incidence=22, elevation=60, side="right", ascending=True, range=600, tleAge=0, azimuth=90, orbitRadius=6900)
    values.update(extra)
    return op(time, sat_ecef, **values)


def test_convergence_angle_bh_and_baseline():
    x = ecef_of((0, 0))[0]
    # 500 km up, 500 km to either side: each look 45° from vertical, 90° apart.
    pair = relate(pair_op(0, (x + 500, 0, 500)), pair_op(DAY, (x + 500, 0, -500)), ZERO)
    assert pair.convergence == pytest.approx(90, abs=0.05)
    assert pair.bh == pytest.approx(2, abs=0.005)
    assert pair.days == pytest.approx(1)
    assert pair.baseline == pytest.approx(1000 * math.sqrt(0.5) * 1000, abs=50)
    assert relate(pair_op(5, (x + 500, 0, 0)), pair_op(1, (x + 500, 0, 100)), ZERO).a.time == 1


def test_stereo_pairs_of_passes():
    t = EPOCH + 3600000
    tokyo = [PlanTarget("tokyo", (139.7, 35.7), [])]
    ops = plan_access([sat()], tokyo, PlanOptions(t, t + 4 * DAY, None, "none", 10)).opportunities
    assert len(ops) > 3
    pairs = stereo_pairs(ops, [sat()], tokyo, StereoOptions(15, 60, 2, True))
    assert pairs
    for p in pairs:
        assert 15 <= p.convergence <= 60 and p.days <= 2 and p.b.time > p.a.time
    assert stereo_pairs(ops, [sat()], tokyo, StereoOptions(89, 90, 2, True)) == []


def test_no_stereo_pair_of_optical_and_sar():
    x = ecef_of((0, 0))[0]
    sats = [sat(), sat(sar=True)]
    ops = [pair_op(0, (x + 500, 0, 300)), pair_op(3600000, (x + 500, 0, -300), satellite=1)]
    assert stereo_pairs(ops, sats, [ZERO], StereoOptions(1, 90, 7, False)) == []
    assert len(stereo_pairs([ops[0], replace(ops[1], satellite=0)], sats, [ZERO], StereoOptions(1, 90, 7, False))) == 1


def test_repeat_pairs_chain_the_returns_on_one_track():
    x = ecef_of((0, 0))[0]
    sats = [sat(sar=True), replace(sat(sar=True, id="other"), name="other")]
    ops = [
        pair_op(0, (x + 500, 0, 300)),
        pair_op(DAY, (x + 500, 0, -300), incidence=40),  # another track: steeper look
        pair_op(11 * DAY, (x + 500, 0.1, 300)),
        pair_op(22 * DAY, (x + 500, 0.2, 300), ascending=False),  # going the other way
        pair_op(22 * DAY + 1000, (x + 500, 0.2, 300), azimuth=270),  # looking from the other side
        pair_op(33 * DAY, (x + 500, 0.3, 300)),
    ]
    pairs = repeat_pairs(ops, sats, [ZERO])
    assert [(p.a.time / DAY, round(p.b.time / DAY)) for p in pairs] == [(0, 11), (11, 33)]
    # 100 m apart across the line of sight.
    assert 90 < pairs[0].baseline < 110
    assert repeat_pairs(ops, sats, [ZERO], RepeatOptions(1, 2, 50, False)) == []


def test_repeat_pairs_keep_satellites_apart_unless_the_series_is_asked_for():
    x = ecef_of((0, 0))[0]
    sats = [sat(sar=True), replace(sat(sar=True, id="other"), name="other")]
    ops = [pair_op(0, (x + 500, 0, 300)), pair_op(DAY, (x + 500, 0, 300), satellite=1)]
    assert repeat_pairs(ops, sats, [ZERO]) == []
    assert len(repeat_pairs(ops, sats, [ZERO], RepeatOptions(1, 2, None, True))) == 1
    assert repeat_pairs(ops, [sat(), sat()], [ZERO]) == []


# --- revisit map ---------------------------------------------------------------------------

def test_box_around_the_targets_cut_into_cells():
    b = box_of([PlanTarget("a", (139, 35), [])])
    assert b == (138.75, 34.75, 139.25, 35.25)
    cells = grid_cells(b, 4)
    assert len(cells) == 16
    assert cells[0].center == pytest.approx((138.8125, 35.1875))
    assert cell_targets(cells)[15].label == "セル 16"


def test_chances_and_longest_wait_of_each_cell():
    h = 3600000
    ops = [pair_op(10 * h, (0, 0, 0)), pair_op(10 * h + 30000, (0, 0, 0), satellite=1), pair_op(30 * h, (0, 0, 0)), pair_op(40 * h, (0, 0, 0), target=2)]
    stats = revisit_stats(ops, 3, 0, 48 * h)
    assert stats[0] == RevisitStats(2, 20, 20)
    assert stats[1] == RevisitStats(0, None, 48)
    assert stats[2] == RevisitStats(1, None, 40)
    assert revisit_color(stats[1], 0, 48) == (148, 163, 184)
    assert revisit_color(RevisitStats(1, None, 10), 10, 48) == (22, 163, 74)
    assert revisit_color(RevisitStats(1, None, 48), 10, 48) == (220, 38, 38)
    assert hours_text(5.25) == "5.3 時間"
    assert hours_text(50) == "2 日 2 時間"
    assert hours_text(None) == "—"
