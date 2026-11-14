"""
地点ごとの撮像予定 (tsudo, 2026-11-14: 「撮像計画カレンダーとは別に、この地点
を次いつ取る計画があるかを可視化したい」): for places (a layer's features, or
points clicked on the map), the plans ahead that take each of them, on a
timeline: the places one under another, time running to the right. Read
only: nothing is written anywhere.

What each place's line shows, from the start of today (JST) for `days` days:

- 計画: the rows of the history (TBL_REQUEST_HISTORY) of those days whose
  area takes the place (the row's own, else its request's in the ledger
  when the table keeps no shapes), or whose shot does (TBL_REQUEST_SHOT:
  what the satellite takes, larger than the request's area, or the box of
  several requests taken together): the requests handed out (採用 / 不明 /
  不採用 as the system answered), held back (見送り), and the systems' own
  plans (要求外). Each at the time planned (the system's answer, else its
  shot's pass), else over its whole day (時刻未定).
- 依頼: the days a request in force (有効) whose area takes the place is
  still to be planned on (model.occurs_on: in its period and, 定点, on its
  repeat), that have no row of it yet: the daily calculation will try it on
  them.

「次」 is the first plan from now on that is in a system's plan (採用・要求外)
or not answered yet (不明); else the first day of a request; else none.
不採用 and 見送り are shown only when asked (they will not be taken).

Pure Python (no Qt): the shares of the places' areas are measured on points
spread over them, the way the day's fitting measures an area; the dock
passes QGIS's geometry instead (faster on large shapes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Sequence

from .model import JST, ImagingRequest, bbox_of, day_bounds, in_rings, jst_day, occurs_on
from .store import AWAITING, DECLINED, HELD_BACK, PLANNED, UNREQUESTED, HistoryRow, ShotRow, state_of

#: The days ahead that may be shown, and the default.
DAY_CHOICES = (3, 7, 14, 30, 60, 90)
DAYS = 14
#: The most places shown (a layer of more: its first ones).
MOST_PLACES = 500
#: The states of a plan that will be taken (or may be: not answered), what 「次」 looks at.
COMING = (PLANNED, UNREQUESTED, AWAITING)
#: Those that will not, shown only when asked.
NOT_TAKEN = (DECLINED, HELD_BACK)
#: The kind of a mark for a request's day still to be planned.
REQUEST = "依頼"
#: About how many points measure the share of a place's area.
SHARE_CELLS = 200
_WEEKDAYS = "月火水木金土日"


@dataclass
class Place:
    """A place asked about: its name and its shape (longitude / latitude)."""
    name: str
    #: A polygon's rings (outer rings and holes in one list); else a line's parts; else its points.
    rings: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    points: list = field(default_factory=list)
    #: Where it is from: "click-3", or the layer's feature id.
    key: str = ""
    #: More about it for its tooltip (a clicked point's coordinates).
    note: str = ""

    @property
    def kind(self) -> str:
        return "範囲" if self.rings else "線" if self.lines else "点"

    def coords(self) -> list:
        return [p for r in self.rings for p in r] or [p for line in self.lines for p in line] or list(self.points)

    @property
    def box(self) -> Optional[tuple]:
        return bbox_of([self.coords()])


@dataclass
class Mark:
    """One thing on a place's line: a plan (a history row) or a day a request will be planned on."""
    #: The plan's state (store.STATES), or REQUEST.
    state: str
    #: The JST day, `2026-11-15`.
    day: str
    #: When (ms): a plan's time; for one without a time (and a request's day), the day's start and end.
    start: float
    end: float
    timed: bool = False
    row: Optional[HistoryRow] = None
    shot: Optional[ShotRow] = None
    #: A request's day: the requests to be planned on it.
    requests: list = field(default_factory=list)
    #: The share of the place's area it takes (1 for a point inside; None when not measured, as for a line).
    share: Optional[float] = None
    #: Taken by its shot only (the place is outside the request's area, inside what the satellite takes).
    by_shot: bool = False

    @property
    def is_plan(self) -> bool:
        return self.state != REQUEST


@dataclass
class Lane:
    """A place's line: its marks in time order, and what 「次」 says."""
    place: Place
    marks: list = field(default_factory=list)
    #: The first coming plan (or request day) from now on; None when there is none.
    next: Optional[Mark] = None


def row_state(row: HistoryRow) -> str:
    return state_of(row.result, row.adoption, row.captured)


def days_of(first: date, days: int) -> list[date]:
    return [first + timedelta(days=i) for i in range(max(1, days))]


def span_of(first: date, days: int) -> tuple[float, float]:
    """ms of the timeline's start (the first day's JST midnight) and end (after its last day)."""
    start = day_bounds(first)[0]
    return start, start + max(1, days) * 86400000


# --- Whether a shape takes a place ------------------------------------------------------------------------------


def _overlap(a: Optional[tuple], b: Optional[tuple]) -> bool:
    return a is not None and b is not None and a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _segments(rings: Sequence) -> list:
    out = []
    for ring in rings:
        for i in range(1, len(ring)):
            out.append((ring[i - 1], ring[i]))
    return out


def _crosses(a, b, c, d) -> bool:
    def side(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2, d3, d4 = side(c, d, a), side(c, d, b), side(a, b, c), side(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _edges_cross(lines: Sequence, rings: Sequence) -> bool:
    theirs = _segments(rings)
    return any(_crosses(a, b, c, d) for a, b in _segments(lines) for c, d in theirs)


def share_of(place: Place, rings: Sequence) -> Optional[float]:
    """
    Whether an area (rings) takes a place, and how much of it: None when it
    does not; for a point 1; for an area the share of it inside (0 when they
    only touch: less than one point measured); for a line the share of its
    vertices inside (0 when it only crosses).
    """
    if not rings or not _overlap(place.box, bbox_of(rings)):
        return None
    if place.rings:
        from .passes import grid_cells

        samples = grid_cells(place.rings, SHARE_CELLS)
        inside = sum(1 for x, y in samples if in_rings(rings, x, y))
        if inside:
            return inside / len(samples)
        if (any(in_rings(place.rings, p[0], p[1]) for r in rings for p in r) or any(in_rings(rings, p[0], p[1]) for p in place.coords())
                or _edges_cross(place.rings, rings)):
            return 0.0
        return None
    if place.lines:
        vertices = place.coords()
        inside = sum(1 for p in vertices if in_rings(rings, p[0], p[1]))
        if inside:
            return inside / len(vertices)
        return 0.0 if _edges_cross(place.lines, rings) else None
    inside = sum(1 for p in place.points if in_rings(rings, p[0], p[1]))
    return inside / len(place.points) if inside else None


# --- The lines ------------------------------------------------------------------------------------------------


def plan_time(row: HistoryRow, shot: Optional[ShotRow]) -> Optional[float]:
    """When a row is to be taken (ms): the system's answer, else its shot's pass; None when not known."""
    for t in (row.planned_at, shot.pass_time if shot is not None else None):
        if t is not None and math.isfinite(t):
            return t
    return None


class _Grid:
    """Boxes (west, south, east, north) by the whole degrees they cover: which of them a place may meet, without trying them all."""

    #: A box over more cells than this is tried with every place.
    MOST_CELLS = 400

    def __init__(self, boxes: Sequence[Optional[tuple]]):
        self.cells: dict[tuple, list] = {}
        self.wide: list = []
        for i, box in enumerate(boxes):
            if box is None:
                continue
            cells = self._cells(box)
            if cells is None:
                self.wide.append(i)
                continue
            for c in cells:
                self.cells.setdefault(c, []).append(i)

    def _cells(self, box: tuple) -> Optional[list]:
        w, s, e, n = (math.floor(v) for v in box)
        if (e - w + 1) * (n - s + 1) > self.MOST_CELLS:
            return None
        return [(x, y) for x in range(w, e + 1) for y in range(s, n + 1)]

    def near(self, box: Optional[tuple], everything: int) -> list[int]:
        """The boxes' indexes (in order) that may meet `box` (all `everything` of them for a place over many cells)."""
        if box is None:
            return []
        cells = self._cells(box)
        if cells is None:
            return list(range(everything))
        found = set(self.wide)
        for c in cells:
            found.update(self.cells.get(c, ()))
        return sorted(found)


def _union(*boxes: Optional[tuple]) -> Optional[tuple]:
    boxes = tuple(b for b in boxes if b is not None)
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def lanes_of(places: Sequence[Place], rows: Sequence[HistoryRow], shots: dict, requests: Sequence[ImagingRequest],
             first: date, days: int, now: float, *, areas: Optional[dict] = None, not_taken: bool = False,
             share: Callable[[Place, Sequence], Optional[float]] = share_of) -> list[Lane]:
    """
    Each place's line from `first` (a JST day) for `days` days: the history
    `rows` of those days (any order; `areas` the ledger's by request id, for
    rows kept without their own), `shots` by SHOT_ID, the ledger's
    `requests`. `not_taken`: 不採用 and 見送り too. `share` says whether
    (and how much of) a place an area takes (share_of; the dock's QGIS one).
    Each place is tried only with the plans and requests near it (a grid of
    whole degrees), each request once (not once a day).
    """
    areas = areas or {}
    the_days = days_of(first, days)
    day_texts = {d.isoformat() for d in the_days}
    rows = [r for r in rows if r.day in day_texts]
    # The days each request already has a row on (handed out, held back, answered; shown or not): not 依頼 any more on them.
    planned_days: dict[str, set] = {}
    for r in rows:
        if r.request_id:
            planned_days.setdefault(r.request_id, set()).add(r.day)
    if not not_taken:
        rows = [r for r in rows if row_state(r) not in NOT_TAKEN]
    # Each request's days still to be planned on (its period, its repeat), worked out once.
    live = []
    for q in requests:
        if q.status != "有効" or not q.rings:
            continue
        open_days = [d for d in the_days if d.isoformat() not in planned_days.get(q.id, ()) and occurs_on(q, d)]
        if open_days:
            live.append((q, {d.isoformat() for d in open_days}))
    # What each row's plan takes: its area (or its request's), and its shot's.
    plans = []
    for r in rows:
        shot = shots.get(r.shot_id) if r.shot_id else None
        rings = r.rings or areas.get(r.request_id) or []
        shot_rings = shot.rings if shot is not None and shot.rings else []
        plans.append((r, shot, rings, shot_rings))
    plan_grid = _Grid([_union(bbox_of(rings) if rings else None, bbox_of(sr) if sr else None) for _r, _s, rings, sr in plans])
    request_grid = _Grid([bbox_of(q.rings) for q, _days in live])
    lanes = []
    for place in places:
        box = place.box
        marks: list[Mark] = []
        by_shot: set = set()
        for i in plan_grid.near(box, len(plans)):
            r, shot, rings, shot_rings = plans[i]
            got = share(place, rings) if rings else None
            outside = False
            if got is None and shot_rings:
                # Outside the request's area, inside what the satellite takes: once a shot (not once each request in it).
                if shot.shot_id in by_shot:
                    continue
                got = share(place, shot_rings)
                outside = got is not None
                if outside:
                    by_shot.add(shot.shot_id)
            if got is None:
                continue
            t = plan_time(r, shot)
            start, end = (t, t) if t is not None else day_bounds(date.fromisoformat(r.day))
            marks.append(Mark(row_state(r), r.day, start, end, t is not None, r, shot, share=None if place.lines else got,
                              by_shot=outside))
        # A shot taken only by its outline whose own row also takes the place (another row of it): not twice.
        inside_shots = {m.row.shot_id for m in marks if m.row is not None and m.row.shot_id and not m.by_shot}
        marks = [m for m in marks if not (m.by_shot and m.row.shot_id in inside_shots)]
        covering = []
        for i in request_grid.near(box, len(live)):
            q, open_days = live[i]
            got = share(place, q.rings)
            if got is not None:
                covering.append((q, open_days, got))
        for d in the_days:
            text = d.isoformat()
            asked = [(q, got) for q, open_days, got in covering if text in open_days]
            if asked:
                start, end = day_bounds(d)
                best = max(g for _q, g in asked)
                marks.append(Mark(REQUEST, text, start, end, requests=[q for q, _g in asked], share=None if place.lines else best))
        marks.sort(key=lambda m: (m.start, m.state == REQUEST, m.end))
        lanes.append(Lane(place, marks, next_of(marks, now)))
    return lanes


def next_of(marks: Sequence[Mark], now: float) -> Optional[Mark]:
    """The first coming plan from now on (one without a time counts all its day); else the first request's day still ahead."""
    plans = [m for m in marks if m.is_plan and m.state in COMING and m.end >= now]
    if plans:
        return min(plans, key=lambda m: (m.start if m.timed else max(m.start, now), not m.timed))
    asked = [m for m in marks if m.state == REQUEST and m.end >= now]
    return min(asked, key=lambda m: m.start) if asked else None


def sort_by_next(lanes: Sequence[Lane]) -> list[Lane]:
    """The lines with the soonest 「次」 first (a plan before a request's day), those with none last; the order kept otherwise."""
    def key(lane: Lane) -> tuple:
        m = lane.next
        if m is None:
            return (2, math.inf)
        return (0 if m.is_plan else 1, m.start)
    return sorted(lanes, key=key)


# --- Words ------------------------------------------------------------------------------------------------------


def day_text(day: str) -> str:
    """「11/15(土)」."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{d.month}/{d.day}({_WEEKDAYS[d.weekday()]})"


def when_text(m: Mark) -> str:
    """「11/15(土) 10:32」, 「11/15(土) 時刻未定」, a request's day 「11/16(日)」."""
    if m.timed:
        t = datetime.fromtimestamp(m.start / 1000, tz=JST)
        return f"{day_text(jst_day(m.start).isoformat())} {t:%H:%M}"
    return day_text(m.day) + ("" if m.state == REQUEST else " 時刻未定")


def mark_name(m: Mark) -> str:
    if m.row is not None:
        return m.row.name or m.row.external_id or m.row.request_id
    return "、".join(q.name or q.id for q in m.requests)


def next_text(lane: Lane) -> str:
    """What the line says next to the place's name: 「次 11/15(土) 10:32 採用・ASNARO-2」, 「依頼あり 11/16(日)〜」, 「予定なし」."""
    m = lane.next
    if m is None:
        return "予定なし"
    if m.state == REQUEST:
        return f"依頼あり {day_text(m.day)}〜（計画前）"
    who = m.row.satellite or m.row.system if m.row is not None else ""
    return f"次 {when_text(m)} {m.state}" + (f"・{who}" if who else "")


def _percent(share: Optional[float]) -> str:
    if share is None or share >= 0.995:
        return ""
    return "1% 未満" if share < 0.01 else f"{share * 100:.0f}%"


def mark_tip(m: Mark, place: Place) -> str:
    """A mark's details (plain text): what, when, by which system and satellite, how much of the place."""
    lines = []
    if m.state == REQUEST:
        lines.append(f"{day_text(m.day)}: 依頼 {len(m.requests)} 件（この日の計算でシステムに出す候補。まだ計画前）")
        for q in m.requests[:8]:
            period = ""
            if math.isfinite(q.start) and math.isfinite(q.end):
                period = f" {day_text(jst_day(q.start).isoformat())}〜{day_text(jst_day(q.end).isoformat())}"
            how = q.category + ("・" + q.repeat if q.category == "定点" and q.repeat else "") + "・" + q.achieve
            lines.append(f"・{q.name or q.id}（{how}{period}・優先度 {q.effective}）")
        if len(m.requests) > 8:
            lines.append(f"ほか {len(m.requests) - 8} 件")
    else:
        r = m.row
        lines.append(f"{mark_name(m)}: {m.state}")
        lines.append(when_text(m) + ("（システムの回答）" if r.planned_at is not None else "（撮像の範囲のパスの時刻）" if m.timed else ""))
        parts = [p for p in (r.system, r.satellite) if p]
        if parts:
            lines.append(" / ".join(parts))
        if r.requester:
            lines.append(f"依頼者 {r.requester}")
        if r.management_no:
            lines.append(f"管理番号 {r.management_no}")
        if m.state == UNREQUESTED:
            lines.append("システムの定常の計画（依頼なし）")
        note = r.adoption_reason or r.reason
        if note:
            lines.append(note)
    pct = _percent(m.share)
    if pct and place.kind == "範囲":
        lines.append(f"{place.name} の {pct} を含む")
    if m.by_shot:
        lines.append("依頼の範囲の外、撮像の範囲（衛星が撮る範囲）の中")
    return "\n".join(lines)


def tally_text(lanes: Sequence[Lane]) -> str:
    """「地点 12・次の計画あり 5・依頼だけ 3・予定なし 4」."""
    plans = sum(1 for lane in lanes if lane.next is not None and lane.next.is_plan)
    asked = sum(1 for lane in lanes if lane.next is not None and not lane.next.is_plan)
    parts = [f"地点 {len(lanes)}", f"計画あり {plans}"]
    if asked:
        parts.append(f"依頼だけ {asked}")
    parts.append(f"予定なし {len(lanes) - plans - asked}")
    return "・".join(parts)


# --- Where it goes on the screen (ui/place_timeline.py paints it) -------------------------------------------------

#: The places' names and 「次」 (pixels wide), each line's height, the heading's.
LABEL = 230
ROW = 36
HEAD = 22
#: The strip at the bottom of a line where the requests' days are.
STRIP = 7
#: A timed plan's width.
TICK = 4


@dataclass
class Layout:
    width: float
    height: float
    start: float
    end: float
    #: (x, label or "", weekend) of each day's start.
    days: list
    #: x of now (None when outside).
    now: Optional[float]
    #: (y, name, next text, tooltip) of each line.
    labels: list
    #: (x0, x1, y0, y1, mark) of every mark, plans over the requests' days.
    marks: list


def layout(lanes: Sequence[Lane], start: float, end: float, width: float, now: Optional[float] = None) -> Layout:
    """Where everything goes in a timeline `width` pixels wide."""
    width = max(LABEL + 240.0, width or 800.0)
    plot = width - LABEL - 8

    def x(t: float) -> float:
        return LABEL + (min(end, max(start, t)) - start) / (end - start) * plot

    days = []
    n_days = max(1, round((end - start) / 86400000))
    # Labels far enough apart: every day when there is room, else every k days.
    every = max(1, math.ceil(n_days * 58 / plot))
    for i in range(n_days):
        t = start + i * 86400000
        d = jst_day(t)
        days.append((x(t), day_text(d.isoformat()) if i % every == 0 else "", d.weekday() >= 5))
    labels, marks = [], []
    for i, lane in enumerate(lanes):
        y = HEAD + i * ROW
        tip = f"{lane.place.name}（{lane.place.kind}）" + (f"\n{lane.place.note}" if lane.place.note else "") + f"\n{next_text(lane)}"
        labels.append((y, lane.place.name, next_text(lane), tip))
        top, bottom = y + 3, y + ROW - STRIP - 3
        for m in (m for m in lane.marks if not m.is_plan):
            marks.append((x(m.start), max(x(m.start) + 1, x(m.end)), y + ROW - STRIP - 1, y + ROW - 2, m))
        # Plans without a time under those with one.
        for m in sorted((m for m in lane.marks if m.is_plan), key=lambda m: m.timed):
            if m.timed:
                cx = x(m.start)
                marks.append((cx - TICK / 2, cx + TICK / 2, top, bottom, m))
            else:
                marks.append((x(m.start) + 1, max(x(m.start) + 2, x(m.end) - 1), top + 4, bottom - 4, m))
    now_x = x(now) if now is not None and start <= now <= end else None
    return Layout(width, HEAD + len(lanes) * ROW + 4, start, end, days, now_x, labels, marks)


def lane_at(lay: Layout, py: float) -> int:
    """The line at a pixel's height (-1 above or below them)."""
    i = math.floor((py - HEAD) / ROW)
    return i if py >= HEAD and 0 <= i < len(lay.labels) else -1


def marks_at(lay: Layout, px: float, py: float, slop: float = 3) -> list[Mark]:
    """
    The marks at a pixel (timed plans within `slop` pixels across, every
    mark within 2 pixels up and down): those drawn on top first (timed
    plans, the others, requests' days).
    """
    i = lane_at(lay, py)
    if i < 0 or px < LABEL:
        return []
    y = HEAD + i * ROW
    out = []
    for x0, x1, y0, y1, m in lay.marks:
        if not y <= y0 < y + ROW or not y0 - 2 <= py <= y1 + 2:
            continue
        pad = slop if m.timed else 0
        if x0 - pad <= px <= x1 + pad:
            out.append(m)
    return sorted(out, key=lambda m: (not m.is_plan, not m.timed))
