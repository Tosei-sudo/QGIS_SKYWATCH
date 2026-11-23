"""
開いた画像の履歴・画像 ID で開く (tsudo 2026-11-18: 画像カタログから開くのを、もっと
もっと簡単に): an image opened once opens again with the files of last time
after one look (the catalog not asked, the folder not looked through, the
files not chosen again); an id typed is looked up in the catalogs; the
confirmation dialog itself.
"""
import os

import pytest

pytest.importorskip("qgis.core")

from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402

from mikage.core.catalog.search import record_of  # noqa: E402
from mikage.qgis_io.catalog_io import opened_images  # noqa: E402
from mikage.qgis_io.obs_images import IMAGE_ID  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_catalog import plugin, searched  # noqa: E402,F401
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402
from mikage.ui import open_again_dialog  # noqa: E402


def _queries(fake):
    return [form for url, form in fake.asked if url.endswith("/query") and "returnCountOnly" not in form]


def test_an_image_opened_once_opens_again_with_its_files_after_one_look(plugin, monkeypatch):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    searched(dock)
    scene = folder / "2026" / "Scene_D"
    for rel in ("PAN/a_P.tif", "PAN/b_P.tif", "PAN/c_P.tif"):
        (scene / rel).parent.mkdir(parents=True, exist_ok=True)
        geotiff(scene / rel)
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    record = record_of({"attributes": {"IMG_ID": "D", "SENSOR": "S1", "ACQ": 1_700_000_000_000, "PATH": "\\\\nas\\images\\2026\\scene_d"}},
                       dock._shown_catalog.fields)
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: files[1:])
    layers = dock.open_records([record])
    assert [l.name() for l in layers] == ["PAN/b_P.tif", "PAN/c_P.tif"]
    # Kept: where, the files found and those opened, the catalog's record.
    e = opened_images().find("D")
    assert e.catalog_label == "衛星画像" and os.path.normcase(e.place) == os.path.normcase(str(scene))
    assert e.files == ["PAN/a_P.tif", "PAN/b_P.tif", "PAN/c_P.tif"] and e.opened == ["PAN/b_P.tif", "PAN/c_P.tif"]
    assert e.acquired == 1_700_000_000_000 and e.feature["attributes"]["IMG_ID"] == "D"
    assert "D" in dock._opened_model.stringList()
    QgsProject.instance().removeAllMapLayers()
    # Opened again by its id: one look (the files of last time ticked), no catalog, no folder, no choice.
    monkeypatch.setattr(dock, "choose_images", lambda *a: pytest.fail("not chosen again"))
    monkeypatch.setattr(dock, "choose_folder", lambda *a: pytest.fail("no folder asked"))
    asked_before = len(_queries(fake))
    shown = []
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda parent, entry: shown.append(entry) or ["PAN/a_P.tif"])
    opened = []
    dock.open_id.setText("D")
    dock.open_image_id(dock.open_id.text(), then=opened.append)
    assert [x.image_id for x in shown] == ["D"] and len(_queries(fake)) == asked_before
    [[layer]] = opened
    assert layer.name() == "D" and layer.customProperty(IMAGE_ID) == "D" and layer.source().endswith("a_P.tif")
    assert layer in QgsProject.instance().mapLayers().values()
    assert "前回のファイルで表示" in dock.status.text()
    # The choice is kept for the next time; 「最近開いた画像」 lists it.
    assert opened_images().find("D").opened == ["PAN/a_P.tif"]
    dock._fill_recent()
    [action] = dock.recent_menu.actions()
    assert action.text().startswith("D ") and action.toolTip() == e.place
    # Not opened: やめる.
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda parent, entry: None)
    opened.clear()
    dock.open_image_id("D", then=opened.append)
    assert opened == []


def test_an_id_not_opened_before_is_looked_up_in_the_catalog(plugin, monkeypatch):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    dock.show()
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda *a: pytest.fail("not opened before"))
    opened, missing = [], []
    dock.open_image_id("A  B", then=opened.append, missing=lambda: missing.append(1))
    assert wait_for(lambda: opened)
    [[layer]] = opened
    assert layer.customProperty(IMAGE_ID) == "A" and layer.source().endswith("A.TIF")
    assert _queries(fake)[-1]["where"] == "IMG_ID IN ('A')"
    assert opened_images().find("A") is not None
    # An id no catalog has.
    dock.open_image_id("Z'9", then=opened.append, missing=lambda: missing.append("Z"))
    assert wait_for(lambda: missing == ["Z"])
    assert _queries(fake)[-1]["where"] == "IMG_ID IN ('Z''9')"
    assert dock.status.text() == "画像カタログに画像 ID「Z'9」の画像なし"
    # Opened before, but the file has gone since: looked up in the catalog again.
    (folder / "2026" / "A.TIF").rename(folder / "2026" / "moved.tif")
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda parent, entry: [])
    asked = len(_queries(fake))
    dock.open_image_id("A", then=opened.append)
    assert wait_for(lambda: len(_queries(fake)) > asked)
    # 「カタログで探し直す」.
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda parent, entry: open_again_dialog.FROM_CATALOG)
    asked = len(_queries(fake))
    dock.open_image_id("A", then=opened.append)
    assert wait_for(lambda: len(_queries(fake)) > asked)


def test_images_open_at_the_top_of_the_layer_tree_even_with_a_group_chosen(plugin, monkeypatch):
    """
    tsudo 2026-11-23: an image opened from 観測記録 while a group was chosen in
    the layer list went into that group. Now an image opens at the top level
    (QGIS puts a layer added the usual way where the layer list's choice is).
    """
    from qgis.core import QgsLayerTreeRegistryBridge, QgsVectorLayer

    p, iface, folder, fake, _ = plugin
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    mine = QgsVectorLayer("Point?crs=EPSG:4326", "自分のレイヤー", "memory")
    project.addMapLayer(mine, False)
    group = root.insertGroup(0, "作業")
    group.addLayer(mine)
    # The group chosen in the layer list (QGIS sets where new layers go so).
    bridge = project.layerTreeRegistryBridge()
    bridge.setLayerInsertionPoint(QgsLayerTreeRegistryBridge.InsertionPoint(group, 0))
    try:
        _open_with_a_group_chosen(p, folder, monkeypatch, root, group)
    finally:
        # (the project lives on to the next tests: a group gone there would be where their layers go)
        bridge.setLayerInsertionPoint(QgsLayerTreeRegistryBridge.InsertionPoint(root, 0))
        root.removeChildNode(group)


def _open_with_a_group_chosen(p, folder, monkeypatch, root, group):
    from qgis.core import QgsVectorLayer

    project = QgsProject.instance()
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda *a: pytest.fail("not opened before"))
    monkeypatch.setattr(p.api.image_catalog, "choose_folder", lambda mapping: str(folder))
    obs = p.api.observation_dock
    went = []
    # From 観測記録: a record's image not open is opened from the catalog by its id.
    obs.open_image("A", "A", then=lambda: went.append(1))
    assert wait_for(lambda: went)
    [layer] = [l for l in project.mapLayers().values() if l.customProperty(IMAGE_ID) == "A"]
    node = root.findLayer(layer.id())
    assert node.parent() is root and root.children().index(node) == 0
    assert [c.name() for c in group.children()] == ["自分のレイヤー"]
    # A folder record's images: their group at the top level too.
    dock = p.api.image_catalog
    searched(dock)
    scene = folder / "2026" / "Scene_D"
    for rel in ("PAN/a_P.tif", "PAN/b_P.tif"):
        (scene / rel).parent.mkdir(parents=True, exist_ok=True)
        geotiff(scene / rel)
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: files)
    record = record_of({"attributes": {"IMG_ID": "D", "SENSOR": "S1", "PATH": "\\\\nas\\images\\2026\\scene_d"}}, dock._shown_catalog.fields)
    assert len(dock.open_records([record])) == 2
    d = root.findGroup("D")
    assert d.parent() is root and root.children().index(d) == 0
    assert [c.name() for c in group.children()] == ["自分のレイヤー"]
    # Added the usual way, a layer still goes where the layer list's choice is.
    other = QgsVectorLayer("Point?crs=EPSG:4326", "ほかのレイヤー", "memory")
    project.addMapLayer(other)
    assert root.findLayer(other.id()).parent() is group


def test_the_image_opened_is_chosen_and_the_obs_dock_goes_to_it(plugin, monkeypatch):
    """
    Found retaking the help's pictures (2026-11-23): an image put at the top of
    the layer tree by hand is not chosen in the layer list the way QGIS chooses
    a layer it adds itself, so 観測記録 stayed on the image open before.
    """
    import qgis.utils

    p, iface, folder, fake, _ = plugin
    monkeypatch.setattr(qgis.utils, "iface", iface)
    dock, obs = p.api.image_catalog, p.api.observation_dock
    searched(dock)
    monkeypatch.setattr(dock, "choose_folder", lambda mapping: str(folder))
    monkeypatch.setattr(dock, "choose_images", lambda r, files, more: files)
    # One image, then another: each chosen, and 観測記録 goes to it.
    for image_id, scene in (("D", "Scene_D"), ("E", "Scene_E")):
        (folder / "2026" / scene / "PAN").mkdir(parents=True, exist_ok=True)
        geotiff(folder / "2026" / scene / "PAN" / "a_P.tif")
        record = record_of({"attributes": {"IMG_ID": image_id, "SENSOR": "S1", "PATH": f"\\\\nas\\images\\2026\\{scene.lower()}"}},
                           dock._shown_catalog.fields)
        [layer] = dock.open_records([record])
        assert iface.activeLayer() is layer
        assert wait_for(lambda: obs.book is not None and obs.book.current is not None and layer.id() in obs.book.current.layer_ids)


def test_the_images_of_ids_are_listed_in_the_catalog(plugin):
    p, iface, folder, fake, _ = plugin
    dock = p.api.image_catalog
    dock.show_image_ids(["C", "A", "Q"], "自分の記録の画像")
    assert wait_for(lambda: dock.status.text().startswith("自分の記録の画像"))
    assert sorted(r.id for r in dock.records) == ["A", "C"]
    assert dock.status.text() == "自分の記録の画像 2 件（1 件はカタログになし）"


def test_the_dialog_ticks_the_files_of_last_time_and_says_what_is_gone(tmp_path):
    from mikage.core.catalog.opened import OpenedImage

    (tmp_path / "PAN").mkdir()
    geotiff(tmp_path / "PAN" / "a.tif")
    e = OpenedImage("D", "u", "衛星画像", str(tmp_path), False, "\\\\nas\\d", ["PAN/a.tif", "PAN/b.tif"], ["PAN/a.tif"], opened_at=1)
    d = open_again_dialog.OpenAgainDialog(e)
    items = [d.files.item(i) for i in range(d.files.count())]
    assert [i.text().split()[0] for i in items] == ["PAN/a.tif", "PAN/b.tif"]
    assert [i.checkState() for i in items] == [Qt.CheckState.Checked, Qt.CheckState.Unchecked]
    assert items[1].text().endswith("なし") and d.open_button.isEnabled() and d.open_button.isDefault()
    assert d.chosen() == ["PAN/a.tif"]
    # A file gone ticked: not opened from here.
    items[1].setCheckState(Qt.CheckState.Checked)
    assert not d.open_button.isEnabled() and "見つからない" in d.note.text()
    items[1].setCheckState(Qt.CheckState.Unchecked)
    d.open_button.click()
    assert d.answer == ["PAN/a.tif"]
    # One file, gone.
    one = OpenedImage("E", "u", "衛星画像", str(tmp_path / "e.tif"), False, "")
    d = open_again_dialog.OpenAgainDialog(one)
    assert d.files.isHidden() and not d.open_button.isEnabled()
    d.catalog_button.click()
    assert d.answer == open_again_dialog.FROM_CATALOG
