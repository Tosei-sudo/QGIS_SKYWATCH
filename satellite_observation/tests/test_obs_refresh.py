"""
観測記録の読み直し (ObservationBook.refresh, EsriObservationStore.load_changed):
others' records read quietly and merged; a form opened on a record someone
else changed meanwhile is not saved over it; `refreshMinutes`.
"""
from dataclasses import replace

from satellite_observation.core.esri import EsriClient
from satellite_observation.core.gpkg import open_local
from satellite_observation.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs
from satellite_observation.core.obs.flows import now_ms
from satellite_observation.core.obs.model import ObservationVocabulary, observation_store_of
from satellite_observation.core.obs.stores import EsriObservationStore, LocalAreaStore, LocalTaxonomyStore
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer
from satellite_observation.tests.test_obs import ALL, DATES, URL, day, obs


def layer(names=None):
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in (names or ALL + ["CONFIDENCE", "UPDATED_BY"])}))
    return fake


def stores(fake, **config):
    c = observation_store_of({"url": URL, **config}, [], 0)
    return EsriObservationStore(c, EsriClient(fake)), EsriObservationStore(c, EsriClient(fake))


def book_of(store, tmp_path, said=None):
    db = open_local(str(tmp_path / "o.gpkg"))
    b = ObservationBook(store, LocalAreaStore(db, "x"), LocalTaxonomyStore(db, "x"), ObservationVocabulary(), observer=lambda: "田中",
                        prefs=Prefs(), say=(said if said is not None else []).append)
    b.reload()
    b.set_open_images([ObservedImage("img", "img.tif", day(1), None, "L1")], "L1")
    return b, db


def test_only_the_records_saved_since_are_asked():
    fake = layer()
    mine, _ = stores(fake)
    now = now_ms()
    mine.save([obs(id="old", updated_at=day(20)), obs(id="new", updated_at=now)])
    got = mine.load_changed(now - 60_000)
    assert [o.id for o in got] == ["new"]
    # A layer without 更新日時, or that deletes for good, cannot tell: read everything instead.
    assert stores(layer([n for n in ALL if n != "UPDATED_AT"]))[0].load_changed(now) is None
    assert stores(layer(), softDelete=False)[0].load_changed(now) is None


def test_others_records_are_merged_and_told(tmp_path):
    fake = layer()
    mine, theirs = stores(fake)
    mine.save([obs(id="a", updated_at=now_ms()), obs(id="b", updated_at=now_ms())])
    said = []
    book, db = book_of(mine, tmp_path, said)
    assert book.refresh() is None  # nothing new
    b = next(o for o in theirs.load() if o.id == "b")
    theirs.save([replace(b, note="ほかの人", updated_by="鈴木", updated_at=now_ms()), obs(id="c", updated_at=now_ms())])
    text = book.refresh()
    assert text and "2 件を反映" in text
    here = {o.id: o for o in book.records}
    assert set(here) == {"a", "b", "c"} and here["b"].note == "ほかの人"
    assert book.changed_by_others == {"b", "c"}
    assert book.refresh() is None  # read again: the same
    db.close()


def test_a_store_that_cannot_tell_is_read_whole(tmp_path):
    fake = layer([n for n in ALL if n != "UPDATED_AT"] + ["UPDATED_BY"])
    mine, theirs = stores(fake)
    mine.save([obs(id="a")])
    book, db = book_of(mine, tmp_path)
    theirs.save([obs(id="b")])
    assert "1 件を反映" in book.refresh()
    assert {o.id for o in book.records} == {"a", "b"}
    db.close()


def test_a_form_opened_before_someone_elses_change_does_not_save_over_it(tmp_path):
    fake = layer()
    mine, theirs = stores(fake)
    mine.save([obs(id="a", updated_at=now_ms())])
    said = []
    book, db = book_of(mine, tmp_path, said)
    editing = book.record("a")
    a = next(o for o in theirs.load() if o.id == "a")
    theirs.save([replace(a, note="鈴木の変更", updated_by="鈴木", updated_at=now_ms() + 5000)])
    book.refresh()
    values = FormValues(object=editing.object_name, kind=editing.kind, activity="航行", note="")
    assert book.submit(editing.lon, editing.lat, values, editing=editing) is None
    assert "鈴木 さんが更新したため保存せず" in said[-1]
    assert next(o for o in theirs.load() if o.id == "a").note == "鈴木の変更"
    # Opened again (the record as read now): saved.
    saved = book.submit(editing.lon, editing.lat, replace(values, note="鈴木の変更"), editing=book.record("a"))
    assert saved is not None and saved.activity == "航行"
    db.close()


def test_refresh_minutes():
    problems = []
    assert observation_store_of({"url": URL, "refreshMinutes": 5}, problems, 0).refresh_minutes == 5
    assert observation_store_of({"url": URL}, problems, 0).refresh_minutes is None
    assert problems == []
    assert observation_store_of({"url": URL, "refreshMinutes": 0}, problems, 0).refresh_minutes is None
    assert "refreshMinutes" in problems[0]


# ---- 保存できなかった記録 (drafts) -------------------------------------------------------------------

def test_a_draft_of_a_change_is_not_sent_over_someone_elses_later_change(tmp_path):
    from satellite_observation.core.esri import EsriError

    fake = layer()
    mine, theirs = stores(fake)
    mine.save([obs(id="a", updated_at=now_ms())])
    book, db = book_of(mine, tmp_path)
    editing = book.record("a")
    real = mine.save

    def down(_records):
        raise EsriError("接続不可")

    mine.save = down
    values = FormValues(object=editing.object_name, kind=editing.kind, activity="航行", note="私の変更")
    assert book.submit(editing.lon, editing.lat, values, editing=editing) is None
    [d] = book.drafts()
    assert d.editing_id == "a" and d.base_updated == editing.updated_at and d.values["note"] == "私の変更"
    mine.save = real
    # Someone else changes it meanwhile; read here.
    a = next(o for o in theirs.load() if o.id == "a")
    theirs.save([replace(a, note="鈴木の変更", updated_by="鈴木", updated_at=now_ms() + 5000)])
    book.reload()
    assert book.send_draft(d.id) is None
    assert "鈴木 さんが更新したため送らず" in book.drafts()[0].reason
    assert next(o for o in theirs.load() if o.id == "a").note == "鈴木の変更"
    # A new record whose answer was lost (the server holds it): sent again, it is not added twice.
    mine.save = down
    new = FormValues(object="新しいモノ", kind="船舶")
    assert book.submit(139.7, 35.6, new) is None
    kept = [x for x in book.drafts() if not x.editing][0]
    real([kept.record])  # the server took it after all
    mine.save = real
    mine2, _ = stores(fake)  # as after QGIS started again: the store knows nothing of it
    book.store = mine2
    book.reload()
    assert book.send_draft(kept.id) is not None
    assert sum(1 for r in fake.layers[URL].rows if r["attributes"]["OBJECT_NAME"] == "新しいモノ") == 1
    assert book.drop_draft(d.id) and book.drafts() == []
    db.close()
