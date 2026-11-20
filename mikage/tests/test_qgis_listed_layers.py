"""
観測記録's images without their files as layers of their footprints
(tsudo 2026-11-18: 画像無しで記録している画像、仮想レイヤーでフットプリントだけでも
追加したほうがわかりやすい。これを消すことで、観測記録の方からも外れる).
"""
from datetime import datetime, timezone

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer  # noqa: E402

from mikage.core.catalog.search import CatalogRecord, listed_image  # noqa: E402
from mikage.core.obs.model import ListedImage  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.listed_layers import GROUP, IMAGE_PROPERTY, listed_image_of  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.features.observations import Observations  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402

#: A footprint turned a little (not its box): the layer has the catalog's shape.
RING = [[139.62, 35.52], [139.78, 35.50], [139.80, 35.68], [139.64, 35.70], [139.62, 35.52]]


def ms(day: int, hour: int = 1) -> int:
    return int(datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture
def bare(tmp_path):
    """The plugin with no image open."""
    QgsProject.instance().clear()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    settings.set_recorder("田中")

    iface = FakeIface()
    p = small_plugin(iface, Observations)
    p.initGui()
    dock = p.api.observation_dock
    dock.show()
    yield p, iface, dock, tmp_path
    p.unload()
    QgsProject.instance().clear()
    QgsSettings().remove("mikage")


def _record(image_id: str, day: int, ring=None) -> CatalogRecord:
    geometry = {"rings": [ring]} if ring else {"xmin": 139.9, "ymin": 35.3, "xmax": 140.1, "ymax": 35.5}
    return CatalogRecord({"IMG_ID": image_id}, geometry, image_id, ms(day), None, None, "", None, None, None)


def _group():
    return QgsProject.instance().layerTreeRoot().findGroup(GROUP)


def _layers() -> list[QgsVectorLayer]:
    group = _group()
    return [node.layer() for node in group.findLayers()] if group is not None else []


def _names() -> list[str]:
    return [layer.name() for layer in _layers()]


def _width(layer) -> float:
    return layer.renderer().symbol().symbolLayer(0).strokeWidth()


def test_the_catalogs_images_without_files_are_layers_of_their_footprints(bare):
    p, iface, dock, _ = bare
    project = QgsProject.instance()
    images = [listed_image(_record("IMG1", 1, RING)), listed_image(_record("IMG2", 2))]
    assert images[0].rings == [RING] and images[0].box == pytest.approx([139.62, 35.50, 139.80, 35.70]) and images[1].rings is None
    dock.list_catalog_images(images)
    # One layer per image in the group at the top of the list, named as the image, not a change to the project.
    root = project.layerTreeRoot()
    assert root.children()[0] is _group() and _names() == ["IMG1", "IMG2"]
    assert not project.isDirty()
    one, two = _layers()
    assert listed_image_of(one) == "IMG1" and one.customProperty("skipMemoryLayersCheck") == 1 and one.readOnly()
    feature = next(one.getFeatures())
    assert feature["image_id"] == "IMG1" and feature["name"] == "IMG1" and feature["acquired"].toMSecsSinceEpoch() == ms(1)
    # The catalog's polygon (not its box) for the first, the envelope's box for the second.
    assert feature.geometry().equals(QgsGeometry.fromWkt("MultiPolygon(((" + ",".join(f"{x} {y}" for x, y in RING) + ")))"))
    assert next(two.getFeatures()).geometry().boundingBox().toString(2) == "139.90,35.30 : 140.10,35.50"
    # The image recorded on: its layer chosen, its outline wider.
    assert dock.book.current.id == "IMG1" and iface.activeLayer() is one
    assert _width(one) > _width(two)
    dock.step(1)
    assert iface.activeLayer() is two and _width(two) > _width(one)
    # Its layer chosen in the layer list: the image recorded on.
    iface.setActiveLayer(one)
    assert dock.book.current.id == "IMG1"
    # The listing kept in QgsSettings keeps the shape (a later session draws it again).
    assert "rings" in settings.SettingsPrefs().get("listed_images/ローカル")


def test_removing_a_footprints_layer_takes_its_image_off_the_list_and_keeps_its_records(bare):
    p, iface, dock, _ = bare
    project = QgsProject.instance()
    dock.list_catalog_images([listed_image(_record(f"IMG{d}", d, RING)) for d in (1, 2, 3)])
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.object_input.setText("あおば")
    dock.submit()
    book = dock.book
    assert [o.image_id for o in book.living()] == ["IMG1"]
    # Another image's layer removed: off the list; the image recorded on and its form stay.
    dock.map_clicked(139.71, 35.61, None)
    assert dock.form.isVisible()
    project.removeMapLayer(_layers()[1].id())
    APP.processEvents()
    assert [i.id for i in book.listed] == ["IMG1", "IMG3"] and [i.id for i in book.images] == ["IMG1", "IMG3"]
    assert book.current.id == "IMG1" and dock.form.isVisible() and _names() == ["IMG1", "IMG3"]
    assert dock.status.text() == "IMG2 を一覧から除外（記録は保持）"
    # The current image's layer removed: the next one recorded on, the form closed; the records stay.
    dock.close_form()
    project.removeMapLayer(_layers()[0].id())
    APP.processEvents()
    assert [i.id for i in book.listed] == ["IMG3"] and book.current.id == "IMG3" and iface.activeLayer() is _layers()[0]
    assert [o.image_id for o in book.living()] == ["IMG1"]
    assert "IMG2" not in settings.SettingsPrefs().get("listed_images/ローカル")
    # The group removed: every image of it off the list, nothing chosen.
    root = project.layerTreeRoot()
    root.removeChildNode(_group())
    APP.processEvents()
    assert book.listed == [] and book.current is None and _group() is None and not dock.work_area.isVisible()
    assert [o.image_id for o in book.living()] == ["IMG1"]


def test_the_layers_follow_the_list_typed_and_taken_off_in_the_dock(bare):
    p, iface, dock, _ = bare
    # ＋ without 範囲: a layer without a shape, said in its name.
    dock.list_button.setChecked(True)
    dock.list_id.setText("EXT-1")
    dock.list_name.setText("外部")
    dock._list_from_form()
    assert _names() == ["外部（範囲なし）"] and next(_layers()[0].getFeatures()).geometry().isNull()
    # The time typed: the layer has it.
    from qgis.PyQt.QtCore import QDateTime

    dock.time_input.setDateTime(QDateTime.fromMSecsSinceEpoch(ms(2, 12)))
    dock._set_image_time()
    assert next(_layers()[0].getFeatures())["acquired"].toMSecsSinceEpoch() == ms(2, 12)
    # 「一覧から除外」: the layer goes (and the group with its last layer).
    dock._unlist()
    assert _group() is None and dock.book.listed == []


def test_only_this_image_hides_the_other_footprints(bare):
    p, iface, dock, _ = bare
    dock.list_catalog_images([listed_image(_record(f"IMG{d}", d, RING)) for d in (1, 2)])
    root = QgsProject.instance().layerTreeRoot()
    one, two = _layers()
    dock.only.setChecked(True)
    assert [root.findLayer(x.id()).itemVisibilityChecked() for x in (one, two)] == [True, False]
    dock.step(1)
    assert [root.findLayer(x.id()).itemVisibilityChecked() for x in (one, two)] == [False, True]
    dock.only.setChecked(False)
    assert all(root.findLayer(x.id()).itemVisibilityChecked() for x in (one, two))


def test_the_images_stay_listed_when_the_project_is_cleared_or_the_plugin_reloaded(bare, tmp_path):
    p, iface, dock, _ = bare
    project = QgsProject.instance()
    dock.list_catalog_images([listed_image(_record(f"IMG{d}", d, RING)) for d in (1, 2)])
    # A new project: every layer goes, the images stay listed and their layers come back.
    project.clear()
    assert [i.id for i in dock.book.listed] == ["IMG1", "IMG2"]
    assert wait_for(lambda: _names() == ["IMG1", "IMG2"])
    assert not project.isDirty()
    # A project saved with them read again: the layers read back (empty) are replaced, not doubled.
    path = str(tmp_path / "with.qgz")
    assert project.write(path)
    project.read(path)
    assert wait_for(lambda: _names() == ["IMG1", "IMG2"])
    assert len([x for x in project.mapLayers().values() if x.customProperty(IMAGE_PROPERTY)]) == 2
    assert next(_layers()[0].getFeatures(), None) is not None
    assert [i.id for i in dock.book.listed] == ["IMG1", "IMG2"]
    # Every layer removed at once (as the tests do): the same.
    project.removeAllMapLayers()
    assert wait_for(lambda: _names() == ["IMG1", "IMG2"])
    # The plugin unloaded: the layers go, the list stays for the next time.
    p.unload()
    assert _group() is None

    again = small_plugin(iface, Observations)
    again.initGui()
    try:
        assert [i.id for i in again.api.observation_dock.book.listed] == ["IMG1", "IMG2"]
        assert _names() == ["IMG1", "IMG2"]
    finally:
        again.unload()
    p.initGui()  # the fixture unloads it


def test_each_store_has_its_own_images_without_files(bare, tmp_path):
    import json

    p, iface, dock, _ = bare
    dock.list_catalog_images([listed_image(_record("IMG1", 1, RING))])
    config = tmp_path / "config.json"
    url = "https://example.com/arcgis/rest/services/Obs/FeatureServer/0"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": url}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.reload()
    assert dock.store_set.label == "共有" and _group() is None
    dock.store_select.setCurrentIndex(dock.store_select.findText("ローカル"))
    assert _names() == ["IMG1"]


def test_rings_are_read_back_only_when_they_are_rings():
    from mikage.core.obs.model import parse_listed_images, rings_of

    assert rings_of([[[1, 2], [3, 4], [5, 6, 7]]]) == [[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]]
    for bad in (None, [], [[[1, 2], [3, 4]]], [[[1, 2], [3, "x"], [5, 6]]], [[[1, 2], [3, float("nan")], [5, 6]]], [[1, 2, 3]]):
        assert rings_of(bad) is None
    image = ListedImage("A", "A", 1, [0, 0, 1, 1], [[[0, 0], [1, 0], [1, 1], [0, 0]]])
    import json

    assert parse_listed_images(json.dumps([image.to_json()])) == [image]
    # Kept as before when it has none (no "rings" key).
    assert "rings" not in ListedImage("B", "B").to_json()
    from mikage.core.catalog.search import MAX_FOOTPRINT_POINTS, footprint_rings

    many = [[[i * 1e-4, 0] for i in range(MAX_FOOTPRINT_POINTS)] + [[0, 1]]]
    assert footprint_rings({"rings": many}) is None and footprint_rings({"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}) is None
