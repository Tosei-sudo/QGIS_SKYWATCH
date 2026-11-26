"""
「画面が止まった」 in the log (2026-11-18): when QGIS's main thread (the
map, the docks, every click) does not come round for STALL_MS or more, a
line goes to the log (tab MIKAGE): how long, what the main thread was
doing (2026-11-27), the background work running then (the plugin's and
QGIS's tasks), and how much of it was Python collecting garbage (which
stops every thread). So a stutter on someone's PC can be traced to what
caused it without measuring there.

A timer on the main thread notes when it comes round (every BEAT_MS; a
call of a few microseconds); a turn later than BEAT_MS + STALL_MS is a
stop. Lines come at most every QUIET_S (the stops in between are counted
in the next one).

What the main thread was doing: a thread of its own looks every SAMPLE_MS
whether the main thread is late (a time compared, nothing else) and, while
it is, notes where its Python is (the plugin's functions, innermost
first). GDAL, QGIS and the network let go of Python's lock while they work,
so the place is seen even while the main thread waits on a file or a
server; no Python on the main thread is QGIS's own work (drawing, layers,
the window).

通信の記録 (2026-11-27): every REPORT_S, the plugin's requests to Esri
since (core/request_stats.py) in one line: how many, how long, and the
tables that took longest.
"""
from __future__ import annotations

import collections
import gc
import os
import sys
import threading
import time
from typing import Optional

from qgis.core import Qgis, QgsApplication, QgsMessageLog
from qgis.PyQt.QtCore import QObject, QTimer

from ..core import request_stats
from .tasks import LOG_TAG

BEAT_MS = 100
#: A stop at least this long (ms) is told.
STALL_MS = 300
#: Longer than this (s) is the PC sleeping, not a stop.
ASLEEP_S = 120
#: At most one line this often (s).
QUIET_S = 10.0
#: How often the main thread's place is looked at while it is late, and from how late (ms since its last turn).
SAMPLE_MS = 50
SAMPLE_AFTER_MS = BEAT_MS + 150
#: 通信の記録: a line this often (s), when there were requests.
REPORT_S = 600

#: The plugin's folder: frames from files under it are the plugin's.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The main thread's place when it runs no Python (QGIS's own work).
QGIS_OWN = ""


def _plugin_name(filename: str) -> Optional[str]:
    """A file of the plugin as a short module name (qgis_io/obs_images.py → obs_images), else None."""
    try:
        path = os.path.abspath(filename)
    except (TypeError, ValueError):
        return None
    if not path.startswith(_PLUGIN_DIR + os.sep):
        return None
    return os.path.splitext(os.path.basename(path))[0]


def place_of(frame) -> str:
    """
    Where a thread's Python is, for the log: the plugin's innermost functions
    (up to three, innermost first: `obs_images.gdal_metadata ← observed_image
    ← refresh_images`), with the call it is in outside the plugin
    (`（gdal.Open）`); another's Python by its function; QGIS_OWN for none.
    """
    if frame is None:
        return QGIS_OWN
    outside = None
    mine: list[str] = []
    f = frame
    depth = 0
    while f is not None and len(mine) < 3 and depth < 200:
        code = f.f_code
        module = _plugin_name(code.co_filename)
        if module is None:
            if not mine and outside is None:
                outside = f"{os.path.splitext(os.path.basename(code.co_filename))[0]}.{code.co_name}"
        else:
            name = code.co_name if mine else f"{module}.{code.co_name}"
            if not mine or name != mine[-1]:
                mine.append(name)
        f = f.f_back
        depth += 1
    if not mine:
        return f"ほかの Python（{outside}）" if outside else QGIS_OWN
    text = " ← ".join(mine)
    return f"{text}（{outside}）" if outside else text


def describe_places(places: list) -> str:
    """The places noted during a stop, the most seen first (two at most): 「画面の処理: …」."""
    if not places:
        return ""
    counts = collections.Counter(places)
    shown = []
    for place, n in counts.most_common(2):
        if shown and n < 0.25 * len(places):
            break
        shown.append("QGIS 自身（描画・レイヤーの読み込み・画面の組み立てなど）" if place == QGIS_OWN else place)
    return "画面の処理: " + "、".join(shown)


class StallWatch(QObject):
    """Tells the main thread's stops in the log while it lives (start / stop), and the requests every REPORT_S."""

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
        #: The main thread's places noted since its last turn (by the looking thread).
        self._places: list[str] = []
        self._places_lock = threading.Lock()
        self._main_id = threading.get_ident()
        self._looking: Optional[threading.Thread] = None
        self._stop_looking = threading.Event()
        #: 通信の記録.
        self.report_timer = QTimer(self)
        self.report_timer.setInterval(REPORT_S * 1000)
        self.report_timer.timeout.connect(self.report)
        #: Each request line written, for tests.
        self.reported: list[str] = []

    def start(self) -> None:
        if not self.ENABLED:
            return
        if self._collected not in gc.callbacks:
            gc.callbacks.append(self._collected)
        self._last = time.perf_counter()
        self._main_id = threading.get_ident()
        self.timer.start()
        if self._looking is None or not self._looking.is_alive():
            # An event of its own: a thread being stopped never sees a later start's.
            self._stop_looking = threading.Event()
            self._looking = threading.Thread(target=self._look, args=(self._stop_looking,), name="MIKAGE stall watch", daemon=True)
            self._looking.start()
        # The requests from now on.
        request_stats.STATS.take()
        self.report_timer.start()

    def stop(self) -> None:
        self.timer.stop()
        self.report_timer.stop()
        self._stop_looking.set()
        self._looking = None
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

    def _look(self, stop: threading.Event) -> None:
        """The looking thread: while the main thread is late, where its Python is, every SAMPLE_MS."""
        while not stop.wait(SAMPLE_MS / 1000):
            if (time.perf_counter() - self._last) * 1000 < SAMPLE_AFTER_MS:
                continue
            try:
                place = place_of(sys._current_frames().get(self._main_id))
            except Exception:  # noqa: BLE001 (never let the watch itself fail)
                continue
            with self._places_lock:
                if len(self._places) < 2000:
                    self._places.append(place)

    def _beat(self) -> None:
        now = time.perf_counter()
        late = (now - self._last) * 1000 - BEAT_MS
        self._last = now
        collected, self._gc_ms = self._gc_ms, 0.0
        with self._places_lock:
            places, self._places = self._places, []
        if late < STALL_MS or late > ASLEEP_S * 1000:
            return
        if now - self._told_at < QUIET_S:
            self._held += 1
            self._held_longest = max(self._held_longest, late)
            return
        self._told_at = now
        self._tell(late, collected, places)

    def _tell(self, late: float, collected: float, places: Optional[list] = None) -> None:
        try:
            names = [t.description() for t in QgsApplication.taskManager().activeTasks()]
        except RuntimeError:
            names = []
        names = list(dict.fromkeys(n for n in names if n))
        work = "、".join(names[:6]) + (f" ほか {len(names) - 6} 件" if len(names) > 6 else "") if names else "なし"
        parts = []
        doing = describe_places(places or [])
        if doing:
            parts.append(doing)
        parts.append(f"裏の処理: {work}")
        if collected >= 0.2 * late:
            parts.append(f"うち Python のメモリ整理 {collected / 1000:.2f} 秒")
        if self._held:
            parts.append(f"この前の {QUIET_S:.0f} 秒にも {self._held} 回（最長 {self._held_longest / 1000:.2f} 秒）")
            self._held, self._held_longest = 0, 0.0
        line = f"画面が {late / 1000:.2f} 秒止まった（{'／'.join(parts)}）"
        self.told.append((late, line))
        QgsMessageLog.logMessage(line, LOG_TAG, Qgis.MessageLevel.Info, notifyUser=False)

    def report(self) -> None:
        """通信の記録: the requests since the last line, when there were any."""
        summary = request_stats.STATS.take()
        if not summary.count:
            return
        line = request_stats.describe(summary)
        self.reported.append(line)
        QgsMessageLog.logMessage(line, LOG_TAG, Qgis.MessageLevel.Info, notifyUser=False)
