"""The dashboard's figures (the viewer's dashboard.ts and the time axis of time.ts)."""
import math
from datetime import date, datetime

from satellite_observation.core.dashboard import (
    MAX_VALUES, OTHERS, add_time, bin_chosen, categories, category_value, choose_range, choose_value, filter_expression, floor_time,
    format_time, histogram, in_values, number_value, pick_group, pick_measure, short, step_at_least, time_bins,
)


def ms(*args):
    return datetime(*args).timestamp() * 1000


def test_short_numbers_and_values():
    assert (short(1234), short(12.34), short(0.1254), short(3.0), short(None), short(math.nan)) == ("1,234", "12.3", "0.125", "3", "—", "—")
    assert (category_value(None), category_value(2.0), category_value(2.5), category_value(True), category_value(date(2024, 5, 1))) == (
        "", "2", "2.5", "true", "2024-05-01")
    assert (number_value("12"), number_value(""), number_value(True), number_value("x"), number_value(3)) == (12.0, None, None, None, 3.0)


def test_values_counted_summed_and_averaged():
    rows = [("公園", 10), ("公園", 20), ("公園", 30), ("学校", 40), ("学校", 50), ("病院", 60)]
    counted, listed = categories(rows, "count")
    assert [(c.value, c.figure) for c in counted] == [("公園", 3), ("学校", 2), ("病院", 1)] and listed == {"公園", "学校", "病院"}
    assert [(c.value, c.figure) for c in categories(rows, "sum")[0]] == [("学校", 90), ("公園", 60), ("病院", 60)]
    assert [(c.value, c.figure) for c in categories(rows, "mean")[0]] == [("病院", 60), ("学校", 45), ("公園", 20)]
    # A value chosen stays listed with nothing.
    assert [(c.value, c.count) for c in categories([("a", None)], "count", {"z"})[0]] == [("a", 1), ("z", 0)]


def test_the_smallest_values_go_into_others():
    rows = [(f"v{i:02d}", None) for i in range(20) for _ in range(20 - i)]
    entries, listed = categories(rows, "count")
    assert len(entries) == MAX_VALUES and entries[-1].value == OTHERS and entries[-1].count == sum(range(1, 7))
    assert "v13" in listed and "v14" not in listed
    assert in_values("v19", {OTHERS}, listed) and not in_values("v00", {OTHERS}, listed)


def test_a_number_in_bins():
    h = histogram([10, 20, 30, 40, 50, 60])
    assert (h.low, h.high, len(h.counts), sum(h.counts), h.counts[-1], h.mean) == (10, 60, 20, 6, 1, 35)
    assert h.bin_range(19) == (57.5, 60) and bin_chosen((57.5, 60), 57.5, 60)
    assert histogram([5, 5]).counts == [2] and histogram([]) is None
    assert histogram([1, 2], (0, 10)).low == 0


def test_clicks_choose_values_and_ranges():
    assert choose_value(set(), "a", False) == {"a"}
    assert choose_value({"a"}, "a", False) == set()
    assert choose_value({"a"}, "b", False) == {"b"}
    assert choose_value({"a"}, "b", True) == {"a", "b"} and choose_value({"a", "b"}, "a", True) == {"b"}
    assert choose_range(None, 1, 2, False) == (1, 2) and choose_range((1, 2), 1, 2, False) is None
    assert choose_range((1, 2), 5, 6, True) == (1, 6)


def test_the_attributes_chosen_at_first():
    fields = [("fid", "integer"), ("name", "string"), ("kind", "string"), ("value", "double"), ("code", "integer")]
    values = {"name": ["a", "b", "c", "d"], "kind": ["x", "x", "y", "y"], "code": [1, 1, 2, 2]}
    assert pick_group(fields, lambda n: values[n]) == "kind"
    assert pick_group(fields, lambda n: values[n], coded=["code"]) == "code"
    assert pick_measure(fields) == "value" and pick_measure([("objectid", "integer")]) is None


def test_the_time_axis_in_calendar_steps():
    assert step_at_least(86_400_000) == ("day", 1) and step_at_least(40 * 86_400_000) == ("month", 3)
    assert floor_time(ms(2024, 5, 15, 13, 20), "month") == ms(2024, 5, 1)
    assert floor_time(ms(2024, 5, 15, 13, 20), "week") == ms(2024, 5, 13)
    assert floor_time(ms(2024, 5, 15, 13, 20), "hour", 6) == ms(2024, 5, 15, 12)
    assert add_time(ms(2024, 1, 31), "month", 1) == ms(2024, 2, 29)
    assert format_time(ms(2024, 5, 15, 13, 20), "month") == "2024-05" and format_time(ms(2024, 5, 15, 13, 20), "minute") == "2024-05-15 13:20"
    bins = time_bins([ms(2024, m, 15) for m in range(1, 7)])
    assert bins.step == ("week", 1) and sum(bins.counts) == 6 and bins.counts[0] == 1 and bins.unit == "day"
    assert bins.end_of(0) == bins.starts[1] - 1
    assert time_bins([]) is None


def test_the_filter_as_a_where_clause():
    assert filter_expression("kind", {"学校"}, set(), None, None) == "\"kind\" IN ('学校')"
    assert filter_expression("kind", {"学校", "病院"}, set(), "value", (55.0, 60.0)) == (
        "\"kind\" IN ('学校', '病院') AND \"value\" >= 55.0 AND \"value\" <= 60.0")
    assert filter_expression("code", {"1", ""}, set(), None, None, numeric_group=True) == '("code" IN (1) OR "code" IS NULL)'
    assert filter_expression("kind", {OTHERS}, {"a", "b"}, None, None) == "(\"kind\" NOT IN ('a', 'b') OR \"kind\" IS NULL)"
    assert filter_expression("kind", {OTHERS}, {"a", ""}, None, None) == "(\"kind\" NOT IN ('a') AND \"kind\" IS NOT NULL AND \"kind\" <> '')"
    assert filter_expression("kind", {"it's"}, set(), None, None, quote=lambda n: n) == "kind IN ('it''s')"
    assert filter_expression("kind", set(), set(), "value", None) is None
