"""
The day's work end to end, without Qt: what is asked of the services
(forecast places), the candidates fitted, and what is written back
(history rows, raises, follow-ups).

Each system's file may be written by its own person (tsudo, 2026-11-09):
with a `system` given, only that system's file is written, and only its
requests (taken for it, or held back where it could take them) go to the
history and have their raises saved; the rows of the other systems are
left as they are. What the others wrote earlier the same day is pinned
(`DayPlan.target`, daily.Candidate.pinned), so no request is handed to two
systems and none is lost between them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from ..plan.satellites import SatelliteSpec
from ..plan.weather import CloudSeries
from .daily import HELD, TAKEN, Candidate, allocate, gather, wants_weather
from .export import Written, convert_priority
from .follow import missed_ids
from .model import ImagingRequest, occurs_on
from .store import HistoryRow
from .systems import ExportSystem
from .view import systems_of
from .weather import DAYLIGHT, daytime_cloud, places_of


def forecast_places(requests: Sequence[ImagingRequest], day: date, sats: Sequence[SatelliteSpec]) -> list[tuple]:
    """The places whose forecasts the day's optical requests need."""
    seen: dict = {}
    for req in requests:
        if occurs_on(req, day) and wants_weather(req, sats):
            for p in places_of(req):
                seen[p] = None
    return list(seen)


def clouds_of(requests: Sequence[ImagingRequest], day: date, forecasts: dict[str, CloudSeries],
              hours: Sequence[float] = DAYLIGHT) -> dict:
    """Each request's daytime cover (None where not forecast)."""
    return {r.id: daytime_cloud(r, forecasts, day, hours) for r in requests}


@dataclass
class DayPlan:
    """The day's candidates, as fitted."""
    day: date
    candidates: list = field(default_factory=list)
    systems: list = field(default_factory=list)
    sats: list = field(default_factory=list)
    #: History rows found 未撮像 and not yet raised for (their raise is offered).
    missed_rows: list = field(default_factory=list)
    #: What the day's history says was handed out already: request id → (system, satellite).
    written: dict = field(default_factory=dict)
    #: The (request id, system) pairs of those rows, to tell whether others wrote meanwhile.
    handed: frozenset = frozenset()
    #: The system whose file is written from here; None for all of them.
    target: Optional[str] = None

    def refit(self) -> list[Candidate]:
        """Fits again (after a change by hand), those written for a system other than the target pinned there."""
        for c in self.candidates:
            w = self.written.get(c.request.id)
            c.pinned = w if self.target and w and w[0] != self.target else None
        self.candidates = allocate(self.candidates, self.systems, self.sats)
        return self.candidates

    def others_handed(self) -> frozenset:
        """The (request id, system) pairs handed out earlier by the systems not written from here."""
        return frozenset(p for p in self.handed if p[1] != self.target) if self.target else frozenset()


def handed_today(history: Sequence[HistoryRow], day: date) -> list[HistoryRow]:
    """The rows of a day handed out (採用) and not yet followed up."""
    d = day.isoformat()
    return [r for r in history if r.day == d and r.result == TAKEN and r.system and r.captured in ("", "未判定")]


def plan_day(requests: Sequence[ImagingRequest], day: date, systems: Sequence[ExportSystem], sats: Sequence[SatelliteSpec],
             clouds: dict, history: Sequence[HistoryRow] = (), target: Optional[str] = None) -> DayPlan:
    """The candidates of a day, gathered and fitted (`target`: the one system written from here)."""
    missed = missed_ids(history)
    cands = gather(requests, day, sats, clouds, missed)
    plan = DayPlan(day, cands, list(systems), list(sats), [r for r in history if r.request_id in missed and r.captured == "未撮像" and not r.boost_done])
    today = handed_today(history, day)
    plan.written = {r.request_id: (r.system, r.satellite) for r in today}
    plan.handed = frozenset((r.request_id, r.system) for r in today)
    plan.target = target or None
    plan.refit()
    return plan


def of_system(c: Candidate, system: Optional[str]) -> bool:
    """Whether a candidate is written with `system`'s file: taken for it, or held back where it could go (None: every one)."""
    return system is None or system in systems_of(c)


def history_of(plan: DayPlan, run: float, files: dict, run_id: Optional[str] = None,
               failures: Optional[dict] = None, system: Optional[str] = None) -> list[HistoryRow]:
    """
    The history rows of a day written out: every candidate, taken or held
    back (`files`: system name → file name). A candidate taken for a system
    whose file was not written (`failures`: system name → why; or simply
    absent from `files`) is kept as 見送り with the reason, never as 採用:
    it was not handed to anyone, so it is neither followed up nor 完了.

    With `system`, only that system's candidates (of_system), those held
    back named with it, so that writing it again replaces only its rows.
    """
    run_id = run_id or str(uuid.uuid4()).upper()
    failures = failures or {}
    rows = []
    for c in plan.candidates:
        if not of_system(c, system):
            continue
        # Held back: named with the system written, when only one is (its rows are replaced when it is written again).
        name = c.system.system if c.system else (system or "")
        result, reason = c.result, c.reason or "・".join(c.warnings)
        if result == TAKEN and name not in files:
            result, reason = HELD, f"書き出し失敗（{failures.get(name) or 'ファイルなし'}）"
        taken = result == TAKEN
        rows.append(HistoryRow(
            run_id=run_id, run_date=run, day=plan.day.isoformat(), request_id=c.request.id, system=name,
            satellite=c.satellite.name if c.satellite else "",
            priority=str(convert_priority(c.priority, c.system.priority)) if c.system else str(c.priority),
            cloud=None if c.cloud is None else round(c.cloud, 1), scenes=c.scenes if taken else None,
            result=result, reason=reason, file=files.get(name, "") if taken else "",
            captured="未判定" if taken else "", boost_done=0))
    return rows


def not_written(plan: DayPlan, files: dict, system: Optional[str] = None) -> set:
    """The requests taken for a system whose file was not written (of `system` only, when given)."""
    return {c.request.id for c in plan.candidates
            if c.result == TAKEN and of_system(c, system) and (c.system.system if c.system else "") not in files}


def boost_changes(plan: DayPlan, skip: Optional[set] = None, system: Optional[str] = None) -> list[tuple[int, dict]]:
    """
    The raises taken (object id, {boost}); those of `skip` (requests not
    written) are left for the next day, those not of `system` (when given)
    for whoever writes theirs.
    """
    skip = skip or set()
    return [(c.request.object_id, {"boost": c.boost_offer}) for c in plan.candidates
            if c.boost_offer is not None and c.boost_taken and c.request.object_id is not None and c.request.id not in skip
            and of_system(c, system)]


def missed_done(plan: DayPlan, skip: Optional[set] = None, system: Optional[str] = None) -> list[tuple[int, dict]]:
    """The 未撮像 rows whose raise has now been offered (taken or not): they are not offered again (but those of `skip` are,
    and those of requests not of `system`, when given)."""
    skip = set(skip or ())
    if system is not None:
        skip |= {c.request.id for c in plan.candidates if not of_system(c, system)}
    return [(r.object_id, {"boostDone": 1}) for r in plan.missed_rows if r.object_id is not None and r.request_id not in skip]


def written_summary(files: Sequence[Written]) -> str:
    """「SYS-A 12 件・SYS-C 3 件」."""
    return "・".join(f"{w.system.label} {w.count} 件" for w in files) or "出力なし"
