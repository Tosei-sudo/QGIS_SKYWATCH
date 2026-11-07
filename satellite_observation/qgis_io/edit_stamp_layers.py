"""
Esri layers QGIS edits itself (ArcGIS feature service layers opened from the
settings file's `layers`, a web map, or the data source manager): when an
edit is saved, the features added get every tracking field and the features
changed the "updated" ones (core/edit_stamp.py), as the plugin's own
writes do.
"""
from __future__ import annotations

from typing import Any, Optional

from qgis.core import QgsMapLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QDateTime, Qt, QVariant

from ..core import edit_stamp
from ..core.time_zone import UTC, TimeShift, rule_for, write_instant
from .time_zone_fields import layer_url

PROVIDER = "arcgisfeatureserver"

_TYPES = {
    QVariant.DateTime: "esriFieldTypeDate", QVariant.Date: "esriFieldTypeDate", QVariant.String: "esriFieldTypeString",
    QVariant.Double: "esriFieldTypeDouble", QVariant.LongLong: "esriFieldTypeBigInteger", QVariant.Int: "esriFieldTypeInteger",
}


def is_esri(layer: QgsMapLayer) -> bool:
    return isinstance(layer, QgsVectorLayer) and layer.providerType() == PROVIDER


def _field_types(layer: QgsVectorLayer) -> dict[str, str]:
    fields = layer.fields()
    out = {}
    for i, f in enumerate(fields):
        if fields.fieldOrigin(i) != fields.OriginProvider:
            continue
        out[f.name()] = _TYPES.get(f.type(), "")
    return out


def _qt_value(value: Any, kind: str, url: Optional[str]) -> Any:
    if kind == "esriFieldTypeDate" and isinstance(value, (int, float)):
        rule = rule_for(url)
        ms = write_instant(value, TimeShift(rule.zone, rule.service or UTC)) if rule else value
        return QDateTime.fromMSecsSinceEpoch(int(round(ms)), Qt.TimeSpec.UTC)
    return value


def stamp_buffer(layer: QgsVectorLayer, when: Optional[int] = None, user: Optional[str] = None) -> int:
    """Sets the tracking fields of the features added or changed in the edit buffer; how many features got them."""
    buffer = layer.editBuffer()
    if buffer is None:
        return 0
    fields = edit_stamp.tracked_fields(_field_types(layer))
    if not fields:
        return 0
    when = edit_stamp.now_ms() if when is None else when
    user = edit_stamp.user_name() if user is None else user
    url = layer_url(layer)
    added = set(buffer.addedFeatures().keys())
    changed = (set(buffer.changedAttributeValues().keys()) | set(buffer.changedGeometries().keys())) - added
    count = 0
    for fids, adding in ((sorted(added), True), (sorted(changed), False)):
        values = edit_stamp.stamp_values(fields, adding, when, user)
        if not values:
            continue
        for fid in fids:
            for name, value in values.items():
                index = layer.fields().indexOf(name)
                kind = next((k for n, k in fields.values() if n == name), "")
                layer.changeAttributeValue(fid, index, _qt_value(value, kind, url))
            count += 1
    return count


class EditStampLayers:
    """Watches the Esri layers added to the project and stamps their edits before they are saved."""

    def __init__(self):
        #: The ids of the layers watched (so none is connected twice).
        self.watched: set[str] = set()
        #: False once the plugin is unloaded (the layers' connections stay, doing nothing).
        self.active = True

    def layers_added(self, layers) -> None:
        for layer in layers:
            if not is_esri(layer) or layer.id() in self.watched:
                continue
            self.watched.add(layer.id())
            layer.beforeCommitChanges.connect(lambda *_, lay=layer: self._before_commit(lay))

    def layers_removed(self, ids) -> None:
        self.watched.difference_update(ids)

    def _before_commit(self, layer: QgsVectorLayer) -> None:
        if not self.active:
            return
        try:
            stamp_buffer(layer)
        except RuntimeError:
            # The layer was deleted under the signal.
            pass
