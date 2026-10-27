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
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .areas import CheckedArea, box_ring
from .book import ObservationBook
from .flows import ObjectFlow, living, time_key
from .geo import js_round
from .image_time import parse_time
from .model import Observation
from .report import ReportOptions
from .taxonomy import path_of, within


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
        "objectName": o.object_name, "kind": o.kind, "activity": o.activity, "present": o.present, "confidence": o.confidence or "",
        "note": o.note, "lon": o.lon, "lat": o.lat, "length": o.length, "heading": o.heading, "recordedBy": o.recorded_by or "",
        "updatedBy": o.updated_by or "", "createdAt": _iso(o.created_at), "updatedAt": _iso(o.updated_at), "deletedAt": _iso(o.deleted_at),
    }


def flow_out(f: ObjectFlow) -> dict[str, Any]:
    last = f.observations[-1]
    return {
        "objectId": f.object_id, "name": f.name, "kind": f.kind, "first": _iso(f.first), "last": _iso(f.last), "seen": f.seen,
        "present": f.present, "activity": f.activity, "confidence": last.confidence or "", "distance": js_round(f.distance),
        "lon": last.lon, "lat": last.lat,
        "steps": [{"from": _iso(s.start), "to": _iso(s.end), "present": s.present, "activity": s.activity, "images": s.count,
                   "moved": None if s.moved is None else js_round(s.moved)} for s in f.steps],
    }


def area_out(a: CheckedArea) -> dict[str, Any]:
    return {
        "id": a.id, "imageId": a.image_id, "imageName": a.image_name, "imageTime": _iso(a.image_time), "kind": a.kind, "note": a.note,
        "recordedBy": a.recorded_by or "", "polygon": {"type": "Polygon", "coordinates": [a.ring]}, "createdAt": _iso(a.created_at),
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
        return b

    def _record(self, b: ObservationBook, record_id: Any) -> Observation:
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
        """The records (of an image, a thing by id or name, a class and below, a period)."""
        b = self._b()
        path = path_of(kind) if kind is not None else None
        t0 = _time(start, "start") if start is not None else None
        t1 = _time(end, "end") if end is not None else None
        rows = b.records if deleted else living(b.records)
        return [record_out(o) for o in rows
                if (image is None or o.image_id == image) and (object is None or object in (o.object_id, o.object_name))
                and (not path or within(o.kind, path)) and (t0 is None or time_key(o) >= t0) and (t1 is None or time_key(o) <= t1)]

    def observed_things(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        """The things and their flows (first, last, how their activity went, how far they moved)."""
        b = self._b()
        path = path_of(kind) if kind is not None else None
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
        found = b.candidates(x, y, _str(kind, "kind") or "", _maybe_num(length, "length", 0), _maybe_num(heading, "heading", -360, 360), image)
        return [{"objectId": c.object_id, "name": c.name, "kind": c.kind, "distance": js_round(c.distance),
                 "score": js_round(c.score * 1000) / 1000, "reasons": list(c.reasons), "record": record_out(c.source)} for c in found]

    def checked_areas(self, image: Optional[str] = None, deleted: bool = False) -> list[dict[str, Any]]:
        b = self._b()
        return [area_out(a) for a in b.areas if (deleted or a.deleted_at is None) and (image is None or a.image_id == image)]

    # ---- writing -------------------------------------------------------

    def add_observation(self, lon: float, lat: float, image: Optional[str] = None, object: Optional[str] = None,
                        kind: Optional[str] = None, activity: Optional[str] = None, confidence: Optional[str] = None,
                        note: Optional[str] = None, present: bool = True, length: Optional[float] = None,
                        heading: Optional[float] = None) -> dict[str, Any]:
        """Records a thing at a place on this image (or `image`); the kind, activity and 確度 not given are the last ones."""
        b = self._b()
        self._image(b, _str(image, "image"))
        levels = [c.label for c in b.confidences()]
        if confidence and confidence not in levels:
            raise ValueError(f"確度「{confidence}」なし（{'・'.join(levels)}）")
        x, y = _num(lon, "lon", -180, 180), _num(lat, "lat", -90, 90)
        size, turn = _maybe_num(length, "length", 0), _maybe_num(heading, "heading", -360, 360)
        record = self._run("観測記録の追加", lambda bk: bk.add(
            x, y, image=image, object=_str(object, "object") or "", kind=_str(kind, "kind"), activity=_str(activity, "activity"),
            confidence=confidence, note=_str(note, "note") or "", present=bool(present), length=size, heading=turn))
        return record_out(self._said(b, record, "観測記録"))

    def update_observation(self, record_id: str, **changes: Any) -> dict[str, Any]:
        """Changes a record: kind, activity, confidence, note, present, lon, lat, length, heading (a thing's name: rename_observed_thing)."""
        b = self._b()
        self._record(b, record_id)
        allowed = {"kind", "activity", "confidence", "note", "present", "lon", "lat", "length", "heading"}
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
        if not any(o.object_id == object_id for o in b.records):
            raise LookupError(f"モノ「{object_id}」なし")
        return self._said(b, self._run("モノの名前の変更", lambda bk: bk.rename(object_id, n)), "名前")

    def delete_observations(self, ids: Any) -> int:
        b = self._b()
        rows = [self._record(b, i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
        self._said(b, self._run("観測記録の削除", lambda bk: bk.remove(rows)), "削除")
        return len(rows)

    def add_checked_area(self, bbox: Any = None, polygon: Any = None, kind: Optional[str] = None, note: Optional[str] = None,
                         image: Optional[str] = None) -> dict[str, Any]:
        """Records a checked area on this image (or `image`): `bbox` or `polygon`, the kind looked for (None all)."""
        b = self._b()
        self._image(b, _str(image, "image"))
        ring = ring_of(bbox, polygon)
        area = self._run("確認範囲の記録", lambda bk: bk.add_area(ring, _str(kind, "kind") or "", _str(note, "note") or "", image))
        return area_out(self._said(b, area, "確認範囲"))

    def delete_checked_areas(self, ids: Any) -> int:
        b = self._b()
        want = {str(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])}
        rows = [a for a in b.areas if a.id in want and a.deleted_at is None]
        if len(rows) != len(want):
            raise LookupError("確認範囲の ID に該当なし")
        self._said(b, self._run("確認範囲の削除", lambda bk: bk.remove_areas(rows)), "削除")
        return len(rows)

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
        selection = b.report_selection(options)
        # The QGIS side draws the map; without it, none.
        html = self._report(options) if self._report is not None else b.report(options)
        if save:
            with open(save, "w", encoding="utf-8") as f:
                f.write(html)
        return {"html": html, "things": len(selection.flows), "records": len(selection.records), **({"path": save} if save else {})}
