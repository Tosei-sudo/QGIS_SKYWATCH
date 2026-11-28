"""
「データに質問」 (core/data_chat.py, qgis_io/data_chat_io.py; tsudo 2026-11-24):
a question typed on the data, answered with a few sentences and the table
they come from. Each answer is a card: the question, the sentence (the
generative AI's, checked against the table, else the program's), the table
(read and counted by the program), 「どう調べたか」 (the SQL, the tables read and
their rows), and the table to 「データ分析」, a CSV file or the clipboard. The
newest card is on top; 「新しい話」 forgets the earlier questions (a question
may follow up the ones before: 「では先々月は？」).

Shown only when the settings give データに質問 a model and the person uses the
generative AI's features (opt-in).
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFontDatabase
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from ..core import audit, errors
from ..core.analysis import cell_text, csv_text, tsv_text
from ..qgis_io.data_chat_io import Answer, DataChat, columns_of, opened_in_analysis
from .docks import PluginDock, plain_tip
from .flow_layout import FlowLayout

TITLE = "データに質問"
#: The rows a card shows (all of them go to CSV, the clipboard and 「データ分析」).
SHOWN_ROWS = 200
#: The cards kept (older ones go).
CARDS = 20
#: How often the stage is shown while it works (ms).
SHOW_MS = 300
FOOTNOTE = "文は生成 AI が書き、表と照合してから出す。表と数はプログラムが読んで数えたもの（生成 AI は表の中身を作らない）。"


class AnswerCard(QFrame):
    """One question and its answer."""

    def __init__(self, answer: Answer, dock: "DataChatDock"):
        super().__init__()
        self.answer = answer
        self.dock = dock
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        q = QLabel(self.tr("Q. {q}").format(q=answer.question))
        q.setWordWrap(True)
        q.setStyleSheet("font-weight: 600;")
        q.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(q)
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
        self.how.setStyleSheet("color: palette(mid);")
        lay.addWidget(self.how)
        self.table: Optional[QTableWidget] = None
        result = answer.result
        if result is not None and not answer.refused:
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
                more.setStyleSheet("color: palette(mid);")
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
        if result is not None and not answer.refused:
            for key, text, tip, slot in [
                ("analysis", self.tr("データ分析で開く"), self.tr("この SQL を「データ分析」に入れる（続きの集計・保存・レイヤーにする）"),
                 lambda: dock.open_in_analysis(self.answer)),
                ("csv", self.tr("CSV 保存…"), self.tr("表を CSV ファイルに保存"), lambda: dock.save_csv(self.answer)),
                ("copy", self.tr("表をコピー"), self.tr("表をタブ区切りでクリップボードへ"), lambda: dock.copy(self.answer)),
            ]:
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


class DataChatDock(PluginDock):
    """「データに質問」."""

    def __init__(self, iface, context, open_settings: Callable[[], None], parent=None,
                 analysis: Optional[Callable[[], object]] = None):
        super().__init__("SatObsDataChat", TITLE, context, open_settings, parent)
        self.iface = iface
        self.chat = DataChat(context, self)
        #: The dock 「データ分析」 (shown), for 「データ分析で開く」.
        self._analysis = analysis
        self.cards: list[AnswerCard] = []
        self._started = 0.0
        self._stage_text = ""

        intro = QLabel(self.tr("ミカゲにつながっている表（観測記録・撮像依頼・撮像予定・通報など）に、文で質問できます。"
                               "読むだけで、表を変えることはしません。"))
        intro.setWordWrap(True)
        self.body.addWidget(intro)
        row = QHBoxLayout()
        self.field = QLineEdit()
        self.field.setObjectName("SatObsDataChatField")
        self.field.setClearButtonEnabled(True)
        self.field.setPlaceholderText(self.tr("例: 先月の舞鶴の記録は何件？"))
        self.field.setToolTip(plain_tip(self.tr("質問を書いて Enter。続けて「では先々月は？」のように聞ける（「新しい話」で前の質問を忘れる）")))
        self.field.returnPressed.connect(lambda: self.ask())
        row.addWidget(self.field, 1)
        self.ask_button = QPushButton(self.tr("聞く"))
        self.ask_button.clicked.connect(lambda _on=False: self.ask())
        row.addWidget(self.ask_button)
        self.body.addLayout(row)
        flow = FlowLayout()
        self.cancel_button = QPushButton(self.tr("取消"))
        self.cancel_button.setToolTip(plain_tip(self.tr("作成中の答えを止める")))
        self.cancel_button.clicked.connect(lambda _on=False: self.chat.cancel())
        self.new_button = QPushButton(self.tr("新しい話"))
        self.new_button.setToolTip(plain_tip(self.tr("前の質問を忘れて、新しく聞く（答えのカードは残る）")))
        self.new_button.clicked.connect(lambda _on=False: self.new_conversation())
        self.clear_button = QPushButton(self.tr("カードを消す"))
        self.clear_button.setToolTip(plain_tip(self.tr("答えのカードを全部消す（前の質問も忘れる）")))
        self.clear_button.clicked.connect(lambda _on=False: self.clear())
        for b in (self.cancel_button, self.new_button, self.clear_button):
            flow.addWidget(b)
        holder = QWidget()
        holder.setLayout(flow)
        self.body.addWidget(holder)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        self.body.addLayout(self.list)
        self.body.addStretch(1)
        foot = QLabel(self.tr(FOOTNOTE))
        foot.setWordWrap(True)
        foot.setStyleSheet("color: palette(mid);")
        self.body.addWidget(foot)
        self._timer = QTimer(self)
        self._timer.setInterval(SHOW_MS)
        self._timer.timeout.connect(self._show_stage)
        self.chat.assistant.changed.connect(self.apply)
        self.visibilityChanged.connect(lambda on: self.chat.warm() if on and self.chat.offered else None)
        self.apply()

    # ---- shown or not -------------------------------------------------------------------------------

    def apply(self) -> None:
        """The question field usable while the settings give the task a model and the person uses the generative AI."""
        on = self.chat.offered
        self.field.setEnabled(on)
        self._update_buttons()
        if not on:
            self.say(self.tr("使えない: 生成 AI のモデルがないか（管理者に連絡: textAssist.tasks.dataChat）、個人の設定で生成 AI の機能を使わない"))
        elif not self.chat.busy:
            self.say("")

    def _update_buttons(self) -> None:
        on, busy = self.chat.offered, self.chat.busy
        self.ask_button.setEnabled(on and not busy)
        self.cancel_button.setEnabled(busy)
        self.new_button.setEnabled(bool(self.chat.history))
        self.clear_button.setEnabled(bool(self.cards))

    def say(self, text: str) -> None:
        self.status.setText(text)

    # ---- asking -------------------------------------------------------------------------------------

    def ask(self, text: Optional[str] = None) -> bool:
        """Asks `text` (else the field's); False when it does not start."""
        question = (text if text is not None else self.field.text()).strip()
        if not question:
            self.say(self.tr("質問を入力"))
            return False
        if text is not None:
            self.field.setText(question)
        if self.chat.busy:
            self.say(self.tr("前の質問に答えている途中（終わってから。止めるなら「取消」）"))
            return False
        self._started = time.monotonic()
        self._stage_text = ""
        self._timer.start()
        started = self.chat.ask(question, self._answered, self._stage)
        if not started:
            self._timer.stop()
            self.apply()
            return False
        self._update_buttons()
        return True

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

    def _answered(self, answer: Answer) -> None:
        if sip.isdeleted(self):
            return
        self._timer.stop()
        if answer.refused == "取消":
            self.say(self.tr("取消"))
            self._update_buttons()
            return
        card = AnswerCard(answer, self)
        self.list.insertWidget(0, card)
        self.cards.insert(0, card)
        while len(self.cards) > CARDS:
            old = self.cards.pop()
            self.list.removeWidget(old)
            old.deleteLater()
        self.field.clear()
        self.say("")
        self._update_buttons()

    def new_conversation(self) -> None:
        self.chat.new_conversation()
        self.say(self.tr("新しい話（前の質問は使わない）"))
        self._update_buttons()

    def clear(self) -> None:
        for c in self.cards:
            self.list.removeWidget(c)
            c.deleteLater()
        self.cards = []
        self.chat.new_conversation()
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
