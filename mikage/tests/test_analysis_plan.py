"""「データ分析」 の読み込みの計画 (core/analysis_plan.py): the builder's conditions and join keys narrow the Esri reading, never the result."""
import random

import pytest

from mikage.core import analysis_plan
from mikage.core.analysis import Builder, Column, Database, Figure, Join, Table, build_sql, referenced
from mikage.core.analysis_plan import esri_text, fields_of, key_values, plan_of, read_tables, worth
from mikage.core.esri import EsriClient
from mikage.tests.fake_esri import FakeEsri, FakeLayer

AI = "https://example.com/arcgis/rest/services/AI/FeatureServer/0"
CAT = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
OBS = "https://example.com/arcgis/rest/services/Obs/FeatureServer/0"
S, D, I, T = "esriFieldTypeString", "esriFieldTypeDouble", "esriFieldTypeInteger", "esriFieldTypeDate"


def parts(builder, values=None):
    plan = plan_of(builder, build_sql(builder), values or {})
    return {a: [p.text for p in ps] for a, ps in plan.parts.items()}


def test_the_parts_of_the_condition_on_one_table_go_to_the_server():
    b = Builder("TBL_AI_RESULT", [Join("TBL_IMAGE_CATALOG", "IMAGE_ID", "IMG_ID")],
                't0."MODEL" = :model AND t0.SCORE >= 0.5 AND (t1.ACQ_N > 1 OR t1.PATH IS NULL) AND t0.CLS LIKE \'s%\' '
                "AND t0.A = t1.B AND upper(t0.CLS) = 'S' AND t0.SCORE + 1 > 2 AND t0.N NOT BETWEEN 1 AND 3 AND NOT t0.K IN ('a', 'b')",
                ["t0.IMAGE_ID"], [Figure("count")])
    assert parts(b) == {
        "t0": ['t0."MODEL" = :model', 't0."SCORE" >= 0.5', 't0."N" NOT BETWEEN 1 AND 3', "(NOT t0.\"K\" IN ('a', 'b'))"],
        "t1": ['(t1."ACQ_N" > 1 OR t1."PATH" IS NULL)'],
    }
    # AND before OR: `a OR b AND c` is not split (it would be `(a OR b) AND c`).
    assert parts(Builder("T", [Join("U", "A", "B")], "t0.A = 1 OR t0.B = 2 AND t1.C = 3")) == {}
    assert parts(Builder("T", [], "(t0.A = 1 OR t0.B = 2) AND C = 3")) == {"t0": ['(t0."A" = 1 OR t0."B" = 2)', '"C" = 3']}
    # Unqualified columns: the base table's without joins, not known with them.
    assert parts(Builder("T", [Join("U", "A", "B")], "C = 3")) == {}
    # A comment may leave out the rest of the line: nothing goes.
    assert parts(Builder("T", [], "A = 1 -- AND B = 2")) == {}
    # A table the SQL names twice (a self join) is read for both: nothing narrows it.
    assert parts(Builder("T", [Join("T", "A", "A")], "t0.B = 1 AND t1.B = 2")) == {}
    assert parts(Builder("T", [], "t0.A IN (SELECT A FROM T)")) == {}
    # SQL written by hand: no plan (only the builder's own SQL is understood).
    b = Builder("T", [], "t0.A = 1")
    assert plan_of(b, build_sql(b) + "\nLIMIT 3", {}) is None and plan_of(None, "SELECT 1", {}) is None


def test_a_left_joined_table_gets_only_parts_false_for_missing_rows():
    b = Builder("TBL_IMAGE_CATALOG", [Join("TBL_AI_RESULT", "IMG_ID", "IMAGE_ID", "left")],
                "t1.MODEL = 'x' AND (t1.MODEL IS NULL OR t1.SCORE > 1) AND NOT t1.SCORE IS NOT NULL AND t1.CLS <> :c AND t0.N = 1")
    assert parts(b, {"c": "car"}) == {"t1": ["t1.\"MODEL\" = 'x'", 't1."CLS" <> :c'], "t0": ['t0."N" = 1']}
    plan = plan_of(b, build_sql(b), {"c": "car"})
    # Keys: from the kept side to the other only.
    assert [(k.source, k.target) for k in plan.keys] == [("t0", "t1")]
    inner = Builder("A", [Join("B", "X", "Y"), Join("C", "Z", "W", "inner", "t1")])
    assert [(k.source, k.source_column, k.target, k.target_column) for k in plan_of(inner, build_sql(inner)).keys] == [
        ("t0", "X", "t1", "Y"), ("t1", "Y", "t0", "X"), ("t1", "Z", "t2", "W"), ("t2", "W", "t1", "Z")]


def test_the_esri_text_of_a_part_or_none_when_the_server_may_read_it_otherwise():
    fields = fields_of([{"name": "MODEL", "type": S}, {"name": "SCORE", "type": D}, {"name": "N", "type": I}, {"name": "AT", "type": T},
                        {"name": "GID", "type": "esriFieldTypeGUID"}])

    def esri(where, values=None):
        b = Builder("T", [], where)
        plan = plan_of(b, build_sql(b), values or {})
        return [esri_text(p, fields, values or {}) for p in plan.parts.get("t0", [])]

    assert esri("t0.model = :m AND score >= :s AND n IN (1, 2, -3) AND N BETWEEN .5 AND 2",
                {"m": "it's", "s": "0.5"}) == ["MODEL = 'it''s'", "SCORE >= 0.5", "N IN (1, 2, -3)", "N BETWEEN 0.5 AND 2"]
    # A text that is a number against a number column: compared as that number (SQLite's affinity), else not sent.
    assert esri("SCORE > '0.25' AND SCORE > :x AND SCORE > 'abc' AND SCORE > :y", {"x": "1e3", "y": "7"}) == [
        "SCORE > 0.25", None, None, "SCORE > 7"]
    # Dates, GUIDs, unknown columns, a number against a text (a value or a column): not sent. Each comparison has its own kind.
    assert esri("AT >= '2026-11-01' AND GID = 'x' AND NOPE = 1 AND MODEL = 1 AND MODEL = N AND (MODEL = 'a' OR N = 1)") == [
        None, None, None, None, None, "(MODEL = 'a' OR N = 1)"]
    assert esri("t0.MODEL IS NOT NULL AND (MODEL = 'a' OR NOT MODEL = 'b')") == [
        "MODEL IS NOT NULL", "(MODEL = 'a' OR (NOT MODEL = 'b'))"]


def test_the_values_of_a_join_key_as_esri_literals():
    assert key_values([Column("A", "INTEGER")], [(1,), (2.0,), (" 3",), (None,), ("x",), (1,)], "a", "number") == ["1", "2", "3"]
    # A text SQLite may take for a number, but written otherwise: no keys (all rows are read).
    assert key_values([Column("A", "REAL")], [("1e3",)], "A", "number") is None
    assert key_values([Column("A", "TEXT")], [("a'b",), (2,), (None,)], "A", "text") == ["'a''b'", "'2'"]
    assert key_values([Column("A", "TEXT")], [(2.5,)], "A", "text") is None
    assert key_values([Column("A", "TEXT")], [("x",)], "A", "number") is None and key_values([Column("A")], [], "B", "text") is None
    assert worth(10, None, 1000, 100) and not worth(5001, None, 1000, 100)
    assert worth(600, 5000, 1000, 100_000) and not worth(600, 1500, 1000, 100_000) and worth(5000, 200_000, 1000, 100_000)
    assert worth(0, 10, 1000, 100)


class Reader:
    def __init__(self, columns, rows):
        self.columns, self.rows = columns, rows

    def read(self, _cancelled=None):
        return self.columns, self.rows


def ai_and_catalog(page=1000):
    fake = FakeEsri()
    ai = fake.add_layer(AI, FakeLayer({"IMAGE_ID": S, "MODEL": S, "SCORE": D}, max_record_count=page))
    cat = fake.add_layer(CAT, FakeLayer({"IMG_ID": S, "SAT": S}, max_record_count=page))
    for i in range(40):
        cat.add({"IMG_ID": f"I{i}", "SAT": "A" if i % 2 else "B"})
    for img, model, score in [("I1", "m1", 0.9), ("I1", "m1", 0.4), ("I2", "m1", 0.8), ("I3", "m2", 0.9), ("I9", "m2", 0.7)]:
        ai.add({"IMAGE_ID": img, "MODEL": model, "SCORE": score})
    tables = [Table("TBL_AI_RESULT", "AI", "esri", "aiResult", AI), Table("TBL_IMAGE_CATALOG", "画像", "esri", "catalog", CAT)]
    return fake, tables


def run(fake, tables, builder, values=None, conditions=None, plan=True, limit=100_000):
    sql = build_sql(builder)
    used = referenced(sql, [t.name for t in tables])
    prepared = [(t, None) for t in tables if t.name in used]
    reads = read_tables(EsriClient(fake), prepared, plan_of(builder, sql, values or {}) if plan else None, conditions or {},
                        values or {}, False, limit, lambda _t: None)
    db = Database()
    for r in reads:
        db.add(r.table.name, r.columns, r.rows)
    return sorted(db.run(sql, values or {}).rows, key=repr), {r.table.name: (len(r.rows), r.note) for r in reads}


def wheres(fake, url):
    return [form.get("where", "") for u, form in fake.asked if u == f"{url}/query" and form.get("returnCountOnly") != "true"]


def test_the_catalog_is_read_for_the_images_of_the_ai_results_only():
    fake, tables = ai_and_catalog(page=5)
    b = Builder("TBL_AI_RESULT", [Join("TBL_IMAGE_CATALOG", "IMAGE_ID", "IMG_ID")], "t0.MODEL = :model AND t0.SCORE >= 0.5",
                ["t1.SAT"], [Figure("count")])
    rows, reads = run(fake, tables, b, {"model": "m1"})
    assert rows == [("A", 1), ("B", 1)]
    assert reads == {"TBL_AI_RESULT": (2, "サーバーで絞り込み: 条件"), "TBL_IMAGE_CATALOG": (2, "サーバーで絞り込み: 結合キー 2 件")}
    assert wheres(fake, AI) == ["(MODEL = 'm1') AND (SCORE >= 0.5)"]
    assert wheres(fake, CAT) == ["IMG_ID IN ('I1', 'I2')"]
    # The same as reading everything.
    fake.asked.clear()
    assert run(fake, tables, b, {"model": "m1"}, plan=False)[0] == rows
    assert len(wheres(fake, CAT)) >= 8


def test_a_small_joined_table_is_read_first_and_narrows_the_base_table():
    fake, tables = ai_and_catalog(page=5)
    b = Builder("TBL_IMAGE_CATALOG", [Join("TBL_AI_RESULT", "IMG_ID", "IMAGE_ID")], "t1.MODEL = 'm2'", ["t0.IMG_ID"], [Figure("count")])
    rows, reads = run(fake, tables, b)
    assert rows == [("I3", 1), ("I9", 1)]
    assert list(reads) == ["TBL_AI_RESULT", "TBL_IMAGE_CATALOG"] and reads["TBL_IMAGE_CATALOG"] == (2, "サーバーで絞り込み: 結合キー 2 件")
    assert wheres(fake, CAT) == ["IMG_ID IN ('I3', 'I9')"]
    # 「なくても残す」: the catalog keeps its images without results, so it is read whole, the results for its images.
    b.joins[0].type = "left"
    fake.asked.clear()
    rows, reads = run(fake, tables, b)
    assert len(rows) == 2 and reads["TBL_IMAGE_CATALOG"] == (40, "")
    assert run(fake, tables, b, plan=False)[0] == rows


def test_no_keys_reads_nothing_and_a_refused_condition_reads_as_before(monkeypatch):
    fake, tables = ai_and_catalog(page=5)
    b = Builder("TBL_AI_RESULT", [Join("TBL_IMAGE_CATALOG", "IMAGE_ID", "IMG_ID")], "t0.MODEL = 'none'", [], [Figure("count")])
    rows, reads = run(fake, tables, b)
    assert rows == [(0,)] and reads["TBL_IMAGE_CATALOG"] == (0, "結合する行がないので読み込みなし") and wheres(fake, CAT) == []

    answer = fake.answer

    def refusing(url, form):
        if "MODEL" in form.get("where", "") or " IN (" in form.get("where", ""):
            return {"error": {"code": 400, "message": "Unable to perform query."}}
        return answer(url, form)

    monkeypatch.setattr(fake, "answer", refusing)
    b.where = "t0.MODEL = 'm1'"
    rows, reads = run(fake, tables, b)
    assert rows == [(3,)]
    assert reads == {"TBL_AI_RESULT": (5, "サーバーで絞り込めず、読み込み条件だけで読み込み"),
                     "TBL_IMAGE_CATALOG": (40, "サーバーで絞り込めず、読み込み条件だけで読み込み")}
    # Without joins (no count asked first): the refused read is read again without it.
    rows, reads = run(fake, tables, Builder("TBL_AI_RESULT", [], "t0.MODEL = 'm1'", [], [Figure("count")]))
    assert rows == [(3,)] and reads == {"TBL_AI_RESULT": (5, "サーバーで絞り込めず、読み込み条件だけで読み込み")}


def test_keys_go_in_chunks_and_a_project_layer_gives_keys_too(monkeypatch):
    monkeypatch.setattr(analysis_plan, "CHUNK", 2)
    fake, tables = ai_and_catalog(page=5)
    layer = Table("watch", "注目画像", "layer", layer_id="L1")
    b = Builder("watch", [Join("TBL_IMAGE_CATALOG", "IMG", "IMG_ID")], "", ["t1.SAT"], [Figure("count")])
    reader = Reader([Column("IMG"), Column("NOTE")], [["I1", "a"], ["I4", "b"], ["I5", None], [None, "c"], ["I1", "d"]])
    sql = build_sql(b)
    reads = read_tables(EsriClient(fake), [(layer, reader), (tables[1], None)], plan_of(b, sql), {}, {}, False, 100_000, lambda _t: None)
    assert [(r.table.name, len(r.rows), r.note) for r in reads] == [("watch", 5, ""), ("TBL_IMAGE_CATALOG", 3, "サーバーで絞り込み: 結合キー 3 件")]
    assert wheres(fake, CAT) == ["IMG_ID IN ('I1', 'I4')", "IMG_ID IN ('I5')"]
    # Over the limit: an error, as before (never a part of the rows).
    with pytest.raises(Exception, match="上限"):
        read_tables(EsriClient(fake), [(layer, reader), (tables[1], None)], plan_of(b, sql), {}, {}, False, 2, lambda _t: None)


def test_the_plan_never_changes_the_result():
    """Random tables, joins and conditions: the result with the plan is the result of reading everything."""
    rng = random.Random(20261117)
    for n in range(60):
        fake = FakeEsri()
        urls = [f"https://example.com/arcgis/rest/services/R/FeatureServer/{i}" for i in range(3)]
        layers = [fake.add_layer(u, FakeLayer({"ID": I, "K": I, "NAME": S, "V": D}, max_record_count=3)) for u in urls]
        for layer in layers:
            for _ in range(rng.randint(0, 25)):
                layer.add({"ID": rng.choice([None, *range(8)]), "K": rng.choice([None, *range(8)]),
                           "NAME": rng.choice([None, "a", "b", "c"]), "V": rng.choice([None, 0.5, 1.5, 2.5])})
        tables = [Table(f"R{i}", f"R{i}", "esri", "", u) for i, u in enumerate(urls)]
        joins = [Join("R1", rng.choice(["ID", "K"]), rng.choice(["ID", "K"]), rng.choice(["inner", "left"]))]
        if rng.random() < 0.6:
            joins.append(Join("R2", rng.choice(["ID", "K", "NAME"]) if rng.random() < 0.8 else "NAME",
                              rng.choice(["ID", "K"]), rng.choice(["inner", "left"]), rng.choice(["", "t1"])))
            if joins[1].left == "NAME":
                joins[1].right = "NAME"
        conditions = []
        for _ in range(rng.randint(0, 3)):
            alias = rng.choice(["t0", "t1", "t2"][:len(joins) + 1])
            conditions.append(rng.choice([
                f"{alias}.ID >= {rng.randint(0, 7)}", f"{alias}.NAME = '{rng.choice('abc')}'", f"{alias}.NAME IS NULL",
                f"({alias}.V > 1 OR {alias}.NAME = 'a')", f"{alias}.K IN (1, 3, 5)", f"{alias}.V BETWEEN 1 AND 2",
                f"{alias}.ID <> :p", f"{alias}.K IS NOT NULL", f"NOT {alias}.NAME = 'b'"]))
        b = Builder("R0", joins, " AND ".join(conditions), ["t0.NAME"], [Figure("count"), Figure("countDistinct", "t0.ID")])
        values = {"p": str(rng.randint(0, 7))}
        planned = run(fake, tables, b, values)
        plain = run(fake, tables, b, values, plan=False)
        assert planned[0] == plain[0], (n, build_sql(b), planned[1])
