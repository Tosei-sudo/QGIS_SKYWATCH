"""
An operation of 「ミカゲと話す」 (ui/chat_dock.py; 文で操作's card until
2026-11-28, tsudo: 「文で操作の機能は全廃しよう。名前は任せるけど、チャットで質問も
操作もできるように」): the card in the chat that says what a sentence was read
as before anything is done.

- The operation its words name (no model), else the one the model chose
  among those of the roles in use (「操作: 名前」), else none (one can be
  chosen on the card).
- The card: the operation (another can be chosen), the values read from the
  sentence by code (system, day or period, year, place, satellites,
  画像検索's 衛星・種別 with the catalog's sensors they name, screen: each can
  be changed; one not read is left empty, and a value the operation needs
  empty keeps its button off), what will happen, and the button. Nothing is
  done before the button; nothing is saved, written or sent by it (the
  screens' own buttons do that, with their own questions). Done or left,
  the card stays in the chat as it was.

What each operation does is the plugin's (features/chat.py, `host.run`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional, Protocol, Sequence

from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtWidgets import QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..core import audit
from ..core.catalog.api import kind_word, sensor_words_of
from ..core.plan.api import kinds
from ..core.text_command import ALL, BY_KEY, Operation, Reading, lat_lon_in, read
from ..qgis_io.tasks import error_text
from .docks import plain_tip
from .text_assist_ui import PLAIN_STYLE, WARN_STYLE

#: The day shown for 「not given」 in a date field.
UNSET = QDate(2000, 1, 1)


class OperationHost(Protocol):
    """What an operation's card needs from the plugin (features/chat.py)."""

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

    def sensors(self) -> list[str]:
        """The sensors of the catalog chosen in 画像カタログ (画像検索's 衛星・種別; none before they are read)."""

    def pick_sensors(self, words: list[str]) -> Any:
        """The catalog's sensors the words name (core/catalog/sensors.SensorPick); None when it has no sensor attribute."""

    def find_place(self, text: str, point: Optional[tuple], then: Callable[[Any, str], None]) -> None:
        """Where `text` (or `point`) is: then(core/text_request.AreaChoice or None, why none)."""

    def run(self, reading: Reading, place: Any) -> str:
        """Does the operation with these values (`place`: the AreaChoice found); what was done, for the status bar."""

    def today(self) -> date: ...


@dataclass
class FoundIn:
    """What 画像検索 found in one catalog: its records newest first, or why it was not searched."""
    catalog: Any
    #: Its name as 画像カタログ lists it.
    label: str
    records: list
    #: How many match (None when the catalog does not say, or it was not searched).
    total: Optional[int]
    #: Why it was not searched or failed ("" when it was).
    problem: str = ""


@dataclass
class ImagesFound:
    """
    What 画像検索 found for the chat (features/chat.py search_images): each catalog searched (`parts`; tsudo
    2026-11-28 「複数画像カタログがある場合はどうなる？」), and their records together newest first (`records`, each
    one's catalog in `catalogs`).
    """
    parts: list
    #: Where, when and which sensors (「呉・2026-11-21〜2026-11-27・SAR（3 センサー）」).
    said: str = ""
    records: list = field(default_factory=list)
    catalogs: list = field(default_factory=list)

    @property
    def searched(self) -> list:
        """The parts searched (not those that could not be)."""
        return [p for p in self.parts if not p.problem]

    @property
    def total(self) -> Optional[int]:
        """How many match in all (None when a catalog searched does not say)."""
        totals = [p.total for p in self.searched]
        return None if any(t is None for t in totals) else sum(totals)


def images_found(parts: Sequence[FoundIn], said: str, newest: Callable[[Any], float]) -> ImagesFound:
    """The parts' records together, newest first by `newest(record)` (a catalog's own order kept between equals)."""
    pairs = [(p.catalog, r) for p in parts for r in p.records]
    pairs.sort(key=lambda cr: -newest(cr[1]))
    return ImagesFound(list(parts), said, [r for _c, r in pairs], [c for c, _r in pairs])


class ChatHost(OperationHost, Protocol):
    """What 「ミカゲと話す」 needs from the plugin besides the operations (features/chat.py)."""

    def known_place(self, name: str) -> bool:
        """Whether `name` is a place known to the plugin (a named area, an interest): 画像検索 without the model."""

    def search_images(self, reading: Reading, place: Any, done: Callable[[Optional[ImagesFound], str], None]) -> None:
        """画像検索 behind the screen (画像カタログ's own search): done(found, "") or done(None, why not)."""

    def catalogs(self) -> list:
        """The catalogs 画像カタログ lists (none without it)."""

    def open_images(self, found: ImagesFound, indexes: Sequence[int]) -> str:
        """Those of the records found opened (as from the catalog's list); what was done."""

    def show_images(self, found: ImagesFound, part: FoundIn) -> str:
        """A catalog's records found listed in 画像カタログ, the dock in front."""

    def open_image_ids(self, ids: Sequence[str]) -> str:
        """Images opened by their ids (画像 ID で開く, one by one); what was done."""

    def list_image_ids(self, ids: Sequence[str]) -> str:
        """Images of these ids listed in 画像カタログ, the dock in front."""

    def open_records(self, ids: Sequence[str]) -> str:
        """The first of these 観測記録 shown in its form; what was done."""


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


def catalog_sensors(host: OperationHost) -> list[str]:
    """The sensors of the catalog chosen in 画像カタログ (none when the host can not say)."""
    sensors = getattr(host, "sensors", None)
    return list(sensors() or []) if sensors is not None else []


def _row(label: str, *widgets: QWidget) -> tuple[QWidget, QLabel]:
    row = QWidget()
    box = QHBoxLayout(row)
    box.setContentsMargins(0, 0, 0, 0)
    name = QLabel(label)
    name.setMinimumWidth(72)
    name.setProperty("rowName", True)
    box.addWidget(name)
    for w in widgets:
        box.addWidget(w)
    box.addStretch(1)
    return row, name


class OperationCard(QFrame):
    """What a sentence was read as, its values to change, and the button that does it (one card of the chat)."""

    def __init__(self, host: OperationHost, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.host = host
        self.setObjectName("SatObsChatOperation")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.reading: Optional[Reading] = None
        #: The place found for the place field (core/text_request.AreaChoice), and for which text.
        self.place_found: Any = None
        self._place_for: Optional[str] = None
        #: What run() did (for tests and the console); whether it was done or left (the card then stays as it was).
        self.message = ""
        self.finished = False
        #: Told what was done (the dock: the status bar's message).
        self.on_done: Callable[[str], None] = lambda _m: None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        self.title = QLabel(self.tr("操作"))
        self.title.setStyleSheet("QLabel { font-weight: 600; }")
        layout.addWidget(self.title)
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
        # 画像検索: the kinds (光学・SAR・IR: satelliteKinds), satellites, series or sensors, and the catalog's sensors they name.
        names = "・".join(kinds().names)
        self.sensors = QLineEdit()
        self.sensors.setClearButtonEnabled(True)
        self.sensors.setMinimumWidth(200)
        self.sensors.setPlaceholderText(self.tr("{k}・衛星の名前（、で区切る）").format(k=names))
        self.sensors.setToolTip(plain_tip(self.tr(
            "種別（{k}）、衛星・シリーズの名前（別名も）、カタログのセンサー名。画像カタログで選んでいるカタログの、"
            "当たるセンサーで検索（種別は衛星の一覧の種別から）。空はすべてのセンサー").format(k=names)))
        self.sensors_note = QLabel()
        self.sensors_note.setWordWrap(True)
        self.sensors_note.setStyleSheet("QLabel { font-size: 11px; color: #57534e; }")
        # The sensors named on a line of their own, under the words.
        self.sensors_row = QWidget()
        lines = QVBoxLayout(self.sensors_row)
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(2)
        words_row, _ = _row(self.tr("衛星・種別"), self.sensors)
        note_row, _ = _row("", self.sensors_note)
        # The note takes the row's width (not the stretch after it).
        note_row.layout().takeAt(note_row.layout().count() - 1)
        note_row.layout().setStretchFactor(self.sensors_note, 1)
        lines.addWidget(words_row)
        lines.addWidget(note_row)
        self.screen_choice = QComboBox()
        self.screen_row, _ = _row(self.tr("画面"), self.screen_choice)
        self.rows = {"system": self.system_row, "period": self.day_row, "year": self.year_row, "place": self.place_row,
                     "satellites": self.satellites_row, "sensors": self.sensors_row, "screen": self.screen_row}
        for row in self.rows.values():
            layout.addWidget(row)
        # The values' names as wide as the widest (「衛星・種別」).
        names = [w for w in self.findChildren(QLabel) if w.property("rowName")]
        for name in names:
            name.setMinimumWidth(max(72, *(n.sizeHint().width() for n in names)))
        for w in (self.system, self.year, self.screen_choice):
            w.currentIndexChanged.connect(lambda _i: self._update_button())
        for w in (self.first, self.last):
            w.dateChanged.connect(lambda _d: self._update_button())
        self.satellites.textChanged.connect(lambda _t: self._update_button())
        self.sensors.textChanged.connect(lambda _t: self._show_sensors())

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
        self.cancel_button.setToolTip(plain_tip(self.tr("この操作はしない（カードは残る）")))
        self.cancel_button.clicked.connect(lambda _on=False: self.leave())
        buttons.addWidget(self.cancel_button)
        self.run_button = QPushButton(self.tr("実行"))
        self.run_button.clicked.connect(lambda _on=False: self.run())
        buttons.addWidget(self.run_button)
        layout.addLayout(buttons)
        self._ops: list[Operation] = []
        self._text = ""

    # ---- what is shown --------------------------------------------------------------------------------

    def show_reading(self, reading: Optional[Reading], text: str, ops: Sequence[Operation], seconds: float = 0.0,
                     caution: str = "", problem: str = "") -> None:
        """What `text` was read as (None: no operation found; one can still be chosen)."""
        self._ops = list(ops)
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
        self._show_values(read(op, self._text, h.today(), h.systems(), h.place_names(), h.satellites(), h.screens(), by="chosen",
                               sensors=catalog_sensors(h)))

    def _show_values(self, reading: Optional[Reading]) -> None:
        self.reading = reading
        for row in self.rows.values():
            row.hide()
        self.will.setVisible(reading is not None)
        self.run_button.setVisible(reading is not None)
        if reading is None:
            self.title.setText(self.tr("操作"))
            self.place_found, self._place_for = None, None
            return
        op, h = reading.operation, self.host
        slots = set(op.slots)
        self.title.setText(self.tr("操作: {t}").format(t=op.title or op.name))
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
        if "sensors" in slots:
            self.sensors.blockSignals(True)
            self.sensors.setText("、".join(reading.sensors))
            self.sensors.blockSignals(False)
            self.sensors_row.show()
            self._show_sensors(update=False)
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

    # ---- 画像検索's sensors ------------------------------------------------------------------------------

    def sensor_pick(self) -> Any:
        """What 衛星・種別 names in the catalog (core/catalog/sensors.SensorPick); None when it can not say."""
        words = sensor_words_of(self.sensors.text())
        pick = getattr(self.host, "pick_sensors", None)
        return pick(words) if words and pick is not None else None

    def _show_sensors(self, update: bool = True) -> None:
        """The catalog's sensors 衛星・種別 names, beside it (the button off when it names none)."""
        words = sensor_words_of(self.sensors.text())
        pick = self.sensor_pick()
        warn = False
        tip = ""
        if not words:
            text = self.tr("（なし: すべてのセンサー）")
        elif pick is None:
            text = self.tr("画像カタログにセンサーの項目がなく、衛星・種別では絞らない") if hasattr(self.host, "pick_sensors") else ""
            warn = bool(text)
        elif not pick.sensors:
            warn = True
            if not pick.listed and any(kind_word(w) for w in words):
                text = self.tr("センサーの一覧が読めず、{k}で探せない（衛星の名前なら探せる）").format(
                    k="・".join(dict.fromkeys(k for k in (kind_word(w) for w in words) if k)))
            else:
                text = self.tr("当たるセンサーが画像カタログにない")
        else:
            n = len(pick.sensors)
            shown = "・".join(pick.sensors[:4]) + (self.tr(" ほか {m}").format(m=n - 4) if n > 4 else "")
            text = self.tr("→ {s}（{n} センサー）").format(s=shown, n=n) if pick.listed else self.tr("→ {s}（名前のまま）").format(s=shown)
            tip = self.tr("このセンサーで検索: {s}").format(s="、".join(pick.sensors))
            if pick.unknown:
                text += self.tr("。種別のわからない {u} は含めない").format(u=len(pick.unknown))
                tip += "\n" + self.tr("種別のわからないセンサー（衛星の一覧にない・種別なし）: {s}").format(s="、".join(pick.unknown))
            if pick.missing:
                text += self.tr("。「{m}」は当たるセンサーなし").format(m="、".join(pick.missing))
        self.sensors_note.setText(text)
        self.sensors_note.setToolTip(plain_tip(tip) if tip else "")
        self.sensors_note.setStyleSheet("QLabel { font-size: 11px; color: %s; }" % ("#b45309" if warn else "#57534e"))
        if update:
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
        if "sensors" in slots:
            out.sensors = sensor_words_of(self.sensors.text())
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
        if r.sensors:
            pick = self.sensor_pick()
            if pick is not None and not pick.sensors:
                return False
        return True

    def _update_button(self) -> None:
        if self.reading is not None:
            self.will.setText(self._will(self.reading.operation))
        self.run_button.setEnabled(self.ready() and not self.finished)

    # ---- doing it ----------------------------------------------------------------------------------------

    def run(self) -> bool:
        """
        The button: the operation done with the card's values, recorded in the audit log (kind command, as 文で操作's
        were: a log's readers and `auditLog.events` go on as they were); False when not.
        """
        if self.finished or not self.ready():
            return False
        reading = self.chosen()
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
        if reading.sensors:
            detail["sensors"] = reading.sensors
        if reading.screen:
            detail["screen"] = reading.screen
        try:
            said = self.host.run(reading, self.place_found)
        except Exception as error:  # noqa: BLE001 (a screen that could not be opened: said, recorded, the card stays)
            audit.record("command", op.name, op.title, result="error", detail=detail, error=str(error))
            self.state.setText(self.tr("実行に失敗: {m}").format(m=error_text(error)))
            self.state.setStyleSheet(WARN_STYLE)
            self.state.show()
            return False
        audit.record("command", op.name, op.title, detail=detail)
        self.message = said or self.tr("{t} を表示").format(t=op.title or op.name)
        self._finish(self.tr("実行した: {m}").format(m=self.message))
        self.on_done(self.message)
        return True

    def leave(self) -> None:
        """「取りやめ」: nothing done; the card stays as it was, its buttons off."""
        if not self.finished:
            self._finish(self.tr("取りやめた"))

    def _finish(self, said: str) -> None:
        self.finished = True
        self.state.setText(said)
        self.state.setStyleSheet(PLAIN_STYLE)
        self.state.show()
        for w in (self.operation, *self.rows.values()):
            w.setEnabled(False)
        self.run_button.setEnabled(False)
        self.cancel_button.hide()
