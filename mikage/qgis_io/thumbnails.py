"""
The 新着画像 cards' slow parts, one at a time off the main thread: the
thumbnails (fetched from the address the catalog's `thumbnail` pattern gives,
through QGIS's network with the authentication configuration chosen for that
address, or read from a share), kept as files in the profile
(`settings.thumbnail_folder`), and 「写っている地物」 (`footprintLayers`).

Only what the cards in view ask for is fetched, newest asked first; a fetch
that failed is not tried again for RETRY_SECONDS.
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Callable, Optional

from qgis.PyQt.QtCore import QObject, QUrl, Qt
from qgis.PyQt.QtGui import QImage

from ..core.catalog.thumbnail import is_address
from ..core.esri import EsriError
from .tasks import Cancelled, run_task

#: A failed fetch is tried again after this long.
RETRY_SECONDS = 600
#: The largest thumbnail kept as it came (larger ones are made smaller first).
MAX_SIDE = 1024
#: A file larger than this is not read as a thumbnail (a whole image by mistake).
MAX_BYTES = 30 * 1024 * 1024
#: The thumbnails kept: at most this many files and bytes (the least recently used go first).
KEEP_FILES = 3000
KEEP_BYTES = 300 * 1024 * 1024


def cache_path(folder: str, address: str) -> str:
    """The file a thumbnail is kept in (by its address, so a pattern changed fetches anew)."""
    return os.path.join(folder, hashlib.sha1(address.encode("utf-8")).hexdigest() + ".png")


def cached(folder: str, address: str) -> Optional[str]:
    """The kept file of a thumbnail, its time touched (kept longer); None when not kept."""
    path = cache_path(folder, address)
    if not os.path.isfile(path):
        return None
    try:
        os.utime(path)
    except OSError:
        pass
    return path


def _read(address: str, authcfg: Optional[str]) -> bytes:
    if is_address(address):
        from .network import get_bytes

        return get_bytes(address, authcfg)
    path = QUrl(address).toLocalFile() if address.lower().startswith("file:") else address
    if not os.path.isfile(path):
        raise ValueError(f"ファイルなし（{path}）")
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        raise ValueError(f"ファイルが大きすぎる（{size // (1024 * 1024)} MB。サムネイルは {MAX_BYTES // (1024 * 1024)} MB まで）")
    with open(path, "rb") as f:
        return f.read()


def fetch(folder: str, address: str, authcfg: Optional[str] = None) -> str:
    """Fetches a thumbnail (in a task) and keeps it; the kept file. Raises with the reason when it is not an image."""
    data = _read(address, authcfg)
    image = QImage()
    if not image.loadFromData(data):
        head = data[:200].decode("utf-8", "replace").strip()
        hint = "（サーバーの答え: " + head[:80] + "）" if head.startswith(("{", "<")) else ""
        raise ValueError("画像として読めない" + hint)
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(MAX_SIDE, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    os.makedirs(folder, exist_ok=True)
    path = cache_path(folder, address)
    temporary = path + ".part"
    if not image.save(temporary, "PNG"):
        raise OSError("サムネイルを保存できない（" + folder + "）")
    os.replace(temporary, path)
    return path


def prune(folder: str, keep_files: int = KEEP_FILES, keep_bytes: int = KEEP_BYTES) -> int:
    """The least recently used thumbnails removed past the limits; how many."""
    try:
        entries = [e for e in os.scandir(folder) if e.is_file() and e.name.endswith(".png")]
    except OSError:
        return 0
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    total, removed = 0, 0
    for i, e in enumerate(entries):
        total += e.stat().st_size
        if i >= keep_files or total > keep_bytes:
            try:
                os.remove(e.path)
                removed += 1
            except OSError:
                pass
    return removed


class CardWork(QObject):
    """
    The cards' slow work, one job at a time in a hidden task: `want(jobs)`
    gives the jobs of the cards in view (by key, the first first); a job
    already done, running or failed lately is not started again.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        #: (key, work(task) → result, done(result), failed(message)) waiting, the first next.
        self._waiting: list[tuple] = []
        self._running: Optional[str] = None
        self._task = None
        #: Keys failed lately: when (time.monotonic()) and why.
        self.failures: dict[str, tuple[float, str]] = {}
        self._closed = False
        self._written = 0

    def failure(self, key: str) -> Optional[str]:
        """Why a job failed lately (None: not failed, or long enough ago to be tried again)."""
        failed = self.failures.get(key)
        if failed is None:
            return None
        if time.monotonic() - failed[0] > RETRY_SECONDS:
            del self.failures[key]
            return None
        return failed[1]

    def want(self, jobs: list[tuple[str, Callable[[Any], Any], Callable[[Any], None], Callable[[str], None]]]) -> None:
        """The jobs wanted now (those of cards out of view are dropped), started one after another."""
        seen: set[str] = set()
        waiting = []
        for job in jobs:
            key = job[0]
            if key in seen or key == self._running or self.failure(key) is not None:
                continue
            seen.add(key)
            waiting.append(job)
        self._waiting = waiting
        self._next()

    def busy(self, key: str) -> bool:
        return key == self._running or any(j[0] == key for j in self._waiting)

    def _next(self) -> None:
        if self._closed or self._running is not None or not self._waiting:
            return
        key, work, done, failed = self._waiting.pop(0)
        self._running = key

        def guarded(task):
            # What is to be expected (no file, a server's refusal, not an image) is told on the card, not logged as a fault.
            try:
                return True, work(task)
            except Cancelled:
                raise
            except (OSError, ValueError, EsriError) as error:
                return False, str(error) or type(error).__name__

        def finish(result) -> None:
            ok, value = result
            if not ok:
                fail(value)
                return
            self._running, self._task = None, None
            if not self._closed:
                done(value)
                self._next()

        def fail(message: str) -> None:
            self._running, self._task = None, None
            self.failures[key] = (time.monotonic(), message)
            if not self._closed:
                failed(message)
                self._next()

        self._task = run_task("新着画像のカード", guarded, finish, fail, hidden=True)

    def close_down(self) -> None:
        self._closed = True
        self._waiting = []
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
