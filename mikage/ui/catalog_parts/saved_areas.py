"""
画像カタログドックの保存した範囲 (ui/catalog_parts, docs/architecture.md): the areas drawn kept under a name, chosen again, renamed
and deleted. A part of CatalogDock (ui/catalog_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QInputDialog, QLineEdit

from ...core.catalog.api import saved_areas as saved_area_list
from ...qgis_io import settings
from ...qgis_io.geometry import from_wgs84
from ..docks import ask_no
from .common import local_now


class SavedAreasPart:
    """画像カタログドックの保存した範囲."""

    def saved_areas(self) -> list:
        """The areas saved (newest first)."""
        return settings.saved_areas()

    def saved_area(self):
        """The saved area chosen (a SavedArea), or None."""
        return saved_area_list.find(self.saved_areas(), self.saved_box.currentData())

    def _fill_saved_areas(self, chosen: Optional[str] = None) -> None:
        """The list of saved areas read again, `chosen` (an id; else the one chosen before, else the newest) chosen."""
        chosen = chosen if chosen is not None else self.saved_box.currentData()
        areas = self.saved_areas()
        self.saved_box.blockSignals(True)
        self.saved_box.clear()
        for a in areas:
            self.saved_box.addItem(a.name, a.id)
            self.saved_box.setItemData(self.saved_box.count() - 1, self.tr("{name}\n保存: {when}・頂点 {n}").format(
                name=a.name, when=a.saved or "-", n=len(a.ring) - 1), Qt.ItemDataRole.ToolTipRole)
        if not areas:
            self.saved_box.addItem(self.tr("（保存した範囲なし）"), None)
        i = self.saved_box.findData(chosen) if chosen else -1
        self.saved_box.setCurrentIndex(i if i >= 0 else 0)
        self.saved_box.setEnabled(bool(areas))
        self.saved_box.blockSignals(False)
        for a in self.saved_menu.actions():
            a.setEnabled(bool(areas))
        self._show_area()

    def _no_saved_area(self) -> str:
        return self.tr("保存した範囲なし。「描いた範囲」で描いて「保存…」で保存")

    def _saved_chosen(self, move: bool = False) -> None:
        """A saved area chosen: shown on the map, the map moved to it (`move`) when none of it is in view."""
        area = self.saved_area()
        self._show_area()
        if area is None:
            self.status.setText(self._no_saved_area())
            return
        if move and not self._in_view(area.ring):
            self.zoom_to_saved_area()

    def _in_view(self, ring) -> bool:
        view = self.extent()
        shape = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]])
        return shape.intersects(QgsGeometry.fromRect(QgsRectangle(*view)))

    def zoom_to_saved_area(self) -> None:
        """The map moved to the saved area chosen, with a margin."""
        area = self.saved_area()
        if area is None:
            return
        xs, ys = [p[0] for p in area.ring], [p[1] for p in area.ring]
        box = from_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(QgsRectangle(min(xs), min(ys), max(xs), max(ys)))
        if box.isEmpty():
            box = box.buffered(max(self.canvas.mapUnitsPerPixel() * 200, 1e-6))
        box.scale(1.3)
        self.canvas.setExtent(box)
        self.canvas.refresh()

    def save_area(self, name: str) -> Optional[str]:
        """
        The area drawn saved under `name` (one of the same name replaced) and
        chosen in 「保存した範囲」; the reason it was not, or None.
        """
        if not self.drawn:
            return self.tr("保存する範囲がない。「範囲を描く」で地図に描画")
        try:
            areas, area = saved_area_list.add_area(self.saved_areas(), name, self.drawn, local_now())
        except ValueError as e:
            return str(e)
        settings.set_saved_areas(areas)
        self._fill_saved_areas(area.id)
        self.area_box.setCurrentIndex(self.area_box.findData("saved"))
        self.status.setText(self.tr("範囲「{name}」を保存。「保存した範囲」から選べる").format(name=area.name))
        return None

    def ask_save_area(self) -> None:
        if not self.drawn:
            self.status.setText(self.tr("保存する範囲がない。「範囲を描く」で地図に描画"))
            return
        name, ok = QInputDialog.getText(self, self.tr("範囲の保存"), self.tr("範囲の名前（自分の PC に保存。「保存した範囲」から選べる）"),
                                        QLineEdit.EchoMode.Normal, self.tr("範囲 {when}").format(when=local_now()))
        if not ok or not name.strip():
            return
        if saved_area_list.by_name(self.saved_areas(), name) is not None and not ask_no(
                self, self.tr("範囲の保存"), self.tr("「{name}」はすでにある。今描いた範囲で置き換え").format(name=name.strip()), yes=self.tr("置き換え")):
            return
        problem = self.save_area(name)
        if problem:
            self.status.setText(problem)

    def rename_saved_area(self, name: str) -> Optional[str]:
        area = self.saved_area()
        if area is None:
            return self._no_saved_area()
        try:
            areas = saved_area_list.rename_area(self.saved_areas(), area.id, name)
        except ValueError as e:
            return str(e)
        settings.set_saved_areas(areas)
        self._fill_saved_areas(area.id)
        return None

    def ask_rename_saved_area(self) -> None:
        area = self.saved_area()
        if area is None:
            return
        name, ok = QInputDialog.getText(self, self.tr("範囲の名前の変更"), self.tr("新しい名前"), QLineEdit.EchoMode.Normal, area.name)
        if not ok or not name.strip() or name.strip() == area.name:
            return
        problem = self.rename_saved_area(name)
        if problem:
            self.status.setText(problem)

    def delete_saved_area(self) -> None:
        area = self.saved_area()
        if area is None:
            return
        settings.set_saved_areas(saved_area_list.delete_area(self.saved_areas(), area.id))
        self._fill_saved_areas("")
        self.status.setText(self.tr("範囲「{name}」を削除").format(name=area.name))

    def ask_delete_saved_area(self) -> None:
        area = self.saved_area()
        if area is not None and ask_no(self, self.tr("保存した範囲の削除"), self.tr("「{name}」を削除").format(name=area.name), yes=self.tr("削除")):
            self.delete_saved_area()
