"""
分類体系の取り込み（表から） (tsudo, 2026-11-22: 「Taxonomyに、CSV(というかテーブル？)
からインポートする機能が欲しいね。大分類からの階層が、それぞれ別の列にあるイメージ」).

A table (a CSV or Excel file, or a layer's attribute table) with one row per
class, or per individual: the levels in columns from the left (大分類, then
below), and optionally an individual's name, specifications (諸元), the
activities, reference photos and what an individual is called; on a row
naming one individual, its other names (個体の別名) and specifications
(個体の諸元: 番号…).

1. `guess_uses`: what each column is, by its name (the classification's own
   level names, 中分類・小分類・分類1・第 2 階層・LEVEL1…, 個体名 and the
   classification's names for individuals, the specification fields).
2. `read_rows`: each row's class. A blank on the left is the row above's
   (Excel's merged cells, the same 大分類 left out; `fill_down`); a row whose
   level columns are all blank is the class above's (one row per
   individual); the path ends at its last name. A blank between two names,
   or a blank on the left with nothing above, leaves the row out.
3. `plan_import`: the classification after the import, and what happens to
   each class (新規・変更・削除・そのまま), against the classification as it
   is: 追加・更新 (ADD: classes added, values written where the table has
   one, individuals and photos added) or 置き換え (REPLACE: the classes not in
   the table removed, except those records use and the classes above them;
   a column's item as the table says, a blank clearing it). A name that
   differs from a class's only by full / half width or case is that class
   (its name kept). Renaming and moving are not done here (the editor does
   them, with the records under the class).
4. `taxonomy_table` / `taxonomy_csv`: the classification written the same
   way (one row per class, no blank on the left; when individuals have
   specifications of their own, a row for each of them below its class), to
   edit in a spreadsheet and take in again.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from ..util import is_number, js_string
from .taxonomy import ALIAS_FIELD, Member, SpecField, Taxon, kind_of, normalize, segment_name, within

#: What a column is.
LEVEL, LEVEL_NAMES, MEMBERS, NAME_LABEL, ACTIVITIES, PHOTOS, SPEC, NEW_SPEC, SKIP = (
    "level", "levels", "members", "nameLabel", "activities", "photos", "spec", "newSpec", "skip")
#: An individual's specification (別名 among them; `arg` the field) and a new one (the column's name), on a row naming one.
MEMBER_SPEC, NEW_MEMBER_SPEC = "memberSpec", "newMemberSpec"
#: 追加・更新 / 置き換え.
ADD, REPLACE = "add", "replace"
#: What happens to a class.
NEW, CHANGED, SAME, REMOVED, KEPT = "new", "changed", "same", "removed", "kept"
STATUS_LABELS = {NEW: "＋ 新規", CHANGED: "✎ 変更", SAME: "そのまま", REMOVED: "－ 削除", KEPT: "そのまま"}
#: The longest values the classification's table keeps (docs/admin/esri-tables.md TBL_TAXONOMY).
MAX_PATH, MAX_MEMBERS, MAX_SPECS = 1000, 8000, 16000
#: Level choices offered at least (more when the table has more columns).
MIN_LEVELS = 6

#: Column names taken for a level (besides the classification's own level names).
_LEVEL_NAMES = {"大分類", "中分類", "小分類", "細分類", "細目", "分類", "種別", "種類", "級", "型式", "形式", "型", "用途", "車種", "機種", "艦種",
                "船種", "クラス"}
_LEVEL_PATTERN = re.compile(r"^(第\s*\d+\s*階層|階層\s*\d+|(分類|種別|level|lv|class|category|kind|tier)\s*[_-]?\s*\d+)$", re.IGNORECASE)
_MEMBER_NAMES = {"個体", "個体名", "個体の名前", "個体の名称", "既知の個体", "members", "member", "individual", "individuals", "艦名", "船名", "機番", "機体番号", "艦番号"}
_NAME_LABEL_NAMES = {"個体名の呼称", "name_label", "namelabel", "呼称"}
_LEVEL_NAMES_NAMES = {"階層名", "この枝の階層名", "levels", "level_names"}
_ACTIVITY_NAMES = {"活動", "activities", "activity"}
_PHOTO_NAMES = {"参考写真", "写真", "写真url", "参考写真url", "photo", "photos", "photo_url", "image_url"}
#: An individual's column: 個体の別名, 個体の諸元: 番号, 個体の番号.
_MEMBER_COLUMN = re.compile(r"^個体の\s*(?:諸元\s*[:：]\s*)?(.+)$")
#: A unit written after a column's name: 全長（m）, 全長(m), 全長[m].
_UNIT = re.compile(r"\s*[（(\[［]\s*([^）)\]］]*)\s*[）)\]］]\s*$")
_LIST = re.compile(r"[,、\n\r]+")
_PHOTO_SPLIT = re.compile(r"[\s、]+|,(?=\s*(?:https?:|data:image/|\.{0,2}/))", re.IGNORECASE)


@dataclass(frozen=True)
class Use:
    """What a column is: `arg` the level (1 the top) or the specification field."""
    kind: str
    arg: Any = None

    @property
    def label(self) -> str:
        if self.kind == LEVEL:
            return f"階層 {self.arg}"
        if self.kind == SPEC:
            return f"諸元: {self.arg}"
        if self.kind == NEW_SPEC:
            return "諸元に追加（列の名前で）"
        if self.kind == MEMBER_SPEC:
            return "個体の別名" if self.arg == ALIAS_FIELD else f"個体の諸元: {self.arg}"
        if self.kind == NEW_MEMBER_SPEC:
            return "個体の諸元に追加（列の名前で）"
        return {LEVEL_NAMES: "階層名（この枝）", MEMBERS: "既知の個体", NAME_LABEL: "個体名の呼称", ACTIVITIES: "活動", PHOTOS: "参考写真（URL）", SKIP: "使わない"}[self.kind]


SKIPPED = Use(SKIP)


def use_choices(columns: int, spec_fields: Sequence[SpecField]) -> list[Use]:
    """The uses a column can have, in the order offered."""
    levels = [Use(LEVEL, n) for n in range(1, max(MIN_LEVELS, columns) + 1)]
    member = [Use(MEMBER_SPEC, ALIAS_FIELD), *(Use(MEMBER_SPEC, f.name) for f in spec_fields if f.use != "class" and f.name != ALIAS_FIELD)]
    return [*levels, Use(LEVEL_NAMES), Use(MEMBERS), Use(NAME_LABEL), Use(ACTIVITIES), Use(PHOTOS),
            *(Use(SPEC, f.name) for f in spec_fields if f.use != "member"), Use(NEW_SPEC), *member, Use(NEW_MEMBER_SPEC), SKIPPED]


def parse_use(text: Any, spec_fields: Sequence[SpecField] = ()) -> Use:
    """
    A column's use as a script writes it: as the dialog shows it (階層 1,
    既知の個体, 諸元: 全長, 個体の別名, 個体の諸元: 番号, 使わない…) or short
    (level1, members, spec:全長, newSpec, memberAliases, memberSpec:番号,
    newMemberSpec, skip; None too). ValueError when it is none of them.
    """
    if isinstance(text, Use):
        return text
    if text is None:
        return SKIPPED
    raw = str(text).strip()
    word = norm(raw)
    m = re.match(r"^(?:階層|level|lv)\s*[:_-]?\s*(\d+)$", word)
    if m and int(m.group(1)) >= 1:
        return Use(LEVEL, int(m.group(1)))
    m = re.match(r"^(?:諸元|spec)\s*[:：]\s*(.+)$", unicodedata.normalize("NFKC", raw), re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        known = {norm(f.name): f.name for f in spec_fields}
        if norm(name) not in known:
            raise ValueError(f"諸元の項目「{name}」なし（新しい項目は「諸元に追加」）")
        return Use(SPEC, known[norm(name)])
    m = re.match(r"^(?:個体の諸元|memberspec|member_spec)\s*[:：]\s*(.+)$", unicodedata.normalize("NFKC", raw), re.IGNORECASE)
    if m or word in ("個体の別名", "memberaliases", "member_aliases"):
        name = m.group(1).strip() if m else ALIAS_FIELD
        known = {norm(f.name): f.name for f in spec_fields}
        known[norm(ALIAS_FIELD)] = ALIAS_FIELD
        if norm(name) not in known:
            raise ValueError(f"個体の諸元の項目「{name}」なし（新しい項目は「個体の諸元に追加」）")
        return Use(MEMBER_SPEC, known[norm(name)])
    short = {"levels": LEVEL_NAMES, "members": MEMBERS, "namelabel": NAME_LABEL, "name_label": NAME_LABEL, "activities": ACTIVITIES,
             "photos": PHOTOS, "newspec": NEW_SPEC, "new_spec": NEW_SPEC, "newmemberspec": NEW_MEMBER_SPEC, "new_member_spec": NEW_MEMBER_SPEC,
             "skip": SKIP, "": SKIP}
    if word in short:
        return Use(short[word])
    for kind in (LEVEL_NAMES, MEMBERS, NAME_LABEL, ACTIVITIES, PHOTOS, NEW_SPEC, NEW_MEMBER_SPEC, SKIP):
        if word in (norm(Use(kind).label), norm(column_name(Use(kind).label)[0])):
            return Use(kind)
    raise ValueError(f"列の使い方「{raw}」が不明（階層 1・既知の個体・諸元: 全長・個体の別名・使わない など）")


def norm(name: str) -> str:
    """A name as compared: full / half width, case and spaces aside (ｺﾝﾃﾅ船 = コンテナ船)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name or "")).strip().casefold()


def column_name(column: str) -> tuple[str, Optional[str]]:
    """A column's name without the unit written after it, and the unit (全長（m） → 全長, m)."""
    m = _UNIT.search(column)
    if m and m.start() > 0:
        return column[:m.start()].strip(), (m.group(1).strip() or None)
    return column.strip(), None


def member_column_name(column: str) -> tuple[str, Optional[str]]:
    """An individual's column's specification and unit (個体の諸元: 全長（m） → 全長, m; 個体の別名 → 別名)."""
    name, unit = column_name(column)
    m = _MEMBER_COLUMN.match(unicodedata.normalize("NFKC", name).strip())
    return (m.group(1).strip() if m else name), unit


def _resolved(u: Use, column: str) -> Use:
    """A use as the rows hold it: a new specification by its column's name."""
    if u.kind == NEW_SPEC:
        return Use(NEW_SPEC, column_name(column)[0])
    if u.kind == NEW_MEMBER_SPEC:
        return Use(NEW_MEMBER_SPEC, member_column_name(column)[0])
    return u


def guess_uses(columns: Sequence[str], taxonomy: Sequence[Taxon] = (), spec_fields: Sequence[SpecField] = ()) -> list[Use]:
    """What each column is, by its name; the levels numbered from the left. Without any level found, the columns on the left are."""
    levels = {norm(n) for n in _LEVEL_NAMES}
    labels = set()
    for t in taxonomy:
        levels.update(norm(n) for n in (t.levels or []) if n)
        if t.name_label:
            labels.add(norm(t.name_label))
    specs = {norm(f.name): f.name for f in spec_fields if f.use != "member"}
    member_specs = {norm(f.name): f.name for f in spec_fields if f.use != "class"}
    member_specs[norm(ALIAS_FIELD)] = ALIAS_FIELD
    only_member = {norm(f.name): f.name for f in spec_fields if f.use == "member"}
    out: list[Use] = []
    level = 0
    taken: set[Use] = set()
    for c in columns:
        name, _unit = column_name(c)
        key = norm(c)
        bare = norm(name)
        use = SKIPPED
        # 級・型式: the names of a level in several branches, as the table written here joins them.
        parts = [norm(x) for x in re.split(r"[・/／]", name) if x.strip()]
        own = _MEMBER_COLUMN.match(unicodedata.normalize("NFKC", name).strip())
        if own and norm(name) not in {norm(n) for n in _MEMBER_NAMES}:
            field_name = member_specs.get(norm(own.group(1)))
            use = Use(MEMBER_SPEC, field_name) if field_name else Use(NEW_MEMBER_SPEC)
        elif bare in only_member:
            use = Use(MEMBER_SPEC, only_member[bare])
        elif key in levels or bare in levels or _LEVEL_PATTERN.match(key) or (len(parts) > 1 and all(x in levels for x in parts)):
            level += 1
            use = Use(LEVEL, level)
        elif bare in specs and Use(SPEC, specs[bare]) not in taken:
            use = Use(SPEC, specs[bare])
        elif bare in {norm(n) for n in _MEMBER_NAMES} or bare in labels:
            use = Use(MEMBERS)
        elif bare in {norm(n) for n in _NAME_LABEL_NAMES}:
            use = Use(NAME_LABEL)
        elif bare in {norm(n) for n in _LEVEL_NAMES_NAMES}:
            use = Use(LEVEL_NAMES)
        elif bare in {norm(n) for n in _ACTIVITY_NAMES}:
            use = Use(ACTIVITIES)
        elif bare in {norm(n) for n in _PHOTO_NAMES}:
            use = Use(PHOTOS)
        if use in taken and use.kind not in (LEVEL, SKIP, NEW_SPEC, NEW_MEMBER_SPEC):
            use = SKIPPED
        taken.add(use)
        out.append(use)
    if not level:
        # The columns on the left, up to the first one with another use.
        for i, use in enumerate(out):
            if use != SKIPPED:
                break
            out[i] = Use(LEVEL, i + 1)
    return out


def check_uses(uses: Sequence[Use]) -> Optional[str]:
    """Why the uses cannot be taken (None when they can): the levels from 1 on, each once; an item once."""
    levels = sorted(u.arg for u in uses if u.kind == LEVEL)
    if not levels:
        return "階層の列なし。大分類の列に「階層 1」を選択"
    if levels != list(range(1, len(levels) + 1)):
        return "階層の列は 1 から順に、重ならないように選択"
    others = [u for u in uses if u.kind not in (LEVEL, SKIP, NEW_SPEC, NEW_MEMBER_SPEC)]
    if len(others) != len(set(others)):
        return "同じ項目を 2 つの列で選択中。1 つの列だけを選択"
    if any(u.kind in (MEMBER_SPEC, NEW_MEMBER_SPEC) for u in uses) and Use(MEMBERS) not in uses:
        return "個体の別名・諸元の列には、個体の名前の列（既知の個体）も選択"
    return None


def cell_text(value: Any) -> str:
    """A cell as text (a whole number without .0, nothing for an empty cell)."""
    if value is None:
        return ""
    if isinstance(value, float) and is_number(value):
        return js_string(int(value)) if value == int(value) else js_string(value)
    return str(value).strip()


@dataclass
class TableRow:
    """A row of the table: its number (the file's line, or a feature id) and its cells by column."""
    number: int
    values: dict[str, Any]


@dataclass
class RowClass:
    """A row read: its class (None: left out, `error` why) and its other cells by use (blank ones too)."""
    number: int
    path: Optional[list[str]]
    values: dict[Use, str] = field(default_factory=dict)
    error: str = ""


def read_rows(rows: Iterable[TableRow], columns: Sequence[str], uses: Sequence[Use], fill_down: bool = True) -> list[RowClass]:
    """Each row's class (see the module's 2.); rows with nothing written are left out without a word."""
    level_cols = sorted((i for i, u in enumerate(uses) if u.kind == LEVEL), key=lambda i: uses[i].arg)
    others = [(i, u) for i, u in enumerate(uses) if u.kind not in (LEVEL, SKIP)]
    out: list[RowClass] = []
    above: list[str] = []
    for row in rows:
        cells = [segment_name(cell_text(row.values.get(columns[i]))) for i in level_cols]
        values = {_resolved(u, columns[i]): cell_text(row.values.get(columns[i])) for i, u in others}
        if not any(cells) and not any(values.values()):
            continue
        first = next((k for k, c in enumerate(cells) if c), None)
        if first is None:
            if fill_down and above:
                out.append(RowClass(row.number, list(above), values))
            else:
                out.append(RowClass(row.number, None, values, "分類の列がすべて空欄"))
            continue
        if first and not fill_down:
            out.append(RowClass(row.number, None, values, f"「{columns[level_cols[0]]}」が空欄"))
            continue
        if first and len(above) < first:
            out.append(RowClass(row.number, None, values, f"「{columns[level_cols[first - 1]]}」が空欄（上の行にもない）"))
            continue
        prefix = list(above[:first])
        rest = cells[first:]
        last = max(k for k, c in enumerate(rest) if c)
        gap = next((k for k, c in enumerate(rest[:last]) if not c), None)
        if gap is not None:
            above = prefix + rest[:gap]
            shown = " › ".join(c or "（空欄）" for c in [*prefix, *rest[:last + 1]])
            out.append(RowClass(row.number, None, values, f"途中の階層「{columns[level_cols[first + gap]]}」が空欄（{shown}）"))
            continue
        path = prefix + rest[:last + 1]
        above = path
        if len(kind_of(path)) > MAX_PATH:
            out.append(RowClass(row.number, None, values, f"分類が長すぎる（{MAX_PATH} 文字まで）"))
            continue
        out.append(RowClass(row.number, path, values))
    return out


def generic_level_name(name: str) -> bool:
    """Whether a level column's name only numbers the level (分類1, 第 2 階層, LEVEL3): not taken as a level's name."""
    return bool(_LEVEL_PATTERN.match(norm(name)))


def level_names_of(columns: Sequence[str], uses: Sequence[Use]) -> list[str]:
    """The level columns' names, from the top."""
    return [columns[i] for i in sorted((i for i, u in enumerate(uses) if u.kind == LEVEL), key=lambda i: uses[i].arg)]


@dataclass
class ClassResult:
    """What the import does to a class."""
    path: list[str]
    status: str
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: The rows of the table that name it (as their class or above).
    rows: list[int] = field(default_factory=list)
    #: Whether the table names it.
    in_table: bool = True

    @property
    def kind(self) -> str:
        return kind_of(self.path)

    def text(self) -> str:
        """What is shown beside it: the changes, the notes, the rows."""
        parts = [*self.changes, *self.notes]
        if not self.in_table and self.status == SAME:
            parts.append("表になし（そのまま残す）")
        rows = f"行 {', '.join(str(n) for n in self.rows[:3])}{' ほか' if len(self.rows) > 3 else ''}" if self.rows else ""
        if rows and self.status in (NEW, CHANGED):
            return "、".join(parts) + (f"（{rows}）" if parts else rows)
        return "、".join(parts)


@dataclass
class ImportPlan:
    """The classification after the import, what happens to each class, the rows left out."""
    mode: str
    taxa: list[Taxon]
    classes: list[ClassResult]
    errors: list[tuple[int, str]]
    #: The specification fields with those the table adds (None: none added).
    spec_fields: Optional[list[SpecField]] = None
    rows: int = 0

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in (NEW, CHANGED, SAME, REMOVED, KEPT)}
        for c in self.classes:
            out[c.status] += 1
        return out

    @property
    def changed(self) -> bool:
        """Whether there is anything to save."""
        return self.spec_fields is not None or any(c.status in (NEW, CHANGED, REMOVED) for c in self.classes)

    def summary(self) -> str:
        n = self.counts()
        parts = [f"新規 {n[NEW]}", f"変更 {n[CHANGED]}"]
        if self.mode == REPLACE:
            parts.append(f"削除 {n[REMOVED]}")
        parts.append(f"そのまま {n[SAME] + n[KEPT]}")
        parts.append(f"取り込まない行 {len(self.errors)}")
        return "・".join(parts)

    def signature(self) -> tuple:
        """What it would do, to tell whether the classification changed between the dry run and the import."""
        return (tuple((c.kind, c.status, tuple(c.changes)) for c in self.classes),
                tuple(f.name for f in self.spec_fields) if self.spec_fields is not None else None)

    def removed(self) -> list[ClassResult]:
        return [c for c in self.classes if c.status == REMOVED]

    def removed_tops(self) -> list[str]:
        """The topmost kind of each branch a 置き換え removes (whose records a shared store is asked for)."""
        gone = {c.kind for c in self.removed()}
        return [c.kind for c in self.removed() if kind_of(c.path[:-1]) not in gone]


def _key(path: Sequence[str]) -> str:
    return "\u0000".join(path)


def _split(text: str) -> list[str]:
    return [w.strip() for w in _LIST.split(text or "") if w.strip()]


def _photos(text: str) -> list[str]:
    return [w.strip() for w in _PHOTO_SPLIT.split(text or "") if w and w.strip()]


def _names(items: Sequence[str], limit: int = 3) -> str:
    return "・".join(items[:limit]) + (f" ほか {len(items) - limit} 件" if len(items) > limit else "")


def plan_import(existing: Sequence[Taxon], spec_fields: Sequence[SpecField], rows: Sequence[RowClass], columns: Sequence[str],
                uses: Sequence[Use], mode: str = ADD, level_names: bool = True, used_kinds: Iterable[str] = ()) -> ImportPlan:
    """The classification after the import (see the module's 3.); `used_kinds` the kinds records use (a 置き換え keeps them)."""
    taxa: dict[str, Taxon] = {}
    order: list[str] = []
    for t in normalize(copy.deepcopy(list(existing))):
        taxa[_key(t.path)] = t
        order.append(_key(t.path))
    status: dict[str, ClassResult] = {}
    children: dict[tuple, str] = {(_key(t.path[:-1]), norm(t.path[-1])): k for k, t in taxa.items()}
    seen_order: list[str] = []
    own_rows: dict[str, list[RowClass]] = {}
    noted: set[tuple[str, str]] = set()

    def result(k: str) -> ClassResult:
        if k not in status:
            status[k] = ClassResult(list(taxa[k].path), SAME)
        return status[k]

    for r in rows:
        if r.path is None:
            continue
        actual: list[str] = []
        for name in r.path:
            parent = _key(actual)
            k = children.get((parent, norm(name)))
            if k is None:
                path = [*actual, name]
                k = _key(path)
                taxa[k] = Taxon(path=path)
                order.append(k)
                children[(parent, norm(name))] = k
                status[k] = ClassResult(list(path), NEW)
            elif taxa[k].path[-1] != name and (k, name) not in noted:
                noted.add((k, name))
                result(k).notes.append(f"行 {r.number} の「{name}」を「{taxa[k].path[-1]}」とみなす（全角・半角などの違い）")
            actual = list(taxa[k].path)
            c = result(k)
            if r.number not in c.rows:
                c.rows.append(r.number)
            if k not in seen_order:
                seen_order.append(k)
        own_rows.setdefault(_key(actual), []).append(r)

    # The items the table has columns for, in the columns' order.
    columns_used = list(dict.fromkeys(_resolved(u, columns[i]) for i, u in enumerate(uses)))
    fields = list(spec_fields)
    known_fields = {f.name for f in fields} | {ALIAS_FIELD}
    added_fields: list[SpecField] = []
    for i, u in enumerate(uses):
        if u.kind not in (NEW_SPEC, NEW_MEMBER_SPEC):
            continue
        name, unit = (column_name if u.kind == NEW_SPEC else member_column_name)(columns[i])
        if name and name not in known_fields:
            values = [r.values.get(Use(u.kind, name), "") for r in rows if r.path is not None]
            numeric = [v for v in values if v]
            kind = "number" if numeric and all(is_number(_number(v)) for v in numeric) else "text"
            added_fields.append(SpecField(name, kind, unit, use="both" if u.kind == NEW_SPEC else "member"))
            known_fields.add(name)
    member_uses = [u for u in columns_used if u.kind in (MEMBER_SPEC, NEW_MEMBER_SPEC) and u.arg]
    units = {f.name: f.unit for f in [*fields, *added_fields]}

    def spec_text(name: str, value: str) -> str:
        return f"{value} {units[name]}" if units.get(name) and value else value

    for k, rs in own_rows.items():
        t = taxa[k]
        c = result(k)
        changes: list[str] = []
        # The table's values of this class: individuals and photos together, the others the first written (a different one is noted).
        members: list[str] = []
        photos: list[str] = []
        single: dict[Use, tuple[str, int]] = {}
        # An individual's values: from the rows naming it alone (the first written; a different one noted).
        own: dict[str, dict[Use, tuple[str, int]]] = {}
        for r in rs:
            named = _split(r.values.get(Use(MEMBERS), ""))
            written = {u: v for u, v in r.values.items() if u.kind in (MEMBER_SPEC, NEW_MEMBER_SPEC) and u.arg}
            if len(named) == 1:
                mine = own.setdefault(named[0], {})
                for u, v in written.items():
                    had = mine.get(u)
                    if had is None or not had[0]:
                        mine[u] = (v, r.number)
                    elif v and had[0] != v:
                        c.notes.append(f"行 {r.number} の{named[0]}の{_use_word(u)}「{v}」は使わない（行 {had[1]} の「{had[0]}」）")
            elif any(written.values()):
                why = "個体が 2 つ以上。1 行に 1 個体で" if named else "既知の個体が空欄"
                c.notes.append(f"行 {r.number} の個体の別名・諸元は使わない（{why}）")
            for u, v in r.values.items():
                if u.kind in (MEMBER_SPEC, NEW_MEMBER_SPEC):
                    continue
                if u.kind == MEMBERS:
                    members.extend(m for m in _split(v) if m not in members)
                elif u.kind == PHOTOS:
                    photos.extend(p for p in _photos(v) if p not in photos)
                elif v:
                    had = single.get(u)
                    if had is None:
                        single[u] = (v, r.number)
                    elif had[0] != v:
                        c.notes.append(f"行 {r.number} の{_use_word(u)}「{v}」は使わない（行 {had[1]} の「{had[0]}」）")
        new = c.status == NEW
        replace = mode == REPLACE
        if Use(MEMBERS) in columns_used:
            now = list(t.members or [])
            add = [m for m in members if m not in now]
            gone = [m for m in now if m not in members] if replace else []
            after = [m for m in now if m not in gone] + add
            if len(",".join(after)) > MAX_MEMBERS:
                c.notes.append(f"既知の個体が長すぎて保存できないため取り込まない（{MAX_MEMBERS} 文字まで）")
            elif add or gone:
                t.members = after or None
                if add:
                    changes.append(f"既知の個体に {_names(add)} を追加" if not new else f"既知の個体 {_names(add)}")
                if gone:
                    changes.append(f"既知の個体から {_names(gone)} を外す")
        if Use(PHOTOS) in columns_used:
            now = list(t.photos or [])
            add = [p for p in photos if p not in now]
            gone = [p for p in now if p not in photos and not p.startswith("local:")] if replace else []
            if add or gone:
                t.photos = [p for p in now if p not in gone] + add or None
                if add:
                    changes.append(f"参考写真 {len(add)} 枚を追加")
                if gone:
                    changes.append(f"参考写真 {len(gone)} 枚を外す")
        specs = dict(t.specs or {})
        spec_changes = []
        for u in [x for x in columns_used if x.kind in (SPEC, NEW_SPEC)]:
            name = u.arg
            value = single.get(u, ("", 0))[0]
            old = specs.get(name, "")
            if value and value != old:
                specs[name] = value
                spec_changes.append(f"{name} {spec_text(name, old)} → {spec_text(name, value)}" if old else f"{name} {spec_text(name, value)}")
            elif replace and not value and old:
                del specs[name]
                spec_changes.append(f"{name}を消す")
        if spec_changes:
            if len(json.dumps(specs, ensure_ascii=False, separators=(",", ":"))) > MAX_SPECS:
                c.notes.append(f"諸元が長すぎて保存できないため取り込まない（{MAX_SPECS} 文字まで）")
            else:
                t.specs = specs or None
                changes.extend(spec_changes)
        for name, values in own.items() if member_uses else ():
            if name not in (t.members or []):
                continue
            m = t.member(name)
            mine = dict(m.specs or {})
            said = []
            for u in member_uses:
                value = values.get(u, ("", 0))[0]
                old = mine.get(u.arg, "")
                if value and value != old:
                    mine[u.arg] = value
                    said.append(f"{name}の{u.arg} {spec_text(u.arg, old)} → {spec_text(u.arg, value)}" if old else
                                f"{name}の{u.arg} {spec_text(u.arg, value)}")
                elif replace and not value and old:
                    del mine[u.arg]
                    said.append(f"{name}の{u.arg}を消す")
            if not said:
                continue
            if len(json.dumps(mine, ensure_ascii=False, separators=(",", ":"))) > MAX_SPECS:
                c.notes.append(f"{name}の諸元が長すぎて保存できないため取り込まない（{MAX_SPECS} 文字まで）")
                continue
            t.member_info = {**(t.member_info or {}), name: Member(name, mine or None, m.photos, m.note, m.id)}
            changes.extend(said)
        for use, attr, word in ((Use(LEVEL_NAMES), "levels", "階層名"), (Use(ACTIVITIES), "activities", "活動"),
                                (Use(NAME_LABEL), "name_label", "個体名の呼称")):
            if use not in columns_used:
                continue
            value = single.get(use, ("", 0))[0]
            old = getattr(t, attr)
            want: Any = (_split(value) or None) if attr in ("activities", "levels") else (value or None)
            if want is None and not replace:
                continue
            if want != (old or None):
                setattr(t, attr, want)
                shown = (" › " if attr == "levels" else "、").join(want) if isinstance(want, list) else want
                changes.append(f"{word}を「{shown}」に" if want else f"{word}を消す")
        c.changes.extend(changes)
        if changes and not new:
            c.status = CHANGED

    # The level columns' names for the 大分類 without level names (not when a column gives them: 階層名).
    names = level_names_of(columns, uses)
    if level_names and names and not any(generic_level_name(n) for n in names) and Use(LEVEL_NAMES) not in columns_used:
        for k in seen_order:
            t = taxa[k]
            if len(t.path) == 1 and not t.levels:
                t.levels = list(names)
                c = result(k)
                if c.status == NEW:
                    c.changes.insert(0, f"階層名「{' › '.join(names)}」")
                else:
                    c.changes.insert(0, f"階層名を「{' › '.join(names)}」に")
                    c.status = CHANGED

    seen = set(seen_order)
    used = [k for k in used_kinds if k]
    removed: list[str] = []
    for k in order:
        if k in seen:
            continue
        c = result(k)
        c.in_table = False
        if mode == REPLACE:
            path = taxa[k].path
            if any(within(u, path) for u in used):
                c.status = KEPT
                c.notes.append("表にないが、記録があるので残す")
            else:
                c.status = REMOVED
                c.notes.append("表にないので削除")
                removed.append(k)
    if mode == REPLACE:
        kept_order = [*seen_order, *(k for k in order if k not in seen and k not in removed)]
    else:
        kept_order = [k for k in order if k not in removed]
    after = normalize(taxa[k] for k in kept_order)
    shown = normalize([*(taxa[k] for k in kept_order), *(taxa[k] for k in removed)])
    classes = [result(_key(t.path)) for t in shown]
    errors = [(r.number, r.error) for r in rows if r.path is None]
    return ImportPlan(mode, after, classes, errors, [*fields, *added_fields] if added_fields else None, len(rows))


@dataclass
class ImportRequest:
    """What to take in: the table, what each column is, how (the dialog and the Python API make it)."""
    columns: list[str]
    rows: list[TableRow]
    uses: list[Use]
    mode: str = ADD
    fill_down: bool = True
    level_names: bool = True
    #: Where the table is from (a file's name, a layer's), for the log and the message.
    source: str = ""


def plan_request(request: ImportRequest, existing: Sequence[Taxon], spec_fields: Sequence[SpecField],
                 used_kinds: Iterable[str] = ()) -> ImportPlan:
    """`plan_import` of a request; ValueError when its columns' uses do not make classes (check_uses)."""
    problem = check_uses(request.uses)
    if problem:
        raise ValueError(problem)
    rows = read_rows(request.rows, request.columns, request.uses, request.fill_down)
    return plan_import(existing, spec_fields, rows, request.columns, request.uses, request.mode, request.level_names, used_kinds)


def backup_text(taxa: Sequence[Taxon]) -> str:
    """The classification as the settings file writes it (「JSON から読み込み」 takes it back)."""
    from .taxonomy import nest

    return json.dumps(nest(list(taxa)), ensure_ascii=False, indent=2) + "\n"


def backup_name(now: Optional[float] = None) -> str:
    """A backup's file name: the time it was made (`now` ms; 2026-11-22_203015.json)."""
    from datetime import datetime

    t = datetime.fromtimestamp(now / 1000) if now is not None else datetime.now()
    return f"{t.strftime('%Y-%m-%d_%H%M%S')}.json"


def _number(text: str) -> Any:
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _use_word(u: Use) -> str:
    return {SPEC: f"{u.arg}", NEW_SPEC: f"{u.arg}", MEMBER_SPEC: f"{u.arg}", NEW_MEMBER_SPEC: f"{u.arg}", LEVEL_NAMES: "階層名", ACTIVITIES: "活動",
            NAME_LABEL: "個体名の呼称"}.get(u.kind, "値")


# ---------------------------------------------------------------------------
# 書き出し: the classification as a table to edit and take in again.


def export_level_names(taxa: Sequence[Taxon], depth: int) -> list[str]:
    """
    The level columns' names: each level's name as the branches call it
    (several joined: 級・型式), else 階層 n.
    """
    out = []
    for d in range(depth):
        names: list[str] = []
        for t in taxa:
            if len(t.path) == 1 and t.levels and d < len(t.levels) and t.levels[d]:
                names.extend(n for n in re.split(r"[・/／]", t.levels[d]) if n.strip() and n.strip() not in names)
        out.append("・".join(names) if names else f"階層 {d + 1}")
    # Two levels with the same name would be read back as one column.
    for i, n in enumerate(out):
        if out.index(n) != i:
            out[i] = f"階層 {i + 1}"
    return out


def taxonomy_table(taxa: Sequence[Taxon], spec_fields: Sequence[SpecField]) -> tuple[list[str], list[list[str]]]:
    """
    The classification as a table: one row per class (every level written), its own settings beside. When
    individuals have specifications of their own (別名 among them), each individual is a row of its own below its
    class (its class's levels, its name, 個体の別名 and 個体の諸元: x; the class's columns blank).
    """
    items = normalize(taxa)
    depth = max([1, *(len(t.path) for t in items)])
    specs = [f.name for f in spec_fields if f.use != "member"]
    for t in items:
        for name in (t.specs or {}):
            if name not in specs:
                specs.append(name)
    had = {k for t in items for n in t.members or [] for k in (t.member(n).specs or {})}
    own = [n for n in [ALIAS_FIELD, *(f.name for f in spec_fields)] if n in had]
    own.extend(sorted(had - set(own)))
    own = list(dict.fromkeys(own))
    units = {f.name: f.unit for f in spec_fields}

    def head_of(n: str, prefix: str = "") -> str:
        return f"{prefix}{n}（{units[n]}）" if units.get(n) else f"{prefix}{n}"

    head = [*export_level_names(items, depth), "階層名", "個体名の呼称", "既知の個体", "活動", "参考写真", *(head_of(n) for n in specs),
            *("個体の別名" if n == ALIAS_FIELD else head_of(n, "個体の諸元: ") for n in own)]
    rows = []
    for t in items:
        levels = [t.path[d] if d < len(t.path) else "" for d in range(depth)]
        rows.append([
            *levels, "、".join(t.levels or []), t.name_label or "",
            "" if own and t.members else "、".join(t.members or []),
            "、".join(t.activities or []), "\n".join(p for p in (t.photos or []) if not p.startswith("local:")),
            *((t.specs or {}).get(n, "") for n in specs), *("" for _ in own),
        ])
        for name in (t.members or []) if own else ():
            mine = t.member(name).specs or {}
            rows.append([*levels, "", "", name, "", "", *("" for _ in specs), *(mine.get(n, "") for n in own)])
    return head, rows


def taxonomy_csv(taxa: Sequence[Taxon], spec_fields: Sequence[SpecField]) -> str:
    """The table as CSV with a BOM (opens in a spreadsheet as it is)."""
    head, rows = taxonomy_table(taxa, spec_fields)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(head)
    w.writerows(rows)
    return "﻿" + out.getvalue()


def taxonomy_csv_name(now: Optional[float] = None) -> str:
    from datetime import datetime

    t = datetime.fromtimestamp(now / 1000) if now is not None else datetime.now()
    return f"taxonomy-{t.strftime('%Y%m%d')}.csv"


__all__ = [
    "ADD", "REPLACE", "NEW", "CHANGED", "SAME", "REMOVED", "KEPT", "LEVEL", "LEVEL_NAMES", "MEMBERS", "NAME_LABEL", "ACTIVITIES", "PHOTOS", "SPEC",
    "NEW_SPEC", "SKIP", "STATUS_LABELS", "Use", "SKIPPED", "TableRow", "RowClass", "ClassResult", "ImportPlan", "use_choices", "guess_uses",
    "check_uses", "read_rows", "plan_import", "taxonomy_table", "taxonomy_csv", "taxonomy_csv_name", "norm",
    "column_name", "cell_text", "level_names_of", "generic_level_name", "ImportRequest", "plan_request", "backup_text", "backup_name", "parse_use",
    "export_level_names", "MEMBER_SPEC", "NEW_MEMBER_SPEC", "member_column_name",
]
