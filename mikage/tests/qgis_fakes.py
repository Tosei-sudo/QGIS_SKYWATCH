"""A stand-in for QGIS's interface (just what the plugin uses), for qgis.testing tests."""
from qgis.gui import QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtCore import QEventLoop, QObject, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QFrame, QGridLayout, QMainWindow, QMenu, QStackedWidget, QStatusBar, QWidget

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction


class FakeIface(QObject):
    currentLayerChanged = pyqtSignal(object)
    newProjectCreated = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.window = QMainWindow()
        # As QGIS 3.34 builds its map area: a widget whose grid has the message bar's frame over a stack
        # of pages (the map canvas, the welcome page shown with no project open).
        central = QWidget()
        central.setObjectName("centralwidget")
        grid = QGridLayout(central)
        grid.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.canvas = QgsMapCanvas()
        self.canvas.resize(800, 600)
        self.stack.addWidget(self.canvas)
        self.welcome = QWidget()
        self.stack.addWidget(self.welcome)
        self.bar_frame = QFrame()
        self.bar_frame.setFixedHeight(0)
        grid.addWidget(self.bar_frame, 0, 0, 1, 1)
        grid.addWidget(self.stack, 0, 0, 2, 1)
        self.window.setCentralWidget(central)
        # The size the window had before the map area was built so (tests measure on the map in pixels).
        self.window.resize(200, 100)
        self.bar = QgsMessageBar(self.window)
        self.status = FakeStatusBar()
        self.window.setStatusBar(self.status)
        self.menus: list = []
        self._menu_actions: list = []
        self.docks: list = []
        self.layer = None
        #: Custom layer actions: (action, layer type) and the layers each was put on.
        self.layer_actions: list = []
        self.layer_action_layers: dict = {}
        #: Layer ▸ Create Layer, with QGIS's scratch layer item and one after it.
        self.new_layer_menu = QMenu(self.window)
        for name, text in (("mActionNewGeoPackageLayer", "新規 GeoPackage レイヤー…"), ("mActionNewMemoryLayer", "新規一時スクラッチレイヤー…"),
                           ("mActionNewVirtualLayer", "新規仮想レイヤー…")):
            action = QAction(text, self.window)
            action.setObjectName(name)
            self.new_layer_menu.addAction(action)
        self.add_feature = QAction("地物を追加", self.window)
        self.window.show()
        # As QGIS's layer list: the active layer removed, another (here none) is the active one.
        from qgis.core import QgsProject

        QgsProject.instance().layersWillBeRemoved.connect(self._layers_going)

    def _layers_going(self, layer_ids) -> None:
        layer = self.layer
        if layer is None:
            return
        try:
            gone = layer.id() in layer_ids
        except RuntimeError:  # deleted already
            gone = True
        if gone:
            self.layer = None
            self.currentLayerChanged.emit(None)

    def mainWindow(self):  # noqa: N802
        return self.window

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return self.bar

    def statusBarIface(self):  # noqa: N802
        return self.status

    def activeLayer(self):  # noqa: N802
        return self.layer

    def setActiveLayer(self, layer):  # noqa: N802
        self.layer = layer
        self.currentLayerChanged.emit(layer)

    def newLayerMenu(self):  # noqa: N802
        return self.new_layer_menu

    def newProject(self, promptToSaveFlag=False):  # noqa: N802,N803 (QGIS's names)
        """As QGIS: the project cleared (no question asked here), then newProjectCreated."""
        from qgis.core import QgsProject

        QgsProject.instance().clear()
        self.newProjectCreated.emit()
        return True

    def addProject(self, path):  # noqa: N802
        from qgis.core import QgsProject

        return QgsProject.instance().read(path)

    def actionSaveProject(self):  # noqa: N802
        """Saves the project to its file (a project without one stays unsaved, as when QGIS's file dialog is cancelled)."""
        if not hasattr(self, "_save_project"):
            from qgis.core import QgsProject

            self._save_project = QAction("プロジェクトを保存", self.window)
            self._save_project.triggered.connect(lambda: QgsProject.instance().write() if QgsProject.instance().fileName() else None)
        return self._save_project

    def actionAddFeature(self):  # noqa: N802
        return self.add_feature

    def addToolBar(self, name):  # noqa: N802
        return self.window.addToolBar(name)

    def addDockWidget(self, area, dock):  # noqa: N802
        self.window.addDockWidget(area, dock)
        # As QGIS does (QgisApp::addDockWidget shows the dock).
        dock.show()
        self.docks.append(dock)

    def removeDockWidget(self, dock):  # noqa: N802
        self.window.removeDockWidget(dock)
        self.docks.remove(dock)

    def addPluginToMenu(self, menu, action):  # noqa: N802
        self.menus.append((menu, action.text()))
        self._menu_actions.append(action)

    def menu_tree(self) -> list:
        """The plugin's menu as shown: its own submenus as (title, [their entries]), a separator as "-"."""
        def entry(action):
            if action.isSeparator():
                return "-"
            if action.menu() is not None and action.menu().objectName().startswith("SatObsMenu"):
                return (action.text(), [entry(a) for a in action.menu().actions()])
            return action.text()

        return [entry(a) for a in self._menu_actions]

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


class FakeStatusBar(QStatusBar):
    """QgsStatusBar's calls the plugin makes (addPermanentWidget with an anchor, showMessage)."""

    def addPermanentWidget(self, widget, stretch=0, anchor=None):  # noqa: N802
        super().addPermanentWidget(widget, stretch)


def wait_for(condition, ms=15000):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start(20)
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    timer.stop()
    return condition()
