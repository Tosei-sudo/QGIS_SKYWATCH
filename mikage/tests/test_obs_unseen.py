"""
「不可」 (tsudo 2026-11-19, 案A): a thing that could not be seen in an
image (under cloud, haze, a shadow…) is recorded as neither 継続 nor 消失,
alone (the form's 「不可」) or by a 不可範囲 drawn over the image.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from mikage.core.esri import EsriClient, EsriError
from mikage.core.gpkg import open_local
from mikage.core.obs.alerts import AlertSettings, select as alert_select
from mikage.core.obs.areas import area_label, area_store_of, box_ring, covering_areas
from mikage.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs
from mikage.core.obs.csv import observations_csv
from mikage.core.obs.export import ExportFilter, select_records
from mikage.core.obs.flows import ImageRef, carry_overs, flow_of, match_candidates
from mikage.core.obs.model import DEFAULT_UNSEEN_REASONS, Observation, ObservationVocabulary, observation_store_of, state_text, unseen_label, vocabulary_of
from mikage.core.obs.report import ReportContext, ReportOptions, report_html, select_for_report
from mikage.core.obs.stores import EsriAreaStore, EsriObservationStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore
from mikage.tests.fake_esri import FakeEsri, FakeLayer


def day(d: int) -> int:
    return int(datetime(2026, 10, d, 1, tzinfo=timezone.utc).timestamp() * 1000)


def obs(**o) -> Observation:
    base = dict(id="o1", image_id="d1", image_name="d1.tif", image_time=day(1), object_id="A", object_name="A", kind="船舶",
                activity="停泊", present=True, note="", lon=139.7, lat=35.6, created_at=day(20), updated_at=day(20), deleted_at=None)
    base.update(o)
    return Observation(**base)


@pytest.fixture
def book(tmp_path):
    db = open_local(str(tmp_path / "o.gpkg"))
    said: list[str] = []
    b = ObservationBook(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(),
                        observer=lambda: "田中", prefs=Prefs(), say=said.append)
    b.said = said
    b.reload()
    b.set_open_images([ObservedImage("d1", "d1.tif", day(1), [139.6, 35.5, 139.8, 35.7], "L1"),
                       ObservedImage("d3", "d3.tif", day(3), [139.6, 35.5, 139.8, 35.7], "L3"),
                       ObservedImage("d5", "d5.tif", day(5), [139.6, 35.5, 139.8, 35.7], "L5")], "L1")
    yield b
    db.close()


CLOUD = box_ring([139.69, 35.59, 139.72, 35.62])


class TestRecordsAndFlows:
    def test_says_why_and_is_neither_there_nor_gone(self):
        assert unseen_label("雲") == "不可（雲）" and unseen_label("") == "不可" and unseen_label("不可") == "不可"
        assert state_text(obs(present=False, unseen="霞", activity="")) == "不可（霞）"
        assert state_text(obs(present=False, activity="")) == "消失" and state_text(obs()) == "停泊"

    def test_the_next_image_carries_the_thing_from_its_last_record_that_saw_it(self):
        seen = obs(id="s", activity="停泊", lon=139.70)
        hidden = obs(id="h", image_id="d3", image_time=day(3), present=False, activity="", unseen="雲", lon=139.70)
        records = [seen, hidden]
        # In the cloudy image itself it is recorded (nothing waits); in the next one it comes from where it was last seen.
        assert carry_overs(records, ImageRef("d3", "d3", day(3))) == []
        [c] = carry_overs(records, ImageRef("d5", "d5", day(5)))
        assert c.source.id == "s" and c.side == "before"
        # An earlier image looks at the later one that saw it, not the 不可 one.
        assert [x.source.id for x in carry_overs([hidden, replace(seen, id="later", image_id="d9", image_time=day(9))],
                                                  ImageRef("d2", "d2", day(2)))] == ["later"]
        # Only records that saw it are offered as the same thing.
        assert match_candidates([hidden], ImageRef("d5", "d5", day(5)), 139.70, 35.60) == []

    def test_the_flow_has_a_step_of_its_own_and_knows_the_last_it_was_seen(self):
        rows = [obs(id="1", activity="停泊"), obs(id="2", image_id="d3", image_time=day(3), present=False, activity="", unseen="雲"),
                obs(id="3", image_id="d4", image_time=day(4), present=False, activity="", unseen="雲")]
        f = flow_of(rows)
        assert [(s.present, s.activity, s.unseen, s.count) for s in f.steps] == [(True, "停泊", "", 1), (False, "", "雲", 2)]
        # The latest known: still 停泊 (not 消失); its latest image could not see it.
        assert (f.present, f.activity, f.unseen, f.seen, f.last) == (True, "停泊", "雲", 1, day(4))
        f = flow_of(rows + [obs(id="4", image_id="d5", image_time=day(5), present=False, activity="")])
        assert (f.present, f.unseen, f.steps[-1].unseen) == (False, "", "")

    def test_the_reasons_come_from_the_settings(self):
        problems: list[str] = []
        assert ObservationVocabulary().unseen_reasons == DEFAULT_UNSEEN_REASONS == ["雲", "霞", "影", "画像の欠け", "その他"]
        assert vocabulary_of({"unseenReasons": ["雲", " 煙 ", "雲", ""]}, problems).unseen_reasons == ["雲", "煙"]
        assert vocabulary_of({"unseenReasons": "雲"}, problems).unseen_reasons == DEFAULT_UNSEEN_REASONS
        assert problems == ["observationVocabulary.unseenReasons は不可の理由（雲・霞など）の文字列の配列で指定"]


class TestBook:
    def test_a_cloud_drawn_records_the_things_under_it_as_unseen(self, book):
        a = book.submit(139.70, 35.60, FormValues(object="A", kind="船舶", activity="停泊"))
        b = book.submit(139.75, 35.65, FormValues(object="B", kind="船舶", activity="航行"))
        book.step(1)
        assert book.keeps_unseen() and book.keeps_unseen_areas()
        area = book.add_area(CLOUD, kind="船舶", note="港の上に雲", unseen=" 雲 ")
        assert (area.unseen, area.kind, area.note) == ("雲", "", "港の上に雲") and area_label(area) == "不可範囲（雲）"
        assert book.status == "不可範囲（雲）を記録（中の前後の画像のモノ 1 件を不可として記録）"
        [hidden] = [o for o in book.here() if o.object_id == a.object_id]
        assert (hidden.present, hidden.unseen, hidden.activity, hidden.lon) == (False, "雲", "", a.lon)
        # Only B waits; 「すべて消失」 cannot reach A (not 消失: not seen).
        assert [c.source.object_id for c in book.carried()] == [b.object_id]
        # Read back from the GeoPackage the same.
        book.reload()
        assert book.record(hidden.id).unseen == "雲" and not book.record(hidden.id).present
        assert [x.unseen for x in book.image_areas()] == ["雲"]
        # The next image: A from where it was last seen, 停泊.
        book.step(1)
        assert sorted((c.source.object_id, c.source.activity) for c in book.carried()) == sorted([(a.object_id, "停泊"), (b.object_id, "航行")])

    def test_carry_all_and_absent_all_leave_the_things_in_a_cloud(self, book):
        a = book.submit(139.70, 35.60, FormValues(object="A", kind="船舶"))
        b = book.submit(139.75, 35.65, FormValues(object="B", kind="船舶"))
        c = book.submit(139.705, 35.605, FormValues(object="C", kind="船舶"))
        book.step(1)
        # A checked area over the cloud: the cloud wins (what could not be seen is not 消失).
        book.add_area(box_ring([139.68, 35.58, 139.73, 35.63]), kind="")
        book.add_area(CLOUD, unseen="雲")
        book.remove([o for o in book.here() if o.unseen])  # (as if A and C came into it later)
        assert {x.source.object_id for x in book.unseen_carried()} == {a.object_id, c.object_id}
        assert book.inside_carried() == []
        made = book.carry_all()
        assert [o.object_id for o in made] == [b.object_id] and "不可範囲内の 2 件を除く" in book.status
        assert book.absent_all() == []
        marked = book.mark_unseen()
        assert sorted(o.object_id for o in marked) == sorted([a.object_id, c.object_id]) and all(o.unseen == "雲" for o in marked)
        assert book.status == "2 のモノを不可として記録（不可範囲内）"

    def test_one_thing_unseen_by_the_form_and_back_to_seen(self, book):
        a = book.submit(139.70, 35.60, FormValues(object="A", kind="船舶", activity="停泊"))
        book.step(1)
        [c] = book.carried()
        hidden = book.submit(c.source.lon, c.source.lat, FormValues(object="A", kind="船舶", activity="停泊"), False, source=c.source, unseen="影")
        assert (hidden.object_id, hidden.present, hidden.unseen, hidden.activity) == (a.object_id, False, "影", "")
        assert book.status == "A（不可（影））を記録"
        # Opened again and saved as there: 継続.
        seen = book.submit(hidden.lon, hidden.lat, FormValues(object="A", kind="船舶", activity="停泊"), True, editing=hidden)
        assert (seen.present, seen.unseen, seen.activity) == (True, "", "停泊")
        # And by the API's update.
        assert book.update(seen.id, unseen="霞").unseen == "霞" and not book.record(seen.id).present
        assert book.update(seen.id, present=True).unseen == ""
        # A carried thing straight away (the dock's carry).
        book.step(1)
        assert book.carry(a.object_id, unseen="雲").unseen == "雲" and book.status == "A を不可（雲）として記録"

    def test_deleting_a_cloud_takes_its_unseen_records_and_restoring_brings_them_back(self, book):
        a = book.submit(139.70, 35.60, FormValues(object="A", kind="船舶"))
        book.step(1)
        area = book.add_area(CLOUD, unseen="雲")
        other = book.add_area(box_ring([139.75, 35.65, 139.76, 35.66]), unseen="霞")
        assert other is not None and len([o for o in book.here() if o.unseen]) == 1
        assert book.remove_areas([area])
        assert book.status == "不可範囲 1 件を削除（中の不可の記録 1 件も削除）"
        assert [c.source.object_id for c in book.carried()] == [a.object_id]
        assert book.restore_areas([x for x in book.image_areas(deleted=True) if x.id == area.id])
        assert "中の前後の画像のモノ 1 件を不可として記録" in book.status and book.carried() == []

    def test_no_cloud_where_the_store_cannot_keep_it(self, book, monkeypatch):
        monkeypatch.setattr(type(book.area_store), "keeps_unseen", lambda self: False)
        book.step(1)
        assert book.add_area(CLOUD, unseen="雲") is None and "UNSEEN_REASON" in book.status
        assert book.keeps_unseen() and not book.keeps_unseen_areas()


class TestEsri:
    URL = "https://x/FeatureServer/0"

    def layer(self, unseen=True):
        fake = FakeEsri()
        names = ["OBS_ID", "IMAGE_ID", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "CREATED_AT", "UPDATED_AT", "DELETED_AT",
                 *(["UNSEEN_REASON"] if unseen else [])]
        types = {n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeSmallInteger" if n == "PRESENT" else "esriFieldTypeString"
                 for n in names}
        return fake, fake.add_layer(self.URL, FakeLayer(types))

    def test_keeps_the_reason_and_leaves_present_empty(self):
        fake, layer = self.layer()
        config = observation_store_of({"url": self.URL}, [], 0)
        store = EsriObservationStore(config, EsriClient(fake))
        assert not store.keeps_unseen()  # nothing asked yet
        store.save([obs(id="r1", present=False, activity="", unseen="雲")])
        attrs = layer.rows[0]["attributes"]
        assert (attrs["PRESENT"], attrs["UNSEEN_REASON"]) == (None, "雲") and store.keeps_unseen()
        [read] = EsriObservationStore(config, EsriClient(fake)).load()
        assert (read.present, read.unseen) == (False, "雲")
        # Seen again: the reason cleared, PRESENT 1.
        store.save([replace(read, present=True, unseen="", activity="停泊")])
        attrs = layer.rows[0]["attributes"]
        assert (attrs["PRESENT"], attrs["UNSEEN_REASON"]) == (1, None)

    def test_refuses_an_unseen_record_the_layer_cannot_tell(self):
        fake, layer = self.layer(unseen=False)
        store = EsriObservationStore(observation_store_of({"url": self.URL}, [], 0), EsriClient(fake))
        store.save([obs(id="r1")])
        assert not store.keeps_unseen()
        with pytest.raises(EsriError, match="UNSEEN_REASON"):
            store.save([obs(id="r2", present=False, activity="", unseen="雲")])
        assert len(layer.rows) == 1

    def test_a_cloud_area_in_the_checked_areas_layer(self):
        fake = FakeEsri()
        url = "https://x/FeatureServer/2"
        layer = fake.add_layer(url, FakeLayer({n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeString"
                                               for n in ("AREA_ID", "IMAGE_ID", "KIND", "NOTE", "CREATED_AT", "UPDATED_AT", "DELETED_AT",
                                                         "UNSEEN_REASON")}, "esriGeometryPolygon"))
        store = EsriAreaStore("確認範囲", area_store_of(url, [], "a"), EsriClient(fake))
        from mikage.core.obs.areas import CheckedArea
        store.save([CheckedArea("n1", "d3", "d3.tif", day(3), CLOUD, "", "", day(20), day(20), unseen="雲"),
                    CheckedArea("n2", "d3", "d3.tif", day(3), CLOUD, "船舶", "", day(20), day(20))])
        assert [r["attributes"]["UNSEEN_REASON"] for r in layer.rows] == ["雲", None]
        loaded = EsriAreaStore("確認範囲", area_store_of(url, [], "a"), EsriClient(fake)).load()
        assert sorted((a.id, a.unseen) for a in loaded) == [("n1", "雲"), ("n2", "")]
        # A cloud covers any kind; a checked area does not count it, nor the other way round.
        assert [a.id for a in covering_areas(loaded, "d3", 139.7, 35.6, "航空機", unseen=True)] == ["n1"]
        assert [a.id for a in covering_areas(loaded, "d3", 139.7, 35.6, "船舶")] == ["n2"]


class TestOutputs:
    def test_csv_export_report_and_alerts(self):
        rows = [obs(id="1"), obs(id="2", image_id="d3", image_time=day(3), present=False, activity="", unseen="雲"),
                obs(id="3", object_id="B", object_name="B", present=False, activity="")]
        text = observations_csv(rows)
        assert "不可（雲）" in text and text.count("なし") == 1
        assert [o.id for o in select_records(rows, ExportFilter(presence="unseen"))] == ["2"]
        assert [o.id for o in select_records(rows, ExportFilter(presence="absent"))] == ["3"]
        assert [o.id for o in select_records(rows, ExportFilter(presence="present"))] == ["1"]
        sel = select_for_report(rows, [], ReportOptions(start=day(1) - 1, end=day(9)), [])
        html = report_html(sel, ReportOptions(start=day(1) - 1, end=day(9), records=True, flows=True), ReportContext(store="ローカル", author="", made=day(20)))
        assert "不可（雲）" in html and "最新の画像では不可（雲）" in html

    def test_an_unseen_record_is_never_reported(self):
        rows = [obs(id="1"), obs(id="2", present=False, activity="", unseen="雲")]
        assert [i.record.id for i in alert_select(rows, AlertSettings(present_only=False, score=None))] == ["1"]


def test_csv_column_and_export_source_say_why():
    from mikage.core.obs.export import RECORD_SOURCES, STANDARD_COLUMNS
    assert "unseen" in RECORD_SOURCES and ("UNSEEN_REASON", "unseen") in STANDARD_COLUMNS
    assert json.dumps(state_text(obs(present=False, unseen="雲")), ensure_ascii=False) == '"不可（雲）"'
