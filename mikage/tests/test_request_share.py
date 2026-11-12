"""
Areas too big for one shot (tsudo, 2026-11-12: 「広域撮像の分配なんだけど、今1回
とったとこって回避されるようになってる？」「効率を考えたら、相互に分担すべき
でしょ？」 → 1〜5): shared out among the system's satellites (分担), each shot a
row of the file with its part of the area, what is taken already (撮像済み)
or handed out on earlier days (撮像待ち) left out, and 完了 only once the
images take 95% of the area.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from mikage.core.catalog import catalog_of
from mikage.core.esri import EsriClient
from mikage.core.request.adoption import answer_changes, match_answers
from mikage.core.request.covered import (
    GOAL, Covered, mask_of, read_covered, samples_of, share_of, subtracts, waiting_outlines, waiting_rows,
)
from mikage.core.request.daily import (
    HELD, PART, TAKEN, WAITING, allocate, cover_text, gather, occurrence_key, part_text,
)
from mikage.core.request.export import Context, raw_value, render, row_rings
from mikage.core.request.follow import follow_and_save, follow_up, found_text, next_step
from mikage.core.request.model import ImagingRequest, bbox_of, day_bounds, in_rings, square_rings
from mikage.core.request.passes import day_passes
from mikage.core.request.result_file import read_answers, result_file_of
from mikage.core.plan.orbit import ecef_of, track_angles, unit
from mikage.core.request.shots import area_box
from mikage.core.request.simulate import SUMMARY, run_passes, simulate
from mikage.core.request.store import (
    DEFAULT_HISTORY_FIELDS, HistoryRow, ShotRow, add_history, history_attributes, history_row_of, load_history, load_requests,
    request_store_of, save_request,
)
from mikage.core.request.systems import export_systems_of
from mikage.core.request.workflow import history_of, keep_answers, plan_day, shots_of
from mikage.tests.fake_esri import FakeEsri, FakeLayer
from mikage.tests.test_request import ledger_layers
from mikage.tests.test_request_passes import BELOW, DAY, H, SAT, T

LEDGER = "https://gis.example/rest/services/R/FeatureServer/0"
HISTORY = "https://gis.example/rest/services/R/FeatureServer/1"
CATALOG = "https://gis.example/rest/services/I/FeatureServer/0"


def _checksum(line: str) -> str:
    total = sum(int(ch) if ch.isdigit() else 1 if ch == "-" else 0 for ch in line[:68])
    return line[:68] + str(total % 10)


def follower(spec, name: str, hours: float = 1.5):
    """A satellite on the same ground track `hours` behind (its plane turned with the Earth): another of the series."""
    l2 = spec.tle2
    turns = float(l2[52:63])
    raan = (float(l2[17:25]) + 15.04 * hours) % 360
    anomaly = (float(l2[43:51]) - 360 * turns * hours / 24) % 360
    l2 = l2[:17] + f"{raan:8.4f}" + l2[25:43] + f"{anomaly:8.4f}" + l2[51:]
    return replace(spec, name=name, id=name, tle2=_checksum(l2))


A = replace(SAT, name="SAT-A", id="SAT-A")
B = follower(A, "SAT-B")


def system(**kw):
    problems: list = []
    [s] = export_systems_of([{"system": "SYS", "series": ["ISS"], "columns": [
        {"key": "ID", "source": "id"}, {"key": "SAT", "source": "satellite"}, {"key": "AREA", "source": "geometry", "geometry": "wkt"},
        {"key": "NO", "source": "shareNo"}, {"key": "OF", "source": "shareCount"}, {"key": "COV", "source": "coverage"},
        {"key": "MINE", "source": "shareCoverage"}, {"key": "LINK", "source": "linkId"}], **kw}], problems)
    assert problems == []
    return s


def wide(rid: str = "W", size: float = 100, **kw) -> ImagingRequest:
    lon, lat = BELOW
    base = dict(id=rid, name="広域", start=T - 6 * H, end=T + 10 * 24 * H, rings=square_rings(lon, lat, size))
    base.update(kw)
    return ImagingRequest(**base)


def fit(reqs, sats=(A, B), systems=None, covered=None, cands=None):
    systems = systems or [system()]
    passes = day_passes(reqs, DAY, list(sats))
    cands = cands if cands is not None else gather(reqs, DAY, list(sats), {}, set(), covered)
    return allocate(cands, systems, list(sats), passes, DAY), passes, systems


def taken(out):
    return [c for c in out if c.result == TAKEN]


def test_a_wide_area_is_shared_among_the_systems_satellites():
    out, passes, _ = fit([wide()])
    rows = taken(out)
    # Before: SAT-A alone took its best strip (8%); with times 1〜4 SAT-B took that very strip again.
    assert [(c.key, c.satellite.name, c.part, c.parts) for c in rows] == [("W", "SAT-A", 1, 2), ("W/2", "SAT-B", 2, 2)]
    a, b = rows
    assert b.main is a and a.main is None and part_text(b) == "分担 2/2"
    # Another strip: the shares add up, the time's coverage on both rows.
    assert a.gain > 0 and b.gain > 0 and abs(a.gain + b.gain - a.coverage) < 1e-9 and b.coverage == a.coverage < GOAL
    assert all(c.warnings == [f"{PART}{round(a.coverage * 100)}%"] for c in rows)
    assert a.shots[0].sat == 0 and b.shots[0].sat == 1 and a.scenes == a.shots[0].frames and b.scenes == b.shots[0].frames
    # One shot of each a satellite: their target cells apart.
    cells = passes.targets["W"].cells
    in_a = {i for i, p in enumerate(cells) if in_rings([a.shots[0].ring()], *p)}
    in_b = {i for i, p in enumerate(cells) if in_rings([b.shots[0].ring()], *p)}
    assert in_a and in_b and len(in_a & in_b) <= 0.2 * min(len(in_a), len(in_b))
    # A system that says `share: false`: its first satellite alone, as before.
    alone = taken(fit([wide()], systems=[system(share=False)])[0])
    assert [(c.key, c.satellite.name, c.part, c.parts) for c in alone] == [("W", "SAT-A", 1, 1)]
    assert alone[0].coverage == alone[0].gain < a.coverage


def test_later_times_get_only_the_passes_left():
    out, _, _ = fit([wide(times_min=1, times_max=4)])
    rows = taken(out)
    assert [c.key for c in rows] == ["W", "W/2"]
    assert [(c.key, c.reason) for c in out if c.result == HELD] == [
        (occurrence_key("W", k), "撮像機会なし（ほかの回との間隔）") for k in (2, 3, 4)]
    # The day counts once (not two times: the shots of one time).
    assert not any(w.startswith("回数が足りない") for c in rows for w in c.warnings)


def test_each_shot_is_a_row_with_its_part_of_the_area():
    req = wide()
    out, _, systems = fit([req])
    [written] = render(out, systems, DAY, T)
    lines = written.text.strip().split("\r\n")
    assert lines[0] == "ID,SAT,AREA,NO,OF,COV,MINE,LINK" and len(lines) == 3 and written.count == 2
    rows = taken(out)
    ctx = Context(systems[0], DAY, T)
    for c in rows:
        ring = row_rings(c, systems[0].merge_margin)[0]
        shot = c.shots[0]
        # Inside the shot's scenes (by the margin) and inside the request's box along the track: not the whole area.
        assert all(in_rings([shot.ring()], *p) for p in ring[:-1])
        x0, x1, y0, y1 = area_box(req, shot.frame)
        for p in ring:
            x, y = track_angles(unit(ecef_of(p)), shot.frame)
            assert x0 - 1e-9 <= x <= x1 + 1e-9 and y0 - 1e-9 <= y <= y1 + 1e-9
        assert raw_value(c, "geometry", ctx, geometry="wkt").startswith("POLYGON ((")
        assert raw_value(c, "geometry", ctx, geometry="wkt") != raw_value(replace(c, part=0), "geometry", ctx, geometry="wkt")
        assert raw_value(c, "shareNo", ctx) == c.part and raw_value(c, "shareCount", ctx) == 2
        assert raw_value(c, "coverage", ctx) == round(c.coverage * 100, 1)
        assert raw_value(c, "shareCoverage", ctx) == round(c.gain * 100, 1)
    # Each its own 外部連携ID, the first one's as before sharing (part 1 is the plain request).
    assert len({raw_value(c, "linkId", ctx) for c in rows}) == 2
    # A row of the whole area says nothing of shares.
    small = taken(fit([wide("S", size=5)])[0])
    assert [(c.part, raw_value(c, "shareNo", Context(systems[0], DAY, T)), raw_value(c, "coverage", Context(systems[0], DAY, T)))
            for c in small] == [(0, None, None)]
    # The history: a row a shot, its area the part (as the file), its shot.
    plan = plan_day([req], DAY, systems, [A, B], {}, passes=day_passes([req], DAY, [A, B]))
    hist = history_of(plan, T, {"SYS": "SYS.csv"}, "RUN")
    assert [(h.request_id, h.part, h.satellite, h.shot_id) for h in hist] == [("W", 1, "SAT-A", "RUN-1"), ("W", 2, "SAT-B", "RUN-2")]
    assert hist[0].rings == row_rings(plan.candidates[0], systems[0].merge_margin)
    assert len({h.link_id for h in hist}) == 2
    shots = shots_of(plan, T, {"SYS": "SYS.csv"}, "RUN")
    assert [(s.shot_id, s.satellite, s.request_ids) for s in shots] == [("RUN-1", "SAT-A", ["W"]), ("RUN-2", "SAT-B", ["W"])]
    assert abs(shots[0].coverage + shots[1].coverage - plan.candidates[0].coverage) < 1e-9
    # Kept in the table: PART_NO (0 / empty for a row of the whole area).
    a = history_attributes(hist[1], DEFAULT_HISTORY_FIELDS)
    assert a["PART_NO"] == 2 and history_row_of(a, None, DEFAULT_HISTORY_FIELDS, "OBJECTID").part == 2
    assert history_row_of({**a, "PART_NO": None}, None, DEFAULT_HISTORY_FIELDS, "OBJECTID").part == 0
    # Written again: each shot's answer kept on its own row.
    earlier = [replace(h, object_id=i, adoption="採用" if h.part == 1 else "不採用") for i, h in enumerate(hist, 1)]
    again = keep_answers(earlier, hist)
    assert [(r.part, r.adoption) for r in again.rows] == [(1, "採用"), (2, "不採用")]


def test_a_shot_left_out_by_hand_stays_out():
    req = wide()
    out, passes, systems = fit([req])
    a, b = taken(out)
    b.manual = HELD
    again = allocate(out, systems, [A, B], passes, DAY)
    rows = taken(again)
    assert [c.key for c in rows] == ["W"] and rows[0].parts == 1 and b.pass_key in rows[0].skip
    held = [c for c in again if c.result == HELD]
    assert held == [b] and b.reason == "手動で見送り"
    # Taken again by hand: shared out again.
    b.manual = TAKEN
    assert [c.key for c in taken(allocate(again, systems, [A, B], passes, DAY))] == ["W", "W/2"]


def test_what_is_taken_or_waiting_is_left_out():
    req = wide()
    first, passes, _ = fit([req], sats=(A,))
    [a] = taken(first)
    strip = [[a.shots[0].ring()]]
    # That strip taken already: the day's shot takes another part, and the coverage counts both.
    got = Covered(taken=strip)
    out, _, _ = fit([req], sats=(A,), covered={"W": got})
    [c] = taken(out)
    cells = passes.targets["W"].cells
    before = mask_of(cells, strip)
    mine = mask_of(cells, [[c.shots[0].ring()]])
    assert c.gain > 0 and sum(1 for x, y in zip(before, mine) if x and y) <= 0.2 * sum(mine)
    assert abs(c.coverage - (sum(before) / len(cells) + c.gain)) < 1e-9
    assert c.warnings == [f"{PART}{round(c.coverage * 100)}%（撮像済み・撮像待ちを含む）"]
    assert cover_text(c).startswith("撮像済み 約 ") and cover_text(c).endswith(" を除く")
    # The same as 撮像待ち (handed out yesterday, not followed up yet).
    out, _, _ = fit([req], sats=(A,), covered={"W": Covered(waiting=strip, waiting_days=["2026-11-11"])})
    [w] = taken(out)
    assert w.shots[0].ring() == c.shots[0].ring() and "撮像待ち 約 " in cover_text(w)
    # Near its deadline the waiting part is not left out (its shots go again as insurance).
    near = replace(req, end=T + 6 * H)
    cands = gather([near], DAY, [A], {}, set(), {"W": Covered(waiting=strip, waiting_days=["2026-11-11"])})
    assert cands[0].near
    [n] = taken(allocate(cands, [system()], [A], passes, DAY))
    assert n.shots[0].ring() == a.shots[0].ring()


def test_taken_or_waiting_whole_is_held_back():
    req = wide("S", size=5)
    whole = [req.rings]
    [c] = gather([req], DAY, [A], {}, set(), {"S": Covered(taken=whole)})
    assert c.held == "撮像済み（範囲の約 100%）"
    [c] = gather([req], DAY, [A], {}, set(), {"S": Covered(waiting=whole, waiting_days=["2026-11-10", "2026-11-11"])})
    assert c.held == f"{WAITING}11/10・11/11 の出力で範囲の約 100%）"
    out, _, _ = fit([req], sats=(A,), cands=[c])
    assert out[0].result == HELD and out[0].reason == c.held
    # Taken by hand: it goes, the whole area again.
    c.manual = TAKEN
    out, _, _ = fit([req], sats=(A,), cands=[c])
    assert out[0].result == TAKEN and f"{c.held}（手動で採用）" in out[0].warnings
    # Near its deadline: it goes, with a word.
    near = replace(req, end=T + 6 * H)
    [c] = gather([near], DAY, [A], {}, set(), {"S": Covered(waiting=whole, waiting_days=["2026-11-11"])})
    assert c.held == "" and c.warnings == [f"{WAITING}11/11 の出力で範囲の約 100%）・期限間近のため出力"]
    # Only 期間内1回 once a day: 毎回 and several times a day take the whole area each time.
    assert subtracts(req) and not subtracts(replace(req, achieve="毎回")) and not subtracts(replace(req, times_min=2))
    [c] = gather([replace(req, achieve="毎回")], DAY, [A], {}, set(), {"S": Covered(taken=whole)})
    assert c.held == "" and c.covered is None


def _row(rid, day, **kw):
    return HistoryRow("RUN", T, day, rid, "SYS", "SAT-A", **kw)


def test_waiting_is_what_earlier_days_handed_out_and_not_followed_up():
    req = wide()
    requests = {"W": req, "E": replace(req, id="E", achieve="毎回")}
    two, one = (DAY - timedelta(days=n) for n in (2, 1))
    rows = [_row("W", two.isoformat(), shot_id="S1"), _row("W", two.isoformat(), shot_id="S2", adoption="不採用"),
            _row("W", DAY.isoformat(), shot_id="S3"), _row("W", (DAY - timedelta(days=3)).isoformat(), captured="撮像済み"),
            _row("W", one.isoformat(), part=2, rings=[[[1, 1], [2, 1], [2, 2], [1, 1]]]), _row("E", two.isoformat()),
            _row("W", one.isoformat(), result="見送り")]
    waiting = waiting_rows(rows, requests, DAY)
    # Not the one answered 不採用, nor the day's own, nor one settled, nor a 毎回 request's, nor one held back.
    assert [(r.day, r.shot_id, r.part) for r in waiting] == [(two.isoformat(), "S1", 0), (one.isoformat(), "", 2)]
    shot = ShotRow(shot_id="S1", run_id="RUN", run_date=T, day=two.isoformat(), system="SYS", rings=[[[0, 0], [1, 0], [1, 1], [0, 0]]])
    outlines = waiting_outlines(waiting, requests, {"S1": shot})
    assert outlines == {"W": [[shot.rings[0]], [[[1, 1], [2, 1], [2, 2], [1, 1]]]]}
    # Without its shot: the area its row was written with, else the request's.
    assert waiting_outlines([_row("W", two.isoformat(), shot_id="S9")], requests, {}) == {"W": [req.rings]}


def _store():
    problems: list = []
    store = request_store_of({"label": "依頼", "url": LEDGER, "history": HISTORY, "catalog": "画像"}, problems, 0)
    assert problems == []
    return store


def _server(polygons: bool = False):
    fake = ledger_layers(FakeEsri())
    if polygons:
        fake.layers[HISTORY].geometry = "esriGeometryPolygon"
    fake.layers[HISTORY].types["PART_NO"] = "esriFieldTypeInteger"
    fake.add_layer(CATALOG, FakeLayer({"IMAGE_ID": "esriFieldTypeString", "ACQ": "esriFieldTypeDate", "SENSOR": "esriFieldTypeString",
                                       "URL": "esriFieldTypeString"}, "esriGeometryPolygon"))
    return fake


def _image(fake, iid, rings, at=T, sensor="SAT-A"):
    fake.layers[CATALOG].add({"IMAGE_ID": iid, "ACQ": at, "SENSOR": sensor}, {"rings": rings, "spatialReference": {"wkid": 4326}})


def _catalog():
    return catalog_of({"label": "画像", "url": CATALOG, "fields": {"id": "IMAGE_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "URL"}}, [], 0)


def _half(req, east: bool):
    w, s, e, n = bbox_of(req.rings)
    mid = (w + e) / 2
    a, b = (mid, e) if east else (w, mid)
    return [[[a, s - 0.01], [a, n + 0.01], [b, n + 0.01], [b, s - 0.01], [a, s - 0.01]]]


def test_follow_up_keeps_a_partly_taken_area_open():
    fake, store = _server(), _store()
    client = EsriClient(fake)
    req = replace(wide("W", size=20), boost=100, achieve="期間内1回", start=T - 6 * H, end=T + 5 * 86400000)
    save_request(client, store, None, req, T - 86400000)
    fake.layers[HISTORY].add({"REQ_ID": "W", "TARGET_DAY": DAY.isoformat(), "RESULT": "採用", "CAPTURED": "未判定", "SYSTEM": "SYS",
                              "SATELLITE": "SAT-A"})
    _image(fake, "IMG-W", _half(req, east=False))
    history = load_history(client, store, None)
    by_id = {r.id: r for r in load_requests(client, store, None)}
    found, changed = follow_and_save(client, store, None, _catalog(), None, history, by_id, T + 86400000)
    # Its image takes half of it: the raise undone, not 完了 (the rest goes out on the next days).
    assert [(f.captured, f.image_id) for f in found] == [("撮像済み", "IMG-W")] and changed
    assert 0.4 < found[0].coverage < 0.6
    saved = load_requests(client, store, None)[0]
    assert (saved.boost, saved.status) == (0, "有効")
    assert "範囲の一部だけ撮像済みの依頼 1 件は完了にせず" in found_text(found)
    assert next_step(load_history(client, store, None)[0], saved, DAY) == "撮像済み（範囲の残りは次の出力へ）"
    # What the next day reads: the half taken, so the day's work leaves it out.
    covered, notes = read_covered(client, store, None, _catalog(), None, [saved], DAY, [], None)
    assert notes == [] and 0.4 < share_of(samples_of(saved), covered["W"].taken) < 0.6
    # The other half comes in on the next day's row: now 完了.
    nxt = DAY.replace(day=DAY.day + 1)
    fake.layers[HISTORY].add({"REQ_ID": "W", "TARGET_DAY": nxt.isoformat(), "RESULT": "採用", "CAPTURED": "未判定", "SYSTEM": "SYS",
                              "SATELLITE": "SAT-A"})
    _image(fake, "IMG-E", _half(req, east=True), at=day_bounds(nxt)[0] + 10 * H)
    history = [h for h in load_history(client, store, None) if h.captured == "未判定"]
    found, changed = follow_and_save(client, store, None, _catalog(), None, history, {saved.id: saved}, day_bounds(nxt)[1] + H)
    assert [f.image_id for f in found] == ["IMG-E"] and found[0].coverage >= GOAL and changed
    assert load_requests(client, store, None)[0].status == "完了"


def test_an_image_without_a_shape_takes_the_whole_area():
    fake, store = _server(), _store()
    client = EsriClient(fake)
    req = replace(wide("W", size=20), start=T - 6 * H, end=T + 5 * 86400000)
    save_request(client, store, None, req, T - 86400000)
    fake.layers[HISTORY].add({"REQ_ID": "W", "TARGET_DAY": DAY.isoformat(), "RESULT": "採用", "CAPTURED": "未判定"})
    _image(fake, "IMG-1", _half(req, east=False))
    # The catalog gives no shapes back (a view of images without them): the image counts for the whole area, as before.
    ask = client.ask

    def shapeless(url, params, token=None, *args, **kw):
        got = ask(url, params, token, *args, **kw)
        if url.startswith(CATALOG) and params.get("returnGeometry") == "true":
            got = {**got, "features": [{"attributes": f.get("attributes")} for f in got.get("features") or []]}
        return got

    client.ask = shapeless
    history = load_history(client, store, None)
    by_id = {r.id: r for r in load_requests(client, store, None)}
    found, changed = follow_and_save(client, store, None, _catalog(), None, history, by_id, T + 86400000)
    assert changed and [f.captured for f in found] == ["撮像済み"] and found[0].coverage == 1.0
    assert load_requests(client, store, None)[0].status == "完了"


def test_each_shot_row_is_followed_up_on_its_own_part():
    fake, store = _server(polygons=True), _store()
    client = EsriClient(fake)
    req = replace(wide("W", size=20), start=T - 6 * H, end=T + 5 * 86400000)
    save_request(client, store, None, req, T - 86400000)
    west, east = _half(req, east=False), _half(req, east=True)
    rows = [HistoryRow("RUN", T, DAY.isoformat(), "W", "SYS", "SAT-A", part=1, rings=west),
            HistoryRow("RUN", T, DAY.isoformat(), "W", "SYS", "SAT-B", part=2, rings=east)]
    assert add_history(client, store, None, rows) == 2
    # Only the west half was taken: the east shot's row is still waiting (not settled by the west's image).
    _image(fake, "IMG-W", [[[p[0], p[1]] for p in west[0]]])
    assert load_history(client, store, None)[1].rings == []
    history = load_history(client, store, None, shapes=True)
    assert [h.part for h in history] == [1, 2] and history[1].rings
    by_id = {r.id: r for r in load_requests(client, store, None)}
    found = follow_up(client, _catalog(), None, history, by_id, T + 86400000, 3)
    assert [(f.row.part, f.captured, f.image_id) for f in found] == [(1, "撮像済み", "IMG-W")]
    # One image over both parts settles one of them only.
    _image(fake, "IMG-ALL", [[[p[0], p[1]] for p in req.rings[0]]], sensor="SAT-B")
    found = follow_up(client, _catalog(), None, history, by_id, T + 86400000, 3)
    assert sorted((f.row.part, f.image_id) for f in found) == [(1, "IMG-W"), (2, "IMG-ALL")]


def test_a_shots_link_id_answers_that_shot_only():
    spec = result_file_of({"format": "csv", "columns": [{"key": "REF", "target": "linkId"}, {"key": "STATUS", "target": "result"},
                                                         {"key": "REQ", "target": "id"}]}, "r", [])
    day = DAY.isoformat()
    rows = [HistoryRow("RUN", T, day, "W", "SYS", "SAT-A", part=1, link_id="AAAA", object_id=1, adoption="不明"),
            HistoryRow("RUN", T, day, "W", "SYS", "SAT-B", part=2, link_id="BBBB", object_id=2, adoption="不明")]
    reqs = [ImagingRequest("W", name="広域")]

    def answers(text):
        got, problems = read_answers(text.encode("utf-8"), spec)
        assert problems == []
        return got

    reading = match_answers(answers("REF,STATUS,REQ\nAAAA,OK,W\nBBBB,NG,W\n"), rows, reqs, "SYS", day, spec, "r.csv")
    assert reading.problems == [] and reading.missing == []
    assert {oid: v["adoption"] for oid, v in answer_changes(reading)} == {1: "採用", 2: "不採用"}
    # The request's id alone answers every shot of it; a shot's own row wins over it.
    reading = match_answers(answers("REF,STATUS,REQ\n,OK,W\nBBBB,NG,\n"), rows, reqs, "SYS", day, spec, "r.csv")
    assert {oid: v["adoption"] for oid, v in answer_changes(reading)} == {1: "採用", 2: "不採用"}


def test_the_simulation_hands_out_the_rest_on_the_next_days():
    req = replace(wide(), start=T - 6 * H, end=T + 30 * H)
    passes = run_passes([req], DAY, 2, [A, B])
    sim = simulate([req], DAY, 2, [system()], [A, B], passes=passes)
    first, second = sim.days
    # Shared out (one request taken, two rows), only part of it: on to the next day, not 完了.
    assert (first.taken, first.partial, first.used["SYS"][0]) == (1, 1, 2)
    assert second.candidates == 1
    assert dict((label, f(sim)) for label, f in SUMMARY)["範囲の一部だけ（残りを翌日以降へ）"] >= 1
