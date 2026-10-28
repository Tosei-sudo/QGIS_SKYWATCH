"""
The settings file's `layers` at start: added when the settings file has
been read and the project is still empty (QGIS started without a project),
and to each new project (「新規プロジェクト」), as the viewer opens them at
start. 「設定ファイルのレイヤーを追加」 adds them at any time.
"""
from __future__ import annotations

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject

from ..qgis_io import settings
from ..qgis_io.startup_layers import add_startup_layers

TITLE = "設定ファイルのレイヤー"


def _empty() -> bool:
    project = QgsProject.instance()
    return not project.mapLayers() and not project.fileName()


class StartupLayers(QObject):
    def __init__(self, iface, context, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        #: Waiting for the first settings file read (a URL is read in a task).
        self.pending = True
        context.configChanged.connect(self._config_read)
        iface.newProjectCreated.connect(self._new_project)

    def _config_read(self) -> None:
        if not self.pending or self.context.loading:
            return
        self.pending = False
        if settings.startup_layers_on() and self.context.config.startup_layers and _empty():
            self.add(only_if_empty=True)

    def _new_project(self) -> None:
        # While the first settings file is being read, that read adds them.
        if not self.pending and settings.startup_layers_on() and self.context.config.startup_layers and _empty():
            self.add(only_if_empty=True)

    def close(self) -> None:
        try:
            self.iface.newProjectCreated.disconnect(self._new_project)
        except (TypeError, RuntimeError):
            pass

    def add(self, only_if_empty: bool = False) -> None:
        """Adds the settings file's `layers` to the project."""
        entries = self.context.config.startup_layers
        if not entries:
            self.iface.messageBar().pushInfo(TITLE, self.tr("設定ファイルに layers なし"))
            return
        add_startup_layers(entries, self.context.source, self._finished, _empty if only_if_empty else (lambda: True))

    def _finished(self, layers, problems) -> None:
        if layers:
            self.iface.messageBar().pushInfo(TITLE, self.tr("{n} 件を追加").format(n=len(layers)))
        if problems:
            self.iface.messageBar().pushWarning(TITLE, " / ".join(problems))
