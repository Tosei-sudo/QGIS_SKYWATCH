"""scripts/make_plugin_repo.py: an internal QGIS plugin repository (plugins.xml + zips) for closed networks."""
import configparser
import datetime
import importlib.util
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def repo():
    return load("make_plugin_repo")


def version_in(path):
    meta = configparser.ConfigParser(interpolation=None)
    meta.read_string(zipfile.ZipFile(path).read("satellite_observation/metadata.txt").decode("utf-8"))
    return meta["general"]["version"]


def fake_win64(path, out):
    """The regular zip with a file under ext/win64, as build_zip.py --win64 lays it out (no wheels needed)."""
    target = os.path.join(out, os.path.basename(path).replace(".zip", "-win64.zip"))
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(target, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        dst.writestr("satellite_observation/ext/win64/sgp4/__init__.py", "")
    return target


def test_build_zip_stamps_the_version(tmp_path):
    b = load("build_zip")
    target = b.main("", str(tmp_path), version="0.4.0.20261108123000")
    assert os.path.basename(target) == "satellite_observation-0.4.0.20261108123000.zip"
    assert version_in(target) == "0.4.0.20261108123000"
    # The rest of metadata.txt stays as written.
    text = zipfile.ZipFile(target).read("satellite_observation/metadata.txt").decode("utf-8")
    assert "qgisMinimumVersion=3.34" in text and text.count("\nversion=") == 1
    with pytest.raises(SystemExit):
        b.main("", str(tmp_path), version="0.4 beta")


def test_release_version_follows_metadata_and_time(repo):
    now = datetime.datetime(2026, 11, 8, 12, 30, 5, tzinfo=datetime.timezone.utc)
    assert repo.release_version(now) == f"{repo.base_version()}.20261108123005"
    later = repo.release_version(now + datetime.timedelta(seconds=1))
    assert repo.compare_versions(later, repo.release_version(now)) == 1
    assert repo.compare_versions(repo.release_version(now), repo.base_version()) == 1


def test_compare_versions_agrees_with_qgis(repo):
    pairs = [("0.4.0.20261108123000", "0.4.0"), ("0.4.0", "0.3.0"), ("0.10", "0.9"), ("1.0", "1.0rc1"),
             ("0.4.0.20261108123000", "0.4.0.20261108123001"), ("1.0", "1.0"), ("0.1.0.20270101000000", "0.3.0")]
    expected = [1, 1, 1, 1, 2, 0, 2]
    assert [repo.compare_versions(a, b) for a, b in pairs] == expected
    sys.path.insert(0, "/usr/share/qgis/python")
    try:
        from pyplugin_installer.version_compare import compareVersions
    except Exception:  # noqa: BLE001 - no QGIS here: the hand-written expectations above still hold
        return
    finally:
        sys.path.remove("/usr/share/qgis/python")
    assert [compareVersions(a, b) for a, b in pairs] == expected


def test_publishes_both_kinds(repo, tmp_path):
    b = load("build_zip")
    regular = b.main("", str(tmp_path / "dist"), version="0.4.0.20261108123000")
    win64 = fake_win64(regular, str(tmp_path / "dist"))
    out = tmp_path / "repo"
    addresses = repo.publish(str(out), [regular, win64], "http://intra.example/qgis/", changelog="通報を追加",
                             today="2026-11-08")
    assert addresses == ["http://intra.example/qgis/plugins.xml", "http://intra.example/qgis/plugins-win64.xml"]
    assert sorted(os.listdir(out)) == ["icon.svg", "plugins-win64.xml", "plugins.xml",
                                       "satellite_observation.0.4.0.20261108123000.win64.zip",
                                       "satellite_observation.0.4.0.20261108123000.zip"]
    for xml, name in [("plugins.xml", "satellite_observation.0.4.0.20261108123000.zip"),
                      ("plugins-win64.xml", "satellite_observation.0.4.0.20261108123000.win64.zip")]:
        node = ET.parse(out / xml).getroot().find("pyqgis_plugin")
        assert node.get("version") == "0.4.0.20261108123000"
        assert node.findtext("file_name") == name
        # QGIS takes the plugin's id from the file name before the first dot: it must be the installed folder's.
        assert node.findtext("file_name").partition(".")[0] == "satellite_observation"
        assert node.findtext("download_url") == f"http://intra.example/qgis/{name}"
        assert node.findtext("qgis_minimum_version") == "3.34"
        assert node.findtext("experimental") == "False"  # shown without「実験的なプラグインも表示する」
        assert node.findtext("changelog") == "通報を追加"
    with zipfile.ZipFile(out / "satellite_observation.0.4.0.20261108123000.win64.zip") as z:
        assert "satellite_observation/ext/win64/sgp4/__init__.py" in z.namelist()


def test_refuses_a_version_not_newer(repo, tmp_path):
    b = load("build_zip")
    out = str(tmp_path / "repo")
    new = b.main("", str(tmp_path / "a"), version="0.4.0.20261108123000")
    repo.publish(out, [new])
    old = b.main("", str(tmp_path / "b"), version="0.4.0.20261101000000")
    for zips in ([new], [old]):
        with pytest.raises(SystemExit):
            repo.publish(out, zips)
    # Going back on purpose.
    repo.publish(out, [old], allow_older=True)
    assert ET.parse(os.path.join(out, "plugins.xml")).getroot().find("pyqgis_plugin").get("version") == "0.4.0.20261101000000"
    # The other kind's list is not involved.
    assert not os.path.exists(os.path.join(out, "plugins-win64.xml"))


def test_refuses_other_zips(repo, tmp_path):
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("another_plugin/metadata.txt", "[general]\nversion=1.0\n")
    with pytest.raises(SystemExit):
        repo.publish(str(tmp_path / "repo"), [str(other)])


def test_folder_urls(repo, tmp_path):
    assert repo.folder_url(r"\\fileserver\共有\qgis plugins") == "file://fileserver/%E5%85%B1%E6%9C%89/qgis%20plugins"
    assert repo.folder_url(str(tmp_path)) == tmp_path.as_uri()
