"""
サーバーが遅いとき (tsudo 2026-11-28), in the 「撮像依頼」 dock: 「保存」 closes the
form at once and lists the request as 「送信中」; saves made in a row wait
their turn (not refused); one not saved opens its form again; only the saved
request is put in the list (the ledger is not read again).
"""
import threading

import pytest

pytest.importorskip("qgis.core")
from qgis.core import QgsPointXY  # noqa: E402

from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_requests import LEDGER, plugin  # noqa: E402,F401 (the fixture)


def _slow(fake):
    """The ledger's edits wait until let go (a slow server)."""
    hold = threading.Event()
    hold.set()
    answer = fake.answer

    def slow(url, form):
        if url == f"{LEDGER}/applyEdits":
            hold.wait(10)
        return answer(url, form)

    fake.answer = slow
    return hold


def _typed(dock, name, priority=150):
    dock.edit(new=True)
    form = dock.form
    form.name.setText(name)
    form.kind.setCurrentText("SAR")
    form.category.setCurrentText("臨時")
    form.priority.setValue(priority)
    form._point_clicked(QgsPointXY(135.3, 34.6))
    return form


def _reads(fake):
    """The ledger's whole reads (load_requests asks for every row)."""
    return sum(1 for url, form in fake.asked if url == f"{LEDGER}/query" and form.get("where") == "1=1")


def test_a_request_saved_is_listed_at_once_and_the_next_waits_its_turn(plugin):
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    ledger = fake.layers[LEDGER]
    hold = _slow(fake)
    hold.clear()
    reads = _reads(fake)
    form = _typed(dock, "大阪湾")
    form._save_clicked()
    # Closed and listed now, marked 送信中, while the ledger has not answered.
    assert not form.isVisible() and not ledger.rows
    [r] = dock.requests
    assert set(dock.sending) == {r.id} and dock.model.item(0, 0).text() == "大阪湾（送信中）"
    assert dock.model.item(0, 0).toolTip().startswith("送信中")
    # The next one is not refused: it waits its turn.
    assert dock.new_button.isEnabled()
    _typed(dock, "東京湾")._save_clicked()
    assert len(dock.requests) == 2 and len(dock._writes) == 1
    # The first changed again while it is sent: based on what its save leaves (not someone else's change).
    dock.chosen = next(x for x in dock.requests if x.name == "大阪湾")
    dock.edit()
    dock.form.priority.setValue(120)
    dock.form._save_clicked()
    hold.set()
    assert wait_for(lambda: not dock.sending and dock.task is None and not dock._writes), dock.status.text()
    assert sorted((x["attributes"]["REQ_NAME"], x["attributes"]["PRIORITY"]) for x in ledger.rows) == [("大阪湾", 120), ("東京湾", 150)]
    assert sorted((x.name, x.priority) for x in dock.requests) == [("大阪湾", 120), ("東京湾", 150)]
    assert all("送信中" not in dock.model.item(i, 0).text() for i in range(dock.model.rowCount()))
    # Only the saved ones put in the list: the ledger was not read again.
    assert _reads(fake) == reads


def test_a_request_not_saved_opens_its_form_again(plugin):
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    answer = fake.answer
    failed = []

    def refusing(url, form):
        if url == f"{LEDGER}/applyEdits" and not failed:
            failed.append(url)
            return {"error": {"code": 500, "message": "busy"}}
        return answer(url, form)

    fake.answer = refusing
    _typed(dock, "大阪湾")._save_clicked()
    assert wait_for(lambda: dock.form is not None and dock.form.isVisible() and not dock.sending)
    assert dock.form.name.text() == "大阪湾" and "保存に失敗" in dock.form.message.text()
    assert dock.requests == [] and dock.model.rowCount() == 0
    dock.form._save_clicked()
    assert wait_for(lambda: not dock.sending and len(fake.layers[LEDGER].rows) == 1)


def test_a_request_saved_while_the_ledger_is_read_stays_listed(plugin):
    p, iface, fake, tmp_path = plugin
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None)
    hold = threading.Event()
    answer = fake.answer

    def slow_read(url, form):
        if url == f"{LEDGER}/query" and form.get("where") == "1=1":
            hold.wait(10)
        return answer(url, form)

    fake.answer = slow_read
    dock.reload()
    assert dock.task is not None
    _typed(dock, "大阪湾")._save_clicked()
    # Waits behind the read; the read does not take it off the list.
    assert len(dock._writes) == 1 and [r.name for r in dock.requests] == ["大阪湾"]
    hold.set()
    assert wait_for(lambda: not dock.sending and dock.task is None and len(fake.layers[LEDGER].rows) == 1)
    assert [r.name for r in dock.requests] == ["大阪湾"] and dock.model.item(0, 0).text() == "大阪湾"
