"""
「先読み」 on the QGIS side (core/preload.py): once QGIS has started and the
settings file is read, the reads of the items the settings file's `preload`
lists are started in the background (each dock reads in a task as when it
is opened), so the dock shows them at once when opened.

The reads go through the docks' own paths (PluginContext.esri()), so what
they read also lands where every Esri read lands (the offline cache of
server outages). Other parts may add items of their own (`add`).

The items are read one after another, not all at once: each read's Python
(the answer's JSON, the records) holds Python's GIL, and with several at
once QGIS's main thread waited for it far longer (the map stuttered).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..core.preload import ITEMS, StartupClock

#: Starts a read: start(then, failed) → False when there is nothing to read (not configured, already read).
Start = Callable[[Callable[[], None], Callable[[str], None]], bool]

LOG_TAG = "SKYWATCH"


@dataclass
class Item:
    key: str
    label: str
    start: Start
    #: Read even when the settings file's `preload` does not list it (items added by other parts).
    always: bool = False


class Preloader(QObject):
    """The reads ahead (`plugin.preloader`): started by `schedule`, after QGIS has started."""

    #: An item's read ended: key, whether it was read, seconds, the reason when not.
    item_done = pyqtSignal(str, bool, float, str)
    #: Every read of the round ended.
    finished = pyqtSignal()

    #: How long after QGIS has started (and the settings file is read) the reads begin (ms).
    DELAY_MS = 1000
    #: A read not told done after this long counts as failed (s): the round still ends.
    TIMEOUT_S = 180

    def __init__(self, context, clock: Optional[StartupClock] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.context = context
        self.clock = clock or StartupClock()
        self.items: dict[str, Item] = {}
        #: The keys still being read in this round (one at a time).
        self.running: dict[str, float] = {}
        #: The items of this round not started yet.
        self._queue: list[Item] = []
        self._starting = False
        #: The round wanted while one runs: started when it ends.
        self._again = False
        self._round = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.run)
        self._closed = False

    def add(self, key: str, label: str, start: Start, always: bool = False) -> None:
        """An item to read ahead (one of core/preload.ITEMS, or another part's own with `always`)."""
        self.items[key] = Item(key, label, start, always)

    def wanted(self) -> list[Item]:
        chosen = set(getattr(self.context.config, "preload", ITEMS))
        return [i for i in self.items.values() if i.always or i.key in chosen]

    def schedule(self, delay_ms: Optional[int] = None) -> None:
        """Reads ahead in a moment (again after another settings file; a round running is let end first)."""
        if self._closed:
            return
        self._timer.start(self.DELAY_MS if delay_ms is None else delay_ms)

    def run(self) -> None:
        """Starts the wanted items' reads now, one after another."""
        if self._closed:
            return
        if self.running or self._queue:
            self._again = True
            return
        if getattr(self.context, "loading", False):
            return  # The settings file is still being read: asked again when it is.
        self._round += 1
        self._queue = list(self.wanted())
        self._start_next(self._round)

    def _start_next(self, round_: int) -> None:
        """Starts the next item that has something to read (none left: the round ends)."""
        self._starting = True
        try:
            self._start_from_queue(round_)
        finally:
            self._starting = False
        if not self.running and not self._queue and not self._closed and round_ == self._round:
            self._round_ended()

    def _start_from_queue(self, round_: int) -> None:
        while self._queue and not self._closed and round_ == self._round:
            item = self._queue.pop(0)
            t0 = self.clock.now()
            self.running[item.key] = t0

            def then(item=item, t0=t0) -> None:
                self._ended(round_, item, t0, True, "")

            def failed(message: str, item=item, t0=t0) -> None:
                self._ended(round_, item, t0, False, str(message))

            try:
                started = item.start(then, failed)
            except Exception as error:  # noqa: BLE001 (one item never keeps the others from being read)
                self.running.pop(item.key, None)
                self._log(f"{item.label}の先読みに失敗: {error}", Qgis.MessageLevel.Warning)
                continue
            if not started:
                self.running.pop(item.key, None)
                continue
            if item.key in self.running:
                # Being read (not told done at once): the next waits for it.
                QTimer.singleShot(int(self.TIMEOUT_S * 1000), lambda item=item, t0=t0: self._ended(round_, item, t0, False, "時間切れ"))
                return

    def _ended(self, round_: int, item: Item, t0: float, ok: bool, message: str) -> None:
        if self._closed or round_ != self._round or self.running.get(item.key) != t0:
            return  # told twice (done, then timed out), or of an older round
        del self.running[item.key]
        took = self.clock.now() - t0
        self.clock.read(item.label, took, ok, message)
        self.item_done.emit(item.key, ok, took, message)
        if not self.running and not self._starting:
            # (Told done while being started: the loop starting them goes on by itself.)
            if self._queue:
                # The next one after a moment (the main thread first shows what was read).
                QTimer.singleShot(0, lambda: self._start_next(round_))
            else:
                self._round_ended()

    def _round_ended(self) -> None:
        self.finished.emit()
        if self._again:
            self._again = False
            self.schedule(0)

    def _log(self, text: str, level=Qgis.MessageLevel.Info) -> None:
        QgsMessageLog.logMessage(text, LOG_TAG, level, notifyUser=False)

    def close(self) -> None:
        self._closed = True
        self._timer.stop()
        self.running.clear()
        self._queue.clear()
