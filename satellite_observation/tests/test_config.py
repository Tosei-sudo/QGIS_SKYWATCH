"""The settings file: ported from the viewer's tests of each entry's reader, and JSONC."""
import json
import os

import pytest

from satellite_observation.core.auth import arcgis_portal_of
from satellite_observation.core.catalog import catalog_of
from satellite_observation.core.config import load_jsonc, parse_config, parse_config_text, read_config_file, JsoncError
from satellite_observation.core.obs.areas import area_store_of
from satellite_observation.core.obs.model import confidences_of, observation_store_of, vocabulary_of, ConfidenceLevel
from satellite_observation.core.obs.taxonomy import kind_of, spec_fields_of, taxonomy_of, SpecField, Taxon
from satellite_observation.core.plan.modes import sensor_catalog_of, sensor_mode_of
from satellite_observation.core.plan.satellites import BUILT_IN_DEFAULTS, ground_stations_of, look_side_of, satellite_catalog_of, GroundStation
from satellite_observation.core.plan.store import DEFAULT_PLAN_FIELDS, plan_store_of
from satellite_observation.core.plan.weather import DEFAULT_CLOUD_FORECAST, cloud_forecast_of, CloudForecastConfig

HERE = os.path.dirname(__file__)


class TestJsonc:
    def test_reads_comments_and_trailing_commas_but_not_inside_strings(self):
        text = '''
        {
          // a line comment
          "url": "https://example.com/a//b", /* a block
          comment */ "list": [1, 2, 3,],
          "text": "/* kept */ // kept",
        }'''
        assert load_jsonc(text) == {"url": "https://example.com/a//b", "list": [1, 2, 3], "text": "/* kept */ // kept"}

    def test_escaped_quotes_stay_in_strings(self):
        assert load_jsonc('{"a": "x\\"// y", "b": 1}') == {"a": 'x"// y', "b": 1}

    def test_says_where_it_cannot_be_read(self):
        with pytest.raises(JsoncError, match="3 行"):
            load_jsonc('{\n "a": 1\n "b": 2}')
        config, problems = parse_config_text("{")
        assert problems and problems[0].startswith("設定ファイルを JSON として読み込み不可")
        assert config.satellite_catalogs == []

    def test_reads_the_viewer_example_and_ignores_keys_it_does_not_use(self):
        config, problems = read_config_file(os.path.join(HERE, "data", "viewer-config.jsonc"))
        assert problems == []
        assert [c.label for c in config.satellite_catalogs] == ["衛星カタログ"]
        assert config.satellite_catalogs[0].fields["tle1"] == "TLE_LINE1"
        assert [m.mode for m in config.sensor_catalogs[1].modes] == ["ストリップマップ", "スポットライト", "スキャンSAR"]
        assert config.ground_stations == [GroundStation("勝浦", 140.3, 35.15, 5)]
        assert config.plan_stores[0].catalog == "衛星画像"
        assert config.observation_stores[0].fields["activity"] == "ACT"
        assert config.observation_stores[0].areas.url.endswith("/FeatureServer/2")
        assert config.observation_vocabulary.match_radius == 3000
        assert [c.label for c in config.observation_vocabulary.confidences][:2] == ["確定", "ほぼ確実"]
        assert config.arcgis_portals[1].url == "https://gis.example.com/portal"
        assert config.image_catalogs[0].fields["source"] == ["COG_URL", "FILE_PATH"]

    def test_not_an_object(self):
        assert parse_config([])[1] == ["設定はオブジェクトで指定"]


class TestSatelliteCatalogs:
    def test_reads_the_attribute_names_and_defaults(self):
        problems = []
        catalog = satellite_catalog_of(
            {
                "label": "衛星",
                "url": "https://example.com/arcgis/rest/services/Sats/FeatureServer/0?x=1",
                "fields": {"name": "SAT_NAME", "tle1": "L1", "tle2": "L2", "maxOffNadir": "MAX_ANGLE", "swath": "SWATH_M", "lookSide": "LOOK", "kind": "SENSOR", "minInterval": "GAP_MIN", "maxPerDay": "PER_DAY", "series": "SERIES", "leadTime": "LEAD_H"},
                "swathUnit": "m",
                "defaults": {"maxOffNadir": 25},
            },
            problems,
            0,
        )
        assert problems == []
        assert catalog.url == "https://example.com/arcgis/rest/services/Sats/FeatureServer/0"
        assert catalog.defaults == {"maxOffNadir": 25, "minOffNadir": 0, "swath": 10, "scenesPerPass": 1, "minInterval": 0, "maxPerDay": 0, "leadTime": 0}
        assert catalog.swath_unit == "m"

    def test_refuses_catalogs_it_cannot_use(self):
        problems = []
        assert satellite_catalog_of({"url": "https://e.com/a/FeatureServer", "fields": {"tle": "T"}}, problems, 0) is None
        assert satellite_catalog_of({"url": "https://e.com/a/FeatureServer/0", "fields": {"tle1": "A"}}, problems, 1) is None
        assert len(problems) == 2

    def test_is_read_from_the_settings(self):
        config, problems = parse_config({"satelliteCatalogs": [{"url": "https://e.com/a/FeatureServer/0", "fields": {"tle": "TLE", "name": "NAME"}}, "bad"]})
        assert len(config.satellite_catalogs) == 1
        assert problems == ["satelliteCatalogs[1] はオブジェクトで指定"]

    def test_bad_names_defaults_and_patterns(self):
        problems = []
        catalog = satellite_catalog_of({"url": "https://e.com/a/MapServer/3", "fields": {"tle": "T", "name": "bad name"}, "defaults": {"swath": -1}, "sarPattern": "("}, problems, 2)
        assert catalog.fields == {"tle": "T"}
        assert catalog.defaults == BUILT_IN_DEFAULTS
        assert catalog.label == "衛星カタログ 3"
        assert catalog.sar_pattern.search("radar")
        assert problems == [
            "satelliteCatalogs[2] の fields.name は属性名（英数字と _）で指定",
            "satelliteCatalogs[2] の defaults.swath は 0 以上の数で指定",
            "satelliteCatalogs[2] の sarPattern は正規表現として読み込み不可",
        ]

    def test_reads_the_look_side_in_english_letters_and_japanese(self):
        assert [look_side_of(v) for v in ["right", "R", "右", "Left", "l", "左側", "both", "", None]] == ["right", "right", "right", "left", "left", "left", "both", "both", "both"]


class TestSensorCatalogs:
    def test_reads_modes_written_in_the_settings(self):
        problems = []
        catalog = sensor_catalog_of(
            {
                "label": "センサー",
                "swathUnit": "m",
                "sensors": [
                    {"sensor": "ICEYE", "kind": "SAR", "lookSide": "right", "minInterval": 5, "modes": [{"mode": "Strip", "swath": 30000, "length": 50000, "maxOffNadir": 30}, {"name": "Spot", "swath": 5000, "lookSide": "both", "default": True, "primary": "はい"}]},
                    {"sensor": "WorldView", "kind": "optical", "maxOffNadir": 30},
                    {"modes": []},
                ],
            },
            problems,
            0,
        )
        assert problems == ["sensorCatalogs[0].sensors[2] に sensor（センサー名）なし"]
        assert [m.values() for m in catalog.modes] == [
            {"sensor": "ICEYE", "mode": "Strip", "sar": True, "swath": 30, "length": 50, "maxOffNadir": 30, "minInterval": 5, "lookSide": "right"},
            {"sensor": "ICEYE", "mode": "Spot", "sar": True, "swath": 5, "minInterval": 5, "lookSide": "both", "primary": True},
            {"sensor": "WorldView", "mode": "", "sar": False, "maxOffNadir": 30},
        ]

    def test_maps_a_table_of_modes_by_role_and_says_what_is_wrong(self):
        problems = []
        table = sensor_catalog_of({"url": "https://example.com/arcgis/rest/services/Sensors/FeatureServer/1", "fields": {"sensor": "SENSOR", "mode": "MODE", "swath": "SWATH_KM", "resolution": "RES_M", "kind": "TYPE", "primary": "IS_DEFAULT"}}, problems, 1)
        assert problems == []
        assert table.url == "https://example.com/arcgis/rest/services/Sensors/FeatureServer/1"
        assert sensor_mode_of({"SENSOR": "ICEYE", "MODE": "Spot", "SWATH_KM": 5, "RES_M": "0.5", "TYPE": "SAR", "IS_DEFAULT": 1}, table).values() == {"sensor": "ICEYE", "mode": "Spot", "sar": True, "swath": 5, "resolution": 0.5, "primary": True}
        assert sensor_mode_of({"MODE": "Spot"}, table) is None
        assert sensor_catalog_of({"url": "https://example.com/x"}, problems, 2) is None
        assert sensor_catalog_of({"url": "https://example.com/arcgis/rest/services/S/FeatureServer/1", "fields": {"mode": "MODE"}}, problems, 3) is None
        assert problems == ["sensorCatalogs[2] は sensors（モードの一覧）か、…/FeatureServer/1 のようなテーブルの url を記述", "sensorCatalogs[3] に fields.sensor（センサー名の属性）なし"]
        config = parse_config({
            "satelliteCatalogs": [{"url": "https://example.com/arcgis/rest/services/Sat/FeatureServer/0", "fields": {"tle": "TLE", "sensor": "SENSOR"}}],
            "sensorCatalogs": [{"sensors": [{"sensor": "ICEYE", "modes": [{"mode": "Spot"}]}]}],
        })[0]
        assert config.satellite_catalogs[0].fields["sensor"] == "SENSOR"
        assert [m.values() for m in config.sensor_catalogs[0].modes] == [{"sensor": "ICEYE", "mode": "Spot"}]


class TestPlanStoresStationsForecast:
    def test_reads_plan_stores_with_default_field_names(self):
        problems = []
        store = plan_store_of({"label": "計画", "url": "https://gis.example/arcgis/rest/services/Plans/FeatureServer/0", "fields": {"status": "STATE"}, "catalog": "画像"}, problems, 0)
        assert store.fields == {**DEFAULT_PLAN_FIELDS, "status": "STATE"}
        assert store.match_minutes == 30
        assert store.miss_after_days == 3
        assert plan_store_of({"url": "https://x/MapServer"}, problems, 1) is None
        assert len(problems) == 1
        assert len(parse_config({"planStores": [{"url": "https://gis.example/arcgis/rest/services/Plans/FeatureServer/0"}]})[0].plan_stores) == 1

    def test_reads_ground_stations_with_a_default_lowest_elevation(self):
        problems = []
        assert ground_stations_of([{"name": "勝浦", "lon": 140.3, "lat": 35.15}, {"lon": 200, "lat": 0}, {"name": "S", "lon": 15.4, "lat": 78.2, "minElevation": 10}], problems) == [
            GroundStation("勝浦", 140.3, 35.15, 5),
            GroundStation("S", 15.4, 78.2, 10),
        ]
        assert len(problems) == 1
        config = parse_config({"groundStations": [{"name": "A", "lon": 1, "lat": 2}], "cloudForecast": False})[0]
        assert len(config.ground_stations) == 1
        assert config.cloud_forecast is None
        assert parse_config({})[0].cloud_forecast == DEFAULT_CLOUD_FORECAST

    def test_checks_the_cloud_forecast(self):
        problems = []
        assert cloud_forecast_of({"url": "https://wx.example/v1/forecast", "label": "社内"}, problems) == CloudForecastConfig("https://wx.example/v1/forecast", "社内")
        assert cloud_forecast_of(False, problems) is None
        assert cloud_forecast_of("x", problems) == DEFAULT_CLOUD_FORECAST
        assert len(problems) == 1


class TestImageCatalogsPortals:
    entry = {
        "label": "衛星画像",
        "url": "https://gis.example.com/arcgis/rest/services/Catalog/FeatureServer/0",
        "fields": {"id": "IMG_ID", "acquired": "ACQ_TIME", "registered": "REG_TIME", "systemRegistered": "SYS_TIME", "sensor": "SENSOR", "angle": "OFF_NADIR", "azimuth": "AZIMUTH", "source": ["COG_URL", "FILE_PATH"]},
        "labels": {"angle": "オフナディア角"},
        "columns": [{"field": "CLOUD", "label": "雲量"}, "ORBIT"],
        "where": "STATUS = 'OK'",
    }

    def test_reads_the_attribute_names_by_role(self):
        problems = []
        c = catalog_of(self.entry, problems, 0)
        assert (c.label, c.url, c.fields, c.labels, c.columns, c.where, c.max_results, c.angle_kind, c.orbit_height) == (
            "衛星画像", self.entry["url"], self.entry["fields"], {"angle": "オフナディア角"},
            [{"field": "CLOUD", "label": "雲量"}, {"field": "ORBIT", "label": "ORBIT"}], "STATUS = 'OK'", 500, "offNadir", 600,
        )
        assert problems == []

    def test_needs_a_layer_url_and_a_source_field(self):
        problems = []
        assert catalog_of({**self.entry, "url": "https://gis.example.com/arcgis/rest/services/Catalog/FeatureServer"}, problems, 0) is None
        assert catalog_of({"url": self.entry["url"], "fields": {"id": "ID"}}, problems, 1) is None
        c = catalog_of({"url": self.entry["url"], "fields": {"source": "PATH", "sensor": "bad name;"}}, problems, 2)
        assert (c.label, c.fields["source"]) == ("カタログ 3", ["PATH"])
        assert len(problems) == 3
        assert catalog_of({**self.entry, "angleKind": "incidence", "orbitHeight": 520}, problems, 0).orbit_height == 520

    def test_reads_portals_https_only_the_root_without_sharing(self):
        problems = []
        p = arcgis_portal_of({"label": "社内", "url": "https://gis.example.com/portal/sharing/rest/", "clientId": "Abc123"}, problems, 0)
        assert (p.label, p.url, p.client_id) == ("社内", "https://gis.example.com/portal", "Abc123")
        assert arcgis_portal_of({"url": "http://localhost:4175/portal", "clientId": "x"}, problems, 1).label == "localhost:4175"
        assert problems == []
        assert arcgis_portal_of({"url": "http://gis.example.com/portal", "clientId": "x"}, problems, 2) is None
        assert arcgis_portal_of({"url": "https://gis.example.com/portal"}, problems, 3) is None
        assert arcgis_portal_of({"url": "https://gis.example.com/portal", "clientId": "a b"}, problems, 4) is None
        assert arcgis_portal_of({"url": "https://user:pw@gis.example.com/", "clientId": "x"}, problems, 5) is None
        assert len(problems) == 4


class TestObservationSettings:
    def test_reads_observation_stores_and_the_vocabulary(self):
        problems = []
        store = observation_store_of({"label": "観測", "url": "https://x/FeatureServer/2", "fields": {"kind": "TYPE", "bogus": "X"}, "softDelete": False, "activities": ["入港", ""]}, problems, 0)
        assert store.fields["kind"] == "TYPE"
        assert store.fields["id"] == "OBS_ID"
        assert store.soft_delete is False
        assert store.activities == ["入港"]
        assert problems == ["observationStores[0] の fields.bogus は使用不可の役割"]
        assert observation_store_of({"url": "https://x/MapServer/2"}, problems, 1) is None
        assert vocabulary_of({"kinds": ["艦艇"]}, problems).kinds == ["艦艇"]

    def test_takes_confidence_levels_as_words_or_objects(self):
        problems = []
        assert confidences_of(["確定", {"label": "可能性大", "color": "#d97706", "note": "特徴が一致"}, {"label": "確定"}, {"color": "red"}, {"label": "x", "color": "url(evil)"}], problems, "c") == [
            ConfidenceLevel("確定"),
            ConfidenceLevel("可能性大", "#d97706", "特徴が一致"),
            ConfidenceLevel("x"),
        ]
        assert problems == ["c[2] の「確定」は重複", "c[3] は文字列か { label } で指定", "c[4] の color は CSS の色で指定"]

    def test_confidences_are_part_of_the_vocabulary_and_of_a_store(self):
        problems = []
        v = vocabulary_of({"confidences": ["A", "B", "C", "D", "E"], "matchRadius": 5000}, problems)
        assert [c.label for c in v.confidences] == ["A", "B", "C", "D", "E"]
        assert v.match_radius == 5000
        assert [c.label for c in vocabulary_of({}, problems).confidences] == ["確定", "推定", "不明"]
        assert vocabulary_of({"matchRadius": -1}, problems).match_radius == 2000
        assert problems == ["observationVocabulary.matchRadius は正の数（m）で指定"]
        store = observation_store_of({"url": "https://x/FeatureServer/0", "confidences": ["確実", "推定"], "areas": "https://x/FeatureServer/2/"}, problems, 0)
        assert store.confidences == [ConfidenceLevel("確実"), ConfidenceLevel("推定")]
        assert (store.areas.url, store.areas.soft_delete, store.areas.fields["id"]) == ("https://x/FeatureServer/2", True, "AREA_ID")

    def test_reads_the_areas_layer(self):
        problems = []
        a = area_store_of({"url": "https://x/FeatureServer/3", "fields": {"kind": "TARGET", "bad": "X"}, "softDelete": False}, problems, "a")
        assert (a.url, a.soft_delete, a.fields["kind"], a.fields["id"]) == ("https://x/FeatureServer/3", False, "TARGET", "AREA_ID")
        assert area_store_of("https://x/MapServer/3", problems, "b") is None
        assert problems == ["a の fields.bad は使用不可の役割", "b は編集できる …/FeatureServer/1 のようなポリゴンレイヤーの URL で指定"]

    def test_reads_the_taxonomy_plain_names_too(self):
        problems = []
        items = taxonomy_of(["車両", {"name": "船舶", "children": ["漁船", {"bogus": 1}]}], problems)
        assert [kind_of(t.path) for t in items] == ["車両", "船舶", "船舶 > 漁船"]
        assert problems == ["observationTaxonomy[1].children[1] は name のある分類（または名前の文字列）で指定"]
        assert taxonomy_of({}, problems) is None
        tree = [
            {"name": "船舶", "levels": ["大分類", "種別", "級"], "nameLabel": "艦名", "children": [
                {"name": "支援船", "children": [{"name": "〇〇級", "members": ["△△", "□□"], "specs": {"全長": "167"}}]},
                {"name": "護衛艦"},
            ]},
            {"name": "航空機", "children": [{"name": "F-15", "children": ["F-15J"]}]},
        ]
        v = vocabulary_of({"taxonomy": tree, "activities": ["入港"]}, [])
        assert v.kinds == ["船舶", "航空機"]
        assert len(v.taxonomy) == 7
        assert vocabulary_of({"kinds": ["艦艇"]}, []).taxonomy == [Taxon(["艦艇"])]

    def test_reads_the_specification_fields(self):
        problems = []
        assert spec_fields_of(["英名", {"name": "全長", "type": "number", "unit": "m"}, {"name": "x", "type": "date"}, "英名"], problems) == [
            SpecField("英名", "text"),
            SpecField("全長", "number", "m"),
        ]
        assert problems == ["specFields[2].type は text・number・list・url のいずれかで指定"]
        assert vocabulary_of({"specFields": ["艦番号"]}, []).spec_fields == [SpecField("艦番号", "text")]
