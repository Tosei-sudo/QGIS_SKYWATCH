"""
「類似画像の検索」 (the viewer's catalog-similar.ts): images of a catalog taken
like a given one, for comparison: the same sensor, an angle and an azimuth
within a tolerance, and a footprint over the same ground. The catalog is
asked for the sensor and the angles (over the footprint's extent); how much
of the footprint each one covers is measured by the caller (QGIS geometry),
and the list goes from the most alike.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Optional, Sequence

from ..util import is_number
from .config import CatalogConfig
from .query import EMPTY_SEARCH, CatalogSearch
from .search import CatalogRecord


@dataclass(frozen=True)
class SimilarTolerance:
    """How alike the images must be."""
    #: Only the reference's sensor.
    same_sensor: bool = True
    #: Degrees either side of the reference's angle; None for any angle.
    angle: Optional[float] = 5
    #: Degrees either side of the reference's azimuth (through north); None for any azimuth.
    azimuth: Optional[float] = 20
    #: The least share of the reference's footprint an image must cover, 0 to 1 (0: any overlap).
    min_overlap: float = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(value: Any) -> "SimilarTolerance":
        d = value if isinstance(value, dict) else {}

        def number(v: Any) -> Optional[float]:
            return float(v) if is_number(v) and v >= 0 else None

        return SimilarTolerance(
            same_sensor=d.get("same_sensor") is not False,
            angle=number(d["angle"]) if "angle" in d else DEFAULT_TOLERANCE.angle,
            azimuth=number(d["azimuth"]) if "azimuth" in d else DEFAULT_TOLERANCE.azimuth,
            min_overlap=min(1.0, number(d.get("min_overlap")) or 0.0),
        )


DEFAULT_TOLERANCE = SimilarTolerance()


def _round(v: float) -> float:
    return math.floor(v * 1e6 + 0.5) / 1e6


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def similar_search(catalog: CatalogConfig, reference: CatalogRecord, tolerance: SimilarTolerance,
                   base: CatalogSearch = EMPTY_SEARCH) -> CatalogSearch:
    """The catalog search for images like `reference` (dates and SQL of `base` kept)."""
    f = catalog.fields
    search = replace(base, sensor="", max_angle=None, azimuth_from=None, azimuth_to=None)
    if f.get("sensor") and tolerance.same_sensor and reference.sensor:
        search = replace(search, sensor=reference.sensor)
    parts: list[str] = []
    if f.get("angle") and is_number(reference.angle) and is_number(tolerance.angle) and tolerance.angle >= 0:
        parts += [f"{f['angle']} >= {_num(_round(reference.angle - tolerance.angle))}",
                  f"{f['angle']} <= {_num(_round(reference.angle + tolerance.angle))}"]
    if f.get("azimuth") and is_number(reference.azimuth) and is_number(tolerance.azimuth) and 0 <= tolerance.azimuth < 180:
        # From > to goes through north (where_of writes it as an OR).
        search = replace(search, azimuth_from=_round((reference.azimuth - tolerance.azimuth) % 360),
                         azimuth_to=_round((reference.azimuth + tolerance.azimuth) % 360))
    if base.where.strip():
        parts.append(f"({base.where.strip()})")
    return replace(search, where=" AND ".join(parts))


def azimuth_difference(a: float, b: float) -> float:
    """The difference of two directions in degrees, 0 to 180."""
    d = abs((a - b) % 360)
    return 360 - d if d > 180 else d


@dataclass
class SimilarImage:
    """One image found, with how much of the reference it covers (None when it could not be measured)."""
    record: CatalogRecord
    overlap: Optional[float]
    #: How unlike the reference it is: 0 is the same; the list goes from the smallest.
    distance: float


def _same(r: CatalogRecord, reference: CatalogRecord) -> bool:
    return (r is reference
            or (r.object_id is not None and r.object_id == reference.object_id and r.attributes == reference.attributes)
            or (reference.id != "" and r.id == reference.id)
            or (reference.source is not None and r.source == reference.source))


def rank_similar(reference: CatalogRecord, records: Sequence[CatalogRecord], tolerance: SimilarTolerance,
                 overlap_of: Callable[[CatalogRecord], Optional[float]]) -> list[SimilarImage]:
    """
    The images like `reference` among `records`, the most alike first: the
    reference itself and those covering less than `min_overlap` (or nothing)
    of it left out. `overlap_of` measures the share of the reference an
    image's footprint covers.
    """
    found: list[SimilarImage] = []
    for record in records:
        if _same(record, reference):
            continue
        overlap = overlap_of(record)
        if overlap is not None and (overlap <= 0 or overlap < tolerance.min_overlap):
            continue
        # Each difference against its tolerance (or a natural scale without one), and the ground not shared.
        distance = 1.0 if overlap is None else 1 - overlap
        if reference.angle is not None and record.angle is not None:
            scale = tolerance.angle if is_number(tolerance.angle) and tolerance.angle > 0 else 10
            distance += abs(record.angle - reference.angle) / scale
        if reference.azimuth is not None and record.azimuth is not None:
            scale = tolerance.azimuth if is_number(tolerance.azimuth) and tolerance.azimuth > 0 else 45
            distance += azimuth_difference(record.azimuth, reference.azimuth) / scale
        if reference.sensor and record.sensor != reference.sensor:
            distance += 1
        found.append(SimilarImage(record, overlap, distance))
    return sorted(found, key=lambda s: s.distance)
