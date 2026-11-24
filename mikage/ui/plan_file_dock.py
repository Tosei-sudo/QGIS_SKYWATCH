"""
The 「撮像計画ファイル出力」 dock: the day's work of writing the imaging plan
files from the request ledger (`requestStores`, `requestExports`). It is a
dock of its own, apart from 「撮像依頼」, since those writing the files are
not those registering requests (tsudo, 2026-11-07).

「計算」 reads the ledger and the history rows still open, asks the cloud
forecast for the optical requests, works out the satellites' passes over
them from their orbits (core/request/passes.py) and fits the day's
requests into the passes and the systems' limits. Requests put into one
shot (まとめ) say so in 結果・理由 (tsudo, 2026-11-11), and the time of the
pass is in 撮像時刻. 「地図に表示」 draws the requests shown with their own
areas and, dashed, the shots the files are written with (the outline of
the scenes, a merged row's area), before anything is written. The table
shows every candidate: ticking 採用 or not,
moving rows up and down and ticking a raise are kept and the fitting is
done again. 「書き出し」 writes one file per system (CSV, or JSON checked
against its schema), then the history and the raises taken.
「上限シミュレーション…」 tries other daily limits over the coming days
without writing anything (limit_sim_dialog.py).

The table can be narrowed to a system, a satellite series, a 依頼グループ, a
result and words (core/request/view.py): the files of each system are
written by different people, so the system, series and group chosen are
kept (per user). The group only narrows what is shown.
With a system chosen, 「書き出し」 writes that system's file only, with its
history rows and raises (tsudo, 2026-11-09); the requests another system's
person wrote earlier that day stay in that system (pinned, counted in the
status after 計算), and writing stops when someone wrote meanwhile (計算
again). 「すべてのシステム」
writes every system's file, as before.
"""
from __future__ import annotations

import dataclasses
import os
import time
from datetime import date
from typing import Optional

from qgis.core import QgsRectangle
from qgis.PyQt.QtCore import QCoreApplication, QDate, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableView,
)

from ..core import audit, errors
from ..core.catalog.api import CatalogConfig
from ..core.esri import EsriError
from ..core.named_areas import settings_for as named_area_settings
from ..core.plan.api import attach, on_day, summary_text
from ..core.plan.api import fetch_clouds
from ..core.request.api import ahead_span, chances_of, ScheduleRepository, wanted
from ..core.request.api import (
    HELD, TAKEN, chance_text, cloud_tip, cover_text, kind_text, merge_text, occurrence_text, pace_text, part_text, pass_text,
    room_text, satellite_name, daily_time_text as time_text,
)
from ..core.request.api import convert_priority, encoded, files_of, leftover_files, render, row_rings, write
from ..core.request.api import missed_ids
from ..core.request.api import follow_areas, redo_count
from ..core.request.api import look_text
from ..core.request.api import day_bounds, jst_day, priority_text
from ..core.request.api import day_passes, find_passes
from ..core.request.api import QuotaData
from ..core.request.api import UseBook
from ..core.request.api import system_names
from ..core.request.api import BOOST, NO_SYSTEM, CandidateFilter, series_choices, shown
from ..core.request.api import (
    RequestStoreConfig, Saved, followed_where, handed_where,
    history_where, replaced_where, shots_replaced_where,
)
from ..core.request.api import (
    DayPlan, Rewrite, boost_changes, clouds_of, forecast_places, history_of, keep_answers, missed_done, new_run_id, not_written, old_tle_text,
    orbiting, plan_day, radar_of, shots_of, written_summary,
)
from ..qgis_io import settings
from ..qgis_io.geometry import from_wgs84
from ..qgis_io.network import get_bytes
from ..qgis_io.request_io import load_schema
from ..qgis_io.tasks import error_text, run_task
from .docks import PluginDock, plain_label, plain_tip
from .flow_layout import FlowLayout
from .list_views import column_menus
from .memo_column import MEMO, memo_item
from .plan_overlay import PlanOverlay
from .check_filter import CheckFilter
from .request_groups import GroupFilter

#: 「メモ」: the request's note, hidden until shown from the header's menu (tsudo 2026-11-24).
COLUMNS = ("採用", "システム", "衛星", "撮像時刻", "依頼", "依頼グループ", "区分", "優先度", "雲量", "枚数", "結果・理由", "繰り上げ", MEMO)
#: The column of 結果・理由.
RESULT_COLUMN = COLUMNS.index("結果・理由")
#: The column of 繰り上げ (checked to take the raise).
BOOST_COLUMN = COLUMNS.index("繰り上げ")
#: How 「該当システムなし」 is kept in the settings (an empty value is 「すべてのシステム」).
_NO_SYSTEM_KEPT = "*none*"


def _now_ms() -> float:
    return time.time() * 1000


class PlanFileDock(PluginDock):
    """The day's candidates, changes by hand, and writing the files."""

    def __init__(self, iface, context, open_settings, satellites, requests_changed=None, parent=None):
        super().__init__("SatObsPlanFiles", QCoreApplication.translate("PlanFileDock", "撮像計画ファイル出力"), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        #: The requests' areas and the shots on the map (「地図に表示」).
        self.overlay = PlanOverlay(self.canvas)
        #: The candidate chosen in the table (drawn thick, its shot's requests too).
        self.chosen = None
        #: Calls back with the satellites to plan with (the imaging plan's catalog), once they are read.
        self.satellites = satellites
        #: Told when the ledger was read or changed here (the 「撮像依頼」 dock shows it again).
        self.requests_changed = requests_changed or (lambda requests=None: None)
        self.store: Optional[RequestStoreConfig] = None
        self.plan: Optional[DayPlan] = None
        self.task = None
        #: The 上限シミュレーション window, while open.
        self.simulation = None
        self._rendering = False
        #: The candidates the table shows (row → candidate), after the filters.
        self.shown: list = []
        #: The system and series filters kept from last time, until their choices are there.
        self._kept_filter = settings.plan_file_filter()
        #: The files last written.
        self.paths: list = []
        #: The 依頼グループ of the store's table, as last read (core/request/groups.py).
        self.groups: list = []
        #: The 「計画と実績の照合」 dialog, once opened.
        self.results = None
        #: The 「年間撮像上限」 dialog, while open.
        self.quota_window = None
        self._build()
        context.configChanged.connect(self._config_changed)
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()

    def _visibility(self, visible: bool) -> None:
        if visible:
            self.draw()
        else:
            self.overlay.unpaint()
            self.canvas.refresh()

    def _build(self) -> None:
        layout = self.body
        self.store_choice = QComboBox()
        self.store_choice.setToolTip(self.tr("依頼の保存先"))
        self.store_choice.currentIndexChanged.connect(self._store_chosen)
        layout.addWidget(self.store_choice)
        # Rows of buttons and filters wrap, so that a dock at its usual width shows them all (and 「書き出し」)
        # without scrolling sideways.
        top = FlowLayout()
        top.addWidget(QLabel(self.tr("対象日（日本時間）")))
        self.day = QDateEdit()
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        today = jst_day(_now_ms())
        self.day.setDate(QDate(today.year, today.month, today.day))
        top.addWidget(self.day)
        self.run_button = QPushButton(self.tr("計算"))
        self.run_button.setToolTip(self.tr("撮像済みの確認、最新の依頼の取得、雲量予報のあと、その日の依頼を枠に詰める"))
        self.run_button.clicked.connect(lambda: self.run())
        top.addWidget(self.run_button)
        self.sim_button = QPushButton(self.tr("上限シミュレーション…"))
        self.sim_button.setToolTip(self.tr("日ごとの上限や使うシステムを変えたときの枠超過・期限切れを、今後の日数で試算（依頼は変わらない）"))
        self.sim_button.clicked.connect(self.open_simulation)
        top.addWidget(self.sim_button)
        self.results_button = QPushButton(self.tr("照合…"))
        self.results_button.setToolTip(self.tr("出力した依頼が撮像されたかを画像カタログと照合（計画と実績の照合）"))
        self.results_button.clicked.connect(lambda: self.show_results())
        top.addWidget(self.results_button)
        self.quota_button = QPushButton(self.tr("年間上限…"))
        self.quota_button.setToolTip(self.tr("センサー・モードごとの年間撮像上限の消費と残り、撮像ごとの「数えない」（ノイズ・ジャミングなど）"))
        self.quota_button.clicked.connect(lambda: self.show_quotas())
        top.addWidget(self.quota_button)
        layout.addLayout(top)
        # Request names and the services' messages: shown as written, never as HTML.
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        filters = FlowLayout()
        self.system_filter = QComboBox()
        self.system_filter.setToolTip(self.tr("このシステムの依頼だけを表示（見送りは、そのシステムで撮れるものも）。次回も同じ選択"))
        self.series_filter = QComboBox()
        self.series_filter.setToolTip(self.tr("この衛星シリーズの依頼だけを表示（採用は割り当てた衛星、見送りは撮れる衛星のシリーズ）。次回も同じ選択"))
        # 依頼グループ: only what is shown (the files are written per system, whoever's the requests are).
        self.group_filter = GroupFilter("plan_files")
        self.group_filter.setToolTip(self.tr("依頼グループで絞り込み（チェックしたグループのどれか。表示だけで、書き出しはシステム単位）。次回も同じ選択"))
        # 結果 and 依頼グループ: check boxes, several at once (rows of any of those ticked shown).
        self.result_filter = CheckFilter(
            self.tr("すべての結果"), [(TAKEN, TAKEN, "", "#16a34a"), (HELD, HELD, "", "#9ca3af"),
                                    (self.tr("繰り上げ候補"), BOOST, self.tr("優先度の繰り上げを勧める依頼（未撮像になったもの・期限まで 2 日以内の期間内1回）"), "#b45309")],
            key="plan_files/results", tip=self.tr("この結果の依頼だけを表示（チェックしたもののどれか）。次回も同じ選択"))
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(self.tr("名称・ID・番号・衛星"))
        self.search.setToolTip(self.tr("名称・依頼ID・管理番号・識別番号・依頼グループ・割り当てた衛星で絞り込み（空白で区切るとすべてを含むもの）"))
        self.search.setMinimumWidth(160)
        for w in (self.system_filter, self.series_filter, self.group_filter, self.result_filter):
            # Long system or series names do not widen the dock.
            w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(14)
            filters.addWidget(w)
        for w in (self.system_filter, self.series_filter):
            w.currentIndexChanged.connect(self._filter_changed)
        for w in (self.group_filter, self.result_filter):
            w.changed.connect(self._filter_changed)
        self.search.textChanged.connect(lambda _text: self.render())
        filters.addWidget(self.search)
        layout.addLayout(filters)
        self.model = QStandardItemModel(self)
        self.model.itemChanged.connect(self._item_changed)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().currentRowChanged.connect(lambda current, _: self._row(current.row()))
        layout.addWidget(self.table, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.table_columns = column_menus("plan-files", self.table, self, hidden=(self.tr(MEMO),))
        moves = FlowLayout()
        self.up_button = QPushButton(self.tr("上へ"))
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button = QPushButton(self.tr("下へ"))
        self.down_button.clicked.connect(lambda: self.move(1))
        self.reset_button = QPushButton(self.tr("手動の変更を戻す"))
        self.reset_button.clicked.connect(self.reset)
        for b in (self.up_button, self.down_button, self.reset_button):
            moves.addWidget(b)
        self.on_map = QCheckBox(self.tr("地図に表示"))
        self.on_map.setChecked(True)
        self.on_map.setToolTip(self.tr("表示中の依頼の範囲（採用は緑、まとめは青、見送りは灰）と、書き出す撮像の範囲（破線）を地図に表示。"
                                       "行を選ぶとその依頼へ移動し、同じ撮像の依頼も太線で表示"))
        self.on_map.toggled.connect(lambda _on: self.draw())
        moves.addWidget(self.on_map)
        layout.addLayout(moves)
        # What each system has left of its day: a line of its own (beside the buttons it was squeezed to a
        # column one character wide).
        self.room = QLabel()
        self.room.setWordWrap(True)
        layout.addWidget(self.room)
        # What the yearly quotas of the day's satellites have left (年間撮像上限) and how long the satellites with use time
        # limits are used (軌道の使用時間), after the day's fitting.
        self.quota_text = plain_label(QLabel())
        self.quota_text.setWordWrap(True)
        self.quota_text.hide()
        layout.addWidget(self.quota_text)
        out = QHBoxLayout()
        out.addWidget(QLabel(self.tr("出力先")))
        self.folder = QLineEdit(settings.request_export_folder())
        self.folder.setPlaceholderText(self.tr("設定ファイルで folder を指定していないシステムの出力先"))
        out.addWidget(self.folder, 1)
        browse = QPushButton(self.tr("参照…"))
        browse.clicked.connect(self._browse)
        out.addWidget(browse)
        self.write_button = QPushButton(self.tr("書き出し"))
        self.write_button.setToolTip(self.tr("採用の依頼をシステムごとのファイルに書き出し、出力履歴と繰り上げを保存。"
                                             "システムを選んでいればそのシステムの分だけ（ほかのシステムのファイル・出力履歴はそのまま）"))
        self.write_button.clicked.connect(lambda: self.write())
        out.addWidget(self.write_button)
        layout.addLayout(out)
        self._update_buttons()

    def show_quotas(self):
        """Opens 「年間撮像上限」 for the chosen store, on the year of the chosen day."""
        if self.store is None:
            return None
        from .quota_dialog import QuotaDialog
        if self.quota_window is not None:
            self.quota_window.close()
        self.quota_window = QuotaDialog(self.context, self.store, self.chosen_day(), self)
        self.quota_window.show()
        self.quota_window.run()
        return self.quota_window

    def show_results(self, run: bool = True):
        """Opens 「計画と実績の照合」 (and follows the history up)."""
        from .plan_results_dialog import PlanResultsDialog
        if self.results is None:
            self.results = PlanResultsDialog(self)
        self.results.show()
        self.results.raise_()
        if run:
            self.results.run()
        return self.results

    def _config_changed(self) -> None:
        stores = self.context.config.request_stores
        self.store_choice.blockSignals(True)
        self.store_choice.clear()
        for s in stores:
            self.store_choice.addItem(s.label)
        self.store_choice.blockSignals(False)
        self.store_choice.setVisible(len(stores) > 1)
        self.store = stores[0] if stores else None
        self.plan = None
        self.groups = []
        self.group_filter.set_groups([], ())
        self._fill_systems()
        self._fill_series()
        if self.results is not None:
            self.results.close()
            self.results = None
        if self.quota_window is not None:
            self.quota_window.close()
            self.quota_window = None
        self.render()
        self.status.setText(self.tr("撮像依頼の保存先が未設定（管理者に連絡: requestStores）［E703］") if self.store is None else "")

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.request_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self.plan = None
        self.groups = []
        self.group_filter.set_groups([], ())
        self.render()

    # --- Filters --------------------------------------------------------------------------------

    def _fill_systems(self) -> None:
        """The system filter's choices: every system of the settings, and 「該当システムなし」."""
        box = self.system_filter
        was = box.currentData() if box.count() else None
        if was is None and self._kept_filter.get("system"):
            kept = self._kept_filter["system"]
            was = NO_SYSTEM if kept == _NO_SYSTEM_KEPT else kept
        box.blockSignals(True)
        box.clear()
        box.addItem(self.tr("すべてのシステム"), None)
        for s in self.context.config.request_exports:
            box.addItem(s.label if s.label == s.system else f"{s.label}（{s.system}）", s.system)
        box.addItem(self.tr("該当システムなし"), NO_SYSTEM)
        index = box.findData(was) if was is not None else 0
        box.setCurrentIndex(max(0, index))
        box.blockSignals(False)

    def _fill_series(self) -> None:
        """The series filter's choices: those of the day's candidates, and the systems' own."""
        box = self.series_filter
        was = box.currentData() if box.count() else None
        if was is None and self._kept_filter.get("series"):
            was = self._kept_filter["series"]
        box.blockSignals(True)
        box.clear()
        box.addItem(self.tr("すべてのシリーズ"), None)
        names = series_choices(self.plan.candidates if self.plan else [], self.context.config.request_exports)
        if was and was.lower() not in {n.lower() for n in names}:
            names.append(was)
        for name in names:
            box.addItem(name, name)
        index = next((i for i in range(box.count()) if was and str(box.itemData(i) or "").lower() == was.lower()), 0)
        box.setCurrentIndex(index)
        box.blockSignals(False)

    def target(self) -> Optional[str]:
        """The system whose file 「書き出し」 writes: the one chosen, or None for all of them (and for 該当システムなし)."""
        return self.system_filter.currentData() or None

    def candidate_filter(self) -> CandidateFilter:
        """The filters as chosen."""
        return CandidateFilter(system=self.system_filter.currentData(), series=self.series_filter.currentData(),
                               result=tuple(self.result_filter.chosen()), text=self.search.text(),
                               group=tuple(self.group_filter.chosen()), mine=self.group_filter.my)

    def _filter_changed(self, _index: int = 0) -> None:
        system, series = self.system_filter.currentData(), self.series_filter.currentData()
        kept = _NO_SYSTEM_KEPT if system == NO_SYSTEM else system or ""
        self._kept_filter = {"system": kept, "series": series or ""}
        settings.set_plan_file_filter(kept, series or "")
        if self.plan is not None and self.task is None and self.plan.target != self.target():
            # What the other systems' people wrote is pinned only when another system is written from here.
            self.plan.target = self.target()
            self.plan.refit()
        self.render()

    def open_simulation(self) -> None:
        """Opens 「上限シミュレーション」 for the chosen store, from the chosen day."""
        if self.store is None:
            return
        from .limit_sim_dialog import LimitSimDialog
        if self.simulation is not None:
            self.simulation.close()
        self.simulation = LimitSimDialog(self.context, self.store, self.satellites, self.chosen_day(), self)
        self.simulation.show()

    def token(self) -> Optional[str]:
        return self.context.token_for(self.store.url, self.store.token) if self.store else None

    def _area_follow(self) -> Optional[tuple]:
        """
        For 地域の形に合わせる before 「計算」: the key its last look is kept
        under, the named area settings, their token, and the layer's last edit
        time and when it was looked at (None without the named area layer).
        """
        areas = named_area_settings(self.context.config)
        if areas is None or self.store is None:
            return None
        key = f"{self.store.url}|{areas.areas.url}"
        return key, areas, self.context.token_for(areas.areas.url, areas.areas.token), settings.area_follow_stamp(key)

    def repository(self) -> ScheduleRepository:
        """The store's reads and writes (データ窓口), its token as it is now."""
        return ScheduleRepository(self.context.esri(), self.store, self.token())

    def close_all(self) -> None:
        """Stops nothing running but forgets the day (when the plugin is unloaded)."""
        self.plan = None
        self.chosen = None
        self.overlay.unpaint()
        if self.simulation is not None:
            self.simulation.close()
            self.simulation = None
        if self.results is not None:
            self.results.close()
            self.results = None
        if self.quota_window is not None:
            self.quota_window.close()
            self.quota_window = None

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, self.tr("出力先"), self.folder.text())
        if folder:
            self.folder.setText(folder)

    def chosen_day(self) -> date:
        d = self.day.date()
        return date(d.year(), d.month(), d.day())

    def catalog(self) -> Optional[CatalogConfig]:
        store, catalogs = self.store, self.context.config.image_catalogs
        if store is None:
            return None
        if store.catalog:
            return next((c for c in catalogs if c.label == store.catalog), None)
        return catalogs[0] if catalogs else None

    # --- Working out the day --------------------------------------------------------------------

    def run(self) -> None:
        """Works the day out (satellites first, then the services in a task)."""
        if self.task is not None or self.store is None:
            return
        self.status.setText(self.tr("衛星カタログを取得中…"))
        self.run_button.setEnabled(False)
        self.satellites(self._run_with, self._satellites_failed)

    def _satellites_failed(self, message: str) -> None:
        """The satellite catalog could not be read: says why and gives 「計算」 back (#70)."""
        self.status.setText(self.tr("衛星カタログの取得に失敗: {m}").format(m=message))
        self._update_buttons()

    def _run_with(self, sats) -> None:
        store = self.store
        config = self.context.config
        systems = config.request_exports
        day = self.chosen_day()
        repo = self.repository()
        forecast = config.cloud_forecast
        authcfg = settings.auth_for(forecast.url) if forecast else None
        now = _now_ms()
        catalog = self.catalog()
        catalog_token = self.context.token_for(catalog.url, catalog.token) if catalog else None
        follow = self._area_follow()
        self.status.setText(self.tr("依頼と雲量予報を取得中…"))

        def work(task):
            notes = []
            # 衛星の障害情報 as they are now: each satellite (mode) carries those that hit it (their passes are left out),
            # and the day's satellites their stricter values (outages.on_day; the same order, the passes' indices).
            outages, problem = self.context.read_outages(repo.client, task.isCanceled)
            if problem:
                notes.append(problem)
            hit = attach(sats, outages, system_names(systems))
            day_sats = on_day(hit, day, now)
            requests = repo.load_requests(task.isCanceled)
            # 地域の形に合わせる (tsudo 2026-11-24): the requests following an area whose shape was changed (in ArcGIS Pro too)
            # take it before the day is worked out (only when the named area layer was edited since the last time).
            followed = None
            if follow is not None and any(r.named_area_id and r.area_follow for r in requests):
                areas, area_token, (since, checked) = follow[1:]
                try:
                    followed = follow_areas(repo.client, store, repo.token, areas, requests, now, since, area_token, task.isCanceled,
                                            checked)
                    requests = followed.requests
                    if followed.result is not None:
                        notes += [t for t in (followed.result.text(),) if t] + followed.result.problems
                except Exception as error:  # noqa: BLE001 (the day goes on with the requests' areas as they are, said so)
                    notes.append(f"地域の形に合わせられず、依頼の今の範囲で計算（{error_text(error)}）")
            groups, note = repo.read_groups(task.isCanceled)
            if note:
                notes.append(note)
            # Only the rows still to be followed up or raised for, not the whole (ever growing) table. They are not
            # followed up here (tsudo, 2026-11-11: 「その照合、計算時の照合なくして」): as 照合 (「照合…」, the
            # scheduled scheduled_follow.py) left them.
            history = (repo.load_history(history_where(store, open_rows=True), cancelled=task.isCanceled, shapes=True)
                       if store.history else [])
            task.progress(50)
            # What of the 期間内1回 areas is taken already (the catalog's images of their 撮像済み rows) or handed out on
            # earlier days and not followed up yet (撮像待ち): left out of the day's shots (covered.py).
            covered = {}
            if store.history:
                try:
                    covered, more = repo.read_covered(catalog, catalog_token, requests, day, history,
                                                      self.context.satellite_names(repo.client, task.isCanceled), task.isCanceled)
                    notes += more
                except Exception as error:  # noqa: BLE001 (the day goes on with the whole areas, said so)
                    notes.append(f"撮像済み・撮像待ちの範囲を読めず、範囲全体で計算（{error_text(error)}）")
            task.progress(60)
            forecasts = {}
            # Also for the requests missed and carried over to this day (they are candidates too).
            places = forecast_places(requests, day, day_sats, missed_ids(history))
            if places and forecast is not None:
                try:
                    forecasts = fetch_clouds(forecast, places, lambda url: get_bytes(url, authcfg), task.isCanceled)
                except Exception as error:  # noqa: BLE001 (the day goes on without the weather)
                    notes.append(f"雲量予報の取得に失敗（{error_text(error)}）")
            elif places:
                notes.append("雲量予報なし（cloudForecast: false）")
            task.progress(70)
            # The satellites' passes over the day's requests, from their orbits (once; the hand's changes fit again on them).
            passes = day_passes(requests, day, hit, now, task.isCanceled)
            task.check()
            if passes.problems:
                notes.append("TLE を読めない衛星は軌道なしで件数だけで詰める（" + "、".join(passes.problems[:3]) + "）")
            # The passes of the next days over the 期間内1回 requests: their chances left (改修案 4, chances.py).
            ahead = None
            looked = wanted(requests, day, missed_ids(history)) if store.look_ahead_days > 0 else []
            if looked:
                first, last = ahead_span(day, store.look_ahead_days)
                ahead = find_passes(looked, first, last, hit, now, task.isCanceled)
                task.check()
            # How each 定点 request's last image was taken: the passes like it first (改修案 5, geometry.py).
            last_looks = {}
            if store.same_geometry:
                try:
                    last_looks = repo.read_last_looks(requests, day, task.isCanceled)
                except Exception as error:  # noqa: BLE001 (the day goes on without it, said so)
                    notes.append(f"前回の撮り方を読めず、それなしで計算（{error_text(error)}）")
            quotas = QuotaData()
            if store.quotas:
                # What the yearly quotas have left (年間撮像上限): the shots of their years, as 照合 left them.
                try:
                    quotas = repo.read_quota_data([day], task.isCanceled)
                except Exception as error:  # noqa: BLE001 (the day goes on without the quotas, said so)
                    notes.append(f"年間撮像上限を読めず、上限なしで計算（{error_text(error)}）")
                notes += quotas.problems[:3]
            others = []
            if store.shots and any(s.use_time.limited for s in systems):
                # 軌道の使用時間: the shots of the days before and after handed out on the day's passes count too.
                try:
                    others = repo.read_other_uses(day, task.isCanceled)
                except Exception as error:  # noqa: BLE001 (the day goes on with its own shots only, said so)
                    notes.append(f"前後の日の撮像の使用時間を読めず、この日の撮像だけで計算（{error_text(error)}）")
            outage_line = summary_text(day_sats, *day_bounds(day), now)
            if outage_line:
                notes.insert(0, outage_line)
            redo = redo_count(history, requests, jst_day(now))
            if redo:
                notes.append(f"地域の形が変わって出し直し候補の行 {redo} 件（撮像予定の理由欄）")
            return (requests, history, forecasts, passes, notes, groups, quotas, covered, (looked, ahead), last_looks, others,
                    (day_sats, outages), followed)

        def done(result):
            self.task = None
            (requests, history, forecasts, passes, notes, groups, quotas, covered, (looked, ahead), last_looks, others,
             (sats, outages), followed) = result
            self.context.set_outages(outages)
            if followed is not None and follow is not None:
                # Kept when nothing was held back (those are tried again at the next 「計算」).
                if followed.stamp is not None and not (followed.result and followed.result.held):
                    settings.set_area_follow_stamp(follow[0], followed.stamp, now)
            if groups is not None:
                self.groups = groups
            self.group_filter.set_groups(self.groups, (r.group for r in requests))
            self.requests_changed(requests)
            # At each pass's moment for the satellites fitted on the orbits; the daylight hours' mean for the others.
            used = orbiting(systems, sats)
            clouds = clouds_of(requests, day, forecasts, passes=passes, sats=used, radar=radar_of(sats))
            book = quotas.book(day) if quotas.quotas else None
            chances = chances_of(looked, day, passes, ahead, sats, forecasts, used, clouds, store.look_ahead_days) if ahead else None
            self.plan = plan_day(requests, day, systems, sats, clouds, history, self.target(), passes, book, covered, chances,
                                 last_looks, UseBook(others))
            self._fill_series()
            self.render()
            # The shots of an area shared out past its first are rows of it, not requests of their own.
            own = [c for c in self.plan.candidates if c.main is None]
            taken = sum(1 for c in own if c.result == TAKEN)
            text = self.tr("{day}: 候補 {n} 件・採用 {t} 件・見送り {h} 件").format(day=day.isoformat(), n=len(own), t=taken,
                                                                         h=len(own) - taken)
            pinned = sum(1 for c in self.plan.candidates if c.pinned)
            if pinned:
                text += self.tr("（うち ほかのシステムで書き出し済み {n} 件）").format(n=pinned)
            text += self.merged_text() + self.shared_text() + self.chances_text()
            if not systems:
                text += self.tr("（設定ファイルに requestExports なし）")
            # The orbits it was placed on from TLEs far from the day (改修案 10).
            old = old_tle_text(self.plan)
            self.status.setText("。".join([text, *notes, *([old] if old else [])]))
            self._update_buttons()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("計算に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("本日分の計算"), work, done, failed)

    def merged_text(self) -> str:
        """「・まとめ 2 組（5 件）」: requests put into one shot, after a calculation."""
        if self.plan is None:
            return ""
        leads = [c for c in self.plan.candidates if c.result == TAKEN and c.merged and not c.member]
        if not leads:
            return ""
        return self.tr("・まとめ {n} 組（{m} 件を {n} 回の撮像に）").format(n=len(leads), m=sum(len(c.together) for c in leads))

    def chances_text(self) -> str:
        """「・残りの撮像機会が少なく先に置いた依頼 2 件・晴れる日を待つ依頼 1 件」: what the chances did, after a calculation."""
        if self.plan is None:
            return ""
        own = [c for c in self.plan.candidates if c.main is None and not c.extra]
        scarce = sum(1 for c in own if c.scarce)
        later = sum(1 for c in own if chance_text(c) and c.later)
        parts = []
        if scarce:
            parts.append(self.tr("残りの撮像機会が少なく先に置いた依頼 {n} 件").format(n=scarce))
        if later:
            parts.append(self.tr("晴れる日を待つ依頼 {n} 件").format(n=later))
        return "・" + "・".join(parts) if parts else ""

    def shared_text(self) -> str:
        """「・分担 2 件（5 回の撮像に）」: areas too big for one shot handed out in shots of several satellites or passes."""
        if self.plan is None:
            return ""
        heads = [c for c in self.plan.candidates if c.result == TAKEN and c.part == 1 and c.parts > 1]
        if not heads:
            return ""
        return self.tr("・分担 {n} 件（{m} 回の撮像に）").format(n=len(heads), m=sum(c.parts for c in heads))

    # --- The table ------------------------------------------------------------------------------

    def render(self) -> None:
        self._rendering = True
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        plan = self.plan
        chosen = self.candidate_filter()
        self.shown = shown(plan.candidates, chosen) if plan else []
        for c in self.shown:
            r = c.request
            take = QStandardItem()
            take.setCheckable(True)
            take.setCheckState(Qt.CheckState.Checked if c.result == TAKEN else Qt.CheckState.Unchecked)
            take.setToolTip(self.tr("外すと見送り、入れると枠を超えても採用"))
            prio = f"{priority_text(r.priority)} → {c.priority}"
            if c.system:
                prio += f"（{convert_priority(c.priority, c.system.priority)}）"
            cloud = "" if not (c.weather or c.mixed) else ("—" if c.forecast is None else f"{round(c.forecast)}%")
            reason = c.reason if c.result == HELD else ""
            merged = merge_text(c) if c.result == TAKEN else ""
            note = "・".join(x for x in (reason, merged, part_text(c), cover_text(c), kind_text(c), chance_text(c), look_text(c), pace_text(c), *c.warnings) if x)
            boost = QStandardItem(f"+{c.boost_offer - r.boost}（{c.boost_reason}）" if c.boost_offer is not None else "")
            if c.boost_offer is not None:
                boost.setCheckable(True)
                boost.setCheckState(Qt.CheckState.Checked if c.boost_taken else Qt.CheckState.Unchecked)
                boost.setToolTip(self.tr("チェックで優先度を 100 繰り上げ（書き出しのときに台帳へ保存）"))
            taken = c.result == TAKEN
            scenes = (self.tr("同乗") if c.member else str(c.scenes)) if taken else ""
            times = occurrence_text(c)
            items = [take, *(QStandardItem(t) for t in (
                c.system.label if c.system else "", satellite_name(c), time_text(c.planned) if taken else "",
                f"{r.name}（{times}）" if times else r.name, r.group,
                r.category, prio, cloud, scenes, (c.result + ("：" + note if note else "")))), boost, memo_item(r.note)]
            result = items[RESULT_COLUMN]
            color = QColor("#16a34a" if taken else "#6b7280")
            if taken and merged:
                # Put together with others (まとめ): told apart from the rest.
                color = QColor("#2563eb")
            result.setForeground(QBrush(color))
            if c.warnings:
                result.setForeground(QBrush(QColor("#b45309")))
            tip = pass_text(c) if taken else ""
            if tip:
                for item in items[2:4]:
                    item.setToolTip(plain_tip(tip))
            if merged:
                result.setToolTip(plain_tip(merged))
            if c.weather or c.mixed:
                items[COLUMNS.index("雲量")].setToolTip(plain_tip(cloud_tip(c)))
            self.model.appendRow(items)
        self._rendering = False
        # The day's limits of each system (システムの障害 may make them stricter).
        rooms = (room_text(plan.candidates, plan.systems, plan.sats, plan.day, plan.passes.now if plan.passes is not None else None)
                 if plan else {})
        listed = [s for s in (plan.systems if plan else []) if not chosen.system or s.system == chosen.system]
        total = len(plan.candidates) if plan else 0
        parts = [self.tr("表示 {n}/{m} 件").format(n=len(self.shown), m=total)] if len(self.shown) != total else []
        self.room.setText("　".join(parts + [f"{s.label} {rooms[s.system]}" for s in listed]))
        self.quota_text.setText("")
        lines, low = [], False
        if plan is not None and plan.quotas is not None and plan.quotas.usages:
            able = {id(s): s for c in plan.candidates for group in c.able.values() for s in group}
            texts = plan.quotas.texts(able.values())
            lines += [self.tr("年間上限 ") + t + (self.tr("　※残りわずか") if warn else "") for t, warn in texts]
            low = any(warn for _, warn in texts)
        if plan is not None:
            # 軌道の使用時間: each satellite with limits used on the day (its longest pass, its day).
            texts = plan.uses.texts()
            lines += [self.tr("使用時間 ") + t + (self.tr("　※残りわずか") if warn else "") for t, warn in texts]
            low = low or any(warn for _, warn in texts)
        self.quota_text.setText("\n".join(lines))
        self.quota_text.setStyleSheet("color: #b45309;" if low else "")
        self.quota_text.setVisible(bool(self.quota_text.text()))
        if self.chosen is not None and not any(c is self.chosen for c in self.shown):
            self.chosen = None
        self.draw()
        self._update_buttons()

    # --- The map --------------------------------------------------------------------------------

    def _row(self, row: int) -> None:
        if 0 <= row < len(self.shown):
            self.chosen = self.shown[row]
            self.draw()
            self.zoom()

    def map_polygons(self) -> list:
        """
        What 「地図に表示」 draws: each request shown with its own area (taken
        green, put together blue, held back grey), and the shots of the rows
        taken (the outline of the scenes, dashed; the area a merged row is
        written with). A row a system writes as the rectangle round its area
        (`areaShape` rotatedBox) has that rectangle dashed in amber. The chosen
        one, its shot and the requests in it stand out.
        """
        chosen = self.chosen
        mine = set(map(id, chosen.shots)) if chosen is not None else set()
        # The shots of an area shared out (分担) stand out together: the request's area is drawn once.
        head = (chosen.main or chosen) if chosen is not None else None
        out, shots, boxes = [], {}, []
        for c in sorted(self.shown, key=lambda c: c is chosen or c is head):
            taken = c.result == TAKEN
            if taken and not c.member:
                for s in c.shots:
                    # A shot of an area shared out: the part its row is written with.
                    shots[id(s)] = (s, row_rings(c, c.system.merge_margin)[0] if c.part and c.system else s.area_ring())
                if c.system is not None and c.system.area_shape == "rotatedBox" and not c.part and not c.together and c.request.rings:
                    boxes.append((row_rings(c, c.system.merge_margin, c.system.area_shape), "#d97706", 3 if c is head else 1, "", True))
            if not c.request.rings or c.main is not None:
                continue
            color = ("#2563eb" if c.merged else "#16a34a") if taken else "#9ca3af"
            fellow = taken and bool(mine) and any(id(s) in mine for s in c.shots)
            out.append(([[list(p[:2]) for p in r] for r in c.request.rings], color, 3 if c is head else 2 if fellow else 1,
                        (c.request.name or c.request.id) if c is head else ""))
        out += [([ring], "#7c3aed", 3 if id(s) in mine else 1, "", True) for s, ring in shots.values()]
        return out + boxes

    def draw(self) -> None:
        if not self.on_map.isChecked() or not self.isVisible() or self.plan is None:
            self.overlay.unpaint()
            self.canvas.refresh()
            return
        self.overlay.paint(self.map_polygons())
        self.canvas.refresh()

    def zoom(self) -> None:
        """To the chosen request and its shot."""
        c = self.chosen
        if c is None:
            return
        points = [p[:2] for r in c.request.rings for p in r] + [p for s in c.shots for p in s.area_ring()]
        if not points:
            return
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        box = QgsRectangle(min(xs), min(ys), max(xs), max(ys))
        box = box.buffered(max(box.width(), box.height()) * 0.2 + 0.01)
        self.canvas.setExtent(from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box))
        self.canvas.refresh()

    def _item_changed(self, item: QStandardItem) -> None:
        if self.plan is None or self._rendering:
            return
        if not 0 <= item.row() < len(self.shown):
            return
        c = self.shown[item.row()]
        checked = item.checkState() == Qt.CheckState.Checked
        if self.task is not None:
            # Not while writing: the files and history are made from the plan as it was (#84).
            was = c.result == TAKEN if item.column() == 0 else c.boost_taken
            if checked != was:
                self._rendering = True
                item.setCheckState(Qt.CheckState.Checked if was else Qt.CheckState.Unchecked)
                self._rendering = False
            return
        if item.column() == 0:
            c.manual = TAKEN if checked else HELD
        elif item.column() == BOOST_COLUMN:
            c.boost_taken = checked
        else:
            return
        # Not while the model is telling of the change: refitting rebuilds the model.
        QTimer.singleShot(0, lambda: self.refit(c))

    def refit(self, keep=None) -> None:
        self.plan.refit()
        self.render()
        row = next((i for i, c in enumerate(self.shown) if c is keep), -1) if keep is not None else -1
        if row >= 0:
            self.table.selectRow(row)

    def move(self, step: int) -> None:
        """Moves the chosen row up or down past the next row shown (kept as a place in the order)."""
        if self.plan is None or self.task is not None:
            return
        row = self.table.currentIndex().row()
        rows = self.shown
        if not (0 <= row < len(rows)) or not (0 <= row + step < len(rows)):
            return
        for i, c in enumerate(self.plan.candidates):
            c.rank = float(i)
        a, b = rows[row], rows[row + step]
        a.rank, b.rank = b.rank, a.rank
        self.refit(a)

    def reset(self) -> None:
        if self.plan is None or self.task is not None:
            return
        for c in self.plan.candidates:
            c.manual, c.rank, c.boost_taken = None, None, True
        self.refit()

    def _update_buttons(self) -> None:
        busy = self.task is not None
        has = self.plan is not None and bool(self.plan.candidates)
        self.run_button.setEnabled(not busy and self.store is not None)
        self.sim_button.setEnabled(self.store is not None)
        for b in (self.up_button, self.down_button, self.reset_button):
            b.setEnabled(has and not busy)
        target = self.target()
        self.write_button.setEnabled(has and not busy and self.system_filter.currentData() != NO_SYSTEM and any(
            c.result == TAKEN and not c.pinned and (target is None or c.system.system == target) for c in self.plan.candidates))
        label = next((s.label for s in self.context.config.request_exports if s.system == target), target) if target else ""
        self.write_button.setText(self.tr("{system} を書き出し").format(system=label) if target else self.tr("書き出し"))

    # --- Writing --------------------------------------------------------------------------------

    def write(self, ask: bool = True) -> None:
        """
        Builds the files (schemas read and the day's earlier history counted
        in a task), asks, writes them, then saves the history and raises.
        """
        if self.plan is None or self.task is not None or self.system_filter.currentData() == NO_SYSTEM:
            return
        live = self.plan
        if live.target != self.target():
            # The system chosen while a task ran: fitted for it before writing.
            live.target = self.target()
            live.refit()
            self.render()
        # One copy for the files, the history and the raises, whatever is changed in the table meanwhile (#84).
        plan = dataclasses.replace(live, candidates=[dataclasses.replace(c, warnings=list(c.warnings)) for c in live.candidates])
        target = plan.target
        run = _now_ms()
        repo = self.repository()
        self.status.setText(self.tr("ファイルを作成中…"))

        def work(task):
            day = plan.day.isoformat()
            if target and repo.store and repo.store.history:
                # Another system written since 計算 would leave a request in two files, or in none.
                now = frozenset((r.request_id, r.system) for r in repo.load_history(handed_where(repo.store, day))
                                if r.system and r.system != target)
                if now != plan.others_handed():
                    return None, sorted({s for _, s in now ^ plan.others_handed()})
            systems = [s for s in plan.systems if s.system == target] if target else plan.systems
            files = render(plan.candidates, systems, plan.day, run, load_schema)
            earlier = repo.count_history(replaced_where(repo.store, day, target)) if repo.store and repo.store.history else 0
            return files, earlier

        def done(result):
            self.task = None
            self._update_buttons()
            files, earlier = result
            if files is None:
                labels = {s.system: s.label for s in plan.systems}
                self.status.setText(self.tr("計算のあとに {list} の出力履歴が変わったため書き出しを取りやめ。"
                                            "「計算」し直してから書き出し").format(list="・".join(labels.get(s, s) for s in earlier)))
                return
            self._write_files(plan, run, files, ask, earlier)

        def failed(message):
            self.task = None
            self.status.setText(self.tr("ファイルの作成に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("出力ファイルの作成"), work, done, failed)
        self._update_buttons()

    def _confirm(self, lines: list, earlier: int, label: str = "") -> Optional[bool]:
        """
        Asks before writing: None to stop, else whether the day's earlier
        history rows (not yet followed up; of the system written, `label`)
        are replaced rather than kept.
        """
        if not earlier:
            ok = QMessageBox.question(self, self.tr("書き出し"), plain_tip("\n".join(lines))) == QMessageBox.StandardButton.Yes
            return False if ok else None
        whose = self.tr("{system} の").format(system=label) if label else ""
        lines = [*lines, "", self.tr("この日の{whose}出力履歴（未判定）がすでに {n} 件ある。置き換えると前回の行を消して今回の行を保存。"
                                      "採否は、今回も同じシステムに出す依頼には引き継ぎ、今回出さない依頼の行は採否ごと残る").format(whose=whose, n=earlier)]
        box = QMessageBox(QMessageBox.Icon.Question, self.tr("書き出し"), plain_tip("\n".join(lines)), parent=self)
        replace = box.addButton(self.tr("置き換えて書き出し"), QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton(self.tr("追加して書き出し"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(self.tr("取りやめ"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(replace)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is replace else False if clicked is keep else None

    @staticmethod
    def _leftovers(plan: DayPlan, run: float, targets: list, fallback: str = "") -> list:
        """
        (system, folder, name) of the files in the folders named as a system's
        files of the day with another number (`maxPerFile`): left from an
        earlier writing split otherwise, they are deleted once its files are
        written. Never a file that could be another system's of the same
        folder (`fallback`: the folder of the systems that name none).
        """
        def same(a: str, b: str) -> bool:
            return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

        out = []
        for system in {w.system.system: w.system for w, _ in targets}.values():
            if system.max_per_file <= 0:
                continue
            for folder in dict.fromkeys(f for w, f in targets if w.system is system):
                try:
                    listed = os.listdir(folder)
                except OSError:
                    continue
                names = [w.file_name for w, f in targets if w.system is system and f == folder]
                others = [s for s in plan.systems if s.system != system.system and (s.folder or fallback)
                          and same(s.folder or fallback, folder)]
                out += [(system.system, folder, n) for n in leftover_files(system, plan.day, run, names, listed, others)]
        return out

    @staticmethod
    def _label(w) -> str:
        """「A システム」, one of a system's files 「A システム（2/3）」."""
        return f"{w.system.label}（{w.part}/{w.parts}）" if w.parts > 1 else w.system.label

    def _write_files(self, plan: DayPlan, run: float, files: list, ask: bool, earlier: int = 0) -> None:
        # A system split into files (maxPerFile) writes all of them or, when one is not fit (schema …), none.
        broken = [w for w in files if w.errors]
        unfit = {w.system.system for w in broken}
        good = [w for w in files if w.system.system not in unfit]
        fallback = self.folder.text().strip()
        if any(not w.system.folder for w in good) and not fallback:
            self.status.setText(self.tr("出力先のフォルダを指定"))
            return
        targets = [(w, w.system.folder or fallback) for w in good]
        existing = [os.path.join(f, w.file_name) for w, f in targets if os.path.exists(os.path.join(f, w.file_name))]
        leftovers = self._leftovers(plan, run, targets, fallback)
        replace = bool(earlier)
        target = plan.target
        label = next((s.label for s in plan.systems if s.system == target), target or "")
        if ask:
            lines = [f"{self._label(w)}: {os.path.join(f, w.file_name)}（{w.count} 件）" for w, f in targets]
            if target:
                lines.append(self.tr("{system} の分だけ書き出し（ほかのシステムのファイルと出力履歴はそのまま）").format(system=label))
            if broken:
                lines += [self.tr("書き出さないもの（スキーマに不一致など）:")] + [f"{self._label(w)}: {'、'.join(w.errors[:3])}" for w in broken]
                if any(w.parts > 1 for w in broken):
                    lines.append(self.tr("（分けたファイルのどれかが合わないシステムは、全部のファイルを書き出さない）"))
            if existing:
                lines.append(self.tr("上書きするファイル {n} 件").format(n=len(existing)))
            if leftovers:
                lines.append(self.tr("前の書き出しで残っている番号違いのファイル {n} 件を削除: {list}").format(
                    n=len(leftovers), list="、".join(n for _, _, n in leftovers[:5]) + (" …" if len(leftovers) > 5 else "")))
            choice = self._confirm(lines, earlier, label)
            if choice is None:
                self.status.setText(self.tr("書き出しを取りやめ"))
                return
            replace = choice
        paths, problems = [], []
        done_files = []
        failures: dict = {}
        for w in broken:
            failures.setdefault(w.system.system, "、".join(w.errors[:2]))
        for w, folder in targets:
            try:
                path = write(w, folder)
                paths.append(path)
                done_files.append(w)
                audit.record("export", "plan-file", path, count=w.count, detail={"system": w.system.system, "day": plan.day.isoformat()}, file=path)
            except OSError as error:
                reason, said = errors.describe(error)
                problems.append(f"{self._label(w)}: {reason}")
                audit.error("file-save", getattr(error, "filename", None) or folder, reason,
                            detail={"what": "plan-file", "system": w.system.system, "said": said})
                failures.setdefault(w.system.system, reason)
                continue
            _, notes = encoded(w)
            problems += [f"{self._label(w)}: {n}" for n in notes + w.warnings[:3]]
        # A request is 採用 with the file it is in; those of a file not written are kept as 見送り.
        written = files_of(done_files)
        cleared = 0
        for system, folder, name in leftovers:
            if system in failures:
                continue
            try:
                os.remove(os.path.join(folder, name))
                cleared += 1
                audit.record("export", "plan-file-removed", os.path.join(folder, name), detail={"system": system})
            except OSError as error:
                problems.append(self.tr("{name} の削除に失敗: {m}").format(name=name, m=errors.describe(error)[0]))
        self.paths = paths
        if fallback:
            settings.set_request_export_folder(fallback)
        repo = self.repository()
        # A request taken for a system whose file was not written is kept as 見送り, its raise left for later.
        run_id = new_run_id()
        rows = history_of(plan, run, written, run_id, failures=failures, system=target)
        # The shots of the rows written (撮像の範囲), each request's history row naming its own (SHOT_ID).
        shots = shots_of(plan, run, written, run_id, target) if repo.store and repo.store.shots else []
        skip = not_written(plan, written, target)
        boosts = boost_changes(plan, skip, target)
        done_rows = missed_done(plan, skip, target)
        day = plan.day.isoformat()

        linked = any(w.links for w in done_files)

        def work(task):
            notes = []
            if linked and repo.store is not None and repo.store.history:
                # A file naming its rows by 外部連携ID, and a table that cannot keep them: the answers cannot be matched by them.
                lacking = repo.history_has(("linkId",))
                if lacking:
                    notes.append(f"出力履歴に属性 {lacking[0]} がないため外部連携IDの保存に失敗（管理者に連絡）［E704］")
            removed = 0
            rewrite = Rewrite(rows=list(rows))
            if replace and earlier and repo.store.history:
                # The systems' answers already read in are carried to the new rows, or kept (tsudo, 2026-11-09).
                # With their areas: an answer for an area changed since (地域の形に合わせる) is not carried.
                before = repo.load_history(replaced_where(repo.store, day, target), shapes=True)
                rewrite = keep_answers(before, rows)
                gone = repo.delete_history_rows(rewrite.gone)
                removed, notes = int(gone), notes + gone.problems
                if repo.store.shots:
                    # The earlier shots go with their rows; those of rows kept as they were stay, and those of rows
                    # already followed up (not replaced: what they were taken in, and its 「数えない」).
                    left = {r.shot_id for r in before if r.object_id not in set(rewrite.gone)}
                    try:
                        left |= {r.shot_id for r in repo.load_history(followed_where(repo.store, day, target))}
                        notes += repo.delete_shots(shots_replaced_where(repo.store, day, target, sorted(left))).problems
                    except EsriError as error:
                        notes.append(f"前回の撮像の範囲を消せず（{error}）")
            saved = repo.add_history(rewrite.rows)
            try:
                kept_shots = repo.add_shots(shots)
            except EsriError as error:
                # The history (what each request was) matters more: kept even when the shots cannot be.
                kept_shots = Saved(0, [f"撮像の範囲の保存に失敗: {error}"])
            notes += kept_shots.problems
            raised = repo.update_requests(boosts, run)
            marked = repo.update_history(done_rows)
            return saved, raised, marked, removed, rewrite, notes + saved.problems + raised.problems + marked.problems, int(kept_shots)

        def done(result):
            self.task = None
            saved, raised, _, removed, rewrite, failed_saves, shot_count = result
            text = self.tr("書き出し: {s}").format(s=written_summary(done_files))
            if cleared:
                text += self.tr("・残っていた番号違いのファイル {n} 件を削除").format(n=cleared)
            if repo.store.history:
                text += (self.tr("・出力履歴 {n} 件").format(n=saved) if saved == len(rows)
                         else self.tr("・出力履歴 {n}/{m} 件").format(n=saved, m=len(rows)))
                if removed:
                    text += self.tr("（前回の {n} 件を置き換え）").format(n=removed)
                if rewrite.carried:
                    text += self.tr("・採否を引き継いだ行 {n} 件").format(n=rewrite.carried)
                if rewrite.kept:
                    text += self.tr("・採否ごと残した前回の行 {n} 件").format(n=rewrite.kept)
            if shots:
                text += (self.tr("・撮像の範囲 {n} 件").format(n=shot_count) if shot_count == len(shots)
                         else self.tr("・撮像の範囲 {n}/{m} 件").format(n=shot_count, m=len(shots)))
            if raised:
                text += self.tr("・繰り上げ {n} 件").format(n=raised)
            if broken:
                text += self.tr("・書き出さなかったシステム {n}（{list}）").format(n=len(unfit), list="、".join(f"{self._label(w)}: {w.errors[0]}" for w in broken))
            if problems:
                text += "・" + "、".join(problems)
            if failed_saves:
                text += self.tr("。保存に失敗したもの（もう一度「書き出し」で置き換えるとやり直せる）: {m}").format(m="、".join(failed_saves[:5]))
            self.status.setText(text)
            self._update_buttons()
            self.requests_changed()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("ファイルは書き出し済み。出力履歴と繰り上げの保存に失敗（もう一度「書き出し」で置き換えるとやり直せる）: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("出力履歴の保存"), work, done, failed)
        self._update_buttons()
