"""
Which of the day's candidates the 「撮像計画ファイル出力」 table shows: by
system, by satellite series, by result and by words (tsudo, 2026-11-09:
the files of each system are written by different people).

A candidate belongs to the system it was taken for; one held back belongs
to every system with a satellite that can take it (so the person of that
system can still take it in by hand). Its series are those of the
satellite it was given, or else of the satellites that can take it (in the
chosen system), or else the series the request names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .daily import HELD, TAKEN, Candidate
from .systems import ExportSystem

#: The system filter's value for candidates no system can take (該当システムなし).
NO_SYSTEM = ""
#: The result filter's value for candidates offered a raise (繰り上げ候補).
BOOST = "繰り上げ候補"
RESULTS = (TAKEN, HELD, BOOST)


@dataclass
class CandidateFilter:
    """What the table shows: None (or "") for no condition on that item."""
    #: A system name, NO_SYSTEM, or None for all.
    system: Optional[str] = None
    series: Optional[str] = None
    #: TAKEN, HELD or BOOST.
    result: Optional[str] = None
    #: Words all found in the name, request id, 管理番号, 識別番号 or satellite (any case).
    text: str = ""


def systems_of(c: Candidate) -> list[str]:
    """The systems a candidate belongs to: the one it was taken for, or those that could take it."""
    if c.result == TAKEN and c.system is not None:
        return [c.system.system]
    return list(c.able)


def series_of(c: Candidate, system: Optional[str] = None) -> list[str]:
    """A candidate's satellite series (within `system` when given)."""
    if c.result == TAKEN and c.satellite is not None and (system is None or c.system is not None and c.system.system == system):
        return [c.satellite.series] if c.satellite.series else []
    sats = c.able.get(system, ()) if system else [s for v in c.able.values() for s in v]
    out = list(dict.fromkeys(s.series for s in sats if s.series))
    return out or list(c.request.series)


def matches(c: Candidate, f: CandidateFilter) -> bool:
    """Whether the table shows a candidate."""
    if f.system is not None:
        if f.system == NO_SYSTEM:
            if c.system is not None or c.able:
                return False
        elif f.system not in systems_of(c):
            return False
    if f.series and f.series.lower() not in {s.lower() for s in series_of(c, f.system or None)}:
        return False
    if f.result == BOOST:
        if c.boost_offer is None:
            return False
    elif f.result and c.result != f.result:
        return False
    words = f.text.lower().split()
    if words:
        r = c.request
        hay = " ".join((r.name, r.id, r.management_no, r.identification_no, c.satellite.name if c.satellite else "")).lower()
        if not all(w in hay for w in words):
            return False
    return True


def shown(cands: Sequence[Candidate], f: CandidateFilter) -> list[Candidate]:
    """The candidates the table shows, in their order."""
    return [c for c in cands if matches(c, f)]


def series_choices(cands: Sequence[Candidate], systems: Sequence[ExportSystem] = ()) -> list[str]:
    """The series to choose from: those of the day's candidates, then the systems' own (`series`), once each."""
    seen: dict = {}
    for c in cands:
        for s in series_of(c):
            seen.setdefault(s.lower(), s)
    for system in systems:
        for s in system.series:
            seen.setdefault(s.lower(), s)
    return sorted(seen.values(), key=str.lower)
