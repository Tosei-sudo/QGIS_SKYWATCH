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

import dataclasses
import math
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from ..plan.satellites import SatelliteSpec
from ..plan.weather import CloudSeries
from .daily import HELD, TAKEN, Candidate, allocate, asks_weather, carries_over, gather, occurrence_key
from .export import Written, convert_priority, row_rings
from .follow import missed_ids
from .link_id import link_ids
from .model import ImagingRequest, occurs_on
from .passes import Passes
from .quota import QuotaBook
from .route import system_of_satellite
from .store import HistoryRow, ShotRow
from .systems import ExportSystem
from .view import systems_of
from .weather import DAYLIGHT, Clouds, cloud_when, daytime_cloud, places_of


def forecast_places(requests: Sequence[ImagingRequest], day: date, sats: Sequence[SatelliteSpec],
                    missed: Sequence[str] = ()) -> list[tuple]:
    """
    The places whose forecasts the day's optical requests need: those of the
    day, and those missed (`missed`: the ids found 未撮像, follow.missed_ids)
    carried over to it (daily.gather takes them too, so their weather counts
    as any other's). Those of either kind (任意) that optical satellites can
    take too are asked for (their optical passes are weighed, daily.mixed_kinds).
    """
    seen: dict = {}
    missed = set(missed)
    for req in requests:
        if (occurs_on(req, day) or req.id in missed and carries_over(req, day)) and asks_weather(req, sats):
            for p in places_of(req):
                seen[p] = None
    return list(seen)


def orbiting(systems: Sequence[ExportSystem], sats: Sequence[SatelliteSpec]) -> set:
    """The indices of the satellites fitted on their passes: those of a system fitted by the orbits (`orbit`, not false)."""
    out = set()
    for i, sat in enumerate(sats):
        own = system_of_satellite(sat, systems)
        if own is not None and own.orbit:
            out.add(i)
    return out


def radar_of(sats: Sequence[SatelliteSpec]) -> frozenset:
    """The indices of the SAR satellites (modes): the clouds never matter to their passes."""
    return frozenset(i for i, s in enumerate(sats) if s.sar)


def clouds_of(requests: Sequence[ImagingRequest], day: date, forecasts: dict[str, CloudSeries],
              hours: Sequence[float] = DAYLIGHT, passes: Optional[Passes] = None,
              sats: Optional[set] = None, radar: frozenset = frozenset()) -> Clouds:
    """
    Each request's cover of the day (None where not forecast). With the
    orbits (`passes`; `sats`: the indices of the satellites fitted on them,
    None for all known), each of an optical request's passes that day is
    told its own (`cloud`, at its moment) and the request's is that of its
    least cloudy pass; without passes (or forecasts at their moments), the
    daylight hours' mean. The passes of `radar` (SAR satellites, radar_of)
    are told none and do not count (a request of either kind: its least
    cloudy optical pass).
    """
    out = Clouds()
    for r in requests:
        timed = []
        if passes is not None:
            for op in passes.of(r, day):
                if sats is not None and op.satellite not in sats:
                    continue
                if op.satellite in radar:
                    op.cloud = None
                    continue
                op.cloud = cloud_when(r, forecasts, op.time) if forecasts else None
                if op.cloud is not None:
                    timed.append(op.cloud)
        if timed:
            out[r.id] = min(timed)
            out.timed.add(r.id)
        else:
            out[r.id] = daytime_cloud(r, forecasts, day, hours)
    return out


@dataclass
class DayPlan:
    """The day's candidates, as fitted."""
    day: date
    candidates: list = field(default_factory=list)
    systems: list = field(default_factory=list)
    sats: list = field(default_factory=list)
    #: History rows found 未撮像 and not yet raised for (their raise is offered).
    missed_rows: list = field(default_factory=list)
    #: What the day's history says was handed out already: request id (with its time, occurrence_key) → (system, satellite).
    written: dict = field(default_factory=dict)
    #: The (request id, system) pairs of those rows, to tell whether others wrote meanwhile.
    handed: frozenset = frozenset()
    #: The system whose file is written from here; None for all of them.
    target: Optional[str] = None
    #: The satellites' passes over the day's requests (worked out once; None: fitted by counting).
    passes: Optional[Passes] = None
    #: What the yearly quotas have left on the day (年間撮像上限, quota.QuotaData.book); None: none is kept.
    quotas: Optional[QuotaBook] = None

    def refit(self) -> list[Candidate]:
        """Fits again (after a change by hand), those written for a system other than the target pinned there."""
        for c in self.candidates:
            w = self.written.get(c.key)
            c.pinned = w if self.target and w and w[0] != self.target else None
        self.candidates = allocate(self.candidates, self.systems, self.sats, self.passes, self.day, self.quotas)
        return self.candidates

    def others_handed(self) -> frozenset:
        """The (request id, system) pairs handed out earlier by the systems not written from here."""
        return frozenset(p for p in self.handed if p[1] != self.target) if self.target else frozenset()


def handed_today(history: Sequence[HistoryRow], day: date) -> list[HistoryRow]:
    """The rows of a day handed out (採用) and not yet followed up."""
    d = day.isoformat()
    return [r for r in history if r.day == d and r.result == TAKEN and r.system and r.captured in ("", "未判定")]


def plan_day(requests: Sequence[ImagingRequest], day: date, systems: Sequence[ExportSystem], sats: Sequence[SatelliteSpec],
             clouds: dict, history: Sequence[HistoryRow] = (), target: Optional[str] = None,
             passes: Optional[Passes] = None, quotas: Optional[QuotaBook] = None, covered: Optional[dict] = None) -> DayPlan:
    """
    The candidates of a day, gathered and fitted (`target`: the one system
    written from here; `passes`: the orbits'; `quotas`: what the yearly
    quotas have left; `covered`: what of the 期間内1回 areas is taken or
    handed out on earlier days, covered.read_covered).
    """
    missed = missed_ids(history)
    cands = gather(requests, day, sats, clouds, missed, covered)
    plan = DayPlan(day, cands, list(systems), list(sats), [r for r in history if r.request_id in missed and r.captured == "未撮像" and not r.boost_done],
                   passes=passes, quotas=quotas)
    today = handed_today(history, day)
    plan.written = {occurrence_key(r.request_id, r.occurrence, r.part): (r.system, r.satellite) for r in today}
    plan.handed = frozenset((r.request_id, r.system) for r in today)
    plan.target = target or None
    plan.refit()
    return plan


def of_system(c: Candidate, system: Optional[str]) -> bool:
    """Whether a candidate is written with `system`'s file: taken for it, or held back where it could go (None: every one)."""
    return system is None or system in systems_of(c)


def file_of(files: dict, system: str, key: str) -> Optional[str]:
    """
    The file a request (`key`: its id, with its time from the second,
    Candidate.key) was written in, None when not written: `files` maps a
    system to its file's name, or (split by `maxPerFile`, export.files_of)
    to {key: file name}.
    """
    f = files.get(system)
    return f.get(key) if isinstance(f, dict) else f


def new_run_id() -> str:
    """The id of one writing out (its history rows and shots share it)."""
    return str(uuid.uuid4()).upper()


def _file_rows(plan: DayPlan, files: dict, system: Optional[str]) -> list[Candidate]:
    """The rows of the files written (each a shot, or the shots of a big area), in order."""
    return [c for c in plan.candidates
            if c.result == TAKEN and not c.member and of_system(c, system) and c.system is not None
            and file_of(files, c.system.system, c.key) is not None]


def shot_ids_of(plan: DayPlan, files: dict, run_id: str, system: Optional[str] = None) -> dict:
    """request id (with its time, Candidate.key) → the id of the shot that takes it (the row's and those put into its shot)."""
    out: dict = {}
    for n, c in enumerate(_file_rows(plan, files, system), 1):
        for m in (c.together or [c]):
            out[m.key] = f"{run_id}-{n}"
    return out


def shots_of(plan: DayPlan, run: float, files: dict, run_id: str, system: Optional[str] = None) -> list[ShotRow]:
    """
    The shots of the files written, one a row of a file (tsudo, 2026-11-11:
    「マージした結果、どの範囲になったかも見たい」): the polygon the row is
    written with for requests put together (the box along the track around
    them, Shot.area_ring), otherwise the outline of the scenes taken (each
    shot's, for an area too big for one); the pass, and the requests in it.

    A request fitted without the orbits has one too (tsudo: 「個の画像に関して
    管理する情報はショットのほうがいい」): its area, no pass. Each names the
    sensor and mode it is taken with, what its scenes count against
    (年間撮像上限), and starts 未判定.
    """
    out = []
    for n, c in enumerate(_file_rows(plan, files, system), 1):
        members = c.together or [c]
        sat = c.satellite
        row = ShotRow(
            shot_id=f"{run_id}-{n}", run_id=run_id, run_date=run, day=plan.day.isoformat(), system=c.system.system,
            satellite=sat.name if sat else "", scenes=c.scenes, shots=max(1, len(c.shots)),
            coverage=c.gain if c.part else c.coverage,
            request_id=c.request.id, request_ids=[m.request.id for m in members], names=[m.request.name or m.request.id for m in members],
            file=file_of(files, c.system.system, c.key) or "", sensor=(sat.sensor or "") if sat else "", mode=(sat.mode or "") if sat else "",
            rings=[s.area_ring() for s in c.shots] if c.shots else [[list(p[:2]) for p in r] for r in c.request.rings])
        if c.shots:
            first = c.shots[0]
            look = first.look
            row.pass_time, row.off_nadir, row.incidence = first.middle, round(look["offNadir"], 2), round(look["incidence"], 2)
            row.orbit_dir, row.look_side = "昇交" if first.ascending else "降交", first.side
        out.append(row)
    return out


def history_of(plan: DayPlan, run: float, files: dict, run_id: Optional[str] = None,
               failures: Optional[dict] = None, system: Optional[str] = None) -> list[HistoryRow]:
    """
    The history rows of a day written out: every candidate, taken or held
    back (`files`: system name → file name, see file_of). A candidate taken
    for a system whose file was not written (`failures`: system name → why;
    or simply absent from `files`) is kept as 見送り with the reason, never
    as 採用: it was not handed to anyone, so it is neither followed up nor 完了.

    With `system`, only that system's candidates (of_system), those held
    back named with it, so that writing it again replaces only its rows.

    A request put into another's shot (まとめ) keeps its own row, its own
    area (the schedule shows requests as asked), with the request whose row
    in the file carries it (`merged_into`) and no scenes of its own. Each
    row placed on the orbits names its shot (`shot_id`, shots_of with the
    same `run_id`).

    A request taken several times a day has a row per time (`occurrence`);
    a time past the n it needs that found no room has none (it was only
    tried in the room left). An area too big for one shot has a row per
    shot (`part` 1 …, 分担), its area the part that shot takes (as its file
    row); a shot left out by hand has none (the time's first row tells).

    Each row handed out has its 外部連携ID (`link_id`, the one its file
    writes as `linkId`, link_id.link_ids); a row held back has none.
    """
    run_id = run_id or new_run_id()
    failures = failures or {}
    shot_ids = shot_ids_of(plan, files, run_id, system)
    day = plan.day.isoformat()
    links: dict = {}
    rows = []
    for c in plan.candidates:
        if not of_system(c, system) or c.extra and c.result != TAKEN or c.main is not None and c.result != TAKEN:
            continue
        # Held back: named with the system written, when only one is (its rows are replaced when it is written again).
        name = c.system.system if c.system else (system or "")
        result, reason = c.result, c.reason or "・".join(c.warnings)
        file = file_of(files, name, c.key)
        if result == TAKEN and file is None:
            result, reason = HELD, f"書き出し失敗（{failures.get(name) or 'ファイルなし'}）"
        taken = result == TAKEN
        if taken and name not in links:
            links[name] = link_ids(plan.candidates, day, c.system)
        rows.append(HistoryRow(
            run_id=run_id, run_date=run, day=day, request_id=c.request.id,
            name=c.request.name, management_no=c.request.management_no, requester=c.request.requester, group=c.request.group,
            system=name,
            satellite=c.satellite.name if c.satellite else "",
            priority=str(convert_priority(c.priority, c.system.priority)) if c.system else str(c.priority),
            cloud=None if c.forecast is None else round(c.forecast, 1), scenes=c.scenes if taken else None,
            merged_into=c.lead.request.id if taken and c.member else "",
            shot_id=shot_ids.get(c.key, "") if taken else "", occurrence=c.occurrence, part=c.part if taken else 0,
            link_id=links[name].get(c.key, "") if taken else "",
            result=result, reason=reason, file=(file or "") if taken else "",
            captured="未判定" if taken else "", boost_done=0,
            # Handed out: the system's answer is awaited (core/request/adoption.py). The area, for maps of the day's plan.
            adoption="不明" if taken else "",
            rings=row_rings(c, c.system.merge_margin) if taken and c.part and c.system else [[list(p[:2]) for p in r] for r in c.request.rings]))
    return rows


#: The systems' answers (core/request/adoption.py) that writing a day again keeps (不明 is no answer).
ANSWERS = ("採用", "不採用")


@dataclass
class Rewrite:
    """A day written again over its earlier rows (store.replaced_where)."""
    #: The rows to save: the new ones, those handed to a system that had answered with its answer.
    rows: list = field(default_factory=list)
    #: The object ids of the earlier rows to delete.
    gone: list = field(default_factory=list)
    #: How many new rows took an earlier answer, and how many earlier answered rows are kept as they were.
    carried: int = 0
    kept: int = 0


def answered(row: HistoryRow) -> bool:
    """A row handed out that its system answered (from its result file or by hand)."""
    return row.result == TAKEN and row.adoption in ANSWERS


def keep_answers(earlier: Sequence[HistoryRow], rows: Sequence[HistoryRow]) -> Rewrite:
    """
    「置き換えて書き出し」 without losing the systems' answers (tsudo,
    2026-11-09): a new row handed to the system that answered for the same
    request takes that answer (採否, its reason, the planned time, the
    system's id and where it came from; the latest when there are several);
    an answered row whose request is not handed to that system this time is
    kept as it was (the system has it in its plan, or said no). The rest of
    `earlier` is replaced.
    """
    out = Rewrite()
    answers: dict = {}
    for r in sorted(earlier, key=lambda r: r.run_date if math.isfinite(r.run_date) else 0):
        if answered(r):
            answers[(r.request_id, r.occurrence, r.part, r.system)] = r
    taken: set = set()
    for row in rows:
        old = answers.get((row.request_id, row.occurrence, row.part, row.system)) if row.result == TAKEN else None
        if old is not None:
            row = dataclasses.replace(row, adoption=old.adoption, adoption_reason=old.adoption_reason, planned_at=old.planned_at,
                                      external_id=old.external_id, adoption_source=old.adoption_source)
            taken.add((row.request_id, row.occurrence, row.part, row.system))
            out.carried += 1
        out.rows.append(row)
    for r in earlier:
        if r.object_id is None:
            continue
        if answered(r) and (r.request_id, r.occurrence, r.part, r.system) not in taken:
            out.kept += 1
        else:
            out.gone.append(r.object_id)
    return out


def not_written(plan: DayPlan, files: dict, system: Optional[str] = None) -> set:
    """The requests taken for a system whose file was not written (of `system` only, when given)."""
    return {c.request.id for c in plan.candidates
            if c.result == TAKEN and of_system(c, system) and file_of(files, c.system.system if c.system else "", c.key) is None}


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
    """「SYS-A 12 件・SYS-C 3 件」, a system split into files 「SYS-B 250 件（3 ファイル）」."""
    by: dict = {}
    for w in files:
        n, k = by.get(w.system.system, (w.system.label, 0, 0))[1:]
        by[w.system.system] = (w.system.label, n + w.count, k + 1)
    return "・".join(f"{label} {n} 件" + (f"（{k} ファイル）" if k > 1 else "") for label, n, k in by.values()) or "出力なし"
