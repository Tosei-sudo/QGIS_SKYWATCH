"""
When an image was taken, as the viewer reads it (timeline.ts `imageTime()`
and time.ts): its GDAL metadata (the acquisition items satellite products
write), the NITF IDATIM, a time in its file name, the TIFF DateTime.

Text without a time zone is local time, as the viewer reads it; times are ms
since the epoch.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

MIN_YEAR, MAX_YEAR = 1800, 2200

#: Where an image's time came from, as the viewer names it.
ORIGINS = {
    "layer": "レイヤーの時系列",
    "metadata": "メタデータ",
    "nitf": "NITF",
    "name": "ファイル名",
    "tiff": "TIFF の DateTime",
    "manual": "手入力",
}


def _local(y: int, mo: int = 1, d: int = 1, h: int = 0, mi: int = 0, s: int = 0, ms: int = 0) -> Optional[int]:
    if y < MIN_YEAR or y > MAX_YEAR or not 1 <= mo <= 12 or not 1 <= d <= 31 or h > 24 or mi > 59 or s > 60:
        return None
    try:
        base = datetime(y, mo, d)
    except ValueError:
        # 2024-02-31 is not a day.
        return None
    t = base + timedelta(hours=h, minutes=mi, seconds=s, milliseconds=ms)
    return int(round(t.timestamp() * 1000))


def _zoned(y: int, mo: int, d: int, h: int, mi: int, s: int, ms: int, zone: str) -> Optional[int]:
    if _local(y, mo, d) is None:
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


def _n(s: Optional[str]) -> int:
    return 0 if s is None else int(s)


def _fraction(s: Optional[str]) -> int:
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
        args = (_n(y), _n(mo), _n(d), _n(h), _n(mi), _n(s), _fraction(f))
        return _zoned(*args, zone) if zone else _local(*args)
    m = _MONTH.match(text)
    if m:
        return _local(_n(m.group(1)), _n(m.group(2)))
    m = _COMPACT.match(text)
    if m:
        y, mo, d, h, mi, s, f, zone = m.groups()
        args = (_n(y), _n(mo), _n(d), _n(h), _n(mi), _n(s), _fraction(f))
        return _zoned(*args, zone) if zone else _local(*args)
    m = _JAPANESE.match(text)
    if m:
        return _local(_n(m.group(1)), _n(m.group(2)), _n(m.group(3)) if m.group(3) else 1, _n(m.group(4)), _n(m.group(5)), _n(m.group(6)))
    return None


_NAME_PATTERNS = [
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[T_-]?(\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.](\d{2})[-_.](\d{2})(?:[T_ -](\d{2})[-_:.]?(\d{2})(?:[-_:.]?(\d{2}))?)?(?!\d)"),
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)"),
]


def time_from_name(name: str) -> Optional[int]:
    """The time in a file name (`S2A_20240501T012345_…`, `img_2024-05-01.tif`), or None."""
    base = re.sub(r"^.*[/\\]", "", name)
    for pattern in _NAME_PATTERNS:
        m = pattern.search(base)
        if not m:
            continue
        t = _local(*(_n(g) for g in m.groups()))
        if t is not None:
            return t
    return None


#: Item names of GDAL metadata that say when an image was taken, best first.
_ACQUISITION = [
    re.compile(r"^(acquisition|acq)_?(start_?)?(date_?time|time|date)?(_utc)?$", re.IGNORECASE),
    re.compile(r"collect(ion)?_?start|first_?line_?time|sensing_?(start|time)|imaging_?date_?time|scene_?(center_?)?time|start_?time", re.IGNORECASE),
    re.compile(r"^(image|imaging|acquisition|capture)_?date$", re.IGNORECASE),
    re.compile(r"date_?time|datetime", re.IGNORECASE),
]


def time_from_metadata(items: Iterable[tuple[str, str]]) -> Optional[int]:
    """When an image was taken, from its GDAL metadata items (name, value): the acquisition items satellite products write, or None."""
    rows = [(k, (v or "").strip()) for k, v in items]
    for pattern in _ACQUISITION:
        for name, value in rows:
            if not pattern.search(name):
                continue
            t = parse_time(value)
            if t is not None:
                return t
    # IMAGING_DATE + IMAGING_TIME, as two items.
    date = next((v for k, v in rows if re.fullmatch(r"imaging_?date", k, re.IGNORECASE)), None)
    clock = next((v for k, v in rows if re.fullmatch(r"imaging_?time", k, re.IGNORECASE)), None)
    return parse_time(f"{date}T{clock}") if date and clock else None


def metadata_items_of_xml(xml: str) -> list[tuple[str, str]]:
    """The items of a GDAL metadata XML (TIFF tag 42112)."""
    return [(m.group(1), m.group(2).strip()) for m in re.finditer(r'<Item\b[^>]*\bname="([^"]+)"[^>]*>([^<]*)</Item>', xml)]


def nitf_time(idatim: Optional[str]) -> Optional[int]:
    """The NITF image date and time (IDATIM, CCYYMMDDhhmmss, UTC)."""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", (idatim or "").strip())
    if not m:
        return None
    return parse_time(f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}Z")


def image_time(name: str, metadata: dict[str, str]) -> Optional[tuple[int, str]]:
    """
    When an image was taken and where that came from, in the viewer's order:
    the GDAL metadata items (the default domain without the TIFF and NITF
    tags), the NITF IDATIM, the file name, the TIFF DateTime. None when nothing says.
    """
    items = [(k, v) for k, v in metadata.items() if not k.upper().startswith(("TIFFTAG_", "NITF_"))]
    t = time_from_metadata(items)
    if t is not None:
        return t, "metadata"
    t = nitf_time(metadata.get("NITF_IDATIM"))
    if t is not None:
        return t, "nitf"
    t = time_from_name(name)
    if t is not None:
        return t, "name"
    raw = metadata.get("TIFFTAG_DATETIME")
    t = parse_time(raw.rstrip("\0")) if raw else None
    return (t, "tiff") if t is not None else None
