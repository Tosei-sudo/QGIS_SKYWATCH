"""
「移動体と検出の照合」 as a Processing algorithm (the viewer's AI 検出と照合):
checks detections (AI results, or any point / polygon layer) against a
moving-object feed open in 「移動体（AIS・ADS-B）」. For each detection, the
nearest object where it was at the detection's time (its `image_time`, the
acquisition time of a chosen image, or as typed) within a time window and a
distance. The result is a copy of the detections with columns added
(ais_status, ais_id, ais_name, ais_dist_m, ais_dt_s), coloured by the match:
unmatched detections (ships no AIS reports, say) in red.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink, QgsField, QgsFields,
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingLayerPostProcessorInterface, QgsProcessingParameterDateTime,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer, QgsRendererCategory, QgsSymbol, QgsVectorLayer, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, QVariant
from qgis.PyQt.QtGui import QColor, QIcon

from ..core.moving import match_detections, utc_text
from ..core.obs.image_time import parse_time
from ..qgis_io.moving_layer import OPEN

HERE = os.path.dirname(os.path.dirname(__file__))
MATCHED, UNMATCHED, NO_TIME = "一致", "なし", "時刻なし"
TIME_KEYS = ["field", "image", "manual"]
TIME_LABELS = ["各検出の撮像日時（image_time）", "画像の撮影時刻", "日時を入力"]
ADDED = [("ais_status", QVariant.String, "照合"), ("ais_id", QVariant.String, "一致した ID"), ("ais_name", QVariant.String, "一致した名前"),
         ("ais_dist_m", QVariant.Double, "距離（m）"), ("ais_dt_s", QVariant.Int, "時刻の差（秒）")]

#: Post-processors waiting for their layer (QGIS holds them only by pointer).
_waiting: set = set()


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def time_value(value) -> float | None:
    """A detection's time from its attribute: a date-time, ms since 1970, or text (no zone: local, as the viewer's)."""
    if isinstance(value, QDateTime):
        return float(value.toMSecsSinceEpoch()) if value.isValid() else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.replace(".", "", 1).isdigit():
            return float(text)
        t = parse_time(text)
        return None if t is None else float(t)
    return None


class MatchLook(QgsProcessingLayerPostProcessorInterface):
    """Unmatched in red, matched in green, without a time in grey."""

    def __init__(self, name: str):
        super().__init__()
        self.name = name

    def postProcessLayer(self, layer, context, feedback) -> None:  # noqa: N802 (QGIS's name)
        _waiting.discard(self)
        layer.setName(self.name)
        if not isinstance(layer, QgsVectorLayer):
            return
        categories = []
        for value, color in ((UNMATCHED, "#e03131"), (MATCHED, "#2f9e44"), (NO_TIME, "#868e96")):
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(QColor(color))
            categories.append(QgsRendererCategory(value, symbol, value))
        layer.setRenderer(QgsCategorizedSymbolRenderer("ais_status", categories))
        layer.triggerRepaint()


class MovingMatchAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    FEED = "FEED"
    TIME_FROM = "TIME_FROM"
    IMAGE = "IMAGE"
    TIME = "TIME"
    WINDOW = "WINDOW"
    DISTANCE = "DISTANCE"
    OUTPUT = "OUTPUT"

    def __init__(self):
        super().__init__()
        self._name = ""

    def createInstance(self) -> "MovingMatchAlgorithm":  # noqa: N802 (QGIS's name)
        return MovingMatchAlgorithm()

    def name(self) -> str:
        return "moving_match"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("移動体と検出の照合")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "moving_match.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "検出（AI の結果などのポイント・ポリゴン）を、「移動体（AIS・ADS-B）」で開いたフィード・履歴ファイルと照合。\n\n"
            "検出の時刻に各移動体がいた位置（前後の報告から補間）が距離内にあれば、近いものと一致と判定。"
            "時刻は各検出の image_time、選んだ画像の撮影時刻、入力した日時のいずれか。\n"
            "結果は検出の写しに ais_status（一致・なし・時刻なし）・ais_id・ais_name・ais_dist_m・ais_dt_s を加えたレイヤー。不一致の検出は赤で表示。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, tr("検出レイヤー"), [QgsProcessing.SourceType.TypeVectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.FEED, tr("移動体のフィード（移動体ドックで開いたもの）"), [QgsProcessing.SourceType.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterEnum(self.TIME_FROM, tr("時刻"), [tr(x) for x in TIME_LABELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterRasterLayer(self.IMAGE, tr("画像（画像の撮影時刻のとき）"), optional=True))
        self.addParameter(QgsProcessingParameterDateTime(self.TIME, tr("日時（入力のとき）"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.WINDOW, tr("時間の幅（± 分）"), QgsProcessingParameterNumber.Type.Double, 10, False, 0))
        self.addParameter(QgsProcessingParameterNumber(self.DISTANCE, tr("距離（m）"), QgsProcessingParameterNumber.Type.Double, 500, False, 1))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("照合結果")))

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        source = self.parameterAsSource(parameters, self.INPUT, context)
        feed_layer = self.parameterAsVectorLayer(parameters, self.FEED, context)
        if source is None:
            raise QgsProcessingException(tr("検出レイヤーを開く必要あり"))
        moving = OPEN.get(feed_layer.id()) if feed_layer is not None else None
        if moving is None:
            raise QgsProcessingException(tr("移動体のフィードか履歴ファイルを開く必要あり（「移動体（AIS・ADS-B）」ドック）"))
        how = TIME_KEYS[self.parameterAsEnum(parameters, self.TIME_FROM, context)]
        fixed = None
        if how == "image":
            from ..qgis_io.obs_images import observed_image

            image = self.parameterAsRasterLayer(parameters, self.IMAGE, context)
            if image is None:
                raise QgsProcessingException(tr("画像を選択"))
            fixed = observed_image(image).time
            if fixed is None:
                raise QgsProcessingException(tr("この画像の撮影時刻が不明（観測記録ドックで入力可能）"))
        elif how == "manual":
            typed = self.parameterAsDateTime(parameters, self.TIME, context)
            if not typed.isValid():
                raise QgsProcessingException(tr("日時を入力"))
            fixed = float(typed.toMSecsSinceEpoch())
        elif source.fields().indexOf("image_time") < 0:
            raise QgsProcessingException(tr("検出レイヤーに image_time の属性なし（画像の撮影時刻か日時を選択）"))
        window = max(0.0, self.parameterAsDouble(parameters, self.WINDOW, context)) * 60_000
        distance = max(1.0, self.parameterAsDouble(parameters, self.DISTANCE, context))

        to_wgs = QgsCoordinateTransform(source.sourceCrs(), QgsCoordinateReferenceSystem("EPSG:4326"), context.transformContext())
        features, points = [], []
        for f in source.getFeatures():
            if feedback.isCanceled():
                break
            if not f.hasGeometry():
                continue
            g = f.geometry()
            kind = g.type()
            if kind == QgsWkbTypes.GeometryType.PolygonGeometry:
                anchor = g.pointOnSurface()
            elif kind == QgsWkbTypes.GeometryType.LineGeometry:
                anchor = g.interpolate(g.length() / 2)
            elif kind == QgsWkbTypes.GeometryType.PointGeometry:
                anchor = g.centroid() if g.isMultipart() else g
            else:
                continue
            p = to_wgs.transform(anchor.asPoint())
            t = fixed if fixed is not None else time_value(f["image_time"])
            points.append((p.x(), p.y(), t))
            features.append(f)
        matches = match_detections(points, moving.store.tracks.values(), window, distance)

        fields = QgsFields()
        names = {name for name, _, _ in ADDED}
        for field in source.fields():
            if field.name() not in names:
                fields.append(field)
        for name, kind, alias in ADDED:
            field = QgsField(name, kind)
            field.setAlias(alias)
            fields.append(field)
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(), source.sourceCrs())
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        matched = no_time = 0
        for f, point, m in zip(features, points, matches):
            status = NO_TIME if point[2] is None else MATCHED if m else UNMATCHED
            matched += status == MATCHED
            no_time += status == NO_TIME
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            values = {field.name(): f[field.name()] for field in source.fields()}
            values.update(ais_status=status, ais_id=m.id if m else None, ais_name=m.name if m else None,
                          ais_dist_m=round(m.distance, 1) if m else None, ais_dt_s=round(m.dt) if m else None)
            out.setAttributes([values.get(fields.at(i).name()) for i in range(fields.count())])
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
        unmatched = len(features) - matched - no_time
        feedback.pushInfo(tr("一致 {m} 件・なし {u} 件").format(m=f"{matched:,}", u=f"{unmatched:,}")
                          + (tr("・時刻なし {n} 件").format(n=f"{no_time:,}") if no_time else ""))
        if fixed is not None:
            feedback.pushInfo(tr("時刻 {t} ± {w} 分").format(t=utc_text(fixed), w=f"{window / 60_000:g}"))
        self._name = tr("{name}（{feed} と照合）").format(name=source.sourceName(), feed=moving.feed.label)
        return {self.OUTPUT: dest}

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        for layer_id in context.layersToLoadOnCompletion():
            details = context.layerToLoadOnCompletionDetails(layer_id)
            if details.outputName == self.OUTPUT:
                post = MatchLook(self._name)
                _waiting.add(post)
                details.setPostProcessor(post)
        return {}


def default_inputs(layers: list, active=None) -> dict:
    """The dialog's first choice: the active detection layer (else the first point or polygon layer not a feed), the first feed."""
    feeds = [l for l in layers if isinstance(l, QgsVectorLayer) and l.id() in OPEN]
    shapes = [l for l in layers if isinstance(l, QgsVectorLayer) and l.id() not in OPEN and l.customProperty("satobs/moving") is None
              and l.geometryType() in (QgsWkbTypes.GeometryType.PointGeometry, QgsWkbTypes.GeometryType.PolygonGeometry)]
    answer = {}
    if shapes:
        detections = active if active in shapes else shapes[0]
        answer[MovingMatchAlgorithm.INPUT] = detections.id()
        if detections.fields().indexOf("image_time") < 0:
            answer[MovingMatchAlgorithm.TIME_FROM] = 1
    if feeds:
        answer[MovingMatchAlgorithm.FEED] = feeds[0].id()
    return answer
