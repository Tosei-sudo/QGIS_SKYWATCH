"""
画像補正: 画像補正, 影から高さ, スペクトル, 「スワイプ比較」, 「位置ずらし」 and its 「解除」,
「クイックパンシャープン」 (placed in 解析・処理, by the Processing algorithm's 「パンシャープン…」).
"""
from __future__ import annotations

from ..qgis_io.quick_pan import QuickPanKeeper
from ..ui.enhance_dock import EnhanceDock
from ..ui.quick_pan import QuickPanSwitch
from ..ui.shadow_dock import ShadowHeightDock
from ..ui.shift import LayerShift
from ..ui.spectral_dock import SpectralDock
from ..ui.swipe import SwipeCompare
from .base import DockSpec, Feature, close


class Enhance(Feature):
    def __init__(self, host):
        super().__init__(host)
        #: 「スワイプ比較」 and 「位置ずらし」's map tools (plugin.api.swipe, plugin.api.shift).
        host.swipe = None
        host.shift = None
        #: クイックパンシャープン: its links kept as layers come and go, and the entry's switch.
        host.quick_pan_keeper = None
        host.quick_pan = None

    def start(self) -> None:
        # Before the docks: 画像補正 shows a layer's link from the start.
        self.host.quick_pan_keeper = QuickPanKeeper()

    def dock_specs(self) -> dict:
        iface, context, settings, window = self.iface, self.context, self.host.open_settings, self.window()
        return {
            "SatObsEnhance": DockSpec("画像補正", "enhance", lambda: EnhanceDock(iface, context, settings, window)),
            "SatObsShadowHeight": DockSpec("影から高さ", "shadow_height", lambda: ShadowHeightDock(iface, context, settings, window)),
            "SatObsSpectral": DockSpec("スペクトル", "spectral", lambda: SpectralDock(iface, context, settings, window)),
        }

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

    def unload(self) -> None:
        close(self.host.quick_pan)
        self.host.quick_pan = None
        if self.host.quick_pan_keeper is not None:
            self.host.quick_pan_keeper.unload()
            self.host.quick_pan_keeper = None
        close(self.host.swipe)
        self.host.swipe = None
        close(self.host.shift)
        self.host.shift = None
