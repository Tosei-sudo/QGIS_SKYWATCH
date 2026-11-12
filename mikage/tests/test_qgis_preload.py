"""
「先読み」 in QGIS: once QGIS has started and the settings file is read, the
satellite catalog and the request ledger are read in the background, so the
docks have them when first opened; the startup's timings go to the log.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsApplication, QgsProject, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QObject  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.request.store import DEFAULT_REQUEST_FIELDS  # noqa: E402
from mikage.qgis_io import preload, settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.qgis_io.preload import Preloader  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_plan import LINE1, LINE2  # noqa: E402

SATS = "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
LEDGER = "https://example.com/arcgis/rest/services/Req/FeatureServer/0"


def start(tmp_path, monkeypatch, extra=None, ledger_rows=()):
    QgsSettings().remove("mikage")
    monkeypatch.setattr(Preloader, "DELAY_MS", 0)
    config = {"cloudForecast": False,
              "satelliteCatalogs": [{"label": "衛星", "url": SATS, "fields": {"name": "NAME", "tle": "TLE"}}],
              "requestStores": [{"label": "撮像依頼", "url": LEDGER}], **(extra or {})}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    fake.add_layer(SATS, FakeLayer({"NAME": "esriFieldTypeString", "TLE": "esriFieldTypeString"})).add({"NAME": "OPT 1", "TLE": f"{LINE1}\n{LINE2}"})
    types = {n: "esriFieldTypeString" for n in DEFAULT_REQUEST_FIELDS.values()}
    types.update({n: "esriFieldTypeDate" for n in ("START_AT", "END_AT", "CREATED_AT", "UPDATED_AT")})
    ledger = fake.add_layer(LEDGER, FakeLayer(types, "esriGeometryPolygon"))
    for attributes, geometry in ledger_rows:
        ledger.add(attributes, geometry)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    logged = []
    log = QgsApplication.messageLog()

    def heard(message, tag, _level):
        if tag == preload.LOG_TAG and message.startswith("起動 "):
            logged.append(message)

    log.messageReceived.connect(heard)
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    return p, fake, logged, lambda: log.messageReceived.disconnect(heard)


@pytest.fixture
def cleanup():
    done = []
    yield done
    for f in done:
        f()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def asked(fake, url):
    return [f for u, f in fake.asked if u == url + "/query" and "returnCountOnly" not in f]


def test_reads_ahead_after_start(tmp_path, monkeypatch, cleanup):
    p, fake, logged, stop = start(tmp_path, monkeypatch)
    cleanup += [stop, p.unload]
    requests = p.docks["SatObsRequests"]
    assert not requests.isVisible() and not requests._loaded

    assert wait_for(lambda: logged)
    # Read without the docks being opened.
    assert requests._loaded and p.docks["SatObsImagingPlan"]._loaded
    assert [s.name for s in p.docks["SatObsImagingPlan"].catalog_sats] == ["OPT 1"]
    assert logged[0].startswith("起動 ") and "先読み: " in logged[0] and "撮像依頼" in logged[0]
    times = p.api.startup_times
    assert set(times["steps"]) >= {"プラグインの読み込み", "ドックの作成", "メニュー・ツールバー", "設定ファイルの読み込み"}
    # The imaging plan dock, open at start, read its catalog itself: not read twice.
    assert times["reads"]["撮像依頼"]["ok"] and "衛星カタログ" not in times["reads"]
    assert len(asked(fake, SATS)) == 1
    # Opening the dock now does not read the ledger again.
    n = len(asked(fake, LEDGER))
    requests.show()
    APP.processEvents()
    assert len(asked(fake, LEDGER)) == n


def test_closed_requests_dock_draws_nothing_on_the_map(tmp_path, monkeypatch, cleanup):
    """Read ahead while closed: the requests' areas appear on the map only once the dock is opened."""
    ring = [[139.0, 35.0], [139.1, 35.0], [139.1, 35.1], [139.0, 35.0]]
    rows = [({"REQ_ID": f"R{i}", "REQ_NAME": f"依頼 {i}", "STATUS": "有効"}, {"rings": [ring], "spatialReference": {"wkid": 4326}})
            for i in range(3)]
    p, fake, logged, stop = start(tmp_path, monkeypatch, ledger_rows=rows)
    cleanup += [stop, p.unload]
    requests = p.docks["SatObsRequests"]
    assert wait_for(lambda: logged) and requests._loaded and len(requests.requests) == 3
    assert not requests.isVisible() and not requests.overlay.painted()
    requests.show()
    requests.raise_()
    APP.processEvents()
    assert requests.overlay.painted()
    requests.hide()
    APP.processEvents()
    assert not requests.overlay.painted()


def test_reads_the_catalog_of_a_closed_imaging_plan(tmp_path, monkeypatch, cleanup):
    # The imaging plan dock closed when QGIS was quit: its catalog is read ahead too.
    from mikage import plugin as plugin_module

    monkeypatch.setattr(plugin_module, "INITIAL_DOCKS", ("SatObsImageCatalog",))
    p, fake, logged, stop = start(tmp_path, monkeypatch)
    cleanup += [stop, p.unload]
    assert wait_for(lambda: logged)
    assert p.docks["SatObsImagingPlan"]._loaded and p.api.startup_times["reads"]["衛星カタログ"]["ok"]


def test_preload_false_reads_nothing(tmp_path, monkeypatch, cleanup):
    p, fake, logged, stop = start(tmp_path, monkeypatch, {"preload": False})
    cleanup += [stop, p.unload]
    assert wait_for(lambda: logged)
    assert not p.docks["SatObsRequests"]._loaded and asked(fake, LEDGER) == []
    assert "先読み" not in logged[0]


def test_failed_read_is_told(tmp_path, monkeypatch, cleanup):
    p, fake, logged, stop = start(tmp_path, monkeypatch, {"preload": ["requests"]})
    cleanup += [stop, p.unload]
    # The ledger cannot be read (another thread: the server down).
    fake.layers.pop(LEDGER)
    assert wait_for(lambda: logged)
    assert p.api.startup_times["reads"]["撮像依頼"]["ok"] is False
    assert "撮像依頼" in logged[0] and "失敗" in logged[0]


class FakeContext(QObject):
    def __init__(self, preload=("requests",)):
        super().__init__()
        self.loading = False

        class C:
            pass
        self.config = C()
        self.config.preload = preload


def test_preloader_items_and_timeout(monkeypatch):
    context = FakeContext(("requests",))
    loader = Preloader(context)
    told, ends = [], []
    loader.item_done.connect(lambda k, ok, _s, m: told.append((k, ok, m)))
    loader.finished.connect(lambda: ends.append(1))
    pending = {}

    def requests(then, failed):
        pending["requests"] = (then, failed)
        return True

    loader.add("requests", "撮像依頼", requests)
    loader.add("events", "イベント", lambda then, failed: then() or True)  # not in `preload`: not read
    loader.add("cache", "ほかの部分の表", lambda then, failed: then() or True, always=True)
    loader.add("none", "設定なし", lambda then, failed: False, always=True)
    loader.add("broken", "壊れた", lambda then, failed: 1 / 0, always=True)
    loader.run()
    # One at a time: the next starts once the first is read.
    assert told == [] and ends == [] and loader.running.keys() == {"requests"}
    # Asked again while reading: once this round ends.
    loader.run()
    pending.pop("requests")[1]("つながらない")
    assert told == [("requests", False, "つながらない")] and ends == []
    assert wait_for(lambda: ends == [1], 2000)
    assert told == [("requests", False, "つながらない"), ("cache", True, "")]
    assert wait_for(lambda: "requests" in pending, 2000)
    # A read never told done ends the round all the same.
    monkeypatch.setattr(Preloader, "TIMEOUT_S", 0.05)
    then, _failed = pending.pop("requests")
    then()  # the second round's read
    assert wait_for(lambda: ends == [1, 1], 2000)
    told.clear()
    loader.run()
    assert wait_for(lambda: ends == [1, 1, 1], 2000)
    assert told == [("requests", False, "時間切れ"), ("cache", True, "")]
    pending.pop("requests")[0]()  # told after the time ran out: not counted twice
    assert told == [("requests", False, "時間切れ"), ("cache", True, "")]
    loader.close()


def test_preloader_reads_one_at_a_time():
    context = FakeContext(("satellites", "requests"))
    loader = Preloader(context)
    started, pending, ends = [], {}, []
    loader.finished.connect(lambda: ends.append(1))

    def item(key):
        def start(then, failed):
            started.append(key)
            pending[key] = then
            return True
        return start

    loader.add("satellites", "衛星カタログ", item("satellites"))
    loader.add("requests", "撮像依頼", item("requests"))
    loader.add("offline", "控え", item("offline"), always=True)
    loader.run()
    assert started == ["satellites"]
    pending.pop("satellites")()
    assert wait_for(lambda: started == ["satellites", "requests"], 2000)
    pending.pop("requests")()
    assert wait_for(lambda: started[-1] == "offline", 2000) and ends == []
    pending.pop("offline")()
    assert ends == [1]
    loader.close()
