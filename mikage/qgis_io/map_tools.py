"""
地図のツールの受け渡し: the plugin's map tools taking the map and giving it back.

Most of the plugin's map tools turn their owner off when another tool takes
the map (位置ずらし, スワイプ比較, クイック幾何補正, クリック抽出, スペクトル,
影から高さ, the catalog's 範囲を描く and 地図で選択, 撮像計画's 地図で指定,
地点ごとの撮像予定, 関心地域, 観測記録's クリック記録 …: their button comes off).
Such a tool must never be given the map back later by another tool: its
owner is off, so the tool would go on working with its button off
(位置ずらし → クイック幾何補正 → off moved the image on a drag; tsudo
2026-11-24, 「手順を踏まないと発生しない不具合」), and it would still be the
map's tool after the plugin is unloaded.

So a tool turned off gives the map back to the person's own tool (QGIS's pan,
identify… in use before the first of the plugin's tools took the map), never
to another of the plugin's that turned off meanwhile. A tool whose owner
stays on when another takes the map (撮像依頼's form, which keeps its button
pressed) is given the map back as before (`turns_off=False`).

While a tool takes the map through `take`, the tool it replaces is told it is
deactivated; its owner turning off then gives nothing back (`give_back` does
nothing meanwhile): a tool given back in the middle of another's taking
turned that one off before it started (カタログ「地図で選択」 →
スワイプ比較 left the swipe's tool on the map with the swipe off).
"""
from __future__ import annotations

from typing import Optional

from qgis.PyQt import sip

#: The tool's Qt property: one of the plugin's (and whether its owner turns off when another takes the map).
OURS = "mikageTool"
TURNS_OFF = "mikageToolTurnsOff"

#: Per map canvas (by id): the person's own tool in use before the plugin's took the map.
_theirs: dict = {}
#: While `take` hands a canvas to a tool: the tool it replaces turning off gives nothing back.
_taking: set = set()


def is_ours(tool) -> bool:
    """Whether a map tool is one of the plugin's."""
    if tool is None or sip.isdeleted(tool):
        return False
    try:
        return bool(tool.property(OURS))
    except RuntimeError:
        return False


def _given_back_to(tool) -> bool:
    """Whether the map may be given back to this tool: the person's own, or one of the plugin's whose owner stays on."""
    if tool is None or sip.isdeleted(tool):
        return False
    try:
        return not tool.property(OURS) or not tool.property(TURNS_OFF)
    except RuntimeError:
        return False


def take(canvas, tool, turns_off: bool = True) -> None:
    """`tool` on the map (`turns_off`: its owner turns off when another takes the map); the tool it replaces kept to give back."""
    tool.setProperty(OURS, True)
    tool.setProperty(TURNS_OFF, bool(turns_off))
    current = canvas.mapTool()
    if current is tool:
        return
    if _given_back_to(current):
        _theirs[id(canvas)] = current
    _taking.add(id(canvas))
    try:
        canvas.setMapTool(tool)
    finally:
        _taking.discard(id(canvas))


def give_back(canvas, tool) -> None:
    """
    `tool` off the map, when it is the map's tool: the tool kept by `take`
    back on (never one of the plugin's that turned off), else no tool.
    Nothing while another tool is taking the map (`take`).
    """
    if canvas is None or sip.isdeleted(canvas) or id(canvas) in _taking:
        return
    current = canvas.mapTool()
    if tool is None or current is not tool:
        return
    theirs: Optional[object] = _theirs.get(id(canvas))
    if theirs is not None and theirs is not tool and _given_back_to(theirs):
        try:
            canvas.setMapTool(theirs)
            return
        except RuntimeError:  # gone with its plugin
            pass
    _theirs.pop(id(canvas), None)
    canvas.unsetMapTool(tool)


def forget(canvas, tool) -> None:
    """A tool whose owner goes away (a closed form) is never given the map back."""
    if canvas is not None and _theirs.get(id(canvas)) is tool:
        _theirs.pop(id(canvas), None)


def leave(canvas) -> None:
    """The plugin unloaded: none of its tools stays the map's tool (the person's back, else none)."""
    if canvas is None or sip.isdeleted(canvas):
        return
    current = canvas.mapTool()
    if is_ours(current):
        theirs = _theirs.get(id(canvas))
        if theirs is not None and not is_ours(theirs) and _given_back_to(theirs):
            try:
                canvas.setMapTool(theirs)
            except RuntimeError:
                canvas.unsetMapTool(current)
        else:
            canvas.unsetMapTool(current)
    _theirs.pop(id(canvas), None)
