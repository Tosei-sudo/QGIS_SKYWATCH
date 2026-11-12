"""Satellite names that differ between catalogs (`satelliteAliases`)."""
from dataclasses import replace

import pytest

from mikage.core.catalog.images import ANY, patterns_for
from mikage.core.config import parse_config
from mikage.core.plan.names import (
    NO_NAMES, SatelliteNames, aliases_of, alias_tables_of, best_named, load_satellite_names, name_rank, plain,
)
from mikage.core.plan.satellites import sample_satellites
from mikage.core.request.route import why_not

TABLE = "https://example.com/arcgis/rest/services/sat/FeatureServer/2"


class FakeClient:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.asked = rows, fail, []

    def query_all(self, url, where="1=1", token=None, out_fields="*", geometry=True, cancelled=None, **_):
        self.asked.append((url, where, out_fields, geometry))
        if self.fail:
            raise RuntimeError("接続不可")
        return [{"attributes": r} for r in self.rows]


def names_of(rows):
    return load_satellite_names(FakeClient(rows), alias_tables_of({"url": TABLE, "fields": {"satellite": "SAT", "alias": "ALIAS"}}, []),
                                lambda url, token: token)[0]


def test_plain_and_alias_lists():
    assert plain("WorldView-3") == "worldview3"
    assert aliases_of("WV03, WV3、wv-3") == ("WV03", "WV3", "wv-3")
    assert aliases_of(None) == ()
    assert aliases_of(["A", " ", "A"]) == ("A",)


def test_config_reads_the_tables():
    config, problems = parse_config({"satelliteAliases": {"label": "対応表", "url": TABLE, "where": "USE = 1",
                                                          "fields": {"satellite": "SAT", "alias": "ALIAS"}}})
    assert problems == []
    t = config.satellite_aliases[0]
    assert (t.label, t.url, t.fields, t.where) == ("対応表", TABLE, {"satellite": "SAT", "alias": "ALIAS"}, "USE = 1")
    config, problems = parse_config({"satelliteAliases": [{"url": TABLE, "fields": {"satellite": "SAT"}}, {"url": "x"}]})
    assert config.satellite_aliases == []
    assert problems == ["satelliteAliases[0] に fields.alias（別名の属性）なし",
                        "satelliteAliases[1] の url は …/FeatureServer/2 のようなテーブルの URL で指定"]
    assert parse_config({})[0].satellite_aliases == []


def test_rows_of_one_satellite_are_grouped():
    client = FakeClient([{"SAT": "WorldView-3", "ALIAS": "WV03"}, {"SAT": "worldview 3", "ALIAS": "WV3"},
                         {"SAT": "SkySat-C12", "ALIAS": "SkySat"}, {"SAT": "SkySat-C13", "ALIAS": "SkySat"},
                         {"SAT": "", "ALIAS": "X"}, {"SAT": "GeoEye-1", "ALIAS": None}])
    tables = alias_tables_of({"url": TABLE, "fields": {"satellite": "SAT", "alias": "ALIAS"}, "where": "USE = 1"}, [])
    names, problems = load_satellite_names(client, tables, lambda url, token: token)
    assert problems == []
    assert client.asked == [(TABLE, "USE = 1", "SAT,ALIAS", False)]
    assert names.expand("WorldView-3") == {"worldview3", "wv03", "wv3"}
    assert names.same("WV-3", "WorldView-3") and names.same("WV03", "wv3")
    # One alias for several satellites of a series: each is the alias's, but they are not one another's.
    assert names.expand("SkySat-C12") == {"skysatc12", "skysat"}
    assert names.same("SkySat", "SkySat-C13")
    assert not names.same("SkySat-C12", "SkySat-C13")
    assert not names.same("GeoEye-1", "WV03")
    assert NO_NAMES.expand("WorldView-3") == {"worldview3"} and not NO_NAMES


def test_a_table_that_fails_is_reported():
    tables = alias_tables_of({"url": TABLE, "fields": {"satellite": "SAT", "alias": "ALIAS"}}, [])
    names, problems = load_satellite_names(FakeClient([], fail=True), tables, lambda url, token: token)
    assert not names
    assert problems == ["衛星名の対応表 1 の読み込みに失敗（接続不可）"]


def test_the_image_naming_the_satellite_best_is_taken():
    names = names_of([{"SAT": "WorldView-3", "ALIAS": "WV03"}])
    found = [{"S": "GF2"}, {"S": "WV-1"}, {"S": "WV03"}]
    wanted = names.expand("WorldView-3")
    assert best_named(found, lambda f: f["S"], wanted) == {"S": "WV03"}
    # Without the table, no name matches: the first image, as before.
    assert best_named(found, lambda f: f["S"], NO_NAMES.expand("WorldView-3")) == {"S": "GF2"}
    # The same name wins over one inside another (WV-1 is inside WV-10, but WV-10 is the satellite).
    assert best_named([{"S": "WV1"}, {"S": "WV-10"}], lambda f: f["S"], {"wv10"}) == {"S": "WV-10"}
    assert name_rank("SkySat", {"skysatc12"}) == 1 and name_rank("", {"a"}) == 0


def test_image_patterns_of_a_sensors_other_name():
    names = names_of([{"SAT": "WorldView-3", "ALIAS": "WV03"}])
    patterns = {"WorldView-3": ["-P1BS-"], ANY: ["\\.tif$"]}
    assert patterns_for(patterns, "WV03", names) == ["-P1BS-"]
    assert patterns_for(patterns, "WV03") == ["\\.tif$"]
    assert patterns_for(patterns, "GF2", names) == ["\\.tif$"]


def test_requests_name_satellites_by_their_aliases():
    sat = sample_satellites()[0]
    names = names_of([{"SAT": sat.name, "ALIAS": "ALIAS-1"}])
    with_aliases = replace(sat, aliases=tuple(sorted(names.expand(sat.name, sat.id))))
    from mikage.core.request.model import ImagingRequest

    req = ImagingRequest(id="R", name="R", satellites=["alias 1"])
    assert why_not(req, sat) == "衛星"
    assert why_not(req, with_aliases) != "衛星"


def test_the_plugin_reads_the_tables_once_until_the_settings_change():
    pytest.importorskip("qgis.core")
    from mikage.qgis_io.context import PluginContext

    context = PluginContext()
    client = FakeClient([{"SAT": "WorldView-3", "ALIAS": "WV03"}])
    assert context.satellite_names(client) is NO_NAMES and client.asked == []
    context.config, _ = parse_config({"satelliteAliases": {"url": TABLE, "fields": {"satellite": "SAT", "alias": "ALIAS"}}})
    assert not context.known_names()
    assert context.satellite_names(client).same("WV03", "WorldView-3")
    assert context.satellite_names(client).same("WV03", "WorldView-3")
    assert len(client.asked) == 1
    assert context.known_names().same("WV03", "WorldView-3")
    assert "衛星名の対応表" in [s.role for s in context.services()]
    context.configChanged.emit()
    assert not context.known_names()
    # A table that cannot be read: no other names, asked again next time.
    assert not context.satellite_names(FakeClient([], fail=True))
    assert context.satellite_names(client) and len(client.asked) == 2
