"""
Times written as text, read as the viewer reads them (time.ts `parseTime` for text): ISO 8601 and
the like, compact (20240131T120000), Japanese (2024年1月31日 12時). Text without a time zone is
local time; times are ms since the epoch. Shared by the features (core/obs/image_time.py reads
an image's time with it; the settings file's time zones, moving objects, the algorithms).
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

MIN_YEAR, MAX_YEAR = 1800, 2200


def local_ms(y: int, mo: int = 1, d: int = 1, h: int = 0, mi: int = 0, s: int = 0, ms: int = 0) -> Optional[int]:
    """A local time's ms since the epoch (None: not a day, or a year out of MIN_YEAR…MAX_YEAR)."""
    if y < MIN_YEAR or y > MAX_YEAR or not 1 <= mo <= 12 or not 1 <= d <= 31 or h > 24 or mi > 59 or s > 60:
        return None
    try:
        base = datetime(y, mo, d)
    except ValueError:
        # 2024-02-31 is not a day.
        return None
    t = base + timedelta(hours=h, minutes=mi, seconds=s, milliseconds=ms)
    return int(round(t.timestamp() * 1000))


def zoned_ms(y: int, mo: int, d: int, h: int, mi: int, s: int, ms: int, zone: str) -> Optional[int]:
    """A time at a zone ("Z", "+09:00", "+0900", "+09") as ms since the epoch (None: not a day or zone)."""
    if local_ms(y, mo, d) is None:
        return None
    utc = datetime(y, mo, d, tzinfo=timezone.utc) + timedelta(hours=h, minutes=mi, seconds=s, milliseconds=ms)
    t = int(round(utc.timestamp() * 1000))
    if zone in ("Z", "z"):
        return t
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", zone)
    if not m:
        return None
    minutes = (int(m.group(2)) * 60 + int(m.group(3) or 0)) * (-1 if m.group(1) == "-" else 1)
    return t - minutes * 60_000


def number(s: Optional[str]) -> int:
    """A matched group's number (0 when it did not match)."""
    return 0 if s is None else int(s)


def fraction_ms(s: Optional[str]) -> int:
    """A second's fraction's digits as ms ("5" → 500)."""
    return int(math.floor(float(f"0.{s}") * 1000 + 0.5)) if s else 0


_ISO = re.compile(r"^(\d{4})([-/.:])(\d{1,2})\2(\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?\s*(Z|z|[+-]\d{2}:?\d{2}|[+-]\d{2})?$")
_MONTH = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:[T_ ]?(\d{2})(\d{2})(\d{2})?(?:\.(\d+))?)?(Z)?$")
_JAPANESE = re.compile(r"^(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?\s*(?:(\d{1,2})[時:](?:(\d{1,2})分?)?(?:(\d{1,2})秒?)?)?$")


def parse_time(value) -> Optional[int]:
    """The time of a text (ISO 8601 and the like, compact, Japanese), or None (as the viewer's parseTime for text)."""
    if value is None or not isinstance(value, str):
        return None
    text = value.strip()
    if not text or re.fullmatch(r"\d{4}", text):
        return None
    m = _ISO.match(text)
    if m:
        y, _, mo, d, h, mi, s, f, zone = m.groups()
        args = (number(y), number(mo), number(d), number(h), number(mi), number(s), fraction_ms(f))
        return zoned_ms(*args, zone) if zone else local_ms(*args)
    m = _MONTH.match(text)
    if m:
        return local_ms(number(m.group(1)), number(m.group(2)))
    m = _COMPACT.match(text)
    if m:
        y, mo, d, h, mi, s, f, zone = m.groups()
        args = (number(y), number(mo), number(d), number(h), number(mi), number(s), fraction_ms(f))
        return zoned_ms(*args, zone) if zone else local_ms(*args)
    m = _JAPANESE.match(text)
    if m:
        return local_ms(number(m.group(1)), number(m.group(2)), number(m.group(3)) if m.group(3) else 1, number(m.group(4)), number(m.group(5)), number(m.group(6)))
    return None
