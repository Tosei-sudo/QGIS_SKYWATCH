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
    # Its `layers` are examples on example.com: not opened here (test_qgis_startup_layers covers them).
    settings.set_startup_layers_on(False)
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    assert [d.windowTitle() for d in iface.docks] == ["画像カタログ", "撮像計画", "保存した撮像計画", "衛星パス", "観測記録", "分類体系の編集", "通知", "画像補正", "影から高さ", "スペクトル", "移動体（AIS・ADS-B）"]
    assert [t for _, t in iface.menus] == ["画像カタログ", "撮像計画", "保存した撮像計画", "衛星パス", "観測記録", "分類体系の編集", "通知", "画像補正", "影から高さ", "スペクトル", "移動体（AIS・ADS-B）", "移動目標撮像計画…", "新規スクラッチレイヤー（テンプレート）…", "パンシャープン…", "変化抽出（2 時期）…", "自動位置合わせ…", "土地被覆分類…", "簡易オルソ補正…", "測地線バッファ…", "正距方位での重心…", "移動体と検出の照合…", "設定ファイルのレイヤーを追加", "背景地図", "ArcGIS Web マップを開く…", "ArcGIS Web マップとして保存…", "スワイプ比較", "位置ずらし", "位置ずらしの解除", "ドックを初期位置に戻す", "設定…"]
    # Only the image catalog and the imaging plan are open at first (QGIS shows every dock it adds).
    assert [d.windowTitle() for d in iface.docks if d.isVisible()] == ["画像カタログ", "撮像計画"]
    # They work as if opened: the catalog's footprints drawn on the map.
    assert plugin.docks["SatObsImageCatalog"].overlay is not None and plugin.docks["SatObsNotices"].isHidden()
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
    assert not dialog.startup_layers.isChecked()
    # 「パンシャープン」 in the Processing toolbox while the plugin is loaded.
    assert QgsApplication.processingRegistry().algorithmById("satobs:pansharpen") is not None
    plugin.unload()
    assert iface.docks == [] and iface.menus == []
    assert QgsApplication.processingRegistry().algorithmById("satobs:pansharpen") is None


def test_the_docks_start_in_three_groups_of_tabs_and_come_back_on_screen():
    from qgis.PyQt.QtCore import QPoint, Qt

    from satellite_observation import classFactory

    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    d, w = plugin.docks, iface.window
    right, left = Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.LeftDockWidgetArea

    # Hidden docks keep their place: shown, each group is one set of tabs.
    for dock in d.values():
        dock.show()

    def tabs(name):
        return sorted(x.objectName() for x in [d[name], *w.tabifiedDockWidgets(d[name])])

    assert tabs("SatObsImagingPlan") == ["SatObsImagingPlan", "SatObsSatellitePass", "SatObsSavedPlans"]
    assert tabs("SatObsImageCatalog") == ["SatObsEnhance", "SatObsImageCatalog", "SatObsNotices", "SatObsShadowHeight", "SatObsSpectral"]
    assert tabs("SatObsObservations") == ["SatObsMoving", "SatObsObservations", "SatObsTaxonomy"]
    assert [w.dockWidgetArea(d[n]) for n in ("SatObsImagingPlan", "SatObsImageCatalog", "SatObsObservations")] == [right, left, right]
    # A floating dock off every screen is docked again when shown.
    d["SatObsSatellitePass"].setFloating(True)
    d["SatObsSatellitePass"].move(QPoint(-20000, -20000))
    d["SatObsSatellitePass"].hide()
    d["SatObsSatellitePass"].show()
    assert wait_for(lambda: not d["SatObsSatellitePass"].isFloating())
    # Moved away: back where it started, still open.
    w.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, d["SatObsNotices"])
    d["SatObsObservations"].setFloating(True)
    [home] = [a for a in plugin.actions if a.text() == "ドックを初期位置に戻す"]
    home.trigger()
    assert w.dockWidgetArea(d["SatObsNotices"]) == left and tabs("SatObsImageCatalog") == ["SatObsEnhance", "SatObsImageCatalog", "SatObsNotices", "SatObsShadowHeight", "SatObsSpectral"]
    assert not d["SatObsObservations"].isFloating() and d["SatObsNotices"].isVisible()
    plugin.unload()


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


def test_a_settings_file_shipped_in_the_plugin_folder(tmp_path, monkeypatch):
    """The viewer's config.json shipped in the plugin's folder is read when none is chosen; a chosen one wins."""
    import shutil

    from satellite_observation.qgis_io.context import PluginContext
    from satellite_observation.ui.docks import ConfigBar

    shutil.copy(os.path.join(HERE, "data", "viewer-config.jsonc"), tmp_path / "config.jsonc")
    monkeypatch.setattr(settings, "PLUGIN_DIR", str(tmp_path))
    settings.set_config_source("")
    context = PluginContext()
    context.reload()
    assert context.source == str(tmp_path / "config.jsonc") and context.bundled
    assert context.problems == [] and context.config.satellite_catalogs[0].label == "衛星カタログ"
    bar = ConfigBar(context, lambda: None)
    assert bar.label.text() == "同梱の設定ファイル読み込み済み"
    # A file chosen in the settings dialog wins over the shipped one.
    chosen = tmp_path / "mine.json"
    chosen.write_text("{}", encoding="utf-8")
    settings.set_config_source(str(chosen))
    context.reload()
    assert context.source == str(chosen) and not context.bundled
    assert bar.label.text() == "設定ファイル読み込み済み"
    # Without either: the defaults.
    settings.set_config_source("")
    os.remove(tmp_path / "config.jsonc")
    context.reload()
    assert context.source == "" and not context.bundled


def test_each_feature_has_its_own_icon():
    from qgis.PyQt.QtCore import QBuffer, QByteArray

    from satellite_observation import classFactory

    settings.set_config_source("")
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()

    def png(action) -> bytes:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        action.icon().pixmap(24, 24).save(buffer, "PNG")
        return bytes(data)

    drawn = [png(a) for a in plugin.actions]
    assert all(not a.icon().isNull() for a in plugin.actions)
    # No two menu entries look the same.
    assert len(set(drawn)) == len(plugin.actions) == 30
    plugin.unload()


def test_a_service_out_of_reach_is_logged_as_a_line_not_a_warning():
    from qgis.core import Qgis

    from satellite_observation.core.esri import EsriError

    logs, failed = [], []
    QgsApplication.messageLog().messageReceived.connect(lambda m, tag, level: logs.append((tag, level, m)))

    def work(task):
        raise EsriError("接続不可（https://x.example/FeatureServer/0）")

    run_task("衛星カタログの読み込み", work, failed=failed.append)
    assert wait_for(lambda: failed)
    assert failed == ["接続不可（https://x.example/FeatureServer/0）"]
    mine = [(level, m) for tag, level, m in logs if tag == "衛星撮像計画・観測記録"]
    assert mine == [(Qgis.MessageLevel.Info, "衛星カタログの読み込み: 接続不可（https://x.example/FeatureServer/0）")]
