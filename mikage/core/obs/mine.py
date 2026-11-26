"""
「自分の記録」 (tsudo, 2026-11-18: 過去の自分がどの画像のどの観測結果を作ったのかが
わかりにくい): the records one made, across the images, to look over and fix.

Who is oneself: the name the records are saved with (「記録者」, OBSERVER) or
the user name the edits are stamped with and 分担 holds (core/edit_stamp,
USERNAME), case and spaces aside. The records are those made in a period
(記録日時), shown one per row or one row per image.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .model import Observation

DAY_MS = 86_400_000
#: The periods offered (days; 0: since the start of today), 7 days first (tsudo 2026-11-18: 期間の既定 30 日;
#: 2026-11-26: 30 日分は長いので、7 日分を既定に).
PERIODS = [(7, "7 日"), (30, "30 日"), (0, "今日"), (90, "90 日")]
DEFAULT_DAYS = 7


def same_name(a: str, b: str) -> bool:
    """Two user names are the same person (case and spaces aside)."""
    return bool((a or "").strip()) and (a or "").strip().casefold() == (b or "").strip().casefold()


def is_mine(name: str, mine: Sequence[str]) -> bool:
    """Whether a record's 記録者 (or 更新者) is one of one's names."""
    return any(same_name(name, n) for n in mine)


def my_names(observer: str, user: str) -> list[str]:
    """One's names: the 「記録者」 name and the user name (each once, the empty left out)."""
    out: list[str] = []
    for n in (observer, user):
        n = (n or "").strip()
        if n and not any(same_name(n, o) for o in out):
            out.append(n)
    return out


def start_of_day(now_ms: float, zone_offset_ms: Optional[float] = None) -> int:
    """The start of the local day holding `now_ms` (this computer's zone unless an offset is given)."""
    if zone_offset_ms is None:
        t = time.localtime(now_ms / 1000)
        zone_offset_ms = t.tm_gmtoff * 1000
    local = now_ms + zone_offset_ms
    return int(local - local % DAY_MS - zone_offset_ms)


def since_of(days: int, now_ms: float, zone_offset_ms: Optional[float] = None) -> int:
    """The start of a period of `days` days back (0: today, from its start)."""
    today = start_of_day(now_ms, zone_offset_ms)
    return today if days <= 0 else int(now_ms - days * DAY_MS)


@dataclass
class MineFilter:
    """What the list shows."""
    #: The names whose records are shown; empty: everyone's.
    names: list[str] = field(default_factory=list)
    #: 記録日時 from (ms, included) and to (ms, excluded; None: open).
    since: Optional[int] = None
    until: Optional[int] = None
    #: 大分類 (the first part of the kind); '' all.
    root: str = ""
    text: str = ""
    #: Only the records someone else changed afterwards.
    changed_by_others: bool = False
    deleted: bool = False


def root_of(kind: str) -> str:
    """The 大分類 of a kind (`船舶 > 支援船` → 船舶)."""
    return (kind or "").split(">")[0].strip()


def changed_by_other(o: Observation, mine: Sequence[str]) -> bool:
    """Whether someone not oneself saved the record after it was made."""
    return bool(o.updated_by.strip()) and not is_mine(o.updated_by, mine) and o.updated_at > o.created_at


def select(records: Iterable[Observation], flt: MineFilter, me: Sequence[str] = ()) -> list[Observation]:
    """The records the filter lets through, newest made first."""
    words = [w.casefold() for w in flt.text.split() if w]
    out = []
    for o in records:
        if o.deleted_at is not None and not flt.deleted:
            continue
        if flt.names and not is_mine(o.recorded_by, flt.names):
            continue
        if flt.since is not None and o.created_at < flt.since:
            continue
        if flt.until is not None and o.created_at >= flt.until:
            continue
        if flt.root and root_of(o.kind) != flt.root:
            continue
        if flt.changed_by_others and not changed_by_other(o, me or flt.names):
            continue
        if words:
            hay = " ".join([o.object_name, o.object_id, o.kind, o.activity, o.note, o.image_name, o.image_id]).casefold()
            if not all(w in hay for w in words):
                continue
        out.append(o)
    return sorted(out, key=lambda o: (-o.created_at, o.image_id, o.object_name))


@dataclass
class ImageRow:
    """One image of the records shown (「画像ごと」)."""
    image_id: str
    image_name: str
    image_time: Optional[int]
    records: list[Observation]
    #: Records per 大分類, the most first.
    roots: list[tuple[str, int]]
    #: When the last of them was made.
    last_made: int


def by_image(records: Sequence[Observation]) -> list[ImageRow]:
    """The records as one row per image, the image recorded on last first."""
    groups: dict[str, list[Observation]] = {}
    for o in records:
        groups.setdefault(o.image_id, []).append(o)
    rows = []
    for image_id, rs in groups.items():
        named = next((o for o in rs if o.image_name), rs[0])
        timed = next((o.image_time for o in rs if o.image_time is not None), None)
        counts: dict[str, int] = {}
        for o in rs:
            if o.deleted_at is None:
                counts[root_of(o.kind) or "（種別なし）"] = counts.get(root_of(o.kind) or "（種別なし）", 0) + 1
        roots = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        rows.append(ImageRow(image_id, named.image_name or image_id, timed, rs, roots, max(o.created_at for o in rs)))
    return sorted(rows, key=lambda r: (-r.last_made, r.image_id))


def roots_text(roots: Sequence[tuple[str, int]]) -> str:
    return "・".join(f"{k} {n}" for k, n in roots)


def observer_names(records: Iterable[Observation]) -> list[str]:
    """The 記録者 names of records, each once (as first written), in the order of their number of records."""
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    for o in records:
        n = o.recorded_by.strip()
        if not n:
            continue
        key = n.casefold()
        shown.setdefault(key, n)
        counts[key] = counts.get(key, 0) + 1
    return [shown[k] for k in sorted(counts, key=lambda k: (-counts[k], k))]
