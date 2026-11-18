"""JSON read in pieces (core/json_pieces.py): the same value and the same errors as json.loads, with the GIL handed on between elements."""
import gc
import json
import random
import sys
import threading
import time

import pytest

from mikage.core import json_pieces


@pytest.fixture
def always_pieces(monkeypatch):
    monkeypatch.setattr(json_pieces, "PIECES_FROM", 0)


def answer(n: int, vertices: int = 5) -> dict:
    """An Esri query answer of `n` polygons."""
    features = []
    for i in range(n):
        ring = [[139 + k / 100, 35 + (k % 3) / 100] for k in range(vertices)] + [[139, 35]]
        features.append({"attributes": {"OBJECTID": i, "NAME": f"画像 {i}", "CLOUD": i / 7, "NOTE": None, "OK": i % 2 == 0},
                         "geometry": {"rings": [ring]}})
    return {"objectIdFieldName": "OBJECTID", "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}],
            "features": features, "exceededTransferLimit": False, "spatialReference": {"wkid": 4326}}


SAMPLES = [
    {}, [], {"a": []}, {"a": [], "b": {}}, [[]], [{}], {"a": [1, 2, 3]}, {"a": [{"b": 1}, 2, "x", None, [3]]},
    {"features": [{"x": 1}], "more": [[1, 2], [3]]}, [{"a": 1}, {"a": 2}], {"a": "]}\"{[,"}, {"ü": "日本語", "e": 1e300},
    {"same": 1, "nested": {"features": [{"deep": [{"x": 1}]}]}}, [[{"a": 1}], [2]], {"n": -0.0, "t": True, "f": False},
]


@pytest.mark.parametrize("value", SAMPLES)
@pytest.mark.parametrize("indent", [None, 2, "\t"])
def test_same_value_as_json_loads(value, indent, always_pieces):
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    assert json_pieces.loads(text) == json.loads(text)
    spaced = " \n\t" + json.dumps(value, ensure_ascii=False, separators=(" ,\r\n ", " : ")) + " \n"
    assert json_pieces.loads(spaced) == json.loads(spaced)


def test_large_answer(always_pieces):
    text = json.dumps(answer(3000))
    assert json_pieces.loads(text) == json.loads(text)
    assert json_pieces.loads(text.encode("utf-8")) == json.loads(text)


def test_random_values(always_pieces):
    rng = random.Random(7)

    def make(depth):
        kind = rng.randrange(7 if depth < 4 else 4)
        if kind == 0:
            return rng.randrange(-5, 5)
        if kind == 1:
            return rng.choice(["", "a,b", "]", "}", '"', "画像", "\\"])
        if kind == 2:
            return rng.choice([None, True, False, 1.5])
        if kind == 3:
            return rng.random()
        if kind in (4, 5):
            return [make(depth + 1) for _ in range(rng.randrange(4))]
        return {rng.choice("abcde"): make(depth + 1) for _ in range(rng.randrange(4))}

    for _ in range(300):
        value = {"features": [make(0) for _ in range(rng.randrange(5))], "x": make(0)} if rng.random() < .5 else make(0)
        text = json.dumps(value, indent=rng.choice([None, 1]))
        assert json_pieces.loads(text) == json.loads(text)


@pytest.mark.parametrize("text", [
    "", " ", "{", "[", '{"a":1,}', '[{"a":1},]', '{"a" 1}', '{"a":1 "b":2}', '[{"a":1} {"b":2}]', '{a:1}',
    '{"a":[{"b":1}]', '[{"a":1}]]', '{"a":1}x', '[{"a":1}] [2]', '{"a":[{"b":1}}', '{"a":[{"b":1}],]}', "﻿{}",
    '{"a":[{"b":1},{"c":2}]] }', '[{"a":1}}',
])
def test_same_errors(text, always_pieces):
    with pytest.raises(ValueError):
        json.loads(text)
    with pytest.raises(json.JSONDecodeError):
        json_pieces.loads(text)


def test_small_texts_in_one_call(monkeypatch):
    called = []
    monkeypatch.setattr(json_pieces.json, "loads", lambda t: called.append(t) or {})
    json_pieces.loads('{"a": [{"b": 1}]}')
    assert called


def test_the_main_thread_is_not_held_for_the_whole_answer():
    """
    A large answer read in another thread: the main thread's Python goes on meanwhile (json.loads held it for the
    whole read). Counted as the longest stretch without a turn for this thread while the other reads. Python's
    garbage collection is left out here (it runs less often while the plugin is loaded: tasks.collect_less_often).
    """
    text = json.dumps(answer(4000, vertices=30))
    assert len(text) > json_pieces.PIECES_FROM

    def longest_wait(parse) -> float:
        started, done = threading.Event(), threading.Event()

        kept = []  # (freeing a value read takes its own while: not counted)

        def read():
            started.set()
            for _ in range(3):
                kept.append(parse(text))
            done.set()

        thread = threading.Thread(target=read)
        before = sys.getswitchinterval()
        sys.setswitchinterval(0.001)
        gc.disable()
        try:
            thread.start()
            started.wait()
            longest, last = 0.0, time.perf_counter()
            while not done.is_set():
                now = time.perf_counter()
                longest = max(longest, now - last)
                last = now
        finally:
            thread.join()
            gc.enable()
            sys.setswitchinterval(before)
        return longest

    whole, pieces = longest_wait(json.loads), longest_wait(json_pieces.loads)
    assert pieces < whole / 3, (whole, pieces)
