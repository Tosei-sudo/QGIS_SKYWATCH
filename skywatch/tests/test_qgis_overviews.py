"""
Overviews for catalog images (qgis_io/overviews_io): an external .ovr beside
the image, or in the cache folder (GDAL_PAM_PROXY_DIR) when the image's
folder cannot be written; the image itself is never changed.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer  # noqa: E402

from skywatch.core.overviews import OverviewOptions  # noqa: E402
from skywatch.qgis_io import overviews_io, settings  # noqa: E402
from skywatch.qgis_io.overviews_io import (  # noqa: E402
    BUILT, CACHED, HAS_OVERVIEWS, READ_ONLY, SMALL, OverviewQueue, build_external,
)
from skywatch.tests.qgis_fakes import wait_for  # noqa: E402


def image(path, size=(3000, 2000), bands=3):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), size[0], size[1], bands, gdal.GDT_UInt16, ["TILED=YES"])
    ds.SetGeoTransform([139.6, 0.0001, 0, 35.7, 0, -0.0001])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(bands):
        ds.GetRasterBand(b + 1).WriteArray(np.full((size[1], size[0]), 100 + b, dtype=np.uint16))
    ds = None
    return str(path)


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def overview_count(path):
    ds = gdal.Open(path)
    return ds.GetRasterBand(1).GetOverviewCount()


@pytest.fixture
def no_cache(monkeypatch):
    monkeypatch.setattr(overviews_io, "cache_folder", lambda: None)


@pytest.fixture
def read_only(tmp_path):
    """A folder with an image that cannot be written (chattr +i as root, chmod otherwise)."""
    folder = tmp_path / "ro"
    folder.mkdir()
    path = image(folder / "img.tif")
    if os.geteuid() == 0:
        if subprocess.run(["chattr", "+i", str(folder)], capture_output=True).returncode != 0:
            pytest.skip("読み取り専用フォルダを作れず")
        undo = ["chattr", "-i", str(folder)]
    else:
        os.chmod(folder, 0o555)
        undo = ["chmod", "755", str(folder)]
    yield path
    subprocess.run(undo)


def test_beside_the_image_untouched(tmp_path, no_cache):
    path = image(tmp_path / "a.tif")
    before = digest(path)
    seen = []
    outcome = build_external(path, OverviewOptions(), seen.append)
    assert outcome.kind == BUILT and outcome.factors == (2, 4, 8, 16)
    assert os.path.exists(path + ".ovr") and digest(path) == before
    assert overview_count(path) == 4 and seen and seen[-1] >= 99
    # Nothing left beside it but the .ovr.
    assert sorted(os.listdir(tmp_path)) == ["a.tif", "a.tif.ovr"]
    ds = gdal.Open(path)
    ov = ds.GetRasterBand(2).GetOverview(0)
    assert ov.XSize == 1500 and int(ov.ReadAsArray().mean()) == 101
    assert build_external(path, OverviewOptions()).kind == HAS_OVERVIEWS


def test_small_and_internal_are_left(tmp_path, no_cache):
    small = image(tmp_path / "s.tif", (1000, 800), 1)
    assert build_external(small, OverviewOptions()).kind == SMALL
    assert build_external(small, OverviewOptions(min_size=0)).kind == BUILT
    inner = image(tmp_path / "i.tif")
    ds = gdal.Open(inner, gdal.GA_Update)
    ds.BuildOverviews("NEAREST", [2])
    ds = None
    assert build_external(inner, OverviewOptions()).kind == HAS_OVERVIEWS
    assert not os.path.exists(inner + ".ovr")


def test_cancel_leaves_nothing(tmp_path, no_cache):
    path = image(tmp_path / "c.tif")
    with pytest.raises(overviews_io.Cancelled):
        build_external(path, OverviewOptions(), cancelled=lambda: True)
    assert sorted(os.listdir(tmp_path)) == ["c.tif"]


def test_read_only_without_cache(read_only, no_cache):
    assert build_external(read_only, OverviewOptions()).kind == READ_ONLY


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def in_fresh_gdal(cache, code):
    """
    Runs `code` in a new Python with GDAL_PAM_PROXY_DIR set from the start
    (GDAL reads it only once per process, so not in this one); its JSON.
    """
    script = (
        "import json, os\n"
        "from osgeo import gdal\n"
        "from skywatch.core.overviews import OverviewOptions\n"
        "from skywatch.qgis_io import overviews_io as o\n"
        "def count(p):\n"
        "    ds = gdal.Open(p)\n"
        "    return ds.GetRasterBand(1).GetOverviewCount()\n"
        + code
    )
    env = dict(os.environ, GDAL_PAM_PROXY_DIR=str(cache), PYTHONPATH=ROOT, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_read_only_goes_to_cache(read_only, tmp_path):
    before = digest(read_only)
    cache = tmp_path / "cache"
    cache.mkdir()
    out = in_fresh_gdal(cache, f"r = o.build_external({read_only!r}, OverviewOptions())\n"
                               f"print(json.dumps([r.kind, count({read_only!r}), o.build_external({read_only!r}, OverviewOptions()).kind]))")
    assert out == [CACHED, 4, HAS_OVERVIEWS]
    assert sorted(os.listdir(os.path.dirname(read_only))) == ["img.tif"] and digest(read_only) == before
    assert any(n.endswith(".ovr") for n in os.listdir(cache))
    # Found again by a new QGIS (process); not without the cache folder.
    assert in_fresh_gdal(cache, f"print(json.dumps(count({read_only!r})))") == 4
    assert overview_count(read_only) == 0


def test_cache_location_even_when_writable(tmp_path):
    folder = tmp_path / "w"
    folder.mkdir()
    path = image(folder / "img.tif")
    # What the image's own .aux.xml says is kept.
    (folder / "img.tif.aux.xml").write_text('<PAMDataset><Metadata><MDI key="NOTE">beside</MDI></Metadata></PAMDataset>')
    before = digest(path), (folder / "img.tif.aux.xml").read_text()
    cache = tmp_path / "cache"
    cache.mkdir()
    out = in_fresh_gdal(cache, f"r = o.build_external({path!r}, OverviewOptions(location='cache'))\n"
                               f"ds = gdal.Open({path!r})\n"
                               f"print(json.dumps([r.kind, count({path!r}), ds.GetMetadataItem('NOTE')]))")
    assert out == [CACHED, 4, "beside"]
    assert sorted(os.listdir(folder)) == ["img.tif", "img.tif.aux.xml"]
    assert (digest(path), (folder / "img.tif.aux.xml").read_text()) == before
    names = os.listdir(cache)
    assert any(re.match(r"^[0-9a-f]{12}_img\.tif\.ovr$", n) for n in names) and "gdal_pam_proxy.dat" in names
    assert in_fresh_gdal(cache, f"print(json.dumps(count({path!r})))") == 4
    assert overview_count(path) == 0
    n, size = overviews_io.cache_size(str(cache))
    assert n >= 2 and size > 0


def test_cache_location_without_cache(tmp_path, no_cache):
    path = image(tmp_path / "n.tif")
    assert build_external(path, OverviewOptions(location="cache")).kind == overviews_io.NO_CACHE
    assert sorted(os.listdir(tmp_path)) == ["n.tif"]


def test_start_configure_and_clear_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(overviews_io, "default_cache_folder", lambda: str(tmp_path / "default"))
    settings.set_overview_cache_setting("")
    gdal.SetConfigOption("GDAL_PAM_PROXY_DIR", None)
    try:
        # Nothing set: the plugin's folder in the profile.
        assert overviews_io.start_cache() == str(tmp_path / "default")
        assert gdal.GetConfigOption("GDAL_PAM_PROXY_DIR") == str(tmp_path / "default")
        # Already set (QGIS's gdal_pam, or the person's) and no cacheFolder: kept.
        gdal.SetConfigOption("GDAL_PAM_PROXY_DIR", str(tmp_path / "qgis"))
        assert overviews_io.start_cache() == str(tmp_path / "qgis")
        assert overviews_io.configure_cache(OverviewOptions()) is None
        # A cacheFolder in the settings file: used from the next start.
        mine = tmp_path / "mine"
        assert overviews_io.configure_cache(OverviewOptions(cache_folder=str(mine))) == str(mine)
        assert settings.overview_cache_setting() == str(mine) and overviews_io.current.cache_folder == str(mine)
        assert overviews_io.start_cache() == str(mine) and mine.is_dir()
        assert overviews_io.configure_cache(OverviewOptions(cache_folder=str(mine))) is None
        for name in ("gdal_pam_proxy.dat", "000000__x.tif.ovr", "0123456789ab_y.tif.ovr", "notes.txt"):
            (mine / name).write_text("x")
        assert overviews_io.cache_size(str(mine)) == (2, 2)
        assert overviews_io.clear_cache_next_start() == str(mine)
        assert settings.overview_cache_to_clear() == str(mine)
        overviews_io.start_cache()
        # Only the cache's files go.
        assert sorted(os.listdir(mine)) == ["notes.txt"] and not settings.overview_cache_to_clear()
    finally:
        gdal.SetConfigOption("GDAL_PAM_PROXY_DIR", None)
        settings.set_overview_cache_to_clear("")
        settings.set_overview_cache_setting("")
        overviews_io.current = OverviewOptions()


def test_before_open_keeps_aux_in_cache(tmp_path):
    folder = tmp_path / "w"
    folder.mkdir()
    path = image(folder / "img.tif")
    cache = tmp_path / "cache"
    cache.mkdir()
    # Statistics QGIS computes on a layer opened after before_open go to the cache, not beside the image.
    out = in_fresh_gdal(cache, "from qgis.core import QgsApplication, QgsRasterLayer\n"
                               "app = QgsApplication([], False); app.initQgis()\n"
                               "gdal.SetConfigOption('GDAL_PAM_PROXY_DIR', os.environ['GDAL_PAM_PROXY_DIR'])  # QGIS sets its own\n"
                               "o.current = OverviewOptions(location='cache')\n"
                               f"o.before_open({path!r})\n"
                               f"layer = QgsRasterLayer({path!r}, 'x', 'gdal')\n"
                               "layer.dataProvider().histogram(1)\n"
                               "layer.dataProvider().bandStatistics(1)\n"
                               "del layer\n"
                               f"ds = gdal.Open({path!r}); ds.GetRasterBand(1).ComputeStatistics(False); ds = None\n"
                               f"print(json.dumps(sorted(os.listdir({str(folder)!r}))))")
    assert out == ["img.tif"]
    assert any(n.endswith("_img.tif.aux.xml") for n in os.listdir(cache))


def test_queue_builds_once_and_reloads(tmp_path, no_cache):
    path = image(tmp_path / "q.tif")
    other = image(tmp_path / "r.tif")
    layer = QgsRasterLayer(path, "q", "gdal")
    twin = QgsRasterLayer(path, "q2", "gdal")
    second = QgsRasterLayer(other, "r", "gdal")
    url = QgsRasterLayer("/vsicurl/https://example.com/x.tif", "u", "gdal")
    assert layer.isValid() and not layer.dataProvider().hasPyramids()
    QgsProject.instance().addMapLayers([layer, twin, second])
    q = OverviewQueue()
    ended = []
    q.finished = lambda outcome, error: ended.append((outcome, error))
    try:
        assert q.request([layer, twin, second, url], OverviewOptions()) == [path, other]
        assert q.request([layer], OverviewOptions()) == []
        assert q.request([layer], OverviewOptions(enabled=False)) == []
        assert wait_for(lambda: len(ended) == 2, 60000)
        assert [o.kind for o, _ in ended] == [BUILT, BUILT]
        assert layer.dataProvider().hasPyramids() and twin.dataProvider().hasPyramids()
        assert q.pending() == []
    finally:
        QgsProject.instance().removeAllMapLayers()


def test_layers_added_any_way(tmp_path, no_cache, monkeypatch):
    path = image(tmp_path / "any.tif")
    layer = QgsRasterLayer(path, "any", "gdal")
    url = QgsRasterLayer("/vsicurl/https://example.com/x.tif", "u", "gdal")
    queued = []
    monkeypatch.setattr(overviews_io.queue, "request", lambda layers, options, bar=None: queued.append([local_path_of(l) for l in layers]) or [])
    try:
        overviews_io.current = OverviewOptions(scope="catalog")
        overviews_io.layers_added([layer, url])
        assert queued == []
        overviews_io.current = OverviewOptions(enabled=False)
        overviews_io.layers_added([layer])
        assert queued == []
        overviews_io.current = OverviewOptions()
        overviews_io.layers_added([layer, url])
        assert queued == [[path]]
    finally:
        overviews_io.current = OverviewOptions()


def local_path_of(layer):
    return overviews_io.local_path(layer)


def test_layers_added_cache_from_next_time(tmp_path):
    folder = tmp_path / "w"
    folder.mkdir()
    path = image(folder / "img.tif")
    cache = tmp_path / "cache"
    cache.mkdir()
    head = "from qgis.core import QgsApplication, QgsRasterLayer\n" \
           "app = QgsApplication([], False); app.initQgis()\n" \
           "gdal.SetConfigOption('GDAL_PAM_PROXY_DIR', os.environ['GDAL_PAM_PROXY_DIR'])\n"
    # Opened by QGIS first (not from the catalog): the layer keeps writing beside the image (QGIS's statistics, on close)...
    out = in_fresh_gdal(cache, head + "o.current = OverviewOptions(location='cache')\n"
                                      f"layer = QgsRasterLayer({path!r}, 'x', 'gdal')\n"
                                      "o.queue.request = lambda layers, options, bar=None: [o.local_path(l) for l in layers]\n"
                                      "print(json.dumps(o.layers_added([layer])))")
    assert out == [path]
    beside = [n for n in os.listdir(folder) if n != "img.tif"]
    stamp = {n: os.path.getmtime(folder / n) for n in beside}
    # ...but from the next opening on, GDAL reads and writes the one in the cache.
    out = in_fresh_gdal(cache, head + f"layer = QgsRasterLayer({path!r}, 'x', 'gdal')\n"
                                      "layer.dataProvider().bandStatistics(1)\n"
                                      f"ds = gdal.Open({path!r}); ds.GetRasterBand(1).ComputeStatistics(False); ds = None\n"
                                      "del layer\n"
                                      f"print(json.dumps(sorted(os.listdir({str(folder)!r}))))")
    assert out == sorted(["img.tif"] + beside)
    assert {n: os.path.getmtime(folder / n) for n in beside} == stamp
    proxied = [n for n in os.listdir(cache) if n.endswith("_img.tif.aux.xml")]
    assert proxied and "STATISTICS_" in (cache / proxied[0]).read_text()


def test_jpeg_four_bands_builds_without_ycbcr(tmp_path, no_cache):
    """#80: GDAL refuses YCbCr for four bands, so RGBN/RGBA Byte images get plain JPEG overviews."""
    mem = gdal.GetDriverByName("MEM")
    three = mem.Create("", 10, 10, 3, gdal.GDT_Byte)
    four = mem.Create("", 10, 10, 4, gdal.GDT_Byte)
    options = OverviewOptions(compress="JPEG")
    assert overviews_io._config(three, options)["PHOTOMETRIC_OVERVIEW"] == "YCBCR"
    assert "PHOTOMETRIC_OVERVIEW" not in overviews_io._config(four, options)
    assert overviews_io._config(four, options)["COMPRESS_OVERVIEW"] == "JPEG"
    path = str(tmp_path / "rgbn.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 3000, 2000, 4, gdal.GDT_Byte, ["TILED=YES"])
    ds.SetGeoTransform([139.6, 0.0001, 0, 35.7, 0, -0.0001])
    for b in range(4):
        ds.GetRasterBand(b + 1).Fill(50 + b)
    ds = None
    outcome = build_external(path, options)
    assert outcome.kind == BUILT and overview_count(path) == 4
