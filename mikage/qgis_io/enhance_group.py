"""
画像補正 on a layer-tree group: the rasters under it (nested groups too, not
the background maps), whether they are alike (the same bands and data types,
so one band assignment and one shared range fit all), and the group's choice
for 「グループで範囲を共通にする」.

The choice is kept on the group (custom property `satobs/enhanceShare`): a
key the members' settings name while their range is shared, "off" when it
was turned off. A layer whose settings name a key leaves the shared range
when it is no longer under the group with that key (`sync_shares`).
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Optional

from qgis.core import QgsLayerTreeGroup, QgsProject, QgsRasterLayer

from . import enhance_renderer as er
from .obs_images import is_image_layer

#: The group's choice: a share key, or "off".
SHARE_PROPERTY = "satobs/enhanceShare"
OFF = "off"


def group_rasters(group: QgsLayerTreeGroup) -> list:
    """The image rasters under `group` (nested groups too, hidden ones too), top of the layer tree first."""
    out = []
    for node in group.findLayers():
        layer = node.layer()
        if isinstance(layer, QgsRasterLayer) and layer.isValid() and is_image_layer(layer):
            out.append(layer)
    return out


def layout_of(layer: QgsRasterLayer) -> tuple:
    """What has to match for one band assignment and one range: each band's data type and description."""
    provider = layer.dataProvider()
    return tuple((int(provider.dataType(b)), provider.bandDescription(b) or "") for b in range(1, layer.bandCount() + 1))


def alike(layers: list) -> bool:
    """Whether the layers have the same bands (count, data types, descriptions)."""
    return bool(layers) and len({layout_of(layer) for layer in layers}) == 1


def share_key(group: QgsLayerTreeGroup) -> str:
    """The group's share key ("" when it has none or the choice is "off")."""
    value = str(group.customProperty(SHARE_PROPERTY, "") or "")
    return "" if value == OFF else value


def share_wanted(group: QgsLayerTreeGroup, layers: list) -> bool:
    """「グループで範囲を共通にする」 as the dock shows it: the group's choice, else on when the rasters are alike."""
    if not alike(layers):
        return False
    value = str(group.customProperty(SHARE_PROPERTY, "") or "")
    return value != OFF if value else True


def set_share(group: QgsLayerTreeGroup, on: bool) -> str:
    """Keeps the choice on the group; the key the members' settings name ("" when off)."""
    if not on:
        group.setCustomProperty(SHARE_PROPERTY, OFF)
        return ""
    key = share_key(group)
    if not key or _groups_with(key, group) > 1:
        key = uuid.uuid4().hex[:12]
        group.setCustomProperty(SHARE_PROPERTY, key)
    return key


def _groups_with(key: str, group: QgsLayerTreeGroup) -> int:
    root = _root_of(group)
    return sum(1 for g in root.findGroups(True) if share_key(g) == key) if root is not None else 1


def _root_of(node) -> Optional[QgsLayerTreeGroup]:
    while node is not None and node.parent() is not None:
        node = node.parent()
    return node


def sync_shares(project: Optional[QgsProject] = None) -> list:
    """
    Layers whose settings name a share key but are no longer under the group
    with that key (moved out, the group gone) get their own range back; the
    layers changed. Of groups with the same key (a copied group) the first
    in the layer tree keeps it.
    """
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    groups: dict = {}
    for group in root.findGroups(True):
        key = share_key(group)
        if key and key not in groups:
            groups[key] = group
    changed = []
    for layer in project.mapLayers().values():
        r = er.enhance_renderer(layer) if isinstance(layer, QgsRasterLayer) else None
        key = r.settings.share if r is not None else ""
        if not key:
            continue
        group = groups.get(key)
        if group is None or group.findLayer(layer.id()) is None:
            er.apply(layer, replace(r.settings, share=""))
            changed.append(layer)
    return changed
