"""
The settings file's `layers` in QGIS: each entry opened as a layer in a
task (WMS capabilities, WFS and Esri service information are read without
holding up the map), then added to the project in order, the first entry
at the bottom. Entries that cannot be opened say why; the rest are added.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.core import QgsMapLayer, QgsProject, QgsRasterLayer, QgsVectorLayer, QgsVectorTileLayer
from qgis.PyQt.QtCore import QCoreApplication

from ..core.startup_layers import LayerSource, StartupLayer, layer_source, resolve_url
from . import settings
from .tasks import run_task


def make_layer(source: LayerSource) -> QgsMapLayer:
    if source.kind == "vectortile":
        return QgsVectorTileLayer(source.source, source.name)
    if source.kind == "vector":
        layer = QgsVectorLayer(source.source, source.name, source.provider)
        if source.subset and layer.isValid():
            layer.setSubsetString(source.subset)
        return layer
    return QgsRasterLayer(source.source, source.name, source.provider)


def open_layers(entries: Sequence[StartupLayer], config_source: str, check: Optional[Callable[[], None]] = None,
                ) -> tuple[list[QgsMapLayer], list[str]]:
    """The layers of `entries` (in the calling thread, moved to the main thread) and why the others could not be opened."""
    layers: list[QgsMapLayer] = []
    problems: list[str] = []
    main = QCoreApplication.instance().thread()
    for entry in entries:
        if check:
            check()
        url = resolve_url(entry.url, config_source)
        source = layer_source(entry, config_source, settings.auth_for(url))
        layer = make_layer(source)
        if not layer.isValid():
            error = layer.error().summary() if layer.error() and not layer.error().isEmpty() else ""
            problems.append(f"{source.name}（{entry.type}）を開けず" + (f": {error}" if error else ""))
            continue
        if source.notes:
            problems.append(f"{source.name}: " + "・".join(source.notes))
        layer.moveToThread(main)
        layers.append(layer)
    return layers, problems


def add_startup_layers(entries: Sequence[StartupLayer], config_source: str,
                       finished: Callable[[list[QgsMapLayer], list[str]], None],
                       still_wanted: Callable[[], bool] = lambda: True) -> None:
    """
    Opens `entries` in a task and adds them to the project (the first at the
    bottom); `finished` hears what was added and the problems. Nothing is
    added when `still_wanted` says no by then (a project opened meanwhile).
    """
    def done(result) -> None:
        layers, problems = result
        if not still_wanted():
            finished([], [])
            return
        project = QgsProject.instance()
        for layer in layers:
            # Each added on top of the ones before: the first entry ends at the bottom, as in the viewer.
            project.addMapLayer(layer)
        finished(layers, problems)

    run_task("設定ファイルのレイヤーの読み込み", lambda task: open_layers(entries, config_source, task.check), done,
             lambda message: finished([], [f"設定ファイルのレイヤーの読み込みに失敗: {message}"]))
