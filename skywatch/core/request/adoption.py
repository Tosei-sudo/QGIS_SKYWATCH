"""
The satellite systems' own answers to what was handed out to them (採否:
採用 / 不採用, 不明 until they answer), and the plans they made without
being asked (要求外, e.g. their routine tasks), all kept in the history
table (TBL_REQUEST_HISTORY): the day's plan — what will be taken — is its
rows of that target day (the 「撮像予定」 dock, a dashboard on ArcGIS).

tsudo, 2026-11-09: 「依頼した結果、その日の計画を見たい。採用されたかどうかを知りたい。
出力されるだけじゃだめ」; answers by a result file per system and by hand;
要求外 plans too; in the history, not the ledger (「台帳は日々の計画立案の素材でしかない」).

A file's rows are matched to the requests by their key (id, 管理番号 or
識別番号, `resultFile.keySource`). A row of a request handed to that system
that day answers its history row(s). A row of a request not handed to it
that day, or of no request at all, is 要求外 (planned by the system itself).
A request handed out and not in the file is `missing` (不採用, or left 不明).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .model import ImagingRequest
from .result_file import ADOPTED, REJECTED, WAITING, Answer, ResultFile
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

    @property
    def unrequested(self) -> bool:
        return not self.rows


@dataclass
class Reading:
    """A system's file read against a target day."""
    system: str
    day: str
    #: The file's name (kept as ADOPTION_SOURCE).
    source: str
    matches: list = field(default_factory=list)
    #: Rows handed to the system that the file leaves out, and what they become (不採用, or 不明: unchanged).
    missing: list = field(default_factory=list)
    missing_as: str = REJECTED
    problems: list = field(default_factory=list)

    def tally(self) -> dict:
        """{採用: n, 不採用: n, 要求外: n, ファイルになし: n}."""
        out = {ADOPTED: 0, REJECTED: 0, UNREQUESTED: 0, NOT_IN_FILE: len(self.missing)}
        for m in self.matches:
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


def handed_rows(day_rows: Sequence[HistoryRow], system: str) -> list[HistoryRow]:
    """The day's rows of requests handed out to `system` (its answers are awaited or given)."""
    return [r for r in day_rows if r.system == system and r.result == "採用" and r.request_id]


def match_answers(answers: Sequence[Answer], day_rows: Sequence[HistoryRow], requests: Sequence[ImagingRequest],
                  system: str, day: str, spec: ResultFile, source: str) -> Reading:
    """A file's rows matched to the day's rows of `system` (`day_rows`: load_day) and the ledger."""
    reading = Reading(system, day, source, missing_as=spec.missing)
    by_key: dict[str, ImagingRequest] = {}
    doubled: set = set()
    for req in requests:
        k = key_of(req, spec.key_source)
        if not k:
            continue
        if k in by_key and by_key[k].id != req.id:
            doubled.add(k)
        by_key.setdefault(k, req)
    handed = handed_rows(day_rows, system)
    answered: set = set()
    for a in answers:
        req = by_key.get(a.key) if a.key else None
        if a.key in doubled:
            reading.problems.append(f"{a.line} 行目: {a.key} は複数の依頼にあるため「{req.name}」とした")
        rows = [r for r in handed if req is not None and r.request_id == req.id]
        if not rows and a.answer == REJECTED:
            # Not ours, or not handed to it this day: nothing to answer, and not in its plan.
            what = f"「{req.name}」" if req is not None else (a.key or "（キーなし）")
            reading.problems.append(f"{a.line} 行目: 不採用の {what} はこの日このシステムに出力していないため読み飛ばし")
            continue
        if req is not None and req.id in answered:
            reading.problems.append(f"{a.line} 行目: 「{req.name}」が重複（後の行を使う）")
            reading.matches = [m for m in reading.matches if not (m.request is not None and m.request.id == req.id)]
        if req is not None:
            answered.add(req.id)
        reading.matches.append(Match(a, req, rows))
    reading.missing = [r for r in handed if r.request_id not in answered]
    return reading


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
    """The file's 要求外 rows (planned by the system itself), as history rows of the day."""
    run_id = run_id or str(uuid.uuid4()).upper()
    out = []
    for m in reading.matches:
        if not m.unrequested:
            continue
        a, req = m.answer, m.request
        out.append(HistoryRow(
            run_id=run_id, run_date=now, day=reading.day, request_id=req.id if req else "", system=reading.system,
            satellite=a.satellite, result=UNREQUESTED, reason=NOT_HANDED if req else "", captured="",
            name=(a.name or (req.name if req else "") or a.external_id or a.key), management_no=req.management_no if req else "",
            requester=req.requester if req else "", adoption=ADOPTED, adoption_reason=a.reason, planned_at=a.planned_at,
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
