"""
The 「撮像計画ファイル出力」 dock: the day's work of writing the imaging plan
files from the request ledger (`requestStores`, `requestExports`). It is a
dock of its own, apart from 「撮像依頼」, since those writing the files are
not those registering requests (tsudo, 2026-11-07).

「計算」 follows up what was handed out before (the image catalog asked
whether those requests were taken), reads the ledger again, asks the cloud
forecast for the optical requests, works out the satellites' passes over
them from their orbits (core/request/passes.py) and fits the day's
requests into the passes and the systems' limits. Requests put into one
shot (まとめ) say so in 結果・理由 (tsudo, 2026-11-11), and the time of the
pass is in 撮像時刻. The table shows every candidate: ticking 採用 or not,
moving rows up and down and ticking a raise are kept and the fitting is
done again. 「書き出し」 writes one file per system (CSV, or JSON checked
against its schema), then the history and the raises taken.
「上限シミュレーション…」 tries other daily limits over the coming days
without writing anything (limit_sim_dialog.py).

The table can be narrowed to a system, a satellite series, a result and
words (core/request/view.py): the files of each system are written by
different people, so the system and series chosen are kept (per user).
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

from qgis.PyQt.QtCore import QCoreApplication, QDate, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableView,
)

from ..core.catalog import CatalogConfig
from ..core.plan.weather import fetch_clouds
from ..core.request.daily import HELD, TAKEN, merge_text, pass_text, room_text, satellite_name, time_text
from ..core.request.export import convert_priority, encoded, render, write
from ..core.request.follow import follow_and_save, found_text
from ..core.request.model import jst_day, priority_text
from ..core.request.passes import day_passes
from ..core.request.view import BOOST, NO_SYSTEM, CandidateFilter, series_choices, shown
from ..core.request.store import (
    RequestStoreConfig, add_history, count_history, delete_history_rows, handed_where, history_where, load_history, load_requests,
    replaced_where, update_history, update_requests,
)
from ..core.request.workflow import (
    DayPlan, Rewrite, boost_changes, clouds_of, forecast_places, history_of, keep_answers, missed_done, not_written, plan_day,
    written_summary,
)
from ..qgis_io import settings
from ..qgis_io.network import get_bytes
from ..qgis_io.request_io import load_schema
from ..qgis_io.tasks import run_task
from .docks import PluginDock, plain_label, plain_tip
from .list_views import column_menus

COLUMNS = ("採用", "システム", "衛星", "撮像時刻", "依頼", "区分", "優先度", "雲量", "枚数", "結果・理由", "繰り上げ")
#: The column of 結果・理由.
RESULT_COLUMN = COLUMNS.index("結果・理由")
#: How 「該当システムなし」 is kept in the settings (an empty value is 「すべてのシステム」).
_NO_SYSTEM_KEPT = "*none*"


def _now_ms() -> float:
    return time.time() * 1000


class PlanFileDock(PluginDock):
    """The day's candidates, changes by hand, and writing the files."""

    def __init__(self, iface, context, open_settings, satellites, requests_changed=None, parent=None):
        super().__init__("SatObsPlanFiles", QCoreApplication.translate("PlanFileDock", "撮像計画ファイル出力"), context, open_settings, parent)
        self.iface = iface
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
        #: The 「計画と実績の照合」 dialog, once opened.
        self.results = None
        self._build()
        context.configChanged.connect(self._config_changed)
        self._config_changed()

    def _build(self) -> None:
        layout = self.body
        self.store_choice = QComboBox()
        self.store_choice.setToolTip(self.tr("依頼の保存先"))
        self.store_choice.currentIndexChanged.connect(self._store_chosen)
        layout.addWidget(self.store_choice)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("対象日（日本時間）")))
        self.day = QDateEdit()
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        today = jst_day(_now_ms())
        self.day.setDate(QDate(today.year, today.month, today.day))
        top.addWidget(self.day)
        self.run_button = QPushButton(self.tr("計算"))
        self.run_button.setToolTip(self.tr("撮像済みの確認・依頼の再読み込み・雲量予報のあと、その日の依頼を枠に詰める"))
        self.run_button.clicked.connect(lambda: self.run())
        top.addWidget(self.run_button)
        self.sim_button = QPushButton(self.tr("上限シミュレーション…"))
        self.sim_button.setToolTip(self.tr("日ごとの上限や使うシステムを変えた場合の枠超過・期限切れを今後の日数で試算（台帳には書き込まない）"))
        self.sim_button.clicked.connect(self.open_simulation)
        top.addWidget(self.sim_button)
        self.results_button = QPushButton(self.tr("照合…"))
        self.results_button.setToolTip(self.tr("出力した依頼が撮像されたかを画像カタログと照合（計画と実績の照合）"))
        self.results_button.clicked.connect(lambda: self.show_results())
        top.addWidget(self.results_button)
        top.addStretch(1)
        layout.addLayout(top)
        # Request names and the services' messages: shown as written, never as HTML.
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        filters = QHBoxLayout()
        self.system_filter = QComboBox()
        self.system_filter.setToolTip(self.tr("このシステムの依頼だけを表示（見送りは、そのシステムで撮れるものも）。次回も同じ選択"))
        self.series_filter = QComboBox()
        self.series_filter.setToolTip(self.tr("この衛星シリーズの依頼だけを表示（採用は割り当てた衛星、見送りは撮れる衛星のシリーズ）。次回も同じ選択"))
        self.result_filter = QComboBox()
        for text, value in ((self.tr("すべての結果"), None), (TAKEN, TAKEN), (HELD, HELD), (self.tr("繰り上げ候補"), BOOST)):
            self.result_filter.addItem(text, value)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(self.tr("名称・ID・番号・衛星"))
        self.search.setToolTip(self.tr("名称・依頼ID・管理番号・識別番号・割り当てた衛星で絞り込み（空白で区切るとすべてを含むもの）"))
        self.search.setMinimumWidth(90)
        for w in (self.system_filter, self.series_filter, self.result_filter):
            # Long system or series names do not widen the dock.
            w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(8)
            filters.addWidget(w)
            w.currentIndexChanged.connect(self._filter_changed)
        self.search.textChanged.connect(lambda _text: self.render())
        filters.addWidget(self.search, 1)
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
        layout.addWidget(self.table, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.table_columns = column_menus("plan-files", self.table, self)
        moves = QHBoxLayout()
        self.up_button = QPushButton(self.tr("上へ"))
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button = QPushButton(self.tr("下へ"))
        self.down_button.clicked.connect(lambda: self.move(1))
        self.reset_button = QPushButton(self.tr("手動の変更を戻す"))
        self.reset_button.clicked.connect(self.reset)
        for b in (self.up_button, self.down_button, self.reset_button):
            moves.addWidget(b)
        moves.addStretch(1)
        self.room = QLabel()
        self.room.setWordWrap(True)
        moves.addWidget(self.room)
        layout.addLayout(moves)
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
        self._fill_systems()
        self._fill_series()
        if self.results is not None:
            self.results.close()
            self.results = None
        self.render()
        self.status.setText(self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし") if self.store is None else "")

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.request_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self.plan = None
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
                               result=self.result_filter.currentData(), text=self.search.text())

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

    def close_all(self) -> None:
        """Stops nothing running but forgets the day (when the plugin is unloaded)."""
        self.plan = None
        if self.simulation is not None:
            self.simulation.close()
            self.simulation = None
        if self.results is not None:
            self.results.close()
            self.results = None

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
        self.status.setText(self.tr("衛星カタログを読み込み中…"))
        self.run_button.setEnabled(False)
        self.satellites(self._run_with, self._satellites_failed)

    def _satellites_failed(self, message: str) -> None:
        """The satellite catalog could not be read: says why and gives 「計算」 back (#70)."""
        self.status.setText(self.tr("衛星カタログの読み込みに失敗: {m}").format(m=message))
        self._update_buttons()

    def _run_with(self, sats) -> None:
        store = self.store
        config = self.context.config
        systems = config.request_exports
        day = self.chosen_day()
        catalog = self.catalog()
        client = self.context.esri()
        token = self.token()
        catalog_token = self.context.token_for(catalog.url, catalog.token) if catalog else None
        forecast = config.cloud_forecast
        authcfg = settings.auth_for(forecast.url) if forecast else None
        now = _now_ms()
        self.status.setText(self.tr("撮像済みの確認・依頼の読み込み・雲量予報を問い合わせ中…"))

        def work(task):
            notes = []
            requests = load_requests(client, store, token, task.isCanceled)
            # Only the rows still to be followed up or raised for, not the whole (ever growing) table.
            history = (load_history(client, store, token, history_where(store, open_rows=True), cancelled=task.isCanceled)
                       if store.history else [])
            task.progress(30)
            if history and catalog is not None:
                by_id = {r.id: r for r in requests}
                names = self.context.satellite_names(client, task.isCanceled)
                problems = []
                found, changed = follow_and_save(client, store, token, catalog, catalog_token, history, by_id, now, names, task.isCanceled,
                                                 problems)
                if found:
                    notes.append(found_text(found))
                if problems:
                    notes.append("照合結果の保存に一部失敗（" + "、".join(problems[:3]) + "）")
                if changed:
                    requests = load_requests(client, store, token, task.isCanceled)
            elif store.history and catalog is None:
                notes.append("画像カタログがないため撮像済みの確認なし")
            task.progress(60)
            forecasts = {}
            places = forecast_places(requests, day, sats)
            if places and forecast is not None:
                try:
                    forecasts = fetch_clouds(forecast, places, lambda url: get_bytes(url, authcfg), task.isCanceled)
                except Exception as error:  # noqa: BLE001 (the day goes on without the weather)
                    notes.append(f"雲量予報の取得失敗（{error}）")
            elif places:
                notes.append("雲量予報なし（cloudForecast: false）")
            task.progress(70)
            # The satellites' passes over the day's requests, from their orbits (once; the hand's changes fit again on them).
            passes = day_passes(requests, day, sats, now, task.isCanceled)
            task.check()
            if passes.problems:
                notes.append("TLE を読めない衛星は軌道なしで件数だけで詰める（" + "、".join(passes.problems[:3]) + "）")
            return requests, history, forecasts, passes, notes

        def done(result):
            self.task = None
            requests, history, forecasts, passes, notes = result
            self.requests_changed(requests)
            clouds = clouds_of(requests, day, forecasts)
            self.plan = plan_day(requests, day, systems, sats, clouds, history, self.target(), passes)
            self._fill_series()
            self.render()
            taken = sum(1 for c in self.plan.candidates if c.result == TAKEN)
            text = self.tr("{day}: 候補 {n} 件・採用 {t} 件・見送り {h} 件").format(day=day.isoformat(), n=len(self.plan.candidates), t=taken,
                                                                         h=len(self.plan.candidates) - taken)
            pinned = sum(1 for c in self.plan.candidates if c.pinned)
            if pinned:
                text += self.tr("（うち ほかのシステムで書き出し済み {n} 件）").format(n=pinned)
            text += self.merged_text()
            if not systems:
                text += self.tr("（設定ファイルに requestExports なし）")
            self.status.setText("。".join([text, *notes]))
            self._update_buttons()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("計算失敗: {m}").format(m=message))
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
            cloud = "" if not c.weather else ("—" if c.cloud is None else f"{round(c.cloud)}%")
            reason = c.reason if c.result == HELD else ""
            merged = merge_text(c) if c.result == TAKEN else ""
            note = "・".join(x for x in (reason, merged, *c.warnings) if x)
            boost = QStandardItem(f"+{c.boost_offer - r.boost}（{c.boost_reason}）" if c.boost_offer is not None else "")
            if c.boost_offer is not None:
                boost.setCheckable(True)
                boost.setCheckState(Qt.CheckState.Checked if c.boost_taken else Qt.CheckState.Unchecked)
                boost.setToolTip(self.tr("チェックで優先度を 100 繰り上げ（書き出しのときに台帳へ保存）"))
            taken = c.result == TAKEN
            scenes = (self.tr("同乗") if c.member else str(c.scenes)) if taken else ""
            items = [take, *(QStandardItem(t) for t in (
                c.system.label if c.system else "", satellite_name(c), time_text(c.planned) if taken else "", r.name, r.category,
                prio, cloud, scenes, (c.result + ("：" + note if note else "")))), boost]
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
            self.model.appendRow(items)
        self._rendering = False
        rooms = room_text(plan.candidates, plan.systems) if plan else {}
        listed = [s for s in (plan.systems if plan else []) if not chosen.system or s.system == chosen.system]
        total = len(plan.candidates) if plan else 0
        parts = [self.tr("表示 {n}/{m} 件").format(n=len(self.shown), m=total)] if len(self.shown) != total else []
        self.room.setText("　".join(parts + [f"{s.label} {rooms[s.system]}" for s in listed]))
        self._update_buttons()

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
        elif item.column() == len(COLUMNS) - 1:
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
        store, client, token = self.store, self.context.esri(), self.token()
        self.status.setText(self.tr("ファイルを作成中…"))

        def work(task):
            day = plan.day.isoformat()
            if target and store and store.history:
                # Another system written since 計算 would leave a request in two files, or in none.
                now = frozenset((r.request_id, r.system) for r in load_history(client, store, token, handed_where(store, day))
                                if r.system and r.system != target)
                if now != plan.others_handed():
                    return None, sorted({s for _, s in now ^ plan.others_handed()})
            systems = [s for s in plan.systems if s.system == target] if target else plan.systems
            files = render(plan.candidates, systems, plan.day, run, load_schema)
            earlier = count_history(client, store, token, replaced_where(store, day, target)) if store and store.history else 0
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
        lines = [*lines, "", self.tr("この日の{whose}出力履歴（未判定）が既に {n} 件あります。"
                                      "置き換えると前回の行を消してから今回の行を保存します。"
                                      "システムの採否（結果ファイル・手入力）は、今回も同じシステムに出す依頼の行へ引き継ぎ、"
                                      "今回は出さない依頼の行は採否ごと残します。").format(whose=whose, n=earlier)]
        box = QMessageBox(QMessageBox.Icon.Question, self.tr("書き出し"), plain_tip("\n".join(lines)), parent=self)
        replace = box.addButton(self.tr("置き換えて書き出し"), QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton(self.tr("追加して書き出し"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(self.tr("取りやめ"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(replace)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is replace else False if clicked is keep else None

    def _write_files(self, plan: DayPlan, run: float, files: list, ask: bool, earlier: int = 0) -> None:
        broken = [w for w in files if w.errors]
        good = [w for w in files if not w.errors]
        fallback = self.folder.text().strip()
        if any(not w.system.folder for w in good) and not fallback:
            self.status.setText(self.tr("出力先のフォルダを指定"))
            return
        targets = [(w, w.system.folder or fallback) for w in good]
        existing = [os.path.join(f, w.file_name) for w, f in targets if os.path.exists(os.path.join(f, w.file_name))]
        replace = bool(earlier)
        target = plan.target
        label = next((s.label for s in plan.systems if s.system == target), target or "")
        if ask:
            lines = [f"{w.system.label}: {os.path.join(f, w.file_name)}（{w.count} 件）" for w, f in targets]
            if target:
                lines.append(self.tr("{system} の分だけ書き出します（ほかのシステムのファイル・出力履歴はそのまま）").format(system=label))
            if broken:
                lines += [self.tr("書き出さないもの（スキーマに不一致など）:")] + [f"{w.system.label}: {'、'.join(w.errors[:3])}" for w in broken]
            if existing:
                lines.append(self.tr("上書きするファイル {n} 件").format(n=len(existing)))
            choice = self._confirm(lines, earlier, label)
            if choice is None:
                self.status.setText(self.tr("書き出しを取りやめ"))
                return
            replace = choice
        paths, problems = [], []
        written: dict = {}
        failures = {w.system.system: "、".join(w.errors[:2]) for w in broken}
        for w, folder in targets:
            try:
                paths.append(write(w, folder))
                written[w.system.system] = w.file_name
            except OSError as error:
                reason = error.strerror or str(error)
                problems.append(f"{w.system.label}: {reason}")
                failures[w.system.system] = reason
                continue
            _, notes = encoded(w)
            problems += [f"{w.system.label}: {n}" for n in notes + w.warnings[:3]]
        self.paths = paths
        if fallback:
            settings.set_request_export_folder(fallback)
        store, client, token = self.store, self.context.esri(), self.token()
        # A request taken for a system whose file was not written is kept as 見送り, its raise left for later.
        rows = history_of(plan, run, written, failures=failures, system=target)
        skip = not_written(plan, written, target)
        boosts = boost_changes(plan, skip, target)
        done_rows = missed_done(plan, skip, target)
        day = plan.day.isoformat()

        def work(task):
            notes = []
            removed = 0
            rewrite = Rewrite(rows=list(rows))
            if replace and earlier and store.history:
                # The systems' answers already read in are carried to the new rows, or kept (tsudo, 2026-11-09).
                rewrite = keep_answers(load_history(client, store, token, replaced_where(store, day, target)), rows)
                gone = delete_history_rows(client, store, token, rewrite.gone)
                removed, notes = int(gone), notes + gone.problems
            saved = add_history(client, store, token, rewrite.rows)
            raised = update_requests(client, store, token, boosts, run)
            marked = update_history(client, store, token, done_rows)
            return saved, raised, marked, removed, rewrite, notes + saved.problems + raised.problems + marked.problems

        def done(result):
            self.task = None
            saved, raised, _, removed, rewrite, failed_saves = result
            text = self.tr("書き出し: {s}").format(s=written_summary([w for w, _ in targets if w.system.system in written]))
            if store.history:
                text += (self.tr("・出力履歴 {n} 件").format(n=saved) if saved == len(rows)
                         else self.tr("・出力履歴 {n}/{m} 件").format(n=saved, m=len(rows)))
                if removed:
                    text += self.tr("（前回の {n} 件を置き換え）").format(n=removed)
                if rewrite.carried:
                    text += self.tr("・採否を引き継いだ行 {n} 件").format(n=rewrite.carried)
                if rewrite.kept:
                    text += self.tr("・採否ごと残した前回の行 {n} 件").format(n=rewrite.kept)
            if raised:
                text += self.tr("・繰り上げ {n} 件").format(n=raised)
            if broken:
                text += self.tr("・書き出さなかったシステム {n}（{list}）").format(n=len(broken), list="、".join(f"{w.system.label}: {w.errors[0]}" for w in broken))
            if problems:
                text += "・" + "、".join(problems)
            if failed_saves:
                text += self.tr("。保存できなかったもの（もう一度「書き出し」で置き換えてやり直せます）: {m}").format(m="、".join(failed_saves[:5]))
            self.status.setText(text)
            self._update_buttons()
            self.requests_changed()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("ファイルは書き出し済み。出力履歴・繰り上げの保存に失敗（もう一度「書き出し」で置き換えてやり直せます）: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("出力履歴の保存"), work, done, failed)
        self._update_buttons()
