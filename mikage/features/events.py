"""
イベント: the dock, what the images, records and requests add to it (「イベントに追加」) and its read ahead.
"""
from __future__ import annotations

from ..core.preload import ITEMS as PRELOAD_ITEMS
from ..ui.events_dock import EventsDock
from .base import DockSpec, Feature


class Events(Feature):
    def __init__(self, host):
        super().__init__(host)
        # Its port: 「イベントに追加」 of the images, records and requests.
        host.ports.events = lambda: host.docks.get("SatObsEvents")

    def dock_specs(self) -> dict:
        iface, context, settings, window, ports = self.iface, self.context, self.host.open_settings, self.window(), self.ports
        return {"SatObsEvents": DockSpec("イベント", "events", lambda: EventsDock(iface, context, settings, ports, window))}

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
