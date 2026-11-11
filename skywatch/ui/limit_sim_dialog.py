"""
「上限シミュレーション」, opened from the 「撮像計画ファイル出力」 dock: the
day's work run over the coming days with the systems' daily limits, or
which systems are used, changed by hand, to see how many requests overflow,
go on to the next system, or miss their deadline before the limits are put
in the settings (core/request/simulate.py).

The ledger and the history are only read; nothing is written. The settings'
limits are run alongside, so each figure can be set against them.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Optional

from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QDateEdit, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout,
)

from ..core.plan.weather import fetch_clouds
from ..core.request.model import CATEGORIES, priority_text
from ..core.request.simulate import (
    MOST_DAYS, SUMMARY, SystemChange, change_of, changed_systems, deadline_text, horizon_days, run_passes, simulate,
)
from ..core.request.store import history_where, load_history, load_requests
from ..core.request.workflow import clouds_of, forecast_places, orbiting
from ..qgis_io import settings
from ..qgis_io.network import get_bytes
from ..qgis_io.tasks import run_task

#: The columns of the systems' table.
SYSTEM_COLUMNS = ("使う", "システム", "件数上限", "枚数上限", *(f"{c}枠" for c in CATEGORIES))
DAY_COLUMNS = ("日", "候補", "採用", "まとめ", "枠超過", "回送", "雲量", "該当なし", "機会なし", "繰り上げ", "持ち越し", "欠測", "期限切れ", "使用")
EXPIRED_COLUMNS = ("期限", "依頼", "区分", "優先度", "最終日の理由")


def _limit(text: str) -> Optional[int]:
    """A limit typed in: a whole number 0 or more, or blank for none."""
    t = text.strip()
    if not t:
        return None
    try:
        n = int(float(t))
    except ValueError:
        return None
    return max(0, n)


class LimitSimDialog(QDialog):
    """The days to run, the systems' limits to try, and what comes of them."""

    def __init__(self, context, store, satellites, day: date, parent=None):
        super().__init__(parent)
        self.context = context
        self.store = store
        #: Calls back with the satellites (the imaging plan's catalog).
        self.satellites = satellites
        self.task = None
        #: What was read: (requests, history, sats); forecasts by (start, days).
        self.data = None
        self.forecasts: dict = {}
        self.forecast_key = None
        self.base = None
        self.trial = None
        self.setObjectName("SatObsLimitSim")
        self.setWindowTitle(self.tr("上限シミュレーション"))
        self.resize(820, 640)
        self._build(day)

    def _build(self, day: date) -> None:
        layout = QVBoxLayout(self)
        hint = QLabel(self.tr("日ごとの上限や使うシステムを変えた場合に、今後の日数で何件が枠超過・次のシステムへ回送・期限切れになるかを試算。"
                              "台帳・出力履歴には書き込まない。採用した依頼は撮像できたものとして計算。"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("開始日（日本時間）")))
        self.start = QDateEdit()
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("yyyy-MM-dd")
        self.start.setDate(QDate(day.year, day.month, day.day))
        top.addWidget(self.start)
        top.addWidget(QLabel(self.tr("日数")))
        self.days = QSpinBox()
        self.days.setRange(1, MOST_DAYS)
        self.days.setValue(14)
        top.addWidget(self.days)
        self.weather = QCheckBox(self.tr("雲量予報を使う"))
        self.weather.setToolTip(self.tr("予報のある日だけ雲量で見送り・優先度に反映。予報のない日は晴れとして計算"))
        forecast = self.context.config.cloud_forecast
        self.weather.setChecked(forecast is not None)
        self.weather.setEnabled(forecast is not None)
        top.addWidget(self.weather)
        top.addStretch(1)
        layout.addLayout(top)

        self.systems = QTableWidget(0, len(SYSTEM_COLUMNS))
        self.systems.setHorizontalHeaderLabels([self.tr(c) for c in SYSTEM_COLUMNS])
        self.systems.verticalHeader().hide()
        self.systems.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.systems.setToolTip(self.tr("上限は空欄で制限なし"))
        layout.addWidget(self.systems)
        buttons = QHBoxLayout()
        self.run_button = QPushButton(self.tr("試算"))
        self.run_button.clicked.connect(lambda: self.run())
        buttons.addWidget(self.run_button)
        self.reset_button = QPushButton(self.tr("設定の値に戻す"))
        self.reset_button.clicked.connect(self.fill_systems)
        buttons.addWidget(self.reset_button)
        self.reload_button = QPushButton(self.tr("台帳を読み直す"))
        self.reload_button.setToolTip(self.tr("依頼・出力履歴・雲量予報を読み直してから試算"))
        self.reload_button.clicked.connect(lambda: self.run(reload=True))
        buttons.addWidget(self.reload_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.tabs = QTabWidget()
        self.summary = self._table(("項目", "設定の値", "試算", "差"))
        self.daily = self._table(DAY_COLUMNS)
        self.expired = self._table(EXPIRED_COLUMNS)
        self.tabs.addTab(self.summary, self.tr("合計"))
        self.tabs.addTab(self.daily, self.tr("日ごと"))
        self.tabs.addTab(self.expired, self.tr("期限切れ"))
        layout.addWidget(self.tabs, 1)
        self.fill_systems()

    def _table(self, columns) -> QTableWidget:
        t = QTableWidget(0, len(columns))
        t.setHorizontalHeaderLabels([self.tr(c) for c in columns])
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        return t

    # --- The systems tried ----------------------------------------------------------------------

    def fill_systems(self) -> None:
        """The systems' table with the settings' values."""
        systems = self.context.config.request_exports
        self.systems.setRowCount(len(systems))
        for row, s in enumerate(systems):
            ch = change_of(s)
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            use.setCheckState(Qt.CheckState.Checked)
            self.systems.setItem(row, 0, use)
            name = QTableWidgetItem(s.label)
            name.setFlags(Qt.ItemFlag.ItemIsEnabled)
            name.setData(Qt.ItemDataRole.UserRole, s.system)
            self.systems.setItem(row, 1, name)
            values = [ch.count, ch.scenes, *(ch.quota.get(c) for c in CATEGORIES)]
            for col, v in enumerate(values, start=2):
                self.systems.setItem(row, col, QTableWidgetItem("" if v is None else str(v)))

    def changes(self) -> dict:
        """What the systems' table says to try: system name → SystemChange."""
        out = {}
        for row in range(self.systems.rowCount()):
            name = self.systems.item(row, 1).data(Qt.ItemDataRole.UserRole)
            text = [self.systems.item(row, col).text() if self.systems.item(row, col) else "" for col in range(2, len(SYSTEM_COLUMNS))]
            quota = {c: _limit(t) for c, t in zip(CATEGORIES, text[2:]) if _limit(t) is not None}
            out[name] = SystemChange(self.systems.item(row, 0).checkState() == Qt.CheckState.Checked,
                                     _limit(text[0]), _limit(text[1]), quota)
        return out

    def chosen_start(self) -> date:
        d = self.start.date()
        return date(d.year(), d.month(), d.day())

    # --- Running --------------------------------------------------------------------------------

    def run(self, reload: bool = False) -> None:
        """Reads the ledger (once, or again), then runs the settings' limits and those tried."""
        if self.task is not None or self.store is None:
            return
        start, days = self.chosen_start(), self.days.value()
        key = (start, days) if self.weather.isChecked() else None
        if self.data is not None and not reload and key == self.forecast_key:
            self._simulate()
            return
        self.run_button.setEnabled(False)
        self.status.setText(self.tr("衛星カタログを読み込み中…"))
        self.satellites(lambda sats: self._load(sats, start, days, key, reload or self.data is None), self._satellites_failed)

    def _satellites_failed(self, message: str) -> None:
        """The satellite catalog could not be read: says why and gives 「試算」 back (#70)."""
        self.status.setText(self.tr("衛星カタログの読み込みに失敗: {m}").format(m=message))
        self.run_button.setEnabled(True)

    def _load(self, sats, start: date, days: int, key, read_ledger: bool) -> None:
        store, client = self.store, self.context.esri()
        token = self.context.token_for(store.url, store.token)
        forecast = self.context.config.cloud_forecast if key is not None else None
        authcfg = settings.auth_for(forecast.url) if forecast else None
        old = self.data
        self.status.setText(self.tr("依頼・出力履歴・雲量予報を読み込み中…"))

        def work(task):
            notes = []
            if read_ledger or old is None:
                requests = load_requests(client, store, token, task.isCanceled)
                history = (load_history(client, store, token, history_where(store, open_rows=True), cancelled=task.isCanceled)
                           if store.history else [])
            else:
                requests, history = old[0], old[1]
            forecasts = {}
            if forecast is not None:
                places = list(dict.fromkeys(p for d in horizon_days(start, days) for p in forecast_places(requests, d, sats)))
                if places:
                    try:
                        forecasts = fetch_clouds(forecast, places, lambda url: get_bytes(url, authcfg), task.isCanceled)
                    except Exception as error:  # noqa: BLE001 (the run goes on without the weather)
                        notes.append(f"雲量予報の取得失敗（{error}）")
            return requests, history, forecasts, notes

        def done(result):
            self.task = None
            requests, history, forecasts, notes = result
            self.data = (requests, history, list(sats))
            self.forecasts, self.forecast_key = forecasts, key
            self._simulate(notes)

        def failed(message):
            self.task = None
            self.status.setText(self.tr("読み込み失敗: {m}").format(m=message))
            self.run_button.setEnabled(True)

        self.task = run_task(self.tr("上限シミュレーションの読み込み"), work, done, failed)

    def _simulate(self, notes=()) -> None:
        """Runs the settings' limits and those tried in a task (a large ledger takes seconds, #83); draws the result."""
        if self.task is not None:
            return
        requests, history, sats = self.data
        start, days = self.chosen_start(), self.days.value()
        forecasts = self.forecasts if self.weather.isChecked() else {}

        def clouds_for(systems_, passes):
            """A day's cover by request: at the moment of each pass of the satellites fitted on the orbits, else the daylight mean."""
            used = orbiting(systems_, sats)
            return lambda day: clouds_of(requests, day, forecasts, passes=passes, sats=used) if forecasts else {}

        systems = self.context.config.request_exports
        tried = changed_systems(systems, self.changes())
        self.run_button.setEnabled(False)
        self.status.setText(self.tr("試算中…"))

        def work(task):
            # The satellites' passes over the days, once for both runs.
            passes = run_passes(requests, start, days, sats, time.time() * 1000, task.isCanceled)
            task.check()
            task.progress(30)
            base = simulate(requests, start, days, systems, sats, clouds_for(systems, passes), history, task.isCanceled, passes)
            task.check()
            task.progress(65)
            trial = simulate(requests, start, days, tried, sats, clouds_for(tried, passes), history, task.isCanceled, passes)
            task.check()
            return base, trial

        def done(result):
            self.task = None
            self.base, self.trial = result
            self.render()
            text = self.tr("{start} から {n} 日・依頼 {r} 件で試算（台帳への書き込みなし）").format(
                start=start.isoformat(), n=days, r=len(requests))
            if not systems:
                text += self.tr("（設定ファイルに requestExports なし）")
            self.status.setText("。".join([text, *notes]))
            self.run_button.setEnabled(True)

        def failed(message):
            self.task = None
            self.status.setText(self.tr("試算できず: {m}").format(m=message))
            self.run_button.setEnabled(True)

        self.task = run_task(self.tr("上限シミュレーションの試算"), work, done, failed)

    # --- Showing --------------------------------------------------------------------------------

    def render(self) -> None:
        base, trial = self.base, self.trial
        self.summary.setRowCount(len(SUMMARY))
        for row, (label, get) in enumerate(SUMMARY):
            b, t = get(base), get(trial)
            cells = [self.tr(label), str(b), str(t), "" if t == b else f"{t - b:+d}"]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 3 and t != b:
                    worse = (t < b) if label == "採用" else (t > b)
                    item.setForeground(QBrush(QColor("#b91c1c" if worse else "#16a34a")))
                self.summary.setItem(row, col, item)
        labels = {s.system: s.label for s in self.context.config.request_exports}
        limits = {s.system: s for s in trial.systems}
        self.daily.setRowCount(len(trial.days))
        for row, d in enumerate(trial.days):
            used = []
            for name, (n, scenes) in d.used.items():
                s = limits.get(name)
                count = f"{n}/{s.limit['count']}" if s and "count" in s.limit else str(n)
                sc = f"{scenes}/{s.limit['scenes']}" if s and "scenes" in s.limit else str(scenes)
                used.append(f"{labels.get(name, name)} {count} 件・{sc} 枚")
            cells = [d.day.isoformat(), d.candidates, d.taken, d.merged, d.over, d.spilled, d.cloud, d.no_system, d.no_pass, d.raised,
                     d.carried, d.lost, len(d.expired), "　".join(used)]
            for col, v in enumerate(cells):
                item = QTableWidgetItem(str(v))
                if col == DAY_COLUMNS.index("期限切れ") and d.expired:
                    item.setForeground(QBrush(QColor("#b91c1c")))
                self.daily.setItem(row, col, item)
        expired = trial.expired
        self.expired.setRowCount(len(expired))
        for row, e in enumerate(expired):
            r = e.request
            cells = [deadline_text(r), r.name or r.id, r.category, priority_text(r.priority), e.reason]
            for col, text in enumerate(cells):
                self.expired.setItem(row, col, QTableWidgetItem(text))
        self.tabs.setTabText(2, self.tr("期限切れ（{n}）").format(n=len(expired)))
