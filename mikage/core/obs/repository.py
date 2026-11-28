"""
観測記録の分担と通報記録のデータ窓口 (docs/architecture.md): the reads and writes of the assignments
table (分担) and of the alert log (通報記録), each with its table (or the alert settings), the client and
its token bound once.

A screen makes one where a step starts (on its own thread: the token as it is then) and uses it in
the step's task. Each method is the function of the same name with the three bound (the SQL, the
TBL_ names and how times are written stay in those).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..esri import EsriClient
from ..sources import EsriSource
from .alerts import (
    AlertFormat, AlertItem, AlertSettings, LogChanges, LogRow, Moment, load_log, poll_log, remove_log, reported_ids, write_log, write_log_again,
)
from .assignments import Assignment, assignments_of_images, load_assignments, release, take, update


@dataclass(frozen=True)
class AssignmentRepository:
    """分担 (who looks at which image): read, taken, changed and given back."""

    client: EsriClient
    source: EsriSource
    token: Optional[str]

    def load_assignments(self, where: str = "", done_since: Optional[float] = None) -> list[Assignment]:
        return load_assignments(self.client, self.source, self.token, where, done_since)

    def take(self, image_id: str, user: str, now: int, image_name: str = "", image_time: Optional[int] = None, note: str = "") -> Assignment:
        return take(self.client, self.source, image_id, user, now, image_name, image_time, note, self.token)

    def update(self, x: Assignment, before: Assignment) -> Assignment:
        return update(self.client, self.source, x, before, self.token)

    def release(self, x: Assignment) -> None:
        return release(self.client, self.source, x, self.token)

    def assignments_of_images(self, image_ids: Iterable[str], chunk: int = 200) -> dict[str, list[Assignment]]:
        return assignments_of_images(self.client, self.source, image_ids, self.token, chunk)


@dataclass(frozen=True)
class AlertLogRepository:
    """通報記録 (the alert log): what was reported, written, read, removed and polled."""

    client: EsriClient
    settings: AlertSettings
    token: Optional[str]

    def reported_ids(self, ids: Optional[Iterable[str]] = None) -> set[str]:
        return reported_ids(self.client, self.settings, self.token, ids)

    def write_log(self, report: AlertFormat, items: Sequence[AlertItem], lines: Sequence[str], at: Moment,
                  batch: Optional[str] = None) -> int:
        return write_log(self.client, self.settings, report, items, lines, at, self.token, batch)

    def write_log_again(self, report: AlertFormat, items: Sequence[AlertItem], lines: Sequence[str], at: Moment, batch: str) -> int:
        return write_log_again(self.client, self.settings, report, items, lines, at, batch, self.token)

    def load_log(self, start: Optional[float] = None, end: Optional[float] = None, limit: int = 20_000) -> list[LogRow]:
        return load_log(self.client, self.settings, start, end, self.token, limit)

    def remove_log(self, rows: Sequence[LogRow]) -> int:
        return remove_log(self.client, self.settings, rows, self.token)

    def poll_log(self, known: Iterable[int], since: Optional[float] = None, limit: int = 2000) -> LogChanges:
        return poll_log(self.client, self.settings, known, since, self.token, limit)
