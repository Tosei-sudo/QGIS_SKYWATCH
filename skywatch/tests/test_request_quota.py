"""
年間撮像上限 (tsudo, 2026-11-11: 「センサー、モードごとに、年間撮像枚数の上限が
あるので、それを管理したい」「当初年は現在の消費枚数を設定したい」「9月から
1年とかあるので、ウィンドウは変えられるように」「全部あるわけではない」「撮って
もノイズとかジャミングでカウントされない場合もある」「外部の画像カタログには書き
込めないので、こちらで完結できることが必須」「個の画像に関して管理する情報は
ショットのほうがいい」「撮像済みは、ショット側にも持たせたら」): the quotas'
years, what a year has used per shot, the day's fitting and the simulation
kept within them, and the shots told whether they were taken.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from skywatch.core.esri import EsriClient, EsriError
from skywatch.core.request.daily import QUOTA, QUOTA_OVER, TAKEN, allocate, gather
from skywatch.core.request.follow import Finding, save_marked
from skywatch.core.request.model import JST
from skywatch.core.request.passes import day_passes
from skywatch.core.request.quota import (
    MISSED, NO_ROWS, PENDING, REJECTED, USED, Quota, QuotaBook, QuotaData, quotas_of, read_quota_data, shot_state, usage_of, window_of,
)
from skywatch.core.request.simulate import SUMMARY, simulate
from skywatch.core.request.store import (
    DEFAULT_QUOTA_FIELDS, DEFAULT_SHOT_FIELDS, HistoryRow, ShotRow, followed_where, load_history, request_store_of, shot_outcome,
    sync_shots, update_shots,
)
from skywatch.core.request.workflow import plan_day
from skywatch.tests.fake_esri import FakeEsri, FakeLayer
from skywatch.tests.test_plan import BELOW, T
from skywatch.tests.test_request import DAY as REQ_DAY, HISTORY, LEDGER, LAST, D, ledger_layers, request, sat
from skywatch.tests.test_request_passes import DAY, SAT, along, point, system

SHOTS = "https://gis.example/rest/services/R/FeatureServer/22"
QUOTAS = "https://gis.example/rest/services/R/FeatureServer/23"
F = DEFAULT_QUOTA_FIELDS


def _ms(day: str) -> float:
    """The JST midnight of a day (a date attribute)."""
    return datetime.fromisoformat(day).replace(tzinfo=JST).timestamp() * 1000


def _shot(shot_id, day, scenes=1, captured="未判定", **kw) -> ShotRow:
    return ShotRow(shot_id, "RUN", 0, day, system="SYS", satellite=kw.pop("satellite", "OPT 1"), scenes=scenes, captured=captured, **kw)


def _row(rid, shot_id, captured="未判定", **kw) -> HistoryRow:
    return HistoryRow(run_id="RUN", run_date=0, day=kw.pop("day", "2026-11-08"), request_id=rid, name=rid, system="SYS",
                      captured=captured, shot_id=shot_id, **kw)


# ---------------------------------------------------------------- the quotas' years and rows

def test_a_year_starting_in_any_month():
    w = window_of(date(2026, 11, 11), 9)
    assert (w.first, w.last, w.text) == (date(2026, 9, 1), date(2027, 8, 31), "2026/9/1〜2027/8/31")
    assert window_of(date(2026, 8, 31), 9).first == date(2025, 9, 1)
    assert window_of(date(2026, 9, 1), 9).first == date(2026, 9, 1)
    assert window_of(date(2026, 11, 11)).text == "2026/1/1〜2026/12/31"
    assert window_of(date(2026, 2, 3), 13).first == date(2026, 1, 1)
    assert w.has("2027-08-31") and not w.has("2027-09-01") and not w.has("") and w.before().first == date(2025, 9, 1)


def test_the_rows_of_the_table_and_what_cannot_be_used():
    problems = []
    rows = [
        {F["sensor"]: "WV110", F["mode"]: "パン", F["limit"]: 500, F["startMonth"]: 9, F["initialUsed"]: 300,
         F["countFrom"]: _ms("2026-10-01"), "OBJECTID": 3},
        {F["sensor"]: "WV110", F["limit"]: "800"},
        {F["sensor"]: "SAR-X", F["startMonth"]: 13, F["initialUsed"]: 20},
        {F["sensor"]: "wv-110", F["mode"]: "パン", F["limit"]: 1},
        {F["sensor"]: "", F["limit"]: 5},
    ]
    quotas = quotas_of(rows, F, problems)
    assert [(q.label, q.limit, q.start_month) for q in quotas] == [
        ("SAR-X（全モード）", None, 1), ("wv-110（パン）", 1, 1), ("WV110（全モード）", 800, 1), ("WV110（パン）", 500, 9)]
    pan = quotas[3]
    assert (pan.initial_used, pan.count_from, pan.object_id) == (300, date(2026, 10, 1), 3)
    # A month past 12 is January; an initial count with no day it was counted to is not counted (said so).
    assert quotas[0].initial_used == 0
    assert problems == ["年間撮像上限「SAR-X（全モード）」の年の始まりの月は 1〜12 で（1 月として数える）",
                        "年間撮像上限「SAR-X（全モード）」の当初の消費枚数には数え始める日が必要（当初の消費枚数は数えない）",
                        "年間撮像上限「wv-110（パン）」の行が 2 つ以上（どちらの上限も守る）"]


def test_which_satellites_and_shots_count_against_a_quota():
    whole, pan = Quota("WV110"), Quota("WV110", "パン")
    wv3 = sat("WorldView-3", "WorldView", mode="パン")
    assert not whole.matches_satellite(wv3)
    wv3 = replace(wv3, sensor="WV110")
    assert whole.matches_satellite(wv3) and pan.matches_satellite(wv3)
    assert whole.matches_satellite(replace(wv3, mode="マルチ")) and not pan.matches_satellite(replace(wv3, mode="マルチ"))
    # A satellite without a sensor counts against a quota named after it.
    assert Quota("opt-1").matches_satellite(sat("OPT 1", "OPT")) and Quota("OPT 1").matches_shot(_shot("X", "2026-11-08"))
    assert Quota("WV110", "パン").matches_shot(_shot("X", "2026-11-08", sensor="wv110", mode="パン"))


# ---------------------------------------------------------------- what a year has used

def test_what_became_of_each_shot():
    shot = _shot("X-1", "2026-11-08")
    assert shot_state(shot, [_row("A", "X-1", "撮像済み"), _row("B", "X-1", "未撮像")]) == USED
    assert shot_state(shot, [_row("A", "X-1"), _row("B", "X-1", "未撮像")]) == PENDING
    assert shot_state(shot, [_row("A", "X-1", "未撮像"), _row("B", "X-1", adoption="不採用")]) == MISSED
    assert shot_state(shot, [_row("A", "X-1", adoption="不採用")]) == REJECTED
    # Settled on the shot itself (its rows are not read then); no rows naming it any more: counted nowhere.
    assert shot_state(replace(shot, captured="撮像済み")) == USED and shot_state(replace(shot, captured="未撮像")) == MISSED
    assert shot_state(shot) == NO_ROWS and shot_state(shot, looked=False) == PENDING


def test_the_year_used_with_the_first_count_the_uncounted_and_the_pending():
    q = Quota("OPT 1", limit=20, start_month=9, initial_used=6, count_from=date(2026, 10, 1))
    shots = [
        _shot("old", "2026-08-30", 5, "撮像済み"),                 # the year before
        _shot("before", "2026-09-20", 4, "撮像済み"),              # in INITIAL_USED
        _shot("taken", "2026-10-02", 3, "撮像済み", uncounted=1, uncounted_reason="ノイズ"),
        _shot("noise", "2026-10-03", 2, "撮像済み", uncounted=5),  # never more than it has
        _shot("open", "2026-11-08", 2),
        _shot("gone", "2026-11-08", 7),                            # its rows were written again
        _shot("missed", "2026-10-05", 4, "未撮像"),
        _shot("other", "2026-10-05", 9, "撮像済み", satellite="SAR 2"),
    ]
    rows = {"open": [_row("A", "open")]}
    u = usage_of(q, q.window(date(2026, 11, 8)), shots, rows)
    assert (u.initial, u.used, u.uncounted, u.pending, u.consumed, u.remaining) == (6, 2, 3, 2, 8, 10)
    assert [(s.shot.shot_id, s.state) for s in u.shots] == [
        ("taken", USED), ("noise", USED), ("missed", MISSED), ("gone", NO_ROWS), ("open", PENDING)]
    assert u.text() == "OPT 1（全モード）: 年間 20 枚のうち 消費 8（当初 6）・見込み 2・数えない 3・残り 10（2026/9/1〜2027/8/31）"
    assert u.text(12) == "OPT 1（全モード）: 年間 20 枚のうち 消費 8（当初 6）・見込み 2・今回 12・数えない 3・2 枚超過（2026/9/1〜2027/8/31）"
    assert not u.low() and u.low(9)
    # The day planned again: its open shots are not 見込み (they are fitted again).
    again = usage_of(q, q.window(date(2026, 11, 8)), shots, rows, ("2026-11-08", "2026-11-08"))
    assert again.pending == 0 and [s.replanned for s in again.shots if s.shot.shot_id == "open"] == [True]
    # The next year starts from nothing (the first count was in the year before).
    nxt = usage_of(q, q.window(date(2027, 9, 1)), shots + [_shot("new", "2027-09-02", 1, "撮像済み")])
    assert (nxt.initial, nxt.used, nxt.remaining) == (0, 1, 19)
    # Not limited: counted all the same.
    free = usage_of(Quota("OPT 1"), window_of(date(2026, 11, 8)), shots, rows)
    assert free.remaining is None and free.text().startswith("OPT 1（全モード）: 上限なし・消費 ")


def test_the_book_of_the_day():
    data = QuotaData(quotas=[Quota("OPT 1", limit=5), Quota("OPT 1", "広域"), Quota("SAR 2", limit=1)],
                     shots=[_shot("a", "2026-11-01", 3, "撮像済み")])
    book = data.book(date(2026, 11, 8))
    assert [u.quota.label for u in book.usages] == ["OPT 1（全モード）", "SAR 2（全モード）"] and book.indices == [0, 2]
    opt = sat("OPT 1", "OPT")
    assert book.over(opt, 2) is None and book.over(opt, 3) is book.usages[0] and book.over(None, 9) is None
    book.take(opt, 2)
    assert book.left(book.usages[0]) == 0 and book.over(opt, 1) is book.usages[0]
    assert book.over_text(book.usages[0]) == f"{QUOTA_OVER}OPT 1（全モード） 残り 0 枚・手動で採用）"
    assert book.texts([opt]) == [("OPT 1（全モード）: 年間 5 枚のうち 消費 3・今回 2・残り 0（2026/1/1〜2026/12/31）", True)]
    book.reset()
    assert book.left(book.usages[0]) == 2
    # What a simulation handed out on the days before.
    assert data.book(date(2026, 11, 8), extra={(0, date(2026, 1, 1)): 2}).left(book.usages[0]) == 2
    assert data.book(date(2026, 11, 8), extra={(0, date(2026, 1, 1)): 2}).usages[0].remaining == 0


# ---------------------------------------------------------------- the day's fitting

def _fit(reqs, quotas, sats=(SAT,), cands=None):
    systems = [system()]
    passes = day_passes(reqs, DAY, list(sats))
    cands = cands if cands is not None else gather(reqs, DAY, list(sats), {}, set())
    return allocate(cands, systems, list(sats), passes, DAY, quotas), passes


def test_the_fitting_stops_at_what_is_left_but_for_one_taken_by_hand():
    a, far = point(1, BELOW, 300), point(4, along(120))
    book = QuotaData(quotas=[Quota("ISS", limit=1)]).book(DAY)
    out, _ = _fit([a, far], book)
    c = {x.request.id: x for x in out}
    assert c["R1"].result == TAKEN and c["R4"].result != TAKEN and c["R4"].reason == QUOTA
    assert book.texts([SAT]) == [(f"ISS（全モード）: 年間 1 枚のうち 消費 0・今回 1・残り 0（{book.usages[0].window.text}）", True)]
    # Taken by hand: it goes past the quota, warned.
    c["R4"].manual = TAKEN
    out, _ = _fit([a, far], book, cands=out)
    c = {x.request.id: x for x in out}
    assert c["R4"].result == TAKEN and any(w.startswith(QUOTA_OVER) for w in c["R4"].warnings)
    assert book.left(book.usages[0]) == -1
    # The warning goes once the hand is taken off again (and the request is held again).
    c["R4"].manual = None
    out, _ = _fit([a, far], book, cands=out)
    c = {x.request.id: x for x in out}
    assert c["R4"].reason == QUOTA and not any(w.startswith(QUOTA_OVER) for w in c["R4"].warnings)
    # Enough left: both go; no quota: as before.
    assert all(x.result == TAKEN for x in _fit([a, far], QuotaData(quotas=[Quota("ISS", limit=2)]).book(DAY))[0])
    assert all(x.result == TAKEN for x in _fit([a, far], None)[0])


def test_a_mode_used_up_gives_way_to_another():
    wide, fine = replace(SAT, mode="広域", sensor="ISS-CAM"), replace(SAT, mode="高分解能", sensor="ISS-CAM", resolution=0.5)
    reqs = [point(1, BELOW)]
    out, _ = _fit(reqs, QuotaData(quotas=[Quota("iss cam", "高分解能", limit=0)]).book(DAY), sats=(fine, wide))
    assert out[0].result == TAKEN and out[0].satellite.mode == "広域"
    out, _ = _fit(reqs, QuotaData(quotas=[Quota("iss cam", limit=0)]).book(DAY), sats=(fine, wide))
    assert out[0].result != TAKEN and out[0].reason == QUOTA


def test_plan_day_takes_the_book():
    a, far = point(1, BELOW, 300), point(4, along(120))
    book = QuotaData(quotas=[Quota("ISS", limit=1)]).book(DAY)
    plan = plan_day([a, far], DAY, [system()], [SAT], {}, passes=day_passes([a, far], DAY, [SAT]), quotas=book)
    assert [c.reason for c in plan.candidates if c.result != TAKEN] == [QUOTA] and plan.quotas is book


def test_the_simulation_keeps_the_quota_over_the_days():
    from skywatch.core.request.systems import export_systems_of
    [sys_a] = export_systems_of([{"system": "SYS-A", "label": "A", "kind": "光学", "series": ["OPT-X"], "limit": {"count": 5},
                                  "format": "csv", "columns": [{"key": "ID", "source": "id"}]}], [])
    sats = [sat("OPT-X 1", "OPT-X", resolution=0.5)]
    reqs = [request("R-1", category="定点", achieve="毎回", priority=300, end=LAST + 5 * D)]
    # Tokyo takes 2 scenes of 10 km: 2 on the first day, 2 on the second, the third has 1 left.
    data = QuotaData(quotas=[Quota("OPT-X 1", limit=5)])
    run = simulate(reqs, REQ_DAY, 3, [sys_a], sats, quotas=data)
    assert [d.taken for d in run.days] == [1, 1, 0] and [d.quota for d in run.days] == [0, 0, 1]
    assert dict((label, f(run)) for label, f in SUMMARY)["年間上限で見送り"] == 1
    # A new year starts from nothing (the year of December 31 against that of January 1).
    reqs = [request("R-1", category="定点", achieve="毎回", priority=300, start=_ms("2026-12-01"), end=_ms("2027-01-10"))]
    run = simulate(reqs, date(2026, 12, 30), 4, [sys_a], sats, quotas=QuotaData(quotas=[Quota("OPT-X 1", limit=3)]))
    assert [d.taken for d in run.days] == [1, 0, 1, 0]


# ---------------------------------------------------------------- the shots told whether they were taken

def _services(shot_fields=None):
    fake = ledger_layers(FakeEsri())
    types = {n: "esriFieldTypeString" for n in DEFAULT_SHOT_FIELDS.values()}
    types.update(RUN_DATE="esriFieldTypeDate", TARGET_DATE="esriFieldTypeDate", PASS_TIME="esriFieldTypeDate", SCENES="esriFieldTypeInteger",
                 SHOT_COUNT="esriFieldTypeInteger", REQ_COUNT="esriFieldTypeInteger", UNCOUNTED_SCENES="esriFieldTypeInteger")
    if shot_fields is not None:
        types = {k: v for k, v in types.items() if k in shot_fields}
    fake.add_layer(SHOTS, FakeLayer(types, "esriGeometryPolygon"))
    fake.add_layer(QUOTAS, FakeLayer({"SENSOR": "esriFieldTypeString", "SENSOR_MODE": "esriFieldTypeString", "ANNUAL_LIMIT": "esriFieldTypeInteger",
                                      "YEAR_START_MONTH": "esriFieldTypeSmallInteger", "INITIAL_USED": "esriFieldTypeInteger",
                                      "COUNT_FROM": "esriFieldTypeDate", "NOTE": "esriFieldTypeString"}, "esriGeometryNone"))
    problems = []
    store = request_store_of({"label": "依頼", "url": LEDGER, "history": HISTORY, "shots": SHOTS, "quotas": QUOTAS}, problems, 0)
    assert problems == [] and store.quotas == QUOTAS
    return fake, store


def _history(fake, rid, shot_id, captured="未判定", day="2026-11-08", **kw):
    fake.layers[HISTORY].add({"RUN_ID": "RUN", "REQ_ID": rid, "REQ_NAME": rid, "TARGET_DAY": day, "SYSTEM": "SYS", "RESULT": "採用",
                              "CAPTURED": captured, "SHOT_ID": shot_id, **kw})


def _shot_row(fake, shot_id, day="2026-11-08", scenes=1, captured="未判定", **kw):
    return fake.layers[SHOTS].add({"SHOT_ID": shot_id, "RUN_ID": "RUN", "TARGET_DAY": day, "SATELLITE": "OPT 1", "SCENES": scenes,
                                   "CAPTURED": captured, **kw})


def test_the_store_reads_the_quota_table():
    problems = []
    store = request_store_of({"url": LEDGER, "shots": SHOTS, "quotas": "nowhere", "quotaFields": {"limit": "LIMIT_A"}}, problems, 0)
    assert store.quotas is None and store.quota_fields["limit"] == "LIMIT_A" and "quotas" in problems[0]
    problems = []
    assert request_store_of({"url": LEDGER, "quotas": QUOTAS}, problems, 0).quotas == QUOTAS
    assert problems and "shots" in problems[0]
    store = request_store_of({"url": LEDGER, "history": HISTORY}, [], 0)
    assert followed_where(store, "2026-11-08", "SYS") == (
        "TARGET_DAY = '2026-11-08' AND CAPTURED IN ('撮像済み', '未撮像') AND SYSTEM = 'SYS'")


def test_a_shot_is_taken_when_any_of_its_requests_is():
    assert shot_outcome([_row("A", "X", "未撮像"), _row("B", "X", "撮像済み", image_id="I-2", merged_into="A"),
                         _row("C", "X", "撮像済み", image_id="I-1")]) == ("撮像済み", "I-1")
    assert shot_outcome([_row("A", "X", "未撮像"), _row("B", "X", "未撮像")]) == ("未撮像", "")
    assert shot_outcome([_row("A", "X", "未撮像"), _row("B", "X")]) == ("未判定", "")
    assert shot_outcome([_row("A", "X", "未撮像", result="見送り")]) == ("未判定", "")
    fake, store = _services()
    client = EsriClient(fake)
    _history(fake, "R1", "X-1", "撮像済み", IMAGE_ID="IMG-1")
    _history(fake, "R2", "X-1", "未撮像", MERGED_INTO="R1")
    _history(fake, "R4", "X-2", "未撮像")
    _history(fake, "R5", "X-3")
    for i in (1, 2, 3, 4):
        _shot_row(fake, f"X-{i}")
    saved = sync_shots(client, store, None, ["X-1", "X-2", "X-3", "X-4", "", "X-1"])
    assert saved == 2 and not saved.problems
    got = {r["attributes"]["SHOT_ID"]: (r["attributes"]["CAPTURED"], r["attributes"].get("IMAGE_ID")) for r in fake.layers[SHOTS].rows}
    assert got == {"X-1": ("撮像済み", "IMG-1"), "X-2": ("未撮像", None), "X-3": ("未判定", None), "X-4": ("未判定", None)}
    # Nothing to change the second time.
    assert sync_shots(client, store, None, ["X-1", "X-2"]) == 0
    # A layer without those attributes (made before them) is left alone.
    fake, store = _services(shot_fields={"OBJECTID", "SHOT_ID", "TARGET_DAY"})
    _history(fake, "R1", "X-1", "撮像済み")
    fake.layers[SHOTS].add({"SHOT_ID": "X-1", "TARGET_DAY": "2026-11-08"})
    assert sync_shots(EsriClient(fake), store, None, ["X-1"]) == 0


def test_marked_by_hand_the_shot_follows():
    fake, store = _services()
    client = EsriClient(fake)
    _history(fake, "R1", "X-1")
    _shot_row(fake, "X-1")
    [row] = load_history(client, store, None, "1=1")
    problems = []
    save_marked(client, store, None, [Finding(row, "撮像済み", "IMG-9")], {}, [row], T, problems)
    assert problems == []
    a = fake.layers[SHOTS].rows[0]["attributes"]
    assert (a["CAPTURED"], a["IMAGE_ID"]) == ("撮像済み", "IMG-9")


def test_a_shot_layer_that_fails_does_not_stop_the_follow_up():
    fake, store = _services()
    client = EsriClient(fake)
    _history(fake, "R1", "X-1")
    del fake.layers[SHOTS]
    [row] = load_history(client, store, None, "1=1")
    problems = []
    save_marked(client, store, None, [Finding(row, "未撮像")], {}, [row], T, problems)
    assert fake.layers[HISTORY].rows[0]["attributes"]["CAPTURED"] == "未撮像"
    assert len(problems) == 1 and problems[0].startswith("撮像の範囲の撮像結果を更新できず")


def test_uncounted_scenes_are_kept_on_the_shot():
    fake, store = _services()
    client = EsriClient(fake)
    oid = _shot_row(fake, "X-1", scenes=3, captured="撮像済み")
    saved = update_shots(client, store, None, [(oid, {"uncounted": 2, "uncountedReason": "ジャミング"})])
    assert saved == 1
    a = fake.layers[SHOTS].rows[0]["attributes"]
    assert (a["UNCOUNTED_SCENES"], a["UNCOUNTED_REASON"]) == (2, "ジャミング")
    fake, store = _services(shot_fields={"OBJECTID", "SHOT_ID", "TARGET_DAY"})
    with pytest.raises(EsriError, match="UNCOUNTED_SCENES"):
        update_shots(EsriClient(fake), store, None, [(1, {"uncounted": 1})])


def test_reading_the_quotas_and_the_shots_of_their_years():
    fake, store = _services()
    client = EsriClient(fake)
    fake.layers[QUOTAS].add({"SENSOR": "OPT 1", "ANNUAL_LIMIT": 10, "YEAR_START_MONTH": 9, "INITIAL_USED": 4, "COUNT_FROM": _ms("2026-10-01")})
    fake.layers[QUOTAS].add({"SENSOR": "SAR 2", "SENSOR_MODE": "スポット"})
    _shot_row(fake, "taken", "2026-11-01", 2, "撮像済み", UNCOUNTED_SCENES=1, UNCOUNTED_REASON="ノイズ")
    _shot_row(fake, "open", "2026-11-09", 3)
    _shot_row(fake, "before", "2026-09-15", 5, "撮像済み")
    _shot_row(fake, "last-year", "2026-08-20", 7, "撮像済み")
    _history(fake, "R1", "open", day="2026-11-09")
    data = read_quota_data(client, store, None, [date(2026, 11, 10)])
    assert [q.label for q in data.quotas] == ["OPT 1（全モード）", "SAR 2（スポット）"] and data.problems == []
    # The shots from the earliest year's start (SAR 2's year is the calendar year); the history only for the open one.
    assert {s.shot_id for s in data.shots} == {"taken", "open", "before", "last-year"} and list(data.rows) == ["open"]
    [u, _] = data.usages(date(2026, 11, 10))
    assert (u.consumed, u.uncounted, u.pending, u.remaining) == (5, 1, 3, 2)
    # Planning 2026-11-09 again: its open shot is fitted again, not 見込み.
    assert data.book(date(2026, 11, 9)).usages[0].remaining == 5
    # No quota rows: nothing else is read; no shot layer: only the first count (said so).
    fake.layers[QUOTAS].rows = []
    assert read_quota_data(client, store, None, [date(2026, 11, 10)]).shots == []
    fake.layers[QUOTAS].add({"SENSOR": "OPT 1", "ANNUAL_LIMIT": 10})
    data = read_quota_data(client, replace(store, shots=None), None, [date(2026, 11, 10)])
    assert data.shots == [] and data.problems == ["撮像の範囲（requestStores[].shots）がないため、年間撮像上限は当初の消費枚数だけで数える"]
    # A table without its sensor attribute cannot be used.
    with pytest.raises(EsriError, match="属性 NO_SUCH なし"):
        read_quota_data(client, replace(store, quota_fields={**store.quota_fields, "sensor": "NO_SUCH"}), None, [date(2026, 11, 10)])
