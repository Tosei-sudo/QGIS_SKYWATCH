"""
「変化抽出（2 時期）」 as a Processing algorithm (the viewer's change dialog):
a band of an image before and one after, compared pixel by pixel on one grid
(qgis_io.change_io) into a one-band image shown blue where the value fell,
white where it did not change and red where it rose; the pixels that changed
more than the thresholds (by default the mean ± 2 standard deviations) as
polygons with their direction, area and pixels.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsColorRampShader, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingLayerPostProcessorInterface, QgsProcessingParameterBand,
    QgsProcessingParameterBoolean, QgsProcessingParameterDefinition, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer, QgsRasterLayer,
    QgsRasterShader, QgsSingleBandPseudoColorRenderer, QgsWkbTypes, QgsCoordinateReferenceSystem,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtGui import QColor, QIcon

from ..core.change import CHANGE_METHODS, DIRECTIONS, describe_thresholds
from ..core.enhance.color_scale import COLOR_SCALES
from ..qgis_io.change_io import MAX_POLYGONS, RESAMPLING, ChangeError, change_files
from .pansharpen_algorithm import file_of

HERE = os.path.dirname(os.path.dirname(__file__))
DIRECTION_LABELS = ["増加と減少", "増加だけ", "減少だけ"]
RESAMPLE_KEYS = list(RESAMPLING)
RESAMPLE_LABELS = ["バイリニア", "最近傍（値の変化なし）"]

#: Post-processors waiting for their layer (QGIS holds them only by pointer).
_waiting: set = set()


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def blue_white_red(layer: QgsRasterLayer, low: float, high: float) -> None:
    """The change image in the viewer's blueWhiteRed scale from `low` (blue) to `high` (red), no change in white."""
    ramp = QgsColorRampShader(low, high)
    ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
    items = []
    for position, rgb in COLOR_SCALES["blueWhiteRed"]:
        value = low + (high - low) * position
        items.append(QgsColorRampShader.ColorRampItem(value, QColor(*rgb[:3]), f"{value:.4g}"))
    ramp.setColorRampItemList(items)
    shader = QgsRasterShader(low, high)
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(low)
    renderer.setClassificationMax(high)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


class ChangeLook(QgsProcessingLayerPostProcessorInterface):
    """Names and colours the layers when QGIS opens them."""

    def __init__(self, name: str, scale: tuple = ()):
        super().__init__()
        self.name, self.scale = name, scale

    def postProcessLayer(self, layer, context, feedback) -> None:  # noqa: N802 (QGIS's name)
        _waiting.discard(self)
        layer.setName(self.name)
        if self.scale and isinstance(layer, QgsRasterLayer):
            blue_white_red(layer, *self.scale)


class ChangeAlgorithm(QgsProcessingAlgorithm):
    BEFORE = "BEFORE"
    BEFORE_BAND = "BEFORE_BAND"
    AFTER = "AFTER"
    AFTER_BAND = "AFTER_BAND"
    METHOD = "METHOD"
    OUTLINE = "OUTLINE"
    THRESHOLD = "THRESHOLD"
    DIRECTION = "DIRECTION"
    MIN_PIXELS = "MIN_PIXELS"
    RESAMPLE = "RESAMPLE"
    OUTPUT = "OUTPUT"
    OUTLINES = "OUTLINES"

    def __init__(self):
        super().__init__()
        self._looks: dict = {}

    def createInstance(self) -> "ChangeAlgorithm":  # noqa: N802 (QGIS's name)
        return ChangeAlgorithm()

    def name(self) -> str:
        return "change"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("変化抽出（2 時期）")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "change.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "同じ場所の 2 枚を比較し、値が下がった所を青、上がった所を赤（変化なしが白）で表示、大きく変わった所をポリゴン化。\n\n"
            "2 枚は細かいほうの画像の格子にそろえて比較（重なる範囲）。座標系が違えば再投影。\n"
            "しきい値は「変化なし」（差は 0、比は 1）からの隔たり。0 なら、変化の平均 ± 標準偏差の 2 倍を超えた所を変化と判定。\n"
            "変化域は 4 近傍でつながる画素のまとまり。最小の大きさ未満のまとまりと穴は除く。属性は方向・面積（m²、楕円体）・画素数。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterRasterLayer(self.BEFORE, tr("前の画像")))
        self.addParameter(QgsProcessingParameterBand(self.BEFORE_BAND, tr("前のバンド"), 1, self.BEFORE))
        self.addParameter(QgsProcessingParameterRasterLayer(self.AFTER, tr("後の画像")))
        self.addParameter(QgsProcessingParameterBand(self.AFTER_BAND, tr("後のバンド"), 1, self.AFTER))
        self.addParameter(QgsProcessingParameterEnum(self.METHOD, tr("比べ方"), [tr(m.label) for m in CHANGE_METHODS], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(self.OUTLINE, tr("変化箇所をポリゴン化"), True))
        advanced = [
            QgsProcessingParameterNumber(self.THRESHOLD, tr("しきい値（0 で自動: 平均 ± 2σ）"), QgsProcessingParameterNumber.Type.Double, 0, False, 0),
            QgsProcessingParameterEnum(self.DIRECTION, tr("向き"), [tr(x) for x in DIRECTION_LABELS], defaultValue=0),
            QgsProcessingParameterNumber(self.MIN_PIXELS, tr("最小の大きさ（画素）"), QgsProcessingParameterNumber.Type.Integer, 9, False, 1),
            QgsProcessingParameterEnum(self.RESAMPLE, tr("再サンプリング"), [tr(x) for x in RESAMPLE_LABELS], defaultValue=0),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, tr("変化画像")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTLINES, tr("変化域"), QgsProcessing.SourceType.TypeVectorPolygon,
                                                            optional=True, createByDefault=True))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        before = self.parameterAsRasterLayer(parameters, self.BEFORE, context)
        after = self.parameterAsRasterLayer(parameters, self.AFTER, context)
        if before is None or after is None:
            raise QgsProcessingException(tr("前と後の画像を開く必要あり"))
        if not file_of(before) or not file_of(after):
            raise QgsProcessingException(tr("ファイルの画像（GDAL で開いたラスター）のみ対応"))
        bands = (self.parameterAsInt(parameters, self.BEFORE_BAND, context) or 1, self.parameterAsInt(parameters, self.AFTER_BAND, context) or 1)
        if file_of(before) == file_of(after) and bands[0] == bands[1]:
            raise QgsProcessingException(tr("前と後に違う画像（バンド）を選択"))
        self._files = (file_of(before), bands[0], file_of(after), bands[1])
        self._name = f"{before.name()}_{after.name()}_{CHANGE_METHODS[self.parameterAsEnum(parameters, self.METHOD, context)].id}"[-120:]
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        out = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        method = CHANGE_METHODS[self.parameterAsEnum(parameters, self.METHOD, context)]
        outline = self.parameterAsBool(parameters, self.OUTLINE, context)
        direction = DIRECTIONS[self.parameterAsEnum(parameters, self.DIRECTION, context)]
        before, before_band, after, after_band = self._files
        try:
            result = change_files(
                before, before_band, after, after_band, out, method,
                resample=RESAMPLE_KEYS[self.parameterAsEnum(parameters, self.RESAMPLE, context)],
                outline=outline, threshold=self.parameterAsDouble(parameters, self.THRESHOLD, context) or None,
                direction=direction, min_pixels=self.parameterAsInt(parameters, self.MIN_PIXELS, context),
                progress=lambda p, text: (feedback.setProgress(p), text and feedback.setProgressText(tr(text))),
                cancelled=feedback.isCanceled,
            )
        except ChangeError as e:
            raise QgsProcessingException(tr(str(e)))
        self._looks = {self.OUTPUT: (self._name, (method.center - result.spread, method.center + result.spread))}
        answer = {self.OUTPUT: out}
        feedback.pushInfo(tr("{w} × {h} px を比較").format(w=f"{result.width:,}", h=f"{result.height:,}"))
        if outline:
            fields = QgsFields()
            for name, kind in (("方向", QVariant.String), ("面積_m2", QVariant.Double), ("画素数", QVariant.Int)):
                fields.append(QgsField(name, kind))
            crs = QgsCoordinateReferenceSystem.fromWkt(result.wkt) if result.wkt else QgsCoordinateReferenceSystem()
            sink, dest = self.parameterAsSink(parameters, self.OUTLINES, context, fields, QgsWkbTypes.Type.Polygon, crs)
            if sink is not None:
                measure = QgsDistanceArea()
                measure.setSourceCrs(crs, context.transformContext())
                measure.setEllipsoid("EPSG:7030")
                total = 0.0
                for rings, way, pixels in result.polygons:
                    geometry = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring] for ring in rings])
                    area = round(measure.measureArea(geometry), 1)
                    total += area
                    f = QgsFeature(fields)
                    f.setGeometry(geometry)
                    f.setAttributes(["増加" if way == "increase" else "減少", area, pixels])
                    sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                answer[self.OUTLINES] = dest
                self._looks[self.OUTLINES] = (tr("{name} の変化域").format(name=self._name), ())
                feedback.pushInfo(tr("変化 {n} か所（{t}）、計 {a} m²").format(
                    n=len(result.polygons), t=describe_thresholds(result.thresholds, direction, method), a=f"{total:,.1f}"))
                if result.truncated:
                    feedback.pushWarning(tr("大きい順に {n} か所まで").format(n=f"{MAX_POLYGONS:,}"))
        return answer

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        for layer_id in context.layersToLoadOnCompletion():
            details = context.layerToLoadOnCompletionDetails(layer_id)
            look = self._looks.get(details.outputName)
            if look:
                post = ChangeLook(*look)
                _waiting.add(post)
                details.setPostProcessor(post)
        return {}


def default_inputs(layers: list, time_of) -> dict:
    """The dialog's first choice, as the viewer's: the two earliest images when every one has a time, else the lowest two in the list."""
    from qgis.core import QgsProject

    files = [l for l in layers if isinstance(l, QgsRasterLayer) and file_of(l)]
    if len(files) < 2:
        return {}
    order = QgsProject.instance().layerTreeRoot().layerOrder()
    rank = {l.id(): -order.index(l) if l in order else 0 for l in files}
    times = [time_of(l) for l in files]
    if all(t is not None for t in times):
        rank = {l.id(): t for l, t in zip(files, times)}
    first, second = sorted(files, key=lambda l: rank[l.id()])[:2]
    return {ChangeAlgorithm.BEFORE: first.id(), ChangeAlgorithm.AFTER: second.id()}
