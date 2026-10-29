"""
「物体検出（AI）」 as a Processing algorithm (the viewer's 物体検出 dialog): a
YOLO model (boxes, rotated boxes or instance outlines; also YOLOX and
Detectron2 exports) exported to ONNX runs over an image as it is shown, in
overlapping tiles, and the objects found become polygons with their class
and score.

Models are those of the settings file's `aiModels` (kind detect), or an
ONNX file chosen here. An Ultralytics model carries its class names and
input size, so it needs no settings. ONNX Runtime must be there (see
qgis_io/ai_runtime.py).
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from qgis.core import (
    QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsFillSymbol, QgsGeometry, QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingException, QgsProcessingLayerPostProcessorInterface, QgsProcessingParameterDefinition, QgsProcessingParameterEnum,
    QgsProcessingParameterExtent, QgsProcessingParameterFeatureSink, QgsProcessingParameterFile, QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterString, QgsRasterLayer, QgsSingleSymbolRenderer, QgsVectorLayer, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, QVariant
from qgis.PyQt.QtGui import QIcon

from ..core.ai.detect import class_filter, class_name
from ..core.ai.model_config import DetectModelConfig, detector_spec
from ..qgis_io.ai_io import angle_degrees, band_values, check_bands, detect_layer, detection_rings, grid_of, render_rgba, summary
from ..qgis_io.ai_transfer_io import mark_origin
from ..qgis_io.ai_runtime import AiError, local_model, model_from

HERE = os.path.dirname(os.path.dirname(__file__))
TASK_LABELS = {"detect": "物体検出（矩形）", "obb": "物体検出（回転矩形）", "segment": "インスタンスセグメンテーション（輪郭）"}

#: The settings file's models and where the file is (for relative paths), given by the plugin.
_models: Callable[[], tuple[list, str]] = lambda: ([], "")
#: Post-processors waiting for their layer (QGIS holds them only by pointer).
_waiting: set = set()


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def set_models(models: Callable[[], tuple[list, str]]) -> None:
    """Where the algorithm finds the settings file's models: `() -> (aiModels, the settings file's path or URL)`."""
    global _models
    _models = models


def detect_models() -> tuple[list[DetectModelConfig], str]:
    """The settings file's detection models, then those trained in QGIS (「学習済み」, absolute paths)."""
    from ..qgis_io.settings import trained_models

    models, base = _models()
    own = [m for m in models if isinstance(m, DetectModelConfig)]
    return own + [DetectModelConfig(tr("学習済み: {label}").format(label=label), path) for label, path in trained_models()], base


def source_fields(fields: QgsFields) -> None:
    """The fields every AI layer has: where its features come from (as the viewer's sourceFields)."""
    for name, kind in (("image", QVariant.String), ("image_id", QVariant.String), ("image_time", QVariant.DateTime),
                       ("model", QVariant.String), ("detected_at", QVariant.DateTime), ("note", QVariant.String)):
        fields.append(QgsField(name, kind))


def image_info(layer: QgsRasterLayer) -> tuple:
    """The image's name, id and acquisition time (a QDateTime or None), as the observation book knows them."""
    from ..qgis_io.obs_images import observed_image
    from ..qgis_io.plan_layer import qdatetime

    try:
        image = observed_image(layer)
    except Exception:  # noqa: BLE001 (a layer the book cannot read)
        return layer.name(), None, None
    return image.name, image.id, qdatetime(image.time) if image.time is not None else None


def outline_symbol() -> QgsFillSymbol:
    return QgsFillSymbol.createSimple({"color": "0,229,255,46", "outline_color": "0,229,255,255", "outline_width": "0.5"})


class AiLook(QgsProcessingLayerPostProcessorInterface):
    """Names the layer and draws its outlines as the viewer's, when QGIS opens it."""

    def __init__(self, name: str):
        super().__init__()
        self.name = name

    def postProcessLayer(self, layer, context, feedback) -> None:  # noqa: N802 (QGIS's name)
        _waiting.discard(self)
        layer.setName(self.name)
        if isinstance(layer, QgsVectorLayer):
            layer.setRenderer(QgsSingleSymbolRenderer(outline_symbol()))
            # As the model made them: a later change makes a feature 「修正」 when it is sent to a database.
            mark_origin(layer)
            layer.triggerRepaint()


class DetectAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    MODEL = "MODEL"
    MODEL_FILE = "MODEL_FILE"
    EXTENT = "EXTENT"
    PIXEL_SIZE = "PIXEL_SIZE"
    SCORE = "SCORE"
    IOU = "IOU"
    OVERLAP = "OVERLAP"
    CLASSES = "CLASSES"
    OUTPUT = "OUTPUT"

    def __init__(self):
        super().__init__()
        self._name = ""
        #: What was found (for scripts and tests): the DetectRun of the last run.
        self.result = None

    def createInstance(self) -> "DetectAlgorithm":  # noqa: N802 (QGIS's name)
        return DetectAlgorithm()

    def name(self) -> str:
        return "detect"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("物体検出（AI）")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "ai_detect.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "ONNX に書き出した YOLO（Ultralytics の v5・v8・v11 以降、回転矩形・インスタンスセグメンテーション、YOLOX、Detectron2）で、"
            "画像の物体を検出してポリゴン化。画像は表示のまま（バンドの組み合わせ・コントラスト）で解析。\n\n"
            "モデルは設定ファイルの aiModels、または ONNX ファイルを指定。Ultralytics のモデルはクラス名と入力の大きさを内蔵。\n"
            "範囲を画素の大きさで描画し、モデルの入力の大きさのタイル（重なりあり）に分けて解析。小さな物体は画素を小さく（拡大）。\n"
            "onnxruntime が必要（Windows 版の zip は同梱）。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("対象の画像")))
        labels = [m.label for m in detect_models()[0]] + [tr("ファイルを指定（下の欄）")]
        self.addParameter(QgsProcessingParameterEnum(self.MODEL, tr("モデル（ONNX）"), labels, defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(self.MODEL_FILE, tr("モデルのファイル"), extension="onnx", optional=True))
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, tr("解析する範囲（指定なしで画像全体）"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.PIXEL_SIZE, tr("画素の大きさ（画像の単位、0 で画像の画素）"),
                                                       QgsProcessingParameterNumber.Type.Double, 0, False, 0))
        advanced = [
            QgsProcessingParameterNumber(self.SCORE, tr("スコアのしきい値（0 でモデルの既定）"), QgsProcessingParameterNumber.Type.Double, 0, False, 0, 0.99),
            QgsProcessingParameterNumber(self.IOU, tr("重なりの判定（IoU、0 でモデルの既定）"), QgsProcessingParameterNumber.Type.Double, 0, False, 0, 0.95),
            QgsProcessingParameterNumber(self.OVERLAP, tr("タイルの重なり（%）"), QgsProcessingParameterNumber.Type.Integer, 20, False, 0, 50),
            QgsProcessingParameterString(self.CLASSES, tr("検出するクラス（カンマ区切り、空ならすべて）"), optional=True),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("検出結果"), QgsProcessing.SourceType.TypeVectorPolygon))

    def _model_source(self, parameters, context) -> tuple[Optional[DetectModelConfig], str, str]:
        """The chosen model: its settings (None for a file), its path or URL, and the settings file it is relative to."""
        path = self.parameterAsFile(parameters, self.MODEL_FILE, context)
        models, base = detect_models()
        index = self.parameterAsEnum(parameters, self.MODEL, context)
        if 0 <= index < len(models):
            return models[index], models[index].url, base
        if path:
            return None, path, ""
        raise QgsProcessingException(tr("モデルを選択（設定ファイルの aiModels か ONNX ファイル）"))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(tr("画像を開く必要あり"))
        self._config, self._source, self._base = self._model_source(parameters, context)
        self._crs = layer.crs()
        extent = self.parameterAsExtent(parameters, self.EXTENT, context, self._crs)
        self._extent = extent if not extent.isNull() and not extent.isEmpty() else layer.extent()
        self._extent = self._extent.intersect(layer.extent()) if layer.extent().intersects(self._extent) else self._extent
        size = self.parameterAsDouble(parameters, self.PIXEL_SIZE, context)
        if not size > 0:
            size = layer.rasterUnitsPerPixelX() if layer.providerType() == "gdal" else 0
        if not size > 0:
            # A service without its own pixels: about 4,000 pixels across.
            size = max(self._extent.width(), self._extent.height()) / 4000
        self._pixel_size = size
        # Drawn off the main thread: a copy of the layer, as QGIS's own map rendering algorithms do.
        self._layer = layer.clone()
        self._layer_name = layer.name()
        self._image = image_info(layer)
        self._transform_context = context.transformContext()
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        try:
            return self._process(parameters, context, feedback)
        finally:
            # The layer copies go now: a refused run's algorithm lives on in QGIS's exception until exit, after GDAL.
            self._layer = None

    def _process(self, parameters, context, feedback) -> dict:
        started = time.monotonic()
        error = ""
        try:
            feedback.setProgressText(tr("モデルを読み込み中…"))
            path = local_model(self._source, self._base, progress=lambda p: feedback.setProgress(10 * p), cancelled=feedback.isCanceled)
            label = self._config.label if self._config else os.path.basename(path)
            model = model_from(path, label)
            spec = detector_spec(self._config, model.metadata, model.signature)
            score = self.parameterAsDouble(parameters, self.SCORE, context)
            iou = self.parameterAsDouble(parameters, self.IOU, context)
            if 0 < score < 1:
                spec.score = score
            if 0 < iou < 1:
                spec.iou = iou
            classes = spec.classes
            feedback.pushInfo(tr("{task}{kind}・入力 {w}×{h}・{classes}・{backend}で実行").format(
                task=TASK_LABELS[spec.task], kind="（Detectron2 形式）" if spec.format == "detectron2" else "",
                w=spec.input_size[0], h=spec.input_size[1], backend=model.backend,
                classes=f"{len(classes)} クラス（{'・'.join(classes[:8])}{'…' if len(classes) > 8 else ''}）" if classes else "クラス名なし（番号で表示）"))
            grid = grid_of(self._extent, self._pixel_size)
            feedback.pushInfo(tr("{w} × {h} 画素（画素 {s:g}）を解析").format(w=f"{grid.width:,}", h=f"{grid.height:,}", s=self._pixel_size))
            if spec.bands:
                # A model trained here on band values: the bands as they are, not the image as shown.
                check_bands(self._layer, spec.bands)
                feedback.pushInfo(tr("バンド {bands} の値で解析（表示の設定は不使用）").format(bands=", ".join(map(str, spec.bands))))
                render = lambda extent, w, h: band_values(self._layer, spec.bands, extent, w, h)  # noqa: E731
            else:
                render = lambda extent, w, h: render_rgba(self._layer, extent, w, h, self._crs, self._transform_context)  # noqa: E731
            run = detect_layer(
                model, spec, render, grid,
                overlap=self.parameterAsInt(parameters, self.OVERLAP, context) / 100,
                classes=class_filter(self.parameterAsString(parameters, self.CLASSES, context)),
                progress=lambda text, done: (feedback.setProgressText(tr(text)), done is not None and feedback.setProgress(10 + 88 * done)),
                cancelled=feedback.isCanceled,
            )
        except AiError as e:
            if feedback.isCanceled():
                return {}
            # Raised after the handler: the error's frames (one holding the layer) do not live on with the run.
            error = str(e)
        if error:
            raise QgsProcessingException(tr(error))
        self.result = run

        fields = QgsFields()
        fields.append(QgsField("class", QVariant.String))
        fields.append(QgsField("class_id", QVariant.Int))
        fields.append(QgsField("score", QVariant.Double))
        if spec.task == "obb":
            fields.append(QgsField("angle", QVariant.Double))
        source_fields(fields)
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, QgsWkbTypes.Type.Polygon, self._crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        image, image_id, image_time = self._image
        now = QDateTime.currentDateTimeUtc()
        for d in run.detections:
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPolygonXY(detection_rings(d, grid)))
            values = [class_name(d.cls, classes), d.cls, round(d.score, 3)]
            if spec.task == "obb":
                values.append(angle_degrees(d))
            f.setAttributes(values + [image, image_id, image_time, label, now, None])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        seconds = time.monotonic() - started
        if run.detections:
            feedback.pushInfo(tr("{n} 件: {summary}（{tiles} タイル、{backend}、{s:.1f} 秒）").format(
                n=f"{len(run.detections):,}", summary=summary(run.detections, classes), tiles=run.tiles, backend=model.backend, s=seconds))
        else:
            feedback.pushWarning(tr("検出なし（{tiles} タイル、スコア {score} 以上）。画素を小さく（拡大）、またはしきい値の引き下げを推奨").format(
                tiles=run.tiles, score=spec.score))
        self._name = tr("物体検出（{label}）").format(label=label)
        feedback.setProgress(100)
        return {self.OUTPUT: dest}

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        for layer_id in context.layersToLoadOnCompletion():
            details = context.layerToLoadOnCompletionDetails(layer_id)
            if details.outputName == self.OUTPUT and self._name:
                post = AiLook(self._name)
                _waiting.add(post)
                details.setPostProcessor(post)
        return {}


def default_inputs(layers: list, active=None, canvas=None) -> dict:
    """
    The dialog's first choice, as the viewer's (its view at the screen's
    fineness): the active image (else the top visible one), the map's
    extent, and the screen's pixel size there.
    """
    from qgis.core import QgsCoordinateTransform, QgsProject

    images = [l for l in layers if isinstance(l, QgsRasterLayer)]
    if not images:
        return {}
    layer = active if active in images else images[0]
    answer = {DetectAlgorithm.INPUT: layer.id()}
    if canvas is not None:
        settings = canvas.mapSettings()
        try:
            to = QgsCoordinateTransform(settings.destinationCrs(), layer.crs(), QgsProject.instance())
            extent = to.transformBoundingBox(canvas.extent())
        except Exception:  # noqa: BLE001 (outside the CRS's area)
            return answer
        if extent.intersects(layer.extent()):
            extent = extent.intersect(layer.extent())
            answer[DetectAlgorithm.EXTENT] = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()} [{layer.crs().authid()}]"
            width = canvas.width() or settings.outputSize().width()
            if width > 0:
                answer[DetectAlgorithm.PIXEL_SIZE] = to.transformBoundingBox(canvas.extent()).width() / width
    return answer
