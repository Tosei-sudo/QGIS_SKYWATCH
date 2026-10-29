"""The Processing provider 「衛星撮像計画・観測記録」 (id `satobs`)."""
from __future__ import annotations

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from .align_algorithm import AlignAlgorithm
from .change_algorithm import ChangeAlgorithm
from .classify_algorithm import ClassifyAlgorithm
from .detect_algorithm import DetectAlgorithm
from . import gp_algorithm
from .geodesic_algorithms import GeodesicBufferAlgorithm, GeodesicCentroidAlgorithm
from .moving_match_algorithm import MovingMatchAlgorithm
from .ortho_algorithm import SimpleOrthoAlgorithm
from .pansharpen_algorithm import PanSharpenAlgorithm
from .train_algorithm import TrainAlgorithm

HERE = os.path.dirname(os.path.dirname(__file__))


class SatObsProvider(QgsProcessingProvider):
    def id(self) -> str:
        return "satobs"

    def name(self) -> str:
        return QCoreApplication.translate("SatelliteObservation", "衛星撮像計画・観測記録")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "icon.svg"))

    def loadAlgorithms(self) -> None:  # noqa: N802 (QGIS's name)
        self.addAlgorithm(PanSharpenAlgorithm())
        self.addAlgorithm(ChangeAlgorithm())
        self.addAlgorithm(AlignAlgorithm())
        self.addAlgorithm(ClassifyAlgorithm())
        self.addAlgorithm(SimpleOrthoAlgorithm())
        self.addAlgorithm(GeodesicBufferAlgorithm())
        self.addAlgorithm(GeodesicCentroidAlgorithm())
        self.addAlgorithm(MovingMatchAlgorithm())
        self.addAlgorithm(DetectAlgorithm())
        self.addAlgorithm(TrainAlgorithm())
        for algorithm in gp_algorithm.registered():
            self.addAlgorithm(algorithm)
