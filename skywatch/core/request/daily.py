"""
The day's work (日次出力): the requests of a day gathered, weighed by the
weather, raised when they were missed or their deadline is near, sent to
the systems that can take them and fitted into each system's daily limits
in order of priority.

1. Candidates: requests 有効 on the day (period, repeat); a 期間内1回 request
   already taken is 完了 and so left out.
2. Weather (optical): the day's cover over the area. At or above the
   request's 「実施しない雲量」 it is held back (雲量), unless it is
   期間内1回 with 2 days or less left: then it goes, with a warning.
   The cover's tenth becomes the priority's ones digit.
3. Raises (繰り上げ): requests missed (未撮像) or 期間内1回 near the deadline
   are offered for a raise of 100; those ticked count now. A 定点 request
   missed on a day of its repeat (weekday, interval) is carried over to the
   next day planned, even when that is not one of its days (持ち越し).
4. Order: output priority, then the nearer deadline, then the older request.
5. Fitting: in that order, each request goes to the first system (its own
   order, then `requestExports`') with a satellite that can take it and room
   left: the system's `limit` (count, scenes), its `quota` by category, and
   the satellite's `maxPerDay`. A request naming satellites tries them in
   that order; otherwise the finest resolution with room comes first.
   Requests that fit nowhere are held back (枠超過, or 該当システムなし).

   With the satellites' orbits (passes.Passes, tsudo 2026-11-11) a request
   goes to a pass of the satellite over it that day (撮像機会なし when there
   is none): into a shot already planned on that pass when it fits in it
   (まとめ, shots.py; it then adds no row to the system's file and uses
   neither the satellite's interval nor its day), or into a new shot where
   the satellite has time (`minInterval` between shots, `maxPerDay` shots
   a day; 衛星の空きなし when it has none). A satellite without a readable
   TLE, or a system with `orbit: false`, is fitted by counting as before:
   `maxPerDay` requests a day, scenes from a grid over the area.

Hand changes (taking in, leaving out, moving up or down) are kept on the
candidates and the fitting is done again.

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
from .model import ImagingRequest, clamp_priority, day_bounds, days_left, next_boost, occurs_on
from .passes import Passes
from .route import fitting, scenes_of, system_of_satellite, why_not
from .shots import GOAL, Book, Placement
from .systems import ExportSystem
from .weather import with_cloud

#: Days before the deadline from which a 期間内1回 request goes whatever the weather (and is offered a raise).
DEADLINE_DAYS = 2

TAKEN, HELD = "採用", "見送り"
#: Why a request is held back in the fitting.
NO_SYSTEM, OVER, NO_PASS, BUSY = "該当システムなし", "枠超過", "撮像機会なし", "衛星の空きなし（撮像間隔・1 日の回数）"
#: The start of the warning of an area the day's passes take only part of.
PART = "この日の撮像機会で範囲の約 "


@dataclass
class Candidate:
    """One request on the day, and what became of it."""
    request: ImagingRequest
    #: The day's cover over the area (percent); None when not asked (SAR) or not forecast.
    cloud: Optional[float] = None
    #: Whether the weather was asked for this request (optical).
    weather: bool = False
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
        return lead is not None and lead.request.id != self.request.id

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
        """The raise that counts now."""
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
        if not self.weather:
            return ""
        if self.cloud is None:
            return "予報なし"
        no_go = self.request.cloud_no_go
        return "雲多" if no_go is not None and self.cloud >= no_go else "OK"


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


def gather(requests: Sequence[ImagingRequest], day: date, sats: Sequence[SatelliteSpec],
           clouds: dict, missed: set) -> list[Candidate]:
    """
    The candidates of a day: requests that occur on it, with their cover
    (`clouds`: request id → percent or None), holds for the weather, and
    raises offered (`missed`: ids of requests found 未撮像 and not yet raised for it).
    """
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
        if wants_weather(req, sats):
            c.weather = True
            c.cloud = clouds.get(req.id)
            if c.cloud is None:
                c.warnings.append("予報なし")
            elif req.cloud_no_go is not None and c.cloud >= req.cloud_no_go:
                if near:
                    c.warnings.append(f"雲量 {round(c.cloud)}%（実施しない雲量 {round(req.cloud_no_go)}% 以上・期限間近のため出力）")
                else:
                    c.held = f"雲量 {round(c.cloud)}%（{round(req.cloud_no_go)}% 以上）"
        reasons = []
        if req.id in missed:
            reasons.append("未撮像")
        if near:
            reasons.append(f"期限まで {max(0, left)} 日")
        offer = next_boost(req)
        if reasons and offer is not None:
            c.boost_offer = offer
            c.boost_reason = "・".join(reasons)
        out.append(c)
    return out


def _order_key(c: Candidate):
    end = c.request.end if math.isfinite(c.request.end) else math.inf
    created = c.request.created if c.request.created is not None else math.inf
    return (c.priority, end, created, c.request.name)


def ordered(cands: Sequence[Candidate]) -> list[Candidate]:
    """Candidates in the order they are fitted: by hand where set, otherwise by priority, deadline and age."""
    by_priority = sorted(cands, key=_order_key)
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


def _manual_warning(w: str) -> bool:
    """A warning the fitting itself adds (made again by each fitting)."""
    return w.startswith(OVER) or w.endswith("（手動で採用）") or w.startswith(PART)


def _room_fits(system: ExportSystem, room: Room, category: str, rows: int, scenes: int) -> bool:
    return (("count" not in system.limit or room.count + rows <= system.limit["count"])
            and ("scenes" not in system.limit or room.scenes + scenes <= system.limit["scenes"])
            and (not rows or category not in system.quota or room.by_category.get(category, 0) + rows <= system.quota[category]))


def allocate(cands: Sequence[Candidate], systems: Sequence[ExportSystem], sats: Sequence[SatelliteSpec],
             passes: Optional[Passes] = None, day: Optional[date] = None) -> list[Candidate]:
    """
    Fits the candidates into the systems' limits (in place); the candidates in
    their order. With `passes` (the satellites' orbits) and `day`, to the
    passes of the day, put together where they fit in one shot.
    """
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
    out = ordered(cands)
    by_name = {s.system: s for s in systems}

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

    def placed(c: Candidate, system: ExportSystem, sat: SatelliteSpec, p: Placement) -> None:
        room = rooms[system.system]
        c.result, c.system, c.satellite = TAKEN, system, sat
        c.shots = book.take(c, p)
        c.coverage = p.coverage
        if p.new:
            room.count += 1
            room.by_category[c.request.category] = room.by_category.get(c.request.category, 0) + 1
        room.scenes += p.scenes
        if p.coverage is not None and p.coverage < GOAL:
            c.warnings.append(f"{PART}{round(p.coverage * 100)}%")

    def by_orbit(c: Candidate, system: ExportSystem, sat: SatelliteSpec, why: set, forced: bool = False) -> bool:
        """Puts a candidate on a pass of the satellite; whether it went (`why` told what stopped it)."""
        i = index[id(sat)]
        ops = passes.of(c.request, day, i)
        if not ops:
            why.add(NO_PASS)
            return False
        options = book.joins(c, i, ops, system.merge) + book.new_shots(c, i, ops, forced)
        room = rooms[system.system]
        for p in options:
            if p.busy and not forced:
                why.add(BUSY)
                continue
            fits = _room_fits(system, room, c.request.category, 1 if p.new else 0, p.scenes)
            if not fits and not forced:
                why.add(OVER)
                continue
            placed(c, system, sat, p)
            if p.busy:
                c.warnings.append(f"{BUSY}（手動で採用）")
            elif not fits:
                c.warnings.append("枠超過（手動で採用）")
            return True
        if not options:
            # No pass it fits on (its reach, its angles), or none the satellite has time for.
            why.add(BUSY if book.saw_busy else NO_PASS)
        return False

    # Those already written for another system first: they hold their room there, whatever comes before them.
    pinned = [c for c in out if c.pinned and c.pinned[0] in by_name]
    for c in pinned + [c for c in out if not (c.pinned and c.pinned[0] in by_name)]:
        c.result, c.reason, c.system, c.satellite = HELD, "", None, None
        c.shots, c.coverage = [], None
        c.warnings = [w for w in c.warnings if not _manual_warning(w)]
        req = c.request
        c.able = {}
        for system in _systems_for(req, systems):
            able = [s for s in members[system.system] if why_not(req, s) is None]
            if able:
                c.able[system.system] = able
        if c.pinned and c.pinned[0] in by_name:
            system = by_name[c.pinned[0]]
            choices = c.able.get(system.system) or members[system.system]
            sat = next((s for s in choices if c.pinned[1] in (s.name, s.id)), choices[0] if choices else None)
            if sat is not None and orbits(system, sat) and by_orbit(c, system, sat, set(), forced=True):
                c.warnings = [w for w in c.warnings if not _manual_warning(w)]
                continue
            scenes = scenes_of(req, *(((sat.swath, sat.length) if sat is not None and (req.modes or req.satellites) else system.scene_km)))
            counted(c, system, sat, scenes)
            continue
        if c.manual == HELD:
            c.reason = "手動で見送り"
            continue
        if c.held and c.manual != TAKEN:
            c.reason = c.held
            continue
        forced = c.manual == TAKEN
        tried_any = False
        why: set = set()
        for system in _systems_for(req, systems):
            able = list(c.able.get(system.system, ()))
            if not able:
                continue
            tried_any = True
            if req.satellites:
                order = {n: i for i, n in enumerate(req.satellites)}
                able.sort(key=lambda s: min(order.get(s.name, 99), order.get(s.id, 99)))
            else:
                able.sort(key=lambda s: (s.resolution if s.resolution is not None else math.inf, -left(system, s)))
            room = rooms[system.system]
            for sat in able:
                if orbits(system, sat):
                    if by_orbit(c, system, sat, why):
                        break
                    continue
                scenes = scenes_of(req, *(((sat.swath, sat.length) if req.modes or req.satellites else system.scene_km)))
                used = per_sat.get(platform_key(sat), 0)
                if (not sat.maxPerDay or used + 1 <= sat.maxPerDay) and _room_fits(system, room, req.category, 1, scenes):
                    counted(c, system, sat, scenes)
                    break
                why.add(OVER)
            if c.result != TAKEN and forced and able:
                sat = able[0]
                if not (orbits(system, sat) and by_orbit(c, system, sat, set(), forced=True)):
                    counted(c, system, sat, scenes_of(req, *system.scene_km))
                    c.warnings.append(f"{NO_PASS}（手動で採用）" if orbits(system, sat) else "枠超過（手動で採用）")
            if c.result != TAKEN:
                continue
            if c.held:
                c.warnings.append(f"{c.held}（手動で採用）")
            break
        if c.result != TAKEN:
            c.reason = (NO_SYSTEM if not tried_any else OVER if OVER in why else BUSY if BUSY in why
                        else NO_PASS if NO_PASS in why else OVER)
    for c in out:
        if c.result == TAKEN and c.shots:
            c.scenes = 0 if c.member else sum(s.frames for s in c.shots)
    return out


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
    others = [m.request.name or m.request.id for m in c.together if m.request.id != c.request.id]
    return f"まとめ {len(c.together)} 件（{'・'.join(others)} と 1 回の撮像）"


def time_text(ms: Optional[float]) -> str:
    """A moment as JST 「14:05」."""
    from datetime import datetime

    from .model import JST
    if ms is None or not math.isfinite(ms):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%H:%M")


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
