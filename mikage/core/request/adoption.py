"""
The satellite systems' own answers to what was handed out to them (採否:
採用 / 不採用, 不明 until they answer), and the plans they made without
being asked (要求外, e.g. their routine tasks), all kept in the history
table (TBL_REQUEST_HISTORY): the day's plan — what will be taken — is its
rows of that target day (the 「撮像予定」 dock, a dashboard on ArcGIS).

tsudo, 2026-11-09: 「依頼した結果、その日の計画を見たい。採用されたかどうかを知りたい。
出力されるだけじゃだめ」; answers by a result file per system and by hand;
要求外 plans too; in the history, not the ledger (「台帳は日々の計画立案の素材でしかない」).

A file's rows are matched to the requests by their keys (id, 管理番号,
識別番号: the columns of those targets, tried in that order; numbers match
without their leading zeros, `000123` = `123`). Rows for another system (by
the `system` column) are skipped. Each row is for the day it says, whatever
day the dock shows (tsudo, 2026-11-20: 「別の日の選択でも正しくインポートしてほしい」):
its 対象日 (`day` column), else the JST day of its 撮像予定 (`plannedAt`);
a row saying neither is for the file's day (file_day: the day most of its
rows say, else the day most of the requests it names were handed out on,
round the day shown, else the day shown). A row of a request handed to that
system that day answers its history row(s); one whose 撮像予定 falls on a day
the request was not handed out on answers the file's day's row of it (planned
off its day). A row of a request not handed to it, or of no request at all, is
要求外 (planned by the system itself) when it is 採用, put on the day of its
撮像予定 (「計画外のやつは、選択している日付じゃなくて、その撮像予定の日付のところに出してほしい」).
A request handed out on a day the file answers for (the file's day, the days
it states, the days whose rows it answers) and not in the file is `missing`
(不採用, or left 不明).

A request taken several times a day (1 日の回数) has a history row per
time: a file row naming the request answers them all, one naming a time
(its `occurrenceId`, `R1-2`: the id, a hyphen and the time) answers that
time only (and the requests put into its shot).

A file row giving the 外部連携ID its order file wrote (`linkId`,
core/request/link_id.py) answers the very row of that id (and the requests
put into its shot), tried before the request's keys (tsudo, 2026-11-12:
「日付の蓋然性とそのIDから特定の依頼と照合できるように」). As an id is short
and not unique for ever, it is looked for among the system's rows of the
row's day and of the `linkDays` days before it (and the day after: a day cut
otherwise), the nearest day first; the same id on two requests of that day
is not used (the request's keys decide, when the file has them). So an
answer about a request handed out the day before (planned late by the
system) finds that day's row.

Read again, a file replaces the 要求外 rows a file put in before on the days
it answers for, and those of the same plan (its system's id and 撮像予定)
on any other day (put on the day shown by a reading before 2026-11-20).
"""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from .model import ImagingRequest, jst_day
from .result_file import ADOPTED, KEY_SOURCES, LINK_ID, REJECTED, WAITING, Answer, ResultFile
from .store import MANUAL, UNREQUESTED, HistoryRow

#: Why a request handed out is 不採用 when the file leaves it out.
NOT_IN_FILE = "結果ファイルになし"
#: Why a 要求外 row is one although it names a request.
NOT_HANDED = "この日・このシステムには出力していない依頼"


@dataclass
class Match:
    """One row of a file, matched to the day's rows."""
    answer: Answer
    #: The request it names (None: not one of ours).
    request: Optional[ImagingRequest] = None
    #: The day's history rows of that request handed to the system ([]: 要求外).
    rows: list = field(default_factory=list)
    #: The time it names of a request taken several times a day (`R1-2`); None for every time.
    time: Optional[int] = None
    #: The target day of the rows it answers (the row's day, or the one its 外部連携ID was found on); for 要求外, the
    #: day it is put on (its 撮像予定's).
    day: str = ""
    #: The shot it names of an area shared out (分担, by its 外部連携ID); None for every shot.
    part: Optional[int] = None

    @property
    def unrequested(self) -> bool:
        return not self.rows


@dataclass
class Reading:
    """A system's file read against the history (the days its rows are for)."""
    system: str
    #: The file's day (file_day): the day most of its rows are for; rows saying no day are for it.
    day: str
    #: The file's name (kept as ADOPTION_SOURCE).
    source: str
    matches: list = field(default_factory=list)
    #: Rows handed to the system that the file leaves out, and what they become (不採用, or 不明: unchanged).
    missing: list = field(default_factory=list)
    missing_as: str = REJECTED
    problems: list = field(default_factory=list)
    #: The day the dock showed when it was read ("" when not told).
    shown: str = ""
    #: The days the file answers for (its day, the days it states and those whose rows it answers): their rows
    #: handed out and not in the file are `missing`, their 要求外 from a file before are replaced.
    days: list = field(default_factory=list)
    #: The 要求外 rows a file put in before that this reading replaces (deleted when it is saved).
    replaced: list = field(default_factory=list)

    def placed(self) -> list[str]:
        """The days the file is for: those it answers for and those its 要求外 go on, in order."""
        return sorted(set(self.days) | {m.day for m in self.matches if m.unrequested and m.day})

    def touched(self) -> list[str]:
        """Every day the reading writes on (answers, an earlier day's row a 外部連携ID names too, 要求外, missing), in order."""
        days = set(self.placed()) | {m.day for m in self.matches if m.day} | {r.day for r in self.missing}
        return sorted(days)

    def tally(self, day: Optional[str] = None) -> dict:
        """{採用: n, 不採用: n, 不明: n, 要求外: n, ファイルになし: n}, of `day` only when given."""
        missing = [r for r in self.missing if day is None or r.day == day]
        out = {ADOPTED: 0, REJECTED: 0, WAITING: 0, UNREQUESTED: 0, NOT_IN_FILE: len(missing)}
        for m in self.matches:
            if day is not None and m.day != day:
                continue
            if m.unrequested:
                out[UNREQUESTED] += 1
            else:
                out[m.answer.answer] += 1
        return out


def key_of(req: ImagingRequest, source: str) -> str:
    """A request's key as a file writes it."""
    if source == "managementNo":
        return req.management_no.strip()
    if source == "identificationNo":
        return req.identification_no.strip()
    return req.id.strip()


def same_key(key: str) -> str:
    """A key as compared: trimmed, upper case, without braces; a number without its leading zeros (000123 → 123)."""
    t = key.strip().strip("{}").strip().upper()
    if re.fullmatch(r"[+-]?\d+", t):
        return str(int(t))
    return t


def handed_rows(day_rows: Sequence[HistoryRow], system: str) -> list[HistoryRow]:
    """The day's rows of requests handed out to `system` (its answers are awaited or given)."""
    return [r for r in day_rows if r.system == system and r.result == "採用" and r.request_id]


def link_window(day: str, link_days: int) -> tuple[str, str]:
    """The target days (first, last) whose rows a result file of `day` looks its 外部連携ID up in."""
    d = date.fromisoformat(day)
    return (d - timedelta(days=max(0, link_days))).isoformat(), (d + timedelta(days=1)).isoformat()


def _days_before(day: str, row_day: str) -> Optional[int]:
    """How many days `row_day` is before `day` (−1: the day after); None when one is not a day."""
    try:
        return (date.fromisoformat(day) - date.fromisoformat(row_day[:10])).days
    except ValueError:
        return None


def nearest_linked(rows: Sequence[HistoryRow], day: str, link_days: int) -> list[HistoryRow]:
    """
    Rows sharing one 外部連携ID: those of the nearest target day to `day` in
    the window (the day itself, then the days before it, then the day after).
    """
    placed = []
    for r in rows:
        n = _days_before(day, r.day)
        if n is not None and -1 <= n <= link_days:
            placed.append(((abs(n), n < 0), r))
    if not placed:
        return []
    best = min(p for p, _ in placed)
    return [r for p, r in placed if p == best]


def planned_day(a: Answer) -> str:
    """The JST day (`2026-11-10`) of a file row's 撮像予定 ("" without one)."""
    if a.planned_at is None or not math.isfinite(a.planned_at):
        return ""
    try:
        return jst_day(a.planned_at).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def own_day(a: Answer) -> str:
    """The day a file row says it is for: its 対象日 (`day`), else the day of its 撮像予定 ("": neither)."""
    return a.day or planned_day(a)


def _system_names(system: str, label: str = "") -> set:
    return {same_key(system)} | ({same_key(label)} if label else set())


def _best_day(counts: dict, shown: str) -> str:
    """The day counted most; of two, the nearer to the day shown (the day itself, then before it, then after)."""
    def rank(d: str) -> tuple:
        n = _days_before(shown, d)
        return -counts[d], abs(n) if n is not None else 1 << 30, n is not None and n < 0, d
    return min(counts, key=rank)


def read_window(answers: Sequence[Answer], day: str, link_days: int, system: str = "", label: str = "") -> tuple[str, str]:
    """
    The target days (first, last) whose rows of the system a result file is
    matched against (store.load_system_rows): the days its rows say and the
    day shown (`day`), from `link_days` before them (a 外部連携ID's earlier
    row) to `link_days` after (at least the day after; a file without dates
    looks that far round the day shown for the day of its requests).
    """
    names = _system_names(system, label) if system else None
    days = {day}
    for a in answers:
        d = own_day(a)
        if d and (names is None or not a.system or same_key(a.system) in names):
            days.add(d)
    known = []
    for d in days:
        try:
            known.append(date.fromisoformat(d[:10]))
        except ValueError:
            continue
    if not known:
        return link_window(day, link_days)
    span = max(0, link_days)
    return (min(known) - timedelta(days=span)).isoformat(), (max(known) + timedelta(days=max(1, span))).isoformat()


def _file_day(mine: list, by_request: dict, linked: dict, shown: str, link_days: int) -> str:
    """
    The day a file is about (its rows saying no day are for it): the day
    most of its rows say; without any, the day most of the requests (and
    外部連携ID) it names were handed out on, within `link_days` (at least 1)
    of the day shown; else the day shown.
    """
    said = Counter(d for d in (own_day(a) for a, _req, _twice, _time in mine) if d)
    if said:
        return _best_day(said, shown)
    near = max(1, link_days)
    named: Counter = Counter()
    for a, req, _twice, _time in mine:
        days = set()
        link = a.keys.get(LINK_ID, "")
        if link:
            days |= {r.day for r in linked.get(same_key(link), ())}
        if req is not None:
            days |= {r.day for r in by_request.get(req.id, ())}
        for d in days:
            n = _days_before(shown, d)
            if n is not None and abs(n) <= near:
                named[d] += 1
    return _best_day(named, shown) if named else shown


def _plan_key(external_id: str, planned_at: Optional[float]) -> Optional[tuple]:
    """A 要求外 plan as it is told apart: the system's id of it and its 撮像予定 (None without one)."""
    if planned_at is None or not math.isfinite(planned_at):
        return None
    return same_key(external_id or ""), round(planned_at)


def match_answers(answers: Sequence[Answer], day_rows: Sequence[HistoryRow], requests: Sequence[ImagingRequest],
                  system: str, day: str, spec: ResultFile, source: str, label: str = "",
                  nearby: Sequence[HistoryRow] = ()) -> Reading:
    """
    A file's rows matched to the history rows of `system` and the ledger:
    `day_rows` and `nearby` (store.load_system_rows over read_window: the
    rows handed to the system, and its 要求外, of the days round those the
    file says; rows given in both are used once). Each row is for the day it
    says (own_day), else the file's day (file_day), whatever day is shown
    (`day`).
    """
    missing = spec.missing_value or (WAITING if spec.says_result() or any(a.stated for a in answers) else REJECTED)
    names = _system_names(system, label)
    pool, seen = [], set()
    for r in list(day_rows) + list(nearby):
        k = ("oid", r.object_id) if r.object_id is not None else ("row", id(r))
        if k not in seen:
            seen.add(k)
            pool.append(r)
    handed = handed_rows(pool, system)
    on_day: dict = {}
    by_request: dict = {}
    linked: dict = {}
    for r in handed:
        on_day.setdefault(r.day, []).append(r)
        by_request.setdefault(r.request_id, []).append(r)
        if r.link_id:
            linked.setdefault(same_key(r.link_id), []).append(r)
    by_key: dict = {}
    doubled: dict = {}
    for k_source in KEY_SOURCES:
        if not any(k_source in a.keys for a in answers):
            continue
        found, twice = by_key.setdefault(k_source, {}), doubled.setdefault(k_source, set())
        for req in requests:
            k = same_key(key_of(req, k_source))
            if not k:
                continue
            if k in found and found[k].id != req.id:
                twice.add(k)
            found.setdefault(k, req)
    ledger = {q.id: q for q in requests}
    # The file's rows of this system, each with the request its keys name.
    mine, other_systems = [], set()
    for a in answers:
        if a.system and same_key(a.system) not in names:
            other_systems.add(a.system)
            continue
        req, twice, time = None, False, None
        for k_source in KEY_SOURCES:
            k = same_key(a.keys.get(k_source, ""))
            if k and k in by_key.get(k_source, {}):
                req, twice = by_key[k_source][k], k in doubled[k_source]
                break
        if req is None:
            req, time = _occurrence_of(a, by_key.get("id", {}))
        mine.append((a, req, twice, time))
    file_day = _file_day(mine, by_request, linked, day, spec.link_days)
    reading = Reading(system, file_day, source, missing_as=missing, shown=day)
    covered = {file_day}
    by_link_days: dict = {}
    off_day = 0
    unknown_links = 0
    answered: set = set()

    def place(a: Answer, req: Optional[ImagingRequest], time: Optional[int], at: str, own: list, part: Optional[int],
              late: bool, anchor: str) -> None:
        """Puts a file row on the rows it answers (`own` and those put into its shot), or as 要求外, or skips it."""
        if a.day and at == anchor:
            # The day it states is one the file answers for (not when it answers another day's row).
            covered.add(a.day)
        pool_at = on_day.get(at, ())
        # A request put into another's shot (まとめ) was in that one's row: its answer is theirs too.
        shots = {r.shot_id for r in own if r.shot_id}
        rows = own + [r for r in pool_at if req is not None and r.merged_into == req.id and r.request_id != req.id
                      and (time is None or r.shot_id in shots)]
        if not rows and a.answer != ADOPTED:
            # Not ours, or not handed to it that day: nothing to answer, and not in its plan.
            what = f"「{req.name}」" if req is not None else (a.key or "（キーなし）")
            when = "この日" if anchor == file_day else f" {anchor} に"
            reading.problems.append(f"{a.line} 行目: {a.answer}の {what} は{when}このシステムに出力していないため読み飛ばし")
            return
        if not rows:
            # 要求外: on the day of its 撮像予定 (not the day shown).
            at = planned_day(a) or anchor
        elif not late:
            covered.add(at)
        if req is not None and (req.id, time, at, part) in answered:
            reading.problems.append(f"{a.line} 行目: 「{req.name}」が重複（後の行を使う）")
            reading.matches = [m for m in reading.matches
                               if not (m.request is not None and (m.request.id, m.time, m.day, m.part) == (req.id, time, at, part))]
        if req is not None:
            answered.add((req.id, time, at, part))
        reading.matches.append(Match(a, req, rows, time, at, part))

    def of_request(day_: str, req: ImagingRequest, time: Optional[int]) -> list:
        return [r for r in on_day.get(day_, ()) if r.request_id == req.id and (time is None or r.occurrence == time)]

    # Rows naming a request not handed out on their day, tried on the file's day once the others are placed.
    deferred = []
    for a, req, twice, time in mine:
        anchor = own_day(a) or file_day
        at, own, part, late = anchor, None, None, False
        link = a.keys.get(LINK_ID, "")
        if link:
            found = nearest_linked(linked.get(same_key(link), ()), anchor, spec.link_days)
            if req is not None and len({r.request_id for r in found}) > 1:
                found = [r for r in found if r.request_id == req.id] or found
            if len({r.request_id for r in found}) > 1:
                reading.problems.append(f"{a.line} 行目: 外部連携ID {link} が {found[0].day} の複数の依頼にあるため依頼の番号で照合")
            elif found:
                lead = found[0]
                if req is not None and req.id != lead.request_id:
                    reading.problems.append(f"{a.line} 行目: 外部連携ID {link} は「{lead.name or lead.request_id}」の行"
                                            f"（依頼の番号の「{req.name or req.id}」でなく ID の行とした）")
                req = ledger.get(lead.request_id) or ImagingRequest(lead.request_id, name=lead.name)
                twice, time, at, own = False, lead.occurrence, lead.day, found
                # One shot of an area shared out (分担) answers for itself.
                part = lead.part if lead.part > 0 else None
                if at != anchor:
                    by_link_days[at] = by_link_days.get(at, 0) + 1
                    late = True
            elif req is None:
                unknown_links += 1
        if twice:
            reading.problems.append(f"{a.line} 行目: {a.key} は複数の依頼にあるため「{req.name}」とした")
        if own is None:
            own = of_request(anchor, req, time) if req is not None else []
            if not own and req is not None and anchor != file_day and of_request(file_day, req, time):
                deferred.append((a, req, time, anchor))
                continue
        place(a, req, time, at, own, part, late, anchor)
    for a, req, time, anchor in deferred:
        # Planned off its day (a request handed out on the file's day, its 撮像予定 or 対象日 another day): the file's day's
        # row, unless another row of the file answers that one (then it is a plan of the system's own on its day).
        if any(m.request is not None and m.request.id == req.id and m.day == file_day and m.rows for m in reading.matches):
            place(a, req, time, anchor, [], None, False, anchor)
            continue
        off_day += 1
        place(a, req, time, file_day, of_request(file_day, req, time), None, False, anchor)
    if other_systems:
        reading.problems.append(f"ほかのシステム（{'・'.join(sorted(other_systems))}）の行を読み飛ばし")
    for d, n in sorted(by_link_days.items()):
        reading.problems.append(f"外部連携IDで {d} の出力の行に照合した {n} 行（その日の採否として記録）")
    if off_day:
        reading.problems.append(f"ほかの日の {off_day} 行（その日に出力していない依頼）を {file_day} の出力の行に照合（{file_day} の採否として記録）")
    if unknown_links:
        first, last = link_window(file_day, spec.link_days)
        reading.problems.append(f"外部連携IDが {first}〜{last} の出力履歴にない {unknown_links} 行（依頼の番号もなし）")
    # A request put into another's shot answered by its own row too: its own answer counts for it.
    named = {(rid, d) for rid, _, d, _ in answered}
    for m in reading.matches:
        if m.request is not None:
            m.rows = [r for r in m.rows if r.request_id == m.request.id or (r.request_id, r.day) not in named]
    # A time answered on its own row wins over the row naming the whole request.
    timed = {(m.request.id, r.occurrence, r.day) for m in reading.matches if m.request is not None and m.time is not None
             and m.part is None for r in m.rows if r.request_id == m.request.id}
    for m in reading.matches:
        if m.request is not None and m.time is None:
            m.rows = [r for r in m.rows if r.request_id != m.request.id or (r.request_id, r.occurrence, r.day) not in timed]
    # A shot of an area shared out answered on its own row (its 外部連携ID) wins over the rows naming its time or request.
    shot_rows = {id(r) for m in reading.matches if m.part is not None for r in m.rows}
    for m in reading.matches:
        if m.part is None:
            m.rows = [r for r in m.rows if id(r) not in shot_rows]
    answered_rows = {id(r) for m in reading.matches for r in m.rows}
    reading.days = sorted(covered)
    reading.missing = [r for d in reading.days for r in on_day.get(d, ()) if id(r) not in answered_rows]
    # The 要求外 a file put in before: those of the days it answers for, and of the same plan on another day.
    plans = {k for k in (_plan_key(m.answer.external_id or m.answer.key, m.answer.planned_at) for m in reading.matches if m.unrequested) if k}
    reading.replaced = [r for r in pool if r.result == UNREQUESTED and r.system == system and r.adoption_source != MANUAL
                        and r.object_id is not None and (r.day in covered or _plan_key(r.external_id, r.planned_at) in plans)]
    return reading


def _occurrence_of(a: Answer, by_id: dict) -> tuple:
    """(request, time) of a row naming one time of a request taken several times a day (`R1-2`); (None, None) otherwise."""
    found = re.fullmatch(r"(.+)-(\d+)", (a.keys.get("id") or "").strip())
    if not found:
        return None, None
    req = by_id.get(same_key(found.group(1)))
    if req is None or req.times[1] <= 1 or int(found.group(2)) < 1:
        return None, None
    return req, int(found.group(2))


def answer_changes(reading: Reading) -> list[tuple[int, dict]]:
    """The history rows answered (object id, {role: value}): the file's answers, then those it leaves out."""
    out = []
    for m in reading.matches:
        for row in m.rows:
            if row.object_id is None:
                continue
            values = {"adoption": m.answer.answer, "adoptionReason": m.answer.reason or None,
                      "plannedAt": m.answer.planned_at, "externalId": m.answer.external_id or None,
                      "adoptionSource": reading.source}
            if m.answer.satellite:
                values["satellite"] = m.answer.satellite
            out.append((row.object_id, values))
    if reading.missing_as == REJECTED:
        for row in reading.missing:
            if row.object_id is not None:
                out.append((row.object_id, {"adoption": REJECTED, "adoptionReason": NOT_IN_FILE, "plannedAt": None,
                                            "externalId": None, "adoptionSource": reading.source}))
    return out


def unrequested_rows(reading: Reading, now: float, run_id: Optional[str] = None) -> list[HistoryRow]:
    """The file's 要求外 rows (planned by the system itself), as history rows of the day of each one's 撮像予定."""
    run_id = run_id or str(uuid.uuid4()).upper()
    out = []
    for m in reading.matches:
        if not m.unrequested:
            continue
        a, req = m.answer, m.request
        out.append(HistoryRow(
            run_id=run_id, run_date=now, day=m.day or reading.day, request_id=req.id if req else "", system=reading.system,
            satellite=a.satellite, result=UNREQUESTED, reason=NOT_HANDED if req else "", captured="",
            name=(a.name or (req.name if req else "") or a.external_id or a.key),
            management_no=req.management_no if req else a.keys.get("managementNo", ""),
            requester=req.requester if req else "", group=req.group if req else "", adoption=ADOPTED, adoption_reason=a.reason, planned_at=a.planned_at,
            external_id=a.external_id or a.key, adoption_source=reading.source,
            rings=a.rings or ([[list(p[:2]) for p in r] for r in req.rings] if req else [])))
    return out


def manual_changes(rows: Sequence[HistoryRow], answer: str, reason: str = "",
                   planned_at: Optional[float] = None) -> list[tuple[int, dict]]:
    """
    Answers given by hand to rows handed out (`answer`: 採用 / 不採用, or
    不明 to take an answer back); 要求外 and held-back rows are left as they are.
    """
    out = []
    for row in rows:
        if row.object_id is None or row.result != "採用" or not row.request_id:
            continue
        if answer == WAITING:
            out.append((row.object_id, {"adoption": WAITING, "adoptionReason": None, "plannedAt": None, "adoptionSource": None}))
        else:
            out.append((row.object_id, {"adoption": answer, "adoptionReason": reason or None,
                                        "plannedAt": planned_at if planned_at is not None else row.planned_at,
                                        "adoptionSource": MANUAL}))
    return out


def manual_unrequested(day: str, system: str, name: str, now: float, rings: list, satellite: str = "",
                       planned_at: Optional[float] = None, note: str = "", external_id: str = "") -> HistoryRow:
    """A 要求外 plan added by hand (the system's routine task told by phone, …)."""
    return HistoryRow(
        run_id=str(uuid.uuid4()).upper(), run_date=now, day=day, request_id="", system=system, satellite=satellite,
        result=UNREQUESTED, captured="", name=name, adoption=ADOPTED, adoption_reason=note, planned_at=planned_at,
        external_id=external_id, adoption_source=MANUAL, rings=rings)
