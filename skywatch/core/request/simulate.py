"""
Trying daily limits before writing (上限シミュレーション): the day's work
of 撮像計画ファイル出力 run over the coming days, on a copy of the ledger,
with the systems' limits or the systems used changed by hand. Nothing is
written: neither files, nor the history, nor the ledger.

Each day is fitted as the real day would be (daily.gather / allocate):
priority, the next system when one is full, raises of 100 offered for 未撮像
or a deadline 2 days away (taken, as the confirmation screen ticks them),
and the weather where a forecast covers the day. Between days the copy
changes as the follow-up would change the ledger, taking every request
handed out as captured: its raise is undone and, 期間内1回, it is 完了.
A 期間内1回 request still not handed out on its last day has missed its
deadline (期限切れ); before that it is carried to the next day (持ち越し).
A 毎回 request not handed out on its day loses that day (欠測).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional, Sequence

from ..plan.satellites import SatelliteSpec
from .daily import TAKEN, Candidate, _systems_for, allocate, gather
from .follow import missed_ids
from .model import ImagingRequest, days_left, jst_day
from .route import system_of_satellite, why_not
from .store import HistoryRow
from .systems import ExportSystem

#: The most days a run covers.
MOST_DAYS = 60


@dataclass
class SystemChange:
    """What is tried for one system: whether it is used, and its limits (None: no limit)."""
    enabled: bool = True
    count: Optional[int] = None
    scenes: Optional[int] = None
    #: Limits by category ({"定点": n, "臨時": n}); a category left out has none.
    quota: dict = field(default_factory=dict)


def change_of(system: ExportSystem) -> SystemChange:
    """A system's limits as the settings give them."""
    return SystemChange(True, system.limit.get("count"), system.limit.get("scenes"), dict(system.quota))


def changed_systems(systems: Sequence[ExportSystem], changes: dict) -> list[ExportSystem]:
    """The systems with what is tried (`changes`: system name → SystemChange); those switched off are left out."""
    out = []
    for s in systems:
        ch = changes.get(s.system)
        if ch is None:
            out.append(s)
            continue
        if not ch.enabled:
            continue
        limit = {k: int(v) for k, v in (("count", ch.count), ("scenes", ch.scenes)) if v is not None}
        quota = {k: int(v) for k, v in ch.quota.items() if v is not None}
        out.append(dataclasses.replace(s, limit=limit, quota=quota))
    return out


@dataclass
class Expired:
    """A 期間内1回 request that was never handed out by its last day."""
    request: ImagingRequest
    day: date
    #: Why it was held back on its last day (枠超過, 雲量 …).
    reason: str


@dataclass
class DayResult:
    """One day of a run."""
    day: date
    candidates: int = 0
    taken: int = 0
    #: Held back as no system had room (枠超過).
    over: int = 0
    #: Taken by a system other than the first that could have taken it (次のシステムへ).
    spilled: int = 0
    #: Held back for the weather.
    cloud: int = 0
    #: Held back as no system has a satellite for it.
    no_system: int = 0
    #: Raises offered (and taken) on the day.
    raised: int = 0
    #: 期間内1回 requests not handed out, with days still left.
    carried: int = 0
    #: 毎回 requests not handed out on their day.
    lost: int = 0
    expired: list = field(default_factory=list)
    #: system name → (requests, scenes) taken.
    used: dict = field(default_factory=dict)


@dataclass
class Simulation:
    """A run over the coming days."""
    start: date
    systems: list
    days: list = field(default_factory=list)

    def total(self, name: str) -> int:
        """The sum of one count over the days (`taken`, `over` …; `expired` counts requests)."""
        if name == "expired":
            return sum(len(d.expired) for d in self.days)
        return sum(getattr(d, name) for d in self.days)

    @property
    def expired(self) -> list[Expired]:
        return [e for d in self.days for e in d.expired]

    @property
    def carried_at_end(self) -> int:
        """期間内1回 requests still not handed out after the last day."""
        return self.days[-1].carried if self.days else 0


def _first_system(req: ImagingRequest, systems: Sequence[ExportSystem], members: dict) -> Optional[str]:
    """The first system (in the order the request tries them) with a satellite that can take it."""
    for s in _systems_for(req, systems):
        if any(why_not(req, sat) is None for sat in members[s.system]):
            return s.system
    return None


def simulate(requests: Sequence[ImagingRequest], start: date, days: int, systems: Sequence[ExportSystem],
             sats: Sequence[SatelliteSpec], clouds: Optional[Callable[[date], dict]] = None,
             history: Sequence[HistoryRow] = (), cancelled: Optional[Callable[[], bool]] = None) -> Simulation:
    """
    Runs the day's work from `start` for `days` days on a copy of `requests`.
    `clouds` gives a day's cover by request id (None where not forecast);
    `history` tells which requests were found 未撮像 (offered a raise on the first day).
    """
    ledger = [dataclasses.replace(r) for r in requests]
    systems = list(systems)
    members = {s.system: [] for s in systems}
    for sat in sats:
        own = system_of_satellite(sat, systems)
        if own is not None:
            members[own.system].append(sat)
    missed = missed_ids(history)
    out = Simulation(start, systems)
    for i in range(max(1, min(MOST_DAYS, days))):
        if cancelled and cancelled():
            break
        day = start + timedelta(days=i)
        cands: list[Candidate] = gather(ledger, day, sats, clouds(day) if clouds else {}, missed if i == 0 else set())
        cands = allocate(cands, systems, sats)
        result = DayResult(day, candidates=len(cands))
        for c in cands:
            req = c.request
            if c.boost_offer is not None:
                result.raised += 1
                req.boost = c.boost_offer
            if c.result == TAKEN:
                result.taken += 1
                name = c.system.system
                n, scenes = result.used.get(name, (0, 0))
                result.used[name] = (n + 1, scenes + c.scenes)
                if _first_system(req, systems, members) not in (None, name):
                    result.spilled += 1
                # Handed out, so taken as captured: the follow-up undoes the raise and ends a 期間内1回 request.
                req.boost = 0
                if req.achieve == "期間内1回":
                    req.status = "完了"
                continue
            if c.reason == "枠超過":
                result.over += 1
            elif c.reason == "該当システムなし":
                result.no_system += 1
            elif c.held and c.reason == c.held:
                result.cloud += 1
            if req.achieve == "期間内1回":
                if days_left(req, day) <= 0:
                    result.expired.append(Expired(req, day, c.reason))
                else:
                    result.carried += 1
            else:
                result.lost += 1
        out.days.append(result)
    return out


def horizon_days(start: date, days: int) -> list[date]:
    """The days of a run."""
    return [start + timedelta(days=i) for i in range(max(1, min(MOST_DAYS, days)))]


def deadline_text(req: ImagingRequest) -> str:
    """The request's last day (JST)."""
    try:
        return jst_day(req.end).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


#: The rows of the summary: (label, how it is got from a run).
SUMMARY = (
    ("採用", lambda s: s.total("taken")),
    ("枠超過で見送り", lambda s: s.total("over")),
    ("次のシステムへ回送", lambda s: s.total("spilled")),
    ("雲量で見送り", lambda s: s.total("cloud")),
    ("該当システムなし", lambda s: s.total("no_system")),
    ("繰り上げ", lambda s: s.total("raised")),
    ("欠測（毎回）", lambda s: s.total("lost")),
    ("期限切れ（期間内1回）", lambda s: s.total("expired")),
    ("最終日の持ち越し", lambda s: s.carried_at_end),
)
