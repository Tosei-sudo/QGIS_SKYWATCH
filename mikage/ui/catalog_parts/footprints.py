"""
画像カタログドックの選択と地図の表示 (ui/catalog_parts, docs/architecture.md): the images chosen, their footprints on the map, the map
tools and zooming. A part of CatalogDock (ui/catalog_dock.py): its methods as they were, on the dock's widgets and
state.
"""
from __future__ import annotations

from ...core.catalog.api import CatalogRecord
from ...qgis_io import map_tools, settings
from ...qgis_io.catalog_io import footprint, layers_extent
from ...qgis_io.geometry import WGS84, from_wgs84
from ...qgis_io.map_view_io import fitted_boxes, fitted_extent
from ..catalog_overlay import FootprintOverlay


class FootprintsPart:
    """画像カタログドックの選択と地図の表示."""

    def chosen(self) -> list[CatalogRecord]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.records[r] for r in rows]

    def choose(self, records) -> None:
        """Chooses records of the list (as the map's selection follows)."""
        wanted = {id(r) for r in records}
        selection = self.table.selectionModel()
        selection.clearSelection()
        flags = selection.SelectionFlag.Select | selection.SelectionFlag.Rows
        for row, r in enumerate(self.model.records):
            if id(r) in wanted:
                selection.select(self.model.index(row, 0), flags)

    def set_footprint_style(self, style: str) -> None:
        """「フットプリント」: 'fill', 'outline' or 'hidden', kept for next time (per user)."""
        settings.set_footprint_style(style)
        if self.footprint_box.currentData() != style:
            self.footprint_box.setCurrentIndex(max(0, self.footprint_box.findData(style)))
        if self.overlay is not None:
            self.overlay.set_style(style)

    def _redraw(self) -> None:
        if self.overlay is None:
            return
        reference = footprint(self.reference[0].geometry) if self.reference else None
        self.overlay.show([footprint(r.geometry) for r in self.model.records], reference)
        self._selection_changed()

    def _selection_changed(self, *args) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if self.overlay is not None:
            self.overlay.choose(rows, [self.model.records[r].id for r in rows])
        has = bool(rows)
        for key in ("open", "record", "zoom", "copy", "folder", "similar"):
            self.image_actions[key].setEnabled(has)
        if len(rows) == 1:
            self.table.scrollTo(self.model.index(rows[0], 0))

    def _picked(self, lon: float, lat: float, tolerance: float, add: bool) -> None:
        if self.overlay is None:
            return
        row = self.overlay.at(lon, lat, tolerance)
        selection = self.table.selectionModel()
        if row is None:
            if not add:
                selection.clearSelection()
            return
        index = self.model.index(row, 0)
        flags = selection.SelectionFlag.Rows
        if add:
            selection.select(index, flags | selection.SelectionFlag.Toggle)
        else:
            selection.select(index, flags | selection.SelectionFlag.ClearAndSelect)

    def _pick_toggled(self, on: bool) -> None:
        self._use_tool(self.tool, on)

    def _draw_toggled(self, on: bool) -> None:
        self._use_tool(self.draw_tool, on)

    def _use_tool(self, tool, on: bool) -> None:
        if on:
            map_tools.take(self.canvas, tool)
        else:
            map_tools.give_back(self.canvas, tool)

    def zoom_to_chosen(self) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        box = self.overlay.extent(rows) if self.overlay else None
        if box is None:
            return
        crs = self.canvas.mapSettings().destinationCrs()
        if abs(self.canvas.rotation()) > 1e-9:
            # A turned map (「向き」): QGIS's extent is before the rotation, so the box is fitted to the turned view.
            extent = fitted_boxes([(box, WGS84)], crs, self.canvas.rotation(), 1.2)
        else:
            extent = from_wgs84(crs).transformBoundingBox(box)
            extent.scale(1.2)
        if extent is None:
            return
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def zoom_to_layers(self, layers) -> None:
        """The map moved to the extent of the layers (their image files'; the box around all of them), with a small margin."""
        crs = self.canvas.mapSettings().destinationCrs()
        extent = layers_extent(layers, crs)
        if extent is None:
            return
        if extent.isEmpty():
            extent = extent.buffered(max(self.canvas.mapUnitsPerPixel() * 200, 1e-6))
            extent.scale(1.05)
        elif abs(self.canvas.rotation()) > 1e-9:
            # A turned map (「向き」): the images' outlines fitted to the turned view (QGIS's extent is before the rotation).
            extent = fitted_extent(layers, crs, self.canvas.rotation()) or extent
        else:
            extent.scale(1.05)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def _visibility(self, visible: bool) -> None:
        if not visible:
            self._work_timer.stop()
        elif self.model.work is not None and self.model.records:
            self.load_work()
        if visible and self.model.reviews is not None and self.model.records:
            self.load_ratings()
        if visible and self.overlay is None:
            self.overlay = FootprintOverlay(self.canvas, self.footprint_box.currentData())
            self.overlay.show_area(self.drawn_shape())
            self._redraw()
        elif not visible and self.overlay is not None:
            self.buttons["pick"].setChecked(False)
            self.draw_button.setChecked(False)
            self.overlay.remove()
            self.overlay = None
