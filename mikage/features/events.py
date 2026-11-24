"""
イベント: the dock, what the images, records and requests add to it (「イベントに追加」) and its read ahead.
参考情報 (出典つきの参考情報, 2026-11-24): its dock, linked to the events, things and records (core/reference_info.py).
"""
from __future__ import annotations

from ..core.preload import ITEMS as PRELOAD_ITEMS
from ..ui.events_dock import EventsDock
from ..ui.reference_dock import ReferenceDock
from .base import DockSpec, Feature


class Events(Feature):
    def __init__(self, host):
        super().__init__(host)
        # Its ports: 「イベントに追加」 of the images, records and requests; the infos of the images, records and events.
        host.ports.events = lambda: host.docks.get("SatObsEvents")
        host.ports.references = lambda: host.docks.get("SatObsReferences")

    def dock_specs(self) -> dict:
        iface, context, settings, window, ports = self.iface, self.context, self.host.open_settings, self.window(), self.ports
        return {"SatObsEvents": DockSpec("イベント", "events", lambda: EventsDock(iface, context, settings, ports, window)),
                "SatObsReferences": DockSpec("参考情報", "references", lambda: ReferenceDock(iface, context, settings, ports, window))}

    def connect(self) -> None:
        # 「ファイルなしで記録」 of the events' images goes to 「観測記録」.
        self.docks["SatObsEvents"].observations = self.observations_if_allowed()

    def preload(self) -> list:
        docks = self.docks

        def events(then, failed) -> bool:
            dock = docks.get("SatObsEvents")
            if dock is None or dock.settings is None or dock.events is not None:
                return False
            dock.reload(then, failed=failed)
            return True

        return [("events", PRELOAD_ITEMS["events"], events, False)]
