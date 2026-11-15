"""
担当 (core/roles.py) in QGIS: the roles the person ticked, kept in the profile
(QgsSettings `mikage/roles`, so the menus are right at once on the next
start) and in the settings file's Esri table `userRoles` under the person's
user name (so they follow the person to any PC).

* Ticked in 「最初の設定」/「担当と個人設定…」 (`choose`): the profile at once,
  then the table in a task. Until the table has them (not set in the settings
  file, or the server did not answer) they are marked to be sent
  (`mikage/rolesPending`), and sent again on the next start.
* On each start (a read ahead, after QGIS has started): what is waiting is
  sent; else the table is read, and what it holds for the person is used
  (an administrator may change a person's rows there).

The table not answering never takes roles away: the ones kept in the profile
stay in use.
"""
from __future__ import annotations

import json
from typing import Callable, Optional, Sequence

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..core import edit_stamp, roles
from .tasks import run_task

KEY = "mikage/roles"
PENDING_KEY = "mikage/rolesPending"
#: Before 2026-11-15: one role (a layout's name) chosen.
OLD_KEY = "mikage/role"


def stored_codes() -> list[str]:
    """The roles kept in this profile (the one role of before, too)."""
    s = QgsSettings()
    raw = s.value(KEY, None)
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except ValueError:
            value = []
        return roles.clean_codes(value if isinstance(value, list) else [])
    old = s.value(OLD_KEY, "")
    return roles.codes_of([old]) if isinstance(old, str) and old else []


def store_codes(codes: Sequence[str]) -> None:
    QgsSettings().setValue(KEY, json.dumps(roles.clean_codes(codes)))


def chosen_before() -> bool:
    """Whether roles were ever chosen in this profile (none ticked counts)."""
    s = QgsSettings()
    return s.contains(KEY) or bool(s.value(OLD_KEY, ""))


def pending() -> bool:
    return QgsSettings().value(PENDING_KEY, "") == "1"


def set_pending(on: bool) -> None:
    QgsSettings().setValue(PENDING_KEY, "1" if on else "")


class RoleKeeper(QObject):
    """The person's roles (`codes`), what they allow, and their row in the settings file's table."""

    #: The roles in use changed (chosen here, or read from the table).
    changed = pyqtSignal()
    #: A message for the person (the table could not be written or read).
    said = pyqtSignal(str)

    def __init__(self, context, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.context = context
        self._codes = stored_codes()
        if not QgsSettings().contains(KEY) and self._codes:
            # The one role chosen before: kept as the roles, and sent to the table like a new choice.
            store_codes(self._codes)
            set_pending(True)
        #: Why the table could not be read or written last ("" when it could).
        self.failure = ""
        #: Whether the roles in use are the ones the table holds (read or written in this run).
        self.in_table = False
        self.busy = False
        self._tasks: list = []

    # ---- what is in use --------------------------------------------------------------------------------

    @property
    def codes(self) -> list[str]:
        return list(self._codes)

    @property
    def names(self) -> list[str]:
        return roles.names_of(self._codes)

    @property
    def key(self) -> str:
        """The roles as one name (「撮像計画の担当 ＋ 管理者」; "" for none): what the toolbar's own choice is kept under."""
        return " ＋ ".join(self.names)

    def allows(self, feature: str) -> bool:
        return roles.allows(feature, self._codes)

    @property
    def source(self):
        return getattr(self.context.config, "user_roles", None)

    @staticmethod
    def user() -> str:
        return edit_stamp.user_name()

    def where_kept(self) -> str:
        """For the setup page: where the choice is kept, and whether it is there yet."""
        if self.source is None:
            return "この PC だけに保存（設定ファイルに担当の表 userRoles がないため）"
        user = self.user() or "（利用者名なし）"
        if pending():
            return f"Esri の担当の表に「{user}」として保存（未送信。次の起動か「適用」で送る）"
        return f"Esri の担当の表に「{user}」として保存（別の PC でも同じ担当）"

    def _use(self, codes: Sequence[str]) -> None:
        codes = roles.clean_codes(codes)
        same = codes == self._codes
        self._codes = codes
        store_codes(codes)
        if not same:
            self.changed.emit()

    # ---- choosing --------------------------------------------------------------------------------------

    def choose(self, codes: Sequence[str]) -> None:
        """The roles ticked: in use at once, kept here, and sent to the table."""
        self._use(codes)
        set_pending(True)
        self.push()

    def _start(self, description: str, work, done, failed) -> None:
        task = run_task(description, work, done, failed)
        self._tasks.append(task)
        for signal in (task.taskCompleted, task.taskTerminated):
            signal.connect(lambda task=task: self._tasks.remove(task) if task in self._tasks else None)

    def push(self, then: Optional[Callable[[], None]] = None, failed: Optional[Callable[[str], None]] = None) -> bool:
        """Sends the roles in use to the table (a task); False without a table."""
        source = self.source
        if source is None:
            return False
        client, user, codes = self.context.esri(), self.user(), self.codes
        token = self.context.token_for(source.url, source.token)
        self.busy = True

        def work(_task):
            return roles.save_user_roles(client, source, user, codes, token)

        def done(_codes) -> None:
            self.busy = False
            if codes == self._codes:
                set_pending(False)
                self.failure, self.in_table = "", True
            else:  # chosen again meanwhile: that choice is on its way
                self.push()
            if then is not None:
                then()

        def not_done(message: str) -> None:
            self.busy = False
            self.failure = message
            self.said.emit(f"担当を Esri の担当の表に保存できませんでした（この PC では選んだ担当で動きます。次の起動でもう一度送ります）: {message}")
            if failed is not None:
                failed(message)

        self._start("担当の保存", work, done, not_done)
        return True

    # ---- reading ---------------------------------------------------------------------------------------

    def load(self, then: Optional[Callable[[], None]] = None, failed: Optional[Callable[[str], None]] = None) -> bool:
        """
        What the table holds for the person, in use (a task); what is waiting is
        sent instead. False without a table (a read ahead: nothing to read).
        """
        source = self.source
        if source is None:
            return False
        if pending():
            return self.push(then, failed)
        client, user = self.context.esri(), self.user()
        token = self.context.token_for(source.url, source.token)
        self.busy = True

        def work(_task):
            return roles.load_user_roles(client, source, user, token)

        def done(codes) -> None:
            self.busy = False
            if source is not self.source or pending():
                # Another settings file, or chosen meanwhile (that choice is the one to keep).
                if then is not None:
                    then()
                return
            self.failure, self.in_table = "", True
            self._use(codes)
            if then is not None:
                then()

        def not_done(message: str) -> None:
            self.busy = False
            self.failure = message
            if failed is not None:
                failed(message)

        self._start("担当の読み込み", work, done, not_done)
        return True
