"""The viewer's webmap.test.ts, for core/webmap.py (the renderer cases are in test_qgis_webmap.py: QGIS makes the styles)."""
import json

import pytest

from satellite_observation.core.basemaps import BaseMapConfig
from satellite_observation.core.webmap import (
    MAX_COLLECTION_FEATURES, SaveLayer, ServiceRef, WebMapItemRef, build_web_map, label_field_of, match_base_map, parse_item_input,
    plan_web_map, portal_root, template_of, tile_urls, token_allowed,
)

ID = "0123456789abcdef0123456789abcdef"


def test_item_pages_map_viewer_links_rest_urls_and_bare_ids():
    assert parse_item_input(f"https://www.arcgis.com/home/item.html?id={ID}") == WebMapItemRef("https://www.arcgis.com", ID)
    assert parse_item_input(f"https://org.maps.arcgis.com/apps/mapviewer/index.html?webmap={ID}") == WebMapItemRef("https://org.maps.arcgis.com", ID)
    assert parse_item_input(f"https://gis.example.com/portal/home/webmap/viewer.html?webmap={ID.upper()}") == WebMapItemRef("https://gis.example.com/portal", ID)
    assert parse_item_input(f"https://gis.example.com/portal/sharing/rest/content/items/{ID}/data") == WebMapItemRef("https://gis.example.com/portal", ID)
    assert parse_item_input(ID, "https://gis.example.com/portal/home/") == WebMapItemRef("https://gis.example.com/portal", ID)
    assert parse_item_input(ID) == WebMapItemRef("https://www.arcgis.com", ID)


def test_what_is_not_an_item_is_refused():
    assert parse_item_input("https://www.arcgis.com/home/item.html?id=abc") is None
    assert parse_item_input(f"javascript:alert(1)//?id={ID}") is None
    assert parse_item_input("hello") is None
    assert portal_root("ftp://x") is None


def test_a_sign_in_goes_only_to_the_portal_host_or_among_arcgis_online_hosts():
    assert token_allowed("https://gis.example.com/server/rest/services/A/FeatureServer/0", "https://gis.example.com/portal")
    assert token_allowed("https://services1.arcgis.com/x/arcgis/rest/services/A/FeatureServer/0", "https://org.maps.arcgis.com")
    assert not token_allowed("https://evil.example.org/FeatureServer/0", "https://gis.example.com/portal")
    assert not token_allowed("http://gis.example.com/FeatureServer/0", "https://gis.example.com/portal")
    assert token_allowed("http://localhost:4175/svc/x", "http://localhost:4175/svc/portal")


def sms(color, size=10):
    return {"type": "esriSMS", "style": "esriSMSSquare", "color": color, "size": size, "outline": {"color": [255, 255, 255, 255], "width": 1}}


WEBMAP = {
    "version": "2.28",
    "spatialReference": {"wkid": 102100, "latestWkid": 3857},
    "operationalLayers": [
        {
            "id": "parks", "title": "公園", "layerType": "ArcGISFeatureLayer", "url": "https://example.com/arcgis/rest/services/Parks/FeatureServer/0",
            "visibility": False, "opacity": 0.8, "minScale": 500000,
            "layerDefinition": {
                "definitionExpression": "KIND = 1",
                "drawingInfo": {
                    "renderer": {"type": "uniqueValue", "field1": "KIND", "uniqueValueInfos": [{"value": "1", "symbol": sms([34, 139, 34, 255])}, {"value": "2", "symbol": sms([30, 144, 255, 255])}]},
                    "labelingInfo": [{"labelExpressionInfo": {"expression": "$feature.NAME"}}],
                },
            },
            "popupInfo": {"fieldInfos": [{"fieldName": "NAME", "label": "名前"}, {"fieldName": "KIND", "label": "KIND"}]},
        },
        {
            "title": "グループ", "layerType": "GroupLayer", "visibility": True, "opacity": 0.5,
            "layers": [
                {"title": "地図", "layerType": "ArcGISMapServiceLayer", "url": "https://example.com/arcgis/rest/services/Base/MapServer", "visibleLayers": [0, 2],
                 "layers": [{"id": 2, "layerDefinition": {"definitionExpression": "TYPE = 'A'"}}]},
                {"title": "画像", "layerType": "ArcGISImageServiceLayer", "url": "https://example.com/arcgis/rest/services/Img/ImageServer", "opacity": 0.5,
                 "renderingRule": {"rasterFunction": "NDVI"}, "mosaicRule": {"mosaicMethod": "esriMosaicNone", "where": "Cloud < 0.2"}},
            ],
        },
        {"title": "WMS", "layerType": "WMS", "url": "https://example.com/wms", "visibleLayers": ["a", "b"]},
        {"title": "WMTS", "layerType": "WMTS", "wmtsInfo": {"url": "https://example.com/wmts", "layerIdentifier": "ortho", "tileMatrixSet": "GoogleMapsCompatible"}, "format": "image/png"},
        {"title": "タイル", "layerType": "WebTiledLayer", "templateUrl": "https://{subDomain}.tiles.example.com/{level}/{col}/{row}.png", "subDomains": ["a", "b"]},
        {"title": "KML", "layerType": "KML", "url": "https://example.com/a.kml"},
        {
            "title": "注記",
            "featureCollection": {"layers": [{
                "layerDefinition": {"name": "点", "geometryType": "esriGeometryPoint",
                                    "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "NAME", "type": "esriFieldTypeString"}],
                                    "drawingInfo": {"renderer": {"type": "simple", "symbol": sms([255, 0, 0, 128], 12)}}},
                "featureSet": {"geometryType": "esriGeometryPoint", "spatialReference": {"wkid": 102100},
                               "features": [{"geometry": {"x": 15557000, "y": 4257000}, "attributes": {"OBJECTID": 1, "NAME": "東京"}}]},
            }]},
        },
    ],
    "baseMap": {"title": "Topographic", "baseMapLayers": [{"layerType": "VectorTileLayer", "title": "World Topographic",
                                                          "styleUrl": "https://basemaps.example.com/arcgis/rest/services/Topo/VectorTileServer/resources/styles/root.json"}]},
    "initialState": {"viewpoint": {"targetGeometry": {"xmin": 15540000, "ymin": 4240000, "xmax": 15580000, "ymax": 4270000, "spatialReference": {"wkid": 102100}}}},
    "bookmarks": [{"name": "東京駅", "extent": {"xmin": 15558000, "ymin": 4256000, "xmax": 15560000, "ymax": 4258000, "spatialReference": {"wkid": 102100}}}],
}


@pytest.fixture(scope="module")
def plan():
    return plan_web_map(WEBMAP)


def test_feature_layers_with_their_where_visibility_opacity_scales_renderer_and_labels(plan):
    parks = plan.layers[0]
    assert parks.type == "service" and parks.esri
    assert parks.ref == ServiceRef("esri", "https://example.com/arcgis/rest/services/Parks/FeatureServer/0", "0", {"where": "KIND = 1"})
    assert (parks.visible, parks.opacity, parks.min_scale, parks.max_scale) == (False, 0.8, 500000, None)
    assert parks.renderer["type"] == "uniqueValue" and label_field_of(parks.labeling) == "NAME"
    assert parks.aliases == {"NAME": "名前"}


def test_groups_flattened_multiplying_their_opacity_and_map_and_image_settings(plan):
    map_layer, image = plan.layers[1:3]
    assert (map_layer.title, map_layer.opacity, map_layer.ref.layer) == ("地図", 0.5, "map")
    assert map_layer.ref.settings == {"layers": [0, 2], "layerDefs": {"2": "TYPE = 'A'"}}
    assert image.opacity == 0.25 and image.ref.layer == "image"
    assert image.ref.settings == {"renderingRule": '{"rasterFunction":"NDVI"}', "where": "Cloud < 0.2", "method": "esriMosaicNone"}


def test_wms_one_layer_per_name_wmts_and_web_tiled_layers(plan):
    assert [(x.title, x.ref.layer) for x in plan.layers[3:5]] == [("WMS: a", "a"), ("WMS: b", "b")]
    assert plan.layers[5].ref == ServiceRef("wmts", "https://example.com/wmts", "ortho", matrix_set="GoogleMapsCompatible", format="image/png")
    assert plan.layers[6].type == "tiles"
    assert plan.layers[6].urls == ["https://a.tiles.example.com/{z}/{x}/{y}.png", "https://b.tiles.example.com/{z}/{x}/{y}.png"]


def test_feature_collections_and_what_cannot_be_shown(plan):
    notes = plan.layers[7]
    assert notes.type == "collection"
    part = notes.parts[0]
    assert (part.name, part.geometry_type, part.wkid, len(part.features)) == ("点", "esriGeometryPoint", 102100, 1)
    assert part.features[0]["attributes"]["NAME"] == "東京" and part.renderer["type"] == "simple"
    assert plan.unsupported == [("KML", "KML レイヤーには非対応")]


def test_the_base_map_the_extent_and_the_bookmarks(plan):
    assert plan.basemap.title == "Topographic"
    first = plan.basemap.layers[0]
    assert (first.type, first.url, first.reference) == ("vector-tiles", "https://basemaps.example.com/arcgis/rest/services/Topo/VectorTileServer", False)
    e = plan.extent
    assert (e.xmin, e.ymin, e.xmax, e.ymax, e.wkid) == (15540000, 4240000, 15580000, 4270000, 102100)
    assert plan.bookmarks[0][0] == "東京駅"


def test_the_base_map_matched_with_the_settings_file_by_url(plan):
    configs = [
        BaseMapConfig("gsi", "地理院", "xyz", "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"),
        BaseMapConfig("topo", "Topo", "esri", "https://basemaps.example.com/arcgis/rest/services/Topo/VectorTileServer", max_zoom=22),
    ]
    assert match_base_map(plan.basemap, configs).id == "topo"
    assert match_base_map(plan.basemap, configs[:1]) is None


def test_documents_that_are_not_web_maps_are_refused():
    with pytest.raises(ValueError):
        plan_web_map({"hello": 1})


def test_label_expressions_tiles_and_templates():
    assert label_field_of([{"labelExpressionInfo": {"expression": '$feature["名 前"]'}}]) == "名 前"
    assert label_field_of([{"labelExpression": "[NAME]"}]) == "NAME"
    assert label_field_of([{"labelExpressionInfo": {"expression": "Upper($feature.NAME)"}}]) is None
    assert tile_urls("https://t/{level}/{row}/{col}", None) == ["https://t/{z}/{y}/{x}"]
    assert template_of("https://{a-c}.tile.example/{z}/{x}/{y}.png") == "https://{subDomain}.tile.example/{level}/{col}/{row}.png"


def test_service_layers_feature_collections_the_view_the_base_map_and_bookmarks_written():
    renderer = {"type": "simple", "symbol": {"type": "esriSMS", "style": "esriSMSCircle", "color": [51, 102, 255, 255], "size": 6}}
    labeling = [{"labelExpressionInfo": {"expression": '$feature["NAME"]'}}]
    result = build_web_map(
        [
            SaveLayer("公園", True, 0.7, "ref", min_scale=100000, ref=ServiceRef("esri", "https://e.com/FeatureServer/0", "0", {"where": "KIND = 1"}),
                      renderer=renderer, labeling=labeling),
            SaveLayer("地図", False, 1, "ref", ref=ServiceRef("esri", "https://e.com/MapServer", "map", {"layers": [1], "layerDefs": {"1": "A = 1"}})),
            SaveLayer("WMS", True, 1, "ref", ref=ServiceRef("wms", "https://e.com/wms?SERVICE=WMS&map=x", "roads")),
            SaveLayer("一時", True, 1, "features", fields=[{"name": "NAME", "alias": "名前", "type": "esriFieldTypeString"}],
                      features=[("point", {"x": 15557000.123, "y": 4257000.0}, {"NAME": "東京"}),
                                ("polygon", {"rings": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 0.0]]]}, {"NAME": "面"})]),
            SaveLayer("画像.tif", True, 1, "none", reason="画像は書き込み不可"),
        ],
        extent=(0, 0, 100, 100),
        basemap=BaseMapConfig("gsi", "地理院", "xyz", "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png", '<a href="x">国土地理院</a>'),
        bookmarks=[("B", (40, 40, 60, 60))],
    )
    out = json.loads(json.dumps(result.json))
    assert out["version"].startswith("2.")
    assert result.included == ["公園", "地図", "WMS", "一時"]
    assert result.skipped == [("画像.tif", "画像は書き込み不可")]
    parks, map_layer, wms, temp = out["operationalLayers"]
    assert {k: parks[k] for k in ("layerType", "url", "visibility", "opacity", "minScale", "showLabels")} == {
        "layerType": "ArcGISFeatureLayer", "url": "https://e.com/FeatureServer/0", "visibility": True, "opacity": 0.7, "minScale": 100000, "showLabels": True}
    assert parks["layerDefinition"]["definitionExpression"] == "KIND = 1"
    assert parks["layerDefinition"]["drawingInfo"] == {"renderer": renderer, "labelingInfo": labeling}
    assert map_layer["visibleLayers"] == [1] and map_layer["layers"] == [{"id": 1, "layerDefinition": {"definitionExpression": "A = 1"}}]
    assert (wms["layerType"], wms["url"], wms["visibleLayers"]) == ("WMS", "https://e.com/wms?map=x", ["roads"])
    assert [p["layerDefinition"]["geometryType"] for p in temp["featureCollection"]["layers"]] == ["esriGeometryPoint", "esriGeometryPolygon"]
    assert temp["featureCollection"]["layers"][0]["featureSet"]["features"][0] == {"geometry": {"x": 15557000.12, "y": 4257000.0}, "attributes": {"__OBJECTID": 1, "NAME": "東京"}}
    base = out["baseMap"]["baseMapLayers"][0]
    assert (base["layerType"], base["templateUrl"], base["copyright"]) == ("WebTiledLayer", "https://cyberjapandata.gsi.go.jp/xyz/std/{level}/{col}/{row}.png", "国土地理院")
    assert out["initialState"]["viewpoint"]["targetGeometry"]["spatialReference"]["wkid"] == 102100
    assert out["bookmarks"][0]["name"] == "B" and out["bookmarks"][0]["extent"]["xmax"] == 60


def test_what_is_written_reads_back():
    result = build_web_map([
        SaveLayer("公園", False, 0.5, "ref", ref=ServiceRef("esri", "https://e.com/FeatureServer/0", "0", {"where": "X = 1"})),
        SaveLayer("点", True, 1, "features", fields=[{"name": "NAME", "type": "esriFieldTypeString"}], features=[("point", {"x": 1.0, "y": 2.0}, {"NAME": "a"})]),
    ])
    plan = plan_web_map(json.loads(json.dumps(result.json)))
    assert (plan.layers[0].visible, plan.layers[0].opacity, plan.layers[0].ref.settings) == (False, 0.5, {"where": "X = 1"})
    assert plan.layers[1].type == "collection" and plan.layers[1].parts[0].features[0]["attributes"]["NAME"] == "a"
    assert result.json["baseMap"] == {"title": "背景なし", "baseMapLayers": []}


def test_feature_collections_that_are_too_big_are_left_out():
    many = [("point", {"x": float(i), "y": float(i)}, {}) for i in range(MAX_COLLECTION_FEATURES + 1)]
    result = build_web_map([SaveLayer("多い", True, 1, "features", features=many)])
    assert result.included == [] and result.skipped[0][0] == "多い"
