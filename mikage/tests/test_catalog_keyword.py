"""
「フリーワード」 of the image catalog search: the settings file's
`fields.keywords`, the words as LIKE conditions (values, never SQL), the
watches and 「類似画像の検索」 keeping them, and a search against the fake.
"""
import json

import pytest

from mikage.core.catalog import catalog_of
from mikage.core.catalog.config import CatalogConfig
from mikage.core.catalog.query import EMPTY_SEARCH, MAX_WORDS, keyword_condition, where_of, word_variants, words_of
from mikage.core.catalog.search import read_catalog_layer, search_catalog
from mikage.core.catalog.similar import DEFAULT_TOLERANCE, similar_search
from mikage.core.catalog.watch import CatalogWatch, conditions_text, new_watch, watch_query
from mikage.core.esri import EsriClient, EsriError, condition_problem, contains, like_text
from mikage.tests.fake_esri import FakeEsri, FakeLayer

URL = "https://gis.example.com/arcgis/rest/services/Catalog/FeatureServer/0"
ENTRY = {
    "label": "衛星画像", "url": URL,
    "fields": {"id": "IMG_ID", "sensor": "SENSOR", "acquired": "ACQ", "source": "PATH", "keywords": ["NOTE", "IMG_ID", "AREA"]},
    "columns": [{"field": "NOTE", "label": "備考"}],
}
CATALOG = catalog_of(ENTRY, [], 0)
TYPES = {"IMG_ID": "esriFieldTypeString", "NOTE": "esriFieldTypeString", "AREA": "esriFieldTypeString", "SENSOR": "esriFieldTypeString"}


def test_keywords_are_one_name_or_several():
    problems = []
    assert CATALOG.keyword_fields == ["NOTE", "IMG_ID", "AREA"]
    one = catalog_of({**ENTRY, "fields": {**ENTRY["fields"], "keywords": "NOTE"}}, problems, 0)
    assert one.keyword_fields == ["NOTE"] and problems == []
    # Headings: a column's, a role's, else the attribute name.
    assert CATALOG.keyword_labels() == ["備考", "画像ID", "AREA"]
    none = catalog_of({**ENTRY, "fields": {"source": "PATH"}}, problems, 0)
    assert none.keyword_fields == [] and "keywords" not in none.fields
    bad = catalog_of({**ENTRY, "fields": {"source": "PATH", "keywords": ["NOTE", "NOTE", "x; DROP", 3]}}, problems, 1)
    assert bad.keyword_fields == ["NOTE"]
    assert problems == ["imageCatalogs[1] の fields.keywords は属性名（英数字と _）か、その配列で指定"]


def test_a_watch_keeps_the_keyword_fields_with_its_catalog():
    again = CatalogConfig.from_json(json.loads(json.dumps(CATALOG.to_json())))
    assert again.keyword_fields == ["NOTE", "IMG_ID", "AREA"]
    # A watch made before 「フリーワード」 has none.
    old = CatalogConfig.from_json({**CATALOG.to_json(), "fields": {"id": "IMG_ID", "source": ["PATH"]}})
    assert old.keyword_fields == []


def test_words_are_split_at_any_space_once_each():
    assert words_of("  横須賀　臨時 横須賀\tEV01\n") == ["横須賀", "臨時", "EV01"]
    assert words_of("a\x00b") == ["a", "b"] and words_of("") == [] and words_of("　 ") == []


def test_each_word_is_found_in_any_keyword_field_partly_and_any_case():
    s = EMPTY_SEARCH.with_(keyword="tokyo 臨時")
    assert where_of(CATALOG, TYPES, s) == (
        "(UPPER(NOTE) LIKE '%TOKYO%' ESCAPE '\\' OR UPPER(NOTE) LIKE '%ＴＯＫＹＯ%' ESCAPE '\\'"
        " OR UPPER(IMG_ID) LIKE '%TOKYO%' ESCAPE '\\' OR UPPER(IMG_ID) LIKE '%ＴＯＫＹＯ%' ESCAPE '\\'"
        " OR UPPER(AREA) LIKE '%TOKYO%' ESCAPE '\\' OR UPPER(AREA) LIKE '%ＴＯＫＹＯ%' ESCAPE '\\')"
        " AND (UPPER(NOTE) LIKE '%臨時%' ESCAPE '\\' OR UPPER(IMG_ID) LIKE '%臨時%' ESCAPE '\\' OR UPPER(AREA) LIKE '%臨時%' ESCAPE '\\')")
    # With the rest: after the catalog's where and the person's SQL.
    both = where_of(catalog_of({**ENTRY, "where": "STATUS = 'OK'"}, [], 0), TYPES, EMPTY_SEARCH.with_(sensor="S1", where="CLOUD < 10", keyword="臨時"))
    assert both.startswith("(STATUS = 'OK') AND SENSOR = 'S1' AND (CLOUD < 10) AND (UPPER(NOTE) LIKE '%臨時%'")
    # Nothing typed, or a catalog without keyword fields: no condition.
    assert where_of(CATALOG, TYPES, EMPTY_SEARCH.with_(keyword=" 　")) == "1=1"
    plain = catalog_of({**ENTRY, "fields": {"source": "PATH"}}, [], 0)
    assert where_of(plain, TYPES, EMPTY_SEARCH.with_(keyword="臨時")) == "1=1"


def test_the_ways_a_word_may_be_written():
    assert word_variants("臨時") == ["臨時"]
    assert word_variants("ＷＶ０３") == ["ＷＶ０３", "WV03"]
    assert word_variants("wv03") == ["wv03", "ｗｖ０３"]
    assert word_variants("よこすか") == ["よこすか", "ヨコスカ"]
    assert word_variants("ﾖｺｽｶ") == ["ﾖｺｽｶ", "ヨコスカ", "よこすか"]


def test_typed_words_are_values_never_sql():
    """Quotes, LIKE wildcards and the escape character are taken as they are (SQL injection checks)."""
    attacks = ["x' OR '1'='1", "') OR (1=1", "%", "_", "50%_off", "a\\", "\\%", "1=1; DROP TABLE T --", "/* x */", "'", "''", "a') --"]
    for text in attacks:
        parts = keyword_condition(CATALOG, TYPES, text)
        assert len(parts) == len(words_of(text)), text
        for part in parts:
            # Each condition is whole (no quote or parenthesis left open, nothing after it) whatever was typed.
            assert condition_problem(part) is None, text
            assert part.startswith("(UPPER(NOTE) LIKE '%") and part.endswith("%' ESCAPE '\\')"), text
        assert condition_problem(where_of(CATALOG, TYPES, EMPTY_SEARCH.with_(keyword=text))) is None, text
    assert like_text("50%_o'k\\") == "50\\%\\_o''k\\\\"
    assert contains("NOTE", "50%") == "NOTE LIKE '%50\\%%' ESCAPE '\\'"
    assert contains("NOTE", "o'k", any_case=True) == "UPPER(NOTE) LIKE '%O''K%' ESCAPE '\\'"


def test_too_many_or_too_long_words_and_non_text_fields_are_refused():
    with pytest.raises(EsriError, match=f"{MAX_WORDS} 語まで"):
        keyword_condition(CATALOG, TYPES, " ".join(f"w{i}" for i in range(MAX_WORDS + 1)))
    with pytest.raises(EsriError, match="100 文字まで"):
        keyword_condition(CATALOG, TYPES, "x" * 101)
    # Only text attributes are asked (LIKE on a number fails on most databases); a type not known is tried.
    numbers = {**TYPES, "IMG_ID": "esriFieldTypeInteger"}
    assert "IMG_ID" not in keyword_condition(CATALOG, numbers, "臨時")[0]
    assert "UPPER(AREA)" in keyword_condition(CATALOG, {"NOTE": "esriFieldTypeString"}, "臨時")[0]
    with pytest.raises(EsriError, match="文字列の属性なし"):
        keyword_condition(CATALOG, {"NOTE": "esriFieldTypeDouble", "IMG_ID": "esriFieldTypeOID", "AREA": "esriFieldTypeDate"}, "臨時")


def test_watches_and_similar_images_keep_the_free_word():
    w = new_watch("臨時", CATALOG, TYPES, EMPTY_SEARCH.with_(acquired_from="2026-01-01", keyword="臨時 EV01"), [], now=0)
    assert w.conditions.keyword == "臨時 EV01" and w.conditions.acquired_from == ""
    again = CatalogWatch.from_json(json.loads(json.dumps(w.to_json())))
    assert again.conditions.keyword == "臨時 EV01"
    assert "UPPER(NOTE) LIKE '%臨時%'" in watch_query(again, None)["where"]
    assert conditions_text(CATALOG, w.conditions, []) == "衛星画像 ・ 範囲なし ・ フリーワード「臨時 EV01」"
    # A watch kept before (no keyword) reads as none.
    old = w.to_json()
    del old["conditions"]["keyword"]
    assert CatalogWatch.from_json(old).conditions.keyword == ""
    from mikage.core.catalog.search import record_of

    reference = record_of({"attributes": {"IMG_ID": "R", "SENSOR": "S1", "PATH": "x"}}, CATALOG.fields)
    assert similar_search(CATALOG, reference, DEFAULT_TOLERANCE, EMPTY_SEARCH.with_(keyword="臨時")).keyword == "臨時"


def test_a_search_finds_only_the_images_holding_every_word():
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({**TYPES, "ACQ": "esriFieldTypeDate", "PATH": "esriFieldTypeString"}, "esriGeometryPolygon"))
    rows = [("A", "横須賀港 定期観測", "神奈川"), ("B", "横須賀港 臨時観測（ＥＶ０１）", None), ("C", "東京港 臨時 50%雲", "東京"),
            ("D", "東京港 臨時 500雲", "東京"), ("WV03_X", None, None)]
    for n, (image_id, note, area) in enumerate(rows):
        layer.add({"IMG_ID": image_id, "NOTE": note, "AREA": area, "ACQ": n, "PATH": f"https://x/{image_id}.tif"})
    client = EsriClient(fake)
    info = read_catalog_layer(client, CATALOG, None)

    def ids(keyword):
        return sorted(r.id for r in search_catalog(client, CATALOG, info, EMPTY_SEARCH.with_(keyword=keyword), None, None).records)

    assert ids("臨時") == ["B", "C", "D"]
    assert ids("臨時 横須賀") == ["B"]
    assert ids("ev01") == ["B"]  # full-width in the catalog, small letters typed
    assert ids("50%") == ["C"]  # % is a character, not "anything"
    assert ids("%") == ["C"]
    assert ids("_") == ["WV03_X"]
    assert ids("wv03") == ["WV03_X"]  # the image ID too
    assert ids("東京 神奈川") == []
    assert ids("x' OR '1'='1") == []
