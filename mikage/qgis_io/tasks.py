"""
Heavy work and requests in QgsTask, so the UI never waits: a function runs
off the main thread with a progress callback and a cancel check, and its
result (or error) comes back on the main thread.

The tasks' Python shares Python's GIL with QGIS's main thread, which calls
into Python for the plugin's map items, lists and events: while the plugin
is loaded, a thread hands the GIL on after 1 ms instead of Python's 5 ms
(share_gil_faster), and a result whose showing holds the main thread long
is told in the log (SLOW_MS), so what still stutters can be found; and
Python's garbage collection, which stops every thread, runs less often
(collect_less_often).
"""
from __future__ import annotations

import gc
import sys
import threading
import time
import traceback
from typing import Any, Callable, Optional

from qgis.core import Qgis, QgsApplication, QgsFeedback, QgsMessageLog, QgsTask

from ..core import audit, errors
from ..core.esri import EsriError

LOG_TAG = "MIKAGE"


class Cancelled(Exception):
    """The person cancelled the task."""


def _coded(shown: str, detail: str = "") -> Optional[dict]:
    """The audit log's detail of a failure: its code (エラーコード, core/errors.py) and what the screen left out."""
    out = {k: v for k, v in (("code", errors.code_of(shown)), ("detail", detail)) if v}
    return out or None


def error_text(error: BaseException) -> str:
    """
    What a screen shows for an error it caught itself (not in a task): its
    text with its エラーコード (core/errors.py). What only the log needs (a
    server's or Python's own words, a path) is written to QGIS's log.
    """
    shown, detail = errors.describe(error)
    if detail:
        QgsMessageLog.logMessage(f"{shown}（{detail}）", LOG_TAG, Qgis.MessageLevel.Info, notifyUser=False)
    return shown


#: The task whose work runs in this thread (its requests stop when it is cancelled).
_current = threading.local()


def current_feedback() -> Optional[QgsFeedback]:
    """The feedback of the task running in this thread, for requests to stop on cancel; None outside a task."""
    task = getattr(_current, "task", None)
    return task.feedback if task is not None else None


class FunctionTask(QgsTask):
    """Runs `work(task)` off the main thread; `done(result)` or `failed(message)` on the main thread."""

    def __init__(
        self,
        description: str,
        work: Callable[["FunctionTask"], Any],
        done: Optional[Callable[[Any], None]] = None,
        failed: Optional[Callable[[str], None]] = None,
        flags: QgsTask.Flag = QgsTask.Flag.CanCancel,
    ):
        super().__init__(description, flags)
        self._work = work
        self._done = done
        self._failed = failed
        self.result: Any = None
        self.error: Optional[str] = None
        #: Cancelled with the task: a request waiting on the network stops at once.
        self.feedback = QgsFeedback()
        #: The plugin is going (unloaded): the answer is dropped, its docks are gone (#88).
        self.discarded = False
        #: The progress last shown, and when (PROGRESS_EVERY).
        self._shown = -1.0
        self._shown_at = 0.0

    # Called by the work.
    def progress(self, percent: float) -> None:
        """
        How far the work is (0–100). Shown when it moved by a whole point or
        PROGRESS_EVERY has passed: each change is a message to the main
        thread (the status bar's bar), and work that tells its progress for
        every row or line (GDAL does) sent thousands a second.
        """
        percent = max(0.0, min(100.0, percent))
        now = time.monotonic()
        if abs(percent - self._shown) < 1.0 and percent not in (0.0, 100.0) and now - self._shown_at < PROGRESS_EVERY:
            return
        if percent == self._shown:
            return
        self._shown, self._shown_at = percent, now
        self.setProgress(percent)

    def check(self) -> None:
        """Raises Cancelled when the person cancelled."""
        if self.isCanceled():
            raise Cancelled()

    def cancel(self) -> None:
        self.feedback.cancel()
        try:
            super().cancel()
        except RuntimeError:
            pass  # already ended and deleted by the task manager

    def run(self) -> bool:
        _current.task = self
        try:
            self.result = self._work(self)
            return True
        except Cancelled:
            self.error = "取消"
            return False
        except EsriError as error:
            # A service that cannot be reached or refuses: shown where it was asked (its text and code, core/errors.py),
            # logged as a line with the detail the screen leaves out (the address, the server's own words), not a warning.
            self.error = str(error) or type(error).__name__
            QgsMessageLog.logMessage(f"{self.description()}: {errors.detailed(error)}", LOG_TAG, Qgis.MessageLevel.Info)
            audit.error("task", self.description(), self.error, detail=_coded(self.error, error.detail))
            return False
        except Exception as error:  # noqa: BLE001 (any failure is shown, not raised)
            # The screen: the plugin's own words, or the code of a file error or of an unexpected one (E901) without
            # Python's English; the log: all of it with its traceback (a warning of the plugin's: in the audit log too).
            self.error, detail = errors.describe(error)
            self.error = self.error or type(error).__name__
            QgsMessageLog.logMessage(f"{self.description()}: {self.error}{'（' + detail + '）' if detail else ''}\n{traceback.format_exc()}",
                                     LOG_TAG, Qgis.MessageLevel.Warning)
            return False
        finally:
            _current.task = None

    def finished(self, ok: bool) -> None:
        started = time.perf_counter()
        try:
            self._finished(ok)
        finally:
            took = (time.perf_counter() - started) * 1000
            if took >= SLOW_MS:
                QgsMessageLog.logMessage(f"{self.description()}: 結果の表示に {took:.0f} ms（その間 QGIS の画面が止まる）",
                                         LOG_TAG, Qgis.MessageLevel.Info, notifyUser=False)

    def _finished(self, ok: bool) -> None:
        if self.discarded:
            if not ok:
                QgsMessageLog.logMessage(f"{self.description()}: プラグインの停止で取り消し（{self.error or '取消'}）",
                                         LOG_TAG, Qgis.MessageLevel.Info)
            return
        try:
            if ok:
                if self._done:
                    self._done(self.result)
            elif self._failed:
                self._failed(self.error or ("取消" if self.isCanceled() else "失敗"))
        except RuntimeError as error:
            if "has been deleted" not in str(error):
                QgsMessageLog.logMessage(f"{self.description()}: 結果の表示で失敗\n{traceback.format_exc()}", LOG_TAG, Qgis.MessageLevel.Critical)
            else:
                # The window that asked was closed meanwhile: nothing to show the answer in (#88).
                QgsMessageLog.logMessage(f"{self.description()}: 結果の表示先が閉じられていたため表示せず", LOG_TAG, Qgis.MessageLevel.Info)
        except Exception:  # noqa: BLE001 (a failing callback is logged, never taken down with QGIS)
            QgsMessageLog.logMessage(f"{self.description()}: 結果の表示で失敗\n{traceback.format_exc()}", LOG_TAG, Qgis.MessageLevel.Critical)


#: A task's result shown on the main thread for at least this long (ms) is told in the log (the screen stops meanwhile).
SLOW_MS = 200
#: A task's progress moving by less than a point is shown at most this often (s).
PROGRESS_EVERY = 0.25

#: How long a thread may keep Python's GIL while another waits for it (s), while the plugin is loaded. Python's own 0.005
#: let one task's Python work hold each of the main thread's calls into Python up to 5 ms (with four, about 25 ms).
SWITCH_INTERVAL = 0.001
#: Python's own interval before the plugin changed it (None: not changed).
_switch_before: float | None = None


def share_gil_faster() -> None:
    """While the plugin is loaded: the GIL is handed on after SWITCH_INTERVAL (never longer than it was)."""
    global _switch_before
    now = sys.getswitchinterval()
    if _switch_before is None and now > SWITCH_INTERVAL:
        _switch_before = now
        sys.setswitchinterval(SWITCH_INTERVAL)


def restore_gil_sharing() -> None:
    """The plugin is unloaded: Python's interval as it was (unless someone else changed it since)."""
    global _switch_before
    if _switch_before is not None:
        if abs(sys.getswitchinterval() - SWITCH_INTERVAL) < 1e-9:
            sys.setswitchinterval(_switch_before)
        _switch_before = None


#: While the plugin is loaded, Python's garbage collection runs after this many new objects (Python's own: 700).
#: A collection stops every thread, the main one too; reading a large answer makes hundreds of thousands of objects,
#: and with 700 Python walked through all it holds again and again meanwhile (each walk tens to hundreds of ms of a
#: still screen). Measured (two reads of 20,000 features at once): the screen's longest stops a third as long with
#: the answers read in pieces (core/json_pieces.py) and this, neither alone helping much (2026-11-18).
COLLECT_AFTER = 10_000
#: Python's thresholds before the plugin changed them (None: not changed).
_collect_before: Optional[tuple[int, ...]] = None


def collect_less_often() -> None:
    """While the plugin is loaded: garbage collected after COLLECT_AFTER new objects (never more often than it was)."""
    global _collect_before
    now = gc.get_threshold()
    if _collect_before is None and 0 < now[0] < COLLECT_AFTER:
        _collect_before = now
        gc.set_threshold(COLLECT_AFTER, *now[1:])


def restore_collection() -> None:
    """The plugin is unloaded: Python's thresholds as they were (unless someone else changed them since)."""
    global _collect_before
    if _collect_before is not None:
        if gc.get_threshold()[0] == COLLECT_AFTER:
            gc.set_threshold(*_collect_before)
        _collect_before = None


#: Running tasks are kept here so Python does not collect them early.
_running: set[FunctionTask] = set()
#: Tasks made while QGIS is still starting, started once it has (hold_tasks / release_tasks).
_held: list[FunctionTask] = []
_holding = False


def hold_tasks() -> None:
    """
    QGIS is starting (the plugin loads before the other plugins): tasks wait
    until release_tasks. A task's Python running while another plugin's
    modules are still being imported crashed QGIS (signal 11 in sip, seen
    with the catalog's 「センサーの取得」 at start, 2026-11-14).
    """
    global _holding
    _holding = True


def release_tasks() -> None:
    """QGIS has started: the tasks held start now (those cancelled meanwhile not)."""
    global _holding
    _holding = False
    held, _held[:] = list(_held), []
    for task in held:
        if task.discarded or task.status() not in (QgsTask.TaskStatus.Queued, QgsTask.TaskStatus.OnHold):
            _running.discard(task)
            continue
        QgsApplication.taskManager().addTask(task)


def run_task(description: str, work: Callable[[FunctionTask], Any], done: Optional[Callable[[Any], None]] = None,
             failed: Optional[Callable[[str], None]] = None, hidden: bool = False) -> FunctionTask:
    """
    Starts a task in QGIS's task manager (its progress shows in the status
    bar, where it can be cancelled). `hidden`: a small task run again and
    again (a poll) is kept out of the status bar and its notices.
    """
    flags = QgsTask.Flag.CanCancel
    if hidden:
        for name in ("Hidden", "Silent"):  # Hidden: QGIS 3.26+
            flag = getattr(QgsTask.Flag, name, None)
            if flag is not None:
                flags = flags | flag
    task = FunctionTask(description, work, done, failed, flags)
    _running.add(task)
    task.taskCompleted.connect(lambda: _running.discard(task))
    task.taskTerminated.connect(lambda: _running.discard(task))
    if _holding:
        _held.append(task)
    else:
        QgsApplication.taskManager().addTask(task)
    return task


def cancel_all(wait_ms: int = 5000) -> int:
    """
    Cancels the plugin's running tasks and drops their answers, waiting up to
    `wait_ms` in all for them to stop (the plugin is unloaded: its docks and
    the local store go next, #88). The number cancelled.
    """
    held = set(_held)
    _held.clear()
    for task in held:
        task.discarded = True
        _running.discard(task)
    tasks = list(_running)
    for task in tasks:
        task.discarded = True
        task.cancel()
    deadline = time.monotonic() + wait_ms / 1000
    for task in tasks:
        left = int((deadline - time.monotonic()) * 1000)
        if left <= 0:
            break
        task.waitForFinished(left)
    return len(tasks)
