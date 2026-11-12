"""
The day's work (日次出力): the requests of a day gathered, weighed by the
weather, raised when they were missed or their deadline is near, sent to
the systems that can take them and fitted into each system's daily limits
in order of priority.

1. Candidates: requests 有効 on the day (period, repeat); a 期間内1回 request
   already taken is 完了 and so left out.
2. Weather (optical): the cover over the area, at the moment of each of
   the day's passes with the orbits (weather.cloud_when; the least cloudy
   one counts), otherwise the daylight hours' mean. At or above the
   request's 「実施しない雲量」 it is held back (雲量), unless it is
   期間内1回 with 2 days or less left: then it goes, with a warning.
   The cover's tenth becomes the priority's ones digit.
3. Raises (繰り上げ): requests missed (未撮像) or 期間内1回 near the deadline
   are offered for a raise of 100; those ticked count now. A 定点 request
   missed on a day of its repeat (weekday, interval) is carried over to the
   next day planned, even when that is not one of its days (持ち越し).
4. Order: output priority, then the nearer deadline, then the older request.
   Within the same hundred of the output priority (101–200, 201–300 …; a
   raise moves a request up one) those near their deadline or with few
   chances left (chances.py: 「今日を逃すと残り 1 回」) come first, and an
   optical request whose day is cloudy and that has a much clearer pass in
   the next days comes last (改修案 4, tsudo 2026-11-12).
5. Fitting: in that order, each request goes to the first system (its own
   order, then `requestExports`') with a satellite that can take it and room
   left: the system's `limit` (count, scenes), its `quota` by category, and
   the satellite's `maxPerDay`. A request naming satellites tries them in
   that order; otherwise the finest resolution with room comes first (for
   an optical one, after the less cloudy: by the tenth of the cover of
   each satellite's least cloudy pass).
   Requests that fit nowhere are held back (枠超過, or 該当システムなし).

   With the satellites' orbits (passes.Passes, tsudo 2026-11-11) a request
   goes to a pass of the satellite over it that day (撮像機会なし when there
   is none): into a shot already planned on that pass when it fits in it
   (まとめ, shots.py; it then adds no row to the system's file and uses
   neither the satellite's interval nor its day), or into a new shot where
   the satellite has time (`minInterval` between shots, `maxPerDay` shots
   a day; 衛星の空きなし when it has none). An optical request leaves out
   the passes whose cover at their moment is at or above its 「実施しない
   雲量」 (unless near its deadline or taken by hand) and tries the less
   cloudy first, by tenths (a few percent between two passes tell nothing
   of forecasts three hours apart); its cover is then that of the pass it
   is taken on (`shot_cloud`). A satellite without a readable
   TLE, or a system with `orbit: false`, is fitted by counting as before:
   `maxPerDay` requests a day, scenes from a grid over the area.

A request taken several times a day (1 日の回数: n 回以上・m 回以下, tsudo
2026-11-11) is one candidate per time (`occurrence` 1 … m, each a row of
the file and of the history). The first n go at the request's priority;
the times past n only after every request's own (the extra rounds in
order: every request's n+1-th before any n+2-th), in the room left. Each
time goes on its own pass, `times_gap` hours (1) or more from the others
of the same request (「撮像機会なし（ほかの回との間隔）」), never into a
shot that already takes the request. When fewer than n are taken, those
taken still go, with 「回数が足りない（2/3 回）」 (tsudo: 「取れる分だけ出
す」); the day then does not count. Fitted by counting (no orbits), the
times are simply more requests on the satellites.

Hand changes (taking in, leaving out, moving up or down) are kept on the
candidates and the fitting is done again.

A request of either kind (任意) that both optical and SAR satellites can
take (`Candidate.mixed`, 改修案 1, tsudo 2026-11-12) is weighed by the
weather on its optical passes only: those at or above its 「実施しない雲量」
(ANY_KIND_NO_GO when it names none) are not used, so that a cloudy day
goes to a SAR satellite; a SAR pass is never too cloudy. Its priority and
its holding back are not touched by the weather (a SAR pass may take it).

A sensor or mode with a yearly quota (年間撮像上限, quota.py; tsudo
2026-11-11) takes no more scenes than its year has left, what the shots
handed out and not settled yet will use taken off: a request then tries the
next satellite or mode, and is held back (年間上限) when none has room. One
taken by hand (or pinned) goes past it, with a warning.

A request already in the file another system's person wrote today is
pinned there (`Candidate.pinned`): it is fitted first, to that system and
satellite, whatever the limits or the hand say, since that file is not
written again by whoever writes another system (tsudo, 2026-11-09).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from ..plan.satellites import SatelliteSpec, platform_key, satellite_label
from .chances import Chances, clearer_text, scarce_text
from .covered import Covered, Shares, percent, shares_of, short_day, subtracts
from .model import ImagingRequest, clamp_priority, day_bounds, days_left, next_boost, occurs_on
from .passes import Passes
from .quota import QUOTA, QUOTA_OVER, QuotaBook
from .route import fitting, scenes_of, system_of_satellite, why_not
from .shots import GOAL, Book, Placement
from .systems import ExportSystem
from .weather import cloud_step, with_cloud

#: Days before the deadline from which a 期間内1回 request goes whatever the weather (and is offered a raise).
DEADLINE_DAYS = 2
#: The cover (percent) from which an optical pass is not used for a request of either kind (任意, both optical and SAR
#: satellites can take it) that names no 「実施しない雲量」: a cloudy day goes to SAR (改修案 1, tsudo 2026-11-12).
ANY_KIND_NO_GO = 50.0

TAKEN, HELD = "採用", "見送り"
#: Why a request is held back in the fitting.
NO_SYSTEM, OVER, NO_PASS, BUSY = "該当システムなし", "枠超過", "撮像機会なし", "衛星の空きなし（撮像間隔・1 日の回数）"
#: Held back as the passes the satellites have room for are all too cloudy at their moment.
CLOUDY = "雲量（空いている撮像機会は撮像時刻の予報で実施しない雲量以上）"
#: The start of the warning of an area the day's passes take only part of.
PART = "この日の撮像機会で範囲の約 "
#: The start of the warning of a pass too cloudy at its moment taken all the same (near the deadline, by hand).
SHOT_CLOUD = "撮像時刻の雲量 "
#: Held back as the passes left are too close to the request's other times that day (1 日の回数).
GAP = "撮像機会なし（ほかの回との間隔）"
#: The start of the warning of a request taken fewer times than its 1 日の回数 asks (the day does not count).
FEW = "回数が足りない（"
#: Held back as its area is taken already (撮像済み), or about to be by what was handed out on earlier days (撮像待ち).
TAKEN_ALREADY = "撮像済み（範囲の約 "
WAITING = "撮像待ち（"


def occurrence_key(request_id: str, occurrence: int = 1, part: int = 0) -> str:
    """
    One time of a request on a day, as the files and history tell them
    apart: its id, 「R1#2」 from the second time; a shot of an area too big
    for one past its first (分担) 「R1/2」, 「R1#2/3」.
    """
    key = request_id if occurrence <= 1 else f"{request_id}#{occurrence}"
    return key if part <= 1 else f"{key}/{part}"


@dataclass
class Candidate:
    """One request on the day, and what became of it."""
    request: ImagingRequest
    #: The day's cover over the area (percent): at its least cloudy pass (`timed`) or the daylight hours' mean;
    #: None when not asked (SAR) or not forecast. It sets the order and the priority's ones digit.
    cloud: Optional[float] = None
    #: Whether the weather was asked for this request (optical).
    weather: bool = False
    #: Either kind (任意) with both optical and SAR satellites able to take it: the weather counts on its optical passes
    #: only (`cloud` is that of its least cloudy optical pass); its priority and holding back do not take it.
    mixed: bool = False
    #: Set by fitting, for one `mixed`: the cover of the optical passes it was kept off (too cloudy); None when none.
    optical_cloud: Optional[float] = None
    #: Whether `cloud` is at the moment of a pass (from the orbits), not the daylight hours' mean.
    timed: bool = False
    #: 期間内1回 with DEADLINE_DAYS or less left (or few chances left, `scarce`): it goes whatever the weather.
    near: bool = False
    #: Its chances on the day and after it (chances.Chances); None when not worked out.
    chances: Optional[Chances] = field(default=None, repr=False, compare=False)
    #: Near as it has SCARCE chances or fewer left after the day (not by days): it comes first in its band.
    scarce: bool = False
    #: An optical request whose day is cloudy, with a much clearer pass in the next days: it comes last in its band.
    later: bool = False
    #: How a 定点 request's last image was taken (geometry.LastLook): the passes like it go first; None when not known.
    last_look: Optional[object] = field(default=None, repr=False, compare=False)
    #: The raise offered (the boost it would have), and why; None when none.
    boost_offer: Optional[int] = None
    boost_reason: str = ""
    #: Whether the offered raise is taken (ticked).
    boost_taken: bool = True
    #: Missed (未撮像) on a day of its repeat and carried over to this day, not one of its repeat.
    carried: bool = False
    #: Held back before fitting (the reason), e.g. 雲量.
    held: str = ""
    #: The person's say: None, TAKEN (keep in, even over the limits) or HELD (leave out).
    manual: Optional[str] = None
    #: A place in the order set by hand (smaller first); None to go by priority.
    rank: Optional[float] = None
    # --- set by fitting ---
    result: str = HELD
    reason: str = ""
    system: Optional[ExportSystem] = None
    satellite: Optional[SatelliteSpec] = None
    scenes: int = 1
    warnings: list = field(default_factory=list)
    #: The satellites (modes) that can take it, by system name: the systems it can go to, in the order tried
    #: (catalog order within a system; set by fitting, whatever became of it).
    able: dict = field(default_factory=dict)
    #: (system name, satellite name) when already written today in the file of a system not written from here.
    pinned: Optional[tuple] = None
    #: The shots it is taken in, from the orbits (shots.Shot; empty when fitted by counting).
    shots: list = field(default_factory=list)
    #: The share of an area the day's shots take; None for all of it (or not worked out).
    coverage: Optional[float] = None
    #: The cover over the area at the moment of the pass it is taken on (percent); None when fitted by counting.
    shot_cloud: Optional[float] = None
    #: Which of the request's times on the day (1 日の回数): 1 …; the first carries the raise for them all.
    occurrence: int = 1
    first: Optional["Candidate"] = field(default=None, repr=False, compare=False)
    #: What of its area is taken already and handed out on earlier days (covered.py; 期間内1回 areas), and their shares.
    covered: Optional[Covered] = field(default=None, repr=False, compare=False)
    shares: Shares = field(default_factory=Shares)
    # --- set by fitting, for an area too big for one shot (分担, tsudo 2026-11-12) ---
    #: Which of the time's shots it hands out (1 …, each a row of the file with that shot's part of the area); 0 for a
    #: candidate handing out the whole area.
    part: int = 0
    #: How many shots the time is handed out in (its rows; 0 when not shared out).
    parts: int = 0
    #: The share of the area its own shot adds (None for one handing out the whole area).
    gain: Optional[float] = None
    #: For the time's shots past the first (part 2 …): the candidate of its first shot, which carries the time.
    main: Optional["Candidate"] = field(default=None, repr=False, compare=False)
    #: For a part: (satellite platform, the pass's moment in seconds), so that a part left out by hand stays out.
    pass_key: Optional[tuple] = None
    #: For the first shot: the passes left out by hand (pass_key of its parts held back).
    skip: set = field(default_factory=set, repr=False, compare=False)
    #: The points of its area taken once its shots are in (Placement.cells), for the satellites sharing it.
    cells: Optional[bytearray] = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        """The request's id, with the time from the second on and the shot from the second on (occurrence_key)."""
        return occurrence_key(self.request.id, self.occurrence, self.part)

    @property
    def extra(self) -> bool:
        """A time past the n the request needs (taken only in the room left)."""
        return self.occurrence > self.request.times[0]

    @property
    def forecast(self) -> Optional[float]:
        """The cover written out (cloudForecast, the history): at its pass when the orbits placed it, else the day's."""
        if self.mixed and self.satellite is not None and self.satellite.sar:
            # Either kind taken by a SAR satellite: the clouds do not matter to it.
            return None
        return self.shot_cloud if self.shot_cloud is not None else self.cloud

    @property
    def no_go(self) -> Optional[float]:
        """The cover from which a pass is too cloudy for it: its 「実施しない雲量」 (optical), or for either kind ANY_KIND_NO_GO."""
        if self.weather:
            return self.request.cloud_no_go
        if self.mixed:
            return optical_no_go(self.request)
        return None

    @property
    def lead(self) -> Optional["Candidate"]:
        """The candidate whose row in the file carries this one when it is put together with others (itself for that row)."""
        if len(self.shots) == 1 and self.shots[0].merged:
            return self.shots[0].members[0]
        return None

    @property
    def merged(self) -> bool:
        """Put together with other requests into one shot (まとめ)."""
        return self.lead is not None

    @property
    def member(self) -> bool:
        """Carried by another request's row (put into its shot): no row of its own in the file."""
        lead = self.lead
        return lead is not None and lead.key != self.key

    @property
    def together(self) -> list:
        """The candidates of its shot when put together (the lead first); [] otherwise."""
        return list(self.shots[0].members) if self.merged else []

    @property
    def planned(self) -> Optional[float]:
        """When the satellite takes it (ms), from the orbits; None when fitted by counting."""
        return self.shots[0].member_time(self) if self.shots else None

    @property
    def boost(self) -> int:
        """The raise that counts now (the first time's, for the request's other times; the first shot's, for a part)."""
        if self.main is not None:
            return self.main.boost
        if self.first is not None:
            return self.first.boost
        if self.boost_offer is not None and self.boost_taken:
            return self.boost_offer
        return self.request.boost

    @property
    def base(self) -> int:
        """The priority after raises, before the weather."""
        return clamp_priority(self.request.priority - self.boost)

    @property
    def priority(self) -> int:
        """The output priority (1 … 1000): raises, then the cloud tenth in the ones digit."""
        return with_cloud(self.base, self.cloud) if self.weather else self.base

    @property
    def weather_flag(self) -> str:
        if not self.weather and not (self.mixed and self.forecast is not None):
            return ""
        cloud = self.forecast
        if cloud is None:
            return "予報なし"
        no_go = self.no_go
        return "雲多" if no_go is not None and cloud >= no_go else "OK"


def carries_over(req: ImagingRequest, day: date) -> bool:
    """
    Whether a request missed (未撮像) goes to a day that is not one of its
    repeat (a 定点 request by weekday or interval): 有効 and the day still
    in its period. Others come every day of their period anyway.
    """
    if req.status != "有効":
        return False
    first, last = day_bounds(day)
    return math.isfinite(req.start) and math.isfinite(req.end) and req.start <= last and req.end >= first


def wants_weather(req: ImagingRequest, sats: Sequence[SatelliteSpec]) -> bool:
    """Whether the weather matters: 光学, or 任意 when every satellite that can take it is optical."""
    if req.kind == "光学":
        return True
    if req.kind == "SAR":
        return False
    able = fitting(req, sats)
    return bool(able) and all(not s.sar for s in able)


def mixed_kinds(req: ImagingRequest, sats: Sequence[SatelliteSpec]) -> bool:
    """
    Either kind (任意) that optical and SAR satellites can both take: the
    weather picks between them, on its optical passes only (改修案 1).
    """
    if req.kind != "任意":
        return False
    able = fitting(req, sats)
    return any(s.sar for s in able) and any(not s.sar for s in able)


def asks_weather(req: ImagingRequest, sats: Sequence[SatelliteSpec]) -> bool:
    """Whether a request's forecast is asked for: optical (wants_weather), or either kind with optical satellites too."""
    return wants_weather(req, sats) or mixed_kinds(req, sats)


def optical_no_go(req: ImagingRequest) -> float:
    """The cover from which an optical pass is not used for a request of either kind: its own, else ANY_KIND_NO_GO."""
    return req.cloud_no_go if req.cloud_no_go is not None else ANY_KIND_NO_GO


def gather(requests: Sequence[ImagingRequest], day: date, sats: Sequence[SatelliteSpec],
           clouds: dict, missed: set, covered: Optional[dict] = None, chances: Optional[dict] = None) -> list[Candidate]:
    """
    The candidates of a day: requests that occur on it, with their cover
    (`clouds`: request id → percent or None, weather.Clouds telling those at
    a pass's moment), holds for the weather, and raises offered (`missed`:
    ids of requests found 未撮像 and not yet raised for it). `covered`
    (request id → covered.Covered): what of a 期間内1回 area is taken
    already or handed out on earlier days; one taken (or about to be) to
    GOAL is held back (撮像済み, 撮像待ち: unless near its deadline).
    `chances` (request id → chances.Chances): one with few chances left is
    near its deadline whatever the days; one with a much clearer pass later
    comes last in its priority band.
    """
    timed = getattr(clouds, "timed", ())
    out = []
    for req in requests:
        carried = not occurs_on(req, day) and req.id in missed and carries_over(req, day)
        if not occurs_on(req, day) and not carried:
            continue
        c = Candidate(req, carried=carried)
        if carried:
            c.warnings.append("未撮像の持ち越し")
        left = days_left(req, day)
        near = req.achieve == "期間内1回" and left <= DEADLINE_DAYS
        ch = (chances or {}).get(req.id)
        c.chances = ch
        if not near and req.achieve == "期間内1回" and ch is not None and ch.scarce:
            # 「今日を逃すと残り 1 回」: the day is its chance, whatever the days left.
            near = c.scarce = True
        c.near = near
        if wants_weather(req, sats):
            c.weather = True
            c.cloud = clouds.get(req.id)
            c.timed = c.cloud is not None and req.id in timed
            # At the passes' moments: the least cloudy is still too cloudy, so every pass is.
            every = "撮像時刻の予報でどのパスも " if c.timed else ""
            if c.cloud is None:
                c.warnings.append("予報なし")
            elif req.cloud_no_go is not None and c.cloud >= req.cloud_no_go:
                if near:
                    c.warnings.append(f"雲量 {round(c.cloud)}%（{every}実施しない雲量 {round(req.cloud_no_go)}% 以上・期限間近のため出力）")
                else:
                    c.held = f"雲量 {round(c.cloud)}%（{every}{round(req.cloud_no_go)}% 以上）"
        elif mixed_kinds(req, sats):
            # Either kind: its optical passes are weighed (in the fitting), never the request itself.
            c.mixed = True
            c.cloud = clouds.get(req.id)
            c.timed = c.cloud is not None and req.id in timed
        if c.weather and not near and ch is not None and ch.yields(c.cloud):
            c.later = True
        got = (covered or {}).get(req.id)
        if got is not None and subtracts(req):
            c.covered = got
            c.shares = shares_of(req, got)
            hold = cover_hold(c)
            if hold:
                c.held = hold
            elif c.shares.both >= GOAL and c.shares.taken < GOAL:
                c.warnings.append(f"{waiting_text(c)}・期限間近のため出力")
        reasons = []
        if req.id in missed:
            reasons.append("未撮像")
        if near:
            reasons.append(scarce_text(ch) if c.scarce else f"期限まで {max(0, left)} 日")
        offer = next_boost(req)
        if reasons and offer is not None:
            c.boost_offer = offer
            c.boost_reason = "・".join(reasons)
        out.append(c)
        for k in range(2, req.times[1] + 1):
            out.append(Candidate(req, cloud=c.cloud, weather=c.weather, mixed=c.mixed, timed=c.timed, near=c.near, carried=carried,
                                 held=c.held, warnings=list(c.warnings), occurrence=k, first=c, chances=ch, scarce=c.scarce,
                                 later=c.later))
    return out


def waiting_text(c: Candidate) -> str:
    """「撮像待ち（11/10・11/11 の出力で範囲の約 98%）」."""
    days = "・".join(short_day(d) for d in sorted(c.covered.waiting_days)) if c.covered else ""
    return f"{WAITING}{days + ' の' if days else ''}出力で範囲の約 {percent(c.shares.both)}%）"


def cover_hold(c: Candidate) -> str:
    """Why a candidate is held back for what of its area is taken or waiting ("" when it goes)."""
    if c.covered is None:
        return ""
    if c.shares.taken >= GOAL:
        return f"{TAKEN_ALREADY}{percent(c.shares.taken)}%）"
    if c.shares.both >= GOAL and not c.near:
        return waiting_text(c)
    return ""


def outlines_for(c: Candidate) -> list:
    """
    What of a candidate's area its shots leave out (covered.Covered): the
    taken and the waiting; only the taken near its deadline or taken by hand
    (and nothing when it is taken by hand although taken whole).
    """
    got = c.covered
    if got is None:
        return []
    taken = list(got.taken) if c.shares.taken < GOAL else []
    if c.manual == TAKEN or c.near:
        return taken
    return taken + list(got.waiting)


def cover_text(c: Candidate) -> str:
    """What of its area a candidate leaves out: 「撮像済み 約 40%・撮像待ち 約 20% を除く」 ("" when none)."""
    if c.covered is None or c.result != TAKEN or c.main is not None:
        return ""
    sh = c.shares
    parts = []
    if 0 < sh.taken < GOAL:
        parts.append(f"撮像済み 約 {percent(sh.taken)}%")
    if sh.waiting > 0 and not (c.manual == TAKEN or c.near):
        parts.append(f"撮像待ち 約 {percent(sh.waiting)}%")
    if not parts:
        return ""
    return "・".join(parts) + (" を除く" if c.gain is not None else "（範囲全体を出力）")


def chance_text(c: Candidate) -> str:
    """
    What the chances did: 「残りの撮像機会 1 回（11/14 まで）」 for one put
    first, 「11/14 のほうが晴れる見込み（雲 10%）」 for one put last and left
    out for want of room ("" otherwise).
    """
    if c.scarce and c.chances is not None and c.main is None:
        return scarce_text(c.chances)
    if c.later and c.result == HELD and c.reason in (OVER, BUSY) and c.chances is not None and c.chances.clearer:
        return clearer_text(c.chances)
    return ""


def kind_text(c: Candidate) -> str:
    """Either kind taken by SAR as its optical passes were too cloudy: 「光学は雲量 80% のため SAR」 ("" otherwise)."""
    if not c.mixed or c.result != TAKEN or c.satellite is None or not c.satellite.sar or c.optical_cloud is None:
        return ""
    return f"光学は雲量 {round(c.optical_cloud)}% のため SAR"


def part_text(c: Candidate) -> str:
    """Which of the shots of an area shared out a candidate is: 「分担 2/3」 ("" for one handing out the whole area)."""
    if c.part <= 0 or c.parts <= 1:
        return ""
    return f"分担 {c.part}/{c.parts}"


#: The width of the steps of the output priority within which the chances weigh the order (改修案 4): the hundreds, as a
#: raise (繰り上げ) moves a request up one of them and leaves its last two digits (its place among the others there).
BAND = 100


def _order_key(c: Candidate):
    end = c.request.end if math.isfinite(c.request.end) else math.inf
    created = c.request.created if c.request.created is not None else math.inf
    # Within its hundred: near its deadline or few chances left first, a clearer day ahead last.
    urgency = 0 if c.near else 2 if c.later else 1
    return ((c.priority - 1) // BAND, urgency, c.priority, end, created, c.request.name)


def ordered(cands: Sequence[Candidate]) -> list[Candidate]:
    """
    Candidates in the order they are fitted: by hand where set, otherwise by
    priority, deadline and age; a request's times past the n it needs after
    every request's own, round by round.
    """
    def key(c: Candidate):
        return (c.occurrence - c.request.times[0], _order_key(c)) if c.extra else (0, _order_key(c))

    by_priority = sorted(cands, key=key)
    ranked = {id(c): (c.rank if c.rank is not None else float(i)) for i, c in enumerate(by_priority)}
    return sorted(by_priority, key=lambda c: ranked[id(c)])


def _systems_for(req: ImagingRequest, systems: Sequence[ExportSystem]) -> list[ExportSystem]:
    """The systems in the order a request tries them: its own first, then the settings' order."""
    named = [s for name in req.systems for s in systems if s.system == name]
    return named + [s for s in systems if s not in named]


@dataclass
class Room:
    """What a system has used of its day."""
    count: int = 0
    scenes: int = 0
    by_category: dict = field(default_factory=dict)


def placement_rows(p: Placement) -> int:
    """The rows of a system's file a placement adds: none to join a shot, one a shot of an area too big for one, else one."""
    if not p.new:
        return 0
    return len(p.shots) if p.coverage is not None else 1


def _manual_warning(w: str) -> bool:
    """A warning the fitting itself adds (made again by each fitting)."""
    return (w.startswith(OVER) or w.endswith("（手動で採用）") or w.startswith(PART) or w.startswith(SHOT_CLOUD)
            or w.startswith(FEW) or w.startswith(QUOTA_OVER))


def times_of(c: Candidate) -> list[float]:
    """The moments a candidate is taken at (ms; each shot of a big area), from the orbits; [] when fitted by counting."""
    return [s.member_time(c) for s in c.shots]


def occurrence_text(c: Candidate) -> str:
    """Which time of a request taken several times a day: 「2/3 回目」, past the n it needs 「4 回目・追加」."""
    n, m = c.request.times
    if m <= 1:
        return ""
    if c.occurrence <= n:
        return f"{c.occurrence}/{n} 回目"
    return f"{c.occurrence} 回目・追加"


def _room_fits(system: ExportSystem, room: Room, category: str, rows: int, scenes: int) -> bool:
    return (("count" not in system.limit or room.count + rows <= system.limit["count"])
            and ("scenes" not in system.limit or room.scenes + scenes <= system.limit["scenes"])
            and (not rows or category not in system.quota or room.by_category.get(category, 0) + rows <= system.quota[category]))


def _cloud_near(ops: Sequence, ms: float) -> Optional[float]:
    """The cover told the pass nearest a moment (its own request's area); None when none was told."""
    known = [o for o in ops if o.cloud is not None]
    return min(known, key=lambda o: abs(o.time - ms)).cloud if known else None


def _placement_cloud(p: Placement, ops: Sequence) -> Optional[float]:
    """The cover over a request's area at the moment of a placement's pass (the mean over its shots for a big area)."""
    times = [p.into.time] if p.into is not None else [s.time for s in p.shots]
    values = [v for v in (_cloud_near(ops, t) for t in times) if v is not None]
    return sum(values) / len(values) if values else None


def _tenth(cloud: Optional[float]) -> int:
    """How passes are told apart by the weather: the cover's tenth (no forecast after every tenth)."""
    return 10 if cloud is None else cloud_step(cloud)


def allocate(cands: Sequence[Candidate], systems: Sequence[ExportSystem], sats: Sequence[SatelliteSpec],
             passes: Optional[Passes] = None, day: Optional[date] = None,
             quotas: Optional[QuotaBook] = None) -> list[Candidate]:
    """
    Fits the candidates into the systems' limits (in place); the candidates in
    their order. With `passes` (the satellites' orbits) and `day`, to the
    passes of the day, put together where they fit in one shot. With
    `quotas`, within what the yearly quotas have left (what each is given is
    kept on it, `QuotaBook.spent`).
    """
    book_quota = quotas if quotas is not None else QuotaBook()
    book_quota.reset()
    rooms = {s.system: Room() for s in systems}
    #: Requests a day of the satellites fitted by counting (platform → n).
    per_sat: dict[str, int] = {}
    members = {s.system: [] for s in systems}
    for sat in sats:
        sys_ = system_of_satellite(sat, systems)
        if sys_ is not None:
            members[sys_.system].append(sat)
    index = {id(s): i for i, s in enumerate(sats)}
    book = Book(sats, passes) if passes is not None and day is not None else None
    # The shots of an area shared out are worked out again; those left out by hand stay out (and are shown).
    held_parts = [c for c in cands if c.main is not None and c.manual == HELD]
    out = ordered([c for c in cands if c.main is None])
    by_name = {s.system: s for s in systems}
    #: Each request's times on the day (1 日の回数), to keep them apart (and the shots of each, once shared out).
    times: dict = {}
    #: The shots past the first of each candidate (分担), by id of that candidate.
    parts_of: dict = {}
    for c in out:
        times.setdefault(c.request.id, []).append(c)
        c.result, c.shots = HELD, []
        c.part, c.parts, c.gain, c.cells = 0, 0, None, None
        c.skip = {p.pass_key for p in held_parts if p.main is c and p.pass_key}

    def orbits(system: ExportSystem, sat: SatelliteSpec) -> bool:
        return book is not None and system.orbit and book.known(index.get(id(sat), -1))

    def left(system: ExportSystem, sat: SatelliteSpec) -> float:
        """How many more the satellite takes today (shots, or requests when counted)."""
        most = sat.maxPerDay or 10 ** 6
        if orbits(system, sat):
            return most - len(book.of_platform(index[id(sat)]))
        return most - per_sat.get(platform_key(sat), 0)

    def counted(c: Candidate, system: ExportSystem, sat: Optional[SatelliteSpec], scenes: int) -> None:
        room = rooms[system.system]
        c.result, c.system, c.satellite, c.scenes = TAKEN, system, sat, scenes
        room.count += 1
        room.scenes += scenes
        room.by_category[c.request.category] = room.by_category.get(c.request.category, 0) + 1
        if sat is not None:
            per_sat[platform_key(sat)] = per_sat.get(platform_key(sat), 0) + 1
        quota_check(c, sat, scenes)
        book_quota.take(sat, scenes)

    def quota_check(c: Candidate, sat: Optional[SatelliteSpec], scenes: int) -> None:
        """Warns of a request taken past a quota's year (by hand, or pinned)."""
        over = book_quota.over(sat, scenes)
        if over is not None:
            c.warnings.append(book_quota.over_text(over))

    def placed(c: Candidate, system: ExportSystem, sat: SatelliteSpec, p: Placement, cloud: Optional[float]) -> None:
        c.result, c.system, c.satellite = TAKEN, system, sat
        c.shots = book.take(c, p)
        c.coverage = p.coverage
        c.shot_cloud = cloud
        no_go = c.no_go
        if cloud is not None and no_go is not None and cloud >= no_go and (c.cloud is None or c.cloud < no_go or c.mixed):
            # Only too cloudy passes had room, and it goes all the same (the day's own cover was said in gather otherwise).
            why = "期限間近" if c.near else "手動で採用" if c.manual == TAKEN else "書き出し済み"
            c.warnings.append(f"{SHOT_CLOUD}{round(cloud)}%（実施しない雲量 {round(no_go)}% 以上・{why}のため出力）")
        c.gain, c.cells = p.gain, p.cells
        spend(c, system, sat, p)

    def spend(c: Candidate, system: ExportSystem, sat: SatelliteSpec, p: Placement) -> None:
        """A placement's rows (one a shot of an area too big for one), scenes and quota off the system's day."""
        room = rooms[system.system]
        rows = placement_rows(p)
        if rows:
            room.count += rows
            room.by_category[c.request.category] = room.by_category.get(c.request.category, 0) + rows
        room.scenes += p.scenes
        quota_check(c, sat, p.scenes)
        book_quota.take(sat, p.scenes)

    def sky(c: Candidate, system: ExportSystem, sat: SatelliteSpec) -> int:
        """The tenth of the cover of a satellite's least cloudy pass over a request (the day's for one fitted by counting)."""
        if not c.weather:
            return 0
        if not orbits(system, sat):
            return _tenth(c.cloud)
        told = [o.cloud for o in passes.of(c.request, day, index[id(sat)]) if o.cloud is not None]
        return _tenth(min(told)) if told else 10

    def weighed(c: Candidate, sat: SatelliteSpec) -> bool:
        """Whether the weather weighs a satellite's passes for a candidate: optical, or either kind's optical satellites."""
        return c.weather or c.mixed and not sat.sar

    def clearest(c: Candidate, system: ExportSystem, sat: SatelliteSpec) -> Optional[float]:
        """The cover of an optical satellite's least cloudy pass over a candidate (the day's when counted); None when not told."""
        if not orbits(system, sat):
            return c.cloud
        told = [o.cloud for o in passes.of(c.request, day, index[id(sat)]) if o.cloud is not None]
        return min(told) if told else None

    def overcast(c: Candidate, system: ExportSystem, sat: SatelliteSpec) -> bool:
        """
        For either kind (mixed): an optical satellite whose passes over it are
        all too cloudy (its least cloudy at or above the 「実施しない雲量」; the
        day's cover when fitted by counting). Never a SAR one, nor without a forecast.
        """
        if not c.mixed or sat.sar:
            return False
        cloud = clearest(c, system, sat)
        return cloud is not None and cloud >= optical_no_go(c.request)

    def options_of(c: Candidate, system: ExportSystem, sat: SatelliteSpec, why: set, forced: bool) -> Optional[list]:
        """The passes of the satellite a candidate may go on (its times apart, the weather, those left out by hand); None: none."""
        i = index[id(sat)]
        ops = passes.of(c.request, day, i)
        if c.skip:
            ops = [o for o in ops if (platform_key(sat), round(o.time / 1000)) not in c.skip]
        if not ops:
            why.add(NO_PASS)
            return None
        # The request's other times that day: on other passes, `times_gap` hours or more away.
        taken = [t for o in times[c.request.id] if o.occurrence != c.occurrence and o.result == TAKEN for t in times_of(o)]
        if taken:
            # A minute at least: two shots of one satellite that close are on one pass.
            gap = max(c.request.gap_ms, 60000.0)
            ops = [o for o in ops if all(abs(o.time - t) >= gap for t in taken)]
            if not ops:
                why.add(GAP)
                return None
        no_go = c.no_go
        if weighed(c, sat) and no_go is not None and not (forced or c.near or c.manual == TAKEN):
            # Passes too cloudy at their moment are not used (those the forecast does not reach are).
            clear = [o for o in ops if o.cloud is None or o.cloud < no_go]
            if not clear:
                if c.mixed:
                    told = [o.cloud for o in ops if o.cloud is not None]
                    c.optical_cloud = min(told) if told else None
                why.add(CLOUDY)
                return None
            ops = clear
        return ops

    def fits_of(c: Candidate, system: ExportSystem, sat: SatelliteSpec):
        """Whether an area's shots (how many, their scenes) still fit the system's day and the satellite's quota."""
        room = rooms[system.system]
        return lambda shots, scenes: (_room_fits(system, room, c.request.category, shots, scenes)
                                      and book_quota.over(sat, scenes) is None)

    def by_orbit(c: Candidate, system: ExportSystem, sat: SatelliteSpec, why: set, forced: bool = False,
                 start: Optional[bytearray] = None) -> bool:
        """
        Puts a candidate on a pass of the satellite; whether it went (`why`
        told what stopped it). `start`: the points of an area too big for one
        shot taken already (撮像済み・撮像待ち), left out by its shots.
        """
        i = index[id(sat)]
        ops = options_of(c, system, sat, why, forced)
        if ops is None:
            return False
        told = passes.of(c.request, day, i)
        joins = book.joins(c, i, ops, system.merge, system.merge_margin) if start is None else []
        options = joins + book.new_shots(c, i, ops, forced, start, fits_of(c, system, sat))
        clouds = {id(p): _placement_cloud(p, told) for p in options} if weighed(c, sat) else {}
        if weighed(c, sat):
            # The less cloudy first, by tenths; within a tenth as before (into a shot planned, then the smaller angle).
            options.sort(key=lambda p: (p.busy, _tenth(clouds[id(p)])))
        room = rooms[system.system]
        for p in options:
            if p.busy and not forced:
                why.add(BUSY)
                continue
            fits = _room_fits(system, room, c.request.category, placement_rows(p), p.scenes)
            if not fits and not forced:
                why.add(OVER)
                continue
            if not forced and book_quota.over(sat, p.scenes) is not None:
                why.add(QUOTA)
                continue
            placed(c, system, sat, p, clouds.get(id(p)))
            if p.busy:
                c.warnings.append(f"{BUSY}（手動で採用）")
            elif not fits:
                c.warnings.append("枠超過（手動で採用）")
            return True
        if not options:
            # No pass it fits on (its reach, its angles), or none the satellite has time for.
            why.add(BUSY if book.saw_busy else NO_PASS)
        return False

    def sorted_able(c: Candidate, system: ExportSystem, able: Sequence[SatelliteSpec]) -> list[SatelliteSpec]:
        """The satellites (modes) of a system a candidate tries, in order: those it names, else the less cloudy and finer first."""
        able = list(able)
        if c.request.satellites:
            order = {n: i for i, n in enumerate(c.request.satellites)}
            # Either kind: an optical satellite under clouds after the others (a SAR one named next takes it).
            able.sort(key=lambda s: (overcast(c, system, s), min(order.get(s.name, 99), order.get(s.id, 99))))
        else:
            # Optical: the satellite with the less cloudy pass first (by tenths), then the finest resolution.
            # Either kind: as before, but an optical satellite under clouds after the SAR ones (改修案 1).
            able.sort(key=lambda s: (sky(c, system, s), overcast(c, system, s), s.resolution if s.resolution is not None else math.inf,
                                     -left(system, s)))
        return able

    def start_of(c: Candidate) -> Optional[bytearray]:
        """The points of an area too big for one shot that its shots leave out (撮像済み・撮像待ち); None for none."""
        if book is None:
            return None
        outlines = outlines_for(c)
        if not outlines:
            return None
        cells = book.cells_of(c.request, outlines)
        return cells if cells is not None and any(cells) else None

    def fit(c: Candidate) -> None:
        c.result, c.reason, c.system, c.satellite = HELD, "", None, None
        c.shots, c.coverage, c.shot_cloud, c.optical_cloud = [], None, None, None
        c.warnings = [w for w in c.warnings if not _manual_warning(w)]
        req = c.request
        c.able = {}
        for system in _systems_for(req, systems):
            able = [s for s in members[system.system] if why_not(req, s) is None]
            if able:
                c.able[system.system] = able
        start = start_of(c)
        if c.pinned and c.pinned[0] in by_name:
            system = by_name[c.pinned[0]]
            choices = c.able.get(system.system) or members[system.system]
            sat = next((s for s in choices if c.pinned[1] in (s.name, s.id)), choices[0] if choices else None)
            if sat is not None and orbits(system, sat) and by_orbit(c, system, sat, set(), forced=True, start=start):
                c.warnings = [w for w in c.warnings if not _manual_warning(w)]
                return
            scenes = scenes_of(req, *(((sat.swath, sat.length) if sat is not None and (req.modes or req.satellites) else system.scene_km)))
            counted(c, system, sat, scenes)
            return
        if c.manual == HELD:
            c.reason = "手動で見送り"
            return
        if c.held and c.manual != TAKEN:
            c.reason = c.held
            return
        forced = c.manual == TAKEN
        tried_any = False
        why: set = set()
        for system in _systems_for(req, systems):
            able = list(c.able.get(system.system, ()))
            if not able:
                continue
            tried_any = True
            able = sorted_able(c, system, able)
            room = rooms[system.system]
            for sat in able:
                if orbits(system, sat):
                    if by_orbit(c, system, sat, why, start=start):
                        break
                    continue
                if overcast(c, system, sat) and not (c.near or forced):
                    # Either kind, counted: an optical satellite on a cloudy day is left for a SAR one.
                    c.optical_cloud = c.cloud
                    why.add(CLOUDY)
                    continue
                scenes = scenes_of(req, *(((sat.swath, sat.length) if req.modes or req.satellites else system.scene_km)))
                used = per_sat.get(platform_key(sat), 0)
                if (not sat.maxPerDay or used + 1 <= sat.maxPerDay) and _room_fits(system, room, req.category, 1, scenes):
                    if book_quota.over(sat, scenes) is None:
                        counted(c, system, sat, scenes)
                        break
                    why.add(QUOTA)
                    continue
                why.add(OVER)
            if c.result != TAKEN and forced and able:
                sat = able[0]
                if not (orbits(system, sat) and by_orbit(c, system, sat, set(), forced=True, start=start)):
                    counted(c, system, sat, scenes_of(req, *system.scene_km))
                    c.warnings.append(f"{NO_PASS}（手動で採用）" if orbits(system, sat) else "枠超過（手動で採用）")
            if c.result != TAKEN:
                continue
            if c.held:
                c.warnings.append(f"{c.held}（手動で採用）")
            if c.mixed and c.satellite is not None and c.satellite.sar:
                # Either kind gone to SAR: the optical satellites it passed over for the clouds (kind_text).
                told = [clearest(c, system, s) for s in able if overcast(c, system, s)]
                told = [t for t in told if t is not None]
                c.optical_cloud = min(told) if told else c.optical_cloud
            break
        if c.result != TAKEN:
            c.reason = (NO_SYSTEM if not tried_any else QUOTA if QUOTA in why else OVER if OVER in why else BUSY if BUSY in why
                        else CLOUDY if CLOUDY in why else GAP if GAP in why else NO_PASS if NO_PASS in why else OVER)

    def share(c: Candidate) -> None:
        """
        An area too big for one shot that its satellite takes only part of:
        the system's other satellites (one mode a platform, in the order the
        candidate tries them) add their passes, on what is still open, until
        GOAL of it is taken (分担; tsudo, 2026-11-12: 「1日1回にしておいても、
        分担して、カバレッジを高めようとしてくれるんだよね？」). Each keeps its
        interval and day, the system its day, the quota its year.
        """
        system = c.system
        if (book is None or system is None or not system.share or c.cells is None or c.coverage is None
                or c.coverage >= GOAL or c.satellite is None):
            return
        used = {platform_key(c.satellite)}
        for sat in sorted_able(c, system, c.able.get(system.system, ())):
            if c.coverage >= GOAL:
                break
            key = platform_key(sat)
            if key in used or not orbits(system, sat):
                continue
            ops = options_of(c, system, sat, set(), False)
            if not ops:
                continue
            got = [p for p in book.new_shots(c, index[id(sat)], ops, False, c.cells, fits_of(c, system, sat)) if not p.busy]
            if not got or not got[0].shots:
                continue
            p = got[0]
            used.add(key)
            c.shots.extend(book.take(c, p))
            c.cells, c.coverage = p.cells, p.coverage
            spend(c, system, sat, p)

    def explode(c: Candidate) -> None:
        """
        The shots of an area too big for one, each a row of its own (tsudo,
        2026-11-12: 撮る部分だけをファイルに): the first of its own satellite
        stays with the candidate (part 1), the others become parts 2 … in
        time order, each on its satellite with its own pass.
        """
        if c.result != TAKEN or c.gain is None or not c.shots:
            return
        mine = index.get(id(c.satellite), -1)
        own = [s for s in c.shots if s.sat == mine]
        head = min(own or c.shots, key=lambda s: s.time)
        rest = sorted((s for s in c.shots if s is not head), key=lambda s: s.time)
        c.part, c.parts = 1, 1 + len(rest)
        c.shots, c.gain = [head], head.gain
        if weighed(c, c.satellite):
            c.shot_cloud = _cloud_near(passes.of(c.request, day, head.sat), head.time)
        parts = []
        for n, shot in enumerate(rest, 2):
            sat = sats[shot.sat]
            p = Candidate(c.request, cloud=c.cloud, weather=c.weather, mixed=c.mixed, timed=c.timed, near=c.near, carried=c.carried,
                          occurrence=c.occurrence, first=c.first, main=c, covered=c.covered, shares=c.shares)
            p.result, p.system, p.satellite, p.able = TAKEN, c.system, sat, c.able
            p.shots, p.part, p.parts, p.gain, p.coverage = [shot], n, c.parts, shot.gain, c.coverage
            p.pass_key = (platform_key(sat), round(shot.time / 1000))
            p.warnings = [w for w in c.warnings if not _manual_warning(w)]
            if weighed(c, sat):
                p.shot_cloud = _cloud_near(passes.of(c.request, day, shot.sat), shot.time)
            shot.members = [p]
            parts.append(p)
        parts_of[id(c)] = parts
        times[c.request.id].extend(parts)
        if c.coverage is not None and c.coverage < GOAL:
            # Short of the goal with every satellite that had room (what was taken before counts).
            before = "（撮像済み・撮像待ちを含む）" if outlines_for(c) else ""
            for x in (c, *parts):
                x.warnings.append(f"{PART}{percent(c.coverage)}%{before}")

    # Those already written for another system first: they hold their room there, whatever comes before them.
    pinned = [c for c in out if c.pinned and c.pinned[0] in by_name]
    for c in pinned + [c for c in out if not (c.pinned and c.pinned[0] in by_name)]:
        fit(c)
        if c.result == TAKEN:
            share(c)
            explode(c)
    for c in out:
        if c.result == TAKEN and c.shots:
            c.scenes = 0 if c.member else sum(s.frames for s in c.shots)
    for same in times.values():
        need = same[0].request.times[0]
        got = [c for c in same if c.result == TAKEN]
        n = len({c.occurrence for c in got})
        if got and n < need:
            # Taken fewer times than the day needs: what fits goes all the same (tsudo: 「取れる分だけ出す」).
            for c in got:
                c.warnings.append(f"{FEW}{n}/{need} 回）")
    result: list[Candidate] = []
    for c in out:
        result.append(c)
        for p in parts_of.get(id(c), ()):
            p.scenes = sum(s.frames for s in p.shots)
            result.append(p)
        if c.result == TAKEN:
            # Shots left out by hand: shown, so that they can be taken again.
            for p in held_parts:
                if p.main is c:
                    p.result, p.reason, p.shots = HELD, "手動で見送り", []
                    result.append(p)
    return result


def room_text(cands: Sequence[Candidate], systems: Sequence[ExportSystem]) -> dict[str, str]:
    """What each system has left: 「12/20 件・30/50 枚」."""
    out = {}
    for s in systems:
        # A request put into another's shot is in that one's row.
        taken = [c for c in cands if c.result == TAKEN and c.system is s and not c.member]
        parts = [f"{len(taken)}{'/' + str(s.limit['count']) if 'count' in s.limit else ''} 件",
                 f"{sum(c.scenes for c in taken)}{'/' + str(s.limit['scenes']) if 'scenes' in s.limit else ''} 枚"]
        out[s.system] = "・".join(parts)
    return out


def satellite_name(c: Candidate) -> str:
    return satellite_label(c.satellite) if c.satellite else ""


def merge_text(c: Candidate) -> str:
    """What the calculation shows of a request put together with others: 「まとめ 3 件（…）」 or 「…の撮像にまとめ」."""
    if not c.merged:
        return ""
    lead = c.lead
    if c.member:
        return f"まとめ（「{lead.request.name or lead.request.id}」の撮像に同乗）"
    others = [m.request.name or m.request.id for m in c.together if m.key != c.key]
    return f"まとめ {len(c.together)} 件（{'・'.join(others)} と 1 回の撮像）"


def time_text(ms: Optional[float]) -> str:
    """A moment as JST 「14:05」."""
    from datetime import datetime

    from .model import JST
    if ms is None or not math.isfinite(ms):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%H:%M")


def cloud_tip(c: Candidate) -> str:
    """Where a candidate's cover comes from (the calculation's tooltip)."""
    if c.mixed:
        no_go = round(optical_no_go(c.request))
        head = f"種別「任意」: 雲量は光学のパスだけに効き、{no_go}% 以上の光学のパスは使わない（SAR には効かない）"
        if c.forecast is None:
            return head if c.satellite is not None and c.satellite.sar else head + "。雲量予報なし"
        return head + "。" + ("撮像時刻の予報" if c.shot_cloud is not None else "いちばん雲の少ない光学のパスの予報" if c.timed
                             else "日中（地方時 9〜15 時）の平均")
    if not c.weather:
        return ""
    if c.forecast is None:
        return "雲量予報なし"
    how = "前後の予報を時刻の近さで按分、範囲の格子点の平均"
    if c.shot_cloud is not None and c.planned is not None:
        text = f"撮像時刻 {time_text(c.planned)} の予報（{how}）"
        if c.timed and c.cloud is not None and round(c.cloud) != round(c.shot_cloud):
            text += f"。この日いちばん雲の少ないパスは {round(c.cloud)}%（優先度の 1 の位はこちら）"
        return text
    if c.timed:
        return f"この日いちばん雲の少ないパスの撮像時刻の予報（{how}）"
    return "日中（地方時 9〜15 時）の平均（軌道で詰めない衛星）"


def pass_text(c: Candidate) -> str:
    """The pass a request is taken on (the calculation's tooltip): when, angles, direction, scenes."""
    if not c.shots:
        return ""
    shot = c.shots[0]
    look = shot.look
    parts = [f"{time_text(c.planned)}（日本時間）", f"オフナディア角 {look['offNadir']:.1f}°", f"入射角 {look['incidence']:.1f}°",
             "昇交" if shot.ascending else "降交", f"{shot.side}観測", f"連続 {shot.frames} シーン"]
    if len(c.shots) > 1:
        parts.append(f"ほかに {len(c.shots) - 1} 回の撮像")
    if c.coverage is not None:
        parts.append(f"範囲の約 {round(c.coverage * 100)}%")
    return "・".join(parts)
