"""
文章の手伝い on the screens (core/text_assist.py, core/text_tasks.py,
qgis_io/text_assist_io.py). Each shows only when the settings give its task
a model (`textAssist.tasks`), and each only proposes: nothing changes until
the person takes the proposal, and nothing is saved by it.

- NoteTidy (tidyNote): 「整える」 beside a 備考 field (観測記録・確認範囲); the
  備考 written again shows under the field with 「置き換え」 and 「閉じる」, and
  what to check in it (numbers it does not have, much longer). 「置き換え」
  changes the field as typing does (Ctrl+Z takes it back).
- NoteFields (noteFields): 「項目を拾う」 beside the record's 備考: the 種別・活動・
  確度・寸法・モノ read from it, for the fields still empty (and a 種別 made
  finer: 船舶 → 船舶 > 補給艦), 「欄に入力」; a value differing from one
  written is only shown.
- HandoverDraft (handoverDraft): 「申し送りの下書き」 under the 申し送り of the
  handover memo, made from the memo's lists (made first when there are
  none), with 「置き換え」・「末尾に追加」・「閉じる」.
- SearchWords (searchWords): 「言葉で探す」 on the observation export: a sentence
  made into its conditions, 「条件に入力」.
- SummaryDialog (summary): a few sentences on the records chosen (or found),
  to copy.
- NoteTranslator (translate): the 備考 of the 通報 in English (`note.en`).
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from qgis.PyQt.QtCore import QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from ..core.text_assist import (
    Draft, handover_draft, handover_messages, handover_topics, note_draft, note_messages, note_terms, sentence_limit, sentences_done,
)
from ..core.text_tasks import (
    FIELD_KEYS, SEARCH_KEYS, FieldsDraft, SearchDraft, fields_draft, fields_messages, lines_done, search_draft, search_messages,
    summary_draft, summary_messages, summary_source, translate_messages, translated,
)
from ..qgis_io.text_assist_io import SHOW_MS, Asked
from .fit import fit_text

WRITING_STYLE = "QLabel { background: #f1f5f9; color: #334155; padding: 3px; }"
OFFER_STYLE = "QLabel { background: #ecfdf5; color: #065f46; padding: 3px; }"
WARN_STYLE = "QLabel { background: #fef3c7; color: #92400e; padding: 3px; }"
PLAIN_STYLE = "QLabel { color: #57534e; padding: 3px; }"


class ProposalPanel(QWidget):
    """Under a field: the text being written, then the proposal with its buttons (or why there is none)."""

    def __init__(self, append: bool = False, take: str = "", parent: Optional[QWidget] = None):
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
        self.replace_button = QPushButton(take or self.tr("置き換え"))
        self.append_button = QPushButton(self.tr("末尾に追加"))
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

    def say(self, text: str, notes: Sequence[str] = ()) -> None:
        self.draft = None
        self.label.setText(text)
        self.label.setStyleSheet(PLAIN_STYLE)
        self.warning.setText("。".join(notes))
        self.warning.setVisible(bool(notes))
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

    def start(self, task: str, title: str, messages, done: Callable[[str, float], None], one_line: bool = False,
              until: Optional[Callable[[str], bool]] = None) -> bool:
        def finished(text: str, seconds: float) -> None:
            self._stop()
            done(text, seconds)

        def failed(message: str) -> None:
            self._stop()
            if message == "取消":
                self.panel.hide()
            else:
                self.panel.say(self.tr("作成に失敗: {m}").format(m=message))

        self.asked = self.assistant.ask(task, title, messages, finished, failed, one_line, until)
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


def _with_caution(assistant, task: str, draft: Draft) -> Draft:
    """The draft with what to say of a small model for the task (core/text_assist.LARGE_TASKS)."""
    caution = assistant.caution(task)
    return Draft(draft.text, draft.foreign, draft.longer, draft.same, (*draft.notes, caution)) if caution else draft


class _TaskButton(QObject):
    """A button for one task, shown only while the settings give the task a model, with its proposal panel."""

    task = ""

    def __init__(self, assistant, button, panel: ProposalPanel, parent: QObject):
        super().__init__(parent)
        self.assistant = assistant
        self.button = button
        self.panel = panel
        self._asking = _Asking(assistant, panel, self)
        assistant.changed.connect(self._settings_changed)

    @property
    def offered(self) -> bool:
        return self.assistant.has(self.task)

    def _settings_changed(self) -> None:
        self.button.setVisible(self.offered)
        if not self.offered:
            self._asking.cancel()
            self.panel.hide()


class NoteTidy(_TaskButton):
    """「整える」 for one 備考 line edit."""

    task = "tidyNote"

    def __init__(self, edit: QLineEdit, assistant, glossary_keeper=None, kind: Callable[[], str] = lambda: "",
                 root: Callable[[], str] = lambda: "", parent: Optional[QObject] = None):
        super().__init__(assistant, QToolButton(), ProposalPanel(), parent or edit)
        button = self.button
        button.setText(self.tr("整える"))
        button.setToolTip(self.tr("備考を判読記録の文体と単語集の語に整えた案を、欄の下に表示（この PC の小さな生成 AI）。"
                                  "「置き換え」を押すまで備考は変わらない"))
        self.edit = edit
        self.keeper = glossary_keeper
        self.kind = kind
        self.root = root
        button.clicked.connect(lambda _on=False: self.tidy())
        self.panel.replace_button.setToolTip(self.tr("備考を案で置き換え（Ctrl+Z で戻せる）"))
        self.panel.replace_button.clicked.connect(lambda _on=False: self.take())
        #: The 備考 the proposal is for.
        self.given = ""
        edit.textChanged.connect(self._text_changed)
        self._settings_changed()

    def row(self, *more: QWidget) -> QWidget:
        """The field with its button (and `more` buttons), for the form's row."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.edit, 1)
        h.addWidget(self.button)
        for b in more:
            h.addWidget(b)
        return w

    def _settings_changed(self) -> None:
        super()._settings_changed()
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
        t = s.task(self.task) if s is not None else None
        note = self.edit.text()
        if t is None or not note.strip() or self._asking.running:
            return False
        glossary = self.keeper.glossary if self.keeper is not None else None
        root = self.root() or ""
        messages = note_messages(t, note, self.kind() or "", note_terms(glossary, note, root))
        self.given = note

        def done(text: str, seconds: float) -> None:
            draft = note_draft(text, note, glossary, root)
            if not draft.text:
                self.panel.say(self.tr("案の作成に失敗（モデルの答えが空）"))
            elif draft.same:
                self.panel.say(self.tr("直すところなし"))
            else:
                self.panel.offer(draft, seconds)

        return self._asking.start(self.task, self.tr("備考を整える"), messages, done, one_line=True)

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


class NoteFields(_TaskButton):
    """
    「項目を拾う」 for the observation record's 備考: the fields read from it
    (`form`: `fields_choices()`, `fields_values()`, `apply_fields(values)`),
    proposed for those still empty.
    """

    task = "noteFields"
    NAMES = {"kind": "種別", "activity": "活動", "confidence": "確度", "length": "長さ", "width": "幅", "heading": "向き", "object": "モノ"}

    def __init__(self, edit: QLineEdit, assistant, form, parent: Optional[QObject] = None):
        super().__init__(assistant, QToolButton(), ProposalPanel(), parent or edit)
        button = self.button
        button.setText(self.tr("項目を拾う"))
        button.setToolTip(self.tr("備考から種別・活動・確度・長さ・幅・向き・モノを拾い、空いている欄に入れる案を表示。「欄に入力」を押すまで欄は変わらない"))
        self.panel.replace_button.setText(self.tr("欄に入力"))
        self.edit = edit
        self.form = form
        self.values: dict = {}
        self.given = ""
        button.clicked.connect(lambda _on=False: self.pick())
        self.panel.replace_button.setToolTip(self.tr("空いている欄に案の値を入力（記録の保存はまだ）"))
        self.panel.replace_button.clicked.connect(lambda _on=False: self.take())
        edit.textChanged.connect(self._text_changed)
        self._settings_changed()

    def _settings_changed(self) -> None:
        super()._settings_changed()
        self._text_changed()

    def _text_changed(self, _text: str = "") -> None:
        text = self.edit.text()
        self.button.setEnabled(bool(text.strip()))
        if self.panel.isVisible() and text != self.given:
            self._asking.cancel()
            self.panel.hide()

    def pick(self) -> bool:
        s = self.assistant.settings
        t = s.task(self.task) if s is not None else None
        note = self.edit.text()
        if t is None or not note.strip() or self._asking.running:
            return False
        choices = self.form.fields_choices()
        now = self.form.fields_values()
        messages = fields_messages(t, note, now.get("kind", ""), choices["kinds"], choices["activities"], choices["confidences"])
        self.given = note

        def done(text: str, seconds: float) -> None:
            got: FieldsDraft = fields_draft(text, note, choices["resolve_kind"], choices["activities_of"], choices["confidences"],
                                            now.get("kind", ""), choices["kinds"])
            kept, notes = {}, list(got.dropped)
            for k, v in got.values.items():
                if _empty(now.get(k)) or (k == "kind" and _deeper(v, now.get(k, ""))):
                    # An empty field, or the 種別 written so far made finer (船舶 → 船舶 > 補給艦).
                    kept[k] = v
                elif not _same_value(now.get(k), v):
                    shown = FieldsDraft({k: v}).text.split(" ", 1)[-1]
                    notes.append(self.tr("{f}は入力済みのため変えない（案: {v}）").format(f=self.NAMES[k], v=shown))
            self.values = kept
            if not kept:
                self.panel.say(self.tr("空いている欄に入れる値なし"), notes)
                return
            shown = FieldsDraft(kept)
            self.panel.offer(_with_caution(self.assistant, self.task, Draft(shown.text, notes=tuple(notes))), seconds)

        return self._asking.start(self.task, self.tr("項目の提案"), messages, done, until=lines_done(FIELD_KEYS))

    def take(self) -> None:
        if self.panel.draft is None or not self.values:
            return
        self.panel.hide()
        self.form.apply_fields(dict(self.values))
        self.values = {}


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _deeper(path: Sequence[str], kind: str) -> bool:
    """Whether `path` is a class under the 種別 `kind` (its first names the same, and more)."""
    now = [p.strip() for p in kind.split(">") if p.strip()]
    return 0 < len(now) < len(path) and list(path[:len(now)]) == now


def _same_value(now: Any, value: Any) -> bool:
    if isinstance(value, list):
        return [p.strip() for p in str(now).split(">")] == list(value)
    if isinstance(value, float):
        try:
            return abs(float(str(now)) - value) < 0.05
        except ValueError:
            return False
    return str(now).strip() == str(value)


class HandoverDraft(_TaskButton):
    """「申し送りの下書き」 for the handover memo's 申し送り (the memo's lists from `memo`, made first by `make` when empty)."""

    task = "handoverDraft"

    def __init__(self, memo_edit: QPlainTextEdit, assistant, memo: Callable[[], str], make: Callable[[Callable[[], None]], bool],
                 parent: Optional[QObject] = None):
        super().__init__(assistant, QPushButton(), ProposalPanel(append=True), parent or memo_edit)
        button = self.button
        button.setText(self.tr("申し送りの下書き"))
        button.setToolTip(self.tr("引き継ぎメモの一覧から、次の担当者への申し送りを数文で下書き（この PC の小さな生成 AI）。"
                                  "「置き換え」「末尾に追加」を押すまで申し送りは変わらない"))
        self.memo_edit = memo_edit
        self.memo = memo
        self.make = make
        button.clicked.connect(lambda _on=False: self.draft())
        self.panel.replace_button.clicked.connect(lambda _on=False: self.take(False))
        self.panel.append_button.clicked.connect(lambda _on=False: self.take(True))
        #: Waiting for the memo's lists to be made (「取消」 then drops the draft).
        self.waiting = False
        self.panel.cancel_button.clicked.connect(lambda _on=False: self._stop_waiting())
        self._settings_changed()

    def _settings_changed(self) -> None:
        if not self.offered:
            self._stop_waiting()
        super()._settings_changed()

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
        t = s.task(self.task) if s is not None else None
        if t is None or self._asking.running or self.waiting:
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
        if not handover_topics(memo) and not written.strip():
            # Nothing to tell: a model asked anyway writes of what the memo does not have.
            self.panel.say(self.tr("申し送ることがメモにない（期限が近い依頼・担当中の画像・通報がない）"))
            return False

        def done(text: str, seconds: float) -> None:
            draft = handover_draft(text, memo, written)
            if draft.text:
                self.panel.offer(draft, seconds)
            elif draft.notes:
                self.panel.say(self.tr("案の作成に失敗（メモにない内容の文だけ）"), draft.notes)
            else:
                self.panel.say(self.tr("案の作成に失敗（モデルの答えが空）"))

        return self._asking.start(self.task, self.tr("申し送りの下書き"), handover_messages(t, memo, written), done,
                                  until=sentences_done(sentence_limit(t.prompt)))

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


class SearchWords(_TaskButton):
    """
    「言葉で探す」 on the observation export: a sentence (「先週の横須賀の船舶」)
    made into its conditions. `form`: `search_choices()` (places, roots,
    confidences), `prepare(then)` (the named areas read first), `today()`,
    `apply_search(draft)`.
    """

    task = "searchWords"

    def __init__(self, assistant, form, parent: Optional[QObject] = None):
        super().__init__(assistant, QPushButton(), ProposalPanel(), parent)
        button = self.button
        button.setText(self.tr("条件を作成"))
        button.setToolTip(self.tr("書いた文から期間・地名・大分類・確度・有無の条件の案を作成。「条件に入力」を押すまで条件は変わらない"))
        self.panel.replace_button.setText(self.tr("条件に入力"))
        self.form = form
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(self.tr("例: 先週の横須賀の船舶、確度は推定以上"))
        self.edit.setClearButtonEnabled(True)
        self.edit.returnPressed.connect(self.ask)
        self.edit.textChanged.connect(lambda t: self.button.setEnabled(bool(t.strip())))
        button.clicked.connect(lambda _on=False: self.ask())
        button.setEnabled(False)
        self.panel.replace_button.clicked.connect(lambda _on=False: self.take())
        self.row = QWidget()
        h = QHBoxLayout(self.row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.edit, 1)
        h.addWidget(button)
        self.found: Optional[SearchDraft] = None
        self.waiting = False
        self.panel.cancel_button.clicked.connect(lambda _on=False: self._stop_waiting())
        self._settings_changed()

    def _settings_changed(self) -> None:
        super()._settings_changed()
        self.row.setVisible(self.offered)

    def _stop_waiting(self) -> None:
        if self.waiting:
            self.waiting = False
            self.panel.hide()

    def ask(self) -> bool:
        s = self.assistant.settings
        t = s.task(self.task) if s is not None else None
        words = self.edit.text()
        if t is None or not words.strip() or self._asking.running or self.waiting:
            return False
        self.waiting = True
        self.panel.writing(self.tr("地名を取得中…"))

        def ready() -> None:
            if not self.waiting:
                return
            self.waiting = False
            places, roots, confidences = self.form.search_choices()
            kinds = self.form.search_kinds() if hasattr(self.form, "search_kinds") else []
            today = self.form.today()
            messages = search_messages(t, words, today, places, roots, confidences)

            def done(text: str, seconds: float) -> None:
                found = search_draft(text, places, roots, confidences, words, today, kinds)
                self.found = found
                if found.empty:
                    self.panel.say(self.tr("条件を読み取れない"), list(found.dropped))
                else:
                    self.panel.offer(_with_caution(self.assistant, self.task, Draft(found.text, notes=found.dropped)), seconds)

            self._asking.start(self.task, self.tr("言葉で探す"), messages, done, until=lines_done(SEARCH_KEYS))

        self.form.prepare(ready)
        return True

    def take(self) -> None:
        if self.panel.draft is None or self.found is None:
            return
        self.panel.hide()
        self.form.apply_search(self.found)


class SummaryDialog(QDialog):
    """「まとめ」: a few sentences on records, written while shown, to copy (or edit first)."""

    task = "summary"

    def __init__(self, assistant, records: Sequence[Any], when: Callable[[Optional[float]], str], what: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        fit_text(self)
        self.setWindowTitle(self.tr("まとめ（文章の手伝い）"))
        self.assistant = assistant
        self.source = summary_source(records, when)
        self.count = len(records)
        box = QVBoxLayout(self)
        self.head = QLabel(self.tr("{w}{n} 件の記録のまとめ（生成 AI の下書き。記録と照らしてから使う）").format(
            w=f"{what}: " if what else "", n=self.count))
        self.head.setWordWrap(True)
        box.addWidget(self.head)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setMinimumSize(460, 160)
        box.addWidget(self.text_edit, 1)
        self.panel = ProposalPanel(take=self.tr("コピー"))
        self.panel.close_button.hide()
        box.addWidget(self.panel)
        row = QHBoxLayout()
        row.addStretch(1)
        self.copy_button = QPushButton(self.tr("コピー"))
        self.copy_button.clicked.connect(lambda _on=False: self.copy())
        self.again_button = QPushButton(self.tr("作り直す"))
        self.again_button.clicked.connect(lambda _on=False: self.start())
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        for b in (self.copy_button, self.again_button, close):
            b.setAutoDefault(False)
            row.addWidget(b)
        box.addLayout(row)
        self._asking = _Asking(assistant, self.panel, self)
        self.panel.replace_button.hide()
        self.draft: Optional[Draft] = None

    def start(self) -> bool:
        s = self.assistant.settings
        t = s.task(self.task) if s is not None else None
        if t is None or self._asking.running:
            return False
        self.copy_button.setEnabled(False)
        self.again_button.setEnabled(False)

        def done(text: str, seconds: float) -> None:
            self.again_button.setEnabled(True)
            draft = _with_caution(self.assistant, self.task, summary_draft(text, self.source))
            self.draft = draft
            self.text_edit.setPlainText(draft.text)
            self.copy_button.setEnabled(bool(draft.text))
            warnings = draft.warnings
            took = self.tr("{s:.1f} 秒で作成").format(s=seconds)
            self.panel.say(took if draft.text else self.tr("案の作成に失敗（モデルの答えが空）"), [self.tr("確認: ") + "。".join(warnings)] if warnings else [])
            self.panel.close_button.hide()

        started = self._asking.start(self.task, self.tr("まとめ"), summary_messages(t, self.source), done,
                                     until=sentences_done(sentence_limit(t.prompt)))
        if not started:
            self.again_button.setEnabled(True)
        return started

    def copy(self) -> None:
        QApplication.clipboard().setText(self.text_edit.toPlainText())
        self.panel.say(self.tr("クリップボードにコピー"))
        self.panel.close_button.hide()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt)
        self._asking.cancel()
        super().closeEvent(event)


class NoteTranslator(QObject):
    """
    英訳 (translate) of 備考 for the 通報's `note.en`: each 備考 once (kept for
    the session), several in one request, the stage shown by `show(text)`.
    """

    task = "translate"

    def __init__(self, assistant, show: Callable[[str], None], parent: Optional[QObject] = None):
        super().__init__(parent)
        self.assistant = assistant
        self.show = show
        #: 備考 → its English.
        self.known: dict[str, str] = {}
        #: 備考 → what to check in its English (numbers it does not have).
        self.checks: dict[str, str] = {}
        self.asked: Optional[Asked] = None
        self._timer = QTimer(self)
        self._timer.setInterval(SHOW_MS)
        self._timer.timeout.connect(lambda: self.asked is not None and self.show(f"{self.asked.stage} {self.asked.text}".strip()))

    @property
    def offered(self) -> bool:
        return self.assistant.has(self.task)

    @property
    def running(self) -> bool:
        return self.asked is not None

    def missing(self, notes) -> list[str]:
        return list(dict.fromkeys(n.strip() for n in notes if n and n.strip() and n.strip() not in self.known))

    def translate(self, notes, done: Callable[[], None], failed: Callable[[str], None]) -> bool:
        s = self.assistant.settings
        t = s.task(self.task) if s is not None else None
        wanted = self.missing(notes)
        if t is None or self.running:
            return False
        if not wanted:
            done()
            return True

        def finished(texts: list[str], seconds: float) -> None:
            self._timer.stop()
            self.asked = None
            for note, raw in zip(wanted, texts):
                d = translated(raw, note)
                self.known[note] = d.text
                if d.warnings:
                    self.checks[note] = "。".join(d.warnings)
            caution = self.assistant.caution(self.task)
            self.show(self.tr("{n} 件の備考を英訳（{s:.1f} 秒）{c}").format(n=len(wanted), s=seconds, c=f"。{caution}" if caution else ""))
            done()

        def fail(message: str) -> None:
            self._timer.stop()
            self.asked = None
            failed(message)

        self.asked = self.assistant.ask_each(self.task, self.tr("備考の英訳"), [translate_messages(t, n) for n in wanted], finished, fail)
        if self.asked is None:
            return False
        self._timer.start()
        return True

    def cancel(self) -> None:
        if self.asked is not None:
            self.asked.cancel()
