"""
The image recorded on drawn over the other images (tsudo 2026-11-23,
操作性点検 10): with several images open, ◀ ▶, the image chooser or the layer
panel changed the image recorded on while the map still showed another one
drawn over it, so marks went on an image not seen.

`bring_up` moves the image's layers just over the highest other image that
is drawn over it where it lies (shown, its footprint overlapping); the other
images are not hidden, nothing moves when none covers it. What moves is the
branch of the layer tree that holds the image, under the group the two
share: a layer, a record's group of files, or a group of one's own with the
image in it (the image stays in its groups; 操作性点検の再検証 I4: it was
taken out of them). A node moves as QGIS moves one in the layer panel (a
copy put in place, then the node taken away: pinned_layers.arrange); with a
custom layer order (「レイヤ順序」 panel) that order is changed instead. A
pinned image (「一番上に固定」) is left above.
"""
from __future__ import annotations

from typing import Optional, Sequence

from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsLayerTreeNode, QgsMapLayer, QgsProject

from .obs_images import _box
from .pinned_layers import in_pinned


def _overlap(a: Optional[list[float]], b: Optional[list[float]]) -> bool:
    if a is None or b is None:
        return True
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def covering(layer_ids: Sequence[str], others: Sequence[str], project: Optional[QgsProject] = None) -> Optional[QgsMapLayer]:
    """The highest layer of the other images drawn over the image (shown, overlapping it), None when none is."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    order = [layer.id() for layer in root.layerOrder() if layer is not None]
    mine = [order.index(x) for x in layer_ids if x in order]
    if not mine:
        return None
    top = min(mine)
    boxes = [_box(project.mapLayer(x)) for x in layer_ids if project.mapLayer(x) is not None]
    for layer_id in order[:top]:
        if layer_id not in others or layer_id in layer_ids:
            continue
        node = root.findLayer(layer_id)
        layer = project.mapLayer(layer_id)
        if node is None or layer is None or not node.isVisible() or in_pinned(node):
            continue
        box = _box(layer)
        if any(_overlap(box, b) for b in boxes):
            return layer
    return None


def bring_up(layer_ids: Sequence[str], others: Sequence[str], project: Optional[QgsProject] = None) -> bool:
    """Moves the image's layers just over the highest other image drawn over it; True when they moved."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    over = covering(layer_ids, others, project)
    if over is None:
        return False
    order = [layer.id() for layer in root.layerOrder() if layer is not None]
    mine = sorted((x for x in layer_ids if x in order), key=order.index)
    if root.hasCustomLayerOrder():
        rest = [x for x in order if x not in mine]
        at = rest.index(over.id())
        root.setCustomLayerOrderByIds(rest[:at] + mine + rest[at:])
        return True
    target = root.findLayer(over.id())
    nodes = [node for node in (root.findLayer(x) for x in mine) if isinstance(node, QgsLayerTreeLayer)]
    if target is None or not nodes:
        return False
    common = _common_group([*nodes, target])
    above = _branch(target, common)
    if common is None or above is None:
        return False
    branches: list[QgsLayerTreeNode] = []
    for node in nodes:
        branch = _branch(node, common)
        if branch is not None and branch is not above and all(branch is not b for b in branches):
            branches.append(branch)
    if not branches:
        return False
    for branch in branches:
        copy = branch.clone()
        common.insertChildNode(common.children().index(above), copy)
        common.removeChildNode(branch)
    return True


def _common_group(nodes: Sequence[QgsLayerTreeNode]) -> Optional[QgsLayerTreeGroup]:
    """The deepest group holding all the nodes."""
    paths = []
    for node in nodes:
        path = []
        while node is not None:
            path.append(node)
            node = node.parent()
        paths.append(path[::-1])
    common = None
    for level in zip(*paths):
        if not all(x is level[0] for x in level) or not isinstance(level[0], QgsLayerTreeGroup):
            break
        common = level[0]
    return common


def _branch(node: Optional[QgsLayerTreeNode], group: Optional[QgsLayerTreeGroup]) -> Optional[QgsLayerTreeNode]:
    """The child of `group` that holds the node (the node itself when it is one)."""
    while node is not None and node.parent() is not group:
        node = node.parent()
    return node
