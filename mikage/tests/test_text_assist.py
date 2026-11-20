"""文章の手伝い (core/text_assist.py): the settings, what the model is asked, and the checks of what it wrote."""
from mikage.core.config import parse_config
from mikage.core.obs.glossary import Glossary, Term
from mikage.core import text_assist as ta


GLOSSARY = Glossary([
    Term("入港", variants=("入航", "入湊"), kinds=("船舶",)),
    Term("停泊中"),
    Term("岸壁", kinds=("船舶",)),
    Term("駐機", kinds=("航空機",)),
])


def test_one_model_is_given_the_first_two_tasks():
    """The first settings (2026-11-19): one model, for 備考を整える and 申し送りの下書き."""
    problems = []
    s = ta.text_assist_settings_of("models/sarashina-1b-int4", problems)
    assert not problems and s.models == (ta.TextModel("model", "models/sarashina-1b-int4"),)
    assert [t.name for t in s.tasks] == ["tidyNote", "handoverDraft"] and s.keep_minutes == 10.0
    assert s.has("tidyNote") and s.has("handoverDraft") and not s.has("noteFields")
    tidy = s.task("tidyNote")
    assert (tidy.max_tokens, tidy.prompt, tidy.examples) == (160, ta.NOTE_PROMPT, ta.NOTE_EXAMPLES)
    assert s.task("handoverDraft").max_tokens == 320 and s.model_for("handoverDraft").path == "models/sarashina-1b-int4"
    s = ta.text_assist_settings_of({"model": "m.zip", "threads": 4, "maxTokens": 80, "handoverMaxTokens": 400, "keepMinutes": 0,
                                    "notePrompt": "短く", "handoverPrompt": "申し送りを", "noteExamples": [
                                        {"kind": "船舶", "note": "とまってる", "tidy": "停泊中。"}]}, problems)
    assert not problems
    assert s.models[0].threads == 4 and s.keep_minutes == 0.0
    tidy, handover = s.task("tidyNote"), s.task("handoverDraft")
    assert (tidy.max_tokens, tidy.prompt, handover.max_tokens, handover.prompt) == (80, "短く", 400, "申し送りを")
    assert tidy.examples == (("種別: 船舶\n備考: とまってる", "停泊中。"),)


def test_models_by_name_and_a_model_for_each_task():
    """tsudo 2026-11-19: 「モデル一覧のコンフィグと、やることに対してどのモデルをはめるか…はめがない場合機能しない」."""
    problems = []
    s = ta.text_assist_settings_of({
        "models": {"small": "\\\\server\\models\\sarashina-1b-int4", "large": {"path": "models/sarashina-3b-int4", "threads": 6}},
        "tasks": {"summary": {"model": "large", "maxTokens": 600, "prompt": "短くまとめる"}, "tidyNote": "small",
                  "noteFields": "large", "handoverDraft": {"model": "large"}},
        "keepMinutes": 5}, problems)
    assert not problems
    assert [(m.name, m.threads) for m in s.models] == [("small", 0), ("large", 6)]
    # In the order the tasks are listed (not as written).
    assert [(t.name, t.model) for t in s.tasks] == [("tidyNote", "small"), ("handoverDraft", "large"), ("noteFields", "large"),
                                                   ("summary", "large")]
    assert s.task("summary").max_tokens == 600 and s.task("summary").prompt == "短くまとめる"
    assert s.task("noteFields").prompt == ta.FIELDS_PROMPT and s.task("noteFields").examples == ()
    # A task given no model is not offered.
    assert not s.has("searchWords") and not s.has("translate") and s.model_for("translate") is None
    assert s.model_for("tidyNote").path.endswith("sarashina-1b-int4") and s.keep_minutes == 5.0


def test_wrong_settings_are_said_and_what_is_left_works():
    problems = []
    assert ta.text_assist_settings_of({"threads": 2}, problems) is None
    assert ta.text_assist_settings_of(3, problems) is None
    s = ta.text_assist_settings_of({"model": "m", "threads": -1, "maxTokens": 1.5, "keepMinutes": "x", "notePrompt": "",
                                    "noteExamples": [{"note": 1}], "modle": "typo"}, problems)
    assert s.models == (ta.TextModel("model", "m"),) and s.task("tidyNote") == ta.task_of("tidyNote", "model")
    joined = "\n".join(problems)
    for word in ("textAssist.model", "textAssist は", "threads", "maxTokens", "keepMinutes", "notePrompt", "noteExamples[0]", "modle"):
        assert word in joined, word
    problems = []
    s = ta.text_assist_settings_of({"models": {"a": "x", "b": 3, "": "y"}, "model": "z",
                                    "tasks": {"tidyNote": "a", "summary": "nothing", "dream": "a", "translate": {"model": "a", "examples": []},
                                              "noteFields": {"model": "a", "maxTokens": 9}}}, problems)
    assert [m.name for m in s.models] == ["a"]
    assert [t.name for t in s.tasks] == ["tidyNote", "noteFields", "translate"] and s.task("noteFields").max_tokens == 160
    joined = "\n".join(problems)
    for word in ("model と models", "models.b", "名前が空", "tasks.summary のモデル「nothing」は models にない", "tasks.dream",
                 "tasks.translate の examples", "tasks.noteFields.maxTokens"):
        assert word in joined, word
    problems = []
    s = ta.text_assist_settings_of({"models": {"a": "x"}}, problems)
    assert s.tasks == () and "割り当てたタスクがない" in problems[0]


def test_the_settings_file_key():
    config, problems = parse_config({"textAssist": {"models": {"large": "\\\\server\\models\\sarashina-3b"}, "tasks": {"summary": "large"}}})
    assert config.text_assist.model_for("summary").path == "\\\\server\\models\\sarashina-3b" and not problems
    assert parse_config({})[0].text_assist is None


def test_the_note_is_asked_with_its_kind_the_glossary_words_and_the_examples():
    s = ta.task_of("tidyNote", "m")
    terms = ta.note_terms(GLOSSARY, "岸壁に入航して停泊中", "船舶")
    # Its other spellings' words first (written as the glossary does), then the words it uses; not the 大分類's
    # other words (a small model writes them into the 備考).
    assert terms == ["入港", "停泊中", "岸壁"]
    assert ta.note_terms(GLOSSARY, "入航した", "船舶") == ["入港"]
    assert ta.note_terms(GLOSSARY, "駐機場", "航空機") == ["駐機"]
    assert ta.note_terms(None, "x") == [] and ta.note_terms(Glossary(), "x") == []
    messages = ta.note_messages(s, "岸壁に入航して停泊中", "船舶 > 駆逐艦", terms)
    assert messages[0] == {"role": "system", "content": ta.NOTE_PROMPT}
    assert [m["role"] for m in messages[1:]] == ["user", "assistant"] * len(ta.NOTE_EXAMPLES) + ["user"]
    assert messages[-1]["content"] == "種別: 船舶 > 駆逐艦\n表記: 入港、停泊中、岸壁\n備考: 岸壁に入航して停泊中"
    # A very long 備考 is cut.
    assert len(ta.note_messages(s, "あ" * 1000)[-1]["content"]) == len("備考: ") + ta.NOTE_LIMIT


def test_what_the_model_wrote_is_cleaned():
    assert ta.clean_note("備考: 「岸壁に停泊中。」\n\n### 入力") == "岸壁に停泊中。"
    assert ta.clean_note("\n```\n案：岸壁に停泊中。\n```") == "岸壁に停泊中。"
    assert ta.clean_note("岸壁　に　停泊中。 2  隻") == "岸壁　に　停泊中。 2 隻"
    assert ta.clean_paragraph("申し送り: 期限間近の依頼が 1 件。\n\n\n\n担当中の画像あり。\n") == "期限間近の依頼が 1 件。\n\n担当中の画像あり。"
    assert ta.clean_paragraph("### 出力\n下書き本文") == "下書き本文"
    # sarashina2.2 1B says what it did after the 備考.
    assert ta.note_draft("エプロンに 4 機駐機。表記を統一し、備考を簡潔に書き直しました。", "エプロンに4機").text == "エプロンに 4 機駐機。"
    assert ta.note_draft("書き直しました。", "x").text == "書き直しました。"
    # A small model writes a letter (tried on sarashina2.2 3B): its address, introduction and closing are left out.
    letter = ("引き継ぎ担当者様、\n\nまず、横須賀定点の期限が近い。\n\n東側が未確認の模様。\n\n"
              "以上、引き継ぎ事項をまとめさせていただきました。ご不明な点はお問い合わせください。")
    assert ta.clean_paragraph(letter) == "まず、横須賀定点の期限が近い。\n\n東側が未確認の模様。"
    assert ta.clean_paragraph("申し送りは以下の通りです。\n期限間近の依頼が 1 件。") == "期限間近の依頼が 1 件。"
    assert ta.clean_paragraph("東側は未確認の模様") == "東側は未確認の模様"
    assert ta.clean_paragraph("以上") == "以上"
    assert ta.clean_paragraph("・期限が近い。\n① 東側が未確認。\n2. 通報済み。\n11/20 09:00 まで") == "期限が近い。\n東側が未確認。\n通報済み。\n11/20 09:00 まで"


def test_a_paragraph_stops_at_the_sentences_asked_for():
    assert ta.sentence_limit(ta.HANDOVER_PROMPT) == 4 and ta.sentence_limit(ta.SUMMARY_PROMPT) == 6
    assert ta.sentence_limit("３～５文で") == 5 and ta.sentence_limit("短く書いて") is None
    assert ta.sentences_done(None) is None
    done = ta.sentences_done(2)
    assert not done("申し送りは以下の通りです。\n期限が近い。")
    assert done("申し送りは以下の通りです。\n期限が近い。東側が未確認。")


def test_numbers_it_made_up_are_found():
    assert ta.numbers_in("2隻、１２３ｍ、1,200 t、二十五日、百五十、三") == ["2", "123", "1200", "25", "150", "3"]
    assert ta.foreign_numbers("たぶん2隻くらい", "2 隻程度と推定。") == ()
    assert ta.foreign_numbers("二隻", "2 隻") == ()
    assert ta.foreign_numbers("2隻", "3 隻、全長 150 m") == ("3", "150")
    assert ta.foreign_numbers("1.5km", "1.50 km") == ()


def test_a_note_draft_has_the_glossary_words_and_what_to_check():
    d = ta.note_draft("備考: 岸壁に入航、停泊中。3隻", "岸壁に入航してとまってる", GLOSSARY, "船舶")
    assert d.text == "岸壁に入港、停泊中。3隻" and d.foreign == ("3",) and not d.same
    assert d.warnings == ["元にない数字: 3"]
    assert ta.note_draft("岸壁に停泊中。", "岸壁に停泊中").same
    long = ta.note_draft("あ" * 100, "短い備考")
    assert long.longer and "長い" in long.warnings[0]


def test_the_handover_is_asked_from_the_memo_without_its_own_handover_part():
    memo = ("引き継ぎメモ 11/19 08:00〜11/19 20:00\n\n■ 観測記録: 新規 3 件\n\n"
            "■ 未処理の撮像依頼: 2 件（うち 24 時間以内に期限 1 件）\n  【期限間近】横須賀\n\n■ 申し送り\n  手書きの 7 件")
    source = ta.handover_source(memo)
    assert "■ 申し送り" not in source and "手書き" not in source and "【期限間近】横須賀" in source
    messages = ta.handover_messages(ta.task_of("handoverDraft", "m"), memo, "横須賀を優先")
    assert messages[0]["content"] == ta.HANDOVER_PROMPT
    assert messages[1]["content"].startswith("引き継ぎメモ:\n引き継ぎメモ 11/19") and messages[1]["content"].endswith(
        "書きかけの申し送り（生かして書く）:\n横須賀を優先")
    d = ta.handover_draft("申し送り: 期限間近の横須賀を優先。新規 3 件、残り 9 件。", memo)
    assert d.text == "期限間近の横須賀を優先。新規 3 件、残り 9 件。" and d.foreign == ("9",)
    # The memo is cut to its first part when very long.
    assert ta.handover_source("x\n" * 5000, 100).endswith("…（以下略）")


def test_a_model_without_a_chat_template_gets_plain_text():
    text = ta.plain_prompt([{"role": "system", "content": "指示"}, {"role": "user", "content": "備考: a"}])
    assert text == "### 指示\n指示\n\n### 入力\n備考: a\n\n### 出力\n"
