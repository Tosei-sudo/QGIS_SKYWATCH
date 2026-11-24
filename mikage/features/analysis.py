"""
解析・処理: データ分析, データに質問 (its generative AI given a model in the settings: else its entry is put away), the Processing algorithms' dialogs (パンシャープン, 変化抽出, 自動位置合わせ,
土地被覆分類, 簡易オルソ補正, 測地線バッファ, 正距方位での重心, 移動体と検出の照合) and 「サーバ処理…」.
"""
from __future__ import annotations

from qgis.core import QgsProject, QgsVectorLayer

from ..processing_provider.pansharpen_algorithm import default_inputs
from ..ui.analysis_dock import AnalysisDock
from ..ui.data_chat_dock import TITLE as CHAT_TITLE, DataChatDock
from ..ui.gp_dialog import RemoteProcessingDialog
from .base import DockSpec, Feature, close, tr

#: The vector algorithms of the menu: (icon, algorithm, entry).
VECTOR_ALGORITHMS = (("geodesic_buffer", "mikage:geodesic_buffer", "測地線バッファ…"),
                     ("geodesic_centroid", "mikage:geodesic_centroids", "正距方位での重心…"),
                     ("moving_match", "mikage:moving_match", "移動体と検出の照合…"))


def _layers() -> list:
    return list(QgsProject.instance().mapLayers().values())


class Analysis(Feature):
    COMMANDS = ("open_pansharpen", "open_change", "open_align", "open_classify", "open_simple_ortho", "open_vector_algorithm",
                "open_remote_processing", "open_data_chat")

    def __init__(self, host):
        super().__init__(host)
        #: 「サーバ処理」, once opened.
        host.remote_dialog = None

    def dock_specs(self) -> dict:
        iface, context, settings, window = self.iface, self.context, self.host.open_settings, self.window()
        return {"SatObsAnalysis": DockSpec("データ分析", "analysis", lambda: AnalysisDock(iface, context, settings, window)),
                "SatObsDataChat": DockSpec(CHAT_TITLE, "data_chat",
                                           lambda: DataChatDock(iface, context, settings, window, self._analysis_dock))}

    def connect(self) -> None:
        # データに質問: its entry only while its generative AI is given a model and the person uses it.
        self.context.text_assist.changed.connect(self._chat_offered)
        self._chat_offered()

    def _analysis_dock(self):
        dock = self.docks.get("SatObsAnalysis")
        if dock is not None:
            dock.show()
            dock.raise_()
            if not dock.tables:
                dock.refresh()
        return dock

    def _chat_offered(self) -> None:
        """「データに質問」 in the menu (and its dock) while the settings give it a model and the person uses the generative AI."""
        title = tr(CHAT_TITLE)
        hidden = self.host.hidden_entries
        offered = self.context.text_assist.has("dataChat")
        changed = (title in hidden) == offered
        if offered:
            hidden.discard(title)
        else:
            hidden.add(title)
        if changed:
            self.host.apply_roles()

    def open_data_chat(self, question: str = ""):
        """「データに質問」 shown, with a question asked (`plugin.open_data_chat("…")`, 文で操作's field); None when not offered."""
        dock = self.docks.get("SatObsDataChat")
        if dock is None or not dock.chat.offered:
            return None
        dock.show()
        dock.raise_()
        if question:
            dock.ask(question)
        else:
            dock.field.setFocus()
        return dock

    def add_actions(self) -> None:
        # 「パンシャープン…」: the Processing algorithm's dialog (the algorithm also in the Processing toolbox).
        self.entry("pansharpen", "パンシャープン…", "open_pansharpen")
        self.entry("change", "変化抽出（2 時期）…", "open_change")
        self.entry("align", "自動位置合わせ…", "open_align")
        self.entry("classify", "土地被覆分類…", "open_classify")
        self.entry("simple_ortho", "簡易オルソ補正…", "open_simple_ortho")
        for name, algorithm, label in VECTOR_ALGORITHMS:
            self.entry(name, label, lambda a=algorithm: self.host.open_vector_algorithm(a))
        # 「サーバ処理…」: a task of a geoprocessing / OGC Processes service as a Processing algorithm.
        self.entry("gp", "サーバ処理…", "open_remote_processing")

    def open_pansharpen(self) -> None:
        """The algorithm's dialog, with the finest one-band image as pan and a multi-band one as ms."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        processing.execAlgorithmDialog("mikage:pansharpen", default_inputs(_layers(), self.iface.activeLayer()))

    def open_change(self) -> None:
        """The algorithm's dialog, with the two earliest images (else the lowest two in the list), the earlier as before."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.change_algorithm import default_inputs as change_inputs
        from ..qgis_io.obs_images import observed_image

        processing.execAlgorithmDialog("mikage:change", change_inputs(_layers(), lambda layer: observed_image(layer).time))

    def open_align(self) -> None:
        """The algorithm's dialog, with the active image to align and the finest other as the reference."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.align_algorithm import default_inputs as align_inputs

        processing.execAlgorithmDialog("mikage:align", align_inputs(_layers(), self.iface.activeLayer()))

    def open_classify(self) -> None:
        """The algorithm's dialog, with the active image and a polygon layer for the training."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.classify_algorithm import default_inputs as classify_inputs

        processing.execAlgorithmDialog("mikage:classify", classify_inputs(_layers(), self.iface.activeLayer()))

    def open_simple_ortho(self) -> None:
        """The algorithm's dialog, with the active image and a DEM-like layer."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        from ..processing_provider.ortho_algorithm import default_inputs as ortho_inputs

        processing.execAlgorithmDialog("mikage:simple_ortho", ortho_inputs(_layers(), self.iface.activeLayer()))

    def open_vector_algorithm(self, algorithm: str) -> None:
        """A vector algorithm's dialog, with the active vector layer as its input."""
        import processing  # QGIS's Processing plugin (loaded with QGIS)

        layer = self.iface.activeLayer()
        if algorithm == "mikage:moving_match":
            from ..processing_provider.moving_match_algorithm import default_inputs as match_inputs

            inputs = match_inputs(_layers(), layer)
        else:
            inputs = {"INPUT": layer.id()} if isinstance(layer, QgsVectorLayer) else {}
        processing.execAlgorithmDialog(algorithm, inputs)

    def open_remote_processing(self) -> None:
        host = self.host
        if host.remote_dialog is None:
            host.remote_dialog = RemoteProcessingDialog(self.context, self.window())
        host.remote_dialog.prepare()
        host.remote_dialog.show()
        host.remote_dialog.raise_()

    def unload(self) -> None:
        try:
            self.context.text_assist.changed.disconnect(self._chat_offered)
        except (TypeError, RuntimeError):
            pass
        if "SatObsDataChat" in self.docks:
            self.docks["SatObsDataChat"].close_all()
        if "SatObsAnalysis" in self.docks:
            self.docks["SatObsAnalysis"].close_all()
        close(self.host.remote_dialog)
        self.host.remote_dialog = None
