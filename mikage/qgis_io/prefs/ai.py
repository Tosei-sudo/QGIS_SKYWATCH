"""
AIの個人設定 (QgsSettings, per user profile: docs/architecture.md, 設定を機能ごとに). Read
through qgis_io/settings.py as before (it lists them); a new one of this feature goes here.
"""
from __future__ import annotations

import json
import os

from ..qsettings import pref, set_pref


#: The most trained models remembered for 物体検出（AI）.
TRAINED_MAX = 20


def trained_models() -> list[tuple[str, str]]:
    """Models trained in this profile (newest first), as (label, path), those whose file is still there."""
    try:
        value = json.loads(pref("trained_models", "[]"))
    except ValueError:
        return []
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and os.path.isfile(item["path"]):
            out.append((str(item.get("label") or os.path.basename(item["path"])), item["path"]))
    return out


def remember_trained_model(label: str, path: str) -> None:
    """Lists a trained model first among 物体検出（AI）'s models (the same file once)."""
    path = os.path.abspath(path)
    kept = [(l, p) for l, p in trained_models() if os.path.abspath(p) != path]
    items = [{"label": label, "path": path}] + [{"label": l, "path": p} for l, p in kept]
    set_pref("trained_models", json.dumps(items[:TRAINED_MAX], ensure_ascii=False))


#: 生成 AI の機能 (文章の手伝い・文で操作; tsudo 2026-11-21: 「生成AIの機能はオプトインにしよう」): off until the person turns it on
#: (担当と個人設定). The tests turn it on (conftest).
TEXT_ASSIST_DEFAULT = "0"


def text_assist_on() -> bool:
    """Whether the person uses the generative AI's features (the settings giving them a model)."""
    return pref("text_assist/on", TEXT_ASSIST_DEFAULT) == "1"


def set_text_assist_on(on: bool) -> None:
    set_pref("text_assist/on", "1" if on else "0")
