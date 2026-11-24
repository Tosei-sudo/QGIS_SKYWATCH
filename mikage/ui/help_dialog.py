"""
「使い方（ヘルプ）」: the help for everyday users (help/index.html, in Japanese), shown in QGIS.

The page is one HTML file in the plugin folder, with nothing from outside (it
works the same in a closed network), so it also opens as it is in a browser
(「ブラウザで開く」). Here a contents tree from its h2/h3 headings, a search
and links:

* `#id`: that section of the page;
* `mikage:open/<menu text>`: runs that entry of the plugin's menu (the dock
  or dialog it opens), through the `run` given by the plugin;
* anything else (http, file): the system's browser.

The search (core/help_search.py: words split at spaces, every one in the
section; width, case and kana folded) marks every hit yellow and the one shown
orange, in the middle of the page. 「▲ 前」「▼ 次」, Enter / Shift+Enter in the
box and F3 / Shift+F3 anywhere in the window go from hit to hit, from where the
reader clicked if they did, round at the ends; Ctrl+F goes back to the box. The
contents keep the sections with a hit, with their number of hits, the section of
the hit shown selected; clicking one goes to its first hit.

An エラーコード typed in the search (「E101」, 「e-101」, 「Ｅ１０１」) goes to its
row of 「エラーコード一覧」 (marked whole), with the code's text beside the box;
`go("E101")` does the same (the message bar's 「対処法」, a status line's click).

Admin matters (the settings file, the Esri tables) stay in the README.
"""
from __future__ import annotations

import html
import os
import re
from typing import Callable, Optional

from qgis.PyQt.QtCore import QEvent, QObject, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QKeySequence, QTextCharFormat, QTextCursor
from qgis.PyQt.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter, QTextBrowser, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

try:
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt 5
    from qgis.PyQt.QtWidgets import QShortcut

from ..core import errors
from ..core.help_search import Found, Index
from .fit import fit_text

PAGE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "help", "index.html")
#: Links that run an entry of the plugin's menu.
OPEN = "mikage:open/"

#: Every hit, and the one shown.
HIT = "#fde68a"
CURRENT = "#fb923c"
#: The row of an エラーコード searched for.
ROW = "#fef3c7"

_HEADING = re.compile(r'<h([23])\s+id="([^"]+)"[^>]*>(.*?)</h\1>', re.S)
_TAG = re.compile(r"<[^>]+>")


def read_page(path: str = PAGE) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _text(fragment: str) -> str:
    return html.unescape(_TAG.sub("", fragment)).strip()


def headings(page: str) -> list[tuple[int, str, str]]:
    """(level 2 or 3, id, title) of the page's sections in order."""
    return [(int(level), anchor, _text(title)) for level, anchor, title in _HEADING.findall(page)]


def sections(page: str) -> dict[str, str]:
    """The plain text of each section (from its heading to the next h2/h3), by id."""
    body = page.split("<body>", 1)[-1]
    found = list(_HEADING.finditer(body))
    out = {}
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(body)
        out[m.group(2)] = _text(re.sub(r"<!--.*?-->", "", body[m.start():end], flags=re.S))
    return out


def links(page: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', page)


class HelpDialog(QDialog):
    def __init__(self, run: Optional[Callable[[str], bool]] = None, parent=None, path: str = PAGE):
        super().__init__(parent)
        fit_text(self)
        self.run = run
        self.path = path
        self.setWindowTitle(self.tr("MIKAGE の使い方"))
        # Wide enough for the pictures of 「画面つきの手順書」 (at most 760 pixels) beside the contents.
        self.resize(1120, 760)
        page = read_page(path)
        main = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("ヘルプを検索（空白で区切ると複数の語。エラーコードも可）"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.find)
        # Enter and Shift+Enter here (never the dialog's: it pressed 「ブラウザで開く」 too).
        self.search.installEventFilter(self)
        top.addWidget(self.search, 1)
        self.hits_label = QLabel()
        top.addWidget(self.hits_label)
        self.previous_button = self._button(self.tr("▲ 前"), self.tr("前の一致へ（Shift+Enter・Shift+F3）"), self.find_previous)
        self.next_button = self._button(self.tr("▼ 次"), self.tr("次の一致へ（Enter・F3）"), self.find_next)
        top.addWidget(self.previous_button)
        top.addWidget(self.next_button)
        browser = self._button(self.tr("ブラウザで開く"), self.tr("同じページを PC のブラウザで開く（印刷・拡大に）"),
                               lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.path)))
        top.addWidget(browser)
        main.addLayout(top)
        # Anywhere in the window: the page and the contents too.
        for key, slot in (("F3", self.find_next), ("Shift+F3", self.find_previous), (QKeySequence.StandardKey.Find, self.to_search)):
            QShortcut(QKeySequence(key), self, slot)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        # The title, and while searching its number of hits (a column of its own: never cut off with a long title).
        self.tree.setColumnCount(2)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        #: The contents' item of each section, by id.
        self.items: dict[str, QTreeWidgetItem] = {}
        parent_item = None
        for level, anchor, title in headings(page):
            item = QTreeWidgetItem([title, ""])
            item.setData(0, Qt.ItemDataRole.UserRole, anchor)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.items[anchor] = item
            if level == 2 or parent_item is None:
                self.tree.addTopLevelItem(item)
                parent_item = item
            else:
                parent_item.addChild(item)
        self.tree.itemClicked.connect(lambda item, _c: self._section_clicked(item))
        split.addWidget(self.tree)
        self.page = QTextBrowser()
        self.page.setOpenLinks(False)
        self.page.anchorClicked.connect(self.follow)
        self.page.document().setBaseUrl(QUrl.fromLocalFile(path))
        self.page.setHtml(page)
        split.addWidget(self.page)
        split.setStretchFactor(1, 3)
        split.setSizes([250, 870])
        main.addWidget(split, 1)
        starts = self._heading_positions()
        self.index = Index(self.page.document().toPlainText(), starts)
        #: (start, end) of each section with its subsections (an h2 to the next h2), by id.
        self.ranges = self._ranges(page, starts)
        self.found = Found([])
        self.hits: list[QTextCursor] = []
        self.current = -1
        #: Where the page's cursor was left at the hit shown (another place: the reader clicked there).
        self._left_at = -1
        #: The エラーコード searched for (its row shown, its text beside the box), or "".
        self.code = ""
        #: Its own cell's hit, and the cells of its row (marked whole).
        self._row_index: Optional[int] = None
        self._row: list[QTextCursor] = []
        self._enable()

    def _button(self, text: str, tip: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        # Not a default button: Enter in the box is the search's.
        button.setAutoDefault(False)
        button.clicked.connect(slot)
        return button

    def _heading_positions(self) -> list[tuple[int, str]]:
        """(position in the document, id) of each h2/h3 heading, in order."""
        ids = set(self.items)
        out = []
        block = self.page.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel() in (2, 3):
                names = []
                fragments = block.begin()
                while not fragments.atEnd():
                    names += fragments.fragment().charFormat().anchorNames()
                    fragments += 1
                anchor = next((n for n in names if n in ids), None)
                if anchor is not None:
                    out.append((block.position(), anchor))
            block = block.next()
        return out

    def _ranges(self, page: str, starts: list[tuple[int, str]]) -> dict[str, tuple[int, int]]:
        level = {anchor: lv for lv, anchor, _t in headings(page)}
        end_of_page = self.page.document().characterCount()
        out = {}
        for i, (position, anchor) in enumerate(starts):
            later = [p for p, a in starts[i + 1:] if level[a] <= level[anchor]]
            out[anchor] = (position, later[0] if later else end_of_page)
        return out

    # Links and sections.

    def go(self, anchor: str) -> None:
        """Shows the section with that id, or the row of an エラーコード (「E101」)."""
        code = errors.normalized(anchor)
        if code is not None:
            if self.search.text() == code:
                self.find(code)
            else:
                self.search.setText(code)  # (found by `find`, as typed)
            return
        self.page.scrollToAnchor(anchor)

    def follow(self, url: QUrl) -> bool:
        text = url.toString()
        if text.startswith(OPEN):
            entry = QUrl.fromPercentEncoding(text[len(OPEN):].encode())
            return bool(self.run and self.run(entry))
        if url.hasFragment() and (not url.scheme() or url.isLocalFile() and url.toLocalFile() == self.path):
            self.go(url.fragment())
            return True
        QDesktopServices.openUrl(url)
        return True

    def _section_clicked(self, item: QTreeWidgetItem) -> None:
        """While searching, the section's first hit (the next ones from there); else the section."""
        anchor = item.data(0, Qt.ItemDataRole.UserRole)
        start, end = self.ranges.get(anchor, (0, 0))
        first = self.index.first_in(self.found, start, end) if self.hits else None
        if first is None:
            self.go(anchor)
        else:
            self._show(first)

    # Search.

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.search and event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.find_previous()
            else:
                self.find_next()
            return True
        return super().eventFilter(watched, event)

    def to_search(self) -> None:
        """Ctrl+F: back to the box, its words selected (to type others over them)."""
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def find(self, text: str) -> int:
        """
        Marks every hit of the words, shows the first, keeps in the contents
        the sections with one (with their number of hits); the number of hits.
        An エラーコード (however typed) is searched as the code, its row in
        「エラーコード一覧」 shown first and marked whole.
        """
        text = text.strip()
        self.code = errors.normalized(text) or ""
        self.found = self.index.search(self.code or text)
        doc = self.page.document()
        self.hits = []
        for start, end in self.found.hits:
            cursor = QTextCursor(doc)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            self.hits.append(cursor)
        self.current, self._left_at = -1, -1
        row = self._code_row() if self.code else None
        self._row_index = row
        self._row = self._row_cells(self.hits[row]) if row is not None else []
        self._contents()
        self._enable()
        if self.hits:
            self._show(row or 0)
        else:
            self._mark()
            self.hits_label.setText(self._code_text() if self.code else self._none() if text else "")
        return len(self.hits)

    def _none(self) -> str:
        if len(self.found.words) > 1:
            return self.tr("0 件（すべての語がある節なし）")
        return self.tr("0 件")

    def find_next(self) -> None:
        self._step(1)

    def find_previous(self) -> None:
        self._step(-1)

    def _step(self, step: int) -> None:
        if not self.hits:
            return
        n = len(self.hits)
        at = self.page.textCursor().position()
        if self.current < 0 or at != self._left_at:
            # From where the reader clicked: the first hit after it (going on), the last before it (going back).
            i = self.index.first_in(self.found, at)
            i = (n if i is None else i) - (step < 0)
        else:
            i = self.current + step
        note = ""
        if i >= n:
            i, note = 0, self.tr("最初に戻りました")
        elif i < 0:
            i, note = n - 1, self.tr("最後に戻りました")
        self._show(i, note)

    def _show(self, i: int, note: str = "") -> None:
        """Shows hit i: orange, in the middle of the page (a code's row near the top), its section selected in the contents."""
        self.current = i
        start = self.found.hits[i][0]
        cursor = QTextCursor(self.page.document())
        cursor.setPosition(start)
        self.page.setTextCursor(cursor)
        self._left_at = start
        self._mark()
        bar = self.page.verticalScrollBar()
        rect = self.page.cursorRect(cursor)
        if self.code and i == self._row_index:
            # The code's row near the top, with its kind's heading above it in sight.
            bar.setValue(bar.value() + rect.top() - 80)
        else:
            bar.setValue(bar.value() + rect.center().y() - self.page.viewport().height() // 2)
        item = self.items.get(self.index.section_at(start))
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        else:
            self.tree.clearSelection()
        count = self.tr("{i} / {n} 件").format(i=i + 1, n=len(self.hits))
        if note:
            count += f"（{note}）"
        self.hits_label.setText(f"{self._code_text()}（{count}）" if self.code else count)

    def _mark(self) -> None:
        """Every hit yellow, the one shown orange (a code's row lighter, whole)."""
        selections = []
        for cells, colour in ((self._row, ROW), (self.hits, HIT)):
            for k, cursor in enumerate(cells):
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format = QTextCharFormat()
                sel.format.setBackground(QColor(CURRENT if cells is self.hits and k == self.current else colour))
                selections.append(sel)
        self.page.setExtraSelections(selections)

    def _contents(self) -> None:
        """The sections with a hit, each with its number (a part: those of its subsections too); all when not searching."""
        searching = bool(self.found.words)
        counts = self.found.counts
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            shown_children = 0
            total = counts.get(top.data(0, Qt.ItemDataRole.UserRole), 0)
            for j in range(top.childCount()):
                child = top.child(j)
                n = counts.get(child.data(0, Qt.ItemDataRole.UserRole), 0)
                child.setHidden(searching and not n)
                child.setText(1, str(n) if searching else "")
                shown_children += n > 0
                total += n
            top.setHidden(searching and not total)
            top.setText(1, str(total) if searching else "")
            top.setExpanded(searching and shown_children > 0)

    def _enable(self) -> None:
        for button in (self.previous_button, self.next_button):
            button.setEnabled(bool(self.hits))
    def _code_text(self) -> str:
        """Beside the box for a code: its text, or that the list has no such code."""
        known = errors.CODES.get(self.code)
        if known is not None:
            return f"{self.code} {known.text}"
        if self.code in errors.RETIRED:
            return self.tr("{c} は使われなくなったコード（{t}）").format(c=self.code, t=errors.RETIRED[self.code])
        return self.tr("{c} は一覧にない").format(c=self.code)

    def _code_row(self) -> Optional[int]:
        """Which hit is the code's own cell in 「エラーコード一覧」 (the cell holds the code alone), if any."""
        for i, cursor in enumerate(self.hits):
            if cursor.block().text().strip() == self.code and cursor.currentTable() is not None:
                return i
        return None

    @staticmethod
    def _row_cells(cursor: QTextCursor) -> list[QTextCursor]:
        """The cells of the table row the cursor is in, each selected."""
        table = cursor.currentTable()
        if table is None:
            return []
        row = table.cellAt(cursor).row()
        cells = []
        for column in range(table.columns()):
            cell = table.cellAt(row, column)
            selected = cell.firstCursorPosition()
            selected.setPosition(cell.lastCursorPosition().position(), QTextCursor.MoveMode.KeepAnchor)
            cells.append(selected)
        return cells

    def plain_text(self) -> str:
        return self.page.document().toPlainText()

