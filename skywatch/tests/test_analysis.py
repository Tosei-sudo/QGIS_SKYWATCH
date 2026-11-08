"""「データ分析」 without QGIS (core/analysis.py): tables of the settings file, SQL reading, the builder, reading only, CSV."""
import pytest

from skywatch.core.analysis import (
    Analysis, AnalysisError, Builder, Column, Database, Figure, Join, Table, analyses_of, analyses_text, build_sql, csv_text,
    esri_tables, esri_wkt, examples, parameters, read_esri, referenced, suggest_join, table_name, wants_shape,
)
from skywatch.core.config import parse_config
from skywatch.core.esri import EsriClient
from skywatch.tests.fake_esri import FakeEsri, FakeLayer

CAT = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
AI = "https://example.com/arcgis/rest/services/AI/FeatureServer/0"
AI2 = "https://example.com/arcgis/rest/services/AI/FeatureServer/1"
EVENTS = "https://example.com/arcgis/rest/services/Events/FeatureServer/0"
LINKS = "https://example.com/arcgis/rest/services/Events/FeatureServer/1"
S, D, T = "esriFieldTypeString", "esriFieldTypeDouble", "esriFieldTypeDate"

CONFIG = {
    "imageCatalogs": [{"label": "衛星画像", "url": CAT, "fields": {"id": "IMG_ID", "acquired": "ACQ", "source": "PATH"}}],
    "aiResultSources": [
        {"label": "船舶検出", "url": AI, "fields": {"imageId": "IMAGE_ID", "class": "CLS", "score": "SCORE", "model": "MODEL"}},
        {"label": "車両", "url": AI2, "fields": {"imageId": "IMG", "class": "CLS"}},
    ],
    "events": {"layer": EVENTS, "links": LINKS},
}


def tables():
    config, problems = parse_config(CONFIG)
    assert problems == []
    return esri_tables(config)


def test_the_tables_of_the_settings_file_are_named_as_the_readme():
    ts = tables()
    assert [t.name for t in ts] == ["TBL_IMAGE_CATALOG", "TBL_EVENT", "TBL_EVENT_LINK", "TBL_AI_RESULT", "TBL_AI_RESULT_2"]
    assert ts[3].title == "外部 AI の結果（船舶検出）" and ts[3].roles["model"] == "MODEL" and ts[0].roles["id"] == "IMG_ID"
    assert ts[2].roles["event"] == "EVENT_ID"
    # Joins from the roles: AI result → catalog, link → event.
    assert suggest_join(ts[3], ts[0]) == ("IMAGE_ID", "IMG_ID")
    assert suggest_join(ts[0], ts[4]) == ("IMG_ID", "IMG")
    assert suggest_join(ts[1], ts[2]) == ("EVENT_ID", "EVENT_ID")
    # Two project layers: the same attribute name, not the ids.
    a = Table("A", "A", "layer", columns=[Column("fid", "INTEGER"), Column("code")])
    b = Table("B", "B", "layer", columns=[Column("fid", "INTEGER"), Column("CODE")])
    assert suggest_join(a, b) == ("code", "CODE")
    assert table_name("観測 点(2)") == "観測_点_2" and table_name("2026 ships") == "_2026_ships"


def test_reading_the_sql():
    sql = "SELECT a.x, ':nope' FROM tbl_ai_result a -- TBL_EVENT :hidden\nJOIN \"TBL_IMAGE_CATALOG\" c ON c.ID = a.ID WHERE a.M = :model AND a.S >= :min OR a.M = :model"
    assert referenced(sql, ["TBL_AI_RESULT", "TBL_IMAGE_CATALOG", "TBL_EVENT"]) == ["TBL_AI_RESULT", "TBL_IMAGE_CATALOG"]
    assert parameters(sql) == ["model", "min"]
    assert not wants_shape(sql) and wants_shape("select SHAPE from x") and not wants_shape("select 'SHAPE' from x")
    # A time of day is no parameter.
    assert parameters("select time('12:30:00')") == [] and parameters("select a::text") == []


def test_only_reading_statements_run():
    db = Database()
    db.add("T", [Column("A"), Column("N", "INTEGER")], [("x", 1), ("y", 2)])
    for sql in ["DELETE FROM T", "DROP TABLE T", "ATTACH DATABASE '/tmp/x.db' AS x", "PRAGMA table_info(T)", "CREATE TABLE U (a)",
                "UPDATE T SET N = 3", "INSERT INTO T VALUES ('z', 3)"]:
        with pytest.raises(AnalysisError, match="読み取り"):
            db.run(sql)
    with pytest.raises(AnalysisError, match="1 つ"):
        db.run("SELECT 1; SELECT 2")
    with pytest.raises(AnalysisError, match="表がない: U"):
        db.run("SELECT * FROM U")
    with pytest.raises(AnalysisError, match=":v"):
        db.run("SELECT * FROM T WHERE A = :v")
    # Still all there; the column's type makes a value given as text a number.
    r = db.run("WITH s AS (SELECT * FROM T WHERE N >= :n) SELECT A, N * 10 AS M FROM s ORDER BY A", {"n": "2"})
    assert r.columns == ["A", "M"] and r.rows == [("y", 20)]
    r = db.run("SELECT A FROM T", limit=1)
    assert r.rows == [("x",)] and r.cut


def test_the_builder_joins_groups_and_counts():
    db = Database()
    db.add("TBL_AI_RESULT", [Column("IMAGE_ID"), Column("CLS"), Column("MODEL"), Column("SCORE", "REAL")],
           [("A", "ship", "m1", 0.9), ("A", "ship", "m1", 0.7), ("A", "car", "m1", 0.8), ("B", "ship", "m1", 0.6), ("C", "ship", "m2", 0.9)])
    db.add("TBL_IMAGE_CATALOG", [Column("IMG_ID"), Column("ACQ")], [("A", "2026-11-01 10:00:00"), ("B", "2026-11-02 11:00:00"), ("D", "x")])
    b = Builder("TBL_AI_RESULT", [Join("TBL_IMAGE_CATALOG", "IMAGE_ID", "IMG_ID")], 't0."MODEL" = :model',
                ["t0.IMAGE_ID", "t1.ACQ", "t0.CLS"], [Figure("count"), Figure("avg", "t0.SCORE")])
    sql = build_sql(b)
    assert "JOIN \"TBL_IMAGE_CATALOG\" AS t1 ON t0.\"IMAGE_ID\" = t1.\"IMG_ID\"" in sql and "GROUP BY" in sql
    r = db.run(sql, {"model": "m1"})
    assert r.columns == ["IMAGE_ID", "ACQ", "CLS", "件数", "SCOREの平均"]
    assert r.rows[0][:4] == ("A", "2026-11-01 10:00:00", "ship", 2) and r.rows[0][4] == pytest.approx(0.8)
    assert sorted(x[:4] for x in r.rows[1:]) == [("A", "2026-11-01 10:00:00", "car", 1), ("B", "2026-11-02 11:00:00", "ship", 1)]
    # Images without results stay with a left join; the same column of two tables gets its table's name.
    b2 = Builder("TBL_IMAGE_CATALOG", [Join("TBL_AI_RESULT", "IMG_ID", "IMAGE_ID", "left")], "", ["t0.IMG_ID"],
                 [Figure("countDistinct", "t1.CLS", "クラスの数")], descending=False)
    assert db.run(build_sql(b2)).rows == [("D", 0), ("B", 1), ("A", 2)]
    b3 = Builder("TBL_AI_RESULT", [Join("TBL_AI_RESULT", "IMAGE_ID", "IMAGE_ID")], "", ["t0.CLS", "t1.CLS"], [Figure("count")])
    assert db.run(build_sql(b3)).columns == ["CLS", "t1_CLS", "件数"]
    with pytest.raises(AnalysisError, match="合計"):
        build_sql(Builder("T", figures=[Figure("sum")]))
    assert build_sql(Builder("T")) == 'SELECT t0.*\nFROM "T" AS t0'


def test_examples_and_their_round_trip():
    found = examples(tables())
    names = [a.name for a in found]
    assert names[0] == "外部 AI の結果（船舶検出）: モデルを指定して画像・クラスごとの件数"
    assert "外部 AI の結果（車両）: 画像・クラスごとの件数" in names and "イベント: 種類ごとの項目の数" in names
    first = found[0]
    assert parameters(first.sql) == ["model"] and "TBL_IMAGE_CATALOG" in first.sql
    a = Analysis("船舶", first.sql, first.builder, {"model": "m1"}, {"TBL_AI_RESULT": "SCORE >= 0.5", "X": ""}, "sql")
    back = analyses_of(analyses_text([a, Analysis("空")]) )
    assert [x.name for x in back] == ["船舶", "空"]
    assert back[0].builder == first.builder and back[0].values == {"model": "m1"} and back[0].conditions == {"TBL_AI_RESULT": "SCORE >= 0.5"}
    assert back[0].mode == "sql" and back[1].mode == "builder"
    assert analyses_of("not json") == [] and analyses_of('{"name": "x"}') == [] and analyses_of('[{"name": ""}, 3]') == []


def test_an_esri_table_is_read_with_its_conditions_dates_and_shapes():
    fake = FakeEsri()
    layer = fake.add_layer(AI, FakeLayer({"IMAGE_ID": S, "CLS": S, "SCORE": D, "MODEL": S, "AT": T}, "esriGeometryPolygon"))
    ring = [[140.0, 35.0], [140.0, 35.1], [140.1, 35.1], [140.1, 35.0], [140.0, 35.0]]
    layer.add({"IMAGE_ID": "A", "CLS": "ship", "SCORE": 0.9, "MODEL": "m1", "AT": 1762560000000}, {"rings": [ring]})
    layer.add({"IMAGE_ID": "B", "CLS": "ship", "SCORE": 0.2, "MODEL": "m2", "AT": None}, {"rings": [ring]})
    t = tables()[3]
    t.where = "SCORE >= 0.1"
    columns, rows = read_esri(EsriClient(fake), t, "MODEL = 'm1'", shape=True)
    names = [c.name for c in columns]
    assert names[-1] == "SHAPE" and {"IMAGE_ID", "SCORE", "AT"} <= set(names) and t.spatial
    assert len(rows) == 1
    row = dict(zip(names, rows[0]))
    assert row["IMAGE_ID"] == "A" and row["SCORE"] == 0.9 and row["SHAPE"].startswith("POLYGON ((140")
    assert len(row["AT"]) == 19 and row["AT"][4] == "-"
    assert next(c for c in columns if c.name == "IMAGE_ID").role == "imageId"
    assert next(c for c in columns if c.name == "SCORE").type == "REAL"
    with pytest.raises(AnalysisError, match="上限"):
        read_esri(EsriClient(fake), tables()[3], "", limit=1)


def test_esri_shapes_as_wkt():
    assert esri_wkt({"x": 1.5, "y": 2}) == "POINT (1.5 2.0)"
    assert esri_wkt({"x": None, "y": None}) is None and esri_wkt(None) is None
    assert esri_wkt({"paths": [[[0, 0], [1, 1]]]}) == "LINESTRING (0 0, 1 1)"
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
    hole = [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]]
    other = [[20, 0], [20, 5], [25, 5], [25, 0], [20, 0]]
    assert esri_wkt({"rings": [outer, hole]}).startswith("POLYGON ((0 0") and esri_wkt({"rings": [outer, hole]}).count("(") == 3
    assert esri_wkt({"rings": [outer, hole, other]}).startswith("MULTIPOLYGON (((0 0")


def test_csv_keeps_formulas_out():
    db = Database()
    db.add("T", [Column("A"), Column("N", "REAL")], [("=1+1", -2.0), ("名前,カンマ", 1.5)])
    text = csv_text(db.run("SELECT * FROM T"))
    assert text.splitlines() == ["A,N", "'=1+1,-2", '"名前,カンマ",1.5']
