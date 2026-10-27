"""
The image catalog in QGIS: footprints as geometries, the share of one
footprint another covers, opening an image of the catalog as a raster
layer (a COG through /vsicurl/, or a local path through `pathMappings`),
reading back what a layer opened from the catalog was, and the results as
a memory layer.

A layer opened from the catalog carries (as custom properties, saved in the
project) the catalog's image id `satobs/imageId` (used by the observations
and the imaging plan's follow-up), the catalog `satobs/catalog` and
`satobs/catalogUrl`, the feature `satobs/feature` (attributes and footprint,
for 「類似画像の検索」) and the satellite's view `satobs/sensorAzimuth` /
`satobs/sensorElevation` for other plugins; its metadata has the catalog's
rows. No token is put on a layer.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsFields, QgsGeometry, QgsLayerMetadata, QgsMapLayer, QgsPointXY,
    QgsProject, QgsRasterLayer, QgsVectorLayer, QgsVectorLayerTemporalProperties,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.paths import PathError, PathMapping, resolve_path
from ..core.catalog.search import CatalogRecord, catalog_info, record_of, sensor_view_of
from ..core.util import is_number, ms_of
from . import settings
from .obs_images import IMAGE_ID, image_layer
from .plan_layer import make_field, qdatetime

CATALOG = "satobs/catalog"
CATALOG_URL = "satobs/catalogUrl"
FEATURE = "satobs/feature"
SENSOR_AZIMUTH = "satobs/sensorAzimuth"
SENSOR_ELEVATION = "satobs/sensorElevation"

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _signed_area(ring: Sequence[Sequence[float]]) -> float:
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) / 2


def footprint(geometry: Optional[dict[str, Any]]) -> Optional[QgsGeometry]:
    """An Esri polygon (WGS 84; outer rings clockwise, holes anticlockwise) as a QGIS geometry; None without one."""
    if not isinstance(geometry, dict):
        return None
    rings = geometry.get("rings")
    if not rings:
        if all(is_number(geometry.get(k)) for k in ("xmin", "ymin", "xmax", "ymax")):
            x0, y0, x1, y1 = geometry["xmin"], geometry["ymin"], geometry["xmax"], geometry["ymax"]
            rings = [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]
        else:
            return None
    polygons: list[list[list[QgsPointXY]]] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        points = [QgsPointXY(p[0], p[1]) for p in ring]
        if _signed_area(ring) <= 0 or not polygons:
            polygons.append([points])
        else:
            polygons[-1].append(points)
    if not polygons:
        return None
    g = QgsGeometry.fromMultiPolygonXY(polygons)
    if not g.isGeosValid():
        g = g.makeValid()
    return g


def _distance_area() -> QgsDistanceArea:
    d = QgsDistanceArea()
    d.setSourceCrs(WGS84, QgsProject.instance().transformContext())
    d.setEllipsoid("EPSG:7030")
    return d


def overlap_measure(reference: Optional[QgsGeometry]) -> Callable[[CatalogRecord], Optional[float]]:
    """The share of `reference` (geodesic area) each record's footprint covers; None when it cannot be measured."""
    if reference is None or reference.isEmpty():
        return lambda record: None
    measure = _distance_area()
    own = measure.measureArea(reference)

    def overlap_of(record: CatalogRecord) -> Optional[float]:
        g = footprint(record.geometry)
        if g is None or not own > 0:
            return None
        try:
            return min(1.0, measure.measureArea(reference.intersection(g)) / own)
        except Exception:  # noqa: BLE001 (a footprint GEOS cannot intersect is not measured)
            return None

    return overlap_of


class Cancelled(Exception):
    """The person did not choose the folder of a local path."""


def open_record(record: CatalogRecord, catalog: CatalogConfig, mappings: Sequence[PathMapping],
                choose_folder: Callable[[PathMapping], Optional[str]]) -> QgsRasterLayer:
    """
    The image of a record as a raster layer (not yet in the project): a URL
    through /vsicurl/ with the authentication chosen for its server, a local
    path as it is, under a mapping's URL, or in the folder chosen for its
    prefix (asked through `choose_folder` the first time). Raises PathError
    with the reason when it cannot be opened, Cancelled when no folder was chosen.
    """
    source = record.source
    if source is None:
        raise PathError(f"{record.id or '画像'} に画像の場所なし")
    if source.kind == "url":
        url = source.value if not source.value.startswith("//") else "https:" + source.value
    else:
        resolved = resolve_path(source.value, mappings, settings.path_folder)
        if resolved.kind == "folder":
            folder = choose_folder(resolved.mapping)
            if not folder:
                raise Cancelled()
            settings.set_path_folder(resolved.mapping.prefix, folder)
            resolved = resolve_path(source.value, mappings, settings.path_folder)
        url = resolved.value
    is_url = url.lower().startswith(("http://", "https://"))
    name = record.id or url.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    layer = image_layer(url, name, image_id=record.id or None, time=ms_of(record.acquired),
                        authcfg=settings.auth_for(url) if is_url else None)
    if not layer.isValid():
        raise PathError(f"{record.id or url} の画像を開けず（{url if is_url else source.value}）")
    remember(layer, record, catalog)
    return layer


def remember(layer: QgsMapLayer, record: CatalogRecord, catalog: CatalogConfig) -> None:
    """Puts what the catalog says of an image on its layer (custom properties and metadata)."""
    if record.id:
        layer.setCustomProperty(IMAGE_ID, record.id)
    layer.setCustomProperty(CATALOG, catalog.label)
    layer.setCustomProperty(CATALOG_URL, catalog.url)
    layer.setCustomProperty(FEATURE, json.dumps(record.feature(), ensure_ascii=False))
    view = sensor_view_of(record, catalog)
    if view:
        layer.setCustomProperty(SENSOR_AZIMUTH, view["azimuth"])
        layer.setCustomProperty(SENSOR_ELEVATION, view["elevation"])
    metadata = layer.metadata() if hasattr(layer, "metadata") else QgsLayerMetadata()
    metadata.setTitle(record.id or layer.name())
    metadata.setAbstract("\n".join(f"{k}: {v}" for k, v in catalog_info(record, catalog)))
    metadata.addKeywords("画像カタログ", [catalog.label])
    layer.setMetadata(metadata)


def record_of_layer(layer: QgsMapLayer, catalogs: Sequence[CatalogConfig]) -> Optional[tuple[CatalogRecord, CatalogConfig]]:
    """The catalog record a layer was opened from, with its catalog (the settings file's of the same URL); None for other layers."""
    text = layer.customProperty(FEATURE)
    url = layer.customProperty(CATALOG_URL)
    if not text or not url:
        return None
    catalog = next((c for c in catalogs if c.url == url), None)
    if catalog is None:
        return None
    try:
        feature = json.loads(text)
    except (TypeError, ValueError):
        return None
    return record_of(feature, catalog.fields), catalog


def _kind_of(values: list[Any], field_type: Optional[str]) -> str:
    if field_type in ("esriFieldTypeDate", "esriFieldTypeTimestampOffset"):
        return "date"
    present = [v for v in values if v is not None and v != ""]
    if present and all(isinstance(v, bool) or (isinstance(v, int) and abs(v) < 2 ** 31) for v in present):
        return "integer"
    if present and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "double"
    return "string"


def results_layer(records: Sequence[CatalogRecord], catalog: CatalogConfig, types: dict[str, str], name: str) -> QgsVectorLayer:
    """
    The listed images as a memory layer (WGS 84): their footprints with every
    attribute, the acquisition time driving the temporal controller.
    """
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", name, "memory")
    names: list[str] = []
    for r in records:
        for k in r.attributes:
            if k not in names:
                names.append(k)
    kinds = {n: _kind_of([r.attributes.get(n) for r in records], types.get(n)) for n in names}
    acquired = catalog.fields.get("acquired")
    if acquired and acquired in kinds and kinds[acquired] != "date":
        # Text or numbers of times: kept as they are, with the time beside them for the controller.
        names.append("acquired_time")
        kinds["acquired_time"] = "date"
    fields = QgsFields()
    for n in names:
        fields.append(make_field(n, kinds[n]))
    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()
    features = []
    for r in records:
        feature = QgsFeature(layer.fields())
        g = footprint(r.geometry)
        if g is not None:
            feature.setGeometry(g)
        values = []
        for n in names:
            v = r.acquired if n == "acquired_time" else r.attributes.get(n)
            if kinds[n] == "date":
                t = ms_of(v)
                values.append(qdatetime(t) if t is not None else None)
            elif kinds[n] == "string":
                values.append(None if v is None else str(v))
            else:
                values.append(None if v is None or (isinstance(v, float) and math.isnan(v)) else v)
        feature.setAttributes(values)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    time_field = "acquired_time" if "acquired_time" in kinds else acquired if acquired in kinds else None
    if time_field:
        temporal = layer.temporalProperties()
        temporal.setIsActive(True)
        temporal.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField)
        temporal.setStartField(time_field)
    return layer
