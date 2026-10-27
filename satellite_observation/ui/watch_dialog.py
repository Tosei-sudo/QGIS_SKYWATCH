"""
The dialogs of 新着画像の通知: 「この条件で監視…」 (a new watch over the map's
extent, the area drawn, the chosen features or nowhere in particular) and 「この地点の新着画像を
監視…」 (a new watch of a point, or the point added to a watch).
"""
from __future__ import annotations

from typing import Optional, Sequence

from qgis.core import QgsMapLayer, QgsWkbTypes
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QRadioButton, QVBoxLayout,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.query import CatalogSearch
from ..core.catalog.watch import CatalogWatch, areas_of, conditions_text
from ..qgis_io.geometry import shape_of, to_wgs84


def extent_area(canvas) -> list[dict]:
    """The map's extent as a watch area (WGS 84)."""
    box = to_wgs84(canvas.mapSettings().destinationCrs()).transformBoundingBox(canvas.extent())
    x0, y0 = max(-180.0, box.xMinimum()), max(-90.0, box.yMinimum())
    x1, y1 = min(180.0, box.xMaximum()), min(90.0, box.yMaximum())
    return areas_of([], [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]])


def selected_areas(layer: Optional[QgsMapLayer]) -> list[dict]:
    """The chosen features of a vector layer as watch areas: points as points, polygons as polygons (lines left out)."""
    if layer is None or layer.type() != QgsMapLayer.LayerType.VectorLayer or not layer.selectedFeatureCount():
        return []
    transform = to_wgs84(layer.crs())
    points: list = []
    polygons: list = []
    for feature in layer.selectedFeatures():
        shape = shape_of(feature.geometry(), transform)
        if shape is None:
            continue
        if feature.geometry().type() == QgsWkbTypes.GeometryType.PointGeometry:
            points += [[x, y] for x, y in shape.vertices]
        elif shape.polygons:
            polygons += shape.polygons
    return areas_of(points, polygons)


class WatchDialog(QDialog):
    """「この条件で監視」: the watch's name and area."""

    def __init__(self, catalog: CatalogConfig, conditions: CatalogSearch, canvas, layer: Optional[QgsMapLayer], parent=None,
                 drawn: Optional[list] = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("新着画像の監視"))
        self.catalog = catalog
        self.conditions = conditions
        self.canvas = canvas
        self.layer = layer
        self.drawn = drawn
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(conditions_text(catalog, conditions, [], with_areas=False))
        form.addRow(self.tr("名前"), self.name)
        self.area = QComboBox()
        self.area.addItem(self.tr("表示範囲"), "extent")
        if drawn:
            self.area.addItem(self.tr("描いた範囲"), "drawn")
            self.area.setCurrentIndex(1)
        if selected_areas(layer):
            self.area.addItem(self.tr("選択中の地物（{name}）").format(name=layer.name()), "selected")
        self.area.addItem(self.tr("指定なし"), "none")
        form.addRow(self.tr("範囲"), self.area)
        layout.addLayout(form)
        note = QLabel(self.tr("条件: {text}").format(text=conditions_text(catalog, conditions, [], with_areas=False)))
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel(self.tr("初回の確認は既存の画像の記録のみ。以降の新着画像を通知")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def areas(self) -> list[dict]:
        kind = self.area.currentData()
        if kind == "extent":
            return extent_area(self.canvas)
        if kind == "drawn":
            return areas_of([], [[self.drawn]])
        if kind == "selected":
            return selected_areas(self.layer)
        return []


class PointWatchDialog(QDialog):
    """「この地点の新着画像を監視」: a new watch of the point, or the point added to a watch."""

    def __init__(self, lon: float, lat: float, catalogs: Sequence[CatalogConfig], watches: Sequence[CatalogWatch], parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("この地点の新着画像の監視"))
        self.catalogs = list(catalogs)
        #: Only watches over areas can take a point (one over nowhere in particular already sees it).
        self.watches = [w for w in watches if w.areas]
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.tr("地点 {lat:.4f}, {lon:.4f}").format(lat=lat, lon=lon)))
        self.new = QRadioButton(self.tr("新しい監視"))
        self.new.setChecked(True)
        layout.addWidget(self.new)
        form = QFormLayout()
        self.catalog = QComboBox()
        for c in self.catalogs:
            self.catalog.addItem(c.label)
        form.addRow(self.tr("カタログ"), self.catalog)
        self.name = QLineEdit(self.tr("地点 {lat:.4f}, {lon:.4f}").format(lat=lat, lon=lon))
        form.addRow(self.tr("名前"), self.name)
        layout.addLayout(form)
        self.add = QRadioButton(self.tr("既存の監視に追加"))
        self.watch = QComboBox()
        for w in self.watches:
            self.watch.addItem(w.name, w.id)
        layout.addWidget(self.add)
        layout.addWidget(self.watch)
        self.add.setEnabled(bool(self.watches))
        self.watch.setEnabled(False)
        self.add.toggled.connect(self.watch.setEnabled)
        self.add.toggled.connect(lambda on: (self.catalog.setEnabled(not on), self.name.setEnabled(not on)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.catalogs) or bool(self.watches))
        if not self.catalogs and self.watches:
            self.add.setChecked(True)
            self.new.setEnabled(False)

    def chosen_watch(self) -> Optional[str]:
        """The watch to add the point to; None for a new one."""
        return self.watch.currentData() if self.add.isChecked() else None

    def chosen_catalog(self) -> Optional[CatalogConfig]:
        i = self.catalog.currentIndex()
        return self.catalogs[i] if 0 <= i < len(self.catalogs) else None
