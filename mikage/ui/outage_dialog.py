"""
「衛星の障害情報」 (tsudo, 2026-11-16): the outages of the satellites
(`satelliteOutages`, core/plan/outages.py) in a list, and their form.

The list shows those not recovered (予定・発生中・見込み超過) first, the
recovered ones too on 「復旧済みも表示」 (they are never deleted: the
history). 新規・編集・復旧・削除 are for the imaging planners
(`outages.edit`); the others only look. Each save goes to the table in a task,
and the outages read again are handed to the context so that the plan, the
satellite tree and the schedule show them at once.

Times are Japan's (as the plugin's days), whatever the PC's zone.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Callable, Optional, Sequence

from qgis.PyQt.QtCore import QDateTime, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from ..core.plan.api import plain
from ..core.plan.api import (
    DIRECTIONS, ONGOING, OVERDUE, PLANNED, RECOVERED, Outage, check_outage, delete_outages, load_outages, new_id, save_outage,
    when_text,
)
from ..qgis_io.tasks import error_text, run_task
from .docks import ask_no, plain_label, plain_tip
from .fit import fit_text
from .list_views import column_menus

COLUMNS = ("状態", "衛星", "センサー・モード", "発生", "復旧見込み", "復旧", "影響", "理由", "メモ")
STATUS_COLORS = {ONGOING: "#c2410c", OVERDUE: "#b91c1c", PLANNED: "#b45309", RECOVERED: "#6b7280"}
#: The feature of 新規・編集・復旧・削除 (core/roles.FEATURES).
EDIT = "outages.edit"
#: Japan's offset (the edits show JST on any PC).
JST_MS = 9 * 3_600_000
#: The longest texts (the table's lengths cut them anyway).
REASON_MAX, OTHER_MAX = 1000, 1000


def now_ms() -> float:
    return time.time() * 1000


def _time_edit(ms: float) -> QDateTimeEdit:
    """A date-time box in JST (kept as UTC moved by 9 hours, so that the PC's zone never comes in)."""
    e = QDateTimeEdit()
    e.setTimeSpec(Qt.TimeSpec.UTC)
    e.setDateTime(QDateTime.fromMSecsSinceEpoch(int(ms) + JST_MS, Qt.TimeSpec.UTC))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd HH:mm")
    return e


def _ms(edit: QDateTimeEdit) -> float:
    """The moment of a box of `_time_edit` (seconds dropped)."""
    ms = edit.dateTime().toMSecsSinceEpoch() - JST_MS
    return float(ms - ms % 60_000)


def _choices(sats: Sequence) -> dict:
    """name → {sensor → [modes]} of the satellites known (and each series, under its own name, with no sensor)."""
    out: dict = {}
    for s in sats:
        sensors = out.setdefault(s.name, {})
        modes = sensors.setdefault(s.sensor or "", [])
        if s.mode and s.mode not in modes:
            modes.append(s.mode)
    for s in sats:
        if s.series and plain(s.series) not in {plain(n) for n in out}:
            out.setdefault(s.series, {})
    return out


class OutageForm(QDialog):
    """One outage: what it hits, why, when, and its impacts."""

    def __init__(self, sats: Sequence, outage: Optional[Outage] = None, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.original = outage
        self.choices = _choices(sats)
        self.series = {s.series for s in sats if s.series} - {s.name for s in sats}
        self.setWindowTitle(self.tr("衛星の障害の登録") if outage is None else self.tr("衛星の障害の編集"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.satellite = QComboBox()
        self.satellite.setEditable(True)
        for name in self.choices:
            self.satellite.addItem(name)
            if name in self.series:
                self.satellite.setItemData(self.satellite.count() - 1, self.tr("シリーズ名: そのシリーズの全機"), Qt.ItemDataRole.ToolTipRole)
        self.satellite.setToolTip(self.tr("衛星カタログの名前（ID・別名でも）。シリーズ名ならそのシリーズの全機"))
        self.satellite.setCurrentText(outage.satellite if outage else "")
        form.addRow(self.tr("衛星"), self.satellite)
        self.sensor = QComboBox()
        self.sensor.setEditable(True)
        self.sensor.setToolTip(self.tr("このセンサーだけなら選ぶ（空は全センサー）"))
        form.addRow(self.tr("センサー"), self.sensor)
        self.mode = QComboBox()
        self.mode.setEditable(True)
        self.mode.setToolTip(self.tr("このモードだけなら選ぶ（空は全モード）"))
        form.addRow(self.tr("モード"), self.mode)
        self.satellite.currentTextChanged.connect(lambda _=None: self._fill_sensors())
        self.sensor.currentTextChanged.connect(lambda _=None: self._fill_modes())
        self._fill_sensors(outage.sensor if outage else "", outage.mode if outage else "")
        self.reason = QLineEdit(outage.reason if outage else "")
        self.reason.setMaxLength(REASON_MAX)
        self.reason.setPlaceholderText(self.tr("姿勢制御の不具合、電力不足、計画停止 など"))
        form.addRow(self.tr("理由"), self.reason)
        now = now_ms()
        self.start = _time_edit(outage.start if outage else now)
        form.addRow(self.tr("発生日時（日本時間）"), self.start)
        self.expected, self.no_expected = self._optional_time(outage.expected if outage else None, self.tr("未定"), now, True)
        form.addRow(self.tr("復旧見込み"), self._row(self.expected, self.no_expected))
        self.recovered, self.has_recovered = self._optional_time(outage.recovered if outage else None, self.tr("復旧済み"), now, False)
        form.addRow(self.tr("復旧日時"), self._row(self.recovered, self.has_recovered))
        layout.addWidget(self._impacts(outage))
        notes = QFormLayout()
        self.other = QLineEdit(outage.other if outage else "")
        self.other.setMaxLength(OTHER_MAX)
        self.other.setToolTip(self.tr("計算には使わず、注意として出す影響（画質の低下など）"))
        notes.addRow(self.tr("その他の影響"), self.other)
        self.note = QPlainTextEdit(outage.note if outage else "")
        self.note.setFixedHeight(60)
        notes.addRow(self.tr("メモ"), self.note)
        layout.addLayout(notes)
        self.problem = plain_label(QLabel())
        self.problem.setStyleSheet("color: #b91c1c")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("保存"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _optional_time(self, ms: Optional[float], label: str, now: float, inverted: bool) -> tuple[QDateTimeEdit, QCheckBox]:
        """A time box and its check box (`inverted`: ticked means no time, as 「未定」)."""
        edit = _time_edit(ms if ms is not None else now)
        box = QCheckBox(label)
        box.setChecked((ms is None) == inverted)
        box.toggled.connect(lambda on: edit.setEnabled(on != inverted))
        edit.setEnabled(box.isChecked() != inverted)
        return edit, box

    @staticmethod
    def _row(*widgets) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            row.addWidget(x)
        row.addStretch(1)
        return w

    def _impacts(self, o: Optional[Outage]) -> QGroupBox:
        group = QGroupBox(self.tr("影響（いくつでも）"))
        grid = QGridLayout(group)
        self.stop = QCheckBox(self.tr("撮像停止"))
        self.stop.setToolTip(self.tr("この衛星（センサー・モード）の撮像機会を使わない"))
        self.stop.setChecked(bool(o and o.stop))
        grid.addWidget(self.stop, 0, 0, 1, 3)
        grid.addWidget(QLabel(self.tr("使えない方向")), 1, 0)
        self.directions: dict = {}
        for k, (role, asc, side, label) in enumerate(DIRECTIONS):
            box = QCheckBox(self.tr(label))
            box.setToolTip(self.tr("チェックした軌道の向き・観測方向の撮像機会を使わない（「昇交の左のみ撮れる」はほかの 3 つをチェック）"))
            box.setChecked(bool(o and (asc, side) in o.closed))
            self.directions[(asc, side)] = box
            grid.addWidget(box, 1 + k // 2, 1 + k % 2)
        self.limits: dict = {}
        rows = (
            ("max_off_nadir", self.tr("オフナディア角の上限"), QDoubleSpinBox, (0.1, 90.0, 20.0), " °"),
            ("max_per_day", self.tr("1 日の撮像回数の上限"), QSpinBox, (1, 999, 1), self.tr(" 回")),
            ("scenes_per_pass", self.tr("1 パスのシーン数の上限"), QSpinBox, (1, 999, 1), self.tr(" シーン")),
            ("min_interval", self.tr("撮像間隔の下限"), QDoubleSpinBox, (0.1, 1440.0, 10.0), self.tr(" 分")),
        )
        for k, (key, label, kind, (low, high, default), suffix) in enumerate(rows):
            box = QCheckBox(label)
            spin = kind()
            spin.setRange(low, high)
            if isinstance(spin, QDoubleSpinBox):
                spin.setDecimals(1)
            spin.setSuffix(suffix)
            value = getattr(o, key) if o else None
            spin.setValue(value if value is not None else default)
            box.setChecked(value is not None)
            spin.setEnabled(box.isChecked())
            box.toggled.connect(spin.setEnabled)
            self.limits[key] = (box, spin)
            grid.addWidget(box, 3 + k, 0)
            grid.addWidget(spin, 3 + k, 1)
        self.no_night = QCheckBox(self.tr("夜間は撮像できない"))
        self.no_night.setToolTip(self.tr("撮像の場所で太陽が地平線より下の撮像機会を使わない"))
        self.no_night.setChecked(bool(o and o.no_night))
        grid.addWidget(self.no_night, 3 + len(rows), 0, 1, 3)
        return group

    def _fill_sensors(self, sensor: Optional[str] = None, mode: Optional[str] = None) -> None:
        keep = self.sensor.currentText() if sensor is None else sensor
        sensors = self._sensors_of(self.satellite.currentText())
        self.sensor.blockSignals(True)
        self.sensor.clear()
        self.sensor.addItem("")
        self.sensor.setItemData(0, self.tr("（全センサー）"), Qt.ItemDataRole.ToolTipRole)
        self.sensor.addItems([s for s in sensors if s])
        self.sensor.setCurrentText(keep)
        self.sensor.blockSignals(False)
        self._fill_modes(mode)

    def _fill_modes(self, mode: Optional[str] = None) -> None:
        keep = self.mode.currentText() if mode is None else mode
        sensors = self._sensors_of(self.satellite.currentText())
        chosen = self.sensor.currentText().strip()
        modes = [m for s, ms in sensors.items() if not chosen or plain(s) == plain(chosen) for m in ms]
        self.mode.clear()
        self.mode.addItem("")
        self.mode.setItemData(0, self.tr("（全モード）"), Qt.ItemDataRole.ToolTipRole)
        self.mode.addItems(list(dict.fromkeys(modes)))
        self.mode.setCurrentText(keep)

    def _sensors_of(self, name: str) -> dict:
        key = plain(name)
        return next((v for n, v in self.choices.items() if plain(n) == key), {})

    def outage(self) -> Outage:
        """The outage as the form says (with the id and object id of the one edited)."""
        o = self.original
        limits = {key: (spin.value() if box.isChecked() else None) for key, (box, spin) in self.limits.items()}
        for key in ("max_per_day", "scenes_per_pass"):
            if limits[key] is not None:
                limits[key] = int(limits[key])
        values = dict(
            satellite=self.satellite.currentText().strip(), sensor=self.sensor.currentText().strip(), mode=self.mode.currentText().strip(),
            reason=self.reason.text().strip(), start=_ms(self.start),
            expected=None if self.no_expected.isChecked() else _ms(self.expected),
            recovered=_ms(self.recovered) if self.has_recovered.isChecked() else None,
            stop=self.stop.isChecked(), closed=frozenset(k for k, box in self.directions.items() if box.isChecked()),
            no_night=self.no_night.isChecked(), other=self.other.text().strip(), note=self.note.toPlainText().strip(), **limits,
        )
        if o is None:
            return Outage(id=new_id(), **values)
        return dataclasses.replace(o, **values)

    def _accept(self) -> None:
        problems = check_outage(self.outage())
        if problems:
            self.problem.setText("、".join(problems))
            return
        self.accept()


class OutageDialog(QDialog):
    """The outages of the table: the list, and 新規・編集・復旧・削除 for those who plan."""

    def __init__(self, context, satellites: Callable[[], list], allows: Callable[[str], bool] = lambda _f: True, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.context = context
        self.satellites = satellites
        self.allows = allows
        self.task = None
        self.outages: list[Outage] = list(context.known_outages())
        #: The outages the table shows (row → Outage).
        self.shown: list[Outage] = []
        #: What was just saved, said again once read again.
        self.note = ""
        self.setWindowTitle(self.tr("衛星の障害情報"))
        self.resize(1000, 520)
        self._build()
        self.render()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.show_recovered = QCheckBox(self.tr("復旧済みも表示"))
        self.show_recovered.setToolTip(self.tr("復旧した障害（履歴）も一覧に出す"))
        self.show_recovered.toggled.connect(lambda _on: self.render())
        top.addWidget(self.show_recovered)
        top.addStretch(1)
        self.reload_button = QPushButton(self.tr("読み直し"))
        self.reload_button.clicked.connect(lambda: self.run())
        top.addWidget(self.reload_button)
        layout.addLayout(top)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.model = QStandardItemModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        self.table.doubleClicked.connect(lambda _i: self.edit())
        layout.addWidget(self.table, 1)
        self.columns = column_menus("outages", self.table, self)
        row = QHBoxLayout()
        self.add_button = QPushButton(self.tr("新規…"))
        self.add_button.setToolTip(self.tr("衛星の障害を登録"))
        self.add_button.clicked.connect(lambda: self.add())
        self.edit_button = QPushButton(self.tr("編集…"))
        self.edit_button.clicked.connect(lambda: self.edit())
        self.recover_button = QPushButton(self.tr("復旧"))
        self.recover_button.setToolTip(self.tr("選んだ障害の復旧日時を今に変更（あとで「編集…」で修正できる）"))
        self.recover_button.clicked.connect(lambda: self.recover())
        self.delete_button = QPushButton(self.tr("削除…"))
        self.delete_button.setToolTip(self.tr("間違えて入れた障害を消す（復旧した障害は消さずに「復旧」）"))
        self.delete_button.clicked.connect(lambda: self.delete())
        for b in (self.add_button, self.edit_button, self.recover_button, self.delete_button):
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)
        self._update_buttons()

    @property
    def source(self):
        return self.context.config.satellite_outages

    def token(self) -> Optional[str]:
        return self.context.token_for(self.source.url, self.source.token)

    # --- Reading ----------------------------------------------------------------------------------

    def run(self) -> None:
        """Reads the table again."""
        if self.task is not None:
            return
        if self.source is None:
            self.status.setText(self.tr("衛星の障害情報の表が未設定（管理者に連絡: satelliteOutages）［E703］"))
            return
        source, client, token = self.source, self.context.esri(), self.token()
        self.status.setText(self.tr("衛星の障害情報を取得中…"))

        def work(task):
            return load_outages(client, source, token, task.isCanceled)

        def done(outages):
            self.task = None
            self.outages = list(outages)
            self.context.set_outages(self.outages)
            self.render()

        def failed(message):
            self.task = None
            self.note = ""
            self.status.setText(self.tr("取得に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("衛星の障害情報の取得"), work, done, failed)
        self._update_buttons()

    # --- Showing ----------------------------------------------------------------------------------

    def render(self) -> None:
        now = now_ms()
        chosen = {o.id for o in self.selected()}
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        order = {ONGOING: 0, OVERDUE: 0, PLANNED: 1, RECOVERED: 2}
        everything = sorted(self.outages, key=lambda o: (order[o.status(now)], -o.start, o.target))
        self.shown = [o for o in everything if self.show_recovered.isChecked() or o.open(now)]
        for o in self.shown:
            status = o.status(now)
            part = " ".join(x for x in (o.sensor, o.mode) if x) or self.tr("（全部）")
            cells = [status, o.satellite, part, when_text(o.start), when_text(o.expected), when_text(o.recovered, ""),
                     o.impact_text(), o.reason, o.note]
            items = [QStandardItem(c) for c in cells]
            items[0].setForeground(QBrush(QColor(STATUS_COLORS[status])))
            items[0].setToolTip(plain_tip(o.short_text(now)))
            for k in (COLUMNS.index("影響"), COLUMNS.index("理由"), COLUMNS.index("メモ")):
                items[k].setToolTip(plain_tip(cells[k]))
            self.model.appendRow(items)
        for r, o in enumerate(self.shown):
            if o.id in chosen:
                self.table.selectRow(r)
        opened = sum(1 for o in self.outages if o.open(now))
        text = self.tr("復旧していない障害 {n} 件（全 {m} 件）").format(n=opened, m=len(self.outages))
        if self.source is None:
            text = self.tr("衛星の障害情報の表が未設定（管理者に連絡: satelliteOutages）［E703］")
        self.status.setText("。".join(t for t in (self.note, text) if t))
        self.note = ""
        self._update_buttons()

    def selected(self) -> list[Outage]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.shown[r] for r in rows if 0 <= r < len(self.shown)]

    def _update_buttons(self) -> None:
        editing = self.allows(EDIT) and self.source is not None
        for b in (self.add_button, self.edit_button, self.recover_button, self.delete_button):
            b.setVisible(editing)
        idle = self.task is None
        chosen = self.selected()
        now = now_ms()
        self.reload_button.setEnabled(idle and self.source is not None)
        self.add_button.setEnabled(idle)
        self.edit_button.setEnabled(idle and len(chosen) == 1)
        self.recover_button.setEnabled(idle and any(o.recovered is None and o.start <= now for o in chosen))
        self.delete_button.setEnabled(idle and bool(chosen))

    # --- 新規・編集・復旧・削除 --------------------------------------------------------------------

    def form(self, outage: Optional[Outage] = None) -> OutageForm:
        return OutageForm(self.satellites(), outage, self)

    def add(self) -> Optional[OutageForm]:
        if not self.allows(EDIT) or self.task is not None or self.source is None:
            return None
        form = self.form()
        form.accepted.connect(lambda: self._save([form.outage()], self.tr("障害を登録")))
        form.open()
        return form

    def edit(self) -> Optional[OutageForm]:
        chosen = self.selected()
        if len(chosen) != 1 or not self.allows(EDIT) or self.task is not None or self.source is None:
            return None
        form = self.form(chosen[0])
        form.accepted.connect(lambda: self._save([form.outage()], self.tr("障害を更新")))
        form.open()
        return form

    def recover(self) -> None:
        now = float(int(now_ms()) // 60_000 * 60_000)
        chosen = [o for o in self.selected() if o.recovered is None and o.start <= now]
        if chosen and self.allows(EDIT):
            self._save([dataclasses.replace(o, recovered=now) for o in chosen], self.tr("{n} 件を復旧に").format(n=len(chosen)))

    def delete(self, ask: bool = True) -> None:
        chosen = self.selected()
        if not chosen or not self.allows(EDIT) or self.task is not None or self.source is None:
            return
        names = "、".join(o.target for o in chosen[:5]) + (self.tr(" ほか {n} 件").format(n=len(chosen) - 5) if len(chosen) > 5 else "")
        if ask and not ask_no(self, self.tr("障害の削除"), self.tr(
                "選んだ {n} 件の障害（{names}）を削除（復元できない）。復旧した障害は、削除せず「復旧」にすると履歴に残る").format(
                n=len(chosen), names=names), yes=self.tr("削除")):
            return
        source, client, token = self.source, self.context.esri(), self.token()

        def work(task):
            return delete_outages(client, source, chosen, token)

        def done(result):
            self.task = None
            gone, problems = result
            self.note = "。".join(t for t in (self.tr("{n} 件を削除").format(n=len(gone)), "、".join(problems[:3])) if t)
            self.run()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("削除に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("衛星の障害情報の削除"), work, done, failed)
        self._update_buttons()

    def _save(self, outages: list[Outage], what: str) -> None:
        if self.task is not None or self.source is None:
            return
        source, client, token = self.source, self.context.esri(), self.token()

        def work(task):
            saved, problems = [], []
            for o in outages:
                try:
                    saved.append(save_outage(client, source, o, token))
                except Exception as error:  # noqa: BLE001 (each outage on its own; told)
                    problems.append(f"{o.target}: {error_text(error)}")
            return saved, problems

        def done(result):
            self.task = None
            saved, problems = result
            text = what if len(saved) == len(outages) else self.tr("{what}（{n}/{m} 件）").format(what=what, n=len(saved), m=len(outages))
            self.note = "。".join(t for t in (text, "、".join(problems[:3])) if t)
            self.run()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("保存に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("衛星の障害情報の保存"), work, done, failed)
        self._update_buttons()
