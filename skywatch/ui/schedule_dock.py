"""
The 「撮像予定」 dock: a day's imaging plan as the satellite systems answered
it, picked on a calendar, listed and drawn on the map (tsudo, 2026-11-09:
「依頼した結果、その日の計画を見たい。採用されたかどうかを知りたい」).

A day's rows are those of the history table (TBL_REQUEST_HISTORY) for that
target day: the requests handed out (採用 / 不採用 by the system, or
不明), those held back (見送り), and the plans the systems made without
being asked (要求外), narrowed by system, state, 依頼グループ (the group the
history row was written with, else the ledger's) and words. Answers come from each system's result file
(`requestExports[].resultFile`) or by hand; 要求外 plans from the file or
by hand. core/request/adoption.py does the matching.
"""
from __future__ import annotations

import math
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsProject, QgsRectangle, QgsRendererCategory,
    QgsVectorLayer,
)
from qgis.gui import QgsMapToolExtent
from qgis.PyQt.QtCore import QCoreApplication, QDate, QDateTime, Qt, QTime, QTimer, QVariant
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QStandardItem, QStandardItemModel, QTextCharFormat
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCalendarWidget, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableView,
)

from ..core.request.adoption import (
    NOT_IN_FILE, Reading, answer_changes, manual_changes, manual_unrequested, match_answers, unrequested_rows,
)
from ..core.request.groups import read_groups
from ..core.request.model import JST, jst_day
from ..core.request.result_file import ADOPTED, REJECTED, WAITING, read_answers
from ..core.request.store import (
    ADOPTION_ROLES, AWAITING, DECLINED, HELD_BACK, IMAGE_ARRIVED, PLANNED, STATES, UNREQUESTED, HistoryRow, RequestStoreConfig, add_history,
    day_counts, delete_history, delete_history_rows, history_has, load_day, load_requests, state_of, unrequested_where,
    update_history,
)
from ..qgis_io.geometry import from_wgs84, multipolygon_of, polygon_of, shape_of, to_wgs84
from ..qgis_io.request_io import load_schema
from ..qgis_io.tasks import run_task
from .docks import PluginDock, plain_label, plain_tip
from .list_views import column_menus
from .plan_overlay import PlanOverlay
from .request_groups import GroupFilter

COLUMNS = ("状態", "名称", "依頼グループ", "管理番号", "システム", "衛星", "撮像予定", "撮像", "依頼者", "理由・メモ")
_TAKEN, _NOTE = COLUMNS.index("撮像"), COLUMNS.index("理由・メモ")
#: Each state's colour, on the list and the map.
STATE_COLORS = {PLANNED: "#16a34a", UNREQUESTED: "#7c3aed", AWAITING: "#2563eb", DECLINED: "#dc2626", HELD_BACK: "#9ca3af"}
#: The rows 「計画に入ったもの」 (what will be taken).
IN_PLAN = (PLANNED, UNREQUESTED)


def _now_ms() -> float:
    return time.time() * 1000


def _when(ms: Optional[float]) -> str:
    """「10:32」 (JST) of a planned time; the day too when it is not the target day's."""
    if ms is None or not math.isfinite(ms):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%m/%d %H:%M")


def row_state(row: HistoryRow) -> str:
    """Its state; a request whose image came in is 採用 (it was planned, whether or not the system said so)."""
    return state_of(row.result, row.adoption, row.captured)


def captured_text(row: HistoryRow) -> str:
    """「撮像済み」・「未撮像」・「未判定」 of a request handed out ("" for the others)."""
    return row.captured if row.result == "採用" and row.request_id else ""


def row_note(row: HistoryRow) -> str:
    """What the list says of why: the system's reason, else the plugin's (held back, warnings)."""
    return row.adoption_reason or row.reason


def tally_text(rows: list) -> str:
    """「計画 12（要求外 3）・不明 2・不採用 1・見送り 4」."""
    n = {s: 0 for s in STATES}
    for r in rows:
        n[row_state(r)] += 1
    parts = [f"計画 {n[PLANNED] + n[UNREQUESTED]}" + (f"（要求外 {n[UNREQUESTED]}）" if n[UNREQUESTED] else "")]
    parts += [f"{s} {n[s]}" for s in (AWAITING, DECLINED, HELD_BACK) if n[s]]
    return "・".join(parts)


class ScheduleDock(PluginDock):
    """A day's imaging plan: what the systems took in (and not), and their own plans."""

    def __init__(self, iface, context, open_settings, parent=None):
        super().__init__("SatObsSchedule", QCoreApplication.translate("ScheduleDock", "撮像予定"), context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.overlay = PlanOverlay(self.canvas)
        self.store: Optional[RequestStoreConfig] = None
        #: The day's rows (load_day), and those the list shows after the filters.
        self.rows: list[HistoryRow] = []
        self.shown: list[HistoryRow] = []
        self.chosen: Optional[HistoryRow] = None
        #: The day the rows are of (`2026-11-10`), once read.
        self.day: str = ""
        #: The ledger's areas by request id, for a history table without shapes.
        self._areas: dict = {}
        #: The ledger's 依頼グループ by request id, for rows written without one (before groups, or put in a group since).
        self._ledger_groups: dict = {}
        #: The groups of the store's table (core/request/groups.py), and whether it was read.
        self.groups: list = []
        self.groups_read = False
        #: The history's 採否 fields it lacks (answers cannot be kept without them).
        self.missing_fields: list = []
        self.task = None
        self._loaded = False
        self._month: tuple = ()
        self.unrequested_dialog = None
        #: Reading again once the task running is done (a day chosen meanwhile).
        self._again = QTimer(self)
        self._again.setSingleShot(True)
        self._again_args: tuple = (False, None)
        self._again.timeout.connect(self._read_again)
        self._build()
        context.configChanged.connect(self._config_changed)
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()

    # --- Building -------------------------------------------------------------------------------

    def _build(self) -> None:
        layout = self.body
        self.store_choice = QComboBox()
        self.store_choice.setToolTip(self.tr("依頼の保存先"))
        self.store_choice.currentIndexChanged.connect(self._store_chosen)
        layout.addWidget(self.store_choice)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(False)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setToolTip(self.tr("太字は計画のある日（緑は計画に入ったものあり、青は採否の分からないものだけ）"))
        today = jst_day(_now_ms())
        self.calendar.setSelectedDate(QDate(today.year, today.month, today.day))
        self.calendar.selectionChanged.connect(lambda: self.reload())
        self.calendar.currentPageChanged.connect(lambda _y, _m: self._mark_month())
        layout.addWidget(self.calendar)
        filters = QHBoxLayout()
        self.system_filter = QComboBox()
        self.system_filter.setToolTip(self.tr("このシステムの計画だけを表示"))
        self.state_filter = QComboBox()
        self.state_filter.addItem(self.tr("すべての状態"), None)
        self.state_filter.addItem(self.tr("計画に入ったもの（採用・要求外）"), "plan")
        for s in STATES:
            self.state_filter.addItem(s, s)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(self.tr("名称・番号・依頼者・グループ・衛星"))
        self.group_filter = GroupFilter("schedule")
        for w in (self.system_filter, self.state_filter, self.group_filter):
            w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(8)
            w.currentIndexChanged.connect(lambda _i: self.render())
            filters.addWidget(w)
        self.search.textChanged.connect(lambda _t: self.render())
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        # Names and keys from the ledger and the systems' result files: shown as written, never as HTML.
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
        self.table.selectionModel().currentRowChanged.connect(lambda current, _: self._row(current.row()))
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        layout.addWidget(self.table, 1)
        # The columns shown, hidden and in order from the header's right-click menu, kept per user.
        self.table_columns = column_menus("schedule", self.table, self)
        answer = QHBoxLayout()
        answer.addWidget(QLabel(self.tr("採否")))
        self.adopt_button = QPushButton(self.tr("採用"))
        self.adopt_button.setToolTip(self.tr("選んだ依頼（出力したもの）をシステムが採用したとして記録（手入力）"))
        self.adopt_button.clicked.connect(lambda: self.answer(ADOPTED))
        self.reject_button = QPushButton(self.tr("不採用"))
        self.reject_button.setToolTip(self.tr("選んだ依頼をシステムが採用しなかったとして記録（手入力。理由は右の欄）"))
        self.reject_button.clicked.connect(lambda: self.answer(REJECTED))
        self.wait_button = QPushButton(self.tr("不明に戻す"))
        self.wait_button.clicked.connect(lambda: self.answer(WAITING))
        self.reason = QLineEdit()
        self.reason.setPlaceholderText(self.tr("理由（任意）"))
        for w in (self.adopt_button, self.reject_button, self.wait_button):
            answer.addWidget(w)
        answer.addWidget(self.reason, 1)
        layout.addLayout(answer)
        more = QHBoxLayout()
        self.import_button = QPushButton(self.tr("結果ファイルの取り込み…"))
        self.import_button.setToolTip(self.tr("システムから返ってきた採否のファイル（requestExports の resultFile）を読み、"
                                              "この日の採用・不採用と要求外の計画を記録"))
        self.import_button.clicked.connect(lambda: self.import_file())
        self.add_button = QPushButton(self.tr("要求外を追加…"))
        self.add_button.setToolTip(self.tr("依頼していない計画（システム側の定常の撮像など）を手で追加"))
        self.add_button.clicked.connect(lambda: self.open_unrequested())
        self.delete_button = QPushButton(self.tr("削除"))
        self.delete_button.setToolTip(self.tr("選んだ要求外の計画を消す（依頼の行は消さない）"))
        self.delete_button.clicked.connect(lambda: self.delete_unrequested())
        for w in (self.import_button, self.add_button, self.delete_button):
            more.addWidget(w)
        more.addStretch(1)
        layout.addLayout(more)
        bottom = QHBoxLayout()
        self.on_map = QCheckBox(self.tr("地図に表示"))
        self.on_map.setChecked(True)
        self.on_map.toggled.connect(lambda _on: self.draw())
        self.layer_button = QPushButton(self.tr("レイヤーに出す"))
        self.layer_button.setToolTip(self.tr("表示中の計画を一時レイヤー（状態で色分け）にする"))
        self.layer_button.clicked.connect(lambda: self.to_layer())
        self.reload_button = QPushButton(self.tr("再読み込み"))
        self.reload_button.clicked.connect(lambda: self.reload(refresh=True))
        bottom.addWidget(self.on_map)
        bottom.addStretch(1)
        bottom.addWidget(self.layer_button)
        bottom.addWidget(self.reload_button)
        layout.addLayout(bottom)
        self._update_buttons()

    # --- Settings -------------------------------------------------------------------------------

    def _config_changed(self) -> None:
        stores = self.context.config.request_stores
        self.store_choice.blockSignals(True)
        self.store_choice.clear()
        for s in stores:
            self.store_choice.addItem(s.label)
        self.store_choice.blockSignals(False)
        self.store_choice.setVisible(len(stores) > 1)
        self.store = stores[0] if stores else None
        self._fill_systems()
        self._forget()
        if self.store is None:
            self.status.setText(self.tr("設定ファイルに撮像依頼の保存先（requestStores）なし"))
        elif not self.store.history:
            self.status.setText(self.tr("撮像依頼の保存先に出力履歴（history）なし。撮像予定は出力履歴に記録する"))
        elif self.isVisible():
            self.reload(refresh=True)

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.request_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self._forget()
        self.reload(refresh=True)

    def _forget(self) -> None:
        self.rows, self.shown, self.chosen, self.day, self._areas, self._month = [], [], None, "", {}, ()
        self._ledger_groups, self.groups, self.groups_read = {}, [], False
        self.group_filter.set_groups([], ())
        self._loaded = False
        self.render()

    def _fill_systems(self) -> None:
        box = self.system_filter
        was = box.currentData() if box.count() else None
        box.blockSignals(True)
        box.clear()
        box.addItem(self.tr("すべてのシステム"), None)
        for s in self.context.config.request_exports:
            box.addItem(s.label if s.label == s.system else f"{s.label}（{s.system}）", s.system)
        index = box.findData(was) if was is not None else 0
        box.setCurrentIndex(max(0, index))
        box.blockSignals(False)

    def _visibility(self, visible: bool) -> None:
        if visible and self.store is not None and not self._loaded:
            self.reload(refresh=True)
        if not visible:
            self.overlay.unpaint()
            self.canvas.refresh()
        else:
            self.draw()

    def token(self) -> Optional[str]:
        return self.context.token_for(self.store.url, self.store.token) if self.store else None

    def chosen_day(self) -> date:
        d = self.calendar.selectedDate()
        return date(d.year(), d.month(), d.day())

    def set_day(self, day: date) -> None:
        """Shows a day (reads it; the calendar's choice does)."""
        if self.chosen_day() != day:
            self.calendar.setSelectedDate(QDate(day.year, day.month, day.day))
        elif day.isoformat() != self.day:
            self.reload()

    # --- Reading --------------------------------------------------------------------------------

    def _start(self, what: str, work, done, failed=None) -> bool:
        if self.task is not None:
            return False

        def finish(result):
            self.task = None
            self._update_buttons()
            done(result)

        def fail(message):
            self.task = None
            self._update_buttons()
            (failed or self.status.setText)(message)

        self.task = run_task(what, work, finish, fail)
        self._update_buttons()
        return True

    def _read_again(self) -> None:
        refresh, then = self._again_args
        self._again_args = (False, None)
        self.reload(refresh, then)

    def reload(self, refresh: bool = False, then=None) -> None:
        """Reads the chosen day's rows (`refresh`: the ledger's areas and the table's fields again too)."""
        if self.store is None or not self.store.history:
            return
        if self.task is not None:
            # A day chosen while another is read: read again once it is done.
            # (A timer of the dock's own: gone with it when the plugin is unloaded meanwhile.)
            self._again_args = (refresh or self._again_args[0], then or self._again_args[1])
            self._again.start(300)
            return
        store, client, token = self.store, self.context.esri(), self.token()
        day = self.chosen_day().isoformat()
        areas = None if refresh or not self._areas else self._areas
        ledger_groups = dict(self._ledger_groups) if areas is not None else {}
        read_table = refresh or not self.groups_read
        self.status.setText(self.tr("{d} の計画を読み込み中…").format(d=day))

        def work(task):
            rows = load_day(client, store, token, day, task.isCanceled)
            missing = history_has(client, store, token, ADOPTION_ROLES) if refresh or areas is None else None
            found, groups_of = areas, ledger_groups
            if (found is None or any(r.request_id and not r.rings and r.request_id not in found for r in rows)
                    or any(r.request_id and not r.group and r.request_id not in groups_of for r in rows)):
                # A table without shapes (or rows written before it had them): the ledger's areas. Rows written
                # without a group: the ledger's group (a request gone from the ledger: none, not asked again).
                requests = load_requests(client, store, token, task.isCanceled)
                found = {q.id: q.rings for q in requests}
                groups_of = {q.id: q.group for q in requests}
                groups_of.update({r.request_id: "" for r in rows if r.request_id and r.request_id not in groups_of})
            groups = read_groups(client, store.groups, store.group_fields, token, task.isCanceled) if read_table else None
            return rows, missing, found, groups_of, groups

        def done(result):
            rows, missing, found, groups_of, groups = result
            self.rows, self._areas, self._ledger_groups, self.day = rows, found, groups_of, day
            if groups is not None and groups[0] is not None:
                self.groups, self.groups_read = groups[0], True
            self.group_filter.set_groups(self.groups, (self.group_of(r) for r in rows))
            if missing is not None:
                self.missing_fields = missing
            self._loaded = True
            if self.chosen is not None:
                self.chosen = next((r for r in rows if r.object_id == self.chosen.object_id), None)
            self.render()
            self._mark_month(force=True)
            if then:
                then()

        self._start(self.tr("撮像予定の読み込み"), work, done)

    def _mark_month(self, force: bool = False) -> None:
        """Marks the days of the month shown that have a plan (bold; green when something is in it)."""
        if self.store is None or not self.store.history:
            return
        year, month = self.calendar.yearShown(), self.calendar.monthShown()
        if self._month == (year, month) and not force:
            return
        self._month = (year, month)
        first = date(year, month, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        store, client, token = self.store, self.context.esri(), self.token()

        def done(counts):
            plain = QTextCharFormat()
            day = first
            while day <= last:
                self.calendar.setDateTextFormat(QDate(day.year, day.month, day.day), plain)
                day += timedelta(days=1)
            for text, tally in counts.items():
                try:
                    d = date.fromisoformat(text)
                except ValueError:
                    continue
                f = QTextCharFormat()
                f.setFontWeight(QFont.Weight.Bold)
                planned = tally.get(PLANNED, 0) + tally.get(UNREQUESTED, 0)
                if planned:
                    f.setForeground(QBrush(QColor(STATE_COLORS[PLANNED])))
                elif tally.get(AWAITING):
                    f.setForeground(QBrush(QColor(STATE_COLORS[AWAITING])))
                f.setToolTip("・".join(f"{s} {tally[s]}" for s in STATES if tally.get(s)))
                self.calendar.setDateTextFormat(QDate(d.year, d.month, d.day), f)

        def failed(_message):
            self._month = ()

        run_task(self.tr("撮像予定の日の印"), lambda task: day_counts(client, store, token, first.isoformat(), last.isoformat()),
                 done, failed)

    # --- The list -------------------------------------------------------------------------------

    def group_of(self, row: HistoryRow) -> str:
        """A row's 依頼グループ: the one it was written with, else the request's in the ledger."""
        return row.group or self._ledger_groups.get(row.request_id, "")

    def _passes(self, row: HistoryRow) -> bool:
        system, state = self.system_filter.currentData(), self.state_filter.currentData()
        if system and row.system != system:
            return False
        if not self.group_filter.passes(self.group_of(row)):
            return False
        s = row_state(row)
        if state == "plan" and s not in IN_PLAN:
            return False
        if state and state != "plan" and s != state:
            return False
        words = self.search.text().lower().split()
        if words:
            text = " ".join((row.name, row.management_no, row.requester, self.group_of(row), row.satellite, row.request_id,
                             row.external_id)).lower()
            if not all(w in text for w in words):
                return False
        return True

    def render(self) -> None:
        self.shown = [r for r in self.rows if self._passes(r)]
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(h) for h in COLUMNS])
        chosen_row = -1
        for i, r in enumerate(self.shown):
            state = row_state(r)
            items = [QStandardItem(t) for t in (state, r.name or r.external_id or r.request_id, self.group_of(r), r.management_no,
                                                r.system, r.satellite, _when(r.planned_at), captured_text(r), r.requester, row_note(r))]
            items[0].setForeground(QBrush(QColor(STATE_COLORS[state])))
            source = IMAGE_ARRIVED if r.captured == "撮像済み" and r.result == "採用" and not r.adoption_source else r.adoption_source
            if source:
                items[0].setToolTip(plain_tip(self.tr("採否の入力元: {s}").format(s=source)))
            elif state == AWAITING:
                items[0].setToolTip(self.tr("システムの採否が分からない（画像が入れば採用になる）"))
            if r.image_id:
                items[_TAKEN].setToolTip(plain_tip(self.tr("画像 {i}").format(i=r.image_id)))
            if row_note(r):
                items[_NOTE].setToolTip(plain_tip(row_note(r)))  # reasons from the systems' files
            self.model.appendRow(items)
            if self.chosen is not None and r.object_id == self.chosen.object_id:
                chosen_row = i
        if chosen_row >= 0:
            self.table.selectionModel().blockSignals(True)
            self.table.selectRow(chosen_row)
            self.table.selectionModel().blockSignals(False)
        if self.day:
            text = f"{self.day}: {tally_text(self.rows)}" if self.rows else self.tr("{d}: 計画なし").format(d=self.day)
            if len(self.shown) != len(self.rows):
                text += self.tr("（表示 {n} 件）").format(n=len(self.shown))
            if self.missing_fields:
                text += self.tr("。出力履歴に属性 {f} がないため採否は記録できない（tools/arcgis/create_tables.py で足す）").format(
                    f="・".join(self.missing_fields))
            self.status.setText(text)
        self.draw()
        self._update_buttons()

    def selected(self) -> list[HistoryRow]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.shown[i] for i in rows if 0 <= i < len(self.shown)]

    def _row(self, row: int) -> None:
        if 0 <= row < len(self.shown):
            self.chosen = self.shown[row]
            self.draw()
            self.zoom()

    def rings_of(self, row: HistoryRow) -> list:
        return row.rings or self._areas.get(row.request_id) or []

    def draw(self) -> None:
        if not self.on_map.isChecked() or not self.isVisible():
            self.overlay.unpaint()
            self.canvas.refresh()
            return
        polygons = []
        # The chosen one last, so it is on top.
        for r in sorted(self.shown, key=lambda r: self.chosen is not None and r.object_id == self.chosen.object_id):
            rings = self.rings_of(r)
            if rings:
                chosen = self.chosen is not None and r.object_id == self.chosen.object_id
                polygons.append((rings, STATE_COLORS[row_state(r)], 3 if chosen else 1, (r.name or r.external_id) if chosen else ""))
        self.overlay.paint(polygons)
        self.canvas.refresh()

    def zoom(self) -> None:
        box: Optional[QgsRectangle] = None
        for r in ([self.chosen] if self.chosen else self.shown):
            for ring in self.rings_of(r):
                for x, y in ring:
                    if box is None:
                        box = QgsRectangle(x, y, x, y)
                    else:
                        box.combineExtentWith(x, y)
        if box is None:
            return
        box = box.buffered(max(box.width(), box.height()) * 0.2 + 0.01)
        self.canvas.setExtent(from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(box))
        self.canvas.refresh()

    def _update_buttons(self) -> None:
        ready = self.store is not None and bool(self.store and self.store.history) and self.task is None and bool(self.day)
        can_answer = ready and not self.missing_fields
        chosen = self.selected() if self.model.rowCount() else []
        handed = [r for r in chosen if r.result == "採用" and r.request_id]
        for b in (self.adopt_button, self.reject_button, self.wait_button):
            b.setEnabled(can_answer and bool(handed))
        self.import_button.setEnabled(can_answer)
        self.add_button.setEnabled(can_answer)
        self.delete_button.setEnabled(ready and any(r.result == UNREQUESTED for r in chosen))
        self.layer_button.setEnabled(bool(self.shown))
        self.reload_button.setEnabled(self.store is not None and self.task is None)

    # --- Answers by hand ------------------------------------------------------------------------

    def answer(self, answer: str, rows: Optional[list] = None) -> None:
        """Records the systems' answer to the rows chosen (handed out): 採用 / 不採用, or 不明 again."""
        rows = self.selected() if rows is None else rows
        changes = manual_changes(rows, answer, self.reason.text().strip())
        if not changes:
            return
        store, client, token = self.store, self.context.esri(), self.token()

        def work(task):
            return update_history(client, store, token, changes)

        def done(saved):
            self.reason.clear()
            note = self.tr("{a} {n} 件を記録").format(a=answer, n=int(saved))
            if saved.problems:
                note += "・" + "、".join(saved.problems[:3])
            self.reload(then=lambda: self.status.setText(f"{self.status.text()}。{note}"))

        self._start(self.tr("採否の記録"), work, done)

    # --- Result files ---------------------------------------------------------------------------

    def _file_system(self) -> Optional[object]:
        """The system whose file is read: the one chosen, else asked among those with a `resultFile`."""
        systems = [s for s in self.context.config.request_exports if s.result_file is not None]
        chosen = self.system_filter.currentData()
        if chosen:
            system = next((s for s in self.context.config.request_exports if s.system == chosen), None)
            if system is not None and system.result_file is None:
                self.status.setText(self.tr("{s} に結果ファイルの設定（requestExports の resultFile）なし").format(s=system.label))
                return None
            return system
        if not systems:
            self.status.setText(self.tr("結果ファイルの設定（requestExports の resultFile）のあるシステムなし。採否は手入力で"))
            return None
        if len(systems) == 1:
            return systems[0]
        names = [s.label for s in systems]
        name, ok = QInputDialog.getItem(self, self.tr("結果ファイルの取り込み"), self.tr("どのシステムのファイルか"), names, 0, False)
        return systems[names.index(name)] if ok else None

    def import_file(self, path: Optional[str] = None, system=None, ask: bool = True) -> None:
        """Reads a system's result file for the day shown, tells what it says, and records it."""
        system = system or self._file_system()
        if system is None or not self.day:
            return
        if path is None:
            folder = system.folder or ""
            path, _ = QFileDialog.getOpenFileName(self, self.tr("{s} の結果ファイル").format(s=system.label), folder,
                                                  self.tr("結果ファイル (*.csv *.tsv *.json *.txt);;すべて (*)"))
            if not path:
                return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as error:
            self.status.setText(self.tr("読めない: {m}").format(m=error.strerror or error))
            return
        spec = system.result_file
        store, client, token = self.store, self.context.esri(), self.token()
        day, name = self.day, os.path.basename(path)

        def work(task):
            answers, problems = read_answers(data, spec, load_schema)
            rows = load_day(client, store, token, day, task.isCanceled)
            requests = load_requests(client, store, token, task.isCanceled)
            reading = match_answers(answers, rows, requests, system.system, day, spec, name, system.label)
            reading.problems = problems + reading.problems
            return reading

        def done(reading: Reading):
            if ask and not self._confirm_reading(reading, system.label):
                self.status.setText(self.tr("取り込みを取りやめ"))
                return
            self._save_reading(reading)

        self._start(self.tr("結果ファイルの読み込み"), work, done)

    def _confirm_reading(self, reading: Reading, label: str) -> bool:
        t = reading.tally()
        lines = [self.tr("{s}・{d}・{f}").format(s=label, d=reading.day, f=reading.source),
                 self.tr("採用 {a}・不採用 {r}・要求外 {u}").format(a=t[ADOPTED], r=t[REJECTED], u=t[UNREQUESTED])
                 + (self.tr("・不明 {w}").format(w=t[WAITING]) if t[WAITING] else "")]
        if t[NOT_IN_FILE]:
            lines.append(self.tr("出力したがファイルにない依頼 {n} 件: {what}").format(
                n=t[NOT_IN_FILE], what=REJECTED if reading.missing_as == REJECTED else self.tr("不明のまま")))
        lines.append(self.tr("この日・このシステムの要求外（前に結果ファイルから入れたもの）は置き換える"))
        if reading.problems:
            lines += ["", self.tr("読めなかった行など:")] + reading.problems[:8] + (["…"] if len(reading.problems) > 8 else [])
        return QMessageBox.question(self, self.tr("結果ファイルの取り込み"), plain_tip("\n".join(lines))) == QMessageBox.StandardButton.Yes

    def _save_reading(self, reading: Reading) -> None:
        store, client, token = self.store, self.context.esri(), self.token()
        changes = answer_changes(reading)
        adds = unrequested_rows(reading, _now_ms())

        def work(task):
            gone = delete_history(client, store, token, unrequested_where(store, reading.day, reading.system, by_hand=False))
            changed = update_history(client, store, token, changes)
            added = add_history(client, store, token, adds)
            return changed, added, gone.problems + changed.problems + added.problems

        def done(result):
            changed, added, problems = result
            t = reading.tally()
            note = self.tr("{f}: 採用 {a}・不採用 {r}・要求外 {u} を記録").format(
                f=reading.source, a=t[ADOPTED], r=t[REJECTED] + (t[NOT_IN_FILE] if reading.missing_as == REJECTED else 0), u=int(added))
            if t[WAITING]:
                note += self.tr("（不明 {w}）").format(w=t[WAITING])
            if problems or reading.problems:
                note += self.tr("（{m}）").format(m="、".join((problems + reading.problems)[:3]))
            self.reload(then=lambda: self.status.setText(f"{self.status.text()}。{note}"))

        self._start(self.tr("採否の記録"), work, done)

    # --- 要求外 by hand ---------------------------------------------------------------------------

    def open_unrequested(self) -> None:
        if self.unrequested_dialog is not None:
            self.unrequested_dialog.close()
        self.unrequested_dialog = UnrequestedDialog(self)
        self.unrequested_dialog.show()

    def add_unrequested(self, row: HistoryRow) -> None:
        store, client, token = self.store, self.context.esri(), self.token()

        def done(saved):
            note = self.tr("要求外「{n}」を追加").format(n=row.name) if saved else "、".join(saved.problems[:3])
            self.reload(then=lambda: self.status.setText(f"{self.status.text()}。{note}"))

        self._start(self.tr("要求外の追加"), lambda task: add_history(client, store, token, [row]), done)

    def delete_unrequested(self, rows: Optional[list] = None, ask: bool = True) -> None:
        rows = [r for r in (self.selected() if rows is None else rows) if r.result == UNREQUESTED and r.object_id is not None]
        if not rows:
            return
        if ask and QMessageBox.question(self, self.tr("削除"), self.tr("要求外の計画 {n} 件を消しますか").format(n=len(rows))) \
                != QMessageBox.StandardButton.Yes:
            return
        store, client, token = self.store, self.context.esri(), self.token()
        ids = [r.object_id for r in rows]

        def done(saved):
            note = self.tr("要求外 {n} 件を削除").format(n=int(saved))
            self.reload(then=lambda: self.status.setText(f"{self.status.text()}。{note}"))

        self._start(self.tr("要求外の削除"), lambda task: delete_history_rows(client, store, token, ids), done)

    # --- A layer --------------------------------------------------------------------------------

    def to_layer(self) -> Optional[QgsVectorLayer]:
        """The rows shown as a scratch layer (WGS 84 polygons), coloured by state."""
        if not self.shown:
            return None
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", self.tr("撮像予定 {d}").format(d=self.day), "memory")
        names = ("状態", "名称", "依頼グループ", "管理番号", "システム", "衛星", "撮像予定", "撮像", "依頼者", "理由", "対象日", "依頼ID", "外部ID",
                 "入力元")
        provider = layer.dataProvider()
        provider.addAttributes([QgsField(n, QVariant.String) for n in names])
        layer.updateFields()
        features = []
        for r in self.shown:
            f = QgsFeature(layer.fields())
            f.setAttributes([row_state(r), r.name, self.group_of(r), r.management_no, r.system, r.satellite, _when(r.planned_at),
                             captured_text(r), r.requester, row_note(r), r.day, r.request_id, r.external_id, r.adoption_source])
            rings = [[tuple(p[:2]) for p in ring] for ring in self.rings_of(r)]
            if rings:
                # One ring a polygon; more, a polygon each (as the map shows them).
                f.setGeometry(polygon_of(rings) if len(rings) == 1 else multipolygon_of(rings))
            features.append(f)
        provider.addFeatures(features)
        layer.updateExtents()
        categories = []
        for state in STATES:
            color = QColor(STATE_COLORS[state])
            fill = QColor(color)
            fill.setAlpha(60)
            symbol = QgsFillSymbol.createSimple({"color": f"{fill.red()},{fill.green()},{fill.blue()},{fill.alpha()}",
                                                 "outline_color": color.name(), "outline_width": "0.6"})
            categories.append(QgsRendererCategory(state, symbol, state))
        layer.setRenderer(QgsCategorizedSymbolRenderer("状態", categories))
        QgsProject.instance().addMapLayer(layer)
        return layer

    def close_all(self) -> None:
        """Takes the plan off the map (when the plugin is unloaded)."""
        self._again.stop()
        if self.unrequested_dialog is not None:
            self.unrequested_dialog.close()
            self.unrequested_dialog = None
        self.overlay.unpaint()


class UnrequestedDialog(QDialog):
    """A 要求外 plan added by hand: its name, system, satellite, time and area (drawn on the map or a selected feature)."""

    def __init__(self, dock: ScheduleDock):
        super().__init__(dock)
        self.dock = dock
        self.canvas = dock.canvas
        self.rings: list = []
        self.setWindowTitle(self.tr("要求外の計画を追加（{d}）").format(d=dock.day))
        self.setModal(False)
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText(self.tr("例: 定常観測 東京湾"))
        form.addRow(self.tr("名称"), self.name)
        self.system = QComboBox()
        for s in dock.context.config.request_exports:
            self.system.addItem(s.label if s.label == s.system else f"{s.label}（{s.system}）", s.system)
        if dock.system_filter.currentData():
            self.system.setCurrentIndex(max(0, self.system.findData(dock.system_filter.currentData())))
        form.addRow(self.tr("システム"), self.system)
        self.satellite = QLineEdit()
        form.addRow(self.tr("衛星"), self.satellite)
        self.timed = QCheckBox(self.tr("撮像予定"))
        self.when = QDateTimeEdit()
        self.when.setDisplayFormat("yyyy-MM-dd HH:mm")
        day = dock.chosen_day()
        self.when.setDateTime(QDateTime(QDate(day.year, day.month, day.day), QTime(10, 0)))
        self.when.setEnabled(False)
        self.timed.toggled.connect(self.when.setEnabled)
        timed = QHBoxLayout()
        timed.addWidget(self.timed)
        timed.addWidget(self.when, 1)
        form.addRow(self.tr("日時（日本時間）"), timed)
        self.note = QLineEdit()
        form.addRow(self.tr("メモ"), self.note)
        area = QHBoxLayout()
        self.box_button = QPushButton(self.tr("地図で範囲"))
        self.box_button.setCheckable(True)
        self.box_button.toggled.connect(self._tool)
        self.selected_button = QPushButton(self.tr("選択中の地物から"))
        self.selected_button.clicked.connect(self._from_selection)
        self.area_label = QLabel(self.tr("範囲なし（一覧だけに出る）"))
        area.addWidget(self.box_button)
        area.addWidget(self.selected_button)
        area.addWidget(self.area_label, 1)
        form.addRow(self.tr("範囲"), area)
        self.message = plain_label(QLabel())
        form.addRow(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.extent_tool = QgsMapToolExtent(self.canvas)
        self.extent_tool.extentChanged.connect(self._extent_drawn)
        self.finished.connect(lambda _r: self._tool(False))

    def _tool(self, on: bool) -> None:
        if on:
            self.canvas.setMapTool(self.extent_tool)
        elif self.canvas.mapTool() is self.extent_tool:
            self.canvas.unsetMapTool(self.extent_tool)

    def _extent_drawn(self, extent: QgsRectangle) -> None:
        if extent.isEmpty():
            return
        g = QgsGeometry.fromRect(extent)
        g.transform(to_wgs84(self.canvas.mapSettings().destinationCrs()))
        b = g.boundingBox()
        self.set_rings([[[b.xMinimum(), b.yMinimum()], [b.xMaximum(), b.yMinimum()], [b.xMaximum(), b.yMaximum()],
                         [b.xMinimum(), b.yMaximum()], [b.xMinimum(), b.yMinimum()]]])
        self.box_button.setChecked(False)

    def _from_selection(self) -> None:
        layer = self.dock.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.selectedFeatureCount():
            self.message.setText(self.tr("作業中のレイヤーで地物を選択"))
            return
        shape = shape_of(next(layer.getSelectedFeatures()).geometry(), to_wgs84(layer.crs()))
        if shape is None:
            return
        if shape.polygons:
            self.set_rings([[list(p) for p in ring] for ring in shape.polygons[0]])
        else:
            xs, ys = [v[0] for v in shape.vertices], [v[1] for v in shape.vertices]
            pad = 0.045 if len(shape.vertices) == 1 else 0
            w, s, e, n = min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad
            self.set_rings([[[w, s], [e, s], [e, n], [w, n], [w, s]]])

    def set_rings(self, rings: list) -> None:
        self.rings = rings
        self.message.setText("")
        self.area_label.setText(self.tr("範囲あり"))

    def save(self) -> None:
        name = self.name.text().strip()
        if not name:
            self.message.setText(self.tr("名称を入力"))
            return
        if self.system.currentData() is None:
            self.message.setText(self.tr("設定ファイルにシステム（requestExports）なし"))
            return
        planned = None
        if self.timed.isChecked():
            dt = self.when.dateTime()
            d, t = dt.date(), dt.time()
            planned = datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), tzinfo=JST).timestamp() * 1000
        row = manual_unrequested(self.dock.day, self.system.currentData(), name, _now_ms(), self.rings,
                                 satellite=self.satellite.text().strip(), planned_at=planned, note=self.note.text().strip())
        self.dock.add_unrequested(row)
        self.accept()
