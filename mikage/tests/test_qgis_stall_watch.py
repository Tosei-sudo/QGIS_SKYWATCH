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


def test_what_the_main_thread_was_doing_is_named(watch):
    """2026-11-27: the line names the plugin's functions the main thread was in (here this test's), innermost first."""
    w, heard = watch

    def read_slowly():
        time.sleep(0.6)

    stop_screen(0, read_slowly)
    assert wait_for(lambda: heard, 5000)
    assert "画面の処理: test_qgis_stall_watch.read_slowly" in heard[0], heard[0]
    # Before the background work, so the cause comes first.
    assert heard[0].index("画面の処理") < heard[0].index("裏の処理")


def test_places_are_described():
    assert stall_watch.describe_places([]) == ""
    assert stall_watch.describe_places([stall_watch.QGIS_OWN] * 3).startswith("画面の処理: QGIS 自身")
    # The most seen first; a second only when seen in a quarter of the looks or more.
    assert stall_watch.describe_places(["a.f"] * 6 + ["b.g"] * 2) == "画面の処理: a.f、b.g"
    assert stall_watch.describe_places(["a.f"] * 9 + ["b.g"]) == "画面の処理: a.f"
    assert stall_watch.place_of(None) == stall_watch.QGIS_OWN


def test_place_of_a_frame_inside_and_outside_the_plugin():
    import sys

    def inner():
        return stall_watch.place_of(sys._getframe())

    def outer():
        return inner()

    place = outer()
    assert place.startswith("test_qgis_stall_watch.inner ← outer ← "), place
    # Another's code under the plugin's: named as the call the plugin is in.
    import json

    seen = []

    class Probe:
        def __repr__(self):
            seen.append(stall_watch.place_of(sys._getframe(1)))
            return "1"

    json.dumps({"a": 1}, default=str)  # (json's encoder is C: no frame of its own)
    import reprlib

    reprlib.repr(Probe())
    assert seen and "（reprlib." in seen[0], seen


def test_requests_are_reported(watch, monkeypatch):
    """通信の記録: the requests since the last line, the tables that took longest first."""
    from mikage.core import request_stats

    w, heard_stops = watch
    heard = []

    def got(message, tag, _level):
        if tag == LOG_TAG and message.startswith("通信"):
            heard.append(message)

    QgsApplication.messageLog().messageReceived.connect(got)
    try:
        base = "https://gis.example/arcgis/rest/services/Requests/FeatureServer/0"
        request_stats.STATS.note(base, {}, 0.2, 3000, name="撮像依頼台帳")
        request_stats.STATS.note(base + "/query", {"where": "1=1"}, 3.2, 6_300_000, name="撮像依頼台帳")
        request_stats.STATS.note(base + "/query", {"where": "STATUS = '有効'"}, 0.5, 20_000)
        w.report()
        assert len(heard) == 1
        assert "3 回" in heard[0] and "6.3 MB" in heard[0], heard[0]
        assert "撮像依頼台帳 検索 2 回（全件 1）計 3.7 秒・最長 3.2 秒" in heard[0], heard[0]
        # Nothing since: no line.
        w.report()
        assert len(heard) == 1
    finally:
        QgsApplication.messageLog().messageReceived.disconnect(got)
