"""
The plugin's pages beside the map (tsudo, 2026-11-14: 「メインのドックに、切り替える感じで」):
a large page in the place of the map, switched with it by tabs over it
(「地図｜新着画像（3）×」), as views are in ArcGIS Pro.

QGIS's map area is a stack of pages (checked in QGIS 3.34: the map canvas,
the welcome page shown with no project open, …; the canvas's parent). The
plugin's pages are added to that stack; the tab strip is put over it only
while one of them is open (QGIS's own widgets are moved down one row and put
back when the last closes or the plugin unloads). QGIS goes back to the map
by itself when a layer is added (an image opened) or a project opened, and
the tabs follow whatever page is in front.

Which pages were open is kept per user (`settings.central_views`): they are
open again (the map in front) when QGIS starts next.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QBoxLayout, QGridLayout, QStackedWidget, QTabBar, QWidget

from ..qgis_io import settings


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


#: The map's tab.
MAP = ""


@dataclass
class _View:
    key: str
    title: str
    icon: QIcon
    widget: QWidget
    open: bool = False


class CentralViews(QObject):
    """The plugin's pages in QGIS's map area (`plugin.central`)."""

    #: The page now in front: a view's key, MAP ("") for the map (or QGIS's own pages).
    changed = pyqtSignal(str)

    def __init__(self, iface, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        canvas = iface.mapCanvas()
        stack = canvas.parentWidget() if canvas is not None else None
        #: QGIS's stack of map-area pages; None when this QGIS is not built so (the pages can't be shown).
        self.stack: Optional[QStackedWidget] = stack if isinstance(stack, QStackedWidget) else None
        self.views: dict[str, _View] = {}
        #: The keys of the open views, in tab order.
        self.order: list[str] = []
        self.bar: Optional[QTabBar] = None
        #: QGIS's widgets moved down a row for the tabs: (widget, row, column, rows, columns) as they were.
        self._moved: list[tuple] = []
        self._box: Optional[QBoxLayout] = None
        #: The page shown when going back to the map: QGIS's own one that was in front before (the map or the welcome page).
        self._back = 0
        self._syncing = False
        if self.stack is not None:
            self._back = max(0, self.stack.indexOf(canvas))
            self.stack.currentChanged.connect(self._stack_changed)

    @property
    def available(self) -> bool:
        """Whether QGIS's map area can hold the plugin's pages."""
        return self.stack is not None

    def add_view(self, key: str, title: str, icon: QIcon, widget: QWidget) -> None:
        """Makes a page known (not yet open)."""
        self.views[key] = _View(key, title, icon, widget)

    # --- Opening and closing ----------------------------------------------------------------------

    def is_open(self, key: str) -> bool:
        view = self.views.get(key)
        return view is not None and view.open

    def current(self) -> str:
        """The key of the page in front (MAP for the map or another of QGIS's pages)."""
        if self.stack is None:
            return MAP
        widget = self.stack.currentWidget()
        return next((v.key for v in self.views.values() if v.open and v.widget is widget), MAP)

    def open(self, key: str, front: bool = True) -> bool:
        """Opens a page (its tab added) and, with `front`, shows it; False when it can't be."""
        view = self.views.get(key)
        if view is None or self.stack is None:
            return False
        if not view.open:
            view.open = True
            self.order.append(key)
            if self.stack.indexOf(view.widget) < 0:
                self.stack.addWidget(view.widget)
            self._put_bar()
            self._sync_tabs()
            self._remember()
        if front:
            self.show(key)
        return True

    def show(self, key: str) -> None:
        """Brings an open page (or the map, MAP) to the front."""
        if self.stack is None:
            return
        if key == MAP:
            self.show_map()
            return
        view = self.views.get(key)
        if view is None or not view.open:
            return
        if self.current() == MAP:
            self._back = self.stack.currentIndex()
        self.stack.setCurrentWidget(view.widget)

    def show_map(self) -> None:
        """The map (or QGIS's page that was in front before, such as the welcome page) in front again."""
        if self.stack is None or self.current() == MAP:
            return
        ours = {v.widget for v in self.views.values()}
        back = self._back if 0 <= self._back < self.stack.count() and self.stack.widget(self._back) not in ours else 0
        self.stack.setCurrentIndex(back)

    def toggle(self, key: str) -> None:
        """A page and the map in turn: the map when the page is in front, else the page (opened when it was not)."""
        if self.current() == key:
            self.show_map()
        else:
            self.open(key)

    def close(self, key: str) -> None:
        """Closes a page: its tab goes (the map in front when it was); the tabs go with the last one."""
        view = self.views.get(key)
        if view is None or not view.open or self.stack is None:
            return
        if self.current() == key:
            self.show_map()
        view.open = False
        self.order.remove(key)
        self.stack.removeWidget(view.widget)
        view.widget.hide()
        if not self.order:
            self._take_bar()
        else:
            self._sync_tabs()
        self._remember()

    def set_title(self, key: str, title: str) -> None:
        """A page's tab text (as 「新着画像（3）」)."""
        view = self.views.get(key)
        if view is None:
            return
        view.title = title
        if self.bar is not None and key in self.order:
            self.bar.setTabText(1 + self.order.index(key), title)

    def restore(self) -> None:
        """The pages open when QGIS was last closed open again, behind the map."""
        for key in settings.central_views():
            if key in self.views:
                self.open(key, front=False)

    def _remember(self) -> None:
        settings.set_central_views(list(self.order))

    # --- The tabs ---------------------------------------------------------------------------------

    def _put_bar(self) -> None:
        """The tab strip over the map area (once); QGIS's widgets there move down a row."""
        if self.bar is not None or self.stack is None:
            return
        holder = self.stack.parentWidget()
        layout = holder.layout() if holder is not None else None
        bar = QTabBar(holder)
        bar.setObjectName("MikageCentralTabs")
        bar.setDocumentMode(True)
        bar.setExpanding(False)
        bar.setTabsClosable(True)
        bar.setMovable(False)
        bar.setUsesScrollButtons(True)
        bar.setElideMode(Qt.TextElideMode.ElideNone)
        bar.currentChanged.connect(self._tab_chosen)
        bar.tabCloseRequested.connect(lambda i: self.close(bar.tabData(i)) if i > 0 else None)
        if isinstance(layout, QGridLayout):
            items = []
            for i in range(layout.count()):
                widget = layout.itemAt(i).widget()
                if widget is None:
                    items = None  # a layout or a spacer in it: not moved (no tabs, the pages still switch)
                    break
                items.append((widget, *layout.getItemPosition(i)))
            if items is None:
                bar.deleteLater()
                return
            for widget, row, column, rows, columns in items:
                layout.removeWidget(widget)
            for widget, row, column, rows, columns in items:
                layout.addWidget(widget, row + 1, column, rows, columns)
            layout.addWidget(bar, 0, 0, 1, max(1, layout.columnCount()))
            self._moved = items
        elif isinstance(layout, QBoxLayout):
            layout.insertWidget(max(0, layout.indexOf(self.stack)), bar)
            self._box = layout
        else:
            bar.deleteLater()
            return
        self.bar = bar
        bar.show()

    def _take_bar(self) -> None:
        """The tab strip taken away, QGIS's widgets back where they were."""
        bar, self.bar = self.bar, None
        if bar is None:
            return
        holder = self.stack.parentWidget() if self.stack is not None else None
        layout = holder.layout() if holder is not None else None
        try:
            if isinstance(layout, QGridLayout):
                layout.removeWidget(bar)
                for widget, row, column, rows, columns in self._moved:
                    layout.removeWidget(widget)
                for widget, row, column, rows, columns in self._moved:
                    layout.addWidget(widget, row, column, rows, columns)
            elif self._box is not None:
                self._box.removeWidget(bar)
        except RuntimeError:  # QGIS closing: its widgets already gone
            pass
        self._moved, self._box = [], None
        bar.hide()
        bar.setParent(None)
        bar.deleteLater()

    def _sync_tabs(self) -> None:
        """The tabs as the open pages are, the one in front chosen."""
        bar = self.bar
        if bar is None:
            return
        self._syncing = True
        try:
            while bar.count():
                bar.removeTab(0)
            bar.addTab(QgsApplication.getThemeIcon("/mActionNewMap.svg"), tr("地図"))
            bar.setTabData(0, MAP)
            bar.setTabToolTip(0, tr("地図に切り替え"))
            for side in (QTabBar.ButtonPosition.RightSide, QTabBar.ButtonPosition.LeftSide):
                bar.setTabButton(0, side, None)
            for key in self.order:
                view = self.views[key]
                i = bar.addTab(view.icon, view.title)
                bar.setTabData(i, key)
                bar.setTabToolTip(i, tr("{title}に切り替え（× で閉じる）").format(title=view.title))
            current = self.current()
            bar.setCurrentIndex(0 if current == MAP else 1 + self.order.index(current))
        finally:
            self._syncing = False

    def _tab_chosen(self, index: int) -> None:
        if self._syncing or self.bar is None or index < 0:
            return
        self.show(self.bar.tabData(index) or MAP)

    def _stack_changed(self, _index: int) -> None:
        """Whichever page came to the front (QGIS's going back to the map too): the tabs follow."""
        key = self.current()
        if key == MAP and self.stack is not None:
            self._back = self.stack.currentIndex()
        if self.bar is not None:
            self._syncing = True
            try:
                self.bar.setCurrentIndex(0 if key == MAP or key not in self.order else 1 + self.order.index(key))
            finally:
                self._syncing = False
        self.changed.emit(key)

    def unload(self) -> None:
        """The plugin unloads: the map in front, its pages and the tabs gone (what was open is kept for next time)."""
        if self.stack is None:
            return
        try:
            self.stack.currentChanged.disconnect(self._stack_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            if self.current() != MAP:
                self.show_map()
            for view in self.views.values():
                if view.open:
                    self.stack.removeWidget(view.widget)
                    view.widget.hide()
                    view.open = False
        except RuntimeError:  # QGIS closing
            pass
        self.order = []
        self._take_bar()
