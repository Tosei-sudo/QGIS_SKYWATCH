"""
The 依頼グループ filter shared by the 「撮像依頼」, 「撮像計画ファイル出力」 and
「撮像予定」 docks (core/request/groups.py): すべてのグループ, 自分のグループ
(the groups whose MEMBERS hold the user name), each group, グループなし. The
choice is kept per user and dock. The box is hidden while there are no
groups at all (no table and no request with one), unless a choice kept
from before still filters.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QComboBox

from ..core import edit_stamp
from ..core.request.groups import ALL, MINE, NO_GROUP, RequestGroup, listed, matches, mine, same
from ..qgis_io import settings
from .docks import plain_tip

#: The item of the request form and the bulk edit that takes a request out of its group.
NO_GROUP_TEXT = "（グループなし）"


class GroupFilter(QComboBox):
    """The 依頼グループ filter of one dock (`where`: the key its choice is kept under)."""

    def __init__(self, where: str, parent=None):
        super().__init__(parent)
        self.where = where
        self.groups: list[RequestGroup] = []
        #: The user's groups (for 自分のグループ).
        self.my: tuple = ()
        self._kept: Optional[str] = settings.group_filter(where) or None
        self._filled = False
        self._seen: Optional[frozenset] = None
        self.setToolTip(self.tr("依頼グループで絞り込み（自分のグループ: 依頼グループのテーブルでメンバーに自分のユーザー名があるもの）。次回も同じ選択"))
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.currentIndexChanged.connect(self._chosen)
        self.set_groups([], ())

    def set_groups(self, groups: Sequence[RequestGroup], seen: Iterable[str] = (), user: Optional[str] = None,
                   force: bool = True) -> None:
        """
        The choices: the table's groups and those found on the requests or
        rows shown (`seen`), the choice kept. Without `force`, nothing is done
        while the groups and the names seen are as last time.
        """
        seen = frozenset(s.strip() for s in seen if s and s.strip())
        if not force and self._filled and list(groups) == self.groups and seen == self._seen:
            return
        self.groups, self._seen = list(groups), seen
        user = edit_stamp.user_name() if user is None else user
        self.my = tuple(mine(self.groups, user))
        was = self.currentData() if self._filled else self._kept
        names = listed(self.groups, seen)
        if was not in (ALL, MINE, NO_GROUP) and not any(same(was, n) for n in names):
            names.append(was)
        stopped = {g.name.casefold() for g in self.groups if not g.active}
        notes = {g.name.casefold(): g.note for g in self.groups if g.note}
        self.blockSignals(True)
        self.clear()
        self.addItem(self.tr("すべてのグループ"), ALL)
        if self.my or was == MINE:
            self.addItem(self.tr("自分のグループ"), MINE)
            self.setItemData(self.count() - 1, plain_tip("、".join(self.my) or self.tr("メンバーに自分のユーザー名（{u}）があるグループなし").format(
                u=user or self.tr("不明"))), Qt.ItemDataRole.ToolTipRole)
        for n in names:
            self.addItem(n + (self.tr("（使用停止）") if n.casefold() in stopped else ""), n)
            if notes.get(n.casefold()):
                self.setItemData(self.count() - 1, plain_tip(notes[n.casefold()]), Qt.ItemDataRole.ToolTipRole)
        self.addItem(self.tr("グループなし"), NO_GROUP)
        index = next((i for i in range(self.count()) if self._is(self.itemData(i), was)), 0)
        self.setCurrentIndex(index)
        self.blockSignals(False)
        self._filled = True
        self.setVisible(bool(names) or self.value() is not ALL)

    @staticmethod
    def _is(data, value) -> bool:
        if value in (ALL, MINE, NO_GROUP) or data in (ALL, MINE, NO_GROUP):
            return data == value
        return same(data, value)

    def value(self) -> Optional[str]:
        """ALL (None), MINE, NO_GROUP or a group's name."""
        return self.currentData() if self.count() else self._kept

    def choose(self, value: Optional[str]) -> None:
        """Chooses a filter (a group's name is added when it is not listed)."""
        index = next((i for i in range(self.count()) if self._is(self.itemData(i), value)), -1)
        if index < 0:
            self._kept = value
            self._filled = False
            self.set_groups(self.groups, self._seen or ())
            return
        self.setCurrentIndex(index)

    def passes(self, group: str) -> bool:
        """Whether a request (or a row) of this group is shown."""
        return matches(group or "", self.value(), self.my)

    def _chosen(self, _index: int) -> None:
        self._kept = self.value()
        settings.set_group_filter(self.where, self._kept or "")
