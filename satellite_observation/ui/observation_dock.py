"""
The 観測記録 dock (the viewer's observation panel): things seen on the
images recorded image by image, in order of acquisition, as points on the
map, carried over between images, and followed as each thing's flow.

The workings are core.obs.book.ObservationBook; this dock shows it, sends
the map's clicks to it, and runs what an Esri store answers in a task (the
local GeoPackage answers at once, on this thread). Keys work while the dock
has the focus: PageUp / PageDown step through the images, R records by
clicks, L by two points, A a checked area, 1–9 pick an activity, Alt+1–5 a
match candidate, Esc steps back.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

from qgis.core import QgsProject, QgsRasterLayer, QgsRectangle
from qgis.PyQt.QtCore import QDateTime, QEventLoop, QStringListModel, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QKeySequence, QPainter
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDateTimeEdit, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSizePolicy, QTabBar, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

try:  # Qt6
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QShortcut

from ..core.obs.api import ObservationApi
from ..core.obs.areas import CheckedArea, area_label, box_ring
from ..core.obs.book import FormValues, ObservationBook, ObservedImage
from ..core.obs.csv import csv_file_name, local_time, observations_csv
from ..core.obs.flows import (
    CarryOver, MatchCandidate, ObjectFlow, ends_of, flow_of, heading_text, living, now_ms, time_key, two_point,
)
from ..core.obs.geo import get_distance, js_round, km_text
from ..core.obs.model import Observation
from ..core.obs.output import observation_layers
from ..core.obs.setup import StoreSet, store_sets
from ..core.obs.taxonomy import (
    children_of, depth_of, find, kind_of, level_name, member_index, name_label_of, nest, path_of, spec_text, specs_at, within,
)
from ..qgis_io import settings
from ..qgis_io.context import PluginContext
from ..qgis_io.geometry import from_wgs84, to_wgs84
from ..qgis_io.obs_images import open_images, set_manual_time
from ..qgis_io.obs_layers import observation_memory_layer
from ..qgis_io.tasks import run_task
from .docks import PluginDock
from .flow_layout import FlowLayout
from .obs_overlay import Mark, ObservationOverlay, Scene, color_of
from .obs_tool import ObservationTool

HINT = "地図クリックで記録（白抜きの印は継続）"
HINT_TWO = "前端（船首・機首）→ 後端の 2 点クリックで記録（白抜きの印は継続）"
HINT_AREA = "確認範囲: 地図で 2 点クリック（対角）、または「表示範囲」「画像の範囲」"
NO_IMAGE = "画像なし（画像を開く、または ＋ でファイルなしの画像を追加）"
ACTIVITY_COLORS = ["#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5", "#e11d48"]


def _split(text: str) -> list[str]:
    import re

    return [w.strip() for w in re.split(r"[,、\n]", text) if w.strip()]


def _ms_of(edit: QDateTimeEdit) -> int:
    return edit.dateTime().toMSecsSinceEpoch()


class FlowStrip(QWidget):
    """A thing's flow along time: a bar per step, coloured by activity, the images as ticks."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.flow: Optional[ObjectFlow] = None
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_flow(self, flow: Optional[ObjectFlow]) -> None:
        self.flow = flow
        if flow:
            tips = [f"{(s.activity or '—') if s.present else '不在'}: {local_time(s.start)}{f' 〜 {local_time(s.end)}' if s.end != s.start else ''}"
                    for s in flow.steps]
            self.setToolTip("\n".join(tips))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        flow = self.flow
        if not flow:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = self.width()
        n = len(flow.observations)
        gap = (flow.last - flow.first) / (n - 1) if n > 1 else 0
        span = max(1, flow.last + gap - flow.first)

        def x(t: float) -> float:
            return width / 2 if flow.first == flow.last else 0.01 * width + (t - flow.first) / span * 0.98 * width

        activities = list(dict.fromkeys(s.activity for s in flow.steps))
        for i, s in enumerate(flow.steps):
            nxt = flow.steps[i + 1] if i + 1 < len(flow.steps) else None
            start = x(s.start)
            end = x(nxt.start) if nxt else max(x(s.end + gap), start + 6)
            color = QColor(ACTIVITY_COLORS[activities.index(s.activity) % len(ACTIVITY_COLORS)]) if s.present else QColor("#d4d4d8")
            p.setPen(QColor("#dc2626") if not s.present else Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(int(start), 6, int(max(6, end - start)), 14, 3, 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#111111"))
        for o in flow.observations:
            p.drawRect(int(x(time_key(o)) - 1), 22, 2, 8)
        p.end()


class ObservationDock(PluginDock):
    """Recording what is seen on the images, image by image."""

    #: A message of the book, from any thread (shown on this one).
    said = pyqtSignal(str)
    #: The records or the classification changed (for the classification editor).
    changed = pyqtSignal()

    def __init__(self, iface, context: PluginContext, open_settings: Callable[[], None], parent: Optional[QWidget] = None,
                 edit_taxonomy: Optional[Callable[[Optional[list]], None]] = None):
        super().__init__("SatObsObservations", "観測記録", context, open_settings, parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.edit_taxonomy = edit_taxonomy
        self.sets: list[StoreSet] = []
        self.books: dict[str, ObservationBook] = {}
        self.book: Optional[ObservationBook] = None
        self.store_set: Optional[StoreSet] = None
        self.tab = "image"
        self.chosen: Optional[str] = None
        self.taxon: Optional[list[str]] = None
        self.class_filter: Optional[list[str]] = None
        #: The form's place and what it is about: (lon, lat, the record changed, the thing carried over).
        self.pending: Optional[tuple] = None
        self.offered: list[MatchCandidate] = []
        self.active = False
        self.two_point = settings.two_point_setting()
        self.area_mode = False
        self.front: Optional[tuple] = None
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
        self.said.connect(self._show_status)

        self._build()
        self.overlay = ObservationOverlay(self.canvas)
        self.overlay.hide()
        self.tool = ObservationTool(self.canvas, self.overlay, self.map_clicked, self.map_hovered, self.map_dragged,
                                    lambda: self.active and not self.area_mode and not self.front)
        self.tool.deactivated.connect(self._tool_gone)
        self.canvas.extentsChanged.connect(self._extent_changed)
        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        self.iface.currentLayerChanged.connect(self._layer_chosen)
        context.configChanged.connect(self.reset_stores)
        self.visibilityChanged.connect(self._visibility)
        self.reset_stores()

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
        self.store_select = QComboBox()
        self.store_select.setToolTip(tr("記録の保存先"))
        self.store_select.currentIndexChanged.connect(self._store_chosen)
        b.addWidget(self.store_select)

        # The image.
        row = QHBoxLayout()
        self.prev_button = self._button("◀", tr("前の画像（撮像日時の順、PageUp）"), lambda: self.step(-1))
        self.image_select = QComboBox()
        self.image_select.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.image_select.setToolTip(tr("画像"))
        self.image_select.activated.connect(self._image_chosen)
        self.next_button = self._button("▶", tr("次の画像（撮像日時の順、PageDown）"), lambda: self.step(1))
        self.list_button = self._button("＋", tr("ファイルなしの画像の追加（カタログ外・未オープンの画像を ID・撮像日時で指定）"),
                                        self._toggle_list_form, checkable=True)
        self.only = QCheckBox(tr("この画像のみ"))
        self.only.setToolTip(tr("この画像のみ地図に表示（他の画像は非表示）"))
        self.only.toggled.connect(self._apply_only)
        for w in (self.prev_button, self.image_select, self.next_button, self.list_button, self.only):
            row.addWidget(w)
        b.addLayout(row)

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
        note = QLabel(tr("ファイルなしで記録（背景地図・他の画像の上をクリック）。保存先ごとにこの QGIS で保持"))
        note.setWordWrap(True)
        f.addRow(note)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("追加"), "", self._list_from_form))
        self.unlist_button = self._button(tr("一覧から除外"), tr("この画像の一覧からの除外（記録は残る）"), self._unlist)
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
        t.addWidget(QLabel(tr("撮像日時不明（入力）")))
        self.time_input = QDateTimeEdit()
        self.time_input.setCalendarPopup(True)
        self.time_input.setDisplayFormat("yyyy-MM-dd HH:mm")
        t.addWidget(self.time_input, 1)
        t.addWidget(self._button(tr("入力"), "", self._set_image_time))
        self.time_row.hide()
        b.addWidget(self.time_row)

        # Recording.
        row = QHBoxLayout()
        self.record_button = self._button(tr("クリック記録"), tr("クリック地点にこの画像の観測を記録（R）"), self.set_active, checkable=True)
        self.two_point_button = self._button(tr("2 点"), tr("前端（船首・機首）→ 後端の 2 点クリックで、中心・長さ・向きを記録（L）"),
                                             self.set_two_point, checkable=True)
        self.two_point_button.setChecked(self.two_point)
        self.area_button = self._button(tr("確認範囲"), tr("確認済みの範囲の記録（範囲内の対象は記録がすべて、記録のないものは不在）（A）"),
                                        self.set_area_mode, checkable=True)
        for w in (self.record_button, self.two_point_button, self.area_button):
            row.addWidget(w)
        row.addStretch(1)
        b.addLayout(row)
        row = QHBoxLayout()
        self.carry_label = QLabel()
        self.carry_all_button = self._button(tr("すべて継続"), tr("前後の画像のモノ（白抜きの印）を、この画像でも継続として一括記録（確認範囲内のものを除く）"),
                                             self.carry_all)
        self.absent_inside_button = self._button("", tr("確認範囲内で記録のない前後の画像のモノを、この画像での不在として一括記録"), self.absent_inside)
        row.addWidget(self.carry_label, 1)
        row.addWidget(self.carry_all_button)
        row.addWidget(self.absent_inside_button)
        b.addLayout(row)

        # A checked area.
        self.area_form = QGroupBox(tr("確認範囲"))
        f = QFormLayout(self.area_form)
        self.area_range = QLabel()
        f.addRow(self.area_range)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("表示範囲"), tr("地図の表示範囲を確認範囲に"), self._area_view))
        self.area_image_button = self._button(tr("画像の範囲"), tr("画像の範囲全体を確認範囲に"), self._area_image)
        actions.addWidget(self.area_image_button)
        f.addRow(actions)
        self.area_kind = QLineEdit()
        self.area_kind.setPlaceholderText(tr("空欄ですべての種別"))
        self.area_kinds_model = self._completer(self.area_kind)
        self.area_note = QLineEdit()
        f.addRow(tr("対象の種別"), self.area_kind)
        f.addRow(tr("備考"), self.area_note)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("確認範囲の記録（Enter）"), "", self._submit_area))
        actions.addWidget(self._button(tr("取消（Esc）"), "", lambda: self.area_button.setChecked(False)))
        f.addRow(actions)
        self.area_kind.returnPressed.connect(self._submit_area)
        self.area_note.returnPressed.connect(self._submit_area)
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
        self.kind_path_box = QWidget()
        self.kind_path_layout = FlowLayout(self.kind_path_box)
        f.addRow(self.kind_path_box)
        self.specs_label = QLabel()
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
        self.length_label = QLabel(tr("長さ（m）"))
        self.heading_label = QLabel(tr("向き（°）"))
        f.addRow(self.length_label, self.length_input)
        f.addRow(self.heading_label, self.heading_input)
        self.measure_note = QLabel()
        f.addRow(self.measure_note)
        self.note_input = QLineEdit()
        f.addRow(tr("備考"), self.note_input)
        actions = QHBoxLayout()
        self.submit_button = self._button(tr("記録（Enter）"), "", lambda: self.submit(True))
        self.absent_button = self._button(tr("不在"), tr("この画像での不在（見当たらず）を記録"), lambda: self.submit(False))
        self.delete_button = self._button(tr("削除"), tr("この記録の削除（「詳細」の「削除済みも表示」から復元可）"), self._delete_pending)
        for w in (self.submit_button, self.absent_button, self.delete_button, self._button(tr("取消（Esc）"), "", self.close_form)):
            actions.addWidget(w)
        f.addRow(actions)
        for edit in (self.object_input, self.kind_input, self.activity_input, self.length_input, self.heading_input, self.note_input):
            edit.returnPressed.connect(lambda: self.submit(True))
        self.form.hide()
        b.addWidget(self.form)

        self.status = QLabel()
        self.status.setWordWrap(True)
        b.addWidget(self.status)

        # The lists.
        self.tabs = QTabBar()
        for name in (tr("この画像"), tr("モノ"), tr("分類")):
            self.tabs.addTab(name)
        self.tabs.setTabToolTip(2, tr("種別の分類体系（階層・階層名・既知の個体）"))
        self.tabs.currentChanged.connect(self._tab_changed)
        b.addWidget(self.tabs)
        self.class_filter_row = QWidget()
        row = QHBoxLayout(self.class_filter_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.class_filter_label = QLabel()
        row.addWidget(self.class_filter_label, 1)
        unfilter = QToolButton()
        unfilter.setText("×")
        unfilter.setToolTip(tr("分類の絞り込み解除"))
        unfilter.clicked.connect(lambda: self.filter_class(None))
        row.addWidget(unfilter)
        self.class_filter_row.hide()
        b.addWidget(self.class_filter_row)
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.cellDoubleClicked.connect(self._row_double_clicked)
        b.addWidget(self.table, 1)
        self._row_actions: list[tuple[Callable, Optional[Callable]]] = []

        # The chosen thing's flow.
        self.flow_box = QGroupBox()
        fl = QVBoxLayout(self.flow_box)
        self.flow_summary = QLabel()
        self.flow_summary.setWordWrap(True)
        self.flow_strip = FlowStrip()
        self.steps_table = QTableWidget(0, 4)
        self.steps_table.setHorizontalHeaderLabels([tr("期間"), tr("活動"), tr("画像"), tr("移動")])
        self.steps_table.verticalHeader().hide()
        self.steps_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.steps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.steps_table.horizontalHeader().setStretchLastSection(True)
        self.steps_table.cellClicked.connect(self._step_clicked)
        fl.addWidget(self.flow_summary)
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
        actions.addWidget(self._button(tr("名前・種別の変更"), "", self._rename_chosen))
        actions.addWidget(self._button(tr("統合"), "", self._merge_chosen))
        actions.addWidget(self._button(tr("範囲へ移動"), "", lambda: self._fit_flow(self._chosen_flow())))
        self.remove_all_button = self._button(tr("このモノの記録を削除"), "", self._remove_chosen)
        actions.addWidget(self.remove_all_button)
        more.addRow(actions)
        self.flow_more.toggled.connect(lambda on: [w.setVisible(on) for w in self.flow_more.findChildren(QWidget)])
        for w in self.flow_more.findChildren(QWidget):
            w.setVisible(False)
        fl.addWidget(self.flow_more)
        self.flow_box.hide()
        b.addWidget(self.flow_box)

        # The chosen class (「分類」).
        self.taxon_box = QGroupBox()
        tf = QFormLayout(self.taxon_box)
        self.taxon_name = QLineEdit()
        self.taxon_parent = QComboBox()
        self.taxon_levels = QLineEdit()
        self.taxon_levels.setPlaceholderText("大分類、種別、級")
        self.taxon_levels.setToolTip(tr("大分類から順に、読点区切り。この分類より下の枝はここが優先"))
        self.taxon_label = QLineEdit()
        self.taxon_label.setPlaceholderText("艦名・機番")
        self.taxon_label.setToolTip(tr("最下位の個体（実名）の呼び方"))
        self.taxon_members = QLineEdit()
        self.taxon_members.setToolTip(tr("この分類の既知の個体（実名）、読点区切り。記録時にモノ欄へ入力で分類も自動入力"))
        tf.addRow(tr("名前"), self.taxon_name)
        tf.addRow(tr("上位"), self.taxon_parent)
        tf.addRow(tr("この枝の階層名"), self.taxon_levels)
        tf.addRow(tr("個体名の呼称"), self.taxon_label)
        tf.addRow(tr("既知の個体"), self.taxon_members)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("変更"), "", self._apply_taxon))
        actions.addWidget(self._button(tr("モノ一覧"), "", lambda: self.filter_class(self.taxon)))
        self.taxon_delete = self._button(tr("削除"), tr("この分類と下位分類の削除（記録の種別は上位の分類に）"), self._delete_taxon)
        actions.addWidget(self.taxon_delete)
        tf.addRow(actions)
        child = QHBoxLayout()
        self.taxon_child = QLineEdit()
        self.taxon_child.returnPressed.connect(self._add_child)
        child.addWidget(self.taxon_child, 1)
        child.addWidget(self._button(tr("追加"), "", self._add_child))
        tf.addRow(tr("下位分類の追加"), child)
        self.taxon_box.hide()
        b.addWidget(self.taxon_box)
        self.taxa_box = QWidget()
        gl = QFormLayout(self.taxa_box)
        gl.setContentsMargins(0, 0, 0, 0)
        root = QHBoxLayout()
        self.taxon_root = QLineEdit()
        self.taxon_root.setPlaceholderText("船舶・航空機…（「船舶 > 支援船」で階層ごと）")
        self.taxon_root.returnPressed.connect(self._add_root)
        root.addWidget(self.taxon_root, 1)
        root.addWidget(self._button(tr("追加"), "", self._add_root))
        gl.addRow(tr("大分類の追加"), root)
        actions = QHBoxLayout()
        self.editor_button = self._button(tr("編集ツール"), tr("分類体系の編集ツール（ツリーのドラッグで移動・並べ替え、名前の直接編集）"),
                                          lambda: self.edit_taxonomy and self.edit_taxonomy(self.taxon))
        self.editor_button.setVisible(self.edit_taxonomy is not None)
        actions.addWidget(self.editor_button)
        actions.addWidget(self._button(tr("JSON 保存"), tr("分類体系の JSON 保存（config.json の observationVocabulary.taxonomy に記載可）"), self.save_taxonomy_file))
        actions.addWidget(self._button(tr("JSON 読み込み"), tr("分類体系の JSON 読み込み（現在の分類と置き換え）"), self.load_taxonomy_file))
        actions.addWidget(self._button(tr("更新"), tr("保存先から分類体系を読み直し（他の人の編集を反映）"), self.reload_taxonomy))
        gl.addRow(actions)
        self.taxa_note = QLabel()
        self.taxa_note.setWordWrap(True)
        gl.addRow(self.taxa_note)
        self.taxa_box.hide()
        b.addWidget(self.taxa_box)

        # 詳細.
        self.more = QGroupBox(tr("詳細"))
        self.more.setCheckable(True)
        self.more.setChecked(False)
        mf = QFormLayout(self.more)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText(tr("名前・ID・種別・活動"))
        self.filter_input.textChanged.connect(lambda _t: self.render())
        self.deleted_check = QCheckBox(tr("削除済みも表示"))
        self.deleted_check.toggled.connect(lambda _on: self.render())
        self.observer_input = QLineEdit(settings.recorder_setting())
        self.observer_input.setToolTip(tr("記録・更新に付く名前（空欄で認証設定のユーザー名）"))
        self.observer_input.editingFinished.connect(self._observer_typed)
        mf.addRow(tr("モノの絞り込み"), self.filter_input)
        mf.addRow(self.deleted_check)
        mf.addRow(tr("記録者"), self.observer_input)
        actions = QHBoxLayout()
        actions.addWidget(self._button(tr("報告書…"), tr("期間・分類・確度を指定した報告書（HTML 保存・印刷）"), self.open_report))
        actions.addWidget(self._button(tr("再読み込み"), tr("保存先から記録を再読み込み"), self.reload))
        actions.addWidget(self._button(tr("CSV 保存"), tr("すべての記録の CSV 保存（モノ・撮像日時の順）"), self.save_csv))
        actions.addWidget(self._button(tr("レイヤー化"), tr("記録をポイント、モノの流れをラインとして一時レイヤー化（属性テーブル・タイムライン・書き出しで利用可）"),
                                       self.to_layers))
        mf.addRow(actions)
        self.clear_button = self._button(tr("ローカル記録の消去…"), tr("ローカル GeoPackage の記録・確認範囲の完全な消去（削除済みのみも可。復元不可）"),
                                         lambda: self.clear_local())
        mf.addRow(self.clear_button)
        self.more.toggled.connect(lambda on: [w.setVisible(on) for w in self.more.findChildren(QWidget)])
        for w in self.more.findChildren(QWidget):
            w.setVisible(False)
        b.addWidget(self.more)

        # Keys, while the dock has the focus.
        def key(seq: str, slot: Callable) -> None:
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            s.activated.connect(slot)

        key("PgUp", lambda: self.step(-1))
        key("PgDown", lambda: self.step(1))
        key("Esc", self.escape)
        key("R", lambda: self.record_button.setChecked(not self.active))
        key("L", lambda: self.two_point_button.setChecked(not self.two_point))
        key("A", lambda: self.area_button.setChecked(not self.area_mode))
        for n in range(1, 10):
            key(str(n), lambda n=n: self._chip(n))
        for n in range(1, 6):
            key(f"Alt+{n}", lambda n=n: self._candidate_key(n))

    # ---- the stores ----------------------------------------------------

    def _observer(self) -> str:
        url = self.store_set.config.url if self.store_set and self.store_set.config else None
        return settings.recorder_name(url)

    def reset_stores(self) -> None:
        """The stores of the settings file (again), the local one last."""
        c = self.context
        try:
            self.sets = store_sets(c.config.observation_stores, c.config.observation_vocabulary, c.gpkg, c.esri, c.token_for)
        except Exception as error:  # noqa: BLE001 (the local GeoPackage could not be opened)
            self.sets = []
            self._show_status(self.tr("保存先の準備失敗: {e}").format(e=error))
        self.books = {}
        was = self.store_select.currentText()
        self.store_select.blockSignals(True)
        self.store_select.clear()
        for s in self.sets:
            self.store_select.addItem(s.label)
        i = self.store_select.findText(was)
        self.store_select.setCurrentIndex(i if i >= 0 else 0)
        self.store_select.blockSignals(False)
        self.store_select.setVisible(len(self.sets) > 1)
        self._store_chosen(self.store_select.currentIndex())

    def _book_of(self, s: StoreSet) -> ObservationBook:
        book = self.books.get(s.label)
        if book is None:
            # Who records is read here (QgsSettings, the authentication database), never in a task.
            book = ObservationBook(s.records, s.areas, s.taxonomy, s.vocabulary, observer=lambda: self._observer_cache,
                                   prefs=settings.SettingsPrefs(), say=self.said.emit)
            self.books[s.label] = book
        return book

    def _store_chosen(self, index: int) -> None:
        if not (0 <= index < len(self.sets)):
            self.book = self.store_set = None
            self.render()
            return
        self.close_form()
        self.store_set = self.sets[index]
        self._observer_cache = self._observer()
        self.book = self._book_of(self.store_set)
        self.refresh_images()
        if not self.book.loaded:
            self.reload()
        else:
            self.render()

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

    def _say(self, message: str) -> None:
        if self.book is not None:
            self.book.status = message
        self._show_status(message)

    def do(self, description: str, work: Callable[[ObservationBook], Any], after: Optional[Callable[[Any], None]] = None) -> Any:
        """
        Runs what changes the book: at once for the local store (its result
        returned), else in a task, one after another (None returned; `after` gets the result).
        """
        book, s = self.book, self.store_set
        if book is None or s is None:
            return None
        if not s.remote:
            result = work(book)
            if after is not None:
                after(result)
            self.render()
            return result
        self._queue.append((description, work, after, book))
        self._next()
        return None

    def _next(self) -> None:
        if self._running or not self._queue:
            return
        description, work, after, book = self._queue.pop(0)
        self._running = True

        def finish(result=None, message: Optional[str] = None) -> None:
            self._running = False
            if message is not None:
                self._say(self.tr("{d}の失敗: {m}").format(d=description, m=message))
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
            raise RuntimeError("観測記録は使用不可")
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
            raise RuntimeError(f"{description}の失敗: {got['error']}")
        return got.get("result")

    def report_html(self, options) -> str:
        """The report with its map drawn (waits for the map; for scripts)."""
        from .obs_report_dialog import ReportMap

        book = self.book
        selection = book.report_selection(options)
        url: dict[str, Optional[str]] = {}
        if options.map and (selection.records or selection.areas):
            loop = QEventLoop()
            kinds = list(dict.fromkeys([*book.vocabulary.kinds, *(t.path[0] for t in book.taxonomy())]))

            def done(data_url, _image) -> None:
                url["map"] = data_url
                loop.quit()

            # Kept until drawn (its job goes with it).
            drawer = ReportMap(self.canvas, selection.records, selection.areas if options.areas else [], kinds, done, self._say)
            drawer.start()
            if not url:
                loop.exec()
        return book.report(options, url.get("map"))

    def api(self) -> ObservationApi:
        """The observations for scripts (plugin.api)."""
        return ObservationApi(lambda: self.book, self.run_now, self.report_html)

    def reload(self) -> None:
        """Reads the records again from the store."""
        self.do(self.tr("観測記録の読み込み"), lambda b: b.reload())

    def reload_taxonomy(self) -> None:
        """Reads the classification again from the store (others may have changed it)."""
        self.do(self.tr("分類の読み込み"), lambda b: b.reload_taxonomy())

    # ---- the images ------------------------------------------------------

    def refresh_images(self) -> None:
        """The project's images again."""
        if self.book is None:
            return
        active = self.iface.activeLayer()
        self.book.set_open_images(open_images(), active.id() if isinstance(active, QgsRasterLayer) else None)
        self.render()

    def _layers_changed(self, *_args) -> None:
        self.refresh_images()

    def _layer_chosen(self, layer) -> None:
        if self._following or self.book is None or not isinstance(layer, QgsRasterLayer):
            return
        if self.book.go_to_layer(layer.id()):
            self._went()

    def _went(self) -> None:
        """Another image: the form closes, the layer is chosen, 「この画像のみ」 follows."""
        self.close_form()
        self.front = None
        image = self.book.current if self.book else None
        if image is not None and image.layer_id:
            layer = QgsProject.instance().mapLayer(image.layer_id)
            if layer is not None and self.iface.activeLayer() is not layer:
                self._following = True
                try:
                    self.iface.setActiveLayer(layer)
                finally:
                    self._following = False
        self._apply_only()
        self.render()

    def step(self, by: int) -> Optional[ObservedImage]:
        """The next (or previous) image in time."""
        if self.book is None:
            return None
        image = self.book.step(by)
        self._went()
        return image

    def go_to(self, image_id: str) -> bool:
        if self.book is None or not self.book.go_to(image_id):
            return False
        self._went()
        return True

    def _image_chosen(self, index: int) -> None:
        if self.book and 0 <= index < len(self.book.images):
            self.book.current = self.book.images[index]
            self._went()

    def _toggle_list_form(self, on: bool) -> None:
        self.list_form.setVisible(on)
        if not on:
            return
        image = self.book.current if self.book else None
        listed = image is not None and not image.file
        self.list_id.setText(image.id if listed else "")
        self.list_name.setText(image.name if listed and image.name != image.id else "")
        self.list_time_known.setChecked(bool(listed and image.time is not None))
        self.list_time.setDateTime(QDateTime.fromMSecsSinceEpoch(image.time) if listed and image.time is not None else QDateTime.currentDateTime())
        self.list_box.setChecked(bool(listed and image.box))
        self.unlist_button.setVisible(listed)
        if self.book:
            self.recorded_model.setStringList([i.id for i in self.book.recorded_images()])
        self.list_id.setFocus()

    def _list_from_form(self) -> None:
        if self.book is None:
            return
        image_id = self.list_id.text().strip()
        known = next((i for i in self.book.recorded_images() if i.id == image_id), None)
        time = _ms_of(self.list_time) if self.list_time_known.isChecked() else (known.time if known else None)
        box = self._view_box() if self.list_box.isChecked() else None
        current = self.book.current
        replacing = current.id if current is not None and not current.file else None
        name = self.list_name.text().strip() or (known.name if known else "")
        if self.book.list_image(image_id, name, time, box, replacing=replacing) is not None:
            self.list_button.setChecked(False)
            self._went()

    def _unlist(self) -> None:
        image = self.book.current if self.book else None
        if image is not None and not image.file and self.book.unlist_image(image.id):
            self.list_button.setChecked(False)
            self._went()

    def _set_image_time(self) -> None:
        image = self.book.current if self.book else None
        if image is None:
            return
        t = _ms_of(self.time_input)
        if image.layer_id:
            layer = QgsProject.instance().mapLayer(image.layer_id)
            if layer is not None:
                set_manual_time(layer, t)
        self.do(self.tr("撮像日時の入力"), lambda b: b.set_image_time(t))

    def _apply_only(self, *_args) -> None:
        """「この画像のみ」: only the current image's layer shows; the others come back as they were."""
        root = QgsProject.instance().layerTreeRoot()
        rasters = [layer for layer in QgsProject.instance().mapLayers().values() if isinstance(layer, QgsRasterLayer)]
        image = self.book.current if self.book else None
        if self.only.isChecked() and image is not None and self.isVisible():
            if self._shown is None:
                self._shown = {}
            for layer in rasters:
                node = root.findLayer(layer.id())
                if node is None:
                    continue
                self._shown.setdefault(layer.id(), node.itemVisibilityChecked())
                node.setItemVisibilityChecked(layer.id() == image.layer_id)
        elif self._shown is not None:
            for layer_id, visible in self._shown.items():
                node = root.findLayer(layer_id)
                if node is not None:
                    node.setItemVisibilityChecked(visible)
            self._shown = None

    def _view_box(self) -> list[float]:
        e = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
        return [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]

    # ---- the modes -------------------------------------------------------

    def _use_tool(self, on: bool) -> None:
        if on:
            if self.canvas.mapTool() is not self.tool:
                self._previous_tool = self.canvas.mapTool()
                self.canvas.setMapTool(self.tool)
        elif self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
            if self._previous_tool is not None:
                self.canvas.setMapTool(self._previous_tool)

    def _tool_gone(self) -> None:
        # Another map tool chosen: recording stops.
        if self.area_mode:
            self.area_button.setChecked(False)
        if self.active:
            self.record_button.setChecked(False)

    def set_active(self, on: bool, quiet: bool = False) -> None:
        """Turns 「クリック記録」 on or off."""
        if on and (self.book is None or self.book.current is None):
            self._say(NO_IMAGE)
            on = False
        if self.record_button.isChecked() != on:
            self.record_button.blockSignals(True)
            self.record_button.setChecked(on)
            self.record_button.blockSignals(False)
        if on == self.active:
            return
        self.active = on
        self.record_button.setText(self.tr("クリック記録中") if on else self.tr("クリック記録"))
        self._use_tool(on or self.area_mode)
        if not on:
            self.front = None
            self.close_form()
        elif not quiet:
            self._say(HINT_TWO if self.two_point else HINT)
        self.draw()

    def set_two_point(self, on: bool) -> None:
        """Turns 「2 点」 on (front then rear: centre, length, heading) or off (a point)."""
        if self.two_point_button.isChecked() != on:
            self.two_point_button.blockSignals(True)
            self.two_point_button.setChecked(on)
            self.two_point_button.blockSignals(False)
        self.two_point = on
        self.front = None
        settings.set_two_point(on)
        if on and not self.active:
            self.set_active(True, quiet=True)
        self._say(HINT_TWO if on else HINT)
        self.draw()

    def set_area_mode(self, on: bool) -> None:
        """Turns 「確認範囲」 on (two corners on the map, the view or the image, then its kind) or off."""
        if on and (self.book is None or self.book.current is None):
            self._say(NO_IMAGE)
            on = False
        if self.area_button.isChecked() != on:
            self.area_button.blockSignals(True)
            self.area_button.setChecked(on)
            self.area_button.blockSignals(False)
        self.area_mode = on
        self.area_ring = None
        self.area_corner = None
        self.area_form.setVisible(on)
        self._use_tool(on or self.active)
        if on:
            self.close_form()
            self.front = None
            self.area_kind.clear()
            self.area_note.clear()
            self.area_image_button.setEnabled(bool(self.book.current.box))
            self._say(HINT_AREA)
        self._render_area_range()
        self.draw()

    def escape(self) -> None:
        if self.area_mode:
            # A corner clicked: that goes first; then the mode.
            if self.area_corner is not None and self.area_ring is None:
                self.area_corner = None
                self._say(HINT_AREA)
                self.draw()
            else:
                self.set_area_mode(False)
        elif self.front is not None:
            self.front = None
            self._say(self.tr("2 点目の取消"))
            self.draw()
        elif self.form.isVisible():
            self.close_form()
        elif self.list_form.isVisible():
            self.list_button.setChecked(False)

    # ---- the map -----------------------------------------------------------

    def map_clicked(self, lon: float, lat: float, mark: Optional[Mark]) -> None:
        """A click on the map while recording (or making an area)."""
        if self.book is None:
            return
        if self.area_mode:
            if self.area_corner is None or self.area_ring is not None:
                self.area_corner = (lon, lat)
                self.area_ring = None
                self._say(self.tr("対角をクリック（Esc で取消）"))
            else:
                (x1, y1), (x2, y2) = self.area_corner, (lon, lat)
                self._set_area_ring(box_ring([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]))
            self.draw()
            return
        if not self.active:
            return
        if self.front is None and mark is not None and mark.key:
            if mark.role == "obs":
                record = self.book.record(mark.key)
                if record is not None:
                    return self.open_form(record.lon, record.lat, editing=record)
            if mark.role == "ghost":
                c = next((x for x in self.book.carried() if x.source.id == mark.key), None)
                if c is not None:
                    return self.open_form(c.source.lon, c.source.lat, source=c.source)
        if not self.two_point:
            return self.open_form(lon, lat)
        # Two points: the front, then the rear.
        if self.front is None:
            self.close_form()
            self.front = (lon, lat)
            self.cursor = None
            self._say(self.tr("後端をクリック（Esc で取消）"))
            self.draw()
            return
        m = two_point(self.front, (lon, lat))
        self.front = None
        self.open_form(m.lon, m.lat, measured=(m.length, m.heading))

    def map_hovered(self, lon: float, lat: float) -> None:
        if self.front is not None or (self.area_mode and self.area_corner is not None and self.area_ring is None):
            self.cursor = (lon, lat)
            self.draw()

    def map_dragged(self, mark: Mark, lon: float, lat: float, done: bool) -> None:
        """A record's mark dragged: its place corrected when let go."""
        if self.book is None or not mark.key:
            return
        if not done:
            self.dragging = (mark.key, lon, lat)
            self.draw()
            return
        self.dragging = None
        key = mark.key
        self.do(self.tr("位置の修正"), lambda b: b.move(key, lon, lat))

    def _extent_changed(self) -> None:
        if self.overlay.isVisible():
            self.overlay.updatePosition()
            self.overlay.update()

    def _set_area_ring(self, ring: list) -> None:
        self.area_ring = ring
        self.area_corner = None
        self._render_area_range()
        self.area_kind.setFocus()

    def _area_view(self) -> None:
        self._set_area_ring(box_ring(self._view_box()))
        self.draw()

    def _area_image(self) -> None:
        image = self.book.current if self.book else None
        if image is not None and image.box:
            self._set_area_ring(box_ring(image.box))
            self.draw()

    def _render_area_range(self) -> None:
        ring = self.area_ring
        if not ring:
            self.area_range.setText(self.tr("範囲: 未指定（地図で 2 点クリック）"))
            return
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        w, e, s, n = min(lons), max(lons), min(lats), max(lats)
        mid = (s + n) / 2
        self.area_range.setText(self.tr("範囲: {w} × {h}").format(w=km_text(get_distance([w, mid], [e, mid])), h=km_text(get_distance([w, s], [w, n]))))

    def _submit_area(self) -> None:
        if not self.area_ring:
            self._say(self.tr("範囲を指定（地図で 2 点クリック、または「表示範囲」「画像の範囲」）"))
            return
        ring, kind, note = self.area_ring, self.area_kind.text(), self.area_note.text()

        def after(area) -> None:
            if area is not None:
                message = self.status.text()
                self.set_area_mode(False)
                self._say(message)

        self.do(self.tr("確認範囲の記録"), lambda b: b.add_area(ring, kind, note), after)

    # ---- the form ----------------------------------------------------------

    def open_form(self, lon: float, lat: float, editing: Optional[Observation] = None, source: Optional[Observation] = None,
                  measured: Optional[tuple] = None) -> None:
        """Opens the form for a new record at a place, a record to change, or a thing carried over."""
        if self.book is None:
            return
        self.pending = (lon, lat, editing, source)
        known = editing or source
        length = measured[0] if measured else (known.length if known else None)
        heading = measured[1] if measured else (known.heading if known else None)
        self.length_input.setText("" if length is None else f"{length:.1f}")
        self.heading_input.setText("" if heading is None else f"{heading:.1f}")
        measuring = self.two_point or length is not None
        for w in (self.length_label, self.length_input, self.heading_label, self.heading_input, self.measure_note):
            w.setVisible(measuring)
        last = self.book.last
        self.object_input.setText(known.object_name if known else "")
        self.kind_input.setText(known.kind if known else (last.kind or (self.book.vocabulary.kinds[0] if self.book.vocabulary.kinds else "")))
        self.activity_input.setText(known.activity if known else last.activity)
        self.note_input.setText(editing.note if editing else "")
        self._fill_confidence((known.confidence or "") if known else last.confidence)
        self.delete_button.setVisible(editing is not None)
        self.absent_button.setVisible(known is not None)
        self.submit_button.setText(self.tr("更新（Enter）") if editing else self.tr("継続（Enter）") if source else self.tr("記録（Enter）"))
        self.form.setTitle(editing.object_name if editing else source.object_name if source else self.tr("新規の記録"))
        self.form.show()
        self._render_chips()
        self._render_kind_path()
        self._render_candidates()
        self.draw()
        # A new thing gets its name typed; a known one is saved with Enter (or 1–9 picks its activity first).
        self.raise_()
        (self.submit_button if known else self.object_input).setFocus()

    def close_form(self) -> None:
        if self.pending is None and not self.form.isVisible():
            return
        self.pending = None
        self.form.hide()
        self.offered = []
        self._clear(self.candidates_layout)
        self.candidates_box.hide()
        self.draw()

    def form_values(self) -> FormValues:
        return FormValues(object=self.object_input.text(), kind=self.kind_input.text(), activity=self.activity_input.text(),
                          note=self.note_input.text(), confidence=self.confidence_select.currentData() or "",
                          length=self.length_input.text(), heading=self.heading_input.text())

    def submit(self, present: bool = True) -> None:
        """Records what the form says (a new record, a change, or a thing carried over)."""
        if self.book is None or self.pending is None:
            return
        lon, lat, editing, source = self.pending
        values = self.form_values()
        # Offered first by the next new record, in later sessions too.
        self.book.remember(values.kind, values.activity if present else None, values.confidence)
        self.close_form()
        self.do(self.tr("記録"), lambda b: b.submit(lon, lat, values, present, editing, source))

    def _delete_pending(self) -> None:
        if self.pending is None or self.pending[2] is None:
            return
        record = self.pending[2]
        self.close_form()
        self.remove([record])

    @staticmethod
    def _clear(layout: FlowLayout) -> None:
        layout.clear()

    def _chip_button(self, text: str, tip: str, run: Callable, pressed: bool = False) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setCheckable(True)
        b.setChecked(pressed)
        b.setAutoRaise(False)
        b.clicked.connect(lambda _on=False: run())
        return b

    def _activity_words(self) -> list[str]:
        if self.book is None:
            return []
        used = [o.activity for o in self.book.living() if o.activity]
        return list(dict.fromkeys([*self.book.vocabulary.activities, *used]))[:9]

    def _render_chips(self) -> None:
        self._clear(self.chips_layout)
        value = self.activity_input.text()
        for i, w in enumerate(self._activity_words()):
            self.chips_layout.addWidget(self._chip_button(f"{i + 1} {w}", f"{w}（{i + 1}）", lambda w=w: self._pick_activity(w), value == w))

    def _pick_activity(self, word: str) -> None:
        self.activity_input.setText(word)
        self._render_chips()

    def _chip(self, n: int) -> None:
        words = self._activity_words()
        if self.form.isVisible() and 0 < n <= len(words):
            self._pick_activity(words[n - 1])

    def _kind_changed(self) -> None:
        """The kind typed or chosen; for a new record, also what the next new record starts with."""
        if self.book is not None and self.pending is not None and self.pending[2] is None and self.pending[3] is None:
            self.book.remember(kind=self.kind_input.text())
        self._render_kind_path()
        self._render_candidates()

    def _render_kind_path(self) -> None:
        """The kind's levels: the path so far (a click goes back up), then the classes below it and its known individuals."""
        self._clear(self.kind_path_layout)
        if self.book is None:
            return
        taxa = self.book.taxonomy()
        path = path_of(self.kind_input.text())

        def choose(p: list[str]) -> None:
            self.kind_input.setText(kind_of(p))
            self._kind_changed()

        for i, name in enumerate(path):
            if i:
                self.kind_path_layout.addWidget(QLabel("›"))
            tip = f"{level_name(taxa, path, i) or f'第 {i + 1} 階層'}（クリックでこの階層まで）"
            self.kind_path_layout.addWidget(self._chip_button(name, tip, lambda p=path[:i + 1]: choose(p), i == len(path) - 1))
        below = children_of(taxa, path)[:24]
        if below:
            self.kind_path_layout.addWidget(QLabel(f"{level_name(taxa, path, len(path)) or ('下位' if path else '大分類')}:"))
            for t in below:
                self.kind_path_layout.addWidget(self._chip_button(t.path[-1], f"{kind_of(t.path)} を選択", lambda p=t.path: choose(list(p))))
        label = name_label_of(taxa, path)
        here = find(taxa, path)
        members = (here.members if here else None) or []
        if members:
            self.kind_path_layout.addWidget(QLabel(f"{label or '個体'}:"))
            typed = self.object_input.text().strip()
            for m in members[:24]:
                self.kind_path_layout.addWidget(self._chip_button(m, f"{m} として記録", lambda m=m: (self.object_input.setText(m), self._object_typed()), typed == m))
        self.kind_path_box.setVisible(self.kind_path_layout.count() > 0)
        self.object_label.setText(f"モノ（{label}）" if label else "モノ")
        self._render_specs(taxa, path)
        self._render_measure()

    def _render_specs(self, taxa, path: list[str]) -> None:
        specs = specs_at(taxa, path) if path else {}
        fields = {f.name: f for f in self.book.spec_fields}
        order = [*[f.name for f in self.book.spec_fields], *[k for k in specs if k not in fields]]
        text = "・".join(f"{k} {spec_text(fields.get(k), specs[k][0])}" for k in order if k in specs)
        self.specs_label.setText(text)
        self.specs_label.setVisible(bool(text))

    def _render_measure(self) -> None:
        """The measured length against the class's 全長, when both are known."""
        import re

        try:
            length = float(self.length_input.text())
        except ValueError:
            length = 0
        path = path_of(self.kind_input.text())
        spec = specs_at(self.book.taxonomy(), path).get("全長") if self.book and path else None
        try:
            full = float(re.sub(r"[^\d.]", "", spec[0])) if spec else 0
        except ValueError:
            full = 0
        self.measure_note.setText(f"全長 {spec[0]} m（{spec[1][-1]}）の {js_round(length / full * 100)}%" if length > 0 and full > 0 else "")

    def _object_typed(self) -> None:
        """A known thing's name typed: its kind and latest activity come with it; a known individual's name brings its class."""
        if self.book is None or not self.form.isVisible():
            return
        name = self.object_input.text().strip()
        flow = self.book.flow_by_name(name)
        if flow is None:
            member = member_index(self.book.taxonomy()).get(name)
            if member:
                self.kind_input.setText(kind_of(member))
                self._render_kind_path()
            self._render_candidates()
            return
        self.kind_input.setText(flow.kind)
        self._render_kind_path()
        if not self.activity_input.text():
            self.activity_input.setText(flow.activity)
        self._render_chips()
        self._render_candidates()

    def _number(self, edit: QLineEdit) -> Optional[float]:
        try:
            return float(edit.text()) if edit.text().strip() else None
        except ValueError:
            return None

    def _render_candidates(self) -> None:
        """The things offered as the same one, under 「モノ」 in a new record's form."""
        self._clear(self.candidates_layout)
        p = self.pending
        if self.book is None or p is None or p[2] is not None or p[3] is not None:
            self.offered = []
            self.candidates_box.hide()
            return
        self.offered = self.book.candidates(p[0], p[1], self.kind_input.text(), self._number(self.length_input), self._number(self.heading_input))
        typed = self.object_input.text().strip()
        self.candidates_layout.addWidget(QLabel(self.tr("同一候補:")))
        for i, c in enumerate(self.offered):
            tip = f"{c.kind or '種別なし'}・{'・'.join(c.reasons)}・{local_time(c.source.image_time)} の記録（Alt+{i + 1}）"
            self.candidates_layout.addWidget(self._chip_button(f"{i + 1} {c.name}", tip, lambda c=c: self.pick_candidate(c), typed == c.name))
        self.candidates_box.setVisible(bool(self.offered))

    def pick_candidate(self, c: MatchCandidate) -> None:
        """A candidate chosen: the record is of that thing (its kind and activity come with it)."""
        self.object_input.setText(c.name)
        self._object_typed()
        self.submit_button.setFocus()

    def _candidate_key(self, n: int) -> None:
        if self.form.isVisible() and 0 < n <= len(self.offered):
            self.pick_candidate(self.offered[n - 1])

    def _fill_confidence(self, value: str) -> None:
        self.confidence_select.blockSignals(True)
        self.confidence_select.clear()
        self.confidence_select.addItem(self.tr("確度なし"), "")
        levels = self.book.confidences() if self.book else []
        for c in levels:
            self.confidence_select.addItem(c.label, c.label)
            if c.note:
                self.confidence_select.setItemData(self.confidence_select.count() - 1, c.note, Qt.ItemDataRole.ToolTipRole)
        if value and not any(c.label == value for c in levels):
            self.confidence_select.addItem(value, value)
        self.confidence_select.setCurrentIndex(max(0, self.confidence_select.findData(value)))
        self.confidence_select.blockSignals(False)
        self._render_confidence()

    def _render_confidence(self) -> None:
        value = self.confidence_select.currentData()
        level = next((c for c in (self.book.confidences() if self.book else []) if c.label == value), None)
        self.confidence_select.setToolTip(level.note or "" if level else "")
        self.confidence_select.setStyleSheet(f"QComboBox {{ border-left: 4px solid {level.color}; }}" if level and level.color else "")

    # ---- what the book does, from the dock ---------------------------------

    def ask_clear(self, book: ObservationBook) -> Optional[bool]:
        """Asks what to clear: True only the deleted, False everything, None nothing."""
        living_records = len(book.living())
        gone = len(book.records) - living_records
        areas = len(book.areas)
        text = self.tr("{store} の記録を完全に消去（元に戻せません）。\n\n記録 {n} 件（うち削除済み {d} 件）、確認範囲 {a} 件。"
                       "\n分類体系・参考写真は残ります。").format(store=book.label, n=len(book.records), d=gone, a=areas)
        box = QMessageBox(QMessageBox.Icon.Warning, self.tr("ローカル記録の消去"), text, parent=self)
        everything = box.addButton(self.tr("すべて消去"), QMessageBox.ButtonRole.DestructiveRole)
        deleted = box.addButton(self.tr("削除済みのみ消去"), QMessageBox.ButtonRole.AcceptRole)
        deleted.setEnabled(gone > 0 or any(a.deleted_at is not None for a in book.areas))
        cancel = box.addButton(self.tr("取消"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        return False if clicked is everything else True if clicked is deleted else None

    def clear_local(self, deleted_only: Optional[bool] = None) -> Optional[tuple[int, int]]:
        """Removes the local records for good, after asking (`deleted_only` given: without asking)."""
        book = self.book
        if book is None or self.store_set is None or self.store_set.remote or not book.can_clear():
            return None
        if deleted_only is None:
            deleted_only = self.ask_clear(book)
            if deleted_only is None:
                return None
        self.close_form()
        self.chosen = None
        result = self.do(self.tr("ローカル記録の消去"), lambda b: b.clear(deleted_only))
        self.refresh_images()
        return result

    def carry(self, object_id: str, present: bool = True) -> None:
        self.do(self.tr("継続の記録"), lambda b: b.carry(object_id, present))

    def carry_all(self) -> None:
        self.do(self.tr("すべて継続"), lambda b: b.carry_all())

    def absent_inside(self) -> None:
        self.do(self.tr("確認範囲内の不在"), lambda b: b.absent_inside())

    def remove(self, rows: list[Observation]) -> None:
        self.do(self.tr("記録の削除"), lambda b: b.remove(rows))

    def restore(self, rows: list[Observation]) -> None:
        self.do(self.tr("記録の復元"), lambda b: b.restore(rows))

    def remove_areas(self, rows: list[CheckedArea]) -> None:
        self.do(self.tr("確認範囲の削除"), lambda b: b.remove_areas(rows))

    def restore_areas(self, rows: list[CheckedArea]) -> None:
        self.do(self.tr("確認範囲の復元"), lambda b: b.restore_areas(rows))

    def choose(self, object_id: Optional[str]) -> None:
        """Shows a thing's flow under 「モノ」 (None: none)."""
        self.chosen = object_id
        self.flow_more.setChecked(False)
        if object_id and self.tab != "objects":
            self.tabs.setCurrentIndex(1)
        else:
            self.render()

    def filter_class(self, path: Optional[list[str]]) -> None:
        """Limits 「モノ」 to a class and below (None: all)."""
        self.class_filter = list(path) if path else None
        if self.class_filter and self.tab != "objects":
            self.tabs.setCurrentIndex(1)
        else:
            self.render()

    def _chosen_flow(self) -> Optional[ObjectFlow]:
        return self.book.flow(self.chosen) if self.book and self.chosen else None

    def _rename_chosen(self) -> None:
        flow = self._chosen_flow()
        if flow is None:
            return
        name = self.rename_input.text().strip() or flow.name
        kind = self.rekind_input.text().strip()
        self.do(self.tr("名前の変更"), lambda b: b.rename(flow.object_id, name, kind_of(path_of(kind)) if kind else None))

    def _merge_chosen(self) -> None:
        flow = self._chosen_flow()
        into = self.merge_select.currentData()
        if flow is None or not into:
            return
        self.chosen = into
        self.do(self.tr("統合"), lambda b: b.merge(flow.object_id, into))

    def _remove_chosen(self) -> None:
        flow = self._chosen_flow()
        if flow is None:
            return
        # Two steps, so one click never deletes a thing's records.
        if not self.remove_all_button.property("armed"):
            self.remove_all_button.setProperty("armed", True)
            self.remove_all_button.setText(self.tr("再度押すと削除"))
            return
        self.remove(list(flow.observations))

    def _fit(self, points: list, buffer_m: float = 200) -> None:
        if not points:
            return
        transform = from_wgs84(self.canvas.mapSettings().destinationCrs())
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        pad = buffer_m / 111_320
        rect = QgsRectangle(min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)
        try:
            self.canvas.setExtent(transform.transformBoundingBox(rect))
        except Exception:  # noqa: BLE001
            return
        self.canvas.refresh()

    def _fit_flow(self, flow: Optional[ObjectFlow]) -> None:
        if flow:
            self._fit([(o.lon, o.lat) for o in flow.observations])

    def _fit_area(self, area: CheckedArea) -> None:
        self._fit(area.ring, 0)

    def _center(self, o: Observation) -> None:
        transform = from_wgs84(self.canvas.mapSettings().destinationCrs())
        try:
            at = transform.transform(o.lon, o.lat)
        except Exception:  # noqa: BLE001
            return
        extent = self.canvas.extent()
        inner = extent.buffered(-min(extent.width(), extent.height()) * 0.1)
        if not inner.contains(at):
            self.canvas.setCenter(at)
            self.canvas.refresh()

    def open_record(self, o: Observation) -> None:
        """Goes to a record's image and opens it in the form."""
        if self.book is None:
            return
        entry = next((e for e in self.book.images if e.id == o.image_id), None)
        if entry is not None and entry is not self.book.current:
            self.book.current = entry
            self._went()
        self._center(o)
        if entry is None:
            self._say(self.tr("画像（{n}）が開かれておらず移動不可").format(n=o.image_name or o.image_id))
            return
        if not self.active:
            self.set_active(True)
        self.open_form(o.lon, o.lat, editing=o)

    # ---- the classification ------------------------------------------------

    def _apply_taxon(self) -> None:
        path = self.taxon
        if not path:
            return
        parent = path_of(self.taxon_parent.currentData() or "")
        own = {"levels": _split(self.taxon_levels.text()), "name_label": self.taxon_label.text().strip(),
               "members": _split(self.taxon_members.text())}
        name = self.taxon_name.text()

        def after(to) -> None:
            if to:
                self.taxon = to
                if self.class_filter and within(kind_of(self.class_filter), path):
                    self.class_filter = None

        self.do(self.tr("分類の変更"), lambda b: b.update_class(path, name=name, parent=parent, **own), after)

    def _delete_taxon(self) -> None:
        path = self.taxon
        if not path:
            return
        if not self.taxon_delete.property("armed"):
            self.taxon_delete.setProperty("armed", True)
            self.taxon_delete.setText(self.tr("再度押すと削除"))
            return
        self.taxon = path[:-1] or None
        if self.class_filter and within(kind_of(self.class_filter), path):
            self.class_filter = None
        self.do(self.tr("分類の削除"), lambda b: b.remove_class(path))

    def _add_child(self) -> None:
        name = self.taxon_child.text().strip()
        if not name or not self.taxon:
            return
        path = [*self.taxon, *path_of(name)]
        self.taxon = path
        self.taxon_child.clear()
        self.do(self.tr("分類の追加"), lambda b: b.add_class(path))

    def _add_root(self) -> None:
        path = path_of(self.taxon_root.text())
        if not path:
            return
        self.taxon = path
        self.taxon_root.clear()
        self.do(self.tr("分類の追加"), lambda b: b.add_class(path))

    def save_taxonomy_file(self) -> None:
        """The classification as `config.json`'s `observationVocabulary.taxonomy` writes it."""
        if self.book is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, self.tr("分類体系の JSON 保存"), "observation-taxonomy.json", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(nest(self.book.taxonomy()), ensure_ascii=False, indent=2) + "\n")
        self._say(self.tr("分類体系を保存: {p}").format(p=path))

    def load_taxonomy_file(self, path: Optional[str] = None) -> None:
        if self.book is None:
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, self.tr("分類体系の JSON 読み込み"), "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as f:
                value = json.load(f)
            items = value if isinstance(value, list) else value.get("taxonomy") if isinstance(value, dict) else None
            if not isinstance(items, list):
                raise ValueError("分類の配列なし")
        except (OSError, ValueError) as error:
            self._say(self.tr("分類の読み込み失敗: {e}").format(e=error))
            return
        self.do(self.tr("分類の読み込み"), lambda b: b.set_taxonomy(items))

    # ---- saving --------------------------------------------------------------

    def save_csv(self, path: Optional[str] = None) -> Optional[str]:
        """Saves the records as CSV (the deleted ones too when shown)."""
        if self.book is None:
            return None
        rows = self.book.records if self.deleted_check.isChecked() else self.book.living()
        if not rows:
            self._say(self.tr("記録なし"))
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr("CSV 保存"), csv_file_name(now_ms()), "CSV (*.csv)")
        if not path:
            return None
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(observations_csv(rows))
        self._say(self.tr("CSV を保存: {p}").format(p=path))
        return path

    def to_layers(self) -> list:
        """Adds the records as a point layer, the checked areas as polygons and the flows as lines."""
        if self.book is None:
            return []
        made = observation_layers(self.book.records, self.book.areas)
        if not made:
            self._say(self.tr("記録なし"))
            return []
        layers = [observation_memory_layer(m) for m in made]
        QgsProject.instance().addMapLayers(layers)
        self._say(self.tr("{n} をレイヤーとして追加").format(n="・".join(m.title for m in made)))
        return layers

    def open_report(self) -> None:
        if self.book is None:
            return
        from .obs_report_dialog import ReportDialog

        ReportDialog(self, self.book, self.canvas, self).exec()

    # ---- showing ---------------------------------------------------------------

    def _tab_changed(self, index: int) -> None:
        self.tab = ("image", "objects", "classes")[index]
        self.render()

    def _set_head(self, *names: str) -> None:
        self.table.clear()
        self.table.setColumnCount(len(names))
        self.table.setHorizontalHeaderLabels(list(names))
        self.table.setRowCount(0)
        self._row_actions = []

    def _row(self, cells: list, on_click: Optional[Callable] = None, on_double: Optional[Callable] = None,
             style: str = "", widget: Optional[QWidget] = None, selected: bool = False) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for i, c in enumerate(cells):
            item = QTableWidgetItem("" if c is None else str(c))
            if style == "deleted":
                font = item.font()
                font.setStrikeOut(True)
                item.setFont(font)
                item.setForeground(QColor("#888888"))
            elif style == "ghost":
                item.setForeground(QColor("#555555"))
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            elif style == "inside":
                item.setForeground(QColor("#0f766e"))
            elif style == "area":
                item.setForeground(QColor("#0f766e"))
            self.table.setItem(r, i, item)
        if widget is not None:
            self.table.setCellWidget(r, self.table.columnCount() - 1, widget)
        self._row_actions.append((on_click, on_double))
        if selected:
            self.table.selectRow(r)

    def _row_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._row_actions) and self._row_actions[row][0]:
            self._row_actions[row][0]()

    def _row_double_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._row_actions) and self._row_actions[row][1]:
            self._row_actions[row][1]()

    def _small(self, *buttons: tuple) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        for text, tip, slot in buttons:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            lay.addWidget(b)
        return box

    def render(self) -> None:
        """Shows the book as it is now."""
        book = self.book
        image = book.current if book else None
        # The images.
        self.image_select.blockSignals(True)
        self.image_select.clear()
        for e in book.images if book else []:
            self.image_select.addItem(f"{local_time(e.time) if e.time is not None else '日時不明'}  {e.name}{'' if e.file else '（ファイルなし）'}")
        if book and image in book.images:
            self.image_select.setCurrentIndex(book.images.index(image))
        self.image_select.blockSignals(False)
        many = bool(book and len(book.images) > 1)
        self.prev_button.setEnabled(many)
        self.next_button.setEnabled(many)
        self.time_row.setVisible(image is not None and image.time is None)
        self.clear_button.setEnabled(bool(book and self.store_set and not self.store_set.remote and book.can_clear()))
        if book is None:
            self._set_head(self.tr("観測記録"))
            self._row([self.tr("保存先なし")])
            self.draw()
            return
        if not self.status.text() and book.status:
            self.status.setText(book.status)
        carried = book.carried()
        inside = book.inside_carried(carried)
        self.carry_label.setText(self.tr("前後の画像のモノ {n} 件").format(n=len(carried)) if carried else "")
        self.carry_all_button.setVisible(len(carried) > len(inside))
        self.absent_inside_button.setVisible(bool(inside))
        self.absent_inside_button.setText(self.tr("確認範囲内 {n} 件を不在").format(n=len(inside)))
        shown = book.records if self.deleted_check.isChecked() else book.living()
        here = sorted((o for o in shown if image and o.image_id == image.id), key=lambda o: o.object_name)
        objects = book.flows()
        taxa = book.taxonomy()
        self.tabs.setTabText(0, self.tr("この画像 {n}").format(n=len([o for o in here if o.deleted_at is None])))
        self.tabs.setTabText(1, self.tr("モノ {n}").format(n=len(objects)))
        self.tabs.setTabText(2, self.tr("分類 {n}").format(n=len(taxa)))
        # What the form offers.
        self.objects_model.setStringList(list(dict.fromkeys(f.name for f in objects if f.name)))
        kinds = list(dict.fromkeys(k for k in [*book.vocabulary.kinds, *(kind_of(t.path) for t in taxa), *(f.kind for f in objects)] if k))
        self.kinds_model.setStringList(kinds)
        self.area_kinds_model.setStringList(kinds)
        self.activities_model.setStringList(list(dict.fromkeys(a for a in [*book.vocabulary.activities, *(o.activity for o in book.living())] if a)))
        self.class_filter_row.setVisible(self.tab == "objects" and bool(self.class_filter))
        self.class_filter_label.setText(self.tr("分類: {k} 以下").format(k=kind_of(self.class_filter)) if self.class_filter else "")
        if self.tab == "image":
            self._render_image(here, carried, inside)
        elif self.tab == "objects":
            self._render_objects(objects)
        else:
            self._render_classes(taxa, objects)
        self._render_flow()
        self._render_taxon(taxa, objects)
        self.changed.emit()
        self.draw()

    def _render_image(self, here: list[Observation], carried: list[CarryOver], inside: list[CarryOver]) -> None:
        tr = self.tr
        self._set_head(tr("モノ"), tr("種別"), tr("活動"), tr("長さ・向き"), tr("備考"), tr("更新"), "")
        book = self.book

        def measure(o: Observation) -> str:
            return f"{km_text(o.length)}・{heading_text(o.heading)}" if o.length is not None else ""

        for o in here:
            gone = o.deleted_at is not None
            widget = self._small((tr("復元"), tr("削除した記録の復元"), lambda o=o: self.restore([o]))) if gone else \
                self._small((tr("流れ"), tr("このモノの流れの表示"), lambda o=o: self.choose(o.object_id)))
            self._row([o.object_name, o.kind, o.activity if o.present else tr("不在"), measure(o), o.note, local_time(o.updated_at), ""],
                      None if gone else (lambda o=o: (self._ensure_active(), self.open_form(o.lon, o.lat, editing=o), self._center(o))),
                      style="deleted" if gone else "", widget=widget)
        inside_ids = {id(c) for c in inside}
        for c in carried:
            s = c.source
            widget = self._small((tr("継続"), tr("この画像でも継続として記録"), lambda s=s: self.carry(s.object_id, True)),
                                 (tr("不在"), tr("この画像での不在を記録"), lambda s=s: self.carry(s.object_id, False)))
            side = tr("前") if c.side == "before" else tr("後")
            self._row([s.object_name, s.kind, f"{s.activity}（{side}の画像 {local_time(s.image_time)}）", measure(s),
                       tr("確認範囲内（記録なし）") if id(c) in inside_ids else "", "", ""],
                      lambda s=s: (self._ensure_active(), self.open_form(s.lon, s.lat, source=s), self._center(s)),
                      style="inside" if id(c) in inside_ids else "ghost", widget=widget)
        for a in book.image_areas(deleted=self.deleted_check.isChecked()):
            gone = a.deleted_at is not None
            widget = self._small((tr("復元"), tr("削除した確認範囲の復元"), lambda a=a: self.restore_areas([a]))) if gone else \
                self._small((tr("削除"), tr("この確認範囲の削除"), lambda a=a: self.remove_areas([a])))
            self._row([area_label(a), a.kind, "", "", a.note, local_time(a.updated_at), ""], lambda a=a: self._fit_area(a),
                      style="deleted" if gone else "area", widget=widget)
        if self.table.rowCount() == 0:
            self._row([tr("記録なし（「クリック記録」で地図をクリック）") if book.current else tr("画像なし")])

    def _ensure_active(self) -> None:
        if not self.active:
            self.set_active(True, quiet=True)

    def _render_objects(self, objects: list[ObjectFlow]) -> None:
        tr = self.tr
        self._set_head(tr("モノ"), tr("種別"), tr("最新"), tr("初出"), tr("最終"), tr("画像"))
        q = self.filter_input.text().strip().lower()
        within_ = self.class_filter
        listed = [f for f in objects if within(f.kind, within_)] if within_ else objects
        if q:
            listed = [f for f in listed if any(q in v.lower() for v in [f.name, f.object_id, f.kind, *(o.activity for o in f.observations)])]
        for f in listed:
            self._row([f.name, f.kind, f.activity if f.present else tr("不在"), local_time(f.first), local_time(f.last), f.seen],
                      lambda f=f: self.choose(None if self.chosen == f.object_id else f.object_id), lambda f=f: self._fit_flow(f),
                      selected=f.object_id == self.chosen)
        if self.table.rowCount() == 0:
            self._row([tr("該当するモノなし") if objects else tr("記録なし")])

    def _render_classes(self, taxa, objects: list[ObjectFlow]) -> None:
        tr = self.tr
        self._set_head(tr("分類"), tr("階層"), tr("モノ"), tr("記録"))
        records = self.book.living()
        for t in taxa:
            depth = len(t.path) - 1
            name = f"{'　' * depth}{'└ ' if depth else ''}{t.path[depth]}"
            things = sum(1 for f in objects if within(f.kind, t.path))
            count = sum(1 for o in records if within(o.kind, t.path))
            same = bool(self.taxon) and kind_of(self.taxon) == kind_of(t.path)
            self._row([name, level_name(taxa, t.path, depth), things, count],
                      lambda t=t: self._choose_taxon(None if self.taxon and kind_of(self.taxon) == kind_of(t.path) else list(t.path)),
                      lambda t=t: self.filter_class(list(t.path)), selected=same)
        if self.table.rowCount() == 0:
            self._row([tr("分類なし（下の「大分類の追加」から）")])

    def _choose_taxon(self, path: Optional[list[str]]) -> None:
        self.taxon = path
        self.render()

    def _render_flow(self) -> None:
        book = self.book
        rows = [o for o in book.living() if o.object_id == self.chosen] if self.chosen else []
        show = self.tab == "objects" and bool(rows)
        self.flow_box.setVisible(show)
        if not show:
            return
        flow = flow_of(rows)
        tr = self.tr
        self.flow_box.setTitle(f"{flow.name}（{flow.kind or tr('種別なし')}・{flow.object_id}）")
        latest = (flow.activity or tr("活動の記録なし")) if flow.present else tr("不在")
        self.flow_summary.setText(f"{local_time(flow.first)} 〜 {local_time(flow.last)}・{flow.seen} 画像で確認・移動 {km_text(flow.distance)}・最新: {latest}")
        self.flow_strip.set_flow(flow)
        self.steps_table.setRowCount(0)
        self._steps = flow.steps
        for s in flow.steps:
            r = self.steps_table.rowCount()
            self.steps_table.insertRow(r)
            cells = [local_time(s.start) if s.start == s.end else f"{local_time(s.start)} 〜 {local_time(s.end)}",
                     (s.activity or "—") if s.present else tr("不在"), str(s.count), km_text(s.moved) if s.moved is not None and s.moved > 0 else ""]
            for i, c in enumerate(cells):
                item = QTableWidgetItem(c)
                if not s.present:
                    item.setForeground(QColor("#dc2626"))
                self.steps_table.setItem(r, i, item)
        if self.flow_box.property("object") != flow.object_id:
            self.flow_box.setProperty("object", flow.object_id)
            self.rename_input.setText(flow.name)
            self.rekind_input.setText(flow.kind)
            self.remove_all_button.setProperty("armed", False)
            self.remove_all_button.setText(tr("このモノの記録を削除"))
        self.merge_select.clear()
        self.merge_select.addItem(tr("統合先のモノ…"), "")
        for f in book.flows():
            if f.object_id != flow.object_id:
                self.merge_select.addItem(f"{f.name}（{f.kind}・{local_time(f.last)}）", f.object_id)

    def _step_clicked(self, row: int, _col: int) -> None:
        steps = getattr(self, "_steps", [])
        if 0 <= row < len(steps):
            self.open_record(steps[row].observations[0])

    def _render_taxon(self, taxa, objects: list[ObjectFlow]) -> None:
        classes = self.tab == "classes"
        self.taxa_box.setVisible(classes)
        path = self.taxon if self.taxon and find(taxa, self.taxon) else None
        self.taxon_box.setVisible(classes and path is not None)
        if not classes:
            return
        self.taxa_note.setText(self.tr("保存先: {s}・最大 {d} 階層（行のダブルクリックでその分類のモノ一覧）").format(
            s=self.book.taxonomy_store.label, d=depth_of(taxa)))
        if path is None:
            return
        t = find(taxa, path)
        things = sum(1 for f in objects if within(f.kind, path))
        self.taxon_box.setTitle(f"{kind_of(path)}（{level_name(taxa, path, len(path) - 1) or f'第 {len(path)} 階層'}・モノ {things}）")
        if self.taxon_box.property("path") != kind_of(path):
            self.taxon_box.setProperty("path", kind_of(path))
            self.taxon_name.setText(path[-1])
            self.taxon_levels.setText("、".join(t.levels or []))
            self.taxon_label.setText(t.name_label or "")
            self.taxon_members.setText("、".join(t.members or []))
            self.taxon_child.clear()
            self.taxon_delete.setProperty("armed", False)
            self.taxon_delete.setText(self.tr("削除"))
        self.taxon_child.setPlaceholderText(f"{level_name(taxa, path, len(path)) or '下位分類'}の名前")
        self.taxon_parent.clear()
        self.taxon_parent.addItem(self.tr("（なし: 大分類）"), "")
        for x in taxa:
            if not within(kind_of(x.path), path):
                self.taxon_parent.addItem(kind_of(x.path), kind_of(x.path))
        self.taxon_parent.setCurrentIndex(max(0, self.taxon_parent.findData(kind_of(path[:-1]))))

    # ---- the overlay ---------------------------------------------------------

    def scene(self) -> Scene:
        """What is drawn: this image's records, the things carried over, the chosen flow, the areas and the clicked place."""
        s = Scene()
        book = self.book
        if book is None:
            return s
        image = book.current
        kinds = list(dict.fromkeys([*book.vocabulary.kinds, *(t.path[0] for t in book.taxonomy()), *((path_of(f.kind) or [""])[0] for f in book.flows())]))
        levels = book.confidences()
        ring_of = {c.label: c.color for c in levels if c.color}
        surest = levels[0].label if levels else None

        def color(kind: str) -> str:
            return color_of((path_of(kind) or [""])[0], kinds)

        if self.chosen and self.tab == "objects":
            rows = [o for o in book.living() if o.object_id == self.chosen]
            if rows:
                flow = flow_of(rows)
                seen = [o for o in flow.observations if o.present]
                if len(seen) > 1:
                    s.track = [(o.lon, o.lat) for o in seen]
                for o in flow.observations:
                    s.marks.append(Mark("step", o.lon, o.lat, label=f"{local_time(o.image_time)[5:]}{'' if o.present else '（不在）'}", present=o.present))
        if image is not None:
            s.areas = [(a.ring, area_label(a)) for a in book.image_areas()]
            for c in book.carried():
                o = c.source
                s.marks.append(Mark("ghost", o.lon, o.lat, color(o.kind), o.object_name, ends=ends_of(o.lon, o.lat, o.length, o.heading),
                                    heading=o.heading, key=o.id))
            editing = self.pending[2].id if self.pending and self.pending[2] else None
            for o in book.living():
                if o.image_id != image.id:
                    continue
                lon, lat = (self.dragging[1], self.dragging[2]) if self.dragging and self.dragging[0] == o.id else (o.lon, o.lat)
                what = (f"・{o.activity}" if o.activity else "") if o.present else "（不在）"
                sure = f"（{o.confidence}）" if o.confidence and o.confidence != surest else ""
                s.marks.append(Mark("obs", lon, lat, color(o.kind), f"{o.object_name}{what}{sure}", o.present, ring_of.get(o.confidence or ""),
                                    ends=ends_of(lon, lat, o.length, o.heading), heading=o.heading, editing=o.id == editing, key=o.id))
        p = self.pending
        if p and not p[2]:
            s.pending = (p[0], p[1])
        if self.area_mode:
            if self.area_ring:
                s.area_pending = self.area_ring
            elif self.area_corner and self.cursor:
                (x1, y1), (x2, y2) = self.area_corner, self.cursor
                s.area_pending = box_ring([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)])
            elif self.area_corner:
                s.pending = self.area_corner
        if self.front:
            s.front = self.front
            if self.cursor:
                s.cursor = self.cursor
                m = two_point(self.front, self.cursor)
                s.rubber_label = f"{km_text(m.length)}・{heading_text(m.heading)}"
        return s

    def draw(self) -> None:
        if not hasattr(self, "overlay"):
            return
        self.overlay.set_scene(self.scene())
        self.overlay.setVisible(self.isVisible())

    def _visibility(self, visible: bool) -> None:
        if visible:
            self.refresh_images()
            return
        if self.area_mode:
            self.set_area_mode(False)
        self.set_active(False)
        self.close_form()
        if self.only.isChecked():
            self.only.setChecked(False)
        self._apply_only()
        self.overlay.hide()

    def close_down(self) -> None:
        """Called when the plugin unloads."""
        self._visibility(False)
        self._use_tool(False)
        project = QgsProject.instance()
        for signal, slot in ((project.layersAdded, self._layers_changed), (project.layersRemoved, self._layers_changed),
                             (self.iface.currentLayerChanged, self._layer_chosen), (self.canvas.extentsChanged, self._extent_changed),
                             (self.context.configChanged, self.reset_stores)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        scene = self.canvas.scene()
        if scene is not None:
            scene.removeItem(self.overlay)
