"""
「クリック抽出（AI）」 (the viewer's SegmentTool): a Segment Anything model
(SAM, MobileSAM…, exported to ONNX as an encoder and a decoder) outlines
the object under a click on an image. Shift + click takes in more of the
last object, Alt + click leaves a part of it out; 「1 つ取り消し」 takes the
last click back, 「レイヤー化」 makes the outlines a temporary layer, Esc or
「終了」 puts the tool away.

The image is the active layer when it is a raster, else the top visible
raster, drawn as the map shows it now; the encoder sees it once per view
(again when the map moves), each click only runs the light decoder. Models
load and run in QGIS's tasks, so the map stays free; clicks made meanwhile
wait their turn. The panel sits over the map, as the viewer's.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np
from qgis.core import (
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsLayerTreeLayer, QgsMapLayer, QgsMapRendererParallelJob, QgsMapSettings, QgsMapToPixel,
    QgsPointXY, QgsProject, QgsRasterLayer, QgsSingleSymbolRenderer, QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import QDateTime, QObject, Qt, QVariant
from qgis.PyQt.QtGui import QColor, QImage
from qgis.PyQt.QtWidgets import QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..core.ai.mask_outline import mask_outline, ring_area
from ..core.ai.model_config import SamModelConfig
from ..core.ai.sam import SAM_MEAN, SAM_STD, SamPoint, part_at, sam_hwc, sam_mask, sam_prompt, sam_scale, sam_tensor
from ..processing_provider.detect_algorithm import image_info, outline_symbol, source_fields
from ..qgis_io.ai_io import image_rgba
from ..qgis_io.ai_runtime import AiError, LoadedModel, local_model, model_from, name_of
from ..qgis_io.tasks import run_task

PICK = "pick"
OUTLINE = QColor(0, 229, 255)


def split_pair(paths: list[str]) -> tuple[str, str]:
    """An encoder and a decoder chosen together, in either order: by name (…encoder… / …decoder…), else the larger file is the encoder."""
    if len(paths) != 2:
        raise AiError("エンコーダーとデコーダーの 2 つの .onnx を一緒に選択")
    a, b = paths
    na, nb = os.path.basename(a).lower(), os.path.basename(b).lower()
    sizes = [os.path.getsize(p) if os.path.isfile(p) else 0 for p in (a, b)]
    encoder_first = "encoder" in na or "decoder" in nb or ("encoder" not in na + nb and "decoder" not in na + nb and sizes[0] >= sizes[1])
    return (a, b) if encoder_first else (b, a)


@dataclass
class SamImage:
    """The encoder's view of one drawn picture, kept while the view stays put."""
    key: tuple
    to_pixel: QgsMapToPixel
    width: int
    height: int
    scale: float
    side: int
    embeddings: Any


@dataclass
class SamObject:
    """An object being outlined: its clicked points, its outline (map rings) and how it is drawn."""
    points: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    rings: Optional[list] = None
    band: Optional[QgsRubberBand] = None


class SegmentMapTool(QgsMapTool):
    def __init__(self, canvas, tool: "SegmentTool"):
        super().__init__(canvas)
        self.tool = tool
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 (QGIS's name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        modifiers = event.modifiers()
        self.tool.click(event.mapPoint(), extend=bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
                        exclude=bool(modifiers & Qt.KeyboardModifier.AltModifier))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.tool.set_active(False)
            event.accept()
        else:
            event.ignore()


class SegmentPanel(QFrame):
    """The tool's panel over the map: the model, what is happening, and the buttons."""

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.setObjectName("SatObsSegmentPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        top = QHBoxLayout()
        title = QLabel(self.tr("<b>クリック抽出（AI）</b>"))
        top.addWidget(title)
        top.addWidget(QLabel(self.tr("モデル")))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(180)
        top.addWidget(self.model_combo, 1)
        layout.addLayout(top)
        hint = QLabel(self.tr("画像の物体のクリックで輪郭を抽出。Shift+クリックで範囲を拡張、Alt+クリックで除外。"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        self.undo_button = QPushButton(self.tr("1 つ取り消し"))
        self.keep_button = QPushButton(self.tr("レイヤー化"))
        self.keep_button.setDefault(True)
        self.close_button = QPushButton(self.tr("終了"))
        for b in (self.undo_button, self.keep_button, self.close_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.setMaximumWidth(420)
        self.hide()


class SegmentTool(QObject):
    """The tool: its toggle `action`, the map tool, the panel, the models and the outlines being made."""

    def __init__(self, iface, context, action, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.iface = iface
        self.context = context
        self.canvas: QgsMapCanvas = iface.mapCanvas()
        self.action = action
        action.setCheckable(True)
        action.toggled.connect(self.set_active)
        self.active = False
        self.tool = SegmentMapTool(self.canvas, self)
        self.panel = SegmentPanel(self.canvas)
        self.panel.model_combo.activated.connect(self._combo_activated)
        self.panel.undo_button.clicked.connect(self.undo)
        self.panel.keep_button.clicked.connect(lambda: self.keep())
        self.panel.close_button.clicked.connect(lambda: self.set_active(False))
        self.files: list[tuple[str, str, str]] = []
        self.image: Optional[SamImage] = None
        self.objects: list[SamObject] = []
        self._previous_tool = None
        self._queue: list[Callable[[], None]] = []
        self._busy = False
        #: The last layer made (for scripts and tests).
        self.layer: Optional[QgsVectorLayer] = None
        self.canvas.mapToolSet.connect(self._tool_set)
        context.configChanged.connect(self.fill_models)
        self.fill_models()
        self._update()

    # The models.

    def configs(self) -> list[SamModelConfig]:
        return [m for m in self.context.config.ai_models if isinstance(m, SamModelConfig)]

    def fill_models(self) -> None:
        combo = self.panel.model_combo
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for i, m in enumerate(self.configs()):
            combo.addItem(m.label, ("config", i))
        for i, (label, _, _) in enumerate(self.files):
            combo.addItem(f"📄 {label}", ("file", i))
        if combo.count() == 0:
            combo.addItem(self.tr("（モデルを選択）"), None)
        combo.addItem(self.tr("ファイルから選択…"), PICK)
        combo.setCurrentIndex(max(self._index_of(current), 0))
        combo.blockSignals(False)

    def _index_of(self, data) -> int:
        """The combo's item with `data` (findData does not match tuples), -1 when none."""
        combo = self.panel.model_combo
        return next((i for i in range(combo.count()) if data is not None and combo.itemData(i) == data), -1)

    def _combo_activated(self, _index: int) -> None:
        if self.panel.model_combo.currentData() == PICK:
            paths, _ = QFileDialog.getOpenFileNames(self.panel, self.tr("エンコーダーとデコーダーの ONNX"), "", "ONNX (*.onnx)")
            self.panel.model_combo.setCurrentIndex(0)
            if paths:
                try:
                    self.add_model_files(paths)
                except AiError as e:
                    self.say(str(e))
            return
        self.image = None
        self.prepare()

    def add_model_files(self, paths: list[str]) -> None:
        """Adds an encoder and a decoder chosen as files and chooses them."""
        encoder, decoder = split_pair(paths)
        self.files.append((" + ".join(os.path.basename(p) for p in paths), encoder, decoder))
        self.fill_models()
        self.panel.model_combo.setCurrentIndex(self._index_of(("file", len(self.files) - 1)))
        self.image = None
        self.prepare()

    def chosen(self) -> Optional[tuple[str, str, str, Optional[SamModelConfig], str]]:
        """The chosen model: its label, encoder, decoder (paths or URLs), settings and the settings file they are relative to."""
        data = self.panel.model_combo.currentData()
        if not isinstance(data, tuple):
            return None
        kind, i = data
        if kind == "config" and i < len(self.configs()):
            m = self.configs()[i]
            return m.label, m.encoder, m.decoder, m, self.context.source
        if kind == "file" and i < len(self.files):
            label, encoder, decoder = self.files[i]
            return label, encoder, decoder, None, ""
        return None

    @staticmethod
    def load(chosen, cancelled=lambda: False) -> tuple[LoadedModel, LoadedModel]:
        """The encoder and decoder, loaded (once; downloaded once when they are URLs). Off the main thread."""
        label, encoder, decoder, _, base = chosen
        return (model_from(local_model(encoder, base, cancelled=cancelled), f"{label}（エンコーダー）"),
                model_from(local_model(decoder, base, cancelled=cancelled), f"{label}（デコーダー）"))

    def prepare(self) -> None:
        """Loads the chosen model when the tool starts, so the first click is quicker."""
        chosen = self.chosen()
        if chosen is None:
            self.note(self.tr("Segment Anything（SAM・MobileSAM など）のエンコーダーとデコーダーの ONNX を選択"))
            return
        self.note(self.tr("モデルを読み込み中…"))

        def done(models) -> None:
            if self.active and self.chosen() == chosen:
                self.note(self.tr("準備完了（{backend}）。画像をクリック").format(backend=models[0].backend))

        def failed(message: str) -> None:
            if self.chosen() == chosen:
                self.note(self.tr("モデルの読み込みに失敗: {e}").format(e=message))

        self._enqueue("クリック抽出のモデルの読み込み", lambda task: self.load(chosen, task.isCanceled), done, failed)

    # On and off.

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        if self.action.isChecked() != active:
            self.action.blockSignals(True)
            self.action.setChecked(active)
            self.action.blockSignals(False)
        if active:
            if self.canvas.mapTool() is not self.tool:
                self._previous_tool = self.canvas.mapTool()
                self.canvas.setMapTool(self.tool)
            self.panel.move(10, 10)
            self.panel.adjustSize()
            self.panel.show()
            self.panel.raise_()
            self.prepare()
        else:
            self.panel.hide()
            self.clear()
            self.image = None
            if self.canvas.mapTool() is self.tool:
                try:
                    self.canvas.setMapTool(self._previous_tool) if self._previous_tool is not None else self.canvas.unsetMapTool(self.tool)
                except RuntimeError:  # the tool went with its plugin
                    self.canvas.unsetMapTool(self.tool)
        self._update()

    def _tool_set(self, new, old=None) -> None:
        if self.active and new is not self.tool:
            self.set_active(False)

    # The image.

    def target_layer(self) -> Optional[QgsRasterLayer]:
        """The image looked at: the active layer when it is a raster, else the top visible raster."""
        active = self.iface.activeLayer()
        if isinstance(active, QgsRasterLayer) and active.isValid():
            return active
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            layer = node.layer()
            if isinstance(layer, QgsRasterLayer) and layer.isValid() and node.isVisible() and layer in self.canvas.layers():
                return layer
        return None

    def draw(self, layer: QgsMapLayer) -> tuple[np.ndarray, QgsMapToPixel, tuple]:
        """The layer drawn alone as the map shows it now: its RGBA pixels, the pixels' placement, and what identifies the view."""
        settings = QgsMapSettings(self.canvas.mapSettings())
        settings.setLayers([layer])
        settings.setBackgroundColor(QColor(0, 0, 0, 0))
        settings.setDevicePixelRatio(1)
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        image: QImage = job.renderedImage()
        e = settings.visibleExtent()
        key = (layer.id(), round(e.xMinimum(), 6), round(e.yMinimum(), 6), round(e.xMaximum(), 6), round(e.yMaximum(), 6),
               settings.rotation(), settings.destinationCrs().authid(), image.width(), image.height())
        return image, QgsMapToPixel(settings.mapToPixel()), key

    # A click.

    def click(self, point: QgsPointXY, extend: bool = False, exclude: bool = False) -> None:
        """A click on the map: a new object, or (Shift / Alt) a point taken in or left out of the last one."""
        layer = self.target_layer()
        if layer is None:
            self.note(self.tr("画像なし（先に画像を開く）"))
            return
        chosen = self.chosen()
        if chosen is None:
            self.note(self.tr("先にモデルを選択"))
            return
        adding = (extend or exclude) and bool(self.objects)
        obj = self.objects[-1] if adding else SamObject()
        if not adding:
            self.objects.append(obj)
        marker = QgsVertexMarker(self.canvas)
        marker.setCenter(QgsPointXY(point))
        marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        marker.setIconSize(10)
        marker.setPenWidth(2)
        marker.setColor(QColor("#e74c3c") if exclude else QColor("#2ecc71"))
        marker.setFillColor(QColor("#e74c3c") if exclude else QColor("#2ecc71"))
        obj.markers.append(marker)
        obj.points.append((QgsPointXY(point), not exclude))
        self._update()
        self.outline(obj, layer, chosen)

    def outline(self, obj: SamObject, layer: QgsMapLayer, chosen) -> None:
        """Outlines `obj` from its points (replacing its outline), in a task after those before it."""
        def start() -> None:
            if obj not in self.objects or not obj.points:
                self._next()
                return
            image = self.image
            side = (chosen[3].input_size if chosen[3] and chosen[3].input_size else None) or 1024
            drawn = None
            try:
                picture, to_pixel, key = self.draw(layer)
            except Exception as e:  # noqa: BLE001
                self.note(self.tr("抽出失敗: {e}").format(e=e))
                self._next()
                return
            key = key + (chosen[1], chosen[2])
            if image is None or image.key != key:
                scale, w, h = sam_scale(picture.width(), picture.height(), side)
                small = picture.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
                drawn = (key, to_pixel, picture.width(), picture.height(), scale, side, image_rgba(small))
                self.note(self.tr("画像を解析中（エンコーダー）…"))
            else:
                self.note(self.tr("輪郭を抽出中…"))
            current = image if drawn is None else None
            points = []
            tp = to_pixel if drawn is None else drawn[1]
            for p, positive in obj.points:
                px = tp.transform(p)
                points.append(SamPoint(px.x(), px.y(), positive))
            started = time.monotonic()

            def work(task):
                encoder, decoder = self.load(chosen, task.isCanceled)
                img = current
                if img is None:
                    key_, to_pixel_, width, height, scale, side_, rgba = drawn
                    img = SamImage(key_, to_pixel_, width, height, scale, side_, encode(encoder, rgba, side_, chosen[3]))
                inside = [p for p in points if 0 <= p.x < img.width and 0 <= p.y < img.height]
                if not inside:
                    return img, None
                mask = decode(decoder, img, inside)
                # The part of the mask under the first point taken in: the object clicked, not others like it.
                first = next((p for p in inside if p.positive), inside[0])
                polygons = mask_outline(part_at(mask, first.x, first.y), 1)
                largest = max(polygons, key=lambda poly: abs(ring_area(poly[0]))) if polygons else None
                return img, largest

            def done(result) -> None:
                img, polygon = result
                self.image = img
                if obj in self.objects:
                    self._set_outline(obj, polygon, img.to_pixel)
                    self.note(self.tr("{n} 個を抽出（{s:.1f} 秒）。「レイヤー化」で一時レイヤーに変換").format(
                        n=self.count(), s=time.monotonic() - started) if obj.rings else self.tr("この点からの輪郭取得に失敗"))
                self._update()
                self._next()

            def failed(message: str) -> None:
                self.note(self.tr("抽出失敗: {e}").format(e=message))
                self._update()
                self._next()

            run_task(self.tr("クリック抽出"), work, done, failed)

        self._queue.append(start)
        self._pump()

    def _set_outline(self, obj: SamObject, polygon, to_pixel: QgsMapToPixel) -> None:
        if obj.band is not None:
            self.canvas.scene().removeItem(obj.band)
            obj.band = None
        obj.rings = None
        if not polygon:
            return
        rings = []
        for ring in polygon:
            points = [to_pixel.toMapCoordinatesF(float(x), float(y)) for x, y in ring]
            rings.append(points + points[:1])
        obj.rings = rings
        band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        band.setColor(OUTLINE)
        band.setFillColor(QColor(0, 229, 255, 46))
        band.setWidth(2)
        band.setToGeometry(QgsGeometry.fromPolygonXY(rings), self.canvas.mapSettings().destinationCrs())
        obj.band = band

    # The queue: one task at a time, in the order asked.

    def _enqueue(self, description: str, work, done, failed) -> None:
        def then(handle):
            def call(value) -> None:
                try:
                    handle(value)
                finally:
                    self._next()
            return call

        def start() -> None:
            run_task(self.tr(description), work, then(done), then(failed))

        self._queue.append(start)
        self._pump()

    def _pump(self) -> None:
        if self._busy or not self._queue:
            return
        self._busy = True
        self._queue.pop(0)()

    def _next(self) -> None:
        self._busy = False
        self._pump()

    def idle(self) -> bool:
        """Nothing loading or outlining (for tests)."""
        return not self._busy and not self._queue

    # Undo, keep, clear.

    def undo(self) -> None:
        """Removes the last click: the last object's last point (outlining it again from the rest), or the object."""
        if not self.objects:
            return
        obj = self.objects[-1]
        if obj.markers:
            self.canvas.scene().removeItem(obj.markers.pop())
        if obj.points:
            obj.points.pop()
        if not obj.points:
            self._set_outline(obj, None, None)
            self.objects.pop()
        else:
            layer, chosen = self.target_layer(), self.chosen()
            if layer is not None and chosen is not None:
                self.outline(obj, layer, chosen)
        self.note(self.tr("{n} 個を抽出中").format(n=self.count()))
        self._update()

    def keep(self) -> Optional[QgsVectorLayer]:
        """Makes the outlines a temporary layer (in the map's CRS) and starts again."""
        done = [o for o in self.objects if o.rings]
        if not done:
            return None
        chosen = self.chosen()
        label = chosen[0] if chosen else "SAM"
        crs = self.canvas.mapSettings().destinationCrs()
        layer = QgsVectorLayer(f"Polygon?crs={crs.authid() or 'EPSG:4326'}", self.tr("AI 抽出"), "memory")
        if not crs.authid():
            layer.setCrs(crs)
        fields = QgsFields()
        fields.append(QgsField("id", QVariant.Int))
        fields.append(QgsField("class", QVariant.String))
        source_fields(fields)
        provider = layer.dataProvider()
        provider.addAttributes(list(fields))
        layer.updateFields()
        fields = layer.fields()
        target = self.target_layer()
        image, image_id, image_time = image_info(target) if target is not None else (None, None, None)
        now = QDateTime.currentDateTimeUtc()
        features = []
        for i, o in enumerate(self.objects):
            if not o.rings:
                continue
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPolygonXY(o.rings))
            f.setAttributes([i + 1, None, image, image_id, image_time, label, now, None])
            features.append(f)
        provider.addFeatures(features)
        layer.updateExtents()
        layer.setRenderer(QgsSingleSymbolRenderer(outline_symbol()))
        QgsProject.instance().addMapLayer(layer, False)
        QgsProject.instance().layerTreeRoot().insertChildNode(0, QgsLayerTreeLayer(layer))
        self.layer = layer
        self.say(self.tr("AI 抽出 を作成（{n} 件、一時レイヤー）").format(n=len(features)))
        self.clear()
        return layer

    def clear(self) -> None:
        for o in self.objects:
            for m in o.markers:
                self.canvas.scene().removeItem(m)
            if o.band is not None:
                self.canvas.scene().removeItem(o.band)
        self.objects = []
        self._update()

    def outlines(self) -> list[QgsGeometry]:
        """The outlines made so far (not yet a layer), in the map's CRS."""
        return [QgsGeometry.fromPolygonXY(o.rings) for o in self.objects if o.rings]

    def count(self) -> int:
        return sum(1 for o in self.objects if o.rings)

    def _update(self) -> None:
        self.panel.keep_button.setEnabled(self.count() > 0)
        self.panel.undo_button.setEnabled(bool(self.objects))

    def note(self, text: str) -> None:
        self.panel.note.setText(text)

    def say(self, text: str) -> None:
        self.iface.messageBar().pushInfo(self.tr("クリック抽出（AI）"), text)

    def close(self) -> None:
        self.set_active(False)
        for signal, slot in ((self.canvas.mapToolSet, self._tool_set), (self.context.configChanged, self.fill_models)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.panel.deleteLater()


def encode(encoder: LoadedModel, rgba: np.ndarray, side: int, config: Optional[SamModelConfig]) -> np.ndarray:
    """The encoder's image embeddings for the scaled picture (RGBA at its top left)."""
    first = encoder.signature.inputs[0]
    if len(first.dims) == 3 and first.dims[2] == 3:
        # An encoder with its preprocessing inside: the scaled picture as it is, height × width × RGB.
        feed = sam_hwc(rgba)
    else:
        feed = sam_tensor(rgba, side, (config.mean if config else None) or SAM_MEAN, (config.std if config else None) or SAM_STD)[None]
    outputs = encoder.run({first.name: feed})
    return outputs[encoder.output_names[0]]


def decode(decoder: LoadedModel, image: SamImage, points: list[SamPoint]) -> np.ndarray:
    """Runs the decoder for `points` and returns the picture-sized mask."""
    coords, labels = sam_prompt(points, image.scale)
    names = decoder.input_names
    feeds: dict[str, np.ndarray] = {}
    for name, value in (("point_coords", coords), ("point_labels", labels), ("mask_input", np.zeros((1, 1, 256, 256), np.float32)),
                        ("has_mask_input", np.zeros(1, np.float32)), ("orig_im_size", np.array([image.height, image.width], np.float32))):
        if name in names:
            feeds[name] = value
    feeds[name_of(names, "image_embeddings", 0)] = image.embeddings
    outputs = decoder.run(feeds)
    out = decoder.output_names
    masks = outputs[name_of(out, "masks", 0)]
    scores_name = "iou_predictions" if "iou_predictions" in out else next((n for n in out if outputs[n].ndim == 2), None)
    return sam_mask(masks, outputs[scores_name] if scores_name else None, image.width, image.height, image.scale, image.side)
