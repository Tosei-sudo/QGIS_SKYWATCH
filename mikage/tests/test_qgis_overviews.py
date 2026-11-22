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
import time

import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsProject, QgsRasterLayer  # noqa: E402

from mikage.core.overviews import OverviewOptions  # noqa: E402
from mikage.qgis_io import overviews_io, settings  # noqa: E402
from mikage.qgis_io.overviews_io import (  # noqa: E402
    BUILT, CACHED, HAS_OVERVIEWS, READ_ONLY, SMALL, OverviewQueue, build_external,
)
from mikage.tests.qgis_fakes import wait_for  # noqa: E402


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
        "from mikage.core.overviews import OverviewOptions\n"
        "from mikage.qgis_io import overviews_io as o\n"
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
    if overviews_io.gdaladdo():
        # Built by gdaladdo aside, then named in the .aux.xml GDAL itself put in the cache folder.
        assert any(re.match(r"^[0-9a-f]{12}_img\.tif\.ovr$", n) for n in os.listdir(cache))
    # Found again by a new QGIS (process); not without the cache folder.
    assert in_fresh_gdal(cache, f"print(json.dumps(count({read_only!r})))") == 4
    assert overview_count(read_only) == 0


def test_read_only_goes_to_cache_without_gdaladdo(read_only, tmp_path):
    """Without gdaladdo GDAL itself writes the levels to the cache folder, in QGIS (as before)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    out = in_fresh_gdal(cache, "o.gdaladdo = lambda: None\n"
                               f"r = o.build_external({read_only!r}, OverviewOptions())\n"
                               f"print(json.dumps([r.kind, r.native, count({read_only!r})]))")
    assert out == [CACHED, False, 4]
    assert sorted(os.listdir(os.path.dirname(read_only))) == ["img.tif"]
    assert not any(re.match(r"^[0-9a-f]{12}_img\.tif\.ovr$", n) for n in os.listdir(cache))
    assert in_fresh_gdal(cache, f"print(json.dumps(count({read_only!r})))") == 4


def test_read_only_attached_falls_back_when_not_found(read_only, tmp_path):
    """When GDAL does not find the .ovr named in its .aux.xml, it builds the levels itself as before."""
    if not overviews_io.gdaladdo():
        pytest.skip("gdaladdo なし")
    cache = tmp_path / "cache"
    cache.mkdir()
    out = in_fresh_gdal(cache, "o.attach_native = lambda path, ovr: None\n"
                               f"r = o.build_external({read_only!r}, OverviewOptions())\n"
                               f"print(json.dumps([r.kind, r.native, count({read_only!r})]))")
    assert out == [CACHED, False, 4]
    assert not any(re.match(r"^[0-9a-f]{12}_img\.tif\.ovr$", n) for n in os.listdir(cache))


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


# ---- The levels made by gdaladdo in a process of its own (QGIS keeps drawing the map smoothly) ----

needs_gdaladdo = pytest.mark.skipif(overviews_io.gdaladdo() is None, reason="gdaladdo なし")


@needs_gdaladdo
def test_built_by_gdaladdo_in_its_own_process(tmp_path, no_cache, monkeypatch):
    runs = []
    run = overviews_io.run_gdaladdo
    monkeypatch.setattr(overviews_io, "run_gdaladdo", lambda *a, **k: runs.append(a[3]) or run(*a, **k))
    # Not built in QGIS: GDAL's BuildOverviews of this process is never called.
    monkeypatch.setattr(gdal.Dataset, "BuildOverviews", lambda *a, **k: pytest.fail("QGIS の中で作成"))
    folder = tmp_path / "日本語 のフォルダ"
    folder.mkdir()
    path = image(folder / "画像 1.tif")
    seen = []
    outcome = build_external(path, OverviewOptions(), seen.append)
    assert outcome.kind == BUILT and runs == [[2, 4, 8, 16]]
    assert overview_count(path) == 4 and seen[-1] >= 99
    assert sorted(os.listdir(folder)) == ["画像 1.tif", "画像 1.tif.ovr"]
    ds = gdal.Open(path)
    assert int(ds.GetRasterBand(3).GetOverview(1).ReadAsArray().mean()) == 102


@needs_gdaladdo
def test_gdaladdo_gets_the_options(tmp_path, no_cache, monkeypatch):
    asked = []
    popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: asked.append((args, k)) or popen(args, **k))
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    path = image(tmp_path / "o.tif")
    assert build_external(path, OverviewOptions(resampling="CUBIC", compress="LZW")).kind == BUILT
    (args, kwargs), = asked
    text = " ".join(args)
    assert "-r cubic image.vrt 2 4 8 16" in text and "--config COMPRESS_OVERVIEW LZW" in text
    # Half the cores (the others stay for QGIS), no folder on the command line, GDAL's cache folder not passed on.
    assert "--config GDAL_NUM_THREADS 4" in text and os.path.basename(kwargs["cwd"]).startswith("satobs-ovr-")
    assert "GDAL_PAM_PROXY_DIR" not in kwargs["env"]
    asked.clear()
    other = image(tmp_path / "p.tif")
    assert build_external(other, OverviewOptions(threads=1)).kind == BUILT
    assert "--config GDAL_NUM_THREADS 1" in " ".join(asked[0][0])


def test_progress_of_gdaladdo():
    import io

    for text, percent in ((b"", 0), (b"0", 0), (b"0.", 2.5), (b"0...", 7.5), (b"0...10..", 15), (b"0...10...20...30...40...50...60...70...80...90...100 - done.\n", 100)):
        state = {"percent": 0.0}
        overviews_io._read_progress(io.BytesIO(text), state)
        assert state["percent"] == percent, text


def test_without_gdaladdo_built_in_qgis(tmp_path, no_cache, monkeypatch):
    monkeypatch.setattr(overviews_io, "gdaladdo", lambda: None)
    path = image(tmp_path / "q.tif")
    assert build_external(path, OverviewOptions()).kind == BUILT and overview_count(path) == 4
    other = image(tmp_path / "c.tif")
    with pytest.raises(overviews_io.Cancelled):
        build_external(other, OverviewOptions(), cancelled=lambda: True)
    assert sorted(os.listdir(tmp_path)) == ["c.tif", "q.tif", "q.tif.ovr"]


def test_failing_gdaladdo_falls_back_to_qgis(tmp_path, no_cache, monkeypatch):
    fake = tmp_path / "bin" / "gdaladdo"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'ERROR 1: something went wrong' >&2\nexit 1\n")
    fake.chmod(0o755)
    if sys.platform == "win32":
        pytest.skip("シェルのスクリプトで代用")
    monkeypatch.setattr(overviews_io, "gdaladdo", lambda: str(fake))
    logged = []
    monkeypatch.setattr(overviews_io.QgsMessageLog, "logMessage", lambda text, *a, **k: logged.append(text))
    path = image(tmp_path / "f.tif")
    assert build_external(path, OverviewOptions()).kind == BUILT and overview_count(path) == 4
    assert any("something went wrong" in t and "QGIS の中で作り直し" in t for t in logged)


def test_cancel_stops_gdaladdo(tmp_path, no_cache, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("シェルのスクリプトで代用")
    fake = tmp_path / "bin" / "gdaladdo"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nprintf '0...10'\nexec sleep 60\n")
    fake.chmod(0o755)
    monkeypatch.setattr(overviews_io, "gdaladdo", lambda: str(fake))
    started = []
    popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: started.append(popen(args, **k)) or started[-1])
    path = image(tmp_path / "c.tif")
    seen, clock = [], []

    def cancelled():
        clock.append(1)
        return len(clock) > 5  # after the process has run for a moment

    t0 = time.monotonic()
    with pytest.raises(overviews_io.Cancelled):
        build_external(path, OverviewOptions(), seen.append, cancelled)
    assert time.monotonic() - t0 < 10 and started[0].poll() is not None
    assert seen and seen[-1] == 10 * 0.95
    assert sorted(os.listdir(tmp_path)) == ["bin", "c.tif"]


# ---- location "temp": a temporary folder per QGIS, deleted when it closes (tsudo, 2026-11-22) ----

def test_temp_location_even_when_writable(tmp_path):
    folder = tmp_path / "w"
    folder.mkdir()
    path = image(folder / "img.tif")
    before = digest(path)
    cache = tmp_path / "cache"
    cache.mkdir()
    temp = tmp_path / "temp"
    out = in_fresh_gdal(cache, f"options = OverviewOptions(location='temp', temp_folder={str(temp)!r})\n"
                               "r = o.build_external(" + repr(path) + ", options)\n"
                               "session = o.temp_folder(options, make=False)\n"
                               f"n = count({path!r})\n"
                               "again = o.build_external(" + repr(path) + ", options).kind\n"
                               "size = o.temp_size(options)\n"
                               "o.current = options\n"
                               "left = o.end_session()\n"
                               "gone = not os.path.exists(session)\n"
                               # Next time (the .ovr gone), quietly none: built again into this QGIS's folder.
                               f"m = count({path!r})\n"
                               "r2 = o.build_external(" + repr(path) + ", options)\n"
                               f"print(json.dumps([r.kind, os.path.dirname(r.ovr) == session, n, again, size[0], left, gone, m, r2.kind, count({path!r})]))")
    assert out == [overviews_io.TEMPORARY, True, 4, HAS_OVERVIEWS, 1, 0, True, 0, overviews_io.TEMPORARY, 4]
    # Nothing beside the image; the cache keeps only GDAL's index and the .aux.xml naming the .ovr.
    assert sorted(os.listdir(folder)) == ["img.tif"] and digest(path) == before
    names = os.listdir(cache)
    assert "gdal_pam_proxy.dat" in names and not any(n.endswith(".ovr") for n in names)
    sessions = os.listdir(temp)
    assert len(sessions) == 1 and re.match(r"^mikage-\d+-[0-9a-f]{8}$", sessions[0])
    assert [n for n in os.listdir(temp / sessions[0]) if n.endswith(".ovr")] == [overviews_io.cached_ovr_name(path)]


def test_temp_location_without_cache(tmp_path, no_cache):
    path = image(tmp_path / "n.tif")
    options = OverviewOptions(location="temp", temp_folder=str(tmp_path / "t"))
    assert build_external(path, options).kind == overviews_io.NO_CACHE
    assert sorted(os.listdir(tmp_path)) == ["n.tif"]


def test_temp_session_name_and_running(monkeypatch):
    name = overviews_io.session_name()
    assert re.match(rf"^mikage-{os.getpid()}-[0-9a-f]{{8}}$", name)
    # Kept in the environment: a reloaded plugin (module read again) keeps the same folder; another process does not.
    assert overviews_io.session_name() == name and os.environ[overviews_io.SESSION_ENV] == name
    monkeypatch.setenv(overviews_io.SESSION_ENV, "mikage-1-0123abcd")
    assert overviews_io.session_name() not in (name, "mikage-1-0123abcd")
    monkeypatch.setenv(overviews_io.SESSION_ENV, name)
    assert overviews_io.process_running(os.getpid())
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    assert not overviews_io.process_running(ended.pid) and not overviews_io.process_running(0)


def test_sweep_temp_keeps_running_ones(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        mine = overviews_io.session_name()
        names = {
            "dead": f"mikage-{ended.pid}-0123abcd",
            "running": f"mikage-{running.pid}-0123abcd",
            "mine": mine,
            # An earlier QGIS that had this one's process id.
            "same id": f"mikage-{os.getpid()}-ffffffff",
        }
        for name in names.values():
            (root / name).mkdir()
            (root / name / "x.tif.ovr").write_bytes(b"x")
        # Others in the folder are never touched.
        (root / "mikage-notes").mkdir()
        (root / "mikage-123-0123abcd.txt").write_text("x")
        assert overviews_io.sweep_temp(str(root)) == 2
        assert sorted(os.listdir(root)) == sorted([names["running"], mine, "mikage-notes", "mikage-123-0123abcd.txt"])
        assert overviews_io.sweep_temp(str(tmp_path / "none")) == 0
    finally:
        running.kill()
        running.wait()


def test_temp_swept_at_start_and_remembered(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "system"))
    monkeypatch.setattr(overviews_io, "default_cache_folder", lambda: str(tmp_path / "default"))
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    left = f"mikage-{ended.pid}-0123abcd"
    system = tmp_path / "system" / "MIKAGE" / "overviews" / left
    custom = tmp_path / "custom" / left
    for folder in (system, custom):
        folder.mkdir(parents=True)
        (folder / "a.tif.ovr").write_bytes(b"x")
    gdal.SetConfigOption("GDAL_PAM_PROXY_DIR", None)
    try:
        # The settings file's tempFolder is remembered for the next start (its variables filled in then).
        monkeypatch.setenv("MIKAGE_TEST_ROOT", str(tmp_path))
        overviews_io.configure_cache(OverviewOptions(location="temp", temp_folder="%MIKAGE_TEST_ROOT%/custom"))
        assert settings.overview_temp_setting() == "%MIKAGE_TEST_ROOT%/custom"
        overviews_io.start_cache()
        assert not system.exists() and not custom.exists()
    finally:
        gdal.SetConfigOption("GDAL_PAM_PROXY_DIR", None)
        settings.set_overview_temp_setting("")
        settings.set_overview_cache_setting("")
        overviews_io.current = OverviewOptions()


def test_cache_folder_with_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("MIKAGE_TEST_ROOT", str(tmp_path))
    gdal.SetConfigOption("GDAL_PAM_PROXY_DIR", None)
    try:
        assert overviews_io.configure_cache(OverviewOptions(cache_folder="%MIKAGE_TEST_ROOT%/c")) is None  # none in use yet
        assert overviews_io.start_cache() == str(tmp_path / "c") and (tmp_path / "c").is_dir()
        # The same folder written another way: no restart asked.
        assert overviews_io.configure_cache(OverviewOptions(cache_folder="%MIKAGE_TEST_ROOT%/c")) is None
    finally:
        gdal.SetConfigOption("GDAL_PAM_PROXY_DIR", None)
        settings.set_overview_cache_setting("")
        overviews_io.current = OverviewOptions()


def test_end_session_deletes_this_qgis_folders_only(tmp_path):
    options = OverviewOptions(location="temp", temp_folder=str(tmp_path / "a"))
    first = overviews_io.temp_folder(options)
    # tempFolder changed while QGIS runs: both go.
    second = overviews_io.temp_folder(OverviewOptions(location="temp", temp_folder=str(tmp_path / "b")))
    other = tmp_path / "a" / "mikage-1-0123abcd"
    for folder in (first, second, str(other)):
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, "x.tif.ovr"), "wb").close()
    overviews_io.current = options
    try:
        assert overviews_io.temp_size(options) == (1, 0)
        assert overviews_io.end_session() == 0
        assert not os.path.exists(first) and not os.path.exists(second) and other.is_dir()
        assert overviews_io.temp_size(options) == (0, 0)
    finally:
        overviews_io.current = OverviewOptions()


def test_temp_message(tmp_path, monkeypatch):
    shown = []

    class Bar:
        def pushMessage(self, title, text, level, duration):  # noqa: N802 (QGIS's name)
            shown.append(text)

    monkeypatch.setattr(overviews_io, "reload_layers", lambda path: 0)
    monkeypatch.setattr(overviews_io, "point_to_overviews", lambda path, ovr: None)
    q = OverviewQueue()
    q.message_bar = Bar()
    q._current = str(tmp_path / "t.tif")
    q._ended(overviews_io.Outcome(overviews_io.TEMPORARY, q._current, (2,), str(tmp_path / "x.ovr")), None)
    assert shown == ["t.tif のピラミッドを一時フォルダに作成（QGIS を閉じると消去）"]


def test_settings_dialog_shows_the_temp_folder(tmp_path, monkeypatch):
    from mikage.qgis_io.context import PluginContext
    from mikage.ui.settings_dialog import SettingsDialog

    options = OverviewOptions(location="temp", temp_folder=str(tmp_path / "t"))
    folder = overviews_io.temp_folder(options)
    with open(os.path.join(folder, "x.tif.ovr"), "wb") as f:
        f.write(b"x" * 1048576)
    monkeypatch.setattr(overviews_io, "cache_folder", lambda: str(tmp_path / "cache"))
    overviews_io.current = options
    try:
        dialog = SettingsDialog(PluginContext())
        text = dialog.cache.text()
        assert text.startswith(f"一時フォルダ {folder}（1 件、1.0 MB。QGIS を閉じると消去）\nキャッシュ {tmp_path / 'cache'}（0 件")
        dialog.reject()
        overviews_io.current = OverviewOptions()
        dialog = SettingsDialog(PluginContext())
        assert dialog.cache.text().startswith(str(tmp_path / "cache"))
        dialog.reject()
    finally:
        overviews_io.end_session()
        overviews_io.current = OverviewOptions()


@pytest.mark.parametrize("location", ["cache", "temp"])
def test_before_open_moves_aux_for(location, tmp_path, monkeypatch):
    path = image(tmp_path / "img.tif")
    moved = []
    monkeypatch.setattr(overviews_io, "cache_folder", lambda: str(tmp_path))
    monkeypatch.setattr(overviews_io, "pam_proxy", lambda: (None, None))
    monkeypatch.setattr(overviews_io, "proxy_pams", lambda p, made=None: moved.append(p) or made.append(p + ".aux.xml"))
    overviews_io.current = OverviewOptions(location=location)
    try:
        assert overviews_io.before_open(path) and moved == [path]
        overviews_io.current = OverviewOptions()
        assert not overviews_io.before_open(path) and moved == [path]
    finally:
        overviews_io.current = OverviewOptions()
