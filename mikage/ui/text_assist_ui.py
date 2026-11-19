"""
文章の手伝い on the screens (core/text_assist.py, qgis_io/text_assist_io.py):

- NoteTidy: a 「整える」 button beside a 備考 field (観測記録・確認範囲). The
  備考 is written again by the model in the background; the proposal shows
  under the field with 「置き換える」 and 「閉じる」, and what to check in it
  (numbers it does not have, much longer). The field is changed only by
  「置き換える」 (as typing does: Ctrl+Z takes it back), never saved by it.
- HandoverDraft: 「申し送りの下書き」 under the 申し送り of the handover memo,
  made from the memo's lists (made first when there are none), with
  「置き換える」・「末尾に足す」・「閉じる」.

Nothing shows without the settings' `textAssist`.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ..core.text_assist import Draft, handover_draft, handover_messages, note_draft, note_messages, note_terms
from ..qgis_io.text_assist_io import SHOW_MS, Asked

WRITING_STYLE = "QLabel { background: #f1f5f9; color: #334155; padding: 3px; }"
OFFER_STYLE = "QLabel { background: #ecfdf5; color: #065f46; padding: 3px; }"
WARN_STYLE = "QLabel { background: #fef3c7; color: #92400e; padding: 3px; }"
PLAIN_STYLE = "QLabel { color: #57534e; padding: 3px; }"


class ProposalPanel(QWidget):
    """Under a field: the text being written, then the proposal with its buttons (or why there is none)."""

    def __init__(self, append: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        box.addWidget(self.label)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(WARN_STYLE)
        box.addWidget(self.warning)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.replace_button = QPushButton(self.tr("置き換える"))
        self.append_button = QPushButton(self.tr("末尾に足す"))
        self.cancel_button = QPushButton(self.tr("取消"))
        self.close_button = QPushButton(self.tr("閉じる"))
        for b in (self.replace_button, self.append_button, self.cancel_button, self.close_button):
            b.setAutoDefault(False)
            row.addWidget(b)
        row.addStretch(1)
        box.addLayout(row)
        self._append = append
        self.close_button.clicked.connect(lambda _on=False: self.hide())
        self.draft: Optional[Draft] = None
        self.hide()

    def writing(self, stage: str, text: str = "") -> None:
        self.draft = None
        shown = text.strip()
        self.label.setText(f"{stage} {shown}" if shown else stage)
        self.label.setStyleSheet(WRITING_STYLE)
        self.warning.hide()
        for b in (self.replace_button, self.append_button, self.close_button):
            b.hide()
        self.cancel_button.show()
        self.show()

    def offer(self, draft: Draft, seconds: float = 0.0) -> None:
        self.draft = draft
        took = self.tr("（{s:.1f} 秒）").format(s=seconds) if seconds else ""
        self.label.setText(self.tr("案{took}: {text}").format(took=took, text=draft.text))
        self.label.setStyleSheet(OFFER_STYLE)
        warnings = draft.warnings
        self.warning.setText(self.tr("確認: ") + "。".join(warnings) if warnings else "")
        self.warning.setVisible(bool(warnings))
        self.replace_button.show()
        self.append_button.setVisible(self._append)
        self.cancel_button.hide()
        self.close_button.show()
        self.show()

    def say(self, text: str) -> None:
        self.draft = None
        self.label.setText(text)
        self.label.setStyleSheet(PLAIN_STYLE)
        self.warning.hide()
        for b in (self.replace_button, self.append_button, self.cancel_button):
            b.hide()
        self.close_button.show()
        self.show()


class _Asking(QObject):
    """One field's requests: the panel kept up to date while the model writes."""

    def __init__(self, assistant, panel: ProposalPanel, parent: QObject):
        super().__init__(parent)
        self.assistant = assistant
        self.panel = panel
        self.asked: Optional[Asked] = None
        self._timer = QTimer(self)
        self._timer.setInterval(SHOW_MS)
        self._timer.timeout.connect(self._show)
        panel.cancel_button.clicked.connect(lambda _on=False: self.cancel())

    def _show(self) -> None:
        if self.asked is not None:
            self.panel.writing(self.asked.stage, self.asked.text)

    def start(self, title: str, messages, max_tokens: int, done: Callable[[str, float], None], one_line: bool = False) -> bool:
        def finished(text: str, seconds: float) -> None:
            self._stop()
            done(text, seconds)

        def failed(message: str) -> None:
            self._stop()
            if message == "取消":
                self.panel.hide()
            else:
                self.panel.say(self.tr("作成できず: {m}").format(m=message))

        self.asked = self.assistant.ask(title, messages, max_tokens, finished, failed, one_line)
        if self.asked is None:
            return False
        self.panel.writing(self.asked.stage)
        self._timer.start()
        return True

    def _stop(self) -> None:
        self._timer.stop()
        self.asked = None

    def cancel(self) -> None:
        if self.asked is not None:
            self.asked.cancel()

    @property
    def running(self) -> bool:
        return self.asked is not None


class NoteTidy(QObject):
    """「整える」 for one 備考 line edit."""

    def __init__(self, edit: QLineEdit, assistant, glossary_keeper=None, kind: Callable[[], str] = lambda: "",
                 root: Callable[[], str] = lambda: "", parent: Optional[QObject] = None):
        super().__init__(parent or edit)
        self.edit = edit
        self.assistant = assistant
        self.keeper = glossary_keeper
        self.kind = kind
        self.root = root
        self.button = QToolButton()
        self.button.setText(self.tr("整える"))
        self.button.setToolTip(self.tr("備考を判読記録の文体と単語集の語に整えた案を、欄の下に出す（文章の手伝い。この PC の小さな生成 AI。"
                                       "「置き換える」を押すまで備考は変わらない）"))
        self.button.clicked.connect(lambda _on=False: self.tidy())
        self.panel = ProposalPanel()
        self.panel.replace_button.setToolTip(self.tr("備考を案に置き換える（Ctrl+Z で戻せる）"))
        self.panel.replace_button.clicked.connect(lambda _on=False: self.take())
        self._asking = _Asking(assistant, self.panel, self)
        #: The 備考 the proposal is for.
        self.given = ""
        edit.textChanged.connect(self._text_changed)
        assistant.changed.connect(self._settings_changed)
        self._settings_changed()

    def row(self) -> QWidget:
        """The field with its button (for the form's row)."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.edit, 1)
        h.addWidget(self.button)
        return w

    def _settings_changed(self) -> None:
        self.button.setVisible(self.assistant.available)
        if not self.assistant.available:
            self._asking.cancel()
            self.panel.hide()
        self._text_changed()

    def _text_changed(self, _text: str = "") -> None:
        text = self.edit.text()
        self.button.setEnabled(bool(text.strip()))
        draft = self.panel.draft
        if self.panel.isVisible() and text != self.given and not (draft and text == draft.text):
            # Another 備考 now (typed, another record opened): the proposal was for the one before.
            self._asking.cancel()
            self.panel.hide()

    def tidy(self) -> bool:
        """Asks the model for the 備考 tidied (False when it cannot start)."""
        s = self.assistant.settings
        note = self.edit.text()
        if s is None or not note.strip() or self._asking.running:
            return False
        glossary = self.keeper.glossary if self.keeper is not None else None
        root = self.root() or ""
        messages = note_messages(s, note, self.kind() or "", note_terms(glossary, note, root))
        self.given = note

        def done(text: str, seconds: float) -> None:
            draft = note_draft(text, note, glossary, root)
            if not draft.text:
                self.panel.say(self.tr("案を作れず（モデルの答えが空）"))
            elif draft.same:
                self.panel.say(self.tr("直すところなし"))
            else:
                self.panel.offer(draft, seconds)

        return self._asking.start(self.tr("備考を整える"), messages, s.max_tokens, done, one_line=True)

    def take(self) -> None:
        """The proposal in place of the 備考 (as typing does: Ctrl+Z takes it back)."""
        draft = self.panel.draft
        if draft is None:
            return
        self.panel.hide()
        self.edit.selectAll()
        self.edit.insert(draft.text)
        self.given = ""
        self.edit.setFocus()


class HandoverDraft(QObject):
    """「申し送りの下書き」 for the handover memo's 申し送り (the memo's lists from `memo`, made first by `make` when empty)."""

    def __init__(self, memo_edit: QPlainTextEdit, assistant, memo: Callable[[], str], make: Callable[[Callable[[], None]], bool],
                 parent: Optional[QObject] = None):
        super().__init__(parent or memo_edit)
        self.memo_edit = memo_edit
        self.assistant = assistant
        self.memo = memo
        self.make = make
        self.button = QPushButton(self.tr("申し送りの下書き"))
        self.button.setToolTip(self.tr("引き継ぎメモの一覧から、次の担当者への申し送りを数文で下書き（文章の手伝い。この PC の小さな生成 AI。"
                                       "「置き換える」「末尾に足す」を押すまで申し送りは変わらない）"))
        self.button.clicked.connect(lambda _on=False: self.draft())
        self.panel = ProposalPanel(append=True)
        self.panel.replace_button.clicked.connect(lambda _on=False: self.take(False))
        self.panel.append_button.clicked.connect(lambda _on=False: self.take(True))
        self._asking = _Asking(assistant, self.panel, self)
        #: Waiting for the memo's lists to be made (「取消」 then drops the draft).
        self.waiting = False
        self.panel.cancel_button.clicked.connect(lambda _on=False: self._stop_waiting())
        assistant.changed.connect(self._settings_changed)
        self._settings_changed()

    def _settings_changed(self) -> None:
        self.button.setVisible(self.assistant.available)
        if not self.assistant.available:
            self._stop_waiting()
            self._asking.cancel()
            self.panel.hide()

    def _stop_waiting(self) -> None:
        if self.waiting:
            self.waiting = False
            self.panel.hide()

    def _made(self) -> None:
        if self.waiting:
            self.waiting = False
            self.draft()

    def draft(self) -> bool:
        """Asks for the draft (the memo made first when it has not been)."""
        s = self.assistant.settings
        if s is None or self._asking.running or self.waiting:
            return False
        memo = self.memo()
        if not memo.strip():
            self.panel.writing(self.tr("引き継ぎメモを作成中…（作成後に下書き）"))
            self.waiting = True
            if not self.make(self._made):
                self.waiting = False
                self.panel.hide()
                return False
            return True
        written = self.memo_edit.toPlainText()

        def done(text: str, seconds: float) -> None:
            draft = handover_draft(text, memo, written)
            if draft.text:
                self.panel.offer(draft, seconds)
            else:
                self.panel.say(self.tr("案を作れず（モデルの答えが空）"))

        return self._asking.start(self.tr("申し送りの下書き"), handover_messages(s, memo, written), s.handover_max_tokens, done)

    def take(self, append: bool) -> None:
        draft = self.panel.draft
        if draft is None:
            return
        self.panel.hide()
        cursor = self.memo_edit.textCursor()
        if append:
            cursor.movePosition(cursor.MoveOperation.End)
            before = self.memo_edit.toPlainText()
            cursor.insertText(("\n" if before and not before.endswith("\n") else "") + draft.text)
        else:
            cursor.select(cursor.SelectionType.Document)
            cursor.insertText(draft.text)
        self.memo_edit.setTextCursor(cursor)
        self.memo_edit.setFocus()
