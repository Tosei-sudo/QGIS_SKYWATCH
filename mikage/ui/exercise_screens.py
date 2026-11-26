"""
判読の練習's screens (ui/exercise_run.py runs an exercise with them): the list
of exercises to choose from (問題の一覧), the small window kept above QGIS
while one is solved (instruction, time, hint, 提出), the review after it is
submitted (答え合わせ: the score, each answer as it came out with its
explanation) and the person's own results with the classes they most often
take for others (これまでの結果).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Optional, Sequence

from qgis.PyQt.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.training.api import (
    best_of, CHANGE, exam_taken, EXAM, Exercise, ITEM_NAMES, kind_text, MEASURE, MISSED, MISSED_BONUS, mistaken, Outcome,
    RIGHT, tries_of,
)
from .emphasis import emphasize
from .fit import fit_text

#: The names the tests and the help's pictures find the windows by.
BAR_NAME, REVIEW_NAME = "MikageExerciseBar", "MikageExerciseReview"
#: The windows' widths (px): the bar over the map, the review over the docks at the right.
BAR_WIDTH, REVIEW_WIDTH = 470, 600
ALL_COURSES = "（すべてのコース）"
GOOD, BAD, ORANGE, GREY = "#15803d", "#b91c1c", "#c2410c", "#6b7280"
BAR_COLORS = {"find": "#16a34a", "kind": "#2563eb", "activity": "#7c3aed", "measure": "#0891b2", "change": "#d97706"}


def minutes_text(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"


def clock_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _item(text: str, color: Optional[str] = None, bold: bool = False, align: Optional[Qt.AlignmentFlag] = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
    if color:
        item.setForeground(QColor(color))
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    if align is not None:
        item.setTextAlignment(int(align | Qt.AlignmentFlag.AlignVCenter))
    return item


def _under(top: int, widget: Optional[QWidget]) -> int:
    """`top`, or the bottom of a widget shown (on the screen) when lower."""
    try:
        if widget is not None and widget.isVisible():
            return max(top, widget.mapToGlobal(QPoint(0, widget.height())).y())
    except RuntimeError:
        pass
    return top


def _height_for(window: QWidget, width: int) -> int:
    """A window's height for a width (its wrapped texts' lines), no taller than it needs."""
    layout = window.layout()
    if layout is not None and layout.hasHeightForWidth():
        return max(layout.heightForWidth(width), layout.minimumSize().height())
    return window.sizeHint().height()


def _table(columns: Sequence[str]) -> QTableWidget:
    t = QTableWidget(0, len(columns))
    t.setHorizontalHeaderLabels(list(columns))
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(False)
    return t


def _score_text(score: Optional[float]) -> str:
    return "—" if score is None else f"{score:g} 点"


# ---- 問題の一覧 ------------------------------------------------------------------------------------------

class ExerciseList(QDialog):
    """The exercises to choose from, by course; `choice` is the one to start (its key), None when closed."""

    def __init__(self, exercises: Sequence[Exercise], results: Sequence[dict], roles: Sequence[str], source_text: str,
                 show_results: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("判読の練習【練習】"))
        self.exercises = list(exercises)
        self.results = list(results)
        self.roles = set(roles)
        self.choice: Optional[str] = None
        box = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("コース")))
        self.course = QComboBox()
        courses = list(dict.fromkeys(e.course for e in self.exercises if e.course))
        self.course.addItems([*courses, self.tr(ALL_COURSES)] if len(courses) > 1 else courses or [self.tr(ALL_COURSES)])
        top.addWidget(self.course)
        self.mine = QCheckBox(self.tr("自分の担当の問題だけ"))
        self.mine.setChecked(any(e.roles for e in self.exercises))
        self.mine.setVisible(any(e.roles for e in self.exercises))
        top.addWidget(self.mine)
        top.addStretch(1)
        self.source = where = QLabel(source_text)
        where.setStyleSheet("color: #555;")
        top.addWidget(where)
        box.addLayout(top)
        self.table = _table([self.tr("順"), self.tr("問題"), self.tr("種類"), self.tr("難しさ"), self.tr("形式"), self.tr("制限時間"),
                             self.tr("最高点"), self.tr("状態")])
        header = self.table.horizontalHeader()
        for c in range(self.table.columnCount()):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c == 1 else QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.table, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detail.setMinimumHeight(84)
        self.detail.setStyleSheet("background: #f5f7fa; border: 1px solid #dde3ea; border-radius: 4px; padding: 8px;")
        box.addWidget(self.detail)
        row = QHBoxLayout()
        past = QPushButton(self.tr("これまでの結果…"))
        past.clicked.connect(show_results)
        row.addWidget(past)
        row.addStretch(1)
        self.start = QPushButton(self.tr("開始"))
        self.start.setDefault(True)
        self.start.clicked.connect(self._go)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.reject)
        row.addWidget(self.start)
        row.addWidget(close)
        box.addLayout(row)
        self.course.currentIndexChanged.connect(self._fill)
        self.mine.toggled.connect(self._fill)
        self.table.itemSelectionChanged.connect(self._chosen)
        self.table.itemDoubleClicked.connect(lambda _item: self._go())
        self._fill()
        self.resize(900, 460)

    def shown(self) -> list[Exercise]:
        course = self.course.currentText()
        out = [e for e in self.exercises if course == self.tr(ALL_COURSES) or e.course == course]
        if self.mine.isChecked():
            out = [e for e in out if not e.roles or self.roles & set(e.roles)]
        courses = {c: i for i, c in enumerate(dict.fromkeys(e.course for e in self.exercises))}
        return sorted(out, key=lambda e: (courses[e.course], e.order, e.title))

    def state_of(self, e: Exercise) -> str:
        tries = tries_of(self.results, e.key)
        if e.exam:
            return self.tr("済（1 回だけ）") if tries else self.tr("未（1 回だけ）")
        return self.tr("済（{n} 回）").format(n=len(tries)) if tries else self.tr("未")

    def _fill(self) -> None:
        shown = self.shown()
        self.table.setRowCount(len(shown))
        for r, e in enumerate(shown):
            best = best_of(self.results, e.key)
            cells = [_item(str(e.order or r + 1)), _item(e.title), _item(e.type), _item(e.difficulty or "—"),
                     _item(e.mode, ORANGE if e.exam else None, bold=e.exam),
                     _item(f"{e.time_limit:g} 分" if e.time_limit else "—"), _item(_score_text(best)), _item(self.state_of(e))]
            if best is not None:
                cells[6].setForeground(QColor(GOOD if best >= e.pass_score else BAD))
            cells[0].setData(Qt.ItemDataRole.UserRole, e.key)
            for c, item in enumerate(cells):
                self.table.setItem(r, c, item)
        if shown:
            first = next((r for r, e in enumerate(shown) if not tries_of(self.results, e.key)), 0)
            self.table.selectRow(first)
        self._chosen()

    def current(self) -> Optional[Exercise]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        key = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((e for e in self.exercises if e.key == key), None)

    def _chosen(self) -> None:
        e = self.current()
        if e is None:
            self.detail.setText(self.tr("練習の問題がない"))
            self.start.setEnabled(False)
            return
        taken = e.exam and exam_taken(self.results, e.key)
        self.start.setEnabled(not taken)
        self.start.setText(self.tr("開始") if not tries_of(self.results, e.key) or e.exam else self.tr("もう一度"))
        image = e.image_id or os.path.basename(e.image_source)
        before = f"（前の画像: {e.before_image_id or os.path.basename(e.before_image_source)}）" if e.type == CHANGE else ""
        limit = f"制限時間 {e.time_limit:g} 分・" if e.time_limit else ""
        note = ("<br><span style='color:#b91c1c'>この試験は受験済み（1 回だけ）</span>" if taken else
                "<br><span style='color:#c2410c'>試験: ヒントなし・1 回だけ・時間が来たら自動で提出</span>" if e.exam else "")
        self.detail.setText(
            f"<b>{e.order or ''}{'. ' if e.order else ''}{e.title}</b>（{e.type}・{e.difficulty or '—'}）<br>"
            f"指示: {e.instruction}<br>"
            f"<span style='color:#555'>画像: {image}{before} ／ 対象: {e.target_text()} ／ {limit}採点: {e.rules_text()}</span>{note}")

    def _go(self) -> None:
        e = self.current()
        if e is None or (e.exam and exam_taken(self.results, e.key)):
            return
        self.choice = e.key
        self.accept()


# ---- 解いている間の窓 -----------------------------------------------------------------------------------

class ExerciseBar(QWidget):
    """The exercise being solved: what to do, the time, a hint (練習) and 提出. Above QGIS, not blocking it."""

    submit_pressed = pyqtSignal()
    quit_pressed = pyqtSignal()
    hint_pressed = pyqtSignal()
    area_pressed = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, exercise: Exercise, main: Optional[QWidget] = None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        fit_text(self)
        self.main = main
        self.exercise = exercise
        self.setObjectName(BAR_NAME)
        self.setWindowTitle(self.tr("判読の練習【練習】"))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 10, 14, 10)
        head = QHBoxLayout()
        kind = QLabel(f"判読の練習 · {exercise.type} · " + (f"<b style='color:{ORANGE}'>試験</b>" if exercise.exam else "練習"))
        kind.setStyleSheet("color: #555;")
        head.addWidget(kind, 1)
        self.time = QLabel()
        font = self.time.font()
        font.setBold(True)
        self.time.setFont(font)
        head.addWidget(self.time)
        box.addLayout(head)
        self.title = QLabel(f"{exercise.order}. {exercise.title}" if exercise.order else exercise.title)
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setWordWrap(True)
        box.addWidget(self.title)
        self.text = QLabel(exercise.instruction)
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        box.addWidget(self.text)
        rules = QLabel(f"対象: {exercise.target_text()} ／ 採点: {exercise.rules_text()}")
        rules.setWordWrap(True)
        rules.setStyleSheet("color: #555;")
        box.addWidget(rules)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.state)
        row = QHBoxLayout()
        self.hint = QPushButton(self.tr("ヒント"))
        self.hint.setToolTip(self.tr("あと何件あるかを表示（練習だけ）"))
        self.hint.setVisible(not exercise.exam)
        self.hint.clicked.connect(self.hint_pressed)
        self.area = QPushButton(self.tr("範囲へ移動"))
        self.area.clicked.connect(self.area_pressed)
        self.quit = QPushButton(self.tr("取消"))
        self.quit.clicked.connect(self.quit_pressed)
        self.submit = QPushButton(self.tr("提出して答え合わせ"))
        self.submit.setDefault(True)
        emphasize(self.submit)
        self.submit.clicked.connect(self.submit_pressed)
        for b in (self.hint, self.area, self.quit):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.submit)
        box.addLayout(row)
        self.setMinimumWidth(BAR_WIDTH)
        self.resize(BAR_WIDTH, _height_for(self, BAR_WIDTH))

    def place(self, over: Optional[QWidget] = None, below: Optional[QWidget] = None) -> None:
        """
        In the top right corner of the map (`over`; under `below` on it: 練習モード's band), else of QGIS's window
        (below the toolbars).
        """
        target = over if over is not None else self.main
        if target is None:
            return
        try:
            area = QRect(target.mapToGlobal(QPoint(0, 0)), target.size())
        except RuntimeError:
            return
        if over is not None:
            self.move(QPoint(area.right() - self.frameGeometry().width() - 12, _under(area.top(), below) + 8))
        else:
            self.move(QPoint(area.right() - self.width() - 24, area.top() + 130))

    def say(self, text: str, color: str = "#333") -> None:
        self.state.setText(f"<span style='color:{color}'>{text}</span>" if text else "")

    def show_time(self, seconds: float, left: Optional[float]) -> None:
        if left is None:
            self.time.setText(self.tr("経過 {t}").format(t=clock_text(seconds)))
            self.time.setStyleSheet("color: #555;")
        else:
            self.time.setText(self.tr("残り {t}").format(t=clock_text(left)))
            self.time.setStyleSheet(f"color: {BAD if left < 60 else '#333'};")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        self.closed.emit()
        super().closeEvent(event)


# ---- 答え合わせ -----------------------------------------------------------------------------------------

STATE_COLORS = {RIGHT: GOOD, MISSED: BAD, MISSED_BONUS: GREY, "誤り": GREY}


def answer_text(item, given: bool = False) -> tuple[str, str]:
    """(kind or what changed, activity or the measures) of an answer, or of the record taken for it."""
    a, m = item.answer, item.mark
    if given and m is None:
        return "—", "—"
    src = m if given else a
    kind = kind_text(src.kind) or ("（なし）" if given else "—")
    if a.change:
        kind = f"{src.change or '—'}（{kind}）" if (src.change or not given) else kind
    if a.measured():
        parts = []
        if a.length is not None:
            parts.append(f"長さ {src.length:.0f} m" if src.length is not None else "長さ —")
        if a.width is not None:
            parts.append(f"幅 {src.width:.0f} m" if src.width is not None else "幅 —")
        if a.heading is not None:
            parts.append(f"向き {src.heading:.0f}°" if src.heading is not None else "向き —")
        return kind, "・".join(parts)
    return kind, (src.activity or "—")


class ReviewWindow(QWidget):
    """The try as it came out: the score, its parts, each answer and its explanation. Above QGIS, not blocking it."""

    chosen = pyqtSignal(int)
    again_pressed = pyqtSignal()
    next_pressed = pyqtSignal()
    missed_pressed = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, outcome: Outcome, seconds: float, has_next: bool, main: Optional[QWidget] = None,
                 reference: Optional[Callable[[str], Optional[QPixmap]]] = None, note: str = ""):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        fit_text(self)
        self.main = main
        self.outcome = outcome
        self.reference = reference
        e = outcome.exercise
        show = e.show_answers
        self.setObjectName(REVIEW_NAME)
        self.setWindowTitle(self.tr("答え合わせ【練習】"))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 10, 14, 10)
        head = QLabel(f"判読の練習 <b>{e.order}. {e.title}</b>（{e.mode}）" if e.order else f"判読の練習 <b>{e.title}</b>（{e.mode}）")
        head.setStyleSheet("color: #555;")
        box.addWidget(head)
        line = QHBoxLayout()
        self.score = QLabel(f"{outcome.score:g} 点")
        font = self.score.font()
        font.setPointSizeF(font.pointSizeF() * 2.0)
        font.setBold(True)
        self.score.setFont(font)
        line.addWidget(self.score)
        passed = outcome.passed
        self.verdict = QLabel(f"<b style='color:{GOOD if passed else BAD}'>{'合格' if passed else '不合格'}</b>"
                              f"（{e.pass_score:g} 点以上）　{minutes_text(seconds)}")
        line.addWidget(self.verdict)
        line.addStretch(1)
        box.addLayout(line)
        grid = QGridLayout()
        self.bars: dict[str, QProgressBar] = {}
        r = 0
        for key, (earned, possible) in outcome.parts.items():
            if possible <= 0:
                continue
            grid.addWidget(QLabel(ITEM_NAMES.get(key, key)), r, 0)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(int(round(earned / possible * 1000)))
            bar.setTextVisible(False)
            bar.setFixedHeight(10)
            bar.setStyleSheet(f"QProgressBar {{ background: #e5e7eb; border: none; border-radius: 5px; }}"
                              f" QProgressBar::chunk {{ background: {BAR_COLORS.get(key, '#2563eb')}; border-radius: 5px; }}")
            grid.addWidget(bar, r, 1)
            grid.addWidget(QLabel(f"{earned:g} / {possible:g}"), r, 2)
            self.bars[key] = bar
            r += 1
        if outcome.penalty or outcome.extras:
            grid.addWidget(QLabel(self.tr("誤り")), r, 0)
            grid.addWidget(QLabel(f"−{outcome.penalty:g}（{len(outcome.extras)} 件）"), r, 2)
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)
        self.table = _table(["#", self.tr("結果"), self.tr("正しい答え"), self.tr("自分の答え"),
                             self.tr("測った値" if e.type == MEASURE else "活動"), self.tr("点")])
        header = self.table.horizontalHeader()
        for c in range(self.table.columnCount()):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c in (2, 3) else QHeaderView.ResizeMode.ResizeToContents)
        self.rows: list = []
        for item in outcome.items:
            self.rows.append(item)
        for m in outcome.extras:
            self.rows.append(m)
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            if hasattr(row, "answer"):
                truth_kind, truth_more = answer_text(row)
                given_kind, given_more = answer_text(row, given=True)
                color = STATE_COLORS.get(row.state, ORANGE)
                mark = "●" if row.found else "○"
                cells = [_item(str(row.answer.number)), _item(f"{mark} {row.state}", color), _item(truth_kind), _item(given_kind),
                         _item(f"{given_more}（正: {truth_more}）" if row.found and given_more != truth_more else given_more),
                         _item(f"{row.earned:.0f}", align=Qt.AlignmentFlag.AlignRight)]
            else:
                cells = [_item("—"), _item("× 誤り", GREY), _item(self.tr("（なし）")), _item(kind_text(row.kind) or "—"),
                         _item(row.activity or "—"), _item(f"−{e.scoring.extra_penalty:g}", align=Qt.AlignmentFlag.AlignRight)]
            for c, cell in enumerate(cells):
                self.table.setItem(r, c, cell)
        shown = min(max(len(self.rows), 2), 10)
        self.table.setFixedHeight(self.table.horizontalHeader().sizeHint().height() + 2 * self.table.frameWidth() + 2
                                  + self.table.verticalHeader().defaultSectionSize() * shown)
        box.addWidget(self.table)
        explain = QFrame()
        explain.setStyleSheet("QFrame { background: #f5f7fa; border: 1px solid #dde3ea; border-radius: 4px; }")
        er = QHBoxLayout(explain)
        self.picture = QLabel()
        self.picture.setFixedSize(QSize(96, 72))
        self.picture.setStyleSheet("border: none; background: #0f2233;")
        self.picture.setScaledContents(True)
        er.addWidget(self.picture)
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        self.explanation.setTextFormat(Qt.TextFormat.RichText)
        self.explanation.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.explanation.setStyleSheet("border: none;")
        self.explanation.setMinimumHeight(72)
        er.addWidget(self.explanation, 1)
        box.addWidget(explain)
        self.note = QLabel(note)
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #555;")
        self.note.setVisible(bool(note))
        box.addWidget(self.note)
        if not show:
            self.table.hide()
            explain.hide()
            hidden = QLabel(self.tr("この試験は、正解と解説を表示しない設定（点と合否だけ）"))
            hidden.setWordWrap(True)
            box.addWidget(hidden)
        row = QHBoxLayout()
        self.to_missed = QPushButton(self.tr("見落としへ移動"))
        self.to_missed.setFlat(True)
        self.to_missed.setStyleSheet("QPushButton { color: #2563eb; text-decoration: underline; border: none; }")
        self.to_missed.setVisible(show and outcome.missed > 0)
        self.to_missed.clicked.connect(self.missed_pressed)
        row.addWidget(self.to_missed)
        row.addStretch(1)
        self.again = QPushButton(self.tr("もう一度"))
        self.again.setVisible(not e.exam)
        self.again.clicked.connect(self.again_pressed)
        self.next = QPushButton(self.tr("次の問題へ") if has_next else self.tr("問題の一覧へ"))
        emphasize(self.next)
        self.next.clicked.connect(self.next_pressed)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        row.addWidget(self.again)
        row.addWidget(self.next)
        row.addWidget(close)
        box.addLayout(row)
        self.table.itemSelectionChanged.connect(self._row_chosen)
        self.setMinimumWidth(560)
        self.resize(REVIEW_WIDTH, _height_for(self, REVIEW_WIDTH))
        if show and self.rows:
            first = next((r for r, row in enumerate(self.rows) if hasattr(row, "state") and row.state != RIGHT), 0)
            self.table.selectRow(first)

    def place(self, over: Optional[QWidget] = None, below: Optional[QWidget] = None) -> None:
        """
        At the right of QGIS's window (over the docks, which are not used now), from the top of the map (`over`;
        under `below` on it).
        """
        if self.main is None:
            return
        try:
            area = QRect(self.main.mapToGlobal(QPoint(0, 0)), self.main.size())
            top = _under(over.mapToGlobal(QPoint(0, 0)).y(), below) if over is not None else area.top() + 110
        except RuntimeError:
            return
        self.move(QPoint(area.right() - self.frameGeometry().width() - 12, top))

    def select(self, index: int) -> None:
        if 0 <= index < len(self.rows):
            self.table.selectRow(index)

    def _row_chosen(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        row = self.rows[index]
        if hasattr(row, "answer"):
            a = row.answer
            title = f"解説（{a.number}. {kind_text(a.kind) or '—'}）"
            text = a.explanation or self.tr("（解説なし）")
            ref = f"<br><span style='color:#555'>参考写真: {os.path.basename(a.reference)}</span>" if a.reference else ""
            self.explanation.setText(f"<b>{title}</b><br>{text}{ref}")
            pixmap = self.reference(a.reference) if (self.reference and a.reference) else None
            self.picture.setPixmap(pixmap if pixmap is not None else QPixmap())
            self.picture.setVisible(pixmap is not None)
        else:
            self.explanation.setText(self.tr("<b>誤り</b><br>正解のどれにも当たらない記録（範囲の中で、対象の種別のもの）。地図で × の所を見直し"))
            self.picture.setVisible(False)
        self.chosen.emit(index)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        self.closed.emit()
        super().closeEvent(event)


# ---- これまでの結果 ------------------------------------------------------------------------------------

class MyResults(QDialog):
    """
    The person's tries (this computer's, and the organisation's results table's: another computer's too), and the
    classes they most often take for others (of the tries kept here: the table's rows have no detail).
    """

    def __init__(self, results: Sequence[dict], parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("これまでの結果【練習】"))
        box = QVBoxLayout(self)
        tries = sorted(results, key=lambda r: r.get("finished") or "", reverse=True)
        self.table = _table([self.tr("日時"), self.tr("問題"), self.tr("形式"), self.tr("点"), self.tr("合否"), self.tr("時間"),
                             self.tr("見つけた・見落とし・誤り")])
        header = self.table.horizontalHeader()
        for c in range(self.table.columnCount()):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c == 1 else QHeaderView.ResizeMode.ResizeToContents)
        self.table.setRowCount(len(tries))
        for r, t in enumerate(tries):
            try:
                when = datetime.fromisoformat(t.get("finished") or "").strftime("%Y-%m-%d %H:%M")
            except ValueError:
                when = t.get("finished") or ""
            passed = t.get("passed") is True
            cells = [_item(when), _item(t.get("title") or t.get("exercise") or ""), _item(t.get("mode") or "", ORANGE if t.get("mode") == EXAM else None),
                     _item(f"{t.get('score', 0):g}", align=Qt.AlignmentFlag.AlignRight), _item("合格" if passed else "不合格", GOOD if passed else BAD),
                     _item(minutes_text(t.get("seconds") or 0)), _item(f"{t.get('found', 0)}・{t.get('missed', 0)}・{t.get('extra', 0)}")]
            for c, cell in enumerate(cells):
                self.table.setItem(r, c, cell)
        elsewhere = any(t.get("fromTable") for t in tries)
        box.addWidget(QLabel(self.tr("解いた問題（新しい順。組織の結果の表から、ほかの PC で解いたものも）") if elsewhere
                             else self.tr("この PC で解いた問題（新しい順）")))
        box.addWidget(self.table, 1)
        box.addWidget(QLabel(self.tr("<b>取り違えやすい組み合わせ</b>（正しい種別 → 答えた種別、回数）")))
        pairs = mistaken(results)
        text = "<br>".join(f"{t} → {g}　{n} 回" for t, g, n in pairs) if pairs else self.tr("まだなし")
        self.pairs = QLabel(text)
        self.pairs.setWordWrap(True)
        self.pairs.setTextFormat(Qt.TextFormat.RichText)
        self.pairs.setStyleSheet("background: #f5f7fa; border: 1px solid #dde3ea; border-radius: 4px; padding: 8px;")
        box.addWidget(self.pairs)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        box.addLayout(row)
        self.resize(820, 480)
