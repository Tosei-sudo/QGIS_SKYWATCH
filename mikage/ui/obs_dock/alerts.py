"""
観測記録ドックの通報 (ui/obs_dock, docs/architecture.md 段階7): the records' alerts as they are recorded
(alertReports: watch, check, notice) and the rows already reported. A part of ObservationDock
(ui/observation_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtWidgets import QPushButton

from ...core.obs.api import AlertLogRepository, ObservationBook, Observation
from ...qgis_io.tasks import run_task
from .common import _Saved


class AlertsPart:
    """観測記録ドックの通報."""

    def _alert_watch(self):
        """The alert settings when saved records are to be checked against them (`alertReports.notifyOnSave`)."""
        s = self.context.config.alert_reports
        return s if s is not None and s.notify_on_save else None

    def _watching(self, work: Callable, after: Optional[Callable]) -> tuple[Callable, Callable]:
        """`work` that also returns the records it added or changed, and `after` that checks them."""
        def watched(book: ObservationBook):
            before = {o.id: o.updated_at for o in book.records}
            result = work(book)
            return _Saved(result, [o for o in book.records if o.deleted_at is None and before.get(o.id) != o.updated_at])

        def then(result) -> None:
            saved = result if isinstance(result, _Saved) else _Saved(result, [])
            if after is not None:
                after(saved.result)
            if saved.changed:
                self.check_alerts(saved.changed)

        return watched, then

    def check_alerts(self, records: list[Observation]) -> None:
        """
        Checks records just saved against the conditions (the rules and
        geofences read once per settings file and kept 10 minutes, what was
        reported asked each time), in a task; those notable are said in the
        message bar with 「通報文を作成」.
        """
        from ...core.obs.api import load_geofences, load_rules, name_places, reported_ids, alert_select as select
        from ...core.obs.api import areas_at, points_of

        s = self._alert_watch()
        book = self.book
        if s is None or book is None:
            return
        client, now = self.context.esri(), time.monotonic()
        cached = self._alert_conditions if self._alert_conditions and self._alert_conditions[0] is s and now - self._alert_conditions[1] < 600 else None
        levels = book.confidences()
        history = list(book.records)

        def work(_task):
            if cached:
                rules, fences = cached[2], cached[3]
            else:
                rules = load_rules(client, s.rules, None) if s.rules else None
                fences = load_geofences(client, s.geofences, None) if s.geofences else []
            done = reported_ids(client, s, None, ids=[o.id for o in records]) if s.table else set()
            items = select(records, s, rules, fences, levels, done, history=history)
            if items and (s.named_areas or s.countries):
                # Only for those notable: their squares from the cache (place_cache), the server asked for the others.
                points = points_of(items)
                named = areas_at(client, s.named_areas, points, None) if s.named_areas else []
                countries = areas_at(client, s.countries, points, None) if s.countries else []
                name_places(items, named, countries, levels)
            return rules, fences, items

        def finished(result) -> None:
            rules, fences, items = result
            self._alert_conditions = (s, now if not cached else cached[1], rules, fences)
            if items and book is self.book:
                self._alert_notice(items)

        def failed(message: str) -> None:
            QgsMessageLog.logMessage(self.tr("通報の条件の確認に失敗: {m}").format(m=message), self.tr("観測記録"), Qgis.MessageLevel.Warning)

        run_task(self.tr("通報の条件の確認"), work, finished, failed)

    def _alert_notice(self, items) -> None:
        """
        「注目の記録」 in the message bar, with 「通報文を作成」 (those records put
        in 「通報」). One notice at a time: while it is still shown, the records
        of the next save join it (saving one after another does not stack them).
        """
        bar = self.iface.messageBar()
        shown = self._alert_widget is not None and any(w is self._alert_widget for w in bar.items())
        if shown:
            ids = {i.record.id for i in items}
            items = [i for i in self._alert_items if i.record.id not in ids] + list(items)
        names = "・".join(f"{i.record.object_name}（{i.record.activity}）" if i.record.activity else i.record.object_name for i in items[:3])
        more = self.tr(" ほか {n} 件").format(n=len(items) - 3) if len(items) > 3 else ""
        widget = bar.createMessage(self.tr("注目の記録"), self.tr("通報の条件に合う記録: {r}{m}").format(r=names, m=more))
        button = QPushButton(self.tr("通報文を作成"))

        def open_() -> None:
            self.open_alerts(0).add_items(items)
            bar.popWidget(widget)

        button.clicked.connect(open_)
        widget.layout().addWidget(button)
        if shown:
            bar.popWidget(self._alert_widget)
        bar.pushWidget(widget, Qgis.MessageLevel.Info, 0)
        self._alert_widget, self._alert_items = widget, list(items)

    def refresh_reported(self) -> None:
        """
        Reads which records are in the 通報 log table (in a task; none
        without `alertReports.table`); their rows and marks are highlighted.
        """
        s = self.context.config.alert_reports
        self._reported_asked += 1
        asked = self._reported_asked
        if s is None or s.table is None:
            if self.reported:
                self.reported = set()
                self.render()
            return
        repo = AlertLogRepository(self.context.esri(), s, None)

        def finished(ids) -> None:
            if asked != self._reported_asked:
                return  # asked again since
            if set(ids) != self.reported:
                self.reported = set(ids)
                self.render()

        def failed(message: str) -> None:
            QgsMessageLog.logMessage(self.tr("通報済みの記録の取得に失敗: {m}").format(m=message), self.tr("観測記録"),
                                     Qgis.MessageLevel.Warning)

        run_task(self.tr("通報済みの記録の取得"), lambda _t: repo.reported_ids(), finished, failed)

    def mark_reported(self, ids) -> None:
        """Records just put in the 通報 log table: highlighted at once (without reading the table again)."""
        new = set(ids) - self.reported
        if new:
            self.reported |= new
            self.render()
