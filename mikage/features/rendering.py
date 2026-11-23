"""
地図の描画: 地図の描画を速くする設定 (`rasterSpeed`: GDAL's options, QGIS's once), MIKAGE の速い描画（試作）,
the overviews of the images added (`overviews`) and complex SAR images shown as amplitude (`complexImages`).
"""
from __future__ import annotations

from typing import Optional

from qgis.core import Qgis, QgsMessageLog, QgsProject
from qgis.PyQt.QtWidgets import QPushButton

from ..core.enhance.api import image_rule_for
from ..core.raster_speed import shown as shown_option
from ..qgis_io import complex_sar, fast_draw, overviews_io, raster_speed
from ..qgis_io.preload import LOG_TAG
from .base import Feature, disconnect, tr


class Rendering(Feature):
    COMMANDS = ("open_raster_speed",)

    def __init__(self, host):
        super().__init__(host)
        #: The settings file read once, QGIS's options looked at (once in a session).
        self._raster_read = False
        self._raster_tuned = False
        self._qgis_started = False
        self.raster_dialog = None
        #: MIKAGE の速い描画（試作）: the raster layers coloured by MIKAGE while the person has it on (qgis_io/fast_draw.py).
        self.fast_draw: Optional[fast_draw.FastDraw] = None
        self._overviews_ready = False
        self._overviews_waiting: list = []

    def start(self) -> None:
        context = self.context
        context.configChanged.connect(self._overviews_changed)
        # 地図の描画を速くする設定 (`rasterSpeed`): GDAL's as the file says, QGIS's options once QGIS has started too.
        context.configChanged.connect(self._raster_speed_changed)
        # MIKAGE の速い描画（試作）: on for those who turned it on in 「地図の描画の速さ」.
        self.fast_draw = fast_draw.FastDraw()
        if fast_draw.on():
            self.fast_draw.start()
        # Images added in any way get overviews too (`overviews.scope` "all"), once the settings file is read;
        # complex SAR images are shown as amplitude first (`complexImages`). Before the docks' own layersAdded (imageRules).
        QgsProject.instance().layersAdded.connect(self._overview_layers_added)

    def qgis_started(self) -> None:
        self._qgis_started = True
        self._tune_qgis()

    # ---- 地図の描画を速くする設定 (qgis_io/raster_speed.py) ---------------------------------------------

    def _raster_speed_changed(self) -> None:
        if self.context is None or self.context.loading:
            return
        options = self.context.config.raster_speed
        raster_speed.remember_options(options)
        raster_speed.apply_gdal(options)
        self._raster_read = True
        self._tune_qgis()

    def _map_canvases(self) -> list:
        try:
            return list(self.iface.mapCanvases())
        except (AttributeError, RuntimeError, TypeError):
            return [self.iface.mapCanvas()]

    def _tune_qgis(self) -> None:
        """QGIS's options slower than its defaults changed once in the profile, once QGIS has started and the settings file is read."""
        if self._raster_tuned or not (self._raster_read and self._qgis_started) or self.context is None:
            return
        self._raster_tuned = True
        changes = raster_speed.apply_qgis_once(self.context.config.raster_speed, self._map_canvases())
        QgsMessageLog.logMessage(tr("地図の描画: {s}").format(s=raster_speed.summary()), LOG_TAG, Qgis.MessageLevel.Info,
                                 notifyUser=False)
        if changes:
            self._tell_qgis_changes(changes)

    def _tell_qgis_changes(self, changes) -> None:
        """The message bar: which of QGIS's options were changed, 「詳しく…」 and 「元に戻す」."""
        bar = self.iface.messageBar()
        item = bar.createMessage(tr("地図の描画"), tr("速く描くため QGIS のオプション {n} 件を変更（{names}）").format(
            n=len(changes), names="・".join(c.short for c in changes)))
        item.setToolTip("\n".join(f"{c.label}: {shown_option(c.key, c.before)} → {shown_option(c.key, c.after)}（{c.why}）"
                                   for c in changes))
        more = QPushButton(tr("詳しく…"), item)
        more.clicked.connect(lambda _on=False: self.host.open_raster_speed())
        item.layout().addWidget(more)
        undo = QPushButton(tr("元に戻す"), item)

        def put_back(_on=False) -> None:
            back = raster_speed.revert_qgis(self._map_canvases())
            try:
                bar.popWidget(item)
            except RuntimeError:
                pass
            bar.pushInfo(tr("地図の描画"), tr("QGIS のオプション {n} 件を復元").format(n=len(back)))

        undo.clicked.connect(put_back)
        item.layout().addWidget(undo)
        bar.pushWidget(item, Qgis.MessageLevel.Info, 0)
        self._raster_message = item

    def open_raster_speed(self, parent=None):
        """「地図の描画の速さ」: what is set, turning it off and on, what only a person can change (modal over `parent`)."""
        from ..ui.raster_speed_dialog import RasterSpeedDialog

        if self.context is None:
            return None
        context = self.context
        if parent is not None:
            dialog = RasterSpeedDialog(lambda: context.config.raster_speed, self._map_canvases, parent, self.fast_draw)
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()
            return None
        if self.raster_dialog is not None:
            self.raster_dialog.deleteLater()
        self.raster_dialog = RasterSpeedDialog(lambda: context.config.raster_speed, self._map_canvases, self.window(),
                                               self.fast_draw)
        self.raster_dialog.show()
        self.raster_dialog.raise_()
        return self.raster_dialog

    # ---- 概観 (overviews) and complex SAR images ------------------------------------------------------

    def _overviews_changed(self) -> None:
        if self.context is None:
            return
        overviews_io.configure_cache(self.context.config.overviews)
        if not self._overviews_ready:
            self._overviews_ready = True
            # Those added before the settings file was read (a project opened at start): its `location` and `scope` apply.
            ids = set(QgsProject.instance().mapLayers())
            waiting = [layer for layer_id, layer in self._overviews_waiting if layer_id in ids]
            self._overviews_waiting = []
            if waiting:
                self._raster_layers_added(waiting)

    def _overview_layers_added(self, layers) -> None:
        if not self._overviews_ready:
            self._overviews_waiting += [(layer.id(), layer) for layer in layers]
            return
        self._raster_layers_added(layers)

    def _complex_mode(self, layer):
        """How a complex SAR image is shown: the first imageRules entry for its file name with `complexImages`, else the settings file's."""
        config = self.context.config if self.context else None
        if config is None:
            return None
        path = overviews_io.local_path(layer)
        rule = image_rule_for(config.image_rules, path) if path and config.image_rules else None
        if rule is not None and rule.complex_images is not None:
            return rule.complex_images or None
        return config.complex_images

    def _raster_layers_added(self, layers) -> None:
        """Complex SAR images first get their amplitude VRT (`complexImages`), whose overviews are then built, not those of I and Q."""
        layers = list(layers)
        complex_sar.layers_added(layers, self._complex_mode, self.iface.messageBar())
        overviews_io.layers_added(layers, self.iface.messageBar())

    def unload(self) -> None:
        # GDAL as it was before the plugin (QGIS's options stay: they are QGIS's own defaults).
        raster_speed.revert_gdal()
        disconnect(self.context.configChanged, self._raster_speed_changed)
        if self.raster_dialog is not None:
            self.raster_dialog.close()
            self.raster_dialog.deleteLater()
            self.raster_dialog = None
        # QGIS's own renderers back on the layers (the plugin's code goes with it).
        if self.fast_draw is not None:
            self.fast_draw.stop()
            self.fast_draw.deleteLater()
            self.fast_draw = None
        disconnect(QgsProject.instance().layersAdded, self._overview_layers_added)
