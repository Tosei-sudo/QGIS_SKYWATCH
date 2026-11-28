"""
The operations of 「ミカゲと話す」 (ui/chat_dock.py, ui/chat_operation.py, features/chat.py; 文で操作 until 2026-11-28)
with a model of the test's own: no toolbar of 文で操作 any more, the dock only while the task has a model, a sentence
its words name read without the model (a question is not: it goes to the tables), another the model chooses apart from
the tables' conversation, the card's values read by code and changed by hand, nothing done before its button, the
card staying in the chat once done or left, the operations each feature offers on its docks, and the audit log's line.
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
from mikage.features.chat import Chat  # noqa: E402
from mikage.features.maps import Maps  # noqa: E402
from mikage.features.plan import Plan  # noqa: E402
from mikage.features.requests import Requests  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_requests import CONFIG, plugin  # noqa: E402,F401 (the fixture)
from mikage.tests.test_qgis_text_assist import FakeWriter  # noqa: E402
from mikage.ui.chat_dock import AnswerCard, Said  # noqa: E402
from mikage.ui.chat_operation import OperationCard  # noqa: E402

PLUGIN_FEATURES = (Plan, Requests, Maps, Chat)


def _setup(p, tmp_path, replies, task=True, extra=None):
    """The settings with a textAssist (ミカゲと話す given the model when `task`), the model replaced by FakeWriter."""
    gate = threading.Event()
    gate.set()
    loaded = []
    a = p.context.text_assist

    def writer_of(folder, threads):
        w = FakeWriter(folder, threads, replies, gate, 9.0)
        loaded.append(w)
        return w

    a._writer_of = writer_of
    a._folder_of = lambda source, base, cancelled=lambda: False: os.path.join(os.path.dirname(base), source)
    tasks = {"chat": {"model": "large", "words": {"計画ファイル出力": ["発注ファイル"]}}} if task else {"tidyNote": "large"}
    config = {**CONFIG, "textAssist": {"models": {"large": "models/qwen-9b"}, "tasks": tasks}, **(extra or {})}
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(tmp_path / "config.json"))
    p.context.reload()
    assert wait_for(lambda: a.available)
    return p.docks["SatObsDataChat"], loaded


def send(dock, text, kind=OperationCard):
    """`text` sent; the newest card of `kind` once the chat is idle."""
    before = len(dock.cards(kind))
    assert dock.send(text)
    assert wait_for(lambda: not dock.busy and len(dock.cards(kind)) > before), [type(w).__name__ for w in dock.items]
    return dock.last(kind)


@pytest.fixture
def records(monkeypatch):
    lines = []

    def record(kind, action, target="", **kw):
        if kind in ("chat", "command"):
            lines.append((kind, action, target, kw))

    monkeypatch.setattr(audit, "record", record)
    return lines


def test_the_chat_shows_only_while_the_task_has_a_model(plugin):
    p, iface, fake, tmp_path = plugin
    dock, _loaded = _setup(p, tmp_path, [], task=False)
    assert "ミカゲと話す" in p.hidden_entries and not dock.chat.offered and p.open_chat("x") is None
    # No toolbar of 文で操作 any more.
    assert iface.mainWindow().findChild(object, "MikageCommandToolbar") is None and not hasattr(p, "command_box")
    dock, _loaded = _setup(p, tmp_path, [])
    assert "ミカゲと話す" not in p.hidden_entries and dock.chat.offered and p.open_chat() is dock and dock.isVisible()
    # The operations of the features there (撮像計画・撮像依頼・地図; no 画像カタログ, no 観測記録 in this assembly).
    keys = [o.key for o in p.feature(Chat).operations()]
    assert keys == ["planFiles", "schedule", "follow", "quotas", "passes", "placeSchedule", "imagingPlan", "newRequest", "jump",
                    "openScreen"]


def test_words_choose_the_operation_and_the_button_does_it(plugin, records):
    p, iface, fake, tmp_path = plugin
    dock, loaded = _setup(p, tmp_path, [])
    plan_files = p.docks["SatObsPlanFiles"]
    called = []
    plan_files.run = lambda: called.append((plan_files.day.date(), plan_files.system_filter.currentData()))
    card = send(dock, "Aシステムの明日のオーダーファイルを出力して")
    # The sentence, then its card; no model for words.
    assert isinstance(dock.items[-2], Said) and dock.items[-2].label.text() == "Aシステムの明日のオーダーファイルを出力して"
    assert loaded == [] and dock.field.text() == ""
    r = card.chosen()
    tomorrow = p.feature(Chat).today() + timedelta(days=1)
    assert r.operation.key == "planFiles" and r.systems == ["SYS-A"] and r.period == (tomorrow, tomorrow)
    assert card.title.text() == "操作: 撮像計画ファイル出力"
    assert card.how.text() == "言葉から" and card.run_button.text() == "開いて計算" and card.run_button.isEnabled()
    assert card.system.currentData() == "SYS-A" and card.first.value() == tomorrow and not card.last.isVisibleTo(card)
    assert "書き出しは" in card.will.text()
    # Nothing done before the button.
    assert called == [] and records == []
    card.run_button.click()
    assert called == [(QDate(tomorrow.year, tomorrow.month, tomorrow.day), "SYS-A")]
    assert plan_files.isVisible()
    # The card stays in the chat, done: what was done said, its button off.
    assert card in dock.items and card.finished and not card.run_button.isEnabled() and not card.cancel_button.isVisibleTo(card)
    assert card.message == f"撮像計画ファイル出力: {tomorrow.isoformat()}・A を計算（書き出しは画面で）"
    assert card.state.text() == "実行した: " + card.message and dock.message == card.message
    # The audit log: kind command, as 文で操作's were.
    kind, action, target, kw = records[0]
    assert (kind, action, target) == ("command", "計画ファイル出力", "撮像計画ファイル出力")
    assert kw["detail"]["text"] == "Aシステムの明日のオーダーファイルを出力して" and kw["detail"]["by"] == "words"
    assert kw["detail"]["operation"] == "計画ファイル出力"
    assert kw["detail"]["systems"] == ["SYS-A"] and kw["detail"]["period"] == [tomorrow.isoformat()] * 2
    # Pressed again: nothing (done once).
    assert not card.run() and len(called) == 1
    # The settings' words name it too.
    assert send(dock, "明日の発注ファイル").chosen().operation.key == "planFiles"


def test_a_card_left_stays_as_it_was(plugin, records):
    p, iface, fake, tmp_path = plugin
    dock, _loaded = _setup(p, tmp_path, [])
    card = send(dock, "Bの計画ファイルを出力")
    assert card.chosen().period is None and card.first.text() == "（なし）" and not card.run_button.isEnabled()
    card.first.set_value(p.feature(Chat).today())
    assert card.run_button.isEnabled()
    card.cancel_button.click()
    assert card.finished and card.state.text() == "取りやめた" and not card.run_button.isEnabled() and not card.first.isEnabled()
    assert records == []


def test_a_question_goes_to_the_tables_not_to_an_operation_by_its_words(plugin):
    """文で操作's field opened 撮像予定 for 「今日の撮像予定は何件？」 (10 of the study's 30 questions went to an operation)."""
    p, iface, fake, tmp_path = plugin
    dock, loaded = _setup(p, tmp_path, ["NG: 表がない"])
    answer = send(dock, "今日の撮像予定は何件？", AnswerCard)
    assert answer.answer.question == "今日の撮像予定は何件？" and loaded and loaded[0].asked[0][0][-1]["content"] == "今日の撮像予定は何件？"
    assert not dock.cards(OperationCard)


def test_the_model_chooses_apart_from_the_tables(plugin, records):
    p, iface, fake, tmp_path = plugin
    dock, loaded = _setup(p, tmp_path, ["操作: 計画ファイル出力", "操作: なし"])
    card = send(dock, "来週月曜のB系の依頼を出力")
    asked, max_tokens, one_line = loaded[0].asked[0]
    # 文で操作's prompt and list, the sentence last; not kept (the tables' conversation stays read).
    assert asked[0]["content"].startswith("あなたは地図ソフト MIKAGE の操作を選ぶ係です") and "- 計画ファイル出力: " in asked[0]["content"]
    assert asked[-1]["content"] == "依頼: 来週月曜のB系の依頼を出力" and loaded[0].keeps == [False]
    assert one_line and max_tokens == 24
    r = card.chosen()
    today = p.feature(Chat).today()
    monday = today + timedelta(days=7 - today.weekday())
    assert r.operation.key == "planFiles" and r.systems == ["SYS-B"] and r.period == (monday, monday)
    assert card.how.text().startswith("生成 AI が選択")
    # 「なし」, and the tables cannot answer it either (no table here): a card to choose one by hand, read from the sentence.
    card = send(dock, "東京のあたりを地図に")
    assert card.reading is None and not card.run_button.isVisibleTo(card) and "合う操作も見つからず" in card.state.text()
    card.operation.setCurrentIndex(card.operation.findData("jump"))
    assert card.chosen().operation.key == "jump" and card.place.text() == "東京" and card.how.text() == "選び直し"
    assert not [r for r in records if r[0] == "command"]


def test_a_question_the_tables_cannot_answer_may_be_an_operation(plugin):
    """「今年の年間上限はあとどれくらい残ってる？」: a question; the tables say 「NG」, the model then chooses 年間上限."""
    p, iface, fake, tmp_path = plugin
    dock, loaded = _setup(p, tmp_path, ["NG: 年間上限の残りは表にない", "操作: 年間上限"])
    card = send(dock, "今年の年間上限はあとどれくらい残ってる？")
    assert card.chosen().operation.key == "quotas" and "表からは答えられないため、操作として読んだ" in card.caution.text()
    assert loaded[0].keeps == [True, False] and not dock.cards(AnswerCard)


def test_the_operations_open_their_screens(plugin, records):
    p, iface, fake, tmp_path = plugin
    dock, _loaded = _setup(p, tmp_path, [])
    # 撮像予定: the day shown.
    send(dock, "明後日の撮像予定").run_button.click()
    day = p.feature(Chat).today() + timedelta(days=2)
    schedule = p.docks["SatObsSchedule"]
    assert schedule.isVisible() and schedule.chosen_day() == day
    # 地点ごとの撮像予定: a point written.
    card = send(dock, "35.3, 139.6 のタイムラインを見たい")
    assert card.chosen().operation.key == "placeSchedule" and card.place.text() == "35.30000, 139.60000"
    assert wait_for(lambda: card.place_found is not None) and card.run_button.isEnabled()
    card.run_button.click()
    places = p.docks["SatObsPlaceSchedule"]
    assert places.isVisible() and places.clicked and places.clicked[-1].points == [[pytest.approx(139.6), pytest.approx(35.3)]]
    # 座標・地名へ移動.
    send(dock, "35.3, 139.6 へ移動").run_button.click()
    assert p.coordinates.marked is not None
    # 画面を開く: an entry of the menu (two operations' words: the model asked; it says nothing here, so the card names both).
    card = send(dock, "年間撮像上限の画面を開いて")
    assert card.reading is None and card.state.text() == "候補: 年間上限・画面を開く。下で選べば、文から値を読む"
    card.operation.setCurrentIndex(card.operation.findData("openScreen"))
    assert card.screen_choice.currentData() == "年間撮像上限…"
    card.run_button.click()
    assert p.docks["SatObsPlanFiles"].quota_window is not None and p.docks["SatObsPlanFiles"].quota_window.isVisible()
    p.docks["SatObsPlanFiles"].quota_window.close()
    assert [r[1] for r in records if r[0] == "command"] == ["撮像予定", "地点の撮像予定", "移動", "画面を開く"]


def test_the_roles_in_use_limit_the_operations(plugin, monkeypatch):
    p, iface, fake, tmp_path = plugin
    dock, _loaded = _setup(p, tmp_path, [])
    monkeypatch.setattr(p, "allows", lambda feature: feature not in ("撮像計画ファイル出力", "撮像予定"))
    keys = [o.key for o in p.feature(Chat).operations()]
    assert "planFiles" not in keys and "schedule" not in keys and "follow" not in keys and "jump" in keys
    # Its words no longer choose it.
    card = send(dock, "Aシステムの明日のオーダーファイルを出力して")
    assert card.reading is None or card.reading.operation.key != "planFiles"


def test_every_operation_has_a_feature_that_runs_it():
    """Each operation of core/text_command is offered by one feature's text_operations (or ミカゲと話す's own 画面を開く)."""
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
    config = {**CONFIG, "textAssist": {"models": {"large": "models/sarashina-3b"}, "tasks": {"chat": "large"}}}
    (tmp_path / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(tmp_path / "config.json"))
    p.context.reload()
    assert wait_for(lambda: a.configured)
    dock = p.docks["SatObsDataChat"]
    assert not a.enabled and not a.available and not a.has("chat") and not dock.chat.offered and "ミカゲと話す" in p.hidden_entries
    # Nothing is asked of the model while off.
    failed = []
    assert a.ask("chat", "x", [{"role": "user", "content": "x"}], lambda *_: None, failed.append) is None
    assert "使わない設定" in failed[0] and not a.prime("chat", [{"role": "user", "content": "x"}]) and a.writer is None
    dialog = p.open_first_setup()
    assert dialog.ai.isEnabled() and not dialog.ai.isChecked()
    dialog.ai.setChecked(True)
    dialog.accept()
    assert a.enabled and ai.text_assist_on() and dock.chat.offered and "ミカゲと話す" not in p.hidden_entries
    # And off again: the entry goes, the model is let go.
    dialog = p.open_first_setup()
    assert dialog.ai.isChecked()
    dialog.ai.setChecked(False)
    dialog.accept()
    assert not a.enabled and not dock.chat.offered and a.writer is None and "ミカゲと話す" in p.hidden_entries


def test_the_switch_waits_for_a_model_in_the_settings(plugin, monkeypatch):
    from mikage.qgis_io.prefs import ai

    monkeypatch.setattr(ai, "TEXT_ASSIST_DEFAULT", "0")
    p, iface, fake, tmp_path = plugin
    dialog = p.open_first_setup()
    # No textAssist: it can not be turned on.
    assert not dialog.ai.isEnabled() and "未設定（管理者に連絡: textAssist）" in dialog.ai.toolTip()
    dialog.reject()
