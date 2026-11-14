"""
地点ごとの撮像予定 (core/request/place_plans.py, 2026-11-14): for each place
the plans ahead of the history (at their time, or over their day), the days
requests in force will be planned on, what is next, and where they go on
the timeline.
"""
import math
from datetime import date, datetime, timedelta

from mikage.core.request.model import JST, ImagingRequest, day_bounds
from mikage.core.request.place_plans import (
    HEAD, LABEL, REQUEST, ROW, Lane, Place, day_text, lanes_of, layout, mark_tip, marks_at, next_of, next_text, share_of,
    sort_by_next, span_of, tally_text,
)
from mikage.core.request.store import HistoryRow, ShotRow

DAY = date(2026, 11, 14)  # a Saturday
TOKYO = [[[139.8, 35.45], [139.9, 35.45], [139.9, 35.55], [139.8, 35.55], [139.8, 35.45]]]
SHOT = [[[139.7, 35.4], [140.0, 35.4], [140.0, 35.6], [139.7, 35.6], [139.7, 35.4]]]
OSAKA = [[[135.4, 34.6], [135.5, 34.6], [135.5, 34.7], [135.4, 34.7], [135.4, 34.6]]]


def at(day: date, hh: int, mm: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=JST).timestamp() * 1000


def row(rid, day, **kw) -> HistoryRow:
    kw.setdefault("result", "採用")
    kw.setdefault("adoption", "不明")
    return HistoryRow("RUN", at(DAY, 8), day.isoformat(), rid, **kw)


def request(rid, rings, start=DAY, end=DAY + timedelta(days=13), **kw) -> ImagingRequest:
    return ImagingRequest(rid, name=kw.pop("name", rid), start=day_bounds(start)[0], end=day_bounds(end)[1], rings=rings, **kw)


def test_whether_an_area_takes_a_point_an_area_or_a_line():
    point = Place("点", points=[[139.85, 35.5]])
    assert share_of(point, TOKYO) == 1 and share_of(point, OSAKA) is None
    # An area: the share inside, 0 when they only touch (less than one point measured).
    half = Place("半分", rings=[[[139.85, 35.45], [139.95, 35.45], [139.95, 35.55], [139.85, 35.55], [139.85, 35.45]]])
    assert 0.4 < share_of(half, TOKYO) < 0.6
    big = Place("大", rings=[[[139.0, 35.0], [141.0, 35.0], [141.0, 36.0], [139.0, 36.0], [139.0, 35.0]]])
    tiny = [[[139.851, 35.501], [139.852, 35.501], [139.852, 35.502], [139.851, 35.502], [139.851, 35.501]]]
    assert share_of(big, tiny) == 0.0
    assert share_of(Place("内", rings=tiny), TOKYO) == 1
    # A line: crossing counts.
    line = Place("線", lines=[[[139.7, 35.5], [140.0, 35.5]]])
    assert share_of(line, TOKYO) == 0.0
    assert share_of(Place("外", lines=[[[139.0, 35.0], [139.1, 35.1]]]), TOKYO) is None


def test_the_plans_ahead_of_each_place():
    now = at(DAY, 9)
    tomorrow = DAY + timedelta(days=1)
    rows = [
        # Answered with a time.
        row("R-1", tomorrow, name="東京湾", system="SYS-A", satellite="ASNARO-2", adoption="採用", planned_at=at(tomorrow, 10, 32)),
        # Not answered: its shot's pass tells the time.
        row("R-2", DAY + timedelta(days=2), name="東京 2", system="SYS-B", shot_id="RUN-1"),
        # Without a time: over its day.
        row("R-3", DAY + timedelta(days=3), name="東京 3", system="SYS-A"),
        # Declined and held back: only when asked.
        row("R-4", DAY + timedelta(days=4), name="東京 4", adoption="不採用"),
        row("R-5", DAY + timedelta(days=4), name="東京 5", result="見送り", adoption=""),
        # The system's own plan with its area.
        HistoryRow("RUN", at(DAY, 8), DAY.isoformat(), "", name="定常", result="要求外", adoption="採用", system="SYS-A",
                   planned_at=at(DAY, 7), rings=TOKYO),
        # Elsewhere.
        row("R-9", tomorrow, name="大阪湾", rings=OSAKA),
        # Outside the days.
        row("R-1", DAY + timedelta(days=20), name="東京湾"),
    ]
    shots = {"RUN-1": ShotRow("RUN-1", "RUN", 0, rows[1].day, pass_time=at(DAY + timedelta(days=2), 22, 5), rings=SHOT)}
    requests = [request(f"R-{i}", TOKYO, name=f"依頼 {i}") for i in (1, 2, 3, 4, 5)] + [request("R-9", OSAKA)]
    # The history table keeps no shapes: the ledger's areas.
    areas = {q.id: q.rings for q in requests}
    place = Place("東京の点", points=[[139.85, 35.5]])
    osaka = Place("大阪の点", points=[[135.45, 34.65]])
    nowhere = Place("どこでもない", points=[[150.0, 40.0]])
    lanes = lanes_of([place, osaka, nowhere], rows, shots, requests, DAY, 7, now, areas=areas)
    tokyo = lanes[0]
    plans = [m for m in tokyo.marks if m.is_plan]
    assert [(m.state, m.row.name, m.timed) for m in plans] == [
        ("要求外", "定常", True), ("採用", "東京湾", True), ("不明", "東京 2", True), ("不明", "東京 3", False)]
    assert plans[2].start == at(DAY + timedelta(days=2), 22, 5) and plans[2].shot is shots["RUN-1"]
    assert (plans[3].start, plans[3].end) == day_bounds(DAY + timedelta(days=3))
    # Requests' days: each day of the 7 a request still has no row on (R-1 has one tomorrow, …); one mark a day.
    days = [m for m in tokyo.marks if m.state == REQUEST]
    assert len(days) == 7
    assert [q.id for q in days[0].requests] == ["R-1", "R-2", "R-3", "R-4", "R-5"]
    assert [q.id for q in days[1].requests] == ["R-2", "R-3", "R-4", "R-5"]
    # 不採用 and 見送り have their rows that day: not 依頼 then, and not shown unless asked.
    assert [q.id for q in days[4].requests] == ["R-1", "R-2", "R-3"]
    # Next: 要求外 at 07:00 is past; 東京湾 tomorrow 10:32.
    assert tokyo.next.row.name == "東京湾"
    assert next_text(tokyo) == "次 11/15(日) 10:32 採用・ASNARO-2"
    assert next_text(lanes[1]) == "次 11/15(日) 時刻未定 不明"
    assert lanes[2].marks == [] and next_text(lanes[2]) == "予定なし"
    assert tally_text(lanes) == "地点 3・計画あり 2・予定なし 1"
    # Asked: 不採用 and 見送り too (not next).
    shown = lanes_of([place], rows, shots, requests, DAY, 7, now, areas=areas, not_taken=True)[0]
    assert {m.row.name for m in shown.marks if m.is_plan} >= {"東京 4", "東京 5"} and shown.next.row.name == "東京湾"
    # The tooltips say what, when and who.
    tip = mark_tip(plans[1], place)
    assert tip.splitlines()[:3] == ["東京湾: 採用", "11/15(日) 10:32（システムの回答）", "SYS-A / ASNARO-2"]
    assert "撮像の範囲のパスの時刻" in mark_tip(plans[2], place)
    assert mark_tip(days[0], place).startswith("11/14(土): 依頼 5 件")


def test_a_place_only_in_the_shot_and_a_request_s_repeat():
    now = at(DAY, 9)
    two = DAY + timedelta(days=2)
    # Two requests taken in one shot (まとめ); the place is in the shot, outside both areas: one mark, not two.
    a = [[[139.72, 35.42], [139.75, 35.42], [139.75, 35.45], [139.72, 35.45], [139.72, 35.42]]]
    b = [[[139.95, 35.55], [139.98, 35.55], [139.98, 35.58], [139.95, 35.58], [139.95, 35.55]]]
    rows = [row("A", two, name="A", shot_id="S-1", rings=a), row("B", two, name="B", shot_id="S-1", merged_into="A", rings=b)]
    shots = {"S-1": ShotRow("S-1", "RUN", 0, two.isoformat(), pass_time=at(two, 10), rings=SHOT)}
    place = Place("間", points=[[139.85, 35.5]])
    lane = lanes_of([place], rows, shots, [], DAY, 7, now)[0]
    assert [(m.row.name, m.by_shot) for m in lane.marks] == [("A", True)]
    assert "撮像の範囲（衛星が撮る範囲）の中" in mark_tip(lane.marks[0], place)
    # Inside B's area: B's own mark, not one by the shot.
    lane = lanes_of([Place("B の中", points=[[139.96, 35.56]])], rows, shots, [], DAY, 7, now)[0]
    assert [(m.row.name, m.by_shot) for m in lane.marks] == [("B", False)]
    # 定点 on Mondays and Thursdays only; 停止 not at all; its last day past the days shown.
    fixed = request("F", TOKYO, category="定点", achieve="毎回", repeat="曜日:月,木")
    stopped = request("S", TOKYO, status="停止")
    lane = lanes_of([place], [], {}, [fixed, stopped], DAY, 14, now)[0]
    assert [day_text(m.day) for m in lane.marks] == ["11/16(月)", "11/19(木)", "11/23(月)", "11/26(木)"]
    assert next_text(lane) == "依頼あり 11/16(月)〜（計画前）"
    # A polygon place: how much of it.
    half = Place("半分", rings=[[[139.85, 35.45], [139.95, 35.45], [139.95, 35.55], [139.85, 35.55], [139.85, 35.45]]])
    mark = lanes_of([half], [], {}, [fixed], DAY, 7, now)[0].marks[0]
    assert "半分 の 5" in mark_tip(mark, half) or "半分 の 4" in mark_tip(mark, half)


def test_next_and_the_order_by_it():
    now = at(DAY, 12)
    rows = [row("R-1", DAY, name="今日の朝", planned_at=at(DAY, 9)), row("R-2", DAY, name="今日（時刻未定）")]
    lanes = lanes_of([Place("x", points=[[139.85, 35.5]])], rows, {}, [], DAY, 3, now, areas={"R-1": TOKYO, "R-2": TOKYO})
    # The morning's is past; one of today without a time still counts.
    assert lanes[0].next.row.name == "今日（時刻未定）" and next_text(lanes[0]) == "次 11/14(土) 時刻未定 不明"
    assert next_of(lanes[0].marks, at(DAY + timedelta(days=1), 0, 1)) is None
    a = Lane(Place("a"))
    later = lanes_of([Place("b", points=[[139.85, 35.5]])], [row("R-3", DAY + timedelta(days=2), planned_at=at(DAY + timedelta(days=2), 1))],
                     {}, [request("Q", TOKYO)], DAY, 3, now, areas={"R-3": TOKYO})[0]
    asked = lanes_of([Place("c", points=[[139.85, 35.5]])], [], {}, [request("Q", TOKYO)], DAY, 3, now)[0]
    assert later.next.is_plan and not asked.next.is_plan
    assert [lane.place.name for lane in sort_by_next([a, asked, later, lanes[0]])] == ["x", "b", "c", "a"]


def test_where_the_marks_go_and_what_is_at_a_pixel():
    now = at(DAY, 12)
    tomorrow = DAY + timedelta(days=1)
    rows = [row("R-1", tomorrow, name="計画", planned_at=at(tomorrow, 12)), row("R-2", tomorrow, name="終日")]
    place = Place("x", points=[[139.85, 35.5]])
    lanes = lanes_of([place, Place("y")], rows, {}, [request("Q", TOKYO)], DAY, 4, now, areas={"R-1": TOKYO, "R-2": TOKYO})
    start, end = span_of(DAY, 4)
    assert end - start == 4 * 86400000
    lay = layout(lanes, start, end, LABEL + 8 + 400, now)
    # 100 pixels a day: now (noon of the first) at 50, the plan at noon tomorrow at 150.
    assert lay.height == HEAD + 2 * ROW + 4 and math.isclose(lay.now, LABEL + 50)
    assert [(round(x), text, weekend) for x, text, weekend in lay.days] == [
        (LABEL, "11/14(土)", True), (LABEL + 100, "11/15(日)", True), (LABEL + 200, "11/16(月)", False), (LABEL + 300, "11/17(火)", False)]
    timed = next(m for *_box, m in lay.marks if m.timed)
    y = HEAD + 10
    assert marks_at(lay, LABEL + 151, y) == [timed, next(m for *_b, m in lay.marks if m.is_plan and not m.timed)]
    # The day without a time anywhere on its day; a request's day (Q has no row) on the 14th.
    assert [m.row.name for m in marks_at(lay, LABEL + 120, y)] == ["終日"]
    assert [m.state for m in marks_at(lay, LABEL + 20, HEAD + ROW - 3)] == [REQUEST]
    assert marks_at(lay, LABEL + 20, HEAD + ROW + 10) == [] and marks_at(lay, 10, y) == []
