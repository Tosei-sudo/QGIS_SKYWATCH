"""「ドックのレイアウト」: named layouts of the plugin's docks, saved, used again, deleted, and moved by file."""
import json

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QRect, Qt  # noqa: E402

from skywatch.tests.qgis_fakes import FakeIface  # noqa: E402

RIGHT, LEFT, BOTTOM = Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea


@pytest.fixture(autouse=True)
def clean_settings():
    QgsSettings().remove("skywatch")
    yield
    QgsSettings().remove("skywatch")


@pytest.fixture
def plugin():
    from skywatch import classFactory

    iface = FakeIface()
    iface.window.resize(1400, 900)
    plugin = classFactory(iface)
    plugin.initGui()
    APP.processEvents()
    yield plugin
    plugin.unload()


def open_names(plugin):
    return sorted(n for n, d in plugin.docks.items() if d.isVisible())


def tabs(window, dock):
    return sorted(x.objectName() for x in [dock, *window.tabifiedDockWidgets(dock)])


def entries(menu):
    menu.rebuild()
    return [("-" if a.isSeparator() else a.text()) for a in menu.actions()]


def test_a_layout_is_saved_and_brought_back(plugin):
    w, d, menu = plugin.iface.window, plugin.docks, plugin.dock_layouts
    # The examples are offered at first; nothing of one's own to delete yet.
    assert entries(menu) == ["撮像計画の担当", "観測記録の担当", "画像判読", "-", "現在の配置を保存…", "削除", "-",
                             "ファイルへ書き出し…", "ファイルから読み込み…"]
    assert not menu.actions()[5].menu().isEnabled()
    # Arranged by hand: observations and taxonomy as tabs at the bottom, the AI results floating, the catalog left.
    for name in d:
        d[name].hide()
    w.addDockWidget(BOTTOM, d["SatObsObservations"])
    w.addDockWidget(BOTTOM, d["SatObsTaxonomy"])
    w.tabifyDockWidget(d["SatObsObservations"], d["SatObsTaxonomy"])
    w.addDockWidget(LEFT, d["SatObsImageCatalog"])
    for name in ("SatObsObservations", "SatObsTaxonomy", "SatObsImageCatalog", "SatObsAiResults"):
        d[name].show()
    d["SatObsAiResults"].setFloating(True)
    d["SatObsAiResults"].setGeometry(QRect(200, 150, 320, 240))
    d["SatObsTaxonomy"].raise_()
    APP.processEvents()
    menu.save_current("観測記録の担当（自分用）")
    saved = menu.store.saved()["観測記録の担当（自分用）"]
    taxonomy = next(g for g in saved["groups"] if any(e["name"] == "SatObsTaxonomy" for e in g["docks"]))
    assert taxonomy["area"] == "bottom" and taxonomy["front"] == "SatObsTaxonomy"
    assert [f["name"] for f in saved["floating"]] == ["SatObsAiResults"] and saved["floating"][0]["geometry"][:2] == [200, 150]

    # Something else in between, then the saved one again.
    plugin.place_docks()
    assert menu.use("撮像計画の担当")
    APP.processEvents()
    assert open_names(plugin) == ["SatObsImageCatalog", "SatObsImagingPlan", "SatObsPlanFiles", "SatObsRequests", "SatObsSatellitePass", "SatObsSavedPlans"]
    assert w.dockWidgetArea(d["SatObsImagingPlan"]) == RIGHT and tabs(w, d["SatObsImagingPlan"]) == ["SatObsImagingPlan", "SatObsSatellitePass", "SatObsSavedPlans"]
    assert d["SatObsImagingPlan"].geometry().y() < d["SatObsRequests"].geometry().y()
    assert menu.use("観測記録の担当（自分用）")
    APP.processEvents()
    assert open_names(plugin) == ["SatObsAiResults", "SatObsImageCatalog", "SatObsObservations", "SatObsTaxonomy"]
    assert w.dockWidgetArea(d["SatObsObservations"]) == BOTTOM and tabs(w, d["SatObsObservations"]) == ["SatObsObservations", "SatObsTaxonomy"]
    assert not d["SatObsTaxonomy"].visibleRegion().isEmpty() and d["SatObsObservations"].visibleRegion().isEmpty()
    assert d["SatObsAiResults"].isFloating() and d["SatObsAiResults"].geometry() == QRect(*saved["floating"][0]["geometry"])
    assert w.dockWidgetArea(d["SatObsImageCatalog"]) == LEFT and not d["SatObsImageCatalog"].isFloating()
    # The one last used is checked; saved ones can be deleted, the examples not.
    menu.rebuild()
    assert [a.text() for a in menu.actions() if a.isChecked()] == ["観測記録の担当（自分用）"]
    delete = next(a for a in menu.actions() if a.text() == "削除").menu()
    assert [a.text() for a in delete.actions()] == ["観測記録の担当（自分用）"]
    menu.store.delete("観測記録の担当（自分用）")
    assert "観測記録の担当（自分用）" not in entries(menu) and not menu.use("観測記録の担当（自分用）")


def test_saving_under_an_example_s_name_replaces_it(plugin):
    menu, d = plugin.dock_layouts, plugin.docks
    for dock in d.values():
        dock.hide()
    d["SatObsObservations"].show()
    APP.processEvents()
    menu.save_current("撮像計画の担当")
    assert entries(menu)[:3] == ["観測記録の担当", "画像判読", "撮像計画の担当"]
    plugin.place_docks()
    d["SatObsImagingPlan"].show()
    menu.use("撮像計画の担当")
    assert open_names(plugin) == ["SatObsObservations"]
    # Deleted: the example comes back.
    menu.store.delete("撮像計画の担当")
    assert entries(menu)[:3] == ["撮像計画の担当", "観測記録の担当", "画像判読"]


def test_a_dock_the_layout_does_not_name_is_closed_and_broken_settings_are_ignored(plugin):
    menu = plugin.dock_layouts
    menu.store.save("一つだけ", {"groups": [{"area": "left", "docks": [{"name": "SatObsSpectral", "visible": True}, {"name": "SatObsGone"}],
                                            "front": "SatObsSpectral"}], "floating": []})
    plugin.docks["SatObsObservations"].show()
    assert menu.use("一つだけ")
    assert open_names(plugin) == ["SatObsSpectral"]
    assert plugin.iface.window.dockWidgetArea(plugin.docks["SatObsSpectral"]) == LEFT
    QgsSettings().setValue("skywatch/dockLayouts", "{not json")
    assert menu.store.saved() == {} and "撮像計画の担当" in entries(menu)


def test_layouts_go_to_a_file_and_come_back_from_it(plugin, tmp_path):
    menu = plugin.dock_layouts
    plugin.place_docks()
    menu.save_current("撮像の担当 A")
    path = tmp_path / "layouts.json"
    assert menu.store.export(str(path)) == 4
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "skywatch.dockLayouts" and "撮像の担当 A" in data["layouts"]
    QgsSettings().remove("skywatch/dockLayouts")
    assert menu.store.import_file(str(path)) == ["撮像計画の担当", "観測記録の担当", "画像判読", "撮像の担当 A"]
    assert "撮像の担当 A" in menu.store.saved()
    other = tmp_path / "other.json"
    other.write_text('{"layouts": {}}', encoding="utf-8")
    with pytest.raises(ValueError):
        menu.store.import_file(str(other))


def test_a_broken_file_s_layouts_are_cleaned_before_use(plugin, tmp_path):
    menu = plugin.dock_layouts
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"kind": "skywatch.dockLayouts", "layouts": {
        " 壊れた ": {"groups": [{"area": "middle", "docks": [{"name": "SatObsObservations"}, "x", {"name": 3}], "size": ["a", 1]},
                               "not a group"],
                    "floating": [{"name": "SatObsRequests", "geometry": [1, 2, "x", 4]}, 5]},
        "空でない": "not a layout"}}), encoding="utf-8")
    assert menu.store.import_file(str(path)) == ["壊れた"]
    layout = menu.store.saved()["壊れた"]
    assert layout["groups"] == [{"area": "right", "docks": [{"name": "SatObsObservations", "visible": True}],
                                 "front": "SatObsObservations", "size": None}]
    assert layout["floating"][0]["geometry"] is None
    assert menu.use("壊れた")
    assert plugin.docks["SatObsObservations"].isVisible() and plugin.docks["SatObsRequests"].isFloating()
