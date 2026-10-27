"""The observations as CSV, as the viewer writes them (observations.ts `observationsCsv`)."""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Iterable, Optional

from ..util import iso_text
from .flows import time_key
from .geo import to_fixed
from .model import Observation
from .taxonomy import kind_of, path_of


def local_time(t: Optional[float]) -> str:
    """`yyyy-mm-dd hh:mm` in local time ('' when unknown)."""
    if t is None or not math.isfinite(t):
        return ""
    return datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M")


def _iso(t: Optional[float]) -> str:
    return "" if t is None or not math.isfinite(t) else iso_text(t)


def _cell(v) -> str:
    text = "" if v is None else str(v)
    return '"' + text.replace('"', '""') + '"' if re.search(r'[",\r\n]', text) else text


def depth_columns(records: Iterable[Observation], name: str = "分類") -> list[str]:
    """A column per level of the kinds (分類1 the top)."""
    depth = max([1, *(len(path_of(o.kind)) for o in records)])
    return [f"{name}{i + 1}" for i in range(depth)]


def observations_csv(records: list[Observation]) -> str:
    """The CSV of observations (with a BOM, for spreadsheets), by thing and in flow order."""
    levels = depth_columns(records)
    head = ["観測ID", "モノID", "モノ", "種別", *levels, "活動", "有無", "確度", "撮像日時", "画像ID", "画像", "経度", "緯度", "長さ（m）", "向き（度）",
            "備考", "記録者", "更新者", "登録日時", "更新日時", "削除日時"]
    rows = []
    for o in sorted(records, key=lambda o: (o.object_id, time_key(o), o.created_at)):
        path = path_of(o.kind)
        rows.append([
            o.id, o.object_id, o.object_name, kind_of(path), *(path[i] if i < len(path) else "" for i in range(len(levels))),
            o.activity, "あり" if o.present else "なし", o.confidence or "", _iso(o.image_time), o.image_id, o.image_name,
            to_fixed(o.lon, 7), to_fixed(o.lat, 7), "" if o.length is None else to_fixed(o.length, 1), "" if o.heading is None else to_fixed(o.heading, 1),
            o.note, o.recorded_by or "", o.updated_by or "", _iso(o.created_at), _iso(o.updated_at), _iso(o.deleted_at),
        ])
    return "\ufeff" + "\r\n".join(",".join(_cell(c) for c in r) for r in [head, *rows]) + "\r\n"


def csv_file_name(now_ms: float) -> str:
    """observations_20261025.csv"""
    return f"observations_{datetime.fromtimestamp(now_ms / 1000).strftime('%Y%m%d')}.csv"
