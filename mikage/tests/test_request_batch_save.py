"""
Several imaging requests saved together (core/request/store.py
save_requests, 2026-11-12): 一括編集, レイヤーから取り込み and the 送信待ち
upload send EDIT_BATCH requests to an applyEdits (not three requests each),
and each request keeps its own checks (#73, #74) and its own result. Also
the client's own batching of large applyEdits / deleteFeatures.
"""
import json
from dataclasses import replace

import pytest

from mikage.core.config import parse_config
from mikage.core.esri import DELETE_BATCH, EDIT_BATCH, EsriClient, EsriError, UnreachableError
from mikage.core.obs.book import Prefs
from mikage.core.obs.model import observation_store_of
from mikage.core.obs.stores import EsriObservationStore
from mikage.core.offline import OfflineWork
from mikage.core.offline_upload import upload_all
from mikage.core.request.bulk import RequestChange, apply_change
from mikage.core.request.store import _chunks, load_requests, request_store_of, save_request, save_requests, waiting_ids
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_obs import ALL, DATES, URL, obs
from mikage.tests.test_offline import Switch, client_of
from mikage.tests.test_request import FIRST, H, LEDGER, request, server


def _store():
    return request_store_of({"label": "依頼", "url": LEDGER}, [], 0)


def _asked(fake, op):
    return [form for url, form in fake.asked if url.endswith("/" + op)]


def _ledger(n):
    """A ledger holding n requests (R-000…), and those requests as read."""
    store, fake = _store(), server()
    client = EsriClient(fake)
    saved = save_requests(client, store, None, [request(f"R-{i:03d}", priority=500) for i in range(n)], FIRST)
    assert len(saved.saved) == n and not saved.failed
    return store, fake, client, sorted(load_requests(client, store, None), key=lambda r: r.id)


def test_a_bulk_change_of_many_requests_is_a_few_requests_to_the_ledger():
    store, fake, client, mine = _ledger(450)
    fake.asked.clear()
    result = apply_change(client, store, None, mine, RequestChange(priority=100, status="停止"), lambda: FIRST + H)
    assert len(result.saved) == 450 and not result.failed
    # One layer information, the ids asked 100 at a time, 200 saved to an applyEdits (was 3 requests per request: 1350).
    edits = _asked(fake, "applyEdits")
    assert [len(json.loads(e["updates"])) for e in edits] == [200, 200, 50]
    assert len(_asked(fake, "query")) == 5
    assert len(fake.asked) == 1 + 5 + 3
    got = load_requests(client, store, None)
    assert {(r.priority, r.status) for r in got} == {(100, "停止")} and len(got) == 450
    assert all(r.pending_update is None for r in result.saved)


def test_each_request_keeps_its_checks_and_result_in_a_batch():
    store, fake, client, mine = _ledger(5)
    # R-001 saved by someone else meanwhile, R-002 deleted by someone else, a new one added in the same batch.
    theirs = next(r for r in load_requests(client, store, None) if r.id == "R-001")
    save_request(EsriClient(fake), store, None, replace(theirs, note="ほかの人"), FIRST + H)
    fake.layers[LEDGER].rows = [r for r in fake.layers[LEDGER].rows if r["attributes"]["REQ_ID"] != "R-002"]
    todo = [replace(r, priority=50) for r in mine] + [request("R-NEW", priority=50)]
    out = save_requests(client, store, None, todo, FIRST + 2 * H)
    assert sorted(r.id for r in out.saved) == ["R-000", "R-003", "R-004", "R-NEW"]
    assert out.conflicts == 2 and sorted(r.id for r, _e in out.failed) == ["R-001", "R-002"]
    assert any("削除済み" in str(e) for _r, e in out.failed) and any("読み込み後に更新" in str(e) for _r, e in out.failed)
    rows = {r["attributes"]["REQ_ID"]: r["attributes"] for r in fake.layers[LEDGER].rows}
    assert "R-002" not in rows and rows["R-001"]["PRIORITY"] == 500 and rows["R-NEW"]["PRIORITY"] == 50
    assert next(r for r in out.saved if r.id == "R-NEW").object_id is not None
    # A request already there but not known here (an add whose answer was lost, #73): changed, not added twice.
    again = save_requests(client, store, None, [request("R-NEW", priority=60)], FIRST + 3 * H)
    assert len(again.saved) == 1 and [r["attributes"]["PRIORITY"] for r in fake.layers[LEDGER].rows if r["attributes"]["REQ_ID"] == "R-NEW"] == [60]


class Refusing:
    """Answers through `fake`, refusing one request's update in each applyEdits, or failing whole applyEdits."""

    def __init__(self, fake, refuse=None, fail=0, unreachable_after=None):
        self.fake, self.refuse, self.fail, self.unreachable_after, self.edits = fake, refuse, fail, unreachable_after, 0

    def __call__(self, url, form):
        if url.endswith("/applyEdits"):
            self.edits += 1
            if self.unreachable_after is not None and self.edits > self.unreachable_after:
                raise UnreachableError("応答なし")
            if self.edits <= self.fail:
                raise EsriError("サーバーのエラー")
        body = self.fake(url, form)
        if url.endswith("/applyEdits") and self.refuse:
            answer = json.loads(body)
            updates = json.loads(form.get("updates") or "[]")
            for f, r in zip(updates, answer["updateResults"]):
                if f["attributes"].get("REQ_ID") == self.refuse:
                    r.update(success=False, error={"description": "ロック中"})
            body = json.dumps(answer, ensure_ascii=False).encode("utf-8")
        return body


def test_a_request_the_server_refuses_fails_alone_and_saving_it_again_is_no_conflict():
    store, fake, client, mine = _ledger(4)
    refusing = EsriClient(Refusing(fake, refuse="R-002"))
    out = save_requests(refusing, store, None, [replace(r, priority=10) for r in mine], FIRST + H)
    assert sorted(r.id for r in out.saved) == ["R-000", "R-001", "R-003"]
    [(failed, error)] = out.failed
    assert failed.id == "R-002" and "ロック中" in str(error) and out.conflicts == 0
    # Kept as read, with the try remembered: the server may hold it (here the fake did save it), so saving again is no conflict.
    assert failed.pending_update == FIRST + H and failed.updated == mine[2].updated
    again = save_requests(client, store, None, [replace(failed, priority=20)], FIRST + 2 * H)
    assert len(again.saved) == 1 and not again.failed


def test_a_batch_that_cannot_be_sent_fails_alone_and_the_next_one_goes():
    store, fake, client, mine = _ledger(EDIT_BATCH + 10)
    out = save_requests(EsriClient(Refusing(fake, fail=1)), store, None, [replace(r, priority=10) for r in mine], FIRST + H)
    assert len(out.failed) == EDIT_BATCH and all("サーバーのエラー" in str(e) for _r, e in out.failed)
    assert len(out.saved) == 10
    names = [r for r, _e in apply_change(EsriClient(Refusing(fake, fail=1)), store, None, mine[:3], RequestChange(kind="SAR"),
                                         lambda: FIRST + 2 * H).failed]
    assert names == ["R-000", "R-001", "R-002"]


def test_cut_off_on_the_way_without_offline_work_the_rest_fail_at_once():
    store, fake, client, mine = _ledger(EDIT_BATCH * 2 + 5)
    transport = Refusing(fake, unreachable_after=1)
    out = save_requests(EsriClient(transport), store, None, [replace(r, priority=10) for r in mine], FIRST + H)
    assert len(out.saved) == EDIT_BATCH and len(out.failed) == EDIT_BATCH + 5
    assert transport.edits == 2  # not one timeout per request


def test_cancel_stops_between_batches():
    store, fake, client, mine = _ledger(EDIT_BATCH + 5)
    asked = iter([False, True])
    out = save_requests(client, store, None, [replace(r, priority=10) for r in mine], FIRST + H, lambda: next(asked))
    assert out.canceled and len(out.saved) == EDIT_BATCH and not out.failed


def test_the_same_request_twice_is_saved_in_turn():
    chunks = list(_chunks([request("A"), request("B"), request("A"), request("C")], 200))
    assert [[r.id for r in c] for c in chunks] == [["A", "B"], ["A", "C"]]
    assert [len(c) for c in _chunks([request(f"R{i}") for i in range(450)], 200)] == [200, 200, 50]


def test_offline_the_requests_are_kept_and_sent_together_later(tmp_path):
    fake = server()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    store = _store()
    client = client_of(work, switch)
    save_requests(client, store, None, [request(f"R{i}", priority=500) for i in range(3)], 1000)
    loaded = sorted(load_requests(client, store, None), key=lambda r: r.id)
    switch.down = True
    out = apply_change(client, store, None, loaded, RequestChange(priority=100), lambda: 2000)
    assert len(out.saved) == 3 and not out.failed
    assert waiting_ids(client, store) == {"R0", "R1", "R2"}
    # Someone else changes R1 meanwhile: it stays waiting, told; the others are sent in one applyEdits.
    theirs = next(r for r in load_requests(EsriClient(fake), store, None) if r.id == "R1")
    save_request(EsriClient(fake), store, None, replace(theirs, priority=900), 3000)
    switch.down = False
    work.probe(switch)
    fake.asked.clear()
    cfg, _ = parse_config({"requestStores": [{"label": "依頼", "url": LEDGER}]})
    report = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, Prefs(), lambda: None)
    assert report.sent == {"request": 2} and len(report.kept) == 1 and "ほかの人" in report.kept[0]
    assert len(_asked(fake, "applyEdits")) == 1
    assert [p.item_id for p in work.pending("request")] == ["R1"] and work.pending("request")[0].note
    online = {r.id: r.priority for r in load_requests(EsriClient(fake), store, None)}
    assert online == {"R0": 100, "R1": 900, "R2": 100}


def test_cut_off_on_the_way_with_offline_work_the_rest_wait(tmp_path):
    fake = server()
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    store = _store()
    save_requests(EsriClient(fake), store, None, [request(f"R{i:03d}", priority=500) for i in range(EDIT_BATCH + 3)], 1000)
    transport = Refusing(fake, unreachable_after=1)
    client = client_of(work, transport)
    loaded = sorted(load_requests(client, store, None), key=lambda r: r.id)
    out = save_requests(client, store, None, [replace(r, priority=10) for r in loaded], 2000)
    assert len(out.saved) == EDIT_BATCH + 3 and not out.failed
    assert len(waiting_ids(client, store)) == 3
    # The batch cut off was tried: its save may be on the server (no conflict when sent).
    assert all(p.data.get("tried") == [2000] for p in work.pending("request"))


def test_the_client_sends_large_edits_in_batches():
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({"NAME": "esriFieldTypeString"}))
    for i in range(10):
        layer.add({"NAME": f"old{i}"})
    client = EsriClient(fake)
    result = client.apply_edits(URL, adds=[{"attributes": {"NAME": f"n{i}"}} for i in range(EDIT_BATCH * 2 + 1)],
                                updates=[{"attributes": {"OBJECTID": 1, "NAME": "x"}}], deletes=[2, 3])
    assert len(_asked(fake, "applyEdits")) == 3
    assert len(result.add_results) == EDIT_BATCH * 2 + 1 and [r["objectId"] for r in result.add_results][:2] == [11, 12]
    assert len(result.update_results) == 1 and len(result.delete_results) == 2 and not result.failures()
    fake.asked.clear()
    results = client.delete_features(URL, list(range(11, 11 + DELETE_BATCH + 2)))
    assert len(_asked(fake, "deleteFeatures")) == 2 and len(results) == DELETE_BATCH + 2


def test_records_removed_for_good_are_deleted_in_batches_and_a_cut_off_tells_what_went():
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in ALL if n != "DELETED_AT"}, max_record_count=2000))
    config = observation_store_of({"url": URL}, [], 0)
    store = EsriObservationStore(config, EsriClient(fake))
    records = [obs(id=f"d{i}") for i in range(DELETE_BATCH + 20)]
    store.save(records)
    calls = []

    def flaky(url, form):
        if url.endswith("/deleteFeatures"):
            calls.append(form)
            if len(calls) == 2:
                raise EsriError("応答なし")
        return fake(url, form)

    store.client = EsriClient(flaky)
    with pytest.raises(EsriError):
        store.remove(records)
    assert len(store.last_removed) == DELETE_BATCH
    assert len(fake.layers[URL].rows) == 20
