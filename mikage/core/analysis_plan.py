"""
「データ分析」 の読み込みの計画 (tsudo, 2026-11-17: 「最適化が必要」): what of a
「かんたん集計」 is done on the Esri server, so that fewer rows come to the PC.

The query still runs on the PC (core/analysis.py); this only lets the server
leave out rows the query would leave out anyway. So the server may keep more
rows than the query, never fewer: anything it might read otherwise is not
sent, and a condition the server refuses is no error (the table is read as
before, with its 読み込み条件 only).

1. 条件: the builder's WHERE is split at its top-level ANDs. A part that
   names the columns of one table only, with comparisons (= <> < <= > >=),
   IN, BETWEEN, IS [NOT] NULL and AND / OR / NOT of them, is also sent as
   that table's Esri WHERE. Not sent: dates (local time text on the PC, the
   service's own on the server), LIKE (case on the server depends on the
   database), text against numbers, functions, arithmetic, subqueries. A
   table joined with 「なくても残す」 gets a part only when the part is not
   true for its missing rows (all NULL), else the join would keep rows the
   query leaves out.
2. 結合キー: the Esri tables are read smallest first (a count asked of the
   server), and a joined table only for the keys of a table read before it
   (`KEY IN (…)`, CHUNK values a request): both ways along 「ある行だけ」,
   from the kept side to the other along 「なくても残す」. Used when it takes
   fewer requests than reading the table (or the table is over its limit).

Only when the SQL is the builder's own (unchanged), and only for a table the
SQL names once (a table read for two aliases, or also in a subquery, is read
as before).
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .analysis import AnalysisError, Builder, Column, Table, aliases, build_sql, read_esri, words

#: Values in one `IN (…)` (Oracle takes up to 1,000).
CHUNK = 500
#: The most `IN` requests for a table whose size the server did not say.
UNKNOWN_CHUNKS = 10
#: The rows of one page when the layer says nothing (query_all's own).
PAGE = 2000

_NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ALIAS = re.compile(r"t\d{1,3}", re.IGNORECASE)
_KEYWORDS = {"AND", "OR", "NOT", "IN", "BETWEEN", "IS", "NULL"}
_TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qid>"(?:[^"]|"")+")
  | (?P<num>(?:\d+(?:\.\d+)?|\.\d+)(?![\w.]))
  | (?P<param>:[^\W\d]\w*)
  | (?P<word>[^\W\d]\w*)
  | (?P<op><=|>=|<>|!=|==|=|<|>)
  | (?P<punct>[(),.\-])
  | (?P<other>[+*/%|&~!?@$#])
""", re.VERBOSE)

_NUMERIC_TYPES = {"esrifieldtypeoid", "esrifieldtypesmallinteger", "esrifieldtypeinteger", "esrifieldtypebiginteger",
                  "esrifieldtypesingle", "esrifieldtypedouble"}


# ---- the builder's WHERE, read -----------------------------------------------------------------

def _tokens(text: str) -> Optional[list[tuple[str, str]]]:
    """
    (kind, text) of a condition: arithmetic and the like as "other" (a part
    with it is not sent); None for what may hide an AND (a comment, a quote
    left open, `[name]`, `;` …).
    """
    out, i = [], 0
    while i < len(text):
        if text.startswith(("--", "/*"), i):
            # A comment: what it leaves out of the SQL is not known here.
            return None
        m = _TOKEN.match(text, i)
        if not m or m.end() == i:
            return None
        kind = m.lastgroup
        if kind != "space":
            value = m.group(0)
            if kind == "word" and value.upper() in _KEYWORDS:
                kind, value = "kw", value.upper()
            out.append((kind, value))
        i = m.end()
    return out


def _parts(tokens: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """
    The tokens split at the ANDs outside parentheses (not the AND of a
    BETWEEN); all of them as one part when an OR is outside parentheses too
    (AND before OR: `a OR b AND c` is `a OR (b AND c)`).
    """
    depth = 0
    for kind, value in tokens:
        if kind == "punct" and value in "()":
            depth += 1 if value == "(" else -1
        elif kind == "kw" and value == "OR" and depth == 0:
            return [tokens] if tokens else []
    parts, now, depth, between = [], [], 0, set()
    for kind, value in tokens:
        if value == "(" and kind == "punct":
            depth += 1
        elif value == ")" and kind == "punct":
            depth -= 1
        elif kind == "kw" and value == "BETWEEN":
            between.add(depth)
        elif kind == "kw" and value == "AND":
            if depth in between:
                between.discard(depth)
            elif depth == 0:
                parts.append(now)
                now = []
                continue
        now.append((kind, value))
    parts.append(now)
    return [p for p in parts if p]


class _Parser:
    """A condition of comparisons, IN, BETWEEN, IS [NOT] NULL, AND, OR, NOT and parentheses, as a tree; ValueError else."""

    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
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
        raise ValueError("no operator")

    def operand(self) -> tuple:
        kind, value = self.peek()
        if kind == "num":
            self.i += 1
            return ("num", value if not value.startswith(".") else "0" + value)
        if kind == "punct" and value == "-" and self.peek(1)[0] == "num":
            self.i += 2
            v = self.tokens[self.i - 1][1]
            return ("num", "-" + (v if not v.startswith(".") else "0" + v))
        if kind == "str":
            self.i += 1
            return ("str", value[1:-1].replace("''", "'"))
        if kind == "param":
            self.i += 1
            return ("param", value[1:])
        if kind in ("word", "qid"):
            self.i += 1
            first = value if kind == "word" else value[1:-1].replace('""', '"')
            if self.is_("punct", "."):
                if kind != "word":
                    raise ValueError("qualifier")
                self.i += 1
                k, v = self.peek()
                if k not in ("word", "qid"):
                    raise ValueError("column")
                self.i += 1
                return ("col", first.lower(), v if k == "word" else v[1:-1].replace('""', '"'))
            if self.is_("punct", "("):
                raise ValueError("function")
            return ("col", None, first)
        raise ValueError("operand")


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
    return operand(tree)


def _sql_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


# ---- the plan ----------------------------------------------------------------------------------

@dataclass
class Part:
    """A part of the builder's WHERE on the columns of one table."""
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
    #: Alias → table name (t0 the base table, t1 … the joined ones).
    tables: dict[str, str]
    #: The aliases whose table the SQL names once (the table is read for that alias only).
    own: set[str]
    parts: dict[str, list[Part]]
    keys: list[Key]


def _normal(sql: str) -> str:
    return " ".join((sql or "").split())


def plan_of(builder: Optional[Builder], sql: str, values: Optional[dict[str, Any]] = None) -> Optional[Plan]:
    """What can be done on the server for the builder's query; None when `sql` is not the builder's (written by hand)."""
    if builder is None or not builder.table:
        return None
    try:
        own_sql = build_sql(builder)
    except AnalysisError:
        return None
    if _normal(own_sql) != _normal(sql):
        return None
    names = aliases(builder)
    tables = {a: t for a, t in names}
    used: dict[str, int] = {}
    for w in words(sql):
        used[w.lower()] = used.get(w.lower(), 0) + 1
    own = {a for a, t in names if used.get(t.lower(), 0) == 1}
    left = {f"t{i + 1}" for i, j in enumerate(builder.joins) if j.type == "left"}
    parts: dict[str, list[Part]] = {}
    tokens = _tokens(builder.where or "")
    for piece in _parts(tokens or []):
        try:
            tree = _Parser(piece).parse()
        except ValueError:
            continue
        for p in _flat(tree):
            found = {c[1] for c in _columns(p)}
            if not found:
                continue
            if None in found:
                if builder.joins:
                    # Which table an unqualified column is of is not known before the tables are read.
                    continue
                found = (found - {None}) | {"t0"}
            if len(found) != 1:
                continue
            alias = found.pop()
            if alias not in own or any(_ALIAS.fullmatch(c[1] or "t0") is None for c in _columns(p)):
                continue
            if alias in left and not _null_rejected(p, values or {}):
                continue
            parts.setdefault(alias, []).append(Part(alias, _sql(p, _sqlite_operand) or "", p))
    keys = []
    for i, j in enumerate(builder.joins):
        alias, of = f"t{i + 1}", (j.of or "t0").lower()
        keys.append(Key(of, j.left, alias, j.right))
        if j.type != "left":
            keys.append(Key(alias, j.right, of, j.left))
    return Plan(tables, own, parts, keys)


def _sqlite_operand(o: tuple) -> Optional[str]:
    kind = o[0]
    if kind == "col":
        return (f"{o[1]}." if o[1] else "") + '"' + o[2].replace('"', '""') + '"'
    if kind == "num":
        return o[1]
    if kind == "str":
        return _sql_quote(o[1])
    if kind == "param":
        return ":" + o[1]
    return None


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


# ---- Esri text of a part -----------------------------------------------------------------------

def _kind(field_type: str) -> Optional[str]:
    """"number", "text" or None (dates, GUIDs, shapes …: not compared on the server)."""
    t = (field_type or "").lower()
    if t in _NUMERIC_TYPES:
        return "number"
    if t == "esrifieldtypestring":
        return "text"
    return None


def fields_of(info_fields: Sequence[dict]) -> dict[str, tuple[str, str]]:
    """Lower-case name → (name, type) of a layer's fields."""
    return {f["name"].lower(): (f["name"], str(f.get("type", ""))) for f in info_fields
            if isinstance(f, dict) and isinstance(f.get("name"), str)}


def esri_text(part: Part, fields: dict[str, tuple[str, str]], values: Optional[dict[str, Any]] = None) -> Optional[str]:
    """
    The part as an Esri WHERE (field names as the layer has them, `:name` as
    its value), or None when the server might read it otherwise than SQLite.
    """
    values = values or {}

    def predicate(p: tuple) -> Optional[str]:
        found = set()
        for c in _columns(p):
            f = fields.get(c[2].lower())
            if f is None or not _FIELD.fullmatch(f[0]):
                return None
            found.add(_kind(f[1]))
        if len(found) != 1 or None in found:
            # No column (a value against a value), dates, or a number against a text.
            return None
        kind = found.pop()

        def operand(o: tuple) -> Optional[str]:
            if o[0] == "col":
                return fields[o[2].lower()][0]
            if o[0] == "num":
                return o[1] if kind == "number" else None
            text = o[1] if o[0] == "str" else values.get(o[1])
            if text is None:
                return None
            text = str(text)
            if kind == "text":
                return _sql_quote(text)
            # A number column compares a text that is a number as that number (SQLite's affinity).
            return text if _NUMBER.fullmatch(text) else None

        return _sql(p, operand)

    def walk(t: tuple) -> Optional[str]:
        if t[0] in ("or", "and"):
            items = [walk(x) for x in t[1]]
            return None if None in items else "(" + f" {t[0].upper()} ".join(items) + ")"
        if t[0] == "not":
            inner = walk(t[1])
            return None if inner is None else f"(NOT {inner})"
        return predicate(t)

    return walk(part.tree)


# ---- join keys ---------------------------------------------------------------------------------

def _column_kind(column: Column) -> Optional[str]:
    return "number" if column.type in ("INTEGER", "REAL") else "text" if column.type == "TEXT" else None


def key_values(columns: Sequence[Column], rows: Sequence[Sequence[Any]], name: str, kind: str) -> Optional[list[str]]:
    """
    The values of column `name` as Esri literals of `kind` (each once, NULL
    left out: it joins nothing), or None when one cannot be written so that
    the server matches what SQLite matches (a float in a text column …).
    """
    from .analysis import plain
    at = next((i for i, c in enumerate(columns) if c.name.lower() == name.lower()), None)
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


# ---- reading the tables ------------------------------------------------------------------------

@dataclass
class Read:
    table: Table
    columns: list[Column]
    rows: list[list[Any]]
    #: What was done on the server (Japanese, "" for nothing).
    note: str = ""


def _count(client: Any, table: Table, clauses: Sequence[str], token: Optional[str]) -> Optional[int]:
    from .esri import EsriError
    from .util import is_number
    try:
        n = client.ask(f"{table.url}/query", {"where": " AND ".join(clauses) or "1=1", "returnCountOnly": "true"}, token).get("count")
    except EsriError:
        return None
    return int(n) if is_number(n) else None


def read_tables(client: Any, prepared: Sequence[tuple[Table, Any]], plan: Optional[Plan], conditions: dict[str, str],
                values: Optional[dict[str, Any]], shape: bool, limit: int, token_for: Callable[[Table], Optional[str]],
                cancelled: Optional[Callable[[], bool]] = None, progress: Optional[Callable[[float], None]] = None) -> list[Read]:
    """
    Each table once (a project layer by its reader, an Esri table by
    `read_esri`), with what the plan lets the server do; in the order read.
    """
    from .esri import EsriError, own_condition
    out: list[Read] = []
    read: dict[str, Read] = {}
    steps = len(prepared) + 1

    def step() -> None:
        if progress is not None:
            progress(100 * len(out) / steps)

    for t, reader in prepared:
        if reader is not None:
            step()
            columns, rows = reader.read(cancelled)
            read[t.name] = Read(t, list(columns), list(rows))
            out.append(read[t.name])
    esri = [t for t, reader in prepared if reader is None]
    alias_of = {plan.tables[a]: a for a in plan.own} if plan else {}
    infos: dict[str, Any] = {}
    pushed: dict[str, list[str]] = {}
    for t in esri:
        a = alias_of.get(t.name)
        if plan and a and (plan.parts.get(a) or any(k.target == a for k in plan.keys)):
            try:
                infos[t.name] = client.layer(t.url, token_for(t))
            except EsriError:
                continue
            fields = fields_of(infos[t.name].raw.get("fields") or [])
            texts = [esri_text(p, fields, values) for p in plan.parts.get(a, [])]
            pushed[t.name] = [x for x in texts if x]
    targets = {t.name for t in esri if t.name in infos and any(k.target == alias_of[t.name] for k in plan.keys)} if plan else set()
    counts: dict[str, Optional[int]] = {}
    refused: set[str] = set()
    order = list(esri)
    if targets:
        for t in esri:
            base = [f"({t.where})"] if t.where and t.where.strip() else []
            condition = conditions.get(t.name, "")
            if condition and condition.strip():
                try:
                    base.append(own_condition(condition))
                except EsriError:
                    counts[t.name] = None
                    continue
            mine = [f"({p})" for p in pushed.get(t.name, [])]
            counts[t.name] = _count(client, t, base + mine, token_for(t))
            if counts[t.name] is None and mine:
                counts[t.name] = _count(client, t, base, token_for(t))
                if counts[t.name] is not None:
                    # The server refuses the builder's condition: read as before.
                    pushed[t.name] = []
                    refused.add(t.name)
        place = {t.name: i for i, t in enumerate(esri)}
        order.sort(key=lambda t: (counts.get(t.name) is None, counts.get(t.name) or 0, place[t.name]))
    for t in order:
        step()
        notes: list[str] = []
        key = None
        a = alias_of.get(t.name)
        if t.name in targets:
            info = infos[t.name]
            fields = fields_of(info.raw.get("fields") or [])
            best = None
            for k in plan.keys:
                if k.target != a or plan.tables.get(k.source) not in read:
                    continue
                f = fields.get(k.target_column.lower())
                kind = _kind(f[1]) if f else None
                if kind is None or not _FIELD.fullmatch(f[0]):
                    continue
                source = read[plan.tables[k.source]]
                found = key_values(source.columns, source.rows, k.source_column, kind)
                if found is not None and (best is None or len(found) < len(best[1])):
                    best = (f[0], found, k)
            page = max(1, min(PAGE, info.max_record_count or PAGE))
            if best is not None and worth(len(best[1]), counts.get(t.name), page, limit):
                key = (best[0], best[1])
        mine = pushed.get(t.name, [])
        columns, rows = read_esri(client, t, conditions.get(t.name, ""), shape, limit, token_for(t), cancelled,
                                  pushed=mine, key=key, notes=notes)
        if "fallback" in notes or t.name in refused:
            note = "サーバーで絞り込めず、読み込み条件だけで読み込み"
        else:
            done = (["条件"] if mine else []) + ([f"結合キー {len(key[1]):,} 件"] if key else [])
            note = "サーバーで絞り込み: " + "・".join(done) if done else ""
            if key is not None and not key[1]:
                note = "結合する行がないので読み込みなし"
        read[t.name] = Read(t, columns, rows, note)
        out.append(read[t.name])
    return out
