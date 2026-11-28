"""
機能の使われ方 (tsudo, 2026-11-28: 「どう使われているか収集してみる？ ロギング
ツールみたいだけど、機密情報は入れないツールだし問題ないと思う」): how often
each entry of the plugin's menu, each toolbar button and key, and each button
inside the docks is used, by day and by the roles of the person, to see what
could be folded away (the review of 2026-11-27 found too much shown at once).

Counted, never one line per click: (day, roles, plugin version, dock, feature,
place) → how many times, and how many minutes (a dock shown, the plugin
running). The places (PLACES): the plugin's menu, its toolbar, a key, the map's
right-click menu, a help link, 文で操作, a control inside a dock, a dock shown,
the plugin started.

Never in it: what was typed or chosen (a box's text, a list's value), the
contents of images or records, file names, coordinates, the person's name.
Labels of buttons lose the numbers in them (「通知（3）」 → 「通知」, other
digits → #).

Who: a random id per QGIS profile (USER_KEY), not the user name, so the
numbers say how many people use a feature, not who (the same person on two
PCs counts twice). The roles ticked (担当) go with each row.

Where:

- `Counter.add`, on the main thread, adds to a dict and nothing else (no file,
  no Qt, no network);
- every SAVE_MINUTES what was counted is merged into this profile's file
  (mikage/feature-use/counts.json) in a background task;
- with the settings file's `featureUse` table (TBL_FEATURE_USE), every
  `sendMinutes` what is new is sent as rows that are only added, never
  changed: the table's sums are the counts. Each batch has an id (SEND_ID):
  a batch whose answer did not come is looked for before it is sent again,
  so nothing is counted twice. What cannot be sent stays in the file.

`featureUse`: false counts nothing; absent, counted on this PC only (「機能の
使われ方…」 shows this PC's); a table URL (or { url, fields, sendMinutes }):
sent there. MIKAGE 練習版 and 練習モード count nothing.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional, Sequence

from .esri import EsriClient, EsriError, fit_value, quote
from .sources import EsriSource, esri_source_of
from .sql import timestamp
from .util import is_record

#: Where a feature was used from, with its name on screen.
PLACES: dict[str, str] = {
    "MENU": "メニュー",
    "TOOLBAR": "ツールバー",
    "KEY": "キー",
    "MAP_MENU": "地図の右クリック",
    "HELP": "ヘルプのリンク",
    "COMMAND": "文で操作",
    "OTHER": "そのほか",
    "DOCK": "ドックの中",
    "SHOWN": "ドックの表示",
    "START": "起動",
}
DOCK, SHOWN, START, OTHER = "DOCK", "SHOWN", "START", "OTHER"
#: The places counted as uses of a feature (not a dock shown, not the plugin started).
USE_PLACES = tuple(p for p in PLACES if p not in (SHOWN, START))
#: The feature of the START rows (the plugin running: started how many times, for how many minutes).
START_FEATURE = "MIKAGE"
#: A role filter for the people who ticked no role.
NO_ROLE = "NONE"

#: The table's attributes by role (`featureUse.fields`).
DEFAULT_FIELDS = {
    "date": "USE_DATE", "user": "USER_KEY", "roles": "ROLE_NAMES", "dock": "DOCK_NAME", "feature": "FEATURE_NAME",
    "place": "PLACE_KIND", "count": "USE_COUNT", "minutes": "USE_MINUTES", "version": "PLUGIN_VERSION", "send": "SEND_ID",
}
#: Without these the table is not written to (E704).
REQUIRED = ("date", "user", "feature", "place", "count")

#: How often what was counted goes to this PC's file (minutes), and the table by default.
SAVE_MINUTES = 10
SEND_MINUTES = 60
#: The least `sendMinutes` (a busy server is not asked every minute by every PC).
MIN_SEND_MINUTES = 5
#: The first send after the start (s): after QGIS's own start and the read ahead.
FIRST_SEND = 180
#: Rows sent in one request (each with its own SEND_ID).
BATCH_ROWS = 500
#: Days kept on this PC (counted here only, or not sent yet).
KEEP_DAYS = 400
#: A label's length at most.
MAX_LABEL = 60
FILE = "counts.json"

_COUNT_IN_BRACKETS = re.compile(r"\s*[（(][^（()）]*\d[^（()）]*[）)]")
_DIGITS = re.compile(r"\d+(?:[.,:/]\d+)*")
_SPACE = re.compile(r"\s+")
#: Where a tooltip's explanation starts.
_EXPLANATION = re.compile(r"[（(。]")

#: A row: day (yyyy-mm-dd, this PC's), roles ("OBSERVER,INTERPRETER"), version, dock, feature, place, count, minutes.
Row = list
Key = tuple


@dataclass(frozen=True)
class FeatureUseSettings:
    """The settings file's `featureUse`."""
    enabled: bool = True
    #: The table rows are sent to; None: counted on this PC only.
    source: Optional[EsriSource] = None
    send_minutes: float = SEND_MINUTES


DEFAULT_FEATURE_USE = FeatureUseSettings()


def feature_use_of(value: Any, problems: list[str]) -> FeatureUseSettings:
    """`featureUse`: false / true, a table's URL, or { "url", "fields", "where", "sendMinutes", "enabled" }."""
    if isinstance(value, bool):
        return FeatureUseSettings(enabled=value)
    if isinstance(value, str):
        return FeatureUseSettings(source=esri_source_of(value, "featureUse", DEFAULT_FIELDS, problems, editable=True))
    if not is_record(value):
        problems.append('featureUse は false か、テーブルの URL か { "url", "fields", "sendMinutes" } で指定')
        return DEFAULT_FEATURE_USE
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append("featureUse.enabled は true / false で指定")
        enabled = True
    source = None
    if "url" in value:
        source = esri_source_of({k: value[k] for k in ("url", "fields", "where") if k in value}, "featureUse", DEFAULT_FIELDS,
                                problems, editable=True)
    minutes = value.get("sendMinutes", SEND_MINUTES)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes < MIN_SEND_MINUTES:
        problems.append(f"featureUse.sendMinutes は {MIN_SEND_MINUTES} 以上の分数で指定")
        minutes = SEND_MINUTES
    unknown = sorted(set(value) - {"url", "fields", "where", "sendMinutes", "enabled"})
    if unknown:
        problems.append(f"featureUse の {'・'.join(unknown)} は使えない項目（url・fields・where・sendMinutes・enabled）")
    return FeatureUseSettings(enabled=enabled, source=source, send_minutes=float(minutes))


def label_of(text: Optional[str]) -> str:
    """A control's text as it is counted: no mnemonic &, no counts in brackets, other digits as #, one line, MAX_LABEL at most."""
    text = (text or "").split("\n", 1)[0]
    text = text.replace("&&", "\0").replace("&", "").replace("\0", "&")
    text = _COUNT_IN_BRACKETS.sub("", text)
    text = _DIGITS.sub("#", text)
    return _SPACE.sub(" ", text).strip()[:MAX_LABEL]


def tip_label(text: Optional[str]) -> str:
    """A tooltip as a control's name: its first line up to the first bracket or 。 (the explanation after it left out)."""
    first = (text or "").split("\n", 1)[0]
    short = _EXPLANATION.split(first, 1)[0]
    return label_of(short if short.strip() else first)


def roles_text(codes: Iterable[str]) -> str:
    """The roles of a row: their codes in order, with commas ("" for none)."""
    return ",".join(sorted({c for c in codes if c}))


def new_user_key() -> str:
    """A profile's id in the table (random: nothing of the person's name or computer in it)."""
    return secrets.token_hex(8)


def today() -> str:
    return date.today().isoformat()


def day_ms(day: str) -> int:
    """The start of a day of this PC (00:00 here) as the instant the table's date holds (ms since the epoch)."""
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day).timestamp() * 1000)


# ---- counting (the main thread) ----------------------------------------------------------------------------


class Counter:
    """What was used since the last `take`: (day, roles, version, dock, feature, place) → [count, minutes]."""

    def __init__(self, roles: str = "", version: str = "", clock: Callable[[], str] = today):
        self.roles = roles
        self.version = version
        self.paused = False
        self._clock = clock
        self._counts: dict[Key, list] = {}

    def add(self, feature: str, place: str, dock: str = "", count: int = 1, minutes: float = 0.0) -> None:
        if self.paused or not feature:
            return
        key = (self._clock(), self.roles, self.version, dock, feature, place)
        kept = self._counts.get(key)
        if kept is None:
            self._counts[key] = [count, minutes]
        else:
            kept[0] += count
            kept[1] += minutes

    def take(self) -> dict[Key, list]:
        """What was counted, and nothing counted any more (the dict itself handed over: no copy)."""
        counts, self._counts = self._counts, {}
        return counts

    def put_back(self, counts: dict[Key, list]) -> None:
        """Counts taken but not kept (the file could not be written) added back."""
        for key, (count, minutes) in counts.items():
            kept = self._counts.setdefault(key, [0, 0.0])
            kept[0] += count
            kept[1] += minutes

    def rows(self) -> list:
        """What is counted now, as the file's rows (not taken)."""
        return [[*key, value[0], value[1]] for key, value in self._counts.items()]

    def __len__(self) -> int:
        return len(self._counts)


# ---- this PC's file (any thread) ---------------------------------------------------------------------------


@dataclass
class Batch:
    """Rows to send together under one SEND_ID; `tried`: sent once without an answer (looked for first)."""
    id: str
    rows: list
    tried: bool = False


class UseStore:
    """
    This profile's counts (feature-use/counts.json): `pending` (not in a
    batch yet) and `batches` (to be sent). Each change reads, changes and
    writes the file under a lock (a task and the unload may write).
    """

    def __init__(self, folder: str):
        self.folder = folder
        self.path = os.path.join(folder, FILE)
        self.lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"pending": [], "batches": []}
        except (OSError, ValueError):
            # Not readable (cut by a crash while written elsewhere): put aside, started afresh.
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass
            return {"pending": [], "batches": []}
        if not isinstance(data, dict):
            return {"pending": [], "batches": []}
        data.setdefault("pending", [])
        data.setdefault("batches", [])
        return data

    def _write(self, data: dict) -> None:
        os.makedirs(self.folder, exist_ok=True)
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, self.path)

    @staticmethod
    def _recent(rows: Iterable, first: str) -> list:
        return [r for r in rows if isinstance(r, list) and len(r) == 8 and isinstance(r[0], str) and r[0] >= first]

    def merge(self, counts: dict[Key, list], now: Optional[str] = None) -> None:
        """Adds what was counted to `pending` (days older than KEEP_DAYS let go)."""
        first = (date.fromisoformat(now or today()) - timedelta(days=KEEP_DAYS)).isoformat()
        with self.lock:
            data = self._read()
            pending: dict[Key, list] = {}
            for r in self._recent(data["pending"], first):
                pending[tuple(r[:6])] = r
            for key, (count, minutes) in counts.items():
                kept = pending.get(key)
                if kept is None:
                    pending[key] = [*key, count, round(minutes, 3)]
                else:
                    kept[6] += count
                    kept[7] = round(kept[7] + minutes, 3)
            data["pending"] = [r for k, r in pending.items() if k[0] >= first]
            data["batches"] = [b for b in data["batches"] if isinstance(b, dict) and self._recent(b.get("rows") or [], first)]
            self._write(data)

    def batches(self, size: int = BATCH_ROWS) -> list[Batch]:
        """`pending` put into new batches (each its own id), then every batch not sent, oldest first."""
        with self.lock:
            data = self._read()
            pending = data["pending"]
            if pending:
                for i in range(0, len(pending), size):
                    data["batches"].append({"id": secrets.token_hex(16), "rows": pending[i:i + size], "tried": False})
                data["pending"] = []
                self._write(data)
            return [Batch(str(b.get("id")), list(b.get("rows") or []), bool(b.get("tried"))) for b in data["batches"]
                    if isinstance(b, dict) and b.get("id")]

    def _change(self, batch_id: str, change: Callable[[list], list]) -> None:
        with self.lock:
            data = self._read()
            data["batches"] = change([b for b in data["batches"] if isinstance(b, dict)])
            self._write(data)

    def tried(self, batch_id: str) -> None:
        self._change(batch_id, lambda bs: [{**b, "tried": True} if b.get("id") == batch_id else b for b in bs])

    def sent(self, batch_id: str) -> None:
        self._change(batch_id, lambda bs: [b for b in bs if b.get("id") != batch_id])

    def rows(self) -> list:
        """Every row kept here (pending and not sent)."""
        with self.lock:
            data = self._read()
        out = [r for r in data["pending"] if isinstance(r, list) and len(r) == 8]
        for b in data["batches"]:
            if isinstance(b, dict):
                out.extend(r for r in b.get("rows") or [] if isinstance(r, list) and len(r) == 8)
        return out

    def waiting(self) -> int:
        """How many rows wait to be sent."""
        return len(self.rows())


# ---- the table ---------------------------------------------------------------------------------------------


def _fields_present(client: EsriClient, source: EsriSource, token: Optional[str]):
    info = client.layer(source.url, token)
    types = {k.upper(): k for k in info.field_types}
    missing = [source.fields[k] for k in REQUIRED if source.fields[k].upper() not in types]
    if missing:
        raise EsriError(f"使われ方の表に必要な項目がない（管理者に連絡: {'・'.join(missing)}）", code="E704")
    # Each role's attribute as the table spells it (or None when it has none).
    names = {role: types.get(name.upper()) for role, name in source.fields.items()}
    return info, names


def attributes_of(row: Row, user_key: str, send_id: str, names: dict, info) -> dict:
    """A row of the table (only the attributes it has; texts cut to their lengths)."""
    day, roles, version, dock, feature, place, count, minutes = row
    values = {"date": day_ms(day), "user": user_key, "roles": roles, "dock": dock, "feature": feature, "place": place,
              "count": int(count), "minutes": round(float(minutes), 2), "version": version, "send": send_id}
    types, lengths = info.field_types, info.field_lengths
    out = {}
    for role, value in values.items():
        name = names.get(role)
        if not name:
            continue
        if isinstance(value, str) and lengths.get(name):
            value = value[:lengths[name]]
        out[name] = fit_value(value if value != "" else None, types.get(name))
    return out


def _arrived(client: EsriClient, source: EsriSource, send_name: Optional[str], batch_id: str, token: Optional[str]) -> bool:
    """Whether a batch sent once without an answer is in the table (no SEND_ID: taken as not)."""
    if not send_name:
        return False
    answer = client.ask(source.url + "/query", {"where": f"{send_name} = {quote(batch_id)}", "returnCountOnly": "true", "f": "json"}, token)
    return int(answer.get("count") or 0) > 0


def send(client: EsriClient, source: EsriSource, store: UseStore, user_key: str, token: Optional[str] = None,
         cancelled: Callable[[], bool] = lambda: False) -> int:
    """
    Sends the rows kept here (each batch in one request): how many were
    added. A batch the server refused stays (sent again next time); one
    whose answer did not come is looked for first.
    """
    batches = store.batches()
    if not batches:
        return 0
    info, names = _fields_present(client, source, token)
    added = 0
    for batch in batches:
        if cancelled():
            break
        if batch.tried and _arrived(client, source, names.get("send"), batch.id, token):
            store.sent(batch.id)
            continue
        adds = [{"attributes": attributes_of(r, user_key, batch.id, names, info)} for r in batch.rows]
        store.tried(batch.id)
        result = client.apply_edits(source.url, adds=adds, token=token)
        failures = result.failures()
        if failures:
            # Refused (not lost on the way): sent again as it is next time.
            raise EsriError(code="E305", detail="使われ方の表: " + "、".join(failures[:3]))
        store.sent(batch.id)
        added += len(adds)
    return added


def role_where(name: str, role: str) -> str:
    """The rows of people with `role` among theirs (NO_ROLE: of people with none; "": everyone)."""
    if not role:
        return ""
    if role == NO_ROLE:
        return f"({name} IS NULL OR {name} = '')"
    r = role.replace("'", "''")
    return f"({name} = '{r}' OR {name} LIKE '{r},%' OR {name} LIKE '%,{r}' OR {name} LIKE '%,{r},%')"


def where_of(source: EsriSource, first: str, last: str, role: str = "", roles_name: Optional[str] = None) -> str:
    """The rows of the days `first`…`last` (this PC's days, both included) and of a role."""
    f = source.fields
    parts = [f"{f['date']} >= {timestamp(day_ms(first))}",
             f"{f['date']} < {timestamp(day_ms((date.fromisoformat(last) + timedelta(days=1)).isoformat()))}"]
    if role:
        parts.append(role_where(roles_name or f["roles"], role))
    if source.where:
        parts.append(f"({source.where})")
    return " AND ".join(parts)


#: A sum over the table: (dock, feature, place, user, count, minutes).
Group = tuple


def table_groups(client: EsriClient, source: EsriSource, first: str, last: str, role: str = "", token: Optional[str] = None,
                 page: int = 2000, limit: int = 200_000) -> tuple[list[Group], bool]:
    """
    The table's sums by dock, feature, place and person for the days and the
    role (the server adds them up: the rows themselves are many). True when
    the server gave fewer groups than there are (its limit).
    """
    info, names = _fields_present(client, source, token)
    group_by = [names[k] for k in ("dock", "feature", "place", "user") if names.get(k)]
    stats = [{"statisticType": "sum", "onStatisticField": names["count"], "outStatisticFieldName": "USE_SUM"}]
    if names.get("minutes"):
        stats.append({"statisticType": "sum", "onStatisticField": names["minutes"], "outStatisticFieldName": "MINUTES_SUM"})
    if role and not names.get("roles"):
        raise EsriError(f"使われ方の表に担当の項目がない（{source.fields['roles']}）", code="E704")
    params = {"where": where_of(source, first, last, role, names.get("roles")), "outStatistics": json.dumps(stats),
              "groupByFieldsForStatistics": ",".join(group_by), "orderByFields": ",".join(group_by), "f": "json"}
    caps = info.raw.get("advancedQueryCapabilities") if isinstance(info.raw.get("advancedQueryCapabilities"), dict) else {}
    if caps.get("supportsStatistics") is False:
        raise EsriError("使われ方の表のサービスが集計（outStatistics）に対応していない", code="E104")
    # Sums in pages where the server says it can (ArcGIS 10.7 and later); else the first maxRecordCount of them.
    pages = bool(caps.get("supportsPaginationOnAggregatedQueries"))
    size = max(1, min(page, info.max_record_count or page))
    out: list[Group] = []
    capped = False
    offset = 0
    while True:
        ask = dict(params)
        if pages:
            ask.update(resultOffset=str(offset), resultRecordCount=str(size))
        answer = client.ask(source.url + "/query", ask, token)
        features = answer.get("features") or []
        for feature in features:
            a = {k.upper(): v for k, v in (feature.get("attributes") or {}).items()}

            def text(role: str) -> str:
                name = names.get(role)
                return str(a.get(name.upper()) or "") if name else ""

            out.append((text("dock"), text("feature"), text("place"), text("user"),
                        int(a.get("USE_SUM") or 0), float(a.get("MINUTES_SUM") or 0)))
        more = bool(answer.get("exceededTransferLimit"))
        if not more or not features:
            break
        if not pages or len(out) >= limit:
            capped = True
            break
        offset += len(features)
    return out, capped


def local_groups(rows: Iterable[Row], user_key: str, first: str, last: str, role: str = "") -> list[Group]:
    """This PC's rows as the table's sums (`table_groups`) for the days and the role."""
    sums: dict[tuple, list] = {}
    for day, roles, _version, dock, feature, place, count, minutes in rows:
        if not first <= day <= last or not has_role(roles, role):
            continue
        kept = sums.setdefault((dock, feature, place, user_key), [0, 0.0])
        kept[0] += int(count)
        kept[1] += float(minutes)
    return [(*k, v[0], v[1]) for k, v in sorted(sums.items())]


def has_role(roles: str, role: str) -> bool:
    if not role:
        return True
    codes = [c for c in (roles or "").split(",") if c]
    return not codes if role == NO_ROLE else role in codes


# ---- the summary of 「機能の使われ方…」 --------------------------------------------------------------------


@dataclass
class UseLine:
    """A feature (a menu entry, or a control of a dock) over the days chosen."""
    dock: str
    feature: str
    #: Uses (from every place but a dock shown).
    count: int = 0
    #: A dock's: how many times it was shown (also brought to the front among tabs) and for how many minutes.
    shown: int = 0
    minutes: float = 0.0
    places: dict = field(default_factory=dict)
    people: set = field(default_factory=set)

    @property
    def users(self) -> int:
        return len(self.people)

    @property
    def main_place(self) -> str:
        """Where it was used from most ("" when not used)."""
        used = [(n, p) for p, n in self.places.items() if n > 0]
        return max(used, key=lambda t: (t[0], -USE_PLACES.index(t[1]) if t[1] in USE_PLACES else 0))[1] if used else ""


@dataclass
class UseSummary:
    lines: list
    #: The people who used MIKAGE in the days (and of the role).
    people: int
    #: The table gave only part of its sums (its limit).
    capped: bool = False


def summarize(groups: Iterable[Group], known: Iterable[tuple[str, str]] = ()) -> UseSummary:
    """
    The groups by feature (dock, feature), with the features `known` that were
    not used at all; the least used first (by people, then uses).
    """
    lines: dict[tuple[str, str], UseLine] = {}
    everyone: set = set()
    for dock, feature, place, user, count, minutes in groups:
        if user:
            everyone.add(user)
        if place == START:
            continue
        line = lines.setdefault((dock, feature), UseLine(dock, feature))
        if place == SHOWN:
            line.shown += count
            line.minutes += minutes
        else:
            line.count += count
            line.places[place] = line.places.get(place, 0) + count
        if (count > 0 or minutes > 0) and user:
            line.people.add(user)
    for dock, feature in known:
        lines.setdefault((dock, feature), UseLine(dock, feature))
    ordered = sorted(lines.values(), key=lambda line: (line.users, line.count, line.shown, line.dock, line.feature))
    return UseSummary(ordered, len(everyone))


def csv_text(summary: UseSummary, newline: str = "\r\n") -> str:
    """The summary as CSV (a header, one line per feature)."""
    import csv
    import io

    buffer = io.StringIO()
    w = csv.writer(buffer, lineterminator=newline)
    w.writerow(["ドック", "機能", "回数", "使った人", "使った人の割合", "主にどこから", "表示した回数", "表示した時間（分）"])
    for line in summary.lines:
        share = f"{line.users / summary.people:.0%}" if summary.people else ""
        w.writerow([line.dock, line.feature, line.count, line.users, share, PLACES.get(line.main_place, line.main_place),
                    line.shown or "", f"{line.minutes:.0f}" if line.minutes else ""])
    return buffer.getvalue()


def period(days: int, now: Optional[str] = None) -> tuple[str, str]:
    """The last `days` days up to today (both included; 0: from the first day kept)."""
    last = now or today()
    if days <= 0:
        return "2000-01-01", last
    return (date.fromisoformat(last) - timedelta(days=days - 1)).isoformat(), last


def elapsed_minutes(since: float, now: Optional[float] = None) -> float:
    return max(0.0, ((now if now is not None else time.monotonic()) - since) / 60.0)


def names_of(places: Sequence[str]) -> list[str]:
    return [PLACES.get(p, p) for p in places]
