"""
The 依頼グループ filter shared by the 「撮像依頼」, 「撮像計画ファイル出力」,
「撮像予定」 and 「地点ごとの撮像予定」 docks (core/request/groups.py): a
drop-down of check boxes (ui/check_filter.py; 2026-11-16, several groups at
once) with 自分のグループ (the groups whose MEMBERS hold the user name), each
group and グループなし; nothing ticked is すべてのグループ. The choice is kept
per user and dock (one choice as the single-choice box kept it). The box is
hidden while there are no groups at all (no table and no request with
one), unless a choice kept from before still filters.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from qgis.PyQt.QtCore import QCoreApplication

from ..core import edit_stamp
from ..core.request.groups import MINE, NO_GROUP, RequestGroup, listed, matches, mine, same
from .check_filter import CheckFilter

#: The item of the request form and the bulk edit that takes a request out of its group.
NO_GROUP_TEXT = "（グループなし）"


def _tr(text: str) -> str:
    return QCoreApplication.translate("GroupFilter", text)


class GroupFilter(CheckFilter):
    """The 依頼グループ filter of one dock (`where`: the key its choice is kept under)."""

    def __init__(self, where: str, parent=None):
        super().__init__(_tr("すべてのグループ"), key=f"request_groups/{where}", parent=parent,
                         tip=_tr("依頼グループで絞り込み（チェックしたグループのどれか。自分のグループ: 依頼グループのテーブルで"
                                 "メンバーに自分のユーザー名があるもの）。次回も同じ選択"))
        self.where = where
        self.groups: list[RequestGroup] = []
        #: The user's groups (for 自分のグループ).
        self.my: tuple = ()
        self._filled = False
        self._seen: Optional[frozenset] = None
        self.set_groups([], ())

    @staticmethod
    def same(a, b) -> bool:
        if a in (MINE, NO_GROUP) or b in (MINE, NO_GROUP):
            return a == b
        return isinstance(a, str) and isinstance(b, str) and same(a, b)

    def set_groups(self, groups: Sequence[RequestGroup], seen: Iterable[str] = (), user: Optional[str] = None,
                   force: bool = True) -> None:
        """
        The choices: the table's groups and those found on the requests or
        rows shown (`seen`), the choices kept. Without `force`, nothing is
        done while the groups and the names seen are as last time.
        """
        seen = frozenset(s.strip() for s in seen if s and s.strip())
        if not force and self._filled and list(groups) == self.groups and seen == self._seen:
            return
        self.groups, self._seen = list(groups), seen
        user = edit_stamp.user_name() if user is None else user
        self.my = tuple(mine(self.groups, user))
        ticked = self.chosen()
        names = listed(self.groups, seen)
        names += [v for v in ticked if v not in (MINE, NO_GROUP) and isinstance(v, str) and not any(same(v, n) for n in names)]
        stopped = {g.name.casefold() for g in self.groups if not g.active}
        notes = {g.name.casefold(): g.note for g in self.groups if g.note}
        items = []
        if self.my or MINE in ticked:
            items.append((_tr("自分のグループ"), MINE, "、".join(self.my) or _tr("メンバーに自分のユーザー名（{u}）があるグループなし").format(
                u=user or _tr("不明"))))
        for n in names:
            items.append((n + (_tr("（使用停止）") if n.casefold() in stopped else ""), n, notes.get(n.casefold(), "")))
        items.append((_tr("グループなし"), NO_GROUP, ""))
        self.set_items(items)
        self._filled = True
        self.setVisible(bool(names) or not self.is_all())

    def choose(self, values) -> None:
        """Ticks these groups only (one, a list, or None / [] for all); a group not listed is added."""
        super().choose(values)
        if any(not any(self.same(v, k) for _t, k in self.items()) for v in self.chosen()):
            self.set_groups(self.groups, self._seen or ())

    def passes(self, group: str) -> bool:
        """Whether a request (or a row) of this group is shown."""
        return matches(group or "", self.chosen(), self.my)
