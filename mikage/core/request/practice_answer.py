"""
The answer a satellite system would send back (採否の結果ファイル), made for
練習モード's チュートリアル, 「返ってきた採否を入れる」 (ui/tutorial_courses.py): there is no
system to answer while practising, so the file is made here from the rows
handed out on the day, in the form the system's `resultFile` reads
(core/request/result_file.py): CSV (or other separated text) and JSON by
`columns`. Every row handed out is taken (採用) but the last one, left out so
that it stays 不明 (or 不採用, as the file's `missing` says) for the step that
puts in an answer heard by phone. A form only a JSON Schema describes is not
made (None).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from .result_file import ResultColumn, ResultFile
from .store import PLANNED, HistoryRow

#: When the answer says each row is to be taken: the day at this hour (the system's time), a few minutes apart.
PLANNED_HOUR = (10, 30)


def handed_out(rows: Sequence[HistoryRow], system: str) -> list[HistoryRow]:
    """The rows of `system` a file was written for (taken, with a row of their own), answered or not."""
    return [r for r in rows if r.system == system and r.result == PLANNED and not r.merged_into and r.request_id]


def _planned(row: HistoryRow, i: int) -> datetime:
    day = datetime.strptime(row.day, "%Y-%m-%d")
    return day + timedelta(hours=PLANNED_HOUR[0], minutes=PLANNED_HOUR[1] + 7 * i)


def _time_text(t: datetime, fmt: str) -> Any:
    if fmt == "unix":
        return int(t.timestamp())
    if fmt == "unixms":
        return int(t.timestamp() * 1000)
    if fmt == "iso-date":
        return t.strftime("%Y-%m-%d")
    if "%" in fmt:
        return t.strftime(fmt)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def _taken_word(col: ResultColumn, spec: ResultFile) -> str:
    """What the file writes for 採用 (the column's map, or the system's `values`, turned round)."""
    for written, value in col.map.items():
        if value == PLANNED and written not in ("", "*"):
            return written
    for written, value in (spec.reverse.get("result") or {}).items():
        if value == PLANNED:
            return written
    return PLANNED


def _values(cols: Sequence[ResultColumn], spec: ResultFile, row: HistoryRow, i: int) -> list[Any]:
    """The row's value of each column."""
    out: list[Any] = []
    seen: dict[str, int] = {}
    for col in cols:
        target = col.target or ""
        nth = seen.get(target, 0)
        seen[target] = nth + 1
        twice = sum(1 for c in cols if c.target == target) > 1
        value: Any = ""
        if col.only:
            value = col.only[0]
        elif target == "id":
            value = row.request_id
        elif target == "linkId":
            value = row.link_id
        elif target == "result":
            value = _taken_word(col, spec)
        elif target == "plannedAt":
            # (the first column saying a format is the one read with: result_file._joined)
            fmt = next((c.format for c in cols if c.target == target and c.format), "") if twice else col.format
            text = _time_text(_planned(row, i), fmt)
            # A date and a time in two columns: the first part in the first, the rest in the second.
            if twice and isinstance(text, str) and " " in text:
                first, rest = text.split(" ", 1)
                text = first if nth == 0 else rest
            value = text
        elif target == "satellite":
            value = row.satellite
        elif target == "name":
            value = row.name
        elif target == "externalId":
            value = f"PRACTICE-{i + 1:03d}"
        out.append(value)
    return out


def _set(record: dict, path: str, value: Any) -> None:
    keys = [k for k in path.split(".") if k] or [path]
    for k in keys[:-1]:
        record = record.setdefault(k, {})
    record[keys[-1]] = value


def answer_bytes(spec: ResultFile, rows: Sequence[HistoryRow]) -> Optional[bytes]:
    """The file answering `rows` (one system's, handed out): all taken but the last; None for a form not made here."""
    if not rows:
        return None
    cols = [c for c in spec.columns if not c.fixed]
    if not cols or not any(c.target in ("id", "linkId") for c in cols):
        return None
    answered = list(rows[:-1]) if len(rows) > 1 else list(rows)
    if spec.format == "json":
        if spec.schema is not None:
            return None
        records = []
        for i, row in enumerate(answered):
            record: dict = {}
            for col, value in zip(cols, _values(cols, spec, row, i)):
                if col.key:
                    _set(record, col.key, value)
            records.append(record)
        value: Any = records
        if spec.records:
            value = {}
            _set(value, spec.records, records)
        return json.dumps(value, ensure_ascii=False, indent=1).encode(spec.encoding or "utf-8")
    if spec.format not in ("csv", "text", "tsv", "txt"):
        return None
    if any(c.position for c in cols):
        width = max(c.position or 0 for c in cols)
        width = max(width, len(cols))
    else:
        width = len(cols)
    placed: list[Optional[ResultColumn]] = [None] * width
    rest = iter(range(width))
    for col in cols:
        if col.position and placed[col.position - 1] is None:
            placed[col.position - 1] = col
    for col in cols:
        if not col.position:
            at = next(i for i in rest if placed[i] is None)
            placed[at] = col
    text = io.StringIO()
    writer = csv.writer(text, delimiter=spec.delimiter or ",", lineterminator="\r\n")
    for _ in range(spec.skip_lines):
        writer.writerow([])
    if spec.header:
        writer.writerow([c.key if c is not None else "" for c in placed])
    for i, row in enumerate(answered):
        values = dict(zip((id(c) for c in cols), _values(cols, spec, row, i)))
        writer.writerow([values.get(id(c), "") if c is not None else "" for c in placed])
    return text.getvalue().encode(spec.encoding or "utf-8", errors="replace")
