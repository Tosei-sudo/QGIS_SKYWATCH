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

Its 衛星名 (asked for with a `SatelliteLookup`; core/obs/satellites.py): the
catalog's 「センサー」 (kept on the layer, else read from the catalog's
attributes kept on it), else the file's metadata, else its name, else the
one typed for it (kept on the layer).

`image_layer()` opens an image the way both docks should: a sign-in is an
authcfg id in the layer's URI (QGIS adds the credentials when asking), so no
token reaches the project file.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from qgis.core import (
    QgsDateTimeRange, QgsMapLayer, QgsProject, QgsProviderRegistry,
    QgsRasterLayer,
)

from ..core.obs.api import ObservedImage, joined_image
from ..core.obs.api import image_time, parse_time
from ..core.obs.api import NO_CHOICES, SatelliteChoices, clean as clean_satellite, satellite_in_metadata
from .complex_sar import source_of
from .geometry import to_wgs84
from .plan_layer import qdatetime
from .shift_io import SOURCE as SHIFT_SOURCE

#: Layer properties (kept in the project): the catalog's image id, and the time typed for an image that does not say (ms).
IMAGE_ID = "satobs/imageId"
IMAGE_TIME = "satobs/imageTime"
#: The image's path below the record's folder, for a catalog record whose images are in a folder (catalog_io).
IMAGE_FILE = "satobs/imageFile"
#: 衛星名: the catalog's 「センサー」 (catalog_io), and the one typed for an image that does not say (「衛星不明（入力）」).
SATELLITE = "satobs/satellite"
SATELLITE_TYPED = "satobs/satelliteTyped"


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


def _own_source(layer: QgsMapLayer) -> str:
    """
    The layer's own source: an image moved by 位置ずらし shows a VRT of it
    (shift_io), whose name is not the image's (a record made on it went to an
    image 「<uuid>.vrt」 without its time, 2026-11-19).
    """
    try:
        moved = layer.customProperty(SHIFT_SOURCE)
    except (AttributeError, RuntimeError):
        moved = None
    return str(moved or "") or layer.source()


def image_path(layer: QgsMapLayer) -> str:
    """Where the layer's image is (its path or URL, without /vsicurl/; a complex SAR image's own, not its VRT's), '' when unknown."""
    shown = source_of(layer)
    if shown:
        return shown
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), _own_source(layer))
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
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), _own_source(layer))
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


@dataclass(frozen=True)
class SatelliteLookup:
    """What finding an image's 衛星名 needs: the names known (spelling, file names) and the catalogs (their sensor attribute)."""
    choices: SatelliteChoices = NO_CHOICES
    catalogs: Sequence[Any] = ()


#: The satellite each file's metadata names (by path; '' none), read once (cleared by forget()).
_metadata_names: dict[str, str] = {}
_metadata_lock = threading.Lock()


def forget() -> None:
    """Forgets the files' metadata read (on the plugin's unload)."""
    with _metadata_lock:
        _metadata_names.clear()


def metadata_satellite(layer: QgsMapLayer) -> str:
    """
    The satellite a layer's file names in its metadata (the default and
    IMAGERY domains, a SICD's XML: satellites.satellite_in_metadata), '' when
    none does or the file is not a GDAL one on this computer (a URL is not
    opened again). Read once per file.
    """
    if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "gdal":
        return ""
    path = source_of(layer)
    if not path:
        try:
            path = QgsProviderRegistry.instance().decodeUri("gdal", _own_source(layer)).get("path") or ""
        except Exception:  # noqa: BLE001
            path = ""
    path = str(path or "")
    if not path or re.match(r"^/vsi(curl|s3|gs|az|adls|oss|swift|http)", path):
        return ""
    with _metadata_lock:
        if path in _metadata_names:
            return _metadata_names[path]
    try:
        from osgeo import gdal
    except ImportError:
        return ""
    found = ""
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        dataset = gdal.Open(path)
        if dataset is not None:
            items: dict[str, Any] = {}
            for domain in ("", "IMAGERY"):
                items.update(dataset.GetMetadata(domain) or {})
            texts = [t for t in (dataset.GetMetadata("xml:DES") or []) if isinstance(t, str)]
            found = satellite_in_metadata(items, texts)
    except Exception:  # noqa: BLE001 (a file GDAL cannot read here says nothing)
        found = ""
    finally:
        gdal.PopErrorHandler()
    with _metadata_lock:
        _metadata_names[path] = found
    return found


def image_satellite(layer: QgsMapLayer, lookup: Optional[SatelliteLookup] = None) -> tuple[str, str]:
    """
    衛星名 of a layer's image and where it came from (satellites.ORIGINS):
    the catalog's (on the layer, else from the catalog's attributes kept on
    it), the file's metadata, its name, or the one typed; ('', '') when none says.
    """
    lookup = lookup or SatelliteLookup()
    own = clean_satellite(layer.customProperty(SATELLITE))
    if own:
        return own, "catalog"
    if layer.customProperty(IMAGE_ID) and lookup.catalogs:
        # Opened from the catalog before 衛星名 was kept on the layer (2026-11-20): its attributes are.
        from .catalog_io import record_of_layer

        try:
            found = record_of_layer(layer, lookup.catalogs)
        except Exception:  # noqa: BLE001
            found = None
        sensor = clean_satellite(found[0].sensor) if found else ""
        if sensor:
            return sensor, "catalog"
    named = metadata_satellite(layer)
    if named:
        return lookup.choices.spelled(named), "metadata"
    named = lookup.choices.in_file_name(file_name(layer))
    if named:
        return named, "name"
    typed = clean_satellite(layer.customProperty(SATELLITE_TYPED))
    if typed:
        return typed, "manual"
    return "", ""


def set_typed_satellite(layer: QgsMapLayer, name: str) -> None:
    """Keeps the 衛星名 typed for an image on its layer (so in the project)."""
    layer.setCustomProperty(SATELLITE_TYPED, clean_satellite(name))


def observed_image(layer: QgsRasterLayer, satellites: Optional[SatelliteLookup] = None) -> ObservedImage:
    """
    A layer's image: the catalog's id (else the file name) as its id and its
    name; its time from the layer, else its file (name, metadata), else the
    one typed for it; with `satellites`, its 衛星名 (image_satellite).
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
    satellite, satellite_origin = image_satellite(layer, satellites) if satellites is not None else ("", "")
    return ObservedImage(id=image_id, name=image_id, time=t, box=_box(layer), layer_id=layer.id(), origin=origin,
                         satellite=satellite, satellite_origin=satellite_origin)


#: Providers of maps a server draws (background maps: XYZ tiles, WMS・WMTS, ArcGIS map services): not images.
MAP_PROVIDERS = ("wms", "arcgismapserver", "xyz")


def is_image_layer(layer: QgsMapLayer) -> bool:
    """
    Whether a raster layer is an image to record on: one opened from the
    catalog, or any other but a background map (tiles, WMS, a map service).
    A background map was listed as 「日時不明  {y}.png」 (tsudo 2026-11-11),
    chosen as the image when nothing else was open.
    """
    return bool(layer.customProperty(IMAGE_ID)) or layer.providerType() not in MAP_PROVIDERS


def open_images(project: Optional[QgsProject] = None, satellites: Optional[SatelliteLookup] = None) -> list[ObservedImage]:
    """
    The project's raster layers as images to record on (not the background
    maps): the layers of one catalog image id (a record's images in its
    folder) as one image (top of the layer tree first); with `satellites`, their 衛星名.
    """
    project = project or QgsProject.instance()
    order = {layer_id: i for i, layer_id in enumerate(project.layerTreeRoot().findLayerIds())}
    rasters = [layer for layer in project.mapLayers().values()
               if isinstance(layer, QgsRasterLayer) and layer.isValid() and is_image_layer(layer)]
    rasters.sort(key=lambda layer: order.get(layer.id(), len(order)))
    out: list = []
    by_id: dict[str, list] = {}
    for layer in rasters:
        image = observed_image(layer, satellites)
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
