"""
Complex SAR images shown as amplitude: a SICD (NITF whose image bands are
the real part I and the imaginary part Q, ISUBCAT "I" and "Q"), a raster of
complex pixels (CInt16, CFloat32… as Sentinel-1 SLC), or two bands named
I / Q (or real / imag). QGIS draws such bands as they are, so the plugin
puts a GDAL VRT in front of the image when it is added to the project, in
any way: its band is the amplitude sqrt(I² + Q²), or 20·log10 of it (dB,
the default; `complexImages` in the settings file). GDAL's own pixel
functions compute it while drawing ("complex" pairs I and Q, "mod" / "dB"
take the magnitude), so no library is needed and the image is never
written. Pixels whose I and Q are both 0 (SICD's padding) are no data.

The VRTs are kept in the profile's `complex-sar` folder, named after the
image's path, size and time, so an image opened again reuses them, and the
project saved with the layer reads them again. The layer remembers the
image it shows (COMPLEX_SOURCE): its file name, NITF metadata and imageRules
are the image's. The VRT carries the image's metadata, georeferencing (GCPs,
RPC) too. Its overviews are built like any image's (`overviews`), from the
amplitude, as averaging I and Q themselves would cancel out.

An image with I/Q in pairs gets one band per pair; one of amplitude and
phase (ISUBCAT "M" / "P", SICD's AMP8I_PHS8I) shows its amplitude band
(as stored: an AmpTable is not applied).
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from typing import Optional, Sequence
from xml.etree import ElementTree

from osgeo import gdal

#: Layer properties: the image a converted layer shows, and how (a mode of core.complex_images.MODES).
COMPLEX_SOURCE = "satobs/complexSource"
COMPLEX_MODE = "satobs/complexMode"

_I = {"I", "REAL", "RE"}
_Q = {"Q", "IMAG", "IMAGINARY", "IM"}


@dataclass(frozen=True)
class Output:
    """One band shown: from a complex band (`bands` one), an I and a Q band (two), or an amplitude band (`amplitude`)."""
    bands: tuple[int, ...]
    amplitude: bool = False


def _label(band) -> tuple[str, str]:
    """A band's NITF ISUBCAT and its description, upper case."""
    sub = (band.GetMetadataItem("NITF_ISUBCAT") or "").strip().upper()
    return sub, (band.GetDescription() or "").strip().upper()


def outputs_of(ds) -> list[Output]:
    """
    The amplitude bands to show for a GDAL dataset: one per complex band,
    per I band followed by its Q band (NITF ISUBCAT, else the bands'
    descriptions), per NITF amplitude band "M" beside a phase band "P";
    [] when the image is not complex.
    """
    if ds is None or ds.RasterCount == 0:
        return []
    bands = [ds.GetRasterBand(i + 1) for i in range(ds.RasterCount)]
    out = [Output((i + 1,)) for i, b in enumerate(bands) if gdal.DataTypeIsComplex(b.DataType)]
    if out:
        return out
    labels = [_label(b) for b in bands]
    for use in (0, 1):  # the ISUBCAT first, then the descriptions
        names = [label[use] for label in labels]
        i = 0
        pairs = []
        while i + 1 < len(names):
            if names[i] in _I and names[i + 1] in _Q:
                pairs.append(Output((i + 1, i + 2)))
                i += 2
            else:
                i += 1
        if pairs:
            return pairs
    subs = [label[0] for label in labels]
    if "P" in subs:
        return [Output((i + 1,), amplitude=True) for i, s in enumerate(subs) if s == "M"]
    return []


def is_complex(path: str) -> bool:
    """Whether the image at `path` is one shown as amplitude (False when GDAL cannot open it)."""
    return bool(_outputs_at(path))


def _outputs_at(path: str) -> list[Output]:
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        return outputs_of(gdal.Open(path))
    except Exception:  # noqa: BLE001 (what GDAL cannot read is not complex)
        return []
    finally:
        gdal.PopErrorHandler()


def vrt_name(path: str, mode: str) -> str:
    """The VRT's file name for an image and a mode: the image's name after a hash of its path, size and time."""
    stat = os.stat(path)
    key = f"{os.path.normcase(os.path.abspath(path))}|{stat.st_size}|{stat.st_mtime_ns}|{mode}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^\w.-]+", "_", os.path.basename(path))[:80]
    return f"{digest}_{stem}.{mode}.vrt"


def _source(parent, path: str, band: int) -> None:
    source = ElementTree.SubElement(parent, "SimpleSource")
    name = ElementTree.SubElement(source, "SourceFilename", relativeToVRT="0")
    name.text = path
    ElementTree.SubElement(source, "SourceBand").text = str(band)


def _write(path: str, root) -> None:
    """Writes a VRT whole: to a temporary name first, so a half-written one is never read."""
    part = f"{path}.{uuid.uuid4().hex[:8]}.part"
    ElementTree.ElementTree(root).write(part, encoding="utf-8")
    os.replace(part, path)


def build_vrt(path: str, mode: str, folder: str) -> Optional[str]:
    """
    The amplitude (or dB) VRT of a complex image in `folder` (made when
    missing; reused when there): its path, None when the image is not
    complex. I and Q go through a second VRT (`….iq.vrt`) pairing them into
    complex pixels.
    """
    outputs = _outputs_at(path)
    if not outputs:
        return None
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, vrt_name(path, mode))
    if os.path.isfile(target):
        return target
    src = gdal.Open(path)
    width, height = src.RasterXSize, src.RasterYSize
    pairs = [o for o in outputs if len(o.bands) == 2]
    iq_path = None
    if pairs:
        iq_path = target[: -len(f".{mode}.vrt")] + ".iq.vrt"
        iq = ElementTree.Element("VRTDataset", rasterXSize=str(width), rasterYSize=str(height))
        for n, o in enumerate(pairs, 1):
            band = ElementTree.SubElement(iq, "VRTRasterBand", dataType="CFloat32", band=str(n), subClass="VRTDerivedRasterBand")
            ElementTree.SubElement(band, "PixelFunctionType").text = "complex"
            for b in o.bands:
                _source(band, path, b)
        _write(iq_path, iq)
    # The image's georeferencing, GCPs and metadata (RPC too) from GDAL's own VRT of it, its bands replaced.
    skeleton = gdal.Translate("", src, format="VRT", bandList=[1])
    root = ElementTree.fromstring(skeleton.GetMetadata("xml:VRT")[0])
    skeleton = None
    for band in root.findall("VRTRasterBand"):
        root.remove(band)
    pair_no = 0
    for n, o in enumerate(outputs, 1):
        band = ElementTree.SubElement(root, "VRTRasterBand", dataType="Float32", band=str(n), subClass="VRTDerivedRasterBand")
        if o.amplitude:
            source_path, source_band = path, o.bands[0]
        elif len(o.bands) == 2:
            pair_no += 1
            source_path, source_band = iq_path, pair_no
        else:
            source_path, source_band = path, o.bands[0]
        ElementTree.SubElement(band, "Description").text = ("dB" if mode == "dB" else "amplitude") + (f" {n}" if len(outputs) > 1 else "")
        if not o.amplitude:
            ElementTree.SubElement(band, "SourceTransferType").text = "CFloat64"
        if mode == "dB":
            ElementTree.SubElement(band, "NoDataValue").text = "-inf"
            ElementTree.SubElement(band, "PixelFunctionType").text = "dB"
            ElementTree.SubElement(band, "PixelFunctionArguments", fact="20")
        else:
            ElementTree.SubElement(band, "NoDataValue").text = "0"
            ElementTree.SubElement(band, "PixelFunctionType").text = "mod"
        _source(band, source_path, source_band)
    _write(target, root)
    return target


def source_of(layer) -> Optional[str]:
    """The image a converted layer shows (its COMPLEX_SOURCE), None for other layers."""
    try:
        value = layer.customProperty(COMPLEX_SOURCE)
    except (AttributeError, RuntimeError):
        return None
    return str(value) if value else None


# ---- QGIS ----

def default_folder() -> str:
    from . import settings
    return os.path.join(settings.profile_folder(), "complex-sar")


def convert_layer(layer, mode: str, folder: Optional[str] = None) -> bool:
    """
    Points a raster layer of a complex image at its amplitude (dB) VRT and
    stretches it (2–98 % of its values, gray); True when it was converted.
    Layers already converted, of URLs or of other images are left as they are.
    """
    from .overviews_io import local_path
    path = local_path(layer)
    if not path or source_of(layer) or not is_complex(path):
        return False
    try:
        vrt = build_vrt(path, mode, folder or default_folder())
    except (OSError, RuntimeError) as error:
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(f"複素 SAR 画像の振幅 VRT を作れず（{path}: {error}）", "SKYWATCH", Qgis.MessageLevel.Warning)
        return False
    if not vrt:
        return False
    from qgis.core import QgsDataProvider
    layer.setCustomProperty(COMPLEX_SOURCE, path)
    layer.setCustomProperty(COMPLEX_MODE, mode)
    layer.setDataSource(vrt, layer.name(), "gdal", QgsDataProvider.ProviderOptions())
    stretch(layer)
    return True


def stretch(layer, low: float = 0.02, high: float = 0.98) -> None:
    """Gray, band 1, stretched between the `low` and `high` cumulative cuts of its values (sampled)."""
    from qgis.core import QgsContrastEnhancement, QgsRectangle, QgsSingleBandGrayRenderer, QgsRasterMinMaxOrigin
    if not layer.isValid():
        return
    provider = layer.dataProvider()
    renderer = QgsSingleBandGrayRenderer(provider, 1)
    lo, hi = provider.cumulativeCut(1, low, high, QgsRectangle(), 250000)
    enhancement = QgsContrastEnhancement(provider.dataType(1))
    enhancement.setContrastEnhancementAlgorithm(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum)
    if hi > lo:
        enhancement.setMinimumValue(lo)
        enhancement.setMaximumValue(hi)
    renderer.setContrastEnhancement(enhancement)
    origin = QgsRasterMinMaxOrigin()
    origin.setLimits(QgsRasterMinMaxOrigin.Limits.CumulativeCut)
    origin.setCumulativeCutLower(low)
    origin.setCumulativeCutUpper(high)
    renderer.setMinMaxOrigin(origin)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def layers_added(layers: Sequence, mode, message_bar=None, folder: Optional[str] = None) -> list:
    """
    Converts the complex images among `layers`; the layers converted, told
    in the message bar. `mode` is a mode of MODES, None (none converted), or
    a function giving a layer's (an imageRules entry's for its file name).
    """
    done = []
    for layer in layers:
        shown = mode(layer) if callable(mode) else mode
        if shown and convert_layer(layer, shown, folder):
            done.append((layer, shown))
    if done and message_bar is not None:
        for shown in dict.fromkeys(m for _, m in done):
            these = [layer for layer, m in done if m == shown]
            names = "、".join(layer.name() for layer in these[:3]) + (" ほか" if len(these) > 3 else "")
            message_bar.pushInfo("複素 SAR 画像", f"{names} を{'振幅（dB）' if shown == 'dB' else '振幅'}で表示（I・Q から計算）")
    return [layer for layer, _ in done]
