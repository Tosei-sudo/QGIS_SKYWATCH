"""
Writing one system's file at a time (tsudo, 2026-11-09: each system's file
is written by its own person): only that system's history rows and raises,
and what the others wrote earlier the same day pinned to their systems.
"""
from __future__ import annotations

from dataclasses import replace

from skywatch.core.request.daily import HELD, TAKEN
from skywatch.core.request.store import DEFAULT_HISTORY_FIELDS, HistoryRow, RequestStoreConfig, handed_where, replaced_where
from skywatch.core.request.workflow import boost_changes, handed_today, history_of, ledger_results, missed_done, not_written, plan_day
from skywatch.tests.test_request import DAY, FIRST, H, SATS, request, systems

PREVIOUS = DAY.replace(day=DAY.day - 1)


def _reqs(**r3):
    # SYS-A takes two a day (one 臨時); SYS-C takes the rest of the optical ones.
    return [
        request("R-1", priority=100, category="定点", achieve="毎回", object_id=11),
        request("R-2", priority=300, category="定点", achieve="毎回", object_id=12),
        request("R-3", priority=200, object_id=13, **r3),
        request("R-4", priority=400, object_id=14),
    ]


def test_one_systems_rows_and_raises():
    # R-4 was missed yesterday: its raise is offered today. R-5 comes last.
    missed = HistoryRow("RUN-0", FIRST - 24 * H, PREVIOUS.isoformat(), "R-4", "SYS-C", "CONST-Z 7", captured="未撮像", object_id=91)
    reqs = [*_reqs(), request("R-5", priority=600, object_id=15)]
    plan = plan_day(reqs, DAY, systems(), SATS, {}, [missed], target="SYS-C")
    by = {c.request.id: c for c in plan.candidates}
    assert {k: (c.result, c.system.system) for k, c in by.items()} == {
        "R-1": (TAKEN, "SYS-A"), "R-3": (TAKEN, "SYS-A"), "R-2": (TAKEN, "SYS-C"), "R-4": (TAKEN, "SYS-C"), "R-5": (TAKEN, "SYS-C")}
    assert not any(c.pinned for c in plan.candidates) and by["R-4"].boost_offer == 100
    # Left out by hand: R-5 is held back where it could go (SYS-A, SYS-C).
    by["R-5"].manual = HELD
    plan.refit()

    rows = history_of(plan, FIRST + H, {"SYS-C": "c.json"}, "RUN-C", system="SYS-C")
    assert [(r.request_id, r.system, r.result) for r in rows] == [("R-2", "SYS-C", TAKEN), ("R-4", "SYS-C", TAKEN), ("R-5", "SYS-C", HELD)]
    assert rows[0].file == "c.json" and rows[0].captured == "未判定" and rows[2].captured == ""
    # The ledger's 「最新の出力」 (dashboards): only the requests of SYS-C's rows, R-5 held back there.
    assert {oid: (v["lastResult"], v["lastSystem"]) for oid, v in ledger_results(plan, rows)} == {
        12: (TAKEN, "SYS-C"), 14: (TAKEN, "SYS-C"), 15: (HELD, "SYS-C")}
    # R-4's raise is SYS-C's person's to save, not SYS-A's; written for every system, as before.
    assert boost_changes(plan, system="SYS-C") == [(14, {"boost": 100})] and missed_done(plan, system="SYS-C") == [(91, {"boostDone": 1})]
    assert boost_changes(plan, system="SYS-A") == [] and missed_done(plan, system="SYS-A") == []
    assert boost_changes(plan) == [(14, {"boost": 100})]
    # SYS-C's file failing: only its own requests are not handed out.
    assert not_written(plan, {}, "SYS-C") == {"R-2", "R-4"} and not_written(plan, {}) == {"R-1", "R-2", "R-3", "R-4"}
    failed = history_of(plan, FIRST + H, {}, "RUN-C", failures={"SYS-C": "スキーマ不一致"}, system="SYS-C")
    assert (failed[0].result, failed[0].system, failed[0].reason) == (HELD, "SYS-C", "書き出し失敗（スキーマ不一致）")
    # All systems at once: every candidate, held ones without a system (as before).
    every = history_of(plan, FIRST + H, {"SYS-A": "a.csv", "SYS-C": "c.json"}, "RUN")
    assert {r.request_id: r.system for r in every} == {"R-1": "SYS-A", "R-3": "SYS-A", "R-2": "SYS-C", "R-4": "SYS-C", "R-5": ""}


def test_what_others_wrote_is_pinned():
    # SYS-C's person writes first.
    first = plan_day(_reqs(), DAY, systems(), SATS, {}, [], target="SYS-C")
    c_rows = history_of(first, FIRST + H, {"SYS-C": "c.json"}, "RUN-C", system="SYS-C")
    assert [r.request_id for r in handed_today(c_rows, DAY)] == ["R-2", "R-4"]

    # Then a request above all others comes in, and SYS-A's person works the day out: R-2 and R-4 stay in SYS-C's file
    # (taken there first); R-0 and R-1 fill SYS-A, so R-3 now goes to SYS-C too, which its person must write again.
    reqs = [request("R-0", priority=50, category="定点", achieve="毎回"), *_reqs()]
    plan = plan_day(reqs, DAY, systems(), SATS, {}, c_rows, target="SYS-A")
    by = {c.request.id: c for c in plan.candidates}
    assert {k for k, c in by.items() if c.pinned} == {"R-2", "R-4"} and by["R-2"].pinned == ("SYS-C", c_rows[0].satellite)
    assert by["R-2"].result == TAKEN and by["R-2"].system.system == "SYS-C" and by["R-2"].satellite.name == c_rows[0].satellite
    assert [k for k, c in by.items() if c.result == TAKEN and c.system.system == "SYS-A"] == ["R-0", "R-1"]
    assert by["R-3"].system.system == "SYS-C" and not by["R-3"].pinned
    assert plan.others_handed() == {("R-2", "SYS-C"), ("R-4", "SYS-C")}
    # Leaving a pinned one out by hand does nothing: it is in SYS-C's file.
    by["R-2"].manual = HELD
    plan.refit()
    assert by["R-2"].result == TAKEN
    a_rows = history_of(plan, FIRST + 2 * H, {"SYS-A": "a.csv"}, "RUN-A", system="SYS-A")
    assert [(r.request_id, r.system, r.result) for r in a_rows] == [("R-0", "SYS-A", TAKEN), ("R-1", "SYS-A", TAKEN)]

    # SYS-C's person again, after a raise put R-3 first: SYS-A's two stay there (over its limits even), R-3 goes to SYS-C,
    # and SYS-C's own earlier rows are not pinned (its file is written again).
    reqs = [request("R-0", priority=50, category="定点", achieve="毎回"), *_reqs(boost=195)]
    again = plan_day(reqs, DAY, systems(), SATS, {}, c_rows + a_rows, target="SYS-C")
    by = {c.request.id: c for c in again.candidates}
    assert {k for k, c in by.items() if c.pinned} == {"R-0", "R-1"}
    assert {k: c.system.system for k, c in by.items()} == {"R-0": "SYS-A", "R-1": "SYS-A", "R-2": "SYS-C", "R-3": "SYS-C", "R-4": "SYS-C"}
    assert again.others_handed() == {("R-0", "SYS-A"), ("R-1", "SYS-A")}
    # All systems: nothing pinned (every file written again), as before.
    again.target = None
    again.refit()
    assert not any(c.pinned for c in again.candidates) and again.others_handed() == frozenset()
    assert by["R-3"].system.system == "SYS-A"
    # Rows followed up, held back or of another day are not what a file holds.
    old = [replace(c_rows[0], captured="撮像済み"), replace(c_rows[1], day=PREVIOUS.isoformat()), replace(c_rows[0], result=HELD)]
    assert handed_today(old, DAY) == []


def test_history_conditions_of_one_system():
    store = RequestStoreConfig(label="t", url="https://example.com/x/FeatureServer/0", history="https://example.com/x/FeatureServer/1",
                               history_fields=dict(DEFAULT_HISTORY_FIELDS))
    assert replaced_where(store, "2026-11-08", "SYS-A").endswith("AND SYSTEM = 'SYS-A'")
    assert "SYSTEM" not in replaced_where(store, "2026-11-08")
    assert handed_where(store, "2026-11-08").endswith("AND RESULT = '採用'")
    assert replaced_where(store, "2026-11-08", "A'B").endswith("SYSTEM = 'A''B'")
