"""
画像カタログドックの類似画像 (ui/catalog_parts, docs/architecture.md): 「類似画像の検索」: the tolerance and the search for images like the
chosen one. A part of CatalogDock (ui/catalog_dock.py): its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from ...core.catalog.api import CatalogConfig, CatalogRecord, SimilarTolerance, rank_similar, similar_search
from ...qgis_io import settings
from ...qgis_io.catalog_io import footprint, overlap_measure, record_of_layer
from .common import _Listed


class SimilarPart:
    """画像カタログドックの類似画像."""

    def _load_tolerance(self) -> None:
        t = SimilarTolerance.from_json(settings.similar_tolerance())
        self.same_sensor.setChecked(t.same_sensor)
        self.angle_tolerance.setValue(t.angle if t.angle is not None else 5)
        self.azimuth_tolerance.setValue(t.azimuth if t.azimuth is not None else 20)
        self.min_overlap.setValue(round(t.min_overlap * 100))

    def tolerance(self) -> SimilarTolerance:
        return SimilarTolerance(self.same_sensor.isChecked(), self.angle_tolerance.value(), self.azimuth_tolerance.value(),
                                self.min_overlap.value() / 100)

    def find_similar(self, record: CatalogRecord, catalog: CatalogConfig) -> None:
        """Lists the images like `record` (an image of `catalog`), the most alike first."""
        self.reference = (record, catalog)
        i = next((n for n, c in enumerate(self.catalogs()) if c.url == catalog.url), -1)
        if i >= 0:
            self.catalog_box.setCurrentIndex(i)
        self.reference_label.setText(self.tr("比較元: {id}").format(id=record.id or self.tr("（画像ID なし）")))
        self.similar.show()
        self.show()
        self.raise_()
        self._search_similar()

    def find_similar_of_layer(self, layer) -> bool:
        """「類似画像の検索…」 of a layer opened from the catalog; False for other layers."""
        found = record_of_layer(layer, self.catalogs())
        if found is None:
            return False
        self.find_similar(*found)
        return True

    def _similar_of_chosen(self) -> None:
        chosen = self.chosen()
        if chosen and self._shown_catalog is not None:
            self.find_similar(chosen[0], self._shown_catalog)

    def _search_similar(self) -> None:
        record, catalog = self.reference
        tolerance = self.tolerance()
        settings.set_similar_tolerance(tolerance.to_json())
        search = similar_search(catalog, record, tolerance, self.conditions())
        reference = footprint(record.geometry)
        box = None
        if reference is not None:
            b = reference.boundingBox()
            box = [b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum()]
        measure = overlap_measure(reference)

        def rank(records):
            ranked = rank_similar(record, records, tolerance, measure)
            return [s.record for s in ranked], {id(s.record): s.overlap for s in ranked}

        self._read(_Listed(catalog, search, box, rank))
