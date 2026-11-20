"""
イベント: the dock, what the images, records and requests add to it (「イベントに追加」) and its read ahead.
"""
from __future__ import annotations

from ..core.preload import ITEMS as PRELOAD_ITEMS
from ..ui.events_dock import EventsDock
from .base import DockSpec, Feature

#: The docks whose 「イベントに追加」 goes to 「イベント」.
LINKED = ("SatObsImageCatalog", "SatObsObservations", "SatObsRequests")


class Events(Feature):
    def dock_specs(self) -> dict:
        iface, context, settings, window = self.iface, self.context, self.host.open_settings, self.window()
        return {"SatObsEvents": DockSpec("イベント", "events", lambda: EventsDock(iface, context, settings, lambda: self.docks, window))}

    def connect(self) -> None:
        docks = self.docks
        for name in LINKED:
            docks[name].event_linker = lambda: docks.get("SatObsEvents")

    def preload(self) -> list:
        docks = self.docks

        def events(then, failed) -> bool:
            dock = docks.get("SatObsEvents")
            if dock is None or dock.settings is None or dock.events is not None:
                return False
            dock.reload(then, failed=failed)
            return True

        return [("events", PRELOAD_ITEMS["events"], events, False)]
