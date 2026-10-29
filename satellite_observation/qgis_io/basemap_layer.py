"""
A base map (背景地図) as a QGIS layer at the bottom of the project. One at a
time: switching replaces the layer the plugin added (marked with a custom
property), choosing none removes it. The layer is opened in a task (a style
JSON and the server information are read there), so the map never waits.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from qgis.core import QgsMapLayer, QgsProject, QgsRasterLayer, QgsVectorTileLayer
from qgis.PyQt.QtCore import QCoreApplication

from ..core.basemaps import BaseMapConfig, attribution_text, raster_source, resolve_url, style_tiles, vector_tile_source
from . import settings
from .network import get_bytes
from .tasks import run_task

#: The custom property marking the base map layer (its value: the base map's id).
PROPERTY = "satellite_observation/base_map"


def _json(url: str, authcfg: Optional[str]) -> dict:
    try:
        value = json.loads(get_bytes(url, authcfg).decode("utf-8-sig"))
    except ValueError as error:
        raise ValueError(f"JSON として読み込み不可（{url.split('?', 1)[0]}）") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON のオブジェクトでなし（{url.split('?', 1)[0]}）")
    return value


def open_base_map(base: BaseMapConfig, config_source: str) -> tuple[QgsMapLayer, list[str]]:
    """The layer of a base map (made in the calling thread, moved to the main thread) and notes on what QGIS does differently."""
    url = resolve_url(base.url, config_source)
    authcfg = settings.auth_for(url.rstrip("/"))
    notes: list[str] = []
    if base.type == "xyz":
        layer: QgsMapLayer = QgsRasterLayer(raster_source(base, url, authcfg), base.label, "wms")
    elif base.type == "mvt":
        layer = QgsVectorTileLayer(vector_tile_source(base, url, authcfg), base.label)
    elif base.type == "style":
        style = _json(url, authcfg)
        tiles = style_tiles(style, url)
        if tiles is None:
            raise ValueError("スタイルにベクタータイルのソースなし")
        if "pmtiles" in tiles:
            raise ValueError(f"PMTiles（{tiles['pmtiles'].split('?', 1)[0]}）は QGIS 3.34 で読み込み不可")
        if "tilejson" in tiles:
            info = _json(tiles["tilejson"], authcfg)
            found = info.get("tiles")
            if not isinstance(found, list) or not found or not isinstance(found[0], str):
                raise ValueError("TileJSON にタイルの URL なし")
            tiles = {"tiles": found[0], "minzoom": int(info.get("minzoom", 0)), "maxzoom": int(info.get("maxzoom", 14))}
        layer = QgsVectorTileLayer(vector_tile_source(base, tiles["tiles"], authcfg, url, tiles["minzoom"], tiles["maxzoom"]), base.label)
    else:
        style = resolve_url(base.style, config_source) if base.style else ""
        layer = QgsVectorTileLayer(vector_tile_source(base, url, authcfg, style), base.label)
        if base.token and not authcfg:
            notes.append("設定ファイルの token は使わず（「設定…」でこのサービスの認証設定を選択）")
    if not layer.isValid():
        error = layer.error().summary() if not layer.error().isEmpty() else ""
        raise ValueError(f"開けず{'：' + error if error else ''}")
    if base.type in ("style", "esri"):
        # The style (and its icons) read here, in the task: the server's own, or the one given.
        message, ok = layer.loadDefaultStyle()
        if not ok:
            notes.append(f"スタイルを読めず、QGIS の既定の色で表示（{message}）" if message else "スタイルを読めず、QGIS の既定の色で表示")
    if base.attributions:
        text = attribution_text(base.attributions)
        if hasattr(layer, "serverProperties") and hasattr(layer.serverProperties(), "setAttribution"):
            layer.serverProperties().setAttribution(text)
        else:
            layer.setAttribution(text)
    layer.setCustomProperty(PROPERTY, base.id)
    layer.moveToThread(QCoreApplication.instance().thread())
    return layer, notes


def base_map_layers(project: Optional[QgsProject] = None) -> list[QgsMapLayer]:
    """The base map layers the plugin added to the project."""
    project = project or QgsProject.instance()
    return [layer for layer in project.mapLayers().values() if layer.customProperty(PROPERTY)]


def current_base_map(project: Optional[QgsProject] = None) -> str:
    """The id of the base map shown ("" for none)."""
    layers = base_map_layers(project)
    return str(layers[0].customProperty(PROPERTY)) if layers else ""


def put_base_map(layer: Optional[QgsMapLayer], project: Optional[QgsProject] = None) -> None:
    """Replaces the base map with `layer` (at the bottom of the layer list); None removes it."""
    project = project or QgsProject.instance()
    old = [x.id() for x in base_map_layers(project)]
    if old:
        project.removeMapLayers(old)
    if layer is None:
        return
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    root.insertLayer(len(root.children()), layer)


def switch_base_map(base: Optional[BaseMapConfig], config_source: str, finished: Callable[[str, list[str]], None]):
    """
    Shows `base` (None: no base map). `finished(id, problems)` hears what is
    shown afterwards and what went wrong; the task is returned (None when there was none to run).
    """
    if base is None:
        put_base_map(None)
        finished("", [])
        return None

    def done(result) -> None:
        layer, notes = result
        put_base_map(layer)
        finished(base.id, [f"{base.label}: {n}" for n in notes])

    return run_task(f"背景地図「{base.label}」の読み込み", lambda task: open_base_map(base, config_source), done,
                    lambda message: finished(current_base_map(), [f"背景地図「{base.label}」: {message}"]))
