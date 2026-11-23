"""
「分類体系の取り込み」 in QGIS: from the menu and the editor's 「取り込み ▾」, a CSV, an Excel sheet or a layer, the columns'
uses, the dry run's tree, the import (a backup kept first) into the dock's store, and 「書き出し ▾」's table read back.
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsProject, QgsVectorLayer  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.api import kind_of  # noqa: E402
from mikage.core.obs.api import taxonomy_import as ti  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io import taxonomy_import_io as tio  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_alerts import plugin  # noqa: E402,F401
from mikage.tests.test_taxonomy_import import COLUMNS, EXISTING, TABLE  # noqa: E402


@pytest.fixture(autouse=True)
def empty_project():
    yield
    QgsProject.instance().removeAllMapLayers()


@pytest.fixture
def backups(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    monkeypatch.setattr(tio, "backup_folder", lambda: str(folder))
    return folder


def sample_csv(path, rows=TABLE, columns=COLUMNS):
    lines = [",".join(columns), *(",".join(r) for r in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return str(path)


def kinds(book):
    return [kind_of(t.path) for t in book.kept_taxonomy()]


def opened(p, dock):
    action = next(a for a in p.actions if a.objectName() == "SatObsTaxonomyImport")
    action.trigger()
    dialog = dock._taxonomy_import
    assert dialog.isVisible()
    return dialog


def test_a_csv_is_tried_then_taken_in_with_a_backup_kept(plugin, backups):  # noqa: F811
    p, _iface, dock, tmp_path = plugin
    book = dock.book
    assert book.set_taxonomy(list(EXISTING))
    dialog = opened(p, dock)
    assert "ローカル" in dialog.store_label.text() and not dialog.try_button.isEnabled()
    assert dialog.choose_file(sample_csv(tmp_path / "分類一覧.csv"))
    assert dialog.source_note.text().startswith("12 行・6 列") and not dialog.sheet.isVisible()
    assert [c.currentText() for c in dialog.combos] == ["階層 1", "階層 2", "階層 3", "既知の個体", "諸元: 別名", "諸元: 全長"]
    assert "大分類 › 種別 › 級・型式" in dialog.level_names.text() and dialog.level_names.isEnabled()
    # Two columns as one level: told, nothing tried.
    dialog.combos[1].setCurrentIndex(0)
    assert "重ならない" in dialog.uses_note.text() and not dialog.try_button.isEnabled()
    dialog.combos[1].setCurrentIndex(1)
    assert dialog.uses_note.text() == "" and dialog.try_button.isEnabled()

    assert dialog.try_run()
    plan = dialog.plan
    assert plan is not None and dialog.import_button.isEnabled() and dialog.summary.text() == plan.summary()
    assert dialog.errors.isVisible() is False or dialog.errors.rowCount() == 1
    assert dialog.errors.rowCount() == 1 and dialog.errors.item(0, 0).text() == "行 11"
    top = [dialog.tree.topLevelItem(i).text(0) for i in range(dialog.tree.topLevelItemCount())]
    assert top[:3] == ["船舶", "航空機", "車両"]
    ship = dialog.tree.topLevelItem(0)
    support = next(ship.child(i) for i in range(ship.childCount()) if ship.child(i).text(0) == "支援船")
    supply = next(support.child(i) for i in range(support.childCount()) if support.child(i).text(0) == "補給艦")
    assert supply.text(1) == "＋ 新規" and "全長 221 m" in supply.text(2)
    cargo = next(ship.child(i) for i in range(ship.childCount()) if ship.child(i).text(0) == "貨物船")
    container = next(cargo.child(i) for i in range(cargo.childCount()) if cargo.child(i).text(0) == "コンテナ船")
    assert "とみなす" in container.text(2)
    # Only what changes (and the classes above it).
    dialog.changed_only.setChecked(True)
    shown = [dialog.tree.topLevelItem(i).text(0) for i in range(dialog.tree.topLevelItemCount())]
    assert "その他" not in shown and "船舶" in shown
    # Nothing saved by the dry run.
    assert book.kept_taxonomy() == EXISTING

    assert dialog.run_import(confirm=False)
    assert "船舶 > 支援船 > 補給艦" in kinds(book) and "その他" in kinds(book)
    assert dialog.summary.text().startswith("取り込み済み: ") and not dialog.import_button.isEnabled()
    assert "表から取り込み: " in dialog.status.text() and "取り込む前の分類体系の控え" in dialog.status.text()
    [backup] = list(backups.glob("*.json"))
    assert json.loads(backup.read_text(encoding="utf-8"))[0]["name"] == "船舶"
    # The editor shows it.
    editor = p.docks["SatObsTaxonomy"]
    editor.show()
    editor.render()
    assert editor.tree.topLevelItemCount() == len([t for t in book.taxonomy() if len(t.path) == 1])

    # Again: nothing to change.
    assert dialog.try_run()
    assert not dialog.import_button.isEnabled() and "変更なし" in dialog.status.text()


def test_replacing_removes_classes_not_in_the_table_after_asking(plugin, backups, monkeypatch):  # noqa: F811
    p, _iface, dock, tmp_path = plugin
    book = dock.book
    assert book.set_taxonomy(list(EXISTING))
    dialog = opened(p, dock)
    dialog.choose_file(sample_csv(tmp_path / "t.csv"))
    dialog.replace_mode.setChecked(True)
    assert dialog.try_run()
    removed = [c.kind for c in dialog.plan.removed()]
    assert "その他" in removed and "船舶 > 貨物船 > コンテナ船" not in removed
    asked = []
    monkeypatch.setattr("mikage.ui.taxonomy_import_dialog.ask_no", lambda _w, _t, text: asked.append(text) or False)
    assert not dialog.run_import()
    assert asked and "表にない分類" in asked[0] and "その他" in kinds(book)
    monkeypatch.setattr("mikage.ui.taxonomy_import_dialog.ask_no", lambda _w, _t, _text: True)
    assert dialog.run_import()
    assert "その他" not in kinds(book) and "建物 > 格納庫" in kinds(book)
    # A change of mode throws the dry run away.
    dialog.add_mode.setChecked(True)
    assert dialog.plan is None and not dialog.import_button.isEnabled()


def test_an_excel_sheet_and_a_layer_are_read_too(plugin, backups):  # noqa: F811
    from osgeo import gdal, ogr

    p, _iface, dock, tmp_path = plugin
    gdal.UseExceptions()
    path = str(tmp_path / "分類一覧.xlsx")
    ds = ogr.GetDriverByName("XLSX").CreateDataSource(path)
    memo = ds.CreateLayer("メモ", geom_type=ogr.wkbNone)
    memo.CreateField(ogr.FieldDefn("説明", ogr.OFTString))
    sheet = ds.CreateLayer("分類一覧", geom_type=ogr.wkbNone)
    for c in COLUMNS:
        sheet.CreateField(ogr.FieldDefn(c, ogr.OFTString))
    for row in TABLE:
        f = ogr.Feature(sheet.GetLayerDefn())
        for i, v in enumerate(row):
            if v:
                f.SetField(i, v)
        sheet.CreateFeature(f)
    ds = None
    dialog = opened(p, dock)
    assert dialog.choose_file(path)
    assert dialog.sheet.isVisible() and [dialog.sheet.itemText(i) for i in range(dialog.sheet.count())] == ["メモ", "分類一覧"]
    dialog.sheet.setCurrentIndex(1)
    assert dialog.source_note.text().startswith("12 行・6 列")
    assert dialog.try_run() and dialog.plan.errors[0][0] == 11

    layer = QgsVectorLayer("None?field=大分類:string&field=種別:string", "分類の表", "memory")
    features = []
    for a, b in [("船舶", "漁船"), ("", "客船")]:
        f = QgsFeature(layer.fields())
        f.setAttributes([a, b])
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    dialog.set_layer(layer)
    assert dialog.source_note.text().startswith("2 件・2 列") and dialog.from_layer.isChecked()
    assert dialog.try_run() and dialog.run_import(confirm=False)
    assert {"船舶 > 漁船", "船舶 > 客船"} <= set(kinds(dock.book))


def test_the_editors_menus_open_the_import_and_write_the_table(plugin, backups, tmp_path):  # noqa: F811
    p, _iface, dock, _ = plugin
    book = dock.book
    assert book.set_taxonomy(list(EXISTING))
    editor = p.docks["SatObsTaxonomy"]
    imports = [a.text() for a in editor.import_button.menu().actions()]
    exports = [a.text() for a in editor.export_button.menu().actions()]
    assert imports == ["表から取り込み（CSV・Excel・レイヤー）…", "JSON から読み込み（今の分類体系と置き換え）…"]
    assert exports == ["表に書き出し（CSV。取り込みのひな形にも）…", "JSON に保存（設定ファイルに書ける形）…"]
    editor.import_button.menu().actions()[0].trigger()
    assert dock._taxonomy_import.isVisible()
    out = tmp_path / "分類.csv"
    assert dock.save_taxonomy_table_file(str(out)) == str(out)
    text = out.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("大分類,種別,")
    # Read back: the same classification.
    dialog = dock._taxonomy_import
    dialog.choose_file(str(out))
    assert dialog.try_run() and not dialog.plan.changed


def test_an_esri_store_tries_and_takes_in_tasks(plugin, backups, tmp_path):  # noqa: F811
    from mikage.tests.test_qgis_observations import FIELDS, TYPES, URL

    p, _iface, dock, _ = plugin
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: TYPES.get(n, "esriFieldTypeString") for n in FIELDS}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"observationStores": [{"label": "共有", "url": URL}]}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    assert dock.store_set.remote and wait_for(lambda: dock.book.loaded)
    dialog = opened(p, dock)
    dialog.choose_file(sample_csv(tmp_path / "t.csv"))
    assert dialog.try_run()
    assert wait_for(lambda: dialog.plan is not None)
    assert dialog.run_import(confirm=False)
    assert wait_for(lambda: dialog.summary.text().startswith("取り込み済み"))
    assert "船舶 > 支援船 > 補給艦" in kinds(dock.book)
    assert ti.backup_name().endswith(".json") and list(backups.glob("*.json"))
