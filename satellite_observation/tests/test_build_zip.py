"""scripts/build_zip.py: the viewer's config.json shipped in the zip, checked first."""
import importlib.util
import os
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))


def build():
    spec = importlib.util.spec_from_file_location("build_zip", os.path.join(ROOT, "scripts", "build_zip.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ships_the_viewers_config(tmp_path):
    b = build()
    target = b.main(os.path.join(HERE, "data", "viewer-config.jsonc"), str(tmp_path))
    names = zipfile.ZipFile(target).namelist()
    assert "satellite_observation/config.jsonc" in names
    assert "satellite_observation/metadata.txt" in names
    assert not any("/tests/" in n for n in names)


def test_refuses_a_broken_config(tmp_path):
    b = build()
    broken = tmp_path / "config.json"
    broken.write_text('{"satelliteCatalogs": [', encoding="utf-8")
    with pytest.raises(SystemExit):
        b.main(str(broken), str(tmp_path))
