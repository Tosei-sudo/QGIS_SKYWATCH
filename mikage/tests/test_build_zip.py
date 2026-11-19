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
    assert "mikage/config.jsonc" in names
    assert "mikage/metadata.txt" in names
    assert not any("/tests/" in n for n in names)


def test_the_genai_wheel_is_carried_without_its_model_builder(tmp_path):
    """onnxruntime-genai's models/ (the builder that converts a model, with its own Python) is not needed to run one."""
    b = build()
    path = tmp_path / "onnxruntime_genai-0.20.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(path, "w") as w:
        for name in ("onnxruntime_genai/__init__.py", "onnxruntime_genai/onnxruntime-genai.dll", "onnxruntime_genai/models/builder.py",
                     "onnxruntime_genai/models/builders/llama.py", "onnxruntime_genai-0.20.0.dist-info/LICENSE",
                     "onnxruntime_genai-0.20.0.dist-info/RECORD"):
            w.writestr(name, "x")
    names = [n for n, _ in b.wheel_files(str(path), "onnxruntime_genai", drop=b.GENAI_DROP)]
    assert names == ["onnxruntime_genai/__init__.py", "onnxruntime_genai/onnxruntime-genai.dll", "onnxruntime_genai/LICENSE"]


def test_refuses_a_broken_config(tmp_path):
    b = build()
    broken = tmp_path / "config.json"
    broken.write_text('{"satelliteCatalogs": [', encoding="utf-8")
    with pytest.raises(SystemExit):
        b.main(str(broken), str(tmp_path))


def test_locks_the_plugin_to_the_shipped_config(tmp_path):
    b = build()
    target = b.main(os.path.join(HERE, "data", "viewer-config.jsonc"), str(tmp_path), lock_config=True)
    z = zipfile.ZipFile(target)
    assert "mikage/config.lock" in z.namelist()
    assert "--lock-config" in z.read("mikage/config.lock").decode("utf-8")
    # Without the option: no lock.
    target = b.main(os.path.join(HERE, "data", "viewer-config.jsonc"), str(tmp_path))
    assert "mikage/config.lock" not in zipfile.ZipFile(target).namelist()


def test_refuses_a_lock_without_a_config(tmp_path, monkeypatch):
    b = build()
    plugin = tmp_path / "mikage"
    plugin.mkdir()
    (plugin / "metadata.txt").write_text("[general]\nversion=0.0.1\n", encoding="utf-8")
    monkeypatch.setattr(b, "PLUGIN", str(plugin))
    with pytest.raises(SystemExit):
        b.main("", str(tmp_path / "out"), lock_config=True)
