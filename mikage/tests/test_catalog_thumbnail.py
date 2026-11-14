"""
新着画像のカード (tsudo, 2026-11-14): the thumbnail address pattern of a
catalog (`imageCatalogs[].thumbnail`) and 「写っている地物」 (`footprintLayers`),
without QGIS.
"""
import json

import pytest

from mikage.core.catalog.config import catalog_of
from mikage.core.catalog.thumbnail import (
    DEFAULT_HEIGHT, DEFAULT_WIDTH, Contents, FootprintLayer, ThumbnailPattern, contents_line, contents_of, feature_text,
    fill, footprint_layers_of, footprint_of, pattern_problems, thumbnail_address, thumbnail_of,
)
from mikage.core.esri import EsriClient
from mikage.tests.fake_esri import FakeEsri, FakeLayer

FIELDS = {"id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR"}
# 2025-11-14 15:30 UTC (2025-11-15 00:30 in Tokyo).
ACQ = 1763134200000
ATTRS = {"IMG_ID": "A 1&2", "ACQ": ACQ, "SENSOR": "S1", "CLOUD": 7, "PATH": "x y"}
# An askew footprint: its corners are not the box's.
ASKEW = {"rings": [[[139.6, 35.5], [139.62, 35.7], [139.8, 35.68], [139.78, 35.48], [139.6, 35.5]]]}
SQUARE = {"rings": [[[139.6, 35.48], [139.6, 35.7], [139.8, 35.7], [139.8, 35.48], [139.6, 35.48]]]}
PLACES = "https://example.com/arcgis/rest/services/Places/FeatureServer/0"


def test_roles_times_and_attributes_are_put_in():
    url = fill("https://ql/{id}.jpg?d={acquired:%Y%m%d}&t={acquired:%Y%m%d%H@Asia/Tokyo}&s={sensor}&c={attr.CLOUD:03d}&p={attr.PATH}",
               ATTRS, FIELDS, SQUARE)
    assert url == "https://ql/A%201%262.jpg?d=20251114&t=2025111500&s=S1&c=007&p=x%20y"
    # ISO by default (UTC), unix seconds and milliseconds.
    assert fill("{acquired}", ATTRS, FIELDS, None) == "2025-11-14T15:30:00Z"
    assert fill("{acquired:unix}|{acquired:unixms}", ATTRS, FIELDS, None) == "1763134200|1763134200000"
    # A role the catalog does not have, or a value missing: empty.
    assert fill("{angle}-{attr.NOPE}", ATTRS, FIELDS, None) == "-"


def test_a_file_path_is_used_as_it_comes_out():
    assert fill("\\\\nas\\ql\\{acquired:%Y}\\{id}.png", ATTRS, FIELDS, None) == "\\\\nas\\ql\\2025\\A 1&2.png"
    assert fill("{{literal}}/{id}", ATTRS, FIELDS, None) == "{literal}/A 1&2"


def test_box_centre_and_corners():
    url = fill("https://q/?bbox={bbox}&c={center.lat},{lon}&ur={ur.lat:.4f},{ur.lon:.4f}&ll={llx},{lly}", ATTRS, FIELDS, SQUARE)
    assert url == "https://q/?bbox=139.6,35.48,139.8,35.7&c=35.59,139.7&ur=35.7000,139.8000&ll=139.6,35.48"
    place = footprint_of(ASKEW)
    # The footprint's own vertex nearest each corner of its box.
    assert place.corners == {"ul": (139.62, 35.7), "ur": (139.8, 35.68), "lr": (139.78, 35.48), "ll": (139.6, 35.5)}
    assert place.box == (139.6, 35.48, 139.8, 35.7)
    assert fill("{ul.lon},{ul.lat}|{lr.lon},{lr.lat}", {}, {}, ASKEW) == "139.62,35.7|139.78,35.48"


def test_web_mercator_metres():
    out = fill("{bbox@3857}|{xmin:.0f@3857}|{center.lat:.1f@EPSG:3857}", {}, {}, {"rings": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]]})
    box, xmin, lat = out.split("|")
    assert box == "0,0,1113194.908,1118889.975"
    assert xmin == "0"
    assert lat == "557305.3"


def test_coordinates_without_a_footprint_give_no_address():
    assert fill("https://q/{bbox}", ATTRS, FIELDS, None) is None
    assert fill("https://q/{id}", ATTRS, FIELDS, None) == "https://q/A%201%262"


def test_size_and_token():
    pattern = ThumbnailPattern("https://q/{id}?w={width}&h={height}&token={token}", 320, 200)
    feature = {"attributes": {"IMG_ID": "B"}, "geometry": SQUARE}
    assert thumbnail_address(pattern, feature, FIELDS, "abc") == "https://q/B?w=320&h=200&token=abc"
    assert thumbnail_address(pattern, feature, FIELDS) == "https://q/B?w=320&h=200&token="


@pytest.mark.parametrize("pattern, problem", [
    ("https://q/{id", "{ と } の対応が合わない"),
    ("https://q/{name}", "{name} は使えない"),
    ("https://q/{acquired:%Y@Mars/Base}", "タイムゾーン「Mars/Base」が不明"),
    ("https://q/{bbox@4612}", "座標系「4612」"),
    ("https://q/{ur.lat:zz}", "数の書式「zz」"),
    ("https://q/{id@3857}", "@ は使えない"),
    ("https://q/{ul.alt}", "{ul.alt} は使えない"),
])
def test_pattern_problems(pattern, problem):
    found = pattern_problems(pattern)
    assert found and problem in found[0]


def test_good_patterns_have_no_problems():
    for pattern in ("https://q/{id}/{acquired:%Y/%m/%d@+09:00}.jpg?b={bbox@3857}&ur={ur.lat:.6f},{urx}&{{x}}",
                    "\\\\nas\\{sensor}\\{attr.NAME}.png", "{attr.DAY:%Y%m%d}{token}{width}x{height}"):
        assert pattern_problems(pattern) == []


def test_thumbnail_setting():
    problems = []
    assert thumbnail_of("https://q/{id}", problems, "t") == ThumbnailPattern("https://q/{id}", DEFAULT_WIDTH, DEFAULT_HEIGHT)
    assert thumbnail_of({"url": "https://q/{id}", "width": 320, "height": 240}, problems, "t") == ThumbnailPattern("https://q/{id}", 320, 240)
    assert problems == []
    assert thumbnail_of({"url": "https://q/{id}", "width": 5000}, problems, "t").width == DEFAULT_WIDTH
    assert problems == ["t.width は 16〜2048 の整数（ピクセル）で指定"]
    problems.clear()
    assert thumbnail_of(12, problems, "t") is None and problems
    problems.clear()
    assert thumbnail_of("https://q/{what}", problems, "t") is None
    assert problems[0].startswith("t: {what} は使えない")
    assert thumbnail_of(None, problems, "t") is None


def test_catalog_reads_thumbnail_and_footprint_layers():
    problems = []
    catalog = catalog_of({"label": "衛星画像", "url": "https://example.com/arcgis/rest/services/Images/FeatureServer/0",
                          "fields": {"id": "IMG_ID", "source": "PATH"}, "thumbnail": {"url": "https://q/{id}.jpg", "width": 640},
                          "footprintLayers": [{"label": "施設", "url": PLACES, "fields": ["NAME", "NAME_EN"],
                                               "format": "{NAME}（{NAME_EN}）"}]}, problems, 0)
    assert problems == []
    assert catalog.thumbnail == ThumbnailPattern("https://q/{id}.jpg", 640, DEFAULT_HEIGHT)
    assert catalog.footprint_layers == [FootprintLayer("施設", PLACES, ("NAME", "NAME_EN"), "{NAME}（{NAME_EN}）")]


@pytest.mark.parametrize("entry, problem", [
    ({"url": "https://example.com/x", "fields": ["NAME"]}, "url は …/FeatureServer/0"),
    ({"url": PLACES}, "fields は表示する属性名"),
    ({"url": PLACES, "fields": ["NAME"], "format": "{NAME_EN}"}, "{NAME_EN} は fields にない属性"),
    ({"url": PLACES, "fields": ["NAME"], "where": "1=1; DROP TABLE X"}, "where は 1 つの条件式"),
    ({"url": PLACES, "fields": ["NAME"], "max": 0}, "max はカードに出す地物の数の上限"),
])
def test_footprint_layer_problems(entry, problem):
    problems = []
    footprint_layers_of([entry], problems, "f")
    assert problems and problem in problems[0]


def test_footprint_layer_defaults():
    problems = []
    layers = footprint_layers_of([{"url": PLACES + "/", "fields": "NAME", "where": "KIND = 'port'", "max": 50, "token": "t"}], problems, "f")
    assert problems == []
    layer = layers[0]
    assert (layer.label, layer.fields, layer.where, layer.max, layer.token) == ("レイヤー 1", ("NAME",), "KIND = 'port'", 50, "t")
    assert footprint_layers_of({"url": PLACES}, problems, "f") == [] and problems == ['f は配列で指定（[{ "label", "url", "fields" }]）']


def test_feature_text():
    both = FootprintLayer("施設", PLACES, ("NAME", "NAME_EN"), "{NAME}（{NAME_EN}）")
    assert feature_text(both, {"NAME": "横須賀本港", "NAME_EN": "Yokosuka Port"}) == "横須賀本港（Yokosuka Port）"
    # An empty value: its brackets go too.
    assert feature_text(both, {"NAME": "長浦港", "NAME_EN": None}) == "長浦港"
    assert feature_text(both, {"NAME": "長浦港", "NAME_EN": "  "}) == "長浦港"
    plain = FootprintLayer("施設", PLACES, ("NAME", "NAME_EN"))
    assert feature_text(plain, {"NAME": "東京港", "NAME_EN": "Port of Tokyo"}) == "東京港 Port of Tokyo"
    assert feature_text(plain, {"NAME": None, "NAME_EN": None}) == ""


def _places(count=0, max_record_count=1000):
    fake = FakeEsri()
    layer = fake.add_layer(PLACES, FakeLayer({"NAME": "esriFieldTypeString", "NAME_EN": "esriFieldTypeString", "KIND": "esriFieldTypeString"},
                                             "esriGeometryPoint", max_record_count=max_record_count))
    layer.add({"NAME": "横須賀本港", "NAME_EN": "Yokosuka Port", "KIND": "port"}, {"x": 139.66, "y": 35.6})
    layer.add({"NAME": "長浦港", "NAME_EN": None, "KIND": "port"}, {"x": 139.64, "y": 35.61})
    layer.add({"NAME": "長浦港", "NAME_EN": None, "KIND": "port"}, {"x": 139.65, "y": 35.62})
    layer.add({"NAME": None, "NAME_EN": None, "KIND": "pier"}, {"x": 139.7, "y": 35.6})
    layer.add({"NAME": "成田", "NAME_EN": "Narita", "KIND": "airport"}, {"x": 140.38, "y": 35.77})  # outside
    for i in range(count):
        layer.add({"NAME": f"岸壁{i:04d}", "NAME_EN": None, "KIND": "berth"}, {"x": 139.7, "y": 35.65})
    return fake, EsriClient(fake)


def test_contents_names_every_feature_over_the_footprint():
    _, client = _places()
    layer = FootprintLayer("施設", PLACES, ("NAME", "NAME_EN"), "{NAME}（{NAME_EN}）")
    contents = contents_of(client, layer, SQUARE)
    assert contents.texts == ["横須賀本港（Yokosuka Port）", "長浦港 ×2", "（名前なし）"]
    assert (contents.count, contents.read, contents.cut) == (4, None, False)
    assert contents_line(contents) == "横須賀本港（Yokosuka Port）、長浦港 ×2、（名前なし）"
    assert contents_line(Contents([], 0)) == "なし"
    # A where: only those.
    ports = FootprintLayer("港", PLACES, ("NAME",), where="KIND = 'port'")
    assert contents_of(client, ports, SQUARE).texts == ["横須賀本港", "長浦港 ×2"]
    # No footprint: nothing asked.
    assert contents_of(client, layer, None) == Contents([], 0)


def test_contents_read_every_page():
    # 「略さずすべて」: the service gives 100 at a time, all 2504 are read.
    _, client = _places(count=2500, max_record_count=100)
    contents = contents_of(client, FootprintLayer("施設", PLACES, ("NAME",)), SQUARE)
    assert contents.count == 2504 and not contents.cut
    assert len(contents.texts) == 2503  # 長浦港 ×2 once
    assert "岸壁2499" in contents.texts


def test_contents_cut_at_max_counts_them_all():
    _, client = _places(count=30)
    contents = contents_of(client, FootprintLayer("施設", PLACES, ("NAME",), max=10), SQUARE)
    assert (contents.read, contents.count, contents.cut) == (10, 34, True)
    assert Contents.from_json(json.loads(json.dumps(contents.to_json()))) == contents
    assert Contents.from_json({"texts": "x"}) is None
