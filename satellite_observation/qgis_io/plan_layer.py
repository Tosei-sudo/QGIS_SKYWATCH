"""The plan's scenes as a memory layer, with its time set up for the temporal controller."""
from __future__ import annotations

from qgis.core import (
    QgsFeature, QgsField, QgsFields, QgsVectorLayer, QgsVectorLayerTemporalProperties,
)
from qgis.PyQt.QtCore import QDateTime, QMetaType, QTimeZone, QVariant

from ..core.plan.output import PlanLayer
from .geometry import multipolygon_of

_META = {"date": "QDateTime", "string": "QString", "double": "Double", "integer": "Int"}
_VARIANT = {"date": "DateTime", "string": "String", "double": "Double", "integer": "Int"}


def make_field(name: str, kind: str) -> QgsField:
    """A field of a kind (`date`, `string`, `double`, `integer`): QMetaType from QGIS 3.38, QVariant before (and no QVariant types on Qt6)."""
    try:
        return QgsField(name, getattr(QMetaType.Type, _META[kind]))
    except TypeError:
        return QgsField(name, getattr(QVariant, _VARIANT[kind]))


def qdatetime(ms: float) -> QDateTime:
    """A moment as a QDateTime in UTC."""
    return QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc())


def memory_layer(plan: PlanLayer, name: str) -> QgsVectorLayer:
    """A memory layer (WGS84 multipolygons) of a plan's features, its `time` field driving the temporal controller."""
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", name, "memory")
    fields = QgsFields()
    for f in plan.fields:
        qf = make_field(f.name, f.kind)
        qf.setAlias(f.alias)
        fields.append(qf)
    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()
    for i, f in enumerate(plan.fields):
        layer.setFieldAlias(i, f.alias)
    features = []
    for rings, attributes in plan.features:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(multipolygon_of(rings))
        values = []
        for f in plan.fields:
            v = attributes.get(f.name)
            values.append(qdatetime(v) if f.kind == "date" and v is not None else v)
        feature.setAttributes(values)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    temporal = layer.temporalProperties()
    temporal.setIsActive(True)
    temporal.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField)
    temporal.setStartField("time")
    return layer
