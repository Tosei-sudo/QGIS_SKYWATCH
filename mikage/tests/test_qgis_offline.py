"""
The offline work on screen (ui/offline_status.py): the status-bar button
while a server is down, the check that finds it back, the question with the
counts, then the work sent (or thrown away) and the docks read again.
"""
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from mikage.core.config import parse_config  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.model import observation_store_of  # noqa: E402
from mikage.core.obs.offline_stores import OfflineObservationStore  # noqa: E402
from mikage.core.obs.stores import EsriObservationStore  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_obs import URL, obs  # noqa: E402
from mikage.tests.test_offline import Switch, obs_layer  # noqa: E402
from mikage.ui import offline_status  # noqa: E402


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


def test_the_question_at_the_start_stops_with_the_plugin(setup, monkeypatch):
    """
    Work left from an earlier session is asked about a little after the start, but not by a plugin unloaded
    meanwhile: the question is a modal box, and asked by the controller of an earlier test it held the whole run.
    """
    context, controller, switch, fake, reloaded, iface = setup
    work = context.offline
    controller.act_forced.setChecked(True)
    store = OfflineObservationStore(EsriObservationStore(observation_store_of({"label": "共有", "url": URL}, [], 0), context.esri()), work)
    store.save([obs(id="n4")])
    asked = []
    monkeypatch.setattr(offline_status, "ask_upload", lambda parent, w, back: asked.append(back) or "later")
    controller.act_forced.setChecked(False)
    APP.processEvents()
    assert asked == [True] and work.sendable() == 1
    assert controller.start_timer.isActive() and controller.start_timer.isSingleShot()  # started with the settings file
    controller._ask_left_over()
    assert asked == [True, False]
    controller.unload()
    assert not controller.start_timer.isActive()
    controller._ask_left_over()
    assert asked == [True, False]


def test_one_list_of_what_waits_sends_the_chosen_opens_and_drops(setup, monkeypatch):
    """「送信待ち」: the offline changes and the records not saved in one list, each with why it waits."""
    from qgis.PyQt.QtCore import Qt

    from mikage.core.obs.drafts import Draft, OutboxDrafts
    from mikage.ui.outbox_dialog import CHECK_COLOR

    context, controller, switch, fake, reloaded, iface = setup
    work = context.offline
    controller.act_forced.setChecked(True)
    store = OfflineObservationStore(EsriObservationStore(observation_store_of({"label": "共有", "url": URL}, [], 0), context.esri()), work)
    store.save([obs(id="n3", object_name="オフライン中")])
    monkeypatch.setattr(offline_status, "ask_upload", lambda parent, w, back: "later")
    controller.act_forced.setChecked(False)
    shelf = OutboxDrafts(work)
    shelf.keep("共有", Draft(obs(id="d1", object_name="通信失敗"), reason="ネットワークに接続できません"))
    shelf.keep("共有", Draft(obs(id="d2", object_name="他人が変更"), editing_id="d2", base_updated=1,
                             reason="ほかの人が先に更新していました", needs_check=True))
    APP.processEvents()
    assert controller.button.text() == "送信待ち 3 件" and work.sendable() == 2
    dialog = controller.open_list()
    rows = {dialog.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[2]: dialog.tree.topLevelItem(i)
            for i in range(dialog.tree.topLevelItemCount())}
    assert rows["n3"].text(4) == "オフライン中の変更" and rows["d1"].text(4) == "保存できず: ネットワークに接続できません"
    assert rows["d2"].text(4).startswith("要確認") and rows["d2"].foreground(0).color().name() == CHECK_COLOR
    assert all(r.text(1) == "観測記録" for r in rows.values()) and rows["d1"].text(3).startswith("通信失敗")
    # Only one draft can be opened in the form; the offline change cannot.
    rows["n3"].setSelected(True)
    assert not dialog.open_button.isEnabled()
    rows["n3"].setSelected(False)
    opened = []
    controller.open_form = lambda store_label, record_id: opened.append((store_label, record_id)) or True
    rows["d2"].setSelected(True)
    assert dialog.open_button.isEnabled() and dialog.open_chosen() and opened == [("共有", "d2")]
    # Sending with nothing chosen: all but the one to look at.
    dialog.render()
    dialog.tree.clearSelection()
    dialog.send()
    assert wait_for(lambda: not controller.uploading and work.total() == 1)
    assert sorted(r["attributes"]["OBJECT_NAME"] for r in fake.layers[URL].rows) == ["オフライン中", "通信失敗"]
    assert "送信" in dialog.status.text() and [p.item_id for p in work.pending()] == ["d2"]
    # Thrown away when chosen (asked first).
    dialog.tree.topLevelItem(0).setSelected(True)
    monkeypatch.setattr("mikage.ui.outbox_dialog.ask_no", lambda *a: False)
    assert dialog.drop() == 0 and work.total() == 1
    monkeypatch.setattr("mikage.ui.outbox_dialog.ask_no", lambda *a: True)
    assert dialog.drop() == 1 and work.total() == 0 and dialog.tree.topLevelItemCount() == 0
    APP.processEvents()
    assert not controller.button.isVisibleTo(iface.window)
