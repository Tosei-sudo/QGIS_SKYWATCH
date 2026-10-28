"""
Layers QGIS opens itself (ArcGIS feature services, WFS, OGC API – Features:
the settings file's `layers`, or added in the data source manager) whose
dates are in another zone than the service says (the settings file's
`timeZone` / `timeZones`): their providers read the dates, so each date field
gets a virtual field beside it with the date as the instant it stands for
(`SEEN（Asia/Tokyo）`), by the expression function `satobs_time_zone`.
The plugin's own requests (catalogs, stores, AI results …) are converted
where they are made (core/esri.py), so their layers need none.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Callable, Optional

from qgis.core import (
    QgsExpression,
    QgsField,
    QgsMapLayer,
    QgsProviderRegistry,
    QgsVectorLayer,
    qgsfunction,
)
from qgis.PyQt.QtCore import QDateTime, Qt, QVariant

from ..core.esri import EsriClient, EsriError
from ..core.time_zone import UTC, TimeShift, TimeZoneRule, parse_time_zone, read_instant, read_text_instant, rule_for, service_zone_of, shift_text
from .tasks import run_task

FUNCTION = "satobs_time_zone"
#: The layer's shift (`Asia/Tokyo（サービス側: UTC）`), once its fields are added.
PROPERTY = "satobs/timeZone"
#: The providers whose layers get the fields, and whether the zone the service reads dates in is asked of it (Esri).
PROVIDERS = {"arcgisfeatureserver": True, "WFS": False, "OAPIF": False}

_HELP = """<h3>satobs_time_zone(値, データのタイムゾーン, サービスのタイムゾーン)</h3>
サービスが別のタイムゾーンで読んでいる日時を、データのタイムゾーンの時計の時刻として読んだ日時。
文字列はそのタイムゾーンの時刻として読む（末尾の Z・+09:00 は無視）。"""


def corrected(value, data: str, service: str):
    """`value` (a date-time, or text) read as clock time in `data`, the service having read it in `service`; None when it is neither."""
    zone = parse_time_zone(data)
    if zone is None or value is None:
        return None
    shift = TimeShift(zone, parse_time_zone(service) or UTC)
    if isinstance(value, QDateTime):
        if value.isNull() or not value.isValid():
            return None
        t = read_instant(value.toMSecsSinceEpoch(), shift)
    elif isinstance(value, datetime):
        t = read_instant((value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)).timestamp() * 1000, shift)
    elif isinstance(value, str):
        t = read_text_instant(value, zone)
    else:
        return None
    if t is None:
        return None
    return QDateTime.fromMSecsSinceEpoch(int(round(t)), Qt.TimeSpec.UTC)


@qgsfunction(args="auto", group="衛星観測", register=False, helpText=_HELP)
def satobs_time_zone(value, data, service, feature, parent):  # noqa: ARG001 (QGIS's arguments)
    return corrected(value, data, service)


def register() -> None:
    if not QgsExpression.isFunctionName(FUNCTION):
        QgsExpression.registerFunction(satobs_time_zone)


def unregister() -> None:
    if QgsExpression.isFunctionName(FUNCTION):
        QgsExpression.unregisterFunction(FUNCTION)


def layer_url(layer: QgsMapLayer) -> Optional[str]:
    """The service URL of a layer QGIS opened from one of PROVIDERS, else None."""
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() not in PROVIDERS:
        return None
    url = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source()).get("url")
    return url if isinstance(url, str) and url else None


def field_name(name: str, rule: TimeZoneRule) -> str:
    return f"{name}（{rule.zone.name}）"


def add_fields(layer: QgsVectorLayer, rule: TimeZoneRule, shift: TimeShift) -> list[str]:
    """Adds the corrected field of each date-time field (not again when there); the names added."""
    added: list[str] = []
    names = set(layer.fields().names())
    for f in list(layer.fields()):
        if f.type() != QVariant.DateTime or layer.fields().fieldOrigin(layer.fields().indexOf(f.name())) == layer.fields().OriginExpression:
            continue
        name = field_name(f.name(), rule)
        if name in names:
            continue
        expression = f"{FUNCTION}({QgsExpression.quotedColumnRef(f.name())}, {QgsExpression.quotedString(shift.data.name)}, {QgsExpression.quotedString(shift.service.name)})"
        field = QgsField(name, QVariant.DateTime)
        field.setComment(f"{f.name()} を {shift_text(shift)} として読んだ日時")
        layer.addExpressionField(expression, field)
        added.append(name)
    layer.setCustomProperty(PROPERTY, shift_text(shift))
    return added


class TimeZoneFields:
    """Gives the layers added to the project their corrected date fields."""

    def __init__(self, esri: Callable[[], EsriClient]):
        #: An Esri client for a task (its requests carry the authentication chosen for the service).
        self.esri = esri

    def layers_added(self, layers) -> None:
        for layer in layers:
            url = layer_url(layer)
            rule = rule_for(url)
            if rule is None:
                continue
            if rule.service or not PROVIDERS[layer.providerType()]:
                add_fields(layer, rule, TimeShift(rule.zone, rule.service or UTC))
                continue
            self._ask_service(layer, url, rule)

    def _ask_service(self, layer: QgsVectorLayer, url: str, rule: TimeZoneRule) -> None:
        """An Esri layer: the zone it reads its dates in, from its information (`dateFieldsTimeReference`), then the fields."""
        layer_id = layer.id()

        def work(task):
            try:
                return self.esri().layer(url).raw
            except EsriError:
                return {}

        def done(info) -> None:
            from qgis.core import QgsProject

            target = QgsProject.instance().mapLayer(layer_id)
            if target is not None:
                add_fields(target, rule, TimeShift(rule.zone, service_zone_of(info, rule)))

        run_task("日時のタイムゾーンの確認", work, done, lambda message: done({}))
