"""観測記録のエクスポート (core/obs/export.py): the settings, which records, CSV / JSON by columns or a JSON Schema, place names."""
import json

from skywatch.core.config import parse_config
from skywatch.core.obs.alerts import Geofence, NamedArea, ring_size
from skywatch.core.obs.csv import observations_csv
from skywatch.core.obs.export import (
    BUILTIN_CSV, BUILTIN_JSON, ExportContext, ExportFilter, ExportSettings, export_settings_of, file_name_of, items_of, render,
    select_records, settings_for,
)
from skywatch.core.obs.model import ConfidenceLevel
from skywatch.tests.test_obs import day, obs


def square(w, s, e, n):
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


BAY = NamedArea("東京湾", square(139.5, 35.0, 140.1, 35.7), "海域", ring_size(square(139.5, 35.0, 140.1, 35.7)), (139.5, 35.0, 140.1, 35.7))
PORT = NamedArea("横須賀港", square(139.65, 35.25, 139.75, 35.35), "港", ring_size(square(139.65, 35.25, 139.75, 35.35)),
                 (139.65, 35.25, 139.75, 35.35))
LEVELS = [ConfidenceLevel("確実"), ConfidenceLevel("推定"), ConfidenceLevel("不明")]


def settings(value):
    problems = []
    s = export_settings_of(value, problems)
    return s, problems


class TestSettings:
    def test_the_built_in_formats_are_always_there_and_csv_is_the_default(self):
        s = settings_for(None)
        assert [f.name for f in s.formats] == [BUILTIN_CSV, BUILTIN_JSON]
        assert s.format().name == BUILTIN_CSV
        assert s.named_areas is None

    def test_reads_named_formats_and_takes_places_from_alert_reports(self):
        config, problems = parse_config({
            "alertReports": {"namedAreas": "https://x/arcgis/rest/services/A/FeatureServer/3", "formats": [{"name": "速報"}]},
            "observationExports": {"default": "集計", "formats": [
                {"name": "集計", "encoding": "cp932", "delimiter": "\t", "columns": ["id", {"key": "PLACE", "source": "place", "empty": "なし"}]},
                {"name": "連携", "format": "json", "schema": "schemas/obs.schema.json"},
            ]},
        })
        assert problems == []
        s = settings_for(config.observation_exports, config.alert_reports)
        assert [f.name for f in s.formats] == [BUILTIN_CSV, BUILTIN_JSON, "集計", "連携"]
        assert s.default == "集計" and s.named_areas.url.endswith("/FeatureServer/3")
        f = s.format()
        assert f.encoding == "cp932" and f.delimiter == "\t" and [c.key for c in f.columns] == ["id", "PLACE"]
        assert f.needs(["place"]) and not f.needs(["geofences"])
        assert s.format("連携").needs(["place"])  # a schema in a file: might write anything
        # With named areas the built-in JSON gets the place keys.
        assert [c.key for c in s.format(BUILTIN_JSON).columns][-3:] == ["PLACE", "PLACE_KIND", "PLACES"]

    def test_problems_are_said(self):
        s, problems = settings({"default": "なし", "formats": [
            {"name": BUILTIN_CSV, "columns": ["id"]},
            {"name": "a", "columns": [{"key": "X", "source": "nope"}]},
            {"name": "b", "format": "xml", "columns": ["id"], "schema": {}},
            {"columns": ["id"]},
        ]})
        assert [f.name for f in s.formats] == ["b"]
        text = "\n".join(problems)
        for part in ("組み込みのフォーマット名", "source「nope」は使用不可", "csv か json", "schema は format が json のときのみ", "name（フォーマットの名前）なし",
                     "default は formats の name"):
            assert part in text, part


class TestSelect:
    def test_by_period_place_class_confidence_presence_and_deleted(self):
        records = [
            obs(id="a", object_id="A", image_time=day(2), kind="船舶 > 支援船", confidence="確実", lon=139.7, lat=35.3),
            obs(id="b", object_id="B", image_time=day(5), kind="航空機", confidence="推定", lon=139.9, lat=35.5),
            obs(id="c", object_id="C", image_time=day(8), kind="船舶", confidence="不明", present=False, lon=141.0, lat=35.5),
            obs(id="d", object_id="D", image_time=day(5), kind="船舶", deleted_at=day(21)),
        ]
        ids = lambda f: [o.id for o in select_records(records, f, LEVELS)]  # noqa: E731
        assert ids(ExportFilter()) == ["a", "b", "c"]
        assert ids(ExportFilter(deleted=True)) == ["a", "b", "c", "d"]
        assert ids(ExportFilter(start=day(3), end=day(6))) == ["b"]
        # 登録日時 as the basis: all made on day 20.
        assert ids(ExportFilter(start=day(19), basis="created")) == ["a", "b", "c"]
        assert ids(ExportFilter(polygons=[BAY.rings])) == ["a", "b"]
        assert ids(ExportFilter(polygons=[PORT.rings, square(140.9, 35.4, 141.1, 35.6)])) == ["a", "c"]
        assert ids(ExportFilter(roots=["船舶"])) == ["a", "c"]
        assert ids(ExportFilter(confidence="推定")) == ["a", "b"]
        assert ids(ExportFilter(presence="absent")) == ["c"]

    def test_a_hole_is_outside(self):
        donut = square(139.0, 35.0, 140.0, 36.0) + square(139.4, 35.4, 139.6, 35.6)
        records = [obs(id="in", lon=139.2, lat=35.2), obs(id="hole", lon=139.5, lat=35.5)]
        assert [o.id for o in select_records(records, ExportFilter(polygons=[donut]))] == ["in"]


class TestWrite:
    def records(self):
        return [
            obs(id="a", object_id="A", object_name="船A", image_time=day(2), kind="船舶 > 支援船", lon=139.7, lat=35.3, length=152.25, note="=1+1"),
            obs(id="c", object_id="C", object_name="船C", image_time=day(8), kind="船舶", present=False, lon=141.0, lat=35.5),
        ]

    def test_the_built_in_csv_is_as_before_and_adds_place_names(self):
        rs = self.records()
        s = settings_for(None)
        plain = render(items_of(rs), ExportContext(s.format(), ExportFilter(), day(20)))
        assert plain.text == observations_csv(rs) and plain.data.startswith(b"\xef\xbb\xbf\xef\xbb\xbf") is False
        assert plain.file_name.startswith("observations_") and plain.file_name.endswith(".csv")
        fences = [Geofence("湾内", BAY.rings)]
        out = render(items_of(rs, [BAY, PORT], fences), ExportContext(s.format(), ExportFilter(), day(20), places=True, fences=True))
        head, first, second = out.text.lstrip("﻿").strip().split("\r\n")
        assert head.endswith("削除日時,地名,地名の種別,含まれる地名,ジオフェンス")
        assert first.endswith(",横須賀港,港,横須賀港・東京湾,湾内") and second.endswith(",,,,")

    def test_csv_by_columns_with_levels_maps_formats_affixes_and_file_values(self):
        s, problems = settings({"formats": [{"name": "集計", "delimiter": ";", "dateFormat": "%Y/%m/%d %H:%M", "timeZone": "Asia/Tokyo", "columns": [
            "id", {"key": "TOP", "source": "kind", "level": 1}, {"key": "LAST", "source": "kind", "level": -1},
            {"key": "AT", "source": "imageTime"}, {"key": "P", "source": "present", "map": {"true": "1", "false": "0"}},
            {"key": "PLACE", "source": "place", "empty": "（なし）", "prefix": "〔", "suffix": "〕"}, {"key": "LAT", "source": "lat", "digits": 3},
            {"key": "LEN", "source": "length", "format": "%.0f"}, {"key": "MGRS", "source": "mgrs"}, {"key": "NOTE", "source": "note"},
            {"key": "AREA", "source": "areaName"}, {"key": "SYS", "value": "SKY"}, {"key": "N", "source": "index"},
        ]}]})
        assert problems == []
        f = s.formats[0]
        out = render(items_of(self.records(), [BAY, PORT]), ExportContext(f, ExportFilter(area_name="描いた範囲"), day(20), places=True))
        lines = out.text.split("\r\n")
        assert lines[0] == "id;TOP;LAST;AT;P;PLACE;LAT;LEN;MGRS;NOTE;AREA;SYS;N"
        cells = lines[1].split(";")
        assert cells[:8] == ["a", "船舶", "支援船", "2026/10/02 10:00", "1", "〔横須賀港〕", "35.300", "152"]
        assert cells[8].startswith("54S") and cells[9] == "'=1+1" and cells[10:] == ["描いた範囲", "SKY", "1"]
        assert lines[2].split(";")[4:6] == ["0", "（なし）"]
        # A CSV is utf-8-sig unless the format says (for spreadsheets).
        assert out.text.encode("utf-8-sig") == out.data

    def test_json_of_columns_keeps_types(self):
        s = settings_for(None)
        out = render(items_of(self.records()), ExportContext(s.format(BUILTIN_JSON), ExportFilter(), day(20)))
        value = json.loads(out.text)
        assert out.file_name.endswith(".json") and len(value) == 2
        a = value[0]
        assert a["OBS_ID"] == "a" and a["PRESENT"] is True and a["LENGTH_M"] in (152.2, 152.3) and a["NOTE"] == "=1+1"
        assert a["IMAGE_TIME"] == "2026-10-02T10:00:00+09:00" and a["DELETED_AT"] is None and a["LAT"] == 35.3

    def test_json_by_a_schema_is_built_and_checked(self):
        schema = {
            "type": "object", "required": ["exportedAt", "records"],
            "properties": {
                "exportedAt": {"type": "string", "format": "date-time", "x-source": "exportedAt"},
                "by": {"type": "string", "x-source": "exportedBy"},
                "count": {"type": "integer", "x-source": "count"},
                "records": {"type": "array", "x-source": "records", "items": {"type": "object", "required": ["id", "place"], "properties": {
                    "id": {"type": "string", "x-source": "id"},
                    "place": {"type": "string", "x-source": "place"},
                    "where": {"type": "object", "x-source": "geometry", "x-geometry": "geojson", "x-digits": 3},
                    "seen": {"type": "boolean", "x-source": "present"},
                    "len": {"type": ["number", "null"], "x-source": "length"},
                }}},
            },
        }
        s, problems = settings({"formats": [{"name": "連携", "format": "json", "timeZone": "UTC", "schema": schema}]})
        assert problems == []
        out = render(items_of(self.records(), [BAY, PORT]), ExportContext(s.formats[0], ExportFilter(), day(20), "田中", places=True))
        value = json.loads(out.text)
        assert value["exportedAt"] == "2026-10-20T01:00:00Z" and value["by"] == "田中" and value["count"] == 2
        a, c = value["records"]
        assert a == {"id": "a", "place": "横須賀港", "where": {"type": "Point", "coordinates": [139.7, 35.3]}, "seen": True, "len": 152.2} \
            or a["len"] == 152.3
        # 船C is in no named area: an empty text; no length: null.
        assert c["place"] == "" and c["len"] is None and out.errors == []
        schema["properties"]["records"]["items"]["properties"]["place"]["minLength"] = 1
        out = render(items_of(self.records(), [BAY, PORT]), ExportContext(s.formats[0], ExportFilter(), day(20), places=True))
        assert out.errors == ["$.records[1].place: 1 文字未満"]

    def test_a_schema_file_that_cannot_be_read(self):
        s, _ = settings({"formats": [{"name": "連携", "format": "json", "schema": "nowhere.json"}]})

        def load(path):
            raise OSError("ない")

        out = render(items_of(self.records()), ExportContext(s.formats[0], ExportFilter(), day(20)), load)
        assert not out.data and "nowhere.json" in out.errors[0]

    def test_cp932_characters_it_cannot_hold(self):
        s, _ = settings({"formats": [{"name": "SJIS", "encoding": "shift_jis", "columns": ["objectName"]}]})
        out = render(items_of([obs(object_name="船🚢")]), ExportContext(s.formats[0], ExportFilter(), day(20)))
        assert out.data.decode("cp932").splitlines()[1] == "船?" and out.errors

    def test_the_file_name(self):
        s, _ = settings({"formats": [{"name": "a/b", "columns": ["id"], "fileName": "{name}_{now:%Y%m%d_%H%M}.{ext}"}]})
        assert file_name_of(s.formats[0], day(20)) == "a_b_20261020_1000.csv"
        assert ExportSettings().format() is None
