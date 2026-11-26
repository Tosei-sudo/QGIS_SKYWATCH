"""
クイックパンシャープン (tsudo, 2026-11-23): two images chosen in the layer
panel, and the multispectral one is drawn pan-sharpened where it shows: what
the map draws, at the map's pixels, each time it draws. No file is made (for
a file there is the Processing algorithm 「パンシャープン」).

- Which is the pan: the one with fewer value bands (an alpha band is not
  counted: some pans carry one), else the finer pixel (core.enhance.pick_pan).
- The multispectral layer is drawn by 画像補正's renderer (turned on with the
  bands it showed when it was not on); the renderer state's `pan` is the link
  here, whose `sharpened()` gives the picture bands: the multispectral image
  read with bilinear resampling (from a provider of its own, so the layer's
  settings stay as they are) and the pan read at the same pixels
  (reprojected when its CRS differs; its alpha band, if any, as a mask), put
  together by core.enhance.pan_sharpen with one model (weights and gains)
  fitted once over the overlap at the pan's resolution, as the Processing
  algorithm fits it. The fit runs as a task, the image drawn as it is until
  it is ready; one model for every drawing keeps the colours as the map
  moves, and the parts of a drawing meet without seams.
- Zoomed out to the multispectral image's own pixel or coarser, and off the
  pan, the image is drawn as it is (the pan adds nothing there).
- Where the pan has no value, the multispectral image shows as it is.
- The pan layer is hidden while it is used, and shown again when the mode
  ends (when it was shown before). The link is kept on the multispectral
  layer (`mikage/quickPan`, JSON), so a project opens with it.
- It ends when either layer goes, when the multispectral layer is drawn by
  another renderer (画像補正's 補正 turned off, QGIS's own style chosen), or
  when it is turned off. A renderer this put on is taken off again when it
  ends, if the correction was not changed meanwhile.
- 位置ずらし (qgis_io.shift_io) moves the two layers together (ui/shift.py:
  the other one follows), so the detail stays on the picture (tsudo
  2026-11-27: 「クイックパンシャープンした状態で位置ずらしがうまくできない」:
  the move put QGIS's own renderer on for a moment, which ended the link, the
  pan came back, and the image kept being read from where it was). A move is
  not an end; once done, the link reads from the new sources; the model is
  kept when the two are where they were to each other, else fitted again
  (the image drawn with the model before until then).
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Optional

import numpy as np
from qgis.core import (
    QgsCoordinateTransform, QgsMultiBandColorRenderer, QgsProject, QgsRasterDataProvider, QgsRasterLayer, QgsRasterProjector,
    QgsRectangle, QgsSingleBandGrayRenderer,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..core.enhance.api import METHODS, PanSharpenModel, fit_model, pan_sharpen, pick_pan
from ..core.errors import Coded
from . import enhance_renderer as er
from . import fast_draw
from . import shift_io
from .pansharpen_io import Overlap, sample_windows
from .tasks import run_task

PROPERTY = "mikage/quickPan"
#: GDAL's (and QGIS's) colour interpretation of an alpha band.
ALPHA_BAND = 6
#: The fit takes the whole overlap at the pan's resolution up to this many pixels, else windows spread over it.
FIT_WHOLE = 1024 * 1024
FIT_COUNT = 4
FIT_SIZE = 256
#: The 2 時期 red/cyan pair's property (ui/colorize_box.py): such an image is shown in one colour, not sharpened.
PAIR_PROPERTY = "satobs/pair"
TASK = "クイックパンシャープンの準備"


class QuickPanError(Coded):
    """A reason the two layers cannot be put together (shown as it is)."""


def is_alpha(provider, band: int) -> bool:
    try:
        return int(provider.colorInterpretation(band)) == ALPHA_BAND
    except (TypeError, ValueError):
        return False


def value_bands(provider) -> list:
    """The bands (1-based) that hold values: all but an alpha band."""
    return [b for b in range(1, provider.bandCount() + 1) if not is_alpha(provider, b)]


def _in(layer: QgsRasterLayer, crs) -> QgsRectangle:
    """The layer's extent in `crs` (QuickPanError when it cannot be transformed)."""
    if layer.crs() == crs:
        return QgsRectangle(layer.extent())
    try:
        out = QgsCoordinateTransform(layer.crs(), crs, QgsProject.instance()).transformBoundingBox(layer.extent())
    except Exception as error:  # noqa: BLE001 (QgsCsException and friends)
        raise QuickPanError("座標系の変換に失敗", code="E508") from error
    if out.isEmpty() or not out.isFinite():
        raise QuickPanError("座標系の変換に失敗", code="E508")
    return out


def ground_pixel(layer: QgsRasterLayer, crs) -> float:
    """The side of the layer's pixel in `crs`'s units (the square root of its area)."""
    extent = _in(layer, crs)
    w, h = layer.width(), layer.height()
    if w <= 0 or h <= 0:
        return 0.0
    return float((extent.width() / w * extent.height() / h) ** 0.5)


def choose(first, second) -> tuple:
    """(pan, multispectral) of the two layers; QuickPanError with the reason when they cannot be put together."""
    for layer in (first, second):
        if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
            raise QuickPanError("ラスター画像を 2 つ選択")
    if first.id() == second.id():
        raise QuickPanError("別々の 2 つの画像を選択")
    crs = first.crs()
    got = pick_pan((len(value_bands(first.dataProvider())), ground_pixel(first, crs)),
                   (len(value_bands(second.dataProvider())), ground_pixel(second, crs)))
    if isinstance(got, str):
        raise QuickPanError(got)
    pan, ms = (first, second) if got == 0 else (second, first)
    if ms.customProperty(PAIR_PROPERTY):
        raise QuickPanError("2 時期の色分け重ね合わせ中の画像は対象外（重ね合わせを解除してから）")
    if _in(pan, ms.crs()).intersect(ms.extent()).isEmpty():
        raise QuickPanError("2 つの画像の重なりなし")
    return pan, ms


def initial_settings(layer: QgsRasterLayer) -> er.EnhanceSettings:
    """画像補正 for a layer that had none: the bands it showed (QGIS's own renderer), no correction."""
    r = layer.renderer()
    if isinstance(r, QgsMultiBandColorRenderer) and min(r.redBand(), r.greenBand(), r.blueBand()) > 0:
        return er.EnhanceSettings(bands=(r.redBand(), r.greenBand(), r.blueBand()))
    if isinstance(r, QgsSingleBandGrayRenderer) and r.grayBand() > 0:
        return er.EnhanceSettings(bands=(r.grayBand(),))
    return er.default_settings(layer)


def plain_copy(renderer):
    """A copy of a layer's renderer to put back later: QGIS's own type for MIKAGE's fast drawing (which wraps it again)."""
    if isinstance(renderer, fast_draw.FastMultiBand):
        return fast_draw.multiband_copy(renderer)
    if isinstance(renderer, fast_draw.FastGray):
        return fast_draw.gray_copy(renderer)
    return renderer.clone()


def _values(block, width: int, height: int) -> tuple:
    """A block's values (H×W, its own type) and where they count; (None, None) when it has none."""
    dtype = er.numpy_type(block.dataType())
    if dtype is None or block.isEmpty():
        return None, None
    a = np.frombuffer(bytes(block.data()), dtype=dtype, count=width * height).reshape(height, width)
    valid = np.isfinite(a) if a.dtype.kind == "f" else np.ones(a.shape, dtype=bool)
    if block.hasNoDataValue():
        valid &= a != block.noDataValue()
    elif block.hasNoData():
        # A no-data bitmap (off the image): those pixels are 0; a real 0 of a pan is taken as no value too (fast).
        valid &= a != 0
    return a, valid


class PanLink:
    """A multispectral layer drawn with a pan's detail: the providers it reads, the model, how strong."""

    def __init__(self, ms: QgsRasterLayer, pan: QgsRasterLayer, method: str = "gram-schmidt", strength: float = 1.0):
        if method not in METHODS:
            raise QuickPanError(f"方式 {method} なし")
        self.ms_id = ms.id()
        self.pan_id = pan.id()
        self.method = method
        self.strength = float(strength)
        #: The weights and gains, once fitted (None: the image is drawn as it is).
        self.model: Optional[PanSharpenModel] = None
        #: Why the fit failed ("" when it did not).
        self.failed = ""
        self.fitting = False
        #: Where the multispectral image is from the pan (map units, its CRS): now, and when the model was fitted.
        self.relative = (0.0, 0.0)
        self.model_relative: Optional[tuple] = None
        #: The renderer this put on the layer and the one it had, while the correction is as put (taken off at the end).
        self.before: Optional[tuple] = None
        self.lock = threading.Lock()
        self._generation = 0
        self._task = None
        #: (layer, signal, slot) connected for this link.
        self.connections: list = []
        self.take(ms, pan)

    def take(self, ms: QgsRasterLayer, pan: QgsRasterLayer) -> None:
        """The providers read from, made again from the layers' sources now."""
        ms_provider = ms.dataProvider().clone()
        hint = QgsRasterDataProvider.ProviderCapability.ProviderHintCanPerformProviderResampling
        if ms_provider.providerCapabilities() & hint:
            # Bilinear when zoomed in, as pan-sharpening wants it (the layer itself keeps its own resampling).
            ms_provider.enableProviderResampling(True)
            ms_provider.setZoomedInResamplingMethod(QgsRasterDataProvider.ResamplingMethod.Bilinear)
        pan_provider = pan.dataProvider().clone()
        reader = pan_provider
        projector = None
        if pan.crs() != ms.crs():
            projector = QgsRasterProjector()
            projector.setInput(pan_provider)
            projector.setCrs(pan.crs(), ms.crs(), QgsProject.instance().transformContext())
            reader = projector
        pan_bands = value_bands(pan_provider)
        ms_bands = value_bands(ms_provider)
        if not pan_bands or not ms_bands:
            raise QuickPanError("値のバンドのない画像あり")
        pan_extent = _in(pan, ms.crs())
        with self.lock:
            self._ms = ms_provider
            self._pan_provider = pan_provider
            self._projector = projector
            self._pan = reader
            self.ms_bands = ms_bands
            self.pan_band = pan_bands[0]
            self.pan_alpha = next((b for b in range(1, pan_provider.bandCount() + 1) if is_alpha(pan_provider, b)), None)
            #: The multispectral image's own pixel and the pan's extent and pixel, in the multispectral CRS.
            self.ms_pixel = (ms.extent().width() / max(1, ms.width()), ms.extent().height() / max(1, ms.height()))
            self.pan_extent = pan_extent
            self.pan_pixel = (pan_extent.width() / max(1, pan.width()), pan_extent.height() / max(1, pan.height()))
            ms_extent = ms.extent()
            self.relative = (ms_extent.xMinimum() - pan_extent.xMinimum(), ms_extent.yMaximum() - pan_extent.yMaximum())

    def as_fitted(self) -> bool:
        """Whether the model is there and the two images are where they were to each other when it was fitted."""
        if self.model is None or self.model_relative is None:
            return False
        tolerance = 1e-6 * max(self.pan_pixel[0], self.pan_pixel[1], 1e-12)
        return all(abs(a - b) <= tolerance for a, b in zip(self.relative, self.model_relative))

    # ---- drawing ----

    def sharpens(self, extent: QgsRectangle, width: int, height: int) -> bool:
        """Whether a drawing of `extent` at `width` × `height` gets the pan's detail (finer than the image, on the pan)."""
        if self.model is None or width <= 0 or height <= 0:
            return False
        if extent.width() / width >= self.ms_pixel[0] * 0.999 and extent.height() / height >= self.ms_pixel[1] * 0.999:
            return False
        return extent.intersects(self.pan_extent)

    def _read_pan(self, extent: QgsRectangle, width: int, height: int, feedback=None) -> tuple:
        pan, valid = _values(self._pan.block(self.pan_band, extent, width, height, feedback), width, height)
        if pan is not None and self.pan_alpha is not None:
            alpha, _ = _values(self._pan.block(self.pan_alpha, extent, width, height, feedback), width, height)
            if alpha is not None:
                valid &= alpha != 0
        return pan, valid

    def _read(self, extent: QgsRectangle, width: int, height: int, no_data=(), feedback=None) -> Optional[tuple]:
        """(multispectral B×H×W, where it counts, pan H×W, where both count), or None (no pan here)."""
        with self.lock:
            ms, ms_valid = er.read_bands(self._ms, self.ms_bands, extent, width, height, no_data, feedback)
            if er.canceled(feedback):
                return None
            pan, pan_valid = self._read_pan(extent, width, height, feedback)
        if pan is None or not pan_valid.any():
            return None
        return np.stack(ms), ms_valid, pan, ms_valid & pan_valid

    def sharpened(self, bands, extent: QgsRectangle, width: int, height: int, no_data=(), feedback=None) -> Optional[tuple]:
        """
        The picture bands `bands` (1-based) of `extent` with the pan's detail
        and the mask of pixels that count, as `enhance_renderer.read_bands`
        gives them; None when they are drawn as they are (see `sharpens`).
        """
        model = self.model
        if model is None or any(b not in self.ms_bands for b in bands) or not self.sharpens(extent, width, height):
            return None
        read = self._read(extent, width, height, no_data, feedback)
        if read is None or er.canceled(feedback):
            return None
        ms, ms_valid, pan, both = read
        order = list(range(len(self.ms_bands)))
        out = np.empty_like(ms)
        method, strength = self.method, self.strength

        def strip(y0: int, y1: int) -> None:
            sharp, _no_data, _ = pan_sharpen(pan[y0:y1], ms[:, y0:y1], method, "auto", order, strength, model)
            out[:, y0:y1] = np.where(both[y0:y1], sharp, ms[:, y0:y1])

        er.in_strips(height, strip, feedback)
        if er.canceled(feedback):
            return None
        return [out[self.ms_bands.index(b)] for b in bands], ms_valid

    # ---- the model ----

    def fit(self, cancelled: Callable[[], bool] = lambda: False) -> PanSharpenModel:
        """The weights and gains over the overlap, read at the pan's resolution (the whole, or windows spread over it)."""
        with self.lock:
            area = self.pan_extent.intersect(self._ms.extent())
            px, py = self.pan_pixel
        if area.isEmpty() or px <= 0 or py <= 0:
            raise QuickPanError("2 つの画像の重なりなし")
        cols, rows = max(1, round(area.width() / px)), max(1, round(area.height() / py))
        if cols * rows <= FIT_WHOLE:
            windows = [(area, cols, rows)]
        else:
            windows = [(QgsRectangle(area.xMinimum() + x0 * px, area.yMaximum() - y1 * py, area.xMinimum() + x1 * px,
                                     area.yMaximum() - y0 * py), x1 - x0, y1 - y0)
                       for x0, y0, x1, y1 in sample_windows(Overlap(0, 0, cols, rows), FIT_COUNT, FIT_SIZE)]
        pans, stacks = [], []
        for rect, w, h in windows:
            if cancelled():
                raise QuickPanError("中止")
            read = self._read(rect, w, h)
            if read is None:
                continue
            ms, _ms_valid, pan, both = read
            pans.append(pan[both].astype(np.float64))
            stacks.append(ms[:, both])
        if not pans or not sum(p.size for p in pans):
            raise QuickPanError("両方に値のある画素なし", code="E506")
        fit_pan = np.concatenate(pans)[:, None]
        fit_ms = np.concatenate(stacks, axis=1)[:, :, None]
        return fit_model(fit_pan, fit_ms, list(range(fit_ms.shape[0])), np.ones(fit_pan.shape, dtype=bool), self.method, "auto")

    def start_fit(self, done: Callable[["PanLink"], None], keep: bool = False) -> None:
        """
        Fits the model in a task (the image drawn as it is until then; `keep`:
        with the model it has); `done(link)` on the main thread when it ends.
        """
        self.cancel_fit()
        self._generation += 1
        generation = self._generation
        if not keep:
            self.model = None
            self.model_relative = None
        self.failed = ""
        self.fitting = True
        relative = self.relative

        def current() -> bool:
            return generation == self._generation

        def work(task):
            return self.fit(lambda: task.isCanceled() or not current())

        def finished(model) -> None:
            if current():
                self.fitting = False
                self.model = model
                self.model_relative = relative
                done(self)

        def failed(message: str) -> None:
            if current():
                self.fitting = False
                self.model = None
                self.model_relative = None
                self.failed = message
                done(self)

        self._task = run_task(TASK, work, finished, failed)

    def cancel_fit(self) -> None:
        self._generation += 1
        self.fitting = False
        task = self._task
        self._task = None
        if task is not None and not sip.isdeleted(task):
            try:
                task.cancel()
            except RuntimeError:
                pass

    def record(self) -> dict:
        return {"pan": self.pan_id, "method": self.method, "strength": self.strength}


class Signals(QObject):
    #: A multispectral layer's id: its クイックパンシャープン started, changed (the fit ended, an option) or ended.
    changed = pyqtSignal(str)


_signals: Optional[Signals] = None
#: The links by the multispectral layer's id.
_links: dict = {}


def signals() -> Signals:
    global _signals
    if _signals is None:
        _signals = Signals()
    return _signals


def _layer(layer_id: str):
    layer = QgsProject.instance().mapLayer(layer_id)
    return layer if isinstance(layer, QgsRasterLayer) else None


def links() -> list:
    return list(_links.values())


def link_of(layer) -> Optional[PanLink]:
    """The link that draws `layer` (the multispectral one) sharpened."""
    return _links.get(layer.id()) if layer is not None and not sip.isdeleted(layer) else None


def used_as_pan(layer) -> Optional[PanLink]:
    """The link whose pan `layer` is."""
    if layer is None or sip.isdeleted(layer):
        return None
    return next((link for link in _links.values() if link.pan_id == layer.id()), None)


def partner(layer) -> Optional[QgsRasterLayer]:
    """The other layer of the link `layer` is in (its pan, or the image it is the pan of), or None."""
    link = link_of(layer) or used_as_pan(layer)
    if link is None:
        return None
    return _layer(link.pan_id if link.ms_id == layer.id() else link.ms_id)


def _saved(layer) -> dict:
    try:
        record = json.loads(str(layer.customProperty(PROPERTY) or "") or "{}")
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def _fitted(link: PanLink) -> None:
    ms = _layer(link.ms_id)
    if ms is not None:
        ms.triggerRepaint()
    signals().changed.emit(link.ms_id)


def _watch(link: PanLink, ms: QgsRasterLayer, pan: QgsRasterLayer) -> None:
    ms_id = link.ms_id
    moves = shift_io.signals()
    for owner, signal, slot in ((ms, ms.rendererChanged, lambda: _renderer_changed(ms_id)),
                                (ms, ms.dataSourceChanged, lambda: _source_changed(ms_id)),
                                (pan, pan.dataSourceChanged, lambda: _source_changed(ms_id)),
                                (moves, moves.moved, lambda layer_id: _moved(ms_id, layer_id))):
        signal.connect(slot)
        link.connections.append((owner, signal, slot))


def _unwatch(link: PanLink) -> None:
    for owner, signal, slot in link.connections:
        if sip.isdeleted(owner):
            continue  # gone, and its connections with it
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass
    link.connections = []


def _renderer_changed(ms_id: str) -> None:
    """Another renderer on the multispectral layer: a new correction renderer gets the link, any other ends it."""
    link, ms = _links.get(ms_id), _layer(ms_id)
    if link is None or ms is None or shift_io.moving(ms):
        # (Moved: QGIS's own renderer for a moment, the correction put back at the end; see `_moved`.)
        return
    r = er.enhance_renderer(ms)
    if r is None:
        stop(ms)
    else:
        r.state.pan = link


def _source_changed(ms_id: str) -> None:
    """A layer of the link has a new source: its pixels read from there, the model fitted again (kept when they lie as they did)."""
    link, ms = _links.get(ms_id), _layer(ms_id)
    pan = _layer(link.pan_id) if link is not None else None
    if link is None or ms is None or pan is None or shift_io.moving(ms) or shift_io.moving(pan):
        # (A move: read again when it is done, `_moved`.)
        return
    try:
        link.take(ms, pan)
    except QuickPanError:
        stop(ms)
        return
    if link.as_fitted():
        link.cancel_fit()
    else:
        link.start_fit(_fitted, keep=True)
    ms.triggerRepaint()
    signals().changed.emit(ms_id)


def _moved(ms_id: str, layer_id: str) -> None:
    """位置ずらし has moved a layer of the link: the correction (and the link on it) back on the image, read from the new place."""
    link, ms = _links.get(ms_id), _layer(ms_id)
    if link is None or ms is None or layer_id not in (link.ms_id, link.pan_id):
        return
    r = er.enhance_renderer(ms)
    if r is None:
        stop(ms)
        return
    r.state.pan = link
    _source_changed(ms_id)


def start(ms: QgsRasterLayer, pan: QgsRasterLayer, method: str = "gram-schmidt", strength: float = 1.0,
          pan_shown: Optional[bool] = None) -> PanLink:
    """
    Draws `ms` with `pan`'s detail (choose() tells which is which). The pan is
    hidden; `pan_shown`: whether it was shown before (opening a project: as
    saved, the pan left as it is). A layer already in a link leaves it first.
    """
    for layer in (ms, pan):
        for old in (link_of(layer), used_as_pan(layer)):
            if old is not None:
                stop(_layer(old.ms_id))
    link = PanLink(ms, pan, method, strength)
    renderer = er.enhance_renderer(ms)
    if renderer is None and ms.customProperty(er.PROPERTY) and er.restore_layer(ms):
        renderer = er.enhance_renderer(ms)
    if renderer is None:
        old = plain_copy(ms.renderer()) if ms.renderer() is not None else None
        renderer = er.apply(ms, initial_settings(ms))
        if old is not None:
            link.before = (old, renderer.settings)
    node = QgsProject.instance().layerTreeRoot().findLayer(pan.id())
    if pan_shown is None:
        pan_shown = bool(node is not None and node.itemVisibilityChecked())
        if node is not None:
            node.setItemVisibilityChecked(False)
    _links[ms.id()] = link
    renderer.state.pan = link
    _watch(link, ms, pan)
    ms.setCustomProperty(PROPERTY, json.dumps(dict(link.record(), panShown=bool(pan_shown)), ensure_ascii=False))
    link.start_fit(_fitted)
    signals().changed.emit(ms.id())
    return link


def stop(ms, show_pan: bool = True) -> None:
    """Draws `ms` as it is again; the pan shown again when it was before (`show_pan`)."""
    if ms is None or sip.isdeleted(ms):
        return
    link = _links.pop(ms.id(), None)
    record = _saved(ms)
    ms.removeCustomProperty(PROPERTY)
    if link is None:
        return
    _unwatch(link)
    link.cancel_fit()
    r = er.enhance_renderer(ms)
    if r is not None and r.state.pan is link:
        r.state.pan = None
    pan = _layer(link.pan_id)
    node = QgsProject.instance().layerTreeRoot().findLayer(link.pan_id) if pan is not None else None
    if show_pan and node is not None and record.get("panShown"):
        node.setItemVisibilityChecked(True)
    if link.before is not None and r is not None and r.settings == link.before[1]:
        # Put on only for this and not changed since: the layer's own renderer back.
        er.remove(ms)
        ms.setRenderer(link.before[0])
    ms.triggerRepaint()
    signals().changed.emit(ms.id())


def end(link: PanLink) -> None:
    """Ends a link (as `stop` on its multispectral layer)."""
    stop(_layer(link.ms_id))


def set_options(ms, method: Optional[str] = None, strength: Optional[float] = None) -> None:
    """Another method (the model fitted again) or strength (0–2: 1 the standard, 0 the multispectral image resampled)."""
    link = link_of(ms)
    if link is None:
        return
    if strength is not None:
        link.strength = max(0.0, min(2.0, float(strength)))
    if method is not None and method != link.method:
        if method not in METHODS:
            raise QuickPanError(f"方式 {method} なし")
        link.method = method
        link.start_fit(_fitted)
    record = _saved(ms)
    ms.setCustomProperty(PROPERTY, json.dumps(dict(record, **link.record()), ensure_ascii=False))
    ms.triggerRepaint()
    signals().changed.emit(ms.id())


def restore(layers=None) -> int:
    """The links kept on layers (a project opened) put back, where both layers are there; how many."""
    count = 0
    for ms in list(layers if layers is not None else QgsProject.instance().mapLayers().values()):
        if not isinstance(ms, QgsRasterLayer) or sip.isdeleted(ms) or ms.id() in _links or not ms.customProperty(PROPERTY):
            continue
        record = _saved(ms)
        pan = _layer(str(record.get("pan") or ""))
        if pan is None or not pan.isValid() or not ms.isValid():
            continue
        try:
            start(ms, pan, str(record.get("method") or "gram-schmidt"), float(record.get("strength", 1.0)),
                  pan_shown=bool(record.get("panShown")))
            count += 1
        except (QuickPanError, ValueError, TypeError):
            continue
    return count


class QuickPanKeeper(QObject):
    """Ends links whose layers go and puts back the ones kept in a project as it is opened (from the plugin's start to its unload)."""

    def __init__(self, project: Optional[QgsProject] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.project = project or QgsProject.instance()
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.setInterval(0)
        # After what reads the project now (画像補正 puts its corrections back on the same signals).
        self._later.timeout.connect(lambda: restore())
        self.project.layersWillBeRemoved.connect(self.going)
        self.project.layersAdded.connect(self._added)
        self.project.readProject.connect(self._added)
        restore()

    def _added(self, *_args) -> None:
        self._later.start()

    def going(self, ids) -> None:
        ids = set(ids)
        for link in list(_links.values()):
            if link.ms_id in ids or link.pan_id in ids:
                stop(_layer(link.ms_id), show_pan=link.pan_id not in ids)

    def unload(self) -> None:
        """The links let go, the layers left as they are (kept on them: the plugin started again puts them back)."""
        self._later.stop()
        for signal, slot in ((self.project.layersWillBeRemoved, self.going), (self.project.layersAdded, self._added),
                             (self.project.readProject, self._added)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for link in list(_links.values()):
            _unwatch(link)
            link.cancel_fit()
            ms = _layer(link.ms_id)
            r = er.enhance_renderer(ms) if ms is not None else None
            if r is not None and r.state.pan is link:
                r.state.pan = None
            if ms is not None:
                ms.triggerRepaint()
        _links.clear()
