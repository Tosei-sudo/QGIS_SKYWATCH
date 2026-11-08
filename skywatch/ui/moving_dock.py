"""
The dock 「移動体（AIS・ADS-B）」 (the viewer's moving-dialog.ts): connects
feeds of ships and aircraft (those of the settings file's `movingFeeds`, or
one by URL) and opens history files (CSV / GeoJSON with a time column), each
as a pair of layers (qgis_io.moving_layer). The fields are found by name;
「詳細」 sets them by hand, with the kind of symbol, the polling interval and
how long tracks are drawn and objects kept.

「撮影時刻に同期」 turns QGIS's temporal controller to the half hour up to
the active image's acquisition time, so the feeds show where every object
was when it was taken; 「ライブに復帰」 turns it off again.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from qgis.core import QgsDateTimeRange, QgsProject, QgsRasterLayer, QgsTemporalNavigationObject
from qgis.PyQt.QtCore import QCoreApplication, QDateTime, QTimeZone
from qgis.PyQt.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.moving import FEED_TYPES, KINDS, ROLES, FeedConfig, feed_type_of, feed_url, history_rows
from ..qgis_io.moving_layer import MovingLayer
from .docks import PluginDock

#: How long the temporal controller's range is when set to an acquisition time: the half hour up to it.
ALIGN_WINDOW = 30 * 60_000


def _tr(text: str) -> str:
    return QCoreApplication.translate("MovingDock", text)


class MovingDock(PluginDock):
    """The feeds open, their state, and what connects or opens another."""

    def __init__(self, iface, context, open_settings: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__("SatObsMoving", _tr("移動体（AIS・ADS-B）"), context, open_settings, parent)
        self.iface = iface
        self.layers: list[MovingLayer] = []

        presets = QHBoxLayout()
        self.preset = QComboBox()
        self.preset_button = QPushButton(_tr("接続"))
        self.preset_button.clicked.connect(self.connect_preset)
        presets.addWidget(QLabel(_tr("登録済み")))
        presets.addWidget(self.preset, 1)
        presets.addWidget(self.preset_button)
        self.presets_row = QWidget()
        self.presets_row.setLayout(presets)
        presets.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(self.presets_row)

        by_url = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("wss://… / https://…/StreamServer / https://…/positions.json")
        self.url.returnPressed.connect(self.connect_url)
        self.type = QComboBox()
        self.type.addItem(_tr("自動（URL から）"), "auto")
        for key, label in FEED_TYPES.items():
            self.type.addItem(label, key)
        url_button = QPushButton(_tr("接続"))
        url_button.clicked.connect(self.connect_url)
        by_url.addWidget(self.url, 1)
        by_url.addWidget(self.type)
        by_url.addWidget(url_button)
        self.body.addLayout(by_url)

        history = QHBoxLayout()
        file_button = QPushButton(_tr("履歴ファイルを開く…"))
        file_button.setToolTip(_tr("CSV（時刻・緯度・経度の列）か GeoJSON。時系列コントローラーで時刻を動かすと、その時刻の位置を補間して表示"))
        file_button.clicked.connect(lambda: self.open_history())
        history.addWidget(file_button)
        history.addStretch(1)
        self.body.addLayout(history)

        self.details = QGroupBox(_tr("詳細（種別・間隔・航跡・属性の対応）"))
        self.details.setCheckable(True)
        self.details.setChecked(False)
        inner = QWidget()
        QVBoxLayout(self.details).addWidget(inner)
        form = QFormLayout(inner)
        self.kind = QComboBox()
        self.kind.addItem(_tr("自動"), None)
        for key, label in KINDS.items():
            self.kind.addItem(label, key)
        form.addRow(_tr("種別"), self.kind)

        def minutes(minimum: int) -> QSpinBox:
            box = QSpinBox()
            box.setRange(minimum - 1, 100_000)
            box.setSpecialValueText(_tr("自動"))
            box.setValue(minimum - 1)
            return box

        self.interval = QSpinBox()
        self.interval.setRange(2, 3600)
        self.interval.setValue(10)
        form.addRow(_tr("ポーリング間隔（秒）"), self.interval)
        self.trail, self.stale, self.remove = minutes(0), minutes(0), minutes(1)
        # 0 minutes of trail is a real choice: −1 stands for automatic.
        self.trail.setMinimum(-1)
        self.stale.setMinimum(-1)
        self.trail.setValue(-1)
        self.stale.setValue(-1)
        form.addRow(_tr("航跡（分）"), self.trail)
        form.addRow(_tr("薄くする（分）"), self.stale)
        form.addRow(_tr("消す（分）"), self.remove)
        form.addRow(QLabel(_tr("属性名（空欄は自動。入れ子は MetaData.MMSI のように . で連結）")))
        self.roles: dict[str, QLineEdit] = {}
        for role, label in ROLES.items():
            edit = QLineEdit()
            edit.setPlaceholderText(_tr("自動"))
            self.roles[role] = edit
            form.addRow(label, edit)
        self.details.toggled.connect(inner.setVisible)
        inner.setVisible(False)
        self.body.addWidget(self.details)

        self.body.addWidget(QLabel(_tr("表示中のフィード")))
        self.list = QTreeWidget()
        self.list.setHeaderLabels([_tr("フィード"), _tr("種別"), _tr("状態")])
        self.list.setRootIsDecorated(False)
        self.body.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        self.close_button = QPushButton(_tr("切断"))
        self.close_button.setToolTip(_tr("選んだフィードを切断してレイヤーを削除"))
        self.close_button.clicked.connect(self.close_selected)
        align = QPushButton(_tr("撮影時刻に同期"))
        align.setToolTip(_tr("アクティブな画像の撮影時刻までの 30 分を時系列コントローラーで表示し、その時刻の位置を表示"))
        align.clicked.connect(lambda: self.align_to_image())
        live = QPushButton(_tr("ライブに復帰"))
        live.setToolTip(_tr("時系列コントローラーを止めて、最新の位置に復帰"))
        live.clicked.connect(self.go_live)
        for b in (self.close_button, align, live):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.body.addLayout(buttons)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)

        self.toggleViewAction().triggered.connect(lambda on: on and self.raise_())
        iface.mapCanvas().temporalRangeChanged.connect(self.follow_time)
        context.configChanged.connect(self._presets)
        self._presets()

    # Feeds.

    def _presets(self, *_) -> None:
        feeds = self.context.config.moving_feeds
        self.preset.clear()
        for feed in feeds:
            self.preset.addItem(f"{feed.label}（{FEED_TYPES[feed.type]}）", feed)
        self.presets_row.setVisible(bool(feeds))

    def _base(self) -> Optional[str]:
        """Where relative URLs of the settings file start."""
        source = self.context.source or ""
        return source if source.lower().startswith(("http://", "https://")) else None

    def _settings(self) -> dict:
        if not self.details.isChecked():
            return {}
        return {"trail": self.trail.value() if self.trail.value() >= 0 else None, "stale": self.stale.value() if self.stale.value() >= 0 else None,
                "remove": self.remove.value() if self.remove.value() >= 1 else None}

    def _with_details(self, feed: FeedConfig) -> FeedConfig:
        """The feed with what 「詳細」 sets over it."""
        if not self.details.isChecked():
            return feed
        fields = {**feed.fields, **{role: e.text().strip() for role, e in self.roles.items() if e.text().strip()}}
        return FeedConfig(feed.label, feed.type, feed.url, self.interval.value() if feed.type == "poll" else feed.interval, fields,
                          self.kind.currentData() or feed.kind)

    def connect_preset(self) -> Optional[MovingLayer]:
        feed = self.preset.currentData()
        return self.connect_feed(feed) if feed else None

    def connect_url(self) -> Optional[MovingLayer]:
        text = self.url.text().strip()
        if not text:
            self.note.setText(_tr("フィードの URL を入力"))
            return None
        kind = self.type.currentData()
        kind = feed_type_of(text) if kind == "auto" else kind
        try:
            url = feed_url(text, kind)
        except ValueError as e:
            self.note.setText(str(e))
            return None
        host = url.split("//", 1)[-1].split("/", 1)[0].split("?")[0]
        return self.connect_feed(FeedConfig(host, kind, url, self.interval.value() if kind == "poll" else None))

    def connect_feed(self, feed: FeedConfig) -> MovingLayer:
        """Connects a feed, as a pair of layers on top of the project."""
        layer = MovingLayer(self._with_details(feed), settings=self._settings(), base=self._base(), say=self._say, parent=self)
        self._add(layer)
        self.note.setText(_tr("{label} に接続中…").format(label=feed.label))
        return layer

    def open_history(self, path: Optional[str] = None) -> Optional[MovingLayer]:
        """Opens a history file (CSV / GeoJSON) as a pair of layers."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, _tr("移動体の履歴ファイル"), "", _tr("CSV・GeoJSON (*.csv *.tsv *.txt *.geojson *.json)"))
            if not path:
                return None
        try:
            with open(path, "rb") as f:
                rows = history_rows(f.read(), path)
        except (OSError, ValueError) as e:
            self.note.setText(_tr("{name} を開けず: {e}").format(name=os.path.basename(path), e=e))
            return None
        name = os.path.splitext(os.path.basename(path))[0]
        feed = self._with_details(FeedConfig(name, "poll", path))
        layer = MovingLayer(feed, history=True, settings=self._settings(), say=self._say, parent=self)
        self._add(layer)
        layer.add_rows(rows)
        span = layer.store.range()
        self.note.setText(_tr("{name}: {n} 件の移動体").format(name=name, n=f"{len(layer.store.tracks):,}")
                          + (f"（{_utc(span[0])} 〜 {_utc(span[1])}）" if span else ""))
        self._enable_time(layer)
        return layer

    def _add(self, layer: MovingLayer) -> None:
        QgsProject.instance().addMapLayers([layer.trails, layer.points])
        self.layers.append(layer)
        layer.changed.connect(self._refresh)
        layer.points.willBeDeleted.connect(lambda l=layer: self._gone(l))
        self.follow_time()
        self._refresh()

    def _enable_time(self, layer: MovingLayer) -> None:
        """Lets the temporal controller span a history file's times."""
        span = layer.store.range()
        controller = self.iface.mapCanvas().temporalController()
        if span and isinstance(controller, QgsTemporalNavigationObject) and controller.navigationMode() == QgsTemporalNavigationObject.NavigationMode.NavigationOff:
            controller.setTemporalExtents(QgsDateTimeRange(_qdt(span[0]), _qdt(span[1])))

    def _gone(self, layer: MovingLayer) -> None:
        layer.close()
        if layer in self.layers:
            self.layers.remove(layer)
        trails = layer.trails
        try:
            if QgsProject.instance().mapLayer(trails.id()) is not None:
                QgsProject.instance().removeMapLayer(trails.id())
        except RuntimeError:
            pass
        self._refresh()

    def close_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        layer = self.layers[self.list.indexOfTopLevelItem(item)]
        QgsProject.instance().removeMapLayer(layer.points.id())

    def _refresh(self) -> None:
        self.list.clear()
        for layer in self.layers:
            self.list.addTopLevelItem(QTreeWidgetItem([layer.feed.label, layer.kind_text(), layer.status_text()]))
        self.close_button.setEnabled(bool(self.layers))

    def _say(self, text: str) -> None:
        self.iface.messageBar().pushInfo(_tr("移動体"), text)

    # Time.

    def follow_time(self, *_) -> None:
        """Every feed at the end of the temporal controller's range while it is on, else live."""
        settings = self.iface.mapCanvas().mapSettings()
        t = settings.temporalRange().end().toMSecsSinceEpoch() if settings.isTemporal() and settings.temporalRange().end().isValid() else None
        for layer in self.layers:
            layer.set_time(t)

    def align_to_image(self, layer=None) -> bool:
        """The temporal controller to the half hour up to the active image's acquisition time."""
        from ..qgis_io.obs_images import observed_image

        layer = layer or self.iface.activeLayer()
        if not isinstance(layer, QgsRasterLayer):
            self.note.setText(_tr("撮影時刻を使う画像をレイヤーパネルで選択"))
            return False
        t = observed_image(layer).time
        if t is None:
            self.note.setText(_tr("この画像の撮影時刻が不明（観測記録ドックで入力可能）"))
            return False
        controller = self.iface.mapCanvas().temporalController()
        span = QgsDateTimeRange(_qdt(t - ALIGN_WINDOW), _qdt(t))
        if isinstance(controller, QgsTemporalNavigationObject):
            controller.setNavigationMode(QgsTemporalNavigationObject.NavigationMode.FixedRange)
            controller.setTemporalExtents(span)
        self.iface.mapCanvas().setTemporalRange(span)
        self.note.setText(_tr("{name} の撮影時刻 {t} の位置を表示").format(name=layer.name(), t=_utc(t)))
        return True

    def go_live(self) -> None:
        controller = self.iface.mapCanvas().temporalController()
        if isinstance(controller, QgsTemporalNavigationObject):
            controller.setNavigationMode(QgsTemporalNavigationObject.NavigationMode.NavigationOff)
        self.iface.mapCanvas().setTemporalRange(QgsDateTimeRange(QDateTime(), QDateTime()))
        self.iface.mapCanvas().mapSettings().setIsTemporal(False)
        for layer in self.layers:
            layer.set_time(None)

    def close_all(self) -> None:
        for layer in list(self.layers):
            layer.close()
        try:
            self.iface.mapCanvas().temporalRangeChanged.disconnect(self.follow_time)
        except (TypeError, RuntimeError):
            pass


def _qdt(ms: float) -> QDateTime:
    return QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc())


def _utc(ms: float) -> str:
    from ..core.moving import utc_text

    return utc_text(ms)
