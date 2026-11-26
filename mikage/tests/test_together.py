"""同時に読む (core/together.py): independent reads at once, their answers in order, an error raised as reading in turn would."""
import threading
import time

import pytest

from mikage.core import together
from mikage.core.together import read_together


def test_reads_go_at_once_and_answer_in_order():
    started = time.perf_counter()

    def slow(value):
        def read():
            time.sleep(0.2)
            return value
        return read

    assert read_together(slow(1), slow(2), slow(3)) == [1, 2, 3]
    assert time.perf_counter() - started < 0.45
    assert read_together() == [] and read_together(lambda: 5) == [5]


def test_at_most_limit_at_once():
    now = [0, 0]
    lock = threading.Lock()

    def read():
        with lock:
            now[0] += 1
            now[1] = max(now[1], now[0])
        time.sleep(0.05)
        with lock:
            now[0] -= 1

    read_together(*([read] * 9), limit=3)
    assert now[1] == 3


def test_the_first_error_in_order_after_all_ended():
    ended = []

    def fails(text, wait):
        def read():
            time.sleep(wait)
            ended.append(text)
            raise ValueError(text)
        return read

    with pytest.raises(ValueError, match="first"):
        read_together(fails("first", 0.1), lambda: ended.append("ok"), fails("second", 0.0))
    assert sorted(ended) == ["first", "ok", "second"]


def test_what_the_starting_thread_carries(monkeypatch):
    here = threading.local()
    monkeypatch.setattr(together, "_CARRIED", [(lambda: getattr(here, "task", None), lambda t: setattr(here, "task", t))])
    here.task = "the task"
    seen = read_together(lambda: getattr(here, "task", None), lambda: getattr(here, "task", None))
    assert seen == ["the task", "the task"]
    assert here.task == "the task"


def test_a_tasks_reads_carry_it():
    """qgis_io.tasks hands each read its task: a request in it stops when the task is cancelled (network.current_feedback)."""
    pytest.importorskip("qgis.core")
    from mikage.qgis_io import tasks

    class Task:
        feedback = "feedback"

    tasks._current.task = Task()
    try:
        assert read_together(tasks.current_feedback, tasks.current_feedback) == ["feedback", "feedback"]
    finally:
        tasks._current.task = None
