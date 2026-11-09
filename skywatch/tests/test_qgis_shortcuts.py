"""
Keys for frequent actions (tsudo, 2026-11-07): registered with QGIS's shortcut
manager (so listed and changeable in Settings ▸ Keyboard Shortcuts), the user's
choice applied, not clashing with QGIS's own keys, gone when the plugin unloads.
"""
import os

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.gui import QgsGui  # noqa: E402
from qgis.PyQt.QtGui import QKeySequence  # noqa: E402

from skywatch.qgis_io import settings  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface  # noqa: E402
from skywatch.ui import shortcuts  # noqa: E402

HERE = os.path.dirname(__file__)

#: QGIS 3.34's own keys (the app's actions, the Processing plugin, the Python console):
#: from the strings of libqgis_app / libqgis_gui and the Python plugins.
QGIS_KEYS = {
    "Ctrl+'", "Ctrl++", "Ctrl+,", "Ctrl+-", "Ctrl+.", "Ctrl+;", "Ctrl+<", "Ctrl+=", "Ctrl+>", "Ctrl+Tab", "Ctrl+Shift+Tab",
    *(f"Ctrl+{c}" for c in "0123456789ABCDEFGHIJKLMNOPQRSWXZ[]"), "Ctrl+F3", "Ctrl+F6", "Shift+F6",
    *(f"Ctrl+Alt+{c}" for c in "+-/;=[]AMPV"), "Ctrl+Alt+T", "Ctrl+Alt+G", "Ctrl+Alt+H", "Ctrl+Alt+R", "Ctrl+Alt+F",
    *(f"Ctrl+Shift+{c}" for c in "'.;ABCDFGHIJLMNOPRSTUVWZ[]"),
    "F1", "F2", "F3", "F5", "F6", "F7", "F11", "PgUp", "PgDown", "Del", "Esc",
}


@pytest.fixture
def plugin():
    from skywatch import classFactory

    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    settings.set_startup_layers_on(False)
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p
    p.unload()
    QgsSettings().remove("shortcuts")


def norm(keys: str) -> str:
    return QKeySequence(keys).toString()


def test_the_main_window_keys_do_not_clash_with_qgis_or_each_other():
    keys = [norm(k) for _n, _l, k in shortcuts.GLOBAL]
    assert len(set(keys)) == len(keys)
    assert not set(keys) & {norm(k) for k in QGIS_KEYS}


def test_every_key_is_in_qgis_shortcut_settings(plugin):
    manager = QgsGui.shortcutsManager()
    for name, label, default in shortcuts.GLOBAL:
        action = manager.actionByName(shortcuts.PREFIX + name)
        assert action is plugin.shortcuts.actions[name]
        assert action.text() == label
        assert action.shortcut().toString() == norm(default)
        assert manager.defaultKeySequence(action) == default
    # The docks' keys too, with a label each.
    activity = manager.shortcutByName("SatObsKeyObsActivity1")
    assert activity.key().toString() == "1" and activity.whatsThis() == "観測記録（ドック内）: 活動 1 番目"
    assert manager.shortcutByName("SatObsKeyObsNextImage").key().toString() == norm("PgDown")
    assert manager.shortcutByName("SatObsKeyPlanRun").key().toString() == norm("Ctrl+Return")
    assert manager.shortcutByName("SatObsKeyTaxonDelete").key().toString() == norm("Del")
    names = [s.objectName() for s in manager.listShortcuts() if s.objectName().startswith(shortcuts.PREFIX)]
    assert len(names) == len(set(names)) == 9 + 9 + 5 + 3 + 4


def test_a_key_changed_in_qgis_is_used_and_kept(plugin):
    manager = QgsGui.shortcutsManager()
    action = plugin.shortcuts.actions["ShowCatalog"]
    assert manager.setKeySequence(action, "Ctrl+Alt+F9")
    assert action.shortcut().toString() == norm("Ctrl+Alt+F9")
    assert manager.setObjectKeySequence(manager.shortcutByName("SatObsKeyObsActivity1"), "F9")
    # Loaded again (QGIS restarted): the keys chosen, not the defaults.
    plugin.unload()
    assert manager.actionByName("SatObsKeyShowCatalog") is None
    assert manager.shortcutByName("SatObsKeyObsActivity1") is None
    plugin.initGui()
    assert plugin.shortcuts.actions["ShowCatalog"].shortcut().toString() == norm("Ctrl+Alt+F9")
    assert manager.shortcutByName("SatObsKeyObsActivity1").key().toString() == "F9"


def test_the_keys_run_their_actions(plugin):
    dock = plugin.docks["SatObsObservations"]
    steps, toggles = [], []
    dock.step = lambda by: steps.append(by)
    dock.toggle_recording = lambda: toggles.append(True)
    plugin.shortcuts.actions["NextImage"].trigger()
    plugin.shortcuts.actions["PreviousImage"].trigger()
    plugin.shortcuts.actions["ToggleRecording"].trigger()
    assert steps == [1, -1] and toggles == [True]
    # The observations dock was opened for them.
    assert dock.isVisibleTo(plugin.iface.mainWindow())
    plugin.docks["SatObsRequests"].hide()
    plugin.shortcuts.actions["ShowRequests"].trigger()
    assert plugin.docks["SatObsRequests"].isVisibleTo(plugin.iface.mainWindow())
