"""
個人の関心地域・地点 in QGIS (core/interests.py): the person's own items as
read from Esri, kept once for the plugin (`InterestBook`: the dock edits them,
the 新着画像 cards name the ones an image covers), and shown on the map as two
memory layers (「関心地点」「関心地域」, each item in its colour, labelled).

The cards' 「写っている地物」 for these is worked out here, not asked of Esri
per image: a person has tens of items, not thousands, and an edit shows on
every card at once (nothing kept to go stale).
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

from qgis.core import (
    QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsMarkerSymbol, QgsPalLayerSettings, QgsPointXY, QgsProject,
    QgsProperty, QgsRectangle, QgsSingleSymbolRenderer, QgsSymbolLayer, QgsTextBufferSettings, QgsTextFormat,
    QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QObject, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..core import edit_stamp
from ..core.interests import (
    AREA, KINDS, POINT, Interest, InterestSettings, delete_interests, load_interests, save_interest,
)
from .catalog_io import footprint
from .tasks import run_task

#: The custom property that marks the plugin's two layers (its value: the kind).
LAYER_PROPERTY = "mikage/interests"
LAYER_NAMES = {POINT: "関心地点", AREA: "関心地域"}
#: A failed read is tried again after this long when asked (the cards ask as they come into view).
RETRY_S = 600


def geometry_of(item: Interest) -> Optional[QgsGeometry]:
    """An item's shape as a QGIS geometry (WGS 84); None without one."""
    if not item.has_shape:
        return None
    if item.kind == POINT:
        return QgsGeometry.fromPointXY(QgsPointXY(item.x, item.y))
    return footprint({"rings": item.rings})


class InterestBook(QObject):
    """The person's points and areas (`items`: None until read), read, saved and deleted in tasks."""

    #: The items were read, saved or deleted, or the settings file changed.
    changed = pyqtSignal()

    def __init__(self, context, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.context = context
        self.items: Optional[list[Interest]] = None
        #: Why the last read failed ("" when it did not).
        self.failure = ""
        self.failed_at = 0.0
        self.loading = False
        self._again = False
        self._then: list[Callable[[], None]] = []
        self._geometries: dict[str, Optional[QgsGeometry]] = {}
        self._tasks: list = []
        context.configChanged.connect(self._config_changed)

    @property
    def settings(self) -> Optional[InterestSettings]:
        return getattr(self.context.config, "interests", None)

    @staticmethod
    def user() -> str:
        return edit_stamp.user_name()

    def _tokens(self, s: InterestSettings) -> dict:
        return {k: self.context.token_for(s.source(k).url, s.source(k).token) for k in s.kinds}

    def _config_changed(self) -> None:
        self.items, self.failure, self.failed_at = None, "", 0.0
        self._geometries = {}
        if self.loading:
            self._again = True
        self.changed.emit()

    def _start(self, description: str, work, done, failed) -> None:
        task = run_task(description, work, done, failed)
        self._tasks.append(task)
        for signal in (task.taskCompleted, task.taskTerminated):
            signal.connect(lambda task=task: self._tasks.remove(task) if task in self._tasks else None)

    # ---- reading -------------------------------------------------------------------------------

    def want(self) -> None:
        """Read when not read yet (and not failed a moment ago): what the cards and the dock call as they are shown."""
        if self.settings is None or self.items is not None or self.loading:
            return
        if self.failure and time.time() - self.failed_at < RETRY_S:
            return
        self.load()

    def load(self, then: Optional[Callable[[], None]] = None) -> None:
        """Reads the person's items again (in a task); `then` once read."""
        s = self.settings
        if s is None:
            return
        if then is not None:
            self._then.append(then)
        if self.loading:
            self._again = True
            return
        self.loading, self._again = True, False
        client, user, tokens = self.context.esri(), self.user(), self._tokens(s)

        def work(_task):
            return load_interests(client, s, user, tokens)

        def done(items) -> None:
            self.loading = False
            if s is not self.settings or self._again:
                self.load()
                return
            self.items, self.failure = list(items), ""
            self._geometries = {}
            waiting, self._then = self._then, []
            self.changed.emit()
            for f in waiting:
                f()

        def failed(message: str) -> None:
            self.loading = False
            if s is not self.settings or self._again:
                self.load()
                return
            self.failure, self.failed_at, self._then = message, time.time(), []
            self.changed.emit()

        self.changed.emit()  # 「読み込み中…」
        self._start("関心地域・地点の読み込み", work, done, failed)

    # ---- writing -------------------------------------------------------------------------------

    def save(self, item: Interest, done: Optional[Callable[[Interest], None]] = None,
             failed: Optional[Callable[[str], None]] = None) -> None:
        s = self.settings
        if s is None:
            if failed:
                failed("設定ファイルに interests なし")
            return
        client, user = self.context.esri(), self.user()
        token = self._tokens(s).get(item.kind)

        def work(_task):
            return save_interest(client, s, item, user, token)

        def finished(saved: Interest) -> None:
            if self.items is not None and s is self.settings:
                self.items = sorted([i for i in self.items if i.id != saved.id] + [saved], key=lambda i: (i.name.casefold(), i.kind, i.id))
                self._geometries.pop(saved.id, None)
                self.changed.emit()
            if done:
                done(saved)

        self._start("関心地域・地点の保存", work, finished, failed or (lambda _m: None))

    def delete(self, items: Sequence[Interest], done: Optional[Callable[[list, list], None]] = None,
               failed: Optional[Callable[[str], None]] = None) -> None:
        s = self.settings
        if s is None or not items:
            return
        client, tokens, items = self.context.esri(), self._tokens(s), list(items)

        def work(_task):
            return delete_interests(client, s, items, tokens)

        def finished(result) -> None:
            gone, not_gone = result
            if self.items is not None and s is self.settings:
                self.items = [i for i in self.items if i.id not in set(gone)]
                self.changed.emit()
            if done:
                done(gone, not_gone)

        self._start("関心地域・地点の削除", work, finished, failed or (lambda _m: None))

    # ---- looking -------------------------------------------------------------------------------

    def find(self, item_id: str) -> Optional[Interest]:
        return next((i for i in self.items or [] if i.id == item_id), None)

    def geometry(self, item: Interest) -> Optional[QgsGeometry]:
        if item.id not in self._geometries:
            self._geometries[item.id] = geometry_of(item)
        return self._geometries[item.id]

    def over(self, esri_geometry: Optional[dict]) -> list[Interest]:
        """The items an image's footprint (Esri JSON, WGS 84) covers or crosses."""
        shape = footprint(esri_geometry) if self.items else None
        if shape is None or shape.isEmpty():
            return []
        box = shape.boundingBox()
        out = []
        for item in self.items or []:
            g = self.geometry(item)
            if g is not None and box.intersects(g.boundingBox()) and shape.intersects(g):
                out.append(item)
        return out


# ---- the map layers --------------------------------------------------------------------------------


def layers_of(project: Optional[QgsProject] = None) -> dict[str, QgsVectorLayer]:
    """The plugin's 「関心地点」「関心地域」 layers in the project, by kind."""
    project = project or QgsProject.instance()
    out = {}
    for layer in project.mapLayers().values():
        kind = layer.customProperty(LAYER_PROPERTY)
        if kind in KINDS and isinstance(layer, QgsVectorLayer) and kind not in out:
            out[kind] = layer
    return out


def _style(layer: QgsVectorLayer, kind: str, default: str) -> None:
    colour = f"coalesce(\"color\", '{default}')"
    # The legend shows the symbol's own colour: the default one (each item drawn in its own).
    base = QColor(default)
    rgb = f"{base.red()},{base.green()},{base.blue()}"
    if kind == POINT:
        symbol = QgsMarkerSymbol.createSimple({"name": "circle", "size": "3.6", "color": rgb, "outline_color": "#ffffff", "outline_width": "0.5"})
        symbol.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.PropertyFillColor, QgsProperty.fromExpression(colour))
    else:
        symbol = QgsFillSymbol.createSimple({"color": rgb + ",50", "outline_color": rgb, "outline_width": "0.7"})
        symbol.symbolLayer(0).setDataDefinedProperty(
            QgsSymbolLayer.PropertyFillColor, QgsProperty.fromExpression(f"set_color_part({colour}, 'alpha', 50)"))
        symbol.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.PropertyStrokeColor, QgsProperty.fromExpression(colour))
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    text = QgsTextFormat()
    text.setSize(9)
    text.setColor(QColor("#111111"))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setColor(QColor("#ffffff"))
    buffer.setSize(1.0)
    text.setBuffer(buffer)
    labels = QgsPalLayerSettings()
    labels.fieldName = "name"
    labels.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    layer.setLabelsEnabled(True)


def _new_layer(kind: str, default: str) -> QgsVectorLayer:
    geometry = "Point" if kind == POINT else "MultiPolygon"
    layer = QgsVectorLayer(f"{geometry}?crs=EPSG:4326", LAYER_NAMES[kind], "memory")
    layer.dataProvider().addAttributes([QgsField(n, QVariant.String) for n in ("id", "name", "color", "note")])
    layer.updateFields()
    for i, alias in enumerate(("ID", "名前", "色", "メモ")):
        layer.setFieldAlias(i, alias)
    layer.setCustomProperty(LAYER_PROPERTY, kind)
    # Read from Esri each time: QGIS need not offer to keep them when the project closes.
    layer.setCustomProperty("skipMemoryLayersCheck", 1)
    layer.setReadOnly(True)
    layer.setAbstract("個人の関心地域・地点（MIKAGE。編集はドック「関心地域・地点」で。Esri から読み直す）")
    _style(layer, kind, default)
    return layer


def show_layers(items: Sequence[Interest], settings: InterestSettings, project: Optional[QgsProject] = None,
                add: bool = True) -> dict[str, QgsVectorLayer]:
    """
    The layers (made and put at the top of the layer list when missing and
    `add`) holding the items as they are now; by kind.
    """
    project = project or QgsProject.instance()
    layers = layers_of(project)
    for kind in settings.kinds:
        layer = layers.get(kind)
        if layer is None:
            if not add:
                continue
            layer = _new_layer(kind, settings.color)
            project.addMapLayer(layer, False)
            project.layerTreeRoot().insertLayer(0, layer)
            layers[kind] = layer
        fill(layer, [i for i in items if i.kind == kind])
    return layers


def fill(layer: QgsVectorLayer, items: Sequence[Interest]) -> dict[str, int]:
    """The layer's features made the items (its feature ids by item id)."""
    provider = layer.dataProvider()
    provider.truncate()
    features = []
    for item in items:
        g = geometry_of(item)
        if g is None:
            continue
        if item.kind == AREA and not g.isMultipart():
            g.convertToMultiType()
        f = QgsFeature(layer.fields())
        f.setAttributes([item.id, item.name, item.color, item.note])
        f.setGeometry(g)
        features.append(f)
    provider.addFeatures(features)
    layer.updateExtents()
    layer.triggerRepaint()
    return {f["id"]: f.id() for f in layer.getFeatures()}


def remove_layers(project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    ids = [layer.id() for layer in layers_of(project).values()]
    if ids:
        project.removeMapLayers(ids)


def select_on_map(item_ids: Sequence[str], project: Optional[QgsProject] = None) -> None:
    """The items chosen in the dock, selected in the layers (so QGIS shows them in its selection colour)."""
    wanted = set(item_ids)
    for layer in layers_of(project).values():
        layer.selectByIds([f.id() for f in layer.getFeatures() if f["id"] in wanted])


def box_of(items: Sequence[Interest]) -> Optional[QgsRectangle]:
    """The items' extent (WGS 84); a point alone is a box of ~1 km."""
    box: Optional[QgsRectangle] = None
    for item in items:
        g = geometry_of(item)
        if g is None:
            continue
        b = g.boundingBox()
        if b.width() == 0 and b.height() == 0:
            p = b.center()
            b = QgsRectangle(p.x() - 0.005, p.y() - 0.005, p.x() + 0.005, p.y() + 0.005)
        if box is None:
            box = QgsRectangle(b)
        else:
            box.combineExtentWith(b)
    return box
