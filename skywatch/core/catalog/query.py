"""
The conditions of an image catalog search as a `where` (SQL) for its Esri
layer, and the reading of its date-like values (the viewer's
catalog-query.ts). The watches build the same queries.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Optional

from ..util import is_number, time_of, utc_text
from .config import CatalogConfig

#: The Esri field types by name.
FieldTypes = dict[str, str]

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def local_day(day: str, plus: int = 0) -> int:
    """The start of a local day (`yyyy-mm-dd`, plus some days), in ms."""
    y, m, d = (int(x) for x in day.split("-"))
    return int((datetime(y, m, d) + timedelta(days=plus)).timestamp() * 1000)


def _next_day(day: str) -> str:
    y, m, d = (int(x) for x in day.split("-"))
    return (date(y, m, d) + timedelta(days=1)).isoformat()


def day_range(field: str, field_type: Optional[str], start: str, end: str) -> list[str]:
    """
    A condition on a date-like field for local days `start` to `end` (both
    included, either may be ''), written for the field's type: date fields
    compare timestamps (UTC, as ArcGIS stores them), date-only fields dates,
    numbers milliseconds, and text the leading `yyyy-mm-dd`.
    """
    def bound(day: str, op: str) -> str:
        if field_type in ("esriFieldTypeDate", "esriFieldTypeTimestampOffset"):
            return f"{field} {op} TIMESTAMP '{utc_text(local_day(day))}'"
        if field_type == "esriFieldTypeDateOnly":
            return f"{field} {op} DATE '{day}'"
        if field_type and re.search(r"Integer|Double|Single", field_type):
            return f"{field} {op} {local_day(day)}"
        return f"{field} {op} '{day}'"

    parts = []
    if _DAY.match(start or ""):
        parts.append(bound(start, ">="))
    if _DAY.match(end or ""):
        parts.append(bound(_next_day(end), "<"))
    return parts


def quote(text: str) -> str:
    """An SQL string literal."""
    return "'" + str(text).replace("'", "''") + "'"


@dataclass(frozen=True)
class CatalogSearch:
    """What the search asks for. Days are local `yyyy-mm-dd` ('' for open)."""
    acquired_from: str = ""
    acquired_to: str = ""
    registered_from: str = ""
    registered_to: str = ""
    system_registered_from: str = ""
    system_registered_to: str = ""
    #: '' for any sensor.
    sensor: str = ""
    #: Upper limit of the angle; None for none.
    max_angle: Optional[float] = None
    #: Azimuth range in degrees; from > to goes through north (350 to 10). None for no limit.
    azimuth_from: Optional[float] = None
    azimuth_to: Optional[float] = None
    #: Extra SQL, ANDed with the rest.
    where: str = ""

    def with_(self, **changes: Any) -> "CatalogSearch":
        return replace(self, **changes)


EMPTY_SEARCH = CatalogSearch()


def _num(v: float) -> str:
    """A number as JavaScript writes it in a template (20, 7.5)."""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def where_of(catalog: CatalogConfig, types: FieldTypes, search: CatalogSearch) -> str:
    """The `where` of a search."""
    f = catalog.fields
    parts: list[str] = []
    if catalog.where:
        parts.append(f"({catalog.where})")
    if f.get("acquired"):
        parts += day_range(f["acquired"], types.get(f["acquired"]), search.acquired_from, search.acquired_to)
    if f.get("registered"):
        parts += day_range(f["registered"], types.get(f["registered"]), search.registered_from, search.registered_to)
    if f.get("sensor") and search.sensor:
        parts.append(f"{f['sensor']} = {quote(search.sensor)}")
    if f.get("systemRegistered"):
        parts += day_range(f["systemRegistered"], types.get(f["systemRegistered"]), search.system_registered_from, search.system_registered_to)
    if f.get("angle") and is_number(search.max_angle):
        parts.append(f"{f['angle']} <= {_num(search.max_angle)}")
    if f.get("azimuth"):
        a, b = search.azimuth_from, search.azimuth_to
        name = f["azimuth"]
        if is_number(a) and is_number(b) and a > b:
            parts.append(f"({name} >= {_num(a)} OR {name} <= {_num(b)})")
        else:
            if is_number(a):
                parts.append(f"{name} >= {_num(a)}")
            if is_number(b):
                parts.append(f"{name} <= {_num(b)}")
    if search.where.strip():
        parts.append(f"({search.where.strip()})")
    return " AND ".join(parts) if parts else "1=1"


def time_text(value: Any) -> str:
    """A date-like value in local time (`2026/10/01 10:23`), or the text as it is."""
    if value is None or value == "":
        return ""
    t = time_of(value)
    if math.isnan(t):
        return str(value)
    return datetime.fromtimestamp(t / 1000).strftime("%Y/%m/%d %H:%M")
