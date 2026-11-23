"""外部から受け取る図形 (core/link_shapes.py): the viewer's link parameters, read strictly."""
import json

import pytest

from mikage.core.link_shapes import (
    DEFAULT_TITLE, MAX_BODY_VALUE, MAX_SHAPES, LinkError, bbox_of, color_of, has_shapes, params_of_json, params_of_query, read_link_shapes, to_wkt,
    zoom_scale,
)


def read(query: str):
    return read_link_shapes(params_of_query(query))


def test_points_lines_polygons_in_any_notation_with_labels_in_order():
    s = read("geom=35.6812,139.7671&label=東京駅&line=35,139;54SUE8843349290&label=&polygon=35,139;35,140;36,140&label=面"
             "&name=テスト&color=F0A&zoom=12&url=https://example.com")
    assert s.title == "テスト" and s.color == "#ff00aa" and s.zoom == 12 and s.errors == [] and s.labeled
    assert [(x.geometry["type"], x.label) for x in s.shapes] == [("Point", "東京駅"), ("LineString", None), ("Polygon", "面")]
    assert s.shapes[0].geometry["coordinates"] == [139.7671, 35.6812]
    assert s.shapes[1].geometry["coordinates"][1] == pytest.approx([139.7671, 35.6812], abs=1e-4)
    # The ring is closed for the sender.
    assert s.shapes[2].geometry["coordinates"][0][0] == s.shapes[2].geometry["coordinates"][0][-1]
    assert to_wkt(s.shapes[0].geometry) == "POINT (139.7671 35.6812)"
    assert bbox_of(s.shapes) == pytest.approx((139, 35, 140, 36))


def test_several_points_and_wkt_and_ewkt():
    s = read("geom=35,139;36,140")
    assert s.shapes[0].geometry == {"type": "MultiPoint", "coordinates": [[139.0, 35.0], [140.0, 36.0]]}
    assert s.title == DEFAULT_TITLE and not s.labeled
    s = read("geom=" + "GEOMETRYCOLLECTION (POINT (40 10), LINESTRING Z (10 10 1, 20 20 2))".replace(" ", "%20"))
    assert [x.geometry["type"] for x in s.shapes] == ["Point", "LineString"]
    assert s.shapes[1].geometry["coordinates"] == [[10, 10], [20, 20]]
    s = read("geom=SRID%3D3857%3BPOINT(15558720 4256310)")
    assert s.shapes[0].geometry["coordinates"] == pytest.approx([139.766, 35.677], abs=1e-3)
    assert to_wkt(read("geom=MULTIPOLYGON(((30 20, 45 40, 10 40, 30 20)))").shapes[0].geometry) == "MULTIPOLYGON (((30 20, 45 40, 10 40, 30 20)))"


def test_values_that_cannot_be_read_say_why_and_the_rest_is_shown():
    s = read("geom=POINT(200 10)&geom=abc&polygon=1,2;3,4&line=35,139&geom=SRID=2451;POINT(1 2)&geom=POINT(1 2)<script>"
             "&geom=POINT EMPTY&geom=35,139&label=a")
    assert s.errors == [
        "geom=POINT(200 10): 緯度経度の範囲外の座標あり（WKT・GeoJSON は経度・緯度の順）",
        "geom=abc: 座標として読めない: abc",
        "polygon=1,2;3,4: ポリゴンには 3 点以上が必要",
        "line=35,139: ラインには 2 点以上が必要",
        "geom=SRID=2451;POINT(1 2): SRID=2451 には非対応（4326・3857）",
        "geom=POINT(1 2)<script>: WKT に使えない文字あり",
        "geom=POINT EMPTY: 空の図形",
    ]
    # The label goes with the first parameter it was written for (which could not be read): none on the point.
    assert [(x.geometry["coordinates"], x.label) for x in s.shapes] == [([139.0, 35.0], None)]
    assert read("geom=" + "1" * 200_001).errors == ["geom=" + "1" * 40 + "…: 長すぎ（200,000 文字まで）"]


def test_too_many_shapes_or_vertices():
    s = read("&".join(["geom=35,139"] * (MAX_SHAPES + 5)))
    assert len(s.shapes) == MAX_SHAPES and s.errors == ["図形が多すぎ（1,000 個まで表示）"]
    line = ";".join(f"35,{139 + i / 1e6}" for i in range(60_001))
    s = read_link_shapes(params_of_query(f"line={line}&line={line}"), MAX_BODY_VALUE)
    assert len(s.shapes) == 1 and s.errors[0].endswith("頂点が多すぎ（全体で 100,000 点まで）")


def test_geojson_features_with_plain_properties_as_fields():
    collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [139.7, 35.6, 12]},
         "properties": {"label": "A", "kind": "mgrs", "n": 3, "x": 1.5, "ok": True, "Kind": "dup", "bad name!": 1, "list": [1], "big": 2**40}},
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[139, 35], [140, 35], [140, 36]]]}, "properties": {"name": "面", "n": 2.5}},
        {"type": "Feature", "geometry": None, "properties": {}},
    ]}
    s = read_link_shapes(params_of_json({"geojson": collection, "name": "AutoMap"}))
    assert s.errors == [] and s.title == "AutoMap"
    assert s.fields == [("kind", "string"), ("n", "double"), ("x", "double"), ("ok", "string"), ("big", "double"), ("name", "string")]
    assert [(x.geometry["type"], x.label) for x in s.shapes] == [("Point", "A"), ("Polygon", "面")]
    assert s.shapes[0].geometry["coordinates"] == [139.7, 35.6]
    assert s.shapes[0].properties["ok"] == "true" and s.shapes[1].geometry["coordinates"][0][-1] == [139, 35]
    # As text in a link too, and a geometry alone.
    s = read("geojson=" + json.dumps({"type": "LineString", "coordinates": [[1, 2], [3, 4]]}))
    assert s.shapes[0].geometry == {"type": "LineString", "coordinates": [[1, 2], [3, 4]]}
    for bad, why in (('{"type": "Point", "coordinates": [1]}', "GeoJSON の座標として読めない"), ("[1, 2]", "GeoJSON の type なし"),
                     ("{", "GeoJSON（JSON）として読めない"), ('{"type": "Point", "coordinates": [1, 95]}', "緯度経度の範囲外"),
                     ('{"type": "FeatureCollection", "features": []}', "図形なし"),
                     ('{"type": "Point", "coordinates": [true, 1]}', "GeoJSON の座標として読めない")):
        assert why in read_link_shapes([("geojson", bad)]).errors[0], bad
    deep = {"type": "Point", "coordinates": [1, 2]}
    for _ in range(12):
        deep = {"type": "GeometryCollection", "geometries": [deep]}
    assert "入れ子が深すぎ" in read_link_shapes([("geojson", deep)]).errors[0]


def test_json_bodies_and_query_strings():
    assert params_of_json({"geom": ["35,139", "36,140"], "label": ["a", None], "zoom": 15, "other": 1}) == [
        ("geom", "35,139"), ("geom", "36,140"), ("label", "a"), ("label", ""), ("zoom", "15")]
    with pytest.raises(LinkError):
        params_of_json({"geom": 35})
    with pytest.raises(LinkError):
        params_of_json([1])
    assert params_of_query("?geom=1,2&x=3") == [("geom", "1,2")]
    assert has_shapes([("label", "a"), ("polygon", "")]) and not has_shapes([("name", "a")])


def test_labels_names_and_colours_are_cleaned():
    s = read("geom=35,139&label=" + "%E2%80%AE悪%0A い" + "x" * 300 + "&name=" + "名" * 150 + "&zoom=30")
    assert s.shapes[0].label.startswith("悪 い") and len(s.shapes[0].label) == 200 and len(s.title) == 100 and s.zoom is None
    assert color_of("#abc") == "#aabbcc" and color_of("red") is None and color_of("12345") is None


def test_zoom_scale_is_the_viewers():
    assert zoom_scale(16) == pytest.approx(8530.9, abs=0.1)
