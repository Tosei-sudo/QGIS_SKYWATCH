"""Shared data that must stay data: CSV cells, web map data sources, LIKE patterns, cached settings, old Pythons."""
import os
import shutil
import subprocess

import pytest

from skywatch.core.geocode import gsi_url, like_text
from skywatch.core.util import formula_safe
from skywatch.core.webmap import plan_web_map

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_a_cell_that_would_be_a_formula_is_text_but_numbers_stay():
    assert formula_safe('=HYPERLINK("http://x")') == '\'=HYPERLINK("http://x")'
    assert formula_safe("@SUM(A1)") == "'@SUM(A1)" and formula_safe("+1+2") == "'+1+2" and formula_safe("-cmd") == "'-cmd"
    assert formula_safe("-12.5") == "-12.5" and formula_safe("+3") == "+3" and formula_safe("1e5") == "1e5"
    assert formula_safe("あおば") == "あおば" and formula_safe("") == ""


def test_the_observation_csv_guards_notes():
    from skywatch.core.obs.csv import _cell

    assert _cell("=1+1") == "'=1+1" and _cell(-12.5) == "-12.5"


def _layer(url: str, kind: str = "ArcGISFeatureLayer", **more) -> dict:
    return {"operationalLayers": [{"id": "a", "title": "a", "layerType": kind, "url": url, **more}]}


def test_a_web_map_cannot_break_out_of_the_data_source():
    good = plan_web_map(_layer("https://portal.example/arcgis/rest/services/x/FeatureServer/0"))
    assert len(good.layers) == 1
    for url in ("https://portal.example/arcgis/rest/services/x' url='https://evil.example/FeatureServer/0",
                "https://portal.example/a b/FeatureServer/0"):
        plan = plan_web_map(_layer(url))
        assert plan.layers == [] and plan.unsupported
    wms = plan_web_map(_layer("https://wms.example/wms?a=1&url=https://evil/", "WMS", visibleLayers=["x"]))
    assert wms.layers == []
    named = plan_web_map(_layer("https://wms.example/wms", "WMS", visibleLayers=["x' url='https://evil/"]))
    assert named.layers == []


def test_like_patterns_escape_the_escape_character():
    assert like_text("a\\") == "a\\\\" and like_text("50%_'") == "50\\%\\_''"
    assert gsi_url("https://x/s", "東京 (港)") == "https://x/s?q=%E6%9D%B1%E4%BA%AC%20(%E5%8F%B7)".replace("%E5%8F%B7", "%E6%B8%AF")


def test_cached_settings_keep_no_secret_in_urls():
    pytest.importorskip("qgis.core")
    from skywatch.qgis_io.settings import _without_tokens

    value = {"movingFeeds": [{"url": "https://ais.example/feed?token=abc&area=1", "token": "t"}],
             "x": "https://a.example/p?key=k", "y": "https://a.example/p?q=1&apikey=z", "note": "token=1 (not a URL)"}
    assert _without_tokens(value) == {"movingFeeds": [{"url": "https://ais.example/feed?area=1"}], "x": "https://a.example/p",
                                      "y": "https://a.example/p?q=1", "note": "token=1 (not a URL)"}


@pytest.mark.skipif(not shutil.which("python3.11"), reason="python3.11 なし")
def test_the_plugin_compiles_on_python_3_11():
    # QGIS 3.34 runs on Python 3.10 / 3.11 on some systems: no 3.12-only syntax (quotes inside f-strings).
    result = subprocess.run(["python3.11", "-m", "compileall", "-q", "-x", "ext", HERE], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-2000:]
