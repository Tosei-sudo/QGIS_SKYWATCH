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
   are offered for a raise of 100; those ticked count now.
4. Order: output priority, then the nearer deadline, then the older request.
5. Fitting: in that order, each request goes to the first system (its own
   order, then `requestExports`') with a satellite that can take it and room
   left: the system's `limit` (count, scenes), its `quota` by category, and
   the satellite's `maxPerDay` (requests a day). A request naming satellites tries them in
   that order; otherwise the finest resolution with room comes first.
   Requests that fit nowhere are held back (枠超過, or 該当システムなし).

Hand changes (taking in, leaving out, moving up or down) are kept on the
candidates and the fitting is done again.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from ..plan.satellites import SatelliteSpec, platform_key, satellite_label
from .model import ImagingRequest, clamp_priority, days_left, next_boost, occurs_on
from .route import fitting, scenes_of, system_of_satellite, why_not
from .systems import ExportSystem
from .weather import with_cloud

#: Days before the deadline from which a 期間内1回 request goes whatever the weather (and is offered a raise).
DEADLINE_DAYS = 2

TAKEN, HELD = "採用", "見送り"


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
        if not occurs_on(req, day):
            continue
        c = Candidate(req)
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


def allocate(cands: Sequence[Candidate], systems: Sequence[ExportSystem], sats: Sequence[SatelliteSpec]) -> list[Candidate]:
    """Fits the candidates into the systems' limits (in place); the candidates in their order."""
    rooms = {s.system: Room() for s in systems}
    per_sat: dict[str, int] = {}
    members = {s.system: [] for s in systems}
    for sat in sats:
        sys_ = system_of_satellite(sat, systems)
        if sys_ is not None:
            members[sys_.system].append(sat)
    out = ordered(cands)
    for c in out:
        c.result, c.reason, c.system, c.satellite = HELD, "", None, None
        c.warnings = [w for w in c.warnings if not w.startswith("枠超過")]
        req = c.request
        if c.manual == HELD:
            c.reason = "手動で見送り"
            continue
        if c.held and c.manual != TAKEN:
            c.reason = c.held
            continue
        tried_any = False
        for system in _systems_for(req, systems):
            able = [s for s in members[system.system] if why_not(req, s) is None]
            if not able:
                continue
            tried_any = True
            if req.satellites:
                order = {n: i for i, n in enumerate(req.satellites)}
                able.sort(key=lambda s: min(order.get(s.name, 99), order.get(s.id, 99)))
            else:
                able.sort(key=lambda s: (s.resolution if s.resolution is not None else math.inf,
                                         -((s.maxPerDay or 10 ** 6) - per_sat.get(platform_key(s), 0))))
            room = rooms[system.system]
            forced = c.manual == TAKEN
            chosen = None
            for sat in able:
                scenes = scenes_of(req, *(((sat.swath, sat.length) if req.modes or req.satellites else system.scene_km)))
                used = per_sat.get(platform_key(sat), 0)
                fits = ((not sat.maxPerDay or used + 1 <= sat.maxPerDay)
                        and ("count" not in system.limit or room.count + 1 <= system.limit["count"])
                        and ("scenes" not in system.limit or room.scenes + scenes <= system.limit["scenes"])
                        and (req.category not in system.quota or room.by_category.get(req.category, 0) + 1 <= system.quota[req.category]))
                if fits:
                    chosen = (sat, scenes)
                    break
            if chosen is None and forced and able:
                sat = able[0]
                chosen = (sat, scenes_of(req, *system.scene_km))
                c.warnings.append("枠超過（手動で採用）")
            if chosen is None:
                continue
            sat, scenes = chosen
            c.result, c.system, c.satellite, c.scenes = TAKEN, system, sat, scenes
            room.count += 1
            room.scenes += scenes
            room.by_category[req.category] = room.by_category.get(req.category, 0) + 1
            per_sat[platform_key(sat)] = per_sat.get(platform_key(sat), 0) + 1
            if c.held:
                c.warnings.append(f"{c.held}（手動で採用）")
            break
        if c.result != TAKEN:
            c.reason = "枠超過" if tried_any else "該当システムなし"
    return out


def room_text(cands: Sequence[Candidate], systems: Sequence[ExportSystem]) -> dict[str, str]:
    """What each system has left: 「12/20 件・30/50 枚」."""
    out = {}
    for s in systems:
        taken = [c for c in cands if c.result == TAKEN and c.system is s]
        parts = [f"{len(taken)}{'/' + str(s.limit['count']) if 'count' in s.limit else ''} 件",
                 f"{sum(c.scenes for c in taken)}{'/' + str(s.limit['scenes']) if 'scenes' in s.limit else ''} 枚"]
        out[s.system] = "・".join(parts)
    return out


def satellite_name(c: Candidate) -> str:
    return satellite_label(c.satellite) if c.satellite else ""
