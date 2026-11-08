"""
一覧の「ビュー」 (tsudo, 2026-11-08): how a list is looked at, kept under a
name and switched to in one click: which columns show and in what order,
the sort (lists that sort by a header click) and the list's filters.

- Columns are dragged by their header to reorder, and shown or hidden from
  the header's right-click menu or 「ビュー ▸ 列の表示」; the arrangement is
  kept as it is (per list, also without a name).
- 「ビュー ▸ 今の表示を保存…」 keeps it under a name, with the filters; a
  saved view is chosen from the same menu. 「標準の表示に戻す」 shows every
  column in the plugin's order.
- Kept per user (QgsSettings `skywatch/listViews/<list>`), not in the settings
  file or the project: each person has their own.

Columns are told by their titles, so a view still fits when a list gains or
loses a column (a column it does not name is shown where it falls).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional, Union

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGroupBox, QHeaderView, QInputDialog, QLineEdit, QMenu, QSpinBox, QToolButton, QWidget,
)

try:
    from qgis.PyQt.QtWidgets import QDoubleSpinBox
except ImportError:  # pragma: no cover
    QDoubleSpinBox = QSpinBox

ROOT = "skywatch/listViews"

#: A filter: (read its value, set a value).
Filter = tuple[Callable[[], Any], Callable[[Any], None]]


def widget_filter(widget: QWidget) -> Filter:
    """Reading and setting a filter widget (line edit, combo box by its data else text, check box, checkable group, spin box)."""
    if isinstance(widget, QLineEdit):
        return widget.text, lambda v: widget.setText("" if v is None else str(v))
    if isinstance(widget, QComboBox):
        def get_combo():
            data = widget.currentData()
            return {"data": data} if isinstance(data, (str, int, float, bool)) and data != "" else {"text": widget.currentText()}

        def set_combo(v):
            if isinstance(v, dict) and "data" in v:
                i = widget.findData(v["data"])
            elif isinstance(v, dict):
                i = widget.findText(str(v.get("text", "")))
                if i < 0 and widget.isEditable():
                    widget.setEditText(str(v.get("text", "")))
                    return
            else:
                i = -1
            if i >= 0:
                widget.setCurrentIndex(i)
        return get_combo, set_combo
    if isinstance(widget, (QCheckBox, QGroupBox)):
        return widget.isChecked, lambda v: widget.setChecked(bool(v))
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        return widget.value, lambda v: widget.setValue(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    raise TypeError(f"ビューで扱えない部品: {type(widget).__name__}")


class ListViews(QObject):
    """The views of one list (a table view or widget): its 「ビュー」 button, the header's menu, the arrangement kept."""

    def __init__(self, key: Union[str, Callable[[], str]], view: QAbstractItemView, filters: Optional[dict[str, Union[QWidget, Filter]]] = None,
                 applied: Optional[Callable[[], None]] = None, parent: Optional[QObject] = None):
        super().__init__(parent or view)
        self._key = key
        self.view = view
        self.filters: dict[str, Filter] = {k: (v if isinstance(v, tuple) else widget_filter(v)) for k, v in (filters or {}).items()}
        self.applied = applied
        #: The view chosen last (its name), per list key.
        self.current: dict[str, str] = {}
        self._applying = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.reapply)
        self._timer_arranged = QTimer(self)
        self._timer_arranged.setSingleShot(True)
        self._timer_arranged.setInterval(0)
        self._timer_arranged.timeout.connect(self._arranged)
        header = self.header
        header.setSectionsMovable(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(lambda pos: self.columns_menu().exec(header.mapToGlobal(pos)))
        header.sectionMoved.connect(self._moved)
        header.sortIndicatorChanged.connect(self._sorted)
        model = view.model()
        if model is not None:
            # Columns made again (a new search, another tab): the arrangement put back once they are.
            for signal in (model.modelReset, model.headerDataChanged, model.columnsInserted):
                signal.connect(self._later)
        self.button = QToolButton()
        self.button.setText(self.tr("ビュー"))
        self.button.setToolTip(self.tr("一覧の見え方（列の表示・並び、並べ替え、絞り込み）を名前を付けて保存・切り替え。列は見出しのドラッグで並べ替え、"
                                       "右クリックで表示・非表示"))
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.button)
        menu.aboutToShow.connect(lambda: self._fill(menu))
        self.button.setMenu(menu)

    # ---- where it is kept --------------------------------------------------------------------------

    @property
    def header(self) -> QHeaderView:
        return self.view.horizontalHeader()

    @property
    def key(self) -> str:
        return self._key() if callable(self._key) else self._key

    def _read(self) -> dict:
        value = QgsSettings().value(f"{ROOT}/{self.key}", "")
        try:
            data = json.loads(value) if isinstance(value, str) and value else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("views"), dict):
            data["views"] = {}
        return data

    def _write(self, data: dict) -> None:
        QgsSettings().setValue(f"{ROOT}/{self.key}", json.dumps(data, ensure_ascii=False))

    def names(self) -> list[str]:
        return list(self._read()["views"])

    # ---- the state of the list ---------------------------------------------------------------------

    def _titles(self) -> list[str]:
        model = self.view.model()
        if model is None:
            return []
        return [str(model.headerData(i, Qt.Orientation.Horizontal) or "") for i in range(model.columnCount())]

    def columns(self) -> list[dict]:
        """The columns in the order shown: their titles and whether hidden."""
        titles = self._titles()
        h = self.header
        order = sorted(range(len(titles)), key=h.visualIndex)
        return [{"title": titles[i], "hidden": h.isSectionHidden(i)} for i in order]

    def state(self, filters: bool = True) -> dict:
        out: dict[str, Any] = {"columns": self.columns()}
        if self.view.isSortingEnabled() and self.header.isSortIndicatorShown():
            i = self.header.sortIndicatorSection()
            titles = self._titles()
            if 0 <= i < len(titles):
                out["sort"] = [titles[i], "desc" if self.header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder else "asc"]
        if filters and self.filters:
            out["filters"] = {k: get() for k, (get, _set) in self.filters.items()}
        return out

    def apply_columns(self, columns: list) -> None:
        """Shows the columns named in their order (hidden as said); the others after them in the plugin's order."""
        titles = self._titles()
        h = self.header
        self._applying = True
        try:
            for i in range(len(titles)):
                h.moveSection(h.visualIndex(i), i)
                h.setSectionHidden(i, False)
            target = 0
            for c in columns or []:
                if not isinstance(c, dict) or c.get("title") not in titles:
                    continue
                i = titles.index(c["title"])
                h.moveSection(h.visualIndex(i), target)
                h.setSectionHidden(i, bool(c.get("hidden")))
                target += 1
            # Never every column hidden.
            if titles and all(h.isSectionHidden(i) for i in range(len(titles))):
                h.setSectionHidden(0, False)
        finally:
            self._applying = False

    def apply(self, state: dict) -> None:
        """Puts the list as `state` says: its columns, the sort, the filters (then the list is told)."""
        if not isinstance(state, dict):
            return
        self.apply_columns(state.get("columns") or [])
        sort = state.get("sort")
        titles = self._titles()
        if self.view.isSortingEnabled() and isinstance(sort, list) and len(sort) == 2 and sort[0] in titles:
            order = Qt.SortOrder.DescendingOrder if sort[1] == "desc" else Qt.SortOrder.AscendingOrder
            self.view.sortByColumn(titles.index(sort[0]), order)
        filters = state.get("filters")
        if isinstance(filters, dict):
            for k, (_get, set_) in self.filters.items():
                if k in filters:
                    set_(filters[k])
        self._arranged()
        if filters is not None and self.applied is not None:
            self.applied()

    def _moved(self, *_args) -> None:
        self._arranged()

    def _sorted(self, *_args) -> None:
        # After the model sorted itself (the header tells first).
        self._timer_arranged.start()

    def _later(self, *_args) -> None:
        if not self._applying:
            self._timer.start()

    def reapply(self) -> None:
        """The arrangement kept for the list put back (its columns were made again; none kept: the plugin's order)."""
        kept = self._read().get("arrangement")
        self.apply_columns(kept.get("columns") or [] if isinstance(kept, dict) else [])

    def _arranged(self) -> None:
        # Not while the columns are being made again (the arrangement is put back first).
        if self._applying or self._timer.isActive() or not self._titles():
            return
        data = self._read()
        data["arrangement"] = self.state(filters=False)
        self._write(data)

    # ---- named views -------------------------------------------------------------------------------

    def save(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("名前を入れる")
        data = self._read()
        data["views"][name] = self.state()
        self._write(data)
        self.current[self.key] = name
        return name

    def choose(self, name: str) -> bool:
        state = self._read()["views"].get(name)
        if state is None:
            return False
        self.current[self.key] = name
        self.apply(state)
        return True

    def delete(self, name: str) -> bool:
        data = self._read()
        if data["views"].pop(name, None) is None:
            return False
        self._write(data)
        if self.current.get(self.key) == name:
            self.current.pop(self.key, None)
        return True

    def reset(self) -> None:
        """Every column shown in the plugin's order (the sort and the filters left as they are)."""
        h = self.header
        self._applying = True
        try:
            for i in range(len(self._titles())):
                h.moveSection(h.visualIndex(i), i)
                h.setSectionHidden(i, False)
        finally:
            self._applying = False
        self.current.pop(self.key, None)
        self._arranged()

    def set_hidden(self, title: str, hidden: bool) -> bool:
        titles = self._titles()
        if title not in titles:
            return False
        i = titles.index(title)
        if hidden and sum(1 for j in range(len(titles)) if not self.header.isSectionHidden(j)) <= 1:
            return False
        self.header.setSectionHidden(i, hidden)
        self._arranged()
        return True

    # ---- menus -------------------------------------------------------------------------------------

    def columns_menu(self, parent: Optional[QWidget] = None) -> QMenu:
        menu = QMenu(self.tr("列の表示"), parent or self.view)
        for c in self.columns():
            a = menu.addAction(c["title"] or self.tr("（名前なし）"))
            a.setCheckable(True)
            a.setChecked(not c["hidden"])
            a.toggled.connect(lambda on, t=c["title"]: self.set_hidden(t, not on))
        menu.addSeparator()
        menu.addAction(self.tr("標準の表示に戻す"), self.reset)
        return menu

    def ask_save(self) -> Optional[str]:
        name, ok = QInputDialog.getText(self.view, self.tr("ビューの保存"), self.tr("ビューの名前（列・並べ替え・絞り込みを保存。自分の PC だけ）"),
                                        QLineEdit.EchoMode.Normal, self.current.get(self.key, ""))
        if not ok or not name.strip():
            return None
        return self.save(name)

    def _fill(self, menu: QMenu) -> None:
        menu.clear()
        names = self.names()
        chosen = self.current.get(self.key)
        for n in names:
            a = menu.addAction(n)
            a.setCheckable(True)
            a.setChecked(n == chosen)
            a.triggered.connect(lambda _on=False, n=n: self.choose(n))
        if not names:
            a = menu.addAction(self.tr("（保存したビューなし）"))
            a.setEnabled(False)
        menu.addSeparator()
        menu.addAction(self.tr("今の表示を名前を付けて保存…"), self.ask_save)
        if chosen in names:
            menu.addAction(self.tr("「{n}」に上書き保存").format(n=chosen), lambda: self.save(chosen))
        if names:
            remove = menu.addMenu(self.tr("ビューの削除"))
            for n in names:
                remove.addAction(n, lambda n=n: self.delete(n))
        menu.addSeparator()
        menu.addMenu(self.columns_menu(menu))
        menu.addAction(self.tr("標準の表示に戻す"), self.reset)
