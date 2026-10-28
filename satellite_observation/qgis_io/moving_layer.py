"""
A layer of moving objects (the viewer's moving-layer.ts): one feed (or
history file) as a memory point layer of ship or aircraft symbols turned to
their heading, labelled (name, else id) when zoomed in, faded when their
last report is old and gone when it is older still, with a line layer of
their tracks of the last minutes beside it.

It follows QGIS's temporal controller: while it is on, the objects stand
where they were at the end of the time shown, interpolated from what the
feed has sent so far (or the history file holds); off, a live feed shows the
latest reports and a history file every object at its last report.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from qgis.core import (
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsMarkerSymbol, QgsPalLayerSettings, QgsPointXY, QgsProperty, QgsRuleBasedRenderer,
    QgsSimpleMarkerSymbolLayer, QgsSingleSymbolRenderer, QgsSvgMarkerSymbolLayer, QgsSymbolLayer, QgsTextBufferSettings, QgsTextFormat,
    QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsLineSymbol,
)
from qgis.PyQt.QtCore import QObject, QTimer, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..core.moving import (
    FEED_TYPES, KINDS, FeedConfig, TrackStore, can_place, detect_fields, display_url, guess_kind, position_at, read_record, records_of,
    trail_of, utc_text,
)
from .moving_feeds import STATUSES, FeedConnection

HERE = os.path.dirname(os.path.dirname(__file__))
COLORS = {"ship": "#1971c2", "aircraft": "#e8590c"}
#: Labels are drawn when zoomed in closer than this scale (about the viewer's 40 m a pixel).
LABEL_SCALE = 150_000
#: The layer is drawn again at most this often for a live feed, ms.
FRAME = 1000
#: Extra attribute columns at most; messages the fields are looked for in before saying they were not found.
MAX_EXTRA_FIELDS = 16
DETECT_TRIES = 20
#: Custom property marking the layers of a feed.
PROPERTY = "satobs/moving"

#: The feed layers open now, by the id of their point layer (for 「移動体と検出の照合」).
OPEN: dict[str, "MovingLayer"] = {}


@dataclass
class MovingSettings:
    """How a feed layer draws and keeps its objects; minutes."""
    trail: float
    stale: float
    remove: float
    keep: float


def default_settings(kind: str) -> MovingSettings:
    """Ships report every few seconds to minutes, aircraft every second."""
    return MovingSettings(5, 1, 5, 360) if kind == "aircraft" else MovingSettings(30, 10, 30, 1440)


CORE_FIELDS = [("id", QVariant.String, "ID"), ("name", QVariant.String, "名前"), ("type", QVariant.String, "種別"),
               ("time", QVariant.String, "最終時刻"), ("lat", QVariant.Double, "緯度"), ("lon", QVariant.Double, "経度"),
               ("heading", QVariant.Double, "方位"), ("course", QVariant.Double, "針路"), ("speed", QVariant.Double, "速度"),
               ("state", QVariant.String, "状態"), ("dir", QVariant.Double, "描画の向き")]


def symbol_renderer(kind: str) -> QgsRuleBasedRenderer:
    """Ship or aircraft symbols turned to "dir", a dot for no direction, faded when old."""
    color = QColor(COLORS[kind])
    root = QgsRuleBasedRenderer.Rule(None)
    pointed = QgsMarkerSymbol()
    svg = QgsSvgMarkerSymbolLayer(os.path.join(HERE, "resources", f"moving_{kind}.svg"), 6)
    svg.setFillColor(color)
    svg.setStrokeColor(QColor("white"))
    svg.setStrokeWidth(0.3)
    svg.setDataDefinedProperty(QgsSymbolLayer.Property.PropertyAngle, QgsProperty.fromField("dir"))
    pointed.changeSymbolLayer(0, svg)
    dot = QgsMarkerSymbol()
    circle = QgsSimpleMarkerSymbolLayer()
    circle.setSize(2.6)
    circle.setColor(color)
    circle.setStrokeColor(QColor("white"))
    dot.changeSymbolLayer(0, circle)
    for symbol in (pointed, dot):
        symbol.setDataDefinedProperty(QgsMarkerSymbol.Property.PropertyOpacity, QgsProperty.fromExpression("if(\"state\" = '古い', 40, 100)"))
    root.appendChild(QgsRuleBasedRenderer.Rule(pointed, 0, 0, '"dir" IS NOT NULL', "向きあり"))
    root.appendChild(QgsRuleBasedRenderer.Rule(dot, 0, 0, '"dir" IS NULL', "向きなし"))
    return QgsRuleBasedRenderer(root)


def labeling() -> QgsVectorLayerSimpleLabeling:
    settings = QgsPalLayerSettings()
    settings.fieldName = 'coalesce("name", "id")'
    settings.isExpression = True
    settings.scaleVisibility = True
    settings.maximumScale = 0
    settings.minimumScale = LABEL_SCALE
    text = QgsTextFormat()
    text.setSize(9)
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(1)
    text.setBuffer(buffer)
    settings.setFormat(text)
    settings.dist = 2
    return QgsVectorLayerSimpleLabeling(settings)


class MovingLayer(QObject):
    """A feed's store, its connection (none for a history file) and its two layers; `changed` when its state or counts change."""
    changed = pyqtSignal()

    def __init__(self, feed: FeedConfig, history: bool = False, settings: Optional[dict] = None, base: Optional[str] = None,
                 say: Optional[Callable[[str], None]] = None, now: Optional[Callable[[], float]] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.feed = feed
        self.history = history
        self.store = TrackStore()
        self.kind = feed.kind or "ship"
        self._kind_fixed = bool(feed.kind)
        self._settings_given = dict(settings or {})
        self.settings = self._settings_for(self.kind)
        self.given = dict(feed.fields)
        self.mapping = dict(self.given)
        self.status = "open" if history else "connecting"
        self.status_detail = ""
        self.received = 0
        self.skipped = 0
        self.time: Optional[float] = None
        self._say = say or (lambda _m: None)
        self._now = now or (lambda: time.time() * 1000)
        self._keys: dict = {}
        self._tries = 0
        self._extra: list[str] = []
        self._last_frame = 0.0
        self._frame = QTimer(self)
        self._frame.setSingleShot(True)
        self._frame.timeout.connect(self.render)
        fields = QgsFields()
        for name, kind, alias in CORE_FIELDS:
            f = QgsField(name, kind)
            f.setAlias(alias)
            fields.append(f)
        self.points = QgsVectorLayer("Point?crs=EPSG:4326", feed.label, "memory")
        self.points.dataProvider().addAttributes(fields.toList())
        self.points.updateFields()
        for i, (_, _, alias) in enumerate(CORE_FIELDS):
            self.points.setFieldAlias(i, alias)
        self.trails = QgsVectorLayer("LineString?crs=EPSG:4326&field=id:string", f"{feed.label} の航跡", "memory")
        for layer in (self.points, self.trails):
            layer.setCustomProperty(PROPERTY, feed.label)
            layer.setReadOnly(True)
        self.points.setLabeling(labeling())
        self.points.setLabelsEnabled(True)
        self._style()
        info = "移動体の履歴ファイル" if history else f"移動体のフィード（{FEED_TYPES[feed.type]}）・{display_url(feed.url)}"
        self.points.setAbstract(info)
        OPEN[self.points.id()] = self
        self.points.willBeDeleted.connect(self.close)
        self.connection: Optional[FeedConnection] = None
        self._ticker: Optional[QTimer] = None
        if not history:
            self.connection = FeedConnection(feed, base, self)
            self.connection.message.connect(self.ingest)
            self.connection.status.connect(self._status)
            self.connection.fields.connect(self._stream_fields)
            # Objects fade and go as time passes, and old reports are let go.
            self._ticker = QTimer(self)
            self._ticker.timeout.connect(self._tick)
            self._ticker.start(5000)

    def _settings_for(self, kind: str) -> MovingSettings:
        s = default_settings(kind)
        for k, v in self._settings_given.items():
            if v is not None:
                setattr(s, k, v)
        return s

    def _style(self) -> None:
        self.points.setRenderer(symbol_renderer(self.kind))
        line = QgsLineSymbol.createSimple({"line_color": COLORS[self.kind], "line_width": "0.4"})
        line.setOpacity(0.65)
        self.trails.setRenderer(QgsSingleSymbolRenderer(line))

    def _status(self, state: str, detail: str) -> None:
        self.status, self.status_detail = state, detail
        self.changed.emit()

    def _stream_fields(self, fields: dict) -> None:
        for role, name in fields.items():
            self.given.setdefault(role, name)
        self.mapping = {**self.mapping, **self.given}

    def _tick(self) -> None:
        self.store.prune(self._now() - self.settings.keep * 60_000)
        self.schedule()

    def set_time(self, t: Optional[float]) -> None:
        """Stands the objects where they were at `t` (the temporal controller), or back to live with None."""
        if t == self.time:
            return
        self.time = t
        self.render()
        self.changed.emit()

    def ingest(self, data, received_at: float) -> None:
        """Reads one message of the feed."""
        self.add_rows(records_of(data), received_at)

    def add_rows(self, rows: list[dict], received_at: Optional[float] = None) -> None:
        """Adds reports (flattened records), finding the fields of each role first."""
        if not rows:
            return
        received_at = self._now() if received_at is None else received_at
        if not can_place(self.mapping):
            for row in rows[:50]:
                self._keys.update(dict.fromkeys(row))
            self.mapping = detect_fields(self._keys, self.given)
            if not self._kind_fixed:
                kind = guess_kind(self._keys)
                if kind != self.kind:
                    self.kind = kind
                    self.settings = self._settings_for(kind)
                    self._style()
            if not can_place(self.mapping):
                self.skipped += len(rows)
                self._tries += 1
                if self._tries == DETECT_TRIES or self.history:
                    missing = [{"id": "ID", "lat": "緯度", "lon": "経度"}[r] for r in ("id", "lat", "lon") if not self.mapping.get(r)]
                    self.status_detail = f"{'・'.join(missing)}の属性なし（「詳細」で指定）"
                    self._say(f"{self.feed.label}: {self.status_detail}")
                self.changed.emit()
                return
            self.status_detail = ""
            self._add_extra_fields(rows)
        added = 0
        for row in rows:
            record = read_record(row, self.mapping, received_at)
            if record is None:
                self.skipped += 1
                continue
            if self.store.add(record):
                added += 1
        self.received += added
        if self.history:
            self.render()
        else:
            self.schedule()
        self.changed.emit()

    def _add_extra_fields(self, rows: list[dict]) -> None:
        """Columns for the other values of the first reports (numbers and text), a few at most."""
        mapped = set(self.mapping.values())
        taken = {f.name() for f in self.points.fields()}
        types: dict[str, QVariant.Type] = {}
        for row in rows[:50]:
            for k, v in row.items():
                if k in mapped or k in ("_lon", "_lat") or k in taken or v is None:
                    continue
                kind = QVariant.Double if isinstance(v, (int, float)) and not isinstance(v, bool) else QVariant.String
                was = types.get(k)
                types[k] = QVariant.String if was is not None and was != kind else kind
        extra = list(types.items())[:MAX_EXTRA_FIELDS]
        if extra:
            self.points.dataProvider().addAttributes([QgsField(k, kind) for k, kind in extra])
            self.points.updateFields()
            self._extra = [k for k, _ in extra]

    def schedule(self) -> None:
        """Draws again soon (at most once a second)."""
        if self._frame.isActive():
            return
        self._frame.start(int(max(0, FRAME - (time.monotonic() * 1000 - self._last_frame))))

    def shown_time(self) -> float:
        span = self.store.range()
        return self.time if self.time is not None else (span[1] if self.history and span else self._now())

    def render(self) -> None:
        """Puts every object where it is at the time shown, with its trail, and drops those gone."""
        self._last_frame = time.monotonic() * 1000
        live = self.time is None and not self.history
        at = self.shown_time()
        s = self.settings
        # A history file without a time shows every object at its last report, faded when it is old.
        hold = float("inf") if self.time is None and self.history else s.remove * 60_000
        fields = self.points.fields()
        points, lines = [], []
        for track in self.store.tracks.values():
            if not track.fixes:
                continue
            last = track.fixes[-1]
            if live:
                p = position_at(track, last.t, hold=0)
                p.age = max(0.0, at - last.t)
            else:
                p = position_at(track, at, hold=hold)
            if p is None or p.age > hold:
                continue
            faded = p.age > s.stale * 60_000
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p.lon, p.lat)))
            values = {
                "id": track.id, "name": track.name, "type": track.kind, "time": utc_text(last.t if live else at - p.age),
                "lat": round(p.lat, 6), "lon": round(p.lon, 6), "heading": track.heading, "course": track.course,
                "speed": None if p.speed is None else round(p.speed, 1), "state": "補間" if p.interpolated else "古い" if faded else "最新",
                "dir": p.dir,
            }
            for k in self._extra:
                v = track.attrs.get(k)
                values[k] = str(v).lower() if isinstance(v, bool) else v
            f.setAttributes([values.get(fields.at(i).name()) for i in range(fields.count())])
            points.append(f)
            if s.trail > 0:
                trail = trail_of(track, at - s.trail * 60_000, at, (p.lon, p.lat))
                if len(trail) >= 2:
                    line = QgsFeature(self.trails.fields())
                    line.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in trail]))
                    line.setAttributes([track.id])
                    lines.append(line)
        for layer, features in ((self.points, points), (self.trails, lines)):
            provider = layer.dataProvider()
            provider.truncate()
            provider.addFeatures(features)
            layer.updateExtents()
            layer.triggerRepaint()

    def status_text(self) -> str:
        """The state in words."""
        parts = ["履歴" if self.history else STATUSES.get(self.status, self.status), f"{len(self.store.tracks):,} 件"]
        if self.time is not None:
            parts.append(f"{utc_text(self.time)} の位置")
        if self.store.dropped:
            parts.append(f"上限で {self.store.dropped:,} 件を除外")
        if self.status_detail:
            parts.append(self.status_detail)
        return "・".join(parts)

    def kind_text(self) -> str:
        return KINDS[self.kind]

    def close(self) -> None:
        """Disconnects (the layer was removed, or the plugin unloaded)."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self._ticker is not None:
            self._ticker.stop()
        self._frame.stop()
        if self.status != "closed":
            self.status = "closed"
            self.changed.emit()
        for k in [k for k, v in OPEN.items() if v is self]:
            del OPEN[k]
