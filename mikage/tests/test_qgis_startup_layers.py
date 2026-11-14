"""The settings file's `layers` added when QGIS starts with an empty project (and from the menu)."""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402

from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402


@pytest.fixture
def config(tmp_path):
    QgsSettings().remove("mikage")
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
    QgsSettings().remove("mikage")


def start():
    from mikage import classFactory

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


def test_visible_false_is_added_unchecked(config):
    entries = json.loads(config.read_text(encoding="utf-8"))
    entries["layers"][1]["visible"] = False
    config.write_text(json.dumps(entries), encoding="utf-8")
    plugin, iface = start()
    assert wait_for(lambda: len(QgsProject.instance().mapLayers()) == 2)
    shown = {l.layer().name(): l.isVisible() for l in QgsProject.instance().layerTreeRoot().findLayers()}
    assert shown == {"stations.geojson": False, "ortho.tif": True}
    # 「設定ファイルのレイヤーを追加」 adds it unchecked too.
    plugin.startup_layers.add()
    assert wait_for(lambda: len(QgsProject.instance().mapLayers()) == 4)
    assert [(l.layer().name(), l.isVisible()) for l in QgsProject.instance().layerTreeRoot().findLayers()] == [
        ("stations.geojson", False), ("ortho.tif", True), ("stations.geojson", False), ("ortho.tif", True)]
    plugin.unload()


def test_added_on_its_own_leaves_the_project_unchanged(config):
    """QGIS does not ask 「現在のプロジェクトを保存しますか」 because of what the plugin added on its own."""
    from qgis.PyQt.QtCore import QTimer

    entries = json.loads(config.read_text(encoding="utf-8"))
    entries.update({"baseMaps": [{"id": "t", "label": "T", "url": "./tiles/{z}/{x}/{y}.png"}], "defaultBaseMap": "t"})
    config.write_text(json.dumps(entries), encoding="utf-8")
    project = QgsProject.instance()

    # As the QGIS window does: changed at once when the layer panel gets a layer (QgisApp::markDirty), and again a
    # moment later when the map bridge sets the project's CRS from the first layer.
    def mark(*_):
        project.setDirty(True)
        QTimer.singleShot(0, lambda: project.setDirty(True))

    project.layerTreeRoot().addedChildren.connect(mark)
    try:
        plugin, iface = start()
        assert wait_for(lambda: len(project.mapLayers()) == 3)
        wait_for(lambda: False, 300)
        assert not project.isDirty()
        # In a new project too.
        project.clear()
        iface.newProjectCreated.emit()
        assert wait_for(lambda: len(project.mapLayers()) == 3)
        wait_for(lambda: False, 300)
        assert not project.isDirty()
        # 「設定ファイルのレイヤーを追加」 is the person's change: QGIS asks to save it.
        plugin.startup_layers.add()
        assert wait_for(lambda: len(project.mapLayers()) == 5)
        wait_for(lambda: False, 300)
        assert project.isDirty()
        plugin.unload()
    finally:
        project.layerTreeRoot().addedChildren.disconnect(mark)


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


def test_added_beside_the_plugins_base_map(config):
    from mikage.qgis_io.basemap_layer import PROPERTY, put_base_map

    # 「背景地図」's defaultBaseMap may be shown before the layers are read.
    base = QgsVectorLayer("Point?crs=EPSG:4326", "base", "memory")
    base.setCustomProperty(PROPERTY, "osm")
    put_base_map(base)
    plugin, iface = start()
    assert wait_for(lambda: len(QgsProject.instance().mapLayers()) == 3)
    assert names() == ["stations.geojson", "ortho.tif", "base"]
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
