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
