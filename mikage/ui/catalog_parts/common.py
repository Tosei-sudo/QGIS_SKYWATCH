"""
What the 画像カタログドック's parts share (ui/catalog_parts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from qgis.PyQt.QtWidgets import QLineEdit

from ...core.catalog.api import CatalogConfig, CatalogSearch, CatalogRecord
from ...core.util import read_clock, read_day_time, read_number

TITLE = "画像カタログ"


def _number(edit: QLineEdit) -> Optional[float]:
    """The number typed (full-width digits and a ° too); None when empty or unreadable (`input_problem` tells which)."""
    try:
        return read_number(edit.text())
    except ValueError:
        return None

@dataclass

class _Listed:
    """The search the list shows (or is being read), to read on from with 「続きを読む」."""
    catalog: CatalogConfig
    search: CatalogSearch
    box: Any
    #: 「類似画像の検索」: the images read → those listed (the most alike first) and their overlaps; None for the usual search.
    rank: Optional[Callable[[list], tuple[list, dict]]] = None
    #: Every image read so far, in the service's order (newest first).
    read: list = field(default_factory=list)
    total: Optional[int] = None
    #: Where the next images start (the service's offset); None when there are no more.
    next_offset: Optional[int] = None


def _key(record: CatalogRecord) -> Any:
    """What tells an image of the catalog from the others (an image read twice, the list having moved on since, is listed once)."""
    return ("oid", record.object_id) if record.object_id is not None else ("id", record.id, record.acquired)


def _when(edit: QLineEdit, day: str = "") -> str:
    """
    The day, or day and time, typed as `yyyy-mm-dd` or `yyyy-mm-dd hh:mm`
    (2026/11/9 10:30, 20261109, full-width digits too; a time alone on
    `day`, the start's day); '' when empty or unreadable (`input_problem`
    tells which).
    """
    try:
        return read_day_time(edit.text(), day)
    except ValueError:
        return ""


def local_now() -> str:
    """Now, this PC's time, as the saved areas keep it (2026-11-19 12:43)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _is_clock(text: str) -> bool:
    """Whether `text` is a time of day alone (12:00)."""
    try:
        return bool(read_clock(text))
    except ValueError:
        return False


def _period(start: QLineEdit, end: QLineEdit) -> tuple[str, str]:
    """A period typed (`_when`): the end may be a time alone (〜 12:00), on the start's day."""
    a = _when(start)
    return a, _when(end, a[:10])

#: The hint of the period boxes.
PERIOD_TIP = ("日付だけなら、その日の 0 時から（〜の左）・その日の終わりまで（〜の右）。"
              "時刻をつけると分（秒も書けば秒）の単位で絞る（例 2026-11-09 10:30。10時30分・1030 も可）。"
              "右に時刻だけ（12:00）なら左と同じ日。時刻はこの PC の時刻で、一覧の日時と同じ")
