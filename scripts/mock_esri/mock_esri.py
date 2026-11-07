"""
A stand-in for ArcGIS feature services, for working on the plugin (and the
viewer) without an Esri server. Python's standard library only, the data in
SQLite (kept between runs).

    python3 scripts/mock_esri/mock_esri.py                  # http://127.0.0.1:8765, sample data on first run
    python3 scripts/mock_esri/mock_esri.py --port 9000 --token secret
    python3 scripts/mock_esri/mock_esri.py --reset          # back to the sample data
    python3 scripts/mock_esri/mock_esri.py new-image        # registers a new catalog image now (新着通知の確認)

Each start writes <data>/config.mock.jsonc: a settings file whose services
point at this server. Choose it in the plugin's settings dialog (設定…).

What it answers (as ArcGIS Server 11 does, for what the plugin and QGIS ask):

    /arcgis/rest/info, /arcgis/rest/services
    /arcgis/rest/services/<name>/FeatureServer              layers and tables
    /arcgis/rest/services/<name>/FeatureServer/<id>         layer information
        /query           where (SQL, run by SQLite), objectIds, geometry (box),
                         outFields, orderByFields, resultOffset / resultRecordCount,
                         returnIdsOnly, returnCountOnly, returnExtentOnly,
                         returnDistinctValues, outSR / inSR (4326, 3857)
        /applyEdits      adds, updates, deletes (rollbackOnFailure)
        /addFeatures, /updateFeatures, /deleteFeatures
    /arcgis/rest/services/<name>/FeatureServer/applyEdits   edits of several layers
    /arcgis/tokens/generateToken, /arcgis/sharing/rest/generateToken
    /files/<path>        the files under <data>/files (HTTP ranges, for /vsicurl/)

Not a full server: geometry filters compare bounding boxes, there are no
attachments, statistics, versions or relationships. The layers are defined
in JSON (see sample.py and README.md); more can be added with --services.
"""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "data")
VERSION = 11.1

#: Short names of field types (in service definitions).
TYPES = {
    "string": "esriFieldTypeString", "text": "esriFieldTypeString",
    "integer": "esriFieldTypeInteger", "int": "esriFieldTypeInteger",
    "smallinteger": "esriFieldTypeSmallInteger", "biginteger": "esriFieldTypeBigInteger",
    "double": "esriFieldTypeDouble", "single": "esriFieldTypeSingle", "float": "esriFieldTypeDouble",
    "date": "esriFieldTypeDate", "guid": "esriFieldTypeGUID", "globalid": "esriFieldTypeGlobalID",
}
INTEGER_TYPES = {"esriFieldTypeInteger", "esriFieldTypeSmallInteger", "esriFieldTypeBigInteger"}
REAL_TYPES = {"esriFieldTypeDouble", "esriFieldTypeSingle"}
GEOMETRY_TYPES = {"point": "esriGeometryPoint", "multipoint": "esriGeometryMultipoint", "polyline": "esriGeometryPolyline",
                  "polygon": "esriGeometryPolygon", "envelope": "esriGeometryEnvelope"}
MERCATOR = {102100, 3857, 900913, 102113}
R = 6378137.0


class ServiceError(Exception):
    """Answered as an Esri error (`{"error": {...}}`)."""

    def __init__(self, message: str, code: int = 400, details: Iterable[str] = ()):
        super().__init__(message)
        self.code = code
        self.details = list(details)


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_of_text(text: str) -> Optional[int]:
    """An ISO 8601 date / time (UTC when it has no zone) in ms, or None."""
    t = text.strip().replace(" ", "T")
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


# --- geometry ---------------------------------------------------------------------------------------------


def _to_mercator(x: float, y: float) -> list[float]:
    y = max(-85.06, min(85.06, y))
    return [x * math.pi / 180 * R, math.log(math.tan(math.pi / 4 + y * math.pi / 360)) * R]


def _from_mercator(x: float, y: float) -> list[float]:
    return [x / R * 180 / math.pi, (2 * math.atan(math.exp(y / R)) - math.pi / 2) * 180 / math.pi]


def wkid_of(sr: Any) -> int:
    if isinstance(sr, dict):
        sr = sr.get("latestWkid") or sr.get("wkid")
    try:
        return int(sr)
    except (TypeError, ValueError):
        return 4326


def reproject(geometry: Optional[dict], source: int, target: int) -> Optional[dict]:
    """A geometry between WGS 84 and Web Mercator (others are left as they are)."""
    if not geometry or source == target or not ({source, target} & MERCATOR and {source, target} & {4326}):
        return geometry
    move = _to_mercator if target in MERCATOR else _from_mercator

    def points(ps):
        return [[*move(p[0], p[1]), *p[2:]] for p in ps]

    g = {k: v for k, v in geometry.items() if k != "spatialReference"}
    if "x" in g and g["x"] is not None:
        g["x"], g["y"] = move(g["x"], g["y"])
    if "points" in g:
        g["points"] = points(g["points"])
    if "paths" in g:
        g["paths"] = [points(p) for p in g["paths"]]
    if "rings" in g:
        g["rings"] = [points(p) for p in g["rings"]]
    if "xmin" in g:
        (g["xmin"], g["ymin"]), (g["xmax"], g["ymax"]) = move(g["xmin"], g["ymin"]), move(g["xmax"], g["ymax"])
    g["spatialReference"] = {"wkid": target, "latestWkid": 3857 if target in MERCATOR else target}
    return g


def bbox_of(g: Optional[dict]) -> Optional[tuple[float, float, float, float]]:
    """The bounding box of an Esri point, multipoint, polyline, polygon or envelope."""
    if not g:
        return None
    if "xmin" in g:
        return g["xmin"], g["ymin"], g["xmax"], g["ymax"]
    if g.get("x") is not None:
        return g["x"], g["y"], g["x"], g["y"]
    pts = g.get("points") or [p for part in (g.get("paths") or g.get("rings") or []) for p in part]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def filter_geometry(text: str, kind: Optional[str]) -> dict:
    """The `geometry` of a query: JSON, `x,y` or `xmin,ymin,xmax,ymax`."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            raise ServiceError("geometry が JSON 以外") from None
    nums = [float(v) for v in text.split(",") if v.strip()]
    if len(nums) == 2:
        return {"x": nums[0], "y": nums[1]}
    if len(nums) == 4:
        return {"xmin": nums[0], "ymin": nums[1], "xmax": nums[2], "ymax": nums[3]}
    raise ServiceError("geometry が読めない")


# --- where clauses ----------------------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<string>[Nn]?'(?:[^']|'')*')
  | (?P<number>\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+)
  | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<quoted>"[^"]+")
  | (?P<op><>|!=|>=|<=|\|\||[=<>(),+\-*/%])
""", re.VERBOSE)
_KEYWORDS = {"AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN", "ESCAPE", "TRUE", "FALSE", "CASE", "WHEN", "THEN",
             "ELSE", "END", "CAST", "AS", "INTEGER", "FLOAT", "REAL", "VARCHAR", "CHAR"}
_FUNCTIONS = {"UPPER": "UPPER", "LOWER": "LOWER", "TRIM": "TRIM", "ABS": "ABS", "ROUND": "ROUND", "COALESCE": "COALESCE",
              "CHAR_LENGTH": "LENGTH", "LENGTH": "LENGTH", "SUBSTRING": "SUBSTR", "SUBSTR": "SUBSTR", "MOD": "MOD"}


def sql_where(where: Optional[str], columns: dict[str, str]) -> tuple[str, list[Any]]:
    """
    An ArcGIS where clause as SQLite: `TIMESTAMP '…'` / `DATE '…'` and
    CURRENT_TIMESTAMP / CURRENT_DATE as ms, field names checked
    (`columns`: upper-case name → column), string literals bound.
    """
    if not where or not where.strip():
        return "1=1", []
    out: list[str] = []
    args: list[Any] = []
    pos = 0
    tokens: list[tuple[str, str]] = []
    while pos < len(where):
        m = _TOKEN.match(where, pos)
        if not m:
            raise ServiceError("where が読めない", details=[f"'{where[pos:pos + 20]}' 付近"])
        pos = m.end()
        if m.lastgroup != "space":
            tokens.append((m.lastgroup, m.group()))
    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        up = text.upper()
        if kind == "name" and up in ("TIMESTAMP", "DATE") and i + 1 < len(tokens) and tokens[i + 1][0] == "string":
            literal = tokens[i + 1][1].lstrip("Nn")[1:-1].replace("''", "'")
            ms = ms_of_text(literal)
            if ms is None:
                raise ServiceError(f"{up} の日時が読めない（{literal}）")
            out.append(str(ms))
            i += 2
            continue
        if kind == "name" and up == "CURRENT_TIMESTAMP":
            out.append(str(now_ms()))
        elif kind == "name" and up == "CURRENT_DATE":
            out.append(str(now_ms() // 86_400_000 * 86_400_000))
        elif kind == "name" and up in _KEYWORDS:
            out.append(up)
        elif kind == "name" and up in _FUNCTIONS and i + 1 < len(tokens) and tokens[i + 1][1] == "(":
            out.append(_FUNCTIONS[up])
        elif kind in ("name", "quoted"):
            key = text.strip('"').upper()
            if key not in columns:
                raise ServiceError("where に存在しない属性", details=[text])
            out.append(columns[key])
        elif kind == "string":
            args.append(text.lstrip("Nn")[1:-1].replace("''", "'"))
            out.append("?")
        elif kind == "op" and text == "!=":
            out.append("<>")
        else:
            out.append(text)
        i += 1
    return " ".join(out), args


# --- storage ----------------------------------------------------------------------------------------------


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def field_of(name: str, spec: Any) -> dict:
    """A field definition from `"TYPE"` or `{"type": …, "length": …, "alias": …}`."""
    spec = {"type": spec} if isinstance(spec, str) else dict(spec)
    t = spec.get("type", "string")
    t = TYPES.get(str(t).lower(), t)
    f = {"name": name, "type": t, "alias": spec.get("alias", name), "nullable": spec.get("nullable", True),
         "editable": spec.get("editable", t != "esriFieldTypeOID")}
    if t == "esriFieldTypeString":
        f["length"] = int(spec.get("length", 255))
    if t == "esriFieldTypeDate":
        f["length"] = 8
    if spec.get("domain"):
        f["domain"] = spec["domain"]
    return f


class Layer:
    """One layer or table of a service: its definition and its SQLite table."""

    def __init__(self, service: str, definition: dict):
        self.service = service
        self.id = int(definition.get("id", 0))
        self.name = definition.get("name") or f"{service}_{self.id}"
        gt = definition.get("geometryType")
        self.geometry_type = GEOMETRY_TYPES.get(str(gt).lower(), gt) if gt else None
        self.oid = definition.get("objectIdField", "OBJECTID")
        self.max_record_count = int(definition.get("maxRecordCount", 2000))
        self.pagination = bool(definition.get("supportsPagination", True))
        raw = definition.get("fields") or {}
        items = raw.items() if isinstance(raw, dict) else [(f["name"], f) for f in raw]
        self.fields = [field_of(self.oid, "esriFieldTypeOID")] + [field_of(n, s) for n, s in items if n != self.oid]
        self.types = {f["name"]: f["type"] for f in self.fields}
        self.description = definition.get("description", "")
        self.table = q(f"t_{service}_{self.id}")

    @property
    def is_table(self) -> bool:
        return self.geometry_type is None

    def columns(self) -> dict[str, str]:
        """Upper-case field name → SQL column."""
        return {f["name"].upper(): ("oid" if f["name"] == self.oid else q(f["name"])) for f in self.fields}

    def definition(self) -> dict:
        d = {"id": self.id, "name": self.name, "objectIdField": self.oid, "maxRecordCount": self.max_record_count,
             "supportsPagination": self.pagination, "description": self.description,
             "fields": {f["name"]: {k: v for k, v in f.items() if k not in ("name", "editable", "nullable")} for f in self.fields[1:]}}
        if self.geometry_type:
            d["geometryType"] = self.geometry_type
        return d


class Store:
    """The services and their features in one SQLite file."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS _layers (service TEXT, id INTEGER, definition TEXT, PRIMARY KEY (service, id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
        self.layers: dict[tuple[str, int], Layer] = {}
        self.reload()

    def reload(self) -> None:
        with self.lock:
            self.layers = {}
            for service, _id, text in self.db.execute("SELECT service, id, definition FROM _layers ORDER BY service, id"):
                layer = Layer(service, json.loads(text))
                self.layers[(service.lower(), layer.id)] = layer

    def meta(self, key: str, value: Optional[str] = None) -> Optional[str]:
        with self.lock:
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO _meta VALUES (?, ?)", (key, value))
                return value
            row = self.db.execute("SELECT value FROM _meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def services(self) -> list[str]:
        seen: dict[str, str] = {}
        for layer in self.layers.values():
            seen.setdefault(layer.service.lower(), layer.service)
        return sorted(seen.values())

    def service_layers(self, service: str) -> list[Layer]:
        return sorted((l for (s, _), l in self.layers.items() if s == service.lower()), key=lambda l: l.id)

    def layer(self, service: str, layer_id: int) -> Layer:
        layer = self.layers.get((service.lower(), layer_id))
        if layer is None:
            raise ServiceError("レイヤーなし", code=404 if not self.service_layers(service) else 400,
                               details=[f"{service}/FeatureServer/{layer_id}"])
        return layer

    def define(self, service: str, definition: dict, replace: bool = False) -> Layer:
        """Adds (or with `replace`, recreates) a layer; new fields of an existing one are added as columns."""
        layer = Layer(service, definition)
        with self.lock:
            old = self.layers.get((service.lower(), layer.id))
            if old and replace:
                self.db.execute(f"DROP TABLE IF EXISTS {old.table}")
                old = None
            cols = ", ".join(f"{q(f['name'])} {self._sql_type(f['type'])}" for f in layer.fields[1:])
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {layer.table} (oid INTEGER PRIMARY KEY AUTOINCREMENT"
                            f"{', ' + cols if cols else ''}, _geom TEXT, _xmin REAL, _ymin REAL, _xmax REAL, _ymax REAL)")
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({layer.table})")}
            for f in layer.fields[1:]:
                if f["name"] not in have:
                    self.db.execute(f"ALTER TABLE {layer.table} ADD COLUMN {q(f['name'])} {self._sql_type(f['type'])}")
            self.db.execute("INSERT OR REPLACE INTO _layers VALUES (?, ?, ?)", (service, layer.id, json.dumps(layer.definition(), ensure_ascii=False)))
            self.layers[(service.lower(), layer.id)] = layer
        return layer

    def drop_all(self) -> None:
        with self.lock:
            for layer in self.layers.values():
                self.db.execute(f"DROP TABLE IF EXISTS {layer.table}")
            self.db.execute("DELETE FROM _layers")
            self.db.execute("DELETE FROM _meta")
            self.db.execute("DELETE FROM sqlite_sequence") if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else None
            self.layers = {}

    @staticmethod
    def _sql_type(t: str) -> str:
        return "INTEGER" if t in INTEGER_TYPES or t == "esriFieldTypeDate" else "REAL" if t in REAL_TYPES else "TEXT"

    # --- values -------------------------------------------------------------------------------------

    @staticmethod
    def stored(field: dict, value: Any) -> Any:
        """A value as the column keeps it; raises ValueError when the field cannot take it."""
        if value is None:
            return None
        t = field["type"]
        if t == "esriFieldTypeDate":
            if isinstance(value, bool):
                raise ValueError(f"{field['name']} は日時（ms）で指定")
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str):
                if not value.strip():
                    return None
                ms = ms_of_text(value)
                if ms is not None:
                    return ms
            raise ValueError(f"{field['name']} は日時（ms）で指定（{value!r}）")
        if t in INTEGER_TYPES:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)) and float(value).is_integer():
                return int(value)
            if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
                return int(value)
            if isinstance(value, str) and not value.strip():
                return None
            raise ValueError(f"{field['name']} は整数で指定（{value!r}）")
        if t in REAL_TYPES:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                if not value.strip():
                    return None
                try:
                    return float(value)
                except ValueError:
                    pass
            raise ValueError(f"{field['name']} は数値で指定（{value!r}）")
        if isinstance(value, (dict, list)):
            raise ValueError(f"{field['name']} は文字列で指定")
        text = value if isinstance(value, str) else json.dumps(value) if isinstance(value, bool) else str(value)
        if field.get("length") and len(text) > field["length"]:
            raise ValueError(f"{field['name']} が長すぎる（{len(text)} 文字、上限 {field['length']}）")
        return text

    def _row_values(self, layer: Layer, attributes: dict, warn: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        by_upper = {f["name"].upper(): f for f in layer.fields[1:]}
        for name, value in (attributes or {}).items():
            f = by_upper.get(str(name).upper())
            if f is None:
                if str(name).upper() != layer.oid.upper():
                    warn.append(str(name))
                continue
            out[f["name"]] = self.stored(f, value)
        return out

    @staticmethod
    def _geometry_values(geometry: Optional[dict], in_sr: int) -> dict[str, Any]:
        if not geometry:
            return {"_geom": None, "_xmin": None, "_ymin": None, "_xmax": None, "_ymax": None}
        g = reproject(geometry, wkid_of(geometry.get("spatialReference")) if geometry.get("spatialReference") else in_sr, 4326)
        g = {k: v for k, v in g.items() if k != "spatialReference"}
        box = bbox_of(g)
        return {"_geom": json.dumps(g), "_xmin": box[0] if box else None, "_ymin": box[1] if box else None,
                "_xmax": box[2] if box else None, "_ymax": box[3] if box else None}

    # --- edits --------------------------------------------------------------------------------------

    def add(self, layer: Layer, feature: dict, in_sr: int = 4326, warn: Optional[list[str]] = None) -> int:
        values = self._row_values(layer, feature.get("attributes") or {}, warn if warn is not None else [])
        if not layer.is_table:
            values.update(self._geometry_values(feature.get("geometry"), in_sr))
        cols = list(values)
        with self.lock:
            cur = self.db.execute(f"INSERT INTO {layer.table} ({', '.join(q(c) for c in cols)}) VALUES ({', '.join('?' for _ in cols)})"
                                  if cols else f"INSERT INTO {layer.table} DEFAULT VALUES", [values[c] for c in cols])
            return int(cur.lastrowid)

    def update(self, layer: Layer, feature: dict, in_sr: int = 4326, warn: Optional[list[str]] = None) -> int:
        attributes = feature.get("attributes") or {}
        oid = next((v for k, v in attributes.items() if str(k).upper() == layer.oid.upper()), None)
        if not isinstance(oid, (int, float)) or isinstance(oid, bool):
            raise ValueError(f"{layer.oid} なし")
        values = self._row_values(layer, attributes, warn if warn is not None else [])
        if not layer.is_table and feature.get("geometry"):
            values.update(self._geometry_values(feature["geometry"], in_sr))
        with self.lock:
            if not self.db.execute(f"SELECT 1 FROM {layer.table} WHERE oid = ?", (int(oid),)).fetchone():
                raise LookupError(int(oid))
            if values:
                self.db.execute(f"UPDATE {layer.table} SET {', '.join(q(c) + ' = ?' for c in values)} WHERE oid = ?",
                                [*values.values(), int(oid)])
        return int(oid)

    def delete(self, layer: Layer, oid: int) -> bool:
        with self.lock:
            return self.db.execute(f"DELETE FROM {layer.table} WHERE oid = ?", (int(oid),)).rowcount > 0

    # --- reading ------------------------------------------------------------------------------------

    def select(self, layer: Layer, where: str = "1=1", args: Iterable[Any] = (), order: str = "oid") -> list[sqlite3.Row]:
        with self.lock:
            self.db.row_factory = sqlite3.Row
            try:
                return list(self.db.execute(f"SELECT * FROM {layer.table} WHERE {where} ORDER BY {order}", list(args)))
            except sqlite3.Error as error:
                raise ServiceError("条件で問い合わせできない", details=[str(error)]) from None
            finally:
                self.db.row_factory = None

    def count(self, layer: Layer) -> int:
        with self.lock:
            return self.db.execute(f"SELECT COUNT(*) FROM {layer.table}").fetchone()[0]

    def extent(self, layer: Layer, where: str = "1=1", args: Iterable[Any] = ()) -> Optional[dict]:
        with self.lock:
            row = self.db.execute(f"SELECT MIN(_xmin), MIN(_ymin), MAX(_xmax), MAX(_ymax) FROM {layer.table} WHERE {where}", list(args)).fetchone()
        if row is None or row[0] is None:
            return None
        return {"xmin": row[0], "ymin": row[1], "xmax": row[2], "ymax": row[3], "spatialReference": {"wkid": 4326, "latestWkid": 4326}}


# --- the service ------------------------------------------------------------------------------------------


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


class Service:
    """Answers ArcGIS REST requests from a Store."""

    def __init__(self, store: Store, token: Optional[str] = None, log=print):
        self.store = store
        self.token = token
        self.issued: set[str] = set()
        self.log = log

    # --- information ------------------------------------------------------------------------------------

    def layer_info(self, layer: Layer) -> dict:
        info: dict[str, Any] = {
            "currentVersion": VERSION, "id": layer.id, "name": layer.name, "type": "Table" if layer.is_table else "Feature Layer",
            "description": layer.description, "copyrightText": "mock_esri", "defaultVisibility": True,
            "editFieldsInfo": None, "relationships": [], "isDataVersioned": False,
            "supportsRollbackOnFailureParameter": True, "supportsStatistics": False, "supportsAdvancedQueries": True,
            "supportsCoordinatesQuantization": False, "supportsApplyEditsWithGlobalIds": False,
            "advancedQueryCapabilities": {"supportsPagination": layer.pagination, "supportsOrderBy": True, "supportsDistinct": True,
                                          "supportsQueryWithResultType": False, "supportsReturningGeometryCentroid": False,
                                          "supportsStatistics": False, "supportsCountDistinct": False},
            "objectIdField": layer.oid, "globalIdField": "", "displayField": next((f["name"] for f in layer.fields[1:] if f["type"] == "esriFieldTypeString"), layer.oid),
            "typeIdField": "", "types": [], "templates": [], "fields": layer.fields,
            "maxRecordCount": layer.max_record_count, "standardMaxRecordCount": layer.max_record_count,
            "supportedQueryFormats": "JSON", "capabilities": "Create,Delete,Query,Update,Editing", "hasAttachments": False,
            "htmlPopupType": "esriServerHTMLPopupTypeNone", "hasM": False, "hasZ": False, "useStandardizedQueries": True,
            "dateFieldsTimeReference": {"timeZone": "UTC", "respectsDaylightSaving": False},
        }
        if not layer.is_table:
            extent = self.store.extent(layer) or {"xmin": -180, "ymin": -90, "xmax": 180, "ymax": 90, "spatialReference": {"wkid": 4326, "latestWkid": 4326}}
            info.update({"geometryType": layer.geometry_type, "extent": extent, "sourceSpatialReference": {"wkid": 4326, "latestWkid": 4326},
                         "drawingInfo": {"renderer": self._renderer(layer.geometry_type), "transparency": 0, "labelingInfo": None},
                         "minScale": 0, "maxScale": 0})
        return info

    @staticmethod
    def _renderer(geometry_type: str) -> dict:
        if geometry_type in ("esriGeometryPoint", "esriGeometryMultipoint"):
            symbol = {"type": "esriSMS", "style": "esriSMSCircle", "color": [230, 0, 0, 255], "size": 6, "angle": 0, "xoffset": 0, "yoffset": 0,
                      "outline": {"color": [255, 255, 255, 255], "width": 1}}
        elif geometry_type == "esriGeometryPolyline":
            symbol = {"type": "esriSLS", "style": "esriSLSSolid", "color": [230, 0, 0, 255], "width": 1.5}
        else:
            symbol = {"type": "esriSFS", "style": "esriSFSSolid", "color": [230, 0, 0, 40],
                      "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": [230, 0, 0, 255], "width": 1}}
        return {"type": "simple", "symbol": symbol, "label": "", "description": ""}

    def service_info(self, service: str) -> dict:
        layers = self.store.service_layers(service)
        if not layers:
            raise ServiceError("サービスなし", code=404, details=[service])
        boxes = [self.store.extent(l) for l in layers if not l.is_table]
        boxes = [b for b in boxes if b]
        full = ({"xmin": min(b["xmin"] for b in boxes), "ymin": min(b["ymin"] for b in boxes), "xmax": max(b["xmax"] for b in boxes),
                 "ymax": max(b["ymax"] for b in boxes)} if boxes else {"xmin": -180, "ymin": -90, "xmax": 180, "ymax": 90})
        full["spatialReference"] = {"wkid": 4326, "latestWkid": 4326}
        return {
            "currentVersion": VERSION, "serviceDescription": "mock_esri", "hasVersionedData": False, "supportsDisconnectedEditing": False,
            "hasStaticData": False, "maxRecordCount": max(l.max_record_count for l in layers), "supportedQueryFormats": "JSON",
            "capabilities": "Create,Delete,Query,Update,Editing", "description": "", "copyrightText": "mock_esri",
            "spatialReference": {"wkid": 4326, "latestWkid": 4326}, "initialExtent": full, "fullExtent": full,
            "allowGeometryUpdates": True, "units": "esriDecimalDegrees", "syncEnabled": False, "supportsApplyEditsWithGlobalIds": False,
            "layers": [{"id": l.id, "name": l.name, "parentLayerId": -1, "defaultVisibility": True, "subLayerIds": None,
                        "minScale": 0, "maxScale": 0, "geometryType": l.geometry_type, "type": "Feature Layer"} for l in layers if not l.is_table],
            "tables": [{"id": l.id, "name": l.name, "type": "Table"} for l in layers if l.is_table],
        }

    # --- query --------------------------------------------------------------------------------------------

    def query(self, layer: Layer, form: dict[str, str]) -> dict:
        where, args = sql_where(form.get("where"), layer.columns())
        clauses, params = [f"({where})"], list(args)
        if form.get("objectIds", "").strip():
            try:
                ids = [int(float(i)) for i in re.split(r"[,\s]+", form["objectIds"].strip()) if i]
            except ValueError:
                raise ServiceError("objectIds が数以外") from None
            clauses.append(f"oid IN ({', '.join('?' for _ in ids)})")
            params.extend(ids)
        in_sr = wkid_of(form.get("inSR") or 4326)
        if form.get("geometry", "").strip() and not layer.is_table:
            g = filter_geometry(form["geometry"], form.get("geometryType"))
            g = reproject(g, wkid_of(g.get("spatialReference")) if g.get("spatialReference") else in_sr, 4326)
            box = bbox_of(g)
            if box:
                distance = float(form.get("distance") or 0)
                if distance and (form.get("units") or "").lower() in ("esrisrunit_meter", "esrisrunit_kilometer", ""):
                    d = distance * (1000 if "kilo" in (form.get("units") or "").lower() else 1) / 111_320
                    box = (box[0] - d, box[1] - d, box[2] + d, box[3] + d)
                clauses.append("_xmin <= ? AND _xmax >= ? AND _ymin <= ? AND _ymax >= ?")
                params.extend([box[2], box[0], box[3], box[1]])
        sql = " AND ".join(clauses)
        oid = layer.oid
        if _truthy(form.get("returnIdsOnly")):
            ids = [r["oid"] for r in self.store.select(layer, sql, params)]
            if _truthy(form.get("returnCountOnly")):
                return {"count": len(ids)}
            return {"objectIdFieldName": oid, "objectIds": ids}
        if _truthy(form.get("returnCountOnly")) and not _truthy(form.get("returnExtentOnly")):
            return {"count": len(self.store.select(layer, sql, params))}
        if _truthy(form.get("returnExtentOnly")):
            out_sr = wkid_of(form.get("outSR") or 4326)
            extent = self.store.extent(layer, sql, params)
            out: dict[str, Any] = {"extent": reproject(extent, 4326, out_sr) if extent else {"xmin": "NaN", "ymin": "NaN", "xmax": "NaN", "ymax": "NaN"}}
            if _truthy(form.get("returnCountOnly")):
                out["count"] = len(self.store.select(layer, sql, params))
            return out

        by_upper = {f["name"].upper(): f for f in layer.fields}
        names = (form.get("outFields") or "").strip()
        if names in ("", "*"):
            chosen = layer.fields if names == "*" else [layer.fields[0]]
        else:
            chosen = []
            for n in names.split(","):
                f = by_upper.get(n.strip().upper())
                if f is None:
                    raise ServiceError("outFields に存在しない属性", details=[n.strip()])
                if f not in chosen:
                    chosen.append(f)
        order = self._order(form.get("orderByFields"), layer)
        rows = self.store.select(layer, sql, params, order)
        distinct = _truthy(form.get("returnDistinctValues"))
        geometry = not layer.is_table and not distinct and form.get("returnGeometry", "true").strip().lower() != "false"
        out_sr = wkid_of(form.get("outSR") or 4326)
        features = []
        seen = set()
        for r in rows:
            attributes = {f["name"]: r["oid" if f["name"] == oid else f["name"]] for f in chosen}
            if distinct:
                key = tuple(attributes.values())
                if key in seen:
                    continue
                seen.add(key)
            feature: dict[str, Any] = {"attributes": attributes}
            if geometry:
                g = json.loads(r["_geom"]) if r["_geom"] else None
                feature["geometry"] = {k: v for k, v in reproject(g, 4326, out_sr).items() if k != "spatialReference"} if g and out_sr != 4326 else g
            features.append(feature)
        offset = int(float(form.get("resultOffset") or 0))
        if "resultOffset" in form and not layer.pagination:
            raise ServiceError("ページングは使用不可（supportsPagination: false）")
        size = layer.max_record_count
        if form.get("resultRecordCount", "").strip():
            size = max(1, min(size, int(float(form["resultRecordCount"]))))
        page = features[offset:offset + size]
        answer: dict[str, Any] = {"objectIdFieldName": oid, "uniqueIdField": {"name": oid, "isSystemMaintained": True},
                                  "globalIdFieldName": "", "fields": [{k: v for k, v in f.items() if k in ("name", "type", "alias", "length")} for f in chosen],
                                  "features": page}
        if not layer.is_table:
            answer["geometryType"] = layer.geometry_type
            answer["spatialReference"] = {"wkid": out_sr, "latestWkid": 3857 if out_sr in MERCATOR else out_sr}
        if offset + size < len(features):
            answer["exceededTransferLimit"] = True
        return answer

    @staticmethod
    def _order(text: Optional[str], layer: Layer) -> str:
        if not text or not text.strip():
            return "oid"
        columns = layer.columns()
        parts = []
        for item in text.split(","):
            m = re.fullmatch(r"\s*\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*(ASC|DESC)?\s*", item, re.IGNORECASE)
            if not m or m.group(1).upper() not in columns:
                raise ServiceError("orderByFields に存在しない属性", details=[item.strip()])
            parts.append(f"{columns[m.group(1).upper()]} {(m.group(2) or 'ASC').upper()}")
        return ", ".join(parts + ["oid"])

    # --- edits --------------------------------------------------------------------------------------------

    @staticmethod
    def _json(form: dict[str, str], key: str, default: Any) -> Any:
        text = form.get(key)
        if text is None or not text.strip():
            return default
        try:
            return json.loads(text)
        except ValueError:
            raise ServiceError(f"{key} が JSON 以外") from None

    @staticmethod
    def _ids(value: Any) -> list[int]:
        if isinstance(value, list):
            return [int(v) for v in value]
        return [int(float(v)) for v in re.split(r"[,\s]+", str(value or "").strip()) if v]

    def edits(self, layer: Layer, adds: list, updates: list, deletes: list[int], rollback: bool, in_sr: int) -> dict:
        store = self.store
        warn: list[str] = []
        results: dict[str, list] = {"addResults": [], "updateResults": [], "deleteResults": []}
        failed = False
        with store.lock:
            store.db.execute("BEGIN")
            try:
                for f in adds:
                    try:
                        oid = store.add(layer, f, in_sr, warn)
                        results["addResults"].append({"objectId": oid, "globalId": None, "success": True})
                    except ValueError as error:
                        failed = True
                        results["addResults"].append({"objectId": -1, "success": False, "error": {"code": 1000, "description": str(error)}})
                for f in updates:
                    oid = next((v for k, v in (f.get("attributes") or {}).items() if str(k).upper() == layer.oid.upper()), None)
                    try:
                        store.update(layer, f, in_sr, warn)
                        results["updateResults"].append({"objectId": oid, "globalId": None, "success": True})
                    except LookupError:
                        failed = True
                        results["updateResults"].append({"objectId": oid, "success": False, "error": {"code": 1019, "description": "Object is missing."}})
                    except ValueError as error:
                        failed = True
                        results["updateResults"].append({"objectId": oid, "success": False, "error": {"code": 1000, "description": str(error)}})
                for oid in deletes:
                    ok = store.delete(layer, oid)
                    failed = failed or not ok
                    results["deleteResults"].append({"objectId": oid, "globalId": None, "success": ok,
                                                     **({} if ok else {"error": {"code": 1019, "description": "Object is missing."}})})
                if failed and rollback:
                    store.db.execute("ROLLBACK")
                    for key in results:
                        for r in results[key]:
                            if r.get("success"):
                                r["success"] = False
                                r["error"] = {"code": 1017, "description": "Rolled back（rollbackOnFailure）"}
                else:
                    store.db.execute("COMMIT")
            except BaseException:
                store.db.execute("ROLLBACK")
                raise
        if warn:
            self.log(f"  注意: {layer.service}/{layer.id} にない属性は無視: {', '.join(sorted(set(warn)))}")
        return results

    def apply_edits(self, layer: Layer, form: dict[str, str]) -> dict:
        rollback = form.get("rollbackOnFailure", "true").strip().lower() != "false"
        return self.edits(layer, self._json(form, "adds", []), self._json(form, "updates", []),
                          self._ids(form.get("deletes")), rollback, wkid_of(form.get("inSR") or 4326))

    def service_apply_edits(self, service: str, form: dict[str, str]) -> list:
        out = []
        rollback = form.get("rollbackOnFailure", "true").strip().lower() != "false"
        for edit in self._json(form, "edits", []):
            layer = self.store.layer(service, int(edit.get("id", 0)))
            r = self.edits(layer, edit.get("adds") or [], edit.get("updates") or [], self._ids(edit.get("deletes") or []), rollback, 4326)
            out.append({"id": layer.id, **r})
        return out

    def delete_features(self, layer: Layer, form: dict[str, str]) -> dict:
        ids = self._ids(form.get("objectIds"))
        if form.get("where", "").strip() or form.get("geometry", "").strip():
            ids += [i for i in self.query(layer, {"where": form.get("where") or "1=1", "geometry": form.get("geometry", ""),
                                                   "inSR": form.get("inSR", ""), "returnIdsOnly": "true"})["objectIds"] if i not in ids]
        return {"deleteResults": self.edits(layer, [], [], ids, False, 4326)["deleteResults"]}

    # --- tokens -------------------------------------------------------------------------------------------

    def check_token(self, form: dict[str, str], headers) -> None:
        if not self.token:
            return
        given = form.get("token")
        for h in ("X-Esri-Authorization", "Authorization"):
            v = headers.get(h)
            if v and v.lower().startswith("bearer "):
                given = given or v[7:].strip()
        if not given:
            raise ServiceError("Token Required", code=499)
        if given != self.token and given not in self.issued:
            raise ServiceError("Invalid token.", code=498)

    def generate_token(self, form: dict[str, str]) -> dict:
        if not form.get("username"):
            raise ServiceError("username と password が必要", code=400)
        token = secrets.token_urlsafe(24)
        self.issued.add(token)
        expiration = int(form.get("expiration") or 60)
        return {"token": token, "expires": now_ms() + expiration * 60_000, "ssl": False}

    # --- routing ------------------------------------------------------------------------------------------

    def answer(self, path: str, form: dict[str, str], headers) -> Any:
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        low = [p.lower() for p in parts]
        if low[-1:] == ["generatetoken"]:
            return self.generate_token(form)
        if low[:2] != ["arcgis", "rest"]:
            raise ServiceError("ページなし", code=404, details=[path])
        rest, low = parts[2:], low[2:]
        if low == ["info"]:
            return {"currentVersion": VERSION, "fullVersion": "11.1.0", "authInfo": {
                "isTokenBasedSecurity": bool(self.token), **({"tokenServicesUrl": "/arcgis/tokens/generateToken", "shortLivedTokenValidity": 60} if self.token else {})}}
        if low[:1] != ["services"]:
            raise ServiceError("ページなし", code=404, details=[path])
        rest, low = rest[1:], low[1:]
        self.check_token(form, headers)
        if not rest:
            return {"currentVersion": VERSION, "folders": [], "services": [{"name": s, "type": "FeatureServer"} for s in self.store.services()]}
        if len(rest) < 2 or low[1] not in ("featureserver", "mapserver"):
            raise ServiceError("サービスなし", code=404, details=["/".join(rest)])
        service = rest[0]
        tail, tail_low = rest[2:], low[2:]
        if not tail:
            return self.service_info(service)
        if tail_low == ["applyedits"]:
            return self.service_apply_edits(service, form)
        if tail_low == ["layers"]:
            return {"layers": [self.layer_info(l) for l in self.store.service_layers(service) if not l.is_table],
                    "tables": [self.layer_info(l) for l in self.store.service_layers(service) if l.is_table]}
        if not re.fullmatch(r"\d+", tail[0]):
            raise ServiceError("ページなし", code=404, details=[path])
        layer = self.store.layer(service, int(tail[0]))
        op = tail_low[1] if len(tail_low) > 1 else ""
        if not op:
            return self.layer_info(layer)
        if op == "query":
            return self.query(layer, form)
        if op == "applyedits":
            return self.apply_edits(layer, form)
        if op == "addfeatures":
            return {"addResults": self.edits(layer, self._json(form, "features", []), [], [], form.get("rollbackOnFailure", "true") != "false",
                                             wkid_of(form.get("inSR") or 4326))["addResults"]}
        if op == "updatefeatures":
            return {"updateResults": self.edits(layer, [], self._json(form, "features", []), [], form.get("rollbackOnFailure", "true") != "false",
                                                wkid_of(form.get("inSR") or 4326))["updateResults"]}
        if op == "deletefeatures":
            return self.delete_features(layer, form)
        raise ServiceError("この操作は未対応", details=[op])


# --- HTTP -------------------------------------------------------------------------------------------------


def make_handler(service: Service, files: str, quiet: bool = False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mock_esri/1"

        def log_message(self, fmt, *args):  # noqa: N802 - the base class's name
            pass

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Headers", "Authorization, X-Esri-Authorization, Content-Type, Range")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):  # noqa: N802
            self._handle("HEAD")

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def _form(self) -> dict[str, str]:
            url = urlsplit(self.path)
            form = dict(parse_qsl(url.query, keep_blank_values=True))
            if self.command == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                kind = (self.headers.get("Content-Type") or "").lower()
                if "multipart/form-data" in kind:
                    form.update(_multipart(body, kind))
                elif body:
                    form.update(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
            return form

        def _handle(self, method: str) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/files/") or path == "/files":
                return self._file(unquote(path[len("/files/"):]), method)
            started = time.time()
            try:
                form = self._form()
            except (ValueError, UnicodeDecodeError):
                form = {}
            try:
                value = service.answer(path, form, self.headers)
                status = 200
            except ServiceError as error:
                value = {"error": {"code": error.code, "message": str(error), "details": error.details}}
                status = 200  # ArcGIS answers errors with 200 and an error body
            except Exception as error:  # a bug of the mock: say it rather than drop the connection
                value = {"error": {"code": 500, "message": f"モックの内部エラー: {type(error).__name__}: {error}", "details": []}}
                status = 200
            body = json.dumps(value, ensure_ascii=False, indent=2 if form.get("f") == "pjson" else None).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)
            if not quiet:
                op = path.rsplit("/", 1)[-1]
                note = ""
                if isinstance(value, dict) and "error" in value:
                    note = f" → エラー {value['error']['code']}: {value['error']['message']} {' '.join(value['error']['details'])}"
                elif isinstance(value, dict) and "features" in value:
                    note = f" → {len(value['features'])} 件"
                elif isinstance(value, dict) and "addResults" in value:
                    note = " → " + " ".join(f"{k[:-7]} {len(v)}" for k, v in value.items() if v)
                where = f" where={form['where']}" if form.get("where") and op == "query" else ""
                print(f"{time.strftime('%H:%M:%S')} {method} {path}{where}{note} ({(time.time() - started) * 1000:.0f} ms)", flush=True)

        def _file(self, relative: str, method: str) -> None:
            full = os.path.realpath(os.path.join(files, relative))
            if not full.startswith(os.path.realpath(files) + os.sep) or not os.path.isfile(full):
                self.send_response(404)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            size = os.path.getsize(full)
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            partial = bool(m) and size > 0
            if partial:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2) or 0))
                if start > end:
                    self.send_response(416)
                    self._cors()
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if partial else 200)
            self._cors()
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if method != "HEAD":
                with open(full, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))

    return Handler


def _multipart(body: bytes, kind: str) -> dict[str, str]:
    m = re.search(r'boundary="?([^";]+)"?', kind)
    if not m:
        return {}
    out = {}
    for part in body.split(b"--" + m.group(1).encode()):
        head, _, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if name and b"filename=" not in head:
            out[name.group(1).decode()] = value.rstrip(b"\r\n").decode("utf-8", "replace")
    return out


# --- command line -----------------------------------------------------------------------------------------


def load_services(store: Store, definition: dict, replace: bool = False, features: bool = True) -> int:
    """
    Defines the layers of `{"services": [{"name": …, "layers": [{…, "features": […]}]}]}`
    (and adds their features); the number of features added.
    """
    added = 0
    for s in definition.get("services") or []:
        for l in s.get("layers") or []:
            layer = store.define(s["name"], l, replace=replace)
            if features:
                for f in l.get("features") or []:
                    store.add(layer, f)
                    added += 1
    return added


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ArcGIS フィーチャーサービスのモック（Esri なしでの開発用）")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "new-image", "config"],
                        help="serve: 起動（既定） / new-image: 画像カタログに新着画像を 1 件登録 / config: 設定ファイルだけ書き出し")
    parser.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス（既定 127.0.0.1。ほかの PC から使うなら 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=DEFAULT_DATA, help=f"データのフォルダ（既定 {DEFAULT_DATA}）")
    parser.add_argument("--reset", action="store_true", help="データを消してサンプルから作り直す")
    parser.add_argument("--empty", action="store_true", help="サンプルの地物を入れない（レイヤーの定義だけ）")
    parser.add_argument("--services", action="append", default=[], help="レイヤーの定義（JSON）を足す。複数可")
    parser.add_argument("--token", help="このトークン（か generateToken で発行したもの）がない要求を断る")
    parser.add_argument("--public-url", help="設定ファイルに書く URL の先頭（既定 http://<host>:<port>）")
    parser.add_argument("--quiet", action="store_true", help="要求を 1 行ずつ表示しない")
    a = parser.parse_args(argv)

    sys.path.insert(0, HERE)
    import sample  # noqa: E402 - next to this file

    os.makedirs(a.data, exist_ok=True)
    db_path = os.path.join(a.data, "mock.sqlite")
    files = os.path.join(a.data, "files")
    host = "127.0.0.1" if a.host in ("0.0.0.0", "", "::") else a.host
    base = (a.public_url or f"http://{host}:{a.port}").rstrip("/")
    store = Store(db_path)
    if a.reset:
        store.drop_all()
    if not store.layers:
        added = load_services(store, sample.services(files, base, with_features=not a.empty))
        sample.write_images(files)
        store.meta("created", str(now_ms()))
        print(f"サンプルデータを作成（{added} 件）: {db_path}")
    else:
        # New layers of the sample (a newer version of this script) are added, kept data is left as it is.
        for s in sample.services(files, base, with_features=False)["services"]:
            for l in s["layers"]:
                old = store.layers.get((s["name"].lower(), int(l.get("id", 0))))
                if old is None:
                    store.define(s["name"], l)
                    print(f"レイヤーを追加: {s['name']}/FeatureServer/{l.get('id', 0)}")
                    continue
                # New fields of the sample (the tracking fields …) are added as columns.
                have = {f["name"].lower() for f in old.fields}
                missing = {n: v for n, v in l["fields"].items() if n.lower() not in have}
                if missing:
                    definition = old.definition()
                    store.define(s["name"], {**definition, "fields": {**definition["fields"], **missing}})
                    print(f"属性を追加: {s['name']}/FeatureServer/{l.get('id', 0)}（{', '.join(missing)}）")
    for path in a.services:
        with open(path, encoding="utf-8-sig") as f:
            n = load_services(store, json.load(f))
        print(f"定義を読み込み: {path}（{n} 件）")

    if a.command == "new-image":
        image_id = sample.add_new_image(store, files, base)
        print(f"新着画像を登録: {image_id}")
        return 0
    config_path = os.path.join(a.data, "config.mock.jsonc")
    sample.write_config(config_path, base, files, a.token)
    print(f"設定ファイル: {config_path}")
    if a.command == "config":
        return 0

    service = Service(store, a.token)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(service, files, a.quiet))
    print(f"起動: {base}/arcgis/rest/services （Ctrl+C で終了）")
    for name in store.services():
        print(f"  {base}/arcgis/rest/services/{name}/FeatureServer")
    if a.token:
        print(f"  トークン: {a.token}（設定ファイルの token に書き込み済み）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
