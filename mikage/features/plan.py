"""
撮像計画: 撮像計画, 衛星パス, 雲量マップ, 「移動目標撮像計画…」, 「雲量を属性に付与…」,
「ここを撮像計画」 on the map, and the satellite catalog read ahead.
"""
from __future__ import annotations

from qgis.core import QgsProject

from ..core.preload import ITEMS as PRELOAD_ITEMS
from ..ui.cloud_dock import CloudDock
from ..ui.imaging_plan_dock import ImagingPlanDock
from ..ui.moving_target_dialog import MovingTargetDialog
from ..ui.satellite_pass_dock import SatellitePassDock
from .base import DockSpec, Feature, close


class PlanSatellites:
    """The satellites port (ui/ports.SatellitesPort): the imaging plan's satellite catalog."""

    def __init__(self, dock: ImagingPlanDock):
        self.dock = dock

    def read(self, then, failed=None) -> None:
        dock = self.dock
        dock.load_catalog(lambda: then([*dock.catalog_sats]), failed)

    def known(self) -> list:
        return self.dock.catalog_sats


class Plan(Feature):
    COMMANDS = ("open_moving_targets", "open_cloud_cover")

    def __init__(self, host):
        super().__init__(host)
        #: 「移動目標撮像計画」, once opened (plugin.api.moving_targets).
        host.moving_targets = None
        # Its port: the satellites (撮像依頼, 撮像計画ファイル出力, 撮像予定 and 衛星の障害情報 use them).
        host.ports.satellites = lambda: PlanSatellites(host.docks["SatObsImagingPlan"]) if "SatObsImagingPlan" in host.docks else None

    def dock_specs(self) -> dict:
        iface, context, settings, window, docks = self.iface, self.context, self.host.open_settings, self.window(), self.docks
        return {
            "SatObsImagingPlan": DockSpec("撮像計画", "imaging_plan", lambda: ImagingPlanDock(iface, context, settings, window)),
            "SatObsSatellitePass": DockSpec("衛星パス", "satellite_pass",
                                            lambda: SatellitePassDock(iface, context, settings, docks["SatObsImagingPlan"], window)),
            "SatObsCloud": DockSpec("雲量マップ", "cloud_map",
                                    lambda: CloudDock(iface, context, settings, lambda: self.host.open_cloud_cover(), window)),
        }

    def connect(self) -> None:
        plan = self.docks["SatObsImagingPlan"]
        # The plan's reminders as notices (通知).
        notices = self.ports.notices()
        if notices is not None:
            plan.on_reminder = notices.add_plan_notice
        # 「臨時依頼にする」: what the plan finds goes to the ledger, through the 「撮像依頼」 form (tsudo, 2026-11-09).
        plan.make_requests = self._requests_from_plan

    def _requests_from_plan(self, ops: list, sats: list, targets: list):
        """The 「撮像依頼」 dock shown, its form opened with 臨時 requests of the plan's opportunities."""
        dock = self.ports.requests()
        dock.show()
        dock.raise_()
        return dock.from_plan(ops, sats, targets)

    def add_actions(self) -> None:
        self.entry("moving_targets", "移動目標撮像計画…", "open_moving_targets")
        # 「雲量を属性に付与…」: each feature's cover at its time and place (the 「雲量マップ」 dock shows the forecast).
        self.entry("cloud_cover", "雲量を属性に付与…", "open_cloud_cover")

    def map_menu(self) -> dict:
        return {"ここを撮像計画": lambda x, y: self.docks["SatObsImagingPlan"].plan_at(x, y)}

    def preload(self) -> list:
        docks, config = self.docks, lambda: self.context.config

        def satellites(then, failed) -> bool:
            dock = docks.get("SatObsImagingPlan")
            if dock is None or not config().satellite_catalogs or dock.loaded:
                return False
            dock.load_catalog(then, failed)
            return True

        return [("satellites", PRELOAD_ITEMS["satellites"], satellites, False)]

    def open_moving_targets(self, layer=None) -> MovingTargetDialog:
        """Opens 「移動目標撮像計画」 (once; kept while the plugin is loaded)."""
        host = self.host
        if host.moving_targets is None:
            host.moving_targets = MovingTargetDialog(self.iface, self.docks["SatObsImagingPlan"].plan_moving, self.window())
        host.moving_targets.open_for(layer)
        return host.moving_targets

    def open_cloud_cover(self) -> None:
        """「雲量を属性に付与」's dialog, with the active vector layer and its date-time field."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.cloud_algorithm import default_inputs as cloud_inputs

        layers = list(QgsProject.instance().mapLayers().values())
        processing.execAlgorithmDialog("mikage:cloud_cover", cloud_inputs(layers, self.iface.activeLayer()))

    def unload(self) -> None:
        if "SatObsCloud" in self.docks:
            self.docks["SatObsCloud"].unload()

    def unload_after_docks(self) -> None:
        close(self.host.moving_targets)
        self.host.moving_targets = None
