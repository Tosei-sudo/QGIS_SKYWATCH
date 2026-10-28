"""Place search (core.geocode), with the viewer's own cases (test/geocode.test.ts)."""
import json

import pytest

from satellite_observation.core.config import parse_config
from satellite_observation.core.geocode import (
    GeocoderConfig, PlaceSearch, decode_csv, fold_name, like_text, nominatim_url, parse_arcgis, parse_features, parse_gsi,
    parse_nominatim, read_gazetteer, search_gazetteer,
)


def test_reads_gsi_address_search():
    data = [{"geometry": {"coordinates": [138.727363, 35.360626], "type": "Point"}, "type": "Feature", "properties": {"addressCode": "", "title": "富士山"}}]
    [p] = parse_gsi(data, "地理院")
    assert (p.name, p.lon_lat, p.source, p.detail, p.extent) == ("富士山", (138.727363, 35.360626), "地理院", None, None)
    assert parse_gsi({"error": 1}, "地理院") == []


def test_reads_nominatim_with_its_bounding_box_as_west_south_east_north():
    data = [{"lat": "35.6812", "lon": "139.7671", "display_name": "東京駅, 丸の内, 千代田区", "type": "station", "boundingbox": ["35.67", "35.69", "139.76", "139.77"]}]
    [p] = parse_nominatim(data, "OSM")
    assert (p.name, p.detail, p.lon_lat, p.extent) == ("東京駅", "station、丸の内、千代田区", (139.7671, 35.6812), (139.76, 35.67, 139.77, 35.69))


def test_reads_arcgis_candidates():
    data = {"candidates": [{"address": "横浜駅", "location": {"x": 139.62, "y": 35.46}, "attributes": {"Type": "Train Station"},
                            "extent": {"xmin": 139.61, "ymin": 35.45, "xmax": 139.63, "ymax": 35.47}}]}
    [p] = parse_arcgis(data, "Esri")
    assert (p.name, p.detail, p.lon_lat, p.extent) == ("横浜駅", "Train Station", (139.62, 35.46), (139.61, 35.45, 139.63, 35.47))


def test_reads_an_esri_layers_points_centroids_and_polygons():
    data = {"features": [
        {"attributes": {"NAME": "A港"}, "geometry": {"x": 135.1, "y": 34.6}},
        {"attributes": {"NAME": "B地区"}, "centroid": {"x": 135.5, "y": 34.7}, "geometry": {"rings": [[[135, 34], [136, 34], [136, 35], [135, 34]]]}},
        {"attributes": {"NAME": "C"}, "geometry": {"paths": [[[130, 30], [132, 32]]]}},
        {"attributes": {"NAME": None}, "geometry": {"x": 1, "y": 1}},
    ]}
    assert [(p.name, p.lon_lat, p.extent) for p in parse_features(data, "NAME", "社内")] == [
        ("A港", (135.1, 34.6), None), ("B地区", (135.5, 34.7), (135, 34, 136, 35)), ("C", (131, 31), (130, 30, 132, 32))]


CSV = "名称,種別,緯度,経度\n横須賀港,港,35.28,139.67\n横浜港,港,35.45,139.65\nヨコハマ駅,駅,35.46,139.62\n壊れた行,,abc,1\n"


def test_csv_gazetteer_finds_the_columns_and_keeps_the_rest_as_detail():
    g = read_gazetteer(CSV, "社内")
    assert len(g.rows) == 3
    p = g.rows[0][1]
    assert (p.name, p.detail, p.lon_lat, p.source) == ("横須賀港", "港", (139.67, 35.28), "社内")


def test_csv_gazetteer_matches_names_however_they_are_typed_names_starting_with_it_first():
    g = read_gazetteer(CSV, "社内")
    assert [p.name for p in search_gazetteer(g, "港")] == ["横浜港", "横須賀港"]
    assert [p.name for p in search_gazetteer(g, "よこはま")] == ["ヨコハマ駅"]
    assert [p.name for p in search_gazetteer(g, "横")] == ["横浜港", "横須賀港"]
    assert fold_name("ＡＢＣ ｶﾀｶﾅ") == "abcかたかな"


def test_csv_gazetteer_says_which_columns_are_missing_and_reads_shift_jis():
    with pytest.raises(ValueError, match="緯度"):
        read_gazetteer("a,b\n1,2\n", "社内")
    assert decode_csv("名称,lat,lon\n".encode("cp932")) == "名称,lat,lon\n"


def test_like_and_urls():
    assert like_text("50%_o'k") == "50\\%\\_o''k"
    assert nominatim_url("https://osm.test", "東京 駅") == "https://osm.test/search?q=%E6%9D%B1%E4%BA%AC%20%E9%A7%85&format=jsonv2&limit=8&accept-language=ja"


def test_asks_every_geocoder_keeps_their_order_and_reports_the_ones_that_fail():
    asked = []

    def get(url):
        asked.append(url)
        if url.startswith("https://gsi.test"):
            return json.dumps([{"geometry": {"coordinates": [139, 35]}, "properties": {"title": "地理院の場所"}}]).encode()
        if url == "https://csv.test/places.csv":
            return "name,lat,lon\n社内の場所,36,140\n".encode()
        raise OSError("HTTP 500")

    def esri(url, params, token):
        asked.append((url, params["where"], token))
        return {"features": [{"attributes": {"NAME": "施設の場所"}, "geometry": {"x": 135, "y": 34}}]}

    search = PlaceSearch([
        GeocoderConfig("csv", "社内", "https://csv.test/places.csv"),
        GeocoderConfig("gsi", "地理院", "https://gsi.test/AddressSearch"),
        GeocoderConfig("nominatim", "OSM", "https://osm.test"),
        GeocoderConfig("features", "施設", "https://esri.test/Places/FeatureServer/0/", token="t", field="NAME"),
    ], get, esri)
    places, errors = search.search("場所")
    assert [(p.name, p.source) for p in places] == [("社内の場所", "社内"), ("地理院の場所", "地理院"), ("施設の場所", "施設")]
    assert errors == ["OSM: HTTP 500"]
    assert ("https://esri.test/Places/FeatureServer/0/query", "NAME LIKE '%場所%' ESCAPE '\\'", "t") in asked
    # The CSV is read once.
    search.search("社内")
    assert asked.count("https://csv.test/places.csv") == 1
    assert not PlaceSearch([], get, esri).enabled


def test_the_settings_file_lists_geocoders_and_says_what_is_wrong():
    config, problems = parse_config({"geocoders": [
        {"type": "csv", "label": "地名表", "url": "./places.csv", "field": "名称"},
        {"type": "features", "url": "https://x.test/FeatureServer/0"},
        {"type": "google", "url": "https://x.test"},
        {"type": "gsi"},
    ]})
    assert [(g.type, g.label, g.url, g.field) for g in config.geocoders] == [("csv", "地名表", "./places.csv", "名称")]
    assert problems == [
        "geocoders[1] の features には名前の属性（field）が必要",
        "geocoders[2] の type は gsi・nominatim・arcgis・features・csv のいずれかで指定",
        "geocoders[3] に url（http(s) か相対パス）なし",
    ]
    assert parse_config({})[0].geocoders == []  # none: nothing typed is sent anywhere
