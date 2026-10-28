"""
「パンシャープン」 as a Processing algorithm: a panchromatic band and a
multispectral image (both opened in QGIS from files GDAL reads) into a
GeoTIFF at the panchromatic resolution (qgis_io.pansharpen_io). Runs in the
background like any algorithm; the result layer gets the multispectral
layer's 画像補正 (bands and correction), as the viewer's result keeps them.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingLayerPostProcessorInterface, QgsProcessingParameterBand,
    QgsProcessingParameterDefinition, QgsProcessingParameterEnum, QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer, QgsRasterLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..core.enhance.pansharpen import METHODS
from ..qgis_io import enhance_renderer as er
from ..qgis_io.pansharpen_io import RESAMPLING, PanSharpenError, pan_sharpen_files

HERE = os.path.dirname(os.path.dirname(__file__))
METHOD_LABELS = ["Gram-Schmidt（推奨・色を維持）", "IHS（くっきり・色が変わることも）", "Brovey（コントラスト強め）"]
RESAMPLE_KEYS = list(RESAMPLING)
RESAMPLE_LABELS = ["バイリニア", "バイキュービック"]
WEIGHT_KEYS = ["auto", "equal"]
WEIGHT_LABELS = ["自動（パンクロに合わせて推定）", "均等"]

#: Post-processors waiting for their layer (QGIS holds them only by pointer).
_waiting: set = set()


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def file_of(layer: QgsRasterLayer) -> str:
    """The file GDAL opens for a layer (its source without QGIS's options); empty when not GDAL."""
    if layer is None or layer.providerType() != "gdal":
        return ""
    return layer.source().split("|")[0]


class KeepLook(QgsProcessingLayerPostProcessorInterface):
    """Puts the multispectral layer's 画像補正 on the result layer when QGIS opens it."""

    def __init__(self, settings: str, name: str):
        super().__init__()
        self.settings = settings
        self.name = name

    def postProcessLayer(self, layer, context, feedback) -> None:  # noqa: N802 (QGIS's name)
        _waiting.discard(self)
        if not isinstance(layer, QgsRasterLayer):
            return
        layer.setName(self.name)
        if self.settings:
            layer.setCustomProperty(er.PROPERTY, self.settings)
            er.restore_layer(layer)


class PanSharpenAlgorithm(QgsProcessingAlgorithm):
    PAN = "PAN"
    PAN_BAND = "PAN_BAND"
    MS = "MS"
    METHOD = "METHOD"
    RESAMPLE = "RESAMPLE"
    STRENGTH = "STRENGTH"
    WEIGHTS = "WEIGHTS"
    OUTPUT = "OUTPUT"

    def __init__(self):
        super().__init__()
        self._look = ("", "")

    def createInstance(self) -> "PanSharpenAlgorithm":  # noqa: N802 (QGIS's name)
        return PanSharpenAlgorithm()

    def name(self) -> str:
        return "pansharpen"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("パンシャープン")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "pansharpen.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "高解像度のパンクロ画像の細部を、低解像度のマルチスペクトル画像に合成。"
            "結果はパンクロの解像度で、マルチスペクトル画像のバンド・型・ノーデータのまま（2 つの画像の重なる範囲）。\n\n"
            "Gram-Schmidt: 色を最もよく維持。IHS: 最もくっきり、色が変わることも。Brovey: コントラスト強め。\n"
            "輝度の重み「自動」はパンクロに合うようにバンドの重みを推定。強さ 100 % で標準、0 % で再サンプリングのみ。\n"
            "結果のレイヤーにはマルチスペクトル画像の画像補正（バンド割当・補正）を引き継ぎ。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterRasterLayer(self.PAN, tr("パンクロ画像")))
        self.addParameter(QgsProcessingParameterBand(self.PAN_BAND, tr("パンクロのバンド"), 1, self.PAN))
        self.addParameter(QgsProcessingParameterRasterLayer(self.MS, tr("マルチスペクトル画像")))
        self.addParameter(QgsProcessingParameterEnum(self.METHOD, tr("方式"), [tr(x) for x in METHOD_LABELS], defaultValue=0))
        advanced = [
            QgsProcessingParameterEnum(self.RESAMPLE, tr("再サンプリング"), [tr(x) for x in RESAMPLE_LABELS], defaultValue=0),
            QgsProcessingParameterNumber(self.STRENGTH, tr("強さ（%）"), QgsProcessingParameterNumber.Type.Double, 100, False, 0, 200),
            QgsProcessingParameterEnum(self.WEIGHTS, tr("輝度の重み"), [tr(x) for x in WEIGHT_LABELS], defaultValue=0),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, tr("パンシャープン画像")))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        pan = self.parameterAsRasterLayer(parameters, self.PAN, context)
        ms = self.parameterAsRasterLayer(parameters, self.MS, context)
        if pan is None or ms is None:
            raise QgsProcessingException(tr("パンクロ画像とマルチスペクトル画像を開く必要あり"))
        if pan.id() == ms.id() or file_of(pan) == file_of(ms):
            raise QgsProcessingException(tr("別々のパンクロ画像とマルチスペクトル画像を選択"))
        if not file_of(pan) or not file_of(ms):
            raise QgsProcessingException(tr("ファイルの画像（GDAL で開いたラスター）のみ対応"))
        self._files = (file_of(pan), file_of(ms))
        self._look = (str(ms.customProperty(er.PROPERTY) or ""), f"{ms.name()}_pansharpen")
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        out = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        pan, ms = self._files
        try:
            result = pan_sharpen_files(
                pan, ms, out,
                pan_band=self.parameterAsInt(parameters, self.PAN_BAND, context) or 1,
                method=METHODS[self.parameterAsEnum(parameters, self.METHOD, context)],
                resample=RESAMPLE_KEYS[self.parameterAsEnum(parameters, self.RESAMPLE, context)],
                strength=self.parameterAsDouble(parameters, self.STRENGTH, context) / 100,
                weights=WEIGHT_KEYS[self.parameterAsEnum(parameters, self.WEIGHTS, context)],
                progress=lambda p, text: (feedback.setProgress(p), feedback.setProgressText(tr(text))),
                cancelled=feedback.isCanceled,
            )
        except PanSharpenError as e:
            raise QgsProcessingException(tr(str(e)))
        feedback.pushInfo(tr("{w} × {h} px、{b} バンド").format(w=f"{result['width']:,}", h=f"{result['height']:,}", b=result["bands"]))
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
    """The dialog's first choice, as the viewer's: the finest one-band image as pan, a multi-band image (the active one first) as ms."""
    files = [l for l in layers if isinstance(l, QgsRasterLayer) and file_of(l)]
    gray = sorted((l for l in files if l.bandCount() == 1), key=lambda l: l.rasterUnitsPerPixelX())
    multi = [l for l in files if l.bandCount() > 1]
    if active in multi:
        multi.remove(active)
        multi.insert(0, active)
    out = {}
    if gray:
        out[PanSharpenAlgorithm.PAN] = gray[0].id()
    if multi:
        out[PanSharpenAlgorithm.MS] = multi[0].id()
    return out
