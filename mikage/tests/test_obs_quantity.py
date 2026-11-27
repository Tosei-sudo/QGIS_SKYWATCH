"""
数量 (tsudo 2026-11-24: 「観測記録に、数量の項目を作ろう」): one record may
stand for several things of its kind (車両 ×30, a group not placed one by
one). Empty stands for 1 (older records too); 2 or more is kept; 消失 and
不可 keep none.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from mikage.core.esri import EsriClient
from mikage.core.gpkg import open_local
from mikage.core.obs.alerts import SOURCES as ALERT_SOURCES
from mikage.core.obs.api import record_out
from mikage.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs
from mikage.core.obs.csv import observations_csv
from mikage.core.obs.export import ExportContext, ExportFilter, export_settings_of, items_of, render, settings_for
from mikage.core.obs.flows import flow_of
from mikage.core.obs.model import (
    DEFAULT_OBSERVATION_FIELDS, Observation, ObservationVocabulary, kept_quantity, observation_store_of, quantity_mark, quantity_of,
    read_quantity,
)
from mikage.core.obs.output import observation_layers
from mikage.core.obs import record_import as ri
from mikage.core.obs.stores import EsriObservationStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore, quantity_supported
from mikage.tests.fake_esri import FakeEsri, FakeLayer


def day(d: int) -> int:
    return int(datetime(2026, 10, d, 1, tzinfo=timezone.utc).timestamp() * 1000)


def obs(**o) -> Observation:
    base = dict(id="o1", image_id="d1", image_name="d1.tif", image_time=day(1), object_id="A", object_name="駐車場の車両", kind="車両",
                activity="駐車", present=True, note="", lon=139.7, lat=35.6, created_at=day(20), updated_at=day(20), deleted_at=None)
    base.update(o)
    return Observation(**base)


@pytest.fixture
def book(tmp_path):
    db = open_local(str(tmp_path / "o.gpkg"))
    said: list[str] = []
    b = ObservationBook(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(),
                        observer=lambda: "田中", prefs=Prefs(), say=said.append)
    b.reload()
    b.set_open_images([ObservedImage("d1", "d1.tif", day(1), [139.6, 35.5, 139.8, 35.7], "L1"),
                       ObservedImage("d3", "d3.tif", day(3), [139.6, 35.5, 139.8, 35.7], "L3")], "L1")
    yield b
    db.close()


class TestValue:
    @pytest.mark.parametrize("text, n", [("", None), ("  ", None), ("1", 1), ("30", 30), ("３０", 30), ("30 台", 30), ("12隻", 12),
                                         ("×30", 30), ("x 5", 5), ("1,200", 1200)])
    def test_reads_a_whole_number_as_typed(self, text, n):
        assert read_quantity(text) == n

    @pytest.mark.parametrize("text", ["0", "2.5", "-3", "たくさん", "30 m", "約30"])
    def test_refuses_what_is_no_count_of_things(self, text):
        with pytest.raises(ValueError):
            read_quantity(text)

    def test_kept_from_two_and_never_on_gone_or_unseen(self):
        assert [kept_quantity(n) for n in (None, 1, 2, 30.0, 0, True)] == [None, None, 2, 30, None, None]
        assert kept_quantity(5, present=False) is None
        assert quantity_of(obs()) == 1 and quantity_of(obs(quantity=30)) == 30
        assert quantity_of(obs(present=False, activity="")) == 0 and quantity_of(obs(present=False, unseen="雲")) == 0
        assert quantity_mark(obs(quantity=30)) == "×30" and quantity_mark(obs()) == "" and quantity_mark(obs(quantity=1)) == ""
        assert DEFAULT_OBSERVATION_FIELDS["quantity"] == "QUANTITY"


class TestBook:
    def test_a_group_is_recorded_carried_with_its_count_and_gone_without_one(self, book):
        group = book.submit(139.70, 35.60, FormValues(object="第3駐車場の車両", kind="車両", activity="駐車", quantity="３０台"))
        one = book.submit(139.75, 35.65, FormValues(object="", kind="車両", activity="移動", quantity=""))
        assert (group.quantity, one.quantity) == (30, None)
        book.reload()
        assert book.record(group.id).quantity == 30 and book.record(one.id).quantity is None
        book.step(1)
        # 継続 (the row's, and すべて継続): the count of the image before, changed in the form when it changed.
        carried = book.carry(group.object_id)
        assert carried.quantity == 30
        fewer = book.submit(carried.lon, carried.lat, FormValues(object=carried.object_name, kind="車両", activity="駐車", quantity="22"),
                            editing=carried)
        assert fewer.quantity == 22
        # 消失 keeps none, whatever the form said.
        gone = book.submit(one.lon, one.lat, FormValues(kind="車両", quantity="5"), False, source=one)
        assert not gone.present and gone.quantity is None
        assert flow_of([group, fewer]).quantities() == [30, 22]

    def test_one_typed_or_emptied_is_one_thing(self, book):
        r = book.submit(139.70, 35.60, FormValues(kind="車両", quantity="12"))
        back = book.submit(r.lon, r.lat, FormValues(object=r.object_name, kind="車両", quantity="1"), editing=r)
        assert back.quantity is None
        again = book.update(back.id, quantity=8)
        assert again.quantity == 8
        assert book.update(again.id, present=False).quantity is None

    def test_absent_all_and_unseen_drop_the_count(self, book):
        r = book.submit(139.70, 35.60, FormValues(kind="車両", quantity="12"))
        book.step(1)
        [gone] = book.absent_all()
        assert gone.object_id == r.object_id and gone.quantity is None

    def test_bulk_edit_sets_the_count_of_those_still_there(self, book):
        a = book.submit(139.70, 35.60, FormValues(kind="車両"))
        b = book.submit(139.71, 35.61, FormValues(kind="車両", quantity="4"))
        c = book.submit(139.72, 35.62, FormValues(kind="車両"), False)
        out = book.update_many([a.id, b.id, c.id], quantity=10)
        assert sorted(o.id for o in out.saved) == sorted([a.id, b.id]) and out.uncounted == 1
        assert "消失・不可の 1 件は数量なしのまま" in out.text()
        assert {book.record(i).quantity for i in (a.id, b.id)} == {10} and book.record(c.id).quantity is None
        back = book.update_many([a.id, b.id], quantity=1)
        assert len(back.saved) == 2 and all(book.record(i).quantity is None for i in (a.id, b.id))

    def test_a_record_not_saved_waits_with_its_count(self, book):
        def refuse(_rows):
            raise RuntimeError("つながらない")
        book.store.save = refuse
        assert book.submit(139.70, 35.60, FormValues(kind="車両", quantity="7")) is None
        [draft] = book.drafts()
        assert draft.values["quantity"] == "7" and draft.record.quantity == 7


class TestFlow:
    def test_a_change_of_count_is_a_step_of_its_own(self):
        rows = [obs(id="1", image_id="d1", image_time=day(1), quantity=30), obs(id="2", image_id="d2", image_time=day(2), quantity=30),
                obs(id="3", image_id="d3", image_time=day(3), quantity=22), obs(id="4", image_id="d4", image_time=day(4), present=False, activity=""),
                obs(id="5", image_id="d5", image_time=day(5), quantity=31)]
        f = flow_of(rows)
        assert [(s.count, s.quantity, s.present) for s in f.steps] == [(2, 30, True), (1, 22, True), (1, 0, False), (1, 31, True)]
        assert f.quantities() == [30, 22, 31] and f.quantity == 31
        # One thing all along: nothing to show.
        assert flow_of([obs(id="1"), obs(id="2", image_id="d2", image_time=day(2))]).quantities() == []


class TestStores:
    URL = "https://x/FeatureServer/0"

    def layer(self, quantity=True):
        fake = FakeEsri()
        names = ["OBS_ID", "IMAGE_ID", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "CREATED_AT", "UPDATED_AT", "DELETED_AT",
                 *(["QUANTITY"] if quantity else [])]
        types = {n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeSmallInteger" if n == "PRESENT"
                 else "esriFieldTypeInteger" if n == "QUANTITY" else "esriFieldTypeString" for n in names}
        return fake, fake.add_layer(self.URL, FakeLayer(types))

    def test_the_esri_layer_keeps_two_or_more_and_empty_for_one(self):
        fake, layer = self.layer()
        config = observation_store_of({"url": self.URL}, [], 0)
        store = EsriObservationStore(config, EsriClient(fake))
        assert not quantity_supported(store)  # nothing asked yet
        store.save([obs(id="r1", quantity=30), obs(id="r2", object_id="B")])
        assert [r["attributes"]["QUANTITY"] for r in layer.rows] == [30, None] and quantity_supported(store)
        layer.rows[1]["attributes"]["QUANTITY"] = 1  # as another system may write it
        read = {o.id: o.quantity for o in EsriObservationStore(config, EsriClient(fake)).load()}
        assert read == {"r1": 30, "r2": None}

    def test_a_layer_without_it_keeps_none_and_reads_back_the_same(self):
        fake, layer = self.layer(quantity=False)
        store = EsriObservationStore(observation_store_of({"url": self.URL}, [], 0), EsriClient(fake))
        store.save([obs(id="r1", quantity=30)])
        assert "QUANTITY" not in layer.rows[0]["attributes"] and not store.keeps_quantity()
        assert store.as_kept([obs(id="r1", quantity=30)])[0].quantity is None

    def test_the_local_store_keeps_it(self, tmp_path):
        db = open_local(str(tmp_path / "o.gpkg"))
        store = LocalObservationStore(db)
        store.save([obs(id="r1", quantity=12), obs(id="r2", object_id="B")])
        assert {o.id: o.quantity for o in store.load()} == {"r1": 12, "r2": None} and quantity_supported(store)
        db.close()


class TestOutputs:
    def rows(self):
        return [obs(id="a", quantity=30), obs(id="b", object_id="B", object_name="トラック"),
                obs(id="c", object_id="C", object_name="バス", present=False, activity="")]

    def test_csv_json_layers_and_scripts(self):
        rows = self.rows()
        text = observations_csv(rows).lstrip("﻿")
        head, *lines = text.strip().split("\r\n")
        # 数量, then the AI columns (2026-11-27) after it.
        assert head.endswith(",数量,出どころ,AI結果のレイヤー,AI結果ID,AI確信度,AIモデル,AI検出日時,継続の判定")
        assert [line.split(",")[-8] for line in lines] == ["30", "1", ""]
        s = settings_for(None)
        out = render(items_of(rows), ExportContext(s.format("標準（JSON）"), ExportFilter(), day(20)))
        data = json.loads(out.text)
        assert [d["QUANTITY"] for d in data] == [30, 1, None] and list(data[0])[-8:-6] == ["QUANTITY", "SOURCE"]
        points = observation_layers(rows, [])[0]
        assert [f.name for f in points.fields if f.name == "quantity"] == ["quantity"]
        assert [values["quantity"] for _g, values in points.features] == [30, 1, None]
        assert [record_out(o)["quantity"] for o in rows] == [30, 1, None]
        assert "quantity" in ALERT_SOURCES

    def test_a_named_export_column(self):
        problems: list[str] = []
        s = export_settings_of({"formats": [{"name": "台数", "format": "csv", "columns": ["objectName", {"key": "N", "source": "quantity"}]}]},
                               problems)
        assert problems == []
        out = render(items_of(self.rows()), ExportContext(s.format("台数"), ExportFilter(), day(20)))
        assert out.text.strip().split("\r\n") == ["objectName,N", "駐車場の車両,30", "トラック,1", "バス,"]


class TestImport:
    def test_a_count_column_is_read_and_checked(self):
        rows = [ri.SourceRow(1, {"画像ID": "d1", "種別": "車両", "台数": "30"}, 139.7, 35.6),
                ri.SourceRow(2, {"画像ID": "d1", "種別": "車両", "台数": ""}, 139.71, 35.6),
                ri.SourceRow(3, {"画像ID": "d1", "種別": "車両", "台数": "2.5"}, 139.72, 35.6)]
        mapping = ri.guess_mapping(["画像ID", "種別", "台数"])
        assert mapping["quantity"] == "台数"
        planned = ri.plan(rows, mapping, [])
        assert [p.action for p in planned] == [ri.NEW, ri.NEW, ri.ERROR]
        assert [p.record.quantity for p in planned[:2]] == [30, None] and "数量" in planned[2].reason


def test_older_records_read_as_one_thing():
    """A record kept before 数量 (no attribute at all, as drafts and old files hold them)."""
    old = replace(obs(), quantity=None)
    assert quantity_of(old) == 1 and quantity_mark(old) == ""
