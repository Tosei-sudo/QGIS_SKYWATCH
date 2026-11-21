"""
The libraries the Windows zip carries in ext/win64 (scripts/build_zip.py
--win64), put on Python's path.

A package with native code (.pyd/.dll: onnxruntime, onnxruntime-genai, the
C++ sgp4) is never loaded from the plugin's folder but from a copy of it
outside, in the person's local application data. Windows keeps a loaded DLL
locked until QGIS quits, and Python cannot unload one, so a DLL loaded from
the plugin's folder made QGIS fail to update the plugin from a zip ("Failed
to unzip the plugin package") or to uninstall it ("Failed to remove the
directory").

The copy is made in a temporary folder and then renamed (whole or not at
all). Right after the DLLs are written an antivirus may still be reading
them, and Windows then refuses the rename: it is tried again for a few
seconds, and when it is still refused the finished temporary copy is used as
it is. When the local application data cannot be written (no rights, no
space) the copy goes to the temporary folder. When no copy can be made the
package is not loaded at all (NotCopied, an ImportError): the AI tools say
why and the orbits use the pure-Python sgp4, rather than locking the
plugin's folder (2026-11-21).

The copy is named after its contents, so a new zip with other libraries
gets a new copy, and older copies are removed when nothing holds them.
Pure-Python packages (tzdata) are read from the plugin's folder as they are.
Where each package was loaded from is told to the reporter (the plugin's
log) so a failed update can be traced.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from typing import Callable, Optional

#: Where the Windows zip keeps its libraries.
FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ext", "win64")
NATIVE = (".pyd", ".dll", ".so")
#: Pauses (s) between tries of the copy's rename, refused while an antivirus reads the new DLLs (about 3 s in all).
RENAME_WAITS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.5)


class NotCopied(ImportError):
    """A package with native code that could not be copied out of the plugin's folder (so it is not loaded)."""


def cache_root() -> str:
    """Where packages with native code are copied: the local application data (Windows), else the temporary folder."""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "mikage", "win64")


def _roots(root: Optional[str]) -> list[str]:
    """Where to try copying, in order: the one given, else the local application data and then the temporary folder."""
    if root:
        return [root]
    out = [cache_root()]
    spare = os.path.join(tempfile.gettempdir(), "mikage", "win64")
    if os.path.normcase(os.path.abspath(spare)) != os.path.normcase(os.path.abspath(out[0])):
        out.append(spare)
    return out


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


def _rename(work: str, target: str) -> bool:
    """Renames `work` to `target`, tried again while Windows refuses (an antivirus reading the new DLLs); whether `target` is there."""
    for wait in (*RENAME_WAITS, None):
        try:
            os.rename(work, target)
            return True
        except OSError:
            if os.path.isdir(target):
                return True  # Another QGIS made it meanwhile.
            if wait is None:
                return False
            time.sleep(wait)
    return False


def _copy(package: str, package_dir: str, name: str, root: str) -> str:
    """The folder holding a copy of `package` in `root` (made when missing), with older copies of it removed when possible."""
    target = os.path.join(root, name)
    if not os.path.isdir(target):
        os.makedirs(root, exist_ok=True)
        work = tempfile.mkdtemp(prefix=f".{name}-", dir=root)
        try:
            shutil.copytree(package_dir, os.path.join(work, package), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        if not _rename(work, target):
            target = work  # A whole copy all the same, outside the plugin's folder: used as it is, removed another time.
        elif os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
    used = os.path.basename(target)
    for other in os.listdir(root):
        if other != used and (other.startswith(f"{package}-") or other.startswith(f".{package}-")):
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
#: What happened to each package (its name → (where it was loaded from, or why not; whether that is a problem)).
_told: dict[str, tuple[str, bool]] = {}
_reporter: Optional[Callable[[str, bool], None]] = None


def _tell(package: str, text: str, problem: bool = False) -> None:
    _told[package] = (text, problem)
    if _reporter is not None:
        try:
            _reporter(text, problem)
        except Exception:  # noqa: BLE001 (a log that cannot write stops nothing)
            pass


def set_reporter(reporter: Optional[Callable[[str, bool], None]]) -> None:
    """Where to tell where the packages were loaded from (text, whether a problem): those so far at once, then each new one."""
    global _reporter
    _reporter = reporter
    if reporter is not None:
        for text, problem in list(_told.values()):
            try:
                reporter(text, problem)
            except Exception:  # noqa: BLE001
                pass


def add_package(package: str, folder: str = FOLDER, root: Optional[str] = None, first: bool = False) -> Optional[str]:
    """
    Put the Windows zip's `package` on sys.path (at the front when `first`):
    the folder used, or None when the zip has none. NotCopied when it has
    native code and no copy outside the plugin's folder could be made.
    """
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
        name = f"{package}-{_digest(package_dir, files)}"
        reasons = []
        for place in _roots(root):
            try:
                path = _copy(package, package_dir, name, place)
                break
            except OSError as error:  # No rights, no space: the next place.
                reasons.append(f"{place}: {error.strerror or error}")
        else:
            why = "; ".join(reasons)
            _tell(package, f"{package} をプラグインのフォルダーの外に写せず、読み込まない（{why}）。"
                           "プラグインのフォルダーから直接読むと、QGIS を閉じるまで更新・アンインストールが失敗するため", True)
            raise NotCopied(f"{package} をプラグインのフォルダーの外に写せず（{why}）")
        _tell(package, f"{package} を {path} から読み込み（プラグインのフォルダーの外の写し）")
    if path not in sys.path:
        if first:
            sys.path.insert(0, path)
        else:
            sys.path.append(path)
    _added[key] = path
    return path
