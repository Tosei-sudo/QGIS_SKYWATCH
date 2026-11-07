"""
The 「撮像計画ファイル出力」 dock: the day's work of writing the imaging plan
files from the request ledger (`requestStores`, `requestExports`). It is a
dock of its own, apart from 「撮像依頼」, since those writing the files are
not those registering requests (tsudo, 2026-11-07).

「計算」 follows up what was handed out before (the image catalog asked
whether those requests were taken), reads the ledger again, asks the cloud
forecast for the optical requests and fits the day's requests into the
systems' limits. The table shows every candidate: ticking 採用 or not,
moving rows up and down and ticking a raise are kept and the fitting is
done again. 「書き出し」 writes one file per system (CSV, or JSON checked
against its schema), then the history and the raises taken.
「上限シミュレーション…」 tries other daily limits over the coming days
without writing anything (limit_sim_dialog.py).
"""
from __future__ import annotations

import math
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
from ..core.request.daily import HELD, TAKEN, room_text, satellite_name
from ..core.request.export import convert_priority, encoded, render, write
from ..core.request.follow import follow_and_save, found_text
from ..core.request.model import JST, jst_day, priority_text
from ..core.request.store import RequestStoreConfig, add_history, load_history, load_requests, update_history, update_requests
from ..core.request.workflow import (
    DayPlan, boost_changes, clouds_of, forecast_places, history_of, missed_done, plan_day, written_summary,
)
from ..qgis_io import settings
from ..qgis_io.network import get_bytes
from ..qgis_io.request_io import load_schema
from ..qgis_io.tasks import run_task
from .docks import PluginDock

COLUMNS = ("採用", "システム", "衛星", "依頼", "区分", "優先度", "雲量", "枚数", "結果・理由", "繰り上げ")


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
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
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
        self.write_button.setToolTip(self.tr("採用の依頼をシステムごとのファイルに書き出し、出力履歴と繰り上げを保存"))
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
        self.satellites(self._run_with)

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
            history = load_history(client, store, token, cancelled=task.isCanceled) if store.history else []
            task.progress(30)
            if history and catalog is not None:
                by_id = {r.id: r for r in requests}
                names = self.context.satellite_names(client, task.isCanceled)
                found, changed = follow_and_save(client, store, token, catalog, catalog_token, history, by_id, now, names, task.isCanceled)
                if found:
                    notes.append(found_text(found))
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
            return requests, history, forecasts, notes

        def done(result):
            self.task = None
            requests, history, forecasts, notes = result
            self.requests_changed(requests)
            clouds = clouds_of(requests, day, forecasts)
            self.plan = plan_day(requests, day, systems, sats, clouds, history)
            self.render()
            taken = sum(1 for c in self.plan.candidates if c.result == TAKEN)
            text = self.tr("{day}: 候補 {n} 件・採用 {t} 件・見送り {h} 件").format(day=day.isoformat(), n=len(self.plan.candidates), t=taken,
                                                                         h=len(self.plan.candidates) - taken)
            if not systems:
                text += self.tr("（設定ファイルに requestExports なし）")
            self.status.setText("。".join([text, *notes]))
            self._update_buttons()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("計算失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("本日分の計算"), work, done, failed)

    # --- The table ------------------------------------------------------------------------------

    def render(self) -> None:
        self._rendering = True
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        plan = self.plan
        for c in plan.candidates if plan else []:
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
            note = "・".join(x for x in (reason, *c.warnings) if x)
            boost = QStandardItem(f"+{c.boost_offer - r.boost}（{c.boost_reason}）" if c.boost_offer is not None else "")
            if c.boost_offer is not None:
                boost.setCheckable(True)
                boost.setCheckState(Qt.CheckState.Checked if c.boost_taken else Qt.CheckState.Unchecked)
                boost.setToolTip(self.tr("チェックで優先度を 100 繰り上げ（書き出しのときに台帳へ保存）"))
            items = [take, *(QStandardItem(t) for t in (
                c.system.label if c.system else "", satellite_name(c), r.name, r.category, prio, cloud,
                str(c.scenes) if c.result == TAKEN else "", (c.result + ("：" + note if note else "")))), boost]
            color = QColor("#16a34a" if c.result == TAKEN else "#6b7280")
            items[8].setForeground(QBrush(color))
            if c.warnings:
                items[8].setForeground(QBrush(QColor("#b45309")))
            self.model.appendRow(items)
        self._rendering = False
        rooms = room_text(plan.candidates, plan.systems) if plan else {}
        self.room.setText("　".join(f"{s.label} {rooms[s.system]}" for s in (plan.systems if plan else [])))
        self._update_buttons()

    def _item_changed(self, item: QStandardItem) -> None:
        if self.plan is None or self._rendering:
            return
        c = self.plan.candidates[item.row()]
        checked = item.checkState() == Qt.CheckState.Checked
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
        if keep is not None:
            self.table.selectRow(self.plan.candidates.index(keep))

    def move(self, step: int) -> None:
        """Moves the chosen row up or down (kept as a place in the order)."""
        if self.plan is None:
            return
        row = self.table.currentIndex().row()
        cands = self.plan.candidates
        if not (0 <= row < len(cands)) or not (0 <= row + step < len(cands)):
            return
        for i, c in enumerate(cands):
            c.rank = float(i)
        cands[row].rank, cands[row + step].rank = cands[row + step].rank, cands[row].rank
        self.refit(cands[row])

    def reset(self) -> None:
        if self.plan is None:
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
        self.write_button.setEnabled(has and not busy and any(c.result == TAKEN for c in self.plan.candidates))

    # --- Writing --------------------------------------------------------------------------------

    def write(self, ask: bool = True) -> None:
        """Builds the files (schemas read in a task), asks, writes them, then saves the history and raises."""
        plan = self.plan
        if plan is None or self.task is not None:
            return
        run = _now_ms()
        self.status.setText(self.tr("ファイルを作成中…"))

        def done(files):
            self.task = None
            self._update_buttons()
            self._write_files(plan, run, files, ask)

        def failed(message):
            self.task = None
            self.status.setText(self.tr("ファイルの作成に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("出力ファイルの作成"), lambda task: render(plan.candidates, plan.systems, plan.day, run, load_schema),
                             done, failed)
        self._update_buttons()

    def _write_files(self, plan: DayPlan, run: float, files: list, ask: bool) -> None:
        broken = [w for w in files if w.errors]
        good = [w for w in files if not w.errors]
        fallback = self.folder.text().strip()
        if any(not w.system.folder for w in good) and not fallback:
            self.status.setText(self.tr("出力先のフォルダを指定"))
            return
        targets = [(w, w.system.folder or fallback) for w in good]
        existing = [os.path.join(f, w.file_name) for w, f in targets if os.path.exists(os.path.join(f, w.file_name))]
        if ask:
            lines = [f"{w.system.label}: {os.path.join(f, w.file_name)}（{w.count} 件）" for w, f in targets]
            if broken:
                lines += [self.tr("書き出さないもの（スキーマに不一致など）:")] + [f"{w.system.label}: {'、'.join(w.errors[:3])}" for w in broken]
            if existing:
                lines.append(self.tr("上書きするファイル {n} 件").format(n=len(existing)))
            if QMessageBox.question(self, self.tr("書き出し"), "\n".join(lines)) != QMessageBox.StandardButton.Yes:
                self.status.setText(self.tr("書き出しを取りやめ"))
                return
        paths, problems = [], []
        written: dict = {}
        for w, folder in targets:
            try:
                paths.append(write(w, folder))
                written[w.system.system] = w.file_name
            except OSError as error:
                problems.append(f"{w.system.label}: {error.strerror or error}")
                continue
            _, notes = encoded(w)
            problems += [f"{w.system.label}: {n}" for n in notes + w.warnings[:3]]
        self.paths = paths
        if fallback:
            settings.set_request_export_folder(fallback)
        store, client, token = self.store, self.context.esri(), self.token()
        rows = history_of(plan, run, written)
        boosts = boost_changes(plan)
        done_rows = missed_done(plan)

        def work(task):
            saved = add_history(client, store, token, rows)
            raised = update_requests(client, store, token, boosts, run)
            update_history(client, store, token, done_rows)
            return saved, raised

        def done(result):
            self.task = None
            saved, raised = result
            text = self.tr("書き出し: {s}").format(s=written_summary([w for w, _ in targets if w.system.system in written]))
            if store.history:
                text += self.tr("・出力履歴 {n} 件").format(n=saved)
            if raised:
                text += self.tr("・繰り上げ {n} 件").format(n=raised)
            if broken:
                text += self.tr("・書き出さなかったシステム {n}（{list}）").format(n=len(broken), list="、".join(f"{w.system.label}: {w.errors[0]}" for w in broken))
            if problems:
                text += "・" + "、".join(problems)
            self.status.setText(text)
            self._update_buttons()
            self.requests_changed()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("ファイルは書き出し済み。出力履歴・繰り上げの保存に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("出力履歴の保存"), work, done, failed)
        self._update_buttons()
