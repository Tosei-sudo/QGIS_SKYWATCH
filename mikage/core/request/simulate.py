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

With the satellites' orbits (`passes`, passes.find_passes over the whole
run) each day goes to the passes of that day as the real day does: no pass
(撮像機会なし), the satellites' interval and shots a day (counted as 枠超過),
and requests put into one shot (まとめ: no row of their own).

The yearly quotas (年間撮像上限, `quotas`) are kept day by day: what the
earlier days of the run handed out is taken off what is left (handed out =
captured), and a year starting within the run starts afresh.

The systems' use time limits (軌道の使用時間, `useTime`, use_time.py) are
kept on each day's passes, a pass crossing midnight with the shots the day
before handed out on it (not those handed out before the run).

An area too big for one shot shared out among satellites (分担) is one
request taken, its shots rows of their systems. A 期間内1回 area whose
day's shots take less than 95% of it (with what earlier days took) stays
on: the next days hand out only the rest (範囲の一部だけ), as the
follow-up and the day's work would (covered.py).

A request taken several times a day (1 日の回数) counts as taken on a day
when its n times are handed out; taken fewer times (回数不足) the day does
not count: 期間内1回 goes on to the next day, 毎回 loses it. Its times past
n count when taken and are not counted when they found no room.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional, Sequence

from ..plan.satellites import SatelliteSpec
from .covered import GOAL, Covered, subtracts
from .daily import BUSY, CLOUDY, GAP, NO_PASS, NO_SYSTEM, OVER, TAKEN, Candidate, _systems_for, allocate, gather
from .follow import missed_ids
from .model import ImagingRequest, day_bounds, days_left, jst_day
from .passes import Passes, find_passes
from .quota import QUOTA, QuotaData
from .route import system_of_satellite, why_not
from .store import HistoryRow
from .systems import ExportSystem
from .use_time import USE_DAY, USE_PASS, UseBook

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
    #: Held back as no satellite that can take it passes over it that day (from the orbits).
    no_pass: int = 0
    #: Taken in another request's shot (まとめ: no row of their own).
    merged: int = 0
    #: Raises offered (and taken) on the day.
    raised: int = 0
    #: 期間内1回 requests not handed out, with days still left.
    carried: int = 0
    #: 毎回 requests not handed out on their day.
    lost: int = 0
    #: Requests handed out fewer times than their 1 日の回数 asks (the day does not count for them).
    short: int = 0
    #: Held back as every mode that could take them had used its year's scenes (年間撮像上限).
    quota: int = 0
    #: Held back as the satellites that could take them would be used past their seconds a pass or a day (軌道の使用時間).
    use_time: int = 0
    #: 期間内1回 areas handed out with less than 95% of them taken so far (the rest goes on to the next days).
    partial: int = 0
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


def run_passes(requests: Sequence[ImagingRequest], start: date, days: int, sats: Sequence[SatelliteSpec],
               now: Optional[float] = None, cancelled: Optional[Callable[[], bool]] = None) -> Passes:
    """The passes over the days of a run (worked out once for the settings' limits and those tried)."""
    first = day_bounds(start)[0]
    last = day_bounds(horizon_days(start, days)[-1])[1]
    return find_passes(requests, first, last, sats, now, cancelled)


def simulate(requests: Sequence[ImagingRequest], start: date, days: int, systems: Sequence[ExportSystem],
             sats: Sequence[SatelliteSpec], clouds: Optional[Callable[[date], dict]] = None,
             history: Sequence[HistoryRow] = (), cancelled: Optional[Callable[[], bool]] = None,
             passes: Optional[Passes] = None, quotas: Optional[QuotaData] = None,
             chances: Optional[Callable[[Sequence[ImagingRequest], date, dict], dict]] = None) -> Simulation:
    """
    Runs the day's work from `start` for `days` days on a copy of `requests`.
    `clouds` gives a day's cover by request id (None where not forecast);
    `history` tells which requests were found 未撮像 (offered a raise on the first day);
    `passes` the satellites' passes over the run (run_passes; None: fitted by counting);
    `quotas` the yearly quotas and the shots of their years (quota.read_quota_data);
    `chances(ledger, day, the day's clouds)` the 期間内1回 requests' chances left (chances.chances_of, within the run).
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
    run_days = horizon_days(start, days)
    # The days of the run are planned again: what was handed out for them is not 見込み.
    replanned = (run_days[0].isoformat(), run_days[-1].isoformat())
    #: (quota index, year's first day) → scenes handed out by the run's earlier days.
    handed: dict = {}
    #: request id → what of its area the run's earlier days handed out (taken as captured).
    covered: dict = {}
    #: The uses of the day before's shots (a pass crossing midnight counts them).
    before: list = []
    for i in range(max(1, min(MOST_DAYS, days))):
        if cancelled and cancelled():
            break
        day = start + timedelta(days=i)
        day_clouds = clouds(day) if clouds else {}
        left = chances(ledger, day, day_clouds) if chances else None
        cands: list[Candidate] = gather(ledger, day, sats, day_clouds, missed if i == 0 else set(), covered, left)
        book = quotas.book(day, replanned, handed) if quotas is not None and quotas.quotas else None
        uses = UseBook(before)
        cands = allocate(cands, systems, sats, passes, day, book, uses)
        before = uses.own()
        if book is not None:
            for index, u in zip(book.indices, book.usages):
                key = (index, u.window.first)
                handed[key] = handed.get(key, 0) + book.spent.get(id(u), 0)
        result = DayResult(day, candidates=sum(1 for c in cands if not c.extra and c.main is None))
        times: dict = {}
        for c in cands:
            times.setdefault(c.request.id, []).append(c)
            req = c.request
            if c.boost_offer is not None:
                result.raised += 1
            if c.result == TAKEN:
                # The shots of an area shared out past its first are rows, not requests.
                result.taken += 0 if c.main is not None else 1
                name = c.system.system
                n, scenes = result.used.get(name, (0, 0))
                # One put into another's shot is in that one's row.
                result.used[name] = (n + (0 if c.member else 1), scenes + c.scenes)
                if c.member:
                    result.merged += 1
                if _first_system(req, systems, members) not in (None, name):
                    result.spilled += 1
                continue
            if c.extra:
                # A time past the n it needs, tried only in the room left.
                continue
            if c.reason == QUOTA:
                result.quota += 1
            elif c.reason in (USE_PASS, USE_DAY):
                result.use_time += 1
            elif c.reason in (OVER, BUSY):
                result.over += 1
            elif c.reason == NO_SYSTEM:
                result.no_system += 1
            elif c.reason in (NO_PASS, GAP):
                result.no_pass += 1
            elif c.held and c.reason == c.held or c.reason == CLOUDY:
                result.cloud += 1
        for same in times.values():
            req = same[0].request
            if same[0].boost_offer is not None:
                req.boost = same[0].boost_offer
            got = len({c.occurrence for c in same if c.result == TAKEN})
            if got >= req.times[0]:
                # Handed out, so taken as captured: the follow-up undoes the raise and ends a 期間内1回 request.
                req.boost = 0
                share = next((c.coverage for c in same if c.result == TAKEN and c.main is None and c.coverage is not None), None)
                if req.achieve == "期間内1回" and subtracts(req) and share is not None and share < GOAL:
                    # Only part of its area: what was handed out counts as taken, the rest goes on.
                    got_area = covered.setdefault(req.id, Covered())
                    got_area.taken.extend([s.ring()] for c in same if c.result == TAKEN for s in c.shots)
                    result.partial += 1
                    continue
                if req.achieve == "期間内1回":
                    req.status = "完了"
                continue
            if got:
                result.short += 1
            if req.achieve == "期間内1回":
                if days_left(req, day) <= 0:
                    first = next((c for c in same if c.result != TAKEN and not c.extra), same[0])
                    result.expired.append(Expired(req, day, first.reason or f"回数不足（{got}/{req.times[0]} 回）"))
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
    ("年間上限で見送り", lambda s: s.total("quota")),
    ("使用時間の上限で見送り", lambda s: s.total("use_time")),
    ("次のシステムへ回送", lambda s: s.total("spilled")),
    ("雲量で見送り", lambda s: s.total("cloud")),
    ("該当システムなし", lambda s: s.total("no_system")),
    ("撮像機会なし", lambda s: s.total("no_pass")),
    ("まとめて採用（ほかの依頼の撮像に同乗）", lambda s: s.total("merged")),
    ("繰り上げ", lambda s: s.total("raised")),
    ("回数不足（1 日の回数に届かず）", lambda s: s.total("short")),
    ("範囲の一部だけ（残りを翌日以降へ）", lambda s: s.total("partial")),
    ("欠測（毎回）", lambda s: s.total("lost")),
    ("期限切れ（期間内1回）", lambda s: s.total("expired")),
    ("最終日の持ち越し", lambda s: s.carried_at_end),
)
