"""
The viewer's image correction drawn by QGIS: a raster renderer that reads the
picture bands from the provider, stretches raw values (16-bit, float) to
8 bits on their own statistics, and runs the correction (core.enhance) on
what is drawn — the canvas's pixels, never the whole image, so big COGs cost
what the screen costs (QGIS reads the overview that fits).

- DRA: the range comes from the area in view (`EnhanceSettings.view`, set
  from the map canvas; without it, from the area being drawn), read at a
  coarse size once per view and shared by every part of one drawing, so
  tiles get one range. 「範囲を固定」 keeps the last range.
- Without DRA, 16-bit and float images stretch on the whole image's 2–98 %
  (QGIS's default), 8-bit images are shown as they are.
- Sharpen reads `margin` pixels more around the area drawn, so there are no
  seams between parts.

The renderer keeps its settings in the layer's custom property
`satobs/enhance` (JSON) and writes itself into the project as QGIS's own
multiband-colour or single-band-gray renderer with the same raw stretch, so a
project opens without the plugin too; with it, `restore_layer()` puts the
correction back.
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from typing import Optional

import numpy as np
from qgis.PyQt import sip
from qgis.core import (
    Qgis, QgsColorRampShader, QgsContrastEnhancement, QgsMultiBandColorRenderer, QgsRasterBlock, QgsRasterLayer,
    QgsRasterRenderer, QgsRasterShader, QgsRectangle, QgsSingleBandGrayRenderer, QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtGui import QColor

from ..core.enhance.color_classes import class_palette, class_range
from ..core.enhance.color_scale import apply_palette, color_scale_palette
from ..core.enhance.histogram import channel_bins
from ..core.enhance.pipeline import Pipeline
from ..core.enhance.process import process
from ..core.enhance.raster import RasterHistogram, compute_raster_stretch, sample_raster_histogram, stretch_to_rgba, valid_mask
from ..core.enhance.stats import count_pixels, resolve_ops

RENDERER_TYPE = "satobsEnhance"
PROPERTY = "satobs/enhance"
#: The longest side of the coarse reads for statistics.
STATS_SIZE = 512
#: The raw stretch without DRA (QGIS's default cumulative cut).
WHOLE_STRETCH = {"lowPercent": 2, "highPercent": 2}

#: Threads for one drawing, and the fewest rows a thread gets.
THREADS = max(1, min(8, os.cpu_count() or 1))
STRIP = 128
_POOL: Optional[ThreadPoolExecutor] = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(THREADS, thread_name_prefix="satobs-enhance")
    return _POOL


EIGHT_BIT = {Qgis.DataType.Byte}
if hasattr(Qgis.DataType, "Int8"):
    EIGHT_BIT.add(Qgis.DataType.Int8)

_NUMPY = {
    Qgis.DataType.Byte: np.uint8, Qgis.DataType.UInt16: np.uint16, Qgis.DataType.Int16: np.int16,
    Qgis.DataType.UInt32: np.uint32, Qgis.DataType.Int32: np.int32,
    Qgis.DataType.Float32: np.float32, Qgis.DataType.Float64: np.float64,
}
if hasattr(Qgis.DataType, "Int8"):
    _NUMPY[Qgis.DataType.Int8] = np.int8


@dataclass(frozen=True)
class EnhanceSettings:
    """What the renderer draws; saved as JSON on the layer."""
    #: Bands shown, 1-based: one (gray) or three (R, G, B).
    bands: tuple = (1, 2, 3)
    #: The correction, `{"version": 1, "ops": [...]}`. DRA is an `autoStretch` step first.
    pipeline: dict = field(default_factory=lambda: Pipeline().to_json())
    #: Keep the DRA range when the view moves.
    locked: bool = False
    #: Values that are no data in every picture band, besides the file's own.
    no_data: tuple = ()
    #: The colour scale, the viewer's LayerColorScale: {"scale": name or stops, "reverse", "fade", "classes"}; None for none.
    color: Optional[dict] = None

    def to_json(self) -> str:
        d = asdict(self)
        d["bands"] = list(self.bands)
        d["no_data"] = list(self.no_data)
        return json.dumps(d, ensure_ascii=False)

    @staticmethod
    def from_json(text: str) -> "EnhanceSettings":
        d = json.loads(text)
        p = Pipeline.from_json(d.get("pipeline") or {"version": 1, "ops": []})
        bands = tuple(int(b) for b in d.get("bands") or (1,))
        if len(bands) not in (1, 3):
            bands = bands[:1]
        color = d.get("color")
        return EnhanceSettings(bands, p.to_json(), bool(d.get("locked", False)),
                               tuple(float(v) for v in d.get("no_data") or ()), color if isinstance(color, dict) else None)

    @property
    def correction(self) -> Pipeline:
        return Pipeline.from_json(self.pipeline)

    @property
    def classes(self) -> Optional[dict]:
        """Colour classes, when the image is shown by classes of its values (no correction then)."""
        return (self.color or {}).get("classes") or None

    @property
    def palette(self) -> Optional[np.ndarray]:
        """The 256-entry RGBA table the picture is coloured through, or None."""
        return palette_of(self.color)

    @property
    def dra(self) -> Optional[dict]:
        """The `autoStretch` step's options when DRA is on."""
        return self.correction.get("autoStretch")


_palettes: dict = {}


def palette_of(color: Optional[dict]) -> Optional[np.ndarray]:
    """The palette of a LayerColorScale (classes over their own value range), kept by its JSON."""
    if not color:
        return None
    key = json.dumps(color, sort_keys=True)
    if key not in _palettes:
        classes = color.get("classes")
        if classes:
            table = class_palette(classes, class_range(classes))
        else:
            table = color_scale_palette(color.get("scale") or "gray", reverse=bool(color.get("reverse")), fade=bool(color.get("fade")))
        if len(_palettes) > 32:
            _palettes.clear()
        _palettes[key] = table
    return _palettes[key]


class EnhanceState:
    """What the clones of one layer's renderer share: the view and the statistics."""

    def __init__(self):
        self.lock = threading.Lock()
        #: The area in view, in the layer's CRS (set from the canvas).
        self.view: Optional[QgsRectangle] = None
        #: Statistics by what they were taken for.
        self.cache: dict = {}
        #: The last DRA result, kept while locked.
        self.kept: Optional[tuple] = None
        self.generation = 0

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()
            self.generation += 1


def default_settings(layer: QgsRasterLayer) -> EnhanceSettings:
    """Bands 1, 2, 3 (or the first alone for fewer than three), no correction."""
    n = layer.bandCount()
    return EnhanceSettings(bands=(1, 2, 3) if n >= 3 else (1,))


def read_bands(provider, bands, extent: QgsRectangle, width: int, height: int, no_data=(), feedback=None) -> tuple:
    """The picture bands as H×W arrays of their own type (float64 where unreadable) and the mask of pixels that count."""
    arrays = []
    valid = np.ones((height, width), dtype=bool)
    for b in bands:
        block = provider.block(b, extent, width, height, feedback)
        dtype = _NUMPY.get(block.dataType())
        if dtype is None or block.isEmpty():
            a = np.full((height, width), np.nan)
        else:
            a = np.frombuffer(bytes(block.data()), dtype=dtype, count=width * height).reshape(height, width)
            if block.hasNoDataValue():
                valid &= a != block.noDataValue()
            if block.hasNoData() and not block.hasNoDataValue():
                # A no-data bitmap (no value to compare with): ask the block where it is.
                for y, x in zip(*np.nonzero(a == 0)):
                    if block.isNoData(int(y), int(x)):
                        valid[y, x] = False
        arrays.append(a)
    valid &= valid_mask(arrays, no_data)
    return arrays, valid


def _coarse_size(extent: QgsRectangle, layer_extent: QgsRectangle, xres: float, yres: float) -> tuple:
    """A read of `extent` at most STATS_SIZE a side and no finer than the image."""
    w = max(1, min(STATS_SIZE, int(round(extent.width() / xres)) if xres > 0 else STATS_SIZE))
    h = max(1, min(STATS_SIZE, int(round(extent.height() / yres)) if yres > 0 else STATS_SIZE))
    scale = min(STATS_SIZE / max(w, h), 1.0)
    return max(1, int(w * scale)), max(1, int(h * scale))


class EnhanceRenderer(QgsRasterRenderer):
    def __init__(self, input_=None, settings: Optional[EnhanceSettings] = None, state: Optional[EnhanceState] = None,
                 eight_bit: Optional[bool] = None):
        super().__init__(input_, RENDERER_TYPE)
        self.settings = settings or EnhanceSettings()
        self.state = state or EnhanceState()
        self._eight_bit = eight_bit

    # ---- QgsRasterRenderer ----

    def clone(self):
        r = EnhanceRenderer(self.input(), self.settings, self.state, self._eight_bit)
        r.copyCommonProperties(self, True)
        return r

    def usesBands(self):  # noqa: N802 (QGIS's name)
        return list(self.settings.bands)

    def writeXml(self, doc, parent):  # noqa: N802 (QGIS's name)
        fallback = self.fallback()
        if fallback is not None:
            fallback.writeXml(doc, parent)

    def block(self, band_no, extent, width, height, feedback=None):  # noqa: ARG002
        out = QgsRasterBlock(Qgis.DataType.ARGB32_Premultiplied, width, height)
        provider = self.input()
        if provider is None or width <= 0 or height <= 0:
            return out
        try:
            rgba = self.render(extent, width, height, feedback)
        except Exception:  # noqa: BLE001 (a drawing error must not take QGIS down; the area stays empty)
            import traceback

            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage(traceback.format_exc(), "衛星撮像計画・観測記録", Qgis.MessageLevel.Warning)
            out.setIsNoData()
            return out
        alpha = rgba[..., 3].astype(np.uint32)
        opacity = self.opacity()
        if opacity < 1:
            alpha = (alpha * opacity + 0.5).astype(np.uint32)
        # Premultiplied ARGB32 in native order (0xAARRGGBB as a 32-bit number).
        r, g, b = (rgba[..., c].astype(np.uint32) * alpha // 255 for c in range(3))
        argb = (alpha << 24) | (r << 16) | (g << 8) | b
        out.setData(np.ascontiguousarray(argb, dtype=np.uint32).tobytes())
        return out

    # ---- the picture ----

    def eight_bit(self) -> bool:
        if self._eight_bit is None:
            p = self.input()
            self._eight_bit = all(p.dataType(b) in EIGHT_BIT for b in self.settings.bands)
        return self._eight_bit

    def render(self, extent: QgsRectangle, width: int, height: int, feedback=None) -> np.ndarray:
        """The corrected H×W×4 uint8 RGBA of `extent`."""
        s = self.settings
        correction = Pipeline() if s.classes else s.correction
        m = correction.margin
        read_extent, rw, rh = extent, width, height
        if m:
            px = extent.width() / width
            py = extent.height() / height
            read_extent = QgsRectangle(extent.xMinimum() - m * px, extent.yMinimum() - m * py,
                                       extent.xMaximum() + m * px, extent.yMaximum() + m * py)
            rw, rh = width + 2 * m, height + 2 * m
        bands, valid = read_bands(self.input(), s.bands, read_extent, rw, rh, s.no_data, feedback)
        black, white, ops = self.ranges(extent, correction)
        mode = "gray" if len(s.bands) == 1 else "rgb"
        palette = s.palette

        def rows(y0: int, y1: int) -> np.ndarray:
            """Output rows y0..y1 (of the area read), from those rows and `m` more each side."""
            a0, a1 = max(0, y0 - m), min(rh, y1 + m)
            rgba = stretch_to_rgba([b[a0:a1] for b in bands], valid[a0:a1], black, white)
            out = process(rgba, ops, mode=mode)[y0 - a0:y1 - a0]
            return out if palette is None else apply_palette(out, palette)

        # Strips in threads (NumPy lets go of the GIL); with `m` rows of margin each is what the whole would give.
        parts = min(THREADS, max(1, height // STRIP))
        cuts = [m + round(i * height / parts) for i in range(parts + 1)]
        if parts == 1:
            out = rows(cuts[0], cuts[1])
        else:
            out = np.vstack(list(_pool().map(lambda i: rows(cuts[i], cuts[i + 1]), range(parts))))
        if m:
            out = out[:, m:m + width]
        return np.ascontiguousarray(out)

    def ranges(self, extent: QgsRectangle, correction: Pipeline) -> tuple:
        """The raw black and white per band, and the steps with DRA resolved."""
        s = self.settings
        if s.classes:
            # Classes take the values as read, stretched over the classes' own range (no correction).
            lo, hi = class_range(s.classes)
            return [lo] * len(s.bands), [hi] * len(s.bands), []
        n = len(s.bands)
        dra = correction.get("autoStretch")
        ops = list(correction.ops)
        if self.eight_bit():
            black, white = [0.0] * n, [255.0] * n
            if dra:
                hist = self._view_stats(extent, eight=True)
                ops = resolve_ops(ops, hist)
            return black, white, ops
        if dra:
            stretch = self._view_stats(extent, eight=False, options=dra)
            ops = [op for op in ops if op["op"] != "autoStretch"]
        else:
            stretch = self._whole_stretch()
        if stretch is None:
            return [0.0] * n, [1.0] * n, ops
        return stretch["black"], stretch["white"], ops

    def _key(self) -> tuple:
        return (tuple(self.settings.bands), tuple(self.settings.no_data))

    def _coarse(self, area: QgsRectangle):
        p = self.input()
        layer_extent = p.extent()
        area = area.intersect(layer_extent)
        if area.isEmpty():
            return None
        xres = layer_extent.width() / max(1, p.xSize()) if p.capabilities() & p.Size else 0
        yres = layer_extent.height() / max(1, p.ySize()) if p.capabilities() & p.Size else 0
        w, h = _coarse_size(area, layer_extent, xres, yres)
        return read_bands(p, self.settings.bands, area, w, h, self.settings.no_data)

    def _whole_stretch(self) -> Optional[dict]:
        key = ("whole",) + self._key()
        st = self.state
        with st.lock:
            if key not in st.cache:
                read = self._coarse(self.input().extent())
                stats = sample_raster_histogram(*read) if read else None
                st.cache[key] = compute_raster_stretch(stats, WHOLE_STRETCH) if stats else None
            return st.cache[key]

    def _view_stats(self, extent: QgsRectangle, eight: bool, options: Optional[dict] = None):
        """The DRA statistics of the view: a histogram of 8-bit codes, or the raw stretch."""
        st = self.state
        area = st.view if st.view is not None and not st.view.isEmpty() else extent
        opts = json.dumps(options, sort_keys=True) if options else ""
        key = ("view", eight, opts, area.toString(6)) + self._key()
        with st.lock:
            if self.settings.locked and st.kept is not None and st.kept[0] == (eight, opts) + self._key():
                return st.kept[1]
            if key not in st.cache:
                read = self._coarse(area)
                if read is None:
                    result = None
                elif eight:
                    bands, valid = read
                    rgba = stretch_to_rgba(bands, valid, [0.0] * len(bands), [255.0] * len(bands))
                    result = count_pixels(rgba, "gray" if len(bands) == 1 else "rgb")
                else:
                    stats = sample_raster_histogram(*read)
                    result = compute_raster_stretch(stats, options) if stats else None
                # Views come and go; keep only the latest few.
                for old in [k for k in st.cache if k[0] == "view"][:-8]:
                    del st.cache[old]
                st.cache[key] = result
            st.kept = ((eight, opts) + self._key(), st.cache[key])
            return st.cache[key]

    # ---- in the project ----

    def legendSymbologyItems(self):  # noqa: N802 (QGIS's name)
        """The classes, for the layer's legend (a smooth scale has none, as QGIS's own renderers in the layer tree)."""
        classes = self.settings.classes
        if not classes:
            return []
        return [(c.get("label") or str(c["value"]), QColor(*_rgba_of(c["color"])))
                for c in sorted(classes["classes"], key=lambda c: c["value"])]

    def _pseudo_color(self, stretch: Optional[dict]) -> QgsSingleBandPseudoColorRenderer:
        """QGIS's single-band pseudo-colour renderer with the classes or the scale (over the raw stretch, without the correction)."""
        s = self.settings
        classes = s.classes
        shader = QgsColorRampShader()
        if classes:
            items = sorted(classes["classes"], key=lambda c: c["value"])
            exact = classes["kind"] == "exact"
            shader.setColorRampType(QgsColorRampShader.Type.Exact if exact else QgsColorRampShader.Type.Discrete)
            ramp = [QgsColorRampShader.ColorRampItem(float("inf") if not exact and i == len(items) - 1 else c["value"],
                                                     QColor(*_rgba_of(c["color"])), c.get("label") or str(c["value"]))
                    for i, c in enumerate(items)]
            lo, hi = class_range(classes)
        else:
            lo, hi = (stretch["black"][0], stretch["white"][0]) if stretch else (0.0, 255.0)
            palette = s.palette
            shader.setColorRampType(QgsColorRampShader.Type.Interpolated)
            # Every 8th code of the palette (the fade and the reverse come with it).
            ramp = [QgsColorRampShader.ColorRampItem(lo + (hi - lo) * v / 255, QColor(*(int(x) for x in palette[v])), "")
                    for v in list(range(0, 256, 8)) + [255]]
        shader.setColorRampItemList(ramp)
        shader.setMinimumValue(lo)
        shader.setMaximumValue(hi)
        raster_shader = QgsRasterShader(lo, hi)
        raster_shader.setRasterShaderFunction(shader)
        return QgsSingleBandPseudoColorRenderer(self.input(), s.bands[0], raster_shader)

    def fallback(self) -> Optional[QgsRasterRenderer]:
        """QGIS's own renderer with the same raw stretch (for the project file, and QGIS without the plugin)."""
        p = self.input()
        if p is None:
            return None
        s = self.settings
        stretch = None
        if not self.eight_bit():
            try:
                stretch = self._whole_stretch()
            except Exception:  # noqa: BLE001 (no statistics: QGIS's own min and max)
                stretch = None

        def enhancement(i: int, band: int) -> QgsContrastEnhancement:
            ce = QgsContrastEnhancement(p.dataType(band))
            ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum)
            if stretch:
                ce.setMinimumValue(stretch["black"][i])
                ce.setMaximumValue(stretch["white"][i])
            else:
                ce.setMinimumValue(0)
                ce.setMaximumValue(255)
            return ce

        if len(s.bands) == 1 and s.color:
            r = self._pseudo_color(stretch)
        elif len(s.bands) == 1:
            r = QgsSingleBandGrayRenderer(p, s.bands[0])
            r.setContrastEnhancement(enhancement(0, s.bands[0]))
        else:
            r = QgsMultiBandColorRenderer(p, *s.bands)
            r.setRedContrastEnhancement(enhancement(0, s.bands[0]))
            r.setGreenContrastEnhancement(enhancement(1, s.bands[1]))
            r.setBlueContrastEnhancement(enhancement(2, s.bands[2]))
        r.copyCommonProperties(self, True)
        return r


def _rgba_of(color) -> tuple:
    c = [int(round(float(x))) for x in list(color)[:4]]
    return tuple(c + [0] * (3 - len(c)) + ([255] if len(c) < 4 else []))[:4]


def enhance_renderer(layer: QgsRasterLayer) -> Optional[EnhanceRenderer]:
    r = layer.renderer()
    return r if isinstance(r, EnhanceRenderer) else None


#: Renderers put on layers. A layer made in C++ (opened from a project or by QGIS) has a Python wrapper only
#: while Python holds it, and the renderer handed to it lives only as long as that wrapper: without this, the
#: Python half of the renderer goes and QGIS draws the layer with the plain base renderer.
_kept: set = set()


def _keep(renderer: EnhanceRenderer) -> None:
    for r in [r for r in _kept if sip.isdeleted(r)]:
        _kept.discard(r)
    _kept.add(renderer)


def apply(layer: QgsRasterLayer, settings: EnhanceSettings) -> EnhanceRenderer:
    """Draws the layer with the correction (or updates it) and keeps the settings on the layer."""
    current = enhance_renderer(layer)
    if current is not None:
        bands_changed = current.settings.bands != settings.bands or current.settings.no_data != settings.no_data
        current.settings = settings
        if bands_changed:
            current._eight_bit = None
            current.state.clear()
        renderer = current
    else:
        renderer = EnhanceRenderer(layer.dataProvider(), settings)
        old = layer.renderer()
        if old is not None:
            renderer.copyCommonProperties(old, True)
        layer.setRenderer(renderer)
        _keep(renderer)
    layer.setCustomProperty(PROPERTY, settings.to_json())
    layer.triggerRepaint()
    return renderer


def remove(layer: QgsRasterLayer) -> None:
    """Back to QGIS's own renderer (the same bands and raw stretch)."""
    current = enhance_renderer(layer)
    if current is not None:
        fallback = current.fallback()
        if fallback is not None:
            layer.setRenderer(fallback)
        _kept.discard(current)
    layer.removeCustomProperty(PROPERTY)
    layer.triggerRepaint()


def restore_layer(layer) -> bool:
    """Puts the correction kept on a layer (opened from a project) back; True when it did."""
    if not isinstance(layer, QgsRasterLayer) or enhance_renderer(layer) is not None:
        return False
    text = layer.customProperty(PROPERTY)
    if not text:
        return False
    try:
        settings = EnhanceSettings.from_json(str(text))
    except (ValueError, TypeError):
        return False
    if any(b < 1 or b > layer.bandCount() for b in settings.bands):
        return False
    apply(layer, settings)
    return True


def view_histograms(provider, settings: EnhanceSettings, state: EnhanceState, view: QgsRectangle) -> dict:
    """
    Counts (256 per channel, one for gray) of the area in view before and
    after the correction (and the colour scale, counted as the image's
    colour mode, as the viewer counts what it shows), read at most
    STATS_SIZE a side. `provider` must be the caller's own (a clone, off the
    main thread).
    """
    r = EnhanceRenderer(provider, settings, state)
    read = r._coarse(view)
    if read is None:
        return {"before": [], "after": []}
    bands, valid = read
    black, white, ops = r.ranges(view.intersect(provider.extent()), settings.correction)
    gray = len(settings.bands) == 1
    before = stretch_to_rgba(bands, valid, black, white)
    after = process(before, ops, mode="gray" if gray else "rgb")
    palette = settings.palette
    if palette is None:
        return {"before": channel_bins(before, gray), "after": channel_bins(after, gray)}
    shown = count_pixels(apply_palette(after, palette), "gray" if gray else "rgb").bins
    return {"before": channel_bins(before, gray), "after": [b.astype(np.int64) for b in shown]}


def value_histogram(provider, settings: EnhanceSettings) -> Optional[RasterHistogram]:
    """
    The values of the whole image's first picture band (read at most
    STATS_SIZE a side), as the viewer's getValueHistogram: the raw values of
    16-bit and float images, the codes 0..255 of 8-bit ones. For classes;
    `provider` must be the caller's own (a clone, off the main thread).
    """
    one = replace(settings, bands=settings.bands[:1], color=None)
    r = EnhanceRenderer(provider, one, EnhanceState())
    read = r._coarse(provider.extent())
    if read is None:
        return None
    bands, valid = read
    if r.eight_bit():
        codes = np.clip(np.asarray(bands[0])[valid].astype(np.int64), 0, 255)
        return RasterHistogram((0.0, 255.0), [np.bincount(codes, minlength=256).astype(np.float64)], int(codes.size)) \
            if codes.size else None
    return sample_raster_histogram(bands, valid)


def set_view(layer: QgsRasterLayer, view: Optional[QgsRectangle]) -> None:
    """The area in view, in the layer's CRS, for DRA."""
    r = enhance_renderer(layer)
    if r is not None:
        r.state.view = QgsRectangle(view) if view is not None else None


def with_pipeline(settings: EnhanceSettings, pipeline: Pipeline) -> EnhanceSettings:
    return replace(settings, pipeline=pipeline.to_json())
