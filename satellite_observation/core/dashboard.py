"""
The dashboard's figures, as the viewer's dashboard.ts and the time axis of
time.ts: features per value of an attribute (or a sum or a mean of a
number, the smallest values together as 「その他」), the distribution of a
number in 20 bins, the count over time in calendar steps, how a click on a
value or a bin changes the filter, and the filter as a subset expression.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional

#: What is summed per value of the attribute.
STATISTICS = {"count": "件数", "sum": "合計", "mean": "平均"}
#: The most values listed; the rest go into 「その他」.
MAX_VALUES = 15
BINS = 20
OTHERS = "\u0000others"
#: Attribute names left out of the automatic choices.
ID_NAMES = ("id", "fid", "objectid", "gid")


def short(n: Optional[float]) -> str:
    """A number as short text: 1,234, 12.3, 0.125."""
    if n is None or not math.isfinite(n):
        return "—"
    whole = float(n).is_integer()
    digits = 0 if abs(n) >= 100 or whole else 1 if abs(n) >= 1 else 3
    text = f"{n:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def category_value(value: Any) -> str:
    """A value as the key it is grouped by ('' for none; a date as YYYY-MM-DD; 2.0 as 2)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e21 else repr(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def number_value(value: Any) -> Optional[float]:
    """A value as a number, or None (text that reads as one counts)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        try:
            n = float(str(value).strip())
        except ValueError:
            return None
    return n if math.isfinite(n) else None


@dataclass
class Category:
    value: str
    #: Features with the value.
    count: int
    #: The statistic shown (件数, 合計 or 平均; nan for a mean of nothing).
    figure: float


def categories(rows: Iterable[tuple[str, Optional[float]]], statistic: str, chosen: Iterable[str] = ()) -> tuple[list[Category], set]:
    """(value, number) of each feature summed per value, the largest first, at most MAX_VALUES (the rest as OTHERS); and the values listed by name."""
    sums: dict[str, list] = {}
    for value, number in rows:
        s = sums.setdefault(value, [0, 0.0, 0])
        s[0] += 1
        if statistic != "count" and number is not None:
            s[1] += number
            s[2] += 1
    # The values chosen stay listed even when the other filters leave none of them.
    for v in chosen:
        if v != OTHERS:
            sums.setdefault(v, [0, 0.0, 0])

    def figure(s: list) -> float:
        return float(s[0]) if statistic == "count" else s[1] if statistic == "sum" else (s[1] / s[2] if s[2] else math.nan)

    def key(item):
        f = figure(item[1])
        return (-(0 if math.isnan(f) else f), item[0])

    entries = sorted(sums.items(), key=key)
    if len(entries) > MAX_VALUES:
        kept, rest = entries[:MAX_VALUES - 1], entries[MAX_VALUES - 1:]
        others = [sum(s[0] for _, s in rest), sum(s[1] for _, s in rest), sum(s[2] for _, s in rest)]
        entries = [*kept, (OTHERS, others)]
    listed = {v for v, _ in entries if v != OTHERS}
    return [Category(v, s[0], figure(s)) for v, s in entries], listed


def in_values(value: str, chosen: set, listed: set) -> bool:
    """Whether a feature's value is among those chosen (OTHERS: any value not listed by name)."""
    return value in chosen or (OTHERS in chosen and value not in listed)


@dataclass
class Histogram:
    low: float
    high: float
    counts: list
    width: float
    mean: float
    n: int

    def bin_range(self, i: int) -> tuple[float, float]:
        low = self.low + i * self.width
        return low, self.high if i == len(self.counts) - 1 else self.low + (i + 1) * self.width


def histogram(values: list, chosen: Optional[tuple[float, float]] = None) -> Optional[Histogram]:
    """The values in BINS equal bins over their range (and the chosen range); None for no values."""
    if not values:
        return None
    low, high = min(values), max(values)
    if chosen:
        low, high = min(low, chosen[0]), max(high, chosen[1])
    width = (high - low) / BINS if high > low else 1.0
    counts = [0] * (BINS if high > low else 1)
    for v in values:
        counts[min(len(counts) - 1, int(math.floor((v - low) / width)))] += 1
    return Histogram(low, high, counts, width, sum(values) / len(values), len(values))


def bin_chosen(chosen: Optional[tuple[float, float]], low: float, high: float) -> bool:
    return chosen is not None and chosen[0] <= low and chosen[1] >= high


def choose_value(chosen: set, value: str, several: bool) -> set:
    """The values chosen after a click on `value` (Ctrl / Shift: add or remove; else only it, or none when it was the only one)."""
    if several:
        return chosen - {value} if value in chosen else chosen | {value}
    return set() if chosen == {value} else {value}


def choose_range(chosen: Optional[tuple[float, float]], low: float, high: float, extend: bool) -> Optional[tuple[float, float]]:
    """The range chosen after a click on the bin low–high (Shift: widen the range chosen to it; again: none)."""
    if extend and chosen:
        return min(chosen[0], low), max(chosen[1], high)
    return None if chosen == (low, high) else (low, high)


def pick_group(fields: list[tuple[str, str]], values_of: Callable[[str], list], coded: Iterable[str] = ()) -> Optional[str]:
    """The attribute to group by at first: one with codes, else the one of fewest values that repeat (2 to 50, text first)."""
    texts = [(name, kind) for name, kind in fields if kind in ("string", "integer") and name.lower() not in ID_NAMES]
    coded = set(coded)
    for name, _ in texts:
        if name in coded:
            return name
    ranked = []
    for name, kind in texts:
        sample = values_of(name)
        n = len({category_value(v) for v in sample})
        if 1 < n <= 50 and n < len(sample):
            ranked.append((kind != "string", n, name))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return ranked[0][2] if ranked else None


def pick_measure(fields: list[tuple[str, str]]) -> Optional[str]:
    return next((name for name, kind in fields if kind in ("double", "integer") and name.lower() not in ID_NAMES), None)


# ---- The time axis (local calendar) ----

UNITS = ("second", "minute", "hour", "day", "week", "month", "year")
_FIXED = {"second": 1000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000, "week": 7 * 86_400_000}
STEPS = [("second", 1), ("second", 10), ("minute", 1), ("minute", 5), ("minute", 15), ("hour", 1), ("hour", 3), ("hour", 6), ("hour", 12),
         ("day", 1), ("week", 1), ("month", 1), ("month", 3), ("month", 6), ("year", 1), ("year", 2), ("year", 5), ("year", 10),
         ("year", 25), ("year", 50), ("year", 100)]


def _local(t: float) -> datetime:
    return datetime.fromtimestamp(t / 1000)


def _ms(d: datetime) -> float:
    return d.timestamp() * 1000


def add_time(t: float, unit: str, count: int) -> float:
    """`t` moved by `count` units along the local calendar (a month after 31 January is the end of February)."""
    d = _local(t)
    if unit in ("year", "month"):
        months = d.year * 12 + d.month - 1 + (count * 12 if unit == "year" else count)
        year, month = divmod(months, 12)
        day = min(d.day, calendar.monthrange(year, month + 1)[1])
        return _ms(d.replace(year=year, month=month + 1, day=day))
    if unit in ("day", "week"):
        return _ms(d + timedelta(days=count * (7 if unit == "week" else 1)))
    return t + count * _FIXED[unit]


def floor_time(t: float, unit: str, count: int = 1) -> float:
    """The start of the `count`-unit step `t` is in (years at 1 January, weeks on Mondays)."""
    d = _local(t)
    if unit == "year":
        return _ms(datetime(d.year // count * count, 1, 1))
    if unit == "month":
        m = (d.year * 12 + d.month - 1) // count * count
        return _ms(datetime(m // 12, m % 12 + 1, 1))
    if unit == "week":
        day = datetime(d.year, d.month, d.day) - timedelta(days=d.weekday())
        return _ms(day)
    if unit == "day":
        day = datetime(d.year, d.month, d.day)
        if count > 1:
            day = day.replace(day=1 + (d.day - 1) // count * count)
        return _ms(day)
    size = _FIXED[unit] * count
    offset = -d.astimezone().utcoffset().total_seconds() * 1000
    return math.floor((t - offset) / size) * size + offset


def step_length(step: tuple[str, int]) -> float:
    unit, count = step
    day = 86_400_000
    return _FIXED.get(unit, 30.44 * day if unit == "month" else 365.25 * day) * count


def step_at_least(ms: float) -> tuple[str, int]:
    return next((s for s in STEPS if step_length(s) >= ms), STEPS[-1])


def steps_between(start: float, end: float, step: tuple[str, int], limit: int = 2000) -> list:
    out = []
    t = floor_time(start, *step)
    while t <= end and len(out) < limit:
        if t >= start:
            out.append(t)
        following = add_time(t, *step)
        if following <= t:
            break
        t = following
    return out


def format_time(t: float, unit: str = "second") -> str:
    d = _local(t)
    if unit == "year":
        return str(d.year)
    if unit == "month":
        return f"{d.year}-{d.month:02d}"
    if unit in ("week", "day"):
        return d.strftime("%Y-%m-%d")
    if unit in ("hour", "minute"):
        return d.strftime("%Y-%m-%d %H:%M")
    return d.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class TimeBins:
    starts: list
    counts: list
    step: tuple
    first: float
    last: float

    @property
    def unit(self) -> str:
        return "day" if self.step[0] == "week" else self.step[0]

    def end_of(self, i: int) -> float:
        return add_time(self.starts[i], *self.step) - 1


def time_bins(times: list) -> Optional[TimeBins]:
    """The features' times (ms) counted in about 40 calendar steps; None for none."""
    if not times:
        return None
    times = sorted(times)
    first, last = times[0], times[-1]
    step = step_at_least(max(1000, (last - first) / 40))
    starts = steps_between(floor_time(first, *step), last, step, 400)
    counts = [0] * len(starts)
    j = 0
    for t in times:
        while j < len(starts) - 1 and t >= starts[j + 1]:
            j += 1
        counts[j] += 1
    return TimeBins(starts, counts, step, first, last)


# ---- The filter as a subset expression ----

def _literal(value: str, numeric: bool) -> str:
    if numeric and number_value(value) is not None:
        return value
    return "'" + value.replace("'", "''") + "'"


def filter_expression(group: Optional[str], chosen: set, listed: set, measure: Optional[str], chosen_range: Optional[tuple[float, float]],
                      numeric_group: bool = False, quote: Callable[[str], str] = lambda name: '"' + name.replace('"', '""') + '"') -> Optional[str]:
    """The values and range chosen as a WHERE clause (QGIS expressions, OGR SQL and ArcGIS take it alike); None when nothing is chosen."""
    parts = []
    if group and chosen:
        field = quote(group)
        named = sorted(v for v in chosen if v not in ("", OTHERS))
        terms = []
        if named:
            terms.append(f"{field} IN ({', '.join(_literal(v, numeric_group) for v in named)})")
        if "" in chosen:
            terms.append(f"{field} IS NULL" if numeric_group else f"({field} IS NULL OR {field} = '')")
        if OTHERS in chosen:
            # Every value not listed by name; the empty one too unless it is listed.
            kept = sorted(v for v in listed if v != "")
            terms_rest = [f"{field} NOT IN ({', '.join(_literal(v, numeric_group) for v in kept)})"] if kept else []
            if "" in listed:
                terms_rest += [f"{field} IS NOT NULL"] + ([] if numeric_group else [f"{field} <> ''"])
                terms.append("(" + " AND ".join(terms_rest) + ")")
            elif terms_rest:
                terms.append(f"({terms_rest[0]} OR {field} IS NULL)")
            else:
                terms.append("1 = 1")
        parts.append(terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")")
    if measure and chosen_range:
        field = quote(measure)
        parts.append(f"{field} >= {chosen_range[0]!r} AND {field} <= {chosen_range[1]!r}")
    return " AND ".join(parts) if parts else None
