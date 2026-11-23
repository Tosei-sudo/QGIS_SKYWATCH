"""
「使い方（ヘルプ）」: the help for everyday users (help/index.html, in Japanese), shown in QGIS.

The page is one HTML file in the plugin folder, with nothing from outside (it
works the same in a closed network), so it also opens as it is in a browser
(「ブラウザで開く」). Here a contents tree from its h2/h3 headings, a search
that marks every hit (Enter for the next) and hides the headings without one,
and links:

* `#id`: that section of the page;
* `mikage:open/<menu text>`: runs that entry of the plugin's menu (the dock
  or dialog it opens), through the `run` given by the plugin;
* anything else (http, file): the system's browser.

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

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QTextCharFormat, QTextCursor
from qgis.PyQt.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QTextBrowser, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core import errors
from .fit import fit_text

PAGE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "help", "index.html")
#: Links that run an entry of the plugin's menu.
OPEN = "mikage:open/"

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
        self.texts = sections(page)
        main = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("ヘルプを検索（エラーコードも可。Enter で次）"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.find)
        self.search.returnPressed.connect(self.find_next)
        top.addWidget(self.search, 1)
        self.hits_label = QLabel()
        top.addWidget(self.hits_label)
        browser = QPushButton(self.tr("ブラウザで開く"))
        browser.setToolTip(self.tr("同じページを PC のブラウザで開く（印刷・拡大に）"))
        browser.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.path)))
        top.addWidget(browser)
        main.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        parent_item = None
        for level, anchor, title in headings(page):
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, anchor)
            if level == 2 or parent_item is None:
                self.tree.addTopLevelItem(item)
                parent_item = item
            else:
                parent_item.addChild(item)
        self.tree.itemClicked.connect(lambda item, _c: self.go(item.data(0, Qt.ItemDataRole.UserRole)))
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
        self.hits: list[QTextCursor] = []
        self.current = -1
        #: The エラーコード searched for (its row shown, its text beside the box), or "".
        self.code = ""

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

    # Search.

    def find(self, text: str) -> int:
        """
        Marks every hit of the text, shows the first, hides the headings
        without one; the number of hits. An エラーコード (however typed) is
        searched as the code, its row in 「エラーコード一覧」 first and marked whole.
        """
        text = text.strip()
        self.code = errors.normalized(text) or ""
        if self.code:
            text = self.code
        self.hits, self.current = [], -1
        doc = self.page.document()
        if text:
            cursor = doc.find(text, 0)
            while not cursor.isNull():
                self.hits.append(cursor)
                cursor = doc.find(text, cursor)
        marked = QTextCharFormat()
        marked.setBackground(QColor("#fde68a"))
        selections = []
        row = self._code_row() if self.code else None
        if row is not None:
            # The row first (Enter goes on to the other mentions), each of its cells marked.
            self.hits.insert(0, self.hits.pop(row))
            whole = QTextCharFormat()
            whole.setBackground(QColor("#fef3c7"))
            for cell in self._row_cells(self.hits[0]):
                sel = QTextEdit.ExtraSelection()
                sel.cursor, sel.format = cell, whole
                selections.append(sel)
        for cursor in self.hits:
            sel = QTextEdit.ExtraSelection()
            sel.cursor, sel.format = cursor, marked
            selections.append(sel)
        self.page.setExtraSelections(selections)
        needle = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            shown_children = 0
            for j in range(top.childCount()):
                child = top.child(j)
                hit = not needle or needle in self.texts.get(child.data(0, Qt.ItemDataRole.UserRole), "").lower()
                child.setHidden(not hit)
                shown_children += hit
            own = not needle or needle in self.texts.get(top.data(0, Qt.ItemDataRole.UserRole), "").lower()
            top.setHidden(not (own or shown_children))
            top.setExpanded(bool(needle) and shown_children > 0)
        self.hits_label.setText(self._code_text() if self.code else self.tr("{n} 件").format(n=len(self.hits)) if text else "")
        if self.hits:
            self.find_next()
        return len(self.hits)

    def find_next(self) -> None:
        if not self.hits:
            return
        self.current = (self.current + 1) % len(self.hits)
        cursor = QTextCursor(self.hits[self.current])
        self.page.setTextCursor(cursor)
        self.page.ensureCursorVisible()
        if self.code and self.current == 0:
            # The code's row near the top, with its kind's heading above it in sight.
            bar = self.page.verticalScrollBar()
            bar.setValue(bar.value() + self.page.cursorRect(cursor).top() - 80)
        count = self.tr("{i} / {n} 件").format(i=self.current + 1, n=len(self.hits))
        self.hits_label.setText(f"{self._code_text()}（{count}）" if self.code else count)

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

