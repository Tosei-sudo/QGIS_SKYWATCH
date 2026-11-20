"""
The features' 登録票 (mikage/features, docs/architecture.md): each module one feature the plugin
lists, every dock placed, every command and right-click entry answered once.
"""
import importlib
import inspect
import os

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from mikage import plugin as plugin_module  # noqa: E402
from mikage.features.base import Feature  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402

FEATURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "features")


def _feature_modules():
    for f in sorted(os.listdir(FEATURES_DIR)):
        if f.endswith(".py") and f not in ("__init__.py", "base.py"):
            yield importlib.import_module(f"mikage.features.{f[:-3]}")


def test_each_module_is_one_listed_feature():
    listed = set(plugin_module.FEATURES)
    for module in _feature_modules():
        found = [c for _n, c in inspect.getmembers(module, inspect.isclass) if issubclass(c, Feature) and c is not Feature
                 and c.__module__ == module.__name__]
        assert len(found) == 1, module.__name__
        assert found[0] in listed, f"{found[0].__name__} が plugin.FEATURES にない"
    assert len(listed) == len(plugin_module.FEATURES)


def test_every_dock_entry_and_command_has_its_place():
    p = plugin_module.MikagePlugin(FakeIface())
    docks = [name for f in p.features for name in f.dock_specs()]
    assert sorted(docks) == sorted(plugin_module.DOCK_ORDER) and len(set(docks)) == len(docks)
    placed = [n for _area, names in plugin_module.DOCK_GROUPS for n in names]
    assert sorted(placed) == sorted(docks)
    texts = [t for f in p.features for t in f.map_menu()]
    assert sorted(texts) == sorted(plugin_module.MAP_MENU)
    keys = [item[0] for f in p.features for item in f.preload()]
    assert sorted(keys) == sorted(plugin_module.PRELOAD_ORDER)
    commands = [c for f in p.features for c in f.COMMANDS]
    assert len(set(commands)) == len(commands)
    for name in commands:
        assert callable(getattr(p, name)) and not hasattr(plugin_module.MikagePlugin, name), name
    with pytest.raises(AttributeError):
        p.open_nothing  # noqa: B018 (not a command: as any other missing attribute)


def test_a_feature_touches_only_its_own_docks():
    """Another feature's dock only through its port (ui/ports.py): no other dock's name in a feature's module."""
    import ast

    p = plugin_module.MikagePlugin(FakeIface())
    docks = set(plugin_module.DOCK_ORDER)
    for feature in p.features:
        module = inspect.getmodule(type(feature))
        own = set(feature.dock_specs())
        named = {n.value for n in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(n, ast.Constant) and n.value in docks}
        assert named <= own, f"{module.__name__} が他の機能のドックを直接さわる: {sorted(named - own)}"


@pytest.mark.parametrize("port, dock", [
    ("ObservationsPort", "mikage.ui.observation_dock.ObservationDock"),
    ("CatalogPort", "mikage.ui.catalog_dock.CatalogDock"),
    ("RequestsPort", "mikage.ui.request_dock.RequestDock"),
    ("EventsPort", "mikage.ui.events_dock.EventsDock"),
    ("NoticesPort", "mikage.ui.notices_dock.NoticesDock"),
    ("SatellitesPort", "mikage.features.plan.PlanSatellites"),
])
def test_the_ports_are_kept(port, dock):
    """Each port's dock (or adapter) has what the port lists (names, and its functions' arguments)."""
    from mikage.ui import ports

    protocol = getattr(ports, port)
    module, name = dock.rsplit(".", 1)
    cls = getattr(importlib.import_module(module), name)
    for member, value in vars(protocol).items():
        if member.startswith("_"):
            continue
        assert hasattr(cls, member), f"{name} に {member} がない"
        if inspect.isfunction(value) and member not in ("show", "raise_"):
            wanted = list(inspect.signature(value).parameters)
            have = list(inspect.signature(getattr(cls, member)).parameters)
            assert have[:len(wanted)] == wanted, f"{name}.{member}{have} ≠ {port}{wanted}"
    for member in getattr(protocol, "__annotations__", {}):
        assert member in ("book",) or hasattr(cls, member), f"{name} に {member} がない"
