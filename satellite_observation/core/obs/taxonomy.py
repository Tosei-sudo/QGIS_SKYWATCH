"""
The classification (分類体系) of the things observations are about, as the
viewer (taxonomy.ts): a tree whose branches go as deep as they need, kept as
a flat list of classes by path (parents implied).

A record's kind (種別) is the path of its class written out
(`船舶 > 支援船 > 〇〇級`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..util import is_number, is_record, js_string

#: Written between the levels of a kind.
SEPARATOR = " > "
_SPLIT = re.compile(r"\s*[>›＞]\s*")


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

    def own(self) -> dict[str, Any]:
        """Its own settings, without the empty ones."""
        return _own_of(self.levels, self.name_label, self.members, self.specs, self.photos)

    def to_json(self) -> dict[str, Any]:
        """As the viewer writes a class (`nameLabel`)."""
        out: dict[str, Any] = {"path": list(self.path)}
        own = self.own()
        for k in ("levels", "name_label", "members", "specs", "photos"):
            if k in own:
                out["nameLabel" if k == "name_label" else k] = own[k]
        return out


@dataclass
class SpecField:
    """One specification field (諸元の項目)."""
    name: str
    #: text; number; list (several, 読点区切り); url.
    type: str = "text"
    unit: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, **({"unit": self.unit} if self.unit else {})}


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


def _own_of(levels, name_label, members, specs, photos) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if levels:
        out["levels"] = list(levels)
    if name_label:
        out["name_label"] = name_label
    if members:
        out["members"] = list(dict.fromkeys(members))
    s = specs_of(specs)
    if s:
        out["specs"] = s
    if photos:
        out["photos"] = list(dict.fromkeys(photos))
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
        s = specs_of(t.specs)
        if s:
            had.specs = {**s, **(had.specs or {})}
        if t.photos:
            had.photos = list(dict.fromkeys([*(had.photos or []), *t.photos]))

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
            out.append(Taxon(path=path, **_own_of(t.get("levels"), t.get("nameLabel"), t.get("members"), t.get("specs"), t.get("photos"))))
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
            node["nameLabel" if k == "name_label" else k] = v
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
        if _words(v.get("members")):
            node["members"] = _words(v.get("members"))
        if specs_of(v.get("specs")):
            node["specs"] = specs_of(v.get("specs"))
        if _words(v.get("photos")):
            node["photos"] = _words(v.get("photos"))
        if children:
            node["children"] = children
        return node

    return flatten([t for t in (tree(v, f"{at}[{i}]") for i, v in enumerate(value)) if t is not None])


def with_kinds(items: list[Taxon], kinds: Iterable[str]) -> list[Taxon]:
    """The classification with the kinds records use that it does not have yet."""
    return normalize([*items, *(Taxon(path=path_of(k)) for k in kinds)])


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
        raise ValueError("自身の下位への移動は不可")
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
            own = {"levels": t.levels, "name_label": t.name_label, "members": t.members, "specs": t.specs, "photos": t.photos}
            own.update(changes)
            out.append(Taxon(path=list(t.path), **_own_of(own["levels"], own["name_label"], own["members"], own["specs"], own["photos"])))
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
        # As the viewer: the untrimmed name decides a repeat.
        if any(x.name == f["name"] for x in out):
            continue
        unit = f.get("unit")
        out.append(SpecField(f["name"].strip(), kind, unit.strip() if isinstance(unit, str) and unit.strip() else None))
    return out


def specs_at(items: list[Taxon], path: list[str]) -> dict[str, tuple[str, list[str]]]:
    """A class's specifications, its own and those above (the nearest wins), by field: (value, the class that says)."""
    out: dict[str, tuple[str, list[str]]] = {}
    for d in range(len(path), 0, -1):
        t = find(items, path[:d])
        for name, value in ((t.specs if t else None) or {}).items():
            out.setdefault(name, (value, path[:d]))
    return out


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


def class_by_name(items: list[Taxon], name: str, fields: Optional[list[SpecField]] = None) -> Optional[list[str]]:
    """The class a name stands for: a class's own name, else one of its other names (別名) or its 英名, any case."""
    fields = DEFAULT_SPEC_FIELDS if fields is None else fields
    q = name.strip().lower()
    if not q:
        return None
    own = next((t for t in items if t.path[-1].lower() == q), None)
    if own:
        return own.path
    names = [f.name for f in fields if f.type == "list" or "名" in f.name]
    for t in items:
        for n in names:
            if any(w.strip().lower() == q for w in re.split(r"[,、]", (t.specs or {}).get(n, ""))):
                return t.path
    return None


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
            out.append(Taxon(path=[js_string(p) for p in t["path"]], **_own_of(t.get("levels"), t.get("nameLabel"), t.get("members"), t.get("specs"), t.get("photos"))))
    return normalize(out)


#: The attributes of a classification table by role.
DEFAULT_TAXONOMY_FIELDS: dict[str, str] = {
    "path": "PATH",
    "levels": "LEVELS",
    "nameLabel": "NAME_LABEL",
    "members": "MEMBERS",
    "specs": "SPECS",
    "photos": "PHOTOS",
    "order": "SORT_ORDER",
}
#: The table row that holds the specification fields (its PATH), not a class.
FIELDS_ROW = "#諸元項目"


@dataclass
class TaxonomyTableConfig:
    """A store's `taxonomyTable`: an editable Esri table, one row per class."""
    url: str
    fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TAXONOMY_FIELDS))
    token: Optional[str] = None


_FEATURE_LAYER = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)
_NAME = re.compile(r"^[\w.]+$", re.ASCII)


def taxonomy_table_of(value: Any, problems: list[str], at: str) -> Optional[TaxonomyTableConfig]:
    """A `taxonomyTable` entry; None (the reason in `problems`) when it cannot be used."""
    v = {"url": value} if isinstance(value, str) else value
    if not is_record(v) or not isinstance(v.get("url"), str) or not _FEATURE_LAYER.search(v["url"].split("?", 1)[0]):
        problems.append(f"{at} は編集できる …/FeatureServer/1 のようなテーブルの URL で指定")
        return None
    fields = dict(DEFAULT_TAXONOMY_FIELDS)
    if is_record(v.get("fields")):
        for k, name in v["fields"].items():
            if k not in DEFAULT_TAXONOMY_FIELDS:
                problems.append(f"{at}.fields.{k} は使用不可の役割")
            elif isinstance(name, str) and _NAME.match(name):
                fields[k] = name
            else:
                problems.append(f"{at}.fields.{k} は属性名で指定")
    token = v.get("token")
    return TaxonomyTableConfig(url=v["url"].split("?", 1)[0].rstrip("/"), fields=fields, token=token if isinstance(token, str) and token else None)
