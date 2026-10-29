"""The settings file's base maps, as the viewer reads them (config.test.ts), and their QGIS sources."""
from urllib.parse import parse_qsl

from satellite_observation.core.basemaps import (
    DEFAULT_BASE_MAPS, attribution_text, base_map_type, pmtiles_archive, raster_source, resolve_url, style_tiles, vector_tile_source,
)
from satellite_observation.core.config import parse_config


def test_the_viewers_list_unless_written():
    config, problems = parse_config({})
    assert [b.id for b in config.base_maps] == ["gsi-std", "gsi-pale", "gsi-photo", "gsi-vector", "osm", "esri-osm"]
    assert config.default_base_map == "" and problems == []
    # Written: it replaces the list (an empty list leaves only 「なし」, as in a closed network).
    config, _ = parse_config({"baseMaps": []})
    assert config.base_maps == []


def test_types_from_the_url():
    assert base_map_type("https://x/arcgis/rest/services/Base/VectorTileServer") == "esri"
    assert base_map_type("https://x/arcgis/rest/services/Base/VectorTileServer/") == "esri"
    assert base_map_type("https://x/tiles/{z}/{x}/{y}.pbf") == "mvt"
    assert base_map_type("https://x/tiles/{z}/{x}/{y}.mvt?key=1") == "mvt"
    assert base_map_type("https://x/tiles/{z}/{x}/{y}.png") == "xyz"
    assert base_map_type("https://x/style.json") == "style"


def test_entries_and_their_problems():
    config, problems = parse_config({
        "baseMaps": [
            {"id": "a", "url": "./tiles/{z}/{x}/{y}.png", "maxZoom": 16},
            {"id": "b", "label": "社内", "url": "https://g/VectorTileServer", "token": "t", "style": "https://g/root.json"},
            {"id": "c", "url": "https://x/{z}/{x}/{y}.pbf"},
            {"id": "a", "url": "https://dup/{z}/{x}/{y}.png"},
            {"id": "d", "url": "https://x", "type": "wms"},
            {"url": "https://x"},
            "x",
        ],
        "defaultBaseMap": "b",
    })
    a, b, c = config.base_maps
    assert (a.label, a.type, a.max_zoom) == ("a", "xyz", 16)
    assert (b.label, b.type, b.token, b.style) == ("社内", "esri", "t", "https://g/root.json")
    assert (c.type, c.max_zoom) == ("mvt", 14)
    assert config.default_base_map == "b"
    assert problems == [
        "baseMaps の id「a」が重複",
        "baseMaps[4] の type「wms」は xyz・mvt・style・esri のいずれかで指定",
        "baseMaps[5] に id か url（http(s) か相対パス）なし",
        "baseMaps[6] はオブジェクトで指定",
    ]
    _, problems = parse_config({"defaultBaseMap": "nope"})
    assert problems == ["defaultBaseMap「nope」は baseMaps になし"]
    _, problems = parse_config({"baseMaps": {}})
    assert problems == ["baseMaps は配列で指定"]


def test_relative_urls_against_the_settings_file():
    assert resolve_url("./t/{z}/{x}/{y}.png", "https://site/viewer/config.json") == "https://site/viewer/t/{z}/{x}/{y}.png"
    assert resolve_url("./t/{z}/{x}/{y}.png", "/data/config.json") == "file:///data/t/{z}/{x}/{y}.png"
    assert resolve_url("//host/a", "") == "https://host/a"


def test_qgis_sources():
    std = DEFAULT_BASE_MAPS[0]
    source = raster_source(std, std.url, "abc1234")
    assert source == "type=xyz&zmin=0&zmax=18&authcfg=abc1234&url=https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"
    esri = DEFAULT_BASE_MAPS[5]
    params = dict(parse_qsl(vector_tile_source(esri, esri.url + "/")))
    assert params == {"type": "xyz", "serviceType": "arcgis", "url": esri.url}
    params = dict(parse_qsl(vector_tile_source(DEFAULT_BASE_MAPS[3], "https://t/{z}/{x}/{y}.pbf", style_url="https://s/std.json", zmin=4, zmax=16)))
    assert params == {"type": "xyz", "url": "https://t/{z}/{x}/{y}.pbf", "zmin": "4", "zmax": "16", "styleUrl": "https://s/std.json"}


def test_a_styles_tiles():
    style = {"sources": {"r": {"type": "raster", "tiles": ["x"]}, "v": {"type": "vector", "tiles": ["../t/{z}/{x}/{y}.pbf"], "minzoom": 4, "maxzoom": 16}}}
    assert style_tiles(style, "https://s/style/std.json") == {"tiles": "https://s/t/{z}/{x}/{y}.pbf", "minzoom": 4, "maxzoom": 16}
    assert style_tiles({"sources": {"v": {"type": "vector", "url": "tiles.json"}}}, "https://s/a/style.json") == {"tilejson": "https://s/a/tiles.json"}
    assert style_tiles({"sources": {}}, "https://s") is None


def test_pmtiles_in_a_style():
    # 地理院 ベクトル (optimal_bvmap style/std.json): its PMTiles archive is also served as {z}/{x}/{y} tiles.
    gsi = {"sources": {"v": {"type": "vector", "minzoom": 4, "maxzoom": 16,
                             "tiles": ["pmtiles://https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/optimal_bvmap-v1.pmtiles/{z}/{x}/{y}"]}}}
    assert style_tiles(gsi, "https://gsi-cyberjapan.github.io/optimal_bvmap/style/std.json") == {
        "tiles": "https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/{z}/{x}/{y}.pbf", "minzoom": 4, "maxzoom": 16}
    # Others: QGIS 3.34 cannot read them.
    assert style_tiles({"sources": {"v": {"type": "vector", "url": "pmtiles://./a.pmtiles"}}}, "https://s/style/x.json") == {
        "pmtiles": "https://s/style/a.pmtiles"}
    assert pmtiles_archive("pmtiles://https://h/b.pmtiles/{z}/{x}/{y}") == "https://h/b.pmtiles"
    assert pmtiles_archive("https://h/{z}/{x}/{y}.pbf") is None


def test_attribution_as_text():
    assert attribution_text(DEFAULT_BASE_MAPS[5].attributions) == "Esri, © OpenStreetMap contributors"
