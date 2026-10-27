"""
The images observed: the project's raster layers, as the viewer's
imageInfo — the image's id (the catalog's, kept on the layer, else its file
name), its name, when it was taken and its footprint.

When it was taken, in the viewer's order: the layer's own time (its
temporal properties), the GDAL metadata items, the NITF date, the file name,
the TIFF DateTime; else the time typed for it (kept on the layer, so in the
project).

`image_layer()` opens an image the way both docks should: a sign-in is an
authcfg id in the layer's URI (QGIS adds the credentials when asking), so no
token reaches the project file.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from qgis.core import (
    QgsDateTimeRange, QgsMapLayer, QgsProject, QgsProviderRegistry,
    QgsRasterLayer,
)

from ..core.obs.book import ObservedImage
from ..core.obs.image_time import image_time, parse_time
from .geometry import to_wgs84
from .plan_layer import qdatetime

#: Layer properties (kept in the project): the catalog's image id, and the time typed for an image that does not say (ms).
IMAGE_ID = "satobs/imageId"
IMAGE_TIME = "satobs/imageTime"


def image_layer(source: str, name: str, image_id: Optional[str] = None, time: Optional[int] = None,
                authcfg: Optional[str] = None, provider: str = "gdal") -> QgsRasterLayer:
    """
    A raster layer of an image (a path or URL; a URL is read through
    /vsicurl/), signed in with an authentication configuration when given,
    with the catalog's id and time on it. Never put a token in `source`.
    """
    if provider == "gdal":
        path = f"/vsicurl/{source}" if re.match(r"^https?://", source, re.IGNORECASE) else source
        parts = {"path": path}
        if authcfg:
            parts["authcfg"] = authcfg
        uri = QgsProviderRegistry.instance().encodeUri("gdal", parts)
    else:
        parts = {"url": source}
        if authcfg:
            parts["authcfg"] = authcfg
        uri = QgsProviderRegistry.instance().encodeUri(provider, parts)
    layer = QgsRasterLayer(uri, name, provider)
    if image_id:
        layer.setCustomProperty(IMAGE_ID, image_id)
    if time is not None:
        temporal = layer.temporalProperties()
        temporal.setIsActive(True)
        temporal.setFixedTemporalRange(QgsDateTimeRange(qdatetime(time), qdatetime(time)))
    return layer


def file_name(layer: QgsMapLayer) -> str:
    """The layer's file name (as the viewer's shortName), else its name."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
        path = parts.get("path") or parts.get("url") or ""
    except Exception:  # noqa: BLE001
        path = ""
    path = re.sub(r"^/vsi\w+/", "", str(path or ""))
    base = re.split(r"[?#]", path)[0].rstrip("/")
    return os.path.basename(base) or layer.name()


def _metadata(layer: QgsRasterLayer) -> dict[str, str]:
    """The GDAL metadata of the default domain (the TIFF and NITF tags too), {} when not a GDAL image."""
    if layer.providerType() != "gdal":
        return {}
    try:
        from osgeo import gdal
    except ImportError:
        return {}
    parts = QgsProviderRegistry.instance().decodeUri("gdal", layer.source())
    path = parts.get("path")
    if not path or re.match(r"^/vsicurl/", path):
        # Not opened again over the network: the provider's own metadata then.
        return {}
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        dataset = gdal.Open(path)
        return dict(dataset.GetMetadata() or {}) if dataset else {}
    except Exception:  # noqa: BLE001 (an image GDAL cannot read here says nothing)
        return {}
    finally:
        gdal.PopErrorHandler()


def _layer_time(layer: QgsRasterLayer) -> Optional[int]:
    temporal = layer.temporalProperties()
    if not temporal or not temporal.isActive():
        return None
    try:
        begin = temporal.fixedTemporalRange().begin()
    except AttributeError:
        return None
    return begin.toMSecsSinceEpoch() if begin.isValid() else None


def manual_time(layer: QgsMapLayer) -> Optional[int]:
    v = layer.customProperty(IMAGE_TIME)
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return parse_time(str(v))


def set_manual_time(layer: QgsMapLayer, t: int) -> None:
    layer.setCustomProperty(IMAGE_TIME, str(int(t)))


def _box(layer: QgsRasterLayer) -> Optional[list[float]]:
    crs = layer.crs()
    if not crs.isValid():
        return None
    extent = layer.extent()
    if extent.isEmpty():
        return None
    try:
        e = to_wgs84(crs).transformBoundingBox(extent)
    except Exception:  # noqa: BLE001
        return None
    return [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]


def observed_image(layer: QgsRasterLayer) -> ObservedImage:
    name = file_name(layer)
    image_id = layer.customProperty(IMAGE_ID) or name
    t = _layer_time(layer)
    origin = "layer" if t is not None else ""
    if t is None:
        found = image_time(name, _metadata(layer))
        if found:
            t, origin = found
    if t is None:
        t = manual_time(layer)
        origin = "manual" if t is not None else ""
    return ObservedImage(id=str(image_id), name=name, time=t, box=_box(layer), layer_id=layer.id(), origin=origin)


def open_images(project: Optional[QgsProject] = None) -> list[ObservedImage]:
    """The project's raster layers as images to record on."""
    project = project or QgsProject.instance()
    return [observed_image(layer) for layer in project.mapLayers().values() if isinstance(layer, QgsRasterLayer) and layer.isValid()]
