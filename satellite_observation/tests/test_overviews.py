"""The settings file's `overviews` and the levels built."""
from satellite_observation.core.config import parse_config
from satellite_observation.core.overviews import DEFAULT_OVERVIEWS, OverviewOptions, overview_factors, overviews_of


def test_default_is_on():
    config, problems = parse_config({})
    assert config.overviews == DEFAULT_OVERVIEWS and DEFAULT_OVERVIEWS.enabled and not problems


def test_bool_and_object():
    problems = []
    assert overviews_of(False, problems) == OverviewOptions(enabled=False)
    o = overviews_of({"resampling": "cubic", "minSize": 512, "compress": "lzw", "cacheFolder": " D:/cache ",
                      "location": "Cache"}, problems)
    assert o == OverviewOptions(True, "CUBIC", 512, "LZW", "D:/cache", "cache") and not problems
    config, problems = parse_config({"overviews": {"enabled": False}})
    assert not config.overviews.enabled and not problems


def test_bad_values_fall_back():
    problems = []
    o = overviews_of({"resampling": "fancy", "minSize": -1, "compress": 3, "cacheFolder": 1, "enabled": "yes",
                      "location": "elsewhere"}, problems)
    assert o == DEFAULT_OVERVIEWS and len(problems) == 6
    problems = []
    assert overviews_of("on", problems) == DEFAULT_OVERVIEWS and problems


def test_factors():
    assert overview_factors(200, 100) == []
    assert overview_factors(1000, 300) == [2, 4]
    assert overview_factors(4000, 3000) == [2, 4, 8, 16]
