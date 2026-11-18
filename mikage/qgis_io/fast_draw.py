"""
MIKAGE の速い描画（試作）. tsudo, 2026-11-18, chosen after 「地図の描画を速く
する設定」 (qgis_io/raster_speed.py) showed that most of a 16-bit image's
drawing is QGIS's own colouring (≈ 90 of ≈ 120 ms for a 1700×1073 map): QGIS
colours pixel by pixel in C++ (QgsMultiBandColorRenderer /
QgsSingleBandGrayRenderer ::block), MIKAGE looks every pixel up in a table of
every value of the band's type (NumPy, in threads): 127 → 46 ms a map.

The layer keeps QGIS's renderer as far as anyone can tell: FastMultiBand and
FastGray *are* QgsMultiBandColorRenderer and QgsSingleBandGrayRenderer (Python
subclasses), so the layer's symbology page, saving to the project (their own
writeXml: a project opens without the plugin as it always did), the legend
and the min/max updates are QGIS's. Only `block()` is MIKAGE's, and it gives
the very pixels QGIS's would (tests/test_qgis_fast_draw.py compares them
byte for byte), as QGIS 3.34's code does it (qgsmultibandcolorrenderer.cpp,
qgssinglebandgrayrenderer.cpp, qgscontrastenhancement*.cpp), down to its
quirks: the multiband renderer asks all three enhancements whether the *red*
value is displayable; a value is cut to its low 8 bits (qRgba), so a clipped
green or blue value (-1) is 255; once a layer has a transparency (an empty
one, left by its properties dialog) the opacity is an int of 255.

What MIKAGE does not draw itself goes to QGIS's own block() (the C++ base):
bands that are not 8- or 16-bit integers (floats, 32-bit), a user-defined
enhancement, a min equal to the max, transparent pixel lists, an enhancement
whose own answers differ from the table at a few values (`agrees`: one made
for another type than the band's, or another QGIS's formulas), or any error.

Turned on by the person (「地図の描画の速さ」, kept in the profile): every
raster layer drawn by those two renderers gets the fast one, those added
later too, and again after its style is changed (the symbology page gives
the layer a new QGIS renderer). Off, or the plugin unloaded: QGIS's own
renderer with the same settings back on each.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.core import (
    Qgis, QgsContrastEnhancement, QgsMapLayer, QgsMultiBandColorRenderer, QgsProject, QgsRasterBlock, QgsRasterLayer,
    QgsSingleBandGrayRenderer,
)

from . import settings

KEY = "rasterSpeed/fastDraw"
CE = QgsContrastEnhancement.ContrastEnhancementAlgorithm
#: The integer types QGIS colours through a table of every value (≤ 16 bits): their NumPy type and range.
TYPES = {
    Qgis.DataType.Byte: (np.uint8, 0, 255),
    Qgis.DataType.UInt16: (np.uint16, 0, 65535),
    Qgis.DataType.Int16: (np.int16, -32768, 32767),
}
if hasattr(Qgis.DataType, "Int8"):
    TYPES[Qgis.DataType.Int8] = (np.int8, -128, 127)
#: qgsDoubleNear's default epsilon.
EPSILON = 4 * np.finfo(np.float64).eps
OPAQUE = np.uint32(0xFF000000)


def on() -> bool:
    return settings._get(KEY) == "1"


def set_on(value: bool) -> None:
    settings._set(KEY, "1" if value else "0")


# ---- the tables: what QGIS's contrast enhancement gives each value of a type -------------------------------

_tables: dict = {}
_tables_lock = threading.Lock()
TABLES_KEPT = 64
_MISSING = object()


def _cached(key, make):
    with _tables_lock:
        found = _tables.get(key, _MISSING)
    if found is not _MISSING:
        return found
    value = make()
    with _tables_lock:
        if len(_tables) >= TABLES_KEPT:
            _tables.clear()
        _tables[key] = value
    return value


def ce_key(ce, data_type) -> Optional[tuple]:
    if ce is None:
        return None
    return int(data_type), int(ce.contrastEnhancementAlgorithm()), ce.minimumValue(), ce.maximumValue()


def _enhanced(v: np.ndarray, algorithm, mn: float, mx: float, data_type) -> Optional[tuple]:
    """QGIS's enhanceContrast and isValueInDisplayableRange for the values `v` (float64): (int64, bool), None when not MIKAGE's."""
    lo, hi = TYPES[data_type][1:]
    if algorithm == CE.NoEnhancement or algorithm == CE.ClipToMinimumMaximum:
        # QgsContrastEnhancementFunction::enhance (and QgsClipToMinMaxEnhancement's inside min..max).
        if data_type == Qgis.DataType.Byte:
            enhanced = v.astype(np.int64)
        else:
            enhanced = (((v - lo) / (hi - lo)) * 255.0).astype(np.int64)
    elif algorithm in (CE.StretchToMinimumMaximum, CE.StretchAndClipToMinimumMaximum):
        if mx - mn == 0:
            return None  # QGIS divides by zero there
        enhanced = np.clip((((v - mn) / (mx - mn)) * 255.0).astype(np.int64), 0, 255)
    else:
        return None  # user defined: QGIS's own
    if algorithm in (CE.StretchAndClipToMinimumMaximum, CE.ClipToMinimumMaximum):
        displayable = ~((v < mn) | (v > mx))
        enhanced = np.where(displayable, enhanced, -1)
    else:
        displayable = (v >= lo) & (v <= hi)
    return enhanced, displayable


def enhancement_table(ce: QgsContrastEnhancement, data_type) -> Optional[tuple]:
    """
    (enhanced int32, displayable bool or None when every value is) for every value of the band's type, lowest first:
    what `ce.enhanceContrast` and `ce.isValueInDisplayableRange` give; None when QGIS's own drawing has to do it.
    """
    def make():
        lo, hi = TYPES[data_type][1:]
        found = _enhanced(np.arange(lo, hi + 1, dtype=np.float64), ce.contrastEnhancementAlgorithm(), ce.minimumValue(),
                          ce.maximumValue(), data_type)
        if found is None:
            return None
        enhanced, displayable = found
        return enhanced.astype(np.int32), (None if displayable.all() else displayable)

    table = _cached(ce_key(ce, data_type), make)
    if table is not None and not agrees(ce, data_type, table):
        return None
    return table


def probes(ce, data_type) -> list:
    """Values where a table could go wrong: the type's ends, the min and max and their neighbours, a few between."""
    lo, hi = TYPES[data_type][1:]
    points = {lo, hi, 0, 1, (lo + hi) // 2}
    for x in (ce.minimumValue(), ce.maximumValue()):
        for d in (-1, 0, 1):
            points.add(int(np.floor(x)) + d)
            points.add(int(np.ceil(x)) + d)
    points.update(int(x) for x in np.linspace(lo, hi, 9))
    return sorted(p for p in points if lo <= p <= hi)


def agrees(ce, data_type, table) -> bool:
    """
    Whether QGIS's own enhancement gives the table's values at the probes: a band of another type than the
    enhancement's (QGIS keeps its type out of reach) or another QGIS's formulas leave the drawing to QGIS.
    """
    lo = TYPES[data_type][1]
    enhanced, displayable = table
    for p in probes(ce, data_type):
        if ce.enhanceContrast(float(p)) != int(enhanced[p - lo]):
            return False
        if ce.isValueInDisplayableRange(float(p)) != (True if displayable is None else bool(displayable[p - lo])):
            return False
    return True


# ---- reading ---------------------------------------------------------------------------------------------

class Unsupported(Exception):
    """QGIS's own drawing has to do it."""


#: The most pixels asked one by one whether they are in a no-data bitmap (beyond: QGIS's own drawing).
BITMAP_ASKS = 50_000


def band_type(provider, band: int):
    """The band's type as its provider gives it; Unsupported when MIKAGE does not draw it (floats, 32 bits)."""
    data_type = provider.dataType(band)
    if data_type not in TYPES:
        raise Unsupported()
    return data_type


def read(provider, band: int, data_type, extent, width: int, height: int, feedback) -> tuple:
    """A band as QGIS's renderer reads it: (values H·W of its type, no data H·W bool or None); Unsupported otherwise."""
    block = provider.block(band, extent, width, height, feedback)
    if block is None or block.isEmpty() or block.dataType() != data_type:
        raise Unsupported()
    values = np.frombuffer(bytes(block.data()), dtype=TYPES[data_type][0], count=width * height)
    if block.hasNoDataValue():
        no_data = values == block.noDataValue()
        if not no_data.any():
            no_data = None
    elif block.hasNoData():
        # A no-data bitmap (an area off the image asked for, user no-data ranges; not when QGIS draws a map, which
        # asks for the image's part of it): its pixels hold 0, so only those are asked, up to a bound.
        zeros = np.flatnonzero(values == 0)
        if len(zeros) > BITMAP_ASKS:
            raise Unsupported()
        no_data = np.zeros(width * height, dtype=bool)
        for i in zeros:
            if block.isNoData(int(i)):
                no_data[i] = True
    else:
        no_data = None
    return values, no_data


def index(values: np.ndarray, data_type) -> np.ndarray:
    """Positions in a table of every value of the type (lowest first)."""
    if TYPES[data_type][1] == 0:
        return values
    # Two's complement: the signed value plus 2^(n-1) is the unsigned view with its top bit flipped.
    unsigned = values.view(np.uint16 if values.dtype == np.int16 else np.uint8)
    return unsigned ^ (unsigned.dtype.type(1) << (8 * unsigned.dtype.itemsize - 1))


def low_byte(values) -> np.ndarray:
    """qRgba's `& 0xff` of an int (two's complement for a negative one)."""
    return np.asarray(values).astype(np.int64) & 0xFF


def near_one(x) -> np.ndarray:
    """qgsDoubleNear(x, 1.0)."""
    d = np.asarray(x, dtype=np.float64) - 1.0
    return (d > -EPSILON) & (d <= EPSILON)


def pack(r, g, b, opacity) -> np.ndarray:
    """qRgba(r, g, b, 255) where the opacity is (near) 1, else qRgba(o·r, o·g, o·b, o·255) truncated (premultiplied)."""
    r, g, b = np.asarray(r), np.asarray(g), np.asarray(b)
    o = np.asarray(opacity, dtype=np.float64)
    plain = OPAQUE | (low_byte(r) << 16) | (low_byte(g) << 8) | low_byte(b)
    scaled = ((low_byte(np.trunc(o * 255.0)) << 24) | (low_byte(np.trunc(o * r)) << 16)
              | (low_byte(np.trunc(o * g)) << 8) | low_byte(np.trunc(o * b)))
    return np.where(near_one(o), plain, scaled).astype(np.uint32)


def channel_table(enhanced: Optional[np.ndarray], data_type, opacity: float, shift: int, alpha: bool) -> np.ndarray:
    """One band's part of the packed pixel for every value of its type (a constant opacity): ORed with the others'."""
    lo, hi = TYPES[data_type][1:]
    values = enhanced.astype(np.int64) if enhanced is not None else np.arange(lo, hi + 1, dtype=np.int64)
    if near_one(opacity):
        part = low_byte(values) << shift
        if alpha:
            part = part | 0xFF000000
    else:
        part = low_byte(np.trunc(float(opacity) * values)) << shift
        if alpha:
            part = part | (int(low_byte(np.trunc(float(opacity) * 255.0))) << 24)
    return part.astype(np.uint32)


# ---- drawing ---------------------------------------------------------------------------------------------

#: Threads for one drawing's table lookups, and the fewest pixels a thread gets.
THREADS = max(1, min(8, os.cpu_count() or 1))
PART = 1 << 17
_POOL: Optional[ThreadPoolExecutor] = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(THREADS, thread_name_prefix="mikage-fastdraw")
    return _POOL


def in_parts(n: int, fill) -> None:
    """fill(start, end) over 0..n, in threads when it is worth it (NumPy lets go of Python while it looks up)."""
    parts = min(THREADS, max(1, n // PART))
    if parts == 1:
        fill(0, n)
        return
    edges = np.linspace(0, n, parts + 1).astype(int)
    for future in [_pool().submit(fill, int(s), int(e)) for s, e in zip(edges[:-1], edges[1:])]:
        future.result()


def _output(argb: np.ndarray, width: int, height: int) -> QgsRasterBlock:
    out = QgsRasterBlock(Qgis.DataType.ARGB32_Premultiplied, width, height)
    out.setData(argb.tobytes())
    return out


def opacity_of(renderer) -> float:
    """
    The opacity QGIS colours with: its transparency's `alphaValue(…, mOpacity * 255) / 255.0` once the layer has one
    (an empty one, from the layer's properties), which gives back an int; transparent pixel lists are QGIS's own.
    """
    transparency = renderer.rasterTransparency()
    if transparency is None:
        return renderer.opacity()
    if not transparency.isEmpty():
        raise Unsupported()
    return int(renderer.opacity() * 255) / 255.0


def _table(ce, data_type) -> Optional[tuple]:
    if ce is None:
        return None
    table = enhancement_table(ce, data_type)
    if table is None:
        raise Unsupported()
    return table


def multiband_argb(renderer, extent, width: int, height: int, feedback=None) -> np.ndarray:
    """QgsMultiBandColorRenderer::block's pixels (premultiplied ARGB32, H·W uint32); Unsupported when QGIS's own has to."""
    opacity = opacity_of(renderer)
    provider = renderer.input()
    n = width * height
    bands = [renderer.redBand(), renderer.greenBand(), renderer.blueBand()]
    if all(band <= 0 for band in bands):
        raise Unsupported()  # QGIS draws nothing
    ces = [renderer.redContrastEnhancement(), renderer.greenContrastEnhancement(), renderer.blueContrastEnhancement()]
    alpha_band = renderer.alphaBand()
    # An unset band's value is 0, of the others' type (its enhancement, if any, is checked for that type by the probes).
    known = [band_type(provider, band) for band in bands if band > 0]
    types = [band_type(provider, band) if band > 0 else known[0] for band in bands]
    # QGIS's short way (no transparency, three bands, no enhancement, or 8 bits not enhanced): the values as they are.
    if all(band > 0 for band in bands) and alpha_band <= 0 and not (renderer.usesTransparency()):
        if all(ty == Qgis.DataType.Byte for ty in types):
            plain = all(ce is None or ce.contrastEnhancementAlgorithm() == CE.NoEnhancement for ce in ces)
        else:
            plain = all(ce is None for ce in ces)
        if plain:
            ces, opacity = [None, None, None], 1.0
    tables = [_table(ce, ty) for ce, ty in zip(ces, types)]
    if any(t is not None and ty != types[0] for t, ty in zip(tables, types)):
        raise Unsupported()  # enhancements of another type asked about the red value
    reads = {}
    for band in bands + [alpha_band]:
        if band > 0 and band not in reads:
            reads[band] = read(provider, band, band_type(provider, band), extent, width, height, feedback)
    values = [reads[band][0] if band > 0 else np.zeros(n, dtype=TYPES[ty][0]) for band, ty in zip(bands, types)]
    no_data = [reads[b][1] for b in sorted(set(b for b in bands if b > 0)) if reads[b][1] is not None]
    # Shown only where every enhancement finds the *red* value displayable (QGIS asks them all about red).
    shown = [t[1] for t in tables if t is not None and t[1] is not None]
    hide = None
    if shown:
        hide = _cached(("hide", int(types[0]), tuple(ce_key(ce, ty) for ce, ty in zip(ces, types))),
                       lambda: ~np.logical_and.reduce(shown))
    color = np.uint32(renderer.renderColorForNodataPixel() & 0xFFFFFFFF)
    alpha = reads[alpha_band][0] if alpha_band > 0 else None
    indexes = [index(v, ty) for v, ty in zip(values, types)]
    argb = np.empty(n, dtype=np.uint32)
    if alpha is None:
        key = ("rgb", tuple((ce_key(ce, ty), int(ty)) for ce, ty in zip(ces, types)), opacity)
        parts = _cached(key, lambda: [channel_table(None if t is None else t[0], ty, opacity, 16 - 8 * c, c == 0)
                                      for c, (t, ty) in enumerate(zip(tables, types))])

        def fill(s, e):
            out = parts[0][indexes[0][s:e]]
            out |= parts[1][indexes[1][s:e]]
            out |= parts[2][indexes[2][s:e]]
            if hide is not None:
                out[hide[indexes[0][s:e]]] = color
            for mask in no_data:
                out[mask[s:e]] = color
            argb[s:e] = out
    else:
        def fill(s, e):
            channels = [t[0][i[s:e]] if t is not None else v[s:e] for t, i, v in zip(tables, indexes, values)]
            a = alpha[s:e].astype(np.float64)
            out = pack(channels[0], channels[1], channels[2], opacity * (a / 255.0))
            out[a == 0] = color
            if hide is not None:
                out[hide[indexes[0][s:e]]] = color
            for mask in no_data:
                out[mask[s:e]] = color
            argb[s:e] = out
    in_parts(n, fill)
    return argb


def gray_argb(renderer, extent, width: int, height: int, feedback=None) -> np.ndarray:
    """QgsSingleBandGrayRenderer::block's pixels (premultiplied ARGB32, H·W uint32); Unsupported when QGIS's own has to."""
    opacity = opacity_of(renderer)
    provider = renderer.input()
    n = width * height
    band = renderer.grayBand()
    data_type = band_type(provider, band)
    ce = renderer.contrastEnhancement()
    table = _table(ce, data_type)
    values, no_data = read(provider, band, data_type, extent, width, height, feedback)
    alpha_band = renderer.alphaBand()
    alpha = None
    if alpha_band > 0:
        alpha = values if alpha_band == band else read(provider, alpha_band, band_type(provider, alpha_band), extent, width,
                                                         height, feedback)[0]
    white_to_black = renderer.gradient() == QgsSingleBandGrayRenderer.Gradient.WhiteToBlack
    color = np.uint32(renderer.renderColorForNodataPixel() & 0xFFFFFFFF)
    i = index(values, data_type)

    def grays():
        lo, hi = TYPES[data_type][1:]
        gray = table[0].astype(np.int64) if table is not None else np.arange(lo, hi + 1, dtype=np.int64)
        return 255 - gray if white_to_black else gray

    argb = np.empty(n, dtype=np.uint32)
    if alpha is None:
        # One table from the value straight to the pixel, the values not displayable included.
        def make():
            out = pack(*(grays(),) * 3, opacity)
            if table is not None and table[1] is not None:
                out[~table[1]] = color
            return out

        pixels = _cached(("gray", ce_key(ce, data_type), int(data_type), white_to_black, opacity, int(color)), make)

        def fill(s, e):
            out = pixels[i[s:e]]
            if no_data is not None:
                out[no_data[s:e]] = color
            argb[s:e] = out
    else:
        gray_of = _cached(("grays", ce_key(ce, data_type), int(data_type), white_to_black), grays)

        def fill(s, e):
            a = alpha[s:e].astype(np.float64)
            out = pack(*(gray_of[i[s:e]],) * 3, opacity * (a / 255.0))
            out[a == 0] = color
            if table is not None and table[1] is not None:
                out[~table[1][i[s:e]]] = color
            if no_data is not None:
                out[no_data[s:e]] = color
            argb[s:e] = out
    in_parts(n, fill)
    return argb


# ---- the renderers ---------------------------------------------------------------------------------------

def _copy_enhancement(ce) -> Optional[QgsContrastEnhancement]:
    return QgsContrastEnhancement(ce) if ce is not None else None


def multiband_copy(source, cls=QgsMultiBandColorRenderer):
    """`source`'s settings in a new renderer of `cls` (QGIS's own or FastMultiBand)."""
    r = cls(source.input(), source.redBand(), source.greenBand(), source.blueBand())
    r.setRedContrastEnhancement(_copy_enhancement(source.redContrastEnhancement()))
    r.setGreenContrastEnhancement(_copy_enhancement(source.greenContrastEnhancement()))
    r.setBlueContrastEnhancement(_copy_enhancement(source.blueContrastEnhancement()))
    r.copyCommonProperties(source, True)
    return r


def gray_copy(source, cls=QgsSingleBandGrayRenderer):
    r = cls(source.input(), source.grayBand())
    r.setContrastEnhancement(_copy_enhancement(source.contrastEnhancement()))
    r.setGradient(source.gradient())
    try:
        r.setLegendSettings(type(source.legendSettings())(source.legendSettings()))
    except (AttributeError, TypeError):
        pass
    r.copyCommonProperties(source, True)
    return r


LOG_TAG = "MIKAGE"
#: Errors already in the log (a drawing has many parts, and every view draws again: each said once).
_told: set = set()


def _drawn(renderer, base, argb_of, band_no, extent, width, height, feedback):
    """MIKAGE's pixels, else QGIS's own block() (the C++ base) for what MIKAGE leaves to it."""
    if width > 0 and height > 0 and renderer.input() is not None:
        try:
            return _output(argb_of(renderer, extent, width, height, feedback), width, height)
        except Unsupported:
            pass
        except Exception as e:  # noqa: BLE001 (QGIS's own drawing instead of an empty area)
            said = f"{type(e).__name__}: {e}"
            if said not in _told:
                _told.add(said)
                import traceback

                from qgis.core import QgsMessageLog
                QgsMessageLog.logMessage("MIKAGE の速い描画: QGIS の描画に切り替え\n" + traceback.format_exc(), LOG_TAG,
                                         Qgis.MessageLevel.Warning, notifyUser=False)
    return base.block(renderer, band_no, extent, width, height, feedback)


class FastMultiBand(QgsMultiBandColorRenderer):
    """QGIS's multiband colour renderer, drawn by MIKAGE (multiband_argb)."""

    def clone(self):
        return multiband_copy(self, FastMultiBand)

    def block(self, band_no, extent, width, height, feedback=None):
        return _drawn(self, QgsMultiBandColorRenderer, multiband_argb, band_no, extent, width, height, feedback)


class FastGray(QgsSingleBandGrayRenderer):
    """QGIS's single band gray renderer, drawn by MIKAGE (gray_argb)."""

    def clone(self):
        return gray_copy(self, FastGray)

    def block(self, band_no, extent, width, height, feedback=None):
        return _drawn(self, QgsSingleBandGrayRenderer, gray_argb, band_no, extent, width, height, feedback)


#: Renderers put on layers (a C++-made layer's renderer has its Python half only while Python holds it; see enhance_renderer._kept).
_kept: set = set()


def _keep(renderer) -> None:
    for r in [r for r in _kept if sip.isdeleted(r)]:
        _kept.discard(r)
    _kept.add(renderer)


def fast(layer) -> bool:
    return isinstance(layer, QgsRasterLayer) and isinstance(layer.renderer(), (FastMultiBand, FastGray))


def drawable(renderer) -> bool:
    """Whether MIKAGE colours what the renderer shows: its bands of 8 or 16 bits."""
    provider = renderer.input()
    if provider is None:
        return False
    if isinstance(renderer, QgsMultiBandColorRenderer):
        bands = [renderer.redBand(), renderer.greenBand(), renderer.blueBand()]
    else:
        bands = [renderer.grayBand()]
    if all(band <= 0 for band in bands):
        return False
    return all(provider.dataType(band) in TYPES for band in bands + [renderer.alphaBand()] if band > 0)


def wrap(layer) -> bool:
    """
    MIKAGE draws the layer (QGIS's multiband colour or single band gray renderer, its settings kept); whether it
    does now. Not redrawn: the pixels are the same.
    """
    if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
        return False
    r = layer.renderer()
    if isinstance(r, (FastMultiBand, FastGray)):
        _keep(r)  # (one put there by other code, a copy of another layer's)
        return True
    if r is None or type(r) not in (QgsMultiBandColorRenderer, QgsSingleBandGrayRenderer) or not drawable(r):
        return False  # other renderers (画像補正's, colour ramps, palettes) and floats: as they are
    new = multiband_copy(r, FastMultiBand) if type(r) is QgsMultiBandColorRenderer else gray_copy(r, FastGray)
    layer.setRenderer(new)
    _keep(new)
    return True


def unwrap(layer) -> bool:
    """QGIS's own renderer back with the same settings; whether it was MIKAGE's."""
    if not isinstance(layer, QgsRasterLayer):
        return False
    r = layer.renderer()
    if isinstance(r, FastMultiBand):
        layer.setRenderer(multiband_copy(r))
    elif isinstance(r, FastGray):
        layer.setRenderer(gray_copy(r))
    else:
        return False
    _kept.discard(r)
    return True


class FastDraw(QObject):
    """Keeps the project's raster layers drawn by MIKAGE while it is on: those added, and those whose style changed."""

    def __init__(self, parent=None, project: Optional[QgsProject] = None):
        super().__init__(parent)
        self.project = project or QgsProject.instance()
        self.running = False
        self._watched: dict = {}

    def start(self) -> int:
        """On: every raster layer now and later; how many are drawn by MIKAGE."""
        if not self.running:
            self.running = True
            self.project.layersAdded.connect(self._added)
        self._added(list(self.project.mapLayers().values()))
        return self.count()

    def stop(self, put_back: bool = True) -> None:
        if self.running:
            self.running = False
            try:
                self.project.layersAdded.disconnect(self._added)
            except (TypeError, RuntimeError):
                pass
        for layer_id, (layer, slot) in list(self._watched.items()):
            if not sip.isdeleted(layer):
                try:
                    layer.rendererChanged.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
                if put_back:
                    unwrap(layer)
        self._watched.clear()

    def count(self) -> int:
        return sum(1 for layer in self.project.mapLayers().values() if fast(layer))

    def _added(self, layers) -> None:
        for layer in layers:
            if not isinstance(layer, QgsRasterLayer) or layer.type() != QgsMapLayer.LayerType.RasterLayer:
                continue
            if layer.id() not in self._watched:
                def changed(layer=layer) -> None:
                    # A new renderer from the symbology page, a pasted style…: MIKAGE's again a moment later
                    # (not inside QGIS's own setRenderer).
                    QTimer.singleShot(0, lambda: self._again(layer))

                layer.rendererChanged.connect(changed)
                layer.willBeDeleted.connect(lambda layer_id=layer.id(): self._watched.pop(layer_id, None))
                self._watched[layer.id()] = (layer, changed)
            wrap(layer)

    def _again(self, layer) -> None:
        if self.running and not sip.isdeleted(layer):
            wrap(layer)
