"""
「ミカゲと話す」 (core/chat.py; tsudo 2026-11-28: 「文で操作の機能は全廃しよう。
名前は任せるけど、チャットで質問も操作もできるように」, design doc 「チャットで
質問と操作（文で操作の廃止） 設計方針案」, 「この案で進める」): one chat where a
sentence asks about the data or asks for an operation. The older messages are
on top, the field at the bottom; each sentence typed shows, then what it got:

- an answer from the tables (データに質問 until then: the sentence, checked
  against the table, the table read and counted by the program, 「どう調べた
  か」, the table to 「データ分析」, a CSV file or the clipboard; images and
  records it lists open from its buttons);
- the images of a place (画像カタログ searched behind the screen: how many,
  the newest with 「開く」, 「カタログで見る」);
- an operation's card (文で操作's until then: the operation and the values
  read, nothing done before its button);
- what a follow-up opened (「いちばん新しいのを開いて」, 「2枚目を地図で」: the
  list shown last).

Code routes each sentence (core/chat.route) and the generative AI is asked
only at the end. 「新しい話」 forgets the earlier questions and the list shown
(a question may follow up the ones before: 「では先々月は？」).

Shown only when the settings give ミカゲと話す a model (`textAssist.tasks.chat`;
dataChat and command read as it) and the person uses the generative AI's
features (opt-in).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFontDatabase
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from ..core import audit, chat, errors
from ..core.analysis import cell_text, csv_text, tsv_text
from ..core.text_command import BY_KEY, Operation, Reading, read, word_matches
from ..core.util import iso_text, ms_of
from ..qgis_io.data_chat_io import Answer, DataChat, columns_of, opened_in_analysis
from .chat_operation import ChatHost, ImagesFound, OperationCard, catalog_sensors
from .docks import PluginDock, plain_tip
from .flow_layout import FlowLayout

TITLE = chat.TITLE
#: The rows a card shows (all of them go to CSV, the clipboard and 「データ分析」).
SHOWN_ROWS = 200
#: The images a card of 画像検索 lists (the newest).
SHOWN_IMAGES = 10
#: The sentences and cards kept (older ones go).
ITEMS = 60
#: How often the stage is shown while it works (ms).
SHOW_MS = 300
FOOTNOTE = ("答えの文は生成 AI が書き、表と照合してから出す。表と数はプログラムが読んで数えたもの（生成 AI は表の中身を作らない）。"
            "操作はカードのボタンを押すまで何もしない。書き出し・取り込み・保存・送信は開いた画面のボタンで。")
JST = timezone(timedelta(hours=9))
NOTE_STYLE = "QLabel { color: palette(mid); }"


def jst_text(value) -> str:
    """A time of the catalog (ms, ISO …) as 「2026-11-27 10:32」 (JST); "" when not one."""
    ms = ms_of(value)
    return datetime.fromtimestamp(ms / 1000.0, JST).strftime("%Y-%m-%d %H:%M") if ms is not None else ""


class Said(QWidget):
    """A sentence the person typed (on the right)."""

    def __init__(self, text: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(40, 0, 0, 0)
        row.addStretch(1)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setStyleSheet("QLabel { background: palette(alternate-base); border: 1px solid palette(mid); border-radius: 6px;"
                                 " padding: 4px 8px; }")
        row.addWidget(self.label)


class NoteCard(QLabel):
    """A short reply of the chat (what a follow-up opened, why nothing was done)."""

    def __init__(self, text: str):
        super().__init__(text)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setContentsMargins(4, 0, 4, 0)


class AnswerCard(QFrame):
    """One question's answer from the tables."""

    def __init__(self, answer: Answer, dock: "ChatDock"):
        super().__init__()
        self.answer = answer
        self.dock = dock
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        self.sentence = QLabel(answer.sentence or answer.refused)
        self.sentence.setWordWrap(True)
        self.sentence.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.sentence)
        if answer.refused:
            how = self.tr("答えられなかった")
        elif answer.by_model:
            how = self.tr("文: 生成 AI（表と照合済み）")
        else:
            how = self.tr("文: プログラム（生成 AI の文は使わなかった）")
        took = self.tr("{s:.0f} 秒").format(s=answer.seconds) if answer.seconds >= 1 else ""
        self.how = QLabel(" · ".join(x for x in (how, took) if x))
        self.how.setStyleSheet(NOTE_STYLE)
        lay.addWidget(self.how)
        self.table: Optional[QTableWidget] = None
        result = answer.result
        #: The images or records the table lists (core/chat.Shown), for its buttons and a follow-up.
        self.shown: Optional[chat.Shown] = None
        if result is not None and not answer.refused:
            self.shown = chat.shown_of(result.columns, result.rows, answer.labels)
            t = QTableWidget(min(len(result.rows), SHOWN_ROWS), len(result.columns))
            t.setHorizontalHeaderLabels(columns_of(answer))
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            t.verticalHeader().setVisible(False)
            for i, row in enumerate(result.rows[:SHOWN_ROWS]):
                for j, v in enumerate(row):
                    item = QTableWidgetItem(cell_text(v))
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                    t.setItem(i, j, item)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            t.horizontalHeader().setStretchLastSection(True)
            rows = min(len(result.rows), 8)
            t.setFixedHeight(t.horizontalHeader().height() + max(rows, 1) * t.verticalHeader().defaultSectionSize() + 6)
            lay.addWidget(t)
            self.table = t
            if len(result.rows) > SHOWN_ROWS:
                more = QLabel(self.tr("表示は先頭 {n:,} 行（全 {m:,} 行は CSV・コピー・データ分析で）").format(n=SHOWN_ROWS, m=len(result.rows)))
                more.setStyleSheet(NOTE_STYLE)
                lay.addWidget(more)
        self.detail = QPlainTextEdit(answer.describe())
        self.detail.setReadOnly(True)
        self.detail.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.detail.setMaximumHeight(160)
        self.detail.setVisible(False)
        flow = FlowLayout()
        self.toggle = QToolButton()
        self.toggle.setText(self.tr("どう調べたか"))
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolTip(plain_tip(self.tr("生成 AI が書いた SQL、読んだ表と行数、途中で直したこと")))
        self.toggle.toggled.connect(self._toggled)
        self.toggle.setEnabled(bool(answer.describe().strip()))
        flow.addWidget(self.toggle)
        self.buttons: dict[str, QPushButton] = {}
        entries = []
        shown = self.shown
        if shown is not None and shown.kind == "image":
            if len(shown.ids) == 1:
                entries.append(("open", self.tr("画像を開く"), self.tr("この画像を開く（画像 ID で開く）"),
                                lambda: dock.open_shown(shown, [0])))
            entries.append(("catalog", self.tr("カタログで見る"), self.tr("表の画像を画像カタログに一覧（そこから開く）"),
                            lambda: dock.list_in_catalog(shown)))
        elif shown is not None:
            entries.append(("record", self.tr("記録を開く"), self.tr("表の最初の記録を観測記録のフォームに（ほかは「2件目を開いて」のように）"),
                            lambda: dock.open_shown(shown, [0])))
        if result is not None and not answer.refused:
            entries += [
                ("analysis", self.tr("データ分析で開く"), self.tr("この SQL を「データ分析」に入れる（続きの集計・保存・レイヤーにする）"),
                 lambda: dock.open_in_analysis(self.answer)),
                ("csv", self.tr("CSV 保存…"), self.tr("表を CSV ファイルに保存"), lambda: dock.save_csv(self.answer)),
                ("copy", self.tr("表をコピー"), self.tr("表をタブ区切りでクリップボードへ"), lambda: dock.copy(self.answer)),
            ]
        for key, text, tip, slot in entries:
            b = QPushButton(text)
            b.setToolTip(plain_tip(tip))
            b.clicked.connect(lambda _on=False, slot=slot: slot())
            flow.addWidget(b)
            self.buttons[key] = b
        holder = QWidget()
        holder.setLayout(flow)
        lay.addWidget(holder)
        lay.addWidget(self.detail)

    def _toggled(self, on: bool) -> None:
        self.detail.setVisible(on)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)


class ImagesCard(QFrame):
    """The images 画像検索 found: how many, the newest with 「開く」, 「カタログで見る」."""

    def __init__(self, found: ImagesFound, dock: "ChatDock"):
        super().__init__()
        self.found = found
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        n, total = len(found.records), found.total
        if not n:
            said = self.tr("{w}: 画像なし").format(w=found.said)
        elif total is not None and total > n:
            said = self.tr("{w}: {t:,} 枚（新しい {n:,} 枚を読んだ）").format(w=found.said, t=total, n=n)
        else:
            said = self.tr("{w}: {n:,} 枚").format(w=found.said, n=n)
        self.said = QLabel(said)
        self.said.setWordWrap(True)
        self.said.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.said)
        self.open_buttons: list[QPushButton] = []
        if n:
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            for i, r in enumerate(found.records[:SHOWN_IMAGES]):
                for j, text in enumerate((f"{i + 1}.", jst_text(r.acquired), r.sensor or "", r.id or "")):
                    label = QLabel(text)
                    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                    grid.addWidget(label, i, j)
                b = QPushButton(self.tr("開く"))
                b.setToolTip(plain_tip(self.tr("この画像を開く（画像カタログの一覧から開くのと同じ）")))
                b.clicked.connect(lambda _on=False, i=i: dock.open_shown(self.shown, [i]))
                grid.addWidget(b, i, 4)
                self.open_buttons.append(b)
            grid.setColumnStretch(3, 1)
            lay.addLayout(grid)
            more = self.tr("番号で「2枚目を開いて」「いちばん新しいのを開いて」とも書ける")
            if n > SHOWN_IMAGES:
                more = self.tr("新しい {k} 枚を表示（ほかは「カタログで見る」で）。").format(k=SHOWN_IMAGES) + more
            hint = QLabel(more)
            hint.setWordWrap(True)
            hint.setStyleSheet(NOTE_STYLE)
            lay.addWidget(hint)
        flow = FlowLayout()
        self.catalog_button = QPushButton(self.tr("カタログで見る"))
        self.catalog_button.setToolTip(plain_tip(self.tr("画像カタログを前に出す（この検索の一覧）")))
        self.catalog_button.clicked.connect(lambda _on=False: dock.show_found(found))
        flow.addWidget(self.catalog_button)
        holder = QWidget()
        holder.setLayout(flow)
        lay.addWidget(holder)
        #: The images listed, for a follow-up (their times as ISO text, for 「いちばん新しい」).
        times = []
        for r in found.records:
            ms = ms_of(r.acquired)
            times.append(iso_text(ms) if ms is not None else "")
        self.shown = chat.Shown("image", tuple(r.id or "" for r in found.records), tuple(times))


class ChatDock(PluginDock):
    """「ミカゲと話す」."""

    def __init__(self, iface, context, open_settings: Callable[[], None], parent=None,
                 host: Optional[ChatHost] = None, analysis: Optional[Callable[[], object]] = None):
        super().__init__("SatObsDataChat", TITLE, context, open_settings, parent)
        self.iface = iface
        self.chat = DataChat(context, self)
        #: The plugin's operations and screens (features/chat.py).
        self.host = host
        #: The dock 「データ分析」 (shown), for 「データ分析で開く」.
        self._analysis = analysis
        #: The sentences and cards, oldest first.
        self.items: list[QWidget] = []
        #: The list a follow-up points at (the last answer's images or records), and the images found with it.
        self.shown: Optional[chat.Shown] = None
        self._found: Optional[ImagesFound] = None
        #: What the status bar was told last (for tests and the console).
        self.message = ""
        self._searching = False
        self._started = 0.0
        self._stage_text = ""

        intro = QLabel(self.tr("ミカゲのデータ（観測記録・撮像依頼・撮像予定・通報など）への質問と、操作の頼み"
                               "（計画ファイルの出力、画像の検索、画面を開く…）を文で書けます。質問は表を読むだけで変えません。"))
        intro.setWordWrap(True)
        self.body.addWidget(intro)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("SatObsDataChatList")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.list = QVBoxLayout(holder)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(8)
        self.list.addStretch(1)
        self.scroll.setWidget(holder)
        # Room for a card or two however small the dock (the intro above wraps to several lines when narrow).
        self.scroll.setMinimumHeight(220)
        #: Whether the list follows its bottom (a sentence or card added, or the person at the bottom): a card
        #: laid out later (its table, an answer's text) keeps it there; scrolled up to read, it stays put.
        self._at_bottom = True
        bar = self.scroll.verticalScrollBar()
        bar.valueChanged.connect(lambda v: setattr(self, "_at_bottom", v >= self.scroll.verticalScrollBar().maximum() - 4))
        bar.rangeChanged.connect(self._ranged)
        self.body.addWidget(self.scroll, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        row = QHBoxLayout()
        self.field = QLineEdit()
        self.field.setObjectName("SatObsDataChatField")
        self.field.setClearButtonEnabled(True)
        self.field.setPlaceholderText(self.tr("例: 先月の舞鶴の記録は何件？ / 呉の今週のSAR画像ある？ / Aシステムの明日の計画ファイルを出力"))
        self.field.setToolTip(plain_tip(self.tr(
            "質問か、したいことを書いて Enter。質問は表から答える（続けて「では先々月は？」）。操作はカードが出て、ボタンを押すまで何もしない。"
            "答えの画像・記録は「2枚目を開いて」のように開ける（「新しい話」で前の質問と一覧を忘れる）")))
        self.field.returnPressed.connect(lambda: self.send())
        row.addWidget(self.field, 1)
        self.send_button = QPushButton(self.tr("送る"))
        self.send_button.clicked.connect(lambda _on=False: self.send())
        row.addWidget(self.send_button)
        self.body.addLayout(row)
        flow = FlowLayout()
        self.cancel_button = QPushButton(self.tr("取消"))
        self.cancel_button.setToolTip(plain_tip(self.tr("作成中の答えを止める")))
        self.cancel_button.clicked.connect(lambda _on=False: self.cancel())
        self.new_button = QPushButton(self.tr("新しい話"))
        self.new_button.setToolTip(plain_tip(self.tr("前の質問と答えの一覧を忘れて、新しく話す（カードは残る）")))
        self.new_button.clicked.connect(lambda _on=False: self.new_conversation())
        self.clear_button = QPushButton(self.tr("会話を消す"))
        self.clear_button.setToolTip(plain_tip(self.tr("文とカードを全部消す（前の質問も忘れる）")))
        self.clear_button.clicked.connect(lambda _on=False: self.clear())
        for b in (self.cancel_button, self.new_button, self.clear_button):
            flow.addWidget(b)
        buttons = QWidget()
        buttons.setLayout(flow)
        self.body.addWidget(buttons)
        foot = QLabel(self.tr(FOOTNOTE))
        foot.setWordWrap(True)
        foot.setStyleSheet(NOTE_STYLE)
        self.body.addWidget(foot)
        self._timer = QTimer(self)
        self._timer.setInterval(SHOW_MS)
        self._timer.timeout.connect(self._show_stage)
        self.chat.assistant.changed.connect(self.apply)
        self.visibilityChanged.connect(lambda on: self.chat.warm() if on and self.chat.offered else None)
        self.apply()

    # ---- shown or not -------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.chat.busy or self._searching

    def apply(self) -> None:
        """The field usable while the settings give the task a model and the person uses the generative AI."""
        on = self.chat.offered
        self.field.setEnabled(on)
        self._update_buttons()
        if not on:
            self.say(self.tr("使えない: 生成 AI のモデルがないか（管理者に連絡: textAssist.tasks.chat）、個人の設定で生成 AI の機能を使わない"))
        elif not self.busy:
            self.say("")

    def _update_buttons(self) -> None:
        on, busy = self.chat.offered, self.busy
        self.send_button.setEnabled(on and not busy)
        self.cancel_button.setEnabled(busy)
        self.new_button.setEnabled(bool(self.chat.history) or self.shown is not None)
        self.clear_button.setEnabled(bool(self.items))

    def say(self, text: str) -> None:
        self.status.setText(text)

    def _tell(self, message: str) -> None:
        """What was done, in the status bar too."""
        self.message = message
        try:
            self.iface.statusBarIface().showMessage(self.tr("ミカゲと話す: {m}").format(m=message), 8000)
        except (AttributeError, RuntimeError):
            pass

    # ---- the list --------------------------------------------------------------------------------------

    def _add(self, widget: QWidget) -> QWidget:
        """A sentence or a card at the bottom (the oldest go past ITEMS), the list scrolled to it."""
        self.list.insertWidget(self.list.count(), widget)
        self.items.append(widget)
        while len(self.items) > ITEMS:
            old = self.items.pop(0)
            self.list.removeWidget(old)
            old.deleteLater()
        self._at_bottom = True
        QTimer.singleShot(0, self._to_bottom)
        self._update_buttons()
        return widget

    def _to_bottom(self) -> None:
        if not sip.isdeleted(self):
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _ranged(self, _low: int, high: int) -> None:
        """The list grew (a card added or laid out): kept at its bottom while it follows it."""
        if self._at_bottom:
            self.scroll.verticalScrollBar().setValue(high)

    def cards(self, kind: type) -> list:
        """The cards of a kind, oldest first."""
        return [w for w in self.items if isinstance(w, kind)]

    def last(self, kind: type = QWidget) -> Optional[QWidget]:
        """The newest card of a kind (None when there is none)."""
        found = self.cards(kind)
        return found[-1] if found else None

    def note(self, text: str) -> NoteCard:
        return self._add(NoteCard(text))

    # ---- a sentence --------------------------------------------------------------------------------------

    def send(self, text: Optional[str] = None) -> bool:
        """Sends `text` (else the field's): routed by code (core/chat.route), the model asked at the end. False when it does not start."""
        sentence = (text if text is not None else self.field.text()).strip()
        if not sentence:
            self.say(self.tr("文を入力"))
            return False
        if not self.chat.offered:
            self.apply()
            return False
        if self.busy:
            self.say(self.tr("前の文に答えている途中（終わってから。止めるなら「取消」）"))
            return False
        h = self.host
        ops = h.operations() if h is not None else []
        screens = h.screens() if h is not None else []
        words = self.chat.words()
        images = None
        known = point = False
        if h is not None and any(o.key == "catalog" for o in ops):
            images = self._read(BY_KEY["catalog"], sentence, "words")
            known = bool(images.place) and h.known_place(images.place)
            point = images.point is not None
        route = chat.route(sentence, self.shown, ops, words, screens, known, point)
        self._add(Said(sentence))
        self.field.clear()
        self.say("")
        if route.kind == chat.FOLLOW:
            self.open_shown(self.shown, list(route.opened))
        elif route.kind == chat.IMAGES:
            self._search(images)
        elif route.kind == chat.OPERATION:
            self._operation(sentence, ops, self._read(route.operation, sentence, "words"))
        elif chat.asks(sentence) or not ops:
            self._ask(sentence, ops)
        else:
            self._pick(sentence, ops, question=False)
        return True

    def _read(self, op: Operation, text: str, by: str) -> Reading:
        h = self.host
        return read(op, text, h.today(), h.systems(), h.place_names(), h.satellites(), h.screens(), by=by,
                    sensors=catalog_sensors(h))

    def _start(self) -> None:
        self._started = time.monotonic()
        self._stage_text = ""
        self._timer.start()
        self._update_buttons()

    def _stop(self) -> None:
        self._timer.stop()
        self.say("")
        self._update_buttons()

    # ---- a question: the tables ----------------------------------------------------------------------

    def _ask(self, question: str, ops: Sequence[Operation]) -> None:
        self._start()

        def answered(answer: Answer) -> None:
            if sip.isdeleted(self):
                return
            if answer.cannot and ops and answer.refused != "取消":
                # 「NG」: the tables do not answer it; an operation may be what was meant (「今年の年間上限はあとどれくらい？」).
                self._pick(question, ops, question=True, refused=answer)
                return
            self._answered(answer)

        if not self.chat.ask(question, answered, self._stage):
            self._stop()
            self.apply()

    def _answered(self, answer: Answer) -> None:
        self._stop()
        if answer.refused == "取消":
            self.note(self.tr("取消"))
            return
        card = self._add(AnswerCard(answer, self))
        if card.shown is not None:
            self.shown, self._found = card.shown, None

    # ---- an operation ---------------------------------------------------------------------------------

    def _pick(self, text: str, ops: Sequence[Operation], question: bool, refused: Optional[Answer] = None) -> None:
        """The model chooses the operation (「操作: 名前」); none: the tables asked (not a question), else said."""
        self._start()
        self._stage(self.tr("生成 AI が操作を選択中…"))

        def done(op: Optional[Operation], raw: str, seconds: float) -> None:
            if sip.isdeleted(self):
                return
            if op is not None:
                self._stop()
                self._operation(text, ops, self._read(op, text, "model"), seconds,
                                self.tr("表からは答えられないため、操作として読んだ") if question else "")
                return
            if refused is not None:
                self._answered(refused)
            elif not question:
                # No operation: it may still be asked of the tables (「呉の船の記録を一覧」).
                self._timer.stop()
                self._ask_after_pick(text, ops, raw)
            else:
                self._stop()

        def failed(message: str) -> None:
            if sip.isdeleted(self):
                return
            if refused is not None:
                self._answered(refused)
                return
            self._stop()
            if message != "取消":
                self._operation(text, ops, None, problem=self.tr("生成 AI で読めない: {m}（下で選べば、文から値を読む）").format(m=message))
            else:
                self.note(self.tr("取消"))

        if not self.chat.pick_operation(text, list(ops), done, failed):
            self._stop()
            self.apply()

    def _ask_after_pick(self, text: str, ops: Sequence[Operation], raw: str) -> None:
        """No operation chosen for a sentence that is no question: the tables asked; neither: a card to choose one by hand."""
        self._start()

        def answered(answer: Answer) -> None:
            if sip.isdeleted(self):
                return
            if answer.refused and answer.refused != "取消" and ops:
                self._stop()
                found = word_matches(text, ops, self.chat.words())
                why = (self.tr("候補: {c}。下で選べば、文から値を読む").format(c="・".join(o.name for o in found)) if found else
                       self.tr("表からも答えられず、合う操作も見つからず（生成 AI の答え: {a}）。下で選べば、文から値を読む").format(
                           a=raw.strip().splitlines()[0][:40] if raw.strip() else self.tr("なし")))
                self._operation(text, ops, None, problem=why)
                return
            self._answered(answer)

        if not self.chat.ask(text, answered, self._stage):
            self._stop()
            self.apply()

    def _operation(self, text: str, ops: Sequence[Operation], reading: Optional[Reading], seconds: float = 0.0,
                   note: str = "", problem: str = "") -> OperationCard:
        card = OperationCard(self.host)
        caution = self.chat.assistant.caution(chat.TASK) if reading is not None and reading.by == "model" else ""
        card.show_reading(reading, text, ops, seconds, "　".join(x for x in (note, caution) if x), problem)
        card.on_done = self._tell
        return self._add(card)

    # ---- images ----------------------------------------------------------------------------------------

    def _search(self, reading: Reading) -> None:
        """画像検索 behind the screen: the place found (a named area, a point; none: the map's extent), then the catalog."""
        h = self.host
        self._searching = True
        self._start()
        self._stage(self.tr("画像カタログを検索中…"))

        def over(found: Optional[ImagesFound], problem: str) -> None:
            if sip.isdeleted(self):
                return
            self._searching = False
            self._stop()
            if found is None:
                self.note(self.tr("画像を検索できない: {m}").format(m=problem))
                return
            card = self._add(ImagesCard(found, self))
            self.shown, self._found = card.shown, found

        def with_place(place, problem: str) -> None:
            if sip.isdeleted(self):
                return
            if place is None and (reading.place or reading.point):
                over(None, problem or self.tr("場所が見つからない"))
                return
            h.search_images(reading, place, over)

        if reading.place or reading.point:
            h.find_place(reading.place or "", reading.point, with_place)
        else:
            with_place(None, "")

    def open_shown(self, shown: Optional[chat.Shown], indexes: Sequence[int]) -> str:
        """The images or records of a list opened (a follow-up, a card's button); what was done, said in the chat."""
        h = self.host
        if shown is None or h is None:
            return ""
        indexes = [i for i in indexes if 0 <= i < len(shown.ids)][:chat.OPEN_LIMIT]
        if not indexes:
            return ""
        if shown.kind == "record":
            said = h.open_records([shown.ids[i] for i in indexes])
        elif self._found is not None and shown is self.shown and len(self._found.records) == len(shown.ids):
            said = h.open_images(self._found, indexes)
        else:
            card = next((c for c in self.cards(ImagesCard) if c.shown is shown), None)
            said = h.open_images(card.found, indexes) if card is not None else h.open_image_ids([shown.ids[i] for i in indexes])
        if said:
            self.note(said)
            self._tell(said)
        return said

    def show_found(self, found: ImagesFound) -> None:
        if self.host is not None:
            self._tell(self.host.show_images(found))

    def list_in_catalog(self, shown: chat.Shown) -> None:
        if self.host is not None:
            self._tell(self.host.list_image_ids(list(shown.ids)))

    # ---- the stage --------------------------------------------------------------------------------------

    def _stage(self, text: str) -> None:
        if not sip.isdeleted(self):
            self._stage_text = text
            self._show_stage()

    def _show_stage(self) -> None:
        stage = self._stage_text
        model_stage = self.chat.stage()
        if model_stage and "読み込み" in model_stage:
            stage = model_stage
        seconds = time.monotonic() - self._started
        self.say(f"{stage}（{seconds:.0f} 秒）" if stage else "")

    # ---- the conversation ---------------------------------------------------------------------------

    def new_conversation(self) -> None:
        self.chat.new_conversation()
        self.shown, self._found = None, None
        self.say(self.tr("新しい話（前の質問と一覧は使わない）"))
        self._update_buttons()

    def clear(self) -> None:
        for w in self.items:
            self.list.removeWidget(w)
            w.deleteLater()
        self.items = []
        self.chat.new_conversation()
        self.shown, self._found = None, None
        self._update_buttons()

    def cancel(self) -> None:
        self.chat.cancel()

    # ---- the table elsewhere ------------------------------------------------------------------------------

    def open_in_analysis(self, answer: Answer) -> bool:
        """「データ分析で開く」: the SQL in the dock 「データ分析」 (shown), the tables' read conditions with it."""
        dock = self._analysis() if self._analysis is not None else None
        if dock is None:
            self.say(self.tr("「データ分析」を開けない"))
            return False
        opened_in_analysis(dock, answer, answer.conditions)
        self.say(self.tr("「データ分析」に SQL を入れた（実行は「実行」で）"))
        return True

    def save_csv(self, answer: Answer, path: Optional[str] = None) -> Optional[str]:
        if answer.result is None:
            return None
        if path is None:
            path, _f = QFileDialog.getSaveFileName(self, self.tr("答えの表を保存"), "data_chat.csv", self.tr("CSV (*.csv)"))
            if not path:
                return None
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(csv_text(answer.result))
        except OSError as error:
            reason, said = errors.describe(error)
            self.say(self.tr("保存に失敗: {m}").format(m=reason))
            audit.error("file-save", path, reason, detail={"what": "data-chat-csv", "said": said})
            return None
        audit.record("export", "data-chat-csv", path, count=len(answer.result.rows), detail={"question": answer.question}, file=path)
        self.say(self.tr("{n:,} 行を保存: {p}").format(n=len(answer.result.rows), p=os.path.basename(path)))
        return path

    def copy(self, answer: Answer) -> None:
        if answer.result is None:
            return
        text = tsv_text(answer.result)
        QApplication.clipboard().setText(text)
        audit.record("copy", "data-chat-result", count=len(answer.result.rows), detail={"question": answer.question}, text=text)
        self.say(self.tr("{n:,} 行をコピー").format(n=len(answer.result.rows)))

    def close_all(self) -> None:
        self.chat.cancel()
        self._timer.stop()
