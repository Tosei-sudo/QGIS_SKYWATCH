"""観測記録の一括取込み without the screen: column guessing, the dry run, the import with the store's checks."""
from dataclasses import replace

from mikage.core.esri import EsriClient
from mikage.core.obs import record_import as ri
from mikage.core.obs.csv import observations_csv
from mikage.core.obs.model import observation_store_of
from mikage.core.obs.stores import EsriObservationStore, LocalObservationStore
from mikage.core.obs.taxonomy import Taxon
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_bulk_edit import _book
from mikage.tests.test_obs import ALL, DATES, URL, day, obs


def rows(*dicts, points=None):
    return [ri.SourceRow(i + 2, d, *(points[i] if points else (None, None))) for i, d in enumerate(dicts)]


def test_columns_are_guessed_from_the_plugins_own_exports_and_usual_names():
    standard = ["観測ID", "モノID", "モノ", "種別", "分類1", "活動", "有無", "確度", "撮像日時", "画像ID", "画像", "経度", "緯度", "長さ（m）", "向き（度）",
                "幅（m）", "備考"]
    m = ri.guess_mapping(standard)
    assert m == {"id": "観測ID", "image_id": "画像ID", "image_name": "画像", "image_time": "撮像日時", "object": "モノ", "kind": "種別",
                 "activity": "活動", "present": "有無", "confidence": "確度", "note": "備考", "length": "長さ（m）", "heading": "向き（度）",
                 "width": "幅（m）", "lon": "経度", "lat": "緯度"}
    m = ri.guess_mapping(["OBS_ID", "IMAGE_ID", "OBJECT_NAME", "KIND", "LON", "LAT", "LENGTH_M", "HEADING_DEG"])
    assert m["id"] == "OBS_ID" and m["object"] == "OBJECT_NAME" and m["heading"] == "HEADING_DEG" and m["lon"] == "LON"
    assert ri.guess_mapping(["scene_id", "class", "x", "y"]) == {"image_id": "scene_id", "kind": "class", "lon": "x", "lat": "y"}


def test_the_dry_run_tells_new_duplicates_errors_and_continues_known_things():
    there = [obs(id="r1", object_id="T1", object_name="あおば", kind="船舶 > 護衛艦", lon=139.7, lat=35.6)]
    mapping = {"image_id": "img", "object": "name", "kind": "kind", "confidence": "conf", "present": "p", "length": "len"}
    points = [(139.8, 35.7), (139.7, 35.6), (139.9, 35.5), (140.0, 35.5), (140.0, 35.5), (139.0, 35.0), (139.0, 35.0), (None, None),
              (139.90001, 35.50001)]
    source = rows(
        {"img": "d2", "name": "あおば", "kind": "船舶 > 護衛艦", "conf": "", "p": "あり", "len": "120"},
        {"img": "img", "name": "あおば", "kind": "船舶 > 護衛艦", "conf": "", "p": "", "len": ""},
        {"img": "d2", "name": "", "kind": "船舶", "conf": "確定", "p": "なし", "len": ""},
        {"img": "d2", "name": "新しい船", "kind": "未知", "conf": "", "p": "", "len": ""},
        {"img": "d3", "name": "新しい船", "kind": "未知", "conf": "", "p": "", "len": ""},
        {"img": "", "name": "x", "kind": "船舶", "conf": "", "p": "", "len": ""},
        {"img": "d2", "name": "y", "kind": "船舶", "conf": "ほぼ", "p": "?", "len": "長い"},
        {"img": "d2", "name": "z", "kind": "船舶", "conf": "", "p": "", "len": ""},
        {"img": "d2", "name": "", "kind": "船舶", "conf": "", "p": "", "len": ""},
        points=points)
    planned = ri.plan(source, mapping, there, ["確定", "推定"], [Taxon(["船舶"]), Taxon(["船舶", "護衛艦"])], image_times={"d2": day(2)})
    assert [p.action for p in planned] == ["new", "duplicate", "new", "new", "new", "error", "error", "error", "duplicate"]
    first = planned[0].record
    assert (first.object_id, first.object_name, first.image_time, first.length) == ("T1", "あおば", day(2), 120.0)
    third = planned[2].record
    assert third.object_name == "船舶-1" and not third.present and third.activity == "" and third.confidence == "確定"
    assert planned[3].warnings == ["分類体系にない種別「未知」"] and planned[3].record.object_id == planned[4].record.object_id
    assert planned[5].reason == "画像IDが空"
    assert planned[6].reason == "長さ（m）が数でない、有無が読めない（?）、確度「ほぼ」なし（確定・推定）"
    assert "位置がない" in planned[7].reason
    assert ri.summary(planned) == "新規 4 件、重複（飛ばす） 2 件、エラー 3 件（注意 2 件）"


def test_records_with_their_id_are_updated_only_when_asked():
    there = [obs(id="r1", kind="船舶", activity="停泊", note=""), obs(id="r2", deleted_at=day(3))]
    mapping = {"id": "ID", "image_id": "IMG", "activity": "ACT", "note": "NOTE", "lon": "X", "lat": "Y"}
    source = rows({"ID": "r1", "IMG": "img", "ACT": "航行", "NOTE": "直した", "X": "139.7", "Y": "35.6"},
                  {"ID": "r2", "IMG": "img", "ACT": "航行", "NOTE": "", "X": "139.7", "Y": "35.6"},
                  {"ID": "r1", "IMG": "img", "ACT": "停泊", "NOTE": "", "X": "139.7", "Y": "35.6"},
                  {"ID": "r9", "IMG": "img2", "ACT": "停泊", "NOTE": "", "X": "139.71", "Y": "35.61"})
    left = ri.plan(source, mapping, there)
    assert [p.action for p in left] == ["existing", "error", "existing", "new"]
    assert left[3].warnings == ["観測ID r9 の記録はない（新規として取り込む）"]
    changed = ri.plan(source, mapping, there, update_existing=True)
    assert [p.action for p in changed] == ["update", "error", "same", "new"]
    assert changed[0].changes == {"activity": ("停泊", "航行"), "note": ("", "直した")} and changed[0].record.id == "r1"
    assert changed[1].reason == "削除済みの記録"


def test_the_plugins_own_csv_goes_back_in(tmp_path):
    record = obs(id="r1", object_name="あおば", kind="船舶 > 護衛艦", length=120.0, heading=45.0, confidence="確定")
    path = tmp_path / "observations.csv"
    path.write_text(observations_csv([record]), encoding="utf-8")
    columns, source = ri.read_csv(str(path))
    assert columns[0] == "観測ID" and len(source) == 1 and source[0].number == 2
    mapping = ri.guess_mapping(columns)
    # Into an empty store: a new record of the same thing, image and time.
    [p] = ri.plan(source, mapping, [])
    assert p.action == "new"
    o = p.record
    assert (o.image_id, o.image_name, o.image_time, o.object_name, o.kind, o.length, o.heading, o.confidence, o.lon, o.lat) == (
        "img", "img.tif", day(1), "あおば", "船舶 > 護衛艦", 120.0, 45.0, "確定", 139.7, 35.6)
    # Into the store it came from: the record is there.
    assert ri.plan(source, mapping, [record])[0].action == "existing"
    # Tab separated, Shift_JIS.
    path.write_bytes("画像ID\t経度\t緯度\t種別\nimg9\t140\t36\t航空機\n".encode("cp932"))
    columns, source = ri.read_csv(str(path))
    assert columns == ["画像ID", "経度", "緯度", "種別"] and source[0].values["種別"] == "航空機"


def test_the_import_saves_in_one_go_and_keeps_others_changes(tmp_path):
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in ALL + ["CONFIDENCE", "UPDATED_BY", "RECORDED_BY"]}))
    config = observation_store_of({"url": URL}, [], 0)
    mine, theirs = EsriObservationStore(config, EsriClient(fake)), EsriObservationStore(config, EsriClient(fake))
    mine.save([obs(id="a", note=""), obs(id="b", note="", object_id="B", object_name="船舶-2", lon=139.8)])
    said = []
    book, _db = _book(mine, tmp_path, said)
    b = next(o for o in theirs.load() if o.id == "b")
    theirs.save([replace(b, note="ほかの人", updated_at=day(25))])
    mapping = {"id": "ID", "image_id": "IMG", "note": "NOTE", "lon": "X", "lat": "Y"}
    source = rows({"ID": "a", "IMG": "img", "NOTE": "取込", "X": "", "Y": ""}, {"ID": "b", "IMG": "img", "NOTE": "取込", "X": "", "Y": ""},
                  {"ID": "", "IMG": "d5", "NOTE": "新規", "X": "141", "Y": "36"})
    planned = ri.plan(source, mapping, book.records, update_existing=True)
    assert [p.action for p in planned] == ["update", "update", "new"]
    outcome = ri.apply(book, planned)
    assert (outcome.added, outcome.updated, outcome.conflicts) == (1, 1, 1)
    assert said[-1].startswith("新規 1 件・更新 1 件を保存、ほかの人が先に変えた 1 件は上書きせず")
    saved = {o.id: o for o in theirs.load()}
    assert saved["a"].note == "取込" and saved["b"].note == "ほかの人" and len(saved) == 3
    new = next(o for o in saved.values() if o.id not in ("a", "b"))
    assert (new.image_id, new.note) == ("d5", "新規") and next(o for o in book.records if o.id == new.id).recorded_by == "田中"
    assert {o.id for o in book.records} == set(saved)


def test_a_local_store_takes_the_import(tmp_path):
    from mikage.core.gpkg import open_local

    db = open_local(str(tmp_path / "s.gpkg"))
    said = []
    book, _db = _book(LocalObservationStore(db), tmp_path, said)
    planned = ri.plan(rows({"IMG": "d1", "K": "船舶"}, points=[(139.7, 35.6)]), {"image_id": "IMG", "kind": "K"}, book.records)
    out = ri.apply(book, planned)
    assert out.added == 1 and book.records[0].object_name == "船舶-1"
    assert ri.apply(book, []).added == 0
