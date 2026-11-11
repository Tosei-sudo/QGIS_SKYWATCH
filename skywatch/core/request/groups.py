"""
依頼グループ (request groups): whose a request is, so that each person can
find their own among many (tsudo, 2026-11-11: 「撮像依頼、件数が増えてくると、
どれが自分の依頼かわからなくなる」).

The groups are rows of an Esri table (`requestStores[].groups`,
TBL_REQUEST_GROUP): data that changes as people and work change, so it is
kept in Esri, not in the settings file. A request names its group in the
ledger (REQUEST_GROUP); the history keeps the group as it was when the day
was written (for the dashboards). A group's MEMBERS are the user names
(the environment variable USERNAME, as `editTracking`) of its people:
「自分のグループ」 is every group the user is a member of.

The 「撮像依頼」, 「撮像計画ファイル出力」 and 「撮像予定」 docks filter by group
with the choices made here: all, 自分のグループ, one group, or グループなし.
Names are compared without case or surrounding spaces.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError
from ..util import is_number

#: The attribute names used when the settings do not map a role.
DEFAULT_GROUP_FIELDS: dict[str, str] = {
    "name": "GROUP_NAME", "users": "MEMBERS", "order": "SORT_ORDER", "active": "ACTIVE", "note": "NOTE",
}
#: The filter's values besides a group's name: every request (None), the user's groups, and those with no group.
ALL: Optional[str] = None
MINE = "*mine*"
NO_GROUP = "*none*"
_SEPARATORS = re.compile(r"[,、，;；\s]+")


@dataclass(frozen=True)
class RequestGroup:
    """One row of the groups table."""
    name: str
    #: The user names of its people.
    users: tuple = ()
    #: Where it is listed (smaller first; None after those with a number).
    order: Optional[float] = None
    #: Offered for new requests (a group no longer used stays listed in the filters, for its old requests).
    active: bool = True
    note: str = ""

    def has(self, user: str) -> bool:
        """Whether a user (a user name) is one of its people."""
        return bool(user.strip()) and any(same(u, user) for u in self.users)


def same(a: str, b: str) -> bool:
    """Whether two group (or user) names are the same, case and surrounding spaces aside."""
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def users_of(value: Any) -> tuple:
    """The user names of a MEMBERS value: 「tsudo, yamada」 (commas, 、, semicolons, spaces or lines between them)."""
    out: list[str] = []
    for name in _SEPARATORS.split(_text(value)):
        if name and not any(same(name, u) for u in out):
            out.append(name)
    return tuple(out)


def _active(v: Any) -> bool:
    if v is None or v == "":
        return True
    if v is False or (is_number(v) and v == 0):
        return False
    return not re.fullmatch(r"(0|false|no|off|いいえ|無効|停止)", _text(v), re.IGNORECASE)


def group_of(a: dict, f: dict) -> Optional[RequestGroup]:
    """The group of a row's attributes (by the table's names); None without a name."""
    name = _text(a.get(f["name"]))
    if not name:
        return None
    order = a.get(f["order"])
    if isinstance(order, str):
        try:
            order = float(order)
        except ValueError:
            order = None
    return RequestGroup(name=name, users=users_of(a.get(f["users"])),
                        order=float(order) if is_number(order) and math.isfinite(order) else None,
                        active=_active(a.get(f["active"])) if f["active"] in a else True, note=_text(a.get(f["note"])))


def groups_of(rows: Iterable[dict], f: dict) -> list[RequestGroup]:
    """
    The groups of the table's rows, in their order (SORT_ORDER, then name).
    A name given twice is one group: the first row's, with the people of
    both, used while either row is.
    """
    out: dict[str, RequestGroup] = {}
    for a in rows:
        g = group_of(a, f)
        if g is None:
            continue
        key = g.name.casefold()
        if key in out:
            first = out[key]
            users = first.users + tuple(u for u in g.users if not any(same(u, x) for x in first.users))
            out[key] = RequestGroup(first.name, users, first.order, first.active or g.active, first.note or g.note)
        else:
            out[key] = g
    return sorted(out.values(), key=lambda g: (g.order is None, g.order or 0, g.name.casefold()))


def load_groups(client: EsriClient, url: Optional[str], fields: dict, token: Optional[str],
                cancelled=None) -> list[RequestGroup]:
    """The groups of the table at `url` ([] when there is none); EsriError when it cannot be read."""
    if not url:
        return []
    info = client.layer(url, token, refresh=True)
    if fields["name"] not in set(info.field_types):
        raise EsriError(f"依頼グループのテーブルに属性 {fields['name']} なし")
    rows = client.query_all(url, "1=1", token, geometry=False, limit=10000, cancelled=cancelled)
    return groups_of((r.get("attributes") or {} for r in rows), fields)


def read_groups(client: EsriClient, url: Optional[str], fields: dict, token: Optional[str],
                cancelled=None) -> tuple[Optional[list], str]:
    """
    The groups ([] without a table), or (None, why) when the table cannot be
    read: the requests are then listed with the groups found on them.
    """
    try:
        return load_groups(client, url, fields, token, cancelled), ""
    except Exception as error:  # noqa: BLE001 (never stops the requests being read)
        return None, f"依頼グループのテーブルを読めず（{error}）"


# --- Choosing -------------------------------------------------------------------------------

def mine(groups: Sequence[RequestGroup], user: str) -> list[str]:
    """The names of the groups the user is one of the people of."""
    return [g.name for g in groups if g.has(user)]


def matches(group: str, chosen: Optional[str], my_groups: Iterable[str] = ()) -> bool:
    """Whether a request of `group` passes the filter `chosen` (ALL, MINE, NO_GROUP or a group's name)."""
    if chosen is ALL or chosen == "":
        return True
    if chosen == MINE:
        return bool(group.strip()) and any(same(group, g) for g in my_groups)
    if chosen == NO_GROUP:
        return not group.strip()
    return same(group, chosen)


def listed(groups: Sequence[RequestGroup], seen: Iterable[str] = ()) -> list[str]:
    """
    The groups a filter lists: those of the table in its order, then names
    found on requests but not in the table (a group taken off it, or typed
    while there was none), by name.
    """
    names = [g.name for g in groups]
    extra: dict[str, str] = {}
    for s in seen:
        s = (s or "").strip()
        if s and not any(same(s, n) for n in names):
            extra.setdefault(s.casefold(), s)
    return names + sorted(extra.values(), key=str.casefold)


def offered(groups: Sequence[RequestGroup], current: str = "") -> list[str]:
    """The groups a request can be put in (those in use), and the one it has (even one no longer used)."""
    names = [g.name for g in groups if g.active]
    current = (current or "").strip()
    if current and not any(same(current, n) for n in names):
        names.append(current)
    return names


def first_group(groups: Sequence[RequestGroup], user: str, last: str = "", table: bool = True) -> str:
    """
    The group a new request starts in: the one last saved while it is still
    in use (any, without a table), else the first group in use that the
    user is one of the people of, else none.
    """
    last = (last or "").strip()
    if last and (not table or any(g.active and same(g.name, last) for g in groups)):
        return next((g.name for g in groups if same(g.name, last)), last)
    return next((g.name for g in groups if g.active and g.has(user)), "")


def filter_text(chosen: Optional[str]) -> str:
    """The filter as words (for a status line)."""
    if chosen is ALL or chosen == "":
        return "すべてのグループ"
    if chosen == MINE:
        return "自分のグループ"
    if chosen == NO_GROUP:
        return "グループなし"
    return chosen
