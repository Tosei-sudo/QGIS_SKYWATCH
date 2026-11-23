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

The 「撮像依頼」, 「撮像計画ファイル出力」, 「撮像予定」 and 「地点ごとの撮像予定」
docks filter by group with the choices made here: all, or any of
自分のグループ, the groups and グループなし ticked (2026-11-16: several at once).
Names are compared without case or surrounding spaces.

Since 2026-11-23 (tsudo: 「撮像依頼のグループをミカゲから追加、編集できる機能が欲しい」)
the groups are also added, changed, put in order, stopped and deleted from
the plugin (「依頼グループ…」, ui/request_group_dialog.py) by those who ask
for images or plan (`request_groups.edit`): `save_group`, `save_orders`,
`delete_groups` here, and `rename_requests` carrying a new name to the
ledger's requests (the history keeps the name it was written with).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Sequence, Union

from ..esri import EsriClient, EsriError, fit_value
from ..util import is_number

if TYPE_CHECKING:
    from .bulk import BulkResult
    from .store import RequestStoreConfig

#: The attribute names used when the settings do not map a role.
DEFAULT_GROUP_FIELDS: dict[str, str] = {
    "name": "GROUP_NAME", "users": "MEMBERS", "order": "SORT_ORDER", "active": "ACTIVE", "note": "NOTE",
}
#: The filter's values besides a group's name: every request (None), the user's groups, and those with no group.
ALL: Optional[str] = None
MINE = "*mine*"
NO_GROUP = "*none*"
_SEPARATORS = re.compile(r"[,、，;；\s]+")
#: The longest name, members and note (TBL_REQUEST_GROUP's lengths; a table's own shorter ones cut them too).
NAME_MAX, MEMBERS_MAX, NOTE_MAX = 255, 4000, 1000
#: The most rows read of the table (a few dozen groups in practice).
LIMIT = 10000


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
    #: Its rows in the table (object ids; several when a name was given twice): saved on the first, the others
    #: deleted then (their people are in it). () before it is saved.
    object_ids: tuple = ()

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


def group_of(a: dict, f: dict, oid: str = "") -> Optional[RequestGroup]:
    """The group of a row's attributes (by the table's names; `oid`: the object id's); None without a name."""
    name = _text(a.get(f["name"]))
    if not name:
        return None
    order = a.get(f["order"])
    if isinstance(order, str):
        try:
            order = float(order)
        except ValueError:
            order = None
    object_id = a.get(oid) if oid else None
    return RequestGroup(name=name, users=users_of(a.get(f["users"])),
                        order=float(order) if is_number(order) and math.isfinite(order) else None,
                        active=_active(a.get(f["active"])) if f["active"] in a else True, note=_text(a.get(f["note"])),
                        object_ids=(int(object_id),) if is_number(object_id) else ())


def groups_of(rows: Iterable[dict], f: dict, oid: str = "") -> list[RequestGroup]:
    """
    The groups of the table's rows, in their order (SORT_ORDER, then name).
    A name given twice is one group: the first row's, with the people of
    both, used while either row is.
    """
    out: dict[str, RequestGroup] = {}
    for a in rows:
        g = group_of(a, f, oid)
        if g is None:
            continue
        key = g.name.casefold()
        if key in out:
            first = out[key]
            users = first.users + tuple(u for u in g.users if not any(same(u, x) for x in first.users))
            out[key] = RequestGroup(first.name, users, first.order, first.active or g.active, first.note or g.note,
                                    first.object_ids + g.object_ids)
        else:
            out[key] = g
    return sorted(out.values(), key=lambda g: (g.order is None, g.order or 0, g.name.casefold()))


def load_groups(client: EsriClient, url: Optional[str], fields: dict, token: Optional[str],
                cancelled=None) -> list[RequestGroup]:
    """The groups of the table at `url` ([] when there is none); EsriError when it cannot be read."""
    if not url:
        return []
    info = _table(client, url, fields, token)
    rows = client.query_all(url, "1=1", token, geometry=False, limit=LIMIT, cancelled=cancelled)
    return groups_of((r.get("attributes") or {} for r in rows), fields, info.object_id_field)


def _table(client: EsriClient, url: str, fields: dict, token: Optional[str]):
    """The table's description (read again); EsriError (E704) without the name's attribute."""
    info = client.layer(url, token, refresh=True)
    if fields["name"] not in set(info.field_types):
        raise EsriError(f"依頼グループの表に必要な項目がない（管理者に連絡: {fields['name']}）", code="E704")
    return info


def read_groups(client: EsriClient, url: Optional[str], fields: dict, token: Optional[str],
                cancelled=None) -> tuple[Optional[list], str]:
    """
    The groups ([] without a table), or (None, why) when the table cannot be
    read: the requests are then listed with the groups found on them.
    """
    try:
        return load_groups(client, url, fields, token, cancelled), ""
    except Exception as error:  # noqa: BLE001 (never stops the requests being read)
        return None, f"依頼グループのテーブルの読み込みに失敗: {error}"


# --- Choosing -------------------------------------------------------------------------------

def mine(groups: Sequence[RequestGroup], user: str) -> list[str]:
    """The names of the groups the user is one of the people of."""
    return [g.name for g in groups if g.has(user)]


def matches(group: str, chosen: Union[None, str, Iterable[str]], my_groups: Iterable[str] = ()) -> bool:
    """
    Whether a request of `group` passes the filter `chosen`: ALL, MINE,
    NO_GROUP or a group's name, or several of them (any; none: all).
    """
    if chosen is ALL or chosen == "":
        return True
    if not isinstance(chosen, str):
        chosen = [c for c in chosen if c]
        my_groups = tuple(my_groups)
        return not chosen or any(matches(group, c, my_groups) for c in chosen)
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


def filter_text(chosen: Union[None, str, Iterable[str]]) -> str:
    """The filter as words (for a status line; several: 「第1班・グループなし」)."""
    if chosen is not ALL and not isinstance(chosen, str):
        chosen = [c for c in chosen if c]
        return "・".join(filter_text(c) for c in chosen) if chosen else filter_text(ALL)
    if chosen is ALL or chosen == "":
        return "すべてのグループ"
    if chosen == MINE:
        return "自分のグループ"
    if chosen == NO_GROUP:
        return "グループなし"
    return chosen


# --- Adding and changing (「依頼グループ…」, 2026-11-23) ------------------------------------------

def members_text(users: Iterable[str]) -> str:
    """The MEMBERS value of user names: 「tsudo, yamada」."""
    return ", ".join(u for u in users if u)


def _is(a: RequestGroup, b: RequestGroup) -> bool:
    """Whether two groups are the same rows of the table (a group not saved yet is no other)."""
    return bool(a.object_ids) and bool(set(a.object_ids) & set(b.object_ids))


def check_group(g: RequestGroup, groups: Sequence[RequestGroup]) -> list[str]:
    """What keeps a group from being saved among `groups` (the table's): no name, too long, a name another group has."""
    problems = []
    name = g.name.strip()
    if not name:
        problems.append("グループ名を入れる")
    elif len(name) > NAME_MAX:
        problems.append(f"グループ名は {NAME_MAX} 文字まで")
    else:
        other = next((o for o in groups if same(o.name, name) and not _is(o, g)), None)
        if other is not None:
            problems.append(f"同じ名前のグループ「{other.name}」がある（大文字・小文字と前後の空白は区別しない）")
    if len(members_text(g.users)) > MEMBERS_MAX:
        problems.append(f"メンバーが長すぎる（カンマ区切りで {MEMBERS_MAX} 文字まで）")
    if len(g.note) > NOTE_MAX:
        problems.append(f"メモは {NOTE_MAX} 文字まで")
    return problems


def group_attributes(g: RequestGroup, fields: dict, types: dict, lengths: dict) -> dict:
    """The row of a group as the table takes it (only the attributes it has; 有効 as 1 / 0, or 「1」「0」 in text)."""
    values = {"name": g.name.strip(), "users": members_text(g.users) or None, "order": g.order,
              "active": g.active, "note": g.note.strip() or None}
    out = {}
    for role, v in values.items():
        name = fields.get(role)
        if not name or name not in types:
            continue
        t = types[name]
        if role == "active":
            v = ("1" if v else "0") if t == "esriFieldTypeString" else (1 if v else 0)
        elif role == "order" and v is not None and "Integer" in (t or ""):
            v = int(round(v))
        if isinstance(v, str) and lengths.get(name):
            v = v[:lengths[name]]
        out[name] = fit_value(v, t)
    return out


def _answer(results: list, what: str) -> dict:
    r = (results or [{}])[0]
    if not r.get("success"):
        raise EsriError(code="E305", detail=f"{what}: " + ((r.get("error") or {}).get("description") or ""))
    return r


def save_group(client: EsriClient, url: str, fields: dict, g: RequestGroup, token: Optional[str] = None) -> RequestGroup:
    """
    Adds the group (no rows yet) or saves it over its first row, the other
    rows of its name deleted (their people are in it). The table is read
    first, so that a name someone else has just given (or a group deleted
    on another PC) is told, not saved twice. The group as saved.
    """
    info = _table(client, url, fields, token)
    oid = info.object_id_field
    rows = client.query_all(url, "1=1", token, geometry=False, limit=LIMIT)
    now = groups_of((r.get("attributes") or {} for r in rows), fields, oid)
    there = {i for x in now for i in x.object_ids}
    if g.object_ids and not set(g.object_ids) & there:
        raise EsriError(code="E302", detail=f"依頼グループ「{g.name}」")
    problems = check_group(g, now)
    if problems:
        raise ValueError("、".join(problems))
    g = replace(g, name=g.name.strip(), note=g.note.strip())
    attributes = group_attributes(g, fields, info.field_types, info.field_lengths)
    if not g.object_ids:
        result = client.apply_edits(url, adds=[{"attributes": attributes}], token=token)
        r = _answer(result.add_results, "追加")
        return replace(g, object_ids=(int(r["objectId"]),) if is_number(r.get("objectId")) else ())
    first, *rest = [i for i in g.object_ids if i in there]
    result = client.apply_edits(url, updates=[{"attributes": {**attributes, oid: first}}], deletes=rest, token=token)
    _answer(result.update_results, "更新")
    return replace(g, object_ids=(first,))


def order_changes(groups: Sequence[RequestGroup], index: int, step: int) -> list[RequestGroup]:
    """
    The groups whose 表示順 changes when groups[index] moves `step` places
    (-1 up, +1 down): all are numbered 1, 2, 3… in the new order, so that
    groups with no number (or the same one) come in order too.
    """
    to = index + step
    if not 0 <= index < len(groups) or not 0 <= to < len(groups) or step == 0:
        return []
    moved = list(groups)
    moved.insert(to, moved.pop(index))
    return [replace(g, order=float(k + 1)) for k, g in enumerate(moved) if g.order != k + 1]


def save_orders(client: EsriClient, url: str, fields: dict, groups: Sequence[RequestGroup],
                token: Optional[str] = None) -> tuple[int, list[str]]:
    """Saves the 表示順 of the groups (one request); how many were saved and why the others were not."""
    info = _table(client, url, fields, token)
    name = fields.get("order")
    if not name or name not in info.field_types:
        return 0, [f"表示順の項目がない（管理者に連絡: {name}）"]
    todo = [g for g in groups if g.object_ids]
    problems = [f"{g.name}: 未保存" for g in groups if not g.object_ids]
    if not todo:
        return 0, problems
    t = info.field_types[name]
    updates = [{"attributes": {info.object_id_field: g.object_ids[0],
                               name: int(round(g.order)) if g.order is not None and "Integer" in t else g.order}} for g in todo]
    results = client.apply_edits(url, updates=updates, token=token).update_results
    saved = 0
    for g, r in zip(todo, results):
        if r.get("success"):
            saved += 1
        else:
            problems.append(f"{g.name}: " + ((r.get("error") or {}).get("description") or "保存失敗"))
    return saved, problems


def delete_groups(client: EsriClient, url: str, groups: Sequence[RequestGroup],
                  token: Optional[str] = None) -> tuple[list[str], list[str]]:
    """Deletes the groups (each of their rows); the names deleted and why the others were not."""
    mine = [g for g in groups if g.object_ids]
    failed = [f"{g.name}: 未保存" for g in groups if not g.object_ids]
    if not mine:
        return [], failed
    try:
        results = client.delete_features(url, [i for g in mine for i in g.object_ids], token)
    except EsriError as error:
        return [], failed + [f"{g.name}: {error}" for g in mine]
    by_oid = {r.get("objectId"): r for r in results if isinstance(r, dict)}
    done = []
    for g in mine:
        answers = [by_oid.get(i) or {"success": all(x.get("success") for x in results if isinstance(x, dict))} for i in g.object_ids]
        bad = next((a for a in answers if not a.get("success")), None)
        if bad is None:
            done.append(g.name)
        else:
            failed.append(f"{g.name}: " + ((bad.get("error") or {}).get("description") or "削除失敗"))
    return done, failed


def group_counts(client: EsriClient, store: RequestStoreConfig, token: Optional[str],
                 cancelled: Optional[Callable[[], bool]] = None) -> dict[str, int]:
    """How many requests of the ledger are in each group (by the name in lower case; "" for none). Only the group is read."""
    from .store import _layer

    name = store.fields.get("group")
    names, oid = _layer(client, store.url, token, store.label, (store.fields["id"],))
    if not name or name not in names:
        return {}
    rows = client.query_all(store.url, "1=1", token, out_fields=f"{oid},{name}", geometry=False, limit=50000, cancelled=cancelled)
    out: dict[str, int] = {}
    for r in rows:
        key = _text((r.get("attributes") or {}).get(name)).casefold()
        out[key] = out.get(key, 0) + 1
    return out


def rename_requests(client: EsriClient, store: RequestStoreConfig, token: Optional[str], old: str, new: str,
                    now: Callable[[], float], canceled: Callable[[], bool] = lambda: False) -> BulkResult:
    """
    Puts the ledger's requests of group `old` in `new` (the group renamed):
    read again, saved together as 「一括編集」 does (one changed by someone
    else since is told, not overwritten). The history is left as written.
    """
    from .bulk import BulkResult, RequestChange, apply_change
    from .store import load_requests

    if same(old, new):
        return BulkResult()
    requests = [r for r in load_requests(client, store, token, canceled) if same(r.group, old)]
    return apply_change(client, store, token, requests, RequestChange(group=new.strip()), now, canceled)
