"""
サーバ処理 in QGIS: the transport core/gp.py's client runs over (QGIS's
network access manager, its proxy and the authentication configuration
chosen for the service), the features of a layer as what a server takes,
and a run's results as layers (features as memory layers, images and a
job's result map service as raster layers).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from qgis.core import (
    QgsArcGisRestContext, QgsArcGisRestUtils, QgsBlockingNetworkRequest, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
    QgsFeatureRequest, QgsField, QgsFields, QgsGeometry, QgsJsonUtils, QgsProject, QgsRasterLayer, QgsVectorLayer, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QByteArray, QUrl, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest

from ..core.gp import FeatureInput, GpError, GpOutput
from . import settings
from .tasks import current_feedback
from .webmap_io import _attribute

WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def http(method: str, url: str, body: Optional[bytes], headers: dict, feedback=None) -> tuple:
    """One request through QGIS (in a task or a Processing run): (status, headers with lower-case names, body)."""
    request = QNetworkRequest(QUrl(url))
    for name, value in headers.items():
        request.setRawHeader(name.encode(), value.encode())
    blocking = QgsBlockingNetworkRequest()
    authcfg = settings.auth_for(url)
    if authcfg:
        blocking.setAuthCfg(authcfg)
    feedback = feedback or current_feedback()
    if method == "GET":
        error = blocking.get(request, True, feedback)
    elif method == "POST":
        error = blocking.post(request, QByteArray(body or b""), True, feedback)
    elif method == "DELETE":
        error = blocking.deleteResource(request, feedback)
    else:
        raise GpError(f"{method} には非対応")
    reply = blocking.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if error != QgsBlockingNetworkRequest.ErrorCode.NoError and not isinstance(status, int):
        # No answer at all; the message may repeat the address with a token in its query.
        detail = re.sub(r"\?\S*", "", blocking.errorMessage())
        raise GpError(f"接続不可（{url.split('?', 1)[0]}{'：' + detail if detail else ''}）")
    names = {bytes(h).decode("latin-1").lower(): bytes(reply.rawHeader(h)).decode("latin-1") for h in reply.rawHeaderList()}
    return int(status or 0), names, bytes(reply.content())


def feature_input(source, crs: QgsCoordinateReferenceSystem, project: Optional[QgsProject] = None, table: bool = False) -> FeatureInput:
    """The features of a layer (or Processing source) as a server takes them: Esri JSON in Web Mercator, GeoJSON in WGS 84."""
    project = project or QgsProject.instance()
    to_mercator = QgsCoordinateTransform(crs, WEB_MERCATOR, project)
    to_degrees = QgsCoordinateTransform(crs, WGS84, project)
    context = QgsArcGisRestContext()
    fields = []
    for f in source.fields():
        definition = QgsArcGisRestUtils.fieldDefinitionToJson(f)
        if definition.get("type") in ("esriFieldTypeOID", None):
            continue
        definition["alias"] = f.alias() or f.name()
        fields.append(definition)
    names = [d["name"] for d in fields]
    esri, geojson = [], []
    kind = ""
    for feature in source.getFeatures(QgsFeatureRequest()):
        attributes = {n: _attribute(feature[n]) for n in names}
        geometry = feature.geometry()
        if table or geometry.isNull() or geometry.isEmpty():
            esri.append({"attributes": attributes})
            geojson.append({"type": "Feature", "geometry": None, "properties": attributes})
            continue
        mercator = QgsGeometry(geometry)
        mercator.transform(to_mercator)
        degrees = QgsGeometry(geometry)
        degrees.transform(to_degrees)
        esri.append({"geometry": QgsArcGisRestUtils.geometryToJson(mercator, context), "attributes": attributes})
        geojson.append({"type": "Feature", "geometry": json.loads(degrees.asJson(8)), "properties": attributes})
        if not kind:
            kind = {QgsWkbTypes.GeometryType.PointGeometry: "esriGeometryMultipoint" if QgsWkbTypes.isMultiType(geometry.wkbType()) else "esriGeometryPoint",
                    QgsWkbTypes.GeometryType.LineGeometry: "esriGeometryPolyline"}.get(geometry.type(), "esriGeometryPolygon")
    return FeatureInput(esri, geojson, fields, "" if table else kind)


_GEOJSON_TYPES = {"integer": QVariant.LongLong, "double": QVariant.Double, "string": QVariant.String, "other": QVariant.String}


def _crs_of(srs: Optional[str]) -> QgsCoordinateReferenceSystem:
    if not srs:
        return WEB_MERCATOR
    crs = QgsCoordinateReferenceSystem(srs) if srs.startswith("EPSG:") else QgsCoordinateReferenceSystem.fromWkt(srs)
    return crs if crs.isValid() else WEB_MERCATOR


def features_layer(output: GpOutput, name: str) -> QgsVectorLayer:
    """A features result as a memory layer, in the result's own CRS."""
    crs = _crs_of(output.srs)
    if output.format == "esri":
        kind = output.geometry_type
        wkb = {"esriGeometryPoint": "Point", "esriGeometryMultipoint": "MultiPoint", "esriGeometryPolyline": "MultiLineString",
               "esriGeometryPolygon": "MultiPolygon", "esriGeometryEnvelope": "MultiPolygon"}.get(kind, "")
        if not wkb:
            first = next((f.get("geometry") for f in output.features if isinstance(f.get("geometry"), dict)), None)
            wkb = ("Point" if "x" in first else "MultiLineString" if "paths" in first else "MultiPolygon" if "rings" in first else "None") if first else "None"
            kind = {"Point": "esriGeometryPoint", "MultiLineString": "esriGeometryPolyline", "MultiPolygon": "esriGeometryPolygon"}.get(wkb, "")
        layer = QgsVectorLayer(f"{wkb}?crs={crs.authid()}" if wkb != "None" else "None", name, "memory")
        if wkb != "None" and not crs.authid():
            layer.setCrs(crs)
        fields = QgsFields()
        for f in output.fields:
            if f["type"] in ("esriFieldTypeGeometry", "esriFieldTypeBlob", "esriFieldTypeRaster"):
                continue
            field = QgsField(f["name"], QgsArcGisRestUtils.convertFieldType(f["type"]))
            if f.get("alias"):
                field.setAlias(f["alias"])
            fields.append(field)
        if not output.fields:
            # No field list: every attribute as text, numbers as numbers.
            seen: dict = {}
            for item in output.features[:1000]:
                for key, value in (item.get("attributes") or {}).items():
                    seen.setdefault(key, QVariant.Double if isinstance(value, (int, float)) and not isinstance(value, bool) else QVariant.String)
            for key, variant in seen.items():
                fields.append(QgsField(key, variant))
        layer.dataProvider().addAttributes(fields.toList())
        layer.updateFields()
        date = QgsArcGisRestUtils.convertFieldType("esriFieldTypeDate")
        made = []
        for item in output.features:
            feature = QgsFeature(layer.fields())
            geometry = item.get("geometry")
            if wkb != "None" and isinstance(geometry, dict):
                g = QgsArcGisRestUtils.convertGeometry(geometry, kind, "m" in geometry, "z" in geometry)[0]
                if g is not None:
                    shape = QgsGeometry(g)
                    if wkb.startswith("Multi"):
                        shape.convertToMultiType()
                    feature.setGeometry(shape)
            for key, value in (item.get("attributes") or {}).items():
                index = layer.fields().indexOf(key)
                if index < 0:
                    continue
                if value is not None and layer.fields().at(index).type() == date:
                    value = QgsArcGisRestUtils.convertDateTime(value)
                feature.setAttribute(index, value)
            made.append(feature)
        layer.dataProvider().addFeatures(made)
        layer.updateExtents()
        return layer
    # GeoJSON (WGS 84).
    collection = json.dumps({"type": "FeatureCollection", "features": output.features}, ensure_ascii=False)
    fields = QgsFields()
    for key, kind in output.fields:
        fields.append(QgsField(key, _GEOJSON_TYPES[kind]))
    features = QgsJsonUtils.stringToFeatureList(collection, fields)
    first = next((f.geometry() for f in features if not f.geometry().isNull()), None)
    wkb = QgsWkbTypes.displayString(QgsWkbTypes.multiType(first.wkbType())) if first is not None else "None"
    layer = QgsVectorLayer(f"{wkb}?crs=EPSG:4326" if first is not None else "None", name, "memory")
    layer.dataProvider().addAttributes(fields.toList())
    layer.updateFields()
    made = []
    for f in features:
        feature = QgsFeature(layer.fields())
        if first is not None and not f.geometry().isNull():
            shape = QgsGeometry(f.geometry())
            shape.convertToMultiType()
            feature.setGeometry(shape)
        for key, _ in output.fields:
            feature.setAttribute(key, f[key] if f.fields().indexOf(key) >= 0 else None)
        made.append(feature)
    layer.dataProvider().addFeatures(made)
    layer.updateExtents()
    return layer


def image_layer(url: str, name: str) -> Optional[QgsRasterLayer]:
    """An image result (GeoTIFF, PNG…) read over the network by GDAL; None when it cannot be opened."""
    layer = QgsRasterLayer(f"/vsicurl/{url}", name, "gdal")
    return layer if layer.isValid() else None


def map_server_layer(url: str, name: str) -> Optional[QgsRasterLayer]:
    """A job's result map service (`…/MapServer/jobs/<id>`) as a map layer; None when it cannot be opened."""
    authcfg = settings.auth_for(url)
    auth = f" authcfg='{authcfg}'" if authcfg else ""
    layer = QgsRasterLayer(f"crs='EPSG:3857' format='PNG32' layer='' url='{url}'{auth}", name, "arcgismapserver")
    return layer if layer.isValid() else None
