"""
文で操作 on the screen (core/text_command.py; tsudo 2026-11-21): the field on
its own toolbar 「文で操作」, and the card under it that says what the
sentence was read as before anything is done.

- Typed and Enter: the operation its words name (no model), else the one the
  model chooses among those of the roles in use (「操作: 名前」). The field
  shows only while the settings give 文で操作 (`textAssist.tasks.command`) a
  model; the model reads the list of operations once the field is clicked
  (the sentence is then read in about a second).
- The card: the sentence, the operation (another can be chosen), the values
  read from it by code (system, day or period, year, place, satellites,
  screen: each can be changed; one not read is left empty, and a value the
  operation needs empty keeps its button off), what will happen, and the
  button. Nothing is done before the button; nothing is saved, written or
  sent by it (the screens' own buttons do that, with their own questions).

What each operation does is the plugin's (features/command.py, `host.run`).
"""
from __future__ import annotations

import re
import time
from datetime import date
from typing import Any, Callable, Optional, Protocol, Sequence

from qgis.PyQt.QtCore import QDate, QEvent, QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox, QDateEdit, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QToolBar, QVBoxLayout, QWidget,
)

from ..core import audit
from ..core.data_chat import TASK as DATA_TASK, is_question
from ..core.text_command import (
    ALL, BY_KEY, Operation, Reading, by_words, command_messages, lat_lon_in, operation_of, read, reply_done, word_matches,
)
from ..qgis_io.text_assist_io import SHOW_MS, Asked
from ..qgis_io.tasks import error_text
from .docks import plain_tip
from .fit import fit_text
from .text_assist_ui import PLAIN_STYLE, WARN_STYLE, WRITING_STYLE

TASK = "command"
#: The day shown for 「not given」 in a date field.
UNSET = QDate(2000, 1, 1)


class CommandHost(Protocol):
    """What 文で操作 needs from the plugin (features/command.py)."""

    def operations(self) -> list[Operation]:
        """The operations of the roles in use (and the settings: 結果ファイル only with a system that has one…)."""

    def systems(self) -> list:
        """The settings' requestExports (.system, .label)."""

    def place_names(self) -> list[str]:
        """The names of places known now (named areas read, interests), the longest found in a sentence first."""

    def satellites(self) -> list:
        """The satellites known (SatelliteSpec), for their names in a sentence."""

    def screens(self) -> list[str]:
        """The menu's entries the roles in use have (画面を開く)."""

    def find_place(self, text: str, point: Optional[tuple], then: Callable[[Any, str], None]) -> None:
        """Where `text` (or `point`) is: then(core/text_request.AreaChoice or None, why none)."""

    def run(self, reading: Reading, place: Any) -> str:
        """Does the operation with these values (`place`: the AreaChoice found); what was done, for the status bar."""

    def today(self) -> date: ...


class _DayEdit(QDateEdit):
    """A day that may be not given (shown 「（なし）」)."""

    def __init__(self, unset_text: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setDisplayFormat("yyyy-MM-dd (ddd)")
        self.setMinimumDate(UNSET)
        self.setSpecialValueText(unset_text)
        self.set_value(None)

    def value(self) -> Optional[date]:
        d = self.date()
        return None if d == UNSET else date(d.year(), d.month(), d.day())

    def set_value(self, d: Optional[date]) -> None:
        self.setDate(QDate(d.year, d.month, d.day) if d else UNSET)


def _row(label: str, *widgets: QWidget) -> tuple[QWidget, QLabel]:
    row = QWidget()
    box = QHBoxLayout(row)
    box.setContentsMargins(0, 0, 0, 0)
    name = QLabel(label)
    name.setMinimumWidth(72)
    box.addWidget(name)
    for w in widgets:
        box.addWidget(w)
    box.addStretch(1)
    return row, name


class CommandCard(QDialog):
    """What a sentence was read as, its values to change, and the button that does it."""

    def __init__(self, host: CommandHost, parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.host = host
        self.setObjectName("SatObsCommandCard")
        self.setWindowTitle(self.tr("文で操作"))
        self.setMinimumWidth(460)
        self.reading: Optional[Reading] = None
        #: The place found for the place field (core/text_request.AreaChoice), and for which text.
        self.place_found: Any = None
        self._place_for: Optional[str] = None
        #: What run() did last (for tests and the console).
        self.message = ""
        #: Runs the operation (the box sets it: it records and says what was done).
        self.on_run: Callable[[], None] = lambda: None
        layout = QVBoxLayout(self)
        self.said = QLabel()
        self.said.setWordWrap(True)
        self.said.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.said.setStyleSheet("QLabel { color: #57534e; }")
        layout.addWidget(self.said)
        self.state = QLabel()
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        self.operation = QComboBox()
        self.operation.setToolTip(self.tr("違っていれば別の操作を選ぶ（値は文から読み直す）"))
        self.operation.currentIndexChanged.connect(lambda _i: self._operation_chosen())
        self.how = QLabel()
        self.how.setStyleSheet("QLabel { color: #2f6b2f; font-size: 11px; }")
        self.operation_row, _ = _row(self.tr("操作"), self.operation, self.how)
        layout.addWidget(self.operation_row)

        self.system = QComboBox()
        self.system_row, _ = _row(self.tr("システム"), self.system)
        self.first = _DayEdit(self.tr("（なし）"))
        self.last = _DayEdit(self.tr("（なし）"))
        self.until = QLabel(self.tr("〜"))
        self.day_row, self.day_label = _row(self.tr("対象日"), self.first, self.until, self.last)
        self.year = QComboBox()
        self.year_row, _ = _row(self.tr("年"), self.year)
        self.place = QLineEdit()
        self.place.setClearButtonEnabled(True)
        self.place.setMinimumWidth(200)
        self.place.setPlaceholderText(self.tr("地名・緯度経度"))
        self.place.editingFinished.connect(self._place_edited)
        self.place_note = QLabel()
        self.place_note.setStyleSheet("QLabel { font-size: 11px; color: #57534e; }")
        self.place_row, _ = _row(self.tr("場所"), self.place, self.place_note)
        self.satellites = QLineEdit()
        self.satellites.setMinimumWidth(240)
        self.satellites.setPlaceholderText(self.tr("衛星の名前（、で区切る）"))
        self.satellites_row, _ = _row(self.tr("衛星"), self.satellites)
        self.screen_choice = QComboBox()
        self.screen_row, _ = _row(self.tr("画面"), self.screen_choice)
        self.rows = {"system": self.system_row, "period": self.day_row, "year": self.year_row, "place": self.place_row,
                     "satellites": self.satellites_row, "screen": self.screen_row}
        for row in self.rows.values():
            layout.addWidget(row)
        for w in (self.system, self.year, self.screen_choice):
            w.currentIndexChanged.connect(lambda _i: self._update_button())
        for w in (self.first, self.last):
            w.dateChanged.connect(lambda _d: self._update_button())
        self.satellites.textChanged.connect(lambda _t: self._update_button())

        self.will = QLabel()
        self.will.setWordWrap(True)
        self.will.setStyleSheet("QLabel { background: #f4f7fb; border-left: 3px solid #6a8fc8; padding: 6px; }")
        layout.addWidget(self.will)
        self.caution = QLabel()
        self.caution.setWordWrap(True)
        self.caution.setStyleSheet(WARN_STYLE)
        layout.addWidget(self.caution)
        buttons = QHBoxLayout()
        self.timing = QLabel()
        self.timing.setStyleSheet("QLabel { color: #777; font-size: 11px; }")
        buttons.addWidget(self.timing, 1)
        self.cancel_button = QPushButton(self.tr("取りやめ"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.run_button = QPushButton(self.tr("実行"))
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(lambda _on=False: self.on_run())
        buttons.addWidget(self.run_button)
        layout.addLayout(buttons)
        self._ops: list[Operation] = []
        self._text = ""

    # ---- what is shown --------------------------------------------------------------------------------

    def asking(self, text: str, stage: str) -> None:
        """The sentence while the model chooses its operation."""
        self.reading = None
        self.said.setText(self.tr("「{t}」").format(t=text))
        self.state.setText(stage)
        self.state.setStyleSheet(WRITING_STYLE)
        self.state.show()
        for w in (self.operation_row, self.will, self.caution, *self.rows.values()):
            w.hide()
        self.timing.setText("")
        self.run_button.hide()
        self.cancel_button.setText(self.tr("取りやめ"))

    def show_reading(self, reading: Optional[Reading], text: str, ops: Sequence[Operation], seconds: float = 0.0,
                     caution: str = "", problem: str = "") -> None:
        """What `text` was read as (None: no operation found; one can still be chosen)."""
        self._ops = list(ops)
        self.said.setText(self.tr("「{t}」").format(t=text))
        self.operation.blockSignals(True)
        self.operation.clear()
        self.operation.addItem(self.tr("（操作を選ぶ）"), None)
        for o in self._ops:
            self.operation.addItem(o.title or o.name, o.key)
        self.operation.setCurrentIndex(max(0, self.operation.findData(reading.operation.key)) if reading else 0)
        self.operation.blockSignals(False)
        self.operation_row.show()
        self._text = text
        if reading is None:
            self.state.setText(problem or self.tr("合う操作が見つからず（下で選べば、文から値を読む）"))
            self.state.setStyleSheet(PLAIN_STYLE)
            self.state.show()
            self.how.setText("")
        else:
            self.state.hide()
            self.how.setText(self.tr("言葉から") if reading.by == "words" else
                             self.tr("生成 AI が選択（{s:.1f} 秒）").format(s=seconds) if seconds >= 0.1 else self.tr("生成 AI が選択"))
        self.caution.setText(caution)
        self.caution.setVisible(bool(caution))
        self.timing.setText("")
        self._show_values(reading)

    def _operation_chosen(self) -> None:
        key = self.operation.currentData()
        if key is None:
            self._show_values(None)
            return
        op = BY_KEY[key]
        h = self.host
        self.how.setText(self.tr("選び直し"))
        self.state.hide()
        self._show_values(read(op, self._text, h.today(), h.systems(), h.place_names(), h.satellites(), h.screens(), by="chosen"))

    def _show_values(self, reading: Optional[Reading]) -> None:
        self.reading = reading
        for row in self.rows.values():
            row.hide()
        self.will.setVisible(reading is not None)
        self.run_button.setVisible(reading is not None)
        if reading is None:
            self.place_found, self._place_for = None, None
            return
        op, h = reading.operation, self.host
        slots = set(op.slots)
        self.setWindowTitle(self.tr("文で操作: {t}").format(t=op.title or op.name))
        if "system" in slots:
            self.system.blockSignals(True)
            self.system.clear()
            self.system.addItem(self.tr("すべてのシステム"), ALL)
            for s in h.systems():
                self.system.addItem(s.label if s.label == s.system else f"{s.label}（{s.system}）", s.system)
            # Several named (「AとB」): every system (the files are written per system from the screen).
            chosen = reading.systems[0] if len(reading.systems) == 1 else ALL
            self.system.setCurrentIndex(max(0, self.system.findData(chosen)))
            self.system.blockSignals(False)
            self.system_row.show()
        if slots & {"day", "period"}:
            one = "period" not in slots
            self.day_label.setText(self.tr("対象日") if one else self.tr("期間"))
            for w in (self.until, self.last):
                w.setVisible(not one)
            first, last = reading.period or (None, None)
            for w, d in ((self.first, first), (self.last, last)):
                w.blockSignals(True)
                w.set_value(d)
                w.blockSignals(False)
            self.day_row.show()
        if "year" in slots:
            self.year.blockSignals(True)
            self.year.clear()
            this = h.today().year
            for k in range(3):
                self.year.addItem(self.tr("今年（{y}）").format(y=this) if k == 0 else self.tr("{y} 年").format(y=this - k), k)
            self.year.setCurrentIndex(min(2, reading.year_back))
            self.year.blockSignals(False)
            self.year_row.show()
        if "place" in slots:
            written = f"{reading.point[1]:.5f}, {reading.point[0]:.5f}" if reading.point else (reading.place or "")
            self.place.setText(written)
            self.place_row.show()
            self._find_place()
        if "satellites" in slots:
            self.satellites.setText("、".join(reading.satellites))
            self.satellites_row.show()
        if "screen" in slots:
            self.screen_choice.blockSignals(True)
            self.screen_choice.clear()
            self.screen_choice.addItem(self.tr("（画面を選ぶ）"), None)
            for s in h.screens():
                self.screen_choice.addItem(s, s)
            self.screen_choice.setCurrentIndex(max(0, self.screen_choice.findData(reading.screen)) if reading.screen else 0)
            self.screen_choice.blockSignals(False)
            self.screen_row.show()
        self.will.setText(self._will(op))
        self.run_button.setText(op.button)
        self._update_button()

    def _will(self, op: Operation) -> str:
        note = op.note
        if op.key == "catalog" and not self.place.text().strip():
            note += self.tr("（場所がなければ地図の表示範囲）")
        if op.key == "passes" and self.first.value() is None:
            note += self.tr("（日がなければ今）")
        return note

    # ---- the place ------------------------------------------------------------------------------------

    def _place_edited(self) -> None:
        if self.reading is not None and self.place.text().strip() != (self._place_for or ""):
            self._find_place()

    def _find_place(self) -> None:
        text = self.place.text().strip()
        self.place_found, self._place_for = None, text
        if not text:
            self.place_note.setText(self.tr("（なし）") if self.reading and self.reading.operation.key == "catalog" else "")
            self._update_button()
            return
        point = lat_lon_in(text)
        self.place_note.setText(self.tr("検索中…"))
        self._update_button()

        def found(choice, problem: str) -> None:
            if self._place_for != text:
                return
            self.place_found = choice
            if choice is not None:
                self.place_note.setText(choice.label)
                self.place_note.setToolTip(plain_tip(choice.label))
            else:
                self.place_note.setText(problem or self.tr("見つからず（地名・緯度経度を直す）"))
            self._update_button()

        self.host.find_place(text, point, found)

    # ---- the values chosen -------------------------------------------------------------------------------

    def chosen(self) -> Optional[Reading]:
        """The reading with the values as the card has them now (None: no operation chosen)."""
        r = self.reading
        if r is None:
            return None
        slots = set(r.operation.slots)
        out = Reading(r.operation, r.text, by=r.by)
        if "system" in slots:
            out.systems = [self.system.currentData()] if self.system.currentData() not in (None, ALL) else [ALL]
        if slots & {"day", "period"}:
            first, last = self.first.value(), self.last.value()
            if "period" not in slots:
                last = first
            if first and last:
                out.period = (first, last) if first <= last else (last, first)
            elif first or last:
                out.period = (first or last, first or last) if "period" not in slots else (first, last)
        if "year" in slots:
            out.year_back = int(self.year.currentData() or 0)
        if "place" in slots:
            out.place = self.place.text().strip() or None
        if "satellites" in slots:
            out.satellites = [s.strip() for s in re.split(r"[、,，\s]+", self.satellites.text()) if s.strip()]
        if "screen" in slots:
            out.screen = self.screen_choice.currentData()
        return out

    def ready(self) -> bool:
        """Whether the button can be pressed: an operation, what it needs given, the place found (when it needs one)."""
        r = self.chosen()
        if r is None:
            return False
        op = r.operation
        if op.key == "planFiles" and r.period is None:
            return False
        if op.key == "openScreen" and not r.screen:
            return False
        needs_place = op.key in ("placeSchedule", "imagingPlan", "watch", "jump")
        if needs_place and not r.place:
            return False
        if r.place and op.key != "jump" and self.place_found is None:
            return False
        if op.key == "passes" and not r.satellites:
            return False
        return True

    def _update_button(self) -> None:
        if self.reading is not None:
            self.will.setText(self._will(self.reading.operation))
        self.run_button.setEnabled(self.ready())


class CommandBox(QObject):
    """The field 「文で操作」 on its own toolbar, and its card (one per plugin)."""

    def __init__(self, iface, context, host: CommandHost, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.host = host
        self.assistant = context.text_assist
        self.asked: Optional[Asked] = None
        #: What was said last (the status bar's message; for tests and the console).
        self.message = ""
        self.toolbar: QToolBar = iface.addToolBar(self.tr("文で操作"))
        self.toolbar.setObjectName("MikageCommandToolbar")
        self.label = QLabel(self.tr("文で操作"))
        self.label.setContentsMargins(4, 0, 4, 0)
        self.toolbar.addWidget(self.label)
        self.field = QLineEdit()
        self.field.setObjectName("SatObsCommandField")
        self.field.setClearButtonEnabled(True)
        self.field.setMinimumWidth(300)
        self.field.setMaximumWidth(520)
        self.field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.field.setPlaceholderText(self.tr("例: Aシステムの明日のオーダーファイルを出力"))
        self.field.setToolTip(plain_tip(self.tr(
            "したいことを文で書いて Enter。操作と値（システム・日付・場所など）を読み取ったカードが出て、"
            "ボタンを押すまで何もしない。書き出し・取り込み・保存・送信は、開いた画面のボタンで")))
        self.field.returnPressed.connect(self.submit)
        self.field.installEventFilter(self)
        self.toolbar.addWidget(self.field)
        self.card = CommandCard(host, iface.mainWindow())
        self.card.on_run = self.run
        self.card.rejected.connect(self.cancel)
        self._timer = QTimer(self)
        self._timer.setInterval(SHOW_MS)
        self._timer.timeout.connect(self._show)
        self.assistant.changed.connect(self.apply)
        self.apply()

    # ---- shown or not -------------------------------------------------------------------------------

    @property
    def offered(self) -> bool:
        """
        Whether 文で操作 shows: the settings give it a model and the roles in use have an operation, or データに質問
        has one (a question typed here goes there).
        """
        return (self.assistant.has(TASK) and bool(self.host.operations())) or self.asks_data

    @property
    def asks_data(self) -> bool:
        """Whether a question on the data typed here goes to 「データに質問」 (its model given, the host taking it)."""
        return self.assistant.has(DATA_TASK) and hasattr(self.host, "ask_data")

    def apply(self) -> None:
        """Shown while offered (the settings, the roles in use); the card closed when not."""
        on = self.offered
        if self.asks_data:
            self.field.setPlaceholderText(self.tr("例: Aシステムの明日のオーダーファイルを出力 / 先月の舞鶴の記録は何件？"))
        else:
            self.field.setPlaceholderText(self.tr("例: Aシステムの明日のオーダーファイルを出力"))
        self.toolbar.setVisible(on)
        self.toolbar.toggleViewAction().setVisible(on)
        if not on:
            self.cancel()
            self.card.hide()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt's name)
        if watched is self.field and event.type() == QEvent.Type.FocusIn:
            self.prime()
        return False

    def _task(self):
        s = self.assistant.settings
        return s.task(TASK) if s is not None else None

    def _more_words(self) -> dict:
        t = self._task()
        return dict(t.words) if t is not None else {}

    def prime(self) -> None:
        """The model loaded and the list read while the sentence is typed (the reply then in about a second)."""
        t = self._task()
        ops = self.host.operations()
        if t is None or not ops or self.asked is not None:
            return
        # The conversation up to 「依頼: 」 (what every sentence's begins with).
        self.assistant.prime(TASK, command_messages(t, ops, ""))

    # ---- reading the sentence ------------------------------------------------------------------------

    def submit(self, text: Optional[str] = None) -> None:
        """Reads the sentence: its words, else the model; then the card."""
        text = (text if text is not None else self.field.text()).strip()
        if not text or not self.offered:
            return
        self.cancel()
        h = self.host
        ops = h.operations()
        today = h.today()
        op = by_words(text, ops, self._more_words(), h.screens())
        if op is None and self.asks_data and (is_question(text) or not self.assistant.has(TASK) or not ops):
            # A question on the data (「先月の舞鶴の記録は何件？」): answered by 「データに質問」.
            if h.ask_data(text):
                self.message = self.tr("「データに質問」で答える")
                self.field.clear()
                return
        self._place_card()
        if op is not None:
            reading = read(op, text, today, h.systems(), h.place_names(), h.satellites(), h.screens(), by="words")
            self.card.show_reading(reading, text, ops)
            self._show_card()
            return
        t = self._task()
        if t is None:
            self.card.show_reading(None, text, ops)
            self._show_card()
            return
        found = word_matches(text, ops, self._more_words())
        started = time.monotonic()

        def done(raw: str, seconds: float) -> None:
            self._stop()
            chosen = operation_of(raw, ops)
            took = time.monotonic() - started
            caution = self.assistant.caution(TASK)
            if chosen is None:
                why = self.tr("合う操作が見つからず（生成 AI の答え: {a}）。下で選べば、文から値を読む").format(
                    a=raw.strip().splitlines()[0][:40] if raw.strip() else self.tr("なし"))
                if found:
                    why = self.tr("候補: {c}。下で選べば、文から値を読む").format(c="・".join(o.name for o in found))
                self.card.show_reading(None, text, ops, took, caution, why)
                return
            reading = read(chosen, text, today, h.systems(), h.place_names(), h.satellites(), h.screens(), by="model")
            self.card.show_reading(reading, text, ops, took, caution)

        def failed(message: str) -> None:
            self._stop()
            if message == "取消":
                return
            self.card.show_reading(None, text, ops, problem=self.tr("生成 AI で読めない: {m}（下で選べば、文から値を読む）").format(m=message))

        messages = command_messages(t, ops, text)
        self.asked = self.assistant.ask(TASK, self.tr("文で操作"), messages, done, failed, one_line=True,
                                        until=reply_done(ops), keep=True)
        if self.asked is None:
            return
        self.card.asking(text, self.asked.stage)
        self._show_card()
        self._timer.start()

    def _show(self) -> None:
        if self.asked is not None:
            self.card.state.setText(self.asked.stage)

    def _stop(self) -> None:
        self._timer.stop()
        self.asked = None

    def cancel(self) -> None:
        """The model's reading stopped (the card closed meanwhile)."""
        if self.asked is not None:
            self.asked.cancel()
            self._stop()

    def _place_card(self) -> None:
        """The card under the field."""
        if not self.field.isVisible():
            return
        at = self.field.mapToGlobal(self.field.rect().bottomLeft())
        self.card.move(at.x(), at.y() + 4)

    def _show_card(self) -> None:
        self.card.show()
        self.card.raise_()
        self.card.activateWindow()

    # ---- doing it ----------------------------------------------------------------------------------------

    def run(self) -> None:
        """The card's button: the operation done with the card's values, recorded in the audit log, said in the status bar."""
        if not self.card.ready():
            return
        reading = self.card.chosen()
        op = reading.operation
        detail = {"text": reading.text[:300], "operation": op.name, "by": reading.by}
        if reading.systems:
            detail["systems"] = reading.systems
        if reading.period:
            detail["period"] = [d.isoformat() for d in reading.period]
        if reading.place:
            detail["place"] = reading.place
        if reading.satellites:
            detail["satellites"] = reading.satellites
        if reading.screen:
            detail["screen"] = reading.screen
        try:
            said = self.host.run(reading, self.card.place_found)
        except Exception as error:  # noqa: BLE001 (a screen that could not be opened: said, recorded, the card stays)
            audit.record("command", op.name, op.title, result="error", detail=detail, error=str(error))
            self.card.state.setText(self.tr("実行に失敗: {m}").format(m=error_text(error)))
            self.card.state.setStyleSheet(WARN_STYLE)
            self.card.state.show()
            return
        audit.record("command", op.name, op.title, detail=detail)
        self.card.hide()
        self.message = said or self.tr("{t} を表示").format(t=op.title or op.name)
        self.card.message = self.message
        self.iface.statusBarIface().showMessage(self.tr("文で操作: {m}").format(m=self.message), 8000)
        self.field.selectAll()

    def close(self) -> None:
        self.cancel()
        self.card.hide()
        self.card.deleteLater()
        self.iface.mainWindow().removeToolBar(self.toolbar)
        self.toolbar.deleteLater()


