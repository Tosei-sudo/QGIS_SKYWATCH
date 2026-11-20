"""
レイヤーに付け足すもの: QGIS の式の関数 「MIKAGE」 (the records the observation dock read and the
named areas), the date fields corrected for the settings file's timeZone, and who and when in the
tracking fields of Esri layers QGIS edits itself (editTracking).
"""
from __future__ import annotations

from qgis.core import QgsProject

from ..qgis_io import edit_stamp_layers, expression_functions, time_zone_fields
from .base import Feature, disconnect, tr


class Layers(Feature):
    def __init__(self, host):
        super().__init__(host)
        host.place_reader = None
        host.time_zone_fields = None
        host.edit_stamp_layers = None

    def connect(self) -> None:
        host, project = self.host, QgsProject.instance()
        # QGIS の式の関数 「MIKAGE」: the records the observation dock read and the named areas.
        expression_functions.set_book(lambda: getattr(self.ports.observations(), "book", None))
        expression_functions.register()
        host.place_reader = expression_functions.PlaceReader(self.context, refresh=lambda: self.iface.mapCanvas().refreshAllLayers())
        host.place_reader.start()
        # Layers whose dates are in another zone than the service says (the settings file's timeZone): a corrected field per date.
        time_zone_fields.register()
        context = self.context
        host.time_zone_fields = time_zone_fields.TimeZoneFields(
            context.esri, lambda url: context.token_for(url, context.token_of(url)),
            lambda text: self.iface.messageBar().pushWarning(tr("タイムゾーン"), text))
        project.layersAdded.connect(host.time_zone_fields.layers_added)
        context.configChanged.connect(self._time_zones_changed)
        # Esri layers QGIS edits itself: who and when in their tracking fields (the settings file's editTracking).
        host.edit_stamp_layers = edit_stamp_layers.EditStampLayers()
        project.layersAdded.connect(host.edit_stamp_layers.layers_added)
        project.layersRemoved.connect(host.edit_stamp_layers.layers_removed)
        host.edit_stamp_layers.layers_added(project.mapLayers().values())

    def _time_zones_changed(self) -> None:
        if self.host.time_zone_fields is not None:
            self.host.time_zone_fields.layers_added(QgsProject.instance().mapLayers().values())

    def unload(self) -> None:
        host, project = self.host, QgsProject.instance()
        if host.edit_stamp_layers is not None:
            disconnect(project.layersAdded, host.edit_stamp_layers.layers_added)
            disconnect(project.layersRemoved, host.edit_stamp_layers.layers_removed)
            host.edit_stamp_layers.active = False
            host.edit_stamp_layers = None
        if host.time_zone_fields is not None:
            disconnect(project.layersAdded, host.time_zone_fields.layers_added)
            host.time_zone_fields = None
            time_zone_fields.unregister()
        if host.place_reader is not None:
            host.place_reader.stop()
            host.place_reader = None
        expression_functions.unregister()
