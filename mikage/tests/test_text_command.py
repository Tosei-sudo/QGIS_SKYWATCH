"""
文で操作 (core/text_command.py): the operation a sentence's words name, the
model's reply read, and the values read from the sentence by code (the
sentences of the study, 2026-11-21: the real 3B chose the operation of 30
of these 32, and the code read the values of all of them).
"""
from datetime import date
from types import SimpleNamespace as NS

import pytest

from mikage.core import text_assist as ta
from mikage.core import text_command as tc

TODAY = date(2026, 11, 21)  # a Saturday
SYSTEMS = [NS(system="SYS-A", label="A システム（光学）"), NS(system="SYS-B", label="B システム（SAR）"),
           NS(system="SYS-C", label="C システム（光学・コンステレーション）")]
PLACES = ["呉", "横須賀", "佐世保", "舞鶴", "那覇", "厚木", "嘉手納", "大湊"]
SCREENS = ["画像補正", "物体検出（AI）…", "データ分析", "画像カタログ", "撮像予定"]
SATS = [NS(name="ALOS-2", id="39766", series="ALOS", aliases=["だいち2号"], modes=[], sensors=[]),
        NS(name="ASNARO-1", id="40298", series="ASNARO", aliases=[], modes=[], sensors=[])]
D = date.fromisoformat

# (sentence, the operation, its values: systems, period, place, point, screen)
CASES = [
    ("Aシステムの明日のオーダーファイルを出力して", "planFiles", {"systems": ["SYS-A"], "period": (D("2026-11-22"),) * 2}),
    ("SYS-Bの11/25分の計画ファイルを作って", "planFiles", {"systems": ["SYS-B"], "period": (D("2026-11-25"),) * 2}),
    ("明後日のCシステム向けの撮像計画ファイルを書き出し", "planFiles", {"systems": ["SYS-C"], "period": (D("2026-11-23"),) * 2}),
    ("来週月曜のB系の依頼を出力", "planFiles", {"systems": ["SYS-B"], "period": (D("2026-11-23"),) * 2}),
    ("全システムの今日の分のオーダーを出して", "planFiles", {"systems": [tc.ALL], "period": (TODAY, TODAY)}),
    ("Cシステムの計画ファイルを出力", "planFiles", {"systems": ["SYS-C"], "period": None}),
    ("AとBの明日の計画ファイルを両方出して", "planFiles", {"systems": ["SYS-A", "SYS-B"], "period": (D("2026-11-22"),) * 2}),
    ("昨日の撮像予定を見せて", "schedule", {"period": (D("2026-11-20"),) * 2}),
    ("明日の撮像予定でBが不採用にしたものを見たい", "schedule", {"systems": ["SYS-B"], "period": (D("2026-11-22"),) * 2}),
    ("Aシステムから返ってきた結果ファイルを取り込んで", "resultImport", {"systems": ["SYS-A"]}),
    ("Bの採否の結果を読み込みたい", "resultImport", {"systems": ["SYS-B"]}),
    ("先週出した依頼がちゃんと撮れたか確認して", "follow", {"period": (D("2026-11-09"), D("2026-11-15"))}),
    ("今年の年間上限はあとどれくらい残ってる？", "quotas", {"year_back": 0}),
    ("去年の年間撮像上限を見せて", "quotas", {"year_back": 1}),
    ("呉の先週の画像を探して", "catalog", {"place": "呉", "period": (D("2026-11-09"), D("2026-11-15"))}),
    ("横須賀で直近30日に撮ったSAR画像を出して", "catalog", {"place": "横須賀", "period": (D("2026-10-23"), TODAY)}),
    ("大湊の昨日の画像はある？", "catalog", {"place": "大湊", "period": (D("2026-11-20"),) * 2}),
    ("ALOS-2 の明日の軌道を地図に出して", "passes", {"satellites": ["ALOS-2"], "period": (D("2026-11-22"),) * 2}),
    ("佐世保の撮像予定をタイムラインで見たい", "placeSchedule", {"place": "佐世保"}),
    ("舞鶴を来週撮れる衛星を調べて", "imagingPlan", {"place": "舞鶴", "period": (D("2026-11-23"), D("2026-11-29"))}),
    ("呉で新着の画像があったら知らせて", "watch", {"place": "呉"}),
    ("今月の呉の観測記録をCSVで出力して", "obsExport", {"period": (D("2026-11-01"), D("2026-11-30"))}),
    ("艦船の確定の記録だけ先週分エクスポート", "obsExport", {"period": (D("2026-11-09"), D("2026-11-15"))}),
    ("交代するので申し送りを作って", "handover", {}),
    ("選んだ記録で通報文を作成", "alert", {}),
    ("来週から2週間、那覇を毎日撮る依頼を作って", "newRequest", {}),
    ("ASNARO-1 が12月1日まで観測停止になったので登録して", "outage", {"satellites": ["ASNARO-1"]}),
    ("Bシステムが年末年始は受付停止になるので登録", "outage", {"systems": ["SYS-B"], "satellites": []}),
    ("嘉手納へ移動", "jump", {"place": "嘉手納"}),
    ("35.3, 139.6 を表示して", "jump", {"point": (139.6, 35.3), "place": None}),
    ("画像補正の画面を開いて", "openScreen", {"screen": "画像補正"}),
    ("AIで物体検出したい", "openScreen", {"screen": "物体検出（AI）…"}),
]


@pytest.mark.parametrize("text, key, values", CASES, ids=[c[0] for c in CASES])
def test_the_values_are_read_by_code(text, key, values):
    r = tc.read(tc.BY_KEY[key], text, TODAY, SYSTEMS, PLACES, SATS, SCREENS)
    for name, want in values.items():
        assert getattr(r, name) == want, (name, getattr(r, name))


def test_a_value_not_said_is_left_empty():
    r = tc.read(tc.BY_KEY["planFiles"], "Cシステムの計画ファイルを出力", TODAY, SYSTEMS)
    assert r.period is None and r.missing() == ["day"]
    assert tc.read(tc.BY_KEY["jump"], "移動して", TODAY).missing() == ["place"]
    assert tc.read(tc.BY_KEY["openScreen"], "画面を開いて", TODAY, screens=SCREENS).missing() == ["screen"]


def test_dates_look_the_way_the_operation_does():
    # 「今月」: the plans from today, the records from the 1st.
    assert tc.read(tc.BY_KEY["imagingPlan"], "呉を今月撮れる衛星", TODAY, places=PLACES).period == (TODAY, D("2026-11-30"))
    assert tc.read(tc.BY_KEY["catalog"], "呉の今月の画像", TODAY, places=PLACES).period == (D("2026-11-01"), D("2026-11-30"))
    # A day asked for one day only: the first of the period said.
    assert tc.read(tc.BY_KEY["schedule"], "来週の撮像予定", TODAY).period == (D("2026-11-23"),) * 2


def test_the_words_name_one_operation_or_leave_it_to_the_model():
    ops = tc.OPERATIONS
    found = {text: [o.key for o in tc.word_matches(text, ops)] for text, _k, _v in CASES}
    for text, key, _values in CASES:
        # Never a wrong one alone: the right one, or several / none (the model chooses).
        assert found[text] == [key] or len(found[text]) != 1, (text, found[text])
    alone = sum(1 for text, key, _v in CASES if found[text] == [key])
    assert alone >= 25
    # 撮像計画 inside 撮像計画ファイル: only the plan files.
    assert found["明後日のCシステム向けの撮像計画ファイルを書き出し"] == ["planFiles"]
    # Words the settings add.
    assert tc.by_words("発注書を作って", ops) is None
    assert tc.by_words("発注書を作って", ops, {"planFiles": ["発注書"]}).key == "planFiles"
    # A screen of the menu named, and no operation's words: 画面を開く (the real 3B said 「なし」 to this one).
    assert tc.by_words("AIで物体検出したい", ops) is None
    assert tc.by_words("AIで物体検出したい", ops, screens=SCREENS).key == "openScreen"
    assert tc.by_words("今日の東京の天気は？", ops, screens=SCREENS) is None
    # Only among the operations given (the roles in use).
    assert tc.by_words("呉の先週の画像を探して", [tc.BY_KEY["jump"]]) is None


def test_the_conversation_lists_the_operations_and_ends_with_the_sentence():
    task = ta.task_of("command", "large")
    ops = [tc.BY_KEY[k] for k in ("planFiles", "catalog", "jump")]
    messages = tc.command_messages(task, ops, " 呉の画像 ")
    system = messages[0]["content"]
    assert system.startswith(ta.COMMAND_PROMPT) and "- 計画ファイル出力: " in system and "- 画像検索: " in system
    assert "- なし: " in system and "撮像予定" not in system
    # The examples of the operations listed, then the sentence.
    assert [m["content"] for m in messages[1:]] == [
        "依頼: Bシステムの明日分のオーダーファイルを作って", "操作: 計画ファイル出力", "依頼: 横須賀に移動して", "操作: 移動",
        "依頼: 那覇の昨日の画像を見たい", "操作: 画像検索", "依頼: 呉の画像"]
    # The same before the sentence for every sentence (the model keeps it read).
    assert tc.command_messages(task, ops, "嘉手納へ")[:-1] == messages[:-1]
    assert tc.command_messages(task, ops, "")[-1] == {"role": "user", "content": "依頼: "}


def test_the_reply_names_an_operation_of_the_list():
    ops = tc.OPERATIONS
    assert tc.operation_of("操作: 計画ファイル出力", ops).key == "planFiles"
    # Something after the name (a reason, brackets with a full-width colon).
    assert tc.operation_of("操作：画像検索（場所：呉、期間：先週）", ops).key == "catalog"
    # The longer name first (地点の撮像予定 before 撮像予定).
    assert tc.operation_of("操作: 地点の撮像予定", ops).key == "placeSchedule"
    for raw in ("操作: なし", "操作: 天気予報", "申し訳ありませんが、わかりません", ""):
        assert tc.operation_of(raw, ops) is None
    # Not among those given.
    assert tc.operation_of("操作: 計画ファイル出力", [tc.BY_KEY["jump"]]) is None
    done = tc.reply_done(ops)
    assert not done("操作: 計画ファ") and done("操作: 計画ファイル出力") and done("操作: なし") and done("説明\n")


def test_systems_by_code_label_and_letter():
    said = lambda t: tc.systems_in(t, SYSTEMS)  # noqa: E731
    assert said("sys-a の分") == ["SYS-A"] and said("B系") == ["SYS-B"] and said("Cの") == ["SYS-C"]
    assert said("全てのシステム") == [tc.ALL] and said("BとC") == ["SYS-B", "SYS-C"]
    # A letter inside a word is not a system.
    assert said("SAR画像") == [] and said("ALOS-2") == []


def test_places_points_and_screens():
    assert tc.lat_lon_in("北緯は 35.3、139.6 の辺り") == (139.6, 35.3)
    assert tc.lat_lon_in("11/25 と 12 月") is None
    assert tc.place_word("横須賀港の画像", ["横須賀", "横須賀港"]) == "横須賀港"
    # Not a date, a system or an operation's word.
    assert tc.place_word("明日のAシステムの計画ファイル") is None
    assert tc.screen_in("データ分析を開いて", SCREENS) == "データ分析"


def test_the_names_agree_with_the_settings():
    assert {o.key: o.name for o in tc.OPERATIONS} == ta.COMMAND_NAMES
    assert len(tc.OPERATIONS) == 17 and all(o.title and o.button and o.note for o in tc.OPERATIONS)


def test_the_task_and_its_words_in_the_settings():
    problems = []
    s = ta.text_assist_settings_of({"models": {"large": "m"}, "tasks": {"command": {
        "model": "large", "words": {"計画ファイル出力": ["発注書"], "jump": "飛んで", "天気": ["x"], "画像検索": [1]}}}}, problems)
    t = s.task("command")
    assert t.label == "文で操作" and t.max_tokens == 24 and t.prompt == ta.COMMAND_PROMPT and "command" in ta.LARGE_TASKS
    assert t.words == (("planFiles", ("発注書",)), ("jump", ("飛んで",)))
    assert len(problems) == 2 and "「天気」は操作にない" in problems[0] and "画像検索" in problems[1]
    # words is 文で操作's only.
    problems = []
    ta.text_assist_settings_of({"models": {"large": "m"}, "tasks": {"tidyNote": {"model": "large", "words": {}}}}, problems)
    assert problems and "words" in problems[0]


def test_the_helpers():
    ring = [[[139.6, 35.2], [139.8, 35.2], [139.8, 35.4], [139.6, 35.4], [139.6, 35.2]]]
    assert tc.extent_of(ring) == (139.6, 35.2, 139.8, 35.4)
    assert tc.center_of(ring) == pytest.approx((139.7, 35.3))
    assert tc.corners_of(ring) == [[139.6, 35.2], [139.8, 35.2], [139.8, 35.4], [139.6, 35.4]]
    start, end = tc.jst_bounds(TODAY)
    assert end - start == 86_400_000 and start == 1795186800000  # 2026-11-20T15:00Z
    assert tc.day_text(TODAY) == "2026-11-21（土）" and tc.period_days((TODAY, D("2026-11-23"))) == 3
