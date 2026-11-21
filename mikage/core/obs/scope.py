"""
What to ask an observation store for, instead of every record (2026-11-18:
200 people at once; tsudo 「本当に観測記録全件取る意味あるの？」). A feature
asks for the records it needs: of some records, images, things or names, of a
class, of a period, inside a box, made by someone. A RecordQuery is written
as the layer's where (and an envelope for the box) for an Esri store, and
tests a record the same way for the local store and for what waits to be sent.

The records around an image (its own, the latest near its footprint and
time, and their things' records then) are read by the store
(EsriObservationStore.load_around), from the same parts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..esri import like_text, quote
from .. import sql
from ..util import utc_text
from .flows import time_key
from .model import Observation
from .taxonomy import kind_of, path_of, within

#: Values in one `IN (…)` (a long list is asked in parts).
IN_CHUNK = 100
#: Which time a period is of: the image's (else when made), when made, when last changed.
BASES = ("image", "created", "updated")


def time_condition(name: str, field_type: Optional[str], op: str, t: float) -> str:
    """`name op t` (op one of >= <= > <), written for the field's type (as catalog.watch.since_condition)."""
    if field_type == "esriFieldTypeDateOnly":
        return f"{name} {op} {sql.date_of(t)}"
    if field_type and re.search(r"Integer|Double|Single", field_type):
        return f"{name} {op} {int(t)}"
    if field_type == "esriFieldTypeString":
        return f"{name} {op} '{utc_text(t)[:10]}'"
    return f"{name} {op} {sql.timestamp(t)}"


def _between(name: str, field_type: Optional[str], start: Optional[float], end: Optional[float]) -> list[str]:
    return ([time_condition(name, field_type, ">=", start)] if start is not None else []) + \
        ([time_condition(name, field_type, "<=", end)] if end is not None else [])


def basis_time(o: Observation, basis: str) -> Optional[float]:
    """A record's time of a basis (BASES)."""
    if basis == "created":
        return o.created_at
    if basis == "updated":
        return o.updated_at if o.updated_at is not None else o.created_at
    return time_key(o)


@dataclass(frozen=True)
class RecordQuery:
    """
    Which records. The lists of ids, images, things and names are ORed (a
    record of any of them; none given: no such limit); the rest is ANDed.
    """
    ids: tuple = ()
    image_ids: tuple = ()
    object_ids: tuple = ()
    names: tuple = ()
    #: Of these classes (each a kind `A > B`, with what is below it); any of them.
    kinds: tuple = ()
    #: The period (ms, inclusive; None open) of a time (BASES).
    basis: str = "image"
    start: Optional[float] = None
    end: Optional[float] = None
    #: Inside [west, south, east, north] (WGS 84).
    box: Optional[tuple] = None
    #: Made by one of these (記録者, capitals aside).
    observers: tuple = ()
    #: Only those not deleted.
    living: bool = False

    @property
    def lists(self) -> bool:
        return bool(self.ids or self.image_ids or self.object_ids or self.names)

    def everything(self) -> bool:
        """Whether nothing limits it (every record: refused by the stores that ask a server)."""
        return not (self.lists or self.kinds or self.start is not None or self.end is not None or self.box or self.observers)

    def matches(self, o: Observation) -> bool:
        if self.living and o.deleted_at is not None:
            return False
        if self.lists and not (o.id in self.ids or o.image_id in self.image_ids or o.object_id in self.object_ids
                               or o.object_name in self.names):
            return False
        if self.kinds and not any(within(o.kind, path_of(k)) for k in self.kinds):
            return False
        if self.start is not None or self.end is not None:
            t = basis_time(o, self.basis)
            if t is None or (self.start is not None and t < self.start) or (self.end is not None and t > self.end):
                return False
        if self.box:
            w, s, e, n = self.box
            if not (w <= o.lon <= e and s <= o.lat <= n):
                return False
        if self.observers:
            mine = {n.strip().upper() for n in self.observers}
            if (o.recorded_by or "").strip().upper() not in mine:
                return False
        return True

    def wheres(self, fields: dict, types: dict, base: Optional[str] = None) -> list[str]:
        """
        The layer's where for it (several when a list is long: each asks for
        IN_CHUNK values of one list; the records of all of them are the
        answer), ANDed with `base` (the settings file's `where`). Parts on an
        attribute the layer lacks are left out (the record is then tested
        here, by `matches`).
        """
        def has(role: str) -> Optional[str]:
            name = fields.get(role)
            return name if name and name in types else None

        ands = [f"({base})"] if base else []
        if self.living and has("deleted"):
            ands.append(f"{fields['deleted']} IS NULL")
        if self.kinds and has("kind"):
            k = fields["kind"]
            parts = []
            for kind in dict.fromkeys(kind_of(path_of(x)) for x in self.kinds):
                parts += [f"{k} = {quote(kind)}", f"{k} LIKE '{like_text(kind)} > %' ESCAPE '\\'"]
            ands.append("(" + " OR ".join(parts) + ")")
        if self.start is not None or self.end is not None:
            ands += self._period(fields, types)
        if self.observers and has("recordedBy"):
            names = ", ".join(quote(n.strip().upper()) for n in dict.fromkeys(self.observers) if n.strip())
            if names:
                ands.append(f"UPPER({fields['recordedBy']}) IN ({names})")
        if not self.lists:
            return [" AND ".join(ands) or "1=1"]
        out = []
        for role, values in (("id", self.ids), ("imageId", self.image_ids), ("objectId", self.object_ids), ("objectName", self.names)):
            values = [v for v in dict.fromkeys(values) if v not in (None, "")]
            if not values or not has(role):
                continue
            for i in range(0, len(values), IN_CHUNK):
                listed = f"{fields[role]} IN ({','.join(quote(v) for v in values[i:i + IN_CHUNK])})"
                out.append(" AND ".join([*ands, listed]))
        return out

    def _period(self, fields: dict, types: dict) -> list[str]:
        if self.basis in ("created", "updated"):
            name = fields.get("created" if self.basis == "created" else "updated")
            return _between(name, types.get(name), self.start, self.end) if name in types else []
        image, created = fields.get("imageTime"), fields.get("created")
        if image not in types:
            return _between(created, types.get(created), self.start, self.end) if created in types else []
        timed = " AND ".join(_between(image, types.get(image), self.start, self.end))
        if created not in types:
            return [timed]
        untimed = " AND ".join([f"{image} IS NULL", *_between(created, types.get(created), self.start, self.end)])
        return [f"(({timed}) OR ({untimed}))"]


def grown(box: Sequence[float], metres: float) -> tuple:
    """A box [w, s, e, n] grown by `metres` on every side (degrees at its latitude)."""
    import math

    w, s, e, n = box
    dy = metres / 111_320
    middle = math.radians((s + n) / 2)
    dx = metres / max(1.0, 111_320 * math.cos(middle))
    return (max(-180.0, w - dx), max(-90.0, s - dy), min(180.0, e + dx), min(90.0, n + dy))


def envelope(box: Sequence[float]) -> dict:
    """The query's envelope for a box (WGS 84)."""
    w, s, e, n = box
    return {"geometry": f'{{"xmin":{w},"ymin":{s},"xmax":{e},"ymax":{n},"spatialReference":{{"wkid":4326}}}}',
            "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}


def merged(known: Iterable[Observation], read: Iterable[Observation]) -> list[Observation]:
    """`known` with what was `read` put over it (the same record replaced, a new one added)."""
    out = {o.id: o for o in known}
    for o in read:
        out[o.id] = o
    return list(out.values())
