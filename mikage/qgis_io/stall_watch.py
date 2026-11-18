"""
「画面が止まった」 in the log (2026-11-18): when QGIS's main thread (the
map, the docks, every click) does not come round for STALL_MS or more, a
line goes to the log (tab MIKAGE): how long, the background work running
then (the plugin's and QGIS's tasks), and how much of it was Python
collecting garbage (which stops every thread). So a stutter on someone's
PC can be traced to what caused it without measuring there.

A timer on the main thread notes when it comes round (every BEAT_MS; a
call of a few microseconds); a turn later than BEAT_MS + STALL_MS is a
stop. Lines come at most every QUIET_S (the stops in between are counted
in the next one).
"""
from __future__ import annotations

import gc
import time
from typing import Optional

from qgis.core import Qgis, QgsApplication, QgsMessageLog
from qgis.PyQt.QtCore import QObject, QTimer

from .tasks import LOG_TAG

BEAT_MS = 100
#: A stop at least this long (ms) is told.
STALL_MS = 300
#: Longer than this (s) is the PC sleeping, not a stop.
ASLEEP_S = 120
#: At most one line this often (s).
QUIET_S = 10.0


class StallWatch(QObject):
    """Tells the main thread's stops in the log while it lives (start / stop)."""

    #: Off in the plugin's tests (tests/conftest.py; tests/test_qgis_stall_watch.py turns it on).
    ENABLED = True

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.setInterval(BEAT_MS)
        self.timer.timeout.connect(self._beat)
        self._last = 0.0
        #: Garbage collection since the last turn (ms), and when the one running started.
        self._gc_ms = 0.0
        self._gc_started: Optional[float] = None
        #: Stops not told yet (QUIET_S): how many, the longest (ms), and when a line was last written.
        self._held = 0
        self._held_longest = 0.0
        self._told_at = 0.0
        #: Each stop told (ms, the line), for tests.
        self.told: list[tuple[float, str]] = []

    def start(self) -> None:
        if not self.ENABLED:
            return
        if self._collected not in gc.callbacks:
            gc.callbacks.append(self._collected)
        self._last = time.perf_counter()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()
        try:
            gc.callbacks.remove(self._collected)
        except ValueError:
            pass

    def _collected(self, phase: str, _info: dict) -> None:
        # Any thread, with Python's lock held: kept as short as it can be.
        if phase == "start":
            self._gc_started = time.perf_counter()
        elif self._gc_started is not None:
            self._gc_ms += (time.perf_counter() - self._gc_started) * 1000
            self._gc_started = None

    def _beat(self) -> None:
        now = time.perf_counter()
        late = (now - self._last) * 1000 - BEAT_MS
        self._last = now
        collected, self._gc_ms = self._gc_ms, 0.0
        if late < STALL_MS or late > ASLEEP_S * 1000:
            return
        if now - self._told_at < QUIET_S:
            self._held += 1
            self._held_longest = max(self._held_longest, late)
            return
        self._told_at = now
        self._tell(late, collected)

    def _tell(self, late: float, collected: float) -> None:
        try:
            names = [t.description() for t in QgsApplication.taskManager().activeTasks()]
        except RuntimeError:
            names = []
        names = list(dict.fromkeys(n for n in names if n))
        work = "、".join(names[:6]) + (f" ほか {len(names) - 6} 件" if len(names) > 6 else "") if names else "なし"
        parts = [f"裏の処理: {work}"]
        if collected >= 0.2 * late:
            parts.append(f"うち Python のメモリ整理 {collected / 1000:.2f} 秒")
        if self._held:
            parts.append(f"この前の {QUIET_S:.0f} 秒にも {self._held} 回（最長 {self._held_longest / 1000:.2f} 秒）")
            self._held, self._held_longest = 0, 0.0
        line = f"画面が {late / 1000:.2f} 秒止まった（{'／'.join(parts)}）"
        self.told.append((late, line))
        QgsMessageLog.logMessage(line, LOG_TAG, Qgis.MessageLevel.Info, notifyUser=False)
