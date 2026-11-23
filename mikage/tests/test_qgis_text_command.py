"""
文で操作 (ui/text_command.py, features/command.py) with a model of the
test's own: the field only while the task has a model, the list read ahead
when the field is clicked, a sentence its words name read without the
model, another the model chooses (what it read kept), the card's values
read by code and changed by hand, nothing done before its button, the
operations each feature offers on its docks, and the audit log's line.
"""
import json
import os
import threading
from datetime import timedelta

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtCore import QDate  # noqa: E402

from mikage.core import audit  # noqa: E402
from mikage.core import text_command as tc  # noqa: E402
from mikage.features.command import Command  # noqa: E402
from mikage.features.maps import Maps  # noqa: E402
from mikage.features.plan import Plan  # noqa: E402
from mikage.features.requests import Requests  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_requests import CONFIG, plugin  # noqa: E402,F401 (the fixture)
from mikage.tests.test_qgis_text_assist import FakeWriter  # noqa: E402

PLUGIN_FEATURES = (Plan, Requests, Maps, Command)


def _setup(p, tmp_path, replies, task=True, extra=None):
    """The settings with a textAssist (文で操作 given the model when `task`), the model replaced by FakeWriter."""
    gate = threading.Event()
    gate.set()
    loaded = []
    a = p.context.text_assist

    def writer_of(folder, threads):
        w = FakeWriter(folder, threads, replies, gate, 3.0)
        loaded.append(w)
        return w

    a._writer_of = writer_of
    a._folder_of = lambda source, base, cancelled=lambda: False: os.path.join(os.path.dirname(base), source)
    tasks = {"command": {"model": "large", "words": {"計画ファイル出力": ["発注ファイル"]}}} if task else {"tidyNote": "large"}
    config = {**CONFIG, "textAssist": {"models": {"large": "models/sarashina-3b"}, "tasks": tasks}, **(extra or {})}
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(tmp_path / "config.json"))
    p.context.reload()
    assert wait_for(lambda: a.available)
    return p.command_box, loaded


@pytest.fixture
def records(monkeypatch):
    lines = []
    def record(kind, action, target="", **kw):
        if kind == "command":
            lines.append((kind, action, target, kw))

    monkeypatch.setattr(audit, "record", record)
    return lines


def test_the_field_shows_only_while_the_task_has_a_model(plugin):
    p, iface, fake, tmp_path = plugin
    box, _loaded = _setup(p, tmp_path, [], task=False)
    assert not box.offered and not box.toolbar.isVisibleTo(iface.mainWindow())
    assert not box.toolbar.toggleViewAction().isVisible()
    box, _loaded = _setup(p, tmp_path, [])
    assert box.offered and box.toolbar.isVisibleTo(iface.mainWindow())
    # The operations of the features there (撮像計画・撮像依頼・地図; no 画像カタログ, no 観測記録 in this assembly).
    keys = [o.key for o in p.feature(Command).operations()]
    assert keys == ["planFiles", "schedule", "follow", "quotas", "passes", "placeSchedule", "imagingPlan", "newRequest", "jump",
                    "openScreen"]


def test_words_choose_the_operation_and_the_button_does_it(plugin, records):
    p, iface, fake, tmp_path = plugin
    box, loaded = _setup(p, tmp_path, [])
    dock = p.docks["SatObsPlanFiles"]
    called = []
    dock.run = lambda: called.append((dock.day.date(), dock.system_filter.currentData()))
    box.field.setText("Aシステムの明日のオーダーファイルを出力して")
    box.submit()
    card = box.card
    assert card.isVisible() and box.asked is None and loaded == []  # no model for words
    r = card.chosen()
    tomorrow = p.feature(Command).today() + timedelta(days=1)
    assert r.operation.key == "planFiles" and r.systems == ["SYS-A"] and r.period == (tomorrow, tomorrow)
    assert card.how.text() == "言葉から" and card.run_button.text() == "開いて計算" and card.run_button.isEnabled()
    assert card.system.currentData() == "SYS-A" and card.first.value() == tomorrow and not card.last.isVisible()
    assert "書き出しは" in card.will.text()
    # Nothing done before the button.
    assert called == [] and records == []
    card.run_button.click()
    assert called == [(QDate(tomorrow.year, tomorrow.month, tomorrow.day), "SYS-A")]
    assert dock.isVisible() and not card.isVisible()
    assert box.message == f"撮像計画ファイル出力: {tomorrow.isoformat()}・A を計算（書き出しは画面で）"
    kind, action, target, kw = records[0]
    assert (kind, action, target) == ("command", "計画ファイル出力", "撮像計画ファイル出力")
    assert kw["detail"]["text"] == "Aシステムの明日のオーダーファイルを出力して" and kw["detail"]["by"] == "words"
    assert kw["detail"]["systems"] == ["SYS-A"] and kw["detail"]["period"] == [tomorrow.isoformat()] * 2
    # The settings' words name it too.
    box.submit("明日の発注ファイル")
    assert card.chosen().operation.key == "planFiles"
    card.reject()


def test_a_value_not_said_keeps_the_button_off_until_given(plugin):
    p, iface, fake, tmp_path = plugin
    box, _loaded = _setup(p, tmp_path, [])
    box.submit("Bの計画ファイルを出力")
    card = box.card
    assert card.chosen().period is None and card.first.text() == "（なし）" and not card.run_button.isEnabled()
    card.first.set_value(p.feature(Command).today())
    assert card.run_button.isEnabled()
    card.reject()


def test_the_model_chooses_from_the_list_read_ahead(plugin, records):
    p, iface, fake, tmp_path = plugin
    box, loaded = _setup(p, tmp_path, ["操作: 計画ファイル出力", "操作: なし"])
    a = p.context.text_assist
    # Clicked: the model loaded and the list read while the sentence is typed.
    box.prime()
    assert wait_for(lambda: loaded and loaded[0].primed_with and not a.busy)
    primed = loaded[0].primed_with[0]
    assert primed[-1] == {"role": "user", "content": "依頼: "} and "- 計画ファイル出力: " in primed[0]["content"]
    # Not again while it is ready.
    assert not a.prime("command", primed)
    box.submit("来週月曜のB系の依頼を出力")
    assert wait_for(lambda: box.asked is None and box.card.reading is not None)
    asked, max_tokens, one_line = loaded[0].asked[0]
    # The same conversation up to the sentence (what was read ahead), kept for the next.
    assert asked[:-1] == primed[:-1] and asked[-1]["content"] == "依頼: 来週月曜のB系の依頼を出力" and loaded[0].kept
    assert one_line and max_tokens == 24
    card = box.card
    r = card.chosen()
    today = p.feature(Command).today()
    monday = today + timedelta(days=7 - today.weekday())
    assert r.operation.key == "planFiles" and r.systems == ["SYS-B"] and r.period == (monday, monday)
    assert card.how.text().startswith("生成 AI が選択")
    # 「なし」: no operation; one chosen by hand is read from the sentence.
    box.submit("今日の東京の天気は？")
    assert wait_for(lambda: box.asked is None and card.operation_row.isVisible())
    assert card.reading is None and not card.run_button.isVisible() and "合う操作が見つからず" in card.state.text()
    card.operation.setCurrentIndex(card.operation.findData("jump"))
    assert card.chosen().operation.key == "jump" and card.place.text() == "東京" and card.how.text() == "選び直し"
    card.reject()
    assert records == []


def test_an_ask_made_while_the_list_is_read_waits_for_it(plugin):
    p, iface, fake, tmp_path = plugin
    gate = threading.Event()
    box, loaded = _setup(p, tmp_path, ["操作: 撮像予定"])
    a = p.context.text_assist
    original = a._writer_of

    def slow(folder, threads):
        gate.wait(5)
        return original(folder, threads)

    a._writer_of = slow
    box.prime()
    assert a.priming
    box.submit("来週の分を一覧で見たい")
    assert box.asked is not None and box.card.state.isVisible()
    gate.set()
    assert wait_for(lambda: box.asked is None and box.card.reading is not None)
    assert box.card.chosen().operation.key == "schedule" and len(loaded[0].asked) == 1
    box.card.reject()


def test_the_operations_open_their_screens(plugin, records):
    p, iface, fake, tmp_path = plugin
    box, _loaded = _setup(p, tmp_path, [])
    card = box.card
    # 撮像予定: the day shown.
    box.submit("明後日の撮像予定")
    day = p.feature(Command).today() + timedelta(days=2)
    card.run_button.click()
    schedule = p.docks["SatObsSchedule"]
    assert schedule.isVisible() and schedule.chosen_day() == day
    # 地点ごとの撮像予定: a point written.
    box.submit("35.3, 139.6 のタイムラインを見たい")
    assert card.chosen().operation.key == "placeSchedule" and card.place.text() == "35.30000, 139.60000"
    assert wait_for(lambda: card.place_found is not None) and card.run_button.isEnabled()
    card.run_button.click()
    places = p.docks["SatObsPlaceSchedule"]
    assert places.isVisible() and places.clicked and places.clicked[-1].points == [[pytest.approx(139.6), pytest.approx(35.3)]]
    # 座標・地名へ移動.
    box.submit("35.3, 139.6 へ移動")
    card.run_button.click()
    assert p.coordinates.marked is not None
    # 画面を開く: an entry of the menu.
    box.submit("年間撮像上限の画面を開いて")
    # Two operations' words (年間上限, 画面を開く): the model asked; it says nothing here, so the card names both.
    assert wait_for(lambda: box.asked is None and card.operation_row.isVisible())
    assert card.reading is None and card.state.text() == "候補: 年間上限・画面を開く。下で選べば、文から値を読む"
    card.operation.setCurrentIndex(card.operation.findData("openScreen"))
    assert card.screen_choice.currentData() == "年間撮像上限…"
    card.run_button.click()
    assert p.docks["SatObsPlanFiles"].quota_window is not None and p.docks["SatObsPlanFiles"].quota_window.isVisible()
    p.docks["SatObsPlanFiles"].quota_window.close()
    assert [r[1] for r in records] == ["撮像予定", "地点の撮像予定", "移動", "画面を開く"]


def test_the_roles_in_use_limit_the_operations(plugin, monkeypatch):
    p, iface, fake, tmp_path = plugin
    box, _loaded = _setup(p, tmp_path, [])
    monkeypatch.setattr(p, "allows", lambda feature: feature not in ("撮像計画ファイル出力", "撮像予定"))
    keys = [o.key for o in p.feature(Command).operations()]
    assert "planFiles" not in keys and "schedule" not in keys and "follow" not in keys and "jump" in keys
    # Its words no longer choose it.
    box.submit("Aシステムの明日のオーダーファイルを出力して")
    assert box.card.reading is None or box.card.reading.operation.key != "planFiles"
    box.cancel()
    box.card.reject()


def test_every_operation_has_a_feature_that_runs_it():
    """Each operation of core/text_command is offered by one feature's text_operations (or 文で操作's own 画面を開く)."""
    import inspect

    from mikage import plugin as plugin_module

    sources = "".join(inspect.getsource(k) for k in plugin_module.FEATURES)
    for op in tc.OPERATIONS:
        assert op.key == "openScreen" or f'"{op.key}"' in sources, op.key


def test_the_generative_ai_is_opt_in(plugin, monkeypatch):
    """Off for a new person (tsudo 2026-11-21: 「生成AIの機能はオプトインにしよう」): on from 担当と個人設定, at once."""
    from mikage.qgis_io.prefs import ai

    monkeypatch.setattr(ai, "TEXT_ASSIST_DEFAULT", "0")
    p, iface, fake, tmp_path = plugin
    a = p.context.text_assist
    gate = threading.Event()
    gate.set()
    a._writer_of = lambda folder, threads: FakeWriter(folder, threads, [], gate, 3.0)
    a._folder_of = lambda source, base, cancelled=lambda: False: os.path.join(os.path.dirname(base), source)
    config = {**CONFIG, "textAssist": {"models": {"large": "models/sarashina-3b"}, "tasks": {"command": "large"}}}
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(tmp_path / "config.json"))
    p.context.reload()
    assert wait_for(lambda: a.configured)
    box = p.command_box
    assert not a.enabled and not a.available and not a.has("command") and not box.offered
    assert not box.toolbar.isVisibleTo(iface.mainWindow())
    # Nothing is asked of the model while off.
    failed = []
    assert a.ask("command", "x", [{"role": "user", "content": "x"}], lambda *_: None, failed.append) is None
    assert "使わない設定" in failed[0] and not a.prime("command", [{"role": "user", "content": "x"}]) and a.writer is None
    dialog = p.open_first_setup()
    assert dialog.ai.isEnabled() and not dialog.ai.isChecked()
    dialog.ai.setChecked(True)
    dialog.accept()
    assert a.enabled and ai.text_assist_on() and box.offered and box.toolbar.isVisibleTo(iface.mainWindow())
    # And off again: the buttons go, the model is let go.
    dialog = p.open_first_setup()
    assert dialog.ai.isChecked()
    dialog.ai.setChecked(False)
    dialog.accept()
    assert not a.enabled and not box.offered and a.writer is None


def test_the_switch_waits_for_a_model_in_the_settings(plugin, monkeypatch):
    from mikage.qgis_io.prefs import ai

    monkeypatch.setattr(ai, "TEXT_ASSIST_DEFAULT", "0")
    p, iface, fake, tmp_path = plugin
    dialog = p.open_first_setup()
    # No textAssist: it can not be turned on.
    assert not dialog.ai.isEnabled() and "未設定（管理者に連絡: textAssist）" in dialog.ai.toolTip()
    dialog.reject()
