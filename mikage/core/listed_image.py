"""
An image recorded on without its file (one outside the catalog, or not opened here) and its footprint's
rings: what 観測記録 keeps of it and the image catalog gives for it (「ファイルなしで記録」). Shared base:
the catalog makes them, the observations keep them (docs/architecture.md).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from .util import is_number


#: The viewer's names of the record's values (for JSON shared with the viewer and its scripts).
@dataclass
class ListedImage:
    """An image recorded on without its file: one outside the catalog, or not opened here (id, name, time, footprint typed)."""
    id: str
    name: str
    time: Optional[int] = None
    #: [west, south, east, north] (WGS 84), when known.
    box: Optional[list[float]] = None
    #: The footprint's rings ([[longitude, latitude], …] each, WGS 84) as the catalog gives it, when known: drawn as the
    #: image's layer on the map (its extent is `box`). None for one typed with ＋ (the box alone).
    rings: Optional[list[list[list[float]]]] = None
    #: 衛星名: the catalog's 「センサー」 (「ファイルなしで記録」), else typed for it; '' when not known.
    satellite: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "name": self.name, "time": self.time, "box": self.box}
        if self.rings:
            out["rings"] = self.rings
        if self.satellite:
            out["satellite"] = self.satellite
        return out


def rings_of(value: Any) -> Optional[list[list[list[float]]]]:
    """
    A footprint's rings read back (or from a catalog's Esri polygon): rings
    of at least three [longitude, latitude] points, each point's first two
    numbers; None when there is none (or one is not such a ring).
    """
    if not isinstance(value, list) or not value:
        return None
    rings = []
    for ring in value:
        if not isinstance(ring, list):
            return None
        points = []
        for p in ring:
            if not (isinstance(p, (list, tuple)) and len(p) >= 2 and all(is_number(n) and not isinstance(n, bool) and math.isfinite(n)
                                                                         for n in p[:2])):
                return None
            points.append([float(p[0]), float(p[1])])
        if len(points) < 3:
            return None
        rings.append(points)
    return rings
