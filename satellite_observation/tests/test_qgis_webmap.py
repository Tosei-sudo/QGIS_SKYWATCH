"""
ArcGIS Web Maps in QGIS against a local portal and ArcGIS server: a web map
opened as layers (filter, renderer, labels, aliases, scales, base map,
view, bookmarks), the project written back, QGIS styles as renderers, and
items read from and saved to a portal.
"""
import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsArcGisRestUtils, QgsBookmark, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
    QgsGraduatedSymbolRenderer, QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings, QgsProject, QgsRasterLayer, QgsRectangle,
    QgsRendererCategory, QgsRendererRange, QgsSettings, QgsSingleSymbolRenderer, QgsFillSymbol, QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

from satellite_observation.core.basemaps import BaseMapConfig  # noqa: E402
from satellite_observation.core.webmap import WebMapItemRef, plan_web_map  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.webmap_io import (  # noqa: E402
    KEPT, add_opened, auth_with_portal, fetch_web_map, open_plan, project_web_map, save_to_portal,
)
from satellite_observation.qgis_io.webmap_style import esri_labeling, esri_renderer  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

ID = "0123456789abcdef0123456789abcdef"
NEW_ID = "fedcba9876543210fedcba9876543210"

PARKS = {
    "currentVersion": 10.8, "id": 0, "name": "Parks", "type": "Feature Layer", "geometryType": "esriGeometryPoint", "objectIdField": "OBJECTID",
    "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "KIND", "type": "esriFieldTypeInteger"}, {"name": "NAME", "type": "esriFieldTypeString"}],
    "extent": {"xmin": 139, "ymin": 35, "xmax": 140, "ymax": 36, "spatialReference": {"wkid": 4326}}, "capabilities": "Query", "maxRecordCount": 1000,
    "advancedQueryCapabilities": {"supportsPagination": True},
    "drawingInfo": {"renderer": {"type": "simple", "symbol": {"type": "esriSMS", "style": "esriSMSCircle", "color": [0, 0, 0, 255], "size": 8}}},
}
MAP = {"currentVersion": 10.8, "mapName": "Base", "layers": [{"id": 0, "name": "a"}, {"id": 2, "name": "b"}], "spatialReference": {"wkid": 102100},
       "fullExtent": {"xmin": -2e7, "ymin": -2e7, "xmax": 2e7, "ymax": 2e7, "spatialReference": {"wkid": 102100}}, "capabilities": "Map", "singleFusedMapCache": False}
IMAGE = {"currentVersion": 10.8, "name": "Img", "serviceDataType": "esriImageServiceDataTypeRGB", "bandCount": 3, "pixelSizeX": 10, "pixelSizeY": 10,
         "extent": {"xmin": -2e7, "ymin": -2e7, "xmax": 2e7, "ymax": 2e7, "spatialReference": {"wkid": 102100}}, "spatialReference": {"wkid": 102100},
         "capabilities": "Image"}


def sms(color):
    return {"type": "esriSMS", "style": "esriSMSSquare", "color": color, "size": 10, "outline": {"color": [255, 255, 255, 255], "width": 1}}


def web_map(base):
    services = f"{base}/arcgis/rest/services"
    return {
        "version": "2.28",
        "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        "operationalLayers": [
            {"id": "parks", "title": "公園", "layerType": "ArcGISFeatureLayer", "url": f"{services}/Parks/FeatureServer/0", "visibility": False,
             "opacity": 0.8, "minScale": 500000,
             "layerDefinition": {"definitionExpression": "KIND = 1", "drawingInfo": {
                 "renderer": {"type": "uniqueValue", "field1": "KIND", "uniqueValueInfos": [{"value": "1", "label": "一", "symbol": sms([34, 139, 34, 255])},
                                                                                           {"value": "2", "label": "二", "symbol": sms([30, 144, 255, 255])}]},
                 "labelingInfo": [{"labelExpressionInfo": {"expression": "$feature.NAME"}, "symbol": {"type": "esriTS", "color": [0, 0, 0, 255], "font": {"size": 9}}}]}},
             "popupInfo": {"fieldInfos": [{"fieldName": "NAME", "label": "名前"}]}},
            {"title": "グループ", "layerType": "GroupLayer", "opacity": 0.5, "layers": [
                {"title": "地図", "layerType": "ArcGISMapServiceLayer", "url": f"{services}/Base/MapServer", "visibleLayers": [0, 2],
                 "layers": [{"id": 2, "layerDefinition": {"definitionExpression": "TYPE = 'A'"}}]},
                {"title": "画像", "layerType": "ArcGISImageServiceLayer", "url": f"{services}/Img/ImageServer", "renderingRule": {"rasterFunction": "NDVI"}},
            ]},
            {"title": "タイル", "layerType": "WebTiledLayer", "templateUrl": "https://{subDomain}.tiles.example.com/{level}/{col}/{row}.png", "subDomains": ["a"],
             "copyright": "例"},
            {"title": "KML", "layerType": "KML", "url": "https://example.com/a.kml"},
            {"title": "注記", "featureCollection": {"layers": [{
                "layerDefinition": {"name": "点", "geometryType": "esriGeometryPoint",
                                    "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "NAME", "type": "esriFieldTypeString", "alias": "名称"},
                                               {"name": "WHEN", "type": "esriFieldTypeDate"}],
                                    "drawingInfo": {"renderer": {"type": "simple", "symbol": sms([255, 0, 0, 128])}}},
                "featureSet": {"geometryType": "esriGeometryPoint", "spatialReference": {"wkid": 102100},
                               "features": [{"geometry": {"x": 15557000, "y": 4257000}, "attributes": {"OBJECTID": 1, "NAME": "東京", "WHEN": 1760000000000}}]},
            }]}},
        ],
        "baseMap": {"title": "地理院", "baseMapLayers": [{"layerType": "WebTiledLayer", "title": "標準", "templateUrl": "https://cyberjapandata.gsi.go.jp/xyz/std/{level}/{col}/{row}.png"}]},
        "initialState": {"viewpoint": {"targetGeometry": {"xmin": 15540000, "ymin": 4240000, "xmax": 15580000, "ymax": 4270000, "spatialReference": {"wkid": 102100}}}},
        "bookmarks": [{"name": "東京駅", "extent": {"xmin": 15558000, "ymin": 4256000, "xmax": 15560000, "ymax": 4258000, "spatialReference": {"wkid": 102100}}}],
    }


@pytest.fixture(scope="module")
def server():
    """A portal (/portal) and an ArcGIS server (/arcgis) on one local port; `posts` records the portal's writes."""
    state = {"posts": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            self.route(form)

        def do_GET(self):  # noqa: N802
            self.route({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})

        def route(self, form):
            path = urlparse(self.path).path
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            rest = "/portal/sharing/rest"
            if path == f"{rest}/content/items/{ID}":
                return self.answer({"id": ID, "type": "Web Map", "title": "テスト地図", "owner": "tsudo", "extent": [[139, 35], [140, 36]]})
            if path == f"{rest}/content/items/{ID}/data":
                return self.answer(web_map(base))
            if path == f"{rest}/content/items/{'1' * 32}":
                return self.answer({"id": "1" * 32, "type": "Feature Service"})
            if path == f"{rest}/community/self":
                return self.answer({"username": "tsudo"})
            if path == f"{rest}/content/users/tsudo":
                return self.answer({"folders": [{"id": "f1", "title": "地図"}]})
            if path.startswith(f"{rest}/content/users/tsudo"):
                state["posts"].append((path, form))
                return self.answer({"success": True, "id": NEW_ID})
            if path.endswith("/FeatureServer/0/query"):
                ids = [1] if "KIND" in form.get("where", "") else [1, 2]
                if form.get("returnIdsOnly") == "true":
                    return self.answer({"objectIdFieldName": "OBJECTID", "objectIds": ids})
                if form.get("returnCountOnly") == "true":
                    return self.answer({"count": len(ids)})
                if form.get("returnExtentOnly") == "true":
                    return self.answer({"extent": PARKS["extent"]})
                return self.answer({"features": [{"attributes": {"OBJECTID": i, "KIND": i, "NAME": f"公園{i}"}, "geometry": {"x": 139 + i / 10, "y": 35.5}} for i in ids],
                                    "spatialReference": {"wkid": 4326}})
            if path.endswith("/FeatureServer/0"):
                return self.answer(PARKS)
            if path.endswith("/FeatureServer"):
                return self.answer({"layers": [{"id": 0, "name": "Parks"}], "spatialReference": {"wkid": 4326}})
            if path.endswith("/MapServer"):
                return self.answer(MAP)
            if path.endswith("/ImageServer"):
                return self.answer(IMAGE)
            self.send_response(404)
            self.end_headers()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()


@pytest.fixture
def clean():
    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    yield
    QgsProject.instance().clear()
    QgsSettings().remove("satellite_observation")


def no_auth(url):
    return None


def test_a_web_map_opened_as_layers(server, clean):
    base, _ = server
    gsi = BaseMapConfig("gsi", "地理院 標準", "xyz", "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png")
    opened = open_plan(plan_web_map(web_map(base)), "テスト地図", [gsi], no_auth)
    assert opened.basemap_id == "gsi" and opened.basemap_title == "地理院 標準"
    assert opened.problems == [("KML", "KML レイヤーには非対応")]
    assert [o.layer.name() for o in opened.layers] == ["公園", "地図", "画像", "タイル", "注記"]
    notes = dict(opened.notes)
    assert "レイヤーごとの定義式" in notes["地図"] and "表示ルール（renderingRule）" in notes["画像"]
    parks, map_layer, image, tiles, collection = (o.layer for o in opened.layers)
    assert not opened.layers[0].visible and opened.layers[1].visible
    assert parks.subsetString() == "KIND = 1" and parks.featureCount() == 1
    assert parks.opacity() == pytest.approx(0.8) and parks.hasScaleBasedVisibility() and parks.minimumScale() == 500000
    assert isinstance(parks.renderer(), QgsCategorizedSymbolRenderer) and parks.renderer().classAttribute() == "KIND"
    assert parks.labelsEnabled() and parks.fields().field("NAME").alias() == "名前"
    assert map_layer.opacity() == pytest.approx(0.5) and "layer='0,2'" in map_layer.source()
    assert image.providerType() == "arcgismapserver" and image.isValid()
    assert json.loads(tiles.customProperty(KEPT))["layerType"] == "WebTiledLayer" and "a.tiles.example.com" in tiles.source()
    assert collection.providerType() == "memory" and collection.featureCount() == 1
    feature = next(collection.getFeatures())
    assert feature["NAME"] == "東京" and feature["WHEN"].isValid() and collection.fields().field("NAME").alias() == "名称"
    assert collection.crs().authid() == "EPSG:3857" and feature.geometry().asPoint().x() == pytest.approx(15557000)

    iface = FakeIface()
    group = add_opened(opened, iface.mapCanvas())
    project = QgsProject.instance()
    assert group.name() == "テスト地図" and project.layerTreeRoot().children()[0] is group
    # Bottom first in the web map: the last on top of the group.
    assert [node.layer().name() for node in group.findLayers()] == ["注記", "タイル", "画像", "地図", "公園"]
    assert not group.findLayer(parks.id()).itemVisibilityChecked()
    assert project.crs().authid() == "EPSG:3857"
    assert [b.name() for b in project.bookmarkManager().bookmarks()] == ["東京駅"]


def test_a_base_map_not_in_the_settings_file_opens_as_layers_and_is_written_back(server, clean):
    base, _ = server
    plan = plan_web_map(web_map(base))
    opened = open_plan(plan, "テスト地図", [], no_auth)
    assert opened.basemap_id is None and opened.basemap_title == "地理院"
    assert opened.layers[0].basemap and opened.layers[0].layer.name() == "背景: 標準"
    add_opened(opened, FakeIface().mapCanvas())
    result = project_web_map([])
    assert result.json["baseMap"] == plan.basemap.source
    assert "背景: 標準" not in result.included


def test_the_project_written_as_a_web_map(server, clean, tmp_path):
    base, _ = server
    opened = open_plan(plan_web_map(web_map(base)), "テスト地図", [], no_auth)
    iface = FakeIface()
    add_opened(opened, iface.mapCanvas())
    project = QgsProject.instance()
    # A memory layer of our own and an image file.
    memory = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "範囲", "memory")
    f = QgsFeature(memory.fields())
    f.setGeometry(QgsGeometry.fromRect(QgsRectangle(139, 35, 139.1, 35.1)))
    f.setAttributes(["A"])
    memory.dataProvider().addFeatures([f])
    memory.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "255,0,0,128", "outline_color": "0,0,255"})))
    project.addMapLayer(memory)
    path = str(tmp_path / "scene.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([139, 0.01, 0, 35.1, 0, -0.01])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds = None
    project.addMapLayer(QgsRasterLayer(path, "scene"))
    project.bookmarkManager().addBookmark(QgsBookmark())
    iface.mapCanvas().setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    iface.mapCanvas().setExtent(QgsRectangle(15540000, 4240000, 15580000, 4270000))

    result = project_web_map([], iface.mapCanvas())
    out = json.loads(json.dumps(result.json))
    assert result.included == ["公園", "地図", "画像", "タイル", "注記", "範囲"]
    assert result.skipped == [("scene", "画像（ファイル）は Web マップへの書き込み不可")]
    parks, map_layer, image, tiles, collection, area = out["operationalLayers"]
    assert (parks["visibility"], parks["opacity"], parks["minScale"]) == (False, 0.8, 500000)
    assert parks["layerDefinition"]["definitionExpression"] == "KIND = 1"
    renderer = parks["layerDefinition"]["drawingInfo"]["renderer"]
    assert renderer["type"] == "uniqueValue" and renderer["field1"] == "KIND"
    assert [(i["value"], i["symbol"]["color"]) for i in renderer["uniqueValueInfos"]] == [("1", [34, 139, 34, 255]), ("2", [30, 144, 255, 255])]
    assert parks["layerDefinition"]["drawingInfo"]["labelingInfo"][0]["labelExpressionInfo"]["expression"] == '$feature["NAME"]'
    assert map_layer["visibleLayers"] == [0, 2] and map_layer["layers"] == [{"id": 2, "layerDefinition": {"definitionExpression": "TYPE = 'A'"}}]
    assert image["layerType"] == "ArcGISImageServiceLayer" and image["renderingRule"] == {"rasterFunction": "NDVI"}
    assert tiles["templateUrl"] == "https://{subDomain}.tiles.example.com/{level}/{col}/{row}.png" and tiles["title"] == "タイル"
    point = collection["featureCollection"]["layers"][0]
    assert point["featureSet"]["features"][0]["attributes"]["NAME"] == "東京"
    polygon = area["featureCollection"]["layers"][0]
    assert polygon["layerDefinition"]["geometryType"] == "esriGeometryPolygon"
    assert polygon["layerDefinition"]["drawingInfo"]["renderer"]["symbol"]["color"] == [255, 0, 0, 128]
    ring = polygon["featureSet"]["features"][0]["geometry"]["rings"][0]
    assert min(x for x, _ in ring) == pytest.approx(139 * 20037508.34 / 180, abs=0.1)
    view = out["initialState"]["viewpoint"]["targetGeometry"]
    assert view["xmin"] <= 15540000 and view["xmax"] >= 15580000
    assert [b["name"] for b in out["bookmarks"]] == ["東京駅", ""]
    # Read back: the same layers, in the same order.
    plan = plan_web_map(out)
    assert [x.title for x in plan.layers] == ["公園", "地図", "画像", "タイル", "注記", "範囲"]


def test_qgis_styles_as_renderers_and_labels(clean):
    points = QgsVectorLayer("Point?crs=EPSG:4326&field=kind:string&field=name:string", "点", "memory")
    symbol = QgsMarkerSymbol.createSimple({"name": "square", "color": "0,255,0", "size": "4", "outline_color": "17,17,17"})
    other = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,255"})
    points.setRenderer(QgsCategorizedSymbolRenderer("kind", [QgsRendererCategory("1", symbol, "一"), QgsRendererCategory("2", other.clone(), "二", False),
                                                            QgsRendererCategory(None, other.clone(), "その他")]))
    r = esri_renderer(points, "point")
    assert r["type"] == "uniqueValue" and r["field1"] == "kind"
    assert [(i["value"], i["symbol"]["style"], i["symbol"]["color"]) for i in r["uniqueValueInfos"]] == [("1", "esriSMSSquare", [0, 255, 0, 255])]
    assert r["uniqueValueInfos"][0]["symbol"]["size"] == pytest.approx(11.34)
    assert r["defaultSymbol"]["color"] == [0, 0, 255, 255]
    # Read back by QGIS: the same colors.
    back = QgsArcGisRestUtils.convertRenderer(r)
    assert [c.symbol().color().name() for c in back.categories() if c.value() == "1"] == ["#00ff00"]

    lines = QgsVectorLayer("LineString?crs=EPSG:4326&field=area:double", "線", "memory")
    red = QgsLineSymbol.createSimple({"color": "255,0,0", "line_width": "0.5", "line_style": "dash"})
    blue = QgsLineSymbol.createSimple({"color": "0,0,255", "line_width": "0.5"})
    lines.setRenderer(QgsGraduatedSymbolRenderer("area", [QgsRendererRange(100, 500, blue, "大"), QgsRendererRange(0, 100, red, "小")]))
    r = esri_renderer(lines, "line")
    assert r["type"] == "classBreaks" and r["minValue"] == 0
    assert [(i["classMinValue"], i["classMaxValue"], i["symbol"]["color"], i["symbol"]["style"]) for i in r["classBreakInfos"]] == [
        (0, 100, [255, 0, 0, 255], "esriSLSDash"), (100, 500, [0, 0, 255, 255], "esriSLSSolid")]
    # An expression renderer cannot be written.
    lines.setRenderer(QgsGraduatedSymbolRenderer("area * 2", [QgsRendererRange(0, 1, QgsLineSymbol.createSimple({}), "a")]))
    assert esri_renderer(lines, "line") is None

    labels = QgsPalLayerSettings()
    labels.fieldName = "name"
    labels.isExpression = False
    points.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    points.setLabelsEnabled(True)
    assert esri_labeling(points)[0]["labelExpressionInfo"]["expression"] == '$feature["name"]'
    labels.fieldName = 'upper("name")'
    labels.isExpression = True
    points.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    assert esri_labeling(points) is None


def test_a_sign_in_only_where_it_may_go(clean):
    auth = auth_with_portal("https://gis.example.com/portal", "abc1234")
    assert auth("https://gis.example.com/server/rest/services/A/FeatureServer/0") == "abc1234"
    assert auth("https://evil.example.org/FeatureServer/0") is None
    settings.set_auth_map({"https://other.example.org/arcgis": "own0001"})
    assert auth("https://other.example.org/arcgis/rest/services/A/MapServer") == "own0001"


def test_items_read_from_and_saved_to_a_portal(server, clean):
    base, state = server
    portal = f"{base}/portal"
    result = {}

    from satellite_observation.qgis_io.tasks import run_task

    run_task("t", lambda task: fetch_web_map(WebMapItemRef(portal, ID), no_auth), lambda value: result.update(item=value))
    assert wait_for(lambda: "item" in result, 10000)
    item, data = result["item"]
    assert item["title"] == "テスト地図" and data["version"] == "2.28"
    failed = []
    run_task("t", lambda task: fetch_web_map(WebMapItemRef(portal, "1" * 32), no_auth), None, failed.append)
    assert wait_for(lambda: failed, 10000) and "Web マップ以外のアイテム（種類: Feature Service）" in failed[0]

    saved = []
    built = project_web_map([])
    run_task("t", lambda task: save_to_portal(portal, no_auth, built, "新しい地図", "a,b", folder="地図", extent=(139, 35, 140, 36)),
             saved.append)
    assert wait_for(lambda: saved, 10000)
    assert saved[0] == (NEW_ID, "tsudo")
    path, form = state["posts"][-1]
    assert path.endswith("/users/tsudo/f1/addItem")
    assert (form["type"], form["title"], form["tags"], form["extent"]) == ("Web Map", "新しい地図", "a,b", "139,35,140,36")
    assert json.loads(form["text"])["version"].startswith("2.")
    run_task("t", lambda task: save_to_portal(portal, no_auth, built, "上書き", "", user="tsudo", update=ID), saved.append)
    assert wait_for(lambda: len(saved) == 2, 10000)
    assert state["posts"][-1][0].endswith(f"/users/tsudo/items/{ID}/update") and saved[1] == (ID, "tsudo")


@pytest.fixture
def plugin(clean):
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p, iface
    p.unload()


def test_the_dialogs_open_an_item_and_save_over_it(server, plugin, monkeypatch, tmp_path):
    base, state = server
    p, iface = plugin
    dialogs = p.webmaps
    import satellite_observation.ui.webmap_dialog as module

    monkeypatch.setattr(module, "auth_with_portal", lambda portal, authcfg: no_auth)
    dialogs.show_open()
    dialogs.open_dialog.item.setText(f"{base}/portal/home/item.html?id={ID}")
    dialogs.open_dialog.open_typed()
    assert wait_for(lambda: dialogs.task is None and dialogs.opened is not None, 15000)
    assert dialogs.opened == (WebMapItemRef(f"{base}/portal", ID), "テスト地図")
    assert dialogs.open_dialog.status.text() == "Web マップ「テスト地図」の 5 レイヤーを表示（1 レイヤーは読み込み失敗）"
    texts = [dialogs.open_dialog.result.item(i).text() for i in range(dialogs.open_dialog.result.count())]
    assert "読み込み失敗: KML — KML レイヤーには非対応" in texts and "背景地図は「地理院 標準」に設定" in texts
    assert QgsSettings().value("satellite_observation/webmap_portal") == f"{base}/portal"

    dialogs.show_save()
    save = dialogs.save_dialog
    assert save.title.text() == "テスト地図" and save.update.isVisibleTo(save)
    assert save.summary.text().startswith("5 レイヤーを書き込み")
    # Without a sign-in, nothing is sent.
    save.save_portal()
    assert save.status.text() == "ポータルに保存するには認証設定を選択"
    monkeypatch.setattr(save.portal, "keep", lambda portal: "fake001")
    asked = []
    save.confirm = lambda text, action: asked.append((text, action)) or False
    save.update.setChecked(True)
    save.save_portal()
    assert asked[-1][1] == "上書き" and ID in asked[-1][0] and save.status.text() == "保存取消"
    posts = len(state["posts"])
    save.confirm = lambda text, action: True
    save.update.setChecked(True)
    save.save_portal()
    assert wait_for(lambda: len(state["posts"]) > posts, 10000)
    assert wait_for(lambda: "上書き保存完了" in save.status.text(), 10000)
    path = save.save_file(str(tmp_path / "web.json"))
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["operationalLayers"][0]["title"] == "公園"


def test_a_file_that_is_not_a_web_map_says_so(plugin, tmp_path):
    p, _ = plugin
    path = tmp_path / "x.json"
    path.write_text('{"hello": 1}')
    p.webmaps.open_file(str(path))
    assert p.webmaps.open_dialog.status.text() == "読み込み失敗: Web マップの JSON 以外（operationalLayers・baseMap なし）"
    path.write_text("not json")
    p.webmaps.open_file(str(path))
    assert p.webmaps.open_dialog.status.text() == "読み込み失敗: JSON として読み込み不可"
