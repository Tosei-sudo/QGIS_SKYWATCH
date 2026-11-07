"""
Imaging requests (撮像依頼): what is to be taken, where, when and how, kept
in an Esri feature layer (`requestStores`) and handed every day to the
satellite systems as files (docs/imaging-requests.md).

A request is 定点 (taken again and again, on the days its repeat names) or
臨時 (once or every day of its period). Its 達成条件 says whether every day
counts (毎回) or one image in the period is enough (期間内1回).

Priorities run 1 (highest) … 1000; five named levels are bands of 200.
Days are JST days.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

#: Days are cut in Japan Standard Time.
JST = timezone(timedelta(hours=9))

CATEGORIES = ("定点", "臨時")
ACHIEVES = ("毎回", "期間内1回")
STATUSES = ("有効", "停止", "完了")
KINDS = ("任意", "光学", "SAR")
LOOK_SIDES = ("任意", "右", "左")
ORBIT_DIRS = ("任意", "昇交", "降交")
TIMES_OF_DAY = ("任意", "昼", "夜")
SHAPE_KINDS = ("範囲", "点")

#: (name, lowest, highest, the value chosen by name).
PRIORITY_LEVELS = (
    ("最優先", 1, 200, 100),
    ("高", 201, 400, 300),
    ("中", 401, 600, 500),
    ("低", 601, 800, 700),
    ("最低", 801, 1000, 900),
)
PRIORITY_MIN, PRIORITY_MAX = 1, 1000
#: How much one raise (繰り上げ) lifts a request, and the band it stops in.
BOOST_STEP = 100
BOOST_STOP = 200


def clamp_priority(p: float) -> int:
    """A priority held to 1 … 1000 (whole)."""
    if not math.isfinite(p):
        return 500
    return int(min(PRIORITY_MAX, max(PRIORITY_MIN, round(p))))


def level_of(p: float) -> str:
    """The name of the band a priority falls in (最優先 … 最低)."""
    p = clamp_priority(p)
    return next(name for name, low, high, _ in PRIORITY_LEVELS if low <= p <= high)


def level_value(name: str) -> int:
    """The value a level stands for (中 → 500)."""
    return next((v for n, _, _, v in PRIORITY_LEVELS if n == name), 500)


def priority_text(p: float) -> str:
    """「高（342）」."""
    return f"{level_of(p)}（{clamp_priority(p)}）"


@dataclass
class Repeat:
    """When a 定点 request is taken: every day, on weekdays (0 = Monday), or every `every` days from its start."""
    kind: str = "毎日"
    weekdays: frozenset = frozenset()
    every: int = 1


_WEEKDAYS = "月火水木金土日"


def repeat_of(text: Optional[str]) -> Repeat:
    """`毎日` / `曜日:月水金` / `間隔:3`; anything else is every day."""
    t = (text or "").strip()
    m = re.fullmatch(r"曜日\s*[:：]\s*([月火水木金土日、, ]+)", t)
    if m:
        days = frozenset(_WEEKDAYS.index(c) for c in m.group(1) if c in _WEEKDAYS)
        if days:
            return Repeat("曜日", days)
    m = re.fullmatch(r"間隔\s*[:：]\s*(\d+)", t)
    if m and int(m.group(1)) >= 1:
        return Repeat("間隔", every=int(m.group(1)))
    return Repeat()


def repeat_text(r: Repeat) -> str:
    """The text a repeat is kept as."""
    if r.kind == "曜日":
        return "曜日:" + "".join(_WEEKDAYS[d] for d in sorted(r.weekdays))
    if r.kind == "間隔" and r.every > 1:
        return f"間隔:{r.every}"
    return "毎日"


@dataclass
class ImagingRequest:
    """One request. Times are ms since 1970; angles degrees; resolution metres; cloud percent."""
    id: str
    name: str = ""
    category: str = "臨時"
    achieve: str = "期間内1回"
    start: float = math.nan
    end: float = math.nan
    repeat: str = "毎日"
    status: str = "有効"
    priority: int = 500
    boost: int = 0
    kind: str = "任意"
    series: list = field(default_factory=list)
    satellites: list = field(default_factory=list)
    modes: list = field(default_factory=list)
    off_nadir_min: Optional[float] = None
    off_nadir_max: Optional[float] = None
    incidence_min: Optional[float] = None
    incidence_max: Optional[float] = None
    look_side: str = "任意"
    orbit_dir: str = "任意"
    resolution: Optional[float] = None
    time_of_day: str = "任意"
    #: 「この雲量以上なら実施しない」 (optical); None for no limit.
    cloud_no_go: Optional[float] = None
    #: Systems to try first, in order.
    systems: list = field(default_factory=list)
    shape_kind: str = "範囲"
    requester: str = ""
    note: str = ""
    created: Optional[float] = None
    updated: Optional[float] = None
    #: Longitude / latitude rings (WGS 84).
    rings: list = field(default_factory=list)
    #: The feature's object id (None until saved).
    object_id: Optional[int] = None

    @property
    def effective(self) -> int:
        """The priority after raises (繰り上げ)."""
        return clamp_priority(self.priority - self.boost)


def day_bounds(day: date) -> tuple[float, float]:
    """ms of a JST day's first and last moment."""
    start = datetime(day.year, day.month, day.day, tzinfo=JST)
    return start.timestamp() * 1000, (start + timedelta(days=1)).timestamp() * 1000 - 1


def jst_day(ms: float) -> date:
    """The JST day of a moment."""
    return datetime.fromtimestamp(ms / 1000, tz=JST).date()


def occurs_on(req: ImagingRequest, day: date) -> bool:
    """Whether a request is to be handed out on a day: 有効, the day in its period, and (定点) its repeat."""
    if req.status != "有効":
        return False
    first, last = day_bounds(day)
    if not math.isfinite(req.start) or not math.isfinite(req.end) or req.start > last or req.end < first:
        return False
    if req.category != "定点":
        return True
    r = repeat_of(req.repeat)
    if r.kind == "曜日":
        return day.weekday() in r.weekdays
    if r.kind == "間隔":
        return (day - jst_day(req.start)).days % r.every == 0
    return True


def days_left(req: ImagingRequest, day: date) -> int:
    """Whole days from `day` to the request's last day (0 on the last day)."""
    if not math.isfinite(req.end):
        return 10 ** 6
    return (jst_day(req.end) - day).days


def next_boost(req: ImagingRequest) -> Optional[int]:
    """The raise one step more would make; None once the request is in the top band."""
    if req.priority - req.boost <= BOOST_STOP:
        return None
    return req.boost + BOOST_STEP


def bbox_of(rings: Sequence) -> Optional[tuple[float, float, float, float]]:
    """West, south, east, north of rings."""
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def center_of(rings: Sequence) -> Optional[tuple[float, float]]:
    """The middle (longitude, latitude) of rings' box."""
    box = bbox_of(rings)
    if box is None:
        return None
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def square_rings(lon: float, lat: float, size_km: float) -> list:
    """A square of `size_km` around a point (for requests given as a point)."""
    half_lat = size_km / 2 / 110.574
    half_lon = size_km / 2 / (111.320 * max(0.01, math.cos(math.radians(lat))))
    w, e, s, n = lon - half_lon, lon + half_lon, lat - half_lat, lat + half_lat
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


def in_rings(rings: Sequence, x: float, y: float) -> bool:
    """Whether a point is inside rings (even-odd)."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
                inside = not inside
            j = i
    return inside
