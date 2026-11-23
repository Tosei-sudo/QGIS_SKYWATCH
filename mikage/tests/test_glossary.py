"""備考の単語集 (core/obs/glossary.py): the words for the part being typed, other spellings, the table and the settings."""
import pytest

from mikage.core.config import parse_config
from mikage.core.esri import EsriClient, EsriError
from mikage.core.obs.glossary import DEFAULT_GLOSSARY_FIELDS, Glossary, Term, fold, listed, load_glossary
from mikage.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://example.com/arcgis/rest/services/Glossary/FeatureServer/0"

WORDS = Glossary([
    Term("入港", variants=("入航", "入湊"), readings=("にゅうこう",), kinds=("船舶",)),
    Term("入港直後", kinds=("船舶",)),
    Term("停泊中", readings=("ていはくちゅう",)),
    Term("停船", variants=("停止",)),
    Term("停船中"),
    Term("コンテナ"),
    Term("AIS", variants=("ＡＩＳ信号",)),
    Term("タグボート", variants=("タグ", "曳船")),
    Term("駐機", kinds=("航空機",), description="駐機場に止まっている", order=1),
    Term("離陸", kinds=("航空機",), order=2),
])


def words(before: str, root: str = "") -> list[tuple[str, str, str]]:
    return [(s.term.text, s.typed, s.via) for s in WORDS.suggest(before, root)]


def test_the_end_of_the_text_before_the_cursor_is_completed_not_the_whole_field():
    assert words("入") == [("入港", "入", ""), ("入港直後", "入", "")]
    # Mid-sentence, no spaces: the longest end that starts a word.
    assert words("岸壁に入") == [("入港", "入", ""), ("入港直後", "入", "")]
    s = WORDS.suggest("岸壁に入")[0]
    assert s.start == 3 and "岸壁に入"[:s.start] + s.term.text == "岸壁に入港"
    # After punctuation and spaces the part typed starts again.
    assert words("停泊中、入") == [("入港", "入", ""), ("入港直後", "入", "")]
    assert words("停泊中 停")[0][1] == "停"
    # A word written out: only the longer ones that start with it.
    assert words("入港") == [("入港直後", "入港", "")]
    assert words("入港直後") == []
    assert words("") == [] and words("、") == []


def test_words_of_the_kind_being_recorded_and_of_every_kind():
    assert [w for w, *_ in words("入", "船舶")] == ["入港", "入港直後"]
    assert words("入", "航空機") == []
    # The table's order first (駐機 1, 離陸 2), then the 大分類's own, then shorter.
    assert [w for w, *_ in words("駐", "航空機")] == ["駐機"]
    assert WORDS.words("航空機")[:2] == ["駐機", "離陸"]
    assert "入港" not in WORDS.words("航空機") and "停泊中" in WORDS.words("航空機")


def test_readings_other_spellings_and_kana_width_case():
    assert words("にゅう") == [("入港", "にゅう", "reading")]
    # One hiragana is a particle, and hiragana inside a run of them is not where a word starts.
    assert words("に") == [] and words("停泊していて") == []
    # Another spelling typed in full: the word to write instead, first.
    assert words("入航") == [("入港", "入航", "入航")]
    assert "入航 は単語集では 入港" in WORDS.suggest("入航")[0].label
    assert words("こん") == [("コンテナ", "こん", "")]
    assert words("ｺﾝ") == [("コンテナ", "ｺﾝ", "")]
    assert words("ai") == [("AIS", "ai", "")]
    # Letters inside a word of letters are not a word started.
    assert words("CHAI") == []


def test_other_spellings_found_in_the_text_and_fixed():
    found = WORDS.variants_in("入航した。曳船2隻")
    assert [(v.found, v.term, v.start, v.end) for v in found] == [("入航", "入港", 0, 2), ("曳船", "タグボート", 5, 7)]
    assert WORDS.fixed("入航した。曳船2隻") == "入港した。タグボート2隻"
    # Width and case: ＡＩＳ, ais, half-width katakana (the voiced mark is one character with the one before).
    assert WORDS.fixed("ＡＩＳ信号あり、ais受信") == "AISあり、AIS受信"
    assert WORDS.fixed("ﾀｸﾞﾎﾞｰﾄ2隻") == "タグボート2隻"
    # Not inside a longer word of the glossary, not inside a word of letters, not hiragana that looks like it.
    assert WORDS.variants_in("タグボート") == [] and WORDS.variants_in("停船中") == []
    assert WORDS.variants_in("chairs") == [] and WORDS.variants_in("したぐらい") == []
    assert [v.found for v in WORDS.variants_in("停船中で停止")] == ["停止"]
    assert Glossary().variants_in("入航") == [] and Glossary().fixed("入航") == "入航"


def test_a_spelling_that_is_a_word_of_the_kind_is_not_another_spelling():
    g = Glossary([Term("着水", variants=("着陸",), kinds=("飛行艇",)), Term("着陸", kinds=("航空機",))])
    assert [v.found for v in g.variants_in("着陸", "飛行艇")] == ["着陸"]
    assert g.variants_in("着陸", "航空機") == []


def test_one_word_on_two_rows_is_one_word():
    g = Glossary([Term("停泊", variants=("錨泊",), kinds=("船舶",)), Term("停泊", variants=("係留",), kinds=("潜水艦",)),
                  Term(" "), Term("停泊2", kinds=())])
    assert len(g) == 2 and g.terms[0].variants == ("錨泊", "係留") and g.terms[0].kinds == ("船舶", "潜水艦")
    # Blank 大分類 on either row: every 大分類.
    assert Glossary([Term("a", kinds=("x",)), Term("a")]).terms[0].kinds == ()
    # The word itself among its spellings is left out.
    assert Glossary([Term("入港", variants=("入港", "入航"))]).terms[0].variants == ("入航",)


def test_helpers():
    assert listed("入航, 入湊、入港;") == ("入航", "入湊", "入港") and listed(None) == () and listed(["a", " ", "a"]) == ("a",)
    assert fold("ＡＩＳ　コンテナ") == "ais こんてな"


def _table(rows, fields=None):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: "esriFieldTypeString" for n in (fields or DEFAULT_GLOSSARY_FIELDS.values())}, geometry=""))
    if "SORT_ORDER" in layer.types:
        layer.types["SORT_ORDER"] = "esriFieldTypeInteger"
    for r in rows:
        layer.add(r)
    return fake, layer


def test_reads_the_table_with_the_attributes_it_has():
    fake, _ = _table([
        {"TERM": "入港", "VARIANTS": "入航,入湊", "READING": "にゅうこう", "ROOT_KIND": "船舶", "DESCRIPTION": "港に入る", "SORT_ORDER": 2},
        {"TERM": "停泊中", "SORT_ORDER": 1},
        {"TERM": "", "VARIANTS": "x"},
    ])
    config, problems = parse_config({"glossary": URL})
    assert problems == [] and config.glossary.url == URL and config.glossary.fields == DEFAULT_GLOSSARY_FIELDS
    g = load_glossary(EsriClient(fake), config.glossary)
    assert [t.text for t in g.terms] == ["入港", "停泊中"] and g.words() == ["停泊中", "入港"]
    assert g.terms[0] == Term("入港", ("入航", "入湊"), ("にゅうこう",), ("船舶",), "港に入る", 2.0)
    # Only the word is needed; the other attributes are asked for only when the table has them.
    fake, _ = _table([{"WORD": "停泊中"}], fields=["WORD"])
    config, problems = parse_config({"glossary": {"url": URL, "fields": {"term": "WORD"}, "where": "1=1"}})
    assert problems == [] and [t.text for t in load_glossary(EsriClient(fake), config.glossary).terms] == ["停泊中"]
    assert "outFields" in fake.asked[-1][1] and fake.asked[-1][1]["outFields"] == "WORD"
    fake, _ = _table([], fields=["OTHER"])
    with pytest.raises(EsriError, match="単語集の表に属性 TERM なし"):
        load_glossary(EsriClient(fake), parse_config({"glossary": URL})[0].glossary)


def test_settings_problems():
    assert parse_config({"glossary": "nope"})[1] == ["glossary は …/FeatureServer/0 のようなレイヤー・テーブルの URL で指定"]
    assert parse_config({"glossary": {"url": URL, "fields": {"word": "X"}}})[1][0].startswith("glossary.fields.word は使えない役割")
    assert parse_config({})[0].glossary is None
