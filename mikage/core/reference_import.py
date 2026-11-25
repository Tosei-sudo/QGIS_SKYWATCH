"""
参考情報の JSON 取り込み (tsudo, 2026-11-25: 「参考情報をJSONからバルクインポート
できるようにしてほしい フォーマットは任せるけど、JSONスキームを作成してほしい」):
infos written in a JSON file (a list of navigational warnings, another
system's output) added to or updated in the info layer (core/reference_info.py)
in one go.

The file's shape is resources/schemas/reference-info.schema.json, and the file
is checked with it (one definition; the repository keeps it for whoever writes
the files):

    { "format": "mikage-reference-info", "version": 1,
      "defaults": { "source": "海上保安庁" },
      "infos": [ { "id": "NAV-2026-0123", "title": "…", "time": "2026-11-28T09:00:00+09:00",
                   "area": "NA-000012", "links": [ { "type": "event", "id": "EV-…" } ] } ] }

1. `read_file` / `parse_text`: the JSON, checked against the schema (a file
   that does not fit as a whole is refused; a row that does not is an error
   row), `defaults` put into the rows that do not have those keys.
2. `plan_import`: a dry run against the layer: each row 新規 (its `id` not
   there, or no `id`: a new REF-… one), 更新 (only the keys written change; ""
   or null clears; new links count), 変更なし, 重複 (no `id`, and an info of the
   same title, time to the minute and source is there or earlier in the
   file) or エラー, with what changes and why. The place: `lon`/`lat`, else a
   point inside the named area `area` (its 管理番号, else its name or an alias),
   else none. Errors: a time that is not a day, one of lon/lat only, an area
   not found or two of that name, text longer than the layer's attribute
   (never cut), the same id twice. A reliability or class not among the
   known ones is only a warning.
3. `apply_import`: plans again (what is there now) and saves the new and
   changed infos, then adds the links (add only; the same pair stays one
   row); error rows are skipped, the rest go through, and what the server
   did not take is told.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Sequence

from .esri import EsriClient, EsriError
from .json_schema import validate
from .named_areas import OrgArea
from .reference_info import (
    LinkTarget, ReferenceInfo, ReferenceSettings, _info_layer, _token, add_link_pairs, feature_of,
    load_infos, load_links, new_id,
)
from .shapes import point_inside
from .time_text import parse_time

#: The schema file (in the plugin and the repository).
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "schemas",
                           "reference-info.schema.json")
FORMAT = "mikage-reference-info"
VERSION = 1
#: The keys `defaults` may give.
DEFAULT_KEYS = ("published", "source", "sourceRef", "reliability", "summary", "kind", "area", "note")
#: The texts of a row: its key → the info's attribute and the layer's role.
TEXTS = {"title": ("title", "title"), "source": ("source", "source"), "sourceRef": ("source_ref", "sourceRef"),
         "reliability": ("reliability", "reliability"), "summary": ("summary", "summary"), "kind": ("kind", "kind"),
         "note": ("note", "note")}
TIMES = {"time": "time", "published": "published"}
#: What the keys are called on screen.
KEY_LABELS = {"id": "情報ID", "title": "題名", "time": "出来事の日時", "published": "発表日時", "source": "出典", "sourceRef": "文書番号・URL",
              "reliability": "確からしさ", "summary": "要旨", "kind": "分類", "area": "地名", "lon": "経度", "lat": "緯度", "note": "メモ",
              "links": "紐付け", "place": "地点"}
#: What happens to a row.
NEW, UPDATE, SAME, DUPLICATE, ERROR = "new", "update", "same", "duplicate", "error"
ACTION_LABELS = {NEW: "新規", UPDATE: "更新", SAME: "変更なし", DUPLICATE: "重複（飛ばす）", ERROR: "エラー"}
#: How a time is written (the schema's pattern, said in words).
TIME_WORDS = "日時の形（2026-11-24、2026-11-24T10:30、2026-11-24T10:30:00+09:00 など）でない"
_TYPE_WORDS = {"string": "文字", "number": "数", "integer": "整数", "null": "null", "array": "配列", "object": "オブジェクト", "boolean": "真偽値"}


def schema() -> dict:
    """The file's JSON Schema."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


@dataclass
class ImportFile:
    """A file read: its rows (`defaults` in), why it cannot be used at all, and each row's schema errors (by index)."""
    name: str = ""
    rows: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    row_problems: dict[int, list[str]] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return not self.problems


@dataclass
class Known:
    """What the rows are checked against besides the info layer."""
    #: The named areas (all rows of the layer; None: no named area layer, an area is kept as written).
    areas: Optional[Sequence[OrgArea]] = None
    #: Reads the shapes of areas: `shapes(areas)` → their rings by object id (None: those read already only).
    shapes: Optional[Callable[[Sequence[OrgArea]], dict]] = None
    #: 観測記録's 確度 levels and the classification's classes (empty: not checked).
    reliabilities: Sequence[str] = ()
    kinds: Sequence[str] = ()


@dataclass
class Planned:
    """What one row becomes."""
    number: int
    action: str
    #: The info as it will be saved (None for an error), and the one there now (updates, duplicates).
    info: Optional[ReferenceInfo] = None
    was: Optional[ReferenceInfo] = None
    #: What changes (the keys' names), why it is an error or a duplicate, what to know.
    changes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: The links to add (those there already left out).
    links: list[LinkTarget] = field(default_factory=list)
    #: Whether the info itself is written (an update of its links only is not).
    write: bool = False
    title: str = ""
    time: Optional[int] = None

    @property
    def label(self) -> str:
        return ACTION_LABELS[self.action]

    @property
    def text(self) -> str:
        """The row's changes, problems and warnings in a line."""
        if self.action == ERROR:
            return "、".join(self.problems)
        parts = []
        if self.action == DUPLICATE and self.was is not None:
            parts.append(f"同じ題名・日時・出典の参考情報がある（{self.was.id}）")
        elif self.changes:
            parts.append(("変わる: " if self.action == UPDATE else "") + "・".join(self.changes))
        return "　".join(parts + [f"注意: {w}" for w in self.warnings])


@dataclass
class ImportPlan:
    rows: list[Planned] = field(default_factory=list)
    #: What to know about the whole file (no link table and the like).
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in ACTION_LABELS}
        for r in self.rows:
            out[r.action] += 1
        return out

    @property
    def to_save(self) -> list[Planned]:
        return [r for r in self.rows if r.action in (NEW, UPDATE)]

    def summary(self) -> str:
        n = self.counts()
        text = "・".join(f"{ACTION_LABELS[k]} {v}" for k, v in n.items() if v)
        warned = sum(1 for r in self.rows if r.warnings and r.action != ERROR)
        return text + (f"（注意 {warned} 件）" if warned else "") if text else "取り込むものなし"


@dataclass
class ImportResult:
    plan: ImportPlan
    added: int = 0
    updated: int = 0
    links: int = 0
    #: Rows the server did not take: (row number, why).
    failures: list[tuple[int, str]] = field(default_factory=list)
    #: The infos saved, as saved.
    saved: list[ReferenceInfo] = field(default_factory=list)

    def summary(self) -> str:
        n = self.plan.counts()
        parts = [f"追加 {self.added}", f"更新 {self.updated}"]
        if self.links:
            parts.append(f"紐付け {self.links}")
        for k in (SAME, DUPLICATE, ERROR):
            if n[k]:
                parts.append(f"{ACTION_LABELS[k]} {n[k]}")
        if self.failures:
            parts.append(f"保存できず {len(self.failures)}")
        return "・".join(parts)


# ---- reading -------------------------------------------------------------------------------------


def _friendly(message: str) -> tuple[Optional[int], str]:
    """A schema error as (the row's index, words without the schema's terms); None: not a row's."""
    m = re.match(r"^\$\.infos\[(\d+)\](?:\.(\w+))?(?:\[(\d+)\](?:\.(\w+))?)?: (.*)$", message)
    if not m:
        top = re.match(r"^\$(?:\.(\w+))?: (.*)$", message)
        if top and top.group(2).startswith("定義にない "):
            name = top.group(2)[len("定義にない "):]
            return None, f"defaults に書けないキー「{name}」" if top.group(1) == "defaults" else f"知らないキー「{name}」"
        if top and not top.group(1) and top.group(2).startswith("型が "):
            return None, "全体が { \"infos\": [ … ] } の形のオブジェクトでない"
        if top and top.group(1) == "infos" and "要素が" in top.group(2):
            return None, "infos: " + top.group(2).replace("要素が", "参考情報が").replace("個", "件")
        return None, message[2:].lstrip(".: ") if message.startswith("$") else message
    index, key, item, sub, text = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)
    where = KEY_LABELS.get(key or "", key or "")
    if key == "links" and item is not None:
        where = f"紐付け {int(item) + 1} 番目" + ({"type": "の種類", "id": "の ID", "label": "の名前"}.get(sub or "", f"の {sub}" if sub else ""))
        key = f"link.{sub or ''}"
    typed = re.match(r"^型が (.+) 以外$", text)
    if typed:
        types = [t for t in typed.group(1).split("・") if t != "null"] or ["null"]
        text = "・".join(_TYPE_WORDS.get(t, t) for t in types) + "でない"
    elif (text.startswith("形式（") or text.startswith("anyOf")) and key in TIMES:
        text = TIME_WORDS
    elif text.startswith("形式（") and key == "id":
        text = "前後に空白"
    elif text.startswith("形式（") or text.endswith("文字未満"):
        text = "空"
    elif text.startswith("定義にない "):
        name = text[len("定義にない "):]
        return index, f"知らないキー「{name}」" + (f"（{where}）" if key else "")
    elif text.startswith("必須の "):
        name = text[len("必須の "):-len(" なし")]
        return index, f"{KEY_LABELS.get(name, name)}（{name}）がない" if not key else f"{where}: {name} がない"
    elif " があるのに " in text:
        return index, "経度と緯度は両方書く"
    elif "は ['event'" in text:
        text = "event・thing・observation のどれか"
    return index, f"{where}: {text}" if where else text


def parse_text(text: str, name: str = "") -> ImportFile:
    """A file's text as rows (see the module's text)."""
    out = ImportFile(name=name)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        out.problems.append(f"JSON として読めない（{error.lineno} 行 {error.colno} 文字目: {error.msg}）")
        return out
    errors = validate(value, schema())
    for e in errors:
        index, words = _friendly(e)
        if index is None:
            out.problems.append(words)
        elif words not in out.row_problems.setdefault(index, []):
            out.row_problems[index].append(words)
    if out.problems or not isinstance(value, dict) or not isinstance(value.get("infos"), list):
        out.problems = out.problems or ["infos（参考情報の配列）がない"]
        return out
    defaults = value.get("defaults") or {}
    for row in value["infos"]:
        if isinstance(row, dict):
            row = {**{k: v for k, v in defaults.items() if k in DEFAULT_KEYS}, **row}
        out.rows.append(row)
    return out


def read_file(path: str) -> ImportFile:
    """A file (UTF-8, with a BOM too) as rows."""
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except UnicodeDecodeError:
        return ImportFile(name=name, problems=["UTF-8 で読めない（文字コードを UTF-8 にして保存）"])
    except OSError as error:
        return ImportFile(name=name, problems=[f"ファイルを開けない: {error.strerror or error}"])
    return parse_text(text, name)


# ---- planning ------------------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _kind_text(text: str) -> str:
    """A class as the classification writes it (`船舶 > 支援船`)."""
    return re.sub(r"\s*>\s*", " > ", text.strip())


def _minute(ms: Optional[int]) -> Optional[int]:
    return None if ms is None else ms // 60_000


def duplicate_key(info: ReferenceInfo) -> tuple:
    """What makes two infos without an id in the file the same: the title, the time to the minute and the source."""
    return _norm(info.title), _minute(info.time), _norm(info.source)


def find_area(text: str, areas: Sequence[OrgArea]) -> tuple[Optional[OrgArea], str]:
    """The named area of a 管理番号, else of a name or an alias; (None, why) when none or two."""
    key = text.strip().casefold()
    by_id = [a for a in areas if a.id and a.id.strip().casefold() == key]
    if len(by_id) == 1:
        return by_id[0], ""
    if len(by_id) > 1:
        return None, f"管理番号「{text}」の地名が {len(by_id)} つある"
    name = _norm(text)
    found = [a for a in areas if _norm(a.name) == name or any(_norm(x) == name for x in a.aliases)]
    if len(found) > 1:
        found = [a for a in found if a.active] or found
    if len(found) == 1:
        return found[0], ""
    if found:
        return None, f"「{text}」という地名が {len(found)} つある（管理番号で書く）"
    return None, f"地名「{text}」が見つからない"


def _changes(was: ReferenceInfo, now: ReferenceInfo) -> list[str]:
    out = []
    for key, (attr, _role) in TEXTS.items():
        if (getattr(was, attr) or "") != (getattr(now, attr) or ""):
            out.append(KEY_LABELS[key])
    for key, attr in TIMES.items():
        # To the second: an Oracle DATE keeps no milliseconds.
        a, b = getattr(was, attr), getattr(now, attr)
        if (a is None) != (b is None) or (a is not None and a // 1000 != b // 1000):
            out.append(KEY_LABELS[key])
    if (was.area_id or "").casefold() != (now.area_id or "").casefold():
        out.append(KEY_LABELS["area"])
    if was.has_place != now.has_place or (now.has_place and (abs(was.lon - now.lon) > 1e-9 or abs(was.lat - now.lat) > 1e-9)):
        out.append(KEY_LABELS["place"])
    return out


def _value_text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def plan_import(client: EsriClient, settings: ReferenceSettings, data: ImportFile, known: Optional[Known] = None,
                token: Optional[str] = None, links_token: Optional[str] = None, now: Optional[float] = None) -> ImportPlan:
    """What each row of the file would become (nothing written)."""
    plan = ImportPlan()
    if not data.usable:
        return plan
    known = known or Known()
    s = settings.layer
    token = _token(s, token)
    layer = _info_layer(client, settings, token, refresh=True)
    lengths, has = layer.field_lengths, set(layer.field_types)
    f = s.fields
    rows = [r if isinstance(r, dict) else {} for r in data.rows]
    ids = [_value_text(r.get("id")) for r in rows if isinstance(r.get("id"), str) and r["id"].strip()]
    there = {x.id: x for x in load_infos(client, settings, token, ids=ids)} if ids else {}
    titles = [r["title"].strip() for r in rows if isinstance(r.get("title"), str) and r["title"].strip()]
    by_key: dict[tuple, ReferenceInfo] = {}
    for x in (load_infos(client, settings, token, titles=titles) if titles and f["title"] in has else []):
        by_key.setdefault(duplicate_key(x), x)
    links_on = settings.links is not None
    any_links = any(isinstance(r.get("links"), list) and r["links"] for r in rows)
    if any_links and not links_on:
        plan.notes.append("紐付けは使えない設定なので、links は取り込まない（管理者に連絡: referenceInfo.links）")
    linked: dict[str, set] = {}
    if any_links and links_on and there:
        for x in load_links(client, settings, list(there), token=links_token):
            linked.setdefault(x.info_id, set()).add((x.type, x.item_id))
    if known.areas is None and any(_value_text(r.get("area")) for r in rows):
        plan.notes.append("地名のレイヤーがないので、area は確かめずに管理番号として保存する")
    # The named areas named, their shapes read together (those without a point need a point inside).
    areas: dict[str, tuple[Optional[OrgArea], str]] = {}
    if known.areas is not None:
        for r in rows:
            a = _value_text(r.get("area"))
            if a and a not in areas:
                areas[a] = find_area(a, known.areas)
        wanted = [a for a, _why in areas.values() if a is not None and a.rings is None and a.oid is not None]
        if wanted and known.shapes is not None:
            rings = known.shapes(wanted)
            for text, (a, why) in list(areas.items()):
                if a is not None and a.rings is None and a.oid in rings:
                    areas[text] = (replace(a, rings=rings[a.oid]), why)
    reliabilities = {_norm(x): x for x in known.reliabilities}
    kinds = {_norm(_kind_text(x)): _kind_text(x) for x in known.kinds}
    seen_ids: set[str] = set()
    made: set[str] = set(there)
    for index, row in enumerate(rows):
        number = index + 1
        p = Planned(number, ERROR, title=_value_text(row.get("title")))
        plan.rows.append(p)
        problems = list(data.row_problems.get(index, []))
        if not row:
            p.problems = problems or ["参考情報（オブジェクト）でない"]
            continue
        info_id = _value_text(row.get("id"))
        if info_id:
            if info_id in seen_ids:
                problems.append(f"同じ情報ID「{info_id}」がファイルの前の行にもある")
            seen_ids.add(info_id)
        was = there.get(info_id) if info_id else None
        times: dict[str, Optional[int]] = {}
        for key, attr in TIMES.items():
            if key in row and isinstance(row[key], str):
                t = parse_time(row[key])
                if t is None:
                    problems.append(f"{KEY_LABELS[key]}: {row[key]} は日時として読めない")
                times[attr] = t
            elif key in row and row[key] is None:
                times[attr] = None
        p.time = times.get("time", was.time if was else None)
        if problems:
            p.problems = problems
            continue
        base = was or ReferenceInfo(id=info_id or "", title="")
        values: dict[str, Any] = dict(times)
        for key, (attr, role) in TEXTS.items():
            if key not in row:
                continue
            text = _value_text(row[key])
            if key == "kind" and text:
                text = kinds.get(_norm(_kind_text(text)), _kind_text(text))
            if key == "reliability" and text:
                text = reliabilities.get(_norm(text), text)
            name = f.get(role)
            if text and name in lengths and lengths[name] and len(text) > lengths[name]:
                problems.append(f"{KEY_LABELS[key]}が {lengths[name]} 文字を超える（{len(text)} 文字）")
            values[attr] = text
        if info_id and len(info_id) > lengths.get(f["id"], 64) > 0:
            problems.append(f"情報IDが {lengths[f['id']]} 文字を超える")
        # The named area, and the place: lon/lat, else a point inside the area (as 「地名から…」), else as it was.
        area_point = None
        if "area" in row:
            text = _value_text(row["area"])
            if not text:
                values["area_id"] = ""
            elif known.areas is None:
                values["area_id"] = text
            else:
                area, why = areas.get(text, (None, ""))
                if area is None:
                    problems.append(why)
                else:
                    values["area_id"] = area.id
                    if not area.id:
                        p.warnings.append(f"地名「{area.name}」には管理番号がない（地点だけ）")
                    area_point = point_inside(area.rings or [])
                    if area_point is None and "lon" not in row:
                        p.warnings.append(f"地名「{area.name}」の形がないので地点は{'そのまま' if was else 'なし'}")
            name = f.get("areaId")
            if values.get("area_id") and name in lengths and lengths[name] and len(values["area_id"]) > lengths[name]:
                problems.append(f"地名の管理番号が {lengths[name]} 文字を超える")
        if "lon" in row and "lat" in row:
            lon, lat = row.get("lon"), row.get("lat")
            if (lon is None) != (lat is None):
                problems.append("経度と緯度の片方だけ（消すときは両方 null）")
            else:
                values["lon"], values["lat"] = (float(lon), float(lat)) if lon is not None else (None, None)
        elif area_point is not None:
            values["lon"], values["lat"] = area_point
        if problems:
            p.problems = problems
            continue
        info = replace(base, **values)
        if not info.title:
            p.problems = ["題名が空"]
            continue
        if info.time is None:
            p.problems = ["出来事の日時がない"]
            continue
        if info.reliability and reliabilities and _norm(info.reliability) not in reliabilities:
            p.warnings.append(f"確からしさ「{info.reliability}」は確度の段階（{'・'.join(known.reliabilities)}）にない")
        if info.kind and kinds and _norm(info.kind) not in kinds:
            p.warnings.append(f"分類「{info.kind}」は分類体系にない")
        p.title, p.time = info.title, info.time
        targets = []
        for link in row.get("links") or []:
            if not links_on:
                break
            t = LinkTarget(link["type"], link["id"].strip(), _value_text(link.get("label")))
            if (t.type, t.item_id) not in linked.get(info.id, set()) and all((x.type, x.item_id) != (t.type, t.item_id) for x in targets):
                targets.append(t)
        if row.get("links") and not links_on:
            p.warnings.append("紐付けは使えない設定なので links は飛ばす")
        key = duplicate_key(info)
        if was is not None:
            p.was = was
            p.changes = _changes(was, info)
            p.write = bool(p.changes)
            p.links = targets
            if targets:
                p.changes.append(f"紐付け +{len(targets)}")
            p.action = UPDATE if p.write or targets else SAME
            p.info = info
            continue
        other = by_key.get(key)
        if not info_id and other is not None:
            p.action, p.was, p.info = DUPLICATE, other, other
            continue
        if info_id and other is not None and other.id != info_id:
            p.warnings.append(f"同じ題名・日時・出典の参考情報がある（{other.id}）")
        if not info_id:
            info_id = new_id(now)
            while info_id in made:
                info_id = new_id(now)
            info = replace(info, id=info_id)
        made.add(info_id)
        by_key.setdefault(key, info)
        p.action, p.info, p.write, p.links = NEW, info, True, targets
        p.changes = [f"紐付け {len(targets)}"] if targets else []
    return plan


# ---- saving --------------------------------------------------------------------------------------


def apply_import(client: EsriClient, settings: ReferenceSettings, data: ImportFile, known: Optional[Known] = None,
                 token: Optional[str] = None, links_token: Optional[str] = None, now: Optional[float] = None) -> ImportResult:
    """
    Plans the file again (against what is there now) and saves it: the new and
    changed infos, then their links. Error rows are skipped; a row the server
    does not take is told, the others stay saved.
    """
    plan = plan_import(client, settings, data, known, token, links_token, now)
    result = ImportResult(plan)
    s = settings.layer
    token = _token(s, token)
    writes = [p for p in plan.rows if p.action in (NEW, UPDATE) and p.write and p.info is not None]
    if writes:
        layer = _info_layer(client, settings, token, refresh=True)
        adds = [p for p in writes if p.info.oid is None]
        updates = [p for p in writes if p.info.oid is not None]
        answer = client.apply_edits(s.url, adds=[feature_of(p.info, s.fields, layer) for p in adds],
                                    updates=[feature_of(p.info, s.fields, layer) for p in updates], token=token)
        for p, r in zip(adds, answer.add_results or []):
            if r.get("success"):
                result.added += 1
                oid = r.get("objectId")
                p.info = replace(p.info, oid=int(oid) if isinstance(oid, (int, float)) else None)
                result.saved.append(p.info)
            else:
                result.failures.append((p.number, (r.get("error") or {}).get("description") or "追加できない"))
                p.links = []
        for p, r in zip(updates, answer.update_results or []):
            if r.get("success"):
                result.updated += 1
                result.saved.append(p.info)
            else:
                result.failures.append((p.number, (r.get("error") or {}).get("description") or "更新できない（ほかの人が削除済みか）"))
                p.links = []
        answered = len(answer.add_results or []) + len(answer.update_results or [])
        if answered < len(writes):
            for p in (adds[len(answer.add_results or []):] + updates[len(answer.update_results or []):]):
                result.failures.append((p.number, "サーバーの答えがない"))
                p.links = []
    for p in plan.rows:
        if p.action == UPDATE and not p.write and p.links and p.info is not None:
            result.saved.append(p.info)
    pairs = [(p.info.id, t) for p in plan.rows if p.action in (NEW, UPDATE) and p.info is not None for t in p.links]
    if pairs and settings.links is not None:
        try:
            result.links, _skipped = add_link_pairs(client, settings, pairs, links_token)
        except EsriError as error:
            result.failures.append((0, f"紐付けの一部を保存できない: {error}"))
    return result


def plan_lines(plan: ImportPlan) -> list[dict]:
    """The plan as plain values (the Python API's answer)."""
    return [{"row": p.number, "action": p.action, "label": p.label, "id": p.info.id if p.info else "", "title": p.title,
             "changes": list(p.changes), "problems": list(p.problems), "warnings": list(p.warnings),
             "links": [{"type": t.type, "id": t.item_id} for t in p.links]} for p in plan.rows]

