"""
「一番上に固定」 (docs/features/map.md): layer-tree nodes, layers or groups,
that always stay at the top of the layer tree, so they are drawn over
everything else.

The mark is a custom property of the node, so the project file keeps it.
Pinned nodes stay at the top of the tree's root in the order they have in the
tree; a pinned layer inside a group comes out of it. What is added goes just
below them:

- MIKAGE's own additions at the top of the tree: at `top_index(root)` (not 0);
- QGIS's (above the layer selected in the layer panel): the same, and just
  below the pinned ones when the selected node is pinned or in a pinned group
  (`insertion_point`, ui/layer_pins.py sets it);
- anything else (another plugin, a drag in the layer panel): `arrange()` moves
  the pinned ones back to the top a moment later (ui/layer_pins.py).

With a custom layer order (QGIS's Layer Order panel), the pinned ones' layers
also come first there (`arrange_layer_order`).
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsLayerTree, QgsLayerTreeGroup, QgsLayerTreeNode

#: The node's custom property: true while pinned.
PINNED = "mikage/pinned"


def is_pinned(node: Optional[QgsLayerTreeNode]) -> bool:
    """Whether the node itself is pinned (a project file read back may give the mark as text)."""
    if node is None:
        return False
    value = node.customProperty(PINNED, False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def set_pinned(node: QgsLayerTreeNode, on: bool) -> None:
    """Pins the node or takes the pin off (it moves when `arrange` runs next)."""
    if on:
        if not is_pinned(node):
            node.setCustomProperty(PINNED, True)
    elif node.customProperty(PINNED) is not None:
        node.removeCustomProperty(PINNED)


def in_pinned(node: Optional[QgsLayerTreeNode]) -> bool:
    """Whether the node is pinned or inside a pinned group."""
    while node is not None:
        if is_pinned(node):
            return True
        node = node.parent()
    return False


def pinned_nodes(root: QgsLayerTreeGroup) -> list[QgsLayerTreeNode]:
    """The pinned nodes in the order of the tree (those inside a pinned group go with it, so they are not listed)."""
    out: list[QgsLayerTreeNode] = []

    def walk(group: QgsLayerTreeGroup) -> None:
        for child in group.children():
            if is_pinned(child):
                out.append(child)
            elif QgsLayerTree.isGroup(child):
                walk(child)

    walk(root)
    return out


def marked_nodes(root: QgsLayerTreeGroup) -> list[QgsLayerTreeNode]:
    """Every node with the pin mark, those inside a pinned group too (the layer panel shows the pin on each)."""
    out: list[QgsLayerTreeNode] = []

    def walk(group: QgsLayerTreeGroup) -> None:
        for child in group.children():
            if is_pinned(child):
                out.append(child)
            if QgsLayerTree.isGroup(child):
                walk(child)

    walk(root)
    return out


def top_index(root: QgsLayerTreeGroup) -> int:
    """Where something added at the top of the tree goes: just below the last pinned node of the root (0 with none)."""
    top = 0
    for index, child in enumerate(root.children()):
        if is_pinned(child):
            top = index + 1
    return top


def arrange(root: QgsLayerTreeGroup) -> list[tuple[QgsLayerTreeNode, QgsLayerTreeNode]]:
    """
    Moves the pinned nodes to the top of the root, in their order in the tree:
    the (node, its copy) of each moved. A node moves as QGIS moves one in the
    layer panel: a copy put in place, then the node taken away (a node taken
    out of the tree first would take its layers out of the project).
    """
    moved = []
    for index, node in enumerate(pinned_nodes(root)):
        children = root.children()
        if index < len(children) and children[index] is node:
            continue
        copy = node.clone()
        root.insertChildNode(index, copy)
        parent = node.parent()
        if parent is not None:
            parent.removeChildNode(node)
        moved.append((node, copy))
    return moved


def pinned_layer_ids(root: QgsLayerTreeGroup) -> list[str]:
    """The pinned nodes' layers (a pinned group's all), top first."""
    out: list[str] = []
    for node in pinned_nodes(root):
        if QgsLayerTree.isLayer(node):
            out.append(node.layerId())
        else:
            out.extend(child.layerId() for child in node.findLayers())
    return list(dict.fromkeys(out))


def arrange_layer_order(root: QgsLayerTreeGroup) -> bool:
    """With a custom layer order (the Layer Order panel): the pinned layers first there, in the tree's order. Whether it changed."""
    if not root.hasCustomLayerOrder():
        return False
    order = [layer for layer in root.customLayerOrder() if layer is not None]
    pinned = pinned_layer_ids(root)
    if not pinned:
        return False
    by_id = {layer.id(): layer for layer in order}
    first = [by_id[i] for i in pinned if i in by_id]
    wanted = first + [layer for layer in order if layer.id() not in pinned]
    if [layer.id() for layer in wanted] == [layer.id() for layer in order]:
        return False
    root.setCustomLayerOrder(wanted)
    return True


def insertion_point(root: QgsLayerTreeGroup, group: Optional[QgsLayerTreeGroup], position: int) -> tuple[QgsLayerTreeGroup, int]:
    """
    Where QGIS adds new layers, given where it would (above the selected
    layer, at the top of a selected group): just below the pinned ones when
    that is above them, among them, or in a pinned group; as it is otherwise.
    """
    if group is None:
        return root, top_index(root)
    if group is root:
        return root, max(position, top_index(root))
    if in_pinned(group):
        return root, top_index(root)
    return group, position
