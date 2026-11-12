"""
地名・国の控え (tsudo, 2026-11-10: 「国ポリゴンやっぱ大きいので、都度サーバーに
照合してだと時間もかかるので、キャッシュ確認、サーバーの流れ」「全てプリロードは
でか過ぎるので、1回使ったやつはキャッシュにいる」).

The named areas and countries a record is in (通報 `place`・`country`, the
export's places, the expression functions) are looked up square by square
(CELL_DEGREES of longitude and latitude) instead of reading the whole layer:

- A square looked up before: its polygons are here (in memory, else in the
  file on this PC, kept across QGIS sessions), and which of them hold the
  point is told here, without asking the server.
- A square not looked up yet: the server is asked which features cross it
  (their object ids only, a light query), only those not here yet are read
  (attributes and rings, once each), and all are kept.

The square is the unit, not "a polygon kept holds the point": named areas
nest (横須賀本港 in 横須賀港 in 東京湾), and with only 東京湾 kept a point of
横須賀港 would be named 東京湾. Every polygon holding a point crosses its
square, so a square looked up has all of them.

Kept up to date:
- The layer's last edit time (its information's editingInfo, when the
  service tells it) is asked at most every CHECK_SECONDS, and at each 抽出
  (`fresh`). Squares looked up before an edit are asked again when next
  used, and a feature is read again when its edit time changed (the layer's
  editor tracking field, when there is one; else all features of the
  squares asked again).
- A layer that tells no edit time: its squares are asked again after
  CHECK_SECONDS (features added or removed), at each 抽出, and its features
  read again after KEEP_SECONDS (or when their edit field says so).

At most MEMORY_POINTS points in memory and DISK_BYTES in the file: the
least recently used go first (and are read again when needed).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import threading
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from ..esri import EsriClient, EsriError
from ..util import is_number
from .alerts import EsriSource, NamedArea, named_area_of, places_of

#: The side of a square (degrees).
CELL_DEGREES = 1.0
#: How often a layer's last edit time is asked (and, when it tells none, its squares asked again).
CHECK_SECONDS = 600.0
#: How long a feature is kept without reading it again, when the layer tells neither an edit time nor its features' edit times.
KEEP_SECONDS = 86_400.0
#: The points (vertices) kept in memory, about 16 bytes each; the least recently used shapes go first.
MEMORY_POINTS = 4_000_000
#: The size of the file (compressed shapes); the least recently used go first.
DISK_BYTES = 200 * 1024 * 1024
#: Features read at once by their ids (a country can be large).
READ_CHUNK = 50
#: Squares side by side asked in one query at most.
RUN_CELLS = 20
#: A used shape's time in the file is brought up to date at most this often (not a write on every look).
USED_STEP = 3600.0

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS layers (key TEXT PRIMARY KEY, stamp REAL, checked_at REAL, edit_field TEXT, oid_field TEXT)",
    "CREATE TABLE IF NOT EXISTS squares (key TEXT, cx INTEGER, cy INTEGER, oids TEXT, checked_at REAL, stamp REAL, PRIMARY KEY (key, cx, cy))",
    "CREATE TABLE IF NOT EXISTS shapes (key TEXT, oid INTEGER, edit REAL, stamp REAL, read_at REAL, used_at REAL, bytes INTEGER, data BLOB,"
    " PRIMARY KEY (key, oid))",
    "CREATE INDEX IF NOT EXISTS shapes_used ON shapes (used_at)",
)

_UNSEEN = object()


def cell_of(lon: float, lat: float) -> tuple[int, int]:
    """The square a point is in (the east and north edges belong to the next square, but 180° and 90° to the last)."""
    return (min(int(math.floor(lon / CELL_DEGREES)), int(round(180 / CELL_DEGREES)) - 1),
            min(int(math.floor(lat / CELL_DEGREES)), int(round(90 / CELL_DEGREES)) - 1))


def cell_box(cx: int, cy: int) -> tuple[float, float, float, float]:
    return cx * CELL_DEGREES, cy * CELL_DEGREES, (cx + 1) * CELL_DEGREES, (cy + 1) * CELL_DEGREES


def runs_of(cells: Iterable[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """Squares side by side (east to west, one row) as (first cx, last cx, cy), at most RUN_CELLS each."""
    out: list[tuple[int, int, int]] = []
    for cx, cy in sorted(set(cells), key=lambda c: (c[1], c[0])):
        if out and out[-1][2] == cy and out[-1][1] == cx - 1 and cx - out[-1][0] < RUN_CELLS:
            out[-1] = (out[-1][0], cx, cy)
        else:
            out.append((cx, cx, cy))
    return out


def _overlaps(a: tuple, b: tuple) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _number(v: Any) -> Optional[float]:
    return float(v) if is_number(v) else None


@dataclass
class _Shape:
    """A feature as read: its attributes and rings ((n, 2) arrays), and when."""
    attributes: dict
    rings: list
    box: tuple
    points: int
    #: Its edit time (the layer's editor tracking field), when the layer has one.
    edit: Optional[float]
    #: The layer's last edit time it is known to be good under.
    stamp: Optional[float]
    read_at: float
    used_at: float
    #: The named area made of it, by the settings' attribute names (None: not one, e.g. not active).
    areas: dict = field(default_factory=dict, repr=False)


@dataclass
class _Square:
    """The features crossing a square (their ids), and when that was asked."""
    oids: tuple
    checked_at: float
    stamp: Optional[float]


@dataclass
class _Layer:
    key: str
    url: str
    stamp: Optional[float] = None
    checked_at: float = 0.0
    edit_field: str = ""
    oid_field: str = ""
    squares: dict = field(default_factory=dict)
    shapes: dict = field(default_factory=dict)
    #: One lookup with the server at a time per layer (two tasks wanting the same square read it once).
    busy: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _pack(shape: _Shape) -> bytes:
    head = json.dumps({"a": shape.attributes, "n": [len(r) for r in shape.rings]}, ensure_ascii=False).encode("utf-8")
    coords = np.concatenate(shape.rings).astype("<f8").tobytes() if shape.rings else b""
    return zlib.compress(struct.pack("<I", len(head)) + head + coords, 1)


def _unpack(data: bytes) -> tuple[dict, list]:
    raw = zlib.decompress(data)
    (n,) = struct.unpack_from("<I", raw)
    head = json.loads(raw[4:4 + n].decode("utf-8"))
    coords = np.frombuffer(raw, dtype="<f8", offset=4 + n).reshape(-1, 2)
    rings, at = [], 0
    for size in head.get("n") or []:
        rings.append(coords[at:at + size])
        at += size
    return head.get("a") or {}, rings


def _shape_of(attributes: dict, rings: Sequence, edit: Optional[float], stamp: Optional[float], now: float) -> _Shape:
    arrays = []
    for ring in rings or []:
        try:
            a = np.asarray(ring, dtype=float)
        except (TypeError, ValueError):  # points of uneven lengths (some with z)
            a = np.asarray([[p[0], p[1]] for p in ring], dtype=float)
        if a.ndim == 2 and a.shape[0] and a.shape[1] >= 2:
            arrays.append(np.ascontiguousarray(a[:, :2]))
    if arrays:
        both = np.concatenate(arrays)
        box = (float(both[:, 0].min()), float(both[:, 1].min()), float(both[:, 0].max()), float(both[:, 1].max()))
    else:
        box = (0.0, 0.0, 0.0, 0.0)
    return _Shape(attributes, arrays, box, sum(len(a) for a in arrays), edit, stamp, now, now)


class PlaceCache:
    """The squares looked up and the features read, per layer (and its `where`); in memory and, with a file, on this PC."""

    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time,
                 memory_points: int = MEMORY_POINTS, disk_bytes: int = DISK_BYTES):
        self.clock = clock
        self.memory_points = memory_points
        self.disk_bytes = disk_bytes
        self.path: Optional[str] = None
        self._db: Optional[sqlite3.Connection] = None
        #: Memory and the file (held briefly; never while asking a server).
        self._lock = threading.RLock()
        self._layers: dict[str, _Layer] = {}
        self._lru: "OrderedDict[tuple[str, int], int]" = OrderedDict()
        self._points = 0
        if path:
            self.use_file(path)

    # ---- the file ----------------------------------------------------------------

    def use_file(self, path: Optional[str]) -> None:
        """Keeps what is read in `path` (SQLite) from now on; None: memory only. What was in memory is let go."""
        with self._lock:
            self.close()
            if not path:
                return
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                db = sqlite3.connect(path, timeout=10, check_same_thread=False)
                with db:
                    for sql in _SCHEMA:
                        db.execute(sql)
            except (OSError, sqlite3.Error):
                return  # memory only (a profile folder that cannot be written)
            self._db, self.path = db, path

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass
            self._db, self.path = None, None
            self.forget_memory()

    def forget_memory(self) -> None:
        """Lets go of what is in memory (the file keeps it)."""
        with self._lock:
            self._layers.clear()
            self._lru.clear()
            self._points = 0

    def clear(self) -> None:
        """Throws away everything kept (memory and file): read from the servers again when next used."""
        with self._lock:
            self.forget_memory()
            if self._db is not None:
                try:
                    with self._db:
                        for table in ("layers", "squares", "shapes"):
                            self._db.execute(f"DELETE FROM {table}")
                    self._db.execute("VACUUM")
                except sqlite3.Error:
                    pass

    def info(self) -> tuple[int, int, int]:
        """(squares, features, bytes) kept (in the file, else in memory)."""
        with self._lock:
            if self._db is not None:
                try:
                    squares = self._db.execute("SELECT COUNT(*) FROM squares").fetchone()[0]
                    shapes, size = self._db.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM shapes").fetchone()
                    return int(squares), int(shapes), int(size)
                except sqlite3.Error:
                    return 0, 0, 0
            return (sum(len(l.squares) for l in self._layers.values()), sum(len(l.shapes) for l in self._layers.values()),
                    self._points * 16)

    def _sql(self, sql: str, args: Sequence = (), many: bool = False):
        """A statement on the file (none: nothing); a failing file is left alone (memory still works)."""
        if self._db is None:
            return None
        try:
            if many:
                with self._db:
                    return self._db.executemany(sql, args)
            if sql.lstrip().upper().startswith("SELECT"):
                return self._db.execute(sql, args).fetchall()
            with self._db:
                return self._db.execute(sql, args)
        except sqlite3.Error:
            return None

    # ---- memory --------------------------------------------------------------------

    def _layer(self, source: EsriSource) -> _Layer:
        key = json.dumps([source.url, source.where or ""], ensure_ascii=False)
        with self._lock:
            layer = self._layers.get(key)
            if layer is None:
                layer = self._layers[key] = _Layer(key, source.url)
                rows = self._sql("SELECT stamp, checked_at, edit_field, oid_field FROM layers WHERE key = ?", (key,))
                if rows:
                    layer.stamp, layer.checked_at = rows[0][0], rows[0][1] or 0.0
                    layer.edit_field, layer.oid_field = rows[0][2] or "", rows[0][3] or ""
            return layer

    def _square(self, layer: _Layer, cell: tuple[int, int]) -> Optional[_Square]:
        with self._lock:
            sq = layer.squares.get(cell)
            if sq is None:
                rows = self._sql("SELECT oids, checked_at, stamp FROM squares WHERE key = ? AND cx = ? AND cy = ?", (layer.key, *cell))
                if rows:
                    try:
                        sq = layer.squares[cell] = _Square(tuple(int(i) for i in json.loads(rows[0][0])), rows[0][1] or 0.0, rows[0][2])
                    except (TypeError, ValueError):
                        sq = None
            return sq

    def _shape(self, layer: _Layer, oid: int) -> Optional[_Shape]:
        with self._lock:
            shape = layer.shapes.get(oid)
            if shape is not None:
                self._lru.move_to_end((layer.key, oid))
                return shape
            rows = self._sql("SELECT edit, stamp, read_at, used_at, data FROM shapes WHERE key = ? AND oid = ?", (layer.key, oid))
            if not rows:
                return None
            try:
                attributes, rings = _unpack(rows[0][4])
            except (zlib.error, ValueError, struct.error, UnicodeDecodeError):
                return None
            box = (0.0, 0.0, 0.0, 0.0)
            if rings:
                both = np.concatenate(rings)
                box = (float(both[:, 0].min()), float(both[:, 1].min()), float(both[:, 0].max()), float(both[:, 1].max()))
            shape = _Shape(attributes, rings, box, sum(len(r) for r in rings), rows[0][0], rows[0][1], rows[0][2] or 0.0, rows[0][3] or 0.0)
            self._remember(layer, oid, shape)
            return shape

    def _remember(self, layer: _Layer, oid: int, shape: _Shape) -> None:
        with self._lock:
            old = layer.shapes.get(oid)
            if old is not None:
                self._points -= old.points
            layer.shapes[oid] = shape
            self._lru[(layer.key, oid)] = shape.points
            self._lru.move_to_end((layer.key, oid))
            self._points += shape.points
            while self._points > self.memory_points and len(self._lru) > 1:
                (key, gone), n = self._lru.popitem(last=False)
                owner = self._layers.get(key)
                if owner is not None:
                    owner.shapes.pop(gone, None)
                self._points -= n

    def _keep_shapes(self, layer: _Layer, shapes: dict[int, _Shape]) -> None:
        with self._lock:
            for oid, shape in shapes.items():
                self._remember(layer, oid, shape)
            if self._db is None or not shapes:
                return
            rows = []
            for oid, shape in shapes.items():
                data = _pack(shape)
                rows.append((layer.key, oid, shape.edit, shape.stamp, shape.read_at, shape.used_at, len(data), data))
            self._sql("INSERT OR REPLACE INTO shapes (key, oid, edit, stamp, read_at, used_at, bytes, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      rows, many=True)
            self._trim()

    def _trim(self) -> None:
        """The file within `disk_bytes`: the least recently used shapes go (to 90 %)."""
        rows = self._sql("SELECT COALESCE(SUM(bytes), 0) FROM shapes")
        total = int(rows[0][0]) if rows else 0
        if total <= self.disk_bytes:
            return
        goal = self.disk_bytes * 0.9
        gone = []
        for key, oid, size in self._sql("SELECT key, oid, bytes FROM shapes ORDER BY used_at") or []:
            if total <= goal:
                break
            gone.append((key, oid))
            total -= size or 0
        self._sql("DELETE FROM shapes WHERE key = ? AND oid = ?", gone, many=True)

    def _keep_square(self, layer: _Layer, cell: tuple[int, int], sq: _Square) -> None:
        with self._lock:
            layer.squares[cell] = sq
            self._sql("INSERT OR REPLACE INTO squares (key, cx, cy, oids, checked_at, stamp) VALUES (?, ?, ?, ?, ?, ?)",
                      (layer.key, cell[0], cell[1], json.dumps(list(sq.oids)), sq.checked_at, sq.stamp))

    def _keep_layer(self, layer: _Layer) -> None:
        self._sql("INSERT OR REPLACE INTO layers (key, stamp, checked_at, edit_field, oid_field) VALUES (?, ?, ?, ?, ?)",
                  (layer.key, layer.stamp, layer.checked_at, layer.edit_field, layer.oid_field))

    # ---- whether what is kept is still good ---------------------------------------------

    @staticmethod
    def _square_ok(layer: _Layer, sq: Optional[_Square], now: float, fresh: bool) -> bool:
        if sq is None:
            return False
        if layer.stamp is not None:
            return sq.stamp == layer.stamp
        return not fresh and now - sq.checked_at < CHECK_SECONDS

    @staticmethod
    def _shape_ok(layer: _Layer, shape: Optional[_Shape], now: float, edit: Any = _UNSEEN) -> bool:
        if shape is None:
            return False
        if edit is not _UNSEEN and layer.edit_field:
            return shape.edit == edit
        if layer.stamp is not None:
            return shape.stamp == layer.stamp
        if layer.edit_field:
            # Its square was asked (with the edit times) within CHECK_SECONDS.
            return True
        return now - shape.read_at < KEEP_SECONDS

    # ---- the server -------------------------------------------------------------------------

    def _check_layer(self, client: EsriClient, layer: _Layer, token: Optional[str], now: float) -> None:
        info = client.layer(layer.url, token, refresh=True)
        editing = info.raw.get("editingInfo") if isinstance(info.raw.get("editingInfo"), dict) else {}
        stamp = _number(editing.get("dataLastEditDate"))
        if stamp is None:
            stamp = _number(editing.get("lastEditDate"))
        tracking = info.raw.get("editFieldsInfo") if isinstance(info.raw.get("editFieldsInfo"), dict) else {}
        edit_field = tracking.get("editDateField") if isinstance(tracking.get("editDateField"), str) else ""
        types = {n.lower(): n for n in info.field_types}
        layer.stamp, layer.checked_at = stamp, now
        layer.edit_field = types.get(edit_field.lower(), "") if edit_field else ""
        layer.oid_field = info.object_id_field
        with self._lock:
            self._keep_layer(layer)

    @staticmethod
    def _value(a: dict, name: str) -> Any:
        if name in a:
            return a[name]
        low = name.lower()
        return next((v for k, v in a.items() if k.lower() == low), None)

    def _crossing(self, client: EsriClient, source: EsriSource, layer: _Layer, run: tuple[int, int, int],
                  token: Optional[str]) -> dict[int, Any]:
        """The ids (and edit times) of the features crossing a run of squares."""
        w, s, _, n = cell_box(run[0], run[2])
        e = cell_box(run[1], run[2])[2]
        envelope = {"xmin": w, "ymin": s, "xmax": e, "ymax": n, "spatialReference": {"wkid": 4326}}
        names = [layer.oid_field] + ([layer.edit_field] if layer.edit_field else [])
        rows = client.query_all(source.url, source.where or "1=1", token, out_fields=",".join(names), geometry=False, limit=100_000, extra={
            "geometry": json.dumps(envelope), "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects"})
        out: dict[int, Any] = {}
        for row in rows:
            a = row.get("attributes") or {}
            oid = self._value(a, layer.oid_field)
            if is_number(oid):
                out[int(oid)] = _number(self._value(a, layer.edit_field)) if layer.edit_field else None
        return out

    def _read(self, client: EsriClient, layer: _Layer, oids: list[int], token: Optional[str], now: float) -> dict[int, _Shape]:
        """The features of these ids, read (attributes and rings in WGS 84); one gone meanwhile is kept empty."""
        out: dict[int, _Shape] = {}
        size = max(1, min(READ_CHUNK, client.layer(layer.url, token).max_record_count))
        for start in range(0, len(oids), size):
            chunk = oids[start:start + size]
            rows = client.query_all(layer.url, "1=1", token, out_fields="*", geometry=True, limit=len(chunk),
                                    extra={"objectIds": ",".join(str(i) for i in chunk)})
            for row in rows:
                a = row.get("attributes") or {}
                oid = self._value(a, layer.oid_field)
                if not is_number(oid):
                    continue
                edit = _number(self._value(a, layer.edit_field)) if layer.edit_field else None
                out[int(oid)] = _shape_of(a, (row.get("geometry") or {}).get("rings") or [], edit, layer.stamp, now)
            for oid in chunk:
                if oid not in out:
                    out[oid] = _shape_of({}, [], None, layer.stamp, now)
        return out

    # ---- looking up ---------------------------------------------------------------------------

    def areas_at(self, client: EsriClient, source: Optional[EsriSource], points: Iterable[tuple[float, float]],
                 token: Optional[str] = None, fresh: bool = False) -> list[NamedArea]:
        """
        The named areas (or countries) of the squares the points are in, read
        from the server only where not kept (or no longer good); `fresh`: the
        layer's last edit asked now (抽出). When the server cannot be asked,
        what is kept is used if every square is there (else the error).
        """
        if source is None:
            return []
        cells = sorted({cell_of(lon, lat) for lon, lat in points
                        if is_number(lon) and is_number(lat) and math.isfinite(lon) and math.isfinite(lat)})
        if not cells:
            return []
        layer = self._layer(source)
        token = token or source.token
        with layer.busy:
            now = self.clock()
            have: dict[int, _Shape] = {}
            try:
                squares = self._ask(client, source, layer, cells, token, now, fresh, have)
            except EsriError:
                squares = self._kept(layer, cells, have)
                if squares is None:
                    raise
            used = []
            oids = sorted({oid for sq in squares for oid in sq.oids})
            for oid in oids:
                shape = have.get(oid)
                if shape is not None and now - shape.used_at >= USED_STEP:
                    shape.used_at = now
                    used.append((now, layer.key, oid))
            if used:
                with self._lock:
                    self._sql("UPDATE shapes SET used_at = ? WHERE key = ? AND oid = ?", used, many=True)
        out = []
        for oid in oids:
            area = self._area(have[oid], source.fields)
            if area is not None:
                out.append(area)
        return out

    def _ask(self, client: EsriClient, source: EsriSource, layer: _Layer, cells: list[tuple[int, int]], token: Optional[str],
             now: float, fresh: bool, have: dict[int, _Shape]) -> list[_Square]:
        if fresh or not layer.oid_field or now - layer.checked_at >= CHECK_SECONDS:
            self._check_layer(client, layer, token, now)
        kept = {c: self._square(layer, c) for c in cells}
        again = [c for c in cells if not self._square_ok(layer, kept[c], now, fresh)]
        # The squares asked now: the ids crossing them, and the features to read (not here, or changed).
        crossing: dict[int, Any] = {}
        runs = runs_of(again)
        by_run = []
        for run in runs:
            found = self._crossing(client, source, layer, run, token)
            by_run.append((run, found))
            crossing.update(found)
        wanted: set[int] = set()
        verified: dict[int, _Shape] = {}
        for oid, edit in crossing.items():
            shape = self._shape(layer, oid)
            if self._shape_ok(layer, shape, now, edit):
                have[oid] = shape
                if shape.stamp != layer.stamp:
                    verified[oid] = shape
            else:
                wanted.add(oid)
        for c in cells:
            if c in again:
                continue
            for oid in kept[c].oids:
                if oid in have or oid in wanted:
                    continue
                shape = self._shape(layer, oid)
                if self._shape_ok(layer, shape, now):
                    have[oid] = shape
                else:
                    wanted.add(oid)
        read = self._read(client, layer, sorted(wanted), token, now) if wanted else {}
        have.update(read)
        for shape in verified.values():
            shape.stamp = layer.stamp
        with self._lock:
            self._keep_shapes(layer, read)
            if verified:
                self._sql("UPDATE shapes SET stamp = ? WHERE key = ? AND oid = ?",
                          [(layer.stamp, layer.key, oid) for oid in verified], many=True)
        out = []
        asked = set(again)
        for run, found in by_run:
            for cx in range(run[0], run[1] + 1):
                cell = (cx, run[2])
                box = cell_box(*cell)
                oids = tuple(sorted(oid for oid in found if have[oid].points and _overlaps(have[oid].box, box)))
                sq = _Square(oids, now, layer.stamp)
                if cell in asked:
                    self._keep_square(layer, cell, sq)
                    out.append(sq)
        out += [kept[c] for c in cells if c not in asked]
        return out

    def _kept(self, layer: _Layer, cells: list[tuple[int, int]], have: dict[int, _Shape]) -> Optional[list[_Square]]:
        """The squares and features kept, whatever their age (the server cannot be asked); None when one is missing."""
        out = []
        for c in cells:
            sq = self._square(layer, c)
            if sq is None:
                return None
            for oid in sq.oids:
                shape = have.get(oid) or self._shape(layer, oid)
                if shape is None:
                    return None
                have[oid] = shape
            out.append(sq)
        return out

    def _area(self, shape: _Shape, fields: dict[str, str]) -> Optional[NamedArea]:
        key = tuple(sorted(fields.items()))
        if key not in shape.areas:
            shape.areas[key] = named_area_of(shape.attributes, shape.rings, fields) if shape.points else None
        return shape.areas[key]

    def peek(self, source: Optional[EsriSource], lon: float, lat: float) -> tuple[Optional[list[NamedArea]], bool]:
        """
        Without asking any server (the map's render threads): the named areas
        holding a point, smallest first, from what is kept; None when its
        square (or one of its features) is not kept. The second value: whether
        to look it up again (not kept, or kept but no longer known to be good).
        """
        if source is None or not (is_number(lon) and is_number(lat) and math.isfinite(lon) and math.isfinite(lat)):
            return [], False
        layer = self._layer(source)
        cell = cell_of(lon, lat)
        now = self.clock()
        with self._lock:
            sq = self._square(layer, cell)
            if sq is None:
                return None, True
            shapes = [self._shape(layer, oid) for oid in sq.oids]
            if any(s is None for s in shapes):
                return None, True
            stale = (now - layer.checked_at >= CHECK_SECONDS or not self._square_ok(layer, sq, now, False)
                     or not all(self._shape_ok(layer, s, now) for s in shapes))
        areas = [a for a in (self._area(s, source.fields) for s in shapes) if a is not None]
        return places_of(lon, lat, areas), stale


#: The plugin's cache (memory only until the plugin gives it its file, place_cache.sqlite in the profile).
PLACES = PlaceCache()


def areas_at(client: EsriClient, source: Optional[EsriSource], points: Iterable[tuple[float, float]],
             token: Optional[str] = None, fresh: bool = False) -> list[NamedArea]:
    """The named areas (or countries) around the points, from the plugin's cache (PlaceCache.areas_at)."""
    return PLACES.areas_at(client, source, points, token, fresh)


def points_of(records: Iterable[Any]) -> list[tuple[float, float]]:
    """The positions of records (observations or alert items)."""
    out = []
    for r in records:
        o = getattr(r, "record", r)
        out.append((o.lon, o.lat))
    return out
