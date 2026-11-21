"""
画像カタログドックの検索の範囲 (ui/catalog_parts, docs/architecture.md): the area searched over: the map's extent, an area drawn, a
layer's geometry, anywhere. A part of CatalogDock (ui/catalog_dock.py): its methods as they were, on the dock's
widgets and state.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsMapLayer

from ...core.catalog.api import CatalogConfig, areas_of
from ...qgis_io.catalog_io import layer_area


class AreasPart:
    """画像カタログドックの検索の範囲."""

    def _area_chosen(self, index: int) -> None:
        kind = self.area_box.itemData(index)
        if kind == "drawn" and self.drawn is None:
            self.draw_button.setChecked(True)
        if kind == "layer" and self.area_layer.currentLayer() is None:
            active = self.iface.activeLayer()
            if active is not None and layer_area(active) is not None:
                self.area_layer.setLayer(active)
        self._show_layer_row()
        if kind == "saved":
            self._saved_chosen(move=True)

    def _show_layer_row(self) -> None:
        kind = self.area_box.currentData()
        self.layer_row.setVisible(kind == "layer")
        self.saved_row.setVisible(kind == "saved")
        self.save_area_button.setVisible(kind == "drawn")
        self.save_area_button.setEnabled(bool(self.drawn))
        self._show_area()

    def drawn_shape(self) -> Optional[list]:
        """The polygon the search is over: the saved area chosen in 「保存した範囲」, else the area drawn (None for neither)."""
        if self.area_box.currentData() == "saved":
            area = self.saved_area()
            return [list(p) for p in area.ring] if area is not None else None
        return self.drawn

    def _show_area(self) -> None:
        """The polygon of the search on the map (green, dashed): the saved area chosen, else the area drawn."""
        if getattr(self, "overlay", None) is not None:
            self.overlay.show_area(self.drawn_shape())

    def use_layer(self, layer: Optional[QgsMapLayer], selected_only: bool = False) -> None:
        """Searches over a vector layer's geometry (its chosen features only, when asked)."""
        self.area_layer.setLayer(layer)
        self.selected_only.setChecked(selected_only)
        self.area_box.setCurrentIndex(self.area_box.findData("layer"))

    def set_drawn(self, corners: Optional[list]) -> None:
        """The area to search (and watch) drawn on the map: its corners in WGS 84; None forgets it."""
        self.drawn = None
        if corners:
            ring = [list(p) for p in corners]
            if ring[0] != ring[-1]:
                ring.append(list(ring[0]))
            self.drawn = ring
            self.area_box.setCurrentIndex(self.area_box.findData("drawn"))
        elif self.area_box.currentData() == "drawn":
            self.area_box.setCurrentIndex(0)
        self.draw_button.setChecked(False)
        self._show_layer_row()

    def search_area_period(self, start: str, end: str, corners: Optional[list] = None, anywhere: bool = False) -> None:
        """
        Searched over a period (its first and last days, "" open) and an area (its corners in WGS 84; `anywhere`:
        no area, not whatever area the dock had before), the dock in front (イベント's 「カタログで検索」).
        """
        self.acquired_from.setText(start)
        self.acquired_to.setText(end)
        if anywhere:
            self.area_box.setCurrentIndex(self.area_box.findData("none"))
        else:
            self.set_drawn(corners)
        self.show()
        self.raise_()
        self.search()

    def show_event_records(self, catalog: CatalogConfig, records, text: str) -> None:
        """The records listed with what they are in the status line, the dock in front (イベント's 「カタログに表示」)."""
        self.show_records(catalog, records)
        self.status.setText(text)
        self.show()
        self.raise_()

    def area(self):
        """
        Where to search: the map's extent (a box), the area drawn (an Esri
        polygon), a layer's geometry (an Esri polygon, polyline or multipoint)
        or None for anywhere.
        """
        kind = self.area_box.currentData()
        if kind == "extent":
            return self.extent()
        if kind == "drawn" and self.drawn:
            return areas_of([], [[self.drawn]])[0]
        if kind == "saved":
            area = self.saved_area()
            return areas_of([], [[[list(p) for p in area.ring]]])[0] if area is not None else None
        if kind == "layer":
            return layer_area(self.area_layer.currentLayer(), self.selected_only.isChecked())
        return None
