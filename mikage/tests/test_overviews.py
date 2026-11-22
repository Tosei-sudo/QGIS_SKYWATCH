"""The settings file's `overviews` and the levels built."""
from mikage.core.config import parse_config
from mikage.core.overviews import DEFAULT_OVERVIEWS, OverviewOptions, overview_factors, overviews_of


def test_default_is_on():
    config, problems = parse_config({})
    assert config.overviews == DEFAULT_OVERVIEWS and DEFAULT_OVERVIEWS.enabled and not problems


def test_bool_and_object():
    problems = []
    assert overviews_of(False, problems) == OverviewOptions(enabled=False)
    o = overviews_of({"resampling": "cubic", "minSize": 512, "compress": "lzw", "cacheFolder": " D:/cache ",
                      "location": "Cache", "scope": "Catalog"}, problems)
    assert o == OverviewOptions(True, "CUBIC", 512, "LZW", "D:/cache", "cache", "catalog") and not problems
    config, problems = parse_config({"overviews": {"enabled": False}})
    assert not config.overviews.enabled and not problems


def test_bad_values_fall_back():
    problems = []
    o = overviews_of({"resampling": "fancy", "minSize": -1, "compress": 3, "cacheFolder": 1, "enabled": "yes",
                      "location": "elsewhere", "scope": "some"}, problems)
    assert o == DEFAULT_OVERVIEWS and len(problems) == 7
    problems = []
    assert overviews_of("on", problems) == DEFAULT_OVERVIEWS and problems


def test_factors():
    assert overview_factors(200, 100) == []
    assert overview_factors(1000, 300) == [2, 4]
    assert overview_factors(4000, 3000) == [2, 4, 8, 16]


def test_threads():
    from mikage.core.overviews import build_threads

    assert OverviewOptions().threads == 0
    assert overviews_of({"threads": 3}, []).threads == 3
    problems = []
    assert overviews_of({"threads": -1}, problems).threads == 0 and problems
    problems = []
    assert overviews_of({"threads": 1.5}, problems).threads == 0 and problems
    # 0: half the cores, at least one; never more than the PC has.
    assert [build_threads(OverviewOptions(), n) for n in (None, 1, 2, 3, 8, 16)] == [1, 1, 1, 1, 4, 8]
    assert build_threads(OverviewOptions(threads=6), 4) == 4 and build_threads(OverviewOptions(threads=2), 8) == 2


def test_temp_location_and_folder():
    """location "temp" (tsudo, 2026-11-22): the .ovr go to a temporary folder deleted when QGIS is closed."""
    problems = []
    o = overviews_of({"location": "Temp", "tempFolder": " %TEMP%/MIKAGE "}, problems)
    assert o.location == "temp" and o.temp_folder == "%TEMP%/MIKAGE" and not problems
    # Read as before when not given; the earlier keys keep their places.
    assert OverviewOptions(True, "CUBIC", 512, "LZW", "D:/cache", "temp", "catalog", 2).temp_folder == ""
    problems = []
    assert overviews_of({"tempFolder": 3}, problems).temp_folder == "" and len(problems) == 1 and "tempFolder" in problems[0]
    problems = []
    assert overviews_of({"location": "tmp"}, problems).location == "auto" and "temp（一時フォルダ" in problems[0]


def test_expand_folder(tmp_path):
    import os
    import tempfile

    from mikage.core.overviews import TEMP_SUBFOLDER, expand_folder, temp_root

    env = {"LOCALAPPDATA": "C:/Users/a/AppData/Local", "Temp": "C:/Users/a/AppData/Local/Temp"}
    assert expand_folder("%LOCALAPPDATA%/MIKAGE/ovr", env) == "C:/Users/a/AppData/Local/MIKAGE/ovr"
    # Names as Windows reads them (any case); %TEMP% is the system's temporary folder where it is not set.
    assert expand_folder("%TEMP%\\MIKAGE", env) == "C:/Users/a/AppData/Local/Temp\\MIKAGE"
    assert expand_folder("%TMP%/x", {}) == os.path.join(tempfile.gettempdir()) + "/x"
    # Not set: as written; no variable, nothing changed; blank stays blank.
    assert expand_folder("%NOPE%/x", env) == "%NOPE%/x" and expand_folder(" D:/ovr ", env) == "D:/ovr"
    assert expand_folder("  ", env) == "" and expand_folder("\\\\nas\\D$\\ovr", env) == "\\\\nas\\D$\\ovr"
    assert expand_folder("~/ovr", env) == os.path.expanduser("~/ovr")
    assert temp_root(OverviewOptions()) == os.path.join(tempfile.gettempdir(), TEMP_SUBFOLDER)
    assert temp_root(OverviewOptions(temp_folder=str(tmp_path))) == str(tmp_path)
