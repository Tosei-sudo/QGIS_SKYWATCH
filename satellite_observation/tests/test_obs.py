"""
The observations' core (the viewer's observations.test.ts,
observation-extras.test.ts and taxonomy.test.ts): flows, carrying over,
candidates of the same thing, two points, CSV, the stores (a fake Esri
server and the local GeoPackage), checked areas and the report.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone
from itertools import count

import pytest

from satellite_observation.core.esri import EsriClient, EsriError
from satellite_observation.core.gpkg import open_local
from satellite_observation.core.obs.areas import (
    CheckedArea, area_contains, area_store_of, box_ring, covering_areas,
)
from satellite_observation.core.obs.csv import observations_csv
from satellite_observation.core.obs.flows import (
    ImageRef, carried_into, carry_overs, ends_of, flows, heading_text, match_candidates, next_name, two_point,
)
from satellite_observation.core.obs.geo import bearing
from satellite_observation.core.obs.model import (
    ConfidenceLevel, ListedImage, Observation, observation_store_of, parse_listed_images, recorded_images, vocabulary_of,
)
from satellite_observation.core.obs.report import ReportContext, ReportOptions, report_file_name, report_html, select_for_report
from satellite_observation.core.obs.stores import (
    EsriAreaStore, EsriObservationStore, EsriTaxonomyStore, LocalAreaStore, LocalObservationStore, LocalPhotoStore,
    LocalTaxonomyStore,
)
from satellite_observation.core.obs.taxonomy import (
    SpecField, Taxon, TaxonomyTableConfig, class_by_name, find, flatten, kind_of, nest, photos_at, place_taxon, specs_at,
    update_taxon,
)
from satellite_observation.tests.fake_esri import FakeEsri, FakeLayer


def day(d: int) -> int:
    return int(datetime(2026, 10, d, 1, tzinfo=timezone.utc).timestamp() * 1000)


_n = count(1)


def obs(**o) -> Observation:
    base = dict(id=f"o{next(_n)}", image_id="img", image_name="img.tif", image_time=day(1), object_id="A", object_name="船舶-1",
                kind="船舶", activity="停泊", present=True, note="", lon=139.7, lat=35.6, created_at=day(20), updated_at=day(20),
                deleted_at=None)
    base.update(o)
    return Observation(**base)


class TestFlows:
    def test_orders_a_thing_by_image_time_and_splits_it_where_it_changed(self):
        # Recorded newest first: the flow is still in image-time order.
        records = [
            obs(image_id="d5", image_time=day(5), created_at=day(20), activity="航行", lon=139.8),
            obs(image_id="d1", image_time=day(1), created_at=day(21)),
            obs(image_id="d3", image_time=day(3), created_at=day(22)),
            obs(image_id="d7", image_time=day(7), created_at=day(23), present=False, activity=""),
            obs(image_id="d2", image_time=day(2), created_at=day(24), deleted_at=day(25)),
        ]
        [flow] = flows(records)
        assert [o.image_id for o in flow.observations] == ["d1", "d3", "d5", "d7"]
        assert flow.first == day(1) and flow.last == day(7)
        assert flow.seen == 3
        assert flow.present is False
        assert [(s.activity, s.present, s.count) for s in flow.steps] == [("停泊", True, 2), ("航行", True, 1), ("", False, 1)]
        # 0.1° of longitude at 35.6°N is about 9 km.
        assert flow.steps[1].moved > 8000
        assert flow.distance > 8000

    def test_starts_a_new_step_where_a_thing_moved_even_doing_the_same(self):
        [flow] = flows([obs(image_time=day(1), image_id="a"), obs(image_time=day(2), image_id="b", lon=139.71)])
        assert len(flow.steps) == 2

    def test_keeps_a_thing_as_deep_as_any_record_knows_it(self):
        [flow] = flows([obs(image_time=1, kind="船舶 > 支援船 > 〇〇級"), obs(image_time=2, kind="船舶")])
        assert flow.kind == "船舶 > 支援船 > 〇〇級"
        # A later record that says another class wins.
        [moved] = flows([obs(image_time=1, kind="船舶 > 支援船 > 〇〇級"), obs(image_time=2, kind="船舶 > 護衛艦")])
        assert moved.kind == "船舶 > 護衛艦"


class TestCarryOvers:
    records = [
        obs(object_id="A", object_name="A", image_id="d1", image_time=day(1)),
        obs(object_id="A", object_name="A", image_id="d3", image_time=day(3), activity="航行"),
        obs(object_id="B", object_name="B", image_id="d1", image_time=day(1)),
        obs(object_id="B", object_name="B", image_id="d2", image_time=day(2), present=False),
        obs(object_id="C", object_name="C", image_id="d9", image_time=day(9), lon=140.5),
    ]

    def test_offers_each_thing_by_its_nearest_earlier_record_else_a_later_one_unless_it_had_gone(self):
        at4 = carry_overs(self.records, ImageRef("d4", time=day(4)))
        assert [(c.source.object_id, c.side, c.source.activity) for c in at4] == [("A", "before", "航行"), ("C", "after", "停泊")]
        # An image between: A's day-1 record, and B still there then.
        assert [c.source.object_id for c in carry_overs(self.records, ImageRef("x", time=day(1) + 3600000))] == ["A", "B", "C"]

    def test_leaves_out_what_is_already_recorded_in_the_image_and_what_is_outside_it(self):
        assert [c.source.object_id for c in carry_overs(self.records, ImageRef("d3", time=day(3)))] == ["C"]
        assert [c.source.object_id for c in carry_overs(self.records, ImageRef("d4", time=day(4)), lambda lon, lat: lon < 140)] == ["A"]

    def test_copies_a_carried_thing_into_an_image_with_fresh_ids_and_times(self):
        copy = carried_into(self.records[1], ImageRef("d4", "d4.tif", day(4)), day(30))
        assert (copy.object_id, copy.image_id, copy.image_name, copy.image_time, copy.created_at, copy.updated_at, copy.deleted_at, copy.activity) == \
            ("A", "d4", "d4.tif", day(4), day(30), day(30), None, "航行")
        assert copy.id != self.records[1].id


class TestNamesCsvAndConfig:
    def test_numbers_new_things_by_kind(self):
        assert next_name("船舶", ["船舶-1", "船舶-3", "車両-9"]) == "船舶-4"
        assert next_name("", []) == "モノ-1"
        assert next_name("船舶 > 支援船 > 〇〇級", ["〇〇級-1"]) == "〇〇級-2"

    def test_writes_csv_with_a_bom_quoted_where_needed(self):
        text = observations_csv([obs(note='a, "b"')])
        assert text.startswith("﻿観測ID,モノID,モノ")
        assert '"a, ""b"""' in text

    def test_writes_a_csv_column_per_level(self):
        text = observations_csv([obs(kind="船舶 > 支援船 > 〇〇級"), obs(kind="航空機")])
        assert "種別,分類1,分類2,分類3,活動" in text.split("\r\n")[0]
        assert "船舶 > 支援船 > 〇〇級,船舶,支援船,〇〇級,停泊" in text
        assert "航空機,航空機,,,停泊" in text

    def test_writes_length_heading_confidence_and_who_recorded(self):
        text = observations_csv([obs(length=152.25, heading=45.04), obs()])
        assert "経度,緯度,長さ（m）,向き（度）,備考" in text.split("\r\n")[0]
        assert ",152.3,45.0," in text
        head, row = observations_csv([obs(confidence="推定", recorded_by="tanaka", updated_by="sato")])[1:].strip().split("\r\n")
        assert "有無,確度,撮像日時" in head and "備考,記録者,更新者,登録日時" in head
        assert ",あり,推定," in row and ",tanaka,sato," in row

    def test_reads_observation_stores_and_the_vocabulary(self):
        problems: list[str] = []
        store = observation_store_of({"label": "観測", "url": "https://x/FeatureServer/2", "fields": {"kind": "TYPE", "bogus": "X"}, "softDelete": False,
                                      "activities": ["入港", ""]}, problems, 0)
        assert store.fields["kind"] == "TYPE" and store.fields["id"] == "OBS_ID"
        assert store.soft_delete is False
        assert store.activities == ["入港"]
        assert problems == ["observationStores[0] の fields.bogus は使用不可の役割"]
        assert observation_store_of({"url": "https://x/MapServer/2"}, problems, 1) is None
        assert vocabulary_of({"kinds": ["艦艇"]}, problems).kinds == ["艦艇"]


class TestTwoPoints:
    def test_measures_a_thing_from_its_front_and_rear_and_gives_its_ends_back(self):
        # About 200 m apart, the front to the north-east of the rear.
        rear = [139.7, 35.6]
        front = [139.7 + 0.001556, 35.6 + 0.001272]
        m = two_point(front, rear)
        assert 195 < m.length < 205
        assert 40 < m.heading < 50
        assert m.lon == pytest.approx(139.700778, abs=5e-6)
        assert m.lat == pytest.approx(35.600636, abs=5e-6)
        ends = ends_of(m.lon, m.lat, m.length, m.heading)
        assert ends[0] == pytest.approx(front, abs=5e-7)
        assert ends[1] == pytest.approx(rear, abs=5e-7)
        assert ends_of(0, 0, None, None) is None
        assert bearing([0, 0], [-1, 0]) == pytest.approx(270, abs=1e-6)
        assert heading_text(5.4) == "005°"


class TestImagesWithoutTheirFile:
    def test_lists_the_images_the_records_name_newest_first(self):
        listed = recorded_images([
            obs(image_id="EXT-1", image_name="", image_time=None),
            obs(image_id="EXT-1", image_name="", image_time=day(2)),
            obs(image_id="EXT-2", image_name="外部 2", image_time=day(5)),
            obs(image_id="EXT-3", image_time=day(9), deleted_at=day(10)),
        ])
        assert listed == [ListedImage("EXT-2", "外部 2", day(5), None), ListedImage("EXT-1", "EXT-1", day(2), None)]

    def test_reads_the_kept_list_back_leaving_bad_entries_out(self):
        assert parse_listed_images(None) == []
        assert parse_listed_images("{") == []
        assert parse_listed_images('{"id":"x"}') == []
        text = json.dumps([{"id": "A", "name": "", "time": 5, "box": [1, 2, 3, 4]}, {"id": "A"}, {"id": " "}, {"name": "no id"}, {"id": "B", "time": "x", "box": [1, 2]}])
        assert parse_listed_images(text) == [ListedImage("A", "A", 5, [1, 2, 3, 4]), ListedImage("B", "B", None, None)]


class TestCandidates:
    # About 111 m per 0.001° of latitude.
    records = [
        obs(object_id="A", object_name="タンカーA", kind="船舶 > タンカー", image_id="d1", lat=35.6, length=180, heading=90),
        obs(object_id="B", object_name="漁船B", kind="船舶 > 漁船", image_id="d1", lat=35.6005, heading=270),
        obs(object_id="C", object_name="F-15", kind="航空機", image_id="d1", lat=35.6002),
        obs(object_id="D", object_name="遠い船", image_id="d1", lat=35.7),
        # Already recorded in this image: not offered.
        obs(object_id="E", object_name="もうある", image_id="d3", image_time=day(3)),
        obs(object_id="E", object_name="もうある", image_id="d1", lat=35.6001),
    ]
    image = ImageRef("d3", time=day(3))

    def test_offers_the_near_things_not_in_this_image_likeliest_first(self):
        found = match_candidates(self.records, self.image, 139.7, 35.6003, kind="船舶 > タンカー", length=175, heading=92)
        assert [c.name for c in found] == ["タンカーA", "漁船B", "F-15"]
        assert found[0].reasons == ["33 m", "同じ分類", "長さ ±3%", "向き差 2°"]
        # Another branch counts against: the aircraft comes last though nearest.
        assert found[2].score > 1

    def test_keeps_to_the_radius_and_judges_by_the_record_nearest_in_time(self):
        moved = [*self.records, obs(object_id="D", object_name="遠い船", image_id="d2", image_time=day(2), lat=35.60032)]
        assert [c.name for c in match_candidates(moved, self.image, 139.7, 35.6003, radius=100)] == ["遠い船", "F-15", "漁船B", "タンカーA"]
        assert match_candidates(self.records, self.image, 139.7, 35.6003, radius=10) == []


def area(**a) -> CheckedArea:
    base = dict(id=f"a{next(_n)}", image_id="d3", image_name="d3.tif", image_time=day(3), ring=box_ring([139.69, 35.59, 139.71, 35.61]), kind="",
                note="", created_at=day(20), updated_at=day(20), deleted_at=None)
    base.update(a)
    return CheckedArea(**base)


class TestCheckedAreas:
    def test_holds_a_place_inside_its_outline_and_covers_a_kind_and_below(self):
        a = area(kind="船舶")
        assert area_contains(a, 139.7, 35.6) and not area_contains(a, 139.72, 35.6)
        assert covering_areas([a], "d3", 139.7, 35.6, "船舶 > タンカー") == [a]
        assert covering_areas([a], "d3", 139.7, 35.6, "航空機") == []
        assert covering_areas([a], "d1", 139.7, 35.6, "船舶") == []
        assert covering_areas([replace(a, deleted_at=1)], "d3", 139.7, 35.6, "船舶") == []
        assert len(covering_areas([area(kind="")], "d3", 139.7, 35.6, "航空機")) == 1

    def test_reads_its_layer_from_the_settings(self):
        problems: list[str] = []
        a = area_store_of({"url": "https://x/FeatureServer/3", "fields": {"kind": "TARGET", "bad": "X"}, "softDelete": False}, problems, "a")
        assert (a.url, a.soft_delete, a.fields["kind"], a.fields["id"]) == ("https://x/FeatureServer/3", False, "TARGET", "AREA_ID")
        assert area_store_of("https://x/MapServer/3", problems, "b") is None
        assert problems == ["a の fields.bad は使用不可の役割", "b は編集できる …/FeatureServer/1 のようなポリゴンレイヤーの URL で指定"]

    def test_is_kept_in_an_esri_polygon_layer(self):
        fake = FakeEsri()
        url = "https://x/FeatureServer/2"
        layer = fake.add_layer(url, FakeLayer({n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeString"
                                               for n in ("AREA_ID", "IMAGE_ID", "KIND", "NOTE", "CREATED_AT", "UPDATED_AT", "DELETED_AT")}, "esriGeometryPolygon"))
        store = EsriAreaStore("確認範囲", area_store_of(url, [], "a"), EsriClient(fake))
        store.save([area(id="n1", kind="船舶", note="メモ")])
        adds = json.loads(next(f for u, f in fake.asked if u.endswith("/applyEdits"))["adds"])
        assert {k: adds[0]["attributes"][k] for k in ("AREA_ID", "IMAGE_ID", "KIND", "NOTE")} == {"AREA_ID": "n1", "IMAGE_ID": "d3", "KIND": "船舶", "NOTE": "メモ"}
        assert "OBSERVER" not in adds[0]["attributes"]
        assert len(adds[0]["geometry"]["rings"][0]) == 5
        # Another person reads it back and deletes it (marked).
        other = EsriAreaStore("確認範囲", area_store_of(url, [], "a"), EsriClient(fake))
        [loaded] = other.load()
        assert (loaded.id, loaded.image_id, loaded.kind, loaded.deleted_at) == ("n1", "d3", "船舶", None)
        [marked] = other.remove([loaded], day(21))
        assert marked.deleted_at == day(21)
        assert layer.rows[0]["attributes"]["DELETED_AT"] == day(21)


ALL = ["OBS_ID", "IMAGE_ID", "IMAGE_NAME", "IMAGE_TIME", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE", "CREATED_AT", "UPDATED_AT", "DELETED_AT"]
DATES = {"IMAGE_TIME": "esriFieldTypeDate", "CREATED_AT": "esriFieldTypeDate", "UPDATED_AT": "esriFieldTypeDate", "DELETED_AT": "esriFieldTypeDate",
         "PRESENT": "esriFieldTypeSmallInteger"}
URL = "https://x/FeatureServer/0"


def esri_layer(names, types=None):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: (types or {}).get(n, "esriFieldTypeString") for n in names}))
    return fake, layer


class TestEsriObservationStore:
    def test_adds_updates_reads_back_and_marks_deleted_records(self):
        fake, layer = esri_layer(ALL, DATES)
        config = observation_store_of({"url": URL}, [], 0)
        store = EsriObservationStore(config, EsriClient(fake))
        a = obs(id="r1", present=False, note="見当たらない")
        store.save([a])
        attrs = layer.rows[0]["attributes"]
        assert (attrs["OBS_ID"], attrs["PRESENT"], attrs["IMAGE_TIME"], attrs["OBJECTID"]) == ("r1", 0, day(1), 1)
        assert (layer.rows[0]["geometry"]["x"], layer.rows[0]["geometry"]["y"]) == (139.7, 35.6)
        store.save([replace(a, note="直した", lon=139.75)])
        assert len(layer.rows) == 1 and layer.rows[0]["attributes"]["NOTE"] == "直した"
        # A new store (another person) reads it back, and can update it.
        other = EsriObservationStore(config, EsriClient(fake))
        [read] = other.load()
        assert (read.id, read.present, read.note, read.lon, read.image_time, read.deleted_at) == ("r1", False, "直した", 139.75, day(1), None)
        [marked] = other.remove([read], day(30))
        assert marked.deleted_at == day(30)
        assert len(layer.rows) == 1 and layer.rows[0]["attributes"]["DELETED_AT"] == day(30)

    def test_writes_only_the_fields_the_layer_has_and_deletes_features_when_it_cannot_mark_them(self):
        fake, layer = esri_layer(["OBS_ID", "OBJECT_ID", "ACTIVITY", "IMAGE_TIME"])
        store = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))
        a = obs(id="r2")
        store.save([a])
        sent = json.loads(next(f for u, f in fake.asked if u.endswith("/applyEdits"))["adds"])[0]["attributes"]
        assert sorted(sent) == ["ACTIVITY", "IMAGE_TIME", "OBJECT_ID", "OBS_ID"]
        # A date in a text field is written as ISO text, and read back.
        assert layer.rows[0]["attributes"]["IMAGE_TIME"] == "2026-10-01T01:00:00.000Z"
        assert store.load()[0].image_time == day(1)
        assert store.remove([a]) == []
        assert layer.rows == []

    def test_keeps_a_record_the_server_did_not_delete_and_says_so(self):
        fake, layer = esri_layer(["OBS_ID", "OBJECT_ID"])

        def refusing(url, form):
            if url.endswith("/deleteFeatures"):
                return json.dumps({"deleteResults": [{"objectId": 1, "success": False, "error": {"description": "権限なし"}}]}).encode()
            return fake(url, form)

        store = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(refusing))
        a = obs(id="r3")
        store.save([a])
        with pytest.raises(EsriError, match="1 件の削除に失敗: 権限なし"):
            store.remove([a])
        # Still known by its object id: saved again, it is updated, not added twice.
        store.save([replace(a, note="再保存")])
        assert len(layer.rows) == 1

    def test_says_which_required_field_is_missing(self):
        fake, _ = esri_layer(["OBS_ID"])
        store = EsriObservationStore(observation_store_of({"label": "観測", "url": URL}, [], 0), EsriClient(fake))
        with pytest.raises(EsriError, match="観測 に属性 OBJECT_ID なし"):
            store.load()

    def test_writes_and_reads_length_and_heading(self):
        fake, layer = esri_layer(["OBS_ID", "OBJECT_ID", "LENGTH_M", "HEADING"], {"LENGTH_M": "esriFieldTypeDouble", "HEADING": "esriFieldTypeDouble"})
        layer.add({"OBS_ID": "a", "OBJECT_ID": "A", "LENGTH_M": 99.5, "HEADING": None}, {"x": 1, "y": 2})
        store = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake))
        [read] = store.load()
        assert read.length == 99.5 and read.heading is None
        store.save([obs(id="b", length=10, heading=90)])
        adds = json.loads([f for u, f in fake.asked if u.endswith("/applyEdits")][-1]["adds"])[0]["attributes"]
        assert (adds["LENGTH_M"], adds["HEADING"]) == (10, 90)

    def test_reads_present_as_text_and_pages_past_the_record_limit(self):
        fake, layer = esri_layer(["OBS_ID", "OBJECT_ID", "PRESENT"])
        layer.max_record_count = 3
        for i in range(7):
            layer.add({"OBS_ID": f"r{i}", "OBJECT_ID": "A", "PRESENT": "なし" if i == 0 else "あり"}, {"x": 1, "y": 2})
        rows = EsriObservationStore(observation_store_of({"url": URL}, [], 0), EsriClient(fake)).load()
        assert len(rows) == 7
        assert [o.present for o in rows[:2]] == [False, True]


TABLE = "https://x/FeatureServer/3"


def taxonomy_store(fake, label="分類"):
    return EsriTaxonomyStore(TaxonomyTableConfig(TABLE), EsriClient(fake), label)


class TestEsriTaxonomyStore:
    def test_writes_a_row_per_class_and_deletes_the_rows_of_classes_gone(self):
        fake = FakeEsri()
        layer = fake.add_layer(TABLE, FakeLayer({n: "esriFieldTypeString" for n in ("PATH", "LEVELS", "MEMBERS", "SORT_ORDER")}, ""))
        layer.add({"PATH": "船舶 > 支援船", "LEVELS": "大分類,艦種", "MEMBERS": "", "SORT_ORDER": 1})
        layer.add({"PATH": "船舶", "LEVELS": "大分類,艦種", "SORT_ORDER": 0})
        store = taxonomy_store(fake)
        taxa = store.load()
        assert [kind_of(t.path) for t in taxa] == ["船舶", "船舶 > 支援船"]
        assert taxa[0].levels == ["大分類", "艦種"]
        store.save([Taxon(["船舶"]), Taxon(["航空機"], members=["A", "B"])])
        edit = fake.asked[-1][1]
        assert edit["deletes"] == "1"
        assert json.loads(edit["updates"]) == [{"attributes": {"PATH": "船舶", "LEVELS": "", "MEMBERS": "", "SORT_ORDER": 0, "OBJECTID": 2}}]
        # NAME_LABEL is not in the table: left out.
        assert json.loads(edit["adds"]) == [{"attributes": {"PATH": "航空機", "LEVELS": "", "MEMBERS": "A,B", "SORT_ORDER": 1}}]

    def test_reads_a_class_saved_first_first_wherever_its_row_comes(self):
        fake = FakeEsri()
        layer = fake.add_layer(TABLE, FakeLayer({"PATH": "esriFieldTypeString", "SORT_ORDER": "esriFieldTypeString"}, ""))
        for path, order in (("車両", 2), ("航空機", 1), ("船舶", 0)):
            layer.add({"PATH": path, "SORT_ORDER": order})
        assert [kind_of(t.path) for t in taxonomy_store(fake).load()] == ["船舶", "航空機", "車両"]

    def test_writes_specifications_as_json_photos_one_per_line_and_the_fields_in_a_row_of_their_own(self):
        fake = FakeEsri()
        layer = fake.add_layer(TABLE, FakeLayer({n: "esriFieldTypeString" for n in ("PATH", "SPECS", "PHOTOS")}, ""))
        layer.add({"PATH": "船舶", "SPECS": '{"全長":"100"}', "PHOTOS": "https://x/a.jpg\nhttps://x/b.jpg"})
        layer.add({"PATH": "#諸元項目", "SPECS": '[{"name":"全長","type":"number","unit":"m"}]'})
        store = taxonomy_store(fake)
        assert store.load() == [Taxon(["船舶"], specs={"全長": "100"}, photos=["https://x/a.jpg", "https://x/b.jpg"])]
        assert store.load_fields() == [SpecField("全長", "number", "m")]
        # The fields' row is not a class: saving the classes leaves it.
        store.save([Taxon(["船舶"], specs={"全長": "120"})])
        edit = fake.asked[-1][1]
        assert "deletes" not in edit
        assert json.loads(edit["updates"]) == [{"attributes": {"PATH": "船舶", "SPECS": '{"全長":"120"}', "PHOTOS": "", "OBJECTID": 1}}]
        store.save_fields([SpecField("英名", "text")])
        assert json.loads(fake.asked[-1][1]["updates"]) == [{"attributes": {"PATH": "#諸元項目", "SPECS": '[{"name":"英名","type":"text"}]', "OBJECTID": 2}}]


class TestSpecifications:
    taxa = flatten([
        {"name": "船舶", "specs": {"開発国": "日本"}, "photos": ["https://x/ship.jpg"],
         "children": [{"name": "支援船", "specs": {"別名": "AOE、補給艦", "全長": "221"}, "children": [{"name": "〇〇級", "specs": {"全長": "167", "英名": "Oo class"}}]}]},
        {"name": "航空機"},
    ])

    def test_reads_a_classs_specifications_with_those_above_the_nearest_winning(self):
        specs = specs_at(self.taxa, ["船舶", "支援船", "〇〇級"])
        assert specs["全長"] == ("167", ["船舶", "支援船", "〇〇級"])
        assert specs["開発国"] == ("日本", ["船舶"])
        assert photos_at(self.taxa, ["船舶", "支援船"]) == (["https://x/ship.jpg"], ["船舶"])
        assert photos_at(self.taxa, ["航空機"])[0] == []

    def test_finds_a_class_by_its_name_another_name_or_its_english_name(self):
        assert class_by_name(self.taxa, "〇〇級") == ["船舶", "支援船", "〇〇級"]
        assert class_by_name(self.taxa, "補給艦") == ["船舶", "支援船"]
        assert class_by_name(self.taxa, "oo CLASS") == ["船舶", "支援船", "〇〇級"]
        assert class_by_name(self.taxa, "戦車") is None

    def test_changes_and_clears_specifications_and_photos_keeping_the_rest(self):
        changed = update_taxon(self.taxa, ["船舶", "支援船"], specs={"全長": "220", "空": ""})
        assert find(changed, ["船舶", "支援船"]) == Taxon(["船舶", "支援船"], specs={"全長": "220"})
        assert find(update_taxon(self.taxa, ["船舶"], photos=[]), ["船舶"]) == Taxon(["船舶"], specs={"開発国": "日本"})
        assert flatten(nest(self.taxa)) == self.taxa

    def test_places_a_branch_before_a_sibling_or_last(self):
        tree = flatten([{"name": "A", "children": [{"name": "a1"}, {"name": "a2"}, {"name": "a3", "children": [{"name": "x"}]}]}, {"name": "B"}])

        def names(items):
            return [kind_of(t.path) for t in items]

        assert names(place_taxon(tree, ["A", "a3"], ["A", "a1"])) == ["A", "A > a3", "A > a3 > x", "A > a1", "A > a2", "B"]
        assert names(place_taxon(tree, ["A", "a1"], None)) == ["A", "A > a2", "A > a3", "A > a3 > x", "A > a1", "B"]
        assert names(place_taxon(tree, ["B"], ["A"])) == ["B", "A", "A > a1", "A > a2", "A > a3", "A > a3 > x"]


class TestLocalStores:
    def test_keep_records_areas_the_classification_and_photos_in_the_geopackage(self, tmp_path):
        with open_local(str(tmp_path / "o.gpkg")) as db:
            store = LocalObservationStore(db)
            a = obs(id="x1", length=12.5, heading=30.0, confidence="推定", recorded_by="tanaka")
            b = obs(id="x2", image_time=None, present=False, activity="")
            store.save([a, b])
            read = {o.id: o for o in store.load()}
            assert read["x1"] == a
            assert read["x2"] == b
            [marked] = store.remove([a], day(22))
            assert {o.id: o for o in store.load()}["x1"].deleted_at == day(22) == marked.deleted_at
            # Areas: one set per observation store.
            mine, theirs = LocalAreaStore(db, "観測"), LocalAreaStore(db, "別")
            mine.save([area(id="z1", kind="船舶")])
            assert [x.id for x in mine.load()] == ["z1"] and theirs.load() == []
            assert mine.load()[0].ring == box_ring([139.69, 35.59, 139.71, 35.61])
            # The classification and its fields, per store.
            tax = LocalTaxonomyStore(db, "観測")
            assert tax.load() is None and tax.load_fields() is None
            tax.save([Taxon(["航空機"], levels=["大分類", "型式"]), Taxon(["船舶", "支援船"], members=["△△"], specs={"全長": "100"}, photos=["local:p"])])
            tax.save_fields([SpecField("全長", "number", "m")])
            assert [kind_of(t.path) for t in tax.load()] == ["航空機", "船舶", "船舶 > 支援船"]
            assert find(tax.load(), ["船舶", "支援船"]) == Taxon(["船舶", "支援船"], members=["△△"], specs={"全長": "100"}, photos=["local:p"])
            assert tax.load_fields() == [SpecField("全長", "number", "m")]
            tax.save([Taxon(["船舶"])])
            assert [kind_of(t.path) for t in tax.load()] == ["船舶"]
            assert tax.load_fields() == [SpecField("全長", "number", "m")]
            assert LocalTaxonomyStore(db, "別").load() is None
            photos = LocalPhotoStore(db)
            ref = photos.add(b"\x89PNG", "a.png", "image/png")
            assert ref.startswith("local:") and photos.get(ref) == b"\x89PNG"
            photos.remove(ref)
            assert photos.get(ref) is None


class TestReport:
    levels = [ConfidenceLevel("確定"), ConfidenceLevel("推定"), ConfidenceLevel("不明")]
    records = [
        obs(object_id="A", object_name="タンカー<A>", kind="船舶 > タンカー", confidence="確定", image_id="d1", image_name="d1.tif", image_time=day(1)),
        obs(object_id="A", object_name="タンカー<A>", kind="船舶 > タンカー", confidence="推定", image_id="d3", image_name="d3.tif", image_time=day(3), activity="航行", lon=139.75),
        obs(object_id="B", object_name="F-15", kind="航空機", confidence="不明", activity="駐機", image_id="d3", image_name="d3.tif", image_time=day(3)),
        obs(object_id="C", object_name="古い", image_id="d0", image_time=int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)),
        obs(object_id="D", object_name="削除", deleted_at=day(21)),
    ]

    def test_holds_the_period_class_and_confidence_asked_for(self):
        base = ReportOptions(start=day(1) - 3600_000, end=day(4))
        assert sorted(f.name for f in select_for_report(self.records, [], base, self.levels).flows) == ["F-15", "タンカー<A>"]
        assert len(select_for_report(self.records, [], replace(base, kind="船舶"), self.levels).records) == 2
        # At least 推定: 不明 and records without a level are left out.
        assert [o.confidence for o in select_for_report(self.records, [], replace(base, confidence="推定"), self.levels).records] == ["確定", "推定"]
        s = select_for_report(self.records, [area(kind="船舶"), area(kind="航空機"), area(image_time=day(9))], replace(base, kind="船舶 > タンカー"), self.levels)
        assert [a.kind for a in s.areas] == ["船舶"]
        assert [i.id for i in s.images] == ["d1", "d3"]

    def test_is_one_page_with_the_summary_counts_flows_and_areas_its_text_escaped(self):
        options = ReportOptions(title="週報", records=True)
        s = select_for_report(self.records, [area(note="港内すべて")], options, self.levels)
        page = report_html(s, options, ReportContext("ローカル", "田中", day(25), self.levels, "data:image/jpeg;base64,AAAA"))
        assert "<title>週報</title>" in page
        assert "モノ 3 件（最新で所在 3・不在 0）・記録 4 件・画像 3 枚・確認範囲 1 件" in page
        assert '<img src="data:image/jpeg;base64,AAAA"' in page
        assert "タンカー&lt;A&gt;" in page and "タンカー<A>" not in page
        assert "港内すべて" in page and "<h2>記録一覧</h2>" in page and "田中" in page
        made = int(datetime(2026, 10, 25, 12).timestamp() * 1000)
        assert report_file_name("週報 10/25", made) == "週報_10_25_20261025.html"
