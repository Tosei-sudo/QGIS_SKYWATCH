"""
Records whose path is a folder (plugin only): the images of the record are
the files under that folder, at any depth, whose path matches the catalog's
`imagePatterns`. Each sensor names its folders and files its own way, so
the patterns are given per sensor (the value of `fields.sensor`), with `*`
for the others:

    "imagePatterns": {
      "WV03": "(^|/)[^/]*-P1BS-[^/]*\\.TIF$",
      "SPOT7": ["^IMG_[^/]*_P_[^/]*/.*\\.JP2$", "^IMG_[^/]*_MS_[^/]*/.*\\.JP2$"],
      "*": "\\.tiff?$"
    }

A pattern is a regular expression (Python `re`, case ignored) searched in
the path below the folder with `/` between its parts (`PAN/IMG_01.TIF`). A
plain string or array applies to every sensor. Without a pattern for the
record's sensor nor `*`, the usual image files count (DEFAULT_PATTERN).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

from ..plan.output import natural_key
from ..util import is_record

#: The images of a folder when the catalog gives no pattern for the record's sensor.
DEFAULT_PATTERN = r"\.(tiff?|jp2|j2k|ntf|nitf|img)$"
#: The key of the patterns for every other sensor.
ANY = "*"
#: At most this many images are taken from one folder.
MAX_IMAGES = 1000


def _patterns(value: Any, problems: list[str], at: str) -> Optional[list[str]]:
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for i, item in enumerate(items):
        where = f"{at}[{i}]" if isinstance(value, list) else at
        if not isinstance(item, str) or not item:
            problems.append(f"{where} は正規表現の文字列で指定")
            continue
        try:
            re.compile(item, re.IGNORECASE)
        except re.error as error:
            problems.append(f"{where} の正規表現が不正（{error}）")
            continue
        out.append(item)
    return out or None


def image_patterns_of(value: Any, problems: list[str], at: str) -> dict[str, list[str]]:
    """
    `imagePatterns` as {sensor: [pattern, …]} (`*` for every other sensor):
    an object by sensor, or a string or array for every sensor.
    """
    if isinstance(value, (str, list)):
        found = _patterns(value, problems, at)
        return {ANY: found} if found else {}
    if not is_record(value):
        problems.append(f"{at} はセンサーごとの正規表現（{{ \"センサー\": \"…\", \"*\": \"…\" }}）か正規表現で指定")
        return {}
    out: dict[str, list[str]] = {}
    for sensor, patterns in value.items():
        found = _patterns(patterns, problems, f"{at}.{sensor}")
        if found:
            out[str(sensor).strip() or ANY] = found
    return out


def patterns_for(patterns: dict[str, list[str]], sensor: str) -> list[str]:
    """The patterns of a sensor (its own, regardless of case and spaces around), else `*`'s, else DEFAULT_PATTERN."""
    key = (sensor or "").strip().casefold()
    own = next((p for s, p in patterns.items() if s != ANY and s.casefold() == key), None) if key else None
    return own or patterns.get(ANY) or [DEFAULT_PATTERN]


@dataclass(frozen=True)
class FolderImages:
    """The images found in a folder: their paths below it (`/` between parts, natural order), and whether more were left out."""
    files: list[str]
    more: bool = False


def _walk(folder: str) -> Iterable[tuple[str, list[str], list[str]]]:
    return os.walk(folder)


def find_images(folder: str, patterns: Sequence[str], walk: Callable[[str], Iterable] = _walk,
                limit: int = MAX_IMAGES) -> FolderImages:
    """The files under `folder` (at any depth) whose path below it any of `patterns` matches."""
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    found: list[str] = []
    more = False
    for here, dirs, files in walk(folder):
        dirs.sort(key=natural_key)
        rel = os.path.relpath(here, folder).replace(os.sep, "/")
        prefix = "" if rel in (".", "") else rel + "/"
        for name in sorted(files, key=natural_key):
            path = prefix + name
            if any(c.search(path) for c in compiled):
                if len(found) >= limit:
                    more = True
                    break
                found.append(path)
        if more:
            break
    return FolderImages(sorted(found, key=natural_key), more)
