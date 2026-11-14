"""
The settings file's `layers` in QGIS: each entry opened as a layer in a
task (WMS capabilities, WFS and Esri service information are read without
holding up the map), then added to the project in order, the first entry
at the bottom; those with `visible: false` unchecked. Entries that cannot
be opened say why; the rest are added.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.core import QgsMapLayer, QgsProject, QgsRasterLayer, QgsVectorLayer, QgsVectorTileLayer
from qgis.PyQt.QtCore import QCoreApplication, QTimer

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
                ) -> tuple[list[tuple[QgsMapLayer, StartupLayer]], list[str]]:
    """
    The layers of `entries` (in the calling thread, moved to the main thread),
    each with its entry, and why the others could not be opened.
    """
    layers: list[tuple[QgsMapLayer, StartupLayer]] = []
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
        layers.append((layer, entry))
    return layers, problems


def leave_unchanged() -> None:
    """
    The project as not changed, now and once QGIS has set up its first layers:
    the layer panel's map bridge sets the project's CRS from the first layer
    a moment later (in the event loop), which marks the project changed
    again. For what the plugin adds on its own to an untitled project, so that
    QGIS does not ask to save it (at its own start, at 「新規プロジェクト」, at
    opening another project or at closing).
    """
    project = QgsProject.instance()
    project.setDirty(False)

    def later() -> None:
        if not project.fileName():
            project.setDirty(False)

    # Two turns of the event loop: after the bridge's queued update.
    QTimer.singleShot(0, lambda: QTimer.singleShot(0, later))


def add_startup_layers(entries: Sequence[StartupLayer], config_source: str,
                       finished: Callable[[list[QgsMapLayer], list[str]], None],
                       still_wanted: Callable[[], bool] = lambda: True, unchanged: bool = False) -> None:
    """
    Opens `entries` in a task and adds them to the project (the first at the
    bottom); `finished` hears what was added and the problems. Nothing is
    added when `still_wanted` says no by then (a project opened meanwhile).
    `unchanged`: added by the plugin on its own (at start, in a new project),
    so a project not changed before stays not changed (QGIS does not ask to
    save it when another project is opened or QGIS is closed).
    """
    def done(result) -> None:
        opened, problems = result
        if not still_wanted():
            finished([], [])
            return
        project = QgsProject.instance()
        clean = unchanged and not project.isDirty()
        root = project.layerTreeRoot()
        for layer, entry in opened:
            # Each added on top of the ones before: the first entry ends at the bottom, as in the viewer.
            project.addMapLayer(layer)
            node = root.findLayer(layer.id()) if not entry.visible else None
            if node is not None:
                node.setItemVisibilityChecked(False)
        if clean and opened:
            leave_unchanged()
        finished([layer for layer, _ in opened], problems)

    run_task("設定ファイルのレイヤーの読み込み", lambda task: open_layers(entries, config_source, task.check), done,
             lambda message: finished([], [f"設定ファイルのレイヤーの読み込みに失敗: {message}"]))
