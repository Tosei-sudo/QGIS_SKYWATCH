"""
「数えるだけの大分類」 (tsudo 2026-11-19): some departments only count what
each image shows, in a 大分類 they share with those who follow the things.
For the one counting, nothing of those 大分類 is carried over or offered as
the same one; their new records are 数える記録 (COUNT_ONLY = 1), which nobody
gets carried over or offered.
"""
from datetime import datetime, timezone

import pytest

from mikage.core.esri import EsriClient
from mikage.core.gpkg import OBSERVATIONS, open_local
from mikage.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs
from mikage.core.obs.model import Observation, ObservationVocabulary, observation_store_of
from mikage.core.obs.stores import EsriObservationStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore
from mikage.tests.fake_esri import FakeEsri, FakeLayer


def day(d: int) -> int:
    return int(datetime(2026, 11, d, 1, tzinfo=timezone.utc).timestamp() * 1000)


IMAGES = [ObservedImage("d1", "d1.tif", day(1), [139.6, 35.5, 139.8, 35.7], "L1"),
          ObservedImage("d2", "d2.tif", day(2), [139.6, 35.5, 139.8, 35.7], "L2")]


@pytest.fixture
def db(tmp_path):
    d = open_local(str(tmp_path / "o.gpkg"))
    yield d
    d.close()


def book_of(db, who: str, counted=()) -> ObservationBook:
    b = ObservationBook(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(),
                        observer=lambda: who, prefs=Prefs())
    b.counted = frozenset(counted)
    b.reload()
    b.set_open_images(IMAGES, "L1")
    return b


def test_a_counter_makes_counted_records_in_the_chosen_top_classes_only(db):
    counter = book_of(db, "田中", ["車両"])
    car = counter.submit(139.70, 35.60, FormValues(object="", kind="車両 > 乗用車"))
    ship = counter.submit(139.71, 35.61, FormValues(object="", kind="船舶"))
    assert car.count_only and not ship.count_only
    assert counter.counts("車両") and counter.counts("車両 > 乗用車") and not counter.counts("船舶") and not counter.counts("")
    # Kept in the file (and read back).
    again = book_of(db, "田中")
    assert {o.object_name: o.count_only for o in again.records} == {"乗用車": True, "船舶": False}
    # A change keeps what the record was, whoever makes it.
    follower = book_of(db, "鈴木")
    changed = follower.update(car.id, activity="駐車")
    assert changed.count_only and changed.updated_by == "鈴木"


def test_counted_records_are_never_carried_over_nor_offered_to_anyone(db):
    counter = book_of(db, "田中", ["車両"])
    counter.submit(139.70, 35.60, FormValues(object="", kind="車両"))
    follower = book_of(db, "鈴木")
    tracked = follower.submit(139.702, 35.602, FormValues(object="", kind="車両"))
    follower.step(1)
    # Only the follower's own car carries over (the counted one stays in its image).
    assert [c.source.id for c in follower.carried()] == [tracked.id]
    assert [c.source.id for c in follower.candidates(139.7001, 35.6001, "車両")] == [tracked.id]
    assert follower.absent_all() and follower.carried() == []


def test_a_counter_sees_nothing_carried_over_or_offered_in_a_counted_top_class(db):
    follower = book_of(db, "鈴木")
    car = follower.submit(139.70, 35.60, FormValues(object="", kind="車両"))
    ship = follower.submit(139.71, 35.61, FormValues(object="", kind="船舶"))
    counter = book_of(db, "田中", ["車両"])
    counter.step(1)
    # The follower's car is not the counter's to carry over or mark gone; the ship still is.
    assert [c.source.id for c in counter.carried()] == [ship.id]
    assert [c.source.id for c in counter.carried_for("d2")] == [ship.id]
    assert counter.candidates(139.7001, 35.6001, "車両") == []
    assert [c.source.id for c in counter.candidates(139.7101, 35.6101, "船舶")] == [ship.id]
    # 「すべて継続」「すべて不在」 leave the counted 大分類 alone.
    assert [o.object_id for o in counter.absent_all()] == [ship.object_id]
    # A new car is a thing of its own (not the follower's one), and counted.
    mine = counter.submit(139.7001, 35.6001, FormValues(object="", kind="車両"))
    assert mine.object_id != car.object_id and mine.count_only
    # The setting changed: the car carries over again for this person.
    counter.counted = frozenset()
    assert [c.source.id for c in counter.carried()] == [car.id]


def test_a_draft_is_stamped_as_it_would_be_saved(db):
    counter = book_of(db, "田中", ["車両"])
    record = counter.record_from(counter.current, 139.7, 35.6, None, None, FormValues(kind="車両"), True)
    assert not record.count_only
    drafted = counter._by(record)
    assert drafted.count_only and drafted.recorded_by == "田中"


def test_a_file_made_before_gets_the_column(tmp_path):
    path = str(tmp_path / "old.gpkg")
    db = open_local(path)
    db._db.execute('ALTER TABLE "observations" DROP COLUMN "COUNT_ONLY"')
    db._db.commit()
    db.close()
    db = open_local(path)
    db.ensure(OBSERVATIONS)
    store = LocalObservationStore(db)
    store.save([Observation("r1", "d1", "d1.tif", day(1), "A", "車両", "車両", "", True, "", 139.7, 35.6, day(1), day(1), count_only=True)])
    assert [o.count_only for o in store.load()] == [True]
    db.close()


FIELDS = ["OBS_ID", "IMAGE_ID", "IMAGE_NAME", "IMAGE_TIME", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE",
          "OBSERVER", "UPDATED_BY", "CREATED_AT", "UPDATED_AT", "DELETED_AT"]
TYPES = {"IMAGE_TIME": "esriFieldTypeDate", "CREATED_AT": "esriFieldTypeDate", "UPDATED_AT": "esriFieldTypeDate",
         "DELETED_AT": "esriFieldTypeDate", "PRESENT": "esriFieldTypeSmallInteger", "COUNT_ONLY": "esriFieldTypeSmallInteger"}
URL = "https://x/FeatureServer/0"


def obs(record_id: str, count_only: bool) -> Observation:
    return Observation(record_id, "d1", "d1.tif", day(1), f"O{record_id}", "車両", "車両", "", True, "", 139.7, 35.6, day(1), day(1),
                       count_only=count_only)


@pytest.mark.parametrize("with_column", [True, False])
def test_the_esri_layer_keeps_the_mark_when_it_has_the_attribute(with_column):
    fake = FakeEsri()
    names = FIELDS + (["COUNT_ONLY"] if with_column else [])
    layer = FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in names})
    fake.add_layer(URL, layer)
    store = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))
    store.save([obs("c", True), obs("f", False)])
    written = {r["attributes"]["OBS_ID"]: r["attributes"].get("COUNT_ONLY", "なし") for r in layer.rows}
    assert written == ({"c": 1, "f": None} if with_column else {"c": "なし", "f": "なし"})
    read = {o.id: o.count_only for o in store.load()}
    assert read == {"c": with_column, "f": False}
