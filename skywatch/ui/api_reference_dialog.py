"""
「Python API リファレンス」 (core/api_reference.py): what `plugin.api` has, in
groups; for each, what it does, its arguments as read from the code now,
the members of a dock or tool, and an example to copy into the Python console.
"""
from __future__ import annotations

import inspect
from typing import Any, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFontDatabase
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSplitter, QTextBrowser, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.api_reference import ENTRIES, GROUPS, HOW_TO, Entry, entry


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def signature_of(value: Any, name: str) -> str:
    """`name(arguments)` for a function, `name` for a value."""
    if callable(value) and not isinstance(value, type):
        try:
            sig = inspect.signature(value)
        except (TypeError, ValueError):
            return f"{name}(…)"
        params = [p for p in sig.parameters.values() if p.name != "self"]
        text = []
        for p in params:
            if p.kind == p.VAR_POSITIONAL:
                text.append("*" + p.name)
            elif p.kind == p.VAR_KEYWORD:
                text.append("**" + p.name)
            elif p.default is not p.empty:
                text.append(f"{p.name}={p.default!r}")
            else:
                text.append(p.name)
        return f"{name}({', '.join(text)})"
    return name


def _summary(value: Any) -> str:
    doc = inspect.getdoc(value) if callable(value) else ""
    return (doc or "").strip().splitlines()[0] if doc else ""


class ApiReferenceDialog(QDialog):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle(self.tr("Python API リファレンス"))
        self.resize(900, 620)
        main = QVBoxLayout(self)
        intro = QLabel(self.tr("Python コンソールやプロセシングのスクリプトから使う。例はコピーしてコンソールに貼る。"))
        intro.setWordWrap(True)
        main.addWidget(intro)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(self.tr("名前・説明で絞り込み"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        main.addWidget(self.filter)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(lambda item, _p: self.show_entry(item.data(0, Qt.ItemDataRole.UserRole) if item else None))
        split.addWidget(self.tree)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.page = QTextBrowser()
        self.page.setOpenLinks(False)
        rl.addWidget(self.page, 2)
        rl.addWidget(QLabel(self.tr("例")))
        self.example = QPlainTextEdit()
        self.example.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        rl.addWidget(self.example, 1)
        buttons = QHBoxLayout()
        copy = QPushButton(self.tr("例をコピー（API の取り方つき）"))
        copy.setToolTip(self.tr("API の取り方の 2 行と例をクリップボードへ"))
        copy.clicked.connect(lambda: self.copy_example())
        console = QPushButton(self.tr("Python コンソールを開く"))
        console.clicked.connect(lambda: self.open_console())
        buttons.addWidget(copy)
        buttons.addWidget(console)
        buttons.addStretch(1)
        rl.addLayout(buttons)
        split.addWidget(right)
        split.setSizes([260, 640])
        main.addWidget(split, 1)
        self._fill()
        self.choose("intro")

    def _fill(self) -> None:
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for g in GROUPS:
            item = QTreeWidgetItem([self.tr(g)])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(item)
            groups[g] = item
        for e in ENTRIES:
            label = e.title if e.name.count(":") or e.name == "intro" else f"{e.title}  ({e.name})"
            child = QTreeWidgetItem([label])
            child.setData(0, Qt.ItemDataRole.UserRole, e.name)
            child.setToolTip(0, e.text)
            groups[e.group].addChild(child)
        self.tree.expandAll()

    def _filter(self, text: str) -> None:
        t = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            shown = 0
            for j in range(g.childCount()):
                c = g.child(j)
                e = entry(c.data(0, Qt.ItemDataRole.UserRole))
                hit = not t or any(t in x.lower() for x in (e.name, e.title, e.text, " ".join(e.members)))
                c.setHidden(not hit)
                shown += hit
            g.setHidden(shown == 0)

    def choose(self, name: str) -> bool:
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                if g.child(j).data(0, Qt.ItemDataRole.UserRole) == name:
                    self.tree.setCurrentItem(g.child(j))
                    return True
        return False

    def _target(self, e: Entry) -> Any:
        """What the entry stands for in the API (None for the extra pages, or when it cannot be had)."""
        if ":" in e.name or e.name == "intro":
            return None
        try:
            return getattr(self.api, e.name)
        except Exception:  # noqa: BLE001 (a dock not made yet: the page without its members)
            return None

    def page_html(self, name: str) -> str:
        e = entry(name)
        if e is None:
            return ""
        parts = [f"<h3>{_escape(e.title)}</h3>"]
        if ":" not in e.name and e.name != "intro":
            target = getattr(type(self.api), e.name, None)
            if isinstance(target, property):
                parts.append(f"<p><code>api.{_escape(e.name)}</code></p>")
            else:
                parts.append(f"<p><code>api.{_escape(signature_of(getattr(self.api, e.name, None), e.name))}</code></p>")
        parts.append(f"<p>{_escape(e.text)}</p>")
        if e.members:
            obj = self._target(e)
            rows = []
            for m in e.members:
                value = getattr(obj, m, None) if obj is not None else None
                sig = signature_of(value, m) if value is not None else m
                rows.append(f"<li><code>{_escape(sig)}</code>{' — ' + _escape(_summary(value)) if _summary(value) else ''}</li>")
            parts.append(f"<p><b>{self.tr('主な属性・関数')}</b></p><ul>{''.join(rows)}</ul>")
        return "".join(parts)

    def show_entry(self, name: Optional[str]) -> None:
        e = entry(name) if name else None
        if e is None:
            return
        self.current = e.name
        self.page.setHtml(self.page_html(e.name))
        self.example.setPlainText(e.example)

    def copy_example(self) -> str:
        text = self.example.toPlainText()
        e = entry(getattr(self, "current", ""))
        if e is not None and e.group not in ("式の関数",) and not text.startswith(HOW_TO):
            text = HOW_TO + "\n" + text
        QApplication.clipboard().setText(text)
        return text

    def open_console(self) -> None:
        try:
            from qgis.utils import iface

            action = iface.actionShowPythonDialog() if iface is not None else None
            if action is not None:
                action.trigger()
        except Exception:  # noqa: BLE001 (no console in this QGIS)
            pass
