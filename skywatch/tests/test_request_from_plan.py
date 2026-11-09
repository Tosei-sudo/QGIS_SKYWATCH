"""
臨時 requests from the 「撮像計画」 dock's opportunities (core/request/from_plan.py;
tsudo, 2026-11-09: 撮像計画と撮像依頼は同じ業務, 保存・追跡も依頼に一本化).
"""
from skywatch.core.plan.access import PlanTarget
from skywatch.core.plan.targets import Shape, target_of
from skywatch.core.request.from_plan import request_of_ops, requests_of_plan, shot_note, target_rings
from skywatch.core.request.model import ImagingRequest, day_bounds, jst_day, square_rings
from skywatch.tests.test_plan_extras import op, sat, utc

SATS = [sat("OPT-1", sar=False), sat("SAR-1", sar=True, mode="スポットライト"), sat("OPT-2", sar=False)]
POLY = [[139.6, 35.6], [139.8, 35.6], [139.8, 35.8], [139.6, 35.8]]
TARGETS = [PlanTarget("東京港", (139.7, 35.7), POLY, rings=[POLY]), PlanTarget("大阪", (135.5, 34.7), [])]
# 11/10 10:30 JST and 11/12 01:00 JST (the day before in UTC).
T1, T2 = utc(2026, 11, 10, 1, 30), utc(2026, 11, 11, 16, 0)


def test_the_area_of_a_target():
    rings, kind = target_rings(TARGETS[0], 10)
    assert kind == "範囲" and rings == [POLY + [POLY[0]]]
    # A line (or points): their box, a little wider.
    line = PlanTarget("線", (139.75, 35.65), [[139.7, 35.6], [139.8, 35.7]])
    (box,), kind = target_rings(line, 10)
    assert kind == "範囲" and box[0][0] < 139.7 and box[2][1] > 35.7 and box[0] == box[-1]
    # A point: the square of the request's point size.
    assert target_rings(TARGETS[1], 4) == (square_rings(135.5, 34.7, 4), "点")


def test_a_layer_feature_keeps_its_polygon():
    ring = POLY + [POLY[0]]
    target = target_of([Shape([tuple(p) for p in ring], [[ring]], (139.7, 35.7))], "港")
    assert target.rings == [ring] and target_rings(target, 10) == ([ring], "範囲")
    assert target_of([Shape([(139.7, 35.7)])], "点").rings == []


def test_shot_note_lists_the_first_shots():
    ops = [op(T2, satellite=1, offNadir=12.34), op(T1, satellite=0)]
    assert shot_note(ops, SATS) == "撮像計画から: 11/10 10:30 OPT-1（オフナディア 10.0°）、11/12 01:00 SAR-1（スポットライト）（オフナディア 12.3°）"
    many = [op(T1 + i * 60000) for i in range(7)]
    assert shot_note(many, SATS).endswith(" ほか 2 回") and shot_note([], SATS) == ""


def test_one_temporary_request_for_a_targets_opportunities():
    base = ImagingRequest(id="old", name="前の", object_id=7, requester="計画係", cloud_no_go=60, series=["OPT"], priority=600)
    r = request_of_ops([op(T2, satellite=2), op(T1, satellite=0), op(T1 + 1, satellite=0)], SATS, TARGETS[0], "R-1", base=base)
    assert (r.id, r.object_id, r.name, r.category, r.achieve, r.status) == ("R-1", None, "東京港", "臨時", "期間内1回", "有効")
    assert (r.start, r.end) == (day_bounds(jst_day(T1))[0], day_bounds(jst_day(T2))[1])
    # The satellites in time order, once each; one kind; the rest from the last inputs.
    assert (r.satellites, r.modes, r.series, r.kind) == (["OPT-1", "OPT-2"], [], [], "光学")
    assert (r.requester, r.cloud_no_go, r.priority) == ("計画係", 60, 600)
    assert base.name == "前の" and base.id == "old"
    mixed = request_of_ops([op(T1, satellite=0), op(T2, satellite=1)], SATS, TARGETS[1], "R-2", base=base)
    assert (mixed.kind, mixed.cloud_no_go, mixed.modes, mixed.shape_kind) == ("任意", None, ["スポットライト"], "点")
    sar_only = request_of_ops([op(T1, satellite=1)], SATS, TARGETS[1], "R-3")
    assert (sar_only.kind, sar_only.cloud_no_go) == ("SAR", None)


def test_one_request_per_target_in_the_targets_order():
    ids = iter(["A", "B", "C"])
    ops = [op(T2, target=1), op(T1, target=0), op(T1 + 5, target=1)]
    made = requests_of_plan(ops, SATS, TARGETS, lambda: next(ids))
    assert [(r.id, r.name) for r in made] == [("A", "東京港"), ("B", "大阪")]
    assert jst_day(made[1].start) == jst_day(T1) and jst_day(made[1].end) == jst_day(T2)
    assert requests_of_plan([], SATS, TARGETS, lambda: "X") == []
    # Nothing of one request is shared with another.
    made[0].satellites.append("X")
    assert made[1].satellites == ["OPT-1"]
