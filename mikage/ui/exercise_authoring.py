"""
判読の練習の問題を作る (練習モードの段階 3 の 3; the instructors' 「判読の練習の問題を
作る…」 in the production MIKAGE, features/training.py): the organisation's
exercises (問題の一覧: those offered and those 作りかけ), and an exercise's
editor kept beside the map — its image (the observation dock's, as the image
catalog opened it), its area drawn on the map, its answers taken from the
image's observation records (an experienced interpreter's) or clicked on the
map, each with its points and explanation, and its scoring where it is not
the organisation's. 保存 writes the tables (`io`: the feature, in a task);
「練習モードで試す」 saves it and solves it as the people will (the try not
kept). The answers are numbered rings on the map (ui/exercise_map.py).
"""
from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject, QStringListModel, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.obs.api import kind_of, RecordQuery
from ..core.training.api import (
    answers_from_records, CHANGE, CHANGES, check, copied, DEFAULT_SCORING, Draft, DraftAnswer, EXAM, exercise_of_draft, FIND, IDENTIFY,
    kind_text, MEASURE, metres, new_exercise_id, PRACTICE, Scoring, TYPES,
)
from ..qgis_io import map_tools
from ..qgis_io.geometry import to_wgs84
from ..qgis_io.obs_images import IMAGE_ID, image_path
from .catalog_overlay import DrawTool
from .exercise_map import ClickTool, ExerciseMarks, fit_into, free_part, Spot
from .exercise_screens import BAD, GOOD, GREY, ORANGE
from .docks import ask_no
from .emphasis import emphasize
from .fit import fit_text

#: The names the tests and the help's pictures find the windows by.
MANAGER_NAME, EDITOR_NAME = "MikageExerciseManager", "MikageExerciseEditor"
#: What each kind of exercise asks for (under the editor's 種類).
TYPE_HINTS = {
    FIND: "範囲の中の対象の種別を、すべて見つけて記録（見落とし・誤りも採点）",
    IDENTIFY: "印を付けたモノの種別と活動を回答（探さない）",
    MEASURE: "印を付けたモノの長さ・幅・向きを 2〜4 点で測る",
    CHANGE: "前の画像と比べて、来た・継続・消失を記録",
}
#: The answers' columns.
COLUMNS = ("#", "種別", "活動", "長さ m", "幅 m", "向き °", "変化", "配点", "扱い", "許容 m", "解説", "参考写真")
C_NO, C_KIND, C_ACTIVITY, C_LENGTH, C_WIDTH, C_HEADING, C_CHANGE, C_POINTS, C_REQUIRED, C_TOLERANCE, C_EXPLAIN, C_REFERENCE = range(12)
REQUIRED_WORDS = ("必須", "加点")
#: Taken for the same thing as an answer already there (m): offered unticked in 「観測記録から取り込む」.
SAME_PLACE = 10.0
#: The scoring's fields the editor changes: (key, words, unit); weights and the kind's credits apart.
SCORING_ROWS = (("tolerance", "位置の許容", "m"), ("extraPenalty", "誤り 1 件の減点", "点"), ("lengthTolerance", "長さ・幅の許容", "%"),
                ("headingTolerance", "向きの許容", "度"))
WEIGHT_ROWS = (("find", "見つける"), ("kind", "種別"), ("activity", "活動"), ("measure", "測る"), ("change", "変化"))
CREDIT_ROWS = (("parent", "一つ上の分類"), ("top", "大分類だけ"))
EITHER_END = ("組織の既定", "前後を見分ける", "前後を問わない（SAR）")


def _number(text: str) -> Optional[float]:
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _g(v: Optional[float]) -> str:
    return "" if v is None else f"{v:g}"


def area_text(rings: Sequence) -> str:
    """描いた範囲's words: its size (km²) and corners."""
    if not rings:
        return "範囲なし（画像の全体）"
    ring = rings[0]
    if len(ring) < 3:
        return "範囲なし（画像の全体）"
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = 111.32 * math.cos(math.radians(lat0))
    xy = [(p[0] * k, p[1] * 110.57) for p in ring]
    a = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))) / 2
    corners = len(ring) - (1 if ring[0] == ring[-1] else 0)
    return f"描いた範囲 {a:.2f} km²（角 {corners}）"


# ---- 問題の一覧 ------------------------------------------------------------------------------------------

class ExerciseManager(QDialog):
    """The organisation's exercises (offered and 作りかけ) to choose one to change, or to make a new one."""

    new_pressed = pyqtSignal()
    edit_pressed = pyqtSignal(str)
    copy_pressed = pyqtSignal(str)
    active_pressed = pyqtSignal(str, bool)
    delete_pressed = pyqtSignal(str)
    reload_pressed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.setObjectName(MANAGER_NAME)
        self.setWindowTitle(self.tr("判読の練習の問題を作る"))
        self.setModal(False)
        self.drafts: list[Draft] = []
        box = QVBoxLayout(self)
        lead = QLabel(self.tr("組織の問題（問題の表と正解の表）。作りかけは練習の一覧に出ない。「練習モードで試す」で、みんなと同じように解ける"))
        lead.setWordWrap(True)
        box.addWidget(lead)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([self.tr(t) for t in ("コース", "順", "題名", "種類", "形式", "正解", "状態")])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        for c in range(7):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c == 2 else QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.table, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #555;")
        box.addWidget(self.status)
        row = QHBoxLayout()
        self.new_button = QPushButton(self.tr("新しい問題…"))
        self.edit_button = QPushButton(self.tr("編集…"))
        self.copy_button = QPushButton(self.tr("コピーして作る…"))
        self.active_button = QPushButton(self.tr("練習の一覧に出す"))
        self.delete_button = QPushButton(self.tr("削除…"))
        for b in (self.new_button, self.edit_button, self.copy_button, self.active_button, self.delete_button):
            row.addWidget(b)
        row.addStretch(1)
        self.reload_button = QPushButton(self.tr("読み直す"))
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        row.addWidget(self.reload_button)
        row.addWidget(close)
        box.addLayout(row)
        self.new_button.clicked.connect(self.new_pressed)
        self.edit_button.clicked.connect(lambda: self._with_current(self.edit_pressed.emit))
        self.copy_button.clicked.connect(lambda: self._with_current(self.copy_pressed.emit))
        self.active_button.clicked.connect(self._toggle)
        self.delete_button.clicked.connect(lambda: self._with_current(self.delete_pressed.emit))
        self.reload_button.clicked.connect(self.reload_pressed)
        self.table.itemSelectionChanged.connect(self._chosen)
        self.table.itemDoubleClicked.connect(lambda _i: self._with_current(self.edit_pressed.emit))
        self.show_drafts([])
        self.resize(860, 440)

    def show_drafts(self, drafts: Sequence[Draft], keep: Optional[str] = None) -> None:
        """The exercises shown (`keep`: the one to select, by key; else the one selected now)."""
        chosen = keep or (self.current().key if self.current() is not None else None)
        # By course (in the order the table first has them), then by their order in it.
        courses = {c: i for i, c in enumerate(dict.fromkeys(d.course for d in drafts))}
        self.drafts = sorted(drafts, key=lambda d: (courses[d.course], d.order, d.title))
        self.table.setRowCount(len(self.drafts))
        for r, d in enumerate(self.drafts):
            cells = [d.course or self.tr("（コースなし）"), str(d.order), d.title or self.tr("（題名なし）"), d.type, d.mode,
                     str(len(d.answers)), self.tr("出す") if d.active else self.tr("作りかけ")]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 4 and d.mode == EXAM:
                    item.setForeground(Qt.GlobalColor.darkYellow)
                if c == 6:
                    item.setForeground(Qt.GlobalColor.darkGreen if d.active else Qt.GlobalColor.gray)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, d.key)
                self.table.setItem(r, c, item)
        row = next((r for r, d in enumerate(self.drafts) if d.key == chosen), 0 if self.drafts else -1)
        if row >= 0:
            self.table.selectRow(row)
        self._chosen()

    def current(self) -> Optional[Draft]:
        model = self.table.selectionModel()
        rows = model.selectedRows() if model is not None else []
        if not rows:
            return None
        key = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((d for d in self.drafts if d.key == key), None)

    def say(self, text: str, color: str = "#555") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def _with_current(self, emit: Callable[[str], None]) -> None:
        d = self.current()
        if d is not None:
            emit(d.key)

    def _toggle(self) -> None:
        d = self.current()
        if d is not None:
            self.active_pressed.emit(d.key, not d.active)

    def _chosen(self) -> None:
        d = self.current()
        for b in (self.edit_button, self.copy_button, self.active_button, self.delete_button):
            b.setEnabled(d is not None)
        self.active_button.setText(self.tr("作りかけに変更") if d is not None and d.active else self.tr("練習の一覧に出す"))


# ---- 問題の編集 ------------------------------------------------------------------------------------------

class ExerciseEditor(QDialog):
    """One exercise changed beside the map (not blocking it: its area is drawn and its answers clicked there)."""

    save_pressed = pyqtSignal()
    try_pressed = pyqtSignal()
    draw_pressed = pyqtSignal()
    view_area_pressed = pyqtSignal()
    image_pressed = pyqtSignal(str)
    import_pressed = pyqtSignal()
    add_toggled = pyqtSignal(bool)
    show_pressed = pyqtSignal()
    reference_pressed = pyqtSignal()
    #: The answers or the area changed, or an answer chosen (its row; -1: none): the marks drawn again.
    marks_changed = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, draft: Draft, base: Scoring = DEFAULT_SCORING, courses: Sequence[str] = (), kinds: Sequence[str] = (),
                 roles: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.setObjectName(EDITOR_NAME)
        self.setModal(False)
        self.base = base
        self.roles = dict(roles or {})
        self._draft = draft
        self.answers: list[DraftAnswer] = []
        self.area: list = []
        self.dirty = False
        self._filling = False
        #: Asked before closing with changes not saved (the tests answer it).
        self.ask_close: Callable[[], bool] = self._ask_close
        self._closing = False
        self.kinds_model = QStringListModel(list(kinds))
        box = QVBoxLayout(self)
        # Narrow (tabs), the map beside it left to draw on and to see the answers.
        self.tabs = QTabWidget()
        box.addWidget(self.tabs, 1)
        page = QWidget()
        page_box = QVBoxLayout(page)
        left = QFormLayout()
        page_box.addLayout(left)
        page_box.addStretch(1)
        self.tabs.addTab(page, self.tr("問題"))
        self.title_edit = QLineEdit()
        left.addRow(self.tr("題名"), self.title_edit)
        self.instruction = QPlainTextEdit()
        self.instruction.setPlaceholderText(self.tr("例: 範囲の中の船をすべて見つけて、種別と活動を記録してください。"))
        self.instruction.setFixedHeight(self.instruction.fontMetrics().lineSpacing() * 3 + 14)
        left.addRow(self.tr("指示文"), self.instruction)
        kind_row = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItems(list(TYPES))
        kind_row.addWidget(self.kind)
        self.kind_hint = QLabel()
        self.kind_hint.setStyleSheet("color: #555;")
        self.kind_hint.setWordWrap(True)
        kind_row.addWidget(self.kind_hint, 1)
        left.addRow(self.tr("種類"), kind_row)
        self.image_id, self.image_source, self.take_image = self._image_row(left, self.tr("画像"), "image")
        self.before_id, self.before_source, self.take_before = self._image_row(left, self.tr("前の画像"), "before")
        self.before_label = left.labelForField(self.before_id.parentWidget())
        area_row = QHBoxLayout()
        self.area_label = QLabel()
        area_row.addWidget(self.area_label, 1)
        self.draw_button = QPushButton(self.tr("範囲を描く"))
        self.draw_button.setToolTip(self.tr("地図で角をクリック。ダブルクリックか右クリックで閉じる"))
        self.view_button = QPushButton(self.tr("いまの地図の範囲"))
        self.clear_area_button = QPushButton(self.tr("消す"))
        for b in (self.draw_button, self.view_button, self.clear_area_button):
            area_row.addWidget(b)
        left.addRow(self.tr("範囲"), area_row)
        self.targets = QLineEdit()
        self.targets.setPlaceholderText(self.tr("例: 船舶（; で区切って複数。空: すべての種別）"))
        completer = QCompleter(self.kinds_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.targets.setCompleter(completer)
        left.addRow(self.tr("対象の種別"), self.targets)
        right = left
        mode_row = QHBoxLayout()
        self.practice = QRadioButton(self.tr("練習"))
        self.exam = QRadioButton(self.tr("試験（1 回だけ・ヒントなし）"))
        self.mode_group = QButtonGroup(self)
        for b in (self.practice, self.exam):
            self.mode_group.addButton(b)
            mode_row.addWidget(b)
        mode_row.addStretch(1)
        right.addRow(self.tr("形式"), mode_row)
        self.limit = QSpinBox()
        self.limit.setRange(0, 600)
        self.limit.setSuffix(self.tr(" 分"))
        self.limit.setSpecialValueText(self.tr("なし"))
        right.addRow(self.tr("制限時間"), self.limit)
        self.pass_score = QSpinBox()
        self.pass_score.setRange(-1, 100)
        self.pass_score.setSuffix(self.tr(" 点"))
        self.pass_score.setSpecialValueText(self.tr("組織の既定（{n} 点）").format(n=f"{base.pass_score:g}"))
        right.addRow(self.tr("合格点"), self.pass_score)
        self.difficulty = QComboBox()
        self.difficulty.setEditable(True)
        self.difficulty.addItems(["", "易", "中", "難"])
        right.addRow(self.tr("難しさ"), self.difficulty)
        course_row = QHBoxLayout()
        self.course = QComboBox()
        self.course.setEditable(True)
        self.course.addItems(list(dict.fromkeys(c for c in courses if c)))
        course_row.addWidget(self.course, 1)
        course_row.addWidget(QLabel(self.tr("順")))
        self.order = QSpinBox()
        self.order.setRange(0, 9999)
        course_row.addWidget(self.order)
        right.addRow(self.tr("コース"), course_row)
        self.roles_button = QToolButton()
        self.roles_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.roles_menu = QMenu(self.roles_button)
        self.role_actions = {}
        for code, name in self.roles.items():
            action = self.roles_menu.addAction(name)
            action.setCheckable(True)
            action.toggled.connect(self._roles_shown)
            self.role_actions[code] = action
        self.roles_button.setMenu(self.roles_menu)
        right.addRow(self.tr("対象の担当"), self.roles_button)
        self.show_answers = QCheckBox(self.tr("答え合わせのあと、正解と解説を表示"))
        right.addRow("", self.show_answers)
        self.active = QCheckBox(self.tr("練習の一覧に出す（外すと作りかけ）"))
        right.addRow("", self.active)
        # 正解
        answers_box = QWidget()
        self.answers_box = answers_box
        ab = QVBoxLayout(answers_box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        for c in range(len(COLUMNS)):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.resizeSection(C_NO, 34)
        header.resizeSection(C_KIND, 170)
        header.resizeSection(C_EXPLAIN, 260)
        for c in (C_LENGTH, C_WIDTH, C_HEADING, C_POINTS, C_TOLERANCE):
            header.resizeSection(c, 62)
        self.table.setMinimumHeight(150)
        ab.addWidget(self.table, 1)
        self.import_button = QPushButton(self.tr("観測記録から取り込む…"))
        self.import_button.setToolTip(self.tr("この画像の観測記録（範囲の中）を、正解として取り込み"))
        self.add_button = QPushButton(self.tr("地図で足す"))
        self.add_button.setCheckable(True)
        self.add_button.setToolTip(self.tr("地図をクリックした所に正解を追加（Esc か右クリックで終了）"))
        self.remove_button = QPushButton(self.tr("選んだ正解を消す"))
        self.reference_button = QPushButton(self.tr("参考写真…"))
        self.show_button = QPushButton(self.tr("地図で見る"))
        tools = QGridLayout()
        for i, b in enumerate((self.import_button, self.add_button, self.show_button, self.remove_button, self.reference_button)):
            tools.addWidget(b, i // 3, i % 3)
        ab.addLayout(tools)
        self.tabs.addTab(answers_box, self.tr("正解"))
        # 採点の決まり
        rules = QWidget()
        rules_box = QVBoxLayout(rules)
        form = QFormLayout()
        rules_box.addLayout(form)
        rules_box.addStretch(1)
        note = QLabel(self.tr("この問題だけ変えるときに入力。空欄は組織の決まり（薄い字）で採点"))
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        form.addRow(note)
        self.rule_edits: dict[str, QLineEdit] = {}
        for key, words, unit in SCORING_ROWS:
            edit = QLineEdit()
            edit.setPlaceholderText(f"{self._base_value(key):g}")
            edit.setMaximumWidth(70)
            self.rule_edits[key] = edit
            line = QHBoxLayout()
            line.addWidget(edit)
            line.addWidget(QLabel(self.tr(unit)))
            line.addStretch(1)
            form.addRow(self.tr(words), line)
        self.weight_edits: dict[str, QLineEdit] = {}
        weights = QHBoxLayout()
        for key, words in WEIGHT_ROWS:
            edit = QLineEdit()
            edit.setPlaceholderText(f"{base.weight(key):g}")
            edit.setMaximumWidth(50)
            self.weight_edits[key] = edit
            weights.addWidget(QLabel(self.tr(words)))
            weights.addWidget(edit)
        weights.addStretch(1)
        form.addRow(self.tr("配点の割合"), weights)
        credits = QHBoxLayout()
        self.credit_edits: dict[str, QLineEdit] = {}
        for key, words in CREDIT_ROWS:
            edit = QLineEdit()
            edit.setPlaceholderText(f"{base.kind_parent if key == 'parent' else base.kind_top:g}")
            edit.setMaximumWidth(50)
            self.credit_edits[key] = edit
            credits.addWidget(QLabel(self.tr(words)))
            credits.addWidget(edit)
        credits.addStretch(1)
        form.addRow(self.tr("種別の部分点（0〜1）"), credits)
        self.either_end = QComboBox()
        self.either_end.addItems([self.tr(t) for t in EITHER_END])
        ends = QHBoxLayout()
        ends.addWidget(self.either_end)
        ends.addStretch(1)
        form.addRow(self.tr("向き"), ends)
        self.tabs.addTab(rules, self.tr("採点の決まり"))
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.status)
        row = QHBoxLayout()
        self.saved_label = QLabel()
        self.saved_label.setStyleSheet("color: #555;")
        row.addWidget(self.saved_label)
        row.addStretch(1)
        self.try_button = QPushButton(self.tr("練習モードで試す"))
        self.try_button.setToolTip(self.tr("保存してから、練習モードでみんなと同じように解く（試した結果は残らない）"))
        self.save_button = QPushButton(self.tr("保存"))
        self.save_button.setDefault(True)
        emphasize(self.save_button)
        self.close_button = QPushButton(self.tr("閉じる"))
        for b in (self.try_button, self.save_button, self.close_button):
            row.addWidget(b)
        box.addLayout(row)
        # wiring
        self.take_image.clicked.connect(lambda: self.image_pressed.emit("image"))
        self.take_before.clicked.connect(lambda: self.image_pressed.emit("before"))
        self.draw_button.clicked.connect(self.draw_pressed)
        self.view_button.clicked.connect(self.view_area_pressed)
        self.clear_area_button.clicked.connect(lambda: self.set_area([]))
        self.import_button.clicked.connect(self.import_pressed)
        self.add_button.toggled.connect(self.add_toggled)
        self.remove_button.clicked.connect(self.remove_selected)
        self.reference_button.clicked.connect(self.reference_pressed)
        self.show_button.clicked.connect(self.show_pressed)
        self.save_button.clicked.connect(self.save_pressed)
        self.try_button.clicked.connect(self.try_pressed)
        self.close_button.clicked.connect(self.close)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.table.itemChanged.connect(self._cell_changed)
        self.table.itemSelectionChanged.connect(self.marks_changed)
        for w in (self.title_edit, self.image_id, self.image_source, self.before_id, self.before_source, self.targets,
                  *self.rule_edits.values(), *self.weight_edits.values(), *self.credit_edits.values()):
            w.textChanged.connect(self._changed)
        self.instruction.textChanged.connect(self._changed)
        for w in (self.limit, self.pass_score, self.order):
            w.valueChanged.connect(self._changed)
        for w in (self.difficulty, self.course):
            w.currentTextChanged.connect(self._changed)
        self.either_end.currentIndexChanged.connect(self._changed)
        for w in (self.practice, self.exam, self.show_answers, self.active):
            w.toggled.connect(self._changed)
        self.load(draft)
        self.resize(680, 640)

    def _image_row(self, form: QFormLayout, words: str, which: str):
        row = QWidget()
        r = QHBoxLayout(row)
        r.setContentsMargins(0, 0, 0, 0)
        image_id = QLineEdit()
        image_id.setPlaceholderText(self.tr("画像 ID（画像カタログ）"))
        source = QLineEdit()
        source.setPlaceholderText(self.tr("カタログにない画像はファイルか URL"))
        take = QPushButton(self.tr("観測記録ドックの画像"))
        take.setToolTip(self.tr("観測記録ドックでいま選んでいる画像を使用（画像カタログで開いた画像は ID で）"))
        r.addWidget(image_id, 2)
        r.addWidget(source, 3)
        r.addWidget(take)
        form.addRow(words, row)
        row.setObjectName(f"MikageExerciseEditor_{which}")
        return image_id, source, take

    def _base_value(self, key: str) -> float:
        return {"tolerance": self.base.tolerance, "extraPenalty": self.base.extra_penalty, "lengthTolerance": self.base.length_tolerance,
                "headingTolerance": self.base.heading_tolerance}[key]

    # ---- the draft in the fields -------------------------------------------------------------------------

    def load(self, d: Draft) -> None:
        """A draft shown (its changes not saved forgotten)."""
        self._filling = True
        try:
            self._draft = d
            self.setWindowTitle(self.tr("判読の練習の問題を作る — {t}").format(t=d.title or d.id))
            self.title_edit.setText(d.title)
            self.instruction.setPlainText(d.instruction)
            self.kind.setCurrentIndex(TYPES.index(d.type) if d.type in TYPES else 0)
            self.image_id.setText(d.image_id)
            self.image_source.setText(d.image_source)
            self.before_id.setText(d.before_image_id)
            self.before_source.setText(d.before_image_source)
            self.area = [list(map(list, r)) for r in d.area]
            self.targets.setText("; ".join(d.target_kinds))
            (self.exam if d.mode == EXAM else self.practice).setChecked(True)
            self.limit.setValue(int(round(d.time_limit or 0)))
            self.pass_score.setValue(-1 if d.pass_score is None else int(round(d.pass_score)))
            self.difficulty.setCurrentText(d.difficulty)
            self.course.setCurrentText(d.course)
            self.order.setValue(d.order)
            for code, action in self.role_actions.items():
                action.setChecked(code in d.roles)
            self.show_answers.setChecked(d.show_answers)
            self.active.setChecked(d.active)
            s = d.scoring or {}
            for key, edit in self.rule_edits.items():
                edit.setText(_g(s.get(key)) if isinstance(s.get(key), (int, float)) else "")
            weights = s.get("weights") if isinstance(s.get("weights"), dict) else {}
            for key, edit in self.weight_edits.items():
                edit.setText(_g(weights.get(key)) if isinstance(weights.get(key), (int, float)) else "")
            credits = s.get("kindCredit") if isinstance(s.get("kindCredit"), dict) else {}
            for key, edit in self.credit_edits.items():
                edit.setText(_g(credits.get(key)) if isinstance(credits.get(key), (int, float)) else "")
            either = s.get("headingEitherEnd")
            self.either_end.setCurrentIndex(0 if either is None else 2 if either else 1)
            self.answers = [DraftAnswer(**{f: getattr(a, f) for f in DraftAnswer.__dataclass_fields__}) for a in d.answers]
            self._fill_answers()
            self._kind_changed()
            self._roles_shown()
            self.area_label.setText(area_text(self.area))
        finally:
            self._filling = False
        self.dirty = False
        self.saved_label.setText(self.tr("未保存") if d.object_id is None else "")
        self.marks_changed.emit()

    def draft(self) -> Draft:
        """The draft as the fields have it (the problems of what was typed in `typed_problems`)."""
        self.typed_problems: list[str] = []
        d = self._draft
        scoring = {k: v for k, v in (d.scoring or {}).items()
                   if k not in (*self.rule_edits, "weights", "kindCredit", "headingEitherEnd")}
        for key, edit in self.rule_edits.items():
            v = self._typed(edit, dict((k, w) for k, w, _u in SCORING_ROWS)[key])
            if v is not None:
                scoring[key] = v
        weights = {k: v for k, v in ((key, self._typed(edit, dict(WEIGHT_ROWS)[key])) for key, edit in self.weight_edits.items())
                   if v is not None}
        if weights:
            scoring["weights"] = weights
        credits = {}
        for key, edit in self.credit_edits.items():
            v = self._typed(edit, dict(CREDIT_ROWS)[key])
            if v is not None:
                if not 0 <= v <= 1:
                    self.typed_problems.append(self.tr("種別の部分点は 0〜1"))
                credits[key] = v
        if credits:
            scoring["kindCredit"] = credits
        if self.either_end.currentIndex() > 0:
            scoring["headingEitherEnd"] = self.either_end.currentIndex() == 2
        return Draft(
            d.id, title=self.title_edit.text().strip(), type=self.kind.currentText(), instruction=self.instruction.toPlainText().strip(),
            image_id=self.image_id.text().strip(), image_source=self.image_source.text().strip(),
            before_image_id=self.before_id.text().strip(), before_image_source=self.before_source.text().strip(),
            area=[list(map(list, r)) for r in self.area], target_kinds=[k.strip() for k in self.targets.text().replace("；", ";").split(";") if k.strip()],
            difficulty=self.difficulty.currentText().strip(), time_limit=float(self.limit.value()) or None,
            mode=EXAM if self.exam.isChecked() else PRACTICE, show_answers=self.show_answers.isChecked(),
            pass_score=None if self.pass_score.value() < 0 else float(self.pass_score.value()), scoring=scoring,
            course=self.course.currentText().strip(), order=self.order.value(),
            roles=[c for c, a in self.role_actions.items() if a.isChecked()], active=self.active.isChecked(),
            answers=[DraftAnswer(**{f: getattr(a, f) for f in DraftAnswer.__dataclass_fields__}) for a in self.answers],
            object_id=d.object_id, answer_ids_read=d.answer_ids_read)

    def _typed(self, edit: QLineEdit, words: str) -> Optional[float]:
        text = edit.text().strip()
        if not text:
            return None
        v = _number(text)
        if v is None or v < 0:
            self.typed_problems.append(self.tr("「{w}」は 0 以上の数で（{t}）").format(w=words, t=text))
            return None
        return v

    def saved(self, d: Draft) -> None:
        """Written: the object ids kept (another 保存 changes those rows), the changes counted from here."""
        self._draft = d
        for a, written in zip(self.answers, d.answers):
            a.object_id = written.object_id
        self.dirty = False
        self.setWindowTitle(self.tr("判読の練習の問題を作る — {t}").format(t=d.title or d.id))
        self.saved_label.setText(self.tr("保存済み（{t}）").format(t=datetime.now().strftime("%H:%M")))

    def say(self, text: str, color: str = "#333") -> None:
        self.status.setText(f"<span style='color:{color}'>{text}</span>" if text else "")

    # ---- the area, the images and the answers ------------------------------------------------------------

    def set_area(self, rings: list) -> None:
        self.area = [list(map(list, r)) for r in rings]
        self.area_label.setText(area_text(self.area))
        self._changed()
        self.marks_changed.emit()

    def set_image(self, which: str, image_id: str, source: str) -> None:
        ids, sources = (self.image_id, self.image_source) if which == "image" else (self.before_id, self.before_source)
        ids.setText(image_id)
        sources.setText("" if image_id else source)

    def add_answers(self, answers: Sequence[DraftAnswer]) -> None:
        for a in answers:
            a.number = len(self.answers) + 1
            self.answers.append(a)
        self._fill_answers()
        if answers:
            self.tabs.setCurrentWidget(self.answers_box)
            self.table.selectRow(len(self.answers) - 1)
        self._changed()
        self.marks_changed.emit()

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()}, reverse=True)
        for r in rows:
            del self.answers[r]
        for i, a in enumerate(self.answers):
            a.number = i + 1
        self._fill_answers()
        if rows:
            self._changed()
        self.marks_changed.emit()

    def chosen_row(self) -> int:
        model = self.table.selectionModel()
        rows = model.selectedRows() if model is not None else []
        return rows[0].row() if rows else -1

    def set_reference(self, path: str) -> None:
        r = self.chosen_row()
        if 0 <= r < len(self.answers):
            self.answers[r].reference = path
            self._fill_answers()
            self.table.selectRow(r)
            self._changed()

    def spots(self) -> list[Spot]:
        return [Spot(a.lon, a.lat, str(i + 1)) for i, a in enumerate(self.answers)]

    def _fill_answers(self) -> None:
        self._filling, was = True, self._filling
        try:
            self.table.setRowCount(len(self.answers))
            for r, a in enumerate(self.answers):
                values = {C_NO: str(r + 1), C_KIND: kind_text(a.kind), C_ACTIVITY: a.activity, C_LENGTH: _g(a.length), C_WIDTH: _g(a.width),
                          C_HEADING: _g(a.heading), C_POINTS: _g(a.points), C_TOLERANCE: _g(a.tolerance), C_EXPLAIN: a.explanation,
                          C_REFERENCE: os.path.basename(a.reference) if a.reference else ""}
                for c, text in values.items():
                    item = QTableWidgetItem(text)
                    if c in (C_NO, C_REFERENCE):
                        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                    if c == C_REFERENCE and a.reference:
                        item.setToolTip(a.reference)
                    self.table.setItem(r, c, item)
                change = QComboBox()
                change.addItems(["", *CHANGES])
                change.setCurrentText(a.change)
                change.currentTextChanged.connect(lambda text, a=a: self._set(a, "change", text))
                self.table.setCellWidget(r, C_CHANGE, change)
                required = QComboBox()
                required.addItems([self.tr(w) for w in REQUIRED_WORDS])
                required.setCurrentIndex(0 if a.required else 1)
                required.currentIndexChanged.connect(lambda i, a=a: self._set(a, "required", i == 0))
                self.table.setCellWidget(r, C_REQUIRED, required)
        finally:
            self._filling = was
        self.tabs.setTabText(self.tabs.indexOf(self.answers_box), self.tr("正解（{n} 件）").format(n=len(self.answers)))
        self._columns_shown()

    def _set(self, a: DraftAnswer, name: str, value: Any) -> None:
        setattr(a, name, value)
        self._changed()

    def _cell_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        r, c = item.row(), item.column()
        if not 0 <= r < len(self.answers):
            return
        a, text = self.answers[r], item.text().strip()
        if c == C_KIND:
            a.kind = kind_text(text)
        elif c == C_ACTIVITY:
            a.activity = text
        elif c == C_EXPLAIN:
            a.explanation = text
        elif c in (C_LENGTH, C_WIDTH, C_HEADING, C_POINTS, C_TOLERANCE):
            v = _number(text)
            if text and (v is None or v < 0):
                self.say(self.tr("正解 {n}: 「{t}」は数ではない").format(n=r + 1, t=text), BAD)
                return
            name = {C_LENGTH: "length", C_WIDTH: "width", C_HEADING: "heading", C_POINTS: "points", C_TOLERANCE: "tolerance"}[c]
            if name == "points":
                v = v if v else 10.0
            setattr(a, name, v)
        self._changed()

    def _columns_shown(self) -> None:
        """The columns the kind of exercise uses (変化: 変化を見つける only; 測る's values for 測る, else when given)."""
        t = self.kind.currentText()
        self.table.setColumnHidden(C_CHANGE, t != CHANGE)
        measured = t == MEASURE or any(a.length is not None or a.width is not None or a.heading is not None for a in self.answers)
        for c in (C_LENGTH, C_WIDTH, C_HEADING):
            self.table.setColumnHidden(c, not measured)
        self.table.setColumnHidden(C_ACTIVITY, t == MEASURE and not any(a.activity for a in self.answers))

    def _kind_changed(self, *_args) -> None:
        t = self.kind.currentText()
        self.kind_hint.setText(self.tr(TYPE_HINTS.get(t, "")))
        change = t == CHANGE
        row = self.before_id.parentWidget()
        row.setVisible(change)
        if self.before_label is not None:
            self.before_label.setVisible(change)
        self._columns_shown()
        self._changed()

    def _roles_shown(self, *_args) -> None:
        names = [self.roles[c] for c, a in self.role_actions.items() if a.isChecked()]
        self.roles_button.setText("・".join(names) if names else self.tr("だれでも"))
        self._changed()

    def _changed(self, *_args) -> None:
        if not self._filling:
            self.dirty = True

    # ---- closing ------------------------------------------------------------------------------------------

    def _ask_close(self) -> bool:
        return ask_no(self, self.tr("判読の練習の問題を作る"), self.tr("未保存の変更あり。閉じると変更は消える"), yes=self.tr("破棄"))

    def close_now(self) -> None:
        """Closed without asking (練習モードで試す: saved already)."""
        self._closing = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        if self.dirty and not self._closing and not self.ask_close():
            event.ignore()
            return
        self.closed.emit()
        super().closeEvent(event)


# ---- 観測記録から取り込む -------------------------------------------------------------------------------

class RecordPicker(QDialog):
    """The image's records to take as answers (ticked: those not answers already)."""

    def __init__(self, found: Sequence[tuple], kind_of_exercise: str, answers: Sequence[DraftAnswer], parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("観測記録から正解を取り込む"))
        self.found = list(found)
        box = QVBoxLayout(self)
        lead = QLabel(self.tr("この画像の観測記録（範囲の中）。"
                              "経験のある判読者の記録を選び、種別・活動・長さを確認してから、正解にするものに印を付けて取り込み"))
        lead.setWordWrap(True)
        box.addWidget(lead)
        columns = ["", "種別", "活動", "長さ・幅・向き", *(["変化"] if kind_of_exercise == CHANGE else []), "確度", "記録者", "メモ"]
        self.table = QTableWidget(len(self.found), len(columns))
        self.table.setHorizontalHeaderLabels([self.tr(c) for c in columns])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        for c in range(len(columns)):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c == len(columns) - 1 else QHeaderView.ResizeMode.ResizeToContents)
        self.checks: list[QTableWidgetItem] = []
        for r, (o, a) in enumerate(self.found):
            same = any(metres((a.lon, a.lat), (b.lon, b.lat)) <= SAME_PLACE for b in answers)
            check_item = QTableWidgetItem(self.tr("（正解にあり）") if same else "")
            check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check_item.setCheckState(Qt.CheckState.Unchecked if same else Qt.CheckState.Checked)
            self.checks.append(check_item)
            measures = "・".join(p for p in (f"長さ {a.length:.0f} m" if a.length else "", f"幅 {a.width:.0f} m" if a.width else "",
                                               f"向き {a.heading:.0f}°" if a.heading is not None else "") if p) or "—"
            cells = [kind_text(a.kind) or "—", a.activity or "—", measures, *([a.change] if kind_of_exercise == CHANGE else []),
                     getattr(o, "confidence", "") or "—", getattr(o, "recorded_by", "") or "—", (getattr(o, "note", "") or "").strip()]
            self.table.setItem(r, 0, check_item)
            for c, text in enumerate(cells, start=1):
                self.table.setItem(r, c, QTableWidgetItem(text))
        box.addWidget(self.table, 1)
        row = QHBoxLayout()
        every = QPushButton(self.tr("すべて選ぶ"))
        every.clicked.connect(lambda: self._tick_all(True))
        none = QPushButton(self.tr("すべて外す"))
        none.clicked.connect(lambda: self._tick_all(False))
        row.addWidget(every)
        row.addWidget(none)
        row.addStretch(1)
        self.ok = QPushButton(self.tr("取り込む"))
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.accept)
        cancel = QPushButton(self.tr("取消"))
        cancel.clicked.connect(self.reject)
        row.addWidget(self.ok)
        row.addWidget(cancel)
        box.addLayout(row)
        self.resize(820, 420)

    def _tick_all(self, on: bool) -> None:
        for item in self.checks:
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def chosen(self) -> list[DraftAnswer]:
        return [a for (_o, a), item in zip(self.found, self.checks) if item.checkState() == Qt.CheckState.Checked]


# ---- the windows and the map, together -----------------------------------------------------------------

class ExerciseAuthoring(QObject):
    """
    問題づくり: the list and the editor, the map's tools and marks. `place` reaches the plugin (canvas, api, iface);
    `io` the tables (features/training.py: read_drafts, save_draft, set_active, delete_draft, try_out — each in a
    task, then(result) or failed(message) — scoring, roles).
    """

    ended = pyqtSignal()

    def __init__(self, place: Any, io: Any, main: Optional[QWidget] = None):
        super().__init__()
        self.place = place
        self.io = io
        self.main = main
        self.canvas = place.canvas
        self.manager = ExerciseManager(main)
        self.editor: Optional[ExerciseEditor] = None
        #: The list hidden while an exercise is edited (shown again when the editor closes).
        self._list_hidden = False
        self.drafts: list[Draft] = []
        self.marks: Optional[ExerciseMarks] = None
        self._tool = None
        self._busy = False
        self.manager.new_pressed.connect(self.new)
        self.manager.edit_pressed.connect(self.edit)
        self.manager.copy_pressed.connect(self.copy)
        self.manager.active_pressed.connect(self.set_active)
        self.manager.delete_pressed.connect(self.delete)
        self.manager.reload_pressed.connect(self.read)
        self.manager.finished.connect(self._manager_closed)

    # ---- the list -----------------------------------------------------------------------------------------

    def open(self) -> None:
        self.manager.show()
        self.manager.raise_()
        self.read()

    def read(self) -> None:
        self.manager.say(self.tr("問題の表を取得中…"))
        self.manager.reload_button.setEnabled(False)

        def done(drafts) -> None:
            self.manager.reload_button.setEnabled(True)
            self.drafts = list(drafts)
            self.manager.show_drafts(self.drafts)
            self.manager.say(self._count())

        def failed(message: str) -> None:
            self.manager.reload_button.setEnabled(True)
            self.manager.say(self.tr("問題の表の取得に失敗: {m}").format(m=message), BAD)

        self.io.read_drafts(done, failed)

    def _count(self) -> str:
        offered = sum(1 for d in self.drafts if d.active)
        return self.tr("{n} 問（出す {a}・作りかけ {b}）").format(n=len(self.drafts), a=offered, b=len(self.drafts) - offered)

    def _draft(self, key: str) -> Optional[Draft]:
        return next((d for d in self.drafts if d.key == key), None)

    def _replace(self, d: Draft) -> None:
        self.drafts = [d if x.id == d.id else x for x in self.drafts] if any(x.id == d.id for x in self.drafts) else [*self.drafts, d]
        self.manager.show_drafts(self.drafts, keep=d.key)
        self.manager.say(self._count())

    def _editing(self) -> bool:
        if self.editor is not None:
            self.editor.show()
            self.editor.raise_()
            self.manager.say(self.tr("先に編集中の問題を閉じてから選択"), ORANGE)
            return True
        return False

    def new(self) -> Optional[ExerciseEditor]:
        if self._editing():
            return None
        last = self.drafts[-1] if self.drafts else None
        course = (self.manager.current() or last).course if (self.manager.current() or last) is not None else ""
        order = max((d.order for d in self.drafts if d.course == course), default=0) + 1
        d = Draft(new_exercise_id(datetime.now(), {x.id for x in self.drafts}), course=course, order=order, active=False)
        image = self._current_image()
        if image is not None:
            d.image_id, d.image_source = image[0], image[1]
        return self._open_editor(d)

    def edit(self, key: str) -> Optional[ExerciseEditor]:
        d = self._draft(key)
        if d is None or self._editing():
            return None
        return self._open_editor(d)

    def copy(self, key: str) -> Optional[ExerciseEditor]:
        d = self._draft(key)
        if d is None or self._editing():
            return None
        return self._open_editor(copied(d, new_exercise_id(datetime.now(), {x.id for x in self.drafts})))

    def set_active(self, key: str, active: bool) -> None:
        d = self._draft(key)
        if d is None:
            return
        if active:
            problems, _notes = check(d, self.io.scoring)
            if problems:
                self.manager.say(self.tr("練習の一覧に出せない: ") + " / ".join(problems[:3]), BAD)
                return

        def done(written) -> None:
            self._replace(written)
            self.manager.say((self.tr("「{t}」を練習の一覧に追加") if active else self.tr("「{t}」を作りかけに変更")).format(t=d.title or d.id)
                             + self._count(), GOOD)

        self.io.set_active(d, active, done, lambda m: self.manager.say(self.tr("変更に失敗: {m}").format(m=m), BAD))

    def delete(self, key: str) -> None:
        d = self._draft(key)
        if d is None:
            return
        if not ask_no(self.manager, self.tr("判読の練習の問題を作る"),
                      self.tr("「{t}」とその正解 {n} 件を削除（解いた人の結果は残る）").format(t=d.title or d.id, n=len(d.answers)),
                      yes=self.tr("削除")):
            return

        def done(_result) -> None:
            self.drafts = [x for x in self.drafts if x.id != d.id]
            self.manager.show_drafts(self.drafts)
            self.manager.say(self.tr("「{t}」を削除").format(t=d.title or d.id) + self._count(), GOOD)

        self.io.delete_draft(d, done, lambda m: self.manager.say(self.tr("削除に失敗: {m}").format(m=m), BAD))

    def _manager_closed(self, *_args) -> None:
        if self.editor is None:
            self.stop()

    # ---- the editor ---------------------------------------------------------------------------------------

    def _open_editor(self, d: Draft) -> ExerciseEditor:
        courses = list(dict.fromkeys(x.course for x in self.drafts if x.course))
        editor = ExerciseEditor(d, self.io.scoring, courses, self._kinds(), self.io.roles, self.main)
        self.editor = editor
        editor.save_pressed.connect(lambda: self.save())
        editor.try_pressed.connect(lambda: self.save(then_try=True))
        editor.draw_pressed.connect(self.draw_area)
        editor.view_area_pressed.connect(self.view_area)
        editor.image_pressed.connect(self.take_image)
        editor.import_pressed.connect(self.import_records)
        editor.add_toggled.connect(self.add_on_map)
        editor.show_pressed.connect(self.show_on_map)
        editor.reference_pressed.connect(self.choose_reference)
        editor.marks_changed.connect(self.draw_marks)
        editor.closed.connect(self._editor_closed)
        if self.marks is None:
            self.marks = ExerciseMarks(self.canvas)
        editor.show()
        self._place(editor)
        # The list out of the way while one is edited (the map beside the editor to draw on); back when it closes.
        if self.manager.isVisible():
            self._list_hidden = True
            self.manager.hide()
        self.draw_marks()
        if d.area or d.answers:
            self.show_on_map()
        if d.object_id is None and not d.image_id and not d.image_source:
            editor.say(self.tr("画像カタログで問題の画像を開き、「観測記録ドックの画像」で選択"), "#1d4ed8")
        return editor

    def _place(self, editor: QWidget) -> None:
        """Over the docks at the right of QGIS's window (the map's left part left to draw on)."""
        if self.main is None:
            return
        try:
            area = self.main.frameGeometry()
            editor.move(area.right() - editor.frameGeometry().width() - 12, area.top() + 90)
        except RuntimeError:
            pass

    def _kinds(self) -> list[str]:
        book = getattr(self.place.api.observation_dock, "book", None) if self.place.api.observation_dock is not None else None
        try:
            return [kind_of(t.path) for t in book.taxonomy()] if book is not None else []
        except Exception:  # noqa: BLE001
            return []

    def save(self, then_try: bool = False) -> bool:
        """保存 (then_try: and 練習モードで試す once written): checked first; False when not written."""
        editor = self.editor
        if editor is None or self._busy:
            return False
        d = editor.draft()
        problems, notes = check(d, self.io.scoring)
        problems = [*editor.typed_problems, *problems]
        if problems:
            editor.say(self.tr("保存できない: ") + " / ".join(problems), BAD)
            return False
        exercise = exercise_of_draft(d, self.io.scoring) if then_try else None
        self._busy = True
        editor.save_button.setEnabled(False)
        editor.try_button.setEnabled(False)
        editor.say(self.tr("保存中…"), GREY)

        def done(written: Draft) -> None:
            self._busy = False
            if self.editor is not editor:
                return
            editor.save_button.setEnabled(True)
            editor.try_button.setEnabled(True)
            editor.saved(written)
            self._replace(written)
            text = self.tr("問題を保存") + (" " + " / ".join(notes) if notes else "")
            editor.say(text, ORANGE if notes else GOOD)
            if then_try and exercise is not None:
                editor.close_now()
                self.io.try_out(exercise)

        def failed(message: str) -> None:
            self._busy = False
            if self.editor is not editor:
                return
            editor.save_button.setEnabled(True)
            editor.try_button.setEnabled(True)
            editor.say(self.tr("保存に失敗: {m}").format(m=message), BAD)

        self.io.save_draft(d, done, failed)
        return True

    def _editor_closed(self) -> None:
        self._end_tool()
        self.editor = None
        if self.marks is not None:
            self.marks.remove()
            self.marks = None
        if self._list_hidden:
            self._list_hidden = False
            self.manager.show()
            self.manager.raise_()
        elif not self.manager.isVisible():
            self.stop()

    # ---- the map ------------------------------------------------------------------------------------------

    def draw_marks(self) -> None:
        if self.marks is None or self.editor is None:
            return
        row = self.editor.chosen_row()
        self.marks.show_marks(self.editor.area, self.editor.spots(), chosen=row if row >= 0 else None)

    def show_on_map(self) -> None:
        editor = self.editor
        if editor is None:
            return
        points = [p for ring in editor.area for p in ring] or [(a.lon, a.lat) for a in editor.answers]
        row = editor.chosen_row()
        if 0 <= row < len(editor.answers) and not editor.area:
            a = editor.answers[row]
            points = [(a.lon, a.lat)]
        if not points:
            return
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        dx, dy = (max(xs) - min(xs)) * 0.1 or 0.004, (max(ys) - min(ys)) * 0.1 or 0.003
        # (beside the editor: the part of the map it does not cover)
        fit_into(self.canvas, min(xs) - dx, min(ys) - dy, max(xs) + dx, max(ys) + dy, free_part(self.canvas, [editor, self.manager]))

    def _use_tool(self, tool) -> None:
        self._end_tool()
        self._tool = tool
        # (Its button stays pressed while another tool takes the map: it may be given the map back.)
        map_tools.take(self.canvas, tool, turns_off=False)

    def _end_tool(self) -> None:
        tool, self._tool = self._tool, None
        if tool is None:
            return
        try:
            map_tools.give_back(self.canvas, tool)
            map_tools.forget(self.canvas, tool)
            if hasattr(tool, "remove"):
                tool.remove()
        except RuntimeError:
            pass
        editor = self.editor
        if editor is not None and editor.add_button.isChecked():
            editor.add_button.blockSignals(True)
            editor.add_button.setChecked(False)
            editor.add_button.blockSignals(False)

    def draw_area(self) -> None:
        if self.editor is None:
            return

        def drawn(corners: list) -> None:
            self._end_tool()
            if self.editor is not None:
                self.editor.set_area([[*corners, corners[0]]])
                self.editor.say(self.tr("範囲を描画"), GOOD)

        self._use_tool(DrawTool(self.canvas, drawn))
        self.editor.say(self.tr("地図で範囲の角をクリック。ダブルクリックか右クリックで閉じる（Esc でやり直し）"), "#1d4ed8")

    def view_area(self) -> None:
        if self.editor is None:
            return
        box = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
        w, s, e, n = box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()
        self.editor.set_area([[[w, s], [w, n], [e, n], [e, s], [w, s]]])

    def add_on_map(self, on: bool) -> None:
        if self.editor is None:
            return
        if not on:
            self._end_tool()
            return

        def clicked(lon: float, lat: float) -> None:
            editor = self.editor
            if editor is None:
                return
            d = editor.draft()
            kind = (editor.answers[-1].kind if editor.answers else "") or (d.target_kinds[0] if d.target_kinds else "")
            editor.add_answers([DraftAnswer(lon, lat, kind=kind, change="" if d.type != CHANGE else "来た")])
            editor.say(self.tr("正解 {n} を追加。種別・活動・解説を表で修正").format(n=len(editor.answers)), GOOD)

        self._use_tool(ClickTool(self.canvas, clicked, self._end_tool))
        self.editor.add_button.blockSignals(True)
        self.editor.add_button.setChecked(True)
        self.editor.add_button.blockSignals(False)
        self.editor.say(self.tr("地図で正解の所をクリック（Esc か右クリックで終了）"), "#1d4ed8")

    # ---- the images and the records ------------------------------------------------------------------------

    def _current_image(self) -> Optional[tuple[str, str, str]]:
        """The observation dock's image now: (its catalog id or "", its file or "", its id in the dock); None when none."""
        obs = self.place.api.observation_dock
        book = getattr(obs, "book", None) if obs is not None else None
        image = book.current if book is not None else None
        if image is None:
            return None
        layer = QgsProject.instance().mapLayer(image.layer_id) if image.layer_id else None
        if layer is None or layer.customProperty(IMAGE_ID):
            return image.id, "", image.id
        return "", image_path(layer), image.id

    def take_image(self, which: str) -> None:
        editor = self.editor
        if editor is None:
            return
        image = self._current_image()
        if image is None:
            editor.say(self.tr("観測記録ドックに画像なし。先に画像カタログで画像を表示"), BAD)
            return
        editor.set_image(which, image[0], image[1])
        editor.say(self.tr("観測記録ドックの画像「{i}」に変更").format(i=image[2]), GOOD)

    def _dock_id(self, image_id: str, source: str) -> str:
        """An exercise's image's id in the observation dock (an image opened from its file: the dock's id for its layer)."""
        if image_id:
            return image_id
        obs = self.place.api.observation_dock
        book = getattr(obs, "book", None) if obs is not None else None
        for image in book.images if book is not None else []:
            for lid in image.layer_ids:
                layer = QgsProject.instance().mapLayer(lid)
                if layer is not None and image_path(layer) == source:
                    return image.id
        return os.path.basename(source)

    def import_records(self) -> None:
        editor = self.editor
        obs = self.place.api.observation_dock
        if editor is None:
            return
        if obs is None or getattr(obs, "book", None) is None:
            editor.say(self.tr("観測記録ドックは使えない（担当に「観測記録」が必要）"), BAD)
            return
        d = editor.draft()
        if not d.image_id and not d.image_source:
            editor.say(self.tr("先に画像を選択"), BAD)
            return
        image = self._dock_id(d.image_id, d.image_source)
        before = self._dock_id(d.before_image_id, d.before_image_source) if d.type == CHANGE else ""
        ids = tuple(i for i in (image, before) if i)
        editor.say(self.tr("観測記録を取得中…"), GREY)

        def found(records) -> None:
            if self.editor is not editor:
                return
            pairs = answers_from_records(records or [], d.type, image, before, d.area)
            if not pairs:
                where = self.tr("範囲の中に") if d.area else ""
                editor.say(self.tr("画像「{i}」の{w}観測記録なし").format(i=image, w=where), ORANGE)
                return
            picker = RecordPicker(pairs, d.type, editor.answers, editor)
            if picker.exec() != QDialog.DialogCode.Accepted:
                editor.say("")
                return
            chosen = picker.chosen()
            editor.add_answers(chosen)
            editor.say(self.tr("正解を {n} 件取り込み。配点・扱い・解説を確認").format(n=len(chosen)), GOOD)

        obs.fetch(self.tr("判読の練習: 正解にする観測記録の取得"), lambda b: b.fetch(RecordQuery(image_ids=ids, living=True)), found,
                  lambda m: editor.say(self.tr("観測記録の取得に失敗: {m}").format(m=m), BAD))

    def choose_reference(self) -> None:
        editor = self.editor
        if editor is None:
            return
        if editor.chosen_row() < 0:
            editor.say(self.tr("参考写真を付ける正解を表で選択"), ORANGE)
            return
        path, _f = QFileDialog.getOpenFileName(editor, self.tr("参考写真"), "", self.tr("画像 (*.png *.jpg *.jpeg *.gif *.bmp)"))
        if path:
            editor.set_reference(path)

    # ---- ending -------------------------------------------------------------------------------------------

    def stop(self) -> None:
        """The windows closed (without asking: QGIS closing, 練習モード started), the map as it was."""
        self._end_tool()
        if self.editor is not None:
            editor, self.editor = self.editor, None
            try:
                editor.closed.disconnect(self._editor_closed)
            except TypeError:
                pass
            editor.close_now()
        if self.marks is not None:
            self.marks.remove()
            self.marks = None
        try:
            self.manager.finished.disconnect(self._manager_closed)
        except TypeError:
            pass
        self.manager.close()
        self.ended.emit()

