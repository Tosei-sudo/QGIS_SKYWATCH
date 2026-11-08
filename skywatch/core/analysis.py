"""
「データ分析」 (tsudo, 2026-11-08): the Esri tables of the settings file and
the project's own layers and tables joined and summed up in SQL.

The tables a query names are read (Esri: `query` only, never written; a
project layer: its features) into an in-memory SQLite database of the
Python standard library, and the query runs there: anything SQLite does
(JOIN, GROUP BY, COUNT(DISTINCT …), CASE, window functions, WITH …) without
a new library. Only reading statements run (an authorizer refuses the rest:
no ATTACH, no PRAGMA, no writing).

- The Esri tables are named as the README's 「Esri 側のテーブル定義」
  (TBL_IMAGE_CATALOG, TBL_AI_RESULT …; a second entry of one kind
  TBL_AI_RESULT_2), the project's layers by their layer name made a name.
- Columns are the attribute names as they are; dates are text in the
  computer's time zone (`YYYY-MM-DD HH:MM:SS`, so `date(…)`,
  `strftime(…)` and comparisons with '2026-11-08' work); the shape is the
  column SHAPE (WKT in WGS 84), read only when the query names SHAPE.
- `:name` in the query is a value asked for when it runs (as text; columns
  have the type of their attribute, so `SCORE >= :min` compares numbers).
- 「かんたん集計」 (Builder) writes the SQL of a table, its joins (the key
  pairs suggested from the attributes' roles), a condition, the columns
  grouped by and the figures (件数, 異なる値の数, 合計, 平均, 最小, 最大).

An analysis (its name, SQL, the builder's choices, the values of the
parameters and the conditions each table is read with) is kept in the QGIS
project, not in the settings file (tsudo: shared analyses are not wanted
yet).
"""
from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional, Sequence

#: The most rows read from one table (more is an error: narrow it with its condition).
DEFAULT_TABLE_LIMIT = 100_000
#: The most rows a query returns (the rest are left out, and the result says so).
RESULT_LIMIT = 200_000
#: The column holding the shape (WKT, WGS 84).
SHAPE = "SHAPE"
#: The key in the project where the analyses are kept.
PROJECT_SCOPE, PROJECT_KEY = "skywatch", "analyses"

#: Each kind of Esri table: its SQL name (README 「Esri 側のテーブル定義」) and what it is.
KINDS: dict[str, tuple[str, str]] = {
    "catalog": ("TBL_IMAGE_CATALOG", "画像カタログ"),
    "satellite": ("TBL_SATELLITE", "衛星カタログ"),
    "sensor": ("TBL_SENSOR_MODE", "センサーモード"),
    "alias": ("TBL_SATELLITE_ALIAS", "衛星名の対応表"),
    "plan": ("TBL_IMAGING_PLAN", "撮像計画"),
    "request": ("TBL_IMAGING_REQUEST", "撮像依頼台帳"),
    "requestHistory": ("TBL_REQUEST_HISTORY", "撮像依頼の出力履歴"),
    "observation": ("TBL_OBSERVATION", "観測記録"),
    "area": ("TBL_OBSERVATION_AREA", "確認範囲"),
    "taxonomy": ("TBL_TAXONOMY", "分類体系"),
    "alertRule": ("TBL_ALERT_RULE", "通報の条件"),
    "geofence": ("TBL_ALERT_GEOFENCE", "ジオフェンス"),
    "namedArea": ("TBL_NAMED_AREA", "地名のポリゴン"),
    "alertLog": ("TBL_ALERT_LOG", "通報記録"),
    "assignment": ("TBL_ASSIGNMENT", "分担"),
    "handover": ("TBL_HANDOVER_MEMO", "引き継ぎメモ"),
    "event": ("TBL_EVENT", "イベント"),
    "eventLink": ("TBL_EVENT_LINK", "イベントの紐付け"),
    "aiResult": ("TBL_AI_RESULT", "外部 AI の結果"),
    "detection": ("TBL_DETECTION", "検出結果の転記先"),
}

#: Keys that join two kinds of table: (kind, role) = (kind, role), as the plugin writes them.
RELATIONS: list[tuple[tuple[str, str], tuple[str, str]]] = [
    (("aiResult", "imageId"), ("catalog", "id")),
    (("observation", "imageId"), ("catalog", "id")),
    (("assignment", "imageId"), ("catalog", "id")),
    (("requestHistory", "imageId"), ("catalog", "id")),
    (("aiResult", "imageId"), ("observation", "imageId")),
    (("aiResult", "imageId"), ("assignment", "imageId")),
    (("observation", "imageId"), ("assignment", "imageId")),
    (("alertLog", "obsId"), ("observation", "id")),
    (("eventLink", "event"), ("event", "id")),
    (("eventLink", "item"), ("catalog", "id")),
    (("eventLink", "item"), ("observation", "id")),
    (("eventLink", "item"), ("request", "id")),
    (("requestHistory", "requestId"), ("request", "id")),
    (("plan", "satellite"), ("satellite", "name")),
]

#: The figures of 「かんたん集計」: key → (label, SQL with {c} for the column).
AGGREGATES: dict[str, tuple[str, str]] = {
    "count": ("件数", "COUNT(*)"),
    "countDistinct": ("異なる値の数", "COUNT(DISTINCT {c})"),
    "sum": ("合計", "SUM({c})"),
    "avg": ("平均", "AVG({c})"),
    "min": ("最小", "MIN({c})"),
    "max": ("最大", "MAX({c})"),
}
JOIN_TYPES = {"inner": ("ある行だけ（内部結合）", "JOIN"), "left": ("なくても残す（左外部結合）", "LEFT JOIN")}

_ID_COLUMNS = {"objectid", "fid", "gid", "oid", "globalid", "shape", "shape_length", "shape_area", "shape__length", "shape__area"}


class AnalysisError(Exception):
    """A query that cannot run, or a table that cannot be read (the reason in Japanese)."""


# ---- tables -------------------------------------------------------------------------------------

@dataclass
class Column:
    name: str
    #: SQLite type: INTEGER, REAL or TEXT (dates are TEXT).
    type: str = "TEXT"
    #: The attribute's role in the plugin (imageId, kind …), when it has one.
    role: str = ""
    alias: str = ""


@dataclass
class Table:
    """A table a query can name."""
    name: str
    title: str
    #: "esri" (a table of the settings file) or "layer" (a layer or table of the project).
    source: str
    kind: str = ""
    url: str = ""
    token: Optional[str] = None
    #: The settings file's own condition (always added to the one of the analysis).
    where: Optional[str] = None
    #: Attribute names by role.
    roles: dict[str, str] = field(default_factory=dict)
    #: The project layer's id.
    layer_id: str = ""
    #: Whether it has shapes (None: not known until read).
    spatial: Optional[bool] = None
    #: Its columns, once known (read, or from the layer).
    columns: list[Column] = field(default_factory=list)

    def role_of(self, column: str) -> str:
        for role, name in self.roles.items():
            if isinstance(name, str) and name.lower() == column.lower():
                return role
        return ""


def _web(url: Any) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _roles(fields: Any) -> dict[str, str]:
    if not isinstance(fields, dict):
        return {}
    return {k: v for k, v in fields.items() if isinstance(v, str) and v}


def esri_tables(config: Any) -> list[Table]:
    """Every Esri table and layer of the settings file, in the order of the README, by their SQL names."""
    found: list[tuple[str, str, Any, dict[str, str]]] = []

    def add(kind: str, label: str, entry: Any, fields: Any = None) -> None:
        if entry is None or not _web(getattr(entry, "url", None)):
            return
        found.append((kind, label or "", entry, _roles(fields if fields is not None else getattr(entry, "fields", None))))

    c = config
    for x in getattr(c, "image_catalogs", []) or []:
        add("catalog", x.label, x)
    for x in getattr(c, "satellite_catalogs", []) or []:
        add("satellite", x.label, x)
    for x in getattr(c, "sensor_catalogs", []) or []:
        add("sensor", x.label, x)
    for x in getattr(c, "satellite_aliases", []) or []:
        add("alias", x.label, x)
    for x in getattr(c, "plan_stores", []) or []:
        add("plan", x.label, x)
    for x in getattr(c, "request_stores", []) or []:
        add("request", x.label, x)
        if _web(x.history):
            found.append(("requestHistory", x.label, _Url(x.history, x.token), _roles(x.history_fields)))
    for x in getattr(c, "observation_stores", []) or []:
        add("observation", x.label, x)
        add("area", x.label, x.areas)
        add("taxonomy", x.label, x.taxonomy_table)
    alerts = getattr(c, "alert_reports", None)
    if alerts is not None:
        add("alertRule", "", alerts.rules)
        add("geofence", "", alerts.geofences)
        add("namedArea", "", alerts.named_areas)
        add("alertLog", "", alerts.table)
    add("assignment", "", getattr(c, "assignments", None))
    handover = getattr(c, "handover", None)
    add("handover", "", getattr(handover, "table", None))
    events = getattr(c, "events", None)
    if events is not None:
        add("event", "", events.layer)
        add("eventLink", "", events.links)
    for x in getattr(c, "ai_result_sources", []) or []:
        if not getattr(x, "local", False):
            add("aiResult", x.label, x)
    for x in getattr(c, "detection_outputs", []) or []:
        add("detection", x.label, x)

    out: list[Table] = []
    taken: set[str] = set()
    for kind, label, entry, roles in found:
        base, what = KINDS[kind]
        name = unique_name(base, taken)
        title = f"{what}（{label}）" if label and label != what else what
        out.append(Table(name=name, title=title, source="esri", kind=kind, url=entry.url, token=getattr(entry, "token", None),
                         where=getattr(entry, "where", None) or None, roles=roles))
    return out


@dataclass
class _Url:
    url: str
    token: Optional[str] = None


def unique_name(base: str, taken: set[str]) -> str:
    """`base`, or `base_2`, `base_3` … the first not taken (case does not matter); the name is added to `taken`."""
    name, n = base, 1
    while name.lower() in taken:
        n += 1
        name = f"{base}_{n}"
    taken.add(name.lower())
    return name


def table_name(text: str) -> str:
    """A layer's name made a SQL name: letters (any script), digits and _ (a digit first: _ before it)."""
    name = re.sub(r"\W+", "_", text or "").strip("_") or "LAYER"
    return "_" + name if name[0].isdigit() else name


def esri_type(field_type: str) -> str:
    t = (field_type or "").lower()
    if t in ("esrifieldtypeoid", "esrifieldtypeinteger", "esrifieldtypesmallinteger", "esrifieldtypebiginteger"):
        return "INTEGER"
    if t in ("esrifieldtypedouble", "esrifieldtypesingle"):
        return "REAL"
    return "TEXT"


# ---- reading the SQL ----------------------------------------------------------------------------

_STRING_OR_COMMENT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?(?:\*/|$)", re.DOTALL)
_WORD = re.compile(r'"((?:[^"]|"")+)"|\[([^\]]+)\]|`([^`]+)`|([^\W\d]\w*)')
_PARAMETER = re.compile(r"(?<![\w:]):([^\W\d]\w*)")


def _code(sql: str) -> str:
    """The SQL without its string literals and comments (each left as a space)."""
    return _STRING_OR_COMMENT.sub(" ", sql or "")


def words(sql: str) -> list[str]:
    """The names in the SQL (quoted ones unquoted), outside strings and comments."""
    out = []
    for m in _WORD.finditer(_code(sql)):
        q = m.group(1)
        out.append(q.replace('""', '"') if q is not None else (m.group(2) or m.group(3) or m.group(4)))
    return out


def referenced(sql: str, names: Iterable[str]) -> list[str]:
    """The tables of `names` the SQL names (in the order of `names`; case does not matter)."""
    used = {w.lower() for w in words(sql)}
    return [n for n in names if n.lower() in used]


def wants_shape(sql: str) -> bool:
    return any(w.upper() == SHAPE for w in words(sql))


def parameters(sql: str) -> list[str]:
    """The `:name` values the SQL asks for, in order, once each."""
    return list(dict.fromkeys(m.group(1) for m in _PARAMETER.finditer(_code(sql))))


def quote_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_text(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# ---- values -------------------------------------------------------------------------------------

def local_time(ms: Any) -> Optional[str]:
    """An Esri date (ms since 1970, UTC) as text in the computer's time zone; None for none."""
    if ms is None or isinstance(ms, bool):
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ms) / 1000))
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ms)


def plain(value: Any) -> Any:
    """A value as SQLite keeps it: None, int, float or text (a date as text, a boolean as 0/1)."""
    if value is None or isinstance(value, (int, float, str)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value)


def _ring_area(ring: Sequence[Sequence[float]]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:])) / 2


def _coords(points: Sequence[Sequence[float]]) -> str:
    return ", ".join(f"{p[0]!r} {p[1]!r}" for p in points)


def esri_wkt(geometry: Any) -> Optional[str]:
    """An Esri JSON geometry as WKT (points, multipoints, lines, polygons: Esri's clockwise rings outer, the others holes)."""
    if not isinstance(geometry, dict):
        return None
    try:
        if "x" in geometry and "y" in geometry:
            if geometry["x"] is None or geometry["y"] is None or (isinstance(geometry["x"], float) and math.isnan(geometry["x"])):
                return None
            return f"POINT ({float(geometry['x'])!r} {float(geometry['y'])!r})"
        if geometry.get("points"):
            return "MULTIPOINT (" + ", ".join(f"({float(p[0])!r} {float(p[1])!r})" for p in geometry["points"]) + ")"
        if geometry.get("paths"):
            paths = [p for p in geometry["paths"] if len(p) >= 2]
            if len(paths) == 1:
                return f"LINESTRING ({_coords(paths[0])})"
            return "MULTILINESTRING (" + ", ".join(f"({_coords(p)})" for p in paths) + ")" if paths else None
        if geometry.get("rings"):
            polygons: list[list] = []
            for ring in geometry["rings"]:
                if len(ring) < 4:
                    continue
                # Esri: outer rings clockwise (negative area here), holes counter-clockwise.
                if _ring_area(ring) <= 0 or not polygons:
                    polygons.append([ring])
                else:
                    polygons[-1].append(ring)
            if not polygons:
                return None
            parts = ["(" + ", ".join(f"({_coords(r)})" for r in rings) + ")" for rings in polygons]
            return f"POLYGON {parts[0]}" if len(parts) == 1 else "MULTIPOLYGON (" + ", ".join(parts) + ")"
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return None


# ---- the database -------------------------------------------------------------------------------

@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    #: Rows left out past RESULT_LIMIT.
    cut: bool = False
    seconds: float = 0.0


_ALLOWED = {getattr(sqlite3, n) for n in ("SQLITE_SELECT", "SQLITE_READ", "SQLITE_FUNCTION", "SQLITE_RECURSIVE") if hasattr(sqlite3, n)}


def _reading_only(action: int, *_args) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


class Database:
    """The tables read, in memory, and the query run on them."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.tables: dict[str, list[Column]] = {}

    def close(self) -> None:
        self.conn.close()

    def add(self, name: str, columns: Sequence[Column], rows: Iterable[Sequence[Any]]) -> int:
        """Makes table `name` of `columns` with `rows` (values in the columns' order); how many rows."""
        if not columns:
            raise AnalysisError(f"{name} に列がない")
        seen: set[str] = set()
        for c in columns:
            if c.name.lower() in seen:
                raise AnalysisError(f"{name} に同じ名前の列が 2 つ（{c.name}）")
            seen.add(c.name.lower())
        spec = ", ".join(f"{quote_name(c.name)} {c.type}" for c in columns)
        self.conn.execute(f"DROP TABLE IF EXISTS {quote_name(name)}")
        self.conn.execute(f"CREATE TABLE {quote_name(name)} ({spec})")
        marks = ", ".join("?" for _ in columns)
        cur = self.conn.executemany(f"INSERT INTO {quote_name(name)} VALUES ({marks})", (tuple(plain(v) for v in r) for r in rows))
        self.tables[name] = list(columns)
        return cur.rowcount if cur.rowcount >= 0 else 0

    def run(self, sql: str, values: Optional[dict[str, Any]] = None, cancelled: Optional[Callable[[], bool]] = None,
            limit: int = RESULT_LIMIT) -> Result:
        """Runs one reading statement (anything else is refused); its columns and rows."""
        text = (sql or "").strip().rstrip(";").strip()
        if not text:
            raise AnalysisError("SQL が空")
        names = parameters(text)
        given = {n: (values or {}).get(n) for n in names}
        missing = [n for n, v in given.items() if v is None]
        if missing:
            raise AnalysisError("値が未入力: " + "、".join(":" + n for n in missing))
        start = time.monotonic()
        self.conn.set_authorizer(_reading_only)
        if cancelled is not None:
            self.conn.set_progress_handler(lambda: 1 if cancelled() else 0, 10_000)
        try:
            cur = self.conn.execute(text, given)
            if cur.description is None:
                raise AnalysisError("結果を返す SELECT 文（または WITH …）で書く")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit + 1)
        except sqlite3.Warning as error:
            raise AnalysisError(_sql_message(error)) from None
        except sqlite3.DatabaseError as error:
            if cancelled is not None and cancelled():
                raise AnalysisError("取消") from None
            raise AnalysisError(_sql_message(error)) from None
        finally:
            self.conn.set_authorizer(None)
            self.conn.set_progress_handler(None, 0)
        return Result(columns, rows[:limit], len(rows) > limit, time.monotonic() - start)


def _sql_message(error: Exception) -> str:
    text = str(error)
    if "not authorized" in text:
        return "読み取り（SELECT）以外の文は実行できない"
    if "one statement at a time" in text:
        return "一度に実行できる文は 1 つ"
    if text.startswith("no such table:"):
        return "表がない: " + text.split(":", 1)[1].strip() + "（左の一覧の名前で書く）"
    if text.startswith("no such column:"):
        return "列がない: " + text.split(":", 1)[1].strip()
    return "SQL のエラー: " + text


# ---- reading an Esri table ----------------------------------------------------------------------

def read_esri(client: Any, table: Table, where: str = "", shape: bool = False, limit: int = DEFAULT_TABLE_LIMIT,
              token: Optional[str] = None, cancelled: Optional[Callable[[], bool]] = None) -> tuple[list[Column], list[list[Any]]]:
    """The columns and rows of an Esri table (the settings file's condition and `where` both apply)."""
    from .esri import EsriError, TooManyError
    try:
        info = client.layer(table.url, token)
        fields = [f for f in info.raw.get("fields") or [] if isinstance(f, dict) and isinstance(f.get("name"), str)]
        geometry = bool(info.geometry_type) and shape
        clauses = [f"({w})" for w in (table.where, where) if w and w.strip()]
        features = client.query_all(table.url, " AND ".join(clauses) or "1=1", token, geometry=geometry, limit=limit,
                                    cancelled=cancelled)
    except TooManyError as error:
        raise AnalysisError(f"{table.name}: {error}") from None
    except EsriError as error:
        raise AnalysisError(f"{table.name} の読み込みに失敗: {error}") from None
    table.spatial = bool(info.geometry_type)
    columns = [Column(f["name"], esri_type(f.get("type", "")), table.role_of(f["name"]), str(f.get("alias") or ""))
               for f in fields if str(f.get("type", "")).lower() not in ("esrifieldtypegeometry", "esrifieldtypeblob", "esrifieldtyperaster")]
    if geometry and not any(c.name.upper() == SHAPE for c in columns):
        columns.append(Column(SHAPE, "TEXT", "", "形状（WKT）"))
    dates = {f["name"] for f in fields if str(f.get("type", "")).lower() == "esrifieldtypedate"}
    rows = []
    for f in features:
        a = f.get("attributes") or {}
        row = []
        for c in columns:
            if c.name == SHAPE and geometry and c.name not in a:
                row.append(esri_wkt(f.get("geometry")))
            elif c.name in dates:
                row.append(local_time(a.get(c.name)))
            else:
                row.append(a.get(c.name))
        rows.append(row)
    table.columns = columns
    return columns, rows


# ---- join keys ----------------------------------------------------------------------------------

def suggest_join(left: Table, right: Table) -> Optional[tuple[str, str]]:
    """The columns that join `left` to `right`: a relation of their roles, else the same attribute name (not ids)."""
    for (ka, ra), (kb, rb) in RELATIONS:
        for (k1, r1), (k2, r2) in (((ka, ra), (kb, rb)), ((kb, rb), (ka, ra))):
            if left.kind == k1 and right.kind == k2 and left.roles.get(r1) and right.roles.get(r2):
                return left.roles[r1], right.roles[r2]
    lefts = {c.name.lower(): c.name for c in left.columns}
    for c in right.columns:
        if c.name.lower() in lefts and c.name.lower() not in _ID_COLUMNS:
            return lefts[c.name.lower()], c.name
    for role, name in left.roles.items():
        if role in right.roles and role not in ("note", "id"):
            return name, right.roles[role]
    return None


# ---- 「かんたん集計」 -----------------------------------------------------------------------------

@dataclass
class Join:
    table: str
    left: str
    right: str
    #: "inner" or "left".
    type: str = "inner"
    #: The table (alias) the left column is of; "" for the base table.
    of: str = ""


@dataclass
class Figure:
    #: A key of AGGREGATES.
    function: str = "count"
    #: "alias.column" ("" for 件数).
    column: str = ""
    name: str = ""


@dataclass
class Builder:
    table: str = ""
    joins: list[Join] = field(default_factory=list)
    where: str = ""
    #: "alias.column" grouped by.
    group: list[str] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    #: Largest first by the first figure.
    descending: bool = True
    limit: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "table": self.table, "where": self.where, "group": list(self.group), "descending": self.descending, "limit": self.limit,
            "joins": [{"table": j.table, "left": j.left, "right": j.right, "type": j.type, "of": j.of} for j in self.joins],
            "figures": [{"function": f.function, "column": f.column, "name": f.name} for f in self.figures],
        }

    @staticmethod
    def from_json(value: Any) -> "Builder":
        if not isinstance(value, dict):
            return Builder()
        joins = [Join(str(j.get("table") or ""), str(j.get("left") or ""), str(j.get("right") or ""),
                      "left" if j.get("type") == "left" else "inner", str(j.get("of") or ""))
                 for j in value.get("joins") or [] if isinstance(j, dict)]
        figures = [Figure(f.get("function") if f.get("function") in AGGREGATES else "count", str(f.get("column") or ""), str(f.get("name") or ""))
                   for f in value.get("figures") or [] if isinstance(f, dict)]
        limit = value.get("limit")
        return Builder(str(value.get("table") or ""), joins, str(value.get("where") or ""),
                       [str(g) for g in value.get("group") or [] if isinstance(g, str)], figures,
                       bool(value.get("descending", True)), int(limit) if isinstance(limit, int) and limit > 0 else 0)


def aliases(builder: Builder) -> list[tuple[str, str]]:
    """(alias, table) of the base table (t0) and each join (t1, t2 …)."""
    return [("t0", builder.table)] + [(f"t{i + 1}", j.table) for i, j in enumerate(builder.joins)]


def _column(ref: str) -> str:
    alias, _, name = ref.partition(".")
    return f"{alias}.{quote_name(name)}" if name else quote_name(alias)


def figure_name(f: Figure) -> str:
    if f.name:
        return f.name
    label = AGGREGATES[f.function][0]
    return label if f.function == "count" or not f.column else f"{f.column.partition('.')[2] or f.column}の{label}"


def build_sql(builder: Builder) -> str:
    """The SQL of 「かんたん集計」."""
    if not builder.table:
        raise AnalysisError("基にする表を選ぶ")
    lines = []
    group = [g for g in builder.group if g]
    figures = list(builder.figures)
    picked, names = [], []
    for g in group:
        name = g.partition(".")[2] or g
        if name in names:
            # The same attribute of two tables (t0.ID, t1.ID): the second by its table.
            name = g.replace(".", "_")
        names.append(name)
        picked.append(f"{_column(g)} AS {quote_name(name)}")
    for f in figures:
        if f.function != "count" and not f.column:
            raise AnalysisError(f"「{AGGREGATES[f.function][0]}」の列を選ぶ")
        name = figure_name(f)
        while name in names:
            name += "_"
        names.append(name)
        picked.append(AGGREGATES[f.function][1].format(c=_column(f.column)) + f" AS {quote_name(name)}")
    if not picked:
        picked = ["t0.*"]
    lines.append("SELECT " + ",\n       ".join(picked))
    lines.append(f"FROM {quote_name(builder.table)} AS t0")
    for i, j in enumerate(builder.joins):
        if not (j.table and j.left and j.right):
            raise AnalysisError(f"結合 {i + 1} の表と列を選ぶ")
        alias = f"t{i + 1}"
        left = j.of or "t0"
        lines.append(f"{JOIN_TYPES[j.type][1]} {quote_name(j.table)} AS {alias} ON {left}.{quote_name(j.left)} = {alias}.{quote_name(j.right)}")
    if builder.where.strip():
        lines.append(f"WHERE {builder.where.strip()}")
    if group and figures:
        lines.append("GROUP BY " + ", ".join(_column(g) for g in group))
    if figures:
        lines.append(f"ORDER BY {quote_name(names[len(group)])} {'DESC' if builder.descending else 'ASC'}")
    elif group:
        lines.append("ORDER BY " + ", ".join(_column(g) for g in group))
    if builder.limit:
        lines.append(f"LIMIT {int(builder.limit)}")
    return "\n".join(lines)


# ---- an analysis, as kept in the project --------------------------------------------------------

@dataclass
class Analysis:
    name: str
    sql: str = ""
    builder: Builder = field(default_factory=Builder)
    #: The values of `:name` last given.
    values: dict[str, str] = field(default_factory=dict)
    #: The condition each table is read with (Esri: its `where`; a project layer: a QGIS expression).
    conditions: dict[str, str] = field(default_factory=dict)
    #: "builder" or "sql": the page shown.
    mode: str = "builder"

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "sql": self.sql, "builder": self.builder.to_json(), "values": dict(self.values),
                "conditions": {k: v for k, v in self.conditions.items() if v}, "mode": self.mode}

    @staticmethod
    def from_json(value: Any) -> Optional["Analysis"]:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip():
            return None

        def texts(v: Any) -> dict[str, str]:
            return {str(k): str(x) for k, x in v.items() if x is not None} if isinstance(v, dict) else {}

        return Analysis(value["name"].strip(), str(value.get("sql") or ""), Builder.from_json(value.get("builder")),
                        texts(value.get("values")), texts(value.get("conditions")), "sql" if value.get("mode") == "sql" else "builder")


def analyses_of(text: str) -> list[Analysis]:
    """The analyses kept in the project (JSON); none for anything else."""
    import json
    try:
        value = json.loads(text) if text else []
    except ValueError:
        return []
    out = [a for a in (Analysis.from_json(v) for v in value) if a] if isinstance(value, list) else []
    return list({a.name: a for a in out}.values())


def analyses_text(analyses: Sequence[Analysis]) -> str:
    import json
    return json.dumps([a.to_json() for a in analyses], ensure_ascii=False)


def examples(tables: Sequence[Table]) -> list[Analysis]:
    """Ready-made analyses for the tables of the settings file (offered as 「例」, not kept until saved)."""
    out: list[Analysis] = []
    by_kind: dict[str, list[Table]] = {}
    for t in tables:
        if t.source == "esri":
            by_kind.setdefault(t.kind, []).append(t)
    catalog = (by_kind.get("catalog") or [None])[0]
    for t in by_kind.get("aiResult", []):
        image, cls, model = t.roles.get("imageId"), t.roles.get("class"), t.roles.get("model")
        if not image:
            continue
        group = [f"t0.{image}"] + ([f"t0.{cls}"] if cls else [])
        joins = []
        if catalog is not None and catalog.roles.get("id"):
            joins.append(Join(catalog.name, image, catalog.roles["id"]))
            if catalog.roles.get("acquired"):
                group.insert(1, f"t1.{catalog.roles['acquired']}")
        where = f"t0.{quote_name(model)} = :model" if model else ""
        b = Builder(t.name, joins, where, group, [Figure("count")])
        title = f"{t.title}: " + ("モデルを指定して" if model else "") + "画像・クラスごとの件数"
        out.append(Analysis(title, build_sql(b), b))
        if model:
            b2 = Builder(t.name, [], "", [f"t0.{model}"] + ([f"t0.{cls}"] if cls else []),
                         [Figure("count"), Figure("countDistinct", f"t0.{image}", "画像の数")])
            out.append(Analysis(f"{t.title}: モデル・クラスごとの件数と画像の数", build_sql(b2), b2))
    for t in by_kind.get("observation", [])[:1]:
        kind, image = t.roles.get("kind"), t.roles.get("imageId")
        if kind:
            figures = [Figure("count")] + ([Figure("countDistinct", f"t0.{image}", "画像の数")] if image else [])
            b = Builder(t.name, [], "", [f"t0.{kind}"], figures)
            out.append(Analysis(f"{t.title}: 種別ごとの件数", build_sql(b), b))
    events, links = by_kind.get("event"), by_kind.get("eventLink")
    if events and links:
        e, x = events[0], links[0]
        if e.roles.get("id") and x.roles.get("event") and e.roles.get("name") and x.roles.get("type"):
            b = Builder(e.name, [Join(x.name, e.roles["id"], x.roles["event"], "left")], "",
                        [f"t0.{e.roles['name']}", f"t1.{x.roles['type']}"], [Figure("countDistinct", f"t1.{x.roles.get('item') or x.roles['event']}", "項目の数")])
            out.append(Analysis("イベント: 種類ごとの項目の数", build_sql(b), b))
    return out


# ---- writing the result -------------------------------------------------------------------------

def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value) if not value.is_integer() or abs(value) >= 1e16 else str(int(value))
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _safe(text: str) -> str:
    """A cell a spreadsheet would read as a formula, with ' in front (CSV injection)."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(text) else text


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def csv_text(result: Result, delimiter: str = ",") -> str:
    out = io.StringIO()
    w = csv.writer(out, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(result.columns)
    for r in result.rows:
        w.writerow([_safe(cell_text(v)) for v in r])
    return out.getvalue()


def tsv_text(result: Result) -> str:
    lines = ["\t".join(result.columns)]
    for r in result.rows:
        lines.append("\t".join(cell_text(v).replace("\t", " ").replace("\r", " ").replace("\n", " ") for v in r))
    return "\n".join(lines) + "\n"


def column_type(values: Iterable[Any]) -> str:
    """INTEGER, REAL or TEXT: what a column of a result holds (None left out)."""
    kind = ""
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "TEXT"
        kind = "REAL" if isinstance(v, float) or kind == "REAL" else "INTEGER"
    return kind or "TEXT"
