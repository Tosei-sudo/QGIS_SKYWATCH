"""
画像補正: 画像補正, 影から高さ, スペクトル, 「スワイプ比較」, 「位置ずらし」 and its 「解除」, 「クイック幾何補正」,
「クイックパンシャープン」 (placed in 解析・処理, by the Processing algorithm's 「パンシャープン…」), and the toolbar
「地図の向き・補正」 (the dock's 「地図の向き」 buttons, 「補正」 and DRA, on a QGIS toolbar of their own).
"""
from __future__ import annotations

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QTimer

from ..qgis_io.geo_fix_keeper import FixKeeper
from ..qgis_io.map_view_io import typed_toward
from ..qgis_io.quick_pan import QuickPanKeeper
from ..ui.geo_fix import LayerFix
from ..ui.orient_bar import OrientToolbar
from ..ui.enhance_dock import EnhanceDock
from ..ui.quick_pan import QuickPanSwitch
from ..ui.shadow_dock import ShadowHeightDock
from ..ui.shift import LayerShift
from ..ui.spectral_dock import SpectralDock
from ..ui.swipe import SwipeCompare
from .base import DockSpec, Feature, close, tr


def _deleted(layer) -> bool:
    from qgis.PyQt import sip

    return layer is None or sip.isdeleted(layer)


class Enhance(Feature):
    def __init__(self, host):
        super().__init__(host)
        #: 「スワイプ比較」 and 「位置ずらし」's map tools (plugin.api.swipe, plugin.api.shift).
        host.swipe = None
        host.shift = None
        #: クイックパンシャープン: its links kept as layers come and go, and the entry's switch.
        host.quick_pan_keeper = None
        host.quick_pan = None
        #: クイック幾何補正: the images' corrections kept and put back, and the entry's tool (plugin.api.geo_fix).
        host.geo_fix_keeper = None
        host.geo_fix = None
        #: 「地図の向き・補正」: its own toolbar (ui/orient_bar.py).
        host.orient_toolbar = None

    def start(self) -> None:
        # Before the docks: 画像補正 shows a layer's link from the start.
        self.host.quick_pan_keeper = QuickPanKeeper()
        # クイック幾何補正: an image opened gets its kept correction (after the other parts have seen it: complex SAR's amplitude first).
        keeper = FixKeeper(self.context, self.iface, self.window())
        self.host.geo_fix_keeper = keeper
        self._geo_fix_added = lambda layers: QTimer.singleShot(0, lambda: keeper.layers_added(
            [layer for layer in layers if not _deleted(layer)]))
        QgsProject.instance().layersAdded.connect(self._geo_fix_added)

    def dock_specs(self) -> dict:
        iface, context, settings, window = self.iface, self.context, self.host.open_settings, self.window()
        return {
            "SatObsEnhance": DockSpec("画像補正", "enhance", lambda: EnhanceDock(iface, context, settings, window)),
            "SatObsShadowHeight": DockSpec("影から高さ", "shadow_height", lambda: ShadowHeightDock(iface, context, settings, window)),
            "SatObsSpectral": DockSpec("スペクトル", "spectral", lambda: SpectralDock(iface, context, settings, window)),
        }

    def connect(self) -> None:
        # 画像補正「地図の向き」: also the satellite's direction typed in クイック幾何補正 (its corrections as kept).
        host = self.host
        dock = self.docks["SatObsEnhance"]
        dock.orient_row.typed_of = lambda layer: typed_toward(layer, host.geo_fix_keeper)
        # 「地図の向き・補正」: the same buttons, 「補正」 and DRA on a toolbar of their own (tsudo 2026-11-26).
        host.orient_toolbar = OrientToolbar(self.iface, dock, self.window())
        self.roles_changed()

    def roles_changed(self) -> None:
        if self.host.orient_toolbar is not None:
            self.host.orient_toolbar.set_allowed(self.host.allows(tr("画像補正")))

    def add_actions(self) -> None:
        host = self.host
        # 「スワイプ比較」: the active layer (or group) on one side of a line only.
        swipe = self.entry("swipe", "スワイプ比較", name="SatObsSwipe")
        host.swipe = SwipeCompare(self.iface, swipe, self.window())
        # 「位置ずらし」: the active image moved by hand.
        shift = self.entry("shift", "位置ずらし", name="SatObsShift")
        host.shift = LayerShift(self.iface, shift, self.window())
        self.entry("unshift", "位置ずらしの解除", lambda: host.shift.reset())
        # 「クイックパンシャープン」: the two images chosen in the layer panel, the multispectral one drawn sharpened where it shows.
        quick = self.entry("quick_pansharpen", "クイックパンシャープン", name="SatObsQuickPan")
        host.quick_pan = QuickPanSwitch(self.iface, quick, self.window())
        # 「クイック幾何補正」: the active image put where it is, for the terrain (DEM) and landmarks dragged to their places.
        geo = self.entry("geo_fix", "クイック幾何補正", name="SatObsGeoFix")
        host.geo_fix = LayerFix(self.iface, geo, host.geo_fix_keeper,
                                lambda: host.ports.observations() if host.allows(tr("観測記録")) else None, self.window())

    def unload(self) -> None:
        close(self.host.orient_toolbar)
        self.host.orient_toolbar = None
        close(self.host.geo_fix)
        self.host.geo_fix = None
        if self.host.geo_fix_keeper is not None:
            try:
                QgsProject.instance().layersAdded.disconnect(self._geo_fix_added)
            except (TypeError, RuntimeError, AttributeError):
                pass
            self.host.geo_fix_keeper.unload()
            self.host.geo_fix_keeper = None
        close(self.host.quick_pan)
        self.host.quick_pan = None
        if self.host.quick_pan_keeper is not None:
            self.host.quick_pan_keeper.unload()
            self.host.quick_pan_keeper = None
        close(self.host.swipe)
        self.host.swipe = None
        close(self.host.shift)
        self.host.shift = None
