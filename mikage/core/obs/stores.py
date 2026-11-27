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
from typing import Any, Iterable, Optional, Protocol, Sequence

from ..esri import DELETE_BATCH, ConflictError, EsriClient, EsriError, TooManyError, fit_value, point_geometry, quote, ring_geometry
from ..gpkg import AREAS, OBSERVATIONS, PHOTOS, TAXONOMY, GeoPackage, datetime_ms, datetime_text, encode_point, encode_polygon
from ..together import read_together
from ..util import is_number, js_string, ms_of
from .ai_origin import AREA_ROLES, RECORD_ROLES, origin_values, read_origin
from .areas import AreaStoreConfig, CheckedArea
from .flows import now_ms, time_key
from .model import Observation, ObservationStoreConfig, kept_quantity
from .scope import RecordQuery, envelope, merged, time_condition
from .taxonomy import (
    ALIAS_FIELD, FIELDS_ROW, Member, SpecField, Taxon, TaxonomyTableConfig, kind_of, members_of, new_member_id, normalize, path_of,
    segment_name, spec_fields_of, specs_of, split_list, taxa_from_json,
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
    #: Whether what known individuals say of themselves (別名・諸元・参考写真・備考) is kept (else their names only).
    keeps_members: bool

    def load(self) -> Optional[list[Taxon]]:
        """The classification kept, or None when none is kept yet (the configured one applies)."""
        ...

    def save(self, taxa: list[Taxon]) -> None: ...

    def load_fields(self) -> Optional[list[SpecField]]:
        """The specification fields kept, or None when none are kept yet."""
        ...

    def save_fields(self, fields: list[SpecField]) -> None: ...


def present_value(o: Observation) -> Optional[int]:
    """PRESENT as kept: 在 1・消失 0, empty for a 不可 record (neither)."""
    return None if o.unseen else 1 if o.present else 0


def unseen_supported(store: Any) -> bool:
    """
    Whether a store keeps 「不可」 (its layer has the 不可の理由 attribute):
    the local ones do; an Esri one once its layer has been read (no question
    is asked here), else not.
    """
    can = getattr(store, "keeps_unseen", None)
    return bool(can()) if callable(can) else False


def satellite_supported(store: Any) -> bool:
    """
    Whether a store keeps 衛星名 (its layer has SATELLITE, 2026-11-20): the
    local ones do; an Esri one once its layer has been read with it (no
    question is asked here), else not.
    """
    can = getattr(store, "keeps_satellite", None)
    return bool(can()) if callable(can) else False


def quantity_supported(store: Any) -> bool:
    """
    Whether a store keeps 数量 (its layer has QUANTITY, 2026-11-24): the
    local ones do; an Esri one once its layer has been read with it (no
    question is asked here), else not.
    """
    can = getattr(store, "keeps_quantity", None)
    return bool(can()) if callable(can) else False


def _quantity_of(v: Any) -> Optional[int]:
    """QUANTITY as read: 2 or more, else None (empty, 1, or not a number: one thing)."""
    return kept_quantity(_number_of(v))


def ai_missing(store: Any, ask: bool = False) -> list[str]:
    """
    The AI columns (SOURCE, AI_LAYER…, 2026-11-27) a store's layer lacks: an
    AI result is not recorded there (the import says which to add). [] for
    the local ones; an Esri layer as read, else [] (`ask`: its fields are
    asked first, as a save would; [] when they cannot be, the save says why).
    """
    can = getattr(store, "missing_ai", None)
    return list(can(ask)) if callable(can) else []


def _missing_of(layer: "_EsriLayer", fields: dict[str, str], roles: dict, ask: bool) -> list[str]:
    types = layer.known_types()
    if types is None and ask:
        try:
            types, _oid = layer.ready()
        except Exception:  # noqa: BLE001 (the save that follows says why)
            types = None
    return [] if types is None else [fields[r] for r in roles if fields[r] not in types]


def _local_origin(row: dict[str, Any], roles: dict) -> Any:
    return read_origin(row.get, {}, roles, lambda v: "" if v is None else str(v), _number_of, datetime_ms)


def _local_origin_values(ai: Any, roles: dict) -> dict[str, Any]:
    values = origin_values(ai, {}, roles)
    if values.get("AI_DETECTED_AT") is not None:
        values["AI_DETECTED_AT"] = datetime_text(values["AI_DETECTED_AT"])
    return values


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
            unseen = (r.get("UNSEEN_REASON") or "").strip()
            out.append(Observation(
                id=r["OBS_ID"], image_id=r.get("IMAGE_ID") or "", image_name=r.get("IMAGE_NAME") or "",
                image_time=datetime_ms(r.get("IMAGE_TIME")), object_id=r["OBJECT_ID"], object_name=r.get("OBJECT_NAME") or "",
                kind=r.get("KIND") or "", activity=r.get("ACTIVITY") or "", present=not unseen and r.get("PRESENT") != 0,
                unseen=unseen, note=r.get("NOTE") or "",
                lon=g[0], lat=g[1], created_at=created, updated_at=datetime_ms(r.get("UPDATED_AT")) or created,
                deleted_at=datetime_ms(r.get("DELETED_AT")), length=r.get("LENGTH_M"), heading=r.get("HEADING"), width=r.get("WIDTH_M"),
                confidence=r.get("CONFIDENCE") or "", recorded_by=r.get("OBSERVER") or "", updated_by=r.get("UPDATED_BY") or "",
                pixel_x=r.get("PIXEL_X"), pixel_y=r.get("PIXEL_Y"), image_file=r.get("IMAGE_FILE") or "",
                satellite=r.get("SATELLITE") or "", quantity=_quantity_of(r.get("QUANTITY")) if not unseen and r.get("PRESENT") != 0 else None,
                ai=_local_origin(r, RECORD_ROLES),
            ))
        return out

    def save(self, records: list[Observation]) -> None:
        self.gpkg.upsert(OBSERVATIONS, [{
            "OBS_ID": o.id, "OBJECT_ID": o.object_id, "OBJECT_NAME": o.object_name, "IMAGE_ID": o.image_id,
            "IMAGE_NAME": o.image_name, "IMAGE_TIME": datetime_text(o.image_time), "KIND": o.kind, "ACTIVITY": o.activity,
            "PRESENT": present_value(o), "UNSEEN_REASON": o.unseen or None, "CONFIDENCE": o.confidence, "LENGTH_M": o.length, "HEADING": o.heading, "WIDTH_M": o.width,
            "PIXEL_X": o.pixel_x, "PIXEL_Y": o.pixel_y, "IMAGE_FILE": o.image_file or None, "SATELLITE": o.satellite or None,
            "QUANTITY": o.quantity, "NOTE": o.note, "OBSERVER": o.recorded_by, "UPDATED_BY": o.updated_by, "CREATED_AT": datetime_text(o.created_at),
            "UPDATED_AT": datetime_text(o.updated_at), "DELETED_AT": datetime_text(o.deleted_at), "geom": encode_point(o.lon, o.lat),
            **_local_origin_values(o.ai, RECORD_ROLES),
        } for o in records])

    def remove(self, records: list[Observation], now: Optional[int] = None) -> list[Observation]:
        marked = _marked(records, now)
        self.save(marked)
        return marked

    def keeps_unseen(self) -> bool:
        return True

    def keeps_satellite(self) -> bool:
        return True

    def keeps_quantity(self) -> bool:
        return True

    def missing_ai(self, ask: bool = False) -> list[str]:
        return []

    def clear(self, deleted_only: bool = False) -> int:
        """Removes the records for good (only the deleted ones when asked); how many."""
        return self.gpkg.delete(OBSERVATIONS, '"DELETED_AT" IS NOT NULL' if deleted_only else "1")

    def find(self, query: RecordQuery) -> list[Observation]:
        """The records of a query (read from the file on this computer, so every record may be read)."""
        return [o for o in self.load() if query.matches(o)]


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
                unseen=(r.get("UNSEEN_REASON") or "").strip(), satellite=r.get("SATELLITE") or "", ai=_local_origin(r, AREA_ROLES),
            ))
        return out

    def save(self, areas: list[CheckedArea]) -> None:
        self.gpkg.upsert(AREAS, [{
            "AREA_ID": a.id, "IMAGE_ID": a.image_id, "IMAGE_NAME": a.image_name, "IMAGE_TIME": datetime_text(a.image_time),
            "KIND": a.kind, "NOTE": a.note, "OBSERVER": a.recorded_by, "CREATED_AT": datetime_text(a.created_at),
            "UPDATED_AT": datetime_text(a.updated_at), "DELETED_AT": datetime_text(a.deleted_at), "STORE": self.of,
            "UNSEEN_REASON": a.unseen or None, "SATELLITE": a.satellite or None, "geom": encode_polygon([a.ring]),
            **_local_origin_values(a.ai, AREA_ROLES),
        } for a in areas])

    def remove(self, areas: list[CheckedArea], now: Optional[int] = None) -> list[CheckedArea]:
        marked = _marked(areas, now)
        self.save(marked)
        return marked

    def keeps_unseen(self) -> bool:
        return True

    def keeps_satellite(self) -> bool:
        return True

    def missing_ai(self, ask: bool = False) -> list[str]:
        return []

    def clear(self, deleted_only: bool = False) -> int:
        """Removes this store's checked areas for good (only the deleted ones when asked); how many."""
        return self.gpkg.delete(AREAS, '"STORE" = ?' + (' AND "DELETED_AT" IS NOT NULL' if deleted_only else ""), [self.of])

    def find(self, image_ids: Sequence[str] = (), start: Optional[float] = None, end: Optional[float] = None) -> list[CheckedArea]:
        """The checked areas of some images, or of a period (area_in)."""
        return [a for a in self.load() if area_in(a, image_ids, start, end)]


def area_in(a: CheckedArea, image_ids: Sequence[str] = (), start: Optional[float] = None, end: Optional[float] = None) -> bool:
    """Whether a checked area is of one of the images (none given: any) and in the period (its image's time, else when made)."""
    if image_ids and a.image_id not in image_ids:
        return False
    t = a.image_time if a.image_time is not None else a.created_at
    return (start is None or (t is not None and t >= start)) and (end is None or (t is not None and t <= end))


def _row_key(store: str, path: str) -> str:
    return f"{store}\u0000{path}"


class LocalTaxonomyStore:
    """A classification in the local GeoPackage (`taxonomy`), one per observation store."""

    label = LOCAL_LABEL
    keeps_members = True

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
    return not re.fullmatch(r"(0|false|no|なし|いない|不在|消失)", _text(v).strip(), re.IGNORECASE)


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

    def has(self, name: str) -> bool:
        """Whether the layer has an attribute, as read (False before it was read: nothing is asked here)."""
        return self._types is not None and name in self._types

    def ready(self) -> tuple[dict[str, str], str]:
        if self._types is None:
            info = self.client.layer_with(self.url, self.token, self.required)
            types = info.field_types
            for name in self.required:
                if name not in types:
                    raise EsriError(f"{self.label}に必要な項目がない（管理者に連絡: {name}）", code="E704")
            self._types, self._oid = types, info.object_id_field
        return self._types, self._oid

    def known_types(self) -> Optional[dict[str, str]]:
        """The attributes' types when read already (None: not yet; nothing is asked)."""
        return self._types


#: How far apart two 更新日時 may be and still be the same save (services that keep whole seconds).
_STAMP_SLACK_MS = 1000
#: The most records one question may bring (TooManyError past it: a narrower period or condition then).
FIND_LIMIT = 200_000
#: How many records near an image's footprint are read, at or before its time and after it (load_around).
AROUND_BEFORE = 2000
AROUND_AFTER = 500


class EsriObservationStore:
    """Observations in an Esri point feature layer (one record per feature)."""

    def __init__(self, config: ObservationStoreConfig, client: EsriClient, token: Optional[str] = None):
        self.config = config
        self.label = config.label
        self.token = token
        self.client = client
        #: The object id of each record (by its own id) as the layer gave it.
        self.oids: dict[str, int] = {}
        #: Each record's 更新日時 as read (or last saved here): a record changed by someone else since is not overwritten (#74).
        self.stamps: dict[str, Optional[float]] = {}
        #: The 更新日時 of saves whose answer was lost (a timeout): the server may hold them.
        self.pending: dict[str, float] = {}
        #: Earlier such saves of records kept while offline (core/offline.py): none of them is someone else's change either.
        self.tried: dict[str, tuple[float, ...]] = {}
        self.last_saved: set[str] = set()
        self.last_conflicts: set[str] = set()
        #: The ids the last remove went through for, and the records of them marked (kept): when it raises, part may be done.
        self.last_removed: set[str] = set()
        self.last_marked: list[Observation] = []
        self._layer = _EsriLayer(client, config.url, token, config.label, [config.fields["id"], config.fields["objectId"]])

    def _feature_of(self, o: Observation, types: dict[str, str], oid: str) -> dict[str, Any]:
        f = self.config.fields
        values = {
            f["id"]: o.id, f["imageId"]: o.image_id, f["imageName"]: o.image_name, f["imageTime"]: o.image_time,
            f["objectId"]: o.object_id, f["objectName"]: o.object_name, f["kind"]: o.kind, f["activity"]: o.activity,
            f["present"]: None if o.unseen else o.present, f["note"]: o.note, f["length"]: o.length, f["heading"]: o.heading, f["width"]: o.width,
            f["pixelX"]: o.pixel_x, f["pixelY"]: o.pixel_y, f["imageFile"]: o.image_file or None, f["satellite"]: o.satellite or None,
            f["quantity"]: o.quantity, f["confidence"]: o.confidence or "", f["recordedBy"]: o.recorded_by or "", f["updatedBy"]: o.updated_by or "",
            f["created"]: o.created_at, f["updated"]: o.updated_at, f["deleted"]: o.deleted_at, f["unseen"]: o.unseen or None,
            **origin_values(o.ai, f, RECORD_ROLES),
        }
        if o.unseen and f["unseen"] not in types:
            # Without it the record would read back as 在 (PRESENT empty): not saved.
            raise EsriError(f"{self.label}では「不可」を保存できない（管理者に連絡: {f['unseen']}）", code="E704")
        attributes = {name: fit_value(v, types[name]) for name, v in values.items() if name in types}
        if o.id in self.oids:
            attributes[oid] = self.oids[o.id]
        return {"attributes": attributes, "geometry": point_geometry(o.lon, o.lat)}

    def keeps_unseen(self) -> bool:
        """Whether the layer (as read) has the 不可の理由 attribute."""
        return self._layer.has(self.config.fields["unseen"])

    def keeps_satellite(self) -> bool:
        """Whether the layer (as read) has the 衛星名 attribute."""
        return self._layer.has(self.config.fields["satellite"])

    def keeps_quantity(self) -> bool:
        """Whether the layer (as read) has the 数量 attribute."""
        return self._layer.has(self.config.fields["quantity"])

    def missing_ai(self, ask: bool = False) -> list[str]:
        """The AI columns the layer lacks (as read; `ask`: asked first when not read yet)."""
        return _missing_of(self._layer, self.config.fields, RECORD_ROLES, ask)

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
        self.stamps[record_id] = ms_of(a.get(f["updated"]))
        created = ms_of(a.get(f["created"])) or 0
        unseen = _text(a.get(f["unseen"])).strip()
        return Observation(
            id=record_id, image_id=_text(a.get(f["imageId"])), image_name=_text(a.get(f["imageName"])),
            image_time=ms_of(a.get(f["imageTime"])), object_id=object_id, object_name=_text(a.get(f["objectName"])),
            kind=_text(a.get(f["kind"])), activity=_text(a.get(f["activity"])),
            present=not unseen and (_present_of(a[f["present"]]) if f["present"] in a else True), unseen=unseen,
            note=_text(a.get(f["note"])),
            length=_number_of(a.get(f["length"])), heading=_number_of(a.get(f["heading"])),
            width=_number_of(a.get(f["width"])), confidence=_text(a.get(f["confidence"])),
            pixel_x=_number_of(a.get(f["pixelX"])), pixel_y=_number_of(a.get(f["pixelY"])), image_file=_text(a.get(f["imageFile"])),
            satellite=_text(a.get(f["satellite"])), quantity=_quantity_of(a.get(f["quantity"])) if not unseen else None,
            recorded_by=_text(a.get(f["recordedBy"])), updated_by=_text(a.get(f["updatedBy"])), lon=float(x), lat=float(y),
            created_at=created, updated_at=ms_of(a.get(f["updated"])) or created, deleted_at=ms_of(a.get(f["deleted"])),
            ai=read_origin(a.get, f, RECORD_ROLES, _text, _number_of, ms_of),
        )

    def as_kept(self, records: list[Observation]) -> list[Observation]:
        """
        The records as the layer keeps them: the values of the optional
        attributes it does not have left out (a layer made before PIXEL_X・PIXEL_Y・IMAGE_FILE, 2026-11-19, SATELLITE, 2026-11-20,
        or QUANTITY, 2026-11-24),
        so a record read back is the one saved (not 「ほかの人の記録」 changed since).
        """
        types = self._layer.known_types()
        if types is None:
            return records
        f = self.config.fields
        blank = {name: value for role, name, value in (
            ("length", "length", None), ("heading", "heading", None), ("width", "width", None), ("pixelX", "pixel_x", None),
            ("pixelY", "pixel_y", None), ("imageFile", "image_file", ""), ("satellite", "satellite", ""), ("quantity", "quantity", None),
            ("confidence", "confidence", ""), ("aiSource", "ai", None),
        ) if f[role] not in types}
        return [replace(o, **blank) for o in records] if blank else records

    #: Read only as asked (scope.py): what a feature needs, never every record (EsriObservationStore.find).
    scoped = True

    def load(self) -> list[Observation]:
        """Every record. Not used by the plugin (a shared layer may hold millions): see find and load_around."""
        _, oid = self._layer.ready()
        features = self.client.query_all(self.config.url, self.config.where or "1=1", self.token, limit=200_000)
        return [o for o in (self._record_of(f, oid) for f in features) if o is not None]

    def _read(self, where: str, extra: Optional[dict] = None, limit: int = FIND_LIMIT, cut: bool = False) -> list[Observation]:
        _, oid = self._layer.ready()
        features = self.client.query_all(self.config.url, where, self.token, extra=extra, limit=limit, cut=cut)
        return [o for o in (self._record_of(f, oid) for f in features) if o is not None]

    def find(self, query: RecordQuery, limit: int = FIND_LIMIT) -> list[Observation]:
        """
        The records of a query (scope.RecordQuery): asked of the layer, in
        parts when a list is long. A query without any condition is refused
        (every record of a shared layer). More than `limit` records is an
        error (TooManyError: a period or condition narrower then).
        """
        if query.everything():
            raise EsriError("条件なしで全件は読まない（期間・画像・範囲などを指定）")
        types, _ = self._layer.ready()
        extra = envelope(query.box) if query.box else None
        out: list[Observation] = []
        for where in query.wheres(self.config.fields, types, self.config.where):
            out = merged(out, self._read(where, extra, limit))
            if len(out) > limit:
                raise TooManyError(f"件数が上限（{limit:,} 件）を超えた。期間や条件で絞って取得")
        # What the layer could not be asked (an attribute it lacks) is tested here.
        return [o for o in out if query.matches(o)]

    def load_around(self, images: Sequence[tuple], before: int = AROUND_BEFORE, after: int = AROUND_AFTER) -> list[Observation]:
        """
        What recording on some images needs (each `(id, box or None, time or
        None)`): every record of the images (the deleted too), and for each
        with a footprint the latest `before` records near it at or before its
        time and the first `after` after it, with their things' records of
        that stretch of time anywhere. That is enough for 「前後の画像のモノ」
        and 「同一候補」 (each thing's record nearest in time to the image):
        a thing's nearest record is never older than its latest one near the
        image. The stretch follows how often the place is imaged (a busy port:
        days; a place imaged twice a year: years).
        """
        types, _ = self._layer.ready()
        f = self.config.fields
        base = [f"({self.config.where})"] if self.config.where else []
        living = [f"{f['deleted']} IS NULL"] if f["deleted"] in types else []
        timed = f["imageTime"] if f["imageTime"] in types else None
        # The images' own records and those near each image before and after its time: read at once (core/together.py).
        reads = [lambda: self.find(RecordQuery(image_ids=tuple(dict.fromkeys(i for i, _, _ in images if i)))) if images else []]
        sides: list[list[int]] = []
        for _, box, t in images:
            mine = []
            for op, order, count in (("<=", "DESC", before), (">", "ASC", after)):
                if not box or (op == ">" and (t is None or not timed or count <= 0)):
                    continue
                when = [time_condition(timed, types.get(timed), op, t)] if timed and t is not None else []
                where = " AND ".join([*base, *living, *when]) or "1=1"
                order_by = {"orderByFields": f"{timed} {order}"} if timed else {}
                mine.append(len(reads))
                reads.append(lambda where=where, box=box, order_by=order_by, count=count:
                             self._read(where, {**envelope(box), **order_by}, limit=count, cut=True))
            sides.append(mine)
        answers = read_together(*reads)
        out = answers[0]
        spans: dict[str, list[float]] = {}
        for (_, box, t), mine in zip(images, sides):
            if not box:
                continue
            near: list[Observation] = [o for i in mine for o in answers[i]]
            times = [time_key(o) for o in near]
            low = min([*times, *([t] if t is not None else [])], default=None)
            high = max([*times, *([t] if t is not None else [])], default=None)
            for o in near:
                span = spans.setdefault(o.object_id, [low, high])
                span[0] = None if span[0] is None or low is None else min(span[0], low)
                span[1] = None if span[1] is None or high is None else max(span[1], high)
            out = merged(out, near)
        # Each thing's records of that stretch (things of the same stretch asked together).
        groups: dict[tuple, list[str]] = {}
        for object_id, (low, high) in spans.items():
            groups.setdefault((low, high), []).append(object_id)
        for (low, high), ids in groups.items():
            out = merged(out, self.find(RecordQuery(object_ids=tuple(ids), start=low, end=high, living=True)))
        return out

    def load_changed(self, since: float) -> Optional[list[Observation]]:
        """
        The records saved at or after `since` (ms; the 更新日時 field), to be
        merged into those read before; None when the layer has no 更新日時
        or deletes for good (a removed feature cannot be seen this way): read
        everything then.
        """
        from ..sql import since_condition

        types, oid = self._layer.ready()
        updated = self.config.fields["updated"]
        if updated not in types or self.removes_for_good():
            return None
        changed = since_condition(updated, types.get(updated), since)
        where = f"({self.config.where}) AND {changed}" if self.config.where else changed
        features = self.client.query_all(self.config.url, where, self.token, limit=200_000)
        return [o for o in (self._record_of(f, oid) for f in features) if o is not None]

    def load_made(self, names: Sequence[str], since: Optional[float]) -> list[Observation]:
        """
        「自分の記録」: the records made by `names` (記録者, capitals aside;
        empty: anyone) at or after `since` (ms, the 登録日時; None: ever),
        asked of the layer alone (not every record read). The 記録者 and
        登録日時 not on the layer are left out of the question; the list then
        says what to show.
        """
        from ..sql import since_condition

        types, oid = self._layer.ready()
        f = self.config.fields
        parts = [f"({self.config.where})"] if self.config.where else []
        observer = f["recordedBy"]
        if names and observer in types:
            parts.append(f"UPPER({observer}) IN ({', '.join(quote(n.strip().upper()) for n in names)})")
        created = f["created"]
        if since is not None and created in types:
            parts.append(since_condition(created, types.get(created), since))
        features = self.client.query_all(self.config.url, " AND ".join(parts) or "1=1", self.token, limit=200_000)
        return [o for o in (self._record_of(f, oid) for f in features) if o is not None]

    def _check(self, records: list[Observation], types: dict[str, str], oid: str) -> list[str]:
        """
        Asks the layer about the records before saving: those already there
        but not known here (an add whose answer was lost, #73) become
        updates; those changed by someone else since read are left out
        (their reasons returned, #74).
        """
        f = self.config.fields
        updated = f["updated"] if f["updated"] in types else None
        found = self.client.find(self.config.url, f["id"], [o.id for o in records], self.token,
                                 out_fields=",".join(n for n in (f["id"], oid, updated) if n))
        conflicts = []
        for o in list(records):
            a = found.get(o.id)
            if a is None:
                if o.id in self.oids:
                    # Deleted by someone else: added again as it is here (it was kept, not removed, here).
                    self.oids.pop(o.id, None)
                continue
            if is_number(a.get(oid)):
                known = o.id in self.oids
                self.oids[o.id] = int(a[oid])
                if not known:
                    continue
            if updated:
                server, seen = ms_of(a.get(updated)), self.stamps.get(o.id)
                ours = [s for s in (self.pending.get(o.id), *self.tried.get(o.id, ())) if s is not None]
                if (server is not None and seen is not None and abs(server - seen) > _STAMP_SLACK_MS
                        and not any(abs(server - s) <= _STAMP_SLACK_MS for s in ours)):
                    conflicts.append(o.id)
        return conflicts

    def save(self, records: list[Observation]) -> None:
        #: The ids this save wrote, and those it left for someone else's change (for a bulk change to tell which went through).
        self.last_saved, self.last_conflicts = set(), set()
        if not records:
            return
        types, oid = self._layer.ready()
        failed: list[str] = []
        conflicts = set(self._check(records, types, oid))
        self.last_conflicts = set(conflicts)
        records = [o for o in records if o.id not in conflicts]
        for i in range(0, len(records), 200):
            batch = records[i:i + 200]
            adds = [o for o in batch if o.id not in self.oids]
            updates = [o for o in batch if o.id in self.oids]
            try:
                result = self.client.apply_edits(self.config.url, [self._feature_of(o, types, oid) for o in adds],
                                                 [self._feature_of(o, types, oid) for o in updates], token=self.token)
            except EsriError:
                for o in batch:
                    self.pending[o.id] = o.updated_at
                raise
            for o, r in zip(adds, result.add_results):
                if r.get("success") and is_number(r.get("objectId")):
                    self.oids[o.id] = int(r["objectId"])
                    self.stamps[o.id], _ = o.updated_at, self.pending.pop(o.id, None)
                    self.tried.pop(o.id, None)
                    self.last_saved.add(o.id)
                elif not r.get("success"):
                    failed.append((r.get("error") or {}).get("description") or "追加失敗")
            for o, r in zip(updates, result.update_results):
                if r.get("success"):
                    self.stamps[o.id], _ = o.updated_at, self.pending.pop(o.id, None)
                    self.tried.pop(o.id, None)
                    self.last_saved.add(o.id)
                else:
                    failed.append((r.get("error") or {}).get("description") or "更新失敗")
        if conflicts or failed:
            parts = [f"{len(conflicts)} 件はほかの人が先に変更。「最新を取得」を押してから変更"] if conflicts else []
            if failed:
                parts.append(f"{len(failed)} 件をサーバーが受け付けない")
            if failed:
                raise EsriError("・".join(parts), code="E305", detail=failed[0])
            raise ConflictError("・".join(parts))

    def know(self, record_ids: list[str]) -> None:
        """Asks the layer for the object ids of records not known here (before deleting them for good)."""
        new = [i for i in record_ids if i not in self.oids]
        if not new:
            return
        _, oid = self._layer.ready()
        for record_id, attrs in self.client.find(self.config.url, self.config.fields["id"], new, self.token,
                                                  out_fields=f"{self.config.fields['id']},{oid}").items():
            if is_number(attrs.get(oid)):
                self.oids[record_id] = int(attrs[oid])

    def expect(self, record_id: str, seen: Optional[float], sending: Optional[float] = None,
               tried: Iterable[float] = ()) -> None:
        """
        Before sending a change made while the server could not be reached
        (core/obs/offline_stores.py): the record was on the server, with the
        更新日時 `seen` then, so a change someone else made meanwhile is not
        saved over (#74); `sending` is the 更新日時 sent (an answer lost then is
        no conflict), `tried` those of earlier saves whose answer was lost (the
        timeout that started the offline work: the server may hold them).
        """
        self.oids.setdefault(record_id, -1)
        if seen is not None:
            self.stamps[record_id] = seen
        if sending is not None:
            self.pending[record_id] = sending
        tried = tuple(tried)
        if tried:
            self.tried[record_id] = tried

    def removes_for_good(self) -> bool:
        """Whether a delete removes the features (no 削除日時 field to mark, or `softDelete: false`)."""
        types, _ = self._layer.ready()
        return not (self.config.soft_delete and self.config.fields["deleted"] in types)

    def remove(self, records: list[Observation], now: Optional[int] = None) -> list[Observation]:
        marked = _marked(records, now)
        self.last_removed, self.last_marked = set(), []
        if not self.removes_for_good():
            try:
                self.save(marked)
            finally:
                self.last_removed = set(self.last_saved)
                self.last_marked = [o for o in marked if o.id in self.last_saved]
            return marked
        self.last_removed = {o.id for o in records if o.id not in self.oids}
        known = [o for o in records if o.id in self.oids]
        failed: set[int] = set()
        reason = ""
        # DELETE_BATCH to a request; a request that fails leaves what the earlier ones deleted told (last_removed).
        for start in range(0, len(known), DELETE_BATCH):
            part = known[start:start + DELETE_BATCH]
            results = self.client.delete_features(self.config.url, [self.oids[o.id] for o in part], token=self.token)
            # Each record is deleted or not on its own: the ones the server kept stay known, and the person is told.
            kept: set[int] = set()
            for r in results:
                if r.get("success") or not is_number(r.get("objectId")):
                    continue
                kept.add(int(r["objectId"]))
                reason = reason or (r.get("error") or {}).get("description") or ""
            for o in part:
                if self.oids.get(o.id, -1) not in kept:
                    self.oids.pop(o.id, None)
                    self.last_removed.add(o.id)
            failed |= kept
        if failed:
            raise EsriError(f"{len(failed)} 件の削除をサーバーが受け付けない", code="E305", detail=reason)
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
            f["updated"]: a.updated_at, f["deleted"]: a.deleted_at, f["unseen"]: a.unseen or None, f["satellite"]: a.satellite or None,
            **origin_values(a.ai, f, AREA_ROLES),
        }
        if a.unseen and f["unseen"] not in types:
            # Without it the area would read back as a 確認範囲 (everything there recorded): not saved.
            raise EsriError(f"{self.label}では不可範囲を保存できない（管理者に連絡: {f['unseen']}）", code="E704")
        attributes = {name: fit_value(v, types[name]) for name, v in values.items() if name in types}
        if a.id in self.oids:
            attributes[oid] = self.oids[a.id]
        return {"attributes": attributes, "geometry": ring_geometry([a.ring])}

    def keeps_unseen(self) -> bool:
        """Whether the layer (as read) has the 不可の理由 attribute."""
        return self._layer.has(self.config.fields["unseen"])

    def keeps_satellite(self) -> bool:
        """Whether the layer (as read) has the 衛星名 attribute."""
        return self._layer.has(self.config.fields["satellite"])

    def missing_ai(self, ask: bool = False) -> list[str]:
        """The AI columns the layer lacks (as read; `ask`: asked first when not read yet)."""
        return _missing_of(self._layer, self.config.fields, AREA_ROLES, ask)

    def as_kept(self, areas: list[CheckedArea]) -> list[CheckedArea]:
        """
        The areas as the layer keeps them: 衛星名 left out when it has no such attribute (a layer made before 2026-11-20),
        the AI result without SOURCE (before 2026-11-27).
        """
        types = self._layer.known_types()
        if types is None:
            return areas
        blank = {name: value for role, name, value in (("satellite", "satellite", ""), ("aiSource", "ai", None))
                 if self.config.fields[role] not in types}
        return [replace(a, **blank) for a in areas] if blank else areas

    scoped = True

    def load(self) -> list[CheckedArea]:
        """Every checked area. Not used by the plugin (find)."""
        return self._read("1=1")

    def find(self, image_ids: Sequence[str] = (), start: Optional[float] = None, end: Optional[float] = None) -> list[CheckedArea]:
        """The checked areas of some images (none given: any), or of a period (their image's time, else when made)."""
        types, _ = self._layer.ready()
        f = self.config.fields
        ands = []
        if start is not None or end is not None:
            ands += RecordQuery(start=start, end=end).wheres(f, types)
        ids = [i for i in dict.fromkeys(image_ids) if i]
        if not ids:
            if not ands:
                raise EsriError("条件なしで全件は読まない（画像・期間を指定）")
            return [a for a in self._read(" AND ".join(ands)) if area_in(a, (), start, end)]
        out: dict[str, CheckedArea] = {}
        for i in range(0, len(ids), 100):
            listed = f"{f['imageId']} IN ({','.join(quote(x) for x in ids[i:i + 100])})"
            for a in self._read(" AND ".join([*ands, listed])):
                out[a.id] = a
        return [a for a in out.values() if area_in(a, ids, start, end)]

    def _read(self, where: str) -> list[CheckedArea]:
        _, oid = self._layer.ready()
        f = self.config.fields
        out = []
        for feature in self.client.query_all(self.config.url, where, self.token, limit=100_000):
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
                deleted_at=ms_of(a.get(f["deleted"])), unseen=_text(a.get(f["unseen"])).strip(),
                satellite=_text(a.get(f["satellite"])), ai=read_origin(a.get, f, AREA_ROLES, _text, _number_of, ms_of),
            ))
        return out

    def save(self, areas: list[CheckedArea]) -> None:
        if not areas:
            return
        types, oid = self._layer.ready()
        # Already there but not known here (an add whose answer was lost): changed, not added again (#73).
        new = [a.id for a in areas if a.id not in self.oids]
        if new:
            for area_id, attrs in self.client.find(self.config.url, self.config.fields["id"], new, self.token,
                                                    out_fields=f"{self.config.fields['id']},{oid}").items():
                if is_number(attrs.get(oid)):
                    self.oids[area_id] = int(attrs[oid])
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
            raise EsriError(f"{len(failed)} 件をサーバーが受け付けない", code="E305", detail=failed[0])

    def know(self, area_ids: list[str]) -> None:
        """Asks the layer for the object ids of areas not known here (before deleting them for good)."""
        new = [i for i in area_ids if i not in self.oids]
        if not new:
            return
        _, oid = self._layer.ready()
        for area_id, attrs in self.client.find(self.config.url, self.config.fields["id"], new, self.token,
                                                out_fields=f"{self.config.fields['id']},{oid}").items():
            if is_number(attrs.get(oid)):
                self.oids[area_id] = int(attrs[oid])

    def removes_for_good(self) -> bool:
        types, _ = self._layer.ready()
        return not (self.config.soft_delete and self.config.fields["deleted"] in types)

    def remove(self, areas: list[CheckedArea], now: Optional[int] = None) -> list[CheckedArea]:
        marked = _marked(areas, now)
        types, _ = self._layer.ready()
        if self.config.soft_delete and self.config.fields["deleted"] in types:
            self.save(marked)
            return marked
        ids = [self.oids[a.id] for a in areas if a.id in self.oids]
        failed: set[int] = set()
        reason = ""
        if ids:
            # Each one deleted or not on its own: those the server kept stay known, and the person is told (#81).
            for r in self.client.delete_features(self.config.url, ids, token=self.token):
                if not r.get("success") and is_number(r.get("objectId")):
                    failed.add(int(r["objectId"]))
                    reason = reason or (r.get("error") or {}).get("description") or ""
        for a in areas:
            if self.oids.get(a.id, -1) not in failed:
                self.oids.pop(a.id, None)
        if failed:
            raise EsriError(f"確認範囲 {len(failed)} 件の削除をサーバーが受け付けない", code="E305", detail=reason)
        return []


#: The local table's attribute names (the default role names; the individuals' details as JSON in MEMBER_INFO).
DEFAULT_LOCAL_FIELDS = {"path": "PATH", "levels": "LEVELS", "nameLabel": "NAME_LABEL", "members": "MEMBERS", "specs": "SPECS",
                        "photos": "PHOTOS", "activities": "ACTIVITIES", "order": "SORT_ORDER", "memberInfo": "MEMBER_INFO"}


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


def _lines(v: Any) -> list[str]:
    return [p for p in re.split(r"\s*\n\s*", _text(v)) if p]


def _taxon_of_row(a: dict[str, Any], f: dict[str, str]) -> Taxon:
    members = _list(a.get(f["members"])) or None
    info = None
    if f.get("memberInfo"):
        # {name: {specs, photos, note, id}}: the details of the individuals that have some.
        details = _json_of(a.get(f["memberInfo"]), lambda v: v if isinstance(v, dict) else None) or {}
        _, info = members_of([{**v, "name": k} for k, v in details.items() if isinstance(v, dict)])
    return Taxon(
        path=path_of(_text(a.get(f["path"]))),
        levels=_list(a.get(f["levels"])) or None,
        name_label=_text(a.get(f["nameLabel"])).strip() or None,
        members=members,
        specs=_json_of(a.get(f["specs"]), specs_of),
        photos=_lines(a.get(f["photos"])) or None,
        activities=_list(a.get(f["activities"])) or None,
        member_info=info,
    )


def _row_of_taxon(t: Taxon, i: int, f: dict[str, str]) -> dict[str, Any]:
    out = {
        f["path"]: kind_of(t.path), f["levels"]: ",".join(t.levels or []), f["nameLabel"]: t.name_label or "",
        f["members"]: ",".join(t.members or []), f["specs"]: json.dumps(t.specs, ensure_ascii=False, separators=(",", ":")) if t.specs else "",
        f["photos"]: "\n".join(t.photos or []), f["activities"]: ",".join(t.activities or []), f["order"]: i,
    }
    if f.get("memberInfo"):
        details = {}
        for m in t.members or []:
            j = t.member(m).to_json(True)
            if isinstance(j, dict):
                j.pop("name", None)
                details[m] = j
        out[f["memberInfo"]] = json.dumps(details, ensure_ascii=False, separators=(",", ":")) if details else ""
    return out


def _member_of_row(a: dict[str, Any], f: dict[str, str], member_id: str, name: str) -> Member:
    """An individual as its row in a table of individuals says (ALIASES back into its 別名)."""
    specs = dict(_json_of(a.get(f["specs"]), specs_of) or {}) if f.get("specs") else {}
    aliases = split_list(_text(a.get(f["aliases"]))) if f.get("aliases") else []
    if aliases:
        specs[ALIAS_FIELD] = "、".join(aliases)
    photos = _lines(a.get(f["photos"])) if f.get("photos") else []
    note = _text(a.get(f["note"])).strip() if f.get("note") else ""
    return Member(name, specs or None, photos or None, note or None, member_id)


def _row_of_member(path: list[str], m: Member, i: int, f: dict[str, str]) -> dict[str, Any]:
    """An individual's row in a table of individuals (its 別名 in ALIASES, comma separated; the rest of its specs as JSON)."""
    specs = dict(specs_of(m.specs) or {})
    aliases = split_list(specs.pop(ALIAS_FIELD, ""))
    return {
        f["id"]: m.id, f["path"]: kind_of(path), f["name"]: m.name, f["aliases"]: ",".join(aliases),
        f["specs"]: json.dumps(specs, ensure_ascii=False, separators=(",", ":")) if specs else "",
        f["photos"]: "\n".join(m.photos or []), f["note"]: (m.note or "").strip(), f["order"]: i,
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
        #: Each class's row as read (or last saved here): only rows changed here are sent (#74).
        self.rows: dict[str, dict[str, Any]] = {}
        self._fields_row: tuple[Optional[int], Optional[list[SpecField]]] = (None, None)
        self._loaded = False
        self._layer = _EsriLayer(client, config.url, token, "分類のテーブル", [config.fields["path"]])
        m = config.members
        self._members = _EsriLayer(client, m.url, token, "個体のテーブル", [m.fields["id"], m.fields["path"], m.fields["name"]]) if m else None
        #: The table of individuals: each row's object id and attributes as read (or last saved here) by MEMBER_ID, and
        #: the id of each individual by (its class's kind, its name).
        self.member_oids: dict[str, int] = {}
        self.member_rows: dict[str, dict[str, Any]] = {}
        self.member_ids: dict[tuple[str, str], str] = {}

    @property
    def keeps_members(self) -> bool:
        return self._members is not None

    def load(self) -> Optional[list[Taxon]]:
        _, oid = self._layer.ready()
        f = self.config.fields
        rows: list[tuple[Taxon, float]] = []
        self.oids.clear()
        self.rows.clear()
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
            taxon = _taxon_of_row(a, f)
            self.rows[kind_of(path)] = {k: (_text(v) if k != f["order"] else _order_of(v)) for k, v in a.items() if k != oid}
            # The first class is saved as 0: only a missing order goes after the rest.
            rows.append((taxon, order if order is not None else len(rows)))
        self._loaded = True
        if not rows:
            return None
        rows.sort(key=lambda r: r[1])
        taxa = normalize(t for t, _ in rows)
        return self._load_members(taxa) if self._members is not None else taxa

    def _load_members(self, taxa: list[Taxon]) -> list[Taxon]:
        """The classification with what the table of individuals says: each individual's details and id; one it has
        and MEMBERS has not (added there by this version, removed by an older one) comes after the class's others."""
        mc = self.config.members
        _, oid = self._members.ready()
        f = mc.fields
        self.member_oids.clear()
        self.member_rows.clear()
        self.member_ids.clear()
        found: list[tuple[str, str, Member, float]] = []
        for feature in self.client.query_all(mc.url, "1=1", self.token, geometry=False, limit=100_000):
            a = feature.get("attributes") or {}
            member_id = _text(a.get(f["id"])).strip()
            path = path_of(_text(a.get(f["path"])))
            name = segment_name(_text(a.get(f["name"])))
            if not member_id or not path or not name or not is_number(a.get(oid)) or member_id in self.member_oids:
                continue
            self.member_oids[member_id] = int(a[oid])
            self.member_rows[member_id] = {k: (_order_of(v) if k == f["order"] else _text(v)) for k, v in a.items() if k != oid}
            key = (kind_of(path), name)
            # The same individual twice (added by two people at once): the first is kept, the other deleted at the next save.
            if key in self.member_ids:
                continue
            self.member_ids[key] = member_id
            order = _order_of(a.get(f["order"]))
            found.append((key[0], name, _member_of_row(a, f, member_id, name), order if order is not None else 1e12))
        by_kind = {kind_of(t.path): t for t in taxa}
        for kind, name, member, _ in sorted(found, key=lambda r: r[3]):
            t = by_kind.get(kind)
            if t is None:
                continue  # A class no longer there: its individuals' rows go with the next save.
            if name not in (t.members or []):
                t.members = [*(t.members or []), name]
            t.member_info = {**(t.member_info or {}), name: member}
        # Those with no row yet (MEMBERS only) get their id now: a rename before their first save keeps it.
        for t in taxa:
            for name in t.members or []:
                if not t.member(name).id:
                    member_id = self.member_ids.setdefault((kind_of(t.path), name), new_member_id())
                    t.member_info = {**(t.member_info or {}), name: Member(name, id=member_id)}
        return taxa

    def save(self, taxa: list[Taxon]) -> None:
        types, oid = self._layer.ready()
        f = self.config.fields
        everything = normalize(taxa)

        def attributes_of(t: Taxon, i: int) -> dict[str, Any]:
            out = {k: v for k, v in _row_of_taxon(t, i, f).items() if k in types}
            if kind_of(t.path) in self.oids:
                out[oid] = self.oids[kind_of(t.path)]
            return {"attributes": out}

        def unchanged(t: Taxon, i: int) -> bool:
            seen = self.rows.get(kind_of(t.path))
            if seen is None:
                return False
            row = attributes_of(t, i)["attributes"]
            return all((_order_of(v) if k == f["order"] else _text(v)) == seen.get(k, None if k == f["order"] else "")
                       for k, v in row.items() if k != oid)

        kept = {kind_of(t.path) for t in everything}
        # Rows added by an earlier save whose answer was lost (or by someone else, the same class): changed, not added (#73).
        new = [kind_of(t.path) for t in everything if kind_of(t.path) not in self.oids]
        if new:
            for kind, a in self.client.find(self.config.url, f["path"], new, self.token, out_fields=f"{f['path']},{oid}").items():
                if is_number(a.get(oid)):
                    self.oids[kind] = int(a[oid])
        deletes = [i for k, i in self.oids.items() if k not in kept]
        adds = [(t, i) for i, t in enumerate(everything) if kind_of(t.path) not in self.oids]
        # Only the rows changed here: what someone else changed in the others is kept (#74).
        updates = [(t, i) for i, t in enumerate(everything) if kind_of(t.path) in self.oids and not unchanged(t, i)]
        failed: list[str] = []
        if adds or updates or deletes:
            self._apply_classes(adds, updates, deletes, attributes_of, failed)
        if self._members is not None:
            self._save_members(everything, failed)
        if failed:
            raise EsriError(f"分類 {len(failed)} 件をサーバーが受け付けない", code="E305", detail=failed[0])

    def _apply_classes(self, adds, updates, deletes, attributes_of, failed: list[str]) -> None:
        f = self.config.fields
        _, oid = self._layer.ready()
        result = self.client.apply_edits(self.config.url, [attributes_of(t, i) for t, i in adds],
                                         [attributes_of(t, i) for t, i in updates], deletes, token=self.token)

        def remember(t: Taxon, i: int) -> None:
            self.rows[kind_of(t.path)] = {k: (_order_of(v) if k == f["order"] else _text(v))
                                          for k, v in attributes_of(t, i)["attributes"].items() if k != oid}

        for (t, i), r in zip(adds, result.add_results):
            if r.get("success") and is_number(r.get("objectId")):
                self.oids[kind_of(t.path)] = int(r["objectId"])
                remember(t, i)
            elif not r.get("success"):
                failed.append((r.get("error") or {}).get("description") or "追加失敗")
        for (t, i), r in zip(updates, result.update_results):
            if r.get("success"):
                remember(t, i)
            else:
                failed.append((r.get("error") or {}).get("description") or "更新失敗")
        # Rows the server did not delete stay known (deleted again with the next save).
        undeleted = {int(r["objectId"]) for r in result.delete_results if not r.get("success") and is_number(r.get("objectId"))}
        if undeleted:
            failed.append("削除失敗")
        gone = {i for i in deletes if i not in undeleted}
        for k, i in list(self.oids.items()):
            if i in gone:
                del self.oids[k]
                self.rows.pop(k, None)

    def _save_members(self, taxa: list[Taxon], failed: list[str]) -> None:
        """The table of individuals as the classification says: rows added, changed and deleted (only those changed
        here are sent; an individual someone else just added is found and changed, not added twice)."""
        mc = self.config.members
        types, oid = self._members.ready()
        f = mc.fields
        wanted: dict[str, dict[str, Any]] = {}
        for t in taxa:
            kind = kind_of(t.path)
            for i, name in enumerate(t.members or []):
                m = t.member(name)
                member_id = m.id or self.member_ids.get((kind, name)) or new_member_id()
                if member_id in wanted:  # The same id twice (a copied class): the second is another individual.
                    member_id = new_member_id()
                self.member_ids[(kind, name)] = member_id
                row = _row_of_member(t.path, Member(name, m.specs, m.photos, m.note, member_id), i, f)
                wanted[member_id] = {k: v for k, v in row.items() if k in types}
        new = [k for k in wanted if k not in self.member_oids]
        if new:
            names = [wanted[k][f["name"]] for k in new]
            have = self.client.find(mc.url, f["name"], names, self.token, out_fields="*")
            for k in new:
                a = have.get(wanted[k][f["name"]])
                other = _text((a or {}).get(f["id"])).strip()
                if (a and is_number(a.get(oid)) and other and other not in self.member_oids and other not in wanted
                        and _text(a.get(f["path"])) == wanted[k][f["path"]]):
                    row = wanted.pop(k)
                    row[f["id"]] = other
                    wanted[other] = row
                    self.member_oids[other] = int(a[oid])
                    self.member_rows[other] = {x: (_order_of(v) if x == f["order"] else _text(v)) for x, v in a.items() if x != oid}
                    self.member_ids[(row[f["path"]], row[f["name"]])] = other

        def same(member_id: str) -> bool:
            seen = self.member_rows.get(member_id) or {}
            return all((_order_of(v) if k == f["order"] else _text(v)) == seen.get(k, None if k == f["order"] else "")
                       for k, v in wanted[member_id].items())

        adds = [k for k in wanted if k not in self.member_oids]
        updates = [k for k in wanted if k in self.member_oids and not same(k)]
        deletes = [i for k, i in self.member_oids.items() if k not in wanted]
        if not (adds or updates or deletes):
            return
        result = self.client.apply_edits(mc.url, [{"attributes": wanted[k]} for k in adds],
                                         [{"attributes": {**wanted[k], oid: self.member_oids[k]}} for k in updates], deletes, token=self.token)
        for k, r in zip(adds, result.add_results):
            if r.get("success") and is_number(r.get("objectId")):
                self.member_oids[k] = int(r["objectId"])
                self.member_rows[k] = dict(wanted[k])
            else:
                failed.append((r.get("error") or {}).get("description") or "個体の追加失敗")
        for k, r in zip(updates, result.update_results):
            if r.get("success"):
                self.member_rows[k] = dict(wanted[k])
            else:
                failed.append((r.get("error") or {}).get("description") or "個体の更新失敗")
        undeleted = {int(r["objectId"]) for r in result.delete_results if not r.get("success") and is_number(r.get("objectId"))}
        if undeleted:
            failed.append("個体の削除失敗")
        for k, i in list(self.member_oids.items()):
            if i in deletes and i not in undeleted:
                del self.member_oids[k]
                self.member_rows.pop(k, None)
        for key in [key for key, k in self.member_ids.items() if k not in self.member_oids and k not in wanted]:
            del self.member_ids[key]

    def lacks(self, role: str) -> Optional[str]:
        """The attribute of a role the table does not have (its classes' values for it are not saved), else None."""
        types, _ = self._layer.ready()
        name = self.config.fields.get(role)
        return name if name and name not in types else None

    def lacks_member(self, role: str) -> Optional[str]:
        """The attribute of a role the table of individuals does not have (not saved), else None (also with no such table)."""
        if self._members is None:
            return None
        types, _ = self._members.ready()
        name = self.config.members.fields.get(role)
        return name if name and name not in types else None

    def load_fields(self) -> Optional[list[SpecField]]:
        # Read with the classes.
        if not self._loaded:
            self.load()
        return self._fields_row[1]

    def save_fields(self, fields: list[SpecField]) -> None:
        types, oid = self._layer.ready()
        f = self.config.fields
        if f["specs"] not in types:
            raise EsriError(f"分類の表に必要な項目がない（管理者に連絡: {f['specs']}）", code="E704")
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
            raise EsriError(code="E305", detail="諸元の項目: " + (((first or {}).get("error") or {}).get("description") or "応答なし"))
        new_oid = row_oid if row_oid is not None else (int(first["objectId"]) if is_number(first.get("objectId")) else None)
        self._fields_row = (new_oid, list(fields))


def taxonomy_json(taxa: list[Taxon]) -> list[dict[str, Any]]:
    """A classification as the viewer writes a flat list."""
    return [t.to_json() for t in normalize(taxa)]


__all__ = [
    "LOCAL_LABEL", "LocalObservationStore", "LocalAreaStore", "LocalTaxonomyStore", "LocalPhotoStore", "EsriObservationStore",
    "EsriAreaStore", "EsriTaxonomyStore", "is_local_photo", "photo_url", "taxa_from_json", "taxonomy_json",
]
