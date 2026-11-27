"""
The AI columns of a record (tsudo 2026-11-27: 「AIのどのレコードからかも識別したい」
「備考の役割を無くそう」): SOURCE, AI_LAYER, AI_RESULT_ID, AI_SCORE, AI_MODEL,
AI_DETECTED_AT, AI_CONTINUATION on the record (the same but the last on a
誤検出 checked area), kept by every store, shown and written out, and pointed
at a table's row once a detection is sent there. The note is the person's.
"""
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest

from mikage.core.ai_results import ImportEntry, recorded_tags
from mikage.core.esri import EsriClient
from mikage.core.gpkg import open_local
from mikage.core.obs.ai_origin import AiOrigin, of_layer_run, origin_label, origin_of, origin_text, same_time
from mikage.core.obs.api import record_out
from mikage.core.obs.areas import CheckedArea
from mikage.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs
from mikage.core.obs.csv import observations_csv
from mikage.core.obs.export import ExportContext, ExportFilter, items_of, render, settings_for
from mikage.core.obs.model import DEFAULT_OBSERVATION_FIELDS, Observation, ObservationVocabulary, observation_store_of
from mikage.core.obs.output import observation_layers
from mikage.core.obs.stores import EsriObservationStore, LocalAreaStore, LocalObservationStore, LocalTaxonomyStore, ai_missing
from mikage.tests.fake_esri import FakeEsri, FakeLayer

AI_COLUMNS = ["SOURCE", "AI_LAYER", "AI_RESULT_ID", "AI_SCORE", "AI_MODEL", "AI_DETECTED_AT", "AI_CONTINUATION"]
RUN = int(datetime(2026, 11, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)


def day(d: int) -> int:
    return int(datetime(2026, 10, d, 1, tzinfo=timezone.utc).timestamp() * 1000)


def obs(**o) -> Observation:
    base = dict(id="o1", image_id="d1", image_name="d1.tif", image_time=day(1), object_id="A", object_name="船舶", kind="船舶",
                activity="停泊", present=True, note="", lon=139.7, lat=35.6, created_at=day(20), updated_at=day(20), deleted_at=None)
    base.update(o)
    return Observation(**base)


DETECTED = AiOrigin("AI検出", "物体検出（港湾モデル）", "3", 0.87, "港湾モデル", RUN, "自動継続")


def make_book(store, area_store, db):
    said: list[str] = []
    b = ObservationBook(store, area_store, LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(), observer=lambda: "田中",
                        prefs=Prefs(), say=said.append)
    b.said = said
    b.reload()
    b.set_open_images([ObservedImage("d1", "d1.tif", day(1), [139.6, 35.5, 139.8, 35.7], "L1")], "L1")
    return b


@pytest.fixture
def db(tmp_path):
    d = open_local(str(tmp_path / "o.gpkg"))
    yield d
    d.close()


@pytest.fixture
def book(db):
    return make_book(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), db)


def entry(ai, lon=139.70, note=""):
    """A result as 「AI結果」 records it (core/ai_results.import_entry)."""
    return ImportEntry(lon, 35.6, "", "船舶", "停泊", "推定", note, None, None, ai=ai)


class TestWords:
    def test_the_list_and_the_form_say_it_shortly_and_in_full(self):
        assert origin_label(DETECTED) == "AI検出 0.87" and origin_label(None) == ""
        assert origin_text(DETECTED) == "AI検出（港湾モデル・確信度 0.87・物体検出（港湾モデル） #3・自動継続）"
        assert origin_text(AiOrigin("外部AI")) == "外部AI" and origin_text(AiOrigin("外部AI", result_id="d-1")) == "外部AI（#d-1）"
        assert AiOrigin("AI誤検出").rejected and not DETECTED.rejected

    def test_kept_as_json_it_comes_back_the_same(self):
        assert origin_of(asdict(DETECTED)) == DETECTED and origin_of(None) is None and origin_of({"source": ""}) is None
        back = Observation(**json.loads(json.dumps(asdict(obs(ai=DETECTED)))))
        assert back.ai == DETECTED
        area = CheckedArea(**json.loads(json.dumps(asdict(CheckedArea("a", "d1", "d1", None, [], "", "", 0, 0, ai=AiOrigin("AI誤検出", "x", "1"))))))
        assert area.rejection and area.ai.layer == "x"

    def test_a_feature_of_a_detection_run_is_known_by_its_id_and_time(self):
        assert of_layer_run(DETECTED, "3", RUN + 400) and not of_layer_run(DETECTED, "3", RUN + 1500)
        assert not of_layer_run(DETECTED, "4", RUN)
        # A table's row of the same id is not the layer's feature.
        assert not of_layer_run(replace(DETECTED, layer="検出DB"), "3", RUN, tables={"検出DB"})
        # No time known: by the layer's name.
        timeless = replace(DETECTED, detected_at=None)
        assert of_layer_run(timeless, "3", None, layer=DETECTED.layer) and not of_layer_run(timeless, "3", None, layer="別のレイヤー")
        assert same_time(None, None) and not same_time(RUN, None)


class TestLocal:
    def test_records_and_areas_keep_their_origin(self, db):
        store = LocalObservationStore(db)
        store.save([obs(id="r1", ai=DETECTED), obs(id="r2", object_id="B")])
        read = {o.id: o.ai for o in store.load()}
        assert read == {"r1": DETECTED, "r2": None} and ai_missing(store) == []
        areas = LocalAreaStore(db, "ローカル")
        wrong = AiOrigin("AI誤検出", "検出DB", "12", 0.3, "港湾モデル", RUN)
        ring = [[139.6, 35.5], [139.61, 35.5], [139.61, 35.51], [139.6, 35.5]]
        areas.save([CheckedArea("a1", "d1", "d1.tif", day(1), ring, "", "", day(20), day(20), None, ai=wrong)])
        [a] = areas.load()
        assert a.ai == wrong and a.rejection and a.note == ""

    def test_imported_with_the_origin_the_note_the_results_own(self, book):
        saved = book.add_many([entry(DETECTED, note="岸壁の横"), entry(replace(DETECTED, result_id="4", continuation=""), lon=139.71)])
        assert [(o.ai.result_id, o.note) for o in saved] == [("3", "岸壁の横"), ("4", "")]
        book.reload()
        assert {o.ai for o in book.living()} == {DETECTED, replace(DETECTED, result_id="4", continuation="")}
        assert recorded_tags(book.living()) == {"[AI:物体検出（港湾モデル）#3]", "[AI:物体検出（港湾モデル）#4]"}

    def test_a_person_changing_it_keeps_where_it_came_from(self, book):
        [o] = book.add_many([entry(DETECTED)])
        changed = book.submit(o.lon, o.lat, FormValues(object=o.object_name, kind="船舶", activity="移動", note="動いた"), editing=o)
        assert changed.ai == DETECTED and changed.note == "動いた"
        # One recorded by hand (継続 of it too) has none.
        mine = book.submit(139.75, 35.65, FormValues(kind="船舶"))
        assert mine.ai is None

    def test_sent_to_a_table_the_records_and_wrong_ones_name_its_rows(self, book):
        [kept, other] = book.add_many([entry(DETECTED), entry(replace(DETECTED, result_id="5"), lon=139.71)])
        ring = [[139.6, 35.5], [139.61, 35.5], [139.61, 35.51], [139.6, 35.5]]
        [wrong] = book.add_areas([(ring, "", "", AiOrigin("AI誤検出", DETECTED.layer, "9", 0.2, "港湾モデル", RUN))])
        # The database keeps the time to the second.
        moves = [(RUN + 300, "3", "検出DB", "101"), (RUN, "9", "検出DB", "109"), (RUN + 5000, "5", "検出DB", "105")]
        assert book.repoint_ai(moves, ["検出DB"], DETECTED.layer) == 2
        assert "転記先の ID に付け替え: 2 件" in book.said[-1]
        book.reload()
        assert (book.record(kept.id).ai.layer, book.record(kept.id).ai.result_id) == ("検出DB", "101")
        assert book.record(kept.id).ai.score == 0.87 and book.record(other.id).ai.result_id == "5"  # another run: left
        [area] = [a for a in book.areas if a.id == wrong.id]
        assert (area.ai.layer, area.ai.result_id, area.ai.source) == ("検出DB", "109", "AI誤検出")
        # Sent again: nothing more to point (they name the table's rows now).
        assert book.repoint_ai(moves, ["検出DB"], DETECTED.layer) == 0


class TestEsri:
    URL = "https://x/FeatureServer/0"

    def layer(self, ai=True):
        fake = FakeEsri()
        names = ["OBS_ID", "IMAGE_ID", "OBJECT_ID", "OBJECT_NAME", "KIND", "ACTIVITY", "PRESENT", "NOTE", "CREATED_AT", "UPDATED_AT",
                 "DELETED_AT", *(AI_COLUMNS if ai else [])]
        types = {n: "esriFieldTypeDate" if n.endswith("_AT") else "esriFieldTypeSmallInteger" if n == "PRESENT"
                 else "esriFieldTypeDouble" if n == "AI_SCORE" else "esriFieldTypeString" for n in names}
        return fake, fake.add_layer(self.URL, FakeLayer(types))

    def test_the_layer_keeps_the_columns_and_reads_them_back(self):
        fake, layer = self.layer()
        config = observation_store_of({"url": self.URL}, [], 0)
        store = EsriObservationStore(config, EsriClient(fake))
        store.save([obs(id="r1", ai=DETECTED), obs(id="r2", object_id="B")])
        first, second = (r["attributes"] for r in layer.rows)
        assert [first[c] for c in AI_COLUMNS] == ["AI検出", "物体検出（港湾モデル）", "3", 0.87, "港湾モデル", RUN, "自動継続"]
        assert [second[c] for c in AI_COLUMNS] == [None] * 7 and first["NOTE"] == ""
        read = {o.id: o.ai for o in EsriObservationStore(config, EsriClient(fake)).load()}
        assert read == {"r1": DETECTED, "r2": None} and ai_missing(store) == []
        assert [DEFAULT_OBSERVATION_FIELDS[r] for r in ("aiSource", "aiLayer", "aiResultId")] == AI_COLUMNS[:3]

    def test_a_layer_without_them_takes_no_ai_result_but_the_persons_records(self, db):
        fake, layer = self.layer(ai=False)
        store = EsriObservationStore(observation_store_of({"url": self.URL}, [], 0), EsriClient(fake))
        b = make_book(store, LocalAreaStore(db, "ローカル"), db)
        # Not asked before it is read (the screen's check); asked when a save would be.
        assert b.ai_missing(ask=True) == AI_COLUMNS
        assert b.add_many([entry(DETECTED)]) is None and layer.rows == []
        assert "［E704］" in b.said[-1] and "SOURCE・AI_LAYER" in b.said[-1]
        mine = b.submit(139.75, 35.65, FormValues(kind="船舶"))
        assert mine is not None and len(layer.rows) == 1 and "SOURCE" not in layer.rows[0]["attributes"]
        assert store.as_kept([obs(ai=DETECTED)])[0].ai is None


class TestOutputs:
    def rows(self):
        return [obs(id="a", ai=DETECTED), obs(id="b", object_id="B", object_name="バス")]

    def test_csv_json_layers_and_scripts(self):
        text = observations_csv(self.rows()).lstrip("﻿")
        head, first, second = text.strip().split("\r\n")
        assert head.split(",")[-7:] == ["出どころ", "AI結果のレイヤー", "AI結果ID", "AI確信度", "AIモデル", "AI検出日時", "継続の判定"]
        assert first.split(",")[-7:-1] == ["AI検出", "物体検出（港湾モデル）", "3", "0.87", "港湾モデル", first.split(",")[-2]]
        assert first.split(",")[-1] == "自動継続" and first.split(",")[-2] and second.split(",")[-7:] == [""] * 7
        out = render(items_of(self.rows()), ExportContext(settings_for(None).format("標準（JSON）"), ExportFilter(), day(20)))
        data = json.loads(out.text)
        assert list(data[0])[-7:] == AI_COLUMNS and data[0]["AI_SCORE"] == 0.87 and (data[1]["SOURCE"], data[1]["AI_SCORE"]) == ("", None)
        points = observation_layers(self.rows(), [])[0]
        assert [values["source"] for _g, values in points.features] == ["AI検出", ""]
        assert record_out(self.rows()[0])["aiResultId"] == "3" and record_out(self.rows()[1])["aiSource"] == ""
