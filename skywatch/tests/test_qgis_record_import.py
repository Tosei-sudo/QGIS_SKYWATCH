"""「観測記録の一括取込み」 in QGIS: from the plugin menu, a layer or a CSV, the dry run, the import into the dock's store."""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

from skywatch.tests.test_qgis_alerts import plugin  # noqa: E402,F401


@pytest.fixture(autouse=True)
def empty_project():
    yield
    QgsProject.instance().removeAllMapLayers()


def other_section_layer():
    """Another section's points (Web Mercator), with its own column names."""
    layer = QgsVectorLayer("Point?crs=EPSG:3857&field=IMAGE_ID:string&field=class:string&field=name:string&field=memo:string",
                           "他部署の表", "memory")
    to = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), layer.crs(), QgsProject.instance())
    features = []
    for image, cls, name, x, y in [("d9", "船舶", "丸A", 139.70, 35.30), ("d9", "船舶", "丸B", 139.75, 35.32), ("d9", "", "", 139.0, 35.0),
                                   ("", "船舶", "丸C", 139.8, 35.3)]:
        f = QgsFeature(layer.fields())
        f.setAttributes([image, cls, name, "他部署"])
        g = QgsGeometry.fromWkt(f"POINT({x} {y})")
        g.transform(to)
        f.setGeometry(g)
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_a_layer_and_a_csv_go_into_the_chosen_store(plugin, tmp_path):  # noqa: F811
    p, _iface, dock, _ = plugin
    book = dock.book
    assert book.records == []
    action = next(a for a in p.actions if a.objectName() == "SatObsObservationImport")
    action.trigger()
    dialog = dock._import
    assert dialog.isVisible() and "ローカル" in dialog.store_label.text()
    layer = other_section_layer()
    dialog.set_layer(layer)
    assert dialog.source_note.text().startswith("4 件")
    assert dialog.mapping() == {"image_id": "IMAGE_ID", "object": "name", "kind": "class", "note": "memo"}
    planned = dialog.try_run()
    assert [x.action for x in planned] == ["new", "new", "new", "error"]
    assert dialog.summary.text() == "新規 3 件、エラー 1 件" and dialog.import_button.isEnabled()
    assert dialog.table.rowCount() == 4 and dialog.table.item(3, 1).text() == "エラー"
    dialog.errors_only.setChecked(True)
    assert dialog.table.rowCount() == 1
    # Changing a column throws the dry run away.
    dialog.set_mapping("note", "")
    assert not dialog.import_button.isEnabled()
    dialog.set_mapping("note", "memo")
    dialog.try_run()
    assert dialog.run_import(confirm=False)
    assert len(book.records) == 3 and "新規 3 件・更新 0 件を保存" in dialog.status.text()
    a = next(o for o in book.records if o.object_name == "丸A")
    assert (a.image_id, a.note, round(a.lon, 6), round(a.lat, 6)) == ("d9", "他部署", 139.7, 35.3)
    # Again: the same rows are there now.
    assert [x.action for x in dialog.try_run()] == ["duplicate", "duplicate", "duplicate", "error"]
    assert not dialog.import_button.isEnabled()

    # The plugin's own CSV back in, with the records' ids: changes where asked.
    path = tmp_path / "back.csv"
    path.write_text("観測ID,画像ID,活動,経度,緯度\n" + f"{a.id},d9,航行,,\n", encoding="utf-8-sig")
    assert dialog.choose_csv(str(path)) and dialog.source_note.text().startswith("1 行")
    assert [x.action for x in dialog.try_run()] == ["existing"]
    dialog.errors_only.setChecked(False)
    dialog.update_existing.setChecked(True)
    [x] = dialog.try_run()
    assert x.action == "update" and dialog.table.item(0, 3).text() == "活動:  → 航行"
    assert dialog.run_import(confirm=False)
    assert next(o for o in book.records if o.id == a.id).activity == "航行"
