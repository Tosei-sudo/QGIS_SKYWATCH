"""A stand-in for QGIS's interface (just what the plugin uses), for qgis.testing tests."""
from qgis.gui import QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtCore import QEventLoop, QObject, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QMainWindow


class FakeIface(QObject):
    currentLayerChanged = pyqtSignal(object)
    newProjectCreated = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.resize(800, 600)
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar(self.window)
        self.menus: list = []
        self._menu_actions: list = []
        self.docks: list = []
        self.layer = None
        #: Custom layer actions: (action, layer type) and the layers each was put on.
        self.layer_actions: list = []
        self.layer_action_layers: dict = {}
        self.window.show()

    def mainWindow(self):  # noqa: N802
        return self.window

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return self.bar

    def activeLayer(self):  # noqa: N802
        return self.layer

    def setActiveLayer(self, layer):  # noqa: N802
        self.layer = layer
        self.currentLayerChanged.emit(layer)

    def addToolBar(self, name):  # noqa: N802
        return self.window.addToolBar(name)

    def addDockWidget(self, area, dock):  # noqa: N802
        self.window.addDockWidget(area, dock)
        self.docks.append(dock)

    def removeDockWidget(self, dock):  # noqa: N802
        self.window.removeDockWidget(dock)
        self.docks.remove(dock)

    def addPluginToMenu(self, menu, action):  # noqa: N802
        self.menus.append((menu, action.text()))
        self._menu_actions.append(action)

    def removePluginMenu(self, menu, action):  # noqa: N802
        # By the action, as QGIS does (its text may have changed, as 「通知（n）」 does).
        i = self._menu_actions.index(action)
        del self.menus[i], self._menu_actions[i]

    def addCustomActionForLayerType(self, action, menu, layer_type, all_layers):  # noqa: N802
        self.layer_actions.append((action, layer_type))

    def addCustomActionForLayer(self, action, layer):  # noqa: N802
        self.layer_action_layers.setdefault(id(action), []).append(layer)

    def removeCustomActionForLayerType(self, action):  # noqa: N802
        self.layer_actions = [(a, t) for a, t in self.layer_actions if a is not action]
        self.layer_action_layers.pop(id(action), None)
        return True


def wait_for(condition, ms=5000):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start(20)
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    timer.stop()
    return condition()
