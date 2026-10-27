"""
Images an image catalog knows by a local path (`\\\\nas\\img\\…`, `Z:\\…`,
`/mnt/…`, `file://…`), as the viewer's local-paths.ts: `pathMappings` say
what each prefix stands for on this computer.

- with `url`, the same folder served over HTTP: the rest of the path is
  added to the URL and the image opens through /vsicurl/;
- without, the path is first opened as it is (QGIS opens UNC and drive
  paths); when it is not there, the folder chosen for the prefix on this
  computer (kept in QgsSettings) is followed down, case-insensitively.

GDAL reads the .ovr, .RPB / _RPC.TXT and .IMD beside an image by itself.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote, unquote

from ..util import is_record


@dataclass(frozen=True)
class PathMapping:
    """One prefix of local paths and what it stands for."""
    #: The start of the paths, as the catalog writes them (`\\\\nas\\img`, `Z:\\images`). Case and `\\` / `/` do not matter.
    prefix: str
    #: The folder's name when asking for it.
    label: str
    #: The same folder over HTTP; None to choose the folder on this computer.
    url: Optional[str] = None


def path_mapping_of(value: Any, problems: list[str], index: int) -> Optional[PathMapping]:
    """The mapping of one `pathMappings` entry; None (with the reason in `problems`) when it cannot be used."""
    at = f"pathMappings[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    prefix, label, url = value.get("prefix"), value.get("label"), value.get("url")
    if not isinstance(prefix, str) or not path_parts(prefix):
        problems.append(f"{at} に prefix（パスの先頭）なし")
        return None
    if "url" in value and (not isinstance(url, str) or not re.search(r"^(https?:)?//|^\.{0,2}/", url)):
        problems.append(f"{at} の url は http(s) か相対パスの URL で指定")
        return None
    return PathMapping(prefix, label if isinstance(label, str) and label else prefix, url if isinstance(url, str) else None)


def path_mappings_of(value: Any, problems: list[str]) -> list[PathMapping]:
    if not isinstance(value, list):
        problems.append("pathMappings は配列で指定")
        return []
    return [m for m in (path_mapping_of(v, problems, i) for i, v in enumerate(value)) if m is not None]


_FILE = re.compile(r"^file:(//[^/]*)?(/.*)$", re.IGNORECASE)


def _file_path(text: str) -> Optional[str]:
    """The path a `file:` URL names (`file:///C:/x` → `C:/x`, `file://nas/x` → `//nas/x`); None for other text."""
    m = _FILE.match(text)
    if not m:
        return None
    host = m.group(1) if m.group(1) and m.group(1) != "//" and m.group(1).lower() != "//localhost" else ""
    rest = unquote(m.group(2))
    if host:
        return host + rest
    return rest[1:] if re.match(r"^/[A-Za-z]:", rest) else rest


def path_parts(path: str) -> list[str]:
    """
    The parts of a path: `\\\\nas\\img\\a.tif` → `['//nas', 'img', 'a.tif']`,
    `C:\\x\\a.tif` → `['c:', 'x', 'a.tif']` (drive letters in lower case),
    `/mnt/a.tif` → `['/', 'mnt', 'a.tif']`; `file://` URLs as the path they name.
    """
    text = path.strip()
    text = _file_path(text) or text
    text = text.replace("\\", "/")
    head: Optional[str] = None
    if text.startswith("//"):
        server, *rest = text[2:].split("/")
        if not server:
            return []
        head = "//" + server.lower()
        text = "/".join(rest)
    elif re.match(r"^[A-Za-z]:", text):
        head = text[:2].lower()
        text = text[2:]
    elif text.startswith("/"):
        head = "/"
    parts = [p for p in text.split("/") if p not in ("", ".")]
    return [head, *parts] if head else parts


def mapping_for(mappings: Sequence[PathMapping], path: str) -> Optional[tuple[PathMapping, list[str]]]:
    """The mapping with the longest prefix that `path` starts with, and the parts of the path after it."""
    parts = path_parts(path)
    best: Optional[tuple[PathMapping, list[str], int]] = None
    for mapping in mappings:
        prefix = path_parts(mapping.prefix)
        if len(prefix) >= len(parts) or (best and len(prefix) <= best[2]):
            continue
        if all(p.lower() == parts[i].lower() for i, p in enumerate(prefix)):
            best = (mapping, parts[len(prefix):], len(prefix))
    return (best[0], best[1]) if best else None


def url_for(mapping: PathMapping, rest: Sequence[str]) -> str:
    """The URL of the rest of a path under a mapping's `url` (each part encoded as encodeURIComponent does)."""
    return (mapping.url or "").rstrip("/") + "/" + "/".join(quote(p, safe="-_.!~*'()") for p in rest)


def folder_key(prefix: str) -> str:
    """The same prefix however it is written, for keeping its folder."""
    return "/".join(path_parts(prefix)).lower()


def native_path(path: str) -> str:
    """A catalog's path as this computer opens it (a `file:` URL as its path)."""
    text = path.strip()
    return _file_path(text) or text


def find_file(root: str, rest: Sequence[str], listdir: Callable[[str], list[str]] = os.listdir) -> Optional[str]:
    """The file at `rest` inside `root`, each name matched as it is or else regardless of case (as on Windows and most NAS shares)."""
    here = root
    for i, name in enumerate(rest):
        last = i == len(rest) - 1
        kind = os.path.isfile if last else os.path.isdir
        candidate = os.path.join(here, name)
        if kind(candidate):
            here = candidate
            continue
        try:
            entries = listdir(here)
        except OSError:
            return None
        lower = name.lower()
        match = next((e for e in entries if e.lower() == lower and kind(os.path.join(here, e))), None)
        if match is None:
            return None
        here = os.path.join(here, match)
    return here


class PathError(Exception):
    """A local path that cannot be opened, and why."""


@dataclass(frozen=True)
class Resolved:
    """Where a local path leads: `url` (opened through /vsicurl/), `file` (a path here) or `folder` (a folder to choose for `mapping` first)."""
    kind: str
    value: str = ""
    mapping: Optional[PathMapping] = None


def resolve_path(path: str, mappings: Sequence[PathMapping], folder_for: Callable[[str], Optional[str]],
                 exists: Callable[[str], bool] = os.path.isfile) -> Resolved:
    """
    Where `path` leads: a mapping's URL; the path itself when it is here; the
    folder chosen for its mapping followed down; or `folder` when that folder
    is still to be chosen. Raises PathError with the reason when it cannot be opened.
    """
    found = mapping_for(mappings, path)
    if found and found[0].url is not None:
        return Resolved("url", url_for(found[0], found[1]))
    here = native_path(path)
    if exists(here):
        return Resolved("file", here)
    if not found:
        raise PathError(f"「{path}」なし（設定ファイルの pathMappings にも先頭の合う prefix なし）")
    mapping, rest = found
    folder = folder_for(mapping.prefix)
    if not folder:
        return Resolved("folder", mapping=mapping)
    hit = find_file(folder, rest)
    if not hit:
        raise PathError(f"「{mapping.label}」に選んだフォルダ「{folder}」の中に {'/'.join(rest)} なし（選び直すには「フォルダの対応を消去」）")
    return Resolved("file", hit)
