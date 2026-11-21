"""
地図・レイヤー: 「座標・地名へ移動」 and the cursor position, 「外部から図形を受け取る」, 背景地図,
the settings file's layers, 「新規スクラッチレイヤー（テンプレート）…」 and the ArcGIS Web Maps.
"""
from __future__ import annotations

from typing import Optional

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from ..ui import command_runs as runs
from ..ui.basemap_switch import BaseMapSwitch
from ..ui.coordinate_box import CoordinateBox
from ..ui.new_layer_dialog import NewLayerDialog, ScratchDialogHook, offered_templates
from ..ui.shape_receiver import ShapeReceiver
from ..ui.startup import StartupLayers
from ..ui.webmap_dialog import WebMapDialogs
from .base import Feature, close


class Maps(Feature):
    COMMANDS = ("open_new_layer",)

    def __init__(self, host):
        super().__init__(host)
        #: What the plugin (and plugin.api) shows of this feature.
        host.coordinates = None
        host.shape_receiver = None
        host.startup_layers = None
        host.base_maps = None
        host.webmaps = None
        host.new_layer = None
        host.new_layer_action = None
        host.scratch_hook = None

    def start(self) -> None:
        host = self.host
        # 「座標・地名へ移動」 on its own toolbar (before the plugin's), the cursor position in the status bar.
        host.coordinates = CoordinateBox(self.iface, self.context, self.window())
        # 「外部から図形を受け取る」: listens once the settings file is read (`shapeReceiver`).
        host.shape_receiver = ShapeReceiver(self.iface, self.context, self.window())

    def add_actions(self) -> None:
        host = self.host
        self._add_new_layer()
        # The settings file's `layers`: added to an empty project at start, or from the menu.
        host.startup_layers = StartupLayers(self.iface, self.context, self.window())
        self.entry("startup_layers", "設定ファイルのレイヤーを追加", lambda: host.startup_layers.add())
        # 「背景地図」: the toolbar button opens the list; in the menu, a submenu.
        base_maps = self.entry("base_map", "背景地図")
        host.base_maps = BaseMapSwitch(self.iface, self.context, base_maps, self.window())
        # ArcGIS Web Maps: opened as layers, and the project saved as one.
        host.webmaps = WebMapDialogs(self.iface, self.context, host.base_maps.choose, self.window())
        host.webmaps.allows = host.allows
        for name, label, show in (("webmap_open", "ArcGIS Web マップを開く…", host.webmaps.show_open),
                                  ("webmap_save", "ArcGIS Web マップとして保存…", host.webmaps.show_save)):
            self.entry(name, label, show)

    def text_operations(self) -> dict:
        host = self.host
        return {"jump": lambda r, p: runs.jump(host.coordinates, r, p)} if host.coordinates is not None else {}

    def _add_new_layer(self) -> None:
        """「新規スクラッチレイヤー（テンプレート）…」: in the plugin's menu, and in Layer ▸ Create Layer after QGIS's own scratch layer."""
        host = self.host
        action: QAction = self.entry("new_layer", "新規スクラッチレイヤー（テンプレート）…", "open_new_layer",
                                     name="SatObsNewScratchLayer")
        host.new_layer_action = action
        menu = self.iface.newLayerMenu() if hasattr(self.iface, "newLayerMenu") else None
        if menu is not None:
            actions = menu.actions()
            native = next((a for a in actions if a.objectName() == "mActionNewMemoryLayer"), None)
            after = actions[actions.index(native) + 1] if native is not None and actions.index(native) + 1 < len(actions) else None
            menu.insertAction(after, action) if after is not None else menu.addAction(action)
        host.scratch_hook = ScratchDialogHook(self.open_new_layer, self._templates, self.window())

    def _templates(self) -> list:
        return offered_templates(self.context.config.layer_templates)

    def open_new_layer(self, source: str = "builtin", template: Optional[str] = None) -> NewLayerDialog:
        """Opens 「新規スクラッチレイヤー（テンプレート）」 (once; kept while the plugin is loaded)."""
        host = self.host
        if host.new_layer is None:
            host.new_layer = NewLayerDialog(self.iface, lambda: self.context.config.layer_templates, self.window())
        return host.new_layer.open_with(source, template)

    def unload(self) -> None:
        host = self.host
        if host.webmaps is not None:
            host.webmaps.close()
            host.webmaps.deleteLater()
            host.webmaps = None
        if host.base_maps is not None:
            host.base_maps.close()
            host.base_maps = None
        if host.scratch_hook is not None:
            host.scratch_hook.close_down()
            host.scratch_hook.deleteLater()
            host.scratch_hook = None
        if host.new_layer_action is not None and hasattr(self.iface, "newLayerMenu") and self.iface.newLayerMenu() is not None:
            self.iface.newLayerMenu().removeAction(host.new_layer_action)
        host.new_layer_action = None
        close(host.new_layer)
        host.new_layer = None

    def unload_after_docks(self) -> None:
        host = self.host
        for name in ("startup_layers", "coordinates", "shape_receiver"):
            close(getattr(host, name))
            setattr(host, name, None)
