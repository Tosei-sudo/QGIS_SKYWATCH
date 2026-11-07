"""
The plugin in QGIS against scripts/mock_esri over real HTTP (QGIS's network
access manager): the settings file the mock writes, the catalog search and
an image opened from it, its external AI results, the observation store with
its classification, and the mock's layers in QGIS's own ArcGIS provider.
"""
import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings, QgsVectorLayer  # noqa: E402

from satellite_observation.core.esri import EsriClient  # noqa: E402
from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.qgis_io.context import PluginContext  # noqa: E402
from satellite_observation.qgis_io.network import qgis_transport  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_mock_esri import mock_esri, sample, transport  # noqa: E402


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    return str(tmp_path_factory.mktemp("mock_files"))


@pytest.fixture
def plugin(tmp_path, files, monkeypatch):
    store = mock_esri.Store(str(tmp_path / "mock.sqlite"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mock_esri.make_handler(mock_esri.Service(store, log=lambda *_: None), files, quiet=True))
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    mock_esri.load_services(store, sample.services(files, base))
    sample.write_images(files)
    config = tmp_path / "config.mock.jsonc"
    sample.write_config(str(config), base, files)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    QgsSettings().remove("satellite_observation")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_config_source(str(config))
    # The real network, not the tests' offline client.
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(qgis_transport(settings.auth_for)))
    from satellite_observation import classFactory

    iface = FakeIface()
    iface.canvas.setDestinationCrs(QgsProject.instance().crs())
    p = classFactory(iface)
    p.initGui()
    assert p.api.problems == [], p.api.problems
    yield p, iface, base
    p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("satellite_observation")
    httpd.shutdown()
    httpd.server_close()


def test_catalog_image_ai_results_and_observations_from_the_mock(plugin):
    p, iface, _ = plugin
    dock = p.api.image_catalog
    dock.show()
    dock.area_box.setCurrentIndex(dock.area_box.findData("none"))
    dock.search()
    assert wait_for(lambda: dock.search_button.isEnabled() and dock.model.rowCount() == 6)
    newest = max(dock.records, key=lambda r: r.attributes["ACQ_TIME"])
    [layer] = dock.open_records([newest])
    assert isinstance(layer, QgsRasterLayer) and layer.isValid() and layer.width() == sample.PIXELS
    # Its external AI results: the ships and the false alarm (shown from 0.3).
    ai = p.api.ai_results
    ai.show()
    ai._layer_chosen(layer)
    assert wait_for(lambda: ai.image_id == newest.id and len(ai.rows) >= 2)
    assert {r.cls for _, r in ai.rows} >= {"ship"}
    # The observation store: its sample records and the classification with its activities.
    obs = p.api.observation_dock
    obs.show()
    obs.store_select.setCurrentIndex(obs.store_select.findText("モック観測記録"))
    assert wait_for(lambda: obs.book is not None and obs.book.loaded and obs.store_set.label == "モック観測記録")
    assert len(obs.book.records) == 2
    assert obs.book.activities_for("船舶 > 貨物船")[0] == ["入港", "出港", "停泊", "航行", "荷役"]


def test_qgis_arcgis_provider_reads_the_mock(plugin):
    _, _, base = plugin
    rest = f"{base}/arcgis/rest/services"
    catalog = QgsVectorLayer(f"crs='EPSG:4326' url='{rest}/Catalog/FeatureServer/0'", "catalog", "arcgisfeatureserver")
    assert catalog.isValid(), catalog.error().summary()
    assert catalog.featureCount() == 6
    feature = next(catalog.getFeatures())
    assert feature.geometry().area() > 0 and feature["IMG_ID"]
    points = QgsVectorLayer(f"crs='EPSG:4326' url='{rest}/Observations/FeatureServer/0'", "obs", "arcgisfeatureserver")
    assert points.isValid() and points.featureCount() == 2


def test_esri_layers_edited_in_qgis_get_who_and_when(plugin, monkeypatch):
    """A layer opened in QGIS's ArcGIS provider: features added and changed there get the tracking fields on saving."""
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY

    _, _, base = plugin
    monkeypatch.setenv("USERNAME", "qgis-user")
    url = f"{base}/arcgis/rest/services/Places/FeatureServer/0"
    layer = QgsVectorLayer(f"crs='EPSG:4326' url='{url}'", "places", "arcgisfeatureserver")
    assert layer.isValid()
    QgsProject.instance().addMapLayer(layer)
    layer.startEditing()
    feature = QgsFeature(layer.fields())
    feature["NAME"] = "新しい港"
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(139.8, 35.4)))
    assert layer.addFeature(feature)
    old = next(f for f in layer.getFeatures() if f["NAME"] == "東京港")
    layer.changeAttributeValue(old.id(), layer.fields().indexOf("NAME"), "東京港（改）")
    assert layer.commitChanges(), layer.commitErrors()

    rows = {f["attributes"]["NAME"]: f["attributes"] for f in EsriClient(transport).query_all(url, geometry=False)}
    added, changed, kept = rows["新しい港"], rows["東京港（改）"], rows["横須賀港"]
    assert added["CREATED_USER"] == added["UPDATED_USER"] == "qgis-user" and added["CREATED_DATE"] > 1.7e12
    assert changed["UPDATED_USER"] == "qgis-user" and changed["CREATED_USER"] is None
    assert kept["UPDATED_USER"] is None


def test_imaging_plan_from_the_mock_satellites(plugin):
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY

    p, _, _ = plugin
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "地点", "memory")
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(139.75, 35.3)))
    f.setAttributes(["横須賀"])
    layer.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(layer)
    dock = p.api.imaging_plan
    dock.show()
    assert wait_for(lambda: dock.loading_task is None)
    assert "モック衛星: 4 機" in dock.status.text() and "撮像モード" in dock.status.text(), dock.status.text()
    dock.run()
    assert wait_for(lambda: dock.plan_task is None and dock.loading_task is None, 30000), dock.status.text()
    names = {dock.plan["sats"][op.satellite].name for op in dock.shown}
    assert len(dock.shown) > 2 and len(names) >= 2, names
