"""The watches and notices of the image catalogs (core.catalog.watch) in the local GeoPackage, and checking the watches that are due."""
from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from typing import Callable, Optional, Sequence

from ..gpkg import Column, GeoPackage, TableSpec
from ..util import time_of
from .watch import MAX_NOTICES, MAX_SEEN, CatalogWatch, EsriAsk, WatchNotice, check_watch

WATCHES = TableSpec("catalog_watches", None, (Column("WATCH_ID", "TEXT(40)"), Column("NAME", "TEXT(255)"), Column("DATA", "TEXT")),
                    key="WATCH_ID", description="新着画像の監視条件")
NOTICES = TableSpec("watch_notices", None, (
    Column("NOTICE_ID", "TEXT"), Column("WATCH_ID", "TEXT(40)"), Column("FOUND", "DOUBLE"), Column("IS_READ", "SMALLINT"), Column("DATA", "TEXT"),
), key="NOTICE_ID", description="新着画像の通知")


class WatchStore:
    """The watches and notices in the local GeoPackage."""

    def __init__(self, gpkg: GeoPackage):
        self.gpkg = gpkg
        gpkg.ensure(WATCHES)
        gpkg.ensure(NOTICES)

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

    def notices(self) -> list[WatchNotice]:
        """The notices, newest first."""
        out = []
        for r in self.gpkg.rows(NOTICES):
            try:
                n = WatchNotice.from_json(json.loads(r["DATA"]))
            except (ValueError, KeyError, TypeError):
                continue
            n.read = bool(r["IS_READ"])
            out.append(n)

        def order(n: WatchNotice) -> tuple:
            t = time_of(n.acquired)
            return (n.found, 0 if math.isnan(t) else t)

        return sorted(out, key=order, reverse=True)

    def unread(self) -> int:
        return sum(1 for n in self.notices() if not n.read)

    def add_notices(self, notices: Sequence[WatchNotice]) -> None:
        """Adds notices, keeping at most MAX_NOTICES (the oldest go first)."""
        if not notices:
            return
        self.gpkg.upsert(NOTICES, [{"NOTICE_ID": n.id, "WATCH_ID": n.watch_id, "FOUND": n.found, "IS_READ": int(n.read),
                                    "DATA": json.dumps(n.to_json(), ensure_ascii=False)} for n in notices])
        extra = self.notices()[MAX_NOTICES:]
        if extra:
            self.delete_notices([n.id for n in extra])

    def mark_read(self, ids: Optional[Sequence[str]] = None) -> None:
        """Marks notices read (all when `ids` is not given)."""
        if ids is None:
            self.gpkg.update(NOTICES, {"IS_READ": 1}, "1=1")
        else:
            for i in ids:
                self.gpkg.update(NOTICES, {"IS_READ": 1}, '"NOTICE_ID" = ?', (i,))

    def delete_notices(self, ids: Optional[Sequence[str]] = None) -> None:
        """Deletes notices (all when `ids` is not given)."""
        if ids is None:
            self.gpkg.delete(NOTICES, "1=1")
            return
        for i in ids:
            self.gpkg.delete(NOTICES, '"NOTICE_ID" = ?', (i,))


def check_due(store: WatchStore, ask_for: Callable[[CatalogWatch], tuple[CatalogWatch, EsriAsk]], interval: float,
              force: bool = False, now: Optional[float] = None, cancelled: Optional[Callable[[], bool]] = None) -> int:
    """
    Checks the enabled watches last tried longer than `interval` ms ago (all
    of them when `force`), keeping their notices; the number of new notices.
    `ask_for` gives the watch to ask with (its catalog refreshed from the
    settings file) and how to ask its catalog.
    """
    now = time.time() * 1000 if now is None else now

    def due(w: CatalogWatch) -> bool:
        last = w.last_tried if w.last_tried is not None else w.last_checked
        return w.enabled and (force or last is None or now - last >= interval - 5000)

    added = 0
    for watch in [w for w in store.watches() if due(w)]:
        if cancelled and cancelled():
            break
        asked, ask = ask_for(watch)
        result = check_watch(asked, ask, now)
        # The watch may have been deleted or changed meanwhile: keep what the person did, add what was found.
        current = store.watch(watch.id)
        if current is None:
            continue
        after = replace(result.watch, catalog=watch.catalog, name=current.name, enabled=current.enabled, conditions=current.conditions,
                        areas=current.areas, seen=list(dict.fromkeys(current.seen + result.watch.seen))[-MAX_SEEN:])
        store.put_watch(after)
        store.add_notices([replace(n, watch_name=current.name) for n in result.notices])
        added += len(result.notices)
    return added
