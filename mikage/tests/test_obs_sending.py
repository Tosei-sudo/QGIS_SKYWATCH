"""
サーバーが遅いとき (tsudo 2026-11-28): a record made here is saved in one
request (EsriObservationStore.expect_new); a record confirmed is listed at once
(送信中) and sent behind the screen, several together (ObservationBook.hold_submit,
send_held); what is being sent is kept on this computer until the server takes
it (OfflineWork.hold, OutboxDrafts.left_over); how slow the servers answer
(OfflineWork.slowness).
"""
import threading
import time
from dataclasses import replace

import pytest

from mikage.core import offline as offline_module
from mikage.core.esri import EsriClient
from mikage.core.gpkg import open_local
from mikage.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs
from mikage.core.obs.drafts import LEFT_OVER, Draft, OutboxDrafts
from mikage.core.obs.model import ObservationVocabulary, observation_store_of
from mikage.core.obs.stores import EsriObservationStore, LocalAreaStore, LocalTaxonomyStore
from mikage.core.obs.taxonomy import Taxon
from mikage.core.offline import OfflineWork
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_obs import ALL, DATES, URL, day, obs


def layer():
    fake = FakeEsri()
    rows = fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in ALL + ["CONFIDENCE", "UPDATED_BY"]}))
    return fake, rows


def store_of(fake):
    return EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))


def book_of(store, tmp_path, drafts=None, taxonomy=()):
    db = open_local(str(tmp_path / "o.gpkg"))
    b = ObservationBook(store, LocalAreaStore(db, "x"), LocalTaxonomyStore(db, "x"), ObservationVocabulary(taxonomy=list(taxonomy)),
                        observer=lambda: "田中", prefs=Prefs(), drafts=drafts)
    b.set_open_images([ObservedImage("img", "img.tif", day(1), None, "L1")], "L1")
    b.reload()
    return b, db


def asked(fake, since):
    """The requests asked since (the layer's information left out: asked again after an error)."""
    return [op for op in (url.rsplit("/", 1)[-1] for url, _form in fake.asked[since:]) if not op.isdigit()]


def test_a_record_made_here_is_saved_in_one_request(tmp_path):
    fake, rows = layer()
    book, db = book_of(store_of(fake), tmp_path)
    n = len(fake.asked)
    saved = book.submit(139.7, 35.6, FormValues(object="あおば", kind="船舶"))
    assert saved is not None and len(rows.rows) == 1
    # Its id was made just now: the layer is not asked whether it holds it (a slow server: one request, not two).
    assert asked(fake, n) == ["applyEdits"]
    # A change of it is asked about first, as before (someone else's change is not saved over, #74).
    n = len(fake.asked)
    assert book.update(saved.id, note="入港") is not None
    assert asked(fake, n) == ["query", "applyEdits"]
    db.close()


def test_a_record_whose_add_got_no_answer_is_asked_about_when_sent_again(tmp_path):
    fake, rows = layer()
    store = store_of(fake)
    store.save([obs(id="before")])
    o = obs(id="new-1")
    store.expect_new([o.id])
    fake.fail_next = {"error": {"code": 500, "message": "busy"}}
    n = len(fake.asked)
    with pytest.raises(Exception):
        store.save([o])
    assert asked(fake, n) == ["applyEdits"]
    n = len(fake.asked)
    store.save([o])
    # Sent again: the server may hold the first try (#73), so it is asked about first.
    assert asked(fake, n) == ["query", "applyEdits"] and len(rows.rows) == 2


def test_records_confirmed_are_listed_at_once_and_sent_together(tmp_path):
    fake, rows = layer()
    book, db = book_of(store_of(fake), tmp_path)
    first = book.hold_submit(139.7, 35.6, FormValues(object="あおば", kind="船舶"))
    assert book.ask_send()
    second = book.hold_submit(139.8, 35.6, FormValues(object="しらせ", kind="船舶"))
    # Asked once: the second goes with the first.
    assert not book.ask_send()
    assert {o.id for o in book.records} == {first.record.id, second.record.id} == book.sending_ids()
    assert first.record.recorded_by == "田中" and not rows.rows
    n = len(fake.asked)
    sent = book.send_held()
    assert asked(fake, n) == ["applyEdits"] and len(rows.rows) == 2
    assert [o.id for o in sent.saved] == [first.record.id, second.record.id] and not sent.lost
    assert book.sending_ids() == set() and {o.id for o in book.records} == {first.record.id, second.record.id}
    assert book.status == "2 件を記録"
    db.close()


def test_a_record_not_saved_goes_back_and_is_kept_as_a_draft(tmp_path):
    fake, rows = layer()
    store = store_of(fake)
    store.save([obs(id="known", object_name="あおば", note="")])
    book, db = book_of(store, tmp_path)
    known = book.record("known")
    new = book.hold_submit(139.7, 35.6, FormValues(object="しらせ", kind="船舶"))
    change = book.hold_submit(known.lon, known.lat, FormValues(object="あおば", kind="船舶", note="入港"), editing=known)
    assert book.record("known").note == "入港"
    fake.fail_next = {"error": {"code": 500, "message": "busy"}}
    sent = book.send_held()
    # Not saved: the new one is off the list, the change put back; both kept with why, the form's values with them.
    assert book.record(new.record.id) is None and book.record("known").note == ""
    assert {h.record.id for h, _why, _d in sent.lost} == {new.record.id, "known"}
    assert {d.id for d in book.drafts()} == {new.record.id, "known"}
    kept = book.draft(new.record.id)
    assert kept.values["object"] == "しらせ" and "への保存に失敗" in kept.reason and not kept.needs_check
    assert book.sending_ids() == set() and len(rows.rows) == 1
    # Sent again from the draft (the form opened from it): saved, the draft gone.
    again = book.hold_submit(139.7, 35.6, FormValues(object="しらせ", kind="船舶"), draft=new.record.id)
    assert again.record.id == new.record.id
    book.send_held()
    assert len(rows.rows) == 2 and book.draft(new.record.id) is None
    db.close()


def test_records_being_sent_stay_listed_through_a_read(tmp_path):
    fake, rows = layer()
    book, db = book_of(store_of(fake), tmp_path)
    held = book.hold_submit(139.7, 35.6, FormValues(object="あおば", kind="船舶"))
    book.reload()
    assert book.record(held.record.id) is not None
    book.refresh()
    assert book.record(held.record.id) is not None and held.record.id not in book.changed_by_others
    book.send_held()
    assert book.record(held.record.id) is not None and len(rows.rows) == 1
    db.close()


def test_a_record_changed_again_while_it_is_sent_goes_next(tmp_path):
    fake, rows = layer()
    book, db = book_of(store_of(fake), tmp_path)
    held = book.hold_submit(139.7, 35.6, FormValues(object="あおば", kind="船舶"))
    started = threading.Event()
    go = threading.Event()
    real = fake.answer

    def slow(url, form):
        if url.endswith("applyEdits") and not started.is_set():
            started.set()
            go.wait(5)
        return real(url, form)

    fake.answer = slow
    t = threading.Thread(target=book.send_held)
    t.start()
    assert started.wait(5)
    # Changed in the form while the first save is on its way: listed as changed, sent after.
    shown = book.record(held.record.id)
    book.hold_submit(shown.lon, shown.lat, FormValues(object="あおば", kind="船舶", note="入港"), editing=shown)
    assert book.ask_send()
    go.set()
    t.join(5)
    assert book.record(held.record.id).note == "入港" and held.record.id in book.sending_ids()
    book.send_held()
    assert [r["attributes"]["NOTE"] for r in rows.rows] == ["入港"] and book.sending_ids() == set()
    db.close()


def test_a_typed_name_of_an_individual_not_read_asks_the_store_first(tmp_path):
    fake, _rows = layer()
    taxa = [Taxon(["船舶"]), Taxon(["船舶", "護衛艦"], name_label="艦名")]
    book, db = book_of(store_of(fake), tmp_path, taxonomy=taxa)
    if not book.scoped:
        pytest.skip("a store read whole asks nothing")
    assert book.needs_store(FormValues(object="あおば", kind="船舶 > 護衛艦"))
    assert not book.needs_store(FormValues(object="", kind="船舶 > 護衛艦"))
    assert not book.needs_store(FormValues(object="車両", kind="船舶"))
    saved = book.submit(139.7, 35.6, FormValues(object="あおば", kind="船舶 > 護衛艦"))
    assert saved is not None and not book.needs_store(FormValues(object="あおば", kind="船舶 > 護衛艦"))
    db.close()


def test_records_being_sent_when_qgis_ended_become_drafts(tmp_path):
    path = str(tmp_path / "offline.sqlite")
    work = OfflineWork(path)
    shelf = OutboxDrafts(work)
    shelf.hold("共有", Draft(obs(id="a", object_name="あおば"), {"object": "あおば"}, reason="送信中"))
    shelf.hold("共有", Draft(obs(id="b"), reason="送信中"))
    shelf.let_go("共有", ["b"])
    # This session's own are being sent: not left over.
    assert shelf.left_over() == 0 and shelf.all("共有") == []
    work.close()
    # The next start (another session): what was still being sent is a draft, to be sent again or looked at.
    later = OfflineWork(path)
    shelf = OutboxDrafts(later)
    assert shelf.left_over() == 1
    [d] = shelf.all("共有")
    assert d.id == "a" and d.reason == LEFT_OVER and d.values == {"object": "あおば"}
    assert later.counts() == {"draft": 1} and later.sendable() == 1
    assert shelf.left_over() == 0
    later.close()


def test_a_book_keeps_what_it_sends_in_the_outbox_until_it_is_taken(tmp_path):
    fake, rows = layer()
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    book, db = book_of(store_of(fake), tmp_path, drafts=OutboxDrafts(work))
    held = book.hold_submit(139.7, 35.6, FormValues(object="あおば", kind="船舶"))
    left = OfflineWork(str(tmp_path / "offline.sqlite"))
    left.session = "a later one"
    # (as a later start would find it, had QGIS ended now)
    [(store, kept)] = [x for x in _peek_sending(left)]
    assert store == book.store.label and kept["record"]["id"] == held.record.id
    # Nothing waits in the outbox meanwhile (not counted, not sent from there).
    assert work.total() == 0
    book.send_held()
    assert list(_peek_sending(left)) == [] and len(rows.rows) == 1
    db.close()
    work.close()
    left.close()


def _peek_sending(work):
    import json

    with work.lock:
        for store, data in work.db().execute("SELECT store, data FROM sending").fetchall():
            yield store, json.loads(data)


def test_how_slow_the_servers_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(offline_module, "SLOW_MS", 50)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    gate = threading.Event()

    def send(url, form):
        if "slow" in url:
            gate.wait(5)
        return b"{}"

    ask = work.transport(send)
    assert work.slowness() is None
    t = threading.Thread(target=ask, args=("https://gis.example/slow/query", {}))
    t.start()
    time.sleep(0.12)
    host, seconds, waiting = work.slowness()
    assert host == "https://gis.example" and waiting and seconds >= 0.05
    gate.set()
    t.join(5)
    host, seconds, waiting = work.slowness()
    assert not waiting and seconds >= 0.05
    # A quick answer from that server: no longer slow.
    ask("https://gis.example/fast/query", {})
    assert work.slowness() is None
    work.close()


def test_the_status_text_of_slowness():
    from mikage.ui.offline_status import slow_text

    assert slow_text(None) == ""
    assert slow_text(("gis", 12.4, True)) == "応答が遅い（12 秒待ち）"
    assert slow_text(("gis", 7.0, False)) == "応答が遅い（7 秒かかった）"


def test_same_records_as_before():
    # (the drafts and held rows keep the record whole)
    o = obs(id="x", note="n")
    assert Draft(replace(o)).to_json()["record"]["note"] == "n"


def test_a_typed_name_of_an_individual_is_looked_for_as_it_is_sent(tmp_path):
    fake, rows = layer()
    store = store_of(fake)
    # あおば was recorded long ago elsewhere: not among the records read around this image.
    store.save([obs(id="old", image_id="far", object_id="OBJ-AOBA", object_name="あおば", kind="船舶 > 護衛艦", lon=10.0, lat=10.0)])
    taxa = [Taxon(["船舶"]), Taxon(["船舶", "護衛艦"], name_label="艦名")]
    book, db = book_of(store, tmp_path, taxonomy=taxa)
    assert book.record("old") is None
    n = len(fake.asked)
    held = book.hold_submit(139.7, 35.6, FormValues(object="あおば", kind="船舶 > 護衛艦"))
    # Listed now, nothing asked while the person waits.
    assert len(fake.asked) == n and book.record(held.record.id) is not None and held.record.object_id != "OBJ-AOBA"
    book.send_held()
    saved = book.record(held.record.id)
    assert saved.object_id == "OBJ-AOBA" and saved.object_name == "あおば"
    assert {r["attributes"]["OBJECT_ID"] for r in rows.rows} == {"OBJ-AOBA"}
    db.close()


def test_the_same_new_name_typed_twice_while_it_is_sent_is_one_thing(tmp_path):
    fake, rows = layer()
    store = store_of(fake)
    store.save([obs(id="old", image_id="far", object_id="OBJ-AOBA", object_name="あおば", kind="船舶 > 護衛艦", lon=10.0, lat=10.0)])
    taxa = [Taxon(["船舶"]), Taxon(["船舶", "護衛艦"], name_label="艦名")]
    book, db = book_of(store, tmp_path, taxonomy=taxa)
    book.hold_submit(139.7, 35.6, FormValues(object="あおば", kind="船舶 > 護衛艦"))
    book.hold_submit(139.71, 35.6, FormValues(object="あおば", kind="船舶 > 護衛艦"))
    book.send_held()
    assert [r["attributes"]["OBJECT_ID"] for r in rows.rows].count("OBJ-AOBA") == 3
    db.close()


def test_a_checked_area_made_here_is_saved_in_one_request_and_asked_about_when_sent_again():
    from mikage.core.obs.areas import area_store_of
    from mikage.core.obs.stores import EsriAreaStore
    from mikage.tests.test_obs import area

    fake = FakeEsri()
    url = "https://x/FeatureServer/2"
    rows = fake.add_layer(url, FakeLayer({n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeString"
                                          for n in ("AREA_ID", "IMAGE_ID", "KIND", "NOTE", "CREATED_AT", "UPDATED_AT", "DELETED_AT")},
                                         "esriGeometryPolygon"))
    store = EsriAreaStore("確認範囲", area_store_of(url, [], "a"), EsriClient(fake))
    store.load()
    n = len(fake.asked)
    store.expect_new(["n1"])
    store.save([area(id="n1")])
    assert asked(fake, n) == ["applyEdits"] and len(rows.rows) == 1
    # One that did not go (its answer may have been lost): asked about when sent again, so not added twice (#73).
    store.expect_new(["n2"])
    fake.fail_next = {"error": {"code": 500, "message": "busy"}}
    with pytest.raises(Exception):
        store.save([area(id="n2")])
    n = len(fake.asked)
    store.save([area(id="n2")])
    assert asked(fake, n) == ["query", "applyEdits"] and len(rows.rows) == 2
