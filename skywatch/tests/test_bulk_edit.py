"""
一括編集 without the screen: several imaging requests (core/request/bulk.py)
and several observation records (ObservationBook.update_many) changed at
once, with the ledger's checks (#73, #74), the edit stamps and partial
failures told.
"""
from dataclasses import replace

from skywatch.core import edit_stamp
from skywatch.core.edit_stamp import EditTracking
from skywatch.core.esri import EsriClient
from skywatch.core.gpkg import open_local
from skywatch.core.obs.book import ObservationBook, ObservedImage, Prefs
from skywatch.core.obs.model import ObservationVocabulary, observation_store_of
from skywatch.core.obs.stores import EsriObservationStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore
from skywatch.core.request.bulk import RequestChange, apply_change, changed
from skywatch.core.request.store import load_requests, request_store_of, save_request
from skywatch.tests.fake_esri import FakeEsri, FakeLayer
from skywatch.tests.test_obs import ALL, DATES, URL, day, obs
from skywatch.tests.test_request import FIRST, H, HISTORY, LEDGER, request, server


def ledger():
    store = request_store_of({"label": "依頼", "url": LEDGER, "history": HISTORY}, [], 0)
    fake = server()
    layer = fake.layers[LEDGER]
    for n in ("CREATED_DATE", "CREATED_USER", "UPDATED_DATE", "UPDATED_USER"):
        layer.types[n] = "esriFieldTypeDate" if n.endswith("DATE") else "esriFieldTypeString"
    return store, fake, EsriClient(fake)


def test_a_change_leaves_requests_already_so_alone():
    r = request("R-1", priority=300, status="有効", boost=100)
    assert changed(r, RequestChange(priority=300, status="有効")) is None
    c = changed(r, RequestChange(priority=900, reset_boost=True, kind="SAR"))
    assert (c.priority, c.boost, c.kind, c.status) == (900, 0, "SAR", "有効")
    assert (r.priority, r.boost) == (300, 100)  # the one read is not touched
    assert RequestChange(status="保留").problems()
    assert RequestChange().empty
    assert RequestChange(priority=100, status="停止").text() == "優先度 100・状態 停止"


def test_requests_are_changed_one_by_one_and_others_saves_are_kept(monkeypatch):
    """#74 holds for each request: the one someone else saved is told by name, the rest are saved."""
    monkeypatch.setenv("USERNAME", "tsudo")
    edit_stamp.set_tracking(EditTracking())
    try:
        store, fake, client = ledger()
        for rid in ("R-1", "R-2", "R-3", "R-4"):
            save_request(client, store, None, request(rid, priority=500, status="有効"), FIRST)
        mine = load_requests(client, store, None)
        [theirs] = [r for r in load_requests(client, store, None) if r.id == "R-2"]
        theirs.note = "ほかの人"
        save_request(client, store, None, theirs, FIRST + H)
        mine = sorted(mine, key=lambda r: r.id)
        mine[3].status, mine[3].priority = "停止", 100  # R-4 already as asked (as read here)
        now = iter(range(int(FIRST + 2 * H), int(FIRST + 3 * H)))
        result = apply_change(client, store, None, mine, RequestChange(priority=100, status="停止"), lambda: next(now))
        assert [r.id for r in result.saved] == ["R-1", "R-3"]
        assert result.unchanged == 1 and result.conflicts == 1
        assert result.failed[0][0] == "R-2"
        assert "2 件を変更" in result.text() and "保存できず 1 件: R-2" in result.text()
        got = {r.id: r for r in load_requests(client, store, None)}
        assert (got["R-1"].priority, got["R-1"].status) == (100, "停止")
        assert (got["R-2"].priority, got["R-2"].status, got["R-2"].note) == (500, "有効", "ほかの人")
        assert got["R-4"].priority == 500
        rows = {r["attributes"]["REQ_ID"]: r["attributes"] for r in fake.layers[LEDGER].rows}
        assert rows["R-1"]["UPDATED_USER"] == "tsudo" and rows["R-1"]["UPDATED_DATE"] is not None
    finally:
        edit_stamp.set_tracking(None)


def test_a_request_deleted_meanwhile_is_not_added_back():
    store, fake, client = ledger()
    save_request(client, store, None, request("R-1"), FIRST)
    save_request(client, store, None, request("R-2"), FIRST)
    mine = load_requests(client, store, None)
    fake.layers[LEDGER].rows = [r for r in fake.layers[LEDGER].rows if r["attributes"]["REQ_ID"] != "R-1"]
    result = apply_change(client, store, None, mine, RequestChange(kind="SAR"), lambda: FIRST + H)
    assert [r.id for r in result.saved] == ["R-2"] and result.conflicts == 1
    assert [r["attributes"]["REQ_ID"] for r in fake.layers[LEDGER].rows] == ["R-2"]


def _book(store, tmp_path, said):
    db = open_local(str(tmp_path / "o.gpkg"))
    b = ObservationBook(store, LocalAreaStore(db, "x"), LocalTaxonomyStore(db, "x"), ObservationVocabulary(), observer=lambda: "田中",
                        prefs=Prefs(), say=said.append)
    b.reload()
    b.set_open_images([ObservedImage("img", "img.tif", day(1), None, "L1")], "L1")
    return b, db


def test_records_are_changed_together_locally(tmp_path):
    said = []
    db = open_local(str(tmp_path / "local.gpkg"))
    store = LocalObservationStore(db)
    store.save([obs(id="a", kind="船舶", activity="停泊"), obs(id="b", kind="船舶", activity="航行"),
                obs(id="c", present=False, activity=""), obs(id="d", deleted_at=day(21))])
    book, other = _book(store, tmp_path, said)
    out = book.update_many(["a", "b", "c", "d"], kind="船舶 > 護衛艦", activity="停泊", confidence="推定")
    assert [o.id for o in out.saved] == ["a", "b", "c"]
    assert out.missing == 1 and out.absent == 1
    rec = {o.id: o for o in store.load()}
    assert {rec[i].kind for i in "abc"} == {"船舶 > 護衛艦"}
    assert (rec["b"].activity, rec["c"].activity, rec["c"].present) == ("停泊", "", False)
    assert {rec[i].confidence for i in "abc"} == {"推定"} and rec["a"].updated_by == "田中"
    assert said[-1].startswith("3 件の記録を一括変更")
    again = book.update_many(["a"], confidence="推定")
    assert again.saved == [] and again.unchanged == 1
    db.close()
    other.close()


def test_records_someone_else_changed_are_left_and_the_rest_saved(tmp_path):
    """#74 in one save: the conflicting record is told, the others are saved and kept in the book."""
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in ALL + ["CONFIDENCE", "UPDATED_BY"]}))
    config = observation_store_of({"url": URL}, [], 0)
    mine, theirs = EsriObservationStore(config, EsriClient(fake)), EsriObservationStore(config, EsriClient(fake))
    mine.save([obs(id="a"), obs(id="b"), obs(id="c")])
    said = []
    book, db = _book(mine, tmp_path, said)
    b = next(o for o in theirs.load() if o.id == "b")
    theirs.save([replace(b, note="ほかの人", updated_at=day(25))])
    out = book.update_many(["a", "b", "c"], confidence="確定")
    assert sorted(o.id for o in out.saved) == ["a", "c"]
    assert (out.failed, out.conflicts) == (1, 1)
    assert "保存できず 1 件（うち 1 件はほかの人が読み込み後に更新" in said[-1]
    here = {o.id: o for o in book.records}
    assert (here["a"].confidence, here["b"].confidence) == ("確定", "")
    server = {o.id: o for o in theirs.load()}
    assert (server["a"].confidence, server["b"].confidence, server["b"].note) == ("確定", "", "ほかの人")
    db.close()


# ---- 選んだ記録の削除 (ObservationBook.remove_many) ------------------------------------------


def test_records_are_deleted_together_locally_and_can_be_restored(tmp_path):
    said = []
    db = open_local(str(tmp_path / "local.gpkg"))
    store = LocalObservationStore(db)
    store.save([obs(id="a"), obs(id="b"), obs(id="c"), obs(id="d", deleted_at=day(21))])
    book, other = _book(store, tmp_path, said)
    assert not book.removes_for_good()
    out = book.remove_many(["a", "c", "d"])
    assert sorted(o.id for o in out.removed) == ["a", "c"] and out.missing == 1 and out.failed == 0
    assert said[-1] == "2 件の記録を削除・削除済み 1 件"
    assert sorted(o.id for o in book.living()) == ["b"]
    rec = {o.id: o for o in store.load()}
    assert rec["a"].deleted_at is not None and rec["c"].deleted_at is not None and rec["b"].deleted_at is None
    # Soft: still restorable.
    assert book.restore([o for o in book.records if o.id in ("a", "c")])
    assert sorted(o.id for o in book.living()) == ["a", "b", "c"]
    db.close()
    other.close()


def test_records_someone_else_changed_are_not_deleted_and_the_rest_are(tmp_path):
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in ALL + ["CONFIDENCE", "UPDATED_BY"]}))
    config = observation_store_of({"url": URL}, [], 0)
    mine, theirs = EsriObservationStore(config, EsriClient(fake)), EsriObservationStore(config, EsriClient(fake))
    mine.save([obs(id="a"), obs(id="b"), obs(id="c")])
    said = []
    book, db = _book(mine, tmp_path, said)
    b = next(o for o in theirs.load() if o.id == "b")
    theirs.save([replace(b, note="ほかの人", updated_at=day(25))])
    assert not book.removes_for_good()
    out = book.remove_many(["a", "b", "c"])
    assert sorted(o.id for o in out.removed) == ["a", "c"]
    assert (out.failed, out.conflicts) == (1, 1)
    assert said[-1].startswith("2 件の記録を削除・削除できず 1 件（うち 1 件はほかの人が読み込み後に更新")
    here = {o.id: o for o in book.records}
    assert here["a"].deleted_at is not None and here["c"].deleted_at is not None and here["b"].deleted_at is None
    server = {o.id: o for o in theirs.load()}
    assert server["a"].deleted_at is not None and server["b"].deleted_at is None and server["b"].note == "ほかの人"
    db.close()


def test_records_are_removed_for_good_where_the_layer_has_no_soft_delete(tmp_path):
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in ALL if n != "DELETED_AT"}))
    config = observation_store_of({"url": URL}, [], 0)
    store = EsriObservationStore(config, EsriClient(fake))
    store.save([obs(id="a"), obs(id="b"), obs(id="c")])
    said = []
    book, db = _book(store, tmp_path, said)
    assert book.removes_for_good()
    # Someone else already removed b: a and c go, b is told.
    fake.layers[URL].rows = [r for r in fake.layers[URL].rows if r["attributes"]["OBS_ID"] != "b"]
    out = book.remove_many(["a", "b", "c"])
    assert sorted(o.id for o in out.removed) == ["a", "c"] and out.failed == 1
    assert "削除できず 1 件（1 件の削除に失敗" in said[-1]
    assert [o.id for o in book.records] == ["b"]
    assert fake.layers[URL].rows == []
    db.close()
