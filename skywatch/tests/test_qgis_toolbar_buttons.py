"""「ツールバーのボタン」: the toolbar's buttons by role (担当), and each person's own per role."""
import re

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QToolButton  # noqa: E402

from skywatch.qgis_io import settings  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface  # noqa: E402
from skywatch.ui import first_setup, toolbar_buttons  # noqa: E402
from skywatch.ui.dock_layouts import PRESETS  # noqa: E402


@pytest.fixture(autouse=True)
def clean_settings():
    QgsSettings().remove("skywatch")
    settings.set_config_source("")
    yield
    QgsSettings().remove("skywatch")


@pytest.fixture
def plugin():
    from skywatch import classFactory

    plugin = classFactory(FakeIface())
    plugin.initGui()
    yield plugin
    plugin.unload()


def shown(plugin):
    # (「通知」 shows its unread count, 「通知（1）」, when an earlier test left a notice.)
    return [re.sub(r"^通知（\d+）$", "通知", a.text()) for a in plugin.toolbar.actions()]


def test_without_a_role_the_toolbar_is_as_before(plugin):
    assert shown(plugin) == toolbar_buttons.DEFAULT
    # 「背景地図」 opens its list on a click.
    base_maps = next(a for a in plugin.toolbar.actions() if a.text() == "背景地図")
    assert plugin.toolbar.widgetForAction(base_maps).popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup


def test_every_role_of_the_examples_has_its_buttons_and_they_are_menu_entries(plugin):
    assert list(toolbar_buttons.ROLE_SETS) == [name for name, _ in PRESETS]
    texts = {re.sub(r"^通知（\d+）$", "通知", a.text()) for a in plugin.actions}
    for name, buttons in [("", toolbar_buttons.DEFAULT), *toolbar_buttons.ROLE_SETS.items()]:
        assert set(buttons) <= texts, name
        assert len(set(buttons)) == len(buttons) and buttons[-1] == "ツールバーのボタン…"
    # Every entry can be chosen, in menu order.
    assert sorted(plugin.toolbar_buttons.entries) == sorted(texts)
    assert list(plugin.toolbar_buttons.entries)[:3] == ["撮像計画", "保存した撮像計画", "衛星パス"]


def test_choosing_a_role_changes_the_buttons(plugin):
    page = plugin.open_first_setup()
    page.role_buttons["観測記録の担当"].setChecked(True)
    page.accept()
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["観測記録の担当"]
    page = plugin.open_first_setup()
    page.role_buttons[""].setChecked(True)
    page.accept()
    assert shown(plugin) == toolbar_buttons.DEFAULT


def test_own_buttons_are_kept_per_role(plugin):
    first_setup.set_role("画像判読")
    plugin.toolbar_buttons.apply()
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["画像判読"]

    dialog = plugin.open_toolbar_buttons()
    assert dialog.isVisible() and dialog.role_name == "画像判読"
    items = {dialog.list.item(i).text(): dialog.list.item(i) for i in range(dialog.list.count())}
    # The ticked ones first, in toolbar order; then the rest.
    assert dialog.picked() == toolbar_buttons.ROLE_SETS["画像判読"]
    items["スペクトル"].setCheckState(Qt.CheckState.Unchecked)
    items["パンシャープン…"].setCheckState(Qt.CheckState.Checked)
    dialog.list.setCurrentItem(items["観測記録"])
    dialog.move(-1)
    dialog.move(-1)
    dialog.accept()
    mine = shown(plugin)
    assert "スペクトル" not in mine and "パンシャープン…" in mine
    assert mine.index("観測記録") == toolbar_buttons.ROLE_SETS["画像判読"].index("観測記録") - 2
    assert toolbar_buttons.own("画像判読") == mine

    # Another role: its standard; back: the person's own again.
    first_setup.set_role("撮像計画の担当")
    plugin.toolbar_buttons.apply()
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["撮像計画の担当"]
    first_setup.set_role("画像判読")
    plugin.toolbar_buttons.apply()
    assert shown(plugin) == mine

    # 「担当の標準に戻す」: forgotten.
    dialog = plugin.open_toolbar_buttons()
    dialog.reset_button.click()
    dialog.accept()
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["画像判読"]
    assert toolbar_buttons.own("画像判読") is None


def test_a_role_of_the_persons_own_layout_and_texts_no_longer_there(plugin):
    first_setup.set_role("夜間当番")
    toolbar_buttons.set_own("夜間当番", ["観測記録", "昔の機能", "背景地図"])
    plugin.toolbar_buttons.apply()
    assert shown(plugin) == ["観測記録", "背景地図"]
    toolbar_buttons.set_own("夜間当番", None)
    plugin.toolbar_buttons.apply()
    assert shown(plugin) == toolbar_buttons.DEFAULT
    # A broken setting: the standard.
    QgsSettings().setValue(toolbar_buttons.KEY, "{not json")
    assert toolbar_buttons.chosen("画像判読") == toolbar_buttons.ROLE_SETS["画像判読"]
