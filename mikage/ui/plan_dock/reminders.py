"""
撮像計画ドックの通知と予定表 (ui/plan_dock, docs/architecture.md): the reminders before the passes and the calendar file. A part of ImagingPlanDock
(ui/imaging_plan_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import Qgis, QgsMessageLog, QgsRectangle
from qgis.PyQt.QtWidgets import (
    QFileDialog, QPushButton,
)

from ...core.plan.api import due_reminders, merge_reminders, new_reminders, plan_ics, reminder_of, reminder_text
from ...qgis_io import settings
from ...qgis_io.geometry import from_wgs84
from ..docks import ask_no, save_text
from .common import _now_ms


class RemindersPart:
    """撮像計画ドックの通知と予定表."""

    def reminders(self) -> list:
        return [r for r in (reminder_of(v) for v in settings.plan_reminders()) if r is not None]

    def count_reminders(self) -> None:
        n = sum(1 for r in self.reminders() if r.time > _now_ms())
        self.remind_count.setText(self.tr("通知予定 {n} 件").format(n=n) if n else "")

    def set_reminders(self) -> int:
        """Sets reminders before the listed shots."""
        plan = self.plan
        if not plan or not self.shown:
            return 0
        fresh = new_reminders(self.shown, plan["sats"], plan["targets"], self.remind_minutes.value(), self._rings_of, _now_ms())
        settings.set_plan_reminders([r.to_json() for r in merge_reminders(self.reminders(), fresh)])
        self.count_reminders()
        bar = self.iface.messageBar()
        if fresh:
            bar.pushInfo(self.tr("撮像計画"), self.tr("撮像 {n} 件の {m} 分前に通知（QGIS の起動中のみ）").format(n=len(fresh), m=self.remind_minutes.value()))
        else:
            bar.pushInfo(self.tr("撮像計画"), self.tr("今後の撮像がないため、通知の設定なし"))
        return len(fresh)

    def clear_reminders(self) -> None:
        n = len(self.reminders())
        if n and not ask_no(self, self.tr("通知をすべて解除"), self.tr("撮像の通知 {n} 件をすべて解除").format(n=n), yes=self.tr("解除")):
            return
        settings.set_plan_reminders([])
        self.count_reminders()
        self.iface.messageBar().pushInfo(self.tr("撮像計画"), self.tr("撮像の通知をすべて解除"))

    def fire_reminders(self, now: Optional[float] = None) -> int:
        """Shows the reminders that are due in the message bar and the log; returns how many."""
        now = _now_ms() if now is None else now
        kept = self.reminders()
        due, rest = due_reminders(kept, now)
        if len(rest) == len(kept):
            return 0
        settings.set_plan_reminders([r.to_json() for r in rest])
        for r in due:
            text = reminder_text(r, now)
            QgsMessageLog.logMessage(text, self.tr("撮像計画"), Qgis.MessageLevel.Info)
            self._notice(text, r.rings)
            if self.on_reminder is not None:
                self.on_reminder(r, text, now)
        if self.remind_row.isVisible():
            self.count_reminders()
        return len(due)

    def _notice(self, text: str, rings: list) -> None:
        """A notice in the message bar, with 「移動」 to the shot's scenes."""
        bar = self.iface.messageBar()
        widget = bar.createMessage(self.tr("撮像の通知"), text)
        if rings:
            go = QPushButton(self.tr("移動"))

            def zoom() -> None:
                box = QgsRectangle()
                box.setNull()
                for ring in rings:
                    for x, y in ring:
                        box.combineExtentWith(x, y)
                crs = self.canvas.mapSettings().destinationCrs()
                self.canvas.setExtent(from_wgs84(crs).transformBoundingBox(box.buffered(max(box.width(), box.height()) * 0.5 + 0.01)))
                self.canvas.refresh()

            go.clicked.connect(zoom)
            widget.layout().addWidget(go)
        bar.pushWidget(widget, Qgis.MessageLevel.Info, 0)

    def default_ics_name(self) -> str:
        return self.default_file_name()[:-4] + ".ics"

    def save_ics(self, path: Optional[str] = None) -> None:
        """Saves the listed shots as an iCalendar file."""
        plan = self.plan
        if not plan or not self.shown:
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("予定表 (.ics)"), self.default_ics_name(), self.tr("iCalendar (*.ics)"))
        if not path:
            return
        failed = save_text(path, plan_ics(self.shown, plan["sats"], plan["targets"], self.remind_minutes.value(), _now_ms()), newline="",
                           what="imaging-plan-ics")
        if failed:
            self.iface.messageBar().pushWarning(self.tr("撮像計画"), self.tr("予定表を保存できない: {m}").format(m=failed))
            return
        self.iface.messageBar().pushSuccess(self.tr("撮像計画"), self.tr("撮像の予定を予定表（.ics）で保存"))
