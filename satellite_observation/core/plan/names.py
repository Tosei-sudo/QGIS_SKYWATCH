"""
Satellite names that differ between catalogs: the satellite catalog says
「WorldView-3」 where the image catalog's sensor says 「WV03」. The tables
of `satelliteAliases` (Esri tables, so the names are kept with the data,
not in the settings file) give a satellite's other names, a row each (or
several in one row, separated by commas). Names are compared by their
letters and digits only, regardless of case (`WorldView-3` = `worldview3`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from ..esri import EsriClient
from ..util import is_name, is_record, is_url, layer_url

TokenFor = Callable[[str, Optional[str]], Optional[str]]

ALIAS_ROLES = ("satellite", "alias")
ALIAS_ROLE_LABELS = {"satellite": "衛星名", "alias": "別名"}
_TABLE = re.compile(r"/(FeatureServer|MapServer)/\d+/?$", re.IGNORECASE)
_SEPARATORS = re.compile(r"[,、，;；\r\n]+")


def plain(s: Any) -> str:
    """Letters and digits only, lower case: `WorldView-3` → `worldview3`."""
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def aliases_of(value: Any) -> tuple[str, ...]:
    """The names of an `aliases` attribute: 「WV03, WV3」 (commas, 、 or semicolons between them), blanks left out."""
    if value is None or isinstance(value, (dict, bool)):
        return ()
    items = value if isinstance(value, (list, tuple)) else _SEPARATORS.split(str(value))
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


@dataclass
class SatelliteNames:
    """Which names are one satellite's: each group holds a satellite's name, id and aliases (plain)."""
    groups: list[frozenset] = field(default_factory=list)

    @staticmethod
    def of(entries: Iterable[Sequence[Any]]) -> "SatelliteNames":
        """The groups of (name, id, *aliases) entries; entries without aliases add nothing."""
        groups: list[frozenset] = []
        for entry in entries:
            names = frozenset(p for p in (plain(n) for n in entry) if p)
            if len(names) > 1 and names not in groups:
                groups.append(names)
        return SatelliteNames(groups)

    def __bool__(self) -> bool:
        return bool(self.groups)

    def expand(self, *values: Any) -> set[str]:
        """The plain names of `values`, and of every name of the satellites they name."""
        given = {p for p in (plain(v) for v in values) if p}
        out = set(given)
        # Not passed on: 「SkySat」 may be an alias of SkySat-C12 and of SkySat-C13 without the two being one.
        for g in self.groups:
            if given & g:
                out |= g
        return out

    def same(self, a: Any, b: Any) -> bool:
        """Whether two names are one satellite's (the same plain name, or names of one group)."""
        pa, pb = plain(a), plain(b)
        if not pa or not pb:
            return False
        return pa == pb or any(pa in g and pb in g for g in self.groups)


NO_NAMES = SatelliteNames()


def name_rank(value: Any, names: set[str]) -> int:
    """How well a catalog value (an image's sensor) names one of `names`: 2 the same, 1 one inside the other, 0 not."""
    s = plain(value)
    if not s or not names:
        return 0
    if s in names:
        return 2
    return 1 if any(s in n or n in s for n in names) else 0


def best_named(found: Sequence[dict], value_of: Callable[[dict], Any], names: set[str]) -> dict:
    """The feature whose value names the satellite best (the same name, then one inside the other), else the first."""
    best, rank = found[0], 0
    for f in found:
        r = name_rank(value_of(f), names)
        if r > rank:
            best, rank = f, r
            if r == 2:
                break
    return best


@dataclass
class AliasTableConfig:
    """One table of `satelliteAliases`: a row per other name of a satellite."""
    label: str
    url: str
    #: `satellite`: the satellite's name (or id) in the satellite catalog; `alias`: its other name(s).
    fields: dict[str, str]
    token: Optional[str] = None
    where: Optional[str] = None


def alias_tables_of(value: Any, problems: list[str]) -> list[AliasTableConfig]:
    """The tables of `satelliteAliases` (one object, or an array of them), each checked on its own."""
    items = value if isinstance(value, list) else [value]
    out: list[AliasTableConfig] = []
    for i, item in enumerate(items):
        at = f"satelliteAliases[{i}]" if isinstance(value, list) else "satelliteAliases"
        if not is_record(item):
            problems.append(f"{at} はオブジェクトで指定")
            continue
        url = item.get("url")
        if not is_url(url) or not _TABLE.search(url.split("?", 1)[0]):
            problems.append(f"{at} の url は …/FeatureServer/2 のようなテーブルの URL で指定")
            continue
        fields = item.get("fields")
        if not is_record(fields):
            problems.append(f"{at} に fields（属性名の対応）なし")
            continue
        mapped: dict[str, str] = {}
        for role in ALIAS_ROLES:
            name = fields.get(role)
            if name is None or name == "":
                problems.append(f"{at} に fields.{role}（{ALIAS_ROLE_LABELS[role]}の属性）なし")
            elif is_name(name):
                mapped[role] = name
            else:
                problems.append(f"{at} の fields.{role} は属性名（英数字と _）で指定")
        if len(mapped) < len(ALIAS_ROLES):
            continue
        label, token, where = item.get("label"), item.get("token"), item.get("where")
        out.append(AliasTableConfig(
            label=label if isinstance(label, str) and label else f"衛星名の対応表 {i + 1}",
            url=layer_url(url), fields=mapped,
            token=token if isinstance(token, str) and token else None,
            where=where.strip() if isinstance(where, str) and where.strip() else None))
    return out


def load_satellite_names(client: EsriClient, tables: Sequence[AliasTableConfig], token_for: TokenFor,
                         cancelled: Optional[Callable[[], bool]] = None) -> tuple[SatelliteNames, list[str]]:
    """The names of the alias tables (rows of one satellite grouped together), and what failed."""
    by_satellite: dict[str, list[str]] = {}
    problems: list[str] = []
    for table in tables:
        f = table.fields
        try:
            rows = client.query_all(table.url, table.where or "1=1", token_for(table.url, table.token),
                                    out_fields=f"{f['satellite']},{f['alias']}", geometry=False, cancelled=cancelled)
        except Exception as error:  # noqa: BLE001 (reported, the others still read)
            problems.append(f"{table.label} の読み込みに失敗（{error}）")
            continue
        for row in rows:
            a = row.get("attributes") or {}
            satellite = str(a.get(f["satellite"]) or "").strip()
            if plain(satellite):
                by_satellite.setdefault(plain(satellite), [satellite]).extend(aliases_of(a.get(f["alias"])))
    return SatelliteNames.of(by_satellite.values()), problems
