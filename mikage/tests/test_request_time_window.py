"""
時間帯を時刻で指定 (tsudo, 2026-11-26: 「撮像要求に時間帯の指定を、具体的に何時から
何時でできるようにしたい」; 日本時間だけ): a request's 時間帯 「時刻」 takes its shots
only on passes whose time (JST) is from … to, one ending before it starts
running past midnight. Kept in the ledger as TIME_FROM / TIME_TO, handed to
the systems as timeFrom / timeTo, read from texts (「10時から14時」).
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from mikage.core.esri import EsriClient, EsriError
from mikage.core.request.chances import form_text
from mikage.core.request.daily import NO_PASS, OFF_CLOCK, TAKEN, gather
from mikage.core.request.export import Context, raw_value
from mikage.core.request.last import remembered, with_remembered
from mikage.core.request.model import (
    BY_CLOCK, JST, TIMES_OF_DAY, ImagingRequest, clock_minutes, clock_text, in_window, jst_day, window_text,
)
from mikage.core.request.passes import day_passes, find_passes
from mikage.core.request.rows import field_roles, request_of_row, row_of
from mikage.core.request.store import DEFAULT_REQUEST_FIELDS, attributes_of, load_requests, request_of, request_store_of, save_request
from mikage.core.text_request import Clock, applied, clock_of, request_draft, value_text
from mikage.tests.fake_esri import FakeEsri
from mikage.tests.test_plan import BELOW, T
from mikage.tests.test_request import FIRST, LEDGER, ledger_layers, request
from mikage.tests.test_request_passes import SAT, fit, point, system

H = 3600000
MIN = 60000


def jst_minutes(ms: float) -> int:
    t = datetime.fromtimestamp(ms / 1000, tz=JST)
    return t.hour * 60 + t.minute


def clocked(req: ImagingRequest, first: int, last: int) -> ImagingRequest:
    req.time_of_day, req.time_from, req.time_to = BY_CLOCK, clock_text(first % 1440), clock_text(last % 1440)
    return req


# ---- the clock ----------------------------------------------------------------------------------------------

def test_times_are_read_as_minutes_of_the_day():
    assert clock_minutes("10:00") == 600 and clock_minutes("9:30") == 570 and clock_minutes("１４：０５") == 845
    assert clock_minutes("24:00") == 1440 and clock_minutes("10時") == 600
    assert clock_minutes("") is None and clock_minutes("25:00") is None and clock_minutes("10:75") is None
    assert clock_minutes(None) is None and clock_minutes("あさ") is None
    assert clock_text(570) == "09:30" and clock_text(None) == ""
    assert BY_CLOCK in TIMES_OF_DAY


def test_a_window_holds_its_ends_and_may_run_past_midnight():
    day = datetime(2026, 11, 27, tzinfo=JST)

    def at(h, m=0):
        return (day.timestamp() + h * 3600 + m * 60) * 1000

    r = clocked(ImagingRequest(id="R"), 600, 840)
    assert r.window == (600, 840) and window_text(r) == "10:00〜14:00"
    assert in_window(r, at(10)) and in_window(r, at(14)) and in_window(r, at(12, 30))
    assert not in_window(r, at(9, 59)) and not in_window(r, at(14, 1))
    night = clocked(ImagingRequest(id="N"), 22 * 60, 2 * 60)
    assert in_window(night, at(23)) and in_window(night, at(1, 30)) and not in_window(night, at(12))
    # Not by the clock (or no times that read): any time.
    assert ImagingRequest(id="A").window is None and in_window(ImagingRequest(id="A"), at(3))
    broken = ImagingRequest(id="B", time_of_day=BY_CLOCK, time_from="10:00", time_to="")
    assert broken.window is None and window_text(broken) == "任意"
    assert window_text(ImagingRequest(id="D", time_of_day="昼")) == "昼"


# ---- the passes ---------------------------------------------------------------------------------------------

def test_only_the_passes_inside_its_window_are_used():
    at = jst_minutes(T)
    inside = clocked(point(1, BELOW), at - 30, at + 30)
    outside = clocked(point(2, BELOW), at + 120, at + 180)
    plain = point(3, BELOW)
    passes = day_passes([inside, outside, plain], jst_day(T), [SAT])
    assert passes.of(inside, jst_day(T)) and passes.of(plain, jst_day(T))
    assert not passes.of(outside, jst_day(T))
    # Left out only for its 時刻: kept, to say so.
    assert passes.off_clock_on(outside, jst_day(T)) and not passes.off_clock_on(inside, jst_day(T))


def test_a_request_with_passes_only_outside_its_window_is_told_so():
    at = jst_minutes(T)
    outside = clocked(point(1, BELOW), at + 120, at + 180)
    inside = clocked(point(2, BELOW), at - 10, at + 10)
    out, _ = fit([outside, inside])
    got = {c.request.id: c for c in out}
    assert got["R1"].result != TAKEN and got["R1"].reason == OFF_CLOCK == "撮像機会なし（指定の時刻にパスなし）"
    assert got["R2"].result == TAKEN and abs(got["R2"].planned - T) < MIN


def test_a_window_past_midnight_takes_the_pass_inside_it():
    at = jst_minutes(T)
    # The pass at T inside a window that runs over midnight from 1 minute before T (or the day before).
    r = clocked(point(1, BELOW), at - 1, at - 2)
    out, _ = fit([r])
    assert out[0].result == TAKEN


def test_without_a_pass_over_it_the_reason_stays_no_pass():
    # Far north of the satellite's reach: no pass at all, nothing outside the time.
    r = clocked(point(1, (BELOW[0], 80.0)), 0, 1439)
    out, _ = fit([r])
    assert out[0].reason == NO_PASS


def test_the_forms_line_says_the_passes_are_outside_the_time():
    at = jst_minutes(T)
    outside = clocked(point(1, BELOW), at + 120, at + 180)
    passes = find_passes([outside], T - 6 * H, T + 6 * H, [SAT])
    text, red = form_text(outside, passes, [SAT], (T - 6 * H, T + 6 * H, True))
    assert red and "指定の時刻にパスなし" in text


# ---- the ledger ---------------------------------------------------------------------------------------------

def test_the_ledger_keeps_the_times():
    f = DEFAULT_REQUEST_FIELDS
    assert (f["timeFrom"], f["timeTo"]) == ("TIME_FROM", "TIME_TO")
    r = clocked(request("R-1"), 600, 840)
    a = attributes_of(r, f)
    assert (a["TIME_OF_DAY"], a["TIME_FROM"], a["TIME_TO"]) == ("時刻", "10:00", "14:00")
    back = request_of({**a, "OBJECTID": 1}, {"rings": r.rings}, f, "OBJECTID")
    assert back.window == (600, 840) and back.time_of_day == BY_CLOCK
    # 昼 keeps no times.
    a = attributes_of(request("R-2", time_of_day="昼", time_from="10:00", time_to="14:00"), f)
    assert a["TIME_FROM"] is None and a["TIME_TO"] is None
    # A ledger without the attributes: as before.
    old = {k: v for k, v in attributes_of(request("R-3", time_of_day="夜"), f).items() if k not in ("TIME_FROM", "TIME_TO")}
    assert request_of({**old, "OBJECTID": 3}, None, f, "OBJECTID").time_of_day == "夜"


def test_saved_and_read_back_from_the_ledger():
    fake = ledger_layers(FakeEsri())
    client, store = EsriClient(fake), request_store_of({"label": "依頼", "url": LEDGER}, [], 0)
    save_request(client, store, None, clocked(request("R-1"), 22 * 60, 2 * 60), FIRST)
    [got] = load_requests(client, store, None)
    assert got.window == (1320, 120) and (got.time_from, got.time_to) == ("22:00", "02:00")


def test_a_ledger_without_the_times_does_not_save_a_request_by_the_clock():
    fake = ledger_layers(FakeEsri())
    for name in ("TIME_FROM", "TIME_TO"):
        del fake.layers[LEDGER].types[name]
    client, store = EsriClient(fake), request_store_of({"label": "依頼", "url": LEDGER}, [], 0)
    with pytest.raises(EsriError) as e:
        save_request(client, store, None, clocked(request("R-1"), 600, 840), FIRST)
    assert e.value.code == "E704" and "TIME_FROM・TIME_TO" in str(e.value)
    # Others still go.
    save_request(client, store, None, request("R-2", time_of_day="昼"), FIRST)
    assert [r.id for r in load_requests(client, store, None)] == ["R-2"]


# ---- layers, files, the last inputs -------------------------------------------------------------------------

def test_layers_carry_the_times():
    r = clocked(request("R-1"), 600, 840)
    row = row_of(r)
    assert (row["TIME_OF_DAY"], row["TIME_FROM"], row["TIME_TO"]) == ("時刻", "10:00", "14:00")
    roles = field_roles(list(row))
    back = request_of_row(row, roles, r.rings, "範囲")
    assert back.window == (600, 840)
    # Headings, and only the times given: by the clock.
    values = {"名称": "港", "時刻（から）": "9:00", "時刻（まで）": "11:30"}
    got = request_of_row(values, field_roles(list(values)), r.rings, "範囲")
    assert got.time_of_day == BY_CLOCK and got.window == (540, 690)
    plain = request_of_row({"名称": "港"}, field_roles(["名称"]), r.rings, "範囲")
    assert plain.time_of_day == "任意" and plain.window is None


def test_the_files_take_the_times():
    r = clocked(point(1, BELOW), 600, 840)
    [c] = gather([r], jst_day(T), [SAT], {}, set())
    ctx = Context(system(), jst_day(T), T)
    assert raw_value(c, "timeFrom", ctx) == "10:00" and raw_value(c, "timeTo", ctx) == "14:00"
    assert raw_value(c, "timeOfDay", ctx) == "時刻"
    [plain] = gather([point(2, BELOW)], jst_day(T), [SAT], {}, set())
    assert raw_value(plain, "timeFrom", ctx) is None


def test_the_next_new_request_starts_from_the_last_times():
    last = remembered(clocked(request("R-1"), 600, 840))
    assert last["time_from"] == "10:00" and last["time_of_day"] == BY_CLOCK
    new = with_remembered(ImagingRequest(id="N"), last)
    assert new.window == (600, 840)
    assert with_remembered(ImagingRequest(id="N"), {"time_from": 5}).time_from == ""


# ---- texts --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, window", [
    ("10時から14時に撮りたい", (600, 840)),
    ("10:00〜14:00", (600, 840)),
    ("午前10時〜午後2時の間", (600, 840)),
    ("午後2時から5時", (840, 1020)),
    ("22時〜翌2時", (1320, 120)),
    ("10〜14時の間で", (600, 840)),
    ("９時半から１１時", (570, 660)),
])
def test_a_text_names_a_window(text, window):
    got = clock_of(text)
    assert got is not None and tuple(got[0]) == window


@pytest.mark.parametrize("text", ["3〜5時間おき", "雲量20〜30%", "11/26 10時〜11/28 18時", "2026-11-26 10:00", "間隔1時間", "10時から"])
def test_no_window_in_other_numbers(text):
    assert clock_of(text) is None


def test_a_draft_puts_the_window_into_the_request():
    today = date(2026, 11, 26)
    d = request_draft("", "来週月曜から2週間、横須賀港を毎日10時から14時の間に光学で", today)
    assert isinstance(d.values["time_of_day"], Clock) and value_text("time_of_day", d.values["time_of_day"]) == "10:00〜14:00"
    r = applied(ImagingRequest(id="R"), d, ["time_of_day"], note=False)
    assert r.time_of_day == BY_CLOCK and r.window == (600, 840)
    # 昼 / 夜 as before.
    d = request_draft("", "横須賀港を夜間にSARで", today)
    assert d.values["time_of_day"] == "夜"
