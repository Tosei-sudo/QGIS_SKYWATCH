"""
「依頼グループ」 (tsudo, 2026-11-23: 「撮像依頼のグループをミカゲから追加、編集できる機能が欲しい」):
the groups of the 撮像依頼 store's table (`requestStores[].groups`, TBL_REQUEST_GROUP,
core/request/groups.py) in a list, and their form.

The list shows each group's members, how many requests of the ledger are
in it, whether it is in use and its note; the user's own groups in bold.
新規・編集・上へ・下へ・削除 are for those who ask for images or plan
(`request_groups.edit`, as adding requests); the others only look.

- A new group starts with the user as its member.
- A group no longer used is stopped (使用停止) in its form, not deleted: new
  requests can no longer choose it, the filters still list it. Only a
  group no request of the ledger is in can be deleted.
- Renaming a group carries the new name to the ledger's requests of it
  (the history keeps the name it was written with).
- Each save goes to the table in a task; the groups read again are handed
  to the docks that filter by group (`changed`), so that their filters and
  the request form offer them at once.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
    QPushButton, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from ..core import edit_stamp
from ..core.request.api import (
    GROUP_NAME_MAX, GROUP_NOTE_MAX, MEMBERS_MAX, RequestGroup, RequestRepository, RequestStoreConfig, check_group, members_text,
    order_changes, same, users_of,
)
from ..qgis_io.tasks import run_task
from .docks import ask_no, plain_label, plain_tip
from .fit import fit_text
from .list_views import column_menus

COLUMNS = ("グループ名", "メンバー", "依頼", "状態", "メモ")
IN_USE, STOPPED = "使用中", "使用停止"
#: The feature of 新規・編集・上へ・下へ・削除 (core/roles.FEATURES).
EDIT = "request_groups.edit"
_MAP_SERVICE = re.compile(r"/MapServer/\d+/?$", re.IGNORECASE)


def can_edit_table(url: Optional[str]) -> bool:
    """Whether the groups table can be written (a feature service's, not a map service's)."""
    return bool(url) and not _MAP_SERVICE.search(url.split("?", 1)[0])


class RequestGroupForm(QDialog):
    """One group: its name, members, whether it is in use, and its note."""

    def __init__(self, groups: list, group: Optional[RequestGroup] = None, count: Optional[int] = None,
                 users: Optional[list] = None, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.groups = list(groups)
        self.original = group
        self.count = count
        self.user = edit_stamp.user_name()
        self.setWindowTitle(self.tr("依頼グループを作成") if group is None else self.tr("依頼グループを編集"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.name = QLineEdit(group.name if group else "")
        self.name.setMaxLength(GROUP_NAME_MAX)
        self.name.setPlaceholderText(self.tr("第1班、港湾チーム など"))
        self.name.setToolTip(self.tr("依頼の入力画面と一覧の絞り込みに出る名前"))
        self.name.textChanged.connect(lambda _t: self._renamed_note())
        form.addRow(self.tr("グループ名"), self.name)
        members = QHBoxLayout()
        start = group.users if group else ((self.user,) if self.user else ())
        self.members = QLineEdit(members_text(start))
        self.members.setMaxLength(MEMBERS_MAX)
        self.members.setPlaceholderText(self.tr("tsudo, yamada"))
        self.members.setToolTip(self.tr("メンバーのユーザー名（Windows のサインイン名）をカンマ区切りで入力。"
                                        "メンバーには、このグループが「自分のグループ」として出る"))
        members.addWidget(self.members, 1)
        self.me_button = QPushButton(self.tr("自分を追加"))
        self.me_button.setToolTip(self.tr("自分のユーザー名（{u}）をメンバーに追加").format(u=self.user or self.tr("不明")))
        self.me_button.setEnabled(bool(self.user))
        self.me_button.clicked.connect(lambda: self.add_member(self.user))
        members.addWidget(self.me_button)
        self.pick_button = QToolButton()
        self.pick_button.setText(self.tr("候補"))
        self.pick_button.setToolTip(self.tr("担当を選んだ人のユーザー名から追加"))
        self.pick_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.pick_menu = QMenu(self.pick_button)
        self.pick_button.setMenu(self.pick_menu)
        self.pick_menu.aboutToShow.connect(self._fill_picks)
        self.users = list(users or [])
        self.pick_button.setVisible(bool(self.users))
        members.addWidget(self.pick_button)
        holder = QWidget()
        holder.setLayout(members)
        members.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.tr("メンバー"), holder)
        self.active = QCheckBox(self.tr("使用中（新しい依頼で選べる）"))
        self.active.setToolTip(self.tr("外すと使用停止。新しい依頼では選べなくなる。一覧の絞り込みには残る"))
        self.active.setChecked(group.active if group else True)
        form.addRow(self.tr("状態"), self.active)
        self.note = QLineEdit(group.note if group else "")
        self.note.setMaxLength(GROUP_NOTE_MAX)
        self.note.setPlaceholderText(self.tr("担当する依頼の内容など"))
        form.addRow(self.tr("メモ"), self.note)
        self.renamed = plain_label(QLabel())
        self.renamed.setWordWrap(True)
        self.renamed.setStyleSheet("color: #b45309")
        layout.addWidget(self.renamed)
        self.problem = plain_label(QLabel())
        self.problem.setStyleSheet("color: #b91c1c")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("保存"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("取消"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._renamed_note()
        self.resize(560, self.sizeHint().height())

    def add_member(self, user: str) -> None:
        """Adds a user name to the members (once)."""
        user = (user or "").strip()
        now = users_of(self.members.text())
        if user and not any(same(user, u) for u in now):
            self.members.setText(members_text(now + (user,)))

    def _fill_picks(self) -> None:
        self.pick_menu.clear()
        now = users_of(self.members.text())
        for u in self.users:
            action = self.pick_menu.addAction(u)
            action.setEnabled(not any(same(u, x) for x in now))
            action.triggered.connect(lambda _on=False, u=u: self.add_member(u))

    def renaming(self) -> bool:
        """Whether the group edited is given another name."""
        return self.original is not None and not same(self.original.name, self.name.text())

    def _renamed_note(self) -> None:
        text = ""
        if self.renaming() and self.name.text().strip():
            if self.count:
                text = self.tr("保存すると、このグループの依頼 {n} 件のグループ名も「{new}」に変更（出力履歴は書き出したときの名前のまま）").format(
                    n=self.count, new=self.name.text().strip())
            else:
                text = self.tr("出力履歴は書き出したときの名前のまま")
        self.renamed.setText(text)
        self.renamed.setVisible(bool(text))

    def group(self) -> RequestGroup:
        """The group as the form says (with the rows and order of the one edited)."""
        o = self.original
        return RequestGroup(name=self.name.text().strip(), users=users_of(self.members.text()),
                            order=o.order if o else None, active=self.active.isChecked(), note=self.note.text().strip(),
                            object_ids=o.object_ids if o else ())

    def _accept(self) -> None:
        problems = check_group(self.group(), self.groups)
        if problems:
            self.problem.setText("、".join(problems))
            return
        self.accept()


class RequestGroupDialog(QDialog):
    """The groups of a 撮像依頼 store's table: the list, and 新規・編集・上へ・下へ・削除 for those who ask for images or plan."""

    def __init__(self, context, store_of: Callable[[], Optional[RequestStoreConfig]],
                 changed: Callable[[RequestStoreConfig, list, bool], None] = lambda _s, _g, _r: None,
                 allows: Callable[[str], bool] = lambda _f: True, parent=None):
        super().__init__(parent)
        fit_text(self)
        self.context = context
        self.store_of = store_of
        self.changed = changed
        self.allows = allows
        self.task = None
        #: The store read last (the 撮像依頼 dock's), its groups, the requests in each (None: not read) and the users known.
        self.store: Optional[RequestStoreConfig] = None
        self.groups: list[RequestGroup] = []
        self.counts: Optional[dict] = None
        self.users: list[str] = []
        #: Whether the table has the 表示順 attribute (上へ・下へ).
        self.can_order = False
        #: What was just saved, said again once read again.
        self.note = ""
        #: Requests of the ledger were put in a renamed group: told to the docks once the groups are read again.
        self._requests_moved = False
        self.form: Optional[RequestGroupForm] = None
        self.setWindowTitle(self.tr("依頼グループ"))
        self.resize(820, 440)
        self._build()
        self.render()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        self.status = plain_label(QLabel())
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.model = QStandardItemModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        self.table.doubleClicked.connect(lambda _i: self.edit())
        layout.addWidget(self.table, 1)
        self.columns = column_menus("request_groups", self.table, self)
        row = QHBoxLayout()
        self.add_button = QPushButton(self.tr("新規…"))
        self.add_button.setToolTip(self.tr("依頼グループを作成（自分がメンバーに入った状態で開始）"))
        self.add_button.clicked.connect(lambda: self.add())
        self.edit_button = QPushButton(self.tr("編集…"))
        self.edit_button.setToolTip(self.tr("選んだグループの名前・メンバー・使用中か・メモを変更"))
        self.edit_button.clicked.connect(lambda: self.edit())
        self.up_button = QPushButton(self.tr("上へ"))
        self.up_button.setToolTip(self.tr("選んだグループを一覧と入力画面で 1 つ上に表示"))
        self.up_button.clicked.connect(lambda: self.move_group(-1))
        self.down_button = QPushButton(self.tr("下へ"))
        self.down_button.setToolTip(self.tr("選んだグループを一覧と入力画面で 1 つ下に表示"))
        self.down_button.clicked.connect(lambda: self.move_group(1))
        self.delete_button = QPushButton(self.tr("削除…"))
        self.delete_button.setToolTip(self.tr("依頼が 1 件もないグループを削除（使わなくなったグループは「編集…」で使用停止に変更）"))
        self.delete_button.clicked.connect(lambda: self.delete())
        for b in (self.add_button, self.edit_button, self.up_button, self.down_button, self.delete_button):
            row.addWidget(b)
        row.addStretch(1)
        self.reload_button = QPushButton(self.tr("最新を取得"))
        self.reload_button.clicked.connect(lambda: self.run())
        row.addWidget(self.reload_button)
        close = QPushButton(self.tr("閉じる"))
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)
        self._update_buttons()

    def repository(self) -> RequestRepository:
        return RequestRepository(self.context.esri(), self.store, None)

    def _missing(self) -> str:
        """Why there is no list to show (no store or no groups table); "" when there is."""
        if self.store is None:
            return self.tr("撮像依頼の保存先が未設定（管理者に連絡: requestStores）［E703］")
        if not self.store.groups:
            return self.tr("依頼グループの表が未設定（管理者に連絡: requestStores[].groups）［E703］")
        return ""

    # --- Reading ----------------------------------------------------------------------------------

    def run(self) -> None:
        """Gets the groups again (of the 撮像依頼 dock's store as it is now), with the requests in each."""
        if self.task is not None:
            return
        store = self.store_of()
        if store is not self.store:
            self.groups, self.counts, self.users = [], None, []
        self.store = store
        if self._missing():
            self.render()
            return
        repo = self.repository()
        roles = self.context.config.user_roles
        client = self.context.esri()
        roles_token = None
        self.status.setText(self.tr("依頼グループを取得中…"))

        def work(task):
            groups = repo.load_groups(task.isCanceled)
            fields = repo.group_table_fields()
            try:
                counts = repo.group_counts(task.isCanceled)
            except Exception:  # noqa: BLE001 (the list without the counts; deleting then asks the ledger)
                counts = None
            users = []
            if roles is not None:
                from ..core.roles import known_users

                try:
                    users = known_users(client, roles, roles_token)
                except Exception:  # noqa: BLE001 (only hints)
                    users = []
            return groups, fields, counts, users

        def done(result):
            self.task = None
            groups, fields, counts, users = result
            self.groups, self.counts, self.users = list(groups), counts, list(users)
            self.can_order = store.group_fields.get("order", "") in fields
            self.render()
            moved, self._requests_moved = self._requests_moved, False
            self.changed(store, self.groups, moved)

        def failed(message):
            self.task = None
            self.note = ""
            self.status.setText(self.tr("取得に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("依頼グループの取得"), work, done, failed)
        self._update_buttons()

    # --- Showing ----------------------------------------------------------------------------------

    def count_of(self, g: RequestGroup) -> Optional[int]:
        return None if self.counts is None else self.counts.get(g.name.strip().casefold(), 0)

    def render(self) -> None:
        chosen = {g.name.casefold() for g in self.selected()}
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        me = edit_stamp.user_name()
        bold = QFont()
        bold.setBold(True)
        for g in self.groups:
            count = self.count_of(g)
            cells = [g.name, members_text(g.users), "?" if count is None else str(count), IN_USE if g.active else STOPPED, g.note]
            items = [QStandardItem(c) for c in cells]
            if g.has(me):
                items[0].setFont(bold)
                items[0].setToolTip(plain_tip(self.tr("自分（{u}）がメンバーのグループ").format(u=me)))
            items[1].setToolTip(plain_tip(cells[1]))
            items[2].setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items[2].setToolTip(self.tr("台帳でこのグループに入っている依頼の件数") if count is not None
                                else self.tr("依頼の件数を取得できない"))
            if not g.active:
                for item in items:
                    item.setForeground(QBrush(QColor("#6b7280")))
            items[4].setToolTip(plain_tip(cells[4]))
            self.model.appendRow(items)
        for r, g in enumerate(self.groups):
            if g.name.casefold() in chosen:
                self.table.selectRow(r)
        missing = self._missing()
        if missing:
            text = missing
        else:
            used = sum(1 for g in self.groups if g.active)
            text = self.tr("依頼グループ {n} 件（使用中 {m} 件）").format(n=len(self.groups), m=used)
            if self.store is not None and len(self.context.config.request_stores) > 1:
                text = f"{self.store.label}: {text}"
            if self.allows(EDIT) and not can_edit_table(self.store.groups):
                text += self.tr("。依頼グループの表がマップサービスなので変更できない（管理者に連絡: requestStores[].groups）")
        self.status.setText("。".join(t for t in (self.note, text) if t))
        self.note = ""
        self._update_buttons()

    def selected(self) -> list[RequestGroup]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.groups[r] for r in rows if 0 <= r < len(self.groups)]

    def editing(self) -> bool:
        """Whether this user may change the groups here (their role, and a table that takes writes)."""
        return self.allows(EDIT) and not self._missing() and can_edit_table(self.store.groups if self.store else None)

    def _update_buttons(self) -> None:
        editing = self.editing()
        for b in (self.add_button, self.edit_button, self.up_button, self.down_button, self.delete_button):
            b.setVisible(editing)
        idle = self.task is None
        chosen = self.selected()
        one = len(chosen) == 1
        at = self.groups.index(chosen[0]) if one and chosen[0] in self.groups else -1
        self.reload_button.setEnabled(idle and not self._missing())
        self.add_button.setEnabled(idle)
        self.edit_button.setEnabled(idle and one)
        self.up_button.setEnabled(idle and self.can_order and at > 0)
        self.down_button.setEnabled(idle and self.can_order and 0 <= at < len(self.groups) - 1)
        self.delete_button.setEnabled(idle and bool(chosen))
        if not self.can_order and self.groups:
            for b in (self.up_button, self.down_button):
                b.setToolTip(self.tr("依頼グループの表に表示順の項目がないので並べ替えできない（管理者に連絡: SORT_ORDER）"))

    # --- 新規・編集・上へ・下へ・削除 -------------------------------------------------------------

    def _form(self, group: Optional[RequestGroup] = None) -> RequestGroupForm:
        form = RequestGroupForm(self.groups, group, self.count_of(group) if group else None, self.users, self)
        self.form = form
        return form

    def add(self) -> Optional[RequestGroupForm]:
        if not self.editing() or self.task is not None:
            return None
        form = self._form()
        # Put after the others (表示順 one past the last) when the table keeps an order.
        last = max((g.order for g in self.groups if g.order is not None), default=0.0)

        def save():
            g = form.group()
            if self.can_order:
                g = RequestGroup(g.name, g.users, float(int(last) + 1), g.active, g.note)
            self._save(g, None, self.tr("「{name}」を作成").format(name=g.name))

        form.accepted.connect(save)
        form.open()
        return form

    def edit(self) -> Optional[RequestGroupForm]:
        chosen = self.selected()
        if len(chosen) != 1 or not self.editing() or self.task is not None:
            return None
        before = chosen[0]
        form = self._form(before)

        def save():
            g = form.group()
            what = (self.tr("「{old}」を「{new}」に変更").format(old=before.name, new=g.name) if form.renaming()
                    else self.tr("「{name}」を更新").format(name=g.name))
            self._save(g, before, what)

        form.accepted.connect(save)
        form.open()
        return form

    def move_group(self, step: int) -> None:
        """Moves the group chosen up (-1) or down (+1) in the order."""
        chosen = self.selected()
        if len(chosen) != 1 or not self.editing() or not self.can_order or self.task is not None or chosen[0] not in self.groups:
            return
        changes = order_changes(self.groups, self.groups.index(chosen[0]), step)
        if not changes:
            return
        repo, name = self.repository(), chosen[0].name

        def work(task):
            return repo.save_group_orders(changes)

        def done(result):
            self.task = None
            saved, problems = result
            if saved == len(changes):
                # Shown in the new order at once (read again below), the same group still chosen.
                by = {g.name.casefold(): g for g in changes}
                self.groups = sorted((by.get(g.name.casefold(), g) for g in self.groups),
                                     key=lambda g: (g.order is None, g.order or 0, g.name.casefold()))
                self.render()
                self._choose(name)
            self.note = "、".join(problems[:3]) if problems else ""
            self.run()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("並べ替えに失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("依頼グループの並べ替え"), work, done, failed)
        self._update_buttons()

    def _choose(self, name: str) -> None:
        for r, g in enumerate(self.groups):
            if same(g.name, name):
                self.table.selectRow(r)

    def delete(self, ask: bool = True) -> None:
        chosen = self.selected()
        if not chosen or not self.editing() or self.task is not None:
            return
        used = [g for g in chosen if (self.count_of(g) or 0) > 0]
        if used:
            names = "、".join(f"{g.name}（{self.count_of(g)} 件）" for g in used[:5])
            self.status.setText(self.tr("依頼があるグループは削除できない: {names}。使わなくなったグループは「編集…」で「使用中」を外して使用停止に変更").format(
                names=names))
            return
        names = "、".join(g.name for g in chosen[:5]) + (self.tr(" ほか {n} 件").format(n=len(chosen) - 5) if len(chosen) > 5 else "")
        if ask and not ask_no(self, self.tr("依頼グループの削除"), self.tr(
                "選んだ {n} 件の依頼グループ（{names}）を削除（元に戻せない）").format(n=len(chosen), names=names), yes=self.tr("削除")):
            return
        repo = self.repository()
        unknown = self.counts is None

        def work(task):
            if unknown:
                # The counts could not be got with the list: asked now, so that a group in use is never deleted.
                counts = repo.group_counts(task.isCanceled)
                in_use = [g for g in chosen if counts.get(g.name.strip().casefold(), 0) > 0]
                if in_use:
                    return [], [f"{g.name}: 依頼があるので削除できない" for g in in_use]
            return repo.delete_groups(chosen)

        def done(result):
            self.task = None
            gone, problems = result
            self.note = "。".join(t for t in (self.tr("{n} 件を削除").format(n=len(gone)) if gone else "", "、".join(problems[:3])) if t)
            self.run()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("削除に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("依頼グループの削除"), work, done, failed)
        self._update_buttons()

    def _save(self, group: RequestGroup, before: Optional[RequestGroup], what: str) -> None:
        if self.task is not None or self._missing():
            return
        repo = self.repository()
        renamed = before is not None and not same(before.name, group.name)

        def work(task):
            saved = repo.save_group(group)
            moved = repo.rename_requests(before.name, saved.name, edit_stamp.now_ms, task.isCanceled) if renamed else None
            return saved, moved

        def done(result):
            self.task = None
            saved, moved = result
            parts = [what]
            if moved is not None and (moved.saved or moved.failed):
                parts.append(self.tr("依頼のグループ名: {t}").format(t=moved.text()))
            self.note = "・".join(parts)
            self._requests_moved = self._requests_moved or bool(moved is not None and moved.saved)
            self.run()

        def failed(message):
            self.task = None
            self.status.setText(self.tr("保存に失敗: {m}").format(m=message))
            self._update_buttons()

        self.task = run_task(self.tr("依頼グループの保存"), work, done, failed)
        self._update_buttons()

    def close_all(self) -> None:
        if self.form is not None:
            try:
                self.form.close()
            except RuntimeError:
                pass
        self.close()
