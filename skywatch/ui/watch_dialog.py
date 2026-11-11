"""
The dialogs of 新着画像の通知: 「この条件で監視…」 (a new watch over the map's
extent, the area drawn, a layer's features as they are or nowhere in
particular), 「範囲の変更…」 (a watch's area taken again) and 「この地点の新着画像を
監視…」 (a new watch of a point, or the point added to a watch).

A layer's features are copied into the watch as they are when it is set (see
core/catalog/watch.py): the watch goes on with the project closed, and a
layer edited later changes it only through 「範囲の変更…」.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from qgis.core import QgsMapLayer, QgsProject
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QRadioButton, QVBoxLayout, QWidget,
)

from ..core.catalog.config import CatalogConfig
from ..core.catalog.query import CatalogSearch
from ..core.catalog.watch import CatalogWatch, areas_of, conditions_text
from ..qgis_io.catalog_io import layer_watch_areas
from ..qgis_io.geometry import to_wgs84
from .docks import plain_label
from .last_inputs import InputMemory

try:
    from qgis.core import QgsMapLayerProxyModel

    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.Filter.HasGeometry
except AttributeError:  # older bindings: the enum unscoped
    from qgis.core import QgsMapLayerProxyModel

    _GEOMETRY_LAYERS = QgsMapLayerProxyModel.HasGeometry

#: More vertices than this: the dialog says the shape is large (it is sent with every check).
MANY_VERTICES = 20000


def extent_area(canvas) -> list[dict]:
    """The map's extent as a watch area (WGS 84)."""
    box = to_wgs84(canvas.mapSettings().destinationCrs()).transformBoundingBox(canvas.extent())
    x0, y0 = max(-180.0, box.xMinimum()), max(-90.0, box.yMinimum())
    x1, y1 = min(180.0, box.xMaximum()), min(90.0, box.yMaximum())
    return areas_of([], [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]])


def _vertices(areas: Sequence[dict]) -> int:
    n = 0
    for a in areas:
        if a["kind"] == "points":
            n += len(a["points"])
        else:
            n += sum(len(part) for part in a.get("rings") or a.get("paths") or [])
    return n


def _geometry_layer(layer: Optional[QgsMapLayer]) -> Optional[QgsMapLayer]:
    return layer if layer is not None and layer.type() == QgsMapLayer.LayerType.VectorLayer and layer.isSpatial() else None


def origin_layer(origin: Optional[dict[str, Any]]) -> Optional[QgsMapLayer]:
    """The project's layer a watch's areas were taken from: the same layer (by id), else one of the same name; None when there is none."""
    if not origin:
        return None
    project = QgsProject.instance()
    layer = _geometry_layer(project.mapLayer(str(origin.get("id") or "")))
    if layer is None:
        layer = next((_geometry_layer(l) for l in project.mapLayersByName(str(origin.get("name") or "")) if _geometry_layer(l)), None)
    return layer


class AreaChoice(QWidget):
    """
    The area of a watch: the map's extent, the area drawn, a layer's
    features as they are (its chosen ones only, when asked; copied into the
    watch) or nowhere in particular.
    """

    def __init__(self, canvas, drawn: Optional[list] = None, start: Optional[str] = None, layer: Optional[QgsMapLayer] = None,
                 selected_only: Optional[bool] = None, later: bool = True, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.drawn = drawn
        #: Whether the note tells how to take the layer's shape again later (not in 「範囲の変更…」 itself).
        self.later = later
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        self.kind.addItem(self.tr("表示範囲"), "extent")
        if drawn:
            self.kind.addItem(self.tr("描いた範囲"), "drawn")
        self.kind.addItem(self.tr("レイヤー"), "layer")
        self.kind.addItem(self.tr("指定なし"), "none")
        v.addWidget(self.kind)
        # 「レイヤー」: the features of a vector layer, watched over as they are (not their extent).
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(_GEOMETRY_LAYERS)
        self.layer.setAllowEmptyLayer(True)
        self.layer.setToolTip(self.tr("地物の形そのもの（外接矩形ではなく）と重なる新着画像を通知。面・線・点のどれでも"))
        self.selected_only = QCheckBox(self.tr("選択地物のみ"))
        self.layer_row = QWidget()
        row = QHBoxLayout(self.layer_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.layer, 1)
        row.addWidget(self.selected_only)
        v.addWidget(self.layer_row)
        self.note = plain_label(QLabel())
        self.note.setWordWrap(True)
        v.addWidget(self.note)

        layer = _geometry_layer(layer)
        self.layer.setLayer(layer)
        self.selected_only.setChecked(bool(layer is not None and layer.selectedFeatureCount()) if selected_only is None else selected_only)
        if start is None:
            start = "drawn" if drawn else "extent"
        if start == "layer" and layer is None:
            start = "extent"
        index = self.kind.findData(start)
        self.kind.setCurrentIndex(index if index >= 0 else 0)
        self.kind.currentIndexChanged.connect(lambda *_: self._shown())
        self.layer.layerChanged.connect(self._layer_changed)
        self.selected_only.toggled.connect(lambda *_: self._shown())
        self._shown()

    def _layer_changed(self, layer) -> None:
        # Another layer: its chosen features when it has some (as 「選択中の地物」 used to be).
        self.selected_only.blockSignals(True)
        self.selected_only.setChecked(bool(layer is not None and layer.selectedFeatureCount()))
        self.selected_only.blockSignals(False)
        self._shown()

    def _shown(self) -> None:
        on = self.kind.currentData() == "layer"
        self.layer_row.setVisible(on)
        self.note.setVisible(on)
        if on:
            self.note.setText(self._layer_note())

    def _layer_note(self) -> str:
        problem = self.problem()
        if problem:
            return problem
        layer = self.layer.currentLayer()
        n = layer.selectedFeatureCount() if self.selected_only.isChecked() else layer.featureCount()
        text = (self.tr("今の地物 {n} 件の形を監視に写して保存。").format(n=n) if n >= 0 else self.tr("今の地物の形を監視に写して保存。")) + \
            self.tr("あとでレイヤーを編集しても監視の範囲は変わらない")
        if self.later:
            text += self.tr("（「通知」の「監視条件」タブの「範囲の変更…」で取り直し）")
        return text

    def problem(self) -> Optional[str]:
        """Why the layer chosen gives no area; None when it does (or another area is chosen)."""
        if self.kind.currentData() != "layer":
            return None
        layer = self.layer.currentLayer()
        if layer is None:
            return self.tr("範囲のレイヤーが未選択")
        if self.selected_only.isChecked() and not layer.selectedFeatureCount():
            return self.tr("「{name}」に選択地物なし").format(name=layer.name())
        if not layer.featureCount():
            return self.tr("「{name}」に形のある地物なし").format(name=layer.name())
        return None

    def chosen(self) -> tuple[list[dict], Optional[dict[str, Any]]]:
        """The areas chosen (WGS 84; empty: anywhere) and, for a layer, where they came from (core.catalog.watch.layer_origin)."""
        kind = self.kind.currentData()
        if kind == "extent":
            return extent_area(self.canvas), None
        if kind == "drawn" and self.drawn:
            return areas_of([], [[self.drawn]]), None
        if kind == "layer":
            return layer_watch_areas(self.layer.currentLayer(), self.selected_only.isChecked())
        return [], None


class _AreaDialog(QDialog):
    """A dialog with an AreaChoice that is not accepted while its layer gives no area."""

    choice: AreaChoice
    _chosen: Optional[tuple[list[dict], Optional[dict[str, Any]]]] = None

    def accept(self) -> None:
        problem = self.choice.problem()
        chosen = None if problem else self.choice.chosen()
        if chosen is not None and self.choice.kind.currentData() == "layer" and not chosen[0]:
            layer = self.choice.layer.currentLayer()
            problem = self.tr("「{name}」に形のある地物なし").format(name=layer.name() if layer else "")
        if problem:
            self.choice.note.setText(problem)
            self.choice.note.setVisible(True)
            return
        if chosen is not None and _vertices(chosen[0]) > MANY_VERTICES and not getattr(self, "_warned", False):
            # Sent with every check: said once, accepted when OK is pressed again.
            self._warned = True
            self.choice.note.setText(self.tr("頂点が {n} 個と多い形。確認のたびにこの形をサーバーへ送る（受け付けられないときは「失敗の理由」に表示）。"
                                             "よければもう一度 OK").format(n=_vertices(chosen[0])))
            return
        self._chosen = chosen
        super().accept()

    def chosen(self) -> tuple[list[dict], Optional[dict[str, Any]]]:
        """The areas and their layer, as they were when OK was pressed."""
        return self._chosen if self._chosen is not None else self.choice.chosen()

    def areas(self) -> list[dict]:
        return self.chosen()[0]

    def area_layer(self) -> Optional[dict[str, Any]]:
        return self.chosen()[1]


class WatchDialog(_AreaDialog):
    """「この条件で監視」: the watch's name and area."""

    def __init__(self, catalog: CatalogConfig, conditions: CatalogSearch, canvas, layer: Optional[QgsMapLayer], parent=None,
                 drawn: Optional[list] = None, start: Optional[str] = None, selected_only: Optional[bool] = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("新着画像の監視"))
        self.catalog = catalog
        self.conditions = conditions
        self.canvas = canvas
        self.drawn = drawn
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(conditions_text(catalog, conditions, [], with_areas=False))
        form.addRow(self.tr("名前"), self.name)
        self.choice = AreaChoice(canvas, drawn, start, layer, selected_only)
        self.area = self.choice.kind
        form.addRow(self.tr("範囲"), self.choice)
        layout.addLayout(form)
        note = plain_label(QLabel(self.tr("条件: {text}").format(text=conditions_text(catalog, conditions, [], with_areas=False))))
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel(self.tr("初回の確認は既存の画像の記録のみ。以降の新着画像を通知")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class WatchAreaDialog(_AreaDialog):
    """「範囲の変更…」: a watch's area replaced (a layer's shape taken again, the map's extent, …)."""

    def __init__(self, watch: CatalogWatch, canvas, drawn: Optional[list] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("監視の範囲の変更"))
        layout = QVBoxLayout(self)
        now = plain_label(QLabel(self.tr("監視「{name}」の今の範囲: {text}").format(
            name=watch.name, text=conditions_text(watch.catalog, watch.conditions, watch.areas, area_layer=watch.area_layer))))
        now.setWordWrap(True)
        layout.addWidget(now)
        origin = watch.area_layer
        layer = origin_layer(origin)
        form = QFormLayout()
        self.choice = AreaChoice(canvas, drawn, "layer" if origin else None if watch.areas else "none", layer,
                                 bool(origin.get("selectedOnly")) if origin and layer is not None else None, later=False)
        if origin and layer is None:
            # Taken from a layer no longer in the project: another one is to be chosen.
            self.choice.kind.setCurrentIndex(self.choice.kind.findData("layer"))
            self.choice.note.setText(self.tr("元のレイヤー「{name}」がプロジェクトになし。取り直すレイヤーを選択").format(name=origin.get("name") or ""))
        self.area = self.choice.kind
        form.addRow(self.tr("新しい範囲"), self.choice)
        layout.addLayout(form)
        layout.addWidget(QLabel(self.tr("新しい範囲に今ある画像は通知しない。以降の新着画像を通知")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


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
        #: The catalog last chosen is offered first (入力の初期値の記憶).
        self.memory = InputMemory("point_watch", {"catalog": self.catalog})
        self.memory.restore()
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

    def accept(self) -> None:
        if self.new.isChecked():
            self.memory.save()
        super().accept()

    def chosen_watch(self) -> Optional[str]:
        """The watch to add the point to; None for a new one."""
        return self.watch.currentData() if self.add.isChecked() else None

    def chosen_catalog(self) -> Optional[CatalogConfig]:
        i = self.catalog.currentIndex()
        return self.catalogs[i] if 0 <= i < len(self.catalogs) else None
