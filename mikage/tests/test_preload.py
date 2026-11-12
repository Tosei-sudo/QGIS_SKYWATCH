"""「先読み」 without QGIS: the settings file's `preload` and the startup's timings (core/preload.py)."""
from mikage.core.config import parse_config
from mikage.core.preload import ITEMS, StartupClock, preload_of


def test_preload_key():
    assert parse_config({})[0].preload == tuple(ITEMS)
    assert parse_config({"preload": False})[0].preload == ()
    assert parse_config({"preload": True})[0].preload == tuple(ITEMS)
    config, problems = parse_config({"preload": ["events", "requests", "events"]})
    assert config.preload == ("events", "requests") and problems == []


def test_preload_key_mistakes():
    problems: list = []
    assert preload_of(["requests", "catalog", 3], problems) == ("requests",)
    assert len(problems) == 2 and "catalog" in problems[0]
    problems = []
    assert preload_of("yes", problems) == tuple(ITEMS) and problems
    # 保存した撮像計画 is gone (2026-11-09): an older file's savedPlans is just skipped.
    problems = []
    assert preload_of(["savedPlans", "requests"], problems) == ("requests",) and problems == []


def test_clock():
    now = [10.0]
    clock = StartupClock(lambda: now[0])
    now[0] = 10.5
    clock.step("プラグインの読み込み")
    now[0] = 20.0
    clock.lap()  # QGIS loaded others meanwhile
    now[0] = 20.25
    clock.step("ドックの作成")
    clock.step("設定ファイルの読み込み", 1.5)
    clock.read("撮像依頼", 0.75, True)
    clock.read("イベント", 3.0, False, "接続できない")
    times = clock.times()
    assert times["steps"] == {"プラグインの読み込み": 0.5, "ドックの作成": 0.25, "設定ファイルの読み込み": 1.5}
    assert times["total"] == 2.25
    assert times["reads"]["イベント"] == {"seconds": 3.0, "ok": False, "message": "接続できない"}
    assert clock.report() == ("起動 2.25 秒（プラグインの読み込み 0.50 秒、ドックの作成 0.25 秒、設定ファイルの読み込み 1.50 秒）。"
                              "先読み: 撮像依頼 0.75 秒、イベント 3.00 秒（失敗: 接続できない）")
