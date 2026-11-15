"""「ツールバーのボタン」: the toolbar's buttons by role (担当), and each person's own per role."""
import re

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QToolButton  # noqa: E402

from mikage.core import roles  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402
from mikage.ui import toolbar_buttons  # noqa: E402
from mikage.ui.dock_layouts import PRESETS  # noqa: E402


@pytest.fixture(autouse=True)
def clean_settings():
    QgsSettings().remove("mikage")
    settings.set_config_source("")
    yield
    QgsSettings().remove("mikage")


@pytest.fixture
def plugin():
    from mikage import classFactory

    plugin = classFactory(FakeIface())
    plugin.initGui()
    yield plugin
    plugin.unload()


def use(plugin, *names):
    """The roles ticked (as on 「担当と個人設定」; no table in these tests: this profile only)."""
    plugin.role_keeper.choose(roles.codes_of(names))


def shown(plugin):
    # (「通知」 shows its unread count, 「通知（1）」, when an earlier test left a notice.)
    return [re.sub(r"^通知（\d+）$", "通知", a.text()) for a in plugin.toolbar.actions()]


def test_without_a_role_the_toolbar_is_as_before(plugin):
    assert shown(plugin) == toolbar_buttons.DEFAULT
    # 「背景地図」 opens its list on a click.
    base_maps = next(a for a in plugin.toolbar.actions() if a.text() == "背景地図")
    assert plugin.toolbar.widgetForAction(base_maps).popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup


def test_every_role_has_its_buttons_and_they_are_menu_entries_it_uses(plugin):
    assert list(toolbar_buttons.ROLE_SETS) == [r.name for r in roles.ROLES]
    # Every example of 「ドックのレイアウト」 is a role (管理者 has none: it opens no docks of its own).
    assert {name for name, _ in PRESETS} <= set(toolbar_buttons.ROLE_SETS)
    texts = {re.sub(r"^通知（\d+）$", "通知", a.text()) for a in plugin.actions}
    for name, buttons in [("", toolbar_buttons.DEFAULT), *toolbar_buttons.ROLE_SETS.items()]:
        assert set(buttons) <= texts, name
        assert len(set(buttons)) == len(buttons) and buttons[-1] == "ツールバーのボタン…"
        # Nothing the role does not use (none: nothing of the administrators').
        codes = roles.codes_of([name]) if name else []
        assert [b for b in buttons if not roles.allows(b, codes)] == [], name
    # Every entry but the administrators' can be chosen without a role, in menu order.
    admin = {t for t in texts if not roles.allows(t, [])}
    assert admin >= {"接続と表の点検…", "監査ログ…", "Python API リファレンス…"}
    assert sorted(plugin.toolbar_buttons.entries) == sorted(texts - admin)
    assert list(plugin.toolbar_buttons.entries)[:3] == ["撮像計画", "衛星パス", "移動目標撮像計画…"]


def test_choosing_a_role_changes_the_buttons(plugin):
    page = plugin.open_first_setup()
    page.role_checks["観測記録の担当"].setChecked(True)
    page.accept()
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["観測記録の担当"]
    # Two roles: the first one's buttons, then the other's new ones; help and this page last.
    page = plugin.open_first_setup()
    page.role_checks["撮像計画の担当"].setChecked(True)
    page.accept()
    both = shown(plugin)
    assert both == toolbar_buttons.standard("撮像計画の担当 ＋ 観測記録の担当")
    assert both[:3] == ["撮像計画", "衛星パス", "移動目標撮像計画…"] and both[-2:] == ["使い方（ヘルプ）…", "ツールバーのボタン…"]
    assert len(set(both)) == len(both) and {"撮像計画ファイル出力", "観測記録", "通報文の作成…"} <= set(both)
    page = plugin.open_first_setup()
    for check in page.role_checks.values():
        check.setChecked(False)
    page.accept()
    assert shown(plugin) == toolbar_buttons.DEFAULT


def test_the_requester_role_asks_and_looks_and_does_not_plan(plugin):
    page = plugin.open_first_setup()
    page.role_checks["撮像を依頼する人"].setChecked(True)
    page.accept()
    APP.processEvents()
    # The ledger and the day's plan at the right, the places' timeline below; nothing to calculate or write files with.
    window, docks = plugin.iface.window, plugin.docks
    opened = sorted(name for name, d in docks.items() if not d.isHidden())
    assert opened == ["SatObsPlaceSchedule", "SatObsRequests", "SatObsSchedule"]
    assert window.dockWidgetArea(docks["SatObsRequests"]) == Qt.DockWidgetArea.RightDockWidgetArea
    assert window.dockWidgetArea(docks["SatObsPlaceSchedule"]) == Qt.DockWidgetArea.BottomDockWidgetArea
    buttons = shown(plugin)
    assert buttons == toolbar_buttons.ROLE_SETS["撮像を依頼する人"]
    assert {"撮像依頼", "撮像予定", "地点ごとの撮像予定"} <= set(buttons)
    assert not {"撮像計画", "撮像計画ファイル出力", "衛星パス"} & set(buttons)


def test_own_buttons_are_kept_per_role(plugin):
    use(plugin, "画像判読")
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
    use(plugin, "撮像計画の担当")
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["撮像計画の担当"]
    use(plugin, "画像判読")
    assert shown(plugin) == mine
    # The same with one more role: kept apart (「画像判読 ＋ 管理者」).
    use(plugin, "画像判読", "管理者")
    assert plugin.open_toolbar_buttons().role_name == "画像判読 ＋ 管理者"
    plugin.toolbar_dialog.reject()
    assert shown(plugin) == toolbar_buttons.standard("画像判読 ＋ 管理者") and "接続と表の点検…" in shown(plugin)
    use(plugin, "画像判読")

    # 「担当の標準に戻す」: forgotten.
    dialog = plugin.open_toolbar_buttons()
    dialog.reset_button.click()
    dialog.accept()
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["画像判読"]
    assert toolbar_buttons.own("画像判読") is None


def test_own_buttons_with_texts_no_longer_there_or_not_of_the_roles(plugin):
    use(plugin, "画像判読")
    toolbar_buttons.set_own("画像判読", ["観測記録", "昔の機能", "背景地図", "撮像計画ファイル出力"])
    plugin.toolbar_buttons.apply()
    # 「撮像計画ファイル出力」 is the planners' (kept, for when the roles have it again).
    assert shown(plugin) == ["観測記録", "背景地図"]
    toolbar_buttons.set_own("画像判読", None)
    plugin.toolbar_buttons.apply()
    assert shown(plugin) == toolbar_buttons.ROLE_SETS["画像判読"]
    # A name no role has (a layout of the person's own, before roles): the standard without a role.
    assert toolbar_buttons.standard("夜間当番") == toolbar_buttons.DEFAULT
    # A broken setting: the standard.
    QgsSettings().setValue(toolbar_buttons.KEY, "{not json")
    assert toolbar_buttons.chosen("画像判読") == toolbar_buttons.ROLE_SETS["画像判読"]
