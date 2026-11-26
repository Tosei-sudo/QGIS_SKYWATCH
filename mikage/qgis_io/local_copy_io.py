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
- An image not copied (an error, no room, changed while copied, the copy
  cancelled) is read from the share, and not copied again in this QGIS until
  it changes there (another size or time; 「使っていないコピーを消去」 for no
  room). Its pyramids were asked for again from the share, which asked for
  its copy again, without end (tsudo 2026-11-27: 「手元への画像コピーが失敗
  するときがある」「リトライし続けているならそれもやめてほしい」).
- Windows: a file just written is read by the antivirus for a while, and a
  file open in QGIS (a layer, its pixels' GDAL datasets) cannot be removed
  or replaced. A rename refused is tried again for about 10 s; a copy's
  folder that cannot be emptied (an older copy still open) is left as it is
  and the new copy goes beside it (`<name>-2`…); a copy is deleted with its
  `mikage-copy.json` last, so one partly deleted is never taken as whole and
  goes the next time room is made; copies still read when the plugin stops
  (reloaded, updated) are kept whole.
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
    GB, MB, META, NETWORK_TYPES, CopyInfo, LocalCopyOptions, big_enough, copy_name, meta_of, mount_type, place_key,
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
#: Pauses (s) between tries of a rename Windows refuses (an antivirus reading the file just written, as
#: core/win64.RENAME_WAITS; a large image takes longer): about 10 s in all.
REPLACE_WAITS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0, 4.0)


def _log(text: str, level=Qgis.MessageLevel.Info) -> None:
    """A step of the copies in the log (ログメッセージの「MIKAGE」タブ, 「手元のコピー: …」; any thread), to follow what happened
    (tsudo 2026-11-27: 「できる限り検証のため詳しいログを出しておいて」)."""
    QgsMessageLog.logMessage(f"{TITLE}: {text}", LOG_TAG, level, notifyUser=False)


def _mb(n: int) -> str:
    return f"{n / 1024 ** 2:,.0f} MB" if n < GB else f"{n / GB:.2f} GB"


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


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
    return not why_not(path, options)


def why_not(path: str, options: Optional[LocalCopyOptions] = None) -> str:
    """Why an image is not to be copied ('' when it is): as wanted, the reason for the log."""
    options = options or current
    if not options.enabled:
        return "localCopy が false"
    if not path or "://" in path or path.startswith("/vsi"):
        return "URL の画像"
    if original_of(path):
        return "手元のコピーそのもの"
    choice = place_choice(path)
    if choice is False:
        return "読み替えの localCopy が false"
    if choice is None and not network_place(path):
        return "ネットワーク上のフォルダでない"
    try:
        size = os.path.getsize(path)
    except OSError as error:
        return f"大きさを読めない（{error}）"
    if not big_enough(size, options):
        return f"小さい（{_mb(size)}。minMB {options.min_mb}）"
    return ""


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


def _write_meta(info: CopyInfo, waits: Sequence[float] = ()) -> None:
    path = os.path.join(info.folder, META)
    part = path + ".part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(info.to_json(), f, ensure_ascii=False)
    replace(part, path, waits)


def _refused(error: OSError) -> bool:
    """Whether Windows refused a file in use (by QGIS, or an antivirus reading it): access denied, a sharing violation."""
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) in (5, 32, 33)


def replace(part: str, target: str, waits: Sequence[float] = REPLACE_WAITS,
            cancelled: Callable[[], bool] = lambda: False) -> None:
    """os.replace, tried again after each of `waits` while Windows refuses it (a file in use); the last refusal raised."""
    tries = 0
    for wait in (*waits, None):
        try:
            os.replace(part, target)
            if tries:
                _log(f"{os.path.basename(target)} の名前の付け替えが {tries + 1} 回目にできた")
            return
        except OSError as error:
            tries += 1
            if wait is None or not _refused(error) or cancelled():
                _log(f"{os.path.basename(part)} → {os.path.basename(target)} の名前の付け替えができない（{tries} 回目、{error}）",
                     Qgis.MessageLevel.Warning)
                raise
            _log(f"{os.path.basename(target)} の名前の付け替えを Windows が拒否（{error}）。{wait:g} 秒待ってやり直す（{tries} 回目）")
            time.sleep(wait)


def drop(folder: str) -> bool:
    """
    Deletes a copy's folder, its `mikage-copy.json` last (a folder partly
    deleted, a file still open on Windows, is never taken as a whole copy:
    copy_of finds its files missing, and room made later deletes it again).
    Whether it is gone.
    """
    if not os.path.lexists(folder):
        return True
    meta = os.path.join(folder, META)
    left = []
    for root, dirs, files in os.walk(folder, topdown=False):
        for name in files:
            f = os.path.join(root, name)
            if f == meta:
                continue
            try:
                os.remove(f)
            except OSError as error:
                left.append(f"{os.path.relpath(f, folder)}（{error}）")
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError as error:
                left.append(f"{name}/（{error}）")
    if not left:
        try:
            if os.path.exists(meta):
                os.remove(meta)
            os.rmdir(folder)
        except OSError as error:
            left.append(str(error))
    if left:
        # (Windows: a file still open, in QGIS or its GDAL, or read by an antivirus.)
        _log(f"{folder} を消せず（使用中のファイル）: " + "、".join(left[:5]) + ("…" if len(left) > 5 else ""),
             Qgis.MessageLevel.Warning)
        return False
    return not os.path.lexists(folder)


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
    found = []
    for folder in _folders_of(path, base_folder(options)):
        info = _read_meta(folder)
        if info is not None and place_key(info.source) == place_key(path):
            found.append(info)
    if not found:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    # The one used last of those whole and of the image as it is (an older one may be left open: drop).
    for info in sorted(found, key=lambda i: i.used, reverse=True):
        if not still_same(info, st.st_size, st.st_mtime):
            _log(f"{info.folder} は使わない: 共有フォルダの画像が変わった（大きさ {info.size:,} → {st.st_size:,}、"
                 f"更新日時 {_stamp(info.mtime)} → {_stamp(st.st_mtime)}）")
            continue
        missing = [f for f in info.files if not os.path.isfile(os.path.join(info.folder, *f.split("/")))]
        if missing:
            _log(f"{info.folder} は使わない: ファイルが欠けている（{'、'.join(missing)}）")
            continue
        try:
            _write_meta(CopyInfo(info.folder, info.source, info.size, info.mtime, info.files, info.bytes, time.time()))
        except OSError:
            pass
        _remember(info.image, info.source)
        return info.image
    return None


def _folders_of(path: str, base: str) -> list[str]:
    """The folders an image's copies may be in: `<name>`, and `<name>-2`… made while an older one could not be deleted."""
    name = copy_name(path)
    try:
        entries = [e.name for e in os.scandir(base) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return [os.path.join(base, n) for n in entries if n == name or (n.startswith(name + "-") and n[len(name) + 1:].isdigit())]


def _fresh_folder(path: str, base: str, busy: set) -> str:
    """
    An empty folder for an image's new copy: its own (`<name>`), emptied, or
    when that cannot be (Windows: an older copy still open) or is read now
    (`busy`), the first of `<name>-2`… that can.
    """
    name = copy_name(path)
    skipped = []
    for i in range(1, 100):
        folder = os.path.join(base, name if i == 1 else f"{name}-{i}")
        if place_key(folder) in busy:
            skipped.append(f"{os.path.basename(folder)}（表示中）")
            continue
        if drop(folder):
            if skipped:
                _log(f"{os.path.basename(path)} の前のコピーのフォルダ {'、'.join(skipped)} を空けられないので {folder} にコピー")
            return folder
        skipped.append(f"{os.path.basename(folder)}（使用中のファイル）")
    raise CopyError("手元のフォルダを空けられない", detail=os.path.join(base, name))


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
        if drop(info.folder):
            n += 1
            size += info.bytes
    _log(f"使っていないコピーを消去: {n} 件、{_mb(size)}（表示中 {len(busy)} 件は残す）")
    # Room made: the images not copied for want of it may be tried again.
    queue.forget(NO_ROOM)
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
    started = time.perf_counter()
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
    have = copies(base)
    evict = to_evict(have, total, int(options.max_gb * GB), set(busy))
    freed = sum(c.bytes for c in evict or [])
    room = (f"コピー {_mb(total)}、今のコピー {len(have)} 件 {_mb(sum(c.bytes for c in have))}（表示中 {len(busy)} 件）、"
            f"上限 {options.max_gb:g} GB、ディスクの空き {_mb(usage.free)}（残す {_mb(reserve_for(usage.total))}）")
    if evict is None or usage.free + freed - total < reserve_for(usage.total):
        _log(f"{os.path.basename(path)} は入らないのでコピーしない: {room}" +
             ("。表示していないコピーを全部消しても上限を超える" if evict is None else ""), Qgis.MessageLevel.Warning)
        return Outcome(NO_ROOM, path, bytes=total)
    if evict:
        gone = [old for old in evict if drop(old.folder)]
        _log(f"場所を空けるため使っていないコピーを {len(gone)} / {len(evict)} 件消去（{_mb(sum(c.bytes for c in gone))}）: "
             + "、".join(os.path.basename(c.source) for c in evict))
        try:
            # As deleted (a copy still open on Windows stays).
            usage = shutil.disk_usage(base)
        except OSError:
            pass
        if usage.free - total < reserve_for(usage.total):
            _log(f"{os.path.basename(path)} は入らないのでコピーしない（消せないコピーがある）: ディスクの空き {_mb(usage.free)}",
                 Qgis.MessageLevel.Warning)
            return Outcome(NO_ROOM, path, bytes=total)
    folder = _fresh_folder(path, base, set(busy))
    _log(f"コピー開始: {path} → {folder}（{len(files)} ファイル {_mb(total)}: {'、'.join(os.path.basename(f) for f in files)}。"
         f"共有フォルダの画像 {before.st_size:,} バイト、更新日時 {_stamp(before.st_mtime)}。{room}）")
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
            # (Windows: the antivirus reads the file just written, and the rename waits for it.)
            replace(part, target, REPLACE_WAITS, cancelled)
        after = os.stat(path)
        if not still_same(CopyInfo(folder, path, before.st_size, before.st_mtime, (), 0, 0), after.st_size, after.st_mtime):
            _log(f"{os.path.basename(path)} はコピー中に共有フォルダで変わった（大きさ {before.st_size:,} → {after.st_size:,}、"
                 f"更新日時 {_stamp(before.st_mtime)} → {_stamp(after.st_mtime)}）。コピーは消して共有フォルダから表示",
                 Qgis.MessageLevel.Warning)
            drop(folder)
            return Outcome(CHANGED, path)
        image = os.path.join(folder, *names[0].split("/"))
        copied = gdal.OpenEx(image, gdal.OF_RASTER | gdal.OF_READONLY)
        ok = copied is not None and (copied.RasterXSize, copied.RasterYSize, copied.RasterCount) == shape
        copied = None
        if not ok:
            raise CopyError("コピーを画像として読めない", detail=f"{image}（{gdal.GetLastErrorMsg() or '大きさかバンド数が違う'}）")
        _write_meta(CopyInfo(folder, path, before.st_size, before.st_mtime, tuple(names), done, time.time()), REPLACE_WAITS)
    except OSError as error:
        _log(f"{os.path.basename(path)} のコピーに失敗（{_mb(done)} まで）: {error!r}", Qgis.MessageLevel.Warning)
        drop(folder)
        raise CopyError(detail=str(error)) from None
    except BaseException as error:
        _log(f"{os.path.basename(path)} のコピーを中止（{_mb(done)} まで）: " +
             ("取り消し" if isinstance(error, Cancelled) else repr(error)), Qgis.MessageLevel.Info)
        drop(folder)
        raise
    _remember(image, path)
    took = time.perf_counter() - started
    _log(f"コピー完了: {image}（{_mb(done)}、{took:.1f} 秒、{done / MB / max(took, 0.001):.0f} MB/秒）")
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
        why = layer.error().summary() if layer.error() else ""
        layer.setDataSource(QgsProviderRegistry.instance().encodeUri("gdal", {"path": original}), layer.name(), "gdal", options)
        layer.removeCustomProperty(ORIGINAL)
        _log(f"レイヤー「{layer.name()}」を手元のコピーに切り替えられず共有フォルダのまま（{copy} を開けない: "
             f"{why or gdal.GetLastErrorMsg() or '理由不明'}）", Qgis.MessageLevel.Warning)
        ok = False
    else:
        _log(f"レイヤー「{layer.name()}」を手元のコピーから表示: {copy}")
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
        #: Images switched to their copy since the last message: copied now, and copied before (reopened).
        self._switched = 0
        self._reused = 0
        #: Images not copied in this QGIS (place_key → (why, size, time of the image then)): read from the share,
        #: not copied again until the image changes there (tsudo 2026-11-27: 「リトライし続けているならそれもやめてほしい」).
        self._given_up: dict = {}
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
            name = os.path.basename(path)
            if key in busy:
                taken.add(key)
                if pyramids is not None:
                    self._pyramids[key] = pyramids
                _log(f"{name}（レイヤー「{layer.name()}」）はコピー{'中' if self._current and place_key(self._current) == key else '待ち'}"
                     "。終わったらこのレイヤーも手元から表示")
                continue
            why = why_not(path, options)
            if why:
                if why not in ("ネットワーク上のフォルダでない", "手元のコピーそのもの") or place_choice(path) is not None:
                    _log(f"{name}（レイヤー「{layer.name()}」）はコピーしない: {why}")
                continue
            made = copy_of(path, options)
            if made:
                # Copied before (this QGIS, or kept): read there at once.
                _log(f"{name}（レイヤー「{layer.name()}」）は前のコピーを使う: {made}")
                if switch_layers(path, made):
                    self._reused += 1
                    self._tell()
                continue
            given = self.given_up(path)
            if given:
                _log(f"{name}（レイヤー「{layer.name()}」）は前に手元にコピーできなかったのでコピーしない（{given}）。"
                     "共有フォルダから表示")
                continue
            _log(f"{name}（レイヤー「{layer.name()}」）をコピー待ちに追加（待ち {len(self._waiting) + 1} 件"
                 f"{'、コピー中 ' + os.path.basename(self._current) if self._current else ''}）")
            self._waiting.append((path, options))
            busy.add(key)
            taken.add(key)
            if pyramids is not None:
                self._pyramids[key] = pyramids
            start = True
        if start:
            self._next()
        return taken

    def given_up(self, path: str) -> Optional[str]:
        """Why an image was not copied in this QGIS (NO_ROOM, CHANGED, an error, "取消"), while it is as it was then; else None."""
        known = self._given_up.get(place_key(path))
        if known is None:
            return None
        why, size, mtime = known
        try:
            st = os.stat(path)
        except OSError:
            return why
        if st.st_size == size and abs(st.st_mtime - mtime) < 2.0:
            return why
        # Changed on the share since (replaced, or written to the end): tried again.
        self._given_up.pop(place_key(path), None)
        return None

    def forget(self, why: Optional[str] = None) -> None:
        """The images not copied (for `why`, or all) may be tried again (room made: clear_unused)."""
        for key, known in list(self._given_up.items()):
            if why is None or known[0] == why:
                del self._given_up[key]

    def _give_up(self, path: str, why: str) -> None:
        try:
            st = os.stat(path)
            self._given_up[place_key(path)] = (why, st.st_size, st.st_mtime)
        except OSError:
            self._given_up[place_key(path)] = (why, -1, 0.0)
        _log(f"{os.path.basename(path)} はこの QGIS ではもうコピーしない（{why}）。共有フォルダの画像が変わったら"
             "またコピーする（空き不足は「使っていないコピーを消去」でも）")

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
        _log(f"{os.path.basename(path)} のコピーを始める（あと {len(self._waiting)} 件待ち）")

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
            copied = outcome is not None and outcome.kind == COPIED
            if path and not copied:
                # Not copied (cancelled too): not tried again for this image as it is now, as its pyramids are asked
                # for below from the share (which asked for its copy again, and failed again, without end).
                self._give_up(path, outcome.kind if outcome is not None else (error or "失敗"))
            switched = switch_layers(outcome.path, outcome.copy) if copied else []
            if switched:
                self._switched += 1
            elif copied:
                _log(f"{name} のコピーはできたが、その画像のレイヤーはもう開いていない（次に開くときに使う）")
            if error is not None:
                _log(f"{name} のコピーが終わらず: {error}", Qgis.MessageLevel.Info if error == "取消" else Qgis.MessageLevel.Warning)
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
                    bar.pushMessage(TITLE, f"{name} を手元にコピーできない（共有フォルダから表示。この QGIS ではもうコピーしない）: {error}",
                                    Qgis.MessageLevel.Warning, 10)
            if self.finished is not None:
                self.finished(outcome, error)
        finally:
            self._next()
            if not self.pending():
                self._tell()

    def _tell(self) -> None:
        """Once nothing is waiting: how many images are now read from this PC (copied now, or copied before)."""
        if self.pending() or not (self._switched or self._reused):
            return
        n, again, self._switched, self._reused = self._switched, self._reused, 0, 0
        if self.message_bar is None:
            return
        if n:
            text = f"{n + again} 枚の画像を手元にコピーし、手元から表示（共有フォルダの画像はそのまま。" \
                   f"{'QGIS を閉じると消去' if current.location == 'temp' else '次回も使う'}）"
        else:
            text = f"{again} 枚の画像を前に手元にコピーしたもので表示（共有フォルダの画像はそのまま）"
        self.message_bar.pushMessage(TITLE, text, Qgis.MessageLevel.Info, 5)


#: The plugin's queue.
queue = CopyQueue()


def end_session(project: Optional[QgsProject] = None) -> int:
    """
    Deletes this QGIS's temporary copies (`location` "temp": the plugin is
    unloaded as QGIS closes). Those still read by a layer are kept whole (the
    plugin reloaded or updated: they are still copies after; on Windows they
    could not be deleted anyway), and go at the next start. How many are left.
    """
    if current.location != "temp":
        return 0
    folder = base_folder()
    if not os.path.isdir(folder):
        return 0
    try:
        busy = in_use(project)
    except Exception:  # noqa: BLE001 (QGIS closing)
        busy = set()
    try:
        entries = [e.path for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)]
    except OSError:
        entries = []
    gone = kept = 0
    for entry in entries:
        if place_key(entry) in busy:
            kept += 1
        elif drop(entry):
            gone += 1
    try:
        if not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass
    left = len(copies(folder)) if os.path.isdir(folder) else 0
    _log(f"プラグインの停止: 一時フォルダのコピー {gone} 件を消去、表示中の {kept} 件は残す（{folder}。次の起動時に消去）"
         if kept else f"プラグインの停止: 一時フォルダのコピー {gone} 件を消去（{folder}）")
    with _copies_lock:
        _copies.clear()
    return left


def size_of(folder: Optional[str] = None) -> tuple[int, int]:
    """The copies in the folder of copies: how many and their bytes."""
    found = copies(folder)
    return len(found), sum(c.bytes for c in found)
