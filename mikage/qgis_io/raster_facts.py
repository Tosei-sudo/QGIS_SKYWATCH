"""
画像ファイルから読むもの (2026-11-27): what the plugin reads of a raster
layer's own file with GDAL (its metadata by domain; whether its pixels are
complex SAR), read once per layer and kept. Each image added to the project
was opened again four or five times on the main thread (the observation
dock's images, whether it is an image to record on, complex SAR), and
every layer already open again too: each open from a share is a few dozen
ms (more in a folder of thousands of images).

Kept by the layer's id and the file's path: a layer added again (a new id)
reads its file again, so a file rewritten in between is read anew; the
KEPT most recent. One open reads the default domain and the bands (what
each image added needs); another domain ('IMD'…) one open more, once. A
file GDAL cannot open is not kept (asked again next time).
"""
from __future__ import annotations

import collections
import threading
from typing import Any, Optional

#: How many files' reads are kept.
KEPT = 256

_kept: "collections.OrderedDict[tuple[str, str], dict]" = collections.OrderedDict()
_lock = threading.Lock()
#: Opens done (for tests).
opened = 0


def metadata(key: str, path: str, domain: str = "") -> dict[str, str]:
    """The GDAL metadata of a domain of the file ('' the default…), {} when GDAL cannot read it; `key` is the layer's id."""
    found = _read(key, path, ("metadata", domain))
    return dict(found) if found else {}


def complex_outputs(key: str, path: str) -> list:
    """The amplitude bands to show of the file (complex_sar.outputs_of), [] when not complex or GDAL cannot read it."""
    found = _read(key, path, ("complex",))
    return list(found) if found else []


def _read(key: str, path: str, what: tuple) -> Optional[Any]:
    entry_key = (key or "", path)
    with _lock:
        entry = _kept.get(entry_key)
        if entry is not None and what in entry:
            _kept.move_to_end(entry_key)
            return entry[what]
    read = _open_and_read(path, what, first=entry is None)
    if read is None:
        return None
    with _lock:
        entry = _kept.setdefault(entry_key, {})
        entry.update(read)
        _kept.move_to_end(entry_key)
        while len(_kept) > KEPT:
            _kept.popitem(last=False)
        return entry.get(what)


def _open_and_read(path: str, what: tuple, first: bool) -> Optional[dict]:
    """One open of the file: `what`, and on the first one the default domain and the bands too; None when GDAL cannot open it."""
    global opened
    try:
        from osgeo import gdal
    except ImportError:
        return None
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        opened += 1
        dataset = gdal.Open(path)
        if dataset is None:
            return None
        read: dict = {}
        wanted = [what] + ([("metadata", ""), ("complex",)] if first else [])
        for item in dict.fromkeys(wanted):
            if item[0] == "metadata":
                read[item] = dict(dataset.GetMetadata(item[1]) or {})
            else:
                from .complex_sar import outputs_of

                read[item] = tuple(outputs_of(dataset))
        return read
    except Exception:  # noqa: BLE001 (a file GDAL cannot read here says nothing)
        return None
    finally:
        gdal.PopErrorHandler()


def forget() -> None:
    """Forgets every file's reads (on the plugin's unload)."""
    with _lock:
        _kept.clear()
