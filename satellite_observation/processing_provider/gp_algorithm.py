"""
サーバ処理 as Processing algorithms (the viewer's GP / OGC Processes form):
a task of an ArcGIS geoprocessing service or a process of an OGC API –
Processes server, read once, becomes an algorithm of the 「サーバ処理」 group
whose parameters are the task's inputs (features from any vector layer,
sent in the form the server takes). Running it waits for the job, shows its
progress and messages, cancels it on the server when the run is cancelled,
and opens the results as layers.

The algorithms exist while QGIS runs (they are made from what the server
said); 「サーバ処理…」 reads a service and opens one.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from qgis.core import (
    QgsApplication, QgsProcessing, QgsProcessingAlgorithm, QgsProcessingContext, QgsProcessingException, QgsProcessingOutputMultipleLayers,
    QgsProcessingOutputString, QgsProcessingParameterBoolean, QgsProcessingParameterDateTime, QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber, QgsProcessingParameterString,
)
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, Qt
from qgis.PyQt.QtGui import QIcon

from ..core.gp import GpCancelled, GpClient, GpError, GpTask
from ..qgis_io import gp_io

HERE = os.path.dirname(os.path.dirname(__file__))
GROUP = "サーバ処理"
UNITS = [("esriMeters", "メートル"), ("esriKilometers", "キロメートル"), ("esriFeet", "フィート"), ("esriMiles", "マイル"),
         ("esriNauticalMiles", "海里"), ("esriYards", "ヤード")]
LAYERS = "LAYERS"
RESULTS = "RESULTS"

#: Tasks read in this session, by algorithm name: (task, token of the settings file or "").
_tasks: dict = {}


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def algorithm_name(task: GpTask) -> str:
    return "remote_" + hashlib.sha1(task.url.encode()).hexdigest()[:12]


def register(task: GpTask, token: str = "") -> str:
    """Makes `task` an algorithm of the provider; its id (satobs:remote_…)."""
    name = algorithm_name(task)
    _tasks[name] = (task, token)
    provider = QgsApplication.processingRegistry().providerById("satobs")
    if provider is not None:
        provider.refreshAlgorithms()
    return f"satobs:{name}"


def registered() -> list:
    return [RemoteTaskAlgorithm(name) for name in _tasks]


def parameter_names(task: GpTask) -> dict:
    """Processing names of the task's inputs (letters, digits and _ only, unique), by input name."""
    names: dict = {}
    used = {LAYERS, RESULTS}
    for f in task.inputs:
        base = re.sub(r"\W", "_", f.name, flags=re.ASCII) or "INPUT"
        name, n = base, 2
        while name in used or name.endswith("_UNITS"):
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        names[f.name] = name
    return names


def _date(value) -> QDateTime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return QDateTime.fromMSecsSinceEpoch(int(value), Qt.TimeSpec.UTC)
    if isinstance(value, str):
        return QDateTime.fromString(value.replace("Z", "+00:00"), Qt.DateFormat.ISODate)
    return QDateTime()


class RemoteTaskAlgorithm(QgsProcessingAlgorithm):
    def __init__(self, name: str):
        super().__init__()
        self._name = name
        self._task, self._token = _tasks[name]
        self._names = parameter_names(self._task)

    def createInstance(self) -> "RemoteTaskAlgorithm":  # noqa: N802 (QGIS's name)
        return RemoteTaskAlgorithm(self._name)

    def name(self) -> str:
        return self._name

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return self._task.title

    def group(self) -> str:
        return tr(GROUP)

    def groupId(self) -> str:  # noqa: N802 (QGIS's name)
        return "remote"

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "gp.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        kind = tr("ArcGIS のジオプロセシング") if self._task.kind == "esri" else "OGC API – Processes"
        parts = [self._task.description, f"{kind}: {self._task.url}"]
        parts += [f"{f.label}: {f.description}" for f in self._task.inputs if f.description]
        return "\n\n".join(p for p in parts if p)

    def flags(self):
        # Made from what a server said: not offered to models or scripts by name, as it may be gone next time.
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagHideFromModeler

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        for f in self._task.inputs:
            name, label, optional = self._names[f.name], tr(f.label), not f.required
            if f.type == "choice":
                options = [str(c) for c in f.choices]
                default = options.index(str(f.default)) if f.default is not None and str(f.default) in options else (None if optional else 0)
                p = QgsProcessingParameterEnum(name, label, options, False, default, optional)
            elif f.type in ("integer", "number", "linear-unit"):
                kind = QgsProcessingParameterNumber.Type.Integer if f.type == "integer" else QgsProcessingParameterNumber.Type.Double
                default = f.default.get("distance") if isinstance(f.default, dict) else f.default
                p = QgsProcessingParameterNumber(name, label, kind, default if isinstance(default, (int, float)) else None, optional)
                if f.minimum is not None:
                    p.setMinimum(f.minimum)
                if f.maximum is not None:
                    p.setMaximum(f.maximum)
            elif f.type == "boolean":
                p = QgsProcessingParameterBoolean(name, label, bool(f.default), True)
            elif f.type == "date":
                p = QgsProcessingParameterDateTime(name, label, QgsProcessingParameterDateTime.Type.DateTime, None, optional)
                default = _date(f.default)
                if default.isValid():
                    p.setDefaultValue(default)
            elif f.type in ("features", "table"):
                types = {"Point": [QgsProcessing.SourceType.TypeVectorPoint], "LineString": [QgsProcessing.SourceType.TypeVectorLine],
                         "Polygon": [QgsProcessing.SourceType.TypeVectorPolygon]}.get(f.geometry or "", [QgsProcessing.SourceType.TypeVectorAnyGeometry])
                p = QgsProcessingParameterFeatureSource(name, label, [QgsProcessing.SourceType.TypeVector] if f.type == "table" else types,
                                                        None, optional)
            else:
                text = f.default if isinstance(f.default, str) else None
                p = QgsProcessingParameterString(name, label, text, f.type in ("json", "multi"), optional)
            if f.description:
                p.setHelp(f.description)
            self.addParameter(p)
            if f.type == "linear-unit":
                keys = [k for k, _ in UNITS]
                self.addParameter(QgsProcessingParameterEnum(f"{name}_UNITS", tr("{label}の単位").format(label=label), [tr(t) for _, t in UNITS],
                                                             False, keys.index(f.units) if f.units in keys else 0))
        self.addOutput(QgsProcessingOutputMultipleLayers(LAYERS, tr("結果のレイヤー")))
        self.addOutput(QgsProcessingOutputString(RESULTS, tr("結果の値とメッセージ")))

    def values(self, parameters, context) -> dict:
        """The task's input values from the parameters, as core/gp.py takes them."""
        values: dict = {}
        for f in self._task.inputs:
            name = self._names[f.name]
            if parameters.get(name) is None and f.type not in ("boolean",):
                continue
            if f.type == "choice":
                index = self.parameterAsEnum(parameters, name, context)
                values[f.name] = f.choices[index] if 0 <= index < len(f.choices) else None
            elif f.type == "integer":
                values[f.name] = self.parameterAsInt(parameters, name, context)
            elif f.type == "number":
                values[f.name] = self.parameterAsDouble(parameters, name, context)
            elif f.type == "linear-unit":
                unit = UNITS[self.parameterAsEnum(parameters, f"{name}_UNITS", context)][0]
                values[f.name] = {"distance": self.parameterAsDouble(parameters, name, context), "units": unit}
            elif f.type == "boolean":
                values[f.name] = self.parameterAsBool(parameters, name, context)
            elif f.type == "date":
                t = self.parameterAsDateTime(parameters, name, context)
                values[f.name] = t.toMSecsSinceEpoch() if t.isValid() else None
            elif f.type in ("features", "table"):
                source = self.parameterAsSource(parameters, name, context)
                if source is not None:
                    values[f.name] = gp_io.feature_input(source, source.sourceCrs(), context.project(), f.type == "table")
            else:
                values[f.name] = self.parameterAsString(parameters, name, context)
        return values

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        values = self.values(parameters, context)

        def status(text: str, percent: Optional[float]) -> None:
            feedback.setProgressText(tr(text))
            if percent is not None:
                feedback.setProgress(percent)

        client = GpClient(lambda method, url, body, headers: gp_io.http(method, url, body, headers, feedback), self._token or None,
                          cancelled=feedback.isCanceled)
        try:
            result = client.run(self._task, values, status)
        except GpCancelled:
            feedback.reportError(tr("取り消し"))
            return {LAYERS: [], RESULTS: ""}
        except GpError as e:
            raise QgsProcessingException(tr(str(e)))
        for message in result.messages:
            feedback.pushInfo(tr(message))
        layers, lines = [], []
        for output in result.outputs:
            name = f"{self._task.title} - {output.label}"
            layer = None
            if output.kind == "features":
                layer = gp_io.features_layer(output, name)
                if output.truncated:
                    feedback.pushWarning(tr("「{label}」は一部の地物のみ").format(label=output.label))
            elif output.kind == "image":
                layer = gp_io.image_layer(output.url, name)
                if layer is None:
                    lines.append(f"{output.label}: {output.url}")
            elif output.kind == "link":
                lines.append(f"{output.label}: {output.url}")
            else:
                lines.append(f"{output.label}: {output.text}")
            if layer is not None:
                layers.append(layer)
        if result.map_server:
            layer = gp_io.map_server_layer(result.map_server, tr("{title} - 結果の地図").format(title=self._task.title))
            if layer is not None:
                layers.append(layer)
        ids = []
        for layer in layers:
            context.temporaryLayerStore().addMapLayer(layer)
            context.addLayerToLoadOnCompletion(layer.id(), QgsProcessingContext.LayerDetails(layer.name(), context.project(), LAYERS))
            ids.append(layer.id())
        for line in lines:
            feedback.pushInfo(line)
        if not layers and not lines:
            feedback.pushInfo(tr("結果なし"))
        return {LAYERS: ids, RESULTS: "\n".join(lines + [tr(m) for m in result.messages])}
