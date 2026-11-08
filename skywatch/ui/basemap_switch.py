"""
「背景地図」: switches the background map in one click, as the viewer's
header does. The toolbar button opens the list (なし, then the settings
file's `baseMaps` in order); the one shown is checked. `defaultBaseMap` is
shown when QGIS starts with an empty project and in each new project.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QMenu

try:  # Qt6
    from qgis.PyQt.QtGui import QAction, QActionGroup
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction, QActionGroup

from ..qgis_io.basemap_layer import current_base_map, switch_base_map

TITLE = "背景地図"


def _empty() -> bool:
    project = QgsProject.instance()
    return not project.mapLayers() and not project.fileName()


class BaseMapSwitch(QObject):
    def __init__(self, iface, context, action: QAction, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.action = action
        self.menu = QMenu(iface.mainWindow())
        self.menu.aboutToShow.connect(self.fill)
        action.setMenu(self.menu)
        #: The base map being opened (its id), while a task runs.
        self.opening: Optional[str] = None
        self.task = None
        #: Counts the choices, so only the last one's result is shown.
        self.serial = 0
        #: Waiting for the first settings file read.
        self.pending = True
        context.configChanged.connect(self._config_read)
        iface.newProjectCreated.connect(self._new_project)
        self.fill()

    def fill(self) -> None:
        """The list, with the one shown checked."""
        self.menu.clear()
        group = QActionGroup(self.menu)
        shown = self.opening if self.opening is not None else current_base_map()
        for base_id, label in [("", self.tr("なし")), *[(b.id, b.label) for b in self.context.config.base_maps]]:
            entry = self.menu.addAction(label)
            entry.setCheckable(True)
            entry.setChecked(base_id == shown)
            entry.setData(base_id)
            group.addAction(entry)
            entry.triggered.connect(lambda _=False, i=base_id: self.choose(i))

    def choose(self, base_id: str) -> None:
        """Shows the base map of that id ("" for none)."""
        base = next((b for b in self.context.config.base_maps if b.id == base_id), None)
        if base_id and base is None:
            self.iface.messageBar().pushWarning(TITLE, self.tr("「{id}」は設定ファイルの baseMaps になし").format(id=base_id))
            return
        if self.task is not None:
            self.task.cancel()
        self.serial += 1
        serial = self.serial
        self.opening = base_id
        self.task = switch_base_map(base, self.context.source, lambda shown, problems: self._finished(serial, problems))

    def _finished(self, serial: int, problems: list[str]) -> None:
        if serial != self.serial:
            return
        self.task = None
        self.opening = None
        if problems:
            self.iface.messageBar().pushWarning(TITLE, " / ".join(problems))

    def _config_read(self) -> None:
        if not self.context.loading:
            self.fill()
        if not self.pending or self.context.loading:
            return
        self.pending = False
        if self.context.config.default_base_map and _empty():
            self.choose(self.context.config.default_base_map)

    def _new_project(self) -> None:
        if not self.pending and self.context.config.default_base_map and _empty():
            self.choose(self.context.config.default_base_map)

    def close(self) -> None:
        try:
            self.iface.newProjectCreated.disconnect(self._new_project)
        except (TypeError, RuntimeError):
            pass
        if self.task is not None:
            self.task.cancel()
