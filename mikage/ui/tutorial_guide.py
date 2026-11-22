"""
チュートリアル (練習モードの段階 2, features/training.py が始める): a course of
the help's 画面つきの手順書 walked through one operation at a time on the
practice data. A small window that stays above QGIS (GuideWindow) says the
step (its number and words are the help's), a red frame (the help pictures'
red) goes round what to press or look at (RedFrame), and the step is done
when the screen shows it: the next one comes by itself. 「やって見せる」 does
the step, 「戻る」 goes back, 「手順書で見る」 opens the help at the course and ×
puts it aside (the course goes on from there next time). The courses are in
ui/tutorial_courses.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from qgis.PyQt.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QRegion
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .fit import fit_text

#: The help pictures' red (the frames and numbers of 画面つきの手順書).
RED = "#e11d48"
GREEN = "#15803d"
BLUE = "#1d4ed8"
#: How often the screen is looked at (ms), and how long 「できました」 shows before the next step.
TICK_MS = 250
NEXT_MS = 1100
#: Names the tests and the help's pictures find the windows by.
GUIDE_NAME = "MikageTutorialGuide"
FRAME_NAME = "MikageTutorialFrame"


@dataclass
class Step:
    """One operation of a course (one picture of the help's 画面つきの手順書)."""
    #: The help's number of it (「1-2」) and its title.
    number: str
    title: str
    #: What to do, in the help's words (rich text; or a function giving them as the step starts).
    text: Any
    #: What the red frame goes round: a widget, widgets (the box round them all), a QRect on the screen, or None.
    target: Optional[Callable[[], Any]] = None
    #: Whether it is done, given what `begin` gave (None: a step to read, then 次へ).
    done: Optional[Callable[[Any], bool]] = None
    #: Called as the step starts: what `done` compares with (the rows before, …). One with `close()` is closed after.
    begin: Optional[Callable[[], Any]] = None
    #: 「やって見せる」: does the step (None: nothing to show).
    demo: Optional[Callable[[], None]] = None
    #: (button's text, what it does): shown while the frame's target is not on the screen (a dock closed…).
    reveal: Optional[tuple] = None
    #: こうなれば OK: what the screen shows once it is done.
    ok: str = ""
    #: Why the step does not apply now ("" when it does): it is passed by 次へ.
    skip: Optional[Callable[[], str]] = None
    #: Whether what the step needs is there yet (a list being read…): 「やって見せる」 waits for it.
    ready: Optional[Callable[[], bool]] = None


@dataclass
class Course:
    """A course: one flow of the help's 画面つきの手順書 (or 「はじめての MIKAGE」)."""
    key: str
    title: str
    #: The role (担当, core/roles code) it is for, "" for everyone; it is used while the course runs.
    role: str
    summary: str
    #: The help's section of it (its id), for 「手順書で見る」.
    help: str
    steps: list = field(default_factory=list)
    #: Before the first step (the map moved to the practice's area, a layer added…); what it did, for the window.
    setup: Optional[Callable[[], str]] = None


class Counter(QObject):
    """How many times a signal came since the step started (a search finished, a list read…); closed after the step."""

    def __init__(self, signal, when: Optional[Callable[[], bool]] = None):
        super().__init__()
        self.signal = signal
        self.when = when
        self.count = 0
        signal.connect(self._came)

    def _came(self, *_args) -> None:
        if self.when is None or self.when():
            self.count += 1

    def close(self) -> None:
        try:
            self.signal.disconnect(self._came)
        except (TypeError, RuntimeError):
            pass


def screen_rect(target: Any) -> Optional[QRect]:
    """Where `target` (a widget, widgets, a QRect on the screen) is on the screen; None when not shown."""
    if target is None:
        return None
    if isinstance(target, QRect):
        return QRect(target) if target.isValid() and not target.isEmpty() else None
    if isinstance(target, (list, tuple)):
        rect = None
        for t in target:
            r = screen_rect(t)
            if r is not None:
                rect = r if rect is None else rect.united(r)
        return rect
    if isinstance(target, QWidget):
        try:
            if not target.isVisible() or target.window().isMinimized():
                return None
            seen = target.visibleRegion().boundingRect()
            if seen.isEmpty():
                return None
            return QRect(target.mapToGlobal(seen.topLeft()), seen.size())
        except RuntimeError:  # (deleted meanwhile)
            return None
    return None


def bring_into_view(target: Any) -> None:
    """A widget shown but scrolled out of its scroll area (a dock's form): scrolled to."""
    for t in target if isinstance(target, (list, tuple)) else [target]:
        if not isinstance(t, QWidget):
            continue
        try:
            if not t.isVisible() or not t.visibleRegion().isEmpty():
                continue
            parent = t.parentWidget()
            while parent is not None and not isinstance(parent, QScrollArea):
                parent = parent.parentWidget()
            if parent is not None:
                parent.ensureWidgetVisible(t)
        except RuntimeError:
            continue
        return


class RedFrame(QWidget):
    """The red frame round what to press: a window of its own above everything, that clicks go through."""

    BORDER = 3
    MARGIN = 5

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setObjectName(FRAME_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.rect_shown: Optional[QRect] = None

    def show_rect(self, rect: Optional[QRect]) -> None:
        if rect is None:
            self.rect_shown = None
            self.hide()
            return
        outer = rect.adjusted(-self.MARGIN, -self.MARGIN, self.MARGIN, self.MARGIN)
        if outer != self.rect_shown:
            self.rect_shown = outer
            self.setGeometry(outer)
            inner = QRect(0, 0, outer.width(), outer.height()).adjusted(self.BORDER, self.BORDER, -self.BORDER, -self.BORDER)
            self.setMask(QRegion(0, 0, outer.width(), outer.height()).subtracted(QRegion(inner)))
        if not self.isVisible():
            self.show()
            self.raise_()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(RED))
        painter.end()


class GuideWindow(QWidget):
    """The window of the course: the step's number, title and words, and its buttons. Above QGIS, not blocking it."""

    closed = pyqtSignal()

    def __init__(self, main: Optional[QWidget] = None):
        # (no parent: a dialog QGIS opens over its window does not cover or block it)
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        fit_text(self)
        self.main = main
        self.setObjectName(GUIDE_NAME)
        self.setWindowTitle(self.tr("チュートリアル【練習】"))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 10, 14, 10)
        head = QHBoxLayout()
        self.course = QLabel()
        self.course.setStyleSheet("color: #555;")
        head.addWidget(self.course, 1)
        self.count = QLabel()
        self.count.setStyleSheet("color: #555;")
        head.addWidget(self.count)
        box.addLayout(head)
        self.title = QLabel()
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setWordWrap(True)
        box.addWidget(self.title)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setMinimumHeight(60)
        self.text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        box.addWidget(self.text, 1)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.state)
        self.reveal = QPushButton()
        self.reveal.hide()
        box.addWidget(self.reveal, 0, Qt.AlignmentFlag.AlignLeft)
        row = QHBoxLayout()
        self.back = QPushButton(self.tr("◀ 戻る"))
        self.demo = QPushButton(self.tr("やって見せる"))
        self.demo.setToolTip(self.tr("この手順を MIKAGE がやって見せます（練習データなので、そのまま記録などになります）"))
        self.manual = QPushButton(self.tr("手順書で見る"))
        self.manual.setToolTip(self.tr("ヘルプの「画面つきの手順書」のこのコースを開きます"))
        self.next = QPushButton(self.tr("次へ ▶"))
        for b in (self.back, self.demo, self.manual):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.next)
        box.addLayout(row)
        self.setMinimumWidth(460)
        self.resize(500, 300)
        self._placed = False

    def place(self, avoid: Optional[QRect] = None) -> None:
        """At the bottom right of QGIS's window first; moved to another corner when it would hide the frame's target."""
        main = self.main
        if main is None:
            return
        try:
            area = QRect(main.mapToGlobal(QPoint(0, 0)), main.size())
        except RuntimeError:
            return
        w, h = self.frameGeometry().width() or self.width(), self.frameGeometry().height() or self.height()
        corners = [QPoint(area.right() - w - 24, area.bottom() - h - 40), QPoint(area.left() + 24, area.bottom() - h - 40),
                   QPoint(area.right() - w - 24, area.top() + 90), QPoint(area.left() + 24, area.top() + 90)]
        here = self.frameGeometry()
        if self._placed and (avoid is None or not here.intersects(avoid)):
            return
        for corner in corners:
            if avoid is None or not QRect(corner, here.size()).intersects(avoid):
                self.move(corner)
                break
        self._placed = True

    def say(self, text: str, color: str = "#333") -> None:
        self.state.setText(f"<span style='color:{color}'>{text}</span>" if text else "")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        self.closed.emit()
        super().closeEvent(event)


class Tutorial(QObject):
    """
    A course running: the window says the step, the frame goes round its
    target, and the screen is looked at every TICK_MS to see whether it is
    done (then the next step). `keep(course key, step, finished)` is told of
    each step reached (where the course goes on from next time).
    """

    #: The course ended (its last step passed) or was put aside (×): its key and whether it was finished.
    ended = pyqtSignal(str, bool)

    def __init__(self, course: Course, main: Optional[QWidget], keep: Callable[[str, int, bool], None],
                 open_help: Callable[[str], None], more: Optional[Callable[[], None]] = None):
        super().__init__()
        self.course = course
        self.keep = keep
        self.open_help = open_help
        self.more = more
        self.guide = GuideWindow(main)
        self.frame = RedFrame()
        self.index = -1
        self.baseline: Any = None
        self.finished = False
        #: The step's state: "wait" (not done yet), "already" (done when it started), "done" (going to the next), "read".
        self.state = ""
        self._seen_undone = False
        self._next_timer = QTimer(self)
        self._next_timer.setSingleShot(True)
        self._next_timer.timeout.connect(self.forward)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.problem = ""
        self._waited = False
        g = self.guide
        g.back.clicked.connect(self.backward)
        g.next.clicked.connect(self._next_pressed)
        g.demo.clicked.connect(self.show_how)
        g.manual.clicked.connect(lambda: self.open_help(self.course.help))
        g.reveal.clicked.connect(self._reveal)
        g.closed.connect(self._closed)
        self._stopping = False

    # ---- running ---------------------------------------------------------------------------------------------

    def start(self, at: int = 0) -> None:
        note = ""
        if self.course.setup is not None:
            try:
                note = self.course.setup() or ""
            except Exception as error:  # noqa: BLE001 (a course's own step: said in the window, the course goes on)
                note = self.tr("準備できませんでした: {m}").format(m=error)
        self.guide.course.setText(self.course.title)
        self.guide.show()
        self.guide.place()
        self.go(max(0, min(at, len(self.course.steps) - 1)))
        if note:
            self.guide.say(note, BLUE)
        self.timer.start(TICK_MS)

    @property
    def step(self) -> Optional[Step]:
        return self.course.steps[self.index] if 0 <= self.index < len(self.course.steps) else None

    def _close_baseline(self) -> None:
        close = getattr(self.baseline, "close", None)
        if callable(close):
            try:
                close()
            except RuntimeError:
                pass
        self.baseline = None

    def go(self, index: int) -> None:
        """Shows step `index` (its baseline taken now)."""
        self._next_timer.stop()
        self._close_baseline()
        self.index = index
        self.finished = False
        step = self.step
        g = self.guide
        if step is None:
            return
        g.count.setText(self.tr("手順 {i} / {n}").format(i=index + 1, n=len(self.course.steps)))
        g.title.setText(f"{step.number}. {step.title}")
        g.back.setEnabled(index > 0)
        g.demo.setVisible(True)
        g.manual.setVisible(True)
        g.next.setText(self.tr("次へ ▶") if index < len(self.course.steps) - 1 else self.tr("終える ▶"))
        self.problem = ""
        skip = self._call(step.skip) if step.skip is not None else ""
        if skip:
            self.state = "skip"
            g.demo.setEnabled(False)
            g.say(skip + self.tr("　「次へ」で進みます。"), BLUE)
        else:
            self.baseline = self._call(step.begin) if step.begin is not None else None
            g.demo.setEnabled(step.demo is not None)
            if step.done is None:
                self.state = "read"
                g.say(self.tr("読んだら「次へ」を押します。"))
            else:
                self._seen_undone = not self._check()
                self.state = "wait" if self._seen_undone else "already"
                self._say_state()
        # (after `begin`: the words may say what it made, a file's place…)
        g.text.setText((self._call(step.text) or "") if callable(step.text) else step.text)
        self.keep(self.course.key, index, False)
        problem = self.problem
        self.tick()
        if problem:
            g.say(self.tr("うまく準備できませんでした: {m}").format(m=problem), RED)

    def _call(self, fn: Callable, *args) -> Any:
        try:
            return fn(*args)
        except Exception as error:  # noqa: BLE001 (a course's own step: said in the window, the course goes on)
            self.problem = str(error) or type(error).__name__
            return None

    def _check(self) -> bool:
        step = self.step
        if step is None or step.done is None:
            return False
        return bool(self._call(step.done, self.baseline))

    def _say_state(self) -> None:
        step, g = self.step, self.guide
        if self.state == "wait":
            g.say(self.tr("できると、次の手順に進みます。") + (self.tr("　こうなれば OK: {t}").format(t=step.ok) if step and step.ok else ""))
        elif self.state == "already":
            g.say(self.tr("この手順はもうできています。「次へ」で進みます（やり直してもかまいません）。"), BLUE)
        elif self.state == "done":
            g.say(self.tr("✔ できました") + (f"　{step.ok}" if step and step.ok else ""), GREEN)

    def tick(self) -> None:
        step = self.step
        self.problem = ""
        if step is None or self.finished:
            self.frame.show_rect(None)
            return
        target = self._call(step.target) if step.target is not None else None
        rect = screen_rect(target)
        if rect is None and target is not None:
            bring_into_view(target)
            rect = screen_rect(target)
        self.frame.show_rect(rect)
        self.guide.reveal.setVisible(rect is None and step.reveal is not None and self.state != "skip")
        if step.reveal is not None:
            self.guide.reveal.setText(step.reveal[0])
        if rect is not None:
            self.guide.place(avoid=self.frame.rect_shown)
        ready = self.ready()
        self.guide.demo.setEnabled(step.demo is not None and ready and self.state != "skip")
        if not ready and self.state != "done":
            self.guide.say(self.tr("読み込んでいます…"), BLUE)
            self._waited = True
        elif self._waited:
            self._waited = False
            self._say_state() if self.state != "read" else self.guide.say(self.tr("読んだら「次へ」を押します。"))
        if self.state in ("wait", "already"):
            done = self._check()
            if done and self._seen_undone:
                self.state = "done"
                self._say_state()
                self._next_timer.start(NEXT_MS)
            elif not done:
                if not self._seen_undone:
                    self._seen_undone = True
                if self.state == "already":
                    self.state = "wait"
                    self._say_state()
        if self.problem and self.state != "done":
            self.guide.say(self.tr("うまく進めませんでした: {m}").format(m=self.problem), RED)

    def ready(self) -> bool:
        """Whether what the step needs is there yet (「やって見せる」 waits for it)."""
        step = self.step
        return step is None or step.ready is None or bool(self._call(step.ready))

    def forward(self) -> None:
        """The next step (after the last one: the course finished)."""
        if self.index < len(self.course.steps) - 1:
            self.go(self.index + 1)
        else:
            self.finish()

    def _next_pressed(self) -> None:
        if self.finished:
            if self.more is not None:
                self.stop()
                self.more()
            else:
                self.stop()
            return
        self.forward()

    def backward(self) -> None:
        if self.finished:
            self.go(len(self.course.steps) - 1)
        elif self.index > 0:
            self.go(self.index - 1)

    def show_how(self) -> None:
        """「やって見せる」: the step done by MIKAGE (then it goes on as when done by hand)."""
        step = self.step
        if step is None or step.demo is None or not self.ready():
            return
        self.problem = ""
        self._seen_undone = True
        if self.state == "already":
            self.state = "wait"
        self._call(step.demo)
        if self.problem:
            self.guide.say(self.tr("やって見せられませんでした: {m}").format(m=self.problem), RED)
        elif step.done is None:
            self.guide.say(self.tr("やって見せました。「次へ」で進みます。"), BLUE)

    def _reveal(self) -> None:
        step = self.step
        if step is not None and step.reveal is not None:
            self._call(step.reveal[1])

    def finish(self) -> None:
        self._next_timer.stop()
        self._close_baseline()
        self.finished = True
        self.frame.show_rect(None)
        g = self.guide
        g.count.setText(self.tr("おわり"))
        g.title.setText(self.tr("「{c}」を終えました").format(c=self.course.title))
        g.text.setText(self.tr("おつかれさまでした。同じ操作を本番のデータでもします。もう一度するときや、ほかのコースは、"
                               "メニューの「チュートリアル…」か地図の上の「チュートリアル」から選びます。"))
        g.say(self.tr("✔ コースを終えました"), GREEN)
        g.demo.setVisible(False)
        g.reveal.hide()
        g.back.setEnabled(True)
        g.next.setText(self.tr("ほかのコース…") if self.more is not None else self.tr("閉じる"))
        self.keep(self.course.key, len(self.course.steps), True)

    def stop(self) -> None:
        """The course put aside (the window closed): where it was is kept."""
        if self._stopping:
            return
        self._stopping = True
        self.timer.stop()
        self._next_timer.stop()
        self._close_baseline()
        self.frame.show_rect(None)
        try:
            self.guide.close()
            self.guide.deleteLater()
            self.frame.close()
            self.frame.deleteLater()
        except RuntimeError:
            pass
        self.ended.emit(self.course.key, self.finished)

    def _closed(self) -> None:
        if not self._stopping:
            self.stop()


class CourseList(QDialog):
    """「チュートリアル…」: the courses (the person's roles' first), how far each went, and where to start."""

    def __init__(self, courses: Sequence[Course], progress: dict, roles: Sequence[str], role_names: dict, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("チュートリアル【練習】"))
        self.courses = list(courses)
        self.progress = progress
        #: What was chosen: (course key, the step to start at), None when closed.
        self.choice: Optional[tuple] = None
        box = QVBoxLayout(self)
        note = QLabel(self.tr("練習用のデータで、ヘルプの「画面つきの手順書」と同じ操作を 1 つずつ案内します。"
                              "案内の窓の言うとおりに操作すると、次の手順に進みます。わからないときは「やって見せる」を押します。"))
        note.setWordWrap(True)
        box.addWidget(note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("コース"), self.tr("担当"), self.tr("手順"), self.tr("進み具合")])
        self.tree.setRootIsDecorated(False)
        mine = [c for c in self.courses if not c.role or c.role in roles]
        others = [c for c in self.courses if c not in mine]
        for course in mine + others:
            kept = progress.get(course.key) or {}
            if kept.get("done"):
                state = self.tr("済み")
            elif kept.get("step"):
                state = self.tr("手順 {i} まで").format(i=int(kept["step"]) + 1)
            else:
                state = self.tr("まだ")
            role = role_names.get(course.role, "") if course.role else self.tr("だれでも")
            if course.role and course.role in roles:
                role += self.tr("（あなたの担当）")
            item = QTreeWidgetItem([course.title, role, str(len(course.steps)), state])
            item.setToolTip(0, course.summary)
            item.setData(0, Qt.ItemDataRole.UserRole, course.key)
            self.tree.addTopLevelItem(item)
        header = self.tree.header()
        for c in (0, 1, 2):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        rows = self.tree.topLevelItemCount()
        row = self.tree.sizeHintForRow(0) if rows else 20
        self.tree.setMinimumHeight(header.sizeHint().height() + row * rows + 8)
        self.tree.itemDoubleClicked.connect(lambda _item, _col: self._go(False))
        self.tree.currentItemChanged.connect(lambda *_a: self._chosen_changed())
        box.addWidget(self.tree, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: #555;")
        box.addWidget(self.summary)
        buttons = QDialogButtonBox()
        self.go_on = buttons.addButton(self.tr("始める"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.again = buttons.addButton(self.tr("最初から"), QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.tr("閉じる"), QDialogButtonBox.ButtonRole.RejectRole)
        self.go_on.clicked.connect(lambda: self._go(False))
        self.again.clicked.connect(lambda: self._go(True))
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.resize(820, self.sizeHint().height())

    def chosen(self) -> Optional[Course]:
        item = self.tree.currentItem()
        key = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return next((c for c in self.courses if c.key == key), None)

    def _chosen_changed(self) -> None:
        course = self.chosen()
        kept = (self.progress.get(course.key) or {}) if course is not None else {}
        going = bool(kept.get("step")) and not kept.get("done")
        self.go_on.setText(self.tr("続きから") if going else self.tr("始める"))
        self.again.setVisible(going or bool(kept.get("done")))
        self.summary.setText(course.summary if course is not None else "")

    def _go(self, again: bool) -> None:
        course = self.chosen()
        if course is None:
            return
        kept = self.progress.get(course.key) or {}
        at = 0 if again or kept.get("done") else int(kept.get("step") or 0)
        self.choice = (course.key, at)
        self.accept()


def application_modal() -> Optional[QWidget]:
    """The modal window open now (a question QGIS asks), None when none."""
    return QApplication.activeModalWidget()
