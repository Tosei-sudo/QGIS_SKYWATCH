"""
通信の記録 (2026-11-27): the plugin's requests to Esri counted by layer and
kind, for a line in the log now and then (qgis_io/stall_watch.py): how many,
how long, how much, the longest, and how many read a whole table. So the
tables slow on someone's server (an SDE table without its index, a big
ledger read whole) can be found from their log, without measuring there.

Noted by EsriClient for each request (any thread; a few microseconds);
`take` hands what was noted since and starts again.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

#: The layer a request belongs to: the service's layer URL (…/FeatureServer/3, …/MapServer/0).
_LAYER = re.compile(r"^(.*?/(?:FeatureServer|MapServer)/\d+)(?:/|$)", re.IGNORECASE)
_EDITS = ("applyedits", "addfeatures", "updatefeatures", "deletefeatures")


def layer_of(url: str) -> str:
    """The layer's URL of a request (its query left out), else the URL itself without its query."""
    base = url.split("?", 1)[0].rstrip("/")
    found = _LAYER.match(base)
    return found.group(1) if found else base


def kind_of(url: str, form: dict) -> str:
    """情報 (the layer's information), 検索, 件数, ID, 編集 or その他."""
    base = url.split("?", 1)[0].rstrip("/")
    last = base.rsplit("/", 1)[-1].lower()
    if last == "query":
        if str(form.get("returnCountOnly", "")).lower() == "true":
            return "件数"
        if str(form.get("returnIdsOnly", "")).lower() == "true":
            return "ID"
        return "検索"
    if last in _EDITS:
        return "編集"
    if last.isdigit() and _LAYER.match(base):
        return "情報"
    return "その他"


def is_whole(form: dict) -> bool:
    """A search of the whole table (no condition): the reads that grow with the table."""
    where = str(form.get("where") or "").replace(" ", "")
    return where in ("", "1=1")


@dataclass
class Row:
    """What one layer's requests of one kind came to."""

    name: str
    kind: str
    count: int = 0
    seconds: float = 0.0
    size: int = 0
    longest: float = 0.0
    failed: int = 0
    whole: int = 0


@dataclass
class Summary:
    """The requests noted over `seconds`, by layer and kind."""

    seconds: float
    rows: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(r.count for r in self.rows)

    @property
    def size(self) -> int:
        return sum(r.size for r in self.rows)

    @property
    def total(self) -> float:
        return sum(r.seconds for r in self.rows)

    @property
    def failed(self) -> int:
        return sum(r.failed for r in self.rows)


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 100_000 else f"{size / 1000:.0f} KB"


def describe(summary: Summary, top: int = 3) -> str:
    """The log's line: the totals, then the layers that took longest (``top`` of them)."""
    minutes = max(1, round(summary.seconds / 60))
    head = f"通信（{minutes} 分）: {summary.count} 回・{_megabytes(summary.size)}・合計 {summary.total:.1f} 秒"
    if summary.failed:
        head += f"・失敗 {summary.failed} 回"
    slow = sorted(summary.rows, key=lambda r: r.seconds, reverse=True)[:top]
    parts = []
    for r in slow:
        text = f"{r.name} {r.kind} {r.count} 回" + (f"（全件 {r.whole}）" if r.whole else " ")
        text += f"計 {r.seconds:.1f} 秒・最長 {r.longest:.1f} 秒・{_megabytes(r.size)}"
        parts.append(text)
    return head + ("。時間のかかった表: " + "／".join(parts) if parts else "")


class RequestStats:
    """The requests noted since the last `take` (thread-safe)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], Row] = {}
        #: Each layer's name once a request told it (its information is read before its searches).
        self._names: dict[str, str] = {}
        self._since = clock()

    def note(self, url: str, form: dict, seconds: float, size: int, ok: bool = True, name: Optional[str] = None) -> None:
        layer = layer_of(url)
        kind = kind_of(url, form)
        with self._lock:
            if name:
                self._names[layer] = name
            row = self._rows.get((layer, kind))
            if row is None:
                row = self._rows[(layer, kind)] = Row(_short(layer), kind)
            row.count += 1
            row.seconds += seconds
            row.size += size
            row.longest = max(row.longest, seconds)
            if not ok:
                row.failed += 1
            if kind == "検索" and is_whole(form):
                row.whole += 1

    def take(self) -> Summary:
        """What was noted since the last take (and starts again)."""
        with self._lock:
            now = self.clock()
            rows, self._rows = self._rows, {}
            since, self._since = self._since, now
            for (layer, _kind), row in rows.items():
                row.name = self._names.get(layer, row.name)
        return Summary(now - since, list(rows.values()))


def _short(layer: str) -> str:
    """A layer's URL as a name: its service and number (Requests/0)."""
    parts = layer.rstrip("/").split("/")
    if len(parts) >= 3 and parts[-2].lower() in ("featureserver", "mapserver"):
        return f"{parts[-3]}/{parts[-1]}"
    return parts[-1] if parts else layer


#: The plugin's requests (EsriClient notes each one).
STATS = RequestStats()
