"""Docks never make QGIS's window larger than the screen: their bodies scroll, and a floating one fits its screen."""
import pytest

qgis_core = pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QRect  # noqa: E402
from qgis.PyQt.QtGui import QGuiApplication  # noqa: E402

from skywatch.tests.qgis_fakes import FakeIface  # noqa: E402


@pytest.fixture
def plugin():
    from skywatch import classFactory

    QgsSettings().remove("skywatch")
    iface = FakeIface()
    plugin = classFactory(iface)
    plugin.initGui()
    APP.processEvents()
    yield plugin
    plugin.unload()
    QgsSettings().remove("skywatch")


def test_every_dock_open_still_fits_a_small_window(plugin):
    # A laptop's 1280 × 720 (e.g. 1920 × 1080 at 150 %). Before, all docks open needed at least about 1250 × 1055.
    window = plugin.iface.window
    for dock in plugin.docks.values():
        dock.show()
    plugin.place_docks()
    for name in ("撮像計画の担当", "観測記録の担当", "画像判読"):
        assert plugin.dock_layouts.use(name)
        window.resize(1280, 720)
        APP.processEvents()
        assert (window.width(), window.height()) == (1280, 720), name
        assert window.minimumSizeHint().height() < 720 and window.minimumSizeHint().width() < 1280, name
    for name, dock in plugin.docks.items():
        assert dock.minimumSizeHint().height() < 150 and dock.minimumSizeHint().width() < 150, name


def test_a_floating_dock_larger_than_its_screen_fits_it(plugin):
    # As a layout saved on a larger screen and used on this one.
    screen = QGuiApplication.primaryScreen().availableGeometry()
    dock = plugin.docks["SatObsObservations"]
    dock.show()
    dock.setFloating(True)
    dock.setGeometry(QRect(screen.x() + 40, screen.y() + 30, screen.width() + 600, screen.height() + 400))
    APP.processEvents()
    dock.keep_on_screen()
    APP.processEvents()
    assert dock.isFloating()
    assert screen.contains(dock.frameGeometry()), (screen, dock.frameGeometry())
    # One that fits is left where it is.
    dock.setGeometry(QRect(screen.x() + 40, screen.y() + 30, 300, 250))
    APP.processEvents()
    dock.keep_on_screen()
    assert dock.geometry() == QRect(screen.x() + 40, screen.y() + 30, 300, 250)


def test_rows_of_buttons_and_filters_wrap_within_a_usual_docks_width(plugin):
    """
    Before, one line each: 「撮像計画ファイル出力」 needed about 670 px,
    「撮像依頼」 600 and 「撮像予定」 530, so that at a dock's usual width
    「書き出し」, 「再読み込み」 and the filters were off its edge (reached only
    by scrolling sideways) and the room of each system was a column one
    character wide.
    """
    from qgis.PyQt.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QScrollArea

    for name in ("SatObsPlanFiles", "SatObsSchedule", "SatObsRequests"):
        dock = plugin.docks[name]
        dock.show()
        dock.setFloating(True)
        dock.resize(470, 760)
        APP.processEvents()
        area = dock.findChild(QScrollArea)
        inner = area.widget()
        assert inner.minimumSizeHint().width() <= 450, (name, inner.minimumSizeHint().width())
        assert inner.width() <= area.viewport().width(), name
        shown = [w for kind in (QAbstractButton, QComboBox, QLineEdit) for w in inner.findChildren(kind)
                 if w.isVisibleTo(inner) and w.parent() is inner]
        assert len(shown) >= 6, name
        for w in shown:
            assert w.geometry().right() <= inner.width() and w.geometry().left() >= 0, (name, w.toolTip() or w.objectName())
    daily = plugin.docks["SatObsPlanFiles"]
    assert daily.room.width() > 200 and daily.write_button.isVisibleTo(daily)


def test_a_flow_layout_wraps_skips_hidden_widgets_and_centres_each_line():
    from qgis.PyQt.QtWidgets import QLabel, QPushButton, QWidget

    from skywatch.ui.flow_layout import FlowLayout

    host = QWidget()
    flow = FlowLayout(host, spacing=4)
    label, tall, hidden, last = QLabel("x"), QPushButton("tall"), QPushButton("hidden"), QPushButton("last")
    label.setFixedSize(40, 10)
    tall.setFixedSize(60, 30)
    hidden.setFixedSize(500, 30)
    last.setFixedSize(80, 20)
    for w in (label, tall, hidden, last):
        flow.addWidget(w)
    hidden.hide()
    host.resize(120, 200)
    host.show()
    APP.processEvents()
    # The label and the taller button on one line, each at the middle of its height; the hidden one takes no place.
    assert (label.x(), tall.x()) == (0, 44)
    assert label.y() == 10 and tall.y() == 0
    # The next does not fit beside them: on the next line.
    assert (last.x(), last.y()) == (0, 34)
    assert flow.heightForWidth(120) == 54 and flow.heightForWidth(400) == 30
    host.close()
