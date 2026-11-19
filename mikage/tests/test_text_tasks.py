"""文章の手伝い for a model of about 3B (core/text_tasks.py): what is asked, and what is kept of the answers."""
from datetime import date

from mikage.core import text_assist as ta
from mikage.core import text_tasks as tt
from mikage.core.obs.model import Observation

KINDS = ["船舶", "船舶 > 補給艦", "船舶 > 駆逐艦", "船舶 > 駆逐艦 > 〇〇級", "航空機", "航空機 > 戦闘機"]
CONFIDENCES = ["確定", "推定", "可能性"]
ACTIVITIES = {"船舶": ["停泊", "航行", "入港"], "航空機": ["駐機", "離陸"]}


def resolve(text):
    """A path, or a class's last name → its path (as the dock does with the classification)."""
    for k in KINDS:
        if k == text or k.split(" > ")[-1] == text:
            return k.split(" > ")
    return None


def activities_of(kind):
    return ACTIVITIES.get(kind.split(" > ")[0], []) if kind else []


def test_the_answer_lines():
    raw = "```\n- 種別: 船舶 > 補給艦\n活動：停泊\n確度: なし\n長さ: 「約 120 m」\n種別: 航空機\n説明: 余計\n```"
    assert tt.answer_lines(raw, tt.FIELD_KEYS) == {"種別": "船舶 > 補給艦", "活動": "停泊", "長さ": "約 120 m"}


def test_the_kinds_named_to_the_model_start_with_the_kind_written():
    assert tt.kinds_for(KINDS, "航空機")[:2] == ["航空機", "航空機 > 戦闘機"]
    assert tt.kinds_for(KINDS)[:2] == ["船舶", "航空機"] and len(tt.kinds_for(KINDS, limit=3)) == 3


def test_the_fields_are_asked_with_their_choices():
    task = ta.task_of("noteFields", "large")
    messages = tt.fields_messages(task, "補給艦が岸壁に停泊、全長120m程度", "船舶", KINDS[:3], ACTIVITIES, CONFIDENCES)
    assert messages[0] == {"role": "system", "content": ta.FIELDS_PROMPT}
    assert messages[1]["content"].split("\n") == [
        "今の種別: 船舶", "種別の候補: 船舶、船舶 > 補給艦、船舶 > 駆逐艦", "活動の候補（船舶）: 停泊、航行、入港",
        "活動の候補（航空機）: 駐機、離陸", "確度の候補: 確定、推定、可能性", "備考: 補給艦が岸壁に停泊、全長120m程度"]


def test_only_the_fields_the_note_and_the_choices_support_are_kept():
    note = "補給艦が岸壁に停泊、全長120m程度。艦名は「おうみ」。"
    raw = "種別: 補給艦\n活動: 停泊\n確度: 推定\n長さ: 120\n幅: 25\n向き: 不明\nモノ: おうみ"
    d = tt.fields_draft(raw, note, resolve, activities_of, CONFIDENCES)
    assert d.values == {"kind": ["船舶", "補給艦"], "activity": "停泊", "confidence": "推定", "length": 120.0, "object": "おうみ"}
    assert d.dropped == ("幅 25 は備考にない数字",)
    assert d.text == "種別 船舶 > 補給艦・活動 停泊・確度 推定・長さ 120 m・モノ おうみ"
    # A kind not in the classification, an activity of another kind, a level not among them, a name not in the 備考.
    d = tt.fields_draft("種別: 潜水艦\n活動: 駐機\n確度: 確実\nモノ: むらさめ\n向き: 400", "向き400度", resolve, activities_of,
                        CONFIDENCES, kind_now="船舶")
    assert d.values == {}
    assert d.dropped == ("分類にない種別「潜水艦」", "この種別の活動にない「駐機」", "確度にない「確実」", "向き 400 は範囲外",
                         "備考にない名前「むらさめ」")
    # A whole path, and an activity of the kind it picked (not the one written so far).
    d = tt.fields_draft("種別: 航空機 > 戦闘機\n活動: 駐機\n長さ: 約十五 m", "戦闘機が駐機。長さ十五m", resolve, activities_of, CONFIDENCES,
                        kind_now="船舶")
    assert d.values == {"kind": ["航空機", "戦闘機"], "activity": "駐機", "length": 15.0}


def test_the_dates_of_the_words_people_use():
    hints = {name: (a, b) for name, a, b in tt.date_hints(date(2026, 11, 19))}  # a Thursday
    assert hints["今日"] == (date(2026, 11, 19),) * 2 and hints["一昨日"] == (date(2026, 11, 17),) * 2
    assert hints["今週"] == (date(2026, 11, 16), date(2026, 11, 22)) and hints["先週"] == (date(2026, 11, 9), date(2026, 11, 15))
    assert hints["直近 7 日"] == (date(2026, 11, 13), date(2026, 11, 19))
    assert hints["今月"] == (date(2026, 11, 1), date(2026, 11, 30)) and hints["先月"] == (date(2026, 10, 1), date(2026, 10, 31))
    hints = {name: (a, b) for name, a, b in tt.date_hints(date(2026, 1, 5))}
    assert hints["先月"] == (date(2025, 12, 1), date(2025, 12, 31)) and hints["今年"] == (date(2026, 1, 1), date(2026, 12, 31))


def test_the_search_is_asked_with_the_dates_and_the_choices():
    task = ta.task_of("searchWords", "large")
    content = tt.search_messages(task, "先週の横須賀の船舶", date(2026, 11, 19), ["横須賀", "舞鶴"], ["船舶", "航空機"], CONFIDENCES)[1]["content"]
    lines = content.split("\n")
    assert lines[0] == "今日: 2026-11-19（木）" and "先週 2026-11-09 〜 2026-11-15" in lines[1] and "今日 2026-11-19、" in lines[1]
    assert lines[2:] == ["地名の候補: 横須賀、舞鶴", "大分類の候補: 船舶、航空機", "確度の候補: 確定、推定、可能性", "依頼: 先週の横須賀の船舶"]


def test_only_the_conditions_among_the_choices_are_kept():
    places, roots = ["横須賀", "舞鶴"], ["船舶", "航空機"]
    d = tt.search_draft("期間: 2026-11-09 〜 2026-11-15\n地名: 横須賀\n大分類: 船舶、航空機\n確度: 推定以上\n有無: あり",
                        places, roots, CONFIDENCES)
    assert (d.start, d.end, d.place, d.roots, d.confidence, d.presence) == (
        date(2026, 11, 9), date(2026, 11, 15), "横須賀", ("船舶", "航空機"), "推定", "present")
    assert not d.dropped and d.text == "期間 2026-11-09〜2026-11-15・地名 横須賀・大分類 船舶・航空機・確度 推定・有無 ありのみ"
    d = tt.search_draft("期間: 2026年11月19日\n地名: 佐世保\n大分類: 潜水艦\n確度: たぶん\n有無: 消失", places, roots, CONFIDENCES)
    assert (d.start, d.end, d.place, d.roots, d.confidence, d.presence) == (date(2026, 11, 19), date(2026, 11, 19), None, (), None, "absent")
    assert d.dropped == ("地名の候補にない「佐世保」", "大分類にない「潜水艦」", "確度にない「たぶん」")
    # 不可 (雲などで見えなかった記録); 「なし」 is no condition.
    assert tt.search_draft("有無: 不可", [], [], []).text == "有無 不可のみ"
    assert tt.search_draft("有無: なし", [], [], []).empty
    assert tt.search_draft("有無: 全部", [], [], []).dropped == ("有無にない「全部」",)
    # From a day on; the two the wrong way round; nothing read.
    assert tt.search_draft("期間: 2026-11-01 〜", [], [], []).start == date(2026, 11, 1)
    d = tt.search_draft("期間: 2026-11-15 〜 2026-11-09", [], [], [])
    assert (d.start, d.end) == (date(2026, 11, 9), date(2026, 11, 15))
    d = tt.search_draft("期間: 最近\n地名: なし", places, roots, CONFIDENCES)
    assert d.empty and d.dropped == ("日付として読めない期間「最近」",)


def _obs(i, kind, activity="停泊", present=True, note="", length=None, image="d1.tif", unseen=""):
    return Observation(id=f"r{i}", image_id=image, image_name=image, image_time=1_000 * i, object_id=f"o{i}", object_name=f"船{i}",
                       kind=kind, activity=activity, present=present, note=note, lon=0, lat=0, created_at=i, updated_at=i,
                       length=length, confidence="推定", unseen=unseen)


def test_the_summary_is_asked_with_the_counts_first():
    records = [_obs(2, "船舶 > 補給艦", note="岸壁"), _obs(1, "船舶 > 駆逐艦", "航行", length=150), _obs(3, "航空機", "駐機", image="d2.tif"),
               _obs(4, "船舶", present=False)]
    source = tt.summary_source(records, lambda t: f"T{t}")
    lines = source.split("\n")
    assert lines[:4] == ["記録: 4 件（あり 3・消失 1）", "船舶: 2 件（航行 1・停泊 1）", "航空機: 1 件（駐機 1）", "画像: 2 枚（d1.tif・d2.tif）"]
    assert lines[5:] == ["T1000 / 船舶 > 駆逐艦 / 船1 / 航行 / 推定 / 長さ 150 m", "T2000 / 船舶 > 補給艦 / 船2 / 停泊 / 推定 / 岸壁",
                         "T3000 / 航空機 / 船3 / 駐機 / 推定", "T4000 / 船舶 / 船4 / 停泊 / 推定 / 消失"]
    assert tt.summary_source(records, str, most=2).endswith("…（ほか 2 件）")
    assert tt.summary_source(records * 50, str, limit=300).endswith("…（以下略）")
    messages = tt.summary_messages(ta.task_of("summary", "large"), source)
    assert messages[0]["content"] == ta.SUMMARY_PROMPT and messages[1]["content"] == "観測記録:\n" + source
    d = tt.summary_draft("まとめ: 船舶 2 件と航空機 1 件。駆逐艦は全長 150 m、ほかに 7 隻。", source)
    assert d.text == "船舶 2 件と航空機 1 件。駆逐艦は全長 150 m、ほかに 7 隻。" and d.foreign == ("7",)
    # 不可 (seen by no one: neither あり nor 消失).
    source = tt.summary_source([*records, _obs(5, "船舶", "", present=False, unseen="雲")], lambda t: f"T{t}")
    assert source.split("\n")[0] == "記録: 5 件（あり 3・消失 1・不可 1）" and source.endswith("T5000 / 船舶 / 船5 / 推定 / 不可（雲）")


def test_the_english_note():
    messages = tt.translate_messages(ta.task_of("translate", "large"), " 岸壁に停泊中、2 隻 ")
    assert messages == [{"role": "system", "content": ta.TRANSLATE_PROMPT}, {"role": "user", "content": "Note: 岸壁に停泊中、2 隻"}]
    d = tt.translated("English: Two vessels moored at the quay, 3 tugs nearby.\n\nNote: x", "岸壁に停泊中、2 隻")
    assert d.text == "Two vessels moored at the quay, 3 tugs nearby." and d.foreign == ("3",)
