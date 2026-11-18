"""tools/arcgis/dashboard_views_oracle.sql（ArcGIS ダッシュボード用のビュー）がテーブルの定義と合っているか。

Oracle はないので、列の名前と作りの決まりだけを確かめる。列が実際にあるかは sqlglot（入っていれば）で
ビューを 1 本ずつ解析し、テーブルの属性（mikage_schema）と先に作ったビューの列に照らす。
"""
from __future__ import annotations

import os
import re
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "arcgis")
sys.path.insert(0, os.path.abspath(TOOLS))

import mikage_schema as schema  # noqa: E402

SQL_PATH = os.path.join(TOOLS, "dashboard_views_oracle.sql")
DOC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "arcgis-dashboards.md")

#: 作る順（ビューがビューを読むので、この順に流す）と、形状のあるビューの元のテーブル。
VIEWS = {
    "VW_PLAN_HISTORY": "TBL_REQUEST_HISTORY",
    "VW_PLAN_REQUEST_DAY": None,
    "VW_PLAN_SHOT": "TBL_REQUEST_SHOT",
    "VW_PLAN_QUOTA_USAGE": None,
    "VW_PLAN_QUOTA_MONTH": None,
    "VW_PLAN_FIXED_POINT": "TBL_IMAGING_REQUEST",
    "VW_OBS_RECORD": "TBL_OBSERVATION",
    "VW_OBS_THING": "TBL_OBSERVATION",
    "VW_OBS_IMAGE_WORK": None,
    "VW_ALERT_LOG": None,
    "VW_NAMED_AREA_WEEK": None,
    "VW_NAMED_AREA_THING": "TBL_OBSERVATION",
    "VW_NAMED_AREA_STATUS": "TBL_NAMED_AREA",
}


def _text() -> str:
    with open(SQL_PATH, encoding="utf-8") as f:
        return f.read()


def _statements() -> dict:
    """ビューの名前 → コメントを除いた CREATE 文（; なし）。"""
    body = re.sub(r"--[^\n]*", "", _text())
    out = {}
    for stmt in (s.strip() for s in body.split(";")):
        if not stmt:
            continue
        m = re.match(r"CREATE OR REPLACE VIEW (\w+) AS\s", stmt)
        assert m, stmt[:80]
        out[m.group(1)] = stmt
    return out


def test_views_are_created_in_order_and_named_like_the_tables():
    stmts = _statements()
    assert list(stmts) == list(VIEWS)
    for name in stmts:
        assert re.fullmatch(r"VW_[A-Z0-9_]+", name) and len(name) <= 30, name
    # ほかのビューは先に作ったものだけを読む
    for i, (name, stmt) in enumerate(stmts.items()):
        used = set(re.findall(r"\b(VW_\w+)\b", stmt)) - {name}
        assert used <= set(list(VIEWS)[:i]), f"{name} が後のビューを読む: {used}"


def test_header_drops_every_view_in_reverse_order():
    header = _text().split("CREATE OR REPLACE VIEW", 1)[0]
    drops = re.findall(r"DROP VIEW (\w+);", header)
    assert drops == list(reversed(list(VIEWS)))


def test_spatial_views_take_objectid_and_shape_from_the_same_table():
    stmts = _statements()
    for name, table in VIEWS.items():
        # 最後の SELECT（ビューの列）の形状の行と、そのすぐ前の行
        final = stmts[name].rsplit("\nSELECT", 1)[1]
        shape = re.search(r"^\s+(\w+)\.SHAPE,$", final, re.MULTILINE)
        assert bool(shape) == bool(table), name
        if not table:
            continue  # 形状のないビューの OBJECTID は test_columns_exist で（最初の列であること）
        assert schema.BY_NAME[table].geometry, table
        alias = shape.group(1)
        assert re.search(rf"^\s+{alias}\.OBJECTID,\n\s+{alias}\.SHAPE,$", final, re.MULTILINE), name


def test_tables_read_by_the_views_exist():
    names = {t.name for t in schema.TABLES}
    for name, stmt in _statements().items():
        for table in set(re.findall(r"\b(TBL_\w+)\b", stmt)):
            assert table in names, f"{name}: {table}"


def test_doc_lists_every_view():
    with open(DOC_PATH, encoding="utf-8") as f:
        doc = f.read()
    for name in VIEWS:
        assert f"`{name}`" in doc, name


def test_columns_exist():
    """sqlglot でビューを解析し、読む列がテーブル（または先に作ったビュー）にあること。"""
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot.optimizer.qualify import qualify

    tables = {}
    for t in schema.TABLES:
        cols = {"OBJECTID": "INT"}
        if t.geometry:
            cols["SHAPE"] = "TEXT"
        cols.update({f.name: "TEXT" for f in schema.all_fields(t)})
        tables[t.name] = cols
    for name, stmt in _statements().items():
        # sqlglot が読めない Oracle の書き方（TO_DATE の DEFAULT … ON CONVERSION ERROR）を外して解析する
        tree = sqlglot.parse_one(stmt.replace("DEFAULT NULL ON CONVERSION ERROR", ""), dialect="oracle")
        query = qualify(tree.expression.copy(), schema=tables, dialect="oracle",
                        validate_qualify_columns=True, quote_identifiers=False)
        cols = [c.alias_or_name for c in query.selects]
        assert cols[0] == "OBJECTID", name
        assert len(cols) == len(set(cols)), name
        for c in cols:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", c) and len(c) <= 30, f"{name}.{c}"
        tables[name] = {c: "TEXT" for c in cols}
