"""
The 「撮像予定」 dock: a day's imaging plan as the satellite systems answered
it, picked on a calendar, listed and drawn on the map (tsudo, 2026-11-09:
「依頼した結果、その日の計画を見たい。採用されたかどうかを知りたい」).

A day's rows are those of the history table (TBL_REQUEST_HISTORY) for that
target day: the requests handed out (採用 / 不採用 by the system, or
不明), those held back (見送り), and the plans the systems made without
being asked (要求外), narrowed by system, state, 依頼グループ (the group the
history row was written with, else the ledger's) and words. Each request is
drawn with its own area; with the shot layer (`requestStores[].shots`) the
shots written are drawn over them dashed, and 「まとめ」 tells which request
was taken alone, carried others or went in another's shot (tsudo,
2026-11-11, core/request/shot_view.py). Answers come from each system's result file
(`requestExports[].resultFile`) or by hand; 要求外 plans from the file or
by hand. core/request/adoption.py does the matching.

A shot handed out that a satellite outage entered since hits (衛星の障害情報:
撮像停止, its direction, angle or the night at its moment) is told in the
list (「衛星の障害の影響あり」, the satellite in orange); nothing is changed:
whether to hand it out again is the planner's (tsudo 2026-11-16, decision 4).

A row 撮像済み whose image is rated 使えない all over the request's area
(画像の評価, `imageReviews`) is marked too: 「撮像済み（要確認）」 in orange,
「画像の評価: 使えない（雲）」 in 理由; it stays 撮像済み (設計 2).
"""
from __future__ import annotations

import math
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsProject, QgsRectangle, QgsRendererCategory,
    QgsSingleSymbolRenderer, QgsVectorLayer,
)
from qgis.gui import QgsMapToolExtent
from qgis.PyQt.QtCore import QCoreApplication, QDate, QDateTime, Qt, QTime, QTimer, QVariant
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QStandardItem, QStandardItemModel, QTextCharFormat
from ..core.image_reviews import UnusableImages
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCalendarWidget, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QPushButton, QTableView,
)

from ..core.request.api import (
    NOT_IN_FILE, Reading, answer_changes, manual_changes, manual_unrequested, match_answers, read_window, unrequested_rows,
)
from ..core import errors
from ..core.esri import EsriError
from ..core.plan.api import plain
from ..core.plan.api import handed_hit
from ..core.request.api import ScheduleRepository
from ..core.request.api import JST, jst_day
from ..core.request.api import ADOPTED, RESULT_REJECTED as REJECTED, WAITING, read_answers
from ..core.request.api import (
    ADOPTION_ROLES, AWAITING, DECLINED, HELD_BACK, IMAGE_ARRIVED, PLANNED, STATES, UNREQUESTED, HistoryRow, RequestStoreConfig, state_of,
)
from ..core.request.api import companions, shot_text, shot_tip
from ..core.request.api import FOLLOW_REDO, redo_mark, redo_text
from ..qgis_io import map_tools
from ..qgis_io.geometry import from_wgs84, multipolygon_of, polygon_of, shape_of, to_wgs84
from ..qgis_io.request_io import load_schema
from ..qgis_io.tasks import run_task
from .docks import PluginDock, ask_no, plain_label, plain_tip
from .fit import fit_text
from .flow_layout import FlowLayout
from .list_views import column_menus
from .memo_column import MEMO, memo_item
from .plan_overlay import PlanOverlay
from .check_filter import CheckFilter
from .request_groups import GroupFilter

#: 「理由」 (the systems' and the plugin's reasons; 「理由・メモ」 until 2026-11-24) and 「メモ」 (the request's note,
#: from the ledger; hidden until shown from the header's menu, tsudo 2026-11-24).
COLUMNS = ("状態", "名称", "依頼グループ", "管理番号", "システム", "衛星", "まとめ", "撮像予定", "撮像", "画像ID", "依頼者", "理由", MEMO)
_TAKEN, _NOTE, _SHOT, _NAME = COLUMNS.index("撮像"), COLUMNS.index("理由"), COLUMNS.index("まとめ"), COLUMNS.index("名称")
_SATELLITE, _SYSTEM = COLUMNS.index("衛星"), COLUMNS.index("システム")
#: A row a satellite outage hits (衛星の障害情報).
OUTAGE_COLOR = "#c2410c"
#: 地域の形が変わった rows (出し直し候補・前の形で採用済み) in 「理由」.
REDO_COLOR = "#b45309"
#: The shots' outlines on the map (dashed over the requests' areas).
SHOT_COLOR = "#7c3aed"
#: Each state's colour, on the list and the map.
STATE_COLORS = {PLANNED: "#16a34a", UNREQUESTED: "#7c3aed", AWAITING: "#2563eb", DECLINED: "#dc2626", HELD_BACK: "#9ca3af"}
#: The rows 「計画に入ったもの」 (what will be taken).
IN_PLAN = (PLANNED, UNREQUESTED)
#: The states shown until others are chosen: all but 見送り (not handed out that day).
NOT_HELD_BACK = tuple(s for s in STATES if s != HELD_BACK)


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


def image_text(row: HistoryRow) -> str:
    """The id of the image a row was taken in (撮像済み; tsudo 2026-11-16: 「せめて画像IDは表示してほしい」), "" for the others."""
    return row.image_id if row.captured == "撮像済み" else ""


def unusable_marks(unusable: Optional[UnusableImages], rows, areas: dict) -> dict:
    """
    Of a day's rows 撮像済み, those whose image is rated 使えない over the row's
    area (else the request's): object id → 「画像の評価: 使えない（雲）」 (in the task).
    """
    if unusable is None:
        return {}
    taken = [r for r in rows if r.captured == "撮像済み" and r.image_id and r.object_id is not None]
    unusable.read(r.image_id for r in taken)
    out = {}
    for r in taken:
        rings = r.rings or areas.get(r.request_id)
        text = unusable.text(r.image_id, rings) if rings else ""
        if text:
            out[r.object_id] = text
    return out


def row_note(row: HistoryRow) -> str:
    """What the list says of why: the system's reason, else the plugin's (held back, warnings)."""
    return row.adoption_reason or row.reason


def days_text(days: list) -> str:
    """「2026-11-21・2026-11-22」; more than three days, 「2026-11-21〜2026-11-27（7 日）」."""
    return "・".join(days) if len(days) <= 3 else f"{days[0]}〜{days[-1]}（{len(days)} 日）"


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
        #: The ledger's メモ by request id (the 「メモ」 column; the history rows do not keep it).
        self._ledger_notes: dict = {}
        #: The groups of the store's table (core/request/groups.py), and whether it was read.
        self.groups: list = []
        self.groups_read = False
        #: The history's 採否 fields it lacks (answers cannot be kept without them).
        self.missing_fields: list = []
        #: The day's shots (撮像の範囲, the shot layer's rows) by SHOT_ID, and why they could not be read.
        self.shots: dict = {}
        self.shot_problem = ""
        #: The shots' scratch layer last made by 「レイヤーに出す」.
        self.shot_layer: Optional[QgsVectorLayer] = None
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
        #: 衛星の障害情報 as read with the day (and kept up to date), and the satellites known (the imaging plan's catalog,
        #: set by the plugin) for their other names and series.
        self.outages: list = []
        #: Rows 撮像済み whose image is rated 使えない over the area: their object id → 「画像の評価: 使えない（雲）」.
        self.review_marks: dict = {}
        self.satellites_known = lambda: []
        context.outagesChanged.connect(self._outages_changed)
        self.visibilityChanged.connect(self._visibility)
        self._config_changed()

    # --- Building -------------------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        """Whether the day's rows were read (「撮像計画ファイル出力」 asks before reading them again)."""
        return self._loaded

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
        # Rows of filters and buttons wrap, so that a dock at its usual width shows them all without scrolling sideways.
        filters = FlowLayout()
        self.system_filter = QComboBox()
        self.system_filter.setToolTip(self.tr("このシステムの計画だけを表示"))
        # 状態 and 依頼グループ: check boxes, several at once (e.g. 採用 and 不採用 side by side). Unless chosen before:
        # all but 見送り (tsudo, 2026-11-16: 「初期値を、見送り以外にしてほしい」).
        self.state_filter = CheckFilter(
            self.tr("すべての状態"), [(s, s, "", STATE_COLORS[s]) for s in STATES],
            presets=[(self.tr("見送り以外"), NOT_HELD_BACK), (self.tr("計画に入ったもの（採用・要求外）"), IN_PLAN)],
            key="schedule/states", default=NOT_HELD_BACK,
            tip=self.tr("この状態の計画だけを表示（チェックしたもののどれか）。次回も同じ選択"))
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(self.tr("名称・番号・依頼者・グループ・衛星・画像ID"))
        self.group_filter = GroupFilter("schedule")
        for w in (self.system_filter, self.state_filter, self.group_filter):
            w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(14)
            filters.addWidget(w)
        self.system_filter.currentIndexChanged.connect(lambda _i: self.render())
        self.state_filter.changed.connect(self.render)
        self.group_filter.changed.connect(self.render)
        self.search.textChanged.connect(lambda _t: self.render())
        self.search.setMinimumWidth(200)
        filters.addWidget(self.search)
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
        self.table_columns = column_menus("schedule", self.table, self, hidden=(self.tr(MEMO),))
        answer = FlowLayout()
        self.answer_label = QLabel(self.tr("採否"))
        answer.addWidget(self.answer_label)
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
        self.reason.setMinimumWidth(160)
        answer.addWidget(self.reason)
        layout.addLayout(answer)
        more = FlowLayout()
        self.import_button = QPushButton(self.tr("結果ファイルの取り込み…"))
        self.import_button.setToolTip(self.tr("システムから返ってきた採否のファイルを読み込み、採用・不採用と要求外の計画を記録（表示中の日でなく、ファイルの行の日に記録）"))
        self.import_button.clicked.connect(lambda: self.import_file())
        self.add_button = QPushButton(self.tr("要求外を追加…"))
        self.add_button.setToolTip(self.tr("依頼していない計画（システム側の定常の撮像など）を手で追加"))
        self.add_button.clicked.connect(lambda: self.open_unrequested())
        self.delete_button = QPushButton(self.tr("削除"))
        self.delete_button.setToolTip(self.tr("選んだ要求外の計画を削除（依頼の行は残る）"))
        self.delete_button.clicked.connect(lambda: self.delete_unrequested())
        for w in (self.import_button, self.add_button, self.delete_button):
            more.addWidget(w)
        layout.addLayout(more)
        bottom = FlowLayout()
        self.on_map = QCheckBox(self.tr("地図に表示"))
        self.on_map.setChecked(True)
        self.on_map.toggled.connect(lambda _on: self.draw())
        self.shots_on_map = QCheckBox(self.tr("撮像の範囲"))
        self.shots_on_map.setChecked(True)
        self.shots_on_map.setToolTip(self.tr("書き出したときの撮像の範囲（まとめた依頼は書き出した四角）を破線で重ねる。"
                                             "依頼の範囲はそれぞれの依頼のまま"))
        self.shots_on_map.toggled.connect(lambda _on: self.draw())
        self.layer_button = QPushButton(self.tr("レイヤーに出す"))
        self.layer_button.setToolTip(self.tr("表示中の計画をレイヤーとして追加（依頼の範囲を状態で色分け。撮像の範囲は破線の別レイヤー）"))
        self.layer_button.clicked.connect(lambda: self.to_layer())
        self.reload_button = QPushButton(self.tr("最新を取得"))
        self.reload_button.clicked.connect(lambda: self.reload(refresh=True))
        bottom.addWidget(self.on_map)
        bottom.addWidget(self.shots_on_map)
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
            self.status.setText(self.tr("撮像依頼の保存先が未設定（管理者に連絡: requestStores）［E703］"))
        elif not self.store.history:
            self.status.setText(self.tr("出力履歴が未設定のため、撮像予定を記録できない（管理者に連絡: history）［E703］"))
        elif self.isVisible():
            self.reload(refresh=True)

    def _store_chosen(self, index: int) -> None:
        stores = self.context.config.request_stores
        self.store = stores[index] if 0 <= index < len(stores) else None
        self._forget()
        self.reload(refresh=True)

    def _forget(self) -> None:
        self.rows, self.shown, self.chosen, self.day, self._areas, self._month = [], [], None, "", {}, ()
        self._ledger_groups, self._ledger_notes, self.groups, self.groups_read = {}, {}, [], False
        self.shots, self.shot_problem = {}, ""
        self.group_filter.set_groups([], ())
        self.shots_on_map.setVisible(bool(self.store and self.store.shots))
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

    def repository(self) -> ScheduleRepository:
        """The store's reads and writes (データ窓口)."""
        return ScheduleRepository(self.context.esri(), self.store, None)

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
        repo = self.repository()
        day = self.chosen_day().isoformat()
        areas = None if refresh or not self._areas else self._areas
        ledger_groups = dict(self._ledger_groups) if areas is not None else {}
        ledger_notes = dict(self._ledger_notes) if areas is not None else {}
        read_table = refresh or not self.groups_read
        reviews = self.context.config.image_reviews
        unusable = UnusableImages(self.context.esri(), reviews, None) if reviews else None
        self.status.setText(self.tr("{d} の計画を取得中…").format(d=day))

        def work(task):
            rows = repo.load_day(day, task.isCanceled)
            shots, problem = [], ""
            if repo.store.shots:
                try:
                    shots = repo.load_shots(day, task.isCanceled)
                except EsriError as error:
                    # The requests' rows matter most: shown without the shots.
                    problem = str(error)
            missing = repo.history_has(ADOPTION_ROLES) if refresh or areas is None else None
            found, groups_of, notes_of = areas, ledger_groups, ledger_notes
            if (found is None or any(r.request_id and not r.rings and r.request_id not in found for r in rows)
                    or any(r.request_id and not r.group and r.request_id not in groups_of for r in rows)
                    or any(r.request_id and r.request_id not in notes_of for r in rows)):
                # A table without shapes (or rows written before it had them): the ledger's areas. Rows written
                # without a group: the ledger's group (a request gone from the ledger: none, not asked again).
                # The requests' メモ (not kept in the history) with them: read again only for requests not read yet.
                requests = repo.load_requests(task.isCanceled)
                found = {q.id: q.rings for q in requests}
                groups_of = {q.id: q.group for q in requests}
                groups_of.update({r.request_id: "" for r in rows if r.request_id and r.request_id not in groups_of})
                notes_of = {q.id: q.note for q in requests}
                notes_of.update({r.request_id: "" for r in rows if r.request_id and r.request_id not in notes_of})
            groups = repo.read_groups(task.isCanceled) if read_table else None
            outages, _problem = self.context.read_outages(repo.client, task.isCanceled)
            return rows, missing, found, groups_of, notes_of, groups, shots, problem, outages, unusable_marks(unusable, rows, found or {})

        def done(result):
            rows, missing, found, groups_of, notes_of, groups, shots, problem, outages, self.review_marks = result
            self.outages = outages
            self.context.set_outages(outages)
            self.rows, self._areas, self._ledger_groups, self._ledger_notes, self.day = rows, found, groups_of, notes_of, day
            self.shots, self.shot_problem = {s.shot_id: s for s in shots if s.shot_id}, problem
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

        self._start(self.tr("撮像予定の取得"), work, done)

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
        repo = self.repository()

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

        run_task(self.tr("撮像予定の日の印"), lambda task: repo.day_counts(first.isoformat(), last.isoformat()),
                 done, failed)

    # --- The list -------------------------------------------------------------------------------

    def group_of(self, row: HistoryRow) -> str:
        """A row's 依頼グループ: the one it was written with, else the request's in the ledger."""
        return row.group or self._ledger_groups.get(row.request_id, "")

    def note_of(self, row: HistoryRow) -> str:
        """The メモ of a row's request (the ledger's; "" for a 要求外 row or a request gone from the ledger)."""
        return self._ledger_notes.get(row.request_id, "") if row.request_id else ""

    def _passes(self, row: HistoryRow) -> bool:
        system = self.system_filter.currentData()
        if system and row.system != system:
            return False
        if not self.group_filter.passes(self.group_of(row)):
            return False
        if not self.state_filter.passes(row_state(row)):
            return False
        words = self.search.text().lower().split()
        if words:
            text = " ".join((row.name, row.management_no, row.requester, self.group_of(row), row.satellite, row.request_id,
                             row.external_id, row.link_id, image_text(row))).lower()
            if not all(w in text for w in words):
                return False
        return True

    def _outages_changed(self) -> None:
        outages = self.context.known_outages()
        if outages != self.outages:
            self.outages = outages
            if self._loaded:
                self.render()

    def outage_of(self, r: HistoryRow) -> Optional[tuple]:
        """
        The outage hitting a row handed out and not taken yet (its shot's moment, direction, angle; not one the
        system turned down); None when none.
        """
        if not self.outages or r.result != "採用" or r.adoption == REJECTED or r.captured == "撮像済み" or not r.satellite:
            return None
        shot = self.shots.get(r.shot_id) if r.shot_id else None
        spec = next((s for s in self.satellites_known() if plain(r.satellite) in (plain(s.name), plain(s.id))), None)
        others = (spec.id, spec.series, *(spec.aliases or ())) if spec is not None else ()
        center = None
        if shot is not None and shot.rings and shot.rings[0]:
            ring = shot.rings[0]
            center = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
        sensor = shot.sensor if shot is not None else ((spec.sensor or "") if spec is not None else "")
        mode = shot.mode if shot is not None else ((spec.mode or "") if spec is not None else "")
        return handed_hit(self.outages, r.satellite, sensor, mode, others,
                          (shot.pass_time if shot is not None and shot.pass_time is not None else r.planned_at),
                          shot.orbit_dir if shot else "", shot.look_side if shot else "", shot.off_nadir if shot else None,
                          center, _now_ms(), r.system)

    def render(self) -> None:
        self.shown = [r for r in self.rows if self._passes(r)]
        hit_count, hit_kinds = 0, set()
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(h) for h in COLUMNS])
        chosen_row = -1
        # Requests taken several times a day (1 日の回数): each row tells which time it is.
        several = {(r.request_id, r.day) for r in self.rows if r.request_id and r.occurrence > 1}
        # Areas too big for one shot handed out in several shots (分担): each row tells which shot it is.
        shares: dict = {}
        for r in self.rows:
            if r.request_id and r.part > 0 and r.result == "採用":
                key = (r.request_id, r.day, r.occurrence)
                shares[key] = max(shares.get(key, 0), r.part)
        for i, r in enumerate(self.shown):
            state = row_state(r)
            name = r.name or r.external_id or r.request_id
            if (r.request_id, r.day) in several:
                name += self.tr("（{n} 回目）").format(n=r.occurrence)
            if shares.get((r.request_id, r.day, r.occurrence), 0) > 1:
                name += self.tr("（分担 {k}/{n}）").format(k=r.part, n=shares[(r.request_id, r.day, r.occurrence)])
            hit = self.outage_of(r)
            note = row_note(r)
            redo = self.redo_of(r)
            if redo:
                note = redo_text(redo) + (f"。{note}" if note else "")
            mark = self.review_marks.get(r.object_id) if r.captured == "撮像済み" else None
            if mark:
                note = mark + (f"。{note}" if note else "")
            if hit is not None:
                hit_count += 1
                hit_kinds.add(hit[0].for_system)
                head = self.tr("システムの障害の影響あり（{w}）") if hit[0].for_system else self.tr("衛星の障害の影響あり（{w}）")
                note = head.format(w=hit[1]) + (f"。{note}" if note else "")
            items = [QStandardItem(t) for t in (state, name, self.group_of(r), r.management_no,
                                                r.system, r.satellite, shot_text(r, self.rows, self.shots), _when(r.planned_at),
                                                captured_text(r), image_text(r), r.requester, note)] + [memo_item(self.note_of(r))]
            if hit is not None:
                tip = (self.tr("システムの障害（{s}）: {t}。出し直すかは担当が決める（予定は変えない）") if hit[0].for_system
                       else self.tr("衛星の障害（{s}）: {t}。出し直すかは担当が決める（予定は変えない）")).format(
                    s=hit[0].status(_now_ms()), t=hit[0].short_text(_now_ms()))
                for col in ((_SYSTEM, _NOTE) if hit[0].for_system else (_SATELLITE, _NOTE)):
                    items[col].setForeground(QBrush(QColor(OUTAGE_COLOR)))
                    items[col].setToolTip(plain_tip(tip))
            tip = shot_tip(self.shots.get(r.shot_id)) if r.shot_id else ""
            if tip:
                items[_SHOT].setToolTip(plain_tip(tip))
            if r.link_id:
                items[_NAME].setToolTip(plain_tip(self.tr("外部連携ID: {i}").format(i=r.link_id)))
            items[0].setForeground(QBrush(QColor(STATE_COLORS[state])))
            source = IMAGE_ARRIVED if r.captured == "撮像済み" and r.result == "採用" and not r.adoption_source else r.adoption_source
            if source:
                items[0].setToolTip(plain_tip(self.tr("採否の入力元: {s}").format(s=source)))
            elif state == AWAITING:
                items[0].setToolTip(self.tr("システムの採否が分からない（画像が入れば採用になる）"))
            if image_text(r):
                items[_TAKEN].setToolTip(plain_tip(self.tr("画像 {i}").format(i=r.image_id)))
            if mark:
                # Taken, but the image is rated unusable there: marked, left as it is (設計 2).
                items[_TAKEN].setText(self.tr("撮像済み（要確認）"))
                tip = self.tr("{m}。撮像済みのまま（出し直すか、「計画と実績の照合」で未撮像にするかは担当が決める）").format(m=mark)
                for col in (_TAKEN, _NOTE):
                    items[col].setForeground(QBrush(QColor(OUTAGE_COLOR)))
                    items[col].setToolTip(plain_tip(tip))
            if row_note(r) and hit is None and not mark:
                items[_NOTE].setToolTip(plain_tip(row_note(r)))  # reasons from the systems' files
            if redo and hit is None:
                items[_NOTE].setForeground(QBrush(QColor(REDO_COLOR)))
                items[_NOTE].setToolTip(plain_tip(note + (self.tr("。「撮像計画ファイル出力」でこの日を書き出し直すと新しい範囲で出る")
                                                          if redo == FOLLOW_REDO else "")))
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
                text += self.tr("。採否は記録できない（管理者に連絡: 出力履歴の {f}）［E704］").format(
                    f="・".join(self.missing_fields))
            if self.shot_problem:
                text += self.tr("。撮像の範囲を読めない（{m}）").format(m=self.shot_problem)
            if hit_count:
                what = (self.tr("衛星・システムの障害") if len(hit_kinds) > 1 else self.tr("システムの障害") if True in hit_kinds
                        else self.tr("衛星の障害"))
                text += self.tr("。{what}の影響あり {n} 件（予定は変えない。出し直すかは担当が判断）").format(what=what, n=hit_count)
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

    def redo_of(self, row: HistoryRow) -> str:
        """
        A row of today or later handed out with its request's area before the
        area's shape changed (地域の形に合わせる): 出し直し候補, or 前の形で採用済み.
        """
        return redo_mark(row, self._areas.get(row.request_id), jst_day(_now_ms())) if row.named_area_id else ""

    def requests_known(self, requests) -> None:
        """The requests as 「計算」 read them (their areas taken from their areas' shapes): the rows' marks with them."""
        if not self._areas:
            return
        changed = False
        for q in requests:
            if q.id in self._areas and self._areas[q.id] != q.rings:
                self._areas[q.id] = q.rings
                changed = True
        if changed and self._loaded:
            self.render()

    def draw(self) -> None:
        if not self.on_map.isChecked() or not self.isVisible():
            self.overlay.unpaint()
            self.canvas.refresh()
            return
        polygons = []
        # Those taken in the same shot as the chosen one stand out with it.
        fellows = {id(r) for r in companions(self.chosen, self.rows)} if self.chosen is not None else set()
        # The chosen one last, so it is on top.
        for r in sorted(self.shown, key=lambda r: self.chosen is not None and r.object_id == self.chosen.object_id):
            rings = self.rings_of(r)
            if rings:
                chosen = self.chosen is not None and r.object_id == self.chosen.object_id
                polygons.append((rings, STATE_COLORS[row_state(r)], 3 if chosen else 2 if id(r) in fellows else 1,
                                 (r.name or r.external_id) if chosen else ""))
        mine = self.chosen.shot_id if self.chosen is not None else ""
        for shot in self.shown_shots():
            polygons.append((shot.rings, SHOT_COLOR, 3 if shot.shot_id == mine else 1, "", True))
        self.overlay.paint(polygons)
        self.canvas.refresh()

    def shown_shots(self) -> list:
        """The shots of the rows shown (their outlines on the map), when 「撮像の範囲」 is ticked."""
        if not self.shots or not self.shots_on_map.isChecked() or not self.shots_on_map.isVisibleTo(self):
            return []
        ids = dict.fromkeys(r.shot_id for r in self.shown if r.shot_id)
        return [self.shots[i] for i in ids if i in self.shots and self.shots[i].rings]

    def zoom(self) -> None:
        box: Optional[QgsRectangle] = None
        rows = [self.chosen] if self.chosen else self.shown
        shot = self.shots.get(self.chosen.shot_id) if self.chosen is not None and self.chosen.shot_id else None
        rings = [ring for r in rows for ring in self.rings_of(r)] + (shot.rings if shot else [])
        for ring in rings:
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

    def roles_changed(self) -> None:
        """The answers (採否 by hand, the result files, 要求外): for those who plan (core/roles.py); the rows for everyone."""
        answering = self.allows("schedule.answer")
        for w in (self.answer_label, self.adopt_button, self.reject_button, self.wait_button, self.reason,
                  self.import_button, self.add_button, self.delete_button):
            w.setVisible(answering)
        if not answering and self.unrequested_dialog is not None:
            try:
                self.unrequested_dialog.close()
            except RuntimeError:
                pass

    # --- Answers by hand ------------------------------------------------------------------------

    def answer(self, answer: str, rows: Optional[list] = None) -> None:
        """Records the systems' answer to the rows chosen (handed out): 採用 / 不採用, or 不明 again."""
        if self.denied("schedule.answer"):
            return
        rows = self.selected() if rows is None else rows
        changes = manual_changes(rows, answer, self.reason.text().strip())
        if not changes:
            return
        repo = self.repository()

        def work(task):
            return repo.update_history(changes)

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
                self.status.setText(self.tr("{s} は結果ファイルが未設定（管理者に連絡: resultFile）［E703］").format(s=system.label))
                return None
            return system
        if not systems:
            self.status.setText(self.tr("結果ファイルを設定したシステムがない。採否は手入力（管理者に連絡: resultFile）［E703］"))
            return None
        if len(systems) == 1:
            return systems[0]
        names = [s.label for s in systems]
        name, ok = QInputDialog.getItem(self, self.tr("結果ファイルの取り込み"), self.tr("どのシステムのファイルか"), names, 0, False)
        return systems[names.index(name)] if ok else None

    def import_file(self, path: Optional[str] = None, system=None, ask: bool = True) -> None:
        """
        Reads a system's result file, tells what it says, and records it on the
        days its rows are for (対象日, else the 撮像予定's day; the file's day for
        rows saying neither), whatever day is shown (tsudo, 2026-11-20); then
        shows the file's day when the day shown is not one of them.
        """
        if self.denied("schedule.answer"):
            return
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
            self.status.setText(self.tr("読み込みに失敗: {m}").format(m=errors.describe(error)[0]))
            return
        spec = system.result_file
        repo = self.repository()
        day, name = self.day, os.path.basename(path)

        def work(task):
            answers, problems = read_answers(data, spec, load_schema)
            # The system's rows of the days the file's rows say and round them (a 外部連携ID's earlier row), and round
            # the day shown for a file without dates; not only the day shown.
            first, last = read_window(answers, day, spec.link_days, system.system, system.label)
            rows = repo.load_system_rows(system.system, first, last, task.isCanceled)
            requests = repo.load_requests(task.isCanceled)
            reading = match_answers(answers, rows, requests, system.system, day, spec, name, system.label)
            reading.problems = problems + reading.problems
            return reading

        def done(reading: Reading):
            if ask and not self._confirm_reading(reading, system.label):
                self.status.setText(self.tr("取り込みを取りやめ"))
                return
            self._save_reading(reading)

        self._start(self.tr("結果ファイルの読み込み"), work, done)

    def _tally_line(self, t: dict) -> str:
        return (self.tr("採用 {a}・不採用 {r}・要求外 {u}").format(a=t[ADOPTED], r=t[REJECTED], u=t[UNREQUESTED])
                + (self.tr("・不明 {w}").format(w=t[WAITING]) if t[WAITING] else ""))

    def _confirm_reading(self, reading: Reading, label: str) -> bool:
        t = reading.tally()
        days = reading.placed() or [reading.day]
        if len(reading.touched()) > 1:
            # Written on several days: each day's part (and what the file leaves out on it).
            lines = [self.tr("{s}・{f}").format(s=label, f=reading.source)]
            for d in reading.touched():
                n = reading.tally(d)
                lines.append(f"{d}: {self._tally_line(n)}" + (self.tr("・ファイルにない依頼 {m}").format(m=n[NOT_IN_FILE])
                                                               if n[NOT_IN_FILE] else ""))
        else:
            lines = [self.tr("{s}・{d}・{f}").format(s=label, d=days[0], f=reading.source), self._tally_line(t)]
        if reading.shown and reading.shown not in reading.touched():
            lines.append(self.tr("表示中の {s} ではなく、ファイルの行の日（対象日、なければ撮像予定の日）に記録").format(s=reading.shown))
        if t[NOT_IN_FILE]:
            lines.append(self.tr("出力したがファイルにない依頼 {n} 件: {what}").format(
                n=t[NOT_IN_FILE], what=REJECTED if reading.missing_as == REJECTED else self.tr("不明のまま")))
        if reading.replaced:
            lines.append(self.tr("前に結果ファイルから入れたこのシステムの要求外 {n} 件（{d} の分と、ほかの日の同じ計画）は置き換える").format(
                n=len(reading.replaced), d=days_text(reading.days or days)))
        if reading.problems:
            lines += ["", self.tr("読めなかった行など:")] + reading.problems[:8] + (["…"] if len(reading.problems) > 8 else [])
        return ask_no(self, self.tr("結果ファイルの取り込み"), "\n".join(lines), yes=self.tr("取り込み"))

    def _save_reading(self, reading: Reading) -> None:
        repo = self.repository()
        changes = answer_changes(reading)
        adds = unrequested_rows(reading, _now_ms())
        gone_ids = [r.object_id for r in reading.replaced]

        def work(task):
            gone = repo.delete_history_rows(gone_ids)
            changed = repo.update_history(changes)
            added = repo.add_history(adds)
            return changed, added, gone.problems + changed.problems + added.problems

        def done(result):
            changed, added, problems = result
            t = reading.tally()
            days = reading.placed() or [reading.day]
            source = reading.source if days == [reading.shown] else self.tr("{f}（{d}）").format(f=reading.source, d=days_text(days))
            note = self.tr("{f}: 採用 {a}・不採用 {r}・要求外 {u} を記録").format(
                f=source, a=t[ADOPTED], r=t[REJECTED] + (t[NOT_IN_FILE] if reading.missing_as == REJECTED else 0), u=int(added))
            if t[WAITING]:
                note += self.tr("（不明 {w}）").format(w=t[WAITING])
            if problems or reading.problems:
                note += self.tr("（{m}）").format(m="、".join((problems + reading.problems)[:3]))

            def told():
                self.status.setText(f"{self.status.text()}。{note}")
            # The file of another day: that day is shown (the day of most of its rows), so what was recorded is seen.
            if reading.day and self.day not in reading.touched():
                self._show_day(reading.day, told)
            else:
                self.reload(then=told)

        self._start(self.tr("採否の記録"), work, done)

    def _show_day(self, day: str, then=None) -> None:
        """Shows a day on the calendar and reads it, then `then`."""
        try:
            d = date.fromisoformat(day[:10])
        except ValueError:
            self.reload(then=then)
            return
        self.calendar.blockSignals(True)
        self.calendar.setSelectedDate(QDate(d.year, d.month, d.day))
        self.calendar.blockSignals(False)
        self.reload(then=then)

    # --- 要求外 by hand ---------------------------------------------------------------------------

    def open_unrequested(self) -> None:
        if self.denied("schedule.answer"):
            return
        if self.unrequested_dialog is not None:
            self.unrequested_dialog.close()
        self.unrequested_dialog = UnrequestedDialog(self)
        self.unrequested_dialog.show()

    def add_unrequested(self, row: HistoryRow) -> None:
        if self.denied("schedule.answer"):
            return
        repo = self.repository()

        def done(saved):
            note = self.tr("要求外「{n}」を追加").format(n=row.name) if saved else "、".join(saved.problems[:3])
            self.reload(then=lambda: self.status.setText(f"{self.status.text()}。{note}"))

        self._start(self.tr("要求外の追加"), lambda task: repo.add_history([row]), done)

    def delete_unrequested(self, rows: Optional[list] = None, ask: bool = True) -> None:
        rows = [r for r in (self.selected() if rows is None else rows) if r.result == UNREQUESTED and r.object_id is not None]
        if not rows or self.denied("schedule.answer"):
            return
        if ask and not ask_no(self, self.tr("削除"), self.tr("要求外の計画 {n} 件を削除").format(n=len(rows)), yes=self.tr("削除")):
            return
        repo = self.repository()
        ids = [r.object_id for r in rows]

        def done(saved):
            note = self.tr("要求外 {n} 件を削除").format(n=int(saved))
            self.reload(then=lambda: self.status.setText(f"{self.status.text()}。{note}"))

        self._start(self.tr("要求外の削除"), lambda task: repo.delete_history_rows(ids), done)

    # --- A layer --------------------------------------------------------------------------------

    def to_layer(self) -> Optional[QgsVectorLayer]:
        """
        The rows shown as a scratch layer (WGS 84 polygons, each request's own
        area), coloured by state; with the shot layer, their shots as a second
        layer (`shot_layer`, dashed outlines) above it.
        """
        if not self.shown:
            return None
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", self.tr("撮像予定 {d}").format(d=self.day), "memory")
        names = ("状態", "名称", "依頼グループ", "管理番号", "システム", "衛星", "まとめ", "撮像予定", "撮像", "画像ID", "依頼者", "理由", "対象日", "依頼ID",
                 "外部ID", "入力元", "撮像ID", "まとめ先の依頼ID", "外部連携ID")
        provider = layer.dataProvider()
        provider.addAttributes([QgsField(n, QVariant.String) for n in names])
        layer.updateFields()
        features = []
        for r in self.shown:
            f = QgsFeature(layer.fields())
            f.setAttributes([row_state(r), r.name, self.group_of(r), r.management_no, r.system, r.satellite,
                             shot_text(r, self.rows, self.shots), _when(r.planned_at), captured_text(r), image_text(r), r.requester,
                             row_note(r), r.day, r.request_id, r.external_id, r.adoption_source, r.shot_id, r.merged_into, r.link_id])
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
        self.shot_layer = self._shots_layer()
        return layer

    def _shots_layer(self) -> Optional[QgsVectorLayer]:
        """The shots of the rows shown (撮像の範囲): one feature a row of a file, dashed outlines; None without any."""
        ids = dict.fromkeys(r.shot_id for r in self.shown if r.shot_id)
        shots = [self.shots[i] for i in ids if i in self.shots and self.shots[i].rings]
        if not shots:
            return None
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", self.tr("撮像の範囲 {d}").format(d=self.day), "memory")
        names = ("撮像ID", "システム", "衛星", "撮像時刻", "オフナディア角", "入射角", "軌道方向", "観測方向", "シーン数", "撮像回数",
                 "範囲の割合", "依頼件数", "依頼ID", "名称", "出力ファイル")
        provider = layer.dataProvider()
        provider.addAttributes([QgsField(n, QVariant.String) for n in names])
        layer.updateFields()
        features = []
        for s in shots:
            f = QgsFeature(layer.fields())

            def text(v):
                return "" if v is None else str(v)
            f.setAttributes([s.shot_id, s.system, s.satellite, _when(s.pass_time), text(s.off_nadir), text(s.incidence), s.orbit_dir,
                             s.look_side, text(s.scenes), str(s.shots), "" if s.coverage is None else f"{round(s.coverage * 100)}%",
                             str(s.count), ",".join(s.request_ids), "・".join(s.names), s.file])
            rings = [[tuple(p[:2]) for p in ring] for ring in s.rings]
            f.setGeometry(polygon_of(rings) if len(rings) == 1 else multipolygon_of(rings))
            features.append(f)
        provider.addFeatures(features)
        layer.updateExtents()
        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
            "color": "0,0,0,0", "outline_color": SHOT_COLOR, "outline_width": "0.6", "outline_style": "dash"})))
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
        fit_text(self)
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
        # Kept as the row's reason: the list shows it in 「理由」 (its own 「メモ」 column is the requests' note).
        self.note.setToolTip(self.tr("一覧の「理由」の列に出る"))
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
            # (Its button stays pressed while another tool takes the map: it may be given the map back.)
            map_tools.take(self.canvas, self.extent_tool, turns_off=False)
        else:
            map_tools.forget(self.canvas, self.extent_tool)
            if self.canvas.mapTool() is self.extent_tool:
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
            self.message.setText(self.tr("システムが未設定（管理者に連絡: requestExports）［E703］"))
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
