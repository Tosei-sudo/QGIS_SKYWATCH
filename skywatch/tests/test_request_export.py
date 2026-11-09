"""
The systems' files (request/export.py) beyond the basics of test_request.py:
管理番号・識別番号, the area's centre as GeoJSON, a series' satellites as a
list, defaults for empty values (`[]`, `[""]`, `["On"]`), fixed lists, number
formats, and the 「撮像計画ファイル出力」 table's filters (request/view.py).
"""
from __future__ import annotations

import csv
import io
import json

from skywatch.core.request.daily import HELD, TAKEN, allocate, gather
from skywatch.core.request.export import Context, csv_text, geometry_value, json_text, printf, render
from skywatch.core.request.rows import field_roles, request_of_row, row_of
from skywatch.core.request.store import DEFAULT_REQUEST_FIELDS, attributes_of, request_of
from skywatch.core.request.systems import export_systems_of
from skywatch.core.request.view import BOOST, NO_SYSTEM, CandidateFilter, series_choices, series_of, shown, systems_of
from skywatch.core.request.workflow import plan_day
from skywatch.tests.test_request import DAY, FIRST, H, SATS, TOKYO, request, sat, systems


def _system(**kw):
    problems = []
    out = export_systems_of([{"system": "SYS-T", "kind": "光学", "series": ["OPT-X"], "timeZone": "UTC", **kw}], problems)
    assert problems == []
    return out[0]


def _taken(reqs, system, sats=SATS):
    cands = allocate(gather(reqs, DAY, sats, {}, set()), [system], sats)
    return [c for c in cands if c.result == TAKEN]


def test_management_and_identification_numbers_in_the_ledger_and_layers():
    r = request("R-1", management_no="K-2026-0012", identification_no="000123")
    a = attributes_of(r, DEFAULT_REQUEST_FIELDS)
    assert a["MANAGEMENT_NO"] == "K-2026-0012" and a["IDENTIFICATION_NO"] == "000123"
    back = request_of(a, {"rings": TOKYO}, DEFAULT_REQUEST_FIELDS, "OBJECTID")
    assert (back.management_no, back.identification_no) == ("K-2026-0012", "000123")
    # Empty stays empty (null in the layer); a whole number from a numeric field loses its 「.0」.
    assert attributes_of(request("R-2"), DEFAULT_REQUEST_FIELDS)["MANAGEMENT_NO"] is None
    back = request_of({"REQ_ID": "R-3", "MANAGEMENT_NO": 42.0, "IDENTIFICATION_NO": 7}, None, DEFAULT_REQUEST_FIELDS, "OBJECTID")
    assert (back.management_no, back.identification_no) == ("42", "7")
    # Layers: by the ledger's names, or the headings 管理番号・識別番号.
    row = row_of(r)
    assert row["MANAGEMENT_NO"] == "K-2026-0012" and list(row).index("MANAGEMENT_NO") == 2
    roles = field_roles(["名称", "管理番号", "識別番号", "開始", "終了"])
    assert roles["managementNo"] == "管理番号" and roles["identificationNo"] == "識別番号"
    got = request_of_row({"名称": "x", "管理番号": "M-1", "識別番号": 99.0, "開始": "2026-11-08", "終了": "2026-11-09"}, roles, TOKYO, "範囲")
    assert (got.management_no, got.identification_no) == ("M-1", "99")


def test_numbers_and_centre_geojson_in_csv():
    system = _system(columns=[
        {"key": "管理番号", "source": "managementNo"}, {"key": "識別番号", "source": "identificationNo", "format": "%08d"},
        {"key": "中心", "source": "geometry", "geometry": "centerGeojson", "digits": 3},
    ])
    taken = _taken([request("R-1", management_no="K-1", identification_no="123"), request("R-2", priority=600)], system)
    text, warnings = csv_text(taken, Context(system, DAY, FIRST))
    rows = list(csv.reader(io.StringIO(text)))
    assert warnings == []
    assert rows[1][:2] == ["K-1", "00000123"]
    assert json.loads(rows[1][2]) == {"type": "Point", "coordinates": [139.85, 35.5]}
    # No numbers: empty cells (and a number format leaves an empty value alone).
    assert rows[2][:2] == ["", ""]


def test_centre_geojson_is_the_middle_of_the_box():
    assert geometry_value(TOKYO, "centerGeojson", 2, as_object=True) == {"type": "Point", "coordinates": [139.85, 35.5]}
    assert json.loads(geometry_value(TOKYO, "centerGeojson", 1)) == {"type": "Point", "coordinates": [139.9, 35.5]}
    assert geometry_value([], "centerGeojson", 2) is None


def test_a_series_request_lists_the_series_satellites():
    sats = [*SATS, sat("OPT-X 3", "OPT-X", resolution=2.0), sat("OPT-X 2", "OPT-X", resolution=0.8, mode="Wide")]
    system = _system(format="json", schema={
        "type": "object", "properties": {"tasks": {"type": "array", "x-source": "requests", "items": {"type": "object", "properties": {
            "id": {"type": "string", "x-source": "id"},
            "satellites": {"type": "array", "items": {"type": "string"}, "x-source": "seriesSatellites"},
            "satelliteIds": {"type": "array", "items": {"type": "integer"}, "x-source": "seriesSatelliteIds"},
            "seriesText": {"type": "string", "x-source": "seriesSatellites"},
            "series": {"type": "array", "items": {"type": "string"}, "x-source": "series"},
        }}}}})
    reqs = [request("R-1", series=["OPT-X"], resolution=1.0), request("R-2", priority=600)]
    taken = _taken(reqs, system, sats)
    value = json.loads(json_text(taken, system.schema, Context(system, DAY, FIRST))[0])
    one, two = value["tasks"]
    # The series' satellites that can take it (OPT-X 3 is too coarse), once each (OPT-X 2 has two modes), catalog order.
    assert one["satellites"] == ["OPT-X 1", "OPT-X 2"] and one["seriesText"] == "OPT-X 1,OPT-X 2"
    assert one["series"] == ["OPT-X"]
    # Ids "OPT-X 1" are not numbers: kept as text (the schema's validation tells).
    assert one["satelliteIds"] == ["OPT-X 1", "OPT-X 2"]
    # Not given by series: an empty list (or text).
    assert two["satellites"] == [] and two["seriesText"] == "" and two["series"] == []
    # CSV: joined by commas.
    csv_sys = _system(columns=[{"key": "SATS", "source": "seriesSatellites"}, {"key": "SERIES", "source": "series"}])
    rows = list(csv.reader(io.StringIO(csv_text(_taken(reqs, csv_sys, sats), Context(csv_sys, DAY, FIRST))[0])))
    assert rows[1] == ["OPT-X 1,OPT-X 2", "OPT-X"] and rows[2] == ["", ""]


def test_defaults_for_empty_values_and_fixed_lists():
    schema = {
        "type": "object", "required": ["tasks"], "properties": {"tasks": {"type": "array", "x-source": "requests", "items": {
            "type": "object", "required": ["id", "cloud", "sats", "note", "flags", "mode"], "properties": {
                "id": {"type": "string", "x-source": "id"},
                # Required, but empty for most requests: its default.
                "cloud": {"type": "integer", "x-source": "cloudMax", "default": 100},
                "sats": {"type": "array", "items": {"type": "string"}, "minItems": 1, "x-source": "seriesSatellites", "default": [""]},
                "note": {"type": "string", "x-source": "note", "default": "-"},
                "list": {"type": "array", "x-source": "modes", "default": []},
                # Fixed (const) and default-only values, lists as written.
                "flags": {"type": "array", "items": {"type": "string"}, "const": ["On"]},
                "mode": {"type": "array", "items": {"type": "string"}, "default": ["AUTO"]},
                # x-default: false leaves the default to the system.
                "level": {"type": ["string", "null"], "x-source": "timeOfDay", "x-map": {"任意": None}, "default": "L1", "x-default": False},
            }}}}}
    system = _system(format="json", schema=schema)
    taken = _taken([request("R-1", series=["OPT-X"], note="至急", cloud_no_go=60), request("R-2", priority=600)], system)
    text, errors = json_text(taken, schema, Context(system, DAY, FIRST))
    assert errors == []
    one, two = json.loads(text)["tasks"]
    assert one == {"id": "R-1", "cloud": 60, "sats": ["OPT-X 1", "OPT-X 2"], "note": "至急", "list": [], "flags": ["On"],
                   "mode": ["AUTO"], "level": None}
    assert two == {"id": "R-2", "cloud": 100, "sats": [""], "note": "-", "list": [], "flags": ["On"], "mode": ["AUTO"], "level": None}
    # Without the defaults, the required ones are missing (or empty) and the schema says so.
    bare = json.loads(json.dumps(schema))
    for k in ("cloud", "sats", "note"):
        del bare["properties"]["tasks"]["items"]["properties"][k]["default"]
    _, errors = json_text(taken, bare, Context(system, DAY, FIRST))
    assert any("tasks[1].cloud" in e for e in errors) and any("tasks[1].sats" in e for e in errors)


def test_csv_column_defaults_and_fixed_values():
    system = _system(columns=[
        {"key": "SATS", "source": "seriesSatellites", "default": "ANY"}, {"key": "NO", "source": "managementNo", "default": "000"},
        {"key": "CLOUD", "source": "cloudMax", "default": "100", "format": "%d"},
        {"key": "FIXED", "value": "001"}, {"key": "LIST", "value": ["On", "Off"]},
    ])
    taken = _taken([request("R-1", priority=600)], system)
    rows = list(csv.reader(io.StringIO(csv_text(taken, Context(system, DAY, FIRST))[0])))
    assert rows[1] == ["ANY", "000", "100", "001", "On,Off"]


def test_number_formats():
    # Whole conversions round half up first; text that is a number is a number.
    assert printf("%d", 12.5) == "13" and printf("%d", -12.5) == "-12" and printf("%03d", "7") == "007"
    assert printf("%+d", 5) == "+5" and printf("%5d", 42) == "   42" and printf("%-5d|", 42) == "42   |"
    assert printf("%.1f", 2.25) == "2.2" and printf("%06.2f", -3.14159) == "-03.14" and printf("%.2e", 1234.5) == "1.23e+03"
    assert printf("%04X", 255) == "00FF" and printf("%#x", 255) == "0xff" and printf("P%02d", 3) == "P03"
    assert printf("%d%%", 42) == "42%" and printf("ID-%s", "abc") == "ID-abc"
    # What does not fit: the value as it is.
    assert printf("%05d", "abc") == "abc" and printf("%d-%d", 1) == 1 and printf("%d", None) is None
    # In JSON, a padded number stays text only where the schema says string.
    system = _system(format="json", priority={"linear": {"min": 0, "max": 99, "higherIsLarger": True}}, schema={
        "type": "object", "properties": {"tasks": {"type": "array", "x-source": "requests", "items": {"type": "object", "properties": {
            "text": {"type": "string", "x-source": "priority", "x-format": "%03d"},
            "number": {"type": "integer", "x-source": "priority", "x-format": "%03d"},
            "cloud": {"type": "string", "x-source": "cloudMax", "x-format": "%.1f"},
        }}}}})
    taken = _taken([request("R-1", priority=100, cloud_no_go=60)], system)
    task = json.loads(json_text(taken, system.schema, Context(system, DAY, FIRST))[0])["tasks"][0]
    assert task == {"text": "089", "number": 89, "cloud": "60.0"}


# --- The table's filters ---------------------------------------------------------------------

def test_filters_by_system_series_result_and_words():
    sys_ = systems()
    reqs = [
        request("R-1", priority=100, category="定点", achieve="毎回", management_no="K-7"),
        request("R-2", priority=300, category="定点", achieve="毎回"),
        request("R-3", priority=200, identification_no="ID-42"),
        request("R-4", priority=400),
        request("R-5", kind="SAR", priority=500, rings=[[[139.0, 35.0], [139.2, 35.0], [139.2, 35.1], [139.0, 35.1], [139.0, 35.0]]]),
        request("R-6", kind="SAR", priority=600),
        request("R-8", kind="光学", resolution=0.1),
    ]
    plan = plan_day(reqs, DAY, sys_, SATS, {})
    cands = plan.candidates
    by = {c.request.id: c for c in cands}

    def ids(**kw):
        return [c.request.id for c in shown(cands, CandidateFilter(**kw))]

    assert ids() == [c.request.id for c in cands]
    # SYS-A (2 a day) takes R-1 and R-3; R-2 and R-4 go on to SYS-C.
    assert by["R-1"].system.system == "SYS-A" and systems_of(by["R-1"]) == ["SYS-A"]
    assert ids(system="SYS-A") == ["R-1", "R-3"] and ids(system="SYS-C") == ["R-2", "R-4"]
    # Taken elsewhere: not SYS-A's. Held back: in every system that could take it.
    assert "R-2" not in ids(system="SYS-A") and by["R-2"].system.system == "SYS-C"
    assert by["R-6"].result == HELD and ids(system="SYS-B") == ["R-5", "R-6"]
    assert ids(system=NO_SYSTEM) == ["R-8"]
    # Series: the satellite given, or those that can take it.
    assert series_of(by["R-1"]) == ["OPT-X"] and series_of(by["R-6"]) == ["SAR-Y"]
    assert ids(series="sar-y") == ["R-5", "R-6"]
    assert "R-1" in ids(series="OPT-X") and "R-2" not in ids(series="OPT-X")
    assert series_choices(cands, sys_) == ["CONST-Z", "OPT-X", "SAR-Y"]
    # Result, words (name, id, 管理番号, 識別番号, satellite).
    assert ids(result=HELD) == [c.request.id for c in cands if c.result == HELD]
    assert ids(result=BOOST) == []
    assert ids(text="k-7") == ["R-1"] and ids(text="id-42") == ["R-3"] and ids(text="sar-y 1") == ["R-5"]
    assert ids(system="SYS-A", text="R-2") == [] and ids(system="SYS-B", result=HELD) == ["R-6"]


def test_the_docs_samples_hold():
    """docs/request-samples: the settings read, and the C system's example fits its schema."""
    import os

    from skywatch.core.config import parse_config_text
    from skywatch.core.request.schema import validate

    root = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "request-samples")
    with open(os.path.join(root, "config.sample.jsonc"), encoding="utf-8") as f:
        config, problems = parse_config_text(f.read())
    assert problems == [] and [s.system for s in config.request_exports] == ["SYS-A", "SYS-B", "SYS-C"]
    with open(os.path.join(root, "schemas", "sys-c.schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    with open(os.path.join(root, "sys-c.json.example"), encoding="utf-8") as f:
        assert validate(json.load(f), schema) == []
    # The C system's schema, used: a series request lists its satellites, others get the default [""].
    c = next(s for s in config.request_exports if s.system == "SYS-C")
    c.schema = schema
    sats = [sat("CONST-Z1", "CONST-Z", resolution=3.0), sat("CONST-Z2", "CONST-Z", resolution=3.0)]
    reqs = [request("R-0001", series=["CONST-Z"], management_no="K-2026-0012"), request("R-0005", priority=100)]
    files = render(allocate(gather(reqs, DAY, sats, {}, set()), [c], sats), [c], DAY, FIRST + 8 * H)
    assert files[0].errors == []
    tasks = json.loads(files[0].text)["tasks"]
    assert [t["satellites"] for t in tasks] == [[""], ["CONST-Z1", "CONST-Z2"]]
    assert tasks[1]["controlNo"] == "K-2026-0012" and tasks[0]["controlNo"] == "" and tasks[0]["options"] == ["On"]
    assert tasks[1]["center"] == {"type": "Point", "coordinates": [139.85, 35.5]}
