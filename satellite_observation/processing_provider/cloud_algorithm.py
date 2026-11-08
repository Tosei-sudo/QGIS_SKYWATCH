"""
「雲量を属性に付与」 as a Processing algorithm: for each feature of a layer,
the forecast cloud cover (percent) at the time in one of its attributes and
at its place, written into a field of the layer (added when missing).

The place: a point itself; a line's middle; a polygon's grid cells (0.25°,
the mean of those inside, else its point on the surface). The forecast: the
settings file's `cloudForecast` (the service, or the GFS file or folder of
docs/cloud-forecast-file.md, every file of a folder joined so past days are
told too), or a GeoTIFF of that spec chosen here. The cover between the
forecast times either side, or at the nearest.

The forecast is read in the background; the values are written on the main
thread when it ends (postProcessAlgorithm), into the layer's edit buffer
when it is being edited (saved by the person), else saved at once.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsField, QgsGeometry, QgsPointXY, QgsProcessing,
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputNumber, QgsProcessingOutputVectorLayer,
    QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterField, QgsProcessingParameterFile,
    QgsProcessingParameterString, QgsProcessingParameterVectorLayer, QgsProject, QgsVectorDataProvider, QgsVectorLayer,
    QgsVectorLayerFeatureSource, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QDate, QDateTime, Qt, QTime, QTimeZone, QVariant
from qgis.PyQt.QtGui import QIcon

from ..core.cloud_cover import INTERPOLATE, NEAREST, cells_in, has_zone, mean_cover
from ..core.obs.image_time import parse_time
from ..core.plan.weather import FORECAST_DAYS, PAST_DAYS, CloudForecastConfig, fetch_clouds

HERE = os.path.dirname(os.path.dirname(__file__))
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
DAY = 86400000
METHODS = [INTERPOLATE, NEAREST]
METHOD_LABELS = ["前後の予報時刻から補間", "最も近い予報時刻の値"]
ZONES = ["local", "utc"]
ZONE_LABELS = ["PC のタイムゾーン", "UTC"]
SOURCES = ["config", "file"]
SOURCE_LABELS = ["設定ファイルの雲量予報（cloudForecast）", "GeoTIFF を指定（docs/cloud-forecast-file.md の形式）"]
DEFAULT_FIELD = "cloud_pct"

#: The settings file's forecast, given by the plugin.
_forecast: Callable[[], Optional[CloudForecastConfig]] = lambda: None


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def set_forecast(forecast: Callable[[], Optional[CloudForecastConfig]]) -> None:
    """Where the algorithm finds the settings file's `cloudForecast`."""
    global _forecast
    _forecast = forecast


def _local_offset_ms(ms: float) -> float:
    """The PC's offset from UTC at a time, ms."""
    try:
        offset = datetime.fromtimestamp(ms / 1000).astimezone().utcoffset()
    except (OverflowError, OSError, ValueError):
        return 0.0
    return offset.total_seconds() * 1000 if offset else 0.0


def feature_time(value, zone: str = "local") -> Optional[float]:
    """
    A feature's time (ms) from its attribute: a date-time, a date (its noon),
    ms since 1970 or text. A time without a zone is taken in `zone` (the
    PC's, or UTC).
    """
    if isinstance(value, QDateTime):
        if not value.isValid():
            return None
        if zone == "utc" and value.timeSpec() == Qt.TimeSpec.LocalTime:
            value = QDateTime(value.date(), value.time(), QTimeZone.utc())
        return float(value.toMSecsSinceEpoch())
    if isinstance(value, QDate):
        if not value.isValid():
            return None
        return feature_time(QDateTime(value, QTime(12, 0)), zone)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc) if zone == "utc" else value.astimezone()
        return value.timestamp() * 1000
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        # Seconds or ms since 1970 (ms from 2001 on, seconds up to year 33658).
        return v * 1000 if abs(v) < 1e12 else v if math.isfinite(v) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.replace(".", "", 1).isdigit():
            return feature_time(float(text), zone)
        t = parse_time(text)
        if t is None:
            return None
        if zone == "utc" and not has_zone(text):
            t += _local_offset_ms(t)
        return float(t)
    return None


def places_of(geometry: QgsGeometry) -> list[tuple[float, float]]:
    """A WGS 84 geometry's places to read: a polygon's grid cells (else its point on the surface), a point, a line's middle."""
    if geometry is None or geometry.isEmpty():
        return []
    kind = geometry.type()
    if kind == QgsWkbTypes.GeometryType.PolygonGeometry:
        box = geometry.boundingBox()
        engine = QgsGeometry.createGeometryEngine(geometry.constGet())
        engine.prepareGeometry()
        def inside(lon: float, lat: float) -> bool:
            point = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
            return engine.intersects(point.constGet())

        cells = cells_in((box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()), inside)
        if cells:
            return cells
        anchor = geometry.pointOnSurface()
    elif kind == QgsWkbTypes.GeometryType.LineGeometry:
        anchor = geometry.interpolate(geometry.length() / 2)
    elif kind == QgsWkbTypes.GeometryType.PointGeometry:
        anchor = geometry.centroid() if geometry.isMultipart() else geometry
    else:
        return []
    if anchor is None or anchor.isEmpty():
        return []
    p = anchor.asPoint()
    return [(p.x(), p.y())]


class CloudCoverAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    TIME_FIELD = "TIME_FIELD"
    ZONE = "ZONE"
    FIELD = "FIELD"
    METHOD = "METHOD"
    SOURCE = "SOURCE"
    FILE = "FILE"
    SELECTED = "SELECTED"
    OUTPUT = "OUTPUT"
    FILLED = "FILLED"
    MISSING = "MISSING"

    def __init__(self):
        super().__init__()
        self._layer_id = ""
        self._source = None
        self._field = DEFAULT_FIELD
        self._values: dict = {}
        self._ids: list = []

    def createInstance(self) -> "CloudCoverAlgorithm":  # noqa: N802 (QGIS's name)
        return CloudCoverAlgorithm()

    def name(self) -> str:
        return "cloud_cover"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("雲量を属性に付与")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "cloud_cover.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "レイヤーの各地物に、時刻の属性の日時・ジオメトリの場所での雲量予報（％）を属性として書き込み（属性がなければ追加）。\n\n"
            "場所: ポイントはその点、ラインは中点、ポリゴンは内側の 0.25° 格子の平均（小さいポリゴンは内側の 1 点）。\n"
            "雲量予報: 設定ファイルの cloudForecast（サービスなら過去 {past} 日〜{days} 日先、GFS のフォルダならフォルダ内の全ファイルをつないで過去分も）、"
            "または指定した GeoTIFF。予報の範囲外の地物は空（NULL）。\n"
            "編集中のレイヤーは編集バッファへ書き込み（保存は手動）、それ以外はすぐ保存。"
        ).format(past=PAST_DAYS, days=FORECAST_DAYS)

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterVectorLayer(self.INPUT, tr("レイヤー"), [QgsProcessing.SourceType.TypeVectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterField(self.TIME_FIELD, tr("時刻の属性"), parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterEnum(self.ZONE, tr("タイムゾーンのない時刻"), [tr(x) for x in ZONE_LABELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(self.FIELD, tr("書き込む属性名"), DEFAULT_FIELD))
        self.addParameter(QgsProcessingParameterEnum(self.METHOD, tr("時刻の合わせ方"), [tr(x) for x in METHOD_LABELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(self.SOURCE, tr("雲量予報"), [tr(x) for x in SOURCE_LABELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(self.FILE, tr("GeoTIFF（指定するとき）"), extension="tif", optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.SELECTED, tr("選択した地物のみ"), False))
        self.addOutput(QgsProcessingOutputVectorLayer(self.OUTPUT, tr("書き込んだレイヤー")))
        self.addOutput(QgsProcessingOutputNumber(self.FILLED, tr("雲量を書き込んだ地物数")))
        self.addOutput(QgsProcessingOutputNumber(self.MISSING, tr("予報の範囲外・時刻なしの地物数")))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(tr("レイヤーを選択"))
        time_field = self.parameterAsString(parameters, self.TIME_FIELD, context)
        if not time_field or layer.fields().indexOf(time_field) < 0:
            raise QgsProcessingException(tr("時刻の属性を選択"))
        name = (self.parameterAsString(parameters, self.FIELD, context) or "").strip()
        if not name:
            raise QgsProcessingException(tr("書き込む属性名を入力"))
        if name == time_field:
            raise QgsProcessingException(tr("書き込む属性名が時刻の属性と同じ"))
        index = layer.fields().indexOf(name)
        if index >= 0 and layer.fields().at(index).type() not in (QVariant.Double, QVariant.Int, QVariant.LongLong, QVariant.String):
            raise QgsProcessingException(tr("属性「{name}」が数値・文字列でない").format(name=name))
        caps = layer.dataProvider().capabilities()
        if not caps & QgsVectorDataProvider.Capability.ChangeAttributeValues:
            raise QgsProcessingException(tr("このレイヤーは属性を書き換えられない"))
        if index < 0 and not caps & QgsVectorDataProvider.Capability.AddAttributes:
            raise QgsProcessingException(tr("このレイヤーには属性「{name}」を追加できない（先に属性を作成）").format(name=name))
        source = SOURCES[self.parameterAsEnum(parameters, self.SOURCE, context)]
        if source == "file":
            path = self.parameterAsFile(parameters, self.FILE, context)
            if not path:
                raise QgsProcessingException(tr("GeoTIFF を指定"))
            if not os.path.isfile(path):
                raise QgsProcessingException(tr("GeoTIFF がない: {path}").format(path=path))
            self._config = CloudForecastConfig("", os.path.basename(path), file=path)
        else:
            self._config = _forecast()
            if self._config is None:
                raise QgsProcessingException(tr("設定ファイルで雲量予報が使われていない（cloudForecast: false）。GeoTIFF を指定"))
        self._auth = None
        if not self._config.local:
            from ..qgis_io import settings

            self._auth = settings.auth_for(self._config.url)
        self._layer_id = layer.id()
        self._field = name
        self._time_field = time_field
        self._zone = ZONES[self.parameterAsEnum(parameters, self.ZONE, context)]
        self._method = METHODS[self.parameterAsEnum(parameters, self.METHOD, context)]
        self._selected = self.parameterAsBoolean(parameters, self.SELECTED, context)
        self._selection = list(layer.selectedFeatureIds()) if self._selected else None
        if self._selected and not self._selection:
            raise QgsProcessingException(tr("選択した地物なし"))
        self._source = QgsVectorLayerFeatureSource(layer)
        self._to_wgs = QgsCoordinateTransform(layer.crs(), WGS84, context.transformContext())
        self._values = {}
        self._ids = []
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        request = QgsFeatureRequest()
        if self._selection is not None:
            request.setFilterFids(self._selection)
        features = []  # (fid, time, places)
        for f in self._source.getFeatures(request):
            if feedback.isCanceled():
                break
            places = []
            if f.hasGeometry():
                g = QgsGeometry(f.geometry())
                try:
                    g.transform(self._to_wgs)
                except Exception:  # noqa: BLE001 (a feature outside the CRS's area gets no cover)
                    g = None
                places = places_of(g) if g is not None else []
            features.append((f.id(), feature_time(f[self._time_field], self._zone), places))
        self._ids = [fid for fid, _, _ in features]
        wanted = [p for _, t, ps in features if t is not None for p in ps]
        feedback.pushInfo(tr("地物 {n} 件・読む格子 {c} 点").format(n=f"{len(features):,}", c=f"{len({(round(x * 4), round(y * 4)) for x, y in wanted}):,}"))
        forecasts = {}
        if wanted:
            config = self._config
            past = 0
            if not config.local:
                times = [t for _, t, _ in features if t is not None]
                now = datetime.now(timezone.utc).timestamp() * 1000
                past = min(PAST_DAYS, max(0, math.ceil((now - min(times)) / DAY) + 1)) if times and min(times) < now else 0
                feedback.pushInfo(tr("{label} に問い合わせ（過去 {past} 日〜{days} 日先）").format(label=config.label, past=past, days=FORECAST_DAYS))
            else:
                feedback.pushInfo(tr("{label} を読み込み").format(label=config.label))
            fetch = None
            if not config.local:
                from ..qgis_io.network import get_bytes, on_main_thread

                def fetch(url: str) -> bytes:
                    if on_main_thread():
                        raise QgsProcessingException(tr("サービスへの問い合わせはバックグラウンドで実行（処理ツールボックスのダイアログから）"))
                    return get_bytes(url, self._auth)
            try:
                forecasts = fetch_clouds(config, wanted, fetch, feedback.isCanceled, past_days=past, every_file=True)
            except QgsProcessingException:
                raise
            except Exception as error:  # noqa: BLE001 (a file or service that cannot be read is told)
                raise QgsProcessingException(tr("雲量予報を読めない: {error}").format(error=str(error) or type(error).__name__)) from error
            issued = getattr(forecasts, "issued", math.nan)
            if math.isfinite(issued):
                feedback.pushInfo(tr("予報の作成時刻 {t} UTC").format(t=datetime.fromtimestamp(issued / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")))
        filled = no_time = 0
        for fid, t, places in features:
            v = mean_cover(forecasts, places, t, self._method) if t is not None and places else None
            if t is None:
                no_time += 1
            if v is not None:
                filled += 1
                v = round(v, 1)
            self._values[fid] = v
        missing = len(features) - filled
        feedback.pushInfo(tr("雲量あり {f} 件・範囲外 {o} 件").format(f=f"{filled:,}", o=f"{missing - no_time:,}")
                          + (tr("・時刻なし {n} 件").format(n=f"{no_time:,}") if no_time else ""))
        self._filled, self._missing = filled, missing
        return {self.OUTPUT: self._layer_id, self.FILLED: filled, self.MISSING: missing}

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        layer = QgsProject.instance().mapLayer(self._layer_id) or context.getMapLayer(self._layer_id)
        if not isinstance(layer, QgsVectorLayer):
            raise QgsProcessingException(tr("レイヤーが閉じられたため書き込めず"))
        write(layer, self._field, self._values)
        feedback.pushInfo(tr("属性「{name}」に書き込み（{n} 件）").format(name=self._field, n=f"{len(self._values):,}")
                          + (tr("。編集中のため保存は手動") if layer.isEditable() else ""))
        return {self.OUTPUT: self._layer_id, self.FILLED: self._filled, self.MISSING: self._missing}


def write(layer: QgsVectorLayer, name: str, values: dict) -> None:
    """
    The values (by feature id; None for none) into a field (a Double added when
    missing): in the edit buffer when the layer is being edited, else saved.
    """
    editing = layer.isEditable()
    if not editing and not layer.startEditing():
        raise QgsProcessingException(tr("レイヤーを編集できない"))
    try:
        index = layer.fields().indexOf(name)
        if index < 0:
            new = QgsField(name, QVariant.Double, "double", 5, 1)
            new.setAlias(tr("雲量（％）"))
            if not layer.addAttribute(new):
                raise QgsProcessingException(tr("属性「{name}」を追加できない").format(name=name))
            index = layer.fields().indexOf(name)
            if index < 0:
                raise QgsProcessingException(tr("属性「{name}」を追加できない").format(name=name))
        kind = layer.fields().at(index).type()
        for fid, v in values.items():
            if v is not None and kind in (QVariant.Int, QVariant.LongLong):
                v = int(round(v))
            elif v is not None and kind == QVariant.String:
                v = f"{v:g}"
            layer.changeAttributeValue(fid, index, v)
        if not editing and not layer.commitChanges():
            errors = "; ".join(layer.commitErrors())
            layer.rollBack()
            raise QgsProcessingException(tr("保存できない: {errors}").format(errors=errors))
    except Exception:
        if not editing and layer.isEditable():
            layer.rollBack()
        raise


def default_inputs(layers: list, active=None) -> dict:
    """The dialog's first choice: the active vector layer (else the first), and its first date-time field (else one named like a time)."""
    vectors = [l for l in layers if isinstance(l, QgsVectorLayer) and l.isSpatial()]
    if not vectors:
        return {}
    layer = active if active in vectors else vectors[0]
    answer = {CloudCoverAlgorithm.INPUT: layer.id()}
    fields = layer.fields()
    dated = [f.name() for f in fields if f.type() in (QVariant.DateTime, QVariant.Date)]
    named = [f.name() for f in fields if any(k in f.name().lower() for k in ("time", "date", "日時", "時刻"))]
    if dated or named:
        answer[CloudCoverAlgorithm.TIME_FIELD] = (dated or named)[0]
    return answer
