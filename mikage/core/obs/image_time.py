"""
When an image was taken, as the viewer reads it (timeline.ts `imageTime()`
and time.ts): its GDAL metadata (the acquisition items satellite products
write), the NITF IDATIM, a time in its file name, the TIFF DateTime.

Text without a time zone is local time, as the viewer reads it; times are ms
since the epoch.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..time_text import MAX_YEAR, MIN_YEAR, local_ms, number, parse_time  # noqa: F401 (parse_time: read from here too)

#: Where an image's time came from, as the viewer names it.
ORIGINS = {
    "layer": "レイヤーの時系列",
    "metadata": "メタデータ",
    "nitf": "NITF",
    "name": "ファイル名",
    "tiff": "TIFF の DateTime",
    "manual": "手入力",
}


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
        t = local_ms(*(number(g) for g in m.groups()))
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
