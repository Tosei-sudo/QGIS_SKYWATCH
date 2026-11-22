"""
The observations for scripts (the viewer's script/observations.ts), as
`plugin.api` gives them to the Python console and Processing scripts: the
records, things (with their flows), images and checked areas read; records
and checked areas added, changed and deleted; the candidates of the same
thing at a place; the report. Records come out as dicts with ISO times, as
the viewer's scripts see them.

`run(description, work)` runs what asks the store (the QGIS side: at once
for the local GeoPackage, in a task for an Esri store, waiting for it). The
viewer asks once a run before a script writes; here the person runs the
code in their own console, so it does not.

A shared (Esri) store is not read whole (scope.py, 2026-11-18): what a
function needs is asked of it (the records of an image, a thing, a class or
a period); `observations()` with nothing to limit it is refused there.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

from .areas import CheckedArea, box_ring
from .book import ObservationBook
from .flows import ObjectFlow, living, time_key
from .geo import js_round
from .image_time import parse_time
from .model import Observation
from .report import ReportOptions
from .scope import RecordQuery
from .taxonomy import kind_of, path_of, within
from ..door import lazy

# What the rest of the plugin imports of 観測記録 (its door, docs/architecture.md R2).
if TYPE_CHECKING:  # the door (core/door.py): each name is imported from its module when first taken
    from . import place_cache, record_import, taxonomy_import
    from .alert_mail import build_mail
    from .alerts import AlertSettings, load_geofences, load_named_areas, LogRow, Moment, poll_log
    from .areas import area_label
    from .assignments import Assignment, progress_of, progress_text, same_person, STATUSES, update, with_status, work_text
    from .book import FormValues, joined_image, ObservedImage
    from .csv import csv_file_name, local_time, observations_csv
    from .drafts import KIND, OutboxDrafts
    from .export import (
        BASES, ExportContext, ExportFilter, ExportSettings, FENCE_SOURCES, items_of, PLACE_SOURCES, render, select_records,
        settings_for,
    )
    from .flows import CarryOver, ends_of, flow_of, flows, heading_text, MatchCandidate, now_ms, outline_of, side_points, two_point
    from .geo import bearing, get_distance, km_text
    from .glossary import Glossary, load_glossary, NO_GLOSSARY, Suggestion, Variant
    from .image_time import image_time
    from .mine import is_mine, my_names
    from .model import ListedImage, state_text, UNSEEN, unseen_label
    from .offline_stores import OfflineAreaStore, OfflineObservationStore, upload_drafts
    from .output import observation_layers, ObservationLayer
    from .repository import AlertLogRepository, AssignmentRepository
    from .place_cache import areas_at, points_of
    from .record_import import SourceRow
    from .report import report_file_name, ReportSelection
    from .satellites import clean, load_satellite_choices, NO_CHOICES, satellite_in_metadata, SatelliteChoices
    from .scope import merged
    from .setup import LOCAL_LABEL, store_sets, StoreSet
    from .similar import ANGLES_ALL, ANGLES_NEAR, Cancelled, find_similar
    from .stamps import default_count, entry_of, Layout, MOST, Sample, sample_of, sample_text, StampEntry
    from .stores import is_local_photo, LocalObservationStore, LocalPhotoStore, photo_url
    from .taxonomy import (
        activities_at, children_of, class_by_name, find, level_name, member_index, name_label_of, nest, photos_at, spec_text,
        SpecField, specs_at, Taxon,
    )
    from .alerts import (
        AlertItem, OBJECT_LAYERS, select as alert_select, write_file as alert_write_file, build_lines, history_query,
        layer_key_problems, load_rules, manual_items, name_places, object_keys, report_text, reported_ids, score_note,
        uses_source,
    )
    from .assignments import (
        DONE, UNWORKED, roots_text as assignment_roots_text, holder_of, holders_of, is_shared, own_row, roots_by_person,
        work_state,
    )
    from .continuation import AUTO, CANDIDATE, Continuation, Found as ContinuationFound, continuations
    from .export import file_name_of
    from .handover import (
        HOUR_MS, HandoverData, handover_text, write_file as handover_write_file, keep_memo, last_memo, open_requests,
    )
    from .image_time import ORIGINS as IMAGE_TIME_ORIGINS
    from .mine import (
        DEFAULT_DAYS, ImageRow, MineFilter, by_image, changed_by_other, roots_text as mine_roots_text,
        select as mine_select, observer_names, root_of, since_of,
    )
    from .satellites import ORIGINS as SATELLITE_ORIGINS
    from .similar import Found as SimilarFound, gray_of, half_sizes

__getattr__, __dir__ = lazy(__name__)


def _iso(t: Optional[float]) -> Optional[str]:
    if t is None or not math.isfinite(t):
        return None
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(t) % 1000:03d}Z"


def _num(v: Any, name: str, low: float = -math.inf, high: float = math.inf) -> float:
    try:
        n = float(v) if isinstance(v, (int, float, str)) and not isinstance(v, bool) and str(v).strip() != "" else None
    except ValueError:
        n = None
    if n is None or not math.isfinite(n):
        raise TypeError(f"{name} は数値で指定")
    if n < low or n > high:
        raise ValueError(f"{name} は {low:g}〜{high:g} の範囲で指定")
    return n


def _maybe_num(v: Any, name: str, low: float = -math.inf, high: float = math.inf) -> Optional[float]:
    return None if v is None or v == "" else _num(v, name, low, high)


def _str(v: Any, name: str) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise TypeError(f"{name} は文字列で指定")
    return v


def _time(v: Any, name: str) -> int:
    if isinstance(v, datetime):
        return int((v if v.tzinfo else v.astimezone()).timestamp() * 1000)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return int(v)
    t = parse_time(v) if isinstance(v, str) else None
    if t is None:
        raise TypeError(f"{name} は日時（datetime、ミリ秒、または \"2026-10-20T09:00\" のような文字列）で指定")
    return t


def record_out(o: Observation) -> dict[str, Any]:
    """A record as a script sees it (times as ISO text)."""
    return {
        "id": o.id, "imageId": o.image_id, "imageName": o.image_name, "imageTime": _iso(o.image_time), "objectId": o.object_id,
        "objectName": o.object_name, "kind": o.kind, "activity": o.activity, "present": o.present, "unseen": o.unseen,
        "confidence": o.confidence or "", "note": o.note, "lon": o.lon, "lat": o.lat, "length": o.length, "heading": o.heading, "width": o.width, "recordedBy": o.recorded_by or "",
        "pixelX": o.pixel_x, "pixelY": o.pixel_y, "imageFile": o.image_file or "", "satellite": o.satellite or "",
        "updatedBy": o.updated_by or "", "createdAt": _iso(o.created_at), "updatedAt": _iso(o.updated_at), "deletedAt": _iso(o.deleted_at),
    }


def flow_out(f: ObjectFlow) -> dict[str, Any]:
    last = f.observations[-1]
    return {
        "objectId": f.object_id, "name": f.name, "kind": f.kind, "first": _iso(f.first), "last": _iso(f.last), "seen": f.seen,
        "present": f.present, "activity": f.activity, "unseen": f.unseen, "confidence": last.confidence or "", "distance": js_round(f.distance),
        "lon": last.lon, "lat": last.lat,
        "steps": [{"from": _iso(s.start), "to": _iso(s.end), "present": s.present, "activity": s.activity, "unseen": s.unseen, "images": s.count,
                   "moved": None if s.moved is None else js_round(s.moved)} for s in f.steps],
    }


def area_out(a: CheckedArea) -> dict[str, Any]:
    return {
        "id": a.id, "imageId": a.image_id, "imageName": a.image_name, "imageTime": _iso(a.image_time), "kind": a.kind, "note": a.note,
        "unseen": a.unseen, "satellite": a.satellite or "", "recordedBy": a.recorded_by or "", "polygon": {"type": "Polygon", "coordinates": [a.ring]}, "createdAt": _iso(a.created_at),
        "deletedAt": _iso(a.deleted_at),
    }


def ring_of(bbox: Any = None, polygon: Any = None) -> list[list[float]]:
    """An outline: `bbox` [西, 南, 東, 北], or `polygon` (a GeoJSON Polygon or Feature, or one ring of [経度, 緯度])."""
    if bbox is not None:
        ok = isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox)
        if not ok or bbox[0] > bbox[2] or bbox[1] > bbox[3]:
            raise TypeError("bbox は [西, 南, 東, 北]（経緯度）で指定")
        return box_ring(list(bbox))
    ring = polygon
    if isinstance(ring, dict) and ring.get("type") == "Feature":
        ring = ring.get("geometry")
    if isinstance(ring, dict) and ring.get("type") == "Polygon" and isinstance(ring.get("coordinates"), list):
        ring = ring["coordinates"][0] if ring["coordinates"] else None
    ok = isinstance(ring, (list, tuple)) and len(ring) >= 3 and all(
        isinstance(p, (list, tuple)) and len(p) >= 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p) for p in ring)
    if not ok:
        raise TypeError("範囲は bbox: [西, 南, 東, 北] か polygon（GeoJSON の Polygon、または [[経度, 緯度], …]）で指定")
    if len(ring) > 10_000:
        raise ValueError("polygon の点は 10000 個まで")
    return [[float(p[0]), float(p[1])] for p in ring]


class ObservationApi:
    """The functions (`book()` the chosen store's book; `run(description, work)` → work(book)'s result, raising what it raised)."""

    def __init__(self, book: Callable[[], Optional[ObservationBook]], run: Callable[[str, Callable[[ObservationBook], Any]], Any],
                 report: Optional[Callable[[ReportOptions], str]] = None):
        self._book = book
        self._run = run
        self._report = report

    def _b(self) -> ObservationBook:
        b = self._book()
        if b is None:
            raise RuntimeError("観測記録は使用不可")
        if not b.loaded:
            self._run("観測記録の読み込み", lambda x: x.reload())
        if b.needs_around():
            self._run("観測記録の読み込み", lambda x: x.read_around())
        return b

    def _fetch(self, b: ObservationBook, query: RecordQuery) -> list[Observation]:
        """The records of a query: asked of a shared store (raising what it raised), among those read for a local one."""
        return self._run("観測記録の読み込み", lambda x: x.fetch(query))

    def _record(self, b: ObservationBook, record_id: Any) -> Observation:
        if b.scoped and not any(o.id == record_id for o in b.records):
            self._fetch(b, RecordQuery(ids=(str(record_id),)))
        found = next((o for o in b.records if o.id == record_id and o.deleted_at is None), None)
        if found is None:
            raise LookupError(f"観測記録「{record_id}」なし")
        return found

    def _image(self, b: ObservationBook, image: Optional[str]) -> None:
        if image is not None:
            if not any(i.id == image for i in b.images):
                raise LookupError(f"画像「{image}」なし")
        elif b.current is None:
            raise LookupError("記録する画像なし（画像を開くか image で指定）")

    def _said(self, b: ObservationBook, result: Any, what: str) -> Any:
        if result is None or result is False:
            raise RuntimeError(b.status or f"{what}の保存失敗")
        return result

    # ---- reading -------------------------------------------------------

    def observations(self, image: Optional[str] = None, object: Optional[str] = None, kind: Optional[str] = None,
                     start: Any = None, end: Any = None, deleted: bool = False) -> list[dict[str, Any]]:
        """The records (of an image, a thing by id or name, a class and below, a period; a shared store: at least one of them)."""
        b = self._b()
        path = path_of(kind) if kind is not None else None
        t0 = _time(start, "start") if start is not None else None
        t1 = _time(end, "end") if end is not None else None
        if b.scoped:
            query = RecordQuery(image_ids=(str(image),) if image is not None else (),
                                object_ids=(str(object),) if object is not None else (), names=(str(object),) if object is not None else (),
                                kinds=(kind_of(path),) if path else (), start=t0, end=t1, living=not deleted)
            if query.everything():
                raise ValueError(f"{b.label} の記録は全件は読まない（image・object・kind・start・end のどれかを指定）")
            rows = self._fetch(b, query)
        else:
            rows = b.records
        rows = rows if deleted else living(rows)
        return [record_out(o) for o in rows
                if (image is None or o.image_id == image) and (object is None or object in (o.object_id, o.object_name))
                and (not path or within(o.kind, path)) and (t0 is None or time_key(o) >= t0) and (t1 is None or time_key(o) <= t1)]

    def observed_things(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        """
        The things and their flows (first, last, how their activity went, how far they moved). A shared store:
        those of a class (`kind`, every one asked of it), else those of the records read (around the images opened).
        """
        b = self._b()
        path = path_of(kind) if kind is not None else None
        if b.scoped and path:
            of_kind = self._fetch(b, RecordQuery(kinds=(kind_of(path),), living=True))
            self._run("観測記録の読み込み", lambda x: x.fetch_things(o.object_id for o in of_kind))
        return [flow_out(f) for f in b.flows() if not path or within(f.kind, path)]

    def observation_images(self) -> list[dict[str, Any]]:
        b = self._b()
        return [{"id": i.id, "name": i.name, "time": _iso(i.time), "file": i.file, "current": i is b.current, "bbox": i.box} for i in b.images]

    def observe_image(self, image_id: str) -> bool:
        """Records on this image from now on."""
        if not self._b().go_to(str(image_id)):
            raise LookupError(f"画像「{image_id}」なし（observation_images() で一覧可能）")
        return True

    def confidences(self) -> list[dict[str, Any]]:
        return [{"label": c.label, "color": c.color, "note": c.note} for c in self._b().confidences()]

    def observation_candidates(self, lon: float, lat: float, kind: Optional[str] = None, length: Optional[float] = None,
                               heading: Optional[float] = None, image: Optional[str] = None) -> list[dict[str, Any]]:
        """The things of other images that may be the one at a place (同一候補), likeliest first."""
        b = self._b()
        x, y = _num(lon, "lon", -180, 180), _num(lat, "lat", -90, 90)
        if image is not None:
            self._image(b, image)
            if b.needs_around(image):
                self._run("観測記録の読み込み", lambda x: x.read_around(image))
        found = b.candidates(x, y, _str(kind, "kind") or "", _maybe_num(length, "length", 0), _maybe_num(heading, "heading", -360, 360), image)
        return [{"objectId": c.object_id, "name": c.name, "kind": c.kind, "distance": js_round(c.distance),
                 "score": js_round(c.score * 1000) / 1000, "reasons": list(c.reasons), "record": record_out(c.source)} for c in found]

    def checked_areas(self, image: Optional[str] = None, deleted: bool = False) -> list[dict[str, Any]]:
        """The checked areas (of an image; a shared store without one: those read, of the images opened)."""
        b = self._b()
        if b.scoped and image is not None:
            self._run("確認範囲の読み込み", lambda x: x.fetch_areas(image_ids=[str(image)]))
        return [area_out(a) for a in b.areas if (deleted or a.deleted_at is None) and (image is None or a.image_id == image)]

    # ---- writing -------------------------------------------------------

    def add_observation(self, lon: float, lat: float, image: Optional[str] = None, object: Optional[str] = None,
                        kind: Optional[str] = None, activity: Optional[str] = None, confidence: Optional[str] = None,
                        note: Optional[str] = None, present: bool = True, length: Optional[float] = None,
                        heading: Optional[float] = None, width: Optional[float] = None) -> dict[str, Any]:
        """Records a thing at a place on this image (or `image`); the kind, activity and 確度 not given are the last ones."""
        b = self._b()
        self._image(b, _str(image, "image"))
        levels = [c.label for c in b.confidences()]
        if confidence and confidence not in levels:
            raise ValueError(f"確度「{confidence}」なし（{'・'.join(levels)}）")
        x, y = _num(lon, "lon", -180, 180), _num(lat, "lat", -90, 90)
        size, turn, across = _maybe_num(length, "length", 0), _maybe_num(heading, "heading", -360, 360), _maybe_num(width, "width", 0)
        record = self._run("観測記録の追加", lambda bk: bk.add(
            x, y, image=image, object=_str(object, "object") or "", kind=_str(kind, "kind"), activity=_str(activity, "activity"),
            confidence=confidence, note=_str(note, "note") or "", present=bool(present), length=size, heading=turn,
            width=across))
        return record_out(self._said(b, record, "観測記録"))

    def update_observation(self, record_id: str, **changes: Any) -> dict[str, Any]:
        """Changes a record: kind, activity, confidence, note, present, lon, lat, length, heading, width (a thing's name: rename_observed_thing)."""
        b = self._b()
        self._record(b, record_id)
        allowed = {"kind", "activity", "confidence", "note", "present", "lon", "lat", "length", "heading", "width"}
        unknown = [k for k in changes if k not in allowed]
        if unknown:
            raise ValueError(f"変更できない項目: {', '.join(unknown)}（モノの名前は rename_observed_thing で）")
        out: dict[str, Any] = {}
        for key in ("kind", "activity", "confidence", "note"):
            if key in changes:
                out[key] = _str(changes[key], key) or ""
        if "present" in changes:
            out["present"] = bool(changes["present"])
        if "lon" in changes:
            out["lon"] = _num(changes["lon"], "lon", -180, 180)
        if "lat" in changes:
            out["lat"] = _num(changes["lat"], "lat", -90, 90)
        if "length" in changes:
            out["length"] = _maybe_num(changes["length"], "length", 0)
        if "width" in changes:
            out["width"] = _maybe_num(changes["width"], "width", 0)
        if "heading" in changes:
            h = _maybe_num(changes["heading"], "heading", -360, 360)
            out["heading"] = None if h is None else (h % 360 + 360) % 360
        record = self._run("観測記録の変更", lambda bk: bk.update(record_id, **out))
        return record_out(self._said(b, record, "観測記録"))

    def rename_observed_thing(self, object_id: str, name: str) -> bool:
        b = self._b()
        n = (_str(name, "name") or "").strip()
        if not n:
            raise TypeError("name を指定")
        if b.scoped and not any(o.object_id == object_id for o in b.records):
            self._run("観測記録の読み込み", lambda x: x.fetch_things([str(object_id)]))
        if not any(o.object_id == object_id for o in b.records):
            raise LookupError(f"モノ「{object_id}」なし")
        return self._said(b, self._run("モノの名前の変更", lambda bk: bk.rename(object_id, n)), "名前")

    def delete_observations(self, ids: Any) -> int:
        b = self._b()
        wanted = list(ids if isinstance(ids, (list, tuple)) else [ids])
        known = {o.id for o in b.records}
        if b.scoped and any(i not in known for i in wanted):
            self._fetch(b, RecordQuery(ids=tuple(str(i) for i in wanted if i not in known)))
        rows = [self._record(b, i) for i in wanted]
        self._said(b, self._run("観測記録の削除", lambda bk: bk.remove(rows)), "削除")
        return len(rows)

    def clear_observations(self, deleted_only: bool = False) -> dict[str, int]:
        """Removes the local store's records and checked areas for good (only the deleted ones when asked)."""
        b = self._b()
        if not b.can_clear():
            raise RuntimeError(f"{b.label} の記録は消去不可（ローカルのみ）")
        records, areas = self._said(b, self._run("観測記録の消去", lambda bk: bk.clear(bool(deleted_only))), "消去")
        return {"observations": records, "areas": areas}

    def add_checked_area(self, bbox: Any = None, polygon: Any = None, kind: Optional[str] = None, note: Optional[str] = None,
                         image: Optional[str] = None) -> dict[str, Any]:
        """Records a checked area on this image (or `image`): `bbox` or `polygon`, the kind looked for (None all)."""
        b = self._b()
        self._image(b, _str(image, "image"))
        ring = ring_of(bbox, polygon)
        area = self._run("確認範囲の記録", lambda bk: bk.add_area(ring, _str(kind, "kind") or "", _str(note, "note") or "", image))
        return area_out(self._said(b, area, "確認範囲"))

    def delete_checked_areas(self, ids: Any) -> int:
        """Deletes checked areas (a shared store: of those read, checked_areas(image) first for an image not opened)."""
        b = self._b()
        want = {str(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])}
        rows = [a for a in b.areas if a.id in want and a.deleted_at is None]
        if len(rows) != len(want):
            raise LookupError("確認範囲の ID に該当なし")
        self._said(b, self._run("確認範囲の削除", lambda bk: bk.remove_areas(rows)), "削除")
        return len(rows)

    def import_taxonomy(self, columns: Sequence[str], rows: Sequence[Any], uses: Any = None, mode: str = "add", save: bool = False,
                        fill_down: bool = True, level_names: bool = True, source: str = "",
                        backup: Optional[Callable[[str], str]] = None) -> dict[str, Any]:
        """
        「分類体系の取り込み」: a table (its column names, its rows as lists or
        dicts) into the classification. `uses`: each column's use (a list, or
        {column: use}; 階層 1・既知の個体・諸元: 全長・使わない…), else guessed
        from the names; `mode` "add" (追加・更新) or "replace" (置き換え).
        Tried only (nothing saved) unless `save`. What it does (or did).
        """
        from . import taxonomy_import as ti

        if mode not in (ti.ADD, ti.REPLACE):
            raise ValueError('mode は "add"（追加・更新）か "replace"（置き換え）')
        b = self._b()
        columns = [str(c) for c in columns]
        table: list[ti.TableRow] = []
        for n, r in enumerate(rows, start=2):
            if isinstance(r, ti.TableRow):
                table.append(r)
            elif isinstance(r, dict):
                table.append(ti.TableRow(n, {c: r.get(c) for c in columns}))
            else:
                table.append(ti.TableRow(n, dict(zip(columns, list(r)))))
        if uses is None:
            chosen = ti.guess_uses(columns, b.taxonomy(), b.spec_fields)
        elif isinstance(uses, dict):
            unknown = set(uses) - set(columns)
            if unknown:
                raise ValueError(f"表にない列: {', '.join(sorted(map(str, unknown)))}")
            chosen = [ti.parse_use(uses.get(c), b.spec_fields) for c in columns]
        else:
            uses = list(uses)
            if len(uses) != len(columns):
                raise ValueError(f"uses は列の数（{len(columns)}）だけ")
            chosen = [ti.parse_use(u, b.spec_fields) for u in uses]
        problem = ti.check_uses(chosen)
        if problem:
            raise ValueError(problem)
        request = ti.ImportRequest(columns, table, chosen, mode, fill_down, level_names, source)
        if save:
            plan = self._run("分類体系の取り込み", lambda x: x.import_taxonomy(request, None, backup))
            if plan is None:
                raise RuntimeError(b.status or "分類体系の取り込みの失敗")
        else:
            plan = self._run("分類体系の取り込みの試行", lambda x: x.plan_taxonomy_import(request))
        return {
            "saved": bool(save and plan.changed),
            "summary": plan.summary(),
            "counts": plan.counts(),
            "uses": [u.label for u in chosen],
            "classes": [{"kind": c.kind, "status": c.status, "text": c.text()} for c in plan.classes],
            "errors": [{"row": n, "reason": e} for n, e in plan.errors],
            "message": b.status if save else "",
        }

    def observation_report(self, title: Optional[str] = None, start: Any = None, end: Any = None, kind: Optional[str] = None,
                           confidence: Optional[str] = None, map: bool = True, flows: bool = True, areas: bool = True,
                           records: bool = False, save: Optional[str] = None) -> dict[str, Any]:
        """The report: its HTML (and its counts), saved to `save` (a path) when given."""
        b = self._b()
        options = ReportOptions(
            title=(_str(title, "title") or ReportOptions().title)[:200],
            start=_time(start, "start") if start is not None else None, end=_time(end, "end") if end is not None else None,
            kind=str(kind or ""), confidence=str(confidence or ""), map=bool(map), flows=bool(flows), areas=bool(areas), records=bool(records),
        )
        self._run("報告書の記録の読み込み", lambda x: x.read_for_report(options))
        selection = b.report_selection(options)
        # The QGIS side draws the map; without it, none.
        html = self._report(options) if self._report is not None else b.report(options)
        if save:
            with open(save, "w", encoding="utf-8") as f:
                f.write(html)
        return {"html": html, "things": len(selection.flows), "records": len(selection.records), **({"path": save} if save else {})}


__all__ = [
    "AlertLogRepository", "AssignmentRepository",
    "activities_at", "AlertSettings", "ANGLES_ALL", "ANGLES_NEAR", "area_label", "area_out", "areas_at", "Assignment", "BASES",
    "bearing", "box_ring", "build_mail", "Cancelled", "CarryOver", "CheckedArea", "children_of", "class_by_name", "clean",
    "csv_file_name", "default_count", "ends_of", "entry_of", "ExportContext", "ExportFilter", "ExportSettings",
    "FENCE_SOURCES", "find", "find_similar", "flow_of", "flow_out", "flows", "FormValues", "get_distance", "Glossary",
    "heading_text", "image_time", "is_local_photo", "is_mine", "items_of", "joined_image", "js_round", "KIND", "kind_of",
    "km_text", "Layout", "level_name", "ListedImage", "living", "load_geofences", "load_glossary", "load_named_areas",
    "load_satellite_choices", "LOCAL_LABEL", "local_time", "LocalObservationStore", "LocalPhotoStore", "LogRow",
    "MatchCandidate", "member_index", "merged", "Moment", "MOST", "my_names", "name_label_of", "nest", "NO_CHOICES",
    "NO_GLOSSARY", "now_ms", "ObjectFlow", "Observation", "observation_layers", "ObservationApi", "ObservationBook",
    "ObservationLayer", "observations_csv", "ObservedImage", "OfflineAreaStore", "OfflineObservationStore", "OutboxDrafts",
    "outline_of", "parse_time", "path_of", "photo_url", "photos_at", "place_cache", "PLACE_SOURCES", "points_of", "poll_log",
    "progress_of", "progress_text", "record_import", "record_out", "taxonomy_import", "RecordQuery", "render", "report_file_name",
    "ReportOptions", "ReportSelection", "ring_of", "same_person", "Sample", "sample_of", "sample_text",
    "satellite_in_metadata", "SatelliteChoices", "select_records", "settings_for", "side_points", "SourceRow", "spec_text",
    "SpecField", "specs_at", "StampEntry", "state_text", "STATUSES", "store_sets", "StoreSet", "Suggestion", "Taxon",
    "time_key", "two_point", "UNSEEN", "unseen_label", "update", "upload_drafts", "Variant", "with_status", "within",
    "work_text",
    "AUTO", "AlertItem", "CANDIDATE", "Continuation", "ContinuationFound", "DEFAULT_DAYS", "DONE", "HOUR_MS",
    "HandoverData", "IMAGE_TIME_ORIGINS", "ImageRow", "MineFilter", "OBJECT_LAYERS", "SATELLITE_ORIGINS",
    "SimilarFound", "UNWORKED", "alert_select", "alert_write_file", "assignment_roots_text", "build_lines", "by_image",
    "changed_by_other", "continuations", "file_name_of", "gray_of", "half_sizes", "handover_text",
    "handover_write_file", "history_query", "holder_of", "holders_of", "is_shared", "keep_memo", "last_memo",
    "layer_key_problems", "load_rules", "manual_items", "mine_roots_text", "mine_select", "name_places", "object_keys",
    "observer_names", "open_requests", "own_row", "report_text", "reported_ids", "root_of", "roots_by_person",
    "score_note", "since_of", "uses_source", "work_state",
]
