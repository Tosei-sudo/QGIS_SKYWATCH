"""
Heavy work and requests in QgsTask, so the UI never waits: a function runs
off the main thread with a progress callback and a cancel check, and its
result (or error) comes back on the main thread.
"""
from __future__ import annotations

import traceback
from typing import Any, Callable, Optional

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsTask

LOG_TAG = "衛星撮像計画・観測記録"


class Cancelled(Exception):
    """The person cancelled the task."""


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

    # Called by the work.
    def progress(self, percent: float) -> None:
        self.setProgress(max(0.0, min(100.0, percent)))

    def check(self) -> None:
        """Raises Cancelled when the person cancelled."""
        if self.isCanceled():
            raise Cancelled()

    def run(self) -> bool:
        try:
            self.result = self._work(self)
            return True
        except Cancelled:
            self.error = "取消"
            return False
        except Exception as error:  # noqa: BLE001 (any failure is shown, not raised)
            self.error = str(error) or type(error).__name__
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.MessageLevel.Warning)
            return False

    def finished(self, ok: bool) -> None:
        try:
            if ok:
                if self._done:
                    self._done(self.result)
            elif self._failed:
                self._failed(self.error or ("取消" if self.isCanceled() else "失敗"))
        except Exception:  # noqa: BLE001 (a failing callback is logged, never taken down with QGIS)
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.MessageLevel.Critical)


#: Running tasks are kept here so Python does not collect them early.
_running: set[FunctionTask] = set()


def run_task(description: str, work: Callable[[FunctionTask], Any], done: Optional[Callable[[Any], None]] = None,
             failed: Optional[Callable[[str], None]] = None) -> FunctionTask:
    """Starts a task in QGIS's task manager (its progress shows in the status bar, where it can be cancelled)."""
    task = FunctionTask(description, work, done, failed)
    _running.add(task)
    task.taskCompleted.connect(lambda: _running.discard(task))
    task.taskTerminated.connect(lambda: _running.discard(task))
    QgsApplication.taskManager().addTask(task)
    return task
