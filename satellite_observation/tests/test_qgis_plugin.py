"""
The QGIS side, with qgis.testing: the plugin registers its docks and menu,
the settings file is read, and requests go through QGIS's network stack
(off the main thread) with an authcfg.
"""
import http.server
import json
import os
import threading

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.network import qgis_transport  # noqa: E402
from satellite_observation.qgis_io.tasks import run_task  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

HERE = os.path.dirname(__file__)


@pytest.fixture(autouse=True)
def clean_settings():
    QgsSettings().remove("satellite_observation")
    yield
    QgsSettings().remove("satellite_observation")


def test_the_plugin_registers_its_docks_menu_and_reads_the_settings_file():
    from satellite_observation import classFactory

    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    assert [d.windowTitle() for d in iface.docks] == ["撮像計画", "保存した撮像計画", "衛星パス", "観測記録", "分類体系の編集"]
    assert [t for _, t in iface.menus] == ["撮像計画", "保存した撮像計画", "衛星パス", "観測記録", "分類体系の編集", "設定…"]
    assert plugin.api.problems == []
    assert plugin.api.config.satellite_catalogs[0].label == "衛星カタログ"
    roles = [s.role for s in plugin.context.services()]
    assert roles.count("確認範囲") == 1 and "計画の保存先" in roles
    plugin.docks["SatObsObservations"].show()
    assert not plugin.docks["SatObsObservations"].isHidden()
    from satellite_observation.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(plugin.context, iface.window)
    assert dialog.services.rowCount() == len(plugin.context.services())
    assert dialog.problems.count() == 0
    plugin.unload()
    assert iface.docks == [] and iface.menus == []


def test_a_broken_settings_file_says_why(tmp_path):
    from satellite_observation.qgis_io.context import PluginContext

    path = tmp_path / "config.json"
    path.write_text('{"satelliteCatalogs": "x", "planStores": [{"url": "https://x/MapServer/0"}]}', encoding="utf-8")
    settings.set_config_source(str(path))
    context = PluginContext()
    context.reload()
    assert context.problems == ["satelliteCatalogs は配列で指定", "planStores[0] の url は編集できる …/FeatureServer/0 のようなレイヤーの URL で指定"]


class Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
        Handler.seen.append((self.path, body, self.headers.get("Authorization")))
        answer = json.dumps({"objectIdField": "OBJECTID", "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]}).encode()
        if self.path.endswith("/1"):
            answer = b"<html>"
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    Handler.seen = []
    yield f"http://127.0.0.1:{httpd.server_address[1]}/arcgis/rest/services/S/FeatureServer"
    httpd.shutdown()


def test_requests_go_through_qgis_in_a_task_with_the_services_authcfg(server, tmp_path):
    auth = QgsApplication.authManager()
    if not auth.masterPasswordIsSet():
        auth.setMasterPassword("test", True)
    config = QgsAuthMethodConfig("Basic")
    config.setName("test")
    config.setConfig("username", "tanaka")
    config.setConfig("password", "pw")
    assert auth.storeAuthenticationConfig(config)[0]
    settings.set_auth_map({f"{server}/0": config.id()})
    assert settings.recorder_name(f"{server}/0") == "tanaka"
    settings.set_recorder("sato")
    assert settings.recorder_name(f"{server}/0") == "sato"

    client = EsriClient(qgis_transport(settings.auth_for))
    with pytest.raises(RuntimeError, match="QgsTask"):
        client.ask(f"{server}/0")
    results = {}
    run_task("test", lambda task: (client.ask(f"{server}/0", {"where": "1=1"}, token=None), client.ask(f"{server}/1")),
             done=lambda r: results.setdefault("ok", r), failed=lambda m: results.setdefault("error", m))
    assert wait_for(lambda: results)
    assert results == {"error": "応答が JSON 以外"}
    path, body, authorization = Handler.seen[0]
    assert path.endswith("/FeatureServer/0") and "where=1%3D1" in body and "f=json" in body
    assert authorization and authorization.startswith("Basic ")
    # The second service has no authcfg.
    assert Handler.seen[1][2] is None
