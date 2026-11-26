"""
MIKAGE の版 (2026-11-26, tsudo 「練習モードのみのプラグインも同じリポジトリに入れたい。コードとかは完全共有だけど、
起動したら練習モードしか無理みたいな」): the same code makes two plugins.

- MIKAGE (`mikage`): the production, with 練習モード to start and end.
- MIKAGE 練習版 (`mikage_training`): 練習モード only. It starts on its own
  when QGIS starts and cannot end, so the production is never used
  (features/training.py). Made by `scripts/build_zip.py --training-only`,
  which names the plugin's folder (QGIS's plugin id) `mikage_training` and
  puts MARKER in it: this module reads that marker.

The two keep their own personal settings (QgsSettings under `<plugin id>/`)
and their own folder in the QGIS profile (qgis_io/qsettings.py), so both can
be installed in one profile without mixing; only one runs at a time (the
other's plugin id: plugin.py). Pure Python: the tests switch it with
`TRAINING_ONLY`.
"""
from __future__ import annotations

import os

#: The plugin's folder (the zip's top folder).
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The marker of MIKAGE 練習版 in the plugin's folder (scripts/build_zip.py --training-only writes it).
MARKER = "training.only"
MARKER_TEXT = "MIKAGE 練習版: 練習モードだけで動く（scripts/build_zip.py --training-only）\n"
#: The plugin ids (the plugin's folder's name in QGIS) and the names (metadata.txt, the menu, the toolbar).
FULL_ID, TRAINING_ID = "mikage", "mikage_training"
FULL_NAME, TRAINING_NAME = "MIKAGE", "MIKAGE 練習版"

#: Whether this is MIKAGE 練習版 (read once, from the marker).
TRAINING_ONLY: bool = os.path.isfile(os.path.join(PLUGIN_DIR, MARKER))


def training_only() -> bool:
    """Whether this is MIKAGE 練習版: 練習モード only, started with QGIS, never ended."""
    return TRAINING_ONLY


def plugin_id() -> str:
    """This plugin's id in QGIS (its folder's name): also the root of its personal settings and its profile folder."""
    return TRAINING_ID if TRAINING_ONLY else FULL_ID


def other_id() -> str:
    """The other version's plugin id (both may be installed, one runs at a time)."""
    return FULL_ID if TRAINING_ONLY else TRAINING_ID


def name() -> str:
    """This plugin's name (the menu's, the toolbar's)."""
    return TRAINING_NAME if TRAINING_ONLY else FULL_NAME


def other_name() -> str:
    return FULL_NAME if TRAINING_ONLY else TRAINING_NAME
