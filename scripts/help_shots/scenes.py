"""
The help's sample images: 練習モード's (mikage/qgis_io/training_images.py,
moved there 2026-11-22), drawn over the mock's 512 px cartoons.

    QT_QPA_PLATFORM=offscreen python3 scenes.py <the mock's data folder>

Also writes basemap.tif there: a plain map of the same coast over the bay,
under the images in the planning steps (the virtual screen has no internet).
Needs QGIS's Python (GDAL, NumPy, Qt).
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qgis.PyQt.QtGui import QGuiApplication  # noqa: E402

from mikage.qgis_io.training_images import draw_images  # noqa: E402


def main(data: str) -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 (QPainter on images wants one)
    draw_images(data, log=print)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
