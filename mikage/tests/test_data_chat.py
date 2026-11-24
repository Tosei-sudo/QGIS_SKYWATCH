"""
データに質問 (core/data_chat.py): the settings (a server's model, the task's
own), what the model is told of the tables (real names, values, the place
column, deleted records left out), the dates, the SQL's check, PLACE_NAME
made a join (the same rows as a table with the column, and its conditions
still sent to the server), and the answer's check.
"""
import sqlite3
from datetime import datetime

import pytest

from mikage.core import analysis_plan
from mikage.core import data_chat as dc
from mikage.core.analysis import Column, Table, fold
from mikage.core.obs.model import DEFAULT_OBSERVATION_FIELDS
from mikage.core.text_assist import task_of, text_assist_settings_of

NOW = datetime(2026, 11, 24, 10, 0)
AREA_ROLES = {"name": "NAME", "kind": "KIND", "active": "ACTIVE", "id": "NAMED_AREA_ID"}
OBS_COLUMNS = ["OBJECTID", "OBS_ID", "IMAGE_ID", "IMAGE_TIME", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE",
               "LENGTH_M", "CONFIDENCE", "OBSERVER", "CREATED_AT", "DELETED_AT", "NAMED_AREA_OBJECTID"]


def _table(name, kind, roles, columns):
    return Table(name, name, "esri", kind, url=f"https://example.com/{name}/FeatureServer/0", roles=dict(roles),
                 columns=[Column(c, "TEXT", "", "", c == "OBJECTID") for c in columns])


def _schema(values=None, area=True):
    tables = [_table("TBL_OBSERVATION", "observation", DEFAULT_OBSERVATION_FIELDS, OBS_COLUMNS)]
    if area:
        tables.append(_table("TBL_NAMED_AREA", "namedArea", AREA_ROLES, ["OBJECTID", "NAME", "KIND", "ACTIVE"]))
    return dc.schema_of(tables, values or {})


def _db():
    """観測記録 (some deleted, one without a place) and 地名, as the server has them."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE TBL_NAMED_AREA (OBJECTID INTEGER, NAME TEXT, KIND TEXT, ACTIVE INTEGER)")
    con.executemany("INSERT INTO TBL_NAMED_AREA VALUES (?,?,?,?)", [(1, "呉", "港", 1), (2, "舞鶴", "港", 1)])
    con.execute("CREATE TABLE TBL_OBSERVATION (" + ", ".join(f"{c} TEXT" for c in OBS_COLUMNS) + ")")
    rows = [("a", "船舶 > 艦艇 > 護衛艦", "2026-10-03 09:00:00", None, 1), ("b", "船舶 > 艦艇 > 護衛艦", "2026-10-05 09:00:00", None, 1),
            ("c", "船舶 > タグボート", "2026-10-07 09:00:00", None, 2), ("d", "航空機 > 戦闘機", "2026-11-02 09:00:00", None, 2),
            ("e", "船舶 > タグボート", "2026-10-09 09:00:00", "2026-10-10 00:00:00", 1), ("f", "車両", "2026-10-11 09:00:00", None, None)]
    for oid, (obs_id, kind, when, deleted, place) in enumerate(rows, 1):
        values = {c: None for c in OBS_COLUMNS} | {"OBJECTID": oid, "OBS_ID": obs_id, "KIND": kind, "IMAGE_TIME": when,
                                                   "DELETED_AT": deleted, "NAMED_AREA_OBJECTID": place, "CONFIDENCE": "確定"}
        con.execute(f"INSERT INTO TBL_OBSERVATION VALUES ({','.join('?' * len(OBS_COLUMNS))})", [values[c] for c in OBS_COLUMNS])
    return con


def _run(sql, schema):
    """The SQL as data chat runs it: PLACE_NAME a join, and the records deleted left out (the read condition)."""
    con = _db()
    con.execute(f"DELETE FROM TBL_OBSERVATION WHERE NOT ({schema.conditions['TBL_OBSERVATION']})")
    return con.execute(dc.expand_place(sql, schema)).fetchall()


# ---- settings ---------------------------------------------------------------------------------------

def test_a_server_model_and_the_task_of_its_own():
    problems = []
    s = text_assist_settings_of({
        "models": {"pc": "models/qwen3.5-9b", "gpu": {"url": "https://llm.example.local/v1", "model": "qwen3.8-27b", "timeoutSeconds": 120}},
        "tasks": {"dataChat": {"model": "gpu", "tables": ["TBL_OBSERVATION", "request"], "rules": ["「艦艇」は KIND LIKE '%艦艇%'"],
                               "examples": [{"question": "今日の記録は？", "sql": "SELECT COUNT(*) FROM TBL_OBSERVATION"}]},
                  "command": "pc"}}, problems)
    assert problems == []
    m = s.model_for("dataChat")
    assert m.on_server and m.remote == "qwen3.8-27b" and m.timeout == 120 and m.path == ""
    assert m.chat_url == "https://llm.example.local/v1/chat/completions"
    t = s.task("dataChat")
    assert t.label == "データに質問" and t.max_tokens == 400 and t.tables == ("TBL_OBSERVATION", "request")
    assert t.rules == ("「艦艇」は KIND LIKE '%艦艇%'",) and t.examples == (("今日の記録は？", "SELECT COUNT(*) FROM TBL_OBSERVATION"),)
    assert not s.model_for("command").on_server


def test_a_server_model_written_wrong_is_said():
    problems = []
    s = text_assist_settings_of({"models": {"a": {"url": "llm.local"}, "b": {"url": "http://x/v1", "path": "m", "timeoutSeconds": 1}},
                                 "tasks": {"dataChat": {"model": "b", "tables": "TBL_OBSERVATION", "examples": [{"q": 1}]}}}, problems)
    assert any("models.a.url" in p for p in problems)
    assert any("path と url" in p for p in problems) and any("timeoutSeconds" in p for p in problems)
    assert any("examples[0]" in p for p in problems)
    # A table named alone is a list of one.
    assert s.task("dataChat").tables == ("TBL_OBSERVATION",)


# ---- what the model is told -------------------------------------------------------------------------

def test_the_tables_told_with_their_values_the_place_column_and_no_deleted_records():
    schema = _schema({("TBL_OBSERVATION", "KIND"): ["船舶 > 艦艇 > 護衛艦", "船舶 > タグボート", "航空機 > 戦闘機"],
                      ("TBL_OBSERVATION", "CONFIDENCE"): ["不明", "推定", "確定"], ("TBL_NAMED_AREA", "NAME"): ["呉", "舞鶴"]})
    text = dc.schema_text(schema)
    assert "■ TBL_OBSERVATION 観測記録（1 行 = 画像の上の 1 つのモノの記録。削除済みは入っていない）" in text
    assert "KIND 種別のパス（例 '船舶 > 艦艇 > 護衛艦' '船舶 > タグボート' '航空機 > 戦闘機'。大分類は KIND LIKE '船舶%'）" in text
    assert "CONFIDENCE 確度（不明・推定・確定）" in text and "PLACE_NAME 記録の地名（呉・舞鶴）" in text
    # Columns the layer does not have are not told (SATELLITE, HEADING …), nor deleted's, nor the place's key.
    assert "SATELLITE" not in text and "HEADING" not in text and "DELETED_AT" not in text and "NAMED_AREA_OBJECTID" not in text
    assert schema.conditions == {"TBL_OBSERVATION": "DELETED_AT IS NULL"}
    assert schema.place == ("TBL_OBSERVATION", "NAMED_AREA_OBJECTID", "TBL_NAMED_AREA", "OBJECTID", "NAME")
    assert schema.labels["KIND"] == "種別のパス" and schema.labels["PLACE_NAME"] == "地名"
    rules = dc.rules_text(schema, ["「艦艇」は KIND LIKE '%艦艇%'"])
    assert "撮像日時 IMAGE_TIME" in rules and "TBL_OBSERVATION の PLACE_NAME（結合しなくてよい）" in rules
    assert "- 「艦艇」は KIND LIKE '%艦艇%'" in rules and "NG: 理由" in rules


def test_without_the_named_areas_there_is_no_place_column():
    schema = _schema(area=False)
    assert schema.place is None and "PLACE_NAME" not in dc.schema_text(schema)
    assert dc.expand_place("SELECT COUNT(*) FROM TBL_OBSERVATION WHERE PLACE_NAME = '呉'", schema).endswith("PLACE_NAME = '呉'")


def test_the_dates_and_the_labels_of_a_result():
    p = dc.Periods(NOW)
    text = p.text()
    assert text.startswith("日付（今日は 2026-11-24（火））: 今日 2026-11-24 / 昨日 2026-11-23 / 明日 2026-11-25")
    assert "今週 2026-11-23〜2026-11-29 / 先週 2026-11-16〜2026-11-22 / 今月 2026-11-01〜2026-11-30 / 先月 2026-10-01〜2026-10-31" in text
    assert "直近7日 2026-11-18〜2026-11-24 / 今年 2026-01-01〜2026-12-31" in text
    assert p.label("2026-10") == "（先月）" and p.label("2026-11") == "（今月）" and p.label("2026-09") == ""
    assert p.label("2026-11-23") == "（昨日）" and p.label(3) == ""


def test_the_conversation_with_examples_of_the_tables_own_names_and_a_follow_up():
    values = {("TBL_NAMED_AREA", "NAME"): ["呉", "横須賀"], ("TBL_OBSERVATION", "CONFIDENCE"): ["推定", "確定"]}
    schema = _schema(values)
    task = task_of("dataChat", "pc")
    history = [dc.Turn("先月の呉の記録は何件？", "SELECT COUNT(*) FROM TBL_OBSERVATION WHERE PLACE_NAME = '呉'")]
    m = dc.sql_messages(task, schema, dc.Periods(NOW), "では舞鶴は？", history, values)
    assert m[0]["role"] == "system" and m[0]["content"].startswith(task.prompt) and m[0]["content"].endswith("【ここまで】")
    assert m[1]["content"] == "先月の呉の記録で、確度が確定のものは何件？"
    assert m[2]["content"] == ("SELECT COUNT(*) FROM TBL_OBSERVATION WHERE PLACE_NAME = '呉' AND CONFIDENCE = '確定' "
                               "AND IMAGE_TIME >= '2026-10-01' AND IMAGE_TIME < '2026-11-01'")
    assert m[3]["content"] == "来週の天気は？" and m[4]["content"].startswith("NG:")
    assert m[-3:] == [{"role": "user", "content": "先月の呉の記録は何件？"}, {"role": "assistant", "content": history[0].sql},
                      {"role": "user", "content": "では舞鶴は？"}]
    # The beginning is the same for every question of the day (read once, kept).
    assert dc.sql_messages(task, schema, dc.Periods(NOW.replace(hour=15)), "x")[:-1] == dc.sql_messages(task, schema, dc.Periods(NOW), "")
    # Without values read: the study's place and level.
    assert dc.sql_messages(task, _schema(), dc.Periods(NOW), "")[1]["content"] == "先月の舞鶴の記録で、確度が確定のものは何件？"
    again = dc.retry_messages(m, "SELECT X FROM TBL_OBSERVATION", "列がない: X")
    assert again[-2] == {"role": "assistant", "content": "SELECT X FROM TBL_OBSERVATION"} and "列がない: X" in again[-1]["content"]


# ---- the SQL ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, sql, why", [
    ("```sql\nSELECT COUNT(*) FROM TBL_OBSERVATION;\n```", "SELECT COUNT(*) FROM TBL_OBSERVATION", ""),
    ("SELECT 1 FROM TBL_OBSERVATION\n\nこの SQL は…", "SELECT 1 FROM TBL_OBSERVATION", ""),
    ("<think>\n\n</think>\n\nSELECT KIND FROM TBL_OBSERVATION（種別）", "SELECT KIND FROM TBL_OBSERVATION", ""),
    ("NG: 天気のデータはつながっていません", None, "天気のデータはつながっていません"),
    ("わかりません", None, ""),
])
def test_the_sql_read_from_the_reply(raw, sql, why):
    assert dc.sql_of(raw) == (sql, why)


def test_only_one_select_of_the_tables_offered_is_run():
    names = ["TBL_OBSERVATION", "TBL_NAMED_AREA"]
    dc.check_sql("SELECT COUNT(*) FROM TBL_OBSERVATION o, TBL_NAMED_AREA a WHERE o.NAMED_AREA_OBJECTID = a.OBJECTID", names)
    dc.check_sql("WITH m AS (SELECT KIND FROM TBL_OBSERVATION) SELECT COUNT(*) FROM m", names)
    dc.check_sql("SELECT KIND, COUNT(*) FROM TBL_OBSERVATION GROUP BY KIND, ACTIVITY", names)
    for sql, said in [("UPDATE TBL_OBSERVATION SET KIND = 'x'", "SELECT 文ではない"),
                      ("SELECT 1 FROM TBL_OBSERVATION; DELETE FROM TBL_OBSERVATION", "2 つ以上"),
                      ("SELECT * FROM TBL_SATELLITE_OUTAGE", "つながっていない表: TBL_SATELLITE_OUTAGE"),
                      ("SELECT * FROM TBL_OBSERVATION JOIN TBL_ALERT_LOG l ON 1=1, TBL_EVENT", "TBL_ALERT_LOG・TBL_EVENT"),
                      ("SELECT 'it''s", "読めない")]:
        with pytest.raises(dc.ChatError, match=said):
            dc.check_sql(sql, names)


@pytest.mark.parametrize("sql, rows", [
    ("SELECT COUNT(*) FROM TBL_OBSERVATION WHERE PLACE_NAME = '呉' AND IMAGE_TIME >= '2026-10-01' AND IMAGE_TIME < '2026-11-01'", [(2,)]),
    ("SELECT PLACE_NAME, COUNT(*) FROM TBL_OBSERVATION GROUP BY PLACE_NAME ORDER BY PLACE_NAME", [(None, 1), ("呉", 2), ("舞鶴", 2)]),
    ("SELECT o.PLACE_NAME, o.KIND FROM TBL_OBSERVATION AS o WHERE o.PLACE_NAME LIKE '舞%' AND KIND LIKE '航空機%'", [("舞鶴", "航空機 > 戦闘機")]),
    ("SELECT KIND FROM TBL_OBSERVATION WHERE PLACE_NAME IS NULL", [("車両",)]),
    ("SELECT COUNT(*) FROM TBL_OBSERVATION WHERE PLACE_NAME = '呉' OR KIND = '車両'", [(3,)]),
    ("SELECT COUNT(DISTINCT PLACE_NAME) FROM TBL_OBSERVATION", [(2,)]),
    ("SELECT KIND, COUNT(*) FROM TBL_OBSERVATION WHERE KIND IN (SELECT KIND FROM TBL_OBSERVATION WHERE PLACE_NAME = '舞鶴') GROUP BY KIND"
     " ORDER BY KIND", [("航空機 > 戦闘機", 1), ("船舶 > タグボート", 1)]),
])
def test_place_name_made_a_join_gives_the_rows_of_a_table_with_the_column(sql, rows):
    assert _run(sql, _schema()) == rows


def test_the_join_keeps_the_conditions_sent_to_the_server():
    schema = _schema()
    tables = {"tbl_observation": ("TBL_OBSERVATION", {fold(c) for c in OBS_COLUMNS}),
              "tbl_named_area": ("TBL_NAMED_AREA", {"objectid", "name", "kind", "active"})}
    ran = dc.expand_place("SELECT COUNT(*) FROM TBL_OBSERVATION WHERE PLACE_NAME = '呉' AND KIND LIKE '船舶%'", schema)
    assert ran == ("SELECT COUNT(*) FROM TBL_OBSERVATION JOIN TBL_NAMED_AREA _place ON TBL_OBSERVATION.NAMED_AREA_OBJECTID = "
                   "_place.OBJECTID WHERE _place.NAME = '呉' AND TBL_OBSERVATION.KIND LIKE '船舶%'")
    plan = analysis_plan.plan_of(ran, {}, tables)
    assert [p.text for p in plan.parts["_place"]] == ["_place.\"NAME\" = '呉'"]
    assert [p.text for p in plan.parts["tbl_observation"]] == ["TBL_OBSERVATION.\"KIND\" LIKE '船舶%'"]
    # The records read for the place's key (the place read first), and nothing kept back.
    assert {(k.source, k.target) for k in plan.keys} == {("_place", "tbl_observation"), ("tbl_observation", "_place")}
    assert plan.kept == {}
    # Grouped by place: a left join (records without one stay), its column keeps its name.
    ran = dc.expand_place("SELECT PLACE_NAME, COUNT(*) FROM TBL_OBSERVATION GROUP BY PLACE_NAME", schema)
    assert ran.startswith("SELECT _place.NAME AS PLACE_NAME, COUNT(*) FROM TBL_OBSERVATION LEFT JOIN TBL_NAMED_AREA _place")


# ---- the answer -------------------------------------------------------------------------------------

def test_the_answer_is_written_from_the_table_labelled_and_counted():
    columns = ["MONTH", "KIND", "n"]
    rows = [("2026-10", "船舶 > 艦艇 > 護衛艦", 3), ("2026-10", "船舶 > タグボート", 2), ("2026-11", "船舶 > 艦艇 > 護衛艦", 4)]
    text = dc.answer_input("先月と今月の記録は？", columns, rows, dc.Periods(NOW), {"KIND": "種別"})
    assert "MONTH\t種別\tn" in text and "2026-10（先月）\t船舶 > 艦艇 > 護衛艦\t3" in text and "2026-11（今月）" in text
    assert "プログラムが数えた数: 種別: 護衛艦 2・タグボート 1" in text
    many = dc.answer_input("一覧", ["ID"], [(f"x{i}",) for i in range(25)], dc.Periods(NOW))
    assert "（ほか 5 行。全部で 25 行）" in many and "x19" in many and "x20" not in many
    m = dc.answer_messages("q", ["n"], [(1,)], dc.Periods(NOW))
    assert m[0]["content"] == dc.ANSWER_PROMPT and m[1]["content"].startswith("質問: q")


def test_the_answer_checked_against_the_table():
    rows = [("呉", 12), ("舞鶴", 7), ("横須賀", 5)]
    sql = "SELECT PLACE_NAME, COUNT(*) FROM TBL_OBSERVATION GROUP BY PLACE_NAME ORDER BY 2 DESC"
    assert dc.check_answer("地名ごとに", sql, "呉が 12 件で最も多く、次いで舞鶴 7 件、横須賀 5 件でした。", rows) == []
    assert dc.check_answer("地名ごとに", sql, "合計 24 件で、3 か所に記録がありました。", rows) == []
    assert dc.check_answer("地名ごとに", sql, "呉が 13 件で最も多い。", rows) == ["表にない数 13"]
    assert dc.check_answer("地名ごとに", sql, "呉が 7 件、舞鶴が 12 件。", rows) == ["取り違え 7", "取り違え 12"]
    assert dc.check_answer("地名ごとに", sql, "舞鶴が 7 件、呉が 12 件。", rows) == ["表と順が違う"]
    assert dc.check_answer("地名ごとに", sql, "「佐世保」はありません。", rows) == ["表にない語「佐世保」"]
    assert dc.check_answer("今日は？", "SELECT COUNT(*) FROM T", "2026年11月24日の記録は 3 件。", [(3,)]) == []
    assert dc.check_answer("x", "SELECT 1", "5 件ありました。", []) == ["表にない数 5", "0 行なのに該当があるように書いた"]
    assert dc.check_answer("x", "SELECT 1", "該当するものはありませんでした。", []) == []


def test_the_program_sentence_and_headings():
    assert dc.plain_answer(["COUNT(*)"], [(12,)]) == "12 件でした。"
    assert dc.plain_answer(["AVG(LENGTH_M)"], [(123.456,)]) == "AVG(LENGTH_M)は 123.46 でした。"
    assert dc.plain_answer(["KIND"], []) == "該当するものはありませんでした。"
    assert dc.plain_answer(["KIND", "n"], [("a", 1), ("b", 2)]) == "2 行の結果です（下の表）。"
    assert dc.header_label("count(*)") == "件数" and dc.header_label("KIND", {"KIND": "種別"}) == "種別"
    assert dc.clean_answer("<think>x</think>\n- 呉は 3 件。\n- 舞鶴は 2 件。") == "呉は 3 件。舞鶴は 2 件。"


@pytest.mark.parametrize("text, asks", [
    ("先月の舞鶴の記録は何件？", True), ("呉で一番多い種別は", True), ("今日の撮像予定を地名ごとに", True),
    ("Aシステムの明日のオーダーファイルを出力", False), ("撮像予定を開く", False), ("横須賀へ移動", False),
])
def test_a_question_on_the_data(text, asks):
    assert dc.is_question(text) is asks


def test_how_it_was_found():
    text = dc.describe("SELECT 1 FROM T WHERE PLACE_NAME = 'x'", "SELECT 1 FROM T JOIN A _place …", {"T": 1234, "A": 2},
                       {"T": "DELETED_AT IS NULL"}, ["1 回目の SQL がエラー"])
    assert "実行した SQL（地名の列をプログラムが結合に直したもの）" in text
    assert "読んだ表: T 1,234 行（条件 DELETED_AT IS NULL）、A 2 行" in text and text.endswith("1 回目の SQL がエラー")
