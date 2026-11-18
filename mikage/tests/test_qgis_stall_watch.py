"""「画面が止まった」 in the log (qgis_io/stall_watch.py): how long the main thread stopped, the work running then, the part that was Python's garbage collection."""
import gc
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt.QtCore import QTimer  # noqa: E402

from mikage.qgis_io import stall_watch  # noqa: E402
from mikage.qgis_io.stall_watch import StallWatch  # noqa: E402
from mikage.qgis_io.tasks import LOG_TAG, run_task  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402


@pytest.fixture
def watch(monkeypatch):
    monkeypatch.setattr(StallWatch, "ENABLED", True)
    heard = []

    def got(message, tag, _level):
        if tag == LOG_TAG and message.startswith("画面が "):
            heard.append(message)

    QgsApplication.messageLog().messageReceived.connect(got)
    w = StallWatch()
    w.start()
    yield w, heard
    w.stop()
    QgsApplication.messageLog().messageReceived.disconnect(got)


def stop_screen(seconds, then=None):
    """The main thread busy for `seconds` (as a slow result shown, or a long collection)."""
    QTimer.singleShot(0, lambda: (time.sleep(seconds) if then is None else then()))


def test_a_stop_is_told_with_the_work_running(watch):
    w, heard = watch
    ended = []

    def busy(task):
        while not ended:
            task.check()
            time.sleep(0.01)

    run_task("長い読み込み", busy, ended.append, ended.append)
    wait_for(lambda: False, 300)
    stop_screen(0.6)
    try:
        assert wait_for(lambda: heard, 5000)
    finally:
        ended.append(1)
    assert heard[0].startswith("画面が 0.") and "裏の処理: 長い読み込み" in heard[0]
    assert "メモリ整理" not in heard[0]
    assert w.told[0][0] >= stall_watch.STALL_MS


def test_short_turns_are_not_told(watch):
    w, heard = watch
    stop_screen(0.05)
    wait_for(lambda: False, 600)
    assert heard == []


def test_garbage_collection_is_named(watch, monkeypatch):
    w, heard = watch
    monkeypatch.setattr(stall_watch, "STALL_MS", 40)
    many = [[i] for i in range(1_500_000)]

    def collect():
        gc.collect()
        gc.collect()

    stop_screen(0, collect)
    try:
        assert wait_for(lambda: heard, 5000)
    finally:
        del many
    assert "Python のメモリ整理" in heard[0], heard


def test_stops_close_together_are_told_once(watch, monkeypatch):
    w, heard = watch
    stop_screen(0.4)
    assert wait_for(lambda: len(heard) == 1, 5000)
    wait_for(lambda: False, 200)
    stop_screen(0.4)
    wait_for(lambda: False, 800)
    assert len(heard) == 1
    # The next line (after QUIET_S) counts the one held.
    w._told_at -= stall_watch.QUIET_S
    stop_screen(0.4)
    assert wait_for(lambda: len(heard) == 2, 5000)
    assert "にも 1 回" in heard[1]
