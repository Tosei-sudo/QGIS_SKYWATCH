"""
「測地線バッファ」 and 「正距方位での重心」 as Processing algorithms (the
viewer's 測地線バッファ and ポリゴンの重心): QGIS's own buffer and centroids
work in the layer's CRS (degrees for a longitude / latitude layer, stretched
distances in Web Mercator); these work on the WGS 84 ellipsoid
(qgis_io.geodesic_io), whatever the layer's CRS.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsFeature, QgsFeatureSink, QgsFields, QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..qgis_io.geodesic_io import dissolve, geodesic_buffer, geodesic_centroid, parts_of

HERE = os.path.dirname(os.path.dirname(__file__))
CAP_KEYS = ["round", "flat", "square"]
CAP_LABELS = ["丸", "平ら", "四角"]


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class GeodesicBufferAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    DISTANCE = "DISTANCE"
    SEGMENTS = "SEGMENTS"
    CAP = "CAP"
    DISSOLVE = "DISSOLVE"
    OUTPUT = "OUTPUT"

    def createInstance(self) -> "GeodesicBufferAlgorithm":  # noqa: N802 (QGIS's name)
        return GeodesicBufferAlgorithm()

    def name(self) -> str:
        return "geodesic_buffer"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("測地線バッファ")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "geodesic_buffer.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "WGS 84 楕円体上の距離（m）で地物を拡張（負の距離でポリゴンを縮小）。レイヤーの座標系によらず同じ距離。\n\n"
            "点の丸いバッファは本当の測地線の円。線・ポリゴンは地物ごとに中心の正距方位図法で拡張して戻す。"
            "QGIS 本体の「バッファ」はレイヤーの座標系の単位（経緯度なら度）で拡張。\n"
            "バッファが空になる地物（負の距離の点・線、縮んで消えるポリゴン）は除外。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, tr("入力レイヤー"), [QgsProcessing.SourceType.TypeVectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterNumber(self.DISTANCE, tr("距離（m、負でポリゴンを縮小）"), QgsProcessingParameterNumber.Type.Double, 100))
        self.addParameter(QgsProcessingParameterNumber(self.SEGMENTS, tr("円弧の細かさ（90° あたりの分割数）"), QgsProcessingParameterNumber.Type.Integer,
                                                       16, False, 1, 90))
        self.addParameter(QgsProcessingParameterEnum(self.CAP, tr("線の端"), [tr(x) for x in CAP_LABELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(self.DISSOLVE, tr("結果を 1 つに融合（ディゾルブ）"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("測地線バッファ"), QgsProcessing.SourceType.TypeVectorPolygon))

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(tr("入力レイヤーを開く必要あり"))
        distance = self.parameterAsDouble(parameters, self.DISTANCE, context)
        if distance == 0:
            raise QgsProcessingException(tr("距離を入力"))
        segments = self.parameterAsInt(parameters, self.SEGMENTS, context) or 16
        cap = CAP_KEYS[self.parameterAsEnum(parameters, self.CAP, context)]
        merge = self.parameterAsBool(parameters, self.DISSOLVE, context)
        crs, transform_context = source.sourceCrs(), context.transformContext()
        fields = QgsFields() if merge else source.fields()
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, QgsWkbTypes.Type.MultiPolygon, crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        total = source.featureCount() or 1
        buffered, emptied, no_geometry = [], 0, 0
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            feedback.setProgress(90 * i / total)
            if not f.hasGeometry():
                no_geometry += 1
                continue
            g = geodesic_buffer(f.geometry(), crs, transform_context, distance, segments, cap)
            if g is None:
                emptied += 1
                continue
            g.convertToMultiType()
            if merge:
                buffered.append(g)
                continue
            out = QgsFeature(fields)
            out.setGeometry(g)
            out.setAttributes(f.attributes())
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            buffered.append(None)
        if merge and buffered:
            g = dissolve(buffered, crs, transform_context)
            if g is not None:
                g.convertToMultiType()
                out = QgsFeature(fields)
                out.setGeometry(g)
                sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
        feedback.pushInfo(tr("{n} 件のバッファを 1 つに統合").format(n=len(buffered)) if merge else tr("{n} 件のバッファを作成").format(n=len(buffered)))
        if emptied:
            feedback.pushInfo(tr("{n} 件はバッファが空になるため除外").format(n=emptied))
        if no_geometry:
            feedback.pushInfo(tr("{n} 件はジオメトリがないため除外").format(n=no_geometry))
        return {self.OUTPUT: dest}


class GeodesicCentroidAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    PER_PART = "PER_PART"
    INSIDE = "INSIDE"
    OUTPUT = "OUTPUT"

    def createInstance(self) -> "GeodesicCentroidAlgorithm":  # noqa: N802 (QGIS's name)
        return GeodesicCentroidAlgorithm()

    def name(self) -> str:
        return "geodesic_centroids"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("正距方位での重心")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "geodesic_centroid.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "地物ごとの重心を、地上（WGS 84 楕円体）で算出。地物の中心の正距方位図法で重心を求めて戻すため、"
            "経緯度や Web メルカトルの伸びに引っ張られない。\n\n"
            "ポリゴンは面積（穴は差し引き）、線は長さ、点は等しく重み付け。属性は元の地物から引き継ぎ。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, tr("入力レイヤー"), [QgsProcessing.SourceType.TypeVectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterBoolean(self.PER_PART, tr("マルチポリゴンはパートごとに作成"), False))
        self.addParameter(QgsProcessingParameterBoolean(self.INSIDE, tr("重心が外に出る形は内側の点を使用"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("重心"), QgsProcessing.SourceType.TypeVectorPoint))

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(tr("入力レイヤーを開く必要あり"))
        per_part = self.parameterAsBool(parameters, self.PER_PART, context)
        inside = self.parameterAsBool(parameters, self.INSIDE, context)
        crs, transform_context = source.sourceCrs(), context.transformContext()
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, source.fields(), QgsWkbTypes.Type.Point, crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        total = source.featureCount() or 1
        made = skipped = 0
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            feedback.setProgress(100 * i / total)
            count = 0
            if f.hasGeometry():
                for part in parts_of(f.geometry()) if per_part else [f.geometry()]:
                    point = geodesic_centroid(part, crs, transform_context, inside)
                    if point is None:
                        continue
                    out = QgsFeature(source.fields())
                    out.setGeometry(point)
                    out.setAttributes(f.attributes())
                    sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
                    count += 1
            made += count
            skipped += 0 if count else 1
        feedback.pushInfo(tr("重心 {n} 点を作成").format(n=made))
        if skipped:
            feedback.pushInfo(tr("形状のない地物 {n} 件をスキップ").format(n=skipped))
        return {self.OUTPUT: dest}
