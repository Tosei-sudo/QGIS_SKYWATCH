"""
画像の評価 in QGIS (core/image_reviews.py): the named areas an image shows,
each cut by the image's footprint (the shape of its row), how much of each
the 不可範囲 cover (the window's first ratings), and the records in each
(the window lists the named areas recorded in first).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from qgis.core import QgsGeometry, QgsPointXY

from ..core.esri import EsriClient
from ..core.named_areas import NamedAreaSettings, area_of
from ..core.shapes import esri_rings
from .geometry import rings_geometry

#: Named areas over an image read at most (a coast of many small harbours; more are left out and said so).
MOST = 500


@dataclass
class AreaChoice:
    """A named area the image shows: its 管理番号 and name, the part shown (WGS 84 rings) and what is in it."""
    area_id: str
    name: str
    kind: str = ""
    rings: list = field(default_factory=list, repr=False)
    #: The part shown (km², roughly), the smaller the more precise.
    size: float = 0.0
    #: The image's records inside it.
    records: int = 0
    #: The share of it the 不可範囲 cover, and their reasons.
    unseen: float = 0.0
    reasons: tuple = ()


def footprint_of(image_id: str, box: Optional[Sequence[float]], listed: Sequence = ()) -> Optional[list]:
    """An image's footprint rings: the catalog's (an image listed without its file), else its box; None when not known."""
    for x in listed:
        if getattr(x, "id", None) == image_id and getattr(x, "rings", None):
            return [list(map(list, r)) for r in x.rings]
    if box and len(box) == 4:
        w, s, e, n = box
        return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
    return None


def geometry_rings(g: QgsGeometry) -> list:
    """The rings of a (multi)polygon geometry as [[longitude, latitude], …] lists ([] for an empty one)."""
    if g is None or g.isEmpty():
        return []
    polygons = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
    return [[[p.x(), p.y()] for p in ring] for polygon in polygons for ring in polygon if len(ring) >= 3]


def _km2(g: QgsGeometry) -> float:
    """A WGS 84 geometry's area, roughly (degrees² scaled by the latitude)."""
    if g is None or g.isEmpty():
        return 0.0
    lat = g.centroid().asPoint().y() if not g.centroid().isEmpty() else 0.0
    return g.area() * 111.32 * 111.32 * max(0.01, math.cos(math.radians(lat)))


def areas_over(client: EsriClient, settings: NamedAreaSettings, footprint: Sequence, token: Optional[str] = None,
               most: int = MOST) -> tuple[list[tuple], bool]:
    """
    The active named areas crossing a footprint (each (OrgArea, rings)), of
    every use (地名・関心地域・両方), and whether more were left out. Asked by
    the footprint (the layer's spatial index), not read whole.
    """
    source = settings.areas
    token = token or source.token
    geometry = {"rings": esri_rings(footprint), "spatialReference": {"wkid": 4326}}
    rows = client.query_all(source.url, source.where or "1=1", token, geometry=True, limit=most + 1, cut=True, extra={
        "geometry": json.dumps(geometry), "geometryType": "esriGeometryPolygon", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects"})
    oid = client.layer(source.url, token).object_id_field
    out = []
    for row in rows[:most]:
        rings = (row.get("geometry") or {}).get("rings") or []
        area = area_of(row.get("attributes") or {}, source.fields, oid)
        if rings and area.active and (area.name or area.id):
            out.append((area, rings))
    return out, len(rows) > most


def choices_of(found: Sequence[tuple], footprint: Sequence, points: Sequence[tuple] = (),
               unseen: Sequence[tuple] = ()) -> list[AreaChoice]:
    """
    The named areas as the window lists them: each cut by the footprint
    (those it only touches left out), with the records inside and the share
    the 不可範囲 (`unseen`: (rings, reason) each) cover; the named areas
    recorded in first, then the smaller first.
    """
    foot = rings_geometry(footprint).makeValid()
    blocked = [(rings_geometry(r).makeValid(), reason) for r, reason in unseen]
    out = []
    for area, rings in found:
        part = rings_geometry(rings).makeValid().intersection(foot)
        size = _km2(part)
        if part.isEmpty() or size <= 0:
            continue
        inside = sum(1 for lon, lat in points if part.contains(QgsGeometry.fromPointXY(QgsPointXY(lon, lat))))
        share, reasons = unseen_share(part, blocked)
        out.append(AreaChoice(area.id, area.name or area.id, area.kind, geometry_rings(part), size, inside, share, reasons))
    out.sort(key=lambda c: (c.records == 0, c.size, c.name.casefold()))
    return out


def unseen_share(part: QgsGeometry, blocked: Sequence[tuple]) -> tuple[float, tuple]:
    """How much of `part` the 不可範囲 cover (0–1), and the reasons of those over it."""
    total = part.area()
    if total <= 0 or not blocked:
        return 0.0, ()
    over = [(g, reason) for g, reason in blocked if g.intersects(part)]
    if not over:
        return 0.0, ()
    union = QgsGeometry.unaryUnion([g for g, _r in over]).intersection(part)
    return min(1.0, union.area() / total), tuple(dict.fromkeys(r for _g, r in over if r))


def whole_unseen(footprint: Sequence, unseen: Sequence[tuple]) -> tuple[float, tuple]:
    """The share of the footprint the 不可範囲 cover, and their reasons (the 画像全体 row's first rating)."""
    return unseen_share(rings_geometry(footprint).makeValid(), [(rings_geometry(r).makeValid(), reason) for r, reason in unseen])
