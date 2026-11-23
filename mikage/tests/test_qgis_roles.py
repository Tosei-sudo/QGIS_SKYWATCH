"""
担当と使える機能 in QGIS (tsudo, 2026-11-15): the roles each person ticks
(as many as they like) decide the menu entries, toolbar buttons, docks, the
docks' buttons, keys and Processing tools shown; kept in the settings file's
Esri table `userRoles` under the person's user name.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsApplication, QgsProject, QgsSettings  # noqa: E402

from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.qgis_io import roles_io, settings  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui.settings_dialog import SettingsDialog  # noqa: E402

ROLES = "https://example.com/arcgis/rest/services/MIKAGE/FeatureServer/30"
S, T = "esriFieldTypeString", "esriFieldTypeDate"
FIELDS = {"USER_NAME": S, "ROLE_NAME": S, "CREATED_USER": S, "CREATED_DATE": T}
ADMIN_ENTRIES = ("接続と表の点検…", "監査ログ…", "Python API リファレンス…", "サーバ処理…")


@pytest.fixture(autouse=True)
def clean():
    QgsSettings().remove("mikage")
    settings.set_config_source("")
    yield
    QgsSettings().remove("mikage")
    settings.set_config_source("")


def start(iface=None):
    from mikage import classFactory

    p = classFactory(iface or FakeIface())
    p.initGui()
    return p


@pytest.fixture
def plugin():
    p = start()
    yield p
    p.unload()


@pytest.fixture
def with_table(tmp_path, monkeypatch):
    """A settings file with `userRoles`, its table in a fake Esri, the person 「tsudo」."""
    monkeypatch.setenv("USERNAME", "tsudo")
    fake = FakeEsri()
    table = fake.add_layer(ROLES, FakeLayer(dict(FIELDS), ""))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"userRoles": ROLES}), encoding="utf-8")
    settings.set_config_source(str(path))
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    QgsProject.instance().removeAllMapLayers()
    return fake, table


def visible(p, text):
    return p._entries_in_menu_order()[text].isVisible()


def choose(p, *codes):
    p.role_keeper.choose(list(codes))
    APP.processEvents()


def algorithms(p):
    return {a.name() for a in QgsApplication.processingRegistry().providerById(p.provider.id()).algorithms()}


def test_without_a_role_everything_but_the_administrators(plugin):
    p = plugin
    assert p.role_keeper.codes == []
    assert [t for t in ADMIN_ENTRIES if visible(p, t)] == []
    assert visible(p, "撮像計画ファイル出力") and visible(p, "観測記録") and visible(p, "画像カタログ")
    assert visible(p, "担当と個人設定…") and visible(p, "設定…") and visible(p, "使い方（ヘルプ）…")
    # The administrators' Processing tools are not there either (サーバ処理 has none here); the others are.
    assert {"train_detector", "make_dataset"} <= algorithms(p)
    choose(p, "ADMIN")
    assert all(visible(p, t) for t in ADMIN_ENTRIES)
    # 管理者 alone: none of the others' features (tick them too).
    assert not visible(p, "観測記録") and not visible(p, "撮像計画ファイル出力")


def test_the_person_who_asks_sees_no_plan_files_nor_records(plugin):
    p = plugin
    observations, requests, schedule = p.docks["SatObsObservations"], p.docks["SatObsRequests"], p.docks["SatObsSchedule"]
    observations.show()
    requests.show()
    schedule.show()
    choose(p, "REQUESTER")
    # Menu entries and a submenu with nothing left in it.
    assert visible(p, "撮像依頼") and visible(p, "撮像予定") and not visible(p, "撮像計画ファイル出力") and not visible(p, "年間撮像上限…")
    assert not visible(p, "観測記録") and not visible(p, "通報文の作成…")
    menus = {m.title(): m for m in p._submenus}
    assert menus["撮像依頼"].menuAction().isVisible() and menus["観測記録"].menuAction().isVisible()  # 移動体・ダッシュボード
    # The records' dock put away, and kept away (QGIS's own Panels menu, a layout).
    assert not observations.isVisible()
    observations.show()
    APP.processEvents()
    assert wait_for(lambda: not observations.isVisible())
    assert "SatObsObservations" not in p.allowed_docks() and "SatObsRequests" in p.allowed_docks()
    # The keys of the records do nothing.
    assert not p.shortcuts.actions["ShowObservations"].isEnabled() and p.shortcuts.actions["ShowCatalog"].isEnabled()
    # In the docks: asking, yes; answering (採否), no.
    assert requests.new_button.isVisibleTo(requests) and requests.edit_button.isVisibleTo(requests)
    assert not schedule.adopt_button.isVisibleTo(schedule) and not schedule.import_button.isVisibleTo(schedule)
    assert schedule.denied("schedule.answer") and "撮像計画の担当" in schedule.status.text()
    # The planner too: the answers and the files come back.
    choose(p, "REQUESTER", "PLANNER")
    assert visible(p, "撮像計画ファイル出力") and schedule.adopt_button.isVisibleTo(schedule)
    assert not schedule.denied("schedule.answer")


def test_the_observer_does_not_ask_and_the_trainer_does_not_record(plugin):
    p = plugin
    requests, ai = p.docks["SatObsRequests"], p.docks["SatObsAiResults"]
    choose(p, "OBSERVER")
    assert not requests.new_button.isVisibleTo(requests) and not requests.delete_button.isVisibleTo(requests)
    assert ai.import_button.isVisibleTo(ai) and not ai.train_button.isVisibleTo(ai)
    assert not visible(p, "AI 学習（物体検出）…") and "train_detector" not in algorithms(p)
    choose(p, "TRAINER")
    assert visible(p, "AI結果") and not ai.import_button.isVisibleTo(ai) and ai.train_button.isVisibleTo(ai)
    assert visible(p, "AI 学習（物体検出）…") and "train_detector" in algorithms(p)
    assert not visible(p, "観測記録")


def test_the_settings_file_is_changed_by_the_administrators_only(plugin, tmp_path):
    p = plugin
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    # Choosing the first one: anyone.
    dialog = SettingsDialog(p.context, p.iface.mainWindow(), may_change_source=False)
    assert not dialog.source.isReadOnly()
    dialog.deleteLater()
    settings.set_config_source(str(path))
    dialog = SettingsDialog(p.context, p.iface.mainWindow(), may_change_source=False)
    assert dialog.source.isReadOnly() and "管理者" in dialog.source.toolTip()
    dialog.deleteLater()
    dialog = SettingsDialog(p.context, p.iface.mainWindow(), may_change_source=True)
    assert not dialog.source.isReadOnly()
    dialog.deleteLater()


def test_a_menu_entry_run_another_way_says_whose_it_is(plugin, monkeypatch):
    p = plugin
    told = []
    monkeypatch.setattr(p.iface.messageBar(), "pushInfo", lambda title, text: told.append((title, text)))
    choose(p, "OBSERVER")
    assert not p.run_menu_entry("撮像計画ファイル出力")
    assert told == [("撮像計画ファイル出力", "担当「撮像計画の担当」の機能（メニュー「担当と個人設定…」で担当を選ぶと使える）")]


def test_the_roles_are_kept_in_the_table_under_the_user_name(with_table):
    fake, table = with_table
    table.add({"USER_NAME": "TSUDO", "ROLE_NAME": "OBSERVER"})
    table.add({"USER_NAME": "sato", "ROLE_NAME": "ADMIN"})
    p = start()
    try:
        assert p.api.problems == [], p.api.problems
        # Read from the table (a read ahead after QGIS has started; here at once).
        assert p.role_keeper.load()
        assert wait_for(lambda: p.role_keeper.names == ["観測記録の担当"])
        assert not visible(p, "撮像計画ファイル出力") and roles_io.stored_codes() == ["OBSERVER"]
        # Ticked on the page: in use at once, then into the table (another person's rows untouched).
        page = p.open_first_setup()
        assert page.chosen_roles() == ["観測記録の担当"] and "「tsudo」として保存" in page.where.text()
        page.role_checks["撮像計画の担当"].setChecked(True)
        page.relayout.setChecked(False)
        page.accept()
        assert visible(p, "撮像計画ファイル出力")
        assert wait_for(lambda: not p.role_keeper.busy and not roles_io.pending())
        rows = sorted((r["attributes"]["USER_NAME"].upper(), r["attributes"]["ROLE_NAME"]) for r in table.rows)
        assert rows == [("SATO", "ADMIN"), ("TSUDO", "OBSERVER"), ("TSUDO", "PLANNER")]
        # An administrator changes the person's rows: the next read uses them.
        table.rows = [r for r in table.rows if r["attributes"]["ROLE_NAME"] != "PLANNER"]
        table.add({"USER_NAME": "tsudo", "ROLE_NAME": "ADMIN"})
        assert p.role_keeper.load()
        assert wait_for(lambda: p.role_keeper.codes == ["OBSERVER", "ADMIN"])
        assert visible(p, "監査ログ…") and not visible(p, "撮像計画ファイル出力")
    finally:
        p.unload()


def test_a_choice_the_table_did_not_take_is_sent_on_the_next_start(with_table, monkeypatch):
    fake, table = with_table
    gone = fake.layers.pop(ROLES)
    p = start()
    said = []
    p.role_keeper.said.connect(said.append)
    try:
        choose(p, "PLANNER")
        assert wait_for(lambda: said)
        assert "担当の保存に失敗" in said[0] and roles_io.pending()
        # Still in use here.
        assert p.role_keeper.codes == ["PLANNER"] and visible(p, "撮像計画ファイル出力")
        assert "未送信" in p.open_first_setup().where.text()
        p.setup_dialog.reject()
    finally:
        p.unload()
    # Next start, the server back: what is waiting is sent instead of reading.
    fake.layers[ROLES] = gone
    p = start()
    try:
        assert p.role_keeper.codes == ["PLANNER"]
        assert p.role_keeper.load()
        assert wait_for(lambda: not roles_io.pending() and not p.role_keeper.busy)
        assert [(r["attributes"]["USER_NAME"], r["attributes"]["ROLE_NAME"]) for r in table.rows] == [("tsudo", "PLANNER")]
    finally:
        p.unload()


def test_without_the_table_the_choice_stays_on_this_pc(plugin):
    p = plugin
    assert not p.role_keeper.load()  # nothing to read
    choose(p, "INTERPRETER")
    # Marked to be sent: into the table once the settings file has one.
    assert roles_io.stored_codes() == ["INTERPRETER"] and roles_io.pending()
    assert p.role_keeper.where_kept().startswith("この PC だけに保存")
