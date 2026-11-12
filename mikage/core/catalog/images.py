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
import time
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


def patterns_for(patterns: dict[str, list[str]], sensor: str, names=None) -> list[str]:
    """
    The patterns of a sensor (its own, regardless of case and spaces around,
    else those of a name `names` (SatelliteNames) gives it), else `*`'s,
    else DEFAULT_PATTERN.
    """
    key = (sensor or "").strip().casefold()
    own = next((p for s, p in patterns.items() if s != ANY and s.casefold() == key), None) if key else None
    if own is None and key and names:
        own = next((p for s, p in patterns.items() if s != ANY and names.same(s, sensor)), None)
    return own or patterns.get(ANY) or [DEFAULT_PATTERN]


@dataclass(frozen=True)
class FolderImages:
    """
    The images found in a folder: their paths below it (`/` between parts,
    natural order), whether more were left out, why the look stopped early
    (`stopped`: too deep, too many entries, too long, cancelled; "" when it
    went through) and how many sub-folders could not be read.
    """
    files: list[str]
    more: bool = False
    stopped: str = ""
    unreadable: int = 0


#: How deep below the record's folder images are looked for.
MAX_DEPTH = 8
#: How many files and folders are looked at, at most (matching or not).
MAX_ENTRIES = 50_000
#: How long the look may take (seconds).
MAX_SECONDS = 60.0


def _is_link(path: str) -> bool:
    """A symbolic link or (Windows) a junction: not followed, as a loop of them never ends (#76)."""
    try:
        if os.path.islink(path):
            return True
        is_junction = getattr(os.path, "isjunction", None)
        return bool(is_junction and is_junction(path))
    except OSError:
        return True


def _walk(folder: str, errors: Optional[list] = None) -> Iterable[tuple[str, list[str], list[str]]]:
    """os.walk without links or junctions; unreadable folders go to `errors`."""
    for here, dirs, files in os.walk(folder, followlinks=False, onerror=(errors.append if errors is not None else None)):
        dirs[:] = [d for d in dirs if not _is_link(os.path.join(here, d))]
        yield here, dirs, files


def find_images(folder: str, patterns: Sequence[str], walk: Optional[Callable[[str], Iterable]] = None,
                limit: int = MAX_IMAGES, max_depth: int = MAX_DEPTH, max_entries: int = MAX_ENTRIES,
                max_seconds: float = MAX_SECONDS, cancelled: Optional[Callable[[], bool]] = None,
                clock: Callable[[], float] = time.monotonic) -> FolderImages:
    """
    The files under `folder` (down to `max_depth` folders) whose path below
    it any of `patterns` matches. The look is bounded (entries, time), can
    be cancelled, does not follow links or junctions, and says why it
    stopped (#76).
    """
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    found: list[str] = []
    more, stopped = False, ""
    errors: list = []
    seen, started = 0, clock()
    for here, dirs, files in (walk(folder) if walk is not None else _walk(folder, errors)):
        rel = os.path.relpath(here, folder).replace(os.sep, "/")
        depth = 0 if rel in (".", "") else rel.count("/") + 1
        if depth >= max_depth and dirs:
            dirs[:] = []
            stopped = stopped or f"{max_depth} 階層より深いフォルダは見ていない"
        dirs.sort(key=natural_key)
        prefix = "" if rel in (".", "") else rel + "/"
        seen += len(files) + len(dirs)
        for name in sorted(files, key=natural_key):
            path = prefix + name
            if any(c.search(path) for c in compiled):
                if len(found) >= limit:
                    more = True
                    break
                found.append(path)
        if more:
            break
        if cancelled and cancelled():
            stopped = "取消"
            break
        if seen > max_entries:
            more, stopped = True, f"ファイル・フォルダが {max_entries:,} を超えたため途中まで"
            break
        if clock() - started > max_seconds:
            more, stopped = True, f"{int(max_seconds)} 秒を超えたため途中まで"
            break
    return FolderImages(sorted(found, key=natural_key), more, stopped, len(errors))
