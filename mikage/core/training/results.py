"""
判読の練習's results: each try submitted, kept on this computer (the practice
folder's `exercise_results.json`, per QGIS profile; kept when the practice
data is made again) and, when the organisation has a results table, sent to it
as one row (TBL_TRAINING_RESULT: the one thing the practice writes to the
production). The same record is the row's values and the file's entry.

The kinds taken for others (取り違え) are read from the tries' details:
which class each answer truly was, and which the person said.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from typing import Iterable, Optional

from .exercises import EXAM, ITEM_NAMES, Outcome, kind_text


def _round(v: Optional[float], digits: int = 1) -> Optional[float]:
    return None if v is None else round(v, digits)


def result_record(outcome: Outcome, started: datetime, finished: datetime, attempt: int, user: str, hints: int = 0) -> dict:
    """A try as kept and sent (the results table's columns: core/training/tables.py RESULT_FIELDS)."""
    e = outcome.exercise
    detail = []
    for item in outcome.items:
        a, m = item.answer, item.mark
        row = {"no": a.number, "state": item.state, "required": a.required, "kind": kind_text(a.kind), "given": kind_text(m.kind) if m else "",
               "earned": round(item.earned, 1), "possible": round(item.possible, 1)}
        if a.activity:
            row.update(activity=a.activity, givenActivity=m.activity if m else "")
        if a.change:
            row.update(change=a.change, givenChange=m.change if m else "")
        for key, truth, given in (("length", a.length, m.length if m else None), ("width", a.width, m.width if m else None),
                                  ("heading", a.heading, m.heading if m else None)):
            if truth is not None:
                row[key] = _round(truth)
                row["given" + key[0].upper() + key[1:]] = _round(given)
        if item.distance is not None:
            row["distance"] = round(item.distance)
        detail.append(row)
    for m in outcome.extras:
        detail.append({"no": 0, "state": "誤り", "kind": "", "given": kind_text(m.kind), "earned": 0, "possible": 0})
    kind = outcome.kind_score()
    return {
        "exercise": e.key, "exerciseId": e.id, "source": e.source, "title": e.title, "course": e.course, "type": e.type, "mode": e.mode,
        "attempt": attempt, "user": user, "started": started.isoformat(timespec="seconds"), "finished": finished.isoformat(timespec="seconds"),
        "seconds": max(0, int(round((finished - started).total_seconds()))), "score": outcome.score, "passed": outcome.passed,
        "found": outcome.found, "missed": outcome.missed, "extra": len(outcome.extras), "kindScore": None if kind is None else round(kind * 100, 1),
        "hints": hints, "parts": {ITEM_NAMES[k]: list(v) for k, v in outcome.parts.items()}, "penalty": outcome.penalty, "detail": detail,
    }


def read_results(path: str) -> list:
    """The tries kept on this computer, the oldest first ([] when none or not readable)."""
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in value if isinstance(r, dict) and isinstance(r.get("exercise"), str)] if isinstance(value, list) else []


def add_result(path: str, record: dict) -> list:
    """A try kept (after those before); all of them."""
    results = [*read_results(path), record]
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    os.replace(temporary, path)
    return results


def tries_of(results: Iterable[dict], key: str) -> list:
    return [r for r in results if r.get("exercise") == key]


def best_of(results: Iterable[dict], key: str) -> Optional[float]:
    scores = [r.get("score") for r in tries_of(results, key) if isinstance(r.get("score"), (int, float))]
    return max(scores) if scores else None


def exam_taken(results: Iterable[dict], key: str) -> bool:
    """Whether an exam was taken already (one try only)."""
    return any(r.get("mode") == EXAM for r in tries_of(results, key))


def confusions(results: Iterable[dict]) -> Counter:
    """(the true class, the class said) → how many times, of the answers found in the tries (the right ones too)."""
    out: Counter = Counter()
    for r in results:
        for row in r.get("detail") or []:
            if not isinstance(row, dict) or row.get("no", 0) == 0:
                continue
            truth, given = row.get("kind") or "", row.get("given") or ""
            if truth and given and row.get("state") != "見落とし":
                out[(truth, given)] += 1
    return out


def mistaken(results: Iterable[dict], top: int = 5) -> list:
    """The classes most often taken for another: [(true, said, times)], the most first."""
    pairs = [(t, g, n) for (t, g), n in confusions(results).items() if t != g and not g.startswith(t + " > ")]
    return sorted(pairs, key=lambda p: (-p[2], p[0], p[1]))[:top]
