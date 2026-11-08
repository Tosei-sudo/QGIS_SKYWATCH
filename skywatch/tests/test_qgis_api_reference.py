"""「Python API リファレンス」: every name of plugin.api has a page, the members named exist, examples go to the clipboard."""
import ast

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from skywatch.core.api_reference import ENTRIES, GROUPS, HOW_TO  # noqa: E402
from skywatch.plugin import PluginApi  # noqa: E402
from skywatch.tests.test_qgis_alerts import plugin  # noqa: E402,F401


@pytest.fixture(autouse=True)
def empty_project():
    yield
    QgsProject.instance().removeAllMapLayers()


def test_every_name_of_the_api_has_a_page_and_every_example_is_python():
    names = {n for n in dir(PluginApi) if not n.startswith("_")}
    pages = {e.name for e in ENTRIES}
    assert names - pages == set(), "API にあってリファレンスにない"
    assert {n for n in pages if ":" not in n and n != "intro"} - names == set(), "リファレンスにあって API にない"
    assert all(e.group in GROUPS and e.title and e.text and e.example for e in ENTRIES)
    for e in ENTRIES:
        if e.group != "式の関数":
            ast.parse(HOW_TO + "\n" + e.example)


def test_the_dialog_from_the_menu_shows_arguments_and_members(plugin):  # noqa: F811
    p, _iface, _dock, _ = plugin
    action = next(a for a in p.actions if a.objectName() == "SatObsApiReference")
    action.trigger()
    dialog = p.api_reference
    assert dialog.isVisible() and dialog.current == "intro"
    # The members named exist on the docks and tools.
    missing = []
    for e in ENTRIES:
        if not e.members:
            continue
        target = getattr(p.api, e.name)
        missing += [f"{e.name}.{m}" for m in e.members if not hasattr(target, m)]
    assert missing == []
    assert dialog.choose("add_observation")
    html = dialog.page_html("add_observation")
    assert "add_observation(lon, lat, image=None" in html and "記録を 1 件追加" in html
    html = dialog.page_html("analysis")
    assert "api.analysis" in html and "algorithm_id(name=None)" in html and "set_sql(" in html
    assert dialog.choose("analysis")
    text = dialog.copy_example()
    assert text.startswith(HOW_TO) and QApplication.clipboard().text() == text and 'a.load("モデル別")' in text
    assert dialog.choose("expression:places") and dialog.copy_example() == "skywatch_place($geometry)\narray_to_string(skywatch_places($geometry), '・')"
    dialog.filter.setText("スワイプ")
    shown = [dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount()) if not dialog.tree.topLevelItem(i).isHidden()]
    assert [g.text(0) for g in shown] == ["ドックと道具"]
