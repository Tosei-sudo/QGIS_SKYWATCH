"""The Processing provider 「MIKAGE」 (id `mikage`)."""
from __future__ import annotations

import os
from typing import Callable

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from .align_algorithm import AlignAlgorithm
from .append_algorithm import AppendAlgorithm
from .annotation_algorithm import DatasetPointsToBoxesAlgorithm, PointsToBoxesAlgorithm
from .change_algorithm import ChangeAlgorithm
from .classify_algorithm import ClassifyAlgorithm
from .cloud_algorithm import CloudCoverAlgorithm
from .dataset_algorithm import DatasetAlgorithm
from .detect_algorithm import DetectAlgorithm
from . import analysis_algorithm, gp_algorithm
from .geodesic_algorithms import GeodesicBufferAlgorithm, GeodesicCentroidAlgorithm
from .moving_match_algorithm import MovingMatchAlgorithm
from .ortho_algorithm import SimpleOrthoAlgorithm
from .pansharpen_algorithm import PanSharpenAlgorithm
from .pixel_box_algorithm import PixelBoxAlgorithm
from .train_algorithm import TrainAlgorithm
from ..core.roles import ALGORITHM_FEATURES

HERE = os.path.dirname(os.path.dirname(__file__))


class SatObsProvider(QgsProcessingProvider):
    #: Whether the roles in use (担当, core/roles.py) have a feature: the plugin sets it; a tool of a feature they do not
    #: have is left out of the toolbox (refreshAlgorithms after a change). Everything for `qgis_process`.
    allowed: Callable[[str], bool] = staticmethod(lambda _feature: True)

    def id(self) -> str:
        return "mikage"

    def name(self) -> str:
        return QCoreApplication.translate("Mikage", "MIKAGE")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "icon.svg"))

    def loadAlgorithms(self) -> None:  # noqa: N802 (QGIS's name)
        allowed = self.allowed
        for algorithm in (PanSharpenAlgorithm(), ChangeAlgorithm(), AlignAlgorithm(), ClassifyAlgorithm(), SimpleOrthoAlgorithm(),
                          GeodesicBufferAlgorithm(), GeodesicCentroidAlgorithm(), MovingMatchAlgorithm(), CloudCoverAlgorithm(),
                          DetectAlgorithm(), TrainAlgorithm(), DatasetAlgorithm(), PointsToBoxesAlgorithm(),
                          DatasetPointsToBoxesAlgorithm(), PixelBoxAlgorithm(), AppendAlgorithm()):
            feature = ALGORITHM_FEATURES.get(algorithm.name())
            if feature is None or allowed(feature):
                self.addAlgorithm(algorithm)
        for algorithm in analysis_algorithm.registered():
            self.addAlgorithm(algorithm)
        # The services' tasks (「サーバ処理…」): the administrators'.
        if allowed("サーバ処理…"):
            for algorithm in gp_algorithm.registered():
                self.addAlgorithm(algorithm)
