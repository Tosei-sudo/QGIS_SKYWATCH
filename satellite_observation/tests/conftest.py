import os
import sys

# The plugin's folder's parent, so `satellite_observation` imports as a package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest


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
