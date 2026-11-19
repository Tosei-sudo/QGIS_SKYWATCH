"""
「保存した範囲」 (tsudo, 2026-11-19): areas drawn for the image catalog's
search kept under a name and chosen again later from 「範囲」.

- Kept per user as the list views are (QgsSettings `mikage/catalog/savedAreas`, see
  qgis_io/settings.py), not in the settings file, the project or Esri: a
  search area is one person's shortcut, it needs no table on the server and
  works without a network.
- Newest first. Saving under a name already used replaces that area (it
  moves to the top); at most `LIMIT` are kept (the oldest go).
- The corners are WGS 84 (longitude, latitude), the ring closed.

Pure Python (no QGIS), so the reading and writing are tested on their own.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from typing import Optional, Sequence

#: The most areas kept (each a few corners; the oldest beyond go).
LIMIT = 300


@dataclass(frozen=True)
class SavedArea:
    id: str
    name: str
    #: The corners (longitude, latitude), the ring closed.
    ring: tuple
    #: When it was saved, local time "2026-11-19 12:43".
    saved: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "ring": [list(p) for p in self.ring], "saved": self.saved}


def closed_ring(corners: Sequence[Sequence[float]]) -> tuple:
    """The corners as a closed ring of (lon, lat) tuples (repeated corners in a row dropped)."""
    ring: list = []
    for p in corners:
        point = (float(p[0]), float(p[1]))
        if not ring or ring[-1] != point:
            ring.append(point)
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return tuple(ring)


def _area(item) -> Optional[SavedArea]:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    try:
        ring = closed_ring(item.get("ring") or [])
    except (TypeError, ValueError, IndexError):
        return None
    # A polygon: three corners at least (four points with the first again).
    if not name or len(ring) < 4:
        return None
    return SavedArea(str(item.get("id") or uuid.uuid4().hex), name, ring, str(item.get("saved") or ""))


def read_areas(text: object) -> list[SavedArea]:
    """The areas kept (as `write_areas` wrote them); anything unreadable is left out, never an error."""
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("areas") if isinstance(data, dict) else None
    areas = [a for a in (_area(i) for i in (items or [])) if a is not None]
    seen: set = set()
    unique = []
    for a in areas:
        if a.id not in seen:
            seen.add(a.id)
            unique.append(a)
    return unique[:LIMIT]


def write_areas(areas: Sequence[SavedArea]) -> str:
    return json.dumps({"areas": [a.to_json() for a in list(areas)[:LIMIT]]}, ensure_ascii=False)


def find(areas: Sequence[SavedArea], area_id: Optional[str]) -> Optional[SavedArea]:
    return next((a for a in areas if a.id == area_id), None)


def by_name(areas: Sequence[SavedArea], name: str) -> Optional[SavedArea]:
    name = name.strip()
    return next((a for a in areas if a.name == name), None)


def add_area(areas: Sequence[SavedArea], name: str, corners: Sequence[Sequence[float]], saved: str) -> tuple[list[SavedArea], SavedArea]:
    """
    The areas with a new one first (a name already used: that one replaced,
    keeping its id so a view that chose it still finds it). ValueError for
    no name or fewer than three corners.
    """
    name = name.strip()
    if not name:
        raise ValueError("範囲の名前を入れる")
    ring = closed_ring(corners)
    if len(ring) < 4:
        raise ValueError("頂点が 3 つ以上の範囲だけ保存できる")
    old = by_name(areas, name)
    area = SavedArea(old.id if old else uuid.uuid4().hex, name, ring, saved)
    rest = [a for a in areas if a.id != area.id]
    return ([area] + rest)[:LIMIT], area


def rename_area(areas: Sequence[SavedArea], area_id: str, name: str) -> list[SavedArea]:
    """The areas with one renamed (in its place). ValueError for no name or a name another area has."""
    name = name.strip()
    if not name:
        raise ValueError("範囲の名前を入れる")
    other = by_name(areas, name)
    if other is not None and other.id != area_id:
        raise ValueError(f"「{name}」はほかの範囲の名前")
    return [replace(a, name=name) if a.id == area_id else a for a in areas]


def delete_area(areas: Sequence[SavedArea], area_id: str) -> list[SavedArea]:
    return [a for a in areas if a.id != area_id]
