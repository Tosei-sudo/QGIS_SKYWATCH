"""Cloud cover at features' times and places, and the grid of the cloud map (core.cloud_cover)."""
import math
from datetime import datetime, timezone

from mikage.core.cloud_cover import (
    EDGE, INTERPOLATE, NEAREST, cells_in, cover_at, has_zone, map_grid, mean_cover,
)
from mikage.core.plan.weather import CloudForecastConfig, CloudForecasts, CloudSeries, fetch_clouds, forecast_urls
from mikage.core.plan.weather_file import join_forecasts
from mikage.tests.test_weather_file import _write

HOUR = 3600000
#: A small region (100–110° E, 30–40° N) so the files are quick to write.
REGION = ((99.875, 0.25, 0, 40.125, 0, -0.25), 41, 41)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp() * 1000


def test_cover_between_or_at_the_nearest_time():
    t0 = utc(2026, 11, 8)
    s = CloudSeries([t0, t0 + 3 * HOUR, t0 + 6 * HOUR], [0.0, 60.0, math.nan])
    assert cover_at(s, t0 + HOUR) == 20
    assert cover_at(s, t0 + HOUR, NEAREST) == 0
    assert cover_at(s, t0 + 2 * HOUR, NEAREST) == 60
    # Next to a missing value: the one there is.
    assert cover_at(s, t0 + 5 * HOUR, NEAREST) == 60
    # Just outside the forecast: its edge; further out, none.
    assert cover_at(s, t0 - EDGE) == 0
    assert cover_at(s, t0 - EDGE - 1) is None
    assert cover_at(None, t0) is None and cover_at(s, None) is None


def test_mean_over_places_the_forecast_tells():
    t0 = utc(2026, 11, 8)
    f = {"35.75,139.75": CloudSeries([t0], [20.0]), "35.75,140": CloudSeries([t0], [60.0])}
    assert mean_cover(f, [(139.75, 35.75), (139.76, 35.74), (140, 35.75), (150, 0)], t0) == 40
    assert mean_cover(f, [(150, 0)], t0) is None


def test_grid_cells_inside_a_shape():
    inside = cells_in((139.6, 35.5, 140.1, 35.8), lambda lon, lat: True)
    assert inside == [(139.75, 35.5), (140.0, 35.5), (139.75, 35.75), (140.0, 35.75)]
    assert cells_in((139.6, 35.6, 139.7, 35.7), lambda lon, lat: True) == []
    # A huge shape is thinned out to the limit.
    big = cells_in((100, 20, 150, 50), lambda lon, lat: True, limit=50)
    assert len(big) == 50 and big[0] == (100.0, 20.0)


def test_map_grid_over_an_extent():
    step, lons, lats = map_grid(139.0, 35.0, 140.0, 36.0)
    assert step == 0.25 and lons[0] == 139.0 and lons[-1] == 140.0 and lats[0] == 36.0 and lats[-1] == 35.0
    step, lons, lats = map_grid(100, 0, 160, 50)
    assert step == 3.0 and len(lons) <= 22 and len(lats) <= 22
    _, _, lats = map_grid(0, 80, 10, 95)
    assert max(lats) <= 90


def test_zones_in_text():
    assert has_zone("2026-11-08T06:00:00Z") and has_zone("2026-11-08 15:00+09:00") and has_zone("2026-11-08 06:00 UTC")
    assert not has_zone("2026-11-08 15:00") and not has_zone("2026/11/08")


def test_service_asked_for_past_days():
    config = CloudForecastConfig("https://wx.example/v1/forecast", "x")
    (_, url), = forecast_urls(config, [(139.7, 35.7)], past_days=200)
    assert "past_days=92" in url
    (_, url), = forecast_urls(config, [(139.7, 35.7)])
    assert "past_days" not in url


def test_forecasts_joined_the_newer_winning():
    def forecast(issued, times, cover):
        f = CloudForecasts({"a": CloudSeries(times, cover)})
        f.issued = issued
        return f

    t0 = utc(2026, 11, 6)
    day = 24 * HOUR
    older = forecast(t0, [t0, t0 + 12 * HOUR, t0 + day, t0 + 2 * day], [10, 20, 30, 40])
    newer = forecast(t0 + day, [t0 + day, t0 + 2 * day], [70, 80])
    out = join_forecasts([newer, older])
    assert out.issued == t0 + day
    assert out["a"].times == [t0, t0 + 12 * HOUR, t0 + day, t0 + 2 * day]
    assert out["a"].cover == [10, 20, 70, 80]


def test_every_file_of_a_folder(tmp_path):
    t6, t7 = utc(2026, 11, 6), utc(2026, 11, 7)
    _write(str(tmp_path / "gfs_tcdc_2026110600.tif"), [t6, t6 + 12 * HOUR, t7], [10, 20, 30], region=REGION)
    _write(str(tmp_path / "gfs_tcdc_2026110700.tif"), [t7, t7 + 6 * HOUR], [70, 80], region=REGION)
    config = CloudForecastConfig("", "GFS", folder=str(tmp_path))
    place = [(105, 35)]
    newest = fetch_clouds(config, place, None)
    assert newest["35,105"].times == [t7, t7 + 6 * HOUR]
    joined = fetch_clouds(config, place, None, every_file=True)
    assert joined["35,105"].cover == [10, 20, 70, 80]
    assert cover_at(joined["35,105"], t6 + 6 * HOUR, INTERPOLATE) == 15
