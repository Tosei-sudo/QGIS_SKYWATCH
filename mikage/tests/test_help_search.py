"""The help's search (core/help_search.py): words in every section, folding, positions in the text given."""
from mikage.core.help_search import TOP, Index, fold, folded, words_of

TEXT = (
    "MIKAGE の使い方。撮像依頼から観測記録まで。"  # 0: before the first heading
    "撮像依頼\n依頼を登録して、依頼文から下書き。モノの位置。"
    "削除\n依頼を削除するときは確認が出る。ＭＩＫＡＧＥ の画面。"
    "ｺﾝﾃﾅ船\nコンテナ船の記録。"
)
STARTS = [(TEXT.index("撮像依頼\n"), "request"), (TEXT.index("削除\n"), "delete"), (TEXT.index("ｺﾝﾃﾅ船\n"), "ship")]


def _texts(index, found):
    return [index.text[s:e] for s, e in found.hits]


def test_words_and_folding():
    assert words_of(" 依頼　削除  依頼 ") == ["依頼", "削除"] and words_of("") == []
    # Width, case and kana: one way of writing.
    assert fold("ＭＩＫＡＧＥ") == fold("mikage") == "mikage"
    assert fold("モノ") == fold("もの") and fold("ｺﾝﾃﾅ") == fold("コンテナ") and fold("ｶﾞｲﾄﾞ") == fold("ガイド")
    # Each character of the folded text points at the one it comes from.
    text, at = folded("aｶﾞb")
    assert text == "aがb" and at == [0, 1, 3]


def test_hits_are_in_the_sections_with_every_word():
    index = Index(TEXT, STARTS)
    assert index.section_at(0) == TOP and index.section_at(STARTS[1][0] + 3) == "delete"
    found = index.search("依頼")
    # Each where it is in the text; 「依頼」 in 「依頼文」 too.
    assert _texts(index, found) == ["依頼"] * 5
    assert found.counts == {TOP: 1, "request": 3, "delete": 1}
    # Two words: only the sections holding both, both marked.
    found = index.search("依頼 削除")
    assert found.counts == {"delete": 3} and _texts(index, found) == ["削除", "依頼", "削除"]
    assert index.search("依頼 ないことば").hits == [] and index.search("  ").words == []
    # Written another way: found where it is, as it is written there.
    assert _texts(index, index.search("mikage")) == ["MIKAGE", "ＭＩＫＡＧＥ"]
    assert _texts(index, index.search("もの")) == ["モノ"]
    assert _texts(index, index.search("コンテナ")) == ["ｺﾝﾃﾅ", "コンテナ"]


def test_a_hit_inside_another_is_one():
    index = Index(TEXT, STARTS)
    found = index.search("依頼 依頼文")
    assert "依頼文" in _texts(index, found) and "依頼" in _texts(index, found)
    starts = [s for s, _e in found.hits]
    assert starts == sorted(set(starts)) and all(e <= s2 for (_s, e), (s2, _e) in zip(found.hits, found.hits[1:]))


def test_the_first_hit_from_a_place():
    index = Index(TEXT, STARTS)
    found = index.search("依頼")
    delete = STARTS[1][0]
    first = index.first_in(found, delete)
    assert found.hits[first][0] > delete and found.hits[first - 1][0] < delete
    # Within a section: none in 「ship」.
    assert index.first_in(found, STARTS[2][0], len(TEXT)) is None and index.first_in(found, len(TEXT)) is None
