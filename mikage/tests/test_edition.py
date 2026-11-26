"""
core/edition.py: MIKAGE and MIKAGE 練習版 from the same code. Read from the
source (no QGIS): the plugin's personal settings and its profile folder go
through qgis_io/qsettings.py only, so the two never mix theirs.
"""
import os
import re

from mikage.core import edition

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Where QgsSettings may be made: the plugin's keys (qsettings.py) and QGIS's own keys (raster_speed.py, 地図の描画を速くする設定).
SETTINGS_MAKERS = {os.path.join("qgis_io", "qsettings.py"), os.path.join("qgis_io", "raster_speed.py")}


def _sources():
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "ext")]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as f:
                    yield os.path.relpath(path, ROOT), f.read()


def test_the_two_plugins():
    assert (edition.FULL_ID, edition.TRAINING_ID) == ("mikage", "mikage_training")
    assert not edition.training_only()  # (no marker in the source folder: gitignored)
    assert (edition.plugin_id(), edition.other_id(), edition.name()) == ("mikage", "mikage_training", "MIKAGE")


def test_the_training_edition_names(monkeypatch):
    monkeypatch.setattr(edition, "TRAINING_ONLY", True)
    assert (edition.plugin_id(), edition.other_id(), edition.name(), edition.other_name()) == (
        "mikage_training", "mikage", "MIKAGE 練習版", "MIKAGE")


def test_personal_settings_and_the_profile_folder_go_through_qsettings():
    """A key written as 「mikage/…」 or a folder of its own in the profile would be shared by the two plugins."""
    found = []
    for path, text in _sources():
        if "QgsSettings(" in text and path not in SETTINGS_MAKERS:
            found.append(f"{path}: QgsSettings()")
        if "qgisSettingsDirPath" in text and path != os.path.join("qgis_io", "qsettings.py"):
            found.append(f"{path}: qgisSettingsDirPath")
        for m in re.finditer(r"^([A-Z_]*(KEY|ROOT|GROUP)[A-Z_]*)\s*=\s*[\"']mikage/", text, re.MULTILINE):
            found.append(f"{path}: {m.group(1)}")
    assert found == []
