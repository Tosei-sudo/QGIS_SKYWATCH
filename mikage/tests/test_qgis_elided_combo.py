"""
A drop-down as wide as where it is, not as its longest item (tsudo, 2026-11-25: 「観測結果の画像選択プルダウン、
長いIDのとき幅がすごいことになる」): ui/elided_combo.py, the 観測記録 dock's image and 画像補正's 「比べる画像」.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtGui import QFontMetrics, QHelpEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QHBoxLayout, QStyleFactory, QToolTip, QWidget  # noqa: E402

from mikage.features.observations import Observations  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.feature_harness import small_plugin  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface  # noqa: E402
from mikage.tests.test_qgis_observations import geotiff  # noqa: E402
from mikage.ui.elided_combo import ElidedCombo, shortened  # noqa: E402

#: Image names as long as real ones (Sentinel-1, Sentinel-2, ALOS-2 products).
LONG = [
    "S1A_IW_GRDH_1SDV_20261001T084512_20261001T084537_061234_07A1B2_9F3C",
    "S2B_MSIL2A_20261002T012649_N0511_R074_T54SUE_20261002T034512",
    "ALOS2_0123456780-261003_UBSR2.1GUD_HH_HV_ORTHO_JAXA_TOKYO_BAY",
]


def settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        APP.processEvents()


def test_a_long_middle_is_cut_in_its_middle_and_the_head_and_tail_kept():
    fm = QFontMetrics(QApplication.font())
    head, middle, tail = "2026-10-02 10:00  ", LONG[1], "（Sentinel-2B）"
    whole = head + middle + tail
    assert shortened(fm, whole, fm.horizontalAdvance(whole), (head, middle, tail)) == whole
    text = shortened(fm, whole, fm.horizontalAdvance(whole) // 2, (head, middle, tail))
    assert text.startswith(head + "S2B_") and text.endswith("034512" + tail) and "…" in text
    assert fm.horizontalAdvance(text) <= fm.horizontalAdvance(whole) // 2
    # No room for the middle with the tail: the tail goes (it is in the list and the tooltip).
    narrow = fm.horizontalAdvance(head + tail)
    text = shortened(fm, whole, narrow, (head, middle, tail))
    assert text.startswith(head + "S2B") and text.endswith("4512") and "…" in text and fm.horizontalAdvance(text) <= narrow
    # Nor without it: the whole text loses its end instead.
    narrower = fm.horizontalAdvance(head) + 10
    text = shortened(fm, whole, narrower, (head, middle, tail))
    assert text.startswith("2026") and text.endswith("…") and fm.horizontalAdvance(text) <= narrower
    # Without parts: the end.
    assert shortened(fm, middle, fm.horizontalAdvance(middle) // 2).endswith("…")


def test_the_box_is_as_wide_as_its_row_and_its_list_as_its_longest_item():
    short = ElidedCombo(20)
    short.add_parts("2026-10-01 10:00  ", "d1.tif")
    long = ElidedCombo(20)
    for name in LONG:
        long.add_parts("2026-10-01 10:00  ", name, "（Sentinel-1A）")
    # Before, Qt made the box as wide as its longest item (643 pixels here).
    assert long.minimumSizeHint().width() == short.minimumSizeHint().width()
    assert long.sizeHint().width() == short.sizeHint().width()
    # In a row at the right of the screen (a dock on the right of the window).
    screen = QApplication.primaryScreen().availableGeometry()
    row = QWidget()
    QHBoxLayout(row).addWidget(long)
    row.setGeometry(screen.right() - 560, screen.top() + 50, 560, 40)
    row.show()
    settle()
    try:
        long.setCurrentIndex(1)
        shown = long.shown_text()
        assert shown.startswith("2026-10-01 10:00  S2B_") and shown.endswith("（Sentinel-1A）") and "…" in shown
        assert long.currentText() == "2026-10-01 10:00  " + LONG[1] + "（Sentinel-1A）"
        assert long.list_width() > long.fontMetrics().horizontalAdvance(long.itemText(0))
        # Fusion (Linux, QGIS's own choice) widens a popup itself; Windows' style keeps it as wide as the box.
        for name in ("Fusion", "Windows"):
            style = QStyleFactory.create(name)
            long.setStyle(style)
            settle()
            long.showPopup()
            popup = long.view().window()
            assert popup is not row
            geometry = popup.geometry()
            # As wide as the longest item (at most the screen), toward the left: within the screen.
            assert geometry.width() >= min(long.list_width(), screen.width()) and geometry.width() > long.width(), name
            assert geometry.left() < long.mapToGlobal(QPoint(0, 0)).x(), name
            assert screen.left() <= geometry.left() and geometry.right() <= screen.right(), (name, screen, geometry)
            long.hidePopup()
            settle()
    finally:
        row.hide()
        row.deleteLater()


def test_full_tip_tells_the_chosen_item_whole():
    box = ElidedCombo(16, full_tip=True)
    box.setToolTip("対象の画像と重ねる別の時期の画像")
    box.add_parts("", LONG[0], "", "id0")
    assert box.tip() == LONG[0] + "\n対象の画像と重ねる別の時期の画像"
    box.show()
    settle()
    try:
        QApplication.sendEvent(box, QHelpEvent(QEvent.Type.ToolTip, QPoint(5, 5), box.mapToGlobal(QPoint(5, 5))))
        assert QToolTip.text() == box.tip()
        QToolTip.hideText()
        # Without it, the box's own tip (the 観測記録 dock's names the image).
        plain = ElidedCombo(16)
        plain.setToolTip("画像")
        plain.addItem(LONG[0])
        assert plain.tip() == "画像"
    finally:
        box.hide()


@pytest.fixture
def plugin_with(tmp_path):
    """The 観測記録 dock first shown with images of these names open (Qt sized its drop-down then)."""
    made = []

    def make(names):
        while made:  # one plugin at a time (they share the processing provider)
            made.pop().unload()
        QgsProject.instance().removeAllMapLayers()
        QgsSettings().remove("mikage")
        settings._set("local_gpkg", str(tmp_path / f"local{len(names)}.gpkg"))
        settings.set_recorder("田中")
        iface = FakeIface()
        layers = []
        for day, name in enumerate(names, start=1):
            layer = QgsRasterLayer(geotiff(tmp_path / f"{name}.tif", f"2026-10-0{day}T01:00:00Z"), name)
            assert layer.isValid()
            layers.append(layer)
        QgsProject.instance().addMapLayers(layers)
        iface.canvas.setDestinationCrs(layers[0].crs())
        iface.canvas.setLayers(layers)
        iface.canvas.setExtent(layers[0].extent())
        p = small_plugin(iface, Observations)
        p.initGui()
        made.append(p)
        dock = p.api.observation_dock
        dock.show()
        settle()
        return dock

    yield make
    for p in made:
        p.unload()
    QgsProject.instance().removeAllMapLayers()
    QgsSettings().remove("mikage")


def test_a_long_image_id_does_not_widen_the_observation_dock(plugin_with):
    short = plugin_with(["d1", "d2", "d3"])
    short_width = short.body.parentWidget().minimumSizeHint().width()
    dock = plugin_with(LONG)
    # Before: about 1,030 pixels (the drop-down 643), the dock's row of buttons scrolled out of sight.
    assert dock.body.parentWidget().minimumSizeHint().width() == short_width
    box = dock.image_select
    assert box.minimumSizeHint().width() < box.fontMetrics().horizontalAdvance("X") * 30
    dock.step(1)
    settle()
    assert box.currentText().endswith(LONG[1] + ".tif")
    shown = box.shown_text()
    # The time kept, the ID cut in its middle (its start and its end shown).
    assert shown.startswith(box.currentText()[:16] + "  S2B_") and shown.endswith("12.tif") and "…" in shown
    assert LONG[1] in box.toolTip()
    # ◀ ▶ ＋ as narrow as their marks (Qt's buttons are at least 80 pixels wide).
    for button in (dock.prev_button, dock.next_button, dock.list_button):
        assert button.width() < 60


def test_the_other_image_of_two_dates_does_not_widen_the_enhance_dock(tmp_path):
    from mikage.qgis_io.context import PluginContext
    from mikage.tests.test_qgis_enhance import layer_of, write_tif
    from mikage.ui.enhance_dock import EnhanceDock
    import numpy as np
    from osgeo import gdal

    dem = np.zeros((20, 20), dtype=np.float32)
    project = QgsProject.instance()
    widths = []
    for names in (["t1", "t2"], ["t1", *LONG]):
        iface = FakeIface()
        dock = EnhanceDock(iface, PluginContext(iface.window), lambda: None, iface.window)
        layers = []
        for n in names:
            layer = layer_of(write_tif(tmp_path / f"{n}.tif", [dem], gdal.GDT_Float32))
            layer.setName(n)
            layers.append(layer)
        try:
            project.addMapLayers(layers)
            iface.setActiveLayer(layers[0])
            settle()
            box = dock.colorize_box.other
            assert box.count() == len(names) - 1
            widths.append(box.minimumSizeHint().width())
            if len(names) > 2:
                assert box.currentText() in LONG and box.tip().startswith(box.currentText() + "\n")
        finally:
            project.removeAllMapLayers()
            # As the plugin's unload: what it holds let go (the map's orienter, answering the project otherwise).
            dock.close_down()
            dock.deleteLater()
    assert widths[0] == widths[1]
