"""
The Esri answers 「データ分析」 read lately (tsudo, 2026-11-17: the 最適化エンジン's
first stage): the same query of the same table is answered from memory
within `analysisCacheMinutes` (5 unless set; 0 = never), so running an
analysis again, or another one on the same tables, reads nothing twice.

- A layer that says when its data last changed (its information's
  editingInfo.dataLastEditDate, else lastEditDate) is read again as soon as
  that changes (its information is asked every run anyway).
- What the plugin itself sends to a layer (applyEdits …: core/esri.py)
  forgets that layer's answers.
- 「一覧を更新」 forgets them all. Kept in this QGIS only (never on disk), at
  most MAX_ROWS rows in all (the oldest go first).

An answer with every attribute also answers a query of fewer (the columns
an analysis reads: core/analysis_plan.py), and one with shapes a query
without. The token is part of the key (hashed: another sign-in reads again).
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_MINUTES = 5
#: The rows kept in all (the oldest answers go first).
MAX_ROWS = 300_000
MAX_ENTRIES = 200


@dataclass
class _Entry:
    key: tuple
    #: The attributes it has (lower case), None for all.
    fields: Optional[frozenset]
    geometry: bool
    stamp: Optional[float]
    at: float
    features: Optional[list] = None
    count: Optional[int] = None

    @property
    def rows(self) -> int:
        return len(self.features) if self.features is not None else 1


_lock = threading.Lock()
_entries: list[_Entry] = []


def stamp_of(info: Any) -> Optional[float]:
    """When the layer says its data last changed (ms), from its information; None when it does not."""
    editing = info.get("editingInfo") if isinstance(info, dict) else None
    if not isinstance(editing, dict):
        return None
    for name in ("dataLastEditDate", "lastEditDate"):
        v = editing.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _key(kind: str, url: str, where: str, token: Optional[str]) -> tuple:
    return (kind, (url or "").rstrip("/"), " ".join((where or "").split()),
            hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16])


def _fresh(e: _Entry, stamp: Optional[float], minutes: float, now: float) -> bool:
    return now - e.at < minutes * 60 and e.stamp == stamp


def features(url: str, where: str, token: Optional[str], fields: Optional[frozenset], geometry: bool, stamp: Optional[float],
             minutes: float, now: Optional[float] = None) -> Optional[tuple[list, float]]:
    """The features of an earlier answer that has `fields` (None: all) and the shapes when asked, and its age (s); None for none."""
    if minutes <= 0:
        return None
    now = time.monotonic() if now is None else now
    key = _key("query", url, where, token)
    with _lock:
        for i in range(len(_entries) - 1, -1, -1):
            e = _entries[i]
            if (e.key == key and e.features is not None and _fresh(e, stamp, minutes, now)
                    and (e.fields is None or (fields is not None and fields <= e.fields)) and (e.geometry or not geometry)):
                _entries.append(_entries.pop(i))
                return e.features, now - e.at
    return None


def keep_features(url: str, where: str, token: Optional[str], fields: Optional[frozenset], geometry: bool, stamp: Optional[float],
                  found: list, minutes: float, now: Optional[float] = None) -> None:
    if minutes <= 0 or len(found) > MAX_ROWS:
        return
    key = _key("query", url, where, token)
    with _lock:
        _entries[:] = [e for e in _entries if not (e.key == key and e.fields == fields and e.geometry == geometry)]
        _entries.append(_Entry(key, fields, geometry, stamp, time.monotonic() if now is None else now, features=found))
        _trim()


def count(url: str, where: str, token: Optional[str], stamp: Optional[float], minutes: float, now: Optional[float] = None) -> Optional[int]:
    """The row count an earlier answer gave; None for none."""
    if minutes <= 0:
        return None
    now = time.monotonic() if now is None else now
    key = _key("count", url, where, token)
    with _lock:
        for e in reversed(_entries):
            if e.key == key and _fresh(e, stamp, minutes, now):
                return e.count
    return None


def keep_count(url: str, where: str, token: Optional[str], stamp: Optional[float], n: int, minutes: float,
               now: Optional[float] = None) -> None:
    if minutes <= 0:
        return
    key = _key("count", url, where, token)
    with _lock:
        _entries[:] = [e for e in _entries if e.key != key]
        _entries.append(_Entry(key, None, False, stamp, time.monotonic() if now is None else now, count=n))
        _trim()


def age(url: str, stamp: Optional[float], minutes: float, now: Optional[float] = None) -> Optional[float]:
    """How old (s) the newest answer still kept for a table is; None for none."""
    if minutes <= 0:
        return None
    now = time.monotonic() if now is None else now
    url = (url or "").rstrip("/")
    with _lock:
        ages = [now - e.at for e in _entries if e.key[0] == "query" and e.key[1] == url and _fresh(e, stamp, minutes, now)]
    return min(ages) if ages else None


def forget(url: str) -> None:
    """Forgets a layer's answers (the plugin edited it)."""
    url = (url or "").rstrip("/")
    with _lock:
        _entries[:] = [e for e in _entries if e.key[1] != url]


def clear() -> None:
    with _lock:
        _entries.clear()


def _trim() -> None:
    total = sum(e.rows for e in _entries)
    while _entries and (total > MAX_ROWS or len(_entries) > MAX_ENTRIES):
        total -= _entries.pop(0).rows
