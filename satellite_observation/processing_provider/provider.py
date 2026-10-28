"""The Processing provider 「衛星撮像計画・観測記録」 (id `satobs`)."""
from __future__ import annotations

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from .change_algorithm import ChangeAlgorithm
from .pansharpen_algorithm import PanSharpenAlgorithm

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
