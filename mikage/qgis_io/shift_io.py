"""
Moving an image on the map (the viewer's 位置ずらし, shift.ts): a GDAL raster
layer is moved by `dx, dy` (its CRS's units) from where its file puts it,
without reading or copying the image. The layer is pointed at a small VRT of
its file with a moved georeferencing (in the plugin's folder of the QGIS
profile), keeping its style; its own file and the move are kept in custom
properties, so moving again starts from the file and no move gives the file back.

Everything that reads the layer's source (Processing, export, the plugin's
tools) gets the moved image, as the viewer's saving does.

The move is made where the file keeps its place on the ground, as QGIS
places it: its geotransform; else its GCPs (SAR and other images placed by
corner points; the layer's CRS is theirs); else its RPC model (offsets in
degrees, the layer is in WGS 84); else QGIS's pixel grid (y down). Writing a
geotransform into an image placed by GCPs or RPC made QGIS place it by that
alone, 1-unit pixels from (0, 0) (1° in WGS 84), so the whole map was one
magnified gray pixel until the move was taken back (tsudo 2026-11-18).
The layer keeps its CRS (one set by hand too) and its correction's state
(the view and a 範囲を固定 range) across the move.
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from osgeo import gdal

from ..core.errors import FileFailed

SOURCE = "satobs/shift_source"
SHIFT = "satobs/shift"


def shifts_folder() -> str:
    """Where the moved VRTs are written."""
    from .qsettings import profile_folder

    folder = profile_folder("shifted")
    os.makedirs(folder, exist_ok=True)
    return folder


def original_source(layer) -> str:
    """The layer's own source, before any move."""
    return str(layer.customProperty(SOURCE) or "") or layer.source()


def get_shift(layer) -> tuple[float, float]:
    """How far the layer has been moved, in its CRS's units ((0, 0) when never)."""
    value = layer.customProperty(SHIFT)
    try:
        dx, dy = (float(v) for v in str(value).split(","))
        return dx, dy
    except (TypeError, ValueError):
        return 0.0, 0.0


def can_shift(layer) -> bool:
    from qgis.core import QgsRasterLayer

    return isinstance(layer, QgsRasterLayer) and layer.providerType() == "gdal" and layer.isValid()


#: How QGIS places an image with no georeferencing at all: pixel columns east, rows down from (0, 0).
PIXEL_GRID = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)


def placed_by(ds) -> str:
    """What places `ds` on the map in QGIS: "geotransform", "gcps", "rpc" or "pixels" (nothing)."""
    if ds.GetGeoTransform(can_return_null=True) is not None:
        return "geotransform"
    if ds.GetGCPCount():
        return "gcps"
    if ds.GetMetadata("RPC"):
        return "rpc"
    return "pixels"


def move_dataset(ds, dx: float, dy: float) -> None:
    """Moves `ds` (writable, a VRT) by dx, dy in the units of what places it (see `placed_by`)."""
    how = placed_by(ds)
    if how == "gcps":
        gcps = [gdal.GCP(g.GCPX + dx, g.GCPY + dy, g.GCPZ, g.GCPPixel, g.GCPLine, g.Info, g.Id) for g in ds.GetGCPs()]
        ds.SetGCPs(gcps, ds.GetGCPProjection())
    elif how == "rpc":
        # RPC offsets are WGS 84 degrees: the model moved on the ground (QGIS shows such a layer in EPSG:4326).
        for name, d in (("LONG_OFF", dx), ("LAT_OFF", dy)):
            # (Some files give a unit after the number.)
            ds.SetMetadataItem(name, repr(float(str(ds.GetMetadataItem(name, "RPC")).split()[0]) + d), "RPC")
    else:
        gt = list(ds.GetGeoTransform() if how == "geotransform" else PIXEL_GRID)
        gt[0] += dx
        gt[3] += dy
        ds.SetGeoTransform(gt)


def moved_vrt(source: str, dx: float, dy: float, folder: Optional[str] = None) -> str:
    """A VRT of `source` moved by dx, dy; its path."""
    path = os.path.join(folder or shifts_folder(), f"{uuid.uuid4().hex}.vrt")
    try:
        ds = gdal.Translate(path, source.split("|")[0], format="VRT")
    except RuntimeError as e:  # GDAL with exceptions on
        raise FileFailed(code="E503", detail=f"{source}：{e}") from e
    if ds is None:
        raise FileFailed(code="E503", detail=source)
    try:
        move_dataset(ds, dx, dy)
    except (RuntimeError, TypeError, ValueError) as e:
        ds = None
        try:
            os.remove(path)
        except OSError:
            pass
        raise FileFailed("画像の位置を動かせない", code="E508", detail=f"{source}：{e}") from e
    ds = None
    return path


def set_shift(layer, dx: float, dy: float, folder: Optional[str] = None) -> None:
    """Moves the layer to dx, dy (its CRS's units) from where its own file puts it; (0, 0) gives the file back."""
    from . import geo_fix_io

    if geo_fix_io.is_fixed(layer):
        # Corrected by クイック幾何補正: the shift is part of its warp.
        geo_fix_io.reshift(layer, dx, dy)
        return
    source = original_source(layer)
    if not dx and not dy:
        target = source
    else:
        target = moved_vrt(source, dx, dy, folder)
    point_at(layer, target, source, (dx, dy), folder=folder)


#: Qgis.RasterColorInterpretation.AlphaBand.
ALPHA_BAND = 6


def _alpha_as_source(layer) -> None:
    """
    The renderer's alpha band as the new source has one: QGIS shows an image
    placed by GCPs warped with an alpha band (its last), a corrected image's
    VRT (クイック幾何補正) has none (outside it: no data).
    """
    renderer = layer.renderer()
    provider = layer.dataProvider()
    if renderer is None or provider is None or not hasattr(renderer, "alphaBand"):
        return
    n = provider.bandCount()
    try:
        alpha = n if n > 1 and int(provider.colorInterpretation(n)) == ALPHA_BAND else -1
    except (TypeError, ValueError):
        return
    now = renderer.alphaBand()
    if now != alpha and (now > n or (now < 1 and alpha > 0)):
        renderer.setAlphaBand(alpha)


def point_at(layer, target: str, source: str, shift: tuple, properties: Optional[dict] = None, folder: Optional[str] = None) -> None:
    """
    Points the layer at `target` (a VRT of `source`, its own file; or the
    file itself), with its shift and other custom properties (None removes
    one), keeping its CRS and its correction's state; the VRT shown before,
    when one of ours, is deleted.
    """
    from qgis.core import QgsDataProvider

    from . import enhance_renderer as er
    from . import geo_fix_io

    dx, dy = shift
    old = layer.source() if layer.customProperty(SOURCE) else ""
    crs = layer.crs()
    corrected = er.enhance_renderer(layer)
    state = corrected.state if corrected is not None else None
    # QGIS keeps the layer's style across a new source (the correction as its fallback renderer, put back below).
    layer.setDataSource(target, layer.name(), "gdal", QgsDataProvider.ProviderOptions())
    if crs.isValid() and layer.crs() != crs:
        # A CRS set by hand (the file has none, or another) is the layer's, not the file's.
        layer.setCrs(crs)
    _alpha_as_source(layer)
    for key, value in (properties or {}).items():
        if value is None:
            layer.removeCustomProperty(key)
        else:
            layer.setCustomProperty(key, value)
    if target == source:
        layer.removeCustomProperty(SOURCE)
        layer.removeCustomProperty(SHIFT)
    else:
        layer.setCustomProperty(SOURCE, source)
        if dx or dy:
            layer.setCustomProperty(SHIFT, f"{dx!r},{dy!r}")
        else:
            layer.removeCustomProperty(SHIFT)
    if layer.customProperty(er.PROPERTY):
        er.restore_layer(layer, state)
    layer.triggerRepaint()
    # The VRT moved from is no longer needed.
    if old and old != target:
        if geo_fix_io.ours(old):
            # A corrected image's VRTs (it and its model's).
            geo_fix_io.forget_files(old)
        elif old.startswith(shifts_folder() if folder is None else folder):
            try:
                os.remove(old)
            except OSError:
                pass
