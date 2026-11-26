"""
「前の画像を閉じる」 (qgis_io/catalog_io.close_layers): the images closed
take their AI results with them (the 「AI結果」 dock's layers of those images,
the plugin's detection of those images only) and the groups they leave empty
(tsudo 2026-11-26 「AI結果レイヤーがある場合は、それも消してほしいのと、
グループレイヤーも消してほしい」); what else is there stays.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer  # noqa: E402

from mikage.qgis_io.ai_results_io import RESULTS_OF  # noqa: E402
from mikage.qgis_io.catalog_io import RECORD_GROUP, close_layers  # noqa: E402
from mikage.qgis_io.obs_images import IMAGE_ID  # noqa: E402
from mikage.qgis_io.pinned_layers import PINNED  # noqa: E402
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402


@pytest.fixture
def project():
    project = QgsProject.instance()
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()
    yield project
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()


def image(tmp_path, name: str, image_id: str = "") -> QgsRasterLayer:
    """An image as the catalog opens it (its id on the layer); without an id, one opened by hand (its file name its id)."""
    layer = QgsRasterLayer(geotiff(tmp_path / f"{name}.tif", "2026-10-01T01:00:00Z"), name)
    assert layer.isValid()
    if image_id:
        layer.setCustomProperty(IMAGE_ID, image_id)
    return layer


def external(image_id: str) -> QgsVectorLayer:
    """A layer the 「AI結果」 dock draws an image's external results in."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=class:string", f"外部AI結果 船舶検出（{image_id}）", "memory")
    layer.setCustomProperty(RESULTS_OF, image_id)
    return layer


def detection(name: str, *image_ids: str, fields: str = "class:string&field=score:double") -> QgsVectorLayer:
    """A layer as 「物体検出（AI）」 makes it: a feature per image id."""
    layer = QgsVectorLayer(f"Polygon?crs=EPSG:4326&field={fields}&field=image_id:string&field=model:string&field=detected_at:datetime",
                           name, "memory")
    features = []
    for i, image_id in enumerate(image_ids):
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(139.7 + i * 0.01, 35.6), QgsPointXY(139.7 + i * 0.01, 35.61),
                                                  QgsPointXY(139.71 + i * 0.01, 35.61), QgsPointXY(139.7 + i * 0.01, 35.6)]]))
        f["image_id"] = image_id
        features.append(f)
    layer.dataProvider().addFeatures(features)
    return layer


def add(project, layer, group=None):
    project.addMapLayer(layer, False)
    (group or project.layerTreeRoot()).addLayer(layer)
    return layer


def test_the_images_ai_results_close_with_them(project, tmp_path):
    root = project.layerTreeRoot()
    # A record of two files in its group, the external results QGIS put in the group (above the file chosen), detection at the top.
    group = root.addGroup("A")
    group.setCustomProperty(RECORD_GROUP, "A")
    a1, a2 = add(project, image(tmp_path, "a1", "A"), group), add(project, image(tmp_path, "a2", "A"), group)
    results_a = add(project, external("A"), group)
    detected_a = add(project, detection("物体検出（船舶）", "A", "A"))
    # Kept: results of A and another image together, A's detection being edited, AI 抽出 (no score: not the dock's), the next image's.
    both = add(project, detection("物体検出（2 枚）", "A", "B"))
    edited = add(project, detection("物体検出（修正中）", "A"))
    assert edited.startEditing()
    assert edited.deleteFeature(next(edited.getFeatures()).id()) and edited.isModified()
    extracted = add(project, detection("AI 抽出", "A", fields="id:integer&field=class:string"))
    b = add(project, image(tmp_path, "b", "B"))
    results_b = add(project, external("B"))
    # An image opened by hand: its detection names it by its file name.
    hand = add(project, image(tmp_path, "hand"))
    detected_hand = add(project, detection("物体検出（手）", "hand.tif"))

    going = [layer.id() for layer in (a1, a2, results_a, detected_a, hand, detected_hand)]
    a_ids = [a1.id()]
    assert close_layers([a1.id(), a2.id(), hand.id()]) == 3
    left = set(project.mapLayers())
    assert not set(going) & left
    assert {both.id(), extracted.id(), b.id(), results_b.id()} <= left
    edited.rollBack()
    # Edited and not saved: stays (its changes would be lost).
    assert edited.id() in left
    # The record's group went with its last layers.
    assert root.findGroup("A") is None
    # Nothing to close: nothing taken.
    assert close_layers(a_ids) == 0
    assert {both.id(), b.id(), results_b.id()} <= set(project.mapLayers())


def test_results_stay_while_the_image_is_still_open(project, tmp_path):
    """Another file of the same image open (another record, by hand): its results are still the image's."""
    first, second = add(project, image(tmp_path, "f1", "F")), add(project, image(tmp_path, "f2", "F"))
    results = add(project, external("F"))
    detected = add(project, detection("物体検出（F）", "F"))
    ids = {results.id(), detected.id()}
    assert close_layers([first.id()]) == 1
    assert {second.id()} | ids <= set(project.mapLayers())
    assert close_layers([second.id()]) == 1
    assert not ids & set(project.mapLayers())


def test_the_groups_left_empty_go_and_the_others_stay(project, tmp_path):
    root = project.layerTreeRoot()
    # The image put in a group of a group by hand, with its results: both groups go once empty.
    work = root.addGroup("作業")
    compare = work.addGroup("比較")
    c = add(project, image(tmp_path, "c", "C"), compare)
    add(project, external("C"), compare)
    # A group with something else in it stays, with it.
    keep = root.addGroup("残す")
    d = add(project, image(tmp_path, "d", "D"), keep)
    note = add(project, QgsVectorLayer("Point?crs=EPSG:4326", "メモ", "memory"), keep)
    # A pinned group (「一番上に固定」) stays empty; a group empty before stays.
    pinned = root.insertGroup(0, "固定")
    pinned.setCustomProperty(PINNED, True)
    e = add(project, image(tmp_path, "e", "E"), pinned)
    root.addGroup("空")
    # A group whose other group is left: the outer one stays (only the emptied inner one goes).
    outer = root.addGroup("外")
    inner = outer.addGroup("内")
    outer.addGroup("ほか")
    g = add(project, image(tmp_path, "g", "G"), inner)

    assert close_layers([c.id(), d.id(), e.id(), g.id()]) == 4
    assert root.findGroup("作業") is None and root.findGroup("比較") is None
    assert root.findGroup("残す") is not None and [n.layerId() for n in root.findGroup("残す").findLayers()] == [note.id()]
    assert root.findGroup("固定") is not None and root.findGroup("空") is not None
    assert root.findGroup("内") is None and root.findGroup("外") is not None and root.findGroup("ほか") is not None
    assert set(project.mapLayers()) == {note.id()}
