"""
「雲量マップ」: the cloud cover forecast as a raster layer, moved through its
times with a slider (or played), or following QGIS's temporal controller.

The source is the settings file's `cloudForecast`: a GFS file (or the newest
of a folder) is opened as it is; the service (points only) is asked for a
grid over the map's extent, written as a file of the same spec
(qgis_io.cloud_layer). Any file of that spec can also be opened.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsRasterLayer
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, Qt, QTimer, QTimeZone
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox, QWidget,
)

from ..core.cloud_cover import map_grid
from ..core.plan.weather import FORECAST_DAYS, PAST_DAYS, fetch_clouds
from ..core.plan.weather_file import forecast_file
from ..qgis_io import settings
from ..qgis_io.cloud_layer import PROPERTY, STYLE_LABELS, CloudFile, cloud_file, open_layer, service_file_path, show_band, write_service_file
from ..qgis_io.context import PluginContext
from ..qgis_io.network import get_bytes
from ..qgis_io.tasks import run_task
from .docks import PluginDock, plain_label

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
#: Milliseconds between frames when played.
FRAME_MS = 700


def time_text(ms: float) -> str:
    """`2026-11-08 06:00 UTC（PC 15:00）`"""
    utc = datetime.fromtimestamp(ms / 1000, timezone.utc)
    local = QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc()).toLocalTime()
    same_day = local.date().toString("yyyy-MM-dd") == utc.strftime("%Y-%m-%d")
    there = local.toString("HH:mm" if same_day else "MM-dd HH:mm")
    return f"{utc.strftime('%Y-%m-%d %H:%M')} UTC（PC {there}）"


def lead_text(ms: float, issued: float) -> str:
    """How far the time is from when the forecast was made: `+6 h`, `−3 h`."""
    if not math.isfinite(issued):
        return ""
    hours = round((ms - issued) / 3600000)
    return f"予報 {'+' if hours >= 0 else '−'}{abs(hours)} h"


class CloudDock(PluginDock):
    def __init__(self, iface, context: PluginContext, open_settings: Callable[[], None], open_tool: Optional[Callable[[], None]] = None,
                 parent: QWidget | None = None):
        super().__init__("SatObsCloud", QCoreApplication.translate("CloudDock", "雲量マップ"), context, open_settings, parent)
        self.iface = iface
        self.open_tool = open_tool
        self.file: Optional[CloudFile] = None
        self.layer_id = ""
        self.task = None
        self._following = False

        self.source = plain_label(QLabel())
        self.source.setWordWrap(True)
        self.body.addWidget(self.source)
        load_row = QHBoxLayout()
        self.load_button = QPushButton(self.tr("読み込み"))
        self.load_button.setToolTip(self.tr("設定ファイルの雲量予報を表示（サービスなら地図の表示範囲を取得）"))
        self.load_button.clicked.connect(self.load)
        self.open_button = QPushButton(self.tr("ファイルを開く…"))
        self.open_button.setToolTip(self.tr("雲量の GeoTIFF（1 バンド＝1 予報時刻、docs/cloud-forecast-file.md）"))
        self.open_button.clicked.connect(self.choose_file)
        load_row.addWidget(self.load_button)
        load_row.addWidget(self.open_button)
        load_row.addStretch(1)
        self.body.addLayout(load_row)
        form = QFormLayout()
        self.past = QSpinBox()
        self.past.setRange(0, PAST_DAYS)
        self.past.setSuffix(self.tr(" 日前から"))
        self.past.setToolTip(self.tr("サービスに問い合わせるとき、今日より前の日数（予報は {days} 日先まで）").format(days=FORECAST_DAYS))
        self.past_label = QLabel(self.tr("過去分"))
        form.addRow(self.past_label, self.past)
        self.style = QComboBox()
        for key, label in STYLE_LABELS.items():
            self.style.addItem(self.tr(label), key)
        self.style.currentIndexChanged.connect(lambda _: self.show_index(self.slider.value()))
        form.addRow(self.tr("表示"), self.style)
        self.body.addLayout(form)

        self.when = QLabel(self.tr("—"))
        self.when.setObjectName("SatObsCloudTime")
        font = self.when.font()
        font.setBold(True)
        self.when.setFont(font)
        self.body.addWidget(self.when)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self.show_index)
        self.body.addWidget(self.slider)
        steps = QHBoxLayout()
        self.prev_button = QPushButton("◀")
        self.prev_button.setToolTip(self.tr("前の時刻"))
        self.prev_button.clicked.connect(lambda: self.slider.setValue(self.slider.value() - 1))
        self.play_button = QPushButton(self.tr("再生"))
        self.play_button.setCheckable(True)
        self.play_button.toggled.connect(self.play)
        self.next_button = QPushButton("▶")
        self.next_button.setToolTip(self.tr("次の時刻"))
        self.next_button.clicked.connect(lambda: self.slider.setValue(self.slider.value() + 1))
        self.now_button = QPushButton(self.tr("現在"))
        self.now_button.setToolTip(self.tr("今に最も近い予報時刻"))
        self.now_button.clicked.connect(self.to_now)
        for b in (self.prev_button, self.play_button, self.next_button, self.now_button):
            b.setEnabled(False)
            steps.addWidget(b)
        steps.addStretch(1)
        self.body.addLayout(steps)
        self.follow = QCheckBox(self.tr("QGIS の時系列コントローラーに連動"))
        self.follow.setToolTip(self.tr("時系列コントローラー（ビュー ▸ パネル ▸ 時系列コントローラー）の時刻に最も近い予報を表示"))
        self.follow.setChecked(True)
        self.body.addWidget(self.follow)
        if open_tool is not None:
            tool = QPushButton(self.tr("レイヤーに雲量を付与…"))
            tool.setToolTip(self.tr("各地物の時刻・場所の雲量を属性に書き込み（処理ツール「雲量を属性に付与」）"))
            tool.clicked.connect(lambda: self.open_tool())
            row = QHBoxLayout()
            row.addWidget(tool)
            row.addStretch(1)
            self.body.addLayout(row)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        self.body.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(FRAME_MS)
        self.timer.timeout.connect(self._step)
        canvas = iface.mapCanvas()
        if hasattr(canvas, "temporalRangeChanged"):
            canvas.temporalRangeChanged.connect(self.follow_controller)
        QgsProject.instance().layersWillBeRemoved.connect(self._layers_removed)
        context.configChanged.connect(self.describe)
        self.describe()

    # The source.
    def describe(self) -> None:
        config = self.context.config.cloud_forecast
        if config is None:
            self.source.setText(self.tr("設定ファイルで雲量予報なし（cloudForecast: false）。ファイルを開いて表示"))
        elif config.local:
            where = config.file or config.folder
            self.source.setText(self.tr("{label}: {where}").format(label=config.label, where=where))
        else:
            self.source.setText(self.tr("{label}（地図の表示範囲を最大 20 × 20 点で取得）").format(label=config.label))
        self.load_button.setEnabled(config is not None)
        online = config is not None and not config.local
        self.past.setVisible(online)
        self.past_label.setVisible(online)

    def load(self) -> None:
        config = self.context.config.cloud_forecast
        if config is None or self.task is not None:
            return
        if config.local:
            try:
                path = forecast_file(config)
            except ValueError as error:
                self.status.setText(str(error))
                return
            self.open_file(path, config.label)
            return
        canvas = self.iface.mapCanvas()
        to_wgs = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WGS84, QgsProject.instance())
        try:
            box = to_wgs.transformBoundingBox(canvas.extent())
        except Exception:  # noqa: BLE001 (an extent beyond the CRS's area)
            box = None
        if box is None or box.isEmpty() or not all(math.isfinite(v) for v in (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())):
            self.status.setText(self.tr("地図の表示範囲を経緯度にできない"))
            return
        step, lons, lats = map_grid(box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
        places = [(lon, lat) for lat in lats for lon in lons]
        past = self.past.value()
        authcfg = settings.auth_for(config.url)
        path = service_file_path()
        self.status.setText(self.tr("{label} に問い合わせ中（{n} 点）…").format(label=config.label, n=len(places)))
        self.load_button.setEnabled(False)

        def work(task):
            forecasts = fetch_clouds(config, places, lambda url: get_bytes(url, authcfg), task.isCanceled, past_days=past)
            task.check()
            return write_service_file(path, step, lons, lats, forecasts)

        def done(file):
            self.task = None
            self.load_button.setEnabled(True)
            self.show_file(file, self.tr("雲量 {label}").format(label=config.label))
            self.status.setText(self.tr("{label}: {w} × {h} 点（{step:g}° 間隔）・{n} 時刻").format(
                label=config.label, w=len(lons), h=len(lats), step=step, n=len(file.bands)))

        def failed(message):
            self.task = None
            self.load_button.setEnabled(True)
            self.status.setText(self.tr("雲量予報を取得できず: {message}").format(message=message))

        self.task = run_task(self.tr("雲量マップの取得"), work, done, failed)

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("雲量の GeoTIFF"), "", self.tr("GeoTIFF (*.tif *.tiff);;すべて (*)"))
        if path:
            self.open_file(path, os.path.basename(path))

    def open_file(self, path: str, label: str) -> None:
        try:
            file = cloud_file(path)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.show_file(file, self.tr("雲量 {label}").format(label=label))
        issued = file.issued
        made = (self.tr("・予報の作成 {t} UTC").format(t=datetime.fromtimestamp(issued / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"))
                if math.isfinite(issued) else "")
        self.status.setText(self.tr("{name}: {n} 時刻").format(name=os.path.basename(path), n=len(file.bands)) + made)

    # The layer.
    def layer(self) -> Optional[QgsRasterLayer]:
        layer = QgsProject.instance().mapLayer(self.layer_id) if self.layer_id else None
        return layer if isinstance(layer, QgsRasterLayer) else None

    def show_file(self, file: CloudFile, name: str) -> None:
        """The file as the dock's layer (replacing the one shown), at the time nearest to now."""
        if not file.bands:
            self.status.setText(self.tr("時刻のあるバンドなし"))
            return
        old = self.layer()
        self.file = file
        index = file.nearest(datetime.now(timezone.utc).timestamp() * 1000)
        try:
            layer = open_layer(file, name, self.style.currentData(), index)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.layer_id = ""
        if old is not None:
            QgsProject.instance().removeMapLayer(old.id())
        QgsProject.instance().addMapLayer(layer)
        self.layer_id = layer.id()
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(file.bands) - 1)
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        for w in (self.slider, self.prev_button, self.play_button, self.next_button, self.now_button):
            w.setEnabled(True)
        self.show_index(index)
        self.follow_controller()

    def show_index(self, index: int) -> None:
        """The band at `index` (time order) drawn, its time told."""
        file, layer = self.file, self.layer()
        if file is None or not file.bands or layer is None:
            return
        index = max(0, min(index, len(file.bands) - 1))
        t, band = file.bands[index]
        show_band(layer, band, self.style.currentData())
        self.when.setText(time_text(t) + ("・" + lead_text(t, file.issued) if math.isfinite(file.issued) else ""))
        base = layer.name().split("（")[0]
        layer.setName(f"{base}（{datetime.fromtimestamp(t / 1000, timezone.utc).strftime('%m-%d %H:%M')} UTC）")

    def to_now(self) -> None:
        if self.file is not None:
            self.slider.setValue(self.file.nearest(datetime.now(timezone.utc).timestamp() * 1000))

    def play(self, on: bool) -> None:
        self.play_button.setText(self.tr("停止") if on else self.tr("再生"))
        if on:
            self.timer.start()
        else:
            self.timer.stop()

    def _step(self) -> None:
        if self.file is None or self.layer() is None:
            self.play_button.setChecked(False)
            return
        self.slider.setValue((self.slider.value() + 1) % (self.slider.maximum() + 1))

    def follow_controller(self) -> None:
        """The band nearest to the temporal controller's time, when it is on and followed."""
        if not self.follow.isChecked() or self.file is None or self.play_button.isChecked():
            return
        canvas = self.iface.mapCanvas()
        try:
            if not canvas.mapSettings().isTemporal():
                return
            begin = canvas.temporalRange().begin()
        except AttributeError:
            return
        if not begin.isValid():
            return
        self.slider.setValue(self.file.nearest(float(begin.toMSecsSinceEpoch())))

    def _layers_removed(self, ids) -> None:
        if self.layer_id and self.layer_id in ids:
            self.layer_id = ""
            self.file = None
            self.play_button.setChecked(False)
            for w in (self.slider, self.prev_button, self.play_button, self.next_button, self.now_button):
                w.setEnabled(False)
            self.when.setText(self.tr("—"))

    def unload(self) -> None:
        self.timer.stop()
        try:
            QgsProject.instance().layersWillBeRemoved.disconnect(self._layers_removed)
        except (TypeError, RuntimeError):
            pass


__all__ = ["CloudDock", "PROPERTY"]
