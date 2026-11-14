"""
Imaging requests (撮像依頼): what is to be taken, where, when and how, kept
in an Esri feature layer (`requestStores`) and handed every day to the
satellite systems as files (docs/imaging-requests.md).

A request is 定点 (taken again and again, on the days its repeat names) or
臨時 (once or every day of its period). Its 達成条件 says whether every day
counts (毎回) or one image in the period is enough (期間内1回).

A request may be taken several times a day (1 日の回数, tsudo 2026-11-11:
「n回以上、m回以下」): at least `times_min` (n) shots on different passes,
`times_gap` hours apart, up to `times_max` (m; n when not given) where
there is room; a day counts when n of them are taken.

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
#: Hours between two shots of a request taken several times a day, when it does not say (so that they are on
#: different passes: a satellite passes again after about 1.5 hours).
DEFAULT_TIMES_GAP = 1.0

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
    #: 管理番号 and 識別番号: the requester's own numbers (text), handed to the systems as written.
    management_no: str = ""
    identification_no: str = ""
    #: 依頼グループ: whose request it is (a name of the groups table, core/request/groups.py); "" for none.
    group: str = ""
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
    #: 1 日の回数: at least `times_min` shots a day (the day counts with them), up to `times_max` (None: times_min)
    #: where there is room, `times_gap` hours apart at least (None: DEFAULT_TIMES_GAP).
    times_min: int = 1
    times_max: Optional[int] = None
    times_gap: Optional[float] = None
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
    #: The 更新日時 of a save whose answer was lost (a timeout): the server may hold it (#73, #74).
    pending_update: Optional[float] = field(default=None, compare=False, repr=False)

    @property
    def effective(self) -> int:
        """The priority after raises (繰り上げ)."""
        return clamp_priority(self.priority - self.boost)

    @property
    def times(self) -> tuple[int, int]:
        """(n, m): the shots a day that make the day count, and the most taken where there is room."""
        n = max(1, int(self.times_min or 1))
        return n, max(n, int(self.times_max)) if self.times_max else n

    @property
    def gap_ms(self) -> float:
        """The least time between two of its shots on one day (ms)."""
        hours = self.times_gap if self.times_gap is not None and self.times_gap >= 0 else DEFAULT_TIMES_GAP
        return hours * 3600000


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


def ring_area(ring: Sequence) -> float:
    """A ring's signed area (degrees², anticlockwise positive)."""
    return sum(ring[i - 1][0] * ring[i][1] - ring[i][0] * ring[i - 1][1] for i in range(len(ring))) / 2


def polygons_of(rings: Sequence) -> list[list]:
    """
    Rings grouped into polygons, each its outer ring first and then its
    holes, by how they lie in one another (whichever way they turn): a ring
    inside an even number of others is an outer ring, one inside an odd
    number a hole of the smallest outer ring holding it. Rings of fewer than
    three points are left out; the order of the rings is kept otherwise.
    """
    rs = [r for r in rings if len(r) >= 3]
    areas = [abs(ring_area(r)) for r in rs]
    holders = []
    for i, r in enumerate(rs):
        # A point of the ring not on another ring's edge (the first vertex mostly is not).
        x, y = r[0][0], r[0][1]
        holders.append([j for j, other in enumerate(rs) if j != i and in_rings([other], x, y)])
    depth = [len(h) for h in holders]
    out: dict = {}
    order = []
    for i, r in enumerate(rs):
        if depth[i] % 2 == 0:
            out[i] = [r]
            order.append(i)
    for i, r in enumerate(rs):
        if depth[i] % 2 == 1:
            outer = [j for j in holders[i] if depth[j] == depth[i] - 1]
            if outer:
                out[min(outer, key=lambda j: areas[j])].append(r)
            else:
                out[i] = [r]
                order.append(i)
    return [out[i] for i in sorted(order)]


def _hull(points: Sequence[tuple]) -> list:
    """The convex hull of points (x, y), anticlockwise (monotone chain)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def rotated_box(rings: Sequence) -> list:
    """
    The smallest rectangle on the ground, turned any way, that holds rings
    (requestExports[].areaShape `rotatedBox`, tsudo 2026-11-14: 凹みの多い
    ポリゴンも「矩形回転させれば収まる」): a closed ring of its four corners
    [longitude, latitude], anticlockwise; [] without points. Worked out in a
    plane true to scale around the area (longitudes by the cosine of its
    middle latitude), so that the box is the smallest in metres, not degrees.
    One side of the rectangle always lies along an edge of the area's hull.
    """
    pts = [(float(p[0]), float(p[1])) for r in rings for p in r]
    if not pts:
        return []
    lon0 = pts[0][0]
    # Longitudes made continuous across 180° around the first point.
    pts = [((x - lon0 + 180) % 360 - 180 + lon0, y) for x, y in pts]
    ys = [y for _, y in pts]
    lat0 = (min(ys) + max(ys)) / 2
    k = max(0.01, math.cos(math.radians(lat0)))
    hull = _hull([((x - lon0) * k, y - lat0) for x, y in pts])
    best = None
    if len(hull) >= 3:
        for i in range(len(hull)):
            (x1, y1), (x2, y2) = hull[i], hull[(i + 1) % len(hull)]
            length = math.hypot(x2 - x1, y2 - y1)
            if length == 0:
                continue
            ux, uy = (x2 - x1) / length, (y2 - y1) / length
            us = [x * ux + y * uy for x, y in hull]
            vs = [y * ux - x * uy for x, y in hull]
            area = (max(us) - min(us)) * (max(vs) - min(vs))
            if best is None or area < best[0] * (1 - 1e-12):
                best = (area, ux, uy, min(us), max(us), min(vs), max(vs))
    if best is None:
        # A line or a point: its box along the meridians.
        box = bbox_of([pts])
        w, s, e, n = box
        return [[w, s], [e, s], [e, n], [w, n], [w, s]]
    _, ux, uy, u0, u1, v0, v1 = best
    corners = []
    for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1)):
        x, y = u * ux - v * uy, u * uy + v * ux
        corners.append([x / k + lon0, y + lat0])
    return corners + [list(corners[0])]


def shape_text(rings: Sequence, kind: str = "範囲") -> str:
    """
    What an area is, in words: 「点」, 「矩形」 (a box along the meridians),
    「多角形（頂点 12）」, 「多角形（2 部分・頂点 30・穴 1）」; "" without rings.
    """
    polygons = polygons_of(rings)
    if not polygons:
        return ""
    if kind == "点":
        return "点"
    corners = {(p[0], p[1]) for p in polygons[0][0]}
    box = bbox_of(polygons[0][:1])
    if len(polygons) == 1 and len(polygons[0]) == 1 and box is not None and len(corners) == 4 and corners == {
            (box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])}:
        return "矩形"
    vertices = sum(len(r) - (1 if r[0] == r[-1] else 0) for p in polygons for r in p)
    holes = sum(len(p) - 1 for p in polygons)
    parts = ([f"{len(polygons)} 部分"] if len(polygons) > 1 else []) + [f"頂点 {vertices}"] + ([f"穴 {holes}"] if holes else [])
    return f"多角形（{'・'.join(parts)}）"


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
