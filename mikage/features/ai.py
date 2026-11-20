"""
AI: AI結果, 「物体検出（AI）…」「AI 学習（物体検出）…」「教師データセットの作成…」, the annotations'
conversions, 「クリック抽出（AI）」 and 「検出結果を DB へ転記…」.
"""
from __future__ import annotations

from qgis.core import QgsProject

from ..processing_provider import train_algorithm
from ..ui.ai_results_dock import AiResultsDock
from ..ui.segment_tool import SegmentTool
from ..ui.transfer_dialog import TransferDialog
from .base import DockSpec, Feature, close, tr


def _layers() -> list:
    return list(QgsProject.instance().mapLayers().values())


class Ai(Feature):
    COMMANDS = ("open_detect", "open_train", "open_dataset", "open_points_to_boxes", "open_dataset_points_to_boxes", "open_transfer")

    def __init__(self, host):
        super().__init__(host)
        #: 「クリック抽出（AI）」's map tool and 「検出結果を DB へ転記」, once opened.
        host.segment = None
        host.transfer_dialog = None

    def dock_specs(self) -> dict:
        iface, context, settings, window, docks = self.iface, self.context, self.host.open_settings, self.window(), self.docks
        return {"SatObsAiResults": DockSpec("AI結果", "ai_results", lambda: AiResultsDock(
            iface, context, settings, lambda: docks.get("SatObsObservations"), window))}

    def connect(self) -> None:
        # The training reads the records the observation dock read.
        train_algorithm.set_book(lambda: getattr(self.docks.get("SatObsObservations"), "book", None))

    def add_actions(self) -> None:
        # The AI tools: 「物体検出（AI）…」 as a Processing algorithm (its models from the settings file), 「クリック抽出（AI）」 as a map tool.
        self.entry("ai_detect", "物体検出（AI）…", "open_detect")
        self.entry("ai_train", "AI 学習（物体検出）…", "open_train")
        self.entry("ai_dataset", "教師データセットの作成…", "open_dataset")
        # 「アノテーション変換」: point annotations as boxes turned to the object (a layer, or a training dataset).
        self.entry("ai_boxes", "アノテーション変換（点→矩形）…", "open_points_to_boxes")
        self.entry("ai_dataset_boxes", "アノテーション変換（教師データセット）…", "open_dataset_points_to_boxes")
        segment = self.entry("ai_segment", "クリック抽出（AI）", name="SatObsSegment")
        self.host.segment = SegmentTool(self.iface, self.context, segment, self.window())
        self.entry("ai_transfer", "検出結果を DB へ転記…", "open_transfer")

    def open_detect(self) -> None:
        """The algorithm's dialog, with the active image (else the first), the map's extent and the screen's pixel size."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.detect_algorithm import default_inputs as detect_inputs

        processing.execAlgorithmDialog("mikage:detect", detect_inputs(_layers(), self.iface.activeLayer(), self.iface.mapCanvas()))

    def open_train(self) -> None:
        """The training algorithm's dialog, with the active image (else the first)."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.train_algorithm import default_inputs as train_inputs

        processing.execAlgorithmDialog("mikage:train_detector", train_inputs(_layers(), self.iface.activeLayer()))

    def open_dataset(self) -> None:
        """The dialog making a training dataset of images outside the catalog."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        processing.execAlgorithmDialog("mikage:make_dataset", {})

    def open_points_to_boxes(self) -> None:
        """「アノテーション変換（点→矩形）」 with the active point layer and image."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.annotation_algorithm import default_inputs as box_inputs

        processing.execAlgorithmDialog("mikage:points_to_boxes", box_inputs(_layers(), self.iface.activeLayer()))

    def open_dataset_points_to_boxes(self) -> None:
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        processing.execAlgorithmDialog("mikage:dataset_points_to_boxes", {})

    def open_transfer(self) -> None:
        host = self.host
        if host.transfer_dialog is None:
            host.transfer_dialog = TransferDialog(self.context, lambda text: self.iface.messageBar().pushInfo(tr("検出結果を DB へ転記"), text),
                                                  self.window())
        host.transfer_dialog.prepare(self.iface.activeLayer())
        host.transfer_dialog.show()
        host.transfer_dialog.raise_()

    def unload(self) -> None:
        host = self.host
        train_algorithm.set_book(lambda: None)
        close(host.segment)
        host.segment = None
        close(host.transfer_dialog)
        host.transfer_dialog = None
