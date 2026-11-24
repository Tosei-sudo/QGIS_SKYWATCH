"""
撮像依頼: 撮像依頼, 撮像計画ファイル出力, 撮像予定, 地点ごとの撮像予定, 「依頼グループ…」, 「年間撮像上限…」,
「衛星・システムの障害情報…」, 「この地点の撮像予定」 on the map, the plan's 「臨時依頼を作成」 and the ledger read ahead.
"""
from __future__ import annotations

from ..core.preload import ITEMS as PRELOAD_ITEMS
from ..ui import command_runs as runs
from ..ui.place_schedule_dock import PlaceScheduleDock
from ..ui.plan_file_dock import PlanFileDock
from ..ui.request_dock import RequestDock
from ..ui.schedule_dock import ScheduleDock
from .base import DockSpec, Feature, close, tr

#: The docks whose windows and tasks close with the plugin (`close_all`).
CLOSING = ("SatObsRequests", "SatObsPlanFiles", "SatObsSchedule", "SatObsPlaceSchedule")
#: The docks that filter by 依頼グループ, and how each shows its list again (no reading) once the groups changed.
GROUP_DOCKS = (("SatObsRequests", "render"), ("SatObsPlanFiles", "render"), ("SatObsSchedule", "render"),
               ("SatObsPlaceSchedule", "refit"))


class Requests(Feature):
    COMMANDS = ("open_quotas", "open_outages", "open_request_groups")

    def __init__(self, host):
        super().__init__(host)
        #: 「衛星の障害情報」 (ui/outage_dialog.py), once opened.
        host.outage_dialog = None
        #: 「依頼グループ」 (ui/request_group_dialog.py), once opened.
        host.request_group_dialog = None
        # Its port: 撮像依頼 (イベント chooses requests there, 撮像計画 makes 臨時 ones).
        host.ports.requests = lambda: host.docks.get("SatObsRequests")

    def _satellites(self):
        """The satellites (its port, ui/ports.SatellitesPort), read once (then(list), failed(text))."""
        port = self.ports.satellites()

        def satellites(then, failed=None):
            if port is None:  # (no 撮像計画 in a test's small assembly, tests/feature_harness.py): none known
                then([])
                return
            port.read(then, failed)

        return satellites

    def _known_satellites(self) -> list:
        port = self.ports.satellites()
        return port.known() if port is not None else []

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
        port = self.ports.satellites()
        dock.satellites_known = (lambda: port.known()) if port is not None else self._known_satellites
        return dock

    def _make_plan_files(self):
        changed = self.host.signals.requests_changed
        return PlanFileDock(self.iface, self.context, self.host.open_settings, self._satellites(),
                            lambda requests=None: changed.emit(requests), self.window())

    def _requests_changed(self, requests=None) -> None:
        """
        撮像依頼 written by 「撮像計画ファイル出力」: the 「撮像依頼」 dock shows the ledger as read there, or reads it
        again when it was changed; the 「撮像予定」 dock the day's rows again once files are written.
        """
        request_dock = self.docks.get("SatObsRequests")
        schedule = self.docks.get("SatObsSchedule")
        if requests is None and schedule is not None and schedule.loaded and schedule.isVisible():
            schedule.reload()
        if request_dock is None or request_dock.store is None or request_dock.task is not None:
            return
        if requests is not None:
            request_dock.requests = requests
            request_dock.render()
        elif request_dock.loaded:
            request_dock.reload()

    def connect(self) -> None:
        self.host.signals.requests_changed.connect(self._requests_changed)
        # 「イベントに追加」 of the requests goes to 「イベント」.
        self.docks["SatObsRequests"].event_linker = self.ports.events
        # Its 「依頼グループ…」 opens the groups' list.
        self.docks["SatObsRequests"].group_editor = self.open_request_groups

    def add_actions(self) -> None:
        # 「依頼グループ」: the groups of the requests added and changed from the plugin (tsudo, 2026-11-23).
        self.entry("request_groups", "依頼グループ…", "open_request_groups", name="SatObsRequestGroups")
        # 「年間撮像上限」: what each sensor's (mode's) yearly quota has used and left, and shots not counted (tsudo, 2026-11-11).
        self.entry("quotas", "年間撮像上限…", "open_quotas", name="SatObsQuotas")
        # 「衛星の障害情報」: the satellites' outages (reason, when, impacts) that the calculations leave passes out for (tsudo, 2026-11-16).
        self.entry("outages", "衛星・システムの障害情報…", "open_outages", name="SatObsOutages")

    def map_menu(self) -> dict:
        def plans_here(x: float, y: float) -> None:
            dock = self.docks["SatObsPlaceSchedule"]
            dock.show()
            dock.raise_()
            dock.add_point(x, y)

        return {"この地点の撮像予定": plans_here}

    def text_operations(self) -> dict:
        c, docks = self.context.config, self.docks
        if not c.request_stores:
            return {}
        systems = c.request_exports
        out = {
            "planFiles": lambda r, _p: runs.plan_files(docks["SatObsPlanFiles"], r, systems),
            "follow": lambda r, _p: runs.follow(docks["SatObsPlanFiles"], r),
            "quotas": lambda r, _p: runs.quotas(self.open_quotas(), r),
            "schedule": lambda r, _p: runs.schedule(docks["SatObsSchedule"], r),
            "placeSchedule": lambda r, p: runs.place_schedule(docks["SatObsPlaceSchedule"], r, p),
            "newRequest": lambda r, _p: runs.new_request(docks["SatObsRequests"], r),
        }
        if any(s.result_file is not None for s in systems):
            out["resultImport"] = lambda r, _p: runs.result_import(docks["SatObsSchedule"], r, systems)
        if c.satellite_outages is not None:
            out["outage"] = lambda r, _p: runs.outage(self.open_outages(), r)
        return out

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

    def open_quotas(self):
        """Opens 「年間撮像上限」 (from the 「撮像計画ファイル出力」 dock's store, today's year)."""
        dock = self.docks["SatObsPlanFiles"]
        if dock.store is None:
            self.iface.messageBar().pushInfo("MIKAGE", tr("撮像依頼の保存先が未設定（管理者に連絡: requestStores）［E703］"))
            return None
        return dock.show_quotas()

    def open_outages(self):
        """Opens 「衛星の障害情報」 (once; read again each time it is opened)."""
        host = self.host
        if self.context.config.satellite_outages is None:
            self.iface.messageBar().pushInfo("MIKAGE", tr("衛星の障害情報の表が未設定（管理者に連絡: satelliteOutages）［E703］"))
            return None
        if host.outage_dialog is None:
            from ..ui.outage_dialog import OutageDialog

            host.outage_dialog = OutageDialog(self.context, self._known_satellites, host.allows, self.window())
        host.outage_dialog.show()
        host.outage_dialog.raise_()
        host.outage_dialog.run()
        return host.outage_dialog

    def open_request_groups(self):
        """Opens 「依頼グループ」 for the 「撮像依頼」 dock's store (once; got again each time it is opened)."""
        host = self.host
        dock = self.docks["SatObsRequests"]
        if dock.store is None:
            self.iface.messageBar().pushInfo("MIKAGE", tr("撮像依頼の保存先が未設定（管理者に連絡: requestStores）［E703］"))
            return None
        if not dock.store.groups:
            self.iface.messageBar().pushInfo("MIKAGE", tr("依頼グループの表が未設定（管理者に連絡: requestStores[].groups）［E703］"))
            return None
        if host.request_group_dialog is None:
            from ..ui.request_group_dialog import RequestGroupDialog

            host.request_group_dialog = RequestGroupDialog(self.context, lambda: self.docks["SatObsRequests"].store,
                                                           self._groups_changed, host.allows, self.window())
        dialog = host.request_group_dialog
        dialog.show()
        dialog.raise_()
        dialog.run()
        return dialog

    def _groups_changed(self, store, groups, requests_moved: bool = False) -> None:
        """
        The groups of a store's table as got (or just changed) in 「依頼グループ」: the docks of that
        store list them in their filters (and the request form offers them) at once; requests put
        in a renamed group are got again by the 「撮像依頼」 and 「撮像予定」 docks.
        """
        for name, redraw in GROUP_DOCKS:
            dock = self.docks.get(name)
            if dock is None or dock.store is None or dock.store.groups != store.groups:
                continue
            dock.groups = list(groups)
            if hasattr(dock, "groups_read"):
                dock.groups_read = True
            dock.group_filter.use_groups(dock.groups)
            try:
                getattr(dock, redraw)()
            except RuntimeError:  # closed and deleted already
                pass
        if requests_moved:
            self.host.signals.requests_changed.emit(None)

    def unload(self) -> None:
        for name in CLOSING:
            if name in self.docks:
                self.docks[name].close_all()

    def unload_after_docks(self) -> None:
        close(self.host.outage_dialog)
        self.host.outage_dialog = None
        if self.host.request_group_dialog is not None:
            try:
                self.host.request_group_dialog.close_all()
            except RuntimeError:
                pass
        close(self.host.request_group_dialog)
        self.host.request_group_dialog = None
