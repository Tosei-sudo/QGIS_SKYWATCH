"""
画像の評価 (tsudo, 2026-11-24: 「4,5,7を実装して」「4は、形状ありで、ネームド
エリアごと1行にしようか」; 設計方針案「画像の評価・システムの障害・参考情報」):
when the person reading an image is done with it, they say for each named
area the image shows whether it can be used (使える・一部・使えない), the
cloud they saw and why, one row per image × named area in an Esri polygon
layer (`imageReviews`, TBL_IMAGE_REVIEW). Its shape is the part of the
named area the image shows (the named area's polygon cut by the image's
footprint), so maps and dashboards can colour it as it is. A row without a
named area (NAMED_AREA_ID and NAMED_AREA_NAME empty) is the whole image
(「画像全体」: the sea far from any named place, too).

The rows are written over, not added again: the same image and named area
rated again (by anyone) is the same row, its 評価者 the last one. A named
area without a 管理番号 is told apart by its name. Clearing a rating deletes
its row.

What it is used for (pure here; the windows in ui/image_review_dialog.py):
- the 分担 line of the observation dock: 「評価: 使える 2・使えない 1」 and
  the window, opened by 完了 when the image has none yet;
- the image catalog's column 「評価」 (catalog_text);
- the follow-up of requests (照合, core/request/follow.py): an image whose
  ratings over the request's area are all 使えない does not count as taken
  (unusable_over); a row already 撮像済み is only marked (unusable_text).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional, Sequence

from .esri import EsriClient, EsriError, fit_value, quote
from .shapes import esri_rings, rings_overlap
from .sources import EsriSource, esri_source_of
from .util import is_number, js_string, ms_of
from . import edit_stamp

#: The ratings, best first; 使えない is what the follow-up of requests leaves aside.
GOOD, PART, BAD = "使える", "一部", "使えない"
RATINGS = (GOOD, PART, BAD)
#: The row of the whole image (no named area).
WHOLE = "画像全体"
#: The table's attributes by role.
DEFAULT_REVIEW_FIELDS = {
    "id": "REVIEW_ID", "imageId": "IMAGE_ID", "imageName": "IMAGE_NAME", "imageTime": "IMAGE_TIME", "satellite": "SATELLITE",
    "areaId": "NAMED_AREA_ID", "areaName": "NAMED_AREA_NAME", "rating": "RATING", "cloud": "CLOUD_PCT", "reasons": "REASONS",
    "note": "NOTE", "reviewedBy": "REVIEWED_BY", "reviewedAt": "REVIEWED_AT",
}
#: The attributes without which the table cannot be used.
NEEDED = ("id", "imageId", "rating")
#: The colours of the ratings (the window, the catalog's column, the marks).
COLORS = {GOOD: "#2e7d32", PART: "#b26a00", BAD: "#c62828"}
#: The reasons are written with commas (or 、).
_SPLIT = re.compile(r"[,、，;；\n]+")
#: Image ids asked at a time.
CHUNK = 200


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def reasons_of(value: Any) -> tuple[str, ...]:
    """`雲,霞` → ('雲', '霞'), each once."""
    return tuple(dict.fromkeys(p.strip() for p in _SPLIT.split(_text(value)) if p.strip()))


def rating_of(value: Any) -> str:
    """A rating as written (使える・一部・使えない; ○△× and the English words too); '' for another."""
    t = _text(value)
    if t in RATINGS:
        return t
    return {"○": GOOD, "◯": GOOD, "△": PART, "×": BAD, "✕": BAD, "good": GOOD, "usable": GOOD, "partial": PART, "part": PART,
            "bad": BAD, "unusable": BAD}.get(t.casefold(), "")


def new_id(now: int) -> str:
    """`REV-20261124-213000-1a2b` (the moment in JST and a few random letters)."""
    import secrets
    from datetime import datetime, timedelta, timezone

    at = datetime.fromtimestamp(now / 1000, timezone(timedelta(hours=9)))
    return f"REV-{at:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@dataclass
class ImageReview:
    """One row: an image's rating over one named area (or the whole image)."""
    image_id: str
    rating: str
    #: The named area's 管理番号 and name ('' and '' for the whole image).
    area_id: str = ""
    area_name: str = ""
    #: The cloud seen (%), None when not said.
    cloud: Optional[float] = None
    reasons: tuple[str, ...] = ()
    note: str = ""
    image_name: str = ""
    image_time: Optional[int] = None
    satellite: str = ""
    reviewed_by: str = ""
    reviewed_at: Optional[int] = None
    id: str = ""
    oid: Optional[int] = None
    #: The shape (WGS 84 rings): the named area cut by the footprint; the footprint for the whole image. None when not read.
    rings: Optional[list] = field(default=None, repr=False)

    @property
    def whole(self) -> bool:
        return not self.area_id and not self.area_name

    @property
    def key(self) -> str:
        return area_key(self.area_id, self.area_name)

    @property
    def label(self) -> str:
        return self.area_name or self.area_id or WHOLE

    def detail_text(self) -> str:
        """`使えない（雲 80%・霞）` — the rating, the cloud and the reasons."""
        extra = ([f"雲 {self.cloud:g}%"] if self.cloud is not None else []) + list(self.reasons)
        return self.rating + (f"（{'・'.join(extra)}）" if extra else "")


def area_key(area_id: str, area_name: str) -> str:
    """What tells the rows of an image apart: the 管理番号, else the name (「名前:…」), '' for the whole image."""
    if area_id.strip():
        return area_id.strip()
    return f"名前:{area_name.strip()}" if area_name.strip() else ""


# ---- the settings ----------------------------------------------------------------------------------


def review_source_of(value: Any, problems: list[str]) -> Optional[EsriSource]:
    """`imageReviews`: the URL of the editable polygon layer (TBL_IMAGE_REVIEW), or { url, fields, where, token }."""
    return esri_source_of(value, "imageReviews", DEFAULT_REVIEW_FIELDS, problems, editable=True)


# ---- reading and writing ---------------------------------------------------------------------------


def review_of(a: dict, fields: dict[str, str], oid_field: str = "OBJECTID", rings: Optional[list] = None) -> Optional[ImageReview]:
    """A row as a review; None without its image or a rating it knows."""
    def get(role: str) -> Any:
        name = fields.get(role)
        return a.get(name) if name else None

    image_id, rating = _text(get("imageId")), rating_of(get("rating"))
    if not image_id or not rating:
        return None
    cloud = get("cloud")
    oid = a.get(oid_field)
    return ImageReview(
        image_id=image_id, rating=rating, area_id=_text(get("areaId")), area_name=_text(get("areaName")),
        cloud=float(cloud) if is_number(cloud) else None, reasons=reasons_of(get("reasons")), note=_text(get("note")),
        image_name=_text(get("imageName")), image_time=ms_of(get("imageTime")), satellite=_text(get("satellite")),
        reviewed_by=_text(get("reviewedBy")), reviewed_at=ms_of(get("reviewedAt")), id=_text(get("id")),
        oid=int(oid) if is_number(oid) else None, rings=rings,
    )


def attributes_of(r: ImageReview, fields: dict[str, str], types: dict[str, str], lengths: Optional[dict[str, int]] = None) -> dict:
    """The row as the table takes it (only the attributes it has; text cut to their lengths)."""
    values = {
        "id": r.id, "imageId": r.image_id, "imageName": r.image_name or None, "imageTime": r.image_time,
        "satellite": r.satellite or None, "areaId": r.area_id or None, "areaName": r.area_name or None, "rating": r.rating,
        "cloud": r.cloud, "reasons": ",".join(r.reasons) or None, "note": r.note or None, "reviewedBy": r.reviewed_by or None,
        "reviewedAt": r.reviewed_at,
    }
    lengths = lengths or {}
    out = {}
    for role, v in values.items():
        name = fields.get(role)
        if not name or name not in types:
            continue
        if isinstance(v, str) and lengths.get(name):
            v = v[:lengths[name]]
        out[name] = fit_value(v, types[name])
    return out


def _types(client: EsriClient, source: EsriSource, token: Optional[str], refresh: bool = False):
    info = client.layer(source.url, token, refresh=refresh)
    missing = [source.fields[r] for r in NEEDED if source.fields[r] not in info.field_types]
    if missing:
        raise EsriError(f"画像の評価の表に必要な項目がない（管理者に連絡: {'・'.join(missing)}）", code="E704")
    return info


def _where(source: EsriSource, extra: str) -> str:
    return " AND ".join(f"({c})" for c in (source.where, extra) if c) or "1=1"


def load_reviews(client: EsriClient, source: EsriSource, image_ids: Iterable[str], token: Optional[str] = None,
                 geometry: bool = False) -> dict[str, list[ImageReview]]:
    """The rows of these images by image id (images without one left out), the whole image first, then by name."""
    info = _types(client, source, token)
    f = source.fields
    ids = list(dict.fromkeys(i for i in image_ids if i))
    out: dict[str, list[ImageReview]] = {}
    for start in range(0, len(ids), CHUNK):
        part = ids[start:start + CHUNK]
        where = f"{f['imageId']} IN ({', '.join(quote(x) for x in part)})"
        for row in client.query_all(source.url, _where(source, where), token, geometry=geometry, limit=1_000_000):
            rings = (row.get("geometry") or {}).get("rings") if geometry else None
            r = review_of(row.get("attributes") or {}, f, info.object_id_field, rings)
            if r is not None:
                out.setdefault(r.image_id, []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: (not r.whole, r.label.casefold(), r.oid or 0))
    return out


@dataclass
class Saved:
    """What save_reviews did: the image's rows now, and how many were added, written over and deleted."""
    rows: list[ImageReview]
    added: int = 0
    updated: int = 0
    deleted: int = 0
    problems: list[str] = field(default_factory=list)


def save_reviews(client: EsriClient, source: EsriSource, image_id: str, wanted: Sequence[ImageReview], cleared: Iterable[str] = (),
                 token: Optional[str] = None, now: Optional[int] = None, user: str = "") -> Saved:
    """
    Writes an image's ratings: each of `wanted` over the row of the same
    image and named area (whoever made it), else as a new row; the rows of
    the keys in `cleared` (a rating put back to empty) deleted. Two rows of
    one key (two people at the same moment) are one again afterwards: the
    first kept, written with the last rating.
    """
    info = _types(client, source, token, refresh=True)
    f = source.fields
    types, oid = info.field_types, info.object_id_field
    lengths = getattr(info, "field_lengths", {}) or {}
    now = now if now is not None else edit_stamp.now_ms()
    user = user or edit_stamp.user_name()
    polygon = info.geometry_type == "esriGeometryPolygon"
    before = load_reviews(client, source, [image_id], token).get(image_id, [])
    by_key: dict[str, list[ImageReview]] = {}
    for r in before:
        by_key.setdefault(r.key, []).append(r)
    adds, updates, deletes = [], [], []
    for w in wanted:
        if not rating_of(w.rating):
            continue
        have = sorted(by_key.get(w.key, []), key=lambda r: r.oid or 0)
        row = replace(w, image_id=image_id, reviewed_by=user, reviewed_at=now, id=have[0].id if have and have[0].id else (w.id or new_id(now)))
        feature: dict[str, Any] = {"attributes": attributes_of(row, f, types, lengths)}
        if polygon and row.rings:
            feature["geometry"] = {"rings": esri_rings(row.rings), "spatialReference": {"wkid": 4326}}
        if have and have[0].oid is not None:
            feature["attributes"][oid] = have[0].oid
            updates.append(feature)
            deletes += [r.oid for r in have[1:] if r.oid is not None]
        else:
            adds.append(feature)
    wanted_keys = {w.key for w in wanted}
    for key in set(cleared) - wanted_keys:
        deletes += [r.oid for r in by_key.get(key, []) if r.oid is not None]
    if not (adds or updates or deletes):
        return Saved(before)
    result = client.apply_edits(source.url, adds=adds, updates=updates, deletes=deletes, token=token)
    problems = [((r.get("error") or {}).get("description") or "保存できない行") for r in
                (*(result.add_results or []), *(result.update_results or []), *(result.delete_results or [])) if not r.get("success")]
    after = load_reviews(client, source, [image_id], token).get(image_id, [])
    # Added at the same moment by two PCs: the later rows of a key deleted (the first keeps the key).
    twice: list[int] = []
    seen: dict[str, ImageReview] = {}
    for r in sorted(after, key=lambda r: r.oid or 0):
        if r.key in seen and r.oid is not None:
            twice.append(r.oid)
        else:
            seen[r.key] = r
    if twice:
        client.delete_features(source.url, twice, token)
        after = [r for r in after if r.oid not in twice]
    return Saved(after, added=len(adds), updated=len(updates), deleted=len(deletes), problems=problems)


# ---- what is shown ---------------------------------------------------------------------------------


def counts_of(rows: Iterable[ImageReview]) -> dict[str, int]:
    """How many rows of each rating (in the order of RATINGS; ratings with none left out)."""
    rows = list(rows)
    return {k: n for k in RATINGS if (n := sum(1 for r in rows if r.rating == k))}


def summary_text(rows: Sequence[ImageReview]) -> str:
    """`評価: 使える 2・使えない 1`, `評価: 画像全体 使えない（雲）`, `評価: なし`."""
    if not rows:
        return "評価: なし"
    if len(rows) == 1 and rows[0].whole:
        return f"評価: {WHOLE} {rows[0].detail_text()}"
    return "評価: " + "・".join(f"{k} {n}" for k, n in counts_of(rows).items())


def catalog_text(rows: Sequence[ImageReview]) -> str:
    """
    The catalog's column: the whole image's rating, else the named areas'
    together: one rating when they all agree, else `一部（使えない 1/3）`
    (`一部（一部 2/3）` without 使えない); '' with none.
    """
    if not rows:
        return ""
    whole = next((r for r in rows if r.whole), None)
    if whole is not None:
        return whole.rating
    counts = counts_of(rows)
    if len(counts) == 1:
        return next(iter(counts))
    n = len(rows)
    if BAD in counts:
        return f"{PART}（{BAD} {counts[BAD]}/{n}）"
    return f"{PART}（{PART} {counts.get(PART, 0)}/{n}）"


def catalog_rating(rows: Sequence[ImageReview]) -> str:
    """The rating the catalog's column is coloured by: the whole image's, else the named areas' when they agree, else 一部."""
    text = catalog_text(rows)
    return next((k for k in RATINGS if text == k), PART if text else "")


def lines_of(rows: Sequence[ImageReview]) -> list[str]:
    """One line a row for a tooltip: `呉港: 使えない（雲 80%）`, the person and when kept out."""
    return [f"{r.label}: {r.detail_text()}" + (f"　{r.note}" if r.note else "") for r in rows]


# ---- the follow-up of requests ---------------------------------------------------------------------


def over(rows: Sequence[ImageReview], rings: Sequence) -> list[ImageReview]:
    """The rows of named areas whose shape overlaps an area (rows without a shape left out)."""
    return [r for r in rows if not r.whole and r.rings and rings_overlap(r.rings, rings)]


def unusable_over(rows: Sequence[ImageReview], rings: Sequence) -> bool:
    """
    Whether an image cannot be used for an area (設計 2): the ratings of the
    named areas over it are all 使えない; with none over it, the whole
    image's is 使えない. 一部 is used (it counts as taken).
    """
    near = over(rows, rings) if rings else []
    if near:
        return all(r.rating == BAD for r in near)
    whole = next((r for r in rows if r.whole), None)
    return whole is not None and whole.rating == BAD


def unusable_text(rows: Sequence[ImageReview], rings: Sequence) -> str:
    """`画像の評価: 使えない（雲）` for an image unusable over an area (the reasons of its rows); '' otherwise."""
    if not unusable_over(rows, rings):
        return ""
    near = over(rows, rings) if rings else []
    chosen = near or [r for r in rows if r.whole]
    reasons = list(dict.fromkeys(x for r in chosen for x in r.reasons))
    return f"画像の評価: {BAD}" + (f"（{'・'.join(reasons)}）" if reasons else "")


class UnusableImages:
    """
    For the follow-up of requests (core/request/follow.py, which knows no
    observations: it is given this): whether an image cannot be used for an
    area (unusable_over), its ratings read as asked (with their shapes) and
    kept for the run. A reading that fails counts as no rating (the image
    is taken as before) and is said in `problems`.
    """

    def __init__(self, client: EsriClient, source: EsriSource, token: Optional[str] = None):
        self.client, self.source, self.token = client, source, token
        self.rows: dict[str, list[ImageReview]] = {}
        self.problems: list[str] = []
        #: Images left out of a follow-up for their ratings (id → the text of why), for the log.
        self.left: dict[str, str] = {}

    def read(self, image_ids: Iterable[str]) -> None:
        """Reads the ratings of the images not read yet (one query for many)."""
        ids = [i for i in dict.fromkeys(image_ids) if i and i not in self.rows]
        if not ids:
            return
        try:
            found = load_reviews(self.client, self.source, ids, self.token, geometry=True)
        except EsriError as error:
            if not self.problems:
                self.problems.append(f"画像の評価を読めないので、評価を見ずに照合した（{error}）")
            found = {}
        for i in ids:
            self.rows[i] = found.get(i, [])

    def __call__(self, image_id: str, rings: Sequence) -> bool:
        """Whether the image is all 使えない over the area (its ratings read first when not read yet)."""
        self.read([image_id])
        text = unusable_text(self.rows.get(image_id, []), rings)
        if text:
            self.left[image_id] = text
        return bool(text)

    def text(self, image_id: str, rings: Sequence) -> str:
        """`画像の評価: 使えない（雲）` of an image over an area ('' when it is usable or not rated)."""
        self.read([image_id])
        return unusable_text(self.rows.get(image_id, []), rings)


# ---- the window's first ratings from the 不可範囲 ----------------------------------------------------


def from_unseen(share: float, reasons: Sequence[str], half: float = 0.5) -> tuple[str, tuple[str, ...]]:
    """
    A first rating of a named area from the 不可範囲 over it (設計: half or
    more of it → 使えない, some → 一部, none → '' (nothing said)); its reasons.
    """
    if share >= half - 1e-9:
        return BAD, tuple(dict.fromkeys(reasons))
    if share > 1e-6:
        return PART, tuple(dict.fromkeys(reasons))
    return "", ()


__all__ = [
    "BAD", "COLORS", "DEFAULT_REVIEW_FIELDS", "GOOD", "ImageReview", "PART", "RATINGS", "Saved", "WHOLE", "area_key",
    "catalog_rating", "catalog_text", "counts_of", "from_unseen", "lines_of", "load_reviews", "over", "rating_of", "reasons_of",
    "review_of", "review_source_of", "save_reviews", "summary_text", "UnusableImages", "unusable_over", "unusable_text",
]
