"""
The classification (分類体系) of the things observations are about, as the
viewer (taxonomy.ts): a tree whose branches go as deep as they need, kept as
a flat list of classes by path (parents implied).

A record's kind (種別) is the path of its class written out
(`船舶 > 支援船 > 〇〇級`).
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..util import is_number, is_record, js_string

#: Written between the levels of a kind.
SEPARATOR = " > "
_SPLIT = re.compile(r"\s*[>›＞]\s*")


#: The specification field that holds an individual's (or a class's) other names; an individual's go to their own
#: attribute (ALIASES) in a table of individuals.
ALIAS_FIELD = "別名"


@dataclass
class Member:
    """What a known individual (既知の個体) says of itself, besides its name."""
    name: str
    #: Its own specifications (諸元) by field name, 別名 among them; the class's apply where it says nothing.
    specs: Optional[dict[str, str]] = None
    #: Its reference photos (as a class's).
    photos: Optional[list[str]] = None
    note: Optional[str] = None
    #: Its id (UUID) in a table of individuals: kept across a rename or a move. None until it has one.
    id: Optional[str] = None

    def says(self) -> bool:
        """Whether it says anything besides its name (its id aside)."""
        return bool(specs_of(self.specs) or self.photos or (self.note or "").strip())

    def to_json(self, with_id: bool = False) -> Any:
        """As written in `members`: its name alone, or `{name, specs, photos, note}` (and `id` when asked and known)."""
        if not self.says() and not (with_id and self.id):
            return self.name
        out: dict[str, Any] = {"name": self.name}
        if with_id and self.id:
            out["id"] = self.id
        if specs_of(self.specs):
            out["specs"] = specs_of(self.specs)
        if self.photos:
            out["photos"] = list(self.photos)
        if (self.note or "").strip():
            out["note"] = self.note.strip()
        return out

    def aliases(self) -> list[str]:
        """Its other names (別名)."""
        return split_list((self.specs or {}).get(ALIAS_FIELD, ""))


def split_list(text: str) -> list[str]:
    """The items of a list value (読点・カンマ区切り)."""
    return [w.strip() for w in re.split(r"[,、，]", text or "") if w.strip()]


@dataclass
class Taxon:
    """One class: its path from the root, and what it says of its branch."""
    path: list[str]
    #: The names of the depths from the root, when this class sets them.
    levels: Optional[list[str]] = None
    #: What an individual's name is called below this class (艦名, 機番).
    name_label: Optional[str] = None
    #: Known individuals of this class.
    members: Optional[list[str]] = None
    #: Specifications (諸元) by field name.
    specs: Optional[dict[str, str]] = None
    #: Reference photos: URLs, or `local:` ids of photos kept locally.
    photos: Optional[list[str]] = None
    #: The activities (活動) offered for this class and below, when it names them.
    activities: Optional[list[str]] = None
    #: What its known individuals say of themselves (別名・諸元・参考写真・備考, their ids), by name: only those that
    #: say something or have an id.
    member_info: Optional[dict[str, Member]] = None

    def own(self) -> dict[str, Any]:
        """Its own settings, without the empty ones."""
        return _own_of(self.levels, self.name_label, self.members, self.specs, self.photos, self.activities, self.member_info)

    def member(self, name: str) -> Member:
        """A known individual's details (a bare one when it says nothing)."""
        return (self.member_info or {}).get(name) or Member(name)

    def members_json(self, with_id: bool = False) -> list[Any]:
        """Its known individuals as written in `members` (names, or objects for those that say something)."""
        return [self.member(m).to_json(with_id) for m in self.members or []]

    def to_json(self) -> dict[str, Any]:
        """As the viewer writes a class (`nameLabel`; `members` may hold objects for individuals with details)."""
        out: dict[str, Any] = {"path": list(self.path)}
        own = self.own()
        for k in ("levels", "name_label", "members", "specs", "photos", "activities"):
            if k in own:
                out["nameLabel" if k == "name_label" else k] = self.members_json(True) if k == "members" else own[k]
        return out


#: Where a specification field is used: both classes and individuals, classes only, individuals only.
SPEC_USES = ("both", "class", "member")


@dataclass
class SpecField:
    """One specification field (諸元の項目)."""
    name: str
    #: text; number; list (several, 読点区切り); url.
    type: str = "text"
    unit: Optional[str] = None
    #: Where it is used (SPEC_USES): both (default), class, member (individuals only).
    use: str = "both"
    #: The roots (大分類) it is offered under; None for all.
    roots: Optional[list[str]] = None

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, **({"unit": self.unit} if self.unit else {}),
                **({"use": self.use} if self.use != "both" else {}), **({"roots": list(self.roots)} if self.roots else {})}

    def offered(self, path: list[str], member: bool) -> bool:
        """Whether it is offered for a class (`member` False) or an individual of the class at `path`."""
        if self.use == ("member" if not member else "class"):
            return False
        return not self.roots or bool(path) and path[0] in self.roots


#: The fields offered at first.
DEFAULT_SPEC_FIELDS: list[SpecField] = [
    SpecField("別名", "list"),
    SpecField("英名", "text"),
    SpecField("開発国", "text"),
    SpecField("運用国", "list"),
    SpecField("全長", "number", "m"),
    SpecField("全幅", "number", "m"),
    SpecField("全高", "number", "m"),
    SpecField("排水量", "number", "t"),
    SpecField("就役", "text"),
    SpecField("識別の特徴", "text"),
    SpecField("資料", "url"),
    # Individuals only (艦番号・機番・登録記号, 所属, 母港・基地, 現役・退役など).
    SpecField("番号", "text", use="member"),
    SpecField("所属", "text", use="member"),
    SpecField("拠点", "text", use="member"),
    SpecField("状態", "text", use="member"),
]


def path_of(kind: str) -> list[str]:
    """The path of a kind (`船舶 > 支援船`, also › / ＞ between)."""
    return [s.strip() for s in _SPLIT.split(kind or "") if s.strip()]


def kind_of(path: Iterable[str]) -> str:
    """The kind of a path."""
    return SEPARATOR.join(path)


def within(kind: str, path: list[str]) -> bool:
    """Whether a kind is `path` or below it."""
    p = path_of(kind)
    return len(path) <= len(p) and all(p[i] == s for i, s in enumerate(path))


def _key(path: Iterable[str]) -> str:
    return "\u0000".join(path)


def _segment(name: str) -> str:
    return _SPLIT.sub(" ", name).strip()


def segment_name(name: str) -> str:
    """A class's name as kept: the separators between levels (> › ＞) made spaces, the spaces around taken off."""
    return _segment(name or "")


def _words(value: Any) -> Optional[list[str]]:
    if not isinstance(value, list):
        return None
    return list(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))


def specs_of(value: Any) -> Optional[dict[str, str]]:
    """The specification values that say something."""
    if not is_record(value):
        return None
    out: dict[str, str] = {}
    for k, v in value.items():
        if isinstance(v, list):
            text = "、".join(js_string(x) for x in v)
        elif is_number(v) or isinstance(v, str):
            text = js_string(v).strip()
        else:
            text = ""
        if k.strip() and text:
            out[k.strip()] = text
    return out or None


def members_of(value: Any) -> tuple[Optional[list[str]], Optional[dict[str, Member]]]:
    """Known individuals as written in `members`: names, or objects `{name, specs, photos, note, id}` (a repeated name
    once, its first details kept); (None, None) when not a list."""
    if not isinstance(value, list):
        return None, None
    names: list[str] = []
    info: dict[str, Member] = {}
    for v in value:
        member = None
        if isinstance(v, str):
            name = v.strip()
        elif is_record(v) and isinstance(v.get("name"), str):
            name = v["name"].strip()
            note = v["note"].strip() if isinstance(v.get("note"), str) else ""
            mid = v["id"].strip() if isinstance(v.get("id"), str) else ""
            member = Member(name, specs_of(v.get("specs")), _words(v.get("photos")) or None, note or None, mid or None)
        else:
            continue
        if not name:
            continue
        if name not in names:
            names.append(name)
        if member is not None and name not in info and (member.says() or member.id):
            info[name] = member
    return names or None, info or None


def _member_copy(m: Member, name: str) -> Member:
    specs = specs_of(m.specs)
    return Member(name, dict(specs) if specs else None, list(dict.fromkeys(m.photos)) if m.photos else None,
                  (m.note or "").strip() or None, m.id or None)


def _own_of(levels, name_label, members, specs, photos, activities=None, member_info=None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if levels:
        out["levels"] = list(levels)
    if name_label:
        out["name_label"] = name_label
    if members:
        out["members"] = list(dict.fromkeys(members))
        info = {n: _member_copy(member_info[n], n) for n in out["members"]
                if member_info and n in member_info and (member_info[n].says() or member_info[n].id)}
        if info:
            out["member_info"] = info
    s = specs_of(specs)
    if s:
        out["specs"] = s
    if photos:
        out["photos"] = list(dict.fromkeys(photos))
    acts = _words(activities)
    if acts:
        out["activities"] = acts
    return out


def _copy(t: Taxon, path: Optional[list[str]] = None) -> Taxon:
    return Taxon(path=list(path if path is not None else t.path), **t.own())


def normalize(items: Iterable[Taxon]) -> list[Taxon]:
    """Fills in the parents, parents before children, siblings in the order they came; later duplicates add to the first."""
    by_key: dict[str, Taxon] = {}
    order: list[str] = []

    def put(t: Taxon) -> None:
        k = _key(t.path)
        had = by_key.get(k)
        if had is None:
            by_key[k] = _copy(t)
            order.append(k)
            return
        if t.levels and not had.levels:
            had.levels = list(t.levels)
        if t.name_label and not had.name_label:
            had.name_label = t.name_label
        if t.members:
            had.members = list(dict.fromkeys([*(had.members or []), *t.members]))
        if t.member_info:
            had.member_info = {**t.member_info, **(had.member_info or {})}
        s = specs_of(t.specs)
        if s:
            had.specs = {**s, **(had.specs or {})}
        if t.photos:
            had.photos = list(dict.fromkeys([*(had.photos or []), *t.photos]))
        if t.activities and not had.activities:
            had.activities = list(t.activities)

    for t in items:
        path = [p for p in (_segment(s) for s in t.path) if p]
        if not path:
            continue
        for d in range(1, len(path)):
            put(Taxon(path=path[:d]))
        put(_copy(t, path))
    rank = {k: i for i, k in enumerate(order)}
    children: dict[str, list[Taxon]] = {}
    for k in order:
        t = by_key[k]
        children.setdefault(_key(t.path[:-1]), []).append(t)
    out: list[Taxon] = []

    def walk(parent: str) -> None:
        for t in sorted(children.get(parent, []), key=lambda t: rank[_key(t.path)]):
            out.append(t)
            walk(_key(t.path))

    walk(_key([]))
    return out


def flatten(trees: list[dict[str, Any]], parent: Optional[list[str]] = None) -> list[Taxon]:
    """The flat list of a nested tree (`{name, children, levels, nameLabel, …}`)."""
    parent = parent or []

    def walk(nodes: list[dict[str, Any]], above: list[str]) -> list[Taxon]:
        out: list[Taxon] = []
        for t in nodes:
            path = [*above, t["name"]]
            names, info = members_of(t.get("members"))
            out.append(Taxon(path=path, **_own_of(t.get("levels"), t.get("nameLabel"), names, t.get("specs"), t.get("photos"),
                                                         t.get("activities"), info)))
            if t.get("children"):
                out.extend(walk(t["children"], path))
        return out

    return normalize(walk(trees, parent))


def nest(items: Iterable[Taxon]) -> list[dict[str, Any]]:
    """The nested tree of a flat list (for the settings file)."""
    roots: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    for t in normalize(items):
        node: dict[str, Any] = {"name": t.path[-1]}
        for k, v in t.own().items():
            if k != "member_info":
                node["nameLabel" if k == "name_label" else k] = t.members_json() if k == "members" else v
        nodes[_key(t.path)] = node
        parent = nodes.get(_key(t.path[:-1]))
        if parent is not None:
            parent.setdefault("children", []).append(node)
        else:
            roots.append(node)
    return roots


def from_kinds(kinds: Iterable[str]) -> list[Taxon]:
    """Classes of plain kinds (`kinds: ["船舶", …]`), each a root."""
    return normalize(Taxon(path=path_of(k)) for k in kinds)


def taxonomy_of(value: Any, problems: list[str], at: str = "observationTaxonomy") -> Optional[list[Taxon]]:
    """A nested classification of the settings; None when it cannot be used."""
    if not isinstance(value, list):
        problems.append(f"{at} は分類の配列で指定")
        return None

    def tree(v: Any, where: str) -> Optional[dict[str, Any]]:
        if isinstance(v, str) and v.strip():
            return {"name": v.strip()}
        if not is_record(v) or not isinstance(v.get("name"), str) or not v["name"].strip():
            problems.append(f"{where} は name のある分類（または名前の文字列）で指定")
            return None
        kids = v.get("children")
        children = None
        if isinstance(kids, list):
            children = [c for c in (tree(c, f"{where}.children[{i}]") for i, c in enumerate(kids)) if c is not None]
        elif "children" in v:
            problems.append(f"{where}.children は配列で指定")
        node: dict[str, Any] = {"name": v["name"].strip()}
        if _words(v.get("levels")):
            node["levels"] = _words(v.get("levels"))
        if isinstance(v.get("nameLabel"), str) and v["nameLabel"].strip():
            node["nameLabel"] = v["nameLabel"].strip()
        names, info = members_of(v.get("members"))
        if names:
            node["members"] = [info[n].to_json(True) if info and n in info else n for n in names]
        if specs_of(v.get("specs")):
            node["specs"] = specs_of(v.get("specs"))
        if _words(v.get("photos")):
            node["photos"] = _words(v.get("photos"))
        if "activities" in v:
            if _words(v.get("activities")) is None:
                problems.append(f"{where}.activities は活動の文字列の配列で指定")
            elif _words(v.get("activities")):
                node["activities"] = _words(v.get("activities"))
        if children:
            node["children"] = children
        return node

    return flatten([t for t in (tree(v, f"{at}[{i}]") for i, v in enumerate(value)) if t is not None])


def with_kinds(items: list[Taxon], kinds: Iterable[str]) -> list[Taxon]:
    """The classification with the kinds records use that it does not have yet."""
    # Each kind once (a later class of the same path adds nothing): a few kinds over thousands of records.
    return normalize([*items, *(Taxon(path=path_of(k)) for k in dict.fromkeys(kinds))])


def find(items: list[Taxon], path: list[str]) -> Optional[Taxon]:
    """A class by path."""
    k = _key(path)
    return next((t for t in items if _key(t.path) == k), None)


def children_of(items: list[Taxon], path: list[str]) -> list[Taxon]:
    """The classes right below a path ([] for the roots)."""
    return [t for t in items if len(t.path) == len(path) + 1 and all(t.path[i] == s for i, s in enumerate(path))]


def level_name(items: list[Taxon], path: list[str], depth: int) -> str:
    """The name of a depth (0 the root) in a path's branch: the nearest class on the path that names it."""
    for d in range(min(len(path), depth + 1), 0, -1):
        t = find(items, path[:d])
        levels = t.levels if t else None
        if levels and depth < len(levels) and levels[depth]:
            return levels[depth]
    return ""


def name_label_of(items: list[Taxon], path: list[str]) -> str:
    """What an individual's name is called below a path: the nearest class that says."""
    for d in range(len(path), 0, -1):
        t = find(items, path[:d])
        if t and t.name_label:
            return t.name_label
    return ""


def member_index(items: list[Taxon]) -> dict[str, list[str]]:
    """The class of each known individual's name."""
    out: dict[str, list[str]] = {}
    for t in items:
        for m in t.members or []:
            out.setdefault(m, t.path)
    return out


def rewrite(kind: str, src: list[str], dst: list[str]) -> str:
    """`kind` with `src` (and below) moved to `dst`; unchanged when it is not under `src`."""
    if not within(kind, src):
        return kind
    return kind_of([*dst, *path_of(kind)[len(src):]])


def _under(t: Taxon, p: list[str]) -> bool:
    return len(t.path) >= len(p) and all(t.path[i] == s for i, s in enumerate(p))


def move_taxon(items: list[Taxon], src: list[str], dst: list[str]) -> list[Taxon]:
    """The classification with a class (and its branch) moved or renamed to `dst`."""
    if not src or not dst:
        return list(items)
    if len(dst) > len(src) and all(dst[i] == s for i, s in enumerate(src)):
        raise ValueError("自分の下位には移動できない")
    return normalize(_copy(t, [*dst, *t.path[len(src):]]) if _under(t, src) else t for t in items)


def place_taxon(items: list[Taxon], path: list[str], before: Optional[list[str]]) -> list[Taxon]:
    """The classification with a class's branch placed just before a sibling (`before`), or after its siblings (None)."""
    branch = [t for t in items if _under(t, path)]
    if not branch:
        return list(items)
    rest = [t for t in items if not _under(t, path)]
    parent = path[:-1]
    at = -1
    if before and len(before) == len(path):
        at = next((i for i, t in enumerate(rest) if _key(t.path) == _key(before)), -1)
    end = len(rest)
    if at < 0 and parent:
        last = -1
        for i, t in enumerate(rest):
            if _under(t, parent):
                last = i
        if last >= 0:
            end = last + 1
    index = at if at >= 0 else end
    return normalize([*rest[:index], *branch, *rest[index:]])


def remove_taxon(items: list[Taxon], path: list[str]) -> list[Taxon]:
    """The classification without a class and its branch."""
    return [t for t in items if not _under(t, path)]


def update_taxon(items: list[Taxon], path: list[str], **changes: Any) -> list[Taxon]:
    """The classification with a class's own settings changed (those named; empty clears)."""
    k = _key(path)
    out = []
    for t in items:
        if _key(t.path) == k:
            own = {"levels": t.levels, "name_label": t.name_label, "members": t.members, "specs": t.specs, "photos": t.photos,
                   "activities": t.activities, "member_info": t.member_info}
            own.update(changes)
            out.append(Taxon(path=list(t.path), **_own_of(own["levels"], own["name_label"], own["members"], own["specs"], own["photos"],
                                                          own["activities"], own["member_info"])))
        else:
            out.append(t)
    return normalize(out)


def spec_fields_of(value: Any, problems: list[str], at: str = "specFields") -> Optional[list[SpecField]]:
    """`specFields` (names alone are text fields); None when it cannot be used."""
    if not isinstance(value, list):
        problems.append(f"{at} は諸元の項目の配列で指定")
        return None
    out: list[SpecField] = []
    for i, v in enumerate(value):
        f = {"name": v} if isinstance(v, str) else v
        if not is_record(f) or not isinstance(f.get("name"), str) or not f["name"].strip():
            problems.append(f"{at}[{i}] は name のある項目（または名前の文字列）で指定")
            continue
        kind = f.get("type", "text")
        if kind not in ("text", "number", "list", "url"):
            problems.append(f"{at}[{i}].type は text・number・list・url のいずれかで指定")
            continue
        use = f.get("use", "both")
        if use not in SPEC_USES:
            problems.append(f"{at}[{i}].use は both・class・member のいずれかで指定")
            use = "both"
        roots = f.get("roots")
        if roots is not None and _words(roots) is None:
            problems.append(f"{at}[{i}].roots は大分類の名前の配列で指定")
        # As the viewer: the untrimmed name decides a repeat.
        if any(x.name == f["name"] for x in out):
            continue
        unit = f.get("unit")
        out.append(SpecField(f["name"].strip(), kind, unit.strip() if isinstance(unit, str) and unit.strip() else None, use,
                             _words(roots) or None))
    return out


def specs_at(items: list[Taxon], path: list[str]) -> dict[str, tuple[str, list[str]]]:
    """A class's specifications, its own and those above (the nearest wins), by field: (value, the class that says)."""
    out: dict[str, tuple[str, list[str]]] = {}
    for d in range(len(path), 0, -1):
        t = find(items, path[:d])
        for name, value in ((t.specs if t else None) or {}).items():
            out.setdefault(name, (value, path[:d]))
    return out


def activities_at(items: list[Taxon], path: list[str]) -> tuple[list[str], list[str]]:
    """The activities offered for a path: those of the nearest class on it that names some (and that class), else ([], [])."""
    for d in range(len(path), 0, -1):
        t = find(items, path[:d])
        if t and t.activities:
            return list(t.activities), path[:d]
    return [], []


def photos_at(items: list[Taxon], path: list[str]) -> tuple[list[str], list[str]]:
    """A class's reference photos: its own, else those of the nearest class above that has some."""
    for d in range(len(path), 0, -1):
        t = find(items, path[:d])
        if t and t.photos:
            return t.photos, path[:d]
    return [], []


def spec_text(spec: Optional[SpecField], value: str) -> str:
    """A specification shown with its unit."""
    return f"{value} {spec.unit}" if spec and spec.unit and value else value


def _name_fields(fields: Optional[list[SpecField]]) -> list[str]:
    """The specification fields that hold other names: lists and those named …名 (別名・英名)."""
    fields = DEFAULT_SPEC_FIELDS if fields is None else fields
    names = [f.name for f in fields if f.type == "list" or "名" in f.name]
    return names if ALIAS_FIELD in names else [ALIAS_FIELD, *names]


def class_by_name(items: list[Taxon], name: str, fields: Optional[list[SpecField]] = None) -> Optional[list[str]]:
    """The class a name stands for: a class's own name, else one of its other names (別名) or its 英名, any case;
    else the class of a known individual of that name or other name."""
    q = name.strip().lower()
    if not q:
        return None
    own = next((t for t in items if t.path[-1].lower() == q), None)
    if own:
        return own.path
    names = _name_fields(fields)
    for t in items:
        for n in names:
            if any(w.strip().lower() == q for w in re.split(r"[,、]", (t.specs or {}).get(n, ""))):
                return t.path
    hit = find_member(items, name, fields)
    return hit.path if hit else None


def same_name(name: str) -> str:
    """A name as compared with individuals' names and other names: width, case and spaces aside (ＤＤＨ－１８３ = ddh-183)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name or "")).strip().casefold()


@dataclass
class MemberHit:
    """A known individual found by a name: its class, its own name, and the other name it was found by ("" for its own)."""
    path: list[str]
    name: str
    alias: str = ""


def member_names(t: Taxon, name: str, fields: Optional[list[SpecField]] = None) -> list[str]:
    """An individual's other names (別名, 英名 and other list or …名 fields of its own), as written."""
    specs = t.member(name).specs or {}
    out: list[str] = []
    for f in _name_fields(fields):
        out.extend(w for w in split_list(specs.get(f, "")) if w not in out and w != name)
    return out


def find_member(items: list[Taxon], name: str, fields: Optional[list[SpecField]] = None) -> Optional[MemberHit]:
    """The known individual a name stands for: one of that name (exactly, then width and case aside), else one that
    has it as another name (別名・英名); None when none does."""
    if not name.strip():
        return None
    typed = name.strip()
    for t in items:
        if typed in (t.members or []):
            return MemberHit(list(t.path), typed)
    q = same_name(typed)
    for t in items:
        for m in t.members or []:
            if same_name(m) == q:
                return MemberHit(list(t.path), m)
    for t in items:
        for m in t.members or []:
            for other in member_names(t, m, fields):
                if same_name(other) == q:
                    return MemberHit(list(t.path), m, other)
    return None



def member_finder(items: list[Taxon], fields: Optional[list[SpecField]] = None):
    """`find_member` for many names (a table's rows): the classification looked through once."""
    exact: dict[str, MemberHit] = {}
    same: dict[str, MemberHit] = {}
    other: dict[str, MemberHit] = {}
    for t in items:
        for m in t.members or []:
            exact.setdefault(m, MemberHit(list(t.path), m))
            same.setdefault(same_name(m), MemberHit(list(t.path), m))
    for t in items:
        for m in t.members or []:
            for o in member_names(t, m, fields):
                other.setdefault(same_name(o), MemberHit(list(t.path), m, o))

    def find_one(name: str) -> Optional[MemberHit]:
        typed = (name or "").strip()
        if not typed:
            return None
        q = same_name(typed)
        return exact.get(typed) or same.get(q) or other.get(q)

    return find_one

def member_specs_at(items: list[Taxon], path: list[str], name: str) -> dict[str, tuple[str, list[str]]]:
    """An individual's specifications: its own (said by `[*path, name]`), then its class's and those above."""
    out: dict[str, tuple[str, list[str]]] = {}
    t = find(items, path)
    if t is not None and name in (t.members or []):
        for k, v in (specs_of(t.member(name).specs) or {}).items():
            out[k] = (v, [*path, name])
    for k, v in specs_at(items, path).items():
        out.setdefault(k, v)
    return out


def member_photos_at(items: list[Taxon], path: list[str], name: str) -> tuple[list[str], list[str]]:
    """An individual's reference photos: its own (said by `[*path, name]`), else its class's or the nearest above."""
    t = find(items, path)
    own = t.member(name).photos if t is not None and name in (t.members or []) else None
    return (list(own), [*path, name]) if own else photos_at(items, path)


def new_member_id() -> str:
    """A new individual's id."""
    return str(uuid.uuid4())


def _with_members(items: list[Taxon], path: list[str], change) -> list[Taxon]:
    """The classification with a class's individuals changed: `change(names, info)` edits copies of them in place."""
    t = find(items, path)
    if t is None:
        raise ValueError(f"分類「{kind_of(path)}」がない")
    names = list(t.members or [])
    info = {k: _member_copy(v, k) for k, v in (t.member_info or {}).items()}
    change(names, info)
    return update_taxon(items, path, members=names, member_info=info)


def add_members(items: list[Taxon], path: list[str], names: Iterable[str]) -> list[Taxon]:
    """The classification with individuals added to a class (after its others; names it has already are left)."""
    def change(have: list[str], info: dict[str, Member]) -> None:
        for n in names:
            n = segment_name(n)
            if n and n not in have:
                have.append(n)
                info[n] = Member(n, id=new_member_id())
    return _with_members(items, path, change)


def update_member(items: list[Taxon], path: list[str], name: str, specs: Optional[dict[str, str]] = None,
                  photos: Optional[list[str]] = None, note: Optional[str] = None) -> list[Taxon]:
    """The classification with an individual's details set (each given replaces; None keeps; empty clears)."""
    def change(have: list[str], info: dict[str, Member]) -> None:
        if name not in have:
            raise ValueError(f"個体「{name}」が分類「{kind_of(path)}」にない")
        m = info.get(name) or Member(name)
        if specs is not None:
            m.specs = specs_of(specs)
        if photos is not None:
            m.photos = list(dict.fromkeys(p for p in photos if p)) or None
        if note is not None:
            m.note = note.strip() or None
        m.id = m.id or new_member_id()
        info[name] = m
    return _with_members(items, path, change)


def rename_member(items: list[Taxon], path: list[str], name: str, new: str, keep_old: bool = False) -> list[Taxon]:
    """The classification with an individual renamed (its details and id kept); `keep_old` adds the old name to its
    別名 (records written with it still find it)."""
    new = segment_name(new)
    if not new:
        raise ValueError("個体の名前が空")

    def change(have: list[str], info: dict[str, Member]) -> None:
        if name not in have:
            raise ValueError(f"個体「{name}」が分類「{kind_of(path)}」にない")
        if new != name and new in have:
            raise ValueError(f"個体「{new}」は分類「{kind_of(path)}」にもうある")
        m = info.pop(name, None) or Member(name)
        m.name = new
        if keep_old and new != name:
            specs = dict(m.specs or {})
            aliases = split_list(specs.get(ALIAS_FIELD, ""))
            if name not in aliases:
                specs[ALIAS_FIELD] = "、".join([*aliases, name])
            m.specs = specs
        m.id = m.id or new_member_id()
        info[new] = m
        have[have.index(name)] = new
    return _with_members(items, path, change)


def remove_member(items: list[Taxon], path: list[str], name: str) -> list[Taxon]:
    """The classification without an individual."""
    def change(have: list[str], info: dict[str, Member]) -> None:
        if name in have:
            have.remove(name)
        info.pop(name, None)
    return _with_members(items, path, change)


def move_member(items: list[Taxon], path: list[str], name: str, dst: list[str], before: Optional[str] = None) -> list[Taxon]:
    """The classification with an individual moved to class `dst` (or placed again in its own), just before `before`
    there (None: after the others); its details and id go with it."""
    src = find(items, path)
    if src is None or name not in (src.members or []):
        raise ValueError(f"個体「{name}」が分類「{kind_of(path)}」にない")
    target = find(items, dst)
    if target is None:
        raise ValueError(f"分類「{kind_of(dst)}」がない")
    if kind_of(dst) != kind_of(path) and name in (target.members or []):
        raise ValueError(f"個体「{name}」は分類「{kind_of(dst)}」にもうある")
    member = _member_copy(src.member(name), name)
    member.id = member.id or new_member_id()
    out = remove_member(items, path, name)

    def change(have: list[str], info: dict[str, Member]) -> None:
        at = have.index(before) if before in have else len(have)
        have.insert(at, name)
        info[name] = member
    return _with_members(out, dst, change)


def depth_of(items: list[Taxon], kinds: Iterable[str] = ()) -> int:
    """The deepest depth in use."""
    return max([0, *(len(t.path) for t in items), *(len(path_of(k)) for k in kinds)])


def taxa_from_json(value: Any) -> list[Taxon]:
    """Classes as the viewer writes them (`[{path, levels, nameLabel, …}]`), bad entries left out."""
    if not isinstance(value, list):
        return []
    out = []
    for t in value:
        if is_record(t) and isinstance(t.get("path"), list):
            names, info = members_of(t.get("members"))
            out.append(Taxon(path=[js_string(p) for p in t["path"]], **_own_of(t.get("levels"), t.get("nameLabel"), names, t.get("specs"), t.get("photos"),
                                                                                   t.get("activities"), info)))
    return normalize(out)


#: The attributes of a classification table by role.
DEFAULT_TAXONOMY_FIELDS: dict[str, str] = {
    "path": "PATH",
    "levels": "LEVELS",
    "nameLabel": "NAME_LABEL",
    "members": "MEMBERS",
    "specs": "SPECS",
    "photos": "PHOTOS",
    "activities": "ACTIVITIES",
    "order": "SORT_ORDER",
}
#: The table row that holds the specification fields (its PATH), not a class.
FIELDS_ROW = "#諸元項目"


#: The attributes of a table of known individuals (1 個体 1 行) by role.
DEFAULT_MEMBER_FIELDS: dict[str, str] = {
    "id": "MEMBER_ID",
    "path": "CLASS_PATH",
    "name": "MEMBER_NAME",
    "aliases": "ALIASES",
    "specs": "SPECS",
    "photos": "PHOTOS",
    "note": "NOTE",
    "order": "SORT_ORDER",
}


@dataclass
class MemberTableConfig:
    """`taxonomyTable.members`: an editable Esri table of the known individuals, one row each."""
    url: str
    fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MEMBER_FIELDS))


@dataclass
class TaxonomyTableConfig:
    """A store's `taxonomyTable`: an editable Esri table, one row per class (and a table of individuals, optional)."""
    url: str
    fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TAXONOMY_FIELDS))
    members: Optional[MemberTableConfig] = None


_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)
_NAME = re.compile(r"^[\w.]+$", re.ASCII)


def _table_of(value: Any, defaults: dict[str, str], problems: list[str], at: str) -> Optional[tuple[str, dict[str, str]]]:
    """An editable table's URL and attribute names by role (`URL` or `{url, fields}`); None when it cannot be used."""
    v = {"url": value} if isinstance(value, str) else value
    if not is_record(v) or not isinstance(v.get("url"), str) or not _FEATURE_LAYER.search(v["url"].split("?", 1)[0]):
        problems.append(f"{at} は編集できる …/FeatureServer/1 のようなテーブルの URL で指定")
        return None
    fields = dict(defaults)
    if is_record(v.get("fields")):
        for k, name in v["fields"].items():
            if k not in defaults:
                problems.append(f"{at}.fields.{k} は使えない役割")
            elif isinstance(name, str) and _NAME.match(name):
                fields[k] = name
            else:
                problems.append(f"{at}.fields.{k} は属性名で指定")
    return v["url"].split("?", 1)[0].rstrip("/"), fields


def taxonomy_table_of(value: Any, problems: list[str], at: str) -> Optional[TaxonomyTableConfig]:
    """A `taxonomyTable` entry; None (the reason in `problems`) when it cannot be used."""
    table = _table_of(value, DEFAULT_TAXONOMY_FIELDS, problems, at)
    if table is None:
        return None
    members = None
    if is_record(value) and value.get("members") is not None:
        m = _table_of(value["members"], DEFAULT_MEMBER_FIELDS, problems, f"{at}.members")
        if m is not None:
            if m[0] == table[0]:
                problems.append(f"{at}.members は分類のテーブルとは別のテーブルで指定")
            else:
                members = MemberTableConfig(url=m[0], fields=m[1])
    return TaxonomyTableConfig(url=table[0], fields=table[1], members=members)
