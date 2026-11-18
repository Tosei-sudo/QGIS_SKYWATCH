"""
Observation records read as needed, not whole (2026-11-18: 200 people; tsudo
「本当に観測記録全件取る意味あるの？」): the query a feature asks (scope.py),
what an Esri store reads for it and around an image, the book that reads
only that, and the naming rule that came with it (only individuals are
told apart by name).
"""
from dataclasses import replace

import pytest

from mikage.core.esri import EsriClient, EsriError
from mikage.core.gpkg import open_local
from mikage.core.obs.book import ObservationBook, ObservedImage, Prefs
from mikage.core.obs.model import ObservationVocabulary, observation_store_of
from mikage.core.obs.scope import IN_CHUNK, RecordQuery, grown, merged
from mikage.core.obs.stores import EsriObservationStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore
from mikage.core.obs.taxonomy import Taxon
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_obs import ALL, DATES, URL, day, obs

FIELDS = observation_store_of({"url": URL}, [], 0).fields
TYPES = {n: DATES.get(n, "esriFieldTypeString") for n in ALL + ["OBSERVER"]}
#: The image's footprint (Tokyo Bay) and one far from it.
BAY = [139.6, 35.5, 139.8, 35.7]
FAR = (141.0, 38.0)


# ---- the query -----------------------------------------------------------------


def test_a_query_is_written_as_the_layers_where():
    q = RecordQuery(ids=("a", "b"), names=("あおば",), kinds=("船舶",), start=day(1), end=day(3), observers=(" tanaka ",), living=True)
    [by_id, by_name] = q.wheres(FIELDS, TYPES, base="SITE = 'x'")
    ands = ("(SITE = 'x') AND DELETED_AT IS NULL AND (KIND = '船舶' OR KIND LIKE '船舶 > %' ESCAPE '\\') AND "
            "((IMAGE_TIME >= TIMESTAMP '2026-10-01 01:00:00' AND IMAGE_TIME <= TIMESTAMP '2026-10-03 01:00:00') OR "
            "(IMAGE_TIME IS NULL AND CREATED_AT >= TIMESTAMP '2026-10-01 01:00:00' AND CREATED_AT <= TIMESTAMP '2026-10-03 01:00:00')) AND "
            "UPPER(OBSERVER) IN ('TANAKA')")
    assert by_id == f"{ands} AND OBS_ID IN ('a','b')" and by_name == f"{ands} AND OBJECT_NAME IN ('あおば')"
    # A long list is asked in parts; a period of when made asks that field alone.
    many = RecordQuery(image_ids=tuple(f"i{n}" for n in range(IN_CHUNK + 1)), basis="created", start=day(2))
    assert [w.count(",") for w in many.wheres(FIELDS, TYPES)] == [IN_CHUNK - 1, 0]
    assert many.wheres(FIELDS, TYPES)[1] == "CREATED_AT >= TIMESTAMP '2026-10-02 01:00:00' AND IMAGE_ID IN ('i100')"
    # A part the layer has no attribute for is left to `matches`.
    assert RecordQuery(observers=("x",), living=True).wheres(FIELDS, {"OBS_ID": "esriFieldTypeString"}) == ["1=1"]


def test_a_query_tests_a_record_the_same_way():
    o = obs(id="r", object_id="T", object_name="あおば", kind="船舶 > 護衛艦", image_time=day(2), recorded_by="Tanaka")
    assert RecordQuery(names=("x",), ids=("r",)).matches(o)
    assert RecordQuery(kinds=("船舶",), start=day(2), end=day(2), box=(139, 35, 140, 36), observers=("TANAKA",)).matches(o)
    assert not RecordQuery(kinds=("船舶 > 潜水艦",)).matches(o)
    assert not RecordQuery(kinds=("船",)).matches(o)  # a class, not the start of a word
    assert not RecordQuery(start=day(3)).matches(o) and not RecordQuery(box=(140, 35, 141, 36)).matches(o)
    assert not RecordQuery(ids=("r",), living=True).matches(replace(o, deleted_at=day(4)))
    # Untimed: by when it was made.
    assert RecordQuery(start=day(20)).matches(replace(o, image_time=None))
    assert RecordQuery().everything() and RecordQuery(living=True).everything() and not RecordQuery(kinds=("船舶",)).everything()
    assert [x.id for x in merged([obs(id="a", note="1"), obs(id="b")], [obs(id="a", note="2"), obs(id="c")])] == ["a", "b", "c"]
    w, s, e, n = grown(BAY, 1000)
    assert w == pytest.approx(139.6 - 0.01106, abs=1e-4) and n == pytest.approx(35.7 + 1000 / 111_320)


# ---- an Esri store ---------------------------------------------------------------


def esri(ordered=True):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer(dict(TYPES), ordered=ordered))
    return fake, layer, EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))


def queries(fake):
    return [form for url, form in fake.asked if url == URL + "/query" and form.get("returnCountOnly") != "true"]


def test_an_esri_store_finds_what_is_asked_and_refuses_everything():
    fake, _layer, store = esri()
    store.save([obs(id="a", kind="船舶 > 護衛艦", object_name="あおば"), obs(id="b", kind="航空機", lon=FAR[0], lat=FAR[1]),
                obs(id="c", kind="船舶", deleted_at=day(5))])
    with pytest.raises(EsriError, match="全件は読まない"):
        store.find(RecordQuery(living=True))
    assert [o.id for o in store.find(RecordQuery(kinds=("船舶",)))] == ["a", "c"]
    assert [o.id for o in store.find(RecordQuery(kinds=("船舶",), living=True))] == ["a"]
    assert [o.id for o in store.find(RecordQuery(box=(140.5, 37.5, 141.5, 38.5)))] == ["b"]
    assert sorted(o.id for o in store.find(RecordQuery(names=("あおば",), ids=("b",)))) == ["a", "b"]
    # The box is the query's envelope (the server's spatial index), not read whole and tested here.
    assert '"xmin":140.5' in queries(fake)[-3]["geometry"]


def test_around_an_image_its_records_the_latest_near_it_and_their_things_then():
    fake, layer, store = esri()
    store.save([
        obs(id="here", image_id="d5", image_time=day(5), object_id="H"),
        # Near the image: before (the latest two kept), and after.
        obs(id="old", image_id="d1", image_time=day(1), object_id="OLD"),
        obs(id="b3", image_id="d3", image_time=day(3), object_id="T"),
        obs(id="b4", image_id="d4", image_time=day(4), object_id="U"),
        obs(id="a6", image_id="d6", image_time=day(6), object_id="V"),
        # T's record far away at day 3.5 (in the stretch read): kept; its record of day 1 (before it): not.
        obs(id="t-far", image_id="f", image_time=day(3) + 3_600_000, object_id="T", lon=FAR[0], lat=FAR[1]),
        obs(id="t-old", image_id="f0", image_time=day(1), object_id="T", lon=FAR[0], lat=FAR[1]),
        # Far and of no thing near: not read.
        obs(id="other", image_id="f", image_time=day(4), object_id="X", lon=FAR[0], lat=FAR[1]),
    ])
    got = store.load_around([("d5", tuple(BAY), day(5))], before=3, after=1)
    assert sorted(o.id for o in got) == ["a6", "b3", "b4", "here", "t-far"]
    # Each part asked of the layer: none reads the whole table.
    assert all(form["where"] != "1=1" or form.get("geometry") for form in queries(fake))
    # Without a footprint: only the image's own.
    assert [o.id for o in store.load_around([("d5", None, day(5))])] == ["here"]


# ---- the book ----------------------------------------------------------------------


def book_of(store, tmp_path, said, taxa=None, images=None):
    db = open_local(str(tmp_path / "o.gpkg"))
    classes = LocalTaxonomyStore(db, "x")
    if taxa:
        classes.save(taxa)
    b = ObservationBook(store, LocalAreaStore(db, "x"), classes, ObservationVocabulary(), observer=lambda: "田中", prefs=Prefs(),
                        say=said.append)
    b.set_open_images(images or [], None)
    return b


IMAGES = [ObservedImage("d5", "d5.tif", day(5), list(BAY), "L5"), ObservedImage("f", "f.tif", day(4), [140.9, 37.9, 141.1, 38.1], "LF")]


def test_a_shared_book_reads_around_the_image_and_asks_for_the_rest(tmp_path):
    fake, layer, store = esri()
    store.save([obs(id="here", image_id="d5", image_time=day(5), object_id="H"),
                obs(id="near", image_id="d4", image_time=day(4), object_id="N"),
                obs(id="far", image_id="f", image_time=day(4), object_id="F", lon=FAR[0], lat=FAR[1])])
    said = []
    # No image yet: nothing read but the classification.
    book = book_of(store, tmp_path, said)
    assert book.scoped and book.reload().endswith("画像を選ぶと、その画像とまわりの記録を読み込む") and book.records == []
    book.set_open_images(IMAGES, "L5")
    assert book.current.id == "d5" and book.needs_around()
    book.reload()
    assert sorted(o.id for o in book.records) == ["here", "near"] and said[-1].endswith("この画像 1 件、まわりの記録 1 件")
    assert not book.needs_around()
    # Another image: its surroundings added to those read.
    book.go_to("f")
    assert book.needs_around() and book.read_around().endswith("この画像 1 件、まわりの記録 0 件")
    assert sorted(o.id for o in book.records) == ["far", "here", "near"] and book.read_around() is None
    # What a feature asks is merged in too.
    store.save([obs(id="new", image_id="z", image_time=day(9), object_id="Z", kind="航空機", lon=FAR[0] + 1, lat=FAR[1])])
    assert [o.id for o in book.fetch(RecordQuery(kinds=("航空機",)))] == ["new"] and "new" in {o.id for o in book.records}
    # A failed read is not asked again on every render.
    book.set_open_images([*IMAGES, ObservedImage("q", "q.tif", day(7), [130, 30, 130.1, 30.1], "LQ")])
    assert book.go_to("q")
    fake.layers.pop(URL)
    assert book.read_around().startswith(f"{store.label} から読み込み失敗") and not book.needs_around()


def test_a_refresh_keeps_only_what_belongs_with_the_records_read(tmp_path):
    fake, layer, store = esri()
    store.save([obs(id="here", image_id="d5", image_time=day(5), object_id="H", updated_at=day(5)),
                # H's record before the one near the image: not needed (its nearest in time is that one).
                obs(id="h-far", image_id="f", image_time=day(2), object_id="H", lon=FAR[0], lat=FAR[1], updated_at=day(5))])
    said = []
    book = book_of(store, tmp_path, said, images=IMAGES[:1])
    book.reload()
    assert [o.id for o in book.records] == ["here"]
    theirs = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))
    now = book._read_at
    theirs.save([obs(id="near-new", image_id="d6", image_time=day(6), object_id="N", lon=139.65, lat=35.65, updated_at=now),
                 obs(id="far-new", image_id="g", image_time=day(6), object_id="G", lon=FAR[0], lat=FAR[1], updated_at=now),
                 obs(id="h-new", image_id="g", image_time=day(6), object_id="H", lon=FAR[0], lat=FAR[1], updated_at=now)])
    # Saved by someone else: near the image or of a thing read are kept; elsewhere of another thing, not.
    book.refresh()
    assert sorted(o.id for o in book.records) == ["h-new", "here", "near-new"]


def test_a_local_book_still_reads_everything(tmp_path):
    db = open_local(str(tmp_path / "l.gpkg"))
    store = LocalObservationStore(db)
    store.save([obs(id="a", image_id="x"), obs(id="b", image_id="y", kind="航空機")])
    book = book_of(store, tmp_path, [])
    book.reload()
    assert not book.scoped and sorted(o.id for o in book.records) == ["a", "b"]
    assert [o.id for o in book.fetch(RecordQuery(kinds=("航空機",)))] == ["b"] and not book.needs_around()
    db.close()


# ---- names: only individuals are told apart by them (tsudo 2026-11-18 「それでいいよ」) ----------------


TAXA = [Taxon(["船舶"]), Taxon(["船舶", "護衛艦"], name_label="艦名"), Taxon(["車両"])]


def test_only_an_individuals_name_is_its_thing(tmp_path):
    db = open_local(str(tmp_path / "l.gpkg"))
    book = book_of(LocalObservationStore(db), tmp_path, [], TAXA, IMAGES[:1])
    book.reload()
    assert book.individual("船舶 > 護衛艦") and not book.individual("車両") and not book.individual("")
    assert book.default_name("船舶 > 護衛艦") == "護衛艦" and book.default_name("車両") == "車両" and book.default_name("") == "モノ"
    a = book.add(139.70, 35.60, object="あおば", kind="船舶 > 護衛艦")
    b = book.add(139.71, 35.60, object="あおば", kind="船舶 > 護衛艦")
    assert a.object_id == b.object_id
    # Not individuals: the class's name, each a thing of its own, even typed the same.
    c = book.add(139.72, 35.60, kind="車両")
    d = book.add(139.73, 35.60, kind="車両")
    e = book.add(139.74, 35.60, object="白いバン", kind="車両")
    f = book.add(139.75, 35.60, object="白いバン", kind="車両")
    assert (c.object_name, d.object_name) == ("車両", "車両") and len({c.object_id, d.object_id, e.object_id, f.object_id}) == 4
    # An individual's class name is not its name either.
    g = book.add(139.76, 35.60, kind="船舶 > 護衛艦")
    h = book.add(139.77, 35.60, kind="船舶 > 護衛艦")
    assert g.object_name == h.object_name == "護衛艦" and g.object_id != h.object_id
    assert book.flow_by_name("あおば").object_id == a.object_id and book.flow_by_name("車両") is None
    assert book.flow_by_name(c.object_id).object_id == c.object_id
    db.close()


def test_an_individual_not_read_yet_is_asked_of_a_shared_store(tmp_path):
    fake, layer, store = esri()
    store.save([obs(id="far", image_id="f", image_time=day(2), object_id="AOBA", object_name="あおば", kind="船舶 > 護衛艦",
                    lon=FAR[0], lat=FAR[1])])
    book = book_of(store, tmp_path, [], TAXA, IMAGES[:1])
    book.reload()
    assert book.records == [] and book.flow_by_name("あおば") is None
    assert book.flow_by_name("あおば", "船舶 > 護衛艦", ask=True).object_id == "AOBA"
    # Typed again on this image: the same thing (asked of the store when saved).
    book.records = []
    o = book.add(139.70, 35.60, object="あおば", kind="船舶 > 護衛艦")
    assert o.object_id == "AOBA"
    # A class's name is not asked for.
    asked = len(queries(fake))
    assert book.flow_by_name("車両", "車両", ask=True) is None and len(queries(fake)) == asked


def test_what_waits_offline_is_found_when_asked_for(tmp_path):
    from mikage.core.obs.offline_stores import OfflineObservationStore
    from mikage.core.offline import OfflineWork
    from mikage.tests.test_offline import Switch, client_of

    fake, _layer, _ = esri()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    store = OfflineObservationStore(EsriObservationStore(observation_store_of({"label": "共有", "url": URL}, [], 0), client_of(work, switch)), work)
    assert store.scoped
    store.save([obs(id="a", kind="航空機")])
    planes = RecordQuery(kinds=("航空機",))
    assert [o.id for o in store.find(planes)] == ["a"]
    switch.down = True
    store.save([obs(id="b", kind="航空機"), obs(id="c", kind="船舶")])
    assert store.pending_ids() == {"b", "c"}
    # The cache's answer with what waits that the query asks for (not the 船舶).
    assert sorted(o.id for o in store.find(planes)) == ["a", "b"]
