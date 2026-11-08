"""The Processing provider 「SKYWATCH」 (id `skywatch`)."""
from __future__ import annotations

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from .align_algorithm import AlignAlgorithm
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
from .train_algorithm import TrainAlgorithm

HERE = os.path.dirname(os.path.dirname(__file__))


class SatObsProvider(QgsProcessingProvider):
    def id(self) -> str:
        return "skywatch"

    def name(self) -> str:
        return QCoreApplication.translate("Skywatch", "SKYWATCH")

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
        self.addAlgorithm(CloudCoverAlgorithm())
        self.addAlgorithm(DetectAlgorithm())
        self.addAlgorithm(TrainAlgorithm())
        self.addAlgorithm(DatasetAlgorithm())
        for algorithm in analysis_algorithm.registered():
            self.addAlgorithm(algorithm)
        for algorithm in gp_algorithm.registered():
            self.addAlgorithm(algorithm)
