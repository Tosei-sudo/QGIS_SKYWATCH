"""Each feature starts, works its menu and unloads on its own (tests/feature_harness.py, docs/architecture.md)."""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402

from mikage import plugin as plugin_module  # noqa: E402
from mikage.features import events, images, observations, plan, requests  # noqa: E402
from mikage.features.platform import Platform  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

KINDS = [k for k in plugin_module.FEATURES if k is not Platform]


@pytest.fixture(autouse=True)
def clean():
    QgsSettings().remove("mikage")
    yield
    QgsSettings().remove("mikage")


#: The feature whose port each is (ui/ports.py Ports).
PORTS = {"observations": observations.Observations, "catalog": images.Images, "notices": images.Images,
         "requests": requests.Requests, "events": events.Events, "satellites": plan.Plan}


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.__name__)
def test_a_feature_alone(kind):
    iface = FakeIface()
    p = small_plugin(iface, kind)
    p.initGui()
    try:
        wait_for(lambda: False, 100)
        feature = p.feature(kind)
        # Its docks only (the platform has none), made as in the whole plugin.
        assert set(p.docks) == set(feature.dock_specs())
        # Its ports are there; the other features' give None.
        for name, owner in PORTS.items():
            assert (getattr(p.ports, name)() is not None) == (owner is kind), name
        # Its commands and the platform's; another feature's is not there.
        for name in kind.COMMANDS + Platform.COMMANDS:
            assert callable(getattr(p, name))
        others = {n for k in plugin_module.FEATURES if k not in (kind, Platform) for n in k.COMMANDS}
        for name in others:
            with pytest.raises(AttributeError):
                getattr(p, name)
        # The roles applied: the menu has its entries.
        p.apply_roles()
        assert p.actions
    finally:
        p.unload()
    assert p.docks == {}


def test_every_feature_together_is_the_plugin():
    """Naming them all makes the plugin classFactory makes (the same docks, entries and commands)."""
    whole = plugin_module.MikagePlugin(FakeIface())
    named = small_plugin(FakeIface(), *plugin_module.FEATURES)
    assert [type(f) for f in named.features] == [type(f) for f in whole.features]
    assert sorted(named._commands) == sorted(whole._commands)


def test_a_feature_of_another_plugin_is_refused():
    class Other(Platform):
        pass

    with pytest.raises(ValueError):
        small_plugin(FakeIface(), Other)
