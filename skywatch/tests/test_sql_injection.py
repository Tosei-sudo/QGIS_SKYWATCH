"""Security audit 2026-11-08: SQL injection and the sandbox of 「データ分析」, people's conditions in Esri queries, formulas in copies."""
import os
import sqlite3

import pytest

from skywatch.core.analysis import (
    AnalysisError, Builder, Column, Database, Figure, Join, Result, build_sql, csv_text, read_esri, tsv_text,
)
from skywatch.core.catalog.query import EMPTY_SEARCH, where_of
from skywatch.core.esri import EsriClient, EsriError, condition_problem, own_condition
from skywatch.tests.fake_esri import FakeEsri, FakeLayer
from skywatch.tests.test_analysis import AI, tables
from skywatch.tests.test_catalog import CATALOG


def database():
    db = Database()
    db.add("T", [Column("A", "INTEGER"), Column("B")], [(1, "x"), (2, "y")])
    db.conn.commit()
    return db


@pytest.mark.parametrize("sql", [
    'DROP TABLE IF EXISTS "T"',            # the very text Database.add ran (a cached statement)
    'CREATE TABLE "T" ("A" INTEGER, "B" TEXT)',
    "INSERT INTO T VALUES (3, 'z')", "UPDATE T SET A = 0 RETURNING *", "DELETE FROM T RETURNING *",
    "ATTACH DATABASE 'x.db' AS x", "VACUUM INTO 'v.db'", "PRAGMA table_info(T)", "SELECT * FROM pragma_table_info('T')",
    "SELECT load_extension('x')", "SELECT fts3_tokenizer('simple')", "SELECT FTS3_TOKENIZER('x', x'4141414141414141')",
    "CREATE TEMP VIEW v AS SELECT 1", "BEGIN", "SAVEPOINT a", "SELECT 1; DROP TABLE T",
])
def test_only_reading_runs(sql, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = database()
    with pytest.raises(AnalysisError):
        db.run(sql)
    assert db.conn.execute("SELECT COUNT(*) FROM T").fetchone() == (2,)
    assert os.listdir(tmp_path) == []


def test_values_are_bound_not_written_into_the_sql():
    db = database()
    assert db.run("SELECT A FROM T WHERE B = :b", {"b": "x' OR '1'='1"}).rows == []
    assert db.run("SELECT A FROM T WHERE B = :b", {"b": "y"}).rows == [(2,)]


def test_no_huge_values():
    db = database()
    if not hasattr(db.conn, "setlimit"):
        pytest.skip("Python < 3.11")
    with pytest.raises(AnalysisError):
        db.run("SELECT length(zeroblob(200000000))")
    with pytest.raises(AnalysisError):
        db.run("ATTACH ':memory:' AS m")
    assert db.conn.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0


def test_the_builder_takes_only_its_own_aliases():
    ok = Builder("T", [Join("U", "A", "A", of="t0")], "", ["t1.B"], [Figure("count")])
    assert 'JOIN "U" AS t1 ON t0."A" = t1."A"' in build_sql(ok)
    with pytest.raises(AnalysisError, match="別名"):
        build_sql(Builder("T", [], "", ["x) UNION SELECT sql FROM sqlite_master --.A"], [Figure("count")]))
    with pytest.raises(AnalysisError, match="別名"):
        build_sql(Builder("T", [Join("U", "A", "A", of="1=1 OR t0")], "", [], []))


def test_a_condition_cannot_leave_the_settings_files_condition_out():
    for bad in ["1=1) OR (1=1", "A = 1)", "(A = 1", "A = 'x", "A = 1; DROP", "A = 1 --", "A = 1 /* x */"]:
        assert condition_problem(bad), bad
        with pytest.raises(EsriError):
            own_condition(bad)
    for good in ["MODEL = 'yolo (v8)'", "MODEL = 'it''s ) here'", "(A = 1 OR B = 2) AND C IN (1, 2)", '"NAME" = 1', ""]:
        assert condition_problem(good) is None, good
    assert own_condition(" A = 1 ") == "(A = 1)"


def test_the_catalog_search_keeps_its_where():
    with pytest.raises(EsriError, match="括弧"):
        where_of(CATALOG, {}, EMPTY_SEARCH.with_(where="1=1) OR (1=1"))
    assert where_of(CATALOG, {}, EMPTY_SEARCH.with_(where="CLOUD < 10")).endswith("AND (CLOUD < 10)")


def test_an_analysis_condition_keeps_the_settings_files_where():
    fake = FakeEsri()
    layer = fake.add_layer(AI, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "MODEL": "esriFieldTypeString"}))
    layer.add({"IMAGE_ID": "A", "MODEL": "m1"})
    t = tables()[3]
    t.where = "MODEL = 'm1'"
    with pytest.raises(AnalysisError, match="読み込み条件"):
        read_esri(EsriClient(fake), t, "1=1) OR (1=1")
    columns, rows = read_esri(EsriClient(fake), t, "IMAGE_ID = 'A'")
    assert [dict(zip([c.name for c in columns], r))["IMAGE_ID"] for r in rows] == ["A"]


def test_copies_keep_formulas_out():
    result = Result(["=HYPERLINK(\"x\")", "N"], [("=cmd|' /C calc'!A0", -2.5), ("@SUM(1)", 3), ("ok", None)])
    csv_lines = csv_text(result).splitlines()
    assert csv_lines[0].startswith("\"'=HYPERLINK") and csv_lines[1].startswith("'=cmd") and csv_lines[1].endswith(",-2.5")
    tsv = tsv_text(result).splitlines()
    assert tsv[0].startswith("'=HYPERLINK") and tsv[1] == "'=cmd|' /C calc'!A0\t-2.5" and tsv[2] == "'@SUM(1)\t3" and tsv[3] == "ok\t"
