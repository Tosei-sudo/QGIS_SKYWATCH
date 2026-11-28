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

import json
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .ai_origin import AiOrigin, of_layer_run
from .areas import CheckedArea, area_contains, area_label, box_ring, covering_areas, new_area_id
from .drafts import Draft, PrefsDrafts
from .flows import (
    CarryOver, ImageRef, MatchCandidate, ObjectFlow, carried_into, carry_overs, flow_of, flows, living, match_candidates,
    now_ms,
)
from .model import (
    UNSEEN, ConfidenceLevel, ListedImage, Observation, ObservationVocabulary, new_object_id, new_observation_id, parse_listed_images,
    kept_quantity, read_quantity, recorded_images, state_text, unseen_label,
)
from .report import ReportContext, ReportOptions, ReportSelection, report_html, select_for_report
from .satellites import clean as clean_satellite
from .scope import RecordQuery, grown, merged
from .stores import AreaStore, ObservationStore, TaxonomyStore, ai_missing, quantity_supported, satellite_supported, unseen_supported
from .taxonomy import (
    MemberHit, SpecField, Taxon, activities_at, find, find_member, kind_of, move_taxon, name_label_of, normalize, path_of, place_taxon,
    remove_taxon, rewrite, segment_name, taxonomy_of, update_taxon, with_kinds, within,
)
from . import taxonomy as _tx
from ..together import read_together
from ..util import read_number

_UNSET = object()
#: How far back of the last read a refresh asks for (computers' clocks differ; what is read twice merges the same).
REFRESH_SLACK_MS = 10 * 60 * 1000
#: After the records around an image could not be read, how long before they are asked again (a refresh or 再読み込み asks sooner).
AROUND_RETRY_MS = 60 * 1000


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
    #: Every layer of the image when it has several (a catalog record's images in its folder), `layer_id` the first.
    layers: tuple = ()
    #: 衛星名 (core/obs/satellites.py), '' when not known; where it came from (satellites.ORIGINS), '' when unknown.
    satellite: str = ""
    satellite_origin: str = ""

    @property
    def ref(self) -> ImageRef:
        return ImageRef(self.id, self.name, self.time, self.satellite)

    @property
    def file(self) -> bool:
        return self.layer_id is not None

    @property
    def layer_ids(self) -> tuple:
        """The image's layers (the project's raster layers' ids), () for one listed without its file."""
        return tuple(self.layers) or ((self.layer_id,) if self.layer_id else ())


def joined_image(parts: Sequence[ObservedImage], name: str = "") -> ObservedImage:
    """
    One image to record on of several layers of the same image id (a catalog
    record whose folder has several images): `name` (else the first's), the
    earliest time known, a footprint around them all, every layer (the first
    one's first). The catalog's time (origin "layer") goes first: a duplicate
    of the layer read from its file name did not change it (操作性点検の再検証 R11).
    """
    first = parts[0]
    timed = [p for p in parts if p.time is not None and p.origin == "layer"] or [p for p in parts if p.time is not None]
    earliest = min(timed, key=lambda p: p.time) if timed else None
    boxes = [p.box for p in parts if p.box]
    box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None
    layers = tuple(dict.fromkeys(i for p in parts for i in p.layer_ids))
    named = next((p for p in parts if p.satellite), None)
    return ObservedImage(id=first.id, name=name or first.name, time=earliest.time if earliest else None, box=box,
                         layer_id=layers[0] if layers else None, origin=earliest.origin if earliest else "", layers=layers,
                         satellite=named.satellite if named else "", satellite_origin=named.satellite_origin if named else "")


class Prefs:
    """Small values kept per user (QgsSettings in QGIS): the images listed without their file, per store (in QGIS only while it runs)."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


def _text(n: Optional[float]) -> str:
    return "" if n is None else str(n)


def _seconds(o: Observation) -> Observation:
    def s(t):
        return None if t is None else int(t // 1000)
    return replace(o, image_time=s(o.image_time), created_at=s(o.created_at), updated_at=s(o.updated_at), deleted_at=s(o.deleted_at))


def _same(a: Optional[Observation], b: Observation) -> bool:
    """Whether a record read again is the one known here (services that keep whole seconds aside)."""
    return a is not None and (a == b or _seconds(a) == _seconds(b))


def _width_of(entry: Any) -> Optional[float]:
    """An entry's width, when it has one (entries made before widths have none)."""
    return getattr(entry, "width", None)


def _error_text(error: Exception) -> str:
    return str(error) or type(error).__name__


def _counted(rows: list[Observation]) -> list[Observation]:
    """Records with their 数量 as kept: 2 or more, else none (one thing); none on a 消失 or 不可 record (すべて消失 too)."""
    out = []
    for o in rows:
        n = kept_quantity(o.quantity, o.present and not o.unseen)
        out.append(o if n == o.quantity else replace(o, quantity=n))
    return out


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
    width: str = ""
    #: 数量 as typed ('' or 1: one thing).
    quantity: str = ""


@dataclass
class BulkOutcome:
    """What became of a bulk change of records."""
    saved: list = field(default_factory=list)
    #: Already as asked.
    unchanged: int = 0
    #: 消失 records, whose activity was left empty.
    absent: int = 0
    #: 消失 and 不可 records, left without a 数量.
    uncounted: int = 0
    #: Chosen but no longer here (deleted).
    missing: int = 0
    #: Not saved: of them, changed by someone else since read.
    failed: int = 0
    conflicts: int = 0
    error: str = ""

    def text(self) -> str:
        parts = [f"{len(self.saved)} 件の記録を一括変更"]
        if self.unchanged:
            parts.append(f"変更不要 {self.unchanged} 件")
        if self.absent:
            parts.append(f"消失の {self.absent} 件は活動なしのまま")
        if self.uncounted:
            parts.append(f"消失・不可の {self.uncounted} 件は数量なしのまま")
        if self.failed:
            why = (f"うち {self.conflicts} 件はほかの人が先に変更。「最新を取得」を押してから変更" if self.conflicts else self.error)
            parts.append(f"保存できず {self.failed} 件（{why}）")
        return "・".join(parts)


@dataclass
class RemoveOutcome:
    """What became of deleting several records at once."""
    removed: list = field(default_factory=list)
    #: Chosen but no longer here (already deleted).
    missing: int = 0
    #: Not deleted: of them, changed by someone else since read.
    failed: int = 0
    conflicts: int = 0
    error: str = ""

    def text(self) -> str:
        parts = [f"{len(self.removed)} 件の記録を削除"]
        if self.missing:
            parts.append(f"削除済み {self.missing} 件")
        if self.failed:
            why = (f"うち {self.conflicts} 件はほかの人が先に変更。「最新を取得」を押してから削除" if self.conflicts else self.error)
            parts.append(f"削除できず {self.failed} 件（{why}）")
        return "・".join(parts)


class ObservationBook:
    """The records of one store, and what is done with them."""

    def __init__(self, store: ObservationStore, area_store: AreaStore, taxonomy_store: TaxonomyStore,
                 vocabulary: ObservationVocabulary, observer: Callable[[], str] = lambda: "",
                 prefs: Optional[Prefs] = None, say: Callable[[str], None] = lambda message: None,
                 drafts: Any = None):
        self.store = store
        self.area_store = area_store
        self.taxonomy_store = taxonomy_store
        self.vocabulary = vocabulary
        self.observer = observer
        self.prefs = prefs or Prefs()
        #: Where the records not saved wait (core/obs/drafts.py): the 送信待ち outbox in the plugin.
        self.shelf = drafts if drafts is not None else PrefsDrafts(self.prefs)
        self.say = say
        self.lock = threading.RLock()
        #: Every record and area (deleted ones too).
        self.records: list[Observation] = []
        self.areas: list[CheckedArea] = []
        self._taxonomy: list[Taxon] = list(vocabulary.taxonomy)
        #: The individuals' words for 備考 (note_words) and the classification they are of.
        self._note_words: tuple[Optional[list[Taxon]], tuple] = (None, ())
        self.spec_fields: list[SpecField] = list(vocabulary.spec_fields)
        #: The images in time order: the open ones and those listed without their file.
        self.images: list[ObservedImage] = []
        self.current: Optional[ObservedImage] = None
        self._open: list[ObservedImage] = []
        #: Times typed for images that do not say (by image id).
        self.manual_times: dict[str, int] = {}
        #: 衛星名 typed for images that do not say (by image id).
        self.manual_satellites: dict[str, str] = {}
        self.listed: list[ListedImage] = parse_listed_images(self.prefs.get(self._listed_key))
        #: What the last record said (offered first next time; kept per store across sessions).
        self.last = self._read_last()
        self.loaded = False
        self.status = ""
        #: When the records were last read whole or refreshed (ms, this computer's clock; 0: not yet).
        self._read_at = 0
        #: The draft kept last (a save that did not go through).
        self.last_draft_id = ""
        #: The records a refresh found changed (by someone else): a form opened on one before is not saved over it.
        self.changed_by_others: set[str] = set()
        #: A shared store (Esri) is read only as needed (scope.py, 2026-11-18): the records around the images recorded on
        #: (`_around`: image id → its footprint and time when read) and what features asked for. A local store is read whole.
        self.scoped = bool(getattr(store, "scoped", False))
        self._around: dict[str, tuple] = {}
        #: Image id → (around key, when it could not be read): not asked again for AROUND_RETRY_MS (the screens ask on every render).
        self._around_failed: dict[str, tuple] = {}
        #: Where a place (lon, lat) is in an image's file as the image lies on the map now: (image id, lon, lat) →
        #: (file, pixel x, pixel y), None when the image is not open (set by the plugin: qgis_io/pixel_place.py).
        self.locate: Optional[Callable[[str, float, float], Optional[tuple[str, float, float]]]] = None

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
        """
        Reads the records, areas and classification again from the stores: a
        shared store's records around the image recorded on now (read_around),
        a local store's every record.
        """
        started = now_ms()
        self._say(f"{self.store.label} から記録を取得中…")
        if self.scoped:
            return self._reload_scoped(started)
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
            message = f"{self.area_store.label} から確認範囲の取得に失敗: {_error_text(error)}"
        try:
            records = self.store.load()
            text = f"{self.store.label}: {len(living(records))} 件の記録、{len(flows(records))} のモノ"
        except Exception as error:  # noqa: BLE001
            records = []
            text = f"{self.store.label} から記録の取得に失敗: {_error_text(error)}"
        with self.lock:
            self.records = records
            if not text.startswith(f"{self.store.label} から記録の取得に失敗"):
                self._read_at = started
            self.areas = areas
            self._taxonomy = kept if kept is not None else list(self.vocabulary.taxonomy)
            self.spec_fields = fields if fields is not None else list(self.vocabulary.spec_fields)
            self.loaded = True
        return self._say(message or text)

    def _read_classes(self) -> None:
        try:
            kept = self.taxonomy_store.load()
        except Exception:  # noqa: BLE001 (as the viewer: the configured classification then)
            kept = None
        try:
            fields = self.taxonomy_store.load_fields()
        except Exception:  # noqa: BLE001
            fields = None
        with self.lock:
            self._taxonomy = kept if kept is not None else list(self.vocabulary.taxonomy)
            self.spec_fields = fields if fields is not None else list(self.vocabulary.spec_fields)

    def _reload_scoped(self, started: int) -> str:
        image = self.current
        wanted = [image] if image is not None else []

        def caught(read):
            try:
                return read(), None
            except Exception as error:  # noqa: BLE001 (each told as before)
                return None, error

        # The classes, the records around the image and its checked areas do not need each other: read at once.
        _, (records, failed), (areas, areas_failed) = read_together(
            self._read_classes,
            lambda: caught(lambda: self.store.load_around([self._around_key(i) for i in wanted]) if wanted else []),
            lambda: caught(lambda: self._find_areas(image_ids=[i.id for i in wanted]) if wanted else []))
        if failed is not None:
            with self.lock:
                self.records, self._around = [], {}
                self._around_failed = {i.id: (self._around_key(i), now_ms()) for i in wanted}
                self.loaded = True
            return self._say(f"{self.store.label} から記録の取得に失敗: {_error_text(failed)}")
        message = ""
        if areas_failed is not None:
            areas = []
            message = f"{self.area_store.label} から確認範囲の取得に失敗: {_error_text(areas_failed)}"
        with self.lock:
            self.records = records
            self.areas = areas
            self._around = {i.id: self._around_key(i) for i in wanted}
            self._read_at = started
            self.loaded = True
        if message:
            return self._say(message)
        if image is None:
            return self._say(f"{self.store.label}: 画像を選ぶと、その画像とまわりの記録を取得")
        here = sum(1 for o in records if o.image_id == image.id and o.deleted_at is None)
        return self._say(f"{self.store.label}: この画像 {here} 件、まわりの記録 {len(living(records)) - here} 件")

    def _around_key(self, image: "ObservedImage") -> tuple:
        """What load_around asks for an image: its id, its footprint grown by the 同一候補 radius, its time."""
        box = tuple(grown(image.box, self.vocabulary.match_radius)) if image.box else None
        return (image.id, box, image.time)

    def _find_areas(self, image_ids: Sequence[str] = (), start: Optional[float] = None, end: Optional[float] = None) -> list[CheckedArea]:
        find_ = getattr(self.area_store, "find", None)
        if find_ is None:
            from .stores import area_in

            return [a for a in self.area_store.load() if area_in(a, image_ids, start, end)]
        return find_(image_ids, start, end)

    def needs_around(self, image_id: Optional[str] = None) -> bool:
        """Whether the records around the image recorded now (or the open one of `image_id`) are still to be read (read_around)."""
        image = self._image(image_id)
        if not self.scoped or not self.loaded or image is None:
            return False
        key = self._around_key(image)
        failed = self._around_failed.get(image.id)
        if failed is not None and failed[0] == key and now_ms() - failed[1] < AROUND_RETRY_MS:
            return False
        return self._around.get(image.id) != key

    def read_around(self, image_id: Optional[str] = None) -> Optional[str]:
        """
        Reads the records around the image recorded now (or the open one of `image_id`;
        EsriObservationStore.load_around), and its checked areas, when not read yet;
        merged into those read before. What is said, None when nothing was read.
        """
        image = self._image(image_id)
        if not self.needs_around(image_id):
            return None
        try:
            records = self.store.load_around([self._around_key(image)])
            areas = self._find_areas(image_ids=[image.id])
        except Exception as error:  # noqa: BLE001
            self._around_failed[image.id] = (self._around_key(image), now_ms())
            return self._say(f"{self.store.label} から記録の取得に失敗: {_error_text(error)}")
        self._around_failed.pop(image.id, None)
        with self.lock:
            self.records = merged(self.records, records)
            self.areas = merged(self.areas, areas)
            self._around[image.id] = self._around_key(image)
        here = sum(1 for o in records if o.image_id == image.id and o.deleted_at is None)
        return self._say(f"{self.store.label}: この画像 {here} 件、まわりの記録 {len(living(records)) - here} 件")

    def fetch(self, query: RecordQuery) -> list[Observation]:
        """
        The records of a query (scope.RecordQuery), for what works on more than
        the images recorded on (通報, 書き出し, 報告書, 引き継ぎ…): asked of a
        shared store and kept here too; a local store's are all read already.
        Raises what the store raises.
        """
        if not self.scoped:
            if not self.loaded:
                self.reload()
            with self.lock:
                return [o for o in self.records if query.matches(o)]
        found = self.store.find(query)
        with self.lock:
            self.records = merged(self.records, found)
        return found

    def fetch_areas(self, image_ids: Sequence[str] = (), start: Optional[float] = None, end: Optional[float] = None) -> list[CheckedArea]:
        """The checked areas of some images or a period (as fetch)."""
        from .stores import area_in

        if not self.scoped:
            if not self.loaded:
                self.reload()
            with self.lock:
                return [a for a in self.areas if area_in(a, image_ids, start, end)]
        found = self._find_areas(image_ids, start, end)
        with self.lock:
            self.areas = merged(self.areas, found)
        return found

    def fetch_things(self, object_ids: Iterable[str]) -> list[Observation]:
        """Every record of some things (their whole flow), as fetch."""
        ids = tuple(dict.fromkeys(i for i in object_ids if i))
        return self.fetch(RecordQuery(object_ids=ids)) if ids else []

    def _relevant(self, changed: list[Observation]) -> list[Observation]:
        """Of the records someone else changed, those that belong with the ones read here (a shared store)."""
        things = {o.object_id for o in self.records}
        known = {o.id for o in self.records}
        boxes = [key[1] for key in self._around.values() if key[1]]
        return [o for o in changed if o.id in known or o.image_id in self._around or o.object_id in things
                or any(b[0] <= o.lon <= b[2] and b[1] <= o.lat <= b[3] for b in boxes)]

    def refresh(self) -> Optional[str]:
        """
        Reads what others saved since the last read (or this one) and merges
        it into the records here, quietly: the 更新日時 of a few minutes
        before then (computers' clocks differ). Everything is read again when
        the store cannot tell what changed (a local store, a layer without
        更新日時 or that deletes for good). What changed is said; None when
        nothing did (or it could not be read: then the records stay as they
        were and a later refresh or 「最新を取得」 tries again).
        """
        started = now_ms()
        since = self._read_at
        load_changed = getattr(self.store, "load_changed", None)
        try:
            changed = load_changed(since - REFRESH_SLACK_MS) if load_changed is not None and since else None
            if changed is None:
                # A shared store that cannot tell what changed (or is not reached: its cache): the images' surroundings again.
                records = self.store.load_around(list(self._around.values())) if self.scoped else self.store.load()
        except Exception:  # noqa: BLE001 (quiet: the next refresh tries again)
            return None
        with self.lock:
            if changed is not None and self.scoped:
                changed = self._relevant(changed)
            if changed is None and self.scoped:
                before = {o.id: o for o in self.records}
                news = [o for o in records if not _same(before.get(o.id), o)]
                fresh = {o.id for o in records}
                gone = {o.id for o in self.records if o.image_id in self._around and o.id not in fresh}
                self.records = merged([o for o in self.records if o.id not in gone], records)
            elif changed is None:
                before = {o.id: o for o in self.records}
                news = [o for o in records if not _same(before.get(o.id), o)]
                gone = set(before) - {o.id for o in records}
                self.records = records
            else:
                at = {o.id: i for i, o in enumerate(self.records)}
                news = [o for o in changed if o.id not in at or not _same(self.records[at[o.id]], o)]
                gone = set()
                for o in news:
                    if o.id in at:
                        self.records[at[o.id]] = o
                    else:
                        at[o.id] = len(self.records)
                        self.records.append(o)
            self._read_at = started
            self.changed_by_others |= {o.id for o in news}
        if not news and not gone:
            return None
        return self._say(f"{self.store.label}: ほかの人の記録 {len(news) + len(gone)} 件を取得")

    def reload_taxonomy(self) -> str:
        """
        Reads the classification and specification fields again (as others may
        have changed them in a shared store); kept as they were when it fails.
        """
        self._say(f"{self.taxonomy_store.label} から分類を取得中…")
        try:
            self._read_taxonomy()
        except Exception as error:  # noqa: BLE001
            return self._say(f"{self.taxonomy_store.label} から分類の取得に失敗: {_error_text(error)}")
        return self._say(f"{self.taxonomy_store.label}: 分類 {len(self._taxonomy)} 件を取得")

    def _read_taxonomy(self) -> None:
        """Reads the classification and specification fields again; raises what the store raises (kept as they were)."""
        kept = self.taxonomy_store.load()
        fields = self.taxonomy_store.load_fields()
        with self.lock:
            self._taxonomy = kept if kept is not None else list(self.vocabulary.taxonomy)
            self.spec_fields = fields if fields is not None else list(self.vocabulary.spec_fields)

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

    @property
    def _last_key(self) -> str:
        return f"last_values/{self.store.label}"

    def _read_last(self) -> FormValues:
        try:
            value = json.loads(self.prefs.get(self._last_key) or "{}")
        except ValueError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        return FormValues(**{k: str(value.get(k) or "") for k in ("kind", "activity", "confidence")})

    def remember(self, kind: Optional[str] = None, activity: Optional[str] = None, confidence: Optional[str] = None) -> None:
        """
        What the next new record offers first (None: as it was), kept for the
        next session too. Not in a task: the prefs are QgsSettings in QGIS.
        """
        last = self.last
        self.last = FormValues(kind=last.kind if kind is None else kind_of(path_of(kind)),
                               activity=last.activity if activity is None else activity.strip(),
                               confidence=last.confidence if confidence is None else confidence.strip())
        if self.last != last:
            self.prefs.set(self._last_key, json.dumps({"kind": self.last.kind, "activity": self.last.activity,
                                                       "confidence": self.last.confidence}, ensure_ascii=False))

    def set_open_images(self, images: Sequence[ObservedImage], selected: Optional[str] = None) -> None:
        """The project's images again (call when they change): in order of acquisition time with those listed without their file."""
        with self.lock:
            self._open = list(images)
            self._order(selected)

    def _with_manual(self, info: ObservedImage) -> ObservedImage:
        manual = self.manual_times.get(info.id)
        if info.time is None and manual is not None:
            info = replace(info, time=manual, origin="manual")
        satellite = self.manual_satellites.get(info.id)
        if not info.satellite and satellite:
            info = replace(info, satellite=satellite, satellite_origin="manual")
        return info

    def _order(self, selected: Optional[str] = None) -> None:
        entries = [self._with_manual(i) for i in self._open]
        ids = {e.id for e in entries}
        for listed in self.listed:
            if listed.id not in ids:
                entries.append(self._with_manual(ObservedImage(listed.id, listed.name, listed.time, listed.box, None,
                                                               "manual" if listed.time is not None else "", satellite=listed.satellite)))
        entries.sort(key=lambda e: (e.time if e.time is not None else float("inf"), e.name))
        was = self.current
        keep = None
        if was is not None:
            # The same image: one of its layers still there (another of a record's images may have closed), or listed without its file.
            had = set(was.layer_ids)
            keep = next((e for e in entries if (had & set(e.layer_ids) if had else (not e.layer_id and e.id == was.id))), None)
        if keep is None and selected:
            keep = next((e for e in entries if selected in e.layer_ids), None)
        self.images = entries
        self.current = keep or (entries[0] if entries else None)

    def go_to(self, image_id: str) -> bool:
        """Records on the image of this id (an open one or one listed without its file) from now on."""
        entry = next((e for e in self.images if e.id == image_id), None)
        if entry is not None:
            self.current = entry
        return entry is not None

    def go_to_layer(self, layer_id: str) -> bool:
        """Records on the image of this layer (any of a record's images) from now on."""
        entry = next((e for e in self.images if layer_id in e.layer_ids), None)
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
        self._say(f"{info.name} は開いている画像と同じ ID（その画像で記録）" if entry and entry.file else f"ファイルのない画像 {info.name} を追加")
        return entry

    def list_images(self, images: list[ListedImage], replacing: Sequence[str] = ()) -> Optional[ObservedImage]:
        """
        Lists several images without their files at once (the catalog's
        「ファイルなしで記録」: their ids, times and footprints as the catalog
        says), the first one recorded on from now on; one listed before is
        replaced (the catalog's values win). The images of `replacing` listed
        before go off the list with it, in the same save (the catalog's
        「前の画像を閉じる」; their records stay); nothing goes when none is listed.
        """
        wanted: dict[str, ListedImage] = {}
        for i in images:
            image_id = i.id.strip()
            if image_id and image_id not in wanted:
                wanted[image_id] = replace(i, id=image_id, name=i.name.strip() or image_id)
        if not wanted:
            self._say("画像 ID のある画像を選択")
            return None
        first = next(iter(wanted))
        going = set(replacing) - set(wanted)
        with self.lock:
            gone = [i for i in self.listed if i.id in going]
            self.listed = [i for i in self.listed if i.id not in wanted and i.id not in going] + list(wanted.values())
            self._save_listed()
            self._order()
            self.go_to(first)
        entry = self.current
        if len(wanted) == 1:
            info = wanted[first]
            said = f"{info.name} は開いている画像と同じ ID（その画像で記録）" if entry and entry.file else f"ファイルのない画像 {info.name} を追加"
        else:
            opened = sum(1 for e in self.images if e.file and e.id in wanted)
            said = f"ファイルのない画像 {len(wanted)} 件を追加" + (f"。うち {opened} 件は開いている画像で記録" if opened else "")
        if gone:
            said += f"。前の {gone[0].name} は一覧から除外（記録は残る）" if len(gone) == 1 else f"。前の {len(gone)} 件は一覧から除外（記録は残る）"
        self._say(said)
        return entry

    def unlist_image(self, image_id: str) -> bool:
        """Takes an image without its file off the list (its records stay)."""
        return self.unlist_images([image_id]) > 0

    def unlist_images(self, image_ids: Sequence[str]) -> int:
        """
        Takes images without their files off the list at once (their records
        stay; one of them chosen: the first image then): how many were on it.
        Their layers removed from the map do it (ui/observation_dock).
        """
        wanted = set(image_ids)
        gone = [i for i in self.listed if i.id in wanted]
        if not gone:
            return 0
        with self.lock:
            self.listed = [i for i in self.listed if i.id not in wanted]
            self._save_listed()
            if self.current is not None and not self.current.file and self.current.id in wanted:
                self.current = None
            self._order()
        self._say(f"{gone[0].name} を一覧から除外（記録は残る）" if len(gone) == 1 else f"{len(gone)} 件を一覧から除外（記録は残る）")
        return len(gone)

    def _save_listed(self) -> None:
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

    def keeps_satellite(self) -> bool:
        """Whether the store keeps 衛星名 (its layer has SATELLITE: a local one does; an Esri one once read)."""
        return satellite_supported(self.store)

    def set_image_satellite(self, name: str) -> list[Observation]:
        """
        衛星名 of the image recorded now, typed (for one that does not say): its
        records and checked areas made without it get it (as 撮像日時の入力 does).
        """
        entry = self.current
        name = clean_satellite(name)
        if entry is None or not name:
            return []
        with self.lock:
            self.manual_satellites[entry.id] = name
            if not entry.file:
                self.listed = [replace(i, satellite=name) if i.id == entry.id and not i.satellite else i for i in self.listed]
                self._save_listed()
            self._order()
        image = self.current if self.current is not None and self.current.id == entry.id else None
        name = image.satellite if image is not None and image.satellite else name
        now = now_ms()
        unnamed = [replace(o, satellite=name, updated_at=now) for o in self.records if o.image_id == entry.id and not o.satellite]
        areas = [replace(a, satellite=name, updated_at=now) for a in self.areas if a.image_id == entry.id and not a.satellite]
        if areas:
            try:
                self.area_store.save(areas)
            except Exception as error:  # noqa: BLE001 (said; the records still get it)
                self._say(f"{self.area_store.label} への確認範囲の保存に失敗: {_error_text(error)}")
            else:
                kept = {a.id: a for a in self._kept_areas(areas)}
                with self.lock:
                    self.areas = [kept.get(a.id, a) for a in self.areas]
        if unnamed:
            self._save_records(unnamed, f"{len(unnamed)} 件の記録に衛星名「{name}」を入力")
        else:
            self._say(f"衛星名「{name}」を入力" + (f"（確認範囲 {len(areas)} 件）" if areas else ""))
        return unnamed

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

    def carried_for(self, image: Optional[str] = None) -> list[CarryOver]:
        """What may still be there in this image or the one of `image` (its id); within the footprint for this one."""
        info = self._image(image)
        if info is None:
            return []
        return carry_overs(self.records, info.ref, self.inside() if info is self.current else None)

    def inside_carried(self, carried: Optional[list[CarryOver]] = None) -> list[CarryOver]:
        """
        The carried-over things that lie in one of this image's checked areas (so were not there),
        but in a 不可範囲 (which could not be looked at).
        """
        image = self.current
        if image is None:
            return []
        carried = self.carried() if carried is None else carried
        hidden = {id(c) for c in self.unseen_carried(carried)}
        return [c for c in carried if id(c) not in hidden and covering_areas(self.areas, image.id, c.source.lon, c.source.lat, c.source.kind)]

    def unseen_carried(self, carried: Optional[list[CarryOver]] = None) -> list[CarryOver]:
        """The carried-over things that lie in one of this image's 不可範囲 (could not be seen there)."""
        image = self.current
        if image is None or not any(a.unseen and a.deleted_at is None and a.image_id == image.id for a in self.areas):
            return []
        carried = self.carried() if carried is None else carried
        return [c for c in carried if covering_areas(self.areas, image.id, c.source.lon, c.source.lat, c.source.kind, unseen=True)]

    def unseen_reason_at(self, lon: float, lat: float, image: Optional[str] = None) -> str:
        """Why a place could not be seen in this image (or `image`): the reason of the latest 不可範囲 over it, '' when none is."""
        info = self._image(image)
        if info is None:
            return ""
        over = covering_areas(self.areas, info.id, lon, lat, "", unseen=True)
        return max(over, key=lambda a: a.created_at).unseen if over else ""

    def keeps_quantity(self) -> bool:
        """Whether the store keeps 数量 (its layer has QUANTITY: a local one does; an Esri one once read, 2026-11-24)."""
        return quantity_supported(self.store)

    def ai_missing(self, areas: bool = False, ask: bool = False) -> list[str]:
        """
        The AI columns (SOURCE, AI_LAYER…, 2026-11-27) the store's layer (`areas`: the checked areas') lacks: no AI result
        is recorded there, nor one as wrong. [] for a local store; an Esri layer's as read (`ask`: asked first, as a save
        would, off the screen's thread), else [].
        """
        return ai_missing(self.area_store if areas else self.store, ask)

    def keeps_unseen(self) -> bool:
        """Whether 「不可」 records can be kept (the store's layer has the 不可の理由 attribute)."""
        return unseen_supported(self.store)

    def keeps_unseen_areas(self) -> bool:
        """Whether 不可範囲 can be kept (both layers have the 不可の理由 attribute: the things in one are recorded 不可)."""
        return unseen_supported(self.store) and unseen_supported(self.area_store)

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

    def _placed(self, rows: list[Observation]) -> list[Observation]:
        """
        The records with where they are in their image's file (pixel_x,
        pixel_y, image_file): found for a new record and one moved (another
        place or image) from the image as it lies on the map now (moved by
        位置ずらし too: the file's own pixels), none when the image is not open
        (an old value is not kept for a new place). A record not moved keeps
        its own (an older one without them stays so); a new one given them
        (an AI result found in the file) keeps those.
        """
        before = {o.id: o for o in self.records}
        out = []
        for o in rows:
            was = before.get(o.id)
            if was is not None and (was.lon, was.lat, was.image_id) == (o.lon, o.lat, o.image_id):
                out.append(o)
                continue
            if was is None and o.pixel_x is not None and o.pixel_y is not None:
                out.append(o)
                continue
            found = None
            if self.locate is not None:
                try:
                    found = self.locate(o.image_id, o.lon, o.lat)
                except Exception:  # noqa: BLE001 (never stops a record: saved without its pixels)
                    found = None
            out.append(replace(o, image_file=found[0], pixel_x=found[1], pixel_y=found[2]) if found
                       else replace(o, pixel_x=None, pixel_y=None, image_file=""))
        return out

    def _kept(self, rows: list[Observation]) -> list[Observation]:
        """The records as the store keeps them (an Esri layer without some optional attributes: those left out)."""
        as_kept = getattr(self.store, "as_kept", None)
        try:
            return as_kept(rows) if callable(as_kept) else rows
        except Exception:  # noqa: BLE001 (as they are)
            return rows

    def _kept_areas(self, areas: list[CheckedArea]) -> list[CheckedArea]:
        """The checked areas as the store keeps them (an Esri layer without SATELLITE: none)."""
        as_kept = getattr(self.area_store, "as_kept", None)
        try:
            return as_kept(areas) if callable(as_kept) else areas
        except Exception:  # noqa: BLE001 (as they are)
            return areas

    def _satellite_of(self, image_id: str) -> str:
        """衛星名 of an image open or listed now ('' when not known or not there)."""
        entry = next((e for e in self.images if e.id == image_id), None)
        return entry.satellite if entry is not None else ""

    def _named(self, rows: list[Observation]) -> list[Observation]:
        """
        The records with their image's 衛星名 (core/obs/satellites.py): a record
        without one gets it when its image is open or listed now and says it
        (a new one, one changed); one that has it keeps it.
        """
        out = []
        for o in rows:
            satellite = "" if o.satellite else self._satellite_of(o.image_id)
            out.append(replace(o, satellite=satellite) if satellite else o)
        return out

    def _save_records(self, rows: list[Observation], done: str) -> Optional[list[Observation]]:
        """Saves records in the store and in the list; the records as saved, None when the store refused."""
        if not rows:
            return []
        by = self.observer()
        known = {o.id for o in self.records}
        # Who made (a new record) and who changed it: the one recording now.
        rows = [replace(o, updated_by=by) if o.id in known else replace(o, recorded_by=by, updated_by=by) for o in rows]
        rows = self._named(self._placed(_counted(rows)))
        try:
            self.store.save(rows)
        except Exception as error:  # noqa: BLE001 (said, as the viewer)
            self._say(f"{self.store.label} への保存に失敗: {_error_text(error)}")
            return None
        rows = self._kept(rows)
        with self.lock:
            by_id = {o.id: o for o in rows}
            self.records = [by_id.pop(o.id, o) for o in self.records] + list(by_id.values())
        if done:
            self._say(done)
        return rows

    def individual(self, kind: str) -> bool:
        """
        Whether things of a kind are individuals told apart by name: a class
        on its path names what an individual's name is called (`nameLabel`:
        艦名, 機番). Only their names are keys (tsudo 2026-11-18): other
        things may share a name (車両), and are the same thing only by
        「継続」 or 「同一候補」.
        """
        return bool(kind) and bool(name_label_of(self._taxonomy, path_of(kind)))

    def names_a_thing(self, name: str, kind: str) -> bool:
        """Whether a name typed for a kind names one thing: an individual's own name (not its class's name, given when none is typed)."""
        return bool(name) and name not in path_of(kind) and name != "モノ" and self.individual(kind)

    def flow_by_name(self, name: str, kind: str = "", ask: bool = False) -> Optional[ObjectFlow]:
        """
        The thing a typed name means: one whose id it is, or an individual of
        that name (of `kind`'s classes when given). `ask`: looked for in the
        store too when not among the records read (a shared store; in a task).
        """
        if not name:
            return None

        def pick(records: Iterable[Observation]) -> Optional[ObjectFlow]:
            everything = flows(records)
            by_id = next((f for f in everything if f.object_id == name), None)
            if by_id is not None:
                return by_id
            named = [f for f in everything if f.name == name and self.names_a_thing(name, f.kind)]
            if kind and named:
                top = (path_of(kind) or [""])[0]
                named = [f for f in named if (path_of(f.kind) or [""])[0] == top] or named
            return named[0] if named else None

        found = pick(self.records)
        if found is None and ask and self.scoped and (name.startswith("OBJ-") or self.names_a_thing(name, kind)):
            try:
                rows = self.fetch(RecordQuery(names=(name,), object_ids=(name,), living=True))
                if rows:
                    found = pick(self.fetch_things({o.object_id for o in rows}))
            except Exception:  # noqa: BLE001 (not found then: a new thing)
                found = None
        return found

    def default_name(self, kind: str) -> str:
        """A new thing's name when none is typed: its class's (車両; 船舶 > 護衛艦 → 護衛艦)."""
        path = path_of(kind)
        return path[-1] if path else "モノ"

    def record_from(self, image: ObservedImage, lon: float, lat: float, editing: Optional[Observation], source: Optional[Observation],
                    values: FormValues, present: bool, unseen: str = "") -> Observation:
        """The record the form says (`unseen`: 「不可」, why; then neither there nor gone)."""
        now = now_ms()
        unseen = unseen.strip()
        if unseen:
            present = False

        def number(v: str) -> Optional[float]:
            # Full-width digits and a unit (１２０．５, 120 m, 30°) too; the dock stops on what can't be read.
            try:
                return read_number(v)
            except ValueError:
                return None

        length = number(values.length)
        heading = number(values.heading)
        width = number(values.width)
        measure = {"length": length if length is not None and length > 0 else None, "heading": None if heading is None else (heading % 360 + 360) % 360,
                   "width": width if width is not None and width > 0 else None}
        try:
            # 数量: the dock stops on what can't be read (0, 2.5); none for 消失 and 不可.
            measure["quantity"] = kept_quantity(read_quantity(values.quantity), present)
        except ValueError:
            measure["quantity"] = editing.quantity if editing is not None else None
        name = values.object.strip()
        kind = kind_of(path_of(values.kind))
        activity = values.activity.strip() if present else ""
        confidence = (values.confidence or "").strip()
        if editing is not None:
            # The same thing unless another one's name (an individual's) or id was typed.
            other = self.flow_by_name(name, kind, ask=True) if name and name != editing.object_name else None
            other = other if other is None or other.object_id != editing.object_id else None
            return replace(editing, object_id=other.object_id if other else editing.object_id,
                           object_name=other.name if other else (name or editing.object_name), kind=kind, activity=activity,
                           present=present, unseen=unseen, confidence=confidence, note=values.note, lon=lon, lat=lat, updated_at=now,
                           **measure)
        if source is not None and (not name or name == source.object_name or not self.names_a_thing(name, kind)):
            # 継続・同一候補: the same thing (a name typed for a thing that is not an individual renames it here).
            known = (source.object_id, name or source.object_name)
        else:
            flow = self.flow_by_name(name, kind, ask=True)
            known = (flow.object_id, flow.name) if flow else None
        return Observation(
            id=new_observation_id(), image_id=image.id, image_name=image.name, image_time=image.time, satellite=image.satellite,
            object_id=known[0] if known else new_object_id(),
            object_name=known[1] if known else (name or self.default_name(kind)),
            kind=kind, activity=activity, present=present, unseen=unseen, confidence=confidence, note=values.note, lon=lon, lat=lat,
            created_at=now, updated_at=now, deleted_at=None, **measure,
        )

    def submit(self, lon: float, lat: float, values: FormValues, present: bool = True, editing: Optional[Observation] = None,
               source: Optional[Observation] = None, draft: Optional[str] = None, unseen: str = "",
               image: Optional[ObservedImage] = None) -> Optional[Observation]:
        """
        Records what the form says on the image recorded now, or `image` (the
        one the form was opened on): a new record, a change, or a thing
        carried over. Not saved: kept as a draft (送信待ち,
        「保存できなかった観測記録」); `draft`: the form was opened again from that one
        (its record id is kept, and it goes when saved).
        """
        image = image if image is not None else self.current
        if image is None:
            self._say("画像なし。画像を開くか、＋ でファイルのない画像を追加")
            return None
        if editing is not None:
            now = self.record(editing.id)
            if now is not None and now != editing and editing.id in self.changed_by_others:
                # Read again since the form opened (refresh): someone else's change is not overwritten (#74).
                who = now.updated_by or now.recorded_by
                self._say(f"{editing.object_name} は開いた後に{who + ' さんが' if who else 'ほかの人が'}更新したため保存せず。閉じて開き直してから変更")
                kept = self.draft(draft) if draft else None
                self.keep_draft(Draft(self._by(self.record_from(image, lon, lat, editing, source, values, present, unseen)),
                                      {k: getattr(values, k) for k in FormValues.__dataclass_fields__}, present, editing.id,
                                      editing.updated_at, source.id if source else "", now_ms(), self.status, needs_check=True),
                                replaces=kept.id if kept else None)
                return None
        record = self.record_from(image, lon, lat, editing, source, values, present, unseen)
        present = record.present
        kept = self.draft(draft) if draft else None
        if kept is not None and editing is None:
            # The same record as the first try: one the server took meanwhile is updated, not added twice (#73).
            record = replace(record, id=kept.record.id, created_at=kept.record.created_at)
        self.last = FormValues(kind=record.kind, activity=record.activity if present else self.last.activity, confidence=record.confidence)
        what = (f"（{record.activity}）" if record.activity else "") if present else f"（{state_text(record)}）"
        saved = self._save_records([record], f"{record.object_name}{what}を{'更新' if editing else '記録'}")
        if saved:
            if kept is not None:
                self.drop_draft(kept.id)
            return saved[0]
        # Someone else's change in the way (#74): to be looked at before it is sent again.
        crossed = record.id in (getattr(self.store, "last_conflicts", None) or ())
        self.keep_draft(Draft(self._by(record), {k: getattr(values, k) for k in FormValues.__dataclass_fields__}, present,
                              editing.id if editing else "", editing.updated_at if editing else None,
                              source.id if source else "", now_ms(), self.status, needs_check=crossed),
                        replaces=kept.id if kept else None)
        return None

    def keep_typed(self, image: ObservedImage, lon: float, lat: float, values: FormValues, editing: Optional[Observation] = None,
                   source: Optional[Observation] = None, draft: Optional[str] = None, reason: str = "") -> Draft:
        """
        What a form said and was not recorded yet (its image closed meanwhile, tsudo 2026-11-28: the form closed and
        what was typed was lost): kept as a draft of 「保存できなかった観測記録」, sent only when chosen in its list
        (`needs_check`: it was not recorded by the person). `draft`: the form was one of those, opened again (it
        is replaced).
        """
        kept = self.draft(draft) if draft else None
        record = self.record_from(image, lon, lat, editing, source, values, True)
        if kept is not None and editing is None:
            record = replace(record, id=kept.record.id, created_at=kept.record.created_at)
        d = Draft(self._by(record), {k: getattr(values, k) for k in FormValues.__dataclass_fields__}, True,
                  editing.id if editing else "", editing.updated_at if editing else None, source.id if source else "",
                  now_ms(), reason, needs_check=True)
        self.keep_draft(d, replaces=kept.id if kept else None)
        return self.draft(d.id) or d

    def _by(self, record: Observation) -> Observation:
        """A record as it is saved by the one recording now (as _save_records stamps it), for a draft sent later."""
        by = self.observer()
        if any(o.id == record.id for o in self.records):
            return replace(record, updated_by=by)
        return replace(record, recorded_by=by, updated_by=by)

    # ---- 保存できなかった記録 (core/obs/drafts.py) ------------------------------------
    # Kept in the 送信待ち outbox (self.shelf) and sent from its list (core/obs/offline_stores.upload_drafts).

    def drafts(self) -> list[Draft]:
        """The records of this store not saved, kept on this computer (oldest first)."""
        return self.shelf.all(self.store.label)

    def draft(self, draft_id: str) -> Optional[Draft]:
        return self.shelf.get(self.store.label, draft_id)

    def keep_draft(self, draft: Draft, replaces: Optional[str] = None) -> None:
        # Where it is in its image and its 衛星名, found now (the image may be closed when it is sent).
        draft = replace(draft, record=self._named(self._placed([draft.record]))[0])
        self.shelf.keep(self.store.label, draft, replaces)
        self.last_draft_id = draft.id

    def drop_draft(self, draft_id: str) -> bool:
        return self.shelf.drop(self.store.label, [draft_id]) > 0

    def add(self, lon: float, lat: float, image: Optional[str] = None, object: str = "", kind: Optional[str] = None,
            activity: Optional[str] = None, confidence: Optional[str] = None, note: str = "", present: bool = True,
            length: Optional[float] = None, heading: Optional[float] = None, width: Optional[float] = None,
            quantity: Optional[int] = None) -> Optional[Observation]:
        """Records a new observation (as the form does) on this image or the one of `image` (its id); `quantity`: 数量 (None: one thing)."""
        info = self._image(image)
        if info is None:
            return None
        values = FormValues(object=object, kind=self.last.kind if kind is None else kind,
                            activity=self.last.activity if activity is None else activity,
                            confidence=self.last.confidence if confidence is None else confidence, note=note,
                            length="" if length is None else str(length), heading="" if heading is None else str(heading),
                            width="" if width is None else str(width), quantity="" if quantity is None else str(quantity))
        record = self.record_from(info, lon, lat, None, None, values, present)
        saved = self._save_records([record], f"{record.object_name} を記録")
        return saved[0] if saved else None

    def add_many(self, entries: Sequence[Any], image: Optional[str] = None, keep: bool = False,
                 done: Optional[str] = None) -> Optional[list[Observation]]:
        """
        Records several new observations at once on this image (or `image`), in
        one save: each entry has lon, lat, object, kind, activity, confidence,
        note, length, heading, width (as add takes them). Entries without a name are new
        things named after their class (船舶); the same name typed twice is one thing.
        `keep`: not saved, each is kept in 送信待ち (as the form's records are);
        `done`: what is said when saved (else 「n 件を記録」).
        """
        info = self._image(image)
        if info is None:
            self._say("画像なし。取り込む画像を開いてから取り込み")
            return None
        missing = self.ai_missing(ask=True) if any(getattr(e, "ai", None) is not None for e in entries) else []
        if missing:
            self._say(f"{self.store.label} には AI の結果を記録できない（管理者に連絡: 列 {'・'.join(missing)} を追加）［E704］")
            return None
        made: dict[str, tuple[str, str]] = {}
        rows: list[Observation] = []
        # The things already in the image, and those continued in this save: each once.
        here = {o.object_id for o in living(self.records) if o.image_id == info.id}
        carried = 0
        for e in entries:
            source = getattr(e, "carry", None)
            if source is not None and source.object_id in here:
                source = None
            if source is not None:
                # The same thing: its class unless the AI says it (or one below it), its activity and size when the AI has none.
                kind = e.kind if e.kind and within(e.kind, path_of(source.kind)) else source.kind
                values = FormValues(object="", kind=kind, activity=e.activity or source.activity, confidence=e.confidence, note=e.note,
                                    length=_text(e.length if e.length is not None else source.length),
                                    heading=_text(e.heading if e.heading is not None else source.heading),
                                    width=_text(_width_of(e) if _width_of(e) is not None else source.width))
                record = self.record_from(info, e.lon, e.lat, None, source, values, True)
                # The AI result it was made from (its columns, not the note; 2026-11-27).
                record = replace(record, ai=getattr(e, "ai", None))
                here.add(record.object_id)
                carried += 1
                rows.append(record)
                continue
            values = FormValues(object=e.object, kind=e.kind, activity=e.activity, confidence=e.confidence, note=e.note,
                                length=_text(e.length), heading=_text(e.heading), width=_text(_width_of(e)))
            record = replace(self.record_from(info, e.lon, e.lat, None, None, values, True), ai=getattr(e, "ai", None))
            pixel = getattr(e, "pixel", None)
            if pixel:
                # Where the entry is in the file, found before (an AI result of another system: by the file's own place).
                record = replace(record, image_file=pixel[0], pixel_x=pixel[1], pixel_y=pixel[2])
            name = e.object.strip()
            # The same name typed twice in one save is one thing, but a class's name (given when none is typed).
            if name and name not in path_of(record.kind) and name != "モノ":
                if name in made:
                    record = replace(record, object_id=made[name][0], object_name=made[name][1])
                made.setdefault(name, (record.object_id, record.object_name))
            rows.append(record)
        saved = self._save_records(rows, done or (f"{len(rows)} 件を記録" + (f"（継続 {carried} 件）" if carried else "")))
        if saved is None and keep:
            # Not lost: sent again from 送信待ち (or opened in the form from there).
            for record in rows:
                values = FormValues(object=record.object_name, kind=record.kind, activity=record.activity, note=record.note,
                                    confidence=record.confidence, length=_text(record.length), heading=_text(record.heading),
                                    width=_text(record.width), quantity=_text(record.quantity))
                self.keep_draft(Draft(self._by(record), {k: getattr(values, k) for k in FormValues.__dataclass_fields__}, True,
                                      "", None, "", now_ms(), self.status))
        return saved

    def update(self, record_id: str, **changes: Any) -> Optional[Observation]:
        """Changes a record (its id): kind, activity, present, unseen (不可, why), confidence, note, lon, lat, length, heading, width, quantity (数量)."""
        was = next((o for o in self.records if o.id == record_id and o.deleted_at is None), None)
        if was is None:
            return None
        allowed = {"kind", "activity", "present", "unseen", "confidence", "note", "lon", "lat", "length", "heading", "width", "quantity", "object_name"}
        bad = set(changes) - allowed
        if bad:
            raise ValueError(f"変更不可の項目: {', '.join(sorted(bad))}")
        if "kind" in changes:
            changes["kind"] = kind_of(path_of(changes["kind"]))
        if "unseen" in changes:
            changes["unseen"] = str(changes["unseen"] or "").strip()
        if changes.get("unseen"):
            # 不可: neither there nor gone.
            changes["present"] = False
        elif "present" in changes:
            changes["unseen"] = ""
        if changes.get("present") is False:
            changes["activity"] = ""
        record = replace(was, **changes, updated_at=now_ms())
        saved = self._save_records([record], f"{record.object_name} を更新")
        return saved[0] if saved else None

    def update_many(self, record_ids: Sequence[str], kind: Optional[str] = None, activity: Optional[str] = None,
                    confidence: Optional[str] = None, quantity: Optional[int] = None) -> "BulkOutcome":
        """
        Sets the kind, the activity, the 確度 or the 数量 (1: one thing each) of several records at once (一括編集), in one save.
        A 消失 record keeps no activity, and no 数量 (or a 不可 one). The store's checks hold: records someone else changed since
        they were read are not overwritten; those that went through are kept here, and the rest told.
        """
        outcome = BulkOutcome()
        wanted = set(record_ids)
        targets = [o for o in self.records if o.id in wanted and o.deleted_at is None]
        outcome.missing = len(wanted) - len(targets)
        new_kind = kind_of(path_of(kind)) if kind is not None else None
        now = now_ms()
        rows = []
        for o in targets:
            values: dict[str, Any] = {}
            if new_kind is not None and new_kind != o.kind:
                values["kind"] = new_kind
            if activity is not None and o.present and activity.strip() != o.activity:
                values["activity"] = activity.strip()
            elif activity is not None and not o.present:
                outcome.absent += 1
            if confidence is not None and confidence.strip() != o.confidence:
                values["confidence"] = confidence.strip()
            if quantity is not None and o.present and not o.unseen and kept_quantity(quantity) != kept_quantity(o.quantity):
                values["quantity"] = kept_quantity(quantity)
            elif quantity is not None and (not o.present or o.unseen):
                outcome.uncounted += 1
            if values:
                rows.append(replace(o, **values, updated_at=now))
            else:
                outcome.unchanged += 1
        if not rows:
            self._say(outcome.text())
            return outcome
        by = self.observer()
        rows = [replace(o, updated_by=by) for o in rows]
        try:
            self.store.save(rows)
            saved = rows
        except Exception as error:  # noqa: BLE001 (told; the part that went through is kept)
            done = getattr(self.store, "last_saved", set())
            saved = [o for o in rows if o.id in done]
            outcome.conflicts = len(getattr(self.store, "last_conflicts", set()) & {o.id for o in rows})
            outcome.failed = len(rows) - len(saved)
            outcome.error = _error_text(error)
        with self.lock:
            by_id = {o.id: o for o in saved}
            self.records = [by_id.get(o.id, o) for o in self.records]
        outcome.saved = saved
        self._say(outcome.text())
        return outcome

    def move_many(self, moves: Mapping[str, tuple]) -> "BulkOutcome":
        """
        Places several records at once (クイック幾何補正: an image's records put
        where its corrected image shows them), in one save: {record id: (lon, lat)}.
        Their pixels are found again from the image as it lies now (the same
        pixels, the image's correction having moved them). The store's checks
        hold, as 一括編集's.
        """
        outcome = BulkOutcome()
        targets = [o for o in self.records if o.id in moves and o.deleted_at is None]
        outcome.missing = len(moves) - len(targets)
        now = now_ms()
        rows = []
        for o in targets:
            lon, lat = moves[o.id]
            if (o.lon, o.lat) == (lon, lat):
                outcome.unchanged += 1
            else:
                rows.append(replace(o, lon=float(lon), lat=float(lat), updated_at=now))
        if not rows:
            return outcome
        by = self.observer()
        rows = self._named(self._placed([replace(o, updated_by=by) for o in rows]))
        try:
            self.store.save(rows)
            saved = rows
        except Exception as error:  # noqa: BLE001 (told; the part that went through is kept)
            done = getattr(self.store, "last_saved", set())
            saved = [o for o in rows if o.id in done]
            outcome.conflicts = len(getattr(self.store, "last_conflicts", set()) & {o.id for o in rows})
            outcome.failed = len(rows) - len(saved)
            outcome.error = _error_text(error)
        saved = self._kept(saved)
        with self.lock:
            by_id = {o.id: o for o in saved}
            self.records = [by_id.get(o.id, o) for o in self.records]
        outcome.saved = saved
        return outcome

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
            self._say(f"削除に失敗: {_error_text(error)}")
            return False
        with self.lock:
            ids = {o.id for o in rows}
            kept = {o.id: o for o in marked}
            self.records = [kept.get(o.id, o) for o in self.records if o.id not in ids or o.id in kept]
        self._say(f"{len(rows)} 件の記録を削除")
        return True

    def removes_for_good(self) -> bool:
        """Whether deleting removes the records for good (an Esri layer without soft delete), so they can't be restored."""
        check = getattr(self.store, "removes_for_good", None)
        return bool(check()) if callable(check) else False

    def remove_many(self, record_ids: Sequence[str]) -> "RemoveOutcome":
        """
        Deletes several records at once (選んだ記録の削除), in one go. The store's checks
        hold: a record someone else changed since it was read is left; those that went
        through are taken out (or marked) here, and the rest told.
        """
        outcome = RemoveOutcome()
        wanted = set(record_ids)
        targets = [o for o in self.records if o.id in wanted and o.deleted_at is None]
        outcome.missing = len(wanted) - len(targets)
        if not targets:
            self._say(outcome.text())
            return outcome
        try:
            marked = self.store.remove(targets)
            done = {o.id for o in targets}
        except Exception as error:  # noqa: BLE001 (told; the part that went through is kept)
            done = set(getattr(self.store, "last_removed", set())) & {o.id for o in targets}
            marked = list(getattr(self.store, "last_marked", []))
            outcome.conflicts = len(getattr(self.store, "last_conflicts", set()) & {o.id for o in targets})
            outcome.failed = len(targets) - len(done)
            outcome.error = _error_text(error)
        kept = {o.id: o for o in marked if o.id in done}
        with self.lock:
            self.records = [kept.get(o.id, o) for o in self.records if o.id not in done or o.id in kept]
        outcome.removed = [o for o in targets if o.id in done]
        self._say(outcome.text())
        return outcome

    def restore(self, rows: Iterable[Observation]) -> bool:
        """Brings deleted records back."""
        now = now_ms()
        back = [replace(o, deleted_at=None, updated_at=now) for o in rows]
        return self._save_records(back, f"{len(back)} 件の記録を復元") is not None

    def can_clear(self) -> bool:
        """Whether the store can remove its records for good (the local GeoPackage)."""
        return callable(getattr(self.store, "clear", None)) and callable(getattr(self.area_store, "clear", None))

    def clear(self, deleted_only: bool = False) -> Optional[tuple[int, int]]:
        """
        Removes the records and checked areas for good, with the images listed
        without their file (only the deleted records and areas when asked);
        how many records and areas, None when the store cannot.
        """
        if not self.can_clear():
            self._say(f"{self.store.label} の記録は消去できない")
            return None
        try:
            records = self.store.clear(deleted_only)
            areas = self.area_store.clear(deleted_only)
        except Exception as error:  # noqa: BLE001
            self._say(f"消去に失敗: {_error_text(error)}")
            return None
        with self.lock:
            if deleted_only:
                self.records = [o for o in self.records if o.deleted_at is None]
                self.areas = [a for a in self.areas if a.deleted_at is None]
            else:
                self.records = []
                self.areas = []
                self.listed = []
                self._save_listed()
                self._order()
        what = "削除済みの" if deleted_only else ""
        self._say(f"{self.store.label} の{what}記録 {records} 件・確認範囲 {areas} 件を消去")
        return records, areas

    def read_whole(self, object_ids: Iterable[str]) -> bool:
        """Every record of some things read (a shared store holds more than those read here); False when they could not be."""
        if not self.scoped:
            return True
        try:
            self.fetch_things(object_ids)
            return True
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.store.label} から記録の取得に失敗: {_error_text(error)}")
            return False

    def rename(self, object_id: str, name: str, kind: Optional[str] = None) -> bool:
        """Renames a thing in all its records (and changes its kind, when given)."""
        if not self.read_whole([object_id]):
            return False
        now = now_ms()
        rows = [replace(o, object_name=name, **({"kind": kind} if kind is not None else {}), updated_at=now)
                for o in self.records if o.object_id == object_id]
        return self._save_records(rows, f"モノの名前を「{name}」に変更") is not None

    def merge(self, source: str, into: str) -> bool:
        """Makes `source`'s records `into`'s (the same thing recorded as two)."""
        if source == into:
            return True
        if not self.read_whole([source, into]):
            return False
        target_rows = [o for o in living(self.records) if o.object_id == into]
        if not target_rows:
            return False
        target = flow_of(target_rows)
        now = now_ms()
        rows = [replace(o, object_id=into, object_name=target.name, kind=target.kind or o.kind, updated_at=now)
                for o in self.records if o.object_id == source]
        return self._save_records(rows, f"{len(rows)} 件の記録を「{target.name}」に統合") is not None

    def carry(self, object_id: str, present: bool = True, unseen: str = "", **changes: Any) -> Optional[Observation]:
        """Records a carried-over thing in this image: still there (with changes), gone, or 不可 (`unseen`: why)."""
        image = self.current
        c = next((x for x in self.carried() if x.source.object_id == object_id), None)
        if image is None or c is None:
            return None
        unseen = unseen.strip()
        if unseen:
            present = False
        record = carried_into(c.source, image.ref, now_ms(), **{"present": present, **({} if present else {"activity": ""}),
                                                                 "unseen": unseen, **changes})
        what = unseen_label(unseen) if unseen else "継続" if present else "消失"
        saved = self._save_records([record], f"{record.object_name} を{what}として記録")
        return saved[0] if saved else None

    def carry_all(self, only: Optional[Callable[[CarryOver], bool]] = None) -> list[Observation]:
        """
        Records every carried-over thing as still there in this image (but those in a checked area, which were not);
        with `only`, just the ones it keeps (those the list shows).
        """
        image = self.current
        carried = [c for c in self.carried() if only is None or only(c)]
        inside = self.inside_carried(carried)
        hidden = self.unseen_carried(carried)
        rows = [c for c in carried if c not in inside and c not in hidden]
        if image is None or not rows:
            return []
        now = now_ms()
        made = [carried_into(c.source, image.ref, now) for c in rows]
        left = [f"確認範囲内の {len(inside)} 件"] * bool(inside) + [f"不可範囲内の {len(hidden)} 件"] * bool(hidden)
        note = f"（{'・'.join(left)}を除く）" if left else ""
        return self._save_records(made, f"{len(made)} のモノを継続として記録{note}") or []

    def absent_all(self, only: Optional[Callable[[CarryOver], bool]] = None) -> list[Observation]:
        """
        Records every carried-over thing as gone in this image (those in a checked area too, but
        those in a 不可範囲: what could not be seen is not 消失); with `only`, just the ones it
        keeps (those the list shows).
        """
        image = self.current
        carried = [c for c in self.carried() if only is None or only(c)]
        hidden = self.unseen_carried(carried)
        rows = [c for c in carried if c not in hidden]
        if image is None or not rows:
            return []
        now = now_ms()
        made = [carried_into(c.source, image.ref, now, present=False, activity="") for c in rows]
        note = f"（不可範囲内の {len(hidden)} 件を除く）" if hidden else ""
        return self._save_records(made, f"{len(made)} のモノを消失として記録{note}") or []

    def absent_inside(self, only: Optional[Callable[[CarryOver], bool]] = None) -> list[Observation]:
        """Records the things carried over into a checked area as gone in this image (with `only`, just the ones it keeps)."""
        image = self.current
        rows = [c for c in self.inside_carried() if only is None or only(c)]
        if image is None or not rows:
            return []
        now = now_ms()
        made = [carried_into(c.source, image.ref, now, present=False, activity="") for c in rows]
        return self._save_records(made, f"{len(made)} のモノを消失として記録（確認範囲内）") or []

    def mark_unseen(self, only: Optional[Callable[[CarryOver], bool]] = None, quiet: bool = False) -> list[Observation]:
        """
        Records the things carried over into a 不可範囲 as 不可 in this image, each with its
        area's reason (with `only`, just the ones it keeps). `quiet`: nothing said when saved.
        """
        image = self.current
        rows = [c for c in self.unseen_carried() if only is None or only(c)]
        if image is None or not rows:
            return []
        now = now_ms()
        made = [carried_into(c.source, image.ref, now, present=False, activity="",
                             unseen=self.unseen_reason_at(c.source.lon, c.source.lat) or UNSEEN) for c in rows]
        saved = self._save_records(made, "" if quiet else f"{len(made)} のモノを不可として記録（不可範囲内）")
        return saved or []

    # ---- checked areas -------------------------------------------------

    def add_area(self, ring: Sequence[Sequence[float]], kind: str = "", note: str = "", image: Optional[str] = None,
                 unseen: str = "") -> Optional[CheckedArea]:
        """
        Records a checked area on an image (this one unless given): its outline (longitude / latitude), the kind looked for ('' all).
        `unseen`: a 不可範囲 instead (why: 雲・霞…), of every kind; the things carried over into it on this image are
        recorded 不可 at once (tsudo 2026-11-19, 案A).
        """
        unseen = unseen.strip()
        if unseen and not self.keeps_unseen_areas():
            self._say("この保存先では不可範囲を記録できない（管理者に連絡: UNSEEN_REASON）［E704］")
            return None
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
                           kind="" if unseen else kind_of(path_of(kind)), note=note.strip(), created_at=now, updated_at=now, deleted_at=None,
                           recorded_by=self.observer(), unseen=unseen, satellite=info.satellite)
        what = "不可範囲" if unseen else "確認範囲"
        try:
            self.area_store.save([area])
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.area_store.label} への{what}の保存に失敗: {_error_text(error)}")
            return None
        area = self._kept_areas([area])[0]
        with self.lock:
            self.areas = [*self.areas, area]
        here = self.current is not None and info.id == self.current.id
        if unseen:
            # The things that may be there: 不可 in this image (not left waiting as 前後の画像のモノ).
            marked = self.mark_unseen(lambda c: bool(covering_areas([area], info.id, c.source.lon, c.source.lat, "", unseen=True)),
                                      quiet=True) if here else []
            self._say(f"{area_label(area)}を記録{f'（中の前後の画像のモノ {len(marked)} 件を不可として記録）' if marked else ''}")
            return area
        missing = len(self.inside_carried()) if here else 0
        self._say(f"{area_label(area)}を記録{f'（範囲内で記録のない前後の画像のモノ {missing} 件）' if missing else ''}")
        return area

    def add_areas(self, rings: Sequence[tuple], image: Optional[str] = None) -> Optional[list[CheckedArea]]:
        """
        Records several checked areas on an image in one save: each (outline, kind, note), as add_area takes them, and
        (outline, kind, note, AiOrigin) for one recording an AI result as wrong (誤検出).
        """
        info = self._image(image)
        if info is None:
            return None
        missing = self.ai_missing(areas=True, ask=True) if any(len(spec) > 3 and spec[3] is not None for spec in rings) else []
        if missing:
            self._say(f"{self.area_store.label} には AI の誤検出を記録できない（管理者に連絡: 列 {'・'.join(missing)} を追加）［E704］")
            return None
        now = now_ms()
        made = []
        for spec in rings:
            ring, kind, note = spec[0], spec[1], spec[2]
            ai: Optional[AiOrigin] = spec[3] if len(spec) > 3 else None
            points = [[float(p[0]), float(p[1])] for p in ring]
            if len(points) < 3:
                continue
            if points[0] != points[-1]:
                points.append(list(points[0]))
            made.append(CheckedArea(id=new_area_id(), image_id=info.id, image_name=info.name, image_time=info.time, ring=points,
                                    kind=kind_of(path_of(kind)), note=note.strip(), created_at=now, updated_at=now, deleted_at=None,
                                    recorded_by=self.observer(), satellite=info.satellite, ai=ai))
        if not made:
            return []
        try:
            self.area_store.save(made)
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.area_store.label} への確認範囲の保存に失敗: {_error_text(error)}")
            return None
        made = self._kept_areas(made)
        with self.lock:
            self.areas = [*self.areas, *made]
        self._say(f"確認範囲 {len(made)} 件を記録")
        return made

    def repoint_ai(self, moves: Sequence[tuple[Optional[int], str, str, str]], tables: Iterable[str] = (), layer: str = "") -> int:
        """
        The records (and 誤検出 checked areas) made from features of a detection layer before they were sent to a
        `detectionOutputs` table, pointed at the table's rows: each move (検出日時, feature id, the table's label, its
        OBJECTID there). `tables`: the labels of the tables (an origin naming one is a row already); `layer`: the
        detection layer's name (a run without a time). One save each; how many were pointed (2026-11-27).
        """
        tables = set(tables)
        now = now_ms()

        def moved(items: Iterable[Any]) -> list:
            out = []
            for x in items:
                if x.deleted_at is not None or x.ai is None:
                    continue
                to = next(((label, oid) for at, fid, label, oid in moves if of_layer_run(x.ai, fid, at, tables, layer)), None)
                if to is not None:
                    out.append(replace(x, ai=replace(x.ai, layer=to[0], result_id=str(to[1])), updated_at=now))
            return out

        records, areas = moved(self.records), moved(self.areas)
        done = 0
        if records and not self.ai_missing(ask=True):
            saved = self._save_records(records, "")
            done += len(saved or [])
        if areas and not self.ai_missing(areas=True, ask=True):
            try:
                self.area_store.save(areas)
            except Exception as error:  # noqa: BLE001
                self._say(f"{self.area_store.label} への確認範囲の保存に失敗: {_error_text(error)}")
            else:
                kept = {a.id: a for a in self._kept_areas(areas)}
                with self.lock:
                    self.areas = [kept.get(a.id, a) for a in self.areas]
                done += len(kept)
        if done:
            self._say(f"転記先の ID に付け替え: {done} 件")
        return done

    def add_box_area(self, box: Sequence[float], kind: str = "", note: str = "") -> Optional[CheckedArea]:
        return self.add_area(box_ring(box), kind, note)

    def remove_areas(self, rows: Iterable[CheckedArea]) -> bool:
        """
        Deletes checked areas (kept, marked, where the store does). A 不可範囲 takes its 不可 records
        with it (those of its image inside it that no other 不可範囲 covers): they were its.
        """
        rows = list(rows)
        if not rows:
            return True
        what = "不可範囲" if all(a.unseen for a in rows) else "確認範囲"
        try:
            marked = self.area_store.remove(rows)
        except Exception as error:  # noqa: BLE001
            self._say(f"{what}の削除に失敗: {_error_text(error)}")
            return False
        with self.lock:
            ids = {a.id for a in rows}
            kept = {a.id: a for a in marked}
            self.areas = [kept.get(a.id, a) for a in self.areas if a.id not in ids or a.id in kept]
        gone = self._unseen_records_of(rows)
        if gone and self.remove(gone):
            self._say(f"{what} {len(rows)} 件を削除（中の不可の記録 {len(gone)} 件も削除）")
        else:
            self._say(f"{what} {len(rows)} 件を削除")
        return True

    def _unseen_records_of(self, areas: list[CheckedArea]) -> list[Observation]:
        """The living 不可 records inside deleted 不可範囲 (on their image) that no living one covers."""
        out = []
        for a in areas:
            if not a.unseen:
                continue
            for o in living(self.records):
                if (o.unseen and o.image_id == a.image_id and area_contains(a, o.lon, o.lat)
                        and not covering_areas(self.areas, o.image_id, o.lon, o.lat, "", unseen=True) and o not in out):
                    out.append(o)
        return out

    def restore_areas(self, rows: Iterable[CheckedArea]) -> bool:
        now = now_ms()
        back = [replace(a, deleted_at=None, updated_at=now) for a in rows]
        try:
            self.area_store.save(back)
        except Exception as error:  # noqa: BLE001
            self._say(f"確認範囲の復元に失敗: {_error_text(error)}")
            return False
        with self.lock:
            by_id = {a.id: a for a in back}
            self.areas = [by_id.get(a.id, a) for a in self.areas]
        image = self.current
        hidden = [a for a in back if a.unseen and image is not None and a.image_id == image.id]
        marked = self.mark_unseen(lambda c: bool(covering_areas(hidden, image.id, c.source.lon, c.source.lat, "", unseen=True)),
                                  quiet=True) if hidden else []
        what = "不可範囲" if all(a.unseen for a in back) else "確認範囲"
        self._say(f"{what} {len(back)} 件を復元" + (f"（中の前後の画像のモノ {len(marked)} 件を不可として記録）" if marked else ""))
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

    def activities_for(self, kind: str) -> tuple[list[str], list[str]]:
        """
        The activities offered for a kind: those of the nearest class on its
        path that names some, with that class; else the vocabulary's, with [].
        """
        own, at = activities_at(self.taxonomy(), path_of(kind))
        return (own, at) if own else (list(self.vocabulary.activities), [])

    def activity_words(self, kind: str) -> list[str]:
        """The activities offered for a kind, then those its records (under the same class) have used."""
        offered, at = self.activities_for(kind)
        used = [o.activity for o in self.living() if o.activity and (not at or within(o.kind, at))]
        return list(dict.fromkeys([*offered, *used]))

    def kept_taxonomy(self) -> list[Taxon]:
        return list(self._taxonomy)

    def _save_taxonomy(self, taxa: list[Taxon], records: list[Observation], done: str) -> bool:
        try:
            self.taxonomy_store.save(taxa)
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.taxonomy_store.label} への分類の保存に失敗: {_error_text(error)}")
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
        known individuals, specifications, photos and activities. Its new path, or None.
        """
        path = list(path)
        taxa = self.taxonomy()
        if find(taxa, path) is None:
            self._say(f"分類「{kind_of(path)}」なし")
            return None
        import re

        new_name = re.sub(r"\s*[>›＞]\s*", " ", (name or "").strip()) or path[-1]
        to = [*(list(parent) if parent is not None else path[:-1]), new_name]
        bad = set(own) - {"levels", "name_label", "members", "specs", "photos", "activities"}
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
        if moved and not self._under(path):
            return None
        now = now_ms()
        records = [replace(o, kind=rewrite(o.kind, path, to), updated_at=now) for o in self.records if within(o.kind, path)] if moved else []
        if moved:
            done = f"分類「{kind_of(path)}」を「{kind_of(to)}」に変更（記録 {len(records)} 件）"
        elif reordered:
            done = f"分類「{kind_of(path)}」の順序を変更"
        else:
            done = f"分類「{kind_of(path)}」を変更"
        lacks = getattr(self.taxonomy_store, "lacks", None)
        missing = lacks("activities") if own.get("activities") and lacks else None
        if missing:
            done += f"（分類のテーブルに属性 {missing} がなく、活動は未保存）"
        return to if self._save_taxonomy(taxa, records, done) else None

    def remove_class(self, path: Sequence[str]) -> bool:
        """Removes a class and its branch; the records under it keep the class above."""
        path = list(path)
        if not self._under(path):
            return False
        now = now_ms()
        up = path[:-1]
        records = [replace(o, kind=kind_of(up), updated_at=now) for o in self.records if within(o.kind, path)]
        note = f"（記録 {len(records)} 件は「{kind_of(up) or '種別なし'}」に）" if records else ""
        return self._save_taxonomy(remove_taxon(self.taxonomy(), path), records, f"分類「{kind_of(path)}」を削除{note}")

    # ---- known individuals (既知の個体) -----------------------------------

    @property
    def members_kept(self) -> bool:
        """Whether what known individuals say of themselves (別名・諸元・参考写真・備考) is kept by the classification's
        store (else their names only: an Esri store needs `taxonomyTable.members`)."""
        return bool(getattr(self.taxonomy_store, "keeps_members", False))

    def note_words(self) -> tuple:
        """The known individuals' names and other names, offered in 備考 besides the glossary's words (glossary.member_terms;
        the same tuple while the classification is the same)."""
        taxa = self._taxonomy
        if self._note_words[0] is not taxa:
            from .glossary import member_terms

            self._note_words = (taxa, member_terms(taxa))
        return self._note_words[1]

    def find_member(self, name: str) -> Optional[MemberHit]:
        """The known individual a name (its own, or one of its other names) stands for."""
        return find_member(self.taxonomy(), name, self.spec_fields)

    def _member_edit(self, change: Callable[[list[Taxon]], list[Taxon]], done: str, action: str, target: str,
                     records: Sequence[Observation] = ()) -> bool:
        try:
            taxa = change(self.taxonomy())
        except ValueError as error:
            self._say(str(error))
            return False
        if not self._save_taxonomy(taxa, list(records), done):
            return False
        from .. import audit

        audit.record("edit", action, self.taxonomy_store.label, count=1, detail={"member": target})
        return True

    def add_members(self, path: Sequence[str], names: Iterable[str]) -> list[str]:
        """Adds known individuals to a class (names it has already are left); the names added."""
        path = list(path)
        t = find(self.taxonomy(), path)
        if t is None:
            self._say(f"分類「{kind_of(path)}」なし")
            return []
        have = set(t.members or [])
        added = [n for n in dict.fromkeys(segment_name(n) for n in names) if n and n not in have]
        if not added:
            self._say("追加する個体なし（同じ名前の個体はこの分類にある）")
            return []
        elsewhere = [n for n in added if (hit := self.find_member(n)) is not None and kind_of(hit.path) != kind_of(path)]
        note = f"（{'・'.join(elsewhere[:3])} は別の分類にも同じ名前・別名あり）" if elsewhere else ""
        ok = self._member_edit(lambda taxa: _tx.add_members(taxa, path, added),
                               f"分類「{kind_of(path)}」に個体 {len(added)} 件を追加{note}", "taxonomy-member-add", "、".join(added))
        return added if ok else []

    def update_member(self, path: Sequence[str], name: str, new_name: Optional[str] = None, specs: Optional[dict[str, str]] = None,
                      photos: Optional[list[str]] = None, note: Optional[str] = None) -> Optional[str]:
        """
        Changes a known individual: its name (records written with the old
        name keep it, and it becomes one of the individual's 別名 so they still
        find it), its specifications (別名 among them), photos and note
        (each given replaces; None keeps). Its name after, or None.
        """
        path = list(path)
        t = find(self.taxonomy(), path)
        if t is None or name not in (t.members or []):
            self._say(f"個体「{name}」が分類「{kind_of(path)}」にない")
            return None
        to = segment_name(new_name) if new_name is not None else name
        if not to:
            self._say("個体の名前が空")
            return None
        used = to != name and any(o.object_name == name for o in self.records if o.deleted_at is None)

        def change(taxa: list[Taxon]) -> list[Taxon]:
            if specs is not None or photos is not None or note is not None:
                taxa = _tx.update_member(taxa, path, name, specs, photos, note)
            if to != name:
                taxa = _tx.rename_member(taxa, path, name, to, keep_old=used)
            return taxa

        done = (f"個体「{name}」を「{to}」に変更" + ("（記録の古い名前は別名に）" if used else "")) if to != name else f"個体「{name}」を変更"
        if not self.members_kept and (specs or photos or (note or "").strip()):
            done += f"（{self.taxonomy_store.label}には個体の表がなく、別名・諸元などは未保存）"
        return to if self._member_edit(change, done, "taxonomy-member", to) else None

    def move_member(self, path: Sequence[str], name: str, dst: Sequence[str], before: Optional[str] = None) -> bool:
        """Moves a known individual to another class (or to another place among its class's, `before` one of them; None:
        last). Its records keep their kinds."""
        path, dst = list(path), list(dst)
        where = f"「{kind_of(dst)}」に移動" if kind_of(dst) != kind_of(path) else "の順序を変更"
        return self._member_edit(lambda taxa: _tx.move_member(taxa, path, name, dst, before), f"個体「{name}」{where}",
                                 "taxonomy-member-move", name)

    def remove_member(self, path: Sequence[str], name: str) -> bool:
        """Removes a known individual (records written with its name keep it)."""
        path = list(path)
        return self._member_edit(lambda taxa: _tx.remove_member(taxa, path, name), f"個体「{name}」を削除", "taxonomy-member-remove", name)

    def _under(self, path: Sequence[str]) -> bool:
        """Every record of a class (and below) read, before they follow it to another (a shared store); False when they could not be."""
        if not self.scoped:
            return True
        try:
            self.fetch(RecordQuery(kinds=(kind_of(list(path)),)))
            return True
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.store.label} から分類「{kind_of(list(path))}」の記録の取得に失敗: {_error_text(error)}")
            return False

    def plan_taxonomy_import(self, request: Any) -> Any:
        """
        「分類体系の取り込み」's dry run (a taxonomy_import.ImportRequest): the
        classification read again (others may have changed it) and, for a
        置き換え, the records of the classes it would remove (a shared store),
        then what the import would do (an ImportPlan; nothing saved). Raises
        what the stores raise, ValueError when the columns make no class.
        """
        from . import taxonomy_import as ti

        if request.mode == ti.REPLACE and not self.scoped and not self.loaded:
            self.reload()  # a local store's records (the classes they use are kept)
        self._read_taxonomy()
        if request.mode != ti.REPLACE:
            return ti.plan_request(request, self.taxonomy(), self.spec_fields)
        plan = ti.plan_request(request, self.taxonomy(), self.spec_fields, [o.kind for o in self.records])
        if plan.removed() and self.scoped:
            self.fetch(RecordQuery(kinds=tuple(plan.removed_tops())))
            plan = ti.plan_request(request, self.taxonomy(), self.spec_fields, [o.kind for o in self.records])
        return plan

    def import_taxonomy(self, request: Any, tried: Optional[tuple] = None, backup: Optional[Callable[[str], str]] = None) -> Any:
        """
        Takes a table into the classification: planned again as the dry run
        does; nothing saved when `tried` (the dry run's ImportPlan.signature())
        differs (the classification changed in between), or when `backup`
        (given the classification as it was, as JSON; returns where it kept
        it) fails. The plan saved (or found with nothing to change), else None (the reason said).
        """
        from . import taxonomy_import as ti

        try:
            plan = self.plan_taxonomy_import(request)
        except ValueError as error:
            self._say(str(error))
            return None
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.taxonomy_store.label} から分類の取得に失敗: {_error_text(error)}")
            return None
        if tried is not None and plan.signature() != tried:
            self._say("試行のあとで分類体系が変化（ほかの人の編集など）。もう一度試行")
            return None
        if not plan.changed:
            self._say(f"表から取り込む変更なし（{plan.summary()}）")
            return plan
        kept = ""
        if backup is not None:
            try:
                kept = backup(ti.backup_text(self.taxonomy()))
            except Exception as error:  # noqa: BLE001
                self._say(f"取り込む前の分類体系の控えを保存できないため、取り込みは中止: {_error_text(error)}")
                return None
        if plan.spec_fields is not None and not self.set_spec_fields(plan.spec_fields):
            return None
        done = f"表から取り込み: {plan.summary()}"
        lacks = getattr(self.taxonomy_store, "lacks", None)
        missing = lacks("activities") if lacks and ti.Use(ti.ACTIVITIES) in request.uses else None
        if missing:
            done += f"（分類のテーブルに属性 {missing} がなく、活動は未保存）"
        if kept:
            done += f"（取り込む前の分類体系の控え: {kept}）"
        if not self._save_taxonomy(plan.taxa, [], done):
            return None
        from .. import audit

        n = plan.counts()
        audit.record("edit", "taxonomy-import", self.taxonomy_store.label, count=n[ti.NEW] + n[ti.CHANGED] + n[ti.REMOVED],
                     detail={"source": request.source, "mode": request.mode, "summary": plan.summary(), "backup": kept})
        return plan

    def set_spec_fields(self, fields: list[SpecField]) -> bool:
        """Changes the specification fields (the values of a field taken away stay in the classes)."""
        try:
            self.taxonomy_store.save_fields(fields)
        except Exception as error:  # noqa: BLE001
            self._say(f"{self.taxonomy_store.label} への諸元の項目の保存に失敗: {_error_text(error)}")
            return False
        self.spec_fields = list(fields)
        self._say(f"諸元の項目 {len(fields)} 件を設定")
        return True

    # ---- the report ----------------------------------------------------

    def report_query(self, options: ReportOptions) -> RecordQuery:
        """What a report reads: the records of its period and class."""
        return RecordQuery(kinds=(options.kind,) if options.kind else (), start=options.start, end=options.end, living=True)

    def read_for_report(self, options: ReportOptions) -> None:
        """Reads what a report holds (a shared store: the records and checked areas of its period; in a task). Raises what the store raises."""
        if self.scoped:
            self.fetch(self.report_query(options))
            self.fetch_areas(start=options.start, end=options.end)
        elif not self.loaded:
            self.reload()

    def report_selection(self, options: ReportOptions) -> ReportSelection:
        """The report's records, things and areas among those read (read_for_report first)."""
        return select_for_report(self.records, self.areas, options, self.confidences())

    def report(self, options: ReportOptions, map_url: Optional[str] = None) -> str:
        """The report (observation-report.ts) as one HTML page."""
        selection = self.report_selection(options)
        return report_html(selection, options, ReportContext(self.store.label, self.observer(), now_ms(), self.confidences(), map_url))
