"""
Conditions on a layer's time field (a date, a number of ms, or text written
as the layer writes it): the image catalog asked for a day's images (the
follow-up of the imaging requests), and the times of a catalog search's
period written as a text field writes them.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from . import time_zone
from .esri import EsriClient, EsriError
from .util import utc_text

_SUFFIX = re.compile(r"([+-]\d{2}:?\d{2})$")




def text_sample(client: EsriClient, url: str, name: str, field_type: Optional[str], token: Optional[str],
                where: Optional[str] = None) -> str:
    """
    One value of a text date field, to write conditions the way the layer
    writes its values (`T` or a space, a suffix); "" for other types or when
    none can be read.
    """
    if field_type != "esriFieldTypeString":
        return ""
    try:
        value = client.ask(f"{url}/query", {"where": f"{name} IS NOT NULL" + (f" AND ({where})" if where else ""),
                                            "outFields": name, "returnGeometry": "false", "resultRecordCount": "1"}, token)
    except EsriError:
        return ""
    for f in value.get("features") or []:
        v = (f.get("attributes") or {}).get(name)
        if isinstance(v, str) and time_zone.is_date_time_text(v):
            return v.strip()
    return ""


def text_at(instant: float, like: str = "", zone: Optional["time_zone.TimeZone"] = None) -> str:
    """
    An instant (whole seconds) as a text date field writes its values, to
    compare as text: the separator of `like` (a value of the field; `T` when
    none), clock time in `zone` (the source's `timeZone`; else the offset
    `like` carries; else UTC), no suffix or fraction (a value's `Z` or
    fraction sorts after it, so it stays in a `>=` and out of a `<`), and no
    seconds when the values have none and the seconds are 0 (`10:00` is not
    before `10:00`).
    """
    m = _SUFFIX.search(like or "")
    z = zone or (time_zone.parse_time_zone(m.group(1)) if m else None) or time_zone.UTC
    shape = re.sub(r"(Z|z|[+-]\d{2}:?\d{2})$", "", like) if like else "2000-01-01T00:00:00"
    shape = re.sub(r"\.\d+$", "", shape)
    text = time_zone.write_text(instant, z, shape)
    if not re.search(r"\d{2}:\d{2}:\d{2}$", shape) and text.endswith(":00"):
        text = text[:-3]
    return text


def between_condition(name: str, field_type: Optional[str], start: float, end: float, like: str = "",
                      zone: Optional["time_zone.TimeZone"] = None) -> str:
    """
    `name` between two moments, written for its type. Text is compared as
    text, so the bounds are written as the layer writes its values (`like`:
    `T` or a space between date and time) in its zone (`zone`, the source's
    `timeZone`; else the offset its values carry; else UTC), and the end is
    an open bound (a value's `Z` or fraction does not push it out) (#79).
    """
    if field_type and re.search(r"Integer|Double|Single", field_type):
        return f"{name} >= {math.floor(start)} AND {name} <= {math.ceil(end)}"
    if field_type == "esriFieldTypeString":
        lo = text_at(math.floor(start / 1000) * 1000, like, zone)
        hi = text_at(math.floor(end / 1000) * 1000 + 1000, like, zone)
        return f"{name} >= '{lo}' AND {name} < '{hi}'"
    return f"{name} >= TIMESTAMP '{utc_text(start)}' AND {name} <= TIMESTAMP '{utc_text(end)}'"

