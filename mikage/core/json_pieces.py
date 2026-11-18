"""
JSON read in pieces, so that reading a large answer off the main thread
does not stop QGIS's screen (2026-11-18, 「通信中のカクつき」).

`json.loads` reads a whole text in one call into C, and Python's lock (the
GIL) is not handed on until it returns: a page of 2,000 features (several
MB) held it some 50–100 ms, and two or three reads at once held the main
thread (the map, the docks, every click) for several hundred ms. Here the
large arrays of an answer (an Esri query's `features`, a GeoJSON's
features, a feed's list) are read an element at a time, by the same C
reader: between elements the GIL goes to the main thread when it waits.
The value is the same as `json.loads`'s, and so are the errors
(json.JSONDecodeError, a ValueError).
"""
from __future__ import annotations

import json
import re
from typing import Any

#: Texts shorter than this (characters) are read in one call (they take a few ms at most).
PIECES_FROM = 256 * 1024

_decoder = json.JSONDecoder()
_SPACE = re.compile(r"[ \t\n\r]*")
#: The separator after a value: a comma or the closing bracket, with the spaces around it.
_AFTER = re.compile(r"[ \t\n\r]*([,\]}])[ \t\n\r]*")


def loads(text: str | bytes | bytearray) -> Any:
    """`json.loads`, in pieces when the text is large (an object or array whose values are objects or arrays)."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8")
    if len(text) < PIECES_FROM:
        return json.loads(text)
    i = _SPACE.match(text, 0).end()
    first = text[i:i + 1]
    if first == "{":
        value, end = _object(text, i)
    elif first == "[":
        value, end = _array(text, i)
    else:
        return json.loads(text)
    end = _SPACE.match(text, end).end()
    if end != len(text):
        raise json.JSONDecodeError("Extra data", text, end)
    return value


def _value(text: str, i: int) -> tuple[Any, int]:
    """A value at `i` (spaces skipped before it): an array of objects or arrays element by element, else at once."""
    if text[i:i + 1] == "[":
        j = _SPACE.match(text, i + 1).end()
        if text[j:j + 1] in ("{", "["):
            return _array(text, i)
    return _decoder.raw_decode(text, i)


def _array(text: str, i: int) -> tuple[list, int]:
    """The array opening at `i`, an element at a time."""
    out: list = []
    j = _SPACE.match(text, i + 1).end()
    if text[j:j + 1] == "]":
        return out, j + 1
    raw = _decoder.raw_decode
    while True:
        item, j = raw(text, j)
        out.append(item)
        after = _AFTER.match(text, j)
        if after is None or after.group(1) == "}":
            raise json.JSONDecodeError("Expecting ',' delimiter", text, j)
        j = after.end()
        if after.group(1) == "]":
            return out, j


def _object(text: str, i: int) -> tuple[dict, int]:
    """The object opening at `i`: its keys one by one, a large array among its values element by element."""
    out: dict = {}
    j = _SPACE.match(text, i + 1).end()
    if text[j:j + 1] == "}":
        return out, j + 1
    while True:
        if text[j:j + 1] != '"':
            raise json.JSONDecodeError("Expecting property name enclosed in double quotes", text, j)
        key, j = _decoder.raw_decode(text, j)
        j = _SPACE.match(text, j).end()
        if text[j:j + 1] != ":":
            raise json.JSONDecodeError("Expecting ':' delimiter", text, j)
        j = _SPACE.match(text, j + 1).end()
        out[key], j = _value(text, j)
        after = _AFTER.match(text, j)
        if after is None or after.group(1) == "]":
            raise json.JSONDecodeError("Expecting ',' delimiter", text, j)
        j = after.end()
        if after.group(1) == "}":
            return out, j
