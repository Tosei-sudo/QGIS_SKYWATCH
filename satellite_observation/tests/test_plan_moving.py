"""The viewer's imaging-plan-moving.test.ts and moving-target.test.ts, ported."""
import math
from dataclasses import replace

import pytest

from satellite_observation.core.plan.access import PlanOptions, plan_access
from satellite_observation.core.plan.moving import (
    DEFAULT_UNCERTAINTY, Report, ShipState, UncertaintyModel, circle_target, destination, guess_speed_unit,
    guess_target_fields, plan_ships, predicted_places, predicted_track, refine_shots, sample_targets, ship_at,
    ship_plan, ship_window, targets_of, time_of, uncertainty_at,
)

from .test_plan import BELOW, T, iss

H = 3600000


def km_between(a, b):
    r = math.pi / 180
    h = math.sin((b[1] - a[1]) * r / 2) ** 2 + math.cos(a[1] * r) * math.cos(b[1] * r) * math.sin((b[0] - a[0]) * r / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def ship(**kw):
    base = dict(id="a", name="a", t=0, lon=139, lat=35, course=0, speed=10)
    base.update(kw)
    return ShipState(**base)


# --- dead reckoning -------------------------------------------------------------------------

def test_carries_the_ship_on():
    at = ship_at(ship(), 3 * H)
    assert at[0] == pytest.approx(139, abs=1e-6)
    assert km_between((139, 35), at) == pytest.approx(55.56, abs=0.5)
    assert ship_at(ship(speed=None), 3 * H) == (139, 35)
    assert destination((179.9, 0), 90, 50)[0] < -179


def test_grows_the_circle():
    s = ship()
    assert uncertainty_at(s, 0) == 0.5
    assert uncertainty_at(s, 10 * H) == pytest.approx(0.5 + 10 * (1.852 + 0.5))
    circle = circle_target(s, 2 * H)
    assert len(circle.points) == 24
    assert len(circle.cells) > 50
    assert circle.uncertainty == pytest.approx(uncertainty_at(s, 2 * H))
    assert circle.guessed == 2 * H
    for p in circle.points:
        assert km_between(circle.center, p) == pytest.approx(circle.uncertainty, abs=0.05)


def test_samples_every_hour_to_the_horizon():
    ships = [ship(), ship(id="b", name="b", t=-70 * H, lon=135, lat=34, course=90, speed=5)]
    span = ship_window(ships, 0, 10 * 24 * H)
    assert span == (0, 72 * H)
    targets, samples = sample_targets(ships, *span)
    assert len([s for s in samples if s[0] == 0]) == 73
    assert [s[1] for s in samples if s[0] == 1] == [0, H, 2 * H]
    assert targets[1].center[1] > 35


# --- planning -------------------------------------------------------------------------------

def test_aims_the_pass_where_the_ship_will_be():
    meet = (BELOW[0] + 150 / (111.32 * math.cos(BELOW[1] * math.pi / 180)), BELOW[1])
    lon, lat = destination(meet, 180, 12 * 1.852 * 5)
    s = ShipState("s", "SHIP", T - 5 * H, lon, lat, 0, 12)
    options = PlanOptions(T - 3 * H, T + 3 * H, None, "none", 10)
    targets, samples = sample_targets([s], options.start, options.end)
    first = plan_access([iss()], targets, options)
    shots = refine_shots(first, samples, [s], [iss()], options)
    near = [x for x in shots if abs(x.op.time - T) < 10 * 60000]
    assert len(near) == 1
    shot = near[0]
    assert km_between(shot.target.center, meet) < 5
    assert shot.target.uncertainty == pytest.approx(uncertainty_at(s, shot.op.time), abs=1e-3)
    assert shot.op.coverage > 0
    circles, plan = ship_plan(shots, first)
    assert len(circles) == len(shots)
    assert [o.target for o in plan.opportunities] == list(range(len(shots)))


def test_plan_ships_both_steps():
    lon, lat = destination(BELOW, 180, 12 * 1.852 * 5)
    s = ShipState("s", "SHIP", T - 5 * H, lon, lat, 0, 12)
    out = plan_ships([iss()], [s], PlanOptions(T - 3 * H, T + 3 * H, None, "none", 10))
    circles, result, planned = out
    assert any(abs(o.time - T) < 10 * 60000 for o in result.opportunities)
    assert all(c.uncertainty is not None for c in circles)
    assert planned.end == T + 3 * H
    # Reports older than the horizon: nothing to plan.
    assert isinstance(plan_ships([iss()], [replace(s, t=T - 100 * H)], PlanOptions(T, T + H, None, "none", 10)), str)


# --- targets from a layer -------------------------------------------------------------------

T0 = 1792800000000  # 2026-10-24T00:00:00Z


def report(attributes, lon_lat):
    return Report(attributes, lon_lat)


def test_guess_target_fields():
    f = guess_target_fields(["mmsi", "timestamp", "heading", "sog", "shipname"])
    assert f == {"id": "mmsi", "time": "timestamp", "course": "heading", "speed": "sog", "name": "shipname"}
    assert guess_speed_unit("sog") == "kn"
    assert guess_speed_unit("speed_kmh") == "kmh"
    assert guess_speed_unit("speed_ms") == "ms"


FIELDS = {"id": "TRACK_NO", "time": "OBS_TIME", "course": "HDG", "speed": "SPD", "name": "NAME"}


def test_targets_at_last_report_in_knots():
    reports = [
        report({"TRACK_NO": "A", "OBS_TIME": "2026-10-24T00:00:00.000Z", "HDG": 90, "SPD": 18.52, "NAME": "Alpha"}, (139, 35)),
        report({"TRACK_NO": "A", "OBS_TIME": "2026-10-24T01:00:00.000Z", "HDG": 45, "SPD": 37.04, "NAME": "Alpha"}, (139.2, 35)),
        report({"TRACK_NO": 7, "OBS_TIME": T0, "HDG": 511, "SPD": 0}, (140, 36)),
    ]
    targets = targets_of(reports, FIELDS, "kmh", DEFAULT_UNCERTAINTY, T0)
    assert len(targets) == 2
    a = next(t for t in targets if t.id == "A")
    assert a.t == T0 + H and a.course == 45 and a.speed == pytest.approx(20) and a.name == "Alpha" and a.lon == 139.2
    b = next(t for t in targets if t.id == "7")
    assert b.course is None and b.name == "7"


def test_course_and_speed_from_two_reports():
    reports = [report({"TRACK_NO": "X", "OBS_TIME": T0}, (139, 35)), report({"TRACK_NO": "X", "OBS_TIME": T0 + 6 * H}, (139, 36))]
    [x] = targets_of(reports, {"id": "TRACK_NO", "time": "OBS_TIME"}, "kn", DEFAULT_UNCERTAINTY)
    assert x.course == pytest.approx(0, abs=1e-3)
    assert x.speed == pytest.approx(111.195 / 1.852 / 6, abs=0.05)


def test_reports_without_id():
    assert targets_of([report({"SPD": 3}, (0, 0))], FIELDS, "kn", DEFAULT_UNCERTAINTY) == []


def test_predicted_places():
    model = UncertaintyModel(1, 0, 2)
    [target] = targets_of([report({"id": "S", "time": T0, "course": 0, "speed": 10}, (139, 35))],
                          {"id": "id", "time": "time", "course": "course", "speed": "speed"}, "kn", model)
    places = predicted_places([target], -math.inf, 12, 6)
    assert [(p.t - T0) / H for p in places] == [0, 6, 12]
    assert [p.radius for p in places] == [1, 13, 25]
    assert places[2].center[1] == pytest.approx(35 + 222.24 / 111.195, abs=0.01)
    assert places[0].ring[0] == places[0].ring[-1]
    assert len(predicted_track(target, 12)) == 13


def test_time_of():
    assert time_of(T0) == T0
    assert time_of(T0 / 1000) == T0
    assert time_of("2026-10-24 00:00:00") == T0
    assert time_of("2026-10-24 09:00:00 +0900") == T0
    assert time_of("2026-10-24 00:00:00.123456789 +0000 UTC") == T0 + 123
    assert time_of("20261024T000000Z") == T0
    assert time_of("2026年10月24日 0時") == T0
    assert time_of("2026-02-31") is None
    assert time_of("abc") is None
