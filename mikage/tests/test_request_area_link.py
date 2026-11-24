"""
Requests made from the organisation's areas (組織の関心地域, tsudo 2026-11-24):
the request keeps the area's 管理番号 and whether its area follows the area's
shape, in the ledger, the history, the layers written out and back, and the
drafts from a request's text.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from mikage.core.request.area_link import (
    MOVED_M, area_links, conditions_of, from_area, links_of, previous_for, rings_moved, unlinked,
)
from mikage.core.request.model import ImagingRequest
from mikage.core.request.rows import field_roles, request_of_row, row_of
from mikage.core.request.store import (
    DEFAULT_HISTORY_FIELDS, DEFAULT_REQUEST_FIELDS, HistoryRow, attributes_of, history_attributes, history_row_of, request_of,
)

RING = [[139.65, 35.55], [139.65, 35.60], [139.70, 35.60], [139.70, 35.55], [139.65, 35.55]]
#: About 0.5 m and 3 m in longitude at 35.6°.
HALF_M, THREE_M = 0.5 / 90_500, 3 / 90_500


@dataclass
class Area:
    id: str = "NA-000101"
    name: str = "横須賀港"
    rings: Optional[list] = None
    owner_group: str = ""
    priority: Optional[int] = None


def test_a_request_from_an_area_takes_its_name_shape_group_and_priority():
    base = ImagingRequest(id="r1", name="", group="第2班", priority=500, kind="SAR")
    r = from_area(base, Area(rings=[RING], owner_group="第1班", priority=300))
    assert (r.name, r.shape_kind, r.named_area_id, r.area_follow) == ("横須賀港", "範囲", "NA-000101", True)
    assert (r.group, r.priority, r.kind) == ("第1班", 300, "SAR") and r.rings == [[list(p) for p in RING]]
    # Without 担当グループ・重要度: the form's.
    r = from_area(base, Area(rings=[RING]))
    assert (r.group, r.priority) == ("第2班", 500)
    # An area not numbered: no link (nothing to find it by).
    assert not from_area(base, Area(id="", rings=[RING])).area_follow
    assert from_area(base, Area(rings=[RING]), follow=False).area_follow is False
    assert unlinked(r).named_area_id == "" and not unlinked(r).area_follow and unlinked(r).rings == r.rings


def test_a_shape_moved_by_a_metre_or_more_is_a_change():
    assert not rings_moved([RING], [RING])
    # Started elsewhere, the other way round, without the closing vertex, and a server's rounding: the same.
    open_ring = RING[:-1]
    rotated = open_ring[1:] + open_ring[:1]
    assert not rings_moved([RING], [rotated + rotated[:1]])
    assert not rings_moved([RING], [rotated[::-1]])
    assert not rings_moved([RING], [RING[:-1]])
    assert not rings_moved([RING], [[[x + HALF_M, y] for x, y in RING]])
    assert MOVED_M == 1.0
    assert rings_moved([RING], [[[x + THREE_M, y] if i == 2 else [x, y] for i, (x, y) in enumerate(RING)]])
    # A vertex or a ring more.
    assert rings_moved([RING], [RING[:2] + [[139.675, 35.61]] + RING[2:]])
    assert rings_moved([RING], [RING, [[140, 35], [140, 35.1], [140.1, 35.1], [140, 35]]])
    # The rings in another order: the same.
    other = [[140, 35], [140, 35.1], [140.1, 35.1], [140, 35]]
    assert not rings_moved([RING, other], [other, RING])


def test_each_areas_requests_are_counted_and_the_last_ones_conditions_offered():
    a = ImagingRequest(id="a", name="1", named_area_id="na-000101", area_follow=True, updated=10, kind="SAR", cloud_no_go=30,
                       group="第2班", priority=700)
    b = ImagingRequest(id="b", name="2", named_area_id="NA-000101", status="停止", updated=20, kind="光学", cloud_no_go=20)
    c = ImagingRequest(id="c", name="3", named_area_id="NA-000102", updated=30)
    d = ImagingRequest(id="d", name="4")
    links = area_links([a, b, c, d])
    assert set(links) == {"NA-000101", "NA-000102"}
    assert (links["NA-000101"].active, links["NA-000101"].total, links["NA-000101"].following) == (1, 2, 1)
    assert links["NA-000101"].text() == "1（全 2）" and links["NA-000102"].text() == "1"
    assert links_of(links, " na-000101 ").total == 2 and links_of(links, "NA-9").total == 0
    # The last saved (but the one being edited).
    assert previous_for([a, b, c, d], "NA-000101").id == "b"
    assert previous_for([a, b, c, d], "NA-000101", leave="b").id == "a"
    assert previous_for([a, b, c, d], "") is None and previous_for([d], "NA-000101") is None
    # Its conditions, but the area's own group and priority.
    form = ImagingRequest(id="new", name="横須賀港", group="第1班", priority=300, kind="任意")
    out = conditions_of(form, a)
    assert (out.kind, out.cloud_no_go, out.group, out.priority, out.name) == ("SAR", 30, "第1班", 300, "横須賀港")


def test_the_ledger_and_the_history_keep_the_link():
    f = DEFAULT_REQUEST_FIELDS
    r = ImagingRequest(id="r1", name="横須賀港", rings=[RING], named_area_id="NA-000101", area_follow=True)
    a = attributes_of(r, f)
    assert (a["NAMED_AREA_ID"], a["AREA_FOLLOW"]) == ("NA-000101", 1)
    back = request_of(a, {"rings": [RING]}, f, "OBJECTID")
    assert (back.named_area_id, back.area_follow) == ("NA-000101", True)
    # Following without an area is not following.
    a = attributes_of(replace(r, named_area_id=""), f)
    assert (a["NAMED_AREA_ID"], a["AREA_FOLLOW"]) == (None, 0)
    assert not request_of({**a, "AREA_FOLLOW": 1}, None, f, "OBJECTID").area_follow
    # A ledger without the attributes: none.
    old = {k: v for k, v in attributes_of(r, f).items() if k not in ("NAMED_AREA_ID", "AREA_FOLLOW")}
    back = request_of(old, None, f, "OBJECTID")
    assert (back.named_area_id, back.area_follow) == ("", False)
    hf = DEFAULT_HISTORY_FIELDS
    row = HistoryRow(run_id="x", run_date=1.0, day="2026-11-24", request_id="r1", named_area_id="NA-000101")
    h = history_attributes(row, hf)
    assert h["NAMED_AREA_ID"] == "NA-000101"
    assert history_row_of(h, None, hf, "OBJECTID").named_area_id == "NA-000101"


def test_a_layer_written_out_and_back_keeps_the_link_until_its_shape_is_changed():
    r = ImagingRequest(id="r1", name="横須賀港", rings=[RING], named_area_id="NA-000101", area_follow=True, start=0, end=1)
    values = row_of(r)
    roles = field_roles(list(values))
    assert roles["namedAreaId"] == "NAMED_AREA_ID" and roles["areaFollow"] == "AREA_FOLLOW"
    back = request_of_row(values, roles, [RING], "範囲", existing=r)
    assert (back.named_area_id, back.area_follow) == ("NA-000101", True)
    # The area changed in the layer: no longer following it (still linked).
    moved = [[x + THREE_M * 10, y] for x, y in RING]
    back = request_of_row(values, roles, [moved], "範囲", existing=r)
    assert (back.named_area_id, back.area_follow) == ("NA-000101", False)
    # A layer without the items keeps the request's.
    bare = {k: v for k, v in values.items() if k not in ("NAMED_AREA_ID", "AREA_FOLLOW")}
    back = request_of_row(bare, field_roles(list(bare)), [RING], "範囲", existing=r)
    assert (back.named_area_id, back.area_follow) == ("NA-000101", True)
    # A new row of a layer without them: none.
    back = request_of_row(bare, field_roles(list(bare)), [RING], "範囲")
    assert (back.named_area_id, back.area_follow) == ("", False)
