"""
「自動位置合わせ」 as a Processing algorithm (the viewer's align dialog): an
image is lined up with a reference image without picking points by hand —
phase correlation finds how far it is off (qgis_io.align_io), and the
aligned copy, every band kept, lands on the reference's grid. The result
layer keeps the image's 画像補正.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBand, QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum, QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer, QgsRasterLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..core.align import describe_alignment
from ..qgis_io import enhance_renderer as er
from ..qgis_io.align_io import RESAMPLING, AlignError, aligned_copy, find_alignment
from .pansharpen_algorithm import KeepLook, _waiting, file_of

HERE = os.path.dirname(os.path.dirname(__file__))
HOW_KEYS = ["shift", "affine"]
HOW_LABELS = ["平行移動のみ", "アフィン（回転・伸縮も）"]
PATCHES = [3, 4, 6]
RESAMPLE_KEYS = list(RESAMPLING)
RESAMPLE_LABELS = ["バイリニア", "最近傍（値の変化なし）"]


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class AlignAlgorithm(QgsProcessingAlgorithm):
    REFERENCE = "REFERENCE"
    REFERENCE_BAND = "REFERENCE_BAND"
    TARGET = "TARGET"
    TARGET_BAND = "TARGET_BAND"
    HOW = "HOW"
    PATCHES = "PATCHES"
    RESAMPLE = "RESAMPLE"
    OUTPUT = "OUTPUT"

    def __init__(self):
        super().__init__()
        self._look = ("", "")
        #: What was found (for scripts and tests): the Alignment of the last run.
        self.alignment = None

    def createInstance(self) -> "AlignAlgorithm":  # noqa: N802 (QGIS's name)
        return AlignAlgorithm()

    def name(self) -> str:
        return "align"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("自動位置合わせ")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "align.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "画像の模様を照合（位相相関）して基準の画像とのずれを自動で算出し、ずれを補正した画像を作成。\n\n"
            "重なる範囲全体を粗く照合してから、格子状の数か所を基準の画像の解像度で照合してタイポイントにし、"
            "平行移動（中央値）かアフィン（最小二乗、外れ値は除外）で補正。アフィンは照合できた所が 6 か所以上のときのみ、少なければ平行移動。\n"
            "照合には各画像の選択バンドを使用、結果にはすべてのバンドを保持（基準の画像の格子）。"
            "明るさの違う画像どうしでも照合できるが、模様の少ない画像（水面など）は照合不可。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterRasterLayer(self.REFERENCE, tr("基準の画像")))
        self.addParameter(QgsProcessingParameterBand(self.REFERENCE_BAND, tr("基準のバンド（照合用）"), 1, self.REFERENCE))
        self.addParameter(QgsProcessingParameterRasterLayer(self.TARGET, tr("合わせる画像")))
        self.addParameter(QgsProcessingParameterBand(self.TARGET_BAND, tr("合わせる画像のバンド（照合用）"), 1, self.TARGET))
        self.addParameter(QgsProcessingParameterEnum(self.HOW, tr("直し方"), [tr(x) for x in HOW_LABELS], defaultValue=0))
        advanced = [
            QgsProcessingParameterEnum(self.PATCHES, tr("照合する所（縦横）"), [f"{n} × {n}" for n in PATCHES], defaultValue=1),
            QgsProcessingParameterEnum(self.RESAMPLE, tr("再サンプリング"), [tr(x) for x in RESAMPLE_LABELS], defaultValue=0),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, tr("位置を合わせた画像")))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        reference = self.parameterAsRasterLayer(parameters, self.REFERENCE, context)
        target = self.parameterAsRasterLayer(parameters, self.TARGET, context)
        if reference is None or target is None:
            raise QgsProcessingException(tr("基準の画像と合わせる画像を開く必要あり"))
        if not file_of(reference) or not file_of(target):
            raise QgsProcessingException(tr("ファイルの画像（GDAL で開いたラスター）のみ対応"))
        if reference.id() == target.id() or file_of(reference) == file_of(target):
            raise QgsProcessingException(tr("基準と違う画像を選択"))
        self._files = (file_of(reference), file_of(target))
        self._units = "°" if reference.crs().isGeographic() else " m"
        self._look = (str(target.customProperty(er.PROPERTY) or ""), f"{target.name()}_aligned")
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        out = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        reference, target = self._files
        try:
            feedback.setProgressText(tr("ずれを調査中…"))
            alignment = find_alignment(
                reference, self.parameterAsInt(parameters, self.REFERENCE_BAND, context) or 1,
                target, self.parameterAsInt(parameters, self.TARGET_BAND, context) or 1,
                how=HOW_KEYS[self.parameterAsEnum(parameters, self.HOW, context)],
                patches=PATCHES[self.parameterAsEnum(parameters, self.PATCHES, context)],
                progress=lambda p: feedback.setProgress(80 * p), cancelled=feedback.isCanceled,
            )
            feedback.setProgressText(tr("位置を合わせた画像を作成中…"))
            aligned_copy(reference, target, alignment, out, RESAMPLE_KEYS[self.parameterAsEnum(parameters, self.RESAMPLE, context)])
        except AlignError as e:
            raise QgsProcessingException(tr(str(e)))
        self.alignment = alignment
        feedback.setProgress(100)
        feedback.pushInfo(describe_alignment(alignment.transform, alignment.ties, alignment.tried, alignment.rms_pixels, self._units))
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
    """The dialog's first choice, as the viewer's: the active image to align, the finest other image as the reference."""
    files = [l for l in layers if isinstance(l, QgsRasterLayer) and file_of(l)]
    if len(files) < 2:
        return {}
    target = active if active in files else files[0]
    others = sorted((l for l in files if l is not target), key=lambda l: l.rasterUnitsPerPixelX() * (111_320 if l.crs().isGeographic() else 1))
    return {AlignAlgorithm.TARGET: target.id(), AlignAlgorithm.REFERENCE: others[0].id()}
