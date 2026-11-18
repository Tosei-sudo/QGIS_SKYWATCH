"""地図の描画を速くする設定 (core/raster_speed.py): the settings file's `rasterSpeed`, what is set, QGIS's options to change."""
from mikage.core import raster_speed as rs
from mikage.core.config import parse_config
from mikage.core.raster_speed import (
    DEFAULT_RASTER_SPEED, EARLY_RESAMPLING, MAX_THREADS, PARALLEL, RENDER_CACHE, ZOOMED_IN, ZOOMED_OUT, RasterSpeedOptions,
    cache_for, qgis_changes, raster_speed_of, threads_for,
)

GDAL_38 = 3080400


def test_the_settings_file():
    problems = []
    assert raster_speed_of(True, problems) == DEFAULT_RASTER_SPEED
    assert raster_speed_of(False, problems) == RasterSpeedOptions(enabled=False)
    assert raster_speed_of({"gdalThreads": 3, "gdalCacheMB": "1024", "qgisSettings": False}, problems) == \
        RasterSpeedOptions(True, 3, 1024, False)
    # "auto" and false as the browser config maker's text field writes them too.
    assert raster_speed_of({"gdalThreads": "auto", "gdalCacheMB": "false"}, problems) == RasterSpeedOptions(True, 0, None, True)
    assert raster_speed_of({"gdalThreads": False, "gdalCacheMB": None}, problems) == RasterSpeedOptions(True, None, None, True)
    assert problems == []
    raster_speed_of({"gdalThreads": 0.5, "gdalCacheMB": "lots", "qgisSettings": "yes", "enabled": 1}, problems)
    assert len(problems) == 4 and all(p.startswith("rasterSpeed.") for p in problems)
    assert raster_speed_of("fast", problems) == DEFAULT_RASTER_SPEED
    # In the whole settings file, and back from the plugin's own copy for the next start.
    config, found = parse_config({"rasterSpeed": {"gdalCacheMB": 512}})
    assert config.raster_speed.gdal_cache_mb == 512 and found == []
    assert parse_config({})[0].raster_speed == DEFAULT_RASTER_SPEED
    for options in (DEFAULT_RASTER_SPEED, RasterSpeedOptions(False), RasterSpeedOptions(True, 3, None, False)):
        assert RasterSpeedOptions.from_json(options.to_json()) == options
    assert RasterSpeedOptions.from_json("garbage") == DEFAULT_RASTER_SPEED


def test_gdal_threads_are_the_cores_less_one():
    auto = DEFAULT_RASTER_SPEED
    assert threads_for(auto, 4, GDAL_38) == 3 and threads_for(auto, 16, GDAL_38) == 15
    assert threads_for(auto, 2, GDAL_38) is None and threads_for(auto, 1, GDAL_38) is None  # one decoding core: nothing to share
    assert threads_for(RasterSpeedOptions(gdal_threads=6), 4, GDAL_38) == 4  # never more than the PC has
    assert threads_for(RasterSpeedOptions(gdal_threads=2), 8, GDAL_38) == 2
    assert threads_for(RasterSpeedOptions(gdal_threads=None), 8, GDAL_38) is None
    assert threads_for(RasterSpeedOptions(enabled=False), 8, GDAL_38) is None
    assert threads_for(auto, 8, 3050000) is None  # GDAL before 3.6 reads on one core whatever is set


def test_the_block_cache_grows_but_never_shrinks_by_itself():
    auto = DEFAULT_RASTER_SPEED
    # 16 GB: GDAL's own 5 % → 10 %; 64 GB: 2 GB is less than GDAL's own 3.2 GB, so as it is.
    assert cache_for(auto, 16000, 800) == 1600
    assert cache_for(auto, 32000, 1600) == 2048
    assert cache_for(auto, 64000, 3200) is None
    assert cache_for(auto, None, 800) is None  # the RAM unknown
    # The settings file's size as written, but never over half the RAM.
    assert cache_for(RasterSpeedOptions(gdal_cache_mb=512), 16000, 800) == 512
    assert cache_for(RasterSpeedOptions(gdal_cache_mb=12000), 16000, 800) == 8000
    assert cache_for(RasterSpeedOptions(gdal_cache_mb=800), 16000, 800) is None
    assert cache_for(RasterSpeedOptions(gdal_cache_mb=None), 16000, 800) is None
    assert cache_for(RasterSpeedOptions(enabled=False), 16000, 800) is None


def test_only_qgis_options_slower_than_its_defaults_are_changed():
    # A profile never changed (QGIS's defaults): nothing to do.
    assert qgis_changes({}, 8) == []
    assert qgis_changes({PARALLEL: "true", MAX_THREADS: "-1", RENDER_CACHE: True, ZOOMED_IN: "nearest neighbour"}, 8) == []
    # Turned off or limited (an ini file holds "false" and "2" as text).
    changes = qgis_changes({PARALLEL: "false", MAX_THREADS: "2", RENDER_CACHE: "false"}, 8)
    assert [(c.key, c.before, c.after) for c in changes] == [
        (PARALLEL, "false", True), (MAX_THREADS, "2", -1), (RENDER_CACHE, "false", True)]
    assert all(c.why for c in changes) and changes[0].label == "複数の CPU コアで並列にレイヤーを描画"
    # Limited to as many cores as the PC has: not slower.
    assert qgis_changes({MAX_THREADS: 4}, 4) == []
    # Early resampling only where the default resampling is not nearest neighbour.
    assert [c.key for c in qgis_changes({ZOOMED_OUT: "bilinear"}, 4)] == [EARLY_RESAMPLING]
    assert [c.key for c in qgis_changes({ZOOMED_IN: "cubic", EARLY_RESAMPLING: "false"}, 4)] == [EARLY_RESAMPLING]
    assert qgis_changes({ZOOMED_IN: "cubic", EARLY_RESAMPLING: "true"}, 4) == []
    # What was changed and back (kept as JSON in the profile for 「元に戻す」).
    for change in changes:
        assert rs.Change.from_json(change.to_json()) == rs.Change(change.key, change.before, change.after)
    assert rs.Change.from_json({"before": 1}) is None


def test_values_as_qgis_shows_them():
    assert rs.shown(MAX_THREADS, None) == "すべて" and rs.shown(MAX_THREADS, "2") == "2"
    assert rs.shown(PARALLEL, None) == "オン" and rs.shown(PARALLEL, "false") == "オフ"
    assert rs.shown(EARLY_RESAMPLING, None) == "オフ" and rs.shown(EARLY_RESAMPLING, True) == "オン"
    assert rs.as_bool("TRUE", False) and not rs.as_bool("0", True) and rs.as_bool("maybe", True)
    assert rs.as_int("x", -1) == -1 and rs.as_int(" 3 ", -1) == 3


def test_what_only_a_person_can_do_is_listed():
    titles = [title for title, _text in rs.MANUAL]
    assert len(titles) == len(set(titles)) >= 5
    assert any("ウイルス" in t for t in titles) and any("形式" in t for t in titles)
    assert all(len(text) > 30 for _title, text in rs.MANUAL)
