"""
記録者の既定値 (tsudo 2026-11-25: 観測記録の記録者は、デフォルト値を USERNAME の値に):
the name typed comes first, else the user name the edits are stamped with
(editTracking: USERNAME by default), else the store's sign-in. The boxes
show the name the records get; the user name itself is kept as none typed.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402

from mikage.core import edit_stamp  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.features.rendering import Rendering  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402
from mikage.ui.settings_dialog import SettingsDialog  # noqa: E402

URL = "https://example.com/arcgis/rest/services/Obs/FeatureServer/0"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    QgsSettings().remove("mikage")
    monkeypatch.setenv("USERNAME", "yamada")
    edit_stamp.set_tracking(None)
    yield
    edit_stamp.set_tracking(None)
    QgsSettings().remove("mikage")


def _sign_in(name: str) -> str:
    auth = QgsApplication.authManager()
    if not auth.masterPasswordIsSet():
        auth.setMasterPassword("test", True)
    config = QgsAuthMethodConfig("Basic")
    config.setName("test")
    config.setConfig("username", name)
    config.setConfig("password", "pw")
    assert auth.storeAuthenticationConfig(config)[0]
    settings.set_auth_map({URL: config.id()})
    return config.id()


def test_the_name_typed_else_the_user_name_else_the_sign_in(monkeypatch):
    _sign_in("shared")
    # None typed: the user name (USERNAME), not the sign-in (a shared account on some networks).
    assert settings.recorder_setting() == ""
    assert settings.recorder_shown() == "yamada" and settings.recorder_name(URL) == "yamada"
    # editTracking names the user too.
    edit_stamp.set_tracking(edit_stamp.EditTracking(user_variable="OBS_USER"))
    monkeypatch.setenv("OBS_USER", "suzuki")
    assert settings.recorder_name(URL) == "suzuki"
    edit_stamp.set_tracking(edit_stamp.EditTracking(user="当直"))
    assert settings.recorder_name(URL) == "当直"
    edit_stamp.set_tracking(None)
    # A name typed comes first.
    settings.set_recorder(" 佐藤 ")
    assert settings.recorder_setting() == "佐藤" and settings.recorder_name(URL) == "佐藤"
    # The user name itself, or nothing, is kept as none typed: it follows the user name.
    settings.set_recorder("yamada")
    assert settings.recorder_setting() == ""
    settings.set_recorder("")
    assert settings.recorder_setting() == ""
    # No user name known at all: the sign-in, as before.
    monkeypatch.setattr(edit_stamp, "user_name", lambda *_a, **_k: "")
    assert settings.recorder_name(URL) == "shared"


@pytest.fixture
def dock(tmp_path, request):
    QgsProject.instance().removeAllMapLayers()
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    iface = FakeIface()
    # 「設定…」 has 描画の速さ (rendering) too.
    p = small_plugin(iface, Observations, Rendering, request=request)
    p.initGui()
    layer = QgsRasterLayer(geotiff(tmp_path / "d1.tif", "2026-10-01T01:00:00Z"), "d1")
    assert layer.isValid()
    QgsProject.instance().addMapLayers([layer])
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    iface.canvas.setExtent(layer.extent())
    d = p.api.observation_dock
    d.show()
    yield p, d, tmp_path
    p.unload()
    QgsProject.instance().removeAllMapLayers()


def _record(d, name: str):
    d.set_active(True)
    d.map_clicked(139.7, 35.6, None)
    d.object_input.setText(name)
    d.kind_input.setText("船舶")
    d.submit()
    return [o for o in d.book.living() if o.object_name == name][0]


def test_the_dock_shows_the_user_name_and_records_with_it(dock):
    p, d, tmp_path = dock
    # The box shows the name the records get.
    assert d.observer_input.text() == "yamada"
    assert _record(d, "あおば").recorded_by == "yamada"
    # Another name typed: kept, and the records get it.
    d.observer_input.setText("佐藤")
    d.observer_input.editingFinished.emit()
    assert settings.recorder_setting() == "佐藤"
    assert _record(d, "しらね").recorded_by == "佐藤"
    # Left empty: the user name again.
    d.observer_input.setText("")
    d.observer_input.editingFinished.emit()
    assert d.observer_input.text() == "yamada" and settings.recorder_setting() == ""
    assert _record(d, "こんごう").recorded_by == "yamada"
    # Oneself (「自分の記録のみ」, 「自分の記録」): the user name.
    assert d.my_names() == ["yamada"]


def test_another_settings_file_with_another_user_shows_its_name(dock):
    p, d, tmp_path = dock
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"editTracking": {"user": "当直"}}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.reload()
    assert d.observer_input.text() == "当直" and d.observer() == "当直"
    # A name typed stays whatever the settings file says.
    d.set_observer("佐藤")
    config.write_text(json.dumps({"editTracking": {"user": "日直"}}), encoding="utf-8")
    p.context.reload()
    assert d.observer_input.text() == "佐藤" and d.observer() == "佐藤"


def test_the_settings_dialog_shows_the_user_name(dock):
    p, d, tmp_path = dock
    dialog = SettingsDialog(p.context)
    assert dialog.recorder.text() == "yamada"
    dialog.recorder.setText("佐藤")
    dialog.accept()
    assert settings.recorder_setting() == "佐藤"
    dialog.deleteLater()
    dialog = SettingsDialog(p.context)
    assert dialog.recorder.text() == "佐藤"
    dialog.recorder.setText("yamada")
    dialog.accept()
    dialog.deleteLater()
    assert settings.recorder_setting() == ""


def test_settings_from_the_menu_reach_an_open_dock(dock, monkeypatch):
    p, d, tmp_path = dock
    from mikage.ui import settings_dialog

    def accepted(self):
        self.recorder.setText("佐藤")
        self.accept()
        return 1

    monkeypatch.setattr(settings_dialog.SettingsDialog, "exec", accepted)
    p.open_settings()
    # The box and the name the records get, at once.
    assert d.observer_input.text() == "佐藤" and d.observer() == "佐藤"
