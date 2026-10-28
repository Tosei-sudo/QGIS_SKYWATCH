"""
「背景地図」 in QGIS: the settings file's base maps in the toolbar's list,
one layer at the bottom of the project, switched and removed, and
defaultBaseMap in an empty project.
"""
import http.server
import json
import threading
from dataclasses import replace

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.basemap_layer import PROPERTY, current_base_map  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

STYLE = {
    "version": 8,
    "sources": {"v": {"type": "vector", "tiles": ["./tiles/{z}/{x}/{y}.pbf"], "minzoom": 4, "maxzoom": 16}},
    "layers": [{"id": "water", "type": "fill", "source": "v", "source-layer": "water", "paint": {"fill-color": "#0000ff"}}],
}


@pytest.fixture()
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/style.json"):
                body = json.dumps(STYLE).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            else:
                body = b""
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture()
def plugin(tmp_path, server):
    QgsSettings().remove("satellite_observation")
    QgsProject.instance().clear()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "baseMaps": [
            {"id": "local", "label": "手元のタイル", "url": "./tiles/{z}/{x}/{y}.png", "maxZoom": 12,
             "attributions": "<a href=\"https://example.com\">例</a>"},
            {"id": "vector", "label": "ベクトル", "url": f"{server}/style.json"},
            {"id": "pbf", "label": "タイル", "url": f"{server}/t/{{z}}/{{x}}/{{y}}.pbf"},
        ],
        "defaultBaseMap": "local",
    }), encoding="utf-8")
    settings.set_config_source(str(config))
    from satellite_observation import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    yield p, iface, tmp_path
    p.unload()
    QgsProject.instance().clear()
    QgsSettings().remove("satellite_observation")


def entries(p):
    p.base_maps.fill()
    return [(a.text(), a.isChecked()) for a in p.base_maps.menu.actions()]


def test_switches_the_background_map(plugin):
    p, iface, tmp = plugin
    project = QgsProject.instance()
    # defaultBaseMap in the empty project.
    assert wait_for(lambda: current_base_map() == "local", 10000)
    [layer] = project.mapLayers().values()
    assert layer.name() == "手元のタイル" and layer.providerType() == "wms"
    assert f"file://{tmp}/tiles/{{z}}/{{x}}/{{y}}.png" in layer.source() and "zmax=12" in layer.source()
    assert entries(p) == [("なし", False), ("手元のタイル", True), ("ベクトル", False), ("タイル", False)]
    # Another layer on top: the base map stays at the bottom when switched.
    points = QgsVectorLayer("Point?crs=EPSG:4326", "点", "memory")
    project.addMapLayer(points)
    p.base_maps.choose("vector")
    assert wait_for(lambda: current_base_map() == "vector" and p.base_maps.task is None, 10000)
    layers = [n.layer() for n in project.layerTreeRoot().findLayers()]
    assert [x.name() for x in layers] == ["点", "ベクトル"]
    tiles = layers[1]
    assert tiles.customProperty(PROPERTY) == "vector"
    source = tiles.source()
    assert "tiles/%7Bz%7D" in source or "tiles/{z}" in source
    assert "zmin=4" in source and "zmax=16" in source and "styleUrl=" in source
    # Drawn as the style says.
    assert [st.styleName() for st in tiles.renderer().styles()] == ["water"]
    p.base_maps.choose("pbf")
    assert wait_for(lambda: current_base_map() == "pbf" and p.base_maps.task is None, 10000)
    assert len([x for x in project.mapLayers().values() if x.customProperty(PROPERTY)]) == 1
    assert entries(p)[3] == ("タイル", True)
    # なし: removed, the other layer stays.
    p.base_maps.choose("")
    assert current_base_map() == "" and list(project.mapLayers().values()) == [points]
    assert entries(p)[0] == ("なし", True)


def test_a_style_that_cannot_be_read_says_why(plugin):
    p, iface, tmp = plugin
    assert wait_for(lambda: current_base_map() == "local", 10000)
    # Nothing listens there.
    p.context.config.base_maps[1] = replace(p.context.config.base_maps[1], url="http://127.0.0.1:9/none.json")
    iface.bar.clearWidgets()
    p.base_maps.choose("vector")
    assert wait_for(lambda: p.base_maps.task is None, 15000)
    # The one shown stays.
    assert current_base_map() == "local"
    assert any("背景地図「ベクトル」" in w.text() for w in iface.bar.items())


def test_a_new_project_gets_the_default(plugin):
    p, iface, tmp = plugin
    assert wait_for(lambda: current_base_map() == "local", 10000)
    QgsProject.instance().clear()
    iface.newProjectCreated.emit()
    assert wait_for(lambda: current_base_map() == "local", 10000)
