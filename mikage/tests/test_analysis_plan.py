"""
「データ分析」 の読み込みの計画 (core/analysis_plan.py): the columns read, the
conditions and join keys sent, answers kept — they narrow the Esri reading,
never the result. The server is read as Oracle would (ArcSDE on Oracle).
"""
import random
import re
import sqlite3
from datetime import datetime, timezone

import pytest

from mikage.core import analysis_cache, analysis_plan
from mikage.core.analysis import AnalysisError, Builder, Column, Database, Figure, Join, Table, build_sql, read_esri, referenced, wants_shape
from mikage.core.analysis_plan import (
    describe, esri_text, fields_of, key_values, plan_of, prepare_reads, read_tables, tokens, used_columns, worth,
)
from mikage.core.esri import EsriClient, EsriError
from mikage.tests.fake_esri import FakeEsri, FakeLayer

AI = "https://example.com/arcgis/rest/services/AI/FeatureServer/0"
CAT = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
S, D, I, T = "esriFieldTypeString", "esriFieldTypeDouble", "esriFieldTypeInteger", "esriFieldTypeDate"


@pytest.fixture(autouse=True)
def _no_cache():
    analysis_cache.clear()
    yield
    analysis_cache.clear()


def ms(text):
    return datetime.fromisoformat(text.replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp() * 1000


class OracleEsri(FakeEsri):
    """FakeEsri whose WHERE is read as Oracle would: '' is NULL, LIKE minds the case, UPPER of any letter, TIMESTAMP '…' (UTC)."""

    def found(self, layer, form):
        toks = tokens(form.get("where") or "1=1")
        if toks is None:
            raise EsriError("bad where")
        out, i = [], 0
        while i < len(toks):
            k, v = toks[i]
            if k == "word" and v.upper() == "TIMESTAMP" and i + 1 < len(toks) and toks[i + 1][0] == "str":
                out.append(str(ms(toks[i + 1][1][1:-1])))
                i += 2
                continue
            out.append("NULL" if (k, v) == ("str", "''") else v)
            i += 1
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA case_sensitive_like = ON")
            conn.create_function("UPPER", 1, lambda x: x.upper() if isinstance(x, str) else x)
            names = list(layer.types)
            kinds = {"esriFieldTypeString": "TEXT", "esriFieldTypeDouble": "REAL"}
            conn.execute("CREATE TABLE t (" + ", ".join(f'"{n}" {kinds.get(layer.types[n], "INTEGER")}' for n in names) + ", _i INTEGER)")
            conn.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in names)}, ?)",
                             [[None if r["attributes"].get(n) == "" else r["attributes"].get(n) for n in names] + [j]
                              for j, r in enumerate(layer.rows)])
            keep = {j for (j,) in conn.execute(f"SELECT _i FROM t WHERE {' '.join(out)}")}
        except sqlite3.Error as error:
            raise EsriError(f"ORA: {error}") from None
        finally:
            conn.close()
        return [r for j, r in enumerate(layer.rows) if j in keep]


class Reader:
    def __init__(self, columns, rows):
        self.columns, self.rows = columns, rows

    def read(self, _cancelled=None):
        return self.columns, self.rows


TABLES = {"tbl_ai_result": ("TBL_AI_RESULT", {"objectid", "image_id", "model", "score", "at", "cls"}),
          "tbl_image_catalog": ("TBL_IMAGE_CATALOG", {"objectid", "img_id", "sat", "acq"})}


def parts(sql, values=None, tables=TABLES):
    plan = plan_of(sql, values or {}, tables)
    return {a: [p.text for p in ps] for a, ps in plan.parts.items()}


def test_the_parts_of_the_where_and_the_on_go_to_their_table():
    b = Builder("TBL_AI_RESULT", [Join("TBL_IMAGE_CATALOG", "IMAGE_ID", "IMG_ID")],
                't0."MODEL" = :model AND t0.SCORE >= 0.5 AND (t1.ACQ > 1 OR t1.SAT IS NULL) AND t0.CLS LIKE \'s%\' '
                "AND upper(t0.CLS) = 'S' AND t0.SCORE + 1 > 2 AND t0.SCORE NOT BETWEEN 1 AND 3 AND NOT t0.CLS IN ('a', 'b') AND SAT = 'x'",
                ["t0.IMAGE_ID"], [Figure("count")])
    plan = plan_of(build_sql(b), {}, TABLES)
    assert {a: [p.text for p in ps] for a, ps in plan.parts.items()} == {
        "t0": ['t0."MODEL" = :model', 't0."SCORE" >= 0.5', 't0."CLS" LIKE \'s%\'', 't0."SCORE" NOT BETWEEN 1 AND 3',
               "(NOT t0.\"CLS\" IN ('a', 'b'))"],
        # An unqualified column of one table only is that table's.
        "t1": ['(t1."ACQ" > 1 OR t1."SAT" IS NULL)', '"SAT" = \'x\''],
    }
    assert [w for _t, w in plan.kept[""]] == [analysis_plan.WHY_PARSE] * 2
    # AND before OR: `a OR b AND c` is not split (it would be `(a OR b) AND c`); on two tables, it stays.
    assert parts("SELECT * FROM TBL_AI_RESULT a JOIN TBL_IMAGE_CATALOG c ON a.IMAGE_ID = c.IMG_ID "
                 "WHERE a.MODEL = 'm' OR a.CLS = 'x' AND c.SAT = 'A'") == {}
    # Comments are left out; the clauses after the WHERE end it; CASE … END is not split.
    assert parts("SELECT * FROM TBL_AI_RESULT -- WHERE MODEL = 'x'\nWHERE /* AND SCORE > 9 */ SCORE > 1 AND "
                 "CASE WHEN MODEL = 'a' AND SCORE > 2 THEN 1 END = 1 GROUP BY CLS ORDER BY 1 LIMIT 3") == {"tbl_ai_result": ['"SCORE" > 1']}
    # A table named twice (a self join, a subquery) keeps its rows.
    assert parts("SELECT * FROM TBL_AI_RESULT a JOIN TBL_AI_RESULT b ON a.IMAGE_ID = b.IMAGE_ID WHERE a.MODEL = 'x'") == {}
    assert parts("SELECT * FROM TBL_AI_RESULT t WHERE t.SCORE > 1 AND t.MODEL IN (SELECT MODEL FROM TBL_AI_RESULT)") == {}
    # The table's own name as its qualifier, and a comma join.
    assert parts("SELECT * FROM TBL_AI_RESULT, TBL_IMAGE_CATALOG WHERE TBL_AI_RESULT.IMAGE_ID = TBL_IMAGE_CATALOG.IMG_ID "
                 "AND TBL_AI_RESULT.MODEL = 'm'") == {"tbl_ai_result": ['TBL_AI_RESULT."MODEL" = \'m\'']}
    # What is not understood: no plan (the columns are still left out).
    for sql in ["WITH q AS (SELECT 1) SELECT * FROM TBL_AI_RESULT", "SELECT * FROM TBL_AI_RESULT UNION SELECT * FROM TBL_AI_RESULT",
                "SELECT * FROM TBL_AI_RESULT a RIGHT JOIN TBL_IMAGE_CATALOG c ON 1", "SELECT * FROM (SELECT * FROM TBL_AI_RESULT)",
                "SELECT * FROM TBL_AI_RESULT NATURAL JOIN TBL_IMAGE_CATALOG", "SELECT * FROM main.TBL_AI_RESULT",
                "SELECT * FROM TBL_AI_RESULT WHERE SCORE > 1 AND window = 2", "SELECT * FROM NOPE", "SELECT 'x", "SELECT 1",
                "SELECT * FROM TBL_AI_RESULT; SELECT 2"]:
        assert plan_of(sql, {}, TABLES) is None, sql


def test_join_keys_and_the_right_table_of_a_left_join():
    sql = ("SELECT * FROM TBL_IMAGE_CATALOG c LEFT JOIN TBL_AI_RESULT a ON a.IMAGE_ID = c.IMG_ID AND a.MODEL = 'x' AND c.SAT = 'A' "
           "WHERE a.SCORE > 1 AND (a.MODEL IS NULL OR a.SCORE > 1) AND NOT a.SCORE IS NOT NULL AND a.CLS <> :c AND c.ACQ = 1")
    plan = plan_of(sql, {"c": "car"}, TABLES)
    # The WHERE: only what is not true for a missing row; the ON: its right table only.
    assert {a: [p.text for p in ps] for a, ps in plan.parts.items()} == {
        "a": ['a."SCORE" > 1', 'a."CLS" <> :c', 'a."MODEL" = \'x\''], "c": ['c."ACQ" = 1']}
    assert [w for _t, w in plan.kept["a"]] == [analysis_plan.WHY_NULL, analysis_plan.WHY_NULL]
    assert [w for _t, w in plan.kept["c"]] == [analysis_plan.WHY_ON]
    # Keys: from the kept side to the other only.
    assert [(k.source, k.target) for k in plan.keys] == [("c", "a")]
    b = Builder("A", [Join("B", "X", "Y"), Join("C", "Z", "W", "inner", "t1")])
    tables = {n.lower(): (n, None) for n in "ABC"}
    assert [(k.source, k.source_column, k.target, k.target_column) for k in plan_of(build_sql(b), {}, tables).keys] == [
        ("t0", "X", "t1", "Y"), ("t1", "Y", "t0", "X"), ("t1", "Z", "t2", "W"), ("t2", "W", "t1", "Z")]
    # An `a.X = b.Y` of the WHERE: both ways.
    assert [(k.source, k.target) for k in plan_of("SELECT * FROM A a, B b WHERE a.X = b.Y", {}, tables).keys] == [("a", "b"), ("b", "a")]


def test_the_esri_text_of_a_part_or_why_not_for_oracle():
    fields = fields_of([{"name": "MODEL", "type": S}, {"name": "SCORE", "type": D}, {"name": "N", "type": I}, {"name": "AT", "type": T},
                        {"name": "GID", "type": "esriFieldTypeGUID"}])
    tables = {"t": ("T", {"model", "score", "n", "at", "gid"})}

    def esri(where, values=None):
        plan = plan_of("SELECT * FROM T WHERE " + where, values or {}, tables)
        return [esri_text(p, fields, values or {})[0] for p in plan.parts.get("t", [])]

    assert esri("model = :m AND score >= :s AND n IN (1, 2, -3) AND N BETWEEN .5 AND 2 AND NOT (N > 1 OR N < -1)",
                {"m": "it's", "s": "0.5"}) == ["MODEL = 'it''s'", "SCORE >= 0.5", "N IN (1, 2, -3)", "N BETWEEN 0.5 AND 2",
                                               "(NOT (N > 1 OR N < -1))"]
    # A text that is a number against a number column: compared as that number (SQLite's affinity), else not sent.
    assert esri("SCORE > '0.25' AND SCORE > :x AND SCORE > 'abc' AND SCORE > :y AND SCORE > 1e3", {"x": "1e3", "y": "7"}) == [
        "SCORE > 0.25", None, None, "SCORE > 7", None]
    # Texts: = and IN (not under NOT), never '' (NULL on Oracle), never their order.
    assert esri("MODEL IN ('a', 'b') AND MODEL = '' AND MODEL <> 'a' AND MODEL > 'a' AND NOT MODEL = 'b' AND MODEL NOT IN ('c') "
                "AND MODEL IS NOT NULL AND NOT MODEL IS NULL AND (MODEL = 'a' OR N = 1)") == [
        "MODEL IN ('a', 'b')", None, None, None, None, None, "MODEL IS NOT NULL", "(NOT MODEL IS NULL)", "(MODEL = 'a' OR N = 1)"]
    # LIKE: in upper case when the pattern has letters (SQLite ignores the case of a–z, Oracle does not).
    assert esri("MODEL LIKE 'yolo%' AND MODEL LIKE '%東京_' AND MODEL LIKE 'ａ%' AND MODEL NOT LIKE 'x' AND MODEL LIKE '10\\%%' ESCAPE '\\' "
                "AND MODEL LIKE '' AND MODEL LIKE :p", {"p": "v8%"}) == [
        "UPPER(MODEL) LIKE 'YOLO%'", "MODEL LIKE '%東京_'", None, None, "MODEL LIKE '10\\%%' ESCAPE '\\'", None, "UPPER(MODEL) LIKE 'V8%'"]
    # Dates against 'YYYY-MM-DD…': a day more each side, as instants (the PC's text is local time).
    assert esri("AT >= '2026-11-01' AND AT < :to AND AT BETWEEN '2026-01-31' AND '2026-02-28 12:00' AND AT = '2026-03-01' "
                "AND AT <> '2026-01-01' AND '2026-05-05' <= AT AND AT > '11/01' AND AT >= date('now') AND NOT AT > '2026-01-01'",
                {"to": "2026-12-31 23:59"}) == [
        "AT >= TIMESTAMP '2026-10-31 00:00:00'", "AT < TIMESTAMP '2027-01-02 00:00:00'",
        "(AT >= TIMESTAMP '2026-01-30 00:00:00' AND AT < TIMESTAMP '2026-03-02 00:00:00')",
        "(AT >= TIMESTAMP '2026-02-28 00:00:00' AND AT < TIMESTAMP '2026-03-03 00:00:00')", None,
        "AT >= TIMESTAMP '2026-05-04 00:00:00'", None, None]
    # GUIDs, unknown columns, a number against a text; what of an AND cannot go is left out (more rows, never fewer).
    assert esri("GID = 'x' AND NOPE = 1 AND MODEL = 1 AND MODEL = N AND (N = 1 OR (N = 2 AND MODEL > 'a')) AND NOT (N = 1 AND MODEL = 'a')") == [
        None, None, None, None, "(N = 1 OR N = 2)", None]
    assert esri_text(plan_of("SELECT * FROM T WHERE MODEL <> 'a'", {}, tables).parts["t"][0], fields)[1].startswith("文字の大小比較")


def test_the_values_of_a_join_key_as_esri_literals():
    assert key_values([Column("A", "INTEGER")], [(1,), (2.0,), (" 3",), (None,), ("x",), (1,)], "a", "number") == ["1", "2", "3"]
    # A text SQLite may take for a number, but written otherwise: no keys (all rows are read).
    assert key_values([Column("A", "REAL")], [("1e3",)], "A", "number") is None
    # '' joins nothing on Oracle (it is NULL there).
    assert key_values([Column("A", "TEXT")], [("a'b",), (2,), (None,), ("",)], "A", "text") == ["'a''b'", "'2'"]
    assert key_values([Column("A", "TEXT")], [(2.5,)], "A", "text") is None
    assert key_values([Column("A", "TEXT")], [("x",)], "A", "number") is None and key_values([Column("A")], [], "B", "text") is None
    assert worth(10, None, 1000, 100) and not worth(5001, None, 1000, 100)
    assert worth(600, 5000, 1000, 100_000) and not worth(600, 1500, 1000, 100_000) and worth(5000, 200_000, 1000, 100_000)
    assert worth(0, 10, 1000, 100)


def test_the_columns_the_sql_reads():
    schemas = {"A": [Column("ID", "INTEGER"), Column("NAME"), Column("V", "REAL")], "B": [Column("ID", "INTEGER"), Column("X")]}
    assert used_columns("SELECT COUNT(*) FROM A", schemas) == {"a": set(), "b": set()}
    assert used_columns("SELECT * FROM A", schemas)["a"] == {"id", "name", "v"}
    assert used_columns("SELECT a.*, b.X FROM A a JOIN B b ON a.ID = b.ID WHERE b.X = :x", schemas, {"x": "1"}) == {
        "a": {"id", "name", "v"}, "b": {"id", "x"}}
    # SQLite does not tell what USING and NATURAL JOIN compare: all columns are read.
    assert used_columns("SELECT COUNT(*) FROM A JOIN B USING (ID)", schemas) is None
    assert used_columns("SELECT COUNT(*) FROM A NATURAL JOIN B", schemas) is None
    assert used_columns("WITH q AS (SELECT NAME FROM A WHERE V > 1) SELECT COUNT(*) FROM q, B WHERE B.x IN (SELECT id FROM a)",
                        schemas) == {"a": {"name", "v", "id"}, "b": {"x"}}
    assert used_columns("SELECT rowid FROM A", schemas)["a"] == {"rowid"}
    # An error of the SQL is known before anything is read; EXPLAIN … is left as it is.
    for sql, message in [("SELECT NOPE FROM A", "列がない: NOPE"), ("SELECT * FROM C", "表がない"), ("DELETE FROM A", "読み取り（SELECT）以外"),
                         ("SELECT ID FROM A JOIN B", "SQL のエラー: ambiguous"), ("SELECT 1; SELECT 2", "一度に実行できる文は 1 つ")]:
        with pytest.raises(AnalysisError, match=re.escape(message)):
            used_columns(sql, schemas)
    assert used_columns("EXPLAIN SELECT * FROM A", schemas) is None


def ai_and_catalog(page=1000, fake=None):
    fake = fake or FakeEsri()
    ai = fake.add_layer(AI, FakeLayer({"IMAGE_ID": S, "MODEL": S, "SCORE": D, "NOTE": S}, max_record_count=page))
    cat = fake.add_layer(CAT, FakeLayer({"IMG_ID": S, "SAT": S, "N": I, "PATH": S}, max_record_count=page))
    for i in range(40):
        cat.add({"IMG_ID": f"I{i}", "SAT": "A" if i % 2 else "B", "N": i, "PATH": f"/p/{i}"})
    for img, model, score in [("I1", "m1", 0.9), ("I1", "m1", 0.4), ("I2", "m1", 0.8), ("I3", "m2", 0.9), ("I9", "m2", 0.7)]:
        ai.add({"IMAGE_ID": img, "MODEL": model, "SCORE": score, "NOTE": "n"})
    tables = [Table("TBL_AI_RESULT", "AI", "esri", "aiResult", AI), Table("TBL_IMAGE_CATALOG", "画像", "esri", "catalog", CAT)]
    return fake, tables


def run(fake, tables, sql, values=None, conditions=None, plan=True, limit=100_000, readers=None, minutes=0):
    """The rows of the SQL (sorted) and each table's (rows, note); `plan` False: every table read whole, as before."""
    values, conditions, readers = values or {}, conditions or {}, readers or {}
    used = referenced(sql, [t.name for t in tables])
    prepared = [(t, readers.get(t.name)) for t in tables if t.name in used]
    client = EsriClient(fake)
    if plan:
        reading = prepare_reads(client, prepared, sql, values, conditions, wants_shape(sql), lambda _t: None, cache_minutes=minutes)
        reads = read_tables(client, reading, conditions, limit, lambda _t: None, cache_minutes=minutes)
    else:
        reads = []
        for t, reader in prepared:
            columns, rows = reader.read() if reader else read_esri(client, t, conditions.get(t.name, ""), wants_shape(sql), limit)
            reads.append(analysis_plan.Read(t, columns, rows))
    db = Database()
    for r in reads:
        db.add(r.table.name, r.columns, r.rows)
    return sorted(db.run(sql, values).rows, key=repr), {r.table.name: (len(r.rows), r.note) for r in reads}


def asked(fake, url, what="where"):
    return [form.get(what, "") for u, form in fake.asked if u == f"{url}/query" and form.get("returnCountOnly") != "true"]


def test_the_catalog_is_read_for_the_images_of_the_ai_results_only():
    fake, tables = ai_and_catalog(page=5)
    b = Builder("TBL_AI_RESULT", [Join("TBL_IMAGE_CATALOG", "IMAGE_ID", "IMG_ID")], "t0.MODEL = :model AND t0.SCORE >= 0.5",
                ["t1.SAT"], [Figure("count")])
    rows, reads = run(fake, tables, build_sql(b), {"model": "m1"})
    assert rows == [("A", 1), ("B", 1)]
    assert reads == {"TBL_AI_RESULT": (2, "サーバーで絞り込み: 条件・列 4/5"),
                     "TBL_IMAGE_CATALOG": (2, "サーバーで絞り込み: 結合キー 2 件・列 3/5")}
    assert asked(fake, AI) == ["(MODEL = 'm1') AND (SCORE >= 0.5)"] and asked(fake, AI, "outFields") == ["OBJECTID,IMAGE_ID,MODEL,SCORE"]
    assert asked(fake, CAT) == ["IMG_ID IN ('I1', 'I2')"] and asked(fake, CAT, "outFields") == ["OBJECTID,IMG_ID,SAT"]
    # The same as reading everything.
    fake.asked.clear()
    assert run(fake, tables, build_sql(b), {"model": "m1"}, plan=False)[0] == rows
    assert len(asked(fake, CAT)) >= 8 and set(asked(fake, CAT, "outFields")) == {"*"}


def test_sql_written_by_hand_is_narrowed_too():
    fake, tables = ai_and_catalog(page=5, fake=OracleEsri())
    sql = ("SELECT c.SAT, COUNT(*) AS n FROM TBL_IMAGE_CATALOG c\n"
           "JOIN TBL_AI_RESULT a ON a.IMAGE_ID = c.IMG_ID AND a.MODEL LIKE 'M%' -- the model\n"
           "WHERE a.SCORE >= 0.5 AND c.N < 30 GROUP BY c.SAT ORDER BY n DESC")
    rows, reads = run(fake, tables, sql)
    assert rows == [("A", 3), ("B", 1)]
    # The AI results first (fewer), then the catalog for their images.
    assert list(reads) == ["TBL_AI_RESULT", "TBL_IMAGE_CATALOG"]
    assert asked(fake, AI) == ["(SCORE >= 0.5) AND (UPPER(MODEL) LIKE 'M%')"]
    assert asked(fake, CAT) == ["(N < 30) AND IMG_ID IN ('I1', 'I2', 'I3', 'I9')"]
    assert run(fake, tables, sql, plan=False)[0] == rows


def test_a_small_joined_table_is_read_first_and_a_left_join_keeps_its_side():
    fake, tables = ai_and_catalog(page=5)
    b = Builder("TBL_IMAGE_CATALOG", [Join("TBL_AI_RESULT", "IMG_ID", "IMAGE_ID")], "t1.MODEL = 'm2'", ["t0.IMG_ID"], [Figure("count")])
    rows, reads = run(fake, tables, build_sql(b))
    assert rows == [("I3", 1), ("I9", 1)]
    assert list(reads) == ["TBL_AI_RESULT", "TBL_IMAGE_CATALOG"] and reads["TBL_IMAGE_CATALOG"] == (2, "サーバーで絞り込み: 結合キー 2 件・列 2/5")
    assert asked(fake, CAT) == ["IMG_ID IN ('I3', 'I9')"]
    # 「なくても残す」: the catalog keeps its images without results, so it is read whole, the results for its images.
    b.joins[0].type = "left"
    rows, reads = run(fake, tables, build_sql(b))
    assert len(rows) == 2 and reads["TBL_IMAGE_CATALOG"] == (40, "サーバーで絞り込み: 列 2/5")
    assert run(fake, tables, build_sql(b), plan=False)[0] == rows


def test_no_keys_reads_nothing_and_what_the_server_refuses_is_read_as_before(monkeypatch):
    fake, tables = ai_and_catalog(page=5)
    sql = build_sql(Builder("TBL_AI_RESULT", [Join("TBL_IMAGE_CATALOG", "IMAGE_ID", "IMG_ID")], "t0.MODEL = 'none'", [], [Figure("count")]))
    rows, reads = run(fake, tables, sql)
    assert rows == [(0,)] and reads["TBL_IMAGE_CATALOG"] == (0, "結合する行がないので読み込みなし") and asked(fake, CAT) == []

    answer = fake.answer

    def refusing(url, form):
        if "MODEL" in form.get("where", "") or " IN (" in form.get("where", "") or form.get("outFields", "*") != "*":
            return {"error": {"code": 400, "message": "Unable to perform query."}}
        return answer(url, form)

    monkeypatch.setattr(fake, "answer", refusing)
    sql = sql.replace("'none'", "'m1'")
    rows, reads = run(fake, tables, sql)
    assert rows == [(3,)]
    assert reads == {"TBL_AI_RESULT": (5, "サーバーで絞り込めず、読み込み条件だけで読み込み"),
                     "TBL_IMAGE_CATALOG": (40, "サーバーで絞り込めず、読み込み条件だけで読み込み")}
    # Without joins (no count asked first): the refused read is read again without it.
    rows, reads = run(fake, tables, "SELECT COUNT(*) FROM TBL_AI_RESULT WHERE MODEL = 'm1'")
    assert rows == [(3,)] and reads == {"TBL_AI_RESULT": (5, "サーバーで絞り込めず、読み込み条件だけで読み込み")}


def test_keys_go_in_chunks_many_number_keys_as_a_range_and_a_project_layer_gives_keys_too(monkeypatch):
    monkeypatch.setattr(analysis_plan, "CHUNK", 2)
    fake, tables = ai_and_catalog(page=5)
    layer = Table("watch", "注目画像", "layer", layer_id="L1")
    reader = Reader([Column("IMG"), Column("NOTE")], [["I1", "a"], ["I4", "b"], ["I5", None], [None, "c"], ["I1", "d"]])
    sql = "SELECT c.SAT, COUNT(*) FROM watch w JOIN TBL_IMAGE_CATALOG c ON c.IMG_ID = w.IMG GROUP BY c.SAT"
    tables = tables + [layer, Table("nums", "番号", "layer", layer_id="L2")]
    rows, reads = run(fake, tables, sql, readers={"watch": reader})
    assert rows == [("A", 3), ("B", 1)]
    assert reads == {"watch": (5, ""), "TBL_IMAGE_CATALOG": (3, "サーバーで絞り込み: 結合キー 3 件・列 3/5")}
    assert asked(fake, CAT) == ["IMG_ID IN ('I1', 'I4')", "IMG_ID IN ('I5')"]
    # Over the limit: an error, as before (never a part of the rows).
    with pytest.raises(AnalysisError, match="上限"):
        run(fake, tables, sql, readers={"watch": reader}, limit=2)
    # Many number keys close together: one range (fewer requests than their INs).
    numbers = Reader([Column("N", "INTEGER")], [[n] for n in range(10, 22)])
    fake.asked.clear()
    rows, reads = run(fake, tables, "SELECT COUNT(*) FROM nums JOIN TBL_IMAGE_CATALOG c ON c.N = nums.N",
                      readers={"nums": numbers})
    assert rows == [(12,)] and reads["TBL_IMAGE_CATALOG"] == (12, "サーバーで絞り込み: 結合キーの範囲 10〜21・列 2/5")
    # 3 pages of 5 rather than 6 INs of 2.
    assert asked(fake, CAT) == ["(N BETWEEN 10 AND 21)"] * 3


def test_answers_are_kept_until_the_layer_changes_or_the_minutes_pass(monkeypatch):
    fake, tables = ai_and_catalog(page=50)
    sql = "SELECT COUNT(*) FROM TBL_AI_RESULT WHERE MODEL = 'm1'"
    assert run(fake, tables, sql, minutes=5)[1] == {"TBL_AI_RESULT": (3, "サーバーで絞り込み: 条件・列 2/5")}
    fake.asked.clear()
    rows, reads = run(fake, tables, sql, minutes=5)
    assert rows == [(3,)] and reads["TBL_AI_RESULT"][1] == "サーバーで絞り込み: 条件・列 2/5、キャッシュ（1 分以内）"
    assert asked(fake, AI) == []
    # Fewer columns of the same query: from the answer kept too.
    run(fake, tables, "SELECT * FROM TBL_AI_RESULT", minutes=5)
    fake.asked.clear()
    assert run(fake, tables, "SELECT NOTE FROM TBL_AI_RESULT", minutes=5)[1]["TBL_AI_RESULT"][1].endswith("キャッシュ（1 分以内）")
    assert asked(fake, AI) == []
    # The layer says its data changed: read again.
    fake.layers[AI].extra = {"editingInfo": {"dataLastEditDate": 1}}
    run(fake, tables, sql, minutes=5)
    assert len(asked(fake, AI)) == 1
    fake.asked.clear()
    run(fake, tables, sql, minutes=5)
    assert asked(fake, AI) == []
    fake.layers[AI].extra = {"editingInfo": {"dataLastEditDate": 2}}
    fake.layers[AI].add({"IMAGE_ID": "I7", "MODEL": "m1", "SCORE": 0.1})
    assert run(fake, tables, sql, minutes=5)[0] == [(4,)]
    # The plugin writes to the layer: read again.
    fake.asked.clear()
    EsriClient(fake).ask(f"{AI}/applyEdits", {"adds": "[]"})
    run(fake, tables, sql, minutes=5)
    assert len(asked(fake, AI)) == 1
    # The minutes pass; 0 keeps nothing; 「一覧を更新」 forgets all.
    now = analysis_cache.time.monotonic()
    monkeypatch.setattr(analysis_cache.time, "monotonic", lambda: now + 301)
    fake.asked.clear()
    run(fake, tables, sql, minutes=5)
    assert len(asked(fake, AI)) == 1
    analysis_cache.clear()
    run(fake, tables, sql, minutes=0)
    run(fake, tables, sql, minutes=0)
    assert len(asked(fake, AI)) == 3


def test_the_plan_of_reading_says_what_goes_to_the_server():
    fake, tables = ai_and_catalog(page=5)
    sql = ("SELECT c.SAT, COUNT(*) FROM TBL_AI_RESULT a JOIN TBL_IMAGE_CATALOG c ON a.IMAGE_ID = c.IMG_ID "
           "WHERE a.MODEL = :m AND a.NOTE <> 'x' AND length(c.PATH) > 3 GROUP BY c.SAT")
    reading = prepare_reads(EsriClient(fake), [(t, None) for t in tables], sql, {"m": "m1"}, {"TBL_IMAGE_CATALOG": "N < 30"}, False,
                            lambda _t: None, count_all=True)
    text = describe(reading, {"TBL_IMAGE_CATALOG": "N < 30"})
    assert fake.asked and all(form.get("returnCountOnly") == "true" for u, form in fake.asked if u.endswith("/query"))
    assert "1. TBL_AI_RESULT（AI）  SQL では a" in text and "件数: 3 行（サーバーの答え）" in text
    assert "サーバーへ送る WHERE: (MODEL = 'm1')" in text and "読む列: 4 / 5（OBJECTID, IMAGE_ID, MODEL, NOTE）" in text
    assert "送らない条件: a.\"NOTE\" <> 'x' … 文字の大小比較" in text
    assert "2. TBL_IMAGE_CATALOG（画像）  SQL では c" in text and "サーバーへ送る WHERE: (N < 30)" in text
    # Only the keys of a table read before (the smaller one first).
    assert "結合キー: 先に読む TBL_AI_RESULT の IMAGE_ID の値で IMG_ID を絞る" in text and "TBL_IMAGE_CATALOG の IMG_ID の値" not in text
    assert "PC だけで見る条件: length(c.PATH) > 3 … 関数" in text
    # An error of the SQL: before anything is read.
    with pytest.raises(AnalysisError, match="列がない"):
        prepare_reads(EsriClient(fake), [(t, None) for t in tables], "SELECT NOPE FROM TBL_AI_RESULT", {}, {}, False, lambda _t: None)


def _random_case(rng):
    """Random tables (as Oracle keeps them) and a random SQL on them."""
    fake = OracleEsri()
    urls = [f"https://example.com/arcgis/rest/services/R/FeatureServer/{i}" for i in range(3)]
    fields = {"ID": I, "K": I, "NAME": S, "CODE": S, "V": D, "AT": T}
    layers = [fake.add_layer(u, FakeLayer({**fields, **({"EXTRA": S} if i == 2 else {})}, max_record_count=3)) for i, u in enumerate(urls)]
    days = [ms(f"2026-11-0{d} {h:02d}:30:00") for d in range(1, 10) for h in (0, 9, 15, 23)]
    for i, layer in enumerate(layers):
        for _ in range(rng.randint(0, 18)):
            layer.add({"ID": rng.choice([None, *range(8)]), "K": rng.choice([None, *range(8)]), "NAME": rng.choice([None, "a", "b", "c"]),
                       "CODE": rng.choice([None, "Ab", "ab", "AB", "x_y", "xay", "東京", "ä", "Ä", "10%"]),
                       "V": rng.choice([None, 0.5, 1.5, 2.5]), "AT": rng.choice([None, *days]),
                       **({"EXTRA": rng.choice([None, "e1", "e2"])} if i == 2 else {})})
    tables = [Table(f"R{i}", f"R{i}", "esri", "", u) for i, u in enumerate(urls)]
    n = rng.choice([1, 2, 2, 3])
    aliases = ["a", "b", "c"][:n]
    froms = ["R0 a"]
    where = []

    def condition(x):
        return rng.choice([
            f"{x}.ID >= {rng.randint(0, 7)}", f"{x}.K IN (1, 3, 5)", f"{x}.V BETWEEN 1 AND 2", f"{x}.K IS NULL", f"{x}.NAME IS NOT NULL",
            f"{x}.NAME = '{rng.choice('abc')}'", f"{x}.NAME <> 'b'", f"{x}.NAME IN ('a', 'c')", f"{x}.NAME NOT IN ('a')", f"{x}.NAME = ''",
            f"{x}.NAME <> ''", f"{x}.NAME > 'a'", f"{x}.CODE LIKE 'a%'", f"{x}.CODE LIKE '%B'", f"{x}.CODE LIKE 'x\\_y' ESCAPE '\\'",
            f"{x}.CODE LIKE 'x_y'", f"{x}.CODE NOT LIKE 'a%'", f"{x}.CODE LIKE '東%'", f"{x}.CODE LIKE 'Ä%'", f"{x}.CODE LIKE '10%'",
            f"{x}.AT >= '2026-11-0{rng.randint(1, 9)}'", f"{x}.AT < '2026-11-0{rng.randint(1, 9)} 12:00'", f"{x}.AT = '2026-11-05'",
            f"{x}.AT BETWEEN '2026-11-03' AND '2026-11-06 09:30:00'", f"date({x}.AT) = '2026-11-05'", f"{x}.AT >= :day",
            f"{x}.AT <= '2026-11-04 09:30:00'", f"NOT {x}.AT > '2026-11-06'",
            f"({x}.V > 1 OR {x}.NAME = 'a')", f"NOT {x}.NAME = 'b'", f"NOT ({x}.ID > 3 AND {x}.CODE LIKE 'a%')",
            f"NOT ({x}.ID > 3 OR {x}.CODE LIKE 'a%')", f"({x}.ID = 1 OR ({x}.K = 2 AND {x}.NAME > 'a'))", f"{x}.ID + 1 > 3",
            f"{x}.ID <> :p", f"{x}.CODE = :c", f"{x}.CODE LIKE :like"])

    for i, x in enumerate(aliases[1:], 1):
        other = rng.choice(aliases[:i])
        on = f"{other}.{rng.choice(['ID', 'K'])} = {x}.{rng.choice(['ID', 'K'])}"
        if rng.random() < 0.3:
            on += f" AND {condition(rng.choice([x, other]))}"
        kind = rng.choice(["JOIN", "LEFT JOIN", "LEFT JOIN", ","])
        if kind == ",":
            froms.append(f", R{i} {x}")
            where.append(on)
        else:
            froms.append(f" {kind} R{i} {x} ON {on}")
    for _ in range(rng.randint(0, 4)):
        where.append(condition(rng.choice(aliases)))
    if n == 3 and rng.random() < 0.3:
        where.append("EXTRA = 'e1'" if rng.random() < 0.5 else "a.ID = c.K")
    select = rng.choice(["COUNT(*)", "a.NAME, COUNT(*), COUNT(DISTINCT a.ID)", "*", "a.*", f"{aliases[-1]}.CODE, a.AT, a.V"])
    sql = f"SELECT {select} FROM {''.join(froms)}" + (" WHERE " + " AND ".join(where) if where else "")
    if select.startswith("a.NAME"):
        sql += " GROUP BY a.NAME"
    values = {"p": str(rng.randint(0, 7)), "day": f"2026-11-0{rng.randint(1, 9)}", "c": rng.choice(["Ab", "ab", "東京", "", "x_y"]),
              "like": rng.choice(["a%", "%B", "", "Ä%", "x_y"])}
    return fake, tables, sql, {k: v for k, v in values.items() if f":{k}" in sql}


def test_the_plan_never_changes_the_result(monkeypatch):
    """Random tables, joins and conditions on an Oracle-like server: the result with the plan is the result of reading everything."""
    monkeypatch.setattr(analysis_plan, "CHUNK", 2)
    rng = random.Random(20261117)
    for n in range(300):
        fake, tables, sql, values = _random_case(rng)
        planned = run(fake, tables, sql, values)
        plain = run(fake, tables, sql, values, plan=False)
        assert planned[0] == plain[0], (n, sql, values, planned[1])
