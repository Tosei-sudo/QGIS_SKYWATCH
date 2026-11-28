"""機能の使われ方 in QGIS (ui/feature_use.py, ui/feature_use_dialog.py): counted where chosen, kept, sent and shown."""
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QAbstractButton, QComboBox, QPushButton, QTabBar  # noqa: E402

from mikage.core import feature_use as fu  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.training import sample  # noqa: E402
from mikage.core.training import server as mock_esri  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui import feature_use  # noqa: E402
from mikage.tests.test_feature_use import table_rows, transport  # noqa: E402


@pytest.fixture
def usage(tmp_path):
    store = mock_esri.Store(str(tmp_path / "usage.sqlite"))
    mock_esri.load_services(store, {"services": [{"name": "Usage", "layers": [
        {"id": 0, "name": "機能の使われ方", "fields": sample.FEATURE_USE_FIELDS, "features": []}]}]})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mock_esri.make_handler(mock_esri.Service(store, log=lambda *_: None), str(tmp_path), quiet=True))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/arcgis/rest/services/Usage/FeatureServer/0", store
    httpd.shutdown()
    httpd.server_close()


def start(tmp_path, monkeypatch, config: dict):
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    settings.set_config_source(str(path))
    settings.set_startup_layers_on(False)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(transport))
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    assert wait_for(lambda: not p.context.loading)
    return p, iface


@pytest.fixture
def plugin(tmp_path, monkeypatch, usage):
    url, store = usage
    p, iface = start(tmp_path, monkeypatch, {"featureUse": url})
    yield p, iface, store
    if p.context is not None:
        p.unload()
    QgsSettings().remove("mikage")


def settle() -> None:
    for _ in range(3):
        QCoreApplication.processEvents()


def counted(keeper) -> dict:
    """What is counted in memory now: (dock, feature, place) → count (minutes left out)."""
    out: dict = {}
    for (_day, _roles, _version, dock, feature, place), (count, _minutes) in keeper.counter._counts.items():
        out[(dock, feature, place)] = out.get((dock, feature, place), 0) + count
    return out


def submenu_of(p, text):
    action = p.entry_action(text)
    return next(m for m in p._submenus if action in m.actions()), action


def test_menu_toolbar_keys_help_and_command_are_told_apart(plugin):
    p, iface, _store = plugin
    keeper = p.feature_use
    assert keeper is not None and keeper.settings.source is not None
    keeper.counter.take()
    # The menu: it closes as the entry is chosen, then the entry triggers (as Qt does).
    menu, action = submenu_of(p, "撮像依頼")
    menu.aboutToHide.emit()
    action.trigger()
    settle()
    assert counted(keeper).get(("", "撮像依頼", "MENU")) == 1
    # Its toolbar button (the same action, a dock's: counted when it opens it): the toolbar's own signal.
    dock_action = p.docks["SatObsRequests"].toggleViewAction()
    if dock_action.isChecked():
        dock_action.trigger()
        settle()
    keeper.counter.take()
    button = p.toolbar.widgetForAction(dock_action)
    assert button is not None
    button.click()
    settle()
    assert dock_action.isChecked() and counted(keeper).get(("", "撮像依頼", "TOOLBAR")) == 1
    # A dock closed from its entry: not counted.
    keeper.counter.take()
    dock_action = p.docks["SatObsRequests"].toggleViewAction()
    if not dock_action.isChecked():
        dock_action.trigger()
        settle()
    keeper.counter.take()
    dock_action.trigger()
    settle()
    assert not any(k[2] in fu.USE_PLACES for k in counted(keeper))
    # Unclaimed (QGIS's panel menu, a program): そのほか.
    keeper._menu_closed = -1e9
    dock_action.trigger()
    settle()
    assert counted(keeper).get(("", "撮像依頼", "OTHER")) == 1
    # A popup's choice: only when the popup closed just before (from the toolbar here: no plugin menu closed).
    layouts, popup = p.entry_action("背景地図"), p.entry_action("背景地図").menu()
    keeper.counter.take()
    child = next(a for a in popup.actions() if not a.isSeparator() and a.isEnabled())
    child.trigger()
    settle()
    assert not counted(keeper)
    popup.aboutToHide.emit()
    child.trigger()
    settle()
    on_toolbar = layouts in p.toolbar.actions()
    assert counted(keeper).get(("", "背景地図", "TOOLBAR" if on_toolbar else "OTHER")) == 1
    # A key, a help link, 文で操作.
    keeper.counter.take()
    p.shortcuts.actions["ShowCatalog"].trigger()
    p.run_menu_entry("撮像計画")
    p.run_menu_entry("ドックを初期位置に戻す")
    p.run_menu_entry("撮像依頼", via="COMMAND")
    settle()
    c = counted(keeper)
    assert c.get(("", "画像カタログ", "KEY")) == 1 and c.get(("", "撮像計画", "HELP")) == 1
    assert c.get(("", "ドックを初期位置に戻す", "HELP")) == 1 and c.get(("", "撮像依頼", "COMMAND")) == 1


def test_controls_inside_the_docks_and_how_long_they_are_shown(plugin):
    p, iface, _store = plugin
    keeper = p.feature_use
    dock = p.docks["SatObsImageCatalog"]
    dock.show()
    dock.raise_()
    settle()
    title = p.dock_entry_text("SatObsImageCatalog")
    keeper.walk("SatObsImageCatalog")
    assert any(d == title for d, _label in keeper.controls)
    # Not the dock's own close and float on its title bar (its showing is counted).
    for name in p.docks:
        keeper.walk(name)
    parts = [(p.dock_entry_text(name), b) for name, d in p.docks.items() for b in d.findChildren(QAbstractButton) if feature_use.qt_part(b)]
    assert any(b.metaObject().className() == "QDockWidgetTitleButton" for _t, b in parts)
    assert not any((t, feature_use.button_label(b)) in keeper.controls for t, b in parts)
    assert not any(label in ("Close", "Float", "Scroll Left", "Scroll Right") for _t, label in keeper.controls)
    # A button showing only a mark is named by its tooltip.
    mark = QPushButton("＋")
    mark.setToolTip("分類を追加（選んだ分類の下に）")
    assert feature_use.button_label(mark) == "分類を追加"
    keeper.counter.take()
    button = next(b for b in dock.findChildren(QPushButton) if b.isEnabled() and b.text().strip() and b.window() is dock.window()
                  and b.menu() is None)
    label = fu.label_of(button.text())
    # (Not clicked for real: a button may open a dialog. Its signal is what is listened to.)
    button.clicked.emit(False)
    combos = [c for c in dock.findChildren(QComboBox) if c.property("mikageFeatureUse")]
    named = [(c, label_) for c in combos for (d, label_) in keeper.controls if d == title and label_.startswith("選択: ")]
    assert named, "a list with a name"
    named[0][0].activated.emit(0)
    c = counted(keeper)
    assert c.get((title, label, "DOCK")) == 1
    assert sum(n for (d, f, place), n in c.items() if d == title and f.startswith("選択: ")) == 1
    # A dock's tabs.
    obs = p.docks["SatObsObservations"]
    obs.show()
    settle()
    keeper.walk("SatObsObservations")
    bars = [b for b in obs.findChildren(QTabBar) if b.property("mikageFeatureUse") and b.count() > 1]
    if bars:
        keeper.counter.take()
        bars[0].tabBarClicked.emit(1)
        tab = fu.label_of(bars[0].tabText(1))
        assert counted(keeper).get((p.dock_entry_text("SatObsObservations"), f"タブ: {tab}", "DOCK")) == 1
    # Shown: counted once when shown, its minutes as it goes.
    keeper.counter.take()
    dock.hide()
    settle()
    dock.show()
    settle()
    assert counted(keeper).get(("", title, "SHOWN")) == 1
    keeper._shown["SatObsImageCatalog"] -= 120
    keeper._mark_minutes()
    minutes = sum(m for (_d, _r, _v, d, f, place), (_n, m) in keeper.counter._counts.items() if f == title and place == "SHOWN")
    assert minutes >= 2


def test_sent_to_the_table_with_the_roles_and_shown_least_used_first(plugin, monkeypatch, tmp_path):
    p, iface, store = plugin
    keeper = p.feature_use
    p.role_keeper.choose(["OBSERVER"])
    settle()
    assert keeper.counter.roles == "OBSERVER"
    menu, action = submenu_of(p, "撮像依頼")
    p.docks["SatObsRequests"].hide()
    settle()
    for _ in range(3):  # opened, closed (not counted), opened
        menu.aboutToHide.emit()
        action.trigger()
        settle()
    assert keeper.tick(send=True)
    assert wait_for(lambda: keeper._task is None and keeper.last_sent is not None)
    assert keeper.last_error == ""
    rows = table_rows(store)
    assert rows and {r["USER_KEY"] for r in rows} == {keeper.user_key}
    mine = [r for r in rows if r["FEATURE_NAME"] == "撮像依頼" and r["PLACE_KIND"] == "MENU"]
    assert sum(r["USE_COUNT"] for r in mine) == 2 and {r["ROLE_NAMES"] for r in mine} == {"OBSERVER"}
    assert any(r["PLACE_KIND"] == fu.START for r in rows)
    # Nothing in it says who: no column holds the user name.
    name = p.role_keeper.user()
    assert not any(name and name in str(v) for r in rows for v in r.values())
    # 「機能の使われ方…」 (an administrator's): read from the table, the role's entries nobody used at 0.
    p.role_keeper.choose(["ADMIN"])
    settle()
    dialog = p.open_feature_use()
    assert wait_for(lambda: dialog._task is None and dialog.summary.people > 0)
    by = {(line.dock, line.feature): line for line in dialog.summary.lines}
    assert by[("", "撮像依頼")].count >= 2 and by[("", "撮像依頼")].users == 1
    assert ("", "雲量マップ") in by and by[("", "雲量マップ")].users == 0
    assert dialog.tree.topLevelItemCount() == len(dialog.lines())
    assert "使った人 1 人" in dialog.status.text()
    # Only the observers: the planner's entries a pure observer does not have are not listed at 0.
    dialog.role.setCurrentIndex(dialog.role.findData("OBSERVER"))
    dialog.load()
    assert wait_for(lambda: dialog._task is None)
    features = {line.feature for line in dialog.summary.lines}
    assert "撮像依頼" in features and "撮像計画ファイル出力" not in features
    dialog.kind.setCurrentIndex(dialog.kind.findData("docks"))
    dialog.render()
    assert all(line.dock for line in dialog.lines())
    path = tmp_path / "use.csv"
    from mikage.ui import feature_use_dialog

    monkeypatch.setattr(feature_use_dialog.QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    dialog.kind.setCurrentIndex(0)
    dialog.render()
    dialog.export()
    text = path.read_text(encoding="utf-8-sig")
    assert text.startswith("ドック,機能,回数") and "撮像依頼" in text


def test_without_a_table_this_pcs_counts_are_kept_and_shown(tmp_path, monkeypatch):
    p, iface = start(tmp_path, monkeypatch, {})
    try:
        keeper = p.feature_use
        assert keeper.settings.source is None and keeper.settings.enabled
        p.run_menu_entry("撮像計画")
        assert keeper.tick()
        assert wait_for(lambda: keeper._task is None)
        assert any(r[4] == "撮像計画" and r[5] == "HELP" for r in keeper.store.rows())
        p.run_menu_entry("撮像依頼")
        dialog = p.open_feature_use()
        assert wait_for(lambda: dialog._task is None and dialog.summary.lines)
        by = {line.feature: line for line in dialog.summary.lines}
        assert by["撮像計画"].count == 1 and by["撮像依頼"].count == 1 and dialog.summary.people == 1
        assert "この PC の記録だけ" in dialog.source_label.text()
        # Unloading writes what was counted since.
        p.run_menu_entry("撮像計画")
        folder = keeper.store
        p.unload()
        assert sum(r[6] for r in folder.rows() if r[4] == "撮像計画" and r[5] == "HELP") == 2
    finally:
        if p.context is not None:
            p.unload()
        QgsSettings().remove("mikage")


def test_off_in_the_settings_file_and_while_practising(tmp_path, monkeypatch):
    p, iface = start(tmp_path, monkeypatch, {"featureUse": False})
    try:
        keeper = p.feature_use
        assert not keeper.enabled
        p.run_menu_entry("撮像計画")
        assert len(keeper.counter) == 0 and not keeper.tick()
        keeper.settings = fu.DEFAULT_FEATURE_USE
        keeper.practise(True)  # (the minutes up to now counted, then nothing)
        keeper.counter.take()
        p.run_menu_entry("撮像計画")
        assert len(keeper.counter) == 0
        keeper.practise(False)
        p.run_menu_entry("撮像計画")
        assert list(counted(keeper)) == [("", "撮像計画", "HELP")]
        store = keeper.store
        keeper.settings = fu.FeatureUseSettings(enabled=False)
        p.unload()
        assert store.rows() == []
    finally:
        if p.context is not None:
            p.unload()
        QgsSettings().remove("mikage")


def test_the_entry_is_the_administrators():
    from mikage.core import roles

    assert roles.allows("機能の使われ方…", ["ADMIN"]) and not roles.allows("機能の使われ方…", [])
    assert not roles.allows("機能の使われ方…", ["OBSERVER"])
