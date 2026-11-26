"""
衛星名 on the records and checked areas (tsudo 2026-11-20: 「外部画像の存在を
考えると、観測結果テーブルにも衛星名があってもいいのかな」): where an image's
satellite comes from (core/obs/satellites.py), and the book and stores keeping it.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from mikage.core.catalog.search import listed_image, record_of
from mikage.core.esri import EsriClient
from mikage.core.gpkg import open_local
from mikage.core.obs import alerts, export
from mikage.core.obs.api import area_out, record_out
from mikage.core.obs.areas import CheckedArea, area_store_of, box_ring
from mikage.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs, joined_image
from mikage.core.obs.flows import ImageRef, carried_into
from mikage.core.obs.model import DEFAULT_OBSERVATION_FIELDS, ListedImage, Observation, ObservationVocabulary, observation_store_of, parse_listed_images, recorded_images
from mikage.core.obs.output import observation_layers
from mikage.core.obs.satellites import SatelliteChoices, clean, load_satellite_choices, satellite_in_metadata
from mikage.core.obs.stores import EsriAreaStore, EsriObservationStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore, satellite_supported
from mikage.core.plan.names import SatelliteNames
from mikage.tests.fake_esri import FakeEsri, FakeLayer


def day(d: int) -> int:
    return int(datetime(2026, 10, d, 1, tzinfo=timezone.utc).timestamp() * 1000)


CHOICES = SatelliteChoices(
    catalog=("WV03", "GE01", "Sentinel-2A", "SkySat"),
    satellites=("WorldView-3", "SkySat-C12", "SkySat-C13", "ALOS-2"),
    names=SatelliteNames.of([("WorldView-3", "40115", "WV03", "WV3"), ("SkySat-C12", "x", "SkySat"), ("SkySat-C13", "y", "SkySat")]),
)


class TestWhereItComesFrom:
    def test_the_metadata_items_surest_first(self):
        assert satellite_in_metadata({"SATELLITEID": "WV03", "NITF_ISORCE": "DigitalGlobe"}) == "WV03"
        assert satellite_in_metadata({"nitf_isorce": "  GE01 "}) == "GE01"
        assert satellite_in_metadata({"SPACECRAFT_NAME": "Sentinel-2A"}) == "Sentinel-2A"
        assert satellite_in_metadata({"MISSION_ID": "S1A", "SATELLITE_IDENTIFIER": "SENTINEL-1"}) == "S1A"
        # Saying nothing: unknown, blank, none.
        assert satellite_in_metadata({"NITF_ISORCE": "Unknown", "SATELLITEID": " "}) == ""
        assert satellite_in_metadata({}) == ""

    def test_a_sicd_or_an_image_description(self):
        sicd = "<SICD xmlns='urn:SICD:1.2.1'><CollectionInfo><CollectorName>CAPELLA-13</CollectorName></CollectionInfo></SICD>"
        assert satellite_in_metadata({}, [sicd]) == "CAPELLA-13"
        described = {"TIFFTAG_IMAGEDESCRIPTION": json.dumps({"collect": {"platform": "capella-14", "mode": "spotlight"}})}
        assert satellite_in_metadata(described) == "capella-14"
        assert satellite_in_metadata({"TIFFTAG_IMAGEDESCRIPTION": "<x><Satellite>ICEYE-X2</Satellite></x>"}) == "ICEYE-X2"
        assert satellite_in_metadata({"TIFFTAG_IMAGEDESCRIPTION": "made by a scanner"}) == ""

    def test_written_as_the_image_catalog_writes_that_satellite(self):
        assert CHOICES.spelled("WorldView-3") == "WV03"   # the alias table: one satellite
        assert CHOICES.spelled("wv-03") == "WV03"         # the same letters and digits
        assert CHOICES.spelled("SKYSAT-C12") == "SkySat"  # the catalog does not tell C12 from C13
        assert CHOICES.spelled("alos 2") == "ALOS-2"      # the satellite catalog's when the image catalog has none
        assert CHOICES.spelled("  Pleiades   1A ") == "Pleiades 1A"
        assert CHOICES.spelled("") == ""

    def test_a_word_of_the_file_name(self):
        assert CHOICES.in_file_name("20241010_WV3_P001_PAN.TIF") == "WV03"
        assert CHOICES.in_file_name("C:\\data\\WorldView-3_20241010.tif") == "WV03"
        assert CHOICES.in_file_name("ge01-2024-05-01.ntf") == "GE01"
        assert CHOICES.in_file_name("/x/sentinel-2a_T54SUE.tif") == "Sentinel-2A"
        assert CHOICES.in_file_name("skysat_c13_20240101.tif") == "SkySat"
        # Not part of a word, not a short name, not a number.
        assert CHOICES.in_file_name("NEWV03X.tif") == ""
        assert CHOICES.in_file_name("ortho_2024_0001.tif") == ""
        assert SatelliteChoices(catalog=("S1",)).in_file_name("S1_2024.tif") == ""
        # An alias of two satellites (and not the catalog's own name) says neither.
        shared = SatelliteChoices(satellites=("SkySat-C12", "SkySat-C13"), names=CHOICES.names)
        assert shared.in_file_name("skysat_20240101.tif") == ""

    def test_the_names_offered(self):
        # The catalog's first; a satellite one of them names is not offered twice.
        assert CHOICES.offered() == ["WV03", "GE01", "Sentinel-2A", "SkySat", "ALOS-2"]
        assert clean("x" * 300) == "x" * 255

    def test_read_from_the_catalogs(self):
        class Client:
            def __init__(self):
                self.asked = []

            def ask(self, url, params, token=None):
                self.asked.append((url, params["outFields"]))
                if "Catalog" in url:
                    return {"features": [{"attributes": {"SENSOR": "WV03"}}, {"attributes": {"SENSOR": "GE01"}}]}
                return {"features": [{"attributes": {"SAT_NAME": "WorldView-3"}}, {"attributes": {"SAT_NAME": None}}]}

        class Catalog:
            def __init__(self, url, fields):
                self.url, self.fields, self.token, self.where = url, fields, None, None

        client = Client()
        choices, whole = load_satellite_choices(client, [Catalog("https://x/Catalog/FeatureServer/0", {"sensor": "SENSOR"}),
                                                         Catalog("https://x/Other/FeatureServer/0", {})],
                                                [Catalog("https://x/Sats/FeatureServer/0", {"name": "SAT_NAME"})], CHOICES.names)
        assert whole and choices.catalog == ("GE01", "WV03") and choices.satellites == ("WorldView-3",)
        assert [u for u, _f in client.asked] == ["https://x/Catalog/FeatureServer/0/query", "https://x/Sats/FeatureServer/0/query"]


@pytest.fixture
def book(tmp_path):
    db = open_local(str(tmp_path / "o.gpkg"))
    said: list[str] = []
    b = ObservationBook(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(),
                        observer=lambda: "田中", prefs=Prefs(), say=said.append)
    b.said = said
    b.reload()
    b.set_open_images([ObservedImage("d1", "d1.tif", day(1), [139.6, 35.5, 139.8, 35.7], "L1", satellite="WV03", satellite_origin="catalog"),
                       ObservedImage("ext.tif", "ext.tif", day(3), [139.6, 35.5, 139.8, 35.7], "L3")], "L1")
    b.db = db
    yield b
    db.close()


class TestTheBook:
    def test_a_new_record_and_area_take_the_images_satellite(self, book):
        rec = book.submit(139.7, 35.6, FormValues(object="あおば", kind="船舶"))
        assert rec.satellite == "WV03"
        area = book.add_area(box_ring([139.65, 35.55, 139.75, 35.65]), "船舶")
        assert area.satellite == "WV03"
        # Kept in the store, read back.
        assert LocalObservationStore(book.db).load()[0].satellite == "WV03"
        assert LocalAreaStore(book.db, "ローカル").load()[0].satellite == "WV03"
        # Carried into an image that does not say: none (not the other image's).
        book.go_to("ext.tif")
        carried = book.carry(rec.object_id)
        assert carried.image_id == "ext.tif" and carried.satellite == ""

    def test_typed_for_an_image_that_does_not_say(self, book):
        book.go_to("ext.tif")
        a = book.submit(139.7, 35.6, FormValues(object="", kind="船舶"))
        b = book.submit(139.71, 35.61, FormValues(object="", kind="車両"))
        area = book.add_area(box_ring([139.65, 35.55, 139.75, 35.65]))
        assert (a.satellite, b.satellite, area.satellite) == ("", "", "")
        assert book.keeps_satellite()
        changed = book.set_image_satellite("  GE01 ")
        assert sorted(o.id for o in changed) == sorted([a.id, b.id])
        assert book.current.satellite == "GE01" and book.current.satellite_origin == "manual"
        assert {o.satellite for o in book.here()} == {"GE01"} and book.image_areas()[0].satellite == "GE01"
        assert {o.satellite for o in LocalObservationStore(book.db).load()} == {"GE01"}
        assert LocalAreaStore(book.db, "ローカル").load()[0].satellite == "GE01"
        assert book.said[-1] == "2 件の記録に衛星名「GE01」を入力"
        # The next one takes it; one that has a name keeps it (typed again: only those without).
        c = book.submit(139.72, 35.62, FormValues(object="", kind="船舶"))
        assert c.satellite == "GE01"
        book._save_records([replace(book.record(c.id), satellite="WV03")], "")
        assert book.set_image_satellite("ALOS-2") == [] and book.record(c.id).satellite == "WV03"
        # An image that says its own: the typed one does not replace it.
        book.manual_satellites["d1"] = "X"
        book._order()
        assert book._image("d1").satellite == "WV03"

    def test_a_record_without_one_gets_it_when_saved_again(self, book):
        old = Observation(id="old", image_id="d1", image_name="d1.tif", image_time=day(1), object_id="A", object_name="A", kind="船舶",
                          activity="停泊", present=True, note="", lon=139.7, lat=35.6, created_at=day(2), updated_at=day(2))
        elsewhere = replace(old, id="far", image_id="closed.tif")
        LocalObservationStore(book.db).save([old, elsewhere])
        book.reload()
        assert book.record("old").satellite == ""
        book.update("old", note="見直し")
        book.update("far", note="見直し")
        # Its image is open and says it: filled; an image not open: left empty.
        assert book.record("old").satellite == "WV03" and book.record("far").satellite == ""

    def test_listed_images_keep_it(self, book):
        record = record_of({"attributes": {"ID": "IMG-1", "SENSOR": "GE01", "AT": day(2)}}, {"id": "ID", "sensor": "SENSOR", "acquired": "AT"})
        listed = listed_image(record)
        assert listed.satellite == "GE01"
        book.list_images([listed])
        assert book.current.id == "IMG-1" and book.current.satellite == "GE01" and not book.current.file
        rec = book.submit(139.7, 35.6, FormValues(object="", kind="船舶"))
        assert rec.satellite == "GE01"
        # Kept with the list (and an older list without it reads as none).
        again = parse_listed_images(book.prefs.get(book._listed_key))
        assert [(i.id, i.satellite) for i in again] == [("IMG-1", "GE01")]
        assert parse_listed_images(json.dumps([{"id": "x", "name": "x"}]))[0].satellite == ""
        # One typed with ＋ and given a name later: the list keeps it.
        book.list_image("手入力", "手入力")
        book.set_image_satellite("WV03")
        assert next(i for i in book.listed if i.id == "手入力").satellite == "WV03"
        # The images the records name, for picking again.
        assert {i.id: i.satellite for i in recorded_images(book.records)}["IMG-1"] == "GE01"

    def test_one_image_of_several_layers_and_the_image_ref(self):
        parts = [ObservedImage("R", "a.tif", None, None, "T1"), ObservedImage("R", "b.tif", None, None, "T2", satellite="WV03", satellite_origin="catalog")]
        joined = joined_image(parts, "R")
        assert (joined.satellite, joined.satellite_origin) == ("WV03", "catalog")
        assert joined.ref == ImageRef("R", "R", None, "WV03")
        o = Observation(id="o", image_id="R", image_name="R", image_time=None, object_id="A", object_name="A", kind="船舶", activity="",
                        present=True, note="", lon=0, lat=0, created_at=0, updated_at=0, satellite="GE01")
        assert carried_into(o, ImageRef("S", "S", None, "WV03")).satellite == "WV03"


class TestEsri:
    URL = "https://x/FeatureServer/0"

    def store(self, satellite=True):
        fake = FakeEsri()
        names = ["OBS_ID", "IMAGE_ID", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "CREATED_AT", "UPDATED_AT", "DELETED_AT",
                 *(["SATELLITE"] if satellite else [])]
        types = {n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeSmallInteger" if n == "PRESENT" else "esriFieldTypeString"
                 for n in names}
        layer = fake.add_layer(self.URL, FakeLayer(types))
        config = observation_store_of({"url": self.URL}, [], 0)
        return fake, layer, config

    def record(self, **o):
        base = dict(id="r1", image_id="d1", image_name="d1.tif", image_time=day(1), object_id="A", object_name="A", kind="船舶",
                    activity="停泊", present=True, note="", lon=139.7, lat=35.6, created_at=day(20), updated_at=day(20), satellite="WV03")
        base.update(o)
        return Observation(**base)

    def test_saved_and_read_back(self):
        fake, layer, config = self.store()
        assert DEFAULT_OBSERVATION_FIELDS["satellite"] == "SATELLITE"
        store = EsriObservationStore(config, EsriClient(fake))
        assert not satellite_supported(store)  # nothing asked yet
        store.save([self.record()])
        assert layer.rows[0]["attributes"]["SATELLITE"] == "WV03" and satellite_supported(store)
        [read] = EsriObservationStore(config, EsriClient(fake)).load()
        assert read.satellite == "WV03"
        store.save([self.record(id="r2", satellite="")])
        assert layer.rows[1]["attributes"]["SATELLITE"] is None

    def test_a_layer_without_it(self):
        fake, layer, config = self.store(satellite=False)
        store = EsriObservationStore(config, EsriClient(fake))
        store.save([self.record()])
        assert "SATELLITE" not in layer.rows[0]["attributes"] and not store.keeps_satellite()
        # As kept: none (so a record read back is the one saved, not someone else's change).
        assert store.as_kept([self.record()])[0].satellite == ""

    def test_checked_areas(self):
        fake = FakeEsri()
        url = "https://x/FeatureServer/2"
        names = ("AREA_ID", "IMAGE_ID", "KIND", "NOTE", "CREATED_AT", "UPDATED_AT", "DELETED_AT")
        with_it = fake.add_layer(url, FakeLayer({n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeString"
                                                 for n in (*names, "SATELLITE")}, "esriGeometryPolygon"))
        ring = box_ring([139.6, 35.5, 139.7, 35.6])
        store = EsriAreaStore("確認範囲", area_store_of(url, [], "a"), EsriClient(fake))
        store.save([CheckedArea("n1", "d1", "d1.tif", day(1), ring, "", "", day(20), day(20), satellite="WV03")])
        assert with_it.rows[0]["attributes"]["SATELLITE"] == "WV03" and store.keeps_satellite()
        assert EsriAreaStore("確認範囲", area_store_of(url, [], "a"), EsriClient(fake)).load()[0].satellite == "WV03"
        old = "https://x/FeatureServer/3"
        fake.add_layer(old, FakeLayer({n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeString" for n in names}, "esriGeometryPolygon"))
        store = EsriAreaStore("確認範囲", area_store_of(old, [], "a"), EsriClient(fake))
        area = CheckedArea("n2", "d1", "d1.tif", day(1), ring, "", "", day(20), day(20), satellite="WV03")
        store.save([area])
        assert not store.keeps_satellite() and store.as_kept([area])[0].satellite == ""


def test_outputs_say_it():
    o = Observation(id="o", image_id="d1", image_name="d1.tif", image_time=day(1), object_id="A", object_name="A", kind="船舶",
                    activity="停泊", present=True, note="", lon=139.7, lat=35.6, created_at=day(2), updated_at=day(2), satellite="WV03")
    area = CheckedArea("n1", "d1", "d1.tif", day(1), box_ring([139.6, 35.5, 139.8, 35.7]), "", "", day(2), day(2), satellite="WV03")
    assert record_out(o)["satellite"] == "WV03" and area_out(area)["satellite"] == "WV03"
    assert "satellite" in export.RECORD_SOURCES and ("SATELLITE", "satellite") in export.STANDARD_COLUMNS
    assert "satellite" in alerts.SOURCES
    layers = {layer.title: layer for layer in observation_layers([o], [area])}
    point = layers["観測記録"]
    assert any(f.name == "satellite" and f.alias == "衛星名" for f in point.fields) and point.features[0][1]["satellite"] == "WV03"
    assert layers["確認範囲"].features[0][1]["satellite"] == "WV03"
    assert ListedImage("x", "x", satellite="WV03").to_json()["satellite"] == "WV03" and "satellite" not in ListedImage("x", "x").to_json()
