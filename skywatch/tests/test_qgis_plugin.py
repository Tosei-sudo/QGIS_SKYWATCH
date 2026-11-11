"""
The QGIS side, with qgis.testing: the plugin registers its docks and menu,
the settings file is read, and requests go through QGIS's network stack
(off the main thread) with an authcfg.
"""
import http.server
import json
import os
import re
import threading

import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings  # noqa: E402

from skywatch.core.esri import EsriClient  # noqa: E402
from skywatch.qgis_io import settings  # noqa: E402
from skywatch.qgis_io.network import qgis_transport  # noqa: E402
from skywatch.qgis_io.tasks import run_task  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

HERE = os.path.dirname(__file__)


@pytest.fixture(autouse=True)
def clean_settings():
    QgsSettings().remove("skywatch")
    yield
    QgsSettings().remove("skywatch")


def test_the_plugin_registers_its_docks_menu_and_reads_the_settings_file():
    from skywatch import classFactory

    settings.set_config_source(os.path.join(HERE, "data", "viewer-config.jsonc"))
    # Its `layers` are examples on example.com: not opened here (test_qgis_startup_layers covers them).
    settings.set_startup_layers_on(False)
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    assert [d.windowTitle() for d in iface.docks] == ["画像カタログ", "撮像計画", "撮像依頼", "撮像計画ファイル出力", "撮像予定", "衛星パス", "雲量マップ", "観測記録", "分類体系の編集", "通知", "画像補正", "影から高さ", "スペクトル", "移動体（AIS・ADS-B）", "ダッシュボード", "AI結果", "イベント", "データ分析"]
    # By area (tsudo, 2026-11-07): a submenu each, then the dock layout and 「設定…」 in the menu itself.
    # (「通知」 shows its unread count, 「通知（1）」, when an earlier test left a notice.)
    tree = [(e[0], [re.sub(r"^通知（\d+）$", "通知", t) for t in e[1]]) if isinstance(e, tuple) else e for e in iface.menu_tree()]
    assert tree == [
        ("撮像計画", ["撮像計画", "衛星パス", "移動目標撮像計画…", "-", "雲量マップ", "雲量を属性に付与…"]),
        ("撮像依頼", ["撮像依頼", "撮像計画ファイル出力", "撮像予定"]),
        ("観測記録", ["観測記録", "分類体系の編集", "通報文の作成…", "通報記録の確認…", "観測記録のエクスポート…", "観測記録の一括取込み…", "分担…", "引き継ぎメモ…", "移動体（AIS・ADS-B）", "ダッシュボード"]),
        ("画像", ["画像カタログ", "通知", "-", "画像補正", "影から高さ", "スペクトル", "-", "スワイプ比較", "位置ずらし", "位置ずらしの解除"]),
        ("解析・処理", ["データ分析", "-", "パンシャープン…", "変化抽出（2 時期）…", "自動位置合わせ…", "土地被覆分類…", "簡易オルソ補正…", "-",
                    "測地線バッファ…", "正距方位での重心…", "移動体と検出の照合…", "-", "サーバ処理…"]),
        ("AI", ["物体検出（AI）…", "クリック抽出（AI）", "AI結果", "検出結果を DB へ転記…", "-", "教師データセットの作成…", "アノテーション変換（点→矩形）…", "アノテーション変換（教師データセット）…", "AI 学習（物体検出）…"]),
        ("地図・レイヤー", ["背景地図", "設定ファイルのレイヤーを追加", "新規スクラッチレイヤー（テンプレート）…", "-",
                       "ArcGIS Web マップを開く…", "ArcGIS Web マップとして保存…"]),
        "-", "イベント", "担当と個人設定…", "ツールバーのボタン…", "ドックのレイアウト", "ドックを初期位置に戻す", "接続と表の点検…", "監査ログ…", "使い方（ヘルプ）…", "Python API リファレンス…", "設定…",
    ]
    # Every entry of the plugin is in the menu once.
    shown = [t for e in iface.menu_tree() if isinstance(e, tuple) for t in e[1] if t != "-"] + ["イベント", "担当と個人設定…", "ツールバーのボタン…", "ドックのレイアウト", "ドックを初期位置に戻す", "接続と表の点検…", "監査ログ…", "使い方（ヘルプ）…", "Python API リファレンス…", "設定…"]
    assert sorted(shown) == sorted(a.text() for a in plugin.actions)
    # Only the image catalog and the imaging plan are open at first (QGIS shows every dock it adds).
    assert [d.windowTitle() for d in iface.docks if d.isVisible()] == ["画像カタログ", "撮像計画"]
    # They work as if opened: the catalog's footprints drawn on the map.
    assert plugin.docks["SatObsImageCatalog"].overlay is not None and plugin.docks["SatObsNotices"].isHidden()
    assert plugin.api.problems == []
    assert plugin.api.config.satellite_catalogs[0].label == "衛星カタログ"
    roles = [s.role for s in plugin.context.services()]
    assert roles.count("確認範囲") == 1 and "計画の保存先" not in roles
    plugin.docks["SatObsObservations"].show()
    assert not plugin.docks["SatObsObservations"].isHidden()
    from skywatch.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(plugin.context, iface.window)
    assert dialog.services.rowCount() == len(plugin.context.services())
    assert dialog.problems.count() == 0
    assert not dialog.startup_layers.isChecked()
    # Closed, it no longer fills itself on each reading of the settings (#87); the menu's one is deleted.
    listening = plugin.context.receivers(plugin.context.configChanged)
    dialog.reject()
    assert plugin.context.receivers(plugin.context.configChanged) == listening - 1
    opened = []
    SettingsDialog.exec = lambda self: opened.append(self) or 0
    try:
        plugin.open_settings()
    finally:
        del SettingsDialog.exec
    assert len(opened) == 1 and plugin.context.receivers(plugin.context.configChanged) == listening
    from qgis.PyQt import sip
    from qgis.PyQt.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(opened[0])
    # 「パンシャープン」 in the Processing toolbox while the plugin is loaded.
    assert QgsApplication.processingRegistry().algorithmById("skywatch:pansharpen") is not None
    plugin.unload()
    assert iface.docks == [] and iface.menus == []
    assert QgsApplication.processingRegistry().algorithmById("skywatch:pansharpen") is None


def test_the_settings_file_is_chosen_from_the_menu_only_and_its_problems_go_to_the_message_bar(tmp_path):
    """No dock carries a 「設定…」 button (tsudo, 2026-11-07); a file with problems says so once in the message bar."""
    from qgis.PyQt.QtWidgets import QPushButton

    from skywatch import classFactory

    broken = tmp_path / "broken.json"
    broken.write_text('{"imageCatalogs": 1}', encoding="utf-8")
    settings.set_config_source(str(broken))
    settings.set_startup_layers_on(False)
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    try:
        assert plugin.context.problems
        for dock in plugin.docks.values():
            assert not [b for b in dock.findChildren(QPushButton) if b.text() == "設定…"], dock.windowTitle()
        assert [t for _, t in iface.menus][-1] == "設定…"
        items = iface.bar.items()
        assert len(items) == 1 and "問題 " in items[0].text()
        assert [b for b in items[0].findChildren(QPushButton) if b.text() == "設定…"]
        # Reading the same file again doesn't repeat it.
        plugin.context.reload()
        assert len(iface.bar.items()) == 1
        # A file without problems adds nothing.
        good = tmp_path / "good.json"
        good.write_text("{}", encoding="utf-8")
        settings.set_config_source(str(good))
        plugin.context.reload()
        assert len(iface.bar.items()) == 1
    finally:
        plugin.unload()
        settings.set_config_source("")


def test_the_docks_start_in_three_groups_of_tabs_and_come_back_on_screen():
    from qgis.PyQt.QtCore import QPoint, Qt

    from skywatch import classFactory

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

    assert tabs("SatObsImagingPlan") == ["SatObsCloud", "SatObsImagingPlan", "SatObsPlanFiles", "SatObsRequests", "SatObsSatellitePass", "SatObsSchedule"]
    assert tabs("SatObsImageCatalog") == ["SatObsAnalysis", "SatObsDashboard", "SatObsEnhance", "SatObsImageCatalog", "SatObsNotices", "SatObsShadowHeight", "SatObsSpectral"]
    assert tabs("SatObsObservations") == ["SatObsAiResults", "SatObsEvents", "SatObsMoving", "SatObsObservations", "SatObsTaxonomy"]
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
    assert w.dockWidgetArea(d["SatObsNotices"]) == left and tabs("SatObsImageCatalog") == ["SatObsAnalysis", "SatObsDashboard", "SatObsEnhance", "SatObsImageCatalog", "SatObsNotices", "SatObsShadowHeight", "SatObsSpectral"]
    assert not d["SatObsObservations"].isFloating() and d["SatObsNotices"].isVisible()
    plugin.unload()


def test_a_broken_settings_file_says_why(tmp_path):
    from skywatch.qgis_io.context import PluginContext

    path = tmp_path / "config.json"
    path.write_text('{"satelliteCatalogs": "x", "requestStores": [{"url": "https://x/MapServer/0"}]}', encoding="utf-8")
    settings.set_config_source(str(path))
    context = PluginContext()
    context.reload()
    assert context.problems == ["satelliteCatalogs は配列で指定", "requestStores[0] の url は編集できる …/FeatureServer/0 のようなレイヤーの URL で指定"]


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

    from skywatch.qgis_io.context import PluginContext
    from skywatch.ui.settings_dialog import SettingsDialog

    shutil.copy(os.path.join(HERE, "data", "viewer-config.jsonc"), tmp_path / "config.jsonc")
    monkeypatch.setattr(settings, "PLUGIN_DIR", str(tmp_path))
    settings.set_config_source("")
    context = PluginContext()
    context.reload()
    assert context.source == str(tmp_path / "config.jsonc") and context.bundled
    assert context.problems == [] and context.config.satellite_catalogs[0].label == "衛星カタログ"
    assert SettingsDialog(context).status.text().startswith("同梱の設定ファイルを使用")
    # A file chosen in the settings dialog wins over the shipped one.
    chosen = tmp_path / "mine.json"
    chosen.write_text("{}", encoding="utf-8")
    settings.set_config_source(str(chosen))
    context.reload()
    assert context.source == str(chosen) and not context.bundled
    assert SettingsDialog(context).status.text().startswith("問題なし")
    # Without either: the defaults.
    settings.set_config_source("")
    os.remove(tmp_path / "config.jsonc")
    context.reload()
    assert context.source == "" and not context.bundled


def test_a_locked_zip_reads_only_its_shipped_settings_file(tmp_path, monkeypatch):
    """config.lock beside the shipped config.json (build_zip.py --lock-config): a file chosen in the settings dialog is ignored."""
    import shutil

    from skywatch.qgis_io.context import PluginContext
    from skywatch.ui.settings_dialog import SettingsDialog

    monkeypatch.setattr(settings, "PLUGIN_DIR", str(tmp_path))
    (tmp_path / "config.lock").write_text("", encoding="utf-8")
    chosen = tmp_path / "mine.json"
    chosen.write_text("{}", encoding="utf-8")
    settings.set_config_source(str(chosen))
    # A lock without a shipped file does nothing.
    assert not settings.config_locked()
    assert settings.effective_config_source() == (str(chosen), False)
    shutil.copy(os.path.join(HERE, "data", "viewer-config.jsonc"), tmp_path / "config.jsonc")
    assert settings.config_locked()
    context = PluginContext()
    context.reload()
    assert context.source == str(tmp_path / "config.jsonc") and context.bundled and context.locked
    assert context.config.satellite_catalogs[0].label == "衛星カタログ"
    # The settings dialog shows the shipped file, can't change it, and leaves the person's own choice as it was.
    dialog = SettingsDialog(context)
    assert dialog.source.text() == str(tmp_path / "config.jsonc") and dialog.source.isReadOnly()
    assert dialog.status.text().startswith("同梱の設定ファイルに固定")
    dialog.source.setText(str(chosen))
    dialog._load()
    assert context.source == str(tmp_path / "config.jsonc")
    dialog.accept()
    assert settings.config_source() == str(chosen)
    # Without the lock the person's choice counts again.
    os.remove(tmp_path / "config.lock")
    context.reload()
    assert context.source == str(chosen) and not context.locked
    settings.set_config_source("")


def test_each_feature_has_its_own_icon():
    from qgis.PyQt.QtCore import QBuffer, QByteArray

    from skywatch import classFactory

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
    assert len(set(drawn)) == len(plugin.actions) == 59
    plugin.unload()


def test_a_service_out_of_reach_is_logged_as_a_line_not_a_warning():
    from qgis.core import Qgis

    from skywatch.core.esri import EsriError

    logs, failed = [], []
    QgsApplication.messageLog().messageReceived.connect(lambda m, tag, level: logs.append((tag, level, m)))

    def work(task):
        raise EsriError("接続不可（https://x.example/FeatureServer/0）")

    run_task("衛星カタログの読み込み", work, failed=failed.append)
    assert wait_for(lambda: failed)
    assert failed == ["接続不可（https://x.example/FeatureServer/0）"]
    mine = [(level, m) for tag, level, m in logs if tag == "SKYWATCH"]
    assert mine == [(Qgis.MessageLevel.Info, "衛星カタログの読み込み: 接続不可（https://x.example/FeatureServer/0）")]


def test_every_service_of_the_settings_can_be_given_a_sign_in():
    """#75: the request ledger, its history, AI results, the weather and the rest are in 「接続の認証」."""
    from skywatch.core.config import parse_config
    from skywatch.qgis_io.context import PluginContext

    base = "https://gis.example.com/arcgis/rest/services"
    context = PluginContext()
    context.config, problems = parse_config({
        "requestStores": [{"label": "台帳", "url": f"{base}/Req/FeatureServer/0", "history": f"{base}/Req/FeatureServer/1"}],
        "aiResultSources": [{"label": "外部", "url": f"{base}/Ai/FeatureServer/0",
                             "fields": {"imageId": "IMAGE_ID", "kind": "CLASS", "score": "SCORE"}}],
        "cloudForecast": {"url": "https://weather.example.com/v1/forecast", "label": "社内予報"},
    })
    services = {s.url: s.role for s in context.services()}
    assert services[f"{base}/Req/FeatureServer/0"] == "撮像依頼台帳"
    assert services[f"{base}/Req/FeatureServer/1"] == "出力履歴"
    assert services["https://weather.example.com/v1/forecast"] == "雲量予報"
    assert problems == [] and services[f"{base}/Ai/FeatureServer/0"] == "外部AI結果"


def test_a_settings_url_that_cannot_be_read_falls_back_to_the_last_one_read(monkeypatch):
    """#85: the last file read from the URL (its tokens not kept) rather than nothing; an older answer does not win."""
    from skywatch.core.esri import EsriError
    from skywatch.qgis_io import context as context_module
    from skywatch.qgis_io.context import PluginContext
    from skywatch.tests.qgis_fakes import wait_for

    url = "https://config.example.com/skywatch/config.json"
    text = json.dumps({"requestStores": [{"label": "台帳", "url": "https://g/arcgis/rest/services/R/FeatureServer/0", "token": "SECRET"}]})
    answers = {"next": text}

    def fetch(source, authcfg=None):
        value = answers["next"]
        if isinstance(value, Exception):
            raise value
        return value.encode("utf-8")

    monkeypatch.setattr(context_module, "get_bytes", fetch)
    QgsSettings().remove("skywatch/config_cache")
    settings.set_config_source(url)
    try:
        context = PluginContext()
        context.reload()
        assert wait_for(lambda: not context.loading)
        assert context.config.request_stores[0].token == "SECRET"
        assert "SECRET" not in QgsSettings().value("skywatch/config_cache", "")
        answers["next"] = EsriError("接続不可")
        context.reload()
        assert wait_for(lambda: not context.loading)
        assert context.config.request_stores[0].label == "台帳" and context.config.request_stores[0].token is None
        assert context.problems[0].startswith("設定ファイルの取得不可（接続不可）。前回")
    finally:
        settings.set_config_source("")
        QgsSettings().remove("skywatch/config_cache")
