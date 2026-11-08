"""Cloud forecasts read from a file brought into a closed network (docs/cloud-forecast-file.md)."""
import math
import os
from datetime import datetime, timezone

import pytest

from skywatch.core.config import parse_config
from skywatch.core.plan.weather import (
    DEFAULT_CLOUD_FORECAST, CloudForecastConfig, CloudForecasts, apply_clouds, cloud_at, cloud_forecast_of, fetch_clouds,
)
from skywatch.core.plan.weather_file import (
    CloudGrid, band_time, clouds_from_grid, forecast_file, named_time, newest_file, pixel_of, series_of,
)

HOUR = 3600000


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp() * 1000


#: GFS's 0.25° grid, 0–359.75° east, pixel centres on the grid points.
GFS = (-0.125, 0.25, 0, 90.125, 0, -0.25)


def test_settings_of_a_file_or_folder():
    problems = []
    assert cloud_forecast_of({"folder": " \\\\fs\\wx\\gfs ", "label": "GFS 雲量"}, problems) == CloudForecastConfig("", "GFS 雲量", folder="\\\\fs\\wx\\gfs")
    one = cloud_forecast_of({"file": "D:/wx/a.tif"}, problems)
    assert one == CloudForecastConfig("", "雲量予報（取込）", file="D:/wx/a.tif") and one.local
    assert not DEFAULT_CLOUD_FORECAST.local
    assert problems == []
    # Only one of url, file and folder.
    for bad in ({"file": "a.tif", "folder": "b"}, {"url": "https://wx.example/v1/forecast", "file": "a.tif"}, {"folder": ""}, {"file": 3}):
        assert cloud_forecast_of(bad, problems) == DEFAULT_CLOUD_FORECAST
    assert len(problems) == 4
    assert parse_config({"cloudForecast": {"folder": "/wx"}})[0].cloud_forecast.folder == "/wx"


def test_the_newest_file_of_a_folder():
    names = ["gfs_tcdc_2026110600.tif", "gfs_tcdc_2026110700.tif", "gfs_tcdc_2026110800.tif.part", "readme.txt", "GFS_TCDC_2026110512.TIF"]
    assert newest_file("/wx", lambda _: names) == os.path.join("/wx", "gfs_tcdc_2026110700.tif")
    assert named_time("gfs_tcdc_2026110706.tif") == utc(2026, 11, 7, 6)
    assert math.isnan(named_time("latest.tif"))
    # Without the names, the .tif changed last.
    times = {"a.tif": 2, "b.tiff": 3, "c.png": 9}
    assert newest_file("/wx", lambda _: list(times), lambda p: times[os.path.basename(p)]) == os.path.join("/wx", "b.tiff")
    assert newest_file("/wx", lambda _: ["x.txt"]) is None


def test_band_times():
    assert band_time({"VALID_TIME": "2026-11-07T03:00:00Z"}) == utc(2026, 11, 7, 3)
    # As GDAL tells GRIB bands (older versions add "sec UTC").
    assert band_time({"GRIB_VALID_TIME": "1762484400"}) == 1762484400000
    assert band_time({"GRIB_VALID_TIME": "  1762484400 sec UTC"}) == 1762484400000
    assert band_time({}, "2026-11-07T06:00:00Z") == utc(2026, 11, 7, 6)
    assert math.isnan(band_time({}, "Band 1"))


def test_pixels_round_the_globe():
    grid = CloudGrid(GFS, 1440, 721, [])
    assert pixel_of(grid, 0, 90) == (0, 0)
    assert pixel_of(grid, 139.75, 35.75) == (559, 217)
    # West longitudes on a 0–360° grid.
    assert pixel_of(grid, -0.25, -90) == (1439, 720)
    assert pixel_of(grid, -180, 0) == (720, 360)
    # A grid of -180–180°, cut to a region.
    asia = CloudGrid((99.875, 0.25, 0, 50.125, 0, -0.25), 200, 200, [])
    assert pixel_of(asia, 139.75, 35.75) == (159, 57)
    assert pixel_of(asia, 0, 35.75) is None
    assert pixel_of(asia, 139.75, -30) is None


def test_series_leave_out_no_data():
    t0 = utc(2026, 11, 7)
    grid = CloudGrid(GFS, 1440, 721, [t0 + 3 * HOUR, t0, t0 + HOUR, math.nan, t0 + 2 * HOUR], nodata=255)
    s = series_of(grid, [30, 10, 255, 50, 120])
    assert (s.times, s.cover) == ([t0, t0 + 3 * HOUR], [10.0, 30.0])
    assert series_of(grid, [255] * 5) is None
    scaled = CloudGrid(GFS, 1440, 721, [t0], scale=0.5, offset=1)
    assert series_of(scaled, [40]).cover == [21.0]


def test_places_share_their_grid_cells():
    t0 = utc(2026, 11, 7)
    grid = CloudGrid(GFS, 1440, 721, [t0, t0 + 3 * HOUR], issued=t0)
    asked = []

    def sample(col, row):
        asked.append((col, row))
        return [col % 101, 100]

    out = clouds_from_grid(grid, [(139.7, 35.7), (139.71, 35.71), (-74.0, 40.7)], sample)
    assert isinstance(out, CloudForecasts) and out.issued == t0
    assert asked == [(559, 217), (1144, 197)]
    assert list(out) == ["35.75,139.75", "40.75,-74"]
    assert cloud_at(out["35.75,139.75"], t0 + 1.5 * HOUR) == (559 % 101 + 100) / 2


def test_missing_files_are_told():
    with pytest.raises(ValueError, match="ファイルがない"):
        forecast_file(CloudForecastConfig("", "x", file="/no/such/file.tif"))
    with pytest.raises(ValueError, match="フォルダがない"):
        forecast_file(CloudForecastConfig("", "x", folder="/no/such/folder"))


def _write(path, times, values, region=None, meta=True, grib_times=False):
    gdal = pytest.importorskip("osgeo.gdal")
    transform, w, h = region or (GFS, 1440, 721)
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, len(times), gdal.GDT_Byte,
                                                 ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"])
    ds.SetGeoTransform(transform)
    if meta:
        ds.SetMetadata({"INIT_TIME": "2026-11-07T00:00:00Z", "MODEL": "GFS", "VARIABLE": "TCDC"})
    import numpy as np
    for i, t in enumerate(times):
        band = ds.GetRasterBand(i + 1)
        band.SetNoDataValue(255)
        band.WriteArray(np.full((h, w), values[i], dtype=np.uint8))
        if grib_times:
            band.SetMetadata({"GRIB_VALID_TIME": str(int(t / 1000))})
        else:
            band.SetMetadata({"VALID_TIME": datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    ds = None


def test_reads_the_newest_geotiff_of_a_folder(tmp_path):
    t0 = utc(2026, 11, 7)
    _write(str(tmp_path / "gfs_tcdc_2026110600.tif"), [t0, t0 + 3 * HOUR], [90, 90])
    _write(str(tmp_path / "gfs_tcdc_2026110700.tif"), [t0 + 3 * HOUR, t0], [60, 20])
    config = CloudForecastConfig("", "GFS", folder=str(tmp_path))
    out = fetch_clouds(config, [(139.7, 35.7), (-74.0, 40.7)], lambda url: pytest.fail("no service asked"))
    assert out.issued == t0
    assert sorted(out) == ["35.75,139.75", "40.75,-74"]
    assert cloud_at(out["35.75,139.75"], t0 + 1.5 * HOUR) == 40


def test_reads_grib_times_and_regions(tmp_path):
    t0 = utc(2026, 11, 7)
    path = str(tmp_path / "asia.tif")
    _write(path, [t0, t0 + HOUR], [0, 255], region=((99.875, 0.25, 0, 50.125, 0, -0.25), 200, 200), meta=False, grib_times=True)
    out = fetch_clouds(CloudForecastConfig("", "GFS", file=path), [(139.7, 35.7), (0, 0)], None)
    assert math.isnan(out.issued)
    assert list(out) == ["35.75,139.75"]
    assert out["35.75,139.75"].times == [t0]


def test_apply_from_a_file(tmp_path):
    from skywatch.tests.test_plan_extras import op, sat
    from skywatch.core.plan.access import PlanTarget
    t0 = utc(2026, 11, 7)
    _write(str(tmp_path / "gfs_tcdc_2026110700.tif"), [t0, t0 + 2 * HOUR], [20, 60])
    targets = [PlanTarget("a", (139.7, 35.7), [])]
    ops = [op(t0 + HOUR, satellite=0, target=0)]
    forecasts = fetch_clouds(CloudForecastConfig("", "GFS", folder=str(tmp_path)), [targets[0].center], None)
    assert apply_clouds(ops, [sat()], targets, forecasts) == 1
    assert ops[0].cloud == 40
