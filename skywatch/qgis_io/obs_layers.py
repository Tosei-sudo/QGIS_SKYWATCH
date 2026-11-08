"""The observations as memory layers (points, areas, flows), their time set up for the temporal controller."""
from __future__ import annotations

from qgis.core import QgsFeature, QgsFields, QgsGeometry, QgsPointXY, QgsVectorLayer, QgsVectorLayerTemporalProperties

from ..core.obs.output import ObservationLayer
from .geometry import line_of, polygon_of
from .plan_layer import make_field, qdatetime

_TYPES = {"point": "Point", "line": "LineString", "polygon": "Polygon"}


def _geometry(kind: str, shape) -> QgsGeometry:
    if kind == "point":
        return QgsGeometry.fromPointXY(QgsPointXY(shape[0], shape[1]))
    if kind == "line":
        return line_of(shape)
    return polygon_of([shape])


def observation_memory_layer(data: ObservationLayer) -> QgsVectorLayer:
    layer = QgsVectorLayer(f"{_TYPES[data.geometry]}?crs=EPSG:4326", data.title, "memory")
    fields = QgsFields()
    for f in data.fields:
        fields.append(make_field(f.name, f.kind))
    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()
    for i, f in enumerate(data.fields):
        layer.setFieldAlias(i, f.alias)
    features = []
    for shape, attributes in data.features:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(_geometry(data.geometry, shape))
        values = []
        for f in data.fields:
            v = attributes.get(f.name)
            values.append(qdatetime(v) if f.kind == "date" and v is not None else v)
        feature.setAttributes(values)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    if data.time_field:
        temporal = layer.temporalProperties()
        temporal.setIsActive(True)
        if data.end_field:
            temporal.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeStartAndEndFromFields)
            temporal.setStartField(data.time_field)
            temporal.setEndField(data.end_field)
        else:
            temporal.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField)
            temporal.setStartField(data.time_field)
    return layer
