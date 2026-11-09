"""
The day's work end to end, without Qt: what is asked of the services
(forecast places), the candidates fitted, and what is written back
(history rows, raises, follow-ups).
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
from .store import HistoryRow, day_start_ms
from .systems import ExportSystem
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

    def refit(self) -> list[Candidate]:
        """Fits again (after a change by hand)."""
        self.candidates = allocate(self.candidates, self.systems, self.sats)
        return self.candidates


def plan_day(requests: Sequence[ImagingRequest], day: date, systems: Sequence[ExportSystem], sats: Sequence[SatelliteSpec],
             clouds: dict, history: Sequence[HistoryRow] = ()) -> DayPlan:
    """The candidates of a day, gathered and fitted."""
    missed = missed_ids(history)
    cands = gather(requests, day, sats, clouds, missed)
    plan = DayPlan(day, cands, list(systems), list(sats), [r for r in history if r.request_id in missed and r.captured == "未撮像" and not r.boost_done])
    plan.refit()
    return plan


def history_of(plan: DayPlan, run: float, files: dict, run_id: Optional[str] = None,
               failures: Optional[dict] = None) -> list[HistoryRow]:
    """
    The history rows of a day written out: every candidate, taken or held
    back (`files`: system name → file name). A candidate taken for a system
    whose file was not written (`failures`: system name → why; or simply
    absent from `files`) is kept as 見送り with the reason, never as 採用:
    it was not handed to anyone, so it is neither followed up nor 完了.
    """
    run_id = run_id or str(uuid.uuid4()).upper()
    failures = failures or {}
    rows = []
    for c in plan.candidates:
        system = c.system.system if c.system else ""
        result, reason = c.result, c.reason or "・".join(c.warnings)
        if result == TAKEN and system not in files:
            result, reason = HELD, f"書き出し失敗（{failures.get(system) or 'ファイルなし'}）"
        taken = result == TAKEN
        rows.append(HistoryRow(
            run_id=run_id, run_date=run, day=plan.day.isoformat(), request_id=c.request.id,
            name=c.request.name, management_no=c.request.management_no, requester=c.request.requester, system=system,
            satellite=c.satellite.name if c.satellite else "",
            priority=str(convert_priority(c.priority, c.system.priority)) if c.system else str(c.priority),
            cloud=None if c.cloud is None else round(c.cloud, 1), scenes=c.scenes if taken else None,
            result=result, reason=reason, file=files.get(system, "") if taken else "",
            captured="未判定" if taken else "", boost_done=0))
    return rows


def ledger_results(plan: DayPlan, rows: Sequence[HistoryRow]) -> list[tuple[int, dict]]:
    """
    The ledger's 「最新の出力」 of the requests written out (object id, {role:
    value}), from their history rows: when, the target day, 採用 / 見送り, the
    system, satellite and why. A request whose ledger already holds a later
    day (that day written before this one) keeps it: the fields always tell
    the latest day handed out, so a dashboard can ask 「今日（明日）採用か」.
    """
    by_id = {r.request_id: r for r in rows}
    out = []
    for c in plan.candidates:
        req, row = c.request, by_id.get(c.request.id)
        if row is None or req.object_id is None or (req.last_day and req.last_day > row.day):
            continue
        out.append((req.object_id, {
            "lastRun": row.run_date, "lastDay": day_start_ms(row.day), "lastResult": row.result,
            "lastSystem": row.system or None, "lastSatellite": row.satellite or None, "lastReason": row.reason[:250] or None,
        }))
    return out


def not_written(plan: DayPlan, files: dict) -> set:
    """The requests taken for a system whose file was not written."""
    return {c.request.id for c in plan.candidates if c.result == TAKEN and (c.system.system if c.system else "") not in files}


def boost_changes(plan: DayPlan, skip: Optional[set] = None) -> list[tuple[int, dict]]:
    """The raises taken (object id, {boost}); those of `skip` (requests not written) are left for the next day."""
    skip = skip or set()
    return [(c.request.object_id, {"boost": c.boost_offer}) for c in plan.candidates
            if c.boost_offer is not None and c.boost_taken and c.request.object_id is not None and c.request.id not in skip]


def missed_done(plan: DayPlan, skip: Optional[set] = None) -> list[tuple[int, dict]]:
    """The 未撮像 rows whose raise has now been offered (taken or not): they are not offered again (but those of `skip` are)."""
    skip = skip or set()
    return [(r.object_id, {"boostDone": 1}) for r in plan.missed_rows if r.object_id is not None and r.request_id not in skip]


def written_summary(files: Sequence[Written]) -> str:
    """「SYS-A 12 件・SYS-C 3 件」."""
    return "・".join(f"{w.system.label} {w.count} 件" for w in files) or "出力なし"
