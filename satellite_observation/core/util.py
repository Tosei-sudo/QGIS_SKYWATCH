"""Small helpers shared by the core modules (no Qt / QGIS)."""
from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timezone
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
