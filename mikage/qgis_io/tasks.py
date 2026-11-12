"""
Heavy work and requests in QgsTask, so the UI never waits: a function runs
off the main thread with a progress callback and a cancel check, and its
result (or error) comes back on the main thread.

The tasks' Python shares Python's GIL with QGIS's main thread, which calls
into Python for the plugin's map items, lists and events: while the plugin
is loaded, a thread hands the GIL on after 1 ms instead of Python's 5 ms
(share_gil_faster), and a result whose showing holds the main thread long
is told in the log (SLOW_MS), so what still stutters can be found.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any, Callable, Optional

from qgis.core import Qgis, QgsApplication, QgsFeedback, QgsMessageLog, QgsTask

from ..core import audit
from ..core.esri import EsriError

LOG_TAG = "MIKAGE"


class Cancelled(Exception):
    """The person cancelled the task."""


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

    # Called by the work.
    def progress(self, percent: float) -> None:
        self.setProgress(max(0.0, min(100.0, percent)))

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
            # A service that cannot be reached or refuses: shown where it was asked, logged as a line (not a warning).
            self.error = str(error) or type(error).__name__
            QgsMessageLog.logMessage(f"{self.description()}: {self.error}", LOG_TAG, Qgis.MessageLevel.Info)
            audit.error("task", self.description(), self.error)
            return False
        except Exception as error:  # noqa: BLE001 (any failure is shown, not raised)
            self.error = str(error) or type(error).__name__
            # (A warning of the plugin's: in the audit log too, with its traceback.)
            QgsMessageLog.logMessage(f"{self.description()}: {self.error}\n{traceback.format_exc()}", LOG_TAG, Qgis.MessageLevel.Warning)
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


#: Running tasks are kept here so Python does not collect them early.
_running: set[FunctionTask] = set()


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
    QgsApplication.taskManager().addTask(task)
    return task


def cancel_all(wait_ms: int = 5000) -> int:
    """
    Cancels the plugin's running tasks and drops their answers, waiting up to
    `wait_ms` in all for them to stop (the plugin is unloaded: its docks and
    the local store go next, #88). The number cancelled.
    """
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
