"""
チュートリアル's progress (練習モードの段階 2): how far each course went, in the
practice folder (`progress.json`, per QGIS profile; kept when the practice
data is made again: it is the person's, not the sample's).

    {"obs": {"step": 4, "done": false, "at": "2026-11-22T20:15:00"}, ...}

`step` is the step to go on from (0 = the first); `done` once the last one was passed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional


def read_progress(path: str) -> dict:
    """The progress kept ({} when none or not readable)."""
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, kept in value.items():
        if isinstance(key, str) and isinstance(kept, dict):
            step = kept.get("step")
            out[key] = {"step": step if isinstance(step, int) and step >= 0 else 0, "done": kept.get("done") is True,
                        "at": kept.get("at") if isinstance(kept.get("at"), str) else ""}
    return out


def kept(progress: dict, course: str, step: int, finished: bool, now: Optional[datetime] = None) -> dict:
    """`progress` with the course at `step` (finished once its last step was passed; a course done stays done)."""
    out = {k: dict(v) for k, v in progress.items()}
    before = out.get(course) or {}
    out[course] = {"step": 0 if finished else max(0, step), "done": finished or before.get("done") is True,
                   "at": (now or datetime.now()).isoformat(timespec="seconds")}
    return out


def write_progress(path: str, progress: dict) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=1)
    os.replace(temporary, path)
