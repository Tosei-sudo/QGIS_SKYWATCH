"""入力の初期値の記憶 for imaging requests (core/request/last.py)."""
import json

from satellite_observation.core.request.last import FIELDS, remembered, with_remembered
from satellite_observation.core.request.model import ImagingRequest


def test_a_new_request_starts_from_the_last_saved_conditions():
    saved = ImagingRequest(id="a", name="大阪湾", category="定点", achieve="毎回", repeat="曜日:月水", status="停止", priority=150,
                           kind="SAR", series=["RADAR"], satellites=["RADAR 1"], modes=["SM"], off_nadir_max=30.0,
                           cloud_no_go=None, resolution=1.0, systems=["SYS-B"], requester="山田", note="メモ",
                           rings=[[[135, 34], [136, 34], [136, 35], [135, 34]]], start=1.0, end=2.0)
    last = json.loads(json.dumps(remembered(saved), ensure_ascii=False))
    assert set(last) == set(FIELDS)
    new = with_remembered(ImagingRequest(id="b"), last)
    assert (new.category, new.achieve, new.repeat, new.priority, new.kind) == ("定点", "毎回", saved.repeat, 150, "SAR")
    assert (new.series, new.satellites, new.modes, new.systems) == (["RADAR"], ["RADAR 1"], ["SM"], ["SYS-B"])
    assert (new.off_nadir_max, new.off_nadir_min, new.resolution, new.requester) == (30.0, None, 1.0, "山田")
    # The new request's own: its id, name, period, area, status and note.
    assert (new.id, new.name, new.status, new.note, new.rings) == ("b", "", "有効", "", [])


def test_values_that_no_longer_fit_are_left_out():
    new = with_remembered(ImagingRequest(id="b"), {"kind": "赤外", "priority": "高", "series": "OPT", "off_nadir_max": "30",
                                                   "satellites": ["OPT 1", "", None, 3], "requester": 5})
    base = ImagingRequest(id="b")
    assert (new.kind, new.priority, new.series, new.off_nadir_max, new.requester) == (base.kind, base.priority, [], None, "")
    assert new.satellites == ["OPT 1", "3"]
    assert with_remembered(base, {}) == base
    assert with_remembered(base, {"priority": 5000}).priority == 1000
