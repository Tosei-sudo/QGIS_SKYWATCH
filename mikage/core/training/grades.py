"""
判読の練習の成績 (練習モードの段階 3 の 3): the organisation's results table
(TBL_TRAINING_RESULT, every person's tries) as the instructors read it — each
person's score on each exercise of a course (their best, or their latest),
how far through the course they are, and the classes most often taken for
others over everyone's tries (取り違え: from each try's detail), with what to
do about them. Pure Python: the rows as tables.read_all_results gives them.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from .exercises import EXAM, kind_text
from .tables import RESULT_FIELDS, record_of

#: The periods offered (days; None: all).
PERIODS = (("この 30 日", 30), ("この 90 日", 90), ("この 1 年", 365), ("すべて", None))


@dataclass(frozen=True)
class Column:
    """An exercise of the grid."""
    key: str
    title: str
    course: str
    order: int = 0
    mode: str = ""
    pass_score: Optional[float] = None

    @property
    def exam(self) -> bool:
        return self.mode == EXAM


@dataclass
class Cell:
    """A person's tries of an exercise, as the grid shows them."""
    score: float
    passed: bool
    tries: int
    seconds: int
    finished: str


@dataclass
class Grades:
    """The grid: the people (by name), the course's exercises, each person's cell of each, and their progress."""
    people: list
    columns: list
    cells: dict = field(default_factory=dict)

    def cell(self, person: str, key: str) -> Optional[Cell]:
        return self.cells.get((person, key))

    def progress(self, person: str) -> tuple[int, int]:
        """(exercises passed, exercises of the course)."""
        passed = sum(1 for c in self.columns if (cell := self.cell(person, c.key)) is not None and cell.passed)
        return passed, len(self.columns)

    def passed_by(self, key: str) -> tuple[int, int]:
        """(people who passed an exercise, people who tried it)."""
        tried = [self.cell(p, key) for p in self.people if self.cell(p, key) is not None]
        return sum(1 for c in tried if c.passed), len(tried)


def read_all_results(client: Any, results_url: str, token: Optional[str], cancelled=None, where: str = "1=1") -> list[dict]:
    """Every try of the results table (each person's), with its detail (in a task)."""
    rows = client.query_all(results_url, where, token, geometry=False, cancelled=cancelled, limit=500_000,
                            out_fields=",".join(RESULT_FIELDS))
    return [r for r in (record_of(row) for row in rows) if r is not None]


def courses_of(columns: Iterable[Column], results: Iterable[dict]) -> list[str]:
    """The courses: those of the exercises (in their order), then those only the results name."""
    return list(dict.fromkeys([*(c.course for c in columns), *(r.get("course") or "" for r in results)]))


def _since(period_days: Optional[int], now: datetime) -> str:
    return (now - timedelta(days=period_days)).isoformat(timespec="seconds") if period_days else ""


def in_period(results: Iterable[dict], period_days: Optional[int], now: Optional[datetime] = None) -> list[dict]:
    since = _since(period_days, now or datetime.now())
    return [r for r in results if not since or (r.get("finished") or "") >= since]


def grades(results: Iterable[dict], columns: Sequence[Column], course: str, best: bool = True) -> Grades:
    """
    The grid of a course: its exercises (`columns` of the course, then those only the results name, by their
    title), each person's best try of each (`best`; else their latest) and how many tries.
    """
    mine = [r for r in results if (r.get("course") or "") == course and r.get("user")]
    cols = [c for c in columns if c.course == course]
    known = {c.key for c in cols}
    extra: dict[str, Column] = {}
    for r in mine:
        key = r.get("exercise") or ""
        if key and key not in known and key not in extra:
            extra[key] = Column(key, r.get("title") or key, course, 10_000, r.get("mode") or "")
    cols = [*sorted(cols, key=lambda c: (c.order, c.title)), *sorted(extra.values(), key=lambda c: c.title)]
    tries: dict = defaultdict(list)
    for r in mine:
        tries[(r["user"], r.get("exercise") or "")].append(r)
    cells = {}
    for k, rows in tries.items():
        rows = sorted(rows, key=lambda r: r.get("finished") or "")
        chosen = max(rows, key=lambda r: (float(r.get("score") or 0), r.get("finished") or "")) if best else rows[-1]
        cells[k] = Cell(float(chosen.get("score") or 0), chosen.get("passed") is True, len(rows), int(chosen.get("seconds") or 0),
                        chosen.get("finished") or "")
    people = sorted({u for u, _k in tries}, key=lambda u: u.lower())
    return Grades(people, cols, cells)


# ---- 取り違え ------------------------------------------------------------------------------------------------

def confusion(results: Iterable[dict]) -> tuple[list[str], Counter, dict]:
    """
    The classes said for each true class over the tries' details (the answers found: the right ones too): the
    classes (true ones first, by how often), (true, said) → times, and (true, said) → the people who did.
    """
    counts: Counter = Counter()
    people: dict = defaultdict(set)
    for r in results:
        for row in r.get("detail") or []:
            if not isinstance(row, dict) or row.get("no", 0) == 0 or row.get("state") in ("見落とし", "見落とし（加点）"):
                continue
            truth, given = kind_text(row.get("kind") or ""), kind_text(row.get("given") or "")
            if truth and given:
                counts[(truth, given)] += 1
                if r.get("user"):
                    people[(truth, given)].add(r["user"])
    truths = Counter()
    for (t, _g), n in counts.items():
        truths[t] += n
    order = [t for t, _n in sorted(truths.items(), key=lambda p: (-p[1], p[0]))]
    given = sorted({g for _t, g in counts} - set(order))
    return [*order, *given], counts, dict(people)


def taken_for_others(counts: Counter, top: int = 5) -> list[tuple[str, str, int]]:
    """The pairs of a true class and another said for it (not the same, nor one below it), the most first."""
    pairs = [(t, g, n) for (t, g), n in counts.items() if t != g and not g.startswith(t + " > ")]
    return sorted(pairs, key=lambda p: (-p[2], p[0], p[1]))[:top]


def advice(results: Sequence[dict], table: Grades, counts: Counter, people: dict) -> list[str]:
    """What the instructor may do: the pairs most taken for each other, the exercises few pass, the misses."""
    lines = []
    for t, g, n in taken_for_others(counts, 3):
        right = counts.get((t, t), 0)
        share = n / (n + right) * 100 if n + right else 0
        lines.append(f"「{t}」を「{g}」と答えたのが {n} 回（{len(people.get((t, g), ()))} 人、{share:.0f} %）。"
                     f"見分け方を解説に書くか、この 2 つを見分ける問題を追加")
    hard = []
    for c in table.columns:
        passed, tried = table.passed_by(c.key)
        if tried >= 2 and passed / tried <= 0.5:
            hard.append((passed / tried, c, passed, tried))
    for _share, c, passed, tried in sorted(hard, key=lambda h: (h[0], h[1].title))[:2]:
        lines.append(f"「{c.title}」は合格が {passed} / {tried} 人。指示文や合格点、正解の解説を見直し")
    missed = Counter()
    for r in results:
        for row in r.get("detail") or []:
            if isinstance(row, dict) and row.get("state") == "見落とし" and row.get("kind"):
                missed[kind_text(row["kind"])] += 1
    if missed:
        kind, n = missed.most_common(1)[0]
        if n >= 3:
            lines.append(f"見落としが多いのは「{kind}」（{n} 回）。探し方の問題（見つける）を追加")
    return lines


# ---- CSV ---------------------------------------------------------------------------------------------------------

def grades_csv(table: Grades) -> str:
    """The grid as CSV: a row per person, each exercise's score (空: not tried) and whether passed, and the progress."""
    out = io.StringIO()
    w = csv.writer(out)
    head = ["解いた人"]
    for c in table.columns:
        head += [f"{c.title}（点）", f"{c.title}（合否）", f"{c.title}（回数）"]
    head += ["合格した問題", "問題の数"]
    w.writerow(head)
    for p in table.people:
        row: list[Any] = [p]
        for c in table.columns:
            cell = table.cell(p, c.key)
            row += ["", "", 0] if cell is None else [f"{cell.score:g}", "合格" if cell.passed else "不合格", cell.tries]
        passed, total = table.progress(p)
        row += [passed, total]
        w.writerow(row)
    return out.getvalue()
