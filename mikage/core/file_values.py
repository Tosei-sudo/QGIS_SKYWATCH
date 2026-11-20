"""
Values as they go into the files written for others (撮像依頼の日次出力, 観測記録のエクスポート): one
printf conversion, a value as a CSV cell, and a value turned to its JSON Schema's type. Shared base
(docs/architecture.md): both features write their files with them.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Optional

from .json_schema import resolve

#: Text that is a plain number (a printf number format is applied to it as to a number).
_NUMBER_TEXT = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)\s*$")
#: The printf conversions that take a number (whole ones rounded half up first).
_NUMBER_FORMAT = re.compile(r"%[-+ 0#]*\d*(\.\d+)?[diouxXeEfF]")
_WHOLE_FORMAT = re.compile(r"%[-+ 0#]*\d*(\.\d+)?[diouxX]")


def printf(fmt: str, value: Any) -> Any:
    """
    One printf conversion (`%03d`, `%.1f`, `%+d`, `%5.1f`, `%x`, `%e`, `%s`) with
    text around it (`P%02d`, `%d%%`) applied to a value: whole conversions
    (d i o u x X) round half up first (12.5 → 13). Text that is a number is
    taken as the number by a number conversion. The value as it is when the
    format does not fit (not a number, none or two conversions).
    """
    try:
        if isinstance(value, str) and _NUMBER_FORMAT.search(fmt):
            if not _NUMBER_TEXT.match(value):
                return value
            value = float(value)
        if _WHOLE_FORMAT.search(fmt):
            return fmt % int(math.floor(float(value) + 0.5))
        return fmt % value
    except (TypeError, ValueError, OverflowError):
        return value


def cell_text(v: Any) -> Any:
    """A value as a CSV cell: a list joined by commas, an object as JSON, none as empty."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ",".join(str(cell_text(x)) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


def coerce(value: Any, schema: dict, root: Optional[dict] = None) -> Any:
    """A value turned to the schema's type where that is plain (text ↔ number; a list → an array, or text joined by commas)."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    if isinstance(value, list):
        if "array" not in types:
            return cell_text(value)
        items = resolve(schema.get("items"), root or schema)
        return [coerce(v, items, root) for v in value] if isinstance(items, dict) else value
    if value is None or not types:
        return value
    if "string" in types and isinstance(value, (int, float)) and not isinstance(value, bool) and not any(x in types for x in ("integer", "number")):
        return str(value)
    if isinstance(value, str) and ("integer" in types or "number" in types) and "string" not in types:
        try:
            n = float(value)
        except ValueError:
            return value
        return int(n) if "integer" in types and n.is_integer() else n
    if "integer" in types and isinstance(value, float) and value.is_integer() and "number" not in types:
        return int(value)
    return value
