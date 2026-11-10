import importlib.util
import os
import socketserver
import sys

# One BLAS thread per test process: the AI tests' NumPy otherwise starts a thread per core in each
# pytest-xdist worker, and the threads fight over the cores (slower than one process). Before NumPy loads.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

# The plugin's folder's parent, so `skywatch` imports as a package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy  # noqa: E402
import pytest  # noqa: E402

# The tests' local HTTP servers stop within 0.05 s of shutdown() (0.5 s by default, for each of ~30 tests).
socketserver.BaseServer.serve_forever.__defaults__ = (0.05,)


def pytest_addoption(parser):
    parser.addoption("--full", action="store_true", default=False,
                     help="also the slow tests (AI training until it learns); SATOBS_FULL_TESTS=1 does the same")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: long tests (AI training until it learns), only with --full")
    # With pytest-timeout (requirements-test.txt): a test that hangs (Qt waiting in C++, where Python's signals
    # never come) is stopped after 5 minutes with every thread's stack, instead of holding the run for good.
    if config.pluginmanager.hasplugin("timeout") and not getattr(config.option, "timeout", None):
        config.option.timeout = 300
        config.option.timeout_method = "thread"
    # QGIS / GDAL on the system are built against NumPy 1.x: with NumPy 2 (e.g. pulled in by `pip install onnxruntime`)
    # about 200 tests fail or crash in GDAL. Say so once instead.
    if int(numpy.__version__.split(".")[0]) >= 2 and importlib.util.find_spec("osgeo") is not None:
        try:
            from osgeo import gdal  # noqa: F401
        except Exception:  # noqa: BLE001 - no GDAL for this Python: the QGIS tests skip themselves
            gdal = None
        try:
            if gdal is not None:
                from osgeo import gdal_array  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise pytest.UsageError(
                f"NumPy {numpy.__version__} では QGIS / GDAL が動かない（{e}）。"
                "`pip uninstall numpy` でシステムの NumPy 1.x に戻すか、`pip install \"numpy<2\"`") from e


def pytest_collection_modifyitems(config, items):
    if config.getoption("--full") or os.environ.get("SATOBS_FULL_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="遅いテスト（--full で実行）")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_esri_network(monkeypatch):
    """
    No request leaves the tests: the plugin's Esri client fails at once until a
    test puts its FakeEsri in (initGui reads the settings file and asks the
    catalogs before the test can, and a real request may hang without a network).
    """
    try:
        from skywatch.core.esri import EsriClient, EsriError
        from skywatch.qgis_io.context import PluginContext
    except ImportError:  # no QGIS
        return

    def offline(url, form):
        raise EsriError(f"テストでは通信なし（{url}）")

    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(offline))


@pytest.fixture(autouse=True)
def own_offline_file(monkeypatch, tmp_path):
    """Each test has its own file of offline work (the cache and what waits): none is left from another test."""
    try:
        from skywatch.qgis_io import settings
    except ImportError:  # no QGIS
        return
    monkeypatch.setattr(settings, "offline_path", lambda: str(tmp_path / "offline_work.sqlite"))


@pytest.fixture(autouse=True)
def no_upload_question(monkeypatch):
    """
    The question about the work waiting (a modal box) never opens by itself in a test: nobody answers it, and
    the run would hang until its timeout. The tests that ask put their own answer in.
    """
    try:
        from skywatch.ui import offline_status
    except ImportError:  # no QGIS
        return
    monkeypatch.setattr(offline_status, "ask_upload", lambda parent, work, back: "later")


@pytest.fixture(autouse=True)
def own_place_cache(monkeypatch, tmp_path):
    """Each test has its own 地名・国の控え (memory and file): none is left from another test."""
    from skywatch.core.obs import place_cache

    try:
        from skywatch.qgis_io import settings
    except ImportError:  # no QGIS
        settings = None
    if settings is not None:
        monkeypatch.setattr(settings, "place_cache_path", lambda: str(tmp_path / "place_cache.sqlite"))
    place_cache.PLACES.close()
    yield
    place_cache.PLACES.close()


@pytest.fixture(autouse=True)
def no_time_zone_rules():
    """Each test starts with no settings file's time zones (they are module state, as in the viewer)."""
    from skywatch.core import time_zone

    yield
    time_zone.set_rules([])


@pytest.fixture(autouse=True)
def default_edit_tracking():
    """Each test starts with the default tracking fields (module state, set from the settings file)."""
    from skywatch.core import edit_stamp

    yield
    edit_stamp.set_tracking(None)


@pytest.fixture(autouse=True)
def no_first_setup_page(monkeypatch):
    """「最初の設定」 does not pop up in every plugin test (tests/test_qgis_first_setup.py turns it on)."""
    try:
        from skywatch.ui import first_setup
    except ImportError:  # no QGIS
        return
    monkeypatch.setattr(first_setup, "ENABLED", False)


@pytest.fixture(autouse=True)
def no_preload(monkeypatch):
    """「先読み」 does not start reads of its own in every plugin test (tests/test_qgis_preload.py turns it on)."""
    try:
        from skywatch.qgis_io import preload
    except ImportError:  # no QGIS
        return
    monkeypatch.setattr(preload.Preloader, "DELAY_MS", 24 * 3600 * 1000)
