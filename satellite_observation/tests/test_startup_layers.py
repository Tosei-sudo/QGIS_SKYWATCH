"""The settings file's `layers` (the viewer's config.ts layerOf) and what each is in QGIS."""
import os

from satellite_observation.core.config import parse_config, read_config_file
from satellite_observation.core.startup_layers import StartupLayer, layer_source, resolve_url, startup_layers_of

HERE = os.path.dirname(__file__)


def test_the_viewers_example_reads_without_problems():
    config, problems = read_config_file(os.path.join(HERE, "data", "viewer-config.jsonc"))
    assert problems == []
    assert [l.type for l in config.startup_layers] == [
        "cog", "file", "wms", "wmts", "wfs", "esri", "esri", "esri", "ogc-features", "ogc-tiles", "wcs", "ogc-coverage", "xyz"]
    image = config.startup_layers[7]
    assert image.layer == "image" and image.settings == {"where": "CloudCover <= 0.2", "sortField": "AcquisitionDate"}
    assert config.startup_layers[8].bbox == [139.6, 35.6, 139.9, 35.8] and config.startup_layers[8].filter == "width > 5"


def test_wrong_entries_say_why_as_the_viewer_does():
    problems = []
    layers = startup_layers_of([
        "x", {"type": "cog"}, {"type": "wms", "url": "https://x/wms"}, {"type": "kml", "url": "./a.kml"},
        {"type": "ogc-features", "url": "https://x/api", "layer": "r", "bbox": [1, 2, 0, 3]}, {"type": "cog", "url": "C:\\a.tif"},
    ], problems)
    assert problems == [
        "layers[0] はオブジェクトで指定",
        "layers[1] に url（http(s) か相対パス）なし",
        "layers[2] に layer（レイヤー名）なし",
        "layers[3] の type「kml」は cog・file・wms・wmts・wfs・esri・ogc-features・ogc-tiles・wcs・ogc-coverage・xyz のいずれかで指定",
        "layers[4].bbox は [西, 南, 東, 北]（度）の 4 つの数で指定",
        "layers[5] に url（http(s) か相対パス）なし",
    ]
    assert [(l.type, l.bbox) for l in layers] == [("ogc-features", None)]
    assert parse_config({"layers": {}})[1] == ["layers は配列で指定"]


def test_relative_urls_follow_the_settings_file():
    assert resolve_url("./data/a.geojson", "https://web/viewer/config.json") == "https://web/viewer/data/a.geojson"
    assert resolve_url("../a.tif", "/srv/viewer/conf/config.json") == "/srv/viewer/a.tif"
    assert resolve_url("//cdn/x.tif", "") == "https://cdn/x.tif"
    assert resolve_url("https://x/a.tif", "/srv/config.json") == "https://x/a.tif"


def test_each_type_becomes_a_qgis_layer():
    def src(**kw):
        return layer_source(StartupLayer(**kw), "/srv/viewer/config.json", kw.pop("authcfg", None))

    assert (s := src(type="cog", url="https://x/o.tif")).provider == "gdal" and s.source == "/vsicurl/https://x/o.tif" and s.name == "o.tif"
    assert src(type="file", url="./data/st.geojson").source == "/srv/viewer/data/st.geojson"
    assert src(type="file", url="https://x/shp.zip").source == "/vsizip//vsicurl/https://x/shp.zip"
    assert src(type="file", url="https://x/a.TIF").kind == "raster"
    wms = src(type="wms", url="https://x/wms", layer="roads")
    assert wms.provider == "wms" and "layers=roads" in wms.source and wms.source.endswith("url=https://x/wms")
    wmts = src(type="wmts", url="https://x/wmts", layer="photo", matrix_set="GoogleMapsCompatible", format="image/jpeg")
    assert "tileMatrixSet=GoogleMapsCompatible" in wmts.source and "format=image/jpeg" in wmts.source and "REQUEST=GetCapabilities" in wmts.source
    assert src(type="wmts", url="https://x/wmts", layer="photo").notes == ["matrixSet なし（GoogleMapsCompatible を使用）"]
    assert src(type="wfs", url="https://x/wfs", layer="test:st").source == "url='https://x/wfs' typename='test:st' version='auto'"
    feature = layer_source(StartupLayer("esri", "https://x/arcgis/rest/services/X/FeatureServer", "0"), "", "abc1234")
    assert feature.provider == "arcgisfeatureserver" and "url='https://x/arcgis/rest/services/X/FeatureServer/0'" in feature.source
    assert "authcfg='abc1234'" in feature.source and feature.name == "X 0"
    image = src(type="esri", url="https://x/arcgis/rest/services/Scenes/ImageServer", layer="image", settings={"where": "a"})
    assert image.provider == "arcgismapserver" and "url='https://x/arcgis/rest/services/Scenes/ImageServer'" in image.source
    assert image.name == "Scenes" and image.notes == ["表示ルール（settings）は QGIS の ArcGIS プロバイダーで未対応"]
    oapif = src(type="ogc-features", url="https://x/api", layer="roads", filter="width > 5")
    assert oapif.provider == "OAPIF" and oapif.subset == "width > 5"
    tiles = src(type="ogc-tiles", url="https://x/api", layer="*", format="image/png")
    assert tiles.kind == "raster" and "type=xyz" in tiles.source and "tiles/WebMercatorQuad/{z}/{y}/{x}?f=png" in tiles.source
    assert src(type="ogc-tiles", url="https://x/api", layer="b").kind == "vectortile"
    assert src(type="wcs", url="https://x/ows", layer="dem").source == "identifier=dem&url=https://x/ows"
    assert src(type="ogc-coverage", url="https://x/api", layer="dem").source == "OGCAPI:https://x/api/collections/dem"
    xyz = src(type="xyz", url="https://x/t/{z}/{x}/{y}.png#name=%E8%88%AA%E7%A9%BA%E5%86%99%E7%9C%9F&zmax=18", layer="tiles")
    assert xyz.name == "航空写真" and "zmax=18" in xyz.source and "url=https://x/t/{z}/{x}/{y}.png" in xyz.source
