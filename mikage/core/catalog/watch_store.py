"""
The watches and notices of the image catalogs (core.catalog.watch) in the
local GeoPackage, and checking the watches that are due.

A notice's image (its Esri feature) and what was found out about it are in
DATA; what the lists show is in columns of their own as well, so that tens
of thousands of notices are listed without reading their features
(`notices`), those of the cards shown read when needed (`complete`).
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Sequence

from ..gpkg import Column, GeoPackage, TableSpec
from ..util import time_of
from .watch import MAX_NOTICES, MAX_SEEN, CatalogWatch, EsriAsk, WatchNotice, check_watch

WATCHES = TableSpec("catalog_watches", None, (Column("WATCH_ID", "TEXT(40)"), Column("NAME", "TEXT(255)"), Column("DATA", "TEXT")),
                    key="WATCH_ID", description="新着画像の監視条件")
NOTICES = TableSpec("watch_notices", None, (
    Column("NOTICE_ID", "TEXT"), Column("WATCH_ID", "TEXT(40)"), Column("FOUND", "DOUBLE"), Column("IS_READ", "SMALLINT"), Column("DATA", "TEXT"),
    # What the lists show (added later: filled from DATA for the rows written before, see WatchStore._fill).
    Column("WATCH_NAME", "TEXT(255)"), Column("IMAGE_ID", "TEXT"), Column("ACQUIRED", "TEXT"), Column("TAKEN", "DOUBLE"),
    Column("SENSOR", "TEXT"), Column("CATALOG_LABEL", "TEXT(255)"), Column("KIND", "TEXT(20)"), Column("EARLIER", "SMALLINT"),
), key="NOTICE_ID", description="新着画像の通知")

_LIST = ("NOTICE_ID", "WATCH_ID", "FOUND", "IS_READ", "WATCH_NAME", "IMAGE_ID", "ACQUIRED", "SENSOR", "CATALOG_LABEL", "KIND", "EARLIER")
#: Newest first (ties in the order they were added, as the lists always had them; no time taken: the oldest).
_ORDER = '"FOUND" DESC, "TAKEN" DESC, fid'
#: The list read from an index holding all its columns (the rows themselves, with the features, not read).
_LIST_INDEX = ", ".join(['"FOUND" DESC', '"TAKEN" DESC', "fid"] + [f'"{c}"' for c in _LIST if c != "FOUND"])
#: The most values in one `IN (…)` (SQLite's limit is 999 in older builds).
_CHUNK = 500


def _taken(acquired: Any) -> Optional[float]:
    t = time_of(acquired)
    return None if math.isnan(t) else t


def _row(n: WatchNotice) -> dict[str, Any]:
    return {"NOTICE_ID": n.id, "WATCH_ID": n.watch_id, "FOUND": n.found, "IS_READ": int(n.read),
            "DATA": json.dumps(n.to_json(), ensure_ascii=False), **_columns(n)}


def _columns(n: WatchNotice) -> dict[str, Any]:
    return {"WATCH_NAME": n.watch_name, "IMAGE_ID": n.image_id, "ACQUIRED": json.dumps(n.acquired, ensure_ascii=False),
            "TAKEN": _taken(n.acquired), "SENSOR": n.sensor, "CATALOG_LABEL": n.catalog_label, "KIND": n.kind, "EARLIER": int(n.earlier)}


def _chunks(items: Sequence[Any]) -> list[Sequence[Any]]:
    return [items[i:i + _CHUNK] for i in range(0, len(items), _CHUNK)]


def _marks(items: Sequence[Any]) -> str:
    return ", ".join("?" for _ in items)


class WatchStore:
    """The watches and notices in the local GeoPackage."""

    def __init__(self, gpkg: GeoPackage):
        self.gpkg = gpkg
        gpkg.ensure(WATCHES)
        gpkg.ensure(NOTICES)
        gpkg.ensure_index(NOTICES, "watch_notices_list", _LIST_INDEX)

    def watches(self) -> list[CatalogWatch]:
        out = []
        for r in self.gpkg.rows(WATCHES):
            try:
                out.append(CatalogWatch.from_json(json.loads(r["DATA"])))
            except (ValueError, KeyError, TypeError):
                continue
        return sorted(out, key=lambda w: w.created)

    def watch(self, watch_id: str) -> Optional[CatalogWatch]:
        return next((w for w in self.watches() if w.id == watch_id), None)

    def put_watch(self, watch: CatalogWatch) -> None:
        self.gpkg.upsert(WATCHES, [{"WATCH_ID": watch.id, "NAME": watch.name, "DATA": json.dumps(watch.to_json(), ensure_ascii=False)}])

    def delete_watch(self, watch_id: str) -> None:
        """Deletes a watch with its notices."""
        self.gpkg.delete(WATCHES, '"WATCH_ID" = ?', (watch_id,))
        self.gpkg.delete(NOTICES, '"WATCH_ID" = ?', (watch_id,))

    def _fill(self) -> None:
        """The list's columns of rows written before they were there (or by an older plugin): from their DATA, once."""
        for fid, data in self.gpkg.select(NOTICES, ("fid", "DATA"), '"IMAGE_ID" IS NULL'):
            try:
                n = WatchNotice.from_json(json.loads(data))
            except (ValueError, KeyError, TypeError):
                n = None
            if n is None:
                self.gpkg.delete(NOTICES, "fid = ?", (fid,))  # unreadable: never listed before either
                continue
            self.gpkg.update(NOTICES, _columns(n), "fid = ?", (fid,))

    def notices(self) -> list[WatchNotice]:
        """
        The notices, newest first, without their features (`complete` False;
        `complete` reads them): tens of thousands are listed quickly.
        """
        self._fill()
        out = []
        for (notice_id, watch_id, found, read, watch_name, image_id, acquired, sensor, label, kind, earlier) in self.gpkg.select(
                NOTICES, _LIST, order=_ORDER):
            try:
                value = json.loads(acquired) if acquired else None
            except ValueError:
                value = None
            out.append(WatchNotice(id=notice_id, watch_id=watch_id or "", watch_name=watch_name or "", catalog_label=label or "",
                                   found=float(found or 0), read=bool(read), image_id=image_id or "", acquired=value, sensor=sensor or "",
                                   feature={}, kind=kind, earlier=bool(earlier), complete=False))
        return out

    def complete(self, notices: Sequence[WatchNotice]) -> list[WatchNotice]:
        """`notices` with their features and extra (as kept now, with their read state); those gone left out."""
        wanted = [n.id for n in notices if not n.complete]
        read: dict[str, WatchNotice] = {}
        for chunk in _chunks(wanted):
            for notice_id, is_read, data in self.gpkg.select(NOTICES, ("NOTICE_ID", "IS_READ", "DATA"), f'"NOTICE_ID" IN ({_marks(chunk)})',
                                                              chunk, order=""):
                try:
                    n = WatchNotice.from_json(json.loads(data))
                except (ValueError, KeyError, TypeError):
                    continue
                n.read = bool(is_read)
                read[notice_id] = n
        return [n if n.complete else read[n.id] for n in notices if n.complete or n.id in read]

    def unread(self) -> int:
        return self.gpkg.select(NOTICES, ("COUNT(*)",), '"IS_READ" = 0', order="")[0][0]

    def add_notices(self, notices: Sequence[WatchNotice], replace_existing: bool = False) -> int:
        """
        Adds notices, keeping at most MAX_NOTICES (the oldest go first); those
        already there stay as they are (read, what was found out), unless
        `replace_existing`. How many were added.
        """
        if not notices:
            return 0
        rows = [_row(n) for n in notices]
        if replace_existing:
            self.gpkg.upsert(NOTICES, rows)
            added = len(rows)
        else:
            added = len(self.gpkg.insert_new(NOTICES, rows))
        if added:
            self.gpkg.delete(NOTICES, f'fid IN (SELECT fid FROM "{NOTICES.name}" ORDER BY {_ORDER} LIMIT -1 OFFSET ?)', (MAX_NOTICES,))
        return added

    def put_extra(self, notice_id: str, key: str, value) -> bool:
        """
        Keeps what was found out about a notice's image under `key` of its
        `extra` (its read state as it is now kept); False when the notice is gone.
        """
        rows = self.gpkg.select(NOTICES, ("DATA",), '"NOTICE_ID" = ?', (notice_id,), order="")
        if not rows:
            return False
        try:
            data = json.loads(rows[0][0])
        except (ValueError, TypeError):
            return False
        extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
        data["extra"] = {**extra, key: value}
        self.gpkg.update(NOTICES, {"DATA": json.dumps(data, ensure_ascii=False)}, '"NOTICE_ID" = ?', (notice_id,))
        return True

    def mark_read(self, ids: Optional[Sequence[str]] = None) -> None:
        """Marks notices read (all when `ids` is not given)."""
        if ids is None:
            self.gpkg.update(NOTICES, {"IS_READ": 1}, "1=1")
            return
        for chunk in _chunks(list(ids)):
            self.gpkg.update(NOTICES, {"IS_READ": 1}, f'"NOTICE_ID" IN ({_marks(chunk)})', chunk)

    def delete_notices(self, ids: Optional[Sequence[str]] = None) -> None:
        """Deletes notices (all when `ids` is not given)."""
        if ids is None:
            self.gpkg.delete(NOTICES, "1=1")
            return
        for chunk in _chunks(list(ids)):
            self.gpkg.delete(NOTICES, f'"NOTICE_ID" IN ({_marks(chunk)})', chunk)


@dataclass
class Checked:
    """What checks added: new images (announced), images already there (`earlier`, listed as read) and those left out of the past."""
    new: int = 0
    earlier: int = 0
    left: int = 0
    #: The checks that failed, by watch name: why.
    failed: Optional[dict[str, str]] = None

    def __iadd__(self, other: "Checked") -> "Checked":
        self.new += other.new
        self.earlier += other.earlier
        self.left += other.left
        if other.failed:
            self.failed = {**(self.failed or {}), **other.failed}
        return self


def check_one(store: WatchStore, watch: CatalogWatch, ask_for: Callable[[CatalogWatch], tuple[CatalogWatch, EsriAsk]],
              now: Optional[float] = None, cancelled: Optional[Callable[[], bool]] = None) -> Checked:
    """
    Checks one watch, keeping its notices (and the images already there, while
    its past is read). The watch may be deleted or changed meanwhile: what the
    person did is kept, what was found added.
    """
    now = time.time() * 1000 if now is None else now
    asked, ask = ask_for(watch)
    result = check_watch(asked, ask, now, cancelled)
    current = store.watch(watch.id)
    if current is None:
        return Checked()
    # The past was read for the areas as they were asked: changed meanwhile, it is read again.
    past = result.watch.past if current.past == watch.past else current.past
    after = replace(result.watch, catalog=watch.catalog, name=current.name, enabled=current.enabled, conditions=current.conditions,
                    areas=current.areas, area_layer=current.area_layer, past=past,
                    past_left=result.watch.past_left if past is None else current.past_left,
                    seen=list(dict.fromkeys(current.seen + result.watch.seen))[-MAX_SEEN:])
    store.put_watch(after)
    new = store.add_notices([replace(n, watch_name=current.name) for n in result.notices])
    earlier = store.add_notices([replace(n, watch_name=current.name) for n in result.earlier])
    # Told once: when the past was read by this check.
    left = after.past_left if asked.past is not None and result.watch.past is None else 0
    return Checked(new, earlier, left, {current.name: after.last_error} if after.last_error else None)


def check_due(store: WatchStore, ask_for: Callable[[CatalogWatch], tuple[CatalogWatch, EsriAsk]], interval: float,
              force: bool = False, now: Optional[float] = None, cancelled: Optional[Callable[[], bool]] = None) -> Checked:
    """
    Checks the enabled watches last tried longer than `interval` ms ago (all
    of them when `force`, and those with their past to read), keeping their
    notices; what was added. `ask_for` gives the watch to ask with (its
    catalog refreshed from the settings file) and how to ask its catalog.
    """
    now = time.time() * 1000 if now is None else now

    def due(w: CatalogWatch) -> bool:
        last = w.last_tried if w.last_tried is not None else w.last_checked
        return w.enabled and (force or last is None or w.past is not None or now - last >= interval - 5000)

    checked = Checked()
    for watch in [w for w in store.watches() if due(w)]:
        if cancelled and cancelled():
            break
        checked += check_one(store, watch, ask_for, now, cancelled)
    return checked
