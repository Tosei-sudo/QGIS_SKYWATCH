"""
「一番上に固定」 (tsudo 2026-11-23: 各レイヤーを上位固定表示で常に一番上に表示するという設定が欲しい。
新しいレイヤーが追加されたら、そのレイヤーの下、選択しているレイヤーの上に来るようになる).
"""
import json

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import (  # noqa: E402
    QgsLayerTree, QgsLayerTreeModel, QgsLayerTreeRegistryBridge, QgsProject, QgsSettings, QgsVectorLayer,
)
from qgis.gui import QgsLayerTreeView  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QMenu  # noqa: E402

from mikage.qgis_io import pinned_layers, settings  # noqa: E402
from mikage.qgis_io.pinned_layers import PINNED, arrange, insertion_point, is_pinned, set_pinned, top_index  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402
from mikage.ui.layer_pins import ENTRY, LayerPins  # noqa: E402


@pytest.fixture
def project():
    project = QgsProject.instance()
    project.clear()
    yield project
    project.clear()
    # QGIS's place for new layers back at the top (a test fixed it below its pinned ones).
    project.layerTreeRegistryBridge().setLayerInsertionPoint(QgsLayerTreeRegistryBridge.InsertionPoint(project.layerTreeRoot(), 0))


def layer(name):
    return QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")


def add(project, *names):
    """Layers added as a plugin adds at the bottom (the first name at the top)."""
    out = []
    for name in names:
        made = layer(name)
        project.addMapLayer(made, False)
        project.layerTreeRoot().addLayer(made)
        out.append(made)
    return out


def tree(group=None):
    """The tree's names, top first; a group as (name, [its names])."""
    group = group or QgsProject.instance().layerTreeRoot()
    out = []
    for child in group.children():
        if QgsLayerTree.isGroup(child):
            out.append((child.name(), tree(child)))
        else:
            out.append(child.name())
    return out


def node(name):
    root = QgsProject.instance().layerTreeRoot()
    found = [n for n in root.findLayers() if n.name() == name] or ([root.findGroup(name)] if root.findGroup(name) else [])
    return found[0]


def settle(turns=5):
    for _ in range(turns):
        QCoreApplication.processEvents()


def panel():
    view = QgsLayerTreeView()
    model = QgsLayerTreeModel(QgsProject.instance().layerTreeRoot())
    model.setFlag(QgsLayerTreeModel.Flag.AllowNodeReorder)
    view.setModel(model)
    view._model = model
    return view


# ---- the rules (qgis_io/pinned_layers.py) ----------------------------------------------------------------------


def test_the_mark_is_a_custom_property_read_back_as_text_too(project):
    (a,) = add(project, "a")
    n = node("a")
    assert not is_pinned(n)
    set_pinned(n, True)
    assert is_pinned(n) and n.customProperty(PINNED) is True
    n.setCustomProperty(PINNED, "true")
    assert is_pinned(n)
    n.setCustomProperty(PINNED, "false")
    assert not is_pinned(n)
    set_pinned(n, False)
    assert n.customProperty(PINNED) is None and not is_pinned(n)


def test_pinned_ones_go_to_the_top_in_their_order_and_come_out_of_groups(project):
    add(project, "c", "a", "b")
    root = project.layerTreeRoot()
    group = root.addGroup("g")
    inner = layer("d")
    project.addMapLayer(inner, False)
    group.addLayer(inner)
    set_pinned(node("a"), True)
    set_pinned(node("d"), True)
    assert top_index(root) == 2
    moved = arrange(root)
    settle()
    assert [copy.name() for _, copy in moved] == ["a", "d"]
    assert tree() == ["a", "d", "c", "b", ("g", [])]
    # The layers stay in the project (moved as the layer panel moves one: a copy first, then the node out).
    assert len(project.mapLayers()) == 4
    assert is_pinned(node("a")) and is_pinned(node("d"))
    assert top_index(root) == 2
    # Arranged already: nothing moves.
    assert arrange(root) == []


def test_a_pinned_group_moves_with_its_layers(project):
    add(project, "a", "b")
    root = project.layerTreeRoot()
    group = root.addGroup("refs")
    for name in ("r1", "r2"):
        made = layer(name)
        project.addMapLayer(made, False)
        group.addLayer(made)
    set_pinned(root.findGroup("refs"), True)
    arrange(root)
    settle()
    assert tree() == [("refs", ["r1", "r2"]), "a", "b"]
    assert pinned_layers.pinned_layer_ids(root) == [node("r1").layerId(), node("r2").layerId()]
    # A layer inside a pinned group goes with it (not listed on its own), new layers in it are pinned with it.
    assert pinned_layers.in_pinned(node("r2"))


def test_where_qgis_adds_new_layers(project):
    add(project, "p", "a", "b")
    root = project.layerTreeRoot()
    group = root.addGroup("g")
    pinned_group = root.insertGroup(0, "pg")
    set_pinned(node("p"), True)
    set_pinned(pinned_group, True)
    arrange(root)
    settle()
    assert tree() == [("pg", []), "p", "a", "b", ("g", [])]
    pinned_group = root.findGroup("pg")
    # Above a layer not pinned: as QGIS says.
    assert insertion_point(root, root, 3) == (root, 3)
    # Above a pinned one, at the very top, nothing selected: just below the pinned ones.
    assert insertion_point(root, root, 1) == (root, 2)
    assert insertion_point(root, root, 0) == (root, 2)
    assert insertion_point(root, None, 0) == (root, 2)
    # In a pinned group: below the pinned ones too; in another group: as QGIS says.
    assert insertion_point(root, pinned_group, 0) == (root, 2)
    g = root.findGroup("g")
    assert insertion_point(root, g, 0) == (g, 0)


def test_a_custom_layer_order_has_the_pinned_ones_first(project):
    a, b, c = add(project, "a", "b", "c")
    root = project.layerTreeRoot()
    root.setHasCustomLayerOrder(True)
    root.setCustomLayerOrder([a, b, c])
    set_pinned(node("c"), True)
    assert pinned_layers.arrange_layer_order(root)
    assert [l.name() for l in root.customLayerOrder()] == ["c", "a", "b"]
    assert not pinned_layers.arrange_layer_order(root)


# ---- the layer panel (ui/layer_pins.py) ----------------------------------------------------------------------


def test_a_layer_qgis_adds_goes_just_below_the_pinned_ones(project):
    add(project, "p1", "p2", "a")
    set_pinned(node("p1"), True)
    set_pinned(node("p2"), True)
    pins = LayerPins(project)
    try:
        assert tree() == ["p1", "p2", "a"]
        # QGIS's place left at the top (no layer panel here): the pinned ones go back up a moment later.
        project.addMapLayer(layer("new"))
        assert wait_for(lambda: tree() == ["p1", "p2", "new", "a"])
        # What MIKAGE adds at the top: just below them, nothing moved.
        first = node("p1")
        root = project.layerTreeRoot()
        group = root.insertGroup(top_index(root), "images")
        img = layer("img")
        project.addMapLayer(img, False)
        group.addLayer(img)
        settle()
        assert tree() == ["p1", "p2", ("images", ["img"]), "new", "a"]
        assert node("p1") is first
    finally:
        pins.unload()


def test_qgis_place_is_kept_below_the_pinned_ones(project):
    """QGIS's place for new layers (iface.layerTreeInsertionPoint, from the layer selected) fixed when it is above the pinned ones."""
    add(project, "p", "a", "b")
    root = project.layerTreeRoot()
    pg = root.insertGroup(0, "pg")
    set_pinned(pg, True)
    set_pinned(node("p"), True)
    selected = {"group": root, "position": 0}
    point = lambda: QgsLayerTreeRegistryBridge.InsertionPoint(selected["group"], selected["position"])  # noqa: E731
    pins = LayerPins(project, qgis_point=point)
    try:
        assert tree() == [("pg", []), "p", "a", "b"]
        # The pinned layer selected (QGIS: above it): just below the pinned ones.
        selected.update(group=root, position=1)
        pins._fix_point()
        project.addMapLayer(layer("n1"))
        settle()
        assert tree() == [("pg", []), "p", "n1", "a", "b"]
        # The pinned group selected (QGIS: in it): below the pinned ones, not in the group.
        selected.update(group=root.findGroup("pg"), position=0)
        pins._fix_point()
        project.addMapLayer(layer("n2"))
        settle()
        assert tree() == [("pg", []), "p", "n2", "n1", "a", "b"]
        # A layer not pinned selected: above it, as QGIS does.
        selected.update(group=root, position=5)
        pins._fix_point()
        project.addMapLayer(layer("n3"))
        settle()
        assert tree() == [("pg", []), "p", "n2", "n1", "a", "n3", "b"]
    finally:
        pins.unload()


def test_a_layer_dragged_over_the_pinned_ones_ends_below_them(project):
    add(project, "p", "a", "b")
    set_pinned(node("p"), True)
    pins = LayerPins(project)
    try:
        root = project.layerTreeRoot()
        # As the layer panel drags one: a copy dropped at the top, then the node taken away.
        b = node("b")
        root.insertChildNode(0, b.clone())
        root.removeChildNode(b)
        assert wait_for(lambda: tree() == ["p", "b", "a"])
        # A pinned one dragged into a group comes back to the top.
        group = root.addGroup("g")
        p = node("p")
        group.insertChildNode(0, p.clone())
        root.removeChildNode(p)
        assert wait_for(lambda: tree() == ["p", "b", "a", ("g", [])])
        assert len(project.mapLayers()) == 3
    finally:
        pins.unload()


def test_the_layer_panel_entry_pins_and_unpins_with_the_pin_shown(project):
    add(project, "a", "b", "c")
    view = panel()
    pins = LayerPins(project, view)
    try:
        view.setCurrentIndex(view.node2index(node("c")))
        menu = QMenu()
        view.contextMenuAboutToShow.emit(menu)
        entry = [a for a in menu.actions() if a.text() == ENTRY]
        assert len(entry) == 1 and entry[0].isCheckable() and not entry[0].isChecked()
        entry[0].trigger()
        assert tree() == ["c", "a", "b"]
        c = node("c")
        assert is_pinned(c)
        # The pin beside its name; the moved node still the one chosen.
        assert len(view.indicators(c)) == 1
        assert view.currentNode() is c
        # Ticked when every selected node is pinned; choosing it again takes the pin off (the layer stays where it is).
        menu = QMenu()
        view.contextMenuAboutToShow.emit(menu)
        entry = [a for a in menu.actions() if a.text() == ENTRY][0]
        assert entry.isChecked()
        entry.trigger()
        assert not is_pinned(c) and view.indicators(c) == []
        assert tree() == ["c", "a", "b"]
        # Nothing selected: no entry.
        view.setCurrentIndex(view._model.index(-1, -1))
        view.clearSelection()
        menu = QMenu()
        view.contextMenuAboutToShow.emit(menu)
        assert not [a for a in menu.actions() if a.text() == ENTRY]
    finally:
        pins.unload()


def test_the_entry_sits_after_qgis_moves(project):
    add(project, "a")
    view = panel()
    pins = LayerPins(project, view)
    try:
        view.setCurrentIndex(view.node2index(node("a")))
        menu = QMenu()
        for text in ("全体図に表示", "Move to &Top", "Move to &Bottom", None, "属性テーブルを開く"):
            menu.addSeparator() if text is None else menu.addAction(text)
        view.contextMenuAboutToShow.emit(menu)
        texts = [a.text() for a in menu.actions()]
        assert texts == ["全体図に表示", "Move to &Top", "Move to &Bottom", ENTRY, "", "属性テーブルを開く"]
    finally:
        pins.unload()


def test_several_selected_are_pinned_together_in_their_order(project):
    add(project, "a", "b", "c", "d")
    view = panel()
    pins = LayerPins(project, view)
    try:
        pins.pin([node("d"), node("b")], True)
        assert tree() == ["b", "d", "a", "c"]
        assert all(len(view.indicators(node(n))) == 1 for n in ("b", "d"))
        assert view.indicators(node("a")) == []
    finally:
        pins.unload()
    # Unloaded: the pins no longer shown, the marks kept (the project keeps them).
    assert view.indicators(node("b")) == [] and is_pinned(node("b"))


def test_a_removed_pinned_layer_takes_its_pin_away(project):
    a, b = add(project, "a", "b")
    view = panel()
    pins = LayerPins(project, view)
    try:
        pins.pin([node("b")], True)
        assert len(pins._shown) == 1
        project.removeMapLayer(b.id())
        settle()
        assert pins._shown == []
        assert tree() == ["a"]
    finally:
        pins.unload()


def test_the_project_file_keeps_the_pins(project, tmp_path):
    add(project, "a", "b")
    set_pinned(node("b"), True)
    arrange(project.layerTreeRoot())
    path = str(tmp_path / "pins.qgs")
    assert project.write(path)
    project.clear()
    assert project.read(path)
    assert tree() == ["b", "a"]
    assert is_pinned(node("b")) and not is_pinned(node("a"))
    view = panel()
    pins = LayerPins(project, view)
    try:
        assert len(view.indicators(node("b"))) == 1
        # A project read while the plugin runs: the pins shown once it is read.
        project.clear()
        assert project.read(path)
        assert wait_for(lambda: len(pins._shown) == 1 and len(view.indicators(node("b"))) == 1)
    finally:
        pins.unload()


def test_the_custom_layer_order_follows(project):
    a, b, c = add(project, "a", "b", "c")
    root = project.layerTreeRoot()
    root.setHasCustomLayerOrder(True)
    root.setCustomLayerOrder([a, b, c])
    pins = LayerPins(project)
    try:
        pins.pin([node("c")], True)
        assert [l.name() for l in root.customLayerOrder()] == ["c", "a", "b"]
        # Reordered in the Layer Order panel: the pinned one back first.
        root.setCustomLayerOrder([a, c, b])
        assert wait_for(lambda: [l.name() for l in root.customLayerOrder()] == ["c", "a", "b"])
    finally:
        pins.unload()


# ---- MIKAGE's own additions and the settings file ---------------------------------------------------------------


def test_catalog_images_go_just_below_the_pinned_ones(project):
    from mikage.core.catalog.search import CatalogRecord
    from mikage.qgis_io.catalog_io import add_record_layers

    add(project, "p", "a")
    set_pinned(node("p"), True)
    record = CatalogRecord({}, None, "IMG1", None, None, None, "", None, None, None)
    add_record_layers([layer("b1"), layer("b2")], record, project)
    assert tree() == ["p", ("IMG1", ["b1", "b2"]), "a"]


def test_the_plugin_keeps_pins_from_the_start(project):
    from mikage.features.layers import Layers
    from mikage.tests.feature_harness import small_plugin

    QgsSettings().remove("mikage")
    iface = FakeIface()
    view = panel()
    iface.layerTreeView = lambda: view
    plugin = small_plugin(iface, Layers)
    plugin.initGui()
    try:
        assert plugin.layer_pins is not None and plugin.layer_pins.view is view
        add(project, "a", "b")
        plugin.layer_pins.pin([node("b")], True)
        assert tree() == ["b", "a"]
    finally:
        plugin.unload()
    assert plugin.layer_pins is None


def test_settings_file_layers_can_start_pinned(project, tmp_path):
    from mikage.features.maps import Maps
    from mikage.tests.feature_harness import small_plugin
    from mikage.tests.test_qgis_observations import geotiff

    QgsSettings().remove("mikage")
    geotiff(tmp_path / "ortho.tif")
    (tmp_path / "places.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "A"}, "geometry": {"type": "Point", "coordinates": [139.7, 35.6]}}]}), encoding="utf-8")
    (tmp_path / "roads.geojson").write_text((tmp_path / "places.geojson").read_text(encoding="utf-8"), encoding="utf-8")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"layers": [
        {"type": "file", "url": "./places.geojson", "pinned": True},
        {"type": "cog", "url": "./ortho.tif"},
        {"type": "file", "url": "./roads.geojson", "pinned": True, "visible": False},
    ]}), encoding="utf-8")
    settings.set_config_source(str(path))
    try:
        iface = FakeIface()
        plugin = small_plugin(iface, Maps)
        plugin.initGui()
        try:
            assert wait_for(lambda: len(project.mapLayers()) == 3)
            # The pinned ones on top (the later entry higher), the others below as before.
            assert tree() == ["roads.geojson", "places.geojson", "ortho.tif"]
            assert is_pinned(node("roads.geojson")) and is_pinned(node("places.geojson")) and not is_pinned(node("ortho.tif"))
            assert not node("roads.geojson").itemVisibilityChecked() and node("places.geojson").itemVisibilityChecked()
        finally:
            plugin.unload()
    finally:
        QgsSettings().remove("mikage")
