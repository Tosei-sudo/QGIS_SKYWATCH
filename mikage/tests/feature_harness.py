"""
A feature's small assembly for its tests (docs/architecture.md 機能ごとの小さな組み立て): the plugin with only the
features a test is about (mikage/features), and 設定・担当・ヘルプ (features/platform.py) that every dock uses.

    from mikage.features.images import Images
    from mikage.tests.feature_harness import small_plugin

    p = small_plugin(iface, Images)   # as classFactory(iface): initGui() next
    p.initGui()

The other features' docks, menu entries and reads ahead are not there, and their ports (ui/ports.py) give None, as
when the roles in use do not have them: a test of the image catalog does not fail because of 観測記録's start-up,
nor wait for it. A test of what goes between two features names both (`small_plugin(iface, Images, Observations)`);
the start with every feature stays for the tests of the whole (the menus, the roles, the layouts, the screens).
"""
from __future__ import annotations

from mikage.features.base import Feature
from mikage.features.platform import Platform


#: A test module's PLUGIN_FEATURES = ALL: every feature (the whole plugin, as classFactory makes it).
ALL = "all"


def small_plugin(iface, *features: type[Feature], request=None):
    """
    The plugin (not started: initGui() next) with only `features`, and the platform's. `request` (the fixture's,
    pytest's): a test module using a fixture of another module (`from mikage.tests.test_qgis_alerts import plugin`)
    names the features its own tests need in its PLUGIN_FEATURES, a tuple of them or ALL.
    """
    from mikage.plugin import MikagePlugin

    wanted = getattr(request.module, "PLUGIN_FEATURES", None) if request is not None else None
    if wanted == ALL:
        return MikagePlugin(iface)
    return MikagePlugin(iface, features=(Platform, *(features if wanted is None else wanted)))
