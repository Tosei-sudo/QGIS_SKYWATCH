"""
手元にコピーして見る (`localCopy`). tsudo, 2026-11-27: 「ラスター描画、もっと早く
できないかな… 4分割の大きい画像とかとてもじゃないけど…ネットワークに依存する部分
も大きい？」, then chose 「A 手元にコピーして見る」 of the study.

A large image on a network folder (a share, `\\\\server\\…` or a network
drive) is copied whole, in the background, to a folder on this PC; once
copied, its layers read the copy. Measured (QGIS 3.34, GDAL 3.8, a scene in
4 images of 12000 × 12000 × 3 × 16 bit, a share of 0.5 ms and 1 Gbps / 5 ms
and 100 Mbps): without pyramids the whole scene took 5–24 s / 44–181 s on
the share; an image in strips 1.5 s / 12.5 s a screen at full resolution
even with them; read here, 0.05–0.3 s. Building its pyramids from the copy
took 13 s an image against 20 s from the share, and the copy reads the
image once, in order, as the build from the share did anyway.

    "localCopy": false                      // never copy
    "localCopy": {"minMB": 300,             // images at least this big (MB; 0: all)
                  "location": "temp",       // "temp": deleted when QGIS closes; "cache": kept for next time
                  "folder": "",             // "": %TEMP%\\MIKAGE\\copies or %LOCALAPPDATA%\\MIKAGE\\copies
                  "maxGB": 30}              // the copies at most (those not in use, least recently used first)

Omitted, images of 300 MB and more on network folders are copied to the
temporary folder. A rewrite of `pathMappings` (`pattern` / `to`) may say
`"localCopy": true` (its images are copied wherever they are) or `false`
(never copied) for the images it finds.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .overviews import expand_folder

#: Where the copies go: deleted when QGIS closes, or kept (up to `maxGB`).
LOCATIONS = ("temp", "cache")
#: The folder under the system's temporary folder (location "temp") and under %LOCALAPPDATA% (or ~/.cache: "cache").
SUBFOLDER = os.path.join("MIKAGE", "copies")
#: The file in each copy's folder that tells which image it is a copy of (written last: a folder without it is unfinished).
META = "mikage-copy.json"
#: What a copy needs to keep free on its disk besides itself: this share of the disk, at least RESERVE_MIN bytes.
RESERVE_SHARE = 0.05
RESERVE_MIN = 2 * 1024 ** 3
MB = 1024 ** 2
GB = 1024 ** 3


@dataclass(frozen=True)
class LocalCopyOptions:
    #: Whether large images on network folders are copied at all.
    enabled: bool = True
    #: The smallest image copied (MB): a small one reads fast enough from the share.
    min_mb: int = 300
    #: "temp" (deleted when QGIS closes) or "cache" (kept for the next time, up to `max_gb`).
    location: str = "temp"
    #: The folder ("" for the default of `location`); environment variables as `%TEMP%` may be written.
    folder: str = ""
    #: The copies' total at most (GB): those not in use go, least recently used first, to make room.
    max_gb: float = 30.0

    def to_json(self) -> dict:
        return {"enabled": self.enabled, "minMB": self.min_mb, "location": self.location, "folder": self.folder,
                "maxGB": self.max_gb}


DEFAULT_LOCAL_COPY = LocalCopyOptions()


def local_copy_of(value: Any, problems: list[str]) -> LocalCopyOptions:
    """The settings file's `localCopy` (true / false / an object); the defaults, with the reason in `problems`, for what cannot be read."""
    if isinstance(value, bool):
        return LocalCopyOptions(enabled=value)
    if not isinstance(value, dict):
        problems.append("localCopy は true・false かオブジェクトで指定")
        return DEFAULT_LOCAL_COPY
    d = DEFAULT_LOCAL_COPY
    enabled = value.get("enabled", d.enabled)
    if not isinstance(enabled, bool):
        problems.append("localCopy.enabled は true か false で指定")
        enabled = d.enabled
    min_mb = value.get("minMB", d.min_mb)
    if not (isinstance(min_mb, (int, float)) and not isinstance(min_mb, bool) and min_mb >= 0):
        problems.append("localCopy.minMB は 0 以上の数（MB）で指定")
        min_mb = d.min_mb
    location = value.get("location", d.location)
    location = location.strip().lower() if isinstance(location, str) else location
    if location not in LOCATIONS:
        problems.append("localCopy.location は temp（QGIS を閉じたら消す）か cache（次回も使う）で指定")
        location = d.location
    folder = value.get("folder", d.folder)
    if not isinstance(folder, str):
        problems.append("localCopy.folder はフォルダのパス（文字列）で指定")
        folder = d.folder
    max_gb = value.get("maxGB", d.max_gb)
    if not (isinstance(max_gb, (int, float)) and not isinstance(max_gb, bool) and max_gb > 0):
        problems.append("localCopy.maxGB は 0 より大きい数（GB）で指定")
        max_gb = d.max_gb
    return LocalCopyOptions(enabled, int(min_mb), location, folder.strip(), float(max_gb))


def default_cache_root(environ=None) -> str:
    """Where `location` "cache" keeps the copies by default: %LOCALAPPDATA% (never the roaming profile), else ~/.cache."""
    environ = os.environ if environ is None else environ
    base = environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, SUBFOLDER)


def root_of(options: LocalCopyOptions, environ=None) -> str:
    """The folder of the copies: `folder` (its variables filled in), else the default of `location`."""
    chosen = expand_folder(options.folder, environ)
    if chosen:
        return chosen
    if options.location == "cache":
        return default_cache_root(environ)
    return os.path.join(tempfile.gettempdir(), SUBFOLDER)


def place_key(path: str) -> str:
    """An image's place as compared on this computer (case and `\\` / `/` ignored on Windows)."""
    return os.path.normcase(os.path.abspath(path))


def copy_name(path: str) -> str:
    """The folder of an image's copy: 16 letters of its place, so two images of one name never meet."""
    return hashlib.sha1(place_key(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def big_enough(size: int, options: LocalCopyOptions) -> bool:
    return size >= options.min_mb * MB


def reserve_for(disk_total: int) -> int:
    """What stays free on a disk after a copy."""
    return max(RESERVE_MIN, int(disk_total * RESERVE_SHARE))


def same_folder_files(image: str, files: Iterable[str]) -> list[str]:
    """
    The files of an image (GDAL's file list) that go with its copy: the image
    and those in its folder or below it (.ovr, .aux.xml, .tfw, .RPB, .IMD…),
    each once, the image first. Files elsewhere (GDAL's cache folder's
    .aux.xml, a VRT's sources) stay where they are.
    """
    folder = place_key(os.path.dirname(os.path.abspath(image)))
    out, seen = [], set()
    for f in [image, *files]:
        if not f:
            continue
        key = place_key(f)
        if key in seen:
            continue
        inside = os.path.dirname(key) == folder or key.startswith(folder.rstrip(os.sep) + os.sep)
        if not inside:
            continue
        seen.add(key)
        out.append(f)
    return out


def relative_name(image: str, f: str) -> str:
    """Where a file of an image goes in its copy's folder (its path below the image's folder, with `/`)."""
    return os.path.relpath(os.path.abspath(f), os.path.dirname(os.path.abspath(image))).replace(os.sep, "/")


@dataclass(frozen=True)
class CopyInfo:
    """A copy's folder, as its META file tells (read_meta)."""
    folder: str
    source: str
    size: int
    mtime: float
    files: tuple[str, ...]
    bytes: int
    used: float

    @property
    def image(self) -> str:
        """The copy of the image itself (its first file)."""
        return os.path.join(self.folder, *self.files[0].split("/")) if self.files else ""

    def to_json(self) -> dict:
        return {"source": self.source, "size": self.size, "mtime": self.mtime, "files": list(self.files),
                "bytes": self.bytes, "used": self.used}


def meta_of(folder: str, value: Any) -> Optional[CopyInfo]:
    """A copy's META as read (json), None when it is not one."""
    if not isinstance(value, dict):
        return None
    try:
        files = tuple(str(f) for f in value["files"])
        if not files or any(not f or f.startswith("/") or ".." in f.split("/") for f in files):
            return None
        return CopyInfo(folder, str(value["source"]), int(value["size"]), float(value["mtime"]), files,
                        int(value.get("bytes", 0)), float(value.get("used", 0)))
    except (KeyError, TypeError, ValueError):
        return None


def still_same(info: CopyInfo, size: int, mtime: float) -> bool:
    """Whether the image is as it was copied (a replaced image is copied again)."""
    return info.size == size and abs(info.mtime - mtime) < 2.0


def to_evict(copies: Sequence[CopyInfo], need: int, most: int, in_use: set) -> Optional[list[CopyInfo]]:
    """
    The copies to delete so that `need` more bytes fit under `most`: those
    not in use (their folder's place_key not in `in_use`), least recently used
    first; None when even all of them would not make room.
    """
    total = sum(c.bytes for c in copies)
    if total + need <= most:
        return []
    out = []
    for c in sorted(copies, key=lambda c: c.used):
        if place_key(c.folder) in in_use:
            continue
        out.append(c)
        total -= c.bytes
        if total + need <= most:
            return out
    return None


#: A mount point's file system types that are another computer's (Linux, macOS: /proc/mounts, `mount`).
NETWORK_TYPES = {"nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs", "afs", "ncpfs", "9p", "ceph", "glusterfs",
                 "davfs", "fuse.sshfs", "sshfs", "fuse.rclone", "fuse.davfs2", "webdav"}


def unc_path(path: str) -> bool:
    """`\\\\server\\share\\…` or `//server/share/…` (not `\\\\?\\C:\\…`, a local long path)."""
    p = path.replace("\\", "/")
    return p.startswith("//") and not re.match(r"^//[?.]/[A-Za-z]:", p)


def mount_type(path: str, mounts: Sequence[tuple[str, str]]) -> Optional[str]:
    """The file system type of the longest mount point holding `path` ((mount point, type) pairs)."""
    best, kind = "", None
    p = os.path.abspath(path)
    for point, fstype in mounts:
        point = point.rstrip("/") or "/"
        if (p == point or p.startswith(point.rstrip("/") + "/")) and len(point) > len(best):
            best, kind = point, fstype
    return kind
