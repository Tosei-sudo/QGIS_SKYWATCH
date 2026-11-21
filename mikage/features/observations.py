"""
観測記録: 観測記録 (with the status bar's 送信待ち), 分類体系の編集, 移動体（AIS・ADS-B）,
ダッシュボード, 「通報文の作成…」「通報記録の確認…」「自分の記録…」「観測記録のエクスポート…」
「観測記録の一括取込み…」「分担…」「引き継ぎメモ…」, and the offline cache of the classifications.
"""
from __future__ import annotations

from ..ui import command_runs as runs
from ..ui.dashboard_dock import DashboardDock
from ..ui.moving_dock import MovingDock
from ..ui.observation_dock import ObservationDock
from ..ui.taxonomy_dock import TaxonomyDock
from ..qgis_io import tasks
from .base import DockSpec, Feature, tr


class Observations(Feature):
    COMMANDS = ("edit_taxonomy",)

    def __init__(self, host):
        super().__init__(host)
        # Its port: 観測記録 (the images without files, the records of events and reports, the AI's and training's records).
        host.ports.observations = lambda: host.docks.get("SatObsObservations")

    def dock_specs(self) -> dict:
        iface, context, settings, window, docks = self.iface, self.context, self.host.open_settings, self.window(), self.docks
        return {
            "SatObsObservations": DockSpec("観測記録", "observations", self._make_observations),
            "SatObsTaxonomy": DockSpec("分類体系の編集", "taxonomy",
                                       lambda: TaxonomyDock(iface, context, settings, docks["SatObsObservations"], window)),
            "SatObsMoving": DockSpec("移動体（AIS・ADS-B）", "moving", lambda: MovingDock(iface, context, settings, window)),
            "SatObsDashboard": DockSpec("ダッシュボード", "dashboard", lambda: DashboardDock(iface, context, settings, window)),
        }

    def _make_observations(self) -> ObservationDock:
        offline = self.host.offline
        dock = ObservationDock(self.iface, self.context, self.host.open_settings, self.window(), edit_taxonomy=self.edit_taxonomy)
        # 送信待ち is one list (the status bar's); its records not saved open in this dock's form.
        dock.open_outbox = offline.open_list
        offline.open_form = lambda store, record_id, dock=dock: dock.open_draft(record_id, store)
        offline.changed.connect(dock.render_drafts)
        return dock

    def connect(self) -> None:
        dock = self.docks["SatObsObservations"]
        # 「イベントに追加」 of the records goes to 「イベント」.
        dock.event_linker = self.ports.events
        # A record's image not open opens through 「画像カタログ」 (開いた画像の履歴, else the catalog by its id).
        dock.catalog = self.ports.catalog

    def add_actions(self) -> None:
        dock = lambda: self.docks["SatObsObservations"]  # noqa: E731
        # 「通報」 (alertReports): the notable records as delimited text, and what was reported.
        for name, text, tab in (("alert", "通報文の作成…", 0), ("alert_log", "通報記録の確認…", 1)):
            self.entry(name, text, lambda tab=tab: dock().open_alerts(tab), name="SatObsAlert" if tab == 0 else "SatObsAlertLog")
        # 「自分の記録」: the records one made, across the images, to look over and fix (tsudo, 2026-11-18).
        self.entry("my_records", "自分の記録…", lambda: dock().open_my_records(), name="SatObsMyRecords")
        # 「観測記録のエクスポート」: the records of a period and a place as CSV / JSON (observationExports).
        self.entry("obs_export", "観測記録のエクスポート…", lambda: dock().open_export(), name="SatObsObservationExport")
        # 「観測記録の一括取込み」: a CSV or a layer into the chosen store, after a dry run.
        self.entry("obs_import", "観測記録の一括取込み…", lambda: dock().open_import(), name="SatObsObservationImport")
        # 「分担」 (assignments: who holds which image) and 「引き継ぎメモ」 (the shift handover).
        for name, text, tab in (("assignments", "分担…", 0), ("handover", "引き継ぎメモ…", 1)):
            self.entry(name, text, lambda tab=tab: dock().open_handover(tab), name="SatObsAssignments" if tab == 0 else "SatObsHandover")

    def text_operations(self) -> dict:
        c, dock = self.context.config, lambda: self.docks["SatObsObservations"]  # noqa: E731
        out = {"handover": lambda _r, _p: runs.handover(dock())}
        if c.observation_exports is not None:
            out["obsExport"] = lambda r, _p: runs.obs_export(dock(), r)
        if c.alert_reports is not None:
            out["alert"] = lambda _r, _p: runs.alert(dock())
        return out

    def preload(self) -> list:
        return [("offlineCache", tr("オフライン用の控え（観測記録の分類体系）"), self._warm_offline_cache, True)]

    def after_offline(self, sent: bool) -> None:
        dock = self.docks.get("SatObsObservations")
        if dock is None:
            return
        try:
            if sent:
                dock.reload()
            else:
                # Nothing sent or thrown away: only what others changed meanwhile (not every record again).
                dock.refresh(force=True)
        except RuntimeError:
            pass

    def edit_taxonomy(self, path=None) -> None:
        """Opens the classification editor (at a class)."""
        dock = self.docks.get("SatObsTaxonomy")
        if dock is None:
            return
        dock.show()
        dock.raise_()
        if hasattr(dock, "select"):
            dock.select(path)

    def _warm_offline_cache(self, then, failed) -> bool:
        """
        With the reads ahead (unless `preload` is false): every observation store's classification is read once, so
        it is in the offline cache even for a store not chosen today. The records and checked areas are not (2026-11-18,
        200 people: none reads a whole table; those of the images opened are kept as they are read).
        """
        context = self.context
        config = context.config
        if not config.offline_work.enabled or not config.observation_stores or not getattr(config, "preload", True):
            return False
        from ..core.obs.api import store_sets

        def work(task) -> int:
            sets = store_sets(config.observation_stores, config.observation_vocabulary, context.gpkg, context.esri,
                              context.token_for, local=False)
            n = 0
            for s in sets:
                # Only the server's classification (one kept in the GeoPackage is not read from this thread).
                reads = [s.taxonomy.load, s.taxonomy.load_fields] if s.config.taxonomy_table else []
                for read in reads:
                    if task.isCanceled():
                        return n
                    try:
                        read()
                        n += 1
                    except Exception:  # noqa: BLE001 (one table unreadable: the others are still kept)
                        pass
            return n

        tasks.run_task(tr("オフライン用の控えの更新"), work, lambda _n: then(), failed, hidden=True)
        return True

    def unload(self) -> None:
        for name in ("SatObsMoving", "SatObsDashboard"):
            if name in self.docks:
                self.docks[name].close_all()
