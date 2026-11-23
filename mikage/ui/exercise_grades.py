"""
判読の練習の成績 (練習モードの段階 3 の 3; the instructors' 「判読の練習の成績…」,
features/training.py): everyone's tries of a course as the organisation's
results table has them — a row per person, a column per exercise (their best
score, or their latest, green when it passed, red when not, — not tried) and
how far through the course they are — the classes most often taken for
others (正しい ＼ 答えた) with what to do about them, and the grid as CSV.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.training.api import advice, Column, confusion, courses_of, grades, grades_csv, in_period, PERIODS
from ..qgis_io.tasks import error_text
from .exercise_screens import BAD, GOOD, GREY, minutes_text
from .fit import fit_text

GRADES_NAME = "MikageExerciseGrades"
PASS_BG, FAIL_BG, NONE_BG = "#dcfce7", "#fee2e2", "#f8fafc"
#: The classes shown in 取り違え's table (the most answered first; the rest left out).
MATRIX_KINDS = 7


def _cell(text: str, color: Optional[str] = None, background: Optional[str] = None, bold: bool = False,
          tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
    item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
    if color:
        item.setForeground(QColor(color))
    if background:
        item.setBackground(QColor(background))
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    if tip:
        item.setToolTip(tip)
    return item


def _short(kind: str) -> str:
    """A class's last part (the table's headers; the whole in their tips)."""
    return kind.split(" > ")[-1] if kind else kind


class GradesDialog(QDialog):
    """The grid of a course, its 取り違え and the advice; CSV of the grid."""

    def __init__(self, results: Sequence[dict], columns: Sequence[Column], note: str = "", parent: Optional[QWidget] = None,
                 now: Optional[datetime] = None):
        super().__init__(parent)
        fit_text(self)
        self.setObjectName(GRADES_NAME)
        self.setWindowTitle(self.tr("判読の練習の成績"))
        self.results = list(results)
        self.columns = list(columns)
        self.now = now
        self.table_now = None
        box = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("コース")))
        self.course = QComboBox()
        self.course.addItems([c or self.tr("（コースなし）") for c in courses_of(self.columns, self.results)])
        self._courses = courses_of(self.columns, self.results)
        top.addWidget(self.course)
        top.addSpacing(12)
        top.addWidget(QLabel(self.tr("期間")))
        self.period = QComboBox()
        self.period.addItems([self.tr(p) for p, _d in PERIODS])
        top.addWidget(self.period)
        self.best = QCheckBox(self.tr("一人一問の最高点"))
        self.best.setToolTip(self.tr("外すと、いちばん新しい点"))
        self.best.setChecked(True)
        top.addWidget(self.best)
        top.addStretch(1)
        self.csv_button = QPushButton(self.tr("CSV に書き出す…"))
        top.addWidget(self.csv_button)
        box.addLayout(top)
        self.note = QLabel(note)
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #555;")
        self.note.setVisible(bool(note))
        box.addWidget(self.note)
        self.grid = QTableWidget()
        self.grid.verticalHeader().hide()
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        box.addWidget(self.grid, 3)
        self.summary = QLabel()
        self.summary.setStyleSheet("color: #555;")
        box.addWidget(self.summary)
        lower = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel(self.tr("<b>取り違えの多い組み合わせ</b>（正しい種別 ＼ 答えた種別、回数）")))
        self.matrix = QTableWidget()
        self.matrix.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.matrix.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        left.addWidget(self.matrix, 1)
        lower.addLayout(left, 3)
        right = QVBoxLayout()
        right.addWidget(QLabel(self.tr("<b>指導のヒント</b>")))
        self.advice = QLabel()
        self.advice.setWordWrap(True)
        self.advice.setTextFormat(Qt.TextFormat.RichText)
        self.advice.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.advice.setStyleSheet("background: #f5f7fa; border: 1px solid #dde3ea; border-radius: 4px; padding: 8px;")
        right.addWidget(self.advice, 1)
        lower.addLayout(right, 2)
        box.addLayout(lower, 2)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        box.addLayout(row)
        self.course.currentIndexChanged.connect(self.fill)
        self.period.currentIndexChanged.connect(self.fill)
        self.best.toggled.connect(self.fill)
        self.csv_button.clicked.connect(self.ask_csv)
        self.fill()
        self.resize(1040, 700)

    def chosen_results(self) -> list[dict]:
        """The tries of the course chosen, in the period chosen."""
        course = self._courses[self.course.currentIndex()] if self._courses and self.course.currentIndex() >= 0 else ""
        days = PERIODS[self.period.currentIndex()][1] if self.period.currentIndex() >= 0 else None
        return [r for r in in_period(self.results, days, self.now) if (r.get("course") or "") == course]

    def fill(self, *_args) -> None:
        course = self._courses[self.course.currentIndex()] if self._courses and self.course.currentIndex() >= 0 else ""
        tries = self.chosen_results()
        table = grades(tries, self.columns, course, best=self.best.isChecked())
        self.table_now = table
        heads = [self.tr("解いた人")]
        for i, c in enumerate(table.columns):
            heads.append(f"{i + 1}{'（試）' if c.exam else ''}")
        heads.append(self.tr("進み具合"))
        self.grid.clear()
        self.grid.setColumnCount(len(heads))
        self.grid.setHorizontalHeaderLabels(heads)
        for i, c in enumerate(table.columns):
            head = self.grid.horizontalHeaderItem(i + 1)
            if head is not None:
                head.setToolTip(f"{i + 1}. {c.title}（{c.mode or '練習'}）")
        self.grid.setRowCount(len(table.people))
        for r, person in enumerate(table.people):
            name = QTableWidgetItem(person)
            name.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self.grid.setItem(r, 0, name)
            for i, c in enumerate(table.columns):
                cell = table.cell(person, c.key)
                if cell is None:
                    item = _cell("—", GREY, NONE_BG, tip=self.tr("まだ解いていない"))
                else:
                    when = (cell.finished or "")[:16].replace("T", " ")
                    tip = self.tr("{s} 点・{p}（{n} 回・{t}・{w}）").format(
                        s=f"{cell.score:g}", p="合格" if cell.passed else "不合格", n=cell.tries, t=minutes_text(cell.seconds), w=when)
                    item = _cell(f"{cell.score:g}", GOOD if cell.passed else BAD, PASS_BG if cell.passed else FAIL_BG, bold=True, tip=tip)
                self.grid.setItem(r, i + 1, item)
            passed, total = table.progress(person)
            self.grid.setItem(r, len(heads) - 1, _cell(f"{passed} / {total}", GOOD if total and passed == total else None))
        header = self.grid.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for c in range(1, len(heads)):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c < len(heads) - 1 else QHeaderView.ResizeMode.ResizeToContents)
        names = "　".join(f"{i + 1}. {c.title}" for i, c in enumerate(table.columns))
        self.summary.setText(self.tr("{p} 人・{t} 回の提出").format(p=len(table.people), t=len(tries)) + ("　／　" + names if names else ""))
        self.summary.setToolTip(names)
        kinds, counts, people = confusion(tries)
        self._fill_matrix(kinds, counts)
        lines = advice(tries, table, counts, people)
        self.advice.setText("<br><br>".join(f"・{line}" for line in lines) if lines else
                            self.tr("目立つ取り違えはまだない（提出が増えると、ここに表示）"))
        self.csv_button.setEnabled(bool(table.people))

    def _fill_matrix(self, kinds: list, counts) -> None:
        answered = {}
        for (t, _g), n in counts.items():
            answered[t] = answered.get(t, 0) + n
        truths = [k for k in kinds if k in answered][:MATRIX_KINDS]
        given_totals = {}
        for (t, g), n in counts.items():
            if t in truths:
                given_totals[g] = given_totals.get(g, 0) + n
        givens = [*[k for k in truths if k in given_totals],
                  *sorted((g for g in given_totals if g not in truths), key=lambda g: -given_totals[g])][:MATRIX_KINDS + 2]
        self.matrix.clear()
        self.matrix.setRowCount(len(truths))
        self.matrix.setColumnCount(len(givens))
        self.matrix.setHorizontalHeaderLabels([_short(g) for g in givens])
        self.matrix.setVerticalHeaderLabels([_short(t) for t in truths])
        for c, g in enumerate(givens):
            item = self.matrix.horizontalHeaderItem(c)
            if item is not None:
                item.setToolTip(g)
        for r, t in enumerate(truths):
            item = self.matrix.verticalHeaderItem(r)
            if item is not None:
                item.setToolTip(t)
            total = answered.get(t, 0) or 1
            for c, g in enumerate(givens):
                n = counts.get((t, g), 0)
                right = g == t or g.startswith(t + " > ")
                if not n:
                    cell = _cell("", background="#ffffff")
                elif right:
                    cell = _cell(str(n), GOOD, PASS_BG)
                else:
                    share = n / total
                    shade = QColor(BAD)
                    shade.setAlphaF(min(0.85, 0.15 + share))
                    cell = _cell(str(n), "#ffffff" if share > 0.35 else BAD, bold=True)
                    cell.setBackground(shade)
                cell.setToolTip(f"{t} → {g}: {n} 回")
                self.matrix.setItem(r, c, cell)
        self.matrix.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def csv_text(self) -> str:
        return grades_csv(self.table_now) if self.table_now is not None else ""

    def save_csv(self, path: str) -> None:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(self.csv_text())

    def ask_csv(self) -> Optional[str]:
        course = self._courses[self.course.currentIndex()] if self._courses else ""
        name = f"判読の練習の成績_{course or 'コースなし'}_{datetime.now():%Y%m%d}.csv".replace("/", "_").replace("\\", "_")
        path, _f = QFileDialog.getSaveFileName(self, self.tr("成績を CSV に書き出す"), name, self.tr("CSV (*.csv)"))
        if not path:
            return None
        try:
            self.save_csv(path)
        except OSError as error:
            self.note.setText(self.tr("書き出しに失敗: {e}").format(e=error_text(error)))
            self.note.setVisible(True)
            return None
        self.note.setText(self.tr("書き出し済み: {p}").format(p=path))
        self.note.setVisible(True)
        return path
