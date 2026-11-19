"""
Where a place is in an image's file: its full-resolution pixel from the
file's top-left corner (x right, y down), as the image lies on the map now
(観測記録の PIXEL_X・PIXEL_Y・IMAGE_FILE; tsudo 2026-11-19: 「位置ずらした上で記録
すると、元の画像の位置とずれるため、教師データとして使えなくない？」).

A record placed on an image moved by 位置ずらし (shift_io) is where the thing
is on the ground; its pixel is the file's own (the moved VRT keeps the file's
pixel grid), so AI training finds the thing in the file whatever the image's
move then. A place outside the image gives pixels below 0 or past its size
(tsudo: 「誤って画像範囲外に作った場合にクラッシュしないように。とりあえずマイナス値
とかでいいよ」); one the image's model cannot reach (an RPC far off) gives none.

The records are saved off the main thread (a task for a shared store), where
the project's layers must not be used: `placement_of` reads on the main
thread how a layer is placed (its geotransform, else its GCPs and RPC model,
as QGIS places it), and `Placement` finds pixels with GDAL alone in any
thread.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from osgeo import gdal

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext, QgsPoint, QgsPointXY, QgsProject,
    QgsRasterDataProvider, QgsRasterLayer,
)

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


@dataclass(eq=False)
class Placement:
    """How one image file lies on the map now (its layer's CRS and georeferencing), for any thread."""
    #: The file the pixels are of (IMAGE_FILE).
    file: str
    width: int
    height: int
    #: The layer's CRS (the georeferencing's coordinates are in it) and the project's datum transformations.
    crs: QgsCoordinateReferenceSystem
    context: QgsCoordinateTransformContext = field(default_factory=QgsCoordinateTransformContext)
    #: Its footprint [west, south, east, north] (WGS 84), to choose among an image's files.
    box: Optional[tuple] = None
    geotransform: Optional[tuple] = None
    #: GCPs as (x, y, z, pixel, line), their CRS; RPC metadata items.
    gcps: tuple = ()
    gcp_wkt: str = ""
    rpc: tuple = ()
    #: GDAL's GCP transformation to use (GCP_TPS for one sampled from QGIS's own, when the file could not be read).
    method: str = ""

    def __post_init__(self):
        self._local = threading.local()

    # ---- GDAL, per thread ----

    def _transformer(self):
        t = getattr(self._local, "transformer", None)
        if t is None:
            # A VRT of no bands with the file's georeferencing: GDAL places it as it places the file (no pixels read).
            ds = gdal.GetDriverByName("VRT").Create("", self.width, self.height, 0)
            options = []
            if self.geotransform is not None:
                ds.SetGeoTransform(list(self.geotransform))
            if self.gcps:
                ds.SetGCPs([gdal.GCP(*g) for g in self.gcps], self.gcp_wkt)
            if self.rpc:
                ds.SetMetadata(dict(self.rpc), "RPC")
                height = dict(self.rpc).get("HEIGHT_OFF")
                if height:
                    # As QGIS asks (QgsGdalUtils::rpcAwareCreateTransformer).
                    options.append(f"RPC_HEIGHT={str(height).split()[0]}")
            if self.method:
                options.append(f"METHOD={self.method}")
            t = gdal.Transformer(ds, None, options)
            self._local.ds, self._local.transformer = ds, t
        return t

    def _to_layer(self) -> QgsCoordinateTransform:
        # Made each time (QGIS keeps the PROJ pipelines per thread): none outlives QGIS in a placement kept.
        return QgsCoordinateTransform(WGS84, self.crs, self.context)

    def pixel(self, lon: float, lat: float) -> Optional[tuple[float, float]]:
        """The file's pixel (x, y) at a place (WGS 84); None when it cannot be found (never raises)."""
        try:
            p = self._to_layer().transform(QgsPointXY(lon, lat))
            ok, (x, y, _z) = self._transformer().TransformPoint(1, p.x(), p.y(), 0)
        except Exception:  # noqa: BLE001 (outside what the CRS or the model reaches)
            return None
        if not ok or not (math.isfinite(x) and math.isfinite(y)):
            return None
        return x, y

    def place(self, x: float, y: float) -> Optional[tuple[float, float]]:
        """The place (WGS 84 lon, lat) of a pixel of the file as it lies on the map now; None when it cannot be found."""
        try:
            ok, (px, py, _z) = self._transformer().TransformPoint(0, x, y, 0)
            if not ok or not (math.isfinite(px) and math.isfinite(py)):
                return None
            p = self._to_layer().transform(QgsPointXY(px, py), QgsCoordinateTransform.TransformDirection.ReverseTransform)
        except Exception:  # noqa: BLE001
            return None
        return (p.x(), p.y()) if math.isfinite(p.x()) and math.isfinite(p.y()) else None


# ---- reading a layer (main thread) ----

def gdal_path(layer: QgsRasterLayer) -> str:
    """The path GDAL opens for the layer's source (a moved image's VRT, a complex SAR image's amplitude VRT)."""
    from qgis.core import QgsProviderRegistry

    try:
        parts = QgsProviderRegistry.instance().decodeUri("gdal", layer.source())
        path = str(parts.get("path") or "")
    except Exception:  # noqa: BLE001
        path = ""
    return path or layer.source().split("|")[0]


def layer_file(layer: QgsRasterLayer) -> str:
    """What IMAGE_FILE says for a layer: a catalog record's image below its folder, else the file's name (not a move's VRT)."""
    from .obs_images import IMAGE_FILE, file_name

    return str(layer.customProperty(IMAGE_FILE) or "") or file_name(layer)


def _sample(provider, x: float, y: float) -> Optional[tuple[float, float]]:
    p = provider.transformCoordinates(QgsPoint(x, y), QgsRasterDataProvider.TransformType.TransformImageToLayer)
    if p.isEmpty() or not (math.isfinite(p.x()) and math.isfinite(p.y())):
        return None
    return p.x(), p.y()


def _affine(provider, w: int, h: int) -> Optional[tuple]:
    """The geotransform of a layer QGIS places by one (checked at the far corner and the centre), else None."""
    p00, p10, p01, p11, mid = (_sample(provider, x, y) for x, y in ((0, 0), (w, 0), (0, h), (w, h), (w / 2, h / 2)))
    if None in (p00, p10, p01, p11, mid):
        return None
    gt = (p00[0], (p10[0] - p00[0]) / w, (p01[0] - p00[0]) / h, p00[1], (p10[1] - p00[1]) / w, (p01[1] - p00[1]) / h)
    scale = max(abs(p10[0] - p00[0]) + abs(p10[1] - p00[1]), abs(p01[0] - p00[0]) + abs(p01[1] - p00[1]), 1e-12)
    for (x, y), p in (((w, h), p11), ((w / 2, h / 2), mid)):
        ex, ey = gt[0] + x * gt[1] + y * gt[2], gt[3] + x * gt[4] + y * gt[5]
        if abs(ex - p[0]) + abs(ey - p[1]) > 1e-7 * scale:
            return None
    return gt


def placement_of(layer: QgsRasterLayer, project: Optional[QgsProject] = None) -> Optional[Placement]:
    """How a raster layer's file lies on the map now (main thread); None for a layer not of GDAL or not placed."""
    from .obs_images import _box
    from .shift_io import PIXEL_GRID

    if not isinstance(layer, QgsRasterLayer) or not layer.isValid() or layer.providerType() != "gdal":
        return None
    project = project or QgsProject.instance()
    box = _box(layer)
    common = dict(file=layer_file(layer), crs=layer.crs(), context=project.transformContext(), box=tuple(box) if box else None)
    path = gdal_path(layer)
    ds = None
    if not path.startswith("/vsicurl") and "://" not in path:
        # (Not over the network on the main thread.)
        try:
            ds = gdal.Open(path)
        except RuntimeError:
            ds = None
    if ds is not None:
        # As the file (or the move's VRT) says: GDAL places the copy as QGIS places the file.
        gt = ds.GetGeoTransform(can_return_null=True)
        gcps = tuple((g.GCPX, g.GCPY, g.GCPZ, g.GCPPixel, g.GCPLine) for g in ds.GetGCPs() or ())
        rpc = tuple(sorted((ds.GetMetadata("RPC") or {}).items()))
        if gt is None and not gcps and not rpc:
            gt = PIXEL_GRID
        return Placement(width=ds.RasterXSize, height=ds.RasterYSize, geotransform=tuple(gt) if gt is not None else None,
                         gcps=gcps, gcp_wkt=(ds.GetGCPProjection() or "") if gcps else "", rpc=rpc, **common)
    # The file not read here (a URL): QGIS's own placing, a geotransform when it is one, else sampled on a grid
    # (QGIS shows an image placed by GCPs or RPC warped: its size is about the file's).
    provider = layer.dataProvider()
    w, h = provider.xSize(), provider.ySize()
    if w <= 0 or h <= 0:
        return None
    gt = _affine(provider, w, h)
    if gt is not None:
        return Placement(width=w, height=h, geotransform=gt, **common)
    n = 16
    samples = []
    for i in range(n + 1):
        for j in range(n + 1):
            x, y = w * i / n, h * j / n
            p = _sample(provider, x, y)
            if p is not None:
                samples.append((p[0], p[1], 0.0, x, y))
    if len(samples) < 3:
        return None
    return Placement(width=w, height=h, gcps=tuple(samples), gcp_wkt=layer.crs().toWkt(), method="GCP_TPS", **common)


#: Placements read, by layer id: (its source and CRS then, the placement). Main thread only.
_read: dict[str, tuple] = {}


def cached_placement(layer: QgsRasterLayer, project: Optional[QgsProject] = None) -> Optional[Placement]:
    """placement_of, read again only when the layer's source (a move) or CRS changed."""
    key = (layer.source(), layer.crs().toWkt(), str(layer.customProperty("satobs/imageFile") or ""))
    known = _read.get(layer.id())
    if known is not None and known[0] == key:
        return known[1]
    placement = placement_of(layer, project)
    _read[layer.id()] = (key, placement)
    return placement


def forget() -> None:
    """Drops the placements read (the plugin unloaded: no GDAL dataset is left to Python's exit)."""
    _read.clear()


def choose(placements: Sequence[Placement], lon: float, lat: float) -> Optional[Placement]:
    """Of an image's files, the one over a place (else the first), as obs_images.layer_at."""
    for p in placements:
        b = p.box
        if b and b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            return p
    return placements[0] if placements else None


#: Decimals kept of a record's pixel: 1/100 pixel (a record's lon, lat is kept to about 1 cm).
DIGITS = 2


def pixel_in(placements: Sequence[Placement], lon: float, lat: float) -> Optional[tuple[str, float, float]]:
    """(file, x, y) of a place in an image of these files (to 1/100 pixel); None when not found."""
    p = choose(placements, lon, lat)
    found = p.pixel(lon, lat) if p is not None else None
    return (p.file, round(found[0], DIGITS), round(found[1], DIGITS)) if found else None


def locator(images, project: Optional[QgsProject] = None) -> Callable[[str, float, float], Optional[tuple[str, float, float]]]:
    """
    ObservationBook.locate for the images open now (main thread: their layers
    read here; the function it gives may be called in any thread).
    """
    project = project or QgsProject.instance()
    for gone in [layer_id for layer_id in _read if project.mapLayer(layer_id) is None]:
        del _read[gone]
    by_image: dict[str, list[Placement]] = {}
    for image in images:
        placements = []
        for layer_id in image.layer_ids:
            layer = project.mapLayer(layer_id)
            if isinstance(layer, QgsRasterLayer):
                p = cached_placement(layer, project)
                if p is not None:
                    placements.append(p)
        if placements:
            by_image[image.id] = placements

    def locate(image_id: str, lon: float, lat: float) -> Optional[tuple[str, float, float]]:
        placements = by_image.get(image_id)
        return pixel_in(placements, lon, lat) if placements else None

    return locate


def original_placement(layer: QgsRasterLayer, project: Optional[QgsProject] = None) -> Optional[Placement]:
    """How the layer's own file places it, before any 位置ずらし (main thread)."""
    from .shift_io import SOURCE

    if not layer.customProperty(SOURCE):
        return cached_placement(layer, project)
    own = QgsRasterLayer(str(layer.customProperty(SOURCE)), layer.name(), "gdal")
    if not own.isValid():
        return None
    if layer.crs().isValid():
        own.setCrs(layer.crs())
    for key in ("satobs/imageFile",):
        if layer.customProperty(key):
            own.setCustomProperty(key, layer.customProperty(key))
    p = placement_of(own, project)
    if p is not None:
        p.file = layer_file(layer)
    return p
