"""
The form of one imaging request (撮像依頼の登録・編集): name, 依頼グループ, category,
period and repeat, priority (five levels, or 1–1000 behind 「詳細」),
the conditions on satellites, and the area: drawn on the map as a box,
clicked as a point with a size, or taken from the selected feature.

It stays open beside the map (not modal) so the area can be drawn.

Given `batch` (「レイヤーから取り込み」), the names and areas (and the
管理番号・識別番号 when attributes give them) are the layer's features: the
form then asks only the rest, once for all of them.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Callable, Optional

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsVectorLayer
from qgis.gui import QgsMapToolEmitPoint, QgsMapToolExtent
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..core.request.pick import Picked, checks_of, picked, summary, unknown
from ..core.request.model import (
    ACHIEVES, CATEGORIES, KINDS, LOOK_SIDES, ORBIT_DIRS, PRIORITY_LEVELS, STATUSES, TIMES_OF_DAY, ImagingRequest, bbox_of,
    day_bounds, jst_day, level_of, priority_text, repeat_of, repeat_text, square_rings,
)
from ..qgis_io.geometry import shape_of, to_wgs84
from .docks import plain_label
from .last_inputs import InputMemory
from .plan_overlay import PlanOverlay
from .request_groups import NO_GROUP_TEXT
from .satellite_tree import SatelliteTree

_WEEKDAYS = "月火水木金土日"


def _optional(low: float, high: float, suffix: str, decimals: int = 1) -> QDoubleSpinBox:
    """A number that may be left out (its lowest value reads 「指定なし」)."""
    box = QDoubleSpinBox()
    box.setRange(low - 1, high)
    box.setDecimals(decimals)
    box.setSpecialValueText("指定なし")
    box.setValue(low - 1)
    box.setSuffix(suffix)
    return box


def _get(box: QDoubleSpinBox) -> Optional[float]:
    return None if box.value() <= box.minimum() else box.value()


def _set(box: QDoubleSpinBox, v: Optional[float]) -> None:
    box.setValue(box.minimum() if v is None else v)


def _words(text: str) -> list:
    return [w.strip() for w in text.replace("、", ",").split(",") if w.strip()]


class RequestForm(QDialog):
    """Adds or changes one request; `save(request)` is called with it on 「保存」."""

    def __init__(self, iface, request: ImagingRequest, save: Callable[[ImagingRequest], None],
                 known: Optional[dict] = None, parent=None, satellites: Optional[Callable] = None,
                 batch: Optional[dict] = None):
        super().__init__(parent or iface.mainWindow())
        #: 「レイヤーから取り込み」: {"items": [(name, rings, 範囲 / 点)], "text": what they are}; None for one request.
        self.batch = batch
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.request = request
        self.save = save
        self.rings = [list(r) for r in request.rings]
        self._note = ""
        self._closing = False
        self.overlay = PlanOverlay(self.canvas)
        if batch is not None:
            self.setWindowTitle(batch.get("title") or self.tr("レイヤーから取り込み（{n} 件）").format(n=len(batch["items"])))
        else:
            self.setWindowTitle(self.tr("撮像依頼の編集") if request.object_id is not None else self.tr("撮像依頼の登録"))
        self.setModal(False)
        self._build(known or {})
        self._fill(request)
        self._fit()
        self.extent_tool = QgsMapToolExtent(self.canvas)
        self.extent_tool.extentChanged.connect(self._extent_drawn)
        self.point_tool = QgsMapToolEmitPoint(self.canvas)
        self.point_tool.canvasClicked.connect(self._point_clicked)
        self.finished.connect(self._closed)
        #: The size of a clicked point's area last used (入力の初期値の記憶).
        self.memory = InputMemory("request_form", {"size": self.size})
        self.memory.restore()
        if satellites is None:
            self._no_catalog(self.tr("衛星カタログなし。名前で入力"))
        else:
            self._note = self.tr("衛星カタログを読み込み中…")
            self._names_changed()
            satellites(self._satellites_read, lambda m: self._no_catalog(self.tr("衛星カタログを読めず（{m}）。名前で入力").format(m=m)))
        if batch is not None:
            self._as_batch(batch)

    # --- Building -----------------------------------------------------------------------------

    def _build(self, known: dict) -> None:
        # The fields scroll when the window is shorter than they are (the satellite tree makes the form tall).
        outer = QVBoxLayout(self)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self._body = body
        form = QFormLayout()
        self.name = QLineEdit()
        form.addRow(self.tr("名称"), self.name)
        numbers = QHBoxLayout()
        self.management_no = QLineEdit()
        self.management_no.setToolTip(self.tr("管理番号（任意の文字。システム向けファイルの項目 managementNo で出せる）"))
        self.identification_no = QLineEdit()
        self.identification_no.setToolTip(self.tr("識別番号（任意の文字。管理番号とは別。システム向けファイルの項目 identificationNo で出せる）"))
        numbers.addWidget(self.management_no, 1)
        numbers.addWidget(QLabel(self.tr("識別番号")))
        numbers.addWidget(self.identification_no, 1)
        form.addRow(self.tr("管理番号"), numbers)
        # 依頼グループ: one of the table's groups in use (known["groupTable"]), or typed when there is no table.
        self.group = QComboBox()
        self.group.setToolTip(self.tr("だれの依頼か（依頼グループ）。撮像依頼・撮像計画ファイル出力・撮像予定の一覧をグループで絞り込める"))
        self._group_table = bool(known.get("groupTable"))
        if self._group_table:
            self.group.addItem(self.tr(NO_GROUP_TEXT), "")
            for name in known.get("groups", []):
                self.group.addItem(name, name)
        else:
            self.group.setEditable(True)
            self.group.addItem("", "")
            for name in known.get("groups", []):
                self.group.addItem(name, name)
            self.group.lineEdit().setPlaceholderText(self.tr("グループなし（依頼グループのテーブルがないので入力）"))
        self.group.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.group.setMinimumContentsLength(16)
        group_row = QHBoxLayout()
        group_row.addWidget(self.group)
        group_row.addStretch(1)
        form.addRow(self.tr("依頼グループ"), group_row)
        row = QHBoxLayout()
        self.category = QComboBox()
        self.category.addItems(CATEGORIES)
        self.category.currentTextChanged.connect(self._category_changed)
        self.achieve = QComboBox()
        self.achieve.addItems(ACHIEVES)
        self.achieve.setToolTip(self.tr("毎回: 対象の日ごとに撮像。期間内1回: 期間内に 1 回撮れれば完了"))
        self.status = QComboBox()
        self.status.addItems(STATUSES)
        for w, label in ((self.category, "区分"), (self.achieve, "達成条件"), (self.status, "状態")):
            row.addWidget(QLabel(self.tr(label)))
            row.addWidget(w)
        row.addStretch(1)
        form.addRow(row)

        period = QHBoxLayout()
        self.start = QDateEdit()
        self.end = QDateEdit()
        for d in (self.start, self.end):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        period.addWidget(self.start)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.end)
        period.addWidget(QLabel(self.tr("（日本時間）")))
        period.addStretch(1)
        form.addRow(self.tr("期間"), period)

        self.repeat_row = QWidget()
        repeat = QHBoxLayout(self.repeat_row)
        repeat.setContentsMargins(0, 0, 0, 0)
        self.repeat_kind = QComboBox()
        self.repeat_kind.addItems(["毎日", "曜日", "間隔"])
        self.repeat_kind.currentTextChanged.connect(self._repeat_changed)
        repeat.addWidget(self.repeat_kind)
        self.weekdays = []
        for c in _WEEKDAYS:
            box = QCheckBox(c)
            self.weekdays.append(box)
            repeat.addWidget(box)
        self.every = QSpinBox()
        self.every.setRange(2, 365)
        self.every.setSuffix(self.tr(" 日ごと"))
        repeat.addWidget(self.every)
        repeat.addStretch(1)
        form.addRow(self.tr("繰り返し"), self.repeat_row)
        # 1 日の回数 (tsudo, 2026-11-11: 「n回以上、m回以下」): n shots make the day count, up to m where there is room.
        times = QHBoxLayout()
        self.times_min = QSpinBox()
        self.times_min.setRange(1, 24)
        self.times_min.setSuffix(self.tr(" 回以上"))
        self.times_min.setToolTip(self.tr("その日にこの回数撮れたら達成（別々のパスで）"))
        self.times_max = QSpinBox()
        self.times_max.setRange(0, 24)
        self.times_max.setSpecialValueText(self.tr("下限と同じ"))
        self.times_max.setSuffix(self.tr(" 回以下"))
        self.times_max.setToolTip(self.tr("下限を超える回は、ほかの依頼を置いたあとの空いた枠で出す。空欄（0）なら下限と同じ回数だけ"))
        self.times_gap = _optional(0, 24, self.tr(" 時間"), 1)
        self.times_gap.setSpecialValueText(self.tr("間隔 1 時間"))
        self.times_gap.setToolTip(self.tr("同じ日の回と回の間の最短の間隔（既定 1 時間: 別のパスになる）"))
        for w, label in ((self.times_min, ""), (self.times_max, "〜"), (self.times_gap, "間隔")):
            if label:
                times.addWidget(QLabel(self.tr(label)))
            times.addWidget(w)
        times.addStretch(1)
        form.addRow(self.tr("1 日の回数"), times)

        prio = QHBoxLayout()
        self.level = QComboBox()
        for name, low, high, value in PRIORITY_LEVELS:
            self.level.addItem(f"{name}（{low}〜{high}）", value)
        self.level.activated.connect(lambda i: self.priority.setValue(self.level.itemData(i)))
        self.detail = QPushButton(self.tr("詳細"))
        self.detail.setCheckable(True)
        self.detail.setToolTip(self.tr("優先度を 1〜1000 の数値で指定（1 ほど高い）"))
        self.priority = QSpinBox()
        self.priority.setRange(1, 1000)
        self.priority.valueChanged.connect(self._priority_changed)
        self.detail.toggled.connect(self.priority.setVisible)
        self.priority_label = QLabel()
        prio.addWidget(self.level)
        prio.addWidget(self.detail)
        prio.addWidget(self.priority)
        prio.addStretch(1)
        form.addRow(self.tr("優先度"), prio)
        form.addRow("", self.priority_label)
        layout.addLayout(form)

        cond = QGroupBox(self.tr("条件（空欄・指定なしは問わない）"))
        cf = QFormLayout(cond)
        kinds = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItems(KINDS)
        self.look = QComboBox()
        self.look.addItems(LOOK_SIDES)
        self.orbit = QComboBox()
        self.orbit.addItems(ORBIT_DIRS)
        self.time_of_day = QComboBox()
        self.time_of_day.addItems(TIMES_OF_DAY)
        for w, label in ((self.kind, "種別"), (self.look, "観測方向"), (self.orbit, "軌道方向"), (self.time_of_day, "時間帯")):
            kinds.addWidget(QLabel(self.tr(label)))
            kinds.addWidget(w)
        kinds.addStretch(1)
        cf.addRow(kinds)
        # The satellites as in the imaging plan: kind → series → satellite → mode (none checked: any satellite).
        self.tree = SatelliteTree(start_checked=False, whole_satellites=True)
        self.tree.setToolTip(self.tr("チェックなしは衛星を問わない。シリーズ全体なら衛星シリーズ、一部なら衛星（チェック順に優先）、モードの一部ならセンサーモードとして保存"))
        self.tree.changed.connect(self._tree_changed)
        self.tree.tree.setMinimumHeight(170)
        cf.addRow(self.tr("衛星"), self.tree)
        self.picked_label = QLabel()
        self.picked_label.setWordWrap(True)
        self.picked_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.by_hand = QPushButton(self.tr("手入力"))
        self.by_hand.setCheckable(True)
        self.by_hand.setToolTip(self.tr("衛星シリーズ・衛星・センサーモードを名前で直接入力（カンマ区切り）"))
        cf.addRow("", self.picked_label)
        hand_row = QHBoxLayout()
        hand_row.addWidget(self.by_hand)
        hand_row.addStretch(1)
        cf.addRow("", hand_row)
        self.hand = QWidget()
        hf = QFormLayout(self.hand)
        hf.setContentsMargins(0, 0, 0, 0)
        self.series = QLineEdit()
        self.series.setPlaceholderText("、".join(known.get("series", [])[:8]) or self.tr("カンマ区切り"))
        hf.addRow(self.tr("衛星シリーズ"), self.series)
        self.satellites = QLineEdit()
        self.satellites.setPlaceholderText(self.tr("個別に指定する衛星（書いた順に優先、カンマ区切り）"))
        hf.addRow(self.tr("衛星"), self.satellites)
        self.modes = QLineEdit()
        self.modes.setPlaceholderText("、".join(known.get("modes", [])[:8]) or self.tr("センサー・モード（カンマ区切り）"))
        hf.addRow(self.tr("センサー・モード"), self.modes)
        for edit in (self.series, self.satellites, self.modes):
            edit.textChanged.connect(self._names_changed)
            edit.editingFinished.connect(self._names_typed)
        self.hand.setVisible(False)
        self.by_hand.toggled.connect(self.hand.setVisible)
        cf.addRow(self.hand)
        angles = QHBoxLayout()
        self.ona_min, self.ona_max = _optional(0, 89, "°"), _optional(0, 89, "°")
        self.inc_min, self.inc_max = _optional(0, 89, "°"), _optional(0, 89, "°")
        for w, label in ((self.ona_min, "オフナディア角"), (self.ona_max, "〜"), (self.inc_min, "入射角"), (self.inc_max, "〜")):
            angles.addWidget(QLabel(self.tr(label)))
            angles.addWidget(w)
        angles.addStretch(1)
        cf.addRow(angles)
        more = QHBoxLayout()
        self.resolution = _optional(0.01, 1000, " m", 2)
        self.resolution.setToolTip(self.tr("これより細かい分解能の衛星のみ"))
        self.cloud = _optional(0, 100, " %", 0)
        self.cloud.setToolTip(self.tr("光学: 撮像時刻の雲量予報（軌道で詰めない衛星は日中の平均）がどのパスもこの値以上なら出力しない。"
                                 "この値以上のパスは使わない（期間内1回で期限まで 2 日以内は警告付きで出力）"))
        for w, label in ((self.resolution, "分解能"), (self.cloud, "実施しない雲量")):
            more.addWidget(QLabel(self.tr(label)))
            more.addWidget(w)
        more.addStretch(1)
        cf.addRow(more)
        self.systems = QLineEdit()
        self.systems.setPlaceholderText("、".join(known.get("systems", [])) or self.tr("先に試すシステム（カンマ区切り）"))
        cf.addRow(self.tr("優先するシステム"), self.systems)
        layout.addWidget(cond)

        area = QGroupBox(self.tr("範囲"))
        al = QVBoxLayout(area)
        buttons = QHBoxLayout()
        self.box_button = QPushButton(self.tr("地図で矩形"))
        self.box_button.setCheckable(True)
        self.box_button.toggled.connect(lambda on: self._tool(self.extent_tool, on))
        self.point_button = QPushButton(self.tr("地図で点"))
        self.point_button.setCheckable(True)
        self.point_button.toggled.connect(lambda on: self._tool(self.point_tool, on))
        self.size = QDoubleSpinBox()
        self.size.setRange(0.1, 500)
        self.size.setValue(10)
        self.size.setSuffix(" km")
        self.size.setToolTip(self.tr("点で指定するときの範囲の一辺"))
        self.selected_button = QPushButton(self.tr("選択中の地物"))
        self.selected_button.setToolTip(self.tr("作業中のレイヤーで選択している地物（最初の 1 つ）を範囲に"))
        self.selected_button.clicked.connect(self._from_selection)
        for w in (self.box_button, self.point_button, self.size, self.selected_button):
            buttons.addWidget(w)
        buttons.addStretch(1)
        al.addLayout(buttons)
        # The names of the layer's features (or the plan's targets) in a batch: shown as written, never as HTML.
        self.area_label = plain_label(QLabel())
        al.addWidget(self.area_label)
        layout.addWidget(area)

        people = QFormLayout()
        self.requester = QLineEdit()
        people.addRow(self.tr("依頼者"), self.requester)
        self.note = QPlainTextEdit()
        self.note.setFixedHeight(54)
        people.addRow(self.tr("メモ"), self.note)
        layout.addLayout(people)
        layout.addStretch(1)
        self.message = plain_label(QLabel())
        self.message.setStyleSheet("color: #b91c1c")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Save).setText(self.tr("保存"))
        box.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("閉じる"))
        box.accepted.connect(self._save_clicked)
        box.rejected.connect(self.reject)
        outer.addWidget(box)

    def _fill(self, r: ImagingRequest) -> None:
        self.name.setText(r.name)
        self.category.setCurrentText(r.category)
        self.achieve.setCurrentText(r.achieve)
        self.status.setCurrentText(r.status)
        today = jst_day(datetime.now().timestamp() * 1000)
        first = jst_day(r.start) if math.isfinite(r.start) else today
        last = jst_day(r.end) if math.isfinite(r.end) else today + timedelta(days=7)
        self.start.setDate(QDate(first.year, first.month, first.day))
        self.end.setDate(QDate(last.year, last.month, last.day))
        rep = repeat_of(r.repeat)
        self.repeat_kind.setCurrentText(rep.kind)
        for i, box in enumerate(self.weekdays):
            box.setChecked(i in rep.weekdays)
        self.every.setValue(max(2, rep.every))
        self.times_min.setValue(max(1, int(r.times_min or 1)))
        self.times_max.setValue(int(r.times_max or 0))
        _set(self.times_gap, r.times_gap)
        self.priority.setValue(r.priority)
        detailed = r.priority not in [v for *_, v in PRIORITY_LEVELS]
        self.detail.setChecked(detailed)
        self.priority.setVisible(detailed)
        self.kind.setCurrentText(r.kind)
        self.look.setCurrentText(r.look_side)
        self.orbit.setCurrentText(r.orbit_dir)
        self.time_of_day.setCurrentText(r.time_of_day)
        self.series.setText(",".join(r.series))
        self.satellites.setText(",".join(r.satellites))
        self.modes.setText(",".join(r.modes))
        _set(self.ona_min, r.off_nadir_min)
        _set(self.ona_max, r.off_nadir_max)
        _set(self.inc_min, r.incidence_min)
        _set(self.inc_max, r.incidence_max)
        _set(self.resolution, r.resolution)
        _set(self.cloud, r.cloud_no_go)
        self.systems.setText(",".join(r.systems))
        self.management_no.setText(r.management_no)
        self.identification_no.setText(r.identification_no)
        self._set_group(r.group)
        self.requester.setText(r.requester)
        self.note.setPlainText(r.note)
        self.shape_kind = r.shape_kind
        self._names_typed()
        self._category_changed(r.category)
        self._repeat_changed(rep.kind)
        self._priority_changed(r.priority)
        self._area_changed()

    def _set_group(self, group: str) -> None:
        """Shows a request's group (one no longer in use, or not in the table, is listed for it)."""
        group = (group or "").strip()
        if not self._group_table:
            self.group.setEditText(group)
            return
        index = next((i for i in range(self.group.count()) if str(self.group.itemData(i) or "").casefold() == group.casefold()), -1)
        if index < 0:
            self.group.addItem(self.tr("{g}（使用停止・テーブルにない）").format(g=group), group)
            index = self.group.count() - 1
        self.group.setCurrentIndex(index)

    def group_value(self) -> str:
        """The group chosen ("" for none)."""
        if self._group_table:
            return str(self.group.currentData() or "").strip()
        return self.group.currentText().strip()

    def _fit(self) -> None:
        """As tall as the fields, but no taller than the screen (the rest scrolls)."""
        want = self._body.sizeHint()
        screen = self.screen().availableGeometry() if self.screen() else None
        height = want.height() + 110
        if screen is not None:
            height = min(height, int(screen.height() * 0.9))
        self.resize(max(self.sizeHint().width(), want.width() + 40), height)

    # --- Reactions ----------------------------------------------------------------------------

    def _satellites_read(self, sats) -> None:
        if self._closing:
            return
        self.tree.set(list(sats))
        if not self.tree.items():
            self._no_catalog(self.tr("衛星カタログに衛星なし。名前で入力"))
            return
        self._note = ""
        self._names_typed()

    def _no_catalog(self, text: str) -> None:
        """No satellites to pick from: the names are typed."""
        if self._closing:
            return
        self.tree.setVisible(False)
        self.by_hand.setChecked(True)
        self.by_hand.setVisible(False)
        self._note = text
        self._names_changed()

    def _lists(self) -> Picked:
        return Picked(_words(self.series.text()), _words(self.satellites.text()), _words(self.modes.text()))

    def _names_typed(self) -> None:
        """The names as typed (or as the request has them) shown as checks."""
        names = self._lists()
        listed = self.tree.items()
        if listed:
            self.tree.set_checks(checks_of(listed, names.series, names.satellites, names.modes))
        self._names_changed()

    def _names_changed(self, _text: str = "") -> None:
        names = self._lists()
        lines = [self._note] if self._note else []
        lines.append(self.tr("保存する条件: {s}").format(s=summary(names)))
        listed = self.tree.items()
        if listed:
            odd = unknown(listed, names.series, names.satellites, names.modes)
            odd_names = [*odd.series, *odd.satellites, *odd.modes]
            if odd_names:
                lines.append(self.tr("衛星カタログにない名前（そのまま保存）: {n}").format(n="、".join(odd_names)))
        self.picked_label.setText("\n".join(lines))

    def _tree_changed(self) -> None:
        """The checks written as the request's series, satellites and modes."""
        p = picked(self.tree.items(), self._lists())
        for edit, names in ((self.series, p.series), (self.satellites, p.satellites), (self.modes, p.modes)):
            edit.blockSignals(True)
            edit.setText(",".join(names))
            edit.blockSignals(False)
        self._names_changed()

    def _category_changed(self, text: str) -> None:
        self.repeat_row.setEnabled(text == "定点")

    def _repeat_changed(self, kind: str) -> None:
        for box in self.weekdays:
            box.setVisible(kind == "曜日")
        self.every.setVisible(kind == "間隔")

    def _priority_changed(self, p: int) -> None:
        name = level_of(p)
        self.level.blockSignals(True)
        self.level.setCurrentIndex(next(i for i, (n, *_) in enumerate(PRIORITY_LEVELS) if n == name))
        self.level.blockSignals(False)
        boost = self.request.boost
        text = priority_text(p)
        if boost:
            text += self.tr("・繰り上げ {b}（現在 {now}）").format(b=boost, now=priority_text(p - boost))
        self.priority_label.setText(text)

    def _tool(self, tool, on: bool) -> None:
        if on:
            for b in (self.box_button, self.point_button):
                if (b is self.box_button) != (tool is self.extent_tool):
                    b.setChecked(False)
            self.canvas.setMapTool(tool)
        elif self.canvas.mapTool() is tool:
            self.canvas.unsetMapTool(tool)

    def _to_wgs(self):
        return to_wgs84(self.canvas.mapSettings().destinationCrs())

    def _extent_drawn(self, extent: QgsRectangle) -> None:
        if extent.isEmpty():
            return
        g = QgsGeometry.fromRect(extent)
        g.transform(self._to_wgs())
        box = g.boundingBox()
        w, s, e, n = box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()
        self.rings = [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
        self.shape_kind = "範囲"
        self.box_button.setChecked(False)
        self._area_changed()

    def _point_clicked(self, point: QgsPointXY, _button=None) -> None:
        p = self._to_wgs().transform(point)
        self.rings = square_rings(p.x(), p.y(), self.size.value())
        self.shape_kind = "点"
        self.point_button.setChecked(False)
        self._area_changed()

    def _from_selection(self) -> None:
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.selectedFeatureCount():
            self.message.setText(self.tr("作業中のレイヤーで地物を選択"))
            return
        feature = next(layer.getSelectedFeatures())
        shape = shape_of(feature.geometry(), to_wgs84(layer.crs()))
        if shape is None:
            return
        if shape.polygons:
            self.rings = [[list(p) for p in ring] for ring in shape.polygons[0]]
            self.shape_kind = "範囲"
        else:
            xs = [v[0] for v in shape.vertices]
            ys = [v[1] for v in shape.vertices]
            if len(shape.vertices) == 1:
                self.rings = square_rings(xs[0], ys[0], self.size.value())
                self.shape_kind = "点"
            else:
                w, s, e, n = min(xs), min(ys), max(xs), max(ys)
                self.rings = [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
                self.shape_kind = "範囲"
        self.message.setText("")
        self._area_changed()

    def _as_batch(self, batch: dict) -> None:
        """Names and areas from the layer's features: shown, not asked."""
        self.name.setEnabled(False)
        self.name.setText(batch.get("names", ""))
        for w in (self.box_button, self.point_button, self.size, self.selected_button):
            w.hide()
        self.area_label.setText(batch.get("text", ""))
        self.overlay.paint([(rings, "#f59e0b", 2, name) for name, rings, *_rest in batch["items"]])
        # Numbers from the layer's attributes: what is typed here is for the features whose attribute is empty.
        fields = batch.get("fields") or {}
        for key, edit in (("management_no", self.management_no), ("identification_no", self.identification_no)):
            if fields.get(key):
                edit.setPlaceholderText(self.tr("属性「{field}」から").format(field=fields[key]))
                edit.setToolTip(self.tr("属性「{field}」の値で登録。ここに入れた値は属性が空の地物に").format(field=fields[key]))
        self.canvas.refresh()

    def _area_changed(self) -> None:
        box = bbox_of(self.rings)
        if box is None:
            self.area_label.setText(self.tr("範囲なし（地図で矩形・点、または選択中の地物で指定）"))
            self.overlay.unpaint()
        else:
            w, s, e, n = box
            self.area_label.setText(self.tr("{kind}: 経度 {w:.4f}〜{e:.4f}・緯度 {s:.4f}〜{n:.4f}").format(kind=self.shape_kind, w=w, e=e, s=s, n=n))
            self.overlay.paint([(self.rings, "#f59e0b", 2, self.name.text())])
        self.canvas.refresh()

    # --- Saving -------------------------------------------------------------------------------

    def value(self) -> ImagingRequest:
        """The request as the form has it."""
        r = self.request
        d0, d1 = self.start.date(), self.end.date()
        from datetime import date

        first, _ = day_bounds(date(d0.year(), d0.month(), d0.day()))
        _, last = day_bounds(date(d1.year(), d1.month(), d1.day()))
        kind = self.repeat_kind.currentText()
        from ..core.request.model import Repeat

        rep = Repeat(kind, frozenset(i for i, b in enumerate(self.weekdays) if b.isChecked()), self.every.value() if kind == "間隔" else 1)
        out = ImagingRequest(**{**r.__dict__})
        out.name = self.name.text().strip()
        out.category, out.achieve, out.status = self.category.currentText(), self.achieve.currentText(), self.status.currentText()
        out.start, out.end = first, last
        out.repeat = repeat_text(rep) if out.category == "定点" else "毎日"
        out.priority = self.priority.value()
        out.times_min = self.times_min.value()
        out.times_max = max(self.times_max.value(), out.times_min) if self.times_max.value() > 0 else None
        out.times_gap = _get(self.times_gap)
        out.kind, out.look_side, out.orbit_dir, out.time_of_day = (self.kind.currentText(), self.look.currentText(),
                                                                    self.orbit.currentText(), self.time_of_day.currentText())
        out.series, out.satellites, out.modes = _words(self.series.text()), _words(self.satellites.text()), _words(self.modes.text())
        out.off_nadir_min, out.off_nadir_max = _get(self.ona_min), _get(self.ona_max)
        out.incidence_min, out.incidence_max = _get(self.inc_min), _get(self.inc_max)
        out.resolution, out.cloud_no_go = _get(self.resolution), _get(self.cloud)
        out.systems = _words(self.systems.text())
        out.management_no, out.identification_no = self.management_no.text().strip(), self.identification_no.text().strip()
        out.group = self.group_value()
        out.requester, out.note = self.requester.text().strip(), self.note.toPlainText().strip()
        out.rings, out.shape_kind = [list(r) for r in self.rings], self.shape_kind
        return out

    def problems(self, r: ImagingRequest) -> list:
        out = []
        if not r.name and self.batch is None:
            out.append(self.tr("名称なし"))
        if not r.rings and self.batch is None:
            out.append(self.tr("範囲なし"))
        if r.end < r.start:
            out.append(self.tr("期間の終わりが始まりより前"))
        if r.category == "定点" and repeat_of(r.repeat).kind == "毎日" and self.repeat_kind.currentText() == "曜日":
            out.append(self.tr("曜日を 1 つ以上選択"))
        for low, high, label in ((r.off_nadir_min, r.off_nadir_max, "オフナディア角"), (r.incidence_min, r.incidence_max, "入射角")):
            if low is not None and high is not None and low > high:
                out.append(self.tr("{label}の下限が上限より大きい").format(label=label))
        return out

    def _save_clicked(self) -> None:
        r = self.value()
        problems = self.problems(r)
        if problems:
            self.message.setText("・".join(problems))
            return
        self.message.setText("")
        self.memory.save()
        self.save(r)

    def _closed(self, _result=None) -> None:
        self._closing = True
        for tool in (self.extent_tool, self.point_tool):
            if self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)
        self.overlay.unpaint()
        self.canvas.refresh()
