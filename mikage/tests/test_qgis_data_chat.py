"""
「データに質問」 (ui/data_chat_dock.py, qgis_io/data_chat_io.py) with a model of
the test's own and Esri tables in memory: the entry only while the task has
a model, a question answered (the model's SQL run by 「データ分析」's engine,
records deleted left out and the place joined on the server's side, the
sentence checked against the table), a sentence that does not match
replaced, an SQL that fails given back once, a question the tables cannot
answer, a follow-up, 「データ分析で開く」, the audit log's line, 文で操作's
field handing a question over, and a model on a server.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings  # noqa: E402

from mikage.core import audit  # noqa: E402
from mikage.core.esri import EsriClient, EsriError  # noqa: E402
from mikage.core.text_assist import TextModel  # noqa: E402
from mikage.features.analysis import Analysis  # noqa: E402
from mikage.features.command import Command  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.ai_runtime import AiError  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.qgis_io.text_assist_io import ServerWriter, no_thinking, reply_text, without_thinking  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_qgis_text_assist import FakeWriter  # noqa: E402

OBS = "https://example.com/arcgis/rest/services/Obs/FeatureServer/0"
AREAS = "https://example.com/arcgis/rest/services/Places/FeatureServer/0"
S, D, I = "esriFieldTypeString", "esriFieldTypeDate", "esriFieldTypeInteger"
SQL = ("SELECT COUNT(*) FROM TBL_OBSERVATION WHERE PLACE_NAME = '舞鶴' AND CONFIDENCE = '確定' "
       "AND IMAGE_TIME >= '2026-10-01' AND IMAGE_TIME < '2026-11-01'")


def _ms(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000


def _config(tmp_path, text_assist):
    config = {"observationStores": [{"label": "共有", "url": OBS}],
              "alertReports": {"namedAreas": AREAS, "formats": [{"name": "詳細", "delimiter": ","}]}}
    if text_assist is not None:
        config["textAssist"] = text_assist
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsProject.instance().clear()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    _config(tmp_path, {"models": {"pc": "models/qwen"}, "tasks": {"dataChat": "pc"}})
    fake = FakeEsri()
    obs = fake.add_layer(OBS, FakeLayer({"OBS_ID": S, "IMAGE_ID": S, "IMAGE_TIME": D, "OBJECT_ID": S, "KIND": S, "ACTIVITY": S,
                                         "CONFIDENCE": S, "OBSERVER": S, "CREATED_AT": D, "UPDATED_AT": D, "DELETED_AT": D,
                                         "NAMED_AREA_OBJECTID": I}))
    areas = fake.add_layer(AREAS, FakeLayer({"NAME": S, "KIND": S, "ACTIVE": I}, "esriGeometryPolygon"))
    for name in ("呉", "舞鶴"):
        areas.add({"NAME": name, "KIND": "港", "ACTIVE": 1})
    for i, (kind, conf, when, deleted, place) in enumerate([
            ("船舶 > 艦艇 > 護衛艦", "確定", "2026-10-03T12:00:00", None, 2), ("船舶 > タグボート", "確定", "2026-10-05T12:00:00", None, 2),
            ("船舶 > 艦艇 > 護衛艦", "推定", "2026-10-06T12:00:00", None, 2), ("船舶 > 艦艇 > 護衛艦", "確定", "2026-10-07T12:00:00", "2026-10-08T00:00:00", 2),
            ("航空機 > 戦闘機", "確定", "2026-10-09T12:00:00", None, 1), ("船舶 > タグボート", "確定", "2026-11-02T12:00:00", None, 2)]):
        obs.add({"OBS_ID": f"o{i}", "IMAGE_ID": f"img{i}", "IMAGE_TIME": _ms(when), "OBJECT_ID": f"m{i}", "KIND": kind, "ACTIVITY": "停泊",
                 "CONFIDENCE": conf, "OBSERVER": "山田", "CREATED_AT": _ms(when), "UPDATED_AT": _ms(when),
                 "DELETED_AT": _ms(deleted) if deleted else None, "NAMED_AREA_OBJECTID": place}, {"x": 135.3, "y": 35.5})
    iface = FakeIface()
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p = small_plugin(iface, Analysis, Command)
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, fake, tmp_path
    p.unload()
    QgsProject.instance().clear()
    QgsSettings().remove("mikage")


def _model(p, replies, params=9.0):
    """The model replaced by FakeWriter (its replies in turn)."""
    gate = threading.Event()
    gate.set()
    loaded = []
    a = p.context.text_assist
    a.unload()

    def writer_of(folder, threads):
        w = FakeWriter(folder, threads, replies, gate, params)
        loaded.append(w)
        return w

    a._writer_of = writer_of
    a._folder_of = lambda source, base, cancelled=lambda: False: os.path.join(os.path.dirname(base), source)
    assert wait_for(lambda: a.has("dataChat"))
    return loaded


@pytest.fixture
def records(monkeypatch):
    lines = []

    def record(kind, action, target="", **kw):
        if kind == "chat":
            lines.append((kind, action, target, kw))

    monkeypatch.setattr(audit, "record", record)
    return lines


def _ask(dock, question):
    before = len(dock.cards)
    assert dock.ask(question)
    assert wait_for(lambda: len(dock.cards) > before and not dock.chat.busy)
    return dock.cards[0].answer


def _queries(fake, url):
    return [form for u, form in fake.asked if u == url + "/query" and "returnDistinctValues" not in form]


def test_the_entry_shows_only_while_the_task_has_a_model(plugin):
    p, _iface, _fake, tmp_path = plugin
    assert "データに質問" not in p.hidden_entries and p.allows("データに質問")
    _config(tmp_path, {"models": {"pc": "models/qwen"}, "tasks": {"command": "pc"}})
    p.context.reload()
    assert wait_for(lambda: "データに質問" in p.hidden_entries)
    dock = p.docks["SatObsDataChat"]
    assert not dock.chat.offered and not dock.field.isEnabled() and not dock.ask("何件？")
    assert p.open_data_chat("何件？") is None
    # The person turned the generative AI off: the same.
    _config(tmp_path, {"models": {"pc": "models/qwen"}, "tasks": {"dataChat": "pc"}})
    p.context.reload()
    assert wait_for(lambda: "データに質問" not in p.hidden_entries)
    p.context.text_assist.set_enabled(False)
    assert "データに質問" in p.hidden_entries
    p.context.text_assist.set_enabled(True)
    assert "データに質問" not in p.hidden_entries


def test_a_question_answered_from_the_tables(plugin, records):
    p, _iface, fake, _tmp = plugin
    loaded = _model(p, [SQL, "先月の舞鶴の確定の記録は 2 件でした。"])
    dock = p.open_data_chat()
    answer = _ask(dock, "先月の舞鶴の記録で、確定は何件？")
    assert answer.refused == "" and answer.by_model and answer.sentence == "先月の舞鶴の確定の記録は 2 件でした。"
    # Two records: the one deleted and the 推定 one left out, 呉's and November's too.
    assert answer.result.rows == [(2,)]
    assert "JOIN TBL_NAMED_AREA _place ON TBL_OBSERVATION.NAMED_AREA_OBJECTID = _place.OBJECTID" in answer.ran
    assert answer.counts.keys() == {"TBL_OBSERVATION", "TBL_NAMED_AREA"}
    assert answer.conditions == {"TBL_OBSERVATION": "DELETED_AT IS NULL"}
    # The server was asked for the place's area only, and for the records with the SQL's conditions, deleted ones left out.
    area_where = [f["where"] for f in _queries(fake, AREAS)]
    assert any("NAME = '舞鶴'" in w for w in area_where)
    obs_where = [f["where"] for f in _queries(fake, OBS)]
    assert obs_where and all("DELETED_AT IS NULL" in w and "CONFIDENCE = '確定'" in w for w in obs_where), obs_where
    # What the model was told: the tables with their values read from them, then the question.
    sql_conversation, max_tokens, _one = loaded[0].asked[0]
    system = sql_conversation[0]["content"]
    assert "PLACE_NAME 記録の地名（呉・舞鶴）" in system and "CONFIDENCE 確度（推定・確定）" in system
    assert "KIND 種別のパス（例 '航空機 > 戦闘機' '船舶 > 艦艇 > 護衛艦' '船舶 > タグボート'。大分類は KIND LIKE '航空機%'）" in system
    assert "DELETED_AT" not in system
    assert sql_conversation[-1] == {"role": "user", "content": "先月の舞鶴の記録で、確定は何件？"} and max_tokens == 400
    # The SQL conversation kept read (the next question reads only itself), the answer's not.
    assert loaded[0].keeps == [True, False]
    answer_conversation, answer_tokens, _one = loaded[0].asked[1]
    assert answer_tokens == 240 and "結果の表（1 行）:\n件数\n2" not in answer_conversation[1]["content"]
    assert answer_conversation[1]["content"].endswith("COUNT(*)\n2")
    # The card: the sentence, the table (件数), how it was found.
    card = dock.cards[0]
    assert card.sentence.text() == answer.sentence and "生成 AI（表と照合済み）" in card.how.text()
    assert card.table.horizontalHeaderItem(0).text() == "件数" and card.table.item(0, 0).text() == "2"
    assert "SQL（生成 AI が書いたもの）" in card.detail.toPlainText() and "読んだ表: TBL_OBSERVATION" in card.detail.toPlainText()
    # The audit log: the question, the SQL, the tables' rows; nothing of what they hold.
    (kind, action, target, kw), = records
    assert (kind, action, target) == ("chat", "question", "先月の舞鶴の記録で、確定は何件？")
    assert kw["detail"]["sql"] == answer.ran and kw["detail"]["model"] == "pc" and kw["detail"]["sentence"] == "model"
    assert kw["count"] == 1 and set(kw["detail"]["tables"]) == {"TBL_OBSERVATION", "TBL_NAMED_AREA"}


def test_a_sentence_that_does_not_match_the_table_is_replaced(plugin):
    p, _iface, _fake, _tmp = plugin
    _model(p, [SQL, "先月の舞鶴の確定の記録は 5 件でした。"])
    dock = p.open_data_chat()
    answer = _ask(dock, "先月の舞鶴の記録で、確定は何件？")
    assert not answer.by_model and answer.sentence == "2 件でした。"
    assert any("表にない数 5" in n for n in answer.notes)
    assert "プログラム" in dock.cards[0].how.text()


def test_an_sql_that_fails_is_given_back_once(plugin):
    p, _iface, _fake, _tmp = plugin
    loaded = _model(p, ["SELECT NOPE FROM TBL_OBSERVATION", "SELECT COUNT(*) FROM TBL_OBSERVATION", "4 件でした。"])
    dock = p.open_data_chat()
    answer = _ask(dock, "記録は何件？")
    assert answer.result.rows == [(5,)] and not answer.by_model and answer.sentence == "5 件でした。"
    assert any("1 回目の SQL がエラー" in n and "NOPE" in n for n in answer.notes)
    retry = loaded[0].asked[1][0]
    assert retry[-2] == {"role": "assistant", "content": "SELECT NOPE FROM TBL_OBSERVATION"} and "NOPE" in retry[-1]["content"]
    # Twice wrong: said, no table.
    _model(p, ["SELECT * FROM TBL_SATELLITE_OUTAGE", "SELECT * FROM TBL_SATELLITE_OUTAGE"])
    answer = _ask(dock, "障害中の衛星は？")
    assert answer.result is None and "つながっていない表: TBL_SATELLITE_OUTAGE" in answer.refused
    assert dock.cards[0].table is None


def test_a_question_the_tables_cannot_answer(plugin, records):
    p, _iface, _fake, _tmp = plugin
    _model(p, ["NG: 天気のデータはつながっていません"])
    dock = p.open_data_chat()
    answer = _ask(dock, "明日の舞鶴の天気は？")
    assert answer.result is None and answer.refused == "この質問には表から答えられない（生成 AI: 天気のデータはつながっていません）"
    assert dock.cards[0].sentence.text() == answer.refused and not dock.cards[0].buttons
    assert records[-1][3]["result"] == "error" and records[-1][3]["detail"]["refused"] == answer.refused


def test_a_follow_up_and_a_new_conversation(plugin):
    p, _iface, _fake, _tmp = plugin
    second = SQL.replace("'舞鶴'", "'呉'")
    loaded = _model(p, [SQL, "2 件でした。", second, "1 件でした。", SQL, "2 件でした。"])
    dock = p.open_data_chat()
    _ask(dock, "先月の舞鶴の記録で、確定は何件？")
    answer = _ask(dock, "では呉は？")
    assert answer.result.rows == [(1,)]
    follow = loaded[0].asked[2][0]
    assert follow[-3:] == [{"role": "user", "content": "先月の舞鶴の記録で、確定は何件？"}, {"role": "assistant", "content": SQL},
                           {"role": "user", "content": "では呉は？"}]
    # The instructions the same as before: read once (kept), only what follows read again.
    assert follow[0] == loaded[0].asked[0][0][0]
    dock.new_conversation()
    _ask(dock, "先月の舞鶴の記録で、確定は何件？")
    assert loaded[0].asked[4][0][-2]["role"] == "assistant" and loaded[0].asked[4][0][-2]["content"].startswith("NG:")


def test_the_table_opened_in_data_analysis_saved_and_copied(plugin, tmp_path):
    p, _iface, _fake, _tmp = plugin
    _model(p, ["SELECT PLACE_NAME, KIND FROM TBL_OBSERVATION WHERE IMAGE_TIME < '2026-11-01' ORDER BY PLACE_NAME, KIND",
               "呉と舞鶴に記録がありました。"])
    dock = p.open_data_chat()
    answer = _ask(dock, "10 月までの記録の地名と種別")
    assert answer.result.columns == ["PLACE_NAME", "KIND"] and len(answer.result.rows) == 4
    card = dock.cards[0]
    assert [card.table.horizontalHeaderItem(i).text() for i in range(2)] == ["地名", "種別のパス"]
    card.buttons["analysis"].click()
    analysis = p.docks["SatObsAnalysis"]
    assert analysis.isVisible() and analysis.sql.toPlainText() == answer.ran
    assert analysis.conditions["TBL_OBSERVATION"] == "DELETED_AT IS NULL"
    assert wait_for(lambda: analysis.run() or True) and wait_for(lambda: analysis.result is not None and analysis.task is None)
    assert analysis.result.rows == answer.result.rows
    path = dock.save_csv(answer, str(tmp_path / "chat.csv"))
    assert open(path, encoding="utf-8-sig").read().splitlines()[0] == "PLACE_NAME,KIND"
    dock.copy(answer)
    from qgis.PyQt.QtWidgets import QApplication

    assert QApplication.clipboard().text().splitlines()[0] == "PLACE_NAME\tKIND"


def test_the_command_field_hands_a_question_over(plugin):
    p, iface, _fake, _tmp = plugin
    _model(p, [SQL, "2 件でした。"])
    box = p.command_box
    # 文で操作 has no model here: the field shows for データに質問.
    assert box.offered and box.toolbar.isVisibleTo(iface.mainWindow())
    assert "先月の舞鶴の記録は何件？" in box.field.placeholderText()
    box.submit("先月の舞鶴の記録で、確定は何件？")
    dock = p.docks["SatObsDataChat"]
    assert dock.isVisible() and wait_for(lambda: len(dock.cards) == 1 and not dock.chat.busy)
    assert dock.cards[0].answer.result.rows == [(2,)] and box.field.text() == ""


def test_a_model_on_a_server(plugin, tmp_path):
    p, _iface, _fake, _tmp = plugin
    _config(tmp_path, {"models": {"gpu": {"url": "https://llm.example.local/v1", "model": "qwen3.8-27b"}}, "tasks": {"dataChat": "gpu"}})
    p.context.reload()
    a = p.context.text_assist
    assert wait_for(lambda: a.on_server("dataChat"))
    gate = threading.Event()
    gate.set()
    made = []

    def server_of(model):
        made.append(model)
        return FakeWriter("", 0, [SQL, "2 件でした。"], gate)

    a._server_of = server_of
    a._folder_of = lambda *_a, **_k: pytest.fail("a server's model is not loaded")
    dock = p.open_data_chat()
    answer = _ask(dock, "先月の舞鶴の記録で、確定は何件？")
    assert answer.by_model and answer.result.rows == [(2,)]
    assert made[0].remote == "qwen3.8-27b" and made[0].on_server
    # Nothing is read ahead on this computer for a server.
    assert not a.prime("dataChat", [{"role": "system", "content": "x"}])


def test_the_server_writer_asks_and_reads_the_stream():
    sent = []

    def send(url, body, authcfg, limit_ms):
        sent.append((url, body, authcfg, limit_ms))
        return ("data: " + json.dumps({"choices": [{"delta": {"content": "SELECT 1 "}}]}) + "\n\n"
                + "data: " + json.dumps({"choices": [{"delta": {"content": "FROM T"}}]}) + "\n\ndata: [DONE]\n\n").encode("utf-8")

    w = ServerWriter(TextModel("gpu", "", 0, "https://llm.local/v1/", "qwen", 90.0), send, lambda _u: "auth1")
    shown = []
    assert w.write([{"role": "user", "content": "q"}], 300, wrote=shown.append) == "SELECT 1 FROM T" and shown == ["SELECT 1 FROM T"]
    url, body, authcfg, limit = sent[0]
    assert url == "https://llm.local/v1/chat/completions" and authcfg == "auth1" and limit == 90_000
    assert body == {"messages": [{"role": "user", "content": "q"}], "max_tokens": 300, "temperature": 0, "stream": True,
                    "chat_template_kwargs": {"enable_thinking": False}, "model": "qwen"}
    # One JSON object (a server that does not stream), and its thinking left out.
    assert reply_text(json.dumps({"choices": [{"message": {"content": "<think>x</think>答え"}}]}).encode()) == "<think>x</think>答え"
    assert without_thinking("<think>x</think>\n答え") == "答え" and without_thinking("<think>まだ") == ""

    def down(*_a):
        raise EsriError("接続不可")

    with pytest.raises(AiError, match="E611"):
        ServerWriter(TextModel("gpu", "", 0, "https://llm.local/v1"), down, lambda _u: None).write([], 10)
    with pytest.raises(AiError, match="E611"):
        reply_text(b'{"error": {"message": "model not found"}}')


def test_a_template_that_thinks_is_told_not_to():
    assert no_thinking("<|im_start|>assistant\n<think>\n") == "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    assert no_thinking("<|im_start|>assistant\n") == "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    assert no_thinking("<|im_start|>assistant\n<think>\n\n</think>\n\n").count("<think>") == 1
    assert no_thinking("<|assistant|>") == "<|assistant|>"
