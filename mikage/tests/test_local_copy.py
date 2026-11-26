"""
手元にコピーして見る (`localCopy`, core/local_copy.py): the settings, which
images are copied with their files, where, and which copies make room.
"""
import os

from mikage.core.catalog.paths import PathMapping, PathRewrite, path_mappings_of
from mikage.core.config import parse_config
from mikage.core.local_copy import (
    DEFAULT_LOCAL_COPY, GB, MB, CopyInfo, LocalCopyOptions, big_enough, copy_name, local_copy_of, meta_of, mount_type,
    relative_name, reserve_for, root_of, same_folder_files, still_same, to_evict, unc_path,
)


def test_defaults_and_false():
    problems = []
    assert local_copy_of(True, problems) == DEFAULT_LOCAL_COPY
    assert local_copy_of(False, problems) == LocalCopyOptions(enabled=False)
    assert DEFAULT_LOCAL_COPY == LocalCopyOptions(True, 300, "temp", "", 30.0)
    assert not problems
    config, problems = parse_config({})
    assert config.local_copy == DEFAULT_LOCAL_COPY and not problems
    config, problems = parse_config({"localCopy": False})
    assert not config.local_copy.enabled and not problems


def test_an_object_and_what_cannot_be_read():
    problems = []
    got = local_copy_of({"minMB": 0, "location": " Cache ", "folder": " %LOCALAPPDATA%\\x ", "maxGB": 2.5}, problems)
    assert got == LocalCopyOptions(True, 0, "cache", "%LOCALAPPDATA%\\x", 2.5) and not problems
    got = local_copy_of({"enabled": "yes", "minMB": -1, "location": "disk", "folder": 3, "maxGB": 0}, problems)
    assert got == DEFAULT_LOCAL_COPY
    assert problems == ["localCopy.enabled は true か false で指定", "localCopy.minMB は 0 以上の数（MB）で指定",
                        "localCopy.location は temp（QGIS を閉じたら消す）か cache（次回も使う）で指定",
                        "localCopy.folder はフォルダのパス（文字列）で指定", "localCopy.maxGB は 0 より大きい数（GB）で指定"]
    problems = []
    assert local_copy_of("yes", problems) == DEFAULT_LOCAL_COPY and problems == ["localCopy は true・false かオブジェクトで指定"]
    config, problems = parse_config({"localCopy": {"maxGB": True}})
    assert config.local_copy == DEFAULT_LOCAL_COPY and problems == ["localCopy.maxGB は 0 より大きい数（GB）で指定"]
    assert LocalCopyOptions().to_json() == {"enabled": True, "minMB": 300, "location": "temp", "folder": "", "maxGB": 30.0}


def test_where_the_copies_go(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "local"), "TEMP": str(tmp_path / "t")}
    assert root_of(LocalCopyOptions(location="cache"), env) == os.path.join(str(tmp_path / "local"), "MIKAGE", "copies")
    assert root_of(LocalCopyOptions(location="cache"), {}).endswith(os.path.join(".cache", "MIKAGE", "copies"))
    assert root_of(LocalCopyOptions(), env).endswith(os.path.join("MIKAGE", "copies"))
    assert root_of(LocalCopyOptions(folder="%TEMP%/c"), env) == os.path.join(str(tmp_path / "t"), "c")


def test_a_copy_is_named_by_its_place():
    a, b = copy_name("/share/a/img.tif"), copy_name("/share/b/img.tif")
    assert a != b and len(a) == 16 and copy_name("/share/a/img.tif") == a


def test_how_big():
    options = LocalCopyOptions(min_mb=300)
    assert big_enough(300 * MB, options) and not big_enough(300 * MB - 1, options)
    assert big_enough(0, LocalCopyOptions(min_mb=0))
    assert reserve_for(10 * GB) == 2 * GB and reserve_for(1000 * GB) == 50 * GB


def test_the_files_that_go_with_the_image(tmp_path):
    image = str(tmp_path / "img" / "R1C1.TIF")
    files = [image, image + ".ovr", str(tmp_path / "img" / "R1C1.RPB"), image + ".aux.xml",
             str(tmp_path / "img" / "sub" / "R1C1.IMD"), str(tmp_path / "pam" / "x.aux.xml"), image]
    assert same_folder_files(image, files) == files[:5]
    assert same_folder_files(image, []) == [image]
    assert relative_name(image, files[4]) == "sub/R1C1.IMD" and relative_name(image, image) == "R1C1.TIF"


def test_the_meta_file(tmp_path):
    folder = str(tmp_path)
    info = meta_of(folder, {"source": "//nas/img/a.tif", "size": 10, "mtime": 5.5, "files": ["a.tif", "a.tif.ovr"],
                            "bytes": 12, "used": 7})
    assert info == CopyInfo(folder, "//nas/img/a.tif", 10, 5.5, ("a.tif", "a.tif.ovr"), 12, 7.0)
    assert info.image == os.path.join(folder, "a.tif")
    assert meta_of(folder, info.to_json()) == info
    for bad in (None, [], {"source": "x"}, {"source": "x", "size": 1, "mtime": 1, "files": []},
                {"source": "x", "size": 1, "mtime": 1, "files": ["../a.tif"]},
                {"source": "x", "size": 1, "mtime": 1, "files": ["/etc/a.tif"]},
                {"source": "x", "size": "big", "mtime": 1, "files": ["a.tif"]}):
        assert meta_of(folder, bad) is None
    assert still_same(info, 10, 6.5) and not still_same(info, 11, 5.5) and not still_same(info, 10, 9)


def test_room_from_the_copies_least_recently_used_not_in_use(tmp_path):
    def c(name, size, used):
        return CopyInfo(str(tmp_path / name), "s", 0, 0, ("a",), size, used)

    old, mid, new = c("old", 10, 1), c("mid", 10, 2), c("new", 10, 3)
    assert to_evict([old, mid, new], 5, 40, set()) == []
    assert to_evict([old, mid, new], 15, 40, set()) == [old]
    assert to_evict([new, mid, old], 25, 40, set()) == [old, mid]
    # The one shown stays: the next least recently used goes instead.
    from mikage.core.local_copy import place_key

    assert to_evict([old, mid, new], 15, 40, {place_key(old.folder)}) == [mid]
    # Not even all of them would make room.
    assert to_evict([old, mid, new], 35, 40, {place_key(new.folder)}) is None
    assert to_evict([], 50, 40, set()) is None


def test_network_places():
    assert unc_path("\\\\nas\\img\\a.tif") and unc_path("//nas/img/a.tif")
    assert not unc_path("\\\\?\\C:\\img\\a.tif") and not unc_path("C:\\img\\a.tif") and not unc_path("/mnt/a.tif")
    mounts = [("/", "ext4"), ("/mnt", "tmpfs"), ("/mnt/share", "cifs"), ("/mnt/share2", "nfs4"), ("/mnt/my share", "smb3")]
    assert mount_type("/mnt/share/img/a.tif", mounts) == "cifs"
    assert mount_type("/mnt/share2/a.tif", mounts) == "nfs4"
    assert mount_type("/mnt/sharex/a.tif", mounts) == "tmpfs"
    assert mount_type("/mnt/my share/a.tif", mounts) == "smb3"
    assert mount_type("/home/a.tif", mounts) == "ext4"
    assert mount_type("/home/a.tif", []) is None


def test_a_rewrite_may_copy_or_never_copy_the_images_it_finds():
    """`localCopy` on a rewrite (tsudo 2026-11-27): true, false, or not said (None); only on a rewrite."""
    flat = {"pattern": "^//nas/img/([^/]+)$", "to": "\\\\work\\img\\$1", "label": "作業用"}
    problems = []
    entries = [dict(flat, localCopy=True), dict(flat, localCopy=False), flat, dict(flat, localCopy="yes"),
               {"prefix": "\\\\nas\\img", "localCopy": True}]
    copy, never, unsaid, prefix = path_mappings_of(entries, problems)
    assert copy == PathRewrite(flat["pattern"], flat["to"], "作業用", False, True)
    assert never.local_copy is False and unsaid.local_copy is None
    assert prefix == PathMapping("\\\\nas\\img", "\\\\nas\\img")
    assert problems == ["pathMappings[3] の localCopy は true か false で指定",
                        "pathMappings[4] の localCopy は pattern・to の読み替えにだけ書ける"]
