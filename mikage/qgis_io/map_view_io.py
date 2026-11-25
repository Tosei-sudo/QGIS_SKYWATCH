"""
地図の投影法と向きを開いた画像に合わせる, in QGIS (core/map_view.py has the
arithmetic and the reasons): how an opened image was taken (its catalog
record, the direction typed in クイック幾何補正, its file), the CRS to show
it in, the rotation that puts a true bearing at the top of the map (the
map's grid is not true north away from a projection's centre line), the
extent that fits images in a turned map (QGIS's setExtent does not mind the
rotation), and the map's turned view as a ring (searches over 「表示範囲」).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional, Sequence

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRectangle

from ..core.catalog.api import SAR, CatalogConfig, CatalogRecord, sensor_kind
from ..core.map_view import (
    Directions, bearing_of, fit_box, heading_from_look, off_nadir_of, utm_epsg,
)
from .geometry import WGS84

#: Metres per degree (as the other tools use them), for a small step along a bearing.
M_LAT = 110_574.0
M_LON = 111_320.0


def centre_of(layer, project: Optional[QgsProject] = None) -> Optional[tuple[float, float]]:
    """The middle of a layer's extent in WGS 84 (lon, lat), None without one."""
    extent = QgsRectangle(layer.extent())
    if extent.isNull() or not extent.isFinite():
        return None
    c = extent.center()
    crs = layer.crs()
    if crs.isValid() and crs != WGS84:
        try:
            c = QgsCoordinateTransform(crs, WGS84, project or QgsProject.instance()).transform(c)
        except Exception:  # noqa: BLE001 (QgsCsException)
            return None
    if not (math.isfinite(c.x()) and math.isfinite(c.y())) or abs(c.y()) > 90:
        return None
    return c.x(), c.y()


def utm_crs(lon: float, lat: float) -> QgsCoordinateReferenceSystem:
    return QgsCoordinateReferenceSystem(f"EPSG:{utm_epsg(lon, lat)}")


def crs_for(layer, centre: tuple[float, float], match: bool, turning: bool,
            current: QgsCoordinateReferenceSystem) -> Optional[QgsCoordinateReferenceSystem]:
    """
    The map's CRS for an image (None: the map's stays): with
    「投影法を画像に合わせる」 the image's when it is projected, else the UTM
    zone of its centre; without it, that zone only when the map is turned
    while in degrees (it would skew).
    """
    if match:
        crs = layer.crs()
        if crs.isValid() and not crs.isGeographic():
            return crs
        return utm_crs(*centre)
    if turning and (not current.isValid() or current.isGeographic()):
        return utm_crs(*centre)
    return None


def grid_bearing(crs: QgsCoordinateReferenceSystem, lon: float, lat: float, bearing: float,
                 project: Optional[QgsProject] = None) -> Optional[float]:
    """The bearing on the map's grid (`crs`) of a true bearing at a place."""
    d = 200.0
    a = math.radians(bearing)
    lat2 = lat + d * math.cos(a) / M_LAT
    lon2 = lon + d * math.sin(a) / (M_LON * max(1e-6, math.cos(math.radians(lat))))
    try:
        t = QgsCoordinateTransform(WGS84, crs, project or QgsProject.instance())
        p, q = t.transform(QgsPointXY(lon, lat)), t.transform(QgsPointXY(lon2, lat2))
    except Exception:  # noqa: BLE001 (QgsCsException)
        return None
    return bearing_of(q.x() - p.x(), q.y() - p.y())


# ---- how the image was taken ----------------------------------------------------------------------

def typed_toward(layer, keeper=None) -> Optional[float]:
    """The satellite's direction typed in クイック幾何補正 for the image: on its layer, else of the corrections read (keeper)."""
    from .geo_fix_io import fix_of

    try:
        fix = fix_of(layer)
    except Exception:  # noqa: BLE001 (a layer gone: none)
        fix = None
    if fix is None and keeper is not None:
        try:
            from .geo_fix_keeper import image_key

            fix = keeper.known(*image_key(layer))
        except Exception:  # noqa: BLE001 (a layer or keeper gone: none)
            fix = None
    if fix is None or fix.toward is None:
        return None
    return float(fix.toward) % 360


def file_directions(layer, radar: bool = False) -> dict[str, Any]:
    """
    What the image's file says of how it was taken (the metadata as クイック
    幾何補正 and 簡易オルソ補正 read it, and its rows and columns on the
    ground): {"toward", "off_nadir", "heading", "side", "rows", "radar"}; {}
    when the file can not be read.
    """
    from ..core.geo_fix import heading_from_text, look_side
    from ..core.simple_ortho import sensor_view_from_text
    from . import geo_fix_io
    from .complex_sar import COMPLEX_SOURCE
    from .shift_io import original_source

    try:
        base = geo_fix_io.open_base(original_source(layer).split("|")[0])
    except Exception:  # noqa: BLE001 (no placing, unreadable: nothing from the file)
        return {}
    box = base.footprint()
    lat = (box[1] + box[3]) / 2 if box else None
    look = None
    try:
        look = geo_fix_io.file_look(base)
    except Exception:  # noqa: BLE001
        look = None
    s1 = None
    try:
        s1 = geo_fix_io.sentinel1_look(getattr(base, "real", base.path))
    except Exception:  # noqa: BLE001
        s1 = None
    text = base.text or ""
    radar = bool(radar or layer.customProperty(COMPLEX_SOURCE) or look is not None or s1 is not None or "<SICD" in text)
    out: dict[str, Any] = {"radar": radar, "side": look_side(text)}
    view = sensor_view_from_text(text)
    if look is not None:
        out["toward"] = look.toward
        out["off_nadir"] = off_nadir_of(look.incidence, "incidence")
    elif view is not None:
        out["toward"] = view.azimuth
        out["off_nadir"] = off_nadir_of(view.elevation, "elevation")
    elif radar and base.how == "gcps":
        toward = base.column_direction()
        if toward is not None:
            out["toward"], out["from"] = toward, "columns"
    heading = heading_from_text(text, lat)
    if heading is not None:
        out["heading"] = heading
    if base.how in ("gcps", "rpc"):
        out["rows"] = _rows_way(base)
    return out


def _rows_way(base) -> Optional[float]:
    """The bearing on the ground of the way an image's rows go (down the image), at its middle."""
    import numpy as np

    cx, cy = base.width / 2, base.height / 2
    d = max(1.0, base.height / 100)
    lon, lat = base.ground(np.array([cx, cx]), np.array([cy - d, cy + d]))
    if not np.isfinite(lon).all() or not np.isfinite(lat).all():
        return None
    east = (lon[1] - lon[0]) * M_LON * math.cos(math.radians(float(lat.mean())))
    north = (lat[1] - lat[0]) * M_LAT
    return bearing_of(east, north)


def directions_of(layer, found: Optional[tuple[CatalogRecord, CatalogConfig]] = None, typed: Optional[float] = None,
                  orientation: str = "look", read_file: Callable[..., dict] = file_directions) -> Directions:
    """
    How the image of a layer was taken: the catalog's 方位角 and angle first,
    then the direction typed in クイック幾何補正, then its file (read only when
    those leave what `orientation` needs unknown).
    """
    toward = off = None
    toward_from = ""
    radar = False
    if found is not None:
        record, catalog = found
        if record.azimuth is not None and math.isfinite(record.azimuth):
            toward, toward_from = record.azimuth % 360, "catalog"
        off = off_nadir_of(record.angle, catalog.angle_kind, catalog.orbit_height)
        radar = sensor_kind(record.sensor or "", ()) == SAR
    if toward is None and typed is not None:
        toward, toward_from = typed % 360, "typed"
    heading = None
    heading_from = ""
    side = rows = None
    if orientation == "flight" or toward is None:
        f = read_file(layer, radar)
        radar = radar or bool(f.get("radar"))
        if toward is None and f.get("toward") is not None:
            toward, toward_from = float(f["toward"]) % 360, f.get("from", "file")
        if off is None:
            off = f.get("off_nadir")
        if f.get("heading") is not None:
            heading, heading_from = float(f["heading"]) % 360, "file"
        side, rows = f.get("side"), f.get("rows")
    # A radar's look turned to its track; the image's rows first when the look itself was the columns' (the same file's geometry).
    if heading is None and rows is not None and toward_from == "columns":
        heading, heading_from = rows, "rows"
    if heading is None and radar and toward is not None:
        heading, heading_from = heading_from_look(toward, side), "look"
    if heading is None and rows is not None:
        heading, heading_from = rows, "rows"
    return Directions(toward=toward, toward_from=toward_from, heading=heading, heading_from=heading_from, off_nadir=off)


# ---- the map --------------------------------------------------------------------------------------

def _outline(extent: QgsRectangle, n: int = 8) -> list[QgsPointXY]:
    """Points along a box's edges (a box turns into a curve in another CRS)."""
    x0, y0, x1, y1 = extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()
    out = []
    for i in range(n):
        t = i / n
        out += [QgsPointXY(x0 + (x1 - x0) * t, y0), QgsPointXY(x1, y0 + (y1 - y0) * t),
                QgsPointXY(x1 - (x1 - x0) * t, y1), QgsPointXY(x0, y1 - (y1 - y0) * t)]
    return out


def fitted_extent(layers: Sequence, crs: QgsCoordinateReferenceSystem, rotation: float, margin: float = 1.05,
                  project: Optional[QgsProject] = None) -> Optional[QgsRectangle]:
    """
    The extent to set for the layers to fill a map turned by `rotation` (in
    `crs`, before the rotation, as QGIS takes it), with a margin; None when
    none has an extent that can be shown there.
    """
    return fitted_boxes([(QgsRectangle(layer.extent()), layer.crs()) for layer in layers], crs, rotation, margin, project)


def fitted_boxes(boxes: Sequence[tuple[QgsRectangle, QgsCoordinateReferenceSystem]], crs: QgsCoordinateReferenceSystem,
                 rotation: float, margin: float = 1.05, project: Optional[QgsProject] = None) -> Optional[QgsRectangle]:
    """fitted_extent for boxes, each in its CRS (an invalid one: the map's)."""
    project = project or QgsProject.instance()
    points = []
    for extent, own in boxes:
        if extent.isNull() or not extent.isFinite():
            continue
        pts = _outline(extent)
        if own.isValid() and crs.isValid() and own != crs:
            t = QgsCoordinateTransform(own, crs, project)
            moved = []
            for p in pts:
                try:
                    moved.append(t.transform(p))
                except Exception:  # noqa: BLE001 (QgsCsException: outside what the map's CRS shows)
                    continue
            pts = moved
        points += [(p.x(), p.y()) for p in pts]
    box = fit_box(points, rotation, margin)
    if box is None:
        return None
    cx, cy, w, h = box
    return QgsRectangle(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def visible_ring(canvas) -> Optional[list[list[float]]]:
    """The map's view as it is (turned) in WGS 84, a closed ring of [lon, lat]; None when it can not be shown in degrees."""
    settings = canvas.mapSettings()
    polygon = settings.visiblePolygon()
    try:
        t = QgsCoordinateTransform(settings.destinationCrs(), WGS84, QgsProject.instance())
        pts = [t.transform(QgsPointXY(p.x(), p.y())) for p in polygon]
    except Exception:  # noqa: BLE001 (QgsCsException)
        return None
    ring = [[p.x(), p.y()] for p in pts]
    if len(ring) < 3 or not all(math.isfinite(v) for p in ring for v in p):
        return None
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring
