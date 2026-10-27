"""
The panel's workings without the screen (core/obs/book.py) over the local
GeoPackage, and when images were taken (core/obs/image_time.py).
"""
from datetime import datetime, timezone

import pytest

from satellite_observation.core.gpkg import open_local
from satellite_observation.core.obs.areas import box_ring
from satellite_observation.core.obs.book import FormValues, ObservationBook, ObservedImage, Prefs
from satellite_observation.core.obs.image_time import image_time, metadata_items_of_xml, nitf_time, parse_time, time_from_metadata, time_from_name
from satellite_observation.core.obs.model import ObservationVocabulary
from satellite_observation.core.obs.report import ReportOptions
from satellite_observation.core.obs.stores import LocalAreaStore, LocalObservationStore, LocalTaxonomyStore
from satellite_observation.core.obs.taxonomy import Taxon, kind_of


def day(d: int) -> int:
    return int(datetime(2026, 10, d, 1, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture
def book(tmp_path):
    db = open_local(str(tmp_path / "o.gpkg"))
    said: list[str] = []
    prefs = Prefs()
    b = ObservationBook(LocalObservationStore(db), LocalAreaStore(db, "ローカル"), LocalTaxonomyStore(db, "ローカル"), ObservationVocabulary(),
                        observer=lambda: "田中", prefs=prefs, say=said.append)
    b.said = said
    b.reload()
    images = [ObservedImage("d3", "d3.tif", day(3), [139.6, 35.5, 139.8, 35.7], "L3"), ObservedImage("d1", "d1.tif", day(1), None, "L1"),
              ObservedImage("dx", "dx.tif", None, None, "LX")]
    b.set_open_images(images, "L1")
    yield b
    db.close()


def test_steps_through_the_images_in_time_order_and_types_a_time(book):
    assert [i.id for i in book.images] == ["d1", "d3", "dx"]
    assert book.current.id == "d1"
    assert book.step(1).id == "d3" and book.step(1).id == "dx" and book.step(1).id == "dx"
    rec = book.submit(139.7, 35.6, FormValues(object="", kind="船舶"))
    assert rec.image_time is None and rec.object_name == "船舶-1"
    # The time typed goes to the image and to its records made without one.
    book.set_image_time(day(2))
    assert [i.id for i in book.images] == ["d1", "dx", "d3"]
    assert book.current.id == "dx" and book.current.origin == "manual"
    assert book.record(rec.id).image_time == day(2)


def test_records_carries_over_and_marks_gone(book):
    a = book.submit(139.7, 35.6, FormValues(object="あおば", kind="船舶 > 護衛艦", activity="停泊", confidence="推定"))
    assert (a.recorded_by, a.updated_by, a.confidence) == ("田中", "田中", "推定")
    assert book.last.kind == "船舶 > 護衛艦" and book.last.confidence == "推定"
    book.submit(139.9, 35.6, FormValues(object="遠い", kind="船舶"))
    book.step(1)
    # d3's footprint holds あおば, not the far one.
    assert [c.source.object_name for c in book.carried()] == ["あおば"]
    carried = book.carry(a.object_id, True, activity="航行")
    assert carried.object_id == a.object_id and carried.activity == "航行" and carried.image_id == "d3"
    book.step(1)
    gone = book.carry(a.object_id, False)
    assert gone.present is False and gone.activity == ""
    flow = book.flow(a.object_id)
    assert [s.activity for s in flow.steps] == ["停泊", "航行", ""] and flow.present is False


def test_carries_all_but_those_in_a_checked_area_which_are_gone(book):
    a = book.submit(139.70, 35.60, FormValues(object="A", kind="船舶"))
    b = book.submit(139.75, 35.65, FormValues(object="B", kind="船舶"))
    c = book.submit(139.71, 35.61, FormValues(object="C", kind="航空機"))
    book.step(1)
    area = book.add_area(box_ring([139.69, 35.59, 139.72, 35.62]), kind="船舶", note="港内")
    assert area.recorded_by == "田中" and area.kind == "船舶"
    assert "確認範囲（船舶）を記録（範囲内で記録のない前後の画像のモノ 1 件）" in book.said[-1]
    assert [x.source.object_id for x in book.inside_carried()] == [a.object_id]
    made = book.carry_all()
    assert sorted(o.object_id for o in made) == sorted([b.object_id, c.object_id])
    assert "（確認範囲内の 1 件を除く）" in book.status
    gone = book.absent_inside()
    assert [(o.object_id, o.present) for o in gone] == [(a.object_id, False)]
    assert book.carried() == []
    # Areas are deleted (marked) and brought back.
    assert book.remove_areas([area]) and book.image_areas() == [] and len(book.image_areas(deleted=True)) == 1
    assert book.restore_areas(book.image_areas(deleted=True)) and len(book.image_areas()) == 1


def test_offers_the_same_thing_and_names_it_by_a_known_name(book):
    a = book.submit(139.7, 35.6, FormValues(object="タンカーA", kind="船舶 > タンカー", length="180", heading="90"))
    book.step(1)
    [c] = book.candidates(139.7, 35.6003, "船舶 > タンカー", 175, 92)
    assert c.object_id == a.object_id and c.reasons[1:] == ["同じ分類", "長さ ±3%", "向き差 2°"]
    # The known name typed: the same thing.
    again = book.submit(139.7, 35.6003, FormValues(object="タンカーA", kind="船舶 > タンカー"))
    assert again.object_id == a.object_id
    # Heading is kept within a turn; a bad length is none.
    odd = book.submit(139.7, 35.6, FormValues(object="", kind="船舶", length="-3", heading="-90"))
    assert odd.length is None and odd.heading == 270


def test_changes_deletes_restores_renames_and_merges(book):
    a = book.submit(139.7, 35.6, FormValues(object="A", kind="船舶"))
    b = book.submit(139.8, 35.6, FormValues(object="B", kind="船舶 > 漁船"))
    assert book.update(a.id, note="直した", kind="船舶 ›  支援船").kind == "船舶 > 支援船"
    with pytest.raises(ValueError):
        book.update(a.id, id="x")
    assert book.move(a.id, 139.71, 35.61).lon == 139.71
    assert book.remove([book.record(a.id)]) and book.record(a.id).deleted_at is not None
    assert book.here() == [book.record(b.id)] and len(book.here(deleted=True)) == 2
    assert book.restore([book.record(a.id)]) and book.record(a.id).deleted_at is None
    assert book.rename(b.object_id, "第一丸")
    assert book.flow(b.object_id).name == "第一丸"
    assert book.merge(a.object_id, b.object_id)
    [flow] = book.flows()
    assert flow.name == "第一丸" and len(flow.observations) == 2
    # Saved: read back from the GeoPackage.
    book.reload()
    assert [f.name for f in book.flows()] == ["第一丸"]


def test_renames_moves_and_removes_classes_with_the_records_under_them(book):
    a = book.submit(139.7, 35.6, FormValues(object="A", kind="船舶 > 支援船 > 〇〇級"))
    assert [kind_of(t.path) for t in book.taxonomy()][:4] == ["船舶", "船舶 > 支援船", "船舶 > 支援船 > 〇〇級", "航空機"]
    to = book.update_class(["船舶", "支援船"], name="補助艦", members=["△△"])
    assert to == ["船舶", "補助艦"]
    assert book.record(a.id).kind == "船舶 > 補助艦 > 〇〇級"
    assert "（記録 1 件）" in book.status
    assert book.update_class(["船舶", "補助艦"], parent=["航空機"]) == ["航空機", "補助艦"]
    assert book.record(a.id).kind == "航空機 > 補助艦 > 〇〇級"
    assert book.update_class(["航空機"], name="船舶") is None and "既存" in book.status
    assert book.update_class(["車両"], before=["船舶"]) == ["車両"]
    assert [t.path[0] for t in book.taxonomy() if len(t.path) == 1][:2] == ["車両", "船舶"]
    assert book.remove_class(["航空機", "補助艦"])
    assert book.record(a.id).kind == "航空機"
    assert book.add_class(["船舶", "潜水艦"])
    assert book.set_taxonomy([{"name": "X", "children": ["Y"]}])
    assert [kind_of(t.path) for t in book.kept_taxonomy()] == ["X", "X > Y"]
    # Kept in the store: read back.
    book.reload()
    assert [kind_of(t.path) for t in book.kept_taxonomy()] == ["X", "X > Y"]


def test_lists_images_without_their_file_per_store(book):
    entry = book.list_image("EXT-1", "外部", day(2))
    assert not entry.file and [i.id for i in book.images] == ["d1", "EXT-1", "d3", "dx"]
    rec = book.submit(1, 2, FormValues(object="Z", kind="車両"))
    assert rec.image_id == "EXT-1" and rec.image_time == day(2)
    # Kept in the preferences, by store.
    assert "EXT-1" in book.prefs.get("listed_images/ローカル")
    assert book.unlist_image("EXT-1") and [i.id for i in book.images] == ["d1", "d3", "dx"]
    assert [i.id for i in book.recorded_images()] == ["EXT-1"]


def test_makes_a_report(book):
    book.submit(139.7, 35.6, FormValues(object="A", kind="船舶", confidence="確定"))
    page = book.report(ReportOptions(title="週報"))
    assert "<title>週報</title>" in page and "モノ 1 件" in page and "ローカル" in page and "田中" in page


class TestImageTime:
    def test_reads_text_compact_and_japanese_times(self):
        assert parse_time("2024-05-01T10:00:00Z") == 1714557600000
        assert parse_time("2024-05-01T19:00:00+09:00") == 1714557600000
        assert parse_time("20240501T100000Z") == 1714557600000
        assert parse_time("2024-05-01 10:00") == int(datetime(2024, 5, 1, 10).timestamp() * 1000)
        assert parse_time("2024年5月1日 10時") == int(datetime(2024, 5, 1, 10).timestamp() * 1000)
        assert parse_time("2024-02-31") is None and parse_time("2024") is None and parse_time("abc") is None

    def test_reads_names_metadata_nitf_and_tiff_in_the_viewers_order(self):
        assert time_from_name("S2A_20240501T012345_x.tif") == int(datetime(2024, 5, 1, 1, 23, 45).timestamp() * 1000)
        assert time_from_name("img_2024-05-01.tif") == int(datetime(2024, 5, 1).timestamp() * 1000)
        assert time_from_name("x123456789.tif") is None
        xml = '<GDALMetadata><Item name="PRODUCT">L1</Item><Item name="ACQUISITION_START_TIME">2024-05-01T10:00:00Z</Item></GDALMetadata>'
        assert time_from_metadata(metadata_items_of_xml(xml)) == 1714557600000
        # As the viewer: IMAGING_DATE alone reads first (a local day).
        assert time_from_metadata([("IMAGING_DATE", "2024-05-01"), ("IMAGING_TIME", "10:00:00Z")]) == int(datetime(2024, 5, 1).timestamp() * 1000)
        assert nitf_time("20240501100000") == 1714557600000
        md = {"TIFFTAG_DATETIME": "2020:01:01 00:00:00", "NITF_IDATIM": "20240501100000"}
        assert image_time("x_20230101.tif", md) == (1714557600000, "nitf")
        assert image_time("x_20230101.tif", {"TIFFTAG_DATETIME": "2020:01:01 00:00:00"})[1] == "name"
        assert image_time("x.tif", {"TIFFTAG_DATETIME": "2020:01:01 00:00:00"})[1] == "tiff"
        assert image_time("x.tif", {"ACQUISITION_DATE": "2024-05-01T10:00:00Z", "NITF_IDATIM": "20200101000000"}) == (1714557600000, "metadata")
        assert image_time("x.tif", {}) is None


def test_makes_layers_of_the_records_areas_and_flows(book):
    from satellite_observation.core.obs.output import observation_layers

    assert observation_layers([], []) == []
    a = book.submit(139.7, 35.6, FormValues(object="A", kind="船舶 > 漁船"))
    book.step(1)
    book.carry(a.object_id, True)
    book.add_area(box_ring([139.69, 35.59, 139.72, 35.62]))
    points, areas, lines = observation_layers(book.records, book.areas)
    assert (points.title, points.geometry, len(points.features)) == ("観測記録", "point", 2)
    assert [f.name for f in points.fields][4:6] == ["kind_1", "kind_2"]
    assert points.features[0][1]["kind_2"] == "漁船" and points.features[0][1]["present"] == "あり"
    assert (areas.geometry, areas.features[0][0][0]) == ("polygon", (139.69, 35.59))
    assert (lines.title, lines.end_field, lines.features[0][1]["images"]) == ("観測の流れ", "last", 2)
