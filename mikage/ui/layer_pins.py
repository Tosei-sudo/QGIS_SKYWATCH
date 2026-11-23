"""
「一番上に固定」 in the layer panel (qgis_io/pinned_layers.py has the rules):
the right-click entry, the pin shown on each pinned node, QGIS's place for new
layers kept below the pinned ones, and the pinned ones moved back to the top
when something comes over them (another plugin, a drag in the layer panel).
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from qgis.core import QgsLayerTree, QgsLayerTreeNode, QgsLayerTreeRegistryBridge, QgsProject
from qgis.PyQt.QtCore import QCoreApplication, QObject, QTimer
from qgis.PyQt.QtGui import QIcon

from ..qgis_io import pinned_layers

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "pin_top.svg")
#: The layer panel's right-click entry.
ENTRY = "一番上に固定"


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


class LayerPins(QObject):
    """
    Keeps the pinned nodes of the project's layer tree at its top. `view`: the
    layer panel (QgsLayerTreeView; None: no entry, no pins shown).
    `qgis_point`: where QGIS would add a new layer now (iface.layerTreeInsertionPoint;
    None: QGIS's place is not changed, the pinned ones are moved back instead).
    """

    def __init__(self, project: Optional[QgsProject] = None, view=None,
                 qgis_point: Optional[Callable[[], object]] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.project = project or QgsProject.instance()
        self.root = self.project.layerTreeRoot()
        self.view = view
        self.qgis_point = qgis_point
        self.icon = QIcon(ICON)
        self._indicator = None
        #: The nodes showing the pin.
        self._shown: list[QgsLayerTreeNode] = []
        self._pending = False
        self._arranging = False
        self._active = True
        self._connections: list = []
        root = self.root
        self._connect(root.addedChildren, self._changed)
        self._connect(root.removedChildren, self._changed)
        self._connect(root.willRemoveChildren, self._going)
        self._connect(root.customPropertyChanged, self._property_changed)
        self._connect(root.customLayerOrderChanged, self._changed)
        self._connect(root.hasCustomLayerOrderChanged, self._changed)
        # After QGIS's own (connected before): QGIS sets its place for new layers on these.
        for signal in (root.addedChildren, root.removedChildren, root.visibilityChanged, root.nameChanged):
            self._connect(signal, self._fix_point)
        if view is not None:
            self._connect(view.contextMenuAboutToShow, self._layer_menu)
            selection = view.selectionModel()
            if selection is not None:
                self._connect(selection.currentChanged, self._fix_point)
        self.arrange()

    def _connect(self, signal, slot) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    def unload(self) -> None:
        self._active = False
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections = []
        for node in self._shown:
            self._hide(node)
        self._shown = []

    # ---- pinning -------------------------------------------------------------------------------------------

    def pin(self, nodes, on: bool) -> None:
        """Pins the nodes or takes their pins off, and puts the pinned ones in place now."""
        for node in nodes:
            pinned_layers.set_pinned(node, on)
        self.arrange()

    def _layer_menu(self, menu) -> None:
        """The layer panel's right-click: 「一番上に固定」 for the nodes selected (ticked when all are pinned)."""
        if self.view is None:
            return
        nodes = [n for n in self.view.selectedNodes(True) if n is not None]
        if not nodes:
            return
        action = QAction(self.icon, tr(ENTRY), menu)
        action.setObjectName("mikagePinTop")
        action.setCheckable(True)
        action.setChecked(all(pinned_layers.is_pinned(n) for n in nodes))
        action.setToolTip(tr("いつもレイヤーパネルの一番上に置き、地図でほかのレイヤーの上に描く。新しいレイヤーはこの下に入る"))
        action.triggered.connect(lambda on: self.pin(nodes, on))
        after = _after_moves(menu)
        if after is not None:
            menu.insertAction(after, action)
        else:
            menu.addSeparator()
            menu.addAction(action)

    # ---- keeping them on top -------------------------------------------------------------------------------

    def _changed(self, *_args) -> None:
        if not self._arranging:
            self._later()

    def _property_changed(self, _node, key: str) -> None:
        if key == pinned_layers.PINNED and not self._arranging:
            self._later()

    def _later(self) -> None:
        """Once what is changing the tree now is done (a group just added still gets its layers)."""
        if self._pending or not self._active:
            return
        self._pending = True
        QTimer.singleShot(0, self._run)

    def _run(self) -> None:
        self._pending = False
        if self._active:
            self.arrange()

    def arrange(self) -> None:
        """The pinned nodes to the top of the tree (and of the custom layer order), the pins shown, QGIS's place fixed."""
        if not self._active:
            return
        current = self._current()
        self._arranging = True
        try:
            moved = pinned_layers.arrange(self.root)
            pinned_layers.arrange_layer_order(self.root)
        finally:
            self._arranging = False
        if self.view is not None and current is not None:
            for node, copy in moved:
                if node is current:
                    self.view.setCurrentIndex(self.view.node2index(copy))
                    break
        self._show_pins()
        self._fix_point()

    def _current(self) -> Optional[QgsLayerTreeNode]:
        if self.view is None:
            return None
        try:
            return self.view.currentNode()
        except RuntimeError:
            return None

    def _fix_point(self, *_args) -> None:
        """QGIS's place for new layers: just below the pinned ones when it is above them or in a pinned group."""
        if self.qgis_point is None or not self._active or self._arranging:
            return
        bridge = self.project.layerTreeRegistryBridge()
        if bridge is None:
            return
        try:
            point = self.qgis_point()
        except (RuntimeError, AttributeError):
            return
        group, position = getattr(point, "group", None), getattr(point, "position", 0)
        fixed_group, fixed_position = pinned_layers.insertion_point(self.root, group, position)
        # Set even when QGIS's is right: one fixed before may stand from before the pins changed.
        bridge.setLayerInsertionPoint(QgsLayerTreeRegistryBridge.InsertionPoint(fixed_group, fixed_position))

    # ---- the pins shown ------------------------------------------------------------------------------------

    def _indicator_object(self):
        if self._indicator is None and self.view is not None:
            from qgis.gui import QgsLayerTreeViewIndicator

            self._indicator = QgsLayerTreeViewIndicator(self.view)
            self._indicator.setIcon(self.icon)
            self._indicator.setToolTip(tr("一番上に固定（右クリックの「一番上に固定」で外す）"))
        return self._indicator

    def _show_pins(self) -> None:
        if self.view is None:
            return
        marked = pinned_layers.marked_nodes(self.root)
        for node in [n for n in self._shown if not any(n is m for m in marked)]:
            self._hide(node)
        self._shown = [n for n in self._shown if any(n is m for m in marked)]
        indicator = self._indicator_object()
        for node in marked:
            if not any(node is n for n in self._shown):
                self.view.addIndicator(node, indicator)
                self._shown.append(node)

    def _hide(self, node) -> None:
        if self.view is None or self._indicator is None:
            return
        try:
            self.view.removeIndicator(node, self._indicator)
        except RuntimeError:
            pass

    def _going(self, parent, first: int, last: int) -> None:
        """Nodes about to leave the tree: their pins forgotten (the panel would keep showing one on whatever comes next there)."""
        going: list = []
        for child in parent.children()[first:last + 1]:
            going.append(child)
            if QgsLayerTree.isGroup(child):
                going.extend(_descendants(child))
        for node in going:
            if any(node is n for n in self._shown):
                self._hide(node)
        self._shown = [n for n in self._shown if not any(n is g for g in going)]


#: QGIS's 「一番上に移動」「一番下に移動」 of the layer panel's right-click (qgslayertreeviewdefaultactions.cpp).
_MOVES = ("Move to &Top", "Move to &Bottom")


def _after_moves(menu):
    """The entry after QGIS's 「一番下に移動」 (so 「一番上に固定」 sits with the moves), None when the menu has no such entry."""
    texts = set()
    for text in _MOVES:
        texts.add(text.replace("&", ""))
        texts.add(QCoreApplication.translate("QgsLayerTreeViewDefaultActions", text).replace("&", ""))
    actions = menu.actions()
    last = None
    for index, action in enumerate(actions):
        if action.text().replace("&", "") in texts:
            last = index
    if last is None or last + 1 >= len(actions):
        return None
    return actions[last + 1]


def _descendants(group) -> list:
    out = []
    for child in group.children():
        out.append(child)
        if QgsLayerTree.isGroup(child):
            out.extend(_descendants(child))
    return out
