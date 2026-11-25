"""
The plugin's own entries (MENU_TOP): 「担当と個人設定…」「ツールバーのボタン…」「ドックのレイアウト」
「ドックを初期位置に戻す」「接続と表の点検…」「監査ログ…」「使い方（ヘルプ）…」「Python API リファレンス…」
「設定…」, the settings file's problems in the message bar and the lists' column drag (`columnDrag`).
"""
from __future__ import annotations

from typing import Optional

from qgis.core import Qgis
from qgis.PyQt.QtWidgets import QPushButton

from ..core import errors
from ..qgis_io import settings
from ..ui import error_help, first_setup
from ..ui.dock_layouts import DockLayoutMenu
from ..ui.list_views import set_column_drag
from ..ui.settings_dialog import SettingsDialog
from ..ui.toolbar_buttons import ToolbarButtonsDialog
from .base import Feature, close, icon, tr

#: The dialogs closed when the roles in use no longer have their entry (担当).
ROLE_DIALOGS = (("health_dialog", "接続と表の点検…"), ("api_reference", "Python API リファレンス…"))


class Platform(Feature):
    COMMANDS = ("open_settings", "open_first_setup", "open_toolbar_buttons", "open_health_check", "open_audit_log", "open_help",
                "open_api_reference")

    def __init__(self, host):
        super().__init__(host)
        #: The dialogs and menus of the plugin's own entries, once opened.
        host.setup_dialog = None
        host.toolbar_dialog = None
        host.dock_layouts = None
        host.help_action = None
        host.help_dialog = None
        host.api_reference = None
        host.health_dialog = None
        self._help_menu = None
        self._problems_shown: Optional[tuple] = None
        self._bar_help: Optional[error_help.BarHelp] = None

    def start(self) -> None:
        # `columnDrag`: the lists' columns dragged by their header or only moved from its menu.
        self.context.configChanged.connect(self._column_drag_changed)
        # The docks no longer show the settings file's state: problems go to the message bar, once per file and list.
        self.context.configChanged.connect(self._config_problems)
        # エラーコード: 「対処法」 on the message bar's items with one, and a click on a dock's status line (ui/error_help.py).
        error_help.set_opener(self.open_help)
        self._bar_help = error_help.BarHelp(self.iface.messageBar(), self.window())

    def add_actions(self) -> None:
        host, window = self.host, self.window()
        self.entry("dock_layout", "ドックを初期位置に戻す", "place_docks")
        # 「ドックのレイアウト」: named layouts per kind of work (ui/dock_layouts.py).
        host.dock_layouts = DockLayoutMenu(window, lambda: host.allowed_docks(),
                                           lambda text: self.iface.messageBar().pushSuccess(tr("ドックのレイアウト"), text), window,
                                           floating=first_setup.docks_floating)
        layouts = host.dock_layouts.menuAction()
        layouts.setIcon(icon("dock_layouts"))
        host.add_entry(layouts)
        # 「担当と個人設定」: the role's docks, docks in windows of their own, and the person's settings (ui/first_setup.py).
        self.entry("first_setup", "担当と個人設定…", "open_first_setup")
        # 「ツールバーのボタン…」: the toolbar's buttons for the role, and the person's own (ui/toolbar_buttons.py).
        self.entry("toolbar_buttons", "ツールバーのボタン…", "open_toolbar_buttons", name="SatObsToolbarButtons")
        # 「接続と表の点検」: every ArcGIS service of the settings file asked once (ui/health_dialog.py).
        self.entry("health_check", "接続と表の点検…", "open_health_check")
        # 「監査ログ」: what is recorded where and what was sent (ui/audit_status.py).
        self.entry("audit_log", "監査ログ…", "open_audit_log")
        # 「使い方（ヘルプ）」: the help for everyday users, bundled HTML (help/index.html, ui/help_dialog.py).
        host.help_action = self.entry("help", "使い方（ヘルプ）…", "open_help", name="SatObsHelp")
        # Also in QGIS's Help ▸ Plugins.
        help_menu = self.iface.pluginHelpMenu() if hasattr(self.iface, "pluginHelpMenu") else None
        if help_menu is not None:
            help_menu.addAction(host.help_action)
        self._help_menu = help_menu
        # 「Python API リファレンス」: plugin.api in Japanese with examples (ui/api_reference_dialog.py).
        self.entry("api_reference", "Python API リファレンス…", "open_api_reference", name="SatObsApiReference")
        self.entry("settings", "設定…", "open_settings")

    def roles_changed(self) -> None:
        # Opened before, of a feature now put away.
        for name, feature in ROLE_DIALOGS:
            dialog = getattr(self.host, name, None)
            if dialog is not None and not self.host.allows(tr(feature)):
                dialog.close()

    # ---- the settings file ------------------------------------------------------------------------------

    def open_settings(self) -> None:
        # Another settings file than the one in use: the administrators' (the first one: anyone's).
        dialog = SettingsDialog(self.context, self.window(), may_change_source=self.host.allows("settings.config"),
                                raster_speed=self.host.open_raster_speed)
        try:
            if dialog.exec() and self.context is not None:
                # The 記録者 also in an open 「観測記録」 (its box and the name written on the records).
                obs = self.host.ports.observations()
                if obs is not None and hasattr(obs, "set_observer"):
                    obs.set_observer(settings.recorder_shown())
        finally:
            # Made anew each time: the closed one goes, with its auth lists (#87).
            dialog.deleteLater()

    def _config_problems(self) -> None:
        """「設定ファイルの問題 n 件」 in the message bar after a read that found any (the list is in 「設定…」)."""
        c = self.context
        if c is None or c.loading:
            return
        key = (c.source, tuple(c.problems))
        if not c.problems:
            self._problems_shown = None
            return
        if key == self._problems_shown:
            return
        self._problems_shown = key
        bar = self.iface.messageBar()
        # The エラーコード: the first problem's own (a file that cannot be read, E702; one not fetched, E1xx), else E701.
        code = errors.code_of(c.problems[0]) or "E701"
        item = bar.createMessage(tr("設定ファイル"), errors.tagged(tr("問題 {n} 件（一覧はメニュー「設定…」）").format(n=len(c.problems)), code))
        button = QPushButton(tr("設定…"), item)
        button.clicked.connect(self.open_settings)
        item.layout().addWidget(button)
        item.setToolTip("\n".join(c.problems))
        bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)

    def _column_drag_changed(self) -> None:
        if self.context is not None:
            set_column_drag(self.context.config.column_drag)

    # ---- the plugin's own dialogs ------------------------------------------------------------------------

    def open_help(self, section: Optional[str] = None):
        """「使い方（ヘルプ）…」: the help for everyday users (not blocking QGIS), at a section (its id) if given."""
        from ..ui.help_dialog import HelpDialog

        host = self.host
        if host.help_dialog is None:
            host.help_dialog = HelpDialog(host.run_menu_entry, self.window())
        host.help_dialog.show()
        host.help_dialog.raise_()
        if section:
            host.help_dialog.go(section)
        return host.help_dialog

    def open_api_reference(self):
        """「Python API リファレンス…」: what plugin.api has, with examples (not blocking QGIS)."""
        from ..ui.api_reference_dialog import ApiReferenceDialog

        host = self.host
        if host.api_reference is None:
            host.api_reference = ApiReferenceDialog(host.api, self.window())
        host.api_reference.show()
        host.api_reference.raise_()
        return host.api_reference

    def open_audit_log(self):
        """「監査ログ…」: what is recorded where, what was sent, sending now and checking the folder."""
        return self.host.audit.open_dialog() if self.host.audit is not None else None

    def open_health_check(self):
        """「接続と表の点検…」: the dialog (not blocking QGIS), checking right away."""
        from ..ui.health_dialog import HealthDialog

        host = self.host
        if self.context is None:
            return None
        if host.health_dialog is None:
            host.health_dialog = HealthDialog(self.context, self.window())
        host.health_dialog.show()
        host.health_dialog.raise_()
        host.health_dialog.run()
        return host.health_dialog

    def open_first_setup(self, first: bool = False) -> Optional[first_setup.FirstSetupDialog]:
        """「最初の設定」 (first) or 「担当と個人設定」: the page, open beside the main window (not blocking it)."""
        host = self.host
        if self.context is None:  # unloaded meanwhile
            return None
        if host.setup_dialog is not None:
            host.setup_dialog.deleteLater()
        host.setup_dialog = first_setup.FirstSetupDialog(
            self.window(), lambda: host.allowed_docks(), first=first,
            observation_dock=host.ports.observations, coordinates=lambda: host.coordinates,
            open_settings=self.open_settings, keeper=host.role_keeper, all_docks=lambda: host.docks,
            text_assist=getattr(self.context, "text_assist", None),
            # 「まず練習」: 練習モード (features/training.py), when it is there and the settings file does not hide it.
            practise=(lambda: host.start_training()) if hasattr(host, "start_training") and host.allows(tr("練習モードを始める")) else None)
        # The roles' toolbar buttons (the same roles again: the person's own buttons of them).
        host.setup_dialog.accepted.connect(lambda: host.toolbar_buttons.apply() if host.context is not None else None)
        host.setup_dialog.open()
        return host.setup_dialog

    def open_toolbar_buttons(self):
        """「ツールバーのボタン…」: the buttons for the role in use (beside the main window, not blocking it)."""
        host = self.host
        if self.context is None:
            return None
        if host.toolbar_dialog is not None:
            host.toolbar_dialog.deleteLater()
        host.toolbar_dialog = ToolbarButtonsDialog(host.toolbar_buttons, self.window())
        host.toolbar_dialog.open()
        return host.toolbar_dialog

    def unload(self) -> None:
        host = self.host
        error_help.set_opener(None)
        if self._bar_help is not None:
            self._bar_help.unload()
            self._bar_help.deleteLater()
            self._bar_help = None
        close(host.help_dialog)
        host.help_dialog = None
        if self._help_menu is not None:
            try:
                self._help_menu.removeAction(host.help_action)
            except RuntimeError:
                pass
            self._help_menu = None
        close(host.api_reference)
        host.api_reference = None
        close(host.setup_dialog)
        host.setup_dialog = None
        close(host.toolbar_dialog)
        host.toolbar_dialog = None
        close(host.health_dialog)
        host.health_dialog = None
        if host.dock_layouts is not None:
            host.dock_layouts.deleteLater()
            host.dock_layouts = None
