"""
A local GeoPackage (`observations.gpkg` in the QGIS user profile) written
with sqlite3 alone, so the stores work and are tested without QGIS, and the
file opens in QGIS and ArcGIS Pro as ordinary layers.

The tables use the same attribute names as the Esri layers of the viewer
(OBS_ID, OBJECT_ID …), so records move between them by copy. Dates are GPKG
DATETIME (ISO 8601 text in UTC); geometries are WGS 84 (EPSG:4326).
"""
from __future__ import annotations

import sqlite3
import struct
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .util import iso_text, ms_of

#: GeoPackage's application id ("GPKG") and the version written (1.3).
APPLICATION_ID = 0x47504B47
USER_VERSION = 10300

WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]'
)


@dataclass(frozen=True)
class Column:
    """An attribute: its name and GeoPackage type (TEXT(40), DATETIME, SMALLINT, INTEGER, DOUBLE, BLOB)."""
    name: str
    type: str


@dataclass(frozen=True)
class TableSpec:
    """A table: its name, geometry type (POINT, POLYGON; None for attributes only) and columns."""
    name: str
    geometry: Optional[str]
    columns: tuple[Column, ...]
    #: The column every row has a different value in (the record's own id).
    key: Optional[str] = None
    description: str = ""

    def column(self, name: str) -> Optional[Column]:
        return next((c for c in self.columns if c.name == name), None)


# ---------------------------------------------------------------------------
# Geometry: GeoPackage binary (a header and little-endian WKB)

_WKB_POINT, _WKB_POLYGON = 1, 3


def encode_point(lon: float, lat: float, srs_id: int = 4326) -> bytes:
    """A point as a GeoPackage geometry blob (no envelope, as GDAL writes points)."""
    header = b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", srs_id)
    return header + struct.pack("<BIdd", 1, _WKB_POINT, lon, lat)


def encode_polygon(rings: Sequence[Sequence[Sequence[float]]], srs_id: int = 4326) -> bytes:
    """A polygon (outer ring first, each closed) as a GeoPackage geometry blob with its XY envelope."""
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    flags = 0b00000011  # envelope [minx, maxx, miny, maxy], little endian
    header = b"GP" + bytes([0, flags]) + struct.pack("<i", srs_id) + struct.pack("<dddd", min(xs), max(xs), min(ys), max(ys))
    body = struct.pack("<BII", 1, _WKB_POLYGON, len(rings))
    for ring in rings:
        body += struct.pack("<I", len(ring)) + b"".join(struct.pack("<dd", p[0], p[1]) for p in ring)
    return header + body


def decode_geometry(blob: Optional[bytes]) -> Optional[Any]:
    """A point `(lon, lat)` or polygon rings `[[[lon, lat], …], …]` of a GeoPackage blob; None for others."""
    if not blob or len(blob) < 8 or blob[:2] != b"GP":
        return None
    flags = blob[3]
    envelope = (flags >> 1) & 0b111
    size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(envelope)
    if size is None or flags & 0b00010000:  # empty geometry
        return None
    wkb = blob[8 + size:]
    return _read_wkb(wkb)


def _read_wkb(wkb: bytes) -> Optional[Any]:
    order = "<" if wkb[0] == 1 else ">"
    kind = struct.unpack_from(order + "I", wkb, 1)[0]
    base = kind % 1000
    dims = 3 if kind // 1000 in (1, 2) else 4 if kind // 1000 == 3 else 2
    off = 5
    if base == _WKB_POINT:
        x, y = struct.unpack_from(order + "dd", wkb, off)
        return (x, y)
    if base == _WKB_POLYGON:
        (count,) = struct.unpack_from(order + "I", wkb, off)
        off += 4
        rings = []
        for _ in range(count):
            (n,) = struct.unpack_from(order + "I", wkb, off)
            off += 4
            ring = []
            for _ in range(n):
                x, y = struct.unpack_from(order + "dd", wkb, off)
                ring.append([x, y])
                off += 8 * dims
            rings.append(ring)
        return rings
    return None


# ---------------------------------------------------------------------------
# Dates


def datetime_text(ms: Optional[float]) -> Optional[str]:
    """A GPKG DATETIME of ms since the epoch."""
    return None if ms is None else iso_text(ms)


def datetime_ms(value: Any) -> Optional[int]:
    """ms of a GPKG DATETIME (or a number)."""
    return None if value is None or value == "" else ms_of(value)


# ---------------------------------------------------------------------------


class GeoPackage:
    """A GeoPackage file: its tables created on first use, columns added when a newer plugin needs more."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._ready: set[str] = set()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._init()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "GeoPackage":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _init(self) -> None:
        with self._lock:
            db = self._db
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            db.execute(f"PRAGMA user_version = {USER_VERSION}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY, "
                "organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, "
                "identifier TEXT UNIQUE, description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER, "
                "CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, "
                "geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, "
                "CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name), "
                "CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name), "
                "CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id))"
            )
            rows = [
                ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
                ("WGS 84 geodetic", 4326, "EPSG", 4326, WGS84_WKT, "longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid"),
            ]
            db.executemany("INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)", rows)

    def ensure(self, spec: TableSpec) -> None:
        """Creates the table when it is missing, and adds the columns it lacks."""
        if spec.name in self._ready:
            return
        with self._lock:
            db = self._db
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (spec.name,)).fetchone()
            if not exists:
                parts = ['"fid" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL']
                if spec.geometry:
                    parts.append(f'"geom" {spec.geometry}')
                for c in spec.columns:
                    unique = " UNIQUE" if c.name == spec.key else ""
                    parts.append(f'"{c.name}" {c.type}{unique}')
                db.execute("BEGIN")
                try:
                    db.execute(f'CREATE TABLE "{spec.name}" ({", ".join(parts)})')
                    db.execute(
                        "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, srs_id) VALUES (?, ?, ?, ?, ?)",
                        (spec.name, "features" if spec.geometry else "attributes", spec.name, spec.description, 4326 if spec.geometry else None),
                    )
                    if spec.geometry:
                        db.execute(
                            "INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, 4326, 0, 0)",
                            (spec.name, spec.geometry),
                        )
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
            else:
                have = {r["name"] for r in db.execute(f'PRAGMA table_info("{spec.name}")')}
                for c in spec.columns:
                    if c.name not in have:
                        db.execute(f'ALTER TABLE "{spec.name}" ADD COLUMN "{c.name}" {c.type}')
            self._ready.add(spec.name)

    def rows(self, spec: TableSpec, where: str = "", args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """The rows of a table (`geom` decoded), in the order they were added."""
        self.ensure(spec)
        with self._lock:
            cursor = self._db.execute(f'SELECT * FROM "{spec.name}"' + (f" WHERE {where}" if where else "") + " ORDER BY fid", tuple(args))
            out = []
            for r in cursor:
                row = dict(r)
                if "geom" in row:
                    row["geom"] = decode_geometry(row["geom"])
                out.append(row)
            return out

    def upsert(self, spec: TableSpec, rows: Iterable[dict[str, Any]]) -> None:
        """Adds the rows, or replaces those whose key is already there (one transaction)."""
        self.ensure(spec)
        rows = list(rows)
        if not rows:
            return
        if not spec.key:
            raise ValueError(f"{spec.name} にキーなし")
        with self._lock:
            db = self._db
            db.execute("BEGIN")
            try:
                for row in rows:
                    names = [c.name for c in spec.columns if c.name in row]
                    values = [row[n] for n in names]
                    if spec.geometry and "geom" in row:
                        names.append("geom")
                        values.append(row["geom"])
                    found = db.execute(f'SELECT fid FROM "{spec.name}" WHERE "{spec.key}" = ?', (row[spec.key],)).fetchone()
                    if found:
                        sets = ", ".join(f'"{n}" = ?' for n in names)
                        db.execute(f'UPDATE "{spec.name}" SET {sets} WHERE fid = ?', (*values, found["fid"]))
                    else:
                        cols = ", ".join(f'"{n}"' for n in names)
                        marks = ", ".join("?" for _ in names)
                        db.execute(f'INSERT INTO "{spec.name}" ({cols}) VALUES ({marks})', values)
                self._touch(spec)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def delete(self, spec: TableSpec, where: str, args: Sequence[Any] = ()) -> int:
        """Deletes rows; how many."""
        self.ensure(spec)
        with self._lock:
            cursor = self._db.execute(f'DELETE FROM "{spec.name}" WHERE {where}', tuple(args))
            self._touch(spec)
            return cursor.rowcount

    def _touch(self, spec: TableSpec) -> None:
        """Updates the table's extent and change time in gpkg_contents."""
        if spec.geometry:
            # The extent of all rows, from the blobs' points (cheap enough for local stores).
            xs: list[float] = []
            ys: list[float] = []
            for (blob,) in self._db.execute(f'SELECT geom FROM "{spec.name}"'):
                g = decode_geometry(blob)
                if isinstance(g, tuple):
                    xs.append(g[0])
                    ys.append(g[1])
                elif isinstance(g, list):
                    for r in g:
                        for p in r:
                            xs.append(p[0])
                            ys.append(p[1])
            if xs:
                self._db.execute(
                    "UPDATE gpkg_contents SET min_x = ?, min_y = ?, max_x = ?, max_y = ?, last_change = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name = ?",
                    (min(xs), min(ys), max(xs), max(ys), spec.name),
                )
                return
        self._db.execute("UPDATE gpkg_contents SET last_change = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name = ?", (spec.name,))


# ---------------------------------------------------------------------------
# The plugin's tables (attribute names as the Esri layers of the viewer)

OBSERVATIONS = TableSpec(
    "observations",
    "POINT",
    (
        Column("OBS_ID", "TEXT(40)"),
        Column("OBJECT_ID", "TEXT(40)"),
        Column("OBJECT_NAME", "TEXT(100)"),
        Column("IMAGE_ID", "TEXT(100)"),
        Column("IMAGE_NAME", "TEXT(255)"),
        Column("IMAGE_TIME", "DATETIME"),
        Column("KIND", "TEXT(255)"),
        Column("ACTIVITY", "TEXT(50)"),
        Column("PRESENT", "SMALLINT"),
        Column("CONFIDENCE", "TEXT(50)"),
        Column("LENGTH_M", "DOUBLE"),
        Column("HEADING", "DOUBLE"),
        Column("NOTE", "TEXT(1000)"),
        Column("OBSERVER", "TEXT(50)"),
        Column("UPDATED_BY", "TEXT(50)"),
        Column("CREATED_AT", "DATETIME"),
        Column("UPDATED_AT", "DATETIME"),
        Column("DELETED_AT", "DATETIME"),
    ),
    key="OBS_ID",
    description="観測記録",
)

AREAS = TableSpec(
    "checked_areas",
    "POLYGON",
    (
        Column("AREA_ID", "TEXT(40)"),
        Column("IMAGE_ID", "TEXT(100)"),
        Column("IMAGE_NAME", "TEXT(255)"),
        Column("IMAGE_TIME", "DATETIME"),
        Column("KIND", "TEXT(255)"),
        Column("NOTE", "TEXT(1000)"),
        Column("OBSERVER", "TEXT(50)"),
        Column("CREATED_AT", "DATETIME"),
        Column("UPDATED_AT", "DATETIME"),
        Column("DELETED_AT", "DATETIME"),
        # The observation store (its label) the area belongs to, as the viewer keeps local areas per store.
        Column("STORE", "TEXT(100)"),
    ),
    key="AREA_ID",
    description="確認範囲",
)

#: One row per class and store (the store's label in STORE: the local classification is kept per store, as in the viewer).
TAXONOMY = TableSpec(
    "taxonomy",
    None,
    (
        Column("ROW_KEY", "TEXT"),
        Column("STORE", "TEXT(100)"),
        Column("PATH", "TEXT(255)"),
        Column("LEVELS", "TEXT(255)"),
        Column("NAME_LABEL", "TEXT(50)"),
        Column("MEMBERS", "TEXT(4000)"),
        Column("SPECS", "TEXT"),
        Column("PHOTOS", "TEXT(4000)"),
        Column("SORT_ORDER", "INTEGER"),
    ),
    key="ROW_KEY",
    description="分類体系",
)

#: Reference photos added from files (`local:` ids in a class's photos).
PHOTOS = TableSpec(
    "taxon_photos",
    None,
    (
        Column("PHOTO_ID", "TEXT(40)"),
        Column("NAME", "TEXT(255)"),
        Column("MIME", "TEXT(50)"),
        Column("DATA", "BLOB"),
        Column("CREATED_AT", "DATETIME"),
    ),
    key="PHOTO_ID",
    description="分類の写真",
)

ALL_TABLES = (OBSERVATIONS, AREAS, TAXONOMY, PHOTOS)


def open_local(path: str) -> GeoPackage:
    """The plugin's local GeoPackage, with all its tables."""
    gpkg = GeoPackage(path)
    for spec in ALL_TABLES:
        gpkg.ensure(spec)
    return gpkg
