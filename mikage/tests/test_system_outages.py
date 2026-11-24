"""
システムの障害 (tsudo, 2026-11-24: 「4,5,7を実装して」; design 「画像の評価・システムの
障害・参考情報 設計方針案」): an outage of TBL_SATELLITE_OUTAGE may name a system of
`requestExports` (SYSTEM_NAME): 受付停止 stops every satellite of it, and its daily
limits (件数・枚数) make the system's `limit` stricter on the days it touches.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from mikage.core.plan.outages import (
    DEFAULT_OUTAGE_FIELDS, OUTAGE, OUTAGE_WARNING, SYSTEM_STOP, SYSTEM_WARNING, Outage, attach, attributes_of, day_range,
    handed_hit, matches, on_day, outage_of, outage_reason, save_outage, summary_text, system_limits, warning_of,
)
from mikage.core.request.daily import TAKEN, allocate, day_limit, gather, room_text
from mikage.core.request.passes import day_passes
from mikage.core.request.route import system_names
from mikage.core.request.simulate import SUMMARY, simulate
from mikage.core.request.systems import export_systems_of
from mikage.tests.test_plan import BELOW, T
from mikage.tests.test_request import DAY as REQUEST_DAY, request, sat
from mikage.tests.test_request_passes import DAY, SAT, along, point, system
from mikage.tests.test_satellite_outages import TYPES, setup

import pytest

from mikage.core.esri import EsriError

H = 3_600_000


def stop_of(system_name="SYS", **kw) -> Outage:
    base = dict(id="OUT-S", satellite="", system=system_name, start=T - 24 * H, stop=True)
    base.update(kw)
    return Outage(**base)


def fit(reqs, sats, systems, day=DAY, now=T):
    passes = day_passes(reqs, day, list(sats), now)
    day_sats = on_day(list(sats), day, now)
    cands = gather(reqs, day, day_sats, {}, set())
    return allocate(cands, systems, day_sats, passes, day), day_sats


def test_what_a_systems_outage_is_called():
    o = stop_of(reason="年末年始", expected=T + 48 * H)
    assert o.for_system and o.target == "システム SYS" and o.impacts() == ["受付停止"]
    assert o.blocks(True, "left", 10, 30) == "受付停止"
    limits = stop_of(stop=False, orders_per_day=3, scenes_per_day=10)
    assert limits.impact_text() == "1 日 3 件まで、1 日 10 枚まで" and limits.limits
    # A satellite's is as before.
    assert not Outage(id="O", satellite="ISS", start=T, stop=True).for_system
    assert Outage(id="O", satellite="ISS", start=T, stop=True).impacts() == ["撮像停止"]
    # Its reason: 「システムの受付停止（SYS: 年末年始、… まで）」.
    reason = outage_reason([o], T)
    assert reason.startswith(SYSTEM_STOP + "（SYS: 年末年始、") and reason.endswith(" まで）") and "受付停止、" not in reason
    assert outage_reason([o, Outage(id="O2", satellite="ISS", start=T, stop=True)], T).startswith(OUTAGE + "（")
    assert "障害中の衛星・システム: システム SYS（受付停止）" == summary_text(attach([SAT], [o], lambda _s: "SYS"), *day_range(DAY), T)


def test_which_satellites_it_hits():
    a, b = sat("OPT-X 1", "OPT-X"), sat("SAR-Y 1", "SAR-Y", sar=True, system="SYS-B")
    systems = export_systems_of([
        {"system": "SYS-A", "label": "A", "series": ["OPT-X"], "format": "csv", "columns": [{"key": "ID", "source": "id"}]},
        {"system": "SYS-B", "label": "B", "series": ["SAR-Y"], "format": "csv", "columns": [{"key": "ID", "source": "id"}]},
    ], [])
    names = system_names(systems)
    assert (names(a), names(b), names(sat("OTHER 1", "OTHER"))) == ("SYS-A", "SYS-B", "")
    hit = attach([a, b], [stop_of("sys-a")], names)
    assert hit[0].outages and not hit[1].outages
    # Without the systems: the satellite catalog's own `system`.
    assert matches(stop_of("SYS-B"), b) and not matches(stop_of("SYS-A"), a)
    assert attach([a, b], [stop_of("SYS-B")])[1].outages


def test_stopped_systems_requests_go_to_another_or_are_held_back_for_it():
    req = point(1, BELOW)
    sys_a, sys_b = system(), replace(system(), system="SYS-B", label="SYS-B")
    # Only the one system: held back, 「システムの受付停止（SYS: …）」.
    stopped = attach([SAT], [stop_of(reason="年末年始", expected=T + 48 * H)], system_names([sys_a]))
    [c], _ = fit([req], stopped, [sys_a])
    assert c.result != TAKEN and c.reason.startswith(SYSTEM_STOP + "（SYS: 年末年始、"), c.reason
    # The same satellite through another system (its own satellites in the settings): taken there.
    other = replace(SAT, name="ISS-B", id="ISS-B", system="SYS-B")
    sats = attach([SAT, other], [stop_of()], system_names([sys_a, sys_b]))
    [d], _ = fit([req], sats, [sys_a, sys_b])
    assert d.result == TAKEN and d.system.system == "SYS-B"
    # Stopped only after the day: taken, no warning.
    later = attach([SAT], [stop_of(start=T + 72 * H)], system_names([sys_a]))
    [e], _ = fit([req], later, [sys_a])
    assert e.result == TAKEN and not any(w.startswith((OUTAGE_WARNING, SYSTEM_WARNING)) for w in e.warnings)


def test_the_daily_limits_are_made_stricter_on_its_days():
    reqs = [point(1, BELOW, 300), point(4, along(120))]
    sys_ = system(limit={"count": 5})
    sats = attach([SAT], [stop_of(stop=False, orders_per_day=1)], system_names([sys_]))
    out, day_sats = fit(reqs, sats, [sys_])
    assert [x.result == TAKEN for x in out] == [True, False]
    assert day_limit(sys_, day_sats, DAY, T) == {"count": 1}
    assert room_text(out, [sys_], day_sats, DAY, T) == {"SYS": "1/1 件・1 枚"} and room_text(out, [sys_])["SYS"].startswith("1/5")
    taken = next(x for x in out if x.result == TAKEN)
    assert any(w.startswith(SYSTEM_WARNING) and "1 日 1 件まで" in w for w in taken.warnings), taken.warnings
    # Never looser than the settings' own; the day after, the settings' own.
    assert day_limit(system(limit={"count": 1}), attach([SAT], [stop_of(stop=False, orders_per_day=3)], lambda _s: "SYS"),
                     DAY, T) == {"count": 1}
    ended = attach([SAT], [stop_of(stop=False, orders_per_day=1, recovered=T + H)], lambda _s: "SYS")
    assert system_limits(ended, "SYS", DAY + timedelta(days=2), T) == {} and system_limits(ended, "SYS", DAY, T) == {"count": 1}
    assert system_limits(sats, "OTHER", DAY, T) == {}


def test_the_trial_of_limits_counts_those_held_back():
    systems_ = export_systems_of([{"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT-X"], "limit": {"count": 5},
                                   "format": "csv", "columns": [{"key": "ID", "source": "id"}]}], [])
    first, last = day_range(REQUEST_DAY)
    sats = attach([sat("OPT-X 1", "OPT-X")], [stop_of("SYS-A", start=first, recovered=last + 1)], system_names(systems_))
    run = simulate([request("R-1"), request("R-2")], REQUEST_DAY, 2, systems_, sats)
    assert [d.outage for d in run.days] == [2, 0] and [d.taken for d in run.days] == [0, 2]
    assert {label: f(run) for label, f in SUMMARY}["衛星の障害で見送り"] == 2


def test_shots_handed_to_the_system_are_only_told():
    o = stop_of()
    assert handed_hit([o], "ISS", "", "", [], T, system="SYS")[1] == "受付停止"
    assert handed_hit([o], "ISS", "", "", [], T, system="SYS-B") is None
    assert handed_hit([o], "ISS", "", "", [], T) is None
    assert handed_hit([stop_of(stop=False, orders_per_day=2)], "ISS", "", "", [], T, system="SYS") is None


def test_the_row():
    row = {"OBJECTID": 3, "OUTAGE_ID": "OUT-7", "SAT_NAME": None, "SYSTEM_NAME": " SYS-A ", "START_TIME": T, "STOP_IMAGING": 1,
           "LIMIT_ORDERS_PER_DAY": 4.2, "LIMIT_SCENES_PER_DAY": "12", "NO_ASC_LEFT": 1, "LIMIT_PER_DAY": 2, "NO_NIGHT": 1}
    o = outage_of(row, DEFAULT_OUTAGE_FIELDS)
    assert (o.system, o.satellite, o.stop, o.orders_per_day, o.scenes_per_day) == ("SYS-A", "", True, 4, 12)
    # A satellite's impacts entered on a system's row (in ArcGIS): left aside.
    assert not o.closed and o.max_per_day is None and not o.no_night
    # Neither a satellite nor a system: not an outage. Both: the satellite's (the system's limits left aside).
    assert outage_of({**row, "SYSTEM_NAME": ""}, DEFAULT_OUTAGE_FIELDS) is None
    both = outage_of({**row, "SAT_NAME": "ISS"}, DEFAULT_OUTAGE_FIELDS)
    assert not both.for_system and both.orders_per_day is None
    types = {**TYPES, "SYSTEM_NAME": "esriFieldTypeString", "LIMIT_ORDERS_PER_DAY": "esriFieldTypeInteger",
             "LIMIT_SCENES_PER_DAY": "esriFieldTypeInteger"}
    written = attributes_of(o, DEFAULT_OUTAGE_FIELDS, types, {})
    assert written["SYSTEM_NAME"] == "SYS-A" and written["SAT_NAME"] is None and written["LIMIT_ORDERS_PER_DAY"] == 4
    assert outage_of({**written, "OBJECTID": 3}, DEFAULT_OUTAGE_FIELDS) == o


def test_a_table_without_the_system_attribute_refuses_a_systems_outage():
    client, source, layer = setup()
    with pytest.raises(EsriError, match="SYSTEM_NAME"):
        save_outage(client, source, stop_of())
    assert layer.rows == []
    client2, source2, layer2 = setup({**TYPES, "SYSTEM_NAME": "esriFieldTypeString", "LIMIT_ORDERS_PER_DAY": "esriFieldTypeInteger"})
    saved = save_outage(client2, source2, stop_of(orders_per_day=2))
    assert saved.object_id == 1 and layer2.rows[0]["attributes"]["SYSTEM_NAME"] == "SYS"
