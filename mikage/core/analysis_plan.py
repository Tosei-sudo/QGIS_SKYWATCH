"""
「データ分析」 の読み込みの計画 (tsudo, 2026-11-17: 「最適化が必要」, then
「第三段階以外実装して」): what of a query is done on the Esri server, so that
less comes to the PC.

The query still runs on the PC (core/analysis.py); the server only leaves out
rows and columns the query would leave out anyway. So the server may give
more than the query needs, never less: what it might read otherwise is not
sent, and what it refuses is no error (the table is read as before, with its
読み込み条件 only). The Esri tables are ArcSDE on Oracle (tsudo, 2026-11-17:
「SDEはオラクル上に対応していればいい」), and what is sent is written for it:
an empty text is NULL there, its LIKE minds the case, and its order of texts
may not be SQLite's.

Three steps: the SQL is read (`plan_of`: its tables, its conditions and its
join keys), the reading is planned (`prepare_reads`: the layers' fields, the
columns the SQL reads, what goes to the server, how many rows each table has
and the order), then the tables are read (`read_tables`). 「読み込み計画」
shows the second (`describe`).

1. 列: only the columns the SQL reads (SQLite says which, compiling it on
   empty tables: `used_columns`), and the object id.
2. 条件: the WHERE and the ON of each join, split at their top-level ANDs. A
   part on the columns of one table goes to that table's Esri WHERE when the
   server keeps every row SQLite would: numbers (comparisons, IN, BETWEEN),
   IS [NOT] NULL, texts by = / IN / LIKE (not under a NOT: `UPPER(F) LIKE
   'X%'` when the pattern has letters, SQLite's LIKE ignoring the case of
   a–z) and dates against 'YYYY-MM-DD…' (a day more each side: the PC's text
   is local time, the server's an instant). Not sent: the order of texts
   (< >), text <> and NOT IN, '' (NULL on Oracle), functions, arithmetic,
   subqueries. What of an AND inside a part cannot go is left out (the rest
   keeps more rows). The right table of a LEFT JOIN gets a WHERE part only
   when the part is not true for its missing rows (all NULL), and the parts
   of its own ON.
3. 結合キー: the Esri tables are read smallest first (a count asked of the
   server), a joined table only for the keys of a table read before it:
   `KEY IN (…)` (CHUNK values a request), or `KEY BETWEEN min AND max` when
   that takes fewer requests. Both ways along an inner join (or an
   `a.X = b.Y` of the WHERE), from the kept side to the other along a LEFT
   JOIN. Used when it takes fewer requests than the table (or the table is
   over its limit).
4. Answers kept a few minutes: core/analysis_cache.py.

A statement not understood (WITH, UNION, RIGHT / FULL / NATURAL JOIN, a
subquery or a function in FROM …) keeps all rows (only the columns are left
out), and so does a table the SQL names more than once (a self join, a
subquery).
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Optional, Sequence

from . import analysis_cache
from .analysis import SHAPE, AnalysisError, Column, Table, _reading_only, _sql_message, esri_columns, fold, parameters, read_esri

#: Values in one `IN (…)` (Oracle takes up to 1,000).
CHUNK = 500
#: The most `IN` requests for a table whose size the server did not say.
UNKNOWN_CHUNKS = 10
#: The rows of one page when the layer says nothing (query_all's own).
PAGE = 2000

_NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
#: The names SQLite gives a row's own number (a table without such a column).
_ROWID = {"rowid", "oid", "_rowid_"}
_KEYWORDS = frozenset("""
    SELECT FROM WHERE GROUP HAVING WINDOW ORDER LIMIT OFFSET UNION INTERSECT EXCEPT WITH VALUES JOIN INNER LEFT RIGHT FULL OUTER
    CROSS NATURAL ON USING AS INDEXED AND OR NOT IN BETWEEN IS NULL LIKE GLOB REGEXP MATCH ESCAPE CASE WHEN THEN ELSE END EXISTS
    COLLATE DISTINCT ALL BY ISNULL NOTNULL CAST
""".split())
_TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
  | (?P<blob>[xX]'[^']*')
  | (?P<str>'(?:[^']|'')*')
  | (?P<qid>"(?:[^"]|"")*"|\[[^\]]*\]|`(?:[^`]|``)*`)
  | (?P<num>0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<param>:[^\W\d]\w*|[@$][^\W\d]\w*|\?\d*)
  | (?P<word>[^\W\d]\w*)
  | (?P<op>\|\||<<|>>|<=|>=|<>|!=|==|->>|->|[=<>])
  | (?P<punct>[(),.;])
  | (?P<other>[^'"\[`])
""", re.VERBOSE | re.DOTALL)
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")

_NUMERIC_TYPES = {"esrifieldtypeoid", "esrifieldtypesmallinteger", "esrifieldtypeinteger", "esrifieldtypebiginteger",
                  "esrifieldtypesingle", "esrifieldtypedouble"}

# Why a condition stays on the PC (「読み込み計画」).
WHY_PARSE = "関数・計算・副問い合わせなどを含む（PC で計算）"
WHY_TWO = "2 つ以上の表にまたがる"
WHY_WHICH = "どの表の列か決まらない（表の別名を付けると送れる）"
WHY_TWICE = "SQL に同じ表が 2 回以上ある（自己結合・副問い合わせ）"
WHY_NULL = "LEFT JOIN の右の表で、結合する行がないとき（NULL）も真になる"
WHY_ON = "LEFT JOIN の条件で、右の表以外の列（左の表の行は減らない）"
WHY_ROWID = "rowid を読む（行を減らすと値が変わる）"
WHY_REFUSED = "サーバーが受け付けない"


# ---- tokens ------------------------------------------------------------------------------------

def tokens(sql: str) -> Optional[list[tuple[str, str]]]:
    """(kind, text) of the SQL, comments left out (keywords upper case, kind "kw"); None for a quote left open."""
    out, i = [], 0
    while i < len(sql or ""):
        m = _TOKEN.match(sql, i)
        if not m or m.end() == i:
            return None
        kind = m.lastgroup
        if kind not in ("space", "comment"):
            value = m.group(0)
            if kind == "word" and value.upper() in _KEYWORDS:
                kind, value = "kw", value.upper()
            out.append((kind, value))
        i = m.end()
    return out


def _name(kind: str, value: str) -> str:
    if kind != "qid":
        return value
    inner = value[1:-1]
    return inner.replace('""', '"') if value[0] == '"' else inner.replace("``", "`") if value[0] == "`" else inner


def _opens(t: tuple[str, str]) -> bool:
    return t == ("punct", "(") or t == ("kw", "CASE")


def _closes(t: tuple[str, str]) -> bool:
    return t == ("punct", ")") or t == ("kw", "END")


def _text(toks: Sequence[tuple[str, str]]) -> str:
    """Tokens as text again (for messages)."""
    out, before = "", ""
    for k, v in toks:
        if out and not (v in (")", ",", ".") and k == "punct") and not out.endswith(("(", ".")) \
                and not ((k, v) == ("punct", "(") and before == "word"):
            out += " "
        out += v
        before = k
    return out


# ---- conditions --------------------------------------------------------------------------------

def _parts(toks: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """
    The tokens split at the ANDs outside parentheses and CASE … END (not the
    AND of a BETWEEN); all of them as one part when an OR is outside them
    too (AND before OR: `a OR b AND c` is `a OR (b AND c)`).
    """
    depth = 0
    for t in toks:
        depth += _opens(t) - _closes(t)
        if t == ("kw", "OR") and depth == 0:
            return [toks] if toks else []
    parts, now, depth, between = [], [], 0, set()
    for t in toks:
        if _opens(t):
            depth += 1
        elif _closes(t):
            depth -= 1
        elif t == ("kw", "BETWEEN"):
            between.add(depth)
        elif t == ("kw", "AND"):
            if depth in between:
                between.discard(depth)
            elif depth == 0:
                parts.append(now)
                now = []
                continue
        now.append(t)
    parts.append(now)
    return [p for p in parts if p]


class _Parser:
    """
    A condition of comparisons, IN, BETWEEN, LIKE, IS [NOT] NULL, AND, OR,
    NOT and parentheses, as a tree; ValueError for anything else.
    """

    def __init__(self, toks: list[tuple[str, str]]):
        self.tokens = toks
        self.i = 0

    def peek(self, offset: int = 0) -> tuple[str, str]:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else ("", "")

    def take(self, kind: str, value: Optional[str] = None) -> str:
        k, v = self.peek()
        if k != kind or (value is not None and v != value):
            raise ValueError(f"{kind} {value or ''}")
        self.i += 1
        return v

    def is_(self, kind: str, value: Optional[str] = None) -> bool:
        k, v = self.peek()
        return k == kind and (value is None or v == value)

    def parse(self) -> tuple:
        tree = self.or_()
        if self.i != len(self.tokens):
            raise ValueError("left over")
        return tree

    def or_(self) -> tuple:
        items = [self.and_()]
        while self.is_("kw", "OR"):
            self.i += 1
            items.append(self.and_())
        return items[0] if len(items) == 1 else ("or", items)

    def and_(self) -> tuple:
        items = [self.not_()]
        while self.is_("kw", "AND"):
            self.i += 1
            items.append(self.not_())
        return items[0] if len(items) == 1 else ("and", items)

    def not_(self) -> tuple:
        if self.is_("kw", "NOT"):
            self.i += 1
            return ("not", self.not_())
        return self.predicate()

    def predicate(self) -> tuple:
        if self.is_("punct", "("):
            self.i += 1
            tree = self.or_()
            self.take("punct", ")")
            return tree
        left = self.operand()
        if self.is_("op"):
            op = self.take("op")
            if op not in ("=", "==", "!=", "<>", "<", "<=", ">", ">="):
                raise ValueError("operator")
            return ("cmp", {"==": "=", "!=": "<>"}.get(op, op), left, self.operand())
        if self.is_("kw", "IS"):
            self.i += 1
            negated = self.is_("kw", "NOT")
            if negated:
                self.i += 1
            self.take("kw", "NULL")
            return ("null", left, negated)
        negated = self.is_("kw", "NOT")
        if negated:
            self.i += 1
        if self.is_("kw", "IN"):
            self.i += 1
            self.take("punct", "(")
            items = [self.operand()]
            while self.is_("punct", ","):
                self.i += 1
                items.append(self.operand())
            self.take("punct", ")")
            return ("in", left, items, negated)
        if self.is_("kw", "BETWEEN"):
            self.i += 1
            low = self.operand()
            self.take("kw", "AND")
            return ("between", left, low, self.operand(), negated)
        if self.is_("kw", "LIKE"):
            self.i += 1
            pattern = self.operand()
            escape = None
            if self.is_("kw", "ESCAPE"):
                self.i += 1
                escape = self.operand()
            return ("like", left, pattern, escape, negated)
        raise ValueError("no operator")

    def operand(self) -> tuple:
        kind, value = self.peek()
        if kind == "num":
            self.i += 1
            return _number(value)
        if kind == "other" and value == "-" and self.peek(1)[0] == "num":
            self.i += 2
            n = _number(self.tokens[self.i - 1][1])
            return (n[0], "-" + n[1])
        if kind == "str":
            self.i += 1
            return ("str", value[1:-1].replace("''", "'"))
        if kind == "param" and value.startswith(":"):
            self.i += 1
            return ("param", value[1:])
        if kind in ("word", "qid"):
            self.i += 1
            first = _name(kind, value)
            if self.is_("punct", "."):
                self.i += 1
                k, v = self.peek()
                if k not in ("word", "qid"):
                    raise ValueError("column")
                self.i += 1
                return ("col", first, _name(k, v))
            if self.is_("punct", "("):
                raise ValueError("function")
            return ("col", None, first)
        raise ValueError("operand")


def _number(text: str) -> tuple:
    """("num", text) for a plain decimal (a leading `.` made `0.`), ("numx", text) for 1e3, 0x1F …."""
    if _NUMBER.fullmatch(text):
        return ("num", "0" + text if text.startswith(".") else text)
    return ("numx", text)


def _columns(tree: tuple) -> list[tuple]:
    kind = tree[0]
    if kind == "col":
        return [tree]
    if kind in ("or", "and"):
        return [c for t in tree[1] for c in _columns(t)]
    if kind == "not":
        return _columns(tree[1])
    if kind == "cmp":
        return _columns(tree[2]) + _columns(tree[3])
    if kind == "null":
        return _columns(tree[1])
    if kind == "in":
        return _columns(tree[1]) + [c for t in tree[2] for c in _columns(t)]
    if kind == "between":
        return _columns(tree[1]) + _columns(tree[2]) + _columns(tree[3])
    if kind == "like":
        return _columns(tree[1]) + _columns(tree[2]) + (_columns(tree[3]) if tree[3] else [])
    return []


def _flat(tree: tuple) -> list[tuple]:
    """The ANDed parts of a tree (parentheses around ANDs too)."""
    return [p for t in tree[1] for p in _flat(t)] if tree[0] == "and" else [tree]


def _sql(tree: tuple, operand: Callable[[tuple], Optional[str]]) -> Optional[str]:
    """The tree as SQL text, each operand written by `operand` (None when one cannot be)."""
    kind = tree[0]
    if kind in ("or", "and"):
        items = [_sql(t, operand) for t in tree[1]]
        return None if None in items else "(" + f" {kind.upper()} ".join(items) + ")"
    if kind == "not":
        inner = _sql(tree[1], operand)
        return None if inner is None else f"(NOT {inner})"
    if kind == "cmp":
        a, b = operand(tree[2]), operand(tree[3])
        return None if a is None or b is None else f"{a} {tree[1]} {b}"
    if kind == "null":
        a = operand(tree[1])
        return None if a is None else f"{a} IS {'NOT ' if tree[2] else ''}NULL"
    if kind == "in":
        a, items = operand(tree[1]), [operand(t) for t in tree[2]]
        return None if a is None or None in items else f"{a} {'NOT ' if tree[3] else ''}IN ({', '.join(items)})"
    if kind == "between":
        a, low, high = operand(tree[1]), operand(tree[2]), operand(tree[3])
        return None if None in (a, low, high) else f"{a} {'NOT ' if tree[4] else ''}BETWEEN {low} AND {high}"
    if kind == "like":
        a, p = operand(tree[1]), operand(tree[2])
        e = operand(tree[3]) if tree[3] else ""
        return None if None in (a, p, e) else f"{a} {'NOT ' if tree[4] else ''}LIKE {p}" + (f" ESCAPE {e}" if e else "")
    return operand(tree)


def _sql_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _sqlite_operand(o: tuple) -> Optional[str]:
    kind = o[0]
    if kind == "col":
        return (f'"{o[1]}".' if o[1] is not None and not re.fullmatch(r"\w+", o[1]) else f"{o[1]}." if o[1] else "") \
            + '"' + o[2].replace('"', '""') + '"'
    if kind in ("num", "numx"):
        return o[1]
    if kind == "str":
        return _sql_quote(o[1])
    if kind == "param":
        return ":" + o[1]
    return None


# ---- the SQL -----------------------------------------------------------------------------------

@dataclass
class _Source:
    """A table of the FROM."""
    alias: str
    table: str
    #: "from" (the first), "inner", "cross" (a comma or CROSS JOIN) or "left".
    join: str
    on: list = field(default_factory=list)
    using: bool = False


_CLAUSES = {"FROM", "WHERE", "GROUP", "HAVING", "WINDOW", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT", "VALUES", "WITH", "SELECT"}
_JOIN_WORDS = {"JOIN", "INNER", "LEFT", "CROSS", "NATURAL", "RIGHT", "FULL", "OUTER"}


def _statement(toks: list[tuple[str, str]]) -> Optional[tuple[list[_Source], list]]:
    """The FROM and the WHERE of one plain SELECT; None for anything else (WITH, UNION, a FROM not understood …)."""
    if toks and toks[-1] == ("punct", ";"):
        toks = toks[:-1]
    if not toks or toks[0] != ("kw", "SELECT"):
        return None
    marks, depth = [], 0
    for i, t in enumerate(toks):
        if _opens(t):
            depth += 1
        elif _closes(t):
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0:
            if t == ("punct", ";"):
                return None
            if t[0] == "kw" and t[1] in _CLAUSES:
                marks.append((t[1], i))
    if depth:
        return None
    names = [n for n, _i in marks]
    if names[:2] != ["SELECT", "FROM"] or any(n in ("SELECT", "UNION", "INTERSECT", "EXCEPT", "VALUES", "WITH") for n in names[1:]):
        return None
    if names.count("FROM") > 1 or names.count("WHERE") > 1 or ("WHERE" in names and names.index("WHERE") != 2):
        return None
    for n, i in marks:
        after = toks[i + 1] if i + 1 < len(toks) else ("", "")
        if n in ("GROUP", "ORDER") and after != ("kw", "BY"):
            return None
        # WINDOW may also be a column's name: only `WINDOW w AS (` is the clause.
        if n == "WINDOW" and not (after[0] in ("word", "qid") and i + 2 < len(toks) and toks[i + 2] == ("kw", "AS")):
            return None
    bounds = [i for _n, i in marks] + [len(toks)]
    sources = _sources(toks[bounds[1] + 1:bounds[2]])
    if sources is None:
        return None
    where = toks[bounds[2] + 1:bounds[3]] if "WHERE" in names else []
    return sources, where


def _sources(toks: list[tuple[str, str]]) -> Optional[list[_Source]]:
    out: list[_Source] = []
    i, join, n = 0, "from", len(toks)

    def at(j: int) -> tuple[str, str]:
        return toks[j] if j < n else ("", "")

    while True:
        k, v = at(i)
        if k not in ("word", "qid"):
            # A subquery, a parenthesised join …
            return None
        name = _name(k, v)
        i += 1
        if at(i) in (("punct", "."), ("punct", "(")):
            # main.T, a table-valued function.
            return None
        alias = name
        if at(i) == ("kw", "AS"):
            i += 1
            if at(i)[0] not in ("word", "qid"):
                return None
            alias = _name(*at(i))
            i += 1
        elif at(i)[0] in ("word", "qid"):
            alias = _name(*at(i))
            i += 1
        if at(i) in (("kw", "INDEXED"), ("kw", "NOT")):
            return None
        on: list = []
        using = False
        if at(i) == ("kw", "ON"):
            i += 1
            start, depth = i, 0
            while i < n:
                t = toks[i]
                if _opens(t):
                    depth += 1
                elif _closes(t):
                    depth -= 1
                elif depth == 0 and (t == ("punct", ",") or (t[0] == "kw" and t[1] in _JOIN_WORDS)):
                    break
                i += 1
            on = toks[start:i]
            if not on:
                return None
        elif at(i) == ("kw", "USING"):
            i += 1
            if at(i) != ("punct", "("):
                return None
            while i < n and toks[i] != ("punct", ")"):
                i += 1
            i += 1
            using = True
        if not out and (on or using):
            return None
        out.append(_Source(alias, name, join, on, using))
        if i >= n:
            return out
        t = toks[i]
        if t == ("punct", ","):
            join, i = "cross", i + 1
        elif t == ("kw", "JOIN"):
            join, i = "inner", i + 1
        elif t in (("kw", "INNER"), ("kw", "CROSS")) and at(i + 1) == ("kw", "JOIN"):
            join, i = ("inner" if t[1] == "INNER" else "cross"), i + 2
        elif t == ("kw", "LEFT"):
            i += 1
            if at(i) == ("kw", "OUTER"):
                i += 1
            if at(i) != ("kw", "JOIN"):
                return None
            join, i = "left", i + 1
        else:
            # NATURAL, RIGHT, FULL …
            return None


# ---- the plan ----------------------------------------------------------------------------------

@dataclass
class Part:
    """A part of the WHERE (or of an ON) on the columns of one table."""
    alias: str
    #: The part as SQLite reads it (for messages).
    text: str
    tree: tuple = field(repr=False, default=())


@dataclass
class Key:
    """A join key the rows of `target` can be read for: the values of `source`'s column (read before)."""
    source: str
    source_column: str
    target: str
    target_column: str


@dataclass
class Plan:
    #: Alias (folded) → table name.
    tables: dict[str, str]
    #: The aliases whose table the SQL names once (the table is read for that alias only).
    own: set[str]
    parts: dict[str, list[Part]]
    keys: list[Key]
    #: Alias (folded; "" for none) → the conditions not sent and why.
    kept: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: The aliases as the SQL writes them.
    names: dict[str, str] = field(default_factory=dict)


def plan_of(sql: str, values: Optional[dict[str, Any]] = None,
            tables: Optional[dict[str, tuple[str, Optional[set[str]]]]] = None) -> Optional[Plan]:
    """
    What can be done on the server for the SQL; None when it is not a plain
    SELECT of the tables `tables` (folded name → (name, its folded column
    names or None when not known)).
    """
    toks = tokens(sql)
    if toks is None:
        return None
    statement = _statement(toks)
    if statement is None:
        return None
    sources, where = statement
    tables = tables or {}
    by_alias: dict[str, _Source] = {}
    for s in sources:
        if fold(s.table) not in tables or fold(s.alias) in by_alias:
            return None
        by_alias[fold(s.alias)] = s
    names = {a: tables[fold(s.table)][0] for a, s in by_alias.items()}
    # A table named once: in its FROM only (a qualifier `T.x` aside).
    seen: dict[str, int] = {}
    for i, (k, v) in enumerate(toks):
        if k in ("word", "qid"):
            qualifier = i + 1 < len(toks) and toks[i + 1] == ("punct", ".") and not (i and toks[i - 1] == ("punct", "."))
            if not qualifier:
                seen[fold(_name(k, v))] = seen.get(fold(_name(k, v)), 0) + 1
    own = {a for a, s in by_alias.items() if seen.get(fold(s.table), 0) == 1}
    null_side = {fold(s.alias) for s in sources if s.join == "left"}
    plan = Plan(names, own, {}, [], {}, {a: s.alias for a, s in by_alias.items()})

    def resolve(c: tuple) -> Optional[str]:
        if c[1] is not None:
            return fold(c[1]) if fold(c[1]) in by_alias else None
        if len(sources) == 1:
            return fold(sources[0].alias)
        found = []
        for a, s in by_alias.items():
            columns = tables[fold(s.table)][1]
            if columns is None:
                return None
            if fold(c[2]) in columns:
                found.append(a)
        return found[0] if len(found) == 1 else None

    def keep(alias: str, text: str, why: str) -> None:
        plan.kept.setdefault(alias, []).append((text, why))

    def add(piece: list, right: Optional[str]) -> None:
        """A part of the WHERE (`right` None) or of the ON of a LEFT JOIN of `right`."""
        try:
            tree = _Parser(piece).parse()
        except ValueError:
            keep("", _text(piece), WHY_PARSE)
            return
        for p in _flat(tree):
            text = _sql(p, _sqlite_operand) or _text(piece)
            columns = _columns(p)
            if not columns:
                continue
            found = [resolve(c) for c in columns]
            if None in found:
                keep("", text, WHY_WHICH)
                continue
            if p[0] == "cmp" and p[1] == "=" and p[2][0] == "col" and p[3][0] == "col" and found[0] != found[1]:
                a, b = found
                if right is None:
                    plan.keys += [Key(a, p[2][2], b, p[3][2]), Key(b, p[3][2], a, p[2][2])]
                elif right in (a, b):
                    other, mine = (a, b) if b == right else (b, a)
                    plan.keys.append(Key(other, (p[2] if other == a else p[3])[2], mine, (p[3] if other == a else p[2])[2]))
                continue
            if len(set(found)) != 1:
                keep("", text, WHY_TWO)
                continue
            alias = found[0]
            if right is not None and alias != right:
                keep(alias, text, WHY_ON)
                continue
            if alias not in own:
                keep(alias, text, WHY_TWICE)
                continue
            if right is None and alias in null_side and not _null_rejected(p, values or {}):
                keep(alias, text, WHY_NULL)
                continue
            plan.parts.setdefault(alias, []).append(Part(alias, text, p))

    for piece in _parts(where):
        add(piece, None)
    for s in sources[1:]:
        for piece in _parts(s.on):
            # The ON of an inner join is a WHERE; that of a LEFT JOIN narrows its right table only.
            add(piece, fold(s.alias) if s.join == "left" else None)
    return plan


def _null_rejected(tree: tuple, values: dict[str, Any]) -> bool:
    """Whether the part is not true when the table's columns are all NULL (a row a left join adds for no match)."""
    params: dict[str, Any] = {}

    def operand(o: tuple) -> Optional[str]:
        if o[0] == "col":
            return "NULL"
        if o[0] == "param":
            if values.get(o[1]) is None:
                return None
            params[o[1]] = values[o[1]]
        return _sqlite_operand(o)

    text = _sql(tree, operand)
    if text is None:
        return False
    conn = sqlite3.connect(":memory:")
    try:
        return conn.execute(f"SELECT CASE WHEN ({text}) THEN 1 ELSE 0 END", params).fetchone()[0] == 0
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# ---- the columns the SQL reads -----------------------------------------------------------------

def used_columns(sql: str, schemas: dict[str, Sequence[Column]], values: Optional[dict[str, Any]] = None) -> Optional[dict[str, set[str]]]:
    """
    The columns (folded) the SQL reads of each table (folded name), as SQLite
    says compiling it on empty tables of `schemas`; None for SQL that cannot
    be read so (EXPLAIN …, a USING or NATURAL JOIN: SQLite's authorizer is
    not told of the columns they compare). An error of the SQL is raised
    (AnalysisError) before anything is read.
    """
    text = (sql or "").strip().rstrip(";").strip()
    toks = tokens(text)
    if not text or not toks or toks[0][1].upper() == "EXPLAIN":
        return None
    if ("kw", "USING") in toks or ("kw", "NATURAL") in toks:
        # SQLite does not say what a USING or a NATURAL JOIN compares (it would join on fewer columns).
        return None
    conn = sqlite3.connect(":memory:")
    try:
        for name, columns in schemas.items():
            spec = ", ".join('"' + c.name.replace('"', '""') + f'" {c.type}' for c in columns)
            if not spec:
                return None
            try:
                conn.execute(f'CREATE TABLE "{name.replace(chr(34), chr(34) * 2)}" ({spec})')
            except sqlite3.Error:
                return None
        found: dict[str, set[str]] = {fold(n): set() for n in schemas}
        read = getattr(sqlite3, "SQLITE_READ", 20)

        def authorizer(action: int, arg1: Any = None, arg2: Any = None, *rest) -> int:
            if action == read and isinstance(arg1, str) and isinstance(arg2, str) and arg2:
                found.setdefault(fold(arg1), set()).add(fold(arg2))
            return _reading_only(action, arg1, arg2, *rest)

        conn.set_authorizer(authorizer)
        try:
            conn.execute("EXPLAIN " + text, {n: (values or {}).get(n) for n in parameters(text)})
        except (sqlite3.Error, ValueError, OverflowError) as error:
            raise AnalysisError(_sql_message(error)) from None
        return found
    finally:
        conn.close()


# ---- Esri text of a part -----------------------------------------------------------------------

def _kind(field_type: str) -> Optional[str]:
    """"number", "text", "date" or None (GUIDs, shapes …: not compared on the server)."""
    t = (field_type or "").lower()
    if t in _NUMERIC_TYPES:
        return "number"
    if t == "esrifieldtypestring":
        return "text"
    if t == "esrifieldtypedate":
        return "date"
    return None


def fields_of(info_fields: Sequence[dict]) -> dict[str, tuple[str, str]]:
    """Lower-case name → (name, type) of a layer's fields."""
    return {f["name"].lower(): (f["name"], str(f.get("type", ""))) for f in info_fields
            if isinstance(f, dict) and isinstance(f.get("name"), str)}


def _day(text: str, days: int) -> Optional[str]:
    """`TIMESTAMP '…'` of the day of 'YYYY-MM-DD…' moved by `days`, at 00:00."""
    m = _DATE.match(text or "")
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) + timedelta(days=days)
    except (ValueError, OverflowError):
        return None
    return f"TIMESTAMP '{d.isoformat()} 00:00:00'"


def like_text(name: str, pattern: str, escape: Optional[str]) -> tuple[Optional[str], str]:
    """
    `F LIKE 'p'` as Oracle keeps every row SQLite does (SQLite ignoring the
    case of a–z): `UPPER(F) LIKE 'P'` when the pattern has letters.
    """
    if pattern == "":
        return None, "空文字（Oracle では NULL）"
    if escape is not None and (len(escape) != 1 or escape.upper() != escape.lower()):
        return None, "ESCAPE は記号 1 文字"
    if any(ord(ch) > 127 and ch.upper() != ch.lower() for ch in pattern):
        return None, "大文字・小文字のある英字以外の文字（全角英字など）を含むパターン"
    end = f" ESCAPE {_sql_quote(escape)}" if escape is not None else ""
    if any("a" <= ch.lower() <= "z" for ch in pattern if ord(ch) < 128):
        return f"UPPER({name}) LIKE {_sql_quote(pattern.translate(_ASCII_UPPER))}{end}", ""
    return f"{name} LIKE {_sql_quote(pattern)}{end}", ""



def esri_text(part: Part, fields: dict[str, tuple[str, str]], values: Optional[dict[str, Any]] = None) -> tuple[Optional[str], str]:
    """
    The part as an Esri WHERE for Oracle (field names as the layer has them,
    `:name` as its value), and "" — or None and why, when the server might
    leave out a row SQLite keeps.
    """
    values = values or {}
    why: list[str] = []

    def no(reason: str) -> None:
        why.append(reason)
        return None

    def value(o: tuple) -> Optional[str]:
        if o[0] == "str":
            return o[1]
        if o[0] == "param":
            v = values.get(o[1])
            return None if v is None else str(v)
        return None

    def number(o: tuple) -> Optional[str]:
        if o[0] == "col":
            return fields[o[2].lower()][0]
        if o[0] == "num":
            return o[1]
        text = value(o)
        if text is None:
            return no("未入力の値" if o[0] == "param" else "数の書き方（1e3 など）")
        # A number column compares a text that is a number as that number (SQLite's affinity).
        return text if _NUMBER.fullmatch(text) else no("数の列と数でない値の比較")

    def text_value(o: tuple) -> Optional[str]:
        if o[0] == "col":
            return fields[o[2].lower()][0]
        text = value(o)
        if text is None:
            return no("未入力の値" if o[0] == "param" else "文字の列と数の比較")
        if text == "":
            return no("空文字（Oracle では NULL）")
        return _sql_quote(text)

    def predicate(p: tuple, positive: bool) -> Optional[str]:
        columns = _columns(p)
        if not columns:
            return no("列のない式")
        kinds = set()
        for c in columns:
            f = fields.get(c[2].lower())
            if f is None:
                return no(f"表にない列: {c[2]}")
            if not _FIELD.fullmatch(f[0]):
                return no(f"サーバーに送れない列名: {f[0]}")
            kinds.add(_kind(f[1]))
        if None in kinds:
            return no("数・文字・日付以外の列（GUID など）")
        if len(kinds) > 1:
            return no("型の違う列どうしの比較")
        kind = kinds.pop()
        if p[0] == "null":
            return f"{fields[p[1][2].lower()][0]} IS {'NOT ' if p[2] else ''}NULL" if p[1][0] == "col" else no("列のない式")
        if kind == "number":
            if p[0] == "like":
                return no("数の列の LIKE")
            return _sql(p, number)
        if not positive or p[-1] is True:
            return no("NOT・<> の中の文字・日付の条件（Oracle で残る行が SQLite と違うことがある）")
        if kind == "text":
            if p[0] == "cmp" and p[1] == "=":
                return _sql(p, text_value)
            if p[0] == "in" and p[1][0] == "col" and all(o[0] != "col" for o in p[2]):
                return _sql(p, text_value)
            if p[0] == "like" and p[1][0] == "col":
                pattern = value(p[2])
                escape = value(p[3]) if p[3] else None
                if pattern is None or (p[3] and escape is None):
                    return no("LIKE のパターンが値でない（未入力・列）")
                text, reason = like_text(fields[p[1][2].lower()][0], pattern, escape)
                return text if text else no(reason)
            return no("文字の大小比較・<>（Oracle の並び順や NULL の扱いが SQLite と違うことがある）")
        # Dates: the PC's text is local time ('YYYY-MM-DD HH:MM:SS'), the server's an instant: a day more each side.
        if p[0] == "cmp":
            column, op, other = (p[2], p[1], p[3]) if p[2][0] == "col" else (p[3], {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(p[1], p[1]), p[2])
            name = fields[column[2].lower()][0]
            text = value(other)
            low, high = _day(text or "", -1), _day(text or "", 2)
            if other[0] == "col" or low is None or high is None:
                return no("日付は 'YYYY-MM-DD…' の値と比べると送れる")
            if op in (">", ">="):
                return f"{name} >= {low}"
            if op in ("<", "<="):
                return f"{name} < {high}"
            if op == "=":
                return f"({name} >= {low} AND {name} < {high})"
            return no("日付の <>")
        if p[0] == "between" and p[1][0] == "col":
            name = fields[p[1][2].lower()][0]
            low, high = _day(value(p[2]) or "", -1), _day(value(p[3]) or "", 2)
            if low is None or high is None:
                return no("日付は 'YYYY-MM-DD…' の値と比べると送れる")
            return f"({name} >= {low} AND {name} < {high})"
        return no("日付の IN・LIKE")

    def walk(t: tuple, positive: bool) -> Optional[str]:
        if t[0] in ("or", "and"):
            items = [walk(x, positive) for x in t[1]]
            # Leaving out a part of an AND keeps more rows (of an OR under NOT too).
            if (t[0] == "and") == positive:
                items = [x for x in items if x]
                if not items:
                    return None
            elif None in items:
                return None
            return items[0] if len(items) == 1 else "(" + f" {t[0].upper()} ".join(items) + ")"
        if t[0] == "not":
            inner = walk(t[1], not positive)
            return None if inner is None else f"(NOT {inner})"
        return predicate(t, positive)

    text = walk(part.tree, True)
    return (text, "") if text else (None, "、".join(dict.fromkeys(why)) or "送れない")


# ---- join keys ---------------------------------------------------------------------------------

def _column_kind(column: Column) -> Optional[str]:
    return "number" if column.type in ("INTEGER", "REAL") else "text" if column.type == "TEXT" else None


def key_values(columns: Sequence[Column], rows: Sequence[Sequence[Any]], name: str, kind: str) -> Optional[list[str]]:
    """
    The values of column `name` as Esri literals of `kind` (each once; NULL
    and '' left out: they join nothing on Oracle), or None when one cannot be
    written so that the server matches what SQLite matches (a float in a text
    column …).
    """
    from .analysis import plain
    at = next((i for i, c in enumerate(columns) if fold(c.name) == fold(name)), None)
    if at is None or _column_kind(columns[at]) != kind:
        return None
    out: dict[str, None] = {}
    for r in rows:
        v = plain(r[at]) if at < len(r) else None
        if v is None:
            continue
        if kind == "number":
            if isinstance(v, int):
                out[str(v)] = None
            elif isinstance(v, float):
                if v.is_integer() and abs(v) < 2 ** 53:
                    out[str(int(v))] = None
                else:
                    text = repr(v)
                    if not _NUMBER.fullmatch(text):
                        return None
                    out[text] = None
            elif isinstance(v, str):
                # Kept as text by SQLite unless it is a number: text never equals a number.
                if _NUMBER.fullmatch(v.strip()):
                    out[v.strip()] = None
                elif _maybe_number(v):
                    return None
            else:
                return None
        else:
            if isinstance(v, str):
                if v:
                    out[_sql_quote(v)] = None
            elif isinstance(v, int):
                out[_sql_quote(str(v))] = None
            else:
                return None
    return list(out)


def _maybe_number(text: str) -> bool:
    """Whether SQLite might take a text for a number (1e3, +5 …) that `_NUMBER` does not write."""
    try:
        float(text)
    except ValueError:
        return False
    return True


def worth(keys: int, count: Optional[int], page: int, limit: int) -> bool:
    """Whether reading by `keys` values takes fewer requests than reading the table (or the table is over its limit)."""
    chunks = math.ceil(keys / CHUNK)
    if count is None:
        return chunks <= UNKNOWN_CHUNKS
    if count > limit:
        return True
    return chunks < math.ceil(count / max(1, page))


# ---- planning the reading ----------------------------------------------------------------------

@dataclass
class Read:
    table: Table
    columns: list[Column]
    rows: list[list[Any]]
    #: What was done on the server (Japanese, "" for nothing).
    note: str = ""


@dataclass
class TableRead:
    """How one table is read (what 「読み込み計画」 shows)."""
    table: Table
    reader: Any = None
    #: Its alias in the SQL (folded; "" when the plan has none for it).
    alias: str = ""
    info: Any = None
    #: Why its layer information could not be read.
    problem: str = ""
    fields: dict = field(default_factory=dict)
    #: All its columns (SHAPE among them when the SQL reads shapes).
    columns: list[Column] = field(default_factory=list)
    #: The columns (folded) the SQL reads; None for all.
    wanted: Optional[set[str]] = None
    pushed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    count: Optional[int] = None
    keys: list[Key] = field(default_factory=list)
    refused: bool = False

    @property
    def esri(self) -> bool:
        return self.reader is None


@dataclass
class Reading:
    sql: str
    plan: Optional[Plan]
    #: In the order they are read (project layers first).
    tables: list[TableRead]
    shape: bool
    #: Whether the SQL compiled on empty tables (its columns known).
    compiled: bool = False


def _base(table: Table, condition: str) -> Optional[list[str]]:
    """The settings file's `where` and the 読み込み条件 (None when that cannot be sent: read_esri says why)."""
    from .esri import EsriError, own_condition
    base = [f"({table.where})"] if table.where and table.where.strip() else []
    if condition and condition.strip():
        try:
            base.append(own_condition(condition))
        except EsriError:
            return None
    return base


def _count(client: Any, table: Table, clauses: Sequence[str], token: Optional[str], stamp: Optional[float] = None,
           minutes: float = 0) -> Optional[int]:
    from .esri import EsriError
    from .util import is_number
    where = " AND ".join(clauses) or "1=1"
    known = analysis_cache.count(table.url, where, token, stamp, minutes)
    if known is not None:
        return known
    try:
        n = client.ask(f"{table.url}/query", {"where": where, "returnCountOnly": "true"}, token).get("count")
    except EsriError:
        return None
    if not is_number(n):
        return None
    analysis_cache.keep_count(table.url, where, token, stamp, int(n), minutes)
    return int(n)


def prepare_reads(client: Any, prepared: Sequence[tuple[Table, Any]], sql: str, values: Optional[dict[str, Any]],
                  conditions: dict[str, str], shape: bool, token_for: Callable[[Table], Optional[str]], count_all: bool = False,
                  cache_minutes: float = 0) -> Reading:
    """
    The plan of reading the tables of `prepared` (a project layer by its
    reader, an Esri table by read_esri) for the SQL: their layer
    information, the columns read, the conditions sent and the order. An
    error of the SQL is raised here (AnalysisError), before anything is read.
    """
    from .esri import EsriError
    values = values or {}
    reads = [TableRead(t, reader) for t, reader in prepared]
    for r in reads:
        if r.esri:
            try:
                r.info = client.layer(r.table.url, token_for(r.table))
            except EsriError as error:
                # Read as before (read_esri says what is wrong).
                r.problem = str(error)
                continue
            fields = [f for f in r.info.raw.get("fields") or [] if isinstance(f, dict) and isinstance(f.get("name"), str)]
            r.fields = fields_of(fields)
            r.columns = esri_columns(r.table, fields)
            if r.info.geometry_type and shape and not any(c.name.upper() == SHAPE for c in r.columns):
                r.columns.append(Column(SHAPE, "TEXT", "", "形状（WKT）"))
        else:
            r.columns = list(getattr(r.reader, "columns", None) or r.table.columns)
    used = None
    if all(r.columns and not r.problem for r in reads):
        used = used_columns(sql, {r.table.name: r.columns for r in reads}, values)
    known = {fold(r.table.name): (r.table.name, {fold(c.name) for c in r.columns} if r.columns else None) for r in reads}
    plan = plan_of(sql, values, known)
    alias_of = {plan.tables[a]: a for a in plan.own} if plan else {}
    for r in reads:
        if plan:
            mine = [a for a, t in plan.tables.items() if t == r.table.name]
            r.kept = [k for a in mine for k in plan.kept.get(a, [])]
        if not r.esri or r.info is None:
            continue
        rowid = False
        if used is not None:
            r.wanted = used.get(fold(r.table.name), set())
            rowid = bool(r.wanted & _ROWID - {fold(c.name) for c in r.columns})
        a = alias_of.get(r.table.name)
        if plan is None or a is None:
            continue
        r.alias = a
        if rowid:
            r.kept += [(p.text, WHY_ROWID) for p in plan.parts.get(a, [])]
            continue
        for p in plan.parts.get(a, []):
            text, why = esri_text(p, r.fields, values)
            if text:
                r.pushed.append(text)
            else:
                r.kept.append((p.text, why))
        r.keys = [k for k in plan.keys if k.target == a]
    esri = [r for r in reads if r.esri and r.info is not None]
    if any(r.keys for r in esri) or count_all:
        for r in esri:
            base = _base(r.table, conditions.get(r.table.name, ""))
            if base is None:
                continue
            token = token_for(r.table)
            stamp = analysis_cache.stamp_of(r.info.raw)
            mine = [f"({p})" for p in r.pushed]
            r.count = _count(client, r.table, base + mine, token, stamp, cache_minutes)
            if r.count is None and mine:
                r.count = _count(client, r.table, base, token, stamp, cache_minutes)
                if r.count is not None:
                    # The server refuses the conditions: read as before.
                    r.kept += [(p, WHY_REFUSED) for p in r.pushed]
                    r.pushed, r.refused = [], True
    place = {id(r): i for i, r in enumerate(reads)}
    ordered = [r for r in reads if not r.esri] + sorted(
        [r for r in reads if r.esri],
        key=lambda r: (r.count is None, r.count or 0, place[id(r)]) if any(x.keys for x in esri) else (place[id(r)],))
    return Reading(sql, plan, ordered, shape, used is not None)


def read_tables(client: Any, reading: Reading, conditions: dict[str, str], limit: int, token_for: Callable[[Table], Optional[str]],
                cancelled: Optional[Callable[[], bool]] = None, progress: Optional[Callable[[float], None]] = None,
                cache_minutes: float = 0) -> list[Read]:
    """Each table once, as planned; in the order read."""
    out: list[Read] = []
    read: dict[str, Read] = {}
    steps = len(reading.tables) + 1
    plan = reading.plan
    for r in reading.tables:
        if progress is not None:
            progress(100 * len(out) / steps)
        t = r.table
        if not r.esri:
            columns, rows = r.reader.read(cancelled)
            read[t.name] = Read(t, list(columns), list(rows))
            out.append(read[t.name])
            continue
        notes: list[str] = []
        key = None
        span = None
        token = token_for(t)
        if r.keys and r.info is not None and plan is not None:
            best = None
            for k in r.keys:
                if plan.tables.get(k.source) not in read:
                    continue
                f = r.fields.get(k.target_column.lower())
                kind = _kind(f[1]) if f else None
                if kind not in ("number", "text") or not _FIELD.fullmatch(f[0]):
                    continue
                source = read[plan.tables[k.source]]
                found = key_values(source.columns, source.rows, k.source_column, kind)
                if found is not None and (best is None or len(found) < len(best[1])):
                    best = (f[0], found, kind)
            page = max(1, min(PAGE, r.info.max_record_count or PAGE))
            if best is not None:
                if best[2] == "number" and len(best[1]) > CHUNK:
                    span = _span(client, r, best[0], best[1], conditions, page, limit, token, cache_minutes)
                if span is None and worth(len(best[1]), r.count, page, limit):
                    key = (best[0], best[1])
        mine = list(r.pushed) + ([span[0]] if span else [])
        columns, rows = read_esri(client, t, conditions.get(t.name, ""), reading.shape, limit, token, cancelled,
                                  pushed=mine, key=key, notes=notes, fields=r.wanted, cache_minutes=cache_minutes)
        read[t.name] = Read(t, columns, rows, _note(r, key, span, columns, notes))
        out.append(read[t.name])
    return out


def _span(client: Any, r: TableRead, name: str, keys: Sequence[str], conditions: dict[str, str], page: int, limit: int,
          token: Optional[str], cache_minutes: float) -> Optional[tuple[str, str, str]]:
    """`F BETWEEN min AND max` of many number keys, when its rows take fewer requests than the keys' `IN`s."""
    numbers = sorted(keys, key=float)
    low, high = numbers[0], numbers[-1]
    clause = f"{name} BETWEEN {low} AND {high}"
    base = _base(r.table, conditions.get(r.table.name, ""))
    if base is None:
        return None
    n = _count(client, r.table, base + [f"({p})" for p in r.pushed] + [clause], token, analysis_cache.stamp_of(r.info.raw), cache_minutes)
    if n is None or n > limit:
        return None
    pages = max(1, math.ceil(n / page))
    whole = math.ceil(r.count / page) if r.count is not None and r.count <= limit else math.inf
    return (clause, low, high) if pages < math.ceil(len(keys) / CHUNK) and pages < whole else None


def _note(r: TableRead, key: Optional[tuple], span: Optional[tuple], columns: Sequence[Column], notes: Sequence[str]) -> str:
    if "fallback" in notes or r.refused:
        text = "サーバーで絞り込めず、読み込み条件だけで読み込み"
    elif key is not None and not key[1]:
        return "結合する行がないので読み込みなし"
    else:
        done = (["条件"] if r.pushed else []) + ([f"結合キー {len(key[1]):,} 件"] if key else []) \
            + ([f"結合キーの範囲 {span[1]}〜{span[2]}"] if span else [])
        if len(columns) < len(r.table.columns):
            done.append(f"列 {len(columns)}/{len(r.table.columns)}")
        text = "サーバーで絞り込み: " + "・".join(done) if done else ""
    ages = [float(n.split(":", 1)[1]) for n in notes if n.startswith("cache:")]
    if ages:
        cached = f"キャッシュ（{_ago(max(ages))}）" if "read" not in notes else "一部キャッシュ"
        text = f"{text}、{cached}" if text else cached
    return text


def _ago(seconds: float) -> str:
    return "1 分以内" if seconds < 60 else f"{int(seconds // 60)} 分前"


# ---- 「読み込み計画」 ------------------------------------------------------------------------------

def describe(reading: Reading, conditions: dict[str, str], cache_minutes: float = 0) -> str:
    """The plan of reading, in Japanese (for 「読み込み計画」)."""
    plan = reading.plan
    lines = ["読み込み計画（実行前の見込み。結合キーの値は、前の表を読んでから決まる）", ""]
    if plan is None:
        lines += ["・SQL の FROM・WHERE を読めないので、条件と結合キーはサーバーへ送らない（WITH・UNION・副問い合わせの FROM など）。", ""]
    elif not reading.compiled:
        lines += ["・読む列は決められない（全部の列を読む）。", ""]
    names = plan.names if plan else {}
    for i, r in enumerate(reading.tables, 1):
        t = r.table
        title = f"{t.name}（{t.title}）" if t.title and t.title != t.name else t.name
        alias = f"  SQL では {names.get(r.alias, r.alias)}" if r.alias and fold(names.get(r.alias, r.alias)) != fold(t.name) else ""
        lines.append(f"{i}. {title}{alias}")
        if not r.esri:
            lines.append("   プロジェクトのレイヤー（PC で読む）" + (f"。読み込み条件: {conditions[t.name]}" if conditions.get(t.name) else ""))
        elif r.info is None:
            lines.append(f"   レイヤーの情報を読めない: {r.problem}")
        else:
            base = _base(t, conditions.get(t.name, "")) or []
            where = " AND ".join(base + [f"({p})" for p in r.pushed]) or ("なし（結合キーだけ）" if r.keys else "なし（すべての行）")
            if r.count is not None:
                lines.append(f"   件数: {r.count:,} 行（サーバーの答え）")
            lines.append(f"   サーバーへ送る WHERE: {where}")
            before = {x.table.name for x in reading.tables[:i - 1]}
            for k in r.keys:
                source = plan.tables.get(k.source, k.source) if plan else k.source
                if source in before:
                    lines.append(f"   結合キー: 先に読む {source} の {k.source_column} の値で {k.target_column} を絞る"
                                 f"（IN で {CHUNK} 件ずつ、数値で多いときは範囲。全部読むほうが早ければ使わない）")
            if r.wanted is not None:
                read = [c.name for c in r.columns if fold(c.name) in r.wanted or c.oid]
                lines.append(f"   読む列: {len(read)} / {len(r.columns)}（{', '.join(read)}）")
            else:
                lines.append(f"   読む列: すべて（{len(r.columns)}）")
            got = analysis_cache.age(t.url, analysis_cache.stamp_of(r.info.raw), cache_minutes)
            if got is not None:
                lines.append(f"   キャッシュ: {_ago(got)}に読んだ答えがある（同じ問い合わせはサーバーに聞かない）")
        for text, why in r.kept:
            lines.append(f"   送らない条件: {text} … {why}")
        lines.append("")
    if plan is not None:
        for text, why in plan.kept.get("", []):
            lines.append(f"・PC だけで見る条件: {text} … {why}")
    return "\n".join(lines).rstrip() + "\n"
