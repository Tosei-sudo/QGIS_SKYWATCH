"""Trying daily limits over the coming days (上限シミュレーション): core/request/simulate."""
from __future__ import annotations

from datetime import timedelta

from skywatch.core.request.simulate import (
    SUMMARY, SystemChange, change_of, changed_systems, simulate,
)
from skywatch.core.request.store import HistoryRow
from skywatch.core.request.systems import export_systems_of
from skywatch.tests.test_request import DAY, LAST, D, request, sat

COLS = [{"key": "ID", "source": "id"}]
SATS = [sat("OPT-X 1", "OPT-X", resolution=0.5), sat("CONST-Z 7", "CONST-Z", resolution=3.0)]


def systems(a_count=1):
    problems = []
    out = export_systems_of([
        {"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT-X"], "limit": {"count": a_count}, "format": "csv", "columns": COLS},
        {"system": "SYS-C", "label": "C", "kind": "光学", "series": ["CONST-Z"], "limit": {"count": 1}, "format": "csv", "columns": COLS},
    ], problems)
    assert problems == []
    return out


def test_changing_limits_and_turning_systems_off():
    sys_ = systems()
    changed = changed_systems(sys_, {"SYS-A": SystemChange(True, 3, 10, {"臨時": 2}), "SYS-C": SystemChange(False)})
    assert [s.system for s in changed] == ["SYS-A"]
    assert changed[0].limit == {"count": 3, "scenes": 10} and changed[0].quota == {"臨時": 2}
    # The settings' systems are left as they were; no limit given is no limit.
    assert sys_[0].limit == {"count": 1}
    assert changed_systems(sys_, {"SYS-A": SystemChange(True, None, None)})[0].limit == {}
    assert change_of(sys_[0]) == SystemChange(True, 1, None, {})


def test_days_raises_carry_and_ledger_untouched():
    # One system, one a day: R-1 goes first; R-2 (2 days left) is raised by 100 each day until it goes;
    # the 毎回 R-3 loses the days it does not fit.
    only_a = changed_systems(systems(), {"SYS-C": SystemChange(False)})
    reqs = [request("R-1", priority=300, end=LAST + D), request("R-2", priority=500, end=LAST + D),
            request("R-3", priority=700, category="定点", achieve="毎回")]
    run = simulate(reqs, DAY, 3, only_a, SATS)
    taken = [d.taken for d in run.days]
    assert taken == [1, 1, 1]
    assert [d.over for d in run.days] == [2, 1, 0]
    assert [d.raised for d in run.days] == [2, 1, 0]   # R-1 is 2 days from its deadline too
    assert [d.lost for d in run.days] == [1, 1, 0]
    assert [d.carried for d in run.days] == [1, 0, 0]
    assert run.total("expired") == 0 and run.days[0].used == {"SYS-A": (1, 2)}   # Tokyo takes 2 scenes of 10 km
    # The requests given are not changed (nothing is written).
    assert [(r.status, r.boost) for r in reqs] == [("有効", 0)] * 3
    summary = {label: f(run) for label, f in SUMMARY}
    assert summary["採用"] == 3 and summary["欠測（毎回）"] == 2


def test_deadline_missed_and_spill_to_the_next_system():
    reqs = [request("R-1", priority=100), request("R-2", priority=300, end=LAST)]
    # SYS-A takes one, R-2 goes on to SYS-C.
    run = simulate(reqs, DAY, 1, systems(), SATS)
    assert run.days[0].taken == 2 and run.days[0].spilled == 1 and run.days[0].used == {"SYS-A": (1, 2), "SYS-C": (1, 2)}
    # Without SYS-C there is no room: R-2's last day is today, so it misses its deadline.
    run = simulate(reqs, DAY, 2, changed_systems(systems(), {"SYS-C": SystemChange(False)}), SATS)
    assert [e.request.id for e in run.expired] == ["R-2"] and run.expired[0].reason == "枠超過"
    assert run.days[1].candidates == 0
    # A bigger limit for SYS-A takes both.
    run = simulate(reqs, DAY, 1, changed_systems(systems(), {"SYS-A": SystemChange(True, 2), "SYS-C": SystemChange(False)}), SATS)
    assert run.days[0].taken == 2 and run.total("expired") == 0


def test_weather_and_missed_history():
    reqs = [request("R-1", priority=500, cloud_no_go=50, end=LAST + 9 * D)]
    clouds = {DAY: {"R-1": 80}, DAY + timedelta(days=1): {"R-1": 10}}
    run = simulate(reqs, DAY, 2, systems(), SATS, clouds=lambda d: clouds.get(d, {}))
    assert [d.cloud for d in run.days] == [1, 0] and [d.taken for d in run.days] == [0, 1]
    # A request found 未撮像 is offered a raise on the first day only.
    hist = [HistoryRow(run_id="X", run_date=0, day="2026-11-07", request_id="R-1", system="SYS-A", satellite="", priority="500",
                       cloud=None, scenes=1, result="採用", reason="", file="", captured="未撮像", boost_done=0)]
    run = simulate([request("R-1", priority=500, category="定点", achieve="毎回")], DAY, 2, systems(), SATS, history=hist)
    assert [d.raised for d in run.days] == [1, 0]
