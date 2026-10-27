"""
Where observations, checked areas, the classification and its photos are
kept, as the viewer (observations.ts, observation-areas.ts, taxonomy.ts,
taxon-photos.ts): an Esri feature service shared by everyone using it, or
the local GeoPackage (the viewer's 「このブラウザ」) with the same attribute
names.

The Esri stores ask through an EsriClient (its transport injected), so the
tests use a fake server; in QGIS they run inside a QgsTask.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from typing import Any, Iterable, Optional, Protocol

from ..esri import EsriClient, EsriError, fit_value, point_geometry, ring_geometry
from ..gpkg import AREAS, OBSERVATIONS, PHOTOS, TAXONOMY, GeoPackage, datetime_ms, datetime_text, encode_point, encode_polygon
from ..util import is_number, js_string, ms_of
from .areas import AreaStoreConfig, CheckedArea
from .flows import now_ms
from .model import Observation, ObservationStoreConfig
from .taxonomy import (
    FIELDS_ROW, SpecField, Taxon, TaxonomyTableConfig, kind_of, normalize, path_of, spec_fields_of, specs_of, taxa_from_json,
)

#: The label of the local stores (the viewer's 「このブラウザ」).
LOCAL_LABEL = "ローカル"


class ObservationStore(Protocol):
    label: str

    def load(self) -> list[Observation]:
        """Every record (deleted ones too)."""
        ...

    def save(self, records: list[Observation]) -> None:
        """Adds or replaces records."""
        ...

    def remove(self, records: list[Observation], now: Optional[int] = None) -> list[Observation]:
        """Deletes records: marks them (returns them marked), or removes them where the store says so."""
        ...


class AreaStore(Protocol):
    label: str

    def load(self) -> list[CheckedArea]: ...

    def save(self, areas: list[CheckedArea]) -> None: ...

    def remove(self, areas: list[CheckedArea], now: Optional[int] = None) -> list[CheckedArea]: ...


class TaxonomyStore(Protocol):
    label: str

    def load(self) -> Optional[list[Taxon]]:
        """The classification kept, or None when none is kept yet (the configured one applies)."""
        ...

    def save(self, taxa: list[Taxon]) -> None: ...

    def load_fields(self) -> Optional[list[SpecField]]:
        """The specification fields kept, or None when none are kept yet."""
        ...

    def save_fields(self, fields: list[SpecField]) -> None: ...


def _marked(items: list, now: Optional[int]) -> list:
    now = now_ms() if now is None else now
    return [replace(x, deleted_at=now, updated_at=now) for x in items]


# ---------------------------------------------------------------------------
# Local (the GeoPackage in the user profile)


class LocalObservationStore:
    """Observations in the local GeoPackage (`observations`)."""

    label = LOCAL_LABEL

    def __init__(self, gpkg: GeoPackage):
        self.gpkg = gpkg

    def load(self) -> list[Observation]:
        out = []
        for r in self.gpkg.rows(OBSERVATIONS):
            g = r.get("geom")
            if not isinstance(g, tuple) or not r.get("OBS_ID") or not r.get("OBJECT_ID"):
                continue
            created = datetime_ms(r.get("CREATED_AT")) or 0
            out.append(Observation(
                id=r["OBS_ID"], image_id=r.get("IMAGE_ID") or "", image_name=r.get("IMAGE_NAME") or "",
                image_time=datetime_ms(r.get("IMAGE_TIME")), object_id=r["OBJECT_ID"], object_name=r.get("OBJECT_NAME") or "",
                kind=r.get("KIND") or "", activity=r.get("ACTIVITY") or "", present=r.get("PRESENT") != 0, note=r.get("NOTE") or "",
                lon=g[0], lat=g[1], created_at=created, updated_at=datetime_ms(r.get("UPDATED_AT")) or created,
                deleted_at=datetime_ms(r.get("DELETED_AT")), length=r.get("LENGTH_M"), heading=r.get("HEADING"),
                confidence=r.get("CONFIDENCE") or "", recorded_by=r.get("OBSERVER") or "", updated_by=r.get("UPDATED_BY") or "",
            ))
        return out

    def save(self, records: list[Observation]) -> None:
        self.gpkg.upsert(OBSERVATIONS, [{
            "OBS_ID": o.id, "OBJECT_ID": o.object_id, "OBJECT_NAME": o.object_name, "IMAGE_ID": o.image_id,
            "IMAGE_NAME": o.image_name, "IMAGE_TIME": datetime_text(o.image_time), "KIND": o.kind, "ACTIVITY": o.activity,
            "PRESENT": 1 if o.present else 0, "CONFIDENCE": o.confidence, "LENGTH_M": o.length, "HEADING": o.heading,
            "NOTE": o.note, "OBSERVER": o.recorded_by, "UPDATED_BY": o.updated_by, "CREATED_AT": datetime_text(o.created_at),
            "UPDATED_AT": datetime_text(o.updated_at), "DELETED_AT": datetime_text(o.deleted_at), "geom": encode_point(o.lon, o.lat),
        } for o in records])

    def remove(self, records: list[Observation], now: Optional[int] = None) -> list[Observation]:
        marked = _marked(records, now)
        self.save(marked)
        return marked


class LocalAreaStore:
    """Checked areas in the local GeoPackage (`checked_areas`), one set per observation store."""

    label = LOCAL_LABEL

    def __init__(self, gpkg: GeoPackage, of: str):
        self.gpkg = gpkg
        self.of = of

    def load(self) -> list[CheckedArea]:
        out = []
        for r in self.gpkg.rows(AREAS, '"STORE" = ?', [self.of]):
            g = r.get("geom")
            if not isinstance(g, list) or not g or len(g[0]) < 4 or not r.get("AREA_ID"):
                continue
            created = datetime_ms(r.get("CREATED_AT")) or 0
            out.append(CheckedArea(
                id=r["AREA_ID"], image_id=r.get("IMAGE_ID") or "", image_name=r.get("IMAGE_NAME") or "",
                image_time=datetime_ms(r.get("IMAGE_TIME")), ring=g[0], kind=r.get("KIND") or "", note=r.get("NOTE") or "",
                created_at=created, updated_at=datetime_ms(r.get("UPDATED_AT")) or created,
                deleted_at=datetime_ms(r.get("DELETED_AT")), recorded_by=r.get("OBSERVER") or "",
            ))
        return out

    def save(self, areas: list[CheckedArea]) -> None:
        self.gpkg.upsert(AREAS, [{
            "AREA_ID": a.id, "IMAGE_ID": a.image_id, "IMAGE_NAME": a.image_name, "IMAGE_TIME": datetime_text(a.image_time),
            "KIND": a.kind, "NOTE": a.note, "OBSERVER": a.recorded_by, "CREATED_AT": datetime_text(a.created_at),
            "UPDATED_AT": datetime_text(a.updated_at), "DELETED_AT": datetime_text(a.deleted_at), "STORE": self.of,
            "geom": encode_polygon([a.ring]),
        } for a in areas])

    def remove(self, areas: list[CheckedArea], now: Optional[int] = None) -> list[CheckedArea]:
        marked = _marked(areas, now)
        self.save(marked)
        return marked


def _row_key(store: str, path: str) -> str:
    return f"{store}\u0000{path}"


class LocalTaxonomyStore:
    """A classification in the local GeoPackage (`taxonomy`), one per observation store."""

    label = LOCAL_LABEL

    def __init__(self, gpkg: GeoPackage, name: str):
        self.gpkg = gpkg
        self.name = name

    def _rows(self) -> list[dict[str, Any]]:
        return self.gpkg.rows(TAXONOMY, '"STORE" = ?', [self.name])

    def load(self) -> Optional[list[Taxon]]:
        rows = [r for r in self._rows() if r.get("PATH") and r["PATH"] != FIELDS_ROW]
        if not rows:
            return None
        rows.sort(key=lambda r: (r.get("SORT_ORDER") if is_number(r.get("SORT_ORDER")) else 1e12))
        return normalize(_taxon_of_row(r, DEFAULT_LOCAL_FIELDS) for r in rows if path_of(r["PATH"]))

    def save(self, taxa: list[Taxon]) -> None:
        taxa = normalize(taxa)
        keep = {_row_key(self.name, kind_of(t.path)) for t in taxa} | {_row_key(self.name, FIELDS_ROW)}
        for r in self._rows():
            if r["ROW_KEY"] not in keep:
                self.gpkg.delete(TAXONOMY, '"ROW_KEY" = ?', [r["ROW_KEY"]])
        self.gpkg.upsert(TAXONOMY, [{"ROW_KEY": _row_key(self.name, kind_of(t.path)), "STORE": self.name, **_row_of_taxon(t, i, DEFAULT_LOCAL_FIELDS)} for i, t in enumerate(taxa)])

    def load_fields(self) -> Optional[list[SpecField]]:
        row = next((r for r in self._rows() if r.get("PATH") == FIELDS_ROW), None)
        return _json_of(row.get("SPECS") if row else None, lambda v: spec_fields_of(v, []))

    def save_fields(self, fields: list[SpecField]) -> None:
        self.gpkg.upsert(TAXONOMY, [{"ROW_KEY": _row_key(self.name, FIELDS_ROW), "STORE": self.name, "PATH": FIELDS_ROW,
                                     "SPECS": json.dumps([f.to_json() for f in fields], ensure_ascii=False, separators=(",", ":"))}])


PHOTO_PREFIX = "local:"


def is_local_photo(ref: str) -> bool:
    """Whether a photo is kept locally (not seen by others sharing the classification)."""
    return ref.startswith(PHOTO_PREFIX)


class LocalPhotoStore:
    """Reference photos added from files, kept in the local GeoPackage (`taxon_photos`); named `local:<id>` in the classification."""

    def __init__(self, gpkg: GeoPackage):
        self.gpkg = gpkg

    def add(self, data: bytes, name: str = "", mime: str = "") -> str:
        photo_id = str(uuid.uuid4())
        self.gpkg.upsert(PHOTOS, [{"PHOTO_ID": photo_id, "NAME": name, "MIME": mime, "DATA": data, "CREATED_AT": datetime_text(now_ms())}])
        return PHOTO_PREFIX + photo_id

    def get(self, ref: str) -> Optional[bytes]:
        if not is_local_photo(ref):
            return None
        rows = self.gpkg.rows(PHOTOS, '"PHOTO_ID" = ?', [ref[len(PHOTO_PREFIX):]])
        return rows[0]["DATA"] if rows else None

    def remove(self, ref: str) -> None:
        if is_local_photo(ref):
            self.gpkg.delete(PHOTOS, '"PHOTO_ID" = ?', [ref[len(PHOTO_PREFIX):]])


def photo_url(ref: str) -> Optional[str]:
    """The URL a photo that is not local is shown from (http(s), data: images, relative paths); None for others."""
    return ref if re.match(r"^(https?:|data:image/|\.{0,2}/)", ref, re.IGNORECASE) else None


# ---------------------------------------------------------------------------
# Esri


def _text(v: Any) -> str:
    return "" if v is None else js_string(v)


def _present_of(v: Any) -> bool:
    if v is False or (is_number(v) and v == 0):
        return False
    return not re.fullmatch(r"(0|false|no|なし|いない|不在)", _text(v).strip(), re.IGNORECASE)


def _number_of(v: Any) -> Optional[float]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


class _EsriLayer:
    """A layer's field types and object id field, asked once."""

    def __init__(self, client: EsriClient, url: str, token: Optional[str], label: str, required: Iterable[str]):
        self.client, self.url, self.token, self.label = client, url, token, label
        self.required = list(required)
        self._types: Optional[dict[str, str]] = None
        self._oid = "OBJECTID"

    def ready(self) -> tuple[dict[str, str], str]:
        if self._types is None:
            info = self.client.layer(self.url, self.token, refresh=True)
            types = info.field_types
            for name in self.required:
                if name not in types:
                    raise EsriError(f"{self.label} に属性 {name} なし")
            self._types, self._oid = types, info.object_id_field
        return self._types, self._oid


class EsriObservationStore:
    """Observations in an Esri point feature layer (one record per feature)."""

    def __init__(self, config: ObservationStoreConfig, client: EsriClient, token: Optional[str] = None):
        self.config = config
        self.label = config.label
        self.token = token
        self.client = client
        #: The object id of each record (by its own id) as the layer gave it.
        self.oids: dict[str, int] = {}
        self._layer = _EsriLayer(client, config.url, token, config.label, [config.fields["id"], config.fields["objectId"]])

    def _feature_of(self, o: Observation, types: dict[str, str], oid: str) -> dict[str, Any]:
        f = self.config.fields
        values = {
            f["id"]: o.id, f["imageId"]: o.image_id, f["imageName"]: o.image_name, f["imageTime"]: o.image_time,
            f["objectId"]: o.object_id, f["objectName"]: o.object_name, f["kind"]: o.kind, f["activity"]: o.activity,
            f["present"]: o.present, f["note"]: o.note, f["length"]: o.length, f["heading"]: o.heading,
            f["confidence"]: o.confidence or "", f["recordedBy"]: o.recorded_by or "", f["updatedBy"]: o.updated_by or "",
            f["created"]: o.created_at, f["updated"]: o.updated_at, f["deleted"]: o.deleted_at,
        }
        attributes = {name: fit_value(v, types[name]) for name, v in values.items() if name in types}
        if o.id in self.oids:
            attributes[oid] = self.oids[o.id]
        return {"attributes": attributes, "geometry": point_geometry(o.lon, o.lat)}

    def _record_of(self, feature: dict[str, Any], oid: str) -> Optional[Observation]:
        f = self.config.fields
        a = feature.get("attributes") or {}
        g = feature.get("geometry") or {}
        record_id, object_id = _text(a.get(f["id"])), _text(a.get(f["objectId"]))
        x, y = g.get("x"), g.get("y")
        if not record_id or not object_id or not is_number(x) or not is_number(y):
            return None
        if is_number(a.get(oid)):
            self.oids[record_id] = int(a[oid])
        created = ms_of(a.get(f["created"])) or 0
        return Observation(
            id=record_id, image_id=_text(a.get(f["imageId"])), image_name=_text(a.get(f["imageName"])),
            image_time=ms_of(a.get(f["imageTime"])), object_id=object_id, object_name=_text(a.get(f["objectName"])),
            kind=_text(a.get(f["kind"])), activity=_text(a.get(f["activity"])),
            present=_present_of(a[f["present"]]) if f["present"] in a else True, note=_text(a.get(f["note"])),
            length=_number_of(a.get(f["length"])), heading=_number_of(a.get(f["heading"])), confidence=_text(a.get(f["confidence"])),
            recorded_by=_text(a.get(f["recordedBy"])), updated_by=_text(a.get(f["updatedBy"])), lon=float(x), lat=float(y),
            created_at=created, updated_at=ms_of(a.get(f["updated"])) or created, deleted_at=ms_of(a.get(f["deleted"])),
        )

    def load(self) -> list[Observation]:
        _, oid = self._layer.ready()
        features = self.client.query_all(self.config.url, self.config.where or "1=1", self.token, limit=200_000)
        return [o for o in (self._record_of(f, oid) for f in features) if o is not None]

    def save(self, records: list[Observation]) -> None:
        if not records:
            return
        types, oid = self._layer.ready()
        failed: list[str] = []
        for i in range(0, len(records), 200):
            batch = records[i:i + 200]
            adds = [o for o in batch if o.id not in self.oids]
            updates = [o for o in batch if o.id in self.oids]
            result = self.client.apply_edits(self.config.url, [self._feature_of(o, types, oid) for o in adds],
                                             [self._feature_of(o, types, oid) for o in updates], token=self.token)
            for o, r in zip(adds, result.add_results):
                if r.get("success") and is_number(r.get("objectId")):
                    self.oids[o.id] = int(r["objectId"])
                elif not r.get("success"):
                    failed.append((r.get("error") or {}).get("description") or "追加失敗")
            for r in result.update_results:
                if not r.get("success"):
                    failed.append((r.get("error") or {}).get("description") or "更新失敗")
        if failed:
            raise EsriError(f"{len(failed)} 件の保存に失敗: {failed[0]}")

    def remove(self, records: list[Observation], now: Optional[int] = None) -> list[Observation]:
        marked = _marked(records, now)
        types, _ = self._layer.ready()
        if self.config.soft_delete and self.config.fields["deleted"] in types:
            self.save(marked)
            return marked
        ids = [self.oids[o.id] for o in records if o.id in self.oids]
        if ids:
            results = self.client.delete_features(self.config.url, ids, token=self.token)
            # Each record is deleted or not on its own: the ones the server kept stay known, and the person is told.
            failed: set[int] = set()
            reason = ""
            for r in results:
                if r.get("success") or not is_number(r.get("objectId")):
                    continue
                failed.add(int(r["objectId"]))
                reason = reason or (r.get("error") or {}).get("description") or ""
            for o in records:
                if self.oids.get(o.id, -1) not in failed:
                    self.oids.pop(o.id, None)
            if failed:
                raise EsriError(f"{len(failed)} 件の削除に失敗{': ' + reason if reason else ''}")
        return []


class EsriAreaStore:
    """Checked areas in an Esri polygon feature layer."""

    def __init__(self, label: str, config: AreaStoreConfig, client: EsriClient, token: Optional[str] = None):
        self.label = label
        self.config = config
        self.client = client
        self.token = token
        self.oids: dict[str, int] = {}
        self._layer = _EsriLayer(client, config.url, token, label, [config.fields["id"]])

    def _feature_of(self, a: CheckedArea, types: dict[str, str], oid: str) -> dict[str, Any]:
        f = self.config.fields
        values = {
            f["id"]: a.id, f["imageId"]: a.image_id, f["imageName"]: a.image_name, f["imageTime"]: a.image_time,
            f["kind"]: a.kind, f["note"]: a.note, f["recordedBy"]: a.recorded_by or "", f["created"]: a.created_at,
            f["updated"]: a.updated_at, f["deleted"]: a.deleted_at,
        }
        attributes = {name: fit_value(v, types[name]) for name, v in values.items() if name in types}
        if a.id in self.oids:
            attributes[oid] = self.oids[a.id]
        return {"attributes": attributes, "geometry": ring_geometry([a.ring])}

    def load(self) -> list[CheckedArea]:
        _, oid = self._layer.ready()
        f = self.config.fields
        out = []
        for feature in self.client.query_all(self.config.url, "1=1", self.token, limit=100_000):
            a = feature.get("attributes") or {}
            rings = (feature.get("geometry") or {}).get("rings") or []
            area_id = _text(a.get(f["id"]))
            if not area_id or not rings or len(rings[0]) < 4:
                continue
            if is_number(a.get(oid)):
                self.oids[area_id] = int(a[oid])
            created = ms_of(a.get(f["created"])) or 0
            out.append(CheckedArea(
                id=area_id, image_id=_text(a.get(f["imageId"])), image_name=_text(a.get(f["imageName"])),
                image_time=ms_of(a.get(f["imageTime"])), ring=rings[0], kind=_text(a.get(f["kind"])), note=_text(a.get(f["note"])),
                recorded_by=_text(a.get(f["recordedBy"])), created_at=created, updated_at=ms_of(a.get(f["updated"])) or created,
                deleted_at=ms_of(a.get(f["deleted"])),
            ))
        return out

    def save(self, areas: list[CheckedArea]) -> None:
        if not areas:
            return
        types, oid = self._layer.ready()
        adds = [a for a in areas if a.id not in self.oids]
        updates = [a for a in areas if a.id in self.oids]
        result = self.client.apply_edits(self.config.url, [self._feature_of(a, types, oid) for a in adds],
                                         [self._feature_of(a, types, oid) for a in updates], token=self.token)
        failed = []
        for a, r in zip(adds, result.add_results):
            if r.get("success") and is_number(r.get("objectId")):
                self.oids[a.id] = int(r["objectId"])
            elif not r.get("success"):
                failed.append((r.get("error") or {}).get("description") or "追加失敗")
        for r in result.update_results:
            if not r.get("success"):
                failed.append((r.get("error") or {}).get("description") or "更新失敗")
        if failed:
            raise EsriError(f"{len(failed)} 件の保存に失敗: {failed[0]}")

    def remove(self, areas: list[CheckedArea], now: Optional[int] = None) -> list[CheckedArea]:
        marked = _marked(areas, now)
        types, _ = self._layer.ready()
        if self.config.soft_delete and self.config.fields["deleted"] in types:
            self.save(marked)
            return marked
        ids = [self.oids[a.id] for a in areas if a.id in self.oids]
        if ids:
            self.client.delete_features(self.config.url, ids, token=self.token)
        for a in areas:
            self.oids.pop(a.id, None)
        return []


#: The local table's attribute names (the default role names).
DEFAULT_LOCAL_FIELDS = {"path": "PATH", "levels": "LEVELS", "nameLabel": "NAME_LABEL", "members": "MEMBERS", "specs": "SPECS",
                        "photos": "PHOTOS", "order": "SORT_ORDER"}


def _list(text: Any) -> list[str]:
    return [s.strip() for s in re.split(r"[,、\n]", _text(text)) if s.strip()]


def _json_of(text: Any, read):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return read(json.loads(text)) or None
    except ValueError:
        return None


def _order_of(v: Any) -> Optional[float]:
    if is_number(v):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _taxon_of_row(a: dict[str, Any], f: dict[str, str]) -> Taxon:
    photos = [p for p in re.split(r"\s*\n\s*", _text(a.get(f["photos"]))) if p]
    return Taxon(
        path=path_of(_text(a.get(f["path"]))),
        levels=_list(a.get(f["levels"])) or None,
        name_label=_text(a.get(f["nameLabel"])).strip() or None,
        members=_list(a.get(f["members"])) or None,
        specs=_json_of(a.get(f["specs"]), specs_of),
        photos=photos or None,
    )


def _row_of_taxon(t: Taxon, i: int, f: dict[str, str]) -> dict[str, Any]:
    return {
        f["path"]: kind_of(t.path), f["levels"]: ",".join(t.levels or []), f["nameLabel"]: t.name_label or "",
        f["members"]: ",".join(t.members or []), f["specs"]: json.dumps(t.specs, ensure_ascii=False, separators=(",", ":")) if t.specs else "",
        f["photos"]: "\n".join(t.photos or []), f["order"]: i,
    }


class EsriTaxonomyStore:
    """A classification in an Esri table (one row per class, shared by everyone using the store)."""

    def __init__(self, config: TaxonomyTableConfig, client: EsriClient, label: str, token: Optional[str] = None):
        self.config = config
        self.client = client
        self.label = label
        self.token = token
        #: The object id of each class's row (by its kind).
        self.oids: dict[str, int] = {}
        self._fields_row: tuple[Optional[int], Optional[list[SpecField]]] = (None, None)
        self._loaded = False
        self._layer = _EsriLayer(client, config.url, token, "分類のテーブル", [config.fields["path"]])

    def load(self) -> Optional[list[Taxon]]:
        _, oid = self._layer.ready()
        f = self.config.fields
        rows: list[tuple[Taxon, float]] = []
        self.oids.clear()
        for feature in self.client.query_all(self.config.url, "1=1", self.token, geometry=False, limit=100_000):
            a = feature.get("attributes") or {}
            if _text(a.get(f["path"])) == FIELDS_ROW:
                self._fields_row = (int(a[oid]) if is_number(a.get(oid)) else None, _json_of(a.get(f["specs"]), lambda v: spec_fields_of(v, [])))
                continue
            path = path_of(_text(a.get(f["path"])))
            if not path:
                continue
            if is_number(a.get(oid)):
                self.oids[kind_of(path)] = int(a[oid])
            order = _order_of(a.get(f["order"]))
            # The first class is saved as 0: only a missing order goes after the rest.
            rows.append((_taxon_of_row(a, f), order if order is not None else len(rows)))
        self._loaded = True
        if not rows:
            return None
        rows.sort(key=lambda r: r[1])
        return normalize(t for t, _ in rows)

    def save(self, taxa: list[Taxon]) -> None:
        types, oid = self._layer.ready()
        f = self.config.fields
        everything = normalize(taxa)

        def attributes_of(t: Taxon, i: int) -> dict[str, Any]:
            out = {k: v for k, v in _row_of_taxon(t, i, f).items() if k in types}
            if kind_of(t.path) in self.oids:
                out[oid] = self.oids[kind_of(t.path)]
            return {"attributes": out}

        kept = {kind_of(t.path) for t in everything}
        deletes = [i for k, i in self.oids.items() if k not in kept]
        adds = [(t, i) for i, t in enumerate(everything) if kind_of(t.path) not in self.oids]
        updates = [(t, i) for i, t in enumerate(everything) if kind_of(t.path) in self.oids]
        result = self.client.apply_edits(self.config.url, [attributes_of(t, i) for t, i in adds],
                                         [attributes_of(t, i) for t, i in updates], deletes, token=self.token)
        failed = []
        for (t, _), r in zip(adds, result.add_results):
            if r.get("success") and is_number(r.get("objectId")):
                self.oids[kind_of(t.path)] = int(r["objectId"])
            elif not r.get("success"):
                failed.append((r.get("error") or {}).get("description") or "追加失敗")
        for r in result.update_results:
            if not r.get("success"):
                failed.append((r.get("error") or {}).get("description") or "更新失敗")
        # Rows the server did not delete stay known (deleted again with the next save).
        undeleted = {int(r["objectId"]) for r in result.delete_results if not r.get("success") and is_number(r.get("objectId"))}
        if undeleted:
            failed.append("削除失敗")
        gone = {i for i in deletes if i not in undeleted}
        for k, i in list(self.oids.items()):
            if i in gone:
                del self.oids[k]
        if failed:
            raise EsriError(f"分類 {len(failed)} 件の保存に失敗: {failed[0]}")

    def load_fields(self) -> Optional[list[SpecField]]:
        # Read with the classes.
        if not self._loaded:
            self.load()
        return self._fields_row[1]

    def save_fields(self, fields: list[SpecField]) -> None:
        types, oid = self._layer.ready()
        f = self.config.fields
        if f["specs"] not in types:
            raise EsriError(f"分類のテーブルに属性 {f['specs']} なし")
        row_oid = self._fields_row[0]
        row = {"attributes": {f["path"]: FIELDS_ROW, f["specs"]: json.dumps([x.to_json() for x in fields], ensure_ascii=False, separators=(",", ":")),
                              **({oid: row_oid} if row_oid is not None else {})}}
        if row_oid is not None:
            result = self.client.apply_edits(self.config.url, updates=[row], token=self.token)
            first = (result.update_results or [None])[0]
        else:
            result = self.client.apply_edits(self.config.url, adds=[row], token=self.token)
            first = (result.add_results or [None])[0]
        if not first or not first.get("success"):
            raise EsriError(f"諸元の項目の保存に失敗: {((first or {}).get('error') or {}).get('description') or '応答なし'}")
        new_oid = row_oid if row_oid is not None else (int(first["objectId"]) if is_number(first.get("objectId")) else None)
        self._fields_row = (new_oid, list(fields))


def taxonomy_json(taxa: list[Taxon]) -> list[dict[str, Any]]:
    """A classification as the viewer writes a flat list."""
    return [t.to_json() for t in normalize(taxa)]


__all__ = [
    "LOCAL_LABEL", "LocalObservationStore", "LocalAreaStore", "LocalTaxonomyStore", "LocalPhotoStore", "EsriObservationStore",
    "EsriAreaStore", "EsriTaxonomyStore", "is_local_photo", "photo_url", "taxa_from_json", "taxonomy_json",
]
