"""
「簡易オルソ補正」 as a Processing algorithm (the viewer's simple
orthorectification): an image georeferenced onto a flat ground, without an
RPC model, corrected for the terrain of a DEM and the direction the
satellite looked from (qgis_io.ortho_io). The result layer keeps the image's
画像補正.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterDefinition, QgsProcessingParameterEnum,
    QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer, QgsRasterLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..core.simple_ortho import SensorView
from ..qgis_io import enhance_renderer as er
from ..qgis_io.ortho_io import OrthoError, sensor_view_of, simple_orthorectify
from .pansharpen_algorithm import KeepLook, _waiting, file_of

HERE = os.path.dirname(os.path.dirname(__file__))
RESAMPLE_KEYS = ["bilinear", "nearest"]
RESAMPLE_LABELS = ["バイリニア", "最近傍（値の変化なし）"]


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class SimpleOrthoAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    DEM = "DEM"
    AZIMUTH = "AZIMUTH"
    ELEVATION = "ELEVATION"
    REFERENCE = "REFERENCE"
    RESAMPLE = "RESAMPLE"
    OUTPUT = "OUTPUT"

    def __init__(self):
        super().__init__()
        self._look = ("", "")
        #: What was made (for scripts and tests): the OrthoResult and SensorView of the last run.
        self.result = None
        self.view = None

    def createInstance(self) -> "SimpleOrthoAlgorithm":  # noqa: N802 (QGIS's name)
        return SimpleOrthoAlgorithm()

    def name(self) -> str:
        return "simple_ortho"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("簡易オルソ補正")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "simple_ortho.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "RPC のない、平らな地面（基準高）に載せた画像を、標高データと衛星の方向（方位角・仰角）で地形のずれを補正。\n\n"
            "基準高より h m 高い所は、衛星と反対の向きに h ÷ tan（仰角）m ずれて写るため、その分を戻す。"
            "標高データに値のない所は動かさない。\n"
            "方位角・仰角を入れなければ画像のメタデータ（GDAL が読む項目、同じ名前の .IMD）から読み取り。"
            "基準高を入れなければ画像の範囲の平均標高。\n"
            "結果は元の画像と同じ格子・すべてのバンド。RPC のある画像は QGIS の「ワープ（再投影）」で RPC と DEM を使って補正。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("補正する画像")))
        self.addParameter(QgsProcessingParameterRasterLayer(self.DEM, tr("標高データ（DEM、m）")))
        self.addParameter(QgsProcessingParameterNumber(self.AZIMUTH, tr("衛星の方位角 °（空欄でメタデータから）"), QgsProcessingParameterNumber.Type.Double,
                                                       None, True, 0, 360))
        self.addParameter(QgsProcessingParameterNumber(self.ELEVATION, tr("衛星の仰角 °（空欄でメタデータから）"), QgsProcessingParameterNumber.Type.Double,
                                                       None, True, 1, 90))
        advanced = [
            QgsProcessingParameterNumber(self.REFERENCE, tr("基準高 m（空欄で平均標高）"), QgsProcessingParameterNumber.Type.Double, None, True, -500, 9000),
            QgsProcessingParameterEnum(self.RESAMPLE, tr("再サンプリング"), [tr(x) for x in RESAMPLE_LABELS], defaultValue=0),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, tr("簡易オルソ補正した画像")))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        image = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        dem = self.parameterAsRasterLayer(parameters, self.DEM, context)
        if image is None or dem is None:
            raise QgsProcessingException(tr("補正する画像と標高データを開く必要あり"))
        if not file_of(image) or not file_of(dem):
            raise QgsProcessingException(tr("ファイルの画像（GDAL で開いたラスター）のみ対応"))
        if file_of(image) == file_of(dem):
            raise QgsProcessingException(tr("画像と違う標高データを選択"))
        self._files = (file_of(image), file_of(dem))
        found = sensor_view_of(file_of(image))
        azimuth = self._number(parameters, self.AZIMUTH, context)
        elevation = self._number(parameters, self.ELEVATION, context)
        azimuth = azimuth if azimuth is not None else found and found.azimuth
        elevation = elevation if elevation is not None else found and found.elevation
        if azimuth is None or elevation is None:
            raise QgsProcessingException(tr("衛星の方位角と仰角を入力（.IMD を画像と同じフォルダーに置くと自動読み取り）"))
        self.view = SensorView(azimuth, elevation)
        self._look = (str(image.customProperty(er.PROPERTY) or ""), f"{image.name()}_ortho")
        return True

    def _number(self, parameters, name: str, context):
        value = parameters.get(name)
        if value is None or value == "":
            return None
        return self.parameterAsDouble(parameters, name, context)

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        out = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        image, dem = self._files
        try:
            result = simple_orthorectify(image, dem, out, self.view, self._number(parameters, self.REFERENCE, context),
                                         RESAMPLE_KEYS[self.parameterAsEnum(parameters, self.RESAMPLE, context)],
                                         progress=lambda p: feedback.setProgress(100 * p), cancelled=feedback.isCanceled)
        except OrthoError as e:
            raise QgsProcessingException(tr(str(e)))
        self.result = result
        feedback.pushInfo(tr("方位角 {a}°・仰角 {e}°、基準高 {h} m、標高データの範囲 {c}%、最大 {m} m 移動").format(
            a=f"{self.view.azimuth:g}", e=f"{self.view.elevation:g}", h=round(result.reference), c=round(result.coverage * 100),
            m=f"{result.largest:,.1f}"))
        if result.coverage < 1:
            feedback.pushWarning(tr("標高データのない所（{p}%）は補正なし").format(p=round((1 - result.coverage) * 100)))
        return {self.OUTPUT: out}

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        for layer_id in context.layersToLoadOnCompletion():
            details = context.layerToLoadOnCompletionDetails(layer_id)
            if details.outputName == self.OUTPUT:
                post = KeepLook(*self._look)
                _waiting.add(post)
                details.setPostProcessor(post)
        return {}


def default_inputs(layers: list, active=None) -> dict:
    """The dialog's first choice: the active image, and a one-band layer as the DEM (one named like a DEM first)."""
    files = [l for l in layers if isinstance(l, QgsRasterLayer) and file_of(l)]
    if not files:
        return {}
    image = active if active in files else files[0]
    answer = {SimpleOrthoAlgorithm.INPUT: image.id()}
    dems = [l for l in files if l is not image and l.bandCount() == 1]
    dems.sort(key=lambda l: not any(k in l.name().lower() for k in ("dem", "dsm", "dtm", "srtm", "標高", "dted")))
    if dems:
        answer[SimpleOrthoAlgorithm.DEM] = dems[0].id()
    return answer
