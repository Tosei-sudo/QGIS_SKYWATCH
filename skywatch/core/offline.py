"""
一時的なオフライン作業 (tsudo, 2026-11-08): when a server (an Esri feature
service) cannot be reached, work goes on with what was last read from it,
and what is changed meanwhile waits on this computer to be sent once it
answers again.

Two parts, both in one SQLite file of its own (never the local GeoPackage of
「ローカル」 records: work kept here belongs to its server's store and is only
waiting, so it is allowed even with `localObservations: false`):

- 控え (the response cache): every read answered by a server (layer
  information and queries) is kept, keyed by its URL and form without the
  token. While that server is down the same read is answered from here, so
  the classification, the records, the ledger, place names, … still show.
- 送信待ち (the outbox): a change made while the server is down —
  observation records, checked areas, imaging requests, imaging plans — is
  kept per store and item (the latest version of each item, with what it was
  on the server before the first change, so sending it later does not save
  over someone else's change). The stores read it back over what the server
  (or the cache) holds, so the work shows as done; the screens mark it as
  送信待ち.

A server is marked down when a request to it fails without an answer
(UnreachableError: no connection, a timeout, a 5xx). Its requests then go
straight to the cache (no waiting for each to time out) until a probe
(`probe`) gets an answer. Upload or discard is then the person's choice.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from .esri import Transport, UnreachableError

#: The kinds of work kept, in the order they are counted and sent, with their names on screen.
KINDS: dict[str, str] = {
    "observation": "観測記録",
    "area": "確認範囲",
    "request": "撮像依頼",
    "plan": "撮像計画",
}
#: Requests that change data: never answered from the cache.
_EDITS = re.compile(r"/(applyEdits|addFeatures|updateFeatures|deleteFeatures|calculate|append)/?$", re.IGNORECASE)
#: Reads kept in the cache: layer / service information and queries.
_READS = re.compile(r"(/(FeatureServer|MapServer)(/\d+)?|/query|/queryRelatedRecords)/?$", re.IGNORECASE)
#: Form values left out of the cache key (they change between sessions but not the answer).
_NOT_KEYED = {"token"}


def now_ms() -> int:
    return int(time.time() * 1000)


def host_of(url: str) -> str:
    """The server of a URL (scheme and host, lower case)."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" if parts.netloc else url.split("?", 1)[0].lower()


def is_edit(url: str) -> bool:
    return bool(_EDITS.search(url.split("?", 1)[0]))


def is_cached_read(url: str) -> bool:
    path = url.split("?", 1)[0]
    return not is_edit(path) and bool(_READS.search(path))


def cache_key(url: str, form: dict) -> str:
    kept = sorted((str(k), str(v)) for k, v in form.items() if k not in _NOT_KEYED)
    text = json.dumps([url.split("?", 1)[0].rstrip("/"), kept], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    """JSON-safe (NaN and infinities as null)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass
class Pending:
    """One item waiting to be sent."""
    kind: str
    store: str
    item_id: str
    #: 'save' (added or changed) or 'remove'.
    op: str
    data: dict
    #: Its 更新日時 on the server before the first change made offline (None: new, or not tracked).
    base: Optional[float] = None
    #: Whether it was on the server before (False: made offline).
    known: bool = False
    #: When it was last changed here (ms).
    at: int = 0
    #: What it is, on screen (a name).
    title: str = ""
    #: Why the last try to send it failed ('' none yet).
    note: str = ""
    seq: int = 0


@dataclass
class UploadReport:
    """What became of sending the waiting work."""
    sent: dict = field(default_factory=dict)
    #: Moved to 「未保存の記録」 (observation records someone else changed meanwhile).
    drafts: int = 0
    #: Left waiting, with why.
    kept: list = field(default_factory=list)

    def add(self, kind: str, n: int = 1) -> None:
        if n:
            self.sent[kind] = self.sent.get(kind, 0) + n

    def text(self) -> str:
        parts = [f"{KINDS.get(k, k)} {n} 件" for k, n in self.sent.items() if n]
        out = ("送信: " + "・".join(parts)) if parts else "送信したものなし"
        if self.drafts:
            out += f"。ほかの人が先に更新していた観測記録 {self.drafts} 件は「未保存の記録」へ（開いて確かめてから送信）"
        if self.kept:
            out += f"。送れず残したもの {len(self.kept)} 件（{self.kept[0]}{' ほか' if len(self.kept) > 1 else ''}）"
        return out


class OfflineWork:
    """The cache, the outbox and which servers are down; shared by the plugin's parts (thread safe)."""

    def __init__(self, path: str, cache_mb: float = 500, enabled: bool = True,
                 clock: Callable[[], int] = now_ms):
        self.path = path
        self.enabled = enabled
        self.cache_bytes = int(max(cache_mb, 0) * 1024 * 1024)
        self.clock = clock
        self.lock = threading.RLock()
        #: Servers down: host → (since when, the URL that failed, why).
        self.down_hosts: dict[str, tuple[int, str, str]] = {}
        #: Worked offline by choice (every server treated as down until switched back).
        self.forced = False
        #: Called (from any thread) when a server goes down or comes back, or the outbox changes.
        self.listeners: list[Callable[[], None]] = []
        self._db: Optional[sqlite3.Connection] = None
        self._puts = 0

    # ---- the file -------------------------------------------------------

    def db(self) -> sqlite3.Connection:
        with self.lock:
            if self._db is None:
                folder = os.path.dirname(self.path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, host TEXT, url TEXT, body BLOB,"
                           " at INTEGER, size INTEGER)")
                db.execute("CREATE INDEX IF NOT EXISTS responses_at ON responses(at)")
                db.execute("CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, store TEXT,"
                           " item_id TEXT, op TEXT, data TEXT, base REAL, known INTEGER, at INTEGER, title TEXT, note TEXT,"
                           " UNIQUE(kind, store, item_id))")
                db.commit()
                self._db = db
            return self._db

    def close(self) -> None:
        with self.lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _changed(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 (a screen gone)
                pass

    # ---- which servers are down ----------------------------------------

    def down(self, url: str = "") -> bool:
        """Whether the server of `url` is treated as down (any server when `url` is empty)."""
        if not self.enabled:
            return False
        with self.lock:
            if self.forced:
                return True
            return bool(self.down_hosts) if not url else host_of(url) in self.down_hosts

    @property
    def offline(self) -> bool:
        return self.down()

    def mark_down(self, url: str, reason: str = "") -> None:
        if not self.enabled:
            return
        host = host_of(url)
        with self.lock:
            fresh = host not in self.down_hosts
            if fresh:
                self.down_hosts[host] = (self.clock(), url, reason)
        if fresh:
            self._changed()

    def mark_up(self, host: str) -> None:
        with self.lock:
            gone = self.down_hosts.pop(host, None) is not None
        if gone:
            self._changed()

    def set_forced(self, forced: bool) -> None:
        with self.lock:
            changed = self.forced != forced
            self.forced = forced
        if changed:
            self._changed()

    def probes(self) -> list[tuple[str, str]]:
        """The servers down and a URL of each to ask whether it answers again."""
        with self.lock:
            return [(host, url) for host, (_since, url, _why) in self.down_hosts.items()]

    def probe(self, send: Transport) -> list[str]:
        """
        Asks each server down whether it answers again (through `send`, the
        network itself); those that do are marked up. Any answer counts, even
        an error one: the server is there. The hosts back.
        """
        back = []
        for host, url in self.probes():
            try:
                send(url, {"f": "json"})
            except UnreachableError:
                continue
            except Exception:  # noqa: BLE001 (refused, but it answered)
                pass
            self.mark_up(host)
            back.append(host)
        return back

    def since(self) -> Optional[int]:
        """Since when work has been offline (the first server down), None when online."""
        with self.lock:
            return min((s for s, _u, _w in self.down_hosts.values()), default=None)

    def reasons(self) -> list[str]:
        with self.lock:
            return [f"{host}（{why}）" if why else host for host, (_s, _u, why) in self.down_hosts.items()]

    # ---- the cache ------------------------------------------------------

    def cached(self, url: str, form: dict) -> Optional[tuple[bytes, int]]:
        """The answer kept for this read and when it was read; None when none is kept."""
        try:
            with self.lock:
                row = self.db().execute("SELECT body, at FROM responses WHERE key = ?", (cache_key(url, form),)).fetchone()
        except sqlite3.Error:
            return None
        return (bytes(row[0]), int(row[1])) if row else None

    def remember(self, url: str, form: dict, body: bytes) -> None:
        if not self.enabled or self.cache_bytes <= 0:
            return
        if body[:64].lstrip().startswith(b'{"error"'):
            return  # an error answer never replaces a good one
        try:
            with self.lock:
                db = self.db()
                db.execute("INSERT OR REPLACE INTO responses (key, host, url, body, at, size) VALUES (?, ?, ?, ?, ?, ?)",
                           (cache_key(url, form), host_of(url), url.split("?", 1)[0], sqlite3.Binary(body), self.clock(), len(body)))
                self._puts += 1
                if self._puts % 50 == 1:
                    self._trim(db)
                db.commit()
        except sqlite3.Error:
            pass

    def _trim(self, db: sqlite3.Connection) -> None:
        total = db.execute("SELECT COALESCE(SUM(size), 0) FROM responses").fetchone()[0]
        if total <= self.cache_bytes:
            return
        target = int(self.cache_bytes * 0.8)
        for key, size in db.execute("SELECT key, size FROM responses ORDER BY at").fetchall():
            if total <= target:
                break
            db.execute("DELETE FROM responses WHERE key = ?", (key,))
            total -= size or 0

    def cache_info(self) -> tuple[int, int, Optional[int]]:
        """How many answers are kept, their size (bytes) and when the newest was read."""
        try:
            with self.lock:
                n, size, at = self.db().execute("SELECT COUNT(*), COALESCE(SUM(size), 0), MAX(at) FROM responses").fetchone()
        except sqlite3.Error:
            return 0, 0, None
        return int(n), int(size), at

    def clear_cache(self) -> None:
        with self.lock:
            db = self.db()
            db.execute("DELETE FROM responses")
            db.commit()

    def transport(self, send: Transport) -> Transport:
        """
        `send` with the cache: reads are kept; a server down is not asked (its
        reads come from the cache, its edits fail at once with
        UnreachableError, so the stores keep them to send later).
        """

        def offline_send(url: str, form: dict) -> bytes:
            read = is_cached_read(url)
            if self.down(url):
                kept = self.cached(url, form) if read else None
                if kept is not None:
                    return kept[0]
                raise UnreachableError(f"オフライン中（{host_of(url)} に接続できず、{'控えなし' if read else '変更は送信待ちに'}）")
            try:
                body = send(url, form)
            except UnreachableError as error:
                if not self.enabled:
                    raise
                self.mark_down(url, str(error))
                kept = self.cached(url, form) if read else None
                if kept is not None:
                    return kept[0]
                raise
            if read:
                self.remember(url, form, body)
            return body

        return offline_send

    # ---- the outbox -----------------------------------------------------

    def _pending_of(self, row) -> Pending:
        seq, kind, store, item_id, op, data, base, known, at, title, note = row
        try:
            value = json.loads(data) if data else {}
        except ValueError:
            value = {}
        return Pending(kind, store, item_id, op, value if isinstance(value, dict) else {}, base, bool(known), at or 0,
                       title or "", note or "", seq)

    _COLUMNS = "seq, kind, store, item_id, op, data, base, known, at, title, note"

    def pending(self, kind: Optional[str] = None, store: Optional[str] = None) -> list[Pending]:
        """The items waiting (of a kind, of a store), in the order first kept."""
        where, args = [], []
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if store is not None:
            where.append("store = ?")
            args.append(store)
        sql = f"SELECT {self._COLUMNS} FROM outbox" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY seq"
        try:
            with self.lock:
                rows = self.db().execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        return [self._pending_of(r) for r in rows]

    def item(self, kind: str, store: str, item_id: str) -> Optional[Pending]:
        try:
            with self.lock:
                row = self.db().execute(f"SELECT {self._COLUMNS} FROM outbox WHERE kind = ? AND store = ? AND item_id = ?",
                                        (kind, store, item_id)).fetchone()
        except sqlite3.Error:
            return None
        return self._pending_of(row) if row else None

    def ids(self, kind: str, store: str) -> set[str]:
        return {p.item_id for p in self.pending(kind, store)}

    def keep(self, kind: str, store: str, item_id: str, op: str, data: dict, base: Optional[float] = None,
             known: bool = False, title: str = "") -> Pending:
        """
        Keeps an item to send (replacing what was kept for it, but not what it
        was on the server before: the first change's `base` and `known` stay,
        and so do the saves whose answer was lost, data["tried"]).
        """
        with self.lock:
            before = self.item(kind, store, item_id)
            if before is not None:
                base, known = before.base, before.known
                tried = sorted(set(tried_of(before)) | set(data.get("tried") or ()))
                if tried:
                    data = dict(data, tried=tried)
            db = self.db()
            text = json.dumps(_plain(data), ensure_ascii=False)
            if before is None:
                db.execute("INSERT INTO outbox (kind, store, item_id, op, data, base, known, at, title, note)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
                           (kind, store, item_id, op, text, base, 1 if known else 0, self.clock(), title))
            else:
                db.execute("UPDATE outbox SET op = ?, data = ?, at = ?, title = ?, note = '' WHERE seq = ?",
                           (op, text, self.clock(), title, before.seq))
            db.commit()
            kept = self.item(kind, store, item_id)
        self._changed()
        return kept

    def add_tried(self, kind: str, store: str, item_id: str, stamps: Iterable[Optional[float]]) -> None:
        """
        Remembers the 更新日時 of saves of an item kept here whose answer was
        lost (a timeout: the server may hold them), so sending it later does
        not take that save for someone else's change (data["tried"]).
        """
        new = [float(s) for s in stamps if isinstance(s, (int, float)) and math.isfinite(s)]
        if not new:
            return
        with self.lock:
            p = self.item(kind, store, item_id)
            if p is None:
                return
            tried = sorted(set(tried_of(p)) | set(new))
            data = dict(p.data, tried=tried)
            db = self.db()
            db.execute("UPDATE outbox SET data = ? WHERE seq = ?", (json.dumps(_plain(data), ensure_ascii=False), p.seq))
            db.commit()

    def note(self, item: Pending, note: str) -> None:
        with self.lock:
            db = self.db()
            db.execute("UPDATE outbox SET note = ? WHERE seq = ?", (note, item.seq))
            db.commit()

    def drop(self, kind: str, store: str, item_ids: Iterable[str]) -> int:
        ids = list(item_ids)
        if not ids:
            return 0
        with self.lock:
            db = self.db()
            n = 0
            for item_id in ids:
                n += db.execute("DELETE FROM outbox WHERE kind = ? AND store = ? AND item_id = ?", (kind, store, item_id)).rowcount
            db.commit()
        if n:
            self._changed()
        return n

    def discard(self, kind: Optional[str] = None) -> int:
        """Throws away what is waiting (of a kind, or all); how many."""
        with self.lock:
            db = self.db()
            n = db.execute("DELETE FROM outbox" + (" WHERE kind = ?" if kind else ""), (kind,) if kind else ()).rowcount
            db.commit()
        if n:
            self._changed()
        return n

    def counts(self) -> dict[str, int]:
        """How many items wait, by kind (KINDS order; kinds with none left out)."""
        try:
            with self.lock:
                rows = self.db().execute("SELECT kind, COUNT(*) FROM outbox GROUP BY kind").fetchall()
        except sqlite3.Error:
            return {}
        got = dict(rows)
        return {k: got[k] for k in [*KINDS, *sorted(set(got) - set(KINDS))] if got.get(k)}

    def total(self) -> int:
        return sum(self.counts().values())

    def summary(self) -> list[tuple[str, str, int]]:
        """(kind's name, store, how many) for every store with work waiting."""
        try:
            with self.lock:
                rows = self.db().execute("SELECT kind, store, COUNT(*) FROM outbox GROUP BY kind, store").fetchall()
        except sqlite3.Error:
            return []
        order = {k: i for i, k in enumerate(KINDS)}
        rows.sort(key=lambda r: (order.get(r[0], 99), r[1]))
        return [(KINDS.get(k, k), store, n) for k, store, n in rows]


def tried_of(p: Pending) -> list[float]:
    """The 更新日時 of the saves of a waiting item whose answer was lost (OfflineWork.add_tried)."""
    return [float(s) for s in p.data.get("tried") or () if isinstance(s, (int, float)) and not isinstance(s, bool)]


def offline_of(client: Any) -> Optional[OfflineWork]:
    """The offline work an Esri client keeps to (None: none; the client works only online)."""
    work = getattr(client, "offline", None)
    return work if isinstance(work, OfflineWork) and work.enabled else None


def unreachable(error: BaseException) -> bool:
    return isinstance(error, UnreachableError)
