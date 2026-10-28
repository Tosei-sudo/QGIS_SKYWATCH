"""The settings file's `layers` added when QGIS starts with an empty project (and from the menu)."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402

from satellite_observation.qgis_io import settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from satellite_observation.tests.test_qgis_observations import geotiff  # noqa: E402


@pytest.fixture
def config(tmp_path):
    QgsSettings().remove("satellite_observation")
    QgsProject.instance().clear()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "stations.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "A"}, "geometry": {"type": "Point", "coordinates": [139.7, 35.6]}}]}), encoding="utf-8")
    geotiff(tmp_path / "ortho.tif")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"layers": [
        {"type": "cog", "url": "./ortho.tif"},
        {"type": "file", "url": "./data/stations.geojson"},
        {"type": "file", "url": "./data/missing.geojson"},
    ]}), encoding="utf-8")
    settings.set_config_source(str(path))
    yield path
    QgsProject.instance().clear()
    QgsSettings().remove("satellite_observation")


def start():
    from satellite_observation import classFactory

    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    return plugin, iface


def names():
    return [l.layer().name() for l in QgsProject.instance().layerTreeRoot().findLayers()]


def test_added_to_an_empty_project_the_first_at_the_bottom(config):
    plugin, iface = start()
    assert wait_for(lambda: len(QgsProject.instance().mapLayers()) == 2)
    # The layer tree lists the top first: the first entry ends at the bottom.
    assert names() == ["stations.geojson", "ortho.tif"]
    # The entry that cannot be opened says why; the others are added.
    seen = []
    plugin.startup_layers._finished = lambda layers, problems: seen.append(problems)
    plugin.startup_layers.add()
    assert wait_for(lambda: seen)
    assert any(p.startswith("missing.geojson（file）を開けず") for p in seen[0])
    plugin.unload()


def test_added_to_each_new_project(config):
    plugin, iface = start()
    assert wait_for(lambda: len(QgsProject.instance().mapLayers()) == 2)
    # 「新規プロジェクト」: QGIS clears the project, then says so.
    QgsProject.instance().clear()
    iface.newProjectCreated.emit()
    assert wait_for(lambda: len(QgsProject.instance().mapLayers()) == 2)
    assert names() == ["stations.geojson", "ortho.tif"]
    # Turned off: a new project stays empty.
    settings.set_startup_layers_on(False)
    QgsProject.instance().clear()
    iface.newProjectCreated.emit()
    wait_for(lambda: False, 500)
    assert QgsProject.instance().mapLayers() == {}
    plugin.unload()


def test_not_added_to_a_project_with_layers_nor_when_turned_off(config):
    QgsProject.instance().addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", "mine", "memory"))
    plugin, iface = start()
    wait_for(lambda: False, 500)
    assert names() == ["mine"]
    # 「設定ファイルのレイヤーを追加」 adds them anyway.
    plugin.startup_layers.add()
    assert wait_for(lambda: len(QgsProject.instance().mapLayers()) == 3)
    assert names() == ["stations.geojson", "ortho.tif", "mine"]
    plugin.unload()
    QgsProject.instance().clear()
    settings.set_startup_layers_on(False)
    plugin, iface = start()
    wait_for(lambda: False, 500)
    assert QgsProject.instance().mapLayers() == {}
    plugin.unload()
