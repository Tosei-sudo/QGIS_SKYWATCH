"""
「ミカゲと話す」's routing (core/chat.py; tsudo 2026-11-28: 「文で操作の機能は全廃しよう。名前は任せるけど、チャットで質問も
操作もできるように」): code first, in order (a follow-up of the list shown, images of a place, an operation's words when it is
no question), the model last; the 30 questions of the study all reach the model, and the 32 sentences of 文で操作 their
operation or 画像検索 (4 of them by the model).
"""
import pytest

from mikage.core import chat
from mikage.core import text_command as tc
from mikage.tests.test_text_command import CASES, PLACES, SCREENS

#: The 30 questions of the study of データに質問 (2026-11-24; the bench's questions.py in the project files' data-chat), as typed.
QUESTIONS = [
    "今日の撮像予定は何件？", "11月19日に、システムAで不採用になった依頼の名前と理由を教えて", "先週撮った画像で、呉の船舶の記録は何件？", "今月の観測記録を種別ごとに数えて、多い順に", "あさぎりを最後に確認したのはいつで、どこ？",
    "有効な定点の依頼を、優先度の高い順に5件", "11月の通報を通報者ごとに数えて", "モデル yolo-ship-v3 の検出で、確信度0.8以上のものはクラスごとに何件？", "備考に「出港」とある記録を新しい順に10件、画像IDと備考で",
    "佐世保の記録のうち、AIの結果から取り込んだものは何件？", "昨日までの今月の撮像予定のうち、撮像済みになった割合をシステムごとに", "今、障害中の衛星は？", "対応中のイベントに紐付いた観測記録は何件？",
    "先週、記録が一番多かった地名は？", "山田さんが今月記録した件数は？", "直近7日で消失になった記録を、地名とモノの名前と撮像日時で", "昨日までの撮像予定で、撮像結果が未判定のまま残っているのは何件？",
    "分担で作業中の画像は、誰が何枚持ってる？", "呉の記録で長さ200m以上のものは？種別と長さで", "11月に撮ったSAR画像の数をセンサーごとに", "横須賀の記録数を、先月と今月で比べて", "いちばん新しい引き継ぎメモは誰が書いた？",
    "そのうち推定のものは？", "それを活動ごとに分けて", "明日の呉の天気は？", "依頼グループごとに、有効な依頼の数は？", "今月撮った画像のうち、AIの検出結果があるのに観測記録が1件もない画像は何枚？",
    "「呉 定点」の依頼は今月何回撮れた？", "通報した記録の種別で多いものを3つ", "「呉 定点」の依頼を停止にして",
]


def _route(text, shown=None, ops=tc.OPERATIONS):
    return chat.route(text, shown, ops, {}, SCREENS, known_place=any(p in text for p in PLACES))


@pytest.mark.parametrize("text", QUESTIONS)
def test_a_question_reaches_the_model(text):
    """Not an operation by its words (文で操作's field sent 10 of these to one: 「今日の撮像予定は何件？」 → 撮像予定)."""
    assert _route(text).kind == chat.MODEL


#: The sentences of 文で操作 the model is still asked for (no operation's words, or a question).
BY_MODEL = {"来週月曜のB系の依頼を出力", "今年の年間上限はあとどれくらい残ってる？", "佐世保の撮像予定をタイムラインで見たい", "35.3, 139.6 を表示して"}


@pytest.mark.parametrize("text, key", [(t, k) for t, k, _v in CASES], ids=[c[0] for c in CASES])
def test_an_operation_by_its_words_or_images_searched(text, key):
    r = _route(text)
    if text in BY_MODEL:
        assert r.kind == chat.MODEL
    elif r.kind == chat.IMAGES:
        assert key == "catalog"
    else:
        assert r.kind == chat.OPERATION and r.operation.key == key


def test_images_of_a_place_only_with_a_place_and_a_word_of_looking():
    assert chat.image_search("呉で今週のSAR画像ある？", known_place=True, point=False)
    assert chat.image_search("この辺の昨日の画像を見せて", known_place=False, point=False)
    assert chat.image_search("35.3, 139.6 の画像を探して", known_place=False, point=True)
    # No place known (「数」 is no place), a question for the tables, no word of looking.
    assert not chat.image_search("今月、画像カタログに入った画像は何枚？", known_place=False, point=False)
    assert not chat.image_search("呉の画像の記録は何件？", known_place=True, point=False)
    assert not chat.image_search("呉の画像", known_place=True, point=False)
    # Without 画像検索 (the roles in use), the tables or the model.
    ops = [o for o in tc.OPERATIONS if o.key != "catalog"]
    assert _route("呉の先週の画像を探して", ops=ops).kind != chat.IMAGES


def test_a_follow_up_opens_what_the_answer_before_listed():
    shown = chat.Shown("image", ("A", "B", "C"), ("2026-11-20T01:00:00.000Z", "2026-11-27T01:00:00.000Z", "2026-11-25T01:00:00.000Z"))
    assert chat.follow_up("いちばん新しいのを開いて", shown) == [1]
    assert chat.follow_up("一番古い画像を表示して", shown) == [0]
    assert chat.follow_up("2枚目を地図で", shown) == [1]
    assert chat.follow_up("三番目を開いて", shown) == [2]
    assert chat.follow_up("全部開いて", shown) == [0, 1, 2]
    assert chat.follow_up("C を開いて", shown) == [2]
    assert chat.follow_up("最後のを開いて", shown) == [2]
    assert chat.follow_up("それを開いて", shown) == [0]
    # Out of the list, no word of opening, nothing shown.
    assert chat.follow_up("5枚目を開いて", shown) is None
    assert chat.follow_up("いちばん新しいのはいつ？", shown) is None
    assert chat.follow_up("2枚目を開いて", None) is None
    # 「この範囲」 is the map's, not the list's.
    assert chat.follow_up("この範囲の画像を見せて", shown) is None
    r = _route("いちばん新しいのを開いて", shown)
    assert r.kind == chat.FOLLOW and r.opened == (1,)
    # An operation its words name is not a follow-up (「その日の計画ファイルを出して」).
    assert _route("その日の計画ファイルを出して", shown).kind == chat.OPERATION
    # The most opened at once.
    many = chat.Shown("image", tuple(str(i) for i in range(30)))
    assert len(chat.follow_up("全部開いて", many)) == chat.OPEN_LIMIT


def test_what_an_answer_lists_for_a_follow_up():
    labels = {"IMAGE_ID": "画像ID", "ACQUIRED_AT": "撮像日時", "OBS_ID": "記録ID", "SENSOR": "センサー"}
    shown = chat.shown_of(["IMAGE_ID", "SENSOR", "ACQUIRED_AT"], [("A", "S1", "2026-11-20"), (None, "S2", "x"), ("B", "S1", None)], labels)
    assert shown == chat.Shown("image", ("A", "B"), ("2026-11-20", ""))
    shown = chat.shown_of(["obs_id", "OBJECT_NAME"], [(10, "あさぎり")], labels)
    assert shown.kind == "record" and shown.ids == ("10",)
    assert chat.shown_of(["COUNT(*)"], [(3,)], labels) is None
