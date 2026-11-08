"""
The libraries the Windows zip carries in ext/win64 (scripts/build_zip.py
--win64), put on Python's path.

A package with native code (.pyd/.dll: onnxruntime, the C++ sgp4) is not
loaded from the plugin's folder but from a copy of it outside, in the
person's local application data. Windows keeps a loaded DLL locked until
QGIS quits, and Python cannot unload one, so a DLL loaded from the plugin's
folder made QGIS fail to update the plugin from a zip ("Failed to unzip the
plugin package") or to uninstall it ("Failed to remove the directory").
The copy is named after its contents, so a new zip with other libraries
gets a new copy, and older copies are removed when nothing holds them.
Pure-Python packages (tzdata) are read from the plugin's folder as they are.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from typing import Optional

#: Where the Windows zip keeps its libraries.
FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ext", "win64")
NATIVE = (".pyd", ".dll", ".so")


def cache_root() -> str:
    """Where packages with native code are copied: the local application data (Windows), else the temporary folder."""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "skywatch", "win64")


def _files(package_dir: str) -> list[str]:
    out = []
    for folder, dirs, files in os.walk(package_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        out += [os.path.join(folder, n) for n in sorted(files) if not n.endswith(".pyc")]
    return out


def _digest(package_dir: str, files: list[str]) -> str:
    h = hashlib.sha1()
    for path in files:
        h.update(os.path.relpath(path, package_dir).replace(os.sep, "/").encode("utf-8") + b"\0")
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()[:16]


def _copy(package: str, package_dir: str, files: list[str], root: str) -> str:
    """The folder holding a copy of `package` (made when missing), with older copies of it removed when possible."""
    name = f"{package}-{_digest(package_dir, files)}"
    target = os.path.join(root, name)
    if not os.path.isdir(target):
        os.makedirs(root, exist_ok=True)
        work = tempfile.mkdtemp(prefix=f".{name}-", dir=root)
        try:
            shutil.copytree(package_dir, os.path.join(work, package), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            try:
                os.rename(work, target)  # Whole or not at all; another QGIS may have made it meanwhile.
            except OSError:
                if not os.path.isdir(target):
                    raise
        finally:
            if os.path.isdir(work):
                shutil.rmtree(work, ignore_errors=True)
    for other in os.listdir(root):
        if other != name and (other.startswith(f"{package}-") or other.startswith(f".{package}-")):
            _remove_unused(os.path.join(root, other))
    return target


def _in_use_here(folder: str) -> bool:
    """Whether this QGIS has the copy on its path or has imported from it (a plugin reloaded keeps the old one)."""
    folder = os.path.normcase(os.path.abspath(folder))
    if any(os.path.normcase(os.path.abspath(p)) == folder for p in sys.path if isinstance(p, str) and p):
        return True
    for module in list(sys.modules.values()):
        f = getattr(module, "__file__", None)
        if isinstance(f, str) and os.path.normcase(os.path.abspath(f)).startswith(folder + os.sep):
            return True
    return False


def _remove_unused(folder: str) -> None:
    """
    Removes an older copy only when no QGIS uses it (#82): not one on this
    QGIS's path, and only after the whole folder could be renamed away
    (Windows refuses that while another QGIS has one of its DLLs loaded), so
    a copy in use is left whole rather than with its .py files gone.
    """
    if _in_use_here(folder):
        return
    doomed = f"{folder}.removing-{os.getpid()}"
    try:
        os.rename(folder, doomed)
    except OSError:
        return  # In use (or already gone): kept as it is, removed another time.
    shutil.rmtree(doomed, ignore_errors=True)


_added: dict[str, Optional[str]] = {}


def add_package(package: str, folder: str = FOLDER, root: Optional[str] = None, first: bool = False) -> Optional[str]:
    """Put the Windows zip's `package` on sys.path (at the front when `first`): the folder used, or None when the zip has none."""
    key = os.path.join(folder, package)
    if key in _added:
        return _added[key]
    package_dir = os.path.join(folder, package)
    if not os.path.isdir(package_dir):
        _added[key] = None
        return None
    path = folder
    files = _files(package_dir)
    if any(f.lower().endswith(NATIVE) for f in files):
        try:
            path = _copy(package, package_dir, files, root or cache_root())
        except OSError:
            path = folder  # Cannot copy (no space, no rights): load it in place, as before.
    if path not in sys.path:
        if first:
            sys.path.insert(0, path)
        else:
            sys.path.append(path)
    _added[key] = path
    return path
