"""
「ドックのレイアウト」: named layouts of the plugin's docks, one per kind of
work (tsudo, 2026-11-07), e.g. 「観測記録の担当」 or 「撮像計画の担当」.

A layout says which docks are open and where: each tab group on its side
(in order from the top or the left, with its front tab and size), and each
floating dock with its geometry. Only the plugin's own docks are kept, so
QGIS's panels and toolbars stay as they are. Layouts are kept per user
(QgsSettings) and can be written to a file and read on another PC.

Built-in examples (PRESETS) are offered until a layout of the same name is
saved, which then replaces the example.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, QRect, Qt
from qgis.PyQt.QtWidgets import QDockWidget, QFileDialog, QInputDialog, QMainWindow, QMenu, QMessageBox, QWidget

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

KEY = "satellite_observation/dockLayouts"
LAST_KEY = "satellite_observation/dockLayoutLast"
FILE_KIND = "satellite_observation.dockLayouts"

_AREAS = {
    "left": Qt.DockWidgetArea.LeftDockWidgetArea,
    "right": Qt.DockWidgetArea.RightDockWidgetArea,
    "top": Qt.DockWidgetArea.TopDockWidgetArea,
    "bottom": Qt.DockWidgetArea.BottomDockWidgetArea,
}

#: Built-in examples: (name, [(side, [docks of a tab group, the first in front]), ...]); every dock listed is open.
PRESETS = [
    ("撮像計画の担当", [
        ("right", ["SatObsImagingPlan", "SatObsSatellitePass", "SatObsSavedPlans"]),
        ("right", ["SatObsRequests", "SatObsPlanFiles"]),
        ("left", ["SatObsImageCatalog"]),
    ]),
    ("観測記録の担当", [
        ("left", ["SatObsImageCatalog", "SatObsEnhance"]),
        ("right", ["SatObsObservations", "SatObsAiResults", "SatObsTaxonomy"]),
    ]),
    ("画像判読", [
        ("left", ["SatObsImageCatalog", "SatObsNotices"]),
        ("right", ["SatObsEnhance", "SatObsShadowHeight", "SatObsSpectral"]),
        ("right", ["SatObsObservations"]),
    ]),
]


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def _side(area) -> str:
    return next((k for k, v in _AREAS.items() if v == area), "right")


def preset_layout(groups: list) -> dict:
    """A layout from a PRESETS entry."""
    return {"groups": [{"area": side, "docks": [{"name": n, "visible": True} for n in names], "front": names[0]}
                       for side, names in groups],
            "floating": []}


def capture(window: QMainWindow, docks: dict[str, QDockWidget]) -> dict:
    """The layout of `docks` (name → dock) in `window` now."""
    # Tab groups: Qt lists a hidden tab only in its open neighbours' tabifiedDockWidgets, so join both ways.
    group_of: dict[str, set] = {}
    for name, dock in docks.items():
        if dock.isFloating():
            continue
        members = {name} | {d.objectName() for d in window.tabifiedDockWidgets(dock) if d.objectName() in docks and not d.isFloating()}
        merged = set(members)
        for m in members:
            merged |= group_of.get(m, set())
        for m in merged:
            group_of[m] = merged
    groups, seen = [], set()
    order = list(docks)
    for name in order:
        if name not in group_of or id(group_of[name]) in seen:
            continue
        seen.add(id(group_of[name]))
        names = [n for n in order if n in group_of[name]]
        shown = [docks[n] for n in names if docks[n].isVisible()]
        front = next((d for d in shown if not d.visibleRegion().isEmpty()), shown[0] if shown else None)
        area = window.dockWidgetArea(docks[names[0]])
        geometry = front.geometry() if front is not None else QRect()
        groups.append({
            "area": _side(area),
            "docks": [{"name": n, "visible": docks[n].isVisible()} for n in names],
            "front": front.objectName() if front is not None else names[0],
            "size": [geometry.width(), geometry.height()] if front is not None else None,
            "_pos": (0 if front is not None else 1, geometry.y() if _side(area) in ("left", "right") else geometry.x()),
        })
    # From the top (or the left) on each side, as they are stacked; groups with nothing open last.
    groups.sort(key=lambda g: g.pop("_pos"))
    floating = []
    for name, dock in docks.items():
        if dock.isFloating():
            g = dock.geometry()
            floating.append({"name": name, "visible": dock.isVisible(), "area": _side(window.dockWidgetArea(dock)),
                             "geometry": [g.x(), g.y(), g.width(), g.height()]})
    return {"groups": groups, "floating": floating}


def apply(window: QMainWindow, docks: dict[str, QDockWidget], layout: dict) -> None:
    """Puts `docks` as `layout` says; a dock it doesn't name (one added since) is closed."""
    layout = clean(layout) or {"groups": [], "floating": []}
    listed: set = set()
    resize: list = []
    for group in layout.get("groups") or []:
        members = [(e["name"], bool(e.get("visible", True))) for e in group.get("docks") or [] if e.get("name") in docks and e["name"] not in listed]
        if not members:
            continue
        area = _AREAS.get(group.get("area"), Qt.DockWidgetArea.RightDockWidgetArea)
        first = docks[members[0][0]]
        for name, _ in members:
            listed.add(name)
            docks[name].setFloating(False)
            window.addDockWidget(area, docks[name])
        for name, _ in members[1:]:
            window.tabifyDockWidget(first, docks[name])
        for name, visible in members:
            docks[name].setVisible(visible)
        front = docks.get(group.get("front"))
        if front is not None and front.isVisible():
            front.raise_()
        size = group.get("size")
        if size and front is not None and front.isVisible():
            resize.append((front, area, size))
    for entry in layout.get("floating") or []:
        name = entry.get("name")
        if name not in docks or name in listed:
            continue
        listed.add(name)
        dock = docks[name]
        window.addDockWidget(_AREAS.get(entry.get("area"), Qt.DockWidgetArea.RightDockWidgetArea), dock)
        dock.setFloating(True)
        dock.setVisible(bool(entry.get("visible", True)))
        geometry = entry.get("geometry")
        if geometry and len(geometry) == 4:
            dock.setGeometry(QRect(*[int(v) for v in geometry]))
    for name, dock in docks.items():
        if name not in listed:
            dock.setVisible(False)
    # The width of each side and the height of each group, as saved.
    if resize:
        sides = [(d, s) for d, a, s in resize if a in (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea)]
        rows = [(d, s) for d, a, s in resize if (d, s) not in sides]
        if sides:
            window.resizeDocks([d for d, _ in sides], [int(s[0]) for _, s in sides], Qt.Orientation.Horizontal)
            window.resizeDocks([d for d, _ in sides], [int(s[1]) for _, s in sides], Qt.Orientation.Vertical)
        if rows:
            window.resizeDocks([d for d, _ in rows], [int(s[1]) for _, s in rows], Qt.Orientation.Vertical)
            window.resizeDocks([d for d, _ in rows], [int(s[0]) for _, s in rows], Qt.Orientation.Horizontal)


def _numbers(v, n: int) -> Optional[list]:
    """`n` whole numbers (a size or a geometry), else None."""
    if not isinstance(v, (list, tuple)) or len(v) != n:
        return None
    try:
        out = [int(x) for x in v if not isinstance(x, bool)]
    except (TypeError, ValueError, OverflowError):
        return None
    return out if len(out) == n and all(abs(x) < 100_000 for x in out) else None


def clean(layout) -> Optional[dict]:
    """
    A layout with only what `apply` can use (a file read from another PC, or
    edited by hand, may hold anything): None when it is not a layout at all.
    """
    if not isinstance(layout, dict) or not isinstance(layout.get("groups", []), list) or not isinstance(layout.get("floating", []), list):
        return None
    groups = []
    for g in layout.get("groups") or []:
        if not isinstance(g, dict) or not isinstance(g.get("docks"), list):
            continue
        members = [{"name": e["name"], "visible": e.get("visible", True) is not False} for e in g["docks"]
                   if isinstance(e, dict) and isinstance(e.get("name"), str)]
        if members:
            groups.append({"area": g.get("area") if g.get("area") in _AREAS else "right", "docks": members,
                           "front": g.get("front") if isinstance(g.get("front"), str) else members[0]["name"],
                           "size": _numbers(g.get("size"), 2)})
    floating = [{"name": e["name"], "visible": e.get("visible", True) is not False,
                 "area": e.get("area") if e.get("area") in _AREAS else "right", "geometry": _numbers(e.get("geometry"), 4)}
                for e in layout.get("floating") or [] if isinstance(e, dict) and isinstance(e.get("name"), str)]
    return {"groups": groups, "floating": floating}


def valid(layout) -> bool:
    return clean(layout) is not None


class DockLayoutStore:
    """The saved layouts (QgsSettings, per user profile), in the order saved."""

    def saved(self) -> dict[str, dict]:
        try:
            value = json.loads(QgsSettings().value(KEY, "") or "{}")
        except (TypeError, ValueError):
            return {}
        return {k: v for k, v in value.items() if isinstance(k, str) and valid(v)} if isinstance(value, dict) else {}

    def _write(self, layouts: dict) -> None:
        QgsSettings().setValue(KEY, json.dumps(layouts, ensure_ascii=False))

    def all(self) -> dict[str, dict]:
        """The examples not replaced, then the saved ones."""
        saved = self.saved()
        layouts = {name: preset_layout(groups) for name, groups in PRESETS if name not in saved}
        layouts.update(saved)
        return layouts

    def save(self, name: str, layout: dict) -> None:
        layouts = self.saved()
        layouts[name] = layout
        self._write(layouts)

    def delete(self, name: str) -> None:
        layouts = self.saved()
        layouts.pop(name, None)
        self._write(layouts)

    @property
    def last(self) -> str:
        value = QgsSettings().value(LAST_KEY, "")
        return value if isinstance(value, str) else ""

    @last.setter
    def last(self, name: str) -> None:
        QgsSettings().setValue(LAST_KEY, name)

    def export(self, path: str, names: Optional[list] = None) -> int:
        """Writes the saved layouts (or those named) to a file; how many."""
        layouts = {k: v for k, v in self.all().items() if names is None or k in names}
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"kind": FILE_KIND, "layouts": layouts}, f, ensure_ascii=False, indent=2)
        return len(layouts)

    def import_file(self, path: str) -> list[str]:
        """Saves the layouts in a file written by `export` (same names replaced); their names."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("kind") != FILE_KIND or not isinstance(data.get("layouts"), dict):
            raise ValueError(tr("ドックのレイアウトのファイルではありません"))
        layouts = self.saved()
        names = [k for k, v in data["layouts"].items() if isinstance(k, str) and k.strip() and valid(v)]
        if not names:
            raise ValueError(tr("ドックのレイアウトのファイルに使えるレイアウトがありません"))
        for name in names:
            layouts[name.strip()] = clean(data["layouts"][name])
        self._write(layouts)
        return [n.strip() for n in names]


class DockLayoutMenu(QMenu):
    """
    「ドックのレイアウト」: a submenu of the plugin's menu with each layout (the
    one last used checked), then 「現在の配置を保存…」, 「削除」 and file in/out.
    """

    def __init__(self, window: QMainWindow, docks: Callable[[], dict], message: Callable[[str], None], parent: QWidget | None = None):
        super().__init__(tr("ドックのレイアウト"), parent)
        self.setObjectName("SatObsDockLayouts")
        self.window_ = window
        self.docks = docks
        self.message = message
        self.store = DockLayoutStore()
        self.aboutToShow.connect(self.rebuild)
        self.rebuild()

    def rebuild(self) -> None:
        self.clear()
        last = self.store.last
        layouts = self.store.all()
        for name in layouts:
            action = self.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == last)
            action.triggered.connect(lambda _=False, n=name: self.use(n))
        if layouts:
            self.addSeparator()
        self.addAction(tr("現在の配置を保存…")).triggered.connect(lambda: self.ask_save())
        saved = list(self.store.saved())
        delete = self.addMenu(tr("削除"))
        delete.setEnabled(bool(saved))
        for name in saved:
            delete.addAction(name).triggered.connect(lambda _=False, n=name: self.ask_delete(n))
        self.addSeparator()
        self.addAction(tr("ファイルへ書き出し…")).triggered.connect(lambda: self.ask_export())
        self.addAction(tr("ファイルから読み込み…")).triggered.connect(lambda: self.ask_import())

    # The work, also for the plugin's API and tests.
    def use(self, name: str) -> bool:
        layout = self.store.all().get(name)
        if layout is None:
            return False
        apply(self.window_, self.docks(), layout)
        self.store.last = name
        return True

    def save_current(self, name: str) -> None:
        self.store.save(name, capture(self.window_, self.docks()))
        self.store.last = name

    # The dialogs.
    def ask_save(self) -> None:
        names = list(self.store.all())
        name, ok = QInputDialog.getItem(self.window_, tr("ドックのレイアウトを保存"),
                                        tr("名前（例: 観測記録の担当）。同じ名前のものは置き換えます。"),
                                        names, names.index(self.store.last) if self.store.last in names else -1, True)
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.store.saved() and QMessageBox.question(
                self.window_, tr("ドックのレイアウトを保存"), tr("「{name}」を今の配置で置き換えますか。").format(name=name)) != QMessageBox.StandardButton.Yes:
            return
        self.save_current(name)
        self.message(tr("ドックのレイアウト「{name}」を保存しました").format(name=name))

    def ask_delete(self, name: str) -> None:
        if QMessageBox.question(self.window_, tr("ドックのレイアウトの削除"), tr("「{name}」を削除しますか。").format(name=name)) == QMessageBox.StandardButton.Yes:
            self.store.delete(name)

    def ask_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self.window_, tr("ドックのレイアウトを書き出す"), "dock_layouts.json", tr("JSON (*.json)"))
        if path:
            try:
                n = self.store.export(path)
            except OSError as e:
                QMessageBox.warning(self.window_, tr("ドックのレイアウトを書き出す"), tr("書き出せません: {e}").format(e=e.strerror or e))
                return
            self.message(tr("ドックのレイアウト {n} 件を書き出しました").format(n=n))

    def ask_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.window_, tr("ドックのレイアウトを読み込む"), "", tr("JSON (*.json)"))
        if not path:
            return
        try:
            names = self.store.import_file(path)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            QMessageBox.warning(self.window_, tr("ドックのレイアウトを読み込む"), str(e))
            return
        self.message(tr("ドックのレイアウト {n} 件を読み込みました: {names}").format(n=len(names), names="、".join(names)))
