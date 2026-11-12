"""
Records of many alike things (tsudo, 2026-11-09: 「大量の同じモノが並んでる
とき、観測結果作るの大変」): one thing recorded as the 見本 (sample), the
rest put down with its 種別・活動・確度 and size.

- 連続記録 (stamps): a double click records one where it is, as the 見本
  lies; a press and drag records one turned to the drag (the press is its
  centre, the drag points to its front).
- 列・格子 (rows): the two ends of a row and how many (and, for a grid,
  where the last row starts and how many rows), evenly between them.

Names are given in turn (乗用車-12…) by ObservationBook.add_many. Places
here are longitude / latitude; a row or grid is laid out on a flat plane
round its first place (the few hundred metres of a row or a car park).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .geo import RADIUS, bearing, get_distance, km_text
from .taxonomy import path_of

#: How the things of a row lie (「向き」): as the 見本, across the row, along it.
ROW_HEADINGS = ("sample", "across", "along")
#: The largest row or grid made at once.
MOST = 500


@dataclass
class Sample:
    """What the things recorded after it are given: the 見本."""
    kind: str
    activity: str = ""
    confidence: str = ""
    #: m, and degrees from north (rear to front), when the 見本 was measured.
    length: Optional[float] = None
    width: Optional[float] = None
    heading: Optional[float] = None
    #: The record it was taken from ('' for the last values of the form only), its name, image and place.
    record_id: str = ""
    name: str = ""
    image_id: str = ""
    lon: Optional[float] = None
    lat: Optional[float] = None

    @property
    def measured(self) -> bool:
        return self.length is not None and self.length > 0 and self.heading is not None


def sample_of(record: Any) -> "Sample":
    """A record as the 見本 (its own name is not given to the others)."""
    return Sample(kind=record.kind, activity=record.activity if record.present else "", confidence=record.confidence or "",
                  length=record.length, width=record.width, heading=record.heading, record_id=record.id,
                  name=record.object_name, image_id=record.image_id, lon=record.lon, lat=record.lat)


def size_text(length: Optional[float], width: Optional[float]) -> str:
    """`4.5 × 1.8 m`, `120 m`, '' (m to the tenth below 100 m)."""
    def m(v: float) -> str:
        return f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else km_text(v)[:-2]

    if length is None or not length > 0:
        return ""
    if width is not None and width > 0:
        return f"{m(length)} × {m(width)} m"
    return f"{m(length)} m"


def sample_text(s: Optional[Sample]) -> str:
    """The 見本 as the dock says it: `乗用車-3（乗用車・駐車・推定・4.5 × 1.8 m・045°）`."""
    if s is None:
        return "見本なし（1 件をフォームで記録すると見本に）"
    kind = (path_of(s.kind) or ["種別なし"])[-1]
    parts = [kind, s.activity, s.confidence, size_text(s.length, s.width),
             "" if s.heading is None else f"{math.floor(s.heading + 0.5) % 360:03d}°"]
    said = "・".join(p for p in parts if p)
    return f"{s.name}（{said}）" if s.name else said


@dataclass
class StampEntry:
    """One thing to record as the 見本 (what ObservationBook.add_many takes)."""
    lon: float
    lat: float
    object: str
    kind: str
    activity: str
    confidence: str
    note: str
    length: Optional[float]
    heading: Optional[float]
    width: Optional[float] = None
    carry: Any = None


def entry_of(sample: Sample, lon: float, lat: float, heading: Optional[float] = None, note: str = "") -> StampEntry:
    """A thing at a place as the 見本 (turned to `heading` when given)."""
    h = sample.heading if heading is None else heading % 360
    return StampEntry(lon=lon, lat=lat, object="", kind=sample.kind, activity=sample.activity, confidence=sample.confidence,
                      note=note, length=sample.length, heading=h, width=sample.width)


# ---- rows and grids ----------------------------------------------------------


def _metres(origin: Sequence[float], p: Sequence[float]) -> tuple[float, float]:
    """A place as metres east and north of `origin` (flat, round it)."""
    k = math.pi / 180 * RADIUS
    return (p[0] - origin[0]) * k * math.cos(math.radians(origin[1])), (p[1] - origin[1]) * k


def _place(origin: Sequence[float], east: float, north: float) -> tuple[float, float]:
    k = math.pi / 180 * RADIUS
    return origin[0] + east / (k * max(math.cos(math.radians(origin[1])), 1e-9)), origin[1] + north / k


def row_places(a: Sequence[float], b: Sequence[float], n: int) -> list[tuple[float, float]]:
    """`n` places from `a` to `b`, the ends included."""
    n = max(1, min(int(n), MOST))
    if n == 1:
        return [(a[0], a[1])]
    be, bn = _metres(a, b)
    return [_place(a, be * i / (n - 1), bn * i / (n - 1)) for i in range(n)]


def grid_places(a: Sequence[float], b: Sequence[float], c: Sequence[float], columns: int, rows: int) -> list[tuple[float, float]]:
    """
    A grid: `columns` places along each row (the first from `a` to `b`) and
    `rows` rows, the last starting at `c` (rows parallel to the first: a
    parallelogram, for a car park drawn askew). Row by row from `a`.
    """
    columns = max(1, int(columns))
    rows = max(1, min(int(rows), max(1, MOST // columns)))
    be, bn = _metres(a, b)
    ce, cn = _metres(a, c)
    out = []
    for j in range(rows):
        t = j / (rows - 1) if rows > 1 else 0.0
        for i in range(columns):
            s = i / (columns - 1) if columns > 1 else 0.0
            out.append(_place(a, be * s + ce * t, bn * s + cn * t))
    return out


def _gap(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def row_heading(how: str, row_bearing: float, sample_heading: Optional[float]) -> Optional[float]:
    """
    The heading of the things of a row: 'sample' as the 見本; 'along' or
    'across' the row, of its two ways the one nearer the 見本's (else the row's
    own way, or its right).
    """
    if how == "along":
        ways = [row_bearing % 360, (row_bearing + 180) % 360]
    elif how == "across":
        ways = [(row_bearing + 90) % 360, (row_bearing + 270) % 360]
    else:
        return sample_heading
    if sample_heading is None:
        return ways[0]
    return min(ways, key=lambda w: _gap(w, sample_heading))


def extent_along(length: Optional[float], width: Optional[float], heading: Optional[float], direction: float) -> Optional[float]:
    """How far (m) a thing reaches along a direction (its outline's shadow on that line); None unmeasured."""
    if length is None or not length > 0 or heading is None:
        return None
    t = math.radians(heading - direction)
    return abs(length * math.cos(t)) + abs((width if width and width > 0 else 0.0) * math.sin(t))


def default_count(a: Sequence[float], b: Sequence[float], sample: Optional[Sample], heading: Optional[float]) -> int:
    """
    How many fit from `a` to `b`, the things as long as the 見本 along the line
    with a tenth more between them; 2 when the 見本 has no size.
    """
    d = get_distance(a, b)
    reach = extent_along(sample.length if sample else None, sample.width if sample else None, heading, bearing(a, b)) if d > 0 else None
    if not reach:
        return 2
    return max(2, min(MOST, round(d / (reach * 1.1)) + 1))


@dataclass
class Layout:
    """A row or grid being laid out: the places clicked, how many, how they lie, the places left out."""
    #: 'row' or 'grid'.
    shape: str = "row"
    corners: tuple = ()
    columns: int = 0
    rows: int = 2
    how: str = "sample"
    #: Indexes of the places left out (a car missing from its bay).
    skipped: frozenset = frozenset()

    @property
    def needs(self) -> int:
        """The clicks it takes: two for a row, three for a grid."""
        return 3 if self.shape == "grid" else 2

    @property
    def ready(self) -> bool:
        return len(self.corners) >= self.needs

    def heading(self, sample: Optional[Sample]) -> Optional[float]:
        if len(self.corners) < 2:
            return sample.heading if sample else None
        return row_heading(self.how, bearing(self.corners[0], self.corners[1]), sample.heading if sample else None)

    def places(self, cursor: Optional[Sequence[float]] = None) -> list[tuple[float, float]]:
        """Every place (the pointer standing in for the corner not clicked yet), the left-out ones too."""
        corners = list(self.corners)
        if not self.ready and cursor is not None:
            corners.append(tuple(cursor))
        if len(corners) < 2:
            return []
        columns = max(1, self.columns)
        if self.shape == "grid" and len(corners) >= 3:
            return grid_places(corners[0], corners[1], corners[2], columns, max(1, self.rows))
        return row_places(corners[0], corners[1], columns)

    def kept(self) -> list[tuple[float, float]]:
        return [p for i, p in enumerate(self.places()) if i not in self.skipped]

    def spacing(self) -> tuple[Optional[float], Optional[float]]:
        """The distances (m) between the places of a row and between the rows."""
        if len(self.corners) < 2:
            return None, None
        along = get_distance(self.corners[0], self.corners[1]) / (self.columns - 1) if self.columns > 1 else None
        across = None
        if self.shape == "grid" and len(self.corners) >= 3 and self.rows > 1:
            across = get_distance(self.corners[0], self.corners[2]) / (self.rows - 1)
        return along, across
