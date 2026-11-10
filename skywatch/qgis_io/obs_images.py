"""
The images observed: the project's raster layers, as the viewer's
imageInfo — the image's id (the catalog's, kept on the layer, else its file
name), its name, when it was taken and its footprint.

When it was taken, in the viewer's order: the layer's own time (its
temporal properties), the GDAL metadata items, the NITF date, the file name,
the TIFF DateTime; else the time typed for it (kept on the layer, so in the
project).

An image opened from the catalog is named by its id (tsudo, 2026-11-10: the
records' image names were file names for some images, folder names for
others and ids for those without files): the records' 画像名 is then the
catalog's image id for every catalog image, the file name for the others.

The layers of one catalog image id (a record whose folder has several
images, or the same image opened twice) are one image to record on: what is
recorded on any of its files is the record's.

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

from ..core.obs.book import ObservedImage, joined_image
from ..core.obs.image_time import image_time, parse_time
from .complex_sar import source_of
from .geometry import to_wgs84
from .plan_layer import qdatetime

#: Layer properties (kept in the project): the catalog's image id, and the time typed for an image that does not say (ms).
IMAGE_ID = "satobs/imageId"
IMAGE_TIME = "satobs/imageTime"
#: The image's path below the record's folder, for a catalog record whose images are in a folder (catalog_io).
IMAGE_FILE = "satobs/imageFile"


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


def image_path(layer: QgsMapLayer) -> str:
    """Where the layer's image is (its path or URL, without /vsicurl/; a complex SAR image's own, not its VRT's), '' when unknown."""
    shown = source_of(layer)
    if shown:
        return shown
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
        path = parts.get("path") or parts.get("url") or ""
    except Exception:  # noqa: BLE001
        path = ""
    return re.sub(r"^/vsicurl/", "", str(path or ""))


def file_name(layer: QgsMapLayer) -> str:
    """The layer's file name (as the viewer's shortName; a complex SAR image's own, not its VRT's), else its name."""
    shown = source_of(layer)
    if shown:
        return os.path.basename(shown)
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
    return gdal_metadata(layer)


def gdal_metadata(layer: QgsRasterLayer, domain: str = "") -> dict[str, str]:
    """The GDAL metadata of a domain ('' the default, 'IMD' a Maxar .IMD…), {} when not a GDAL image."""
    if layer.providerType() != "gdal":
        return {}
    try:
        from osgeo import gdal
    except ImportError:
        return {}
    parts = QgsProviderRegistry.instance().decodeUri("gdal", layer.source())
    path = source_of(layer) or parts.get("path")
    if not path or re.match(r"^/vsicurl/", path):
        # Not opened again over the network: the provider's own metadata then.
        return {}
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        dataset = gdal.Open(path)
        return dict(dataset.GetMetadata(domain) or {}) if dataset else {}
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
    """
    A layer's image: the catalog's id (else the file name) as its id and its
    name; its time from the layer, else its file (name, metadata), else the
    one typed for it.
    """
    file = file_name(layer)
    image_id = str(layer.customProperty(IMAGE_ID) or "") or file
    t = _layer_time(layer)
    origin = "layer" if t is not None else ""
    if t is None:
        found = image_time(file, _metadata(layer))
        if found:
            t, origin = found
    if t is None:
        t = manual_time(layer)
        origin = "manual" if t is not None else ""
    return ObservedImage(id=image_id, name=image_id, time=t, box=_box(layer), layer_id=layer.id(), origin=origin)


def open_images(project: Optional[QgsProject] = None) -> list[ObservedImage]:
    """
    The project's raster layers as images to record on: the layers of one
    catalog image id (a record's images in its folder) as one image (top of
    the layer tree first).
    """
    project = project or QgsProject.instance()
    order = {layer_id: i for i, layer_id in enumerate(project.layerTreeRoot().findLayerIds())}
    rasters = [layer for layer in project.mapLayers().values() if isinstance(layer, QgsRasterLayer) and layer.isValid()]
    rasters.sort(key=lambda layer: order.get(layer.id(), len(order)))
    out: list = []
    by_id: dict[str, list] = {}
    for layer in rasters:
        image = observed_image(layer)
        if not layer.customProperty(IMAGE_ID):
            out.append(image)
            continue
        if image.id not in by_id:
            by_id[image.id] = []
            out.append(image.id)
        by_id[image.id].append((layer, image))
    images = []
    for entry in out:
        if isinstance(entry, ObservedImage):
            images.append(entry)
            continue
        parts = by_id[entry]
        images.append(parts[0][1] if len(parts) == 1 else joined_image([image for _layer, image in parts]))
    return images


def image_layers(image: Optional[ObservedImage], project: Optional[QgsProject] = None) -> list[QgsRasterLayer]:
    """The project's layers of an image (several for a record's images in its folder), [] for one listed without its file."""
    project = project or QgsProject.instance()
    layers = [project.mapLayer(layer_id) for layer_id in (image.layer_ids if image is not None else ())]
    return [layer for layer in layers if isinstance(layer, QgsRasterLayer)]


def layer_at(image: Optional[ObservedImage], lon: float, lat: float, project: Optional[QgsProject] = None) -> Optional[QgsRasterLayer]:
    """The image's layer over a place (WGS 84), else its first; None for an image listed without its file."""
    layers = image_layers(image, project)
    for layer in layers:
        box = _box(layer)
        if box and box[0] <= lon <= box[2] and box[1] <= lat <= box[3]:
            return layer
    return layers[0] if layers else None
