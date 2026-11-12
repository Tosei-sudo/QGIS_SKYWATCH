"""「最初の設定」: shown only on the first start in a profile, the role's docks, docks in windows of their own, own settings."""
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402

from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui import first_setup  # noqa: E402


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    QgsSettings().remove("mikage")
    monkeypatch.setattr(first_setup, "ENABLED", True)
    yield
    QgsSettings().remove("mikage")


def start(fresh=True):
    from mikage import classFactory

    iface = FakeIface()
    iface.window.resize(1400, 900)
    if fresh:  # as on a profile the plugin never ran in
        QgsSettings().remove("mikage")
    plugin = classFactory(iface)
    plugin.initGui()
    return plugin


def open_names(plugin):
    return sorted(n for n, d in plugin.docks.items() if d.isVisible())


def test_the_page_comes_on_the_first_start_only():
    plugin = start()
    wait_for(lambda: plugin.setup_dialog is not None and plugin.setup_dialog.isVisible(), ms=5000)
    page = plugin.setup_dialog
    assert page.windowTitle() == "最初の設定" and page.first
    # The examples of 「ドックのレイアウト」 to choose from, the first chosen, and none.
    assert list(page.role_buttons) == ["撮像計画の担当", "観測記録の担当", "画像判読", ""]
    assert page.chosen_role() == "撮像計画の担当"
    page.reject()  # 「あとで」 also counts: not again
    plugin.unload()
    assert QgsSettings().value(first_setup.DONE_KEY) == "1"
    plugin = start(fresh=False)
    APP.processEvents()
    assert not first_setup.is_first_start() and plugin.setup_dialog is None
    plugin.unload()


def test_an_update_of_a_profile_used_before_shows_nothing():
    # Before this page: the plugin's settings are there, the mark is not.
    QgsSettings().setValue("mikage/recorder", "山田")
    assert not first_setup.is_first_start()
    assert QgsSettings().value(first_setup.DONE_KEY) == "1"


def test_a_role_with_docks_in_windows_of_their_own_and_own_settings():
    plugin = start()
    wait_for(lambda: plugin.setup_dialog is not None, ms=5000)
    page = plugin.setup_dialog
    page.role_buttons["観測記録の担当"].setChecked(True)
    page.floating.setChecked(True)
    page.recorder.setText("  佐藤 ")
    page.points.setCurrentIndex(1)
    page.notation.setCurrentIndex(page.notation.findData("mgrs"))
    page.accept()
    APP.processEvents()
    assert open_names(plugin) == ["SatObsAiResults", "SatObsEnhance", "SatObsImageCatalog", "SatObsObservations", "SatObsTaxonomy"]
    # Every dock in its own window, the closed ones too (so they open as windows later), within the screen.
    assert all(d.isFloating() for d in plugin.docks.values())
    shown = [plugin.docks[n].geometry().topLeft() for n in open_names(plugin)]
    assert len(set((p.x(), p.y()) for p in shown)) == len(shown)  # cascaded, not on top of each other
    assert first_setup.docks_floating() and first_setup.role() == "観測記録の担当"
    assert plugin.dock_layouts.store.last == "観測記録の担当"
    obs = plugin.docks["SatObsObservations"]
    assert settings.recorder_setting() == "佐藤" and obs.observer_input.text() == "佐藤"
    assert settings.point_count_setting() == 2 and obs.points == 2 and obs.points_select.currentData() == 2
    assert not obs.active  # choosing the clicks does not start recording
    assert plugin.coordinates.notation == "mgrs"

    # With windows chosen, another layout and 「ドックを初期位置に戻す」 open their docks as windows too.
    assert plugin.dock_layouts.use("画像判読")
    assert all(plugin.docks[n].isFloating() for n in open_names(plugin))
    plugin.place_docks()
    assert all(d.isFloating() for d in plugin.docks.values())

    # From the menu again: back to joined to the main window.
    page = plugin.open_first_setup()
    assert page.windowTitle() == "担当と個人設定" and page.floating.isChecked() and page.chosen_role() == "観測記録の担当"
    assert page.recorder.text() == "佐藤"
    page.role_buttons["撮像計画の担当"].setChecked(True)
    page.joined.setChecked(True)
    page.accept()
    APP.processEvents()
    assert not first_setup.docks_floating()
    assert not any(d.isFloating() for d in plugin.docks.values())
    assert "SatObsImagingPlan" in open_names(plugin) and "SatObsObservations" not in open_names(plugin)
    plugin.unload()


def test_no_role_keeps_the_docks():
    plugin = start()
    wait_for(lambda: plugin.setup_dialog is not None, ms=5000)
    before = open_names(plugin)
    page = plugin.setup_dialog
    page.role_buttons[""].setChecked(True)
    page.accept()
    APP.processEvents()
    assert open_names(plugin) == before and not any(d.isFloating() for d in plugin.docks.values())
    assert first_setup.role() == ""
    plugin.unload()
