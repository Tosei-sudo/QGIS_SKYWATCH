"""
The offline work on screen (ui/offline_status.py): the status-bar button
while a server is down, the check that finds it back, the question with the
counts, then the work sent (or thrown away) and the docks read again.
"""
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from skywatch.core.config import parse_config  # noqa: E402
from skywatch.core.esri import EsriClient  # noqa: E402
from skywatch.core.obs.model import observation_store_of  # noqa: E402
from skywatch.core.obs.offline_stores import OfflineObservationStore  # noqa: E402
from skywatch.core.obs.stores import EsriObservationStore  # noqa: E402
from skywatch.qgis_io.context import PluginContext  # noqa: E402
from skywatch.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from skywatch.tests.test_obs import URL, obs  # noqa: E402
from skywatch.tests.test_offline import Switch, obs_layer  # noqa: E402
from skywatch.ui import offline_status  # noqa: E402


@pytest.fixture
def setup(monkeypatch):
    fake = obs_layer()
    switch = Switch(fake)
    context = PluginContext()
    context.config, _ = parse_config({"observationStores": [{"label": "共有", "url": URL}], "localObservations": False,
                                      "offlineWork": {"probeSeconds": 5}})

    def esri(self):
        client = EsriClient(self.offline.transport(switch))
        client.offline = self.offline
        return client

    monkeypatch.setattr(PluginContext, "esri", esri)
    monkeypatch.setattr(offline_status, "qgis_transport", lambda auth=None: switch)
    reloaded = []
    iface = FakeIface()
    controller = offline_status.OfflineController(iface, context, lambda: reloaded.append(1))
    context.configChanged.emit()
    yield context, controller, switch, fake, reloaded, iface
    controller.unload()
    context.close()


def test_offline_then_back_asks_and_sends(setup, monkeypatch):
    context, controller, switch, fake, reloaded, iface = setup
    work = context.offline
    assert not controller.button.isVisibleTo(iface.window)
    switch.down = True
    store = OfflineObservationStore(EsriObservationStore(observation_store_of({"label": "共有", "url": URL}, [], 0), context.esri()), work)
    store.save([obs(id="n1", object_name="船舶-9")])
    assert work.offline and work.counts() == {"observation": 1}
    APP.processEvents()
    assert controller.button.isVisibleTo(iface.window)
    assert "オフライン作業中" in controller.button.text() and "送信待ち 1 件" in controller.button.text()
    assert controller.timer.isActive()
    asked = []

    def ask(parent, w, back):
        asked.append((back, offline_status.counts_text(w)))
        return "upload"

    monkeypatch.setattr(offline_status, "ask_upload", ask)
    switch.down = False
    controller.probe()
    assert wait_for(lambda: work.total() == 0 and reloaded)
    assert asked == [(True, "・観測記録 1 件（共有）")]
    assert [r["attributes"]["OBJECT_NAME"] for r in fake.layers[URL].rows] == ["船舶-9"]
    APP.processEvents()
    assert not controller.button.isVisibleTo(iface.window) and not controller.timer.isActive()


def test_waiting_work_is_thrown_away_when_asked(setup, monkeypatch):
    context, controller, switch, fake, reloaded, iface = setup
    work = context.offline
    controller.act_forced.setChecked(True)  # 手動でオフライン
    assert work.offline and work.forced
    store = OfflineObservationStore(EsriObservationStore(observation_store_of({"label": "共有", "url": URL}, [], 0), context.esri()), work)
    store.save([obs(id="n2")])
    assert switch.asked == 0  # not even tried
    asked = []
    monkeypatch.setattr(offline_status, "ask_upload", lambda parent, w, back: asked.append(back) or "later")
    controller.act_forced.setChecked(False)
    APP.processEvents()
    assert asked == [True] and "送信待ち 1 件" in controller.button.text()
    monkeypatch.setattr(offline_status, "ask_discard", lambda parent, w: False)
    controller.discard()
    assert work.total() == 1
    monkeypatch.setattr(offline_status, "ask_discard", lambda parent, w: True)
    controller.discard()
    assert work.total() == 0 and reloaded and fake.layers[URL].rows == []
