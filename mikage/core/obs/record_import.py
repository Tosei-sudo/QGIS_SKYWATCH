"""
観測記録の一括取込み (tsudo, 2026-11-08): rows of a CSV file or of a layer
(another section's table, old records, the plugin's own export) become
observation records of the chosen store.

1. `guess_mapping`: which column is which item (the plugin's own export
   headers, 標準（CSV） and 標準（JSON）, and the usual English names are known).
2. `plan`: a dry run: what each row would be (新規, 更新, 変更なし, 重複, 既存
   (not updated), エラー), with the reasons, against the records read.
3. `apply`: the planned new records and changes saved in one go; the store's
   checks hold (a record someone else changed since it was read is not
   overwritten), and what did not go through is told.

A row names its image by its id (the image does not need to be open). The
point is the layer's point (on its surface, for lines and polygons) or the
columns 経度・緯度. A thing named in a row is the thing of that name already
recorded (継続), else a new one (the same new name in several rows: one thing);
without a name, a new thing is named in turn (船舶-1, 船舶-2 …).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional, Sequence

from .flows import flows, living, next_name, now_ms
from .geo import get_distance
from .image_time import parse_time
from .model import Observation, new_object_id, new_observation_id
from .taxonomy import find, kind_of, path_of

#: The items a column can be: key → (label, names a column is guessed by (any case)).
TARGETS: list[tuple[str, str, tuple[str, ...]]] = [
    ("id", "観測ID（既存の記録の更新）", ("観測ID", "OBS_ID", "obs_id", "record_id", "observation_id")),
    ("image_id", "画像ID（必須）", ("画像ID", "IMAGE_ID", "image_id", "imageid", "image", "IMG_ID", "scene_id")),
    ("image_name", "画像名", ("画像", "画像名", "IMAGE_NAME", "image_name")),
    ("image_time", "撮像日時", ("撮像日時", "IMAGE_TIME", "image_time", "acquired", "acquisition_time", "datetime")),
    ("object", "モノ（名前）", ("モノ", "モノの名前", "OBJECT_NAME", "object_name", "OBJ_NAME", "name", "名称")),
    ("kind", "種別", ("種別", "KIND", "kind", "class", "分類", "category")),
    ("activity", "活動", ("活動", "ACTIVITY", "activity")),
    ("present", "有無（あり・なし）", ("有無", "PRESENT", "present", "在不在")),
    ("confidence", "確度", ("確度", "CONFIDENCE", "confidence")),
    ("note", "備考", ("備考", "NOTE", "note", "memo", "メモ", "comment")),
    ("length", "長さ（m）", ("長さ（m）", "長さ", "LENGTH_M", "length", "length_m")),
    ("heading", "向き（度）", ("向き（度）", "向き", "HEADING_DEG", "HEADING", "heading")),
    ("width", "幅（m）", ("幅（m）", "幅", "WIDTH_M", "width", "width_m")),
    ("lon", "経度（点がないとき）", ("経度", "LON", "lon", "lng", "longitude", "x")),
    ("lat", "緯度（点がないとき）", ("緯度", "LAT", "lat", "latitude", "y")),
]
LABELS = {k: label for k, label, _names in TARGETS}
#: Within this many metres, a new row of the same image, class and name (or no name) is the record already there.
SAME_PLACE_M = 5.0

NEW, UPDATE, SAME, DUPLICATE, EXISTING, ERROR = "new", "update", "same", "duplicate", "existing", "error"
ACTION_LABELS = {NEW: "新規", UPDATE: "更新", SAME: "変更なし", DUPLICATE: "重複（飛ばす）", EXISTING: "既存（更新しない）", ERROR: "エラー"}


def guess_mapping(columns: Sequence[str]) -> dict[str, str]:
    """The column of each item, by its name (the first match; a column is used once)."""
    by_lower: dict[str, str] = {}
    for c in columns:
        by_lower.setdefault(c.strip().lower(), c)
    out: dict[str, str] = {}
    used: set[str] = set()
    for key, _label, names in TARGETS:
        for n in names:
            c = by_lower.get(n.lower())
            if c is not None and c not in used:
                out[key] = c
                used.add(c)
                break
    return out


@dataclass
class SourceRow:
    """One row read: its values by column, and its point (WGS 84) when the layer has shapes."""
    number: int
    values: dict[str, Any]
    lon: Optional[float] = None
    lat: Optional[float] = None


@dataclass
class Planned:
    number: int
    action: str
    #: Why (an error, or what was found).
    reason: str = ""
    #: Warnings that do not stop the row (a class not in the classification …).
    warnings: list[str] = field(default_factory=list)
    #: The record as it would be saved (new or changed), or the one there (same, duplicate, existing).
    record: Optional[Observation] = None
    #: An update: item → (was, will be).
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return ACTION_LABELS[self.action]


@dataclass
class ImportOutcome:
    saved: list[Observation] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    failed: int = 0
    conflicts: int = 0
    error: str = ""

    def text(self) -> str:
        parts = [f"新規 {self.added} 件・更新 {self.updated} 件を保存"]
        if self.conflicts:
            parts.append(f"ほかの人が先に変えた {self.conflicts} 件は上書きせず")
        if self.failed:
            parts.append(f"保存できなかった {self.failed} 件" + (f"（{self.error}）" if self.error else ""))
        return "、".join(parts)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value: Any) -> tuple[Optional[float], bool]:
    """The number of a cell (None when empty) and whether it was readable."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, True
    if isinstance(value, bool):
        return None, False
    try:
        n = float(str(value).strip().replace(",", "")) if not isinstance(value, (int, float)) else float(value)
    except ValueError:
        return None, False
    return (n, True) if math.isfinite(n) else (None, False)


_YES = {"あり", "在", "1", "true", "yes", "y", "○", "有", "present"}
_NO = {"なし", "不在", "0", "false", "no", "n", "×", "無", "absent"}


def _present(value: Any) -> Optional[bool]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    if isinstance(value, bool):
        return value
    t = _text(value).lower()
    return True if t in _YES else False if t in _NO else None


def _time(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = float(value)
        return int(n if n > 1e11 else n * 1000) if math.isfinite(n) else None
    if hasattr(value, "toMSecsSinceEpoch"):
        return int(value.toMSecsSinceEpoch()) if (not hasattr(value, "isValid") or value.isValid()) else None
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    return parse_time(str(value))


def _get(row: SourceRow, mapping: dict[str, str], key: str) -> Any:
    column = mapping.get(key)
    return row.values.get(column) if column else None


def plan(rows: Iterable[SourceRow], mapping: dict[str, str], records: Sequence[Observation], confidences: Sequence[str] = (),
         taxonomy: Sequence[Any] = (), update_existing: bool = False, image_times: Optional[dict[str, int]] = None,
         image_names: Optional[dict[str, str]] = None) -> list[Planned]:
    """
    What each row would be (nothing is saved). `records` are the records
    read (deleted ones too); `confidences` the 確度 levels (an unknown one is
    an error); `taxonomy` the classification (a class not in it is a warning);
    `update_existing`: a row whose 観測ID is a record changes it, else it is left.
    """
    now = now_ms()
    by_id = {o.id: o for o in records}
    alive = living(records)
    image_times = dict(image_times or {})
    image_names = dict(image_names or {})
    levels = [c for c in confidences if c]
    names = {f.name: f for f in flows(records)}
    made: dict[str, tuple[str, str]] = {}
    taken = set(names)
    out: list[Planned] = []
    planned_new: list[Observation] = []
    for row in rows:
        problems: list[str] = []
        warnings: list[str] = []
        image_id = _text(_get(row, mapping, "image_id"))
        lon, lat = row.lon, row.lat
        if mapping.get("lon") or mapping.get("lat"):
            x, okx = _number(_get(row, mapping, "lon"))
            y, oky = _number(_get(row, mapping, "lat"))
            if not (okx and oky):
                problems.append("経度・緯度が数でない")
            elif x is not None and y is not None:
                lon, lat = x, y
        numbers: dict[str, Optional[float]] = {}
        for key in ("length", "heading", "width"):
            n, ok = _number(_get(row, mapping, key))
            if not ok:
                problems.append(f"{LABELS[key]}が数でない")
            numbers[key] = n
        if numbers["length"] is not None and numbers["length"] <= 0:
            numbers["length"] = None
        if numbers["width"] is not None and numbers["width"] <= 0:
            numbers["width"] = None
        if numbers["heading"] is not None:
            numbers["heading"] = (numbers["heading"] % 360 + 360) % 360
        present = _present(_get(row, mapping, "present"))
        if present is None:
            problems.append(f"有無が読めない（{_text(_get(row, mapping, 'present'))}）")
            present = True
        confidence = _text(_get(row, mapping, "confidence"))
        if confidence and levels and confidence not in levels:
            problems.append(f"確度「{confidence}」なし（{'・'.join(levels)}）")
        kind = kind_of(path_of(_text(_get(row, mapping, "kind"))))
        if kind and taxonomy and find(list(taxonomy), path_of(kind)) is None:
            warnings.append(f"分類体系にない種別「{kind}」")
        activity = _text(_get(row, mapping, "activity")) if present else ""
        note = _text(_get(row, mapping, "note"))
        name = _text(_get(row, mapping, "object"))
        record_id = _text(_get(row, mapping, "id"))

        # A record there: changed (or left as it is).
        was = by_id.get(record_id) if record_id else None
        if was is not None:
            if problems:
                out.append(Planned(row.number, ERROR, "、".join(problems), warnings, was))
                continue
            if was.deleted_at is not None:
                out.append(Planned(row.number, ERROR, "削除済みの記録", warnings, was))
                continue
            if not update_existing:
                out.append(Planned(row.number, EXISTING, f"観測ID {record_id} の記録がある", warnings, was))
                continue
            wanted: dict[str, Any] = {}
            if mapping.get("kind") and kind != was.kind:
                wanted["kind"] = kind
            if mapping.get("activity") and activity != was.activity:
                wanted["activity"] = activity
            if mapping.get("present") and present != was.present:
                wanted["present"] = present
                if not present:
                    wanted["activity"] = ""
            if mapping.get("confidence") and confidence != was.confidence:
                wanted["confidence"] = confidence
            if mapping.get("note") and note != was.note:
                wanted["note"] = note
            for key in ("length", "heading", "width"):
                if mapping.get(key) and not _same_number(numbers[key], getattr(was, key)):
                    wanted[key] = numbers[key]
            if lon is not None and lat is not None and get_distance([was.lon, was.lat], [lon, lat]) > 0.01:
                wanted["lon"], wanted["lat"] = lon, lat
            if mapping.get("object") and name and name != was.object_name:
                wanted["object_name"] = name
            if not wanted:
                out.append(Planned(row.number, SAME, "", warnings, was))
                continue
            changes = {k: (getattr(was, k), v) for k, v in wanted.items()}
            out.append(Planned(row.number, UPDATE, "", warnings, replace(was, **wanted, updated_at=now), changes))
            continue
        if record_id and was is None and mapping.get("id"):
            warnings.append(f"観測ID {record_id} の記録はない（新規として取り込む）")

        # A new record.
        if not image_id:
            problems.append("画像IDが空")
        if lon is None or lat is None:
            problems.append("位置がない（点のレイヤーか経度・緯度の列）")
        elif not (-180 <= lon <= 180 and -90 <= lat <= 90):
            problems.append("位置が経緯度の範囲外")
        if problems:
            out.append(Planned(row.number, ERROR, "、".join(problems), warnings))
            continue
        image_time = _time(_get(row, mapping, "image_time")) if mapping.get("image_time") else None
        if image_time is None:
            image_time = image_times.get(image_id)
        if image_time is not None:
            image_times.setdefault(image_id, image_time)
        image_name = _text(_get(row, mapping, "image_name")) or image_names.get(image_id) or image_id
        image_names.setdefault(image_id, image_name)

        # The same record already there (or earlier in this file): left out.
        def same(o: Observation) -> bool:
            return (o.image_id == image_id and o.kind == kind and (not name or o.object_name == name)
                    and get_distance([o.lon, o.lat], [lon, lat]) <= SAME_PLACE_M)

        twin = next((o for o in alive if same(o)), None) or next((o for o in planned_new if same(o)), None)
        if twin is not None:
            out.append(Planned(row.number, DUPLICATE, f"同じ画像・種別・場所に記録あり（{twin.object_name}）", warnings, twin))
            continue
        if name and name in names:
            object_id, object_name = names[name].object_id, names[name].name
        elif name and name in made:
            object_id, object_name = made[name]
        else:
            object_id = new_object_id()
            object_name = name or next_name(kind, taken)
            made[object_name] = (object_id, object_name)
        taken.add(object_name)
        if any(o.image_id == image_id and o.object_id == object_id for o in alive) or any(
                o.image_id == image_id and o.object_id == object_id for o in planned_new):
            out.append(Planned(row.number, DUPLICATE, f"{object_name} はこの画像に記録済み", warnings))
            continue
        record = Observation(
            id=new_observation_id(), image_id=image_id, image_name=image_name, image_time=image_time, object_id=object_id,
            object_name=object_name, kind=kind, activity=activity, present=present, note=note, lon=lon, lat=lat, created_at=now,
            updated_at=now, deleted_at=None, length=numbers["length"], heading=numbers["heading"], width=numbers["width"], confidence=confidence,
        )
        planned_new.append(record)
        out.append(Planned(row.number, NEW, "", warnings, record))
    return out


def _same_number(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-6


def counts(planned: Sequence[Planned]) -> dict[str, int]:
    out = {k: 0 for k in ACTION_LABELS}
    for p in planned:
        out[p.action] += 1
    return out


def summary(planned: Sequence[Planned]) -> str:
    c = counts(planned)
    text = "、".join(f"{ACTION_LABELS[k]} {n} 件" for k, n in c.items() if n)
    warned = sum(1 for p in planned if p.warnings)
    return (text or "0 件") + (f"（注意 {warned} 件）" if warned else "")


def apply(book: Any, planned: Sequence[Planned]) -> ImportOutcome:
    """Saves the new records and the changes of a plan (in one save of the book's store)."""
    rows = [p.record for p in planned if p.action in (NEW, UPDATE) and p.record is not None]
    outcome = ImportOutcome()
    if not rows:
        return outcome
    known = {o.id for o in book.records}
    by = book.observer()
    rows = [replace(o, updated_by=by) if o.id in known else replace(o, recorded_by=by, updated_by=by) for o in rows]
    try:
        book.store.save(rows)
        saved = rows
    except Exception as error:  # noqa: BLE001 (told; the part that went through is kept)
        done = getattr(book.store, "last_saved", set())
        saved = [o for o in rows if o.id in done]
        outcome.conflicts = len(getattr(book.store, "last_conflicts", set()) & {o.id for o in rows})
        outcome.failed = max(0, len(rows) - len(saved) - outcome.conflicts)
        outcome.error = str(error) or type(error).__name__
    with book.lock:
        by_id = {o.id: o for o in saved}
        book.records = [by_id.pop(o.id, o) for o in book.records] + list(by_id.values())
    outcome.saved = saved
    outcome.added = sum(1 for o in saved if o.id not in known)
    outcome.updated = len(saved) - outcome.added
    book._say(outcome.text())
    return outcome


def read_csv(path: str) -> tuple[list[str], list[SourceRow]]:
    """The columns and rows of a CSV file (UTF-8 with or without BOM, else Shift_JIS; comma, tab or semicolon)."""
    import csv
    import io

    with open(path, "rb") as f:
        data = f.read()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("文字コードが読めない（UTF-8 か Shift_JIS で保存）")
    head = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(head, delimiters=",\t;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if head.count("\t") > head.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader]
    if not rows:
        return [], []
    columns = []
    for i, c in enumerate(rows[0]):
        name = c.strip() or f"列{i + 1}"
        while name in columns:
            name += "_"
        columns.append(name)
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        out.append(SourceRow(n, {c: (r[i] if i < len(r) else "") for i, c in enumerate(columns)}))
    return columns, out
