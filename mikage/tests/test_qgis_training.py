"""
練習モード (features/training.py) in QGIS: started, the plugin is on the
practice server inside QGIS (its own settings file, its own local files, the
sample's catalog searched over real HTTP), the screens marked, what reaches
outside not usable, the audit log paused and the mail shown; ended, all of
it as before (担当 chosen while practising too), also after QGIS stopped
while practising.
"""
import json
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog  # noqa: E402

from mikage.core import audit  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.qgis_io import roles_io, settings, training_io  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.qgis_io.network import qgis_transport  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui import training_mode  # noqa: E402

PRODUCTION = "https://production.invalid/arcgis/rest/services"
#: conftest's files of each test, followed by the practice's while practising (as settings.py does).
FILES = {"offline_path": "offline", "place_cache_path": "place_cache", "opened_images_path": "opened_images"}


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """The practice's sample, made once (its images take a few seconds): copied into each test's folder."""
    folder = tmp_path_factory.mktemp("training_made")
    f = training_io.TrainingFiles(str(folder))
    training_io.prepare(f)
    return f


def _copy(made, target: str) -> None:
    import shutil

    shutil.copytree(made.root, target)


def _production_config(tmp_path, extra: dict = None) -> str:
    path = tmp_path / "config.production.json"
    value = {"imageCatalogs": [{"label": "本番カタログ", "url": f"{PRODUCTION}/Catalog/FeatureServer/0"}],
             "userRoles": f"{PRODUCTION}/Roles/FeatureServer/0"}
    value.update(extra or {})
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def plugin(tmp_path, monkeypatch, made, request):
    root = tmp_path / "training"
    _copy(made, str(root))
    monkeypatch.setattr(training_io, "folder", lambda: str(root))
    for name, attr in FILES.items():
        mine = getattr(settings, name)
        monkeypatch.setattr(settings, name, lambda mine=mine, attr=attr: (getattr(training_io.active().files, attr)
                                                                          if training_io.active() is not None else mine()))
    # The real network (the practice server answers over HTTP on 127.0.0.1).
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(qgis_transport(settings.auth_for)))
    monkeypatch.setattr(training_mode.StartDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    # (An earlier test's project changes would be asked about: 「保存しますか」 waits for an answer.)
    QgsProject.instance().removeAllMapLayers()
    QgsProject.instance().setDirty(False)
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "production.gpkg"))
    settings.set_config_source(_production_config(tmp_path, getattr(request, "param", None)))
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p, iface
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def _feature(p):
    from mikage.features.training import Training

    return p.feature(Training)


def _menu(iface) -> dict:
    tree = iface.menu_tree()
    return dict(e for e in tree if isinstance(e, tuple))


def test_practice_starts_on_its_own_server_and_files_and_ends_as_before(plugin, tmp_path):
    p, iface = plugin
    training = _feature(p)
    # The menu's first group.
    assert iface.menu_tree()[0] == ("練習モード", ["練習モードを始める", "練習データを最初に戻す…", "練習モードを終える"])
    assert settings.local_gpkg_path() == str(tmp_path / "production.gpkg")
    p.role_keeper.choose(["OBSERVER"])
    roles_before = QgsSettings().value(roles_io.KEY)
    assert p.start_training()
    session = training_io.active()
    assert session is not None and session.server is not None and session.server.running
    # The practice's settings file: every service on the server inside QGIS, nothing of the production.
    assert p.context.source == session.files.config
    text = open(session.files.config, encoding="utf-8").read()
    assert "production.invalid" not in text and "msearch.gsi.go.jp" not in text and session.base in text
    assert [c.label for c in p.context.config.image_catalogs] == ["練習用画像カタログ"]
    assert p.context.config.user_roles is None
    # Its own local files.
    assert settings.local_gpkg_path() == session.files.gpkg
    assert p.context.offline.path == session.files.offline
    # A new project with the practice's background map; the screens marked.
    layers = list(QgsProject.instance().mapLayers().values())
    assert [l.name() for l in layers] == ["練習用の背景地図"] and isinstance(layers[0], QgsRasterLayer) and layers[0].isValid()
    assert iface.canvas.findChild(training_mode.TrainingBand) is not None
    assert iface.window.statusBar().findChild(type(training.mark), training_mode.STATUS_NAME) is training.mark
    assert iface.window.windowTitle().startswith(training_mode.WINDOW_MARK)
    catalog = p.api.image_catalog
    assert catalog.windowTitle() == "画像カタログ" + training_mode.DOCK_MARK
    # What reaches outside the practice cannot be used meanwhile.
    menu = _menu(iface)
    blocked = [a for a in p.actions if a.text().endswith("（練習モードでは使えません）")]
    assert {a.text().split("（練習")[0] for a in blocked} >= {"設定…", "サーバ処理…", "ArcGIS Web マップを開く…"}
    assert all(not a.isEnabled() for a in blocked)
    assert not training._actions["練習モードを始める"].isEnabled() and training._actions["練習モードを終える"].isEnabled()
    assert "練習モードを終える" in menu["練習モード"]
    # Nothing is audited while practising (its start is, in the production's log).
    assert audit.current().paused
    # The sample over HTTP: the catalog's six images.
    catalog.show()
    catalog.area_box.setCurrentIndex(catalog.area_box.findData("none"))
    catalog.search()
    assert wait_for(lambda: catalog.search_button.isEnabled() and catalog.model.rowCount() == 6, 20000)
    # 担当 switched while practising.
    p.role_keeper.choose(["PLANNER"])
    assert p.role_keeper.codes == ["PLANNER"]

    assert p.end_training()
    assert training_io.active() is None and not session.server.running
    assert p.context.source == settings.production_config_source()[0] != session.files.config
    assert settings.local_gpkg_path() == str(tmp_path / "production.gpkg")
    assert QgsSettings().value(roles_io.KEY) == roles_before and p.role_keeper.codes == ["OBSERVER"]
    assert iface.canvas.findChild(training_mode.TrainingBand) is None or not iface.canvas.findChild(training_mode.TrainingBand).isVisible()
    assert catalog.windowTitle() == "画像カタログ"
    # The practice's images listed no more (their server is gone).
    assert catalog.model.rowCount() == 0
    assert not iface.window.windowTitle().startswith(training_mode.WINDOW_MARK)
    assert not [a for a in p.actions if a.text().endswith("（練習モードでは使えません）")]
    assert not audit.current().paused
    assert QgsSettings().value(training_io.ACTIVE_KEY, "") == ""
    assert training._actions["練習モードを始める"].isEnabled() and not training._actions["練習モードを終える"].isEnabled()


def test_unsaved_changes_are_asked_about_first(plugin, monkeypatch):
    from qgis.PyQt.QtWidgets import QMessageBox

    from mikage.features import training as feature

    p, iface = plugin
    asked = []
    QgsProject.instance().setDirty(True)
    monkeypatch.setattr(feature.QMessageBox, "question", lambda *a: asked.append(a[2]) or QMessageBox.StandardButton.Cancel)
    assert not p.start_training() and training_io.active() is None
    assert asked and "保存していない変更" in asked[0]
    monkeypatch.setattr(feature.QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Discard)
    assert p.start_training()
    try:
        assert training_io.active() is not None and not QgsProject.instance().isDirty()
    finally:
        p.end_training()


def test_qgis_stopped_while_practising_puts_back_what_was_kept_aside(tmp_path, monkeypatch, made):
    monkeypatch.setattr(training_io, "folder", lambda: str(tmp_path / "training"))
    QgsSettings().remove("mikage")
    s = QgsSettings()
    s.setValue(roles_io.KEY, json.dumps(["PLANNER"]))
    s.setValue(training_io.ACTIVE_KEY, "1")
    s.setValue(training_io.SAVED_KEY, json.dumps({roles_io.KEY: json.dumps(["OBSERVER"]), roles_io.PENDING_KEY: None}))
    s.setValue(roles_io.PENDING_KEY, "1")
    settings.set_config_source(_production_config(tmp_path))
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    try:
        assert p.role_keeper.codes == ["OBSERVER"]
        assert QgsSettings().value(roles_io.PENDING_KEY, None) is None
        assert QgsSettings().value(training_io.ACTIVE_KEY, "") == ""
        assert training_io.active() is None
    finally:
        p.unload()
        QgsSettings().remove("mikage")


@pytest.mark.parametrize("plugin", [{"training": False}], indirect=True)
def test_training_false_hides_it(plugin):
    p, iface = plugin
    training = _feature(p)
    assert not any(a.isVisible() for a in training._actions.values())
    assert "練習モード" not in _menu(iface) or not p.allows("練習モードを始める")
    assert not p.start_training()


def test_the_mail_is_shown_not_sent_while_practising(plugin, monkeypatch):
    from dataclasses import replace

    from mikage.core.obs.alerts import LogRow, MailSettings
    from mikage.ui import alert_feed

    p, iface = plugin
    opened, shown = [], []
    monkeypatch.setattr(alert_feed.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    monkeypatch.setattr(training_mode.PracticeMail, "exec", lambda self: shown.append(self.body.toPlainText()) or 0)
    assert p.start_training()
    try:
        feed = p.docks["SatObsNotices"].alerts
        config = p.context.config
        config.alert_reports = replace(config.alert_reports, mail=MailSettings(to=["a@x.jp"], subject="【通報】{count}件"))
        feed.rows = {1: LogRow("a", text="船A|停泊", oid=1)}
        feed.mail()
        assert not opened and shown == ["船A|停泊"]
        assert "練習モード" in feed.status.text()
    finally:
        p.end_training()


MASTERS = {
    "satelliteCatalogs": [{"label": "本番衛星", "url": f"{PRODUCTION}/Satellites/FeatureServer/0", "token": "PRODTOKEN",
                           "fields": {"name": "SAT_NAME", "tle1": "TLE_LINE1", "tle2": "TLE_LINE2"}}],
    "observationStores": [{"label": "本番観測記録", "url": f"{PRODUCTION}/Obs/FeatureServer/0", "token": "OBSTOKEN",
                           "taxonomyTable": f"{PRODUCTION}/Obs/FeatureServer/1"}],
    "glossary": f"{PRODUCTION}/Glossary/FeatureServer/0",
}


@pytest.mark.parametrize("plugin", [MASTERS], indirect=True)
def test_the_masters_only_read_are_the_productions(plugin):
    p, iface = plugin
    assert p.start_training()
    try:
        session = training_io.active()
        assert session.from_production == ("satellites", "taxonomy", "glossary")
        config = p.context.config
        # The production's satellites, classes and glossary, with their tokens (kept in memory) ...
        assert [c.label for c in config.satellite_catalogs] == ["本番衛星"] and config.satellite_catalogs[0].token == "PRODTOKEN"
        store = config.observation_stores[0]
        assert store.taxonomy_table.url == f"{PRODUCTION}/Obs/FeatureServer/1" and store.taxonomy_table.token == "OBSTOKEN"
        assert config.glossary.url == f"{PRODUCTION}/Glossary/FeatureServer/0"
        # ... what is written on the practice server ...
        assert store.url.startswith(session.base) and config.request_stores[0].url.startswith(session.base)
        assert [c.label for c in config.image_catalogs] == ["練習用画像カタログ"]
        # ... and the file written without the tokens.
        text = open(session.files.config, encoding="utf-8").read()
        assert f"{PRODUCTION}/Satellites" in text and "PRODTOKEN" not in text and "OBSTOKEN" not in text
        # The production's classes are not edited while practising.
        blocked = [a for a in p.actions if a.text().endswith("（練習モードでは本番の分類体系を読むだけ）")]
        assert [a.text().split("（")[0] for a in blocked] == ["分類体系の編集"] and not blocked[0].isEnabled()
    finally:
        p.end_training()
    assert not [a for a in p.actions if a.text().endswith("（練習モードでは本番の分類体系を読むだけ）")]


@pytest.mark.parametrize("plugin", [dict(MASTERS, training={"fromProduction": False})], indirect=True)
def test_from_production_false_practises_on_the_sample_alone(plugin):
    p, iface = plugin
    assert p.start_training()
    try:
        assert training_io.active().from_production == () and training_io.active().config_text is None
        config = p.context.config
        assert [c.label for c in config.satellite_catalogs] == ["練習用衛星"]
        assert config.observation_stores[0].taxonomy_table.url.startswith(training_io.active().base)
    finally:
        p.end_training()
