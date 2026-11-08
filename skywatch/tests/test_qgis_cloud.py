"""「雲量を属性に付与」 (the Processing algorithm) and 「雲量マップ」 (the dock) in QGIS."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsDateTimeRange, QgsRectangle, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
    QgsRasterLayer, QgsSettings, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, QTimeZone  # noqa: E402

from skywatch.core.plan.weather import CloudForecastConfig, CloudForecasts, CloudSeries, key_of  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.qgis_io.cloud_layer import PROPERTY, cloud_file  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from skywatch.tests.test_weather_file import _write  # noqa: E402

HOUR = 3600000
REGION = ((99.875, 0.25, 0, 40.125, 0, -0.25), 41, 41)
T0 = datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp() * 1000


def qtime(ms):
    return QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc())


@pytest.fixture
def plugin(tmp_path):
    from skywatch import classFactory

    QgsSettings().remove("skywatch")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p, iface, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("skywatch")


def run(params):
    algorithm = QgsApplication.processingRegistry().algorithmById("skywatch:cloud_cover")
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run(params, context, feedback)
    return results, ok, feedback


def layer_of(kind, fields, rows):
    layer = QgsVectorLayer(f"{kind}?crs=EPSG:4326&" + "&".join(f"field={f}" for f in fields), "対象", "memory")
    features = []
    for wkt, attrs in rows:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(attrs)
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_cover_written_from_a_chosen_file(plugin):
    _, _, tmp_path = plugin
    path = str(tmp_path / "gfs_tcdc_2026110700.tif")
    _write(path, [T0, T0 + 2 * HOUR], [20, 60], region=REGION)
    points = layer_of("Point", ["when:datetime", "note:string"], [
        ("POINT(105 35)", [qtime(T0 + HOUR), "a"]),
        ("POINT(105 35)", [qtime(T0 + 30 * HOUR), "outside"]),
        ("POINT(105 35)", [None, "no time"]),
        ("POINT(10 0)", [qtime(T0), "outside the grid"]),
    ])
    from skywatch.processing_provider.cloud_algorithm import default_inputs

    assert default_inputs([points], points) == {"INPUT": points.id(), "TIME_FIELD": "when"}
    results, ok, _ = run({"INPUT": points.id(), "TIME_FIELD": "when", "SOURCE": 1, "FILE": path})
    assert ok and results["FILLED"] == 1 and results["MISSING"] == 3
    assert [f["cloud_pct"] for f in points.getFeatures()] == [40, None, None, None]
    assert not points.isEditable()
    # The nearest time instead; into a layer being edited (left for the person to save).
    points.startEditing()
    _, ok, _ = run({"INPUT": points.id(), "TIME_FIELD": "when", "SOURCE": 1, "FILE": path, "METHOD": 1, "FIELD": "cloud_pct"})
    assert ok and points.isEditable()
    assert next(points.getFeatures())["cloud_pct"] == 20
    points.rollBack()


def test_text_times_polygons_and_the_settings_folder(plugin):
    from skywatch.processing_provider import cloud_algorithm

    _, _, tmp_path = plugin
    day = 24 * HOUR
    _write(str(tmp_path / "gfs_tcdc_2026110600.tif"), [T0 - day, T0], [10, 30], region=REGION, meta=False)
    _write(str(tmp_path / "gfs_tcdc_2026110700.tif"), [T0, T0 + 6 * HOUR], [70, 80], region=REGION, meta=False)
    cloud_algorithm.set_forecast(lambda: CloudForecastConfig("", "GFS", folder=str(tmp_path)))
    shapes = layer_of("Polygon", ["at:string"], [
        ("POLYGON((104.6 34.6,105.6 34.6,105.6 35.6,104.6 35.6,104.6 34.6))", ["2026-11-06 12:00:00"]),
        ("POLYGON((105.01 35.01,105.02 35.01,105.02 35.02,105.01 35.01))", ["2026-11-07T03:00:00Z"]),
        ("POLYGON((105.01 35.01,105.02 35.01,105.02 35.02,105.01 35.01))", ["いつか"]),
    ])
    results, ok, _ = run({"INPUT": shapes.id(), "TIME_FIELD": "at", "ZONE": 1})
    assert ok and results["FILLED"] == 2
    # The older file up to the newer's initial time (10 % on the 6th 00:00), then the newer (70 % on the 7th 00:00).
    assert [f["cloud_pct"] for f in shapes.getFeatures()] == [40, 75, None]
    # Only the selection.
    shapes.selectByIds([next(shapes.getFeatures()).id()])
    results, ok, _ = run({"INPUT": shapes.id(), "TIME_FIELD": "at", "ZONE": 1, "SELECTED": True, "FIELD": "c2"})
    assert ok and [f["c2"] for f in shapes.getFeatures()] == [40, None, None]
    # Without the settings file's forecast, a file must be chosen.
    cloud_algorithm.set_forecast(lambda: None)
    _, ok, _ = run({"INPUT": shapes.id(), "TIME_FIELD": "at"})
    assert not ok
    _, ok, _ = run({"INPUT": shapes.id(), "TIME_FIELD": "at", "FIELD": "at", "SOURCE": 1, "FILE": str(tmp_path / "gfs_tcdc_2026110700.tif")})
    assert not ok


def test_the_service_asked_off_the_main_thread_only(plugin, monkeypatch):
    from skywatch.processing_provider import cloud_algorithm

    cloud_algorithm.set_forecast(lambda: CloudForecastConfig("https://wx.example/v1/forecast", "Open-Meteo"))
    points = layer_of("Point", ["when:datetime"], [("POINT(105 35)", [qtime(T0)])])
    _, ok, feedback = run({"INPUT": points.id(), "TIME_FIELD": "when"})
    assert not ok and "バックグラウンド" in feedback.textLog()


def test_the_cloud_map_moves_through_the_times(plugin):
    p, iface, tmp_path = plugin
    path = str(tmp_path / "gfs_tcdc_2026110700.tif")
    _write(path, [T0 + 2 * HOUR, T0, T0 + HOUR], [60, 20, 40], region=REGION)
    dock = p.docks["SatObsCloud"]
    dock.open_file(path, "GFS")
    layer = dock.layer()
    assert isinstance(layer, QgsRasterLayer) and layer.customProperty(PROPERTY) == path
    assert cloud_file(path).bands == [(T0, 2), (T0 + HOUR, 3), (T0 + 2 * HOUR, 1)]
    # Now is long after the forecast: its last time.
    assert dock.slider.value() == 2 and layer.renderer().band() == 1
    dock.slider.setValue(0)
    assert layer.renderer().band() == 2 and dock.when.text().startswith("2026-11-07 00:00 UTC") and "予報 +0 h" in dock.when.text()
    assert layer.name().endswith("（11-07 00:00 UTC）")
    dock.style.setCurrentIndex(1)
    assert layer.renderer().band() == 2
    # QGIS's temporal controller.
    canvas = iface.mapCanvas()
    canvas.setTemporalRange(QgsDateTimeRange(qtime(T0 + HOUR + 60_000), qtime(T0 + 2 * HOUR)))
    if canvas.mapSettings().isTemporal():
        assert dock.slider.value() == 1 and layer.renderer().band() == 3
    dock.follow_controller()
    dock.next_button.click()
    assert dock.slider.value() == 2
    # Replaced by another file; then removed by the person.
    dock.open_file(path, "GFS")
    assert len([l for l in QgsProject.instance().mapLayers().values() if l.customProperty(PROPERTY)]) == 1
    QgsProject.instance().removeMapLayer(dock.layer().id())
    assert dock.file is None and not dock.slider.isEnabled()


def test_the_cloud_map_of_the_service(plugin, monkeypatch):
    from skywatch.ui import cloud_dock

    p, iface, tmp_path = plugin
    asked = []

    def fake(config, places, fetch, cancelled=None, past_days=0, every_file=False):
        asked.append((len(places), past_days))
        out = CloudForecasts({key_of(p): CloudSeries([T0, T0 + HOUR], [p[0] % 100, 50.0]) for p in places})
        out.issued = T0
        return out

    monkeypatch.setattr(cloud_dock, "fetch_clouds", fake)
    monkeypatch.setattr(cloud_dock, "service_file_path", lambda: str(tmp_path / "service.tif"))
    p.context.config.cloud_forecast = CloudForecastConfig("https://wx.example/v1/forecast", "Open-Meteo")
    iface.mapCanvas().setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    iface.mapCanvas().setExtent(QgsRectangle(139, 35, 140, 36))
    dock = p.docks["SatObsCloud"]
    dock.describe()
    dock.past.setValue(3)
    dock.load()
    assert wait_for(lambda: dock.task is None)
    assert asked and asked[0][1] == 3
    assert dock.layer() is not None and len(dock.file.bands) == 2
    assert "Open-Meteo" in dock.status.text()
