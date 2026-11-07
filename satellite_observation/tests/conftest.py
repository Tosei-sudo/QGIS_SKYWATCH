import importlib.util
import os
import socketserver
import sys

# One BLAS thread per test process: the AI tests' NumPy otherwise starts a thread per core in each
# pytest-xdist worker, and the threads fight over the cores (slower than one process). Before NumPy loads.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

# The plugin's folder's parent, so `satellite_observation` imports as a package.
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
        from satellite_observation.core.esri import EsriClient, EsriError
        from satellite_observation.qgis_io.context import PluginContext
    except ImportError:  # no QGIS
        return

    def offline(url, form):
        raise EsriError(f"テストでは通信なし（{url}）")

    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(offline))


@pytest.fixture(autouse=True)
def no_time_zone_rules():
    """Each test starts with no settings file's time zones (they are module state, as in the viewer)."""
    from satellite_observation.core import time_zone

    yield
    time_zone.set_rules([])


@pytest.fixture(autouse=True)
def default_edit_tracking():
    """Each test starts with the default tracking fields (module state, set from the settings file)."""
    from satellite_observation.core import edit_stamp

    yield
    edit_stamp.set_tracking(None)
