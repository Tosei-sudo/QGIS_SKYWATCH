"""
クイックパンシャープン: two images chosen in the layer panel, the
multispectral one drawn pan-sharpened where the map shows it (no file): which
is the pan (fewer bands, alpha not counted), the drawing (the same pixels as
the Processing algorithm's file at the pan's resolution), what is left as it
is (zoomed out, off the pan, no pan value), the pan hidden and shown again,
the project, the end, the switch and 画像補正's box.
"""
import numpy as np
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsLayerTreeModel, QgsMultiBandColorRenderer, QgsProject, QgsRasterLayer, QgsRectangle,
)
from qgis.gui import QgsLayerTreeView  # noqa: E402
from qgis.PyQt.QtCore import QItemSelectionModel  # noqa: E402

from mikage.core.enhance.pansharpen import pick_pan  # noqa: E402
from mikage.qgis_io import enhance_renderer as er  # noqa: E402
from mikage.qgis_io import pansharpen_io as io  # noqa: E402
from mikage.qgis_io import quick_pan as qp  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_pansharpen import scene, write  # noqa: E402

gdal.UseExceptions()
X0, Y0 = 500000, 4000000


@pytest.fixture(autouse=True)
def keeper():
    """As the plugin has it: links end as their layers go, a project opened puts them back."""
    keeper = qp.QuickPanKeeper()
    yield keeper
    for link in qp.links():
        qp.end(link)
    QgsProject.instance().removeAllMapLayers()
    keeper.unload()


def images(tmp_path, h=200, w=240, pan_alpha=None, ms_bands=4, pan_epsg=32654):
    """A pan at 1 m (with an alpha band of `pan_alpha` when given) and a 4 m multispectral image of the same place."""
    pan, base = scene(h, w)
    ms = base.reshape(4, h // 4, 4, w // 4, 4).mean(axis=(2, 4))[:ms_bands]
    pan_arrays = np.clip(pan, 1, 65535).astype(np.uint16)[None]
    if pan_alpha is not None:
        pan_arrays = np.concatenate([pan_arrays, pan_alpha.astype(np.uint16)[None]])
    if pan_epsg == 32654:
        pan_gt = [X0, 1, 0, Y0, 0, -1]
    else:  # the same ground in Web Mercator (about 1.2 m pixels there)
        from osgeo import osr

        utm, merc = osr.SpatialReference(), osr.SpatialReference()
        utm.ImportFromEPSG(32654)
        merc.ImportFromEPSG(3857)
        for srs in (utm, merc):
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        x, y, _ = osr.CoordinateTransformation(utm, merc).TransformPoint(X0, Y0)
        pan_gt = [x, 1.25, 0, y, 0, -1.25]
    pan_path = write(tmp_path / "pan.tif", pan_arrays, pan_gt, epsg=pan_epsg, alpha=pan_alpha is not None)
    ms_path = write(tmp_path / "ms.tif", ms.astype(np.uint16), [X0, 4, 0, Y0, 0, -4])
    p, m = QgsRasterLayer(pan_path, "pan"), QgsRasterLayer(ms_path, "ms")
    assert p.isValid() and m.isValid()
    QgsProject.instance().addMapLayers([m, p])
    return p, m


def started(ms, pan, **options) -> qp.PanLink:
    link = qp.start(ms, pan, **options)
    assert wait_for(lambda: not link.fitting, 10000)
    assert link.model is not None, link.failed
    return link


def shown(node_layer) -> bool:
    return QgsProject.instance().layerTreeRoot().findLayer(node_layer.id()).itemVisibilityChecked()


def test_the_pan_is_the_one_with_fewer_bands_else_the_finer_pixel():
    assert pick_pan((1, 0.5), (4, 2.0)) == 0
    assert pick_pan((4, 2.0), (1, 0.5)) == 1
    # Fewer bands, whatever the order.
    assert pick_pan((3, 2.0), (1, 0.5)) == 1
    # As many bands: the finer pixel.
    assert pick_pan((1, 2.0), (1, 0.5)) == 1
    assert "判定できない" in pick_pan((3, 1.0), (3, 1.01))
    # The one with fewer bands must be the finer.
    assert "細かくない" in pick_pan((1, 4.0), (4, 2.0))
    assert "値のバンド" in pick_pan((0, 1.0), (4, 2.0))


def test_a_pan_with_an_alpha_band_is_told_by_its_value_bands(tmp_path):
    # pan: a value band and alpha (2 bands); ms: 2 value bands (as many bands as the pan in all).
    alpha = np.full((200, 240), 255)
    pan, ms = images(tmp_path, pan_alpha=alpha, ms_bands=2)
    assert pan.bandCount() == ms.bandCount() == 2
    assert qp.value_bands(pan.dataProvider()) == [1]
    assert qp.choose(ms, pan) == (pan, ms)
    assert qp.choose(pan, ms) == (pan, ms)
    with pytest.raises(qp.QuickPanError, match="別々"):
        qp.choose(ms, ms)


def test_drawn_at_the_pan_resolution_it_is_the_processing_file(tmp_path):
    pan, ms = images(tmp_path)
    out = str(tmp_path / "out.tif")
    io.pan_sharpen_files(pan.source(), ms.source(), out)
    reference = gdal.Open(out).ReadAsArray()
    link = started(ms, pan)
    extent = QgsRectangle(X0 + 20, Y0 - 180, X0 + 220, Y0 - 20)
    bands, valid = link.sharpened((1, 2, 3, 4), extent, 200, 160)
    assert valid.all()
    assert np.array_equal(np.stack(bands), reference[:, 20:180, 20:220])
    # The renderer draws those values (stretched as the image itself is) in place of the resampled ones.
    r = er.enhance_renderer(ms)
    assert r.state.pan is link
    assert isinstance(link.before[0], QgsMultiBandColorRenderer)
    drawn = r.argb(extent, 200, 160)
    link.model, model = None, link.model
    plain = r.argb(extent, 200, 160)
    link.model = model
    assert (drawn != plain).mean() > 0.5


def test_the_map_draws_it(tmp_path):
    """QGIS's own drawing (a parallel job, the renderer cloned) gets the detail; the pan is not drawn."""
    from qgis.core import QgsMapRendererParallelJob, QgsMapSettings
    from qgis.PyQt.QtCore import QSize

    pan, ms = images(tmp_path)

    def render():
        settings = QgsMapSettings()
        settings.setLayers([ms, pan] if shown(pan) else [ms])
        settings.setDestinationCrs(ms.crs())
        settings.setExtent(QgsRectangle(X0 + 40, Y0 - 160, X0 + 200, Y0 - 40))
        settings.setOutputSize(QSize(160, 120))
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        img = job.renderedImage()
        return np.array([[img.pixel(x, y) for x in range(0, 160, 4)] for y in range(0, 120, 4)])

    er.apply(ms, er.EnhanceSettings(bands=(1, 2, 3)))
    plain = render()
    started(ms, pan)
    sharp = render()
    assert (plain != sharp).mean() > 0.5
    # Edges: the sharpened picture changes more from pixel to pixel than the resampled one.
    assert np.abs(np.diff(sharp & 0xFF, axis=1)).mean() > np.abs(np.diff(plain & 0xFF, axis=1)).mean()


def test_zoomed_out_off_the_pan_and_where_it_has_no_value_the_image_is_as_it_is(tmp_path):
    alpha = np.full((200, 240), 255)
    alpha[:, :120] = 0
    pan, ms = images(tmp_path, pan_alpha=alpha)
    link = started(ms, pan)
    whole = ms.extent()
    # At the multispectral pixel (4 m) or coarser: nothing to add.
    assert not link.sharpens(whole, 60, 50)
    assert link.sharpened((1, 2, 3), whole, 30, 25) is None
    assert link.sharpens(whole, 240, 200)
    # Off the pan (far east of it): not sharpened.
    assert not link.sharpens(QgsRectangle(X0 + 1000, Y0 - 100, X0 + 1100, Y0), 100, 100)
    # The pan's transparent half (west) shows the image resampled; the east half gets the detail.
    extent = QgsRectangle(X0 + 40, Y0 - 160, X0 + 200, Y0 - 40)
    bands, _valid = link.sharpened((1, 2, 3), extent, 160, 120)
    resampled, _ = er.read_bands(link._ms, (1, 2, 3), extent, 160, 120)  # noqa: SLF001 (the link's bilinear reader)
    assert np.array_equal(bands[0][:, :70], resampled[0][:, :70])
    assert not np.array_equal(bands[0][:, 90:], resampled[0][:, 90:])


def test_strength_zero_is_the_image_resampled_and_another_method_is_fitted_again(tmp_path):
    pan, ms = images(tmp_path)
    link = started(ms, pan)
    extent = QgsRectangle(X0 + 20, Y0 - 120, X0 + 120, Y0 - 20)
    qp.set_options(ms, strength=0)
    bands, _ = link.sharpened((1, 2, 3), extent, 100, 100)
    resampled, _ = er.read_bands(link._ms, (1, 2, 3), extent, 100, 100)  # noqa: SLF001
    assert all(np.abs(a.astype(int) - b.astype(int)).max() <= 1 for a, b in zip(bands, resampled))
    gs = link.model
    qp.set_options(ms, method="brovey", strength=1)
    assert wait_for(lambda: not link.fitting and link.model is not None, 10000)
    assert link.model is not gs and link.model.gains == [0.0] * 4
    assert qp._saved(ms)["method"] == "brovey"  # noqa: SLF001


def test_the_pan_is_hidden_while_used_and_the_layers_come_back_as_they_were(tmp_path):
    pan, ms = images(tmp_path)
    before = ms.renderer()
    assert isinstance(before, QgsMultiBandColorRenderer)
    started(ms, pan)
    assert not shown(pan)
    assert er.enhance_renderer(ms) is not None
    assert qp.used_as_pan(pan) is qp.link_of(ms)
    qp.stop(ms)
    assert shown(pan)
    assert er.enhance_renderer(ms) is None and isinstance(ms.renderer(), QgsMultiBandColorRenderer)
    assert not ms.customProperty(qp.PROPERTY)
    # A correction on before stays (bands too), and a pan hidden before stays hidden.
    er.apply(ms, er.EnhanceSettings(bands=(3, 2, 1)))
    QgsProject.instance().layerTreeRoot().findLayer(pan.id()).setItemVisibilityChecked(False)
    started(ms, pan)
    qp.stop(ms)
    assert er.enhance_renderer(ms).settings.bands == (3, 2, 1)
    assert not shown(pan)


def test_an_image_drawn_by_mikage_fast_drawing_gets_qgis_own_renderer_back(tmp_path):
    from mikage.qgis_io import fast_draw

    pan, ms = images(tmp_path)
    assert fast_draw.wrap(ms)
    link = started(ms, pan)
    assert type(link.before[0]) is QgsMultiBandColorRenderer
    qp.stop(ms)
    # QGIS's own type (MIKAGE's fast drawing, when on, wraps it again as it does any style change).
    assert type(ms.renderer()) is QgsMultiBandColorRenderer


def test_it_ends_when_a_layer_goes_or_another_renderer_draws_the_image(tmp_path):
    pan, ms = images(tmp_path)
    started(ms, pan)
    # 補正 off (QGIS's own renderer): ended, the pan shown again.
    er.remove(ms)
    assert qp.link_of(ms) is None and shown(pan)
    started(ms, pan)
    QgsProject.instance().removeMapLayer(pan.id())
    assert qp.link_of(ms) is None
    assert er.enhance_renderer(ms) is None


def test_a_pan_in_another_crs_is_reprojected(tmp_path):
    pan, ms = images(tmp_path, pan_epsg=3857)
    assert qp.choose(pan, ms) == (pan, ms)
    link = started(ms, pan)
    extent = QgsRectangle(X0 + 40, Y0 - 160, X0 + 200, Y0 - 40)
    assert link.sharpens(extent, 160, 120)
    bands, valid = link.sharpened((1, 2, 3), extent, 160, 120)
    resampled, _ = er.read_bands(link._ms, (1, 2, 3), extent, 160, 120)  # noqa: SLF001
    assert valid.all()
    assert np.abs(bands[0].astype(int) - resampled[0].astype(int)).mean() > 5


def test_a_project_opens_with_it(tmp_path):
    pan, ms = images(tmp_path)
    started(ms, pan, strength=1.5)
    project = QgsProject.instance()
    path = str(tmp_path / "p.qgz")
    assert project.write(path)
    project.clear()
    assert not qp.links()
    assert project.read(path)
    assert wait_for(lambda: bool(qp.links()) and not qp.links()[0].fitting, 10000)
    ms2 = project.mapLayersByName("ms")[0]
    pan2 = project.mapLayersByName("pan")[0]
    link = qp.link_of(ms2)
    assert link is not None and link.pan_id == pan2.id() and link.strength == 1.5
    assert er.enhance_renderer(ms2).state.pan is link
    assert not shown(pan2)
    # Ended now: the pan comes back, as it was before it was used.
    qp.stop(ms2)
    assert shown(pan2)


def make_switch():
    from mikage.tests.qgis_fakes import FakeIface

    iface = FakeIface()
    model = QgsLayerTreeModel(QgsProject.instance().layerTreeRoot())
    view = QgsLayerTreeView()
    view.setModel(model)
    iface.layerTreeView = lambda: view
    iface._tree = (model, view)
    return iface, view


def select(view, layers):
    selection = view.selectionModel()
    selection.clearSelection()
    root = QgsProject.instance().layerTreeRoot()
    for layer in layers:
        selection.select(view.node2index(root.findLayer(layer.id())), QItemSelectionModel.SelectionFlag.Select)


def test_the_switch_starts_with_two_layers_chosen_and_ends_with_either(tmp_path):
    from mikage.ui.quick_pan import QuickPanSwitch

    from qgis.PyQt.QtWidgets import QMenu
    try:  # Qt6
        from qgis.PyQt.QtGui import QAction
    except ImportError:  # Qt5
        from qgis.PyQt.QtWidgets import QAction

    pan, ms = images(tmp_path)
    iface, view = make_switch()
    action = QAction("クイックパンシャープン", iface.window)
    switch = QuickPanSwitch(iface, action)
    try:
        select(view, [ms])
        assert not switch.toggle()
        assert qp.link_of(ms) is None
        select(view, [pan, ms])
        # The layer panel's right-click has the entry for two images.
        menu = QMenu()
        switch._layer_menu(menu)  # noqa: SLF001 (what contextMenuAboutToShow calls)
        entry = next(a for a in menu.actions() if a.objectName() == "mikageQuickPan")
        assert not entry.isChecked()
        entry.trigger()
        link = qp.link_of(ms)
        assert link is not None and link.pan_id == pan.id()
        assert action.isChecked()
        wait_for(lambda: not link.fitting, 10000)
        # Either one chosen, the same entry ends it.
        select(view, [pan])
        assert action.isChecked()
        action.trigger()
        assert qp.link_of(ms) is None and not action.isChecked()
        # Two images that cannot be put together: said so.
        gray = QgsRasterLayer(pan.source(), "pan2")
        QgsProject.instance().addMapLayer(gray)
        assert not switch.toggle([pan, gray])
        assert qp.links() == []
    finally:
        switch.close()


def test_the_enhance_dock_shows_the_box_while_the_layer_is_in_it(tmp_path):
    from mikage.tests.test_qgis_enhance_group import make_dock

    pan, ms = images(tmp_path)
    iface, dock = make_dock()
    try:
        iface.setActiveLayer(ms)
        box = dock.quick_box
        assert box.isHidden()
        link = started(ms, pan)
        assert not box.isHidden()
        assert "パンクロ: pan" in box.label.text()
        assert dock.enabled.isChecked()
        box.strength.setValue(150)
        box._strength_chosen()  # noqa: SLF001 (the pause after the slider)
        assert link.strength == 1.5
        # A change of the correction keeps the link (the same renderer).
        dock.dra_on.setChecked(True)
        dock.apply()
        assert er.enhance_renderer(ms).state.pan is link
        # The pan's own box says what it is used for.
        iface.setActiveLayer(pan)
        assert "「ms」のパンクロとして使用中" in box.label.text()
        box.off.click()
        assert qp.link_of(ms) is None and box.isHidden()
    finally:
        dock.close_down()
