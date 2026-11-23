"""
The images of 観測記録 listed without their files (the catalog's 「ファイルなしで
記録」, ＋) as layers of their own: one memory layer per image with its
footprint, in the group 「ファイルなしの画像」 at the top of the layer list
(tsudo 2026-11-18: 画像無しで記録している画像、仮想レイヤーでフットプリント
だけでも追加したほうがわかりやすい。これを消すことで、観測記録の方からも外れる).

The layers follow the list (the book's `listed`, kept per store in
QgsSettings): added, renamed and removed with it, never saved as work of
the project (QGIS does not offer to keep them, the project is not marked
changed by them). Removing one by hand (or the group) takes its image off
the list (`removed`), as closing an opened image's layer does; removing
every layer when the project is cleared or another is read does not, the
layers come back once it is done.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from qgis.core import (
    QgsFeature, QgsFillSymbol, QgsGeometry, QgsLayerTreeGroup, QgsMapLayer, QgsPalLayerSettings, QgsProject, QgsRectangle,
    QgsSingleSymbolRenderer, QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtGui import QColor

from ..core.obs.api import ListedImage
from .geometry import rings_geometry
from .pinned_layers import top_index
from .plan_layer import make_field, qdatetime

#: The group the layers go in (at the top of the layer list).
GROUP = "ファイルなしの画像"
#: Each layer's custom property: its image's id (the store's label in STORE_PROPERTY).
IMAGE_PROPERTY = "mikage/listed_image"
STORE_PROPERTY = "mikage/listed_store"
#: The outline's colour: the image recorded on now drawn wider and solid, the others thin and dashed.
COLOR = "#7c3aed"
WIDTH, CURRENT_WIDTH = "0.5", "1.0"
#: The layer name's word for an image with no footprint (typed with ＋ without 範囲).
NO_AREA = "（範囲なし）"


def footprint_geometry(image: ListedImage) -> Optional[QgsGeometry]:
    """The image's footprint (WGS 84): the catalog's polygon, else its box; None without either."""
    if image.rings:
        g = rings_geometry(image.rings)
        if not g.isNull() and not g.isEmpty():
            return g
    if image.box:
        west, south, east, north = image.box
        return QgsGeometry.fromRect(QgsRectangle(west, south, east, north))
    return None


def layer_name(image: ListedImage) -> str:
    return image.name if image.rings or image.box else image.name + NO_AREA


def footprint_layer(image: ListedImage, store: str = "") -> QgsVectorLayer:
    """A memory layer (WGS 84) of one image's footprint: its id, name and time as the attributes, the outline labelled with its name."""
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", layer_name(image), "memory")
    provider = layer.dataProvider()
    provider.addAttributes([make_field("image_id", "string"), make_field("name", "string"), make_field("acquired", "date")])
    layer.updateFields()
    for i, alias in enumerate(("画像 ID", "名称", "撮像日時")):
        layer.setFieldAlias(i, alias)
    feature = QgsFeature(layer.fields())
    g = footprint_geometry(image)
    if g is not None:
        if not g.isMultipart():
            g.convertToMultiType()
        feature.setGeometry(g)
    feature.setAttributes([image.id, image.name, qdatetime(image.time) if image.time is not None else None])
    provider.addFeatures([feature])
    layer.updateExtents()
    layer.setCustomProperty(IMAGE_PROPERTY, image.id)
    layer.setCustomProperty(STORE_PROPERTY, store)
    # Temporary: QGIS need not offer to keep it when the project closes; not edited by hand (removed only).
    layer.setCustomProperty("skipMemoryLayersCheck", 1)
    layer.setReadOnly(True)
    layer.setAbstract("観測記録のファイルなしの画像（MIKAGE）。このレイヤーを削除すると、観測記録の画像の一覧からも除外（記録は残る）")
    _style(layer, False)
    _label(layer)
    return layer


def _style(layer: QgsVectorLayer, current: bool) -> None:
    c = QColor(COLOR)
    symbol = QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": f"{c.red()},{c.green()},{c.blue()}",
                                         "outline_width": CURRENT_WIDTH if current else WIDTH,
                                         "outline_style": "solid" if current else "dash"})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))


def _label(layer: QgsVectorLayer) -> None:
    """The name outside the footprint (its inside, where the records are clicked, stays clear)."""
    text = QgsTextFormat()
    text.setSize(9)
    text.setColor(QColor(COLOR).darker(140))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setColor(QColor("#ffffff"))
    buffer.setSize(1.0)
    text.setBuffer(buffer)
    labels = QgsPalLayerSettings()
    labels.fieldName = "name"
    labels.setFormat(text)
    try:
        from qgis.core import Qgis

        labels.placement = Qgis.LabelPlacement.OutsidePolygons
    except AttributeError:  # before QGIS 3.26: along the outline
        labels.placement = QgsPalLayerSettings.Line
    layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    layer.setLabelsEnabled(True)


def listed_image_of(layer: Optional[QgsMapLayer]) -> Optional[str]:
    """The id of the image without its file a layer is the footprint of (None for any other layer)."""
    if not isinstance(layer, QgsVectorLayer):
        return None
    value = layer.customProperty(IMAGE_PROPERTY)
    return value if isinstance(value, str) and value else None


class ListedLayers(QObject):
    """
    Keeps the project's footprint layers the same as the list (`show`), and
    tells `removed` (the image ids) when some are removed by hand.
    """

    def __init__(self, parent: QObject, removed: Callable[[list[str]], None], project: Optional[QgsProject] = None):
        super().__init__(parent)
        self.project = project or QgsProject.instance()
        self.removed = removed
        #: The layers shown, by image id (the layer's id).
        self.layers: dict[str, str] = {}
        #: The images asked for last (the list object itself: a new one each time the book changes it), the store and the current one.
        self._images: Sequence[ListedImage] = []
        self._store = ""
        self._current: Optional[str] = None
        self._shown: Optional[tuple] = None
        #: Removing the layers ourselves (not by hand), or the project clearing every layer: the image stays listed.
        self._quiet = 0
        self._closed = False
        #: Every layer being removed (removeAll) until the event loop runs again, the project being cleared
        #: (aboutToBeCleared … cleared), and another project's layers being read (loadingLayer … readProject):
        #: the layers are not touched meanwhile (removing one while QGIS reads them crashes it).
        self._clearing = False
        self._in_clear = False
        self._reading = False
        self._hooks = [(self.project.layersWillBeRemoved, self._layers_going), (self.project.removeAll, self._all_going),
                       (self.project.layersRemoved, self._layers_gone), (self.project.aboutToBeCleared, self._clear_starts),
                       (self.project.cleared, self._clear_ends), (self.project.loadingLayer, self._layer_reading),
                       (self.project.readProject, self._project_read)]
        for signal, slot in self._hooks:
            signal.connect(slot)

    # ---- following the list ----------------------------------------------

    def show(self, images: Sequence[ListedImage], current: Optional[str] = None, store: str = "") -> None:
        """
        The layers of these images (the book's `listed`), the current one's
        outline wider. Nothing done when they are the same list as before
        (the book makes a new list for each change).
        """
        self._images, self._current, self._store = images, current, store
        # Asked again while changing the layers (the dock follows the layers added): done once this change ends.
        if self._clearing or self._closed or self._quiet:
            return
        while self._shown is None or self._shown[0] is not self._images or self._shown[1] != self._store:
            images, store = self._images, self._store
            self._sync(images, store)
            self._shown = (images, store, None)
        if self._shown[2] != self._current:
            self._mark(self._current)
            self._shown = (self._shown[0], self._shown[1], self._current)

    def layer_of(self, image_id: Optional[str]) -> Optional[QgsVectorLayer]:
        layer = self.project.mapLayer(self.layers.get(image_id or "", ""))
        return layer if isinstance(layer, QgsVectorLayer) else None

    def layer_ids(self) -> set[str]:
        return set(self.layers.values())

    def _sync(self, images: Sequence[ListedImage], store: str) -> None:
        wanted = {i.id: i for i in images}
        # Layers of the project not ours (another store's, one read with the project, a plugin loaded again): removed.
        ours = set(self.layers.values())
        stale = [layer.id() for layer in self.project.mapLayers().values() if listed_image_of(layer) and layer.id() not in ours]
        for image_id, layer_id in list(self.layers.items()):
            layer = self.project.mapLayer(layer_id)
            image = wanted.get(image_id)
            if layer is None or image is None or layer.customProperty(STORE_PROPERTY) != store:
                stale.append(layer_id)
                del self.layers[image_id]
            elif not _same(layer, image):
                stale.append(layer_id)
                del self.layers[image_id]
        new = [footprint_layer(i, store) for i in images if i.id not in self.layers]
        if not stale and not new:
            return
        self._quietly(lambda: self._replace(stale, new))

    def _replace(self, stale: list[str], new: list[QgsVectorLayer]) -> None:
        if stale:
            self.project.removeMapLayers([x for x in stale if self.project.mapLayer(x) is not None])
        root = self.project.layerTreeRoot()
        group = root.findGroup(GROUP)
        if new:
            if group is None:
                group = root.insertGroup(top_index(root), GROUP)
                group.setExpanded(True)
            for layer in new:
                self.project.addMapLayer(layer, False)
                group.addLayer(layer)
                self.layers[layer.customProperty(IMAGE_PROPERTY)] = layer.id()
        if group is not None and not group.children():
            root.removeChildNode(group)

    def _mark(self, current: Optional[str]) -> None:
        for image_id, layer_id in self.layers.items():
            layer = self.project.mapLayer(layer_id)
            if isinstance(layer, QgsVectorLayer):
                _style(layer, image_id == current)
                layer.triggerRepaint()

    def _quietly(self, change: Callable[[], None]) -> None:
        """A change of our own: the image stays listed, and the project is not marked changed (the layers are not its work)."""
        dirty = self.project.isDirty()
        self._quiet += 1
        try:
            change()
        finally:
            self._quiet -= 1
            if not dirty and self.project.isDirty():
                self.project.setDirty(False)

    # ---- the project's signals --------------------------------------------

    def _layers_going(self, layer_ids) -> None:
        if self._quiet or self._clearing:
            return
        going = set(layer_ids)
        gone = [image_id for image_id, layer_id in self.layers.items() if layer_id in going]
        if not gone:
            return
        for image_id in gone:
            del self.layers[image_id]
        self._shown = None
        self.removed(gone)

    def _all_going(self) -> None:
        # Every layer removed at once (the project cleared: another one read, a new one, QGIS closing): the images stay listed.
        self._clearing = True

    def _clear_starts(self) -> None:
        self._clearing = self._in_clear = True

    def _layers_gone(self, _layer_ids) -> None:
        # Every layer removed but the project not cleared: the layers back once the event loop runs.
        if self._clearing and not self._in_clear:
            QTimer.singleShot(0, self._after_clear)

    def _clear_ends(self) -> None:
        self._in_clear = False
        QTimer.singleShot(0, self._after_clear)

    def _layer_reading(self, *_args) -> None:
        if self._clearing:
            self._reading = True

    def _after_clear(self) -> None:
        # A project being read (QGIS may run the event loop while it reads the layers): its readProject ends it.
        if self._clearing and not self._in_clear and not self._reading:
            self._done_clearing()

    def _project_read(self, *_args) -> None:
        # Layers of ours saved with the project are read back empty (another store's, or gone from the list): replaced.
        self._reading = self._in_clear = False
        self._done_clearing()

    def _done_clearing(self) -> None:
        self._clearing = False
        self.layers = {k: v for k, v in self.layers.items() if self.project.mapLayer(v) is not None}
        self._shown = None
        QTimer.singleShot(0, self._again)

    def _again(self) -> None:
        if not self._clearing and self._shown is None:
            self.show(self._images, self._current, self._store)

    def close(self) -> None:
        """The plugin unloads: the layers go (the images stay listed, for the next time)."""
        self._closed = True
        for signal, slot in self._hooks:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        ids = [x for x in self.layers.values() if self.project.mapLayer(x) is not None]
        self.layers = {}
        if ids:
            self._quietly(lambda: self._replace(ids, []))


def _same(layer: QgsVectorLayer, image: ListedImage) -> bool:
    """Whether a layer still shows an image as it is listed (its name, time and footprint)."""
    if layer.name() != layer_name(image):
        return False
    feature = next(layer.getFeatures(), None)
    if feature is None:
        return False
    acquired = feature["acquired"]
    ms = acquired.toMSecsSinceEpoch() if acquired is not None and hasattr(acquired, "toMSecsSinceEpoch") and acquired.isValid() else None
    if ms != image.time or feature["name"] != image.name:
        return False
    g = footprint_geometry(image)
    have = feature.geometry()
    if g is None:
        return have.isNull() or have.isEmpty()
    if not g.isMultipart():
        g.convertToMultiType()
    return not have.isNull() and have.equals(g)
