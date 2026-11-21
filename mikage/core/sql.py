"""
How the plugin writes times in the services' SQL (docs/architecture.md データ窓口): `TIMESTAMP '…'`
and `DATE '…'`, the same way for every feature. An instant is written in UTC, as the service is
asked; EsriClient moves it to the source's timeZone (time_zone.to_service) where the source says so.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Optional

from .util import utc_text


def timestamp(ms: float) -> str:
    """`TIMESTAMP 'yyyy-mm-dd hh:mm:ss'` of an instant (UTC)."""
    return f"TIMESTAMP '{utc_text(ms)}'"


def date_of(ms: float) -> str:
    """`DATE 'yyyy-mm-dd'`: the day (UTC) of an instant."""
    return f"DATE '{utc_text(ms)[:10]}'"


def date_text(day: str) -> str:
    """`DATE 'yyyy-mm-dd'` of a day already written so."""
    return f"DATE '{day}'"


def day_start(d: date) -> str:
    """`TIMESTAMP 'yyyy-mm-dd 00:00:00'`: the start of a day, as written."""
    return f"TIMESTAMP '{d.isoformat()} 00:00:00'"


def since_condition(name: str, field_type: Optional[str], t: float) -> str:
    """`name` at or after `t`, written for the field's type."""
    if field_type == "esriFieldTypeDateOnly":
        return f"{name} >= {date_of(t)}"
    if field_type and re.search(r"Integer|Double|Single", field_type):
        return f"{name} >= {math.floor(t)}"
    if field_type == "esriFieldTypeString":
        return f"{name} >= '{utc_text(t)[:10]}'"
    return f"{name} >= {timestamp(t)}"
