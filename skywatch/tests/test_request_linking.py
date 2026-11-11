"""
画像とショットの手動関連付け (tsudo, 2026-11-11: 「手の照合は、今の方法に加えて、
この画像カタログのレコードがこのショットという本当の手動関連付け画面がほしい」):
a day's shots, the catalog's images over each, and linking one by hand.
"""
from __future__ import annotations

from datetime import date

import pytest

from skywatch.core.catalog import catalog_of
from skywatch.core.esri import EsriClient, EsriError
from skywatch.core.plan.names import SatelliteNames
from skywatch.core.request.follow import save_marked, undone_changes
from skywatch.core.request.linking import (
    find_images, gap_text, link_findings, link_text, links_of, search_span, time_text, unlink_findings, units_of, with_links,
)
from skywatch.core.request.model import day_bounds
from skywatch.core.request.store import HistoryRow, ShotRow, load_history, load_requests, save_request
from skywatch.tests.fake_esri import FakeLayer
from skywatch.tests.test_request import CATALOG, FIRST, H, D, TOKYO, request
from skywatch.tests.test_request_quota import SHOTS, _history, _services, _shot_row

DAY = "2026-11-08"
BOX = [[[139.7, 35.4], [139.7, 35.6], [140.0, 35.6], [140.0, 35.4], [139.7, 35.4]]]
OSAKA = [[[135.4, 34.6], [135.5, 34.6], [135.5, 34.7], [135.4, 34.7], [135.4, 34.6]]]


def _row(rid, shot_id="", captured="未判定", **kw) -> HistoryRow:
    return HistoryRow(run_id="RUN", run_date=0, day=kw.pop("day", DAY), request_id=rid, name=kw.pop("name", rid),
                      system=kw.pop("system", "SYS"), satellite=kw.pop("satellite", "OPT 1"), captured=captured, shot_id=shot_id, **kw)


def test_the_shots_of_a_day():
    shot = ShotRow("X-1", "RUN", 0, DAY, system="SYS", satellite="OPT 2", pass_time=FIRST + 22 * H, sensor="OPT", mode="高分解能",
                   rings=TOKYO)
    early = ShotRow("X-2", "RUN", 0, DAY, system="SYS", pass_time=FIRST + 9 * H, rings=OSAKA)
    rows = [_row("B", "X-1", merged_into="A", object_id=2), _row("A", "X-1", object_id=1), _row("C", "X-2", object_id=3),
            # Without a shot: a request and the one put into it are one; the request's second time of the day its own.
            _row("D", object_id=4, system="SYS-B", rings=OSAKA), _row("E", merged_into="D", object_id=5, system="SYS-B"),
            _row("D", object_id=6, system="SYS-B", occurrence=2, rings=OSAKA),
            # The ledger's area when the row has none; 見送り is no shot.
            _row("F", object_id=7, system="SYS-C"), _row("G", "X-9", result="見送り", object_id=8)]
    units = units_of(rows, [shot, early], {"F": request("F", rings=TOKYO)})
    assert [(u.key, [r.request_id for r in u.rows]) for u in units] == [
        ("X-2", ["C"]), ("X-1", ["A", "B"]), ("SYS-B|D|1", ["D", "E"]), ("SYS-B|D|2", ["D"]), ("SYS-C|F|1", ["F"])]
    x1 = units[1]
    assert (x1.satellite, x1.pass_time, x1.sensor_text, x1.names, x1.rings) == ("OPT 2", FIRST + 22 * H, "OPT・高分解能", ["A", "B"], TOKYO)
    assert units[2].rings == OSAKA and units[2].pass_time is None and units[4].rings == TOKYO
    # Compared with the middle of the day when there is no pass.
    assert units[2].target_time() == sum(day_bounds(date(2026, 11, 8))) / 2
    assert (x1.captured, x1.image_id) == ("未判定", "")
    x1.rows[1].captured, x1.rows[1].image_id = "撮像済み", "IMG-7"
    assert (x1.captured, x1.image_id) == ("撮像済み", "IMG-7")


def test_times_and_spans():
    assert time_text(FIRST + 22 * H + 4 * 60000) == "2026-11-08 22:04" and time_text(None) == ""
    assert gap_text(FIRST + 65 * 60000, FIRST) == "+1時間05分"
    assert gap_text(FIRST - 12 * 60000, FIRST) == "−12分"
    assert gap_text(FIRST + 2 * D + 3 * H, FIRST) == "+2日3時間" and gap_text(None, FIRST) == ""
    first, last = search_span(DAY, 1, 2)
    assert first == FIRST - D and last == day_bounds(date(2026, 11, 10))[1]
    assert search_span(DAY, -3, 0) == day_bounds(date(2026, 11, 8))


def _catalog(fake):
    images = fake.add_layer(CATALOG, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString"},
                                               "esriGeometryPolygon"))
    box = {"rings": BOX, "spatialReference": {"wkid": 4326}}
    images.add({"IMAGE_ID": "FAR-TIME", "ACQ": FIRST + 23 * H, "SENSOR": "OPT 1"}, box)
    images.add({"IMAGE_ID": "NEAR", "ACQ": FIRST + 21.5 * H, "SENSOR": "OPT 1"}, box)
    images.add({"IMAGE_ID": "OTHER-SAT", "ACQ": FIRST + 22 * H, "SENSOR": "SAR 5"}, box)
    images.add({"IMAGE_ID": "DAY-BEFORE", "ACQ": FIRST - 2 * H, "SENSOR": "OPT 1"}, box)
    images.add({"IMAGE_ID": "TWO-DAYS-AFTER", "ACQ": FIRST + 2 * D + H, "SENSOR": "OPT 1"}, box)
    images.add({"IMAGE_ID": "ELSEWHERE", "ACQ": FIRST + 22 * H, "SENSOR": "OPT 1"},
               {"rings": OSAKA, "spatialReference": {"wkid": 4326}})
    return catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR"}, "imageFiles": False,
                       "where": "IMAGE_ID <> 'HIDDEN'"}, [], 0)


def test_the_images_over_a_shot():
    fake, store = _services()
    catalog = _catalog(fake)
    client = EsriClient(fake)
    shot = ShotRow("X-1", "RUN", 0, DAY, system="SYS", satellite="OPT 1", pass_time=FIRST + 22 * H, rings=TOKYO)
    [unit] = units_of([_row("A", "X-1", object_id=1)], [shot], {})
    req = {"A": request("A", satellites=["OPT 1"])}
    got = find_images(client, catalog, None, unit, req)
    # Over the area, the day before to the day after; the satellite asked for first, then the nearest to the pass.
    assert [(c.image_id, c.matches) for c in got] == [("NEAR", True), ("FAR-TIME", True), ("DAY-BEFORE", True), ("OTHER-SAT", False)]
    assert got[0].acquired == FIRST + 21.5 * H and got[0].sensor == "OPT 1" and got[0].rings == BOX
    assert [c.image_id for c in find_images(client, catalog, None, unit, req, before=0, after=2)][-1] == "OTHER-SAT"
    assert "TWO-DAYS-AFTER" in {c.image_id for c in find_images(client, catalog, None, unit, req, before=0, after=2)}
    # A request open to any satellite: every image counts; its other names (satelliteAliases) too.
    assert all(c.matches for c in find_images(client, catalog, None, unit, {"A": request("A")}))
    names = SatelliteNames.of([("SAR 5", "SAR-FIVE")])
    [handed] = units_of([_row("A", "X-1", object_id=1, satellite="OPT 9")], [shot], {})
    assert {c.image_id for c in find_images(client, catalog, None, handed, {"A": request("A", satellites=["SAR-FIVE"])}, names=names)
            if c.matches} == {"OTHER-SAT"}
    # The catalog's own condition is kept, and the number read is limited.
    query = [form for url, form in fake.asked if url == f"{CATALOG}/query" and "IMAGE_ID <> 'HIDDEN'" in form.get("where", "")]
    assert query and all("geometry" in form for form in query)
    assert len(find_images(client, catalog, None, unit, req, limit=2)) == 2
    # No area, or no time attribute: nothing can be asked.
    with pytest.raises(EsriError):
        find_images(client, catalog, None, units_of([_row("Z", object_id=9)], [], {})[0], {})
    with pytest.raises(EsriError):
        find_images(client, catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID"}, "imageFiles": False}, [], 0), None, unit, req)


def test_link_and_unlink_a_shot():
    fake, store = _services()
    client = EsriClient(fake)
    save_request(client, store, None, request("R1", achieve="期間内1回", boost=100), FIRST - D)
    save_request(client, store, None, request("R2", achieve="期間内1回"), FIRST - D)
    _history(fake, "R1", "X-1")
    _history(fake, "R2", "X-1", MERGED_INTO="R1")
    _history(fake, "R3", "X-2", "撮像済み", IMAGE_ID="IMG-1")
    _shot_row(fake, "X-1")
    _shot_row(fake, "X-2", captured="撮像済み", IMAGE_ID="IMG-1")
    rows = load_history(client, store, None)
    [unit, other] = units_of(rows, [], {})
    assert unit.key == "X-1" and other.key == "X-2"
    # IMG-1 is linked to another shot already: told before linking it here too.
    links = links_of(client, store, None, ["IMG-1", "IMG-2", ""])
    assert list(links) == ["IMG-1"] and link_text(links["IMG-1"]) == "2026-11-08 SYS R3"
    assert with_links([type("C", (), {"image_id": "IMG-1", "used_by": []})()], links, other)[0].used_by == []
    # Linked: every row of the shot 撮像済み with the image, the ledger 完了, the shot too.
    by_id = {r.id: r for r in load_requests(client, store, None)}
    findings = link_findings(unit, "IMG-2")
    assert [(f.row.request_id, f.captured, f.image_id) for f in findings] == [("R1", "撮像済み", "IMG-2"), ("R2", "撮像済み", "IMG-2")]
    problems = []
    assert save_marked(client, store, None, findings, by_id, rows, FIRST + D, problems) and problems == []
    saved = {r.request_id: (r.captured, r.image_id, r.adoption) for r in load_history(client, store, None)}
    assert saved["R1"] == saved["R2"] == ("撮像済み", "IMG-2", "採用")
    assert {r.id: (r.status, r.boost) for r in load_requests(client, store, None)} == {"R1": ("完了", 0), "R2": ("完了", 0)}
    shot = next(r["attributes"] for r in fake.layers[SHOTS].rows if r["attributes"]["SHOT_ID"] == "X-1")
    assert (shot["CAPTURED"], shot["IMAGE_ID"]) == ("撮像済み", "IMG-2")
    assert link_findings(unit, "IMG-2") == []  # already so
    assert [f.image_id for f in link_findings(unit, "IMG-3")] == ["IMG-3", "IMG-3"]  # another image instead
    # Unlinked (back to 未判定): the rows wait for the follow-up again, the requests 有効 again, the shot 未判定.
    by_id = {r.id: r for r in load_requests(client, store, None)}
    rows = load_history(client, store, None)
    back = unlink_findings(unit)
    assert [(f.captured, f.image_id) for f in back] == [("未判定", ""), ("未判定", "")]
    assert undone_changes(back, by_id, rows) == [(by_id["R1"].object_id, {"status": "有効"}), (by_id["R2"].object_id, {"status": "有効"})]
    assert save_marked(client, store, None, back, by_id, rows, FIRST + D, problems) and problems == []
    assert {r.request_id: (r.captured, r.image_id, r.adoption) for r in load_history(client, store, None) if r.shot_id == "X-1"} == {
        "R1": ("未判定", "", "不明"), "R2": ("未判定", "", "不明")}
    assert {r.status for r in load_requests(client, store, None)} == {"有効"}
    shot = next(r["attributes"] for r in fake.layers[SHOTS].rows if r["attributes"]["SHOT_ID"] == "X-1")
    assert shot["CAPTURED"] == "未判定"
    assert unlink_findings(unit) == [] and [f.captured for f in unlink_findings(unit, "未撮像")] == ["未撮像", "未撮像"]
    with pytest.raises(ValueError):
        unlink_findings(unit, "撮像済み")
