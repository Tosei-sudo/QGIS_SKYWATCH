"""The Windows zip's libraries on Python's path: native ones from a copy outside the plugin's folder (core/win64)."""
import os
import sys

import pytest

from mikage.core import win64


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(win64, "_added", {})
    monkeypatch.setattr(win64, "_told", {})
    monkeypatch.setattr(win64, "_reporter", None)
    monkeypatch.setattr(win64, "RENAME_WAITS", (0, 0, 0))
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


def test_without_a_place_to_copy_it_is_not_loaded_from_the_plugin_folder(tmp_path):
    """2026-11-21: loaded in place, its DLLs would lock the plugin's folder again; the AI tools say why, the orbits use the pure-Python sgp4."""
    folder = tmp_path / "win64"
    package(folder, "sgp4", {"__init__.py": b"", "vallado_cpp.cp312-win_amd64.pyd": b"MZ"})
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    told = []
    win64.set_reporter(lambda text, problem: told.append((text, problem)))
    with pytest.raises(win64.NotCopied, match="sgp4 をプラグインのフォルダーの外に写せず"):
        win64.add_package("sgp4", str(folder), str(blocked / "cache"))
    assert str(folder) not in sys.path
    assert len(told) == 1 and told[0][1] and "読み込まない" in told[0][0]
    # An ImportError: core/plan/orbit then takes the pure-Python copy.
    assert issubclass(win64.NotCopied, ImportError)
    # Tried again the next time (the place may be writable by then).
    with pytest.raises(win64.NotCopied):
        win64.add_package("sgp4", str(folder), str(blocked / "cache"))


def test_when_the_local_application_data_cannot_be_written_the_copy_goes_to_the_temporary_folder(tmp_path, monkeypatch):
    folder = tmp_path / "win64"
    package(folder, "onnxruntime", {"__init__.py": b"", "capi/onnxruntime.dll": b"MZ"})
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    monkeypatch.setenv("LOCALAPPDATA", str(blocked))
    monkeypatch.setattr(win64.tempfile, "tempdir", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    path = win64.add_package("onnxruntime", str(folder))
    assert os.path.dirname(path) == str(tmp_path / "temp" / "mikage" / "win64") and sys.path[-1] == path


def test_a_rename_refused_for_a_moment_is_tried_again(tmp_path, monkeypatch):
    """Right after the DLLs are written an antivirus may still read them, and Windows then refuses the copy's rename."""
    folder, cache = tmp_path / "win64", tmp_path / "cache"
    package(folder, "onnxruntime", {"__init__.py": b"", "capi/onnxruntime.dll": b"MZ"})
    real, refused = os.rename, []

    def busy(a, b):
        if os.path.basename(a).startswith(".onnxruntime-") and len(refused) < 2:
            refused.append(a)
            raise PermissionError("being read")
        return real(a, b)

    monkeypatch.setattr(win64.os, "rename", busy)
    path = win64.add_package("onnxruntime", str(folder), str(cache))
    assert len(refused) == 2 and os.path.basename(path).startswith("onnxruntime-")
    assert os.listdir(cache) == [os.path.basename(path)]  # The temporary folder gone.


def test_a_rename_refused_for_good_uses_the_finished_temporary_copy(tmp_path, monkeypatch):
    folder, cache = tmp_path / "win64", tmp_path / "cache"
    package(folder, "onnxruntime", {"__init__.py": b"x = 1\n", "capi/onnxruntime.dll": b"MZ"})
    real = os.rename

    def refused(a, b):
        if os.path.basename(a).startswith(".onnxruntime-"):
            raise PermissionError("being read")
        return real(a, b)

    monkeypatch.setattr(win64.os, "rename", refused)
    told = []
    win64.set_reporter(lambda text, problem: told.append((text, problem)))
    path = win64.add_package("onnxruntime", str(folder), str(cache))
    assert os.path.dirname(path) == str(cache) and os.path.basename(path).startswith(".onnxruntime-")
    assert (cache / os.path.basename(path) / "onnxruntime" / "capi" / "onnxruntime.dll").read_bytes() == b"MZ"
    assert sys.path[-1] == path and str(folder) not in sys.path
    assert told == [(f"onnxruntime を {path} から読み込み（プラグインのフォルダーの外の写し）", False)]
    # The next session renames its copy, and the earlier temporary one goes once nothing uses it.
    monkeypatch.setattr(win64.os, "rename", real)
    sys.path.remove(path)
    win64._added.clear()
    newer = win64.add_package("onnxruntime", str(folder), str(cache))
    assert os.path.basename(newer).startswith("onnxruntime-") and os.listdir(cache) == [os.path.basename(newer)]


def test_where_each_package_was_loaded_from_is_told_to_the_log(tmp_path):
    folder, cache = tmp_path / "win64", tmp_path / "cache"
    package(folder, "sgp4", {"__init__.py": b"", "vallado_cpp.pyd": b"MZ"})
    package(folder, "tzdata", {"__init__.py": b""})
    path = win64.add_package("sgp4", str(folder), str(cache))
    win64.add_package("tzdata", str(folder), str(cache))  # Pure Python, in place: nothing to tell.
    told = []
    win64.set_reporter(lambda text, problem: told.append((text, problem)))  # The plugin's log, set after sgp4 was imported.
    assert told == [(f"sgp4 を {path} から読み込み（プラグインのフォルダーの外の写し）", False)]
    win64.set_reporter(None)


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
