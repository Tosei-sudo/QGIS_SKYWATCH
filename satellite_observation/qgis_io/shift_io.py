"""
Moving an image on the map (the viewer's 位置ずらし, shift.ts): a GDAL raster
layer is moved by `dx, dy` (its CRS's units) from where its file puts it,
without reading or copying the image. The layer is pointed at a small VRT of
its file with a moved georeferencing (in the plugin's folder of the QGIS
profile), keeping its style; its own file and the move are kept in custom
properties, so moving again starts from the file and no move gives the file back.

Everything that reads the layer's source (Processing, export, the plugin's
tools) gets the moved image, as the viewer's saving does.
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from osgeo import gdal

SOURCE = "satobs/shift_source"
SHIFT = "satobs/shift"


def shifts_folder() -> str:
    """Where the moved VRTs are written."""
    from qgis.core import QgsApplication

    folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "satellite_observation", "shifted")
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


def moved_vrt(source: str, dx: float, dy: float, folder: Optional[str] = None) -> str:
    """A VRT of `source` moved by dx, dy; its path."""
    path = os.path.join(folder or shifts_folder(), f"{uuid.uuid4().hex}.vrt")
    ds = gdal.Translate(path, source.split("|")[0], format="VRT")
    if ds is None:
        raise OSError(f"{source} を開けず")
    gt = list(ds.GetGeoTransform())
    gt[0] += dx
    gt[3] += dy
    ds.SetGeoTransform(gt)
    ds = None
    return path


def set_shift(layer, dx: float, dy: float, folder: Optional[str] = None) -> None:
    """Moves the layer to dx, dy (its CRS's units) from where its own file puts it; (0, 0) gives the file back."""
    from qgis.core import QgsDataProvider

    from . import enhance_renderer as er

    source = original_source(layer)
    old = layer.source() if layer.customProperty(SOURCE) else ""
    if not dx and not dy:
        target = source
    else:
        target = moved_vrt(source, dx, dy, folder)
    # QGIS keeps the layer's renderer (its style) across a new source.
    layer.setDataSource(target, layer.name(), "gdal", QgsDataProvider.ProviderOptions())
    if not dx and not dy:
        layer.removeCustomProperty(SOURCE)
        layer.removeCustomProperty(SHIFT)
    else:
        layer.setCustomProperty(SOURCE, source)
        layer.setCustomProperty(SHIFT, f"{dx!r},{dy!r}")
    if layer.customProperty(er.PROPERTY):
        er.restore_layer(layer)
    layer.triggerRepaint()
    # The VRT moved from is no longer needed.
    if old and old != target and old.startswith(shifts_folder() if folder is None else folder):
        try:
            os.remove(old)
        except OSError:
            pass
