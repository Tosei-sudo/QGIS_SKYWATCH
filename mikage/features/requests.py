"""
撮像依頼: 撮像依頼, 撮像計画ファイル出力, 撮像予定, 地点ごとの撮像予定, 「年間撮像上限…」,
「衛星の障害情報…」, 「この地点の撮像予定」 on the map, the plan's 「臨時依頼にする」 and the ledger read ahead.
"""
from __future__ import annotations

from ..core.preload import ITEMS as PRELOAD_ITEMS
from ..ui.place_schedule_dock import PlaceScheduleDock
from ..ui.plan_file_dock import PlanFileDock
from ..ui.request_dock import RequestDock
from ..ui.schedule_dock import ScheduleDock
from .base import DockSpec, Feature, close, tr

#: The docks whose windows and tasks close with the plugin (`close_all`).
CLOSING = ("SatObsRequests", "SatObsPlanFiles", "SatObsSchedule", "SatObsPlaceSchedule")


class Requests(Feature):
    COMMANDS = ("open_quotas", "open_outages")

    def __init__(self, host):
        super().__init__(host)
        #: 「衛星の障害情報」 (ui/outage_dialog.py), once opened.
        host.outage_dialog = None

    def _satellites(self):
        """The imaging plan's satellites, read once (then(list), failed(text))."""
        plan_dock = self.docks["SatObsImagingPlan"]

        def satellites(then, failed=None):
            plan_dock.load_catalog(lambda: then([*plan_dock.catalog_sats]), failed)

        return satellites

    def dock_specs(self) -> dict:
        iface, context, settings, window = self.iface, self.context, self.host.open_settings, self.window()
        return {
            "SatObsRequests": DockSpec("撮像依頼", "requests", lambda: RequestDock(iface, context, settings, self._satellites(), window)),
            "SatObsPlanFiles": DockSpec("撮像計画ファイル出力", "plan_files", self._make_plan_files),
            "SatObsSchedule": DockSpec("撮像予定", "schedule", self._make_schedule),
            "SatObsPlaceSchedule": DockSpec("地点ごとの撮像予定", "place_schedule",
                                            lambda: PlaceScheduleDock(iface, context, settings, window)),
        }

    def _make_schedule(self):
        dock = ScheduleDock(self.iface, self.context, self.host.open_settings, self.window())
        # The satellites' other names and series, for the outages that name them (衛星の障害情報).
        dock.satellites_known = lambda plan_dock=self.docks["SatObsImagingPlan"]: plan_dock.catalog_sats
        return dock

    def _make_plan_files(self):
        request_dock = self.docks["SatObsRequests"]

        def requests_changed(requests=None, request_dock=request_dock):
            # The 「撮像依頼」 dock shows the ledger as read here, or reads it again when it was changed;
            # the 「撮像予定」 dock the day's rows again once files are written.
            schedule = self.docks.get("SatObsSchedule")
            if requests is None and schedule is not None and schedule.loaded and schedule.isVisible():
                schedule.reload()
            if request_dock.store is None or request_dock.task is not None:
                return
            if requests is not None:
                request_dock.requests = requests
                request_dock.render()
            elif request_dock.loaded:
                request_dock.reload()

        return PlanFileDock(self.iface, self.context, self.host.open_settings, self._satellites(), requests_changed, self.window())

    def connect(self) -> None:
        # 「臨時依頼にする」: what the plan finds goes to the ledger, through the 「撮像依頼」 form (tsudo, 2026-11-09).
        self.docks["SatObsImagingPlan"].make_requests = self._requests_from_plan

    def add_actions(self) -> None:
        # 「年間撮像上限」: what each sensor's (mode's) yearly quota has used and left, and shots not counted (tsudo, 2026-11-11).
        self.entry("quotas", "年間撮像上限…", "open_quotas", name="SatObsQuotas")
        # 「衛星の障害情報」: the satellites' outages (reason, when, impacts) that the calculations leave passes out for (tsudo, 2026-11-16).
        self.entry("outages", "衛星の障害情報…", "open_outages", name="SatObsOutages")

    def map_menu(self) -> dict:
        def plans_here(x: float, y: float) -> None:
            dock = self.docks["SatObsPlaceSchedule"]
            dock.show()
            dock.raise_()
            dock.add_point(x, y)

        return {"この地点の撮像予定": plans_here}

    def preload(self) -> list:
        docks = self.docks

        def requests(then, failed) -> bool:
            dock = docks.get("SatObsRequests")
            if dock is None or dock.store is None or dock.loaded:
                return False
            dock.reload(then, failed)
            return True

        return [("requests", PRELOAD_ITEMS["requests"], requests, False)]

    def after_offline(self, sent: bool) -> None:
        dock = self.docks.get("SatObsRequests")
        if dock is not None:
            try:
                dock.reload()
            except RuntimeError:
                pass

    def _requests_from_plan(self, ops: list, sats: list, targets: list):
        """The 「撮像依頼」 dock shown, its form opened with 臨時 requests of the plan's opportunities."""
        dock = self.docks["SatObsRequests"]
        dock.show()
        dock.raise_()
        return dock.from_plan(ops, sats, targets)

    def open_quotas(self):
        """Opens 「年間撮像上限」 (from the 「撮像計画ファイル出力」 dock's store, today's year)."""
        dock = self.docks["SatObsPlanFiles"]
        if dock.store is None:
            self.iface.messageBar().pushInfo("MIKAGE", tr("設定ファイルに撮像依頼の保存先（requestStores）なし"))
            return None
        return dock.show_quotas()

    def open_outages(self):
        """Opens 「衛星の障害情報」 (once; read again each time it is opened)."""
        host = self.host
        if self.context.config.satellite_outages is None:
            self.iface.messageBar().pushInfo("MIKAGE", tr("設定ファイルに衛星の障害情報の表（satelliteOutages）なし"))
            return None
        if host.outage_dialog is None:
            from ..ui.outage_dialog import OutageDialog

            plan = self.docks["SatObsImagingPlan"]
            host.outage_dialog = OutageDialog(self.context, lambda: plan.catalog_sats, host.allows, self.window())
        host.outage_dialog.show()
        host.outage_dialog.raise_()
        host.outage_dialog.run()
        return host.outage_dialog

    def unload(self) -> None:
        for name in CLOSING:
            if name in self.docks:
                self.docks[name].close_all()

    def unload_after_docks(self) -> None:
        close(self.host.outage_dialog)
        self.host.outage_dialog = None
