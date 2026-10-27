"""
The observations of one store as the viewer's panel works them
(observation-panel.ts), without the screen: the records, the checked areas
and the classification read from the stores, the images stepped through in
time order, and every change — a new record, carried over, gone, changed,
deleted, brought back, renamed, merged; an area checked; a class added,
renamed, moved or removed (the records under it follow) — written to the
store and then to the lists here.

The dock draws and asks; the Python API (`plugin.api.observations`) and the
tests call the same methods. Store calls may block (an Esri service): the
dock runs them in a QgsTask.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional, Sequence

from .areas import CheckedArea, area_label, box_ring, covering_areas, new_area_id
from .flows import (
    CarryOver, ImageRef, MatchCandidate, ObjectFlow, carried_into, carry_overs, flow_of, flows, living, match_candidates,
    next_name, now_ms,
)
from .model import (
    ConfidenceLevel, ListedImage, Observation, ObservationVocabulary, new_object_id, new_observation_id, parse_listed_images,
    recorded_images,
)
from .report import ReportContext, ReportOptions, ReportSelection, report_html, select_for_report
from .stores import AreaStore, ObservationStore, TaxonomyStore
from .taxonomy import (
    SpecField, Taxon, find, kind_of, move_taxon, normalize, path_of, place_taxon, remove_taxon, rewrite, taxonomy_of,
    update_taxon, with_kinds, within,
)

_UNSET = object()


@dataclass
class ObservedImage:
    """What the book knows of an image."""
    id: str
    name: str
    time: Optional[int] = None
    #: Its footprint [west, south, east, north] (WGS 84), when known.
    box: Optional[list[float]] = None
    #: The project's raster layer (its id), None for an image listed without its file.
    layer_id: Optional[str] = None
    #: Where its time came from (image_time.ORIGINS), '' when unknown.
    origin: str = ""

    @property
    def ref(self) -> ImageRef:
        return ImageRef(self.id, self.name, self.time)

    @property
    def file(self) -> bool:
        return self.layer_id is not None


class Prefs:
    """Small values kept per user (QgsSettings in QGIS): the images listed without their file, per store."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class StoreError(Exception):
    """A store refused (the reason is in the message, as the panel says it)."""


def _error_text(error: Exception) -> str:
    return str(error) or type(error).__name__


@dataclass
class FormValues:
    """What the record form says."""
    object: str = ""
    kind: str = ""
    activity: str = ""
    note: str = ""
    confidence: str = ""
    length: str = ""
    heading: str = ""


class ObservationBook:
    """The records of one store, and what is done with them."""

    def __init__(self, store: ObservationStore, area_store: AreaStore, taxonomy_store: TaxonomyStore,
                 vocabulary: ObservationVocabulary, observer: Callable[[], str] = lambda: "",
                 prefs: Optional[Prefs] = None, say: Callable[[str], None] = lambda message: None):
        self.store = store
        self.area_store = area_store
        self.taxonomy_store = taxonomy_store
        self.vocabulary = vocabulary
        self.observer = observer
        self.prefs = prefs or Prefs()
        self.say = say
        self.lock = threading.RLock()
        #: Every record and area (deleted ones too).
        self.records: list[Observation] = []
        self.areas: list[CheckedArea] = []
        self._taxonomy: list[Taxon] = list(vocabulary.taxonomy)
        self.spec_fields: list[SpecField] = list(vocabulary.spec_fields)
        #: The images in time order: the open ones and those listed without their file.
        self.images: list[ObservedImage] = []
        self.current: Optional[ObservedImage] = None
        self._open: list[ObservedImage] = []
        #: Times typed for images that do not say (by image id).
        self.manual_times: dict[str, int] = {}
        self.listed: list[ListedImage] = parse_listed_images(self.prefs.get(self._listed_key))
        #: What the last record said (offered first next time).
        self.last = FormValues()
        self.loaded = False
        self.status = ""

    # ---- what is said --------------------------------------------------

    def _say(self, message: str) -> str:
        self.status = message
        self.say(message)
        return message

    @property
    def label(self) -> str:
        return self.store.label

    def confidences(self) -> list[ConfidenceLevel]:
        """The levels of 確度 offered, the surest first."""
        return self.vocabulary.confidences

    # ---- reading -------------------------------------------------------

    def reload(self) -> str:
        """Reads the records, areas and classification again from the stores."""
        self._say(f"{self.store.label} から読み込み中…")
        try:
            kept = self.taxonomy_store.load()
        except Exception:  # noqa: BLE001 (as the viewer: the configured classification then)
            kept = None
        try:
            fields = self.taxonomy_store.load_fields()
        except Exception:  # noqa: BLE001
            fields = None
        message = ""
        try:
            areas = self.area_store.load()
        except Exception as error:  # noqa: BLE001
            areas = []
            message = f"{self.area_store.label} から確認範囲の読み込み失敗: {_error_text(error)}"
        try:
            records = self.store.load()
            text = f"{self.store.label}: {len(living(records))} 件の記録、{len(flows(records))} のモノ"
        except Exception as error:  # noqa: BLE001
            records = []
            text = f"{self.store.label} から読み込み失敗: {_error_text(error)}"
        with self.lock:
            self.records = records
            self.areas = areas
            self._taxonomy = kept if kept is not None else list(self.vocabulary.taxonomy)
            self.spec_fields = fields if fields is not None else list(self.vocabulary.spec_fields)
            self.loaded = True
        return self._say(message or text)

    def flows(self) -> list[ObjectFlow]:
        """Every thing's flow."""
        return flows(self.records)

    def living(self) -> list[Observation]:
        return living(self.records)

    def flow(self, object_id: str) -> Optional[ObjectFlow]:
        rows = [o for o in living(self.records) if o.object_id == object_id]
        return flow_of(rows) if rows else None

    def record(self, record_id: str) -> Optional[Observation]:
        return next((o for o in self.records if o.id == record_id), None)

    def here(self, deleted: bool = False) -> list[Observation]:
        """The records of the image recorded now (the deleted ones too when asked), by name."""
        image = self.current
        if image is None:
            return []
        rows = [o for o in self.records if o.image_id == image.id and (deleted or o.deleted_at is None)]
        return sorted(rows, key=lambda o: o.object_name)

    # ---- the images ----------------------------------------------------

    @property
    def _listed_key(self) -> str:
        return f"listed_images/{self.store.label}"

    def set_open_images(self, images: Sequence[ObservedImage], selected: Optional[str] = None) -> None:
        """The project's images again (call when they change): in order of acquisition time with those listed without their file."""
        with self.lock:
            self._open = list(images)
            self._order(selected)

    def _with_manual(self, info: ObservedImage) -> ObservedImage:
        manual = self.manual_times.get(info.id)
        return replace(info, time=manual, origin="manual") if info.time is None and manual is not None else info

    def _order(self, selected: Optional[str] = None) -> None:
        entries = [self._with_manual(i) for i in self._open]
        ids = {e.id for e in entries}
        for listed in self.listed:
            if listed.id not in ids:
                entries.append(self._with_manual(ObservedImage(listed.id, listed.name, listed.time, listed.box, None, "manual" if listed.time is not None else "")))
        entries.sort(key=lambda e: (e.time if e.time is not None else float("inf"), e.name))
        was = self.current
        keep = None
        if was is not None:
            keep = next((e for e in entries if (e.layer_id == was.layer_id if was.layer_id else (not e.layer_id and e.id == was.id))), None)
        if keep is None and selected:
            keep = next((e for e in entries if e.layer_id == selected), None)
        self.images = entries
        self.current = keep or (entries[0] if entries else None)

    def go_to(self, image_id: str) -> bool:
        """Records on the image of this id (an open one or one listed without its file) from now on."""
        entry = next((e for e in self.images if e.id == image_id), None)
        if entry is not None:
            self.current = entry
        return entry is not None

    def go_to_layer(self, layer_id: str) -> bool:
        entry = next((e for e in self.images if e.layer_id == layer_id), None)
        if entry is not None:
            self.current = entry
        return entry is not None

    def step(self, by: int) -> Optional[ObservedImage]:
        """The next (or previous) image in time."""
        if not self.images:
            return None
        at = self.images.index(self.current) if self.current in self.images else -1
        nxt = self.images[min(len(self.images) - 1, max(0, at + by))]
        self.current = nxt
        return nxt

    def list_image(self, image_id: str, name: str = "", time: Optional[int] = None, box: Optional[list[float]] = None,
                   replacing: Optional[str] = None) -> Optional[ObservedImage]:
        """Lists an image without its file (outside the catalog, or not opened here) and records on it."""
        image_id = image_id.strip()
        if not image_id:
            self._say("画像 ID を入力")
            return None
        info = ListedImage(image_id, name.strip() or image_id, time, box)
        with self.lock:
            self.listed = [i for i in self.listed if i.id not in (image_id, replacing)] + [info]
            self._save_listed()
            self._order()
            self.go_to(image_id)
        entry = self.current
        self._say(f"{info.name} は開いている画像と同じ ID（その画像で記録）" if entry and entry.file else f"{info.name} を追加（ファイルなし）")
        return entry

    def unlist_image(self, image_id: str) -> bool:
        """Takes an image without its file off the list (its records stay)."""
        gone = next((i for i in self.listed if i.id == image_id), None)
        if gone is None:
            return False
        with self.lock:
            self.listed = [i for i in self.listed if i is not gone]
            self._save_listed()
            if self.current is not None and not self.current.file and self.current.id == image_id:
                self.current = None
            self._order()
        self._say(f"{gone.name} を一覧から除外（記録は保持）")
        return True

    def _save_listed(self) -> None:
        import json

        self.prefs.set(self._listed_key, json.dumps([i.to_json() for i in self.listed], ensure_ascii=False))

    def recorded_images(self) -> list[ListedImage]:
        """The images the records name that are not in the list, newest first."""
        ids = {e.id for e in self.images}
        return [i for i in recorded_images(self.records) if i.id not in ids]

    def set_image_time(self, t: int) -> list[Observation]:
        """The time of the image recorded now, typed (for images that do not say); the records made without a time get it."""
        entry = self.current
        if entry is None:
            return []
        with self.lock:
            self.manual_times[entry.id] = t
            if not entry.file:
                self.listed = [replace(i, time=t) if i.id == entry.id else i for i in self.listed]
                self._save_listed()
            self._order()
        now = now_ms()
        timeless = [replace(o, image_time=t, updated_at=now) for o in self.records if o.image_id == entry.id and o.image_time is None]
        if timeless:
            self._save_records(timeless, f"{len(timeless)} 件の記録に撮像日時を入力")
        return timeless

    def inside(self) -> Optional[Callable[[float, float], bool]]:
        """The current image's footprint test."""
        box = self.current.box if self.current else None
        if not box:
            return None
        w, s, e, n = box
        return lambda lon, lat: w <= lon <= e and s <= lat <= n

    # ---- what may still be there ---------------------------------------

    def carried(self) -> list[CarryOver]:
        """What may still be there in this image (the things of the images before and after)."""
        if self.current is None:
            return []
        return carry_overs(self.records, self.current.ref, self.inside())

    def inside_carried(self, carried: Optional[list[CarryOver]] = None) -> list[CarryOver]:
        """The carried-over things that lie in one of this image's checked areas (so were not there)."""
        image = self.current
        if image is None:
            return []
        carried = self.carried() if carried is None else carried
        return [c for c in carried if covering_areas(self.areas, image.id, c.source.lon, c.source.lat, c.source.kind)]

    def candidates(self, lon: float, lat: float, kind: str = "", length: Optional[float] = None, heading: Optional[float] = None,
                   image: Optional[str] = None) -> list[MatchCandidate]:
        """The things of other images that may be the one at a place in this image (同一候補), likeliest first."""
        info = self._image(image)
        if info is None:
            return []
        return match_candidates(self.records, info.ref, lon, lat, kind, length, heading, radius=self.vocabulary.match_radius)

    def _image(self, image: Optional[str]) -> Optional[ObservedImage]:
        if image:
            return next((e for e in self.images if e.id == image), None)
        return self.current

    # ---- writing records -----------------------------------------------

    def _save_records(self, rows: list[Observation], done: str) -> Optional[list[Observation]]:
        """Saves records in the store and in the list; the records as saved, None when the store refused."""
        if not rows:
            return []
        by = self.observer()
        known = {o.id for o in self.records}
        # Who made (a new record) and who changed it: the one recording now.
        rows = [replace(o, updated_by=by) if o.id in known else replace(o, recorded_by=by, updated_by=by) for o in rows]
        try:
            self.store.save(rows)
        except Exception as error:  # noqa: BLE001 (said, as the viewer)
            self._say(f"{self.store.label} への保存失敗: {_error_text(error)}")
            return None
        with self.lock:
            by_id = {o.id: o for o in rows}
            self.records = [by_id.pop(o.id, o) for o in self.records] + list(by_id.values())
        self._say(done)
        return rows

    def flow_by_name(self, name: str) -> Optional[ObjectFlow]:
        if not name:
            return None
        everything = flows(self.records)
        return next((f for f in everything if f.name == name), None) or next((f for f in everything if f.object_id == name), None)

    def record_from(self, image: ObservedImage, lon: float, lat: float, editing: Optional[Observation], source: Optional[Observation],
                    values: FormValues, present: bool) -> Observation:
        """The record the form says."""
        now = now_ms()

        def number(v: str) -> Optional[float]:
            try:
                n = float(v) if v and v.strip() else None
            except ValueError:
                return None
            return n if n is not None and n == n and abs(n) != float("inf") else None

        length = number(values.length)
        heading = number(values.heading)
        measure = {"length": length if length is not None and length > 0 else None, "heading": None if heading is None else (heading % 360 + 360) % 360}
        name = values.object.strip()
        kind = kind_of(path_of(values.kind))
        activity = values.activity.strip() if present else ""
        confidence = (values.confidence or "").strip()
        if editing is not None:
            # The same thing unless another one's name was typed.
            other = self.flow_by_name(name) if name and name != editing.object_name else None
            return replace(editing, object_id=other.object_id if other else editing.object_id,
                           object_name=other.name if other else (name or editing.object_name), kind=kind, activity=activity,
                           present=present, confidence=confidence, note=values.note, lon=lon, lat=lat, updated_at=now, **measure)
        if source is not None and (not name or name == source.object_name):
            known = (source.object_id, source.object_name)
        else:
            flow = self.flow_by_name(name)
            known = (flow.object_id, flow.name) if flow else None
        return Observation(
            id=new_observation_id(), image_id=image.id, image_name=image.name, image_time=image.time,
            object_id=known[0] if known else new_object_id(),
            object_name=known[1] if known else (name or next_name(kind, {f.name for f in flows(self.records)})),
            kind=kind, activity=activity, present=present, confidence=confidence, note=values.note, lon=lon, lat=lat,
            created_at=now, updated_at=now, deleted_at=None, **measure,
        )

    def submit(self, lon: float, lat: float, values: FormValues, present: bool = True, editing: Optional[Observation] = None,
               source: Optional[Observation] = None) -> Optional[Observation]:
        """Records what the form says on the image recorded now (a new record, a change, or a thing carried over)."""
        image = self.current
        if image is None:
            self._say("画像なし（画像を開く、または ＋ でファイルなしの画像を追加）")
            return None
        record = self.record_from(image, lon, lat, editing, source, values, present)
        self.last = FormValues(kind=record.kind, activity=record.activity if present else self.last.activity, confidence=record.confidence)
        what = (f"（{record.activity}）" if record.activity else "") if present else "（不在）"
        saved = self._save_records([record], f"{record.object_name}{what}を{'更新' if editing else '記録'}")
        return saved[0] if saved else None

    def add(self, lon: float, lat: float, image: Optional[str] = None, object: str = "", kind: Optional[str] = None,
            activity: Optional[str] = None, confidence: Optional[str] = None, note: str = "", present: bool = True,
            length: Optional[float] = None, heading: Optional[float] = None) -> Optional[Observation]:
        """Records a new observation (as the form does) on this image or the one of `image` (its id)."""
        info = self._image(image)
        if info is None:
            return None
        values = FormValues(object=object, kind=self.last.kind if kind is None else kind,
                            activity=self.last.activity if activity is None else activity,
                            confidence=self.last.confidence if confidence is None else confidence, note=note,
                            length="" if length is None else str(length), heading="" if heading is None else str(heading))
        record = self.record_from(info, lon, lat, None, None, values, present)
        saved = self._save_records([record], f"{record.object_name} を記録")
        return saved[0] if saved else None

    def update(self, record_id: str, **changes: Any) -> Optional[Observation]:
        """Changes a record (its id): kind, activity, present, confidence, note, lon, lat, length, heading."""
        was = next((o for o in self.records if o.id == record_id and o.deleted_at is None), None)
        if was is None:
            return None
        allowed = {"kind", "activity", "present", "confidence", "note", "lon", "lat", "length", "heading", "object_name"}
        bad = set(changes) - allowed
        if bad:
            raise ValueError(f"変更不可の項目: {', '.join(sorted(bad))}")
        if "kind" in changes:
            changes["kind"] = kind_of(path_of(changes["kind"]))
        if changes.get("present") is False:
            changes["activity"] = ""
        record = replace(was, **changes, updated_at=now_ms())
        saved = self._save_records([record], f"{record.object_name} を更新")
        return saved[0] if saved else None

    def move(self, record_id: str, lon: float, lat: float) -> Optional[Observation]:
        """A record's mark dragged: its place corrected."""
        was = next((o for o in self.records if o.id == record_id and o.deleted_at is None), None)
        if was is None:
            return None
        record = replace(was, lon=lon, lat=lat, updated_at=now_ms())
        saved = self._save_records([record], f"{record.object_name} の位置を修正")
        return saved[0] if saved else None

    def remove(self, rows: Iterable[Observation]) -> bool:
        """Deletes records (kept, marked, where the store does)."""
        rows = list(rows)
        if not rows:
            return True
        try:
            marked = self.store.remove(rows)
        except Exception as error:  # noqa: BLE001
            self._say(f"削除失敗: {_error_text(error)}")
            return False
        with self.lock:
            ids = {o.id for o in rows}
            kept = {o.id: o for o in marked}
            self.records = [kept.get(o.id, o) for o in self.records if o.id not in ids or o.id in kept]
        self._say(f"{len(rows)} 件の記録を削除")
        return True

    def restore(self, rows: Iterable[Observation]) -> bool:
        """Brings deleted records back."""
        now = now_ms()
        back = [replace(o, deleted_at=None, updated_at=now) for o in rows]
        return self._save_records(back, f"{len(back)} 件の記録を復元") is not None

    def rename(self, object_id: str, name: str, kind: Optional[str] = None) -> bool:
        """Renames a thing in all its records (and changes its kind, when given)."""
        now = now_ms()
        rows = [replace(o, object_name=name, **({"kind": kind} if kind is not None else {}), updated_at=now)
                for o in self.records if o.object_id == object_id]
        return self._save_records(rows, f"モノの名前を「{name}」に変更") is not None

    def merge(self, source: str, into: str) -> bool:
        """Makes `source`'s records `into`'s (the same thing recorded as two)."""
        if source == into:
            return True
        target_rows = [o for o in living(self.records) if o.object_id == into]
        if not target_rows:
            return False
        target = flow_of(target_rows)
        now = now_ms()
        rows = [replace(o, object_id=into, object_name=target.name, kind=target.kind or o.kind, updated_at=now)
                for o in self.records if o.object_id == source]
        return self._save_records(rows, f"{len(rows)} 件の記録を「{target.name}」に統合") is not None

    def carry(self, object_id: str, present: bool = True, **changes: Any) -> Optional[Observation]:
        """Records a carried-over thing in this image: still there (with changes), or gone."""
        image = self.current
        c = next((x for x in self.carried() if x.source.object_id == object_id), None)
        if image is None or c is None:
            return None
        record = carried_into(c.source, image.ref, now_ms(), **{"present": present, **({} if present else {"activity": ""}), **changes})
        saved = self._save_records([record], f"{record.object_name} を{'継続' if present else '不在'}として記録")
        return saved[0] if saved else None

    def carry_all(self) -> list[Observation]:
        """Records every carried-over thing as still there in this image (but those in a checked area, which were not)."""
        image = self.current
        carried = self.carried()
        inside = self.inside_carried(carried)
        rows = [c for c in carried if c not in inside]
        if image is None or not rows:
            return []
        now = now_ms()
        made = [carried_into(c.source, image.ref, now) for c in rows]
        note = f"（確認範囲内の {len(inside)} 件を除く）" if inside else ""
        return self._save_records(made, f"{len(made)} のモノを継続として記録{note}") or []

    def absent_inside(self) -> list[Observation]:
        """Records the things carried over into a checked area as gone in this image."""
        image = self.current
        rows = self.inside_carried()
        if image is None or not rows:
            return []
        now = now_ms()
        made = [carried_into(c.source, image.ref, now, present=False, activity="") for c in rows]
        return self._save_records(made, f"{len(made)} のモノを不在として記録（確認範囲内）") or []

    # ---- checked areas -------------------------------------------------

    def add_area(self, ring: Sequence[Sequence[float]], kind: str = "", note: str = "", image: Optional[str] = None) -> Optional[CheckedArea]:
        """Records a checked area on an image (this one unless given): its outline (longitude / latitude), the kind looked for ('' all)."""
        info = self._image(image)
        if info is None:
            return None
        points = [[float(p[0]), float(p[1])] for p in ring]
        if len(points) < 3:
            return None
        if points[0] != points[-1]:
            points.append(list(points[0]))
        now = now_ms()
        area = CheckedArea(id=new_area_id(), image_id=info.id, image_name=info.name, image_time=info.time, ring=points,
                           kind=kind_of(path_of(kind)), note=note.strip(), created_at=now, updated_at=now, deleted_at=None,
                           recorded_by=self.observer())
        try:
            self.area_store.save([area])
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.area_store.label} への確認範囲の保存失敗: {_error_text(error)}")
            return None
        with self.lock:
            self.areas = [*self.areas, area]
        missing = len(self.inside_carried()) if self.current is not None and info.id == self.current.id else 0
        self._say(f"{area_label(area)}を記録{f'（範囲内で記録のない前後の画像のモノ {missing} 件）' if missing else ''}")
        return area

    def add_box_area(self, box: Sequence[float], kind: str = "", note: str = "") -> Optional[CheckedArea]:
        return self.add_area(box_ring(box), kind, note)

    def remove_areas(self, rows: Iterable[CheckedArea]) -> bool:
        """Deletes checked areas (kept, marked, where the store does)."""
        rows = list(rows)
        if not rows:
            return True
        try:
            marked = self.area_store.remove(rows)
        except Exception as error:  # noqa: BLE001
            self._say(f"確認範囲の削除失敗: {_error_text(error)}")
            return False
        with self.lock:
            ids = {a.id for a in rows}
            kept = {a.id: a for a in marked}
            self.areas = [kept.get(a.id, a) for a in self.areas if a.id not in ids or a.id in kept]
        self._say(f"確認範囲 {len(rows)} 件を削除")
        return True

    def restore_areas(self, rows: Iterable[CheckedArea]) -> bool:
        now = now_ms()
        back = [replace(a, deleted_at=None, updated_at=now) for a in rows]
        try:
            self.area_store.save(back)
        except Exception as error:  # noqa: BLE001
            self._say(f"確認範囲の復元失敗: {_error_text(error)}")
            return False
        with self.lock:
            by_id = {a.id: a for a in back}
            self.areas = [by_id.get(a.id, a) for a in self.areas]
        self._say(f"確認範囲 {len(back)} 件を復元")
        return True

    def image_areas(self, deleted: bool = False) -> list[CheckedArea]:
        image = self.current
        if image is None:
            return []
        return [a for a in self.areas if a.image_id == image.id and (deleted or a.deleted_at is None)]

    # ---- the classification --------------------------------------------

    def taxonomy(self) -> list[Taxon]:
        """The classification shown: the kept one with the kinds records use added."""
        return with_kinds(self._taxonomy, [o.kind for o in self.records])

    def kept_taxonomy(self) -> list[Taxon]:
        return list(self._taxonomy)

    def _save_taxonomy(self, taxa: list[Taxon], records: list[Observation], done: str) -> bool:
        try:
            self.taxonomy_store.save(taxa)
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.taxonomy_store.label} への分類の保存失敗: {_error_text(error)}")
            return False
        with self.lock:
            self._taxonomy = normalize(taxa)
        if records:
            return self._save_records(records, done) is not None
        self._say(done)
        return True

    def set_taxonomy(self, value: list) -> bool:
        """Replaces the classification (a flat list of Taxon or `{path}` objects, or nested as the settings file writes it)."""
        problems: list[str] = []
        if value and isinstance(value[0], Taxon):
            taxa: Optional[list[Taxon]] = with_kinds(value, [])
        elif value and isinstance(value[0], dict) and isinstance(value[0].get("path"), list):
            from .taxonomy import taxa_from_json

            taxa = taxa_from_json(value)
        else:
            taxa = taxonomy_of(value, problems)
        if not taxa:
            self._say(problems[0] if problems else "分類の形式不正")
            return False
        return self._save_taxonomy(taxa, [], f"分類 {len(taxa)} 件を設定")

    def add_class(self, path: Sequence[str]) -> bool:
        """Adds a class (its parents too)."""
        path = list(path)
        if not path:
            return False
        return self._save_taxonomy(with_kinds(self.taxonomy(), [kind_of(path)]), [], f"分類「{kind_of(path)}」を追加")

    def update_class(self, path: Sequence[str], name: Optional[str] = None, parent: Optional[Sequence[str]] = None,
                     before: Any = _UNSET, **own: Any) -> Optional[list[str]]:
        """
        Changes a class: its name or parent (the records under it follow), its
        place among its siblings (`before`: a sibling's path, None last), the
        names of its branch's levels, what an individual's name is called, its
        known individuals, specifications and photos. Its new path, or None.
        """
        path = list(path)
        taxa = self.taxonomy()
        if find(taxa, path) is None:
            self._say(f"分類「{kind_of(path)}」なし")
            return None
        import re

        new_name = re.sub(r"\s*[>›＞]\s*", " ", (name or "").strip()) or path[-1]
        to = [*(list(parent) if parent is not None else path[:-1]), new_name]
        bad = set(own) - {"levels", "name_label", "members", "specs", "photos"}
        if bad:
            raise ValueError(f"変更不可の項目: {', '.join(sorted(bad))}")
        taxa = update_taxon(taxa, path, **own)
        moved = kind_of(to) != kind_of(path)
        if moved:
            if find(taxa, to) is not None:
                self._say(f"分類「{kind_of(to)}」は既存（統合は不可、別の名前で）")
                return None
            try:
                taxa = move_taxon(taxa, path, to)
            except ValueError as error:
                self._say(str(error))
                return None
        if before is not _UNSET:
            taxa = place_taxon(taxa, to, [*to[:-1], list(before)[-1]] if before else None)
        reordered = before is not _UNSET and not moved
        now = now_ms()
        records = [replace(o, kind=rewrite(o.kind, path, to), updated_at=now) for o in self.records if within(o.kind, path)] if moved else []
        if moved:
            done = f"分類「{kind_of(path)}」を「{kind_of(to)}」に変更（記録 {len(records)} 件）"
        elif reordered:
            done = f"分類「{kind_of(path)}」の順序を変更"
        else:
            done = f"分類「{kind_of(path)}」を変更"
        return to if self._save_taxonomy(taxa, records, done) else None

    def remove_class(self, path: Sequence[str]) -> bool:
        """Removes a class and its branch; the records under it keep the class above."""
        path = list(path)
        now = now_ms()
        up = path[:-1]
        records = [replace(o, kind=kind_of(up), updated_at=now) for o in self.records if within(o.kind, path)]
        note = f"（記録 {len(records)} 件は「{kind_of(up) or '種別なし'}」に）" if records else ""
        return self._save_taxonomy(remove_taxon(self.taxonomy(), path), records, f"分類「{kind_of(path)}」を削除{note}")

    def set_spec_fields(self, fields: list[SpecField]) -> bool:
        """Changes the specification fields (the values of a field taken away stay in the classes)."""
        try:
            self.taxonomy_store.save_fields(fields)
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.taxonomy_store.label} への諸元の項目の保存失敗: {_error_text(error)}")
            return False
        self.spec_fields = list(fields)
        self._say(f"諸元の項目 {len(fields)} 件を設定")
        return True

    # ---- the report ----------------------------------------------------

    def report_selection(self, options: ReportOptions) -> ReportSelection:
        return select_for_report(self.records, self.areas, options, self.confidences())

    def report(self, options: ReportOptions, map_url: Optional[str] = None) -> str:
        """The report (observation-report.ts) as one HTML page."""
        selection = self.report_selection(options)
        return report_html(selection, options, ReportContext(self.store.label, self.observer(), now_ms(), self.confidences(), map_url))
