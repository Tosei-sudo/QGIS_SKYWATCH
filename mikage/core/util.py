"""Small helpers shared by the core modules (no Qt / QGIS)."""
from __future__ import annotations

import math
import re
import unicodedata
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

_NAME = re.compile(r"^[\w.]+$", re.ASCII)
_URL = re.compile(r"^(https?:)?//|^\.{0,2}/")
_NAIVE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")


def is_record(value: Any) -> bool:
    """A JSON object (not an array, not null)."""
    return isinstance(value, dict)


def is_number(value: Any) -> bool:
    """A finite JSON number (not a boolean)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_name(value: Any) -> bool:
    """An attribute name: ASCII letters, digits, `_` and `.`."""
    return isinstance(value, str) and bool(_NAME.match(value))


def is_url(value: Any) -> bool:
    """An http(s) URL, protocol-relative or a relative path."""
    return isinstance(value, str) and bool(_URL.search(value))


def text_of(value: Any) -> str:
    """A value as trimmed text ('' for null)."""
    return "" if value is None else js_string(value).strip()


def js_string(value: Any) -> str:
    """`String(value)` as JavaScript writes it (true, 3, 1.5)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def words(value: Any, unique: bool = False) -> list[str] | None:
    """The non-empty strings of a list, trimmed; None when it is not a list."""
    if not isinstance(value, list):
        return None
    out = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if unique:
        out = list(dict.fromkeys(out))
    return out


def layer_url(url: str) -> str:
    """A layer URL without its query and trailing slashes."""
    return url.split("?", 1)[0].rstrip("/")


def time_of(value: Any) -> float:
    """
    Milliseconds since the epoch of a date value (a number of ms, or ISO
    text); NaN when it is not one. `2026-10-01 01:23:45` without a zone is
    UTC, as ArcGIS shows it.
    """
    if is_number(value):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return math.nan
    text = value.strip()
    if _NAIVE_TIME.match(text):
        text = text.replace(" ", "T", 1) + "Z"
    return parse_iso(text)


def parse_iso(text: str) -> float:
    """ms of an ISO 8601 text (`Z` or an offset; a date alone is UTC); NaN when it is not one."""
    t = text.strip()
    if t.endswith("Z") or t.endswith("z"):
        t = t[:-1] + "+00:00"
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            d = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        else:
            d = datetime.fromisoformat(t)
            if d.tzinfo is None:
                # As JavaScript: a time without a zone is local time.
                d = d.astimezone()
    except ValueError:
        return math.nan
    return d.timestamp() * 1000.0


def ms_of(value: Any) -> int | None:
    """ms of a date value, or None."""
    t = time_of(value)
    return None if math.isnan(t) else int(round(t))


def iso_text(ms: float) -> str:
    """`2026-10-01T01:23:45.000Z` of ms since the epoch (as JavaScript's toISOString)."""
    d = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def utc_text(ms: float) -> str:
    """`2026-10-01 01:23:45` (UTC)."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_id() -> str:
    """A new UUID."""
    return str(uuid.uuid4())


def unique(items: Iterable[Any]) -> list[Any]:
    """Items in their order, without repeats."""
    return list(dict.fromkeys(items))


def formula_safe(text: str) -> str:
    """
    A CSV cell's text that a spreadsheet won't run as a formula: one that
    starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a quote
    first (a number such as -12.5 stays as it is).
    """
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not re.fullmatch(r"[-+]?\d[\d.,]*(?:[eE][-+]?\d+)?", text):
        return "'" + text
    return text


def use_legacy_names(fields: dict[str, str], names: set, defaults: dict[str, str], legacy: dict[str, str]) -> None:
    """Points a role left at its default at the old default name, when the layer has only the old one."""
    lower = {n.lower() for n in names}
    for role, old in legacy.items():
        if fields.get(role) == defaults[role] and defaults[role].lower() not in lower and old.lower() in lower:
            fields[role] = next(n for n in names if n.lower() == old.lower())


def encodable(text: str, encoding: str) -> tuple[str, bool]:
    """
    `text` as a file in `encoding` can hold it: characters it cannot (in
    cp932: —, –, é, emoji…) become ?. True when some did, to be told.
    """
    try:
        text.encode(encoding)
        return text, False
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding), True


#: Units and marks a typed number may carry (`120m`, `30°`, `45度`).
_UNIT = re.compile(r"\s*(?:m|メートル|°|度)$", re.IGNORECASE)


def read_number(text: str) -> float | None:
    """
    A number typed in a box: full-width digits (`１２０．５`), a thousands
    comma and a unit (`120 m`, `30°`) are read too. None when empty; raises
    ValueError when the text is not a number.
    """
    t = unicodedata.normalize("NFKC", text or "").strip().replace("−", "-")
    if not t:
        return None
    t = _UNIT.sub("", re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", t)).strip()
    n = float(t)
    if not math.isfinite(n):
        raise ValueError(text)
    return n


_DAY_PARTS = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")


def read_day(text: str) -> str:
    """
    A day typed in a box as `yyyy-mm-dd`: `2026/11/9`, `2026-11-9`,
    `2026.11.09`, `20261109`, `2026年11月9日` and full-width digits too. ''
    when empty; raises ValueError when the text is not a day (or no such day,
    2026-02-30).
    """
    t = unicodedata.normalize("NFKC", text or "").strip()
    if not t:
        return ""
    m = _DAY_PARTS.fullmatch(t) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    if not m:
        raise ValueError(text)
    return date(int(m[1]), int(m[2]), int(m[3])).isoformat()


#: A time of day typed: `10:30`, `10:30:15`, `10時`, `10時30分`, `10時30分15秒`, `1030`, `103015`.
_CLOCKS = (re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?"),
           re.compile(r"(\d{1,2})\s*時(?:\s*(\d{1,2})\s*分(?:\s*(\d{1,2})\s*秒)?)?"),
           re.compile(r"(\d{2})(\d{2})(\d{2})?"))
#: The day at the start of a day and time (`2026-11-09`, `2026/11/9`, `2026年11月9日`, `20261109`).
_DAY_HEADS = (re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(\s*日)?"), re.compile(r"(\d{4})(\d{2})(\d{2})"))


def read_clock(text: str) -> str:
    """
    A time of day typed in a box as `hh:mm` (`hh:mm:ss` when seconds are
    typed): `10:30`, `9:05:30`, `10時`, `10時30分`, `1030` and full-width
    digits too. '' when empty; raises ValueError when it is not one (25:00).
    """
    t = unicodedata.normalize("NFKC", text or "").strip()
    if not t:
        return ""
    for pattern in _CLOCKS:
        m = pattern.fullmatch(t)
        if m:
            h, mi = int(m[1]), int(m[2] or 0)
            s = int(m[3]) if m[3] is not None else None
            if h > 23 or mi > 59 or (s is not None and s > 59):
                raise ValueError(text)
            return f"{h:02d}:{mi:02d}" + (f":{s:02d}" if s is not None else "")
    raise ValueError(text)


def read_day_time(text: str, day: str = "") -> str:
    """
    A day, or a day and a time, typed in a box: `yyyy-mm-dd`, or
    `yyyy-mm-dd hh:mm` (`hh:mm:ss` when seconds are typed). The day as
    `read_day` reads it, then a space or `T` (none after 日) and the time as
    `read_clock` reads it: `2026/11/9 10:30`, `2026-11-09T10:30`,
    `20261109 1030`, `2026年11月9日10時30分`. A time alone is on `day`
    (`yyyy-mm-dd`, the start of a period) when one is given. '' when
    empty; raises ValueError when it cannot be read.
    """
    t = unicodedata.normalize("NFKC", text or "").strip()
    if not t:
        return ""
    for head in _DAY_HEADS:
        m = head.match(t)
        if not m:
            continue
        day_text = date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        rest = t[m.end():]
        if not rest.strip():
            return day_text
        clock = rest.lstrip()
        if clock[:1] in ("T", "t"):
            clock = clock[1:]
        elif clock == rest and not m.group(0).endswith("日"):
            # 2026-11-0910:30: the day and the time run together.
            raise ValueError(text)
        return f"{day_text} {read_clock(clock)}"
    if day:
        return f"{day} {read_clock(t)}"
    raise ValueError(text)
