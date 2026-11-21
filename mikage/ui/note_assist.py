"""
備考の単語集 in a field (core/obs/glossary.py): while a 備考 is typed, the
words of the glossary that start with what is being typed show in a list
under the field (the end of the text before the cursor: Japanese runs on
without spaces). Tab takes the first, ↓ then Enter (or a click) the one
chosen; Enter with none chosen goes to the field as before (records). Other
spellings of words found in the text are said under the field, with a button
that writes them as the glossary does. Nothing is stopped: the text stays
free.
"""
from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QEvent, QModelIndex, QObject, QRect, Qt, QTimer
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import QCompleter, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QWidget

from ..core.obs.api import Suggestion, Variant

#: How long after the last change the text is checked for other spellings (ms).
CHECK_MS = 250
_ROW = Qt.ItemDataRole.UserRole + 1


class NoteAssist(QObject):
    """Suggestions and 表記ゆれ for one line edit, from `keeper` (qgis_io/glossary_io.GlossaryKeeper)."""

    def __init__(self, edit: QLineEdit, keeper, root: Callable[[], str] = lambda: "", parent: Optional[QObject] = None):
        super().__init__(parent or edit)
        self.edit = edit
        self.keeper = keeper
        #: The 大分類 of the thing being recorded ("" for none): its words come first, other 大分類's are left out.
        self.root = root
        #: The words offered now (the list's rows).
        self.shown: list[Suggestion] = []
        #: The other spellings found in the text now.
        self.variants: list[Variant] = []
        self._placeholder = edit.placeholderText()
        self._tip = edit.toolTip()
        self._inserting = False

        self.model = QStandardItemModel(self)
        self.completer = QCompleter(self.model, self)
        self.completer.setWidget(edit)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setMaxVisibleItems(10)
        self.completer.activated[QModelIndex].connect(self._clicked)
        # Installed after the completer's own filter, so asked first: Enter and Tab are ours while the list shows.
        self.completer.popup().installEventFilter(self)
        edit.installEventFilter(self)
        edit.textEdited.connect(self._typed)

        # Under the field: the other spellings found (and why the glossary could not be read).
        self.notice = QWidget()
        row = QHBoxLayout(self.notice)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.fix_button = QPushButton(self.tr("単語集の表記に直す"))
        self.fix_button.setAutoDefault(False)
        self.fix_button.setToolTip(self.tr("表記ゆれをすべて単語集の語に書き換える（Ctrl+Z で戻せる）"))
        self.fix_button.clicked.connect(self.fix)
        row.addWidget(self.label, 1)
        row.addWidget(self.fix_button, 0, Qt.AlignmentFlag.AlignTop)
        self.notice.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(CHECK_MS)
        self._timer.timeout.connect(self.check)
        edit.textChanged.connect(lambda _t: self._timer.start())
        keeper.changed.connect(self._glossary_changed)
        self._glossary_changed()

    # ---- the glossary ------------------------------------------------------------------------------------------

    @property
    def glossary(self):
        return self.keeper.glossary

    def _glossary_changed(self) -> None:
        g = self.glossary
        if g:
            self.edit.setPlaceholderText(self.tr("単語集の語を候補に表示（Tab で先頭、↓ で選んで Enter）"))
            tip = self.tr("単語集（{n} 語）から、打っている語の候補を表示。Tab で先頭の候補、↓ で選んで Enter・クリックで入れる。"
                          "表記ゆれは欄の下に表示").format(n=len(g))
            self.edit.setToolTip(f"{self._tip}\n{tip}" if self._tip else tip)
        else:
            self.edit.setPlaceholderText(self._placeholder)
            self.edit.setToolTip(self._tip)
            self.hide_list()
        self.check()

    # ---- the list of words ---------------------------------------------------------------------------------------

    def suggest(self) -> list[Suggestion]:
        """The words for what is being typed at the cursor now."""
        g = self.glossary
        if not g:
            return []
        return g.suggest(self.edit.text()[:self.edit.cursorPosition()], self.root() or "")

    def _typed(self, _text: str = "") -> None:
        if self._inserting:
            return
        self.show_list(self.suggest())

    def show_list(self, found: list[Suggestion]) -> None:
        self.shown = list(found)
        self.model.clear()
        for i, s in enumerate(self.shown):
            item = QStandardItem(s.label)
            item.setData(i, _ROW)
            if s.term.description:
                item.setToolTip(s.term.description)
            item.setEditable(False)
            self.model.appendRow(item)
        popup = self.completer.popup()
        if not self.shown or not self.edit.isVisible():
            self.hide_list()
            return
        width = max(self.edit.width(), popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width() + 12)
        self.completer.complete(QRect(0, 0, width, self.edit.height()))
        # Nothing chosen at first: Enter still goes to the field (records), Tab takes the first word.
        popup.setCurrentIndex(QModelIndex())

    def hide_list(self) -> None:
        popup = self.completer.popup()
        if popup.isVisible():
            popup.hide()

    def choose(self, row: int) -> None:
        """The word of the list's `row` in place of the part being typed."""
        if not 0 <= row < len(self.shown):
            return
        s = self.shown[row]
        self.hide_list()
        cursor = self.edit.cursorPosition()
        # As typing does (Ctrl+Z takes it back), without asking for words again for the word put in.
        self._inserting = True
        try:
            self.edit.setSelection(s.start, max(0, cursor - s.start))
            self.edit.insert(s.term.text)
        finally:
            self._inserting = False
        self.shown = []

    def _clicked(self, index: QModelIndex) -> None:
        row = index.data(_ROW)
        if row is not None:
            self.choose(int(row))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt)
        try:
            return self._filter(obj, event)
        except RuntimeError:
            return False  # The field or the list already deleted (the dock going).

    def _filter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QEvent.Type.FocusIn and self.keeper.due():
            self.keeper.read()
        popup = self.completer.popup()
        if (event.type() == QEvent.Type.ShortcutOverride and obj in (self.edit, popup) and popup.isVisible()
                and event.key() == Qt.Key.Key_Escape):
            # Esc closes the list only (not the dock's 取消, which would close the form with what was typed).
            event.accept()
            return True
        if obj is popup and event.type() == QEvent.Type.KeyPress and self.shown:
            key = event.key()
            current = popup.currentIndex()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and current.isValid():
                self.choose(int(current.data(_ROW)))
                return True
            if key == Qt.Key.Key_Tab and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
                self.choose(int(current.data(_ROW)) if current.isValid() else 0)
                return True
        return False

    # ---- other spellings ---------------------------------------------------------------------------------------

    def check(self) -> None:
        """Says the other spellings in the text (and why the glossary could not be read), under the field."""
        g = self.glossary
        self.variants = g.variants_in(self.edit.text(), self.root() or "") if g else []
        if self.variants:
            pairs = list(dict.fromkeys(f"{v.found} → {v.term}" for v in self.variants))
            self.label.setText(self.tr("表記ゆれ: {pairs}（単語集の表記）").format(pairs="、".join(pairs)))
            self.label.setStyleSheet("QLabel { background: #fef3c7; color: #92400e; padding: 3px; }")
            self.fix_button.show()
            self.notice.show()
        elif self.keeper.problem and self.keeper.configured:
            self.label.setText(self.keeper.problem)
            self.label.setStyleSheet("QLabel { color: #57534e; padding: 3px; }")
            self.fix_button.hide()
            self.notice.show()
        else:
            self.notice.hide()

    def fix(self) -> None:
        """Every other spelling written as its word (as typing does: Ctrl+Z takes it back)."""
        g = self.glossary
        if not g:
            return
        text = self.edit.text()
        fixed = g.fixed(text, self.root() or "")
        if fixed == text:
            return
        self._inserting = True
        try:
            self.edit.selectAll()
            self.edit.insert(fixed)
        finally:
            self._inserting = False
        self.check()
