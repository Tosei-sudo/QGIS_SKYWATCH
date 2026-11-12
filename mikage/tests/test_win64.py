"""The Windows zip's libraries on Python's path: native ones from a copy outside the plugin's folder (core/win64)."""
import os
import sys

import pytest

from mikage.core import win64


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(win64, "_added", {})
    monkeypatch.setattr(sys, "path", list(sys.path))


def package(folder, name, files):
    for rel, data in files.items():
        path = folder / name / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_a_pure_python_package_is_read_in_place(tmp_path):
    package(tmp_path / "win64", "tzdata", {"__init__.py": b"", "zoneinfo/Asia/Tokyo": b"TZif"})
    assert win64.add_package("tzdata", str(tmp_path / "win64"), str(tmp_path / "cache")) == str(tmp_path / "win64")
    assert sys.path[-1] == str(tmp_path / "win64")
    assert not (tmp_path / "cache").exists()


def test_a_package_with_a_dll_is_loaded_from_a_copy_so_qgis_can_replace_the_plugin(tmp_path):
    """A DLL loaded from the plugin's folder stays locked, and QGIS then fails to update or uninstall the plugin."""
    folder, cache = tmp_path / "win64", tmp_path / "cache"
    package(folder, "onnxruntime", {"__init__.py": b"x = 1\n", "capi/onnxruntime.dll": b"MZ1"})
    path = win64.add_package("onnxruntime", str(folder), str(cache), first=True)
    assert os.path.dirname(path) == str(cache) and os.path.basename(path).startswith("onnxruntime-")
    assert sys.path[0] == path and str(folder) not in sys.path
    assert (cache / os.path.basename(path) / "onnxruntime" / "capi" / "onnxruntime.dll").read_bytes() == b"MZ1"
    assert win64.add_package("onnxruntime", str(folder), str(cache)) == path  # Once per session.

    # The next session with the same zip uses the same copy; a new zip's library gets a new one and the old goes.
    win64._added.clear()
    assert win64.add_package("onnxruntime", str(folder), str(cache)) == path
    (folder / "onnxruntime" / "capi" / "onnxruntime.dll").write_bytes(b"MZ2")
    # While this QGIS still has the old copy on its path (a plugin reloaded), it stays whole (#82).
    win64._added.clear()
    newer = win64.add_package("onnxruntime", str(folder), str(cache))
    assert newer != path and sorted(os.listdir(cache)) == sorted([os.path.basename(path), os.path.basename(newer)])
    # The next session (the old copy used by nobody): it goes.
    while path in sys.path:
        sys.path.remove(path)
    win64._added.clear()
    assert win64.add_package("onnxruntime", str(folder), str(cache)) == newer
    assert sorted(os.listdir(cache)) == [os.path.basename(newer)]


def test_the_copy_is_imported(tmp_path):
    folder, cache = tmp_path / "win64", tmp_path / "cache"
    package(folder, "nativepkg_for_test", {"__init__.py": b"VALUE = 7\n", "_core.pyd": b"MZ"})
    win64.add_package("nativepkg_for_test", str(folder), str(cache))
    import nativepkg_for_test  # noqa: PLC0415

    try:
        assert nativepkg_for_test.VALUE == 7 and nativepkg_for_test.__file__.startswith(str(cache))
    finally:
        del sys.modules["nativepkg_for_test"]


def test_without_a_place_to_copy_it_is_loaded_in_place(tmp_path):
    folder = tmp_path / "win64"
    package(folder, "sgp4", {"__init__.py": b"", "vallado_cpp.cp312-win_amd64.pyd": b"MZ"})
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    assert win64.add_package("sgp4", str(folder), str(blocked / "cache")) == str(folder)


def test_without_the_package_nothing_is_added(tmp_path):
    before = list(sys.path)
    assert win64.add_package("sgp4", str(tmp_path), str(tmp_path / "cache")) is None
    assert sys.path == before


def test_the_copies_go_to_the_local_application_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert win64.cache_root() == os.path.join(str(tmp_path), "mikage", "win64")


def test_a_copy_another_qgis_has_loaded_is_left_whole(tmp_path, monkeypatch):
    """#82: when the folder cannot be renamed away (Windows: a DLL in it is loaded), nothing of it is removed."""
    folder, cache = tmp_path / "win64", tmp_path / "cache"
    package(folder, "lockedpkg_for_test", {"__init__.py": b"x = 1\n", "_core.pyd": b"MZ1"})
    old = win64.add_package("lockedpkg_for_test", str(folder), str(cache))
    sys.path.remove(old)
    (folder / "lockedpkg_for_test" / "_core.pyd").write_bytes(b"MZ2")
    real = os.rename

    def locked(a, b):
        if os.path.abspath(a) == os.path.abspath(old):
            raise PermissionError("in use")
        return real(a, b)

    monkeypatch.setattr(win64.os, "rename", locked)
    win64._added.clear()
    newer = win64.add_package("lockedpkg_for_test", str(folder), str(cache))
    assert newer != old and os.path.isfile(os.path.join(old, "lockedpkg_for_test", "__init__.py"))
    sys.path.remove(newer)
