"""
手元にコピーして見る (`localCopy`, core/local_copy.py): a large image on a
network folder is copied whole to this PC in the background (QGIS's task
manager, where it can be cancelled), and once copied its layers read the
copy (QgsRasterLayer.setDataSource: the style, the correction, the CRS and
the custom properties stay). The image itself is only read.

- One image is copied at a time (in order, the share is read best that
  way); one asked for again while waiting or copying is not queued twice.
  Its pyramids wait for the copy and are built from it (overviews_io), then
  put beside the image on the share too where `overviews` would have put
  them there, so whoever opens it next uses them.
- The copy goes with the files GDAL reads with the image in its folder
  (.ovr, .aux.xml, .tfw, .RPB, .IMD…), under the same names, in a folder of
  its own (core/local_copy.copy_name), with `mikage-copy.json` last: the
  image's place, size and time. An image replaced on the share (another
  size or time) is copied again; a copy GDAL cannot read as the image (other
  size or bands) is dropped.
- Where: `location` "temp" a folder of this QGIS under %TEMP%\\MIKAGE\\copies
  (`mikage-<process id>-…`, as the overviews' temporary folder), deleted when
  QGIS closes, what was still open then at the next start; "cache"
  %LOCALAPPDATA%\\MIKAGE\\copies, kept, opened at once the next time.
- Room: at most `maxGB` (copies not in use go first, least recently used),
  and the disk keeps 5 % (at least 2 GB) free; otherwise the image is read
  from the share as before.
- The layer remembers the image (ORIGINAL): 観測記録, 開いた画像の履歴, AI
  results and 「前の画像を閉じる」 know it by that place (obs_images.image_path),
  and a project saved names the image, not the copy (project_written).
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from osgeo import gdal
from qgis.core import (
    Qgis, QgsDataProvider, QgsMapLayer, QgsMessageLog, QgsProject, QgsProviderRegistry, QgsRasterLayer,
)

from ..core import errors
from ..core.local_copy import (
    GB, META, NETWORK_TYPES, CopyInfo, LocalCopyOptions, big_enough, copy_name, meta_of, mount_type, place_key,
    relative_name, reserve_for, root_of, same_folder_files, still_same, to_evict, unc_path,
)
from . import overviews_io
from .tasks import LOG_TAG, Cancelled, FunctionTask, run_task

TITLE = "手元のコピー"
#: The layer's custom property: the image on the share its copy was made from.
ORIGINAL = "mikage/copiedFrom"
#: Bytes read and written at a time (progress and cancel between them).
CHUNK = 8 * 1024 * 1024

#: Outcomes of a copy.
COPIED, NO_ROOM, CHANGED = "copied", "no-room", "changed"


class CopyError(errors.Coded, RuntimeError):
    """An image could not be copied to this PC; why in `detail`."""

    code = "E511"


#: The settings file's `localCopy` now (set by the plugin when it is read: configure).
current = LocalCopyOptions()
_swept: set = set()


def configure(options: LocalCopyOptions) -> None:
    """The settings file's `localCopy` read: kept, and the temporary copies of a QGIS no longer running deleted (once a folder)."""
    global current
    current = options
    for root in {root_of(LocalCopyOptions()), root_of(options)}:
        if root not in _swept and (options.location == "temp" or root == root_of(LocalCopyOptions())):
            _swept.add(root)
            try:
                overviews_io.sweep_temp(root)
            except OSError:
                pass


def base_folder(options: Optional[LocalCopyOptions] = None, make: bool = False) -> str:
    """Where the copies are: this QGIS's folder under the root ("temp"), or the root itself ("cache")."""
    options = options or current
    root = root_of(options)
    folder = os.path.join(root, overviews_io.session_name()) if options.location == "temp" else root
    if make:
        os.makedirs(folder, exist_ok=True)
    return folder


# ---- which images ---------------------------------------------------------------------------------

#: The places (an image, or a folder of images) a rewrite of `pathMappings` with `localCopy` found, and its choice.
_places: dict = {}
_places_lock = threading.Lock()


def mark_place(place: str, copy: Optional[bool]) -> None:
    """The images at `place` (or below it) copied wherever they are (True) or never (False): a rewrite's `localCopy`."""
    if copy is None or not place or "://" in place or place.startswith("/vsi"):
        return
    with _places_lock:
        _places[place_key(place)] = copy


def place_choice(path: str) -> Optional[bool]:
    """A rewrite's `localCopy` for an image (the nearest place given to mark_place), None when none said."""
    key = place_key(path)
    with _places_lock:
        places = dict(_places)
    best, choice = "", None
    for p, copy in places.items():
        if (key == p or key.startswith(p.rstrip(os.sep) + os.sep)) and len(p) > len(best):
            best, choice = p, copy
    return choice


_mounts: tuple = (0.0, [])


def _mount_table() -> list[tuple[str, str]]:
    """(mount point, type) of this computer (Linux: /proc/mounts), read again after a minute."""
    global _mounts
    at, table = _mounts
    if time.monotonic() - at < 60 and table:
        return table
    table = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    table.append((parts[1].replace("\\040", " "), parts[2]))
    except OSError:
        pass
    _mounts = (time.monotonic(), table)
    return table


def network_place(path: str) -> bool:
    """Whether an image is on another computer: `\\\\server\\share\\…`, a network drive (Windows), a network mount (Linux)."""
    if unc_path(path):
        return True
    if sys.platform == "win32":
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if len(drive) != 2 or drive[1] != ":":
            return False
        try:
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
        except (AttributeError, OSError):
            return False
    return (mount_type(path, _mount_table()) or "") in NETWORK_TYPES


def wanted(path: str, options: Optional[LocalCopyOptions] = None) -> bool:
    """Whether an image is to be copied: `localCopy` on, on a network folder (or a rewrite says so), at least `minMB`."""
    options = options or current
    if not options.enabled or not path or "://" in path or path.startswith("/vsi") or original_of(path):
        return False
    choice = place_choice(path)
    if choice is False or (choice is None and not network_place(path)):
        return False
    try:
        return big_enough(os.path.getsize(path), options)
    except OSError:
        return False


# ---- the copies -----------------------------------------------------------------------------------

#: The copies used in this QGIS: place_key(copy of the image) → the image.
_copies: dict = {}
_copies_lock = threading.Lock()


def _read_meta(folder: str) -> Optional[CopyInfo]:
    try:
        with open(os.path.join(folder, META), encoding="utf-8") as f:
            return meta_of(folder, json.load(f))
    except (OSError, ValueError):
        return None


def _write_meta(info: CopyInfo) -> None:
    path = os.path.join(info.folder, META)
    part = path + ".part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(info.to_json(), f, ensure_ascii=False)
    os.replace(part, path)


def _remember(copy: str, original: str) -> None:
    with _copies_lock:
        _copies[place_key(copy)] = original


def original_of(path: str) -> Optional[str]:
    """The image on the share a file is the copy of; None for any other file."""
    if not path:
        return None
    key = place_key(path)
    with _copies_lock:
        known = _copies.get(key)
    if known:
        return known
    folder = os.path.dirname(key)
    try:
        base = place_key(base_folder())
    except OSError:
        return None
    if os.path.dirname(folder) != base:
        return None
    info = _read_meta(folder)
    if info is None or place_key(info.image) != key:
        return None
    _remember(path, info.source)
    return info.source


def copy_of(path: str, options: Optional[LocalCopyOptions] = None) -> Optional[str]:
    """
    The copy of an image made before (this QGIS, or kept: "cache") when the
    image is still as it was copied and the copy whole; None otherwise.
    """
    options = options or current
    if not options.enabled or not path or "://" in path or path.startswith("/vsi"):
        return None
    folder = os.path.join(base_folder(options), copy_name(path))
    info = _read_meta(folder)
    if info is None or place_key(info.source) != place_key(path):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not still_same(info, st.st_size, st.st_mtime):
        return None
    if not all(os.path.isfile(os.path.join(folder, *f.split("/"))) for f in info.files):
        return None
    try:
        _write_meta(CopyInfo(info.folder, info.source, info.size, info.mtime, info.files, info.bytes, time.time()))
    except OSError:
        pass
    _remember(info.image, info.source)
    return info.image


def copies(folder: Optional[str] = None) -> list[CopyInfo]:
    """The whole copies in a folder of copies (base_folder by default)."""
    folder = folder or base_folder()
    out = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return out
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            info = _read_meta(entry.path)
            if info is not None:
                out.append(info)
    return out


def in_use(project: Optional[QgsProject] = None) -> set:
    """The folders of the copies the project's layers read (place_key)."""
    project = project or QgsProject.instance()
    out = set()
    for layer in project.mapLayers().values():
        path = overviews_io.local_path(layer)
        if path and original_of(path):
            out.add(place_key(os.path.dirname(path)))
    return out


def clear_unused(project: Optional[QgsProject] = None) -> tuple[int, int]:
    """Deletes the copies no layer reads (設定…): how many and their bytes."""
    busy = in_use(project)
    n = size = 0
    for info in copies():
        if place_key(info.folder) in busy:
            continue
        shutil.rmtree(info.folder, ignore_errors=True)
        if not os.path.exists(info.folder):
            n += 1
            size += info.bytes
    return n, size


@dataclass(frozen=True)
class Outcome:
    kind: str
    #: The image on the share.
    path: str
    #: Its copy (COPIED).
    copy: str = ""
    #: The bytes copied (COPIED), or needed (NO_ROOM).
    bytes: int = 0


def make_copy(path: str, options: LocalCopyOptions, busy: set = frozenset(),
              progress: Callable[[float], None] = lambda p: None, cancelled: Callable[[], bool] = lambda: False) -> Outcome:
    """
    Copies an image (and the files GDAL reads with it in its folder) to
    this PC, making room under `maxGB` from the copies not in `busy`
    (in_use). Raises Cancelled when `cancelled()` turns true (nothing is
    left), CopyError when it cannot be copied.
    """
    try:
        before = os.stat(path)
    except OSError as error:
        raise CopyError(detail=str(error)) from None
    ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
    if ds is None:
        raise CopyError(detail=gdal.GetLastErrorMsg() or path)
    shape = (ds.RasterXSize, ds.RasterYSize, ds.RasterCount)
    listed = ds.GetFileList() or []
    ds = None
    files = same_folder_files(path, listed)
    try:
        sizes = [os.path.getsize(f) for f in files]
    except OSError as error:
        raise CopyError(detail=str(error)) from None
    total = sum(sizes)
    try:
        base = base_folder(options, make=True)
        usage = shutil.disk_usage(base)
    except OSError as error:
        raise CopyError("手元のフォルダを作れない", detail=str(error)) from None
    evict = to_evict(copies(base), total, int(options.max_gb * GB), set(busy))
    freed = sum(c.bytes for c in evict or [])
    if evict is None or usage.free + freed - total < reserve_for(usage.total):
        return Outcome(NO_ROOM, path, bytes=total)
    for old in evict:
        shutil.rmtree(old.folder, ignore_errors=True)
    folder = os.path.join(base, copy_name(path))
    shutil.rmtree(folder, ignore_errors=True)
    names = [relative_name(path, f) for f in files]
    done = 0
    try:
        os.makedirs(folder, exist_ok=True)
        for f, name in zip(files, names):
            target = os.path.join(folder, *name.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            part = target + ".part"
            with open(f, "rb") as src, open(part, "wb") as dst:
                while True:
                    if cancelled():
                        raise Cancelled()
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
                    done += len(chunk)
                    progress(min(99.0, done * 99.0 / max(total, 1)))
            os.replace(part, target)
        after = os.stat(path)
        if not still_same(CopyInfo(folder, path, before.st_size, before.st_mtime, (), 0, 0), after.st_size, after.st_mtime):
            shutil.rmtree(folder, ignore_errors=True)
            return Outcome(CHANGED, path)
        image = os.path.join(folder, *names[0].split("/"))
        copied = gdal.OpenEx(image, gdal.OF_RASTER | gdal.OF_READONLY)
        ok = copied is not None and (copied.RasterXSize, copied.RasterYSize, copied.RasterCount) == shape
        copied = None
        if not ok:
            raise CopyError("コピーを画像として読めない", detail=image)
        _write_meta(CopyInfo(folder, path, before.st_size, before.st_mtime, tuple(names), done, time.time()))
    except OSError as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise CopyError(detail=str(error)) from None
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    _remember(image, path)
    progress(100.0)
    return Outcome(COPIED, path, image, done)


# ---- the layers -----------------------------------------------------------------------------------

def switch_layer(layer: QgsRasterLayer, copy: str, original: str) -> bool:
    """
    The layer reads the copy instead of the image, keeping its style, its
    correction's state and its CRS (as 位置ずらし does); back on the image
    when the copy cannot be opened. Whether it reads the copy.
    """
    from . import enhance_renderer as er

    crs = layer.crs()
    corrected = er.enhance_renderer(layer)
    state = corrected.state if corrected is not None else None
    options = QgsDataProvider.ProviderOptions()
    options.transformContext = QgsProject.instance().transformContext()
    layer.setCustomProperty(ORIGINAL, original)
    layer.setDataSource(QgsProviderRegistry.instance().encodeUri("gdal", {"path": copy}), layer.name(), "gdal", options)
    if not layer.isValid():
        layer.setDataSource(QgsProviderRegistry.instance().encodeUri("gdal", {"path": original}), layer.name(), "gdal", options)
        layer.removeCustomProperty(ORIGINAL)
        ok = False
    else:
        ok = True
    if crs.isValid() and layer.crs() != crs:
        layer.setCrs(crs)
    if layer.customProperty(er.PROPERTY):
        er.restore_layer(layer, state)
    layer.triggerRepaint()
    return ok


def switch_layers(original: str, copy: str, project: Optional[QgsProject] = None) -> list[QgsRasterLayer]:
    """The project's layers of an image switched to its copy (those read through a VRT, such as 位置ずらし, stay)."""
    project = project or QgsProject.instance()
    key = place_key(original)
    out = []
    for layer in list(project.mapLayers().values()):
        path = overviews_io.local_path(layer)
        if path and place_key(path) == key and switch_layer(layer, copy, original):
            out.append(layer)
    return out


def _written_source(layer: QgsMapLayer, project: QgsProject) -> Optional[str]:
    """How a project names a layer reading a copy: the image on the share (as QGIS writes a path); None for other layers."""
    original = layer.customProperty(ORIGINAL) if isinstance(layer, QgsRasterLayer) else None
    path = overviews_io.local_path(layer) if original else None
    if not (original and path and original_of(path)):
        return None
    return project.pathResolver().writePath(QgsProviderRegistry.instance().encodeUri("gdal", {"path": str(original)}))


def layer_written(layer: QgsMapLayer, element, doc, project: Optional[QgsProject] = None) -> bool:
    """
    A layer being saved in a project (QgsProject.writeMapLayer): one reading
    a copy is written with the image on the share (the temporary copy is gone
    by the next time; the copy is used again when it is opened). Whether it was.
    """
    project = project or QgsProject.instance()
    written = _written_source(layer, project)
    if written is None:
        return False
    source = element.firstChildElement("datasource")
    if source.isNull():
        return False
    while source.hasChildNodes():
        source.removeChild(source.firstChild())
    source.appendChild(doc.createTextNode(written))
    return True


def project_written(doc, project: Optional[QgsProject] = None) -> int:
    """
    A project being saved (QgsProject.writeProject, before its layers): the
    layer tree's entries of the layers reading a copy name the image on the
    share too. How many.
    """
    project = project or QgsProject.instance()
    sources = {}
    for layer in project.mapLayers().values():
        written = _written_source(layer, project)
        if written is not None:
            sources[layer.id()] = written
    n = 0
    tree = doc.elementsByTagName("layer-tree-layer")
    for i in range(tree.count()):
        element = tree.at(i).toElement()
        written = sources.get(element.attribute("id"))
        if written is not None and element.hasAttribute("source"):
            element.setAttribute("source", written)
            n += 1
    return n


# ---- the queue ------------------------------------------------------------------------------------

class CopyQueue:
    """The images waiting to be copied, copied one at a time."""

    def __init__(self):
        self._waiting: list[tuple[str, LocalCopyOptions]] = []
        self._task: Optional[FunctionTask] = None
        self._current: Optional[str] = None
        self.message_bar = None
        #: Images switched to their copy since the last message.
        self._switched = 0
        #: The images being copied whose pyramids were asked for (place_key → `overviews`): built once copied.
        self._pyramids: dict = {}
        #: Called with each Outcome, or the error (tests).
        self.finished: Optional[Callable[[Optional[Outcome], Optional[str]], None]] = None

    def pending(self) -> list[str]:
        return ([self._current] if self._current else []) + [p for p, _ in self._waiting]

    def request(self, layers: Sequence[QgsMapLayer], message_bar=None, pyramids=None) -> set:
        """
        The layers' images to copy queued, and those copied before switched to
        their copy now. The images being copied (now asked or earlier), as
        place_key: their pyramids (`pyramids`, the `overviews` asked with, from
        overviews_io) wait for the copy and are built from it.
        """
        if message_bar is not None:
            self.message_bar = message_bar
        busy = {place_key(p) for p in self.pending()}
        taken = set()
        options = current
        if not options.enabled:
            return taken
        start = False
        for layer in layers:
            path = overviews_io.local_path(layer)
            if not path:
                continue
            key = place_key(path)
            if key in busy:
                taken.add(key)
                if pyramids is not None:
                    self._pyramids[key] = pyramids
                continue
            if not wanted(path, options):
                continue
            made = copy_of(path, options)
            if made:
                # Copied before (this QGIS, or kept): read there at once.
                if switch_layers(path, made):
                    self._switched += 1
                    self._tell()
                continue
            self._waiting.append((path, options))
            busy.add(key)
            taken.add(key)
            if pyramids is not None:
                self._pyramids[key] = pyramids
            start = True
        if start:
            self._next()
        return taken

    def cancel_all(self) -> None:
        """Stops all (the plugin is unloaded)."""
        self._waiting.clear()
        self._pyramids.clear()
        if self._task is not None:
            self._task.cancel()
        self._task, self._current = None, None
        # The message bar of the QGIS window it was given (gone with a plugin reloaded, or a test's window).
        self.message_bar = None

    def _next(self) -> None:
        if self._task is not None or not self._waiting:
            return
        path, options = self._waiting.pop(0)
        self._current = path
        busy = in_use()

        def work(task: FunctionTask) -> Outcome:
            return make_copy(path, options, busy, task.progress, task.isCanceled)

        self._task = run_task(f"手元へのコピー（{os.path.basename(path)}）", work,
                              lambda outcome: self._ended(outcome, None),
                              lambda error: self._ended(None, error))

    def _ended(self, outcome: Optional[Outcome], error: Optional[str]) -> None:
        path, self._current, self._task = self._current, None, None
        name = os.path.basename(path or "")
        bar = self.message_bar
        pyramids = self._pyramids.pop(place_key(path), None) if path else None
        try:
            switched = switch_layers(outcome.path, outcome.copy) if outcome is not None and outcome.kind == COPIED else []
            if switched:
                self._switched += 1
            if switched and pyramids is not None:
                # Its pyramids, from the copy (put beside the image on the share too where they would have gone).
                overviews_io.queue.request(switched, pyramids, bar)
            elif path and pyramids is not None and not switched:
                # Not copied (or no layer reads it any more): its pyramids from the share, as without localCopy.
                layers = [layer for layer in QgsProject.instance().mapLayers().values()
                          if overviews_io.local_path(layer) and place_key(overviews_io.local_path(layer)) == place_key(path)]
                if layers:
                    overviews_io.queue.request(layers, pyramids, bar)
            if outcome is not None and outcome.kind == NO_ROOM and bar is not None:
                bar.pushMessage(TITLE, f"{name} は手元にコピーせず共有フォルダから表示（手元の空きか localCopy.maxGB が足りない。"
                                       f"{outcome.bytes / GB:.1f} GB）", Qgis.MessageLevel.Info, 8)
            elif outcome is not None and outcome.kind == CHANGED and bar is not None:
                bar.pushMessage(TITLE, f"{name} はコピー中に共有フォルダで変わったので、共有フォルダから表示", Qgis.MessageLevel.Info, 8)
            elif error is not None and error != "取消":
                # The task logged it with its detail.
                if bar is not None:
                    bar.pushMessage(TITLE, f"{name} を手元にコピーできない（共有フォルダから表示）: {error}", Qgis.MessageLevel.Warning, 10)
            if self.finished is not None:
                self.finished(outcome, error)
        finally:
            self._next()
            if not self.pending():
                self._tell()

    def _tell(self) -> None:
        """Once nothing is waiting: how many images are now read from this PC."""
        n, self._switched = self._switched, 0
        if n and self.pending():
            self._switched = n
            return
        if n and self.message_bar is not None:
            self.message_bar.pushMessage(
                TITLE, f"{n} 枚の画像を手元にコピーし、手元から表示（共有フォルダの画像はそのまま。"
                       f"{'QGIS を閉じると消去' if current.location == 'temp' else '次回も使う'}）",
                Qgis.MessageLevel.Info, 5)


#: The plugin's queue.
queue = CopyQueue()


def end_session() -> int:
    """
    Deletes this QGIS's temporary copies (`location` "temp": the plugin is
    unloaded as QGIS closes). What is still open goes at the next start.
    How many copies are left.
    """
    if current.location != "temp":
        return 0
    folder = base_folder()
    if not os.path.isdir(folder):
        return 0
    shutil.rmtree(folder, ignore_errors=True)
    left = len(copies(folder)) if os.path.isdir(folder) else 0
    if left:
        QgsMessageLog.logMessage(f"手元のコピーのうち使用中の {left} 件は次の起動時に消去（{folder}）", LOG_TAG, Qgis.MessageLevel.Info)
    with _copies_lock:
        _copies.clear()
    return left


def size_of(folder: Optional[str] = None) -> tuple[int, int]:
    """The copies in the folder of copies: how many and their bytes."""
    found = copies(folder)
    return len(found), sum(c.bytes for c in found)
