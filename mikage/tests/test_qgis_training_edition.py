"""
MIKAGE 練習版 (core/edition.py, scripts/build_zip.py --training-only) in QGIS:
the same plugin with the edition switched on. It practises as soon as QGIS
has started, cannot end it, keeps the project open, keeps no audit log,
keeps its personal settings apart from MIKAGE's, reads of the shipped
settings file only what 練習モード takes of a production, and does nothing
when MIKAGE runs in the same QGIS (and MIKAGE nothing when it does).
"""
import json
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402

from mikage.core import edition  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.qgis_io import qsettings, roles_io, settings, training_io  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.qgis_io.network import qgis_transport  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402
from mikage.tests.test_qgis_training import CATALOG_FIELDS, FILES, PRODUCTION, _copy, made  # noqa: E402,F401
from mikage.ui import training_mode  # noqa: E402

#: The shipped settings file's catalogs: one only for practice (shown while practising), one of the production.
SHIPPED = {
    "imageCatalogs": [
        {"label": "研修用カタログ", "url": f"{PRODUCTION}/Practice/FeatureServer/0", "fields": CATALOG_FIELDS, "mode": "training"},
        {"label": "本番カタログ", "url": f"{PRODUCTION}/Catalog/FeatureServer/0", "fields": CATALOG_FIELDS},
    ],
    "userRoles": f"{PRODUCTION}/Roles/FeatureServer/0",
    "auditLog": {"folder": "//production.invalid/audit"},
}


def _shipped(tmp_path) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(SHIPPED, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def training_edition(monkeypatch):
    """MIKAGE 練習版: the edition switched on, its settings' root emptied before and after."""
    monkeypatch.setattr(edition, "TRAINING_ONLY", True)
    QgsSettings().remove(edition.TRAINING_ID)
    yield
    QgsSettings().remove(edition.TRAINING_ID)


@pytest.fixture
def plugin(training_edition, tmp_path, monkeypatch, made):
    root = tmp_path / "training"
    _copy(made, str(root))
    monkeypatch.setattr(training_io, "folder", lambda: str(root))
    for name, attr in FILES.items():
        mine = getattr(settings, name)
        monkeypatch.setattr(settings, name, lambda mine=mine, attr=attr: (getattr(training_io.active().files, attr)
                                                                          if training_io.active() is not None else mine()))
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(qgis_transport(settings.auth_for)))
    # The settings file shipped in the zip; one chosen in the settings dialog is not read by MIKAGE 練習版.
    shipped = _shipped(tmp_path)
    monkeypatch.setattr(settings, "bundled_config", lambda: shipped)
    chosen = tmp_path / "chosen.json"
    chosen.write_text(json.dumps({"imageCatalogs": [{"label": "選んだカタログ", "url": f"{PRODUCTION}/Chosen/FeatureServer/0"}]}),
                      encoding="utf-8")
    settings.set_config_source(str(chosen))
    # Every settings file the screens are given.
    read: list = []
    reload = PluginContext.reload

    def recorded(self):
        read.append(settings.effective_config_source()[0])
        return reload(self)

    monkeypatch.setattr(PluginContext, "reload", recorded)
    QgsProject.instance().removeAllMapLayers()
    QgsProject.instance().setDirty(False)
    # A layer of the project open as QGIS started (MIKAGE 練習版 does not take it away).
    kept = QgsVectorLayer("Point?crs=EPSG:4326", "自分のレイヤー", "memory")
    QgsProject.instance().addMapLayer(kept)
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p, iface, read, shipped
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsProject.instance().setDirty(False)


def _training(p):
    from mikage.features.training import Training

    return p.feature(Training)


def test_it_practises_from_the_start_and_cannot_end(plugin):
    p, iface, read, shipped = plugin
    training = _training(p)
    session = training_io.active()
    assert session is not None and session.server is not None and session.server.running
    assert p.context.source == session.files.config
    # The screens were never given a production's settings file: nothing before practice, then the practice's.
    assert set(read) <= {"", session.files.config} and read[-1] == session.files.config
    # Of the shipped file, the catalog shown while practising; not the production's, not the one chosen.
    labels = [c.label for c in p.context.config.image_catalogs]
    assert labels == ["練習用画像カタログ", "研修用カタログ"]
    assert p.context.config.user_roles is None
    assert session.production is not None and session.production.source == shipped
    # Not ended: no entry, no button, and asked anyway, refused.
    for text in ("練習モードを始める", "練習モードを終える", "判読の練習の問題を作る…", "判読の練習の成績…", "監査ログ…"):
        assert not p.allows(text), text
    assert not training._actions["練習モードを終える"].isVisible() and not training._actions["練習モードを始める"].isVisible()
    assert training._actions["チュートリアル…"].isVisible() and training._actions["判読の練習…"].isVisible()
    band = iface.canvas.findChild(training_mode.TrainingBand)
    assert band is not None and "練習モードを終える" not in band.buttons and "チュートリアル" in band.buttons
    assert not p.end_training() and training_io.active() is session
    # No audit log at all (nothing of the production done).
    assert p.audit is None
    # The project open is kept, the practice's background map added to it.
    names = sorted(layer.name() for layer in QgsProject.instance().mapLayers().values())
    assert names == ["練習用の背景地図", "自分のレイヤー"]
    # Its own menu and toolbar names.
    assert {menu for menu, _text in iface.menus} == {"MIKAGE 練習版"}
    assert p.toolbar.windowTitle() == "MIKAGE 練習版"


def test_the_help_opens_on_what_differs_in_it(plugin, monkeypatch):
    from mikage.ui.help_dialog import HelpDialog

    p, _iface, _read, _shipped_path = plugin
    went = []
    monkeypatch.setattr(HelpDialog, "go", lambda self, section: went.append(section))
    p.open_help()
    p.open_help()  # (opened again: where the person left it)
    p.open_help("training-tutorial")
    assert went == ["training-edition", "training-tutorial"]
    p.help_dialog.close()


def test_its_personal_settings_are_its_own(plugin, tmp_path):
    p, _iface, _read, _shipped_path = plugin
    QgsSettings().remove(f"{edition.FULL_ID}/roles")
    p.role_keeper.choose(["PLANNER"])
    assert qsettings.key(roles_io.KEY) == "mikage_training/roles"
    assert json.loads(QgsSettings().value("mikage_training/roles")) == ["PLANNER"]
    assert QgsSettings().value("mikage/roles", None) is None
    # Not kept aside to be put back (it never goes back to a production): the roles stay for the next start.
    assert qsettings.value(training_io.ACTIVE_KEY, "") == ""
    assert qsettings.profile_folder("training").endswith(os.path.join("mikage_training", "training"))


def test_the_first_setup_offers_no_settings_file_nor_practice(plugin, monkeypatch):
    from qgis.PyQt.QtWidgets import QPushButton

    p, _iface, _read, _shipped_path = plugin
    dialog = p.open_first_setup(first=True)
    texts = [b.text() for b in dialog.findChildren(QPushButton)]
    assert not any("設定ファイル" in t for t in texts) and not any("まず練習" in t for t in texts)
    dialog.reject()


def test_nothing_before_practice_is_the_production(training_edition, tmp_path, monkeypatch):
    """Before 練習モード is on (the sample being made), the screens' settings file is none: never the production's."""
    shipped = _shipped(tmp_path)
    monkeypatch.setattr(settings, "bundled_config", lambda: shipped)
    settings.set_config_source(str(tmp_path / "chosen.json"))
    assert settings.production_config_source() == (shipped, True)
    assert settings.effective_config_source() == ("", False)
    monkeypatch.setattr(settings, "bundled_config", lambda: "")
    assert settings.production_config_source() == ("", False)


@pytest.mark.parametrize("this, other", [(True, "mikage"), (False, "mikage_training")])
def test_one_runs_when_both_are_on(this, other, monkeypatch):
    """MIKAGE and MIKAGE 練習版 both turned on: the one started second does nothing, and says so."""
    from qgis import utils

    monkeypatch.setattr(edition, "TRAINING_ONLY", this)
    monkeypatch.setattr(utils, "active_plugins", [other])
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    try:
        assert p.stood_aside and not iface.docks and not iface.menus and p.toolbar is None and p.provider is None
        assert training_io.active() is None
        shown = [item.text() for item in iface.bar.items()]
        assert any("どちらか一方" in text and edition.other_name() in text for text in shown), shown
    finally:
        p.unload()
