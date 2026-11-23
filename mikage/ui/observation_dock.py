"""
The 観測記録 dock (the viewer's observation panel): things seen on the
images recorded image by image, in order of acquisition, as points on the
map, carried over between images, and followed as each thing's flow.

The workings are core.obs.book.ObservationBook; this dock shows it, sends
the map's clicks to it, and runs what an Esri store answers in a task (the
local GeoPackage answers at once, on this thread). Keys work while the dock
has the focus: PageUp / PageDown step through the images, R records by
clicks, L by two points, A a checked area, S 「連続」 and G 「並べる」 (many
alike things as a 見本, ui/obs_many.py), 1–9 pick an activity, Alt+1–5 a
match candidate, Esc steps back.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional, Sequence

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QEventLoop, QStringListModel, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDateTimeEdit, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QSizePolicy, QTableWidget, QToolButton, QVBoxLayout, QWidget,
)

try:
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt 5
    from qgis.PyQt.QtWidgets import QShortcut

from ..core.obs.api import (
    FormValues, MatchCandidate, Observation, ObservationApi, ObservationBook, OutboxDrafts, RecordQuery, StoreSet, path_of, store_sets,
)
from ..qgis_io import settings
from ..qgis_io.context import PluginContext
from ..qgis_io.listed_layers import ListedLayers
from ..qgis_io.pixel_place import locator as pixel_locator
from ..qgis_io.tasks import error_text, run_task
from . import shortcuts
from .docks import PluginDock, ask_no, plain_label, plain_tip
from .flow_layout import FlowLayout
from .note_assist import NoteAssist
from .text_assist_ui import NoteFields, NoteTidy
from .list_views import ListViews, column_menus
from .obs_many import ManyPanel
from .obs_overlay import ObservationOverlay
from .obs_tool import ObservationTool
from .obs_dock.alerts import AlertsPart
from .obs_dock.images import ImagesPart
from .obs_dock.modes import ModesPart
from .obs_dock.map_input import MapInputPart
from .obs_dock.form import FormPart
from .obs_dock.records import RecordsPart
from .obs_dock.outputs import OutputsPart
from .obs_dock.table import TablePart
from .obs_dock.drafts import DraftsPart
from .obs_dock.view import ViewPart
from .obs_dock.overlay import OverlayPart
from .obs_dock.common import (  # noqa: F401 (REPORTED_ROW…: read from here too)
    ACTIVITY_COLORS, HINT, HINT_AREA, HINT_POINTS, HINT_UNSEEN, NO_IMAGE, POINT_CHOICES, REPORTED_ROW, WAITING_ROW, FlowStrip,
    _ms_of, _Saved,
)


class ObservationDock(
        AlertsPart, ImagesPart, ModesPart, MapInputPart, FormPart, RecordsPart, OutputsPart, TablePart, DraftsPart, ViewPart, OverlayPart,
        PluginDock):
    """Recording what is seen on the images, image by image."""

    #: A message of the book, from any thread (shown on this one).
    said = pyqtSignal(str)
    #: The records or the classification changed (for the classification editor).
    changed = pyqtSignal()
    #: A server went down or came back, or the work waiting changed (core/offline.py), from any thread.
    offlineChanged = pyqtSignal()

    def __init__(self, iface, context: PluginContext, open_settings: Callable[[], None], parent: Optional[QWidget] = None,
                 edit_taxonomy: Optional[Callable[[Optional[list]], None]] = None):
        super().__init__("SatObsObservations", "観測記録", context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.edit_taxonomy = edit_taxonomy
        self.sets: list[StoreSet] = []
        #: The settings file and its stores when they were last listed (a change picks the first store again).
        self._stores_read: Optional[tuple] = None
        self.books: dict[str, ObservationBook] = {}
        self.book: Optional[ObservationBook] = None
        self._alerts = None
        self._export = None
        self._handover = None
        #: (settings, when read, rules, geofences) of the last check of saved records.
        self._alert_conditions = None
        #: The ids of the records in the 通報 log table (their rows and marks highlighted).
        self.reported: set[str] = set()
        self._reported_asked = 0
        self._alert_widget = None
        self._alert_items: list = []
        self.store_set: Optional[StoreSet] = None
        #: 判読の練習 (ui/exercise_run.py): the one store while an exercise runs (its answers apart from the practice's
        #: records), None: the settings file's; and the store chosen before it, chosen again after.
        self.exercise_store: Optional[StoreSet] = None
        self._store_before = ""
        #: The thing whose flow is shown under the list (「流れ」 of a row), None for none.
        self.chosen: Optional[str] = None
        self.class_filter: Optional[list[str]] = None
        #: The form's place and what it is about: (lon, lat, the record changed, the thing carried over).
        self.pending: Optional[tuple] = None
        #: What the form said when opened (to tell whether anything was typed since).
        self._filled: Optional[FormValues] = None
        #: The kept record (保存できなかった記録) the form is open on again, '' for none.
        self._draft_id = ""
        #: Opens 「送信待ち」 (the status bar's list, ui/outbox_dialog.py); set by the plugin.
        self.open_outbox: Optional[Callable[[], Any]] = None
        #: The image catalog dock (set by the plugin): a record's image not open is opened through it
        #: (開いた画像の履歴, else the catalog asked by the image id), open_image.
        self.catalog: Optional[Callable[[], Any]] = None
        #: 「自分の記録」 (ui/my_records_dialog.py), made when first opened.
        self._my_records = None
        self.offered: list[MatchCandidate] = []
        self.active = False
        self.points = settings.point_count_setting()
        self.area_mode = False
        #: The area being made is a 不可範囲 (not a 確認範囲).
        self.area_unseen = False
        self.rear: Optional[tuple] = None
        #: The points clicked after the rear: the front, then the sides.
        self.placed: list[tuple] = []
        self.cursor: Optional[tuple] = None
        self.area_corner: Optional[tuple] = None
        self.area_ring: Optional[list] = None
        self.dragging: Optional[tuple] = None
        self._shown: Optional[dict[str, bool]] = None
        self._queue: list[tuple] = []
        self._running = False
        self._following = False
        self._previous_tool = None
        self._observer_cache = ""
        #: The books whose records are being read, and what waits for them (by id(book)). The records are read only
        #: once needed (tsudo 2026-11-10): an image chosen here, or something that uses them all (通報, エクスポート…).
        self._reading: set[int] = set()
        #: Books reading the records around their image (one read at a time each), and things whose whole flow is read.
        self._reading_around: set[int] = set()
        self._whole_flows: set[tuple[int, str]] = set()
        self._after_read: dict[int, list] = {}
        #: Whether the dock shows only its note (no image chosen, or no store): None before the first render.
        self._idle: Optional[bool] = None
        #: The settings the satellite names were asked for (_read_satellite_choices).
        self._satellites_asked = None
        self.said.connect(self._show_status)
        #: The images without their files as layers of their footprints (removing one takes its image off the list).
        self.footprints = ListedLayers(self, self._footprints_removed)

        self._build()
        self.overlay = ObservationOverlay(self.canvas, self.mark_style_select.currentData())
        self.overlay.hide()
        self.tool = ObservationTool(self.canvas, self.overlay, self.map_clicked, self.map_hovered, self.map_dragged,
                                    lambda: self.active and not self.area_mode and not self.rear, self.escape,
                                    self.map_entered, double_clicked=self.many.double_clicked, turned=self.many.turned,
                                    stamping=lambda: self.many.stamping, undo=self.many.undo, stepped=self.many.step)
        self.tool.deactivated.connect(self._tool_gone)
        self.canvas.extentsChanged.connect(self._extent_changed)
        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        self.iface.currentLayerChanged.connect(self._layer_chosen)
        context.configChanged.connect(self.reset_stores)
        context.configChanged.connect(self.assignment_bar.reset)
        self.visibilityChanged.connect(self._visibility)
        # Others' records read quietly (another image, after a conflict, and every `refreshMinutes`).
        self._refreshing = False
        #: A forced read asked while one was reading: read once more after it.
        self._refresh_again = False
        self._refreshed_at = 0.0
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(lambda: self.refresh(force=True))
        self.reset_stores()
        self.assignment_bar.reset()

    # ---- building ------------------------------------------------------

    def _button(self, text: str, tip: str = "", slot: Optional[Callable] = None, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setAutoDefault(False)
        if slot is not None:
            (b.toggled if checkable else b.clicked).connect(slot)
        return b

    def _completer(self, edit: QLineEdit) -> QStringListModel:
        model = QStringListModel(self)
        completer = QCompleter(model, edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        edit.setCompleter(completer)
        return model

    def _build(self) -> None:
        b = self.body
        tr = self.tr
        top = QHBoxLayout()
        self.store_select = QComboBox()
        self.store_select.setToolTip(tr("記録の保存先"))
        self.store_select.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.store_select.currentIndexChanged.connect(self._store_chosen)
        top.addWidget(self.store_select, 1)
        # 「自分の記録」 (tsudo 2026-11-18: 過去の自分がどの画像のどの観測結果を作ったのかがわかりにくい).
        self.my_records_button = self._button(tr("自分の記録…"), tr("自分の記録を、画像をまたいで一覧で表示。ダブルクリックで画像を開いて記録を編集"),
                                              lambda: self.open_my_records())
        top.addStretch(0)
        top.addWidget(self.my_records_button)
        b.addLayout(top)

        # The image.
        row = QHBoxLayout()
        self.prev_button = self._button("◀", tr("前の画像（撮像日時の順、PageUp）"), lambda: self.step(-1))
        self.image_select = QComboBox()
        self.image_select.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.image_select.setToolTip(tr("画像"))
        self.image_select.activated.connect(self._image_chosen)
        self.next_button = self._button("▶", tr("次の画像（撮像日時の順、PageDown）"), lambda: self.step(1))
        self.list_button = self._button("＋", tr("ファイルのない画像を一覧に追加（画像 ID と撮像日時で指定）"),
                                        self._toggle_list_form, checkable=True)
        self.image_event_button = self._button("⚑", tr("この画像を対応中のイベント（地震対応など）に追加"),
                                                lambda: self.add_image_to_event())
        self.image_event_button.hide()
        self.only = QCheckBox(tr("この画像のみ"))
        self.only.setToolTip(tr("この画像だけを地図に表示"))
        self.only.toggled.connect(self._apply_only)
        for w in (self.prev_button, self.image_select, self.next_button, self.list_button, self.image_event_button, self.only):
            row.addWidget(w)
        b.addLayout(row)
        # 分担: who holds the image (the settings file's assignments).
        from .assignment_bar import AssignmentBar

        self.assignment_bar = AssignmentBar(self)
        b.addWidget(self.assignment_bar)

        # An image without its file.
        self.list_form = QGroupBox(tr("ファイルなしの画像"))
        f = QFormLayout(self.list_form)
        self.list_id = QLineEdit()
        self.list_id.setPlaceholderText(tr("例: 外部システムの画像番号"))
        self.recorded_model = self._completer(self.list_id)
        self.list_name = QLineEdit()
        self.list_name.setPlaceholderText(tr("空欄で画像 ID"))
        self.list_time = QDateTimeEdit()
        self.list_time.setCalendarPopup(True)
        self.list_time.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.list_time_known = QCheckBox(tr("撮像日時あり"))
        self.list_box = QCheckBox(tr("範囲: 地図の表示範囲"))
        self.list_box.setToolTip(tr("継続候補（白抜きの印）を地図の表示範囲内に限定"))
        f.addRow(tr("画像 ID"), self.list_id)
        f.addRow(tr("名称"), self.list_name)
        times = QHBoxLayout()
        times.addWidget(self.list_time_known)
        times.addWidget(self.list_time, 1)
        f.addRow(tr("撮像日時"), times)
        f.addRow(self.list_box)
        note = QLabel(tr("ファイルなしで、背景地図やほかの画像の上に記録。地図の枠のレイヤーを削除すると、一覧から除外（記録は残る）"))
        note.setWordWrap(True)
        f.addRow(note)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("追加"), "", self._list_from_form))
        self.unlist_button = self._button(tr("一覧から除外"), tr("この画像を一覧から除外（記録は残る）"),
                                          self._unlist)
        actions.addWidget(self.unlist_button)
        actions.addWidget(self._button(tr("取消"), "", lambda: self.list_button.setChecked(False)))
        f.addRow(actions)
        self.list_id.returnPressed.connect(self._list_from_form)
        self.list_form.hide()
        b.addWidget(self.list_form)

        # The time of an image that does not say.
        self.time_row = QWidget()
        t = QHBoxLayout(self.time_row)
        t.setContentsMargins(0, 0, 0, 0)
        t.addWidget(QLabel(tr("撮像日時なし")))
        self.time_input = QDateTimeEdit()
        self.time_input.setCalendarPopup(True)
        self.time_input.setDisplayFormat("yyyy-MM-dd HH:mm")
        t.addWidget(self.time_input, 1)
        t.addWidget(self._button(tr("入力"), "", self._set_image_time))
        self.time_row.hide()
        b.addWidget(self.time_row)

        # 衛星名 of an image that does not say (tsudo 2026-11-20: images outside the catalog).
        self.satellite_row = QWidget()
        t = QHBoxLayout(self.satellite_row)
        t.setContentsMargins(0, 0, 0, 0)
        t.addWidget(QLabel(tr("衛星名なし")))
        self.satellite_input = QComboBox()
        self.satellite_input.setEditable(True)
        self.satellite_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.satellite_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.satellite_input.setToolTip(tr("この画像を撮った衛星（センサー）を選択か入力。"
                                           "衛星名が空の、この画像の記録と確認範囲にも入力"))
        self.satellite_input.lineEdit().setPlaceholderText(tr("例: WV03"))
        self.satellite_input.lineEdit().returnPressed.connect(self._set_image_satellite)
        t.addWidget(self.satellite_input, 1)
        t.addWidget(self._button(tr("入力"), "", self._set_image_satellite))
        self.satellite_row.hide()
        b.addWidget(self.satellite_row)

        # No image chosen (tsudo 2026-11-10): only this note. What follows (recording, the lists, the forms) waits
        # for an image, and the records are not read until then.
        self.idle_note = plain_label(QLabel())
        self.idle_note.setWordWrap(True)
        self.idle_note.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.idle_note.setStyleSheet("QLabel { color: #57534e; padding: 6px 2px; }")
        self.idle_note.hide()
        b.addWidget(self.idle_note, 1)
        self.work_area = QWidget()
        b.addWidget(self.work_area, 1)
        b = QVBoxLayout(self.work_area)
        b.setContentsMargins(0, 0, 0, 0)

        # Recording.
        row = QHBoxLayout()
        self.record_button = self._button(tr("クリック記録"), tr("クリック地点にこの画像の観測を記録（R）"), self.set_active, checkable=True)
        self.points_select = QComboBox()
        for n, label in POINT_CHOICES:
            self.points_select.addItem(tr(label), n)
        self.points_select.setToolTip(tr("記録のクリック数（L で切り替え）\n"
                                         "1 点: 位置\n"
                                         "2 点: 後端 → 前端（船首・機首）で中心・長さ・向き\n"
                                         "3 点: さらに片側の側面（舷側）で幅。左右は同じ幅\n"
                                         "4 点: さらに両側の側面で幅"))
        self.points_select.setCurrentIndex(max(0, self.points - 1))
        self.points_select.currentIndexChanged.connect(lambda _i: self.set_points(self.points_select.currentData()))
        self.area_button = self._button(tr("確認範囲"), tr("確認済みの範囲を記録（A）。範囲内で記録のないモノは消失"),
                                        self.set_area_mode, checkable=True)
        # 不可範囲 (tsudo 2026-11-19): a part of the image under cloud, haze…; the things there are 「不可」, not 消失.
        self.unseen_area_button = self._button(tr("不可範囲"), tr("雲などで判読できない範囲を記録（K）\n中の前後の画像のモノは「不可」として記録。消失にはしない"),
                                               lambda on: self.set_area_mode(on, unseen=True), checkable=True)
        # Many alike things (ui/obs_many.py): put down as the 見本.
        self.stamp_button = self._button(tr("連続"), tr("見本と同じものを続けて記録（S）\n"
                                                        "ダブルクリック: 見本と同じ向きで記録\n"
                                                        "押してドラッグ: ドラッグした方へ向けて記録\n"
                                                        "Backspace: 1 つ取り消し"),
                                         lambda on: self.set_many_mode("stamp" if on else ""), checkable=True)
        self.row_button = self._button(tr("並べる"), tr("見本と同じものを列・格子に並べて記録（G）\n"
                                                      "列の両端（格子はさらに最後の列の始め）をクリックし、数を合わせて Enter"),
                                       lambda on: self.set_many_mode("row" if on else ""), checkable=True)
        for w in (self.record_button, self.points_select, self.area_button, self.unseen_area_button):
            row.addWidget(w)
        row.addStretch(1)
        # 「印」: how the records are drawn on the map (filled marks hid what they mark).
        self.mark_style_select = QComboBox()
        for text, style in ((tr("通常"), "normal"), (tr("枠だけ"), "outline"), (tr("非表示"), "hidden")):
            self.mark_style_select.addItem(text, style)
        self.mark_style_select.setCurrentIndex(max(0, self.mark_style_select.findData(settings.mark_style())))
        self.mark_style_select.setToolTip(tr("地図の記録の印を表示・非表示（H、どこからでも Ctrl+Alt+D）\n"
                                             "通常: 塗りつぶした印\n"
                                             "枠だけ: 輪と枠線だけ（画像が透けて見える）\n"
                                             "非表示: 入力中の点とフォームの記録だけを表示"))
        self.mark_style_select.currentIndexChanged.connect(lambda _i: self.set_mark_style(self.mark_style_select.currentData()))
        #: The style 「H」 comes back to from 「非表示」.
        self._shown_style = settings.mark_style() if settings.mark_style() != "hidden" else "normal"
        row.addWidget(QLabel(tr("印")))
        row.addWidget(self.mark_style_select)
        b.addLayout(row)
        row = QHBoxLayout()
        self.carry_label = QLabel()
        self.carry_all_button = self._button(tr("すべて継続"), tr("前後の画像のモノ（白抜きの印）を、この画像でも継続として一括記録。確認範囲内と不可範囲内のものは除く"),
                                             self.carry_all)
        self.absent_all_button = self._button(tr("すべて消失"), tr("前後の画像のモノ（白抜きの印）を、この画像での消失として一括記録。不可範囲内のものは除く"),
                                              self.absent_all)
        self.absent_inside_button = self._button("", tr("確認範囲内で記録のない前後の画像のモノを、この画像での消失として一括記録"), self.absent_inside)
        self.unseen_inside_button = self._button("", tr("不可範囲内で記録のない前後の画像のモノを、この画像で「不可」として一括記録"),
                                                 self.mark_unseen)
        row.addWidget(self.carry_label, 1)
        row.addWidget(self.carry_all_button)
        row.addWidget(self.absent_all_button)
        row.addWidget(self.absent_inside_button)
        row.addWidget(self.unseen_inside_button)
        b.addLayout(row)
        # 「見本で記録」: its own row (the recording row is full), the panel shown while 連続 or 並べる is on.
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("同じものが並ぶとき")))
        row.addWidget(self.stamp_button)
        row.addWidget(self.row_button)
        row.addStretch(1)
        b.addLayout(row)
        self.many = ManyPanel(self)
        b.addWidget(self.many)

        # A checked area.
        self.area_form = QGroupBox(tr("確認範囲"))
        f = QFormLayout(self.area_form)
        self.area_range = QLabel()
        f.addRow(self.area_range)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("表示範囲"), tr("地図の表示範囲を範囲に"), self._area_view))
        self.area_image_button = self._button(tr("画像の範囲"), tr("画像の範囲全体を範囲に（画像全体が雲のときなど）"), self._area_image)
        actions.addWidget(self.area_image_button)
        f.addRow(actions)
        self.area_kind = QLineEdit()
        self.area_kind.setPlaceholderText(tr("空欄ですべての種別"))
        self.area_kinds_model = self._completer(self.area_kind)
        self.area_note = QLineEdit()
        self.area_kind_label = QLabel(tr("対象の種別"))
        f.addRow(self.area_kind_label, self.area_kind)
        # 不可範囲: why (雲・霞…, `observationVocabulary.unseenReasons`; another may be typed).
        self.area_reason = QComboBox()
        self.area_reason.setEditable(True)
        self.area_reason.setToolTip(tr("不可の理由（一覧にないものは入力）"))
        self.area_reason_label = QLabel(tr("理由"))
        f.addRow(self.area_reason_label, self.area_reason)
        # 文章の手伝い (`textAssist`): 「整える」 beside the field, the proposal under it (tsudo 2026-11-19).
        self.area_note_tidy = NoteTidy(self.area_note, self.context.text_assist, self.context.glossary, self.area_kind.text,
                                       lambda: (path_of(self.area_kind.text()) or [""])[0], self)
        f.addRow(tr("備考"), self.area_note_tidy.row())
        # 備考の単語集 (`glossary`): suggestions while typing, and the other spellings found (tsudo 2026-11-17).
        self.area_note_assist = NoteAssist(self.area_note, self.context.glossary, lambda: (path_of(self.area_kind.text()) or [""])[0], self)
        f.addRow(self.area_note_assist.notice)
        f.addRow(self.area_note_tidy.panel)
        actions = QHBoxLayout()
        self.area_submit_button = self._button(tr("確認範囲を記録（Enter）"), "", self._submit_area)
        actions.addWidget(self.area_submit_button)
        actions.addWidget(self._button(tr("取消（Esc）"), "", lambda: self.set_area_mode(False)))
        f.addRow(actions)
        self.area_kind.returnPressed.connect(self._submit_area)
        self.area_note.returnPressed.connect(self._submit_area)
        self.area_reason.lineEdit().returnPressed.connect(self._submit_area)
        self.area_form.hide()
        b.addWidget(self.area_form)

        # The record's form.
        self.form = QGroupBox()
        f = QFormLayout(self.form)
        self.object_label = QLabel(tr("モノ"))
        self.object_input = QLineEdit()
        self.object_input.setPlaceholderText(tr("空欄で新規"))
        self.objects_model = self._completer(self.object_input)
        self.object_input.textEdited.connect(lambda _t: self._object_typed())
        self.object_input.editingFinished.connect(self._object_typed)
        f.addRow(self.object_label, self.object_input)
        self.candidates_box = QWidget()
        self.candidates_layout = FlowLayout(self.candidates_box)
        f.addRow(self.candidates_box)
        self.kind_input = QLineEdit()
        self.kind_input.setPlaceholderText("船舶 > 支援船 > 〇〇級")
        self.kinds_model = self._completer(self.kind_input)
        self.kind_input.textEdited.connect(lambda _t: self._kind_changed())
        f.addRow(tr("種別"), self.kind_input)
        # One row per level (大分類, 種別, 級…), each with its choices and the chosen one pressed.
        self.kind_path_box = QWidget()
        self.kind_path_layout = QVBoxLayout(self.kind_path_box)
        self.kind_path_layout.setContentsMargins(0, 0, 0, 0)
        self.kind_levels: Optional[QWidget] = None
        f.addRow(self.kind_path_box)
        self.specs_label = plain_label(QLabel())
        self.specs_label.setWordWrap(True)
        f.addRow(self.specs_label)
        self.chips_box = QWidget()
        self.chips_layout = FlowLayout(self.chips_box)
        f.addRow(self.chips_box)
        self.activity_input = QLineEdit()
        self.activities_model = self._completer(self.activity_input)
        self.activity_input.textEdited.connect(lambda _t: self._render_chips())
        f.addRow(tr("活動"), self.activity_input)
        self.confidence_select = QComboBox()
        self.confidence_select.currentIndexChanged.connect(lambda _i: self._render_confidence())
        f.addRow(tr("確度"), self.confidence_select)
        self.length_input = QLineEdit()
        self.length_input.textEdited.connect(lambda _t: (self._render_measure(), self._render_candidates()))
        self.heading_input = QLineEdit()
        self.heading_input.setToolTip(tr("北から時計回り、後端→前端"))
        self.heading_input.textEdited.connect(lambda _t: self._render_candidates())
        self.width_input = QLineEdit()
        self.width_input.setToolTip(tr("側面から側面まで（向きに直角）"))
        self.width_input.textEdited.connect(lambda _t: self._render_measure())
        self.length_label = QLabel(tr("長さ（m）"))
        self.heading_label = QLabel(tr("向き（°）"))
        self.width_label = QLabel(tr("幅（m）"))
        f.addRow(self.length_label, self.length_input)
        f.addRow(self.width_label, self.width_input)
        f.addRow(self.heading_label, self.heading_input)
        self.measure_note = plain_label(QLabel())
        f.addRow(self.measure_note)
        self.note_input = QLineEdit()
        self.note_tidy = NoteTidy(self.note_input, self.context.text_assist, self.context.glossary, self.kind_input.text,
                                  lambda: (path_of(self.kind_input.text()) or [""])[0], self)
        # 項目の提案 (`textAssist.tasks.noteFields`): the fields read from the 備考, for those still empty (tsudo 2026-11-19).
        self.note_fields = NoteFields(self.note_input, self.context.text_assist, self, self)
        f.addRow(tr("備考"), self.note_tidy.row(self.note_fields.button))
        self.note_assist = NoteAssist(self.note_input, self.context.glossary, lambda: (path_of(self.kind_input.text()) or [""])[0], self)
        f.addRow(self.note_assist.notice)
        f.addRow(self.note_tidy.panel)
        f.addRow(self.note_fields.panel)
        actions = QHBoxLayout()
        self.submit_button = self._button(tr("記録（Enter）"), "", lambda: self.submit(True))
        self.absent_button = self._button(tr("消失"), tr("この画像での消失（見当たらず）を記録"), lambda: self.submit(False))
        # 「不可」 (tsudo 2026-11-19): could not be seen in this image (a cloud, a shadow…); ▼ picks why.
        self.unseen_button = QToolButton()
        self.unseen_button.setText(tr("不可"))
        self.unseen_button.setToolTip(tr("雲や影で、この画像では判読できないこと（不可）を記録。消失にはしない\n▼ で理由を選択"))
        self.unseen_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.unseen_menu = QMenu(self.unseen_button)
        self.unseen_button.setMenu(self.unseen_menu)
        self.unseen_button.clicked.connect(lambda _on=False: self.submit(False, unseen=self._unseen_reason()))
        self.delete_button = self._button(tr("削除"), tr("この記録を削除（「詳細 ▸ 削除済みも表示」で復元できる）"), self._delete_pending)
        self.alert_button = self._button(tr("通報"), tr("この記録を通報文に追加（通報の条件に合わなくても）"),
                                         lambda: self.add_to_alerts([self.pending[2]]) if self.pending and self.pending[2] else None)
        for w in (self.submit_button, self.absent_button, self.unseen_button, self.delete_button, self.alert_button,
                  self._button(tr("取消（Esc）"), "", self.close_form)):
            actions.addWidget(w)
        f.addRow(actions)
        for edit in (self.object_input, self.kind_input, self.activity_input, self.length_input, self.width_input, self.heading_input,
                     self.note_input):
            edit.returnPressed.connect(lambda: self.submit(True))
        # Below the list (added after it): the record chosen there opens under it.
        self.form.hide()

        # 一時的なオフライン作業 (core/offline.py): said while this store's server cannot be reached.
        self.offline_note = QLabel()
        self.offline_note.setWordWrap(True)
        self.offline_note.setStyleSheet("QLabel { background: #dbeafe; color: #1e3a8a; padding: 3px; }")
        self.offline_note.hide()
        b.addWidget(self.offline_note)
        offline = getattr(self.context, "offline", None)
        if offline is not None:
            offline.listeners.append(self._offline_changed)
            self.offlineChanged.connect(self._show_offline)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        b.addWidget(self.status)
        # 送信待ち of this store (kept on this computer, core/offline.py): the records not saved and the work kept
        # offline, in the one list of the status bar: sent, opened in the form, or thrown away.
        self.drafts_button = QPushButton()
        self.drafts_button.setStyleSheet("QPushButton { color: #b45309; font-weight: bold; }")
        self.drafts_button.setToolTip(tr("まだ送っていない記録を一覧で表示（送信・修正・破棄）"))
        self.drafts_button.clicked.connect(lambda _on=False: self.open_drafts())
        self.drafts_button.hide()
        b.addWidget(self.drafts_button)

        # The list: this image's records only (tsudo 2026-11-18: the 「モノ」 and 「分類」 tabs went; a thing's
        # flow opens below from its row's 「流れ」, and the classes are edited in 「分類体系の編集」).
        self.class_filter_row = QWidget()
        row = QHBoxLayout(self.class_filter_row)
        row.setContentsMargins(0, 0, 0, 0)
        #: How many records the list shows (「この画像 4 件」, of how many when some are left out).
        self.count_label = plain_label(QLabel())
        self.count_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.count_label)
        # 大分類 (a root of the classification) the list shows.
        self.root_filter_label = QLabel(tr("大分類"))
        row.addWidget(self.root_filter_label)
        self.root_filter = QComboBox()
        self.root_filter.setToolTip(tr("一覧に表示する大分類"))
        self.root_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.root_filter.activated.connect(lambda _i: self.filter_root(self.root_filter.currentData() or None))
        row.addWidget(self.root_filter)
        self.class_filter_label = plain_label(QLabel())
        row.addWidget(self.class_filter_label, 1)
        unfilter = QToolButton()
        unfilter.setText("×")
        unfilter.setToolTip(tr("分類の絞り込みを解除"))
        unfilter.clicked.connect(lambda: self.filter_root(None))
        row.addWidget(unfilter)
        self.unfilter_button = unfilter
        # 「この画像」: only one's own records (記録者 is oneself: the 「記録者」 name or the user name).
        self.mine_only = QCheckBox(tr("自分の記録のみ"))
        self.mine_only.setToolTip(tr("自分の記録だけを表示"))
        self.mine_only.toggled.connect(lambda _on: self.render())
        row.addWidget(self.mine_only)
        b.addWidget(self.class_filter_row)
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Ctrl / Shift + click chooses several records, for 「一括編集」.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.cellDoubleClicked.connect(self._row_double_clicked)
        b.addWidget(self.table, 1)
        self._row_actions: list[tuple[Callable, Optional[Callable]]] = []
        #: The record each row stands for (none for a thing carried over or an area), for 「一括編集」.
        self._row_records: list[list[Observation]] = []
        #: What each row stands for (`o:` a record, `c:` a thing carried over, `a:` a checked area),
        #: so the rows chosen stay chosen when the list is shown again (a read in a task, a refresh).
        self._row_keys: list[str] = []
        #: The list shown last (the image), whose chosen rows are chosen again.
        self._shown_as: Optional[tuple] = None
        self.bulk_button = self._button(tr("一括編集…"), tr("選んだ記録（Ctrl・Shift + クリックで複数）の種別・活動・確度をまとめて変更"),
                                        lambda: self.bulk_edit())
        self.bulk_button.hide()
        self.remove_chosen_button = self._button(tr("選んだ記録を削除…"), tr("選んだ記録（Ctrl・Shift + クリックで複数）をまとめて削除。Delete キーでも削除"),
                                                 lambda: self.remove_selected())
        self.remove_chosen_button.hide()
        delete_key = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.table)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(lambda: self.remove_selected())
        self.alert_add_button = self._button(tr("通報に追加"), tr("選んだ記録を通報文に追加（通報の条件に合わなくても）"),
                                             lambda: self.add_to_alerts())
        self.alert_add_button.hide()
        self.event_add_button = self._button(tr("イベントに追加"), tr("選んだ記録を対応中のイベント（地震対応など）に追加"),
                                             lambda: self.add_to_event())
        self.event_add_button.hide()
        # まとめ (`textAssist.tasks.summary`): a few sentences on the records chosen (tsudo 2026-11-19).
        self.summary_button = self._button(tr("まとめ…"), tr("選んだ記録を数文にまとめて下書きを作成（この PC の生成 AI）"),
                                           lambda: self.summarize())
        self.summary_button.hide()
        self.table.itemSelectionChanged.connect(self._selection_changed)
        chosen = QHBoxLayout()
        chosen.addWidget(self.bulk_button)
        chosen.addWidget(self.remove_chosen_button)
        chosen.addWidget(self.alert_add_button)
        chosen.addWidget(self.event_add_button)
        chosen.addWidget(self.summary_button)
        chosen.addStretch(1)
        # 「ビュー」: the list's columns and the 大分類 shown, kept under a name (per user). The key is the one
        # 「この画像」 had while there were tabs, so the views saved then still apply.
        self.views = ListViews("observations-0", self.table, {
            "classes": (lambda: list(self.class_filter) if self.class_filter else None, self._set_class_filter),
        }, None, self)
        chosen.addWidget(self.views.button)
        b.addLayout(chosen)
        b.addWidget(self.form)

        # The flow of the thing whose 「流れ」 was pressed (pressed again, or ×, it closes).
        self.flow_box = QGroupBox()
        fl = QVBoxLayout(self.flow_box)
        head = QHBoxLayout()
        self.flow_summary = plain_label(QLabel())
        self.flow_summary.setWordWrap(True)
        head.addWidget(self.flow_summary, 1)
        self.flow_close = QToolButton()
        self.flow_close.setText("×")
        self.flow_close.setToolTip(tr("流れを閉じる"))
        self.flow_close.clicked.connect(lambda: self.choose(None))
        head.addWidget(self.flow_close, 0, Qt.AlignmentFlag.AlignTop)
        self.flow_strip = FlowStrip()
        self.steps_table = QTableWidget(0, 4)
        self.steps_table.setHorizontalHeaderLabels([tr("期間"), tr("活動"), tr("画像"), tr("移動")])
        self.steps_table.verticalHeader().hide()
        self.steps_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.steps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.steps_table.horizontalHeader().setStretchLastSection(True)
        self.steps_table.setToolTip(tr("クリックでその画像へ移動して記録の場所を表示。ダブルクリックで記録を編集"))
        self.steps_table.cellClicked.connect(self._step_clicked)
        self.steps_table.cellDoubleClicked.connect(self._step_double_clicked)
        self.steps_columns = column_menus("observation-steps", self.steps_table, self)
        fl.addLayout(head)
        fl.addWidget(self.flow_strip)
        fl.addWidget(self.steps_table)
        self.flow_more = QGroupBox(tr("このモノの詳細"))
        self.flow_more.setCheckable(True)
        self.flow_more.setChecked(False)
        more = QFormLayout(self.flow_more)
        self.rename_input = QLineEdit()
        self.rekind_input = QLineEdit()
        self.rekind_input.setCompleter(QCompleter(self.kinds_model, self.rekind_input))
        self.merge_select = QComboBox()
        more.addRow(tr("名前"), self.rename_input)
        more.addRow(tr("種別"), self.rekind_input)
        more.addRow(tr("同一のモノとして統合"), self.merge_select)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("名前・種別の変更"), tr("このモノのすべての記録の名前・種別を上の値に変える"), self._rename_chosen))
        actions.addWidget(self._button(tr("統合"), tr("このモノの記録を、選んだ統合先のモノの記録にする（同じモノを別々に記録していたとき）"),
                                       self._merge_chosen))
        actions.addWidget(self._button(tr("範囲へ移動"), tr("このモノの記録すべての範囲へ地図を移動"), lambda: self._fit_flow(self._chosen_flow())))
        self.remove_all_button = self._button(tr("このモノの記録を削除"), tr("このモノの記録をすべて削除（2 回押す）"), self._remove_chosen)
        actions.addWidget(self.remove_all_button)
        more.addRow(actions)
        self.flow_more.toggled.connect(lambda on: [w.setVisible(on) for w in self.flow_more.findChildren(QWidget)])
        for w in self.flow_more.findChildren(QWidget):
            w.setVisible(False)
        fl.addWidget(self.flow_more)
        self.flow_box.hide()
        b.addWidget(self.flow_box)

        # 詳細.
        self.more = QGroupBox(tr("詳細"))
        self.more.setCheckable(True)
        self.more.setChecked(False)
        mf = QFormLayout(self.more)
        self.deleted_check = QCheckBox(tr("削除済みも表示"))
        self.deleted_check.toggled.connect(lambda _on: self.render())
        self.observer_input = QLineEdit(settings.recorder_setting())
        self.observer_input.setToolTip(tr("記録に付ける名前（空欄ならログインのユーザー名）"))
        self.observer_input.editingFinished.connect(self._observer_typed)
        mf.addRow(self.deleted_check)
        mf.addRow(tr("記録者"), self.observer_input)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("報告書…"), tr("期間・分類・確度で絞って報告書を作成（保存・印刷）"), self.open_report))
        actions.addWidget(self._button(tr("通報…"), tr("通報の条件に合う記録から通報文を作成"),
                                       lambda: self.open_alerts()))
        actions.addWidget(self._button(tr("最新を取得"), tr("最新の記録を取得（ほかの人の記録も）"), self.reload))
        self.taxonomy_button = self._button(tr("分類体系の編集…"), tr("分類体系を編集"),
                                            lambda: self.edit_taxonomy and self.edit_taxonomy(self.class_filter))
        actions.addWidget(self.taxonomy_button)
        actions.addWidget(self._button(tr("エクスポート…"), tr("期間・場所・大分類で絞って、CSV か JSON に保存"),
                                       lambda: self.open_export()))
        actions.addWidget(self._button(tr("レイヤーとして追加"), tr("記録とモノの流れを、レイヤーとして地図に追加"),
                                       self.to_layers))
        mf.addRow(actions)
        self.clear_button = self._button(tr("この PC の記録を消去…"), tr("この PC の記録と確認範囲を消去（復元できない）"),
                                         lambda: self.clear_local())
        mf.addRow(self.clear_button)
        self.more.toggled.connect(self._more_toggled)
        for w in self.more.findChildren(QWidget):
            w.setVisible(False)
        b.addWidget(self.more)

        # Keys, while the dock has the focus (changeable in Settings ▸ Keyboard Shortcuts).
        key = shortcuts.dock_key
        key(self, "ObsPreviousImage", "観測記録（ドック内）: 前の画像", "PgUp", lambda: self.step(-1))
        key(self, "ObsNextImage", "観測記録（ドック内）: 次の画像", "PgDown", lambda: self.step(1))
        key(self, "ObsSubmit", "観測記録（ドック内）: フォームの記録・更新", "Ctrl+Return", self._submit_key)
        key(self, "ObsSubmitKeypad", "観測記録（ドック内）: フォームの記録・更新（テンキーの Enter）", "Ctrl+Enter", self._submit_key)
        key(self, "ObsEscape", "観測記録（ドック内）: 取消・終了", "Esc", self.escape)
        key(self, "ObsRecord", "観測記録（ドック内）: 記録の開始・終了", "R", self.toggle_recording)
        key(self, "ObsPoints", "観測記録（ドック内）: 入力点数の切り替え", "L", lambda: self.set_points(self.points % 4 + 1))
        key(self, "ObsArea", "観測記録（ドック内）: 確認範囲の記録", "A", lambda: self.set_area_mode(not (self.area_mode and not self.area_unseen)))
        key(self, "ObsUnseenArea", "観測記録（ドック内）: 不可範囲（雲など）の記録", "K",
            lambda: self.set_area_mode(not (self.area_mode and self.area_unseen), unseen=True))
        key(self, "ObsMarks", "観測記録（ドック内）: 地図の印の表示・非表示", "H", self.toggle_marks)
        key(self, "ObsStamp", "観測記録（ドック内）: 連続（見本で続けて記録）", "S", lambda: self.stamp_button.setChecked(not self.stamp_button.isChecked()))
        key(self, "ObsRow", "観測記録（ドック内）: 並べる（見本を列・格子に）", "G", lambda: self.row_button.setChecked(not self.row_button.isChecked()))
        key(self, "ObsUndoMany", "観測記録（ドック内）: 連続・並べるの 1 つ取り消し", "Backspace", lambda: self.many.undo() if self.many.mode else None)
        key(self, "ObsRowMore", "観測記録（ドック内）: 並べる数を 1 つ増やす", "+", lambda: self.many.step(1))
        key(self, "ObsRowFewer", "観測記録（ドック内）: 並べる数を 1 つ減らす", "-", lambda: self.many.step(-1))
        for n in range(1, 10):
            key(self, f"ObsActivity{n}", f"観測記録（ドック内）: 活動 {n} 番目", str(n), lambda n=n: self._chip(n))
        for n in range(1, 6):
            key(self, f"ObsCandidate{n}", f"観測記録（ドック内）: 候補 {n} 番目", f"Alt+{n}", lambda n=n: self._candidate_key(n))

    # ---- the stores ----------------------------------------------------

    def _observer(self) -> str:
        url = self.store_set.config.url if self.store_set and self.store_set.config else None
        return settings.recorder_name(url)

    def reset_stores(self) -> None:
        """
        The stores of the settings file (again), the local one last (unless
        `localObservations` is false). The store chosen stays chosen while the
        settings file and its stores are the same; another file, or other
        stores in it, choose its first store (the local one only when there is no other).
        """
        c = self.context
        try:
            self.sets = [self.exercise_store] if self.exercise_store is not None else store_sets(
                c.config.observation_stores, c.config.observation_vocabulary, c.gpkg, c.esri, c.token_for, local=c.config.local_observations)
        except Exception as error:  # noqa: BLE001 (the local GeoPackage could not be opened)
            self.sets = []
            self._show_status(self.tr("保存先の準備に失敗: {e}").format(e=error_text(error)))
        self.books = {}
        self._after_read = {}
        read = (c.source, tuple((s.label, s.config.url if s.config else None) for s in self.sets))
        same, self._stores_read = read == self._stores_read, read
        was = self.store_select.currentText()
        self.store_select.blockSignals(True)
        self.store_select.clear()
        for s in self.sets:
            self.store_select.addItem(s.label)
        i = self.store_select.findText(was) if same else -1
        self.store_select.setCurrentIndex(i if i >= 0 else 0)
        self.store_select.blockSignals(False)
        self.store_select.setVisible(len(self.sets) > 1)
        self._store_chosen(self.store_select.currentIndex())
        self._fill_satellite_input()

    def use_store(self, store: Optional[StoreSet]) -> None:
        """
        判読の練習: records go to `store` alone while an exercise runs (a store of their own, read afresh); None: the
        settings file's stores again, the one chosen before the exercise chosen again.
        """
        if store is not None and self.exercise_store is None:
            self._store_before = self.store_select.currentText()
        self.close_form()
        self._clear_points()
        self.exercise_store = store
        self.reset_stores()
        if store is None and self._store_before:
            i = self.store_select.findText(self._store_before)
            self._store_before = ""
            if i >= 0 and i != self.store_select.currentIndex():
                self.store_select.setCurrentIndex(i)

    def _book_of(self, s: StoreSet) -> ObservationBook:
        book = self.books.get(s.label)
        if book is None:
            # Who records is read here (QgsSettings, the authentication database), never in a task.
            book = ObservationBook(s.records, s.areas, s.taxonomy, s.vocabulary, observer=lambda: self._observer_cache,
                                   prefs=settings.SettingsPrefs(), say=self.said.emit,
                                   drafts=OutboxDrafts(self.context.offline, settings.SettingsPrefs()))
            book.drafts()  # those kept in QgsSettings before moved to the outbox, here on the main thread
            self.books[s.label] = book
        return book

    def _store_chosen(self, index: int) -> None:
        if not (0 <= index < len(self.sets)):
            self.book = self.store_set = None
            self.render()
            return
        self.close_form()
        if self.many.mode:
            self.set_many_mode("")
        self.many.reset()
        self.store_set = self.sets[index]
        self._observer_cache = self._observer()
        self.book = self._book_of(self.store_set)
        # A quiet read of the store before, still reading, does not hold back this one's (it never tells it ended).
        self._refreshing = self._refresh_again = False
        minutes = self.store_set.config.refresh_minutes if self.store_set.config is not None else None
        self._refresh_timer.stop()
        if minutes:
            self._refresh_timer.start(int(minutes * 60 * 1000))
        # Shown (and the records read once an image is chosen: render).
        self.refresh_images()

    def _observer_typed(self) -> None:
        settings.set_recorder(self.observer_input.text())
        self._observer_cache = self._observer()

    def set_observer(self, name: str) -> None:
        self.observer_input.setText(name.strip())
        self._observer_typed()

    def observer(self) -> str:
        return self._observer_cache if self.book else ""

    # ---- doing -----------------------------------------------------------

    def _show_status(self, message: str) -> None:
        self.status.setText(message)
        if self._idle:
            self._render_idle_note()

    def waiting_ids(self) -> set[str]:
        """The records of this store saved offline and waiting to be sent (marked in the lists and on the map)."""
        ids = getattr(self.book.store, "pending_ids", None) if self.book is not None else None
        try:
            return ids() if callable(ids) else set()
        except Exception:  # noqa: BLE001 (the file of offline work unreadable: nothing marked)
            return set()

    def _offline_changed(self) -> None:
        try:
            self.offlineChanged.emit()  # from any thread
        except RuntimeError:
            pass

    def _show_offline(self) -> None:
        tr = self.tr
        store = self.book.store if self.book is not None else None
        down = bool(getattr(store, "offline", False))
        n = len(self.waiting_ids())
        if down:
            self.offline_note.setText(tr("オフラインで作業中（最後に取得した記録を表示）。保存した記録は「送信待ち」（青）に保管し、つながったときに送信")
                                      + (tr("（送信待ち {n} 件）").format(n=n) if n else ""))
        elif n:
            self.offline_note.setText(tr("送信待ちの記録 {n} 件（青）。ステータスバーの「送信待ち」から送信").format(n=n))
        self.offline_note.setVisible(down or bool(n))

    def say(self, message: str) -> None:
        if self.book is not None:
            self.book.status = message
        self._show_status(message)

    def _place_records(self, book: ObservationBook) -> None:
        """
        How the open images lie on the map now (read here, on the main thread),
        for the records saved next to say where they are in their image's file
        (PIXEL_X・PIXEL_Y・IMAGE_FILE, saved in a task for a shared store).
        """
        try:
            book.locate = pixel_locator(book.images)
        except Exception:  # noqa: BLE001 (never stops a record: saved without its pixels)
            book.locate = None

    def do(self, description: str, work: Callable[[ObservationBook], Any], after: Optional[Callable[[Any], None]] = None,
           watch: bool = False, failed: Optional[Callable[[str], None]] = None) -> Any:
        """
        Runs what changes the book: at once for the local store (its result
        returned), else in a task, one after another (None returned; `after`
        gets the result, `failed` the reason when the task fails).
        """
        book, s = self.book, self.store_set
        if book is None or s is None:
            return None
        self._place_records(book)
        if watch and self._alert_watch() is not None:
            work, after = self._watching(work, after)
        if not s.remote:
            result = work(book)
            if after is not None:
                after(result)
            self.render()
            return result.result if isinstance(result, _Saved) else result
        self._queue.append((description, work, after, book, failed))
        self._next()
        return None

    def _next(self) -> None:
        if self._running or not self._queue:
            return
        description, work, after, book, failed = self._queue.pop(0)
        self._running = True

        def finish(result=None, message: Optional[str] = None) -> None:
            self._running = False
            if message is not None:
                self.say(self.tr("{d}に失敗: {m}").format(d=description, m=message))
                if failed is not None and book is self.book:
                    failed(message)
            elif book is self.book and after is not None:
                after(result)
            if book is self.book:
                self.render()
            self._next()

        run_task(description, lambda task: work(book), lambda r: finish(r), lambda m: finish(message=m))

    def run_now(self, description: str, work: Callable[[ObservationBook], Any]) -> Any:
        """
        Runs what changes the book and waits for it (for scripts): at once for
        the local store, else in a task while QGIS keeps answering.
        """
        book, s = self.book, self.store_set
        if book is None or s is None:
            raise RuntimeError("観測記録は使えない")
        self._place_records(book)
        if not s.remote:
            try:
                return work(book)
            finally:
                self.render()
        got: dict[str, Any] = {}
        loop = QEventLoop()

        def done(result) -> None:
            got["result"] = result
            loop.quit()

        def failed(message: str) -> None:
            got["error"] = message
            loop.quit()

        run_task(description, lambda task: work(book), done, failed)
        if not got:
            loop.exec()
        self.render()
        if "error" in got:
            raise RuntimeError(f"{description}に失敗: {got['error']}")
        return got.get("result")

    def report_html(self, options) -> str:
        """The report with its map drawn (waits for the map; for scripts)."""
        from .obs_report_dialog import ReportMap

        book = self.book
        # What the report holds (a shared store: asked of it for the period and class).
        self.run_now(self.tr("報告書の記録の取得"), lambda b: b.read_for_report(options))
        selection = book.report_selection(options)
        url: dict[str, Optional[str]] = {}
        if options.map and (selection.records or selection.areas):
            loop = QEventLoop()
            kinds = list(dict.fromkeys([*book.vocabulary.kinds, *(t.path[0] for t in book.taxonomy())]))

            def done(data_url, _image) -> None:
                url["map"] = data_url
                loop.quit()

            # Kept until drawn (its job goes with it).
            drawer = ReportMap(self.canvas, selection.records, selection.areas if options.areas else [], kinds, done, self.say)
            drawer.start()
            if not url:
                loop.exec()
        return book.report(options, url.get("map"))

    def api(self) -> ObservationApi:
        """The observations for scripts (plugin.api)."""
        return ObservationApi(lambda: self.book, self.run_now, self.report_html)

    def reload(self) -> None:
        """
        Reads the records again from the store (and which are reported).
        Records not read yet (no image chosen, nothing has asked for them)
        are left until needed.
        """
        book = self.book
        if book is None or (not book.loaded and book.current is None):
            return
        self._read_records()

    def _read_records(self, then: Optional[Callable[[], Any]] = None) -> None:
        """Reads the records, areas and classification (one read at a time per store); `then` once read."""
        book = self.book
        if book is None:
            return
        key = id(book)
        if then is not None:
            waiting = self._after_read.setdefault(key, [])
            if then not in waiting:
                waiting.append(then)
        if key in self._reading:
            return
        self._reading.add(key)
        reading = self._reading

        def work(b: ObservationBook):
            try:
                return b.reload()
            finally:
                reading.discard(key)

        def after(_r) -> None:
            self.refresh_reported()
            for f in self._after_read.pop(key, []):
                f()

        def failed(_message: str) -> None:
            reading.discard(key)
            self._after_read.pop(key, None)

        self.do(self.tr("観測記録の取得"), work, after=after, failed=failed)

    def _read_around(self) -> None:
        """The records around the image recorded now (a shared store), read in a task when not read yet."""
        book = self.book
        if book is None or not book.needs_around() or id(book) in self._reading_around:
            return
        key = id(book)
        reading = self._reading_around
        reading.add(key)

        def work(b: ObservationBook):
            try:
                return b.read_around()
            finally:
                reading.discard(key)

        self.do(self.tr("まわりの記録の取得"), work, failed=lambda _m: reading.discard(key))

    def fetch(self, description: str, work: Callable[[ObservationBook], Any], then: Callable[[Any], None],
              failed: Optional[Callable[[str], None]] = None) -> None:
        """
        Reads what a feature needs of the records (`work(book)`: book.fetch and the like, scope.py) and
        gives it to `then`: at once for the local store, else in a task (`failed` gets the reason when it fails).
        """
        if self.book is None:
            if failed is not None:
                failed(self.tr("観測記録の保存先なし"))
            return
        self.do(description, work, after=then, failed=failed)

    def with_records(self, ids: Sequence[str], then: Callable[[], Any]) -> bool:
        """
        Whether the records of `ids` are here to be used now (book.record): a
        shared store's are asked of it first, as they are now (then `then`
        runs, and False is returned); a local store's once read.
        """
        book = self.book
        ids = tuple(dict.fromkeys(i for i in ids if i))
        if book is None or not ids:
            return True
        if not book.scoped:
            return self.records_ready(then)
        self.fetch(self.tr("観測記録の取得"), lambda b: b.fetch(RecordQuery(ids=ids)), lambda _r: then(), lambda _m: None)
        return False

    def records_ready(self, again: Optional[Callable[[], Any]] = None) -> bool:
        """
        Whether the book is read (the classification, and the records: all of
        the local store's; a shared store's around the image recorded on, the
        rest asked per feature with `fetch`): read only once needed, so the
        first time it is read now. True at once for the local store (read at
        once); else False, and `again` runs once it is read.
        """
        book, s = self.book, self.store_set
        if book is None or s is None or book.loaded:
            return True
        if not s.remote:
            self._read_records()
            return True
        self._read_records(again)
        return False

    def read_now(self) -> Optional[ObservationBook]:
        """The book with its records read, waiting for them when not read yet (scripts, 「CSV 保存」, 「レイヤー化」)."""
        book = self.book
        if book is not None and not book.loaded:
            self.run_now(self.tr("観測記録の取得"), lambda b: b.reload())
            self.refresh_reported()
        return self.book

    #: The least time between two quiet reads on another image (s): flipping through images asks once.
    REFRESH_GAP = 30

    def refresh(self, force: bool = False) -> None:
        """
        Reads quietly what others saved since the last read (an Esri store;
        only the records with a newer 更新日時 when the layer has one). Asked
        on another image (at most every REFRESH_GAP s), after a save that
        found someone else's change, when a record is opened to change, and
        every `refreshMinutes`. A record open in the form that someone else
        changed meanwhile is filled again when nothing was typed yet, else
        told.
        """
        book, s = self.book, self.store_set
        if book is None or s is None or not s.remote or not book.loaded:
            return
        if self._refreshing:
            # One is reading already, maybe from before what this asks for (a change saved just now, a record
            # opened to change): read once more after it, not dropped.
            self._refresh_again = self._refresh_again or force
            return
        if not force and time.monotonic() - self._refreshed_at < self.REFRESH_GAP:
            return
        self._refreshing = True
        self._refresh_again = False
        self._refreshed_at = time.monotonic()

        def finished() -> bool:
            """Whether to read once more (asked while this one read)."""
            self._refreshing = False
            again, self._refresh_again = self._refresh_again, False
            return again and self.book is book

        def done(said) -> None:
            again = finished()
            if said and self.book is book:
                self._editing_changed()  # may read again itself (the form opened again)
            if again and not self._refreshing:
                self.refresh(force=True)

        def failed(_message: str) -> None:
            finished()  # (a later refresh tries again)

        self.do(self.tr("ほかの人の記録の取得"), lambda b: b.refresh(), after=done, failed=failed)

    def _editing_changed(self) -> None:
        """The record open in the form, if someone else changed it: filled again (nothing typed yet) or told."""
        if self.pending is None or self.pending[2] is None or self.book is None:
            return
        editing = self.pending[2]
        now = self.book.record(editing.id)
        if now is None or now == editing:
            return
        who = now.updated_by or now.recorded_by or self.tr("ほかの人")
        if self._filled is not None and self.form_values() == self._filled:
            self.open_form(now.lon, now.lat, editing=now)
            self.iface.messageBar().pushInfo(self.tr("観測記録"), plain_tip(self.tr("開いている記録を {who} さんが更新。最新の内容を表示").format(who=who)))
        else:
            self.iface.messageBar().pushWarning(self.tr("観測記録"), plain_tip(self.tr(
                "開いている記録を {who} さんが更新。このまま「更新」しても上書きしない（Esc で閉じて開き直すと最新の内容）").format(who=who)))

    def reload_taxonomy(self) -> None:
        """Reads the classification again from the store (others may have changed it), and the 備考's glossary."""
        self.do(self.tr("分類の取得"), lambda b: b.reload_taxonomy())
        self.context.glossary.read(force=True)

    def _merge_chosen(self) -> None:
        flow = self._chosen_flow()
        into = self.merge_select.currentData()
        if flow is None or not into:
            return
        if not ask_no(self, self.tr("統合"), self.tr("「{a}」の記録 {n} 件を「{b}」に統合。元に戻すには 1 件ずつ修正が必要").format(
                a=flow.name, n=len(flow.observations), b=self.merge_select.currentText()), yes=self.tr("統合")):
            return
        self.chosen = into
        self.do(self.tr("統合"), lambda b: b.merge(flow.object_id, into))

    def ask_remove(self, records: list[Observation]) -> bool:
        """Asks before deleting the chosen records (取消 by default)."""
        things = len({o.object_id for o in records})
        what = self.tr("選んだ記録 {n} 件（モノ {m} 件）を削除").format(n=len(records), m=things)
        how = (self.tr("この保存先では完全に消え、復元できない") if self.book.removes_for_good()
               else self.tr("「詳細 ▸ 削除済みも表示」で復元できる"))
        return ask_no(self, self.tr("選んだ記録の削除"), what + "\n\n" + how, yes=self.tr("削除"))

    def remove_selected(self, records: Optional[list[Observation]] = None, ask: bool = True) -> Any:
        """Deletes the chosen records (a thing's row: all its records) after asking; what went through and what not is told."""
        book = self.book
        records = self.selected_records() if records is None else records
        if book is None or not records:
            return None
        if ask and not self.ask_remove(records):
            return None
        ids = [o.id for o in records]
        return self.do(self.tr("選んだ記録の削除"), lambda b: b.remove_many(ids))

    def close_down(self) -> None:
        """Called when the plugin unloads."""
        self._visibility(False)
        self._use_tool(False)
        project = QgsProject.instance()
        for signal, slot in ((project.layersAdded, self._layers_changed), (project.layersRemoved, self._layers_changed),
                             (self.iface.currentLayerChanged, self._layer_chosen), (self.canvas.extentsChanged, self._extent_changed),
                             (self.context.configChanged, self.reset_stores), (self.context.configChanged, self.assignment_bar.reset)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.footprints.close()
        if self._alerts is not None:
            try:
                self.context.configChanged.disconnect(self._alerts.reset)
            except (TypeError, RuntimeError):
                pass
            self._alerts.close()
        if self._handover is not None:
            try:
                self.context.configChanged.disconnect(self._handover.reset)
            except (TypeError, RuntimeError):
                pass
            self._handover.close()
        if self._my_records is not None:
            self._my_records.close()
        scene = self.canvas.scene()
        if scene is not None:
            scene.removeItem(self.overlay)
